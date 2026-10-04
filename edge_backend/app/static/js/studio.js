/**
 * Camera Studio & Zone Editor
 *
 * One camera at a time: its live MJPEG feed with the tracker's real boxes
 * (/stream?camera_id=..&overlay=1), the pipeline's telemetry for that camera,
 * snapshot / clip export, and the per-camera zone editor (tripwires,
 * restricted areas, checkout / queue areas, privacy masks, ignore areas and
 * product shelves).
 *
 * Ignore areas are exclusion masks with mask_mode AI_IGNORE: the video is not
 * changed, the AI drops detections inside them (feet inside, or at least
 * ignore_box_fraction of the box). They have their own tool and list, and can
 * be made in one step by clicking a live detected box ("Not a person").
 *
 * Deep links: ?camera_id= (alias ?camera=) picks the camera, ?tool=
 * tripwire|restricted|mask|ignore|product|checkout (+ ?kind=checkout|queue)
 * opens that tool, and ?from=checklist shows a way back to the setup checklist.
 *
 * Zone coordinates are stored normalised (0..1) against the camera's own
 * frame. The feed is displayed with object-fit: contain, so a click has to
 * be mapped through the letterboxed content box, computed from the image's
 * naturalWidth/naturalHeight, before it is normalised.
 *
 * Direct video (WebRtcLive transport 'webrtc', always through the online-
 * access tunnel, which never carries camera pixels): the feed is a <video>
 * backed by one direct WebRTC session (js/webrtc_live.js) instead of MJPEG.
 * It is raw camera video, without the detector's boxes. The session closes
 * when the tab is hidden or the page is left, and reopens when visible.
 */

'use strict';

const DASH = '—';
const canvas = document.getElementById('interactiveCanvas');
const ctx = canvas ? canvas.getContext('2d') : null;
const streamImg = document.getElementById('streamImg');
const streamVideo = document.getElementById('streamVideo');
const viewport = document.getElementById('viewportWrapper');
let studioRtc = null;         // direct-video session of the camera on screen (remote mode)
let studioRtcRetry = null;

let currentMode = 'NONE';
let drawnPoints = [];
let activeCameraId = null;
let studioCameras = [];
let savedZonesForCamera = { tripwires: [], intrusion_zones: [], exclusion_masks: [], products: [], queue_zones: [] };
// Tripwire / restricted area / queue area being edited in place: { kind: 'TRIPWIRE'|'INTRUSION'|'QUEUE', id }.
let editingRule = null;
let telemetryTimer = null;
// Live detected boxes of the camera on screen (GET .../live-status `boxes`), refreshed about once a second.
let liveBoxes = [];
let liveBoxesCamera = null;
let liveCache = null;          // { cameraId, at, data }: the newest live-status, shared with the telemetry panel
let boxTimer = null;
let selectedBox = null;        // { track_id, box } shown in the popover

const MODE_STYLE = {
  TRIPWIRE: { stroke: '#00f0ff', fill: 'rgba(0, 240, 255, 0.2)', min: 2, max: 2, label: 'tripwire' },
  INTRUSION: { stroke: '#ff5b6b', fill: 'rgba(255, 91, 107, 0.22)', min: 3, max: Infinity, label: 'restricted area' },
  EXCLUSION: { stroke: '#a0aec0', fill: 'rgba(160, 174, 192, 0.35)', min: 3, max: Infinity, label: 'privacy mask' },
  PRODUCT_SHELF: { stroke: '#ffd700', fill: 'rgba(255, 215, 0, 0.22)', min: 3, max: Infinity, label: 'product shelf' },
  QUEUE: { stroke: '#c084fc', fill: 'rgba(192, 132, 252, 0.22)', min: 3, max: Infinity, label: 'checkout / queue area' },
  IGNORE: { stroke: '#2dd4bf', fill: 'rgba(45, 212, 191, 0.22)', min: 3, max: Infinity, label: 'ignore area' },
};

// URL ?tool= value <-> draw mode, and the plain-language line under the tool buttons.
const TOOL_MODES = { tripwire: 'TRIPWIRE', restricted: 'INTRUSION', mask: 'EXCLUSION', ignore: 'IGNORE', product: 'PRODUCT_SHELF', checkout: 'QUEUE' };
const QUEUE_HELP = 'Draw around where customers stand at a till (Checkout lane) or wait in line (Queue line). The system times how long people stay there; it works without calibrating the camera.';
const IGNORE_HELP = 'The AI does not look for people here. Use it for posters, mannequins, TV screens or a mirror. The video is not changed.';
const TOOL_HELP = {
  NONE: 'Choose a tool above to see what it does. Is something that is not a person boxed on the picture? Click its box.',
  TRIPWIRE: 'Draw a line across a doorway; people crossing it are counted in or out.',
  INTRUSION: 'Draw around a staff-only area; people inside during its restricted hours raise an alert.',
  EXCLUSION: 'Hide part of the picture for privacy (blur, mosaic, blackout or a solid colour). People there are still counted. To stop the AI seeing a poster or mannequin, use Ignore area instead.',
  IGNORE: IGNORE_HELP,
  PRODUCT_SHELF: 'Draw around a shelf to count hand reaches into it.',
  QUEUE: QUEUE_HELP,
};
const QUEUE_KIND_LABEL = { checkout: 'Checkout lane', queue: 'Queue line' };

// Privacy-mask modes, as applied by services/privacy_mask.py on the server.
const MASK_MODES = {
  BLUR: { label: 'Blur', help: 'Blurs this area in every live view, snapshot and clip. People here are still counted and analysed.' },
  MOSAIC: { label: 'Mosaic', help: 'Pixelates this area in every live view, snapshot and clip. People here are still counted and analysed.' },
  BLACKOUT: { label: 'Blackout', help: 'Covers this area with solid black in every live view, snapshot and clip. People here are still counted and analysed.' },
  COLOR: { label: 'Solid colour', help: 'Covers this area with the chosen colour in every live view, snapshot and clip. People here are still counted and analysed.' },
};
// Masks with this mode are ignore areas: listed and edited in their own card, never as privacy masks.
const IGNORE_MODE = 'AI_IGNORE';
function isIgnoreMask(m) { return !!m && m.mask_mode === IGNORE_MODE; }
// "How much of a person must be inside" -> ignore_box_fraction.
const IGNORE_COVERAGE = [
  { value: 0.6, label: 'Most of them (60%)' },
  { value: 0.5, label: 'Half (50%)' },
  { value: 0.1, label: 'Any part touching (10%)' },
];
const IGNORE_COVERAGE_DEFAULT = 0.6;
function coverageLabel(v) {
  if (!isNum(v)) return null;
  const c = IGNORE_COVERAGE.find((o) => Math.abs(o.value - v) < 0.001);
  return c ? c.label : `Custom (${Math.round(v * 100)}%)`;
}
/** Options for a saved area: a value the server does not report shows as a dash, never a guess. */
function coverageOptions(v) {
  let html = '';
  if (!isNum(v)) html += `<option value="" selected disabled>${DASH} (not reported by this server)</option>`;
  else if (!IGNORE_COVERAGE.some((o) => Math.abs(o.value - v) < 0.001)) html += `<option value="${v}" selected>${escapeHtml(coverageLabel(v))}</option>`;
  return html + IGNORE_COVERAGE.map((o) => `<option value="${o.value}"${isNum(v) && Math.abs(o.value - v) < 0.001 ? ' selected' : ''}>${o.label}</option>`).join('');
}

