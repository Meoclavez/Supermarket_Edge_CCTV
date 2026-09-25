/**
 * Edge AI CCTV - dashboard controller
 *
 * Owns: navigation (Today / Cameras / Store map / Insights / Loss prevention,
 * Settings behind the header gear) with redirects for the old hashes, the
 * header, the live camera matrix, the camera configuration modal, the
 * "Shoppers & footfall" insights view (visitors by hour, journey, areas), the
 * Settings "Sales data" and "System health" cards, the shared helpers other
 * modules use (today.js, loss.js, insights.js), and an opt-in self test
 * (?__selftest=1).
 *
 * Rules this file follows:
 *  - Every number shown comes from an API response. A value the API reports
 *    as null / absent renders as an em dash, never as 0 or a constant.
 *  - Nothing here calls prompt(), confirm() or alert(); destructive actions
 *    use an inline two-step confirmation.
 */

'use strict';

const DASH = '—';

// Top-level views -> their section element.
const VIEW_SECTIONS = {
  today: 'tab-today',
  cameras: 'tab-matrix',
  map: 'tab-floorplan',
  insights: 'tab-insights',
  loss: 'tab-theft',
  settings: 'tab-settings',
};
// Insights sub-views -> their element.
const INSIGHT_SUBS = { recs: 'insights-recs', footfall: 'tab-analytics', report: 'tab-digest' };
// Old hashes (links, bookmarks, the phone app) -> their new home.
const LEGACY_ROUTES = {
  matrix: 'cameras',
  floorplan: 'map',
  analytics: 'insights/footfall',
  actions: 'insights/recs',
  market_ai: 'insights/recs',
  theft: 'loss',
  digest: 'insights/report',
  settings: 'settings',
};
// Every route the self test walks, old ones included.
const VALID_TABS = ['today', 'cameras', 'map', 'insights', 'insights/recs', 'insights/footfall', 'insights/report',
  'loss', 'settings', ...Object.keys(LEGACY_ROUTES)];

let activeCameraFilter = 'ALL';
let currentDecimationFPS = 5;
let allCamerasList = [];
let layoutSnapshot = null;          // last GET /api/v1/layout
let pipelineSnapshot = null;        // last GET /api/v1/layout/pipeline/status
let lastCameraSignature = null;     // change detector for the matrix re-render
let currentView = null;
let currentSub = null;
let lastInsightsSub = 'recs';     // the Insights tab reopens where the operator left it

// ==========================================
// Small helpers (also used by today.js, loss.js and insights.js)
// ==========================================
function el(id) { return document.getElementById(id); }

