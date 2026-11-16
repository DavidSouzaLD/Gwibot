"""Módulo de automação e coleta no Mercado Livre via Playwright (Chromium isolado e otimizado)."""
from __future__ import annotations

import asyncio
import copy
import re
import time
from typing import Any
from urllib.parse import quote_plus

from playwright.sync_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Route,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from app.config import settings
from app.logger import logger
from app.models import ProductItem, SearchMetadata, SearchResponse
from app.parser import build_product_item, parse_keywords
from app.session_manager import session_manager

# Detecção em lote (single-IPC) de formulários de login/verificação e desafios CAPTCHA reais.
# IMPORTANTE: Ignora páginas que já possuem cards de produto renderizados e ignora o badge invisível
# do Google reCAPTCHA v3/Enterprise (size=invisible / .grecaptcha-badge) usado pelo ML em páginas normais.
DETECT_CHALLENGE_JS = """
() => {
    const hasProductCards = Boolean(
        document.querySelector('li.ui-search-layout__item, div.poly-card, div.ui-search-result__wrapper, div.ui-search-rescue')
    );
    if (hasProductCards) {
        return { blocked: false, isAuth: false };
    }

    if (document.querySelector('form[action*="account-verification"], form[action*="login"]')) {
        return { blocked: true, isAuth: true };
    }

    const bodyText = (document.body && document.body.innerText ? document.body.innerText : '').toLowerCase();
    if (
        bodyText.includes('account-verification') ||
        bodyText.includes('para continuar, acesse sua conta') ||
        bodyText.includes(' Digite seu e-mail ou telefone para iniciar sessão')
    ) {
        return { blocked: true, isAuth: true };
    }

    const challengeNodes = Array.from(
        document.querySelectorAll('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="turnstile"], #g-recaptcha, #captcha')
    );

    for (const el of challengeNodes) {
        const src = (el.getAttribute('src') || '').toLowerCase();
        // Ignora reCAPTCHA v3 / Enterprise invisível em background
        if (src.includes('size=invisible') || el.closest('.grecaptcha-badge')) {
            continue;
        }
        const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : { width: 100, height: 100 };
        const style = window.getComputedStyle ? window.getComputedStyle(el) : { display: 'block', visibility: 'visible', opacity: '1' };
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
            continue;
        }
        if (rect.width > 10 && rect.height > 10) {
            return { blocked: true, isAuth: false };
        }
    }

    return { blocked: false, isAuth: false };
}
"""

# Rolagem inteligente: só percorre a página se houver cards sem URL de imagem carregada
SMART_SCROLL_JS = """
async () => {
    const imgs = Array.from(
        document.querySelectorAll('img.poly-component__picture, img.ui-search-result-image__element, li.ui-search-layout__item img, div.poly-card img')
    );
    const needsLazyHydration = imgs.some((img) => {
        const src = img.getAttribute('src') || '';
        const dataSrc = img.getAttribute('data-src') || '';
        return (!src || src.startsWith('data:image')) && !dataSrc;
    });
    if (!needsLazyHydration) {
        return 0;
    }
    const maxSteps = 4;
    const stepDistance = Math.max(800, Math.floor(window.innerHeight * 1.1));
    let steps = 0;
    while (window.scrollY + window.innerHeight < document.body.scrollHeight && steps < maxSteps) {
        window.scrollBy(0, stepDistance);
        steps++;
        await new Promise((r) => setTimeout(r, 15));
    }
    window.scrollTo(0, 0);
    return steps;
}
"""

# Extração em lote da URL de próxima página em uma única chamada JS
NEXT_PAGE_URL_JS = """
() => {
    const selectors = [
        'li.andes-pagination__button--next:not(.andes-pagination__button--disabled) a.andes-pagination__link',
        'a.andes-pagination__link[title="Seguinte"]',
        'a.andes-pagination__link[aria-label*="Seguinte"]'
    ];
    for (const sel of selectors) {
        const el = document.querySelector(sel);
        if (el) {
            const href = el.getAttribute('href') || el.href || '';
            if (href && (href.startsWith('http://') || href.startsWith('https://')) && !href.includes('javascript:')) {
                return href;
            }
        }
    }
    return null;
}
"""

