/**
 * Heatmap history panel (Insights > Shoppers & footfall, #insights-heatmap-history).
 *
 * Replays, sums and compares the hourly heatmap snapshots the server records
 * (services/heatmap_history.py, /api/v1/analytics/heatmaps/*). Nothing here is
 * invented: a cell is shaded only from a recorded snapshot, an hour the cameras
 * were not recording is shown as "not recorded" (never as zero), and a measured
 * hour where nobody was seen is shown as "quiet".
 *
 * Dates are the store's: `from`/`to` are sent without an offset, which the
 * server reads as store-local time. The browser is assumed to run in the
 * store's time zone for "today" and for the labels it computes itself.
 *
 * Public surface: window.edgeHeatmapHistory = { refresh(), show(opts), state() }
 */
(function () {
  'use strict';

  const API = '/api/v1/analytics/heatmaps';
  const HISTORY_POLL_MS = 60 * 1000;
  const SLOW_POLL_MS = 5 * 60 * 1000;
  const PLAY_STEP_MS = 1200;
  const SNAP_CACHE_MAX = 48;
  const HOUR_MS = 3600 * 1000;

  // Same wording as floorplan.js HEATMAP_KINDS (the live map).
  const KINDS = [
    { kind: 'presence', label: 'Walked', title: 'Where people walked' },
    { kind: 'dwell', label: 'Stopped', title: 'Where people stopped (time spent per spot)' },
    { kind: 'interaction', label: 'Touched shelves', title: 'Where people reached into shelves' },
  ];
  const RANGES = [
    { range: 'hour', label: 'Hour' },
    { range: 'day', label: 'Day' },
    { range: 'week', label: 'Week' },
  ];
  const PRESETS = [
    { id: 'week', label: 'This week vs last week' },
    { id: 'sameday', label: 'Today vs same day last week' },
    { id: 'yesterday', label: 'Yesterday vs day before' },
    { id: 'custom', label: 'Custom…' },
  ];

  const S = {
    built: false,
    space: 'floor',
    cameraId: null,
    kind: 'presence',
    range: 'hour',          // hour | day | week | period (from a finding)
    date: null,             // YYYY-MM-DD, store-local
    hour: null,             // hour index within the day
    periodFrom: null,       // YYYY-MM-DD (range 'period')
    periodTo: null,         // YYYY-MM-DD inclusive
    compare: null,          // {a_from, a_to, b_from, b_to, label}
    compareOpen: false,
    wantCurrentHour: false,
    seq: 0,
    layout: null,
    layoutAt: 0,
    cameras: [],
    camerasAt: 0,
    backgrounds: new Map(), // camera id -> {img, url, noSignal, at, aspect}
    snapCache: new Map(),   // "id|updated_at" -> payload
    hist: null,             // last /history payload of the selected day
    model: null,            // derived hour cells
    view: null,             // what is drawn: {mode, matrix, gw, gh, width_m, height_m, shift}
    loaded: null,           // metadata of the last snapshot / aggregate / compare
    dataState: 'loading',
    playTimer: null,
    profile: null,
    profileKey: null,
    profileAt: 0,
    chart: null,
    recs: null,
    recsAt: 0,
    analysisSummary: null,
    lastStageW: 0,
  };

  // ------------------------------------------------------------ utilities

  const pad = (n) => String(n).padStart(2, '0');
  function ymd(d) { return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; }
  function todayStr() { return ymd(new Date()); }
  function addDays(s, n) {
    const d = new Date(`${s}T12:00:00`);
    d.setDate(d.getDate() + n);
    return ymd(d);
  }
  function startOf(s) { return `${s}T00:00:00`; }
  function fmtDay(s, withWeekday = true) {
    const d = new Date(`${s}T12:00:00`);
    if (Number.isNaN(d.getTime())) return s;
    return d.toLocaleDateString(undefined, withWeekday
      ? { weekday: 'short', day: 'numeric', month: 'short' }
      : { day: 'numeric', month: 'short' });
  }
  function fmtSpan(a, b) { return a === b ? fmtDay(a) : `${fmtDay(a, false)} – ${fmtDay(b, false)}`; }
  function fmtClock(iso) {
    const d = iso ? new Date(iso) : null;
    if (!d || Number.isNaN(d.getTime())) return DASH;
    const t = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    return ymd(d) === todayStr() ? `${t} today` : `${d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })} ${t}`;
  }
  function unitWord(unit) {
    return unit === 'seconds' ? 'time' : unit === 'interactions' ? 'shelf reaches' : unit === 'visitors' ? 'visitors' : (unit || '');
  }
  function fmtValue(v, unit) {
    if (!isNum(v)) return DASH;
    if (unit === 'seconds') return formatDuration(v);
    const n = Math.round(v * 10) / 10;
    return `${n.toLocaleString()} ${unit === 'visitors' ? (n === 1 ? 'visitor' : 'visitors') : unit === 'interactions' ? (n === 1 ? 'reach' : 'reaches') : (unit || '')}`.trim();
  }
  function kindInfo(k) { return KINDS.find((x) => x.kind === k) || KINDS[0]; }

  function isShown() {
    const host = el('tab-analytics');
    return !!host && host.getClientRects().length > 0;
  }

  /** fetch JSON with status; never throws. auth.js adds the bearer token. */
  async function api(url, init) {
    if (typeof canPoll === 'function' && !canPoll()) return { ok: false, status: 0, data: null };
    try {
      const res = await fetch(url, init);
      let data = null;
      try { data = await res.json(); } catch (_) { data = null; }
      return { ok: res.ok, status: res.status, data };
    } catch (e) {
      return { ok: false, status: 0, data: null };
    }
  }
  function errText(r, what) {
    const d = r && r.data && r.data.detail;
    if (typeof d === 'string') return d;
    if (Array.isArray(d) && d[0] && d[0].msg) return d[0].msg;
    if (r && r.status === 0) return `Could not load ${what}: the server did not respond.`;
    return `Could not load ${what} (HTTP ${r ? r.status : '?'}).`;
  }

  function scopeParams(extra) {
    const p = new URLSearchParams({ space: S.space, kind: S.kind });
    if (S.space === 'image' && S.cameraId) p.set('camera_id', S.cameraId);
    Object.entries(extra || {}).forEach(([k, v]) => { if (v !== null && v !== undefined) p.set(k, v); });
    return p.toString();
  }

  function token(name) {
    try { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); } catch (_) { return ''; }
  }

  /** Any CSS colour -> [r, g, b, a] (0-255), via a 1x1 canvas. */
  const _probe = document.createElement('canvas');
  _probe.width = 1; _probe.height = 1;
  function rgba(css, fallback) {
    const g = _probe.getContext('2d', { willReadFrequently: true });
    g.clearRect(0, 0, 1, 1);
    g.fillStyle = fallback || '#808080';
    g.fillStyle = css || fallback || '#808080';
    g.fillRect(0, 0, 1, 1);
    const d = g.getImageData(0, 0, 1, 1).data;
    return [d[0], d[1], d[2], d[3]];
  }
  function palette() {
    return {
      bg: token('--hm-canvas-bg') || '#0b0e14',
      outline: token('--hm-outline') || '#8b949e',
      zone: token('--hm-zone') || '#c9d4e0',
      structure: token('--hm-structure') || '#57606a',
      text: token('--hm-canvas-text') || '#e6edf3',
      arrow: token('--hm-arrow') || '#ffffff',
      seqLo: rgba(token('--hm-seq-lo'), '#ffd166'),
      seqHi: rgba(token('--hm-seq-hi'), '#ff4d2e'),
      divLess: rgba(token('--hm-div-less'), '#3b9dff'),
      divMore: rgba(token('--hm-div-more'), '#ff5b3b'),
    };
  }

  // ------------------------------------------------------------ DOM

  function build() {
    const sec = el('insights-heatmap-history');
    if (!sec || S.built) return !!sec;
    sec.classList.add('hm-panel');
    sec.innerHTML = `
      <div class="card-title hm-title">
        <span>Heatmap history</span>
        <span class="badge badge-neutral" id="hmRecorderBadge" title="State of the hourly heatmap recorder">${DASH}</span>
      </div>
      <p class="hm-lede">Recorded every hour from what the cameras saw. Pick a place, what to show and a time; hatched hours were not recorded (camera off), so they are unknown, not zero.</p>
      <div class="hm-controls">
        <label class="hm-field"><span class="hm-field-label">Where</span>
          <select id="hmSpace" class="form-select hm-select"><option value="floor">Whole store (map)</option></select></label>
        <div class="hm-seg" role="group" aria-label="What to show">
          ${KINDS.map((k) => `<button type="button" class="btn btn-sm hm-seg-btn" data-hm-kind="${k.kind}" title="${escapeHtml(k.title)}" aria-pressed="false">${escapeHtml(k.label)}</button>`).join('')}
        </div>
        <div class="hm-seg" role="group" aria-label="Time range">
          ${RANGES.map((r) => `<button type="button" class="btn btn-sm hm-seg-btn" data-hm-range="${r.range}" aria-pressed="false">${r.label}</button>`).join('')}
        </div>
        <label class="hm-field"><span class="hm-field-label">Date</span>
          <input type="date" id="hmDate" class="form-input hm-select" /></label>
        <label class="hm-field" id="hmHourField"><span class="hm-field-label">Hour</span>
          <select id="hmHour" class="form-select hm-select"></select></label>
        <div class="hm-actions">
          <button type="button" class="btn btn-sm" data-action="hm-play" aria-pressed="false" title="Step through the recorded hours of this day">Play day</button>
          <button type="button" class="btn btn-sm" data-action="hm-compare" aria-pressed="false" aria-controls="hmComparePanel">Compare with…</button>
          <button type="button" class="btn btn-sm btn-secondary" data-action="hm-record-now" title="Write the current hour so far now instead of waiting for it to end">Record now</button>
        </div>
      </div>
      <div class="hm-compare-panel" id="hmComparePanel" hidden>
        <div class="hm-seg hm-presets" role="group" aria-label="Compare periods">
          ${PRESETS.map((p) => `<button type="button" class="btn btn-sm hm-seg-btn" data-hm-preset="${p.id}" aria-pressed="false">${escapeHtml(p.label)}</button>`).join('')}
        </div>
        <div class="hm-custom" id="hmCustom" hidden>
          <fieldset class="hm-period"><legend>Before (A)</legend>
            <label class="hm-field"><span class="hm-field-label">From</span><input type="date" id="hmCmpAFrom" class="form-input hm-select" /></label>
            <label class="hm-field"><span class="hm-field-label">To</span><input type="date" id="hmCmpATo" class="form-input hm-select" /></label>
          </fieldset>
          <fieldset class="hm-period"><legend>After (B)</legend>
            <label class="hm-field"><span class="hm-field-label">From</span><input type="date" id="hmCmpBFrom" class="form-input hm-select" /></label>
            <label class="hm-field"><span class="hm-field-label">To</span><input type="date" id="hmCmpBTo" class="form-input hm-select" /></label>
          </fieldset>
          <button type="button" class="btn btn-sm btn-primary" data-action="hm-compare-apply">Compare</button>
        </div>
      </div>
      <div class="hm-strip-wrap" id="hmStripWrap">
        <div class="hm-strip" id="hmStrip" role="group" aria-label="Hours of the selected day"></div>
        <div class="hm-strip-legend" aria-label="Hour legend">
          <span class="hm-key"><span class="hm-swatch hm-swatch-observed"></span>Recorded (darker = busier)</span>
          <span class="hm-key"><span class="hm-swatch hm-swatch-quiet"></span>Quiet: cameras on, nobody seen</span>
          <span class="hm-key"><span class="hm-swatch hm-swatch-off"></span>Camera off / not recorded</span>
          <span class="hm-key"><span class="hm-swatch hm-swatch-future"></span>Not yet</span>
        </div>
      </div>
      <div class="hm-main">
        <div class="hm-stage" id="hmStage">
          <canvas id="hmCanvas" role="img" aria-label="Heatmap"></canvas>
          <div class="hm-stage-msg" id="hmStageMsg" hidden></div>
        </div>
        <div class="hm-side">
          <div class="hm-current">Showing <strong id="hmCurrentHour" data-hm-current-hour>${DASH}</strong></div>
          <div id="hmStatus" class="form-status hm-status" role="status" aria-live="polite"></div>
          <div class="hm-legend" id="hmLegend"></div>
          <dl class="hm-facts" id="hmFacts"></dl>
          <div class="hm-empty-box" id="hmEmpty"></div>
        </div>
      </div>
      <div class="hm-lower">
        <div class="hm-block">
          <h4 class="hm-subtitle">Typical day, last 14 days</h4>
          <div class="hm-chart-box"><canvas id="hmProfileChart" aria-label="Average activity by hour of day"></canvas></div>
          <p class="hm-note" id="hmProfileNote"></p>
        </div>
        <div class="hm-block">
          <h4 class="hm-subtitle">What the heatmaps suggest</h4>
          <div id="hmRecs" class="hm-recs"><div class="fp-empty">Loading…</div></div>
        </div>
      </div>`;
    sec.hidden = false;
    S.built = true;

    el('hmSpace').addEventListener('change', (e) => {
      const v = e.target.value;
      if (v === 'floor') { S.space = 'floor'; S.cameraId = null; } else { S.space = 'image'; S.cameraId = v.slice(4); }
      stopPlay(); load();
    });
    sec.querySelectorAll('[data-hm-kind]').forEach((b) => b.addEventListener('click', () => {
      S.kind = b.dataset.hmKind; stopPlay(); load();
    }));
    sec.querySelectorAll('[data-hm-range]').forEach((b) => b.addEventListener('click', () => {
      S.range = b.dataset.hmRange; S.compare = null; stopPlay(); load();
    }));
    el('hmDate').addEventListener('change', (e) => {
      if (!e.target.value) return;
      S.date = e.target.value;
      if (S.range === 'period') S.range = 'day';
      S.hour = null; stopPlay(); load();
    });
    el('hmHour').addEventListener('change', (e) => {
      S.hour = parseInt(e.target.value, 10);
      S.range = 'hour'; S.compare = null; stopPlay(); load();
    });
    sec.querySelector('[data-action="hm-play"]').addEventListener('click', togglePlay);
    sec.querySelector('[data-action="hm-compare"]').addEventListener('click', toggleCompare);
    sec.querySelector('[data-action="hm-record-now"]').addEventListener('click', recordNow);
    sec.querySelectorAll('[data-hm-preset]').forEach((b) => b.addEventListener('click', () => applyPreset(b.dataset.hmPreset)));
    sec.querySelector('[data-action="hm-compare-apply"]').addEventListener('click', applyCustomCompare);
    el('hmStrip').addEventListener('click', (e) => {
      const cell = e.target.closest('[data-hm-hour-cell]');
      if (!cell) return;
      S.hour = parseInt(cell.dataset.hmHourCell, 10);
      S.range = 'hour'; S.compare = null; stopPlay(); load();
    });
    sec.addEventListener('click', (e) => {
      const jump = e.target.closest('[data-action="hm-jump-latest"]');
      if (jump) { jumpLatest(jump); return; }
      const cite = e.target.closest('[data-hm-cite]');
      if (cite && cite._spec) { show(cite._spec); jumpToStage(); }
    });

    if (typeof ResizeObserver === 'function') {
      const ro = new ResizeObserver(() => {
        const w = el('hmStage') ? el('hmStage').clientWidth : 0;
        // Next frame: resizing the canvas inside the callback would re-trigger the observer.
        if (w && w !== S.lastStageW) { S.lastStageW = w; requestAnimationFrame(() => draw()); }
      });
      ro.observe(el('hmStage'));
    } else {
      window.addEventListener('resize', () => draw());
    }
    return true;
  }

  function jumpToStage() {
    const st = el('hmStage');
    if (st) st.scrollIntoView({ behavior: 'instant', block: 'center' });
  }

  function setStatus(msg, isError) {
    const n = el('hmStatus');
    if (!n) return;
    n.textContent = msg || '';
    n.classList.toggle('form-status-error', !!isError);
  }
  function setState(s) {
    S.dataState = s;
    const sec = el('insights-heatmap-history');
    if (sec) sec.setAttribute('data-hm-state', s);
  }
  function setStageMsg(html) {
    const n = el('hmStageMsg');
    if (!n) return;
    n.innerHTML = html || '';
    n.hidden = !html;
  }

  function syncControls() {
    const sec = el('insights-heatmap-history');
    if (!sec) return;
    sec.querySelectorAll('[data-hm-kind]').forEach((b) => {
      const on = b.dataset.hmKind === S.kind;
      b.classList.toggle('btn-primary', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    sec.querySelectorAll('[data-hm-range]').forEach((b) => {
      const on = !S.compare && b.dataset.hmRange === S.range;
      b.classList.toggle('btn-primary', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    const sp = el('hmSpace');
    if (sp) sp.value = S.space === 'floor' ? 'floor' : `cam:${S.cameraId}`;
    const dt = el('hmDate');
    if (dt) { dt.value = S.date || ''; dt.max = todayStr(); }
    const hf = el('hmHourField');
    if (hf) hf.classList.toggle('hm-dim', S.range !== 'hour' || !!S.compare);
    const cmpBtn = sec.querySelector('[data-action="hm-compare"]');
    if (cmpBtn) {
      cmpBtn.classList.toggle('btn-primary', !!S.compareOpen);
      cmpBtn.setAttribute('aria-pressed', S.compareOpen ? 'true' : 'false');
    }
    const panel = el('hmComparePanel');
    if (panel) panel.hidden = !S.compareOpen;
    sec.querySelectorAll('[data-hm-preset]').forEach((b) => {
      const on = !!S.compare && S.compare.preset === b.dataset.hmPreset;
      b.classList.toggle('btn-primary', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    const strip = el('hmStripWrap');
    if (strip) strip.hidden = !!S.compare;
    const play = sec.querySelector('[data-action="hm-play"]');
    if (play) {
      play.textContent = S.playTimer ? 'Pause' : 'Play day';
      play.setAttribute('aria-pressed', S.playTimer ? 'true' : 'false');
      play.classList.toggle('btn-primary', !!S.playTimer);
      play.disabled = !!S.compare;
    }
  }

  // ------------------------------------------------------------ reference data

  async function ensureLayout(force) {
    if (!force && S.layout && Date.now() - S.layoutAt < SLOW_POLL_MS) return;
    const r = await api('/api/v1/layout');
    S.layoutAt = Date.now();
    S.layout = r.ok && r.data && isNum(r.data.width_m) && isNum(r.data.height_m) ? r.data : (r.ok ? null : S.layout);
  }

  async function ensureCameras(force) {
    if (!force && S.camerasAt && Date.now() - S.camerasAt < SLOW_POLL_MS) return;
    const r = await api('/api/v1/cameras');
    S.camerasAt = Date.now();
    if (r.ok && r.data && Array.isArray(r.data.cameras)) S.cameras = r.data.cameras;
    renderSpaceOptions();
  }

  function renderSpaceOptions() {
    const sp = el('hmSpace');
    if (!sp) return;
    const opts = ['<option value="floor">Whole store (map)</option>'];
    S.cameras.forEach((c) => {
      const staff = c.role === 'stockroom';
      opts.push(`<option value="cam:${escapeHtml(c.id)}"${staff ? ' disabled' : ''}>${escapeHtml(c.name || c.id)}${staff ? ' (staff area, not recorded)' : ''}</option>`);
    });
    if (S.space === 'image' && S.cameraId && !S.cameras.some((c) => c.id === S.cameraId)) {
      opts.push(`<option value="cam:${escapeHtml(S.cameraId)}">${escapeHtml(S.cameraId)} (removed camera)</option>`);
    }
    const html = opts.join('');
    if (sp._html !== html) { sp.innerHTML = html; sp._html = html; }
    sp.value = S.space === 'floor' ? 'floor' : `cam:${S.cameraId}`;
  }

  function cameraName(id) {
    const c = S.cameras.find((x) => x.id === id);
    return c ? (c.name || c.id) : id;
  }

  /** The camera's still picture; X-Frame-Source: no-signal means there is none. */
  async function ensureBackground(camId) {
    const have = S.backgrounds.get(camId);
    if (have && Date.now() - have.at < 60 * 1000) return have;
    const entry = { img: null, url: null, noSignal: true, at: Date.now(), aspect: 16 / 9 };
    if (typeof canPoll === 'function' && !canPoll()) return have || entry;
    try {
      const res = await fetch(`/api/v1/cameras/${encodeURIComponent(camId)}/snapshot?annotate=false`);
      const src = (res.headers.get('X-Frame-Source') || '').toLowerCase();
      if (res.ok && src !== 'no-signal') {
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const img = await new Promise((resolve) => {
          const im = new Image();
          im.onload = () => resolve(im);
          im.onerror = () => resolve(null);
          im.src = url;
        });
        if (img && img.naturalWidth > 0 && img.naturalHeight > 0) {
          entry.img = img; entry.url = url; entry.noSignal = false;
          entry.aspect = img.naturalWidth / img.naturalHeight;
        } else {
          URL.revokeObjectURL(url);
        }
      }
    } catch (_) { /* keep the neutral background */ }
    if (have && have.url && have.url !== entry.url) URL.revokeObjectURL(have.url);
    S.backgrounds.set(camId, entry);
    return entry;
  }

  // ------------------------------------------------------------ day model

  function hourIndex(isoZ, fromMs) { return Math.round((Date.parse(isoZ) - fromMs) / HOUR_MS); }

  function buildModel(hist) {
    const from = Date.parse(hist.from), to = Date.parse(hist.to);
    let n = Math.round((to - from) / HOUR_MS);
    if (!(n >= 23 && n <= 25)) n = 24;
    const settle = (hist.recorder && isNum(hist.recorder.settle_seconds) ? hist.recorder.settle_seconds : 300) * 1000;
    const now = Date.now();
    const cells = Array.from({ length: n }, (_, i) => ({ index: i, label: `${pad(i % 24)}:00`, state: 'future', snap: null, start: from + i * HOUR_MS }));
    (hist.snapshots || []).forEach((s) => {
      const i = hourIndex(s.bucket_start, from);
      const c = cells[i];
      if (!c) return;
      c.snap = s;
      c.state = s.status === 'observed' ? 'observed' : 'quiet';
      const m = /T(\d{2}):/.exec(s.bucket_start_local || '');
      if (m) c.label = `${m[1]}:00`;
    });
    (hist.not_recorded || []).forEach((b) => {
      const c = cells[hourIndex(b, from)];
      if (c && !c.snap) c.state = 'off';
    });
    // The running hour, and one that closed less than the settle time ago, are
    // written later: "in progress", not "camera off".
    cells.forEach((c) => {
      if (!c.snap && c.state !== 'future' && now < c.start + HOUR_MS + settle + 60 * 1000) c.state = 'pending';
    });
    const maxTotal = Math.max(0, ...cells.filter((c) => c.state === 'observed').map((c) => c.snap.total_value || 0));
    const current = cells.find((c) => now >= c.start && now < c.start + HOUR_MS);
    return { cells, maxTotal, from, to, currentIndex: current ? current.index : null };
  }

  const CELL_TEXT = {
    observed: 'recorded',
    quiet: 'quiet: cameras on, nobody seen',
    off: 'camera off / not recorded',
    pending: 'in progress: written after the hour ends',
    future: 'not yet',
  };

  function renderStrip() {
    const strip = el('hmStrip');
    const m = S.model;
    if (!strip) return;
    if (!m) { strip.innerHTML = ''; return; }
    strip.style.setProperty('--hm-cols', String(m.cells.length));
    const cellsHtml = m.cells.map((c) => {
      const level = c.state === 'observed' && m.maxTotal > 0 ? Math.max(0.12, (c.snap.total_value || 0) / m.maxTotal) : 0;
      const partial = c.snap && c.snap.complete === false;
      const sel = S.range === 'hour' && !S.compare && S.hour === c.index;
      const tip = c.snap
        ? `${c.label}: ${CELL_TEXT[c.state]}${c.state === 'observed' ? `, ${fmtValue(c.snap.total_value, c.snap.unit)} in total` : ''}${partial ? ' (so far, hour still open)' : ''}`
        : `${c.label}: ${CELL_TEXT[c.state]}`;
      return `<button type="button" class="hm-cell hm-cell-${c.state}${sel ? ' is-selected' : ''}${partial ? ' is-partial' : ''}"
        data-hm-hour-cell="${c.index}" data-hm-cell-state="${c.state}" title="${escapeHtml(tip)}" aria-label="${escapeHtml(tip)}"
        aria-pressed="${sel ? 'true' : 'false'}"><span class="hm-cell-fill" style="opacity:${level.toFixed(3)}"></span><span class="hm-cell-label">${c.label.slice(0, 2)}</span></button>`;
    }).join('');
    // Rebuild only on change, so a background refresh keeps keyboard focus.
    if (strip._html !== cellsHtml) { strip.innerHTML = cellsHtml; strip._html = cellsHtml; }
    const hs = el('hmHour');
    if (hs) {
      const html = m.cells.map((c) => `<option value="${c.index}">${c.label}${c.state === 'observed' ? '' : ` · ${c.state === 'quiet' ? 'quiet' : c.state === 'off' ? 'not recorded' : c.state === 'pending' ? 'in progress' : 'not yet'}`}</option>`).join('');
      if (hs._html !== html) { hs.innerHTML = html; hs._html = html; }
      if (S.hour !== null && S.hour !== undefined) hs.value = String(S.hour);
    }
  }

  function renderRecorderBadge(rec) {
    const b = el('hmRecorderBadge');
    if (!b) return;
    b.classList.remove('badge-green', 'badge-warning', 'badge-neutral', 'badge-danger');
    if (!rec) { b.textContent = DASH; b.classList.add('badge-neutral'); return; }
    if (!rec.enabled) { b.textContent = 'RECORDING OFF'; b.classList.add('badge-neutral'); b.title = 'Heatmap recording is switched off in the server settings.'; }
    else if (!rec.running) { b.textContent = 'RECORDER STOPPED'; b.classList.add('badge-warning'); b.title = rec.last_error || 'The hourly recorder is not running.'; }
    else if (rec.last_error) { b.textContent = 'RECORDING · ERROR'; b.classList.add('badge-warning'); b.title = rec.last_error; }
    else { b.textContent = 'RECORDING HOURLY'; b.classList.add('badge-green'); b.title = rec.last_recorded_hour ? `Last hour written: ${fmtClock(rec.last_recorded_hour)}` : 'Recording'; }
  }

  // ------------------------------------------------------------ loading

  async function loadHistoryDay() {
    const q = scopeParams({ from: startOf(S.date), to: startOf(addDays(S.date, 1)), bucket: 'hour' });
    const r = await api(`${API}/history?${q}`);
    return r;
  }

  async function snapshot(meta) {
    const key = `${meta.id}|${meta.updated_at || ''}`;
    if (S.snapCache.has(key)) return { ok: true, data: S.snapCache.get(key) };
    const r = await api(`${API}/snapshot/${encodeURIComponent(meta.id)}`);
    if (r.ok && r.data) {
      S.snapCache.set(key, r.data);
      if (S.snapCache.size > SNAP_CACHE_MAX) S.snapCache.delete(S.snapCache.keys().next().value);
    }
    return r;
  }

  function rangeWindow() {
    if (S.range === 'day') return { from: S.date, to: S.date };
    if (S.range === 'week') return { from: addDays(S.date, -6), to: S.date };
    if (S.range === 'period') return { from: S.periodFrom, to: S.periodTo };
    return null;
  }

  /**
   * Load and draw the current selection. quiet: a background refresh, which
   * does not blank the view while it loads.
   */
  async function load(opts) {
    if (!build()) return;
    const quiet = !!(opts && opts.quiet);
    const my = ++S.seq;
    if (!S.date) S.date = todayStr();
    syncControls();
    if (!quiet) { setState('loading'); setStatus('Loading…'); }

    await Promise.all([ensureLayout(opts && opts.force), ensureCameras(opts && opts.force)]);
    if (my !== S.seq) return;
    if (S.space === 'image' && !S.cameraId) { S.space = 'floor'; }
    const bgP = S.space === 'image' ? ensureBackground(S.cameraId) : Promise.resolve(null);

    const hr = await loadHistoryDay();
    if (my !== S.seq) return;
    await bgP;
    if (my !== S.seq) return;
    if (!hr.ok || !hr.data) {
      S.hist = null; S.model = null;
      renderStrip();
      S.view = null; S.loaded = null;
      draw();
      setStageMsg(`<p>${escapeHtml(errText(hr, 'the heatmap history'))}</p>`);
      setStatus(errText(hr, 'the heatmap history'), true);
      setState('error');
      return;
    }
    S.hist = hr.data;
    S.model = buildModel(hr.data);
    renderRecorderBadge(hr.data.recorder);

    if (S.wantCurrentHour && S.model.currentIndex !== null) { S.hour = S.model.currentIndex; S.wantCurrentHour = false; }
    if (S.hour === null || S.hour === undefined || !S.model.cells[S.hour]) {
      const last = S.model.cells.filter((c) => c.snap).pop();
      S.hour = last ? last.index : (S.model.currentIndex !== null ? S.model.currentIndex : Math.min(12, S.model.cells.length - 1));
    }
    renderStrip();
    syncControls();

    const never = !hr.data.first_recorded && !(hr.data.snapshots || []).length;
    if (never && !S.compare) {
      renderNeverRecorded(hr.data.recorder);
      maybeSlowLoads();
      return;
    }
    el('hmEmpty').innerHTML = '';

    if (S.compare) await loadCompare(my);
    else if (S.range === 'hour') await loadHour(my);
    else await loadAggregate(my);
    maybeSlowLoads();
  }

  function maybeSlowLoads() {
    const key = `${S.space}|${S.cameraId || ''}|${S.kind}`;
    if (key !== S.profileKey || Date.now() - S.profileAt > SLOW_POLL_MS) loadProfile();
    if (!S.recs || Date.now() - S.recsAt > SLOW_POLL_MS) loadRecs();
  }

  function whereLabel() {
    return S.space === 'floor' ? 'the whole store' : cameraName(S.cameraId);
  }

  function renderNeverRecorded(rec) {
    S.view = null;
    S.loaded = { type: 'none', ids: [], observed: false, peak_value: null };
    draw();
    const now = Date.now();
    let msg;
    if (!rec || rec.enabled === false) {
      msg = 'Heatmap recording is switched off on this server, so no hourly heatmaps are being kept. Turn on HEATMAP_RECORDING_ENABLED in the server settings to start.';
    } else if (!rec.running) {
      msg = `The hourly heatmap recorder is not running${rec.last_error ? ` (last error: ${rec.last_error})` : ''}. Restart the server to start recording.`;
    } else {
      const settle = isNum(rec.settle_seconds) ? rec.settle_seconds : 300;
      const started = rec.started_at ? Date.parse(rec.started_at) : null;
      const nextHour = (t) => { const d = new Date(t); d.setMinutes(0, 0, 0); d.setHours(d.getHours() + 1); return d.getTime(); };
      const firstDue = started ? nextHour(started) + settle * 1000 : null;
      const sayStarted = started ? `Recording started ${fmtClock(rec.started_at)}; ` : 'Recording is running; ';
      if (firstDue && now < firstDue) {
        msg = `${sayStarted}the first hourly heatmap appears after ${fmtClock(new Date(firstDue).toISOString())}.`;
      } else {
        const due = nextHour(now) + settle * 1000;
        const need = S.space === 'floor'
          ? 'a calibrated camera (placed on the store map) delivering frames'
          : `${cameraName(S.cameraId)} delivering frames with people counting on`;
        msg = `${sayStarted}nothing has been measured for ${whereLabel()} yet. It needs ${need}${S.kind === 'interaction' ? ' and shelf-reach detection switched on' : ''}. The next hourly heatmap is written after ${fmtClock(new Date(due).toISOString())}.`;
      }
    }
    setStageMsg(`<p>${escapeHtml('No heatmap recorded here yet.')}</p>`);
    el('hmEmpty').innerHTML = `<div class="fp-empty">${escapeHtml(msg)}</div>`;
    el('hmFacts').innerHTML = '';
    renderLegend(null);
    setCurrent(DASH);
    setStatus('');
    setState('empty');
  }

  function nothingInPeriod(extra) {
    el('hmEmpty').innerHTML = `<div class="fp-empty">Nothing recorded in this period.${extra ? ' ' + escapeHtml(extra) : ''}
      <button type="button" class="btn btn-sm btn-primary empty-action" data-action="hm-jump-latest">Show the latest recorded hour</button></div>`;
  }

  function setCurrent(text) {
    const n = el('hmCurrentHour');
    if (n) n.textContent = text;
  }

  async function loadHour(my) {
    const cell = S.model.cells[S.hour];
    const dayTxt = fmtDay(S.date);
    const label = cell ? `${dayTxt}, ${cell.label}–${pad((parseInt(cell.label, 10) + 1) % 24)}:00` : dayTxt;
    el('hmEmpty').innerHTML = '';
    if (!cell || !cell.snap) {
      S.view = null;
      S.loaded = { type: 'snapshot', ids: [], observed: false, peak_value: null, hour_state: cell ? cell.state : null };
      draw();
      setCurrent(label);
      const st = cell ? cell.state : 'future';
      const text = st === 'off'
        ? `Not recorded at ${cell.label}: the camera or the pipeline was off, so this hour is unknown, not zero.`
        : st === 'pending'
          ? `${cell.label} is still being recorded. It is written a few minutes after the hour ends; press Record now to capture it so far.`
          : `${cell ? cell.label : 'This hour'} has not happened yet.`;
      setStageMsg(`<p>${escapeHtml(text)}</p>`);
      el('hmFacts').innerHTML = '';
      renderLegend(null);
      const anyRecorded = S.model.cells.some((c) => c.snap);
      if (!anyRecorded) nothingInPeriod();
      setStatus(anyRecorded ? 'Pick a shaded hour in the strip to see what was recorded.' : '');
      setState('empty');
      return;
    }
    const r = await snapshot(cell.snap);
    if (my !== S.seq) return;
    if (!r.ok || !r.data) {
      S.view = null; draw();
      setStageMsg(`<p>${escapeHtml(errText(r, 'this hour'))}</p>`);
      setStatus(errText(r, 'this hour'), true);
      setState('error');
      return;
    }
    const d = r.data;
    const partial = d.complete === false;
    setCurrent(`${label}${partial ? ' (so far)' : ''}`);
    S.loaded = { type: 'snapshot', ids: [d.id], observed: !!d.observed, peak_value: d.peak_value, total_value: d.total_value,
      bucket_start: d.bucket_start, complete: d.complete, unit: d.unit };
    S.view = d.observed ? { mode: 'seq', matrix: d.density_matrix, gw: d.grid_width, gh: d.grid_height, width_m: d.width_m, height_m: d.height_m } : null;
    draw();
    setStageMsg(d.observed ? '' : `<p>${escapeHtml(`Quiet hour: the cameras were on at ${cell.label} and nobody was seen here.`)}</p>`);
    renderLegend(d.observed ? 'seq' : null, d);
    renderFacts([
      ['Total', fmtValue(d.total_value, d.unit), kindTotalHint()],
      ['Busiest spot', d.observed ? `${fmtValue(d.peak_value, d.unit)}${peakPlace(d) ? ' ' + peakPlace(d) : ''}` : DASH],
      ['Measured for', isNum(d.uptime_seconds) ? formatDuration(d.uptime_seconds) : DASH,
        isNum(d.uptime_seconds) ? 'How long the cameras were measuring in this hour' : 'Not known: this hour was rebuilt from stored tracks after a restart'],
      ['Status', partial ? 'So far: the hour is still open and is re-recorded when it ends' : 'Complete hour'],
    ]);
    setStatus(partial ? 'This hour is still open; the view shows it so far.' : '');
    setState('ready');
  }

  function kindTotalHint() {
    return S.kind === 'presence' ? 'Visitors summed over all cells: one visitor counts once in every cell they passed'
      : S.kind === 'dwell' ? 'Time people spent, summed over all cells' : 'Shelf reaches in total';
  }

  async function loadAggregate(my) {
    const w = rangeWindow();
    if (!w || !w.from || !w.to) { S.range = 'day'; return loadAggregate(my); }
    const q = scopeParams({ from: startOf(w.from), to: startOf(addDays(w.to, 1)), bucket: 'hour' });
    const r = await api(`${API}/aggregate?${q}`);
    if (my !== S.seq) return;
    const label = S.range === 'day' ? `${fmtDay(w.from)} (whole day)` : fmtSpan(w.from, w.to);
    setCurrent(label);
    if (!r.ok || !r.data) {
      S.view = null; draw();
      setStageMsg(`<p>${escapeHtml(errText(r, 'this period'))}</p>`);
      setStatus(errText(r, 'this period'), true);
      setState('error');
      return;
    }
    const d = r.data;
    S.loaded = { type: 'aggregate', ids: d.snapshot_ids || [], observed: !!d.observed, peak_value: isNum(d.peak_value) ? d.peak_value : null,
      total_value: d.total_value, recorded_hours: d.recorded_hours, from: d.from, to: d.to, unit: d.unit };
    S.view = d.observed ? { mode: 'seq', matrix: d.density_matrix, gw: d.grid_width, gh: d.grid_height, width_m: d.width_m, height_m: d.height_m } : null;
    draw();
    if (!d.snapshots_used) {
      setStageMsg(`<p>${escapeHtml('Nothing recorded in this period.')}</p>`);
      nothingInPeriod();
      el('hmFacts').innerHTML = '';
      renderLegend(null);
      setStatus('');
      setState('empty');
      return;
    }
    el('hmEmpty').innerHTML = '';
    setStageMsg(d.observed ? '' : `<p>${escapeHtml(`Quiet: ${d.recorded_hours} recorded hour(s) in this period and nobody was seen here.`)}</p>`);
    renderLegend(d.observed ? 'seq' : null, d);
    renderFacts([
      ['Total', fmtValue(d.total_value, d.unit), kindTotalHint()],
      ['Busiest spot', d.observed ? `${fmtValue(d.peak_value, d.unit)}${peakPlace(d) ? ' ' + peakPlace(d) : ''}` : DASH],
      ['Recorded hours', isNum(d.recorded_hours) ? `${d.recorded_hours} h` : DASH, 'Hours with a recorded heatmap in this period; other hours were not recorded'],
      ['Snapshots used', String(d.snapshots_used || 0)],
      d.snapshots_skipped_shape_change ? ['Left out', `${d.snapshots_skipped_shape_change} snapshot(s)`, 'Recorded before the store map was resized'] : null,
    ]);
    setStatus('');
    setState('ready');
  }

  async function loadCompare(my) {
    const c = S.compare;
    const q = scopeParams({ a_from: c.a_from, a_to: c.a_to, b_from: c.b_from, b_to: c.b_to, bucket: 'hour' });
    const r = await api(`${API}/compare?${q}`);
    if (my !== S.seq) return;
    setCurrent(c.label || 'Comparison');
    el('hmEmpty').innerHTML = '';
    if (!r.ok || !r.data) {
      S.view = null; draw();
      setStageMsg(`<p>${escapeHtml(errText(r, 'the comparison'))}</p>`);
      setStatus(errText(r, 'the comparison'), true);
      setState('error');
      return;
    }
    const d = r.data;
    S.loaded = { type: 'compare', observed: !!d.observed, peak_value: isNum(d.peak_abs_difference) ? d.peak_abs_difference : null,
      ids: d.b_summary ? d.b_summary.snapshot_ids : [], a_ids: d.a_summary ? d.a_summary.snapshot_ids : [],
      change_pct: isNum(d.change_pct) ? d.change_pct : null, a: d.a, b: d.b, unit: d.unit };
    if (!d.observed) {
      S.view = null; draw();
      setStageMsg(`<p>${escapeHtml(d.message || 'Nothing to compare.')}</p>`);
      el('hmFacts').innerHTML = '';
      renderLegend(null);
      setStatus('');
      setState('empty');
      return;
    }
    S.view = d.difference_normalised
      ? { mode: 'div', matrix: d.difference_normalised, gw: d.grid_width, gh: d.grid_height, width_m: d.width_m, height_m: d.height_m, shift: d.hotspot_shift }
      : { mode: 'div', matrix: null, gw: d.grid_width, gh: d.grid_height, width_m: d.width_m, height_m: d.height_m, shift: d.hotspot_shift };
    draw();
    setStageMsg(d.difference_normalised ? '' : `<p>${escapeHtml('No difference between the two periods.')}</p>`);
    renderLegend('div', d);
    const pct = isNum(d.change_pct) ? `${d.change_pct > 0 ? '+' : ''}${d.change_pct}%` : DASH;
    renderFacts([
      ['Change per recorded hour', pct, isNum(d.change_pct) ? 'After (B) against before (A), per recorded hour so periods of different length compare fairly' : 'Not measurable: the earlier period had no activity to compare against'],
      ['Before (A)', `${d.a_summary.recorded_hours} recorded h · ${fmtValue(d.a_summary.mean_per_hour, d.unit)} per hour`],
      ['After (B)', `${d.b_summary.recorded_hours} recorded h · ${fmtValue(d.b_summary.mean_per_hour, d.unit)} per hour`],
      ['Busiest area', shiftWords(d.hotspot_shift)],
    ]);
    setStatus('');
    setState('compare');
  }

  function shiftWords(sh) {
    if (!sh) return `${DASH} (one of the periods has too little activity to place its busiest area)`;
    const dx = sh.b[0] - sh.a[0], dy = sh.b[1] - sh.a[1];
    const small = sh.unit === 'm' ? sh.distance < 1 : sh.distance < 0.05;
    if (small) return 'Stayed in the same place';
    const dir = [];
    if (Math.abs(dy) > Math.abs(dx) * 0.4) dir.push(dy > 0 ? (sh.unit === 'm' ? 'down the map' : 'lower in the picture') : (sh.unit === 'm' ? 'up the map' : 'higher in the picture'));
    if (Math.abs(dx) > Math.abs(dy) * 0.4) dir.push(dx > 0 ? 'to the right' : 'to the left');
    const dist = sh.unit === 'm' ? `${sh.distance} m` : `about ${Math.round(sh.distance * 100)}% of the picture`;
    let where = '';
    if (sh.unit === 'm') {
      const za = zoneAt(sh.a[0], sh.a[1]), zb = zoneAt(sh.b[0], sh.b[1]);
      if (za && zb && za !== zb) where = ` (from ${za} to ${zb})`;
      else if (zb) where = ` (in ${zb})`;
    }
    return `Moved ${dist} ${dir.join(' and ')}${where}`;
  }

  function zoneAt(px, py) {
    const zones = (S.layout && S.layout.zones) || [];
    const inside = (poly) => {
      let c = false;
      for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
        const a = poly[i], b = poly[j];
        if (((a.y > py) !== (b.y > py)) && (px < ((b.x - a.x) * (py - a.y)) / ((b.y - a.y) || 1e-9) + a.x)) c = !c;
      }
      return c;
    };
    const z = zones.find((zz) => Array.isArray(zz.polygon) && zz.polygon.length > 2 && inside(zz.polygon));
    return z ? z.name : null;
  }

  /** Busiest cell in plain words: zone name (floor) or part of the picture. */
  function peakPlace(d) {
    const m = d.density_matrix, gw = d.grid_width, gh = d.grid_height;
    if (!m || !gw || !gh) return '';
    let best = -1, bx = 0, by = 0;
    for (let y = 0; y < gh; y++) for (let x = 0; x < gw; x++) if (m[y][x] > best) { best = m[y][x]; bx = x; by = y; }
    if (S.space === 'floor') {
      const W = d.width_m || (S.layout && S.layout.width_m), H = d.height_m || (S.layout && S.layout.height_m);
      if (!W || !H) return '';
      const px = ((bx + 0.5) / gw) * W, py = ((by + 0.5) / gh) * H;
      const z = zoneAt(px, py);
      return z ? `in ${z}` : `near ${px.toFixed(0)} m, ${py.toFixed(0)} m`;
    }
    const col = ['left', 'centre', 'right'][Math.min(2, Math.floor(((bx + 0.5) / gw) * 3))];
    const row = ['top', 'middle', 'bottom'][Math.min(2, Math.floor(((by + 0.5) / gh) * 3))];
    return `at the ${row === 'middle' && col === 'centre' ? 'centre' : `${row} ${col}`} of the picture`;
  }

  function renderFacts(rows) {
    const dl = el('hmFacts');
    if (!dl) return;
    dl.innerHTML = rows.filter(Boolean).map(([k, v, tip]) => `<div class="hm-fact"${tip ? ` title="${escapeHtml(tip)}"` : ''}><dt>${escapeHtml(k)}</dt><dd${v === DASH ? ' class="metric-unobserved"' : ''}>${escapeHtml(v)}</dd></div>`).join('');
  }

  function renderLegend(mode, d) {
    const n = el('hmLegend');
    if (!n) return;
    if (mode === 'seq') {
      n.innerHTML = `<div class="hm-legend-row"><span>Quiet</span><span class="hm-ramp hm-ramp-seq" aria-hidden="true"></span><span>Busy</span></div>
        <div class="hm-note">${escapeHtml(kindInfo(S.kind).title)}. Colour is relative to the busiest spot (${escapeHtml(fmtValue(d && d.peak_value, d && d.unit))}).</div>`;
    } else if (mode === 'div') {
      n.innerHTML = `<div class="hm-legend-row"><span>Less than before</span><span class="hm-ramp hm-ramp-div" aria-hidden="true"></span><span>More than before</span></div>
        <div class="hm-legend-mid">No change (no tint)</div>
        <div class="hm-note">After (B) minus before (A), per recorded hour. The arrow shows where the busiest area moved.</div>`;
    } else {
      n.innerHTML = '';
    }
  }

  // ------------------------------------------------------------ drawing

  function spaceAspect() {
    if (S.space === 'image') {
      const bg = S.backgrounds.get(S.cameraId);
      return bg && bg.aspect ? bg.aspect : 16 / 9;
    }
    const L = S.layout;
    if (L && L.width_m > 0 && L.height_m > 0) return L.width_m / L.height_m;
    const v = S.view;
    if (v && v.width_m > 0 && v.height_m > 0) return v.width_m / v.height_m;
    return 16 / 9;
  }

  function draw() {
    const stage = el('hmStage'), canvas = el('hmCanvas');
    if (!stage || !canvas) return;
    const aspect = Math.max(0.2, Math.min(8, spaceAspect()));
    const avail = stage.clientWidth;
    if (!avail) return;   // hidden: drawn again when the ResizeObserver sees a width
    const maxH = Math.max(240, Math.round(window.innerHeight * 0.62));
    const cssW = avail;
    const cssH = Math.max(180, Math.min(maxH, Math.round(cssW / aspect)));
    const dpr = window.devicePixelRatio || 1;
    canvas.style.width = `${cssW}px`;
    canvas.style.height = `${cssH}px`;
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const P = palette();
    ctx.fillStyle = P.bg;
    ctx.fillRect(0, 0, cssW, cssH);

    // Content rect: the space, fitted (contain) into the canvas.
    let rw = cssW, rh = cssW / aspect;
    if (rh > cssH) { rh = cssH; rw = rh * aspect; }
    const rx = (cssW - rw) / 2, ry = (cssH - rh) / 2;
    const rect = { x: rx, y: ry, w: rw, h: rh };

    if (S.space === 'image') drawCameraBackground(ctx, rect, P);
    else drawFloorBackground(ctx, rect, P);

    const v = S.view;
    if (v && v.matrix && v.gw && v.gh) {
      const heat = heatImage(v.matrix, v.gw, v.gh, v.mode, P);
      let hr = rect;
      if (S.space === 'floor') {
        const W = (S.layout && S.layout.width_m) || v.width_m, H = (S.layout && S.layout.height_m) || v.height_m;
        if (W && H && v.width_m && v.height_m) hr = { x: rect.x, y: rect.y, w: rect.w * (v.width_m / W), h: rect.h * (v.height_m / H) };
      }
      ctx.save();
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = 'high';
      ctx.drawImage(heat, hr.x, hr.y, hr.w, hr.h);
      ctx.restore();
      if (S.space === 'floor') drawZoneOutlines(ctx, rect, P, true);
    }
    if (v && v.mode === 'div' && v.shift) drawShift(ctx, rect, v, P);
    canvas.setAttribute('aria-label', `Heatmap of ${whereLabel()}: ${kindInfo(S.kind).label}`);
  }

  function heatImage(m, gw, gh, mode, P) {
    const off = document.createElement('canvas');
    off.width = gw; off.height = gh;
    const g = off.getContext('2d');
    const img = g.createImageData(gw, gh);
    const lo = P.seqLo, hi = P.seqHi, less = P.divLess, more = P.divMore;
    for (let y = 0; y < gh; y++) {
      const row = m[y] || [];
      for (let x = 0; x < gw; x++) {
        const v = Number(row[x]) || 0;
        const o = (y * gw + x) * 4;
        if (mode === 'div') {
          const a = Math.abs(v);
          if (a <= 0.03) continue;
          const c = v < 0 ? less : more;
          img.data[o] = c[0]; img.data[o + 1] = c[1]; img.data[o + 2] = c[2];
          img.data[o + 3] = Math.round(255 * (0.18 + 0.7 * Math.min(1, a)));
        } else {
          if (v <= 0.02) continue;
          const t = Math.min(1, v);
          img.data[o] = Math.round(lo[0] + (hi[0] - lo[0]) * t);
          img.data[o + 1] = Math.round(lo[1] + (hi[1] - lo[1]) * t);
          img.data[o + 2] = Math.round(lo[2] + (hi[2] - lo[2]) * t);
          img.data[o + 3] = Math.round(255 * (0.22 + 0.63 * t));
        }
      }
    }
    g.putImageData(img, 0, 0);
    return off;
  }

  function drawCameraBackground(ctx, r, P) {
    const bg = S.backgrounds.get(S.cameraId);
    if (bg && bg.img && !bg.noSignal) {
      ctx.drawImage(bg.img, r.x, r.y, r.w, r.h);
      return;
    }
    ctx.strokeStyle = P.outline;
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.strokeRect(r.x + 0.5, r.y + 0.5, r.w - 1, r.h - 1);
    ctx.setLineDash([]);
    ctx.fillStyle = P.text;
    ctx.font = '600 12px "Plus Jakarta Sans", system-ui, sans-serif';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'top';
    ctx.fillText('Camera picture unavailable right now', r.x + 10, r.y + 10);
  }

  function drawFloorBackground(ctx, r, P) {
    const L = S.layout;
    ctx.strokeStyle = P.outline;
    ctx.lineWidth = 1.5;
    ctx.strokeRect(r.x + 0.75, r.y + 0.75, r.w - 1.5, r.h - 1.5);
    if (!L || !(L.width_m > 0)) {
      ctx.fillStyle = P.text;
      ctx.font = '600 12px "Plus Jakarta Sans", system-ui, sans-serif';
      ctx.textBaseline = 'top';
      ctx.fillText('Store map not drawn yet', r.x + 10, r.y + 10);
      return;
    }
    const sx = r.w / L.width_m, sy = r.h / L.height_m;
    const pt = (p) => [r.x + p.x * sx, r.y + p.y * sy];
    ctx.save();
    ctx.strokeStyle = P.structure;
    (L.structures || []).forEach((st) => {
      const poly = st.polygon || [];
      if (poly.length < 2) return;
      ctx.beginPath();
      poly.forEach((p, i) => { const [x, y] = pt(p); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
      const line = st.kind === 'WALL' || st.kind === 'DOOR';
      if (!line) ctx.closePath();
      ctx.lineWidth = st.kind === 'WALL' ? 2 : 1;
      ctx.stroke();
    });
    ctx.restore();
    drawZoneOutlines(ctx, r, P, false);
  }

  function drawZoneOutlines(ctx, r, P, labelsOnly) {
    const L = S.layout;
    if (!L || !(L.width_m > 0)) return;
    const sx = r.w / L.width_m, sy = r.h / L.height_m;
    ctx.save();
    ctx.font = '600 11px "Plus Jakarta Sans", system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    (L.zones || []).forEach((z) => {
      const poly = z.polygon || [];
      if (poly.length < 3) return;
      if (!labelsOnly) {
        ctx.beginPath();
        poly.forEach((p, i) => { const x = r.x + p.x * sx, y = r.y + p.y * sy; if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
        ctx.closePath();
        ctx.setLineDash([5, 4]);
        ctx.strokeStyle = P.zone;
        ctx.lineWidth = 1;
        ctx.stroke();
        ctx.setLineDash([]);
      }
      const cx = poly.reduce((a, p) => a + p.x, 0) / poly.length, cy = poly.reduce((a, p) => a + p.y, 0) / poly.length;
      const tx = r.x + cx * sx, ty = r.y + cy * sy;
      ctx.lineWidth = 3;
      ctx.strokeStyle = P.bg;
      ctx.strokeText(z.name || '', tx, ty);
      ctx.fillStyle = P.zone;
      ctx.fillText(z.name || '', tx, ty);
    });
    ctx.restore();
  }

  function drawShift(ctx, r, v, P) {
    const sh = v.shift;
    if (!sh || !sh.a || !sh.b) return;
    let map;
    if (sh.unit === 'm') {
      const W = (S.layout && S.layout.width_m) || v.width_m, H = (S.layout && S.layout.height_m) || v.height_m;
      if (!W || !H) return;
      map = (p) => [r.x + (p[0] / W) * r.w, r.y + (p[1] / H) * r.h];
    } else {
      map = (p) => [r.x + p[0] * r.w, r.y + p[1] * r.h];
    }
    const [ax, ay] = map(sh.a), [bx, by] = map(sh.b);
    ctx.save();
    ctx.strokeStyle = P.arrow;
    ctx.fillStyle = P.arrow;
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(ax, ay, 6, 0, Math.PI * 2); ctx.stroke();
    const len = Math.hypot(bx - ax, by - ay);
    if (len > 10) {
      const ang = Math.atan2(by - ay, bx - ax);
      ctx.beginPath(); ctx.moveTo(ax + Math.cos(ang) * 6, ay + Math.sin(ang) * 6); ctx.lineTo(bx, by); ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(bx, by);
      ctx.lineTo(bx - 10 * Math.cos(ang - 0.45), by - 10 * Math.sin(ang - 0.45));
      ctx.lineTo(bx - 10 * Math.cos(ang + 0.45), by - 10 * Math.sin(ang + 0.45));
      ctx.closePath(); ctx.fill();
    }
    ctx.restore();
  }

  // ------------------------------------------------------------ play

  function togglePlay() {
    if (S.playTimer) { stopPlay(); setStatus('Paused.'); return; }
    if (S.compare || !S.model) return;
    const recorded = S.model.cells.filter((c) => c.snap);
    if (!recorded.length) { setStatus('Nothing recorded on this day to play.'); return; }
    S.range = 'hour';
    const startAt = S.hour !== null && recorded.some((c) => c.index > S.hour) ? S.hour : -1;
    S.playTimer = setInterval(stepPlay, PLAY_STEP_MS);
    syncControls();
    stepPlay(startAt);
  }

  function stepPlay(fromIndex) {
    if (!S.model) { stopPlay(); return; }
    const cur = isNum(fromIndex) ? fromIndex : S.hour;
    const next = S.model.cells.find((c) => c.index > cur && c.snap);
    if (!next) { stopPlay(); setStatus('Played to the last recorded hour of this day.'); return; }
    const skipped = S.model.cells.filter((c) => c.index > cur && c.index < next.index && c.state === 'off').length;
    S.hour = next.index;
    load().then(() => {
      if (!S.playTimer) return;
      setStatus(skipped ? `Playing · skipped ${skipped} hour${skipped === 1 ? '' : 's'} not recorded (camera off)` : 'Playing the recorded hours of this day…');
    });
  }

  function stopPlay() {
    if (S.playTimer) clearInterval(S.playTimer);
    S.playTimer = null;
    syncControls();
  }

  // ------------------------------------------------------------ compare

  function toggleCompare() {
    S.compareOpen = !S.compareOpen;
    if (!S.compareOpen && S.compare) { S.compare = null; load(); }
    if (S.compareOpen && !S.compare) setStatus('Pick two periods to compare: before (A) and after (B).');
    stopPlay();
    syncControls();
  }

  function presetWindow(id) {
    const t = todayStr();
    if (id === 'week') return { a: [addDays(t, -13), addDays(t, -7)], b: [addDays(t, -6), t], label: 'Last 7 days vs the 7 days before' };
    if (id === 'sameday') return { a: [addDays(t, -7), addDays(t, -7)], b: [t, t], label: `Today vs ${fmtDay(addDays(t, -7))}` };
    if (id === 'yesterday') return { a: [addDays(t, -2), addDays(t, -2)], b: [addDays(t, -1), addDays(t, -1)], label: `${fmtDay(addDays(t, -1))} vs ${fmtDay(addDays(t, -2))}` };
    return null;
  }

  function setCompareDates(a, b) {
    el('hmCmpAFrom').value = a[0]; el('hmCmpATo').value = a[1];
    el('hmCmpBFrom').value = b[0]; el('hmCmpBTo').value = b[1];
  }

  function applyPreset(id) {
    const custom = el('hmCustom');
    if (id === 'custom') {
      custom.hidden = false;
      if (!el('hmCmpBFrom').value) { const w = presetWindow('week'); setCompareDates(w.a, w.b); }
      return;
    }
    custom.hidden = true;
    const w = presetWindow(id);
    setCompareDates(w.a, w.b);
    S.compare = { preset: id, label: w.label, a_from: startOf(w.a[0]), a_to: startOf(addDays(w.a[1], 1)), b_from: startOf(w.b[0]), b_to: startOf(addDays(w.b[1], 1)) };
    stopPlay(); load();
  }

  function applyCustomCompare() {
    const af = el('hmCmpAFrom').value, at = el('hmCmpATo').value, bf = el('hmCmpBFrom').value, bt = el('hmCmpBTo').value;
    if (!af || !at || !bf || !bt) { setStatus('Choose all four dates to compare.', true); return; }
    if (at < af || bt < bf) { setStatus('Each period must end on or after its start date.', true); return; }
    S.compare = { preset: 'custom', label: `${fmtSpan(bf, bt)} vs ${fmtSpan(af, at)}`,
      a_from: startOf(af), a_to: startOf(addDays(at, 1)), b_from: startOf(bf), b_to: startOf(addDays(bt, 1)) };
    stopPlay(); load();
  }

  // ------------------------------------------------------------ record now

  async function recordNow(e) {
    const btn = e.currentTarget;
    if (btn.disabled) return;
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Recording…';
    btn.setAttribute('aria-busy', 'true');
    setStatus('Recording the current hour so far…');
    const r = await api(`${API}/record-now`, { method: 'POST' });
    btn.disabled = false;
    btn.textContent = label;
    btn.removeAttribute('aria-busy');
    if (!r.ok || !r.data) {
      const d = r.data && r.data.detail;
      const msg = typeof d === 'string' ? `Record now failed: ${d}`
        : r.status === 0 ? 'Record now failed: the server did not respond.' : `Record now failed (HTTP ${r.status}).`;
      setStatus(msg, true);
      showToast(msg, 'error');
      return;
    }
    const n = isNum(r.data.rows) ? r.data.rows : 0;
    if (n > 0) {
      showToast(`Recorded the current hour so far (${n} heatmap${n === 1 ? '' : 's'})`, 'ok');
      setStatus(r.data.note || 'Recorded the current hour so far.');
    } else {
      showToast('Nothing to record yet this hour: no camera measured anything', 'error');
      setStatus('Nothing was recorded: no camera measured anything this hour yet.', true);
    }
    S.snapCache.clear();
    if (S.date === todayStr() && !S.compare) { S.range = 'hour'; S.wantCurrentHour = true; }
    await load({ quiet: true });
  }

  // ------------------------------------------------------------ jump to latest

  async function jumpLatest(btn) {
    if (btn) { btn.disabled = true; btn.textContent = 'Looking…'; }
    let found = null;
    for (const [days, bucket] of [[2, 'hour'], [14, 'hour'], [40, 'hour'], [390, 'day']]) {
      const q = scopeParams({ from: startOf(addDays(todayStr(), -days)), bucket });
      const r = await api(`${API}/history?${q}`);
      const snaps = r.ok && r.data ? r.data.snapshots || [] : [];
      if (snaps.length) { found = { snap: snaps[snaps.length - 1], bucket }; break; }
    }
    if (!found) {
      if (btn) { btn.disabled = false; btn.textContent = 'Show the latest recorded hour'; }
      setStatus('No recorded heatmap found for this place and kind.', true);
      return;
    }
    const local = found.snap.bucket_start_local || '';
    S.date = local.slice(0, 10) || S.date;
    S.compare = null;
    if (found.bucket === 'hour') { S.range = 'hour'; S.hour = parseInt(local.slice(11, 13), 10) || 0; }
    else { S.range = 'day'; S.hour = null; }
    stopPlay();
    load();
  }

  // ------------------------------------------------------------ hour profile chart

  async function loadProfile() {
    const key = `${S.space}|${S.cameraId || ''}|${S.kind}`;
    S.profileKey = key;
    S.profileAt = Date.now();
    const r = await api(`${API}/hour-profile?${scopeParams({ days: 14 })}`);
    if (key !== S.profileKey) return;
    S.profile = r.ok ? r.data : null;
    renderProfile(r);
  }

  const noDataPlugin = {
    id: 'hmNoData',
    afterDatasetsDraw(chart) {
      const meta = chart.$hm;
      if (!meta) return;
      const { ctx, chartArea, scales } = chart;
      if (!scales.x || !chartArea) return;
      const step = meta.missing.length > 1 ? Math.abs(scales.x.getPixelForValue(1) - scales.x.getPixelForValue(0)) : chartArea.width;
      ctx.save();
      ctx.strokeStyle = meta.color;
      ctx.lineWidth = 1.5;
      meta.missing.forEach((miss, i) => {
        if (!miss) return;
        const cx = scales.x.getPixelForValue(i), w = step * 0.6, y = chartArea.bottom - 7;
        ctx.strokeRect(cx - w / 2, y, w, 6);
        ctx.beginPath(); ctx.moveTo(cx - w / 2, y + 6); ctx.lineTo(cx + w / 2, y); ctx.stroke();
      });
      ctx.restore();
    },
  };

  function renderProfile(r) {
    const note = el('hmProfileNote');
    const canvas = el('hmProfileChart');
    if (!canvas) return;
    if (S.chart) { try { S.chart.destroy(); } catch (_) { /* already gone */ } S.chart = null; }
    const d = S.profile;
    if (!d) {
      if (note) note.textContent = errText(r, 'the hour-of-day pattern');
      return;
    }
    const hours = d.hours || [];
    const recordedHours = hours.filter((h) => h.mean_value !== null && h.mean_value !== undefined).length;
    if (note) {
      note.textContent = !d.observed
        ? `No recorded hours for ${whereLabel()} in the last 14 days, so there is no typical day yet.`
        : `Average ${unitWord(d.unit)} per recorded hour, over the days that recorded it. ${d.peak_hour ? `Usually busiest at ${d.peak_hour}.` : ''} ${24 - recordedHours > 0 ? `${24 - recordedHours} hour(s) of the day have no data (never recorded), shown as an empty box, not zero.` : ''}`.trim();
    }
    if (typeof Chart === 'undefined') return;
    const ct = chartTheme();
    const barColour = token('--hm-profile-bar') || ct.today || ct.line;
    const cfg = {
      type: 'bar',
      data: {
        labels: hours.map((h) => h.label),
        datasets: [{
          label: `Average ${unitWord(d.unit)}`,
          data: hours.map((h) => (isNum(h.mean_value) ? h.mean_value : null)),
          backgroundColor: hours.map((h) => (d.peak_hour && h.label === d.peak_hour ? (token('--hm-profile-peak') || barColour) : barColour)),
          borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: 'bottom',
          barPercentage: 0.84, categoryPercentage: 1,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: (item) => {
                const h = hours[item.dataIndex];
                return h && isNum(h.mean_value) ? `Average ${fmtValue(h.mean_value, d.unit)}` : 'No data: not recorded';
              },
              footer: (items) => {
                const h = hours[items[0] ? items[0].dataIndex : -1];
                if (!h) return '';
                return h.days_recorded ? `Recorded on ${h.days_recorded} of the last ${d.days} days` : `Not recorded on any of the last ${d.days} days`;
              },
            },
          },
        },
        scales: {
          x: { grid: { display: false }, ticks: { color: ct.tick, maxRotation: 0, autoSkip: true, autoSkipPadding: 6, font: { size: 10 } } },
          y: { grid: { color: ct.grid }, border: { display: false }, beginAtZero: true,
            ticks: { color: ct.tick, font: { size: 10 }, callback: (v) => (d.unit === 'seconds' ? formatDuration(v) : v) } },
        },
      },
      plugins: [noDataPlugin],
    };
    const chart = new Chart(canvas.getContext('2d'), cfg);
    chart.$hm = { missing: hours.map((h) => !isNum(h.mean_value)), color: ct.nodata || token('--hm-off') || ct.tick };
    chart.update('none');
    S.chart = chart;
  }

  // ------------------------------------------------------------ recommendations

  function isHeatmapItem(i) {
    return (typeof i.category === 'string' && i.category.startsWith('HEATMAP_')) || i.source === 'heatmap_trends';
  }

  async function loadRecs() {
    S.recsAt = Date.now();
    const r = await api('/api/v1/analytics/recommendations?days=7');
    if (!r.ok || !r.data) {
      el('hmRecs').innerHTML = `<div class="fp-empty">${escapeHtml(errText(r, 'the recommendations'))}</div>`;
      return;
    }
    S.recs = (r.data.items || []).filter(isHeatmapItem);
    if (!S.recs.length) {
      const a = await api('/api/v1/analytics/business/analysis/latest');
      const run = a.ok && a.data ? a.data.run : null;
      const hm = run && run.result && run.result.heatmap_trends ? run.result.heatmap_trends.summary : null;
      S.analysisSummary = hm;
    }
    renderRecs();
  }

  function periodSpec(p) { return p && p.from && p.to ? { from: p.from, to: p.to } : null; }

  function citesFor(item) {
    const ev = item.evidence || {};
    const cat = item.category || '';
    const period = periodSpec(ev.period);
    const onCam = ev.camera_id ? { space: 'image', camera_id: ev.camera_id } : { space: 'floor' };
    const out = [];
    const periodBtn = (kind) => period && out.push({
      text: `Seen ${fmtSpan(period.from, period.to)}`,
      spec: Object.assign({}, onCam, { kind, range: 'period', from: period.from, to: period.to, compare: null }),
    });
    if (cat === 'HEATMAP_HOTSPOT_SHIFT') {
      const prev = periodSpec(ev.previous_period);
      if (period && prev) {
        out.push({
          text: `Compare ${fmtSpan(prev.from, prev.to)} with ${fmtSpan(period.from, period.to)}`,
          spec: { space: 'floor', kind: 'presence', compare: {
            a_from: startOf(prev.from), a_to: startOf(addDays(prev.to, 1)),
            b_from: startOf(period.from), b_to: startOf(addDays(period.to, 1)),
            label: `${fmtSpan(period.from, period.to)} vs ${fmtSpan(prev.from, prev.to)}` } },
        });
      }
      periodBtn('presence');
    } else if (cat === 'HEATMAP_DEAD_SPACE') {
      periodBtn('presence');
    } else if (cat === 'HEATMAP_CONGESTION') {
      periodBtn('dwell');
      const m = /^(\d{1,2})/.exec(ev.peak_hour_local || '');
      if (m && period) {
        out.push({
          text: `peak ${ev.peak_hour_local}`,
          spec: Object.assign({}, onCam, { kind: 'dwell', range: 'hour', date: period.to, hour: parseInt(m[1], 10), compare: null }),
        });
      }
    } else if (cat === 'HEATMAP_BROWSE_NO_TOUCH') {
      periodBtn('dwell');
      if (period) out.push({ text: 'Shelf reaches then', spec: Object.assign({}, onCam, { kind: 'interaction', range: 'period', from: period.from, to: period.to, compare: null }) });
    } else {
      periodBtn(S.kind);
    }
    return out;
  }

  function renderRecs() {
    const host = el('hmRecs');
    if (!host) return;
    const items = S.recs || [];
    if (!items.length) {
      const s = S.analysisSummary;
      let msg;
      if (s && isNum(s.days_required)) {
        msg = `No heatmap findings yet. They need at least ${s.days_required} days of recorded store-map history`
          + (isNum(s.days_with_floor_history) ? ` (${s.days_with_floor_history} recorded so far).` : '.');
      } else {
        msg = 'No heatmap findings yet. They appear once enough days of heatmap history are recorded and an analysis has run.';
      }
      host.innerHTML = `<div class="fp-empty">${escapeHtml(msg)}</div>`;
      return;
    }
    host.innerHTML = '';
    items.forEach((it) => {
      const card = document.createElement('div');
      card.className = 'action-card hm-rec';
      card.innerHTML = `
        <div class="action-card-header">
          <span class="badge ${it.priority === 'high' || it.priority === 'critical' ? 'badge-danger' : it.priority === 'medium' ? 'badge-warning' : 'badge-neutral'}">${escapeHtml((it.priority || '').toUpperCase() || DASH)}</span>
          <span class="action-title">${escapeHtml(it.title || DASH)}</span>
        </div>
        ${it.do ? `<div class="action-desc"><strong>Do:</strong> ${escapeHtml(it.do)}</div>` : ''}
        <div class="action-meta">${escapeHtml([it.zone, it.evidence && it.evidence.camera_id ? cameraName(it.evidence.camera_id) : null].filter(Boolean).join(' · '))}</div>
        <div class="hm-cites"></div>`;
      const cites = card.querySelector('.hm-cites');
      const list = citesFor(it);
      if (!list.length) cites.innerHTML = `<span class="hm-note">${it.evidence_available === false ? 'Evidence not recorded for older findings' : 'No period cited'}</span>`;
      list.forEach((c) => {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'btn btn-xs hm-cite';
        b.dataset.hmCite = '1';
        b.textContent = c.text;
        b.title = 'Show this on the heatmap above';
        b._spec = c.spec;
        cites.appendChild(b);
      });
      host.appendChild(card);
    });
  }

  // ------------------------------------------------------------ public

  function show(opts) {
    const o = opts || {};
    if (o.space === 'floor') { S.space = 'floor'; S.cameraId = null; }
    if (o.space === 'image' || (o.camera_id && o.space !== 'floor')) { S.space = 'image'; S.cameraId = o.camera_id || S.cameraId; }
    if (KINDS.some((k) => k.kind === o.kind)) S.kind = o.kind;
    if (o.date) S.date = String(o.date).slice(0, 10);
    if (['hour', 'day', 'week', 'period'].includes(o.range)) S.range = o.range;
    if (S.range === 'period') {
      S.periodFrom = o.from ? String(o.from).slice(0, 10) : S.periodFrom;
      S.periodTo = o.to ? String(o.to).slice(0, 10) : S.periodTo;
      if (!S.periodFrom || !S.periodTo) S.range = 'day';
      else S.date = S.periodTo;
    }
    if (o.hour !== undefined && o.hour !== null) { S.hour = parseInt(o.hour, 10); S.range = o.range || 'hour'; }
    if (o.compare) {
      const c = o.compare;
      S.compare = { preset: c.preset || 'custom', label: c.label || 'Comparison', a_from: c.a_from, a_to: c.a_to, b_from: c.b_from, b_to: c.b_to };
      S.compareOpen = true;
    } else if (o.compare === null || o.compare === false || o.range) {
      S.compare = null;
    }
    stopPlay();
    return load();
  }

  function state() {
    return {
      space: S.space, camera_id: S.cameraId, kind: S.kind, range: S.range, date: S.date, hour: S.hour,
      from: S.range === 'period' ? S.periodFrom : null, to: S.range === 'period' ? S.periodTo : null,
      compare: S.compare ? Object.assign({}, S.compare) : null,
      playing: !!S.playTimer,
      data_state: S.dataState,
      loaded: S.loaded ? Object.assign({}, S.loaded) : null,
    };
  }

  function refresh() {
    S.snapCache.clear();
    S.backgrounds.forEach((b) => { if (b.url) URL.revokeObjectURL(b.url); });
    S.backgrounds.clear();
    S.profileAt = 0; S.recsAt = 0; S.recs = null;
    return load({ force: true });
  }

  function onShown() {
    if (!build()) return;
    load();
  }

  function init() {
    if (!build()) return;
    setState('loading');
    window.addEventListener('edge:tab', (e) => {
      const d = e.detail || {};
      if (d.tab === 'insights' && d.sub === 'footfall') setTimeout(onShown, 30);
      else stopPlay();
    });
    window.addEventListener('edge:theme', () => {
      draw();
      if (S.profile) renderProfile({ ok: true, data: S.profile });
    });
    if (isShown()) onShown();
    setInterval(() => {
      if (!isShown() || S.playTimer || (typeof canPoll === 'function' && !canPoll())) return;
      if (S.compare) return;   // a comparison of past periods does not change minute to minute
      load({ quiet: true });
    }, HISTORY_POLL_MS);
  }

  window.edgeHeatmapHistory = { refresh, show, state };

  if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') {
    window.edgeAuth.onReady(init);
  } else if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
