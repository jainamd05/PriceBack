"""One watcher pass: recover unknown refunds, then re-check every guaranteed order against market price."""
from __future__ import annotations

import sqlite3

from .paypal import PayPalClient
from .prices import market_price
from .settle import recover_pending, settle_order


def run_watch_once(conn: sqlite3.Connection, paypal: PayPalClient, now: int | None = None) -> dict:
    recovered = recover_pending(conn, paypal, now=now)
    orders = conn.execute(
        "SELECT * FROM orders WHERE status='CAPTURED' AND policy_json IS NOT NULL ORDER BY id").fetchall()
    prices: dict[str, tuple[int, str]] = {}
    results = []
    for o in orders:
        pid = o["product_id"]
        if pid not in prices:
            try:
                prices[pid] = market_price(conn, pid)
            except KeyError:
                continue  # product no longer in catalog
        cents, source = prices[pid]
        r = settle_order(conn, paypal, o, cents, source, now=now)
        if r["action"] != "skip":
            results.append({"order_id": o["id"], "title": o["title"], "price_source": source, **r})
    return {
        "checked": len(orders),
        "recovered": recovered,
        "refunds": sum(1 for r in results if r["action"] == "refund"),
        "blocked": sum(1 for r in results if r["action"] == "blocked"),
        "results": results,
    }
