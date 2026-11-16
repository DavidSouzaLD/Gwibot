"""Arquitetura modular de destinos de saída (OutputSource: Telegram funcional e WhatsApp preparado)."""
from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
import html
import random
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.logger import logger
from app.models import (
    DEFAULT_PLATFORM_TEMPLATES,
    PLATFORM_MAX_CHARS,
    DeliveryResult,
    OutputDestinationConfig,
    ProductItem,
)
from app.storage import storage_service

_TELEGRAM_HTML_VOID_TAGS = {"br", "hr", "img"}
_TELEGRAM_TAG_RE = re.compile(r"<(/?)([a-zA-Z0-9]+)(?:\s[^>]*)?>")
_MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB limit for Telegram sendPhoto by URL


def _is_valid_http_url(url: str | None) -> tuple[bool, str | None]:
    """Valida sintaticamente uma URL HTTP/HTTPS antes de enviá-la ao Telegram."""
    if not url or not isinstance(url, str):
        return False, "URL vazia ou ausente."
    cleaned = url.strip()
    if len(cleaned) > 2048:
        return False, "URL excede o limite de 2048 caracteres."
    if any(ch.isspace() or ord(ch) < 32 for ch in cleaned):
        return False, "URL contém espaços ou caracteres de controle inválidos."
    try:
        parts = urlsplit(cleaned)
    except Exception:
        return False, "URL malformada."
    if parts.scheme.lower() not in ("http", "https"):
        return False, f"Esquema de URL não suportado: '{parts.scheme}'."
    if not parts.netloc or "." not in parts.netloc:
        return False, "Domínio da URL inválido."
    return True, None


def _strip_html_tags(text: str) -> str:
    """Remove tags HTML e desfaz escape de entidades para fallback em texto puro."""
    no_tags = re.sub(r"<[^>]+>", "", text)
    return html.unescape(no_tags).strip()


def _truncate_html_safe(text: str, max_len: int) -> str:
    """Trunca texto HTML sem quebrar tags no meio e fechando tags abertas."""
    if len(text) <= max_len:
        return text

    budget = max(1, max_len - 1)
    cut = text[:budget]

    # Se cortou dentro de uma tag '<...', recua até antes do '<'
    last_open = cut.rfind("<")
    last_close = cut.rfind(">")
    if last_open > last_close:
        cut = cut[:last_open]

    # Se cortou dentro de uma entidade '&...;', recua até antes do '&'
    last_amp = cut.rfind("&")
    last_semi = cut.rfind(";")
    if last_amp > last_semi and (len(cut) - last_amp) <= 10:
        cut = cut[:last_amp]

    # Calcula tags abertas que precisam ser fechadas
    open_stack: list[str] = []
    for match in _TELEGRAM_TAG_RE.finditer(cut):
        is_closing = bool(match.group(1))
        tag_name = match.group(2).lower()
        if tag_name in _TELEGRAM_HTML_VOID_TAGS:
            continue
        if not is_closing:
            open_stack.append(tag_name)
        elif open_stack and open_stack[-1] == tag_name:
            open_stack.pop()

    closing_suffix = "".join(f"</{tag}>" for tag in reversed(open_stack))
    max_body = max(0, max_len - 1 - len(closing_suffix))
    if len(cut) > max_body:
        cut = cut[:max_body]
        last_open = cut.rfind("<")
        last_close = cut.rfind(">")
        if last_open > last_close:
            cut = cut[:last_open]
        # Recalcula stack após corte adicional
        open_stack = []
        for match in _TELEGRAM_TAG_RE.finditer(cut):
            is_closing = bool(match.group(1))
            tag_name = match.group(2).lower()
            if tag_name in _TELEGRAM_HTML_VOID_TAGS:
                continue
            if not is_closing:
                open_stack.append(tag_name)
            elif open_stack and open_stack[-1] == tag_name:
                open_stack.pop()
        closing_suffix = "".join(f"</{tag}>" for tag in reversed(open_stack))

    return f"{cut.rstrip()}…{closing_suffix}"


def _format_brl(value: float | None) -> str | None:
    if value is None:
        return None
    formatted = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {formatted}"


