"""Suíte de testes automatizados para FastAPI, parser, Playwright, afiliados, Telegram e Ciclos."""
from __future__ import annotations

import json
import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.models import AffiliateConfigModel
from app.outputs import TelegramOutputSource, output_registry
from app.parser import build_product_item
from app.scraper import EXTRACT_CARDS_JS, MercadoLivreScraper, scraper_service
from app.storage import storage_service

client = TestClient(app)


def test_health_and_sources_endpoints() -> None:
    """Garante que GET /api/health e GET /api/sources respondem 200 com a arquitetura registrada."""
    response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "healthy"
    assert payload["playwright_ready"] is True
    assert payload["browser_channel"] == "chromium"
    assert "active_cycles" in payload

    sources_res = client.get("/api/sources")
    assert sources_res.status_code == 200
    sources_data = sources_res.json()
    source_ids = [s["source_id"] for s in sources_data["search_sources"]]
    assert "mercadolivre" in source_ids
    assert "shopee" in source_ids
    assert "amazon" in source_ids

    out_types = [o["output_type"] for o in sources_data["output_types"]]
    assert "telegram" in out_types
    assert "whatsapp" in out_types


def test_index_web_ui() -> None:
    """Garante que a interface web local é servida em GET / com as novas seções."""
    response = client.get("/")
    assert response.status_code == 200
    assert "Gwibot Affiliate Hub" in response.text
    assert "Ciclos Automatizados" in response.text
    assert "/static/app.js" in response.text


def test_search_validation_errors() -> None:
    """Garante tratamento de parâmetros inválidos em GET /api/search."""
    res_no_query = client.get("/api/search")
    assert res_no_query.status_code == 422

    res_blank_query = client.get("/api/search?query=%20%20&limit=10")
    assert res_blank_query.status_code == 400

    res_bad_limit = client.get("/api/search?query=mochila&limit=999")
    assert res_bad_limit.status_code == 422


def test_parser_complete_and_null_fields() -> None:
    """Testa extração completa e preservação de `None` (`null` no JSON) quando indisponível."""
    raw_complete = {
        "id": "MLB-3948571234",
        "name": "Mochila Executiva Impermeável Notebook 15.6",
        "url": "https://produto.mercadolivre.com.br/MLB-3948571234-mochila-executiva-_JM#position=1",
        "promo_fraction": "149",
        "promo_cents": "90",
        "orig_fraction": "199",
        "orig_cents": "90",
        "discount_text": "25% OFF",
        "rating_text": "4.8",
        "reviews_text": "(1.420)",
        "seller_text": "Por Loja Oficial",
        "image_url": "https://http2.mlstatic.com/D_Q_NP_2X_12345-MLB.webp",
    }
    item = build_product_item(raw_complete)
    assert item is not None
    dumped = item.model_dump()
    assert dumped["id"] == "MLB3948571234"
    assert dumped["name"] == "Mochila Executiva Impermeável Notebook 15.6"
    assert dumped["url"] == "https://produto.mercadolivre.com.br/MLB-3948571234-mochila-executiva-_JM"
    assert dumped["original_price"] == 199.90
    assert dumped["promotional_price"] == 149.90
    assert dumped["discount_percentage"] == 25
    assert dumped["rating"] == 4.8
    assert dumped["reviews_count"] == 1420
    assert dumped["seller"] == "Loja Oficial"
    assert dumped["image_url"] == "https://http2.mlstatic.com/D_Q_NP_2X_12345-MLB.webp"
    assert dumped["affiliate_url"] is None

    # Produto parcial (sem desconto, sem avaliação, sem vendedor, sem ID explícito)
    raw_partial = {
        "id": None,
        "raw_html_id": "",
        "name": "Mochila Escolar Simples",
        "url": "https://www.mercadolivre.com.br/mochila-escolar-simples",
        "promo_fraction": "59",
        "promo_cents": None,
        "orig_fraction": None,
        "orig_cents": None,
        "discount_text": None,
        "rating_text": None,
        "reviews_text": None,
        "seller_text": None,
        "image_url": "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7",
    }
    partial_item = build_product_item(raw_partial)
    assert partial_item is not None
    partial_dump = partial_item.model_dump()
    assert partial_dump["id"] is None
    assert partial_dump["promotional_price"] == 59.0
    assert partial_dump["original_price"] is None
    assert partial_dump["discount_percentage"] is None
    assert partial_dump["rating"] is None
    assert partial_dump["reviews_count"] is None
    assert partial_dump["seller"] is None
    assert partial_dump["image_url"] is None


def test_playwright_dom_extraction_in_isolated_chromium() -> None:
    """Executa o Chromium real via Playwright em HTML idêntico ao do Mercado Livre."""
    from playwright.sync_api import sync_playwright

    sample_html = """
    <!DOCTYPE html>
    <html>
    <body>
      <ol class="ui-search-layout">
        <li class="ui-search-layout__item">
          <div class="poly-card">
            <img class="poly-component__picture" src="https://http2.mlstatic.com/D_NQ_NP_MLB11223344.webp" />
            <a class="poly-component__title" href="https://www.mercadolivre.com.br/mochila-teste/p/MLB987654321">Mochila Reforçada 40L</a>
            <span class="poly-component__seller">Por BagStore Oficial</span>
            <div class="poly-component__price">
              <s class="andes-money-amount--previous">
                <span class="andes-money-amount__fraction">250</span>
                <span class="andes-money-amount__cents">00</span>
              </s>
              <div class="poly-price__current">
                <span class="andes-money-amount">
                  <span class="andes-money-amount__fraction">199</span>
                  <span class="andes-money-amount__cents">90</span>
                </span>
                <span class="andes-money-amount__discount">20% OFF</span>
              </div>
            </div>
            <div class="poly-component__reviews">
              <span class="poly-reviews__rating">4.9</span>
              <span class="poly-reviews__total">(312)</span>
            </div>
          </div>
        </li>
      </ol>
    </body>
    </html>
    """

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.set_content(sample_html)
        raw_cards = page.evaluate(EXTRACT_CARDS_JS)
        context.close()
        browser.close()

    assert len(raw_cards) == 1
    item = build_product_item(raw_cards[0])
    assert item is not None
    assert item.id == "MLB987654321"
    assert item.name == "Mochila Reforçada 40L"
    assert item.original_price == 250.0
    assert item.promotional_price == 199.90
    assert item.discount_percentage == 20
    assert item.rating == 4.9
    assert item.reviews_count == 312
    assert item.seller == "BagStore Oficial"
    assert item.image_url == "https://http2.mlstatic.com/D_NQ_NP_MLB11223344.webp"