function escapeHtml(v) {
  return String(v === null || v === undefined ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function isNum(v) { return typeof v === 'number' && Number.isFinite(v); }

function canPoll() {
  if (window.edgeAuth && typeof window.edgeAuth.isAuthenticated === 'function') {
    return window.edgeAuth.isAuthenticated();
  }
  const gate = document.getElementById('authGate');
  return !(gate && gate.style.display !== 'none');
}

async function getJSON(url, fallback = null) {
  if (!canPoll() && !url.includes('/auth/') && !url.includes('/setup/')) {
    return fallback;
  }
  try {
    const res = await fetch(url);
    if (!res.ok) return fallback;
    return await res.json();
  } catch (e) {
    return fallback;
  }
}

/** Short message at the bottom of the screen. kind: 'ok' | 'error' | undefined. */
function showToast(msg, kind) {
  const t = el('toast');
  if (!t) return;
  t.textContent = msg;
  t.classList.toggle('toast-ok', kind === 'ok');
  t.classList.toggle('toast-error', kind === 'error');
  t.style.display = 'block';
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => { t.style.display = 'none'; }, kind === 'error' ? 6000 : 3500);
}

/**
 * Render a metric. null/undefined -> em dash with the "unobserved" style.
 */
function setMetric(id, value, { suffix = '', prefix = '', digits = null } = {}) {
  const node = el(id);
  if (!node) return;
  if (value === null || value === undefined || (typeof value === 'number' && !Number.isFinite(value))) {
    node.textContent = DASH;
    node.classList.add('metric-unobserved');
    node.title = 'Not measured yet';
    return;
  }
  node.classList.remove('metric-unobserved');
  node.title = '';
  const n = typeof value === 'number'
    ? (digits === null ? value.toLocaleString() : value.toFixed(digits))
    : value;
  node.textContent = `${prefix}${n}${suffix}`;
}

function emptyState(message, actionLabel, actionFn) {
  const btn = actionLabel
    ? ` <button type="button" class="btn btn-sm btn-primary empty-action" onclick="${actionFn}">${escapeHtml(actionLabel)}</button>`
    : '';
  return `<div class="fp-empty">${escapeHtml(message)}${btn}</div>`;
}

/** Parse an API time: naive strings are UTC, offset strings are taken as given. */
function parseApiTime(ts) {
  if (!ts) return null;
  const iso = typeof ts === 'string' && /T?\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(ts) && !/[zZ]|[+-]\d{2}:?\d{2}$/.test(ts)
    ? `${ts.replace(' ', 'T')}Z` : ts;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

function formatTimestamp(ts) {
  if (!ts) return DASH;
  const d = parseApiTime(ts);
  return d ? d.toLocaleString() : String(ts);
}

/** "just now", "5 min ago", "3 h ago", or a date for anything older than a day. */
function formatAgo(ts) {
  const d = parseApiTime(ts);
  if (!d) return DASH;
  const s = Math.round((Date.now() - d.getTime()) / 1000);
  if (s < 0) return `in ${formatDuration(-s)}`;
  if (s < 45) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return d.toLocaleDateString();
}

/** Seconds -> "24 s", "1 min 30 s", "2 h 5 min". Never "0.4m" (m reads as metres here). */
function formatDuration(seconds) {
  if (!isNum(seconds)) return DASH;
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  if (s < 3600) {
    const m = Math.floor(s / 60), r = s % 60;
    return r ? `${m} min ${r} s` : `${m} min`;
  }
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return m ? `${h} h ${m} min` : `${h} h`;
}

function theftAuthUrl(url) {
  if (!url) return url;
  return (window.edgeAuth && typeof window.edgeAuth.authUrl === 'function') ? window.edgeAuth.authUrl(url) : url;
}

/**
 * An evidence picture that cannot be loaded (file pruned by retention, moved,
 * or not reachable) is replaced by a plain note instead of a broken image.
 */
document.addEventListener('error', (e) => {
  const img = e.target;
  if (!img || img.tagName !== 'IMG') return;
  const today = img.classList.contains('today-thumb');
  if (!today && !img.classList.contains('evidence-thumb')) return;
  const note = document.createElement(today ? 'span' : 'div');
  note.className = today ? 'today-thumb today-thumb-empty' : 'evidence-thumb-empty';
  note.textContent = today ? '🚫' : 'Evidence image not available';
  note.title = 'The evidence picture could not be loaded';
  const link = img.closest('a');
  (link || img).replaceWith(note);
}, true);

/** Scroll a section into view at once (no smooth scroll, so tests can measure). */
function jumpTo(id) {
  const node = el(id);
  if (node) node.scrollIntoView({ behavior: 'instant', block: 'start' });
  return false;
}

// ==========================================
// 1. Navigation & hash routing
// ==========================================
/** Normalise any route (new, 'insights/<sub>', or an old hash) to {view, sub}. */
function resolveRoute(id) {
  let route = String(id || '').replace(/^#/, '');
  if (LEGACY_ROUTES[route]) route = LEGACY_ROUTES[route];
  let [view, sub] = route.split('/');
  if (!VIEW_SECTIONS[view]) { view = 'today'; sub = null; }
  if (view === 'insights') sub = INSIGHT_SUBS[sub] ? sub : lastInsightsSub;
  else sub = null;
  return { view, sub };
}

function switchTab(id) {
  const { view, sub } = resolveRoute(id);
  const sectionId = VIEW_SECTIONS[view];

  document.querySelectorAll('[data-tab]').forEach((btn) => {
    const on = btn.getAttribute('data-tab') === view;
    btn.classList.toggle('active', on);
    if (on) btn.setAttribute('aria-current', 'page'); else btn.removeAttribute('aria-current');
  });
  document.querySelectorAll('.tab-view').forEach((v) => {
    v.classList.toggle('active', v.id === sectionId);
  });
  if (view === 'insights') {
    Object.entries(INSIGHT_SUBS).forEach(([k, elId]) => {
      const node = el(elId);
      if (node) node.classList.toggle('active', k === sub);
    });
    document.querySelectorAll('#insightsSubNav [data-sub]').forEach((b) => {
      const on = b.getAttribute('data-sub') === sub;
      b.classList.toggle('active', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }
  currentView = view;
  currentSub = sub;
  if (sub) lastInsightsSub = sub;

  const hash = view === 'insights' && sub !== 'recs' ? `#insights/${sub}` : `#${view}`;
  if (window.location.hash !== hash) history.replaceState(null, '', hash);

  // Lets the other modules load their data on demand.
  window.dispatchEvent(new CustomEvent('edge:tab', { detail: { tab: view, sub } }));

  // The blueprint canvas is sized from its container, which has no size
  // while its tab is hidden, so it must be measured again once visible.
  if (view === 'map') fitMapHeight();
  if (view === 'map' && window.blueprintEditor) {
    setTimeout(() => {
      try { window.blueprintEditor.resize(); window.blueprintEditor.resetView(); } catch (e) { /* editor not ready */ }
    }, 60);
  }
  if (view === 'insights' && sub === 'footfall') setTimeout(initOrUpdateCharts, 50);
  if (view === 'settings') loadSettingsCards();

  // Streams are only kept open while the camera matrix is the visible view.
  attachCameraStreams();
}
window.switchTab = switchTab;

function initHashRouting() {
  switchTab(window.location.hash.replace('#', '') || 'today');
}
window.addEventListener('hashchange', () => {
  const hash = window.location.hash.replace('#', '');
  const { view, sub } = resolveRoute(hash);
  if (view !== currentView || sub !== currentSub || LEGACY_ROUTES[hash]) switchTab(hash);
});

/** Cameras are added and managed in the Store map side panel. */
function openDeviceManager() {
  if (window.calibrationTool && window.calibrationTool.isOpen()) window.calibrationTool.close();
  switchTab('map');
  setTimeout(() => {
    const card = el('deviceManagerCard');
    if (card) {
      card.scrollIntoView({ behavior: 'instant', block: 'start' });
      card.classList.add('flash-highlight');
      setTimeout(() => card.classList.remove('flash-highlight'), 1600);
    }
  }, 90);
}

/** Phones: the map is view-only until "Edit map" is pressed (desktop always edits). */
function toggleMapEditing(force) {
  const col = el('fpPlanCol');
  const btn = el('fpEditToggle');
  if (!col) return;
  const on = typeof force === 'boolean' ? force : !col.classList.contains('fp-editing');
  col.classList.toggle('fp-editing', on);
  if (btn) {
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    btn.textContent = on ? 'Done editing' : 'Edit map';
  }
  if (!on && window.blueprintEditor) {
    try { window.blueprintEditor.setTool('view'); } catch (_) { /* editor not ready */ }
  }
}

// ==========================================
// 2. Header and Settings cards (system health, sales data)
// ==========================================
/**
 * The detection engine label reports the backend that is genuinely executing
 * inference, taken from the detector itself; "Not available" when none is.
 */
async function fetchSystemTelemetry() {
  const pipe = await getJSON('/api/v1/layout/pipeline/status', null);
  const set = (id, text) => { const n = el(id); if (n) n.textContent = text; };

  if (pipe) {
    pipelineSnapshot = pipe;
    const d = pipe.detector || {};
    const inference = el('telemetryInferenceBadge');
    if (inference) {
      if (d.available) {
        inference.textContent = `${String(d.backend || '').toUpperCase()} / ${String(d.provider || d.device || '').toUpperCase()}`;
        inference.classList.remove('health-bad');
        inference.title = d.model ? `Model: ${d.model}` : '';
      } else {
        inference.textContent = 'Not available';
        inference.classList.add('health-bad');
        inference.title = d.reason || 'No inference backend is available on this machine.';
      }
    }
    set('telemetryLatencyVal', isNum(d.avg_latency_ms) && d.avg_latency_ms > 0
      ? `${d.avg_latency_ms.toFixed(0)} ms per picture` : (d.available ? 'no pictures analysed yet' : DASH));
    // "Working" is out of the cameras that are on; turned-off ones are counted apart.
    const on = pipe.cameras_online, tot = pipe.cameras_total;
    const enabled = isNum(pipe.cameras_enabled) ? pipe.cameras_enabled : tot;
    const off = isNum(pipe.cameras_off) ? pipe.cameras_off : 0;
    let subtitle = DASH;
    if (isNum(tot)) {
      if (tot === 0) subtitle = 'No cameras yet';
      else if (enabled === 0) subtitle = `All ${tot} camera${tot === 1 ? ' is' : 's are'} turned off`;
      else subtitle = `${on ?? DASH} of ${enabled} camera${enabled === 1 ? '' : 's'} working${off ? ` · ${off} turned off` : ''}${d.available ? '' : ' · detection unavailable'}`;
    }
    set('brandSubtitle', subtitle);
    updateMatrixHud(pipe);
  }
}

let systemHealthLoading = false;

/**
 * Settings > System health. Called when the view opens and every 3 s while it
 * is open. It does not wait for the pipeline request, and each
 * response is shown as soon as it arrives, so the card fills on the first
 * open rather than on a later tick. A request still in flight is not repeated.
 */
async function loadSystemHealth() {
  if (currentView !== 'settings' || systemHealthLoading) return;
  systemHealthLoading = true;
  const set = (id, text) => { const n = el(id); if (n) n.textContent = text; };
  try {
    await Promise.all([
      getJSON('/api/v1/system/hardware', {}).then((hw) => {
        // What the streaming cameras really decode with, not merely the
        // hardware present; the tooltip has the per-camera split and why.
        const inUse = hw.decoder_in_use;
        const labels = { cpu: 'SOFTWARE (CPU)', vaapi: 'GPU (VA-API)', cuda: 'GPU (NVDEC)',
                         mixed: 'GPU + SOFTWARE', none: 'NO STREAMS' };
        set('telemetryDecoderBadge', inUse ? (labels[inUse] || String(inUse).toUpperCase()) : DASH);
        const badge = el('telemetryDecoderBadge');
        if (badge) badge.title = hw.decoder_note || '';
      }),
      getJSON('/api/v1/system/stats', {}).then((stats) => {
        set('telemetryCpuVal', isNum(stats.cpu_usage_percent) ? `${stats.cpu_usage_percent.toFixed(1)} % busy` : DASH);
        set('telemetryRamVal', (isNum(stats.ram_used_gb) && isNum(stats.ram_total_gb))
          ? `${stats.ram_used_gb.toFixed(1)} of ${stats.ram_total_gb.toFixed(0)} GB` : DASH);
        set('telemetryUptimeVal', isNum(stats.uptime_seconds) ? formatDuration(stats.uptime_seconds) : DASH);
      }),
    ]);
  } finally {
    systemHealthLoading = false;
  }
}

/** Store name in the header, from the overview (STORE_NAME on the server). */
async function refreshBrand() {
  const overview = await getJSON('/api/v1/analytics/overview', null);
  const title = el('brandTitle');
  if (title && overview && overview.store_name && overview.store_name !== 'Store') {
    title.textContent = overview.store_name;
    document.title = `${overview.store_name} - Store dashboard`;
  }
}

/** Settings > Sales data: is POS data arriving, and how to connect it. */
async function loadPosCard() {
  const body = el('posStatusBody');
  const badge = el('posStatusBadge');
  if (!body) return;
  const s = await getJSON('/api/v1/analytics/pos/status', null);
  if (!s) { body.innerHTML = emptyState('Sales data status is unavailable: the server did not respond.'); return; }
  if (badge) {
    badge.textContent = s.connected ? 'CONNECTED' : 'NOT CONNECTED';
    badge.classList.toggle('metric-unobserved', !s.connected);
    badge.classList.toggle('badge-green', !!s.connected);
  }
  const ing = s.ingest || {};
  const example = ing.body_example ? JSON.stringify(ing.body_example, null, 2) : '';
  body.innerHTML = `
    <p class="ra-hint">${s.connected
      ? `Sales rows are arriving: ${Number(s.transactions_today).toLocaleString()} today, ${Number(s.transactions_total).toLocaleString()} in total. Last sale ${escapeHtml(formatAgo(s.last_transaction_at))}. Registers seen: ${escapeHtml((s.registers || []).join(', ') || DASH)}.`
      : 'No sales rows have been received. Revenue, conversion and lane figures stay empty until your till system sends them here.'}</p>
    <details class="ra-steps"${s.connected ? '' : ' open'}>
      <summary>How to connect your tills</summary>
      <ol>
        <li>Have the point-of-sale system send each sale to <code>${escapeHtml(ing.method || 'POST')} ${escapeHtml(ing.path || '')}</code>.</li>
        <li>Authentication: ${escapeHtml(ing.auth || DASH)}.</li>
        <li>${escapeHtml(ing.notes || '')}</li>
      </ol>
      ${example ? `<pre class="ra-pre">${escapeHtml(example)}</pre>` : ''}
    </details>`;
}

/** Settings > Appearance mirrors the header theme switch (theme.js owns the setting). */
function syncAppearance() {
  const mode = window.edgeTheme ? window.edgeTheme.mode() : null;
  document.querySelectorAll('[data-set-theme]').forEach((b) => {
    const on = b.getAttribute('data-set-theme') === mode;
    b.classList.toggle('btn-primary', on);
    b.setAttribute('aria-checked', on ? 'true' : 'false');
  });
}
document.addEventListener('click', (e) => {
  const b = e.target.closest && e.target.closest('[data-set-theme]');
  if (!b || !window.edgeTheme) return;
  window.edgeTheme.set(b.getAttribute('data-set-theme'));
  syncAppearance();
  showToast(`Colour theme: ${b.textContent.trim()}`, 'ok');
});
window.addEventListener('edge:theme', syncAppearance);

function loadSettingsCards() {
  loadPosCard();
  loadSystemHealth();
  syncAppearance();
}

/**
 * The store map fills the window below whatever sits above it (header, and
 * the theft banner when one is showing), so the plan never runs off-screen.
 */
function fitMapHeight() {
  const layout = el('fpLayout');
  const view = el('tab-floorplan');
  if (!layout || !view || !view.classList.contains('active')) return;
  const top = Math.round(layout.getBoundingClientRect().top + window.scrollY);
  const next = `${top + 16}px`;
  if (document.documentElement.style.getPropertyValue('--fp-offset') !== next) {
    document.documentElement.style.setProperty('--fp-offset', next);
  }
}
window.addEventListener('resize', fitMapHeight);

// ==========================================
// 3. Live camera matrix
// ==========================================
async function loadCamerasMatrix() {
  const data = await getJSON('/api/v1/cameras', null);
  if (!data) return;
  allCamerasList = data.cameras || [];

  const badge = el('matrixCamCountBadge');
  if (badge) badge.textContent = allCamerasList.length;

  buildAreaChips();
  pruneGridPins();

  // Rebuild every tile only when a camera's card text changed. Anything else
  // (a camera turned off or on, a new status) moves tiles in place, so the
  // tiles that stay keep their picture and nothing is fetched twice.
  const signature = allCamerasList.map((c) => `${c.id}|${c.name}|${c.department}|${c.location}|${c.role || ''}`).join(';');
  if (signature !== lastCameraSignature) {
    lastCameraSignature = signature;
    renderCameraGrid();
  } else {
    applyGridSlots();
  }
}
window.addEventListener('edge:cameras-changed', (e) => { if (!(e.detail && e.detail.from === 'matrix')) loadCamerasMatrix(); });

/** Filter key of a camera: its purpose (role), or 'NONE' when it has none. */
function cameraPurposeKey(cam) { return cam.role || 'NONE'; }

function cameraPurposeLabel(key) {
  if (key === 'NONE') return 'No purpose set';
  const roles = window.edgeRoles;
  return roles && typeof roles.roleLabel === 'function' ? roles.roleLabel(key) : key;
}

function buildAreaChips() {
  const host = el('cameraFilterChips');
  if (!host) return;
  // Grouped by camera purpose (role); the old department field is only a report label now.
  const departments = [...new Set(allCamerasList.map(cameraPurposeKey))].sort();
  host.querySelectorAll('.channel-pill').forEach((n) => n.remove());
  host.style.display = departments.length > 1 ? '' : 'none';
  if (!departments.length) return;
  if (!departments.includes(activeCameraFilter) && activeCameraFilter !== 'ALL') activeCameraFilter = 'ALL';
  const make = (label, value) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = `channel-pill${activeCameraFilter === value ? ' active' : ''}`;
    b.setAttribute('data-filter', value);
    b.textContent = label;
    b.onclick = () => filterCameras(value);
    host.appendChild(b);
  };
  make(`All (${allCamerasList.length})`, 'ALL');
  departments.forEach((d) => make(`${cameraPurposeLabel(d)} (${allCamerasList.filter((c) => cameraPurposeKey(c) === d).length})`, d));
}

function filterCameras(category) {
  activeCameraFilter = category;
  document.querySelectorAll('.channel-pill').forEach((pill) => {
    pill.classList.toggle('active', pill.getAttribute('data-filter') === category);
  });
  cameraGrid.offset = 0;
  cameraGrid.nextAt = 0;
  applyGridSlots();
}

function filterCamerasBySearch(query) {
  cameraGrid.search = String(query || '');
  cameraGrid.offset = 0;
  cameraGrid.nextAt = 0;
  applyGridSlots();
}

const FPS_LABEL = { 1: 'Low', 5: 'Normal', 15: 'High' };
// Pause between two pictures of one grid tile, by "Video smoothness".
const TILE_REFRESH_MS = { 1: 5000, 5: 2000, 15: 1000 };

/** Video smoothness: how often grid tiles refresh, and the enlarged tile's stream rate. */
function setDecimationFPS(fps) {
  currentDecimationFPS = fps;
  document.querySelectorAll('.fps-btn').forEach((btn) => {
    btn.classList.toggle('active', parseInt(btn.getAttribute('data-fps'), 10) === fps);
  });
  if (tileFeed.focusId) openFocusStream(tileFeed.focusId);
  pumpTiles();
  const every = (TILE_REFRESH_MS[fps] || 2000) / 1000;
  showToast(`Video smoothness: ${FPS_LABEL[fps] || `${fps} pictures per second`} (tiles every ${every} s)`, 'ok');
}

function streamUrl(cameraId) {
  const base = `/stream?camera_id=${encodeURIComponent(cameraId)}&fps=${currentDecimationFPS}&overlay=1`;
  return window.edgeAuth && window.edgeAuth.authUrl ? window.edgeAuth.authUrl(base) : base;
}

/*
 * Four tiles, rotating through the cameras.
 *
 * Even as single pictures, 33 tiles on screen meant 33 cameras decoded for
 * the browser. The view shows CAMERA_SLOTS tiles; every few seconds the
 * slots that are not pinned move on to the next cameras in order, wrapping
 * round, so every camera gets the same time on screen. Only the cameras on
 * screen are fetched (tileFeed), so at most TILE_MAX_IN_FLIGHT picture
 * requests plus one enlarged live stream use the browser's six connections.
 *
 * Pins are per slot: a pinned camera stays in its slot and the rotating
 * list is every other camera that is on (and matches the area filter and
 * search). Cameras turned off are never in it and never requested. The
 * delay, pause and pins are this viewer's preferences (localStorage, when
 * the browser allows it).
 */
const CAMERA_SLOTS = 4;
const ROTATE_MIN_S = 3;
const ROTATE_MAX_S = 600;
const ROTATE_DEFAULT_S = 10;
const GRID_PREFS_KEY = 'edge.cameraGrid.v1';
const cameraGrid = {
  delay: ROTATE_DEFAULT_S,                      // seconds on one page
  paused: false,
  pins: new Array(CAMERA_SLOTS).fill(null),     // slot -> pinned camera id
  offset: 0,          // rotating-list index shown in the first free slot
  nextAt: 0,          // when the next page is due; 0 while not counting down
  search: '',
  layout: null,       // last gridLayout()
  confirmOffline: false,
  powerHtml: null,
};

/** Turned off by the operator: no worker, no picture requests, shown as OFF. */
function cameraIsOff(cam) { return !!cam && (cam.status === 'DISABLED' || cam.is_ai_enabled === false); }

function clampRotateDelay(v) { return Math.min(ROTATE_MAX_S, Math.max(ROTATE_MIN_S, Math.round(v))); }

function loadGridPrefs() {
  let saved = null;
  try { saved = JSON.parse(window.localStorage.getItem(GRID_PREFS_KEY) || 'null'); } catch (_) { saved = null; }
  if (!saved || typeof saved !== 'object') return;
  if (Number.isFinite(Number(saved.delay))) cameraGrid.delay = clampRotateDelay(Number(saved.delay));
  cameraGrid.paused = saved.paused === true;
  if (Array.isArray(saved.pins)) {
    const seen = new Set();
    cameraGrid.pins = Array.from({ length: CAMERA_SLOTS }, (_, i) => {
      const id = saved.pins[i];
      if (typeof id !== 'string' || !id || seen.has(id)) return null;
      seen.add(id);
      return id;
    });
  }
}

function saveGridPrefs() {
  try {
    window.localStorage.setItem(GRID_PREFS_KEY, JSON.stringify(
      { delay: cameraGrid.delay, paused: cameraGrid.paused, pins: cameraGrid.pins }));
  } catch (_) { /* storage blocked: the settings last for this visit only */ }
}

/** Forget pins of cameras that were removed or turned off. */
function pruneGridPins() {
  const on = new Set(allCamerasList.filter((c) => !cameraIsOff(c)).map((c) => c.id));
  let changed = false;
  cameraGrid.pins = cameraGrid.pins.map((id) => {
    if (id && !on.has(id)) { changed = true; return null; }
    return id;
  });
  if (changed) saveGridPrefs();
}

/** Cameras that may be shown: on, in the selected area, matching the search. */
function gridPool() {
  const q = cameraGrid.search.trim().toLowerCase();
  return allCamerasList.filter((c) => !cameraIsOff(c)
    && (activeCameraFilter === 'ALL' || cameraPurposeKey(c) === activeCameraFilter)
    && (!q || `${c.name || ''} ${c.location || ''}`.toLowerCase().includes(q)));
}

/**
 * Which camera each slot shows. A pin counts only while its camera is in the
 * pool (a pin outside the area filter is kept, and that slot rotates).
 */
function gridLayout() {
  const pool = gridPool();
  const ids = pool.map((c) => c.id);
  const pins = cameraGrid.pins.map((id) => (id && ids.includes(id) ? id : null));
  const pinned = new Set(pins.filter(Boolean));
  const rotating = ids.filter((id) => !pinned.has(id));
  const freeSlots = pins.map((id, i) => (id ? -1 : i)).filter((i) => i >= 0);
  const n = rotating.length;
  cameraGrid.offset = n ? ((cameraGrid.offset % n) + n) % n : 0;
  const slots = pins.slice();
  const shown = [];
  freeSlots.forEach((slot, k) => {
    if (k >= n) return;
    const idx = (cameraGrid.offset + k) % n;
    slots[slot] = rotating[idx];
    shown.push(idx);
  });
  return { pool, pins, rotating, slots, shown, free: freeSlots.length, canRotate: freeSlots.length > 0 && n > freeSlots.length };
}

function cameraById(id) { return allCamerasList.find((c) => c.id === id) || null; }

function buildCameraCard(cam, slot) {
  const card = document.createElement('div');
  card.className = 'camera-card';
  card.setAttribute('data-cam-name', cam.name || '');
  card.setAttribute('data-cam-loc', cam.location || '');
  card.setAttribute('data-camera-card', cam.id);
  card.setAttribute('data-slot', String(slot));
  const id = escapeHtml(cam.id);
  const online = cam.status === 'ONLINE';
  card.innerHTML = `
    <div class="camera-card-header">
      <div class="camera-card-titles">
        <div class="camera-title">${escapeHtml(cam.name)}</div>
        <div class="cam-meta-text">${[cam.department && cam.department !== 'GENERAL' ? cam.department : '', cam.location || ''].filter(Boolean).map(escapeHtml).join(' · ')}</div>
      </div>
      <div class="camera-card-tools">
        <button type="button" class="cam-pin-btn" data-pin-for="${id}" aria-pressed="false" onclick="toggleCameraPin(${slot}, '${id}')"></button>
        <span class="badge ${online ? 'badge-green' : 'badge-danger'}" data-status-for="${id}"${cam.status === 'AUTH_FAILED' ? ` title="${escapeHtml(AUTH_FAILED_TIP)}"` : ''}>● ${escapeHtml(cameraStatusLabel(cam.status || 'UNKNOWN'))}</span>
      </div>
    </div>

    <div class="camera-video-container" data-focus-for="${id}" role="button" tabindex="0" aria-pressed="false"
      title="Click to enlarge and watch this camera live" onclick="focusCameraTile('${id}')"
      onkeydown="if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); focusCameraTile('${id}'); }">
      <img class="camera-img" data-camera-id="${id}" alt="${escapeHtml(cam.name)} live picture" />
      <div class="camera-overlay-top">
        <span class="cam-hud-badge ${online ? 'cam-hud-stale' : 'cam-hud-offline'}" data-live-for="${id}">${online ? '● waiting for picture' : '● ' + escapeHtml(cameraStatusLabel(cam.status))}</span>
        <span class="cam-hud-badge" data-res-for="${id}" title="Picture size">${DASH}</span>
      </div>
      <div class="camera-overlay-bottom">
        <span class="cam-hud-badge" data-hud-for="${id}" title="People the camera sees right now, and whether it is placed on the store map">${DASH}</span>
        <span class="cam-hud-badge" data-age-for="${id}" title="Age of the newest picture"></span>
      </div>
    </div>

    <!-- V2 MOUNT POINT: camera role badge / setup checklist for this camera. -->
    <div class="cam-role-slot" data-role-slot="${id}"></div>

    <div class="camera-footer">
      <button type="button" class="btn btn-sm" onclick="openCameraConfigModal('${id}')">⚙️ Settings</button>
      <button type="button" class="btn btn-sm" data-reconnect-for="${id}" onclick="reconnectCamera('${id}')" title="Try to connect to this camera now" ${online ? 'hidden' : ''}>Reconnect</button>
      <button type="button" class="btn btn-sm" data-power-for="${id}" onclick="setCameraEnabled('${id}', false)"
        title="Turn this camera off: no video, no analysis and no network traffic until it is turned on again">Turn off</button>
      <a href="/dashboard/studio?camera_id=${encodeURIComponent(cam.id)}" class="btn btn-primary btn-sm" title="Draw counting lines, shelf areas, staff-only areas and privacy masks on this camera">Camera setup</a>
    </div>`;
  return card;
}

function setCardPinned(card, pinned) {
  const btn = card.querySelector('.cam-pin-btn');
  const name = card.getAttribute('data-cam-name') || 'this camera';
  card.classList.toggle('camera-card-pinned', pinned);
  if (!btn) return;
  btn.setAttribute('aria-pressed', pinned ? 'true' : 'false');
  btn.textContent = pinned ? 'Pinned' : 'Pin';
  btn.setAttribute('aria-label', pinned ? `Unpin ${name}: its tile rotates again` : `Pin ${name} to this tile`);
  btn.title = pinned ? 'Pinned: stays here while the other tiles rotate. Click to unpin.'
    : 'Keep this camera in this tile while the other tiles rotate';
}

/** What the grid says instead of tiles, or null when it has tiles. */
function gridEmptyMessage(L) {
  if (!allCamerasList.length) {
    return { key: 'none', html: `
      <div class="matrix-empty">
        <div class="matrix-empty-title">No cameras yet</div>
        <div class="matrix-empty-text">Add a camera by its address, scan the network and USB ports, or add a Dahua
          recorder's channels. Each camera then appears here with its live picture.</div>
        <button type="button" class="btn btn-primary" onclick="openDeviceManager()">Add cameras</button>
      </div>` };
  }
  if (L.pool.length) return null;
  const off = allCamerasList.filter(cameraIsOff).length;
  if (off === allCamerasList.length) {
    return { key: 'all-off', html: `
      <div class="matrix-empty">
        <div class="matrix-empty-title">Every camera is turned off</div>
        <div class="matrix-empty-text">${off} camera${off === 1 ? ' is' : 's are'} turned off, so nothing is shown, analysed or
          fetched. Turn cameras on in the list below.</div>
      </div>` };
  }
  const q = cameraGrid.search.trim();
  const what = q ? `matching "${q}"` : `in "${cameraPurposeLabel(activeCameraFilter)}"`;
  return { key: `filter:${activeCameraFilter}:${q}`, html: emptyState(`No cameras that are on ${what}.`, 'Show all', "filterCameras('ALL'); clearCameraSearch()") };
}

function clearCameraSearch() {
  const input = el('cameraSearchInput');
  if (input) input.value = '';
  filterCamerasBySearch('');
}

/**
 * Put the right camera in each slot. A slot whose camera is unchanged is
 * left alone (its picture and request stay); a slot whose camera changed
 * gets a new tile, and the old camera's request is aborted.
 */
function applyGridSlots() {
  const host = el('cameraMatrixGrid');
  if (!host) return;
  const L = gridLayout();
  cameraGrid.layout = L;
  const message = gridEmptyMessage(L);

  if (message) {
    host.querySelectorAll('[data-slot]').forEach((card) => tileFeedUnregister(card.getAttribute('data-camera-card')));
    if (host.getAttribute('data-empty') !== message.key) {
      host.innerHTML = message.html;
      host.setAttribute('data-empty', message.key);
    }
  } else {
    if (host.hasAttribute('data-empty')) {
      host.innerHTML = '';
      host.removeAttribute('data-empty');
    }
    const cards = L.slots.map((_, slot) => host.querySelector(`[data-slot="${slot}"]`));
    const stale = cards.map((card, slot) => !!card && card.getAttribute('data-camera-card') !== L.slots[slot]);
    // Drop every outgoing camera first, so one that only moved slot is registered again below.
    cards.forEach((card, slot) => { if (stale[slot]) tileFeedUnregister(card.getAttribute('data-camera-card')); });
    L.slots.forEach((id, slot) => {
      let card = cards[slot];
      if (card && !stale[slot]) { setCardPinned(card, L.pins[slot] === id); return; }
      if (card) card.remove();
      const cam = id ? cameraById(id) : null;
      if (!cam) return;
      card = buildCameraCard(cam, slot);
      const after = [...host.querySelectorAll('[data-slot]')].find((n) => Number(n.getAttribute('data-slot')) > slot);
      host.insertBefore(card, after || null);
      setCardPinned(card, L.pins[slot] === id);
      tileFeedRegister(id, card.querySelector('img.camera-img'));
    });
  }

  renderGridControls(L);
  renderCameraPower();
  if (pipelineSnapshot) updateMatrixHud(pipelineSnapshot);
  attachCameraStreams();
}

/** Full rebuild of the tiles, after camera names or areas changed. */
function renderCameraGrid() {
  const host = el('cameraMatrixGrid');
  if (!host) return;
  const keepFocus = tileFeed.focusId;
  tileFeedReset();
  host.innerHTML = '';
  host.removeAttribute('data-empty');
  applyGridSlots();
  if (keepFocus && tileFeed.tiles.has(keepFocus)) {
    setTileFocus(keepFocus);
    attachCameraStreams();
  }
}

// ---- rotation controls

/** "5–8" or "29–30, 1–2": the rotating positions on screen, 1-based. */
function shownRangeLabel(L) {
  const runs = [];
  L.shown.forEach((idx) => {
    const last = runs[runs.length - 1];
    if (last && idx === last[1] + 1) last[1] = idx; else runs.push([idx, idx]);
  });
  return runs.map(([a, b]) => (a === b ? `${a + 1}` : `${a + 1}–${b + 1}`)).join(', ');
}

function renderGridControls(L) {
  const bar = el('gridRotationBar');
  if (!bar) return;
  // Four cameras or fewer fit on one page: nothing to rotate, no controls.
  const show = L.pool.length > CAMERA_SLOTS;
  bar.hidden = !show;
  if (!show) return;
  const blocked = !L.canRotate;
  ['gridPrevBtn', 'gridNextBtn', 'gridPauseBtn'].forEach((id) => { const b = el(id); if (b) b.disabled = blocked; });
  const pause = el('gridPauseBtn');
  if (pause) {
    pause.textContent = cameraGrid.paused ? 'Resume' : 'Pause';
    pause.setAttribute('aria-pressed', cameraGrid.paused ? 'true' : 'false');
    pause.title = cameraGrid.paused ? 'Resume rotating the cameras' : 'Stop rotating; Previous and Next still work';
  }
  const input = el('gridDelayInput');
  if (input && document.activeElement !== input && input.value !== String(cameraGrid.delay)) input.value = String(cameraGrid.delay);
  renderGridStatus();
}

function renderGridStatus() {
  const node = el('gridRotationStatus');
  const L = cameraGrid.layout;
  if (!node || !L) return;
  const pinned = CAMERA_SLOTS - L.free;
  let text = '';
  if (L.pool.length > CAMERA_SLOTS) {
    if (!L.free) {
      text = `All ${CAMERA_SLOTS} tiles are pinned, so nothing rotates. Unpin one to see the other ${L.rotating.length} camera${L.rotating.length === 1 ? '' : 's'}.`;
    } else {
      text = `Cameras ${shownRangeLabel(L)} of ${L.rotating.length}${pinned ? ` · ${pinned} pinned` : ''}`;
      if (cameraGrid.paused) text += ' · paused';
      else if (tileFeed.focusId) text += ' · paused while a camera is enlarged';
      else if (cameraGrid.nextAt) text += ` · next in ${Math.max(0, Math.ceil((cameraGrid.nextAt - Date.now()) / 1000))} s`;
    }
  }
  if (node.textContent !== text) node.textContent = text;
}

/** Previous (-1) or next (+1) page; works while paused. */
function stepCameraGrid(dir) {
  const L = cameraGrid.layout || gridLayout();
  if (!L.canRotate) return;
  const n = L.rotating.length;
  cameraGrid.offset = (((cameraGrid.offset + dir * L.free) % n) + n) % n;
  cameraGrid.nextAt = 0;             // the next page gets the full delay
  applyGridSlots();
}

function toggleCameraRotation() {
  cameraGrid.paused = !cameraGrid.paused;
  cameraGrid.nextAt = 0;
  saveGridPrefs();
  if (cameraGrid.layout) renderGridControls(cameraGrid.layout);
}

function setCameraRotationDelay(value) {
  const input = el('gridDelayInput');
  const note = el('gridDelayNote');
  const v = Number(value);
  if (String(value).trim() === '' || !Number.isFinite(v)) {
    if (input) input.value = String(cameraGrid.delay);
    if (note) note.textContent = `Enter ${ROTATE_MIN_S} to ${ROTATE_MAX_S} seconds.`;
    return;
  }
  const d = clampRotateDelay(v);
  cameraGrid.delay = d;
  if (input) input.value = String(d);
  if (note) note.textContent = d !== v ? `Kept within ${ROTATE_MIN_S} to ${ROTATE_MAX_S} seconds.` : '';
  saveGridPrefs();
  if (cameraGrid.nextAt) cameraGrid.nextAt = Date.now() + d * 1000;     // applies to the page on screen now
  renderGridStatus();
}

/** Pin the camera in this slot, or unpin it. Other tiles keep their cameras. */
function toggleCameraPin(slot, id) {
  if (cameraGrid.pins[slot] === id) {
    cameraGrid.pins[slot] = null;
    const back = gridLayout().rotating.indexOf(id);
    if (back >= 0 && back < cameraGrid.offset) cameraGrid.offset += 1;
  } else {
    const idx = (cameraGrid.layout || gridLayout()).rotating.indexOf(id);
    cameraGrid.pins = cameraGrid.pins.map((p) => (p === id ? null : p));
    cameraGrid.pins[slot] = id;
    // It leaves the rotating list; later cameras move up one place.
    if (idx >= 0 && idx < cameraGrid.offset) cameraGrid.offset -= 1;
  }
  saveGridPrefs();
  applyGridSlots();
}

/** Twice a second: advance the rotation when its time is up. */
function gridTick() {
  const L = cameraGrid.layout;
  const running = !!L && L.canRotate && !cameraGrid.paused && !tileFeed.focusId && tileFeedActive();
  if (!running) {
    cameraGrid.nextAt = 0;           // enlarged, paused or not watched: start over afterwards
  } else if (!cameraGrid.nextAt) {
    cameraGrid.nextAt = Date.now() + cameraGrid.delay * 1000;
  } else if (Date.now() >= cameraGrid.nextAt) {
    stepCameraGrid(1);
    cameraGrid.nextAt = Date.now() + cameraGrid.delay * 1000;
  }
  renderGridStatus();
}

// ---- turning cameras off and on

function cameraName(id) { const c = cameraById(id); return (c && c.name) || id; }

/** PUT the switch for one or many cameras; the tiles follow at once. */
async function setCamerasEnabled(ids, enabled) {
  ids = [...new Set(ids)];
  if (!ids.length) return false;
  const one = ids.length === 1;
  const what = one ? cameraName(ids[0]) : `${ids.length} cameras`;
  try {
    const res = await fetch(one ? `/api/v1/cameras/${encodeURIComponent(ids[0])}/enabled` : '/api/v1/cameras/enabled', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(one ? { enabled } : { camera_ids: ids, enabled }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();
    (one ? [data] : data.cameras || []).forEach((s) => {
      const cam = cameraById(s.camera_id);
      if (cam) { cam.is_ai_enabled = !!s.enabled; cam.status = s.status; }
      const pipe = ((pipelineSnapshot && pipelineSnapshot.cameras) || []).find((c) => c.camera_id === s.camera_id);
      if (pipe) { pipe.status = s.status; pipe.enabled = !!s.enabled; }
    });
    cameraGrid.confirmOffline = false;
    pruneGridPins();
    applyGridSlots();
    showToast(`${what} turned ${enabled ? 'on' : 'off'}`, 'ok');
    window.dispatchEvent(new CustomEvent('edge:cameras-changed', { detail: { from: 'matrix' } }));
    return true;
  } catch (e) {
    showToast(`Could not turn ${what} ${enabled ? 'on' : 'off'}: ${e.message}`, 'error');
    return false;
  }
}

function setCameraEnabled(id, enabled) { return setCamerasEnabled([id], enabled); }

/** On cameras that are not delivering video (unreachable or wrong password). */
function offlineCameras() {
  return allCamerasList.filter((c) => !cameraIsOff(c) && (c.status === 'OFFLINE' || c.status === 'AUTH_FAILED'));
}

function turnAllCamerasOn() { return setCamerasEnabled(allCamerasList.filter(cameraIsOff).map((c) => c.id), true); }
function askTurnOffOffline() { cameraGrid.confirmOffline = true; renderCameraPower(); const b = el('powerOfflineYes'); if (b) b.focus(); }
function cancelTurnOffOffline() { cameraGrid.confirmOffline = false; renderCameraPower(); const b = el('powerOfflineAsk'); if (b) b.focus(); }
function confirmTurnOffOffline() { return setCamerasEnabled(offlineCameras().map((c) => c.id), false); }

/**
 * Under the tiles: every camera that is off, each with its own "Turn on",
 * and the bulk actions (all on; everything offline off, confirmed inline).
 */
function renderCameraPower() {
  const host = el('cameraPowerBar');
  if (!host) return;
  const off = allCamerasList.filter(cameraIsOff);
  const offline = offlineCameras();
  if (!offline.length) cameraGrid.confirmOffline = false;
  let html = '';
  if (off.length) {
    html += `<div class="camera-off-list" role="list" aria-label="Cameras turned off">
      <span class="camera-power-label">Turned off (${off.length})</span>
      ${off.map((c) => `<span class="camera-off-chip" role="listitem">
        <span class="badge badge-neutral camera-off-badge">OFF</span>
        <span class="camera-off-name" title="${escapeHtml(c.location || c.name)}">${escapeHtml(c.name)}</span>
        <button type="button" class="btn btn-xs btn-primary" onclick="setCameraEnabled('${escapeHtml(c.id)}', true)" aria-label="Turn on ${escapeHtml(c.name)}">Turn on</button>
      </span>`).join('')}
    </div>`;
  }
  const actions = [];
  if (off.length > 1) {
    actions.push(`<button type="button" class="btn btn-sm" onclick="turnAllCamerasOn()">Turn all ${off.length} on</button>`);
  }
  if (offline.length) {
    const names = escapeHtml(offline.map((c) => c.name).join(', '));
    actions.push(cameraGrid.confirmOffline
      ? `<span class="camera-power-confirm" role="group" aria-label="Confirm turning off offline cameras">
          Turn off ${offline.length} camera${offline.length === 1 ? '' : 's'} not sending pictures? They stop costing anything until turned on.
          <button type="button" class="btn btn-sm btn-danger" id="powerOfflineYes" onclick="confirmTurnOffOffline()" title="${names}">Turn off</button>
          <button type="button" class="btn btn-sm" onclick="cancelTurnOffOffline()">Cancel</button></span>`
      : `<button type="button" class="btn btn-sm" id="powerOfflineAsk" onclick="askTurnOffOffline()" title="${names}">Turn off ${offline.length} offline camera${offline.length === 1 ? '' : 's'}</button>`);
  }
  if (actions.length) html += `<div class="camera-power-actions">${actions.join('')}</div>`;
  if (html === cameraGrid.powerHtml) return;
  cameraGrid.powerHtml = html;
  host.innerHTML = html;
  host.hidden = !html;
}

/**
 * Per-tile HUD from the pipeline in plain words: people in view, whether the
 * camera is placed on the store map, and how stale the newest frame is.
 */
function updateMatrixHud(pipe) {
  const cams = (pipe && pipe.cameras) || [];
  cams.forEach((c) => {
    const id = c.camera_id;
    const hud = document.querySelector(`[data-hud-for="${CSS.escape(id)}"]`);
    if (hud) {
      const parts = [];
      if (!c.has_frame) {
        parts.push('no picture analysed yet');
      } else if (isNum(c.live_tracks)) {
        parts.push(`${c.live_tracks} ${c.live_tracks === 1 ? 'person' : 'people'} in view`);
      } else {
        parts.push(`${DASH} people in view`);
      }
      if (typeof c.calibrated === 'boolean') parts.push(c.calibrated ? 'on the map' : 'not on the map yet');
      hud.textContent = parts.join(' · ');
      hud.title = c.has_frame && isNum(c.fps)
        ? `${c.fps.toFixed(1)} pictures per second analysed · ${c.detections_last_frame ?? DASH} detections in the last picture`
        : 'People the camera sees right now';
    }
    const age = document.querySelector(`[data-age-for="${CSS.escape(id)}"]`);
    if (age) {
      if (c.status === 'AUTH_FAILED') age.textContent = 'wrong username/password: check them in Settings';
      else if (!c.has_frame) age.textContent = c.last_error ? `no picture: ${c.last_error}` : 'no picture';
      else if (isNum(c.seconds_since_frame)) age.textContent = c.seconds_since_frame > 5 ? `picture ${c.seconds_since_frame.toFixed(0)} s old` : '';
      else age.textContent = '';
      age.title = age.textContent || 'Age of the newest picture';
    }
    renderTileLive(id);
    const status = document.querySelector(`[data-status-for="${CSS.escape(id)}"]`);
    if (status) {
      const online = c.status === 'ONLINE';
      const off = c.status === 'DISABLED';
      status.textContent = `● ${cameraStatusLabel(c.status)}`;
      status.title = c.status === 'AUTH_FAILED' ? AUTH_FAILED_TIP : (online || off ? '' : (c.last_error || ''));
      status.classList.toggle('badge-green', online);
      status.classList.toggle('badge-danger', !online && !off);
      status.classList.toggle('badge-neutral', off);
    }
    const retry = document.querySelector(`[data-reconnect-for="${CSS.escape(id)}"]`);
    if (retry) retry.hidden = c.status === 'ONLINE' || c.status === 'DISABLED';
    setResolutionBadge(id, c);
  });
}

/**
 * The tile's picture size, as measured from the camera's real frames by the
 * pipeline. The tile picture cannot be measured instead: while there is no
 * picture it shows the server's NO SIGNAL slate, whose own size is not the
 * camera's.
 */
function setResolutionBadge(id, c) {
  const badge = document.querySelector(`[data-res-for="${CSS.escape(id)}"]`);
  if (!badge) return;
  const measured = c && c.has_frame && isNum(c.frame_width) && isNum(c.frame_height);
  badge.textContent = measured ? `${c.frame_width}x${c.frame_height}` : DASH;
  badge.title = measured ? 'Picture size the camera is sending' : 'Picture size: not measured, no picture received';
}

/*
 * Camera tiles show single pictures, not streams.
 *
 * A browser opens at most 6 HTTP/1.1 connections to one server, and an MJPEG
 * <img> holds one for as long as it is shown. With one stream per tile, 33
 * cameras took every connection, so the Settings dialog, "Camera setup" and
 * every poll queued behind video that never ends. Tiles now refresh from
 * GET /api/v1/cameras/{id}/snapshot?annotate=false&overlay=1 (the tracker's
 * own boxes, no inference): only tiles on screen, at most TILE_MAX_IN_FLIGHT
 * requests at a time, and only while the Cameras view is shown in a visible
 * tab. One tile at a time can be enlarged; only that one streams live.
 *
 * A tile says LIVE only when it received a real picture recently; otherwise
 * it says how old its picture is, or that none has arrived.
 */
const TILE_MAX_IN_FLIGHT = 2;
const TILE_TIMEOUT_MS = 10000;
const TILE_LIVE_MS = 10000;          // a picture older than this is not "LIVE"
const tileFeed = {
  tiles: new Map(),    // camera id -> {id, img, visible, ctl, url, lastTry, lastFrameAt, source, error, streamHash}
  inFlight: 0,
  observer: null,
  focusId: null,       // the one enlarged tile that streams live
};

function tileFeedActive() {
  const matrix = el('tab-matrix');
  return canPoll() && !document.hidden && !!matrix && matrix.classList.contains('active');
}

function tileObserver() {
  if (!tileFeed.observer && typeof IntersectionObserver === 'function') {
    tileFeed.observer = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        const t = tileFeed.tiles.get(e.target.getAttribute('data-camera-id'));
        if (t && t.img === e.target) t.visible = e.isIntersecting;
      });
      pumpTiles();
    }, { rootMargin: '150px 0px' });
  }
  return tileFeed.observer;
}

function tileFeedRegister(id, img) {
  if (!img) return;
  const obs = tileObserver();
  tileFeed.tiles.set(id, {
    id, img, visible: !obs, ctl: null, url: null, lastTry: 0, lastFrameAt: 0, source: null, error: null, streamHash: null,
  });
  if (obs) obs.observe(img);
}

/** Drop one tile (its camera rotated out or was turned off): abort, free, forget. */
function tileFeedUnregister(id) {
  const t = tileFeed.tiles.get(id);
  if (!t) return;
  if (tileFeed.focusId === id) {
    closeFocusStream();
    tileFeed.focusId = null;
  }
  clearTimeout(t.img._retryTimer);
  if (t.ctl) t.ctl.abort();
  if (t.url) URL.revokeObjectURL(t.url);
  t.url = null;
  if (tileFeed.observer) tileFeed.observer.unobserve(t.img);
  tileFeed.tiles.delete(id);
}

/** Drop every tile: abort its request, free its picture. Before a re-render. */
function tileFeedReset() {
  closeFocusStream();
  if (tileFeed.observer) tileFeed.observer.disconnect();
  tileFeed.tiles.forEach((t) => {
    if (t.ctl) t.ctl.abort();
    if (t.url) URL.revokeObjectURL(t.url);
    t.url = null;
  });
  tileFeed.tiles.clear();
  tileFeed.focusId = null;
}

/** Stop all video traffic. Leaving the view also un-enlarges the focused tile. */
function pauseTiles(leavingView) {
  tileFeed.tiles.forEach((t) => { if (t.ctl) t.ctl.abort(); });
  closeFocusStream();
  if (leavingView && tileFeed.focusId) setTileFocus(null);
}

function pumpTiles() {
  if (!tileFeedActive()) return;
  const now = Date.now();
  const every = TILE_REFRESH_MS[currentDecimationFPS] || 2000;
  const due = [...tileFeed.tiles.values()]
    .filter((t) => t.visible && !t.ctl && t.id !== tileFeed.focusId && t.img.isConnected && now - t.lastTry >= every)
    .sort((a, b) => a.lastTry - b.lastTry);
  while (tileFeed.inFlight < TILE_MAX_IN_FLIGHT && due.length) fetchTilePicture(due.shift());
}

async function fetchTilePicture(t) {
  const ctl = new AbortController();
  t.ctl = ctl;
  t.lastTry = Date.now();
  tileFeed.inFlight += 1;
  const timer = setTimeout(() => ctl.abort(), TILE_TIMEOUT_MS);
  try {
    const url = `/api/v1/cameras/${encodeURIComponent(t.id)}/snapshot?annotate=false&overlay=1`;
    const res = await fetch(url, { signal: ctl.signal, cache: 'no-store', priority: 'low' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const source = res.headers.get('X-Frame-Source') || 'live';
    const blob = await res.blob();
    if (ctl.signal.aborted || !t.img.isConnected || t.id === tileFeed.focusId) return;
    const prev = t.url;
    t.url = URL.createObjectURL(blob);
    t.img.src = t.url;
    if (prev) URL.revokeObjectURL(prev);
    t.source = source;
    t.error = null;
    if (source === 'live') t.lastFrameAt = Date.now();
  } catch (e) {
    // An abort is a pause or a new page, not a camera fault; a timeout is.
    if (!ctl.signal.aborted || Date.now() - t.lastTry >= TILE_TIMEOUT_MS) t.error = ctl.signal.aborted ? 'no answer' : e.message;
  } finally {
    clearTimeout(timer);
    if (t.ctl === ctl) t.ctl = null;
    tileFeed.inFlight -= 1;
    renderTileLive(t.id);
    pumpTiles();
  }
}

/**
 * The LIVE badge of a tile, from what the tile really received: LIVE only
 * with a recent real picture, else its age, or why there is none.
 */
function renderTileLive(id) {
  const badge = document.querySelector(`[data-live-for="${CSS.escape(id)}"]`);
  if (!badge) return;
  const t = tileFeed.tiles.get(id);
  const pipe = ((pipelineSnapshot && pipelineSnapshot.cameras) || []).find((c) => c.camera_id === id);
  const cam = allCamerasList.find((c) => c.id === id);
  const status = pipe ? pipe.status : (cam && cam.status) || 'UNKNOWN';
  // Age of the picture shown: since it arrived, plus how old the camera's
  // newest frame already was then (a stalled camera still has a last frame).
  const camAge = pipe && pipe.has_frame && isNum(pipe.seconds_since_frame) ? pipe.seconds_since_frame : 0;
  const age = t && t.lastFrameAt ? (Date.now() - t.lastFrameAt) / 1000 + camAge : null;
  let text;
  let kind;
  let tip = '';
  if (status === 'DISABLED') {
    text = '● OFF'; kind = 'off'; tip = 'Turned off: nothing is fetched or analysed';
  } else if (status !== 'ONLINE') {
    text = `● ${cameraStatusLabel(status)}`; kind = 'offline';
  } else if (t && t.source === 'no-signal') {
    text = '● NO PICTURE'; kind = 'offline'; tip = 'The server has no picture from this camera right now';
  } else if (age !== null && age * 1000 <= TILE_LIVE_MS) {
    text = '● LIVE'; kind = 'live';
    tip = t && t.id === tileFeed.focusId ? 'Live video' : `Picture refreshed every ${(TILE_REFRESH_MS[currentDecimationFPS] || 2000) / 1000} s`;
  } else if (age !== null) {
    text = `● picture ${formatDuration(age)} old`; kind = 'stale';
    tip = t.error ? `Last refresh failed: ${t.error}` : 'Refreshes while the tile is on screen';
  } else {
    text = t && t.error ? '● no picture yet' : '● waiting for picture'; kind = 'stale';
    tip = t && t.error ? `Picture request failed: ${t.error}` : '';
  }
  if (badge.textContent !== text) badge.textContent = text;
  badge.title = tip;
  badge.classList.toggle('cam-hud-live', kind === 'live');
  badge.classList.toggle('cam-hud-offline', kind === 'offline');
  badge.classList.toggle('cam-hud-stale', kind === 'stale');
  badge.classList.toggle('cam-hud-off', kind === 'off');
}

// ---- the one enlarged, live-streaming tile

function setTileFocus(id) {
  const prev = tileFeed.focusId;
  if (prev && prev !== id) {
    closeFocusStream();
    const t = tileFeed.tiles.get(prev);
    if (t) {
      if (t.url) t.img.src = t.url; else t.img.removeAttribute('src');
      t.lastFrameAt = 0;         // the stream's age says nothing about the next picture
      t.lastTry = 0;
    }
  }
  tileFeed.focusId = id;
  document.querySelectorAll('.camera-card').forEach((card) => {
    const on = !!id && card.getAttribute('data-camera-card') === id;
    card.classList.toggle('camera-card-focus', on);
    const box = card.querySelector('[data-focus-for]');
    if (box) {
      box.setAttribute('aria-pressed', on ? 'true' : 'false');
      box.title = on ? 'Click to shrink back to the grid' : 'Click to enlarge and watch this camera live';
    }
  });
  if (prev) renderTileLive(prev);
  if (id) renderTileLive(id);
}

/** Tile click: enlarge that camera with a live stream; click again to shrink it. */
function focusCameraTile(id) {
  if (tileFeed.focusId === id) {
    setTileFocus(null);
    pumpTiles();
    return;
  }
  setTileFocus(id);
  const t = tileFeed.tiles.get(id);
  if (t && t.ctl) t.ctl.abort();
  if (tileFeedActive()) openFocusStream(id);
  const card = document.querySelector(`[data-camera-card="${CSS.escape(id)}"]`);
  if (card) card.scrollIntoView({ behavior: 'instant', block: 'nearest' });
}

function openFocusStream(id) {
  const t = tileFeed.tiles.get(id);
  if (!t) return;
  const img = t.img;
  img.onerror = () => {
    img.removeAttribute('src');
    t.error = 'live video interrupted';
    renderTileLive(id);
    clearTimeout(img._retryTimer);
    img._retryTimer = setTimeout(() => {
      if (tileFeed.focusId === id && tileFeedActive() && !img.getAttribute('src')) openFocusStream(id);
    }, 1500);
  };
  t.streamHash = null;
  img.src = `${streamUrl(id)}&_t=${Date.now()}`;
}

/** Drop the live stream (removing the src closes its connection). */
function closeFocusStream() {
  const t = tileFeed.focusId ? tileFeed.tiles.get(tileFeed.focusId) : null;
  if (!t) return;
  clearTimeout(t.img._retryTimer);
  t.img.onerror = null;
  if (/\/stream\?/.test(t.img.getAttribute('src') || '')) {
    t.img.removeAttribute('src');
    if (t.url) t.img.src = t.url;      // the last picture, until live again
  }
}

/**
 * An MJPEG <img> fires "load" only for its first picture, so the enlarged
 * tile's freshness is read from the pixels: a changed sample is a new picture.
 */
const focusSampler = document.createElement('canvas');
function sampleFocusStream() {
  const t = tileFeed.focusId ? tileFeed.tiles.get(tileFeed.focusId) : null;
  if (!t || !/\/stream\?/.test(t.img.getAttribute('src') || '') || !t.img.naturalWidth) return;
  try {
    focusSampler.width = 64; focusSampler.height = 36;
    const g = focusSampler.getContext('2d', { willReadFrequently: true });
    g.drawImage(t.img, 0, 0, 64, 36);
    const d = g.getImageData(0, 0, 64, 36).data;
    let h = 0;
    for (let i = 0; i < d.length; i += 3) h = (h * 31 + d[i]) >>> 0;
    if (h !== t.streamHash) {
      t.streamHash = h;
      t.lastFrameAt = Date.now();
      t.source = 'live';
      t.error = null;
    }
  } catch (_) { /* picture not decodable yet */ }
}

/**
 * Start or stop tile traffic for the current view and tab visibility. Called
 * on view switches, tab visibility changes and on a timer.
 */
function attachCameraStreams() {
  if (!tileFeedActive()) {
    const matrix = el('tab-matrix');
    pauseTiles(!matrix || !matrix.classList.contains('active'));
    return;
  }
  const focus = tileFeed.focusId ? tileFeed.tiles.get(tileFeed.focusId) : null;
  if (focus && !/\/stream\?/.test(focus.img.getAttribute('src') || '')) openFocusStream(focus.id);
  pumpTiles();
}
document.addEventListener('visibilitychange', attachCameraStreams);
// Leaving the page (e.g. "Camera setup"): let no picture request outlive it.
window.addEventListener('pagehide', () => pauseTiles(false));
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && tileFeed.focusId && currentView === 'cameras') { setTileFocus(null); pumpTiles(); }
});

/** Twice a second: rotate the grid when due, keep tiles due for a picture moving and their LIVE badges honest. */
function tileTick() {
  gridTick();
  if (!tileFeedActive()) return;
  sampleFocusStream();
  pumpTiles();
  tileFeed.tiles.forEach((t) => { if (t.visible) renderTileLive(t.id); });
}

// ==========================================
// 4. Visitors by hour (shared by Today and Insights > Shoppers & footfall)
// ==========================================
function chartTheme() {
  const tok = (name) => {
    try { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); } catch (_) { return ''; }
  };
  return {
    line: tok('--chart-line'), fill: tok('--chart-fill'), grid: tok('--chart-grid'), tick: tok('--chart-tick'),
    today: tok('--hourly-today'), yday: tok('--hourly-yday'), week: tok('--hourly-week'),
    nodata: tok('--hourly-nodata'), surface: tok('--hourly-surface'),
  };
}

function applyChartDefaults(ct) {
  if (typeof Chart === 'undefined' || !Chart.defaults) return;
  if (ct.tick) Chart.defaults.color = ct.tick;
  if (ct.grid) Chart.defaults.borderColor = ct.grid;
  Chart.defaults.font.family = "'Plus Jakarta Sans', system-ui, sans-serif";
}

/** Same colour at a given alpha, from #rrggbb. */
function withAlpha(hex, a) {
  const m = /^#?([0-9a-f]{6})$/i.exec(String(hex || '').trim());
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}

function hatchPattern(ctx, color) {
  const c = document.createElement('canvas');
  c.width = 8; c.height = 8;
  const g = c.getContext('2d');
  g.strokeStyle = color;
  g.lineWidth = 1.5;
  g.beginPath(); g.moveTo(0, 8); g.lineTo(8, 0); g.moveTo(-2, 2); g.lineTo(2, -2); g.moveTo(6, 10); g.lineTo(10, 6); g.stroke();
  return ctx.createPattern(c, 'repeat');
}

// "No data" hours are drawn as a hatched column behind the bars, so an hour
// the cameras were not recording can never be mistaken for a measured zero.
const hourlyNoDataPlugin = {
  id: 'hourlyNoData',
  beforeDatasetsDraw(chart) {
    const meta = chart.$hourly;
    if (!meta) return;
    const { ctx, chartArea, scales } = chart;
    const x = scales.x;
    if (!x || !chartArea) return;
    const step = meta.statuses.length > 1 ? Math.abs(x.getPixelForValue(1) - x.getPixelForValue(0)) : chartArea.width;
    ctx.save();
    ctx.fillStyle = hatchPattern(ctx, meta.nodataColor);
    meta.statuses.forEach((s, i) => {
      if (s !== 'no_data') return;
      const cx = x.getPixelForValue(i);
      ctx.fillRect(cx - step * 0.42, chartArea.top, step * 0.84, chartArea.bottom - chartArea.top);
    });
    ctx.restore();
  },
};

const hourlyCharts = {};      // canvasId -> Chart
const hourlyData = {};        // canvasId -> last API payload (for theme redraws)

function hourlyLegendHtml(ct) {
  return `
    <span class="hl-item"><span class="hl-swatch hl-bar"></span>Today</span>
    <span class="hl-item"><span class="hl-swatch hl-partial"></span>Part of the hour</span>
    <span class="hl-item"><span class="hl-swatch hl-nodata"></span>No data (not recording)</span>
    <span class="hl-item"><span class="hl-swatch hl-line hl-yday"></span>Yesterday</span>
    <span class="hl-item"><span class="hl-swatch hl-line hl-week"></span>Same day last week</span>`;
}

/**
 * Draw GET /api/v1/analytics/footfall/hourly into a canvas: bars for today
 * (hatched = no data, lighter = part of the hour, nothing = still to come),
 * lines for yesterday and the same weekday last week. Bars are keyed by
 * `index`, not `hour`, so a 23- or 25-hour DST day still lines up.
 */
function renderHourlyVisitors(ids, data) {
  const canvas = el(ids.canvas);
  const badge = ids.badge ? el(ids.badge) : null;
  const note = ids.note ? el(ids.note) : null;
  const legend = ids.legend ? el(ids.legend) : null;
  if (!canvas) return;
  hourlyData[ids.canvas] = { ids, data };

  if (!data || !data.today) {
    if (badge) { badge.textContent = DASH; badge.classList.add('metric-unobserved'); }
    if (note) note.textContent = 'Visitors by hour is unavailable: the server did not respond.';
    return;
  }
  const t = data.today;
  if (badge) {
    badge.textContent = t.source_label || DASH;
    badge.classList.toggle('metric-unobserved', !t.source);
    badge.title = 'How today\'s visitors are counted';
  }
  const ct = chartTheme();
  if (legend) legend.innerHTML = hourlyLegendHtml(ct);

  const hours = t.hours || [];
  const byIndex = (series) => {
    const map = new Map(((series && series.hours) || []).map((h) => [h.index, h]));
    return hours.map((h) => { const o = map.get(h.index); return o && isNum(o.visitors) ? o.visitors : null; });
  };
  const todayVals = hours.map((h) => (isNum(h.visitors) ? h.visitors : null));
  const colours = hours.map((h) => (h.status === 'partial' ? withAlpha(ct.today, 0.45) : ct.today));

  const measured = t.totals ? (t.totals.hours_measured || 0) + (t.totals.hours_partial || 0) : 0;
  if (note) {
    const bits = [];
    if (!measured) bits.push('No visitor counts recorded yet today. Counting starts when a camera is running with people counting on; draw an entrance counting line in Camera setup for exact numbers.');
    if (data.busiest_hour) bits.push(`Busiest so far: ${data.busiest_hour.label} with ${Number(data.busiest_hour.visitors).toLocaleString()} visitors.`);
    if (t.totals && t.totals.hours_no_data) bits.push(`${t.totals.hours_no_data} hour${t.totals.hours_no_data === 1 ? '' : 's'} striped: the cameras were not recording, so those hours are unknown, not zero.`);
    if (data.comparison && data.comparison.note) bits.push(data.comparison.note);
    note.textContent = bits.join(' ');
  }

  if (typeof Chart === 'undefined') return;
  applyChartDefaults(ct);
  const legendText = data.status_legend || {};
  const cfg = {
    type: 'bar',
    data: {
      labels: hours.map((h) => h.label),
      datasets: [
        {
          type: 'bar', label: 'Today', data: todayVals, backgroundColor: colours,
          borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: 'bottom',
          barPercentage: 0.84, categoryPercentage: 1, order: 2,
        },
        {
          type: 'line', label: 'Yesterday', data: byIndex(data.yesterday), borderColor: ct.yday, backgroundColor: ct.yday,
          borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: 0.25, spanGaps: false, order: 1,
        },
        {
          type: 'line', label: 'Same day last week', data: byIndex(data.same_weekday_last_week), borderColor: ct.week, backgroundColor: ct.week,
          borderWidth: 2, borderDash: [5, 4], pointRadius: 0, pointHoverRadius: 4, tension: 0.25, spanGaps: false, order: 1,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (item) => `${item.dataset.label}: ${isNum(item.raw) ? item.raw.toLocaleString() : 'no data'}`,
            footer: (items) => {
              const h = hours[items[0] ? items[0].dataIndex : -1];
              if (!h) return '';
              if (h.status === 'no_data') return legendText.no_data || 'No data: the cameras were not recording.';
              if (h.status === 'partial') return legendText.partial || 'Counts cover part of this hour.';
              if (h.status === 'future') return legendText.future || 'Still to come.';
              return h.in_progress ? 'This hour is still running.' : '';
            },
          },
        },
      },
      scales: {
        x: { grid: { display: false }, ticks: { color: ct.tick, maxRotation: 0, autoSkip: true, autoSkipPadding: 8, font: { size: 10 } } },
        y: { grid: { color: ct.grid }, border: { display: false }, ticks: { color: ct.tick, precision: 0, font: { size: 10 } }, beginAtZero: true },
      },
    },
    plugins: [hourlyNoDataPlugin],
  };
  const old = hourlyCharts[ids.canvas];
  if (old) {
    old.$hourly = { statuses: hours.map((h) => h.status), nodataColor: ct.nodata };
    old.data = cfg.data;
    old.options = cfg.options;
    old.update('none');
    return;
  }
  const chart = new Chart(canvas.getContext('2d'), cfg);
  chart.$hourly = { statuses: hours.map((h) => h.status), nodataColor: ct.nodata };
  chart.update('none');
  hourlyCharts[ids.canvas] = chart;
}

