/* AG Grid (Community) data grids for the merchant console: orders, refund decisions, webhooks and the catalog.
   Loaded before the console script; it only touches the console's globals (h, chip, money, S …) when called.
   Cell renderers always return DOM nodes or plain text, never HTML strings. */
const PBGrid = (() => {
  const ok = typeof agGrid !== 'undefined';
  if (ok && agGrid.ModuleRegistry && agGrid.AllCommunityModule) agGrid.ModuleRegistry.registerModules([agGrid.AllCommunityModule]);

  // The grid reads the console's own colour tokens, so it follows light and dark mode with the rest of the page.
  const theme = ok ? agGrid.themeQuartz.withParams({
    backgroundColor: 'var(--card)',
    foregroundColor: 'var(--ink)',
    textColor: 'var(--ink)',
    subtleTextColor: 'var(--mut)',
    accentColor: 'var(--accent)',
    borderColor: 'var(--line)',
    chromeBackgroundColor: 'var(--card)',
    headerBackgroundColor: 'var(--card)',
    headerTextColor: 'var(--mut)',
    oddRowBackgroundColor: 'color-mix(in srgb, var(--paper-2) 30%, var(--card))',
    rowHoverColor: 'color-mix(in srgb, var(--accent) 7%, transparent)',
    selectedRowBackgroundColor: 'color-mix(in srgb, var(--accent) 12%, transparent)',
    inputBackgroundColor: 'var(--card)',
    inputBorder: {color: 'var(--line-2)'},
    fontFamily: ['Bricolage Grotesque', 'Segoe UI', 'sans-serif'],
    headerFontFamily: ['IBM Plex Mono', 'Consolas', 'monospace'],
    headerFontSize: 11,
    headerFontWeight: 600,
    fontSize: 13.5,
    spacing: 7,
    rowHeight: 50,
    headerHeight: 42,
    wrapperBorder: false,
    wrapperBorderRadius: 0,
    columnBorder: false,
    headerColumnBorder: false,
    rowBorder: {style: 'dashed', color: 'var(--line)'},
    headerRowBorder: {width: 1.5, color: 'var(--ink)'},
  }) : null;

  const grids = {};   // key -> {api, el}
  const dayMs = 86400000;
  const asDate = (t) => (t ? new Date(t * 1000) : null);
  const fmtDate = (p) => (p.value ? when(p.value.getTime() / 1000) : '—');
  const fmtMoney = (p) => (p.value === null || p.value === undefined ? '—' : money(Math.round(p.value * 100)));
  const pinned = (p) => Boolean(p.node && p.node.rowPinned);
  const dateFilter = {
    filter: 'agDateColumnFilter',
    filterParams: {comparator: (day, v) => { if (!v) return -1; const d = new Date(v); d.setHours(0, 0, 0, 0); return d < day ? -1 : d > day ? 1 : 0; }},
  };
  const moneyCol = (headerName, valueGetter, extra = {}) => ({headerName, valueGetter, valueFormatter: fmtMoney, type: 'rightAligned', filter: 'agNumberColumnFilter', cellClass: 'pb-num', minWidth: 110, ...extra});
  const mono = (t) => h('span', {class: 'pb-mono'}, t || '—');

  function make(key, el, {totals: totalsFn, ...opts}) {
    if (grids[key]) return grids[key].api;
    const api = agGrid.createGrid(el, {
      theme,
      domLayout: 'autoHeight',
      animateRows: true,
      pagination: true,
      paginationPageSize: 20,
      paginationPageSizeSelector: [20, 50, 100],
      suppressCellFocus: false,
      enableCellTextSelection: true,
      tooltipShowDelay: 300,
      defaultColDef: {sortable: true, filter: true, resizable: true, flex: 1, minWidth: 90, cellDataType: false},
      onFilterChanged: () => totals(key),
      onRowDataUpdated: () => totals(key),
      ...opts,
    });
    grids[key] = {api, el, totals: totalsFn};
    return api;
  }

  // Pinned totals row that follows the active filters.
  function totals(key) {
    const g = grids[key];
    if (!g || !g.totals) return;
    const rows = [];
    g.api.forEachNodeAfterFilter((n) => { if (n.data) rows.push(n.data); });
    g.api.setGridOption('pinnedBottomRowData', [g.totals(rows)]);
  }

  // Search box + CSV export above a grid.
  function toolbar(key, label, extra = []) {
    const search = h('input', {type: 'search', class: 'pb-grid-search', placeholder: 'Search every column…', 'aria-label': `Search ${label}`});
    search.addEventListener('input', () => grids[key] && grids[key].api.setGridOption('quickFilterText', search.value));
    const csv = h('button', {type: 'button', class: 'ghost small', onclick: () => grids[key] && grids[key].api.exportDataAsCsv({
      fileName: `priceback-${key}-${new Date().toISOString().slice(0, 10)}.csv`,
      processCellCallback: (p) => (p.value instanceof Date ? p.value.toISOString() : p.value),
    })}, 'Export CSV');
    return h('div', {class: 'pb-grid-tools'}, search, h('span', {class: 'pb-grid-actions'}, ...extra, csv));
  }

  function set(key, rows) {
    const g = grids[key];
    if (g) g.api.setGridOption('rowData', rows);
  }

  // ---- orders ---------------------------------------------------------------------------------------------
  function orders(el, rows) {
    if (!grids.orders) {
      make('orders', el, {
        getRowId: (p) => String(p.data.id),
        columnDefs: [
          {headerName: '#', field: 'id', maxWidth: 86, minWidth: 70, sort: 'desc', filter: 'agNumberColumnFilter', cellClass: 'pb-num', valueFormatter: (p) => pinned(p) ? '' : String(p.value)},
          {headerName: 'Product', field: 'title', flex: 2.2, minWidth: 220, tooltipField: 'title',
            cellRenderer: (p) => pinned(p) ? h('b', {}, p.value) : h('div', {class: 'pb-two'}, h('b', {}, p.value), h('small', {}, p.data.paypal_order_id))},
          moneyCol('Paid', (p) => p.data.paid_cents / 100),
          {headerName: 'Status', field: 'status', minWidth: 140, cellRenderer: (p) => pinned(p) ? '' : statusChip(p.value)},
          {headerName: 'Covered', minWidth: 110, valueGetter: (p) => pinned(p) ? '' : (p.data.guaranteed ? 'covered' : 'no'),
            cellRenderer: (p) => p.value === 'covered' ? chip('covered', 'ok') : h('span', {class: 'mut'}, p.value)},
          {headerName: 'Cover ends', minWidth: 160, valueGetter: (p) => asDate(p.data.cover_ends_at), ...dateFilter,
            cellRenderer: (p) => {
              if (pinned(p) || !p.value) return h('span', {class: 'mut'}, pinned(p) ? '' : '—');
              const left = Math.ceil((p.value.getTime() - Date.now()) / dayMs);
              return h('div', {class: 'pb-two'}, h('span', {}, p.value.toLocaleDateString([], {month: 'short', day: 'numeric'})),
                h('small', {class: left > 0 ? '' : 'mut'}, left > 0 ? `${left} day${left === 1 ? '' : 's'} left` : 'window closed'));
            }},
          moneyCol('Refunded', (p) => pinned(p) ? p.data.refunded : refundedFor(p.data.id) / 100, {cellClassRules: {'pb-good': (p) => p.value > 0}}),
          moneyCol('Still at risk', (p) => pinned(p) ? p.data.risk : atRisk(p.data) / 100, {headerTooltip: 'The most this order could still be refunded while its window is open'}),
          {headerName: 'Captured', minWidth: 140, valueGetter: (p) => asDate(p.data.captured_at), valueFormatter: (p) => pinned(p) ? '' : fmtDate(p), ...dateFilter},
          {headerName: 'Webhook', minWidth: 120, valueGetter: (p) => pinned(p) ? '' : (p.data.capture_webhook_at ? 'confirmed' : '—'),
            cellRenderer: (p) => p.value === 'confirmed' ? chip('confirmed', 'ok') : h('span', {class: 'mut'}, p.value)},
        ],
        totals: (rs) => ({id: '', title: `Total · ${rs.length} order${rs.length === 1 ? '' : 's'}`, paid_cents: rs.reduce((a, o) => a + o.paid_cents, 0),
          refunded: rs.reduce((a, o) => a + refundedFor(o.id), 0) / 100, risk: rs.reduce((a, o) => a + atRisk(o), 0) / 100}),
      });
    }
    set('orders', rows);
  }
  function atRisk(o) {
    if (!o.guaranteed || !o.cover_ends_at || o.status !== 'CAPTURED' || o.cover_ends_at * 1000 < Date.now()) return 0;
    return Math.max(0, Math.min(o.max_refund_cents || 0, o.paid_cents) - refundedFor(o.id));
  }

  // ---- refund decisions ------------------------------------------------------------------------------------------
  function refunds(el, rows) {
    if (!grids.refunds) {
      make('refunds', el, {
        getRowId: (p) => String(p.data.id),
        columnDefs: [
          {headerName: 'When', minWidth: 140, sort: 'desc', valueGetter: (p) => asDate(p.data.created_at), valueFormatter: (p) => pinned(p) ? '' : fmtDate(p), ...dateFilter},
          {headerName: 'Order', field: 'order_id', maxWidth: 100, filter: 'agNumberColumnFilter', valueFormatter: (p) => p.value === '' ? '' : `#${p.value}`},
          {headerName: 'Product', field: 'title', flex: 2, minWidth: 200, tooltipField: 'title'},
          {headerName: 'Decision', field: 'action', minWidth: 120, cellRenderer: (p) => pinned(p) ? '' : chip(p.value === 'refund' ? 'refund' : 'blocked', p.value === 'refund' ? 'ok' : 'bad')},
          moneyCol('Amount', (p) => p.data.amount_cents / 100, {cellClassRules: {'pb-good': (p) => p.data.action === 'refund' || pinned(p)}}),
          {headerName: 'Reason', field: 'reason', minWidth: 170, valueFormatter: (p) => human(p.value), tooltipValueGetter: (p) => human(p.value)},
          moneyCol('Market price', (p) => pinned(p) ? null : p.data.market_cents / 100),
          {headerName: 'Price source', field: 'price_source', minWidth: 130, cellRenderer: (p) => pinned(p) ? '' : chip(p.value, p.value === 'simulated' ? 'accent' : '')},
          {headerName: 'PayPal', field: 'status', minWidth: 130, cellRenderer: (p) => pinned(p) || p.data.action !== 'refund' ? h('span', {class: 'mut'}, pinned(p) ? '' : '—') : statusChip(p.value)},
          {headerName: 'Refund ID', field: 'paypal_refund_id', minWidth: 150, cellRenderer: (p) => pinned(p) ? '' : mono(p.value)},
          {headerName: 'Webhook', minWidth: 120, valueGetter: (p) => pinned(p) ? '' : (p.data.webhook_confirmed_at ? 'confirmed' : (p.data.action === 'refund' ? 'not yet' : '—')),
            cellRenderer: (p) => p.value === 'confirmed' ? chip('confirmed', 'ok') : h('span', {class: 'mut'}, p.value)},
          {headerName: 'Message to buyer', minWidth: 260, flex: 2, valueGetter: (p) => p.data.error || p.data.explanation || '', tooltipValueGetter: (p) => p.value,
            cellClassRules: {'pb-bad': (p) => Boolean(p.data && p.data.error)}},
        ],
        totals: (rs) => {
          const paid = rs.filter((x) => x.action === 'refund' && x.status !== 'FAILED');
          return {id: '', created_at: null, order_id: '', title: `Refunded · ${paid.length} of ${rs.length} decisions`, action: '', amount_cents: paid.reduce((a, x) => a + x.amount_cents, 0), reason: ''};
        },
      });
    }
    set('refunds', rows);
  }

  // ---- webhooks ----------------------------------------------------------------------------------------------------
  function ledger(el, rows) {
    if (!grids.ledger) {
      make('ledger', el, {
        getRowId: (p) => String(p.data.event_id),
        columnDefs: [
          {headerName: 'Received', minWidth: 150, sort: 'desc', valueGetter: (p) => asDate(p.data.received_at), valueFormatter: fmtDate, ...dateFilter},
          {headerName: 'Event', field: 'event_type', flex: 1.6, minWidth: 220, cellRenderer: (p) => mono(p.value)},
          {headerName: 'Resource', field: 'resource_id', flex: 1.3, minWidth: 170, cellRenderer: (p) => mono(p.value)},
          {headerName: 'Event ID', field: 'event_id', flex: 1.3, minWidth: 170, cellRenderer: (p) => mono(p.value)},
          {headerName: 'Signature', minWidth: 120, valueGetter: (p) => (p.data.verified ? 'verified' : 'unverified'), cellRenderer: (p) => chip(p.value, p.value === 'verified' ? 'ok' : 'bad')},
        ],
      });
    }
    set('ledger', rows);
  }

  // ---- catalog (inline editing + bulk remove) -------------------------------------------------------------------------
  let onCatalogChange = null;
  function catalog(el, rows, {selectionChanged, changed}) {
    onCatalogChange = changed;
    if (!grids.catalog) {
      make('catalog', el, {
        getRowId: (p) => p.data.id,
        rowHeight: 60,
        rowSelection: {mode: 'multiRow', headerCheckbox: true, checkboxes: true, enableClickSelection: false},
        selectionColumnDef: {pinned: 'left', maxWidth: 52, minWidth: 52},
        stopEditingWhenCellsLoseFocus: true,
        singleClickEdit: false,
        onSelectionChanged: (e) => selectionChanged(e.api.getSelectedRows()),
        onCellValueChanged: async (e) => {
          const field = e.colDef.field;
          if (e.newValue === e.oldValue) return;
          try {
            const p = await api(`/api/products/${encodeURIComponent(e.data.id)}`, {method: 'PATCH', body: JSON.stringify({[field]: e.newValue ?? ''})});
            toast(`Saved “${p.title}”.`);
            if (onCatalogChange) onCatalogChange();
          } catch (err) {
            e.node.setDataValue(field, e.oldValue);
            toast(err.message, 'err');
          }
        },
        columnDefs: [
          {headerName: '', field: 'image_url', maxWidth: 72, minWidth: 72, sortable: false, filter: false, resizable: false,
            cellRenderer: (p) => h('span', {class: 'pb-thumb'}, productArt(p.data))},
          {headerName: 'Name · double-click to edit', field: 'title', flex: 2.4, minWidth: 240, editable: true, tooltipField: 'title',
            valueParser: (p) => String(p.newValue || '').trim() || p.oldValue, cellClass: 'pb-editable'},
          {headerName: 'Category', field: 'category', flex: 1.2, minWidth: 160, editable: true, cellClass: 'pb-editable',
            valueFormatter: (p) => p.value || 'Uncategorised',
            cellEditorSelector: () => {  // pick from existing categories; free text while there are none yet
              const values = [...new Set(S.products.map((x) => x.category).filter(Boolean))].sort();
              return values.length ? {component: 'agSelectCellEditor', params: {values}} : {component: 'agTextCellEditor'};
            }},
          moneyCol('List price', (p) => p.data.price_cents / 100, {headerTooltip: 'Locked: it is the market price buyers paid against'}),
          moneyCol('Market now', (p) => { const sp = (S.stats && S.stats.simulated_prices || {})[p.data.id]; return (sp === undefined ? p.data.price_cents : sp) / 100; },
            {cellClassRules: {'pb-accent': (p) => (S.stats && S.stats.simulated_prices || {})[p.data.id] !== undefined}}),
          {headerName: 'Source', field: 'source', minWidth: 110, cellRenderer: (p) => chip(p.value === 'channel3' ? 'Channel3' : 'manual')},
          {headerName: 'Covered orders', minWidth: 130, type: 'rightAligned', filter: 'agNumberColumnFilter', cellClass: 'pb-num',
            valueGetter: (p) => S.orders.filter((o) => o.product_id === p.data.id && o.guaranteed && o.status === 'CAPTURED').length},
          {headerName: 'ID', field: 'id', minWidth: 150, cellRenderer: (p) => mono(p.value)},
        ],
      });
    }
    const g = grids.catalog;
    if (g.api.getEditingCells().length) return;  // never yank a cell out from under the merchant mid-edit
    set('catalog', rows);
    g.api.refreshCells({force: true});
  }
  const deselectAll = (key) => grids[key] && grids[key].api.deselectAll();

  return {ok, toolbar, orders, refunds, ledger, catalog, deselectAll};
})();
