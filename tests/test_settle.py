import httpx

from app.models import Policy
from app.paypal import PayPalError
from app.settle import evaluate, recover_pending, settle_order
from conftest import make_order

NOW = 1_800_000_000
P = Policy(window_days=14, max_refund_per_order=20, daily_budget=100)


def ev(**kw):
    base = dict(paid_cents=10000, captured_at=NOW, now=NOW + 3600, policy=P, market_cents=8000,
                refunded_cents=0, refunded_today_cents=0, daily_budget_cents=10000)
    base.update(kw)
    return evaluate(**base)


# ---- pure guardrails ----------------------------------------------------------------------
def test_full_difference():
    d = ev()
    assert (d.action, d.amount_cents, d.reason) == ("refund", 2000, "full_difference")


def test_capped_by_order_cap():
    d = ev(market_cents=5000)  # drop 5000, cap 2000
    assert (d.action, d.amount_cents, d.reason) == ("refund", 2000, "capped_by_order_cap")


def test_cap_is_cumulative():
    d = ev(market_cents=5000, refunded_cents=1500)  # owed 3500, only 500 of cap left
    assert (d.action, d.amount_cents) == ("refund", 500)


def test_cap_reached_blocks():
    d = ev(market_cents=5000, refunded_cents=2000)
    assert (d.action, d.reason) == ("blocked", "order_cap_reached")


def test_daily_budget_partial_and_exhausted():
    assert ev(refunded_today_cents=9500, daily_budget_cents=10000).amount_cents == 500
    assert ev(refunded_today_cents=9500, daily_budget_cents=10000).reason == "capped_by_daily_budget"
    d = ev(refunded_today_cents=10000, daily_budget_cents=10000)
    assert (d.action, d.reason) == ("blocked", "daily_budget_exhausted")


def test_skips():
    assert ev(now=NOW + 15 * 86400).reason == "window_expired"
    assert ev(market_cents=10000).reason == "no_drop"
    assert ev(market_cents=12000).reason == "no_drop"
    assert ev(market_cents=0).reason == "invalid_price"
    assert ev(captured_at=None).reason == "not_captured"
    assert ev(refunded_cents=2000, market_cents=8000).reason == "already_refunded"
    p = Policy(window_days=14, max_refund_per_order=20, daily_budget=100, min_drop_pct=5)
    assert ev(policy=p, market_cents=9700).reason == "below_threshold"  # 3% drop
    assert ev(policy=p, market_cents=9400).action == "refund"          # 6% drop


# ---- engine with fake PayPal --------------------------------------------------------------
def test_refund_happens_once_and_is_audited(conn, paypal):
    order = make_order(conn, captured_at=NOW)
    r = settle_order(conn, paypal, order, 8000, "simulated", now=NOW + 60)
    assert r["status"] == "COMPLETED" and r["amount_cents"] == 2000
    assert "dropped" in r["explanation"]
    again = settle_order(conn, paypal, order, 8000, "simulated", now=NOW + 120)
    assert again == {"action": "skip", "reason": "already_refunded"}
    assert len(paypal.refund_calls) == 1
    row = conn.execute("SELECT * FROM settlements").fetchone()
    assert row["price_source"] == "simulated" and row["paypal_refund_id"] == "REF-1"


def test_deeper_drop_refunds_incremental_difference_with_new_key(conn, paypal):
    order = make_order(conn, captured_at=NOW, policy=Policy(window_days=14, max_refund_per_order=50, daily_budget=500))
    settle_order(conn, paypal, order, 8000, "simulated", now=NOW + 60)
    r2 = settle_order(conn, paypal, order, 7000, "simulated", now=NOW + 120)
    assert r2["amount_cents"] == 1000  # total 3000 = full 3000 drop, nothing double-paid
    assert len(set(paypal.refund_calls)) == 2


def test_blocked_is_logged_once(conn, paypal):
    order = make_order(conn, captured_at=NOW)
    settle_order(conn, paypal, order, 5000, "simulated", now=NOW + 60)  # refunds cap 2000
    for t in (120, 180):
        assert settle_order(conn, paypal, order, 4000, "simulated", now=NOW + t)["reason"] == "order_cap_reached"
    blocked = conn.execute("SELECT COUNT(*) FROM settlements WHERE action='blocked'").fetchone()[0]
    assert blocked == 1
    assert len(paypal.refund_calls) == 1


def test_daily_budget_shared_across_orders(conn, paypal):
    pol = Policy(window_days=14, max_refund_per_order=20, daily_budget=30)
    o1 = make_order(conn, captured_at=NOW, policy=pol, n=1)
    o2 = make_order(conn, captured_at=NOW, policy=pol, n=2)
    assert settle_order(conn, paypal, o1, 8000, "simulated", now=NOW + 60)["amount_cents"] == 2000
    r2 = settle_order(conn, paypal, o2, 8000, "simulated", now=NOW + 61)
    assert (r2["amount_cents"], r2["reason"]) == (1000, "capped_by_daily_budget")


def test_unknown_outcome_stays_pending_and_recovers_with_same_key(conn, paypal):
    order = make_order(conn, captured_at=NOW)
    paypal.fail_with = httpx.ConnectError("boom")
    r = settle_order(conn, paypal, order, 8000, "simulated", now=NOW + 60)
    assert r["status"] == "pending_api"
    # while pending it still counts against the cap, so a re-run can't create a second refund
    assert settle_order(conn, paypal, order, 8000, "simulated", now=NOW + 61)["reason"] == "already_refunded"

    paypal.fail_with = None
    assert recover_pending(conn, paypal, now=NOW + 200) == 1
    row = conn.execute("SELECT * FROM settlements").fetchone()
    assert row["status"] == "COMPLETED" and row["paypal_refund_id"] == "REF-1"
    assert len(set(paypal.refund_calls)) == 1  # same idempotency key used on retry


def test_server_error_is_treated_as_unknown_not_failed(conn, paypal):
    order = make_order(conn, captured_at=NOW)
    paypal.fail_with = PayPalError("PayPal 503", status=503)
    assert settle_order(conn, paypal, order, 8000, "simulated", now=NOW + 60)["status"] == "pending_api"


def test_definite_rejection_is_failed_and_retried_with_new_key(conn, paypal):
    order = make_order(conn, captured_at=NOW)
    paypal.fail_with = PayPalError("PayPal 422: CAPTURE_FULLY_REFUNDED", status=422)
    assert settle_order(conn, paypal, order, 8000, "simulated", now=NOW + 60)["status"] == "FAILED"
    paypal.fail_with = None  # failed rows don't count against the cap
    r = settle_order(conn, paypal, order, 8000, "simulated", now=NOW + 120)
    assert r["status"] == "COMPLETED"
    keys = [x["idempotency_key"] for x in conn.execute("SELECT idempotency_key FROM settlements ORDER BY id")]
    assert len(keys) == 2 and keys[0] != keys[1]


def test_order_without_policy_is_never_refunded(conn, paypal):
    order = make_order(conn, captured_at=NOW)
    conn.execute("UPDATE orders SET policy_json=NULL WHERE id=?", (order["id"],))
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order["id"],)).fetchone()
    assert settle_order(conn, paypal, order, 1000, "simulated", now=NOW + 60)["reason"] == "not_guaranteed"
    assert paypal.refund_calls == []