function hexToBgr(hex) {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex || '');
  return m ? [parseInt(m[3], 16), parseInt(m[2], 16), parseInt(m[1], 16)] : [0, 0, 0];
}
function bgrToHex(bgr) {
  if (!Array.isArray(bgr) || bgr.length !== 3) return '#000000';
  return '#' + [bgr[2], bgr[1], bgr[0]].map((c) => Math.max(0, Math.min(255, c | 0)).toString(16).padStart(2, '0')).join('');
}
function maskSig(masks) {
  return JSON.stringify(masks.map((m) => [m.id, m.name, m.mask_mode, m.mask_color_bgr, (m.points || []).length, m.enabled, m.ignore_box_fraction]));
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

// ------------------------------------------------------------ setup checklist return (?from=checklist)
function fromChecklist() { return getUrlParameter('from') === 'checklist'; }
function checklistCameraId() { return activeCameraId || getUrlParameter('camera_id') || getUrlParameter('camera') || ''; }
function checklistUrl() { return `/dashboard?checklist=${encodeURIComponent(checklistCameraId())}#cameras`; }
function syncChecklistLink() {
  const a = el('backToChecklist');
  if (!a) return;
  const on = fromChecklist();
  a.style.display = on ? '' : 'none';
  if (on) a.href = checklistUrl();
}
// Success line for any saved tool; adds the way back when opened from the checklist.
function announceSaved(msg) {
  setDrawStatus(msg, false);
  if (!fromChecklist()) return;
  const s = el('drawStatus');
  if (!s) return;
  const line = document.createElement('span');
  line.className = 'studio-saved-checklist';
  line.append(' Saved. ');
  const a = document.createElement('a');
  a.href = checklistUrl();
  a.className = 'studio-status-link';
  a.id = 'savedBackToChecklist';
  a.textContent = 'Back to the checklist';
  line.append(a, ' to see it ticked off.');
  s.appendChild(line);
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
  const { w: nw, h: nh } = frameSize();
  if (!nw || !nh || !W || !H) return { ox: 0, oy: 0, w: W, h: H };
  const scale = Math.min(W / nw, H / nh);
  const w = nw * scale;
  const h = nh * scale;
  return { ox: (W - w) / 2, oy: (H - h) / 2, w, h };
}

/** Native size of the picture on screen: the direct video's, else the MJPEG image's. */
function frameSize() {
  if (studioDirect() && streamVideo && streamVideo.videoWidth) return { w: streamVideo.videoWidth, h: streamVideo.videoHeight };
  return { w: (streamImg && streamImg.naturalWidth) || 0, h: (streamImg && streamImg.naturalHeight) || 0 };
}

function studioDirect() {
  return !!(window.WebRtcLive && window.WebRtcLive.transportNow() === 'webrtc');
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
  const pop = el('boxPopover');
  if (pop && !pop.hidden && selectedBox) placeBoxPopover(pop, selectedBox);
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
    if (studioDirect() || !streamImg.getAttribute('src')) return;   // direct video: no MJPEG to retry
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

if (streamVideo) {
  let lastVideoSize = '';
  const onVideoSize = () => {
    const sig = `${streamVideo.videoWidth}x${streamVideo.videoHeight}`;
    if (streamVideo.videoWidth) setViewportEmpty('');
    if (sig !== lastVideoSize) { lastVideoSize = sig; resizeCanvas(); }
  };
  streamVideo.addEventListener('loadedmetadata', onVideoSize);
  streamVideo.addEventListener('resize', onVideoSize);
}

/** Direct video badge in the header: honest connection state. */
function setTransportBadge(text, kind, tip) {
  const b = el('liveTransportBadge');
  if (!b) return;
  b.hidden = !text;
  b.textContent = text || '';
  b.title = tip || '';
  b.classList.toggle('badge-green', kind === 'ok');
  b.classList.toggle('badge-warning', kind === 'wait');
  b.classList.toggle('badge-danger', kind === 'err');
}

function closeStudioVideo(reason) {
  clearTimeout(studioRtcRetry);
  studioRtcRetry = null;
  const h = studioRtc;
  studioRtc = null;
  if (h) h.close(reason || 'closed');
}

/** Open the camera's direct video on the <video> (remote mode). */
function openStudioVideo(cameraId) {
  closeStudioVideo('switch-camera');
  if (!streamVideo || !window.WebRtcLive || document.hidden) return;
  setViewportEmpty('Opening a direct video connection to the store…');
  setTransportBadge('Connecting…', 'wait', 'Opening a direct video connection to the store');
  const h = window.WebRtcLive.open(cameraId, streamVideo, { purpose: 'focus' });
  studioRtc = h;
  h.on('state', (state) => {
    if (studioRtc !== h) return;
    if (state === 'connected') {
      const via = window.WebRtcLive.pairLabel(h.pair);
      setTransportBadge(`Direct${via ? ` · ${via}` : ''}`, 'ok',
        'Direct peer-to-peer video; it does not pass through the online-access server. Raw camera video: the detector boxes are shown only on the store network.');
      if (!streamVideo.videoWidth) setViewportEmpty('Connected. Waiting for the first picture…');
    } else if (state === 'failed') {
      studioRtc = null;
      const e = h.error || {};
      const title = e.network ? 'Direct video not possible from this network' : (e.message || 'Direct video failed');
      setTransportBadge(title, 'err', e.detail || '');
      setViewportHtml(`<div class="studio-rtc-fail">
          <div class="studio-rtc-fail-title">${escapeHtml(title)}</div>
          <div>${escapeHtml(e.detail || '')}</div>
          <div class="studio-rtc-fail-actions">
            <button type="button" class="btn btn-sm" onclick="studioRetryVideo()">Try again</button>
            <a class="btn btn-sm btn-primary" href="/dashboard#settings">Check connection (Settings → Online access)</a>
          </div>
        </div>`);
    } else if (state === 'closed') {
      studioRtc = null;
      setTransportBadge('Direct video closed', 'wait', 'Reopens when this page is visible');
      // The box ended it (idle / time cap): reopen while the page is still shown.
      if (h.closeReason === 'expired' && !document.hidden && activeCameraId === cameraId) {
        studioRtcRetry = setTimeout(() => { if (activeCameraId === cameraId && !studioRtc) openStudioVideo(cameraId); }, 1000);
      }
    }
  });
}

function studioRetryVideo() {
  if (activeCameraId && studioDirect()) openStudioVideo(activeCameraId);
}
window.studioRetryVideo = studioRetryVideo;

/** Show the camera: direct video in remote mode, MJPEG with the detector's boxes on the store network. */
async function startFeed(cameraId) {
  const mode = window.WebRtcLive ? await window.WebRtcLive.mode() : 'local';
  if (activeCameraId !== cameraId) return;
  if (viewport) viewport.classList.toggle('video-viewport-rtc', mode === 'webrtc');
  if (mode === 'webrtc') {
    if (streamImg && streamImg.getAttribute('src')) streamImg.removeAttribute('src');
    openStudioVideo(cameraId);
    return;
  }
  closeStudioVideo('local');
  setTransportBadge('', '');
  if (streamImg) {
    setViewportEmpty('');
    const sUrl = `/stream?camera_id=${encodeURIComponent(cameraId)}&overlay=1`;
    streamImg.src = window.edgeAuth && window.edgeAuth.authUrl ? window.edgeAuth.authUrl(sUrl) : sUrl;
  }
}

// Hidden tab: webrtc_live.js closes every session. Visible again: reopen.
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && activeCameraId && studioDirect() && !studioRtc) openStudioVideo(activeCameraId);
});
window.addEventListener('pagehide', () => closeStudioVideo('pagehide'));

function setViewportHtml(html) {
  const e = el('viewportEmpty');
  if (!e) return;
  e.innerHTML = html;
  e.style.display = html ? 'flex' : 'none';
}

function setViewportEmpty(message) {
  const e = el('viewportEmpty');
  if (!e) return;
  e.innerHTML = message ? `<div>${escapeHtml(message)}</div>` : '';
  e.style.display = message ? 'flex' : 'none';
}

/** Pointer position normalised 0..1 to the camera frame (the canvas covers the letterboxed content box). */
function eventToNorm(e) {
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return null;
  return {
    x: Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width)),
    y: Math.min(1, Math.max(0, (e.clientY - rect.top) / rect.height)),
  };
}

