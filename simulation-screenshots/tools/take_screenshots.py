"""Drive the running app in Chrome, step by step, screenshot each step and write README.md.

    python take_screenshots.py <project_root> <base_url> <server_log>
(needs `pip install playwright`; uses your installed Chrome, no browser download)
"""
from __future__ import annotations

import html
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT, BASE, LOG = Path(sys.argv[1]), sys.argv[2].rstrip("/"), Path(sys.argv[3])
OUT = ROOT / "simulation-screenshots"
TOKEN = next(l.split("=", 1)[1].strip().strip('"').strip("'")  # the app ignores surrounding quotes
             for l in (ROOT / ".env").read_text(encoding="utf-8").splitlines() if l.startswith("ADMIN_TOKEN="))
PROMISE_100 = "Refund the difference if the price drops within 14 days, up to $20 per order and $100 per day."
PROMISE_25 = "Refund the difference if the price drops within 14 days, up to $20 per order and $25 per day."

steps: list[dict] = []
prompts = [0]
alerts: list[str] = []

CAP_JS = """(t) => { const d = document.createElement('div'); d.id = '__cap'; d.textContent = t;
  d.style.cssText = 'background:#111827;color:#fff;font:600 17px system-ui,Segoe UI,sans-serif;padding:10px 18px;' +
                    'border-bottom:4px solid #f43f5e;position:relative;z-index:99999';
  document.body.prepend(d); }"""
HL_JS = "(e) => { e.setAttribute('data-hl','1'); e.style.outline = '4px solid #f43f5e'; e.style.outlineOffset = '3px'; }"
CLEAR_JS = """() => { document.getElementById('__cap')?.remove();
  document.querySelectorAll('[data-hl]').forEach(e => { e.style.outline = ''; e.removeAttribute('data-hl'); }); }"""


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:48]


def shot(page, part, title, *, how, data="nothing typed", see, behind="", hl=(), full=True):
    n = len(steps) + 1
    fn = f"{n:02d}-{slug(title)}.png"
    for loc in hl:
        loc.evaluate(HL_JS)
    page.evaluate(CAP_JS, f"Step {n:02d} · {part} · {title}")
    page.wait_for_timeout(150)
    page.screenshot(path=str(OUT / fn), full_page=full)
    page.evaluate(CLEAR_JS)
    steps.append(dict(n=n, file=fn, part=part, title=title, how=how, data=data, see=see, behind=behind))
    print(f"  [{n:02d}] {title}", flush=True)


def terminal(ctx, part, title, text, **kw):
    p = ctx.new_page()
    p.set_viewport_size({"width": 1100, "height": 400})
    p.set_content("<body style='margin:0;background:#0c0c0c'><pre style='margin:0;padding:22px;color:#d4d4d4;"
                  "font:15px/1.5 Consolas,monospace;white-space:pre-wrap'>" + html.escape(text) + "</pre></body>")
    shot(p, part, title, **kw)
    p.close()


def msg_after(page, selector) -> str:
    page.evaluate("() => document.querySelector('#msg').replaceChildren()")
    page.click(selector)
    page.wait_for_function("() => document.querySelector('#msg').textContent.trim().length > 0", timeout=90000)
    page.wait_for_timeout(1500)  # the page refreshes its tables right after showing the message
    return page.inner_text("#msg").strip()


BANNER_JS = """() => { const b = document.querySelector('#banner'), p = b.querySelector('.pill');
  return [...b.childNodes].filter(n => n !== p).map(n => n.textContent).join('').trim() + (p ? ' (' + p.textContent + ')' : ''); }"""
WIDE_CSS = ".wrap{max-width:1640px !important} td.wrapcell{min-width:340px}"


def banner_text(store) -> str:
    return store.evaluate(BANNER_JS)


def widen(page):
    page.add_style_tag(content=WIDE_CSS)


def first_row(page, table) -> str:
    return " | ".join(page.locator(f"{table} tbody tr").first.locator("td").all_inner_texts())


def rows(page, table) -> int:
    return page.locator(f"{table} tbody tr").count()