window.addEventListener('edge:theme', () => {
  Object.values(hourlyData).forEach(({ ids, data }) => renderHourlyVisitors(ids, data));
});

// ==========================================
// 5. Insights > Shoppers & footfall
// ==========================================
function showPanelEmptyState(hostId, message, canvasId) {
  const host = hostId ? el(hostId) : null;
  if (host) host.innerHTML = `<tr><td colspan="5">${emptyState(message)}</td></tr>`;
  if (canvasId) {
    const c = el(canvasId);
    if (c && c.parentElement) {
      let d = c.parentElement.querySelector('.panel-empty');
      if (!d) {
        d = document.createElement('div');
        d.className = 'fp-empty panel-empty';
        c.parentElement.appendChild(d);
      }
      d.textContent = message;
      c.style.display = 'none';
    }
  }
}

async function initOrUpdateCharts() {
  const [funnel, hourly] = await Promise.all([
    getJSON('/api/v1/analytics/funnels', null),
    getJSON('/api/v1/analytics/footfall/hourly', null),
  ]);
  renderHourlyVisitors({ canvas: 'hourlyFootfallChart', badge: 'hourlySourceBadge', note: 'hourlyNote', legend: 'hourlyLegend' }, hourly);

  if (!funnel) {
    const list = el('funnelStagesList');
    if (list) list.innerHTML = emptyState('Shopper journey unavailable: the analytics service did not respond.');
    return;
  }
  renderConversionFunnel(funnel.stages || []);

  const convBadge = el('funnelConvBadge');
  if (convBadge) {
    const v = funnel.conversion_rate_pct;
    convBadge.textContent = isNum(v) ? `BOUGHT: ${v}%` : `BOUGHT: ${DASH}`;
    convBadge.title = isNum(v) ? 'Share of visitors who bought something' : 'Needs sales data from the tills (Settings > Sales data)';
    convBadge.classList.toggle('metric-unobserved', !isNum(v));
  }

  const observedZones = (funnel.zones || []).filter((z) => z.observed);
  const anyEngagement = observedZones.some((z) => isNum(z.interactions) && z.interactions > 0);
  if (!observedZones.length) {
    showPanelEmptyState('frictionZonesTableBody',
      'No area activity recorded yet. Draw store areas on the Store map and calibrate a camera so visits can be counted per area.');
  } else if (!anyEngagement) {
    showPanelEmptyState('frictionZonesTableBody',
      'This ranking needs shelf reaches. Draw product shelf areas in Camera setup and turn on shelf interaction for that camera.');
  } else {
    renderFrictionZones(observedZones);
  }

  const demo = funnel.demographics;
  if (!demo || demo.available === false) {
    showPanelEmptyState(null, (demo && demo.reason) || 'Shopper profile is not measured on this system (no age or basket classifier is enabled).', 'demographicsChart');
  }
}

