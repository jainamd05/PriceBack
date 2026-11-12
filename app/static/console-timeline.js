/* Coverage timeline for the merchant console, drawn with the Bryntum Scheduler (trial build from the jsDelivr CDN).
   One row per covered order. The bar is the protection window the buyer bought under (capture -> capture + window_days);
   diamonds are the watcher's decisions on that order (refund sent / blocked by a guardrail). A red line marks "now".
   The 4.6 MB bundle is fetched only the first time the Coverage view is opened. */
const PBTimeline = (() => {
  const BASE = 'https://cdn.jsdelivr.net/npm/@bryntum/scheduler-trial@7.3.7/';
  const DAY = 86400000;
  let loading = null, sched = null, lastSig = '', lastSpan = '';

  function load() {
    if (window.bryntum && window.bryntum.scheduler) return Promise.resolve();
    if (!loading) {
      loading = new Promise((resolve, reject) => {
        const dark = matchMedia('(prefers-color-scheme: dark)').matches;
        for (const f of ['scheduler.css', dark ? 'svalbard-dark.css' : 'svalbard-light.css']) document.head.append(h('link', {rel: 'stylesheet', href: BASE + f}));
        const sc = h('script', {src: BASE + 'scheduler.umd.js'});
        sc.onload = resolve;
        sc.onerror = () => { loading = null; sc.remove(); reject(new Error('Could not load the Bryntum Scheduler from the CDN. Check your connection and open Coverage again.')); };
        document.head.append(sc);
      });
    }
    return loading;
  }

  const covered = (st) => st.orders.filter((o) => o.guaranteed && o.captured_at && o.cover_ends_at && o.status === 'CAPTURED');

  function build(st) {
    const now = Date.now();
    const orders = covered(st);
    const refunded = (id) => st.settle.filter((x) => x.order_id === id && x.action === 'refund' && x.status !== 'FAILED').reduce((a, x) => a + x.amount_cents, 0);
    const resources = orders.map((o) => {
      const cap = Math.min(o.max_refund_cents || 0, o.paid_cents);
      return {id: o.id, name: o.title, paid: o.paid_cents, cap, refunded: refunded(o.id), endsAt: o.cover_ends_at * 1000};
    });
    const events = [];
    for (const r of resources) {
      const o = orders.find((x) => x.id === r.id);
      const open = r.endsAt > now;
      const used = r.cap > 0 && r.refunded >= r.cap;
      const left = Math.ceil((r.endsAt - now) / DAY);
      events.push({
        id: `w${r.id}`, resourceId: r.id, kind: 'window',
        startDate: new Date(o.captured_at * 1000), endDate: new Date(r.endsAt),
        name: !open ? 'Window closed' : used ? 'Covered · cap used up' : `Covered · ${left} day${left === 1 ? '' : 's'} left`,
        eventColor: !open ? 'gray' : used ? 'orange' : 'green',
      });
    }
    const ids = new Set(resources.map((r) => r.id));
    for (const x of st.settle) {
      if (!ids.has(x.order_id)) continue;
      const refund = x.action === 'refund';
      events.push({
        id: `s${x.id}`, resourceId: x.order_id, kind: x.action, settlement: x,
        startDate: new Date(x.created_at * 1000), duration: 0, durationUnit: 'day',
        name: refund ? `${money(x.amount_cents)} refunded` : `blocked · ${human(x.reason)}`,
        eventColor: refund ? (x.status === 'FAILED' ? 'red' : 'teal') : 'red',
      });
    }
    // Time axis: a day before the first purchase to two days after the last window closes (at least three weeks).
    const starts = orders.map((o) => o.captured_at * 1000), ends = resources.map((r) => r.endsAt);
    const start = new Date(Math.min(now, ...starts) - DAY); start.setHours(0, 0, 0, 0);
    let end = new Date(Math.max(now, ...ends) + 2 * DAY); end.setHours(0, 0, 0, 0);
    if (end - start < 21 * DAY) end = new Date(start.getTime() + 21 * DAY);
    return {resources, events, start, end};
  }

  function create(el, data) {
    const {Scheduler, StringHelper} = window.bryntum.scheduler;
    const enc = (t) => StringHelper.encodeHtml(String(t ?? ''));
    sched = new Scheduler({
      appendTo: el,
      cls: 'pb-sched',
      height: '100%',
      readOnly: true,
      startDate: data.start,
      endDate: data.end,
      viewPreset: 'weekAndDayLetter',
      rowHeight: 58,
      barMargin: 9,
      columns: [
        {text: 'Covered order', field: 'name', width: 250, htmlEncode: false,
          renderer: ({record}) => ({children: [
            {tag: 'div', className: 'pb-sr-t', text: record.name},
            {tag: 'div', className: 'pb-sr-m', text: `#${record.id} · paid ${money(record.paid)}`},
          ]})},
        {text: 'Refunded / cap', field: 'refunded', width: 150, align: 'right', htmlEncode: false,
          renderer: ({record}) => {
            const pct = record.cap ? Math.min(100, Math.round((record.refunded / record.cap) * 100)) : 0;
            return {children: [
              {tag: 'div', className: 'pb-sr-t pb-sr-num', text: `${money(record.refunded)} / ${money(record.cap)}`},
              {tag: 'div', className: 'pb-capbar', children: [{tag: 'div', className: 'pb-capfill' + (pct >= 100 ? ' full' : ''), style: `width:${pct}%`}]},
            ]};
          }},
      ],
      resources: data.resources,
      events: data.events,
      eventRenderer: ({eventRecord}) => enc(eventRecord.name),
      features: {
        timeRanges: {showCurrentTimeLine: true},
        eventTooltip: {
          template: ({eventRecord: e}) => {
            const r = sched.resourceStore.getById(e.resourceId);
            if (e.kind === 'window') {
              return `<div class="pb-tip"><b>${enc(r.name)}</b><span>Order #${enc(r.id)} · paid ${enc(money(r.paid))}</span>` +
                `<span>Covered ${enc(e.startDate.toLocaleDateString())} → ${enc(e.endDate.toLocaleDateString())}</span>` +
                `<span>Refunded ${enc(money(r.refunded))} of a ${enc(money(r.cap))} cap</span></div>`;
            }
            const x = e.data.settlement;
            return `<div class="pb-tip"><b>${enc(x.action === 'refund' ? 'Refund ' + money(x.amount_cents) : 'Blocked by a guardrail')}</b>` +
              `<span>${enc(human(x.reason))} · market ${enc(money(x.market_cents))} (${enc(x.price_source)})</span>` +
              (x.action === 'refund' ? `<span>PayPal: ${enc(human(x.status))}${x.paypal_refund_id ? ' · ' + enc(x.paypal_refund_id) : ''}</span>` : '') +
              `<span>${enc(new Date(x.created_at * 1000).toLocaleString())}</span></div>`;
          },
        },
        eventMenu: false, scheduleMenu: false, cellMenu: false, eventEdit: false,
        eventDrag: false, eventResize: false, eventDragCreate: false, eventCopyPaste: false,
      },
    });
  }

  async function render(el, st) {
    const list = covered(st);
    const emptyEl = el.querySelector('.pb-sched-empty'), host = el.querySelector('.pb-sched-host');
    emptyEl.hidden = list.length > 0;
    host.hidden = list.length === 0;
    if (!list.length) return;
    try { await load(); } catch (e) { toast(e.message, 'err'); return; }
    const data = build(st);
    const sig = JSON.stringify([data.resources, data.events.map((e) => [e.id, e.name, e.eventColor])]);
    const span = `${data.start.getTime()}-${data.end.getTime()}`;
    if (!sched) { create(host, data); lastSig = sig; lastSpan = span; return; }
    if (sig !== lastSig) { sched.resourceStore.data = data.resources; sched.eventStore.data = data.events; lastSig = sig; }
    if (span !== lastSpan) { sched.setTimeSpan(data.start, data.end); lastSpan = span; }
  }

  function zoom(dir) { if (sched) (dir > 0 ? sched.zoomIn() : sched.zoomOut()); }
  function today() { if (sched) sched.scrollToNow({block: 'center', animate: true}); }

  return {render, zoom, today, covered};
})();
