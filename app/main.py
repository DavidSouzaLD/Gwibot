"""Servidor FastAPI para interface web local, sessão, afiliados, destinos e ciclos automatizados."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.affiliates import affiliate_registry, ml_affiliate_provider
from app.config import STATIC_DIR, settings
from app.cycles import cycle_scheduler
from app.logger import logger
from app.models import (
    AffiliateConfigModel,
    AffiliateConvertRequest,
    AffiliateConvertResponse,
    AuthStatusResponse,
    CatalogProductRecord,
    CycleCreateRequest,
    CycleModel,
    CycleRunLog,
    DEFAULT_PLATFORM_TEMPLATES,
    DeliveryResult,
    HealthResponse,
    OutputDestinationConfig,
    PLATFORM_MAX_CHARS,
    PlatformTemplateModel,
    ProductAuditRecord,
    ProductItem,
    QueuedOfferRecord,
    SearchResponse,
    SentOfferRecord,
    TelegramTokenVerifyRequest,
    TelegramTokenVerifyResponse,
    TemplatePreviewRequest,
    TemplatePreviewResponse,
    validate_template_string,
)
from app.outputs import output_registry, render_offer_template
from app.scraper import scraper_service
from app.session_manager import session_manager
from app.sources import source_registry
from app.storage import storage_service


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Recupera ciclos em execução ao iniciar o servidor e finaliza agendamentos no shutdown."""
    await cycle_scheduler.recover_running_cycles_on_startup()
    yield
    await cycle_scheduler.shutdown()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
    description=(
        "Hub local de scraping, geração de links de afiliados do Mercado Livre e "
        "distribuição automatizada de ofertas (Telegram/WhatsApp) via ciclos recorrentes."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def disable_static_cache_middleware(request: Request, call_next: Any) -> Any:
    """Evita cache obsoleto de HTML/CSS/JS no navegador após atualizações da interface."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Padroniza erros de validação de parâmetros da API (HTTP 422)."""
    raw_errors = exc.errors()
    logger.warning("Erro de validação em %s: %s", request.url.path, raw_errors)
    messages = [
        str(err.get("msg", "")).replace("Value error, ", "")
        for err in raw_errors
        if err.get("msg")
    ]
    detail_msg = " | ".join(messages) if messages else "Parâmetros de entrada inválidos."
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder(
            {
                "status": "error",
                "message": detail_msg,
                "detail": detail_msg,
                "details": raw_errors,
            }
        ),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Captura falhas inesperadas e retorna JSON estruturado."""
    logger.error("Erro não tratado em %s: %s", request.url.path, exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "status": "error",
            "message": "Ocorreu um erro interno ao processar a requisição.",
            "detail": str(exc),
        },
    )


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """Serve a interface web local principal."""
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="Interface web não encontrada em /static/index.html")
    return FileResponse(index_file)


@app.get("/api/health", response_model=HealthResponse, tags=["System"])
async def health_check() -> HealthResponse:
    """Verifica a disponibilidade da API, do Playwright, login e ciclos ativos."""
    auth_status = session_manager.get_status()
    return HealthResponse(
        status="healthy",
        service=settings.app_name,
        version=settings.app_version,
        timestamp=datetime.now().astimezone().isoformat(),
        playwright_ready=scraper_service.check_playwright_ready(),
        browser_channel="chromium",
        authenticated=auth_status.authenticated,
        active_cycles=cycle_scheduler.active_cycles_count(),
    )


@app.get("/api/sources", tags=["System"])
async def list_registered_capabilities() -> dict[str, Any]:
    """Lista fontes de pesquisa, provedores de afiliados e canais de saída registrados."""
    return {
        "search_sources": source_registry.list_sources(),
        "affiliate_providers": affiliate_registry.list_providers(),
        "output_types": output_registry.list_output_types(),
    }


# =====================================================================
# Autenticação Persistente (/api/auth/*)
# =====================================================================

@app.get("/api/auth/status", response_model=AuthStatusResponse, tags=["Authentication"])
async def get_auth_status() -> AuthStatusResponse:
    """Retorna o status atual da sessão persistente do Mercado Livre."""
    return session_manager.get_status()


@app.post("/api/auth/login", response_model=AuthStatusResponse, tags=["Authentication"])
async def start_login() -> JSONResponse:
    """Abre uma janela visível do Chromium para login manual do usuário no Mercado Livre."""
    started, status = await session_manager.start_interactive_login()
    http_code = 202 if started else 409
    return JSONResponse(status_code=http_code, content=status.model_dump())


@app.post("/api/auth/logout", response_model=AuthStatusResponse, tags=["Authentication"])
async def logout_session() -> AuthStatusResponse:
    """Remove a sessão persistida em disco e limpa o estado em memória."""
    status = session_manager.get_status()
    if status.state == "logging_in":
        raise HTTPException(
            status_code=409,
            detail="Há um fluxo de login em andamento. Feche a janela de login antes de encerrar a sessão.",
        )
    return session_manager.logout()


# =====================================================================
# Busca Direta (/api/search)
# =====================================================================

@app.get("/api/search", response_model=SearchResponse, tags=["Scraper"])
async def search_products(
    query: str = Query(
        ...,
        min_length=1,
        max_length=120,
        description="Nicho ou palavra-chave para pesquisar no Mercado Livre (ex: mochila)",
    ),
    limit: int = Query(
        default=settings.default_limit,
        ge=settings.min_limit,
        le=settings.max_limit,
        description="Quantidade máxima de produtos a retornar (1 a 120)",
    ),
) -> SearchResponse:
    """Realiza a busca de produtos no Mercado Livre e converte automaticamente links de afiliados."""
    clean_query = query.strip()
    if not clean_query:
        raise HTTPException(
            status_code=400,
            detail="O parâmetro 'query' não pode estar vazio ou conter apenas espaços.",
        )

    logger.info("Requisição GET /api/search recebida | query='%s' | limit=%d", clean_query, limit)
    return await scraper_service.search_products(query=clean_query, limit=limit)


# =====================================================================
# Configuração e Conversão de Afiliados (/api/affiliates/*)
# =====================================================================

@app.get("/api/affiliates/providers", tags=["Affiliates"])
async def list_affiliate_providers_with_configs() -> list[dict[str, Any]]:
    """Lista todas as fontes de afiliado suportadas e futuras com seu schema de campos e configuração salva."""
    return affiliate_registry.list_providers()


@app.get("/api/affiliates/config", response_model=AffiliateConfigModel, tags=["Affiliates"])
async def get_affiliate_settings(provider_id: str = "mercadolivre") -> AffiliateConfigModel:
    """Obtém a configuração atual do provedor de afiliados."""
    return storage_service.get_affiliate_config(provider_id)


@app.put("/api/affiliates/config", response_model=AffiliateConfigModel, tags=["Affiliates"])
async def update_affiliate_settings(config: AffiliateConfigModel) -> AffiliateConfigModel:
    """Salva a configuração oficial de afiliado (Mercado Livre ou outras fontes extensíveis)."""
    try:
        affiliate_registry.get(config.provider_id)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if (
        config.provider_id == "mercadolivre"
        and config.enabled
        and config.matt_tool.strip()
        and not config.matt_tool.strip().isdigit()
    ):
        raise HTTPException(
            status_code=400,
            detail="O campo 'matt_tool' deve conter apenas números (ex: 12345678).",
        )
    return storage_service.save_affiliate_config(config)


@app.post("/api/affiliates/convert", response_model=AffiliateConvertResponse, tags=["Affiliates"])
async def convert_single_affiliate_url(req: AffiliateConvertRequest) -> AffiliateConvertResponse:
    """Converte uma URL de produto em link de afiliado oficial usando a fonte selecionada."""
    try:
        provider = affiliate_registry.get(req.provider_id)
    except KeyError:
        provider = ml_affiliate_provider
    return await provider.convert_url(req.url, product_id=req.product_id)


# =====================================================================
# Destinos de Saída — Telegram & WhatsApp (/api/outputs/*)
# =====================================================================

@app.get("/api/outputs", response_model=list[OutputDestinationConfig], tags=["Outputs"])
async def list_output_destinations() -> list[OutputDestinationConfig]:
    """Lista todos os destinos de publicação configurados."""
    return storage_service.list_destinations()


@app.get("/api/outputs/{destination_id}", response_model=OutputDestinationConfig, tags=["Outputs"])
async def get_output_destination(destination_id: str) -> OutputDestinationConfig:
    """Obtém um destino de publicação específico pelo ID."""
    dest = storage_service.get_destination(destination_id)
    if not dest:
        raise HTTPException(status_code=404, detail="Destino não encontrado.")
    return dest


@app.post("/api/outputs", response_model=OutputDestinationConfig, tags=["Outputs"])
async def create_or_update_output_destination(dest: OutputDestinationConfig) -> OutputDestinationConfig:
    """Cria ou atualiza um destino de publicação (ex: canal/grupo do Telegram)."""
    try:
        out_source = output_registry.get(dest.output_type)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    valid, err = out_source.validate_config(dest.config)
    if not valid:
        raise HTTPException(status_code=400, detail=err)

    return storage_service.save_destination(dest)


@app.put("/api/outputs/{destination_id}", response_model=OutputDestinationConfig, tags=["Outputs"])
async def update_output_destination(destination_id: str, dest: OutputDestinationConfig) -> OutputDestinationConfig:
    """Edita um destino de publicação existente preservando seu ID e histórico."""
    existing = storage_service.get_destination(destination_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Destino não encontrado.")

    try:
        out_source = output_registry.get(dest.output_type)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    valid, err = out_source.validate_config(dest.config)
    if not valid:
        raise HTTPException(status_code=400, detail=err)

    dest.id = destination_id
    return storage_service.save_destination(dest)


@app.delete("/api/outputs/{destination_id}", tags=["Outputs"])
async def delete_output_destination(destination_id: str) -> dict[str, Any]:
    """Remove um destino de publicação."""
    deleted = storage_service.delete_destination(destination_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Destino não encontrado.")
    return {"status": "ok", "deleted_id": destination_id}


@app.post("/api/outputs/{destination_id}/test", response_model=DeliveryResult, tags=["Outputs"])
async def test_output_destination(destination_id: str) -> DeliveryResult:
    """Envia uma oferta de teste para validar as credenciais do destino (ex: Telegram)."""
    dest = storage_service.get_destination(destination_id)
    if not dest:
        raise HTTPException(status_code=404, detail="Destino não encontrado.")

    out_source = output_registry.get(dest.output_type)
    return await out_source.test_connection(dest)


@app.post("/api/outputs/telegram/verify-token", response_model=TelegramTokenVerifyResponse, tags=["Outputs"])
async def verify_telegram_token(req: TelegramTokenVerifyRequest) -> TelegramTokenVerifyResponse:
    """Valida o token do bot diretamente na API oficial do Telegram (getUpdates) de forma segura.

    O token é tratado como sensível: não é registrado em logs e a chamada é feita pelo back-end
    evitando problemas de CORS e exposição indevida.
    """
    token = req.bot_token.strip()
    if not token or ":" not in token:
        return TelegramTokenVerifyResponse(
            valid=False,
            status_code=400,
            message="Token inválido. O formato do Telegram deve ser '<id>:<hash>' gerado pelo @BotFather.",
            raw_description="Invalid token format",
        )

    bot_prefix = token.split(":", 1)[0]
    logger.info("Verificando token do Telegram para Bot ID: %s", bot_prefix)

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params={"limit": 5})
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}

            if resp.status_code == 200 and data.get("ok"):
                updates = data.get("result", [])
                updates_count = len(updates)

                sample_titles: list[str] = []
                chat_ids: list[int] = []

                for upd in updates:
                    msg = (
                        upd.get("message")
                        or upd.get("channel_post")
                        or upd.get("my_chat_member")
                    )

                    if msg and isinstance(msg, dict):
                        chat = msg.get("chat", {})

                        # Captura Chat ID
                        chat_id = chat.get("id")
                        if chat_id and chat_id not in chat_ids:
                            chat_ids.append(chat_id)

                        # Captura título
                        title = chat.get("title") or chat.get("username")
                        if title and title not in sample_titles:
                            sample_titles.append(str(title))

                first_chat_str = f" First Chat ID: {chat_ids[0]}." if chat_ids else ""
                first_title_str = f" First Chat Title: {sample_titles[0]}." if sample_titles else ""
                return TelegramTokenVerifyResponse(
                    valid=True,
                    status_code=200,
                    message=f"Token verified successfully! {updates_count} chats found.{first_chat_str}{first_title_str}".strip(),
                    bot_id=bot_prefix,
                    chat_id=chat_ids[0] if chat_ids else None,
                    updates_count=updates_count,
                    sample_chat_titles=sample_titles,
                )

            # Se status != 200 ou ok == False
            desc = data.get("description") or "Falha na validação do token junto ao Telegram."
            return TelegramTokenVerifyResponse(
                valid=False,
                status_code=resp.status_code,
                message=f"Erro da API do Telegram ({resp.status_code}): {desc}",
                bot_id=bot_prefix,
                raw_description=desc,
            )

    except httpx.TimeoutException:
        logger.warning("Timeout ao contatar a API do Telegram para verificação de token.")
        return TelegramTokenVerifyResponse(
            valid=False,
            status_code=504,
            message="Tempo limite esgotado ao conectar aos servidores do Telegram. Verifique sua conexão à internet.",
            raw_description="Timeout connecting to api.telegram.org",
        )
    except Exception as exc:
        logger.error("Erro inesperado na verificação de token do Telegram: %s", exc)
        return TelegramTokenVerifyResponse(
            valid=False,
            status_code=500,
            message="Não foi possível verificar o token no momento devido a um erro de conexão.",
            raw_description=str(exc),
        )


# =====================================================================
# Templates de Mensagem por Plataforma (/api/templates/*)
# =====================================================================

@app.get("/api/templates", response_model=list[PlatformTemplateModel], tags=["Templates"])
async def list_platform_templates() -> list[PlatformTemplateModel]:
    """Lista os templates de mensagem configurados para cada plataforma (Telegram e WhatsApp)."""
    return storage_service.list_platform_templates()


@app.get("/api/templates/{platform}", response_model=PlatformTemplateModel, tags=["Templates"])
async def get_platform_template(platform: str) -> PlatformTemplateModel:
    """Obtém o template de mensagem da plataforma informada ('telegram' ou 'whatsapp')."""
    if platform not in ("telegram", "whatsapp"):
        raise HTTPException(status_code=400, detail="Plataforma inválida. Use 'telegram' ou 'whatsapp'.")
    return storage_service.get_platform_template(platform)


@app.put("/api/templates/{platform}", response_model=PlatformTemplateModel, tags=["Templates"])
async def update_platform_template(platform: str, payload: PlatformTemplateModel) -> PlatformTemplateModel:
    """Salva o template customizado de uma plataforma ('telegram' ou 'whatsapp')."""
    if platform not in ("telegram", "whatsapp"):
        raise HTTPException(status_code=400, detail="Plataforma inválida. Use 'telegram' ou 'whatsapp'.")
    payload.platform = platform  # type: ignore[assignment]
    return storage_service.save_platform_template(payload)


@app.post("/api/templates/{platform}/reset", response_model=PlatformTemplateModel, tags=["Templates"])
async def reset_platform_template(platform: str) -> PlatformTemplateModel:
    """Restaura o template padrão oficial da plataforma informada."""
    if platform not in ("telegram", "whatsapp"):
        raise HTTPException(status_code=400, detail="Plataforma inválida. Use 'telegram' ou 'whatsapp'.")
    default_tpl = PlatformTemplateModel(
        platform=platform,  # type: ignore[arg-type]
        template_text=DEFAULT_PLATFORM_TEMPLATES[platform],
        parse_mode="Markdown" if platform == "whatsapp" else "HTML",
    )
    return storage_service.save_platform_template(default_tpl)


@app.post("/api/templates/preview", response_model=TemplatePreviewResponse, tags=["Templates"])
async def preview_message_template(req: TemplatePreviewRequest) -> TemplatePreviewResponse:
    """Valida e renderiza a pré-visualização de um template com variáveis dinâmicas."""
    try:
        cleaned = validate_template_string(req.template_text, req.platform)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    sample = req.sample_data or {}
    sample_product = ProductItem(
        id=str(sample.get("id") or "MLB3948571234"),
        name=str(sample.get("titulo") or "Notebook Gamer Acer Nitro V15 RTX 4050 16GB SSD 512GB"),
        url="https://www.mercadolivre.com.br/p/MLB3948571234",
        affiliate_url=str(
            sample.get("link_afiliado")
            or "https://www.mercadolivre.com.br/p/MLB3948571234?matt_tool=84729103&matt_word=meucanal2026"
        ),
        original_price=float(sample.get("preco_original") or 5499.90),
        promotional_price=float(sample.get("preco") or 4199.90),
        discount_percentage=int(sample.get("desconto") or 24),
        rating=float(sample.get("avaliacao") or 4.9),
        reviews_count=348,
        seller=str(sample.get("vendedor") or "Mercado Livre Oficial"),
    )
    cycle_label = str(sample.get("ciclo") or "Ofertas Tech Relâmpago")
    rendered = render_offer_template(
        template_text=cleaned,
        product=sample_product,
        platform=req.platform,
        cycle_name=cycle_label,
    )
    return TemplatePreviewResponse(
        platform=req.platform,
        rendered_text=rendered,
        char_count=len(rendered),
        max_length=PLATFORM_MAX_CHARS.get(req.platform, 1024),
        valid=True,
    )


# =====================================================================
# Ciclos Automatizados (/api/cycles/*)
# =====================================================================

@app.get("/api/cycles", response_model=list[CycleModel], tags=["Cycles"])
async def list_cycles() -> list[CycleModel]:
    """Lista todos os ciclos automatizados e seus estados de execução."""
    return cycle_scheduler.list_cycles_with_runtime()


@app.post("/api/cycles", response_model=CycleModel, tags=["Cycles"])
async def create_cycle(req: CycleCreateRequest) -> CycleModel:
    """Cria uma nova campanha/ciclo automatizado."""
    try:
        source_registry.get(req.source_id)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    created = storage_service.create_cycle(req)
    return cycle_scheduler.enrich_cycle_runtime_info(created)


@app.put("/api/cycles/{cycle_id}", response_model=CycleModel, tags=["Cycles"])
async def update_cycle(cycle_id: str, req: CycleCreateRequest) -> CycleModel:
    """Atualiza as configurações de um ciclo existente e reprograma o agendador se estiver em execução."""
    try:
        source_registry.get(req.source_id)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    updated = storage_service.update_cycle(cycle_id, req)
    if not updated:
        raise HTTPException(status_code=404, detail="Ciclo não encontrado.")

    if updated.state == "running":
        rearmed = await cycle_scheduler.start_cycle(cycle_id, run_immediately=False)
        if rearmed:
            return rearmed
    return cycle_scheduler.enrich_cycle_runtime_info(updated)


@app.delete("/api/cycles/{cycle_id}", tags=["Cycles"])
async def delete_cycle(cycle_id: str) -> dict[str, Any]:
    """Interrompe e remove um ciclo automatizado."""
    await cycle_scheduler.stop_cycle(cycle_id)
    deleted = storage_service.delete_cycle(cycle_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Ciclo não encontrado.")
    return {"status": "ok", "deleted_id": cycle_id}


@app.post("/api/cycles/{cycle_id}/start", response_model=CycleModel, tags=["Cycles"])
async def start_cycle_endpoint(cycle_id: str) -> CycleModel:
    """Inicia o agendamento automático do ciclo e dispara uma execução imediata."""
    updated = await cycle_scheduler.start_cycle(cycle_id, run_immediately=True)
    if not updated:
        raise HTTPException(status_code=404, detail="Ciclo não encontrado.")
    return updated


@app.post("/api/cycles/{cycle_id}/pause", response_model=CycleModel, tags=["Cycles"])
async def pause_cycle_endpoint(cycle_id: str) -> CycleModel:
    """Pausa o agendamento automático do ciclo."""
    updated = await cycle_scheduler.pause_cycle(cycle_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Ciclo não encontrado.")
    return updated


@app.post("/api/cycles/{cycle_id}/resume", response_model=CycleModel, tags=["Cycles"])
async def resume_cycle_endpoint(cycle_id: str) -> CycleModel:
    """Retoma a execução automática de um ciclo pausado."""
    updated = await cycle_scheduler.resume_cycle(cycle_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Ciclo não encontrado.")
    return updated


@app.post("/api/cycles/{cycle_id}/stop", response_model=CycleModel, tags=["Cycles"])
async def stop_cycle_endpoint(cycle_id: str) -> CycleModel:
    """Encerra o agendamento automático do ciclo."""
    updated = await cycle_scheduler.stop_cycle(cycle_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Ciclo não encontrado.")
    return updated


@app.post("/api/cycles/{cycle_id}/run-now", response_model=CycleRunLog, tags=["Cycles"])
async def run_cycle_now_endpoint(cycle_id: str) -> CycleRunLog:
    """Executa imediatamente o fluxo completo do ciclo (Search -> Filter -> Affiliate -> Dedup -> Output)."""
    ok, run_log, msg = await cycle_scheduler.execute_cycle_once(cycle_id)
    if run_log is None:
        status_code = 409 if "simultânea" in msg else 404
        raise HTTPException(status_code=status_code, detail=msg)
    return run_log


# =====================================================================
# Histórico e Monitoramento (/api/history/*)
# =====================================================================

@app.get("/api/history/runs", response_model=list[CycleRunLog], tags=["History"])
async def list_cycle_runs_history(
    limit: int = Query(default=50, ge=1, le=200),
    cycle_id: Optional[str] = Query(default=None),
) -> list[CycleRunLog]:
    """Consulta o histórico de execuções dos ciclos e eventuais erros."""
    return storage_service.list_cycle_runs(limit=limit, cycle_id=cycle_id)


@app.get("/api/history/offers", response_model=list[SentOfferRecord], tags=["History"])
async def list_sent_offers_history(
    limit: int = Query(default=100, ge=1, le=500),
    cycle_id: Optional[str] = Query(default=None),
) -> list[SentOfferRecord]:
    """Consulta o histórico de ofertas enviadas e registradas na deduplicação."""
    return storage_service.list_sent_offers(limit=limit, cycle_id=cycle_id)


@app.delete("/api/history/offers", tags=["History"])
async def clear_sent_offers_history() -> dict[str, str]:
    """Limpa o histórico de ofertas enviadas e execuções de ciclos."""
    storage_service.clear_history()
    return {"status": "ok", "message": "Histórico limpo com sucesso."}


# =====================================================================
# Banco de Produtos e Fila Persistente de Pendentes (/api/products, /api/queue)
# =====================================================================

@app.get("/api/products", response_model=list[CatalogProductRecord], tags=["Catalog"])
async def list_catalog_products_endpoint(
    limit: int = Query(default=100, ge=1, le=500),
    publication_status: Optional[str] = Query(default=None),
) -> list[CatalogProductRecord]:
    """Lista os produtos catalogados no banco local (identificados por ID único do Mercado Livre)."""
    return storage_service.list_catalog_products(limit=limit, publication_status=publication_status)


@app.get("/api/queue", response_model=list[QueuedOfferRecord], tags=["Queue"])
async def list_offer_queue_endpoint(
    limit: int = Query(default=100, ge=1, le=500),
    status: Optional[str] = Query(default=None),
    cycle_id: Optional[str] = Query(default=None),
) -> list[QueuedOfferRecord]:
    """Lista a fila persistente de ofertas pendentes, enviadas ou aguardando retentativa."""
    return storage_service.list_offer_queue(limit=limit, status=status, cycle_id=cycle_id)


@app.post("/api/queue/retry", tags=["Queue"])
async def retry_failed_queue_items_endpoint(
    cycle_id: Optional[str] = Query(default=None),
) -> dict[str, Any]:
    """Reagenda imediatamente itens com falha/aguardando retentativa na fila persistente."""
    rearmed = storage_service.retry_failed_queue_items(cycle_id=cycle_id)
    return {"status": "ok", "rearmed_count": rearmed}


@app.get("/api/observability/metrics", tags=["Observability"])
async def get_observability_metrics_endpoint(
    cycle_id: Optional[str] = Query(default=None),
) -> dict[str, int]:
    """Retorna métricas agregadas do pipeline: encontrados, elegíveis, descartados, pendentes, enviados e falhos."""
    return storage_service.get_pipeline_metrics(cycle_id=cycle_id)


@app.get("/api/observability/products", response_model=list[ProductAuditRecord], tags=["Observability"])
async def list_product_audits_endpoint(
    limit: int = Query(default=100, ge=1, le=500),
    cycle_id: Optional[str] = Query(default=None),
    run_id: Optional[str] = Query(default=None),
    product_id: Optional[str] = Query(default=None),
) -> list[ProductAuditRecord]:
    """Lista logs individuais de auditoria por produto em cada etapa do pipeline."""
    return storage_service.list_product_audits(
        limit=limit,
        cycle_id=cycle_id,
        run_id=run_id,
        product_id=product_id,
    )


# =====================================================================
# Reset Completo do Aplicativo (/api/system/reset)
# =====================================================================

@app.post("/api/system/reset", tags=["System"])
async def reset_all_system_data_endpoint() -> dict[str, str]:
    """Para todos os ciclos ativos e apaga todos os dados do sistema, deixando o app vazio e pronto para configuração."""
    await cycle_scheduler.shutdown()
    cycle_scheduler._running_locks.clear()
    storage_service.reset_all_data()
    scraper_service.clear_cache()
    scraper_service.clear_cooldown()
    session_manager.logout()
    return {
        "status": "ok",
        "message": "Todos os dados, configurações, ciclos, destinos, histórico e sessão foram resetados com sucesso.",
    }


