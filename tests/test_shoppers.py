"""Shopper sign-in (emailed one-time code), my-orders, self-serve demo drops and notifications."""
import pytest
from fastapi.testclient import TestClient

from app import notify, shoppers
from app.main import app, get_paypal
from tests.test_api import activate, buy, sign_in


@pytest.fixture
def client(paypal):
    app.dependency_overrides[get_paypal] = lambda: paypal
    with TestClient(app) as c:
        c.pid = c.post("/api/products", json={"title": "Over-Ear Headphones", "price": 129}).json()["id"]
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def outbox(monkeypatch):
    """Turn on both channels and capture what would be sent."""
    sent = {"email": [], "zapier": []}
    monkeypatch.setenv("BREVO_API_KEY", "test-key")
    monkeypatch.setenv("MAIL_FROM", "demo@priceback.test")
    monkeypatch.setenv("ZAPIER_HOOK_URL", "https://hooks.zapier.test/catch/1/abc")
    monkeypatch.setattr(notify, "send_email", lambda to, subject, text, html_body: sent["email"].append((to, subject, text, html_body)))
    monkeypatch.setattr(notify, "post_zapier", lambda event: sent["zapier"].append(event))
    return sent


def sign_in_by_email(client, outbox, email):
    assert client.post("/api/auth/start", json={"email": email}).status_code == 200
    code = outbox["email"].pop()[1][:6]  # "123456 is your PriceBack sign-in code"
    assert client.post("/api/auth/verify", json={"email": email, "code": code}).status_code == 200


def test_sign_in_with_code(client):
    assert client.get("/api/me").json() == {"email": None}
    sign_in(client, "Ada@Example.com ")
    assert client.get("/api/me").json() == {"email": "ada@example.com"}
    client.post("/api/auth/logout")
    assert client.get("/api/me").json() == {"email": None}


def test_wrong_code_is_limited(client):
    r = client.post("/api/auth/start", json={"email": "a@example.com"}).json()
    wrong = "000000" if r["dev_code"] != "000000" else "111111"
    for _ in range(shoppers.MAX_ATTEMPTS):
        assert client.post("/api/auth/verify", json={"email": "a@example.com", "code": wrong}).status_code == 400
    # even the right code no longer works once the attempts are used up
    assert client.post("/api/auth/verify", json={"email": "a@example.com", "code": r["dev_code"]}).status_code == 400


def test_resend_gap_and_bad_email(client):
    assert client.post("/api/auth/start", json={"email": "b@example.com"}).status_code == 200
    assert client.post("/api/auth/start", json={"email": "b@example.com"}).status_code == 429
    assert client.post("/api/auth/start", json={"email": "not-an-email"}).status_code == 422


