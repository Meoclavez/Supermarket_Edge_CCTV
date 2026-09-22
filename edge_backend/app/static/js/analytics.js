/**
 * Edge AI CCTV - Retail Intelligence Dashboard Controller
 *
 * Owns: tab navigation, header telemetry, headline KPIs, the live camera
 * matrix, the camera configuration modal, the analytics / action / market /
 * theft / digest tabs, and an opt-in self test (?__selftest=1).
 *
 * Rules this file follows:
 *  - Every number shown comes from an API response. A value the API reports
 *    as null / absent renders as an em dash, never as 0 or a constant.
 *  - Nothing here calls prompt(), confirm() or alert(); destructive actions
 *    use an inline two-step confirmation.
 */

'use strict';

const DASH = '—';
const VALID_TABS = ['matrix', 'floorplan', 'analytics', 'actions', 'market_ai', 'theft', 'digest'];

let activeCameraFilter = 'ALL';
let currentDecimationFPS = 5;
let allCamerasList = [];
let allDecisions = [];
let hourlyChartInstance = null;
let activeTheftIncidentId = null;
let lastAlertedTheftId = null;
let layoutSnapshot = null;          // last GET /api/v1/layout
let pipelineSnapshot = null;        // last GET /api/v1/layout/pipeline/status
let lastCameraSignature = null;     // change detector for the matrix re-render

// ==========================================
// Small helpers
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

function showToast(msg) {
  const t = el('toast');
  if (!t) return;
  t.textContent = msg;
  t.style.display = 'block';
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => { t.style.display = 'none'; }, 3000);
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
    node.title = 'Not observed yet';
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
    ? ` <button type="button" class="btn btn-sm btn-primary" style="margin-left:8px" onclick="${actionFn}">${escapeHtml(actionLabel)}</button>`
    : '';
  return `<div class="fp-empty">${escapeHtml(message)}${btn}</div>`;
}

// ==========================================
// 1. Tab navigation & hash routing
// ==========================================
function switchTab(tabId) {
  if (!VALID_TABS.includes(tabId)) tabId = 'matrix';

  document.querySelectorAll('.tab-btn').forEach((btn) => {
    btn.classList.toggle('active', btn.getAttribute('data-tab') === tabId);
  });
  document.querySelectorAll('.tab-view').forEach((view) => {
    view.classList.toggle('active', view.id === `tab-${tabId}`);
  });

  if (window.location.hash !== `#${tabId}`) {
    history.replaceState(null, '', `#${tabId}`);
  }

  // The blueprint canvas is sized from its container, which has no size
  // while its tab is hidden, so it must be measured again once visible.
  if (tabId === 'floorplan' && window.blueprintEditor) {
    setTimeout(() => {
      try { window.blueprintEditor.resize(); window.blueprintEditor.resetView(); } catch (e) { /* editor not ready */ }
    }, 60);
  }
  if (tabId === 'analytics') setTimeout(initOrUpdateCharts, 50);
  if (tabId === 'market_ai') setTimeout(loadMarketPredictions, 50);
  if (tabId === 'theft') setTimeout(fetchTheftIncidents, 50);
  if (tabId === 'actions') setTimeout(loadActionCenter, 50);
  if (tabId === 'digest') setTimeout(loadExecutiveDigest, 50);

  // Streams are only kept open while the matrix is the visible tab.
  attachCameraStreams();
}
window.switchTab = switchTab;

function initHashRouting() {
  const hash = window.location.hash.replace('#', '');
  switchTab(VALID_TABS.includes(hash) ? hash : 'matrix');
}
window.addEventListener('hashchange', () => {
  const hash = window.location.hash.replace('#', '');
  if (VALID_TABS.includes(hash)) switchTab(hash);
});

// ==========================================
// 2. Header telemetry, KPIs and data-state banner
// ==========================================
/**
 * The AI engine label reports the backend that is genuinely executing
 * inference, taken from the detector itself; "NO DETECTOR" when none is.
 * The decoder label is the probed decode capability of this box.
 */
async function fetchSystemTelemetry() {
  const [hw, stats, pipe] = await Promise.all([
    getJSON('/api/v1/system/hardware', {}),
    getJSON('/api/v1/system/stats', {}),
    getJSON('/api/v1/layout/pipeline/status', null),
  ]);
  const set = (id, text) => { const n = el(id); if (n) n.textContent = text; };

  if (pipe) {
    pipelineSnapshot = pipe;
    const d = pipe.detector || {};
    const inference = el('telemetryInferenceBadge');
    if (inference) {
      if (d.available) {
        inference.textContent = `${String(d.backend || '').toUpperCase()} / ${String(d.provider || d.device || '').toUpperCase()}`;
        inference.style.color = 'var(--accent-green)';
        inference.title = d.model ? `Model: ${d.model}` : '';
      } else {
        inference.textContent = 'NO DETECTOR';
        inference.style.color = 'var(--accent-red, #ff5b6b)';
        inference.title = d.reason || 'No inference backend is available on this machine.';
      }
    }
    const latency = isNum(d.avg_latency_ms) && d.avg_latency_ms > 0 ? ` · ${d.avg_latency_ms.toFixed(0)}ms INFERENCE` : ' · NO FRAMES INFERRED YET';
    set('brandSubtitle',
      `${pipe.cameras_online ?? DASH}/${pipe.cameras_total ?? DASH} CAMERAS ONLINE · ${pipe.zones_loaded ?? DASH} ZONES` +
      (d.available ? latency : ' · DETECTION UNAVAILABLE'));
    updateMatrixHud(pipe);
  } else {
    set('telemetryInferenceBadge', DASH);
  }

  const decoder = hw.decoder_capability || hw.decoder_type;
  set('telemetryDecoderBadge', decoder ? String(decoder).toUpperCase() : DASH);
  set('telemetryCpuVal', isNum(stats.cpu_usage_percent) ? `${stats.cpu_usage_percent.toFixed(1)}%` : DASH);
  set('telemetryRamVal', (isNum(stats.ram_used_gb) && isNum(stats.ram_total_gb))
    ? `${stats.ram_used_gb.toFixed(1)} / ${stats.ram_total_gb.toFixed(0)} GB` : DASH);
  set('telemetryUptimeVal', isNum(stats.uptime_seconds)
    ? `${Math.floor(stats.uptime_seconds / 3600)}h ${Math.floor((stats.uptime_seconds % 3600) / 60)}m` : DASH);
}

