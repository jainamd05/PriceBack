"""Shopper notifications.

An outbox table records one message per event (order confirmed, refund sent, refund blocked), so every event
notifies exactly once even if the watcher runs again or the process restarts. Delivery channels:

* Email, by whichever sender is configured:
  - SMTP with Python's smtplib (SMTP_HOST/SMTP_USER/SMTP_PASSWORD, e.g. Gmail with an app password). Free, but
    Render's free plan blocks outbound SMTP ports, so use it locally or on a host that allows SMTP.
  - Brevo's HTTPS API (BREVO_API_KEY + MAIL_FROM). Free tier, and works on Render's free plan.
* Zapier: if ZAPIER_HOOK_URL is set, every event is also POSTed as JSON to that Catch Hook, so a Zap can
  forward it to Slack, Google Sheets, SMS, or its own email step.

Without either channel, messages are still recorded and shown on the shopper's account page.
Sign-in codes are emailed directly and are never sent to Zapier.
"""
from __future__ import annotations

import html
import logging
import smtplib
import sqlite3
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr

import httpx

from .config import get_settings
from .db import connect
from .models import Policy
from .money import fmt_amount, to_cents

log = logging.getLogger("priceback.notify")
BREVO_URL = "https://api.brevo.com/v3/smtp/email"
SYNC = False   # tests deliver inline
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="notify")


def email_provider() -> str | None:
    """'smtp' or 'brevo' (SMTP wins when both are set), or None."""
    s = get_settings()
    if s.smtp_host and s.smtp_user and s.smtp_password:
        return "smtp"
    if s.brevo_api_key and s.mail_from:
        return "brevo"
    return None


def email_configured() -> bool:
    return email_provider() is not None


# ---- channels ---------------------------------------------------------------------------------
def send_email(to: str, subject: str, text: str, html_body: str) -> None:
    if email_provider() == "smtp":
        _send_smtp(to, subject, text, html_body)
    else:
        _send_brevo(to, subject, text, html_body)


def _send_smtp(to: str, subject: str, text: str, html_body: str) -> None:
    s = get_settings()
    msg = EmailMessage()
    msg["From"] = formataddr((s.mail_from_name, s.mail_from))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    ctx = ssl.create_default_context()
    if s.smtp_port == 465:
        with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=15, context=ctx) as smtp:
            smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as smtp:
            smtp.starttls(context=ctx)
            smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(msg)


def _send_brevo(to: str, subject: str, text: str, html_body: str) -> None:
    s = get_settings()
    r = httpx.post(BREVO_URL, timeout=15,
                   headers={"api-key": s.brevo_api_key, "accept": "application/json"},
                   json={"sender": {"name": s.mail_from_name, "email": s.mail_from}, "to": [{"email": to}],
                         "subject": subject, "textContent": text, "htmlContent": html_body})
    if r.status_code >= 300:
        raise RuntimeError(f"Brevo {r.status_code}: {r.text[:200]}")


def post_zapier(event: dict) -> None:
    r = httpx.post(get_settings().zapier_hook_url, json=event, timeout=10)
    r.raise_for_status()


# ---- message templates --------------------------------------------------------------------------
NOTICE = ("This is a simulation prototype of PriceBack, built for a hackathon. Payments run in PayPal's sandbox "
          "with test money: no real money was charged or refunded, and no real order or personal financial data is "
          "involved. Your email is used only to send these demo messages.")