def tiles(page) -> str:
    out = []
    for t in page.locator("#tiles .tile").all():
        out.append(f"{t.locator('.k').inner_text()}: **{t.locator('.v').inner_text()}**")
    return " · ".join(out)


def compile_promise(m, text):
    m.fill("#promise", text)
    m.click("#compile")
    m.wait_for_selector("#activate", timeout=90000)
    vals = {k: m.input_value(f"#c-{k}") for k in ("window", "cap", "daily", "min")}
    src = m.locator("#compiled .badge").first.inner_text()
    return vals, src


def buy(store, title, *, shots: bool, part: str, extra_how=""):
    store.goto(BASE + "/")
    store.wait_for_selector(".card button")
    btn = store.locator(".card", has_text=title).locator("button")
    price = store.locator(".card", has_text=title).locator(".price").inner_text()
    if shots:
        shot(store, part, f"Click Buy with PayPal on {title}",
             how=f"In the store tab, find the **{title} — {price}** card and click **Buy with PayPal** (red box).",
             see="The store still shows the green guarantee banner, so this purchase will be covered.",
             behind="Clicking calls `POST /api/checkout` with only the product id. The server looks the price up in its own "
                    "catalog and asks PayPal to create an order for that amount, so the browser can never change the price.",
             hl=[btn], full=False)
    btn.click()
    store.wait_for_url("**/pretend-paypal/checkout/**", timeout=30000)
    if shots:
        shot(store, part, "PayPal checkout page (pretend)",
             how="The browser was sent to the PayPal approval page returned by `POST /api/checkout`.",
             see=f"The amount is **{price} USD** for *{title}*, with a **Pay Now** button.",
             behind="**With the real PayPal sandbox this is where you log in:** *Email or mobile number* = your "
                    "**Personal (US)** sandbox email (`sb-…@personal.example.com`) → **Next** → *Password* = its password → "
                    "**Log In** → **Pay Now**. Here the pretend PayPal skips the login (pretend buyer already logged in).",
             hl=[store.locator("#paynow")], full=False)
    store.click("#paynow")
    store.wait_for_url(re.compile(r".*/\?paid=\d+"), timeout=30000)
    store.wait_for_selector("#flash .flash")
    flash = store.inner_text("#flash").strip()
    order_no = re.search(r"order #(\d+)", flash).group(1)
    shot(store, part, f"Back on the store: payment received (order #{order_no})",
         how=("Clicked **Pay Now**. PayPal sent the browser back to `/checkout/return?token=…`, "
              "and the server redirected to `/?paid=" + order_no + "`." + extra_how),
         see=f"Green message: *{flash}*",
         behind="On return the server **captures** the payment (takes the money), checks PayPal captured exactly the "
                "catalog price, and saves a **copy of the active promise onto the order** (snapshot). If the merchant changes "
                "the promise later, this order keeps the terms it was bought under.",
         full=False)
    return order_no, price