def test_affiliate_link_generation_and_failure_detection() -> None:
    """Valida geração oficial de links de afiliado do Mercado Livre e detecção de falhas."""
    # 1. Configura credenciais oficiais do afiliado
    cfg_res = client.put(
        "/api/affiliates/config",
        json={
            "provider_id": "mercadolivre",
            "enabled": True,
            "matt_word": "canalofertas",
            "matt_tool": "88776655",
            "use_authenticated_linkbuilder": False,
        },
    )
    assert cfg_res.status_code == 200
    assert cfg_res.json()["matt_word"] == "canalofertas"

    # 2. Converte URL válida do Mercado Livre
    conv_ok = client.post(
        "/api/affiliates/convert",
        json={
            "url": "https://produto.mercadolivre.com.br/MLB-3948571234-mochila-executiva-_JM",
            "product_id": "MLB3948571234",
        },
    )
    assert conv_ok.status_code == 200
    data_ok = conv_ok.json()
    assert data_ok["status"] == "generated"
    assert "matt_tool=88776655" in data_ok["affiliate_url"]
    assert "matt_word=canalofertas" in data_ok["affiliate_url"]

    # 3. Rejeita URL inválida/externa sem simular link falso
    conv_fail = client.post(
        "/api/affiliates/convert",
        json={"url": "https://www.exemplo-externo.com.br/produto/123"},
    )
    assert conv_fail.status_code == 200
    data_fail = conv_fail.json()
    assert data_fail["status"] == "failed"
    assert data_fail["affiliate_url"] is None
    assert data_fail["error"] is not None


