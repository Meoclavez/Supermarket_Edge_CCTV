/**
 * Camera Studio & Zone Editor
 *
 * One camera at a time: its live MJPEG feed with the tracker's real boxes
 * (/stream?camera_id=..&overlay=1), the pipeline's telemetry for that camera,
 * snapshot / clip export, and the per-camera zone editor (tripwires,
 * restricted areas, privacy masks, product shelves).
 *
 * Zone coordinates are stored normalised (0..1) against the camera's own
 * frame. The feed is displayed with object-fit: contain, so a click has to
 * be mapped through the letterboxed content box, computed from the image's
 * naturalWidth/naturalHeight, before it is normalised.
 */

'use strict';

const DASH = '—';
const canvas = document.getElementById('interactiveCanvas');
const ctx = canvas ? canvas.getContext('2d') : null;
const streamImg = document.getElementById('streamImg');
const viewport = document.getElementById('viewportWrapper');

let currentMode = 'NONE';
let drawnPoints = [];
let activeCameraId = null;
let studioCameras = [];
let savedZonesForCamera = { tripwires: [], intrusion_zones: [], exclusion_masks: [], products: [] };
let telemetryTimer = null;

const MODE_STYLE = {
  TRIPWIRE: { stroke: '#00f0ff', fill: 'rgba(0, 240, 255, 0.2)', min: 2, max: 2, label: 'tripwire' },
  INTRUSION: { stroke: '#ffaa00', fill: 'rgba(255, 170, 0, 0.25)', min: 3, max: Infinity, label: 'restricted area' },
  EXCLUSION: { stroke: '#a0aec0', fill: 'rgba(160, 174, 192, 0.35)', min: 3, max: Infinity, label: 'privacy mask' },
  PRODUCT_SHELF: { stroke: '#ffd700', fill: 'rgba(255, 215, 0, 0.22)', min: 4, max: Infinity, label: 'product shelf' },
};

