/**
 * Zoom, pan and full screen for a live camera view (dashboard tiles, Camera setup).
 *
 *   const z = EdgeViewZoom.attach(container, stage, opts)
 *
 * `container` receives the gestures and clips (overflow: hidden); `stage` is
 * the wrapper inside it that holds the picture (<img>/<video>) AND the
 * overlay canvases. Only the stage is transformed (translate + scale, origin
 * top-left), so everything drawn on the canvases stays on the same pixels of
 * the picture, and getBoundingClientRect() of anything inside the stage
 * reports the zoomed rectangle (click-to-coordinate mapping keeps working).
 * HUD badges and buttons outside the stage are not zoomed.
 *
 * Gestures (only while opts.enabled() is true, e.g. while a tile is enlarged):
 *  - mouse wheel zooms at the cursor (1x to 6x); wheel out at 1x scrolls the page
 *  - two-finger pinch zooms at the fingers
 *  - drag pans, only when zoomed in; a drag never counts as a click
 *  - double click toggles 2x at that point (opts.dblclickZoom; single clicks
 *    are then held for ~260 ms and handed to opts.onClick)
 *  - buttons (rendered into opts.controlsHost): zoom out, level (= reset), zoom in, full screen
 *
 * opts: { enabled(), controlsHost, fullscreenTarget, dblclickZoom, onClick(), onChange(state) }
 * handle: { state(), reset(), zoomBy(f), update(), toStage(x, y), fromStage(x, y), destroy() }
 */