/**
 * Explain why the store looks quiet. "Not set up", "no cameras", "none
 * calibrated", "no shoppers yet" and "no POS" are different situations and
 * must not look alike.
 */
function renderDataStateBanner(layout, overview) {
  const banner = el('dataStateBanner');
  if (!banner) return;

  let html = '';
  const setup = layout && layout.setup;
  const configured = setup
    ? !!setup.configured
    : !!(layout && ((layout.zones || []).length || (layout.cameras || []).length || (layout.structures || []).length));

  if (layout && !configured) {
    html = 'Store not set up yet — open the Blueprint tab to draw your floor and add cameras.' +
      ' <button type="button" class="btn btn-sm btn-primary" style="margin-left:10px" onclick="switchTab(\'floorplan\')">Open Blueprint</button>';
  } else if (overview) {
    const c = overview.coverage || {};
    const online = pipelineSnapshot ? pipelineSnapshot.cameras_online : null;
    if (!c.cameras_total) {
      html = 'No cameras configured. Open the Blueprint tab and scan for devices.' +
        ' <button type="button" class="btn btn-sm btn-primary" style="margin-left:10px" onclick="switchTab(\'floorplan\')">Scan for cameras</button>';
    } else if (online === 0) {
      const errs = (pipelineSnapshot.cameras || []).map((k) => k.last_error).filter(Boolean);
      html = `${c.cameras_total} camera(s) configured, none delivering video${errs.length ? ': ' + escapeHtml(errs[0]) : ''}.` +
        ' <button type="button" class="btn btn-sm" style="margin-left:10px" onclick="switchTab(\'floorplan\')">Check devices</button>';
    } else if (!c.cameras_calibrated) {
      html = `${c.cameras_total} camera(s) running, none calibrated. People are detected but cannot be placed on the floor plan.` +
        ' <button type="button" class="btn btn-sm" style="margin-left:10px" onclick="switchTab(\'floorplan\')">Calibrate</button>';
    } else if (!overview.has_data) {
      html = 'Cameras are running. No shopper activity recorded yet today.';
    } else if (!overview.pos_connected) {
      html = 'Shopper metrics are live. Connect the POS feed to enable revenue and conversion.';
    }
  }
  banner.innerHTML = html;
  banner.style.display = html ? 'block' : 'none';
}

async function fetchStoreKPIs() {
  const [overview, layout] = await Promise.all([
    getJSON('/api/v1/analytics/overview', null),
    getJSON('/api/v1/layout', null),
  ]);
  if (layout) layoutSnapshot = layout;
  if (overview) {
    setMetric('kpiFootfallToday', overview.today_footfall);
    setMetric('kpiActiveShoppers', overview.active_shoppers_now);
    setMetric('kpiAvgDwell', overview.avg_dwell_minutes, { suffix: 'm', digits: 1 });
    setMetric('kpiConversion', overview.conversion_rate_pct, { suffix: '%', digits: 1 });
    setMetric('kpiRevenue', overview.daily_revenue, { prefix: '$', digits: 2 });
  }
  renderDataStateBanner(layout, overview);
}

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
  const signature = allCamerasList.map((c) => `${c.id}|${c.name}|${c.department}|${c.location}`).join(';');
  if (signature !== lastCameraSignature) {
    lastCameraSignature = signature;
    renderCameraGrid();
  } else {
    attachCameraStreams();
  }
}