def test_public_server_without_email_refuses_sign_in(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://priceback.example.com")
    assert client.post("/api/auth/start", json={"email": "c@example.com"}).status_code == 503


def test_sign_in_code_is_emailed_not_returned(client, outbox):
    r = client.post("/api/auth/start", json={"email": "d@example.com"}).json()
    assert r == {"sent": True, "email": "d@example.com"}
    (to, subject, text, _), = outbox["email"]
    assert to == "d@example.com" and subject[:6].isdigit()
    assert outbox["zapier"] == []  # codes never leave through Zapier


def test_checkout_needs_sign_in(client):
    r = client.post("/api/checkout", json={"product_id": client.pid})
    assert r.status_code == 401


def test_order_refund_and_block_notify_the_buyer(client, paypal, outbox):
    activate(client)
    sign_in_by_email(client, outbox, "eve@example.com")
    buy(client, client.pid)
    order = client.get("/api/me/orders").json()[0]
    assert order["covered"] and order["max_refund_cents"] == 2000

    r = client.post(f"/api/me/orders/{order['id']}/simulate-drop", json={"drop_pct": 10}).json()
    assert r["action"] == "refund" and r["amount_cents"] == 1290 and r["market_cents"] == 11610
    r = client.post(f"/api/me/orders/{order['id']}/simulate-drop", json={"drop_pct": 30}).json()
    assert r["action"] == "refund" and r["amount_cents"] == 710  # only up to the $20 cap
    r = client.post(f"/api/me/orders/{order['id']}/simulate-drop", json={"drop_pct": 40}).json()
    assert r["action"] == "blocked"

    subjects = [s for _, s, _, _ in outbox["email"]]
    assert subjects[0].startswith(f"Order #{order['id']} confirmed: price protected for 14 days")
    assert subjects[1] == "The price dropped. We refunded you $12.90"
    assert subjects[2] == "The price dropped. We refunded you $7.10"
    assert subjects[3].startswith("The price of Over-Ear Headphones dropped again")
    assert all(to == "eve@example.com" for to, *_ in outbox["email"])
    assert [e["event"] for e in outbox["zapier"]] == ["order_confirmed", "refund_sent", "refund_sent", "refund_blocked"]
    mine = client.get("/api/me/orders").json()[0]
    assert mine["refunded_cents"] == 2000 and [e["status"] for e in mine["emails"]] == ["sent"] * 4

    # re-running the watcher never sends the same email twice
    client.post("/api/watch/run")
    assert len(outbox["email"]) == 4


def test_self_serve_drop_is_scoped_to_own_order(client, paypal):
    activate(client)
    sign_in(client, "first@example.com")
    buy(client, client.pid)
    first = client.get("/api/me/orders").json()[0]["id"]
    sign_in(client, "second@example.com")
    buy(client, client.pid)
    # someone else's order is invisible
    assert client.post(f"/api/me/orders/{first}/simulate-drop", json={"drop_pct": 20}).status_code == 404
    second = client.get("/api/me/orders").json()[0]["id"]
    assert client.post(f"/api/me/orders/{second}/simulate-drop", json={"drop_pct": 20}).json()["action"] == "refund"
    # the product's real price never moved, so the watcher refunds nobody else
    assert client.post("/api/watch/run").json()["refunds"] == 0
    assert len(set(paypal.refund_calls)) == 1


def test_without_channels_messages_are_still_recorded(client, paypal):
    activate(client)
    sign_in(client)
    buy(client, client.pid)
    (o,) = client.get("/api/me/orders").json()
    assert [(e["kind"], e["status"]) for e in o["emails"]] == [("order_confirmed", "not_configured")]


def test_demo_info_is_public(client, monkeypatch):
    monkeypatch.setenv("DEMO_BUYER_EMAIL", "sb-buyer@personal.example.com")
    monkeypatch.setenv("DEMO_BUYER_PASSWORD", "sandbox-pass")
    d = TestClient(app).get("/api/demo").json()
    assert d["buyer_email"] == "sb-buyer@personal.example.com" and d["self_serve_drops"] is True


def test_smtp_sender_with_smtplib(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["server"] = (host, port)
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, context): sent["tls"] = True
        def login(self, user, pw): sent["login"] = (user, pw)
        def send_message(self, msg): sent["msg"] = msg

    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_USER", "shop@gmail.example")
    monkeypatch.setenv("SMTP_PASSWORD", "abcd efgh ijkl mnop")  # Google shows app passwords with spaces
    assert notify.email_provider() == "smtp"
    notify.send_login_code("f@example.com", "123456")
    assert sent["server"] == ("smtp.gmail.com", 587) and sent["tls"]
    assert sent["login"] == ("shop@gmail.example", "abcdefghijklmnop")
    msg = sent["msg"]
    assert msg["To"] == "f@example.com" and msg["Subject"] == "123456 is your PriceBack sign-in code"
    assert "shop@gmail.example" in msg["From"]  # MAIL_FROM defaults to the SMTP login
    assert [p.get_content_type() for p in msg.iter_parts()] == ["text/plain", "text/html"]


def test_email_provider_choice(monkeypatch):
    assert notify.email_provider() is None
    monkeypatch.setenv("BREVO_API_KEY", "k")
    monkeypatch.setenv("MAIL_FROM", "demo@priceback.test")
    assert notify.email_provider() == "brevo"
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_USER", "u@gmail.example")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    assert notify.email_provider() == "smtp"


def test_refund_keys_survive_a_database_reset(client, paypal):
    """Order ids restart at 1 when the DB resets (Render free plan); PayPal must never see a reused request id."""
    activate(client)
    sign_in(client)
    buy(client, client.pid)
    (o,) = client.get("/api/me/orders").json()
    client.post(f"/api/me/orders/{o['id']}/simulate-drop", json={"drop_pct": 10})
    (key,) = paypal.refund_calls
    from app.db import connect
    from contextlib import closing
    with closing(connect()) as conn:
        capture = conn.execute("SELECT capture_id FROM orders WHERE id=?", (o["id"],)).fetchone()[0]
    assert capture and capture in key and not key.startswith(f"pb-{o['id']}-")