def main():
    OUT.mkdir(exist_ok=True)
    for old in OUT.glob("*.png"):
        old.unlink()
    for _ in range(120):
        try:
            urllib.request.urlopen(BASE + "/api/guarantee", timeout=2)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.5)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        ctx = browser.new_context(viewport={"width": 1680, "height": 900}, color_scheme="light")

        # ---- 01 server ------------------------------------------------------------------------------
        print("Part 0", flush=True)
        log = LOG.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        terminal(ctx, "Setup", "Start the app",
                 "PS C:\\...\\PriceBack> .\\.venv\\Scripts\\python simulation-screenshots\\tools\\run_with_pretend_paypal.py run.db 8010\n"
                 + "\n".join(log[:6]),
                 how="Opened PowerShell in the project folder and started the app. (Your normal command is "
                     "`.\\.venv\\Scripts\\python -m uvicorn app.main:app --reload`. This run used a small launcher that starts the "
                     "**same app**, but with a fresh database and the pretend PayPal; see *How this run was made*.)",
                 see="`Application startup complete.` The app is listening on port 8010.",
                 behind="FastAPI starts, creates an empty SQLite database and loads the 6 demo products.",
                 full=False)

        # ---- merchant: login + promise ----------------------------------------------------------------
        print("Part A", flush=True)
        m = ctx.new_page()

        def on_dialog(d):
            if d.type == "prompt":
                prompts[0] += 1
                d.accept(TOKEN)
            else:
                alerts.append(d.message)
                d.accept()
        m.on("dialog", on_dialog)
        m.goto(BASE + "/merchant")
        m.wait_for_function("() => document.querySelector('#active').textContent.length > 0", timeout=30000)
        widen(m)
        m.wait_for_timeout(800)
        shot(m, "Part A · Merchant", "Open the merchant dashboard and log in",
             how=f"Opened **{BASE}/merchant**. The browser showed a small **\"Admin token\"** box "
                 f"**{prompts[0]} time(s)**. Each time we pasted the value after `ADMIN_TOKEN=` from `.env` and clicked **OK** "
                 "(the box is a browser pop-up, so it doesn't appear in screenshots).",
             data="Admin token from `.env` (not shown here, it's a password)",
             see=f"Next to the Compile button: *{m.inner_text('#active')}*. All tiles are 0 and every table says *Nothing yet.*",
             behind="The merchant API refuses requests without the token (HTTP 401). The page asks for it once, then keeps it "
                    "for this tab.")

        m.fill("#promise", PROMISE_100)
        shot(m, "Part A · Merchant", "Type the promise in plain English",
             how="Clicked into the **1 · Your promise** box and typed the sentence below (red box).",
             data=f"`{PROMISE_100}`",
             see="The sentence is in the box; nothing has happened yet.",
             hl=[m.locator("#promise")], full=False)

        vals, src = compile_promise(m, PROMISE_100)
        shot(m, "Part A · Merchant", "Compile the promise into numbers",
             how="Clicked **Compile promise**.",
             data="(the sentence from the previous step)",
             see=f"Four editable boxes: Window (days) **{vals['window']}**, Max refund / order ($) **{vals['cap']}**, "
                 f"Daily budget ($) **{vals['daily']}**, Min drop (%) **{vals['min'] or '(empty = 0)'}**, and the badge "
                 f"**{src}** (*llm* = Gemini read it, *rules* = the backup regex parser read it).",
             behind="The AI only turns the sentence into these 4 numbers; they are validated against hard limits "
                    "(window 1–90 days, cap up to $1,000). **The AI never touches money.** You review the numbers next.",
             hl=[m.locator("#compiled")], full=False)

        act_msg = msg_after(m, "#activate")
        shot(m, "Part A · Merchant", "Activate the promise",
             how="Checked the numbers match the sentence and clicked **Review OK → Activate** (no changes).",
             see=f"Green message: *{act_msg}* and the label now reads *{m.inner_text('#active')}*.",
             behind="The validated policy is saved as the active promise. Only orders placed **from now on** are covered.",
             full=False)

        # ---- shopper: buy headphones -----------------------------------------------------------------
        print("Part B", flush=True)
        store = ctx.new_page()
        store.on("dialog", lambda d: (alerts.append(d.message), d.accept()))
        store.goto(BASE + "/")
        store.wait_for_selector(".card")
        shot(store, "Part B · Shopper", "Open the store",
             how=f"Opened a new tab at **{BASE}/**.",
             see=f"Banner: *{banner_text(store)}* and 6 products with **Buy with PayPal** buttons.",
             behind="The banner comes from `GET /api/guarantee`, the promise you just activated.",
             hl=[store.locator("#banner")], full=False)
        order1, price1 = buy(store, "Wireless Over-Ear Headphones", shots=True, part="Part B · Shopper")

        m.reload()
        m.wait_for_function("() => document.querySelector('#active').textContent.length > 0")
        widen(m)
        m.wait_for_timeout(1000)
        shot(m, "Part B · Merchant", "The paid order on the dashboard",
             how="Switched back to the merchant tab and pressed **F5**.",
             see=f"Orders table: *{first_row(m, '#t-orders')}*. Tiles: {tiles(m)}.",
             behind="*Open exposure* is the most PriceBack could still owe: the $20 per-order cap.",
             hl=[m.locator("#t-orders")])

        # ---- price drop -> refund ----------------------------------------------------------------------
        print("Part C", flush=True)
        r = msg_after(m, "#reset")
        shot(m, "Part C · Price drop", "Reset any old simulated prices",
             how="In **2 · Demo controls**, clicked **Reset simulation**.",
             see=f"Message: *{r}*", behind="Removes any fake prices so we start from the real catalog prices.",
             hl=[m.locator("#reset")], full=False)

        m.select_option("#product", "demo-headphones")
        m.fill("#pct", "20")
        shot(m, "Part C · Price drop", "Choose the product and the drop",
             how="Picked the product in the **Product** dropdown and typed the percentage in **Drop %** (red boxes).",
             data="Product = **Wireless Over-Ear Headphones · $129.00**, Drop % = **20**",
             see="The form is filled; nothing has happened yet.",
             hl=[m.locator("#product"), m.locator("#pct")], full=False)
        r = msg_after(m, "#drop")
        shot(m, "Part C · Price drop", "Simulate the price drop",
             how="Clicked **Simulate price drop**.",
             see=f"Message: *{r}*",
             behind="Real prices don't move during a demo, so a **simulated** market price is stored separately and "
                    "everything it causes is labelled `simulated`.",
             hl=[m.locator("#drop")], full=False)

        r = msg_after(m, "#run")
        shot(m, "Part C · Price drop", "Run the watcher: automatic refund",
             how="Clicked **Run watcher now**.",
             see=f"Message: *{r}*  \nNew audit row: *{first_row(m, '#t-settle')}*  \nTiles: {tiles(m)}",
             behind="Paid $129.00, market $103.20, so $25.80 is owed. The per-order cap left is $20.00 and the daily budget "
                    "left is $100.00. **Refund = the smallest = $20.00** (`capped_by_order_cap`). The decision is written to the "
                    "database first (locked), then the refund is sent to PayPal with a unique `PayPal-Request-Id` so a retry "
                    "can't pay twice. Gemini (or a template) writes the customer message.",
             hl=[m.locator("#t-settle")])

        a = ctx.new_page()
        a.goto(BASE + "/pretend-paypal/activity")
        shot(a, "Part C · Check PayPal", "The refund arrived at PayPal (pretend activity page)",
             how=f"Opened **{BASE}/pretend-paypal/activity**, which stands in for the guide's Step 7.",
             see="Payments received: the $129.00 capture. Refunds sent: one refund of $20.00 with its PayPal-Request-Id.",
             behind="**With the real sandbox:** open a private window → https://www.sandbox.paypal.com → log in with the "
                    "**Business (US)** sandbox account → **Activity**. You'd see the $129.00 payment and the $20.00 refund.",
             full=False)

        # ---- guardrail ----------------------------------------------------------------------------------
        print("Part D", flush=True)
        r = msg_after(m, "#run")
        shot(m, "Part D · Guardrail", "Run again: blocked by the per-order cap",
             how="Changed nothing and clicked **Run watcher now** again.",
             see=f"Message: *{r}*  \nNew audit row: *{first_row(m, '#t-settle')}*",
             behind="$5.80 is technically still owed ($25.80 − $20.00), but this order already used its whole $20 cap. "
                    "The cap is **cumulative** across refunds on the same order.",
             hl=[m.locator("#t-settle")])
        before = rows(m, "#t-settle")
        r = msg_after(m, "#run")
        after = rows(m, "#t-settle")
        shot(m, "Part D · Guardrail", "Run a third time: no duplicate log rows",
             how="Clicked **Run watcher now** a third time.",
             see=f"Message: *{r}*. Audit rows before: **{before}**, after: **{after}**, so no new row was added.",
             behind="The same block for the same price is logged once, not every time the watcher runs.",
             hl=[m.locator("#t-settle")])

        # ---- scenario A + B: mouse ------------------------------------------------------------------------
        print("Scenarios", flush=True)
        order2, price2 = buy(store, "Ergonomic Wireless Mouse", shots=False, part="Scenario A · Shopper",
                             extra_how=" (Same steps as before: Buy with PayPal on the mouse → Pay Now.)")
        m.bring_to_front()
        msg_after(m, "#reset")
        m.select_option("#product", "demo-mouse")
        m.fill("#pct", "10")
        r = msg_after(m, "#drop")
        shot(m, "Scenario A · Small drop", "Mouse drops 10%",
             how="Merchant tab: **Reset simulation**, then Product = mouse, Drop % = 10, then **Simulate price drop**.",
             data="Product = **Ergonomic Wireless Mouse · $45.00**, Drop % = **10**",
             see=f"Message: *{r}*",
             hl=[m.locator("#product"), m.locator("#pct")], full=False)
        r = msg_after(m, "#run")
        shot(m, "Scenario A · Small drop", "Full difference refunded",
             how="Clicked **Run watcher now**.",
             see=f"Message: *{r}*  \nNew audit row: *{first_row(m, '#t-settle')}*",
             behind="$45.00 − $40.50 = $4.50 owed, which is below the $20 cap, so it's refunded in full (`full_difference`). "
                    "The headphones order was checked too, but its price is back to normal after the reset, so nothing happened.",
             hl=[m.locator("#t-settle")])

        m.fill("#pct", "30")
        msg_after(m, "#drop")
        r = msg_after(m, "#run")
        shot(m, "Scenario B · Further drop", "Price falls further: only the extra is refunded",
             how="Same product, Drop % = **30**, then **Simulate price drop**, then **Run watcher now**.",
             data="Product = **Ergonomic Wireless Mouse**, Drop % = **30** (market $31.50)",
             see=f"Message: *{r}*  \nNew audit row: *{first_row(m, '#t-settle')}*  \nTiles: {tiles(m)}",
             behind="Now owed: $45.00 − $31.50 = $13.50, minus the $4.50 already refunded = **$9.00**.",
             hl=[m.locator("#t-settle")])

        before = rows(m, "#t-settle")
        msg_after(m, "#reset")
        r = msg_after(m, "#run")
        shot(m, "Scenario C · No drop", "Prices back to normal: nothing happens",
             how="Clicked **Reset simulation**, then **Run watcher now**.",
             see=f"Message: *{r}*. Audit rows before: **{before}**, after: **{rows(m, '#t-settle')}**.",
             behind="No price drop means `no_drop`, so it's skipped silently.")

        # ---- scenario D: daily budget ---------------------------------------------------------------------
        vals, src = compile_promise(m, PROMISE_25)
        shot(m, "Scenario D · Daily budget", "Compile a promise with a $25 daily budget",
             how="Changed the promise text (only `$100` became `$25`) and clicked **Compile promise**.",
             data=f"`{PROMISE_25}`",
             see=f"Daily budget ($) now **{vals['daily']}** (window {vals['window']}, cap {vals['cap']}), badge **{src}**.",
             hl=[m.locator("#compiled")], full=False)
        r = msg_after(m, "#activate")
        m.select_option("#product", "demo-mouse")
        m.fill("#pct", "50")
        msg_after(m, "#drop")
        r = msg_after(m, "#run")
        shot(m, "Scenario D · Daily budget", "Daily budget used up: refund blocked",
             how="Clicked **Review OK → Activate**, then Product = mouse, Drop % = **50**, then **Simulate price drop**, "
                 "then **Run watcher now**.",
             data="Promise with **$25 per day**; mouse market price $22.50",
             see=f"Message: *{r}*  \nNew audit row: *{first_row(m, '#t-settle')}*",
             behind="The mouse would be owed $9.00 more, but **$33.50 was already refunded today**, more than the new $25 daily "
                    "budget. The daily budget is shared by all orders and follows the currently active promise.",
             hl=[m.locator("#t-settle")])

        compile_promise(m, PROMISE_100)
        r1 = msg_after(m, "#activate")
        r2 = msg_after(m, "#reset")
        shot(m, "Scenario D · Daily budget", "Put the $100 promise back",
             how="Typed the original sentence, then **Compile promise**, then **Review OK → Activate**, then **Reset simulation**.",
             data=f"`{PROMISE_100}`",
             see=f"*{m.inner_text('#active')}*. Final tiles: {tiles(m)}.",
             behind="Everything that happened is in the audit trail below (newest first).")

        a.reload()
        shot(a, "Wrap-up", "Everything PayPal saw (pretend activity page)",
             how=f"Reloaded **{BASE}/pretend-paypal/activity**.",
             see="2 payments ($129.00, $45.00) and 3 refunds ($20.00, $4.50, $9.00). The blocked attempts never reached PayPal.",
             behind="Total refunded $33.50 against $174.00 of guaranteed sales, every cent inside the promise's limits.",
             full=False)

        # ---- tests ----------------------------------------------------------------------------------------
        print("Tests", flush=True)
        t = subprocess.run([str(ROOT / ".venv/Scripts/python.exe"), "-m", "pytest", "-q", "-p", "no:warnings"],
                           cwd=ROOT, capture_output=True, text=True)
        terminal(ctx, "Tests", "Run the automated tests",
                 "PS C:\\...\\PriceBack> .\\.venv\\Scripts\\python -m pytest -q\n" + (t.stdout + t.stderr).strip(),
                 how="In a second PowerShell window (project folder) ran `.\\.venv\\Scripts\\python -m pytest -q`.",
                 see=f"`{(t.stdout.strip().splitlines() or ['?'])[-1]}`",
                 behind="39 tests cover promise parsing, every guardrail, no double refunds, the full checkout → drop → "
                        "refund flow, admin protection and webhook checks, all against a fake PayPal, so no internet is needed.",
                 full=False)
        browser.close()

    write_readme()