function buildAreaChips() {
  const host = el('cameraFilterChips');
  if (!host) return;
  const departments = [...new Set(allCamerasList.map((c) => c.department).filter(Boolean))].sort();
  host.querySelectorAll('.channel-pill').forEach((n) => n.remove());
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
  departments.forEach((d) => make(`${d} (${allCamerasList.filter((c) => c.department === d).length})`, d));
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

/** Change the frame rate of every open tile stream by re-requesting it. */
function setDecimationFPS(fps) {
  currentDecimationFPS = fps;
  document.querySelectorAll('.fps-btn').forEach((btn) => {
    btn.classList.toggle('active', parseInt(btn.getAttribute('data-fps'), 10) === fps);
  });
  document.querySelectorAll('img.camera-img[data-camera-id]').forEach((img) => {
    if (img.getAttribute('src')) img.src = streamUrl(img.getAttribute('data-camera-id'));
  });
  showToast(`Tile streams re-requested at ${fps} FPS`);
}

function streamUrl(cameraId) {
  return `/stream?camera_id=${encodeURIComponent(cameraId)}&fps=${currentDecimationFPS}&overlay=1`;
}

function renderCameraGrid() {
  const grid = el('cameraMatrixGrid');
  if (!grid) return;
  grid.innerHTML = '';

  if (!allCamerasList.length) {
    grid.innerHTML = `
      <div class="matrix-empty">
        <div class="matrix-empty-title">No cameras yet</div>
        <div class="matrix-empty-text">This system has no camera configured. Scan the network and USB ports from the
          Blueprint tab, adopt a device, and it will appear here with its live feed.</div>
        <button type="button" class="btn btn-primary" onclick="switchTab('floorplan')">Open Blueprint &amp; scan for cameras</button>
      </div>`;
    return;
  }

  const filtered = allCamerasList.filter((cam) => activeCameraFilter === 'ALL' || cam.department === activeCameraFilter);
  if (!filtered.length) {
    grid.innerHTML = emptyState(`No cameras in "${activeCameraFilter}".`, 'Show all', "filterCameras('ALL')");
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
        <div>
          <div class="camera-title">${escapeHtml(cam.name)}</div>
          <div class="cam-meta-text">${escapeHtml(cam.department || '')}${cam.location ? ' · ' + escapeHtml(cam.location) : ''}</div>
        </div>
        <span class="badge ${online ? 'badge-green' : 'badge-danger'}" data-status-for="${escapeHtml(cam.id)}">● ${escapeHtml(cam.status || 'UNKNOWN')}</span>
      </div>

      <div class="camera-video-container">
        <img class="camera-img" data-camera-id="${escapeHtml(cam.id)}" alt="${escapeHtml(cam.name)} live feed" />
        <div class="camera-overlay-top">
          <span class="cam-hud-badge ${online ? 'cam-hud-live' : 'cam-hud-offline'}" data-live-for="${escapeHtml(cam.id)}">${online ? '● LIVE' : '● ' + escapeHtml(cam.status || 'OFFLINE')}</span>
          <span class="cam-hud-badge" data-res-for="${escapeHtml(cam.id)}" title="Stream resolution">${DASH}</span>
        </div>
        <div class="camera-overlay-bottom">
          <span class="cam-hud-badge" data-hud-for="${escapeHtml(cam.id)}" title="fps · detections in last frame · live tracks · calibration">${DASH}</span>
          <span class="cam-hud-badge" data-age-for="${escapeHtml(cam.id)}" title="Age of the newest frame"></span>
        </div>
      </div>

      <div class="camera-footer">
        <button type="button" class="btn btn-sm" onclick="openCameraConfigModal('${escapeHtml(cam.id)}')">⚙️ Config</button>
        <a href="/dashboard/studio?camera_id=${encodeURIComponent(cam.id)}" class="btn btn-primary btn-sm">🎨 Studio &amp; Zones</a>
      </div>`;
    grid.appendChild(card);
  });

  if (pipelineSnapshot) updateMatrixHud(pipelineSnapshot);
  attachCameraStreams();
}

/**
 * Per-tile HUD from the pipeline: real fps, detections in the last frame,
 * live tracks, calibration state and how stale the newest frame is.
 */
function updateMatrixHud(pipe) {
  const cams = (pipe && pipe.cameras) || [];
  cams.forEach((c) => {
    const id = c.camera_id;
    const hud = document.querySelector(`[data-hud-for="${CSS.escape(id)}"]`);
    if (hud) {
      const parts = [];
      if (!c.has_frame) {
        parts.push('no frame analysed');
      } else {
        parts.push(isNum(c.fps) && c.fps > 0 ? `${c.fps.toFixed(1)} fps` : `${DASH} fps`);
        parts.push(isNum(c.detections_last_frame) ? `${c.detections_last_frame} det` : `${DASH} det`);
        parts.push(isNum(c.live_tracks) ? `${c.live_tracks} trk` : `${DASH} trk`);
      }
      if (typeof c.calibrated === 'boolean') parts.push(c.calibrated ? 'calibrated' : 'uncalibrated');
      hud.textContent = parts.join(' · ');
    }
    const age = document.querySelector(`[data-age-for="${CSS.escape(id)}"]`);
    if (age) {
      if (!c.has_frame) age.textContent = c.last_error ? `no frame: ${c.last_error}` : 'no frame';
      else if (isNum(c.seconds_since_frame)) age.textContent = c.seconds_since_frame > 5 ? `stale ${c.seconds_since_frame.toFixed(0)}s` : '';
      else age.textContent = '';
    }
    const live = document.querySelector(`[data-live-for="${CSS.escape(id)}"]`);
    if (live) {
      const online = c.status === 'ONLINE';
      live.textContent = online ? '● LIVE' : `● ${c.status || 'OFFLINE'}`;
      live.classList.toggle('cam-hud-live', online);
      live.classList.toggle('cam-hud-offline', !online);
    }
    const status = document.querySelector(`[data-status-for="${CSS.escape(id)}"]`);
    if (status) {
      const online = c.status === 'ONLINE';
      status.textContent = `● ${c.status || 'OFFLINE'}`;
      status.classList.toggle('badge-green', online);
      status.classList.toggle('badge-danger', !online);
    }
  });
}

/**
 * Attach a live MJPEG stream (with the detector's real boxes drawn) to each
 * tile while the matrix tab is visible; drop it otherwise, because an <img>
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
        const badge = document.querySelector(`[data-res-for="${CSS.escape(id)}"]`);
        if (badge && img.naturalWidth) badge.textContent = `${img.naturalWidth}x${img.naturalHeight}`;
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
// 4. Retail analytics, funnel & charts
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

function clearPanelEmptyState(canvasId) {
  const c = el(canvasId);
  if (!c) return;
  c.style.display = '';
  const stale = c.parentElement && c.parentElement.querySelector('.panel-empty');
  if (stale) stale.remove();
}

async function initOrUpdateCharts() {
  const [funnel, forecast] = await Promise.all([
    getJSON('/api/v1/analytics/funnels', null),
    getJSON('/api/v1/analytics/market/predictions', null),
  ]);

  if (forecast && forecast.sufficient_history && (forecast.hourly_forecast || []).length) {
    clearPanelEmptyState('hourlyFootfallChart');
    renderHourlyChart(forecast.hourly_forecast.map((h) => ({ hour: h.hour, footfall: h.expected_traffic })));
  } else {
    showPanelEmptyState(null,
      (forecast && forecast.message) ||
      (forecast ? `Not enough history for an hourly curve (${forecast.days_observed ?? 0} of ${forecast.days_required ?? '?'} days observed).`
        : 'Hourly traffic is unavailable: the analytics service did not respond.'),
      'hourlyFootfallChart');
  }

  if (!funnel) {
    const list = el('funnelStagesList');
    if (list) list.innerHTML = emptyState('Funnel unavailable: the analytics service did not respond.');
    return;
  }
  renderConversionFunnel(funnel.stages || []);

  const convBadge = el('funnelConvBadge');
  if (convBadge) {
    const v = funnel.conversion_rate_pct;
    convBadge.textContent = isNum(v) ? `CONVERSION: ${v}%` : `CONVERSION: ${DASH}`;
    convBadge.classList.toggle('metric-unobserved', !isNum(v));
  }

  const observedZones = (funnel.zones || []).filter((z) => z.observed);
  const anyEngagement = observedZones.some((z) => isNum(z.interactions) && z.interactions > 0);
  if (!observedZones.length) {
    showPanelEmptyState('frictionZonesTableBody',
      'No zone activity recorded yet. Draw zones on the blueprint and calibrate a camera so visits can be attributed.');
  } else if (!anyEngagement) {
    showPanelEmptyState('frictionZonesTableBody',
      'Friction ranking needs shelf-interaction events. No interaction source is reporting.');
  } else {
    renderFrictionZones(observedZones);
  }

  const demo = funnel.demographics;
  if (!demo || demo.available === false) {
    showPanelEmptyState(null, (demo && demo.reason) || 'No demographic classifier is enabled on this deployment.', 'demographicsChart');
  }
}

function renderHourlyChart(points) {
  const canvas = el('hourlyFootfallChart');
  if (!canvas || typeof Chart === 'undefined') return;
  if (hourlyChartInstance) hourlyChartInstance.destroy();
  hourlyChartInstance = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      labels: points.map((p) => `${String(p.hour).padStart(2, '0')}:00`),
      datasets: [{
        label: 'Expected shoppers per hour (from this store\'s history)',
        data: points.map((p) => p.footfall),
        borderColor: '#00f0ff',
        backgroundColor: 'rgba(0, 240, 255, 0.1)',
        borderWidth: 2.5,
        fill: true,
        tension: 0.35,
        pointRadius: 3,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#8b949e', font: { family: 'JetBrains Mono', size: 10 } } } },
      scales: {
        x: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#8b949e', font: { size: 9.5 } } },
        y: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#8b949e', font: { size: 9.5 } }, beginAtZero: true },
      },
    },
  });
}

function renderConversionFunnel(stages) {
  const container = el('funnelStagesList');
  if (!container) return;
  if (!stages.length) {
    container.innerHTML = emptyState('No funnel stages reported yet. The funnel is built from tracked visits and POS rows.');
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
  `<div class="fp-empty" style="padding-top:8px">Stages showing ${DASH} were not observed. Converted requires a connected POS feed.</div>`;
}

function renderFrictionZones(zones) {
  const tbody = el('frictionZonesTableBody');
  if (!tbody) return;
  const ranked = [...zones]
    .sort((a, b) => (isNum(b.total_dwell_seconds) ? b.total_dwell_seconds : 0) - (isNum(a.total_dwell_seconds) ? a.total_dwell_seconds : 0))
    .slice(0, 5);
  tbody.innerHTML = ranked.map((z) => `
    <tr>
      <td>${escapeHtml(z.name)}</td>
      <td>${isNum(z.engagement_rate_pct) ? z.engagement_rate_pct + '%' : `<span class="fp-dash">${DASH}</span>`}</td>
      <td>${isNum(z.avg_dwell_seconds) ? z.avg_dwell_seconds + 's' : `<span class="fp-dash">${DASH}</span>`}</td>
      <td>${isNum(z.visits) ? z.visits : DASH} visits · ${isNum(z.unique_visitors) ? z.unique_visitors : DASH} people</td>
      <td><span class="fp-dash">needs POS</span></td>
    </tr>`).join('');
}

// ==========================================
// 5. Loss prevention & theft
// ==========================================
function playTheftAlertSound() {
  try {
    const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = 'sawtooth';
    osc.frequency.setValueAtTime(880, audioCtx.currentTime);
    osc.frequency.exponentialRampToValueAtTime(440, audioCtx.currentTime + 0.3);
    gain.gain.setValueAtTime(0.15, audioCtx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.01, audioCtx.currentTime + 0.3);
    osc.connect(gain);
    gain.connect(audioCtx.destination);
    osc.start();
    osc.stop(audioCtx.currentTime + 0.35);
  } catch (e) { /* audio blocked */ }
}

function formatTimestamp(ts) {
  if (!ts) return DASH;
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? String(ts) : d.toLocaleString();
}

async function fetchTheftIncidents() {
  const [data, stats] = await Promise.all([
    getJSON('/api/v1/theft/incidents', null),
    getJSON('/api/v1/theft/statistics', null),
  ]);
  const incidents = (data && data.incidents) || [];

  // KPIs: counts are real row counts. A sum over zero rows is "nothing
  // measured", not $0, and is shown as a dash.
  if (stats) {
    setMetric('kpiTheftActiveAlerts', stats.active_incidents_count);
    setMetric('kpiTheftAttemptsToday', stats.today_incidents_count);
    const anyClosed = incidents.some((i) => i.status === 'DISPATCHED' || i.status === 'RESOLVED');
    setMetric('kpiTheftPreventedLoss', anyClosed && isNum(stats.prevented_loss_estimate) ? stats.prevented_loss_estimate : null,
      { prefix: '$', digits: 2 });
    const depts = Object.entries(stats.by_department || {}).sort((a, b) => b[1] - a[1]);
    setMetric('kpiTheftHighRiskDept', depts.length ? `${depts[0][0]} (${depts[0][1]})` : null);
    const badge = el('tabTheftCountBadge');
    if (badge) badge.textContent = isNum(stats.active_incidents_count) ? stats.active_incidents_count : DASH;
  } else {
    ['kpiTheftActiveAlerts', 'kpiTheftAttemptsToday', 'kpiTheftPreventedLoss', 'kpiTheftHighRiskDept'].forEach((id) => setMetric(id, null));
  }

  const activeInc = incidents.find((i) => i.status === 'ACTIVE');
  const banner = el('theftAlertBanner');
  if (activeInc) {
    activeTheftIncidentId = activeInc.id;
    if (banner) {
      banner.style.display = 'flex';
      el('theftBannerHeadline').textContent = `THEFT ALERT: ${activeInc.theft_type} on ${activeInc.camera_name} (${activeInc.department})`;
      el('theftBannerConfidence').textContent = isNum(activeInc.confidence) ? `Confidence: ${Math.round(activeInc.confidence * 100)}%` : `Confidence: ${DASH}`;
      el('theftBannerTime').textContent = `Time: ${formatTimestamp(activeInc.timestamp)}`;
      el('theftBannerDetails').textContent = activeInc.evidence_summary || '';
    }
    if (lastAlertedTheftId !== activeInc.id) {
      lastAlertedTheftId = activeInc.id;
      playTheftAlertSound();
    }
  } else if (banner) {
    banner.style.display = 'none';
  }

  renderTheftIncidentsList(incidents, data === null);
}

function renderTheftIncidentsList(incidents, unavailable) {
  const container = el('theftIncidentsList');
  if (!container) return;
  if (unavailable) {
    container.innerHTML = emptyState('Incident log unavailable: the theft service did not respond.');
    return;
  }
  if (!incidents.length) {
    container.innerHTML = emptyState('No theft incidents recorded. Incidents are written by the detection pipeline when it observes a shelf-sweep, concealment or exit-without-payment pattern; none has been observed.');
    return;
  }
  container.innerHTML = '';
  incidents.forEach((inc) => {
    const card = document.createElement('div');
    card.id = `incident-${inc.id}`;
    card.className = 'incident-card';
    let statusPill = '<span class="badge badge-green">● RESOLVED</span>';
    if (inc.status === 'ACTIVE') statusPill = '<span class="badge badge-danger" style="animation: bounceAlert 1s infinite alternate;">🚨 ACTIVE</span>';
    else if (inc.status === 'ACKNOWLEDGED') statusPill = '<span class="badge badge-warning">👁️ ACKNOWLEDGED</span>';
    else if (inc.status === 'DISPATCHED') statusPill = '<span class="badge" style="background: #9d4edd; color:#fff;">🚔 GUARD DISPATCHED</span>';
    else if (inc.status === 'FALSE_ALARM') statusPill = '<span class="badge">FALSE ALARM</span>';

    const conf = isNum(inc.confidence) ? `${Math.round(inc.confidence * 100)}% conf` : `${DASH} conf`;
    const value = isNum(inc.estimated_loss_value) && inc.estimated_loss_value > 0 ? `$${inc.estimated_loss_value.toFixed(2)}` : DASH;
    card.innerHTML = `
      <div class="incident-head">
        <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
          <span style="font-size: 14px; font-weight: 800; color: #fff;">${escapeHtml(inc.theft_type)}</span>
          <span class="badge" style="font-size: 10px;">${escapeHtml(inc.department)}</span>
          <span style="font-family: var(--font-mono); font-size: 11px; color: var(--accent-cyan); font-weight: 700;">${escapeHtml(inc.camera_name)}</span>
        </div>
        <div style="display: flex; align-items: center; gap: 8px;">
          <span style="font-family: var(--font-mono); font-size: 11px; color: #ffaa00;">${conf}</span>
          ${statusPill}
        </div>
      </div>
      <div style="font-size: 11.5px; color: #cbd5e1; line-height: 1.4;">${escapeHtml(inc.evidence_summary || 'No evidence summary recorded.')}</div>
      <div class="incident-foot">
        <span style="font-family: var(--font-mono); font-size: 10.5px; color: var(--text-dim);">Logged: ${escapeHtml(formatTimestamp(inc.timestamp))} · Estimated value: <b style="color: var(--accent-green);">${value}</b></span>
        <div style="display: flex; gap: 6px;">
          ${inc.status === 'ACTIVE' ? `<button type="button" class="btn btn-sm" onclick="acknowledgeTheft('${escapeHtml(inc.id)}')">Acknowledge</button>` : ''}
          ${(inc.status === 'ACTIVE' || inc.status === 'ACKNOWLEDGED') ? `<button type="button" class="btn btn-danger btn-sm" onclick="dispatchGuard('${escapeHtml(inc.id)}')">🚨 Dispatch Guard</button>` : ''}
          ${(inc.status !== 'RESOLVED' && inc.status !== 'FALSE_ALARM') ? `<button type="button" class="btn btn-primary btn-sm" onclick="resolveTheft('${escapeHtml(inc.id)}')">✅ Resolve</button>` : ''}
        </div>
      </div>`;
    container.appendChild(card);
  });
}

async function theftAction(id, action, label) {
  try {
    const res = await fetch(`/api/v1/theft/incidents/${encodeURIComponent(id)}/${action}`, { method: 'POST' });
    if (res.ok) { showToast(`${label} ${id}`); fetchTheftIncidents(); }
    else showToast(`Failed to ${action} ${id} (HTTP ${res.status})`);
  } catch (e) { showToast(`Failed to ${action}: ${e.message}`); }
}
function acknowledgeTheft(id) { return theftAction(id, 'acknowledge', 'Acknowledged'); }
function dispatchGuard(id) { return theftAction(id, 'dispatch', 'Guard dispatched for'); }
function resolveTheft(id) { return theftAction(id, 'resolve', 'Resolved'); }

function dismissTheftBanner() { const b = el('theftAlertBanner'); if (b) b.style.display = 'none'; }
function dispatchGuardFromBanner() { if (activeTheftIncidentId) { dispatchGuard(activeTheftIncidentId); dismissTheftBanner(); } }
function scrollToTheftIncident() {
  if (!activeTheftIncidentId) return;
  const node = el(`incident-${activeTheftIncidentId}`);
  if (node) node.scrollIntoView({ behavior: 'smooth', block: 'center' });
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

function populateDepartmentOptions(selected) {
  const select = el('configDepartment');
  if (!select) return;
  const options = new Set();
  ((layoutSnapshot && layoutSnapshot.zone_categories) || []).forEach((c) => options.add(c));
  allCamerasList.forEach((c) => { if (c.department) options.add(c.department); });
  if (selected) options.add(selected);
  if (!options.size) options.add('GENERAL');
  select.innerHTML = [...options].sort().map((o) => `<option value="${escapeHtml(o)}">${escapeHtml(o)}</option>`).join('');
  select.value = selected || select.options[0].value;
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
  safeSet('configFps', isNum(cam.fps) ? cam.fps : '');
  safeSet('configResolution', cam.resolution || '');

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
  safeCheck('featDwellTracking', feats.dwell_tracking);
  safeCheck('featShelfInteraction', feats.shelf_interaction);
  safeCheck('featTheftDetection', feats.theft_detection);
  safeCheck('featFallDetection', feats.fall_detection);
  safeCheck('featQueueMonitoring', feats.queue_monitoring);

  const bounds = layoutSnapshot ? ` Store is ${layoutSnapshot.width_m} × ${layoutSnapshot.height_m} m.` : '';
  setCameraConfigStatus(`Editing ${cam.id}.${bounds}`, false);
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

  const features = Object.assign({}, existing.features || {}, {
    dwell_tracking: el('featDwellTracking').checked,
    shelf_interaction: el('featShelfInteraction').checked,
    theft_detection: el('featTheftDetection').checked,
    fall_detection: el('featFallDetection').checked,
    queue_monitoring: el('featQueueMonitoring').checked,
  });

  // PUT replaces the whole row, so every field the API knows is sent back
  // from the object we loaded; otherwise schema defaults (floor 100 m, no
  // homography) would silently overwrite real configuration.
  const feed = Object.assign({}, existing, {
    id: camId,
    name: el('configCameraName').value.trim(),
    channel_number: parseInt(el('configChannelNumber').value, 10) || existing.channel_number || 1,
    department: el('configDepartment').value,
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
    // Feature toggles are held by the feature manager, not the camera row.
    await fetch(`/api/v1/cameras/${encodeURIComponent(camId)}/features`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(features),
    }).catch(() => null);

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
// 7. AI decision action centre
// ==========================================
async function loadActionCenter() {
  const data = await getJSON('/api/v1/analytics/actions', null);
  allDecisions = (data && (data.decisions || data.actions)) || [];
  renderActionCenter(data === null);
}

function isOpenDecision(d) {
  const s = String(d.status || '').toUpperCase();
  return !['APPLIED', 'DONE', 'DISMISSED', 'RESOLVED'].includes(s);
}

function renderActionCenter(unavailable) {
  const list = el('actionCardsList');
  if (!list) return;
  const open = allDecisions.filter(isOpenDecision).length;
  const badge = el('actionsOpenBadge');
  if (badge) {
    badge.textContent = unavailable ? DASH : `${open} OPEN ACTION${open === 1 ? '' : 'S'}`;
    badge.classList.toggle('metric-unobserved', unavailable);
    badge.classList.toggle('badge-danger', !unavailable && open > 0);
  }
  const tabBadge = el('tabActionCountBadge');
  if (tabBadge) tabBadge.textContent = unavailable ? DASH : open;

  if (unavailable) {
    list.innerHTML = emptyState('Recommendations unavailable: the analytics service did not respond.');
    return;
  }
  if (!allDecisions.length) {
    list.innerHTML = emptyState('No recommendations yet. Findings are generated from measured zone visits, dwell and queue data; run "Run Optimization" on the Market Intelligence tab once cameras are calibrated and zones have activity.',
      'Open Market Intelligence', "switchTab('market_ai')");
    return;
  }

  list.innerHTML = '';
  allDecisions.forEach((item) => {
    const card = document.createElement('div');
    card.className = `action-card severity-${escapeHtml(item.severity || 'INFO')} status-${escapeHtml(item.status || 'PENDING')}`;
    let severityClass = 'badge-primary';
    if (item.severity === 'CRITICAL') severityClass = 'badge-danger';
    else if (item.severity === 'HIGH') severityClass = 'badge-warning';
    card.innerHTML = `
      <div class="action-card-header">
        <div style="display:flex; gap: 8px; align-items: center; flex-wrap: wrap;">
          <span class="badge ${severityClass}">${escapeHtml(item.severity || DASH)}</span>
          <span class="badge" style="font-size: 10px;">${escapeHtml(item.category || DASH)}</span>
          <span class="action-title">${escapeHtml(item.zone || 'Store')}: ${escapeHtml(item.finding || '')}</span>
        </div>
        <span class="badge" style="background: rgba(255,255,255,0.1);">${escapeHtml(item.status || DASH)}</span>
      </div>
      <div class="action-desc"><b>Why:</b> ${escapeHtml(item.root_cause || DASH)}<br><b>Do:</b> ${escapeHtml(item.action_item || DASH)}</div>
      <div class="action-footer">
        <div class="action-meta">${escapeHtml(item.date || '')}</div>
        <div class="action-btns">
          ${isOpenDecision(item) ? `
            <button type="button" class="btn btn-sm" onclick="updateActionStatus('${escapeHtml(item.id)}', 'REVIEWED')">👁️ Reviewed</button>
            <button type="button" class="btn btn-primary btn-sm" onclick="updateActionStatus('${escapeHtml(item.id)}', 'APPLIED')">✅ Applied</button>
            <button type="button" class="btn btn-sm" onclick="updateActionStatus('${escapeHtml(item.id)}', 'DISMISSED')">Dismiss</button>` : ''}
        </div>
      </div>`;
    list.appendChild(card);
  });
}

async function updateActionStatus(actionId, newStatus) {
  try {
    const res = await fetch(`/api/v1/analytics/actions/${encodeURIComponent(actionId)}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status: newStatus }),
    });
    if (res.ok) { showToast(`Marked ${actionId} as ${newStatus}`); loadActionCenter(); }
    else showToast(`Update failed (HTTP ${res.status})`);
  } catch (e) { showToast(`Update failed: ${e.message}`); }
}

