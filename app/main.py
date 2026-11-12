"""PriceBack API + static pages.

Public:   /, /product/{id}, /account, /api/products, /api/guarantee, /api/demo, /checkout/return, /api/webhooks/paypal
Shopper:  /api/auth/*, /api/me*, /api/checkout (signed in with an emailed one-time code; session cookie)
Merchant: everything else (protected by x-admin-token when ADMIN_TOKEN is set)
"""
from __future__ import annotations

import asyncio
import hmac
import secrets
import json
import logging
import time
from collections import deque
from contextlib import asynccontextmanager, closing
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import notify, shoppers
from . import policy as policy_mod
from . import prices
from .config import get_settings
from .db import connect, init_db, seed_if_empty, tx
from .models import Policy
from .money import to_cents
from .paypal import PayPalClient, PayPalError
from .settle import settle_order
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


def current_shopper(pb_session: str | None = Cookie(default=None), conn=Depends(get_conn)) -> str | None:
    return shoppers.session_email(conn, pb_session)


def require_shopper(email: str | None = Depends(current_shopper)) -> str:
    if not email:
        raise HTTPException(401, "Sign in with your email first.")
    return email


def _is_owner(token: str) -> bool:
    expected = get_settings().admin_token
    return not expected or (bool(token) and hmac.compare_digest(token.encode(), expected.encode()))


def require_admin(x_admin_token: str = Header(default=""), x_demo_guest: str = Header(default="")) -> None:
    """Owner only: the catalog."""
    if _is_owner(x_admin_token):
        return
    if x_demo_guest == "1" and get_settings().demo_merchant_guest:
        raise HTTPException(403, "Guest mode can't change the catalog. Unlock the console with the admin token to do that.")
    raise HTTPException(401, "Missing or invalid admin token.")


GUEST_COOKIE = "pb_gid"


class Merchant(BaseModel):
    role: str                   # "owner" | "guest"
    guest_id: str | None = None


def merchant_role(response: Response, x_admin_token: str = Header(default=""), x_demo_guest: str = Header(default=""),
                  pb_gid: str | None = Cookie(default=None)) -> Merchant:
    """Owner, or a guest when DEMO_MERCHANT_GUEST is on: the console, the promise and the price lab.

    Each guest browser gets a random id cookie, so its Testing promise is shown only in that browser's store."""
    if _is_owner(x_admin_token):
        return Merchant(role="owner")
    if x_demo_guest == "1" and get_settings().demo_merchant_guest:
        gid = pb_gid if pb_gid and len(pb_gid) == 32 and pb_gid.isalnum() else None
        if not gid:
            gid = secrets.token_hex(16)
            response.set_cookie(GUEST_COOKIE, gid, max_age=7 * 86400, httponly=True, samesite="lax",
                                secure=get_settings().public_base_url.startswith("https://"))
        return Merchant(role="guest", guest_id=gid)
    raise HTTPException(401, "Missing or invalid admin token.")


GUEST_COMPILES_PER_HOUR = 30
_guest_compiles: deque[float] = deque()


def _guest_compile_allowed(now: float) -> bool:
    """Every guest shares one hourly allowance of AI promise compiles, so the Gemini quota can't be drained."""
    while _guest_compiles and _guest_compiles[0] < now - 3600:
        _guest_compiles.popleft()
    if len(_guest_compiles) >= GUEST_COMPILES_PER_HOUR:
        return False
    _guest_compiles.append(now)
    return True


def _mask_email(e: str | None) -> str | None:
    if not e or "@" not in e:
        return e
    name, domain = e.split("@", 1)
    return f"{name[:2]}•••@{domain}"


def _watch_blocking() -> dict:
    with closing(connect()) as conn:
        r = run_watch_once(conn, get_paypal())
        notify.flush(conn)
        return r


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
        if s.seed_catalog and (n := seed_if_empty(conn)):
            log.info("loaded %s products and the price promise from seed_catalog.json", n)
        if s.channel3_api_key and not conn.execute("SELECT 1 FROM products LIMIT 1").fetchone():
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


@app.get("/account", include_in_schema=False)
def account_page():
    return FileResponse(STATIC / "account.html")


# ---- public API --------------------------------------------------------------------------
def _product_out(r) -> dict:
    return {"id": r["id"], "title": r["title"], "image_url": r["image_url"],
            "price_cents": r["price_cents"], "currency": r["currency"], "source": r["source"],
            "category": r["category"]}


@app.get("/api/products")
def products(conn=Depends(get_conn)):
    return [_product_out(r) for r in prices.list_products(conn)]


