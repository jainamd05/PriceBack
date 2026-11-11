import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import connect, init_db  # noqa: E402
from app.models import Policy  # noqa: E402


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PRICEBACK_DB", str(tmp_path / "test.db"))
    for k in ("GEMINI_API_KEY", "CHANNEL3_API_KEY", "ADMIN_TOKEN", "PAYPAL_WEBHOOK_ID",
              "ALLOW_UNVERIFIED_WEBHOOKS", "WATCH_INTERVAL_SECONDS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "test-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://testserver")


class FakePayPal:
    """In-memory stand-in with PayPal's idempotency semantics: same PayPal-Request-Id => same refund."""

    def __init__(self):
        self.orders: dict[str, tuple[int, str]] = {}
        self.capture_calls: list[str] = []
        self.refund_calls: list[str] = []      # request ids, one entry per API call
        self.refunds: dict[str, dict] = {}     # request id -> refund (unique refunds actually created)
        self.fail_with: Exception | None = None
        self.verify_result = True
        self.capture_amount_override: int | None = None
        self._n = 0

    def create_order(self, *, amount_cents, currency, description, reference_id, return_url, cancel_url):
        self._n += 1
        oid = f"ORDER{self._n}"
        self.orders[oid] = (amount_cents, currency)
        return {"id": oid, "approve_url": f"https://paypal.test/approve/{oid}"}

    def capture_order(self, order_id):
        self.capture_calls.append(order_id)
        amount, cur = self.orders[order_id]
        return {"order_status": "COMPLETED", "capture_id": f"CAP-{order_id}", "capture_status": "COMPLETED",
                "amount_cents": self.capture_amount_override or amount, "currency": cur}

    def refund_capture(self, *, capture_id, amount_cents, currency, request_id, note=""):
        self.refund_calls.append(request_id)
        if self.fail_with:
            raise self.fail_with
        if request_id not in self.refunds:
            self.refunds[request_id] = {"id": f"REF-{len(self.refunds) + 1}", "status": "COMPLETED",
                                        "amount_cents": amount_cents}
        r = self.refunds[request_id]
        return {"id": r["id"], "status": r["status"]}

    def verify_webhook(self, headers, raw_body):
        return self.verify_result


@pytest.fixture
def paypal():
    return FakePayPal()


@pytest.fixture
def conn():
    init_db()
    c = connect()
    yield c
    c.close()


def make_order(conn, *, paid_cents=10000, policy: Policy | None = None, captured_at: int | None = None,
               title="Test Headphones", product_id="m-test", n: int = 1):
    """Insert an already-captured, guaranteed order."""
    policy = policy or Policy(window_days=14, max_refund_per_order=20, daily_budget=100)
    now = captured_at if captured_at is not None else int(time.time())
    conn.execute(
        "INSERT INTO orders (paypal_order_id, capture_id, product_id, title, paid_cents, currency, status,"
        " policy_json, created_at, captured_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"PO{n}-{now}", f"CAP{n}", product_id, title, paid_cents, "USD", "CAPTURED",
         policy.model_dump_json(), now, now))
    return conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 1").fetchone()