def test_full_pipeline_mercadolivre_to_affiliate_to_telegram() -> None:
    """Testa o fluxo completo: Cycle -> Search (ML) -> Filter -> Affiliate Link -> Deduplication -> Telegram Output."""
    from playwright.sync_api import Route

    storage_service.clear_history()

    # 1. Configura Afiliado ML
    storage_service.save_affiliate_config(
        AffiliateConfigModel(
            provider_id="mercadolivre",
            enabled=True,
            matt_word="gwibot_afiliado",
            matt_tool="99887766",
            use_authenticated_linkbuilder=False,
        )
    )

    # 2. Mock para capturar os disparos feitos pelo TelegramOutputSource
    telegram_dispatches: list[dict] = []

    async def mock_telegram_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        telegram_dispatches.append(
            {
                "url": str(request.url),
                "method": str(request.url).rsplit("/", 1)[-1],
                "payload": body,
            }
        )
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 1000 + len(telegram_dispatches)}},
        )

    output_registry.register(
        TelegramOutputSource(transport=httpx.MockTransport(mock_telegram_handler))
    )

    # 3. Cadastra destino Telegram via API
    dest_res = client.post(
        "/api/outputs",
        json={
            "name": "Canal Telegram Principal",
            "output_type": "telegram",
            "enabled": True,
            "config": {
                "bot_token": "123456789:AAH_TEST_TOKEN_GWIBOT",
                "chat_id": "-100987654321",
            },
        },
    )
    assert dest_res.status_code == 200
    dest_id = dest_res.json()["id"]

    # 4. Cria um Ciclo com filtros (min_discount=20%, min_rating=4.5, max_offers_per_run=2)
    cycle_res = client.post(
        "/api/cycles",
        json={
            "name": "Campanha Mochilas Premium",
            "query": "mochila notebook",
            "source_id": "mercadolivre",
            "search_limit": 5,
            "max_offers_per_run": 2,
            "interval_minutes": 60,
            "destination_ids": [dest_id],
            "deduplicate": True,
            "filters": {
                "min_price": 100.0,
                "max_price": 300.0,
                "min_discount": 20,
                "min_rating": 4.5,
                "require_affiliate_link": True,
            },
        },
    )
    assert cycle_res.status_code == 200
    cycle_id = cycle_res.json()["id"]

    # 5. Prepara HTML simulando 4 produtos no Mercado Livre (3 aprovados nos filtros, 1 reprovado por desconto baixo)
    ml_search_html = """
    <!DOCTYPE html><html><body>
      <ol class="ui-search-layout">
        <li class="ui-search-layout__item">
          <div class="poly-card">
            <img class="poly-component__picture" src="https://http2.mlstatic.com/D_NQ_NP_MLB500001.webp" />
            <a class="poly-component__title" href="https://www.mercadolivre.com.br/mochila-pro-1/p/MLB500001">Mochila Pro 1</a>
            <span class="poly-component__seller">Por Loja A</span>
            <div class="poly-component__price">
              <s class="andes-money-amount--previous"><span class="andes-money-amount__fraction">250</span></s>
              <div class="poly-price__current">
                <span class="andes-money-amount"><span class="andes-money-amount__fraction">180</span></span>
                <span class="andes-money-amount__discount">28% OFF</span>
              </div>
            </div>
            <div class="poly-component__reviews">
              <span class="poly-reviews__rating">4.8</span>
              <span class="poly-reviews__total">(210)</span>
            </div>
          </div>
        </li>
        <li class="ui-search-layout__item">
          <div class="poly-card">
            <img class="poly-component__picture" src="https://http2.mlstatic.com/D_NQ_NP_MLB500002.webp" />
            <a class="poly-component__title" href="https://www.mercadolivre.com.br/mochila-pro-2/p/MLB500002">Mochila Pro 2</a>
            <span class="poly-component__seller">Por Loja B</span>
            <div class="poly-component__price">
              <s class="andes-money-amount--previous"><span class="andes-money-amount__fraction">300</span></s>
              <div class="poly-price__current">
                <span class="andes-money-amount"><span class="andes-money-amount__fraction">210</span></span>
                <span class="andes-money-amount__discount">30% OFF</span>
              </div>
            </div>
            <div class="poly-component__reviews">
              <span class="poly-reviews__rating">4.9</span>
              <span class="poly-reviews__total">(540)</span>
            </div>
          </div>
        </li>
        <li class="ui-search-layout__item">
          <div class="poly-card">
            <img class="poly-component__picture" src="https://http2.mlstatic.com/D_NQ_NP_MLB500003.webp" />
            <a class="poly-component__title" href="https://www.mercadolivre.com.br/mochila-pro-3/p/MLB500003">Mochila Pro 3</a>
            <div class="poly-component__price">
              <s class="andes-money-amount--previous"><span class="andes-money-amount__fraction">200</span></s>
              <div class="poly-price__current">
                <span class="andes-money-amount"><span class="andes-money-amount__fraction">150</span></span>
                <span class="andes-money-amount__discount">25% OFF</span>
              </div>
            </div>
            <div class="poly-component__reviews">
              <span class="poly-reviews__rating">4.7</span>
              <span class="poly-reviews__total">(95)</span>
            </div>
          </div>
        </li>
        <li class="ui-search-layout__item">
          <div class="poly-card">
            <a class="poly-component__title" href="https://www.mercadolivre.com.br/mochila-sem-desc/p/MLB500004">Mochila Sem Desconto</a>
            <div class="poly-component__price">
              <div class="poly-price__current">
                <span class="andes-money-amount"><span class="andes-money-amount__fraction">190</span></span>
                <span class="andes-money-amount__discount">5% OFF</span>
              </div>
            </div>
            <div class="poly-component__reviews">
              <span class="poly-reviews__rating">4.9</span>
            </div>
          </div>
        </li>
      </ol>
    </body></html>
    """

    def mock_ml_route(route: Route) -> None:
        route.fulfill(status=200, content_type="text/html", body=ml_search_html)

    scraper_service._route_handler = mock_ml_route
    try:
        # 6. Primeira execução do ciclo: busca 4 -> filtra 3 -> envia 2 (max_offers_per_run=2)
        run1_res = client.post(f"/api/cycles/{cycle_id}/run-now")
        assert run1_res.status_code == 200
        run1 = run1_res.json()
        assert run1["status"] == "completed"
        assert run1["searched_count"] == 4
        assert run1["filtered_count"] == 3
        assert run1["affiliate_ok_count"] == 3
        assert run1["deduplicated_new_count"] == 3
        assert run1["sent_count"] == 2

        assert len(telegram_dispatches) == 2
        first_tg = telegram_dispatches[0]
        assert first_tg["method"] == "sendPhoto"
        assert first_tg["payload"]["chat_id"] == "-100987654321"
        assert "Mochila Pro 1" in first_tg["payload"]["caption"]
        assert "28% OFF" in first_tg["payload"]["caption"]
        assert "matt_word=gwibot_afiliado" in first_tg["payload"]["caption"]
        assert "matt_tool=99887766" in first_tg["payload"]["caption"]

        # 7. Segunda execução do mesmo ciclo: deduplicação ignora os 2 já enviados e envia apenas o 3º inédito!
        run2_res = client.post(f"/api/cycles/{cycle_id}/run-now")
        assert run2_res.status_code == 200
        run2 = run2_res.json()
        assert run2["status"] == "completed"
        assert run2["deduplicated_new_count"] == 1
        assert run2["sent_count"] == 1
        assert len(telegram_dispatches) == 3
        assert "Mochila Pro 3" in telegram_dispatches[2]["payload"]["caption"]

        # 8. Verifica histórico de ofertas e execuções via API
        offers_hist = client.get(f"/api/history/offers?cycle_id={cycle_id}").json()
        assert len(offers_hist) == 3
        assert all("matt_word=gwibot_afiliado" in o["affiliate_url"] for o in offers_hist)

        # 9. Testa controles de estado do ciclo (pause / stop)
        pause_res = client.post(f"/api/cycles/{cycle_id}/pause")
        assert pause_res.status_code == 200
        assert pause_res.json()["state"] == "paused"

        stop_res = client.post(f"/api/cycles/{cycle_id}/stop")
        assert stop_res.status_code == 200
        assert stop_res.json()["state"] == "stopped"
    finally:
        scraper_service._route_handler = None


