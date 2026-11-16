"""Motor de agendamento de cadência fixa e pipeline orientado a Banco de Produtos e Fila Persistente.

Novo Fluxo:
Busca -> Banco de Produtos (products_catalog) -> Deduplicação -> Fila de Pendentes (offer_queue) -> Link Afiliado -> Output
- O scheduler prioriza o processamento de ofertas já armazenadas na fila/catálogo antes de abrir o navegador.
- Garante que nenhuma oferta seja publicada duas vezes no mesmo destino.
- Reprocessa automaticamente ofertas pendentes ou com falha transitória de envio.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import random
import time
import uuid

from app.affiliates import affiliate_registry
from app.logger import logger
from app.models import (
    CycleFilters,
    CycleModel,
    CycleRunLog,
    ProductAuditRecord,
    ProductItem,
    SentOfferRecord,
)
from app.outputs import output_registry
from app.parser import parse_keywords
from app.sources import source_registry
from app.storage import storage_service


def _now_dt() -> datetime:
    return datetime.now().astimezone()


def _now_iso() -> str:
    return _now_dt().isoformat()


def _future_iso(minutes: int, base_dt: datetime | None = None) -> str:
    ref = base_dt or _now_dt()
    return (ref + timedelta(minutes=minutes)).isoformat()


def _parse_iso_safe(iso_str: str | None) -> datetime | None:
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            return dt.astimezone()
        return dt
    except Exception:
        return None


class CycleScheduler:
    """Coordena a execução recorrente em cadência fixa, fila persistente de pendentes, deduplicação por destino e envio."""

    def __init__(self) -> None:
        self._running_locks: set[str] = set()
        self._scheduled_tasks: dict[str, asyncio.Task[None]] = {}
        self._scheduler_active = False
        self._cycle_concurrency_sem = asyncio.Semaphore(3)

    def is_cycle_executing_now(self, cycle_id: str) -> bool:
        """Retorna True se o ciclo está executando uma coleta/envio neste exato instante."""
        return cycle_id in self._running_locks

    def enrich_cycle_runtime_info(self, cycle: CycleModel) -> CycleModel:
        """Atualiza o campo `is_running_now` com o estado em tempo real."""
        cycle.is_running_now = self.is_cycle_executing_now(cycle.id)
        return cycle

    def list_cycles_with_runtime(self) -> list[CycleModel]:
        return [self.enrich_cycle_runtime_info(c) for c in storage_service.list_cycles()]

    def active_cycles_count(self) -> int:
        return sum(1 for c in storage_service.list_cycles() if c.state == "running")

    @staticmethod
    def compute_next_scheduled_slot(
        scheduled_dt: datetime,
        interval_minutes: int,
        now_dt: datetime | None = None,
    ) -> datetime:
        """Calcula o próximo horário previsto ancorado na grade de início (start-to-start)."""
        current_now = now_dt or _now_dt()
        step = timedelta(minutes=max(1, interval_minutes))
        next_slot = scheduled_dt + step
        if next_slot <= current_now:
            missed_steps = int((current_now - scheduled_dt) // step) + 1
            next_slot = scheduled_dt + (step * max(1, missed_steps))
        return next_slot

    @staticmethod
    def identify_bottleneck_stage(
        navigation_ms: float,
        extraction_ms: float,
        processing_ms: float,
        affiliate_ms: float,
        dispatch_ms: float,
    ) -> str:
        """Identifica a etapa que consumiu a maior fração do tempo da execução."""
        stages = {
            "navegacao": navigation_ms,
            "extracao": extraction_ms,
            "processamento": processing_ms,
            "afiliado": affiliate_ms,
            "envio": dispatch_ms,
        }
        top_stage, top_val = max(stages.items(), key=lambda kv: kv[1])
        if top_val <= 0.0:
            return "nenhum"
        return top_stage

    # -----------------------------------------------------------------
    # Etapas do Pipeline: Filter & Deduplication
    # -----------------------------------------------------------------

    @staticmethod
    def evaluate_filters(item: ProductItem, filters: CycleFilters) -> tuple[bool, str | None]:
        """Avalia se um produto atende aos filtros do ciclo e retorna `(aprovado, motivo_descarte)`."""
        if not item.url or not item.url.strip().startswith(("http://", "https://")):
            return False, "Produto sem URL HTTP/HTTPS válida"

        price = item.promotional_price
        if filters.min_price is not None:
            if price is None:
                return False, f"Preço promocional ausente (mínimo exigido: R$ {filters.min_price:.2f})"
            if price < filters.min_price:
                return False, f"Preço R$ {price:.2f} abaixo do mínimo R$ {filters.min_price:.2f}"
        if filters.max_price is not None:
            if price is None:
                return False, f"Preço promocional ausente (máximo permitido: R$ {filters.max_price:.2f})"
            if price > filters.max_price:
                return False, f"Preço R$ {price:.2f} acima do máximo R$ {filters.max_price:.2f}"
        if filters.min_discount is not None and filters.min_discount > 0:
            discount = item.discount_percentage or 0
            if discount < filters.min_discount:
                return False, f"Desconto {discount}% abaixo do mínimo {filters.min_discount}%"
        if filters.min_rating is not None and filters.min_rating > 0:
            rating = item.rating or 0.0
            if rating < filters.min_rating:
                return False, f"Avaliação {rating:.1f} abaixo do mínimo {filters.min_rating:.1f}"
        return True, None

    @classmethod
    def passes_filters(cls, item: ProductItem, filters: CycleFilters) -> bool:
        """Avalia se um produto atende aos filtros de preço, desconto e avaliação do ciclo."""
        passed, _ = cls.evaluate_filters(item, filters)
        return passed

    @staticmethod
    def build_dedup_key(source_id: str, item: ProductItem) -> str:
        """Gera a chave canônica de deduplicação do produto baseada no ID do Mercado Livre."""
        _, dedup_key = storage_service.canonical_product_identity(source_id, item)
        return dedup_key

    # -----------------------------------------------------------------
    # Novo Fluxo: Busca -> Banco de Produtos -> Deduplicação -> Fila -> Link Afiliado -> Output
    # -----------------------------------------------------------------

    async def execute_cycle_once(
        self,
        cycle_id: str,
        scheduled_at: str | None = None,
    ) -> tuple[bool, CycleRunLog | None, str]:
        """Executa o pipeline completo priorizando ofertas pendentes já coletadas no banco."""
        cycle = storage_service.get_cycle(cycle_id)
        if not cycle:
            return False, None, "Ciclo não encontrado."

        if cycle_id in self._running_locks:
            msg = f"Execução simultânea bloqueada: o ciclo '{cycle.name}' ({cycle_id}) já está em andamento."
            logger.warning(msg)
            return False, None, msg

        self._running_locks.add(cycle_id)
        t_run_start = time.perf_counter()
        started_dt = _now_dt()
        started_iso = started_dt.isoformat()
        scheduled_iso = scheduled_at or cycle.next_run_at or started_iso
        scheduled_dt = _parse_iso_safe(scheduled_iso) or started_dt
        schedule_delay_sec = max(0.0, round((started_dt - scheduled_dt).total_seconds(), 2))

        run_id = f"run_{uuid.uuid4().hex[:10]}"
        run_log = CycleRunLog(
            id=run_id,
            cycle_id=cycle.id,
            cycle_name=cycle.name,
            source_id=cycle.source_id,
            query=cycle.query,
            scheduled_at=scheduled_iso,
            started_at=started_iso,
            schedule_delay_seconds=schedule_delay_sec,
            status="running",
        )
        storage_service.save_cycle_run(run_log)

        logger.info(
            "Iniciando execução do ciclo '%s' (%s) | query='%s' | limit=%d | previsto=%s | atraso=%.2fs",
            cycle.name,
            cycle.id,
            cycle.query,
            cycle.search_limit,
            scheduled_iso,
            schedule_delay_sec,
        )

        nav_ms_acc = 0.0
        ext_ms_acc = 0.0
        proc_ms_acc = 0.0
        aff_ms_acc = 0.0
        disp_ms_acc = 0.0

        try:
            async with self._cycle_concurrency_sem:
                source = source_registry.get(cycle.source_id)
                if not source.supported:
                    raise RuntimeError(f"Fonte '{source.display_name}' ainda não está ativa para coleta.")

                keywords = parse_keywords(cycle.query)
                is_random_mode = len(keywords) == 0
                if is_random_mode:
                    keywords = [""]

                aff_provider = affiliate_registry.get(cycle.source_id)
                aff_config = storage_service.get_affiliate_config(cycle.source_id)

                destinations = [
                    d
                    for d_id in cycle.destination_ids
                    if (d := storage_service.get_destination(d_id)) is not None and d.enabled
                ]
                active_dest_ids = [d.id for d in destinations if d.id]

                candidates_to_send: list[tuple[str, ProductItem]] = []
                seen_in_batch: set[str] = set()
                kw_warnings: list[str] = []
                any_blocked = False
                total_searched = 0
                total_filtered = 0
                total_discarded = 0
                total_affiliate_ok = 0
                total_affiliate_fail = 0
                audit_records: dict[tuple[str, str], ProductAuditRecord] = {}

                # ETAPA 0: Prioriza ofertas já armazenadas na Fila de Pendentes / Banco de Produtos
                t_q_start = time.perf_counter()
                pending_stored = storage_service.fetch_pending_candidates_for_cycle(
                    cycle=cycle,
                    destinations=destinations,
                    passes_filters_fn=self.passes_filters,
                )
                for dk, p_item in pending_stored:
                    if dk not in seen_in_batch:
                        seen_in_batch.add(dk)
                        candidates_to_send.append((dk, p_item))
                proc_ms_acc += (time.perf_counter() - t_q_start) * 1000.0

                if candidates_to_send:
                    logger.info(
                        "Ciclo '%s' | Recuperados %d produtos pendentes do banco local antes da busca.",
                        cycle.name,
                        len(candidates_to_send),
                    )

                last_offset = getattr(cycle, "last_page_offset", 1) or 1
                if is_random_mode:
                    # Modo aleatório: sorteia página inicial diferente da última para variar ofertas entre ciclos
                    candidate_pages = [p for p in range(1, 16) if p != last_offset]
                    current_page = random.choice(candidate_pages)
                    visited_random_pages: set[int] = {current_page}
                else:
                    current_page = last_offset
                    visited_random_pages = set()
                # Paginação incremental conservadora: no máximo 2 páginas quando a fila local precisa de novos itens
                max_pages_to_scan = 2
                pages_scanned = 0

                # Só realiza nova busca no navegador se a fila local tiver menos produtos que `max_offers_per_run`
                while len(candidates_to_send) < cycle.max_offers_per_run and pages_scanned < max_pages_to_scan:
                    pages_scanned += 1
                    logger.info(
                        "Ciclo '%s' | Busca incremental página %d (cursor=%d) | pendentes atuais=%d/%d",
                        cycle.name,
                        pages_scanned,
                        current_page,
                        len(candidates_to_send),
                        cycle.max_offers_per_run,
                    )

                    # ETAPA 1: Busca no Mercado Livre (reutilizando perfil persistente e salvando em products_catalog)
                    t_search_stage = time.perf_counter()
                    if is_random_mode:
                        try:
                            batch_responses = await source.search_keywords_batch(
                                keywords=keywords,
                                limit_per_keyword=cycle.search_limit,
                                page_number=current_page,
                                enrich_affiliates=False,
                                randomize_general_route=True,
                            )
                        except TypeError:
                            batch_responses = await source.search_keywords_batch(
                                keywords=keywords,
                                limit_per_keyword=cycle.search_limit,
                                page_number=current_page,
                                enrich_affiliates=False,
                            )
                    else:
                        batch_responses = await source.search_keywords_batch(
                            keywords=keywords,
                            limit_per_keyword=cycle.search_limit,
                            page_number=current_page,
                            enrich_affiliates=False,
                        )
                    search_wall_ms = (time.perf_counter() - t_search_stage) * 1000.0

                    per_kw_buckets: list[list[ProductItem]] = []
                    page_had_results = False
                    page_nav_ms = 0.0
                    page_ext_ms = 0.0

                    for kw, kw_res in zip(keywords, batch_responses):
                        kw_label = kw or "Geral"
                        page_nav_ms += kw_res.metadata.navigation_ms
                        page_ext_ms += kw_res.metadata.extraction_ms

                        if kw_res.status == "blocked":
                            any_blocked = True
                            if kw_res.metadata.warning:
                                kw_warnings.append(f"[{kw_label}] {kw_res.metadata.warning}")
                            continue

                        if not kw_res.results:
                            warn = kw_res.metadata.warning or f"Sem resultados para '{kw_label}' na página {current_page}."
                            kw_warnings.append(f"[{kw_label}] {warn}")
                            continue

                        page_had_results = True
                        per_kw_buckets.append(kw_res.results)

                    if page_nav_ms == 0.0 and page_ext_ms == 0.0 and search_wall_ms > 0.0:
                        page_nav_ms = search_wall_ms * 0.75
                        page_ext_ms = search_wall_ms * 0.25

                    nav_ms_acc += page_nav_ms
                    ext_ms_acc += page_ext_ms

                    if any_blocked and not page_had_results:
                        current_page = 1
                        break

                    # ETAPA 2: Banco de Produtos & Filtros
                    t_proc_start = time.perf_counter()
                    raw_products_page: list[ProductItem] = []
                    max_bucket_len = max((len(b) for b in per_kw_buckets), default=0)
                    for i in range(max_bucket_len):
                        for bucket in per_kw_buckets:
                            if i < len(bucket):
                                raw_products_page.append(bucket[i])

                    total_searched += len(raw_products_page)

                    if not page_had_results:
                        proc_ms_acc += (time.perf_counter() - t_proc_start) * 1000.0
                        logger.info(
                            "Ciclo '%s' | Página %d não retornou produtos; reiniciando cursor para a página 1.",
                            cycle.name,
                            current_page,
                        )
                        if is_random_mode and current_page > 1 and pages_scanned < max_pages_to_scan:
                            current_page = 1
                            visited_random_pages.add(1)
                            continue
                        current_page = 1
                        break

                    filtered_page: list[ProductItem] = []
                    for p in raw_products_page:
                        t_f_item = time.perf_counter()
                        prod_id, prod_dk = storage_service.canonical_product_identity(cycle.source_id, p)
                        if not p.id:
                            p.id = prod_id
                        passed, discard_reason = self.evaluate_filters(p, cycle.filters)
                        f_ms = round((time.perf_counter() - t_f_item) * 1000.0, 2)
                        now_audit = _now_iso()
                        if passed:
                            filtered_page.append(p)
                            audit_records[(prod_dk, "__pipeline__")] = ProductAuditRecord(
                                id=f"aud_{run_id}_{prod_id}",
                                run_id=run_id,
                                cycle_id=cycle.id,
                                cycle_name=cycle.name,
                                product_id=prod_id,
                                dedup_key=prod_dk,
                                source_id=cycle.source_id,
                                filter_passed=True,
                                queue_status="eligible",
                                filter_ms=f_ms,
                                created_at=now_audit,
                                updated_at=now_audit,
                            )
                        else:
                            total_discarded += 1
                            storage_service.update_catalog_product_status(
                                prod_id,
                                processing_status="filtered_out",
                                last_error=discard_reason,
                            )
                            audit_records[(prod_dk, "__pipeline__")] = ProductAuditRecord(
                                id=f"aud_{run_id}_{prod_id}",
                                run_id=run_id,
                                cycle_id=cycle.id,
                                cycle_name=cycle.name,
                                product_id=prod_id,
                                dedup_key=prod_dk,
                                source_id=cycle.source_id,
                                filter_passed=False,
                                filter_reason=discard_reason,
                                queue_status="discarded",
                                filter_ms=f_ms,
                                created_at=now_audit,
                                updated_at=now_audit,
                            )

                    total_filtered += len(filtered_page)
                    proc_ms_acc += (time.perf_counter() - t_proc_start) * 1000.0

                    # ETAPA 3: Link Afiliado (para produtos aprovados nos filtros)
                    t_aff_start = time.perf_counter()
                    if filtered_page:
                        await aff_provider.enrich_products(filtered_page, config=aff_config)
                    aff_page_ms = (time.perf_counter() - t_aff_start) * 1000.0
                    aff_ms_acc += aff_page_ms
                    per_item_aff_ms = round(aff_page_ms / max(1, len(filtered_page)), 2)

                    # ETAPA 4: Deduplicação por ID/Destino & Enfileiramento na Fila de Pendentes
                    t_dedup_start = time.perf_counter()
                    eligible_with_keys: list[tuple[str, ProductItem]] = []
                    for item in filtered_page:
                        prod_id, dedup_key = storage_service.canonical_product_identity(cycle.source_id, item)
                        aud = audit_records.get((dedup_key, "__pipeline__"))
                        if aud:
                            aud.affiliate_ms = per_item_aff_ms
                        if item.affiliate_status == "generated" and item.affiliate_url:
                            total_affiliate_ok += 1
                        else:
                            total_affiliate_fail += 1

                        if cycle.filters.require_affiliate_link and item.affiliate_status != "generated":
                            total_discarded += 1
                            if aud:
                                aud.filter_passed = False
                                aud.filter_reason = item.affiliate_error or "Falha na geração do link de afiliado"
                                aud.queue_status = "discarded"
                                aud.error_type = "affiliate"
                                aud.error_message = item.affiliate_error
                                aud.updated_at = _now_iso()
                            continue

                        if not dedup_key or dedup_key in seen_in_batch:
                            continue
                        seen_in_batch.add(dedup_key)
                        eligible_with_keys.append((dedup_key, item))

                    if cycle.deduplicate and eligible_with_keys:
                        unsent_keys = storage_service.filter_unsent_dedup_keys(
                            [dk for dk, _ in eligible_with_keys],
                            destination_ids=active_dest_ids if active_dest_ids else None,
                            resend_after_hours=cycle.resend_after_hours,
                        )
                        for dk, item in eligible_with_keys:
                            if dk in unsent_keys:
                                candidates_to_send.append((dk, item))
                    else:
                        candidates_to_send.extend(eligible_with_keys)

                    proc_ms_acc += (time.perf_counter() - t_dedup_start) * 1000.0

                    if any_blocked:
                        current_page = 1
                        break

                    if is_random_mode:
                        remaining_pages = [p for p in range(1, 16) if p not in visited_random_pages]
                        current_page = random.choice(remaining_pages) if remaining_pages else 1
                        visited_random_pages.add(current_page)
                    else:
                        current_page += 1
                        if current_page > 6:
                            current_page = 1

                # Garante que candidatos recuperados do banco sem link afiliado recebam conversão antes do envio
                items_needing_aff = [
                    item
                    for _, item in candidates_to_send
                    if not item.affiliate_url or item.affiliate_status != "generated"
                ]
                if items_needing_aff:
                    t_aff_extra = time.perf_counter()
                    _, ok_extra, fail_extra = await aff_provider.enrich_products(items_needing_aff, config=aff_config)
                    aff_ms_acc += (time.perf_counter() - t_aff_extra) * 1000.0
                    total_affiliate_ok += ok_extra
                    total_affiliate_fail += fail_extra
                    if cycle.filters.require_affiliate_link:
                        candidates_to_send = [
                            (dk, it)
                            for dk, it in candidates_to_send
                            if it.affiliate_status == "generated" and it.affiliate_url
                        ]

                # Filtra apenas candidatos que ainda precisam ser enviados a pelo menos 1 destino ativo
                if destinations and cycle.deduplicate:
                    deliverable_candidates = [
                        (dk, it)
                        for dk, it in candidates_to_send
                        if any(
                            dest.id
                            and not storage_service.is_offer_already_sent(
                                dk, dest.id, resend_after_hours=cycle.resend_after_hours
                            )
                            for dest in destinations
                        )
                    ]
                else:
                    deliverable_candidates = list(candidates_to_send)

                if is_random_mode and len(deliverable_candidates) > 1:
                    random.shuffle(deliverable_candidates)

                selected_offers = deliverable_candidates[: cycle.max_offers_per_run]
                # Em modo aleatório, enfileira apenas os itens selecionados para esta execução (evita drenar backlog antigo e garante busca aleatória nova a cada ciclo).
                # Em modo nichado, persiste todos os candidatos inéditos na Fila de Pendentes (offer_queue).
                to_enqueue = selected_offers if is_random_mode else candidates_to_send
                if to_enqueue:
                    storage_service.enqueue_pending_offers(
                        cycle_id=cycle.id,
                        cycle_name=cycle.name,
                        source_id=cycle.source_id,
                        candidates=to_enqueue,
                        destinations=destinations,
                        resend_after_hours=cycle.resend_after_hours,
                        deduplicate=cycle.deduplicate,
                    )

                run_log.searched_count = total_searched
                run_log.filtered_count = total_filtered
                run_log.discarded_count = total_discarded
                run_log.affiliate_ok_count = total_affiliate_ok
                run_log.affiliate_fail_count = total_affiliate_fail
                run_log.deduplicated_new_count = len(deliverable_candidates)

                if any_blocked and not deliverable_candidates:
                    err_msg = " | ".join(kw_warnings) or "Coleta bloqueada por verificação do site."
                    run_log.status = "failed"
                    run_log.error_message = err_msg
                    run_log.finished_at = _now_iso()
                    run_log.duration_seconds = round(time.perf_counter() - t_run_start, 2)
                    run_log.navigation_ms = round(nav_ms_acc, 1)
                    run_log.extraction_ms = round(ext_ms_acc, 1)
                    run_log.processing_ms = round(proc_ms_acc, 1)
                    run_log.affiliate_ms = round(aff_ms_acc, 1)
                    run_log.dispatch_ms = 0.0
                    run_log.bottleneck_stage = self.identify_bottleneck_stage(
                        run_log.navigation_ms,
                        run_log.extraction_ms,
                        run_log.processing_ms,
                        run_log.affiliate_ms,
                        run_log.dispatch_ms,
                    )
                    storage_service.record_product_audit_batch(list(audit_records.values()))
                    storage_service.save_cycle_run(run_log)
                    refreshed_blk = storage_service.get_cycle(cycle.id)
                    cur_state_blk = refreshed_blk.state if refreshed_blk else cycle.state
                    next_blk = (
                        self.compute_next_scheduled_slot(
                            scheduled_dt=scheduled_dt,
                            interval_minutes=cycle.interval_minutes,
                        ).isoformat()
                        if cur_state_blk == "running"
                        else None
                    )
                    storage_service.update_cycle_state(
                        cycle.id,
                        cur_state_blk,
                        last_run_at=run_log.finished_at,
                        next_run_at=next_blk,
                        increment_runs=True,
                        last_error=err_msg,
                        last_page_offset=1,
                        last_duration_seconds=run_log.duration_seconds,
                        last_schedule_delay_seconds=run_log.schedule_delay_seconds,
                        last_bottleneck=run_log.bottleneck_stage,
                    )
                    return False, run_log, err_msg

                remaining_queued = 0 if is_random_mode else max(0, len(deliverable_candidates) - len(selected_offers))
                logger.info(
                    "Ciclo '%s' | Fila & Deduplicação: %d disponíveis (%d buscados em %d pág.) -> %d selecionados para envio, %d mantidos na fila.",
                    cycle.name,
                    len(deliverable_candidates),
                    total_searched,
                    pages_scanned,
                    len(selected_offers),
                    remaining_queued,
                )

                # ETAPA 5: OUTPUT DISPATCH (Garantia de não publicar 2x no mesmo destino + persistência imediata por produto)
                t_disp_start = time.perf_counter()
                sent_count = 0
                failed_send_count = 0
                last_send_error: str | None = None

                if not destinations and selected_offers:
                    last_send_error = "Nenhum destino de saída ativo vinculado a este ciclo."
                    logger.warning("Ciclo '%s' | %s", cycle.name, last_send_error)

                for idx, (dedup_key, item) in enumerate(selected_offers):
                    prod_id, _ = storage_service.canonical_product_identity(cycle.source_id, item)
                    if destinations:
                        # Filtra destinos para os quais este produto AINDA NÃO foi enviado (respeitando janela de reenvio)
                        target_dests_for_item = [
                            dest
                            for dest in destinations
                            if not (
                                cycle.deduplicate
                                and dest.id
                                and storage_service.is_offer_already_sent(
                                    dedup_key,
                                    dest.id,
                                    resend_after_hours=cycle.resend_after_hours,
                                )
                            )
                        ]
                        if not target_dests_for_item:
                            continue

                        t_item_disp = time.perf_counter()
                        deliveries = await asyncio.gather(
                            *(
                                output_registry.get(dest.output_type).send_offer(
                                    item, dest, cycle_name=cycle.name
                                )
                                for dest in target_dests_for_item
                            )
                        )
                        item_disp_ms = round((time.perf_counter() - t_item_disp) * 1000.0, 2)
                        now_sent_iso = _now_iso()
                        item_records: list[SentOfferRecord] = []

                        for dest, delivery in zip(target_dests_for_item, deliveries):
                            dest_id_str = dest.id or "unknown"
                            err_detail = delivery.error or (None if delivery.success else "Falha ao enviar oferta.")
                            offer_record = SentOfferRecord(
                                id=f"offer_{uuid.uuid4().hex[:10]}",
                                dedup_key=dedup_key,
                                cycle_id=cycle.id,
                                cycle_name=cycle.name,
                                source_id=cycle.source_id,
                                destination_id=dest_id_str,
                                destination_name=dest.name,
                                product_id=prod_id,
                                product_name=item.name or "Sem título",
                                original_price=item.original_price,
                                promotional_price=item.promotional_price,
                                discount_percentage=item.discount_percentage,
                                rating=item.rating,
                                product_url=item.url or "",
                                affiliate_url=item.affiliate_url or item.url or "",
                                image_url=item.image_url,
                                status="sent" if delivery.success else "failed",
                                error_message=err_detail,
                                sent_at=now_sent_iso,
                            )
                            item_records.append(offer_record)

                            storage_service.mark_queue_delivery_outcome(
                                product_id=prod_id,
                                dedup_key=dedup_key,
                                destination_id=dest_id_str,
                                success=delivery.success,
                                affiliate_url=item.affiliate_url,
                                error=err_detail,
                                error_type=delivery.error_type,
                                retry_after_seconds=delivery.retry_after_seconds,
                                api_status_code=delivery.status_code,
                            )

                            base_aud = audit_records.get((dedup_key, "__pipeline__"))
                            audit_records[(dedup_key, dest_id_str)] = ProductAuditRecord(
                                id=f"aud_{run_id}_{prod_id}_{dest_id_str}",
                                run_id=run_id,
                                cycle_id=cycle.id,
                                cycle_name=cycle.name,
                                product_id=prod_id,
                                dedup_key=dedup_key,
                                source_id=cycle.source_id,
                                destination_id=dest_id_str,
                                destination_name=dest.name,
                                filter_passed=True,
                                queue_status="sent" if delivery.success else (
                                    "failed" if delivery.error_type in ("permanent", "config") else "retry_wait"
                                ),
                                attempts=delivery.attempts,
                                api_status_code=delivery.status_code,
                                api_response=delivery.external_message_id or delivery.delivery_method,
                                error_type=delivery.error_type,
                                error_message=err_detail,
                                filter_ms=base_aud.filter_ms if base_aud else 0.0,
                                affiliate_ms=base_aud.affiliate_ms if base_aud else 0.0,
                                dispatch_ms=item_disp_ms,
                                created_at=now_sent_iso,
                                updated_at=now_sent_iso,
                            )
                            audit_records.pop((dedup_key, "__pipeline__"), None)

                            if delivery.success:
                                sent_count += 1
                            else:
                                failed_send_count += 1
                                last_send_error = err_detail

                        # Persiste imediatamente após cada produto para evitar perda/duplicação em crash/reinício
                        if item_records:
                            storage_service.record_sent_offers_batch(item_records)

                    if (
                        idx < len(selected_offers) - 1
                        and cycle.product_delay_minutes > 0
                        and destinations
                    ):
                        delay_sec = cycle.product_delay_minutes * 60.0
                        logger.info(
                            "Ciclo '%s' | Aguardando %.2f min (%.1fs) antes de enviar o próximo produto (%d/%d)...",
                            cycle.name,
                            cycle.product_delay_minutes,
                            delay_sec,
                            idx + 1,
                            len(selected_offers),
                        )
                        await asyncio.sleep(delay_sec)

                disp_ms_acc += (time.perf_counter() - t_disp_start) * 1000.0

                run_log.pending_count = remaining_queued
                run_log.sent_count = sent_count
                run_log.failed_send_count = failed_send_count
                run_log.finished_at = _now_iso()
                run_log.duration_seconds = round(time.perf_counter() - t_run_start, 2)
                run_log.navigation_ms = round(nav_ms_acc, 1)
                run_log.extraction_ms = round(ext_ms_acc, 1)
                run_log.processing_ms = round(proc_ms_acc, 1)
                run_log.affiliate_ms = round(aff_ms_acc, 1)
                run_log.dispatch_ms = round(disp_ms_acc, 1)
                run_log.bottleneck_stage = self.identify_bottleneck_stage(
                    run_log.navigation_ms,
                    run_log.extraction_ms,
                    run_log.processing_ms,
                    run_log.affiliate_ms,
                    run_log.dispatch_ms,
                )

                combined_kw_warn = " | ".join(kw_warnings) if kw_warnings else None
                run_log.error_message = last_send_error or (combined_kw_warn if sent_count == 0 else None)
                run_log.status = (
                    "completed"
                    if failed_send_count == 0
                    else ("partial" if sent_count > 0 else "failed")
                )
                storage_service.record_product_audit_batch(list(audit_records.values()))
                storage_service.save_cycle_run(run_log)

                refreshed = storage_service.get_cycle(cycle.id)
                current_state = refreshed.state if refreshed else cycle.state
                next_slot_dt = self.compute_next_scheduled_slot(
                    scheduled_dt=scheduled_dt,
                    interval_minutes=cycle.interval_minutes,
                )
                next_run = next_slot_dt.isoformat() if current_state == "running" else None
                storage_service.update_cycle_state(
                    cycle.id,
                    current_state,
                    last_run_at=run_log.finished_at,
                    next_run_at=next_run,
                    increment_runs=True,
                    offers_sent_delta=sent_count,
                    last_error=run_log.error_message,
                    clear_error=(run_log.error_message is None),
                    last_page_offset=current_page,
                    last_duration_seconds=run_log.duration_seconds,
                    last_schedule_delay_seconds=run_log.schedule_delay_seconds,
                    last_bottleneck=run_log.bottleneck_stage,
                )

                logger.info(
                    "Ciclo '%s' concluído em %.2fs (atraso=%.2fs, gargalo=%s) | "
                    "nav=%.0fms extr=%.0fms proc=%.0fms afil=%.0fms envio=%.0fms | "
                    "buscados=%d filtrados=%d fila=%d enviados=%d",
                    cycle.name,
                    run_log.duration_seconds,
                    run_log.schedule_delay_seconds,
                    run_log.bottleneck_stage,
                    run_log.navigation_ms,
                    run_log.extraction_ms,
                    run_log.processing_ms,
                    run_log.affiliate_ms,
                    run_log.dispatch_ms,
                    run_log.searched_count,
                    run_log.filtered_count,
                    run_log.deduplicated_new_count,
                    run_log.sent_count,
                )
                return True, run_log, "Execução concluída com sucesso."

        except Exception as exc:
            err_str = str(exc)
            logger.error("Erro crítico na execução do ciclo '%s': %s", cycle.name, err_str, exc_info=True)
            run_log.status = "failed"
            run_log.error_message = err_str
            run_log.finished_at = _now_iso()
            run_log.duration_seconds = round(time.perf_counter() - t_run_start, 2)
            run_log.navigation_ms = round(nav_ms_acc, 1)
            run_log.extraction_ms = round(ext_ms_acc, 1)
            run_log.processing_ms = round(proc_ms_acc, 1)
            run_log.affiliate_ms = round(aff_ms_acc, 1)
            run_log.dispatch_ms = round(disp_ms_acc, 1)
            run_log.bottleneck_stage = self.identify_bottleneck_stage(
                run_log.navigation_ms,
                run_log.extraction_ms,
                run_log.processing_ms,
                run_log.affiliate_ms,
                run_log.dispatch_ms,
            )
            storage_service.save_cycle_run(run_log)
            # Preserva o estado 'running' em falhas transitórias para que o loop continue e recupere automaticamente
            refreshed_err = storage_service.get_cycle(cycle.id)
            cur_state_err = refreshed_err.state if refreshed_err else cycle.state
            is_config_fatal = isinstance(exc, (KeyError, ValueError)) or "não está ativa" in err_str
            target_state = "error" if (is_config_fatal or cur_state_err != "running") else "running"
            next_run_err = (
                self.compute_next_scheduled_slot(
                    scheduled_dt=scheduled_dt,
                    interval_minutes=cycle.interval_minutes,
                ).isoformat()
                if target_state == "running"
                else None
            )
            storage_service.update_cycle_state(
                cycle.id,
                target_state,
                last_run_at=run_log.finished_at,
                next_run_at=next_run_err,
                increment_runs=True,
                last_error=err_str,
                last_duration_seconds=run_log.duration_seconds,
                last_schedule_delay_seconds=run_log.schedule_delay_seconds,
                last_bottleneck=run_log.bottleneck_stage,
            )
            return False, run_log, err_str
        finally:
            self._running_locks.discard(cycle_id)

    # -----------------------------------------------------------------
    # Agendamento Recorrente em Cadência Fixa (Start-to-Start)
    # -----------------------------------------------------------------

    async def _cycle_loop(self, cycle_id: str, run_immediately: bool = True) -> None:
        """Loop assíncrono de execução periódica ancorado no horário de início (start-to-start)."""
        cycle = storage_service.get_cycle(cycle_id)
        if not cycle:
            return

        scheduled_dt = _now_dt() if run_immediately else (_now_dt() + timedelta(minutes=cycle.interval_minutes))

        if run_immediately:
            await self.execute_cycle_once(cycle_id, scheduled_at=scheduled_dt.isoformat())

        while True:
            cycle = storage_service.get_cycle(cycle_id)
            if not cycle or cycle.state != "running":
                break

            scheduled_dt = self.compute_next_scheduled_slot(
                scheduled_dt=scheduled_dt,
                interval_minutes=cycle.interval_minutes,
            )
            next_run_iso = scheduled_dt.isoformat()
            storage_service.update_cycle_state(cycle_id, "running", next_run_at=next_run_iso)

            wait_seconds = max(0.0, (scheduled_dt - _now_dt()).total_seconds())
            try:
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
            except asyncio.CancelledError:
                break

            cycle = storage_service.get_cycle(cycle_id)
            if not cycle or cycle.state != "running":
                break

            await self.execute_cycle_once(cycle_id, scheduled_at=next_run_iso)

    def _cancel_loop_task(self, cycle_id: str) -> None:
        task = self._scheduled_tasks.pop(cycle_id, None)
        if task and not task.done():
            task.cancel()

    async def start_cycle(self, cycle_id: str, run_immediately: bool = True) -> CycleModel | None:
        """Inicia o ciclo e agenda suas execuções recorrentes."""
        cycle = storage_service.get_cycle(cycle_id)
        if not cycle:
            return None

        self._cancel_loop_task(cycle_id)
        next_run = _now_iso() if run_immediately else _future_iso(cycle.interval_minutes)
        updated = storage_service.update_cycle_state(
            cycle_id,
            "running",
            next_run_at=next_run,
            clear_error=True,
        )
        loop = asyncio.get_running_loop()
        self._scheduled_tasks[cycle_id] = loop.create_task(
            self._cycle_loop(cycle_id, run_immediately=run_immediately)
        )
        logger.info("Ciclo '%s' (%s) iniciado.", cycle.name, cycle_id)
        return self.enrich_cycle_runtime_info(updated) if updated else None

    async def pause_cycle(self, cycle_id: str) -> CycleModel | None:
        """Pausa o agendamento automático do ciclo."""
        self._cancel_loop_task(cycle_id)
        updated = storage_service.update_cycle_state(cycle_id, "paused", next_run_at=None)
        if updated:
            logger.info("Ciclo '%s' (%s) pausado.", updated.name, cycle_id)
            return self.enrich_cycle_runtime_info(updated)
        return None

    async def resume_cycle(self, cycle_id: str) -> CycleModel | None:
        """Retoma um ciclo pausado."""
        cycle = storage_service.get_cycle(cycle_id)
        if not cycle:
            return None
        logger.info("Retomando ciclo '%s' (%s).", cycle.name, cycle_id)
        return await self.start_cycle(cycle_id, run_immediately=True)

    async def stop_cycle(self, cycle_id: str) -> CycleModel | None:
        """Encerra o ciclo definitivamente até novo comando de início."""
        self._cancel_loop_task(cycle_id)
        updated = storage_service.update_cycle_state(cycle_id, "stopped", next_run_at=None)
        if updated:
            logger.info("Ciclo '%s' (%s) encerrado.", updated.name, cycle_id)
            return self.enrich_cycle_runtime_info(updated)
        return None

    async def recover_running_cycles_on_startup(self) -> None:
        """Restaura o agendamento dos ciclos que estavam com status 'running' antes de reiniciar."""
        for cid in list(self._scheduled_tasks.keys()):
            self._cancel_loop_task(cid)
        self._running_locks.clear()

        cycles = storage_service.list_cycles()
        recovered = 0
        now_dt = _now_dt()
        pending_queue_cycles = {
            q.cycle_id
            for q in storage_service.list_offer_queue(limit=500)
            if q.status in ("pending", "retry_wait", "processing")
        }
        for c in cycles:
            if c.state == "running":
                next_dt = _parse_iso_safe(c.next_run_at)
                overdue = next_dt is not None and next_dt <= now_dt
                has_pending = c.id in pending_queue_cycles
                await self.start_cycle(c.id, run_immediately=(overdue or has_pending))
                recovered += 1
        if recovered:
            logger.info("Recuperados %d ciclos ativos após inicialização do servidor.", recovered)

    async def shutdown(self) -> None:
        """Cancela tarefas de agendamento em background durante o desligamento do servidor."""
        for cid in list(self._scheduled_tasks.keys()):
            self._cancel_loop_task(cid)


cycle_scheduler = CycleScheduler()