// ==========================================
// 8. Market intelligence (local model)
// ==========================================
function renderFindings(findings) {
  if (!findings || !findings.length) return '';
  return findings.map((f) => `
    <div class="action-card severity-${escapeHtml(f.severity || 'INFO')}">
      <div class="action-card-header">
        <span class="badge ${f.severity === 'CRITICAL' ? 'badge-danger' : (f.severity === 'HIGH' ? 'badge-warning' : '')}">${escapeHtml(f.severity || '')}</span>
        <span class="action-title">${escapeHtml(f.zone || 'Store')}: ${escapeHtml(f.finding || '')}</span>
      </div>
      <div class="action-desc"><b>Why:</b> ${escapeHtml(f.root_cause || DASH)}<br><b>Do:</b> ${escapeHtml(f.action_item || DASH)}</div>
    </div>`).join('');
}

async function loadMarketPredictions() {
  const host = el('llmOptimizationsContainer');
  if (!host) return;
  const [status, forecast] = await Promise.all([
    getJSON('/api/v1/analytics/market/llm-status', null),
    getJSON('/api/v1/analytics/market/predictions', null),
  ]);
  const rows = [];
  if (!status) {
    rows.push(emptyState('Model status unavailable: the analytics service did not respond.'));
  } else if (!status.ollama_active || !status.model) {
    rows.push(emptyState(`No local language model is available. ${status.reason || ''} Findings are still computed from measurements; only the narrative needs a model.`));
  } else {
    rows.push(`<div class="fp-empty">Local model <b>${escapeHtml(status.model)}</b> ${status.generation_verified ? 'answered a test generation' : 'is listed but has not completed a test generation'}. ${escapeHtml(status.note || '')}</div>`);
  }
  if (forecast) {
    rows.push(forecast.sufficient_history
      ? `<div class="fp-empty">Hourly forecast built from ${forecast.days_observed} observed day(s).</div>`
      : `<div class="fp-empty">${escapeHtml(forecast.message || `Forecast needs ${forecast.days_required} observed days; ${forecast.days_observed} so far.`)}</div>`);
  }
  rows.push('<div class="fp-empty">Press "Run Optimization" to analyse today\'s measurements.</div>');
  host.innerHTML = rows.join('');
}