def test_session_persistence_logout_and_expiration_detection() -> None:
    """Valida persistência de sessão, logout e detecção automática de sessão expirada no scraper."""
    from playwright.sync_api import Route
    from app.session_manager import SessionManager, session_manager

    session_manager.logout()
    res_status = client.get("/api/auth/status")
    assert res_status.status_code == 200
    assert res_status.json()["authenticated"] is False

    sample_storage = {
        "cookies": [
            {
                "name": "ssid",
                "value": "dummy-session-token-not-logged",
                "domain": ".mercadolivre.com.br",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }
    session_manager._save_storage_state(sample_storage)

    reloaded_mgr = SessionManager(
        session_dir=session_manager._session_dir,
        state_file=session_manager._state_file,
        meta_file=session_manager._meta_file,
    )
    assert reloaded_mgr.get_status().authenticated is True

    def mock_expired_redirect(route: Route) -> None:
        if "account-verification" not in route.request.url:
            route.fulfill(
                status=200,
                content_type="text/html",
                body=(
                    '<!DOCTYPE html><html><head>'
                    '<script>window.location.href="https://www.mercadolivre.com.br/gz/account-verification?go=https://lista.mercadolivre.com.br/mochila";</script>'
                    '</head><body>Redirecting...</body></html>'
                ),
            )
        else:
            route.fulfill(
                status=200,
                content_type="text/html",
                body="<html><body>Account Verification Gate</body></html>",
            )

    scraper_service._route_handler = mock_expired_redirect
    try:
        search_res = client.get("/api/search?query=mochila&limit=5")
        assert search_res.status_code == 200
        payload = search_res.json()
        assert payload["status"] == "blocked"
        assert payload["metadata"]["authenticated_session_used"] is True
        assert payload["metadata"]["session_expired"] is True

        after_status = client.get("/api/auth/status").json()
        assert after_status["authenticated"] is False
        assert after_status["state"] == "expired"
    finally:
        scraper_service._route_handler = None

    res_logout = client.post("/api/auth/logout")
    assert res_logout.status_code == 200
    assert res_logout.json()["state"] == "logged_out"


def test_cycle_concurrency_and_telegram_retry_fallback() -> None:
    """Valida bloqueio de execução simultânea de ciclo e fallback sendPhoto -> sendMessage no Telegram."""
    import asyncio
    from app.cycles import cycle_scheduler
    from app.models import OutputDestinationConfig, ProductItem

    # 1. Testa fallback sendPhoto (erro 400) -> sendMessage (200 OK) no TelegramOutputSource
    calls: list[str] = []

    async def mock_tg_fallback(request: httpx.Request) -> httpx.Response:
        method = str(request.url).rsplit("/", 1)[-1]
        calls.append(method)
        if method == "sendPhoto":
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: wrong file identifier"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 777}})

    tg = TelegramOutputSource(transport=httpx.MockTransport(mock_tg_fallback))
    dest = OutputDestinationConfig(
        id="dest_fallback",
        name="Canal Fallback",
        output_type="telegram",
        enabled=True,
        config={"bot_token": "111:AAA", "chat_id": "@testchannel"},
    )
    prod = ProductItem(
        id="MLB777",
        name="Produto Teste Fallback",
        url="https://www.mercadolivre.com.br/p/MLB777",
        affiliate_url="https://www.mercadolivre.com.br/p/MLB777?matt_tool=1&matt_word=a",
        promotional_price=99.90,
        image_url="https://http2.mlstatic.com/invalid-photo.webp",
    )

    delivery = asyncio.run(tg.send_offer(prod, dest, cycle_name="Teste Fallback"))
    assert delivery.success is True
    assert calls == ["sendPhoto", "sendMessage"]
    assert delivery.external_message_id == "777"

    # 2. Testa bloqueio de execução simultânea do mesmo ciclo
    cycles = client.get("/api/cycles").json()
    assert len(cycles) > 0
    cid = cycles[0]["id"]

    cycle_scheduler._running_locks.add(cid)
    try:
        concurrent_res = client.post(f"/api/cycles/{cid}/run-now")
        assert concurrent_res.status_code == 409
        assert "simultânea" in concurrent_res.json()["detail"]
    finally:
        cycle_scheduler._running_locks.discard(cid)


def test_multi_keywords_cycle_edit_and_product_delay_validation() -> None:
    """Valida bugfix de múltiplas palavras-chave (incluindo termo sem resultado), edição de ciclo e validação de tempo entre produtos."""
    from playwright.sync_api import Route
    from app.parser import parse_keywords

    # 1. Normalização de palavras-chave (vírgulas, quebras de linha, espaços extras, vazios e duplicados)
    parsed = parse_keywords("  notebook,  mochilas \n\n  NOTEBOOK , , monitor gamer ;  ")
    assert parsed == ["notebook", "mochilas", "monitor gamer"]

    # 2. Validação de UI (remoção da numeração lateral, textarea #cycleQuery e campo #cycleProductDelay)
    ui_html = client.get("/").text
    assert "1. Pesquisa & Extração" not in ui_html
    assert "2. Ciclos Automatizados" not in ui_html
    assert "3. Afiliados & Destinos" not in ui_html
    assert "4. Histórico & Métricas" not in ui_html
    assert '<textarea' in ui_html
    assert 'id="cycleQuery"' in ui_html
    assert 'id="cycleProductDelay"' in ui_html

    # 3. Regra de validação: product_delay_minutes >= interval_minutes deve falhar (422)
    bad_delay_res = client.post(
        "/api/cycles",
        json={
            "name": "Ciclo Tempo Inválido",
            "query": "notebook, mochila",
            "source_id": "mercadolivre",
            "search_limit": 10,
            "max_offers_per_run": 3,
            "interval_minutes": 15,
            "product_delay_minutes": 15.0,
            "destination_ids": [],
            "deduplicate": True,
        },
    )
    assert bad_delay_res.status_code == 422

    # 4. Regra de validação: (max_offers_per_run * product_delay_minutes) > interval_minutes deve falhar (422)
    overflow_window_res = client.post(
        "/api/cycles",
        json={
            "name": "Ciclo Janela Estourada",
            "query": "notebook",
            "source_id": "mercadolivre",
            "search_limit": 10,
            "max_offers_per_run": 5,
            "interval_minutes": 20,
            "product_delay_minutes": 5.0,  # 5 * 5 = 25 > 20
            "destination_ids": [],
            "deduplicate": True,
        },
    )
    assert overflow_window_res.status_code == 422

    # 5. Cria destino Telegram e ciclo com múltiplas palavras-chave ("termo_inexistente_xyz, mochilas")
    storage_service.clear_history()
    storage_service.save_affiliate_config(
        AffiliateConfigModel(
            provider_id="mercadolivre",
            enabled=True,
            matt_word="gwibot_multi",
            matt_tool="11223344",
            use_authenticated_linkbuilder=False,
        )
    )

    tg_sent: list[dict] = []

    async def mock_tg(request: httpx.Request) -> httpx.Response:
        tg_sent.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 2000 + len(tg_sent)}})

    output_registry.register(TelegramOutputSource(transport=httpx.MockTransport(mock_tg)))

    dest_res = client.post(
        "/api/outputs",
        json={
            "name": "Telegram Multi-Keyword",
            "output_type": "telegram",
            "enabled": True,
            "config": {"bot_token": "999:MULTI", "chat_id": "-100111222"},
        },
    )
    dest_id = dest_res.json()["id"]

    create_res = client.post(
        "/api/cycles",
        json={
            "name": "Ciclo Multi Termos",
            "query": "  termo_inexistente_xyz ,  mochilas \n mochilas ",
            "source_id": "mercadolivre",
            "search_limit": 4,
            "max_offers_per_run": 2,
            "interval_minutes": 30,
            "product_delay_minutes": 5.0,
            "destination_ids": [dest_id],
            "deduplicate": True,
            "filters": {"require_affiliate_link": True},
        },
    )
    assert create_res.status_code == 200
    created_cycle = create_res.json()
    cycle_id = created_cycle["id"]
    # Verifica que a query foi normalizada e deduplicada ao salvar
    assert created_cycle["query"] == "termo_inexistente_xyz, mochilas"
    assert created_cycle["product_delay_minutes"] == 5.0

    # 6. Testa Edição de Ciclo existente (PUT /api/cycles/{cycle_id})
    edit_res = client.put(
        f"/api/cycles/{cycle_id}",
        json={
            "name": "Ciclo Multi Termos Editado",
            "query": "termo_vazio_zero, notebook, mochilas",
            "source_id": "mercadolivre",
            "search_limit": 6,
            "max_offers_per_run": 2,
            "interval_minutes": 45,
            "product_delay_minutes": 0.0,  # 0 para teste rápido sem sleep
            "destination_ids": [dest_id],
            "deduplicate": True,
            "filters": {"require_affiliate_link": True},
        },
    )
    assert edit_res.status_code == 200
    edited_cycle = edit_res.json()
    assert edited_cycle["name"] == "Ciclo Multi Termos Editado"
    assert edited_cycle["query"] == "termo_vazio_zero, notebook, mochilas"
    assert edited_cycle["interval_minutes"] == 45
    assert edited_cycle["product_delay_minutes"] == 0.0

    # 7. Executa ciclo onde "termo_vazio_zero" retorna 0 produtos, mas "notebook" e "mochilas" retornam produtos válidos
    empty_html = "<!DOCTYPE html><html><body><div class='ui-search-rescue'>Não há anúncios que correspondam à sua busca</div></body></html>"
    notebook_html = """
    <!DOCTYPE html><html><body>
      <ol class="ui-search-layout">
        <li class="ui-search-layout__item">
          <div class="poly-card">
            <img class="poly-component__picture" src="https://http2.mlstatic.com/D_NQ_NP_MLB800001.webp" />
            <a class="poly-component__title" href="https://www.mercadolivre.com.br/notebook-gamer/p/MLB800001">Notebook Gamer i7</a>
            <div class="poly-component__price">
              <div class="poly-price__current">
                <span class="andes-money-amount"><span class="andes-money-amount__fraction">4500</span></span>
              </div>
            </div>
          </div>
        </li>
      </ol>
    </body></html>
    """
    mochilas_html = """
    <!DOCTYPE html><html><body>
      <ol class="ui-search-layout">
        <li class="ui-search-layout__item">
          <div class="poly-card">
            <img class="poly-component__picture" src="https://http2.mlstatic.com/D_NQ_NP_MLB800002.webp" />
            <a class="poly-component__title" href="https://www.mercadolivre.com.br/mochila-impermeavel/p/MLB800002">Mochila Impermeável Pro</a>
            <div class="poly-component__price">
              <div class="poly-price__current">
                <span class="andes-money-amount"><span class="andes-money-amount__fraction">199</span></span>
              </div>
            </div>
          </div>
        </li>
      </ol>
    </body></html>
    """

    visited_urls: list[str] = []

    def mock_multi_kw_route(route: Route) -> None:
        url = route.request.url
        visited_urls.append(url)
        if "termo_vazio_zero" in url or "termo-vazio-zero" in url:
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=empty_html)
        elif "notebook" in url:
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=notebook_html)
        else:
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=mochilas_html)

    scraper_service._route_handler = mock_multi_kw_route
    try:
        run_res = client.post(f"/api/cycles/{cycle_id}/run-now")
        assert run_res.status_code == 200
        run_data = run_res.json()
        assert run_data["status"] == "completed"
        # "termo_vazio_zero" retornou 0, mas "notebook" (1) e "mochilas" (1) foram processados e enviados!
        assert run_data["searched_count"] == 2
        assert run_data["sent_count"] == 2
        assert len(tg_sent) == 2
        captions = [m.get("caption", "") for m in tg_sent]
        assert any("Notebook Gamer i7" in c for c in captions)
        assert any("Mochila Impermeável Pro" in c for c in captions)
    finally:
        scraper_service._route_handler = None