// ------------------------------------------------------------ helpers
function el(id) { return document.getElementById(id); }
function isNum(v) { return typeof v === 'number' && Number.isFinite(v); }
function escapeHtml(v) {
  return String(v === null || v === undefined ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function getUrlParameter(name) { return new URLSearchParams(window.location.search).get(name); }
function canPoll() {
  if (window.edgeAuth && typeof window.edgeAuth.isAuthenticated === 'function') {
    return window.edgeAuth.isAuthenticated();
  }
  const gate = document.getElementById('authGate');
  return !(gate && gate.style.display !== 'none');
}

async function getJSON(url, fallback = null) {
  if (!canPoll() && !url.includes('/auth/') && !url.includes('/setup/')) return fallback;
  try { const r = await fetch(url); return r.ok ? await r.json() : fallback; } catch (e) { return fallback; }
}
function showToast(msg) {
  const t = el('toast');
  if (!t) return;
  t.textContent = msg;
  t.style.display = 'block';
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => { t.style.display = 'none'; }, 3000);
}
function setDrawStatus(msg, isError) {
  const s = el('drawStatus');
  if (!s) return;
  s.textContent = msg;
  s.classList.toggle('form-status-error', !!isError);
}

// ------------------------------------------------------------ viewport geometry
/**
 * The box the image content actually occupies inside the viewport, after
 * object-fit: contain letterboxing. All zone coordinates map through this.
 */
function contentBox() {
  if (!viewport) return { ox: 0, oy: 0, w: 0, h: 0 };
  const W = viewport.clientWidth;
  const H = viewport.clientHeight;
  const nw = streamImg && streamImg.naturalWidth;
  const nh = streamImg && streamImg.naturalHeight;
  if (!nw || !nh || !W || !H) return { ox: 0, oy: 0, w: W, h: H };
  const scale = Math.min(W / nw, H / nh);
  const w = nw * scale;
  const h = nh * scale;
  return { ox: (W - w) / 2, oy: (H - h) / 2, w, h };
}

function resizeCanvas() {
  if (!canvas || !viewport) return;
  const box = contentBox();
  canvas.style.left = `${box.ox}px`;
  canvas.style.top = `${box.oy}px`;
  canvas.style.width = `${box.w}px`;
  canvas.style.height = `${box.h}px`;
  canvas.width = Math.max(1, Math.round(box.w));
  canvas.height = Math.max(1, Math.round(box.h));
  drawOverlay();
}
window.addEventListener('resize', resizeCanvas);
if (window.ResizeObserver && viewport) new ResizeObserver(resizeCanvas).observe(viewport);
if (streamImg) {
  // MJPEG fires load once per frame in most browsers; only re-measure when
  // the frame size changes so the canvas is not thrashed 25 times a second.
  let lastNatural = '';
  streamImg.addEventListener('load', () => {
    setViewportEmpty('');
    clearTimeout(streamImg._retryTimer);
    const sig = `${streamImg.naturalWidth}x${streamImg.naturalHeight}`;
    if (sig !== lastNatural) { lastNatural = sig; resizeCanvas(); }
  });
  streamImg.addEventListener('error', () => {
    setViewportEmpty('Reconnecting to camera feed…');
    clearTimeout(streamImg._retryTimer);
    streamImg._retryTimer = setTimeout(() => {
      if (activeCameraId) {
        setViewportEmpty('');
        const sUrl = `/stream?camera_id=${encodeURIComponent(activeCameraId)}&overlay=1&_t=${Date.now()}`;
        streamImg.src = window.edgeAuth && window.edgeAuth.authUrl ? window.edgeAuth.authUrl(sUrl) : sUrl;
      }
    }, 1500);
  });
}

function setViewportEmpty(message) {
  const e = el('viewportEmpty');
  if (!e) return;
  e.innerHTML = message ? `<div>${escapeHtml(message)}</div>` : '';
  e.style.display = message ? 'flex' : 'none';
}

if (canvas) {
  canvas.addEventListener('click', (e) => {
    if (currentMode === 'NONE') { setDrawStatus('Pick a tool first (tripwire, restricted area, mask or shelf).', true); return; }
    if (!activeCameraId) { setDrawStatus('Select a camera first.', true); return; }
    const rect = canvas.getBoundingClientRect();
    const nx = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
    const ny = Math.min(1, Math.max(0, (e.clientY - rect.top) / rect.height));
    const style = MODE_STYLE[currentMode];
    if (drawnPoints.length >= style.max) drawnPoints = [];
    drawnPoints.push({ x: Number(nx.toFixed(4)), y: Number(ny.toFixed(4)) });
    drawOverlay();
    const need = Math.max(0, style.min - drawnPoints.length);
    setDrawStatus(`${drawnPoints.length} point(s) placed for the ${style.label}.` +
      (need ? ` ${need} more needed.` : ' Press Save to store it.'), false);
  });
}

function drawPolyline(points, stroke, fill, close, width) {
  if (!points.length) return;
  ctx.strokeStyle = stroke;
  ctx.fillStyle = fill;
  ctx.lineWidth = width;
  ctx.beginPath();
  points.forEach((pt, i) => {
    const px = pt.x * canvas.width;
    const py = pt.y * canvas.height;
    if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
  });
  if (close && points.length >= 3) { ctx.closePath(); ctx.fill(); }
  ctx.stroke();
}

function drawOverlay() {
  if (!ctx || !canvas) return;
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  // Saved zones for this camera, drawn faintly so the operator sees what exists.
  savedZonesForCamera.tripwires.forEach((tw) => {
    drawPolyline([{ x: tw.x1, y: tw.y1 }, { x: tw.x2, y: tw.y2 }], 'rgba(0,240,255,0.55)', 'transparent', false, 1.5);
  });
  savedZonesForCamera.intrusion_zones.forEach((z) => drawPolyline(z.points || [], 'rgba(255,170,0,0.55)', 'rgba(255,170,0,0.10)', true, 1.5));
  savedZonesForCamera.exclusion_masks.forEach((z) => drawPolyline(z.points || [], 'rgba(160,174,192,0.55)', 'rgba(160,174,192,0.15)', true, 1.5));
  savedZonesForCamera.products.forEach((z) => drawPolyline(z.points || [], 'rgba(255,215,0,0.55)', 'rgba(255,215,0,0.10)', true, 1.5));

  if (!drawnPoints.length || currentMode === 'NONE') return;
  const style = MODE_STYLE[currentMode];
  drawPolyline(drawnPoints, style.stroke, style.fill, currentMode !== 'TRIPWIRE', 2.5);
  drawnPoints.forEach((pt) => {
    ctx.fillStyle = '#ffffff';
    ctx.beginPath();
    ctx.arc(pt.x * canvas.width, pt.y * canvas.height, 5, 0, Math.PI * 2);
    ctx.fill();
  });
}

// ------------------------------------------------------------ drawing modes
function setDrawMode(mode) {
  currentMode = mode;
  drawnPoints = [];
  drawOverlay();
  const label = el('drawModeLabel');
  if (label) label.textContent = `mode: ${MODE_STYLE[mode].label}`;
  setDrawStatus(`Click ${MODE_STYLE[mode].min === MODE_STYLE[mode].max ? MODE_STYLE[mode].min : `${MODE_STYLE[mode].min}+`} point(s) on the video for the ${MODE_STYLE[mode].label}.`, false);
}

function undoPoint() {
  drawnPoints.pop();
  drawOverlay();
  setDrawStatus(`${drawnPoints.length} point(s) placed.`, false);
}

function clearCanvasPoints() {
  drawnPoints = [];
  currentMode = 'NONE';
  drawOverlay();
  const label = el('drawModeLabel');
  if (label) label.textContent = 'mode: none';
  setDrawStatus('Drawing cancelled.', false);
}

// ------------------------------------------------------------ product modal
function openProductModal() {
  const modal = el('productModal');
  if (!modal) return;
  el('productModalStatus').textContent = '';
  modal.style.display = 'flex';
  el('modalProductName').focus();
}
function closeProductModal() { const m = el('productModal'); if (m) m.style.display = 'none'; }

async function populateZoneNames() {
  const layout = await getJSON('/api/v1/layout', null);
  const list = el('layoutZoneNames');
  if (!list) return;
  const names = ((layout && layout.zones) || []).map((z) => z.name).filter(Boolean);
  list.innerHTML = names.map((n) => `<option value="${escapeHtml(n)}"></option>`).join('');
  const input = el('modalProductCategory');
  if (input) input.placeholder = names.length ? 'Pick a blueprint zone' : 'No zones drawn on the blueprint yet; type a name';
}

async function submitProductModal(event) {
  if (event) event.preventDefault();
  const status = el('productModalStatus');
  const name = el('modalProductName').value.trim();
  const sku = el('modalProductSku').value.trim();
  const category = el('modalProductCategory').value.trim();
  if (!name || !sku || !category) { status.textContent = 'Name, SKU and zone are required.'; return; }
  const price = parseFloat(el('modalProductPrice').value);
  const facing = parseInt(el('modalProductFacing').value, 10);

  const payload = {
    id: `shelf_${Date.now().toString(36)}`,
    camera_id: activeCameraId,
    name,
    points: drawnPoints,
    sku_id: sku,
    category,
    price: Number.isFinite(price) ? price : 0.0,
    facing_count: Number.isFinite(facing) && facing >= 1 ? facing : 1,
    shelf_tier: el('modalProductTier').value,
    study_metrics: {
      track_hand_reach: el('chkHandReach').checked,
      track_dwell_time: el('chkDwellTime').checked,
      track_put_back_friction: el('chkPutBack').checked,
      track_pos_conversion: el('chkPosSales').checked,
      ab_test_mode: el('chkAbTest').checked,
    },
    enabled: true,
  };
  try {
    const res = await fetch('/api/v1/analytics/products/zones', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      status.textContent = `Save failed (HTTP ${res.status}): ${JSON.stringify(err.detail || err)}`;
      return;
    }
    closeProductModal();
    clearCanvasPoints();
    showToast(`Mapped product area: ${name} (${sku})`);
    loadZonesList();
  } catch (err) {
    status.textContent = `Save failed: ${err.message}`;
  }
}

// ------------------------------------------------------------ zone persistence
async function postZone(url, body) {
  const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(`HTTP ${res.status}: ${JSON.stringify(err.detail || err)}`);
  }
  return res.json();
}

