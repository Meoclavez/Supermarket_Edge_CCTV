/**
 * Live AI overlay drawn by the page: every confirmed person the box tracks on
 * a live view, with its box, its pose skeleton (17 COCO keypoints) and its
 * live behaviour (normal / watch / alert, with the cues that make it so).
 * A person at "alert" (an incident fired) is coloured by the incident's alert
 * level (behaviour.tier: critical / alert / watch / review) and the chip names
 * that level; an alert without a level keeps the old alert colour and "ALERT".
 *
 * Data: GET /api/v1/live/tracks?cameras=<id>,<id>... (one request for every
 * view on screen, at most 16 cameras), polled at about the analysed rate of
 * those cameras, never faster than 5 per second and not at all while the tab
 * is hidden or no view is showing. Coordinates are normalised (0..1) to the
 * analysed frame, so the same track fits the store-network MJPEG picture and
 * the direct (WebRTC) video alike. The canvas covers the picture's content
 * box (object-fit: contain letterboxing), at devicePixelRatio, and is redrawn
 * on resize. A fresh track moves on by its velocity for at most 0.3 s.
 *
 * Nothing is invented: when the box does not answer, does not offer live
 * tracks (404) or has not analysed a camera yet, the view says so on its
 * status badge and draws nothing.
 *
 *   const v = EdgeLiveOverlay.attach({ key, cameraId, stage, before, media(), isActive(), statusEl, scale() })
 *   v.setCamera(id); v.redraw(); v.isShowing(); v.detach()
 *   EdgeLiveOverlay.isEnabled(key) / setEnabled(key, on) / toggle(key)   "Show AI overlay", per view, remembered
 *   EdgeLiveOverlay.clientDraws(key)   true while this page draws the overlay (the server picture should not)
 *   EdgeLiveOverlay.status()           {state: 'unknown'|'ok'|'unavailable'|'denied'|'error', reason}
 *   EdgeLiveOverlay.highlight(cameraId, trackId, ms)
 *   EdgeLiveOverlay.refresh()          re-check which views are showing (after a focus / transport change)
 * Events: window 'edge:live-overlay' {detail: {key?, enabled?, state}} on a toggle or a status change.
 */
