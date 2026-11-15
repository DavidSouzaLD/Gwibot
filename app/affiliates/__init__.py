"""Provedores modulares de geração de links de afiliados (Mercado Livre, Shopee, Amazon)."""
from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
import json
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from app.config import settings
from app.logger import logger
from app.models import (
    AffiliateConfigModel,
    AffiliateConvertResponse,
    ProductItem,
)
from app.session_manager import session_manager
from app.storage import storage_service


class AffiliateProvider(ABC):
    """Interface base para provedores de links de afiliados."""

    provider_id: str
    display_name: str
    description: str = ""
    supported: bool = True
    fields_schema: list[dict[str, Any]] = []

    @abstractmethod
    async def convert_url(
        self,
        product_url: str,
        product_id: str | None = None,
        config: AffiliateConfigModel | None = None,
    ) -> AffiliateConvertResponse:
        """Converte a URL original de um produto em link de afiliado oficial."""

    async def enrich_products(
        self,
        products: list[ProductItem],
        config: AffiliateConfigModel | None = None,
    ) -> tuple[list[ProductItem], int, int]:
        """Converte URLs de uma lista de produtos em paralelo preservando ID, preço e demais campos.

        Retorna `(produtos_enriquecidos, qtd_sucesso, qtd_falha)`.
        """
        if not products:
            return products, 0, 0

        cfg = config or storage_service.get_affiliate_config(self.provider_id)
        converted_ok = 0
        converted_fail = 0
        sem = asyncio.Semaphore(8)

        async def _enrich_single(item: ProductItem) -> tuple[int, int]:
            if not item.url:
                item.affiliate_url = None
                item.affiliate_status = "failed"
                item.affiliate_error = "Produto sem URL original disponível."
                logger.warning(
                    "Falha na geração de link de afiliado: produto '%s' (id=%s) não possui URL.",
                    item.name,
                    item.id,
                )
                return 0, 1

            # Evita reconverter se já foi enriquecido com sucesso na mesma instância
            if item.affiliate_status == "generated" and item.affiliate_url:
                return 1, 0

            async with sem:
                res = await self.convert_url(item.url, product_id=item.id, config=cfg)
            item.affiliate_url = res.affiliate_url
            item.affiliate_status = res.status
            item.affiliate_error = res.error

            if res.status == "generated" and res.affiliate_url:
                return 1, 0
            if res.status == "failed":
                return 0, 1
            return 0, 0

        results = await asyncio.gather(*(_enrich_single(p) for p in products))
        for ok, fail in results:
            converted_ok += ok
            converted_fail += fail

        return products, converted_ok, converted_fail


