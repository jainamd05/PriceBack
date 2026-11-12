/* Storefront behaviour shared by the home page and product pages. Needs pb.js. */

// Read once at load: the shop's URL-synced filters rewrite the query string before checkoutResult() runs.
const BOOT_QUERY = new URLSearchParams(location.search);

function initNav() {
  const brand = $('#brand');
  if (brand && !brand.querySelector('svg')) brand.prepend(brandMark());
  const links = $('#nav .nav-links');
  if (links && !$('#acct')) links.append(h('span', {id: 'acct', class: 'acct'}));
  loadMe();
  demoBadge();
  const nav = $('#nav');
  if (!nav || !nav.classList.contains('over')) return;
  const onScroll = () => nav.classList.toggle('scrolled', window.scrollY > 40);
  onScroll();
  window.addEventListener('scroll', onScroll, {passive: true});
}

/* Always-visible note that this is a simulation prototype: sandbox money, no real orders or data. */
function demoBadge() {
  if ($('#demo-badge')) return;
  let open = true;
  try { open = sessionStorage.getItem('pb_demo_badge') !== 'min'; } catch (e) {}
  const text = h('span', {class: 'demo-badge-text'}, 'This is a simulation prototype of PriceBack. Payments use PayPal’s sandbox with test money: nothing here touches your real money, cards or data.');
  const badge = h('aside', {id: 'demo-badge', class: 'demo-badge' + (open ? '' : ' min'), 'aria-label': 'Prototype notice'},
    h('b', {}, 'Prototype · simulation only'), text,
    h('button', {type: 'button', class: 'demo-badge-t', 'aria-expanded': String(open), onclick: () => {
      open = !open; badge.classList.toggle('min', !open); badge.lastChild.setAttribute('aria-expanded', String(open));
      badge.lastChild.textContent = open ? 'Hide' : 'What is this?';
      try { sessionStorage.setItem('pb_demo_badge', open ? 'open' : 'min'); } catch (e) {}
    }}, open ? 'Hide' : 'What is this?'));
  document.body.append(badge);
}

function priceEl(cents, cur = 'USD', cls = 'price') {
  const [whole, frac] = money(cents, cur).split('.');
  return h('div', {class: cls}, whole, frac ? h('small', {}, '.' + frac) : null);
}

/* ---- shopper account: sign in with an emailed 6-digit code ------------------------------------------ */
const ME = {email: null, loaded: null, demo: null};
async function jfetch(url, opts = {}) {
  const r = await fetch(url, {credentials: 'same-origin', ...opts, headers: {'content-type': 'application/json', ...(opts.headers || {})}});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(typeof j.detail === 'string' ? j.detail : (Array.isArray(j.detail) && j.detail[0]?.msg) || 'Something went wrong.'); e.status = r.status; throw e; }
  return j;
}
function loadMe() {
  if (!ME.loaded) ME.loaded = Promise.all([jfetch('/api/me').catch(() => ({email: null})), jfetch('/api/demo').catch(() => ({}))])
    .then(([me, demo]) => { ME.email = me.email; ME.demo = demo; renderAccountNav(); return ME; });
  return ME.loaded;
}
function renderAccountNav() {
  const slot = $('#acct');
  if (!slot) return;
  slot.replaceChildren(ME.email
    ? h('a', {href: '/account', class: 'keep acct-link', title: ME.email}, h('span', {class: 'acct-dot'}, initial(ME.email)), 'My orders')
    : h('button', {type: 'button', class: 'ghost small acct-btn', onclick: () => signIn().catch(() => {})}, 'Sign in'));
}
async function signOut() { await jfetch('/api/auth/logout', {method: 'POST'}); ME.email = null; renderAccountNav(); }