if (canvas) {
  canvas.addEventListener('click', (e) => {
    if (currentMode === 'NONE') {
      const p = eventToNorm(e);
      const hit = p ? liveBoxAt(p.x, p.y) : null;
      if (hit) { openBoxPopover(hit); return; }
      closeBoxPopover();
      setDrawStatus('Pick a tool first (tripwire, restricted area, checkout / queue area, privacy mask, ignore area or product shelf), or click a detected box.', true);
      return;
    }
    if (!activeCameraId) { setDrawStatus('Select a camera first.', true); return; }
    if (currentMode === 'IGNORE' && editingRule && editingRule.kind === 'IGNORE') {
      setRuleStatus('ignoreFormStatus', 'The shape of a saved ignore area cannot be moved. Change its name or coverage here, or delete it and draw a new one.', true);
      return;
    }
    const p = eventToNorm(e);
    if (!p) return;
    const nx = p.x;
    const ny = p.y;
    const style = MODE_STYLE[currentMode];
    if (drawnPoints.length >= style.max) drawnPoints = [];
    drawnPoints.push({ x: Number(nx.toFixed(4)), y: Number(ny.toFixed(4)) });
    drawOverlay();
    const need = Math.max(0, style.min - drawnPoints.length);
    setDrawStatus(`${drawnPoints.length} point(s) placed for the ${style.label}.` +
      (need ? ` ${need} more needed.` : ' Press Save to store it.'), false);
  });
  // Pointer over a detected box (no tool chosen): it can be clicked.
  canvas.addEventListener('mousemove', (e) => {
    if (currentMode !== 'NONE') { canvas.style.cursor = ''; return; }
    const p = eventToNorm(e);
    canvas.style.cursor = p && liveBoxAt(p.x, p.y) ? 'pointer' : '';
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
  savedZonesForCamera.exclusion_masks.forEach((z) => {
    if (!isIgnoreMask(z)) drawPolyline(z.points || [], 'rgba(160,174,192,0.55)', 'rgba(160,174,192,0.15)', true, 1.5);
  });
  savedZonesForCamera.products.forEach((z) => drawPolyline(z.points || [], 'rgba(255,215,0,0.55)', 'rgba(255,215,0,0.10)', true, 1.5));
  drawSavedRules();
  drawIgnoreAreas();
  drawLiveBoxes();

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
  closeBoxPopover();
  currentMode = mode;
  drawnPoints = [];
  editingRule = null;
  const row = el('maskModeRow');
  if (row) row.style.display = mode === 'EXCLUSION' ? 'flex' : 'none';
  if (mode === 'EXCLUSION') onMaskModeChange();
  showRuleForm(mode);
  syncToolChrome(mode);
  drawOverlay();
  const label = el('drawModeLabel');
  if (label) label.textContent = `Drawing: ${MODE_STYLE[mode].label}`;
  setDrawStatus(`Click ${MODE_STYLE[mode].min === MODE_STYLE[mode].max ? MODE_STYLE[mode].min : `${MODE_STYLE[mode].min}+`} point(s) on the video for the ${MODE_STYLE[mode].label}.`, false);
}

// Tool explanation line and which tool button shows as pressed.
function syncToolChrome(mode) {
  const help = el('toolHelp');
  if (help) help.textContent = TOOL_HELP[mode] || TOOL_HELP.NONE;
  document.querySelectorAll('.studio-tool-btn[data-tool]').forEach((b) => {
    b.setAttribute('aria-pressed', TOOL_MODES[b.getAttribute('data-tool')] === mode ? 'true' : 'false');
  });
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
  syncToolChrome('NONE');
  drawOverlay();
  const label = el('drawModeLabel');
  if (label) label.textContent = 'Choose a tool';
  setDrawStatus('Drawing cancelled.', false);
}

// ------------------------------------------------------------ product modal
// Product zones listed for the active camera (id -> zone), and the zone being
// edited (null while mapping a newly drawn polygon).
let productZonesById = {};
let editingProductZone = null;
// Study metrics the pipeline does not measure yet (only track_hand_reach is
// used): [checkbox id, stored key]. Kept as stored, never offered as working.
const NOT_MEASURED_STUDY = [
  ['chkDwellTime', 'track_dwell_time'],
  ['chkPutBack', 'track_put_back_friction'],
  ['chkPosSales', 'track_pos_conversion'],
  ['chkAbTest', 'ab_test_mode'],
];

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
  // Not measured yet: shown greyed and unticked whatever is stored, and the
  // stored values are sent back unchanged on save (submitProductModal).
  NOT_MEASURED_STUDY.forEach(([id]) => { const c = el(id); if (c) { c.checked = false; c.disabled = true; } });
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

/** Category words for the product form: ones already used on shelves, then the high-value ones. */
async function populateZoneNames() {
  const data = await getJSON('/api/v1/analytics/products/zones', null);
  const list = el('layoutZoneNames');
  if (!list) return;
  const words = (data && Array.isArray(data.category_suggestions)) ? data.category_suggestions : [];
  const high = new Set((data && data.high_value_categories) || []);
  list.innerHTML = words.map((n) => `<option value="${escapeHtml(n)}">${high.has(n) ? 'high value' : ''}</option>`).join('');
}

async function submitProductModal(event) {
  if (event) event.preventDefault();
  const status = el('productModalStatus');
  const name = el('modalProductName').value.trim();
  const sku = el('modalProductSku').value.trim();
  const category = el('modalProductCategory').value.trim();
  if (!name || !sku || !category) { status.textContent = 'Name, SKU and product category are required.'; return; }
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
    study_metrics: (() => {
      // Only hand reaches are measured. The other keys keep their stored
      // value (an edit) or are off (a new shelf: not measured yet).
      const stored = (editing && editing.study_metrics) || {};
      const sm = { track_hand_reach: el('chkHandReach').checked };
      NOT_MEASURED_STUDY.forEach(([, key]) => { sm[key] = typeof stored[key] === 'boolean' ? stored[key] : false; });
      return sm;
    })(),
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
    announceSaved(editing ? `Updated product area "${name}".` : `Saved product area "${name}".`);
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
    const d = err.detail || err;
    // A role refusal is one readable sentence; other errors keep the status code.
    if (res.status === 403 && typeof d === 'string') throw new Error(d);
    throw new Error(`HTTP ${res.status}: ${typeof d === 'string' ? d : JSON.stringify(d)}`);
  }
  return res.json();
}

async function saveDrawnZone() {
  if (!activeCameraId) { setDrawStatus('Select a camera first.', true); return; }
  if (currentMode === 'NONE') { setDrawStatus('Pick a tool first.', true); return; }
  if (currentMode === 'TRIPWIRE' || currentMode === 'INTRUSION') { await saveRule(); return; }
  if (currentMode === 'QUEUE') { await saveQueueArea(); return; }
  if (currentMode === 'IGNORE') { await saveIgnoreArea(); return; }
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
    announceSaved(`Saved ${style.label} "${name}".`);
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

// Inline two-step delete for privacy masks and product shelves, like the
// tripwire / restricted area rows (askDeleteRule / cancelDeleteRule).
function deleteConfirmHtml(kind, id) {
  return `
      <span class="rule-confirm" style="display:none;">
        <span class="zone-sub">Delete?</span>
        <button type="button" class="btn btn-danger btn-sm rule-delete-yes" onclick="confirmDeleteZone('${kind}', '${id}', this)">Delete</button>
        <button type="button" class="btn btn-sm rule-delete-no" onclick="cancelDeleteRule(this)">Keep</button>
      </span>`;
}
async function confirmDeleteZone(kind, id, btn) {
  const row = btn ? btn.closest('.zone-item') : null;
  if (btn) { btn.disabled = true; btn.textContent = 'Deleting…'; }
  // Close the confirmation first: the list refresh skips a list with one open.
  if (row) {
    const confirmBox = row.querySelector('.rule-confirm');
    const actions = row.querySelector('.rule-actions');
    if (confirmBox) confirmBox.style.display = 'none';
    if (actions) actions.style.display = 'inline-flex';
  }
  if (kind === 'PRODUCT') await deleteProductZone(id);
  else if (kind === 'IGNORE') {
    if (editingRule && editingRule.kind === 'IGNORE' && editingRule.id === id) clearCanvasPoints();
    await deleteZoneAt(`/api/zones/exclusion/${encodeURIComponent(id)}`, 'Ignore area');
  } else await deleteExclusion(id);
  if (btn) { btn.disabled = false; btn.textContent = 'Delete'; }
}

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
    if (container) container.dataset.sig = maskSig(savedZonesForCamera.exclusion_masks.filter((m) => !isIgnoreMask(m)));
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
  // Only real privacy masks: ignore areas, tripwires and restricted areas have their own buttons.
  showToast(await clearMaskKind('privacy', 'All privacy masks cleared.'));
  cancelClearAllZones();
  loadZonesList();
}

function askClearIgnoreAreas() { el('btnClearIgnoreAsk').style.display = 'none'; el('clearIgnoreConfirm').style.display = 'inline-flex'; }
function cancelClearIgnoreAreas() { el('btnClearIgnoreAsk').style.display = ''; el('clearIgnoreConfirm').style.display = 'none'; }
async function confirmClearIgnoreAreas() {
  showToast(await clearMaskKind('ignore', 'All ignore areas cleared.'));
  cancelClearIgnoreAreas();
  loadZonesList();
}

/**
 * POST /api/zones/clear?kinds=privacy|ignore. An older server that only knows
 * kinds=exclusion_masks (which removes both) answers 422: nothing is cleared
 * then, rather than deleting the other kind too.
 */
async function clearMaskKind(kind, okMsg) {
  try {
    const res = await fetch(`/api/zones/clear?kinds=${encodeURIComponent(kind)}`, { method: 'POST' });
    if (res.ok) return okMsg;
    if (res.status === 422) return 'Not cleared: this server cannot clear privacy masks and ignore areas separately yet. Delete them one by one.';
    const data = await res.json().catch(() => ({}));
    return `Clear failed: ${formatApiError(data, res.status)}`;
  } catch (e) { return `Clear failed: ${e.message}`; }
}

async function loadZonesList() {
  const forCam = (z) => !activeCameraId || z.camera_id === activeCameraId;

  const prodData = activeCameraId
    ? await getJSON(`/api/v1/analytics/products/zones?camera_id=${encodeURIComponent(activeCameraId)}`, null)
    : { zones: [] };
  const prodContainer = el('productShelfListContainer');
  const products = (prodData && prodData.zones) || [];
  // Do not rebuild under an open delete confirmation.
  const prodConfirmOpen = !!(prodContainer && prodContainer.querySelector('.rule-confirm[style*="inline-flex"]'));
  if (prodConfirmOpen) {
    productZonesById = {};
    products.forEach((pz) => { productZonesById[pz.id] = pz; });
  }
  if (prodContainer && !prodConfirmOpen) {
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
          <span class="rule-actions">
            <button type="button" class="btn btn-sm" onclick="editProductZone('${id}')" title="Edit product, SKU, shelf level">✏️</button>
            <button type="button" class="btn btn-danger btn-sm rule-delete" onclick="askDeleteRule(this)" title="Delete">🗑️</button>
          </span>${deleteConfirmHtml('PRODUCT', id)}
        </div>`);
    });
  }

  const data = await getJSON('/api/zones', null);
  const exclusion = ((data && data.exclusion_masks) || []).filter(forCam);
  const privacyMasks = exclusion.filter((m) => !isIgnoreMask(m));
  const ignoreAreas = exclusion.filter(isIgnoreMask);
  const tripwires = ((data && data.tripwires) || []).filter(forCam);
  const intrusion = ((data && data.intrusion_zones) || []).filter(forCam);
  const queueZones = ((data && data.queue_zones) || []).filter(forCam);
  renderRuleLists(data === null, tripwires, intrusion);
  renderQueueList(data === null, queueZones);

  const fill = (id, items, emptyMsg, render) => {
    const c = el(id);
    if (!c) return;
    if (data === null) { c.innerHTML = '<div class="fp-empty">Zones unavailable: the zone service did not respond.</div>'; return; }
    c.innerHTML = items.length ? items.map(render).join('') : `<div class="fp-empty">${emptyMsg}</div>`;
  };
  // The list refreshes every few seconds; do not rebuild it under an operator
  // who is changing a mask's mode, or when nothing changed.
  const exContainer = el('exclusionListContainer');
  const exSig = maskSig(privacyMasks);
  const busy = exContainer && ((exContainer.contains(document.activeElement) && document.activeElement !== document.body)
    || !!exContainer.querySelector('.rule-confirm[style*="inline-flex"]'));
  if (exContainer && !busy && (data === null || exContainer.dataset.sig !== exSig)) {
    fill('exclusionListContainer', privacyMasks, 'No privacy masks on this camera.', (ex) => {
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
          <span class="rule-actions">
            <button type="button" class="btn btn-danger btn-sm rule-delete" onclick="askDeleteRule(this)" title="Delete">🗑️</button>
          </span>${deleteConfirmHtml('MASK', id)}
          <div class="mask-row-status form-status" aria-live="polite" style="flex-basis: 100%; margin-top: 2px;">${escapeHtml((MASK_MODES[mode] || {}).help || '')}</div>
        </div>`;
    });
    exContainer.dataset.sig = data === null ? '' : exSig;
  }

  savedZonesForCamera = { tripwires, intrusion_zones: intrusion, exclusion_masks: exclusion, products, queue_zones: queueZones };
  renderIgnoreList(data === null, ignoreAreas);
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
  (savedZonesForCamera.queue_zones || []).forEach((z) => {
    const editing = editingRule && editingRule.kind === 'QUEUE' && editingRule.id === z.id;
    const on = z.enabled !== false;
    drawPolyline(z.points || [], editing ? '#ffffff' : (on ? 'rgba(192,132,252,0.9)' : 'rgba(192,132,252,0.4)'),
      'rgba(192,132,252,0.10)', true, editing ? 3 : 1.8);
    const p = (z.points || [])[0];
    const kind = QUEUE_KIND_LABEL[z.kind] || 'Checkout lane';
    if (p) drawLabel(`🧾 ${z.name || kind} · ${kind.toLowerCase()}${on ? '' : ' (off)'}`, p.x * canvas.width + 4, p.y * canvas.height + 14, '#d8b4fe');
  });
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
  const tw = el('tripwireFormRow'), ra = el('restrictedFormRow'), qa = el('queueFormRow'), ig = el('ignoreFormRow');
  if (tw) tw.style.display = mode === 'TRIPWIRE' ? 'flex' : 'none';
  if (ra) ra.style.display = mode === 'INTRUSION' ? 'flex' : 'none';
  if (qa) qa.style.display = mode === 'QUEUE' ? 'flex' : 'none';
  if (ig) ig.style.display = mode === 'IGNORE' ? 'flex' : 'none';
  if (mode === 'TRIPWIRE' && !editingRule) fillTripwireForm(null);
  if (mode === 'INTRUSION' && !editingRule) fillRestrictedForm(null);
  if (mode === 'QUEUE' && !editingRule) fillQueueForm(null);
  if (mode === 'IGNORE' && !editingRule) fillIgnoreForm(null);
  setRuleStatus('tripwireFormStatus', '');
  setRuleStatus('restrictedFormStatus', '');
  setRuleStatus('queueFormStatus', '');
  setRuleStatus('ignoreFormStatus', '');
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
    announceSaved(`Saved ${style.label} "${saved && saved.name}".`);
    await loadZonesList();
  } catch (err) {
    setRuleStatus(statusId, `Not saved: ${err.message}`, true);
  }
}