function renderConversionFunnel(stages) {
  const container = el('funnelStagesList');
  if (!container) return;
  if (!stages.length) {
    container.innerHTML = emptyState('No journey stages reported yet. The journey is built from tracked visits and sales data.');
    return;
  }
  const top = stages.find((s) => isNum(s.count) && s.count > 0);
  const max = top ? top.count : 1;
  container.innerHTML = stages.map((s) => {
    const observed = isNum(s.count);
    const pct = observed ? Math.max(4, (s.count / max) * 100) : 0;
    return `
      <div class="funnel-stage-row">
        <div class="funnel-stage-label">${escapeHtml(s.stage)}</div>
        <div class="funnel-bar-track"><div class="funnel-bar-fill" style="width:${pct}%"></div></div>
        <div class="funnel-stage-val ${observed ? '' : 'metric-unobserved'}">${observed ? s.count.toLocaleString() : DASH}</div>
      </div>`;
  }).join('') +
  `<div class="fp-empty">Stages showing ${DASH} were not measured. "Bought" needs sales data from the tills.</div>`;
}

function renderFrictionZones(zones) {
  const tbody = el('frictionZonesTableBody');
  if (!tbody) return;
  // Lowest engagement first: the areas where shoppers look but do not reach.
  const ranked = [...zones]
    .filter((z) => isNum(z.visits) && z.visits > 0)
    .sort((a, b) => (isNum(a.engagement_rate_pct) ? a.engagement_rate_pct : 101) - (isNum(b.engagement_rate_pct) ? b.engagement_rate_pct : 101))
    .slice(0, 5);
  if (!ranked.length) {
    showPanelEmptyState('frictionZonesTableBody', 'No area has visits yet today.');
    return;
  }
  tbody.innerHTML = ranked.map((z) => `
    <tr>
      <td>${escapeHtml(z.name)}</td>
      <td>${isNum(z.engagement_rate_pct) ? `${z.engagement_rate_pct} % of visitors` : `<span class="fp-dash">${DASH}</span>`}</td>
      <td>${isNum(z.avg_dwell_seconds) ? escapeHtml(formatDuration(z.avg_dwell_seconds)) : `<span class="fp-dash">${DASH}</span>`}</td>
      <td>${isNum(z.visits) ? z.visits.toLocaleString() : DASH}</td>
      <td><span class="fp-dash" title="Needs sales data from the tills">needs sales data</span></td>
    </tr>`).join('');
}