def test_affiliate_sources_destination_editing_and_platform_templates() -> None:
    """Valida modelo extensível de fontes de afiliado, edição de destinos e templates por plataforma com preview."""
    # 1. Lista fontes de afiliado extensíveis (Mercado Livre ativo + Shopee + Amazon com fields_schema)
    prov_res = client.get("/api/affiliates/providers")
    assert prov_res.status_code == 200
    providers = prov_res.json()
    prov_map = {p["provider_id"]: p for p in providers}
    assert "mercadolivre" in prov_map
    assert "shopee" in prov_map
    assert "amazon" in prov_map
    assert len(prov_map["mercadolivre"]["fields_schema"]) >= 2
    assert len(prov_map["shopee"]["fields_schema"]) >= 2

    # Salva credenciais extensíveis para Shopee sem quebrar Mercado Livre
    shopee_cfg_res = client.put(
        "/api/affiliates/config",
        json={
            "provider_id": "shopee",
            "enabled": True,
            "credentials": {"app_id": "998811", "app_secret": "sh_secret_key"},
        },
    )
    assert shopee_cfg_res.status_code == 200
    assert shopee_cfg_res.json()["credentials"]["app_id"] == "998811"

    # 2. Cria e depois EDITA um destino de publicação (PUT /api/outputs/{id})
    create_dest_res = client.post(
        "/api/outputs",
        json={
            "name": "Canal Original",
            "output_type": "telegram",
            "enabled": True,
            "config": {"bot_token": "12345:ORIG", "chat_id": "@canal_orig"},
        },
    )
    assert create_dest_res.status_code == 200
    dest_id = create_dest_res.json()["id"]

    edit_dest_res = client.put(
        f"/api/outputs/{dest_id}",
        json={
            "name": "Canal Editado VIP",
            "output_type": "telegram",
            "enabled": True,
            "config": {"bot_token": "12345:EDITADO", "chat_id": "@canal_vip"},
        },
    )
    assert edit_dest_res.status_code == 200
    edited_dest = edit_dest_res.json()
    assert edited_dest["id"] == dest_id
    assert edited_dest["name"] == "Canal Editado VIP"
    assert edited_dest["config"]["chat_id"] == "@canal_vip"

    # 3. Templates por plataforma (Telegram vs WhatsApp), Validação e Preview
    tpls_res = client.get("/api/templates")
    assert tpls_res.status_code == 200
    tpls = {t["platform"]: t for t in tpls_res.json()}
    assert "telegram" in tpls
    assert "whatsapp" in tpls

    # Rejeita template sem variável obrigatória {link_afiliado}
    bad_tpl_res = client.put(
        "/api/templates/telegram",
        json={
            "platform": "telegram",
            "template_text": "Oferta: {titulo} por {preco} (sem link)",
            "parse_mode": "HTML",
        },
    )
    assert bad_tpl_res.status_code == 422

    # Rejeita template com variável desconhecida {cupom_secreto}
    unknown_var_res = client.put(
        "/api/templates/whatsapp",
        json={
            "platform": "whatsapp",
            "template_text": "*{titulo}* por *{preco}* com {cupom_secreto} -> {link_afiliado}",
            "parse_mode": "Markdown",
        },
    )
    assert unknown_var_res.status_code == 422

    # Salva templates distintos para Telegram (HTML) e WhatsApp (Markdown)
    custom_tg_res = client.put(
        "/api/templates/telegram",
        json={
            "platform": "telegram",
            "template_text": "⚡ <b>{titulo}</b>\nDe: <s>{preco_original}</s> | Por: <b>{preco}</b> ({desconto})\n👉 {link_afiliado}",
            "parse_mode": "HTML",
        },
    )
    assert custom_tg_res.status_code == 200

    custom_wa_res = client.put(
        "/api/templates/whatsapp",
        json={
            "platform": "whatsapp",
            "template_text": "🟢 *{titulo}*\nDe ~{preco_original}~ por *{preco}* ({desconto})\nCompre: {link_afiliado}",
            "parse_mode": "Markdown",
        },
    )
    assert custom_wa_res.status_code == 200

    # Testa endpoint de Preview (/api/templates/preview)
    preview_res = client.post(
        "/api/templates/preview",
        json={
            "platform": "whatsapp",
            "template_text": custom_wa_res.json()["template_text"],
            "sample_data": {
                "titulo": "Fone Bluetooth ANC",
                "preco": 199.90,
                "preco_original": 299.90,
                "desconto": 33,
                "link_afiliado": "https://mercadolivre.com.br/sec/test",
            },
        },
    )
    assert preview_res.status_code == 200
    prev_data = preview_res.json()
    assert "🟢 *Fone Bluetooth ANC*" in prev_data["rendered_text"]
    assert "R$ 199,90" in prev_data["rendered_text"]
    assert "33% OFF" in prev_data["rendered_text"]

    # Restaura template padrão do Telegram para não impactar outros fluxos
    reset_res = client.post("/api/templates/telegram/reset")
    assert reset_res.status_code == 200

    # 4. Testa verificação segura de token do Telegram (/api/outputs/telegram/verify-token)
    # Formato inválido
    bad_fmt_res = client.post(
        "/api/outputs/telegram/verify-token",
        json={"bot_token": "token_invalido_sem_dois_pontos"},
    )
    assert bad_fmt_res.status_code == 200
    assert bad_fmt_res.json()["valid"] is False

    # Mock de resposta Telegram getUpdates (sucesso e erro 401)
    from unittest.mock import patch

    mock_success_resp = {
        "ok": True,
        "result": [
            {
                "update_id": 100,
                "message": {"chat": {"id": -100123, "title": "Canal Ofertas Teste"}},
            }
        ],
    }

    class DummyResponse:
        def __init__(self, status_code: int, json_data: dict):
            self.status_code = status_code
            self._json = json_data
            self.headers = {"content-type": "application/json"}

        def json(self):
            return self._json

    async def mock_get(url, params=None):
        if "123456789:VALID_TOKEN" in url:
            return DummyResponse(200, mock_success_resp)
        return DummyResponse(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})

    with patch("httpx.AsyncClient.get", side_effect=mock_get):
        ok_verify = client.post(
            "/api/outputs/telegram/verify-token",
            json={"bot_token": "123456789:VALID_TOKEN"},
        )
        assert ok_verify.status_code == 200
        ok_data = ok_verify.json()
        assert ok_data["valid"] is True
        assert ok_data["bot_id"] == "123456789"
        assert ok_data["updates_count"] == 1
        assert "Canal Ofertas Teste" in ok_data["sample_chat_titles"]

        fail_verify = client.post(
            "/api/outputs/telegram/verify-token",
            json={"bot_token": "987654321:INVALID_TOKEN"},
        )
        assert fail_verify.status_code == 200
        fail_data = fail_verify.json()
        assert fail_data["valid"] is False
        assert fail_data["status_code"] == 401
        assert "Unauthorized" in fail_data["message"]