/* Opens the sign-in sheet. Resolves with the email once signed in; rejects if the shopper closes it. */
function signIn(reason) {
  return new Promise((resolve, reject) => {
    let email = '';
    const close = (ok) => { m.remove(); document.removeEventListener('keydown', esc); ok ? resolve(ME.email) : reject(new Error('closed')); };
    const esc = (e) => { if (e.key === 'Escape') close(false); };
    const body = h('div', {class: 'auth-body'});
    const m = h('div', {class: 'modal', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'auth-h', onmousedown: (e) => { if (e.target === m) close(false); }},
      h('div', {class: 'receipt auth'}, h('button', {type: 'button', class: 'auth-x', 'aria-label': 'Close', onclick: () => close(false)}, '×'), body));
    const err = h('p', {class: 'auth-err', role: 'alert'});
    const stepEmail = () => {
      const input = h('input', {type: 'email', required: true, autocomplete: 'email', placeholder: 'you@example.com', value: email, 'aria-label': 'Your email'});
      const go = h('button', {type: 'submit', class: 'accent'}, 'Email me a code', h('span', {class: 'arrow'}, '→'));
      const form = h('form', {onsubmit: async (e) => {
        e.preventDefault(); err.textContent = ''; go.disabled = true; go.classList.add('busy');
        try { const r = await jfetch('/api/auth/start', {method: 'POST', body: JSON.stringify({email: input.value})}); email = r.email; stepCode(r.dev_code); }
        catch (x) { err.textContent = x.message; go.disabled = false; go.classList.remove('busy'); }
      }}, input, go);
      body.replaceChildren(h('div', {class: 'eyebrow'}, 'PriceBack account'), h('h2', {id: 'auth-h'}, 'Sign in with your email'),
        h('p', {}, reason || 'We email you a one-time code. No password. Order confirmations and price-drop refunds are sent to this address.'),
        h('div', {class: 'rule'}), form, err,
        h('p', {class: 'auth-fine'}, 'Simulation prototype: no real money, orders or data. Your email is only used for this demo’s order and refund messages.'));
      setTimeout(() => input.focus(), 60);
    };
    const stepCode = (devCode) => {
      const input = h('input', {inputmode: 'numeric', autocomplete: 'one-time-code', maxlength: 6, pattern: '[0-9]{6}', required: true, placeholder: '••••••', class: 'code-in', 'aria-label': '6-digit code'});
      const go = h('button', {type: 'submit', class: 'accent'}, 'Sign in', h('span', {class: 'arrow'}, '→'));
      const form = h('form', {onsubmit: async (e) => {
        e.preventDefault(); err.textContent = ''; go.disabled = true; go.classList.add('busy');
        try { const r = await jfetch('/api/auth/verify', {method: 'POST', body: JSON.stringify({email, code: input.value})}); ME.email = r.email; renderAccountNav(); toast(`Signed in as ${r.email}.`); close(true); }
        catch (x) { err.textContent = x.message; go.disabled = false; go.classList.remove('busy'); input.select(); }
      }}, input, go);
      input.addEventListener('input', () => { input.value = input.value.replace(/\D/g, '').slice(0, 6); if (input.value.length === 6) form.requestSubmit(); });
      const resend = h('button', {type: 'button', class: 'link', onclick: async () => {
        err.textContent = '';
        try { const r = await jfetch('/api/auth/start', {method: 'POST', body: JSON.stringify({email})}); toast('A new code is on its way.'); if (r.dev_code) stepCode(r.dev_code); }
        catch (x) { err.textContent = x.message; }
      }}, 'Send a new code');
      body.replaceChildren(h('div', {class: 'eyebrow'}, 'Check your inbox'), h('h2', {id: 'auth-h'}, 'Enter the 6-digit code'),
        h('p', {}, 'We sent it to ', h('b', {}, email), '. It works for 10 minutes. Check spam if it isn’t there in a minute.'),
        devCode ? h('p', {class: 'auth-dev'}, `Local mode: no email service is configured, so here is the code: ${devCode}`) : null,
        h('div', {class: 'rule'}), form, err,
        h('div', {class: 'auth-row'}, h('button', {type: 'button', class: 'link', onclick: stepEmail}, 'Use a different email'), resend));
      setTimeout(() => input.focus(), 60);
    };
    stepEmail();
    document.addEventListener('keydown', esc);
    document.body.append(m);
  });
}

/* Before PayPal opens: show the sandbox buyer login testers should pay with (fake money only). */
function payHelp() {
  const d = ME.demo || {};
  if (!d.buyer_email) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const close = (ok) => { m.remove(); ok ? resolve() : reject(new Error('closed')); };
    const copy = (label, value) => h('div', {class: 'line copy'}, h('span', {}, label),
      h('button', {type: 'button', class: 'link', title: 'Copy', onclick: (e) => { navigator.clipboard?.writeText(value); e.currentTarget.textContent = 'copied ✓'; }}, value));
    const m = h('div', {class: 'modal', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'pay-h', onmousedown: (e) => { if (e.target === m) close(false); }},
      h('div', {class: 'receipt auth'},
        h('div', {class: 'eyebrow'}, 'How to pay in this demo'),
        h('h2', {id: 'pay-h'}, 'Use the PayPal sandbox buyer'),
        h('p', {}, 'This is a simulation prototype on PayPal’s sandbox: nothing is really charged, and none of your real money or data is used. On the PayPal page, log in with this test account (click to copy), then pay with its balance or card.'),
        h('div', {class: 'rule'}),
        copy('Email', d.buyer_email), d.buyer_password ? copy('Password', d.buyer_password) : null,
        h('div', {class: 'rule'}),
        h('p', {class: 'auth-fine'}, `Refund emails go to your own address: ${ME.email}.`),
        h('button', {type: 'button', class: 'accent', onclick: () => close(true)}, 'Continue to PayPal', h('span', {class: 'arrow'}, '→')),
        h('button', {type: 'button', class: 'ghost', onclick: () => close(false)}, 'Cancel')));
    document.body.append(m);
    m.querySelector('button.accent').focus();
  });
}

