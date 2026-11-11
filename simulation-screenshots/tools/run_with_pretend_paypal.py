"""Run the REAL PriceBack app with a PRETEND PayPal, for a screenshot walkthrough.

Nothing in app/ is changed. This script:
  * points the app at a throwaway database (so your priceback.db is untouched),
  * blanks the Channel3 key so the 6 demo products load (numbers match SIMULATION-GUIDE.md),
  * swaps ONLY the PayPal client for an in-memory pretend one (same idea as tests/conftest.py FakePayPal),
  * adds two clearly-labelled local pages: /pretend-paypal/checkout/<id> and /pretend-paypal/activity.

Usage (from the project folder):
    .venv\\Scripts\\python simulation-screenshots\\tools\\run_with_pretend_paypal.py <db_path> [port]
"""
from __future__ import annotations

import html
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "simulation-run.db")
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8010
BASE = f"http://127.0.0.1:{PORT}"

# Set before the app loads .env (python-dotenv never overrides variables that already exist).
os.environ.update({
    "PRICEBACK_DB": DB,
    "CHANNEL3_API_KEY": "",          # demo catalog
    "PUBLIC_BASE_URL": BASE,
    "WATCH_INTERVAL_SECONDS": "0",   # watcher runs only when "Run watcher now" is clicked
    "PAYPAL_WEBHOOK_ID": "",
})
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402

from app.main import app, get_paypal  # noqa: E402


class PretendPayPal:
    """Same contract as app.paypal.PayPalClient. Same PayPal-Request-Id => same refund (no double refunds)."""

    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.captures: list[dict] = []
        self.refund_calls: list[str] = []
        self.refunds: dict[str, dict] = {}
        self._n = 0

    def create_order(self, *, amount_cents, currency, description, reference_id, return_url, cancel_url):
        self._n += 1
        oid = f"PRETEND-ORDER-{self._n}"
        self.orders[oid] = {"amount_cents": amount_cents, "currency": currency, "description": description,
                            "return_url": return_url, "cancel_url": cancel_url, "status": "CREATED"}
        return {"id": oid, "approve_url": f"{BASE}/pretend-paypal/checkout/{oid}"}

    def capture_order(self, order_id):
        o = self.orders[order_id]
        o["status"] = "COMPLETED"
        cap = f"PRETEND-CAPTURE-{order_id.rsplit('-', 1)[-1]}"
        self.captures.append({"capture_id": cap, "order_id": order_id, "amount_cents": o["amount_cents"],
                              "description": o["description"], "at": time.time()})
        return {"order_status": "COMPLETED", "capture_id": cap, "capture_status": "COMPLETED",
                "amount_cents": o["amount_cents"], "currency": o["currency"]}

    def refund_capture(self, *, capture_id, amount_cents, currency, request_id, note=""):
        self.refund_calls.append(request_id)
        if request_id not in self.refunds:
            self.refunds[request_id] = {"id": f"PRETEND-REFUND-{len(self.refunds) + 1}", "status": "COMPLETED",
                                        "amount_cents": amount_cents, "capture_id": capture_id,
                                        "note": note, "at": time.time()}
        r = self.refunds[request_id]
        return {"id": r["id"], "status": r["status"]}

    def verify_webhook(self, headers, raw_body):
        return False


PP = PretendPayPal()
app.dependency_overrides[get_paypal] = lambda: PP

STYLE = """<style>
body{margin:0;font:15px/1.5 system-ui,Segoe UI,sans-serif;background:#f5f7fa;color:#1c1e21}
.warn{background:#ffd43b;color:#3d2f00;text-align:center;padding:10px;font-weight:700}
.box{max-width:520px;margin:40px auto;background:#fff;border:1px solid #dde1e7;border-radius:14px;padding:28px}
.logo{font-size:26px;font-weight:800;color:#003087}.logo span{color:#009cde}
.row{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #eef0f3}
.btn{display:block;text-align:center;background:#0070ba;color:#fff;border-radius:999px;padding:12px;
font-weight:700;text-decoration:none;margin-top:18px}.link{display:block;text-align:center;margin-top:12px;color:#0070ba}
table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:6px 8px;border-bottom:1px solid #eef0f3;text-align:left}
.wide{max-width:900px}.mut{color:#6b7280}
</style>"""
BANNER = ('<div class="warn">PRETEND PAYPAL: a local simulation running on this computer. '
          'Not paypal.com, no real or sandbox money.</div>')


def money(c: int) -> str:
    return f"${c / 100:,.2f}"


@app.get("/pretend-paypal/checkout/{oid}", include_in_schema=False)
def pretend_checkout(oid: str):
    o = PP.orders.get(oid)
    if not o:
        return HTMLResponse("Unknown order", 404)
    e = html.escape
    return HTMLResponse(f"""<!doctype html><title>Pretend PayPal Checkout</title>{STYLE}{BANNER}
<div class="box"><div class="logo">Pay<span>Pal</span> <small class="mut">(pretend)</small></div>
<p class="mut">Paying <b>PriceBack Demo Store</b> as <b>demo buyer</b> (pretend account, already logged in)</p>
<div class="row"><span>{e(o['description'])}</span><b>{money(o['amount_cents'])} {e(o['currency'])}</b></div>
<div class="row"><span>Pay with</span><span>PayPal balance (pretend)</span></div>
<div class="row"><span>PayPal order id</span><code>{e(oid)}</code></div>
<a id="paynow" class="btn" href="{e(o['return_url'])}?token={e(oid)}&PayerID=PRETENDBUYER">Pay Now</a>
<a class="link" href="{e(o['cancel_url'])}">Cancel and return to PriceBack Demo Store</a></div>""")


@app.get("/pretend-paypal/activity", include_in_schema=False)
def pretend_activity():
    caps = "".join(f"<tr><td>{c['capture_id']}</td><td>{html.escape(c['description'])}</td>"
                   f"<td>+{money(c['amount_cents'])}</td><td>{time.strftime('%H:%M:%S', time.localtime(c['at']))}</td></tr>"
                   for c in PP.captures) or '<tr><td colspan="4" class="mut">none</td></tr>'
    refs = "".join(f"<tr><td>{r['id']}</td><td>{r['capture_id']}</td><td>-{money(r['amount_cents'])}</td>"
                   f"<td><code>{html.escape(k)}</code></td><td>{time.strftime('%H:%M:%S', time.localtime(r['at']))}</td></tr>"
                   for k, r in PP.refunds.items()) or '<tr><td colspan="5" class="mut">none</td></tr>'
    return HTMLResponse(f"""<!doctype html><title>Pretend PayPal Activity</title>{STYLE}{BANNER}
<div class="box wide"><div class="logo">Pay<span>Pal</span> <small class="mut">(pretend) · merchant activity</small></div>
<h3>Payments received</h3><table><tr><th>Capture id</th><th>Item</th><th>Amount</th><th>Time</th></tr>{caps}</table>
<h3>Refunds sent</h3><table><tr><th>Refund id</th><th>Of capture</th><th>Amount</th><th>PayPal-Request-Id</th><th>Time</th></tr>{refs}</table>
<p class="mut">Refund API calls received: <b>{len(PP.refund_calls)}</b> · unique refunds created: <b>{len(PP.refunds)}</b></p></div>""")


if __name__ == "__main__":
    print(f"PriceBack (real app) + PRETEND PayPal on {BASE}  -  database: {DB}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=PORT)