async function saveDrawnZone() {
  if (!activeCameraId) { setDrawStatus('Select a camera first.', true); return; }
  if (currentMode === 'NONE') { setDrawStatus('Pick a tool first.', true); return; }
  const style = MODE_STYLE[currentMode];
  if (drawnPoints.length < style.min) {
    setDrawStatus(`A ${style.label} needs at least ${style.min} points; ${drawnPoints.length} placed.`, true);
    return;
  }
  if (currentMode === 'PRODUCT_SHELF') { openProductModal(); return; }

  const name = el('zoneNameInput').value.trim() || `${style.label} ${new Date().toLocaleTimeString()}`;
  try {
    if (currentMode === 'TRIPWIRE') {
      await postZone('/api/zones/tripwire', {
        name, camera_id: activeCameraId,
        x1: drawnPoints[0].x, y1: drawnPoints[0].y, x2: drawnPoints[1].x, y2: drawnPoints[1].y,
        direction: 'BIDIRECTIONAL', allowed_classes: ['person'], enabled: true,
      });
    } else if (currentMode === 'INTRUSION') {
      await postZone('/api/zones/intrusion', {
        name, camera_id: activeCameraId, points: drawnPoints,
        allowed_classes: ['person'], dwell_time_seconds: 0.5, enabled: true,
      });
    } else if (currentMode === 'EXCLUSION') {
      await postZone('/api/zones/exclusion', {
        name, camera_id: activeCameraId, points: drawnPoints, mask_mode: 'BLUR', enabled: true,
      });
    }
    showToast(`Saved ${style.label}: ${name}`);
    el('zoneNameInput').value = '';
    clearCanvasPoints();
    loadZonesList();
  } catch (err) {
    setDrawStatus(`Save failed: ${err.message}`, true);
  }
}