// ==========================================
// 6. Camera configuration modal
// ==========================================
function setCameraConfigStatus(msg, isError) {
  const s = el('cameraConfigStatus');
  if (!s) return;
  s.textContent = msg || '';
  s.classList.toggle('form-status-error', !!isError);
}

/** Department label suggestions: labels already in use (nothing invented). */
function departmentSuggestions() {
  const options = new Set();
  allCamerasList.forEach((c) => { if (c.department && c.department !== 'GENERAL') options.add(c.department); });
  return [...options].sort().map((o) => `<option value="${escapeHtml(o)}"></option>`).join('');
}

function populateDepartmentOptions(selected) {
  const input = el('configDepartment');
  if (!input) return;
  const list = el('configDeptList');
  if (list) list.innerHTML = departmentSuggestions();
  // GENERAL is the server default, shown as an empty (optional) label.
  input.value = selected && selected !== 'GENERAL' ? selected : '';
}

/** Department label as the server stores it: upper case, safe characters, GENERAL when empty. */
function normaliseDepartment(v) {
  const s = String(v || '').toUpperCase().replace(/[^A-Z0-9 _&/-]/g, '').replace(/\s+/g, ' ').trim().slice(0, 40);
  return s || 'GENERAL';
}

/** Operator-facing label for a worker status. */
function cameraStatusLabel(status) {
  if (status === 'ONLINE') return 'WORKING';
  if (status === 'AUTH_FAILED') return 'WRONG PASSWORD';
  if (status === 'DISABLED') return 'OFF';
  return status || 'OFFLINE';
}

