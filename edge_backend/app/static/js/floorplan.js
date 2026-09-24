/**
 * Store blueprint editor and live person map.
 *
 * Nothing here is invented. Geometry (rooms, walls, fixtures, doors, zones,
 * camera placements) is loaded from and saved to /api/v1/layout in real-world
 * metres, origin top-left, x right, y down. People come from
 * /api/v1/layout/live, which only reports confirmed tracks from calibrated
 * cameras; density comes from recorded observations. When the server has
 * nothing to show, the view says so and animates nothing.
 *
 * Coordinate systems, kept deliberately separate:
 *   world  - metres, matching the database polygons
 *   screen - canvas CSS pixels, produced by scale + pan
 *
 * Public surface (window.blueprintEditor):
 *   resize(), resetView(), zoom(f), toggleLayer(name, on), toggleSnap(on)
 *   setHeatmapKind('presence'|'dwell'|'interaction')
 *   setTool('view'), startDraw(kind), startDrawZone(), finishPolygon(),
 *   undoPoint(), cancelDraw(), resizeStore(), startPlaceCamera(),
 *   selectCamera(id), selectShape(type, id), load(),
 *   beginPick(cb), cancelPick(), setCalOverlay(overlay|null)
 */
(function () {
  'use strict';

  const API = '/api/v1/layout';

  const CATEGORY_COLORS = {
    ENTRANCE: '#00ff9d', EXIT: '#ffa500', AISLE: '#00d4ff', DEPARTMENT: '#a78bfa',
    SHELF: '#f472b6', CHECKOUT: '#fbbf24', STOCKROOM: '#94a3b8', EXCLUDED: '#475569',
  };

  // Structure kinds are the backend whitelist. WALL and DOOR are polylines.
  const STRUCT_KINDS = ['ROOM', 'WALL', 'SHELF', 'COUNTER', 'DOOR', 'OBSTACLE'];
  const POLYLINE_KINDS = new Set(['WALL', 'DOOR']);
  const KIND_COLORS = {
    ROOM: '#4a90d9', WALL: '#c9d4e0', SHELF: '#f472b6',
    COUNTER: '#fb923c', DOOR: '#fbbf24', OBSTACLE: '#94a3b8',
  };
  const KIND_LABEL = {
    ROOM: 'Room', WALL: 'Wall', SHELF: 'Shelf', COUNTER: 'Counter', DOOR: 'Door', OBSTACLE: 'Obstacle',
  };
  const minPointsFor = (kind) => (POLYLINE_KINDS.has(kind) ? 2 : 3);

  // One colour per camera so people can be traced back to the camera that
  // placed them. Assigned by camera order, stable while the list is stable.
  const CAM_PALETTE = ['#00d4ff', '#00ff9d', '#a78bfa', '#fbbf24', '#f472b6', '#fb923c', '#34d399', '#60a5fa'];

  const GRID_SNAP_M = 0.5;
  const TRAIL_LEN = 10;

  // Canvas colours come from the theme tokens in style.css (--plan-*), read
  // at draw time. They are re-read on 'edge:theme' and the render loop picks
  // them up on its next frame. The category / kind / camera palettes above are
  // data colours; ink() darkens them by --plan-ink-mix so they stay legible
  // on the light plan (0% in the dark theme, i.e. unchanged).
  let T = readPlanTheme();
  window.addEventListener('edge:theme', () => { T = readPlanTheme(); });

  function readPlanTheme() {
    let cs = null;
    try { cs = getComputedStyle(document.documentElement); } catch (_) { /* no CSSOM */ }
    const tok = (name) => (cs ? cs.getPropertyValue(name).trim() : '');
    return {
      bg: tok('--plan-bg'), inner: tok('--plan-inner'), grid: tok('--plan-grid-rgb'),
      text: tok('--plan-text'), text2: tok('--plan-text-2'), muted: tok('--plan-muted'),
      placeholder: tok('--plan-placeholder'), outline: tok('--plan-outline'), marker: tok('--plan-marker'),
      select: tok('--plan-select'), warn: tok('--plan-warn'), danger: tok('--plan-danger'),
      origin: tok('--plan-origin'), heatBlend: tok('--plan-heat-blend') || 'lighter',
      inkMix: (parseFloat(tok('--plan-ink-mix')) || 0) / 100,
    };
  }

  /** Darken a #rgb/#rrggbb data colour toward black by the theme's ink mix. */
  function ink(color) {
    if (!T.inkMix || typeof color !== 'string' || color[0] !== '#') return color;
    const h = color.slice(1);
    const n = parseInt(h.length === 3 ? h.split('').map((c) => c + c).join('') : h, 16);
    if (!isFinite(n)) return color;
    const k = 1 - T.inkMix;
    const c = (v) => Math.round(v * k).toString(16).padStart(2, '0');
    return `#${c((n >> 16) & 255)}${c((n >> 8) & 255)}${c(n & 255)}`;
  }
  const TRACK_TTL_MS = 3000;
  const LIVE_POLL_MS = 1000;
  const METRICS_POLL_MS = 4000;
  const HIT_PX = 7;

  const MODE = { VIEW: 'view', DRAW: 'draw', PLACE_CAMERA: 'place-camera', PICK: 'pick-point' };

  // Heatmap kinds served by GET /api/v1/analytics/heatmaps?kind=...
  const HEATMAP_KINDS = [
    { kind: 'presence', label: 'Presence', title: 'Where people were (tracked floor positions)' },
    { kind: 'dwell', label: 'Dwell', title: 'Seconds spent per cell' },
    { kind: 'interaction', label: 'Interactions', title: 'Shelf reaches, at the shopper\'s floor position' },
  ];
  const HEATMAP_KIND_KEY = 'edge_cctv_heatmap_kind';

  class BlueprintEditor {
    constructor(canvas) {
      this.canvas = canvas;
      this.ctx = canvas.getContext('2d');

      this.layout = null;
      this.zones = [];
      this.structures = [];
      this.cameras = [];
      this.setup = null;            // {configured, zones, structures, cameras, cameras_calibrated}
      this.structuresSupported = null;   // null = unknown until the first load
      this.liveSupported = null;

      this.zoneMetrics = {};
      this.heatmap = null;
      this.heatmapMessage = null;
      this.heatmapKind = 'presence';
      try {
        const saved = window.localStorage.getItem(HEATMAP_KIND_KEY);
        if (HEATMAP_KINDS.some((k) => k.kind === saved)) this.heatmapKind = saved;
      } catch (_) { /* storage unavailable: default kind */ }
      this.coverage = null;
      this.activeNow = null;

      this.tracks = new Map();      // "camera:track" -> {points:[{x,y,t}], lastSeen, ...}
      this.detections = [];         // per-camera live snapshot
      this.liveRunning = false;
      this.uncalibratedTracks = 0;
      this.lastLiveAt = 0;

      this.scale = 20;
      this.offsetX = 0;
      this.offsetY = 0;

      this.mode = MODE.VIEW;
      this.draftKind = null;        // 'ZONE' or a structure kind while drawing
      this.draft = [];
      this.sel = null;              // {type:'zone'|'structure'|'camera', id}
      this.selVertex = null;        // vertex index on the selected shape
      this.hover = null;
      this.dragging = null;
      this.cursorWorld = null;
      this.shiftDown = false;
      this.snapEnabled = true;
      this.pickCallback = null;
      this.calOverlay = null;       // {cameraId, pairs:[{floor:{x,y}}], reprojected:[{x,y}|null]}

      this.showLayers = {
        structures: true, zones: true, cameras: true, heatmap: true,
        persons: true, labels: true, grid: true,
      };

      this._thumbTimer = null;
      this._statusTimer = null;

      this._bindEvents();
      this._mountHeatmapKindToggle();
      this.resize();
      this.load();

      this._liveTimer = setInterval(() => this.refreshLive(), LIVE_POLL_MS);
      this._metricsTimer = setInterval(() => this.refreshMetrics(), METRICS_POLL_MS);
      this._raf = requestAnimationFrame(() => this.render());
    }

    // ------------------------------------------------------------- transforms

    toScreen(xm, ym) {
      return { x: xm * this.scale + this.offsetX, y: ym * this.scale + this.offsetY };
    }

    toWorld(px, py) {
      return { x: (px - this.offsetX) / this.scale, y: (py - this.offsetY) / this.scale };
    }

    worldFromClient(clientX, clientY) {
      const r = this.canvas.getBoundingClientRect();
      return this.toWorld(clientX - r.left, clientY - r.top);
    }

    fitToView() {
      if (!this.layout) return;
      const pad = 48;
      const W = this.canvas.clientWidth, H = this.canvas.clientHeight;
      if (W < 10 || H < 10) return;      // hidden tab: measure again when shown
      const w = W - pad * 2, h = H - pad * 2 - 40;
      this.scale = Math.max(2, Math.min(w / this.layout.width_m, h / this.layout.height_m));
      this.offsetX = (W - this.layout.width_m * this.scale) / 2;
      this.offsetY = (H - this.layout.height_m * this.scale) / 2 + 20;
    }

    resize() {
      const dpr = window.devicePixelRatio || 1;
      const rect = this.canvas.getBoundingClientRect();
      if (rect.width < 1 || rect.height < 1) return;
      this.canvas.width = Math.round(rect.width * dpr);
      this.canvas.height = Math.round(rect.height * dpr);
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      if (this.layout && !this._userZoomed) this.fitToView();
    }

    isVisible() {
      if (document.hidden) return false;
      const tab = document.getElementById('tab-floorplan');
      return !tab || tab.classList.contains('active');
    }

    // ------------------------------------------------------------------ data

    async load() {
      if (window.edgeAuth && typeof window.edgeAuth.isAuthenticated === 'function' && !window.edgeAuth.isAuthenticated()) {
        return;
      }
      try {
        const res = await fetch(API);
        if (!res.ok) throw new Error(`layout request failed: ${res.status}`);
        const data = await res.json();
        this.applyLayout(data);
        this.fitToView();
        this.emitState();
        this.renderSetup();
        this.refreshMetrics();
        this.refreshLive();
        if (window.deviceManager && window.deviceManager.renderCameras) {
          window.deviceManager.cameras = this.cameras;
          window.deviceManager.renderCameras();
        }
      } catch (e) {
        this.setStatus(`Could not load blueprint: ${e.message}`, 'error');
      }
    }

    applyLayout(data) {
      this.layout = data;
      this.zones = data.zones || [];
      this.cameras = data.cameras || [];
      this.structuresSupported = Array.isArray(data.structures);
      this.structures = this.structuresSupported ? data.structures : [];
      this.setup = data.setup || {
        configured: this.zones.length > 0 || this.structures.length > 0 || this.cameras.length > 0,
        zones: this.zones.length, structures: this.structures.length,
        cameras: this.cameras.length,
        cameras_calibrated: this.cameras.filter((c) => c.has_homography).length,
        _client_side: true,
      };
      // Drop a selection that no longer exists.
      if (this.sel && !this.findSel()) this.select(null);
    }

    recomputeSetup() {
      // Kept truthful after local edits without another round trip.
      const configured = this.zones.length > 0 || this.structures.length > 0 || this.cameras.length > 0;
      this.setup = Object.assign({}, this.setup, {
        configured, zones: this.zones.length, structures: this.structures.length,
        cameras: this.cameras.length,
        cameras_calibrated: this.cameras.filter((c) => c.has_homography).length,
      });
      this.renderSetup();
      this.emitState();
    }

    async refreshMetrics() {
      if (!this.isVisible() || !this.layout) return;
      if (window.edgeAuth && typeof window.edgeAuth.isAuthenticated === 'function' && !window.edgeAuth.isAuthenticated()) return;
      const kind = this.heatmapKind;
      try {
        const [fpRes, hmRes] = await Promise.all([
          fetch('/api/v1/analytics/floorplan'),
          fetch(`/api/v1/analytics/heatmaps?resolution_w=60&resolution_h=40&kind=${encodeURIComponent(kind)}`),
        ]);
        if (fpRes.ok) {
          const fp = await fpRes.json();
          this.coverage = fp.coverage || null;
          this.zoneMetrics = {};
          (fp.zones || []).forEach((z) => { this.zoneMetrics[z.id] = z.metrics; });
          this.activeNow = (fp.active_shoppers_now === undefined) ? null : fp.active_shoppers_now;
        }
        if (hmRes.ok && kind === this.heatmapKind) {
          const hm = await hmRes.json();
          this.heatmap = hm.observed ? hm : null;
          this.heatmapMessage = hm.observed ? null : hm.message;
          this._renderHeatmapNote(hm);
        }
        this.emitState();
      } catch (_) { /* transient; next tick retries */ }
    }

    /**
     * Poll the live person map. Only confirmed tracks from calibrated cameras
     * arrive as persons; everything else is reported as a count so the map
     * never shows a dot the pipeline did not place.
     */
    async refreshLive() {
      if (!this.isVisible() || this.liveSupported === false && Date.now() - this.lastLiveAt < 15000) return;
      if (window.edgeAuth && typeof window.edgeAuth.isAuthenticated === 'function' && !window.edgeAuth.isAuthenticated()) return;
      try {
        const res = await fetch(`${API}/live`);
        if (res.status === 404) {
          this.liveSupported = false;
          this.lastLiveAt = Date.now();
          this.tracks.clear();
          this.detections = [];
          this.renderLiveDom();
          return;
        }
        if (!res.ok) return;
        const snap = await res.json();
        this.liveSupported = true;
        this.lastLiveAt = Date.now();
        this.liveRunning = !!snap.running;
        this.detections = snap.detections || [];
        this.uncalibratedTracks = snap.uncalibrated_track_count || 0;

        const now = Date.now();
        (snap.persons || []).forEach((p) => {
          if (typeof p.x_m !== 'number' || typeof p.y_m !== 'number') return;
          const key = `${p.camera_id}:${p.track_id}`;
          let t = this.tracks.get(key);
          if (!t) {
            t = { key, track_id: p.track_id, camera_id: p.camera_id, camera_name: p.camera_name, points: [] };
            this.tracks.set(key, t);
          }
          const last = t.points[t.points.length - 1];
          if (!last || last.x !== p.x_m || last.y !== p.y_m) {
            t.points.push({ x: p.x_m, y: p.y_m, t: now });
            if (t.points.length > TRAIL_LEN) t.points.shift();
          }
          t.lastSeen = now;
          t.zone_id = p.zone_id;
          t.confidence = p.confidence;
        });
        this.expireTracks();
        this.renderLiveDom();
      } catch (_) { /* transient */ }
    }

    expireTracks() {
      const cutoff = Date.now() - TRACK_TTL_MS;
      for (const [k, t] of this.tracks) if (t.lastSeen < cutoff) this.tracks.delete(k);
    }

    // ----------------------------------------------------------------- events

    _bindEvents() {
      const c = this.canvas;
      c.addEventListener('mousedown', (e) => this.onDown(e));
      c.addEventListener('mousemove', (e) => this.onMove(e));
      window.addEventListener('mouseup', (e) => this.onUp(e));
      c.addEventListener('wheel', (e) => this.onWheel(e), { passive: false });
      c.addEventListener('dblclick', (e) => this.onDoubleClick(e));
      c.addEventListener('contextmenu', (e) => { e.preventDefault(); if (this.mode === MODE.DRAW) this.finishPolygon(); });
      window.addEventListener('resize', () => this.resize());
      window.addEventListener('keydown', (e) => this.onKey(e));
      window.addEventListener('keyup', (e) => { if (e.key === 'Shift') this.shiftDown = false; });
      document.addEventListener('visibilitychange', () => { if (this.isVisible()) this.refreshLive(); });
      if (window.ResizeObserver) {
        const wrap = c.parentElement || c;
        new ResizeObserver(() => this.resize()).observe(wrap);
      }
    }

    localPoint(e) {
      const r = this.canvas.getBoundingClientRect();
      return { x: e.clientX - r.left, y: e.clientY - r.top };
    }

    isTyping(e) {
      const tag = (e.target && e.target.tagName || '').toLowerCase();
      return tag === 'input' || tag === 'textarea' || tag === 'select' || (e.target && e.target.isContentEditable);
    }

    onKey(e) {
      if (e.key === 'Shift') this.shiftDown = true;
      if (this.isTyping(e)) return;
      const k = e.key;
      if (k === 'Escape') {
        if (this.mode === MODE.PICK) { this.cancelPick(); return; }
        if (this.mode !== MODE.VIEW) { this.cancelDraw(); return; }
        this.select(null);
        return;
      }
      if (k === 'Enter' && this.mode === MODE.DRAW) { e.preventDefault(); this.finishPolygon(); return; }
      if ((k === 'Backspace') && this.mode === MODE.DRAW) { e.preventDefault(); this.undoPoint(); return; }
      if ((k === 'Delete' || k === 'Backspace') && this.mode === MODE.VIEW && this.sel) {
        e.preventDefault();
        if (this.selVertex !== null) this.deleteSelectedVertex();
        else if (this.sel.type !== 'camera') this.deleteShape(this.sel.type, this.sel.id);
        return;
      }
      if (this.mode !== MODE.VIEW && this.mode !== MODE.DRAW) return;
      const shortcuts = { v: 'view', r: 'ROOM', w: 'WALL', s: 'SHELF', d: 'DOOR', z: 'ZONE' };
      const t = shortcuts[k.toLowerCase()];
      if (t && !e.ctrlKey && !e.metaKey && !e.altKey) {
        if (t === 'view') this.setTool('view');
        else if (t === 'ZONE') this.startDrawZone();
        else this.startDraw(t);
      }
    }

    onDown(e) {
      if (e.button !== 0) return;
      const p = this.localPoint(e);
      const w = this.toWorld(p.x, p.y);
      this.shiftDown = e.shiftKey;

      if (this.mode === MODE.DRAW) { this.addDraftPoint(w); return; }
      if (this.mode === MODE.PLACE_CAMERA) { this.placeCameraAt(w.x, w.y); return; }
      if (this.mode === MODE.PICK) {
        const cb = this.pickCallback;
        this.pickCallback = null;
        this.setMode(MODE.VIEW);
        if (cb) cb({ x: +this.clampX(w.x).toFixed(3), y: +this.clampY(w.y).toFixed(3) });
        return;
      }

      // Selected camera: the rotate handle has priority over everything.
      const handle = this.cameraHandleAt(p.x, p.y);
      if (handle) {
        this.dragging = { kind: 'camera-rotate', id: handle.camera_id };
        return;
      }
      // Selected shape: vertices first, so a corner can always be grabbed.
      const vi = this.vertexAt(p.x, p.y);
      if (vi !== null) {
        this.selVertex = vi;
        const shape = this.findSel();
        this.dragging = { kind: 'vertex', type: this.sel.type, id: this.sel.id, index: vi, moved: false, orig: shape.polygon.map((q) => ({ x: q.x, y: q.y })) };
        this.emitInspector();
        return;
      }

      const cam = this.showLayers.cameras ? this.cameraAt(p.x, p.y) : null;
      if (cam) {
        this.select({ type: 'camera', id: cam.camera_id });
        this.dragging = { kind: 'camera', id: cam.camera_id, moved: false };
        return;
      }

      const hit = this.shapeAt(p.x, p.y, w);
      if (hit) {
        if (!this.sel || this.sel.type !== hit.type || this.sel.id !== hit.id) this.select(hit);
        const shape = this.findSel();
        this.dragging = {
          kind: 'shape', type: hit.type, id: hit.id, start: w, moved: false,
          orig: shape.polygon.map((q) => ({ x: q.x, y: q.y })),
        };
        return;
      }

      this.select(null);
      this.dragging = { kind: 'pan', startX: p.x - this.offsetX, startY: p.y - this.offsetY };
    }

    onMove(e) {
      const p = this.localPoint(e);
      const w = this.toWorld(p.x, p.y);
      this.cursorWorld = w;
      this.shiftDown = e.shiftKey;

      if (this.dragging) {
        const d = this.dragging;
        if (d.kind === 'pan') {
          this.offsetX = p.x - d.startX;
          this.offsetY = p.y - d.startY;
          this._userZoomed = true;
        } else if (d.kind === 'camera') {
          const cam = this.cameras.find((c) => c.camera_id === d.id);
          if (cam) { cam.floor_x = +this.clampX(w.x).toFixed(2); cam.floor_y = +this.clampY(w.y).toFixed(2); d.moved = true; }
        } else if (d.kind === 'camera-rotate') {
          const cam = this.cameras.find((c) => c.camera_id === d.id);
          if (cam) {
            const s = this.toScreen(cam.floor_x, cam.floor_y);
            let deg = Math.atan2(p.y - s.y, p.x - s.x) * 180 / Math.PI + 90;
            deg = ((Math.round(deg) % 360) + 360) % 360;
            if (this.shiftDown) deg = (Math.round(deg / 15) * 15) % 360;
            cam.azimuth_deg = deg;
            d.moved = true;
          }
        } else if (d.kind === 'vertex') {
          const shape = this.findSel();
          if (shape) {
            const pt = this.snap({ x: this.clampX(w.x), y: this.clampY(w.y) });
            shape.polygon[d.index] = pt;
            d.moved = true;
          }
        } else if (d.kind === 'shape') {
          const shape = this.findSel();
          if (shape) {
            let dx = w.x - d.start.x, dy = w.y - d.start.y;
            if (this.snapEnabled) { dx = Math.round(dx / GRID_SNAP_M) * GRID_SNAP_M; dy = Math.round(dy / GRID_SNAP_M) * GRID_SNAP_M; }
            // Keep the whole shape inside the store: limit the delta, not each point.
            const xs = d.orig.map((q) => q.x), ys = d.orig.map((q) => q.y);
            dx = Math.max(-Math.min(...xs), Math.min(this.layout.width_m - Math.max(...xs), dx));
            dy = Math.max(-Math.min(...ys), Math.min(this.layout.height_m - Math.max(...ys), dy));
            shape.polygon = d.orig.map((q) => ({ x: +(q.x + dx).toFixed(3), y: +(q.y + dy).toFixed(3) }));
            d.moved = d.moved || dx !== 0 || dy !== 0;
          }
        }
        return;
      }

      // Hover feedback and cursor.
      let cursor = 'grab';
      if (this.mode === MODE.DRAW || this.mode === MODE.PICK) cursor = 'crosshair';
      else if (this.mode === MODE.PLACE_CAMERA) cursor = 'copy';
      else if (this.cameraHandleAt(p.x, p.y)) cursor = 'alias';
      else if (this.vertexAt(p.x, p.y) !== null) cursor = 'move';
      else {
        const cam = this.showLayers.cameras ? this.cameraAt(p.x, p.y) : null;
        const hit = cam ? null : this.shapeAt(p.x, p.y, w);
        this.hover = cam ? { type: 'camera', id: cam.camera_id } : hit;
        if (cam || hit) cursor = 'pointer';
      }
      this.canvas.style.cursor = cursor;
    }

    onUp() {
      if (!this.dragging) return;
      const d = this.dragging;
      this.dragging = null;
      if (d.kind === 'camera' || d.kind === 'camera-rotate') {
        const cam = this.cameras.find((c) => c.camera_id === d.id);
        if (cam && d.moved) { this.saveCameraPlacement(cam); this.emitInspector(); }
      } else if ((d.kind === 'shape' || d.kind === 'vertex') && d.moved) {
        const shape = this.findSel();
        if (shape) this.saveShape(d.type, shape, d.orig);
        this.emitInspector();
      }
    }

    onWheel(e) {
      e.preventDefault();
      const p = this.localPoint(e);
      const before = this.toWorld(p.x, p.y);
      const factor = e.deltaY < 0 ? 1.12 : 0.89;
      this.scale = Math.max(2, Math.min(400, this.scale * factor));
      const after = this.toWorld(p.x, p.y);
      this.offsetX += (after.x - before.x) * this.scale;
      this.offsetY += (after.y - before.y) * this.scale;
      this._userZoomed = true;
    }

    onDoubleClick(e) {
      const p = this.localPoint(e);
      if (this.mode === MODE.DRAW) { this.finishPolygon(); return; }
      if (this.mode !== MODE.VIEW) return;
      // Double-click on an edge of the selected shape inserts a vertex there.
      const edge = this.edgeAt(p.x, p.y);
      if (edge) {
        const shape = this.findSel();
        const orig = shape.polygon.map((q) => ({ x: q.x, y: q.y }));
        shape.polygon.splice(edge.index + 1, 0, this.snap(edge.point));
        this.selVertex = edge.index + 1;
        this.saveShape(this.sel.type, shape, orig);
        this.emitInspector();
        return;
      }
      const cam = this.cameraAt(p.x, p.y);
      if (cam) this.select({ type: 'camera', id: cam.camera_id });
    }

    // ------------------------------------------------------------- hit tests

    clampX(x) { return Math.max(0, Math.min(this.layout ? this.layout.width_m : x, x)); }
    clampY(y) { return Math.max(0, Math.min(this.layout ? this.layout.height_m : y, y)); }

    snap(pt) {
      if (!this.snapEnabled) return { x: +pt.x.toFixed(2), y: +pt.y.toFixed(2) };
      return {
        x: +(Math.round(pt.x / GRID_SNAP_M) * GRID_SNAP_M).toFixed(2),
        y: +(Math.round(pt.y / GRID_SNAP_M) * GRID_SNAP_M).toFixed(2),
      };
    }

    cameraReachPx() { return Math.max(30, Math.min(160, 6 * this.scale)); }

    cameraAt(px, py) {
      for (let i = this.cameras.length - 1; i >= 0; i--) {
        const c = this.cameras[i];
        const s = this.toScreen(c.floor_x, c.floor_y);
        if (Math.hypot(s.x - px, s.y - py) <= 14) return c;
      }
      return null;
    }

    cameraHandlePos(cam) {
      const s = this.toScreen(cam.floor_x, cam.floor_y);
      const a = ((cam.azimuth_deg || 0) - 90) * Math.PI / 180;
      const r = this.cameraReachPx();
      return { x: s.x + Math.cos(a) * r, y: s.y + Math.sin(a) * r };
    }

    cameraHandleAt(px, py) {
      if (!this.sel || this.sel.type !== 'camera' || !this.showLayers.cameras) return null;
      const cam = this.cameras.find((c) => c.camera_id === this.sel.id);
      if (!cam) return null;
      const h = this.cameraHandlePos(cam);
      return Math.hypot(h.x - px, h.y - py) <= 10 ? cam : null;
    }

    findSel() {
      if (!this.sel) return null;
      if (this.sel.type === 'zone') return this.zones.find((z) => z.id === this.sel.id) || null;
      if (this.sel.type === 'structure') return this.structures.find((s) => s.id === this.sel.id) || null;
      if (this.sel.type === 'camera') return this.cameras.find((c) => c.camera_id === this.sel.id) || null;
      return null;
    }

    vertexAt(px, py) {
      if (!this.sel || this.sel.type === 'camera') return null;
      const shape = this.findSel();
      if (!shape) return null;
      for (let i = 0; i < shape.polygon.length; i++) {
        const s = this.toScreen(shape.polygon[i].x, shape.polygon[i].y);
        if (Math.hypot(s.x - px, s.y - py) <= HIT_PX) return i;
      }
      return null;
    }

    edgeAt(px, py) {
      if (!this.sel || this.sel.type === 'camera') return null;
      const shape = this.findSel();
      if (!shape) return null;
      const poly = shape.polygon;
      const closed = !(this.sel.type === 'structure' && POLYLINE_KINDS.has(shape.kind));
      const n = closed ? poly.length : poly.length - 1;
      for (let i = 0; i < n; i++) {
        const a = this.toScreen(poly[i].x, poly[i].y);
        const b = this.toScreen(poly[(i + 1) % poly.length].x, poly[(i + 1) % poly.length].y);
        const r = segDist(px, py, a.x, a.y, b.x, b.y);
        if (r.d <= HIT_PX) return { index: i, point: this.toWorld(r.x, r.y) };
      }
      return null;
    }

    /** Smallest hit shape wins, so a shelf inside a room is still selectable. */
    shapeAt(px, py, w) {
      const hits = [];
      if (this.showLayers.zones) {
        this.zones.forEach((z) => {
          if (pointInPolygon(w.x, w.y, z.polygon || [])) hits.push({ type: 'zone', id: z.id, area: polygonArea(z.polygon) });
        });
      }
      if (this.showLayers.structures) {
        this.structures.forEach((s) => {
          const poly = s.polygon || [];
          if (POLYLINE_KINDS.has(s.kind)) {
            const tol = Math.max(HIT_PX, ((s.thickness_m || 0.2) * this.scale) / 2 + 2);
            for (let i = 0; i < poly.length - 1; i++) {
              const a = this.toScreen(poly[i].x, poly[i].y), b = this.toScreen(poly[i + 1].x, poly[i + 1].y);
              if (segDist(px, py, a.x, a.y, b.x, b.y).d <= tol) { hits.push({ type: 'structure', id: s.id, area: -1 }); break; }
            }
          } else if (pointInPolygon(w.x, w.y, poly)) {
            hits.push({ type: 'structure', id: s.id, area: polygonArea(poly) });
          }
        });
      }
      if (!hits.length) return null;
      hits.sort((a, b) => a.area - b.area);
      return { type: hits[0].type, id: hits[0].id };
    }

    // --------------------------------------------------------------- drawing

    setMode(mode) {
      this.mode = mode;
      if (mode !== MODE.DRAW) { this.draft = []; this.draftKind = null; }
      this.updateDrawBanner();
      this.updateToolButtons();
      this.emitState();
    }

    setTool(tool) {
      if (tool === 'view') { this.cancelDraw(); return; }
      if (tool === 'ZONE') this.startDrawZone(); else this.startDraw(tool);
    }

    startDraw(kind) {
      if (!STRUCT_KINDS.includes(kind)) return;
      if (this.structuresSupported === false) {
        this.setStatus('This server does not expose /api/v1/layout/structures yet, so rooms and walls cannot be saved.', 'error');
        return;
      }
      this.select(null);
      this.draftKind = kind;
      this.draft = [];
      this.setMode(MODE.DRAW);
      this.setStatus(POLYLINE_KINDS.has(kind)
        ? `Drawing a ${KIND_LABEL[kind].toLowerCase()}: click along its path. Hold Shift for straight segments. Enter or double-click to finish.`
        : `Drawing a ${KIND_LABEL[kind].toLowerCase()}: click each corner. Enter or double-click to finish, Escape to cancel.`);
    }

    startDrawZone() {
      this.select(null);
      this.draftKind = 'ZONE';
      this.draft = [];
      this.setMode(MODE.DRAW);
      this.setStatus('Drawing a zone: click each corner. Enter or double-click to finish, Escape to cancel.');
    }

    startPlaceCamera() {
      this.setMode(MODE.PLACE_CAMERA);
      this.setStatus('Click the plan where this camera is mounted.');
    }

    cancelDraw() {
      this.draft = [];
      this.draftKind = null;
      if (this.mode === MODE.PLACE_CAMERA) window.__pendingCameraPlacement = null;
      this.setMode(MODE.VIEW);
    }

    undoPoint() {
      this.draft.pop();
      this.updateDrawBanner();
    }

    addDraftPoint(w) {
      let pt = { x: this.clampX(w.x), y: this.clampY(w.y) };
      const prev = this.draft[this.draft.length - 1];
      if (prev && this.shiftDown && POLYLINE_KINDS.has(this.draftKind)) pt = orthoTo(prev, pt);
      pt = this.snap(pt);
      // The second mousedown of a double-click lands on the same spot; ignore
      // exact repeats so a double-click finish never leaves a zero-length edge.
      if (prev && Math.abs(prev.x - pt.x) < 1e-6 && Math.abs(prev.y - pt.y) < 1e-6) return;
      this.draft.push(pt);
      this.updateDrawBanner();
    }

    draftPreviewPoint() {
      if (!this.cursorWorld) return null;
      let pt = { x: this.clampX(this.cursorWorld.x), y: this.clampY(this.cursorWorld.y) };
      const prev = this.draft[this.draft.length - 1];
      if (prev && this.shiftDown && POLYLINE_KINDS.has(this.draftKind)) pt = orthoTo(prev, pt);
      return this.snap(pt);
    }

    updateDrawBanner() {
      const banner = document.getElementById('fpDrawBanner');
      if (!banner) return;
      const drawing = this.mode === MODE.DRAW;
      banner.style.display = drawing ? 'flex' : 'none';
      if (!drawing) return;
      const count = document.getElementById('fpDrawCount');
      const hint = document.getElementById('fpDrawHint');
      const finish = document.getElementById('fpDrawFinish');
      const need = this.draftKind === 'ZONE' ? 3 : minPointsFor(this.draftKind);
      const label = this.draftKind === 'ZONE' ? 'zone' : KIND_LABEL[this.draftKind].toLowerCase();
      if (count) count.textContent = this.draft.length;
      if (finish) { finish.textContent = `Finish ${label}`; finish.disabled = this.draft.length < need; }
      if (hint) {
        hint.textContent = this.draft.length < need
          ? `${cap(label)}: click on the plan to add points — at least ${need}.${POLYLINE_KINDS.has(this.draftKind) ? ' Shift = straight.' : ''}`
          : `Add more points, or finish (Enter / double-click) to save this ${label}.`;
      }
    }

    updateToolButtons() {
      const active = this.mode === MODE.DRAW ? this.draftKind : (this.mode === MODE.VIEW ? 'view' : null);
      document.querySelectorAll('#fpTools .fp-tool').forEach((b) => {
        b.classList.toggle('active', b.dataset.tool === active);
      });
    }

    /**
     * Close the draft and persist it immediately. The shape is created with a
     * provisional name so it is on the plan straight away; the inspector then
     * edits it in place.
     */
    async finishPolygon() {
      if (this.mode !== MODE.DRAW) return;
      const kind = this.draftKind;
      const need = kind === 'ZONE' ? 3 : minPointsFor(kind);
      if (this.draft.length < need) {
        this.setStatus(`A ${kind === 'ZONE' ? 'zone' : KIND_LABEL[kind].toLowerCase()} needs at least ${need} points.`, 'error');
        return;
      }
      const polygon = this.draft.slice();
      this.draft = [];
      this.setMode(MODE.VIEW);

      try {
        if (kind === 'ZONE') {
          const zone = await this.post(`${API}/zones`, {
            name: `Zone ${this.zones.length + 1}`, category: 'AISLE', polygon, color: CATEGORY_COLORS.AISLE,
          });
          this.zones.push(zone);
          this.select({ type: 'zone', id: zone.id });
          this.setStatus(`Zone created (${zone.area_m2} m²). Name it in the inspector.`, 'ok');
        } else {
          const count = this.structures.filter((s) => s.kind === kind).length;
          const body = { kind, name: `${KIND_LABEL[kind]} ${count + 1}`, polygon, color: KIND_COLORS[kind] };
          if (POLYLINE_KINDS.has(kind)) body.thickness_m = kind === 'DOOR' ? 0.1 : 0.2;
          const st = await this.post(`${API}/structures`, body);
          this.structures.push(st);
          this.select({ type: 'structure', id: st.id });
          const size = POLYLINE_KINDS.has(kind) ? `${fmt(st.length_m)} m long` : `${fmt(st.area_m2)} m²`;
          this.setStatus(`${KIND_LABEL[kind]} saved (${size}).`, 'ok');
        }
        this.recomputeSetup();
      } catch (e) {
        this.setStatus(`Could not save: ${e.message}`, 'error');
      }
    }

    async post(url, body, method = 'POST') {
      const res = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      if (res.status === 404 && url.includes('/structures')) {
        this.structuresSupported = false;
        throw new Error('the structures API is not available on this server');
      }
      if (!res.ok) {
        let detail = res.statusText;
        try { detail = (await res.json()).detail || detail; } catch (_) { /* not json */ }
        throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
      }
      return res.json();
    }

    // ---------------------------------------------------------- persistence

    async saveShape(type, shape, orig) {
      try {
        if (type === 'zone') {
          const z = await this.post(`${API}/zones/${shape.id}`, {
            polygon: shape.polygon, name: shape.name, category: shape.category, color: shape.color,
          }, 'PUT');
          Object.assign(shape, z);
          this.setStatus(`Zone "${shape.name}" saved.`, 'ok');
        } else {
          const body = { polygon: shape.polygon, name: shape.name, kind: shape.kind, color: shape.color, thickness_m: shape.thickness_m };
          if (shape.properties) body.properties = shape.properties;
          const s = await this.post(`${API}/structures/${shape.id}`, body, 'PUT');
          Object.assign(shape, s);
          this.setStatus(`${KIND_LABEL[shape.kind] || 'Structure'} "${shape.name}" saved.`, 'ok');
        }
      } catch (e) {
        if (orig) shape.polygon = orig;   // the server rejected it: show what is actually stored
        this.setStatus(`Could not save: ${e.message}`, 'error');
      }
      this.emitInspector();
    }

    deleteSelectedVertex() {
      const shape = this.findSel();
      if (!shape || this.selVertex === null) return;
      const need = this.sel.type === 'zone' ? 3 : minPointsFor(shape.kind);
      if (shape.polygon.length <= need) {
        this.setStatus(`This shape needs at least ${need} points. Delete the whole shape instead.`, 'error');
        return;
      }
      const orig = shape.polygon.map((q) => ({ x: q.x, y: q.y }));
      shape.polygon.splice(this.selVertex, 1);
      this.selVertex = null;
      this.saveShape(this.sel.type, shape, orig);
    }

    /** Two-step delete rendered inside the inspector, never a modal. */
    deleteShape(type, id) {
      const shape = type === 'zone' ? this.zones.find((z) => z.id === id) : this.structures.find((s) => s.id === id);
      if (!shape) return;
      const panel = document.getElementById('zoneInspectorBody');
      const actions = panel && panel.querySelector('.fp-actions');
      if (actions && !actions.dataset.confirming) {
        actions.dataset.confirming = '1';
        actions.innerHTML =
          `<span class="fp-empty" style="padding:0">Delete "${escapeHtml(shape.name)}"?${type === 'zone' ? ' Recorded visits are kept.' : ''}</span>
           <button class="btn btn-sm btn-danger" id="fpConfirmDel">Yes, delete</button>
           <button class="btn btn-sm" id="fpAbortDel">Keep</button>`;
        actions.querySelector('#fpAbortDel').addEventListener('click', () => this.emitInspector());
        actions.querySelector('#fpConfirmDel').addEventListener('click', () => this._doDelete(type, id));
        return;
      }
      this._doDelete(type, id);
    }

    async _doDelete(type, id) {
      const url = type === 'zone' ? `${API}/zones/${id}` : `${API}/structures/${id}`;
      try {
        const res = await fetch(url, { method: 'DELETE' });
        if (!res.ok) throw new Error(res.statusText);
        if (type === 'zone') this.zones = this.zones.filter((z) => z.id !== id);
        else this.structures = this.structures.filter((s) => s.id !== id);
        this.select(null);
        this.setStatus('Deleted.', 'ok');
        this.recomputeSetup();
      } catch (e) {
        this.setStatus(`Could not delete: ${e.message}`, 'error');
      }
    }

    async saveCameraPlacement(cam) {
      try {
        await this.post(`${API}/cameras/${cam.camera_id}/placement`, {
          floor_x: cam.floor_x, floor_y: cam.floor_y, floor_z: cam.floor_z,
          azimuth_deg: cam.azimuth_deg, fov_deg: cam.fov_deg,
        }, 'PATCH');
        this.setStatus(`"${cam.name}" at ${cam.floor_x.toFixed(1)}, ${cam.floor_y.toFixed(1)} m, bearing ${Math.round(cam.azimuth_deg)}°.`, 'ok');
        if (window.deviceManager) window.deviceManager.renderCameras();
      } catch (e) {
        this.setStatus(`Could not save placement: ${e.message}`, 'error');
      }
    }

    async placeCameraAt(xm, ym) {
      const pending = window.__pendingCameraPlacement;
      if (!pending) {
        this.setStatus('Choose "Place" on a camera in the Devices panel first, then click the plan.', 'error');
        this.setMode(MODE.VIEW);
        return;
      }
      const cam = this.cameras.find((c) => c.camera_id === pending);
      if (cam) {
        cam.floor_x = +this.clampX(xm).toFixed(2);
        cam.floor_y = +this.clampY(ym).toFixed(2);
        await this.saveCameraPlacement(cam);
        this.select({ type: 'camera', id: cam.camera_id });
      }
      window.__pendingCameraPlacement = null;
      this.setMode(MODE.VIEW);
    }

    async saveStoreSize(name, w, h) {
      const data = await this.post(API, { width_m: w, height_m: h, name }, 'PUT');
      // PUT returns the full layout; keep our richer local copies where the
      // server response lacks them (older servers omit structures).
      this.applyLayout(Object.assign({}, this.layout, data));
      this._userZoomed = false;
      this.fitToView();
      this.recomputeSetup();
      return data;
    }

    // ------------------------------------------------------------ selection

    select(sel) {
      this.sel = sel;
      this.selVertex = null;
      this._stopThumb();
      this.emitInspector();
      if (window.deviceManager && window.deviceManager.highlight) {
        window.deviceManager.highlight(sel && sel.type === 'camera' ? sel.id : null);
      }
    }

    selectCamera(id) {
      const cam = this.cameras.find((c) => c.camera_id === id);
      if (!cam) return;
      this.select({ type: 'camera', id });
      // Bring it into view if the operator is zoomed elsewhere.
      const s = this.toScreen(cam.floor_x, cam.floor_y);
      if (s.x < 0 || s.y < 0 || s.x > this.canvas.clientWidth || s.y > this.canvas.clientHeight) {
        this._userZoomed = false; this.fitToView();
      }
    }

    selectShape(type, id) { this.select({ type, id }); }

    // Compatibility with earlier callers.
    selectZone(id) { this.select(id ? { type: 'zone', id } : null); }

    emitInspector() {
      const panel = document.getElementById('zoneInspectorBody');
      const title = document.getElementById('fpInspectorTitle');
      if (!panel) return;
      const shape = this.findSel();
      if (!shape) {
        if (title) title.textContent = 'Inspector';
        panel.innerHTML = '<div class="fp-empty">Select a zone, structure or camera on the plan to edit it and see what was measured there.</div>';
        return;
      }
      if (this.sel.type === 'zone') this.renderZoneInspector(panel, title, shape);
      else if (this.sel.type === 'structure') this.renderStructureInspector(panel, title, shape);
      else this.renderCameraInspector(panel, title, shape);
    }

    renderZoneInspector(panel, title, zone) {
      if (title) title.textContent = 'Zone';
      const m = (this.zoneMetrics || {})[zone.id];
      const dash = '<span class="fp-dash">&mdash;</span>';
      const val = (v) => (v === null || v === undefined ? dash : `${v}`);
      const cats = this.layout.zone_categories || Object.keys(CATEGORY_COLORS);

      panel.innerHTML = `
        <div class="fp-field"><label for="zName">Zone name</label>
          <input id="zName" name="zoneName" type="text" value="${escapeHtml(zone.name)}"></div>
        <div class="fp-field-row">
          <div class="fp-field"><label for="zCat">Category</label>
            <select id="zCat" name="zoneCategory">${cats.map((c) => `<option value="${c}" ${c === zone.category ? 'selected' : ''}>${c}</option>`).join('')}</select></div>
          <div class="fp-field"><label for="zColor">Colour</label>
            <input id="zColor" name="zoneColor" type="color" value="${zone.color || CATEGORY_COLORS.AISLE}"></div>
        </div>
        <div class="fp-zone-sub">${fmt(zone.area_m2)} m² · ${zone.polygon.length} corners${this.selVertex !== null ? ` · corner ${this.selVertex + 1} selected` : ''}</div>
        ${this.editHints()}
        <div class="fp-saved" id="zSaved"></div>
        ${m && m.observed ? '' : '<div class="fp-empty">No visits recorded in this zone yet.</div>'}
        <div class="metric-pill-grid">
          <div class="metric-pill"><span class="metric-pill-label">Visits today</span><span class="metric-pill-val">${val(m && m.visits)}</span></div>
          <div class="metric-pill"><span class="metric-pill-label">Unique people</span><span class="metric-pill-val">${val(m && m.unique_visitors)}</span></div>
          <div class="metric-pill"><span class="metric-pill-label">Avg dwell</span><span class="metric-pill-val">${m && m.avg_dwell_seconds ? m.avg_dwell_seconds + 's' : dash}</span></div>
          <div class="metric-pill"><span class="metric-pill-label">In zone now</span><span class="metric-pill-val">${val(m && m.occupancy_now)}</span></div>
        </div>
        <div class="fp-actions">
          <button class="btn btn-sm btn-danger" id="fpDelete">Delete zone</button>
          <button class="btn btn-sm" id="fpDone">Done</button>
        </div>`;

      const commit = async () => {
        zone.name = panel.querySelector('#zName').value.trim() || zone.name;
        zone.category = panel.querySelector('#zCat').value;
        zone.color = panel.querySelector('#zColor').value;
        await this.saveShape('zone', zone);
        flashSaved('zSaved');
      };
      ['zName', 'zCat', 'zColor'].forEach((id) => panel.querySelector(`#${id}`).addEventListener('change', commit));
      panel.querySelector('#fpDelete').addEventListener('click', () => this.deleteShape('zone', zone.id));
      panel.querySelector('#fpDone').addEventListener('click', () => this.select(null));
    }

    renderStructureInspector(panel, title, st) {
      if (title) title.textContent = KIND_LABEL[st.kind] || 'Structure';
      const isLine = POLYLINE_KINDS.has(st.kind);
      const canBePolygon = st.polygon.length >= 3;
      const props = st.properties || {};
      panel.innerHTML = `
        <div class="fp-inspector-kind">${escapeHtml(st.kind)}</div>
        <div class="fp-field"><label for="stName">Name</label>
          <input id="stName" name="structureName" type="text" value="${escapeHtml(st.name)}"></div>
        <div class="fp-field-row">
          <div class="fp-field"><label for="stKind">Kind</label>
            <select id="stKind" name="structureKind">${STRUCT_KINDS.map((k) => {
              const disabled = !POLYLINE_KINDS.has(k) && !canBePolygon;
              return `<option value="${k}" ${k === st.kind ? 'selected' : ''} ${disabled ? 'disabled' : ''}>${KIND_LABEL[k]}${disabled ? ' (needs 3+ points)' : ''}</option>`;
            }).join('')}</select></div>
          <div class="fp-field"><label for="stColor">Colour</label>
            <input id="stColor" name="structureColor" type="color" value="${st.color || KIND_COLORS[st.kind]}"></div>
        </div>
        <div class="fp-field-row">
          <div class="fp-field" ${isLine ? '' : 'style="display:none"'}><label for="stThick">Thickness (m)</label>
            <input id="stThick" name="structureThickness" type="number" step="0.05" min="0.02" max="3" value="${st.thickness_m || 0.2}"></div>
          <div class="fp-field"><label for="stHeight">Height (m, optional)</label>
            <input id="stHeight" name="structureHeight" type="number" step="0.1" min="0" max="30" value="${props.height_m != null ? props.height_m : ''}" placeholder="not set"></div>
        </div>
        <div class="fp-kv">
          <span>${isLine ? 'Length' : 'Area'}</span><b>${isLine ? fmt(st.length_m) + ' m' : fmt(st.area_m2) + ' m²'}</b>
          <span>Points</span><b>${st.polygon.length}${this.selVertex !== null ? ` (point ${this.selVertex + 1} selected)` : ''}</b>
        </div>
        ${this.editHints()}
        <div class="fp-saved" id="stSaved"></div>
        <div class="fp-actions">
          <button class="btn btn-sm btn-danger" id="fpDelete">Delete ${KIND_LABEL[st.kind].toLowerCase()}</button>
          <button class="btn btn-sm" id="fpDone">Done</button>
        </div>`;

      const commit = async () => {
        st.name = panel.querySelector('#stName').value.trim() || st.name;
        st.kind = panel.querySelector('#stKind').value;
        st.color = panel.querySelector('#stColor').value;
        st.thickness_m = parseFloat(panel.querySelector('#stThick').value) || st.thickness_m || 0.2;
        const h = panel.querySelector('#stHeight').value;
        st.properties = Object.assign({}, st.properties || {});
        if (h === '') delete st.properties.height_m; else st.properties.height_m = parseFloat(h);
        await this.saveShape('structure', st);
        flashSaved('stSaved');
        this.emitInspector();
      };
      ['stName', 'stKind', 'stColor', 'stThick', 'stHeight'].forEach((id) => panel.querySelector(`#${id}`).addEventListener('change', commit));
      panel.querySelector('#fpDelete').addEventListener('click', () => this.deleteShape('structure', st.id));
      panel.querySelector('#fpDone').addEventListener('click', () => this.select(null));
    }

    editHints() {
      return `<div class="fp-hintline">Drag the shape to move it · drag a corner to reshape · double-click an edge to add a corner · <kbd>Del</kbd> removes the selected corner or the shape.</div>`;
    }

    renderCameraInspector(panel, title, cam) {
      if (title) title.textContent = 'Camera';
      const online = cam.status === 'ONLINE';
      const cp = cam.calibration_points || null;
      const frame = cam.frame_width && cam.frame_height ? `${cam.frame_width}×${cam.frame_height}` : 'unknown until the first frame';
      panel.innerHTML = `
        <div class="fp-zone-head">
          <span class="fp-zone-name" style="color:${camColorFor(this.cameras, cam.camera_id)}">${escapeHtml(cam.name)}</span>
          <span class="badge ${online ? 'badge-green' : 'badge-danger'}">${escapeHtml(cam.status || 'UNKNOWN')}</span>
        </div>
        <img id="fpCamThumb" class="fp-thumb" alt="Live frame from ${escapeHtml(cam.name)}" src="${window.edgeAuth && window.edgeAuth.authUrl ? window.edgeAuth.authUrl(`/api/v1/cameras/${encodeURIComponent(cam.camera_id)}/snapshot?annotate=false&_=${Date.now()}`) : `/api/v1/cameras/${encodeURIComponent(cam.camera_id)}/snapshot?annotate=false&_=${Date.now()}`}">
        <div class="fp-kv">
          <span>Frame</span><b>${frame}</b>
          <span>Live tracks</span><b id="fpCamLive">${this.liveCountFor(cam.camera_id)}</b>
        </div>
        <div class="fp-zone-sub">Drag the marker to move it, drag the wedge tip to turn it, or set exact values.</div>
        <div class="fp-field-row">
          <div class="fp-field"><label for="cX">X (metres)</label>
            <input id="cX" name="cameraX" type="number" step="0.1" min="0" value="${(+cam.floor_x || 0).toFixed(1)}"></div>
          <div class="fp-field"><label for="cY">Y (metres)</label>
            <input id="cY" name="cameraY" type="number" step="0.1" min="0" value="${(+cam.floor_y || 0).toFixed(1)}"></div>
        </div>
        <div class="fp-field-row-3">
          <div class="fp-field"><label for="cAz">Bearing (°)</label>
            <input id="cAz" name="cameraAzimuth" type="number" step="5" min="0" max="359" value="${Math.round(cam.azimuth_deg || 0)}"></div>
          <div class="fp-field"><label for="cFov">FOV (°)</label>
            <input id="cFov" name="cameraFov" type="number" step="5" min="10" max="360" value="${Math.round(cam.fov_deg || 85)}"></div>
          <div class="fp-field"><label for="cZ">Height (m)</label>
            <input id="cZ" name="cameraHeight" type="number" step="0.1" min="0" max="20" value="${(+cam.floor_z || 3).toFixed(1)}"></div>
        </div>
        <div class="fp-saved" id="cSaved"></div>
        ${cam.has_homography
          ? `<div class="fp-cal-state is-cal">Calibrated${cp && cp.image_points ? ` from ${cp.image_points.length} point pairs` : ''}${cp && cp.saved_at ? ` · ${escapeHtml(String(cp.saved_at).slice(0, 16).replace('T', ' '))}` : ''}. People it sees are placed on the plan.</div>`
          : '<div class="fp-cal-state is-uncal">Not calibrated. This camera counts people but cannot place them on the plan, so it adds nothing to zone metrics, the heatmap or the live map.</div>'}
        <div class="fp-actions">
          <button class="btn btn-sm btn-primary" id="fpCalibrate">${cam.has_homography ? 'Recalibrate' : 'Calibrate'}</button>
          <button class="btn btn-sm" id="fpCamPlace">Place by click</button>
          <button class="btn btn-sm btn-danger" id="fpCamRemove">Remove</button>
          <button class="btn btn-sm" id="fpDone">Done</button>
        </div>`;

      const commit = async () => {
        cam.floor_x = this.clampX(parseFloat(panel.querySelector('#cX').value) || 0);
        cam.floor_y = this.clampY(parseFloat(panel.querySelector('#cY').value) || 0);
        cam.azimuth_deg = ((parseFloat(panel.querySelector('#cAz').value) || 0) % 360 + 360) % 360;
        cam.fov_deg = Math.max(10, Math.min(360, parseFloat(panel.querySelector('#cFov').value) || 85));
        cam.floor_z = parseFloat(panel.querySelector('#cZ').value) || 3;
        await this.saveCameraPlacement(cam);
        flashSaved('cSaved');
      };
      ['cX', 'cY', 'cAz', 'cFov', 'cZ'].forEach((id) => panel.querySelector(`#${id}`).addEventListener('change', commit));
      panel.querySelector('#fpCalibrate').addEventListener('click', () => {
        if (window.calibrationTool) window.calibrationTool.open(cam.camera_id);
      });
      panel.querySelector('#fpCamPlace').addEventListener('click', () => {
        window.__pendingCameraPlacement = cam.camera_id;
        this.startPlaceCamera();
      });
      panel.querySelector('#fpCamRemove').addEventListener('click', () => {
        if (window.deviceManager) window.deviceManager.remove(cam.camera_id, cam.name, panel.querySelector('.fp-actions'));
      });
      panel.querySelector('#fpDone').addEventListener('click', () => this.select(null));

      // Live thumbnail while this camera stays selected.
      this._stopThumb();
      this._thumbTimer = setInterval(() => {
        const img = document.getElementById('fpCamThumb');
        const live = document.getElementById('fpCamLive');
        if (!img || !this.sel || this.sel.type !== 'camera' || this.sel.id !== cam.camera_id) { this._stopThumb(); return; }
        const rawUrl = `/api/v1/cameras/${encodeURIComponent(cam.camera_id)}/snapshot?annotate=false&_=${Date.now()}`;
        if (this.isVisible()) img.src = window.edgeAuth && window.edgeAuth.authUrl ? window.edgeAuth.authUrl(rawUrl) : rawUrl;
        if (live) live.textContent = this.liveCountFor(cam.camera_id);
      }, 2000);
    }

    _stopThumb() { if (this._thumbTimer) { clearInterval(this._thumbTimer); this._thumbTimer = null; } }

    liveCountFor(cameraId) {
      const d = this.detections.find((x) => x.camera_id === cameraId);
      if (!d) return this.liveSupported === false ? 'n/a' : '—';
      return `${d.live_tracks}${d.calibrated ? '' : ' (not placeable)'}`;
    }

    resizeStore() {
      const panel = document.getElementById('zoneInspectorBody');
      const title = document.getElementById('fpInspectorTitle');
      if (!panel || !this.layout) return;
      this.sel = null; this.selVertex = null; this._stopThumb();
      if (title) title.textContent = 'Store';
      panel.innerHTML = `
        <div class="fp-zone-sub">The physical extent of the premises, in metres. Shapes outside the new bounds are clamped to fit.</div>
        <div class="fp-field"><label for="sName">Floor name</label>
          <input id="sName" name="storeName" type="text" value="${escapeHtml(this.layout.name || '')}"></div>
        <div class="fp-field-row">
          <div class="fp-field"><label for="sW">Width (m)</label>
            <input id="sW" name="storeWidth" type="number" step="0.5" min="1" max="1000" value="${this.layout.width_m}"></div>
          <div class="fp-field"><label for="sH">Depth (m)</label>
            <input id="sH" name="storeHeight" type="number" step="0.5" min="1" max="1000" value="${this.layout.height_m}"></div>
        </div>
        <div class="fp-saved" id="sSaved"></div>
        <div class="fp-actions">
          <button class="btn btn-sm btn-primary" id="sApply">Apply</button>
          <button class="btn btn-sm" id="sCancel">Cancel</button>
        </div>`;
      panel.querySelector('#sApply').addEventListener('click', async () => {
        try {
          const data = await this.saveStoreSize(
            panel.querySelector('#sName').value,
            parseFloat(panel.querySelector('#sW').value),
            parseFloat(panel.querySelector('#sH').value));
          const saved = document.getElementById('sSaved');
          if (saved) saved.textContent = `Saved — ${data.width_m} × ${data.height_m} m`;
          this.setStatus(`Store set to ${data.width_m} × ${data.height_m} m.`, 'ok');
        } catch (e) {
          this.setStatus(`Could not resize: ${e.message}`, 'error');
        }
      });
      panel.querySelector('#sCancel').addEventListener('click', () => this.select(null));
    }

    // ------------------------------------------------------- setup / status

    renderSetup() {
      const panel = document.getElementById('fpSetupPanel');
      const hint = document.getElementById('fpHint');
      if (!panel || !this.layout) return;
      const s = this.setup || { configured: true };
      if (s.configured) {
        panel.style.display = 'none';
        if (hint) hint.style.display = 'none';
        return;
      }
      panel.style.display = '';
      const step = (n, done, text, sub, btn, fn) => `
        <div class="fp-setup-step ${done ? 'done' : ''}">
          <span class="fp-step-n">${done ? '✓' : n}</span>
          <span class="fp-step-text">${text}<small>${sub}</small></span>
          <button class="btn btn-xs ${done ? '' : 'btn-primary'}" data-fn="${fn}">${btn}</button>
        </div>`;
      panel.innerHTML = `
        <div class="fp-setup-title">Set up your store</div>
        <div class="fp-setup-sub">Nothing is configured yet. Give the floor a name and its real dimensions, then draw the building and the areas you want measured. Everything saves as you go.</div>
        <div class="fp-field"><label for="suName">Floor name</label>
          <input id="suName" name="setupStoreName" type="text" value="${escapeHtml(this.layout.name || 'Store Floor')}"></div>
        <div class="fp-field-row">
          <div class="fp-field"><label for="suW">Width (m)</label>
            <input id="suW" name="setupStoreWidth" type="number" step="0.5" min="1" max="1000" value="${this.layout.width_m}"></div>
          <div class="fp-field"><label for="suH">Depth (m)</label>
            <input id="suH" name="setupStoreHeight" type="number" step="0.5" min="1" max="1000" value="${this.layout.height_m}"></div>
        </div>
        <div class="fp-saved" id="suSaved"></div>
        <div class="fp-actions" style="margin-top:0">
          <button class="btn btn-sm btn-primary" id="suApply">Save floor</button>
        </div>
        <div class="fp-setup-steps">
          ${step(1, s.structures > 0, 'Draw the room outline', 'Click each corner of the shop floor.', 'Draw room', 'room')}
          ${step(2, false, 'Add walls and shelving', 'Optional, but makes the map readable.', 'Draw wall', 'wall')}
          ${step(3, s.zones > 0, 'Draw analytics zones', 'Entrances, aisles, checkouts — what you want measured.', 'Draw zone', 'zone')}
          ${step(4, s.cameras > 0, 'Add cameras', 'Scan the network and USB for cameras, then place and calibrate them.', 'Scan for cameras', 'scan')}
        </div>`;
      panel.querySelector('#suApply').addEventListener('click', async () => {
        try {
          const data = await this.saveStoreSize(
            panel.querySelector('#suName').value,
            parseFloat(panel.querySelector('#suW').value),
            parseFloat(panel.querySelector('#suH').value));
          const saved = document.getElementById('suSaved');
          if (saved) saved.textContent = `Saved — ${escapeHtml(data.name)} · ${data.width_m} × ${data.height_m} m`;
        } catch (e) {
          this.setStatus(`Could not save the floor: ${e.message}`, 'error');
        }
      });
      panel.querySelectorAll('[data-fn]').forEach((b) => b.addEventListener('click', () => {
        const fn = b.dataset.fn;
        if (fn === 'room') this.startDraw('ROOM');
        else if (fn === 'wall') this.startDraw('WALL');
        else if (fn === 'zone') this.startDrawZone();
        else if (fn === 'scan' && window.deviceManager) window.deviceManager.scan();
      }));

      if (hint) {
        hint.style.display = this.mode === MODE.VIEW ? '' : 'none';
        hint.innerHTML = `<div class="fp-hint-title">This floor is empty</div>
          Use <b>Room</b> to trace the shop outline, <b>Wall</b>/<b>Shelf</b>/<b>Door</b> for the building, <b>Zone</b> for the areas to measure.
          Scroll to zoom, drag the background to pan. The panel on the right walks you through it.`;
      }
    }

    renderLiveDom() {
      const host = document.getElementById('fpCamBadges');
      const live = document.getElementById('fpLive');
      if (host) {
        if (this.liveSupported === false) {
          host.innerHTML = '<span class="fp-badge fp-badge-note">live map endpoint not available on this server</span>';
        } else {
          const parts = this.detections.map((d) => {
            const cls = d.status !== 'ONLINE' ? 'fp-badge-off' : d.calibrated ? 'fp-badge-cal' : 'fp-badge-uncal';
            const state = d.status !== 'ONLINE' ? String(d.status || 'offline').toLowerCase() : d.calibrated ? 'calibrated' : 'uncalibrated';
            return `<span class="fp-badge" data-cam="${escapeHtml(d.camera_id)}" title="Select this camera">
              <span class="fp-badge-swatch" style="background:${camColorFor(this.cameras, d.camera_id)}"></span>
              ${escapeHtml(d.camera_name || d.camera_id)} · <b>${d.live_tracks}</b> live · <span class="${cls}">${state}</span></span>`;
          });
          if (this.uncalibratedTracks > 0) {
            parts.push(`<span class="fp-badge fp-badge-note">${this.uncalibratedTracks} ${this.uncalibratedTracks === 1 ? 'person' : 'people'} detected on uncalibrated cameras (not placeable)</span>`);
          }
          host.innerHTML = parts.join('');
          host.querySelectorAll('[data-cam]').forEach((b) => b.addEventListener('click', () => this.selectCamera(b.dataset.cam)));
        }
      }
      if (live) {
        if (this.liveSupported === false) {
          live.className = 'fp-live fp-live-idle';
          live.textContent = 'live map: not supported by this server';
        } else if (!this.liveRunning) {
          live.className = 'fp-live fp-live-idle';
          live.textContent = 'live map: pipeline not running';
        } else {
          const n = this.tracks.size;
          live.className = `fp-live ${n ? '' : 'fp-live-idle'}`;
          live.innerHTML = `live map: <b>${n}</b> ${n === 1 ? 'person' : 'people'} placed · ${this.detections.filter((d) => d.calibrated).length}/${this.detections.length} cameras calibrated`;
        }
      }
    }

    emitState() {
      const el = document.getElementById('fpStats');
      if (el && this.layout) {
        const cal = this.coverage ? this.coverage.cameras_calibrated : this.cameras.filter((c) => c.has_homography).length;
        const tot = this.cameras.length;
        const now = this.activeNow === null || this.activeNow === undefined ? '—' : this.activeNow;
        el.textContent =
          `${escapeHtml(this.layout.name || '')} · ${this.layout.width_m}×${this.layout.height_m} m · ${this.structures.length} structures · ` +
          `${this.zones.length} zones · ${tot} cameras (${cal} calibrated) · ${now} in store now`;
      }
      const modeEl = document.getElementById('fpMode');
      if (modeEl) {
        modeEl.textContent = this.mode === MODE.DRAW
          ? `draw ${this.draftKind === 'ZONE' ? 'zone' : KIND_LABEL[this.draftKind].toLowerCase()}`
          : this.mode.replace('-', ' ');
      }
      const hint = document.getElementById('fpHint');
      if (hint && this.setup && !this.setup.configured) hint.style.display = this.mode === MODE.VIEW ? '' : 'none';
    }

    setStatus(msg, kind) {
      const el = document.getElementById('fpStatus');
      if (!el) return;
      el.textContent = msg;
      el.className = `fp-status fp-${kind || 'info'}`;
      clearTimeout(this._statusTimer);
      if (kind === 'ok') this._statusTimer = setTimeout(() => { el.textContent = ''; }, 4000);
    }

    toggleLayer(name, on) { this.showLayers[name] = !!on; }

    /**
     * Inline presence / dwell / interaction switch placed right after the
     * existing "Heatmap" layer checkbox. Styled inline so it depends on no
     * stylesheet class beyond the shared .btn/.btn-sm/.btn-primary.
     */
    _mountHeatmapKindToggle() {
      const cb = document.getElementById('layerHeatmap');
      const anchor = cb ? (cb.closest('label') || cb) : null;
      if (!anchor || document.getElementById('fpHeatmapKind')) return;
      const group = document.createElement('span');
      group.id = 'fpHeatmapKind';
      group.setAttribute('role', 'group');
      group.setAttribute('aria-label', 'Heatmap kind');
      group.style.cssText = 'display:inline-flex;align-items:center;gap:2px;';
      HEATMAP_KINDS.forEach((k) => {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'btn btn-sm';
        b.dataset.heatmapKind = k.kind;
        b.textContent = k.label;
        b.title = k.title;
        b.style.cssText = 'padding:1px 6px;font-size:10px;line-height:1.4;';
        b.addEventListener('click', () => this.setHeatmapKind(k.kind));
        group.appendChild(b);
      });
      const note = document.createElement('span');
      note.id = 'fpHeatmapNote';
      note.style.cssText = 'font-size:10px;color:var(--text-dim);margin-left:4px;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
      group.appendChild(note);
      anchor.insertAdjacentElement('afterend', group);
      this._syncHeatmapKindButtons();
    }

    _syncHeatmapKindButtons() {
      document.querySelectorAll('#fpHeatmapKind [data-heatmap-kind]').forEach((b) => {
        const on = b.dataset.heatmapKind === this.heatmapKind;
        b.classList.toggle('btn-primary', on);
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
    }

    _renderHeatmapNote(hm) {
      const note = document.getElementById('fpHeatmapNote');
      if (!note) return;
      if (hm && hm.observed) {
        const peak = hm.peak_value !== undefined ? hm.peak_value : hm.peak_count;
        note.textContent = `peak ${peak} ${hm.unit || ''} · ${hm.samples} samples`;
        note.title = note.textContent;
      } else {
        note.textContent = (hm && hm.message) || '';
        note.title = note.textContent;
      }
    }

    setHeatmapKind(kind) {
      if (!HEATMAP_KINDS.some((k) => k.kind === kind)) return;
      this.heatmapKind = kind;
      try { window.localStorage.setItem(HEATMAP_KIND_KEY, kind); } catch (_) { /* not persisted */ }
      this.heatmap = null;
      this.heatmapMessage = null;
      this._syncHeatmapKindButtons();
      const note = document.getElementById('fpHeatmapNote');
      if (note) note.textContent = 'loading…';
      this.refreshMetrics();
    }
    toggleSnap(on) { this.snapEnabled = !!on; }
    zoom(f) { this.scale = Math.max(2, Math.min(400, this.scale * f)); this._userZoomed = true; }
    resetView() { this._userZoomed = false; this.fitToView(); }

    // ------------------------------------------------- calibration support

    /** Ask for one point on the plan; cb receives {x, y} in metres. */
    beginPick(cb, hint) {
      this.pickCallback = cb;
      this.setMode(MODE.PICK);
      this.setStatus(hint || 'Click the matching point on the plan.');
    }

    cancelPick() {
      const cb = this.pickCallback;
      this.pickCallback = null;
      if (this.mode === MODE.PICK) this.setMode(MODE.VIEW);
      if (cb) cb(null);
    }

    setCalOverlay(overlay) { this.calOverlay = overlay || null; }

    // ---------------------------------------------------------------- render

    render() {
      this._raf = requestAnimationFrame(() => this.render());
      if (!this.isVisible()) return;
      const ctx = this.ctx;
      const W = this.canvas.clientWidth, H = this.canvas.clientHeight;
      ctx.clearRect(0, 0, W, H);
      ctx.fillStyle = T.bg;
      ctx.fillRect(0, 0, W, H);

      if (!this.layout) { this.drawCentredText('Loading blueprint…', T.placeholder); return; }

      this.drawFloor();
      if (this.showLayers.heatmap) this.drawHeatmap();
      if (this.showLayers.structures) this.drawStructures();
      if (this.showLayers.zones) this.drawZones();
      this.drawSelectionHandles();
      this.drawDraft();
      if (this.showLayers.cameras) this.drawCameras();
      if (this.showLayers.persons) this.drawPersons();
      this.drawCalOverlay();
      this.drawScaleBar();
    }

    drawCentredText(text, color, dy = 0) {
      const ctx = this.ctx;
      ctx.save();
      ctx.fillStyle = color;
      ctx.font = '600 14px system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(text, this.canvas.clientWidth / 2, this.canvas.clientHeight / 2 + dy);
      ctx.restore();
    }

    drawFloor() {
      const ctx = this.ctx;
      const { width_m, height_m } = this.layout;
      const tl = this.toScreen(0, 0), br = this.toScreen(width_m, height_m);
      ctx.save();
      ctx.fillStyle = T.inner;
      ctx.fillRect(tl.x, tl.y, br.x - tl.x, br.y - tl.y);

      if (this.showLayers.grid) {
        const step = this.scale < 6 ? 10 : this.scale < 14 ? 5 : this.scale < 40 ? 1 : 0.5;
        ctx.lineWidth = 1;
        ctx.beginPath();
        for (let x = 0; x <= width_m + 1e-9; x += step) { const s = this.toScreen(x, 0); ctx.moveTo(s.x, tl.y); ctx.lineTo(s.x, br.y); }
        for (let y = 0; y <= height_m + 1e-9; y += step) { const s = this.toScreen(0, y); ctx.moveTo(tl.x, s.y); ctx.lineTo(br.x, s.y); }
        ctx.strokeStyle = step < 1 ? `rgba(${T.grid},0.06)` : `rgba(${T.grid},0.10)`;
        ctx.stroke();
        if (step < 5) {
          ctx.beginPath();
          const major = step < 1 ? 1 : 5;
          for (let x = 0; x <= width_m + 1e-9; x += major) { const s = this.toScreen(x, 0); ctx.moveTo(s.x, tl.y); ctx.lineTo(s.x, br.y); }
          for (let y = 0; y <= height_m + 1e-9; y += major) { const s = this.toScreen(0, y); ctx.moveTo(tl.x, s.y); ctx.lineTo(br.x, s.y); }
          ctx.strokeStyle = `rgba(${T.grid},0.14)`;
          ctx.stroke();
        }
      }
      ctx.strokeStyle = T.origin;
      ctx.lineWidth = 2;
      ctx.strokeRect(tl.x, tl.y, br.x - tl.x, br.y - tl.y);
      ctx.restore();
    }

    drawHeatmap() {
      if (!this.heatmap || !this.heatmap.density_matrix) return;
      const ctx = this.ctx;
      const { density_matrix: m, grid_width: gw, grid_height: gh } = this.heatmap;
      const cw = (this.layout.width_m / gw) * this.scale, ch = (this.layout.height_m / gh) * this.scale;
      ctx.save();
      // Additive glow on the dark plan; multiply keeps it visible on the light one.
      ctx.globalCompositeOperation = T.heatBlend;
      for (let gy = 0; gy < gh; gy++) {
        for (let gx = 0; gx < gw; gx++) {
          const v = m[gy][gx];
          if (v <= 0.02) continue;
          const s = this.toScreen((gx / gw) * this.layout.width_m, (gy / gh) * this.layout.height_m);
          const r = Math.round(40 + 215 * v), g = Math.round(200 * (1 - v) + 40);
          ctx.fillStyle = `rgba(${r},${g},120,${0.18 + 0.55 * v})`;
          ctx.fillRect(s.x, s.y, cw + 1, ch + 1);
        }
      }
      ctx.restore();
    }

    tracePolygon(poly, close = true) {
      const ctx = this.ctx;
      ctx.beginPath();
      poly.forEach((p, i) => { const s = this.toScreen(p.x, p.y); if (i === 0) ctx.moveTo(s.x, s.y); else ctx.lineTo(s.x, s.y); });
      if (close) ctx.closePath();
    }

    isSelected(type, id) { return !!this.sel && this.sel.type === type && this.sel.id === id; }
    isHovered(type, id) { return !!this.hover && this.hover.type === type && this.hover.id === id; }

    drawStructures() {
      const ctx = this.ctx;
      // Rooms first, then fixtures, then walls and doors on top.
      const order = { ROOM: 0, OBSTACLE: 1, SHELF: 2, COUNTER: 2, WALL: 3, DOOR: 4 };
      const list = this.structures.slice().sort((a, b) => (order[a.kind] || 0) - (order[b.kind] || 0));
      list.forEach((st) => {
        const poly = st.polygon || [];
        const color = ink(st.color || KIND_COLORS[st.kind]) || T.text2;
        const selected = this.isSelected('structure', st.id);
        const hovered = this.isHovered('structure', st.id);
        ctx.save();
        if (st.kind === 'WALL') {
          if (poly.length < 2) { ctx.restore(); return; }
          this.tracePolygon(poly, false);
          ctx.lineCap = 'butt'; ctx.lineJoin = 'miter';
          ctx.lineWidth = Math.max(2, (st.thickness_m || 0.2) * this.scale);
          ctx.strokeStyle = selected ? T.select : hovered ? lighten(color) : color;
          ctx.stroke();
        } else if (st.kind === 'DOOR') {
          if (poly.length < 2) { ctx.restore(); return; }
          // A door is a gap in the wall: paint the floor colour over the wall,
          // then a thin dashed leaf line and the swing arc from the hinge.
          this.tracePolygon(poly, false);
          ctx.lineCap = 'butt';
          ctx.lineWidth = Math.max(3, (st.thickness_m || 0.1) * this.scale + 2);
          ctx.strokeStyle = T.inner;
          ctx.stroke();
          ctx.lineWidth = selected ? 2.5 : 1.5;
          ctx.strokeStyle = selected ? T.select : color;
          ctx.setLineDash([5, 3]);
          ctx.stroke();
          ctx.setLineDash([]);
          const a = this.toScreen(poly[0].x, poly[0].y), b = this.toScreen(poly[1].x, poly[1].y);
          const len = Math.hypot(b.x - a.x, b.y - a.y);
          const ang = Math.atan2(b.y - a.y, b.x - a.x);
          ctx.beginPath();
          ctx.arc(a.x, a.y, len, ang - Math.PI / 2, ang, false);
          ctx.lineWidth = 1;
          ctx.strokeStyle = hexToRgba(color, 0.7);
          ctx.stroke();
          ctx.beginPath(); ctx.moveTo(a.x, a.y);
          ctx.lineTo(a.x + Math.cos(ang - Math.PI / 2) * len, a.y + Math.sin(ang - Math.PI / 2) * len);
          ctx.stroke();
        } else {
          if (poly.length < 3) { ctx.restore(); return; }
          this.tracePolygon(poly, true);
          if (st.kind === 'ROOM') {
            ctx.fillStyle = hexToRgba(color, selected ? 0.12 : hovered ? 0.09 : 0.05);
            ctx.fill();
            ctx.lineWidth = selected ? 3 : 2;
            ctx.strokeStyle = selected ? T.select : color;
            ctx.stroke();
          } else {
            ctx.fillStyle = hexToRgba(color, selected ? 0.32 : hovered ? 0.26 : 0.18);
            ctx.fill();
            // Hatching clipped to the fixture.
            ctx.save();
            ctx.clip();
            const xs = poly.map((p) => this.toScreen(p.x, p.y));
            const minX = Math.min(...xs.map((p) => p.x)), maxX = Math.max(...xs.map((p) => p.x));
            const minY = Math.min(...xs.map((p) => p.y)), maxY = Math.max(...xs.map((p) => p.y));
            ctx.strokeStyle = hexToRgba(color, 0.55);
            ctx.lineWidth = 1;
            ctx.beginPath();
            const span = (maxX - minX) + (maxY - minY);
            for (let d = 0; d <= span; d += 7) {
              ctx.moveTo(minX + d, minY); ctx.lineTo(minX, minY + d);
            }
            ctx.stroke();
            ctx.restore();
            // The hatch replaced the current path; trace the outline again.
            this.tracePolygon(poly, true);
            ctx.lineWidth = selected ? 2.5 : 1.4;
            ctx.strokeStyle = selected ? T.select : color;
            ctx.stroke();
          }
        }
        if (this.showLayers.labels && st.name) {
          const c = POLYLINE_KINDS.has(st.kind) ? midpoint(poly) : polygonCentroid(poly);
          const s = this.toScreen(c.x, c.y);
          ctx.fillStyle = st.kind === 'ROOM' ? hexToRgba(color, 0.9) : T.text;
          ctx.font = st.kind === 'ROOM' ? '800 12px system-ui, sans-serif' : '600 10px system-ui, sans-serif';
          ctx.textAlign = 'center';
          if (st.kind === 'ROOM') {
            const tl = this.toScreen(Math.min(...poly.map((p) => p.x)), Math.min(...poly.map((p) => p.y)));
            ctx.textAlign = 'left';
            ctx.fillText(st.name, tl.x + 6, tl.y + 14);
          } else if (POLYLINE_KINDS.has(st.kind)) {
            ctx.fillText(st.name, s.x, s.y - 6);
          } else {
            ctx.fillText(st.name, s.x, s.y + 4);
          }
        }
        ctx.restore();
      });
    }

    drawZones() {
      const ctx = this.ctx;
      this.zones.forEach((z) => {
        const poly = z.polygon || [];
        if (poly.length < 3) return;
        const selected = this.isSelected('zone', z.id), hovered = this.isHovered('zone', z.id);
        ctx.save();
        this.tracePolygon(poly, true);
        const zc = ink(z.color);
        ctx.fillStyle = hexToRgba(zc, selected ? 0.30 : hovered ? 0.22 : 0.13);
        ctx.fill();
        ctx.strokeStyle = selected ? T.marker : zc;
        ctx.lineWidth = selected ? 2.5 : 1.4;
        ctx.stroke();
        if (this.showLayers.labels) {
          const c = polygonCentroid(poly);
          const s = this.toScreen(c.x, c.y);
          const m = (this.zoneMetrics || {})[z.id];
          ctx.fillStyle = T.text;
          ctx.font = '600 11px system-ui, sans-serif';
          ctx.textAlign = 'center';
          ctx.fillText(z.name, s.x, s.y);
          if (m && m.observed) {
            ctx.fillStyle = zc;
            ctx.font = '500 10px ui-monospace, monospace';
            ctx.fillText(`${m.visits} visits · ${m.avg_dwell_seconds || 0}s`, s.x, s.y + 13);
          }
        }
        ctx.restore();
      });
    }

    drawSelectionHandles() {
      if (!this.sel || this.sel.type === 'camera') return;
      const shape = this.findSel();
      if (!shape) return;
      const ctx = this.ctx;
      ctx.save();
      shape.polygon.forEach((p, i) => {
        const s = this.toScreen(p.x, p.y);
        const active = i === this.selVertex;
        ctx.fillStyle = active ? T.select : T.marker;
        ctx.strokeStyle = T.outline;
        ctx.lineWidth = 1.5;
        const r = active ? 5 : 4;
        ctx.fillRect(s.x - r, s.y - r, r * 2, r * 2);
        ctx.strokeRect(s.x - r, s.y - r, r * 2, r * 2);
      });
      ctx.restore();
    }

    drawDraft() {
      if (this.mode !== MODE.DRAW) return;
      const ctx = this.ctx;
      const color = ink(this.draftKind === 'ZONE' ? CATEGORY_COLORS.ENTRANCE : (KIND_COLORS[this.draftKind] || CATEGORY_COLORS.ENTRANCE));
      const preview = this.draftPreviewPoint();
      ctx.save();
      if (this.draft.length) {
        ctx.beginPath();
        this.draft.forEach((p, i) => { const s = this.toScreen(p.x, p.y); if (i === 0) ctx.moveTo(s.x, s.y); else ctx.lineTo(s.x, s.y); });
        if (preview) { const s = this.toScreen(preview.x, preview.y); ctx.lineTo(s.x, s.y); }
        ctx.strokeStyle = color;
        ctx.setLineDash([6, 4]);
        ctx.lineWidth = this.draftKind === 'WALL' ? Math.max(2, 0.2 * this.scale) : 2;
        ctx.stroke();
        ctx.setLineDash([]);
        if (!POLYLINE_KINDS.has(this.draftKind) && this.draft.length >= 2 && preview) {
          const a = this.toScreen(this.draft[0].x, this.draft[0].y), s = this.toScreen(preview.x, preview.y);
          ctx.beginPath(); ctx.moveTo(s.x, s.y); ctx.lineTo(a.x, a.y);
          ctx.strokeStyle = hexToRgba(color, 0.35); ctx.setLineDash([2, 4]); ctx.stroke(); ctx.setLineDash([]);
        }
      }
      this.draft.forEach((p) => {
        const s = this.toScreen(p.x, p.y);
        ctx.fillStyle = color;
        ctx.beginPath(); ctx.arc(s.x, s.y, 4, 0, Math.PI * 2); ctx.fill();
      });
      if (preview) {
        const s = this.toScreen(preview.x, preview.y);
        ctx.strokeStyle = color; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.arc(s.x, s.y, 5, 0, Math.PI * 2); ctx.stroke();
        ctx.fillStyle = T.text2;
        ctx.font = '500 10px ui-monospace, monospace';
        ctx.textAlign = 'left';
        ctx.fillText(`${preview.x.toFixed(1)}, ${preview.y.toFixed(1)} m`, s.x + 9, s.y - 8);
      }
      ctx.restore();
    }

    drawCameras() {
      const ctx = this.ctx;
      this.cameras.forEach((cam) => {
        const s = this.toScreen(cam.floor_x, cam.floor_y);
        const hovered = this.isHovered('camera', cam.camera_id);
        const selected = this.isSelected('camera', cam.camera_id);
        const online = cam.status === 'ONLINE';
        const color = ink(camColorFor(this.cameras, cam.camera_id));
        const reach = this.cameraReachPx();
        const half = ((cam.fov_deg || 85) * Math.PI) / 180 / 2;
        const bearing = ((cam.azimuth_deg || 0) - 90) * Math.PI / 180;

        ctx.save();
        ctx.beginPath();
        ctx.moveTo(s.x, s.y);
        ctx.arc(s.x, s.y, reach, bearing - half, bearing + half);
        ctx.closePath();
        ctx.fillStyle = hexToRgba(color, selected ? 0.22 : hovered ? 0.18 : 0.10);
        ctx.fill();
        ctx.lineWidth = selected ? 1.6 : 1;
        if (!cam.has_homography) { ctx.setLineDash([4, 4]); ctx.strokeStyle = hexToRgba(T.warn, 0.85); }
        else ctx.strokeStyle = hexToRgba(color, 0.6);
        ctx.stroke();
        ctx.setLineDash([]);

        if (selected) {
          const h = this.cameraHandlePos(cam);
          ctx.beginPath(); ctx.moveTo(s.x, s.y); ctx.lineTo(h.x, h.y);
          ctx.strokeStyle = hexToRgba(color, 0.7); ctx.lineWidth = 1; ctx.stroke();
          ctx.beginPath(); ctx.arc(h.x, h.y, 6, 0, Math.PI * 2);
          ctx.fillStyle = T.marker; ctx.fill();
          ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.stroke();
        }

        ctx.beginPath();
        ctx.arc(s.x, s.y, selected ? 10 : 8, 0, Math.PI * 2);
        ctx.fillStyle = online ? color : T.danger;
        ctx.fill();
        ctx.strokeStyle = selected ? T.marker : T.outline;
        ctx.lineWidth = 2;
        ctx.stroke();

        if (this.showLayers.labels) {
          ctx.fillStyle = T.text2;
          ctx.font = '600 10px system-ui, sans-serif';
          ctx.textAlign = 'center';
          ctx.fillText((cam.name || cam.camera_id).slice(0, 22), s.x, s.y - 14);
          if (!cam.has_homography) { ctx.fillStyle = T.warn; ctx.fillText('uncalibrated', s.x, s.y + 21); }
          else if (!online) { ctx.fillStyle = T.danger; ctx.fillText(String(cam.status || 'offline').toLowerCase(), s.x, s.y + 21); }
        }
        ctx.restore();
      });
    }

    drawPersons() {
      if (!this.tracks.size) return;
      this.expireTracks();
      const ctx = this.ctx;
      const now = Date.now();
      ctx.save();
      for (const t of this.tracks.values()) {
        const color = ink(camColorFor(this.cameras, t.camera_id));
        const pts = t.points;
        if (!pts.length) continue;
        for (let i = 1; i < pts.length; i++) {
          const a = this.toScreen(pts[i - 1].x, pts[i - 1].y), b = this.toScreen(pts[i].x, pts[i].y);
          ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y);
          ctx.strokeStyle = hexToRgba(color, 0.15 + 0.6 * (i / pts.length));
          ctx.lineWidth = 1 + 2 * (i / pts.length);
          ctx.stroke();
        }
        const last = pts[pts.length - 1];
        const s = this.toScreen(last.x, last.y);
        const stale = now - t.lastSeen > 1500;   // still within TTL but not seen this second
        ctx.beginPath(); ctx.arc(s.x, s.y, 6, 0, Math.PI * 2);
        ctx.fillStyle = hexToRgba(color, stale ? 0.5 : 0.95); ctx.fill();
        ctx.strokeStyle = T.outline; ctx.lineWidth = 1.5; ctx.stroke();
        if (this.showLayers.labels) {
          ctx.fillStyle = T.marker;
          ctx.font = '700 9px ui-monospace, monospace';
          ctx.textAlign = 'center';
          ctx.fillText(shortId(t.track_id), s.x, s.y - 10);
        }
      }
      ctx.restore();
    }

    drawCalOverlay() {
      const ov = this.calOverlay;
      if (!ov) return;
      const ctx = this.ctx;
      const color = ink(camColorFor(this.cameras, ov.cameraId));
      ctx.save();
      (ov.pairs || []).forEach((pair, i) => {
        if (!pair.floor) return;
        const s = this.toScreen(pair.floor.x, pair.floor.y);
        ctx.beginPath(); ctx.arc(s.x, s.y, 9, 0, Math.PI * 2);
        ctx.fillStyle = hexToRgba(color, 0.9); ctx.fill();
        ctx.strokeStyle = T.marker; ctx.lineWidth = 1.5; ctx.stroke();
        ctx.fillStyle = T.outline;
        ctx.font = '800 10px ui-monospace, monospace';
        ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillText(String(i + 1), s.x, s.y);
        const rp = ov.reprojected && ov.reprojected[i];
        if (rp) {
          const r = this.toScreen(rp.x, rp.y);
          ctx.beginPath(); ctx.moveTo(s.x, s.y); ctx.lineTo(r.x, r.y);
          ctx.strokeStyle = T.warn; ctx.lineWidth = 1; ctx.stroke();
          ctx.beginPath();
          ctx.moveTo(r.x - 6, r.y - 6); ctx.lineTo(r.x + 6, r.y + 6);
          ctx.moveTo(r.x - 6, r.y + 6); ctx.lineTo(r.x + 6, r.y - 6);
          ctx.strokeStyle = T.warn; ctx.lineWidth = 2; ctx.stroke();
        }
      });
      ctx.restore();
    }

    drawScaleBar() {
      const ctx = this.ctx;
      const metres = this.scale > 60 ? 1 : this.scale > 25 ? 2 : this.scale > 10 ? 5 : 10;
      const px = metres * this.scale;
      const x = 18, y = this.canvas.clientHeight - 64;
      ctx.save();
      ctx.strokeStyle = T.muted; ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(x, y); ctx.lineTo(x + px, y);
      ctx.moveTo(x, y - 4); ctx.lineTo(x, y + 4);
      ctx.moveTo(x + px, y - 4); ctx.lineTo(x + px, y + 4);
      ctx.stroke();
      ctx.fillStyle = T.muted;
      ctx.font = '500 10px ui-monospace, monospace';
      ctx.textAlign = 'left';
      ctx.fillText(`${metres} m`, x + px + 8, y + 4);
      ctx.restore();
    }
  }

  // ------------------------------------------------------------------ helpers

  function pointInPolygon(x, y, poly) {
    let inside = false;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      const xi = poly[i].x, yi = poly[i].y, xj = poly[j].x, yj = poly[j].y;
      if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi || 1e-12) + xi) inside = !inside;
    }
    return inside;
  }

  function polygonArea(poly) {
    let a = 0;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) a += (poly[j].x + poly[i].x) * (poly[j].y - poly[i].y);
    return Math.abs(a / 2);
  }

  function polygonCentroid(poly) {
    let x = 0, y = 0;
    poly.forEach((p) => { x += p.x; y += p.y; });
    return { x: x / poly.length, y: y / poly.length };
  }

  function midpoint(poly) {
    if (poly.length < 2) return poly[0] || { x: 0, y: 0 };
    const i = Math.floor((poly.length - 1) / 2);
    return { x: (poly[i].x + poly[i + 1].x) / 2, y: (poly[i].y + poly[i + 1].y) / 2 };
  }

  /** Distance from (px,py) to segment ab, plus the closest point. */
  function segDist(px, py, ax, ay, bx, by) {
    const dx = bx - ax, dy = by - ay;
    const l2 = dx * dx + dy * dy;
    let t = l2 ? ((px - ax) * dx + (py - ay) * dy) / l2 : 0;
    t = Math.max(0, Math.min(1, t));
    const x = ax + t * dx, y = ay + t * dy;
    return { d: Math.hypot(px - x, py - y), x, y, t };
  }

  function orthoTo(prev, pt) {
    const dx = pt.x - prev.x, dy = pt.y - prev.y;
    return Math.abs(dx) >= Math.abs(dy) ? { x: pt.x, y: prev.y } : { x: prev.x, y: pt.y };
  }

  function camColorFor(cameras, cameraId) {
    const idx = cameras.findIndex((c) => c.camera_id === cameraId);
    if (idx >= 0) return CAM_PALETTE[idx % CAM_PALETTE.length];
    let h = 0;
    for (const ch of String(cameraId || '')) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    return CAM_PALETTE[h % CAM_PALETTE.length];
  }

  function shortId(id) {
    const s = String(id == null ? '' : id);
    return '#' + (s.length > 4 ? s.slice(-4) : s);
  }

  function hexToRgba(hex, alpha) {
    const h = (hex || T.select || '').replace('#', '');
    const n = parseInt(h.length === 3 ? h.split('').map((c) => c + c).join('') : h, 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
  }

  function lighten(hex) {
    const h = (hex || T.text2 || '').replace('#', '');
    const n = parseInt(h.length === 3 ? h.split('').map((c) => c + c).join('') : h, 16);
    const mix = (v) => Math.round(v + (255 - v) * 0.35);
    return `rgb(${mix((n >> 16) & 255)},${mix((n >> 8) & 255)},${mix(n & 255)})`;
  }

  function fmt(v) { return v === null || v === undefined ? '—' : (Math.round(v * 100) / 100).toString(); }
  function cap(s) { return s.charAt(0).toUpperCase() + s.slice(1); }

  function flashSaved(id) {
    const saved = document.getElementById(id);
    if (!saved) return;
    saved.textContent = 'Saved';
    setTimeout(() => { if (saved.isConnected) saved.textContent = ''; }, 2000);
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function initFloorplan() {
    const canvas = document.getElementById('floorplanCanvas');
    if (canvas && !window.blueprintEditor) {
      window.blueprintEditor = new BlueprintEditor(canvas);
      window.storeFloorplanHUD = window.blueprintEditor;   // older inline handlers
    }
    // In-page self-test, only when explicitly requested in the URL.
    if (/[?&]__fptest=1(&|$)/.test(window.location.search)) {
      const s = document.createElement('script');
      s.src = '/static/js/selftest.js?v=2.1.2';
      document.body.appendChild(s);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initFloorplan);
  } else {
    initFloorplan();
  }
})();
