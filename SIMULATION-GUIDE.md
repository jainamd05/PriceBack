# PriceBack — Hands-on Simulation Guide

This guide walks you through the whole PriceBack process yourself, click by click: a merchant makes a promise,
a shopper pays with PayPal, the price drops, and PayPal refunds the difference automatically.
Every step says **what to click**, **what to type**, **what you should see**, and **what happened behind the scenes**.

> Everything runs in the **PayPal Sandbox**. It is fake money: nothing is charged to a real card or account.

---

## The story in one picture

```
 YOU AS THE MERCHANT                 YOU AS THE SHOPPER                 PRICEBACK (automatic)
 ───────────────────                 ──────────────────                 ─────────────────────
 1. Write a promise in English  ──►  2. Buy a product with PayPal  ──►  3. Watch the market price
    "refund the difference..."          (sandbox login + Pay Now)          4. Price dropped? Check the rules
                                                                           5. Send a partial refund via PayPal
                                                                           6. Log everything (audit trail)
```

You will use **three browser tabs**:

| Tab | Address | Who you are |
|---|---|---|
| Merchant dashboard | http://localhost:8000/merchant | the shop owner |
| Store | http://localhost:8000 | the shopper |
| PayPal Sandbox | opens by itself when you click *Buy with PayPal* | the shopper paying |

---

## Step 0 — Collect three things before you start (one time, ~5 min)

### 0.1 Your admin token (the password for the merchant dashboard)

1. Open the project folder `C:\Users\Owner\OneDrive\Desktop\Projects\PriceBack`.
2. Open the file **`.env`** with Notepad or VS Code.
3. Find the line that starts with `ADMIN_TOKEN=`. In your file the value is inside **quotation marks**, like
   `ADMIN_TOKEN="abcde"`. Copy only what's **inside** the quotes (5 characters), **without** the `"` marks.
   Keep it handy; you will paste it in Step 2.

### 0.2 Your PayPal Sandbox **Personal** (buyer) login: used to pay

1. Go to **https://developer.paypal.com** and click **Log in to Dashboard** (top right).
   Log in with your **real** PayPal account (the one you used to create the sandbox app).
2. In the left menu, click **Testing Tools → Sandbox Accounts**.
3. You will see a list of accounts. Find the one whose **Type** is **Personal**.
   Its email looks like `sb-xxxxxxxx@personal.example.com`.
4. Click the **⋯** (three dots) at the end of that row → **View/Edit account**.
5. Check its **Country** is **United States**. The store charges in USD, and an India-based (INR) buyer gets
   *"This seller doesn't accept payments in your currency"*. If it isn't US, click **Create account** →
   **Personal**, Country **United States**, and use that new account instead.
6. Note down the **Email ID** and the **Password** shown there.
   (If no password is shown, click *Change password* and set one you will remember, e.g. `Test1234!`.)

### 0.3 Your PayPal Sandbox **Business** (merchant) login: used to check the refund at the end

Same page as 0.2, but the row whose **Type** is **Business** (`sb-xxxxxxxx@business.example.com`).
Note its **Email ID** and **Password** the same way.

> ⚠ **Both** sandbox accounts must have Country **United States**. The Business one must also be the account your
> sandbox **app** (the Client ID / Secret in `.env`) belongs to. If the seller isn't US-based, PayPal stops at
> *"This seller doesn't accept payments in your currency"*. See the troubleshooting table for the fix.

> Write them down like this so you don't mix them up:
>
> | Account | Email | Password | Used in |
> |---|---|---|---|
> | Admin token | (none) | from `.env` | Step 2 |
> | Sandbox **Personal** (buyer) | sb-…@personal.example.com | … | Step 4 (paying) |
> | Sandbox **Business** (merchant) | sb-…@business.example.com | … | Step 7 (checking the refund) |

---

## Step 1 — Start the app

1. Open **PowerShell** (Start menu → type *PowerShell* → Enter).
2. Copy-paste these two lines and press **Enter**:

   ```powershell
   cd "C:\Users\Owner\OneDrive\Desktop\Projects\PriceBack"
   .\.venv\Scripts\python -m uvicorn app.main:app --reload
   ```