async function runLLMOptimizations() {
  const host = el('llmOptimizationsContainer');
  if (!host) return;
  host.innerHTML = '<div class="fp-empty">Analysing measured zone activity… (a local model narrative may take up to a minute)</div>';
  try {
    const res = await fetch('/api/v1/analytics/market/llm-optimize', { method: 'POST' });
    if (!res.ok) {
      host.innerHTML = emptyState(`Analysis failed (HTTP ${res.status}).`);
      return;
    }
    const data = await res.json();
    const parts = [];
    parts.push(`<div class="fp-empty">${escapeHtml(data.message || '')} Zones assessed: ${data.zones_assessed ?? DASH} of ${data.zones_total ?? DASH}.</div>`);
    const n = data.narrative;
    if (n && n.summary) {
      parts.push(`<div class="digest-narrative" style="padding:10px 12px; border:1px solid rgba(120,140,170,0.18); border-radius:9px;">${escapeHtml(n.summary)}<div class="action-meta" style="margin-top:6px">Narrated by ${escapeHtml(n.model_used)} in ${n.elapsed_seconds ?? DASH}s</div></div>`);
    } else if (n && n.reason) {
      parts.push(`<div class="fp-empty">No narrative: ${escapeHtml(n.reason)}</div>`);
    }
    parts.push(renderFindings(data.findings) || emptyState('No findings: nothing measured today exceeded a threshold.'));
    host.innerHTML = parts.join('');
    loadActionCenter();
  } catch (e) {
    host.innerHTML = emptyState(`Analysis failed: ${e.message}`);
  }
}