function editRule(kind, id) {
  const list = kind === 'TRIPWIRE' ? savedZonesForCamera.tripwires
    : (kind === 'QUEUE' ? (savedZonesForCamera.queue_zones || []) : savedZonesForCamera.intrusion_zones);
  const rule = list.find((r) => r.id === id);
  if (!rule) { showToast('That rule no longer exists.'); loadZonesList(); return; }
  setDrawMode(kind);
  editingRule = { kind, id };
  if (kind === 'QUEUE') {
    fillQueueForm(rule);
    el('zoneNameInput').value = '';
  } else {
    el('zoneNameInput').value = rule.name || '';
    if (kind === 'TRIPWIRE') fillTripwireForm(rule); else fillRestrictedForm(rule);
  }
  const label = el('drawModeLabel');
  if (label) label.textContent = `Editing: ${MODE_STYLE[kind].label}`;
  setDrawStatus(`Editing "${rule.name || id}". Change its settings and press Save; click new points on the video only if you want to move it.`, false);
  drawOverlay();
  const form = el(kind === 'TRIPWIRE' ? 'tripwireFormRow' : (kind === 'QUEUE' ? 'queueFormRow' : 'restrictedFormRow'));
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
const RULE_DELETE = {
  TRIPWIRE: { path: 'tripwire', label: 'Tripwire' },
  INTRUSION: { path: 'intrusion', label: 'Restricted area' },
  QUEUE: { path: 'queue', label: 'Checkout / queue area' },
};
async function confirmDeleteRule(kind, id, btn) {
  if (editingRule && editingRule.id === id) clearCanvasPoints();
  const k = RULE_DELETE[kind] || RULE_DELETE.INTRUSION;
  const row = btn ? btn.closest('.zone-item') : document.querySelector(`.zone-item[data-rule-id="${CSS.escape(id)}"]`);
  const yes = btn || (row && row.querySelector('.rule-delete-yes'));
  if (yes) { yes.disabled = true; yes.textContent = 'Deleting…'; }
  let ok = false;
  let msg;
  try {
    const res = await fetch(`/api/zones/${k.path}/${encodeURIComponent(id)}`, { method: 'DELETE' });
    ok = res.ok;
    msg = ok ? `${k.label} removed.` : `Failed to remove ${k.label} (HTTP ${res.status})`;
  } catch (e) { msg = `Failed to remove ${k.label}: ${e.message}`; }
  showToast(msg);
  // Close the confirmation first: the list refresh skips a list with one open.
  if (row) {
    const confirmBox = row.querySelector('.rule-confirm');
    const actions = row.querySelector('.rule-actions');
    if (confirmBox) confirmBox.style.display = 'none';
    if (actions) actions.style.display = 'inline-flex';
  }
  if (yes) { yes.disabled = false; yes.textContent = 'Delete'; }
  if (!ok && kind === 'QUEUE') queueRowNote = { id, msg, err: true, until: Date.now() + 8000 };
  await loadZonesList();
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

// ------------------------------------------------------------ checkout / queue areas
// /api/zones/queue: {id, name, camera_id, kind: checkout|queue, points (3+), enabled}.

function fillQueueForm(area) {
  const kind = area && area.kind === 'queue' ? 'queue' : 'checkout';
  el('queueFormTitle').textContent = area ? `Editing ${QUEUE_KIND_LABEL[kind].toLowerCase()}: ${area.name || area.id}` : 'New checkout / queue area';
  el('qaName').value = area ? (area.name || '') : '';
  el('qaKind').value = kind;
  const btn = el('btnSaveQueue');
  btn.textContent = area ? '💾 Save changes' : '💾 Save area';
  btn.dataset.label = btn.textContent;
}

async function saveQueueArea() {
  const statusId = 'queueFormStatus';
  const editing = editingRule && editingRule.kind === 'QUEUE' ? editingRule : null;
  const n = drawnPoints.length;
  if (!editing && n < 3) {
    setRuleStatus(statusId, `Click at least three points on the picture around the area first (${n} placed).`, true);
    return;
  }
  if (editing && n > 0 && n < 3) {
    setRuleStatus(statusId, `To move the area click at least three points (${n} placed), or press Cancel to keep its shape.`, true);
    return;
  }
  const kind = el('qaKind').value === 'queue' ? 'queue' : 'checkout';
  const name = el('qaName').value.trim() || QUEUE_KIND_LABEL[kind];
  const body = { name, kind };
  if (n >= 3) body.points = drawnPoints;
  const btn = el('btnSaveQueue');
  const idle = btn.dataset.label || btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Saving…';
  setRuleStatus(statusId, 'Saving…');
  try {
    let data;
    if (editing) {
      data = await sendRule('PATCH', `/api/zones/queue/${encodeURIComponent(editing.id)}`, body);
    } else {
      Object.assign(body, { camera_id: activeCameraId, enabled: true });
      data = await sendRule('POST', '/api/zones/queue', body);
    }
    const saved = (data && data.queue_area) || body;
    const what = (QUEUE_KIND_LABEL[saved.kind] || 'Checkout lane').toLowerCase();
    showToast(`Saved ${what}: ${saved.name}`);
    clearCanvasPoints();
    announceSaved(`Saved ${what} "${saved.name}".`);
    await loadZonesList();
  } catch (err) {
    setRuleStatus(statusId, `Not saved: ${err.message}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = idle;
  }
}

// Inline result shown on a row after enable/disable; survives the list rebuild briefly.
let queueRowNote = null;

async function toggleQueueArea(id, btn) {
  const area = (savedZonesForCamera.queue_zones || []).find((z) => z.id === id);
  if (!area) { showToast('That area no longer exists.'); loadZonesList(); return; }
  const enabled = area.enabled === false;
  if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }
  try {
    const data = await sendRule('PATCH', `/api/zones/queue/${encodeURIComponent(id)}`, { enabled });
    Object.assign(area, (data && data.queue_area) || { enabled });
    queueRowNote = { id, msg: enabled ? 'Turned on: timing people here.' : 'Turned off: not timing people here.', err: false, until: Date.now() + 8000 };
  } catch (e) {
    queueRowNote = { id, msg: `Not changed: ${e.message}`, err: true, until: Date.now() + 8000 };
  }
  renderQueueList(false, savedZonesForCamera.queue_zones || []);
  drawOverlay();
}

function queueRow(z) {
  const id = escapeHtml(z.id);
  const kind = z.kind === 'queue' ? 'queue' : 'checkout';
  const on = z.enabled !== false;
  const note = queueRowNote && queueRowNote.id === z.id && Date.now() < queueRowNote.until ? queueRowNote : null;
  const sub = `${kind === 'queue' ? 'waiting time' : 'time at the till'} · ${(z.points || []).length} points · ${on ? 'on' : 'off'}`;
  return `
    <div class="zone-item qa-row${on ? '' : ' qa-row-off'}" data-rule-id="${id}" data-queue-id="${id}" data-kind="${kind}" data-enabled="${on}">
      <div class="zone-info">
        <span class="zone-name"><span class="qa-kind-badge qa-kind-${kind}">${kind === 'queue' ? 'Queue' : 'Checkout'}</span>${escapeHtml(z.name || z.id)}${on ? '' : ' <span class="badge">off</span>'}</span>
        <span class="zone-sub">${sub}</span>
      </div>
      <span class="rule-actions">
        <button type="button" class="btn btn-sm qa-toggle" aria-pressed="${on}" onclick="toggleQueueArea('${id}', this)" title="${on ? 'Stop timing people in this area' : 'Start timing people in this area'}">${on ? 'Turn off' : 'Turn on'}</button>
        <button type="button" class="btn btn-sm rule-edit" onclick="editRule('QUEUE', '${id}')" title="Edit name, kind or shape">✎ Edit</button>
        <button type="button" class="btn btn-danger btn-sm rule-delete" onclick="askDeleteRule(this)" title="Delete">🗑️</button>
      </span>
      <span class="rule-confirm" style="display:none;">
        <span class="zone-sub">Delete?</span>
        <button type="button" class="btn btn-danger btn-sm rule-delete-yes" onclick="confirmDeleteRule('QUEUE', '${id}', this)">Delete</button>
        <button type="button" class="btn btn-sm rule-delete-no" onclick="cancelDeleteRule(this)">Keep</button>
      </span>
      ${note ? `<div class="qa-row-status form-status${note.err ? ' form-status-error' : ''}" aria-live="polite">${escapeHtml(note.msg)}</div>` : ''}
    </div>`;
}

function renderQueueList(unavailable, items) {
  const c = el('queueAreasListContainer');
  if (!c) return;
  // Do not rebuild under an open delete confirmation or when nothing changed.
  if (c.querySelector('.rule-confirm[style*="inline-flex"]')) return;
  const note = queueRowNote && Date.now() < queueRowNote.until ? queueRowNote : null;
  const sig = unavailable ? 'x' : JSON.stringify([items, note]);
  if (c.dataset.sig === sig) return;
  c.dataset.sig = sig;
  if (unavailable) { c.innerHTML = '<div class="fp-empty">Zones unavailable: the zone service did not respond.</div>'; return; }
  c.innerHTML = items.length ? items.map(queueRow).join('')
    : '<div class="fp-empty">No checkout or queue areas on this camera. Draw one with the Checkout / queue area tool.</div>';
}

// ------------------------------------------------------------ ignore areas (mask_mode AI_IGNORE)
// /api/zones/exclusion with mask_mode AI_IGNORE: {id, name, camera_id, points,
// enabled, ignore_box_fraction}. The video is not changed; the AI drops a
// detection whose feet are inside, or whose box is at least
// ignore_box_fraction inside. Shape changes need a new area (the PATCH takes
// name, coverage and enabled only).

// Inline result shown on a row after a change; survives the list rebuild briefly.
let ignoreRowNote = null;

function ignoreAreasForCamera() { return (savedZonesForCamera.exclusion_masks || []).filter(isIgnoreMask); }

function drawIgnoreAreas() {
  ignoreAreasForCamera().forEach((z) => {
    const editing = editingRule && editingRule.kind === 'IGNORE' && editingRule.id === z.id;
    const on = z.enabled !== false;
    ctx.save();
    ctx.setLineDash([6, 4]);
    drawPolyline(z.points || [], editing ? '#ffffff' : (on ? 'rgba(45,212,191,0.9)' : 'rgba(45,212,191,0.4)'),
      'rgba(45,212,191,0.10)', true, editing ? 3 : 1.8);
    ctx.restore();
    const p = (z.points || [])[0];
    if (p) drawLabel(`🚫 ${z.name || 'Ignore area'} · no AI${on ? '' : ' (off)'}`, p.x * canvas.width + 4, p.y * canvas.height + 14, '#5eead4');
  });
}

function fillIgnoreForm(area) {
  el('ignoreFormTitle').textContent = area ? `Editing ignore area: ${area.name || area.id}` : 'New ignore area';
  el('igName').value = area ? (area.name || '') : '';
  const sel = el('igCoverage');
  const v = area ? area.ignore_box_fraction : IGNORE_COVERAGE_DEFAULT;
  sel.innerHTML = area ? coverageOptions(v) : coverageOptions(IGNORE_COVERAGE_DEFAULT);
  const btn = el('btnSaveIgnore');
  btn.textContent = area ? '💾 Save changes' : '💾 Save ignore area';
  btn.dataset.label = btn.textContent;
}

function readCoverage(select) {
  const v = parseFloat(select && select.value);
  return Number.isFinite(v) && v >= 0.1 && v <= 1 ? v : null;
}

async function saveIgnoreArea() {
  const statusId = 'ignoreFormStatus';
  const editing = editingRule && editingRule.kind === 'IGNORE' ? editingRule : null;
  const n = drawnPoints.length;
  if (!editing && n < 3) {
    setRuleStatus(statusId, `Click at least three points on the picture around the poster, mannequin or screen first (${n} placed).`, true);
    return;
  }
  const name = el('igName').value.trim() || el('zoneNameInput').value.trim() || 'Ignore area';
  const fraction = readCoverage(el('igCoverage'));
  const body = { name };
  if (fraction !== null) body.ignore_box_fraction = fraction;
  const btn = el('btnSaveIgnore');
  const idle = btn.dataset.label || btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Saving…';
  setRuleStatus(statusId, 'Saving…');
  try {
    let data;
    if (editing) {
      data = await sendRule('PATCH', `/api/zones/exclusion/${encodeURIComponent(editing.id)}`, body);
    } else {
      Object.assign(body, { camera_id: activeCameraId, points: drawnPoints, mask_mode: IGNORE_MODE, enabled: true });
      data = await sendRule('POST', '/api/zones/exclusion', body);
    }
    const saved = (data && data.exclusion_mask) || body;
    const reported = isNum(saved.ignore_box_fraction);
    showToast(`Saved ignore area: ${saved.name}`);
    el('zoneNameInput').value = '';
    clearCanvasPoints();
    announceSaved(reported
      ? `Saved ignore area "${saved.name}". The AI no longer looks for people there.`
      : `Saved ignore area "${saved.name}". This server does not report the coverage setting: it ignores people whose feet are inside.`);
    await loadZonesList();
  } catch (err) {
    setRuleStatus(statusId, `Not saved: ${err.message}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = idle;
  }
}

function editIgnoreArea(id) {
  const area = ignoreAreasForCamera().find((z) => z.id === id);
  if (!area) { showToast('That ignore area no longer exists.'); loadZonesList(); return; }
  setDrawMode('IGNORE');
  editingRule = { kind: 'IGNORE', id };
  fillIgnoreForm(area);
  el('zoneNameInput').value = '';
  const label = el('drawModeLabel');
  if (label) label.textContent = 'Editing: ignore area';
  setDrawStatus(`Editing "${area.name || id}". Change its name or how much of a person must be inside, then press Save.`, false);
  drawOverlay();
  const form = el('ignoreFormRow');
  if (form && form.scrollIntoView) form.scrollIntoView({ block: 'nearest' });
}

/** PATCH one ignore area from its row (coverage or enabled); the result is shown on the row. */
async function patchIgnoreArea(id, changes, okMsg) {
  const area = ignoreAreasForCamera().find((z) => z.id === id);
  if (!area) { showToast('That ignore area no longer exists.'); loadZonesList(); return; }
  try {
    const data = await sendRule('PATCH', `/api/zones/exclusion/${encodeURIComponent(id)}`, changes);
    const saved = (data && data.exclusion_mask) || {};
    Object.assign(area, saved);
    const lost = 'ignore_box_fraction' in changes && !isNum(saved.ignore_box_fraction);
    ignoreRowNote = { id, msg: lost ? 'Not changed: this server does not support the coverage setting yet.' : okMsg, err: lost, until: Date.now() + 8000 };
  } catch (e) {
    ignoreRowNote = { id, msg: `Not changed: ${e.message}`, err: true, until: Date.now() + 8000 };
  }
  const c = el('ignoreListContainer');
  if (c) c.dataset.sig = '';
  if (document.activeElement && c && c.contains(document.activeElement)) document.activeElement.blur();
  renderIgnoreList(false, ignoreAreasForCamera());
  drawOverlay();
}

function updateIgnoreCoverage(id, select) {
  const v = readCoverage(select);
  if (v === null) return;
  select.disabled = true;
  patchIgnoreArea(id, { ignore_box_fraction: v }, `Saved: ignored when ${coverageLabel(v).toLowerCase()} of a person is inside.`);
}

function toggleIgnoreArea(id, box) {
  const enabled = !!box.checked;
  box.disabled = true;
  patchIgnoreArea(id, { enabled }, enabled ? 'Turned on: the AI ignores people here.' : 'Turned off: people here are detected again.');
}

function ignoreRow(z) {
  const id = escapeHtml(z.id);
  const on = z.enabled !== false;
  const note = ignoreRowNote && ignoreRowNote.id === z.id && Date.now() < ignoreRowNote.until ? ignoreRowNote : null;
  const sub = `${(z.points || []).length} points · video not changed${on ? '' : ' · off'}`;
  return `
    <div class="zone-item ig-row${on ? '' : ' ig-row-off'}" data-ignore-id="${id}" data-enabled="${on}">
      <div class="zone-info">
        <span class="zone-name">🚫 ${escapeHtml(z.name || z.id)}${on ? '' : ' <span class="badge">off</span>'}</span>
        <span class="zone-sub">${sub}</span>
      </div>
      <div class="ig-row-controls">
        <label class="ig-coverage-label" for="igCov_${id}">Person inside</label>
        <select id="igCov_${id}" class="form-select ig-coverage-select" title="How much of a person must be inside" onchange="updateIgnoreCoverage('${id}', this)">${coverageOptions(z.ignore_box_fraction)}</select>
        <label class="checkbox-label ig-enabled" for="igOn_${id}"><input type="checkbox" id="igOn_${id}" ${on ? 'checked' : ''} onchange="toggleIgnoreArea('${id}', this)" /> Enabled</label>
      </div>
      <span class="rule-actions">
        <button type="button" class="btn btn-sm rule-edit" onclick="editIgnoreArea('${id}')" title="Edit name or coverage">✎ Edit</button>
        <button type="button" class="btn btn-danger btn-sm rule-delete" onclick="askDeleteRule(this)" title="Delete">🗑️</button>
      </span>${deleteConfirmHtml('IGNORE', id)}
      ${note ? `<div class="ig-row-status form-status${note.err ? ' form-status-error' : ''}" aria-live="polite">${escapeHtml(note.msg)}</div>` : ''}
    </div>`;
}

function renderIgnoreList(unavailable, items) {
  const c = el('ignoreListContainer');
  if (!c) return;
  // Do not rebuild under an open delete confirmation, a control in use, or when nothing changed.
  if (c.querySelector('.rule-confirm[style*="inline-flex"]')) return;
  if (c.contains(document.activeElement) && document.activeElement !== document.body) return;
  const note = ignoreRowNote && Date.now() < ignoreRowNote.until ? ignoreRowNote : null;
  const sig = unavailable ? 'x' : JSON.stringify([maskSig(items), note]);
  if (c.dataset.sig === sig) return;
  c.dataset.sig = sig;
  if (unavailable) { c.innerHTML = '<div class="fp-empty">Zones unavailable: the zone service did not respond.</div>'; return; }
  c.innerHTML = items.length ? items.map(ignoreRow).join('')
    : '<div class="fp-empty">No ignore areas. Draw one around a poster, mannequin or screen that the AI mistakes for a person.</div>';
}

// ------------------------------------------------------------ live boxes: "Not a person"
// GET /api/v1/cameras/{id}/live-status `boxes`: [{track_id, box:[x1,y1,x2,y2]
// normalised 0..1 to the analysed frame, motion_state, confidence}]. The canvas
// covers the letterboxed picture (contentBox), so the same normalised box fits
// the store-network MJPEG picture and the direct video alike.

const BOX_STYLE = {
  moving: { stroke: 'rgba(0,255,157,0.9)', dash: [], label: null, text: '#7dffc8' },
  pending: { stroke: 'rgba(255,170,0,0.95)', dash: [5, 4], label: 'confirming', text: '#ffcc66' },
  static: { stroke: 'rgba(170,180,195,0.95)', dash: [3, 3], label: 'static', text: '#d0d7e2', fill: 'rgba(160,174,192,0.14)' },
};
const BOX_STATE_TEXT = {
  moving: 'moving person',
  pending: 'new detection, still being confirmed (first seconds)',
  static: 'static figure: not moving, already left out of counts',
};

/** One box from the server, as {track_id, x1, y1, x2, y2, state, confidence}, or null. */
function normaliseLiveBox(b, data) {
  if (!b) return null;
  const raw = Array.isArray(b.box) ? b.box : (Array.isArray(b.bbox) ? b.bbox : null);
  if (!raw || raw.length < 4 || !raw.every(isNum)) return null;
  let [x1, y1, x2, y2] = raw;
  // Tolerate pixel coordinates from a server that does not normalise.
  if (Math.max(x1, y1, x2, y2) > 1.5) {
    const fw = (data && data.frame_width) || frameSize().w;
    const fh = (data && data.frame_height) || frameSize().h;
    if (!fw || !fh) return null;
    x1 /= fw; x2 /= fw; y1 /= fh; y2 /= fh;
  }
  const c = (v) => Math.min(1, Math.max(0, v));
  const lx = c(Math.min(x1, x2)), rx = c(Math.max(x1, x2)), ty = c(Math.min(y1, y2)), by = c(Math.max(y1, y2));
  if (rx - lx < 0.002 || by - ty < 0.002) return null;
  x1 = lx; x2 = rx; y1 = ty; y2 = by;
  const state = BOX_STYLE[b.motion_state] ? b.motion_state : 'moving';
  return { track_id: b.track_id, x1, y1, x2, y2, state, confidence: isNum(b.confidence) ? b.confidence : null };
}

function setLiveBoxes(cameraId, data) {
  const fresh = !!(data && data.has_frame) && !(isNum(data.seconds_since_frame) && data.seconds_since_frame > 5);
  const list = fresh && Array.isArray(data.boxes) ? data.boxes.map((b) => normaliseLiveBox(b, data)).filter(Boolean) : [];
  const sig = JSON.stringify(list);
  if (liveBoxesCamera === cameraId && setLiveBoxes._sig === sig) return;
  setLiveBoxes._sig = sig;
  liveBoxes = list;
  liveBoxesCamera = cameraId;
  drawOverlay();
}

let boxPollBusy = false;
async function pollLiveBoxes() {
  if (document.hidden || !activeCameraId || boxPollBusy) return;
  const cam = activeCameraId;
  boxPollBusy = true;
  try {
    const data = await getJSON(`/api/v1/cameras/${encodeURIComponent(cam)}/live-status`, null);
    if (cam !== activeCameraId) return;
    liveCache = { cameraId: cam, at: Date.now(), data };
    setLiveBoxes(cam, data);
  } finally {
    boxPollBusy = false;
  }
}

function resetLiveBoxes() {
  liveBoxes = [];
  liveBoxesCamera = null;
  setLiveBoxes._sig = '';
  liveCache = null;
  closeBoxPopover();
}

function drawLiveBoxes() {
  if (liveBoxesCamera !== activeCameraId) return;
  const W = canvas.width, H = canvas.height;
  const sel = selectedBox;
  liveBoxes.forEach((b) => {
    const st = BOX_STYLE[b.state];
    const x = b.x1 * W, y = b.y1 * H, w = (b.x2 - b.x1) * W, h = (b.y2 - b.y1) * H;
    ctx.save();
    ctx.setLineDash(st.dash);
    if (st.fill) { ctx.fillStyle = st.fill; ctx.fillRect(x, y, w, h); }
    ctx.strokeStyle = st.stroke;
    ctx.lineWidth = 1.6;
    ctx.strokeRect(x, y, w, h);
    ctx.restore();
    if (st.label) drawLabel(st.label, x + 3, Math.max(13, y + 14), st.text);
  });
  if (sel) {
    // The clicked box stays outlined even after its track moves on or ends.
    ctx.save();
    ctx.strokeStyle = '#ffffff';
    ctx.lineWidth = 2.6;
    ctx.strokeRect(sel.x1 * W, sel.y1 * H, (sel.x2 - sel.x1) * W, (sel.y2 - sel.y1) * H);
    ctx.restore();
  }
}

/** The smallest live box under a normalised point (nested boxes: the inner one). */
function liveBoxAt(x, y) {
  if (liveBoxesCamera !== activeCameraId) return null;
  let best = null;
  liveBoxes.forEach((b) => {
    if (x < b.x1 || x > b.x2 || y < b.y1 || y > b.y2) return;
    const a = (b.x2 - b.x1) * (b.y2 - b.y1);
    if (!best || a < best.a) best = { a, b };
  });
  return best ? best.b : null;
}

function placeBoxPopover(pop, b) {
  const box = contentBox();
  const W = viewport.clientWidth, H = viewport.clientHeight;
  const pw = pop.offsetWidth || 260, ph = pop.offsetHeight || 90;
  const left = Math.max(8, Math.min(W - pw - 8, box.ox + b.x1 * box.w));
  const below = box.oy + b.y2 * box.h + 6;
  const above = box.oy + b.y1 * box.h - ph - 6;
  const top = below + ph <= H - 4 ? below : (above >= 4 ? above : Math.max(4, Math.min(H - ph - 4, box.oy + b.y1 * box.h)));
  pop.style.left = `${left}px`;
  pop.style.top = `${top}px`;
}

function openBoxPopover(b) {
  const pop = el('boxPopover');
  if (!pop) return;
  selectedBox = { ...b };
  const conf = isNum(b.confidence) ? ` · ${Math.round(b.confidence * 100)}% sure` : '';
  pop.innerHTML = `
    <div class="box-popover-title">Detected: ${escapeHtml(BOX_STATE_TEXT[b.state] || 'person')}${conf}</div>
    <div class="box-popover-actions">
      <button type="button" class="btn btn-warning btn-sm" id="btnNotPerson" onclick="ignoreSelectedBox(this)">Not a person — ignore this spot</button>
      <button type="button" class="btn btn-sm" onclick="closeBoxPopover()">Cancel</button>
    </div>
    <div class="box-popover-status form-status" id="boxPopoverStatus" aria-live="polite"></div>`;
  pop.hidden = false;
  placeBoxPopover(pop, selectedBox);
  drawOverlay();
  const btn = el('btnNotPerson');
  if (btn) btn.focus();
}

function closeBoxPopover() {
  const pop = el('boxPopover');
  clearTimeout(closeBoxPopover._timer);
  if (pop && !pop.hidden) { pop.hidden = true; pop.innerHTML = ''; }
  if (selectedBox) { selectedBox = null; if (ctx) drawOverlay(); }
}
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && selectedBox) closeBoxPopover(); });