def render_offer_template(
    template_text: str,
    product: ProductItem,
    platform: str = "telegram",
    cycle_name: str | None = None,
) -> str:
    """Renderiza um template de mensagem substituindo variáveis dinâmicas com escape seguro por plataforma."""
    is_html = platform == "telegram"
    esc = html.escape if is_html else (lambda s: s)

    title_val = esc(product.name or "Oferta Selecionada")
    promo_val = _format_brl(product.promotional_price) or "Consulte"
    orig_val = (
        _format_brl(product.original_price)
        if product.original_price and (product.original_price > (product.promotional_price or 0))
        else ""
    )
    discount_val = f"{product.discount_percentage}% OFF" if product.discount_percentage else ""

    if product.rating is not None:
        reviews_suffix = f" ({product.reviews_count} avaliações)" if product.reviews_count else ""
        rating_val = f"{product.rating}/5.0{reviews_suffix}"
    else:
        rating_val = ""

    seller_val = esc(product.seller) if product.seller else ""
    link_val = esc(product.affiliate_url or product.url or "")
    cycle_val = esc(cycle_name or "OFERTA SELECIONADA")

    replacements = {
        "{titulo}": title_val,
        "{preco}": promo_val,
        "{preco_original}": orig_val,
        "{desconto}": discount_val,
        "{avaliacao}": rating_val,
        "{vendedor}": seller_val,
        "{link_afiliado}": link_val,
        "{ciclo}": cycle_val,
    }

    optional_vars = ("{preco_original}", "{desconto}", "{avaliacao}", "{vendedor}")
    rendered_lines: list[str] = []

    for raw_line in template_text.splitlines():
        # Se a linha contém uma variável opcional que está vazia para este produto, omite a linha
        if any(opt_var in raw_line and not replacements[opt_var] for opt_var in optional_vars):
            continue
        line = raw_line
        for key, val in replacements.items():
            line = line.replace(key, val)
        rendered_lines.append(line)

    rendered = "\n".join(rendered_lines).strip()
    max_len = PLATFORM_MAX_CHARS.get(platform, 1024)
    if len(rendered) > max_len:
        if is_html:
            rendered = _truncate_html_safe(rendered, max_len)
        else:
            rendered = rendered[: max_len - 1] + "…"
    return rendered


class OutputSource(ABC):
    """Interface padrão para todos os canais de distribuição de ofertas."""

    output_type: str
    display_name: str
    supported: bool = True

    @abstractmethod
    def validate_config(self, config: dict[str, Any]) -> tuple[bool, str | None]:
        """Valida os parâmetros de configuração específicos do destino."""

    @abstractmethod
    def format_offer_caption(self, product: ProductItem, cycle_name: str | None = None) -> str:
        """Formata o texto da oferta com título, preço, desconto, avaliação e link afiliado."""

    @abstractmethod
    async def send_offer(
        self,
        product: ProductItem,
        destination: OutputDestinationConfig,
        cycle_name: str | None = None,
    ) -> DeliveryResult:
        """Envia a oferta formatada (incluindo imagem principal quando disponível) para o destino."""

    @abstractmethod
    async def test_connection(self, destination: OutputDestinationConfig) -> DeliveryResult:
        """Realiza um disparo de teste para validar credenciais e permissões do destino."""