3. **You should see** (last line):

   ```
   INFO:     Application startup complete.
   ```

4. **Leave this window open** the whole time. Closing it stops the app.
   To stop the app at the very end, click in this window and press **Ctrl + C**.

> **Behind the scenes:** the server (FastAPI) starts, opens the database file `priceback.db`, and loads the
> product catalog (6 demo products: headphones, earbuds, speaker, keyboard, mouse, webcam).

---

## Step 2 — Open the merchant dashboard and log in

1. Open Chrome (or Edge) and go to **http://localhost:8000/merchant**
2. A small browser box pops up saying **"Admin token"**.
   **Paste the token from Step 0.1** and click **OK**.
   - ⚠ The box may pop up **several times in a row** (up to 5) the very first time. That's a known quirk:
     paste the **same token** each time. After that, this tab remembers it.
   - If you click *Cancel* by mistake, just reload the page (F5).
3. **You should see** the page **"PriceBack · Merchant"** with these sections:
   - **1 · Your promise**: a text box with a sentence in it.
   - **2 · Demo controls**: a product dropdown, *Drop %*, and buttons.
   - **Six number tiles**: *Guaranteed orders*, *Revenue under guarantee*, *Refunded (total)*, …
   - Three tables: **Refund decisions (audit trail)**, **Orders**, **PayPal webhook ledger**.

> In **Orders** you may already see `#1 Mechanical Keyboard · CREATED · Guaranteed: no`.
> That's an old checkout that was started but never paid. **Ignore it**: PriceBack never refunds an unpaid order.

---

## Step 3 — (Merchant) Write and activate the promise

1. In **1 · Your promise**, make sure the text box contains exactly:

   ```
   Refund the difference if the price drops within 14 days, up to $20 per order and $100 per day.
   ```

2. Click **Compile promise**.
3. **You should see** a row of four editable boxes appear:

   | Field | Value |
   |---|---|
   | Window (days) | `14` |
   | Max refund / order ($) | `20` |
   | Daily budget ($) | `100` |
   | Min drop (%) | `0` (or empty) |

   plus a small badge **parsed by llm** (Gemini read it) or **parsed by rules** (backup reader used).
4. Check the numbers match the sentence. **Don't change anything.**
5. Click **Review OK → Activate**.
6. **You should see** a green message:
   *"Promise is live. New orders are covered by it; existing orders keep the promise they bought under."*
   and next to the Compile button: **Active: "Refund the difference…"**.

> **Behind the scenes:**
> - The AI (Gemini) turns your sentence into these 4 numbers. If Gemini isn't available, a rule-based parser
>   reads the same sentence instead. Either way the result is checked against strict limits
>   (e.g. window 1–90 days, cap $0–$1,000).
> - **The AI never moves money.** It only fills in the form. *You* approve the numbers, and from then on plain,
>   tested code does all the maths.

---

## Step 4 — (Shopper) Buy the headphones with PayPal

1. Open a **new tab** and go to **http://localhost:8000**
2. **You should see** "PriceBack Demo Store" with a **green banner** at the top:
   *"Price-drop guarantee: Refund the difference if the price drops within 14 days… · 14 days · up to $20.00 per order"*
3. Find **Wireless Over-Ear Headphones — $129.00** and click **Buy with PayPal**.
4. The tab moves to the **PayPal Sandbox** (address starts with `https://www.sandbox.paypal.com/…`).

### The PayPal login form

| Screen | What to type / click |
|---|---|
| **"Pay with PayPal"**, box *Email or mobile number* | type the **Personal** sandbox email from Step 0.2 (`sb-…@personal.example.com`) → click **Next** |
| Box *Password* | type the **Personal** sandbox password → click **Log In** |
| Review screen showing **$129.00 USD** and a payment method (PayPal balance or a test Visa) | leave the payment method as it is → click the blue **Pay Now** button (some versions say **Complete Purchase**) |