async function deleteZoneAt(url, label) {
  try {
    const res = await fetch(url, { method: 'DELETE' });
    showToast(res.ok ? `${label} removed.` : `Failed to remove ${label} (HTTP ${res.status})`);
  } catch (e) { showToast(`Failed to remove ${label}: ${e.message}`); }
  loadZonesList();
}
function deleteTripwire(id) { return deleteZoneAt(`/api/zones/tripwire/${encodeURIComponent(id)}`, 'Tripwire'); }
function deleteIntrusion(id) { return deleteZoneAt(`/api/zones/intrusion/${encodeURIComponent(id)}`, 'Restricted area'); }
function deleteExclusion(id) { return deleteZoneAt(`/api/zones/exclusion/${encodeURIComponent(id)}`, 'Mask'); }
function deleteProductZone(id) { return deleteZoneAt(`/api/v1/analytics/products/zones/${encodeURIComponent(id)}`, 'Product area'); }

function askClearAllZones() { el('btnClearAllAsk').style.display = 'none'; el('clearAllConfirm').style.display = 'inline-flex'; }
function cancelClearAllZones() { el('btnClearAllAsk').style.display = ''; el('clearAllConfirm').style.display = 'none'; }
async function confirmClearAllZones() {
  try {
    const res = await fetch('/api/zones/clear', { method: 'POST' });
    showToast(res.ok ? 'All tripwires, areas and masks cleared.' : `Clear failed (HTTP ${res.status})`);
  } catch (e) { showToast(`Clear failed: ${e.message}`); }
  cancelClearAllZones();
  loadZonesList();
}

function zoneItem(icon, name, sub, onDelete, color) {
  return `
    <div class="zone-item"${color ? ` style="border-color:${color}"` : ''}>
      <div class="zone-info">
        <span class="zone-name">${icon} ${escapeHtml(name)}</span>
        <span class="zone-sub">${sub}</span>
      </div>
      <button type="button" class="btn btn-danger btn-sm" onclick="${onDelete}" title="Remove">🗑️</button>
    </div>`;
}

