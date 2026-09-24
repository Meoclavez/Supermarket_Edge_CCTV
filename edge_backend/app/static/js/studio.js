/**
 * Camera Studio & Zone Editor
 *
 * One camera at a time: its live MJPEG feed with the tracker's real boxes
 * (/stream?camera_id=..&overlay=1), the pipeline's telemetry for that camera,
 * snapshot / clip export, and the per-camera zone editor (tripwires,
 * restricted areas, privacy masks and product shelves).
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
// Tripwire / restricted area being edited in place: { kind: 'TRIPWIRE'|'INTRUSION', id }.
let editingRule = null;
let telemetryTimer = null;

const MODE_STYLE = {
  TRIPWIRE: { stroke: '#00f0ff', fill: 'rgba(0, 240, 255, 0.2)', min: 2, max: 2, label: 'tripwire' },
  INTRUSION: { stroke: '#ff5b6b', fill: 'rgba(255, 91, 107, 0.22)', min: 3, max: Infinity, label: 'restricted area' },
  EXCLUSION: { stroke: '#a0aec0', fill: 'rgba(160, 174, 192, 0.35)', min: 3, max: Infinity, label: 'privacy mask' },
  PRODUCT_SHELF: { stroke: '#ffd700', fill: 'rgba(255, 215, 0, 0.22)', min: 4, max: Infinity, label: 'product shelf' },
};

// Privacy-mask modes, as applied by services/privacy_mask.py on the server.
const MASK_MODES = {
  BLUR: { label: 'Blur', help: 'Blurs this area in every live view, snapshot and clip. People here are still counted and analysed.' },
  MOSAIC: { label: 'Mosaic', help: 'Pixelates this area in every live view, snapshot and clip. People here are still counted and analysed.' },
  BLACKOUT: { label: 'Blackout', help: 'Covers this area with solid black in every live view, snapshot and clip. People here are still counted and analysed.' },
  COLOR: { label: 'Solid colour', help: 'Covers this area with the chosen colour in every live view, snapshot and clip. People here are still counted and analysed.' },
  AI_IGNORE: { label: 'Ignore for analysis', help: 'Video is left untouched, but people standing here are not analysed: no counts, visits, shelf or theft events.' },
};

function hexToBgr(hex) {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex || '');
  return m ? [parseInt(m[3], 16), parseInt(m[2], 16), parseInt(m[1], 16)] : [0, 0, 0];
}
function bgrToHex(bgr) {
  if (!Array.isArray(bgr) || bgr.length !== 3) return '#000000';
  return '#' + [bgr[2], bgr[1], bgr[0]].map((c) => Math.max(0, Math.min(255, c | 0)).toString(16).padStart(2, '0')).join('');
}
function maskSig(masks) {
  return JSON.stringify(masks.map((m) => [m.id, m.name, m.mask_mode, m.mask_color_bgr, (m.points || []).length]));
}
function maskModeOptions(selected) {
  return Object.entries(MASK_MODES)
    .map(([value, m]) => `<option value="${value}"${value === selected ? ' selected' : ''}>${m.label}</option>`).join('');
}

// Draw-time selector: explanation line and colour picker follow the mode.
function onMaskModeChange() {
  const mode = el('maskModeSelect').value;
  const help = el('maskModeHelp');
  if (help) help.textContent = (MASK_MODES[mode] || {}).help || '';
  const showColour = mode === 'COLOR';
  el('maskColorInput').style.display = showColour ? '' : 'none';
  el('maskColorLabel').style.display = showColour ? '' : 'none';
}

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
// Overlay colours are drawn onto the video, so they stay the same in the light
// and dark themes (legible on footage either way); redraw on a switch anyway.
window.addEventListener('edge:theme', () => drawOverlay());
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
    if (currentMode === 'NONE') { setDrawStatus('Pick a tool first (tripwire, restricted area, privacy mask or product shelf).', true); return; }
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
  savedZonesForCamera.exclusion_masks.forEach((z) => (z.mask_mode === 'AI_IGNORE'
    ? drawPolyline(z.points || [], 'rgba(0,240,255,0.6)', 'rgba(0,240,255,0.08)', true, 1.5)
    : drawPolyline(z.points || [], 'rgba(160,174,192,0.55)', 'rgba(160,174,192,0.15)', true, 1.5)));
  savedZonesForCamera.products.forEach((z) => drawPolyline(z.points || [], 'rgba(255,215,0,0.55)', 'rgba(255,215,0,0.10)', true, 1.5));
  drawSavedRules();

  if (!drawnPoints.length || currentMode === 'NONE') return;
  const style = MODE_STYLE[currentMode];
  drawPolyline(drawnPoints, style.stroke, style.fill, currentMode !== 'TRIPWIRE', 2.5);
  if (currentMode === 'TRIPWIRE' && drawnPoints.length === 2) {
    drawInArrow(drawnPoints[0], drawnPoints[1], el('twInSide').value, style.stroke, 'IN');
  }
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
  editingRule = null;
  const row = el('maskModeRow');
  if (row) row.style.display = mode === 'EXCLUSION' ? 'flex' : 'none';
  if (mode === 'EXCLUSION') onMaskModeChange();
  showRuleForm(mode);
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
  editingRule = null;
  showRuleForm('NONE');
  if (el('maskModeRow')) el('maskModeRow').style.display = 'none';
  currentMode = 'NONE';
  drawOverlay();
  const label = el('drawModeLabel');
  if (label) label.textContent = 'mode: none';
  setDrawStatus('Drawing cancelled.', false);
}

// ------------------------------------------------------------ product modal
// Product zones listed for the active camera (id -> zone), and the zone being
// edited (null while mapping a newly drawn polygon).
let productZonesById = {};
let editingProductZone = null;

function openProductModal(zone = null) {
  const modal = el('productModal');
  if (!modal) return;
  editingProductZone = zone;
  const z = zone || {};
  const legacyPlacement = z.shelf_tier === 'ENDCAP' ? 'ENDCAP' : 'SHELF';
  el('modalProductName').value = z.name || '';
  el('modalProductSku').value = z.sku_id || '';
  el('modalProductCategory').value = z.category || '';
  el('modalProductPrice').value = isNum(z.price) && z.price > 0 ? String(z.price) : '';
  el('modalProductFacing').value = isNum(z.facing_count) ? String(z.facing_count) : '';
  el('modalProductLevel').value = z.shelf_level || '';
  el('modalProductValueTier').value = z.value_tier || '';
  el('modalProductTier').value = legacyPlacement;
  const sm = z.study_metrics || {};
  el('chkHandReach').checked = sm.track_hand_reach !== false;
  el('chkDwellTime').checked = sm.track_dwell_time !== false;
  el('chkPutBack').checked = sm.track_put_back_friction !== false;
  el('chkPosSales').checked = !!sm.track_pos_conversion;
  el('chkAbTest').checked = !!sm.ab_test_mode;
  const title = el('productModalTitle');
  if (title) title.textContent = zone ? `🛒 Edit product shelf area: ${zone.name}` : '🛒 Map product shelf area';
  el('productModalStatus').textContent = zone && zone.shelf_level_source === 'derived' && !zone.shelf_level
    ? `Shelf level currently derived from position: ${zone.effective_shelf_level}.` : '';
  modal.style.display = 'flex';
  el('modalProductName').focus();
}
function closeProductModal() {
  const m = el('productModal');
  if (m) m.style.display = 'none';
  editingProductZone = null;
}
function editProductZone(id) {
  const zone = productZonesById[id];
  if (!zone) { showToast('That product area is no longer listed; reloading.'); loadZonesList(); return; }
  openProductModal(zone);
}

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

  const editing = editingProductZone;
  const payload = {
    id: editing ? editing.id : `shelf_${Date.now().toString(36)}`,
    camera_id: editing ? editing.camera_id : activeCameraId,
    name,
    points: editing ? editing.points : drawnPoints,
    sku_id: sku,
    category,
    price: Number.isFinite(price) ? price : 0.0,
    facing_count: Number.isFinite(facing) && facing >= 1 ? facing : 1,
    shelf_tier: el('modalProductTier').value,
    shelf_level: el('modalProductLevel').value || null,
    value_tier: el('modalProductValueTier').value || null,
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
    if (!editing) clearCanvasPoints();
    showToast(editing ? `Updated product area: ${name} (${sku})` : `Mapped product area: ${name} (${sku})`);
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
  if (currentMode === 'TRIPWIRE' || currentMode === 'INTRUSION') { await saveRule(); return; }
  const style = MODE_STYLE[currentMode];
  if (drawnPoints.length < style.min) {
    setDrawStatus(`A ${style.label} needs at least ${style.min} points; ${drawnPoints.length} placed.`, true);
    return;
  }
  if (currentMode === 'PRODUCT_SHELF') { openProductModal(); return; }

  const name = el('zoneNameInput').value.trim() || `${style.label} ${new Date().toLocaleTimeString()}`;
  try {
    if (currentMode === 'EXCLUSION') {
      const mode = el('maskModeSelect').value;
      const body = { name, camera_id: activeCameraId, points: drawnPoints, mask_mode: mode, enabled: true };
      if (mode === 'COLOR') body.mask_color_bgr = hexToBgr(el('maskColorInput').value);
      await postZone('/api/zones/exclusion', body);
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
function deleteExclusion(id) { return deleteZoneAt(`/api/zones/exclusion/${encodeURIComponent(id)}`, 'Mask'); }

// Change an existing mask's mode (and colour) in place; result shown inline on the row.
async function updateMaskMode(id) {
  const row = document.querySelector(`[data-mask-id="${CSS.escape(id)}"]`);
  if (!row) return;
  const select = row.querySelector('.mask-mode-select');
  const colour = row.querySelector('.mask-colour-input');
  const note = row.querySelector('.mask-row-status');
  const mode = select.value;
  colour.style.display = mode === 'COLOR' ? '' : 'none';
  const body = { mask_mode: mode };
  if (mode === 'COLOR') body.mask_color_bgr = hexToBgr(colour.value);
  select.disabled = true;
  note.textContent = 'Saving…';
  note.className = 'mask-row-status form-status';
  try {
    const res = await fetch(`/api/zones/exclusion/${encodeURIComponent(id)}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${JSON.stringify(data.detail || data)}`);
    const saved = data.exclusion_mask || {};
    note.textContent = `Saved: ${(MASK_MODES[saved.mask_mode] || {}).help || saved.mask_mode}`;
    const mask = savedZonesForCamera.exclusion_masks.find((m) => m.id === id);
    if (mask) Object.assign(mask, saved);
    // The row already shows the saved state; keep the periodic refresh from
    // rebuilding it and wiping this confirmation.
    const container = el('exclusionListContainer');
    if (container) container.dataset.sig = maskSig(savedZonesForCamera.exclusion_masks);
    drawOverlay();
  } catch (e) {
    note.textContent = `Mode not changed: ${e.message}`;
    note.className = 'mask-row-status form-status form-status-error';
  } finally {
    select.disabled = false;
  }
}
function deleteProductZone(id) { return deleteZoneAt(`/api/v1/analytics/products/zones/${encodeURIComponent(id)}`, 'Product area'); }

function askClearAllZones() { el('btnClearAllAsk').style.display = 'none'; el('clearAllConfirm').style.display = 'inline-flex'; }
function cancelClearAllZones() { el('btnClearAllAsk').style.display = ''; el('clearAllConfirm').style.display = 'none'; }
async function confirmClearAllZones() {
  try {
    // Only privacy masks: tripwires and restricted areas have their own delete buttons.
    const res = await fetch('/api/zones/clear?kinds=exclusion_masks', { method: 'POST' });
    showToast(res.ok ? 'All privacy masks cleared.' : `Clear failed (HTTP ${res.status})`);
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
    productZonesById = {};
    const LEVEL = { TOP: 'top shelf', MIDDLE: 'eye level', BOTTOM: 'bottom shelf' };
    products.forEach((pz) => {
      productZonesById[pz.id] = pz;
      const lvl = pz.effective_shelf_level
        ? `${LEVEL[pz.effective_shelf_level] || pz.effective_shelf_level}${pz.shelf_level_source === 'operator' ? '' : ' (auto)'}`
        : 'level unknown';
      const sub = `${escapeHtml(pz.sku_id)} · ${isNum(pz.price) && pz.price > 0 ? '$' + pz.price.toFixed(2) : 'no price'} · ${escapeHtml(lvl)}`
        + `${pz.effective_value_tier ? ' · ' + escapeHtml(pz.effective_value_tier.toLowerCase()) : ''}`
        + `${pz.shelf_tier === 'ENDCAP' ? ' · endcap' : ''} · ${escapeHtml(pz.category)}`;
      const id = escapeHtml(pz.id);
      prodContainer.insertAdjacentHTML('beforeend', `
        <div class="zone-item" style="border-color: rgba(var(--orange-rgb), 0.3)" data-product-zone="${id}">
          <div class="zone-info">
            <span class="zone-name">🛒 ${escapeHtml(pz.name)}</span>
            <span class="zone-sub">${sub}</span>
          </div>
          <span style="display:flex; gap:6px; flex-shrink:0;">
            <button type="button" class="btn btn-sm" onclick="editProductZone('${id}')" title="Edit product, SKU, shelf level">✏️</button>
            <button type="button" class="btn btn-danger btn-sm" onclick="deleteProductZone('${id}')" title="Remove">🗑️</button>
          </span>
        </div>`);
    });
  }

  const data = await getJSON('/api/zones', null);
  const exclusion = ((data && data.exclusion_masks) || []).filter(forCam);
  const tripwires = ((data && data.tripwires) || []).filter(forCam);
  const intrusion = ((data && data.intrusion_zones) || []).filter(forCam);
  renderRuleLists(data === null, tripwires, intrusion);

  const fill = (id, items, emptyMsg, render) => {
    const c = el(id);
    if (!c) return;
    if (data === null) { c.innerHTML = '<div class="fp-empty">Zones unavailable: the zone service did not respond.</div>'; return; }
    c.innerHTML = items.length ? items.map(render).join('') : `<div class="fp-empty">${emptyMsg}</div>`;
  };
  // The list refreshes every few seconds; do not rebuild it under an operator
  // who is changing a mask's mode, or when nothing changed.
  const exContainer = el('exclusionListContainer');
  const exSig = maskSig(exclusion);
  const busy = exContainer && exContainer.contains(document.activeElement) && document.activeElement !== document.body;
  if (exContainer && !busy && (data === null || exContainer.dataset.sig !== exSig)) {
    fill('exclusionListContainer', exclusion, 'No privacy masks on this camera.', (ex) => {
      const mode = MASK_MODES[ex.mask_mode] ? ex.mask_mode : 'BLUR';
      const id = escapeHtml(ex.id);
      return `
        <div class="zone-item" data-mask-id="${id}" style="flex-wrap: wrap;">
          <div class="zone-info">
            <span class="zone-name">🌫️ ${escapeHtml(ex.name)}</span>
            <span class="zone-sub">${(ex.points || []).length} vertices</span>
          </div>
          <label class="sr-only" for="maskMode_${id}" style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0);">Mode for ${escapeHtml(ex.name)}</label>
          <select id="maskMode_${id}" class="form-select mask-mode-select" style="max-width: 170px; font-size: 11px;" onchange="updateMaskMode('${id}')">${maskModeOptions(mode)}</select>
          <input type="color" class="mask-colour-input" aria-label="Mask colour" value="${bgrToHex(ex.mask_color_bgr)}" style="${mode === 'COLOR' ? '' : 'display:none;'}" onchange="updateMaskMode('${id}')" />
          <button type="button" class="btn btn-danger btn-sm" onclick="deleteExclusion('${id}')" title="Remove">🗑️</button>
          <div class="mask-row-status form-status" aria-live="polite" style="flex-basis: 100%; margin-top: 2px;">${escapeHtml((MASK_MODES[mode] || {}).help || '')}</div>
        </div>`;
    });
    exContainer.dataset.sig = data === null ? '' : exSig;
  }

  savedZonesForCamera = { tripwires, intrusion_zones: intrusion, exclusion_masks: exclusion, products };
  drawOverlay();
}

// ------------------------------------------------------------ tripwires & restricted areas
// Geometry is normalised 0..1 to the camera frame, like masks. A tripwire's
// "in" side is left/right of start -> end as drawn on screen (y down); the
// arrow on the canvas points into it. Settings are edited inline, never via
// prompt()/confirm().

const DAY_KEYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
const DAY_LABELS = { mon: 'Mon', tue: 'Tue', wed: 'Wed', thu: 'Thu', fri: 'Fri', sat: 'Sat', sun: 'Sun' };

function drawInArrow(a, b, inSide, colour, label) {
  const W = canvas.width, H = canvas.height;
  const ax = a.x * W, ay = a.y * H, bx = b.x * W, by = b.y * H;
  const len = Math.hypot(bx - ax, by - ay);
  if (len < 4) return;
  // Right-hand normal of A->B on screen is (-dy, dx); left is its negative.
  const sgn = inSide === 'left' ? -1 : 1;
  const nx = (-(by - ay) / len) * sgn, ny = ((bx - ax) / len) * sgn;
  const mx = (ax + bx) / 2, my = (ay + by) / 2;
  const tipX = mx + nx * 28, tipY = my + ny * 28;
  ctx.strokeStyle = colour; ctx.fillStyle = colour; ctx.lineWidth = 2.5;
  ctx.beginPath(); ctx.moveTo(mx, my); ctx.lineTo(tipX, tipY); ctx.stroke();
  const hx = tipX - nx * 9, hy = tipY - ny * 9;
  ctx.beginPath();
  ctx.moveTo(tipX, tipY);
  ctx.lineTo(hx + ny * 6, hy - nx * 6);
  ctx.lineTo(hx - ny * 6, hy + nx * 6);
  ctx.closePath(); ctx.fill();
  if (label) {
    ctx.font = 'bold 11px sans-serif';
    ctx.fillText(label, tipX + nx * 6 - 6, tipY + ny * 6 + 4);
  }
}

function drawLabel(text, x, y, colour) {
  ctx.font = 'bold 11px sans-serif';
  const w = ctx.measureText(text).width + 8;
  ctx.fillStyle = 'rgba(5,7,11,0.75)';
  ctx.fillRect(x - 2, y - 12, w, 16);
  ctx.fillStyle = colour;
  ctx.fillText(text, x + 2, y);
}

function drawSavedRules() {
  savedZonesForCamera.intrusion_zones.forEach((z) => {
    const editing = editingRule && editingRule.id === z.id;
    const on = z.enabled !== false;
    drawPolyline(z.points || [], editing ? '#ffffff' : (on ? 'rgba(255,91,107,0.85)' : 'rgba(255,91,107,0.35)'),
      'rgba(255,91,107,0.10)', true, editing ? 3 : 1.8);
    const p = (z.points || [])[0];
    if (p) drawLabel(`⛔ ${z.name || 'Restricted area'}${on ? '' : ' (off)'}`, p.x * canvas.width + 4, p.y * canvas.height + 14, '#ff8a95');
  });
  savedZonesForCamera.tripwires.forEach((tw) => {
    const a = { x: tw.x1, y: tw.y1 }, b = { x: tw.x2, y: tw.y2 };
    const editing = editingRule && editingRule.id === tw.id;
    const on = tw.enabled !== false;
    const colour = editing ? '#ffffff' : (on ? '#00f0ff' : 'rgba(0,240,255,0.4)');
    drawPolyline([a, b], colour, 'transparent', false, editing ? 3.5 : 2.2);
    drawInArrow(a, b, tw.in_side || 'right', colour, 'IN');
    drawLabel(`🚪 ${tw.name || 'Tripwire'}${on ? '' : ' (off)'}`, a.x * canvas.width + 4, a.y * canvas.height - 6, '#7ff8ff');
  });
}

function showRuleForm(mode) {
  const tw = el('tripwireFormRow'), ra = el('restrictedFormRow');
  if (tw) tw.style.display = mode === 'TRIPWIRE' ? 'flex' : 'none';
  if (ra) ra.style.display = mode === 'INTRUSION' ? 'flex' : 'none';
  if (mode === 'TRIPWIRE' && !editingRule) fillTripwireForm(null);
  if (mode === 'INTRUSION' && !editingRule) fillRestrictedForm(null);
  setRuleStatus('tripwireFormStatus', '');
  setRuleStatus('restrictedFormStatus', '');
}

function setRuleStatus(id, msg, isError) {
  const n = el(id);
  if (!n) return;
  n.textContent = msg;
  n.classList.toggle('form-status-error', !!isError);
}

function syncRuleForms() {
  const alertOn = el('twAlertEnabled') && el('twAlertEnabled').checked;
  if (el('twAlertDirection')) el('twAlertDirection').disabled = !alertOn;
  if (el('twSeverity')) el('twSeverity').disabled = !alertOn;
  const rows = document.querySelectorAll('#raScheduleRows .sched-row').length;
  const mode = el('raScheduleMode') ? el('raScheduleMode').value : 'restricted_during';
  const help = el('raScheduleHelp');
  if (help) {
    help.textContent = rows === 0
      ? 'No time windows: restricted at all times.'
      : (mode === 'allowed_during' ? 'Alerts outside these windows.' : 'Alerts only inside these windows.');
  }
}

function flipTripwireSide() {
  const s = el('twInSide');
  s.value = s.value === 'right' ? 'left' : 'right';
  drawOverlay();
}

function fillTripwireForm(tw) {
  el('tripwireFormTitle').textContent = tw ? `Editing tripwire: ${tw.name || tw.id}` : 'New tripwire';
  el('twInSide').value = (tw && tw.in_side) || 'right';
  el('twCountsFootfall').checked = tw ? tw.counts_footfall !== false : true;
  el('twAlertEnabled').checked = !!(tw && tw.alert_enabled);
  el('twAlertDirection').value = (tw && tw.alert_direction) || 'in';
  el('twSeverity').value = (tw && tw.severity) || 'WARNING';
  el('twEnabled').checked = tw ? tw.enabled !== false : true;
  el('btnSaveTripwire').textContent = tw ? '💾 Save changes' : '💾 Save tripwire';
  syncRuleForms();
}

function scheduleRowHtml(row, idx) {
  const days = new Set((row && row.days) || ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']);
  const dayBoxes = DAY_KEYS.map((d) => `
      <label class="sched-day" for="raDay_${idx}_${d}"><input type="checkbox" id="raDay_${idx}_${d}" data-day="${d}"${days.has(d) ? ' checked' : ''} />${DAY_LABELS[d]}</label>`).join('');
  return `
    <div class="sched-row" data-idx="${idx}">
      <div class="sched-days">${dayBoxes}</div>
      <div class="rule-inline">
        <label for="raFrom_${idx}" class="zone-sub">From</label>
        <input type="time" id="raFrom_${idx}" class="form-input sched-from" value="${escapeHtml((row && row.from) || '22:00')}" />
        <label for="raTo_${idx}" class="zone-sub">to</label>
        <input type="time" id="raTo_${idx}" class="form-input sched-to" value="${escapeHtml((row && row.to && row.to !== '24:00') ? row.to : (row && row.to === '24:00' ? '00:00' : '07:00'))}" />
        <button type="button" class="btn btn-danger btn-sm sched-remove" onclick="removeScheduleRow(this)" title="Remove this time window">✕</button>
      </div>
    </div>`;
}

let scheduleRowSeq = 0;
function addScheduleRow(row) {
  const host = el('raScheduleRows');
  if (!host) return;
  host.insertAdjacentHTML('beforeend', scheduleRowHtml(row || null, scheduleRowSeq++));
  syncRuleForms();
}
function removeScheduleRow(btn) {
  const r = btn.closest('.sched-row');
  if (r) r.remove();
  syncRuleForms();
}

function fillRestrictedForm(area) {
  el('restrictedFormTitle').textContent = area ? `Editing restricted area: ${area.name || area.id}` : 'New restricted area';
  el('raScheduleRows').innerHTML = '';
  ((area && area.schedule) || []).forEach((r) => addScheduleRow(r));
  el('raScheduleMode').value = (area && area.schedule_mode) || 'restricted_during';
  el('raMinDwell').value = area && Number.isFinite(area.min_dwell_seconds) ? area.min_dwell_seconds : 3;
  el('raSeverity').value = (area && area.severity) || 'HIGH';
  el('raTimezone').value = (area && area.timezone) || '';
  el('raEnabled').checked = area ? area.enabled !== false : true;
  el('btnSaveRestricted').textContent = area ? '💾 Save changes' : '💾 Save restricted area';
  syncRuleForms();
}

function readSchedule() {
  const rows = [];
  const problems = [];
  document.querySelectorAll('#raScheduleRows .sched-row').forEach((r, i) => {
    const days = [...r.querySelectorAll('input[data-day]')].filter((c) => c.checked).map((c) => c.getAttribute('data-day'));
    const from = r.querySelector('.sched-from').value;
    let to = r.querySelector('.sched-to').value;
    if (!days.length) problems.push(`time window ${i + 1} has no days ticked`);
    if (!from || !to) problems.push(`time window ${i + 1} needs both times`);
    if (to === '00:00' && from !== '00:00') to = '24:00';
    rows.push({ days, from, to });
  });
  return { rows, problems };
}

function formatApiError(data, status) {
  const d = data && data.detail;
  if (Array.isArray(d)) {
    return d.map((e) => `${(e.loc || []).filter((x) => x !== 'body').join('.') || 'input'}: ${e.msg}`).join('; ');
  }
  return typeof d === 'string' ? d : `HTTP ${status}`;
}

async function sendRule(method, url, body) {
  const res = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(formatApiError(data, res.status));
  return data;
}

async function saveRule() {
  const isWire = currentMode === 'TRIPWIRE';
  const statusId = isWire ? 'tripwireFormStatus' : 'restrictedFormStatus';
  const style = MODE_STYLE[currentMode];
  const editing = editingRule && editingRule.kind === currentMode ? editingRule : null;
  const redrawn = drawnPoints.length >= style.min;
  if (!editing && !redrawn) {
    setRuleStatus(statusId, `Click ${style.min === 2 ? 'two points' : 'at least three points'} on the video first (${drawnPoints.length} placed).`, true);
    return;
  }
  const name = el('zoneNameInput').value.trim();
  let body;
  if (isWire) {
    body = {
      in_side: el('twInSide').value,
      counts_footfall: el('twCountsFootfall').checked,
      alert_enabled: el('twAlertEnabled').checked,
      alert_direction: el('twAlertDirection').value,
      severity: el('twSeverity').value,
      enabled: el('twEnabled').checked,
    };
    if (redrawn) Object.assign(body, { x1: drawnPoints[0].x, y1: drawnPoints[0].y, x2: drawnPoints[1].x, y2: drawnPoints[1].y });
  } else {
    const sched = readSchedule();
    if (sched.problems.length) { setRuleStatus(statusId, `Not saved: ${sched.problems.join('; ')}.`, true); return; }
    const dwell = Number(el('raMinDwell').value);
    body = {
      schedule: sched.rows,
      schedule_mode: el('raScheduleMode').value,
      min_dwell_seconds: Number.isFinite(dwell) ? dwell : el('raMinDwell').value,
      severity: el('raSeverity').value,
      timezone: el('raTimezone').value.trim() || null,
      enabled: el('raEnabled').checked,
    };
    if (redrawn) body.points = drawnPoints;
  }
  if (name) body.name = name;
  const kindUrl = isWire ? 'tripwire' : 'intrusion';
  setRuleStatus(statusId, 'Saving…');
  try {
    let saved;
    if (editing) {
      const data = await sendRule('PATCH', `/api/zones/${kindUrl}/${encodeURIComponent(editing.id)}`, body);
      saved = isWire ? data.tripwire : data.intrusion_zone;
    } else {
      body.camera_id = activeCameraId;
      if (!body.name) body.name = isWire ? 'Tripwire' : 'Restricted area';
      const data = await sendRule('POST', `/api/zones/${kindUrl}`, body);
      saved = isWire ? data.tripwire : data.intrusion_zone;
    }
    showToast(`Saved ${style.label}: ${saved && saved.name}`);
    el('zoneNameInput').value = '';
    clearCanvasPoints();
    setDrawStatus(`Saved ${style.label} "${saved && saved.name}".`, false);
    await loadZonesList();
  } catch (err) {
    setRuleStatus(statusId, `Not saved: ${err.message}`, true);
  }
}

function editRule(kind, id) {
  const list = kind === 'TRIPWIRE' ? savedZonesForCamera.tripwires : savedZonesForCamera.intrusion_zones;
  const rule = list.find((r) => r.id === id);
  if (!rule) { showToast('That rule no longer exists.'); loadZonesList(); return; }
  setDrawMode(kind);
  editingRule = { kind, id };
  el('zoneNameInput').value = rule.name || '';
  if (kind === 'TRIPWIRE') fillTripwireForm(rule); else fillRestrictedForm(rule);
  const label = el('drawModeLabel');
  if (label) label.textContent = `mode: editing ${MODE_STYLE[kind].label}`;
  setDrawStatus(`Editing "${rule.name || id}". Change its settings and press Save; click new points on the video only if you want to move it.`, false);
  drawOverlay();
  const form = el(kind === 'TRIPWIRE' ? 'tripwireFormRow' : 'restrictedFormRow');
  if (form && form.scrollIntoView) form.scrollIntoView({ block: 'nearest' });
}

function askDeleteRule(btn) {
  const row = btn.closest('.zone-item');
  row.querySelector('.rule-actions').style.display = 'none';
  row.querySelector('.rule-confirm').style.display = 'inline-flex';
}
function cancelDeleteRule(btn) {
  const row = btn.closest('.zone-item');
  row.querySelector('.rule-confirm').style.display = 'none';
  row.querySelector('.rule-actions').style.display = 'inline-flex';
}
async function confirmDeleteRule(kind, id) {
  if (editingRule && editingRule.id === id) clearCanvasPoints();
  await deleteZoneAt(`/api/zones/${kind === 'TRIPWIRE' ? 'tripwire' : 'intrusion'}/${encodeURIComponent(id)}`,
    kind === 'TRIPWIRE' ? 'Tripwire' : 'Restricted area');
}

function scheduleSummary(z) {
  const rows = z.schedule || [];
  if (!rows.length) return 'always restricted';
  const txt = rows.map((r) => `${(r.days || []).map((d) => DAY_LABELS[d] || d).join(',')} ${r.from}–${r.to}`).join('; ');
  return z.schedule_mode === 'allowed_during' ? `allowed ${txt}, restricted otherwise` : `restricted ${txt}`;
}

function ruleRow(kind, rule, icon, sub) {
  const id = escapeHtml(rule.id);
  return `
    <div class="zone-item" data-rule-id="${id}">
      <div class="zone-info">
        <span class="zone-name">${icon} ${escapeHtml(rule.name || rule.id)}${rule.enabled === false ? ' <span class="badge">off</span>' : ''}</span>
        <span class="zone-sub">${sub}</span>
      </div>
      <span class="rule-actions">
        <button type="button" class="btn btn-sm rule-edit" onclick="editRule('${kind}', '${id}')" title="Edit">✎ Edit</button>
        <button type="button" class="btn btn-danger btn-sm rule-delete" onclick="askDeleteRule(this)" title="Delete">🗑️</button>
      </span>
      <span class="rule-confirm" style="display:none;">
        <span class="zone-sub">Delete?</span>
        <button type="button" class="btn btn-danger btn-sm rule-delete-yes" onclick="confirmDeleteRule('${kind}', '${id}')">Delete</button>
        <button type="button" class="btn btn-sm rule-delete-no" onclick="cancelDeleteRule(this)">Keep</button>
      </span>
    </div>`;
}

function renderRuleLists(unavailable, tripwires, intrusion) {
  const render = (containerId, items, emptyMsg, rowFn) => {
    const c = el(containerId);
    if (!c) return;
    // Do not rebuild under an open delete confirmation or when nothing changed.
    if (c.querySelector('.rule-confirm[style*="inline-flex"]')) return;
    const sig = unavailable ? 'x' : JSON.stringify(items);
    if (c.dataset.sig === sig) return;
    c.dataset.sig = sig;
    if (unavailable) { c.innerHTML = '<div class="fp-empty">Zones unavailable: the zone service did not respond.</div>'; return; }
    c.innerHTML = items.length ? items.map(rowFn).join('') : `<div class="fp-empty">${emptyMsg}</div>`;
  };
  render('tripwiresListContainer', tripwires, 'No tripwires on this camera. Draw one across an entrance with the Tripwire tool.',
    (tw) => ruleRow('TRIPWIRE', tw, '🚪',
      `in = ${escapeHtml(tw.in_side || 'right')} side · ${tw.counts_footfall === false ? 'not footfall' : 'footfall'} · ` +
      (tw.alert_enabled ? `alert ${escapeHtml(tw.alert_direction || 'in')} (${escapeHtml(tw.severity || 'WARNING')})` : 'no alert')));
  render('intrusionListContainer', intrusion, 'No restricted areas on this camera. Draw one with the Restricted area tool.',
    (z) => ruleRow('INTRUSION', z, '⛔',
      `${escapeHtml(scheduleSummary(z))} · dwell ${escapeHtml(z.min_dwell_seconds ?? 0)} s · ${escapeHtml(z.severity || 'HIGH')}` +
      (z.timezone ? ` · ${escapeHtml(z.timezone)}` : '')));
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
  out.className = 'selftest-results';
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
    setDrawMode('EXCLUSION');
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
  check('zone lists rendered', ['exclusionListContainer', 'productShelfListContainer', 'tripwiresListContainer', 'intrusionListContainer'].every((id) => el(id).textContent.trim() && el(id).textContent.trim() !== 'Loading…'));
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

// Start only after auth.js has checked the stored token (edgeAuth.onReady).
if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') {
  window.edgeAuth.onReady(initStudio);
} else if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initStudio);
} else {
  initStudio();
}
