"""SQLite storage. Connections run in autocommit mode; use tx() for atomic sections."""
from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager

from .config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    image_url TEXT,
    price_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    source TEXT NOT NULL,              -- 'channel3' | 'merchant'
    updated_at INTEGER NOT NULL,
    category TEXT                      -- storefront grouping, e.g. 'Headphones'
);

-- Simulated market prices for testing the refund flow. Always labelled 'simulated' downstream.
CREATE TABLE IF NOT EXISTS price_overrides (
    product_id TEXT PRIMARY KEY,
    price_cents INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS policies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    source TEXT NOT NULL,              -- 'llm' | 'rules'
    active INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paypal_order_id TEXT UNIQUE NOT NULL,
    capture_id TEXT,
    product_id TEXT NOT NULL,
    title TEXT NOT NULL,
    paid_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,              -- CREATED | CAPTURING | CAPTURED | CAPTURE_FAILED | ...
    policy_json TEXT,                  -- snapshot of the promise in force at purchase time
    created_at INTEGER NOT NULL,
    captured_at INTEGER,               -- start of the guarantee window
    capture_webhook_at INTEGER         -- set when a verified PAYMENT.CAPTURE.COMPLETED arrives
);

-- Audit trail: every refund attempt and every guardrail block.
CREATE TABLE IF NOT EXISTS settlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    action TEXT NOT NULL,              -- 'refund' | 'blocked'
    amount_cents INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL,
    market_cents INTEGER NOT NULL,
    price_source TEXT NOT NULL,        -- 'simulated' | 'channel3' | 'catalog'
    status TEXT NOT NULL,              -- pending_api | COMPLETED | PENDING | FAILED | blocked
    idempotency_key TEXT UNIQUE,
    paypal_refund_id TEXT,
    explanation TEXT,
    error TEXT,
    webhook_confirmed_at INTEGER,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS webhook_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    resource_id TEXT,
    verified INTEGER NOT NULL,
    payload TEXT NOT NULL,
    received_at INTEGER NOT NULL
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(
        path or get_settings().db_path,
        isolation_level=None,  # autocommit; explicit BEGIN IMMEDIATE in tx()
        check_same_thread=False,
        timeout=15,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    """Atomic section. BEGIN IMMEDIATE takes the write lock up front so concurrent
    settlement decisions can't both read the same 'already refunded' total."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_db() -> None:
    with closing(connect()) as conn:
        conn.executescript(SCHEMA)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
        if "category" not in cols:  # databases created before categories existed
            conn.execute("ALTER TABLE products ADD COLUMN category TEXT")