# Script JS executado no contexto da página para extrair todos os cards visíveis com precisão
EXTRACT_CARDS_JS = """
() => {
    const cardSelectors = [
        'li.ui-search-layout__item',
        'div.ui-search-result__wrapper',
        'div.poly-card'
    ];

    let nodes = [];
    for (const sel of cardSelectors) {
        const found = Array.from(document.querySelectorAll(sel));
        if (found.length > 0) {
            nodes = found;
            break;
        }
    }

    const getText = (root, selectors) => {
        for (const s of selectors) {
            const el = root.querySelector(s);
            if (el && el.textContent && el.textContent.trim()) {
                return el.textContent.trim();
            }
        }
        return null;
    };

    const getAttr = (root, selectors, attr) => {
        for (const s of selectors) {
            const el = root.querySelector(s);
            if (el) {
                const val = el.getAttribute(attr);
                if (val && val.trim()) return val.trim();
            }
        }
        return null;
    };

    return nodes.map((card) => {
        // Link e título
        const linkEl = card.querySelector(
            'a.poly-component__title, h2.poly-component__title-wrapper a, a.ui-search-item__group__element.ui-search-link, a.ui-search-link__title-card, a.ui-search-link'
        );
        const name = getText(card, [
            'a.poly-component__title',
            'h2.poly-component__title',
            'h2.ui-search-item__title',
            '.poly-component__title'
        ]) || (linkEl ? linkEl.textContent.trim() : null);

        const url = linkEl ? linkEl.href : null;

        // Preço atual / promocional
        const currentPriceContainer = card.querySelector(
            '.poly-price__current, .ui-search-price__second-line, .poly-component__price'
        );
        let promoFraction = null;
        let promoCents = null;

        if (currentPriceContainer) {
            const amountEl = currentPriceContainer.querySelector(
                '.andes-money-amount:not(.andes-money-amount--previous)'
            ) || currentPriceContainer;
            const fracEl = amountEl.querySelector('.andes-money-amount__fraction');
            const centsEl = amountEl.querySelector('.andes-money-amount__cents');
            promoFraction = fracEl ? fracEl.textContent.trim() : null;
            promoCents = centsEl ? centsEl.textContent.trim() : null;
        }

        // Preço original (anterior)
        const prevAmountEl = card.querySelector(
            's.andes-money-amount--previous, .andes-money-amount--previous, .ui-search-price__original-value'
        );
        let origFraction = null;
        let origCents = null;
        if (prevAmountEl) {
            const oFrac = prevAmountEl.querySelector('.andes-money-amount__fraction');
            const oCents = prevAmountEl.querySelector('.andes-money-amount__cents');
            origFraction = oFrac ? oFrac.textContent.trim() : null;
            origCents = oCents ? oCents.textContent.trim() : null;
        }

        // Percentual de desconto
        const discountText = getText(card, [
            '.andes-money-amount__discount',
            '.poly-price__disc_label',
            '.ui-search-price__discount'
        ]);

        // Avaliação (nota e quantidade)
        let ratingText = getText(card, [
            '.poly-reviews__rating',
            '.ui-search-reviews__rating-number',
            '.andes-review-star-rating__label'
        ]);
        let reviewsText = getText(card, [
            '.poly-reviews__total',
            '.ui-search-reviews__amount'
        ]);

        if (!ratingText || !reviewsText) {
            const srReview = getText(card, [
                '.poly-component__reviews .andes-visually-hidden',
                '.ui-search-reviews .andes-visually-hidden'
            ]);
            if (srReview) {
                if (!ratingText) ratingText = srReview;
                if (!reviewsText) {
                    const opMatch = srReview.match(/(\\d[\\d.]*)\\s*(?:opini|avalia)/i);
                    if (opMatch) reviewsText = opMatch[1];
                }
            }
        }

        // Vendedor
        const sellerText = getText(card, [
            '.poly-component__seller',
            '.ui-search-official-store-label',
            '.ui-search-item__group__element.ui-search-item__brand-discoverability'
        ]);

        // Imagem principal
        const imgEl = card.querySelector(
            'img.poly-component__picture, img.ui-search-result-image__element, img'
        );
        let imageUrl = null;
        if (imgEl) {
            const src = imgEl.getAttribute('src') || '';
            const dataSrc = imgEl.getAttribute('data-src') || '';
            imageUrl = (src && !src.startsWith('data:image')) ? src : (dataSrc || src || null);
        }

        // Possíveis IDs em atributos de dados ou inputs dentro do card
        const rawHtmlId =
            card.getAttribute('data-id') ||
            card.getAttribute('id') ||
            getAttr(card, ['[data-item-id]', 'input[name="itemId"]'], 'data-item-id') ||
            getAttr(card, ['input[name="itemId"]'], 'value') ||
            null;

        return {
            id: rawHtmlId,
            raw_html_id: card.innerHTML.slice(0, 1200),
            name,
            url,
            promo_fraction: promoFraction,
            promo_cents: promoCents,
            orig_fraction: origFraction,
            orig_cents: origCents,
            discount_text: discountText,
            rating_text: ratingText,
            reviews_text: reviewsText,
            seller_text: sellerText,
            image_url: imageUrl
        };
    });
}
"""