class MercadoLivreAffiliateProvider(AffiliateProvider):
    """Provedor oficial de links de afiliados do Mercado Livre Brasil.

    Utiliza os parâmetros oficiais do Programa de Afiliados (`matt_word` e `matt_tool`)
    e/ou o endpoint autenticado do Linkbuilder do Mercado Livre quando habilitado.
    Não simula nem inventa links curtos inválidos.
    """

    provider_id = "mercadolivre"
    display_name = "Mercado Livre Afiliados"
    description = "Programa Oficial de Afiliados do Mercado Livre Brasil (parâmetros matt_word, matt_tool e Linkbuilder)."
    supported = True
    fields_schema = [
        {
            "key": "matt_word",
            "label": "Etiqueta Oficial (matt_word)",
            "type": "text",
            "placeholder": "Ex.: meucanal2026",
            "required": True,
        },
        {
            "key": "matt_tool",
            "label": "ID da Ferramenta (matt_tool numérico)",
            "type": "text",
            "placeholder": "Ex.: 84729103",
            "required": True,
        },
        {
            "key": "use_authenticated_linkbuilder",
            "label": "Tentar Linkbuilder oficial autenticado quando a sessão estiver conectada",
            "type": "checkbox",
            "required": False,
        },
    ]

    VALID_ML_DOMAINS = (
        "mercadolivre.com.br",
        "www.mercadolivre.com.br",
        "produto.mercadolivre.com.br",
        "lista.mercadolivre.com.br",
    )

    _conversion_cache: dict[tuple[str, str, str, bool], tuple[float, AffiliateConvertResponse]] = {}
    _cookie_cache: tuple[float, dict[str, str]] | None = None
    _CACHE_TTL_SEC = 600.0
    _COOKIE_TTL_SEC = 30.0

    @classmethod
    def _is_valid_ml_product_url(cls, url: str) -> tuple[bool, str | None]:
        """Valida se a URL pertence a um domínio elegível de produto do Mercado Livre."""
        if not url or not isinstance(url, str):
            return False, "URL vazia ou inválida."
        try:
            parts = urlsplit(url.strip())
        except Exception:
            return False, "Formato de URL malformado."

        if parts.scheme not in ("http", "https"):
            return False, "Esquema da URL deve ser http ou https."

        host = parts.netloc.lower()
        if "click1.mercadolivre" in host:
            return (
                False,
                "URL de redirecionamento de anúncio patrocinado (click1) não suporta conversão direta sem destino final.",
            )

        if not any(host == d or host.endswith("." + d) for d in cls.VALID_ML_DOMAINS):
            return False, f"Domínio '{host}' não pertence ao catálogo do Mercado Livre Brasil."

        if not parts.path or parts.path == "/":
            return False, "A URL aponta para a página inicial e não para um produto específico."

        return True, None

    @classmethod
    def _load_cached_cookie_jar(cls) -> dict[str, str] | None:
        now = time.monotonic()
        if cls._cookie_cache is not None and (now - cls._cookie_cache[0]) < cls._COOKIE_TTL_SEC:
            return cls._cookie_cache[1]

        storage_path = session_manager.get_storage_state_path()
        if not storage_path or not storage_path.exists():
            cls._cookie_cache = None
            return None

        try:
            raw_state = json.loads(storage_path.read_text(encoding="utf-8"))
            cookies_list: list[dict[str, Any]] = raw_state.get("cookies", [])
            cookie_jar = {
                c["name"]: c["value"]
                for c in cookies_list
                if "mercadolivre" in str(c.get("domain", "")) and c.get("name") and c.get("value")
            }
            if "ssid" not in cookie_jar:
                cls._cookie_cache = None
                return None
            cls._cookie_cache = (now, cookie_jar)
            return cookie_jar
        except Exception:
            cls._cookie_cache = None
            return None

    @classmethod
    async def _try_authenticated_linkbuilder(cls, product_url: str, matt_word: str) -> str | None:
        """Tenta consultar a API oficial do Linkbuilder do Mercado Livre usando a sessão autenticada."""
        cookie_jar = cls._load_cached_cookie_jar()
        if not cookie_jar:
            return None

        try:
            csrf_token = cookie_jar.get("_csrf", "")
            headers = {
                "User-Agent": settings.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": settings.ml_base_url,
                "Referer": f"{settings.ml_base_url}/afiliados/linkbuilder",
            }
            if csrf_token:
                headers["x-csrf-token"] = csrf_token

            payload = {
                "urls": [product_url],
                "tag": matt_word,
            }

            async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
                resp = await client.post(
                    settings.ml_affiliate_stripe_api_url,
                    json=payload,
                    cookies=cookie_jar,
                    headers=headers,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    links = data.get("links") or data.get("urls") or []
                    if isinstance(links, list) and links:
                        short_url = links[0].get("short_url") or links[0].get("affiliate_url")
                        if short_url and str(short_url).startswith("https://"):
                            return str(short_url)
        except Exception as exc:
            logger.debug("Linkbuilder autenticado não retornou shortlink direto: %s", exc)
        return None

    async def convert_url(
        self,
        product_url: str,
        product_id: str | None = None,
        config: AffiliateConfigModel | None = None,
    ) -> AffiliateConvertResponse:
        """Converte a URL do produto em link oficial de afiliado do Mercado Livre."""
        cfg = config or storage_service.get_affiliate_config(self.provider_id)

        is_valid, err_reason = self._is_valid_ml_product_url(product_url)
        if not is_valid:
            logger.warning(
                "Falha ao gerar link de afiliado ML | id=%s | url=%s | motivo=%s",
                product_id,
                product_url,
                err_reason,
            )
            return AffiliateConvertResponse(
                original_url=product_url,
                affiliate_url=None,
                status="failed",
                error=err_reason,
                provider_id=self.provider_id,
            )

        if not cfg.enabled:
            return AffiliateConvertResponse(
                original_url=product_url,
                affiliate_url=None,
                status="not_configured",
                error="Conversão de afiliados do Mercado Livre está desativada nas configurações.",
                provider_id=self.provider_id,
            )

        matt_word = (cfg.matt_word or "").strip()
        matt_tool = (cfg.matt_tool or "").strip()

        if not matt_word or not matt_tool:
            logger.info(
                "Conversão de afiliado ignorada (não configurado) | id=%s | Configure 'matt_word' e 'matt_tool'.",
                product_id,
            )
            return AffiliateConvertResponse(
                original_url=product_url,
                affiliate_url=None,
                status="not_configured",
                error="Credenciais oficiais de afiliado (matt_word e matt_tool) não configuradas.",
                provider_id=self.provider_id,
            )

        if not matt_tool.isdigit():
            err_msg = "O parâmetro 'matt_tool' deve conter o código numérico oficial do afiliado."
            logger.warning("Falha na geração de link de afiliado ML | id=%s | %s", product_id, err_msg)
            return AffiliateConvertResponse(
                original_url=product_url,
                affiliate_url=None,
                status="failed",
                error=err_msg,
                provider_id=self.provider_id,
            )

        cache_key = (product_url.strip(), matt_word, matt_tool, bool(cfg.use_authenticated_linkbuilder))
        now = time.monotonic()
        cached_entry = self._conversion_cache.get(cache_key)
        if cached_entry is not None and (now - cached_entry[0]) < self._CACHE_TTL_SEC:
            return cached_entry[1]

        # 1. Se habilitado, tenta obter o link oficial via endpoint autenticado do Linkbuilder
        if cfg.use_authenticated_linkbuilder:
            short_link = await self._try_authenticated_linkbuilder(product_url, matt_word)
            if short_link:
                logger.info("Link de afiliado gerado via Linkbuilder | id=%s | url=%s", product_id, short_link)
                res_lb = AffiliateConvertResponse(
                    original_url=product_url,
                    affiliate_url=short_link,
                    status="generated",
                    error=None,
                    provider_id=self.provider_id,
                )
                self._conversion_cache[cache_key] = (now, res_lb)
                return res_lb

        # 2. Aplica os parâmetros oficiais de rastreamento do Programa de Afiliados do Mercado Livre
        try:
            parts = urlsplit(product_url.strip())
            query_params = dict(parse_qsl(parts.query, keep_blank_values=True))
            query_params["matt_tool"] = matt_tool
            query_params["matt_word"] = matt_word

            new_query = urlencode(query_params)
            affiliate_url = urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, ""))

            logger.info(
                "Link de afiliado oficial gerado | id=%s | matt_word='%s' | matt_tool='%s'",
                product_id,
                matt_word,
                matt_tool,
            )
            res_ok = AffiliateConvertResponse(
                original_url=product_url,
                affiliate_url=affiliate_url,
                status="generated",
                error=None,
                provider_id=self.provider_id,
            )
            if len(self._conversion_cache) > 2000:
                self._conversion_cache.clear()
            self._conversion_cache[cache_key] = (now, res_ok)
            return res_ok
        except Exception as exc:
            logger.error("Erro ao construir URL de afiliado ML para %s: %s", product_url, exc)
            return AffiliateConvertResponse(
                original_url=product_url,
                affiliate_url=None,
                status="failed",
                error=f"Erro ao montar parâmetros de afiliado: {exc}",
                provider_id=self.provider_id,
            )


