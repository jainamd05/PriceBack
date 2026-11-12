import json

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_paypal

PROMISE = "Refund the difference if the price drops within 14 days, up to $20 per order and $100 per day."


def test_health_endpoint():
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.fixture
def client(paypal):
    app.dependency_overrides[get_paypal] = lambda: paypal
    with TestClient(app) as c:
        c.pid = {name: c.post("/api/products", json={"title": title, "price": price}).json()["id"]
                 for name, title, price in (("headphones", "Over-Ear Headphones", 129), ("mouse", "Wireless Mouse", 45))}
        sign_in(c)
        yield c
    app.dependency_overrides.clear()


def sign_in(client, email="shopper@example.com"):
    r = client.post("/api/auth/start", json={"email": email})
    assert r.status_code == 200, r.text
    r = client.post("/api/auth/verify", json={"email": email, "code": r.json()["dev_code"]})
    assert r.status_code == 200, r.text


def activate(client, text=PROMISE):
    compiled = client.post("/api/policy/compile", json={"text": text}).json()
    r = client.post("/api/policy/activate", json={"text": text, **compiled})
    assert r.status_code == 200, r.text


def buy(client, product_id=None):
    r = client.post("/api/checkout", json={"product_id": product_id or client.pid["headphones"]})
    assert r.status_code == 200, r.text
    oid = r.json()["paypal_order_id"]
    back = client.get(f"/checkout/return?token={oid}", follow_redirects=False)
    assert back.status_code in (302, 307)
    return oid, back.headers["location"]


def test_full_flow_checkout_drop_refund(client, paypal):
    activate(client)
    assert len(client.get("/api/products").json()) == 2
    assert client.get("/api/guarantee").json()["max_refund_per_order"] == 20

    oid, loc = buy(client)
    assert loc.startswith("/?paid=")

    # 20% simulated drop on $129.00 -> drop $25.80, capped at $20 by the promise
    d = client.post("/api/simulate/drop", json={"product_id": client.pid["headphones"], "drop_pct": 20}).json()
    assert d["price_source"] == "simulated" and d["simulated_price_cents"] == 10320

    run = client.post("/api/watch/run").json()
    assert run["refunds"] == 1
    s = client.get("/api/settlements").json()[0]
    assert (s["amount_cents"], s["reason"], s["status"], s["price_source"]) == (2000, "capped_by_order_cap", "COMPLETED", "simulated")

    # re-running never double-refunds
    assert client.post("/api/watch/run").json()["refunds"] == 0
    assert len(set(paypal.refund_calls)) == 1

    st = client.get("/api/stats").json()
    assert st["refunded_cents"] == 2000 and st["guaranteed_orders"] == 1
    assert st["open_exposure_cents"] == 0  # cap fully used


def test_checkout_price_comes_from_server_not_client(client, paypal):
    r = client.post("/api/checkout", json={"product_id": client.pid["mouse"], "price": 1})
    assert r.status_code == 200
    assert paypal.orders[r.json()["paypal_order_id"]] == (4500, "USD")


def test_double_return_captures_once(client, paypal):
    activate(client)
    oid, _ = buy(client)
    client.get(f"/checkout/return?token={oid}", follow_redirects=False)
    assert paypal.capture_calls == [oid]


def test_amount_mismatch_is_not_guaranteed(client, paypal):
    activate(client)
    paypal.capture_amount_override = 1
    buy(client)
    o = client.get("/api/orders").json()[0]
    assert o["status"] == "AMOUNT_MISMATCH" and o["guaranteed"] == 0


def test_orders_expose_protection_window(client, paypal):
    buy(client)  # before any promise: no window
    activate(client)
    buy(client)
    covered, uncovered = client.get("/api/orders").json()
    assert (covered["window_days"], covered["max_refund_cents"]) == (14, 2000)
    assert covered["cover_ends_at"] == covered["captured_at"] + 14 * 86400
    assert "policy_json" not in covered
    assert uncovered["guaranteed"] == 0 and uncovered["cover_ends_at"] is None


def test_order_before_promise_is_not_covered(client, paypal):
    buy(client)  # no active policy
    activate(client)
    client.post("/api/simulate/drop", json={"product_id": client.pid["headphones"], "drop_pct": 30})
    assert client.post("/api/watch/run").json()["refunds"] == 0
    assert paypal.refund_calls == []


def test_merchant_products_add_validate_delete(client):
    r = client.post("/api/products", json={"title": "Desk Lamp", "price": 39.5, "image_url": "https://img.test/l.jpg"})
    assert r.status_code == 200 and r.json()["price_cents"] == 3950
    assert client.post("/api/products", json={"title": "Bad", "price": 5, "image_url": "http://x"}).status_code == 422
    assert client.post("/api/products", json={"title": "Free", "price": 0}).status_code == 422
    assert client.delete(f"/api/products/{r.json()['id']}").status_code == 200
    assert client.delete(f"/api/products/{r.json()['id']}").status_code == 404
    assert len(client.get("/api/products").json()) == 2