class TelegramOutputSource(OutputSource):
    """Destino funcional de publicação via API oficial de Bots do Telegram."""

    output_type = "telegram"
    display_name = "Telegram (Bot / Canal / Grupo)"
    supported = True

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        image_transport: httpx.AsyncBaseTransport | None = None,
        probe_images: bool = True,
        max_inline_retry_after: float = 15.0,
    ) -> None:
        self._transport = transport
        self._image_transport = image_transport
        self._probe_images = probe_images
        self._max_inline_retry_after = max_inline_retry_after
        self._shared_client: httpx.AsyncClient | None = None

    def _get_http_client(self) -> tuple[httpx.AsyncClient, bool]:
        """Retorna cliente HTTP reutilizável (ou isolado quando transport customizado está ativo)."""
        if self._transport is not None:
            return httpx.AsyncClient(timeout=15.0, transport=self._transport), True
        if self._shared_client is None or self._shared_client.is_closed:
            self._shared_client = httpx.AsyncClient(
                timeout=15.0,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._shared_client, False

    async def aclose(self) -> None:
        """Fecha o cliente HTTP compartilhado, se existir."""
        if self._shared_client is not None and not self._shared_client.is_closed:
            await self._shared_client.aclose()
            self._shared_client = None

    @staticmethod
    def _classify_telegram_error(
        status_code: int | None,
        description: str,
        method: str,
    ) -> str:
        """Classifica erro da API do Telegram em 'rate_limit', 'transient' ou 'permanent'."""
        desc_lower = (description or "").lower()
        if status_code == 429 or "too many requests" in desc_lower or "retry after" in desc_lower:
            return "rate_limit"
        if status_code is None or status_code >= 500:
            return "transient"
        if status_code in (401, 403, 404):
            return "permanent"
        if status_code == 400:
            # Erros de mídia/entidade podem ser contornados por fallback; erros de chat/destino são definitivos
            permanent_400_markers = (
                "chat not found",
                "chat_id is empty",
                "user not found",
                "group chat was upgraded",
                "chat_write_forbidden",
                "have no rights to send",
                "bot was blocked",
                "bot was kicked",
                "user is deactivated",
            )
            if any(m in desc_lower for m in permanent_400_markers):
                return "permanent"
            if method == "sendPhoto":
                return "transient"
            return "permanent"
        return "permanent"

    async def _validate_image_url(self, image_url: str | None) -> tuple[bool, str | None]:
        """Valida sintaxe e (quando aplicável) acessibilidade/MIME/tamanho da imagem antes de sendPhoto."""
        ok_syntax, syntax_err = _is_valid_http_url(image_url)
        if not ok_syntax or not image_url:
            return False, syntax_err

        cleaned = image_url.strip()
        lower_path = urlsplit(cleaned).path.lower()
        if lower_path.endswith(".svg") or "blank.gif" in lower_path or "placeholder" in lower_path:
            return False, "Imagem incompatível com sendPhoto (SVG ou placeholder)."

        # Em testes com transport mockado apenas para Telegram (sem _image_transport), evita rede real
        should_probe = self._probe_images and (self._transport is None or self._image_transport is not None)
        if not should_probe:
            return True, None

        try:
            async with httpx.AsyncClient(
                timeout=4.0,
                follow_redirects=True,
                transport=self._image_transport,
            ) as img_client:
                resp = await img_client.head(cleaned)
                if resp.status_code == 405:
                    resp = await img_client.get(cleaned, headers={"Range": "bytes=0-0"})
                if resp.status_code not in (200, 206):
                    return False, f"Imagem inacessível (HTTP {resp.status_code})."
                ctype = (resp.headers.get("content-type") or "").lower().split(";")[0].strip()
                if ctype and (not ctype.startswith("image/") or ctype == "image/svg+xml"):
                    return False, f"Content-Type inválido para imagem: '{ctype}'."
                clen_raw = resp.headers.get("content-length")
                if clen_raw and clen_raw.isdigit() and int(clen_raw) > _MAX_IMAGE_BYTES:
                    return False, f"Imagem excede o limite de 5 MB ({clen_raw} bytes)."
                return True, None
        except httpx.RequestError as exc:
            return False, f"Falha ao validar imagem ({type(exc).__name__}): {exc}"

    def validate_config(self, config: dict[str, Any]) -> tuple[bool, str | None]:
        bot_token = str(config.get("bot_token") or "").strip()
        chat_id = str(config.get("chat_id") or "").strip()
        if not bot_token or ":" not in bot_token:
            return False, "Informe um 'bot_token' válido do BotFather (ex: 123456:ABC-DEF...)."
        if not chat_id:
            return False, "Informe o 'chat_id' do canal/grupo ou usuário (ex: -100123456789 ou @meucanal)."
        return True, None

    def format_offer_caption(
        self,
        product: ProductItem,
        cycle_name: str | None = None,
        template_text: str | None = None,
    ) -> str:
        """Gera a legenda HTML rica para o Telegram aplicando o template configurado."""
        active_template = (
            template_text
            or storage_service.get_platform_template("telegram").template_text
            or DEFAULT_PLATFORM_TEMPLATES["telegram"]
        )
        return render_offer_template(
            template_text=active_template,
            product=product,
            platform="telegram",
            cycle_name=cycle_name,
        )

    async def _post_telegram(
        self,
        api_base: str,
        bot_token: str,
        method: str,
        payload: dict[str, Any],
        max_retries: int = 3,
    ) -> tuple[bool, dict[str, Any], int, str | None, int | None, str | None, float | None]:
        """Executa chamada HTTP para a API do Telegram com backoff exponencial, jitter e tratamento de 429."""
        url = f"{api_base.rstrip('/')}/bot{bot_token}/{method}"
        attempts = 0
        last_error: str | None = None
        last_status: int | None = None
        last_error_type: str | None = None
        last_retry_after: float | None = None

        client, should_close = self._get_http_client()
        try:
            for attempt in range(1, max_retries + 1):
                attempts = attempt
                try:
                    resp = await client.post(url, json=payload)
                    last_status = resp.status_code
                    ctype = resp.headers.get("content-type", "").lower()
                    data = resp.json() if "application/json" in ctype or resp.text.startswith("{") else {}

                    if resp.status_code == 200 and data.get("ok"):
                        return True, data, attempts, None, 200, None, None

                    # Tratamento específico de rate limit (HTTP 429) e retry_after
                    if resp.status_code == 429:
                        raw_ra = data.get("parameters", {}).get("retry_after") or resp.headers.get("Retry-After") or 2
                        try:
                            retry_after = max(1.0, float(raw_ra))
                        except (TypeError, ValueError):
                            retry_after = 2.0
                        last_retry_after = retry_after
                        last_error_type = "rate_limit"
                        last_error = f"Rate limit do Telegram (429). Aguardando {retry_after:g}s."
                        logger.warning("%s (tentativa %d/%d)", last_error, attempt, max_retries)
                        if attempt < max_retries and retry_after <= self._max_inline_retry_after:
                            await asyncio.sleep(retry_after + random.uniform(0.05, 0.25))
                            continue
                        return False, data, attempts, last_error, 429, "rate_limit", retry_after

                    description = str(data.get("description") or f"HTTP {resp.status_code}")
                    last_error = f"Erro Telegram ({method}, HTTP {resp.status_code}): {description}"
                    last_error_type = self._classify_telegram_error(resp.status_code, description, method)

                    # Erros 5xx permitem retentativa com backoff exponencial + jitter
                    if resp.status_code >= 500 and attempt < max_retries:
                        backoff = min(6.0, 0.5 * (2 ** (attempt - 1))) + random.uniform(0.05, 0.25)
                        logger.warning(
                            "Erro temporário %d no Telegram (%s), tentativa %d/%d, aguardando %.2fs.",
                            resp.status_code,
                            method,
                            attempt,
                            max_retries,
                            backoff,
                        )
                        await asyncio.sleep(backoff)
                        continue
                    return False, data, attempts, last_error, resp.status_code, last_error_type, None

                except httpx.RequestError as exc:
                    last_status = None
                    last_error_type = "transient"
                    last_error = f"Falha de rede/timeout ao contatar Telegram ({method}): {exc}"
                    logger.warning("Tentativa %d/%d falhou no Telegram: %s", attempt, max_retries, last_error)
                    if attempt < max_retries:
                        backoff = min(6.0, 0.5 * (2 ** (attempt - 1))) + random.uniform(0.05, 0.25)
                        await asyncio.sleep(backoff)

            return False, {}, attempts, last_error, last_status, last_error_type, last_retry_after
        finally:
            if should_close:
                await client.aclose()

    async def send_offer(
        self,
        product: ProductItem,
        destination: OutputDestinationConfig,
        cycle_name: str | None = None,
    ) -> DeliveryResult:
        """Envia oferta completa com validação de imagem/URL, classificação de erros e fallback seguro."""
        valid, err = self.validate_config(destination.config)
        target_link = (product.affiliate_url or product.url or "").strip()

        if not valid:
            logger.error("Configuração inválida para destino Telegram '%s': %s", destination.name, err)
            return DeliveryResult(
                success=False,
                output_id=destination.id or "unknown",
                output_type=self.output_type,
                destination_name=destination.name,
                product_id=product.id,
                product_name=product.name,
                affiliate_url=target_link,
                error=err,
                error_type="config",
                attempts=0,
            )

        bot_token = str(destination.config["bot_token"]).strip()
        chat_id = str(destination.config["chat_id"]).strip()
        api_base = str(destination.config.get("api_base_url") or "https://api.telegram.org").strip()

        caption = self.format_offer_caption(
            product,
            cycle_name=cycle_name,
            template_text=destination.custom_template,
        )
        link_valid, link_err = _is_valid_http_url(target_link)
        if not link_valid and target_link:
            logger.warning(
                "URL de destino inválida para inline_keyboard (%s) | prod_id=%s | motivo=%s",
                target_link,
                product.id,
                link_err,
            )
        reply_markup = (
            {
                "inline_keyboard": [
                    [{"text": "🛒 Ver Oferta no Mercado Livre", "url": target_link}]
                ]
            }
            if link_valid
            else None
        )

        total_attempts = 0
        fallback_used = False

        # 1. Valida imagem e tenta enviar com foto (sendPhoto) quando válida
        if product.image_url:
            img_ok, img_err = await self._validate_image_url(product.image_url)
            if not img_ok:
                fallback_used = True
                logger.warning(
                    "Imagem pré-validada como inválida/inacessível para '%s' (%s). Pulando sendPhoto para sendMessage.",
                    product.id,
                    img_err,
                )
            else:
                photo_payload: dict[str, Any] = {
                    "chat_id": chat_id,
                    "photo": product.image_url.strip(),
                    "caption": caption,
                    "parse_mode": "HTML",
                }
                if reply_markup:
                    photo_payload["reply_markup"] = reply_markup

                ok, data, attempts, err_msg, status_code, error_type, retry_after = await self._post_telegram(
                    api_base, bot_token, "sendPhoto", photo_payload
                )
                total_attempts += attempts
                if ok:
                    msg_id = str(data.get("result", {}).get("message_id", ""))
                    logger.info(
                        "Oferta enviada ao Telegram (com foto) | destino='%s' | prod_id=%s | msg_id=%s",
                        destination.name,
                        product.id,
                        msg_id,
                    )
                    return DeliveryResult(
                        success=True,
                        output_id=destination.id or "telegram",
                        output_type=self.output_type,
                        destination_name=destination.name,
                        product_id=product.id,
                        product_name=product.name,
                        affiliate_url=target_link,
                        external_message_id=msg_id,
                        status_code=status_code,
                        delivery_method="sendPhoto",
                        attempts=total_attempts,
                    )

                # Erros definitivos de destino/token (401, 403, 404, chat not found) não devem tentar fallback
                if error_type == "permanent":
                    logger.error(
                        "Erro definitivo no sendPhoto para '%s' (%s). Abortando fallback.",
                        product.id,
                        err_msg,
                    )
                    return DeliveryResult(
                        success=False,
                        output_id=destination.id or "telegram",
                        output_type=self.output_type,
                        destination_name=destination.name,
                        product_id=product.id,
                        product_name=product.name,
                        affiliate_url=target_link,
                        error=err_msg,
                        status_code=status_code,
                        error_type=error_type,
                        retry_after_seconds=retry_after,
                        delivery_method="sendPhoto",
                        attempts=total_attempts,
                    )

                fallback_used = True
                logger.warning(
                    "Falha no sendPhoto para '%s' (%s). Realizando fallback para sendMessage.",
                    product.id,
                    err_msg,
                )

        # 2. Envia como mensagem de texto (sendMessage) se não houver foto ou em caso de fallback
        text_payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": caption,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        if reply_markup:
            text_payload["reply_markup"] = reply_markup

        ok, data, attempts, err_msg, status_code, error_type, retry_after = await self._post_telegram(
            api_base, bot_token, "sendMessage", text_payload
        )
        total_attempts += attempts

        # Se falhou por erro de parsing HTML (entities), tenta enviar em texto puro
        if not ok and status_code == 400 and err_msg and "parse entities" in err_msg.lower():
            plain_payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": _strip_html_tags(caption),
                "disable_web_page_preview": False,
            }
            if reply_markup:
                plain_payload["reply_markup"] = reply_markup
            fallback_used = True
            ok, data, extra_attempts, err_msg, status_code, error_type, retry_after = await self._post_telegram(
                api_base, bot_token, "sendMessage", plain_payload, max_retries=1
            )
            total_attempts += extra_attempts

        if ok:
            msg_id = str(data.get("result", {}).get("message_id", ""))
            logger.info(
                "Oferta enviada ao Telegram (texto) | destino='%s' | prod_id=%s | msg_id=%s | fallback=%s",
                destination.name,
                product.id,
                msg_id,
                fallback_used,
            )
            return DeliveryResult(
                success=True,
                output_id=destination.id or "telegram",
                output_type=self.output_type,
                destination_name=destination.name,
                product_id=product.id,
                product_name=product.name,
                affiliate_url=target_link,
                external_message_id=msg_id,
                status_code=status_code,
                fallback_used=fallback_used,
                delivery_method="sendMessage",
                attempts=total_attempts,
            )

        logger.error(
            "Falha ao enviar oferta ao Telegram | destino='%s' | prod_id=%s | tipo=%s | http=%s | erro=%s",
            destination.name,
            product.id,
            error_type,
            status_code,
            err_msg,
        )
        return DeliveryResult(
            success=False,
            output_id=destination.id or "telegram",
            output_type=self.output_type,
            destination_name=destination.name,
            product_id=product.id,
            product_name=product.name,
            affiliate_url=target_link,
            error=err_msg,
            status_code=status_code,
            error_type=error_type,  # type: ignore[arg-type]
            retry_after_seconds=retry_after,
            fallback_used=fallback_used,
            delivery_method="sendMessage",
            attempts=total_attempts,
        )

    async def test_connection(self, destination: OutputDestinationConfig) -> DeliveryResult:
        """Envia uma mensagem de verificação para validar o bot_token e o chat_id configurados."""
        sample_product = ProductItem(
            id="MLB000000000",
            name="Mensagem de Teste — Gwibot Affiliate Hub",
            url="https://www.mercadolivre.com.br",
            affiliate_url="https://www.mercadolivre.com.br?matt_tool=teste&matt_word=gwibot",
            affiliate_status="generated",
            original_price=199.90,
            promotional_price=149.90,
            discount_percentage=25,
            rating=4.9,
            reviews_count=120,
            seller="Gwibot Test",
            image_url=None,
        )
        return await self.send_offer(sample_product, destination, cycle_name="Teste de Conexão")


