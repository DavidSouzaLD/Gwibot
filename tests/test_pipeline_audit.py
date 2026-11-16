"""Testes intensivos de auditoria, confiabilidade, recuperação e estresse do pipeline de ofertas."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.cycles import CycleScheduler
from app.main import app
from app.models import (
    AffiliateConfigModel,
    CycleCreateRequest,
    CycleFilters,
    OutputDestinationConfig,
    ProductItem,
    SearchMetadata,
    SearchResponse,
)
from app.outputs import TelegramOutputSource, _truncate_html_safe, output_registry
from app.sources import SearchSource, source_registry
from app.storage import StorageService, storage_service


def _make_product(idx: int, *, image_url: str | None = None, promo: float = 199.9, discount: int = 20, rating: float = 4.7) -> ProductItem:
    return ProductItem(
        id=f"MLB90000{idx:03d}",
        name=f"Produto Auditoria {idx} " + ("X" * 30),
        url=f"https://www.mercadolivre.com.br/p/MLB90000{idx:03d}",
        original_price=round(promo * 1.25, 2),
        promotional_price=promo,
        discount_percentage=discount,
        rating=rating,
        reviews_count=50 + idx,
        seller="Loja Teste",
        image_url=image_url,
    )


def test_html_truncation_never_breaks_tags() -> None:
    """Garante que truncamento de legenda HTML > 1024 chars nunca deixa tags abertas ou cortadas."""
    long_title = "Notebook <b>Ultra</b> " + ("A" * 1200)
    raw_html = (
        f"📦 <b>{long_title}</b>\n\n"
        f"✅ <b>Por: R$ 1.999,90</b>\n"
        f'<a href="https://www.mercadolivre.com.br/p/MLB123?matt_tool=1&amp;matt_word=gw">Comprar</a>'
    )
    truncated = _truncate_html_safe(raw_html, 1024)
    assert len(truncated) <= 1024
    assert truncated.count("<b>") == truncated.count("</b>")
    assert truncated.count("<a ") == truncated.count("</a>")
    assert not truncated.endswith("<")


def test_telegram_error_handling_image_probe_and_fallback() -> None:
    """Simula HTTP 400, 403, 429, 500, timeouts e imagens inválidas no TelegramOutputSource."""
    async def _run() -> None:
        # 1. Imagem inacessível (403) + content-type inválido -> pula sendPhoto e vai direto para sendMessage
        tg_calls: list[str] = []

        def img_handler(request: httpx.Request) -> httpx.Response:
            if "forbidden" in str(request.url):
                return httpx.Response(403)
            if "htmlpage" in str(request.url):
                return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"})
            if "toobig" in str(request.url):
                return httpx.Response(200, headers={"content-type": "image/jpeg", "content-length": str(8 * 1024 * 1024)})
            if "timeout" in str(request.url):
                raise httpx.ReadTimeout("image timeout", request=request)
            return httpx.Response(200, headers={"content-type": "image/jpeg", "content-length": "12345"})

        def tg_ok_handler(request: httpx.Request) -> httpx.Response:
            method = request.url.path.split("/")[-1]
            tg_calls.append(method)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(tg_calls)}})

        tg = TelegramOutputSource(
            transport=httpx.MockTransport(tg_ok_handler),
            image_transport=httpx.MockTransport(img_handler),
            probe_images=True,
        )
        dest = OutputDestinationConfig(
            id="dest_tg_1",
            name="Canal Teste",
            output_type="telegram",
            enabled=True,
            config={"bot_token": "123456:TESTTOKEN", "chat_id": "-100123456"},
        )

        for bad_img in [
            "https://cdn.example.com/forbidden.jpg",
            "https://cdn.example.com/htmlpage.jpg",
            "https://cdn.example.com/toobig.jpg",
            "https://cdn.example.com/timeout.jpg",
            "not-a-valid-url",
        ]:
            tg_calls.clear()
            prod = _make_product(1, image_url=bad_img)
            prod.affiliate_url = "https://www.mercadolivre.com.br/p/MLB90000001?matt_tool=1&matt_word=w"
            res = await tg.send_offer(prod, dest)
            assert res.success is True
            assert res.delivery_method == "sendMessage"
            assert res.fallback_used is True
            assert tg_calls == ["sendMessage"]

        # 2. Simula 500 transitório seguido de 200 OK
        attempt_counter = {"n": 0}

        def tg_500_then_ok(request: httpx.Request) -> httpx.Response:
            attempt_counter["n"] += 1
            if attempt_counter["n"] == 1:
                return httpx.Response(500, json={"ok": False, "description": "Internal Server Error"})
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 777}})

        tg_retry = TelegramOutputSource(
            transport=httpx.MockTransport(tg_500_then_ok),
            probe_images=False,
        )
        prod_ok = _make_product(2, image_url=None)
        prod_ok.affiliate_url = "https://www.mercadolivre.com.br/p/MLB90000002?matt_tool=1&matt_word=w"
        res_500 = await tg_retry.send_offer(prod_ok, dest)
        assert res_500.success is True
        assert res_500.attempts == 2

        # 3. Simula 429 com retry_after curto (inline) vs longo (deferred para fila)
        rl_counter = {"n": 0}

        def tg_429_short(request: httpx.Request) -> httpx.Response:
            rl_counter["n"] += 1
            if rl_counter["n"] == 1:
                return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 1}, "description": "Too Many Requests"})
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 888}})

        tg_rl_short = TelegramOutputSource(
            transport=httpx.MockTransport(tg_429_short),
            probe_images=False,
            max_inline_retry_after=5.0,
        )
        res_rl = await tg_rl_short.send_offer(prod_ok, dest)
        assert res_rl.success is True
        assert res_rl.attempts == 2

        def tg_429_long(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 60}, "description": "Too Many Requests: retry after 60"})

        tg_rl_long = TelegramOutputSource(
            transport=httpx.MockTransport(tg_429_long),
            probe_images=False,
            max_inline_retry_after=5.0,
        )
        res_rl_long = await tg_rl_long.send_offer(prod_ok, dest)
        assert res_rl_long.success is False
        assert res_rl_long.error_type == "rate_limit"
        assert res_rl_long.status_code == 429
        assert res_rl_long.retry_after_seconds == 60.0

        # 4. Simula 403 permanente (bot was blocked) -> não tenta fallback para sendMessage
        perm_calls: list[str] = []

        def tg_403_perm(request: httpx.Request) -> httpx.Response:
            perm_calls.append(request.url.path.split("/")[-1])
            return httpx.Response(403, json={"ok": False, "description": "Forbidden: bot was blocked by the user"})

        tg_perm = TelegramOutputSource(
            transport=httpx.MockTransport(tg_403_perm),
            probe_images=False,
        )
        prod_img = _make_product(3, image_url="https://http2.mlstatic.com/D_NQ_NP_123.jpg")
        prod_img.affiliate_url = "https://www.mercadolivre.com.br/p/MLB90000003?matt_tool=1&matt_word=w"
        res_perm = await tg_perm.send_offer(prod_img, dest)
        assert res_perm.success is False
        assert res_perm.error_type == "permanent"
        assert res_perm.status_code == 403
        assert perm_calls == ["sendPhoto"]  # Não desperdiçou chamada em sendMessage

        # 5. Simula 400 "can't parse entities" -> fallback para texto puro
        html_calls: list[dict[str, Any]] = []

        def tg_400_entities(request: httpx.Request) -> httpx.Response:
            import json
            body = json.loads(request.content.decode("utf-8"))
            html_calls.append(body)
            if body.get("parse_mode") == "HTML":
                return httpx.Response(400, json={"ok": False, "description": "Bad Request: can't parse entities"})
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 999}})

        tg_entities = TelegramOutputSource(
            transport=httpx.MockTransport(tg_400_entities),
            probe_images=False,
        )
        res_ent = await tg_entities.send_offer(prod_ok, dest)
        assert res_ent.success is True
        assert res_ent.fallback_used is True
        assert "parse_mode" not in html_calls[-1]

    asyncio.run(_run())


def test_multicycle_stress_recovery_and_restart_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Simula 6 ciclos consecutivos com falhas temporárias, definitivas, filtros, multi-destino e reinício."""
    isolated_db = tmp_path / "stress_audit.db"
    test_storage = StorageService(db_path=isolated_db)

    # Injeta storage isolado nos módulos
    import app.cycles as cycles_mod
    import app.outputs as outputs_mod
    import app.affiliates as affiliates_mod
    import app.main as main_mod

    monkeypatch.setattr(cycles_mod, "storage_service", test_storage)
    monkeypatch.setattr(outputs_mod, "storage_service", test_storage)
    monkeypatch.setattr(affiliates_mod, "storage_service", test_storage)
    monkeypatch.setattr(main_mod, "storage_service", test_storage)

    test_storage.save_affiliate_config(
        AffiliateConfigModel(
            provider_id="mercadolivre",
            enabled=True,
            matt_word="auditword",
            matt_tool="998877",
        )
    )

    dest1 = test_storage.save_destination(
        OutputDestinationConfig(
            id="dest_1",
            name="Canal Principal",
            output_type="telegram",
            enabled=True,
            config={"bot_token": "111111:TOKEN1", "chat_id": "-100111"},
        )
    )
    dest2 = test_storage.save_destination(
        OutputDestinationConfig(
            id="dest_2",
            name="Canal Secundário",
            output_type="telegram",
            enabled=True,
            config={"bot_token": "222222:TOKEN2", "chat_id": "-100222"},
        )
    )

    # Mock source com produtos variados (elegíveis, sem imagem, preço baixo descartado, duplicados)
    cycle_call = {"n": 0}

    class StressMockSource(SearchSource):
        source_id = "mercadolivre"
        display_name = "Mock ML Stress"
        supported = True

        async def search(self, query: str, limit: int, page_number: int = 1, enrich_affiliates: bool = True) -> SearchResponse:
            cycle_call["n"] += 1
            base = (page_number - 1) * 5
            items = [
                _make_product(base + 1, image_url="https://http2.mlstatic.com/ok1.jpg", promo=250.0, discount=25),
                _make_product(base + 2, image_url=None, promo=300.0, discount=30),
                _make_product(base + 3, image_url="https://http2.mlstatic.com/ok3.jpg", promo=50.0, discount=10),  # Descartado pelo filtro min_price=100
                _make_product(base + 4, image_url="https://http2.mlstatic.com/ok4.jpg", promo=400.0, discount=35),
            ]
            test_storage.upsert_catalog_products(items, source_id="mercadolivre", query_term=query)
            return SearchResponse(
                status="ok",
                metadata=SearchMetadata(query=query, requested_limit=limit, total_returned=len(items), pages_visited=1, elapsed_seconds=0.05),
                results=items,
            )

    orig_ml_source = source_registry.get("mercadolivre")
    source_registry.register(StressMockSource())

    # Mock Telegram:
    # - dest_2 falha temporariamente (500) na primeira tentativa do produto 1, depois funciona
    # - dest_1 funciona sempre, sem duplicar
    sent_log: list[tuple[str, str]] = []
    transient_state = {"fail_dest2_once": True}

    def stress_tg_handler(request: httpx.Request) -> httpx.Response:
        token = request.url.path.split("/")[1]
        if "222222:TOKEN2" in token and transient_state["fail_dest2_once"]:
            transient_state["fail_dest2_once"] = False
            return httpx.Response(500, json={"ok": False, "description": "Temporary 500"})
        import json
        body = json.loads(request.content.decode("utf-8"))
        chat_id = str(body.get("chat_id"))
        text = str(body.get("caption") or body.get("text") or "")
        sent_log.append((chat_id, text))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent_log)}})

    orig_tg = output_registry.get("telegram")
    output_registry.register(
        TelegramOutputSource(
            transport=httpx.MockTransport(stress_tg_handler),
            probe_images=False,
        )
    )

    try:
        scheduler = CycleScheduler()
        cycle = test_storage.create_cycle(
            CycleCreateRequest(
                name="Ciclo Estresse Auditoria",
                query="notebook",
                source_id="mercadolivre",
                search_limit=10,
                max_offers_per_run=2,
                interval_minutes=5,
                destination_ids=[dest1.id or "", dest2.id or ""],
                filters=CycleFilters(min_price=100.0, min_discount=15),
                deduplicate=True,
            )
        )

        async def _run_stress() -> None:
            # Executa 6 ciclos consecutivos
            for i in range(6):
                # Simula expiração do retry_wait entre ciclos para validar recuperação automática
                with test_storage._connect() as conn:
                    conn.execute(
                        "UPDATE offer_queue SET next_retry_at = ? WHERE status = 'retry_wait'",
                        ((datetime.now().astimezone() - timedelta(seconds=5)).isoformat(),),
                    )
                ok, run_log, msg = await scheduler.execute_cycle_once(cycle.id)
                assert ok is True, f"Ciclo {i+1} falhou: {msg}"
                assert run_log is not None

            # Simula crash/reinício com item preso em 'processing' no SQLite
            with test_storage._connect() as conn:
                conn.execute(
                    "UPDATE offer_queue SET status = 'processing' WHERE status = 'pending'"
                )
            restarted_storage = StorageService(db_path=isolated_db)
            q_after_restart = restarted_storage.list_offer_queue(limit=100)
            assert not any(q.status == "processing" for q in q_after_restart)

        asyncio.run(_run_stress())

        # Verifica ausência de envios duplicados por (chat_id, texto)
        assert len(sent_log) == len(set(sent_log)), "Detectado envio duplicado para o mesmo destino!"
        assert len(sent_log) >= 6

        # Verifica observabilidade individual e métricas via API
        metrics = test_storage.get_pipeline_metrics(cycle.id)
        assert metrics["found"] > 0
        assert metrics["eligible"] > 0
        assert metrics["discarded"] > 0
        assert metrics["sent"] > 0

        audits = test_storage.list_product_audits(cycle_id=cycle.id, limit=200)
        assert len(audits) > 0
        discarded_audits = [a for a in audits if not a.filter_passed]
        assert any("abaixo do mínimo" in (a.filter_reason or "") for a in discarded_audits)

        with TestClient(app) as client:
            r_m = client.get(f"/api/observability/metrics?cycle_id={cycle.id}")
            assert r_m.status_code == 200
            assert r_m.json()["sent"] == metrics["sent"]

            r_p = client.get(f"/api/observability/products?cycle_id={cycle.id}")
            assert r_p.status_code == 200
            assert len(r_p.json()) > 0
    finally:
        source_registry.register(orig_ml_source)
        output_registry.register(orig_tg)