# Bloqueia apenas mídia pesada (vídeo/áudio) e rastreadores externos de anúncios,
# preservando 100% dos recursos first-party do Mercado Livre (mlstatic.com / mercadolivre.com.br)
# para não acionar heurísticas anti-bot na sessão autenticada.
BLOCKED_RESOURCE_TYPES = frozenset({"media"})
BLOCKED_TRACKER_PATTERNS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.net",
    "connect.facebook.net",
    "hotjar.com",
    "clarity.ms",
    "tiktok.com/i18n",
)


class MercadoLivreScraper:
    """Gerencia a coleta de produtos no Mercado Livre via Playwright (perfil persistente Chromium)."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # Garante exclusão mútua assíncrona no perfil persistente do Chromium
        self._concurrency_sem = asyncio.Semaphore(1)
        self._route_handler: Any = None
        self._search_cache: dict[tuple[str, int, int], tuple[float, SearchResponse]] = {}
        self._cache_ttl_seconds: float = 20.0
        # Controle adaptativo de pausa/cooldown quando CAPTCHA ou bloqueio é detectado
        self._cooldown_until: float = 0.0
        self._cooldown_reason: str | None = None
        self._consecutive_blocks: int = 0

    def clear_cache(self) -> None:
        """Limpa o cache em memória de buscas."""
        self._search_cache.clear()

    def is_in_cooldown(self) -> tuple[bool, float, str | None]:
        """Retorna `(em_cooldown, segundos_restantes, motivo)` se o scraper estiver aguardando recuperação legítima."""
        now = time.monotonic()
        if self._cooldown_until > now:
            return True, round(self._cooldown_until - now, 1), self._cooldown_reason
        return False, 0.0, None

    def activate_cooldown(self, seconds: float | None = None, reason: str | None = None) -> float:
        """Ativa janela de espera adaptativa após detecção de CAPTCHA ou bloqueio, interrompendo novas requisições."""
        self._consecutive_blocks += 1
        if seconds is not None:
            wait_sec = seconds
        else:
            # Backoff adaptativo: 60s -> 120s -> 240s -> 300s
            wait_sec = min(300.0, 60.0 * (2 ** max(0, self._consecutive_blocks - 1)))
        self._cooldown_until = time.monotonic() + wait_sec
        self._cooldown_reason = reason or (
            "Coleta temporariamente pausada após detecção de CAPTCHA/bloqueio para aguardar recuperação legítima."
        )
        logger.warning(
            "Circuit Breaker ativado por %.0fs (bloqueios consecutivos=%d): %s",
            wait_sec,
            self._consecutive_blocks,
            self._cooldown_reason,
        )
        return wait_sec

    def clear_cooldown(self) -> None:
        """Limpa o estado de cooldown (ex.: após login/verificação legítima concluída)."""
        self._cooldown_until = 0.0
        self._cooldown_reason = None
        self._consecutive_blocks = 0

    @staticmethod
    def _fast_resource_blocker(route: Route) -> None:
        """Bloqueia apenas mídia pesada e trackers externos de terceiros sem afetar recursos do Mercado Livre."""
        try:
            req = route.request
            if req.resource_type in BLOCKED_RESOURCE_TYPES:
                route.abort()
                return
            url_lower = req.url.lower()
            if any(pat in url_lower for pat in BLOCKED_TRACKER_PATTERNS):
                route.abort()
                return
            route.continue_()
        except PlaywrightError:
            pass

    @staticmethod
    def build_search_url(query: str, page_number: int = 1) -> str:
        """Monta a URL de busca no Mercado Livre a partir de uma palavra-chave ou busca geral (vazia) com suporte a página."""
        clean_term = re.sub(r"[,;\r\n]+", " ", query).strip()
        if not clean_term:
            if page_number > 1:
                return f"{settings.ml_base_url}/ofertas?page={page_number}"
            return f"{settings.ml_base_url}/ofertas"

        normalized = "-".join(clean_term.split())
        encoded_slug = quote_plus(normalized)
        encoded_query = quote_plus(clean_term)

        if page_number > 1:
            offset = 1 + (page_number - 1) * 48
            return f"{settings.ml_search_base_url}/{encoded_slug}_Desde_{offset}_NoIndex_True"

        return f"{settings.ml_search_base_url}/{encoded_slug}#D[A:{encoded_query}]"

    @staticmethod
    def _detect_challenge_or_auth(page: Page) -> tuple[str | None, bool]:
        """Verifica se a página atual exige CAPTCHA visível, verificação de conta ou login.

        Retorna `(mensagem, is_auth_redirect)`. Não tenta resolver nem contornar o bloqueio.
        Ignora páginas que já renderizaram cards de produtos e ignora badges invisíveis do reCAPTCHA v3.
        """
        current_url = (page.url or "").lower()
        if session_manager.is_url_auth_gate(current_url):
            return (
                "O Mercado Livre redirecionou para uma página de verificação de conta/login "
                f"({current_url}). Coleta interrompida em respeito às restrições do site.",
                True,
            )

        try:
            res = page.evaluate(DETECT_CHALLENGE_JS)
            if isinstance(res, dict) and res.get("blocked"):
                if res.get("isAuth"):
                    return (
                        "O Mercado Livre apresentou tela de verificação de conta/login. "
                        "Coleta interrompida em respeito às restrições do site.",
                        True,
                    )
                return (
                    "Desafio CAPTCHA/anti-bot detectado na página. "
                    "A coleta foi interrompida imediatamente sem tentativa de contorno.",
                    False,
                )
        except PlaywrightError:
            pass

        return None, False

    @staticmethod
    def _scroll_page_for_lazy_images(page: Page) -> None:
        """Executa rolagem inteligente apenas se houver cards aguardando hidratação de imagem."""
        try:
            page.evaluate(SMART_SCROLL_JS)
        except PlaywrightError as exc:
            logger.debug("Falha não-crítica ao rolar página: %s", exc)

    @staticmethod
    def _get_next_page_url(page: Page) -> str | None:
        """Obtém a URL da próxima página de resultados em uma única avaliação JS."""
        try:
            href = page.evaluate(NEXT_PAGE_URL_JS)
            if isinstance(href, str) and href:
                return href
        except PlaywrightError:
            pass
        return None

    def _scrape_keyword_on_page(
        self,
        page: Page,
        query: str,
        limit: int,
        page_number: int,
        authenticated_session_used: bool,
    ) -> SearchResponse:
        """Coleta produtos para uma única palavra-chave reutilizando uma `Page` aberta do Playwright."""
        start_time = time.perf_counter()
        collected: list[ProductItem] = []
        seen_keys: set[str] = set()
        pages_visited = 0
        warning_msg: str | None = None
        blocked = False
        session_expired = False
        nav_ms_total = 0.0
        extract_ms_total = 0.0

        search_url = self.build_search_url(query, page_number=page_number)
        logger.info(
            "Iniciando busca no Mercado Livre | query='%s' | page=%d | limit=%d | auth_session=%s | url=%s",
            query,
            page_number,
            limit,
            authenticated_session_used,
            search_url,
        )

        current_url: str | None = search_url

        while (
            current_url
            and len(collected) < limit
            and pages_visited < settings.max_pages_per_search
        ):
            pages_visited += 1
            t_nav_start = time.perf_counter()
            try:
                page.goto(
                    current_url,
                    wait_until="domcontentloaded",
                    timeout=settings.navigation_timeout_ms,
                )
            except PlaywrightTimeoutError:
                nav_ms_total += (time.perf_counter() - t_nav_start) * 1000.0
                warning_msg = f"Timeout ao carregar a página {pages_visited} do Mercado Livre."
                logger.warning(warning_msg)
                break

            # 1. Verifica redirecionamento imediato de URL para login/verificação
            current_page_url = (page.url or "").lower()
            if session_manager.is_url_auth_gate(current_page_url):
                nav_ms_total += (time.perf_counter() - t_nav_start) * 1000.0
                challenge_reason = (
                    "O Mercado Livre redirecionou para uma página de verificação de conta/login "
                    f"({current_page_url}). Coleta interrompida em respeito às restrições do site."
                )
                logger.warning("Bloqueio de URL detectado na página %d: %s", pages_visited, challenge_reason)
                if authenticated_session_used:
                    session_expired = True
                    session_manager.mark_session_expired(
                        "A sessão salva expirou (o Mercado Livre redirecionou para login/verificação). "
                        "Faça login novamente."
                    )
                    challenge_reason = f"{challenge_reason} A sessão salva expirou e foi marcada como desconectada."
                warning_msg = challenge_reason
                blocked = True
                break

            # 2. Aguarda os cards de produto (ou aviso de busca sem resultados) aparecerem antes de checar bloqueios no DOM
            try:
                page.wait_for_selector(
                    "li.ui-search-layout__item, div.poly-card, div.ui-search-result__wrapper, div.ui-search-rescue",
                    state="attached",
                    timeout=settings.selector_timeout_ms,
                )
            except PlaywrightTimeoutError:
                nav_ms_total += (time.perf_counter() - t_nav_start) * 1000.0
                challenge_reason, is_auth_redirect = self._detect_challenge_or_auth(page)
                if challenge_reason:
                    logger.warning("Bloqueio detectado na página %d: %s", pages_visited, challenge_reason)
                    if authenticated_session_used and is_auth_redirect:
                        session_expired = True
                        session_manager.mark_session_expired(
                            "A sessão salva expirou (o Mercado Livre redirecionou para login/verificação). "
                            "Faça login novamente."
                        )
                        challenge_reason = (
                            f"{challenge_reason} A sessão salva expirou e foi marcada como desconectada."
                        )
                    warning_msg = challenge_reason
                    blocked = True
                    break

                logger.info("Nenhum card de produto encontrado na página %d.", pages_visited)
                if pages_visited == 1:
                    warning_msg = "Nenhum produto encontrado para o termo informado ou layout indisponível."
                break

            nav_ms_total += (time.perf_counter() - t_nav_start) * 1000.0

            # Extração DOM + smart scroll
            t_ext_start = time.perf_counter()
            self._scroll_page_for_lazy_images(page)

            raw_cards: list[dict[str, Any]] = page.evaluate(EXTRACT_CARDS_JS)
            if not raw_cards:
                extract_ms_total += (time.perf_counter() - t_ext_start) * 1000.0
                if pages_visited == 1:
                    warning_msg = "Nenhum produto encontrado para o termo informado ou layout indisponível."
                break

            added_this_page = 0
            for raw_card in raw_cards:
                if len(collected) >= limit:
                    break
                item = build_product_item(raw_card)
                if not item:
                    continue

                dedup_key = item.id or item.url or item.name or ""
                if dedup_key and dedup_key in seen_keys:
                    continue
                if dedup_key:
                    seen_keys.add(dedup_key)

                collected.append(item)
                added_this_page += 1

            extract_ms_total += (time.perf_counter() - t_ext_start) * 1000.0

            logger.info(
                "Página %d processada | brutos=%d | novos=%d | acumulado=%d/%d",
                pages_visited,
                len(raw_cards),
                added_this_page,
                len(collected),
                limit,
            )

            if len(collected) >= limit:
                break

            next_url = self._get_next_page_url(page)
            if not next_url or next_url == current_url:
                break

            current_url = next_url
            if settings.page_delay_ms > 0:
                page.wait_for_timeout(min(settings.page_delay_ms, 250))

        elapsed = round(time.perf_counter() - start_time, 2)
        status = "blocked" if blocked else ("ok" if collected else "partial")

        return SearchResponse(
            status=status,
            metadata=SearchMetadata(
                query=query,
                requested_limit=limit,
                total_returned=len(collected),
                pages_visited=pages_visited,
                elapsed_seconds=elapsed,
                navigation_ms=round(nav_ms_total, 1),
                extraction_ms=round(extract_ms_total, 1),
                warning=warning_msg,
                blocked_by_challenge=blocked,
                authenticated_session_used=authenticated_session_used,
                session_expired=session_expired,
            ),
            results=collected[:limit],
        )

    def _run_sync_scrape_keywords(
        self,
        keywords: list[str],
        limits: list[int],
        page_number: int = 1,
    ) -> list[SearchResponse]:
        """Executa a coleta de uma ou mais palavras-chave reutilizando o perfil persistente do Chromium (`user_data_dir`)."""
        authenticated_session_used = session_manager.is_authenticated()
        responses: list[SearchResponse] = []

        # Verifica se há cooldown ativo de CAPTCHA antes de abrir o navegador
        in_cooldown, rem_sec, cd_reason = self.is_in_cooldown()
        if in_cooldown:
            msg = f"{cd_reason} (aguarde {rem_sec:.0f}s para nova tentativa)"
            logger.warning("Busca interrompida preventivamente por cooldown de CAPTCHA: %s", msg)
            for kw, kw_limit in zip(keywords, limits):
                responses.append(
                    SearchResponse(
                        status="blocked",
                        metadata=SearchMetadata(
                            query=kw,
                            requested_limit=kw_limit,
                            total_returned=0,
                            pages_visited=0,
                            elapsed_seconds=0.0,
                            warning=msg,
                            blocked_by_challenge=True,
                            authenticated_session_used=authenticated_session_used,
                        ),
                        results=[],
                    )
                )
            return responses

        try:
            with sync_playwright() as pw:
                with session_manager.open_persistent_context(
                    pw,
                    headless=settings.headless,
                    viewport={"width": 1366, "height": 860},
                ) as (_context, page):
                    if self._route_handler is not None:
                        page.route("**/*", self._route_handler)
                    else:
                        page.route("**/*", self._fast_resource_blocker)

                    page.set_default_navigation_timeout(settings.navigation_timeout_ms)
                    page.set_default_timeout(settings.selector_timeout_ms)

                    for idx, (kw, kw_limit) in enumerate(zip(keywords, limits)):
                        if idx > 0 and self._route_handler is None:
                            # Pequena pausa orgânica entre palavras-chave na mesma aba para evitar rate-limit
                            page.wait_for_timeout(350)
                        sub_res = self._scrape_keyword_on_page(
                            page=page,
                            query=kw,
                            limit=kw_limit,
                            page_number=page_number,
                            authenticated_session_used=authenticated_session_used,
                        )
                        responses.append(sub_res)

                        if sub_res.status == "blocked":
                            # Interrompe imediatamente novas requisições na rodada ao detectar CAPTCHA ou bloqueio
                            self.activate_cooldown(reason=sub_res.metadata.warning)
                            break
                        elif sub_res.status == "ok" and sub_res.results:
                            self._consecutive_blocks = 0

        except Exception as exc:
            logger.error("Erro no navegador durante coleta em lote: %s", exc, exc_info=True)
            if not responses:
                for kw, kw_limit in zip(keywords, limits):
                    responses.append(
                        SearchResponse(
                            status="partial",
                            metadata=SearchMetadata(
                                query=kw,
                                requested_limit=kw_limit,
                                total_returned=0,
                                pages_visited=0,
                                elapsed_seconds=0.0,
                                warning=f"Erro no navegador durante a coleta: {exc}",
                                authenticated_session_used=authenticated_session_used,
                            ),
                            results=[],
                        )
                    )

        return responses

    def _run_sync_scrape(self, query: str, limit: int, page_number: int = 1) -> SearchResponse:
        """Compatibilidade direta para coleta síncrona de um único termo."""
        res_list = self._run_sync_scrape_keywords([query], [limit], page_number=page_number)
        return res_list[0]

    async def search_keywords_batch(
        self,
        keywords: list[str],
        limit_per_keyword: int,
        page_number: int = 1,
        enrich_affiliates: bool = False,
    ) -> list[SearchResponse]:
        """Coleta múltiplas palavras-chave no perfil persistente do Chromium e persiste os produtos no banco."""
        from app.affiliates import ml_affiliate_provider
        from app.storage import storage_service

        clean_kws = [k.strip() for k in keywords if k and k.strip()]
        if not clean_kws:
            if any(isinstance(k, str) and not k.strip() for k in keywords):
                clean_kws = [""]
            else:
                return []

        now = time.monotonic()
        use_cache = self._route_handler is None
        cached_map: dict[str, SearchResponse] = {}
        missing_kws: list[str] = []

        if use_cache:
            for kw in clean_kws:
                ckey = (kw.lower(), limit_per_keyword, page_number)
                entry = self._search_cache.get(ckey)
                if entry is not None and (now - entry[0]) < self._cache_ttl_seconds:
                    copied = copy.deepcopy(entry[1])
                    copied.metadata.cache_hit = True
                    cached_map[kw] = copied
                else:
                    missing_kws.append(kw)
        else:
            missing_kws = list(clean_kws)

        scraped_map: dict[str, SearchResponse] = {}
        if missing_kws:
            limits = [limit_per_keyword] * len(missing_kws)
            async with self._concurrency_sem:
                batch_res = await asyncio.to_thread(
                    self._run_sync_scrape_keywords,
                    missing_kws,
                    limits,
                    page_number,
                )
            for kw, s_res in zip(missing_kws, batch_res):
                scraped_map[kw] = s_res
                # Persiste todos os produtos encontrados no Banco de Produtos (products_catalog)
                if s_res.results:
                    storage_service.upsert_catalog_products(
                        s_res.results,
                        source_id="mercadolivre",
                        query_term=kw,
                        processing_status="discovered",
                    )
                if use_cache and s_res.status == "ok" and s_res.results:
                    if len(self._search_cache) > 256:
                        self._search_cache.clear()
                    self._search_cache[(kw.lower(), limit_per_keyword, page_number)] = (
                        time.monotonic(),
                        copy.deepcopy(s_res),
                    )

        ordered_responses: list[SearchResponse] = []
        for kw in clean_kws:
            if kw in cached_map:
                ordered_responses.append(cached_map[kw])
            elif kw in scraped_map:
                ordered_responses.append(scraped_map[kw])

        if enrich_affiliates:
            for resp in ordered_responses:
                if resp.results:
                    t_aff = time.perf_counter()
                    _, ok_c, fail_c = await ml_affiliate_provider.enrich_products(resp.results)
                    resp.metadata.affiliate_ms = round((time.perf_counter() - t_aff) * 1000.0, 1)
                    resp.metadata.affiliate_converted_count = ok_c
                    resp.metadata.affiliate_failed_count = fail_c
                    storage_service.upsert_catalog_products(
                        resp.results,
                        source_id="mercadolivre",
                        query_term=resp.metadata.query,
                        processing_status="affiliate_ready",
                    )

        return ordered_responses

    async def search_products(
        self,
        query: str,
        limit: int,
        page_number: int = 1,
        enrich_affiliates: bool = True,
    ) -> SearchResponse:
        """Interface assíncrona para execução segura da coleta e conversão opcional de afiliados."""
        from app.affiliates import ml_affiliate_provider

        keywords = parse_keywords(query)
        if not keywords:
            keywords = [query.strip()]

        start_overall = time.perf_counter()
        all_collected: list[ProductItem] = []
        seen_keys: set[str] = set()
        total_pages = 0
        total_nav_ms = 0.0
        total_ext_ms = 0.0
        any_cache_hit = False
        warnings: list[str] = []
        any_blocked = False
        auth_used = False
        sess_expired = False

        per_kw_limit = max(1, (limit + len(keywords) - 1) // len(keywords)) if len(keywords) > 1 else limit
        kw_target = max(per_kw_limit, limit) if len(keywords) > 1 else limit

        sub_responses = await self.search_keywords_batch(
            keywords=keywords,
            limit_per_keyword=kw_target,
            page_number=page_number,
            enrich_affiliates=False,
        )

        for kw, sub_res in zip(keywords, sub_responses):
            total_pages += sub_res.metadata.pages_visited
            total_nav_ms += sub_res.metadata.navigation_ms
            total_ext_ms += sub_res.metadata.extraction_ms
            any_cache_hit = any_cache_hit or sub_res.metadata.cache_hit
            auth_used = auth_used or sub_res.metadata.authenticated_session_used
            sess_expired = sess_expired or sub_res.metadata.session_expired

            if sub_res.status == "blocked":
                any_blocked = True
                if sub_res.metadata.warning:
                    warnings.append(f"[{kw}] {sub_res.metadata.warning}")
                continue

            if not sub_res.results:
                msg = sub_res.metadata.warning or f"Nenhum resultado encontrado para '{kw}'."
                warnings.append(f"[{kw}] {msg}")
                logger.info("Palavra-chave '%s' não retornou resultados; prosseguindo para as demais.", kw)
                continue

            for item in sub_res.results:
                if len(all_collected) >= limit:
                    break
                dedup_key = item.id or item.url or item.name or ""
                if dedup_key and dedup_key in seen_keys:
                    continue
                if dedup_key:
                    seen_keys.add(dedup_key)
                all_collected.append(item)

        ok_count = 0
        fail_count = 0
        aff_ms = 0.0
        if all_collected and enrich_affiliates:
            t_aff_start = time.perf_counter()
            _, ok_count, fail_count = await ml_affiliate_provider.enrich_products(all_collected)
            aff_ms = round((time.perf_counter() - t_aff_start) * 1000.0, 1)
            logger.info(
                "Enriquecimento de afiliados concluído | query='%s' | gerados=%d | falhas=%d | tempo=%.1fms",
                ", ".join(keywords),
                ok_count,
                fail_count,
                aff_ms,
            )

        elapsed_total = round(time.perf_counter() - start_overall, 2)
        final_status = (
            "blocked"
            if (any_blocked and not all_collected)
            else ("ok" if all_collected else "partial")
        )
        combined_warning = " | ".join(warnings) if warnings else None

        return SearchResponse(
            status=final_status,
            metadata=SearchMetadata(
                query=", ".join(keywords),
                requested_limit=limit,
                total_returned=len(all_collected),
                pages_visited=total_pages,
                elapsed_seconds=elapsed_total,
                navigation_ms=round(total_nav_ms, 1),
                extraction_ms=round(total_ext_ms, 1),
                affiliate_ms=aff_ms,
                cache_hit=any_cache_hit,
                warning=combined_warning,
                blocked_by_challenge=any_blocked,
                authenticated_session_used=auth_used,
                session_expired=sess_expired,
                affiliate_converted_count=ok_count,
                affiliate_failed_count=fail_count,
            ),
            results=all_collected[:limit],
        )

    @staticmethod
    def check_playwright_ready() -> bool:
        """Verifica rapidamente se o pacote Playwright está acessível."""
        try:
            import playwright  # noqa: F401
            return True
        except Exception:
            return False


scraper_service = MercadoLivreScraper()
