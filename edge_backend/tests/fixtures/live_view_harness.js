// Runs static/js/live_overlay.js and static/js/view_zoom.js in a tiny fake
// browser (no server, no real browser) and prints one JSON line of results.
// Used by tests/test_live_view_frontend.py.
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const staticDir = process.argv[2];

function makeCtx(log) {
  const ctx = {};
  ['clearRect', 'setTransform', 'save', 'restore', 'strokeRect', 'beginPath', 'moveTo', 'lineTo', 'stroke', 'arc',
    'fill', 'fillRect', 'setLineDash'].forEach((m) => { ctx[m] = (...a) => log.push([m, ...a]); });
  ctx.fillText = (t, x, y) => log.push(['fillText', t, x, y]);
  ctx.measureText = (t) => ({ width: String(t).length * 6 });
  return ctx;
}

function makeEl(tag, extra) {
  const listeners = {};
  return Object.assign({
    tagName: tag.toUpperCase(),
    style: {},
    hidden: false,
    isConnected: true,
    children: [],
    attrs: {},
    className: '',
    textContent: '',
    title: '',
    classList: { set: new Set(), toggle(c, on) { if (on) this.set.add(c); else this.set.delete(c); }, add(c) { this.set.add(c); }, remove(c) { this.set.delete(c); } },
    setAttribute(k, v) { this.attrs[k] = v; },
    getAttribute(k) { return this.attrs[k]; },
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
    insertBefore(c, ref) { const i = this.children.indexOf(ref); this.children.splice(i < 0 ? this.children.length : i, 0, c); c.parentNode = this; return c; },
    remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((x) => x !== this); },
    addEventListener(t, fn) { (listeners[t] = listeners[t] || []).push(fn); },
    removeEventListener() {},
    _listeners: listeners,
  }, extra || {});
}

function browser(fetchImpl) {
  const drawLog = [];
  const events = [];
  const fetches = [];
  const sandbox = {
    console,
    setTimeout,
    clearTimeout,
    Date,
    Math,
    JSON,
    Promise,
    devicePixelRatio: 1,
    localStorage: { _d: {}, getItem(k) { return this._d[k] || null; }, setItem(k, v) { this._d[k] = String(v); } },
    CustomEvent: function CustomEvent(type, init) { this.type = type; this.detail = init && init.detail; },
    dispatchEvent(e) { events.push(e.detail); return true; },
    addEventListener() {},
    requestAnimationFrame: () => 0,
    cancelAnimationFrame: () => {},
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    fetch: (url, opts) => { fetches.push(url); return fetchImpl(url, opts); },
    document: {
      hidden: false,
      documentElement: {},
      addEventListener() {},
      createElement: (tag) => {
        const e = makeEl(tag);
        if (tag === 'canvas') { e.width = 300; e.height = 150; e.getContext = () => makeCtx(drawLog); }
        return e;
      },
    },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  for (const f of ['view_zoom.js', 'live_overlay.js']) {
    vm.runInContext(fs.readFileSync(path.join(staticDir, 'js', f), 'utf8'), sandbox, { filename: f });
  }
  return { sandbox, drawLog, events, fetches };
}

const tick = (ms) => new Promise((r) => setTimeout(r, ms));
const json = (status, body) => Promise.resolve({ ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body) });

function attachView(b) {
  const stage = makeEl('div', { clientWidth: 640, clientHeight: 480 });
  const media = makeEl('img', { naturalWidth: 1920, naturalHeight: 1080, clientWidth: 640, clientHeight: 480, offsetParent: stage, offsetLeft: 0, offsetTop: 0 });
  const status = makeEl('span');
  const view = b.sandbox.EdgeLiveOverlay.attach({ key: 'cameras', cameraId: 'cam1', stage, media: () => media, isActive: () => true, statusEl: status });
  return { stage, media, status, view };
}