def test_product_category_and_edit(client):
    r = client.post("/api/products", json={"title": "Arc 900", "price": 199, "category": "  Headphones "}).json()
    assert r["category"] == "Headphones"
    pid = r["id"]
    e = client.patch(f"/api/products/{pid}", json={"title": "Aurelo Arc 900", "category": "Audio",
                                                   "image_url": "https://img.test/a.jpg"})
    assert e.status_code == 200
    assert (e.json()["title"], e.json()["category"], e.json()["price_cents"]) == ("Aurelo Arc 900", "Audio", 19900)
    assert client.patch(f"/api/products/{pid}", json={"image_url": "http://x"}).status_code == 422
    assert client.patch("/api/products/nope", json={"title": "x"}).status_code == 404
    # the price is not editable: it is the market price for hand-added products
    assert client.patch(f"/api/products/{pid}", json={"price": 1}).json()["price_cents"] == 19900


def test_catalog_refresh_without_channel3_key_keeps_products(client):
    r = client.post("/api/catalog/refresh").json()
    assert r["count"] == 0 and "CHANNEL3_API_KEY" in r["error"]
    assert len(client.get("/api/products").json()) == 2


def test_unknown_product_404(client):
    assert client.post("/api/checkout", json={"product_id": "nope"}).status_code == 404


def test_compile_error_is_422(client):
    assert client.post("/api/policy/compile", json={"text": "refund stuff"}).status_code == 422


def test_admin_token_protects_merchant_routes_only(client, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    assert client.get("/api/products").status_code == 200
    assert client.get("/api/stats").status_code == 401
    assert client.get("/api/stats", headers={"x-admin-token": "wrong"}).status_code == 401
    assert client.get("/api/stats", headers={"x-admin-token": "s3cret"}).status_code == 200
    assert client.post("/api/watch/run").status_code == 401


# ---- webhooks -------------------------------------------------------------------------------
def refund_event(event_id, refund_id):
    return {"id": event_id, "event_type": "PAYMENT.CAPTURE.REFUNDED",
            "resource": {"id": refund_id, "status": "COMPLETED"}}


def test_webhook_requires_configuration(client):
    assert client.post("/api/webhooks/paypal", content=json.dumps(refund_event("E1", "REF-1"))).status_code == 503


def test_webhook_rejects_bad_signature(client, paypal, monkeypatch):
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", "WH-1")
    paypal.verify_result = False
    r = client.post("/api/webhooks/paypal", content=json.dumps(refund_event("E1", "REF-1")))
    assert r.status_code == 400
    assert client.get("/api/ledger").json() == []


def test_verified_webhook_confirms_refund_and_dedupes(client, paypal, monkeypatch):
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", "WH-1")
    activate(client)
    buy(client)
    client.post("/api/simulate/drop", json={"product_id": client.pid["headphones"], "drop_pct": 10})
    client.post("/api/watch/run")
    assert client.get("/api/settlements").json()[0]["webhook_confirmed_at"] is None

    body = json.dumps(refund_event("EVT-1", "REF-1"))
    assert client.post("/api/webhooks/paypal", content=body).json()["result"] == "applied"
    assert client.get("/api/settlements").json()[0]["webhook_confirmed_at"] is not None
    assert client.post("/api/webhooks/paypal", content=body).json()["result"] == "duplicate"
    assert len(client.get("/api/ledger").json()) == 1


def test_unverified_mode_records_but_never_changes_state(client, paypal, monkeypatch):
    monkeypatch.setenv("ALLOW_UNVERIFIED_WEBHOOKS", "true")
    activate(client)
    buy(client)
    client.post("/api/simulate/drop", json={"product_id": client.pid["headphones"], "drop_pct": 10})
    client.post("/api/watch/run")
    r = client.post("/api/webhooks/paypal", content=json.dumps(refund_event("EVT-9", "REF-1"))).json()
    assert r["result"] == "recorded" and r["verified"] is False
    assert client.get("/api/settlements").json()[0]["webhook_confirmed_at"] is None


def test_seed_catalog_fills_an_empty_store(monkeypatch, paypal):
    monkeypatch.setenv("SEED_CATALOG", "true")
    app.dependency_overrides[get_paypal] = lambda: paypal
    with TestClient(app) as c:
        products = c.get("/api/products").json()
        g = c.get("/api/guarantee").json()
    app.dependency_overrides.clear()
    assert len(products) == 100 and all(p["category"] for p in products)
    assert g["active"] and g["window_days"] == 14 and g["max_refund_per_order"] == 20
    from app.db import connect, seed_if_empty
    from contextlib import closing
    with closing(connect()) as conn:
        assert seed_if_empty(conn) == 0  # never seeds twice or over a merchant's own catalog