const AUTH_FAILED_TIP = 'The camera rejected the username/password. Fix them in Settings; '
  + 'automatic retries are paused so the camera does not lock the account.';

/** Retry a camera's connection now (also after a rejected login). */
async function reconnectCamera(cameraId) {
  try {
    const res = await fetch(`/api/v1/cameras/${encodeURIComponent(cameraId)}/reconnect`, { method: 'POST' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    showToast('Reconnecting…');
  } catch (e) {
    showToast(`Reconnect failed: ${e.message}`, 'error');
  }
}

async function openCameraConfigModal(cameraId) {
  const modal = el('modalCameraConfig');
  if (!modal) return;

  let cam = allCamerasList.find((c) => c.id === cameraId);
  if (!cam) cam = await getJSON(`/api/v1/cameras/${encodeURIComponent(cameraId)}`, null);
  if (!cam) { showToast(`Camera ${cameraId} not found`); return; }
  if (!layoutSnapshot) layoutSnapshot = await getJSON('/api/v1/layout', null);

  // Placement lives on the layout entry (metres); prefer it over the feed.
  const placed = ((layoutSnapshot && layoutSnapshot.cameras) || []).find((c) => c.camera_id === cameraId) || {};

  const safeSet = (id, val) => {
    const node = el(id);
    if (node) node.value = (val !== null && val !== undefined) ? val : '';
  };
  const safeCheck = (id, boolVal) => {
    const node = el(id);
    if (node) node.checked = !!boolVal;
  };

  safeSet('configCameraId', cam.id);
  const titleEl = el('cameraModalTitle');
  if (titleEl) titleEl.textContent = `📷 ${cam.name}`;
  safeSet('configCameraName', cam.name || '');
  safeSet('configChannelNumber', isNum(cam.channel_number) ? cam.channel_number : 1);
  populateDepartmentOptions(cam.department);
  safeSet('configLocation', cam.location || '');
  safeSet('configRtspUrl', cam.rtsp_url || '');
  safeSet('configCamUser', '');
  safeSet('configCamPass', '');
  // 0 means the frame rate is not known yet; leave the field empty rather than
  // pre-filling a value the input's own min="1" rejects (which blocked Save).
  safeSet('configFps', isNum(cam.fps) && cam.fps > 0 ? cam.fps : '');
  safeSet('configResolution', cam.resolution || '');
  // The field is the value saved with the camera, which nothing measures; say
  // what the camera is really sending next to it.
  const liveRes = ((pipelineSnapshot && pipelineSnapshot.cameras) || []).find((c) => c.camera_id === cameraId);
  const measuredRes = el('configResolutionMeasured');
  if (measuredRes) {
    measuredRes.textContent = liveRes && liveRes.has_frame && isNum(liveRes.frame_width) && isNum(liveRes.frame_height)
      ? `The camera is sending ${liveRes.frame_width}x${liveRes.frame_height}.`
      : 'Not measured: no picture received from this camera yet.';
  }

  const num = (v) => (isNum(v) ? v : '');
  safeSet('configFloorX', num(placed.floor_x ?? cam.floor_x));
  safeSet('configFloorY', num(placed.floor_y ?? cam.floor_y));
  safeSet('configFloorZ', num(placed.floor_z ?? cam.floor_z));
  safeSet('configHeightZ', num(placed.floor_z ?? cam.floor_z));
  safeSet('configFovDeg', num(placed.fov_deg ?? cam.fov_deg));
  const azimuth = Math.round(isNum(placed.azimuth_deg) ? placed.azimuth_deg : (isNum(cam.azimuth_deg) ? cam.azimuth_deg : 0));
  safeSet('configAzimuthSlider', azimuth);
  const azVal = el('configAzimuthVal');
  if (azVal) azVal.textContent = `${azimuth}°`;

  const feats = cam.features || {};
  safeCheck('featPeopleCounting', feats.people_counting);
  safeCheck('featShelfInteraction', feats.shelf_interaction);
  safeCheck('featTheftDetection', feats.theft_detection);
  // Stored as a fraction (0.05-1), edited as a percentage; empty = server default.
  safeSet('configPersonMaxFrac', isNum(feats.person_max_frame_fraction)
    ? Math.round(feats.person_max_frame_fraction * 1000) / 10 : '');

  const bounds = layoutSnapshot ? ` Store is ${layoutSnapshot.width_m} × ${layoutSnapshot.height_m} m.` : '';
  const live = ((pipelineSnapshot && pipelineSnapshot.cameras) || []).find((c) => c.camera_id === cameraId);
  if (live && live.status === 'AUTH_FAILED') {
    setCameraConfigStatus('This camera rejected the saved username/password. Enter the correct ones below and Save; it reconnects straight away.', true);
  } else {
    setCameraConfigStatus(`Editing ${cam.id}.${bounds}`, false);
  }
  cancelDeleteCurrentCamera();
  modal.style.display = 'flex';
  modal.setAttribute('data-camera-object', JSON.stringify(cam));
}

function closeCameraConfigModal() {
  const modal = el('modalCameraConfig');
  if (modal) modal.style.display = 'none';
}

async function afterCameraChange() {
  lastCameraSignature = '';
  await loadCamerasMatrix();
  layoutSnapshot = await getJSON('/api/v1/layout', layoutSnapshot);
  if (window.blueprintEditor && typeof window.blueprintEditor.load === 'function') {
    try { window.blueprintEditor.load(); } catch (e) { /* editor not ready */ }
  }
  if (window.deviceManager && typeof window.deviceManager.refresh === 'function') {
    try { window.deviceManager.refresh(); } catch (e) { /* optional */ }
  }
}

async function handleCameraConfigSubmit(event) {
  event.preventDefault();
  const modal = el('modalCameraConfig');
  const camId = el('configCameraId').value;
  let existing = {};
  try { existing = JSON.parse(modal.getAttribute('data-camera-object') || '{}'); } catch (e) { existing = {}; }

  const floorX = parseFloat(el('configFloorX').value);
  const floorY = parseFloat(el('configFloorY').value);
  const floorZ = parseFloat(el('configFloorZ').value);
  const fov = parseFloat(el('configFovDeg').value);
  const azimuth = parseFloat(el('configAzimuthSlider').value);
  if (!Number.isFinite(floorX) || !Number.isFinite(floorY)) {
    setCameraConfigStatus('Floor X and Y (metres) are required.', true);
    return;
  }

  // Largest person box for this camera, typed as a percentage of the frame.
  const maxFracRaw = (el('configPersonMaxFrac') ? el('configPersonMaxFrac').value : '').trim();
  let personMaxFrac = null;
  if (maxFracRaw !== '') {
    const pct = parseFloat(maxFracRaw);
    if (!Number.isFinite(pct) || pct < 5 || pct > 100) {
      setCameraConfigStatus('Largest person size must be between 5 and 100 % of the frame, or empty for the server default.', true);
      return;
    }
    personMaxFrac = Math.round(pct * 10) / 1000;
  }

  const camUser = (el('configCamUser') ? el('configCamUser').value : '').trim();
  const camPass = el('configCamPass') ? el('configCamPass').value : '';
  if (camPass && !camUser) {
    setCameraConfigStatus('Enter the camera username together with the new password.', true);
    return;
  }

  // Only the settings the server knows; keys from older builds are not echoed back.
  const features = {
    people_counting: el('featPeopleCounting').checked,
    shelf_interaction: el('featShelfInteraction').checked,
    theft_detection: el('featTheftDetection').checked,
    person_max_frame_fraction: personMaxFrac,
  };

  // PUT replaces the whole row, so every field the API knows is sent back
  // from the object we loaded; otherwise schema defaults (floor 100 m, no
  // homography) would silently overwrite real configuration.
  const feed = Object.assign({}, existing, {
    id: camId,
    name: el('configCameraName').value.trim(),
    channel_number: parseInt(el('configChannelNumber').value, 10) || existing.channel_number || 1,
    department: normaliseDepartment(el('configDepartment').value),
    location: el('configLocation').value.trim(),
    rtsp_url: el('configRtspUrl').value.trim(),
    fps: parseInt(el('configFps').value, 10) || existing.fps,
    resolution: el('configResolution').value.trim() || existing.resolution,
    floor_x: floorX,
    floor_y: floorY,
    floor_z: Number.isFinite(floorZ) ? floorZ : existing.floor_z,
    azimuth_deg: Number.isFinite(azimuth) ? azimuth : existing.azimuth_deg,
    fov_deg: Number.isFinite(fov) ? fov : existing.fov_deg,
    features,
  });

  const saveBtn = el('btnCameraSave');
  if (saveBtn) saveBtn.disabled = true;
  setCameraConfigStatus('Saving…', false);
  try {
    const res = await fetch(`/api/v1/cameras/${encodeURIComponent(camId)}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(feed),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      setCameraConfigStatus(`Save failed (HTTP ${res.status}): ${JSON.stringify(err.detail || err)}`, true);
      return;
    }
    if (camPass) {
      // Stored encrypted server-side; the worker reconnects with it at once.
      const cres = await fetch(`/api/v1/cameras/${encodeURIComponent(camId)}/credentials`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: camUser, password: camPass }),
      });
      if (!cres.ok) {
        const err = await cres.json().catch(() => ({}));
        setCameraConfigStatus(`Camera saved, but the login was not (HTTP ${cres.status}): ${JSON.stringify(err.detail || err)}`, true);
        return;
      }
      el('configCamPass').value = '';
    }
    // Feature toggles: stored on the camera row and pushed to the running pipeline.
    const fres = await fetch(`/api/v1/cameras/${encodeURIComponent(camId)}/features`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(features),
    });
    if (!fres.ok) {
      const err = await fres.json().catch(() => ({}));
      setCameraConfigStatus(`Camera saved, but feature toggles failed (HTTP ${fres.status}): ${JSON.stringify(err.detail || err)}`, true);
      await afterCameraChange();
      return;
    }

    const placement = { floor_x: floorX, floor_y: floorY };
    if (Number.isFinite(floorZ)) placement.floor_z = floorZ;
    if (Number.isFinite(azimuth)) placement.azimuth_deg = azimuth;
    if (Number.isFinite(fov)) placement.fov_deg = fov;
    const pres = await fetch(`/api/v1/layout/cameras/${encodeURIComponent(camId)}/placement`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(placement),
    });
    if (!pres.ok) {
      const err = await pres.json().catch(() => ({}));
      setCameraConfigStatus(`Camera saved, but placement failed (HTTP ${pres.status}): ${JSON.stringify(err.detail || err)}`, true);
      await afterCameraChange();
      return;
    }
    const placed = await pres.json();
    showToast(`Saved ${feed.name} at (${placed.floor_x} m, ${placed.floor_y} m)`);
    closeCameraConfigModal();
    await afterCameraChange();
  } catch (e) {
    setCameraConfigStatus(`Save failed: ${e.message}`, true);
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
}

function askDeleteCurrentCamera() {
  const ask = el('btnCameraDeleteAsk');
  const confirmRow = el('cameraDeleteConfirm');
  if (ask) ask.style.display = 'none';
  if (confirmRow) confirmRow.style.display = 'inline-flex';
}
function cancelDeleteCurrentCamera() {
  const ask = el('btnCameraDeleteAsk');
  const confirmRow = el('cameraDeleteConfirm');
  if (ask) ask.style.display = '';
  if (confirmRow) confirmRow.style.display = 'none';
}
async function confirmDeleteCurrentCamera() {
  const camId = el('configCameraId').value;
  setCameraConfigStatus('Removing…', false);
  try {
    // The layout route also stops the camera's pipeline worker.
    let res = await fetch(`/api/v1/layout/cameras/${encodeURIComponent(camId)}`, { method: 'DELETE' });
    if (res.status === 404 || res.status === 405) {
      res = await fetch(`/api/v1/cameras/${encodeURIComponent(camId)}`, { method: 'DELETE' });
    }
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      setCameraConfigStatus(`Remove failed (HTTP ${res.status}): ${JSON.stringify(err.detail || err)}`, true);
      cancelDeleteCurrentCamera();
      return;
    }
    showToast(`Removed camera ${camId}`);
    closeCameraConfigModal();
    await afterCameraChange();
  } catch (e) {
    setCameraConfigStatus(`Remove failed: ${e.message}`, true);
    cancelDeleteCurrentCamera();
  }
}

// ==========================================
// 7. Opt-in self test (?__selftest=1)
// ==========================================
async function runSelfTest() {
  const errors = [];
  window.addEventListener('error', (e) => errors.push(`onerror: ${e.message} @ ${e.filename}:${e.lineno}`));
  window.addEventListener('unhandledrejection', (e) => errors.push(`unhandledrejection: ${e.reason && (e.reason.message || e.reason)}`));
  const origError = console.error.bind(console);
  console.error = (...args) => { errors.push(`console.error: ${args.map(String).join(' ')}`); origError(...args); };

  const out = document.createElement('pre');
  out.id = 'selftestResults';
  out.className = 'selftest-results';
  document.body.appendChild(out);
  const results = [];
  const check = (name, ok, detail) => results.push(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ` :: ${detail}` : ''}`);

  switchTab('cameras');
  // Let the first polls and the first tile pictures arrive.
  await new Promise((r) => setTimeout(r, 4500));

  const cams = allCamerasList.length;
  check('cameras loaded from /api/v1/cameras', Array.isArray(allCamerasList), `${cams} camera(s)`);
  const tiles = document.querySelectorAll('img.camera-img[data-camera-id]');
  const onScreen = (img) => { const r = img.getBoundingClientRect(); return r.bottom > 0 && r.top < window.innerHeight && r.width > 0; };
  if (cams === 0) {
    check('empty state rendered', !!document.querySelector('.matrix-empty'));
  } else {
    const shown = Math.min(CAMERA_SLOTS, gridPool().length);
    check(`${shown} tile(s): at most ${CAMERA_SLOTS}, cameras that are on only`, tiles.length === shown, `${tiles.length}/${shown}`);
    tiles.forEach((img) => {
      const r = img.getBoundingClientRect();
      const id = img.getAttribute('data-camera-id');
      check(`tile ${id} never holds a stream`, !/\/stream\?/.test(img.getAttribute('src') || ''), img.getAttribute('src'));
      check(`tile ${id} rendered height > 100`, r.height > 100, `${r.width.toFixed(0)}x${r.height.toFixed(0)}`);
      if (onScreen(img)) check(`on-screen tile ${id} has a picture`, img.naturalWidth > 0, `${img.naturalWidth}x${img.naturalHeight}`);
    });
    check('picture requests in flight within the cap', tileFeed.inFlight <= TILE_MAX_IN_FLIGHT, String(tileFeed.inFlight));
    const hud = document.querySelector('[data-hud-for]');
    check('per-tile HUD populated from pipeline', !!hud && hud.textContent.trim() !== DASH, hud && hud.textContent.trim());
    check('no fabricated HUD strings', !/Queue: 2|Dwell Alert|In: 142|Engagement: 64/.test(document.body.textContent));
  }

  // Enlarging a tile gives it the one live stream; video smoothness sets its rate.
  if (tiles.length) {
    const id = tiles[0].getAttribute('data-camera-id');
    focusCameraTile(id);
    check('enlarged tile streams live', /\/stream\?camera_id=/.test(tiles[0].getAttribute('src') || ''), tiles[0].getAttribute('src'));
    setDecimationFPS(1);
    check('video smoothness re-sets the live stream', /fps=1&/.test(tiles[0].getAttribute('src') || ''), tiles[0].getAttribute('src'));
    setDecimationFPS(5);
    check('only one stream open', [...tiles].filter((img) => /\/stream\?/.test(img.getAttribute('src') || '')).length === 1);
  }

  // Every route (old hashes included) lands on its view.
  for (const tab of VALID_TABS) {
    switchTab(tab);
    const { view, sub } = resolveRoute(tab);
    const section = document.getElementById(VIEW_SECTIONS[view]);
    const subOk = view !== 'insights' || (el(INSIGHT_SUBS[sub]) && el(INSIGHT_SUBS[sub]).classList.contains('active'));
    check(`switchTab('${tab}') -> ${view}${sub ? '/' + sub : ''}`, !!section && section.classList.contains('active') && subOk);
  }
  check('no stream and no picture request while the matrix is hidden',
    [...tiles].every((img) => !/\/stream\?/.test(img.getAttribute('src') || '')) && tileFeed.tiles.size === tiles.length
    && [...tileFeed.tiles.values()].every((t) => !t.ctl || t.ctl.signal.aborted) && !tileFeed.focusId);
  switchTab('cameras');
  await new Promise((r) => setTimeout(r, 1500));
  check('pictures refresh again on the camera view', tiles.length === 0 || [...tiles].some((img) => onScreen(img) && img.naturalWidth > 0));

  // Modal opens with metres and closes without prompt/confirm.
  if (cams) {
    await openCameraConfigModal(allCamerasList[0].id);
    const modal = el('modalCameraConfig');
    check('config modal opens', modal && modal.style.display === 'flex');
    check('config modal has metres placement', !!el('configFloorX') && !el('configHeightZ'));
    askDeleteCurrentCamera();
    check('inline delete confirm shown', el('cameraDeleteConfirm').style.display !== 'none');
    cancelDeleteCurrentCamera();
    closeCameraConfigModal();
  }

  await new Promise((r) => setTimeout(r, 800));
  check('no page errors', errors.length === 0, errors.join(' | '));
  const failed = results.filter((r) => r.startsWith('FAIL')).length;
  results.unshift(`SELFTEST ${failed === 0 ? 'OK' : 'FAILED'} (${results.length} checks, ${failed} failed)`);
  out.textContent = results.join('\n');
  window.__selftestDone = true;
}

// Expose public actions on window so inline onclick handlers and console calls always work
window.switchTab = switchTab;
window.openDeviceManager = openDeviceManager;
window.toggleMapEditing = toggleMapEditing;
window.jumpTo = jumpTo;
window.filterCameras = filterCameras;
window.filterCamerasBySearch = filterCamerasBySearch;
window.setDecimationFPS = setDecimationFPS;
window.focusCameraTile = focusCameraTile;
window.toggleCameraPin = toggleCameraPin;
window.stepCameraGrid = stepCameraGrid;
window.toggleCameraRotation = toggleCameraRotation;
window.setCameraRotationDelay = setCameraRotationDelay;
window.setCameraEnabled = setCameraEnabled;
window.setCamerasEnabled = setCamerasEnabled;
window.turnAllCamerasOn = turnAllCamerasOn;
window.askTurnOffOffline = askTurnOffOffline;
window.cancelTurnOffOffline = cancelTurnOffOffline;
window.confirmTurnOffOffline = confirmTurnOffOffline;
window.clearCameraSearch = clearCameraSearch;
window.openCameraConfigModal = openCameraConfigModal;
window.reconnectCamera = reconnectCamera;
window.closeCameraConfigModal = closeCameraConfigModal;
window.handleCameraConfigSubmit = handleCameraConfigSubmit;
window.askDeleteCurrentCamera = askDeleteCurrentCamera;
window.confirmDeleteCurrentCamera = confirmDeleteCurrentCamera;
window.cancelDeleteCurrentCamera = cancelDeleteCurrentCamera;
window.initOrUpdateCharts = initOrUpdateCharts;
window.renderHourlyVisitors = renderHourlyVisitors;

function initAnalytics() {
  loadGridPrefs();
  initHashRouting();
  fetchSystemTelemetry();
  refreshBrand();
  loadCamerasMatrix();

  setInterval(fetchSystemTelemetry, 3000);      // header count, per-tile HUD
  setInterval(loadSystemHealth, 3000);          // Settings health, only while that view is open
  setInterval(loadCamerasMatrix, 5000);         // picks up added / removed cameras
  setInterval(attachCameraStreams, 2000);
  setInterval(tileTick, 500);                   // tile pictures (visible tiles only) and LIVE badges
  setInterval(fitMapHeight, 1000);              // the theft banner can appear or go at any time
  setInterval(() => { if (currentView === 'insights' && currentSub === 'footfall' && !document.hidden) initOrUpdateCharts(); }, 60000);

  if (new URLSearchParams(window.location.search).get('__selftest') === '1') runSelfTest();
}

// Start only after auth.js has checked the stored token (edgeAuth.onReady).
if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') {
  window.edgeAuth.onReady(initAnalytics);
} else if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initAnalytics);
} else {
  initAnalytics();
}
