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
    const on = pipe.cameras_online, tot = pipe.cameras_total;
    set('brandSubtitle', isNum(tot)
      ? (tot === 0 ? 'No cameras yet' : `${on ?? DASH} of ${tot} camera${tot === 1 ? '' : 's'} working${d.available ? '' : ' · detection unavailable'}`)
      : DASH);
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
        // What capture really decodes with, not merely the hardware present.
        const inUse = hw.decoder_in_use;
        set('telemetryDecoderBadge', inUse === 'cpu' ? 'SOFTWARE (CPU)' : (inUse ? String(inUse).toUpperCase() : DASH));
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

  // Re-render only when the set of cameras changed; a re-render tears down
  // every open MJPEG connection.
  const signature = allCamerasList.map((c) => `${c.id}|${c.name}|${c.department}|${c.location}|${c.role || ''}`).join(';');
  if (signature !== lastCameraSignature) {
    lastCameraSignature = signature;
    renderCameraGrid();
  } else {
    attachCameraStreams();
  }
}

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
  renderCameraGrid();
}

function filterCamerasBySearch(query) {
  const q = (query || '').toLowerCase();
  document.querySelectorAll('.camera-card').forEach((card) => {
    const title = (card.getAttribute('data-cam-name') || '').toLowerCase();
    const loc = (card.getAttribute('data-cam-loc') || '').toLowerCase();
    card.style.display = (title.includes(q) || loc.includes(q)) ? '' : 'none';
  });
}

const FPS_LABEL = { 1: 'Low', 5: 'Normal', 15: 'High' };

/** Change the frame rate of every open tile stream by re-requesting it. */
function setDecimationFPS(fps) {
  currentDecimationFPS = fps;
  document.querySelectorAll('.fps-btn').forEach((btn) => {
    btn.classList.toggle('active', parseInt(btn.getAttribute('data-fps'), 10) === fps);
  });
  document.querySelectorAll('img.camera-img[data-camera-id]').forEach((img) => {
    if (img.getAttribute('src')) img.src = streamUrl(img.getAttribute('data-camera-id'));
  });
  showToast(`Video smoothness: ${FPS_LABEL[fps] || `${fps} pictures per second`}`, 'ok');
}

function streamUrl(cameraId) {
  const base = `/stream?camera_id=${encodeURIComponent(cameraId)}&fps=${currentDecimationFPS}&overlay=1`;
  return window.edgeAuth && window.edgeAuth.authUrl ? window.edgeAuth.authUrl(base) : base;
}

