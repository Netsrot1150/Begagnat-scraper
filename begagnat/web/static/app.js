/* The whole frontend. No build step and no framework on purpose: this runs on
   the user's own machine, and a single file they can read beats a bundle.

   Every view is a render function that reads from the API and writes into its
   own container. State lives in `state`; nothing else is shared. */

const state = {
  view: 'finds',
  sources: [],
  regions: [],
  listings: [],
  watches: [],
  selectedKey: null,
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `${response.status} ${response.statusText}`);
  return data;
}

/* ----------------------------------------------------------- formatting */

const kr = (value) =>
  value === null || value === undefined ? '–' : Math.round(value).toLocaleString('sv-SE') + ' kr';
const num = (value, digits = 0) =>
  value === null || value === undefined ? '–' : Number(value).toLocaleString('sv-SE', {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });

const TIER_MARK = { great: '★★★', good: '★★', ok: '★', poor: '·' };

function tierOf(listing) {
  return listing.gpu_detail?.ai_tier || listing.gpu?.ai_tier || '';
}

function when(iso) {
  if (!iso) return '–';
  const then = new Date(iso);
  const minutes = Math.round((Date.now() - then.getTime()) / 60000);
  if (minutes < 1) return 'nyss';
  if (minutes < 60) return `${minutes} min sedan`;
  if (minutes < 60 * 24) return `${Math.round(minutes / 60)} tim sedan`;
  const days = Math.round(minutes / 1440);
  if (days < 30) return `${days} dgr sedan`;
  return then.toLocaleDateString('sv-SE');
}

function interval(seconds) {
  if (seconds % 86400 === 0) return `${seconds / 86400} dygn`;
  if (seconds % 3600 === 0) return `${seconds / 3600} tim`;
  return `${Math.round(seconds / 60)} min`;
}