def test_idempotent_creation_and_pagination_unblock() -> None:
    """Valida a idempotência de criação de ciclos e destinos, e o desbloqueio por paginação rotativa."""
    from playwright.sync_api import Route

    # 1. Idempotência de Destino
    dest_data = {
        "name": "Canal Idempotente",
        "output_type": "telegram",
        "enabled": True,
        "config": {"bot_token": "999999:IDEMP_TOKEN", "chat_id": "-100111222333"},
    }
    r1 = client.post("/api/outputs", json=dest_data)
    assert r1.status_code == 200
    dest1 = r1.json()

    # Reenvio idêntico do destino sem id deve retornar o mesmo registro sem duplicar
    r2 = client.post("/api/outputs", json=dest_data)
    assert r2.status_code == 200
    dest2 = r2.json()
    assert dest1["id"] == dest2["id"]

    # 2. Idempotência de Ciclo
    cycle_data = {
        "name": "Campanha Teste Idempotência",
        "query": "fone bluetooth",
        "source_id": "mercadolivre",
        "search_limit": 5,
        "max_offers_per_run": 2,
        "interval_minutes": 30,
        "destination_ids": [dest1["id"], dest1["id"]],  # destinos duplicados de propósito
        "deduplicate": True,
    }
    c1_res = client.post("/api/cycles", json=cycle_data)
    assert c1_res.status_code == 200
    c1 = c1_res.json()
    assert len(c1["destination_ids"]) == 1  # normalizado para 1 destino único

    # Reenvio com o mesmo nome deve atualizar o ciclo existente em vez de duplicar
    c2_res = client.post("/api/cycles", json=cycle_data)
    assert c2_res.status_code == 200
    c2 = c2_res.json()
    assert c1["id"] == c2["id"]

    # 3. Teste de Paginação Rotativa para Produtos Inéditos
    # Simula página 1 com produto MLB9001 e página 2 com MLB9002
    page1_html = """
    <html><body><ol class="ui-search-layout">
      <li class="ui-search-layout__item"><div class="poly-card">
        <a class="poly-component__title" href="https://www.mercadolivre.com.br/item1/p/MLB9001">Fone Bluetooth P1</a>
        <div class="poly-component__price"><div class="poly-price__current">
          <span class="andes-money-amount"><span class="andes-money-amount__fraction">100</span></span>
        </div></div>
      </div></li>
    </ol></body></html>
    """
    page2_html = """
    <html><body><ol class="ui-search-layout">
      <li class="ui-search-layout__item"><div class="poly-card">
        <a class="poly-component__title" href="https://www.mercadolivre.com.br/item2/p/MLB9002">Fone Bluetooth P2 Inédito</a>
        <div class="poly-component__price"><div class="poly-price__current">
          <span class="andes-money-amount"><span class="andes-money-amount__fraction">150</span></span>
        </div></div>
      </div></li>
    </ol></body></html>
    """

    def mock_pagination_route(route: Route) -> None:
        url = route.request.url
        if "_Desde_" in url:
            route.fulfill(status=200, content_type="text/html", body=page2_html)
        else:
            route.fulfill(status=200, content_type="text/html", body=page1_html)

    scraper_service._route_handler = mock_pagination_route
    try:
        # Marca previamente o produto MLB9001 como já enviado no banco de dados
        from app.models import SentOfferRecord

        storage_service.record_sent_offer(
            SentOfferRecord(
                id="offer_mock_p1",
                dedup_key="mercadolivre:mlb9001",
                cycle_id=c1["id"],
                cycle_name=c1["name"],
                source_id="mercadolivre",
                destination_id=dest1["id"],
                destination_name="Canal Idempotente",
                product_id="MLB9001",
                product_name="Fone Bluetooth P1",
                product_url="https://www.mercadolivre.com.br/item1/p/MLB9001",
                affiliate_url="https://www.mercadolivre.com.br/item1/p/MLB9001",
                status="sent",
                sent_at="2026-11-15T00:00:00Z",
            )
        )

        # Executa o ciclo: a página 1 só tem MLB9001 (duplicado); o scanner deve avançar para a página 2 (_Desde_) e encontrar MLB9002!
        run_res = client.post(f"/api/cycles/{c1['id']}/run-now")
        assert run_res.status_code == 200
        run_data = run_res.json()
        assert run_data["status"] == "completed"
        assert run_data["deduplicated_new_count"] >= 1
        assert run_data["sent_count"] >= 1

        # Verifica se o cursor de página foi persistido para a próxima execução
        refreshed_cycle = storage_service.get_cycle(c1["id"])
        assert refreshed_cycle is not None
        assert refreshed_cycle.last_page_offset > 1
    finally:
        scraper_service._route_handler = None