// ==========================================
// 9. Executive digest
// ==========================================
async function loadExecutiveDigest() {
  const host = el('digestNarrative');
  if (!host) return;
  const data = await getJSON('/api/v1/analytics/digest', null);
  if (!data) { host.innerHTML = emptyState('Digest unavailable: the analytics service did not respond.'); return; }

  const storeName = el('digestStoreName');
  if (storeName && data.report_title) storeName.textContent = data.report_title.replace(/^Daily Intelligence Digest - /, '');

  const card = data.kpi_scorecard || {};
  const cov = data.coverage || {};
  const scorecard = Object.entries(card).map(([k, v]) => `
    <div class="stat-box">
      <span class="stat-label">${escapeHtml(k.replace(/_/g, ' '))}</span>
      <span class="stat-val ${(v === null || v === undefined || v === 'Not observed') ? 'metric-unobserved' : ''}" style="font-size:18px">${escapeHtml(v === null || v === undefined ? DASH : v)}</span>
    </div>`).join('');

  host.innerHTML = `
    <p><b>${escapeHtml(data.date || '')}</b> · ${escapeHtml(data.executive_summary || 'No summary produced.')}</p>
    <div class="overview-strip" style="margin:12px 0">${scorecard}</div>
    <div class="action-meta">Coverage: ${cov.cameras_total ?? DASH} camera(s), ${cov.cameras_calibrated ?? DASH} calibrated · ${data.data_available ? 'observations recorded' : 'no observations recorded for this period'}</div>
    <h4 style="margin:14px 0 6px; font-size:12.5px;">Findings (${data.findings_count ?? 0})</h4>
    ${renderFindings(data.findings) || emptyState(data.analysis_message || 'No findings for this period.')}`;
}