const escapeHtml = (text) =>
  String(text ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* --------------------------------------------------------------- routing */

function show(view) {
  state.view = view;
  $$('.view').forEach((section) => { section.hidden = section.id !== `view-${view}`; });
  $$('.tab').forEach((tab) => tab.classList.toggle('active', tab.dataset.view === view));
  location.hash = view;
  ({
    finds: loadFinds,
    watches: loadWatches,
    search: () => {},
    feed: loadFeed,
    stats: loadStats,
    cards: loadCards,
  })[view]();
}

$$('.tab').forEach((tab) => tab.addEventListener('click', () => show(tab.dataset.view)));

/* ---------------------------------------------------------------- status */

async function loadStatus() {
  try {
    const status = await api('/api/status');
    state.sources = status.sources;
    state.regions = status.regions || [];
    const pill = $('#status-pill');
    if (status.scheduler_running) {
      pill.className = 'pill live';
      pill.textContent = status.scheduler_busy_with
        ? `kör "${status.scheduler_busy_with}"…`
        : `${status.enabled_watches}/${status.watches} bevakningar aktiva`;
    } else {
      pill.className = 'pill dead';
      pill.textContent = 'schemaläggaren står stilla';
    }
    const badge = $('#unseen-badge');
    badge.textContent = status.unseen_events;
    badge.hidden = status.unseen_events === 0;
  } catch (err) {
    $('#status-pill').className = 'pill dead';
    $('#status-pill').textContent = 'ingen kontakt med servern';
  }
}

function renderSourceChecks(container, checked) {
  container.innerHTML = state.sources
    .map((source) => `<label class="check" title="${escapeHtml(source.description)}">
        <input type="checkbox" value="${source.name}"
          ${(checked ? checked.includes(source.name) : source.default) ? 'checked' : ''}>
        ${source.name}${source.default ? '' : ' <span class="muted">(kräver setup)</span>'}
      </label>`)
    .join('');
}

const checkedSources = (container) =>
  $$('input:checked', container).map((input) => input.value);

const capitalise = (text) => text.charAt(0).toUpperCase() + text.slice(1);

function fillRegions(sel, selected = '') {
  $(sel).innerHTML = '<option value="">hela Sverige</option>' +
    state.regions.map((name) =>
      `<option value="${escapeHtml(name)}"${name === selected ? ' selected' : ''}
        >${escapeHtml(capitalise(name))}</option>`).join('');
}

/* ------------------------------------------------------------------ Fynd */

async function loadFinds() {
  const params = new URLSearchParams({ limit: '200' });
  const add = (key, sel) => { const v = $(sel).value; if (v) params.set(key, v); };
  add('max_price', '#f-max-price');
  add('min_vram', '#f-min-vram');
  add('max_kr_per_gb', '#f-max-krgb');
  add('source', '#f-source');
  add('gpu', '#f-gpu');
  add('watch_id', '#f-watch');
  if ($('#f-starred').checked) params.set('starred', 'true');
  if ($('#f-hidden').checked) params.set('include_hidden', 'true');

  const listings = await api(`/api/listings?${params}`);
  state.listings = listings;
  renderFinds(listings);
  fillFilterOptions(listings);
}

function fillFilterOptions(listings) {
  const fill = (sel, values) => {
    const select = $(sel);
    const current = select.value;
    select.innerHTML = '<option value="">alla</option>' +
      [...new Set(values)].filter(Boolean).sort()
        .map((v) => `<option${v === current ? ' selected' : ''}>${escapeHtml(v)}</option>`).join('');
  };
  if ($('#f-source').options.length <= 1) fill('#f-source', listings.map((l) => l.source));
  if ($('#f-gpu').options.length <= 1) fill('#f-gpu', listings.map((l) => l.gpu));
}

function renderFinds(listings) {
  if (!listings.length) {
    $('#finds-table').innerHTML =
      '<p class="empty">Inga sparade annonser ännu. Kör en sökning eller lägg upp en bevakning.</p>';
    return;
  }
  $('#finds-table').innerHTML = `
    <table>
      <thead><tr>
        <th></th><th>Poäng</th><th>Pris</th><th>kr/GB</th><th>VRAM</th>
        <th>Kort</th><th>Annons</th><th>Källa</th><th>Sedd</th><th></th>
      </tr></thead>
      <tbody>${listings.map(findRow).join('')}</tbody>
    </table>`;

  $$('#finds-table tbody tr').forEach((row) => {
    row.addEventListener('click', (event) => {
      if (event.target.closest('a, button')) return;
      selectListing(row.dataset.key);
    });
  });
  $$('#finds-table .star').forEach((button) => {
    button.addEventListener('click', async () => {
      const on = !button.classList.contains('on');
      await api(`/api/listings/${encodeURIComponent(button.dataset.key)}/flags`,
        { method: 'POST', body: { starred: on } });
      button.classList.toggle('on', on);
      button.textContent = on ? '★' : '☆';
    });
  });
  $$('#finds-table .hide-btn').forEach((button) => {
    button.addEventListener('click', async () => {
      await api(`/api/listings/${encodeURIComponent(button.dataset.key)}/flags`,
        { method: 'POST', body: { hidden: true } });
      loadFinds();
    });
  });
}

// A running auction's price is the standing bid, so it is labelled rather than
// compared: "3 kr" on a 24 GB card is nobody's asking price.
function auctionLabel(listing) {
  const a = listing.auction;
  if (!a || !a.is_auction) return '';
  const parts = [a.is_ended ? 'Avslutad auktion' : 'Auktion'];
  parts.push(`${a.bid_count} bud`);
  if (a.next_bid) parts.push(`nästa bud ${kr(a.next_bid)}`);
  if (a.buy_now_price) parts.push(`köp nu ${kr(a.buy_now_price)}`);
  if (a.ends_at) parts.push(`slutar ${String(a.ends_at).slice(0, 16).replace('T', ' ')}`);
  return parts.join(' · ');
}

function findRow(listing) {
  const tier = tierOf(listing);
  return `<tr data-key="${escapeHtml(listing.key)}"
             class="${listing.hidden ? 'is-hidden' : ''}${state.selectedKey === listing.key ? ' selected' : ''}">
    <td><button class="star ${listing.starred ? 'on' : ''}" data-key="${escapeHtml(listing.key)}"
        title="Spara">${listing.starred ? '★' : '☆'}</button></td>
    <td class="num"><span class="tier tier-${tier}">${TIER_MARK[tier] || ''}</span>
        ${num(listing.score, 1)}</td>
    <td class="num">${kr(listing.price_sek)}${listing.import_cost_sek
        ? ' <span class="chip import" title="Inkl. importmoms och tullavgift">imp</span>' : ''}${
        listing.price_is_provisional ? ` <span class="chip auction" title="${escapeHtml(auctionLabel(listing))}">bud</span>` : ''}</td>
    <td class="num">${num(listing.kr_per_gb)}</td>
    <td class="num">${listing.vram_gb ? listing.vram_gb + ' GB' : '–'}</td>
    <td>${escapeHtml(listing.gpu || '?')}</td>
    <td class="title-cell"><a href="${escapeHtml(listing.url)}" target="_blank"
        rel="noopener">${escapeHtml(listing.title)}</a></td>
    <td>${escapeHtml(listing.source)}</td>
    <td class="muted">${when(listing.last_seen)}</td>
    <td><button class="link hide-btn" data-key="${escapeHtml(listing.key)}" title="Dölj">✕</button></td>
  </tr>`;
}

async function selectListing(key) {
  state.selectedKey = key;
  $$('#finds-table tbody tr').forEach((row) =>
    row.classList.toggle('selected', row.dataset.key === key));
  $('#detail').innerHTML = '<p class="muted">Hämtar…</p>';
  const listing = await api(`/api/listings/${encodeURIComponent(key)}`);
  $('#detail').innerHTML = detailHtml(listing);
  $('#detail-note')?.addEventListener('change', (event) =>
    api(`/api/listings/${encodeURIComponent(key)}/flags`,
      { method: 'POST', body: { note: event.target.value } }));
}

function detailHtml(listing) {
  const gpu = listing.gpu_detail || {};
  const window_ = listing.model_prices;
  const verdict = listing.ai;
  // How this ad compares with what the model has been asking lately. A single
  // observation makes the median itself, so 0 % is "typical", not a bargain.
  const cheaper = window_ && listing.price_sek
    ? Math.round((1 - listing.price_sek / window_.median) * 100) : null;
  const verdictWord = cheaper === null ? ''
    : cheaper > 0 ? `${cheaper} % under median`
    : cheaper < 0 ? `${-cheaper} % över median`
    : 'precis på median';

  return `
    <h3><a href="${escapeHtml(listing.url)}" target="_blank" rel="noopener"
        >${escapeHtml(listing.title)}</a></h3>
    <div class="muted">${escapeHtml(listing.source)}${listing.location
        ? ' · ' + escapeHtml(listing.location) : ''} · sedd ${when(listing.last_seen)}</div>
    <dl>
      <dt>Landat pris</dt><dd>${kr(listing.price_sek)}${
        listing.price_is_provisional ? ' <span class="chip auction">pågående bud</span>' : ''}</dd>
      ${auctionLabel(listing) ? `<dt>Auktion</dt><dd>${escapeHtml(auctionLabel(listing))}</dd>` : ''}
      ${listing.import_cost_sek ? `<dt>varav import</dt><dd>${kr(listing.import_cost_sek)}</dd>` : ''}
      ${listing.currency && listing.currency !== 'SEK'
        ? `<dt>Annonspris</dt><dd>${num(listing.price, 2)} ${escapeHtml(listing.currency)}</dd>` : ''}
      <dt>VRAM</dt><dd>${listing.vram_gb ? listing.vram_gb + ' GB' : '–'}</dd>
      <dt>kr per GB</dt><dd>${num(listing.kr_per_gb)}</dd>
      <dt>Poäng</dt><dd>${num(listing.score, 1)}</dd>
      <dt>Först sedd</dt><dd>${when(listing.first_seen)}</dd>
    </dl>
    ${window_ ? `<div class="muted">${escapeHtml(listing.gpu)} de senaste ${window_.days} dagarna
        (${window_.count} annonser): median ${kr(window_.median)}, lägst ${kr(window_.min)}.
        ${cheaper !== null ? `<b style="color:${cheaper > 0 ? 'var(--great)'
          : cheaper < 0 ? 'var(--ok)' : 'var(--fg)'}">Denna ligger ${verdictWord}.</b>` : ''}
      </div>` : ''}
    ${sparkline(listing.price_history)}
    ${verdict ? `<div class="ai"><b>AI:</b> ${escapeHtml(verdict.reason || '')}
        ${verdict.condition ? `<br><span class="muted">skick: ${escapeHtml(verdict.condition)}${
          verdict.mining_risk ? ' · misstänkt mining' : ''}</span>` : ''}</div>` : ''}
    ${gpu.notes ? `<p class="notes">${escapeHtml(gpu.notes)}</p>` : ''}
    <label>Anteckning<textarea id="detail-note" rows="2"
        placeholder="t.ex. ringt säljaren">${escapeHtml(listing.note || '')}</textarea></label>`;
}

/* A price history worth drawing needs at least two distinct observations. */
function sparkline(history) {
  const points = (history || []).filter((p) => p.price_sek != null);
  if (points.length < 2) return '';
  const values = points.map((p) => p.price_sek);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const width = 340;
  const height = 60;
  const coords = points.map((point, index) => [
    (index / (points.length - 1)) * width,
    height - ((point.price_sek - min) / span) * (height - 8) - 4,
  ]);
  const line = coords.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)} ${y.toFixed(1)}`).join(' ');
  return `<div class="muted">Prishistorik ${kr(min)} – ${kr(max)}</div>
    <svg class="spark" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
      <path class="fill" d="${line} L${width} ${height} L0 ${height} Z"></path>
      <path d="${line}"></path>
    </svg>`;
}

$('#f-reload').addEventListener('click', loadFinds);
$$('#view-finds .toolbar input, #view-finds .toolbar select')
  .forEach((input) => input.addEventListener('change', loadFinds));

/* ----------------------------------------------------------- Bevakningar */

async function loadWatches() {
  state.watches = await api('/api/watches');
  const list = $('#watch-list');
  if (!state.watches.length) {
    list.innerHTML = '<p class="empty">Inga bevakningar än. En bevakning kör din sökning ' +
      'automatiskt och lägger nya träffar i flödet.</p>';
  } else {
    list.innerHTML = state.watches.map(watchCard).join('');
    $$('#watch-list [data-act]').forEach((button) =>
      button.addEventListener('click', () => watchAction(button.dataset.act, +button.dataset.id)));
  }
  const select = $('#f-watch');
  select.innerHTML = '<option value="">alla</option>' +
    state.watches.map((w) => `<option value="${w.id}">${escapeHtml(w.name)}</option>`).join('');
}

function watchCard(watch) {
  const criteria = watch.criteria;
  const limits = [
    criteria.max_price ? `≤ ${kr(criteria.max_price)}` : null,
    criteria.min_vram ? `≥ ${criteria.min_vram} GB` : null,
    criteria.max_kr_per_gb ? `≤ ${criteria.max_kr_per_gb} kr/GB` : null,
    criteria.min_tier ? `nivå ${criteria.min_tier}+` : null,
    criteria.region ? capitalise(criteria.region) : null,
  ].filter(Boolean).join(' · ');

  return `<div class="watch ${watch.enabled ? '' : 'off'}">
    <div>
      <h4>${escapeHtml(watch.name)} ${watch.use_ai ? '<span class="chip">AI</span>' : ''}</h4>
      <div class="meta">"${escapeHtml(criteria.query || criteria.models?.join(', ') || '')}"
        på ${escapeHtml((criteria.sources || ['blocket', 'tradera', 'ebay']).join(', '))}
        · var ${interval(watch.interval_s)}${limits ? ' · ' + limits : ''}</div>
      <div class="meta">Senast: ${when(watch.last_run_at)} · ${watch.last_hits} träffar</div>
      ${watch.last_error ? `<div class="err">${escapeHtml(watch.last_error)}</div>` : ''}
    </div>
    <div class="actions">
      <button data-act="run" data-id="${watch.id}">Kör nu</button>
      <button data-act="toggle" data-id="${watch.id}">${watch.enabled ? 'Pausa' : 'Starta'}</button>
      <button data-act="edit" data-id="${watch.id}">Ändra</button>
      <button data-act="delete" data-id="${watch.id}" title="Ta bort">✕</button>
    </div>
  </div>`;
}

async function watchAction(action, id) {
  const watch = state.watches.find((w) => w.id === id);
  if (action === 'edit') return openWatchForm(watch);
  if (action === 'delete') {
    if (!confirm(`Ta bort bevakningen "${watch.name}"?`)) return;
    await api(`/api/watches/${id}`, { method: 'DELETE' });
    return loadWatches();
  }
  if (action === 'toggle') {
    await api(`/api/watches/${id}`, { method: 'PUT', body: { ...watch, enabled: !watch.enabled } });
    return loadWatches();
  }
  if (action === 'run') {
    $('#w-hint').textContent = `Kör "${watch.name}"…`;
    try {
      const result = await api(`/api/watches/${id}/run`, { method: 'POST' });
      const failures = Object.entries(result.errors || {});
      $('#w-hint').textContent = `"${watch.name}": ${result.hits} träffar av ` +
        `${result.fetched} annonser${failures.length ? ` — fel: ${failures
          .map(([name, error]) => `${name} (${error})`).join(', ')}` : ''}`;
    } catch (error) {
      $('#w-hint').innerHTML = `<span class="error">${escapeHtml(error.message)}</span>`;
    }
    loadWatches();
    loadStatus();
  }
}

function openWatchForm(watch) {
  const form = $('#watch-form');
  const criteria = watch?.criteria || {};
  form.hidden = false;
  $('#wf-title').textContent = watch ? `Ändra "${watch.name}"` : 'Ny bevakning';
  $('#wf-id').value = watch?.id || '';
  $('#wf-name').value = watch?.name || '';
  $('#wf-query').value = criteria.query || '';
  $('#wf-max-price').value = criteria.max_price ?? '';
  $('#wf-min-vram').value = criteria.min_vram ?? '';
  $('#wf-max-krgb').value = criteria.max_kr_per_gb ?? '';
  $('#wf-min-tier').value = criteria.min_tier || '';
  fillRegions('#wf-region', criteria.region || '');
  $('#wf-exclude').value = (criteria.exclude || []).join(', ');
  $('#wf-models').value = (criteria.models || []).join(', ');
  $('#wf-interval').value = String(watch?.interval_s || 900);
  $('#wf-enabled').checked = watch ? watch.enabled : true;
  $('#wf-ai').checked = watch ? watch.use_ai : false;
  $('#wf-import').checked = criteria.include_import_cost !== false;
  renderSourceChecks($('#wf-sources'), criteria.sources);
  $('#wf-error').textContent = '';
  form.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

$('#w-new').addEventListener('click', () => openWatchForm(null));
$('#wf-cancel').addEventListener('click', () => { $('#watch-form').hidden = true; });

$('#watch-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const id = $('#wf-id').value;
  const listOf = (sel) => $(sel).value.split(',').map((s) => s.trim()).filter(Boolean);
  const numberOf = (sel) => ($(sel).value === '' ? undefined : Number($(sel).value));
  const body = {
    name: $('#wf-name').value.trim(),
    interval_s: Number($('#wf-interval').value),
    enabled: $('#wf-enabled').checked,
    use_ai: $('#wf-ai').checked,
    criteria: {
      query: $('#wf-query').value.trim(),
      max_price: numberOf('#wf-max-price'),
      min_vram: numberOf('#wf-min-vram'),
      max_kr_per_gb: numberOf('#wf-max-krgb'),
      min_tier: $('#wf-min-tier').value || undefined,
      region: $('#wf-region').value || undefined,
      exclude: listOf('#wf-exclude'),
      models: listOf('#wf-models'),
      sources: checkedSources($('#wf-sources')),
      include_import_cost: $('#wf-import').checked,
    },
  };
  try {
    await api(id ? `/api/watches/${id}` : '/api/watches',
      { method: id ? 'PUT' : 'POST', body });
    $('#watch-form').hidden = true;
    loadWatches();
    loadStatus();
  } catch (error) {
    $('#wf-error').textContent = error.message;
  }
});

/* ------------------------------------------------------------------- Sök */

$('#search-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = $('#search-form button[type=submit]');
  button.disabled = true;
  $('#s-status').textContent = 'söker… (kan ta en halv minut)';
  const numberOf = (sel) => ($(sel).value === '' ? undefined : Number($(sel).value));
  try {
    const result = await api('/api/search', {
      method: 'POST',
      body: {
        store: $('#s-store').checked,
        criteria: {
          query: $('#s-query').value.trim(),
          max_price: numberOf('#s-max-price'),
          min_vram: numberOf('#s-min-vram'),
          max_kr_per_gb: numberOf('#s-max-krgb'),
          region: $('#s-region').value || undefined,
          sources: checkedSources($('#s-sources')),
        },
      },
    });
    renderSearchResults(result);
    $('#s-status').textContent =
      `${result.hits.length} träffar av ${result.fetched} annonser, ${result.new.length} nya`;
  } catch (error) {
    $('#s-status').innerHTML = `<span class="error">${escapeHtml(error.message)}</span>`;
  } finally {
    button.disabled = false;
  }
});

function renderSearchResults(result) {
  const failures = Object.entries(result.errors || {});
  const rows = result.hits.map((hit) => findRow({ ...hit, last_seen: hit.first_seen }));
  $('#search-results').innerHTML = `
    ${failures.map(([name, error]) =>
      `<div class="notice"><b>${escapeHtml(name)}</b> svarade inte: ${escapeHtml(error)}</div>`).join('')}
    ${rows.length ? `<table><thead><tr>
        <th></th><th>Poäng</th><th>Pris</th><th>kr/GB</th><th>VRAM</th>
        <th>Kort</th><th>Annons</th><th>Källa</th><th>Sedd</th><th></th>
      </tr></thead><tbody>${rows.join('')}</tbody></table>`
      : '<p class="empty">Inga träffar.</p>'}
    ${$('#s-rejected').checked && result.rejected.length ? `
      <h3>Bortsorterade (${result.rejected.length})</h3>
      <table><thead><tr><th>Annons</th><th>Källa</th><th>Varför</th></tr></thead><tbody>
        ${result.rejected.map((row) => `<tr>
          <td class="title-cell"><a href="${escapeHtml(row.url)}" target="_blank"
            rel="noopener">${escapeHtml(row.title)}</a></td>
          <td>${escapeHtml(row.source)}</td>
          <td class="muted">${escapeHtml(row.reason)}</td></tr>`).join('')}
      </tbody></table>` : ''}`;
}

/* ----------------------------------------------------------------- Flöde */

const EVENT_LABEL = { new: 'Ny träff', price_drop: 'Prissänkning', error: 'Fel' };

async function loadFeed() {
  const events = await api(`/api/events?limit=200&unseen_only=${$('#feed-unseen').checked}`);
  $('#feed-list').innerHTML = events.length
    ? events.map(eventRow).join('')
    : '<p class="empty">Inget i flödet ännu.</p>';
}

function eventRow(event) {
  const payload = event.payload || {};
  const drop = event.kind === 'price_drop' && payload.previous_price_sek
    ? `<span class="chip drop">${kr(payload.previous_price_sek)} → ${kr(payload.price_sek)}</span>`
    : '';
  return `<div class="event ${event.seen ? '' : 'unseen'}">
    <span class="muted">${EVENT_LABEL[event.kind] || event.kind}</span>
    <span>${payload.url
      ? `<a href="${escapeHtml(payload.url)}" target="_blank" rel="noopener">${escapeHtml(payload.title || event.key)}</a>`
      : escapeHtml(payload.error || event.key || '')}
      ${payload.price_sek ? ` — <b>${kr(payload.price_sek)}</b>` : ''}
      ${payload.kr_per_gb ? ` <span class="chip">${num(payload.kr_per_gb)} kr/GB</span>` : ''}
      ${drop}
      ${event.watch_name ? ` <span class="muted">(${escapeHtml(event.watch_name)})</span>` : ''}</span>
    <span class="when">${when(event.created_at)}</span>
  </div>`;
}

$('#feed-seen').addEventListener('click', async () => {
  await api('/api/events/seen', { method: 'POST', body: {} });
  loadFeed();
  loadStatus();
});
$('#feed-unseen').addEventListener('change', loadFeed);

/* ------------------------------------------------------------ Statistik */

async function loadStats() {
  const rows = await api(`/api/stats?days=${$('#stats-days').value}`);
  if (!rows.length) {
    $('#stats-list').innerHTML =
      '<p class="empty">Ingen prishistorik ännu — den byggs upp av dina sökningar och bevakningar.</p>';
    return;
  }
  const ceiling = Math.max(...rows.map((row) => row.max));
  $('#stats-list').innerHTML = rows.map((row) => {
    const pct = (value) => (value / ceiling) * 100;
    return `<div class="stat-row">
      <div><b>${escapeHtml(row.gpu)}</b><div class="muted">${row.count} annonser</div></div>
      <div class="stat-bar">
        <div class="whisker" style="left:${pct(row.min)}%;width:${pct(row.max - row.min)}%"></div>
        <div class="box" style="left:${pct(row.p25)}%;width:${Math.max(pct(row.p75 - row.p25), 0.4)}%"></div>
        <div class="med" style="left:${pct(row.median)}%"></div>
      </div>
      <div class="muted">min ${kr(row.min)} · median <b style="color:var(--fg)">${kr(row.median)}</b>
        · max ${kr(row.max)}</div>
    </div>`;
  }).join('');
}

$('#stats-days').addEventListener('change', loadStats);

/* ---------------------------------------------------------- Kortkatalog */

async function loadCards() {
  const cards = await api(`/api/gpus?min_vram=${$('#c-min-vram').value || 0}`);
  $('#card-list').innerHTML = `<table><thead><tr>
      <th>Kort</th><th>VRAM</th><th class="num">GB/s</th><th class="num">W</th>
      <th>Nivå</th><th>Att veta</th></tr></thead><tbody>
    ${cards.map((card) => `<tr>
      <td><b>${escapeHtml(card.name)}</b><div class="muted">${escapeHtml(card.arch)}</div></td>
      <td>${(card.vram_variants.length > 1 ? card.vram_variants.join('/') : card.vram_gb)} GB</td>
      <td class="num">${num(card.bandwidth_gbs)}</td>
      <td class="num">${num(card.tdp_w)}</td>
      <td><span class="tier tier-${card.ai_tier}">${TIER_MARK[card.ai_tier] || ''}</span>
        <span class="muted">${escapeHtml(card.ai_tier)}</span></td>
      <td class="muted">${escapeHtml(card.notes)}</td>
    </tr>`).join('')}</tbody></table>`;
}

$('#c-reload').addEventListener('click', loadCards);

/* ------------------------------------------------------------------ boot */

(async function start() {
  await loadStatus();
  renderSourceChecks($('#s-sources'));
  fillRegions('#s-region');
  const watches = await api('/api/watches');
  state.watches = watches;
  $('#f-watch').innerHTML = '<option value="">alla</option>' +
    watches.map((w) => `<option value="${w.id}">${escapeHtml(w.name)}</option>`).join('');
  show(location.hash.slice(1) || 'finds');
  // The scheduler works in the background; keep the header honest about it.
  setInterval(loadStatus, 15000);
})();
