"""Configurações centralizadas da aplicação, sessão, banco de dados e scraper."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

LOGS_DIR = BASE_DIR / "logs"
STATIC_DIR = BASE_DIR / "static"
LOCAL_BROWSERS_DIR = BASE_DIR / ".playwright-browsers"
SESSION_DIR = BASE_DIR / ".session"
CHROMIUM_PROFILE_DIR = SESSION_DIR / "chromium_profile"
SESSION_STATE_FILE = SESSION_DIR / "ml_storage_state.json"
SESSION_META_FILE = SESSION_DIR / "ml_session_meta.json"
DATA_DIR = BASE_DIR / "data"
DB_PATH = Path(os.getenv("GWIBOT_DB_PATH", str(DATA_DIR / "gwibot.db")))

if LOCAL_BROWSERS_DIR.exists() and "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(LOCAL_BROWSERS_DIR)


@dataclass(frozen=True)
class Settings:
    """Parâmetros de execução do servidor, autenticação manual, afiliados e Playwright."""

    app_name: str = "Gwibot - Mercado Livre Scraper & Affiliate Hub"
    app_version: str = "2.0.0"
    host: str = os.getenv("GWIBOT_HOST", "0.0.0.0")
    port: int = int(os.getenv("GWIBOT_PORT", "8000"))

    # URLs do Mercado Livre Brasil
    ml_base_url: str = "https://www.mercadolivre.com.br"
    ml_search_base_url: str = "https://lista.mercadolivre.com.br"
    ml_login_url: str = "https://www.mercadolivre.com.br"
    ml_protected_verify_url: str = "https://myaccount.mercadolivre.com.br/summary"
    ml_affiliate_stripe_api_url: str = (
        "https://www.mercadolivre.com.br/affiliate-program/api/v2/affiliates/createLink"
    )

    # Limites de busca e paginação
    default_limit: int = 20
    min_limit: int = 1
    max_limit: int = 120
    max_pages_per_search: int = 8

    # Timeouts em milissegundos (Playwright) e login interativo
    navigation_timeout_ms: int = int(os.getenv("GWIBOT_NAV_TIMEOUT_MS", "25000"))
    selector_timeout_ms: int = int(os.getenv("GWIBOT_SEL_TIMEOUT_MS", "12000"))
    page_delay_ms: int = int(os.getenv("GWIBOT_PAGE_DELAY_MS", "700"))
    login_timeout_seconds: int = int(os.getenv("GWIBOT_LOGIN_TIMEOUT_SEC", "300"))

    # Execução do navegador para scraping
    headless: bool = os.getenv("GWIBOT_HEADLESS", "true").lower() not in ("0", "false", "no")
    locale: str = "pt-BR"
    timezone_id: str = "America/Sao_Paulo"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )


settings = Settings()
