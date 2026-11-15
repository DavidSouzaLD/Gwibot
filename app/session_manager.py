"""Gerenciamento de sessão persistente e login manual interativo para o Mercado Livre.

Segurança:
- Nunca registra valores de cookies, tokens ou credenciais nos logs nem os expõe na API.
- Restringe permissões de diretório/arquivo ao usuário proprietário do processo.
- Garante exclusão mútua para que apenas um fluxo de login interativo ocorra por vez.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import stat
import subprocess
import threading
import time
from typing import Any

from playwright.sync_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    sync_playwright,
)

from app.config import SESSION_DIR, SESSION_META_FILE, SESSION_STATE_FILE, settings
from app.logger import logger
from app.models import AuthStateType, AuthStatusResponse

# Cookies emitidos pelo Mercado Livre que indicam sessão autenticada (apenas nomes são verificados)
ML_AUTH_COOKIE_NAMES = frozenset({"ssid", "orgnickp", "nsa_rotok"})

# Trechos de URL que indicam que o usuário ainda está em tela de login/verificação
AUTH_GATE_URL_PATTERNS = (
    "/gz/log_in",
    "/gz/login",
    "/jms/mlb/lgz/login",
    "/gz/account-verification",
    "/account-verification",
    "/login",
    "security-verification",
)


def _restrict_owner_permissions(path: Path) -> None:
    """Restringe permissões do arquivo/diretório apenas ao usuário proprietário."""
    try:
        if os.name == "nt":
            username = os.environ.get("USERNAME")
            if username:
                subprocess.run(
                    [
                        "icacls",
                        str(path),
                        "/inheritance:r",
                        "/grant:r",
                        f"{username}:(F)",
                    ],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        else:
            mode = (
                stat.S_IRWXU
                if path.is_dir()
                else (stat.S_IRUSR | stat.S_IWUSR)
            )
            os.chmod(path, mode)
    except Exception as exc:
        logger.warning("Não foi possível ajustar permissões restritas em %s: %s", path.name, exc)


class SessionManager:
    """Controla ciclo de vida da sessão autenticada do Mercado Livre em disco e em memória."""

    def __init__(
        self,
        session_dir: Path = SESSION_DIR,
        state_file: Path = SESSION_STATE_FILE,
        meta_file: Path = SESSION_META_FILE,
    ) -> None:
        self._session_dir = session_dir
        self._state_file = state_file
        self._meta_file = meta_file
        self._state_lock = threading.Lock()
        self._login_flow_lock = threading.Lock()
        self._login_task: asyncio.Task[None] | None = None

        self._state: AuthStateType = "logged_out"
        self._message: str = "Sessão desconectada. Faça login para evitar bloqueios de verificação."
        self._updated_at: str | None = None

        self._load_persisted_state_on_startup()

    def _ensure_secure_dir(self) -> None:
        if not self._session_dir.exists():
            self._session_dir.mkdir(parents=True, exist_ok=True)
            _restrict_owner_permissions(self._session_dir)

    def _load_persisted_state_on_startup(self) -> None:
        """Carrega e valida estruturalmente a sessão salva em disco durante a inicialização."""
        with self._state_lock:
            if not self._state_file.exists():
                self._state = "logged_out"
                self._message = "Sessão desconectada."
                return

            try:
                raw = json.loads(self._state_file.read_text(encoding="utf-8"))
                cookies = raw.get("cookies", [])
                if not isinstance(cookies, list) or not self._has_valid_auth_cookies(cookies):
                    logger.warning("Arquivo de sessão encontrado mas sem cookies de autenticação válidos.")
                    self._state = "expired"
                    self._message = "A sessão salva expirou ou é inválida. Faça login novamente."
                    return

                meta_updated = None
                if self._meta_file.exists():
                    try:
                        meta = json.loads(self._meta_file.read_text(encoding="utf-8"))
                        meta_updated = meta.get("updated_at")
                    except Exception:
                        meta_updated = None

                self._state = "logged_in"
                self._updated_at = meta_updated or datetime.now().astimezone().isoformat()
                self._message = "Sessão autenticada carregada do disco."
                logger.info(
                    "Sessão persistente carregada com sucesso (%d cookies armazenados).",
                    len(cookies),
                )
            except Exception as exc:
                logger.warning("Falha ao ler arquivo de sessão persistente: %s", exc)
                self._state = "error"
                self._message = "Arquivo de sessão corrompido. Faça login novamente."

    @staticmethod
    def _has_valid_auth_cookies(cookies: list[dict[str, Any]]) -> bool:
        """Verifica se os cookies do Mercado Livre contêm identificadores de sessão válidos e não expirados."""
        now_ts = time.time()
        found_auth_cookie = False
        for c in cookies:
            domain = str(c.get("domain", "")).lower()
            name = str(c.get("name", ""))
            expires = c.get("expires", -1)
            if "mercadolivre" not in domain and "mercadolibre" not in domain:
                continue
            if isinstance(expires, (int, float)) and expires > 0 and expires < now_ts:
                continue
            if name in ML_AUTH_COOKIE_NAMES and c.get("value"):
                found_auth_cookie = True
        return found_auth_cookie

    @staticmethod
    def is_url_auth_gate(url: str) -> bool:
        """Retorna True se a URL corresponde a uma página de login ou verificação de conta."""
        lowered = (url or "").lower()
        return any(pattern in lowered for pattern in AUTH_GATE_URL_PATTERNS)

    def get_status(self) -> AuthStatusResponse:
        """Retorna o estado atual da sessão sem expor dados sensíveis."""
        with self._state_lock:
            has_file = self._state_file.exists()
            if self._state == "logged_in" and not has_file:
                self._state = "logged_out"
                self._message = "Sessão desconectada."

            return AuthStatusResponse(
                authenticated=(self._state == "logged_in" and has_file),
                state=self._state,
                message=self._message,
                has_saved_session=has_file,
                updated_at=self._updated_at,
            )

    def get_storage_state_path(self) -> Path | None:
        """Retorna o caminho do arquivo `storage_state` se a sessão estiver ativa, ou `None`."""
        with self._state_lock:
            if self._state == "logged_in" and self._state_file.exists():
                return self._state_file
            return None

    def mark_session_expired(self, reason: str | None = None) -> None:
        """Marca a sessão como expirada e remove o arquivo de estado inválido."""
        with self._state_lock:
            self._remove_files_unlocked()
            self._state = "expired"
            self._updated_at = datetime.now().astimezone().isoformat()
            self._message = reason or (
                "Sessão expirada detectada (redirecionamento para login/verificação). "
                "Faça login novamente."
            )
        logger.warning("Sessão marcada como expirada: %s", self._message)

    def logout(self) -> AuthStatusResponse:
        """Remove os arquivos de sessão do disco e redefine o estado em memória para desconectado."""
        with self._state_lock:
            self._remove_files_unlocked()
            self._state = "logged_out"
            self._updated_at = datetime.now().astimezone().isoformat()
            self._message = "Sessão encerrada e dados locais removidos com sucesso."
        logger.info("Logout executado: arquivos de sessão removidos do disco.")
        return self.get_status()

    def _remove_files_unlocked(self) -> None:
        for fpath in (self._state_file, self._meta_file):
            try:
                if fpath.exists():
                    fpath.unlink()
            except Exception as exc:
                logger.warning("Erro ao remover arquivo de sessão %s: %s", fpath.name, exc)

    def _save_storage_state(self, storage_data: dict[str, Any]) -> None:
        """Salva o estado de sessão em disco de forma atômica e com permissões restritas."""
        self._ensure_secure_dir()
        tmp_state = self._state_file.with_suffix(".tmp")
        tmp_state.write_text(json.dumps(storage_data, ensure_ascii=False, indent=2), encoding="utf-8")
        _restrict_owner_permissions(tmp_state)
        tmp_state.replace(self._state_file)
        _restrict_owner_permissions(self._state_file)

        now_iso = datetime.now().astimezone().isoformat()
        meta = {
            "updated_at": now_iso,
            "cookie_count": len(storage_data.get("cookies", [])),
        }
        self._meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        _restrict_owner_permissions(self._meta_file)

        with self._state_lock:
            self._state = "logged_in"
            self._updated_at = now_iso
            self._message = "Sessão autenticada e validada com sucesso."

    @staticmethod
    def _is_user_logged_in_on_page(page: Page, context: BrowserContext) -> bool:
        """Verifica se o usuário concluiu o login na janela interativa."""
        current_url = page.url or ""
        if not current_url.startswith("https://"):
            return False
        if SessionManager.is_url_auth_gate(current_url):
            return False

        cookies = context.cookies()
        if not SessionManager._has_valid_auth_cookies(cookies):
            return False

        # Verifica indicadores visuais de usuário autenticado no cabeçalho do Mercado Livre
        logged_in_selectors = (
            ".nav-header-username",
            '[data-js="user-menu"]',
            'a[href*="myaccount.mercadolivre.com.br"]',
            ".nav-header-user-myml",
        )
        for sel in logged_in_selectors:
            try:
                if page.locator(sel).count() > 0:
                    return True
            except PlaywrightError:
                continue

        # Caso esteja no domínio mercadolivre.com.br fora do gate e com cookies ssid/orgnickp ativos
        if "mercadolivre.com.br" in current_url.lower():
            return True

        return False

    @staticmethod
    def _verify_protected_access(page: Page) -> bool:
        """Navega para uma página protegida e para uma busca para confirmar que não há redirecionamento de login."""
        try:
            page.goto(
                settings.ml_protected_verify_url,
                wait_until="domcontentloaded",
                timeout=settings.navigation_timeout_ms,
            )
            if SessionManager.is_url_auth_gate(page.url):
                logger.warning("Verificação pós-login falhou: página de conta redirecionou para login.")
                return False
            return True
        except PlaywrightError as exc:
            logger.warning("Erro durante verificação de página protegida pós-login: %s", exc)
            return False

    def _run_interactive_login_sync(self) -> None:
        """Fluxo síncrono que abre o Chromium visível (headed) para login manual do usuário."""
        logger.info("Iniciando fluxo de login interativo em janela visível do Chromium.")
        deadline = time.monotonic() + settings.login_timeout_seconds

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=False,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-default-browser-check",
                        "--no-first-run",
                    ],
                )
                context = browser.new_context(
                    locale=settings.locale,
                    timezone_id=settings.timezone_id,
                    user_agent=settings.user_agent,
                    viewport={"width": 1280, "height": 820},
                )
                page = context.new_page()
                page.set_default_navigation_timeout(settings.navigation_timeout_ms)

                page.goto(settings.ml_login_url, wait_until="domcontentloaded")
                logger.info("Janela de login aberta. Aguardando conclusão manual pelo usuário...")

                login_detected = False
                while time.monotonic() < deadline:
                    if page.is_closed():
                        with self._state_lock:
                            self._state = "logged_out"
                            self._updated_at = datetime.now().astimezone().isoformat()
                            self._message = "Janela de login fechada pelo usuário antes da conclusão."
                        logger.warning("Fluxo de login cancelado: janela fechada pelo usuário.")
                        context.close()
                        browser.close()
                        return

                    if self._is_user_logged_in_on_page(page, context):
                        login_detected = True
                        break

                    page.wait_for_timeout(1200)

                if not login_detected:
                    with self._state_lock:
                        self._state = "error"
                        self._updated_at = datetime.now().astimezone().isoformat()
                        self._message = "Tempo limite de login esgotado. Tente novamente."
                    logger.warning("Timeout atingido aguardando login manual do usuário.")
                    context.close()
                    browser.close()
                    return

                logger.info("Login detectado na janela. Validando sessão em página protegida...")
                if not self._verify_protected_access(page):
                    with self._state_lock:
                        self._state = "error"
                        self._updated_at = datetime.now().astimezone().isoformat()
                        self._message = (
                            "O login não pôde ser validado em página protegida (sessão redirecionada)."
                        )
                    context.close()
                    browser.close()
                    return

                storage_data = context.storage_state()
                self._save_storage_state(storage_data)
                logger.info(
                    "Sessão validada e persistida com sucesso (%d cookies).",
                    len(storage_data.get("cookies", [])),
                )

                context.close()
                browser.close()

        except Exception as exc:
            logger.error("Erro durante fluxo de login interativo: %s", exc, exc_info=True)
            with self._state_lock:
                self._state = "error"
                self._updated_at = datetime.now().astimezone().isoformat()
                self._message = f"Erro ao abrir ou validar janela de login: {exc}"
        finally:
            self._login_flow_lock.release()

    async def start_interactive_login(self) -> tuple[bool, AuthStatusResponse]:
        """Inicia o fluxo de login manual em background se nenhum outro estiver em andamento.

        Retorna `(started, status_response)`. Se já houver um login ativo, retorna `(False, status)`.
        """
        if not self._login_flow_lock.acquire(blocking=False):
            logger.warning("Tentativa de iniciar login recusada: já existe um fluxo de login em andamento.")
            return False, self.get_status()

        with self._state_lock:
            self._state = "logging_in"
            self._updated_at = datetime.now().astimezone().isoformat()
            self._message = (
                "Janela do navegador aberta. Conclua o login no Mercado Livre (incluindo 2FA/verificação)."
            )

        loop = asyncio.get_running_loop()
        self._login_task = loop.create_task(asyncio.to_thread(self._run_interactive_login_sync))
        return True, self.get_status()


session_manager = SessionManager()