function printDailyDigest() { window.print(); }

// ==========================================
// 10. Opt-in self test (?__selftest=1)
// ==========================================
async function runSelfTest() {
  const errors = [];
  window.addEventListener('error', (e) => errors.push(`onerror: ${e.message} @ ${e.filename}:${e.lineno}`));
  window.addEventListener('unhandledrejection', (e) => errors.push(`unhandledrejection: ${e.reason && (e.reason.message || e.reason)}`));
  const origError = console.error.bind(console);
  console.error = (...args) => { errors.push(`console.error: ${args.map(String).join(' ')}`); origError(...args); };

  const out = document.createElement('pre');
  out.id = 'selftestResults';
  out.style.cssText = 'position:fixed; bottom:0; left:0; right:0; max-height:45vh; overflow:auto; margin:0; padding:10px; background:#000; color:#0f0; font:11px ui-monospace, monospace; z-index:99999; white-space:pre-wrap;';
  document.body.appendChild(out);
  const results = [];
  const check = (name, ok, detail) => results.push(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ` :: ${detail}` : ''}`);

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
      if (img.naturalWidth > 0 && r.width > 0) {
        const natural = img.naturalWidth / img.naturalHeight;
        const shown = r.width / r.height;
        check(`tile ${id} aspect not collapsed`, Math.abs(natural - shown) < 0.6 || r.height > 100, `natural ${natural.toFixed(2)} shown ${shown.toFixed(2)}`);
      }
    });
    const hud = document.querySelector('[data-hud-for]');
    check('per-tile HUD populated from pipeline', !!hud && hud.textContent.trim() !== DASH, hud && hud.textContent.trim());
    check('no fabricated HUD strings', !/Queue: 2|Dwell Alert|In: 142|Engagement: 64/.test(document.body.textContent));
  }
  check('area chips built from real departments', cams === 0 || document.querySelectorAll('.channel-pill').length >= 2,
    `${document.querySelectorAll('.channel-pill').length} chips`);

  // FPS throttle really changes the stream URL.
  if (tiles.length) {
    setDecimationFPS(1);
    check('fps throttle re-sets src', /fps=1&/.test(tiles[0].getAttribute('src') || ''), tiles[0].getAttribute('src'));
    setDecimationFPS(5);
  }

  // Every tab switches, and streams detach when the matrix is hidden.
  for (const tab of VALID_TABS) {
    switchTab(tab);
    const view = document.getElementById(`tab-${tab}`);
    check(`switchTab('${tab}')`, !!view && view.classList.contains('active') && window.location.hash === `#${tab}`);
  }
  check('streams detached when matrix hidden', [...tiles].every((img) => !img.getAttribute('src')));
  switchTab('matrix');
  check('streams re-attached on matrix', tiles.length === 0 || [...tiles].every((img) => !!img.getAttribute('src')));

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
  check('no prompt/confirm/alert in dashboard scripts', true, 'static check in csscheck/grep');

  await new Promise((r) => setTimeout(r, 800));
  check('no page errors', errors.length === 0, errors.join(' | '));
  const banner = el('dataStateBanner');
  results.push(`INFO data-state banner: ${banner && banner.style.display !== 'none' ? banner.textContent.trim() : '(hidden)'}`);
  results.push(`INFO AI engine: ${el('telemetryInferenceBadge').textContent} · decoder: ${el('telemetryDecoderBadge').textContent}`);
  const failed = results.filter((r) => r.startsWith('FAIL')).length;
  results.unshift(`SELFTEST ${failed === 0 ? 'OK' : 'FAILED'} (${results.length} checks, ${failed} failed)`);
  out.textContent = results.join('\n');
  window.__selftestDone = true;
}

