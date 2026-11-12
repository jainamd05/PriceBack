"""Guest merchant mode: anyone can explore the console and the price lab, but only the owner can touch the catalog."""
import pytest
from fastapi.testclient import TestClient

from app import main
from app.main import app, get_paypal
from tests.test_api import PROMISE, buy, sign_in

GUEST = {"x-demo-guest": "1"}
OWNER = {"x-admin-token": "s3cret"}


@pytest.fixture
def client(paypal, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    monkeypatch.setenv("DEMO_MERCHANT_GUEST", "true")
    main._guest_compiles.clear()
    app.dependency_overrides[get_paypal] = lambda: paypal
    with TestClient(app) as c:
        c.pid = c.post("/api/products", json={"title": "Over-Ear Headphones", "price": 129}, headers=OWNER).json()["id"]
        yield c
    app.dependency_overrides.clear()


def test_guest_can_run_the_whole_demo(client, paypal):
    assert client.get("/api/demo").json()["merchant_guest"] is True
    compiled = client.post("/api/policy/compile", json={"text": PROMISE}, headers=GUEST).json()
    assert client.post("/api/policy/activate", json={"text": PROMISE, **compiled}, headers=GUEST).status_code == 200
    sign_in(client)
    buy(client, client.pid)
    for path in ("/api/stats", "/api/orders", "/api/settlements", "/api/ledger", "/api/policy"):
        assert client.get(path, headers=GUEST).status_code == 200, path
    assert client.post("/api/simulate/drop", json={"product_id": client.pid, "drop_pct": 10}, headers=GUEST).status_code == 200
    assert client.post("/api/watch/run", headers=GUEST).json()["refunds"] == 1
    assert client.post("/api/simulate/reset", json={}, headers=GUEST).status_code == 200


def test_guest_cannot_touch_the_catalog(client):
    assert client.post("/api/products", json={"title": "X", "price": 5}, headers=GUEST).status_code == 403
    assert client.patch(f"/api/products/{client.pid}", json={"title": "Y"}, headers=GUEST).status_code == 403
    assert client.delete(f"/api/products/{client.pid}", headers=GUEST).status_code == 403
    assert client.post("/api/catalog/refresh", headers=GUEST).status_code == 403
    assert client.patch(f"/api/products/{client.pid}", json={"title": "Y"}, headers=OWNER).status_code == 200


def test_guest_sees_masked_emails(client):
    sign_in(client, "judge.person@example.com")
    buy(client, client.pid)
    emails = {n["email"] for n in client.get("/api/notifications", headers=GUEST).json()}
    assert emails == {"ju•••@example.com"}
    assert {n["email"] for n in client.get("/api/notifications", headers=OWNER).json()} == {"judge.person@example.com"}


def test_guest_ai_previews_are_rate_limited(client, monkeypatch):
    monkeypatch.setattr(main, "GUEST_COMPILES_PER_HOUR", 2)
    codes = [client.post("/api/policy/compile", json={"text": PROMISE}, headers=GUEST).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    assert client.post("/api/policy/compile", json={"text": PROMISE}, headers=OWNER).status_code == 200


def test_guest_mode_off_means_token_only(client, monkeypatch):
    monkeypatch.setenv("DEMO_MERCHANT_GUEST", "false")
    assert client.get("/api/stats", headers=GUEST).status_code == 401
    assert client.post("/api/products", json={"title": "X", "price": 5}, headers=GUEST).status_code == 401


# ---- Testing promises -------------------------------------------------------------------------------------
TEST_TEXT = "Within 3 days, refund the difference up to $5 per order, with a $15 daily limit."


def _guest_promise(client, text=TEST_TEXT):
    compiled = client.post("/api/policy/compile", json={"text": text}, headers=GUEST).json()
    r = client.post("/api/policy/activate", json={"text": text, **compiled}, headers=GUEST)
    assert r.status_code == 200, r.text
    return r.json()


def _owner_promise(client):
    compiled = client.post("/api/policy/compile", json={"text": PROMISE}, headers=OWNER).json()
    assert client.post("/api/policy/activate", json={"text": PROMISE, **compiled}, headers=OWNER).status_code == 200


def test_guest_promise_is_testing_and_private(client):
    _owner_promise(client)
    r = _guest_promise(client)
    assert r["testing"] and r["expires_at"] - int(__import__("time").time()) in range(3 * 3600 - 60, 3 * 3600 + 1)
    # this browser (it has the guest cookie) sees its Testing promise; the store's real promise is untouched
    mine = client.get("/api/guarantee").json()
    assert mine["testing"] and mine["window_days"] == 3
    other = TestClient(app).get("/api/guarantee").json()  # a different browser
    assert not other["testing"] and other["window_days"] == 14
    assert client.get("/api/policy", headers=OWNER).json()["policy"]["window_days"] == 14
    assert client.get("/api/policy", headers=GUEST).json()["testing"] is True


def test_order_under_testing_promise_uses_its_own_budget(client, paypal):
    _owner_promise(client)
    _guest_promise(client)
    sign_in(client, "judge@example.com")
    buy(client, client.pid)
    (o,) = client.get("/api/me/orders").json()
    assert o["testing"] and o["window_days"] == 3 and o["max_refund_cents"] == 500
    assert client.get("/api/orders", headers=OWNER).json()[0]["testing"] is True
    r = client.post(f"/api/me/orders/{o['id']}/simulate-drop", json={"drop_pct": 20}).json()
    assert r["action"] == "refund" and r["amount_cents"] == 500  # capped by the Testing promise's $5
    assert client.get("/api/settlements", headers=OWNER).json()[0]["testing"] == 1
    assert client.get("/api/stats", headers=OWNER).json()["refunded_today_cents"] == 0  # real budget untouched


def test_testing_promise_limits_and_expiry(client, monkeypatch):
    from app import policy as policy_mod
    big = "Refund the difference if the price drops within 60 days, up to $500 per order and $5000 per day."
    r = _guest_promise(client, big)
    assert set(r["lowered"]) == {"window_days", "max_refund_per_order", "daily_budget"}
    assert r["policy"]["window_days"] == 30 and r["policy"]["max_refund_per_order"] == 100
    # three hours later it is gone: the browser sees the store's promise again (none here)
    later = r["expires_at"] + 1
    monkeypatch.setattr(policy_mod.time, "time", lambda: later)
    assert client.get("/api/guarantee").json()["testing"] is False


def test_guest_can_end_their_testing_promise(client):
    _owner_promise(client)
    _guest_promise(client)
    assert client.post("/api/policy/test/end", headers=GUEST).json() == {"ok": True}
    assert client.get("/api/guarantee").json()["window_days"] == 14
