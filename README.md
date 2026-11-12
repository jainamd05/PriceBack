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
pytest -q                     # 65 tests, no network
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

## Try it as a shopper (for judges)

1. Open the store and click **Sign in** (or just **Pay with PayPal**). Enter your own email; we send a one-time code.
2. Buy any product. A sheet shows the **PayPal sandbox buyer login** to use on PayPal's page (fake money).
3. You land back on the store; open **My orders** (`/account`). You get an *order confirmed* email.
4. Pick a drop (−5 % … −50 %) and click **Simulate this price drop**. The engine settles *your order only*: PayPal refunds the
   difference (up to the cap) and a *we refunded you $X* email arrives within seconds. Drop it further to see the cap block it.

Emails go out through **SMTP with Python's `smtplib`** (e.g. Gmail with an app password; set `SMTP_*`) or through
**Brevo's HTTPS API** (`BREVO_API_KEY`). Both are free; use Brevo on Render's free plan, which blocks outbound SMTP. Each event is stored in
a `notifications` outbox with a unique key, so a shopper is never emailed twice for the same event. If `ZAPIER_HOOK_URL`
is set, every event is **also POSTed to a Zapier Catch Hook** as JSON (`event`, `email`, `subject`, `message`, `order_id`,
`account_url`), ready for any Zap (Slack, Google Sheets, SMS…). Sign-in codes are never sent to Zapier.

## Try it as a merchant (for judges)

Open `/merchant` and click **Explore as a guest** (on when `DEMO_MERCHANT_GUEST=true`, as in `render.yaml`). Guests see the
overview, refund decisions, orders, coverage timeline and webhooks, and can drop prices in the **Price lab** and run the
watcher, which refunds every covered buyer of that product. Only the owner (`ADMIN_TOKEN`) can add, edit, remove or import
products. Guests see shoppers' emails masked (`ju•••@example.com`) and share an hourly allowance of AI promise previews.

**Testing promises.** A promise a guest activates never replaces the store's real one. It becomes a *Testing promise*:
shown only in that guest's browser (a random `pb_gid` cookie), labelled "Testing promise (yours)" on the store, and gone
after **3 hours**. Orders placed under it use its terms, are labelled Testing in the console, on *My orders* and in the
emails, and draw on the test promise's own daily budget, never the store's. Guest promises are held to demo limits
(≤ 30 days, ≤ $100 per order, ≤ $200 a day). So a judge can write a promise, buy under it and watch the refund, while other
shoppers keep seeing the real guarantee.

## API

| Public | |
|---|---|
| `GET /api/products`, `GET /api/guarantee`, `GET /api/demo` | catalog, active promise (summary), sandbox buyer login for testers |
| `POST /api/auth/start`, `POST /api/auth/verify`, `POST /api/auth/logout`, `GET /api/me` | shopper sign-in with an emailed 6-digit code |

| Shopper (signed in) | |
|---|---|
| `POST /api/checkout` | create PayPal order → `approve_url` (the order is tied to the shopper's email) |
| `GET /api/me/orders` | my orders, protection window, refund decisions, emails sent |
| `POST /api/me/orders/{id}/simulate-drop` | **demo**: simulate a market drop on *my* order only and settle it |
| `GET /checkout/return` | capture + snapshot the promise |
| `POST /api/webhooks/paypal` | verified webhook ledger |

| Merchant (`x-admin-token` when `ADMIN_TOKEN` is set) | |
|---|---|
| `POST /api/policy/compile`, `POST /api/policy/activate`, `GET /api/policy` | promise lifecycle |
| `POST /api/simulate/drop`, `POST /api/simulate/reset` | **simulated** price changes |
| `POST /api/products`, `PATCH /api/products/{id}`, `DELETE /api/products/{id}` | add / edit (name, photo, category; never the price) / remove a catalog product |
| `POST /api/watch/run`, `POST /api/catalog/refresh` | settle now, import from Channel3 |
| `GET /api/orders`, `/api/settlements`, `/api/ledger`, `/api/stats`, `/api/notifications` | audit trail, dashboard data, notification outbox |

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

| Tool | Where | What it does in PriceBack |
|---|---|---|
| **PayPal** (Orders v2, Payments v2 refunds, Webhooks) | core | Checkout, capture, automatic partial refunds, verified webhook ledger |
| **Channel3** (search + live price) | `app/channel3.py`, `/merchant → Catalog → Import from Channel3` | Imports real products and supplies the live market price the watcher compares against (needs `CHANNEL3_API_KEY`) |
| **AG Grid** Community | `app/static/console-grids.js` | The console's data grids: **Orders** (sort, filter, search, CSV export, a totals row that follows your filters, "cover ends" and "still at risk" columns), **Refund decisions → Table** (an auditable, exportable ledger of every refund and guardrail block), **Webhooks**, and **Catalog → Table** (edit names and categories in place, multi-select bulk remove) |
| **Bryntum Scheduler** (trial build) | `app/static/console-timeline.js`, `/merchant → Coverage timeline` | One row per covered order: the bar is the protection window the buyer bought under, diamonds are each refund or block on that order, and a line marks *now*. Shows at a glance which guarantees are open, which have used up their cap, and what expires next |
| **Zapier** (optional) | `app/notify.py` | Mirrors every shopper event (order confirmed, refund sent, refund blocked) to a Zapier Catch Hook when `ZAPIER_HOOK_URL` is set |
| **Render** | `render.yaml` | One-click Blueprint deploy (Python web service, health check, generated `ADMIN_TOKEN`, watcher every 60 s) |

AG Grid and Bryntum are loaded from the jsDelivr CDN (`ag-grid-community@34.3.1`, `@bryntum/scheduler-trial@7.3.7`), so
there is still no build step. If the CDN is unreachable, the console falls back to its plain HTML tables.

## Tests

`pytest -q` — policy parsing (LLM + fallback), guardrail math, idempotency/recovery, the full checkout →
simulated drop → refund flow, admin auth, and webhook verification/dedup, all against an in-memory PayPal fake
that mimics `PayPal-Request-Id` semantics.

## License

MIT