async function checkout(productId, btn) {
  await loadMe();
  try {
    if (!ME.email) await signIn('Sign in so we can email you the order confirmation, and the refund if the price drops.');
    await payHelp();
  } catch { return; }  // closed the sheet: stay on the page
  const label = [...btn.childNodes].map((n) => n.cloneNode(true));
  btn.disabled = true;
  btn.classList.add('busy');
  btn.textContent = 'Opening PayPal…';
  try {
    const r = await fetch('/api/checkout', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({product_id: productId})});
    const j = await r.json().catch(() => ({}));
    if (r.status === 401) { ME.email = null; ME.loaded = null; renderAccountNav(); throw new Error('Your session ended. Sign in again, then buy.'); }
    if (!r.ok) throw new Error(typeof j.detail === 'string' ? j.detail : 'Checkout failed.');
    location.href = j.approve_url;
  } catch (e) {
    toast(e.message, 'err');
    btn.disabled = false;
    btn.classList.remove('busy');
    btn.replaceChildren(...label);
  }
}

/* Receipt lines for "what would I get back" at a given % drop. */
function calcReceipt(p, pct, g) {
  const paid = p.price_cents;
  const now = Math.round(paid * (1 - pct / 100));
  const r = refundFor(paid, now, g);
  const line = (a, b, cls) => h('div', {class: 'line' + (cls ? ' ' + cls : '')}, h('span', {}, a), h('span', {}, b));
  const why = {
    'no drop': 'Move the slider to see a refund.',
    'capped': `The difference is ${money(paid - now)}, but this guarantee pays at most ${dollars(g.max_refund_per_order)} per order.`,
    'full difference': 'The full difference comes back to you.',
  }[r.why] || `No refund: ${r.why}.`;
  return [
    h('div', {class: 'ttl'}, 'PriceBack · refund estimate'),
    h('div', {class: 'rule'}),
    line(p.title, ''),
    line('You pay today', money(paid)),
    line(`Price after a ${pct}% drop`, money(now)),
    line('Difference', pct ? money(paid - now) : '—'),
    h('div', {class: 'rule'}),
    h('div', {class: 'gets'}, h('span', {}, 'PayPal refunds you'), h('b', {}, money(r.amount))),
    h('div', {class: 'why'}, why),
    h('div', {class: 'rule'}),
    line(`Window: ${g.window_days} days`, `Cap: ${dollars(g.max_refund_per_order)}`),
  ];
}

/* After PayPal sends the buyer back: ?paid= / ?pending= / ?cancelled= / ?error= */
function checkoutResult() {
  const q = BOOT_QUERY;
  let view = null;
  if (q.get('paid')) view = ['ok', '✓', 'Payment received', `Order #${Number(q.get('paid')) || '?'} is paid and a confirmation is on its way to your inbox. If a price-drop guarantee was active, it is now covered: any refund goes straight back to your PayPal account and we email you. Try it from My orders.`];
  else if (q.get('pending')) view = ['wait', '…', 'Payment on hold', `Order #${Number(q.get('pending')) || '?'}: PayPal is still processing this payment (for example a bank transfer, which can take days). The guarantee only covers completed payments, so this order is not covered. A card or PayPal balance completes instantly.`];
  else if (q.get('cancelled')) view = ['wait', '–', 'Checkout cancelled', 'You have not been charged.'];
  else if (q.get('error')) view = ['err', '!', 'Checkout did not complete', 'You have not been charged twice. Please try again.'];
  if (!view) return;
  for (const k of ['paid', 'pending', 'cancelled', 'error']) q.delete(k);
  const rest = new URLSearchParams(location.search);
  for (const k of ['paid', 'pending', 'cancelled', 'error']) rest.delete(k);
  history.replaceState(null, '', location.pathname + (rest.toString() ? '?' + rest : '') + location.hash);   // don't show it again on reload
  const [kind, icon, title, text] = view;
  const close = () => m.remove();
  const m = h('div', {class: 'modal', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'mt', onclick: (e) => { if (e.target === m) close(); }},
    h('div', {class: 'receipt', style: 'text-align:center'},
      h('div', {class: 'stamp ' + kind}, icon),
      h('h2', {id: 'mt'}, title),
      h('p', {}, text),
      h('div', {class: 'rule'}),
      h('div', {class: 'line'}, h('span', {}, 'PriceBack'), h('span', {}, new Date().toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'}))),
      kind === 'ok' ? h('a', {class: 'btn accent', href: '/account', style: 'display:flex;justify-content:center;margin-top:18px'}, 'See my order & try a price drop') : null,
      h('button', {type: 'button', class: kind === 'ok' ? 'ghost' : null, onclick: close}, 'Continue shopping')));
  document.addEventListener('keydown', function esc(e) { if (e.key === 'Escape') { close(); document.removeEventListener('keydown', esc); } });
  document.body.append(m);
  m.querySelector('button').focus();
}
