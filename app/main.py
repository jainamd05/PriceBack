"""PriceBack API + static pages.

Public:   /, /api/products, /api/guarantee, /api/checkout, /checkout/return, /api/webhooks/paypal
Merchant: everything else (protected by x-admin-token when ADMIN_TOKEN is set)
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager, closing
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import policy as policy_mod
from . import prices
from .config import get_settings
from .db import connect, init_db, tx
from .models import Policy
from .money import to_cents
from .paypal import PayPalClient, PayPalError
from .watcher import run_watch_once
from .webhooks import process_event

log = logging.getLogger("priceback")
STATIC = Path(__file__).parent / "static"

_paypal: PayPalClient | None = None


def get_paypal() -> PayPalClient:
    global _paypal
    s = get_settings()
    if not (s.paypal_client_id and s.paypal_client_secret):
        raise HTTPException(503, "PayPal sandbox credentials are not configured (PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET).")
    if _paypal is None:
        _paypal = PayPalClient(s.paypal_client_id, s.paypal_client_secret, s.paypal_base_url,
                               s.paypal_webhook_id, s.paypal_allow_live)
    return _paypal


def get_conn():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def require_admin(x_admin_token: str = Header(default="")) -> None:
    token = get_settings().admin_token
    if token and not hmac.compare_digest(x_admin_token, token):
        raise HTTPException(401, "Missing or invalid admin token.")


def _watch_blocking() -> dict:
    with closing(connect()) as conn:
        return run_watch_once(conn, get_paypal())


async def _watch_loop(interval: int) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            r = await asyncio.to_thread(_watch_blocking)
            if r["refunds"] or r["blocked"] or r["recovered"]:
                log.info("watcher: %s", {k: v for k, v in r.items() if k != "results"})
        except Exception:  # noqa: BLE001
            log.exception("watcher pass failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    s = get_settings()
    with closing(connect()) as conn:
        conn.execute("DELETE FROM products WHERE source='demo'")  # the old built-in sample catalog is gone
        if s.channel3_api_key and not conn.execute(
                "SELECT 1 FROM products WHERE source='channel3' LIMIT 1").fetchone():
            r = prices.refresh_catalog(conn)  # import the catalog so checkout works before anyone visits
            if r["error"]:
                log.warning("Channel3 catalog import failed: %s", r["error"])
    if s.public_base_url.startswith("https://") and not s.admin_token:
        log.warning("ADMIN_TOKEN is not set on a public deployment: merchant endpoints are open.")
    task = asyncio.create_task(_watch_loop(s.watch_interval_seconds)) if s.watch_interval_seconds > 0 else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="PriceBack", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---- pages -------------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/product/{product_id}", include_in_schema=False)
def product_page(product_id: str):
    return FileResponse(STATIC / "product.html")


@app.get("/merchant", include_in_schema=False)
def merchant_page():
    return FileResponse(STATIC / "merchant.html")


# ---- public API --------------------------------------------------------------------------
def _product_out(r) -> dict:
    return {"id": r["id"], "title": r["title"], "image_url": r["image_url"],
            "price_cents": r["price_cents"], "currency": r["currency"], "source": r["source"],
            "category": r["category"]}


@app.get("/api/products")
def products(conn=Depends(get_conn)):
    return [_product_out(r) for r in prices.list_products(conn)]


@app.get("/api/guarantee")
def guarantee(conn=Depends(get_conn)):
    row = policy_mod.get_active(conn)
    if not row:
        return {"active": False}
    p = Policy.model_validate_json(row["policy_json"])
    return {"active": True, "text": row["text"], "window_days": p.window_days,
            "max_refund_per_order": p.max_refund_per_order, "min_drop_pct": p.min_drop_pct}


class CheckoutIn(BaseModel):
    product_id: str


@app.post("/api/checkout")
def checkout(body: CheckoutIn, conn=Depends(get_conn), pp: PayPalClient = Depends(get_paypal)):
    prod = conn.execute("SELECT * FROM products WHERE id=?", (body.product_id,)).fetchone()
    if not prod:
        raise HTTPException(404, "Unknown product.")
    s = get_settings()
    try:  # price always comes from our catalog, never from the client
        res = pp.create_order(amount_cents=prod["price_cents"], currency=prod["currency"],
                              description=prod["title"], reference_id=prod["id"],
                              return_url=f"{s.public_base_url}/checkout/return",
                              cancel_url=f"{s.public_base_url}/checkout/cancel")
    except PayPalError as e:
        log.error("create_order failed: %s", e)
        raise HTTPException(502, "Could not start PayPal checkout.") from e
    conn.execute(
        "INSERT INTO orders (paypal_order_id, product_id, title, paid_cents, currency, status, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (res["id"], prod["id"], prod["title"], prod["price_cents"], prod["currency"], "CREATED", int(time.time())))
    return {"paypal_order_id": res["id"], "approve_url": res["approve_url"]}


@app.get("/checkout/return", include_in_schema=False)
def checkout_return(token: str, conn=Depends(get_conn), pp: PayPalClient = Depends(get_paypal)):
    order = conn.execute("SELECT * FROM orders WHERE paypal_order_id=?", (token,)).fetchone()
    if not order:
        return RedirectResponse("/?error=unknown_order")
    # Claim the order atomically so a double-load of this URL can't capture twice.
    claimed = conn.execute("UPDATE orders SET status='CAPTURING' WHERE id=? AND status='CREATED'",
                           (order["id"],)).rowcount
    if claimed != 1:
        return RedirectResponse(f"/?paid={order['id']}")
    try:
        cap = pp.capture_order(token)
    except PayPalError as e:
        log.error("capture failed for %s: %s", token, e)
        conn.execute("UPDATE orders SET status='CAPTURE_FAILED' WHERE id=?", (order["id"],))
        return RedirectResponse("/?error=capture_failed")

    if cap["capture_status"] != "COMPLETED":
        conn.execute("UPDATE orders SET status=?, capture_id=? WHERE id=?",
                     (f"CAPTURE_{cap['capture_status']}", cap["capture_id"], order["id"]))
        return RedirectResponse(f"/?pending={order['id']}")  # e.g. eCheck: money not settled, not covered
    elif cap["amount_cents"] != order["paid_cents"] or cap["currency"] != order["currency"]:
        conn.execute("UPDATE orders SET status='AMOUNT_MISMATCH', capture_id=? WHERE id=?",
                     (cap["capture_id"], order["id"]))
    else:
        active = policy_mod.get_active(conn)  # snapshot the promise in force right now
        conn.execute(
            "UPDATE orders SET status='CAPTURED', capture_id=?, captured_at=?, policy_json=? WHERE id=?",
            (cap["capture_id"], int(time.time()), active["policy_json"] if active else None, order["id"]))
    return RedirectResponse(f"/?paid={order['id']}")


@app.get("/checkout/cancel", include_in_schema=False)
def checkout_cancel():
    return RedirectResponse("/?cancelled=1")


@app.post("/api/webhooks/paypal")
async def paypal_webhook(request: Request, pp: PayPalClient = Depends(get_paypal)):
    s = get_settings()
    raw = (await request.body()).decode("utf-8")
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON.")
    if not s.paypal_webhook_id and not s.allow_unverified_webhooks:
        raise HTTPException(503, "PAYPAL_WEBHOOK_ID is not configured.")

    verified = False
    if s.paypal_webhook_id:
        headers ={k.lower(): v for k, v in request.headers.items()}
        try:
            verified = await asyncio.to_thread(pp.verify_webhook, headers, raw)
        except PayPalError as e:  # 503 so PayPal retries the delivery
            log.error("webhook verification call failed: %s", e)
            raise HTTPException(503, "Could not verify webhook right now.") from e
        if not verified:
            raise HTTPException(400, "Webhook signature verification failed.")

    try:
        with closing(connect()) as conn:
            result = process_event(conn, event, verified)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "result": result, "verified": verified}


# ---- merchant API ------------------------------------------------------------------------
admin = [Depends(require_admin)]


class CompileIn(BaseModel):
    text: str


@app.post("/api/policy/compile", dependencies=admin)
def policy_compile(body: CompileIn):
    try:
        return policy_mod.compile_policy(body.text)
    except policy_mod.PolicyError as e:
        raise HTTPException(422, str(e)) from e


class ActivateIn(BaseModel):
    text: str
    policy: Policy
    source: str = "rules"


@app.post("/api/policy/activate", dependencies=admin)
def policy_activate(body: ActivateIn, conn=Depends(get_conn)):
    pid = policy_mod.activate(conn, body.text, body.policy, body.source if body.source in ("llm", "rules") else "rules")
    return {"id": pid, "active": True}


@app.get("/api/policy", dependencies=admin)
def policy_get(conn=Depends(get_conn)):
    return policy_mod.policy_row_to_dict(policy_mod.get_active(conn))


@app.post("/api/catalog/refresh", dependencies=admin)
def catalog_refresh(conn=Depends(get_conn)):
    return prices.refresh_catalog(conn)


class ProductIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    price: float = Field(gt=0, le=100000)
    image_url: str | None = Field(default=None, max_length=2000)
    category: str | None = Field(default=None, max_length=60)


@app.post("/api/products", dependencies=admin)
def product_add(body: ProductIn, conn=Depends(get_conn)):
    try:
        pid = prices.add_product(conn, body.title, to_cents(body.price), (body.image_url or "").strip() or None,
                                 category=body.category)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return _product_out(conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone())


class ProductEdit(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    image_url: str | None = Field(default=None, max_length=2000)
    category: str | None = Field(default=None, max_length=60)


@app.patch("/api/products/{product_id}", dependencies=admin)
def product_edit(product_id: str, body: ProductEdit, conn=Depends(get_conn)):
    try:
        prices.edit_product(conn, product_id, title=body.title,
                            image_url=None if body.image_url is None else body.image_url.strip(),
                            category=body.category)
    except KeyError:
        raise HTTPException(404, "Unknown product.")
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return _product_out(conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone())


@app.delete("/api/products/{product_id}", dependencies=admin)
def product_delete(product_id: str, conn=Depends(get_conn)):
    try:
        prices.delete_product(conn, product_id)
    except KeyError:
        raise HTTPException(404, "Unknown product.")
    return {"ok": True}


class DropIn(BaseModel):
    product_id: str
    drop_pct: float | None = Field(default=None, gt=0, lt=100)
    new_price: float | None = Field(default=None, gt=0)


@app.post("/api/simulate/drop", dependencies=admin)
def simulate_drop(body: DropIn, conn=Depends(get_conn)):
    try:
        cents = prices.simulate_drop(conn, body.product_id, drop_pct=body.drop_pct, new_price=body.new_price)
    except KeyError:
        raise HTTPException(404, "Unknown product.")
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return {"product_id": body.product_id, "simulated_price_cents": cents, "price_source": "simulated"}


class ResetIn(BaseModel):
    product_id: str | None = None


@app.post("/api/simulate/reset", dependencies=admin)
def simulate_reset(body: ResetIn, conn=Depends(get_conn)):
    prices.reset_simulation(conn, body.product_id)
    return {"ok": True}


@app.post("/api/watch/run", dependencies=admin)
def watch_run(conn=Depends(get_conn), pp: PayPalClient = Depends(get_paypal)):
    return run_watch_once(conn, pp)


def _rows(conn, sql: str, args: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


@app.get("/api/orders", dependencies=admin)
def orders(conn=Depends(get_conn)):
    return _rows(conn, "SELECT id, paypal_order_id, capture_id, product_id, title, paid_cents, currency, status,"
                       " (policy_json IS NOT NULL) AS guaranteed, created_at, captured_at, capture_webhook_at"
                       " FROM orders ORDER BY id DESC LIMIT 200")


@app.get("/api/settlements", dependencies=admin)
def settlements(conn=Depends(get_conn)):
    return _rows(conn, "SELECT s.id, s.order_id, o.title, s.action, s.amount_cents, s.reason, s.market_cents,"
                       " s.price_source, s.status, s.paypal_refund_id, s.explanation, s.error,"
                       " s.webhook_confirmed_at, s.created_at FROM settlements s JOIN orders o ON o.id=s.order_id"
                       " ORDER BY s.id DESC LIMIT 200")


@app.get("/api/ledger", dependencies=admin)
def ledger(conn=Depends(get_conn)):
    return _rows(conn, "SELECT event_id, event_type, resource_id, verified, received_at FROM webhook_events"
                       " ORDER BY received_at DESC LIMIT 200")


@app.get("/api/stats", dependencies=admin)
def stats(conn=Depends(get_conn)):
    now = int(time.time())
    day_start = now - now % 86400
    active = policy_mod.get_active(conn)
    daily_budget = to_cents(Policy.model_validate_json(active["policy_json"]).daily_budget) if active else 0
    guaranteed = conn.execute("SELECT * FROM orders WHERE status='CAPTURED' AND policy_json IS NOT NULL").fetchall()
    refunded_by_order = {r["order_id"]: r["t"] for r in conn.execute(
        "SELECT order_id, SUM(amount_cents) AS t FROM settlements WHERE action='refund' AND status!='FAILED' "
        "GROUP BY order_id")}
    exposure = 0
    for o in guaranteed:  # worst case still payable on orders whose window is open
        p = Policy.model_validate_json(o["policy_json"])
        if o["captured_at"] and now <= o["captured_at"] + p.window_days * 86400:
            exposure += max(0, min(to_cents(p.max_refund_per_order), o["paid_cents"]) - refunded_by_order.get(o["id"], 0))
    refunded_today = conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) FROM settlements WHERE action='refund' AND status!='FAILED'"
        " AND created_at>=?", (day_start,)).fetchone()[0]
    return {
        "guaranteed_orders": len(guaranteed),
        "revenue_cents": sum(o["paid_cents"] for o in guaranteed),
        "refunded_cents": sum(refunded_by_order.values()),
        "refunded_today_cents": refunded_today,
        "daily_budget_cents": daily_budget,
        "daily_remaining_cents": max(0, daily_budget - refunded_today) if active else 0,
        "open_exposure_cents": exposure,
        "blocked_count": conn.execute("SELECT COUNT(*) FROM settlements WHERE action='blocked'").fetchone()[0],
        "simulated_products": [r["product_id"] for r in conn.execute("SELECT product_id FROM price_overrides")],
        "simulated_prices": {r["product_id"]: r["price_cents"] for r in conn.execute(
            "SELECT product_id, price_cents FROM price_overrides")},
    }