> - Use the **Personal** account here, **not** the Business one: a shop can't pay itself.
> - If the page says "Things don't appear to be working at the moment", wait 30 seconds and click *Buy with PayPal* again.
>   The sandbox is sometimes slow.
> - You are **not** asked for shipping (the order is set to *no shipping*).

5. PayPal sends you back to the store. **You should see** a green message at the top:
   *"Payment received (order #2). Your purchase is covered by the price-drop guarantee if one was active."*
   (The number may be higher if you tried before. Remember it: this is **your order number**.)

> **Behind the scenes (3 PayPal API calls):**
> 1. **Create order:** the server asks PayPal for a $129.00 order. The price comes from the server's
>    catalog, **never from the browser**, so nobody can edit the page and pay $1.
> 2. **You approve** it on PayPal's page.
> 3. **Capture:** when you come back, the server takes the money and checks that PayPal captured exactly $129.00.
>    It also **saves a copy of the active promise onto the order** ("snapshot"). If the merchant changes the
>    promise later, this order keeps the promise it was bought under.

---

## Step 5 — (Merchant) See the order on the dashboard

1. Switch back to the **merchant tab** and press **F5** (it also refreshes by itself every 10 seconds).
2. **You should see**:

   **Orders table:** a new row

   | # | Product | Paid | Status | Guaranteed |
   |---|---|---|---|---|
   | 2 | Wireless Over-Ear Headphones | $129.00 | **CAPTURED** | **yes** |

   **Tiles:**

   | Tile | Value | Meaning |
   |---|---|---|
   | Guaranteed orders | `1` | paid orders covered by a promise |
   | Revenue under guarantee | `$129.00` | money from those orders |
   | Refunded (total) | `$0.00` | nothing refunded yet |
   | Daily budget left | `$100.00` | today's refund budget |
   | Open exposure (worst case) | `$20.00` | the most we could still owe (the $20 cap) |
   | Blocked by guardrails | `0` | |

---

## Step 6 — (Merchant) Make the price drop → automatic refund

Real prices don't change during a demo, so the dashboard lets you **simulate** a price drop. Every simulated
price is clearly labelled **simulated** in the database, the API and the screen.

1. In **2 · Demo controls**, first click **Reset simulation** (clears any old fake prices).
   Message: *"Simulation cleared."*
2. **Product** dropdown → choose **Wireless Over-Ear Headphones · $129.00**
3. **Drop %** → type `20`
4. Click **Simulate price drop**.
   **You should see:** *"Simulated market price is now $103.20 (simulated). Run the watcher to settle."*
5. Click **Run watcher now**.
6. **You should see:**
   *"Watcher checked 1 order(s): 1 refund(s), 0 blocked by guardrails, 0 recovered."*

   And a new row in **Refund decisions (audit trail)**:

   | Order | Action | Amount | Reason | Market price | PayPal | Customer message |
   |---|---|---|---|---|---|---|
   | #2 Wireless Over-Ear Headphones | **refund** | **$20.00** | capped_by_order_cap | $103.20 `simulated` | **COMPLETED** + a refund id | "The price of Wireless Over-Ear Headphones dropped from $129.00 to $103.20 … we refunded $20.00 …" |

   Tiles now: **Refunded (total) $20.00 · Daily budget left $80.00 · Open exposure $0.00**

### Why $20.00 and not $25.80? (the maths the code did)

| | |
|---|---|
| You paid | $129.00 |
| New market price | $103.20 (20% lower) |
| Difference owed | $25.80 |
| Already refunded on this order | $0.00 |
| Cap left on this order | $20.00 (promise: up to $20 per order) |
| Daily budget left | $100.00 |
| **Refund = the smallest of (owed, cap left, budget left)** | **$20.00** → reason `capped_by_order_cap` |