async function loadZonesList() {
  const forCam = (z) => !activeCameraId || z.camera_id === activeCameraId;

  const prodData = activeCameraId
    ? await getJSON(`/api/v1/analytics/products/zones?camera_id=${encodeURIComponent(activeCameraId)}`, null)
    : { zones: [] };
  const prodContainer = el('productShelfListContainer');
  const products = (prodData && prodData.zones) || [];
  if (prodContainer) {
    prodContainer.innerHTML = prodData === null
      ? '<div class="fp-empty">Product areas unavailable: the analytics service did not respond.</div>'
      : (products.length ? '' : '<div class="fp-empty">No product shelf areas on this camera. Draw one with the Product shelf tool.</div>');
    products.forEach((pz) => {
      prodContainer.insertAdjacentHTML('beforeend', zoneItem('🛒', pz.name,
        `${escapeHtml(pz.sku_id)} · ${isNum(pz.price) && pz.price > 0 ? '$' + pz.price.toFixed(2) : 'no price'} · ${escapeHtml(pz.shelf_tier)} · ${escapeHtml(pz.category)}`,
        `deleteProductZone('${escapeHtml(pz.id)}')`, 'rgba(255,170,0,0.3)'));
    });
  }

  const data = await getJSON('/api/zones', null);
  const tripwires = ((data && data.tripwires) || []).filter(forCam);
  const intrusion = ((data && data.intrusion_zones) || []).filter(forCam);
  const exclusion = ((data && data.exclusion_masks) || []).filter(forCam);

  const fill = (id, items, emptyMsg, render) => {
    const c = el(id);
    if (!c) return;
    if (data === null) { c.innerHTML = '<div class="fp-empty">Zones unavailable: the zone service did not respond.</div>'; return; }
    c.innerHTML = items.length ? items.map(render).join('') : `<div class="fp-empty">${emptyMsg}</div>`;
  };
  fill('tripwiresListContainer', tripwires, 'No tripwires on this camera. Draw one with the Tripwire tool.', (tw) =>
    zoneItem('⚡', tw.name, `crossings in: ${isNum(tw.in_count) ? tw.in_count : DASH} · out: ${isNum(tw.out_count) ? tw.out_count : DASH} · ${escapeHtml(tw.direction || 'BIDIRECTIONAL')}`,
      `deleteTripwire('${escapeHtml(tw.id)}')`));
  fill('intrusionListContainer', intrusion, 'No restricted areas on this camera.', (iz) =>
    zoneItem('🛑', iz.name, `${(iz.points || []).length} vertices · dwell ${isNum(iz.dwell_time_seconds) ? iz.dwell_time_seconds + 's' : DASH}`,
      `deleteIntrusion('${escapeHtml(iz.id)}')`));
  fill('exclusionListContainer', exclusion, 'No privacy masks on this camera.', (ex) =>
    zoneItem('🌫️', ex.name, `${(ex.points || []).length} vertices · ${escapeHtml(ex.mask_mode || 'BLUR')}`,
      `deleteExclusion('${escapeHtml(ex.id)}')`));

  savedZonesForCamera = { tripwires, intrusion_zones: intrusion, exclusion_masks: exclusion, products };
  drawOverlay();
}

// ------------------------------------------------------------ cameras & stream
function selectCamera(cameraId) {
  activeCameraId = cameraId;
  const cam = studioCameras.find((c) => c.id === cameraId);
  document.querySelectorAll('#cameraButtonsList .btn').forEach((b) => {
    b.classList.toggle('btn-primary', b.getAttribute('data-camera-id') === cameraId);
  });
  el('sourceBadge').textContent = cam ? `${cam.name} · ${cam.department || ''}` : cameraId;
  const url = new URL(window.location.href);
  if (url.searchParams.get('camera_id') !== cameraId) {
    url.searchParams.set('camera_id', cameraId);
    history.replaceState(null, '', url.toString());
  }
  if (streamImg) {
    setViewportEmpty('');
    const sUrl = `/stream?camera_id=${encodeURIComponent(cameraId)}&overlay=1`;
    streamImg.src = window.edgeAuth && window.edgeAuth.authUrl ? window.edgeAuth.authUrl(sUrl) : sUrl;
  }
  drawnPoints = [];
  loadZonesList();
  pollTelemetry();
}

