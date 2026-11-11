"""Channel3 product data (https://docs.trychannel3.com).

Confirmed from their docs: POST /v1/search {query, limit, filters.price}, GET /v1/products/{id},
header x-api-key, prices at offers[].price.{amount,currency}, availability like 'InStock'.
NOT documented for search results: the image field name -> parsed defensively (see _image).
"""
from __future__ import annotations

import httpx

from .config import Settings
from .money import to_cents


class Channel3Error(Exception):
    pass


def _headers(s: Settings) -> dict:
    return {"x-api-key": s.channel3_api_key, "Content-Type": "application/json"}


def search(s: Settings, query: str, limit: int, http: httpx.Client | None = None) -> list[dict]:
    try:
        client = http or httpx.Client(timeout=20)
        r = client.post(f"{s.channel3_base_url}/v1/search", headers=_headers(s),
                        json={"query": query, "limit": limit})
        if r.status_code >= 400:
            raise Channel3Error(f"Channel3 search {r.status_code}: {r.text[:300]}")
        return r.json().get("products", [])
    except httpx.HTTPError as e:
        raise Channel3Error(f"Channel3 search failed: {e}") from e


def get_product(s: Settings, product_id: str, http: httpx.Client | None = None) -> dict:
    try:
        client = http or httpx.Client(timeout=20)
        r = client.get(f"{s.channel3_base_url}/v1/products/{product_id}", headers=_headers(s))
        if r.status_code >= 400:
            raise Channel3Error(f"Channel3 product {r.status_code}: {r.text[:300]}")
        return r.json()
    except httpx.HTTPError as e:
        raise Channel3Error(f"Channel3 product lookup failed: {e}") from e


def best_offer(offers: list[dict], currency: str = "USD") -> dict | None:
    """Market price = lowest in-stock offer in `currency` (falls back to any listed offer)."""
    def amount(o):
        p = o.get("price") or {}
        return p.get("amount") if p.get("currency", currency) == currency else None

    valid = [o for o in offers or [] if amount(o) is not None]
    in_stock = [o for o in valid
                if str(o.get("availability", "InStock")).replace("_", "").replace(" ", "").lower() == "instock"]
    pool = in_stock or valid
    return min(pool, key=lambda o: float(amount(o))) if pool else None


def _image(p: dict) -> str | None:
    imgs = p.get("images") or p.get("image_urls") or []
    if isinstance(imgs, list) and imgs:
        first = imgs[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return first.get("url")
    return p.get("image_url") or p.get("image")


def product_from_search(p: dict, currency: str = "USD") -> dict | None:
    offer = best_offer(p.get("offers", []), currency)
    if not offer or not p.get("id") or not p.get("title"):
        return None
    return {
        "id": str(p["id"]),
        "title": str(p["title"]),
        "image_url": _image(p),
        "price_cents": to_cents(offer["price"]["amount"]),
        "currency": currency,
    }


def market_price_cents(product: dict, currency: str = "USD") -> int | None:
    offer = best_offer(product.get("offers", []), currency)
    return to_cents(offer["price"]["amount"]) if offer else None
