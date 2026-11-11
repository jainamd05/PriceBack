/* PriceBack shared helpers. Every string goes into the DOM as text, never as HTML. */
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];

function h(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(e.style, v);
    else if (k.startsWith('on') && typeof v === 'function') e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false) e.append(k instanceof Node ? k : String(k));
  return e;
}
const SVGNS = 'http://www.w3.org/2000/svg';
function s(tag, attrs = {}, ...kids) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined) e.setAttribute(k, v);
  for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false) e.append(k instanceof Node ? k : String(k));
  return e;
}

const money = (c, cur = 'USD') => new Intl.NumberFormat('en-US', {style: 'currency', currency: cur}).format((c || 0) / 100);
const dollars = (d) => money(Math.round((d || 0) * 100));
const when = (t) => t ? new Date(t * 1000).toLocaleString([], {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'}) : '';
const ago = (t) => {
  if (!t) return '';
  const d = Math.max(0, Date.now() / 1000 - t);
  if (d < 60) return 'just now';
  if (d < 3600) return `${Math.floor(d / 60)} min ago`;
  if (d < 86400) return `${Math.floor(d / 3600)} h ago`;
  return `${Math.floor(d / 86400)} d ago`;
};
const human = (x) => String(x || '').replace(/_/g, ' ');
const initial = (t) => (String(t || '').trim()[0] || '·').toUpperCase();

/* Free-licence photos from Unsplash (https://unsplash.com/license), hot-linked from their CDN. */
const PHOTO = {
  saleTags: 'https://images.unsplash.com/photo-1775740396807-6fed2cedad0b',     // red SALE tags on a clothing rail
  saleTags2: 'https://images.unsplash.com/photo-1775740396883-2bfe9a9b5768',
  shopper: 'https://images.unsplash.com/photo-1483985988355-763728e1935b',      // woman carrying shopping bags
  bags: 'https://images.unsplash.com/photo-1513884923967-4b182ef167ab',         // paper shopping bags
  tags: 'https://images.unsplash.com/photo-1571907483086-3c0ea40cc16d',         // blank paper price tags
  terminal: 'https://images.unsplash.com/photo-1634733988138-bf2c3a2a13fa',     // card terminal printing a receipt
  pos: 'https://images.unsplash.com/photo-1647427017067-8f33ccbae493',          // checkout counter
};
const photo = (key, w = 1600) => `${PHOTO[key]}?w=${w}&q=72&auto=format&fit=crop`;

const BRAND_SVG = '<svg viewBox="0 0 32 32" aria-hidden="true"><path d="M3 5.5A2.5 2.5 0 0 1 5.5 3H17l12 12a2.4 2.4 0 0 1 0 3.4L18.4 29a2.4 2.4 0 0 1-3.4 0L3 17z" fill="var(--accent)"/><circle cx="9.5" cy="9.5" r="2.3" fill="var(--paper)"/><path d="M16.5 12.5v8.5m-3.4-3.4 3.4 3.4 3.4-3.4" stroke="var(--paper)" stroke-width="2.4" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>';
function brandMark() { const t = document.createElement('template'); t.innerHTML = BRAND_SVG; return t.content.firstChild; } // static, trusted markup

function toast(text, kind = 'ok') {
  let box = $('#toasts');
  if (!box) { box = h('div', {class: 'toasts', id: 'toasts', 'aria-live': 'polite'}); document.body.append(box); }
  const t = h('div', {class: 'toast ' + kind, role: kind === 'err' ? 'alert' : 'status'},
    h('div', {style: 'flex:1'}, text), h('button', {type: 'button', 'aria-label': 'Dismiss', onclick: () => t.remove()}, '×'));
  box.append(t);
  setTimeout(() => t.remove(), kind === 'err' ? 12000 : 6000);
}

function revealOnScroll(root = document) {
  const els = $$('.reveal:not(.in)', root);
  // anything already on screen shows immediately; only content further down fades in
  for (const e of els) { const b = e.getBoundingClientRect(); if (b.top < innerHeight && b.bottom > 0) e.classList.add('in'); }
  if (!('IntersectionObserver' in window)) { els.forEach((e) => e.classList.add('in')); return; }
  const io = new IntersectionObserver((entries) => entries.forEach((en) => {
    if (en.isIntersecting) { en.target.classList.add('in'); io.unobserve(en.target); }
  }), {threshold: 0.12, rootMargin: '0px 0px -40px 0px'});
  els.forEach((e) => io.observe(e));
}

/* Image with a typographic fallback if it is missing or fails to load. */
function productArt(p, cls = 'art') {
  const fallback = () => h('div', {class: cls + ' mono-art', 'aria-hidden': 'true'}, h('span', {}, initial(p.title)));
  if (!(p.image_url && p.image_url.startsWith('https://'))) return fallback();
  const box = h('div', {class: cls});
  const img = h('img', {src: p.image_url, alt: p.title, loading: 'lazy', referrerpolicy: 'no-referrer'});
  img.addEventListener('error', () => box.replaceWith(fallback()));
  box.append(img);
  return box;
}

/* The guarantee maths, mirrored from app/settle.py evaluate() for a single order (no daily budget). */
function refundFor(paidCents, marketCents, g) {
  if (!g || !g.active) return {amount: 0, why: 'no guarantee'};
  const drop = paidCents - marketCents;
  if (drop <= 0) return {amount: 0, why: 'no drop'};
  if (g.min_drop_pct && drop * 100 < g.min_drop_pct * paidCents) return {amount: 0, why: `drop under ${g.min_drop_pct}%`};
  const cap = Math.round(g.max_refund_per_order * 100);
  return drop > cap ? {amount: cap, why: 'capped'} : {amount: drop, why: 'full difference'};
}