def write_readme():
    L = ["# PriceBack: a full simulation run, step by step", "",
         f"Recorded {datetime.now():%d %b %Y, %H:%M} by driving the app in Chrome and taking a screenshot after every step. "
         "Each screenshot has a dark caption bar on top, and a **red box** marks what was clicked or filled in.", "",
         "## How this run was made (read this first)", "",
         "- **The real PriceBack app** ran unchanged: same pages, same promise compiler (Gemini), same watcher, same refund "
         "rules, same database code.",
         "- **PayPal was replaced by a pretend PayPal** for the three money calls (create order, capture, refund). Your "
         "real sandbox currently refuses checkout with `UNSUPPORTED_PAYEE_CURRENCY`, because the seller account behind your "
         "PayPal app isn't US-based. The pretend PayPal behaves like the real one, including *same PayPal-Request-Id = same "
         "refund*. Its pages carry a yellow **PRETEND PAYPAL** banner. Where the real sandbox would show login screens, the "
         "step says exactly what you'd type.",
         "- A **separate, fresh database** was used, so your own `priceback.db` was not touched. The **6 demo products** were "
         "used, so the numbers match `SIMULATION-GUIDE.md`. Order numbers start at #1 here.",
         f"- The admin-token pop-up appeared **{prompts[0]} time(s)** when the dashboard first opened.",
         "- Screenshots were taken on a wide page so the whole audit table fits. On a normal screen the *Customer "
         "message* column may be off to the right: scroll the table sideways.",
         "- To repeat this run yourself: see `tools/` (`run_with_pretend_paypal.py` starts the app, `take_screenshots.py` "
         "clicks through and writes this file).", "",
         "## Steps", ""]
    for s in steps:
        L += [f"- [{s['n']:02d} · {s['title']}](#{s['n']:02d})"]
    L += [""]
    part = None
    for s in steps:
        if s["part"] != part:
            part = s["part"]
            L += [f"---", "", f"### {part}", ""]
        L += [f'<a id="{s["n"]:02d}"></a>', f"#### {s['n']:02d} · {s['title']}", "",
              f"![{s['title']}]({s['file']})", "",
              f"**How we got here:** {s['how']}", "",
              f"**Data entered:** {s['data']}", "",
              f"**What you see:** {s['see']}", ""]
        if s["behind"]:
            L += [f"**Behind the scenes:** {s['behind']}", ""]
    if alerts:
        L += ["---", "", "**Browser alerts during the run:** " + "; ".join(alerts), ""]
    (OUT / "README.md").write_text("\n".join(L), encoding="utf-8")
    print(f"README written, {len(steps)} steps", flush=True)


if __name__ == "__main__":
    main()