class WhatsAppOutputSource(OutputSource):
    """Integração preparada para distribuição futura de ofertas via WhatsApp (Cloud API / Evolution API)."""

    output_type = "whatsapp"
    display_name = "WhatsApp (Integração Futura)"
    supported = False

    def validate_config(self, config: dict[str, Any]) -> tuple[bool, str | None]:
        return True, None

    def format_offer_caption(
        self,
        product: ProductItem,
        cycle_name: str | None = None,
        template_text: str | None = None,
    ) -> str:
        active_template = (
            template_text
            or storage_service.get_platform_template("whatsapp").template_text
            or DEFAULT_PLATFORM_TEMPLATES["whatsapp"]
        )
        return render_offer_template(
            template_text=active_template,
            product=product,
            platform="whatsapp",
            cycle_name=cycle_name,
        )

    async def send_offer(
        self,
        product: ProductItem,
        destination: OutputDestinationConfig,
        cycle_name: str | None = None,
    ) -> DeliveryResult:
        return DeliveryResult(
            success=False,
            output_id=destination.id or "whatsapp",
            output_type=self.output_type,
            destination_name=destination.name,
            product_id=product.id,
            product_name=product.name,
            affiliate_url=product.affiliate_url,
            error="O conector WhatsApp está preparado como integração futura. Utilize um destino Telegram ativo.",
        )

    async def test_connection(self, destination: OutputDestinationConfig) -> DeliveryResult:
        return await self.send_offer(ProductItem(name="Teste WhatsApp"), destination)


class OutputRegistry:
    """Registro central de canais de saída (Output Sources)."""

    def __init__(self) -> None:
        self._outputs: dict[str, OutputSource] = {}
        self.register(TelegramOutputSource())
        self.register(WhatsAppOutputSource())

    def register(self, output: OutputSource) -> None:
        self._outputs[output.output_type] = output

    def get(self, output_type: str) -> OutputSource:
        if output_type not in self._outputs:
            raise KeyError(f"Tipo de destino não registrado: {output_type}")
        return self._outputs[output_type]

    def list_output_types(self) -> list[dict[str, Any]]:
        return [
            {
                "output_type": o.output_type,
                "display_name": o.display_name,
                "supported": o.supported,
            }
            for o in self._outputs.values()
        ]


output_registry = OutputRegistry()
