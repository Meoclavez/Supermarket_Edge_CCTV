/**
 * Camera gallery for the store map (docs/STORE_MAP_V2_CONTRACT.md, stream B).
 *
 * A panel under the map (a bottom sheet on phones) with one card per camera:
 * its cached still picture, name and status chips. Staff drag a card onto the
 * map to place that camera there, or press "Place" to put it in the middle of
 * the map (the keyboard path). Hovering a camera on the map or a card shows a
 * floating picture card.
 *
 * Talks to the map only through window.FloorplanAPI and its 'fp:*' events.
 * Pictures come from window.StillCache (js/still_cache.js): kept in this
 * browser only, never uploaded.
 *
 * All DOM is built here inside #cameraGalleryMount (index.html, store map).
 */
(function () {
  'use strict';

  const PREF_KEY = 'edge.cameraGallery.v1';
  const DRAG_THRESHOLD_PX = 6;
  const HOVER_CAPTURE_DELAY_MS = 350;
  const PREVIEW_HIDE_DELAY_MS = 120;
  const PHONE_MQ = window.matchMedia ? window.matchMedia('(max-width: 760px)') : { matches: false, addEventListener() {} };
  const COARSE_MQ = window.matchMedia ? window.matchMedia('(pointer: coarse)') : { matches: false };

  // ------------------------------------------------------------ helpers

  const ico = (name, opts) => {
    try { return window.EdgeIcon && typeof window.EdgeIcon.svg === 'function' ? window.EdgeIcon.svg(name, opts) : ''; } catch (_) { return ''; }
  };

  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function toast(msg, kind) {
    if (typeof window.showToast === 'function') window.showToast(msg, kind);
  }

  function ago(ms) {
    const s = Math.max(0, (Date.now() - Number(ms || 0)) / 1000);
    if (s < 45) return 'just now';
    const m = Math.round(s / 60);
    if (m < 60) return `${m} min ago`;
    const h = Math.round(m / 60);
    if (h < 24) return `${h} h ago`;
    const d = Math.round(h / 24);
    if (d === 1) return 'yesterday';
    return `${d} days ago`;
  }

  function readPrefs() {
    try { return JSON.parse(window.localStorage.getItem(PREF_KEY) || '{}') || {}; } catch (_) { return {}; }
  }
  function writePrefs() {
    try { window.localStorage.setItem(PREF_KEY, JSON.stringify({ collapsed: state.collapsed, filter: state.filter })); } catch (_) { /* storage blocked */ }
  }

  const cache = () => window.StillCache || null;
  // Placing cameras changes setup: operators see the gallery but cannot drag or place
  // (the server refuses anyway; this only avoids offering what will be refused).
  const canEdit = () => !(window.EdgeAuth && window.EdgeAuth.known && !window.EdgeAuth.canChangeSetup);
  const camName = (c) => (c && (c.name || c.camera_id)) || 'Camera';

  // ------------------------------------------------------------ state

  const prefs = readPrefs();
  const state = {
    api: null,
    cameras: [],
    filter: prefs.filter === 'all' || prefs.filter === 'unplaced' ? prefs.filter : null,   // null: decided on first data
    query: '',
    collapsed: typeof prefs.collapsed === 'boolean' ? prefs.collapsed : !!PHONE_MQ.matches,
    transport: 'unknown',
    selectedId: null,
    refreshRun: null,          // {ctl}
  };

  const els = {};
  const cards = new Map();     // camera_id -> <li>
  let observer = null;

  // ------------------------------------------------------------ shell

  function buildShell(host) {
    host.innerHTML = '';
    const panel = document.createElement('section');
    panel.className = 'cg-panel';
    panel.setAttribute('aria-label', 'Camera gallery');
    panel.innerHTML = `
      <div class="cg-head">
        <button type="button" class="cg-toggle" aria-expanded="true" aria-controls="cgBody">
          <span class="cg-caret" aria-hidden="true">${ico('chevron-down', { size: 'sm' })}</span>
          <span class="cg-title">Cameras <span class="cg-title-hint">— drag one onto the map</span></span>
          <span class="cg-count" aria-live="polite"></span>
        </button>
      </div>
      <div class="cg-body" id="cgBody">
        <div class="cg-tools">
          <label class="cg-search-wrap">
            <span class="cg-search-icon" aria-hidden="true">${ico('search', { size: 'sm' })}</span>
            <input type="search" class="input cg-search" placeholder="Find a camera" aria-label="Find a camera" autocomplete="off" spellcheck="false">
          </label>
          <div class="cg-seg" role="group" aria-label="Which cameras to show">
            <button type="button" class="btn btn-sm cg-seg-btn" data-filter="unplaced" aria-pressed="false">Not placed</button>
            <button type="button" class="btn btn-sm cg-seg-btn" data-filter="all" aria-pressed="false">All</button>
          </div>
          <button type="button" class="btn btn-sm cg-refresh-all" title="Take a new picture from every camera that is on">
            ${ico('refresh-cw', { size: 'sm' })}<span class="cg-refresh-all-label">Refresh all pictures</span>
          </button>
        </div>
        <div class="cg-progress" role="status" aria-live="polite" hidden>
          <span class="cg-progress-text"></span>
          <progress class="cg-progress-bar" max="1" value="0"></progress>
          <button type="button" class="btn btn-xs cg-progress-stop">Stop</button>
        </div>
        <p class="cg-hint"></p>
        <ul class="cg-list" role="list"></ul>
        <div class="cg-empty" hidden></div>
      </div>`;
    host.appendChild(panel);

    els.host = host;
    els.panel = panel;
    els.head = panel.querySelector('.cg-head');
    els.toggle = panel.querySelector('.cg-toggle');
    els.count = panel.querySelector('.cg-count');
    els.body = panel.querySelector('.cg-body');
    els.search = panel.querySelector('.cg-search');
    els.segBtns = Array.from(panel.querySelectorAll('.cg-seg-btn'));
    els.refreshAll = panel.querySelector('.cg-refresh-all');
    els.refreshAllLabel = panel.querySelector('.cg-refresh-all-label');
    els.progress = panel.querySelector('.cg-progress');
    els.progressText = panel.querySelector('.cg-progress-text');
    els.progressBar = panel.querySelector('.cg-progress-bar');
    els.progressStop = panel.querySelector('.cg-progress-stop');
    els.hint = panel.querySelector('.cg-hint');
    els.list = panel.querySelector('.cg-list');
    els.empty = panel.querySelector('.cg-empty');

    els.toggle.addEventListener('click', () => setCollapsed(!state.collapsed));
    els.search.addEventListener('input', () => { state.query = els.search.value.trim().toLowerCase(); renderList(); });
    els.segBtns.forEach((b) => b.addEventListener('click', () => { state.filter = b.dataset.filter; writePrefs(); renderList(); }));
    els.refreshAll.addEventListener('click', () => { if (state.refreshRun) stopRefreshAll(); else startRefreshAll(); });
    els.progressStop.addEventListener('click', stopRefreshAll);
    els.empty.addEventListener('click', (e) => {
      const b = e.target.closest('[data-act="show-all"]');
      if (b) { state.filter = 'all'; writePrefs(); renderList(); }
    });

    applyCollapsed();
    renderHint();
  }

  function setCollapsed(v) {
    state.collapsed = !!v;
    writePrefs();
    applyCollapsed();
  }

  function applyCollapsed() {
    if (!els.panel) return;
    els.panel.classList.toggle('is-collapsed', state.collapsed);
    els.toggle.setAttribute('aria-expanded', state.collapsed ? 'false' : 'true');
    els.body.hidden = state.collapsed;
    // The map's canvas measures its box; let it re-fit after the panel changed height.
    try { window.dispatchEvent(new Event('resize')); } catch (_) {}
  }

  function renderHint() {
    if (!els.hint) return;
    els.hint.textContent = !canEdit()
      ? 'Only an administrator can place cameras on the map.'
      : COARSE_MQ.matches
      ? 'Hold the dotted handle and drag a camera onto the map, or tap Place.'
      : 'Drag a camera onto the map, or press Place to put it in the middle.';
  }

  // ------------------------------------------------------------ list

  function visibleCameras() {
    const q = state.query;
    return state.cameras.filter((c) => {
      if (state.filter === 'unplaced' && c.placed) return false;
      if (!q) return true;
      return camName(c).toLowerCase().includes(q) || String(c.camera_id).toLowerCase().includes(q);
    });
  }

  function renderCount() {
    const n = state.cameras.length;
    const unplaced = state.cameras.filter((c) => !c.placed).length;
    els.count.textContent = n === 0 ? '' : (unplaced ? `${unplaced} not placed` : `all ${n} placed`);
    els.count.classList.toggle('is-todo', unplaced > 0);
  }

  function renderList() {
    if (!els.list) return;
    if (state.filter === null) state.filter = state.cameras.some((c) => !c.placed) ? 'unplaced' : 'all';
    els.segBtns.forEach((b) => b.setAttribute('aria-pressed', b.dataset.filter === state.filter ? 'true' : 'false'));
    renderCount();

    if (observer) observer.disconnect();
    cards.clear();
    els.list.innerHTML = '';
    const list = visibleCameras();
    list.forEach((cam) => {
      const li = buildCard(cam);
      cards.set(String(cam.camera_id), li);
      els.list.appendChild(li);
      if (observer) observer.observe(li);
    });

    let empty = '';
    if (!state.cameras.length) {
      // Empty state: cameras are added in "Cameras & devices" on this page (js/help.js handles data-help-go).
      empty = 'No cameras yet. Add them in "Cameras & devices" on this page (a recorder adds all its channels at once), then drag each one onto the map. <button type="button" class="btn btn-xs btn-primary" data-help-go="add-cameras">Add cameras</button>';
    } else if (!list.length && state.query) {
      empty = `No camera matches “${esc(els.search.value.trim())}”.`;
    } else if (!list.length && state.filter === 'unplaced') {
      empty = 'Every camera is on the map. <button type="button" class="btn btn-xs" data-act="show-all">Show all cameras</button>';
    }
    els.empty.innerHTML = empty;
    els.empty.hidden = !empty;
    els.list.hidden = !list.length;
    els.refreshAll.disabled = !state.cameras.some((c) => c.enabled !== false) && !state.refreshRun;
  }

  function chips(cam) {
    const out = [];
    out.push(cam.placed
      ? `<span class="badge badge-success cg-chip">${ico('check', { size: 'sm' })}Placed</span>`
      : '<span class="badge badge-warning cg-chip">Not placed</span>');
    if (cam.calibrated) out.push(`<span class="badge badge-info cg-chip">${ico('scan', { size: 'sm' })}Calibrated</span>`);
    if (cam.enabled === false) out.push(`<span class="badge badge-neutral cg-chip">${ico('power', { size: 'sm' })}Off</span>`);
    return out.join('');
  }

  function buildCard(cam) {
    const id = String(cam.camera_id);
    const name = camName(cam);
    const li = document.createElement('li');
    li.className = 'cg-card';
    li.dataset.cameraId = id;
    li.classList.toggle('is-placed', !!cam.placed);
    li.classList.toggle('is-off', cam.enabled === false);
    li.classList.toggle('is-selected', state.selectedId === id);
    li.innerHTML = `
      <div class="cg-thumb">
        <img class="cg-img" alt="" decoding="async" draggable="false" hidden>
        <span class="cg-thumb-note"></span>
        <span class="badge badge-warning cg-old" hidden>${ico('clock', { size: 'sm' })}Old picture</span>
        <button type="button" class="btn btn-icon btn-xs cg-shot" aria-label="Refresh the picture of ${esc(name)}" title="Take a new picture">${ico('refresh-cw', { size: 'sm' })}</button>
      </div>
      <div class="cg-info">
        <button type="button" class="cg-name" title="${cam.placed ? 'Show this camera on the map' : 'Open this camera'}">${esc(name)}</button>
        <div class="cg-chips">${chips(cam)}</div>
      </div>
      <div class="cg-actions">
        ${canEdit() ? '<span class="cg-handle" aria-hidden="true" title="Drag onto the map"><span class="cg-grip"></span></span>' : ''}
        ${cam.placed || !canEdit() ? '' : `<button type="button" class="btn btn-xs cg-place" aria-label="Place ${esc(name)} in the middle of the map">${ico('pin', { size: 'sm' })}Place</button>`}
      </div>`;

    li.querySelector('.cg-name').addEventListener('click', (e) => { e.stopPropagation(); if (!dragJustEnded()) focusCamera(id); });
    li.addEventListener('click', (e) => {
      if (dragJustEnded() || e.target.closest('button')) return;
      focusCamera(id);
    });
    li.querySelector('.cg-shot').addEventListener('click', (e) => { e.stopPropagation(); refreshOne(id); });
    const place = li.querySelector('.cg-place');
    if (place) place.addEventListener('click', (e) => { e.stopPropagation(); placeAtCentre(id); });

    li.addEventListener('pointerenter', (e) => { if (e.pointerType === 'mouse' && !drag) showPreviewFor(id, { rect: li.getBoundingClientRect() }); });
    li.addEventListener('pointerleave', (e) => { if (e.pointerType === 'mouse') hidePreviewSoon(); });
    const nameBtn = li.querySelector('.cg-name');
    nameBtn.addEventListener('focus', () => { if (!drag) showPreviewFor(id, { rect: li.getBoundingClientRect() }); });
    nameBtn.addEventListener('blur', () => hidePreviewSoon());

    li.addEventListener('pointerdown', (e) => onCardPointerDown(e, cam, li));
    li.addEventListener('dragstart', (e) => e.preventDefault());

    updateThumb(li, cam);
    return li;
  }

  function camById(id) {
    return state.cameras.find((c) => String(c.camera_id) === String(id)) || null;
  }

  function updateThumb(li, cam) {
    const id = String(cam.camera_id);
    const C = cache();
    const entry = C ? C.peek(id) : null;
    const pend = C ? C.pending(id) : null;
    const err = C ? C.lastError(id) : null;
    const img = li.querySelector('.cg-img');
    const note = li.querySelector('.cg-thumb-note');
    const old = li.querySelector('.cg-old');
    const shot = li.querySelector('.cg-shot');
    const thumb = li.querySelector('.cg-thumb');

    if (entry) {
      if (img.getAttribute('src') !== entry.url) img.src = entry.url;
      img.hidden = false;
      img.title = `Captured ${ago(entry.captured_at)}`;
    } else {
      img.hidden = true;
      img.removeAttribute('src');
    }
    old.hidden = !(entry && entry.stale);

    let text = '';
    if (pend) text = pend === 'running' ? 'Getting picture…' : 'Waiting…';
    else if (!entry && cam.enabled === false) text = 'Camera is off';
    else if (!entry && err) text = "Couldn't get a picture";
    else if (!entry) text = state.transport === 'webrtc' ? 'No picture yet. Tap refresh to get one.' : 'No picture yet';
    note.textContent = text;
    note.hidden = !text;
    note.title = err && !entry ? [err.message, err.detail].filter(Boolean).join('. ') : '';
    thumb.classList.toggle('is-loading', !!pend);
    thumb.classList.toggle('has-picture', !!entry);
    thumb.classList.toggle('is-error', !entry && !!err && !pend);

    shot.disabled = cam.enabled === false || !!pend || !!state.refreshRun;
    shot.title = cam.enabled === false ? 'This camera is off, so no picture is taken' : (entry ? 'Take a new picture' : 'Get a picture');
  }

  function updateCard(id) {
    const li = cards.get(String(id));
    const cam = camById(id);
    if (li && cam) updateThumb(li, cam);
    if (preview.id === String(id) && preview.visible) renderPreview();
  }

  // Store network: fill in missing pictures for the cards that are on screen.
  // Over the public address each picture opens a direct video session, so
  // there it only happens when someone asks (refresh, hover).
  function autoFill(id) {
    const C = cache();
    const cam = camById(id);
    if (!C || !cam || cam.enabled === false || state.transport !== 'local') return;
    if (C.peek(id) || C.pending(id) || C.lastError(id) || document.hidden) return;
    C.capture(id, { enabled: cam.enabled !== false }).catch(() => { /* shown on the card */ });
  }

  function makeObserver() {
    if (typeof window.IntersectionObserver !== 'function') return null;
    return new IntersectionObserver((items) => {
      items.forEach((it) => { if (it.isIntersecting) autoFill(it.target.dataset.cameraId); });
    }, { root: null, rootMargin: '0px 120px 0px 120px', threshold: 0.1 });
  }

  // ------------------------------------------------------------ actions

  function focusCamera(id) {
    if (!state.api) return;
    try { state.api.focusCamera(id); } catch (e) { console.warn('camera gallery: focusCamera failed', e); }
    if (PHONE_MQ.matches) setCollapsed(true);
  }

  function refreshOne(id) {
    const C = cache();
    const cam = camById(id);
    if (!C || !cam) return;
    if (cam.enabled === false) { toast(`${camName(cam)} is off, so no picture is taken.`); return; }
    C.capture(id, { force: true, enabled: true }).catch((e) => {
      if (e && (e.code === 'aborted' || e.code === 'hidden')) return;
      toast(`${camName(cam)}: ${e && e.message ? e.message : "couldn't get a picture"}.`, 'error');
    });
  }

  function mapCentreWorld() {
    const api = state.api;
    if (!api) return null;
    let canvas = null;
    try { canvas = api.canvasElement(); } catch (_) { canvas = null; }
    if (!canvas) return null;
    const r = canvas.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) return null;
    return api.clientToWorld(r.left + r.width / 2, r.top + r.height / 2);
  }

  function placeAt(id, x, y) {
    const cam = camById(id);
    const name = camName(cam);
    return Promise.resolve()
      .then(() => state.api.placeCamera(id, x, y))
      .then(() => toast(`${name} is on the map. Drag it on the map to fine-tune.`, 'ok'))
      .catch((e) => toast(`Couldn't place ${name}: ${(e && e.message) || 'please try again'}.`, 'error'));
  }

  function placeAtCentre(id) {
    const w = mapCentreWorld();
    if (!w) { toast('Open the store map first, then press Place again.', 'error'); return; }
    placeAt(id, w.x_m, w.y_m);
  }

  // ------------------------------------------------------------ refresh all

  function startRefreshAll() {
    const C = cache();
    if (!C || state.refreshRun) return;
    const ids = state.cameras.filter((c) => c.enabled !== false).map((c) => String(c.camera_id));
    if (!ids.length) { toast('Every camera is off, so no pictures can be taken.'); return; }
    // Cards on screen first, so the person sees progress where they are looking.
    const shown = new Set(visibleCameras().map((c) => String(c.camera_id)));
    ids.sort((a, b) => (shown.has(b) ? 1 : 0) - (shown.has(a) ? 1 : 0));
    const ctl = new AbortController();
    state.refreshRun = { ctl };
    els.refreshAllLabel.textContent = 'Stop';
    els.refreshAll.setAttribute('aria-pressed', 'true');
    els.progress.hidden = false;
    cards.forEach((li, id) => { const cam = camById(id); if (cam) updateThumb(li, cam); });
    C.refreshAll(ids, {
      signal: ctl.signal,
      onProgress: (p) => {
        const n = Math.min(p.total, p.done + p.failed + (p.current ? 1 : 0));
        els.progressText.textContent = `Getting pictures: ${n} of ${p.total}`;
        els.progressBar.max = Math.max(1, p.total);
        els.progressBar.value = p.done + p.failed;
      },
    }).then((r) => {
      finishRefreshAll();
      if (r.reason === 'aborted') { toast(`Stopped. ${r.done} of ${r.total} pictures updated.`); return; }
      if (r.reason === 'hidden') { toast('Stopped because the page was hidden. Press “Refresh all pictures” to carry on.'); return; }
      if (r.reason === 'session_limit') { toast('Live video limit reached. Close a live view, then try again.', 'error'); return; }
      if (r.reason === 'unknown_network') { toast("Can't reach the box right now, so no pictures were taken.", 'error'); return; }
      if (r.reason === 'direct_unavailable') { toast(r.message || 'Direct video is off, so no pictures can be taken.', 'error'); return; }
      if (r.reason === 'signed-out') return;
      if (r.failed) toast(`${r.done} of ${r.total} pictures updated. ${r.failed} camera${r.failed === 1 ? '' : 's'} gave no picture.`, 'error');
      else toast(`All ${r.done} pictures updated.`, 'ok');
    }, () => finishRefreshAll());
  }

  function stopRefreshAll() {
    if (state.refreshRun) { try { state.refreshRun.ctl.abort(); } catch (_) {} }
  }

  function finishRefreshAll() {
    state.refreshRun = null;
    if (!els.panel) return;
    els.refreshAllLabel.textContent = 'Refresh all pictures';
    els.refreshAll.removeAttribute('aria-pressed');
    els.progress.hidden = true;
    cards.forEach((li, id) => { const cam = camById(id); if (cam) updateThumb(li, cam); });
  }

  // ------------------------------------------------------------ hover preview

  const preview = { el: null, id: null, visible: false, anchor: null, hideTimer: null, captureTimer: null };

  function ensurePreview() {
    if (preview.el) return preview.el;
    const el = document.createElement('div');
    el.className = 'cg-preview';
    el.setAttribute('aria-hidden', 'true');
    el.hidden = true;
    el.innerHTML = `
      <div class="cg-preview-pic">
        <img class="cg-preview-img" alt="" draggable="false" hidden>
        <span class="cg-preview-empty"></span>
      </div>
      <div class="cg-preview-cap">
        <span class="cg-preview-name"></span>
        <span class="cg-preview-time"></span>
      </div>`;
    document.body.appendChild(el);
    preview.el = el;
    return el;
  }

  function renderPreview() {
    const el = ensurePreview();
    const id = preview.id;
    const cam = camById(id) || { camera_id: id, name: id, enabled: true };
    const C = cache();
    const entry = C ? C.peek(id) : null;
    const pend = C ? C.pending(id) : null;
    const err = C ? C.lastError(id) : null;
    const img = el.querySelector('.cg-preview-img');
    const empty = el.querySelector('.cg-preview-empty');
    el.querySelector('.cg-preview-name').textContent = camName(cam);
    let time = '';
    if (entry) {
      if (img.getAttribute('src') !== entry.url) img.src = entry.url;
      img.hidden = false;
      empty.hidden = true;
      time = `captured ${ago(entry.captured_at)}${entry.stale ? ' · old picture' : ''}`;
    } else {
      img.hidden = true;
      img.removeAttribute('src');
      empty.hidden = false;
      empty.textContent = pend ? 'Getting picture…'
        : cam.enabled === false ? 'Camera is off'
          : err ? "Couldn't get a picture" : 'No picture yet';
    }
    if (!entry && pend) time = 'one moment…';
    el.querySelector('.cg-preview-time').textContent = time;
    el.classList.toggle('is-stale', !!(entry && entry.stale));
    position();
  }

  function position() {
    const el = preview.el;
    if (!el || !preview.anchor) return;
    el.hidden = false;
    el.style.left = '0px';
    el.style.top = '0px';
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const r = el.getBoundingClientRect();
    const w = r.width;
    const h = r.height;
    const m = 8;
    let x;
    let y;
    const a = preview.anchor;
    if (a.rect) {
      x = a.rect.left + a.rect.width / 2 - w / 2;
      y = a.rect.top - h - m;
      if (y < m) y = a.rect.bottom + m;
      if (y + h > vh - m) y = Math.max(m, vh - h - m);
    } else {
      x = a.x + 16;
      y = a.y + 16;
      if (x + w > vw - m) x = a.x - 16 - w;
      if (y + h > vh - m) y = a.y - 16 - h;
    }
    x = Math.min(Math.max(m, x), Math.max(m, vw - w - m));
    y = Math.min(Math.max(m, y), Math.max(m, vh - h - m));
    el.style.left = `${Math.round(x)}px`;
    el.style.top = `${Math.round(y)}px`;
  }

  function showPreviewFor(id, anchor) {
    if (!id) return;
    clearTimeout(preview.hideTimer);
    const sameCam = preview.id === String(id);
    preview.id = String(id);
    preview.anchor = anchor;
    preview.visible = true;
    renderPreview();
    if (!sameCam) {
      clearTimeout(preview.captureTimer);
      preview.captureTimer = setTimeout(() => captureForPreview(preview.id), HOVER_CAPTURE_DELAY_MS);
    }
  }

  // Someone lingers on a camera without a picture: take one (once).
  function captureForPreview(id) {
    if (!preview.visible || preview.id !== id) return;
    const C = cache();
    const cam = camById(id);
    if (!C || !cam || cam.enabled === false) return;
    if (C.peek(id) || C.pending(id) || C.lastError(id) || state.transport === 'unknown') return;
    C.capture(id, { enabled: true }).catch(() => {});
    renderPreview();
  }

  function hidePreviewSoon() {
    clearTimeout(preview.hideTimer);
    preview.hideTimer = setTimeout(hidePreview, PREVIEW_HIDE_DELAY_MS);
  }

  function hidePreview() {
    clearTimeout(preview.hideTimer);
    clearTimeout(preview.captureTimer);
    preview.visible = false;
    preview.id = null;
    if (preview.el) preview.el.hidden = true;
  }

  // ------------------------------------------------------------ drag and drop (pointer events)

  let drag = null;               // {id, cam, startX, startY, pointerId, active, ghost, world, touch}
  let dragEndedAt = 0;
  const dragJustEnded = () => Date.now() - dragEndedAt < 400;

  function onCardPointerDown(e, cam, li) {
    if (drag || !state.api || !canEdit()) return;
    if (e.button !== undefined && e.button !== 0) return;
    if (e.target.closest('button, input, a')) return;
    const fromHandle = !!e.target.closest('.cg-handle');
    // On touch the card scrolls the list; only the handle drags.
    if (e.pointerType !== 'mouse' && !fromHandle) return;
    if (e.pointerType === 'mouse') e.preventDefault();   // no text selection while dragging
    drag = {
      id: String(cam.camera_id), cam, li,
      startX: e.clientX, startY: e.clientY, pointerId: e.pointerId,
      active: false, ghost: null, world: null, touch: e.pointerType !== 'mouse',
    };
    if (fromHandle) { try { e.target.closest('.cg-handle').setPointerCapture(e.pointerId); } catch (_) {} }
    window.addEventListener('pointermove', onDragMove, true);
    window.addEventListener('pointerup', onDragUp, true);
    window.addEventListener('pointercancel', onDragCancel, true);
    window.addEventListener('keydown', onDragKey, true);
  }

  function startDrag() {
    drag.active = true;
    hidePreview();
    const g = document.createElement('div');
    g.className = 'cg-ghost';
    g.setAttribute('aria-hidden', 'true');
    const C = cache();
    const entry = C ? C.peek(drag.id) : null;
    g.innerHTML = `
      ${entry ? `<img class="cg-ghost-img" alt="" src="${esc(entry.url)}">` : `<span class="cg-ghost-icon">${ico('cctv', { size: 'md' })}</span>`}
      <span class="cg-ghost-text"><b class="cg-ghost-name">${esc(camName(drag.cam))}</b><span class="cg-ghost-hint">Drag onto the map</span></span>`;
    document.body.appendChild(g);
    drag.ghost = g;
    document.documentElement.classList.add('cg-drag-active');
    drag.li.classList.add('is-dragging');
    if (PHONE_MQ.matches && els.panel) {
      // Slide the sheet down to its header so the map is free to drop on.
      const peek = Math.max(0, els.panel.offsetHeight - els.head.offsetHeight);
      els.panel.style.setProperty('--cg-peek', `${peek}px`);
      els.panel.classList.add('is-drag-peek');
    }
  }

  function overGallery(x, y) {
    const hit = document.elementFromPoint(x, y);
    return !!(hit && els.panel && els.panel.contains(hit));
  }

  function onDragMove(e) {
    if (!drag || e.pointerId !== drag.pointerId) return;
    const dx = e.clientX - drag.startX;
    const dy = e.clientY - drag.startY;
    if (!drag.active) {
      if (Math.hypot(dx, dy) < DRAG_THRESHOLD_PX) return;
      startDrag();
    }
    e.preventDefault();
    const x = e.clientX;
    const y = e.clientY;
    // Under a finger the ghost sits above the touch point, where it can be seen.
    const gx = x + (drag.touch ? -40 : 14);
    const gy = y + (drag.touch ? -72 : 14);
    drag.ghost.style.transform = `translate(${Math.round(gx)}px, ${Math.round(gy)}px)`;
    let w = null;
    if (!overGallery(x, y)) {
      try { w = state.api.clientToWorld(x, y); } catch (_) { w = null; }
    }
    drag.world = w;
    const over = !!w;
    drag.ghost.classList.toggle('is-over', over);
    drag.ghost.classList.toggle('is-outside', over && w.inside === false);
    const hint = drag.ghost.querySelector('.cg-ghost-hint');
    hint.textContent = !over ? 'Drag onto the map' : (w.inside === false ? 'Outside the map: let go to choose' : 'Let go to place here');
    try { state.api.setDropPreview(over ? w.x_m : null, over ? w.y_m : null); } catch (_) {}
  }

  function onDragUp(e) {
    if (!drag || e.pointerId !== drag.pointerId) return;
    const d = drag;
    endDrag();
    if (!d.active) return;          // a plain click: the click handler deals with it
    if (d.world) placeAt(d.id, d.world.x_m, d.world.y_m);
  }

  function onDragCancel(e) {
    if (!drag || (e && e.pointerId !== undefined && e.pointerId !== drag.pointerId)) return;
    endDrag();
  }

  function onDragKey(e) {
    if (e.key === 'Escape' && drag) { e.preventDefault(); e.stopPropagation(); endDrag(); }
  }

  function endDrag() {
    const d = drag;
    drag = null;
    window.removeEventListener('pointermove', onDragMove, true);
    window.removeEventListener('pointerup', onDragUp, true);
    window.removeEventListener('pointercancel', onDragCancel, true);
    window.removeEventListener('keydown', onDragKey, true);
    if (!d) return;
    if (d.active) {
      dragEndedAt = Date.now();
      if (d.ghost) d.ghost.remove();
      d.li.classList.remove('is-dragging');
      document.documentElement.classList.remove('cg-drag-active');
      if (els.panel) els.panel.classList.remove('is-drag-peek');
      try { state.api.setDropPreview(null, null); } catch (_) {}
    }
  }

  // ------------------------------------------------------------ data from the map

  function setCameras(list) {
    state.cameras = (Array.isArray(list) ? list : []).filter((c) => c && c.camera_id != null)
      .slice().sort((a, b) => camName(a).localeCompare(camName(b), undefined, { numeric: true, sensitivity: 'base' }));
    // No choice stored yet: on a desktop the panel starts closed once every
    // camera is on the map, so the map keeps its full height in daily use
    // (the panel sits under the map and takes ~300 px). Not saved as a choice.
    if (!state.autoCollapseDone && state.cameras.length) {
      state.autoCollapseDone = true;
      if (typeof prefs.collapsed !== 'boolean' && !PHONE_MQ.matches && state.cameras.every((c) => c.placed)) {
        state.collapsed = true;
        applyCollapsed();
      }
    }
    renderList();
  }

  function refreshTransport() {
    const C = cache();
    if (!C) return;
    C.transport().then((t) => {
      state.transport = t;
      cards.forEach((li, id) => { const cam = camById(id); if (cam) updateThumb(li, cam); });
      if (t === 'local') cards.forEach((li, id) => { if (li.offsetParent !== null) autoFill(id); });
    });
  }

  function waitForApi() {
    return new Promise((resolve) => {
      let tries = 0;
      const tick = () => {
        const api = window.FloorplanAPI;
        if (api && api.ready && typeof api.getCameras === 'function') { resolve(api); return; }
        tries += 1;
        setTimeout(tick, Math.min(1000, 100 + tries * 50));
      };
      tick();
    });
  }

  async function start() {
    const host = document.getElementById('cameraGalleryMount');
    if (!host) return;
    if (window.edgeAuth && typeof window.edgeAuth.state === 'function' && window.edgeAuth.state() === 'invalid') return;
    const api = await waitForApi();
    try { await api.ready; } catch (e) { console.warn('camera gallery: the map did not load', e); }
    state.api = api;
    observer = makeObserver();
    buildShell(host);
    if (cache() && cache().ready) { try { await cache().ready; } catch (_) {} }
    let list = [];
    try { list = api.getCameras(); } catch (_) { list = []; }
    setCameras(list);
    refreshTransport();

    window.addEventListener('fp:layout-changed', (e) => {
      const d = e && e.detail;
      let cams = d && Array.isArray(d.cameras) ? d.cameras : null;
      if (!cams) { try { cams = api.getCameras(); } catch (_) { cams = []; } }
      setCameras(cams);
    });
    window.addEventListener('fp:camera-hover', (e) => {
      if (drag && drag.active) return;
      const d = (e && e.detail) || {};
      if (d.camera_id == null) { hidePreviewSoon(); return; }
      showPreviewFor(d.camera_id, { x: Number(d.clientX) || 0, y: Number(d.clientY) || 0 });
    });
    window.addEventListener('fp:camera-selected', (e) => {
      const id = e && e.detail && e.detail.camera_id != null ? String(e.detail.camera_id) : null;
      state.selectedId = id;
      cards.forEach((li, cid) => li.classList.toggle('is-selected', cid === id));
    });
    window.addEventListener('edge:still-updated', (e) => { if (e && e.detail) updateCard(e.detail.camera_id); });
    window.addEventListener('edge:live-transport', refreshTransport);
    window.addEventListener('scroll', () => { if (preview.visible && preview.anchor && preview.anchor.rect) hidePreview(); }, { passive: true, capture: true });
    window.addEventListener('resize', () => { if (preview.visible) hidePreview(); });
    if (typeof PHONE_MQ.addEventListener === 'function') PHONE_MQ.addEventListener('change', () => { renderHint(); applyCollapsed(); });
    // "captured 5 min ago" stays true while the gallery is open (labels only; no network).
    setInterval(() => {
      if (document.hidden || !els.panel || state.collapsed) return;
      cards.forEach((li, id) => { const cam = camById(id); if (cam) updateThumb(li, cam); });
    }, 60000);
  }

  /** Open the gallery at "Not placed" (help.js go 'place-cameras'); every camera placed: all of them. */
  function showUnplaced() {
    if (!els.panel) return false;
    state.filter = state.cameras.some((c) => !c.placed) ? 'unplaced' : 'all';
    writePrefs();
    if (state.collapsed) setCollapsed(false);
    renderList();
    try { els.panel.scrollIntoView({ block: 'start', behavior: 'smooth' }); } catch (_) { els.panel.scrollIntoView(); }
    const first = els.list && els.list.querySelector('.cg-place, .cg-name');
    if (first) { try { first.focus({ preventScroll: true }); } catch (_) {} }
    return true;
  }
  window.EdgeCameraGallery = { showUnplaced };

  function boot() {
    if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') window.edgeAuth.onReady(start);
    else if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
  }

  boot();
})();