@app.get("/api/guarantee")
def guarantee(conn=Depends(get_conn), pb_gid: str | None = Cookie(default=None)):
    """The promise this browser shoppers see: a console guest sees their own Testing promise, labelled so."""
    row = policy_mod.get_for(conn, pb_gid)
    if not row:
        return {"active": False, "testing": False}
    p = Policy.model_validate_json(row["policy_json"])
    testing = row["guest_id"] is not None
    return {"active": True, "text": row["text"], "window_days": p.window_days,
            "max_refund_per_order": p.max_refund_per_order, "min_drop_pct": p.min_drop_pct,
            "testing": testing, "expires_at": row["expires_at"] if testing else None}


class CheckoutIn(BaseModel):
    product_id: str


@app.post("/api/checkout")
def checkout(body: CheckoutIn, shopper: str = Depends(require_shopper), conn=Depends(get_conn),
             pp: PayPalClient = Depends(get_paypal)):
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
        "INSERT INTO orders (paypal_order_id, product_id, title, paid_cents, currency, status, created_at, buyer_email)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (res["id"], prod["id"], prod["title"], prod["price_cents"], prod["currency"], "CREATED", int(time.time()), shopper))
    return {"paypal_order_id": res["id"], "approve_url": res["approve_url"]}


@app.get("/checkout/return", include_in_schema=False)
def checkout_return(token: str, conn=Depends(get_conn), pp: PayPalClient = Depends(get_paypal),
                    pb_gid: str | None = Cookie(default=None)):
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
        active = policy_mod.get_for(conn, pb_gid)  # snapshot the promise in force for this buyer right now
        test_id = active["id"] if active and active["guest_id"] else None
        conn.execute(
            "UPDATE orders SET status='CAPTURED', capture_id=?, captured_at=?, policy_json=?, test_policy_id=?"
            " WHERE id=?",
            (cap["capture_id"], int(time.time()), active["policy_json"] if active else None, test_id, order["id"]))
        notify.flush(conn)  # "order confirmed, price protected until …"
    return RedirectResponse(f"/?paid={order['id']}")


@app.get("/checkout/cancel", include_in_schema=False)
def checkout_cancel():
    return RedirectResponse("/?cancelled=1")


# ---- shopper accounts ----------------------------------------------------------------------
@app.get("/api/demo")
def demo_info():
    """What a tester needs to try the prototype: the PayPal *sandbox* buyer login (fake money only)."""
    s = get_settings()
    return {"buyer_email": s.demo_buyer_email or None, "buyer_password": s.demo_buyer_password or None,
            "self_serve_drops": s.demo_self_serve, "email_enabled": notify.email_configured(),
            "merchant_guest": s.demo_merchant_guest}


class AuthStartIn(BaseModel):
    email: str = Field(max_length=254)


class AuthVerifyIn(BaseModel):
    email: str = Field(max_length=254)
    code: str = Field(max_length=12)


def _auth_error(e: shoppers.AuthError) -> HTTPException:
    return HTTPException(e.status, str(e))


@app.post("/api/auth/start")
def auth_start(body: AuthStartIn, conn=Depends(get_conn)):
    s = get_settings()
    local = not s.public_base_url.startswith("https://")
    if not notify.email_configured() and not local:
        raise HTTPException(503, "Email sign-in is not configured on this server (BREVO_API_KEY / MAIL_FROM).")
    try:
        email = shoppers.normalize(body.email)
        code = shoppers.start(conn, email)
    except shoppers.AuthError as e:
        raise _auth_error(e) from e
    if notify.email_configured():
        try:
            notify.send_login_code(email, code)
        except Exception as e:  # noqa: BLE001
            log.error("sign-in email to %s failed: %s", email, e)
            raise HTTPException(502, "We couldn't send the email. Check the address and try again.") from e
        return {"sent": True, "email": email}
    log.warning("No email provider configured: sign-in code for %s is %s", email, code)
    return {"sent": False, "email": email, "dev_code": code}  # local development only


@app.post("/api/auth/verify")
def auth_verify(body: AuthVerifyIn, response: Response, conn=Depends(get_conn)):
    try:
        token = shoppers.verify(conn, body.email, body.code)
    except shoppers.AuthError as e:
        raise _auth_error(e) from e
    response.set_cookie(shoppers.COOKIE, token, max_age=shoppers.SESSION_TTL, httponly=True, samesite="lax",
                        secure=get_settings().public_base_url.startswith("https://"), path="/")
    return {"email": shoppers.normalize(body.email)}