function renderCameraGrid() {
  const grid = el('cameraMatrixGrid');
  if (!grid) return;
  grid.innerHTML = '';

  if (!allCamerasList.length) {
    grid.innerHTML = `
      <div class="matrix-empty">
        <div class="matrix-empty-title">No cameras yet</div>
        <div class="matrix-empty-text">Add a camera by its address, scan the network and USB ports, or add a Dahua
          recorder's channels. Each camera then appears here with its live picture.</div>
        <button type="button" class="btn btn-primary" onclick="openDeviceManager()">Add cameras</button>
      </div>`;
    return;
  }

  const filtered = allCamerasList.filter((cam) => activeCameraFilter === 'ALL' || cameraPurposeKey(cam) === activeCameraFilter);
  if (!filtered.length) {
    grid.innerHTML = emptyState(`No cameras in "${cameraPurposeLabel(activeCameraFilter)}".`, 'Show all', "filterCameras('ALL')");
    return;
  }

  filtered.forEach((cam) => {
    const card = document.createElement('div');
    card.className = 'camera-card';
    card.setAttribute('data-cam-name', cam.name || '');
    card.setAttribute('data-cam-loc', cam.location || '');
    card.setAttribute('data-camera-card', cam.id);
    const online = cam.status === 'ONLINE';
    card.innerHTML = `
      <div class="camera-card-header">
        <div class="camera-card-titles">
          <div class="camera-title">${escapeHtml(cam.name)}</div>
          <div class="cam-meta-text">${[cam.department && cam.department !== 'GENERAL' ? cam.department : '', cam.location || ''].filter(Boolean).map(escapeHtml).join(' · ')}</div>
        </div>
        <span class="badge ${online ? 'badge-green' : 'badge-danger'}" data-status-for="${escapeHtml(cam.id)}"${cam.status === 'AUTH_FAILED' ? ` title="${escapeHtml(AUTH_FAILED_TIP)}"` : ''}>● ${escapeHtml(cameraStatusLabel(cam.status || 'UNKNOWN'))}</span>
      </div>

      <div class="camera-video-container">
        <img class="camera-img" data-camera-id="${escapeHtml(cam.id)}" alt="${escapeHtml(cam.name)} live picture" />
        <div class="camera-overlay-top">
          <span class="cam-hud-badge ${online ? 'cam-hud-live' : 'cam-hud-offline'}" data-live-for="${escapeHtml(cam.id)}">${online ? '● LIVE' : '● ' + escapeHtml(cameraStatusLabel(cam.status))}</span>
          <span class="cam-hud-badge" data-res-for="${escapeHtml(cam.id)}" title="Picture size">${DASH}</span>
        </div>
        <div class="camera-overlay-bottom">
          <span class="cam-hud-badge" data-hud-for="${escapeHtml(cam.id)}" title="People the camera sees right now, and whether it is placed on the store map">${DASH}</span>
          <span class="cam-hud-badge" data-age-for="${escapeHtml(cam.id)}" title="Age of the newest picture"></span>
        </div>
      </div>

      <!-- V2 MOUNT POINT: camera role badge / setup checklist for this camera. -->
      <div class="cam-role-slot" data-role-slot="${escapeHtml(cam.id)}"></div>

      <div class="camera-footer">
        <button type="button" class="btn btn-sm" onclick="openCameraConfigModal('${escapeHtml(cam.id)}')">⚙️ Settings</button>
        <button type="button" class="btn btn-sm" data-reconnect-for="${escapeHtml(cam.id)}" onclick="reconnectCamera('${escapeHtml(cam.id)}')" title="Try to connect to this camera now" ${online ? 'hidden' : ''}>Reconnect</button>
        <a href="/dashboard/studio?camera_id=${encodeURIComponent(cam.id)}" class="btn btn-primary btn-sm" title="Draw counting lines, shelf areas, staff-only areas and privacy masks on this camera">Camera setup</a>
      </div>`;
    grid.appendChild(card);
  });

  if (pipelineSnapshot) updateMatrixHud(pipelineSnapshot);
  attachCameraStreams();
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
    const live = document.querySelector(`[data-live-for="${CSS.escape(id)}"]`);
    if (live) {
      const online = c.status === 'ONLINE';
      live.textContent = online ? '● LIVE' : `● ${cameraStatusLabel(c.status)}`;
      live.classList.toggle('cam-hud-live', online);
      live.classList.toggle('cam-hud-offline', !online);
    }
    const status = document.querySelector(`[data-status-for="${CSS.escape(id)}"]`);
    if (status) {
      const online = c.status === 'ONLINE';
      status.textContent = `● ${cameraStatusLabel(c.status)}`;
      status.title = c.status === 'AUTH_FAILED' ? AUTH_FAILED_TIP : (online ? '' : (c.last_error || ''));
      status.classList.toggle('badge-green', online);
      status.classList.toggle('badge-danger', !online);
    }
    const retry = document.querySelector(`[data-reconnect-for="${CSS.escape(id)}"]`);
    if (retry) retry.hidden = c.status === 'ONLINE';
    setResolutionBadge(id, c);
  });
}

/**
 * The tile's picture size, as measured from the camera's real frames by the
 * pipeline. The stream <img> cannot be measured instead: while there is no
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

/**
 * Attach a live MJPEG stream (with the detector's real boxes drawn) to each
 * tile while the matrix is visible; drop it otherwise, because an <img>
 * with an MJPEG source keeps decoding forever and every open stream costs a
 * JPEG encode on the edge box.
 */