def test_performance_optimizations_stage_metrics_and_1min_scheduler() -> None:
    """Valida otimizações reais de desempenho, telemetria por etapa e scheduler de 1 minuto sem deriva."""
    from datetime import datetime, timedelta, timezone
    import time
    from playwright.sync_api import Route
    from app.cycles import CycleScheduler

    storage_service.clear_history()
    scraper_service.clear_cache()

    # 1. Valida cálculo de cadência fixa (start-to-start) para ciclos de 1 minuto
    tz = timezone(timedelta(hours=-3))
    slot_0 = datetime(2026, 11, 15, 10, 0, 0, tzinfo=tz)

    # Caso A: Execução durou 14s (terminou às 10:00:14) -> próximo início DEVE ser 10:01:00 (e não 10:01:14)
    finished_normal = datetime(2026, 11, 15, 10, 0, 14, tzinfo=tz)
    next_slot_a = CycleScheduler.compute_next_scheduled_slot(slot_0, interval_minutes=1, now_dt=finished_normal)
    assert next_slot_a == datetime(2026, 11, 15, 10, 1, 0, tzinfo=tz)

    # Caso B: Execução lenta durou 68s (terminou às 10:01:08) -> não acumula sobreposição e ancora em 10:02:00
    finished_slow = datetime(2026, 11, 15, 10, 1, 8, tzinfo=tz)
    next_slot_b = CycleScheduler.compute_next_scheduled_slot(slot_0, interval_minutes=1, now_dt=finished_slow)
    assert next_slot_b == datetime(2026, 11, 15, 10, 2, 0, tzinfo=tz)

    # 2. Testa ciclo real com intervalo de 1 minuto (interval_minutes=1) e 3 palavras-chave em lote
    dest_res = client.post(
        "/api/outputs",
        json={
            "name": "Telegram 1-Min Perf",
            "output_type": "telegram",
            "enabled": True,
            "config": {"bot_token": "777777:PERF_TOKEN", "chat_id": "-100777888999"},
        },
    )
    assert dest_res.status_code == 200
    dest_id = dest_res.json()["id"]

    cycle_1m_res = client.post(
        "/api/cycles",
        json={
            "name": "Ciclo Rápido 1 Minuto",
            "query": "notebook, ssd nvme, monitor 144hz",
            "source_id": "mercadolivre",
            "search_limit": 5,
            "max_offers_per_run": 3,
            "interval_minutes": 1,
            "product_delay_minutes": 0.0,
            "destination_ids": [dest_id],
            "deduplicate": True,
            "filters": {"require_affiliate_link": True},
        },
    )
    assert cycle_1m_res.status_code == 200
    cycle_1m = cycle_1m_res.json()
    assert cycle_1m["interval_minutes"] == 1
    cid = cycle_1m["id"]

    fast_html_template = """
    <!DOCTYPE html><html><body>
      <ol class="ui-search-layout">
        <li class="ui-search-layout__item">
          <div class="poly-card">
            <img class="poly-component__picture" src="https://http2.mlstatic.com/D_NQ_NP_MLB{id}.webp" />
            <a class="poly-component__title" href="https://www.mercadolivre.com.br/item-{id}/p/MLB{id}">Oferta {kw} #{id}</a>
            <div class="poly-component__price">
              <s class="andes-money-amount--previous"><span class="andes-money-amount__fraction">900</span></s>
              <div class="poly-price__current">
                <span class="andes-money-amount"><span class="andes-money-amount__fraction">599</span></span>
                <span class="andes-money-amount__discount">33% OFF</span>
              </div>
            </div>
            <div class="poly-component__reviews">
              <span class="poly-reviews__rating">4.9</span>
              <span class="poly-reviews__total">(150)</span>
            </div>
          </div>
        </li>
      </ol>
    </body></html>
    """

    def mock_fast_route(route: Route) -> None:
        url = route.request.url
        if "notebook" in url:
            body = fast_html_template.format(id="910001", kw="Notebook")
        elif "ssd" in url:
            body = fast_html_template.format(id="910002", kw="SSD")
        else:
            body = fast_html_template.format(id="910003", kw="Monitor")
        route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)

    scraper_service._route_handler = mock_fast_route
    try:
        t0 = time.perf_counter()
        run_res = client.post(f"/api/cycles/{cid}/run-now")
        elapsed_wall = time.perf_counter() - t0
        assert run_res.status_code == 200
        run_log = run_res.json()

        # Deve concluir muito abaixo de 60s (tipicamente < 2.5s para 3 keywords em 1 único Chromium)
        assert elapsed_wall < 10.0
        assert run_log["status"] == "completed"
        assert run_log["searched_count"] == 3
        assert run_log["filtered_count"] == 3
        assert run_log["affiliate_ok_count"] == 3
        assert run_log["deduplicated_new_count"] == 3
        assert run_log["sent_count"] == 3

        # Valida telemetria detalhada por etapa
        assert run_log["scheduled_at"] is not None
        assert run_log["started_at"] is not None
        assert run_log["finished_at"] is not None
        assert run_log["duration_seconds"] is not None and run_log["duration_seconds"] >= 0.0
        assert run_log["schedule_delay_seconds"] >= 0.0
        assert run_log["navigation_ms"] >= 0.0
        assert run_log["extraction_ms"] >= 0.0
        assert run_log["processing_ms"] >= 0.0
        assert run_log["affiliate_ms"] >= 0.0
        assert run_log["dispatch_ms"] >= 0.0
        assert run_log["bottleneck_stage"] in ("navegacao", "extracao", "processamento", "afiliado", "envio", "nenhum")

        # Verifica atualização das métricas de desempenho no CycleModel
        updated_cycle = client.get("/api/cycles").json()
        target_cycle = next(c for c in updated_cycle if c["id"] == cid)
        assert target_cycle["last_duration_seconds"] is not None
        assert target_cycle["last_bottleneck"] == run_log["bottleneck_stage"]

        # Valida deduplicação em lote O(1) em memória
        unsent = storage_service.filter_unsent_dedup_keys(
            ["mercadolivre:mlb910001", "mercadolivre:mlb910002", "mercadolivre:mlb_inedito_999"]
        )
        assert unsent == {"mercadolivre:mlb_inedito_999"}

        # 3. Valida imunidade a falso-positivo de reCAPTCHA v3 invisível e preservação parcial entre keywords
        # Cenário: 'notebook' traz card + iframe invisível reCAPTCHA v3; 'smartphone' sofre bloqueio real.
        # O ciclo deve extrair 'notebook' sem falso-positivo e enviar 'notebook' mesmo se 'smartphone' bloquear.
        notebook_with_v3_badge_html = """
        <!DOCTYPE html><html><body>
          <div class="grecaptcha-badge">
            <iframe src="https://www.google.com/recaptcha/enterprise/anchor?ar=1&size=invisible"></iframe>
          </div>
          <ol class="ui-search-layout">
            <li class="ui-search-layout__item">
              <div class="poly-card">
                <img class="poly-component__picture" src="https://http2.mlstatic.com/D_NQ_NP_MLB920001.webp" />
                <a class="poly-component__title" href="https://www.mercadolivre.com.br/notebook-v3/p/MLB920001">Notebook Ultra V3</a>
                <div class="poly-component__price">
                  <div class="poly-price__current">
                    <span class="andes-money-amount"><span class="andes-money-amount__fraction">3200</span></span>
                  </div>
                </div>
              </div>
            </li>
          </ol>
        </body></html>
        """
        real_captcha_wall_html = """
        <!DOCTYPE html><html><body>
          <div id="captcha" style="width: 300px; height: 250px; display: block;">
            <iframe src="https://www.google.com/recaptcha/api2/bframe" width="300" height="250"></iframe>
          </div>
        </body></html>
        """

        def mock_partial_block_route(route: Route) -> None:
            url = route.request.url
            if "notebook" in url:
                route.fulfill(status=200, content_type="text/html; charset=utf-8", body=notebook_with_v3_badge_html)
            else:
                route.fulfill(status=200, content_type="text/html; charset=utf-8", body=real_captcha_wall_html)

        scraper_service._route_handler = mock_partial_block_route
        partial_cycle_res = client.post(
            "/api/cycles",
            json={
                "name": "Ciclo Resiliente Notebook Smartphone",
                "query": "notebook, smartphone",
                "source_id": "mercadolivre",
                "search_limit": 5,
                "max_offers_per_run": 2,
                "interval_minutes": 1,
                "product_delay_minutes": 0.0,
                "destination_ids": [dest_id],
                "deduplicate": True,
                "filters": {"require_affiliate_link": True},
            },
        )
        partial_cid = partial_cycle_res.json()["id"]
        partial_run_res = client.post(f"/api/cycles/{partial_cid}/run-now")
        assert partial_run_res.status_code == 200
        partial_run = partial_run_res.json()
        assert partial_run["status"] == "completed"
        assert partial_run["searched_count"] == 1
        assert partial_run["sent_count"] == 1
    finally:
        scraper_service._route_handler = None





