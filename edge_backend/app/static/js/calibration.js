/**
 * Camera-to-floor calibration.
 *
 * A camera can only place people on the plan once a homography maps its image
 * pixels to floor metres. The operator builds that mapping here by pairing
 * points: click a floor mark on the live frame, then click the same spot on
 * the plan. Four or more pairs are solved server-side and stored; the tool
 * then reprojects the same image points through the stored homography so the
 * result can be checked visually against the points that were clicked.
 *
 * Image points are always in the camera's NATIVE pixel space (naturalWidth x
 * naturalHeight of the frame), never in the displayed size.
 */
(function () {
  'use strict';

  const API = '/api/v1/layout';
  const MIN_PAIRS = 4;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  const fmt = (v, d = 2) => (typeof v === 'number' && isFinite(v) ? v.toFixed(d) : '—');

  const calibrationTool = {
    cameraId: null,
    camera: null,
    pairs: [],            // [{image:{x,y}, floor:{x,y}}] in native px / metres
    pending: null,        // {x,y} image point waiting for its plan point
    reprojected: null,    // [{x,y}|null] aligned with pairs, after a test
    frameW: null,
    frameH: null,
    hasHomography: false,
    frameSource: null,    // 'live' | 'no-signal' | null
    _ro: null,
    _lastResult: null,

    get editor() { return window.blueprintEditor || null; },
    get panel() { return document.getElementById('fpCalibrationPanel'); },

    isOpen() { return !!this.cameraId; },

    /** The live MJPEG feed shown while calibrating (raw frames, no overlay). */
    frameUrl() { return `/stream?camera_id=${encodeURIComponent(this.cameraId)}&fps=4&_=${Date.now()}`; },

    async open(cameraId) {
      const editor = this.editor;
      const panel = this.panel;
      if (!panel || !editor) return;
      if (this.cameraId && this.cameraId !== cameraId) this.close();

      this.cameraId = cameraId;
      this.camera = (editor.cameras || []).find((c) => c.camera_id === cameraId) || { camera_id: cameraId, name: cameraId };
      this.pairs = [];
      this.pending = null;
      this.reprojected = null;
      this._lastResult = null;
      this._confirmingClear = false;
      this.frameW = this.camera.frame_width || null;
      this.frameH = this.camera.frame_height || null;
      this.hasHomography = !!this.camera.has_homography;

      const layout = document.getElementById('fpLayout');
      if (layout) layout.classList.add('fp-cal-open');
      editor.selectCamera(cameraId);
      this.render();
      requestAnimationFrame(() => editor.resize());

      await Promise.all([this.loadExisting(), this.probeFrame()]);
      this.render();
      this.syncOverlay();
    },

    close() {
      const layout = document.getElementById('fpLayout');
      if (layout) layout.classList.remove('fp-cal-open');
      const img = document.getElementById('fpCalImg');
      if (img) {
        img.onload = null;
        img.src = '';                   // drops the MJPEG connection immediately
      }
      if (this._ro) { this._ro.disconnect(); this._ro = null; }
      if (this.editor) {
        this.editor.cancelPick();
        this.editor.setCalOverlay(null);
        requestAnimationFrame(() => this.editor.resize());
      }
      this.cameraId = null;
      this.camera = null;
      this.pairs = [];
      this.pending = null;
      this.reprojected = null;
      this._confirmingClear = false;
      if (this.panel) this.panel.innerHTML = '';
    },

    async loadExisting() {
      try {
        const res = await fetch(`${API}/cameras/${encodeURIComponent(this.cameraId)}/calibration`);
        if (!res.ok) { this._lastResult = res.status === 404 ? { kind: 'info', text: 'This server does not expose saved calibration points (GET …/calibration is missing). You can still solve and save.' } : null; return; }
        const data = await res.json();
        this.hasHomography = !!data.has_homography;
        if (data.frame_width && data.frame_height) { this.frameW = data.frame_width; this.frameH = data.frame_height; }
        const cp = data.calibration_points;
        if (cp && Array.isArray(cp.image_points) && Array.isArray(cp.floor_points) && cp.image_points.length === cp.floor_points.length) {
          this.pairs = cp.image_points.map((ip, i) => ({ image: { x: ip.x, y: ip.y }, floor: { x: cp.floor_points[i].x, y: cp.floor_points[i].y } }));
          if (cp.frame_width && cp.frame_height) { this.frameW = cp.frame_width; this.frameH = cp.frame_height; }
          this._lastResult = { kind: 'info', text: `Loaded ${this.pairs.length} saved point pairs${cp.saved_at ? ` from ${esc(String(cp.saved_at).slice(0, 16).replace('T', ' '))}` : ''}.` };
        }
      } catch (_) { /* best effort */ }
    },

    /** Find out whether the frame shown is a real one or the no-signal slate. */
    async probeFrame() {
      try {
        const res = await fetch(`/api/v1/cameras/${encodeURIComponent(this.cameraId)}/snapshot?annotate=false&_=${Date.now()}`);
        this.frameSource = res.headers.get('X-Frame-Source') || (res.ok ? 'unknown' : null);
      } catch (_) { this.frameSource = null; }
    },

    // ---------------------------------------------------------------- render

    mountShell() {
      const panel = this.panel;
      if (!panel || !this.cameraId) return;
      const cam = this.camera;

      panel.innerHTML = `
        <div class="fp-cal-head">
          <div>
            <div class="fp-cal-title" id="fpCalTitle">Calibrate ${esc(cam.name || cam.camera_id)}</div>
            <div class="fp-cal-sub">Pair at least ${MIN_PAIRS} floor points. Choose marks that are on the floor itself (tile corners, shelf feet, door thresholds), spread across the view.</div>
          </div>
          <button class="btn btn-sm" id="fpCalClose" title="Close without changing anything">Close</button>
        </div>
        <div class="fp-cal-steps" id="fpCalSteps"></div>
        <div id="fpCalSignalWarning"></div>
        <div class="fp-cal-frame" id="fpCalFrame">
          <img id="fpCalImg" alt="Live frame from ${esc(cam.name || cam.camera_id)}" src="${this.frameUrl()}">
          <canvas id="fpCalCanvas"></canvas>
          <div class="fp-cal-framesize" id="fpCalFrameSize">${this.frameW && this.frameH ? `${this.frameW}×${this.frameH} px` : 'frame size: waiting for first frame'}</div>
        </div>
        <div class="fp-cal-pairs" id="fpCalPairs"></div>
        <div id="fpCalResultWrap"></div>
        <div class="fp-actions" id="fpCalActions"></div>`;

      panel.querySelector('#fpCalClose')?.addEventListener('click', () => this.close());

      const img = panel.querySelector('#fpCalImg');
      const canvas = panel.querySelector('#fpCalCanvas');
      const fit = () => this.fitOverlay();
      img.addEventListener('load', fit);
      if (this._ro) this._ro.disconnect();
      if (window.ResizeObserver) {
        this._ro = new ResizeObserver(fit);
        this._ro.observe(img);
      }
      canvas.addEventListener('click', (e) => this.onImageClick(e));

      // Active poll for first frame dimensions arrival
      let checks = 0;
      const checkFrameArrival = () => {
        if (!this.isOpen()) return;
        if (img && img.naturalWidth > 0 && img.naturalHeight > 0) {
          this.fitOverlay();
        } else if (checks++ < 30) {
          setTimeout(checkFrameArrival, 100);
        }
      };
      checkFrameArrival();
    },

    render() {
      const panel = this.panel;
      if (!panel || !this.cameraId) return;

      if (!panel.querySelector('#fpCalFrame')) {
        this.mountShell();
      }

      const cam = this.camera;
      const n = this.pairs.length;
      const step = this.pending ? 2 : 1;
      const noSignal = this.frameSource === 'no-signal';
      const result = this._lastResult;

      const titleEl = panel.querySelector('#fpCalTitle');
      if (titleEl) titleEl.textContent = `Calibrate ${cam.name || cam.camera_id}`;

      const stepsEl = panel.querySelector('#fpCalSteps');
      if (stepsEl) {
        stepsEl.innerHTML = `
          <span class="fp-cal-step ${step === 1 ? 'active' : ''}">1 · Click a floor point on the image</span>
          <span class="fp-cal-step ${step === 2 ? 'active' : ''}">2 · Click the same point on the plan</span>
          <span class="fp-cal-step ${n >= MIN_PAIRS ? 'active' : ''}">3 · Solve &amp; save (${n}/${MIN_PAIRS})</span>`;
      }

      const warnEl = panel.querySelector('#fpCalSignalWarning');
      if (warnEl) {
        warnEl.innerHTML = noSignal ? '<div class="fp-cal-result err">This camera has no live signal right now: the frame below is a placeholder, so points clicked on it would be meaningless. Restore the feed first.</div>' : '';
      }

      const frameEl = panel.querySelector('#fpCalFrame');
      if (frameEl) {
        frameEl.classList.toggle('is-armed', step === 1 && !noSignal);
      }

      const sizeEl = panel.querySelector('#fpCalFrameSize');
      if (sizeEl && this.frameW && this.frameH) {
        sizeEl.textContent = `${this.frameW}×${this.frameH} px`;
      }

      // Check if frameUrl target has changed significantly (e.g. from selftest redirecting to snapshot)
      const img = panel.querySelector('#fpCalImg');
      if (img) {
        const targetUrl = this.frameUrl();
        const baseTarget = targetUrl.split('&_')[0].split('?_')[0];
        if (!img.src.includes(baseTarget)) {
          img.src = targetUrl;
        }
      }

      const pairsEl = panel.querySelector('#fpCalPairs');
      if (pairsEl) {
        pairsEl.innerHTML = this.renderPairs();
        pairsEl.querySelectorAll('[data-del]').forEach((b) => {
          b.addEventListener('click', (e) => {
            e.stopPropagation();
            this.removePair(parseInt(b.dataset.del, 10));
          });
        });
      }

      const resEl = panel.querySelector('#fpCalResultWrap');
      if (resEl) {
        resEl.innerHTML = result ? `<div class="fp-cal-result ${result.kind}">${result.text}</div>` : '';
      }

      this.renderActions();
      this.fitOverlay();
      if (step === 2) this.armPlanPick();
    },

    renderActions() {
      const panel = this.panel;
      if (!panel) return;
      const actionsEl = panel.querySelector('#fpCalActions');
      if (!actionsEl) return;

      if (this._confirmingClear) {
        actionsEl.innerHTML = `
          <span class="fp-empty" style="padding:0">Remove the stored calibration for this camera? It will stop placing people on the plan until recalibrated.</span>
          <button class="btn btn-sm btn-danger" id="fpCalClearYes">Yes, clear</button>
          <button class="btn btn-sm" id="fpCalClearNo">Cancel</button>`;
        actionsEl.querySelector('#fpCalClearNo')?.addEventListener('click', () => {
          this._confirmingClear = false;
          this.renderActions();
        });
        actionsEl.querySelector('#fpCalClearYes')?.addEventListener('click', () => {
          this._confirmingClear = false;
          this.clear();
        });
        return;
      }

      const n = this.pairs.length;
      const noSignal = this.frameSource === 'no-signal';
      const canSolve = n >= MIN_PAIRS && !noSignal;
      const canTest = this.hasHomography && n > 0;
      const canUndo = !!(this.pending || n > 0);
      const canClear = !!(this.hasHomography || n > 0 || this.pending);
      const clearText = this.hasHomography ? 'Clear calibration' : 'Clear points';
      const clearTitle = this.hasHomography ? 'Remove stored calibration from camera' : 'Discard draft points';

      actionsEl.innerHTML = `
        <button class="btn btn-sm btn-primary" id="fpCalSolve" ${canSolve ? '' : 'disabled'}>Solve &amp; save</button>
        <button class="btn btn-sm" id="fpCalTest" ${canTest ? '' : 'disabled'} title="Project the image points through the stored calibration and compare">Check reprojection</button>
        <button class="btn btn-sm" id="fpCalUndo" ${canUndo ? '' : 'disabled'}>${this.pending ? 'Cancel this point' : 'Remove last pair'}</button>
        <button class="btn btn-sm btn-danger" id="fpCalClear" ${canClear ? '' : 'disabled'} title="${clearTitle}">${clearText}</button>`;

      actionsEl.querySelector('#fpCalSolve')?.addEventListener('click', () => this.solve());
      actionsEl.querySelector('#fpCalTest')?.addEventListener('click', () => this.test());
      actionsEl.querySelector('#fpCalUndo')?.addEventListener('click', () => this.undo());
      actionsEl.querySelector('#fpCalClear')?.addEventListener('click', () => this.onClearClick());
    },

    renderPairs() {
      if (!this.pairs.length && !this.pending) {
        return '<div class="fp-empty">No point pairs yet. Click a floor mark on the image to start.</div>';
      }
      const rows = this.pairs.map((p, i) => {
        const rp = this.reprojected && this.reprojected[i];
        let err = '';
        if (this.reprojected) {
          err = rp ? `<span class="${Math.hypot(rp.x - p.floor.x, rp.y - p.floor.y) > 0.5 ? 'fp-cal-err' : ''}">Δ ${fmt(Math.hypot(rp.x - p.floor.x, rp.y - p.floor.y))} m</span>` : '<span class="fp-cal-err">no projection</span>';
        }
        return `<div class="fp-cal-pair">
          <span class="fp-cal-n" style="background:${this.color()}">${i + 1}</span>
          <span>img ${Math.round(p.image.x)}, ${Math.round(p.image.y)} px</span>
          <span>plan ${fmt(p.floor.x, 2)}, ${fmt(p.floor.y, 2)} m ${err}</span>
          <button class="btn btn-xs btn-danger" data-del="${i}" title="Remove this pair">×</button>
        </div>`;
      });
      if (this.pending) {
        rows.push(`<div class="fp-cal-pair pending">
          <span class="fp-cal-n" style="background:${this.color()}">${this.pairs.length + 1}</span>
          <span>img ${Math.round(this.pending.x)}, ${Math.round(this.pending.y)} px</span>
          <span>now click this point on the plan…</span>
          <span></span>
        </div>`);
      }
      return rows.join('');
    },

    color() {
      const editor = this.editor;
      const cams = editor ? editor.cameras : [];
      const idx = cams.findIndex((c) => c.camera_id === this.cameraId);
      const palette = ['#00d4ff', '#00ff9d', '#a78bfa', '#fbbf24', '#f472b6', '#fb923c', '#34d399', '#60a5fa'];
      return palette[(idx >= 0 ? idx : 0) % palette.length];
    },

    /** Size the overlay canvas to the displayed image and redraw the markers. */
    fitOverlay() {
      const img = document.getElementById('fpCalImg');
      const canvas = document.getElementById('fpCalCanvas');
      const sizeEl = document.getElementById('fpCalFrameSize');
      if (!img || !canvas) return;
      const box = img.getBoundingClientRect();
      const w = box.width, h = box.height;
      if (img.naturalWidth && img.naturalHeight && (this.frameW !== img.naturalWidth || this.frameH !== img.naturalHeight)) {
        // The stream tells us the native size; it wins over an older stored value.
        this.frameW = img.naturalWidth; this.frameH = img.naturalHeight;
        if (sizeEl) sizeEl.textContent = `${this.frameW}×${this.frameH} px`;
      }
      if (w < 2 || h < 2) return;
      const dpr = window.devicePixelRatio || 1;
      canvas.style.width = `${w}px`; canvas.style.height = `${h}px`;
      canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
      const ctx = canvas.getContext('2d');
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      if (!this.frameW || !this.frameH) return;
      const sx = w / this.frameW, sy = h / this.frameH;
      const color = this.color();
      const mark = (pt, label, pendingStyle) => {
        const x = pt.x * sx, y = pt.y * sy;
        ctx.beginPath(); ctx.arc(x, y, 9, 0, Math.PI * 2);
        ctx.fillStyle = pendingStyle ? 'rgba(0,255,157,0.35)' : color; ctx.fill();
        ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5; ctx.stroke();
        ctx.beginPath(); ctx.moveTo(x - 14, y); ctx.lineTo(x + 14, y); ctx.moveTo(x, y - 14); ctx.lineTo(x, y + 14);
        ctx.strokeStyle = 'rgba(255,255,255,0.7)'; ctx.lineWidth = 1; ctx.stroke();
        ctx.fillStyle = '#0b0e14'; ctx.font = '800 10px ui-monospace, monospace';
        ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillText(label, x, y);
      };
      this.pairs.forEach((p, i) => mark(p.image, String(i + 1), false));
      if (this.pending) mark(this.pending, String(this.pairs.length + 1), true);
    },

    // --------------------------------------------------------------- actions

    onImageClick(e) {
      if (this.pending || this.frameSource === 'no-signal') return;
      const img = document.getElementById('fpCalImg');
      const canvas = document.getElementById('fpCalCanvas');
      if (!img || !canvas) return;
      const natW = img.naturalWidth || this.frameW, natH = img.naturalHeight || this.frameH;
      if (!natW || !natH) { this._lastResult = { kind: 'err', text: 'No frame has arrived yet, so pixel coordinates cannot be measured. Wait for the image.' }; this.render(); return; }
      // Map from the image's own displayed box (fractional) to native pixels.
      const r = img.getBoundingClientRect();
      const x = (e.clientX - r.left) * (natW / r.width);
      const y = (e.clientY - r.top) * (natH / r.height);
      this.frameW = natW; this.frameH = natH;
      this.pending = { x: Math.round(x * 10) / 10, y: Math.round(y * 10) / 10 };
      this.reprojected = null;
      this._lastResult = null;
      this.render();
      this.syncOverlay();
    },

    armPlanPick() {
      const editor = this.editor;
      if (!editor || !this.pending) return;
      editor.beginPick((pt) => {
        if (!pt) { this.pending = null; this.render(); this.syncOverlay(); return; }
        this.pairs.push({ image: this.pending, floor: pt });
        this.pending = null;
        this.render();
        this.syncOverlay();
      }, `Point ${this.pairs.length + 1}: click the same floor spot on the plan (Escape to cancel this point).`);
    },

    undo() {
      if (this.pending) { this.pending = null; if (this.editor) this.editor.cancelPick(); }
      else this.pairs.pop();
      this.reprojected = null;
      this.render();
      this.syncOverlay();
    },

    removePair(i) {
      this.pairs.splice(i, 1);
      this.reprojected = null;
      this.render();
      this.syncOverlay();
    },

    syncOverlay() {
      if (!this.editor) return;
      const pairs = this.pairs.map((p) => ({ floor: p.floor }));
      this.editor.setCalOverlay({ cameraId: this.cameraId, pairs, reprojected: this.reprojected });
    },

    async solve() {
      if (this.pairs.length < MIN_PAIRS) return;
      const btn = document.getElementById('fpCalSolve');
      if (btn) { btn.disabled = true; btn.textContent = 'Solving…'; }
      try {
        const res = await fetch(`${API}/cameras/${encodeURIComponent(this.cameraId)}/calibrate`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            image_points: this.pairs.map((p) => p.image),
            floor_points: this.pairs.map((p) => p.floor),
            frame_width: this.frameW, frame_height: this.frameH,
          }),
        });
        if (!res.ok) throw new Error(await errorDetail(res));
        const data = await res.json();
        this.hasHomography = !!data.calibrated;
        this._lastResult = { kind: 'ok', text: `Calibration saved from ${this.pairs.length} pairs. Checking reprojection…` };
        this.render();
        await this.refreshCamera();
        await this.test();
      } catch (e) {
        this._lastResult = { kind: 'err', text: `Could not solve: ${esc(e.message)}` };
        this.render();
      }
    },

    /** Project the clicked image points through the stored homography. */
    async test() {
      if (!this.pairs.length) return;
      try {
        const res = await fetch(`${API}/cameras/${encodeURIComponent(this.cameraId)}/calibration/test`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ image_points: this.pairs.map((p) => p.image) }),
        });
        if (res.status === 404) {
          this._lastResult = { kind: 'info', text: 'Calibration saved. This server has no …/calibration/test endpoint, so the reprojection check is not available.' };
          this.render();
          return;
        }
        if (!res.ok) throw new Error(await errorDetail(res));
        const data = await res.json();
        this.reprojected = (data.floor_points || []).map((p) => (p && typeof p.x === 'number' ? { x: p.x, y: p.y } : null));
        const errs = this.pairs.map((p, i) => (this.reprojected[i] ? Math.hypot(this.reprojected[i].x - p.floor.x, this.reprojected[i].y - p.floor.y) : null)).filter((v) => v !== null);
        const mean = errs.length ? errs.reduce((a, b) => a + b, 0) / errs.length : null;
        const worst = errs.length ? Math.max(...errs) : null;
        this._lastResult = {
          kind: worst !== null && worst > 1 ? 'err' : 'ok',
          text: errs.length
            ? `Reprojection: mean error ${fmt(mean)} m, worst ${fmt(worst)} m over ${errs.length} points. Orange crosses on the plan show where each image point lands; the numbered dots are where you clicked.${worst > 1 ? ' A point is more than a metre off — check that pair or add more spread-out points.' : ''}`
            : 'The server returned no projected points.',
        };
        this.render();
        this.syncOverlay();
      } catch (e) {
        this._lastResult = { kind: 'err', text: `Reprojection check failed: ${esc(e.message)}` };
        this.render();
      }
    },

    onClearClick() {
      if (this.hasHomography) {
        this.clearConfirm();
      } else if (this.pairs.length > 0 || this.pending) {
        this.clearDraft();
      }
    },

    clearDraft() {
      this.pairs = [];
      this.pending = null;
      this.reprojected = null;
      this._lastResult = { kind: 'info', text: 'Draft points cleared.' };
      if (this.editor) this.editor.cancelPick();
      this.render();
      this.syncOverlay();
    },

    clearConfirm() {
      this._confirmingClear = true;
      this.renderActions();
    },

    async clear() {
      this._confirmingClear = false;
      try {
        const res = await fetch(`${API}/cameras/${encodeURIComponent(this.cameraId)}/calibrate`, { method: 'DELETE' });
        if (!res.ok) throw new Error(await errorDetail(res));
        this.hasHomography = false;
        this.pairs = [];
        this.pending = null;
        this.reprojected = null;
        this._lastResult = { kind: 'ok', text: 'Calibration cleared. This camera no longer places people on the plan.' };
        if (this.editor) this.editor.cancelPick();
        this.render();
        this.syncOverlay();
        await this.refreshCamera();
      } catch (e) {
        this._lastResult = { kind: 'err', text: `Could not clear: ${esc(e.message)}` };
        this.render();
      }
    },

    async refreshCamera() {
      const editor = this.editor;
      if (!editor) return;
      await editor.load();
      this.camera = (editor.cameras || []).find((c) => c.camera_id === this.cameraId) || this.camera;
      if (this.cameraId) editor.selectCamera(this.cameraId);
      if (window.deviceManager) window.deviceManager.refreshCameras();
    },
  };

  async function errorDetail(res) {
    try {
      const d = (await res.json()).detail;
      return typeof d === 'string' ? d : JSON.stringify(d);
    } catch (_) { return res.statusText || `HTTP ${res.status}`; }
  }

  window.calibrationTool = calibrationTool;
})();