class FutureAffiliateProviderStub(AffiliateProvider):
    """Adaptador preparado para integração futura (Shopee / Amazon Associates)."""

    supported = False

    def __init__(
        self,
        provider_id: str,
        display_name: str,
        description: str = "",
        fields_schema: list[dict[str, Any]] | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.display_name = display_name
        self.description = description
        self.fields_schema = fields_schema or []

    async def convert_url(
        self,
        product_url: str,
        product_id: str | None = None,
        config: AffiliateConfigModel | None = None,
    ) -> AffiliateConvertResponse:
        return AffiliateConvertResponse(
            original_url=product_url,
            affiliate_url=None,
            status="not_configured",
            error=f"Provedor '{self.display_name}' preparado para integração futura.",
            provider_id=self.provider_id,
        )


class AffiliateRegistry:
    """Registro central de provedores de afiliados."""

    def __init__(self) -> None:
        self._providers: dict[str, AffiliateProvider] = {}
        self.register(MercadoLivreAffiliateProvider())
        self.register(
            FutureAffiliateProviderStub(
                "shopee",
                "Shopee Afiliados",
                description="Integração via Shopee Open Platform GraphQL (App ID & Secret).",
                fields_schema=[
                    {"key": "app_id", "label": "Shopee App ID", "type": "text", "placeholder": "Ex.: 183920192", "required": True},
                    {"key": "app_secret", "label": "Shopee App Secret", "type": "password", "placeholder": "Chave secreta da API", "required": True},
                    {"key": "sub_id", "label": "Sub ID padrão (opcional)", "type": "text", "placeholder": "Ex.: gwibot_telegram", "required": False},
                ],
            )
        )
        self.register(
            FutureAffiliateProviderStub(
                "amazon",
                "Amazon Associates BR",
                description="Programa de Associados da Amazon Brasil (Partner Tag / PA-API 5.0).",
                fields_schema=[
                    {"key": "partner_tag", "label": "Store / Partner Tag", "type": "text", "placeholder": "Ex.: meucanal-20", "required": True},
                    {"key": "access_key", "label": "PA-API Access Key (opcional)", "type": "text", "placeholder": "AKIA...", "required": False},
                    {"key": "secret_key", "label": "PA-API Secret Key (opcional)", "type": "password", "placeholder": "Chave secreta", "required": False},
                ],
            )
        )

    def register(self, provider: AffiliateProvider) -> None:
        self._providers[provider.provider_id] = provider

    def get(self, provider_id: str) -> AffiliateProvider:
        if provider_id not in self._providers:
            raise KeyError(f"Provedor de afiliado desconhecido: {provider_id}")
        return self._providers[provider_id]

    def list_providers(self) -> list[dict[str, Any]]:
        saved_map = storage_service.list_affiliate_configs()
        items: list[dict[str, Any]] = []
        for p in self._providers.values():
            cfg = saved_map.get(p.provider_id) or AffiliateConfigModel(provider_id=p.provider_id)
            items.append(
                {
                    "provider_id": p.provider_id,
                    "display_name": p.display_name,
                    "description": p.description,
                    "supported": p.supported,
                    "fields_schema": p.fields_schema,
                    "config": cfg.model_dump(),
                }
            )
        return items


affiliate_registry = AffiliateRegistry()
ml_affiliate_provider = affiliate_registry.get("mercadolivre")
