# PriceBack — a price-drop guarantee engine on PayPal

A merchant types a promise in plain English:

> "Refund the difference if the price drops within 14 days, up to $20 per order and $100 per day."

PriceBack compiles it into a validated policy, watches market prices for every covered order, and issues
the **partial refund through PayPal** automatically — with hard guardrails, idempotent retries, a
full audit trail, and a webhook-verified ledger.

Built for the [PayPal AI Hackathon](https://paypalaihackathon.devpost.com/). PayPal is the core: take it out
and nothing works. The AI model is swappable.

## How it works

```
merchant promise ──► policy compiler (LLM, regex fallback) ──► validated Policy ──► merchant reviews & activates
shopper ──► PayPal Orders v2 (create → approve → capture) ──► order stored with a snapshot of the promise
watcher ──► market price (Channel3 live, or simulated) ──► evaluate() guardrails ──► PayPal partial refund
PayPal ──► signed webhooks ──► ledger confirms captures & refunds
```

**PayPal APIs used:** Orders v2 (create, capture), Payments v2 (partial refund of a capture, with
`PayPal-Request-Id`), Webhooks (signature verification, `PAYMENT.CAPTURE.COMPLETED`, `PAYMENT.CAPTURE.REFUNDED`).

**AI:** the LLM turns the promise into JSON and writes the customer message. It never touches money — only a
validated `Policy` reaches the refund path, and every amount is computed by deterministic code.

**Catalog:** the merchant adds products in the console (`/merchant → Catalog`), and/or imports them from
**Channel3** (`POST /v1/search`, live market price via `GET /v1/products/{id}`, lowest in-stock offer) when
`CHANNEL3_API_KEY` is set. There is no built-in sample catalog: a fresh install starts with an empty shelf.

## Safety properties (all unit-tested)

- Per-order cap is **cumulative**; a separate **daily budget** is shared across all orders.
- Decision + write-ahead `pending_api` row commit in one `BEGIN IMMEDIATE` transaction, so concurrent runs can't both spend the same cap.
- `PayPal-Request-Id` is derived from (order, price, refunded-so-far, attempt): retrying the same intent **cannot refund twice**.
- Network errors / 5xx / 429 are *unknown outcomes*: they stay pending and are retried with the **same** key. Only definite 4xx rejections become `FAILED`.
- Checkout price always comes from the server catalog, never from the client; captured amount is verified against it.
- Orders keep the promise they bought under; orders placed before any promise are never refunded.
- Webhooks: signature verified via PayPal (raw body forwarded verbatim), deduplicated by event id; **unverified events never change state**.
- Refuses non-sandbox PayPal URLs unless `PAYPAL_ALLOW_LIVE=true`.

## Quickstart (no keys needed for the pages)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # fill in what you have
pytest -q                     # 42 tests, no network
uvicorn app.main:app --reload # http://localhost:8000  (store)   /merchant (dashboard)
```

Without PayPal credentials the store and merchant pages load but checkout returns a clear 503.

## PayPal sandbox setup — do this first (~30 min)

1. developer.paypal.com → **Apps & Credentials → Sandbox → Create app**. Copy Client ID / Secret into `.env`.
2. Make sure the sandbox has a **Business** (merchant) and a **Personal** (buyer) test account — you pay with the Personal one.
3. Run the app, open `/merchant`, add a product (or import from Channel3), then compile + activate the promise.
4. On `/` buy a product → approve in the PayPal sandbox popup with the buyer account → you land back on the store (`?paid=1`).
5. On `/merchant`: pick the product → **Simulate price drop** → **Run watcher now**. A refund row appears with status `COMPLETED`.
6. Confirm the refund in the sandbox business account's activity.
7. **Webhooks:** Dashboard → your app → *Webhooks → Add webhook* with URL `${PUBLIC_BASE_URL}/api/webhooks/paypal` and events
   `PAYMENT.CAPTURE.COMPLETED`, `PAYMENT.CAPTURE.REFUNDED`. Put the *Webhook ID* in `PAYPAL_WEBHOOK_ID`.
   Locally you need a tunnel (ngrok / cloudflared) because PayPal must reach the URL. Rows in the ledger show *verified*.

If step 5 fails, the error text is stored on the settlement row (`/merchant` → "Customer message" column).

## Configuration

See `.env.example`. Key variables: `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_WEBHOOK_ID`, `PUBLIC_BASE_URL`,
`CHANNEL3_API_KEY`, `GEMINI_API_KEY` (optional; model via `LLM_MODEL`), `ADMIN_TOKEN` (**required on any public deployment**),
`WATCH_INTERVAL_SECONDS`.

## API

| Public | |
|---|---|
| `GET /api/products`, `GET /api/guarantee` | catalog, active promise (summary) |
| `POST /api/checkout` | create PayPal order → `approve_url` |
| `GET /checkout/return` | capture + snapshot the promise |
| `POST /api/webhooks/paypal` | verified webhook ledger |

| Merchant (`x-admin-token` when `ADMIN_TOKEN` is set) | |
|---|---|
| `POST /api/policy/compile`, `POST /api/policy/activate`, `GET /api/policy` | promise lifecycle |
| `POST /api/simulate/drop`, `POST /api/simulate/reset` | **simulated** price changes |
| `POST /api/products`, `PATCH /api/products/{id}`, `DELETE /api/products/{id}` | add / edit (name, photo, category; never the price) / remove a catalog product |
| `POST /api/watch/run`, `POST /api/catalog/refresh` | settle now, import from Channel3 |
| `GET /api/orders`, `/api/settlements`, `/api/ledger`, `/api/stats` | audit trail & dashboard data |

## Deploy on Render

`render.yaml` is included. Set the secret env vars in the Render dashboard (`PUBLIC_BASE_URL` must be your
`https://…onrender.com` URL), then register the webhook (step 7). The free plan's disk is ephemeral, so the SQLite DB
resets on each deploy — fine for a demo; attach a disk for durability.

## Honest limits

- **Price drops in the demo are simulated** (clearly labelled `simulated` in the DB, API and UI). Channel3 prices are
  real for the baseline and the live check, but real prices won't move within a hackathon.
- "Market price" = the lowest in-stock Channel3 offer in USD; it is a proxy, not the merchant's own competitor feed.
- Sandbox only: refund timing, fees and payer funding sources aren't representative of production.
- Channel3's docs don't specify the image field in search results; it's parsed defensively. `/merchant → Import from Channel3`
  prints the response keys so you can confirm.
- Single-process SQLite; no multi-merchant tenancy, auth is a single shared admin token.

## Sponsor tools

| Tool | Status |
|---|---|
| PayPal (Orders, Refunds, Webhooks) | core, implemented |
| Channel3 (search + live price) | implemented (needs `CHANNEL3_API_KEY`) |
| Render | `render.yaml` included |
| AG Grid / AG Studio | not yet integrated (planned for the merchant dashboard) |

## Tests

`pytest -q` — policy parsing (LLM + fallback), guardrail math, idempotency/recovery, the full checkout →
simulated drop → refund flow, admin auth, and webhook verification/dedup, all against an in-memory PayPal fake
that mimics `PayPal-Request-Id` semantics.

## License

MIT
