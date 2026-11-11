"""Catalog + market price. The 'market price' is the lowest in-stock Channel3 offer.

Real prices won't move during a demo, so merchants can set a *simulated* price. Anything derived
from it is labelled price_source='simulated' end to end (DB, API, UI).
"""
from __future__ import annotations

import sqlite3
import time
import uuid

import httpx

from . import channel3
from .channel3 import Channel3Error
from .config import Settings, get_settings
from .db import tx

def refresh_catalog(conn: sqlite3.Connection, settings: Settings | None = None,
                    http: httpx.Client | None = None) -> dict:
    """Reload the Channel3 part of the catalog. Products the merchant added by hand are kept."""
    s = settings or get_settings()
    if not s.channel3_api_key:
        return {"source": "channel3", "count": 0, "channel3_response_keys": [],
                "error": "CHANNEL3_API_KEY is not set. Add products by hand, or set the key to import them."}
    try:
        raw = channel3.search(s, s.catalog_query, s.catalog_size, http)
    except Channel3Error as e:
        return {"source": "channel3", "count": 0, "channel3_response_keys": [], "error": str(e)}
    keys = sorted(raw[0].keys()) if raw else []  # shown so the image field can be verified
    items = [p for p in (channel3.product_from_search(r) for r in raw) if p]
    now = int(time.time())
    with tx(conn):
        conn.execute("DELETE FROM products WHERE source='channel3'")
        conn.executemany(
            "INSERT OR REPLACE INTO products (id,title,image_url,price_cents,currency,source,updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            [(p["id"], p["title"], p["image_url"], p["price_cents"], p["currency"], "channel3", now) for p in items],
        )
    return {"source": "channel3", "count": len(items), "channel3_response_keys": keys, "error": None}


def _clean_category(category: str | None) -> str | None:
    category = " ".join((category or "").split())
    return category[:60] or None


def add_product(conn: sqlite3.Connection, title: str, price_cents: int, image_url: str | None = None,
                currency: str = "USD", category: str | None = None) -> str:
    title = title.strip()
    if not title:
        raise ValueError("Product name is required.")
    if price_cents <= 0:
        raise ValueError("Price must be positive.")
    if image_url and not image_url.startswith("https://"):
        raise ValueError("Image URL must start with https://")
    pid = "m-" + uuid.uuid4().hex[:10]
    conn.execute(
        "INSERT INTO products (id,title,image_url,price_cents,currency,source,updated_at,category) VALUES (?,?,?,?,?,?,?,?)",
        (pid, title, image_url or None, price_cents, currency, "merchant", int(time.time()), _clean_category(category)))
    return pid


def edit_product(conn: sqlite3.Connection, product_id: str, *, title: str | None = None,
                 image_url: str | None = None, category: str | None = None) -> None:
    """Change how a product is presented. The price is deliberately not editable here: for products
    added by hand the catalog price IS the market price, so changing it would trigger refunds."""
    sets, args = [], []
    if title is not None:
        if not title.strip():
            raise ValueError("Product name is required.")
        sets.append("title=?"); args.append(title.strip())
    if image_url is not None:
        if image_url and not image_url.startswith("https://"):
            raise ValueError("Image URL must start with https://")
        sets.append("image_url=?"); args.append(image_url or None)
    if category is not None:
        sets.append("category=?"); args.append(_clean_category(category))
    if not sets:
        return
    sets.append("updated_at=?"); args.append(int(time.time()))
    if not conn.execute(f"UPDATE products SET {', '.join(sets)} WHERE id=?", (*args, product_id)).rowcount:
        raise KeyError(product_id)


def delete_product(conn: sqlite3.Connection, product_id: str) -> None:
    with tx(conn):
        if not conn.execute("DELETE FROM products WHERE id=?", (product_id,)).rowcount:
            raise KeyError(product_id)
        conn.execute("DELETE FROM price_overrides WHERE product_id=?", (product_id,))


def list_products(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM products ORDER BY title").fetchall()


def market_price(conn: sqlite3.Connection, product_id: str,
                 http: httpx.Client | None = None) -> tuple[int, str]:
    """-> (price_cents, source) where source is simulated | channel3 | catalog."""
    ov = conn.execute("SELECT price_cents FROM price_overrides WHERE product_id=?", (product_id,)).fetchone()
    if ov:
        return ov["price_cents"], "simulated"
    p = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not p:
        raise KeyError(product_id)
    s = get_settings()
    if p["source"] == "channel3" and s.channel3_api_key:
        try:
            cents = channel3.market_price_cents(channel3.get_product(s, product_id, http), p["currency"])
            if cents:
                return cents, "channel3"
        except Channel3Error:
            pass
    return p["price_cents"], "catalog"


def simulate_drop(conn: sqlite3.Connection, product_id: str, *, drop_pct: float | None = None,
                  new_price: float | None = None) -> int:
    p = conn.execute("SELECT price_cents FROM products WHERE id=?", (product_id,)).fetchone()
    if not p:
        raise KeyError(product_id)
    if (drop_pct is None) == (new_price is None):
        raise ValueError("Provide exactly one of drop_pct or new_price.")
    if drop_pct is not None:
        if not 0 < drop_pct < 100:
            raise ValueError("drop_pct must be between 0 and 100.")
        cents = round(p["price_cents"] * (1 - drop_pct / 100))
    else:
        from .money import to_cents
        cents = to_cents(new_price)
    if cents <= 0:
        raise ValueError("Simulated price must be positive.")
    conn.execute(
        "INSERT INTO price_overrides (product_id, price_cents, created_at) VALUES (?,?,?) "
        "ON CONFLICT(product_id) DO UPDATE SET price_cents=excluded.price_cents, created_at=excluded.created_at",
        (product_id, cents, int(time.time())),
    )
    return cents


def reset_simulation(conn: sqlite3.Connection, product_id: str | None = None) -> None:
    if product_id:
        conn.execute("DELETE FROM price_overrides WHERE product_id=?", (product_id,))
    else:
        conn.execute("DELETE FROM price_overrides")
