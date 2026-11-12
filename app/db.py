"""SQLite storage. Connections run in autocommit mode; use tx() for atomic sections."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path

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

-- Shopper accounts: passwordless sign-in with a one-time code sent to their email.
CREATE TABLE IF NOT EXISTS login_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    code_hash TEXT NOT NULL,           -- sha256(salt + code); the code itself is never stored
    salt TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    used INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS login_codes_email ON login_codes(email, created_at);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,       -- sha256 of the cookie value
    email TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

-- Outbox: one row per message to a shopper. UNIQUE(kind, ref) makes every event notify exactly once.
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    kind TEXT NOT NULL,                -- order_confirmed | refund_sent | refund_blocked | refund_failed
    ref TEXT NOT NULL,                 -- order id or settlement id the message is about
    order_id INTEGER,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL,              -- queued | sent | not_configured | failed
    channel TEXT,                      -- e.g. 'email:brevo, zapier'
    error TEXT,
    created_at INTEGER NOT NULL,
    sent_at INTEGER,
    UNIQUE(kind, ref)
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
        ocols = {r["name"] for r in conn.execute("PRAGMA table_info(orders)")}
        if "buyer_email" not in ocols:  # databases created before shopper accounts existed
            conn.execute("ALTER TABLE orders ADD COLUMN buyer_email TEXT")
        if "test_policy_id" not in ocols:  # set when the order was bought under a guest's Testing promise
            conn.execute("ALTER TABLE orders ADD COLUMN test_policy_id INTEGER")
        pcols = {r["name"] for r in conn.execute("PRAGMA table_info(policies)")}
        if "guest_id" not in pcols:  # Testing promises made by console guests: private to them, short-lived
            conn.execute("ALTER TABLE policies ADD COLUMN guest_id TEXT")
            conn.execute("ALTER TABLE policies ADD COLUMN expires_at INTEGER")


SEED_FILE = Path(__file__).with_name("seed_catalog.json")


def seed_if_empty(conn: sqlite3.Connection, path: Path = SEED_FILE) -> int:
    """Load the starting catalog and price promise into an empty database. Returns products added.

    Render's free plan has no persistent disk, so the database starts empty after every deploy or restart.
    """
    if conn.execute("SELECT 1 FROM products LIMIT 1").fetchone() or not path.exists():
        return 0
    seed = json.loads(path.read_text(encoding="utf-8"))
    now = int(time.time())
    with tx(conn):
        conn.executemany(
            "INSERT OR IGNORE INTO products (id, title, image_url, price_cents, currency, source, category, updated_at)"
            " VALUES (?,?,?,?,?,'merchant',?,?)",
            [(p["id"], p["title"], p.get("image_url"), p["price_cents"], p.get("currency", "USD"), p.get("category"), now)
             for p in seed["products"]])
        pol = seed.get("policy")
        if pol and not conn.execute("SELECT 1 FROM policies WHERE active=1").fetchone():
            conn.execute("INSERT INTO policies (text, policy_json, source, active, created_at) VALUES (?,?,?,1,?)",
                         (pol["text"], json.dumps(pol["policy"], separators=(",", ":")), pol.get("source", "rules"), now))
    return len(seed["products"])
