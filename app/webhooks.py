"""PayPal webhook ledger. Events are deduplicated by id; only signature-verified events change state."""
from __future__ import annotations

import json
import sqlite3
import time

from .db import tx


def process_event(conn: sqlite3.Connection, event: dict, verified: bool) -> str:
    """-> 'duplicate' | 'recorded' (logged only) | 'applied'."""
    event_id = event.get("id")
    if not event_id:
        raise ValueError("Webhook event has no id")
    etype = event.get("event_type", "")
    resource = event.get("resource") or {}
    rid = resource.get("id")
    now = int(time.time())

    with tx(conn):
        cur = conn.execute(
            "INSERT OR IGNORE INTO webhook_events (event_id, event_type, resource_id, verified, payload, received_at)"
            " VALUES (?,?,?,?,?,?)", (event_id, etype, rid, 1 if verified else 0, json.dumps(event), now))
        if cur.rowcount == 0:
            return "duplicate"
        if not verified or not rid:
            return "recorded"
        if etype == "PAYMENT.CAPTURE.REFUNDED":
            # resource.id is the refund id
            conn.execute(
                "UPDATE settlements SET webhook_confirmed_at=?, status=COALESCE(?, status) "
                "WHERE paypal_refund_id=?", (now, resource.get("status"), rid))
        elif etype == "PAYMENT.CAPTURE.COMPLETED":
            conn.execute("UPDATE orders SET capture_webhook_at=? WHERE capture_id=?", (now, rid))
        else:
            return "recorded"
    return "applied"
