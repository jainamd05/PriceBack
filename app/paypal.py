"""Thin PayPal REST client: OAuth, Orders v2, capture refunds, webhook verification.

Direct REST calls (not the Agent Toolkit) so the money flow is explicit and testable.
"""
from __future__ import annotations

import json
import time
from typing import Mapping

import httpx

from .money import fmt_amount, to_cents


class PayPalError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class PayPalClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        base_url: str = "https://api-m.sandbox.paypal.com",
        webhook_id: str = "",
        allow_live: bool = False,
        http: httpx.Client | None = None,
    ):
        if "sandbox" not in base_url and not allow_live:
            raise ValueError("Refusing to use a non-sandbox PayPal URL (set PAYPAL_ALLOW_LIVE=true to override).")
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self.webhook_id = webhook_id
        self._http = http or httpx.Client(timeout=20)
        self._token: str | None = None
        self._token_exp = 0.0

    # ---- plumbing -------------------------------------------------------------
    def _auth(self) -> str:
        if self._token and time.time() < self._token_exp - 30:
            return self._token
        r = self._http.post(
            f"{self.base_url}/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            auth=(self.client_id, self.client_secret),
            headers={"Accept": "application/json"},
        )
        self._check(r)
        j = r.json()
        self._token = j["access_token"]
        self._token_exp = time.time() + int(j.get("expires_in", 300))
        return self._token

    @staticmethod
    def _check(r: httpx.Response) -> None:
        if r.status_code >= 400:
            raise PayPalError(f"PayPal {r.status_code}: {r.text[:400]}", r.status_code)

    def _request(self, method: str, path: str, *, json_body=None, content: str | None = None,
                 headers: Mapping[str, str] | None = None) -> dict:
        h = {"Authorization": f"Bearer {self._auth()}", "Content-Type": "application/json"}
        h.update(headers or {})
        r = self._http.request(method, f"{self.base_url}{path}", json=json_body, content=content, headers=h)
        self._check(r)
        return r.json() if r.content else {}

    # ---- Orders v2 --------------------------------------------------------------
    def create_order(self, *, amount_cents: int, currency: str, description: str, reference_id: str,
                     return_url: str, cancel_url: str) -> dict:
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": reference_id[:256],
                "description": description[:127],
                "amount": {"currency_code": currency, "value": fmt_amount(amount_cents)},
            }],
            "payment_source": {"paypal": {"experience_context": {
                "return_url": return_url,
                "cancel_url": cancel_url,
                "user_action": "PAY_NOW",
                "shipping_preference": "NO_SHIPPING",
            }}},
        }
        j = self._request("POST", "/v2/checkout/orders", json_body=body)
        links = {l.get("rel"): l.get("href") for l in j.get("links", [])}
        approve = links.get("payer-action") or links.get("approve")
        if not approve:
            raise PayPalError("PayPal order created but no approval link returned")
        return {"id": j["id"], "approve_url": approve}

    def capture_order(self, order_id: str) -> dict:
        j = self._request("POST", f"/v2/checkout/orders/{order_id}/capture")
        try:
            cap = j["purchase_units"][0]["payments"]["captures"][0]
        except (KeyError, IndexError) as e:
            raise PayPalError(f"Unexpected capture response shape: {e}") from e
        return {
            "order_status": j.get("status"),
            "capture_id": cap["id"],
            "capture_status": cap.get("status"),
            "amount_cents": to_cents(cap["amount"]["value"]),
            "currency": cap["amount"]["currency_code"],
        }

    # ---- Refunds ----------------------------------------------------------------
    def refund_capture(self, *, capture_id: str, amount_cents: int, currency: str,
                       request_id: str, note: str = "") -> dict:
        """Partial refund. PayPal-Request-Id makes retries safe: the same id never refunds twice."""
        body = {"amount": {"value": fmt_amount(amount_cents), "currency_code": currency}}
        if note:
            body["note_to_payer"] = note[:255]
        j = self._request("POST", f"/v2/payments/captures/{capture_id}/refund",
                          json_body=body, headers={"PayPal-Request-Id": request_id})
        return {"id": j["id"], "status": j.get("status", "COMPLETED")}

    # ---- Webhooks ---------------------------------------------------------------
    def verify_webhook(self, headers: Mapping[str, str], raw_body: str) -> bool:
        """Ask PayPal to verify the signature. The raw body is embedded verbatim so that
        re-serialisation can't change the bytes the signature was computed over."""
        if not self.webhook_id:
            return False
        try:
            fields = {
                "auth_algo": headers["paypal-auth-algo"],
                "cert_url": headers["paypal-cert-url"],
                "transmission_id": headers["paypal-transmission-id"],
                "transmission_sig": headers["paypal-transmission-sig"],
                "transmission_time": headers["paypal-transmission-time"],
                "webhook_id": self.webhook_id,
            }
        except KeyError:
            return False
        payload = "{" + ",".join(f"{json.dumps(k)}:{json.dumps(v)}" for k, v in fields.items()) \
                  + f',"webhook_event":{raw_body}' + "}"
        j = self._request("POST", "/v1/notifications/verify-webhook-signature", content=payload)
        return j.get("verification_status") == "SUCCESS"