(function () {
  'use strict';

  const API = '/api/v1/live/tracks';
  const MAX_IDS = 16;
  const VIS_MIN = 0.3;                 // joints below this visibility are not drawn
  // The picture on screen is older than the server clock (decode, stream and
  // network delay), so tracks are moved on only by the analysis delay beyond
  // that; extrapolating to "now" put boxes ~0.2 s ahead of a panning picture.
  const VIDEO_DELAY_S = 0.2;
  const EXTRAPOLATE_MAX_S = 0.3;
  const SKELETON_MAX_AGE_S = 1.0;      // a coasting track keeps its last pose this long
  const STALE_S = 5;                   // analysed frame older than this: the badge says so
  const MAX_HZ = 5;
  const MIN_HZ = 1;
  const DEFAULT_HZ = 2;
  const IDLE_CHECK_MS = 1000;
  const UNAVAILABLE_RETRY_MS = 60000;
  const PREF_KEY = 'edge.aiOverlay.v1';
  // COCO-17: nose, eyes, ears, shoulders, elbows, wrists, hips, knees, ankles.
  const LIMBS = [[15, 13], [13, 11], [16, 14], [14, 12], [11, 12], [5, 11], [6, 12], [5, 6], [5, 7], [6, 8],
    [7, 9], [8, 10], [1, 2], [0, 1], [0, 2], [1, 3], [2, 4], [3, 5], [4, 6]];
  const LEVEL_WORD = { alert: 'ALERT', watch: 'Watch' };
  // Alert level of the fired incident (services/theft_alert_policy.py), ascending.
  const TIERS = ['review', 'watch', 'alert', 'critical'];
  const TIER_WORD = { critical: 'CRITICAL', alert: 'ALERT', watch: 'WATCH LEVEL', review: 'REVIEW LEVEL' };
  // Palette key per level (the --lo-tier-* colours in css/live_view.css).
  const TIER_COLOR = { critical: 'tierCritical', alert: 'tierAlert', watch: 'tierWatch', review: 'tierReview' };

  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

  // ---------------------------------------------------------------- pure helpers (tested under Node)

  /** Where a W x H picture sits in a boxW x boxH element with object-fit: contain. */
  function contentRect(boxW, boxH, natW, natH) {
    if (!(boxW > 0 && boxH > 0)) return null;
    if (!(natW > 0 && natH > 0)) return { x: 0, y: 0, w: boxW, h: boxH };
    const s = Math.min(boxW / natW, boxH / natH);
    const w = natW * s;
    const h = natH * s;
    return { x: (boxW - w) / 2, y: (boxH - h) / 2, w, h };
  }

  /** Seconds to move a track on by its velocity: only fresh tracks, minus the
   *  picture's own delay, never more than EXTRAPOLATE_MAX_S. */
  function extrapolationSeconds(track, serverNow, analysedAt) {
    if (!track || !track.fresh || !Array.isArray(track.velocity) || !isNum(serverNow) || !isNum(analysedAt)) return 0;
    if (!isNum(track.velocity[0]) || !isNum(track.velocity[1])) return 0;
    return clamp(serverNow - analysedAt - VIDEO_DELAY_S, 0, EXTRAPOLATE_MAX_S);
  }

  /** The colour role of a track: 'static' | 'alert' | 'watch' | 'normal' ("pending_static" is not static yet). */
  function levelOf(track) {
    if (track && track.motion_state === 'static') return 'static';
    const lvl = track && track.behaviour && track.behaviour.level;
    return lvl === 'alert' || lvl === 'watch' ? lvl : 'normal';
  }

  /** The alert level of the incident fired on this person (only at level "alert"), or null. */
  function tierOf(track) {
    if (levelOf(track) !== 'alert') return null;
    const t = track.behaviour.tier;
    return TIERS.includes(t) ? t : null;
  }

  /** Palette key of a track: its level's, or for a fired incident with a level, that level's. */
  function colorKey(track) {
    const tier = tierOf(track);
    return tier ? TIER_COLOR[tier] : levelOf(track);
  }

  /** The chip over a track, or '' for an ordinary person. */
  function chipText(track) {
    const level = levelOf(track);
    if (level === 'static') return 'static';
    const b = track && track.behaviour;
    const labels = b && Array.isArray(b.labels) ? b.labels.filter((x) => typeof x === 'string' && x) : [];
    if (level === 'normal') return labels.slice(0, 2).join(' · ');
    const tier = tierOf(track);
    return [tier ? TIER_WORD[tier] : LEVEL_WORD[level], ...labels.slice(0, 3)].join(' · ');
  }

  /** Normalised box moved on by dt seconds: [x1, y1, x2, y2] or null. */
  function movedBox(track, dt) {
    const b = track && track.box;
    if (!Array.isArray(b) || b.length < 4 || !b.every(isNum)) return null;
    const vx = dt && Array.isArray(track.velocity) && isNum(track.velocity[0]) ? track.velocity[0] * dt : 0;
    const vy = dt && Array.isArray(track.velocity) && isNum(track.velocity[1]) ? track.velocity[1] * dt : 0;
    return [b[0] + vx, b[1] + vy, b[2] + vx, b[3] + vy];
  }

  /** Poll interval for the analysed rates of the cameras on screen (ms). */
  function pollDelayMs(rates) {
    const known = (rates || []).filter((r) => isNum(r) && r > 0);
    const hz = known.length ? clamp(Math.max(...known), MIN_HZ, MAX_HZ) : DEFAULT_HZ;
    return Math.round(1000 / hz);
  }

  // ---------------------------------------------------------------- preferences

  const prefMemory = {};
  function readPrefs() {
    try { return JSON.parse(window.localStorage.getItem(PREF_KEY) || '{}') || {}; } catch (_) { return {}; }
  }
  function isEnabled(key) {
    const k = key || 'default';
    if (k in prefMemory) return prefMemory[k];
    const p = readPrefs();
    prefMemory[k] = p[k] !== false;           // on unless this viewer turned it off
    return prefMemory[k];
  }
  function setEnabled(key, on) {
    const k = key || 'default';
    prefMemory[k] = !!on;
    try {
      const p = readPrefs();
      p[k] = !!on;
      window.localStorage.setItem(PREF_KEY, JSON.stringify(p));
    } catch (_) { /* storage blocked: the choice lasts for this visit */ }
    announce({ key: k, enabled: !!on });
    redrawAll();
    schedule(0);
  }

  // ---------------------------------------------------------------- shared state

  const views = new Set();
  const byCam = new Map();          // camera id -> {cam, offset (server - local seconds), at (local ms)}
  const highlights = new Map();     // camera id -> {trackId, until}
  const net = { state: 'unknown', reason: '', checkedAt: 0, failures: 0 };
  let timer = null;
  let busy = false;
  let raf = 0;

  function announce(extra) {
    try {
      window.dispatchEvent(new CustomEvent('edge:live-overlay', { detail: Object.assign({ state: net.state }, extra || {}) }));
    } catch (_) { /* very old browser */ }
  }

  function setNet(state, reason) {
    const changed = net.state !== state;
    net.state = state;
    net.reason = reason || '';
    if (changed) announce();
  }

  function clientDraws(key) {
    return isEnabled(key) && net.state !== 'unavailable' && net.state !== 'denied';
  }

  function cssVar(name, fallback) {
    try {
      const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fallback;
    } catch (_) { return fallback; }
  }

  // Read once; colours over footage are fixed, but a theme switch re-reads them.
  let palCache = null;
  window.addEventListener('edge:theme', () => { palCache = null; redrawAll(); });
  function palette() {
    if (palCache) return palCache;
    palCache = {
      normal: cssVar('--lo-normal', '#3ddc97'),
      watch: cssVar('--lo-watch', '#ffb020'),
      alert: cssVar('--lo-alert', '#ff4d5e'),
      static: cssVar('--lo-static', '#9aa4b2'),
      tierCritical: cssVar('--lo-tier-critical', '#ff2d55'),
      tierAlert: cssVar('--lo-tier-alert', '#ff4d5e'),
      tierWatch: cssVar('--lo-tier-watch', '#ff9f43'),
      tierReview: cssVar('--lo-tier-review', '#8fb8ff'),
      highlight: cssVar('--lo-highlight', '#ffffff'),
      chipText: cssVar('--lo-chip-text', '#0b0f14'),
      joint: cssVar('--lo-joint', '#ffffff'),
    };
    return palCache;
  }

  // ---------------------------------------------------------------- one view

  function viewActive(v) {
    if (!v.stage || !v.stage.isConnected || !v.cameraId || !isEnabled(v.key)) return false;
    try { return !!v.isActive(); } catch (_) { return false; }
  }

  function mediaEl(v) {
    try { return v.media ? v.media() : null; } catch (_) { return null; }
  }

  function naturalSize(m, cam) {
    if (m && m.tagName === 'VIDEO' && m.videoWidth) return { w: m.videoWidth, h: m.videoHeight };
    if (m && m.tagName === 'IMG' && m.naturalWidth) return { w: m.naturalWidth, h: m.naturalHeight };
    if (cam && isNum(cam.frame_width) && isNum(cam.frame_height)) return { w: cam.frame_width, h: cam.frame_height };
    return { w: 0, h: 0 };
  }

  /** Put the canvas over the picture's content box; returns its CSS rect or null. */
  function placeCanvas(v, cam) {
    const m = mediaEl(v);
    const shown = m && m.clientWidth > 0 && m.clientHeight > 0;
    const bw = shown ? m.clientWidth : v.stage.clientWidth;
    const bh = shown ? m.clientHeight : v.stage.clientHeight;
    const ox = shown && m.offsetParent === v.stage ? m.offsetLeft : 0;
    const oy = shown && m.offsetParent === v.stage ? m.offsetTop : 0;
    const nat = naturalSize(m, cam);
    const r = contentRect(bw, bh, nat.w, nat.h);
    if (!r) return null;
    const c = v.canvas;
    const dpr = clamp(window.devicePixelRatio || 1, 1, 3);
    const zoom = clamp(Number(v.scale()) || 1, 1, 6);
    // Sharp lines when zoomed in, within a sane canvas size.
    const k = Math.min(dpr * zoom, 4096 / Math.max(1, r.w), 4096 / Math.max(1, r.h));
    const pw = Math.max(1, Math.round(r.w * k));
    const ph = Math.max(1, Math.round(r.h * k));
    const sig = `${ox + r.x}|${oy + r.y}|${r.w}|${r.h}`;
    if (v.placeSig !== sig) {
      v.placeSig = sig;
      c.style.left = `${ox + r.x}px`;
      c.style.top = `${oy + r.y}px`;
      c.style.width = `${r.w}px`;
      c.style.height = `${r.h}px`;
    }
    if (c.width !== pw) c.width = pw;
    if (c.height !== ph) c.height = ph;
    return { w: r.w, h: r.h, k, zoom };
  }

  function setStatus(v, text, kind, tip) {
    const s = v.statusEl;
    if (!s) return;
    const show = !!text;
    if (s.hidden === show) s.hidden = !show;
    if (s.textContent !== (text || '')) s.textContent = text || '';
    s.title = tip || '';
    s.classList.toggle('lo-status-ok', kind === 'ok');
    s.classList.toggle('lo-status-warn', kind === 'warn');
    s.classList.toggle('lo-status-off', kind === 'off');
  }

  /** Honest one-line state of a view's AI data; returns {text, kind, tip}. */
  function describe(v, entry, active) {
    if (!active) return { text: '', kind: '' };
    if (net.state === 'unavailable') {
      return { text: 'AI: live AI data unavailable', kind: 'off', tip: net.reason || 'This box does not offer live AI data (GET /api/v1/live/tracks).' };
    }
    if (net.state === 'denied') return { text: 'AI: not allowed for this sign-in', kind: 'off', tip: net.reason };
    if (net.state === 'error' && !entry) return { text: 'AI: live AI data unavailable', kind: 'warn', tip: `No answer from the box: ${net.reason}. Retrying.` };
    if (net.state === 'unknown' && !entry) return { text: 'AI: loading…', kind: '' };
    if (!entry) return { text: 'AI: no live data for this camera', kind: 'off', tip: 'The box did not report this camera (not running or not analysed).' };
    const cam = entry.cam;
    if (!isNum(cam.analysed_at)) return { text: 'AI: not analysed yet', kind: 'warn', tip: 'No frame of this camera has been analysed yet.' };
    const serverNow = Date.now() / 1000 + entry.offset;
    const age = serverNow - cam.analysed_at;
    const hl = highlights.get(v.cameraId);
    if (hl && hl.until > Date.now() && !(cam.tracks || []).some((t) => t.track_id === hl.trackId)) {
      return { text: 'AI: the flagged person is no longer tracked', kind: 'warn', tip: `Track ${hl.trackId} is not in this camera's live tracks now.` };
    }
    if (age > STALE_S) return { text: `AI: data ${Math.round(age)} s old`, kind: 'warn', tip: 'The newest analysed frame of this camera is this old.' };
    const fps = isNum(cam.analysis_fps) ? cam.analysis_fps : null;
    return {
      text: fps !== null ? `AI ${fps < 10 ? fps.toFixed(1) : Math.round(fps)} fps` : 'AI live',
      kind: 'ok',
      tip: fps !== null ? `Frames analysed per second on this camera (measured). Change it in Settings > Analysis speed.` : '',
    };
  }

  /** A label ending at y; kept inside the W x H picture (a person at the edge would cut it off). */
  function chip(g, text, x, y, color, textColor, scale, W, H) {
    const fs = 11 / scale;
    g.font = `700 ${fs}px ui-sans-serif, system-ui, sans-serif`;
    const padX = 4 / scale;
    const h = fs + 6 / scale;
    const w = g.measureText(text).width + padX * 2;
    const xx = Math.max(0, Math.min(x, W - w));
    const yy = Math.max(0, Math.min(y - h, H - h));
    g.fillStyle = color;
    g.fillRect(xx, yy, w, h);
    g.fillStyle = textColor;
    g.textBaseline = 'middle';
    g.fillText(text, xx + padX, yy + h / 2);
  }

  /** Draw one view; returns true while something on it still moves (extrapolation). */
  function draw(v) {
    const c = v.canvas;
    const g = c.getContext('2d');
    const active = viewActive(v);
    const entry = active ? byCam.get(v.cameraId) : null;
    const st = describe(v, entry, active);
    setStatus(v, st.text, st.kind, st.tip);
    const showing = active && !!entry && net.state !== 'unavailable' && net.state !== 'denied';
    v.showing = showing;
    if (!showing) {
      if (!c.hidden) { c.hidden = true; if (g) g.clearRect(0, 0, c.width, c.height); }
      return false;
    }
    const cam = entry.cam;
    const rect = placeCanvas(v, cam);
    if (!rect || !g) return false;
    c.hidden = false;
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.clearRect(0, 0, c.width, c.height);
    g.setTransform(rect.k, 0, 0, rect.k, 0, 0);
    const W = rect.w;
    const H = rect.h;
    const zoom = rect.zoom;
    const pal = palette();
    const serverNow = Date.now() / 1000 + entry.offset;
    const hl = highlights.get(v.cameraId);
    const hlOn = hl && hl.until > Date.now() ? hl.trackId : null;
    let moving = false;
    (Array.isArray(cam.tracks) ? cam.tracks : []).forEach((t) => {
      if (!t || !t.confirmed) return;
      const dt = extrapolationSeconds(t, serverNow, cam.analysed_at);
      if (dt > 0 && dt < EXTRAPOLATE_MAX_S) moving = true;
      const box = movedBox(t, dt);
      if (!box) return;
      const level = levelOf(t);
      const tier = tierOf(t);
      const color = pal[colorKey(t)] || pal[level];
      const dx = dt && Array.isArray(t.velocity) ? (t.velocity[0] || 0) * dt : 0;
      const dy = dt && Array.isArray(t.velocity) ? (t.velocity[1] || 0) * dt : 0;
      const x = box[0] * W;
      const y = box[1] * H;
      const w = (box[2] - box[0]) * W;
      const h = (box[3] - box[1]) * H;
      g.save();
      g.globalAlpha = t.fresh ? 1 : 0.75;
      g.strokeStyle = color;
      g.lineWidth = (tier === 'critical' ? 3.4 : level === 'alert' ? 2.6 : 1.8) / zoom;
      g.setLineDash(t.fresh ? [] : [6 / zoom, 4 / zoom]);
      g.strokeRect(x, y, w, h);
      if (tier === 'critical') {
        // A second, outer box: Critical stands out on footage even for colour-blind viewers.
        g.lineWidth = 1.4 / zoom;
        g.strokeRect(x - 4 / zoom, y - 4 / zoom, w + 8 / zoom, h + 8 / zoom);
      }
      g.restore();

      // Skeleton: the last matched pose, kept for a second while the track coasts.
      const age = isNum(t.age_sec) ? t.age_sec : (t.fresh ? 0 : Infinity);
      if (Array.isArray(t.keypoints) && t.keypoints.length >= 17 && level !== 'static' && (t.fresh || age <= SKELETON_MAX_AGE_S)) {
        const kp = t.keypoints.map((p) => (Array.isArray(p) && isNum(p[0]) && isNum(p[1])
          ? { x: (p[0] + dx) * W, y: (p[1] + dy) * H, ok: isNum(p[2]) ? p[2] >= VIS_MIN : false } : { ok: false }));
        g.save();
        g.globalAlpha = t.fresh ? 0.95 : 0.55;
        g.strokeStyle = color;
        g.lineWidth = 2 / zoom;
        g.lineCap = 'round';
        g.beginPath();
        LIMBS.forEach(([a, b]) => {
          if (!kp[a].ok || !kp[b].ok) return;
          g.moveTo(kp[a].x, kp[a].y);
          g.lineTo(kp[b].x, kp[b].y);
        });
        g.stroke();
        g.fillStyle = pal.joint;
        kp.forEach((p) => {
          if (!p.ok) return;
          g.beginPath();
          g.arc(p.x, p.y, 2.2 / zoom, 0, Math.PI * 2);
          g.fill();
        });
        g.restore();
      }

      if (hlOn && t.track_id === hlOn) {
        g.save();
        g.strokeStyle = pal.highlight;
        g.lineWidth = 3 / zoom;
        g.setLineDash([8 / zoom, 5 / zoom]);
        g.strokeRect(x - 4 / zoom, y - 4 / zoom, w + 8 / zoom, h + 8 / zoom);
        g.restore();
        chip(g, 'Flagged person', x, y + h + 18 / zoom, pal.highlight, pal.chipText, zoom, W, H);
      }
      const text = chipText(t);
      if (text) chip(g, text, x, y, color, pal.chipText, zoom, W, H);
    });
    return moving;
  }

  function redrawAll() {
    let moving = false;
    views.forEach((v) => {
      if (!v.stage.isConnected) return;
      try { if (draw(v)) moving = true; } catch (_) { /* one broken view must not stop the others */ }
    });
    if (moving && !raf && !document.hidden) {
      // Smooth movement between polls, at most ~20 redraws a second.
      let last = 0;
      const step = (ts) => {
        raf = 0;
        if (ts - last < 50) { raf = requestAnimationFrame(step); return; }
        last = ts;
        let still = false;
        views.forEach((v) => { if (v.stage.isConnected) { try { if (draw(v)) still = true; } catch (_) { /* ignore */ } } });
        if (still && !document.hidden) raf = requestAnimationFrame(step);
      };
      raf = requestAnimationFrame(step);
    }
  }

  // ---------------------------------------------------------------- polling

  function activeIds() {
    const ids = [];
    views.forEach((v) => {
      if (!v.stage.isConnected) { views.delete(v); return; }
      if (viewActive(v) && !ids.includes(v.cameraId)) ids.push(v.cameraId);
    });
    return ids.slice(0, MAX_IDS);
  }

  function signedIn() {
    if (typeof window.canPoll === 'function') return window.canPoll();
    if (window.edgeAuth && typeof window.edgeAuth.isAuthenticated === 'function') return window.edgeAuth.isAuthenticated();
    return true;
  }

  function schedule(ms) {
    if (!views.size || document.hidden) { clearTimeout(timer); timer = null; return; }
    clearTimeout(timer);
    timer = setTimeout(poll, Math.max(0, ms));
  }

  async function poll() {
    timer = null;
    if (busy) return;
    const ids = activeIds();
    redrawAll();
    if (!ids.length || !signedIn()) { schedule(IDLE_CHECK_MS); return; }
    if (net.state === 'unavailable' && Date.now() - net.checkedAt < UNAVAILABLE_RETRY_MS) { schedule(IDLE_CHECK_MS); return; }
    busy = true;
    const started = Date.now();
    try {
      const res = await fetch(`${API}?cameras=${ids.map(encodeURIComponent).join(',')}`, { cache: 'no-store' });
      if (res.status === 404 || res.status === 405 || res.status === 501) {
        byCam.clear();
        setNet('unavailable', 'This box does not offer live AI data yet (GET /api/v1/live/tracks answered '
          + `HTTP ${res.status}). The overlay is hidden; nothing is drawn from guesses.`);
      } else if (res.status === 401 || res.status === 403) {
        byCam.clear();
        let why = '';
        try { const b = await res.json(); why = (b && (b.detail || b.message)) || ''; } catch (_) { /* not JSON */ }
        setNet(res.status === 401 ? 'error' : 'denied', typeof why === 'string' && why ? why : `HTTP ${res.status}`);
      } else if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      } else {
        const body = await res.json();
        const local = Date.now();
        const offset = isNum(body && body.server_time) ? body.server_time - local / 1000 : 0;
        const cams = (body && body.cameras && typeof body.cameras === 'object') ? body.cameras : {};
        ids.forEach((id) => {
          const cam = cams[id];
          if (cam && typeof cam === 'object') byCam.set(id, { cam, offset, at: local });
          else byCam.delete(id);   // unknown to the box: omitted, never guessed
        });
        net.failures = 0;
        setNet('ok', '');
      }
    } catch (e) {
      net.failures += 1;
      // Keep the last data a short while only; older than that it is not "live".
      byCam.forEach((entry, id) => { if (Date.now() - entry.at > 5000) byCam.delete(id); });
      setNet('error', (e && e.message) || 'no answer');
    } finally {
      busy = false;
      net.checkedAt = Date.now();
    }
    redrawAll();
    let delay;
    if (net.state === 'error') delay = Math.min(30000, 1000 * 2 ** Math.min(5, net.failures));
    else if (net.state === 'unavailable') delay = IDLE_CHECK_MS;
    else {
      const rates = ids.map((id) => { const e = byCam.get(id); return e ? e.cam.analysis_fps : null; });
      delay = pollDelayMs(rates) - (Date.now() - started);
    }
    schedule(Math.max(50, delay));
  }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      clearTimeout(timer); timer = null;
      if (raf) { cancelAnimationFrame(raf); raf = 0; }
    } else {
      schedule(0);
    }
  });
  window.addEventListener('resize', () => redrawAll());
  window.addEventListener('edge:auth', () => { byCam.clear(); net.state = 'unknown'; net.checkedAt = 0; schedule(0); });

  // ---------------------------------------------------------------- views

  function attach(o) {
    if (!o || !o.stage) return null;
    const v = {
      key: o.key || 'default',
      cameraId: o.cameraId || null,
      stage: o.stage,
      media: o.media || null,
      isActive: typeof o.isActive === 'function' ? o.isActive : () => true,
      statusEl: o.statusEl || null,
      scale: typeof o.scale === 'function' ? o.scale : () => 1,
      canvas: document.createElement('canvas'),
      placeSig: '',
      showing: false,
      ro: null,
    };
    v.canvas.className = 'lo-canvas';
    v.canvas.hidden = true;
    v.canvas.setAttribute('aria-hidden', 'true');
    if (o.before && o.before.parentNode === o.stage) o.stage.insertBefore(v.canvas, o.before);
    else o.stage.appendChild(v.canvas);
    const redraw = () => { v.placeSig = ''; try { draw(v); } catch (_) { /* ignore */ } };
    if (window.ResizeObserver) {
      v.ro = new ResizeObserver(redraw);
      v.ro.observe(o.stage);
    }
    // A new picture size (first frame, stream switch) moves the content box.
    // An MJPEG <img> may fire "load" for every frame: only a size change counts.
    let lastNat = '';
    v.onMedia = () => {
      const n = naturalSize(mediaEl(v), null);
      const sig = `${n.w}x${n.h}`;
      if (sig === lastNat) return;
      lastNat = sig;
      redraw();
    };
    o.stage.addEventListener('load', v.onMedia, true);
    o.stage.addEventListener('loadedmetadata', v.onMedia, true);
    o.stage.addEventListener('resize', v.onMedia, true);
    views.add(v);
    schedule(0);
    return {
      canvas: v.canvas,
      setCamera(id) {
        if (v.cameraId === id) return;
        v.cameraId = id || null;
        v.placeSig = '';
        redraw();
        schedule(0);
      },
      redraw,
      refresh: () => { redraw(); schedule(0); },
      isShowing: () => v.showing,
      detach() {
        views.delete(v);
        if (v.ro) v.ro.disconnect();
        o.stage.removeEventListener('load', v.onMedia, true);
        o.stage.removeEventListener('loadedmetadata', v.onMedia, true);
        o.stage.removeEventListener('resize', v.onMedia, true);
        v.canvas.remove();
        setStatus(v, '', '');
      },
    };
  }

  function highlight(cameraId, trackId, ms) {
    if (!cameraId || !trackId) return;
    highlights.set(cameraId, { trackId: String(trackId), until: Date.now() + (isNum(ms) ? ms : 15000) });
    redrawAll();
    schedule(0);
  }

  window.EdgeLiveOverlay = {
    attach,
    isEnabled,
    setEnabled,
    toggle: (key) => setEnabled(key, !isEnabled(key)),
    clientDraws,
    status: () => ({ state: net.state, reason: net.reason }),
    highlight,
    refresh: () => { redrawAll(); schedule(0); },
    _test: { contentRect, extrapolationSeconds, levelOf, tierOf, colorKey, chipText, movedBox, pollDelayMs, LIMBS, VIS_MIN },
  };
})();
