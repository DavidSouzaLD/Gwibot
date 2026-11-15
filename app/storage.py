"""Camada de persistência SQLite para configurações de afiliados, destinos, ciclos, deduplicação e histórico."""
from __future__ import annotations

from datetime import datetime
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
    CycleCreateRequest,
    CycleFilters,
    CycleModel,
    CycleRunLog,
    CycleStateType,
    DEFAULT_PLATFORM_TEMPLATES,
    OutputDestinationConfig,
    PlatformTemplateModel,
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
                CREATE INDEX IF NOT EXISTS idx_sent_offers_sent_at ON sent_offers(sent_at DESC);
                CREATE INDEX IF NOT EXISTS idx_cycle_runs_started_at ON cycle_runs(started_at DESC);
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

            # Pré-aquece o cache em memória de chaves de produtos já enviados com sucesso
            sent_rows = conn.execute(
                "SELECT DISTINCT dedup_key FROM sent_offers WHERE status = 'sent'"
            ).fetchall()
            self._sent_keys_cache = {r["dedup_key"] for r in sent_rows if r["dedup_key"]}

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

    def is_offer_already_sent(self, dedup_key: str) -> bool:
        """Verifica em O(1) via cache em memória sincronizado se `dedup_key` já foi enviado."""
        if not dedup_key:
            return False
        with self._lock:
            return dedup_key in self._sent_keys_cache

    def filter_unsent_dedup_keys(self, dedup_keys: list[str]) -> set[str]:
        """Retorna o subconjunto de chaves que AINDA NÃO foram enviadas com sucesso."""
        if not dedup_keys:
            return set()
        with self._lock:
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
        """Limpa os registros de ofertas enviadas, execuções de ciclos e cache em memória."""
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM sent_offers")
            conn.execute("DELETE FROM cycle_runs")
            self._sent_keys_cache.clear()
        logger.info("Histórico de ofertas enviadas e execuções limpo.")


storage_service = StorageService()