def _date(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%b %d, %Y").replace(" 0", " ")


def render_html(subject: str, text: str, cta_url: str | None) -> str:
    """A small receipt-style email. All dynamic text is escaped."""
    paras = "".join(
        f'<p style="margin:0 0 14px;line-height:1.55">{html.escape(p).replace(chr(10), "<br>")}</p>'
        for p in text.split("\n\n") if p.strip())
    button = (f'<p style="margin:22px 0 6px"><a href="{html.escape(cta_url, quote=True)}" style="background:#e23d22;'
              'color:#fff7f1;padding:12px 20px;border-radius:999px;text-decoration:none;font-weight:600">'
              'View my orders</a></p>') if cta_url else ""
    return (
        '<div style="background:#f2ede3;padding:28px 12px;font-family:Segoe UI,Helvetica,Arial,sans-serif;color:#141210">'
        '<div style="max-width:520px;margin:0 auto;background:#fbf8f2;border:1px solid #d9d0bf;border-radius:14px;overflow:hidden">'
        '<div style="height:6px;background:repeating-linear-gradient(90deg,#e23d22 0 18px,transparent 18px 26px)"></div>'
        '<div style="margin:18px 28px 0;padding:12px 14px;border-radius:10px;background:#f4e3c2;color:#6b3f05;'
        'font:13px/1.5 Segoe UI,Helvetica,Arial,sans-serif"><b>Simulation prototype, not real.</b> '
        f'{html.escape(NOTICE.split(". ", 1)[1])}</div>'
        '<div style="padding:26px 28px 22px">'
        '<div style="font:600 11px/1 Consolas,monospace;letter-spacing:.16em;text-transform:uppercase;color:#776f5f">PriceBack</div>'
        f'<h1 style="font-size:22px;line-height:1.2;margin:12px 0 16px;letter-spacing:-.02em">{html.escape(subject)}</h1>'
        f'{paras}{button}</div>'
        '<div style="padding:14px 28px;border-top:1px dashed #c4b9a3;font:12px/1.5 Consolas,monospace;color:#776f5f">'
        'PriceBack demo prototype (PayPal sandbox, test money only). You get this because you signed in to try it.</div>'
        '</div></div>')


def _order_message(o: sqlite3.Row) -> tuple[str, str]:
    paid = fmt_amount(o["paid_cents"])
    if not o["policy_json"]:
        return (f"Order #{o['id']} confirmed",
                f"Thanks for buying {o['title']} for ${paid}.\n\n"
                "No price-drop guarantee was active when you paid, so this order is not covered.")
    p = Policy.model_validate_json(o["policy_json"])
    until = _date(o["captured_at"] + p.window_days * 86400)
    if o["test_policy_id"]:
        return (f"Order #{o['id']} confirmed: covered by your Testing promise ({p.window_days} days)",
                f"Thanks for buying {o['title']} for ${paid}.\n\n"
                "This order is covered by the Testing promise you created in the merchant console, not by the "
                f"store's real guarantee. It works the same way: if the price falls before {until}, the difference "
                f"(up to ${fmt_amount(to_cents(p.max_refund_per_order))}) is refunded in the PayPal sandbox and you get "
                "an email, labelled as a test.")
    return (f"Order #{o['id']} confirmed: price protected for {p.window_days} days",
            f"Thanks for buying {o['title']} for ${paid}.\n\n"
            f"Your order is covered by our price-drop guarantee until {until}. If the price falls before then, "
            f"we refund the difference (up to ${fmt_amount(to_cents(p.max_refund_per_order))}) to your PayPal account "
            "automatically, and email you here.")


def _settlement_message(x: sqlite3.Row) -> tuple[str, str, str] | None:
    """-> (kind, subject, text), or None when the shopper isn't told (e.g. a refund PayPal rejected)."""
    if x["action"] == "refund" and x["status"] != "FAILED":
        amt = fmt_amount(x["amount_cents"])
        lines = (x["explanation"] or
                 f"The price of {x['title']} dropped from ${fmt_amount(x['paid_cents'])} to ${fmt_amount(x['market_cents'])}. "
                 f"We refunded ${amt} to your PayPal account.")
        receipt = (f"You paid:      ${fmt_amount(x['paid_cents'])}\n"
                   f"New price:     ${fmt_amount(x['market_cents'])}\n"
                   f"Refunded now:  ${amt}\n"
                   f"PayPal refund: {x['paypal_refund_id'] or 'processing'}")
        if x["test_policy_id"]:
            return ("refund_sent", f"[Testing promise] The price dropped. We refunded you ${amt}",
                    f"Test refund under the Testing promise you created in the merchant console.\n\n{lines}\n\n{receipt}")
        return "refund_sent", f"The price dropped. We refunded you ${amt}", f"{lines}\n\n{receipt}"
    if x["action"] == "blocked":
        why = {
            "order_cap_reached": "you have already received the most this guarantee pays on one order",
            "daily_budget_exhausted": "the store's refund budget for today is used up; the guarantee will retry tomorrow while your window is open",
        }.get(x["reason"], x["reason"].replace("_", " "))
        return ("refund_blocked", f"The price of {x['title']} dropped again",
                f"The price of {x['title']} is now ${fmt_amount(x['market_cents'])}, but no refund was sent this time: {why}.")
    return None


# ---- outbox -------------------------------------------------------------------------------------
def collect(conn: sqlite3.Connection, now: int | None = None) -> list[int]:
    """Queue a message for every new shopper-facing event. Returns the new outbox ids."""
    now = int(now if now is not None else time.time())
    new: list[int] = []

    def queue(email, kind, ref, order_id, subject, text):
        cur = conn.execute("INSERT OR IGNORE INTO notifications (email, kind, ref, order_id, subject, body, status, created_at)"
                           " VALUES (?,?,?,?,?,?,?,?)", (email, kind, ref, order_id, subject, text, "queued", now))
        if cur.rowcount:
            new.append(cur.lastrowid)

    for o in conn.execute("SELECT * FROM orders o WHERE buyer_email IS NOT NULL AND status='CAPTURED'"
                          " AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.ref='o' || o.id)").fetchall():
        subject, text = _order_message(o)
        queue(o["buyer_email"], "order_confirmed", f"o{o['id']}", o["id"], subject, text)
    for x in conn.execute(
            "SELECT s.*, o.buyer_email, o.title, o.paid_cents, o.test_policy_id FROM settlements s"
            " JOIN orders o ON o.id=s.order_id"
            " WHERE o.buyer_email IS NOT NULL AND s.status!='pending_api'"
            " AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.ref='s' || s.id)").fetchall():
        m = _settlement_message(x)
        if m:
            queue(x["buyer_email"], m[0], f"s{x['id']}", x["order_id"], m[1], m[2])
    return new


def deliver(notification_id: int) -> str:
    s = get_settings()
    with closing(connect()) as conn:
        if conn.execute("UPDATE notifications SET status='sending' WHERE id=? AND status='queued'",
                        (notification_id,)).rowcount != 1:
            return "skipped"
        n = conn.execute("SELECT * FROM notifications WHERE id=?", (notification_id,)).fetchone()
        account = f"{s.public_base_url}/account"
        channels, errors = [], []
        if email_configured():
            try:
                send_email(n["email"], n["subject"], f"[SIMULATION PROTOTYPE] {NOTICE}\n\n{n['body']}\n\nYour orders: {account}",
                           render_html(n["subject"], n["body"], account))
                channels.append(f"email:{email_provider()}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"email: {e}")
        if s.zapier_hook_url:
            try:
                post_zapier({"event": n["kind"], "email": n["email"], "subject": n["subject"], "message": n["body"],
                             "order_id": n["order_id"], "account_url": account, "created_at": n["created_at"]})
                channels.append("zapier")
            except Exception as e:  # noqa: BLE001
                errors.append(f"zapier: {e}")
        status = "sent" if channels else ("failed" if errors else "not_configured")
        conn.execute("UPDATE notifications SET status=?, channel=?, error=?, sent_at=? WHERE id=?",
                     (status, ", ".join(channels) or None, "; ".join(errors)[:500] or None,
                      int(time.time()) if channels else None, notification_id))
        if errors:
            log.warning("notification %s: %s", notification_id, errors)
        return status


def flush(conn: sqlite3.Connection) -> int:
    """Queue new events and deliver them (in the background unless SYNC)."""
    try:
        ids = collect(conn)
    except Exception:  # noqa: BLE001 - notifications must never break checkout or settlement
        log.exception("could not queue notifications")
        return 0
    for i in ids:
        if SYNC:
            deliver(i)
        else:
            _pool.submit(deliver, i)
    return len(ids)


def send_login_code(email: str, code: str) -> None:
    subject = f"{code} is your PriceBack sign-in code"
    text = f"Your sign-in code is {code}. It works for 10 minutes.\n\nIf you didn't ask for it, you can ignore this email."
    html_body = render_html("Your sign-in code", f"{text}", None).replace(
        html.escape(f"Your sign-in code is {code}."),
        f'Your sign-in code is <b style="font:700 26px Consolas,monospace;letter-spacing:.2em">{code}</b>.', 1)
    send_email(email, subject, f"{text}\n\n[SIMULATION PROTOTYPE] {NOTICE}", html_body)
