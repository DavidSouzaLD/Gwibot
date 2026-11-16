"""Modelos Pydantic para validação e serialização JSON da API, autenticação, afiliados, destinos e ciclos."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field, model_validator

AuthStateType = Literal["logged_out", "logging_in", "logged_in", "expired", "error"]
AffiliateStatusType = Literal["generated", "failed", "not_configured"]
CycleStateType = Literal["idle", "running", "paused", "stopped", "error"]


class ProductItem(BaseModel):
    """Representa um produto coletado no Mercado Livre.

    Campos indisponíveis no anúncio são sempre serializados como `null`.
    Preserva todos os atributos originais e inclui os metadados do link de afiliado.
    """

    id: Optional[str] = Field(default=None, description="ID do produto no Mercado Livre (ex: MLB123456789)")
    name: Optional[str] = Field(default=None, description="Nome/título do produto")
    url: Optional[str] = Field(default=None, description="URL direta original do produto")
    affiliate_url: Optional[str] = Field(
        default=None,
        description="URL convertida para link de afiliado oficial (ou null se não configurado/falhou)",
    )
    affiliate_status: Optional[AffiliateStatusType] = Field(
        default=None,
        description="Status da conversão de afiliado ('generated', 'failed', 'not_configured')",
    )
    affiliate_error: Optional[str] = Field(
        default=None,
        description="Mensagem de erro caso a geração do link de afiliado tenha falhado",
    )
    original_price: Optional[float] = Field(default=None, description="Preço original sem desconto (R$)")
    promotional_price: Optional[float] = Field(default=None, description="Preço atual/promocional de venda (R$)")
    discount_percentage: Optional[int] = Field(default=None, description="Percentual de desconto inteiro (ex: 15)")
    rating: Optional[float] = Field(default=None, description="Nota média de avaliação (0.0 a 5.0)")
    reviews_count: Optional[int] = Field(default=None, description="Quantidade total de avaliações")
    seller: Optional[str] = Field(default=None, description="Nome da loja ou vendedor exibido no anúncio")
    image_url: Optional[str] = Field(default=None, description="URL da imagem principal do produto")


class SearchMetadata(BaseModel):
    """Metadados sobre a execução da coleta."""

    query: str = Field(..., description="Termo ou nicho pesquisado")
    requested_limit: int = Field(..., description="Limite solicitado pelo usuário")
    total_returned: int = Field(..., description="Total de produtos efetivamente coletados")
    pages_visited: int = Field(..., description="Quantidade de páginas navegadas")
    elapsed_seconds: float = Field(..., description="Tempo total gasto na coleta em segundos")
    timestamp: str = Field(
        default_factory=lambda: datetime.now().astimezone().isoformat(),
        description="Data/hora ISO-8601 da execução",
    )
    warning: Optional[str] = Field(
        default=None,
        description="Aviso caso o site tenha limitado a paginação, exigido verificação humana ou retornado menos itens",
    )
    blocked_by_challenge: bool = Field(
        default=False,
        description="Indica se a coleta foi interrompida por CAPTCHA/autenticação sem tentativa de contorno",
    )
    authenticated_session_used: bool = Field(
        default=False,
        description="Indica se uma sessão autenticada salva foi carregada no navegador",
    )
    session_expired: bool = Field(
        default=False,
        description="Indica se a sessão salva expirou durante a busca (redirecionamento para login detectado)",
    )
    affiliate_converted_count: int = Field(
        default=0,
        description="Quantidade de produtos com link de afiliado gerado com sucesso",
    )
    affiliate_failed_count: int = Field(
        default=0,
        description="Quantidade de produtos cuja geração de link de afiliado falhou",
    )
    navigation_ms: float = Field(default=0.0, description="Tempo gasto em navegação de página (ms)")
    extraction_ms: float = Field(default=0.0, description="Tempo gasto em extração DOM e parsing (ms)")
    affiliate_ms: float = Field(default=0.0, description="Tempo gasto em geração de links de afiliado (ms)")
    cache_hit: bool = Field(default=False, description="Indica se o resultado veio do cache de memória de curta duração")


class SearchResponse(BaseModel):
    """Resposta padrão do endpoint GET /api/search."""

    status: str = Field(default="ok", description="Status da requisição ('ok', 'partial', 'blocked')")
    metadata: SearchMetadata
    results: list[ProductItem] = Field(default_factory=list)


class AuthStatusResponse(BaseModel):
    """Resposta dos endpoints de status e controle de sessão (/api/auth/*)."""

    authenticated: bool = Field(..., description="True se a sessão está autenticada e válida")
    state: AuthStateType = Field(..., description="Estado atual da sessão")
    message: str = Field(..., description="Mensagem amigável para exibição na interface")
    has_saved_session: bool = Field(..., description="Indica se o arquivo de sessão ou perfil existe em disco")
    persistent_profile_ready: bool = Field(
        default=False,
        description="True se o perfil persistente do Chromium (user_data_dir) está inicializado e preservado",
    )
    updated_at: Optional[str] = Field(default=None, description="Timestamp ISO-8601 da última atualização")


class HealthResponse(BaseModel):
    """Resposta padrão do endpoint GET /api/health."""

    status: str = "healthy"
    service: str
    version: str
    timestamp: str
    playwright_ready: bool
    browser_channel: str = "chromium"
    authenticated: bool = False
    active_cycles: int = 0


# =====================================================================
# Modelos de Afiliados (Affiliate Providers — Multi-Store Extensible)
# =====================================================================

ALLOWED_TEMPLATE_VARIABLES = (
    "{titulo}",
    "{preco}",
    "{preco_original}",
    "{link_afiliado}",
    "{desconto}",
    "{avaliacao}",
    "{vendedor}",
    "{ciclo}",
)

MANDATORY_TEMPLATE_VARIABLES = (
    "{titulo}",
    "{preco}",
    "{link_afiliado}",
)

PLATFORM_MAX_CHARS: dict[str, int] = {
    "telegram": 1024,   # Limite oficial de caption em sendPhoto da Bot API do Telegram
    "whatsapp": 4096,   # Limite oficial de texto na Cloud API / Evolution API do WhatsApp
}

DEFAULT_PLATFORM_TEMPLATES: dict[str, str] = {
    "telegram": (
        "📦 <b>{titulo}</b>\n\n"
        "❌ De: <s>{preco_original}</s>\n"
        "✅ <b>Por: {preco}</b>\n"
        "🏷️ Desconto: <b>{desconto}</b>\n"
        "⭐ Avaliação: <b>{avaliacao}</b>\n"
        "<a href=\"{link_afiliado}\"></a>\n"
    ),
    "whatsapp": (
        "📦 *{titulo}*\n\n"
        "❌ De: ~{preco_original}~\n"
        "✅ *Por: {preco}*\n"
        "🏷️ Desconto: *{desconto}*\n"
        "⭐ Avaliação: *{avaliacao}*\n\n"
        "🛒 *Compre aqui:* {link_afiliado}"
    ),
}


def validate_template_string(template_text: str, platform: str = "telegram") -> str:
    """Valida variáveis obrigatórias, variáveis desconhecidas e limite de caracteres por plataforma."""
    import re

    cleaned = (template_text or "").strip()
    if not cleaned:
        raise ValueError("O template de mensagem não pode estar vazio.")

    max_len = PLATFORM_MAX_CHARS.get(platform, 1024)
    if len(cleaned) > max_len:
        raise ValueError(
            f"O template para '{platform}' excede o limite de {max_len} caracteres (atual: {len(cleaned)})."
        )

    missing = [var for var in MANDATORY_TEMPLATE_VARIABLES if var not in cleaned]
    if missing:
        raise ValueError(
            f"O template deve conter todas as variáveis obrigatórias: {', '.join(missing)}."
        )

    found_vars = re.findall(r"\{[a-zA-Z0-9_]+\}", cleaned)
    unknown = [v for v in found_vars if v not in ALLOWED_TEMPLATE_VARIABLES]
    if unknown:
        raise ValueError(
            f"Variável(is) desconhecida(s) no template: {', '.join(sorted(set(unknown)))}. "
            f"Variáveis suportadas: {', '.join(ALLOWED_TEMPLATE_VARIABLES)}."
        )

    return cleaned


class AffiliateConfigModel(BaseModel):
    """Configuração extensível de provedor de afiliados (Mercado Livre, Shopee, Amazon, etc.)."""

    provider_id: str = Field(default="mercadolivre", description="Identificador da loja/provedor")
    enabled: bool = Field(default=True, description="Ativa conversão automática de links de afiliado")
    matt_word: str = Field(
        default="",
        description="Etiqueta/identificador oficial do afiliado no Mercado Livre (matt_word)",
    )
    matt_tool: str = Field(
        default="",
        description="Código numérico da ferramenta oficial do afiliado no Mercado Livre (matt_tool)",
    )
    use_authenticated_linkbuilder: bool = Field(
        default=False,
        description="Tenta consultar o endpoint oficial do Linkbuilder usando a sessão autenticada antes dos parâmetros oficiais",
    )
    credentials: dict[str, Any] = Field(
        default_factory=dict,
        description="Mapa extensível de credenciais específicas por loja (ex: app_id/secret na Shopee, partner_tag na Amazon)",
    )
    updated_at: Optional[str] = None

    @model_validator(mode="after")
    def sync_credentials_map(self) -> "AffiliateConfigModel":
        """Mantém compatibilidade bidirecional entre campos dedicados do ML e o dicionário extensível `credentials`."""
        if self.provider_id == "mercadolivre":
            if self.matt_word and not self.credentials.get("matt_word"):
                self.credentials["matt_word"] = self.matt_word
            elif self.credentials.get("matt_word") and not self.matt_word:
                self.matt_word = str(self.credentials["matt_word"])

            if self.matt_tool and not self.credentials.get("matt_tool"):
                self.credentials["matt_tool"] = self.matt_tool
            elif self.credentials.get("matt_tool") and not self.matt_tool:
                self.matt_tool = str(self.credentials["matt_tool"])

            self.credentials["use_authenticated_linkbuilder"] = self.use_authenticated_linkbuilder
        return self


class AffiliateConvertRequest(BaseModel):
    """Requisição manual para converter uma URL ou lista de URLs em links de afiliado."""

    url: str = Field(..., min_length=10, description="URL original do produto")
    product_id: Optional[str] = Field(default=None, description="ID do produto opcional")
    provider_id: str = Field(default="mercadolivre", description="Identificador da fonte de afiliado")


class AffiliateConvertResponse(BaseModel):
    """Resultado da conversão de uma URL de produto em link de afiliado."""

    original_url: str
    affiliate_url: Optional[str]
    status: AffiliateStatusType
    error: Optional[str] = None
    provider_id: str = "mercadolivre"


class TelegramTokenVerifyRequest(BaseModel):
    """Requisição para verificação segura de token de bot do Telegram."""

    bot_token: str = Field(..., min_length=10, description="Token do bot gerado pelo @BotFather")


class TelegramTokenVerifyResponse(BaseModel):
    """Resposta estruturada da verificação de token via getUpdates."""

    valid: bool
    status_code: int
    message: str
    bot_id: Optional[str] = None
    updates_count: int = 0
    raw_description: Optional[str] = None
    sample_chat_titles: list[str] = Field(default_factory=list)


# =====================================================================
# Modelos de Destinos de Saída & Templates por Plataforma
# =====================================================================

class PlatformTemplateModel(BaseModel):
    """Configuração de template de mensagem personalizável por plataforma (Telegram / WhatsApp)."""

    platform: Literal["telegram", "whatsapp"] = Field(..., description="Plataforma alvo ('telegram' ou 'whatsapp')")
    template_text: str = Field(..., min_length=10, description="Corpo do template com variáveis dinâmicas")
    parse_mode: Literal["HTML", "Markdown", "plain"] = Field(
        default="HTML",
        description="Modo de formatação nativo da plataforma ('HTML' para Telegram, 'Markdown' para WhatsApp)",
    )
    max_length: int = Field(default=1024, description="Limite máximo de caracteres da plataforma")
    available_variables: list[str] = Field(
        default_factory=lambda: list(ALLOWED_TEMPLATE_VARIABLES),
        description="Variáveis dinâmicas disponíveis para uso no template",
    )
    required_variables: list[str] = Field(
        default_factory=lambda: list(MANDATORY_TEMPLATE_VARIABLES),
        description="Variáveis obrigatórias que devem constar no template",
    )
    updated_at: Optional[str] = None

    @model_validator(mode="after")
    def check_template_rules(self) -> "PlatformTemplateModel":
        self.max_length = PLATFORM_MAX_CHARS.get(self.platform, 1024)
        self.template_text = validate_template_string(self.template_text, self.platform)
        return self


class TemplatePreviewRequest(BaseModel):
    """Requisição para pré-visualizar o resultado de um template com dados reais ou de exemplo."""

    platform: Literal["telegram", "whatsapp"] = Field(default="telegram")
    template_text: str = Field(..., min_length=1)
    sample_data: Optional[dict[str, Any]] = Field(default=None)


class TemplatePreviewResponse(BaseModel):
    """Resposta da renderização de pré-visualização de template."""

    platform: str
    rendered_text: str
    char_count: int
    max_length: int
    valid: bool = True


class OutputDestinationConfig(BaseModel):
    """Configuração persistida de um destino de publicação (Telegram, WhatsApp, etc.)."""

    id: Optional[str] = Field(default=None, description="ID único do destino")
    name: str = Field(..., min_length=1, max_length=80, description="Nome amigável (ex: Canal Ofertas Tech)")
    output_type: Literal["telegram", "whatsapp"] = Field(..., description="Tipo de destino ('telegram' ou 'whatsapp')")
    enabled: bool = Field(default=True, description="Se o destino está ativo para envios")
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Configurações específicas do canal (ex: bot_token, chat_id para Telegram)",
    )
    custom_template: Optional[str] = Field(
        default=None,
        description="Template de mensagem específico deste destino (se nulo, usa o template padrão da plataforma)",
    )
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @model_validator(mode="after")
    def validate_optional_custom_template(self) -> "OutputDestinationConfig":
        if self.custom_template is not None and self.custom_template.strip():
            self.custom_template = validate_template_string(self.custom_template, self.output_type)
        else:
            self.custom_template = None
        return self


class DeliveryResult(BaseModel):
    """Resultado do envio de uma oferta para um OutputSource."""

    success: bool
    output_id: str
    output_type: str
    destination_name: str
    product_id: Optional[str] = None
    product_name: Optional[str] = None
    affiliate_url: Optional[str] = None
    external_message_id: Optional[str] = None
    error: Optional[str] = None
    status_code: Optional[int] = None
    error_type: Optional[Literal["transient", "permanent", "rate_limit", "config"]] = None
    retry_after_seconds: Optional[float] = None
    fallback_used: bool = False
    delivery_method: Optional[str] = None
    attempts: int = 1
    timestamp: str = Field(default_factory=lambda: datetime.now().astimezone().isoformat())


# =====================================================================
# Modelos de Ciclos Automatizados (Automated Campaigns / Cycles)
# =====================================================================

class CycleFilters(BaseModel):
    """Filtros aplicados aos produtos coletados antes de converter e publicar."""

    min_price: Optional[float] = Field(default=None, ge=0, description="Preço promocional mínimo (R$)")
    max_price: Optional[float] = Field(default=None, ge=0, description="Preço promocional máximo (R$)")
    min_discount: Optional[int] = Field(default=None, ge=0, le=99, description="Percentual mínimo de desconto (%)")
    min_rating: Optional[float] = Field(default=None, ge=0.0, le=5.0, description="Nota mínima de avaliação (0 a 5)")
    require_affiliate_link: bool = Field(
        default=True,
        description="Exige que o link de afiliado tenha sido gerado com sucesso para enviar",
    )


class CycleCreateRequest(BaseModel):
    """Dados para criação ou atualização de um ciclo automatizado."""

    name: str = Field(..., min_length=1, max_length=100, description="Nome da campanha/ciclo")
    query: str = Field(
        default="",
        description="Um ou mais nichos/palavras-chave separados por vírgula ou quebra de linha (vazio = busca geral)",
    )
    source_id: str = Field(default="mercadolivre", description="Fonte de pesquisa ('mercadolivre', 'shopee', 'amazon')")
    search_limit: int = Field(default=20, ge=1, le=120, description="Quantidade de produtos a buscar por palavra-chave")
    max_offers_per_run: int = Field(default=5, ge=1, le=50, description="Limite máximo de ofertas enviadas por execução")
    interval_minutes: int = Field(default=30, ge=1, le=10080, description="Intervalo entre buscas automáticas (minutos)")
    product_delay_minutes: float = Field(
        default=0.0,
        ge=0.0,
        le=1440.0,
        description="Tempo de espera em minutos entre o envio de cada produto na mesma execução",
    )
    destination_ids: list[str] = Field(default_factory=list, description="IDs dos destinos de publicação")
    filters: CycleFilters = Field(default_factory=CycleFilters, description="Filtros de preço, desconto e avaliação")
    deduplicate: bool = Field(default=True, description="Evita enviar ofertas repetidas entre execuções")
    resend_after_hours: int = Field(
        default=0,
        ge=0,
        le=8760,
        description="Janela de reenvio em horas. 0 = nunca reenviar (deduplicação permanente). Se > 0, permite reenviar após decorridas X horas.",
    )

    @model_validator(mode="after")
    def validate_keywords_and_intervals(self) -> "CycleCreateRequest":
        from app.parser import parse_keywords

        keywords = parse_keywords(self.query)
        self.query = ", ".join(keywords) if keywords else ""

        if self.product_delay_minutes >= self.interval_minutes:
            raise ValueError(
                f"O 'Tempo entre produtos' ({self.product_delay_minutes:g} min) deve ser menor que o "
                f"'Intervalo do ciclo' ({self.interval_minutes} min)."
            )

        total_dispatch_time = self.max_offers_per_run * self.product_delay_minutes
        if self.product_delay_minutes > 0 and total_dispatch_time > self.interval_minutes:
            raise ValueError(
                f"O tempo total de disparo ({self.max_offers_per_run} produtos × "
                f"{self.product_delay_minutes:g} min = {total_dispatch_time:g} min) excede o "
                f"'Intervalo do ciclo' ({self.interval_minutes} min)."
            )
        return self


class CycleModel(CycleCreateRequest):
    """Representação completa de um ciclo persistido e seu estado de execução."""

    id: str
    state: CycleStateType = "idle"
    is_running_now: bool = False
    last_page_offset: int = 1
    last_run_at: Optional[str] = None
    next_run_at: Optional[str] = None
    last_duration_seconds: Optional[float] = None
    last_schedule_delay_seconds: Optional[float] = None
    last_bottleneck: Optional[str] = None
    total_runs: int = 0
    total_offers_sent: int = 0
    last_error: Optional[str] = None
    created_at: str
    updated_at: str


class CycleRunLog(BaseModel):
    """Registro de histórico de uma execução de ciclo (Search -> Filter -> Affiliate -> Dedup -> Output)."""

    id: str
    cycle_id: str
    cycle_name: str
    source_id: str
    query: str
    scheduled_at: Optional[str] = None
    started_at: str
    finished_at: Optional[str] = None
    duration_seconds: float = 0.0
    schedule_delay_seconds: float = 0.0
    navigation_ms: float = 0.0
    extraction_ms: float = 0.0
    processing_ms: float = 0.0
    affiliate_ms: float = 0.0
    dispatch_ms: float = 0.0
    bottleneck_stage: Optional[str] = None
    status: Literal["running", "completed", "partial", "failed"] = "running"
    searched_count: int = 0
    filtered_count: int = 0
    discarded_count: int = 0
    affiliate_ok_count: int = 0
    affiliate_fail_count: int = 0
    deduplicated_new_count: int = 0
    pending_count: int = 0
    sent_count: int = 0
    failed_send_count: int = 0
    error_message: Optional[str] = None


class SentOfferRecord(BaseModel):
    """Registro histórico de oferta enviada para deduplicação e auditoria."""

    id: str
    dedup_key: str
    cycle_id: str
    cycle_name: str
    source_id: str
    destination_id: str
    destination_name: str
    product_id: Optional[str] = None
    product_name: str
    original_price: Optional[float] = None
    promotional_price: Optional[float] = None
    discount_percentage: Optional[int] = None
    rating: Optional[float] = None
    product_url: str
    affiliate_url: str
    image_url: Optional[str] = None
    status: Literal["sent", "failed"] = "sent"
    error_message: Optional[str] = None
    sent_at: str


ProductProcessingStatusType = Literal[
    "discovered",
    "filtered_out",
    "queued",
    "affiliate_ready",
    "published",
    "error",
]
ProductPublicationStatusType = Literal[
    "unpublished",
    "pending",
    "published",
    "partial",
    "failed",
]
QueueOfferStatusType = Literal[
    "pending",
    "processing",
    "retry_wait",
    "sent",
    "failed",
]


class CatalogProductRecord(BaseModel):
    """Registro persistente no Banco de Produtos (products_catalog) identificado pelo ID do Mercado Livre."""

    product_id: str = Field(..., description="ID único canônico do produto (ex: MLB123456789)")
    dedup_key: str = Field(..., description="Chave canônica de deduplicação (ex: mercadolivre:mlb123456789)")
    source_id: str = Field(default="mercadolivre", description="Fonte onde o produto foi encontrado")
    query_terms: str = Field(default="", description="Palavras-chave associadas ao produto")
    name: str = Field(..., description="Nome/título coletado")
    product_url: str = Field(..., description="URL original do produto")
    affiliate_url: Optional[str] = Field(default=None, description="Link afiliado gerado")
    affiliate_status: Optional[AffiliateStatusType] = Field(default=None, description="Status do link afiliado")
    original_price: Optional[float] = None
    promotional_price: Optional[float] = None
    discount_percentage: Optional[int] = None
    rating: Optional[float] = None
    reviews_count: Optional[int] = None
    seller: Optional[str] = None
    image_url: Optional[str] = None
    available: bool = Field(default=True, description="Se o produto está disponível no último scrape")
    first_seen_at: str = Field(..., description="Data/hora da primeira identificação")
    last_seen_at: str = Field(..., description="Data/hora da última identificação/atualização de preço")
    processing_status: ProductProcessingStatusType = Field(
        default="discovered",
        description="Status de processamento ('discovered', 'filtered_out', 'queued', 'affiliate_ready', 'published', 'error')",
    )
    publication_status: ProductPublicationStatusType = Field(
        default="unpublished",
        description="Status de publicação ('unpublished', 'pending', 'published', 'partial', 'failed')",
    )
    last_published_destination_id: Optional[str] = Field(
        default=None,
        description="Último destino onde o produto foi publicado",
    )
    last_published_at: Optional[str] = Field(
        default=None,
        description="Data/hora do último envio bem-sucedido",
    )
    times_seen: int = Field(default=1, description="Quantidade de vezes que o produto foi avistado")
    last_error: Optional[str] = Field(default=None, description="Último erro de processamento ou envio")


class QueuedOfferRecord(BaseModel):
    """Item na fila persistente de ofertas pendentes (offer_queue) para publicação controlada por destino."""

    id: str
    product_id: str
    dedup_key: str
    cycle_id: str
    cycle_name: str
    source_id: str = "mercadolivre"
    destination_id: str
    destination_name: str
    product_snapshot: ProductItem
    affiliate_url: Optional[str] = None
    status: QueueOfferStatusType = "pending"
    attempts: int = 0
    max_attempts: int = 3
    next_retry_at: Optional[str] = None
    last_error: Optional[str] = None
    error_type: Optional[str] = None
    api_status_code: Optional[int] = None
    created_at: str
    updated_at: str
    sent_at: Optional[str] = None


class ProductAuditRecord(BaseModel):
    """Registro individual de observabilidade por produto em cada etapa do pipeline."""

    id: str
    run_id: str
    cycle_id: str
    cycle_name: str
    product_id: str
    dedup_key: str
    source_id: str
    destination_id: Optional[str] = None
    destination_name: Optional[str] = None
    filter_passed: bool = True
    filter_reason: Optional[str] = None
    queue_status: Optional[str] = None
    attempts: int = 0
    api_status_code: Optional[int] = None
    api_response: Optional[str] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    filter_ms: float = 0.0
    affiliate_ms: float = 0.0
    dispatch_ms: float = 0.0
    created_at: str
    updated_at: str