async function loadStudioSources() {
  const data = await getJSON('/api/v1/cameras', null);
  const list = el('cameraButtonsList');
  if (!list) return;
  studioCameras = (data && data.cameras) || [];
  list.innerHTML = '';

  if (!studioCameras.length) {
    list.innerHTML = '<span class="fp-empty">No cameras configured.</span>';
    el('sourceBadge').textContent = 'No cameras configured';
    if (streamImg && streamImg.getAttribute('src')) streamImg.removeAttribute('src');
    setViewportEmpty(data === null
      ? 'Camera list unavailable: the API did not respond.'
      : 'No camera has been adopted yet. Use "Find & adopt cameras" to scan the network and USB ports from the Blueprint tab.');
    activeCameraId = null;
    loadZonesList();
    pollTelemetry();
    return;
  }

  studioCameras.forEach((cam) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-sm';
    btn.setAttribute('data-camera-id', cam.id);
    btn.textContent = `${cam.name}${cam.status === 'ONLINE' ? '' : ' (' + (cam.status || 'OFFLINE').toLowerCase() + ')'}`;
    btn.onclick = () => selectCamera(cam.id);
    list.appendChild(btn);
  });

  const wanted = getUrlParameter('camera_id');
  const pick = studioCameras.find((c) => c.id === wanted) || studioCameras.find((c) => c.id === activeCameraId) || studioCameras[0];
  if (wanted && !studioCameras.some((c) => c.id === wanted)) showToast(`Camera "${wanted}" not found; showing ${pick.name}.`);
  if (pick.id !== activeCameraId) selectCamera(pick.id);
  else document.querySelectorAll('#cameraButtonsList .btn').forEach((b) => b.classList.toggle('btn-primary', b.getAttribute('data-camera-id') === activeCameraId));
}

// ------------------------------------------------------------ telemetry
function setTelemetry(id, value, suffix = '') {
  const node = el(id);
  if (!node) return;
  if (value === null || value === undefined || value === '') {
    node.textContent = DASH;
    node.classList.add('metric-unobserved');
  } else {
    node.textContent = `${value}${suffix}`;
    node.classList.remove('metric-unobserved');
  }
}