> **Behind the scenes, in order:**
> 1. The **watcher** looks at every paid, guaranteed order and gets the current market price
>    (simulated price first, then live Channel3 price, then the catalog price).
> 2. A function called **`evaluate()`** runs 8 checks in order. The first failed check stops it:
>    paid? → valid price? → inside the 14 days? → price really dropped? → drop big enough? →
>    anything still owed? → cap left? → daily budget left?
> 3. Before calling PayPal, the decision is **written to the database first** inside a locked transaction,
>    so two watchers running at once can't both spend the same $20.
> 4. The refund is sent to PayPal with a unique **PayPal-Request-Id**. If the network fails and it retries,
>    PayPal sees the same id and **does not refund twice**.
> 5. Gemini writes the friendly customer message (using only the facts given), or a template is used.

---

## Step 7 — Check that the refund really happened in PayPal

1. Open a **private / incognito window** (Ctrl + Shift + N), so you are logged out of the buyer account.
2. Go to **https://www.sandbox.paypal.com**
3. Log in with the **Business** sandbox account (Step 0.3): email → **Next** → password → **Log In**.
4. Click **Activity** (top menu).
5. **You should see** the **$129.00** payment from the buyer, and a **refund of $20.00**
   (the payment may show as *Partially refunded*).

> Optional: log in with the **Personal** account instead, and you'll see the $20.00 refund *received*.

---

## Step 8 — (Merchant) Try to refund again → the guardrail blocks it