function setBoxPopoverStatus(msg, isError) {
  const s = el('boxPopoverStatus');
  if (!s) return;
  s.textContent = msg;
  s.classList.toggle('form-status-error', !!isError);
}

async function ignoreSelectedBox(btn) {
  const b = selectedBox;
  if (!b || !activeCameraId) return;
  const cam = activeCameraId;
  if (btn) { btn.disabled = true; btn.textContent = 'Adding…'; }
  setBoxPopoverStatus('');
  const t = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  let res;
  let data = {};
  try {
    res = await fetch('/api/zones/exclusion/from-box', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ camera_id: cam, box: [b.x1, b.y1, b.x2, b.y2], pad: 0.1, name: `Not a person ${t}` }),
    });
    data = await res.json().catch(() => ({}));
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = 'Not a person — ignore this spot'; }
    setBoxPopoverStatus(`Not added: ${e.message}`, true);
    return;
  }
  if (!res.ok) {
    if (btn) { btn.disabled = false; btn.textContent = 'Not a person — ignore this spot'; }
    const msg = (res.status === 404 || res.status === 405)
      ? 'This server cannot make an ignore area from a box yet. Draw one with the Ignore area tool.'
      : (res.status === 403 && typeof data.detail === 'string' ? data.detail : formatApiError(data, res.status));
    setBoxPopoverStatus(`Not added: ${msg}`, true);
    return;
  }
  const saved = data.exclusion_mask || data;
  const newId = saved && saved.id;
  const pop = el('boxPopover');
  if (pop && selectedBox === b) {
    pop.innerHTML = `
      <div class="box-popover-msg">Ignore area added. People are no longer detected there.</div>
      <div class="box-popover-actions">
        ${newId ? `<button type="button" class="btn btn-sm" id="btnUndoBoxIgnore" onclick="undoBoxIgnore('${escapeHtml(newId)}', this)">Undo</button>` : ''}
        <button type="button" class="btn btn-sm" onclick="closeBoxPopover()">Close</button>
      </div>
      <div class="box-popover-status form-status" id="boxPopoverStatus" aria-live="polite"></div>`;
    placeBoxPopover(pop, b);
  }
  showToast('Ignore area added.');
  await loadZonesList();
}

