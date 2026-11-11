/* Storefront behaviour shared by the home page and product pages. Needs pb.js. */

function initNav() {
  const brand = $('#brand');
  if (brand && !brand.querySelector('svg')) brand.prepend(brandMark());
  const nav = $('#nav');
  if (!nav || !nav.classList.contains('over')) return;
  const onScroll = () => nav.classList.toggle('scrolled', window.scrollY > 40);
  onScroll();
  window.addEventListener('scroll', onScroll, {passive: true});
}

function priceEl(cents, cur = 'USD', cls = 'price') {
  const [whole, frac] = money(cents, cur).split('.');
  return h('div', {class: cls}, whole, frac ? h('small', {}, '.' + frac) : null);
}

async function checkout(productId, btn) {
  const label = [...btn.childNodes].map((n) => n.cloneNode(true));
  btn.disabled = true;
  btn.classList.add('busy');
  btn.textContent = 'Opening PayPal…';
  try {
    const r = await fetch('/api/checkout', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({product_id: productId})});
    const j = await r.json().catch(() => ({}));
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
  const q = new URLSearchParams(location.search);
  let view = null;
  if (q.get('paid')) view = ['ok', '✓', 'Payment received', `Order #${Number(q.get('paid')) || '?'} is paid. If a price-drop guarantee was active, it is now covered, and any refund goes straight back to your PayPal account.`];
  else if (q.get('pending')) view = ['wait', '…', 'Payment on hold', `Order #${Number(q.get('pending')) || '?'}: PayPal is still processing this payment (for example a bank transfer, which can take days). The guarantee only covers completed payments, so this order is not covered. A card or PayPal balance completes instantly.`];
  else if (q.get('cancelled')) view = ['wait', '–', 'Checkout cancelled', 'You have not been charged.'];
  else if (q.get('error')) view = ['err', '!', 'Checkout did not complete', 'You have not been charged twice. Please try again.'];
  if (!view) return;
  history.replaceState(null, '', location.pathname);   // don't show it again on reload
  const [kind, icon, title, text] = view;
  const close = () => m.remove();
  const m = h('div', {class: 'modal', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'mt', onclick: (e) => { if (e.target === m) close(); }},
    h('div', {class: 'receipt', style: 'text-align:center'},
      h('div', {class: 'stamp ' + kind}, icon),
      h('h2', {id: 'mt'}, title),
      h('p', {}, text),
      h('div', {class: 'rule'}),
      h('div', {class: 'line'}, h('span', {}, 'PriceBack'), h('span', {}, new Date().toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'}))),
      h('button', {type: 'button', onclick: close}, 'Continue shopping')));
  document.addEventListener('keydown', function esc(e) { if (e.key === 'Escape') { close(); document.removeEventListener('keydown', esc); } });
  document.body.append(m);
  m.querySelector('button').focus();
}
