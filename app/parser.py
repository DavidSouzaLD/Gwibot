"""Utilitários de parsing e normalização de dados extraídos dos cards do Mercado Livre."""
from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from app.models import ProductItem

# Padrões para identificar o ID do anúncio/produto do Mercado Livre (ex: MLB1234567890 ou MLB-1234567890)
MLB_ID_PATTERNS = [
    re.compile(r"\b(MLB-?\d{6,14})\b", re.IGNORECASE),
    re.compile(r"/p/(MLB\d{6,14})", re.IGNORECASE),
    re.compile(r"wid=(MLB\d{6,14})", re.IGNORECASE),
    re.compile(r"item_id['\"]?\s*[:=]\s*['\"]?(MLB\d{6,14})", re.IGNORECASE),
]


def normalize_mlb_id(raw: Optional[str]) -> Optional[str]:
    """Extrai e padroniza o código MLB (sem hífen, em maiúsculas) a partir de string ou URL."""
    if not raw:
        return None
    for pattern in MLB_ID_PATTERNS:
        match = pattern.search(raw)
        if match:
            return match.group(1).replace("-", "").upper()
    return None


def parse_keywords(raw_query: Optional[str]) -> list[str]:
    """Divide uma string de busca por vírgulas, ponto-e-vírgula ou quebras de linha.

    Remove espaços extras, descartando entradas vazias e duplicadas (case-insensitive)
    preservando a ordem original.
    """
    if not raw_query:
        return []
    parts = re.split(r"[,;\r\n]+", str(raw_query))
    result: list[str] = []
    seen: set[str] = set()
    for part in parts:
        cleaned = re.sub(r"\s+", " ", part).strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def clean_product_url(raw_url: Optional[str]) -> Optional[str]:
    """Limpa parâmetros de rastreamento excessivos da URL mantendo o link funcional."""
    if not raw_url or not isinstance(raw_url, str):
        return None
    raw_url = raw_url.strip()
    if not raw_url.startswith(("http://", "https://")):
        return None
    try:
        parts = urlsplit(raw_url)
        # Se for redirect click1.mercadolivre.com.br, mantém a URL original para não quebrar o destino
        if "click1.mercadolivre" in parts.netloc:
            return raw_url
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:
        return raw_url


def parse_brl_price(fraction: Optional[str], cents: Optional[str] = None) -> Optional[float]:
    """Converte strings de parte inteira e centavos do padrão brasileiro para float."""
    if not fraction:
        return None
    cleaned_fraction = re.sub(r"[^\d]", "", str(fraction))
    if not cleaned_fraction:
        return None
    value = float(cleaned_fraction)
    if cents:
        cleaned_cents = re.sub(r"[^\d]", "", str(cents))
        if cleaned_cents:
            value += float(cleaned_cents[:2].ljust(2, "0")) / 100.0
    return round(value, 2)


def parse_discount_percentage(raw_discount: Optional[str]) -> Optional[int]:
    """Extrai o valor inteiro do percentual de desconto (ex: '25% OFF' -> 25)."""
    if not raw_discount:
        return None
    match = re.search(r"(\d{1,2})\s*%", str(raw_discount))
    if match:
        val = int(match.group(1))
        return val if 0 < val < 100 else None
    return None


def parse_rating(raw_rating: Optional[str]) -> Optional[float]:
    """Extrai a nota média de avaliação (ex: '4.8' ou 'Avaliação 4,7 de 5' -> 4.7)."""
    if not raw_rating:
        return None
    text = str(raw_rating).replace(",", ".")
    match = re.search(r"\b([0-5](?:\.\d{1,2})?)\b", text)
    if match:
        val = float(match.group(1))
        if 0.0 <= val <= 5.0:
            return round(val, 2)
    return None


def parse_reviews_count(raw_reviews: Optional[str]) -> Optional[int]:
    """Extrai a quantidade de avaliações (ex: '(1.234)' ou '425 opiniões' ou '(2mil)' -> int)."""
    if not raw_reviews:
        return None
    text = str(raw_reviews).strip().lower()
    # Caso tenha 'mil' (ex: 1,5 mil ou 2 mil)
    mil_match = re.search(r"(\d+(?:[.,]\d+)?)\s*mil", text)
    if mil_match:
        num = float(mil_match.group(1).replace(",", "."))
        return int(num * 1000)

    # Remove pontos de milhar e captura dígitos
    digits_match = re.search(r"(\d[\d.]*)", text)
    if digits_match:
        digits = digits_match.group(1).replace(".", "")
        if digits.isdigit():
            return int(digits)
    return None


def parse_seller_name(raw_seller: Optional[str]) -> Optional[str]:
    """Limpa prefixos como 'Por ' ou 'Vendido por ' do nome do vendedor."""
    if not raw_seller:
        return None
    text = re.sub(r"\s+", " ", str(raw_seller)).strip()
    text = re.sub(r"^(?:vendido\s+por|por)\s+", "", text, flags=re.IGNORECASE).strip()
    return text if text else None


def build_product_item(raw: dict[str, Any]) -> Optional[ProductItem]:
    """Constrói e valida um `ProductItem` a partir do dicionário bruto extraído via DOM.

    Não inventa valores: qualquer campo ausente permanece `None` (`null` no JSON).
    """
    name = (raw.get("name") or "").strip() or None
    raw_url = raw.get("url")
    url = clean_product_url(raw_url)

    # Sem título e sem URL não é um card de produto válido
    if not name and not url:
        return None

    item_id = (
        normalize_mlb_id(raw.get("id"))
        or normalize_mlb_id(raw_url)
        or normalize_mlb_id(raw.get("image_url"))
        or normalize_mlb_id(raw.get("raw_html_id"))
    )

    promotional_price = parse_brl_price(raw.get("promo_fraction"), raw.get("promo_cents"))
    original_price = parse_brl_price(raw.get("orig_fraction"), raw.get("orig_cents"))

    discount_percentage = parse_discount_percentage(raw.get("discount_text"))
    if (
        discount_percentage is None
        and original_price is not None
        and promotional_price is not None
        and original_price > promotional_price > 0
    ):
        calc = round(((original_price - promotional_price) / original_price) * 100)
        if 0 < calc < 100:
            discount_percentage = int(calc)

    rating = parse_rating(raw.get("rating_text"))
    reviews_count = parse_reviews_count(raw.get("reviews_text"))
    seller = parse_seller_name(raw.get("seller_text"))

    image_url = (raw.get("image_url") or "").strip() or None
    if image_url and image_url.startswith("data:image"):
        image_url = None

    return ProductItem(
        id=item_id,
        name=name,
        url=url,
        original_price=original_price,
        promotional_price=promotional_price,
        discount_percentage=discount_percentage,
        rating=rating,
        reviews_count=reviews_count,
        seller=seller,
        image_url=image_url,
    )