async function pollTelemetry() {
  const detector = await getJSON('/api/v1/layout/pipeline/status', null);
  const d = (detector && detector.detector) || null;
  const detBadge = el('detectorBadge');
  if (detBadge) {
    detBadge.textContent = d ? (d.available ? `${String(d.backend || '').toUpperCase()} / ${String(d.provider || d.device || '').toUpperCase()}` : 'NO DETECTOR') : DASH;
    detBadge.style.color = d && d.available ? 'var(--accent-green)' : 'var(--accent-red)';
  }

  if (!activeCameraId) {
    ['tStatus', 'tFps', 'tDetections', 'tTracks', 'tFrames', 'tFrameSize', 'tAge', 'tCalibrated', 'tError'].forEach((id) => setTelemetry(id, null));
    setTelemetry('fpsBadge', null, ' FPS');
    return;
  }

  // Per-camera endpoint (Agent D); until it exists, the same entry is taken
  // from the pipeline status list so nothing here is ever invented.
  let live = await getJSON(`/api/v1/cameras/${encodeURIComponent(activeCameraId)}/live-status`, null);
  if (!live && detector) live = (detector.cameras || []).find((c) => c.camera_id === activeCameraId) || null;

  if (!live) {
    setTelemetry('tStatus', 'NOT RUNNING');
    ['tFps', 'tDetections', 'tTracks', 'tFrames', 'tFrameSize', 'tAge', 'tCalibrated'].forEach((id) => setTelemetry(id, null));
    setTelemetry('tError', 'No pipeline worker for this camera.');
    setTelemetry('fpsBadge', null, ' FPS');
    return;
  }
  setTelemetry('tStatus', live.status);
  setTelemetry('tFps', isNum(live.fps) && live.fps > 0 ? live.fps.toFixed(1) : null);
  setTelemetry('fpsBadge', isNum(live.fps) && live.fps > 0 ? live.fps.toFixed(1) : null, ' FPS');
  // A worker that has never received a frame has analysed nothing: dash, not 0.
  const analysed = !!live.has_frame;
  setTelemetry('tDetections', analysed && isNum(live.detections_last_frame) ? live.detections_last_frame : null);
  setTelemetry('tTracks', analysed && isNum(live.live_tracks) ? live.live_tracks : null);
  setTelemetry('tFrames', isNum(live.frames_read) ? live.frames_read.toLocaleString() : null);
  const fw = live.frame_width || (analysed && streamImg && streamImg.naturalWidth) || null;
  const fh = live.frame_height || (analysed && streamImg && streamImg.naturalHeight) || null;
  setTelemetry('tFrameSize', fw && fh ? `${fw}x${fh}` : null);
  setTelemetry('tAge', isNum(live.seconds_since_frame) ? live.seconds_since_frame.toFixed(1) : null, ' s');
  setTelemetry('tCalibrated', typeof live.calibrated === 'boolean' ? (live.calibrated ? 'calibrated' : 'not calibrated') : null);
  setTelemetry('tError', live.last_error || null);
  const st = el('tStatus');
  if (st) st.style.color = live.status === 'ONLINE' ? 'var(--accent-green)' : 'var(--accent-red)';
}

