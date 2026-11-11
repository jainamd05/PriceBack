"""Settlement engine: decide (pure), then refund (idempotent, audited).

Safety properties:
  * evaluate() is a pure function: all guardrails live there and are unit-tested.
  * The decision and a write-ahead 'pending_api' row are committed in one BEGIN IMMEDIATE
    transaction, so concurrent runs can't both spend the same cap/budget.
  * PayPal-Request-Id is derived from (order, market price, refunded so far, attempt), so a retry of
    the same intent can never refund twice.
  * Ambiguous outcomes (timeouts, 5xx, 429) stay 'pending_api' and are retried with the SAME key.
    Only definite 4xx rejections are marked FAILED.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

import httpx

from . import llm
from .db import tx
from .models import Policy
from .money import fmt_amount, to_cents
from .paypal import PayPalClient, PayPalError
from .policy import get_active


@dataclass(frozen=True)
class Decision:
    action: str            # 'skip' | 'blocked' | 'refund'
    amount_cents: int = 0
    reason: str = ""


def evaluate(*, paid_cents: int, captured_at: int | None, now: int, policy: Policy, market_cents: int,
             refunded_cents: int, refunded_today_cents: int, daily_budget_cents: int) -> Decision:
    if captured_at is None:
        return Decision("skip", 0, "not_captured")
    if market_cents <= 0:
        return Decision("skip", 0, "invalid_price")
    if now > captured_at + policy.window_days * 86400:
        return Decision("skip", 0, "window_expired")
    drop = paid_cents - market_cents
    if drop <= 0:
        return Decision("skip", 0, "no_drop")
    if policy.min_drop_pct and drop * 100 < policy.min_drop_pct * paid_cents:
        return Decision("skip", 0, "below_threshold")
    owed = drop - refunded_cents
    if owed <= 0:
        return Decision("skip", 0, "already_refunded")
    order_remaining = to_cents(policy.max_refund_per_order) - refunded_cents
    if order_remaining <= 0:
        return Decision("blocked", 0, "order_cap_reached")
    daily_remaining = daily_budget_cents - refunded_today_cents
    if daily_remaining <= 0:
        return Decision("blocked", 0, "daily_budget_exhausted")
    amount = min(owed, order_remaining, daily_remaining)
    if amount == owed:
        reason = "full_difference"
    elif amount == order_remaining:
        reason = "capped_by_order_cap"
    else:
        reason = "capped_by_daily_budget"
    return Decision("refund", amount, reason)


# ---- helpers ---------------------------------------------------------------------------
def _refunded_for_order(conn: sqlite3.Connection, order_id: int) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) FROM settlements "
        "WHERE order_id=? AND action='refund' AND status!='FAILED'", (order_id,)).fetchone()[0]


def _refunded_today(conn: sqlite3.Connection, now: int) -> int:
    day_start = now - (now % 86400)  # UTC day
    return conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) FROM settlements "
        "WHERE action='refund' AND status!='FAILED' AND created_at>=?", (day_start,)).fetchone()[0]


def explain(title: str, paid_cents: int, market_cents: int, amount_cents: int, reason: str) -> str:
    cap_note = {
        "capped_by_order_cap": " This is the maximum our guarantee covers on a single order.",
        "capped_by_daily_budget": " Our daily guarantee budget limited this refund.",
    }.get(reason, "")
    fallback = (f"The price of {title} dropped from ${fmt_amount(paid_cents)} to ${fmt_amount(market_cents)}. "
                f"Under our price-drop guarantee we refunded ${fmt_amount(amount_cents)} to your PayPal account."
                + cap_note)
    facts = {"product": title, "paid": fmt_amount(paid_cents), "new_price": fmt_amount(market_cents),
             "refunded": fmt_amount(amount_cents), "limited_by": reason}
    text = llm.complete(
        "Write a friendly, factual 2-sentence message to a customer explaining a price-drop refund. "
        "Use ONLY the facts given, in USD. No promises beyond them, no emojis.",
        json.dumps(facts), max_tokens=400)
    return text or fallback


def _definite_failure(e: PayPalError) -> bool:
    return e.status is not None and 400 <= e.status < 500 and e.status != 429


def _send_refund(conn: sqlite3.Connection, paypal: PayPalClient, *, settlement_id: int, capture_id: str,
                 currency: str, amount_cents: int, key: str, title: str, paid_cents: int,
                 market_cents: int, reason: str) -> dict:
    out = {"action": "refund", "amount_cents": amount_cents, "reason": reason, "settlement_id": settlement_id}
    try:
        res = paypal.refund_capture(capture_id=capture_id, amount_cents=amount_cents, currency=currency,
                                    request_id=key, note=f"PriceBack price-drop refund: {title}")
    except PayPalError as e:
        if _definite_failure(e):
            conn.execute("UPDATE settlements SET status='FAILED', error=? WHERE id=?", (str(e)[:500], settlement_id))
            return {**out, "status": "FAILED", "error": str(e)}
        conn.execute("UPDATE settlements SET error=? WHERE id=?", (str(e)[:500], settlement_id))
        return {**out, "status": "pending_api", "error": str(e)}
    except httpx.HTTPError as e:  # outcome unknown: keep pending, retry with same key
        conn.execute("UPDATE settlements SET error=? WHERE id=?", (f"network: {e}"[:500], settlement_id))
        return {**out, "status": "pending_api", "error": str(e)}

    explanation = explain(title, paid_cents, market_cents, amount_cents, reason)
    conn.execute("UPDATE settlements SET status=?, paypal_refund_id=?, explanation=?, error=NULL WHERE id=?",
                 (res["status"], res["id"], explanation, settlement_id))
    return {**out, "status": res["status"], "paypal_refund_id": res["id"], "explanation": explanation}


# ---- public API ------------------------------------------------------------------------
def settle_order(conn: sqlite3.Connection, paypal: PayPalClient, order: sqlite3.Row, market_cents: int,
                 price_source: str, now: int | None = None) -> dict:
    now = int(now if now is not None else time.time())
    if order["status"] != "CAPTURED" or not order["policy_json"] or not order["capture_id"]:
        return {"action": "skip", "reason": "not_guaranteed"}
    policy = Policy.model_validate_json(order["policy_json"])
    active = get_active(conn)
    # Per-order terms come from the promise in force at purchase; the daily budget is a
    # merchant-level guardrail and follows the currently active policy.
    daily_cents = to_cents((Policy.model_validate_json(active["policy_json"]) if active else policy).daily_budget)

    with tx(conn):
        refunded = _refunded_for_order(conn, order["id"])
        d = evaluate(paid_cents=order["paid_cents"], captured_at=order["captured_at"], now=now, policy=policy,
                     market_cents=market_cents, refunded_cents=refunded,
                     refunded_today_cents=_refunded_today(conn, now), daily_budget_cents=daily_cents)
        if d.action == "skip":
            return {"action": "skip", "reason": d.reason}
        if d.action == "blocked":
            last = conn.execute("SELECT action, reason, market_cents FROM settlements WHERE order_id=? "
                                "ORDER BY id DESC LIMIT 1", (order["id"],)).fetchone()
            if not (last and last["action"] == "blocked" and last["reason"] == d.reason
                    and last["market_cents"] == market_cents):  # don't spam the audit log
                conn.execute(
                    "INSERT INTO settlements (order_id, action, amount_cents, reason, market_cents, price_source,"
                    " status, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (order["id"], "blocked", 0, d.reason, market_cents, price_source, "blocked", now))
            return {"action": "blocked", "reason": d.reason}

        base = f"pb-{order['id']}-{market_cents}-{refunded}"
        attempt = conn.execute("SELECT COUNT(*) FROM settlements WHERE idempotency_key LIKE ?",
                               (base + "-%",)).fetchone()[0]
        key = f"{base}-{attempt}"
        cur = conn.execute(
            "INSERT INTO settlements (order_id, action, amount_cents, reason, market_cents, price_source, status,"
            " idempotency_key, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (order["id"], "refund", d.amount_cents, d.reason, market_cents, price_source, "pending_api", key, now))
        settlement_id = cur.lastrowid

    return _send_refund(conn, paypal, settlement_id=settlement_id, capture_id=order["capture_id"],
                        currency=order["currency"], amount_cents=d.amount_cents, key=key, title=order["title"],
                        paid_cents=order["paid_cents"], market_cents=market_cents, reason=d.reason)


def recover_pending(conn: sqlite3.Connection, paypal: PayPalClient, older_than: int = 30,
                    now: int | None = None) -> int:
    """Re-send refunds whose outcome is unknown, with the ORIGINAL idempotency key (safe to repeat)."""
    now = int(now if now is not None else time.time())
    rows = conn.execute(
        "SELECT s.*, o.capture_id, o.currency, o.title, o.paid_cents FROM settlements s "
        "JOIN orders o ON o.id=s.order_id "
        "WHERE s.action='refund' AND s.status='pending_api' AND s.created_at<=?", (now - older_than,)).fetchall()
    for r in rows:
        _send_refund(conn, paypal, settlement_id=r["id"], capture_id=r["capture_id"], currency=r["currency"],
                     amount_cents=r["amount_cents"], key=r["idempotency_key"], title=r["title"],
                     paid_cents=r["paid_cents"], market_cents=r["market_cents"], reason=r["reason"])
    return len(rows)