(function () {
  'use strict';

  const MIN = 1;
  const MAX = 6;
  const STEP = 1.25;
  const DRAG_PX = 5;
  const DBL_MS = 260;

  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

  // ---------------------------------------------------------------- pure math

  /** Keep the scaled stage covering its W x H box (no empty band at an edge). */
  function clampPan(s, tx, ty, W, H) {
    return { tx: clamp(tx, Math.min(0, W - W * s), 0), ty: clamp(ty, Math.min(0, H - H * s), 0) };
  }

  /** New state at scale s2 that keeps container point (px, py) on the same picture point. */
  function zoomAt(st, s2, px, py, W, H) {
    const s = clamp(s2, MIN, MAX);
    const k = s / st.s;
    const c = clampPan(s, px - (px - st.tx) * k, py - (py - st.ty) * k, W, H);
    return s <= MIN + 1e-4 ? { s: 1, tx: 0, ty: 0 } : { s, tx: c.tx, ty: c.ty };
  }

  /** Container (screen-aligned) point -> untransformed stage point, and back. */
  function toStage(st, x, y) { return { x: (x - st.tx) / st.s, y: (y - st.ty) / st.s }; }
  function fromStage(st, x, y) { return { x: x * st.s + st.tx, y: y * st.s + st.ty }; }

  // ---------------------------------------------------------------- full screen

  function fsElement() { return document.fullscreenElement || document.webkitFullscreenElement || null; }
  function fsSupported(node) {
    if (!node) return false;
    const can = !!(node.requestFullscreen || node.webkitRequestFullscreen);
    const allowed = document.fullscreenEnabled !== false || document.webkitFullscreenEnabled === true;
    return can && allowed;
  }
  function toggleFullscreen(node) {
    try {
      if (fsElement() === node) {
        const exit = document.exitFullscreen || document.webkitExitFullscreen;
        const p = exit && exit.call(document);
        if (p && typeof p.catch === 'function') p.catch(() => {});
        return;
      }
      const req = node.requestFullscreen || node.webkitRequestFullscreen;
      const p = req && req.call(node);
      if (p && typeof p.catch === 'function') p.catch(() => {});
    } catch (_) { /* refused by the browser: nothing changes */ }
  }

  const handles = new Set();
  function onFullscreenChange() {
    handles.forEach((h) => {
      if (!h.container.isConnected) { handles.delete(h); return; }
      h.refit();
    });
  }
  document.addEventListener('fullscreenchange', onFullscreenChange);
  document.addEventListener('webkitfullscreenchange', onFullscreenChange);

  const isControl = (target) => !!(target && target.closest
    && target.closest('button, a, input, select, textarea, label, .vz-controls, .cam-rtc-fail, .box-popover'));

  // ---------------------------------------------------------------- one view

  function attach(container, stage, opts) {
    const o = opts || {};
    const st = { s: 1, tx: 0, ty: 0 };
    const pointers = new Map();
    let drag = null;          // {id, x, y, tx, ty, moved}
    let pinch = null;         // {d, mid, start}
    let suppressClick = false;
    let suppressTimer = null;
    let clickTimer = null;
    let controls = null;
    let lastEnabled = null;
    const enabled = () => (typeof o.enabled === 'function' ? !!o.enabled() : true);
    const fsTarget = o.fullscreenTarget || container;

    function size() { return { W: container.clientWidth || 1, H: container.clientHeight || 1 }; }
    function local(e) {
      const r = container.getBoundingClientRect();
      return { x: e.clientX - r.left - (container.clientLeft || 0), y: e.clientY - r.top - (container.clientTop || 0) };
    }

    function apply(silent) {
      if (st.s <= MIN + 1e-4) { st.s = 1; st.tx = 0; st.ty = 0; }
      stage.style.transformOrigin = '0 0';
      stage.style.transform = st.s === 1 ? '' : `translate(${st.tx.toFixed(2)}px, ${st.ty.toFixed(2)}px) scale(${st.s.toFixed(4)})`;
      container.classList.toggle('vz-zoomed', st.s > 1);
      renderControls();
      if (!silent && typeof o.onChange === 'function') {
        try { o.onChange({ s: st.s, tx: st.tx, ty: st.ty }); } catch (_) { /* a listener error must not break zoom */ }
      }
    }

    function set(next) {
      if (next.s === st.s && next.tx === st.tx && next.ty === st.ty) return;
      st.s = next.s; st.tx = next.tx; st.ty = next.ty;
      apply(false);
    }

    function zoomBy(f, px, py) {
      const { W, H } = size();
      set(zoomAt(st, st.s * f, px === undefined ? W / 2 : px, py === undefined ? H / 2 : py, W, H));
    }

    function reset() { set({ s: 1, tx: 0, ty: 0 }); }

    /** After a resize or a full-screen switch: keep the zoom, keep the picture covering the box. */
    function refit() {
      const { W, H } = size();
      const c = clampPan(st.s, st.tx, st.ty, W, H);
      st.tx = c.tx; st.ty = c.ty;
      apply(false);
    }

    // ---- controls

    // Lucide sprite icons (icons/sprite.svg), the same markup as js/icons.js; the old text glyphs
    // (minus, plus, U+26F6) sat off-centre and U+26F6 is a pictograph.
    const ICON = (name) => `<svg class="icon icon-md" aria-hidden="true" focusable="false"><use href="/static/icons/sprite.svg#i-${name}"></use></svg>`;

    function renderControls() {
      const host = o.controlsHost;
      if (!host) return;
      const on = enabled();
      if (!controls) {
        controls = document.createElement('span');
        controls.className = 'vz-controls';
        controls.setAttribute('role', 'group');
        controls.setAttribute('aria-label', 'Zoom');
        controls.innerHTML = `
          <button type="button" class="vz-btn" data-vz="out" aria-label="Zoom out" title="Zoom out">${ICON('zoom-out')}</button>
          <button type="button" class="vz-btn vz-level" data-vz="reset" aria-label="Reset zoom" title="Back to the whole picture">1&times;</button>
          <button type="button" class="vz-btn" data-vz="in" aria-label="Zoom in" title="Zoom in (or use the mouse wheel / pinch; drag to move around)">${ICON('zoom-in')}</button>
          <button type="button" class="vz-btn" data-vz="fs" aria-label="Full screen" title="Full screen">${ICON('maximize-2')}</button>`;
        controls.addEventListener('click', (e) => {
          const b = e.target.closest('[data-vz]');
          e.stopPropagation();
          if (!b) return;
          const what = b.getAttribute('data-vz');
          if (what === 'in') zoomBy(STEP);
          else if (what === 'out') zoomBy(1 / STEP);
          else if (what === 'reset') reset();
          else if (what === 'fs') toggleFullscreen(fsTarget);
        });
        controls.addEventListener('pointerdown', (e) => e.stopPropagation());
        controls.addEventListener('dblclick', (e) => e.stopPropagation());
        host.appendChild(controls);
      }
      controls.hidden = !on;
      const level = controls.querySelector('.vz-level');
      const txt = st.s === 1 ? '1×' : `${st.s.toFixed(st.s < 10 ? 1 : 0)}×`;
      if (level && level.textContent !== txt) level.textContent = txt;
      const out = controls.querySelector('[data-vz="out"]');
      const inn = controls.querySelector('[data-vz="in"]');
      if (out) out.disabled = st.s <= MIN;
      if (inn) inn.disabled = st.s >= MAX;
      const fs = controls.querySelector('[data-vz="fs"]');
      if (fs) {
        fs.hidden = !fsSupported(fsTarget);
        const full = fsElement() === fsTarget;
        fs.setAttribute('aria-pressed', full ? 'true' : 'false');
        fs.title = full ? 'Leave full screen' : 'Full screen';
        const want = full ? 'minimize-2' : 'maximize-2';
        const use = fs.querySelector('use');
        if (use && !use.getAttribute('href').endsWith(want)) use.setAttribute('href', `/static/icons/sprite.svg#i-${want}`);
      }
    }

    /** Call when enabled() may have changed (e.g. a tile was enlarged or shrunk). */
    function update() {
      const on = enabled();
      if (on !== lastEnabled) {
        lastEnabled = on;
        container.classList.toggle('vz-active', on);
        if (!on) {
          pointers.clear(); drag = null; pinch = null;
          if (fsElement() === fsTarget) toggleFullscreen(fsTarget);
          if (st.s !== 1) { st.s = 1; st.tx = 0; st.ty = 0; apply(false); return; }
        }
      }
      renderControls();
    }

    // ---- gestures

    function onWheel(e) {
      if (!enabled() || isControl(e.target)) return;
      let dy = e.deltaY;
      if (e.deltaMode === 1) dy *= 16;          // lines
      else if (e.deltaMode === 2) dy *= 400;    // pages
      if (!dy) return;
      if (st.s <= MIN && dy > 0) return;        // zoomed out already: let the page scroll
      e.preventDefault();
      const p = local(e);
      zoomBy(Math.exp(-clamp(dy, -300, 300) * 0.002), p.x, p.y);
    }

    function startSuppress() {
      suppressClick = true;
      clearTimeout(suppressTimer);
      suppressTimer = setTimeout(() => { suppressClick = false; }, 450);
    }

    function onPointerDown(e) {
      if (!enabled() || isControl(e.target)) return;
      if (e.pointerType === 'mouse' && e.button !== 0) return;
      pointers.set(e.pointerId, local(e));
      if (pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        pinch = { d: Math.hypot(a.x - b.x, a.y - b.y) || 1, mid: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }, start: { ...st } };
        drag = null;
        startSuppress();
      } else if (pointers.size === 1) {
        const p = pointers.get(e.pointerId);
        drag = { id: e.pointerId, x: p.x, y: p.y, tx: st.tx, ty: st.ty, moved: false };
      }
    }

    function onPointerMove(e) {
      if (!pointers.has(e.pointerId)) return;
      const p = local(e);
      pointers.set(e.pointerId, p);
      const { W, H } = size();
      if (pinch && pointers.size >= 2) {
        const [a, b] = [...pointers.values()];
        const d = Math.hypot(a.x - b.x, a.y - b.y) || 1;
        const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
        const z = zoomAt(pinch.start, pinch.start.s * (d / pinch.d), pinch.mid.x, pinch.mid.y, W, H);
        const c = clampPan(z.s, z.tx + (mid.x - pinch.mid.x), z.ty + (mid.y - pinch.mid.y), W, H);
        set(z.s === 1 ? z : { s: z.s, tx: c.tx, ty: c.ty });
        e.preventDefault();
        return;
      }
      if (!drag || drag.id !== e.pointerId) return;
      const dx = p.x - drag.x;
      const dy = p.y - drag.y;
      if (!drag.moved && Math.hypot(dx, dy) > DRAG_PX) {
        drag.moved = true;
        startSuppress();
        try { container.setPointerCapture(e.pointerId); } catch (_) { /* not capturable */ }
      }
      if (drag.moved && st.s > 1) {
        const c = clampPan(st.s, drag.tx + dx, drag.ty + dy, W, H);
        set({ s: st.s, tx: c.tx, ty: c.ty });
        e.preventDefault();
      }
    }

    function onPointerUp(e) {
      if (!pointers.has(e.pointerId)) return;
      pointers.delete(e.pointerId);
      try { if (container.hasPointerCapture && container.hasPointerCapture(e.pointerId)) container.releasePointerCapture(e.pointerId); } catch (_) { /* ignore */ }
      if (drag && drag.id === e.pointerId) {
        if (drag.moved) startSuppress();
        drag = null;
      }
      if (pointers.size < 2) pinch = null;
      if (pointers.size === 1) {
        // One finger left after a pinch: it pans from here, and is never a click.
        const [id, p] = [...pointers.entries()][0];
        drag = { id, x: p.x, y: p.y, tx: st.tx, ty: st.ty, moved: true };
      }
    }

    function onClickCapture(e) {
      if (isControl(e.target)) return;
      // Capture listeners at the target run before its onclick attribute, so
      // stopImmediatePropagation keeps a drag or a held click from reaching it.
      if (suppressClick) {
        suppressClick = false;
        e.stopImmediatePropagation();
        e.preventDefault();
        return;
      }
      if (!enabled() || !o.dblclickZoom) return;   // an ordinary click: the page handles it
      e.stopImmediatePropagation();
      e.preventDefault();
      const p = local(e);
      if (clickTimer) {
        clearTimeout(clickTimer);
        clickTimer = null;
        const { W, H } = size();
        set(st.s > 1 ? { s: 1, tx: 0, ty: 0 } : zoomAt(st, 2, p.x, p.y, W, H));
        return;
      }
      clickTimer = setTimeout(() => {
        clickTimer = null;
        if (typeof o.onClick === 'function') o.onClick();
      }, DBL_MS);
    }

    // A mouse drag on the <img> would start the browser's native image drag,
    // which cancels the pointer (pointercancel) after the first move: no pan.
    function onDragStart(e) { if (enabled()) e.preventDefault(); }

    container.addEventListener('wheel', onWheel, { passive: false });
    container.addEventListener('dragstart', onDragStart);
    container.addEventListener('pointerdown', onPointerDown);
    container.addEventListener('pointermove', onPointerMove);
    container.addEventListener('pointerup', onPointerUp);
    container.addEventListener('pointercancel', onPointerUp);
    container.addEventListener('click', onClickCapture, true);
    let ro = null;
    if (window.ResizeObserver) {
      ro = new ResizeObserver(() => { if (st.s > 1) refit(); });
      ro.observe(container);
    }

    const handle = {
      container,
      refit,
      state: () => ({ s: st.s, tx: st.tx, ty: st.ty }),
      reset,
      zoomBy: (f) => zoomBy(f),
      update,
      toStage: (x, y) => toStage(st, x, y),
      fromStage: (x, y) => fromStage(st, x, y),
      destroy() {
        handles.delete(handle);
        clearTimeout(clickTimer);
        clearTimeout(suppressTimer);
        container.removeEventListener('wheel', onWheel);
        container.removeEventListener('dragstart', onDragStart);
        container.removeEventListener('pointerdown', onPointerDown);
        container.removeEventListener('pointermove', onPointerMove);
        container.removeEventListener('pointerup', onPointerUp);
        container.removeEventListener('pointercancel', onPointerUp);
        container.removeEventListener('click', onClickCapture, true);
        if (ro) ro.disconnect();
        if (controls) controls.remove();
        stage.style.transform = '';
        container.classList.remove('vz-zoomed', 'vz-active');
      },
    };
    handles.add(handle);
    update();
    return handle;
  }

  window.EdgeViewZoom = { attach, MIN, MAX, _math: { clampPan, zoomAt, toStage, fromStage } };
})();
