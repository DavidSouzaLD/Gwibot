"""Arquitetura modular de destinos de saída (OutputSource: Telegram funcional e WhatsApp preparado)."""
from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
import html
from typing import Any

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

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

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
    ) -> tuple[bool, dict[str, Any], int, str | None]:
        """Executa chamada HTTP para a API do Telegram com controle de retentativas e backoff."""
        url = f"{api_base.rstrip('/')}/bot{bot_token}/{method}"
        attempts = 0
        last_error: str | None = None

        async with httpx.AsyncClient(timeout=15.0, transport=self._transport) as client:
            for attempt in range(1, max_retries + 1):
                attempts = attempt
                try:
                    resp = await client.post(url, json=payload)
                    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}

                    if resp.status_code == 200 and data.get("ok"):
                        return True, data, attempts, None

                    # Tratamento de rate limit (HTTP 429)
                    if resp.status_code == 429:
                        retry_after = int(data.get("parameters", {}).get("retry_after", 2))
                        last_error = f"Rate limit do Telegram (429). Aguardando {retry_after}s."
                        logger.warning(last_error)
                        if attempt < max_retries:
                            await asyncio.sleep(min(retry_after, 5))
                            continue

                    description = data.get("description") or f"HTTP {resp.status_code}"
                    last_error = f"Erro Telegram ({method}): {description}"

                    # Erros 5xx permitem retentativa; erros 4xx (exceto 429) retornam imediatamente
                    if resp.status_code >= 500 and attempt < max_retries:
                        await asyncio.sleep(0.8 * attempt)
                        continue
                    return False, data, attempts, last_error

                except httpx.RequestError as exc:
                    last_error = f"Falha de rede ao contatar Telegram ({method}): {exc}"
                    logger.warning("Tentativa %d/%d falhou no Telegram: %s", attempt, max_retries, last_error)
                    if attempt < max_retries:
                        await asyncio.sleep(0.8 * attempt)

        return False, {}, attempts, last_error

    async def send_offer(
        self,
        product: ProductItem,
        destination: OutputDestinationConfig,
        cycle_name: str | None = None,
    ) -> DeliveryResult:
        """Envia oferta completa (imagem, título, preço, desconto, avaliação e link afiliado) ao Telegram."""
        valid, err = self.validate_config(destination.config)
        target_link = product.affiliate_url or product.url or ""

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
            )

        bot_token = str(destination.config["bot_token"]).strip()
        chat_id = str(destination.config["chat_id"]).strip()
        api_base = str(destination.config.get("api_base_url") or "https://api.telegram.org").strip()

        caption = self.format_offer_caption(
            product,
            cycle_name=cycle_name,
            template_text=destination.custom_template,
        )
        reply_markup = (
            {
                "inline_keyboard": [
                    [{"text": "🛒 Ver Oferta no Mercado Livre", "url": target_link}]
                ]
            }
            if target_link.startswith(("http://", "https://"))
            else None
        )

        total_attempts = 0
        # 1. Tenta enviar com imagem principal (sendPhoto) se disponível
        if product.image_url and product.image_url.startswith(("http://", "https://")):
            photo_payload: dict[str, Any] = {
                "chat_id": chat_id,
                "photo": product.image_url,
                "caption": caption,
                "parse_mode": "HTML",
            }
            if reply_markup:
                photo_payload["reply_markup"] = reply_markup

            ok, data, attempts, err_msg = await self._post_telegram(
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
                    attempts=total_attempts,
                )
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

        ok, data, attempts, err_msg = await self._post_telegram(
            api_base, bot_token, "sendMessage", text_payload
        )
        total_attempts += attempts

        if ok:
            msg_id = str(data.get("result", {}).get("message_id", ""))
            logger.info(
                "Oferta enviada ao Telegram (texto) | destino='%s' | prod_id=%s | msg_id=%s",
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
                attempts=total_attempts,
            )

        logger.error(
            "Falha definitiva ao enviar oferta ao Telegram | destino='%s' | prod_id=%s | erro=%s",
            destination.name,
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