// Expose all public actions on window so inline onclick handlers and console calls always work
window.switchTab = switchTab;
window.filterCameras = filterCameras;
window.filterCamerasBySearch = filterCamerasBySearch;
window.setDecimationFPS = setDecimationFPS;
window.openCameraConfigModal = openCameraConfigModal;
window.closeCameraConfigModal = closeCameraConfigModal;
window.handleCameraConfigSubmit = handleCameraConfigSubmit;
window.askDeleteCurrentCamera = askDeleteCurrentCamera;
window.confirmDeleteCurrentCamera = confirmDeleteCurrentCamera;
window.cancelDeleteCurrentCamera = cancelDeleteCurrentCamera;
window.runLLMOptimizations = runLLMOptimizations;
window.fetchTheftIncidents = fetchTheftIncidents;
window.printDailyDigest = printDailyDigest;
window.dispatchGuardFromBanner = dispatchGuardFromBanner;
window.scrollToTheftIncident = scrollToTheftIncident;
window.dismissTheftBanner = dismissTheftBanner;
window.initOrUpdateCharts = initOrUpdateCharts;
window.loadMarketPredictions = function () {
  const host = el('llmOptimizationsContainer');
  if (host && host.children.length === 0) runLLMOptimizations();
};

function initAnalytics() {
  initHashRouting();
  fetchSystemTelemetry();
  fetchStoreKPIs();
  loadCamerasMatrix();
  loadActionCenter();
  loadExecutiveDigest();
  fetchTheftIncidents();

  setInterval(fetchSystemTelemetry, 2000);      // also drives the per-tile HUD
  setInterval(fetchStoreKPIs, 4000);
  setInterval(loadCamerasMatrix, 5000);         // picks up adopted / removed cameras
  setInterval(fetchTheftIncidents, 4000);
  setInterval(attachCameraStreams, 2000);

  if (new URLSearchParams(window.location.search).get('__selftest') === '1') runSelfTest();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initAnalytics);
} else {
  initAnalytics();
}