function attachCameraStreams() {
  if (!canPoll()) return;
  const matrix = el('tab-matrix');
  const matrixVisible = !document.hidden && !!matrix && matrix.classList.contains('active');

  document.querySelectorAll('img.camera-img[data-camera-id]').forEach((img) => {
    const id = img.getAttribute('data-camera-id');
    if (!matrixVisible) {
      if (img.getAttribute('src')) img.removeAttribute('src');
      clearTimeout(img._retryTimer);
      return;
    }
    if (!img.getAttribute('src')) {
      img.onload = () => {
        const live = ((pipelineSnapshot && pipelineSnapshot.cameras) || []).find((c) => c.camera_id === id);
        setResolutionBadge(id, live);
      };
      img.onerror = () => {
        const badge = document.querySelector(`[data-res-for="${CSS.escape(id)}"]`);
        if (badge) badge.textContent = 'reconnecting…';
        img.removeAttribute('src');
        clearTimeout(img._retryTimer);
        img._retryTimer = setTimeout(() => {
          const m = el('tab-matrix');
          if (!document.hidden && m && m.classList.contains('active') && !img.getAttribute('src')) {
            img.src = `${streamUrl(id)}&_t=${Date.now()}`;
          }
        }, 1500);
      };
      img.src = streamUrl(id);
    }
  });
}
document.addEventListener('visibilitychange', attachCameraStreams);

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
  // Let the first polls, the stream attach and the first MJPEG frame arrive.
  await new Promise((r) => setTimeout(r, 4500));

  const cams = allCamerasList.length;
  check('cameras loaded from /api/v1/cameras', Array.isArray(allCamerasList), `${cams} camera(s)`);
  const tiles = document.querySelectorAll('img.camera-img[data-camera-id]');
  if (cams === 0) {
    check('empty state rendered', !!document.querySelector('.matrix-empty'));
  } else {
    check('one tile per camera', tiles.length === cams, `${tiles.length}/${cams}`);
    tiles.forEach((img) => {
      const r = img.getBoundingClientRect();
      const id = img.getAttribute('data-camera-id');
      check(`tile ${id} has src`, /\/stream\?camera_id=/.test(img.getAttribute('src') || ''), img.getAttribute('src'));
      check(`tile ${id} rendered height > 100`, r.height > 100, `${r.width.toFixed(0)}x${r.height.toFixed(0)}`);
      check(`tile ${id} naturalWidth > 0`, img.naturalWidth > 0, `${img.naturalWidth}x${img.naturalHeight}`);
    });
    const hud = document.querySelector('[data-hud-for]');
    check('per-tile HUD populated from pipeline', !!hud && hud.textContent.trim() !== DASH, hud && hud.textContent.trim());
    check('no fabricated HUD strings', !/Queue: 2|Dwell Alert|In: 142|Engagement: 64/.test(document.body.textContent));
  }

  // Video smoothness really changes the stream URL.
  if (tiles.length) {
    setDecimationFPS(1);
    check('video smoothness re-sets src', /fps=1&/.test(tiles[0].getAttribute('src') || ''), tiles[0].getAttribute('src'));
    setDecimationFPS(5);
  }

  // Every route (old hashes included) lands on its view.
  for (const tab of VALID_TABS) {
    switchTab(tab);
    const { view, sub } = resolveRoute(tab);
    const section = document.getElementById(VIEW_SECTIONS[view]);
    const subOk = view !== 'insights' || (el(INSIGHT_SUBS[sub]) && el(INSIGHT_SUBS[sub]).classList.contains('active'));
    check(`switchTab('${tab}') -> ${view}${sub ? '/' + sub : ''}`, !!section && section.classList.contains('active') && subOk);
  }
  check('streams detached when matrix hidden', [...tiles].every((img) => !img.getAttribute('src')));
  switchTab('cameras');
  check('streams re-attached on the camera view', tiles.length === 0 || [...tiles].every((img) => !!img.getAttribute('src')));

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
  initHashRouting();
  fetchSystemTelemetry();
  refreshBrand();
  loadCamerasMatrix();

  setInterval(fetchSystemTelemetry, 3000);      // header count, per-tile HUD
  setInterval(loadSystemHealth, 3000);          // Settings health, only while that view is open
  setInterval(loadCamerasMatrix, 5000);         // picks up added / removed cameras
  setInterval(attachCameraStreams, 2000);
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
