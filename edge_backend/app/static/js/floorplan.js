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
 * The map is the whole *site* (store_layouts.width_m/height_m): the building
 * is just a ROOM on it, next to OUTDOOR areas (car park, loading dock). The
 * site grows on any side through POST /layout/extend, which moves everything
 * placed (and every camera calibration) when space is added left or top.
 *
 * Public surface (window.blueprintEditor):
 *   resize(), resetView(), zoom(f), toggleLayer(name, on), toggleSnap(on)
 *   setHeatmapKind('presence'|'dwell'|'interaction')
 *   setTool('view'|'MEASURE'|kind|'ZONE'), startDraw(kind), startDrawZone(),
 *   startMeasure(), setFreeShape(on), finishPolygon(), undoPoint(), cancelDraw(),
 *   undo(), redo(), duplicateSelected(), resizeStore(), extendMap(sides),
 *   fitMapToContent(), startPlaceCamera(), selectCamera(id), selectShape(type, id),
 *   load(), beginPick(cb), cancelPick(), setCalOverlay(overlay|null)
 *
 * Stable API for other modules (window.FloorplanAPI, defined below), and the
 * events 'fp:layout-changed', 'fp:camera-hover', 'fp:camera-selected' on window.
 */
(function () {
  'use strict';

  /** Icon markup from js/icons.js (window.EdgeIcon); '' when that script is missing. Never throws. */
  const ico = (name, opts) => {
    try { return window.EdgeIcon && typeof window.EdgeIcon.svg === 'function' ? window.EdgeIcon.svg(name, opts) : ''; } catch (_) { return ''; }
  };

  // Camera thumbnail helpers: snapshots on the store network; through the
  // online-access tunnel (direct-video mode) no snapshot is ever requested.
  function thumbTransport() {
    return window.WebRtcLive ? window.WebRtcLive.transportNow() : 'local';
  }
  function thumbSnapshotUrl(cameraId) {
    const raw = `/api/v1/cameras/${encodeURIComponent(cameraId)}/snapshot?annotate=false&max_width=640&_=${Date.now()}`;
    return window.edgeAuth && window.edgeAuth.authUrl ? window.edgeAuth.authUrl(raw) : raw;
  }

  const API = '/api/v1/layout';

  const CATEGORY_COLORS = {
    ENTRANCE: '#00ff9d', EXIT: '#ffa500', AISLE: '#00d4ff', DEPARTMENT: '#a78bfa',
    SHELF: '#f472b6', CHECKOUT: '#fbbf24', STOCKROOM: '#94a3b8', EXCLUDED: '#475569',
  };

  // Structure kinds are the backend whitelist. WALL and DOOR are polylines.
  const STRUCT_KINDS = ['ROOM', 'OUTDOOR', 'WALL', 'SHELF', 'COUNTER', 'DOOR', 'OBSTACLE'];
  const POLYLINE_KINDS = new Set(['WALL', 'DOOR']);
  const KIND_COLORS = {
    ROOM: '#4a90d9', WALL: '#c9d4e0', SHELF: '#f472b6',
    COUNTER: '#fb923c', DOOR: '#fbbf24', OBSTACLE: '#94a3b8', OUTDOOR: '#6b8f71',
  };
  const KIND_LABEL = {
    ROOM: 'Room', WALL: 'Wall', SHELF: 'Shelf', COUNTER: 'Counter', DOOR: 'Door', OBSTACLE: 'Obstacle',
    OUTDOOR: 'Outdoor area',
  };
  const minPointsFor = (kind) => (POLYLINE_KINDS.has(kind) ? 2 : 3);
  // Drawn by press-drag-release as a rectangle unless "Free shape" is on.
  const RECT_KINDS = new Set(['ROOM', 'OUTDOOR', 'SHELF', 'COUNTER', 'OBSTACLE', 'ZONE']);
  // Name suggestions for rooms and outdoor areas (inspector name field).
  const ROOM_PRESETS = ['Sales floor', 'Storeroom', 'Cool room', 'Office', 'Staff room', 'Loading dock', 'Car park'];
  const OUTDOOR_TYPES = ['Car park', 'Loading dock', 'Side lane', 'Trolley bay', 'Garden centre'];
  // Keyboard shortcuts for the tool palette (no modifier keys).
  const TOOL_KEYS = { v: 'view', r: 'ROOM', o: 'OUTDOOR', w: 'WALL', d: 'DOOR', s: 'SHELF', c: 'COUNTER', b: 'OBSTACLE', z: 'ZONE', m: 'MEASURE' };

  // One colour per camera so people can be traced back to the camera that
  // placed them. Assigned by camera order, stable while the list is stable.
  const CAM_PALETTE = ['#00d4ff', '#00ff9d', '#a78bfa', '#fbbf24', '#f472b6', '#fb923c', '#34d399', '#60a5fa'];

  const GRID_SNAP_M = 0.5;
  const TRAIL_LEN = 10;
  const NUDGE_M = 0.1;            // arrow keys; Shift = NUDGE_BIG_M
  const NUDGE_BIG_M = 1;
  const NUDGE_SAVE_MS = 450;      // a burst of arrow presses is saved (and undone) as one step
  const UNDO_LIMIT = 100;
  const EXTEND_MAX_M = 500;       // per side and call (POST /layout/extend)
  const EXTEND_MARGIN_M = 1;      // room left around an item the map grows for
  const SCALE_STEPS = [0.5, 1, 2, 5, 10, 20, 50];
  const MIN_RECT_M = 0.2;

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
  const FAN_PX = 30;              // spacing of camera markers fanned out from one shared spot
  const CAM_LIST_AUTO_OPEN = 8;   // the camera list starts open only up to this many cameras

  const MODE = { VIEW: 'view', DRAW: 'draw', PLACE_CAMERA: 'place-camera', PICK: 'pick-point', MEASURE: 'measure' };

  // Heatmap kinds served by GET /api/v1/analytics/heatmaps?kind=...
  const HEATMAP_KINDS = [
    { kind: 'presence', label: 'Walked', title: 'Where people walked (tracked floor positions today)' },
    { kind: 'dwell', label: 'Stopped', title: 'Where people stopped (time spent per spot today)' },
    { kind: 'interaction', label: 'Touched shelves', title: 'Where people reached into shelves today' },
  ];
  const HEATMAP_KIND_KEY = 'edge_cctv_heatmap_kind';
  // Time range of the map heatmap. Today = the live endpoint above; the others
  // sum the recorded hourly snapshots (GET /api/v1/analytics/heatmaps/aggregate).
  const HEATMAP_RANGES = [
    { range: 'today', label: 'Today', title: 'Today so far, updated live' },
    { range: 'yesterday', label: 'Yesterday', title: 'Recorded hours of yesterday' },
    { range: '7d', label: 'Last 7 days', title: 'Recorded hours of the last 7 days, today included' },
    { range: '30d', label: 'Last 30 days', title: 'Recorded hours of the last 30 days, today included' },
  ];
  const HEATMAP_RANGE_KEY = 'edge_cctv_heatmap_range';
  const HEATMAP_HISTORY_REFRESH_MS = 3 * 60 * 1000;
  // After a 403 (heatmaps not in the plan) the live heatmap is asked for again only after this long.
  const HEATMAP_REFUSED_RETRY_MS = 5 * 60 * 1000;

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
      this.heatmapRange = 'today';
      try {
        const savedRange = window.localStorage.getItem(HEATMAP_RANGE_KEY);
        if (HEATMAP_RANGES.some((r) => r.range === savedRange)) this.heatmapRange = savedRange;
      } catch (_) { /* storage unavailable: default range */ }
      this._heatmapFetchedAt = 0;     // last aggregate fetch (non-today ranges)
      this._heatmapFetchKey = null;   // "range|kind" of that fetch
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
      this.freeShape = false;       // off: rooms, shelves... are dragged as rectangles
      this.measure = null;          // {a:{x,y}, b:{x,y}|null} while measuring
      this.dropPreview = null;      // ghost camera marker while a gallery card is dragged
      this.undoStack = [];          // commands: {label, snaps, undo(), redo()}
      this.redoStack = [];
      this._idMap = new Map();      // id of a deleted-and-recreated shape -> its new id
      this._busyUndo = false;
      this._siteBusy = false;       // a map size change (extend / fit / resize) is in flight
      this._nudge = null;           // pending arrow-key move, saved after a pause
      this._offer = null;           // open "Extend the map to fit?" offer
      this._pointers = new Map();   // active pointers (touch: pinch zoom)
      this._pinch = null;
      this._hoverCam = null;
      this._coarse = false;         // last input was touch or pen: bigger hit areas
      this._dragLabel = null;

      this.showLayers = {
        structures: true, zones: true, cameras: true, heatmap: true,
        persons: true, labels: true, grid: true,
      };

      this._thumbTimer = null;
      this._statusTimer = null;
      this.camListOpen = null;      // null: open only with a few cameras; true/false once toggled
      this._camListHtml = null;
      this._fan = new Map();
      this._fanKey = null;

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

    /** Height of the summary overlay at the bottom of the map (0 when hidden). */
    hudReserve() {
      const hud = document.querySelector('#fpWrapper .fp-hud-bottom');
      if (!hud) return 0;
      const h = hud.getBoundingClientRect().height;
      return h > 0 ? Math.ceil(h) + 10 : 0;
    }

    fitToView() {
      if (!this.layout) return;
      const pad = 36;
      const W = this.canvas.clientWidth, H = this.canvas.clientHeight;
      if (W < 10 || H < 10) return;      // hidden tab: measure again when shown
      // The fitted plan (plus the scale bar under it) stays clear of the
      // summary overlay at the bottom, so the two never overlap.
      const bottom = Math.max(pad, this.hudReserve() + 30);
      const top = pad;
      const w = W - pad * 2, h = H - top - bottom;
      this.scale = Math.max(2, Math.min(w / this.layout.width_m, h / this.layout.height_m));
      this.offsetX = (W - this.layout.width_m * this.scale) / 2;
      this.offsetY = top + (h - this.layout.height_m * this.scale) / 2;
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
        if (!this._readyFired) { this._readyFired = true; resolveReady(); }
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
      // Zones have no stored rotation: keep the in-memory hint across reloads.
      const rot = new Map((this.zones || []).filter((z) => Number.isFinite(z._rotation_deg)).map((z) => [z.id, z._rotation_deg]));
      this.layout = data;
      this.zones = data.zones || [];
      this.zones.forEach((z) => { if (rot.has(z.id)) z._rotation_deg = rot.get(z.id); });
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
      this.emitLayoutChanged();
    }

    emitLayoutChanged() { emit('fp:layout-changed', { cameras: this.apiCameras() }); }

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
      // One request pair at a time: on a big database these answers can take
      // longer than the poll interval, and piled-up requests would use up the
      // browser's connections (map saves then queue behind them).
      if (this._metricsBusy) { this._metricsAgain = true; return; }
      this._metricsBusy = true;
      try {
        await this._refreshMetricsOnce();
      } finally {
        this._metricsBusy = false;
      }
      if (this._metricsAgain) { this._metricsAgain = false; this.refreshMetrics(); }
    }

    async _refreshMetricsOnce() {
      if (!this.isVisible() || !this.layout) return;
      if (window.edgeAuth && typeof window.edgeAuth.isAuthenticated === 'function' && !window.edgeAuth.isAuthenticated()) return;
      const kind = this.heatmapKind;
      const range = this.heatmapRange;
      const heatUrl = this._heatmapUrl(range, kind);
      try {
        const [fpRes, hmRes] = await Promise.all([
          fetch('/api/v1/analytics/floorplan'),
          heatUrl ? fetch(heatUrl) : Promise.resolve(null),
        ]);
        if (fpRes.ok) {
          const fp = await fpRes.json();
          this.coverage = fp.coverage || null;
          this.zoneMetrics = {};
          (fp.zones || []).forEach((z) => { this.zoneMetrics[z.id] = z.metrics; });
          this.activeNow = (fp.active_shoppers_now === undefined) ? null : fp.active_shoppers_now;
        }
        if (hmRes && hmRes.status === 403) {
          // feature_disabled: not in the plan. Ask again only after a while.
          this._heatmapRefusedAt = Date.now();
          this.heatmap = null;
          this._renderHeatmapNote({ observed: false, message: 'Heatmaps are not included in your plan' });
        } else if (hmRes && hmRes.ok && kind === this.heatmapKind && range === this.heatmapRange) {
          this._heatmapRefusedAt = 0;
          const hm = await hmRes.json();
          if (range !== 'today') { this._heatmapFetchedAt = Date.now(); this._heatmapFetchKey = `${range}|${kind}`; }
          this.heatmap = hm.observed ? hm : null;
          this.heatmapMessage = hm.observed ? null : hm.message;
          this._renderHeatmapNote(hm);
        } else if (hmRes && !hmRes.ok && kind === this.heatmapKind && range === this.heatmapRange && range !== 'today') {
          this.heatmap = null;
          this._heatmapFetchedAt = Date.now();
          this._heatmapFetchKey = `${range}|${kind}`;
          this._renderHeatmapNote({ observed: false, message: `Recorded heatmaps could not be loaded (HTTP ${hmRes.status})` });
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
      // Pointer Events: mouse, touch and pen. One finger pans or drags, two pinch-zoom.
      c.addEventListener('pointerdown', (e) => this.onPointerDown(e));
      c.addEventListener('pointermove', (e) => this.onPointerMove(e));
      c.addEventListener('pointerup', (e) => this.onPointerUp(e, false));
      c.addEventListener('pointercancel', (e) => this.onPointerUp(e, true));
      c.addEventListener('pointerleave', (e) => { if (e.pointerType === 'mouse') this._emitCameraHover(null); });
      c.addEventListener('wheel', (e) => this.onWheel(e), { passive: false });
      c.addEventListener('dblclick', (e) => this.onDoubleClick(e));
      c.addEventListener('contextmenu', (e) => { e.preventDefault(); if (this.mode === MODE.DRAW) this.finishPolygon(); });
      window.addEventListener('resize', () => this.resize());
      window.addEventListener('keydown', (e) => this.onKey(e));
      window.addEventListener('keyup', (e) => { if (e.key === 'Shift') this.shiftDown = false; });
      document.addEventListener('visibilitychange', () => { if (this.isVisible()) this.refreshLive(); });
      const listToggle = document.getElementById('fpCamListToggle');
      if (listToggle) {
        listToggle.addEventListener('click', () => {
          this.camListOpen = listToggle.getAttribute('aria-expanded') !== 'true';
          this.renderLiveDom();
        });
      }
      if (window.ResizeObserver) {
        const wrap = c.parentElement || c;
        new ResizeObserver(() => this.resize()).observe(wrap);
        // The summary text can wrap onto more lines: refit so the plan stays clear of it.
        const hud = document.querySelector('#fpWrapper .fp-hud-bottom');
        if (hud) {
          let lastH = 0;
          new ResizeObserver(() => {
            const h = Math.round(hud.getBoundingClientRect().height);
            if (Math.abs(h - lastH) < 2) return;
            lastH = h;
            if (this.layout && !this._userZoomed) this.fitToView();
          }).observe(hud);
        }
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
      const mod = e.ctrlKey || e.metaKey;
      if (k === 'Escape') {
        if (this._offer) { this._resolveOffer('keep'); return; }
        if (this.mode === MODE.PICK) { this.cancelPick(); return; }
        if (this.mode === MODE.MEASURE && this.measure) { this.measure = null; this.setStatus('Measurement cleared. Click two points to measure again.'); return; }
        if (this.mode !== MODE.VIEW) { this.cancelDraw(); return; }
        this.select(null);
        return;
      }
      if (!this.isVisible()) return;
      // While the map size changes the frame may move: no edits until it is done.
      if (this._siteBusy) return;
      // Undo / redo / duplicate work in every mode except while drawing a shape.
      if (mod && !e.altKey && (k === 'z' || k === 'Z' || k === 'y' || k === 'Y')) {
        if (this.mode === MODE.DRAW) return;
        e.preventDefault();
        if (k === 'y' || k === 'Y' || e.shiftKey) this.redo(); else this.undo();
        return;
      }
      if (mod && !e.altKey && (k === 'd' || k === 'D')) {
        e.preventDefault();
        if (this.mode === MODE.VIEW) this.duplicateSelected();
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
      const arrows = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
      if (arrows[k] && this.mode === MODE.VIEW && this.sel && !mod) {
        e.preventDefault();
        const step = e.shiftKey ? NUDGE_BIG_M : NUDGE_M;
        this.nudge(arrows[k][0] * step, arrows[k][1] * step);
        return;
      }
      if (this.mode !== MODE.VIEW && this.mode !== MODE.DRAW && this.mode !== MODE.MEASURE) return;
      const t = TOOL_KEYS[k.toLowerCase()];
      if (t && !mod && !e.altKey) this.setTool(t);
    }

    // ---------------------------------------------------------- pointer input

    onPointerDown(e) {
      if (e.pointerType === 'mouse' && e.button !== 0) return;
      this._coarse = e.pointerType === 'touch' || e.pointerType === 'pen';
      if (this._offer) { this._resolveOffer('keep'); return; }
      try { this.canvas.setPointerCapture(e.pointerId); } catch (_) { /* not capturable */ }
      this._pointers.set(e.pointerId, this.localPoint(e));
      if (this._pointers.size === 2) { this._startPinch(); return; }
      if (this._pointers.size > 2 || this._pinch) return;
      if (this._siteBusy) { this.setStatus('Wait a moment: the map size is being changed.'); return; }
      this.onDown(e);
    }

    onPointerMove(e) {
      if (e.pointerType === 'mouse' && !this._pointers.size) this._coarse = false;
      if (this._pointers.has(e.pointerId)) this._pointers.set(e.pointerId, this.localPoint(e));
      if (this._pinch) { if (this._pointers.size >= 2) this._movePinch(); return; }
      this.onMove(e);
    }

    onPointerUp(e, cancelled) {
      const had = this._pointers.delete(e.pointerId);
      try { this.canvas.releasePointerCapture(e.pointerId); } catch (_) { /* already released */ }
      if (this._pinch) { if (this._pointers.size < 2) this._pinch = null; return; }
      if (!had) return;
      if (cancelled) { this._abortDrag(); return; }
      this.onUp(e);
    }

    /** Second finger down: drop whatever the first finger started and zoom instead. */
    _startPinch() {
      this._abortDrag();
      if (this.mode === MODE.DRAW && this._lastDraftAt && Date.now() - this._lastDraftAt < 400) this.undoPoint();
      const [a, b] = [...this._pointers.values()];
      const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      this._pinch = { dist: Math.max(10, Math.hypot(a.x - b.x, a.y - b.y)), world: this.toWorld(mid.x, mid.y), scale: this.scale };
    }

    _movePinch() {
      const [a, b] = [...this._pointers.values()];
      const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      const d = Math.max(10, Math.hypot(a.x - b.x, a.y - b.y));
      this.scale = Math.max(2, Math.min(400, this._pinch.scale * d / this._pinch.dist));
      // The pinched spot stays under the fingers, so moving both fingers pans too.
      this.offsetX = mid.x - this._pinch.world.x * this.scale;
      this.offsetY = mid.y - this._pinch.world.y * this.scale;
      this._userZoomed = true;
    }

    /** Undo the live effect of an unfinished drag (pinch started, pointer cancelled). */
    _abortDrag() {
      const d = this.dragging;
      this.dragging = null;
      this._dragLabel = null;
      if (!d) return;
      if (d.orig && ['shape', 'vertex', 'resize', 'rotate'].includes(d.kind)) {
        const s = this.findShape(d.type, d.id);
        if (s) { s.polygon = clonePts(d.orig); if (d.before) this.setRotation(d.type, s, snapRotation(d.before)); }
      }
      if ((d.kind === 'camera' || d.kind === 'camera-rotate') && d.before) {
        const cam = this.cameras.find((c) => c.camera_id === d.id);
        if (cam) Object.assign(cam, cameraFields(d.before));
      }
    }

    onDown(e) {
      const p = this.localPoint(e);
      const w = this.toWorld(p.x, p.y);
      this.shiftDown = e.shiftKey;
      this._emitCameraHover(null);

      if (this.mode === MODE.DRAW) {
        if (this.isRectDraw()) {
          const s = this.snap(w);
          this.dragging = { kind: 'rect', start: s, end: s };
        } else {
          this.addDraftPoint(w);
        }
        return;
      }
      if (this.mode === MODE.MEASURE) { this.measureClick(w); return; }
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
        this.dragging = { kind: 'camera-rotate', id: handle.camera_id, before: cameraSnap(handle), moved: false };
        return;
      }
      // Selected shape: the turn handle, then the resize handles of its box;
      // corners themselves win over resizing only with "Free shape" on.
      const fh = this.frameHandleAt(p.x, p.y);
      let vi = this.vertexAt(p.x, p.y);
      if (fh && (fh.kind === 'rotate' || !this.freeShape)) vi = null;
      // Boxed shapes keep their form unless "Free shape" is on (corners resize the box).
      if (vi !== null && !this.freeShape && this.sel && this.hasFrame(this.sel.type, this.findSel())) vi = null;
      if (vi !== null) {
        this.selVertex = vi;
        const shape = this.findSel();
        this.dragging = { kind: 'vertex', type: this.sel.type, id: this.sel.id, index: vi, moved: false,
          orig: clonePts(shape.polygon), before: this.snapShape(this.sel.type, shape) };
        this.emitInspector();
        return;
      }
      if (fh) {
        const shape = this.findSel();
        const f = this.shapeFrame(this.sel.type, shape);
        // Where the press landed relative to the handle, so a press a few
        // pixels off the handle does not resize the shape before it moves.
        const hw = frameToWorld(f, (fh.sx || 0) * f.w / 2, (fh.sy || 0) * f.h / 2);
        const pl = worldToFrame(f, w.x, w.y), hl = worldToFrame(f, hw.x, hw.y);
        this.dragging = { kind: fh.kind, handle: fh, type: this.sel.type, id: this.sel.id, frame: f, moved: false,
          orig: clonePts(shape.polygon), before: this.snapShape(this.sel.type, shape),
          startAngle: Math.atan2(w.y - f.cy, w.x - f.cx), grab: { u: hl.u - pl.u, v: hl.v - pl.v } };
        return;
      }

      const cam = this.showLayers.cameras ? this.cameraAt(p.x, p.y) : null;
      if (cam) {
        this.select({ type: 'camera', id: cam.camera_id });
        this.dragging = { kind: 'camera', id: cam.camera_id, moved: false, before: cameraSnap(cam) };
        return;
      }

      const hit = this.shapeAt(p.x, p.y, w);
      if (hit) {
        if (!this.sel || this.sel.type !== hit.type || this.sel.id !== hit.id) this.select(hit);
        const shape = this.findSel();
        this.dragging = {
          kind: 'shape', type: hit.type, id: hit.id, start: w, moved: false,
          orig: clonePts(shape.polygon), before: this.snapShape(hit.type, shape),
        };
        return;
      }

      // Empty space: deselect and pan (one finger on touch).
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
        } else if (d.kind === 'rect') {
          let end = this.snap(w);
          if (this.shiftDown) {
            // Shift: a square.
            const side = Math.max(Math.abs(end.x - d.start.x), Math.abs(end.y - d.start.y));
            end = { x: d.start.x + Math.sign(end.x - d.start.x || 1) * side, y: d.start.y + Math.sign(end.y - d.start.y || 1) * side };
          }
          d.end = end;
          this._dragLabel = `${fmt(Math.abs(end.x - d.start.x))} × ${fmt(Math.abs(end.y - d.start.y))} m`;
        } else if (d.kind === 'camera') {
          const cam = this.cameras.find((c) => c.camera_id === d.id);
          if (cam) { cam.floor_x = +w.x.toFixed(2); cam.floor_y = +w.y.toFixed(2); d.moved = true; }
        } else if (d.kind === 'camera-rotate') {
          const cam = this.cameras.find((c) => c.camera_id === d.id);
          if (cam) {
            const s = this.cameraScreenPos(cam);
            let deg = Math.atan2(p.y - s.y, p.x - s.x) * 180 / Math.PI + 90;
            deg = ((Math.round(deg) % 360) + 360) % 360;
            if (this.shiftDown) deg = (Math.round(deg / 15) * 15) % 360;
            cam.azimuth_deg = deg;
            d.moved = true;
          }
        } else if (d.kind === 'vertex') {
          const shape = this.findSel();
          if (shape) {
            shape.polygon[d.index] = this.snap({ x: w.x, y: w.y });
            d.moved = true;
          }
        } else if (d.kind === 'shape') {
          const shape = this.findSel();
          if (shape) {
            let dx = w.x - d.start.x, dy = w.y - d.start.y;
            if (this.snapEnabled) { dx = Math.round(dx / GRID_SNAP_M) * GRID_SNAP_M; dy = Math.round(dy / GRID_SNAP_M) * GRID_SNAP_M; }
            // Not clamped: past the edge, releasing offers to extend the map.
            shape.polygon = d.orig.map((q) => ({ x: r3(q.x + dx), y: r3(q.y + dy) }));
            d.moved = d.moved || dx !== 0 || dy !== 0;
          }
        } else if (d.kind === 'resize') {
          this._dragResize(d, w);
        } else if (d.kind === 'rotate') {
          this._dragRotate(d, w);
        }
        return;
      }

      // Hover feedback and cursor.
      let cursor = 'grab';
      let hoverCam = null;
      if (this.mode === MODE.DRAW || this.mode === MODE.PICK || this.mode === MODE.MEASURE) cursor = 'crosshair';
      else if (this.mode === MODE.PLACE_CAMERA) cursor = 'copy';
      else if (this.cameraHandleAt(p.x, p.y)) cursor = 'alias';
      else {
        const fh = this.frameHandleAt(p.x, p.y);
        if (fh && (fh.kind === 'rotate' || !this.freeShape)) cursor = fh.kind === 'rotate' ? 'alias' : fh.cursor;
        else if (this.vertexAt(p.x, p.y) !== null) cursor = 'move';
        else {
          const cam = this.showLayers.cameras ? this.cameraAt(p.x, p.y) : null;
          const hit = cam ? null : this.shapeAt(p.x, p.y, w);
          this.hover = cam ? { type: 'camera', id: cam.camera_id } : hit;
          hoverCam = cam;
          if (cam || hit) cursor = 'pointer';
        }
      }
      this.canvas.style.cursor = cursor;
      if (e.pointerType !== 'touch') this._emitCameraHover(hoverCam, e);
    }

    /** 'fp:camera-hover' with the pointer position, and {camera_id:null} when it leaves. */
    _emitCameraHover(cam, e) {
      if (cam && e) {
        this._hoverCam = cam.camera_id;
        emit('fp:camera-hover', { camera_id: cam.camera_id, clientX: e.clientX, clientY: e.clientY });
      } else if (this._hoverCam) {
        this._hoverCam = null;
        emit('fp:camera-hover', { camera_id: null });
      }
    }

    async onUp() {
      if (!this.dragging) return;
      const d = this.dragging;
      this.dragging = null;
      this._dragLabel = null;
      if (d.kind === 'rect') { await this.finishRect(d); return; }
      if (d.kind === 'camera' || d.kind === 'camera-rotate') {
        const cam = this.cameras.find((c) => c.camera_id === d.id);
        if (cam && d.moved) await this.commitCamera(cam, d.before, d.kind === 'camera' ? 'Move camera' : 'Turn camera');
        this.emitInspector();
      } else if (['shape', 'vertex', 'resize', 'rotate'].includes(d.kind) && d.moved) {
        const shape = this.findShape(d.type, d.id);
        const labels = { shape: 'Move', vertex: 'Move corner', resize: 'Resize', rotate: 'Turn' };
        if (shape) await this.commitGeometry(d.type, shape, d.before, `${labels[d.kind]} ${this.shapeLabel(d.type, shape)}`, d.kind === 'shape' ? d.orig : null);
      }
    }

    _dragResize(d, w) {
      const shape = this.findShape(d.type, d.id);
      if (!shape) return;
      const f = d.frame, h = d.handle;
      const L = worldToFrame(f, w.x, w.y);
      if (d.grab) { L.u += d.grab.u; L.v += d.grab.v; }
      const size = (span, along, sign) => {
        let n = sign * (along - (-sign * span / 2));
        if (this.snapEnabled) n = Math.round(n / GRID_SNAP_M) * GRID_SNAP_M;
        return Math.max(0.1, n);
      };
      let sx = 1, sy = 1;
      if (h.sx && f.w > 1e-6) sx = size(f.w, L.u, h.sx) / f.w;
      if (h.sy && f.h > 1e-6) sy = size(f.h, L.v, h.sy) / f.h;
      if (this.shiftDown && h.sx && h.sy) { const k = Math.max(sx, sy); sx = k; sy = k; }
      const au = h.sx ? -h.sx * f.w / 2 : 0, av = h.sy ? -h.sy * f.h / 2 : 0;
      shape.polygon = d.orig.map((q) => {
        const l = worldToFrame(f, q.x, q.y);
        const r = frameToWorld(f, au + (l.u - au) * sx, av + (l.v - av) * sy);
        return { x: r3(r.x), y: r3(r.y) };
      });
      d.moved = true;
      this._dragLabel = `${fmt(f.w * sx)} × ${fmt(f.h * sy)} m`;
    }

    _dragRotate(d, w) {
      const shape = this.findShape(d.type, d.id);
      if (!shape) return;
      const f = d.frame;
      const a = Math.atan2(w.y - f.cy, w.x - f.cx);
      let deg = f.angle + (a - d.startAngle) * 180 / Math.PI;
      deg = this.shiftDown ? Math.round(deg / 15) * 15 : Math.round(deg);
      deg = ((deg % 360) + 360) % 360;
      const t = (deg - f.angle) * Math.PI / 180;
      const c = Math.cos(t), s = Math.sin(t);
      shape.polygon = d.orig.map((q) => {
        const x = q.x - f.cx, y = q.y - f.cy;
        return { x: r3(f.cx + x * c - y * s), y: r3(f.cy + x * s + y * c) };
      });
      this.setRotation(d.type, shape, deg);
      d.moved = true;
      this._dragLabel = `${deg}°`;
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
      if (this.mode === MODE.DRAW) { if (!this.isRectDraw()) this.finishPolygon(); return; }
      if (this.mode !== MODE.VIEW) return;
      // Double-click on an edge of the selected shape inserts a vertex there.
      const edge = this.edgeAt(p.x, p.y);
      if (edge) {
        const shape = this.findSel();
        const before = this.snapShape(this.sel.type, shape);
        shape.polygon.splice(edge.index + 1, 0, this.snap(edge.point));
        this.selVertex = edge.index + 1;
        this.saveShape(this.sel.type, shape, before.polygon, { before, label: `Add a corner to ${this.shapeLabel(this.sel.type, shape)}` });
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
        const s = this.cameraScreenPos(c);
        if (Math.hypot(s.x - px, s.y - py) <= (this._coarse ? 20 : 14)) return c;
      }
      return null;
    }

    /**
     * Where a camera's marker is drawn. Cameras saved at the same spot (for
     * example a recorder's channels all added at one default point) would be
     * one pile where only the top marker can be grabbed, so each pile is
     * fanned out on screen into a small grid around its spot. Display only:
     * nothing is saved until the operator drags or places a camera, which
     * moves it out of the pile.
     */
    cameraScreenPos(cam) {
      const s = this.toScreen(cam.floor_x, cam.floor_y);
      const fan = this.cameraFan().get(cam.camera_id);
      return fan ? { x: s.x + fan.dx, y: s.y + fan.dy, fanned: fan.size } : s;
    }

    /** camera_id -> {dx, dy, size} screen offsets for cameras sharing a spot (within 5 cm). */
    cameraFan() {
      const key = this.cameras.map((c) => `${c.camera_id}@${c.floor_x},${c.floor_y}`).join('|');
      if (key === this._fanKey) return this._fan;
      const piles = new Map();
      this.cameras.forEach((c) => {
        if (!Number.isFinite(c.floor_x) || !Number.isFinite(c.floor_y)) return;
        const k = `${Math.round(c.floor_x * 20)}:${Math.round(c.floor_y * 20)}`;
        if (!piles.has(k)) piles.set(k, []);
        piles.get(k).push(c);
      });
      const fan = new Map();
      piles.forEach((list) => {
        if (list.length < 2) return;
        const cols = Math.ceil(Math.sqrt(list.length));
        const rows = Math.ceil(list.length / cols);
        list.forEach((c, i) => {
          fan.set(c.camera_id, {
            dx: ((i % cols) - (cols - 1) / 2) * FAN_PX,
            dy: (Math.floor(i / cols) - (rows - 1) / 2) * FAN_PX,
            size: list.length,
          });
        });
      });
      this._fanKey = key;
      this._fan = fan;
      return fan;
    }

    cameraHandlePos(cam) {
      const s = this.cameraScreenPos(cam);
      const a = ((cam.azimuth_deg || 0) - 90) * Math.PI / 180;
      const r = this.cameraReachPx();
      return { x: s.x + Math.cos(a) * r, y: s.y + Math.sin(a) * r };
    }

    cameraHandleAt(px, py) {
      if (!this.sel || this.sel.type !== 'camera' || !this.showLayers.cameras) return null;
      const cam = this.cameras.find((c) => c.camera_id === this.sel.id);
      if (!cam) return null;
      const h = this.cameraHandlePos(cam);
      return Math.hypot(h.x - px, h.y - py) <= (this._coarse ? 16 : 10) ? cam : null;
    }

    findSel() {
      if (!this.sel) return null;
      if (this.sel.type === 'zone') return this.zones.find((z) => z.id === this.sel.id) || null;
      if (this.sel.type === 'structure') return this.structures.find((s) => s.id === this.sel.id) || null;
      if (this.sel.type === 'camera') return this.cameras.find((c) => c.camera_id === this.sel.id) || null;
      return null;
    }

    findShape(type, id) {
      if (type === 'zone') return this.zones.find((z) => z.id === id) || null;
      if (type === 'structure') return this.structures.find((s) => s.id === id) || null;
      return null;
    }

    hitPx() { return this._coarse ? 14 : HIT_PX; }

    /** Polygons get a box with resize and turn handles; walls and doors only corners. */
    hasFrame(type, shape) {
      return !!shape && (type === 'zone' || (type === 'structure' && !POLYLINE_KINDS.has(shape.kind))) && (shape.polygon || []).length >= 3;
    }

    /** Rotation hint in degrees: structures keep it in properties, zones only in memory. */
    getRotation(type, shape) {
      if (!shape) return 0;
      const v = type === 'structure' ? (shape.properties || {}).rotation_deg : shape._rotation_deg;
      return Number.isFinite(+v) ? +v : 0;
    }

    setRotation(type, shape, deg) {
      if (!shape) return;
      const v = Number.isFinite(+deg) ? ((Math.round(+deg * 10) / 10) % 360 + 360) % 360 : 0;
      if (type === 'structure') {
        if (POLYLINE_KINDS.has(shape.kind)) return;
        shape.properties = Object.assign({}, shape.properties || {}, { rotation_deg: v });
      } else {
        shape._rotation_deg = v;
      }
    }

    /**
     * The shape's box in its own turned frame: centre (cx, cy), size w x h in
     * metres and angle (degrees, clockwise on screen). Walls and doors: the
     * plain bounding box.
     */
    shapeFrame(type, shape) {
      const pts = (shape && shape.polygon) || [];
      const angle = this.hasFrame(type, shape) ? this.getRotation(type, shape) : 0;
      const rad = angle * Math.PI / 180;
      const base = { cx: 0, cy: 0, rad };
      let minU = Infinity, maxU = -Infinity, minV = Infinity, maxV = -Infinity;
      pts.forEach((p) => {
        const l = worldToFrame(base, p.x, p.y);
        minU = Math.min(minU, l.u); maxU = Math.max(maxU, l.u);
        minV = Math.min(minV, l.v); maxV = Math.max(maxV, l.v);
      });
      if (!pts.length) { minU = maxU = minV = maxV = 0; }
      const c = frameToWorld(base, (minU + maxU) / 2, (minV + maxV) / 2);
      return { cx: c.x, cy: c.y, w: maxU - minU, h: maxV - minV, angle, rad };
    }

    /** Screen positions of the 8 resize handles and the turn handle of the selected shape. */
    frameHandles() {
      if (!this.sel || this.sel.type === 'camera' || this.mode !== MODE.VIEW) return [];
      const shape = this.findSel();
      if (!this.hasFrame(this.sel.type, shape)) return [];
      const f = this.shapeFrame(this.sel.type, shape);
      const out = [];
      const cursors = ['ew-resize', 'nwse-resize', 'ns-resize', 'nesw-resize'];   // 0, 45, 90, 135 degrees (y down)
      [-1, 0, 1].forEach((sy) => [-1, 0, 1].forEach((sx) => {
        if (!sx && !sy) return;
        const wpt = frameToWorld(f, sx * f.w / 2, sy * f.h / 2);
        const s = this.toScreen(wpt.x, wpt.y);
        // Cursor follows the handle's direction on screen, turned with the shape.
        const dir = ((Math.atan2(sy, sx) + f.rad) * 180 / Math.PI + 360) % 180;
        const cursor = cursors[Math.round(dir / 45) % 4];
        out.push({ kind: 'resize', sx, sy, x: s.x, y: s.y, cursor });
      }));
      const topW = frameToWorld(f, 0, -f.h / 2);
      const top = this.toScreen(topW.x, topW.y);
      const off = this._coarse ? 34 : 26;
      out.push({ kind: 'rotate', x: top.x + Math.sin(f.rad) * off, y: top.y - Math.cos(f.rad) * off, from: top });
      return out;
    }

    frameHandleAt(px, py) {
      const r = this._coarse ? 16 : 9;
      const hs = this.frameHandles();
      if (!hs.length) return null;
      // The turn handle first: it never sits on a corner.
      const rot = hs.find((h) => h.kind === 'rotate');
      if (rot && Math.hypot(rot.x - px, rot.y - py) <= r) return rot;
      // A shape only a few pixels deep on screen (a shelf when zoomed out) is
      // covered by its own handles: pressed inside its body, the handles'
      // reach shrinks so the press moves the shape; from outside they keep
      // their full reach. The nearest handle wins, never just the first listed.
      const shape = this.findSel();
      const f = shape ? this.shapeFrame(this.sel.type, shape) : null;
      const minPx = f ? Math.min(f.w, f.h) * this.scale : Infinity;
      const w = this.toWorld(px, py);
      const inside = !!(shape && shape.polygon && shape.polygon.length >= 3 && pointInPolygon(w.x, w.y, shape.polygon));
      const reach = inside ? Math.min(r, minPx / 3) : r;
      let best = null, bestD = Infinity;
      hs.forEach((h) => {
        if (h.kind !== 'resize') return;
        const d = Math.hypot(h.x - px, h.y - py);
        if (d <= reach && d < bestD) { best = h; bestD = d; }
      });
      return best;
    }

    shapeLabel(type, shape) {
      if (!shape) return 'item';
      if (type === 'zone') return `zone "${shape.name}"`;
      return `${(KIND_LABEL[shape.kind] || 'item').toLowerCase()} "${shape.name}"`;
    }


    vertexAt(px, py) {
      if (!this.sel || this.sel.type === 'camera') return null;
      const shape = this.findSel();
      if (!shape) return null;
      for (let i = 0; i < shape.polygon.length; i++) {
        const s = this.toScreen(shape.polygon[i].x, shape.polygon[i].y);
        if (Math.hypot(s.x - px, s.y - py) <= this.hitPx()) return i;
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
        if (r.d <= this.hitPx()) return { index: i, point: this.toWorld(r.x, r.y) };
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
      if (mode !== MODE.MEASURE) this.measure = null;
      if (this.dragging && this.dragging.kind === 'rect') this.dragging = null;
      this.updateDrawBanner();
      this.updateToolButtons();
      this.emitState();
    }

    setTool(tool) {
      if (tool === 'view') { this.cancelDraw(); return; }
      if (tool === 'MEASURE') { this.startMeasure(); return; }
      if (tool === 'ZONE') this.startDrawZone(); else this.startDraw(tool);
    }

    isRectDraw() { return this.mode === MODE.DRAW && RECT_KINDS.has(this.draftKind) && !this.freeShape; }

    /** "Free shape": draw by clicking corners, and drag single corners of a selected shape. */
    setFreeShape(on) {
      this.freeShape = !!on;
      const cb = document.getElementById('fpFreeShape');
      if (cb && cb.checked !== this.freeShape) cb.checked = this.freeShape;
      if (this.mode === MODE.DRAW) {
        const kind = this.draftKind;
        if (kind === 'ZONE') this.startDrawZone(); else this.startDraw(kind);
      }
    }

    _drawHelp(label, kind) {
      if (POLYLINE_KINDS.has(kind)) return `Drawing a ${label}: click along its path. Hold Shift for straight segments. Enter or double-click to finish.`;
      if (RECT_KINDS.has(kind) && !this.freeShape) return `Drawing a ${label}: press and drag on the map. Hold Shift for a square. Turn on "Free shape" to click each corner instead.`;
      return `Drawing a ${label}: click each corner. Enter or double-click to finish, Escape to cancel.`;
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
      this.setStatus(this._drawHelp(KIND_LABEL[kind].toLowerCase(), kind));
    }

    startDrawZone() {
      this.select(null);
      this.draftKind = 'ZONE';
      this.draft = [];
      this.setMode(MODE.DRAW);
      this.setStatus(this._drawHelp('zone', 'ZONE'));
    }

    startMeasure() {
      this.select(null);
      this.setMode(MODE.MEASURE);
      this.setStatus('Measuring: click two points on the map. Hold Shift for a straight line. Escape clears.');
    }

    measureClick(w) {
      const pt = { x: r3(w.x), y: r3(w.y) };
      if (!this.measure || this.measure.b) { this.measure = { a: pt, b: null }; return; }
      this.measure.b = this.shiftDown ? orthoTo(this.measure.a, pt) : pt;
      const d = Math.hypot(this.measure.b.x - this.measure.a.x, this.measure.b.y - this.measure.a.y);
      this.setStatus(`Distance: ${d.toFixed(2)} m. Click again to start a new measurement.`);
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
      // Not clamped: a shape drawn past the edge offers to extend the map when finished.
      let pt = { x: w.x, y: w.y };
      const prev = this.draft[this.draft.length - 1];
      if (prev && this.shiftDown && POLYLINE_KINDS.has(this.draftKind)) pt = orthoTo(prev, pt);
      pt = this.snap(pt);
      // The second mousedown of a double-click lands on the same spot; ignore
      // exact repeats so a double-click finish never leaves a zero-length edge.
      if (prev && Math.abs(prev.x - pt.x) < 1e-6 && Math.abs(prev.y - pt.y) < 1e-6) return;
      this.draft.push(pt);
      this._lastDraftAt = Date.now();
      this.updateDrawBanner();
    }

    draftPreviewPoint() {
      if (!this.cursorWorld) return null;
      let pt = { x: this.cursorWorld.x, y: this.cursorWorld.y };
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
      const rect = this.isRectDraw();
      if (count) count.textContent = this.draft.length;
      if (count && count.parentElement) count.parentElement.style.display = rect ? 'none' : '';
      if (finish) { finish.textContent = `Finish ${label}`; finish.disabled = rect || this.draft.length < need; finish.style.display = rect ? 'none' : ''; }
      if (hint) {
        hint.textContent = rect
          ? `${cap(label)}: press and drag on the map to draw it. Escape cancels.`
          : this.draft.length < need
            ? `${cap(label)}: click on the plan to add points, at least ${need}.${POLYLINE_KINDS.has(this.draftKind) ? ' Shift = straight.' : ''}`
            : `Add more points, or finish (Enter / double-click) to save this ${label}.`;
      }
    }

    updateToolButtons() {
      const active = this.mode === MODE.DRAW ? this.draftKind
        : this.mode === MODE.MEASURE ? 'MEASURE' : (this.mode === MODE.VIEW ? 'view' : null);
      document.querySelectorAll('#fpTools .fp-tool').forEach((b) => {
        const on = b.dataset.tool === active;
        b.classList.toggle('active', on);
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      const dup = document.getElementById('fpDuplicate');
      if (dup) dup.disabled = !(this.sel && this.sel.type !== 'camera' && this.mode === MODE.VIEW);
    }

    /**
     * Close the draft and persist it immediately. The shape is created with a
     * provisional name so it is on the plan straight away; the inspector then
     * edits it in place.
     */
    async finishPolygon() {
      if (this.mode !== MODE.DRAW || this.isRectDraw()) return;
      const kind = this.draftKind;
      const need = kind === 'ZONE' ? 3 : minPointsFor(kind);
      if (this.draft.length < need) {
        this.setStatus(`A ${kind === 'ZONE' ? 'zone' : KIND_LABEL[kind].toLowerCase()} needs at least ${need} points.`, 'error');
        return;
      }
      const polygon = this.draft.slice();
      this.draft = [];
      this.setMode(MODE.VIEW);
      await this.createShape(kind, polygon, false);
    }

    async finishRect(d) {
      const a = d.start, b = d.end || d.start;
      if (Math.abs(b.x - a.x) < MIN_RECT_M || Math.abs(b.y - a.y) < MIN_RECT_M) {
        this.setStatus('Press and drag on the map to draw the rectangle (or turn on "Free shape" to click each corner).');
        return;
      }
      const x0 = Math.min(a.x, b.x), x1 = Math.max(a.x, b.x), y0 = Math.min(a.y, b.y), y1 = Math.max(a.y, b.y);
      const kind = this.draftKind;
      this.setMode(MODE.VIEW);
      await this.createShape(kind, [{ x: x0, y: y0 }, { x: x1, y: y0 }, { x: x1, y: y1 }, { x: x0, y: y1 }], true);
    }

    /** New zone or structure from drawn points; past the edge it offers to extend the map. */
    async createShape(kind, polygon, isRect) {
      const type = kind === 'ZONE' ? 'zone' : 'structure';
      let snap;
      if (type === 'zone') {
        snap = { type, name: `Zone ${this.zones.length + 1}`, category: 'AISLE', polygon, color: CATEGORY_COLORS.AISLE, products: [] };
      } else {
        const count = this.structures.filter((s) => s.kind === kind).length;
        snap = { type, kind, name: `${KIND_LABEL[kind]} ${count + 1}`, polygon, color: KIND_COLORS[kind],
          properties: POLYLINE_KINDS.has(kind) ? {} : { rotation_deg: 0 } };
        if (POLYLINE_KINDS.has(kind)) snap.thickness_m = kind === 'DOOR' ? 0.1 : 0.2;
        if (kind === 'OUTDOOR') snap.properties.area_type = '';
      }
      if (isRect && type === 'zone') snap._rotation_deg = 0;
      const placed = await this.fitIntoSite(snap.polygon);
      if (!placed) return null;
      snap.polygon = placed;
      try {
        const obj = await this.postSnap(type, snap);
        this.pushCmd(this.cmdCreate(type, this.snapShape(type, obj), `Draw ${this.shapeLabel(type, obj)}`));
        this.select({ type, id: obj.id });
        if (type === 'zone') this.setStatus(`Zone created (${obj.area_m2} m²). Name it in the details panel.`, 'ok');
        else {
          const size = POLYLINE_KINDS.has(kind) ? `${fmt(obj.length_m)} m long` : `${fmt(obj.area_m2)} m²`;
          this.setStatus(`${KIND_LABEL[kind]} saved (${size}).`, 'ok');
        }
        this.recomputeSetup();
        return obj;
      } catch (e) {
        this.setStatus(`Could not save: ${e.message}`, 'error');
        return null;
      }
    }

    async post(url, body, method = 'POST') {
      const res = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      if (res.status === 404 && url.includes('/structures') && method === 'POST') {
        this.structuresSupported = false;
        throw new Error('the structures API is not available on this server');
      }
      if (!res.ok) {
        let detail = res.statusText;
        try { detail = (await res.json()).detail || detail; } catch (_) { /* not json */ }
        const msg = typeof detail === 'string' ? detail : (detail && detail.message) || JSON.stringify(detail);
        const err = new Error(msg);
        err.status = res.status;
        err.detail = detail;
        throw err;
      }
      return res.json();
    }

    // ---------------------------------------------------------- persistence

    /**
     * Save a zone or structure as it is now. ``opts.before`` (a snapShape of
     * the state before the edit) records an undo step labelled ``opts.label``.
     * Returns true when the server accepted it.
     */
    async saveShape(type, shape, orig, opts) {
      const o = opts || {};
      let ok = false;
      try {
        const saved = await this._putShape(type, shape);
        Object.assign(shape, saved);
        if (o.before) this.pushCmd(this.cmdUpdate(type, o.before, this.snapShape(type, shape), o.label || `Edit ${this.shapeLabel(type, shape)}`));
        this.setStatus(type === 'zone' ? `Zone "${shape.name}" saved.` : `${KIND_LABEL[shape.kind] || 'Structure'} "${shape.name}" saved.`, 'ok');
        ok = true;
      } catch (e) {
        // The server rejected it: show what is actually stored.
        if (o.before) restoreSnap(shape, o.before);
        else if (orig) shape.polygon = orig;
        this.setStatus(`Could not save: ${e.message}`, 'error');
      }
      this.emitInspector();
      return ok;
    }

    _putShape(type, shape) {
      if (type === 'zone') {
        const body = { polygon: shape.polygon, name: shape.name, category: shape.category, color: shape.color, products: shape.products || [] };
        if ('sku_id' in shape) body.sku_id = shape.sku_id || null;
        return this.post(`${API}/zones/${encodeURIComponent(shape.id)}`, body, 'PUT');
      }
      const body = { polygon: shape.polygon, name: shape.name, kind: shape.kind, color: shape.color, thickness_m: shape.thickness_m };
      if (shape.properties) body.properties = shape.properties;
      return this.post(`${API}/structures/${encodeURIComponent(shape.id)}`, body, 'PUT');
    }

    deleteSelectedVertex() {
      const shape = this.findSel();
      if (!shape || this.selVertex === null) return;
      const need = this.sel.type === 'zone' ? 3 : minPointsFor(shape.kind);
      if (shape.polygon.length <= need) {
        this.setStatus(`This shape needs at least ${need} points. Delete the whole shape instead.`, 'error');
        return;
      }
      const before = this.snapShape(this.sel.type, shape);
      shape.polygon.splice(this.selVertex, 1);
      this.selVertex = null;
      this.saveShape(this.sel.type, shape, before.polygon, { before, label: `Remove a corner of ${this.shapeLabel(this.sel.type, shape)}` });
    }

    /** Two-step delete rendered inside the inspector, never a modal. */
    deleteShape(type, id) {
      const shape = this.findShape(type, id);
      if (!shape) return;
      const panel = document.getElementById('zoneInspectorBody');
      const actions = panel && panel.querySelector('.fp-actions');
      if (actions && !actions.dataset.confirming) {
        actions.dataset.confirming = '1';
        actions.innerHTML =
          `<span class="fp-empty" style="padding:0">Delete "${escapeHtml(shape.name)}"?${type === 'zone' ? ' Recorded visits are kept.' : ''} Undo (Ctrl+Z) brings it back.</span>
           <button class="btn btn-sm btn-danger" id="fpConfirmDel">Yes, delete</button>
           <button class="btn btn-secondary btn-sm" id="fpAbortDel">Keep</button>`;
        actions.querySelector('#fpAbortDel').addEventListener('click', () => this.emitInspector());
        actions.querySelector('#fpConfirmDel').addEventListener('click', () => this._doDelete(type, id));
        return;
      }
      this._doDelete(type, id);
    }

    async _doDelete(type, id) {
      const shape = this.findShape(type, id);
      const before = shape ? this.snapShape(type, shape) : null;
      try {
        await this.deleteRemote(type, id);
        if (before) this.pushCmd(this.cmdDelete(type, before, `Delete ${this.shapeLabel(type, before)}`));
        this.setStatus('Deleted. Undo (Ctrl+Z) brings it back.', 'ok');
        this.recomputeSetup();
      } catch (e) {
        this.setStatus(`Could not delete: ${e.message}`, 'error');
      }
    }

    async deleteRemote(type, id) {
      const url = type === 'zone' ? `${API}/zones/${encodeURIComponent(id)}` : `${API}/structures/${encodeURIComponent(id)}`;
      const res = await fetch(url, { method: 'DELETE' });
      if (!res.ok && res.status !== 404) throw new Error(res.statusText || `HTTP ${res.status}`);
      if (type === 'zone') this.zones = this.zones.filter((z) => z.id !== id);
      else this.structures = this.structures.filter((s) => s.id !== id);
      if (this.sel && this.sel.type === type && this.sel.id === id) this.select(null);
    }

    /** Create a zone or structure from a snapshot (drawing, duplicate, undo of a delete). */
    async postSnap(type, snap) {
      if (type === 'zone') {
        const z = await this.post(`${API}/zones`, {
          name: snap.name, category: snap.category || 'AISLE', polygon: snap.polygon,
          color: snap.color || CATEGORY_COLORS.AISLE, sku_id: snap.sku_id || null, products: snap.products || [],
        });
        if (Number.isFinite(snap._rotation_deg)) z._rotation_deg = snap._rotation_deg;
        this.zones.push(z);
        return z;
      }
      const body = { kind: snap.kind, name: snap.name, polygon: snap.polygon, color: snap.color || KIND_COLORS[snap.kind],
        properties: Object.assign({}, snap.properties || {}) };
      if (snap.thickness_m) body.thickness_m = snap.thickness_m;
      const st = await this.post(`${API}/structures`, body);
      this.structures.push(st);
      return st;
    }

    /** Store a snapshot's fields on the (possibly re-created) shape it belongs to. */
    async putSnap(type, snap) {
      const id = this.resolveId(snap.id);
      const shape = this.findShape(type, id);
      if (!shape) throw new Error('it is no longer on the map');
      restoreSnap(shape, snap);
      const saved = await this._putShape(type, shape);
      Object.assign(shape, saved);
      if (this.sel && this.sel.type === type && this.sel.id === id) this.emitInspector();
    }

    async saveCameraPlacement(cam, opts) {
      const o = opts || {};
      try {
        await this.post(`${API}/cameras/${encodeURIComponent(cam.camera_id)}/placement`, {
          floor_x: cam.floor_x, floor_y: cam.floor_y, floor_z: cam.floor_z,
          azimuth_deg: cam.azimuth_deg, fov_deg: cam.fov_deg,
        }, 'PATCH');
        cam.map_placed = true;
        if (o.before) this.pushCmd(this.cmdCamera(o.before, cameraSnap(cam), o.label || `Move camera "${cam.name}"`));
        this.setStatus(`"${cam.name}" at ${(+cam.floor_x).toFixed(1)}, ${(+cam.floor_y).toFixed(1)} m, bearing ${Math.round(cam.azimuth_deg || 0)}°.`, 'ok');
        if (window.deviceManager && window.deviceManager.renderCameras) window.deviceManager.renderCameras();
        this.emitLayoutChanged();
        return true;
      } catch (e) {
        if (o.before) Object.assign(cam, cameraFields(o.before));
        this.setStatus(`Could not save placement: ${e.message}`, 'error');
        return false;
      }
    }

    async placeCameraAt(xm, ym) {
      const pending = window.__pendingCameraPlacement;
      if (!pending) {
        this.setStatus('Choose "Place" on a camera in the Devices panel first, then click the plan.', 'error');
        this.setMode(MODE.VIEW);
        return;
      }
      window.__pendingCameraPlacement = null;
      this.setMode(MODE.VIEW);
      await this.placeCameraById(pending, xm, ym);
    }

    /** Put a camera at (x, y) metres and save it; past the edge it offers to extend the map. */
    async placeCameraById(cameraId, xm, ym) {
      const cam = this.cameras.find((c) => c.camera_id === cameraId);
      if (!cam) throw new Error('this camera is not on the map; reload the page');
      const before = cameraSnap(cam);
      cam.floor_x = +(+xm).toFixed(2);
      cam.floor_y = +(+ym).toFixed(2);
      const ok = await this.commitCamera(cam, before, `Place camera "${cam.name}"`);
      const now = this.cameras.find((c) => c.camera_id === cameraId);
      if (now) this.select({ type: 'camera', id: cameraId });
      if (!ok) throw new Error('the camera position was not saved');
    }

    /** Save a moved or turned camera; past the edge it offers to extend the map first. */
    async commitCamera(cam, before, label) {
      const id = cam.camera_id;
      if (!this.insideSite(cam.floor_x, cam.floor_y)) {
        const box = { minX: cam.floor_x, maxX: cam.floor_x, minY: cam.floor_y, maxY: cam.floor_y };
        const choice = await this.offerExtend(box, `"${cam.name}"`);
        if (choice === 'extend') {
          const pending = cameraSnap(cam);
          const ch = await this.extendMap(this.sidesToFit(box));
          const fresh = this.cameras.find((c) => c.camera_id === id);
          if (!ch || !fresh) { if (fresh) Object.assign(fresh, cameraFields(before)); return false; }
          shiftSnap(before, ch.dx, ch.dy);
          shiftSnap(pending, ch.dx, ch.dy);
          Object.assign(fresh, cameraFields(pending));
          cam = fresh;
        } else {
          cam.floor_x = +this.clampX(cam.floor_x).toFixed(2);
          cam.floor_y = +this.clampY(cam.floor_y).toFixed(2);
        }
      }
      return this.saveCameraPlacement(cam, { before, label });
    }

    async saveStoreSize(name, w, h, clamp) {
      const body = { width_m: w, height_m: h, name };
      if (clamp) body.clamp = true;
      if (this._siteBusy) throw new Error('the map size is still being changed; wait a moment');
      const data = await this._runSiteChange(async () => {
        try {
          return await this.post(API, body, 'PUT');
        } catch (e) {
          if (!(e.detail && e.detail.code === 'items_outside')) await this._reloadAfterSiteError();
          throw e;
        }
      });
      // PUT returns the full layout; keep our richer local copies where the
      // server response lacks them (older servers omit structures).
      this.applyLayout(Object.assign({}, this.layout, data));
      if (clamp) this.clearHistory('Undo history cleared: items were moved inside the smaller map.');
      this._userZoomed = false;
      this.fitToView();
      this.recomputeSetup();
      return data;
    }

    // ------------------------------------------------------ map size (site)

    insideSite(x, y) {
      if (!this.layout) return true;
      const e = 1e-6;
      return x >= -e && y >= -e && x <= this.layout.width_m + e && y <= this.layout.height_m + e;
    }

    fitsSite(points) { return (points || []).every((p) => this.insideSite(p.x, p.y)); }

    /** Metres to add on each side so a box fits, with a margin, in 0.5 m steps. */
    sidesToFit(b) {
      const W = this.layout.width_m, H = this.layout.height_m;
      const up = (v) => Math.min(EXTEND_MAX_M, Math.ceil((v + EXTEND_MARGIN_M) / GRID_SNAP_M) * GRID_SNAP_M);
      return {
        left_m: b.minX < 0 ? up(-b.minX) : 0,
        top_m: b.minY < 0 ? up(-b.minY) : 0,
        right_m: b.maxX > W ? up(b.maxX - W) : 0,
        bottom_m: b.maxY > H ? up(b.maxY - H) : 0,
      };
    }

    /**
     * New geometry that may be past the edge: ask, then either extend the map
     * (the points are returned in the new, possibly shifted frame) or keep it
     * inside (points pulled to the edge). Null when extending failed.
     */
    async fitIntoSite(points, what) {
      if (this.fitsSite(points)) return points;
      const box = boundsOf(points);
      const choice = await this.offerExtend(box, what);
      if (choice !== 'extend') return points.map((q) => ({ x: r3(this.clampX(q.x)), y: r3(this.clampY(q.y)) }));
      const ch = await this.extendMap(this.sidesToFit(box));
      if (!ch) return null;
      return points.map((q) => ({ x: r3(q.x + ch.dx), y: r3(q.y + ch.dy) }));
    }

    /**
     * Save an edited shape. Past the edge, offers to extend the map; "Keep it
     * inside" moves a dragged shape back by the overshoot (``moveOrig``) or
     * pulls single points to the edge.
     */
    async commitGeometry(type, shape, before, label, moveOrig) {
      const id = shape.id;
      if (!this.fitsSite(shape.polygon)) {
        const box = boundsOf(shape.polygon);
        const choice = await this.offerExtend(box, this.shapeLabel(type, shape));
        if (choice === 'extend') {
          const pending = clonePts(shape.polygon);
          const rot = this.getRotation(type, shape);
          const ch = await this.extendMap(this.sidesToFit(box));
          const fresh = this.findShape(type, id);
          if (!fresh) return false;
          if (!ch) { restoreSnap(fresh, before); return false; }
          shiftSnap(before, ch.dx, ch.dy);
          fresh.polygon = pending.map((q) => ({ x: r3(q.x + ch.dx), y: r3(q.y + ch.dy) }));
          this.setRotation(type, fresh, rot);
          shape = fresh;
        } else if (moveOrig) {
          shape.polygon = this.keepInsideByDelta(moveOrig, shape.polygon);
        } else {
          shape.polygon = shape.polygon.map((q) => ({ x: r3(this.clampX(q.x)), y: r3(this.clampY(q.y)) }));
        }
      }
      return this.saveShape(type, shape, before.polygon, { before, label });
    }

    /** A moved copy of ``orig`` whose move is cut short at the map edge (shape unchanged). */
    keepInsideByDelta(orig, moved) {
      const W = this.layout.width_m, H = this.layout.height_m;
      let dx = moved[0].x - orig[0].x, dy = moved[0].y - orig[0].y;
      const b = boundsOf(orig);
      dx = Math.max(-b.minX, Math.min(W - b.maxX, dx));
      dy = Math.max(-b.minY, Math.min(H - b.maxY, dy));
      return orig.map((q) => ({ x: r3(q.x + dx), y: r3(q.y + dy) }));
    }

    /**
     * Non-modal question inside the map: resolves 'extend' or 'keep'. Any
     * other click on the map, or Escape, means "keep it inside".
     */
    offerExtend(box, what) {
      if (this._offer) this._resolveOffer('keep');
      const wrap = document.getElementById('fpWrapper');
      if (!wrap || !this.layout) return Promise.resolve('keep');
      const sides = this.sidesToFit(box);
      const el = document.createElement('div');
      el.className = 'fp-extend-offer';
      el.setAttribute('role', 'alertdialog');
      el.setAttribute('aria-label', 'Extend the map to fit?');
      el.innerHTML = `
        <span class="fp-extend-text"><b>${what ? escapeHtml(cap(what)) + ' goes' : 'This goes'} past the edge of the map.</b>
          Extend the map to fit? (${escapeHtml(describeSides(sides))})</span>
        <span class="fp-extend-actions">
          <button type="button" class="btn btn-sm btn-primary" data-act="extend">Extend the map</button>
          <button type="button" class="btn btn-sm btn-secondary" data-act="keep">Keep it inside</button>
        </span>`;
      wrap.appendChild(el);
      return new Promise((resolve) => {
        this._offer = { el, resolve };
        el.querySelectorAll('[data-act]').forEach((b) => b.addEventListener('click', () => this._resolveOffer(b.dataset.act)));
        const first = el.querySelector('[data-act="extend"]');
        if (first) { try { first.focus({ preventScroll: true }); } catch (_) { /* old browser */ } }
      });
    }

    _resolveOffer(choice) {
      const o = this._offer;
      if (!o) return;
      this._offer = null;
      o.el.remove();
      o.resolve(choice);
    }

    /**
     * Add (positive) or remove (negative) metres on each side through
     * POST /layout/extend. Space added left or top moves everything on the
     * server; the view, the undo history and anything in progress move with
     * it. Returns {dx, dy} or null (the reason is shown in the status line).
     */
    async extendMap(sides) {
      const body = {
        left_m: +sides.left_m || 0, top_m: +sides.top_m || 0,
        right_m: +sides.right_m || 0, bottom_m: +sides.bottom_m || 0,
      };
      if (this._siteBusy) { this._siteBusyNotice(); return null; }
      await this.flushNudge();
      return this._runSiteChange(async () => {
        try {
          const data = await this.post(`${API}/extend`, body);
          const ch = this._afterSiteChange(data);
          this.setStatus(`The map is now ${fmt(data.width_m)} × ${fmt(data.height_m)} m.`, 'ok');
          return ch;
        } catch (e) {
          const outside = e.detail && e.detail.code === 'items_outside';
          this.setStatus(outside
            ? `Not changed: ${describeItems(e.detail.items)} would end up outside the map. Move ${e.detail.items.length === 1 ? 'it' : 'them'} first.`
            : `Could not change the map size: ${e.message}`, 'error');
          if (outside) this._lastOutside = e.detail.items;
          else await this._reloadAfterSiteError();
          return null;
        }
      });
    }

    /** POST /layout/fit: the map shrinks or grows to what is on it, plus a margin. */
    async fitMapToContent(margin) {
      if (this._siteBusy) { this._siteBusyNotice(); return null; }
      await this.flushNudge();
      return this._runSiteChange(async () => {
        try {
          const data = await this.post(`${API}/fit`, { margin_m: Number.isFinite(+margin) ? +margin : 2 });
          this._afterSiteChange(data);
          this.setStatus(data.site_change && data.site_change.unchanged
            ? 'The map already fits what is on it.'
            : `The map now fits what is on it: ${fmt(data.width_m)} × ${fmt(data.height_m)} m.`, 'ok');
          return data;
        } catch (e) {
          this.setStatus(`Could not fit the map: ${e.message}`, 'error');
          await this._reloadAfterSiteError();
          return null;
        }
      });
    }

    /**
     * One map size change at a time. The site moves every item on the server,
     * so overlapping requests (repeated clicks) are ignored, not queued, and
     * the Map size / Fit buttons are disabled until the answer is in.
     */
    async _runSiteChange(fn) {
      this._siteBusy = true;
      this._syncSiteButtons();
      // Moving everything can take a while on a big database.
      this.setStatus('Changing the map size… everything on the map moves with it.');
      try {
        return await fn();
      } finally {
        this._siteBusy = false;
        this._syncSiteButtons();
      }
    }

    _siteBusyNotice() {
      this.setStatus('The map size is still being changed; wait a moment.', 'error');
    }

    _syncSiteButtons() {
      const busy = !!this._siteBusy;
      document.querySelectorAll('#zoneInspectorBody .fp-site-btn, #sAll, #sFit, #sApply, #sForce, .fp-extend-offer [data-act="extend"]')
        .forEach((b) => { b.disabled = busy; b.setAttribute('aria-busy', busy ? 'true' : 'false'); });
      this.updateUndoButtons();
    }

    /**
     * After a failed map size change, show what the server really has rather
     * than a local copy that may be stale (the request may have been applied
     * before the answer was lost). When the frame moved, the undo history and
     * anything in progress move with it, as after a successful change.
     */
    async _reloadAfterSiteError() {
      const ref = this._siteRefPoint(this.layout);
      let data;
      try {
        const res = await fetch(API);
        if (!res.ok) return;
        data = await res.json();
      } catch (_) { return; }
      if (!data || !Array.isArray(data.zones)) return;
      const now = ref ? this._siteRefPoint(data, ref.key) : null;
      const dx = now ? r3(now.x - ref.x) : 0, dy = now ? r3(now.y - ref.y) : 0;
      this._afterSiteChange(Object.assign({}, data, { site_change: { dx, dy } }));
    }

    /** A point that moves with the frame: the first vertex of a zone or structure, or a camera. */
    _siteRefPoint(layout, key) {
      if (!layout) return null;
      const pick = [];
      (layout.zones || []).forEach((z) => { if (z.polygon && z.polygon[0]) pick.push([`z:${z.id}`, z.polygon[0]]); });
      (layout.structures || []).forEach((st) => { if (st.polygon && st.polygon[0]) pick.push([`s:${st.id}`, st.polygon[0]]); });
      (layout.cameras || []).forEach((c) => { if (c.map_placed && Number.isFinite(c.floor_x)) pick.push([`c:${c.camera_id}`, { x: c.floor_x, y: c.floor_y }]); });
      const hit = key ? pick.find(([k]) => k === key) : pick[0];
      return hit ? { key: hit[0], x: +hit[1].x, y: +hit[1].y } : null;
    }

    _afterSiteChange(data) {
      const ch = data.site_change || {};
      const dx = +ch.dx || 0, dy = +ch.dy || 0;
      this.applyLayout(Object.assign({}, this.layout, data));
      if (dx || dy) {
        this._shiftHistory(dx, dy);
        const mv = (p) => { if (p) { p.x = r3(p.x + dx); p.y = r3(p.y + dy); } };
        if (this.measure) { mv(this.measure.a); mv(this.measure.b); }
        this.draft.forEach(mv);
        this.tracks.forEach((t) => t.points.forEach(mv));
        // What is on screen stays where it was; only the map edges move.
        if (this._userZoomed) { this.offsetX -= dx * this.scale; this.offsetY -= dy * this.scale; }
      }
      if (!this._userZoomed) this.fitToView();
      // A heatmap drawn in the old frame would be misplaced until the next fetch.
      if (dx || dy) { this.heatmap = null; this._heatmapFetchKey = null; }
      this.recomputeSetup();
      this.refreshMetrics();
      if (this.sel) this.emitInspector();
      return { dx, dy };
    }

    // ---------------------------------------------------------- undo / redo
    //
    // Each command replays API calls: create <-> delete, update old <-> new.
    // Snapshots hold metres in the current map frame; when the map origin
    // moves (space added left or top) every snapshot moves with it.

    snapShape(type, s) {
      const snap = { type, id: s.id, name: s.name, polygon: clonePts(s.polygon || []), color: s.color };
      if (type === 'zone') {
        snap.category = s.category;
        snap.products = (s.products || []).slice();
        if ('sku_id' in s) snap.sku_id = s.sku_id;
        if (Number.isFinite(s._rotation_deg)) snap._rotation_deg = s._rotation_deg;
      } else {
        snap.kind = s.kind;
        snap.thickness_m = s.thickness_m;
        snap.properties = JSON.parse(JSON.stringify(s.properties || {}));
      }
      return snap;
    }

    resolveId(id) {
      let cur = id;
      for (let i = 0; i < 1000 && this._idMap.has(cur); i++) cur = this._idMap.get(cur);
      return cur;
    }

    pushCmd(cmd) {
      if (this._busyUndo) return;   // replaying a command never records a new one
      this.undoStack.push(cmd);
      if (this.undoStack.length > UNDO_LIMIT) this.undoStack.shift();
      this.redoStack = [];
      this.updateUndoButtons();
    }

    clearHistory(msg) {
      this.undoStack = [];
      this.redoStack = [];
      this.updateUndoButtons();
      if (msg) this.setStatus(msg);
    }

    cmdUpdate(type, before, after, label) {
      return { label, snaps: [before, after], undo: () => this.putSnap(type, before), redo: () => this.putSnap(type, after) };
    }

    cmdCreate(type, snap, label) {
      const remove = async () => { await this.deleteRemote(type, this.resolveId(snap.id)); this.recomputeSetup(); };
      const restore = async () => {
        const prev = this.resolveId(snap.id);
        const obj = await this.postSnap(type, snap);
        if (obj.id !== prev) this._idMap.set(prev, obj.id);
        this.select({ type, id: obj.id });
        this.recomputeSetup();
      };
      return { label, snaps: [snap], undo: remove, redo: restore };
    }

    cmdDelete(type, snap, label) {
      const c = this.cmdCreate(type, snap, label);
      return { label, snaps: c.snaps, undo: c.redo, redo: c.undo };
    }

    cmdCamera(before, after, label) {
      const put = async (snap) => {
        const cam = this.cameras.find((c) => c.camera_id === snap.camera_id);
        if (!cam) throw new Error('the camera was removed');
        Object.assign(cam, cameraFields(snap));
        await this.post(`${API}/cameras/${encodeURIComponent(cam.camera_id)}/placement`, {
          floor_x: cam.floor_x, floor_y: cam.floor_y, floor_z: cam.floor_z, azimuth_deg: cam.azimuth_deg, fov_deg: cam.fov_deg,
        }, 'PATCH');
        if (this.sel && this.sel.type === 'camera' && this.sel.id === cam.camera_id) this.emitInspector();
        this.emitLayoutChanged();
      };
      return { label, snaps: [before, after], undo: () => put(before), redo: () => put(after) };
    }

    _shiftHistory(dx, dy) {
      const seen = new Set();
      const all = this.undoStack.concat(this.redoStack);
      all.forEach((c) => (c.snaps || []).forEach((snap) => {
        if (!snap || seen.has(snap)) return;
        seen.add(snap);
        shiftSnap(snap, dx, dy);
      }));
      if (this._nudge && this._nudge.before) shiftSnap(this._nudge.before, dx, dy);
    }

    async undo() { return this._replay(this.undoStack, this.redoStack, 'undo', 'Undone'); }

    async redo() { return this._replay(this.redoStack, this.undoStack, 'redo', 'Redone'); }

    async _replay(from, to, fn, verb) {
      if (this._busyUndo) return;
      if (this._siteBusy) { this._siteBusyNotice(); return; }
      await this.flushNudge();
      const cmd = from.pop();
      if (!cmd) { this.setStatus(fn === 'undo' ? 'Nothing to undo.' : 'Nothing to redo.'); this.updateUndoButtons(); return; }
      this._busyUndo = true;
      this.updateUndoButtons();
      try {
        await cmd[fn]();
        to.push(cmd);
        this.setStatus(`${verb}: ${cmd.label}.`, 'ok');
      } catch (e) {
        this.setStatus(`Could not ${fn} "${cmd.label}": ${e.message}`, 'error');
      } finally {
        this._busyUndo = false;
        this.updateUndoButtons();
        this.emitInspector();
      }
    }

    updateUndoButtons() {
      const u = document.getElementById('fpUndo'), r = document.getElementById('fpRedo');
      const last = this.undoStack[this.undoStack.length - 1], next = this.redoStack[this.redoStack.length - 1];
      if (u) { u.disabled = this._busyUndo || this._siteBusy || !last; u.title = last ? `Undo: ${last.label} (Ctrl+Z)` : 'Nothing to undo'; }
      if (r) { r.disabled = this._busyUndo || this._siteBusy || !next; r.title = next ? `Redo: ${next.label} (Ctrl+Shift+Z)` : 'Nothing to redo'; }
    }

    // ------------------------------------------------- duplicate and nudge

    async duplicateSelected() {
      if (!this.sel || this.sel.type === 'camera') {
        this.setStatus(this.sel ? 'Cameras cannot be duplicated: each one is a real device.' : 'Select a room, shelf, wall or zone first.');
        return;
      }
      const type = this.sel.type;
      const shape = this.findSel();
      if (!shape) return;
      await this.flushNudge();
      const snap = this.snapShape(type, shape);
      delete snap.id;
      snap.name = `${shape.name} copy`;
      // 1 m right and down, or the other way when that would leave the map.
      const b = boundsOf(snap.polygon);
      const ox = b.maxX + 1 <= this.layout.width_m ? 1 : (b.minX - 1 >= 0 ? -1 : 0);
      const oy = b.maxY + 1 <= this.layout.height_m ? 1 : (b.minY - 1 >= 0 ? -1 : 0);
      snap.polygon = snap.polygon.map((q) => ({ x: r3(q.x + ox), y: r3(q.y + oy) }));
      try {
        const obj = await this.postSnap(type, snap);
        this.pushCmd(this.cmdCreate(type, this.snapShape(type, obj), `Duplicate ${this.shapeLabel(type, shape)}`));
        this.select({ type, id: obj.id });
        this.setStatus(`Copied as "${obj.name}". Drag it into place.`, 'ok');
        this.recomputeSetup();
      } catch (e) {
        this.setStatus(`Could not duplicate: ${e.message}`, 'error');
      }
    }

    /** Arrow keys: move the selection; a burst of presses is saved and undone as one step. */
    nudge(dx, dy) {
      const sel = this.sel;
      const obj = this.findSel();
      if (!sel || !obj || !this.layout) return;
      if (!this._nudge || this._nudge.type !== sel.type || this._nudge.id !== sel.id) {
        this.flushNudge();
        this._nudge = { type: sel.type, id: sel.id, before: sel.type === 'camera' ? cameraSnap(obj) : this.snapShape(sel.type, obj) };
      }
      if (sel.type === 'camera') {
        obj.floor_x = r3(this.clampX((+obj.floor_x || 0) + dx));
        obj.floor_y = r3(this.clampY((+obj.floor_y || 0) + dy));
      } else {
        // The whole shape stays on the map; the map is grown with drag or "Map size".
        const b = boundsOf(obj.polygon);
        const mx = Math.max(-b.minX, Math.min(this.layout.width_m - b.maxX, dx));
        const my = Math.max(-b.minY, Math.min(this.layout.height_m - b.maxY, dy));
        obj.polygon = obj.polygon.map((q) => ({ x: r3(q.x + mx), y: r3(q.y + my) }));
      }
      clearTimeout(this._nudge.timer);
      this._nudge.timer = setTimeout(() => this.flushNudge(), NUDGE_SAVE_MS);
    }

    flushNudge() {
      const n = this._nudge;
      if (!n) return Promise.resolve();
      this._nudge = null;
      clearTimeout(n.timer);
      if (n.type === 'camera') {
        const cam = this.cameras.find((c) => c.camera_id === n.id);
        return cam ? this.saveCameraPlacement(cam, { before: n.before, label: `Nudge camera "${cam.name}"` }) : Promise.resolve();
      }
      const shape = this.findShape(n.type, n.id);
      return shape ? this.saveShape(n.type, shape, n.before.polygon, { before: n.before, label: `Nudge ${this.shapeLabel(n.type, shape)}` }) : Promise.resolve();
    }

    // ------------------------------------------------ API for other modules

    apiCameras() {
      return (this.cameras || []).map((c) => ({
        camera_id: c.camera_id,
        name: c.name || c.camera_id,
        // Older servers do not report map_placed: a camera with a spot counts as placed.
        placed: typeof c.map_placed === 'boolean' ? c.map_placed : Number.isFinite(c.floor_x) && Number.isFinite(c.floor_y),
        x_m: Number.isFinite(+c.floor_x) ? +c.floor_x : null,
        y_m: Number.isFinite(+c.floor_y) ? +c.floor_y : null,
        azimuth_deg: Number.isFinite(+c.azimuth_deg) ? +c.azimuth_deg : 0,
        fov_deg: Number.isFinite(+c.fov_deg) ? +c.fov_deg : 85,
        calibrated: !!c.has_homography,
        enabled: c.is_ai_enabled !== false && c.status !== 'DISABLED',
      }));
    }

    apiClientToWorld(clientX, clientY) {
      const r = this.canvas.getBoundingClientRect();
      if (!(r.width > 0) || clientX < r.left || clientX > r.right || clientY < r.top || clientY > r.bottom) return null;
      const w = this.toWorld(clientX - r.left, clientY - r.top);
      return { x_m: r3(w.x), y_m: r3(w.y), inside: this.insideSite(w.x, w.y) };
    }

    /** Select a camera, bring it into view and show its details. */
    focusCamera(cameraId) {
      const cam = this.cameras.find((c) => c.camera_id === cameraId);
      if (!cam) return;
      this.select({ type: 'camera', id: cameraId });
      const s = this.cameraScreenPos(cam);
      const W = this.canvas.clientWidth, H = this.canvas.clientHeight;
      const mx = W * 0.15, my = H * 0.15;
      if (s.x < mx || s.y < my || s.x > W - mx || s.y > H - my - this.hudReserve()) {
        this.offsetX += W / 2 - s.x;
        this.offsetY += (H - this.hudReserve()) / 2 - s.y;
        this._userZoomed = true;
      }
    }

    setDropPreview(xm, ym) {
      this.dropPreview = (xm === null || xm === undefined || ym === null || ym === undefined || !Number.isFinite(+xm) || !Number.isFinite(+ym))
        ? null : { x: +xm, y: +ym };
    }

    // ------------------------------------------------------------ selection

    select(sel) {
      if (this._nudge && (!sel || sel.type !== this._nudge.type || sel.id !== this._nudge.id)) this.flushNudge();
      const was = this.sel;
      this.sel = sel;
      this.selVertex = null;
      this._stopThumb();
      this.emitInspector();
      this.updateToolButtons();
      if (window.deviceManager && window.deviceManager.highlight) {
        window.deviceManager.highlight(sel && sel.type === 'camera' ? sel.id : null);
      }
      if (sel && sel.type === 'camera' && !(was && was.type === 'camera' && was.id === sel.id)) {
        emit('fp:camera-selected', { camera_id: sel.id });
      }
    }

    selectCamera(id) {
      const cam = this.cameras.find((c) => c.camera_id === id);
      if (!cam) return;
      this.select({ type: 'camera', id });
      // Bring it into view if the operator is zoomed elsewhere.
      const s = this.cameraScreenPos(cam);
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
        <div class="fp-field"><label for="zProducts">Products sold here</label>
          <textarea id="zProducts" name="zoneProducts" rows="2" placeholder="e.g. Chips, Lollies, Chocolate">${escapeHtml((zone.products || []).join(', '))}</textarea>
          <span class="fp-hintline">Comma-separated. Insights and the AI summary name these when they talk about this zone.</span></div>
        ${this.geometryFieldsHtml('zone', zone)}
        <div class="fp-zone-sub">${fmt(zone.area_m2)} m² · ${zone.polygon.length} corners${this.selVertex !== null ? ` · corner ${this.selVertex + 1} selected` : ''}${this.camerasLinkedTo(zone.id).length ? ` · counted by ${this.camerasLinkedTo(zone.id).length} uncalibrated camera${this.camerasLinkedTo(zone.id).length === 1 ? '' : 's'}` : ''}</div>
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
          <button class="btn btn-secondary btn-sm" id="fpDup" title="Duplicate (Ctrl+D)">${ico('copy', { size: 'sm' })}Duplicate</button>
          <button class="btn btn-sm btn-danger" id="fpDelete">Delete zone</button>
          <button class="btn btn-secondary btn-sm" id="fpDone">Done</button>
        </div>`;

      const commit = async () => {
        const before = this.snapShape('zone', zone);
        const name = panel.querySelector('#zName').value.trim() || zone.name;
        zone.name = name;
        zone.category = panel.querySelector('#zCat').value;
        zone.color = panel.querySelector('#zColor').value;
        zone.products = panel.querySelector('#zProducts').value.split(/[,\n]/).map((p) => p.trim()).filter(Boolean);
        const label = name !== before.name ? `Rename zone "${before.name}"` : `Edit zone "${name}"`;
        if (await this.saveShape('zone', zone, null, { before, label })) flashSaved('zSaved');
      };
      ['zName', 'zCat', 'zColor', 'zProducts'].forEach((id) => panel.querySelector(`#${id}`).addEventListener('change', commit));
      this.bindGeometryFields(panel, 'zone', zone);
      panel.querySelector('#fpDup').addEventListener('click', () => this.duplicateSelected());
      panel.querySelector('#fpDelete').addEventListener('click', () => this.deleteShape('zone', zone.id));
      panel.querySelector('#fpDone').addEventListener('click', () => this.select(null));
    }

    renderStructureInspector(panel, title, st) {
      if (title) title.textContent = KIND_LABEL[st.kind] || 'Structure';
      const isLine = POLYLINE_KINDS.has(st.kind);
      const canBePolygon = st.polygon.length >= 3;
      const props = st.properties || {};
      const presets = st.kind === 'ROOM' || st.kind === 'OUTDOOR';
      panel.innerHTML = `
        <div class="fp-inspector-kind">${escapeHtml(KIND_LABEL[st.kind] || st.kind)}</div>
        <div class="fp-field"><label for="stName">Name</label>
          <input id="stName" name="structureName" type="text" value="${escapeHtml(st.name)}"${presets ? ' list="fpRoomPresets" autocomplete="off"' : ''}>
          ${presets ? `<datalist id="fpRoomPresets">${ROOM_PRESETS.map((n) => `<option value="${escapeHtml(n)}"></option>`).join('')}</datalist>
          <span class="fp-hintline">Pick a suggestion (Sales floor, Storeroom, Cool room...) or type your own.</span>` : ''}</div>
        ${st.kind === 'OUTDOOR' ? `<div class="fp-field"><label for="stArea">Type of area (optional)</label>
          <input id="stArea" name="structureAreaType" type="text" list="fpOutdoorTypes" autocomplete="off" value="${escapeHtml(props.area_type || '')}" placeholder="e.g. Car park">
          <datalist id="fpOutdoorTypes">${OUTDOOR_TYPES.map((n) => `<option value="${escapeHtml(n)}"></option>`).join('')}</datalist></div>` : ''}
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
        ${this.geometryFieldsHtml('structure', st)}
        <div class="fp-kv">
          <span>${isLine ? 'Length' : 'Area'}</span><b>${isLine ? fmt(st.length_m) + ' m' : fmt(st.area_m2) + ' m²'}</b>
          <span>Points</span><b>${st.polygon.length}${this.selVertex !== null ? ` (point ${this.selVertex + 1} selected)` : ''}</b>
        </div>
        ${this.editHints(isLine)}
        <div class="fp-saved" id="stSaved"></div>
        <div class="fp-actions">
          <button class="btn btn-secondary btn-sm" id="fpDup" title="Duplicate (Ctrl+D)">${ico('copy', { size: 'sm' })}Duplicate</button>
          <button class="btn btn-sm btn-danger" id="fpDelete">Delete ${(KIND_LABEL[st.kind] || 'item').toLowerCase()}</button>
          <button class="btn btn-secondary btn-sm" id="fpDone">Done</button>
        </div>`;

      const commit = async () => {
        const before = this.snapShape('structure', st);
        st.name = panel.querySelector('#stName').value.trim() || st.name;
        st.kind = panel.querySelector('#stKind').value;
        st.color = panel.querySelector('#stColor').value;
        st.thickness_m = parseFloat(panel.querySelector('#stThick').value) || st.thickness_m || 0.2;
        const h = panel.querySelector('#stHeight').value;
        st.properties = Object.assign({}, st.properties || {});
        if (h === '') delete st.properties.height_m; else st.properties.height_m = parseFloat(h);
        const area = panel.querySelector('#stArea');
        if (area) st.properties.area_type = area.value.trim();
        // A kind picked without a colour of its own keeps the look of that kind.
        if (st.kind !== before.kind && before.color === KIND_COLORS[before.kind]) st.color = KIND_COLORS[st.kind];
        const label = st.name !== before.name ? `Rename ${this.shapeLabel('structure', before)}` : `Edit ${this.shapeLabel('structure', st)}`;
        if (await this.saveShape('structure', st, null, { before, label })) flashSaved('stSaved');
        this.emitInspector();
      };
      ['stName', 'stKind', 'stColor', 'stThick', 'stHeight', 'stArea'].forEach((id) => {
        const el = panel.querySelector(`#${id}`);
        if (el) el.addEventListener('change', commit);
      });
      this.bindGeometryFields(panel, 'structure', st);
      panel.querySelector('#fpDup').addEventListener('click', () => this.duplicateSelected());
      panel.querySelector('#fpDelete').addEventListener('click', () => this.deleteShape('structure', st.id));
      panel.querySelector('#fpDone').addEventListener('click', () => this.select(null));
    }

    /** Position (centre), size and angle in metres / degrees, typed in exactly. */
    geometryFieldsHtml(type, shape) {
      const f = this.shapeFrame(type, shape);
      const box = this.hasFrame(type, shape);
      const n = (v, d) => (Math.round(v * 10 ** d) / 10 ** d).toString();
      return `
        <div class="fp-geom">
          <div class="fp-field-row-3">
            <div class="fp-field"><label for="gX">X (m)</label>
              <input id="gX" name="shapeX" type="number" step="0.1" value="${n(f.cx, 2)}" title="Centre, metres from the left edge of the map"></div>
            <div class="fp-field"><label for="gY">Y (m)</label>
              <input id="gY" name="shapeY" type="number" step="0.1" value="${n(f.cy, 2)}" title="Centre, metres from the top edge of the map"></div>
            ${box ? `<div class="fp-field"><label for="gA">Angle (°)</label>
              <input id="gA" name="shapeAngle" type="number" step="1" min="-360" max="360" value="${n(f.angle, 1)}"></div>` : '<div></div>'}
          </div>
          ${box ? `<div class="fp-field-row">
            <div class="fp-field"><label for="gW">Width (m)</label>
              <input id="gW" name="shapeWidth" type="number" step="0.1" min="0.1" max="1000" value="${n(f.w, 2)}"></div>
            <div class="fp-field"><label for="gD">Depth (m)</label>
              <input id="gD" name="shapeDepth" type="number" step="0.1" min="0.1" max="1000" value="${n(f.h, 2)}"></div>
          </div>` : ''}
          <span class="fp-hintline">X and Y are the centre, measured from the top-left corner of the map.</span>
        </div>`;
    }

    bindGeometryFields(panel, type, shape) {
      const read = (id) => { const el = panel.querySelector(`#${id}`); return el ? parseFloat(el.value) : NaN; };
      const apply = () => this.applyGeometry(type, shape, { x: read('gX'), y: read('gY'), w: read('gW'), h: read('gD'), a: read('gA') });
      ['gX', 'gY', 'gW', 'gD', 'gA'].forEach((id) => {
        const el = panel.querySelector(`#${id}`);
        if (el) el.addEventListener('change', apply);
      });
    }

    /** Move / resize / turn a shape to typed values; past the edge it offers to extend the map. */
    async applyGeometry(type, shape, v) {
      const before = this.snapShape(type, shape);
      const f = this.shapeFrame(type, shape);
      const box = this.hasFrame(type, shape);
      const num = (x, dflt) => (Number.isFinite(x) ? x : dflt);
      const nw = box ? Math.max(0.1, num(v.w, f.w)) : f.w;
      const nh = box ? Math.max(0.1, num(v.h, f.h)) : f.h;
      const na = box ? ((num(v.a, f.angle) % 360) + 360) % 360 : 0;
      const sx = box && f.w > 1e-6 ? nw / f.w : 1;
      const sy = box && f.h > 1e-6 ? nh / f.h : 1;
      const nf = { cx: num(v.x, f.cx), cy: num(v.y, f.cy), rad: na * Math.PI / 180 };
      shape.polygon = shape.polygon.map((q) => {
        const l = worldToFrame(f, q.x, q.y);
        const r = box ? frameToWorld(nf, l.u * sx, l.v * sy) : { x: q.x + (nf.cx - f.cx), y: q.y + (nf.cy - f.cy) };
        return { x: r3(r.x), y: r3(r.y) };
      });
      if (box) this.setRotation(type, shape, na);
      const what = this.shapeLabel(type, shape);
      if (await this.commitGeometry(type, shape, before, `Change size or position of ${what}`)) {
        flashSaved(type === 'zone' ? 'zSaved' : 'stSaved');
      }
    }

    editHints(isLine) {
      return isLine
        ? `<div class="fp-hintline">Drag to move · drag a point to reshape · double-click the line to add a point · arrow keys nudge (<kbd>Shift</kbd> = 1 m) · <kbd>Ctrl</kbd>+<kbd>D</kbd> duplicates · <kbd>Ctrl</kbd>+<kbd>Z</kbd> undoes · <kbd>Del</kbd> removes the selected point or the whole item.</div>`
        : `<div class="fp-hintline">Drag to move · the square handles resize · the round handle turns it · arrow keys nudge (<kbd>Shift</kbd> = 1 m) · <kbd>Ctrl</kbd>+<kbd>D</kbd> duplicates · <kbd>Ctrl</kbd>+<kbd>Z</kbd> undoes. Turn on <b>Free shape</b> to drag single corners; double-click an edge to add one; <kbd>Del</kbd> removes the selected corner or the whole item.</div>`;
    }

    camerasLinkedTo(zoneId) {
      return (this.cameras || []).filter((c) => c.watch_zone_id === zoneId && !c.has_homography);
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
        <img id="fpCamThumb" class="fp-thumb" alt="Live frame from ${escapeHtml(cam.name)}"${thumbTransport() === 'local' ? ` src="${thumbSnapshotUrl(cam.camera_id)}"` : ''}>
        <div class="fp-thumb-bar" id="fpCamThumbBar" hidden>
          <span class="fp-thumb-note" id="fpCamThumbNote"></span>
          <button type="button" class="btn btn-secondary btn-xs" id="fpCamThumbRefresh" title="Take one new picture over the direct video connection">Take a new picture</button>
        </div>
        <div class="fp-kv">
          <span>Frame</span><b>${frame}</b>
          <span>Live tracks</span><b id="fpCamLive">${this.liveCountFor(cam.camera_id)}</b>
        </div>
        <div class="fp-zone-sub">Drag the marker to move it (outside the building too, e.g. the car park), drag the wedge tip to turn it, use the arrow keys, or set exact values.${cam.map_placed === false ? ' <b>Not placed yet</b>: it sits at a default spot.' : ''}</div>
        <div class="fp-field-row">
          <div class="fp-field"><label for="cX">X (metres)</label>
            <input id="cX" name="cameraX" type="number" step="0.1" value="${(+cam.floor_x || 0).toFixed(1)}"></div>
          <div class="fp-field"><label for="cY">Y (metres)</label>
            <input id="cY" name="cameraY" type="number" step="0.1" value="${(+cam.floor_y || 0).toFixed(1)}"></div>
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
          : `<div class="fp-cal-state is-uncal">Not calibrated. This camera counts people but cannot place them on the plan, so it adds nothing to the floor heatmap or the live map${cam.watch_zone_id ? '' : ', or to zone metrics until you link it to a zone below'}.</div>`}
        <div class="fp-field"><label for="cZone">Counts into zone${cam.has_homography ? ' (not used: calibrated)' : ''}</label>
          <select id="cZone" name="cameraZone">
            <option value="">— none —</option>
            ${this.zones.filter((z) => z.category !== 'EXCLUDED').map((z) => `<option value="${escapeHtml(z.id)}" ${z.id === cam.watch_zone_id ? 'selected' : ''}>${escapeHtml(z.name)}</option>`).join('')}
          </select>
          <span class="fp-hintline">${cam.has_homography
            ? 'Calibrated cameras place each person in the zone they stand in.'
            : 'Every person this camera sees is counted as a visit to this zone (the aisle or area it looks at). Calibrate later for exact positions.'}</span></div>
        <div class="fp-actions">
          <button class="btn btn-sm btn-primary" id="fpCalibrate">${cam.has_homography ? 'Recalibrate' : 'Calibrate'}</button>
          <button class="btn btn-secondary btn-sm" id="fpCamPlace">Place by click</button>
          <button class="btn btn-sm btn-danger" id="fpCamRemove">Remove</button>
          <button class="btn btn-secondary btn-sm" id="fpDone">Done</button>
        </div>`;

      const commit = async () => {
        const before = cameraSnap(cam);
        const num = (id, dflt) => { const v = parseFloat(panel.querySelector(`#${id}`).value); return Number.isFinite(v) ? v : dflt; };
        cam.floor_x = num('cX', +cam.floor_x || 0);
        cam.floor_y = num('cY', +cam.floor_y || 0);
        cam.azimuth_deg = (num('cAz', 0) % 360 + 360) % 360;
        cam.fov_deg = Math.max(10, Math.min(360, num('cFov', 85)));
        cam.floor_z = Math.max(0, Math.min(20, num('cZ', 3)));
        if (await this.commitCamera(cam, before, `Edit camera "${cam.name}"`)) flashSaved('cSaved');
      };
      ['cX', 'cY', 'cAz', 'cFov', 'cZ'].forEach((id) => panel.querySelector(`#${id}`).addEventListener('change', commit));
      panel.querySelector('#cZone').addEventListener('change', async (ev) => {
        try {
          const r = await this.post(`${API}/cameras/${encodeURIComponent(cam.camera_id)}/zone`, { zone_id: ev.target.value || null }, 'PUT');
          cam.watch_zone_id = r.watch_zone_id;
          this.setStatus(r.watch_zone_id ? `"${cam.name}" now counts into ${r.zone_name}.` : `"${cam.name}" no longer counts into a zone.`, 'ok');
          this.emitInspector();   // the calibration note depends on the link
          flashSaved('cSaved');
        } catch (e) {
          ev.target.value = cam.watch_zone_id || '';
          this.setStatus(`Could not link the camera: ${e.message}`, 'error');
        }
      });
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

      // Live thumbnail while this camera stays selected: a snapshot every 2 s on
      // the store network; in direct-video mode one still, refreshed on request.
      this._stopThumb();
      this._thumbRemote = null;
      this._initThumb(cam.camera_id);
      this._thumbTimer = setInterval(() => {
        const img = document.getElementById('fpCamThumb');
        const live = document.getElementById('fpCamLive');
        if (!img || !this.sel || this.sel.type !== 'camera' || this.sel.id !== cam.camera_id) { this._stopThumb(); return; }
        if (this.isVisible() && this._thumbRemote === false) img.src = thumbSnapshotUrl(cam.camera_id);
        if (live) live.textContent = this.liveCountFor(cam.camera_id);
      }, 2000);
    }

    _stopThumb() {
      if (this._thumbTimer) { clearInterval(this._thumbTimer); this._thumbTimer = null; }
      if (this._thumbImg && window.WebRtcLive) window.WebRtcLive.releaseStill(this._thumbImg);
      this._thumbImg = null;
    }

    /** Pick how the camera thumbnail is shown once the live transport is known. */
    _initThumb(cameraId) {
      const same = () => this.sel && this.sel.type === 'camera' && this.sel.id === cameraId;
      const go = (mode) => {
        const img = document.getElementById('fpCamThumb');
        const bar = document.getElementById('fpCamThumbBar');
        if (!img || !same()) return;
        this._thumbRemote = mode === 'webrtc';
        if (bar) bar.hidden = !this._thumbRemote;
        if (this._thumbRemote) {
          const btn = document.getElementById('fpCamThumbRefresh');
          if (btn) btn.addEventListener('click', () => this._grabThumb(cameraId, true));
          this._grabThumb(cameraId, false);
        } else if (!img.getAttribute('src')) {
          img.src = thumbSnapshotUrl(cameraId);
        }
      };
      if (window.WebRtcLive) window.WebRtcLive.mode().then(go); else go('local');
    }

    /**
     * Direct-video mode: one still picture over a direct WebRTC session. The
     * inspector re-renders on every edit, so the last still of the camera is
     * reused; a new one is taken only on first selection or on request.
     */
    async _grabThumb(cameraId, force) {
      const img = document.getElementById('fpCamThumb');
      const note = document.getElementById('fpCamThumbNote');
      const btn = document.getElementById('fpCamThumbRefresh');
      if (!img || !window.WebRtcLive) return;
      const kept = this._thumbStill;
      if (!force && kept && kept.cameraId === cameraId) {
        window.WebRtcLive.releaseStill(img);
        img._rtcStillUrl = URL.createObjectURL(kept.blob);
        img.src = img._rtcStillUrl;
        this._thumbImg = img;
        if (note) note.textContent = `Still picture from ${kept.at} (direct video).`;
        return;
      }
      if (btn) btn.disabled = true;
      if (note) { note.textContent = 'Taking a picture over direct video…'; note.classList.remove('is-err'); }
      const res = await window.WebRtcLive.loadStill(img, cameraId, { maxWidth: 640 });
      if (res.ok) this._thumbStill = { cameraId, blob: res.blob, at: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) };
      if (!document.body.contains(img) || !this.sel || this.sel.id !== cameraId) {
        window.WebRtcLive.releaseStill(img);
        return;
      }
      this._thumbImg = img;
      if (btn) btn.disabled = false;
      if (!note) return;
      if (res.ok) {
        note.textContent = `Still picture from ${this._thumbStill.at} (direct video).`;
      } else {
        const e = res.error || {};
        note.textContent = `${e.network ? 'Direct video not possible from this network' : (e.message || 'No picture')}. ${e.detail || ''}`.trim();
        note.classList.add('is-err');
      }
    }

    liveCountFor(cameraId) {
      const d = this.detections.find((x) => x.camera_id === cameraId);
      if (!d) return this.liveSupported === false ? 'n/a' : '—';
      return `${d.live_tracks}${d.calibrated ? '' : ' (not placeable)'}`;
    }

    /**
     * "Map size": the site is the whole map (building plus outdoor areas).
     * Space is added on any side with POST /layout/extend; "Fit to content"
     * shrink-wraps it; exact width/depth (right and bottom edges) via PUT.
     */
    resizeStore() {
      const panel = document.getElementById('zoneInspectorBody');
      const title = document.getElementById('fpInspectorTitle');
      if (!panel || !this.layout) return;
      this.flushNudge();
      this.sel = null; this.selVertex = null; this._stopThumb();
      this.updateToolButtons();
      if (title) title.textContent = 'Map size';
      const L = this.layout;
      const side = (key, label) => `<button type="button" class="btn btn-secondary btn-sm fp-site-btn fp-site-${key}" data-side="${key}">${label} <span data-amt>+5 m</span></button>`;
      panel.innerHTML = `
        <div class="fp-zone-sub">The map is the whole site: the building plus any outdoor areas such as the car park or loading dock. Add space on the side you need it; everything already drawn stays where it is.</div>
        <div class="fp-site-now">Map size now: <b>${fmt(L.width_m)} × ${fmt(L.height_m)} m</b></div>
        <div class="fp-field"><label for="sAmt">Metres to add on one side</label>
          <input id="sAmt" name="siteAmount" type="number" step="0.5" min="-500" max="500" value="5">
          <span class="fp-hintline">Then pick the side. A minus number removes empty space from that side.</span></div>
        <div class="fp-site-grid" role="group" aria-label="Add space on a side">
          ${side('top', 'Top')}
          ${side('left', 'Left')}
          <div class="fp-site-box" aria-hidden="true">${fmt(L.width_m)} × ${fmt(L.height_m)} m</div>
          ${side('right', 'Right')}
          ${side('bottom', 'Bottom')}
        </div>
        <div class="fp-actions" style="margin-top:0">
          <button type="button" class="btn btn-secondary btn-sm" id="sAll">Add 5 m all round</button>
          <button type="button" class="btn btn-secondary btn-sm" id="sFit" title="Shrink or grow the map to what is drawn on it, plus 2 m">Fit to content</button>
        </div>
        <div class="fp-saved" id="sSaved"></div>
        <div class="fp-site-outside" id="sOutside" hidden></div>
        <details class="fp-site-exact">
          <summary>Name and exact size</summary>
          <div class="fp-field"><label for="sName">Floor name</label>
            <input id="sName" name="storeName" type="text" value="${escapeHtml(L.name || '')}"></div>
          <div class="fp-field-row">
            <div class="fp-field"><label for="sW">Width (m)</label>
              <input id="sW" name="storeWidth" type="number" step="0.5" min="1" max="1000" value="${L.width_m}"></div>
            <div class="fp-field"><label for="sH">Depth (m)</label>
              <input id="sH" name="storeHeight" type="number" step="0.5" min="1" max="1000" value="${L.height_m}"></div>
          </div>
          <span class="fp-hintline">Width and depth move the right and bottom edges. Shrinking past something on the map is refused, so nothing is cut off by accident.</span>
          <div class="fp-actions">
            <button type="button" class="btn btn-sm btn-primary" id="sApply">Apply</button>
          </div>
        </details>
        <div class="fp-actions">
          <button type="button" class="btn btn-secondary btn-sm" id="sCancel">Done</button>
        </div>`;

      const amount = () => {
        const v = parseFloat(panel.querySelector('#sAmt').value);
        return Number.isFinite(v) ? Math.max(-500, Math.min(500, v)) : 0;
      };
      const syncAmt = () => {
        const v = amount();
        panel.querySelectorAll('[data-amt]').forEach((el) => { el.textContent = `${v >= 0 ? '+' : ''}${fmt(v)} m`; });
      };
      const done = (ok) => {
        const saved = document.getElementById('sSaved');
        if (saved && ok && this.layout) saved.textContent = `Map is now ${fmt(this.layout.width_m)} × ${fmt(this.layout.height_m)} m.`;
      };
      const rerender = () => { if (this.sel === null) this.resizeStore(); };
      panel.querySelector('#sAmt').addEventListener('input', syncAmt);
      panel.querySelectorAll('[data-side]').forEach((b) => b.addEventListener('click', async () => {
        const v = amount();
        if (!v) { this.setStatus('Type how many metres to add first.', 'error'); return; }
        if (this._siteBusy) { this._siteBusyNotice(); return; }
        this._lastOutside = null;
        const ch = await this.extendMap({ [`${b.dataset.side}_m`]: v });
        if (ch) { rerender(); done(true); } else this._showOutside(panel);
      }));
      panel.querySelector('#sAll').addEventListener('click', async () => {
        const ch = await this.extendMap({ left_m: 5, top_m: 5, right_m: 5, bottom_m: 5 });
        if (ch) { rerender(); done(true); }
      });
      panel.querySelector('#sFit').addEventListener('click', async () => {
        if (await this.fitMapToContent(2)) { rerender(); done(true); }
      });
      panel.querySelector('#sApply').addEventListener('click', async () => {
        const name = panel.querySelector('#sName').value;
        const w = parseFloat(panel.querySelector('#sW').value), h = parseFloat(panel.querySelector('#sH').value);
        try {
          await this.saveStoreSize(name, w, h);
          rerender(); done(true);
          this.setStatus(`Map set to ${fmt(this.layout.width_m)} × ${fmt(this.layout.height_m)} m.`, 'ok');
        } catch (e) {
          if (e.detail && e.detail.code === 'items_outside') {
            this._lastOutside = e.detail.items;
            this._showOutside(panel, async () => {
              try {
                await this.saveStoreSize(name, w, h, true);
                rerender(); done(true);
              } catch (e2) { this.setStatus(`Could not resize: ${e2.message}`, 'error'); }
            });
            this.setStatus('Not resized: some items would be cut off. See the list in the panel.', 'error');
          } else {
            this.setStatus(`Could not resize: ${e.message}`, 'error');
          }
        }
      });
      panel.querySelector('#sCancel').addEventListener('click', () => this.select(null));
      // Rendered while a change is in flight: its buttons start disabled.
      this._syncSiteButtons();
    }

    /** The items a refused shrink would have cut off, with an optional "do it anyway". */
    _showOutside(panel, force) {
      const box = panel.querySelector('#sOutside');
      const items = this._lastOutside || [];
      if (!box || !items.length) return;
      box.hidden = false;
      box.innerHTML = `
        <div><b>Not changed.</b> ${items.length === 1 ? 'This item' : `These ${items.length} items`} would end up outside the map:</div>
        <ul>${items.slice(0, 8).map((i) => `<li>${escapeHtml(describeItem(i))}</li>`).join('')}${items.length > 8 ? `<li>and ${items.length - 8} more</li>` : ''}</ul>
        <div class="fp-actions" style="margin-top:4px">
          <button type="button" class="btn btn-secondary btn-xs" id="sShowFirst">Show ${items.length === 1 ? 'it' : 'the first'}</button>
          ${force ? '<button type="button" class="btn btn-danger btn-xs" id="sForce">Shrink anyway (moves them inside)</button>' : ''}
        </div>`;
      box.querySelector('#sShowFirst').addEventListener('click', () => {
        const i = items[0];
        if (i.type === 'camera') this.focusCamera(i.id); else this.selectShape(i.type, i.id);
      });
      const f = box.querySelector('#sForce');
      if (f) f.addEventListener('click', force);
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
          <span class="fp-step-n">${done ? (ico('check', { size: 'sm', label: 'Done' }) || 'Done') : n}</span>
          <span class="fp-step-text">${text}<small>${sub}</small></span>
          <button class="btn btn-xs ${done ? 'btn-secondary' : 'btn-primary'}" data-fn="${fn}">${btn}</button>
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
          ${step(1, s.structures > 0, 'Draw the room outline', 'Press and drag across the shop floor. Add outdoor areas (car park, loading dock) the same way.', 'Draw room', 'room')}
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
          Use <b>Room</b> to drag out the shop floor, <b>Outdoor area</b> for the car park, <b>Wall</b>/<b>Shelf</b>/<b>Door</b> for the building, <b>Zone</b> for the areas to measure.
          Scroll or pinch to zoom, drag the background to pan. <b>Map size</b> adds space on any side.`;
      }
    }

    /**
     * The camera list under the plan: a one-line summary that opens into one
     * chip per camera. It sits below the canvas, never over it, scrolls inside
     * a bounded height, and starts closed when there are many cameras.
     */
    renderLiveDom() {
      const host = document.getElementById('fpCamBadges');
      const toggle = document.getElementById('fpCamListToggle');
      const summary = document.getElementById('fpCamListSummary');
      const live = document.getElementById('fpLive');
      if (host) {
        let html;
        if (this.liveSupported === false) {
          html = '<span class="fp-badge fp-badge-note">live map endpoint not available on this server</span>';
        } else {
          const parts = this.detections.map((d) => {
            // A camera turned off says "off" in a neutral colour, not a fault.
            const off = d.status === 'DISABLED';
            const cls = off ? 'fp-badge-disabled' : d.status !== 'ONLINE' ? 'fp-badge-off' : d.calibrated ? 'fp-badge-cal' : 'fp-badge-uncal';
            const state = off ? 'off' : d.status !== 'ONLINE' ? String(d.status || 'offline').toLowerCase() : d.calibrated ? 'calibrated' : 'uncalibrated';
            return `<button type="button" class="fp-badge" data-cam="${escapeHtml(d.camera_id)}" title="Select this camera on the map">
              <span class="fp-badge-swatch" style="background:${camColorFor(this.cameras, d.camera_id)}"></span>
              ${escapeHtml(d.camera_name || d.camera_id)} · <b>${d.live_tracks}</b> live · <span class="${cls}">${state}</span></button>`;
          });
          if (this.uncalibratedTracks > 0) {
            parts.push(`<span class="fp-badge fp-badge-note">${this.uncalibratedTracks} ${this.uncalibratedTracks === 1 ? 'person' : 'people'} detected on uncalibrated cameras (not placeable)</span>`);
          }
          html = parts.join('');
        }
        if (html !== this._camListHtml) {
          this._camListHtml = html;
          host.innerHTML = html;
          host.querySelectorAll('[data-cam]').forEach((b) => b.addEventListener('click', () => this.selectCamera(b.dataset.cam)));
        }
        const count = this.liveSupported === false ? this.cameras.length : this.detections.length;
        const open = this.camListOpen === null ? count <= CAM_LIST_AUTO_OPEN : this.camListOpen;
        host.hidden = !open;
        if (toggle) {
          toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
          toggle.title = open ? 'Hide the camera list' : 'Show every camera';
        }
        if (summary) {
          const calibrated = this.detections.filter((d) => d.calibrated).length;
          const people = this.detections.reduce((n, d) => n + (Number.isFinite(d.live_tracks) ? d.live_tracks : 0), 0);
          const piled = this.cameraFan().size;
          const bits = [`${count} camera${count === 1 ? '' : 's'}`];
          if (this.liveSupported !== false) bits.push(`${calibrated} calibrated`, `${people} ${people === 1 ? 'person' : 'people'} in view`);
          if (piled) bits.push(`${piled} stacked on one spot (spread out on the map until dragged)`);
          summary.textContent = bits.join(' · ');
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
          : this.mode === MODE.VIEW ? 'select' : this.mode.replace('-', ' ');
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
      // Own row in the layer bar (#fpHeatBar); falls back to after the checkbox.
      const bar = document.getElementById('fpHeatBar');
      const cb = document.getElementById('layerHeatmap');
      const anchor = cb ? (cb.closest('label') || cb) : null;
      if ((!bar && !anchor) || document.getElementById('fpHeatmapKind')) return;
      const group = document.createElement('span');
      group.id = 'fpHeatmapKind';
      group.className = 'fp-heat-kinds';
      group.setAttribute('role', 'group');
      group.setAttribute('aria-label', 'Heatmap: what to show');
      HEATMAP_KINDS.forEach((k) => {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'btn btn-secondary btn-xs fp-heat-kind';
        b.dataset.heatmapKind = k.kind;
        b.textContent = k.label;
        b.title = k.title;
        b.addEventListener('click', () => this.setHeatmapKind(k.kind));
        group.appendChild(b);
      });
      // Time range: Today (live) or a sum of recorded hourly snapshots.
      const ranges = document.createElement('span');
      ranges.id = 'fpHeatRange';
      ranges.className = 'fp-heat-kinds';
      ranges.setAttribute('role', 'group');
      ranges.setAttribute('aria-label', 'Heatmap: time range');
      HEATMAP_RANGES.forEach((r) => {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'btn btn-secondary btn-xs fp-heat-kind';
        b.dataset.heatmapRange = r.range;
        b.textContent = r.label;
        b.title = r.title;
        b.addEventListener('click', () => this.setHeatmapRange(r.range));
        ranges.appendChild(b);
      });
      const legend = document.createElement('span');
      legend.className = 'fp-heat-legend';
      legend.title = 'Colour scale of the heatmap';
      legend.innerHTML = '<span>Quiet</span><span class="fp-heat-ramp" aria-hidden="true"></span><span>Busy</span>';
      const note = document.createElement('span');
      note.id = 'fpHeatmapNote';
      note.className = 'fp-heat-note';
      if (bar) { bar.appendChild(group); bar.appendChild(ranges); bar.appendChild(legend); bar.appendChild(note); }
      else { anchor.insertAdjacentElement('afterend', group); group.insertAdjacentElement('afterend', ranges); ranges.appendChild(note); }
      this._syncHeatmapKindButtons();
    }

    _syncHeatmapKindButtons() {
      document.querySelectorAll('#fpHeatmapKind [data-heatmap-kind]').forEach((b) => {
        const on = b.dataset.heatmapKind === this.heatmapKind;
        b.classList.toggle('btn-primary', on);
        b.classList.toggle('btn-secondary', !on);
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      document.querySelectorAll('#fpHeatRange [data-heatmap-range]').forEach((b) => {
        const on = b.dataset.heatmapRange === this.heatmapRange;
        b.classList.toggle('btn-primary', on);
        b.classList.toggle('btn-secondary', !on);
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
    }

    /**
     * URL of the heatmap for a range, or null when the last recorded-range
     * fetch is still fresh (history changes at most once an hour, so it is
     * re-read every few minutes rather than every metrics tick).
     * Dates are sent without an offset: the server reads them as store time.
     */
    _heatmapUrl(range, kind) {
      // Heatmaps not in the iKorex plan (fleet.js / 403 feature_disabled): do
      // not ask every few seconds for something the box refuses.
      const fleet = window.edgeFleet && typeof window.edgeFleet.status === 'function' ? window.edgeFleet.status() : null;
      if ((fleet && fleet.features && fleet.features.heatmaps === false)
          || (this._heatmapRefusedAt && Date.now() - this._heatmapRefusedAt < HEATMAP_REFUSED_RETRY_MS)) {
        this.heatmap = null;
        return null;
      }
      const k = encodeURIComponent(kind);
      if (range === 'today' || !HEATMAP_RANGES.some((r) => r.range === range)) {
        return `/api/v1/analytics/heatmaps?resolution_w=60&resolution_h=40&kind=${k}`;
      }
      if (this._heatmapFetchKey === `${range}|${kind}` && Date.now() - this._heatmapFetchedAt < HEATMAP_HISTORY_REFRESH_MS) return null;
      const pad = (n) => String(n).padStart(2, '0');
      const day = (offset) => {
        const d = new Date();
        d.setHours(12, 0, 0, 0);
        d.setDate(d.getDate() + offset);
        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T00:00:00`;
      };
      let from, to;
      if (range === 'yesterday') { from = day(-1); to = day(0); }
      else if (range === '7d') { from = day(-6); to = day(1); }
      else { from = day(-29); to = day(1); }
      // Hourly snapshots are kept HEATMAP_HOURLY_RETENTION_DAYS (35 by default), so 30 days sum hours.
      return `/api/v1/analytics/heatmaps/aggregate?space=floor&kind=${k}&bucket=hour&from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`;
    }

    setHeatmapRange(range) {
      if (!HEATMAP_RANGES.some((r) => r.range === range)) return;
      this.heatmapRange = range;
      try { window.localStorage.setItem(HEATMAP_RANGE_KEY, range); } catch (_) { /* not persisted */ }
      this.heatmap = null;
      this.heatmapMessage = null;
      this._heatmapFetchKey = null;
      this._heatmapFetchedAt = 0;
      this._syncHeatmapKindButtons();
      const note = document.getElementById('fpHeatmapNote');
      if (note) note.textContent = 'loading…';
      this.refreshMetrics();
    }

    /** Busiest spot in plain words: which area it is in and how much activity. */
    _heatmapPeakPlace(hm) {
      try {
        const m = hm.density_matrix, gw = hm.grid_width, gh = hm.grid_height;
        if (!m || !gw || !gh || !this.layout) return '';
        let best = -1, bx = 0, by = 0;
        for (let y = 0; y < gh; y++) for (let x = 0; x < gw; x++) if (m[y][x] > best) { best = m[y][x]; bx = x; by = y; }
        const W = hm.width_m > 0 ? hm.width_m : this.layout.width_m, H = hm.height_m > 0 ? hm.height_m : this.layout.height_m;
        const px = ((bx + 0.5) / gw) * W, py = ((by + 0.5) / gh) * H;
        const inside = (poly) => {
          let c = false;
          for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
            const a = poly[i], b = poly[j];
            if (((a.y > py) !== (b.y > py)) && (px < ((b.x - a.x) * (py - a.y)) / ((b.y - a.y) || 1e-9) + a.x)) c = !c;
          }
          return c;
        };
        const zone = (this.zones || []).find((z) => Array.isArray(z.polygon) && z.polygon.length > 2 && inside(z.polygon));
        return zone ? `in ${zone.name}` : `near ${px.toFixed(0)} m, ${py.toFixed(0)} m`;
      } catch (_) { return ''; }
    }

    _renderHeatmapNote(hm) {
      const note = document.getElementById('fpHeatmapNote');
      if (!note) return;
      const rangeInfo = HEATMAP_RANGES.find((r) => r.range === this.heatmapRange) || HEATMAP_RANGES[0];
      const recorded = this.heatmapRange !== 'today';
      if (hm && hm.observed) {
        const peak = hm.peak_value !== undefined ? hm.peak_value : hm.peak_count;
        const where = this._heatmapPeakPlace(hm);
        const what = this.heatmapKind === 'dwell' ? 'busiest stopping spot' : this.heatmapKind === 'interaction' ? 'most shelf reaches' : 'busiest spot';
        note.textContent = `${rangeInfo.label} · ${what}${where ? ' ' + where : ''}`;
        note.title = recorded
          ? `Peak ${peak} ${hm.unit || ''} per spot over ${hm.recorded_hours} recorded hour(s) (${hm.snapshots_used} hourly heatmaps); hours not recorded are left out`
          : `Peak ${peak} ${hm.unit || ''} from ${hm.samples} measurements today`;
      } else if (recorded) {
        // Aggregate with nothing to draw: nothing recorded, or recorded but nobody seen.
        note.textContent = hm && hm.snapshots_used
          ? `${rangeInfo.label} · ${hm.recorded_hours} recorded hour(s), nobody seen`
          : `${rangeInfo.label} · Nothing recorded in this period`;
        note.title = (hm && hm.message) || note.textContent;
      } else {
        note.textContent = (hm && hm.message) || 'Nothing recorded today yet';
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

      // Camera and person labels are queued while drawing and placed last,
      // so they avoid each other, every marker and the area names (flushLabels).
      this._labelQueue = [];
      this._markers = [];
      this._fixedText = [];
      this.drawFloor();
      if (this.showLayers.heatmap) this.drawHeatmap();
      if (this.showLayers.structures) this.drawStructures();
      if (this.showLayers.zones) this.drawZones();
      this.drawSelectionHandles();
      this.drawDraft();
      if (this.showLayers.cameras) this.drawCameras();
      if (this.showLayers.persons) this.drawPersons();
      this.flushLabels();
      this.drawCalOverlay();
      this.drawMeasure();
      this.drawDropPreview();
      this.drawDragLabel();
      this.drawScaleBar();
    }

    /** Queue a text label anchored on a marker at screen (x, y) of radius r. */
    queueLabel(text, x, y, r, opts) {
      if (!this._labelQueue) return;
      this._labelQueue.push(Object.assign({ text: String(text), x, y, r, prio: 1, required: false,
        font: '600 10px system-ui, sans-serif', color: T.text2, prefer: 'up' }, opts || {}));
    }

    /**
     * Greedy placement: labels in priority order try positions around their
     * marker (above, below, right, left, diagonals, then further out) and take
     * the first that overlaps no marker and no label already placed. A label
     * marked required (camera names) is drawn at its preferred spot when all
     * are taken; optional ones (person ids) are left out rather than piled up.
     */
    flushLabels() {
      const queue = this._labelQueue || [];
      this._labelQueue = null;
      if (!queue.length) return;
      const ctx = this.ctx;
      const W = this.canvas.clientWidth, H = this.canvas.clientHeight;
      const placed = [];
      const obstacles = (this._markers || []).map((m) => ({ x: m.x - m.r - 1, y: m.y - m.r - 1, w: 2 * m.r + 2, h: 2 * m.r + 2 }))
        .concat(this._fixedText || []);
      const hit = (a, b) => a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
      ctx.save();
      queue.sort((a, b) => a.prio - b.prio);
      queue.forEach((L) => {
        ctx.font = L.font;
        const w = Math.ceil(ctx.measureText(L.text).width) + 6, h = 13;
        const d = L.r + 3;
        const spots = {
          up: [L.x - w / 2, L.y - d - h], down: [L.x - w / 2, L.y + d], right: [L.x + d + 1, L.y - h / 2],
          left: [L.x - d - 1 - w, L.y - h / 2], ur: [L.x + d - 2, L.y - d - h + 2], ul: [L.x - d + 2 - w, L.y - d - h + 2],
          dr: [L.x + d - 2, L.y + d - 2], dl: [L.x - d + 2 - w, L.y + d - 2],
          up2: [L.x - w / 2, L.y - d - 2 * h - 2], down2: [L.x - w / 2, L.y + d + h + 2],
        };
        const order = L.prefer === 'down'
          ? ['down', 'up', 'right', 'left', 'dr', 'dl', 'ur', 'ul', 'down2', 'up2']
          : ['up', 'down', 'right', 'left', 'ur', 'ul', 'dr', 'dl', 'up2', 'down2'];
        let box = null;
        for (const k of order) {
          const [bx, by] = spots[k];
          const b = { x: bx, y: by, w, h };
          if (b.x < 0 || b.y < 0 || b.x + b.w > W || b.y + b.h > H) continue;
          if (placed.some((p) => hit(b, p)) || obstacles.some((o) => hit(b, o))) continue;
          box = b;
          break;
        }
        if (!box) {
          if (!L.required) return;
          const [bx, by] = spots[order[0]];
          // Kept inside the canvas: a pile at the plan's top edge put its
          // label above y=0, where it was cut off.
          box = { x: Math.max(0, Math.min(bx, W - w)), y: Math.max(0, Math.min(by, H - h)), w, h };
        }
        placed.push(box);
        // Plate in the plan colour keeps the text legible over zones and the heatmap.
        ctx.globalAlpha = 0.82;
        ctx.fillStyle = T.bg;
        ctx.fillRect(box.x, box.y, box.w, box.h);
        ctx.globalAlpha = 1;
        ctx.fillStyle = L.color;
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        ctx.fillText(L.text, box.x + 3, box.y + h / 2 + 0.5);
      });
      ctx.restore();
      this.lastLabelBoxes = placed;   // read by the UI tests (no overlap between labels)
      this.lastFixedText = this._fixedText || [];
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
      // Recorded aggregates carry the extent they were recorded with (the store
      // may have been resized since); the live heatmap always matches the layout.
      const W = this.heatmap.width_m > 0 ? this.heatmap.width_m : this.layout.width_m;
      const H = this.heatmap.height_m > 0 ? this.heatmap.height_m : this.layout.height_m;
      const cw = (W / gw) * this.scale, ch = (H / gh) * this.scale;
      ctx.save();
      // Additive glow on the dark plan; multiply keeps it visible on the light one.
      ctx.globalCompositeOperation = T.heatBlend;
      for (let gy = 0; gy < gh; gy++) {
        for (let gx = 0; gx < gw; gx++) {
          const v = m[gy][gx];
          if (v <= 0.02) continue;
          const s = this.toScreen((gx / gw) * W, (gy / gh) * H);
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
      // Outdoor areas lie under everything, then rooms, fixtures, walls and doors.
      const order = { OUTDOOR: -1, ROOM: 0, OBSTACLE: 1, SHELF: 2, COUNTER: 2, WALL: 3, DOOR: 4 };
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
          } else if (st.kind === 'OUTDOOR') {
            // Open ground: a soft fill and a dashed edge, no hatching.
            ctx.fillStyle = hexToRgba(color, selected ? 0.24 : hovered ? 0.2 : 0.14);
            ctx.fill();
            ctx.setLineDash([8, 5]);
            ctx.lineWidth = selected ? 2.5 : 1.5;
            ctx.strokeStyle = selected ? T.select : color;
            ctx.stroke();
            ctx.setLineDash([]);
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
          const big = st.kind === 'ROOM' || st.kind === 'OUTDOOR';
          ctx.fillStyle = big ? hexToRgba(color, 0.9) : T.text;
          ctx.font = big ? '800 12px system-ui, sans-serif' : '600 10px system-ui, sans-serif';
          ctx.textAlign = 'center';
          if (big) {
            const tl = this.toScreen(Math.min(...poly.map((p) => p.x)), Math.min(...poly.map((p) => p.y)));
            ctx.textAlign = 'left';
            ctx.fillText(st.name, tl.x + 6, tl.y + 14);
          } else if (POLYLINE_KINDS.has(st.kind)) {
            ctx.fillText(st.name, s.x, s.y - 6);
          } else {
            // Shelves and counters: along the fixture's long side, shrunk to
            // fit, or left out (the name is in the inspector) when it cannot.
            this.drawFittedLabel(poly, [{ text: st.name, weight: 600 }], 10);
          }
        }
        ctx.restore();
      });
    }

    /**
     * Text lines centred in a polygon, along its longer side on screen (turned
     * 90° in tall, narrow shapes such as aisles and shelf runs), at the largest
     * size from ``base`` down to 7 px that fits. Returns false (and draws
     * nothing) when even the smallest size does not fit. Every drawn line is
     * registered as fixed text so camera and person labels avoid it.
     */
    drawFittedLabel(poly, lines, base, required = false) {
      if (!lines.length || poly.length < 3) return false;
      const ctx = this.ctx;
      const pts = poly.map((p) => this.toScreen(p.x, p.y));
      const minX = Math.min(...pts.map((p) => p.x)), maxX = Math.max(...pts.map((p) => p.x));
      const minY = Math.min(...pts.map((p) => p.y)), maxY = Math.max(...pts.map((p) => p.y));
      const w = maxX - minX, h = maxY - minY;
      const vertical = h > w * 1.3;
      const along = (vertical ? h : w) - 6, across = (vertical ? w : h) - 2;
      const c = this.toScreen(polygonCentroid(poly).x, polygonCentroid(poly).y);
      const fontOf = (L, px) => `${L.weight || 600} ${px}px ${L.mono ? 'ui-monospace, monospace' : 'system-ui, sans-serif'}`;
      for (let px = base; px >= 7; px--) {
        const lineH = px + 2;
        // Optional lines (all but the first) are dropped before shrinking further.
        for (let n = lines.length; n >= 1; n--) {
          const use = lines.slice(0, n);
          if (use.length * lineH > across) continue;
          const widths = use.map((L) => { ctx.font = fontOf(L, px); return ctx.measureText(L.text).width; });
          if (Math.max(...widths) > along) continue;
          ctx.save();
          ctx.translate(c.x, c.y);
          if (vertical) ctx.rotate(-Math.PI / 2);
          ctx.textAlign = 'center';
          ctx.textBaseline = 'middle';
          const top = -((use.length - 1) * lineH) / 2;
          use.forEach((L, i) => {
            ctx.font = fontOf(L, px);
            ctx.fillStyle = L.color || T.text;
            ctx.fillText(L.text, 0, top + i * lineH);
          });
          ctx.restore();
          const fixed = this._fixedText;
          if (fixed) {
            const bw = vertical ? use.length * lineH : Math.max(...widths);
            const bh = vertical ? Math.max(...widths) : use.length * lineH;
            fixed.push({ x: c.x - bw / 2 - 2, y: c.y - bh / 2 - 2, w: bw + 4, h: bh + 4 });
          }
          return true;
        }
        if (!required && px <= 7) break;
      }
      return false;
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
          const lines = [{ text: z.name, weight: 600, color: T.text }];
          if (m && m.observed) lines.push({ text: `${m.visits} visits · ${m.avg_dwell_seconds || 0}s`, weight: 500, color: zc, mono: true });
          if (!this.drawFittedLabel(poly, lines, 11, true)) {
            // Too small to hold the text: keep the old centred label so a zone is never anonymous.
            ctx.fillStyle = T.text;
            ctx.font = '600 11px system-ui, sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText(z.name, s.x, s.y);
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
      const handles = this.frameHandles();
      if (handles.length) {
        // The box the resize handles belong to, turned with the shape.
        const f = this.shapeFrame(this.sel.type, shape);
        const corners = [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([sx, sy]) => {
          const w = frameToWorld(f, sx * f.w / 2, sy * f.h / 2);
          return this.toScreen(w.x, w.y);
        });
        ctx.beginPath();
        corners.forEach((c, i) => (i ? ctx.lineTo(c.x, c.y) : ctx.moveTo(c.x, c.y)));
        ctx.closePath();
        ctx.setLineDash([4, 3]);
        ctx.strokeStyle = T.select;
        ctx.lineWidth = 1;
        ctx.stroke();
        ctx.setLineDash([]);
      }
      // Corners (vertices): small dots; draggable with "Free shape" on.
      shape.polygon.forEach((p, i) => {
        const s = this.toScreen(p.x, p.y);
        const active = i === this.selVertex;
        ctx.beginPath();
        ctx.arc(s.x, s.y, active ? 5 : (handles.length && !this.freeShape ? 2.5 : 4), 0, Math.PI * 2);
        ctx.fillStyle = active ? T.select : T.marker;
        ctx.fill();
        ctx.strokeStyle = T.outline;
        ctx.lineWidth = 1.5;
        ctx.stroke();
      });
      const hr = this._coarse ? 7 : 5;
      handles.forEach((h) => {
        if (h.kind === 'rotate') {
          ctx.beginPath(); ctx.moveTo(h.from.x, h.from.y); ctx.lineTo(h.x, h.y);
          ctx.strokeStyle = T.select; ctx.lineWidth = 1; ctx.stroke();
          ctx.beginPath(); ctx.arc(h.x, h.y, hr + 2, 0, Math.PI * 2);
          ctx.fillStyle = T.marker; ctx.fill();
          ctx.strokeStyle = T.select; ctx.lineWidth = 2; ctx.stroke();
          // A small turning arc inside the handle.
          ctx.beginPath(); ctx.arc(h.x, h.y, hr - 2, -Math.PI * 0.9, Math.PI * 0.4);
          ctx.lineWidth = 1.5; ctx.stroke();
          return;
        }
        ctx.fillStyle = T.marker;
        ctx.strokeStyle = T.select;
        ctx.lineWidth = 1.5;
        ctx.fillRect(h.x - hr, h.y - hr, hr * 2, hr * 2);
        ctx.strokeRect(h.x - hr, h.y - hr, hr * 2, hr * 2);
      });
      ctx.restore();
    }

    /** Size / angle next to the pointer while drawing, resizing or turning. */
    drawDragLabel() {
      if (!this._dragLabel || !this.cursorWorld) return;
      const s = this.toScreen(this.cursorWorld.x, this.cursorWorld.y);
      this.drawPlateText(this._dragLabel, s.x + 14, s.y + 18, T.text);
    }

    drawPlateText(text, x, y, color) {
      const ctx = this.ctx;
      ctx.save();
      ctx.font = '700 12px system-ui, sans-serif';
      const w = Math.ceil(ctx.measureText(text).width) + 12, h = 20;
      const W = this.canvas.clientWidth, H = this.canvas.clientHeight;
      const bx = Math.max(2, Math.min(x, W - w - 2)), by = Math.max(2, Math.min(y - h / 2, H - h - 2));
      ctx.globalAlpha = 0.92;
      ctx.fillStyle = T.bg;
      roundRect(ctx, bx, by, w, h, 4);
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.strokeStyle = T.outline;
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.fillStyle = color || T.text;
      ctx.textAlign = 'left';
      ctx.textBaseline = 'middle';
      ctx.fillText(text, bx + 6, by + h / 2 + 0.5);
      ctx.restore();
    }

    drawMeasure() {
      if (this.mode !== MODE.MEASURE || !this.measure) return;
      const a = this.measure.a;
      let b = this.measure.b;
      if (!b && this.cursorWorld) b = this.shiftDown ? orthoTo(a, this.cursorWorld) : this.cursorWorld;
      if (!a || !b) return;
      const ctx = this.ctx;
      const sa = this.toScreen(a.x, a.y), sb = this.toScreen(b.x, b.y);
      ctx.save();
      ctx.strokeStyle = T.select;
      ctx.lineWidth = 2;
      if (!this.measure.b) ctx.setLineDash([6, 4]);
      ctx.beginPath(); ctx.moveTo(sa.x, sa.y); ctx.lineTo(sb.x, sb.y); ctx.stroke();
      ctx.setLineDash([]);
      [sa, sb].forEach((p) => {
        ctx.beginPath(); ctx.arc(p.x, p.y, 4, 0, Math.PI * 2);
        ctx.fillStyle = T.select; ctx.fill();
        ctx.strokeStyle = T.outline; ctx.lineWidth = 1.5; ctx.stroke();
      });
      ctx.restore();
      const d = Math.hypot(b.x - a.x, b.y - a.y);
      this.drawPlateText(`${d.toFixed(2)} m`, (sa.x + sb.x) / 2 + 8, (sa.y + sb.y) / 2 - 12, T.text);
    }

    /** Ghost camera marker while a camera card is dragged over the map (camera gallery). */
    drawDropPreview() {
      const p = this.dropPreview;
      if (!p) return;
      const ctx = this.ctx;
      const s = this.toScreen(p.x, p.y);
      const inside = this.insideSite(p.x, p.y);
      ctx.save();
      ctx.beginPath(); ctx.arc(s.x, s.y, 12, 0, Math.PI * 2);
      ctx.fillStyle = hexToRgba(inside ? T.select : T.warn, 0.25); ctx.fill();
      ctx.setLineDash([4, 3]);
      ctx.strokeStyle = inside ? T.select : T.warn; ctx.lineWidth = 2; ctx.stroke();
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.moveTo(s.x - 6, s.y); ctx.lineTo(s.x + 6, s.y);
      ctx.moveTo(s.x, s.y - 6); ctx.lineTo(s.x, s.y + 6);
      ctx.lineWidth = 1.5; ctx.stroke();
      ctx.restore();
      this.drawPlateText(inside ? `${p.x.toFixed(1)}, ${p.y.toFixed(1)} m` : 'Past the edge: the map can grow to fit',
        s.x + 16, s.y - 16, inside ? T.text : T.warn);
    }

    drawDraft() {
      if (this.mode !== MODE.DRAW) return;
      const ctx = this.ctx;
      const color = ink(this.draftKind === 'ZONE' ? CATEGORY_COLORS.ENTRANCE : (KIND_COLORS[this.draftKind] || CATEGORY_COLORS.ENTRANCE));
      const preview = this.draftPreviewPoint();
      const d = this.dragging;
      if (d && d.kind === 'rect') {
        const a = this.toScreen(d.start.x, d.start.y), b = this.toScreen(d.end.x, d.end.y);
        ctx.save();
        ctx.fillStyle = hexToRgba(color, 0.15);
        ctx.fillRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(b.x - a.x), Math.abs(b.y - a.y));
        ctx.setLineDash([6, 4]);
        ctx.strokeStyle = color;
        ctx.lineWidth = 2;
        ctx.strokeRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(b.x - a.x), Math.abs(b.y - a.y));
        ctx.restore();
        return;
      }
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
      const pileLabels = new Map();   // one note per pile of fanned-out cameras
      this.cameras.forEach((cam) => {
        const s = this.cameraScreenPos(cam);
        const hovered = this.isHovered('camera', cam.camera_id);
        const selected = this.isSelected('camera', cam.camera_id);
        // A fanned-out marker is not where the camera hangs: its wedge and
        // name are drawn only while it is pointed at or selected.
        const fanned = !!s.fanned;
        const detailed = !fanned || hovered || selected;
        if (fanned) {
          const at = this.toScreen(cam.floor_x, cam.floor_y);
          const k = `${Math.round(at.x)}:${Math.round(at.y)}`;
          const top = Math.min(s.y, pileLabels.has(k) ? pileLabels.get(k).top : Infinity);
          pileLabels.set(k, { x: at.x, top, size: s.fanned });
        }
        const online = cam.status === 'ONLINE';
        const color = ink(camColorFor(this.cameras, cam.camera_id));
        const reach = this.cameraReachPx();
        const half = ((cam.fov_deg || 85) * Math.PI) / 180 / 2;
        const bearing = ((cam.azimuth_deg || 0) - 90) * Math.PI / 180;

        ctx.save();
        if (detailed) {
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
        }

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

        const r = selected ? 10 : 8;
        if (this._markers) this._markers.push({ x: s.x, y: s.y, r });
        if (this.showLayers.labels && detailed) {
          const name = (cam.name || cam.camera_id);
          this.queueLabel(name.length > 22 ? `${name.slice(0, 21)}…` : name, s.x, s.y, r,
            { prio: 0, required: true, color: T.text2 });
          if (!cam.has_homography) this.queueLabel('uncalibrated', s.x, s.y, r, { prio: 1, required: true, color: T.warn, prefer: 'down' });
          else if (!online) this.queueLabel(String(cam.status || 'offline').toLowerCase(), s.x, s.y, r, { prio: 1, required: true, color: T.danger, prefer: 'down' });
        }
        ctx.restore();
      });
      if (this.showLayers.labels) {
        pileLabels.forEach((pile) => {
          this.queueLabel(`${pile.size} cameras share this spot: drag each to where it hangs`, pile.x, pile.top - 4, 10,
            { prio: 0, required: true, color: T.warn });
        });
      }
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
        if (this._markers) this._markers.push({ x: s.x, y: s.y, r: 6 });
        if (this.showLayers.labels) {
          this.queueLabel(shortId(t.track_id), s.x, s.y, 6,
            { prio: 2, font: '700 9px ui-monospace, monospace', color: T.text });
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

    /**
     * Scale bar, always bottom-left above the summary: a plate in the plan
     * colour with a two-part bar whose length is a round distance (0.5 m to
     * 50 m) for the current zoom, so it reads in both themes at any size.
     */
    drawScaleBar() {
      const ctx = this.ctx;
      const target = this.canvas.clientWidth < 420 ? 64 : 90;
      let metres = SCALE_STEPS[SCALE_STEPS.length - 1];
      for (const m of SCALE_STEPS) { if (m * this.scale >= target) { metres = m; break; } }
      const px = metres * this.scale;
      const label = `${fmt(metres)} m`;
      ctx.save();
      ctx.font = '700 12px system-ui, sans-serif';
      const tw = Math.ceil(ctx.measureText(label).width);
      const padX = 10, plateW = px + tw + padX * 3, plateH = 30;
      // Just above the summary overlay, never underneath it.
      const x = 10, y = this.canvas.clientHeight - Math.max(10, this.hudReserve() + 4) - plateH;
      ctx.globalAlpha = 0.9;
      ctx.fillStyle = T.bg;
      roundRect(ctx, x, y, plateW, plateH, 6);
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.strokeStyle = T.outline;
      ctx.lineWidth = 1;
      ctx.stroke();
      const bx = x + padX, by = y + plateH / 2 - 3, bh = 6;
      ctx.fillStyle = T.text;
      ctx.fillRect(bx, by, px / 2, bh);
      ctx.fillStyle = T.bg;
      ctx.fillRect(bx + px / 2, by, px / 2, bh);
      ctx.strokeStyle = T.text;
      ctx.lineWidth = 1.5;
      ctx.strokeRect(bx, by, px, bh);
      ctx.beginPath();
      ctx.moveTo(bx, by - 4); ctx.lineTo(bx, by + bh + 4);
      ctx.moveTo(bx + px, by - 4); ctx.lineTo(bx + px, by + bh + 4);
      ctx.stroke();
      ctx.fillStyle = T.text;
      ctx.textAlign = 'left';
      ctx.textBaseline = 'middle';
      ctx.fillText(label, bx + px + padX, y + plateH / 2 + 0.5);
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

  // ------------------------------------------------- geometry and snapshots

  function r3(v) { return Math.round(v * 1000) / 1000; }

  function clonePts(pts) { return (pts || []).map((p) => ({ x: p.x, y: p.y })); }

  function boundsOf(pts) {
    const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
    return { minX: Math.min(...xs), maxX: Math.max(...xs), minY: Math.min(...ys), maxY: Math.max(...ys) };
  }

  /** World point -> (u, v) in a frame centred on (cx, cy), turned by rad. */
  function worldToFrame(f, x, y) {
    const c = Math.cos(f.rad), s = Math.sin(f.rad), dx = x - f.cx, dy = y - f.cy;
    return { u: dx * c + dy * s, v: -dx * s + dy * c };
  }

  function frameToWorld(f, u, v) {
    const c = Math.cos(f.rad), s = Math.sin(f.rad);
    return { x: f.cx + u * c - v * s, y: f.cy + u * s + v * c };
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function cameraSnap(cam) {
    return { camera_id: cam.camera_id, floor_x: +cam.floor_x, floor_y: +cam.floor_y, floor_z: cam.floor_z,
      azimuth_deg: cam.azimuth_deg, fov_deg: cam.fov_deg };
  }

  function cameraFields(snap) {
    return { floor_x: snap.floor_x, floor_y: snap.floor_y, floor_z: snap.floor_z, azimuth_deg: snap.azimuth_deg, fov_deg: snap.fov_deg };
  }

  /** Move a shape or camera snapshot by (dx, dy) metres, in place. */
  function shiftSnap(snap, dx, dy) {
    if (!snap) return;
    if (Array.isArray(snap.polygon)) snap.polygon = snap.polygon.map((p) => ({ x: r3(p.x + dx), y: r3(p.y + dy) }));
    if (Number.isFinite(snap.floor_x)) snap.floor_x = r3(snap.floor_x + dx);
    if (Number.isFinite(snap.floor_y)) snap.floor_y = r3(snap.floor_y + dy);
  }

  /** Put a shape snapshot's editable fields back on the shape object. */
  function restoreSnap(shape, snap) {
    shape.polygon = clonePts(snap.polygon);
    ['name', 'color', 'category', 'kind', 'thickness_m', 'sku_id'].forEach((k) => { if (k in snap) shape[k] = snap[k]; });
    if ('products' in snap) shape.products = (snap.products || []).slice();
    if ('properties' in snap) shape.properties = JSON.parse(JSON.stringify(snap.properties || {}));
    if ('_rotation_deg' in snap) shape._rotation_deg = snap._rotation_deg;
  }

  function snapRotation(snap) {
    const v = snap && (snap.properties ? snap.properties.rotation_deg : snap._rotation_deg);
    return Number.isFinite(+v) ? +v : 0;
  }

  function describeSides(sides) {
    const names = { left_m: 'on the left', top_m: 'at the top', right_m: 'on the right', bottom_m: 'at the bottom' };
    const parts = Object.keys(names).filter((k) => sides[k] > 0).map((k) => `${fmt(sides[k])} m ${names[k]}`);
    return parts.length ? `adds ${parts.join(', ')}` : 'no change';
  }

  function describeItem(i) {
    const what = i.type === 'camera' ? 'Camera' : i.type === 'zone' ? 'Zone' : (KIND_LABEL[i.kind] || 'Item');
    return `${what} "${i.name || i.id}"`;
  }

  function describeItems(items) {
    const list = (items || []).slice(0, 3).map(describeItem).join(', ');
    return items.length > 3 ? `${list} and ${items.length - 3} more` : list;
  }

  function emit(name, detail) {
    try { window.dispatchEvent(new CustomEvent(name, { detail })); } catch (_) { /* no CustomEvent */ }
  }

  // --------------------------------------------- window.FloorplanAPI (stable)
  //
  // For other modules (camera gallery). Exists from script load; methods that
  // need the map wait for it (ready resolves after the first layout load).
  //   ready, getCameras(), clientToWorld(clientX, clientY), canvasElement(),
  //   placeCamera(cameraId, x_m, y_m), focusCamera(cameraId), setDropPreview(x_m, y_m | null)
  // Events on window: 'fp:layout-changed' {cameras}, 'fp:camera-hover'
  // {camera_id, clientX, clientY} | {camera_id: null}, 'fp:camera-selected' {camera_id}.
  let resolveReady = () => {};
  const readyPromise = new Promise((resolve) => { resolveReady = resolve; });
  const editorNow = () => window.blueprintEditor || null;
  window.FloorplanAPI = {
    ready: readyPromise,
    getCameras() { const ed = editorNow(); return ed ? ed.apiCameras() : []; },
    clientToWorld(clientX, clientY) { const ed = editorNow(); return ed && ed.layout ? ed.apiClientToWorld(clientX, clientY) : null; },
    canvasElement() { const ed = editorNow(); return ed ? ed.canvas : document.getElementById('floorplanCanvas'); },
    async placeCamera(cameraId, xm, ym) {
      await readyPromise;
      const ed = editorNow();
      if (!ed) throw new Error('The store map is not ready yet.');
      if (!Number.isFinite(+xm) || !Number.isFinite(+ym)) throw new Error('A map position in metres is needed.');
      await ed.placeCameraById(cameraId, +xm, +ym);
    },
    focusCamera(cameraId) { const ed = editorNow(); if (ed) ed.focusCamera(cameraId); },
    setDropPreview(xm, ym) { const ed = editorNow(); if (ed) ed.setDropPreview(xm, ym); },
  };

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

  // Start only after auth.js has checked the stored token (edgeAuth.onReady).
  if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') {
    window.edgeAuth.onReady(initFloorplan);
  } else if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initFloorplan);
  } else {
    initFloorplan();
  }
})();
