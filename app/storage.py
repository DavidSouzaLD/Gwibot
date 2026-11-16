"""Camada de persistência SQLite para configurações de afiliados, destinos, ciclos, deduplicação e histórico."""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import uuid
from typing import Any, Optional

from app.config import DB_PATH
from app.logger import logger
from app.models import (
    AffiliateConfigModel,
    CatalogProductRecord,
    CycleCreateRequest,
    CycleFilters,
    CycleModel,
    CycleRunLog,
    CycleStateType,
    DEFAULT_PLATFORM_TEMPLATES,
    OutputDestinationConfig,
    PlatformTemplateModel,
    ProductItem,
    QueuedOfferRecord,
    SentOfferRecord,
)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


class StorageService:
    """Gerencia armazenamento persistente em SQLite com segurança entre threads."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._db_path = db_path
        self._lock = threading.RLock()
        self._sent_keys_cache: set[str] = set()
        self._sent_dest_cache: set[tuple[str, str]] = set()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS affiliate_configs (
                    provider_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    matt_word TEXT NOT NULL DEFAULT '',
                    matt_tool TEXT NOT NULL DEFAULT '',
                    use_authenticated_linkbuilder INTEGER NOT NULL DEFAULT 0,
                    credentials_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS output_destinations (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    output_type TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    config_json TEXT NOT NULL,
                    custom_template TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS platform_templates (
                    platform TEXT PRIMARY KEY,
                    template_text TEXT NOT NULL,
                    parse_mode TEXT NOT NULL DEFAULT 'HTML',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cycles (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    query TEXT NOT NULL,
                    source_id TEXT NOT NULL DEFAULT 'mercadolivre',
                    search_limit INTEGER NOT NULL DEFAULT 20,
                    max_offers_per_run INTEGER NOT NULL DEFAULT 5,
                    interval_minutes INTEGER NOT NULL DEFAULT 30,
                    product_delay_minutes REAL NOT NULL DEFAULT 0.0,
                    last_page_offset INTEGER NOT NULL DEFAULT 1,
                    destination_ids_json TEXT NOT NULL DEFAULT '[]',
                    filters_json TEXT NOT NULL DEFAULT '{}',
                    deduplicate INTEGER NOT NULL DEFAULT 1,
                    state TEXT NOT NULL DEFAULT 'idle',
                    last_run_at TEXT,
                    next_run_at TEXT,
                    total_runs INTEGER NOT NULL DEFAULT 0,
                    total_offers_sent INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cycle_runs (
                    id TEXT PRIMARY KEY,
                    cycle_id TEXT NOT NULL,
                    cycle_name TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    query TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    searched_count INTEGER NOT NULL DEFAULT 0,
                    filtered_count INTEGER NOT NULL DEFAULT 0,
                    affiliate_ok_count INTEGER NOT NULL DEFAULT 0,
                    affiliate_fail_count INTEGER NOT NULL DEFAULT 0,
                    deduplicated_new_count INTEGER NOT NULL DEFAULT 0,
                    sent_count INTEGER NOT NULL DEFAULT 0,
                    failed_send_count INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT
                );

                CREATE TABLE IF NOT EXISTS sent_offers (
                    id TEXT PRIMARY KEY,
                    dedup_key TEXT NOT NULL,
                    cycle_id TEXT NOT NULL,
                    cycle_name TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    destination_id TEXT NOT NULL,
                    destination_name TEXT NOT NULL,
                    product_id TEXT,
                    product_name TEXT NOT NULL,
                    original_price REAL,
                    promotional_price REAL,
                    discount_percentage INTEGER,
                    rating REAL,
                    product_url TEXT NOT NULL,
                    affiliate_url TEXT NOT NULL,
                    image_url TEXT,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    sent_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_sent_offers_dedup ON sent_offers(dedup_key, status);
                CREATE INDEX IF NOT EXISTS idx_sent_offers_dest ON sent_offers(dedup_key, destination_id, status);
                CREATE INDEX IF NOT EXISTS idx_sent_offers_sent_at ON sent_offers(sent_at DESC);
                CREATE INDEX IF NOT EXISTS idx_cycle_runs_started_at ON cycle_runs(started_at DESC);

                CREATE TABLE IF NOT EXISTS products_catalog (
                    product_id TEXT PRIMARY KEY,
                    dedup_key TEXT NOT NULL,
                    source_id TEXT NOT NULL DEFAULT 'mercadolivre',
                    query_terms TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    product_url TEXT NOT NULL,
                    affiliate_url TEXT,
                    affiliate_status TEXT,
                    original_price REAL,
                    promotional_price REAL,
                    discount_percentage INTEGER,
                    rating REAL,
                    reviews_count INTEGER,
                    seller TEXT,
                    image_url TEXT,
                    available INTEGER NOT NULL DEFAULT 1,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    processing_status TEXT NOT NULL DEFAULT 'discovered',
                    publication_status TEXT NOT NULL DEFAULT 'unpublished',
                    last_published_destination_id TEXT,
                    last_published_at TEXT,
                    times_seen INTEGER NOT NULL DEFAULT 1,
                    last_error TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_products_catalog_dedup ON products_catalog(dedup_key);
                CREATE INDEX IF NOT EXISTS idx_products_catalog_pub ON products_catalog(publication_status, last_seen_at DESC);

                CREATE TABLE IF NOT EXISTS offer_queue (
                    id TEXT PRIMARY KEY,
                    product_id TEXT NOT NULL,
                    dedup_key TEXT NOT NULL,
                    cycle_id TEXT NOT NULL,
                    cycle_name TEXT NOT NULL,
                    source_id TEXT NOT NULL DEFAULT 'mercadolivre',
                    destination_id TEXT NOT NULL,
                    destination_name TEXT NOT NULL,
                    product_snapshot_json TEXT NOT NULL,
                    affiliate_url TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    next_retry_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    sent_at TEXT,
                    UNIQUE(dedup_key, destination_id)
                );

                CREATE INDEX IF NOT EXISTS idx_offer_queue_status ON offer_queue(status, cycle_id, created_at ASC);
                CREATE INDEX IF NOT EXISTS idx_offer_queue_dedup_dest ON offer_queue(dedup_key, destination_id);
                """
            )
            # Migrações leves para bancos existentes
            cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(cycles)").fetchall()
            }
            if "product_delay_minutes" not in cols:
                conn.execute(
                    "ALTER TABLE cycles ADD COLUMN product_delay_minutes REAL NOT NULL DEFAULT 0.0"
                )

            if "last_page_offset" not in cols:
                conn.execute(
                    "ALTER TABLE cycles ADD COLUMN last_page_offset INTEGER NOT NULL DEFAULT 1"
                )
            if "last_duration_seconds" not in cols:
                conn.execute("ALTER TABLE cycles ADD COLUMN last_duration_seconds REAL")
            if "last_schedule_delay_seconds" not in cols:
                conn.execute("ALTER TABLE cycles ADD COLUMN last_schedule_delay_seconds REAL")
            if "last_bottleneck" not in cols:
                conn.execute("ALTER TABLE cycles ADD COLUMN last_bottleneck TEXT")

            run_cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(cycle_runs)").fetchall()
            }
            new_run_cols = {
                "scheduled_at": "TEXT",
                "duration_seconds": "REAL NOT NULL DEFAULT 0.0",
                "schedule_delay_seconds": "REAL NOT NULL DEFAULT 0.0",
                "navigation_ms": "REAL NOT NULL DEFAULT 0.0",
                "extraction_ms": "REAL NOT NULL DEFAULT 0.0",
                "processing_ms": "REAL NOT NULL DEFAULT 0.0",
                "affiliate_ms": "REAL NOT NULL DEFAULT 0.0",
                "dispatch_ms": "REAL NOT NULL DEFAULT 0.0",
                "bottleneck_stage": "TEXT",
            }
            for col_name, col_type in new_run_cols.items():
                if col_name not in run_cols:
                    conn.execute(f"ALTER TABLE cycle_runs ADD COLUMN {col_name} {col_type}")

            aff_cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(affiliate_configs)").fetchall()
            }
            if "credentials_json" not in aff_cols:
                conn.execute(
                    "ALTER TABLE affiliate_configs ADD COLUMN credentials_json TEXT NOT NULL DEFAULT '{}'"
                )

            dest_cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(output_destinations)").fetchall()
            }
            if "custom_template" not in dest_cols:
                conn.execute(
                    "ALTER TABLE output_destinations ADD COLUMN custom_template TEXT"
                )

            # Pré-aquece o cache em memória de chaves de produtos já enviados com sucesso (global e por destino)
            sent_rows = conn.execute(
                "SELECT dedup_key, destination_id FROM sent_offers WHERE status = 'sent'"
            ).fetchall()
            self._sent_keys_cache = {r["dedup_key"] for r in sent_rows if r["dedup_key"]}
            self._sent_dest_cache = {
                (r["dedup_key"], r["destination_id"])
                for r in sent_rows
                if r["dedup_key"] and r["destination_id"]
            }

            # Recupera filas interrompidas ('processing' -> 'pending') após reinicialização do servidor
            conn.execute(
                """
                UPDATE offer_queue
                SET status = 'pending', updated_at = ?
                WHERE status = 'processing'
                """,
                (_now_iso(),),
            )

    # -----------------------------------------------------------------
    # Affiliate Configurations (Multi-Store Extensible)
    # -----------------------------------------------------------------

    def get_affiliate_config(self, provider_id: str = "mercadolivre") -> AffiliateConfigModel:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM affiliate_configs WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()
            if not row:
                return AffiliateConfigModel(provider_id=provider_id)
            creds = json.loads(row["credentials_json"] or "{}") if "credentials_json" in row.keys() else {}
            return AffiliateConfigModel(
                provider_id=row["provider_id"],
                enabled=bool(row["enabled"]),
                matt_word=row["matt_word"],
                matt_tool=row["matt_tool"],
                use_authenticated_linkbuilder=bool(row["use_authenticated_linkbuilder"]),
                credentials=creds,
                updated_at=row["updated_at"],
            )

    def list_affiliate_configs(self) -> dict[str, AffiliateConfigModel]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM affiliate_configs").fetchall()
            result: dict[str, AffiliateConfigModel] = {}
            for row in rows:
                creds = json.loads(row["credentials_json"] or "{}") if "credentials_json" in row.keys() else {}
                result[row["provider_id"]] = AffiliateConfigModel(
                    provider_id=row["provider_id"],
                    enabled=bool(row["enabled"]),
                    matt_word=row["matt_word"],
                    matt_tool=row["matt_tool"],
                    use_authenticated_linkbuilder=bool(row["use_authenticated_linkbuilder"]),
                    credentials=creds,
                    updated_at=row["updated_at"],
                )
            return result

    def save_affiliate_config(self, config: AffiliateConfigModel) -> AffiliateConfigModel:
        now = _now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO affiliate_configs (
                    provider_id, enabled, matt_word, matt_tool, use_authenticated_linkbuilder, credentials_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    matt_word = excluded.matt_word,
                    matt_tool = excluded.matt_tool,
                    use_authenticated_linkbuilder = excluded.use_authenticated_linkbuilder,
                    credentials_json = excluded.credentials_json,
                    updated_at = excluded.updated_at
                """,
                (
                    config.provider_id,
                    1 if config.enabled else 0,
                    config.matt_word.strip(),
                    config.matt_tool.strip(),
                    1 if config.use_authenticated_linkbuilder else 0,
                    json.dumps(config.credentials, ensure_ascii=False),
                    now,
                ),
            )
        logger.info(
            "Configuração de afiliado salva | provider=%s | enabled=%s | matt_word='%s'",
            config.provider_id,
            config.enabled,
            config.matt_word.strip(),
        )
        return self.get_affiliate_config(config.provider_id)

    # -----------------------------------------------------------------
    # Platform Message Templates (Telegram / WhatsApp)
    # -----------------------------------------------------------------

    def get_platform_template(self, platform: str = "telegram") -> PlatformTemplateModel:
        norm_platform = "whatsapp" if platform == "whatsapp" else "telegram"
        default_parse_mode = "Markdown" if norm_platform == "whatsapp" else "HTML"
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM platform_templates WHERE platform = ?",
                (norm_platform,),
            ).fetchone()
            if not row:
                return PlatformTemplateModel(
                    platform=norm_platform,  # type: ignore[arg-type]
                    template_text=DEFAULT_PLATFORM_TEMPLATES[norm_platform],
                    parse_mode=default_parse_mode,  # type: ignore[arg-type]
                )
            return PlatformTemplateModel(
                platform=row["platform"],
                template_text=row["template_text"],
                parse_mode=row["parse_mode"],
                updated_at=row["updated_at"],
            )

    def list_platform_templates(self) -> list[PlatformTemplateModel]:
        return [
            self.get_platform_template("telegram"),
            self.get_platform_template("whatsapp"),
        ]

    def save_platform_template(self, template: PlatformTemplateModel) -> PlatformTemplateModel:
        now = _now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO platform_templates (platform, template_text, parse_mode, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(platform) DO UPDATE SET
                    template_text = excluded.template_text,
                    parse_mode = excluded.parse_mode,
                    updated_at = excluded.updated_at
                """,
                (
                    template.platform,
                    template.template_text,
                    template.parse_mode,
                    now,
                ),
            )
        logger.info("Template de plataforma salvo | platform=%s | chars=%d", template.platform, len(template.template_text))
        return self.get_platform_template(template.platform)

    # -----------------------------------------------------------------
    # Output Destinations (Telegram, WhatsApp, etc.)
    # -----------------------------------------------------------------

    def list_destinations(self) -> list[OutputDestinationConfig]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM output_destinations ORDER BY created_at ASC"
            ).fetchall()
            return [self._row_to_destination(r) for r in rows]

    def get_destination(self, destination_id: str) -> Optional[OutputDestinationConfig]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM output_destinations WHERE id = ?",
                (destination_id,),
            ).fetchone()
            return self._row_to_destination(row) if row else None

    def save_destination(self, dest: OutputDestinationConfig) -> OutputDestinationConfig:
        now = _now_iso()
        dest_id = dest.id
        with self._lock, self._connect() as conn:
            # Idempotência: se dest_id não for informado, procura destino existente com mesmo tipo e configuração essencial
            if not dest_id:
                clean_name = dest.name.strip()
                if dest.output_type == "telegram":
                    chat_id = str(dest.config.get("chat_id", "")).strip()
                    bot_token = str(dest.config.get("bot_token", "")).strip()
                    match_row = conn.execute(
                        """
                        SELECT id FROM output_destinations
                        WHERE output_type = 'telegram'
                          AND (
                              LOWER(TRIM(name)) = LOWER(?)
                              OR (
                                  json_extract(config_json, '$.chat_id') = ?
                                  AND json_extract(config_json, '$.bot_token') = ?
                                  AND ? != ''
                              )
                          )
                        LIMIT 1
                        """,
                        (clean_name, chat_id, bot_token, chat_id),
                    ).fetchone()
                else:
                    match_row = conn.execute(
                        "SELECT id FROM output_destinations WHERE output_type = ? AND LOWER(TRIM(name)) = LOWER(?) LIMIT 1",
                        (dest.output_type, clean_name),
                    ).fetchone()

                if match_row:
                    dest_id = match_row["id"]
                    logger.info("Destino existente reaproveitado por idempotência | id=%s | nome='%s'", dest_id, dest.name)

            dest_id = dest_id or f"dest_{uuid.uuid4().hex[:10]}"

            existing = conn.execute(
                "SELECT created_at FROM output_destinations WHERE id = ?",
                (dest_id,),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            conn.execute(
                """
                INSERT INTO output_destinations (
                    id, name, output_type, enabled, config_json, custom_template, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    output_type = excluded.output_type,
                    enabled = excluded.enabled,
                    config_json = excluded.config_json,
                    custom_template = excluded.custom_template,
                    updated_at = excluded.updated_at
                """,
                (
                    dest_id,
                    dest.name.strip(),
                    dest.output_type,
                    1 if dest.enabled else 0,
                    json.dumps(dest.config, ensure_ascii=False),
                    dest.custom_template,
                    created_at,
                    now,
                ),
            )
        logger.info("Destino de saída salvo | id=%s | tipo=%s | nome='%s'", dest_id, dest.output_type, dest.name)
        return self.get_destination(dest_id)  # type: ignore[return-value]

    def delete_destination(self, destination_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM output_destinations WHERE id = ?", (destination_id,))
            deleted = cur.rowcount > 0
        if deleted:
            logger.info("Destino de saída removido | id=%s", destination_id)
        return deleted

    @staticmethod
    def _row_to_destination(row: sqlite3.Row) -> OutputDestinationConfig:
        custom_tpl = row["custom_template"] if "custom_template" in row.keys() else None
        return OutputDestinationConfig(
            id=row["id"],
            name=row["name"],
            output_type=row["output_type"],
            enabled=bool(row["enabled"]),
            config=json.loads(row["config_json"] or "{}"),
            custom_template=custom_tpl,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # -----------------------------------------------------------------
    # Automated Cycles (Campaigns)
    # -----------------------------------------------------------------

    def list_cycles(self) -> list[CycleModel]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM cycles ORDER BY created_at DESC").fetchall()
            return [self._row_to_cycle(r) for r in rows]

    def get_cycle(self, cycle_id: str) -> Optional[CycleModel]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
            return self._row_to_cycle(row) if row else None

    def create_cycle(self, req: CycleCreateRequest) -> CycleModel:
        now = _now_iso()
        clean_name = req.name.strip()
        unique_dest_ids = list(dict.fromkeys(req.destination_ids))

        with self._lock, self._connect() as conn:
            # Idempotência: se já existe ciclo com o mesmo nome, atualiza-o em vez de duplicar
            existing = conn.execute(
                "SELECT id FROM cycles WHERE LOWER(TRIM(name)) = LOWER(?)",
                (clean_name,),
            ).fetchone()
            if existing:
                existing_id = existing["id"]
                logger.info(
                    "Ciclo existente detectado com nome '%s' (id=%s). Atualizando para garantir idempotência.",
                    clean_name,
                    existing_id,
                )
                self.update_cycle(existing_id, req)
                return self.get_cycle(existing_id)  # type: ignore[return-value]

            cycle_id = f"cycle_{uuid.uuid4().hex[:10]}"
            conn.execute(
                """
                INSERT INTO cycles (
                    id, name, query, source_id, search_limit, max_offers_per_run,
                    interval_minutes, product_delay_minutes, last_page_offset, destination_ids_json, filters_json,
                    deduplicate, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 'idle', ?, ?)
                """,
                (
                    cycle_id,
                    clean_name,
                    req.query.strip(),
                    req.source_id,
                    req.search_limit,
                    req.max_offers_per_run,
                    req.interval_minutes,
                    float(req.product_delay_minutes),
                    json.dumps(unique_dest_ids),
                    req.filters.model_dump_json(),
                    1 if req.deduplicate else 0,
                    now,
                    now,
                ),
            )
        logger.info("Ciclo criado | id=%s | nome='%s' | query='%s'", cycle_id, req.name, req.query)
        return self.get_cycle(cycle_id)  # type: ignore[return-value]

    def update_cycle(self, cycle_id: str, req: CycleCreateRequest) -> Optional[CycleModel]:
        now = _now_iso()
        unique_dest_ids = list(dict.fromkeys(req.destination_ids))
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE cycles SET
                    name = ?,
                    query = ?,
                    source_id = ?,
                    search_limit = ?,
                    max_offers_per_run = ?,
                    interval_minutes = ?,
                    product_delay_minutes = ?,
                    destination_ids_json = ?,
                    filters_json = ?,
                    deduplicate = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    req.name.strip(),
                    req.query.strip(),
                    req.source_id,
                    req.search_limit,
                    req.max_offers_per_run,
                    req.interval_minutes,
                    float(req.product_delay_minutes),
                    json.dumps(unique_dest_ids),
                    req.filters.model_dump_json(),
                    1 if req.deduplicate else 0,
                    now,
                    cycle_id,
                ),
            )
            if cur.rowcount == 0:
                return None
        logger.info("Ciclo atualizado | id=%s | nome='%s' | query='%s'", cycle_id, req.name, req.query)
        return self.get_cycle(cycle_id)

    def update_cycle_state(
        self,
        cycle_id: str,
        state: CycleStateType,
        *,
        last_run_at: Optional[str] = None,
        next_run_at: Optional[str] = None,
        last_duration_seconds: Optional[float] = None,
        last_schedule_delay_seconds: Optional[float] = None,
        last_bottleneck: Optional[str] = None,
        increment_runs: bool = False,
        offers_sent_delta: int = 0,
        last_error: Optional[str] = None,
        clear_error: bool = False,
        last_page_offset: Optional[int] = None,
    ) -> Optional[CycleModel]:
        now = _now_iso()
        with self._lock, self._connect() as conn:
            current = conn.execute("SELECT * FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
            if not current:
                return None

            row_keys = current.keys()
            new_last_run = last_run_at if last_run_at is not None else current["last_run_at"]
            new_next_run = next_run_at if next_run_at is not None else (
                None if state in ("stopped", "paused", "idle") else current["next_run_at"]
            )
            new_runs = int(current["total_runs"]) + (1 if increment_runs else 0)
            new_sent = int(current["total_offers_sent"]) + offers_sent_delta
            new_error = None if clear_error else (
                last_error if last_error is not None else current["last_error"]
            )
            current_offset = int(current["last_page_offset"]) if "last_page_offset" in row_keys else 1
            new_offset = last_page_offset if last_page_offset is not None else current_offset

            new_dur = (
                last_duration_seconds
                if last_duration_seconds is not None
                else (current["last_duration_seconds"] if "last_duration_seconds" in row_keys else None)
            )
            new_delay = (
                last_schedule_delay_seconds
                if last_schedule_delay_seconds is not None
                else (current["last_schedule_delay_seconds"] if "last_schedule_delay_seconds" in row_keys else None)
            )
            new_bottleneck = (
                last_bottleneck
                if last_bottleneck is not None
                else (current["last_bottleneck"] if "last_bottleneck" in row_keys else None)
            )

            conn.execute(
                """
                UPDATE cycles SET
                    state = ?,
                    last_run_at = ?,
                    next_run_at = ?,
                    last_duration_seconds = ?,
                    last_schedule_delay_seconds = ?,
                    last_bottleneck = ?,
                    total_runs = ?,
                    total_offers_sent = ?,
                    last_error = ?,
                    last_page_offset = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    state,
                    new_last_run,
                    new_next_run,
                    new_dur,
                    new_delay,
                    new_bottleneck,
                    new_runs,
                    new_sent,
                    new_error,
                    new_offset,
                    now,
                    cycle_id,
                ),
            )
        return self.get_cycle(cycle_id)

    def delete_cycle(self, cycle_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM cycles WHERE id = ?", (cycle_id,))
            return cur.rowcount > 0

    @staticmethod
    def _row_to_cycle(row: sqlite3.Row) -> CycleModel:
        row_keys = row.keys()
        return CycleModel(
            id=row["id"],
            name=row["name"],
            query=row["query"],
            source_id=row["source_id"],
            search_limit=int(row["search_limit"]),
            max_offers_per_run=int(row["max_offers_per_run"]),
            interval_minutes=int(row["interval_minutes"]),
            product_delay_minutes=float(row["product_delay_minutes"] or 0.0),
            last_page_offset=int(row["last_page_offset"]) if "last_page_offset" in row_keys else 1,
            destination_ids=json.loads(row["destination_ids_json"] or "[]"),
            filters=CycleFilters(**json.loads(row["filters_json"] or "{}")),
            deduplicate=bool(row["deduplicate"]),
            state=row["state"],
            last_run_at=row["last_run_at"],
            next_run_at=row["next_run_at"],
            last_duration_seconds=(
                float(row["last_duration_seconds"])
                if "last_duration_seconds" in row_keys and row["last_duration_seconds"] is not None
                else None
            ),
            last_schedule_delay_seconds=(
                float(row["last_schedule_delay_seconds"])
                if "last_schedule_delay_seconds" in row_keys and row["last_schedule_delay_seconds"] is not None
                else None
            ),
            last_bottleneck=row["last_bottleneck"] if "last_bottleneck" in row_keys else None,
            total_runs=int(row["total_runs"]),
            total_offers_sent=int(row["total_offers_sent"]),
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # -----------------------------------------------------------------
    # Deduplication & Sent Offers History (O(1) Cache + Batch SQL)
    # -----------------------------------------------------------------

    @staticmethod
    def canonical_product_identity(source_id: str, item: ProductItem) -> tuple[str, str]:
        """Retorna `(product_id_canonico, dedup_key)` garantindo identificação única por ID do Mercado Livre."""
        raw_id = (item.id or "").strip().upper()
        identifier = (item.id or item.url or item.name or "").strip().lower()
        dedup_key = f"{source_id}:{identifier}"
        if raw_id:
            return raw_id, dedup_key
        digest = hashlib.sha1(identifier.encode("utf-8")).hexdigest()[:12].upper()
        return f"MLBU_{digest}", dedup_key

    def is_offer_already_sent(self, dedup_key: str, destination_id: Optional[str] = None) -> bool:
        """Verifica em O(1) via cache em memória sincronizado se `dedup_key` já foi enviado (globalmente ou a um destino)."""
        if not dedup_key:
            return False
        with self._lock:
            if destination_id:
                return (dedup_key, destination_id) in self._sent_dest_cache
            return dedup_key in self._sent_keys_cache

    def filter_unsent_dedup_keys(
        self,
        dedup_keys: list[str],
        destination_ids: Optional[list[str]] = None,
    ) -> set[str]:
        """Retorna o subconjunto de chaves que AINDA NÃO foram enviadas com sucesso."""
        if not dedup_keys:
            return set()
        with self._lock:
            if destination_ids:
                # Produto é elegível se ainda não foi enviado para PELO MENOS UM dos destinos ativos
                return {
                    k
                    for k in dedup_keys
                    if k and any((k, d_id) not in self._sent_dest_cache for d_id in destination_ids)
                }
            return {k for k in dedup_keys if k and k not in self._sent_keys_cache}

    def record_sent_offer(self, record: SentOfferRecord) -> None:
        self.record_sent_offers_batch([record])

    def record_sent_offers_batch(self, records: list[SentOfferRecord]) -> None:
        """Persiste uma lista de ofertas enviadas em uma única transação SQLite e atualiza o cache O(1)."""
        if not records:
            return
        with self._lock, self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO sent_offers (
                    id, dedup_key, cycle_id, cycle_name, source_id,
                    destination_id, destination_name, product_id, product_name,
                    original_price, promotional_price, discount_percentage, rating,
                    product_url, affiliate_url, image_url, status, error_message, sent_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        r.id,
                        r.dedup_key,
                        r.cycle_id,
                        r.cycle_name,
                        r.source_id,
                        r.destination_id,
                        r.destination_name,
                        r.product_id,
                        r.product_name,
                        r.original_price,
                        r.promotional_price,
                        r.discount_percentage,
                        r.rating,
                        r.product_url,
                        r.affiliate_url,
                        r.image_url,
                        r.status,
                        r.error_message,
                        r.sent_at,
                    )
                    for r in records
                ],
            )
            for r in records:
                if r.status == "sent" and r.dedup_key:
                    self._sent_keys_cache.add(r.dedup_key)
                    if r.destination_id:
                        self._sent_dest_cache.add((r.dedup_key, r.destination_id))

    # -----------------------------------------------------------------
    # Banco de Produtos (products_catalog) & Fila Persistente (offer_queue)
    # -----------------------------------------------------------------

    def upsert_catalog_products(
        self,
        items: list[ProductItem],
        source_id: str = "mercadolivre",
        query_term: str = "",
        processing_status: str = "discovered",
    ) -> list[CatalogProductRecord]:
        """Insere ou atualiza produtos encontrados no Banco de Produtos (`products_catalog`).

        - Identifica cada produto pelo seu ID único do Mercado Livre.
        - Preserva `first_seen_at` e atualiza `last_seen_at`, `times_seen`, preços e disponibilidade.
        - Não sobrescreve `publication_status = 'published'` se o produto já foi publicado.
        """
        if not items:
            return []
        now = _now_iso()
        upserted_ids: list[str] = []

        with self._lock, self._connect() as conn:
            for item in items:
                prod_id, dedup_key = self.canonical_product_identity(source_id, item)
                if not item.id:
                    item.id = prod_id

                existing = conn.execute(
                    "SELECT * FROM products_catalog WHERE product_id = ?",
                    (prod_id,),
                ).fetchone()

                if existing:
                    existing_terms = [
                        t.strip()
                        for t in (existing["query_terms"] or "").split(",")
                        if t.strip()
                    ]
                    if query_term and query_term.strip() and query_term.strip().lower() not in {
                        t.lower() for t in existing_terms
                    }:
                        existing_terms.append(query_term.strip())
                    merged_terms = ", ".join(existing_terms)

                    current_pub = existing["publication_status"]
                    current_proc = existing["processing_status"]
                    new_proc = (
                        current_proc
                        if current_pub == "published" or current_proc in ("published", "queued")
                        else processing_status
                    )
                    aff_url = item.affiliate_url or existing["affiliate_url"]
                    aff_status = item.affiliate_status or existing["affiliate_status"]

                    conn.execute(
                        """
                        UPDATE products_catalog SET
                            dedup_key = ?,
                            query_terms = ?,
                            name = ?,
                            product_url = ?,
                            affiliate_url = ?,
                            affiliate_status = ?,
                            original_price = ?,
                            promotional_price = ?,
                            discount_percentage = ?,
                            rating = ?,
                            reviews_count = ?,
                            seller = ?,
                            image_url = ?,
                            available = 1,
                            last_seen_at = ?,
                            processing_status = ?,
                            times_seen = times_seen + 1
                        WHERE product_id = ?
                        """,
                        (
                            dedup_key,
                            merged_terms,
                            item.name or existing["name"],
                            item.url or existing["product_url"],
                            aff_url,
                            aff_status,
                            item.original_price if item.original_price is not None else existing["original_price"],
                            item.promotional_price if item.promotional_price is not None else existing["promotional_price"],
                            item.discount_percentage if item.discount_percentage is not None else existing["discount_percentage"],
                            item.rating if item.rating is not None else existing["rating"],
                            item.reviews_count if item.reviews_count is not None else existing["reviews_count"],
                            item.seller or existing["seller"],
                            item.image_url or existing["image_url"],
                            now,
                            new_proc,
                            prod_id,
                        ),
                    )
                    # Sincroniza o snapshot de itens pendentes na fila com o preço atualizado
                    conn.execute(
                        """
                        UPDATE offer_queue
                        SET product_snapshot_json = ?, updated_at = ?
                        WHERE product_id = ? AND status IN ('pending', 'retry_wait')
                        """,
                        (item.model_dump_json(), now, prod_id),
                    )
                else:
                    pub_status = "published" if dedup_key in self._sent_keys_cache else "unpublished"
                    proc_status = "published" if pub_status == "published" else processing_status
                    conn.execute(
                        """
                        INSERT INTO products_catalog (
                            product_id, dedup_key, source_id, query_terms, name, product_url,
                            affiliate_url, affiliate_status, original_price, promotional_price,
                            discount_percentage, rating, reviews_count, seller, image_url,
                            available, first_seen_at, last_seen_at,
                            processing_status, publication_status, times_seen
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 1)
                        """,
                        (
                            prod_id,
                            dedup_key,
                            source_id,
                            query_term.strip(),
                            item.name or "Sem título",
                            item.url or "",
                            item.affiliate_url,
                            item.affiliate_status,
                            item.original_price,
                            item.promotional_price,
                            item.discount_percentage,
                            item.rating,
                            item.reviews_count,
                            item.seller,
                            item.image_url,
                            now,
                            now,
                            proc_status,
                            pub_status,
                        ),
                    )
                upserted_ids.append(prod_id)

            placeholders = ",".join("?" for _ in upserted_ids)
            rows = conn.execute(
                f"SELECT * FROM products_catalog WHERE product_id IN ({placeholders})",
                upserted_ids,
            ).fetchall()
            return [self._row_to_catalog_product(r) for r in rows]

    def update_catalog_product_status(
        self,
        product_id: str,
        *,
        processing_status: Optional[str] = None,
        publication_status: Optional[str] = None,
        affiliate_url: Optional[str] = None,
        affiliate_status: Optional[str] = None,
        destination_id: Optional[str] = None,
        published_at: Optional[str] = None,
        last_error: Optional[str] = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM products_catalog WHERE product_id = ?",
                (product_id,),
            ).fetchone()
            if not row:
                return
            conn.execute(
                """
                UPDATE products_catalog SET
                    processing_status = ?,
                    publication_status = ?,
                    affiliate_url = ?,
                    affiliate_status = ?,
                    last_published_destination_id = ?,
                    last_published_at = ?,
                    last_error = ?
                WHERE product_id = ?
                """,
                (
                    processing_status or row["processing_status"],
                    publication_status or row["publication_status"],
                    affiliate_url if affiliate_url is not None else row["affiliate_url"],
                    affiliate_status if affiliate_status is not None else row["affiliate_status"],
                    destination_id if destination_id is not None else row["last_published_destination_id"],
                    published_at if published_at is not None else row["last_published_at"],
                    last_error,
                    product_id,
                ),
            )

    def enqueue_pending_offers(
        self,
        cycle_id: str,
        cycle_name: str,
        source_id: str,
        candidates: list[tuple[str, ProductItem]],
        destinations: list[OutputDestinationConfig],
    ) -> int:
        """Enfileira produtos aprovados e inéditos na fila persistente (`offer_queue`).

        Garante que uma oferta nunca seja enfileirada ou enviada duas vezes para o mesmo destino.
        Retorna a quantidade de produtos distintos adicionados/atualizados na fila.
        """
        if not candidates:
            return 0

        now = _now_iso()
        target_dests = destinations or [
            OutputDestinationConfig(
                id="__unassigned__",
                name="Pendente de Destino",
                output_type="telegram",
                enabled=True,
                config={},
            )
        ]
        enqueued_products: set[str] = set()

        with self._lock, self._connect() as conn:
            for dedup_key, item in candidates:
                prod_id, _ = self.canonical_product_identity(source_id, item)
                if not item.id:
                    item.id = prod_id

                any_dest_queued = False
                for dest in target_dests:
                    dest_id = dest.id or "__unassigned__"
                    # Nunca enfileira se já foi publicado neste destino
                    if dest_id != "__unassigned__" and (dedup_key, dest_id) in self._sent_dest_cache:
                        continue

                    existing_q = conn.execute(
                        "SELECT id, status FROM offer_queue WHERE dedup_key = ? AND destination_id = ?",
                        (dedup_key, dest_id),
                    ).fetchone()

                    if existing_q:
                        if existing_q["status"] == "sent":
                            continue
                        conn.execute(
                            """
                            UPDATE offer_queue SET
                                cycle_id = ?,
                                cycle_name = ?,
                                product_snapshot_json = ?,
                                affiliate_url = COALESCE(?, affiliate_url),
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                cycle_id,
                                cycle_name,
                                item.model_dump_json(),
                                item.affiliate_url,
                                now,
                                existing_q["id"],
                            ),
                        )
                        any_dest_queued = True
                    else:
                        q_id = f"q_{uuid.uuid4().hex[:12]}"
                        conn.execute(
                            """
                            INSERT INTO offer_queue (
                                id, product_id, dedup_key, cycle_id, cycle_name, source_id,
                                destination_id, destination_name, product_snapshot_json,
                                affiliate_url, status, attempts, max_attempts, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, 3, ?, ?)
                            """,
                            (
                                q_id,
                                prod_id,
                                dedup_key,
                                cycle_id,
                                cycle_name,
                                source_id,
                                dest_id,
                                dest.name,
                                item.model_dump_json(),
                                item.affiliate_url,
                                now,
                                now,
                            ),
                        )
                        any_dest_queued = True

                if any_dest_queued:
                    enqueued_products.add(prod_id)
                    conn.execute(
                        """
                        UPDATE products_catalog SET
                            processing_status = CASE WHEN publication_status = 'published' THEN processing_status ELSE 'queued' END,
                            publication_status = CASE WHEN publication_status = 'published' THEN publication_status ELSE 'pending' END,
                            affiliate_url = COALESCE(?, affiliate_url),
                            affiliate_status = COALESCE(?, affiliate_status)
                        WHERE product_id = ?
                        """,
                        (item.affiliate_url, item.affiliate_status, prod_id),
                    )

        return len(enqueued_products)

    def fetch_pending_candidates_for_cycle(
        self,
        cycle: CycleModel,
        destinations: list[OutputDestinationConfig],
        passes_filters_fn: Any,
    ) -> list[tuple[str, ProductItem]]:
        """Recupera produtos pendentes já armazenados na fila (`offer_queue`) ou no banco (`products_catalog`)
        que atendam aos filtros e palavras-chave do ciclo e ainda não tenham sido publicados nos destinos ativos.
        """
        from app.parser import parse_keywords

        now = _now_iso()
        active_dest_ids = [d.id for d in destinations if d.id]
        keywords = [k.lower() for k in parse_keywords(cycle.query)]
        results: list[tuple[str, ProductItem]] = []
        seen_dedup: set[str] = set()

        with self._lock, self._connect() as conn:
            # 1. Reativa itens cujo tempo de espera de retry (next_retry_at) já venceu ou que ficaram em 'processing'
            conn.execute(
                """
                UPDATE offer_queue
                SET status = 'pending', updated_at = ?
                WHERE status = 'processing'
                   OR (status = 'retry_wait' AND (next_retry_at IS NULL OR next_retry_at <= ?))
                """,
                (now, now),
            )

            # 2. Busca itens pendentes diretamente vinculados ao ciclo (ou compatíveis com seus destinos)
            q_rows = conn.execute(
                """
                SELECT * FROM offer_queue
                WHERE status IN ('pending', 'retry_wait')
                  AND attempts < max_attempts
                  AND cycle_id = ?
                ORDER BY created_at ASC
                """,
                (cycle.id,),
            ).fetchall()

            for row in q_rows:
                dk = row["dedup_key"]
                if dk in seen_dedup:
                    continue
                if cycle.deduplicate:
                    if active_dest_ids:
                        if all((dk, d_id) in self._sent_dest_cache for d_id in active_dest_ids):
                            continue
                    elif dk in self._sent_keys_cache:
                        continue

                try:
                    item = ProductItem(**json.loads(row["product_snapshot_json"]))
                except Exception:
                    continue

                if not passes_filters_fn(item, cycle.filters):
                    continue

                seen_dedup.add(dk)
                results.append((dk, item))

            # 3. Também verifica produtos não publicados no `products_catalog` que correspondam aos termos do ciclo
            cat_rows = conn.execute(
                """
                SELECT * FROM products_catalog
                WHERE source_id = ?
                  AND available = 1
                  AND publication_status IN ('unpublished', 'pending', 'partial', 'failed')
                ORDER BY last_seen_at DESC
                LIMIT 200
                """,
                (cycle.source_id,),
            ).fetchall()

            for crow in cat_rows:
                dk = crow["dedup_key"]
                if dk in seen_dedup:
                    continue
                if cycle.deduplicate:
                    if active_dest_ids:
                        if all((dk, d_id) in self._sent_dest_cache for d_id in active_dest_ids):
                            continue
                    elif dk in self._sent_keys_cache:
                        continue

                # Verifica se o produto pertence a algum termo do ciclo
                stored_terms = (crow["query_terms"] or "").lower()
                prod_title = (crow["name"] or "").lower()
                if keywords and not any(kw in stored_terms or kw in prod_title for kw in keywords):
                    continue

                item = ProductItem(
                    id=crow["product_id"],
                    name=crow["name"],
                    url=crow["product_url"],
                    affiliate_url=crow["affiliate_url"],
                    affiliate_status=crow["affiliate_status"],
                    original_price=crow["original_price"],
                    promotional_price=crow["promotional_price"],
                    discount_percentage=crow["discount_percentage"],
                    rating=crow["rating"],
                    reviews_count=crow["reviews_count"],
                    seller=crow["seller"],
                    image_url=crow["image_url"],
                )
                if not passes_filters_fn(item, cycle.filters):
                    continue

                seen_dedup.add(dk)
                results.append((dk, item))

        return results

    def mark_queue_delivery_outcome(
        self,
        *,
        product_id: str,
        dedup_key: str,
        destination_id: str,
        success: bool,
        affiliate_url: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Atualiza de forma transacional a fila (`offer_queue`) e o catálogo (`products_catalog`) após tentativa de envio."""
        now = _now_iso()
        retry_iso = (datetime.now().astimezone() + timedelta(seconds=30)).isoformat()

        with self._lock, self._connect() as conn:
            if success:
                self._sent_keys_cache.add(dedup_key)
                if destination_id:
                    self._sent_dest_cache.add((dedup_key, destination_id))

                conn.execute(
                    """
                    UPDATE offer_queue SET
                        status = 'sent',
                        attempts = attempts + 1,
                        affiliate_url = COALESCE(?, affiliate_url),
                        last_error = NULL,
                        sent_at = ?,
                        updated_at = ?
                    WHERE dedup_key = ? AND destination_id IN (?, '__unassigned__')
                    """,
                    (affiliate_url, now, now, dedup_key, destination_id),
                )
                conn.execute(
                    """
                    UPDATE products_catalog SET
                        processing_status = 'published',
                        publication_status = 'published',
                        affiliate_url = COALESCE(?, affiliate_url),
                        affiliate_status = 'generated',
                        last_published_destination_id = ?,
                        last_published_at = ?,
                        last_error = NULL
                    WHERE product_id = ? OR dedup_key = ?
                    """,
                    (affiliate_url, destination_id, now, product_id, dedup_key),
                )
            else:
                conn.execute(
                    """
                    UPDATE offer_queue SET
                        attempts = attempts + 1,
                        status = CASE WHEN (attempts + 1) >= max_attempts THEN 'failed' ELSE 'retry_wait' END,
                        next_retry_at = CASE WHEN (attempts + 1) >= max_attempts THEN NULL ELSE ? END,
                        last_error = ?,
                        updated_at = ?
                    WHERE dedup_key = ? AND destination_id IN (?, '__unassigned__')
                    """,
                    (retry_iso, error, now, dedup_key, destination_id),
                )
                conn.execute(
                    """
                    UPDATE products_catalog SET
                        publication_status = CASE WHEN publication_status = 'published' THEN 'published' ELSE 'failed' END,
                        last_error = ?
                    WHERE product_id = ? OR dedup_key = ?
                    """,
                    (error, product_id, dedup_key),
                )

    def retry_failed_queue_items(self, cycle_id: Optional[str] = None) -> int:
        """Reativa ofertas com falha (`failed` ou `retry_wait`) na fila persistente para reprocessamento."""
        now = _now_iso()
        with self._lock, self._connect() as conn:
            if cycle_id:
                cur = conn.execute(
                    """
                    UPDATE offer_queue SET
                        status = 'pending',
                        attempts = 0,
                        next_retry_at = NULL,
                        updated_at = ?
                    WHERE cycle_id = ? AND status IN ('failed', 'retry_wait')
                    """,
                    (now, cycle_id),
                )
            else:
                cur = conn.execute(
                    """
                    UPDATE offer_queue SET
                        status = 'pending',
                        attempts = 0,
                        next_retry_at = NULL,
                        updated_at = ?
                    WHERE status IN ('failed', 'retry_wait')
                    """,
                    (now,),
                )
            return cur.rowcount

    def list_catalog_products(
        self,
        limit: int = 100,
        publication_status: Optional[str] = None,
        processing_status: Optional[str] = None,
    ) -> list[CatalogProductRecord]:
        with self._lock, self._connect() as conn:
            clauses: list[str] = []
            params: list[Any] = []
            if publication_status:
                clauses.append("publication_status = ?")
                params.append(publication_status)
            if processing_status:
                clauses.append("processing_status = ?")
                params.append(processing_status)
            where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            params.append(limit)
            rows = conn.execute(
                f"SELECT * FROM products_catalog {where_sql} ORDER BY last_seen_at DESC LIMIT ?",
                params,
            ).fetchall()
            return [self._row_to_catalog_product(r) for r in rows]

    def list_offer_queue(
        self,
        limit: int = 100,
        status: Optional[str] = None,
        cycle_id: Optional[str] = None,
    ) -> list[QueuedOfferRecord]:
        with self._lock, self._connect() as conn:
            clauses: list[str] = []
            params: list[Any] = []
            if status:
                clauses.append("status = ?")
                params.append(status)
            if cycle_id:
                clauses.append("cycle_id = ?")
                params.append(cycle_id)
            where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            params.append(limit)
            rows = conn.execute(
                f"SELECT * FROM offer_queue {where_sql} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
            return [self._row_to_queued_offer(r) for r in rows]

    @staticmethod
    def _row_to_catalog_product(row: sqlite3.Row) -> CatalogProductRecord:
        return CatalogProductRecord(
            product_id=row["product_id"],
            dedup_key=row["dedup_key"],
            source_id=row["source_id"],
            query_terms=row["query_terms"] or "",
            name=row["name"],
            product_url=row["product_url"],
            affiliate_url=row["affiliate_url"],
            affiliate_status=row["affiliate_status"],
            original_price=row["original_price"],
            promotional_price=row["promotional_price"],
            discount_percentage=row["discount_percentage"],
            rating=row["rating"],
            reviews_count=row["reviews_count"],
            seller=row["seller"],
            image_url=row["image_url"],
            available=bool(row["available"]),
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
            processing_status=row["processing_status"],
            publication_status=row["publication_status"],
            last_published_destination_id=row["last_published_destination_id"],
            last_published_at=row["last_published_at"],
            times_seen=int(row["times_seen"] or 1),
            last_error=row["last_error"],
        )

    @staticmethod
    def _row_to_queued_offer(row: sqlite3.Row) -> QueuedOfferRecord:
        snap = json.loads(row["product_snapshot_json"] or "{}")
        return QueuedOfferRecord(
            id=row["id"],
            product_id=row["product_id"],
            dedup_key=row["dedup_key"],
            cycle_id=row["cycle_id"],
            cycle_name=row["cycle_name"],
            source_id=row["source_id"],
            destination_id=row["destination_id"],
            destination_name=row["destination_name"],
            product_snapshot=ProductItem(**snap),
            affiliate_url=row["affiliate_url"],
            status=row["status"],
            attempts=int(row["attempts"] or 0),
            max_attempts=int(row["max_attempts"] or 3),
            next_retry_at=row["next_retry_at"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            sent_at=row["sent_at"],
        )

    def list_sent_offers(self, limit: int = 100, cycle_id: Optional[str] = None) -> list[SentOfferRecord]:
        with self._lock, self._connect() as conn:
            if cycle_id:
                rows = conn.execute(
                    "SELECT * FROM sent_offers WHERE cycle_id = ? ORDER BY sent_at DESC LIMIT ?",
                    (cycle_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM sent_offers ORDER BY sent_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [
                SentOfferRecord(
                    id=r["id"],
                    dedup_key=r["dedup_key"],
                    cycle_id=r["cycle_id"],
                    cycle_name=r["cycle_name"],
                    source_id=r["source_id"],
                    destination_id=r["destination_id"],
                    destination_name=r["destination_name"],
                    product_id=r["product_id"],
                    product_name=r["product_name"],
                    original_price=r["original_price"],
                    promotional_price=r["promotional_price"],
                    discount_percentage=r["discount_percentage"],
                    rating=r["rating"],
                    product_url=r["product_url"],
                    affiliate_url=r["affiliate_url"],
                    image_url=r["image_url"],
                    status=r["status"],
                    error_message=r["error_message"],
                    sent_at=r["sent_at"],
                )
                for r in rows
            ]

    # -----------------------------------------------------------------
    # Cycle Execution History Logs (Com Telemetria por Etapa)
    # -----------------------------------------------------------------

    def save_cycle_run(self, run_log: CycleRunLog) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cycle_runs (
                    id, cycle_id, cycle_name, source_id, query,
                    scheduled_at, started_at, finished_at,
                    duration_seconds, schedule_delay_seconds,
                    navigation_ms, extraction_ms, processing_ms, affiliate_ms, dispatch_ms,
                    bottleneck_stage, status,
                    searched_count, filtered_count, affiliate_ok_count, affiliate_fail_count,
                    deduplicated_new_count, sent_count, failed_send_count, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    scheduled_at = excluded.scheduled_at,
                    finished_at = excluded.finished_at,
                    duration_seconds = excluded.duration_seconds,
                    schedule_delay_seconds = excluded.schedule_delay_seconds,
                    navigation_ms = excluded.navigation_ms,
                    extraction_ms = excluded.extraction_ms,
                    processing_ms = excluded.processing_ms,
                    affiliate_ms = excluded.affiliate_ms,
                    dispatch_ms = excluded.dispatch_ms,
                    bottleneck_stage = excluded.bottleneck_stage,
                    status = excluded.status,
                    searched_count = excluded.searched_count,
                    filtered_count = excluded.filtered_count,
                    affiliate_ok_count = excluded.affiliate_ok_count,
                    affiliate_fail_count = excluded.affiliate_fail_count,
                    deduplicated_new_count = excluded.deduplicated_new_count,
                    sent_count = excluded.sent_count,
                    failed_send_count = excluded.failed_send_count,
                    error_message = excluded.error_message
                """,
                (
                    run_log.id,
                    run_log.cycle_id,
                    run_log.cycle_name,
                    run_log.source_id,
                    run_log.query,
                    run_log.scheduled_at,
                    run_log.started_at,
                    run_log.finished_at,
                    float(run_log.duration_seconds),
                    float(run_log.schedule_delay_seconds),
                    float(run_log.navigation_ms),
                    float(run_log.extraction_ms),
                    float(run_log.processing_ms),
                    float(run_log.affiliate_ms),
                    float(run_log.dispatch_ms),
                    run_log.bottleneck_stage,
                    run_log.status,
                    run_log.searched_count,
                    run_log.filtered_count,
                    run_log.affiliate_ok_count,
                    run_log.affiliate_fail_count,
                    run_log.deduplicated_new_count,
                    run_log.sent_count,
                    run_log.failed_send_count,
                    run_log.error_message,
                ),
            )

    def list_cycle_runs(self, limit: int = 50, cycle_id: Optional[str] = None) -> list[CycleRunLog]:
        with self._lock, self._connect() as conn:
            if cycle_id:
                rows = conn.execute(
                    "SELECT * FROM cycle_runs WHERE cycle_id = ? ORDER BY started_at DESC LIMIT ?",
                    (cycle_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM cycle_runs ORDER BY started_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            result: list[CycleRunLog] = []
            for r in rows:
                rk = r.keys()
                result.append(
                    CycleRunLog(
                        id=r["id"],
                        cycle_id=r["cycle_id"],
                        cycle_name=r["cycle_name"],
                        source_id=r["source_id"],
                        query=r["query"],
                        scheduled_at=r["scheduled_at"] if "scheduled_at" in rk else None,
                        started_at=r["started_at"],
                        finished_at=r["finished_at"],
                        duration_seconds=float(r["duration_seconds"] or 0.0) if "duration_seconds" in rk else 0.0,
                        schedule_delay_seconds=float(r["schedule_delay_seconds"] or 0.0) if "schedule_delay_seconds" in rk else 0.0,
                        navigation_ms=float(r["navigation_ms"] or 0.0) if "navigation_ms" in rk else 0.0,
                        extraction_ms=float(r["extraction_ms"] or 0.0) if "extraction_ms" in rk else 0.0,
                        processing_ms=float(r["processing_ms"] or 0.0) if "processing_ms" in rk else 0.0,
                        affiliate_ms=float(r["affiliate_ms"] or 0.0) if "affiliate_ms" in rk else 0.0,
                        dispatch_ms=float(r["dispatch_ms"] or 0.0) if "dispatch_ms" in rk else 0.0,
                        bottleneck_stage=r["bottleneck_stage"] if "bottleneck_stage" in rk else None,
                        status=r["status"],
                        searched_count=int(r["searched_count"]),
                        filtered_count=int(r["filtered_count"]),
                        affiliate_ok_count=int(r["affiliate_ok_count"]),
                        affiliate_fail_count=int(r["affiliate_fail_count"]),
                        deduplicated_new_count=int(r["deduplicated_new_count"]),
                        sent_count=int(r["sent_count"]),
                        failed_send_count=int(r["failed_send_count"]),
                        error_message=r["error_message"],
                    )
                )
            return result

    def clear_history(self) -> None:
        """Limpa os registros de ofertas enviadas, fila, catálogo, execuções de ciclos e cache em memória."""
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM sent_offers")
            conn.execute("DELETE FROM offer_queue")
            conn.execute("DELETE FROM products_catalog")
            conn.execute("DELETE FROM cycle_runs")
            self._sent_keys_cache.clear()
            self._sent_dest_cache.clear()
        logger.info("Histórico de ofertas enviadas, fila, banco de produtos e execuções limpo.")


storage_service = StorageService()