1. On the merchant tab, click **Run watcher now** again (don't change anything).
2. **You should see:** *"Watcher checked 1 order(s): 0 refund(s), 1 blocked by guardrails, 0 recovered."*
3. A new audit row: **blocked · $0.00 · order_cap_reached**. Tile **Blocked by guardrails = 1**.
4. Click **Run watcher now** a third time. The message still says *1 blocked*, but **no new row is added**:
   the same block is logged once, not every time.

> **Why:** $5.80 is technically still "owed" ($25.80 − $20.00), but this order already used its whole $20 cap.
> **The cap is cumulative**: it adds up across all refunds on the same order.

🎉 **That's the complete core flow:** promise → purchase → price drop → automatic, capped PayPal refund → blocked repeat.

---

## Step 9 — More test cases you can try

Do these in order. **A** and **B** need one more purchase (repeat **Step 4** with a different product).
**C** and **D** need no purchase.

### A. Small drop → the full difference is refunded

1. Repeat **Step 4** but buy **Ergonomic Wireless Mouse — $45.00** (same PayPal login). Note its order number (e.g. #3).
2. Merchant tab → click **Reset simulation**.
3. Product **Ergonomic Wireless Mouse · $45.00**, Drop % `10` → **Simulate price drop** → *"…now $40.50 (simulated)"*
4. **Run watcher now** → *"Watcher checked 2 order(s): 1 refund(s), 0 blocked…"*
5. New row: **refund · $4.50 · full_difference** (owed $4.50 is below the $20 cap, so it's paid in full).

### B. The price falls further → only the *extra* difference is refunded

1. Same product, Drop % `30` → **Simulate price drop** → *"…now $31.50 (simulated)"*
2. **Run watcher now** → new row: **refund · $9.00 · full_difference**
3. Why $9.00: owed is now $13.50 ($45.00 − $31.50) minus the $4.50 already refunded = **$9.00**.
   Total refunded on the mouse = $13.50.
   Tiles: **Refunded (total) $33.50**, **Daily budget left $66.50**.

### C. No price drop → nothing happens

1. Click **Reset simulation** → **Run watcher now**.
2. *"…0 refund(s), 0 blocked…"*. No new rows (the market price is back to normal, so there's nothing to refund).

### D. The daily budget stops refunds

The daily budget is shared by **all** orders and always follows the **currently active** promise.

1. In **1 · Your promise**, change the text to:
   `Refund the difference if the price drops within 14 days, up to $20 per order and $25 per day.`
2. **Compile promise** → check *Daily budget ($)* says `25` → **Review OK → Activate**.
3. Product **Ergonomic Wireless Mouse**, Drop % `50` → **Simulate price drop** (now $22.50) → **Run watcher now**.
4. New row: **blocked · daily_budget_exhausted**. Today you already refunded $33.50, which is more than $25.
5. **Put it back:** change the text back to `…and $100 per day.` → **Compile promise** → **Review OK → Activate**
   → click **Reset simulation**.

### Expected results at a glance

| Scenario | Paid | Market | Already refunded | Result | Reason |
|---|---|---|---|---|---|
| Headphones −20% | $129.00 | $103.20 | $0 | refund **$20.00** | capped_by_order_cap |
| Headphones, run again | $129.00 | $103.20 | $20.00 | **blocked** | order_cap_reached |
| Mouse −10% | $45.00 | $40.50 | $0 | refund **$4.50** | full_difference |
| Mouse −30% | $45.00 | $31.50 | $4.50 | refund **$9.00** | full_difference |
| Reset (no drop) | any | normal | any | nothing | no_drop (not logged) |
| Daily budget $25 | $45.00 | $22.50 | $13.50 | **blocked** | daily_budget_exhausted |
| Order bought **before** any promise | any | any | any | never refunded | not guaranteed |

> **Daily budget resets** at midnight UTC = **5:30 AM India time**.

---

## Step 10 — (Optional) Webhooks: PayPal confirming back to you

Right now the **PayPal webhook ledger** table says *"Nothing yet."* That's expected: PayPal can't reach
`localhost` on your laptop. To see it work you need a public tunnel:

1. Install **ngrok** (https://ngrok.com, free), then in a **second** PowerShell window run:
   `ngrok http 8000`. Copy the `https://….ngrok-free.app` address it shows.
2. developer.paypal.com → **Apps & Credentials** → click your sandbox app → **Webhooks → Add Webhook**:
   - Webhook URL: `https://<your-ngrok-address>/api/webhooks/paypal`
   - Events: tick **Payment capture completed** and **Payment capture refunded** → **Save**.
3. Copy the new **Webhook ID** into `.env` on the line `PAYPAL_WEBHOOK_ID=` and restart the app
   (Ctrl + C in the first PowerShell window, then run the start command from Step 1 again).
4. Make a new purchase and a new price drop (Steps 4 → 6).
5. **You should see:** rows in **PayPal webhook ledger** with a green **verified** badge, a ✓ in the
   **Capture webhook** column of Orders, and a **webhook ✓** badge next to the refund.

> **Behind the scenes:** every webhook is checked with PayPal's signature verification; fake or unverified
> messages never change anything, and the same event arriving twice is ignored.
> (Note: each time ngrok restarts it gives a new address, so you have to update the webhook URL.)

---

## Step 11 — Run the automated tests (no PayPal, no internet needed)

Open a **new** PowerShell window (leave the app running in the other one) and run:

```powershell
cd "C:\Users\Owner\OneDrive\Desktop\Projects\PriceBack"
.\.venv\Scripts\python -m pytest -q
```

**You should see:** `39 passed`

| Test file | What it proves |
|---|---|
| `tests/test_policy.py` | sentences turn into the right numbers; bad or out-of-range values are rejected |
| `tests/test_settle.py` | every guardrail (cap, cumulative cap, daily budget); no double refunds; network errors retried safely |
| `tests/test_api.py` | full flow checkout → drop → refund; price comes from the server; admin token required; webhooks verified and deduplicated |
| `tests/test_llm.py` | the Gemini request is built correctly; falls back to rules when Gemini is missing or rate-limited |

To run just the full end-to-end flow test with details:

```powershell
.\.venv\Scripts\python -m pytest tests/test_api.py::test_full_flow_checkout_drop_refund -v
```

These tests use a **fake PayPal** built into `tests/conftest.py` that behaves like the real one
(including "same request id = same refund"), so they run in seconds and never touch the internet.

---

## What each part of the dashboard means

| Thing on screen | Meaning |
|---|---|
| **Compile promise** | AI/rules read your sentence → 4 numbers for you to review |
| **Review OK → Activate** | makes the promise live for **new** orders |
| **Simulate price drop** | pretend the market price fell by *Drop %* (labelled `simulated`) |
| **Reset simulation** | remove all fake prices (back to real/catalog prices) |
| **Run watcher now** | check every guaranteed order right now and refund if needed (normally automatic on a timer) |
| **Refresh catalog** | ⚠ reloads products from Channel3 (live products, different names/prices). **Don't click it during this guide**, or the numbers above won't match. |
| Refund decisions | the audit trail: every refund and every block, with the reason |
| Orders | every checkout and whether it's covered |
| PayPal webhook ledger | confirmations sent by PayPal (Step 10) |
| `pending_api` status | PayPal didn't answer clearly (timeout). It will be retried with the same id on the next watcher run, so it can't double-pay. |
| `FAILED` status | PayPal definitely refused. The error text is shown in the *Customer message* column. |

---

## If something goes wrong

| Problem | Fix |
|---|---|
| PowerShell: `uvicorn` / `python` not found | make sure you ran the `cd …PriceBack` line first, and use the exact command from Step 1 |
| "Only one usage of each socket address…" / port in use | the app is already running in another window. Use that one, or close it with Ctrl + C |
| Admin token box keeps coming back / red "Missing or invalid admin token." | the token is wrong. Copy it again from `.env`: only the part after `=`, **without the quotation marks** and without spaces. Close the tab and reopen `/merchant` |
| Clicking **Buy with PayPal** shows an alert "PayPal sandbox credentials are not configured" | `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` are missing in `.env`, so restart after filling them |
| Alert "Could not start PayPal checkout" | the sandbox client id/secret are wrong or PayPal is down. Check the PowerShell window for the error |
| PayPal login says wrong password | you're using the wrong account. Use the **Personal** sandbox account, not your real PayPal and not the Business one |
| PayPal says *"You are logging in to the account of the seller for this purchase"* | you logged in with the **Business** account. Click **Log Out** and log in with the **Personal** one (`…@personal.example.com`). If none exists: Sandbox Accounts → **Create account** → Personal, United States |
| PayPal shows *"This seller doesn't accept payments in your currency"* **right after clicking Buy** (address contains `genericError?code=VU5TVVBQT1JURURfUEFZRUVfQ1VSUkVOQ1k`, which means `UNSUPPORTED_PAYEE_CURRENCY`) | the **seller** (Business) sandbox account behind your app isn't US-based, so it can't receive USD. Sandbox Accounts → **Create account** → **Business**, Country **United States**. Then Apps & Credentials (Sandbox) → **Create App** → Merchant → pick that new US Business account. Put the new Client ID / Secret in `.env` (`PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`), restart the app, buy again in a private window. Use this new Business login in Step 7 |
| PayPal says *"This seller doesn't accept payments in your currency"* **after you log in** | your buyer sandbox account is not US-based (e.g. India/INR) but the store charges USD. Sandbox Accounts → **Create account** → **Personal**, Country **United States** → buy again (in a private window) with that new account. If it still fails, the **Business** account behind your app is non-US: create a US Business account, create a new app linked to it (Apps & Credentials → Create App), put its Client ID/Secret in `.env`, restart |
| PayPal shows a form asking for **phone, name, card number** | that's guest card checkout: you're not logged in. Don't fill it; look for **Log In** at the top of the page (or go back and click *Buy with PayPal* again) and log in with the **Personal** sandbox account. Never enter a real card |
| Store says "Something went wrong with checkout" after paying | look at the PowerShell window. The order shows `CAPTURE_FAILED` in the Orders table. Just buy again |
| Watcher says *"checked 0 order(s)"* | no **paid + guaranteed** order exists. Did you finish Pay Now? Was the promise active **before** you bought? |
| Refund row shows **FAILED** | read the error in the *Customer message* column. Common cause: refunding a payment that's still pending in the sandbox, so wait a minute and buy again |

### Start completely fresh (optional)

1. Stop the app (Ctrl + C).
2. Rename `priceback.db` to `priceback-old.db` (don't delete it, just in case).
3. Start the app again (Step 1). A new empty database is created, and you start from Step 2 (orders begin at #1).

> ⚠ Because a Channel3 key is set in `.env`, a fresh database will try to load **live Channel3 products** instead of
> the 6 demo products (it falls back to the demo products if Channel3 doesn't answer), so product names and prices
> may differ from this guide. The logic and the steps are the same:
> just use whatever product appears and do the maths with its price.