async function main() {
  const out = {};

  // ---- pure helpers
  {
    const b = browser(() => json(404, {}));
    const T = b.sandbox.EdgeLiveOverlay._test;
    out.content = T.contentRect(640, 480, 1920, 1080);
    out.content_unknown = T.contentRect(640, 480, 0, 0);
    out.extrap_capped = T.extrapolationSeconds({ fresh: true, velocity: [0.1, 0] }, 1002, 1000);
    out.extrap_small = T.extrapolationSeconds({ fresh: true, velocity: [0.1, 0] }, 1000.2, 1000);
    out.extrap_coasting = T.extrapolationSeconds({ fresh: false, velocity: [0.1, 0] }, 1000.2, 1000);
    out.extrap_novel = T.extrapolationSeconds({ fresh: true, velocity: null }, 1000.2, 1000);
    out.moved = T.movedBox({ box: [0.1, 0.2, 0.3, 0.4], velocity: [0.2, -0.1] }, 0.5);
    out.levels = [
      T.levelOf({ motion_state: 'static', behaviour: { level: 'alert' } }),
      T.levelOf({ motion_state: 'moving', behaviour: { level: 'alert' } }),
      T.levelOf({ motion_state: 'moving', behaviour: { level: 'watch' } }),
      T.levelOf({ motion_state: 'moving', behaviour: null }),
      T.levelOf({ motion_state: 'pending_static', behaviour: null }),
    ];
    out.chips = [
      T.chipText({ motion_state: 'moving', behaviour: { level: 'alert', labels: ['Reaching: Spirits', 'Hand at chest'] } }),
      T.chipText({ motion_state: 'moving', behaviour: null }),
      T.chipText({ motion_state: 'static' }),
    ];
    out.delays = [T.pollDelayMs([12]), T.pollDelayMs([0.5]), T.pollDelayMs([]), T.pollDelayMs([null, 3])];
    out.limbs = T.LIMBS.length;
    const Z = b.sandbox.EdgeViewZoom._math;
    const z2 = Z.zoomAt({ s: 1, tx: 0, ty: 0 }, 2, 100, 50, 640, 360);
    const p = Z.toStage(z2, 100, 50);
    out.zoom = { z2, fixed: p, back: Z.zoomAt(z2, 0.5, 10, 10, 640, 360), max: Z.zoomAt(z2, 50, 0, 0, 640, 360).s,
      clamp: Z.clampPan(2, 50, -1000, 640, 360) };
  }

  // ---- 404: honest "unavailable", nothing drawn, the server picture keeps its own overlay
  {
    const b = browser(() => json(404, { detail: 'Not Found' }));
    const v = attachView(b);
    await tick(30);
    const LO = b.sandbox.EdgeLiveOverlay;
    out.unavailable = {
      state: LO.status().state,
      clientDraws: LO.clientDraws('cameras'),
      status_text: v.status.textContent,
      status_hidden: v.status.hidden,
      canvas_hidden: v.view.canvas.hidden,
      drew_boxes: b.drawLog.filter((c) => c[0] === 'strokeRect').length,
      fetched: b.fetches,
    };
  }

  // ---- live tracks: box, skeleton (visible joints only), behaviour chip; unconfirmed skipped
  {
    const kp = Array.from({ length: 17 }, (_, i) => [0.4 + i * 0.005, 0.2 + i * 0.03, (i === 9 || i === 10) ? 0.1 : 0.9]);
    const body = {
      server_time: Date.now() / 1000,
      cameras: {
        cam1: {
          seq: 7, analysed_at: Date.now() / 1000, frame_width: 1920, frame_height: 1080, analysis_fps: 4.8,
          tracks: [
            { track_id: 'trk_1', confirmed: true, motion_state: 'moving', box: [0.3, 0.1, 0.5, 0.9], velocity: [0, 0],
              fresh: true, age_sec: 0, keypoints: kp,
              behaviour: { level: 'alert', labels: ['Reaching: Spirits', 'Hand at chest'], head_turns: 0, pattern_score: null, pattern_threshold: 1 } },
            { track_id: 'trk_2', confirmed: false, motion_state: 'pending', box: [0.6, 0.1, 0.7, 0.5], velocity: null,
              fresh: true, age_sec: 0, keypoints: null, behaviour: null },
          ],
        },
      },
    };
    const b = browser(() => json(200, body));
    const v = attachView(b);
    await tick(30);
    const LO = b.sandbox.EdgeLiveOverlay;
    out.live = {
      state: LO.status().state,
      clientDraws: LO.clientDraws('cameras'),
      canvas_hidden: v.view.canvas.hidden,
      canvas_style: v.view.canvas.style,
      showing: v.view.isShowing(),
      strokeRects: b.drawLog.filter((c) => c[0] === 'strokeRect').length,
      limbs: b.drawLog.filter((c) => c[0] === 'lineTo').length,
      joints: b.drawLog.filter((c) => c[0] === 'arc').length,
      texts: b.drawLog.filter((c) => c[0] === 'fillText').map((c) => c[1]),
      status_text: v.status.textContent,
      fetched: b.fetches,
    };
    // Turning the overlay off: nothing drawn, the server picture is asked for clean.
    LO.setEnabled('cameras', false);
    out.off = { clientDraws: LO.clientDraws('cameras'), canvas_hidden: v.view.canvas.hidden, stored: b.sandbox.localStorage._d['edge.aiOverlay.v1'] };
  }

  // ---- a camera the box does not report: no data, said so
  {
    const b = browser(() => json(200, { server_time: Date.now() / 1000, cameras: {} }));
    const v = attachView(b);
    await tick(30);
    out.missing = { status_text: v.status.textContent, canvas_hidden: v.view.canvas.hidden };
  }

  console.log(JSON.stringify(out));
  process.exit(0);
}

main().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
