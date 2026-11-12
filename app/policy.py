"""Turn a merchant's plain-English promise into a validated Policy.

LLM first (if configured), deterministic regex fallback second. The merchant always reviews the
compiled result before it goes live, and nothing unvalidated ever reaches the refund path.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time

from pydantic import ValidationError

from . import llm
from .config import get_settings
from .db import tx
from .models import Policy


class PolicyError(ValueError):
    pass


SYSTEM = (
    "You convert a merchant's refund promise into JSON. Output ONLY one JSON object with keys: "
    "window_days (integer days the promise lasts), max_refund_per_order (number, dollars), "
    "daily_budget (number, dollars per day across all orders), min_drop_pct (number, percent; 0 if none). "
    "Use null for any value the merchant did not state. Do not invent values."
)

_CAP = [
    r"(?:max(?:imum)?|up to|capped at|cap of|at most|no more than)\s*(?:of\s*)?\$\s*(\d+(?:\.\d+)?)",
    r"\$\s*(\d+(?:\.\d+)?)\s*(?:per order|an order|each order|/order|per purchase)",
]
_DAILY = [
    r"\$\s*(\d+(?:\.\d+)?)\s*(?:per day|a day|daily|/day|each day)",
    r"daily\s+(?:budget|cap|limit)[^\d$]{0,20}\$?\s*(\d+(?:\.\d+)?)",
]


def _first(patterns: list[str], text: str) -> float | None:
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return float(m.group(1))
    return None


def _rules_parse(text: str) -> dict:
    window = None
    m = re.search(r"(\d+)\s*(day|week)s?", text, re.I)
    if m:
        window = int(m.group(1)) * (7 if m.group(2).lower() == "week" else 1)
    mn = re.search(r"at least\s*(\d+(?:\.\d+)?)\s*%", text, re.I)
    return {
        "window_days": window,
        "max_refund_per_order": _first(_CAP, text),
        "daily_budget": _first(_DAILY, text),
        "min_drop_pct": float(mn.group(1)) if mn else None,
    }


def _build(data: dict, notes: list[str]) -> Policy:
    if data.get("window_days") in (None, 0):
        raise PolicyError("State how long the promise lasts, e.g. 'within 14 days'.")
    if data.get("max_refund_per_order") in (None, 0):
        raise PolicyError("State a maximum refund per order, e.g. 'up to $20 per order'.")
    daily = data.get("daily_budget")
    if daily in (None, 0):
        daily = get_settings().default_daily_budget
        notes.append(f"No daily budget stated; defaulted to ${daily:g}/day. Review before activating.")
    try:
        return Policy(
            window_days=int(data["window_days"]),
            max_refund_per_order=float(data["max_refund_per_order"]),
            daily_budget=float(daily),
            min_drop_pct=float(data.get("min_drop_pct") or 0),
        )
    except (ValidationError, TypeError, ValueError) as e:
        raise PolicyError(f"Promise values out of range: {e}") from e


def compile_policy(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise PolicyError("Write the promise you want to make to customers.")
    if len(text) > 1000:
        raise PolicyError("Keep the promise under 1000 characters.")

    notes: list[str] = []
    data = llm.extract_json(llm.complete(SYSTEM, text, max_tokens=800, json_mode=True))
    if data:
        try:
            return {"policy": _build(data, notes).model_dump(), "source": "llm", "notes": notes}
        except PolicyError as e:
            notes = [f"LLM output rejected ({e}); used the rule-based parser instead."]
    policy = _build(_rules_parse(text), notes)
    return {"policy": policy.model_dump(), "source": "rules", "notes": notes}


# ---- storage ---------------------------------------------------------------------------
def get_active(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The store's real promise (the owner's). Guests' Testing promises never count here."""
    return conn.execute("SELECT * FROM policies WHERE active=1 AND guest_id IS NULL ORDER BY id DESC LIMIT 1").fetchone()


# ---- Testing promises: a console guest can try a promise without changing the store's real one ----------
TEST_TTL_SECONDS = 3 * 3600
TEST_LIMITS = {"window_days": 30, "max_refund_per_order": 100.0, "daily_budget": 200.0}


def clamp_test(policy: Policy) -> tuple[Policy, list[str]]:
    """Keep guest promises within demo-sized limits. Returns the policy and what was lowered."""
    changes, notes = {}, []
    for field, limit in TEST_LIMITS.items():
        if getattr(policy, field) > limit:
            changes[field] = limit
            notes.append(field)
    return (policy.model_copy(update=changes) if changes else policy), notes


def get_test(conn: sqlite3.Connection, guest_id: str | None, now: int | None = None) -> sqlite3.Row | None:
    if not guest_id:
        return None
    now = int(now if now is not None else time.time())
    return conn.execute("SELECT * FROM policies WHERE active=1 AND guest_id=? AND expires_at>? ORDER BY id DESC LIMIT 1",
                        (guest_id, now)).fetchone()


def get_for(conn: sqlite3.Connection, guest_id: str | None, now: int | None = None) -> sqlite3.Row | None:
    """The promise this browser sees: its own unexpired Testing promise, else the store's real one."""
    return get_test(conn, guest_id, now) or get_active(conn)


def activate_test(conn: sqlite3.Connection, guest_id: str, text: str, policy: Policy, source: str,
                  now: int | None = None) -> sqlite3.Row:
    now = int(now if now is not None else time.time())
    with tx(conn):
        conn.execute("UPDATE policies SET active=0 WHERE guest_id=?", (guest_id,))
        cur = conn.execute(
            "INSERT INTO policies (text, policy_json, source, active, created_at, guest_id, expires_at)"
            " VALUES (?,?,?,1,?,?,?)",
            (text.strip(), policy.model_dump_json(), source, now, guest_id, now + TEST_TTL_SECONDS))
    return conn.execute("SELECT * FROM policies WHERE id=?", (cur.lastrowid,)).fetchone()


def end_test(conn: sqlite3.Connection, guest_id: str) -> None:
    conn.execute("UPDATE policies SET active=0 WHERE guest_id=?", (guest_id,))


def activate(conn: sqlite3.Connection, text: str, policy: Policy, source: str) -> int:
    with tx(conn):
        conn.execute("UPDATE policies SET active=0 WHERE guest_id IS NULL")
        cur = conn.execute(
            "INSERT INTO policies (text, policy_json, source, active, created_at) VALUES (?,?,?,1,?)",
            (text.strip(), policy.model_dump_json(), source, int(time.time())),
        )
    return cur.lastrowid


def policy_row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    keys = row.keys()
    testing = "guest_id" in keys and row["guest_id"] is not None
    return {"id": row["id"], "text": row["text"], "source": row["source"],
            "policy": json.loads(row["policy_json"]), "created_at": row["created_at"],
            "testing": testing, "expires_at": row["expires_at"] if testing else None}
