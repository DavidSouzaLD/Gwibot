"""Abstração modular de fontes de pesquisa de produtos (Mercado Livre, Shopee, Amazon)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.models import SearchMetadata, SearchResponse
from app.parser import parse_keywords
from app.scraper import scraper_service


class SearchSource(ABC):
    """Interface padrão para fontes de coleta de ofertas."""

    source_id: str
    display_name: str
    supported: bool = True

    @abstractmethod
    async def search(
        self,
        query: str,
        limit: int,
        page_number: int = 1,
        enrich_affiliates: bool = True,
    ) -> SearchResponse:
        """Executa a busca de produtos na fonte especificada."""

    async def search_keywords_batch(
        self,
        keywords: list[str],
        limit_per_keyword: int,
        page_number: int = 1,
        enrich_affiliates: bool = False,
    ) -> list[SearchResponse]:
        """Executa busca em lote para múltiplas palavras-chave (reutilizando sessão quando suportado)."""
        results: list[SearchResponse] = []
        for kw in keywords:
            res = await self.search(
                query=kw,
                limit=limit_per_keyword,
                page_number=page_number,
                enrich_affiliates=enrich_affiliates,
            )
            results.append(res)
            if res.status == "blocked":
                break
        return results


class MercadoLivreSearchSource(SearchSource):
    """Fonte de pesquisa ativa para o Mercado Livre Brasil via Playwright."""

    source_id = "mercadolivre"
    display_name = "Mercado Livre Brasil"
    supported = True

    async def search(
        self,
        query: str,
        limit: int,
        page_number: int = 1,
        enrich_affiliates: bool = True,
    ) -> SearchResponse:
        return await scraper_service.search_products(
            query=query,
            limit=limit,
            page_number=page_number,
            enrich_affiliates=enrich_affiliates,
        )

    async def search_keywords_batch(
        self,
        keywords: list[str],
        limit_per_keyword: int,
        page_number: int = 1,
        enrich_affiliates: bool = False,
    ) -> list[SearchResponse]:
        clean_kws = [k.strip() for k in keywords if k and k.strip()] or parse_keywords(",".join(keywords))
        return await scraper_service.search_keywords_batch(
            keywords=clean_kws,
            limit_per_keyword=limit_per_keyword,
            page_number=page_number,
            enrich_affiliates=enrich_affiliates,
        )


class FutureSearchSourceStub(SearchSource):
    """Adaptador preparado para futura integração com Shopee e Amazon sem alterar o núcleo."""

    supported = False

    def __init__(self, source_id: str, display_name: str) -> None:
        self.source_id = source_id
        self.display_name = display_name

    async def search(
        self,
        query: str,
        limit: int,
        page_number: int = 1,
        enrich_affiliates: bool = True,
    ) -> SearchResponse:
        return SearchResponse(
            status="partial",
            metadata=SearchMetadata(
                query=query,
                requested_limit=limit,
                total_returned=0,
                pages_visited=0,
                elapsed_seconds=0.0,
                warning=f"A fonte '{self.display_name}' está preparada na arquitetura para integração futura.",
            ),
            results=[],
        )


class SourceRegistry:
    """Registro central de fontes de pesquisa."""

    def __init__(self) -> None:
        self._sources: dict[str, SearchSource] = {}
        self.register(MercadoLivreSearchSource())
        self.register(FutureSearchSourceStub("shopee", "Shopee Brasil (Futuro)"))
        self.register(FutureSearchSourceStub("amazon", "Amazon Brasil (Futuro)"))

    def register(self, source: SearchSource) -> None:
        self._sources[source.source_id] = source

    def get(self, source_id: str) -> SearchSource:
        if source_id not in self._sources:
            raise KeyError(f"Fonte de pesquisa não registrada: {source_id}")
        return self._sources[source_id]

    def list_sources(self) -> list[dict[str, Any]]:
        return [
            {
                "source_id": s.source_id,
                "display_name": s.display_name,
                "supported": s.supported,
            }
            for s in self._sources.values()
        ]


source_registry = SourceRegistry()
