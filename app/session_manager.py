"""Gerenciamento de sessão persistente (Chromium user_data_dir + 2FA) e login interativo para o Mercado Livre.

Arquitetura de Sessão Persistente:
- Utiliza perfil persistente real do Chromium (`user_data_dir` em `.session/chromium_profile`),
  preservando Cookies, LocalStorage, IndexedDB, Service Workers e Device Tokens de 2FA ("Dispositivo confiável").
- Garante exclusão mútua global (`profile_lock`) para impedir múltiplas instâncias simultâneas no mesmo perfil.
- Separa completamente a reinicialização/atualização do servidor do encerramento da sessão.
- Nunca apaga ou recria o perfil em reinicializações, atualizações ou falhas transitórias de rede.
- Apenas o Logout manual (`POST /api/auth/logout`) limpa os dados de autenticação.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import threading
import time
from typing import Any, Iterator

from playwright.sync_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    sync_playwright,
)

from app.config import (
    CHROMIUM_PROFILE_DIR,
    SESSION_DIR,
    SESSION_META_FILE,
    SESSION_STATE_FILE,
    settings,
)
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
    """Controla o perfil persistente do Chromium (`user_data_dir`) e o ciclo de vida da sessão do Mercado Livre."""

    # Lock global compartilhado entre instâncias na mesma execução para garantir que o perfil
    # Chromium nunca seja aberto por 2 processos do navegador ao mesmo tempo.
    _global_profile_lock = threading.RLock()

    def __init__(
        self,
        session_dir: Path = SESSION_DIR,
        state_file: Path = SESSION_STATE_FILE,
        meta_file: Path = SESSION_META_FILE,
        profile_dir: Path | None = None,
    ) -> None:
        self._session_dir = session_dir
        self._state_file = state_file
        self._meta_file = meta_file
        self._profile_dir = profile_dir or (session_dir / "chromium_profile")
        self._state_lock = threading.RLock()
        self._login_flow_lock = threading.Lock()
        self._login_task: asyncio.Task[None] | None = None

        self._state: AuthStateType = "logged_out"
        self._message: str = "Sessão desconectada. Faça login para evitar bloqueios de verificação."
        self._updated_at: str | None = None

        self._ensure_secure_dir()
        self._load_persisted_state_on_startup()

    @property
    def profile_dir(self) -> Path:
        """Retorna o diretório do perfil persistente do Chromium (`user_data_dir`)."""
        self._ensure_secure_dir()
        return self._profile_dir

    def _ensure_secure_dir(self) -> None:
        if not self._session_dir.exists():
            self._session_dir.mkdir(parents=True, exist_ok=True)
            _restrict_owner_permissions(self._session_dir)
        if not self._profile_dir.exists():
            self._profile_dir.mkdir(parents=True, exist_ok=True)

    def _clean_stale_singleton_locks(self) -> None:
        """Remove apenas arquivos SingletonLock órfãos caso o processo anterior tenha sofrido parada brusca.

        Nunca apaga Cookies, Local Storage, IndexedDB ou dados de 2FA do perfil.
        """
        for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            lock_file = self._profile_dir / lock_name
            if lock_file.exists():
                try:
                    lock_file.unlink()
                except Exception:
                    pass

    @contextmanager
    def open_persistent_context(
        self,
        pw: Playwright,
        *,
        headless: bool,
        viewport: dict[str, int] | None = None,
    ) -> Iterator[tuple[BrowserContext, Page]]:
        """Abre e reutiliza o perfil persistente do Chromium (`user_data_dir`) com exclusão mútua estrita.

        - Impede múltiplas instâncias simultâneas utilizando o mesmo perfil.
        - Hidrata cookies/origins salvos em `ml_storage_state.json` (se existirem) sem perder dados do perfil.
        - Ao fechar o contexto, sincroniza de volta os cookies/tokens atualizados (incluindo 2FA renovado).
        """
        with self._global_profile_lock:
            self._ensure_secure_dir()
            self._clean_stale_singleton_locks()

            vp = viewport or {"width": 1366, "height": 860}
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(self._profile_dir),
                headless=headless,
                locale=settings.locale,
                timezone_id=settings.timezone_id,
                user_agent=settings.user_agent,
                viewport=vp,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-default-browser-check",
                    "--no-first-run",
                    "--disable-dev-shm-usage",
                ],
            )
            try:
                # Se a sessão está autenticada e há cookies salvos em ml_storage_state.json,
                # garante que estejam presentes no perfil persistente
                self._hydrate_context_from_saved_state(context)
                page = context.pages[0] if context.pages else context.new_page()
                yield context, page

                # Se a sessão continua ativa (logged_in), sincroniza cookies rotacionados pelo ML de volta ao snapshot
                with self._state_lock:
                    is_active = self._state == "logged_in"
                if is_active:
                    try:
                        live_cookies = context.cookies()
                        if self._has_valid_auth_cookies(live_cookies):
                            state_snapshot = context.storage_state()
                            self._write_snapshot_files_unlocked(state_snapshot, update_status=False)
                    except Exception as exc:
                        logger.debug("Não foi necessário atualizar snapshot pós-uso: %s", exc)
            finally:
                try:
                    context.close()
                except Exception:
                    pass

    def _hydrate_context_from_saved_state(self, context: BrowserContext) -> None:
        """Garante que cookies persistidos em `ml_storage_state.json` estejam carregados no contexto persistente."""
        with self._state_lock:
            if self._state != "logged_in" or not self._state_file.exists():
                return
            try:
                raw = json.loads(self._state_file.read_text(encoding="utf-8"))
            except Exception:
                return

        saved_cookies = raw.get("cookies", [])
        if not isinstance(saved_cookies, list) or not saved_cookies:
            return

        try:
            existing_cookies = context.cookies()
            existing_names = {
                (c.get("name"), c.get("domain"))
                for c in existing_cookies
                if isinstance(c, dict)
            }
            missing_cookies = [
                c for c in saved_cookies
                if isinstance(c, dict) and (c.get("name"), c.get("domain")) not in existing_names
            ]
            if missing_cookies:
                context.add_cookies(missing_cookies)
        except Exception as exc:
            logger.debug("Aviso ao sincronizar cookies no perfil persistente: %s", exc)

    def _has_profile_cookies_on_disk(self) -> bool:
        """Verifica se o diretório `user_data_dir` já possui banco de Cookies/Local Storage do Chromium."""
        candidates = (
            self._profile_dir / "Default" / "Network" / "Cookies",
            self._profile_dir / "Default" / "Cookies",
            self._profile_dir / "Default" / "Local Storage" / "leveldb",
        )
        return any(p.exists() for p in candidates)

    def _load_persisted_state_on_startup(self) -> None:
        """Carrega e valida a sessão salva em disco durante a inicialização sem jamais apagar o perfil."""
        with self._state_lock:
            meta: dict[str, Any] = {}
            if self._meta_file.exists():
                try:
                    meta = json.loads(self._meta_file.read_text(encoding="utf-8"))
                except Exception:
                    meta = {}

            persisted_status = meta.get("state")
            meta_updated = meta.get("updated_at")

            if persisted_status == "expired":
                self._state = "expired"
                self._updated_at = meta_updated
                self._message = meta.get("message") or (
                    "A sessão salva expirou ou foi revogada pelo Mercado Livre. Faça login novamente."
                )
                return

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

                self._state = "logged_in"
                self._updated_at = meta_updated or datetime.now().astimezone().isoformat()
                self._message = "Sessão persistente autenticada (perfil Chromium + 2FA preservados)."
                logger.info(
                    "Sessão persistente carregada com sucesso (%d cookies armazenados, perfil=%s).",
                    len(cookies),
                    self._profile_dir.name,
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
            profile_ready = self._profile_dir.exists()
            if self._state == "logged_in" and not has_file and not self._has_profile_cookies_on_disk():
                self._state = "logged_out"
                self._message = "Sessão desconectada."

            return AuthStatusResponse(
                authenticated=(self._state == "logged_in" and (has_file or self._has_profile_cookies_on_disk())),
                state=self._state,
                message=self._message,
                has_saved_session=has_file,
                persistent_profile_ready=profile_ready,
                updated_at=self._updated_at,
            )

    def get_storage_state_path(self) -> Path | None:
        """Retorna o caminho do arquivo `storage_state` se a sessão estiver ativa, ou `None`."""
        with self._state_lock:
            if self._state == "logged_in" and self._state_file.exists():
                return self._state_file
            return None

    def is_authenticated(self) -> bool:
        """Retorna True se a sessão atual está autenticada e pronta para uso."""
        return self.get_status().authenticated

    def mark_session_expired(self, reason: str | None = None) -> None:
        """Marca a sessão como expirada quando o Mercado Livre revoga o login.

        IMPORTANTE: Preserva o diretório `chromium_profile` (`user_data_dir`) para não perder
        o reconhecimento de dispositivo confiável (2FA) no próximo login.
        """
        with self._state_lock:
            try:
                if self._state_file.exists():
                    self._state_file.unlink()
            except Exception:
                pass

            self._state = "expired"
            self._updated_at = datetime.now().astimezone().isoformat()
            self._message = reason or (
                "Sessão expirada detectada (redirecionamento para login/verificação). "
                "Faça login novamente."
            )
            try:
                meta = {
                    "state": "expired",
                    "updated_at": self._updated_at,
                    "message": self._message,
                }
                self._meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
        logger.warning("Sessão marcada como expirada (perfil 2FA preservado): %s", self._message)

    def logout(self) -> AuthStatusResponse:
        """Única operação normal de limpeza da sessão: remove estado salvo e limpa o perfil persistente."""
        with self._global_profile_lock, self._state_lock:
            self._remove_files_unlocked()
            # Limpa diretório do perfil persistente apenas no logout manual explícito
            if self._profile_dir.exists():
                shutil.rmtree(self._profile_dir, ignore_errors=True)
            self._profile_dir.mkdir(parents=True, exist_ok=True)

            self._state = "logged_out"
            self._updated_at = datetime.now().astimezone().isoformat()
            self._message = "Sessão encerrada manualmente e dados locais removidos com sucesso."
        logger.info("Logout manual executado: arquivos de sessão e perfil Chromium limpos.")
        return self.get_status()

    def _remove_files_unlocked(self) -> None:
        for fpath in (self._state_file, self._meta_file):
            try:
                if fpath.exists():
                    fpath.unlink()
            except Exception as exc:
                logger.warning("Erro ao remover arquivo de sessão %s: %s", fpath.name, exc)

    def _write_snapshot_files_unlocked(
        self,
        storage_data: dict[str, Any],
        *,
        update_status: bool = True,
    ) -> None:
        self._ensure_secure_dir()
        tmp_state = self._state_file.with_suffix(".tmp")
        tmp_state.write_text(json.dumps(storage_data, ensure_ascii=False, indent=2), encoding="utf-8")
        _restrict_owner_permissions(tmp_state)
        tmp_state.replace(self._state_file)
        _restrict_owner_permissions(self._state_file)

        now_iso = datetime.now().astimezone().isoformat()
        meta = {
            "state": "logged_in",
            "updated_at": now_iso,
            "cookie_count": len(storage_data.get("cookies", [])),
            "origins_count": len(storage_data.get("origins", [])),
            "profile_dir": str(self._profile_dir),
        }
        self._meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        _restrict_owner_permissions(self._meta_file)

        if update_status:
            self._state = "logged_in"
            self._updated_at = now_iso
            self._message = "Sessão autenticada e validada com sucesso (perfil persistente 2FA ativo)."

    def _save_storage_state(self, storage_data: dict[str, Any]) -> None:
        """Salva o estado de sessão em disco de forma atômica e com permissões restritas."""
        with self._state_lock:
            self._write_snapshot_files_unlocked(storage_data, update_status=True)

    @staticmethod
    def _is_user_logged_in_on_page(page: Page, context: BrowserContext) -> bool:
        """Verifica se o usuário concluiu o login (incluindo 2FA) na janela interativa."""
        current_url = page.url or ""
        if not current_url.startswith("https://"):
            return False
        if SessionManager.is_url_auth_gate(current_url):
            return False

        cookies = context.cookies()
        if not SessionManager._has_valid_auth_cookies(cookies):
            return False

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

        if "mercadolivre.com.br" in current_url.lower():
            return True

        return False

    @staticmethod
    def _verify_protected_access(page: Page) -> bool:
        """Navega para uma página protegida para confirmar que não há redirecionamento de login/2FA."""
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
        """Fluxo síncrono que abre o Chromium visível (headed) usando o MESMO `user_data_dir` persistente."""
        logger.info(
            "Iniciando fluxo de login interativo no perfil persistente do Chromium (%s).",
            self._profile_dir,
        )
        deadline = time.monotonic() + settings.login_timeout_seconds

        try:
            with sync_playwright() as pw:
                with self.open_persistent_context(
                    pw,
                    headless=False,
                    viewport={"width": 1280, "height": 820},
                ) as (context, page):
                    page.set_default_navigation_timeout(settings.navigation_timeout_ms)
                    page.goto(settings.ml_login_url, wait_until="domcontentloaded")
                    logger.info("Janela de login aberta no perfil persistente. Aguardando conclusão manual + 2FA...")

                    login_detected = False
                    while time.monotonic() < deadline:
                        if page.is_closed():
                            with self._state_lock:
                                if not self._state_file.exists():
                                    self._state = "logged_out"
                                    self._updated_at = datetime.now().astimezone().isoformat()
                                    self._message = "Janela de login fechada pelo usuário antes da conclusão."
                            logger.warning("Fluxo de login cancelado: janela fechada pelo usuário.")
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
                        return

                    logger.info("Login/2FA detectado. Validando sessão em página protegida...")
                    if not self._verify_protected_access(page):
                        with self._state_lock:
                            self._state = "error"
                            self._updated_at = datetime.now().astimezone().isoformat()
                            self._message = (
                                "O login não pôde ser validado em página protegida (sessão redirecionada)."
                            )
                        return

                    storage_data = context.storage_state()
                    self._save_storage_state(storage_data)

                    # Ao concluir um login legítimo, limpa qualquer cooldown de CAPTCHA no scraper
                    try:
                        from app.scraper import scraper_service
                        scraper_service.clear_cooldown()
                    except Exception:
                        pass

                    logger.info(
                        "Sessão + 2FA validados e persistidos no perfil Chromium (%d cookies).",
                        len(storage_data.get("cookies", [])),
                    )

        except Exception as exc:
            logger.error("Erro durante fluxo de login interativo: %s", exc, exc_info=True)
            with self._state_lock:
                self._state = "error"
                self._updated_at = datetime.now().astimezone().isoformat()
                self._message = f"Erro ao abrir ou validar janela de login: {exc}"
        finally:
            self._login_flow_lock.release()

    async def start_interactive_login(self) -> tuple[bool, AuthStatusResponse]:
        """Inicia o fluxo de login manual em background se nenhum outro estiver em andamento."""
        if not self._login_flow_lock.acquire(blocking=False):
            logger.warning("Tentativa de iniciar login recusada: já existe um fluxo de login em andamento.")
            return False, self.get_status()

        with self._state_lock:
            self._state = "logging_in"
            self._updated_at = datetime.now().astimezone().isoformat()
            self._message = (
                "Janela do navegador aberta no perfil persistente. Conclua o login e 2FA no Mercado Livre."
            )

        loop = asyncio.get_running_loop()
        self._login_task = loop.create_task(asyncio.to_thread(self._run_interactive_login_sync))
        return True, self.get_status()


session_manager = SessionManager()