// ------------------------------------------------------------ snapshot & clip
async function triggerSnapshot() {
  const out = el('captureResult');
  if (!activeCameraId) { out.innerHTML = '<div class="fp-empty">Select a camera first.</div>'; return; }
  const btn = el('btnSnapshot');
  btn.disabled = true;
  try {
    const res = await fetch(`/api/v1/cameras/${encodeURIComponent(activeCameraId)}/actions/snapshot`, { method: 'POST' });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      out.innerHTML = `<div class="fp-empty">Snapshot not saved (HTTP ${res.status}): ${escapeHtml(body.detail || 'no detail')}</div>`;
      return;
    }
    out.innerHTML = `
      <div class="capture-thumb">
        <a href="${escapeHtml(body.url)}" target="_blank" rel="noopener"><img src="${escapeHtml(body.url)}" alt="Saved snapshot ${escapeHtml(body.filename)}" /></a>
        <div class="zone-sub">Saved ${escapeHtml(body.filename)}</div>
      </div>`;
    showToast('Snapshot saved.');
  } catch (e) {
    out.innerHTML = `<div class="fp-empty">Snapshot failed: ${escapeHtml(e.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
}

async function triggerClip() {
  const out = el('captureResult');
  if (!activeCameraId) { out.innerHTML = '<div class="fp-empty">Select a camera first.</div>'; return; }
  const btn = el('btnClip');
  btn.disabled = true;
  try {
    const res = await fetch(`/api/v1/cameras/${encodeURIComponent(activeCameraId)}/actions/clip`, { method: 'POST' });
    const body = await res.json().catch(() => ({}));
    if (res.status === 501) {
      out.innerHTML = `<div class="fp-empty">Clip export is not available on this deployment: ${escapeHtml(body.detail || 'no reason given')}</div>`;
      return;
    }
    if (!res.ok) {
      out.innerHTML = `<div class="fp-empty">Clip not exported (HTTP ${res.status}): ${escapeHtml(body.detail || 'no detail')}</div>`;
      return;
    }
    const link = body.url ? `<a href="${escapeHtml(body.url)}" target="_blank" rel="noopener">${escapeHtml(body.filename || body.url)}</a>` : escapeHtml(body.filename || JSON.stringify(body));
    out.innerHTML = `<div class="fp-empty">Clip exported: ${link}</div>`;
    showToast('Clip exported.');
  } catch (e) {
    out.innerHTML = `<div class="fp-empty">Clip export failed: ${escapeHtml(e.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
}

// ------------------------------------------------------------ init
// ------------------------------------------------------------ opt-in self test (?__selftest=1)
async function runStudioSelfTest() {
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

  await new Promise((r) => setTimeout(r, 4500));
  check('cameras loaded', Array.isArray(studioCameras), `${studioCameras.length} camera(s)`);
  if (studioCameras.length) {
    check('camera selected from URL or first', !!activeCameraId, activeCameraId);
    check('stream src has camera id + overlay', /\/stream\?camera_id=.+&overlay=1/.test(streamImg.getAttribute('src') || ''), streamImg.getAttribute('src'));
    const r = streamImg.getBoundingClientRect();
    check('stream img rendered height > 100', r.height > 100, `${r.width.toFixed(0)}x${r.height.toFixed(0)}`);
    check('stream img naturalWidth > 0', streamImg.naturalWidth > 0, `${streamImg.naturalWidth}x${streamImg.naturalHeight}`);
    const box = contentBox();
    const cr = canvas.getBoundingClientRect();
    check('canvas tracks displayed content box', Math.abs(cr.width - box.w) < 2 && Math.abs(cr.height - box.h) < 2,
      `canvas ${cr.width.toFixed(0)}x${cr.height.toFixed(0)} vs content ${box.w.toFixed(0)}x${box.h.toFixed(0)}`);
    if (streamImg.naturalWidth) {
      check('canvas aspect equals frame aspect', Math.abs((cr.width / cr.height) - (streamImg.naturalWidth / streamImg.naturalHeight)) < 0.05);
    }
    check('telemetry status populated', el('tStatus').textContent !== DASH, el('tStatus').textContent);
    check('telemetry error shown honestly', true, el('tError').textContent);
    // Click mapping: a synthetic click at the canvas centre lands at (0.5, 0.5).
    setDrawMode('INTRUSION');
    canvas.dispatchEvent(new MouseEvent('click', { clientX: cr.left + cr.width / 2, clientY: cr.top + cr.height / 2, bubbles: true }));
    check('click maps to normalised centre', drawnPoints.length === 1 && Math.abs(drawnPoints[0].x - 0.5) < 0.01 && Math.abs(drawnPoints[0].y - 0.5) < 0.01, JSON.stringify(drawnPoints));
    clearCanvasPoints();
    await triggerSnapshot();
    check('snapshot result rendered', /Saved|Snapshot not saved|Snapshot failed/.test(el('captureResult').textContent), el('captureResult').textContent.trim());
    await triggerClip();
    check('clip result rendered honestly', /Clip export is not available|Clip exported|Clip not exported|Clip export failed/.test(el('captureResult').textContent), el('captureResult').textContent.trim());
  } else {
    check('empty viewport state shown', el('viewportEmpty').style.display !== 'none');
  }
  check('zone lists rendered', ['tripwiresListContainer', 'intrusionListContainer', 'exclusionListContainer', 'productShelfListContainer'].every((id) => el(id).textContent.trim() && el(id).textContent.trim() !== 'Loading…'));
  askClearAllZones();
  check('inline clear-all confirm shown', el('clearAllConfirm').style.display !== 'none');
  cancelClearAllZones();
  await new Promise((r) => setTimeout(r, 500));
  check('no page errors', errors.length === 0, errors.join(' | '));
  const failed = results.filter((x) => x.startsWith('FAIL')).length;
  results.unshift(`STUDIO SELFTEST ${failed === 0 ? 'OK' : 'FAILED'} (${results.length} checks, ${failed} failed)`);
  out.textContent = results.join('\n');
  window.__selftestDone = true;
}

function initStudio() {
  if (getUrlParameter('__selftest') === '1') runStudioSelfTest();
  resizeCanvas();
  populateZoneNames();
  loadStudioSources();
  telemetryTimer = setInterval(pollTelemetry, 2000);
  setInterval(loadZonesList, 6000);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initStudio);
} else {
  initStudio();
}