async function undoBoxIgnore(id, btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Undoing…'; }
  try {
    const res = await fetch(`/api/zones/exclusion/${encodeURIComponent(id)}`, { method: 'DELETE' });
    if (!res.ok && res.status !== 404) {
      const data = await res.json().catch(() => ({}));
      throw new Error(formatApiError(data, res.status));
    }
    const pop = el('boxPopover');
    if (pop && !pop.hidden) {
      pop.innerHTML = `
        <div class="box-popover-msg">Undone: the ignore area was removed. People there are detected again.</div>
        <div class="box-popover-actions"><button type="button" class="btn btn-sm" onclick="closeBoxPopover()">Close</button></div>`;
      clearTimeout(closeBoxPopover._timer);
      closeBoxPopover._timer = setTimeout(closeBoxPopover, 5000);
    }
    showToast('Ignore area removed.');
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = 'Undo'; }
    setBoxPopoverStatus(`Not undone: ${e.message}`, true);
  }
  await loadZonesList();
}

// ------------------------------------------------------------ deep links (?tool=&kind=)
let deepLinkApplied = false;
function applyDeepLinkTool() {
  if (deepLinkApplied) return;
  const tool = String(getUrlParameter('tool') || '').toLowerCase();
  if (!tool) return;
  const mode = TOOL_MODES[tool];
  deepLinkApplied = true;
  if (!mode) { setDrawStatus(`Unknown tool "${tool}" in the link; pick a tool below.`, true); return; }
  setDrawMode(mode);
  let label = MODE_STYLE[mode].label;
  if (mode === 'QUEUE') {
    const kind = String(getUrlParameter('kind') || '').toLowerCase() === 'queue' ? 'queue' : 'checkout';
    el('qaKind').value = kind;
    label = QUEUE_KIND_LABEL[kind].toLowerCase();
  }
  setDrawStatus(`Draw the ${label} on the picture, then Save.`, false);
  document.body.setAttribute('data-deeplink-tool', tool);
  const target = el('studioTools');
  if (target && target.scrollIntoView) target.scrollIntoView({ behavior: 'instant', block: 'center' });
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
  syncChecklistLink();
  resetLiveBoxes();
  startFeed(cameraId);
  drawnPoints = [];
  loadZonesList();
  pollLiveBoxes().then(() => pollTelemetry());
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
    closeStudioVideo('no-cameras');
    setViewportEmpty(data === null
      ? 'Camera list unavailable: the API did not respond.'
      : 'No camera has been added yet. Add one on the Store map, in "Cameras & devices".');
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

  const wanted = getUrlParameter('camera_id') || getUrlParameter('camera');
  const pick = studioCameras.find((c) => c.id === wanted) || studioCameras.find((c) => c.id === activeCameraId) || studioCameras[0];
  if (wanted && !studioCameras.some((c) => c.id === wanted)) showToast(`Camera "${wanted}" not found; showing ${pick.name}.`);
  if (pick.id !== activeCameraId) selectCamera(pick.id);
  else document.querySelectorAll('#cameraButtonsList .btn').forEach((b) => b.classList.toggle('btn-primary', b.getAttribute('data-camera-id') === activeCameraId));
  // Tool from the link: only needs the camera chosen, not a loaded frame
  // (points are normalised, and the canvas re-sizes when the first frame arrives).
  applyDeepLinkTool();
}