@app.post("/api/auth/logout")
def auth_logout(response: Response, pb_session: str | None = Cookie(default=None), conn=Depends(get_conn)):
    shoppers.logout(conn, pb_session)
    response.delete_cookie(shoppers.COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(email: str | None = Depends(current_shopper)):
    return {"email": email}


def _my_order(conn, o) -> dict:
    out = {k: o[k] for k in ("id", "product_id", "title", "paid_cents", "currency", "status", "created_at", "captured_at")}
    out["image_url"] = (conn.execute("SELECT image_url FROM products WHERE id=?", (o["product_id"],)).fetchone() or {"image_url": None})["image_url"]
    out["covered"] = bool(o["policy_json"])
    out["testing"] = bool(o["test_policy_id"])  # bought under a console guest's Testing promise
    out["window_days"] = out["cover_ends_at"] = out["max_refund_cents"] = None
    if o["policy_json"]:
        p = Policy.model_validate_json(o["policy_json"])
        out["window_days"], out["max_refund_cents"] = p.window_days, to_cents(p.max_refund_per_order)
        out["cover_ends_at"] = o["captured_at"] + p.window_days * 86400 if o["captured_at"] else None
    out["decisions"] = _rows(conn, "SELECT id, action, amount_cents, reason, market_cents, price_source, status,"
                                   " paypal_refund_id, explanation, created_at FROM settlements WHERE order_id=? ORDER BY id",
                             (o["id"],))
    out["refunded_cents"] = sum(d["amount_cents"] for d in out["decisions"] if d["action"] == "refund" and d["status"] != "FAILED")
    out["emails"] = _rows(conn, "SELECT kind, subject, status, channel, created_at, sent_at FROM notifications"
                                " WHERE order_id=? ORDER BY id", (o["id"],))
    return out


@app.get("/api/me/orders")
def my_orders(email: str = Depends(require_shopper), conn=Depends(get_conn)):
    rows = conn.execute("SELECT * FROM orders WHERE buyer_email=? AND status!='CREATED' ORDER BY id DESC LIMIT 50",
                        (email,)).fetchall()
    return [_my_order(conn, o) for o in rows]


class MyDropIn(BaseModel):
    drop_pct: float = Field(ge=1, le=80)


@app.post("/api/me/orders/{order_id}/simulate-drop")
def my_simulated_drop(order_id: int, body: MyDropIn, email: str = Depends(require_shopper),
                      conn=Depends(get_conn), pp: PayPalClient = Depends(get_paypal)):
    """Demo only: pretend the market price of THIS order's product fell by drop_pct, and settle just this order.
    The product's real price is untouched, so other shoppers' orders are not affected. Every guardrail
    (window, per-order cap, daily budget) still applies, and the refund is labelled 'simulated'."""
    if not get_settings().demo_self_serve:
        raise HTTPException(403, "Self-serve price drops are turned off on this server.")
    o = conn.execute("SELECT * FROM orders WHERE id=? AND buyer_email=?", (order_id, email)).fetchone()
    if not o:
        raise HTTPException(404, "Order not found.")
    if o["status"] != "CAPTURED" or not o["policy_json"]:
        raise HTTPException(409, "This order isn't covered by a price-drop guarantee.")
    market = max(1, round(o["paid_cents"] * (1 - body.drop_pct / 100)))
    result = settle_order(conn, pp, o, market, "simulated")
    notify.flush(conn)
    return {"market_cents": market, **result, "order": _my_order(conn, conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone())}


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
admin = [Depends(require_admin)]        # owner only
merchant = [Depends(merchant_role)]     # owner, or a guest in demo mode


class CompileIn(BaseModel):
    text: str


@app.post("/api/policy/compile")
def policy_compile(body: CompileIn, who: Merchant = Depends(merchant_role)):
    if who.role == "guest" and not _guest_compile_allowed(time.time()):
        raise HTTPException(429, "Guests have used this hour's AI promise previews. Try again later.")
    try:
        return policy_mod.compile_policy(body.text)
    except policy_mod.PolicyError as e:
        raise HTTPException(422, str(e)) from e


class ActivateIn(BaseModel):
    text: str
    policy: Policy
    source: str = "rules"


@app.post("/api/policy/activate")
def policy_activate(body: ActivateIn, conn=Depends(get_conn), who: Merchant = Depends(merchant_role)):
    source = body.source if body.source in ("llm", "rules") else "rules"
    if who.role == "guest":  # a Testing promise: only this guest's browser sees it, and it ends after 3 hours
        policy, lowered = policy_mod.clamp_test(body.policy)
        row = policy_mod.activate_test(conn, who.guest_id, body.text, policy, source)
        return {"id": row["id"], "active": True, "testing": True, "expires_at": row["expires_at"],
                "policy": policy.model_dump(), "lowered": lowered, "limits": policy_mod.TEST_LIMITS}
    pid = policy_mod.activate(conn, body.text, body.policy, source)
    return {"id": pid, "active": True, "testing": False}


@app.post("/api/policy/test/end")
def policy_test_end(conn=Depends(get_conn), who: Merchant = Depends(merchant_role)):
    if who.role == "guest":
        policy_mod.end_test(conn, who.guest_id)
    return {"ok": True}


@app.get("/api/policy")
def policy_get(conn=Depends(get_conn), who: Merchant = Depends(merchant_role)):
    """The owner sees the store's promise; a guest sees their own Testing promise while it lasts."""
    row = policy_mod.get_for(conn, who.guest_id) if who.role == "guest" else policy_mod.get_active(conn)
    out = policy_mod.policy_row_to_dict(row)
    if out is not None and who.role == "guest" and not out["testing"]:
        out["store_promise"] = True  # nothing of their own yet: they're looking at the real promise
    return out


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


@app.post("/api/simulate/drop", dependencies=merchant)
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


@app.post("/api/simulate/reset", dependencies=merchant)
def simulate_reset(body: ResetIn, conn=Depends(get_conn)):
    prices.reset_simulation(conn, body.product_id)
    return {"ok": True}


@app.post("/api/watch/run", dependencies=merchant)
def watch_run(conn=Depends(get_conn), pp: PayPalClient = Depends(get_paypal)):
    r = run_watch_once(conn, pp)
    notify.flush(conn)
    return r


@app.get("/api/notifications")
def notifications_all(conn=Depends(get_conn), who: Merchant = Depends(merchant_role)):
    rows = _rows(conn, "SELECT id, email, kind, order_id, subject, status, channel, error, created_at, sent_at"
                       " FROM notifications ORDER BY id DESC LIMIT 200")
    if who.role == "guest":  # guests never see other testers' email addresses
        for r in rows:
            r["email"] = _mask_email(r["email"])
    return rows


def _rows(conn, sql: str, args: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


@app.get("/api/orders", dependencies=merchant)
def orders(conn=Depends(get_conn)):
    out = []
    for r in conn.execute("SELECT id, paypal_order_id, capture_id, product_id, title, paid_cents, currency, status,"
                          " policy_json, test_policy_id, created_at, captured_at, capture_webhook_at"
                          " FROM orders ORDER BY id DESC LIMIT 200").fetchall():
        o = dict(r)
        pj = o.pop("policy_json")
        o["guaranteed"] = int(pj is not None)
        o["testing"] = bool(o.pop("test_policy_id"))
        o["window_days"] = o["cover_ends_at"] = o["max_refund_cents"] = None
        if pj:  # the promise this order bought under, so the console can draw its protection window
            p = Policy.model_validate_json(pj)
            o["window_days"] = p.window_days
            o["max_refund_cents"] = to_cents(p.max_refund_per_order)
            if o["captured_at"]:
                o["cover_ends_at"] = o["captured_at"] + p.window_days * 86400
        out.append(o)
    return out


@app.get("/api/settlements", dependencies=merchant)
def settlements(conn=Depends(get_conn)):
    return _rows(conn, "SELECT s.id, s.order_id, o.title, o.test_policy_id IS NOT NULL AS testing, s.action,"
                       " s.amount_cents, s.reason, s.market_cents,"
                       " s.price_source, s.status, s.paypal_refund_id, s.explanation, s.error,"
                       " s.webhook_confirmed_at, s.created_at FROM settlements s JOIN orders o ON o.id=s.order_id"
                       " ORDER BY s.id DESC LIMIT 200")


@app.get("/api/ledger", dependencies=merchant)
def ledger(conn=Depends(get_conn)):
    return _rows(conn, "SELECT event_id, event_type, resource_id, verified, received_at FROM webhook_events"
                       " ORDER BY received_at DESC LIMIT 200")


@app.get("/api/stats", dependencies=merchant)
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
    refunded_today = conn.execute(  # against the store's real budget: Testing promises have their own
        "SELECT COALESCE(SUM(s.amount_cents),0) FROM settlements s JOIN orders o ON o.id=s.order_id"
        " WHERE s.action='refund' AND s.status!='FAILED' AND s.created_at>=? AND o.test_policy_id IS NULL",
        (day_start,)).fetchone()[0]
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