// ------------------------------------------------------------ telemetry
const STATIC_TELEMETRY = ['tPending', 'tStatic', 'tStaticMemory', 'tIgnoredDet'];
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
    ['tStatus', 'tFps', 'tDetections', 'tTracks', 'tFrames', 'tFrameSize', 'tAge', 'tCalibrated', 'tError', ...STATIC_TELEMETRY].forEach((id) => setTelemetry(id, null));
    setTelemetry('fpsBadge', null, ' FPS');
    return;
  }

  // Per-camera endpoint (Agent D); until it exists, the same entry is taken
  // from the pipeline status list so nothing here is ever invented.
  // The box poll fetches the same entry about once a second: reuse it while fresh.
  const cam = activeCameraId;
  let live = liveCache && liveCache.cameraId === cam && Date.now() - liveCache.at < 1500 ? liveCache.data : null;
  if (!live) live = await getJSON(`/api/v1/cameras/${encodeURIComponent(cam)}/live-status`, null);
  if (cam !== activeCameraId) return;
  if (!live && detector) live = (detector.cameras || []).find((c) => c.camera_id === activeCameraId) || null;

  if (!live) {
    setTelemetry('tStatus', 'NOT RUNNING');
    ['tFps', 'tDetections', 'tTracks', 'tFrames', 'tFrameSize', 'tAge', 'tCalibrated', ...STATIC_TELEMETRY].forEach((id) => setTelemetry(id, null));
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
  // Static-figure / ignore-area counters; an older server without them shows a dash.
  setTelemetry('tPending', analysed && isNum(live.pending_tracks) ? live.pending_tracks : null);
  setTelemetry('tStatic', analysed && isNum(live.static_tracks) ? live.static_tracks : null);
  setTelemetry('tStaticMemory', isNum(live.static_memory_count) ? live.static_memory_count : null);
  setTelemetry('tIgnoredDet', analysed && isNum(live.ignored_detections_last) ? live.ignored_detections_last : null);
  setTelemetry('tFrames', isNum(live.frames_read) ? live.frames_read.toLocaleString() : null);
  const fw = live.frame_width || (analysed && frameSize().w) || null;
  const fh = live.frame_height || (analysed && frameSize().h) || null;
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
    if (res.status === 403 && body.code === 'video_direct_only') {
      out.innerHTML = '<div class="fp-empty">Snapshot not saved: over online access, live pictures travel only on the direct video connection, never through the online-access server. Save snapshots from the store network.</div>';
      return;
    }
    if (!res.ok) {
      out.innerHTML = `<div class="fp-empty">Snapshot not saved (HTTP ${res.status}): ${escapeHtml(body.detail || 'no detail')}</div>`;
      return;
    }
    // body.url carries its own short-lived access token (images cannot send a header).
    const shotUrl = body.url;
    out.innerHTML = `
      <div class="capture-thumb">
        <a href="${escapeHtml(shotUrl)}" target="_blank" rel="noopener"><img src="${escapeHtml(shotUrl)}" alt="Saved snapshot ${escapeHtml(body.filename)}" /></a>
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
    const direct = studioDirect();
    if (direct) {
      check('direct video: no MJPEG stream', !streamImg.getAttribute('src'), streamImg.getAttribute('src'));
      const rv = streamVideo.getBoundingClientRect();
      check('direct video rendered height > 100', rv.height > 100, `${rv.width.toFixed(0)}x${rv.height.toFixed(0)}`);
      check('direct video has a picture', streamVideo.videoWidth > 0, `${streamVideo.videoWidth}x${streamVideo.videoHeight}`);
    } else {
      check('stream src has camera id + overlay', /\/stream\?camera_id=.+&overlay=1/.test(streamImg.getAttribute('src') || ''), streamImg.getAttribute('src'));
      const r = streamImg.getBoundingClientRect();
      check('stream img rendered height > 100', r.height > 100, `${r.width.toFixed(0)}x${r.height.toFixed(0)}`);
      check('stream img naturalWidth > 0', streamImg.naturalWidth > 0, `${streamImg.naturalWidth}x${streamImg.naturalHeight}`);
    }
    const box = contentBox();
    const cr = canvas.getBoundingClientRect();
    check('canvas tracks displayed content box', Math.abs(cr.width - box.w) < 2 && Math.abs(cr.height - box.h) < 2,
      `canvas ${cr.width.toFixed(0)}x${cr.height.toFixed(0)} vs content ${box.w.toFixed(0)}x${box.h.toFixed(0)}`);
    const fs = frameSize();
    if (fs.w) {
      check('canvas aspect equals frame aspect', Math.abs((cr.width / cr.height) - (fs.w / fs.h)) < 0.05);
    }
    check('telemetry status populated', el('tStatus').textContent !== DASH, el('tStatus').textContent);
    check('telemetry error shown honestly', true, el('tError').textContent);
    // Click mapping: a synthetic click at the canvas centre lands at (0.5, 0.5).
    setDrawMode('EXCLUSION');
    // Re-measure: switching tools shows/hides form rows, which can move the canvas.
    const cr2 = canvas.getBoundingClientRect();
    canvas.dispatchEvent(new MouseEvent('click', { clientX: cr2.left + cr2.width / 2, clientY: cr2.top + cr2.height / 2, bubbles: true }));
    check('click maps to normalised centre', drawnPoints.length === 1 && Math.abs(drawnPoints[0].x - 0.5) < 0.01 && Math.abs(drawnPoints[0].y - 0.5) < 0.01, JSON.stringify(drawnPoints));
    clearCanvasPoints();
    await triggerSnapshot();
    check('snapshot result rendered', /Saved|Snapshot not saved|Snapshot failed/.test(el('captureResult').textContent), el('captureResult').textContent.trim());
    await triggerClip();
    check('clip result rendered honestly', /Clip export is not available|Clip exported|Clip not exported|Clip export failed/.test(el('captureResult').textContent), el('captureResult').textContent.trim());
  } else {
    check('empty viewport state shown', el('viewportEmpty').style.display !== 'none');
  }
  check('zone lists rendered', ['exclusionListContainer', 'ignoreListContainer', 'productShelfListContainer', 'tripwiresListContainer', 'intrusionListContainer', 'queueAreasListContainer'].every((id) => el(id).textContent.trim() && el(id).textContent.trim() !== 'Loading…'));
  check('queue tool button exists', !!el('btnToolQueue') && el('btnToolQueue').getBoundingClientRect().height >= 32);
  check('queue form fields exist', !!el('queueFormRow') && !!el('qaName') && !!el('btnSaveQueue')
    && !!el('qaKind') && ['checkout', 'queue'].every((v) => [...el('qaKind').options].some((o) => o.value === v)));
  const prevMode = currentMode;
  setDrawMode('QUEUE');
  check('queue form shown by the tool', el('queueFormRow').style.display === 'flex' && el('toolHelp').textContent === QUEUE_HELP);
  if (prevMode === 'NONE') clearCanvasPoints(); else setDrawMode(prevMode);
  askClearAllZones();
  check('inline clear-all confirm shown', el('clearAllConfirm').style.display !== 'none');
  cancelClearAllZones();
  check('ignore tool button exists', !!el('btnToolIgnore') && el('btnToolIgnore').getBoundingClientRect().height >= 32);
  check('privacy mask modes exclude ignore', ![...el('maskModeSelect').options].some((o) => o.value === IGNORE_MODE));
  check('privacy list has no ignore areas', !document.querySelector('#exclusionListContainer .ig-row'));
  setDrawMode('IGNORE');
  check('ignore form shown by the tool', el('ignoreFormRow').style.display === 'flex' && el('toolHelp').textContent === IGNORE_HELP
    && el('igCoverage').value === String(IGNORE_COVERAGE_DEFAULT));
  if (prevMode === 'NONE') clearCanvasPoints(); else setDrawMode(prevMode);
  askClearIgnoreAreas();
  check('inline ignore clear-all confirm shown', el('clearIgnoreConfirm').style.display !== 'none');
  cancelClearIgnoreAreas();
  check('telemetry static rows present', STATIC_TELEMETRY.every((id) => !!el(id)));
  await new Promise((r) => setTimeout(r, 500));
  check('no page errors', errors.length === 0, errors.join(' | '));
  const failed = results.filter((x) => x.startsWith('FAIL')).length;
  results.unshift(`STUDIO SELFTEST ${failed === 0 ? 'OK' : 'FAILED'} (${results.length} checks, ${failed} failed)`);
  out.textContent = results.join('\n');
  window.__selftestDone = true;
}

function initStudio() {
  if (getUrlParameter('__selftest') === '1') runStudioSelfTest();
  syncChecklistLink();
  resizeCanvas();
  populateZoneNames();
  loadStudioSources();
  telemetryTimer = setInterval(pollTelemetry, 2000);
  boxTimer = setInterval(pollLiveBoxes, 1000);
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
