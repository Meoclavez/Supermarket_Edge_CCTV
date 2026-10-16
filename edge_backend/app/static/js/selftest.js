/**
 * In-page self-test for the blueprint editor. Loaded by floorplan.js ONLY when
 * the page URL carries ?__fptest=1; it is never referenced otherwise.
 *
 * It drives the real canvas with synthetic pointer events, checks the server
 * state afterwards, and prints everything into #selftestLog so a headless
 * --dump-dom run can be asserted on. Everything it creates is deleted again.
 */
(function () {
  'use strict';
  const log = document.createElement('div');
  log.id = 'selftestLog';
  const results = { pass: 0, fail: 0 };
  function out(name, ok, detail) {
    if (ok === true) results.pass++; else if (ok === false) results.fail++;
    const line = document.createElement('div');
    line.textContent = `${ok === true ? 'PASS' : ok === false ? 'FAIL' : 'INFO'} ${name}${detail ? ' — ' + detail : ''}`;
    log.appendChild(line);
  }
  window.addEventListener('error', (e) => out('window.onerror', false, `${e.message} @ ${e.filename}:${e.lineno}`));
  window.addEventListener('unhandledrejection', (e) => out('unhandledrejection', false, String(e.reason && e.reason.stack || e.reason)));
  const origErr = console.error.bind(console);
  console.error = (...a) => { out('console.error', false, a.map((x) => (x && x.stack) || String(x)).join(' ')); origErr(...a); };

  // Test-only: the sign-in overlay is cosmetic under DEBUG=true (the API is
  // open); remove it so the screenshot shows the tab under test.
  const gateKiller = setInterval(() => { const g = document.getElementById('authGate'); if (g) g.remove(); }, 100);
  setTimeout(() => clearInterval(gateKiller), 6000);

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  async function waitFor(fn, ms) {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) { try { if (fn()) return true; } catch (_) { /* retry */ } await sleep(50); }
    return false;
  }
  async function getLayout() { return (await fetch('/api/v1/layout')).json(); }
  function samePoly(a, b) {
    return a && b && a.length === b.length && a.every((p, i) => Math.abs(p.x - b[i].x) < 1e-6 && Math.abs(p.y - b[i].y) < 1e-6);
  }

  // The editor listens to Pointer Events (mouse, touch and pen).
  function pointer(canvas, type, init) {
    const Ctor = typeof PointerEvent === 'function' ? PointerEvent : MouseEvent;
    canvas.dispatchEvent(new Ctor(type, Object.assign({ pointerId: 1, pointerType: 'mouse', isPrimary: true }, init)));
  }
  function clickWorld(ed, wx, wy, opts = {}) {
    const canvas = ed.canvas;
    const s = ed.toScreen(wx, wy);
    const r = canvas.getBoundingClientRect();
    const init = { bubbles: true, cancelable: true, clientX: r.left + s.x, clientY: r.top + s.y, button: 0, shiftKey: !!opts.shift };
    pointer(canvas, 'pointermove', init);
    pointer(canvas, 'pointerdown', init);
    pointer(canvas, 'pointerup', init);
    canvas.dispatchEvent(new MouseEvent('click', init));
  }
  function dragWorld(ed, from, to, opts = {}) {
    const canvas = ed.canvas;
    const r = canvas.getBoundingClientRect();
    const a = opts.screen ? from : ed.toScreen(from.x, from.y), b = ed.toScreen(to.x, to.y);
    const mk = (p) => ({ bubbles: true, cancelable: true, clientX: r.left + p.x, clientY: r.top + p.y, button: 0 });
    pointer(canvas, 'pointermove', mk(a));
    pointer(canvas, 'pointerdown', mk(a));
    const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
    pointer(canvas, 'pointermove', mk(mid));
    pointer(canvas, 'pointermove', mk(b));
    pointer(canvas, 'pointerup', mk(b));
  }

  async function cssClassAudit() {
    const defined = new Set();
    for (const sheet of document.styleSheets) {
      let rules; try { rules = sheet.cssRules; } catch (_) { continue; }
      const walk = (list) => { for (const r of list) { if (r.selectorText) (r.selectorText.match(/\.[A-Za-z_][\w-]*/g) || []).forEach((c) => defined.add(c.slice(1))); if (r.cssRules) walk(r.cssRules); } };
      walk(rules);
    }
    const used = new Set();
    document.querySelectorAll('#tab-floorplan [class]').forEach((e) => e.classList.forEach((c) => used.add(c)));
    for (const f of ['/static/js/floorplan.js', '/static/js/devices.js', '/static/js/calibration.js']) {
      const src = await (await fetch(f)).text();
      (src.match(/class="([^"]*)"/g) || []).forEach((m) => {
        m.slice(7, -1).replace(/[\w-]*\$\{[^}]*\}[\w-]*/g, ' ').split(/\s+/).forEach((c) => { if (/^[a-z][\w-]*$/i.test(c)) used.add(c); });
      });
    }
    const missing = [...used].filter((c) => !defined.has(c) && !/^(active|done|pending|ok|err|warn|info|is-|dot-)/.test(c) && !['is-selected', 'is-cal', 'is-uncal', 'is-armed', 'error'].includes(c));
    out('css classes referenced are defined', missing.length === 0, missing.length ? 'missing: ' + missing.join(', ') : `${used.size} classes checked`);
  }

  async function run() {
    const created = { structures: [], zones: [] };
    try {
      const ok = await waitFor(() => window.blueprintEditor && window.blueprintEditor.layout, 10000);
      out('editor loaded layout', ok);
      const ed = window.blueprintEditor;
      if (!ok) return;
      if (typeof window.switchTab === 'function') window.switchTab('floorplan');
      await sleep(150);
      ed.resize(); ed.resetView();

      const rect = ed.canvas.getBoundingClientRect();
      out('canvas height > 400', rect.height > 400, `${Math.round(rect.width)}x${Math.round(rect.height)} css px, backing ${ed.canvas.width}x${ed.canvas.height}`);
      const tab = document.getElementById('tab-floorplan');
      out('floorplan tab is active', tab && tab.classList.contains('active'));

      const layout0 = await getLayout();
      out('GET /api/v1/layout has "structures"', Array.isArray(layout0.structures), Array.isArray(layout0.structures) ? `${layout0.structures.length} structures` : 'Agent A endpoints not present yet');
      out('GET /api/v1/layout has "setup"', !!layout0.setup, layout0.setup ? JSON.stringify(layout0.setup) : 'absent (client-side fallback in use)');
      const liveRes = await fetch('/api/v1/layout/live');
      out('GET /api/v1/layout/live', liveRes.ok, liveRes.ok ? JSON.stringify(await liveRes.json()).slice(0, 200) : `HTTP ${liveRes.status}`);

      out('window.FloorplanAPI is present', !!window.FloorplanAPI && typeof window.FloorplanAPI.placeCamera === 'function' && window.FloorplanAPI.ready instanceof Promise);

      // ---- draw a room via synthetic clicks ("Free shape": corner by corner)
      const roomPts = [{ x: 2, y: 2 }, { x: 10, y: 2 }, { x: 10, y: 8 }, { x: 2, y: 8 }];
      ed.setFreeShape(true);
      ed.startDraw('ROOM');
      out('mode after startDraw(ROOM)', ed.mode === 'draw' && ed.draftKind === 'ROOM', `mode=${ed.mode} kind=${ed.draftKind}`);
      roomPts.forEach((p) => clickWorld(ed, p.x, p.y));
      out('room draft has 4 points', ed.draft.length === 4, `draft=${JSON.stringify(ed.draft)}`);
      await ed.finishPolygon();
      await sleep(100);
      let lay = await getLayout();
      const room = (lay.structures || []).find((s) => s.kind === 'ROOM' && samePoly(s.polygon, roomPts));
      if (room) created.structures.push(room.id);
      out('room persisted via POST /api/v1/layout/structures', !!room, room ? `id=${room.id} area=${room.area_m2}` : (Array.isArray(lay.structures) ? 'not found in structures[]' : 'structures API absent: ' + (document.getElementById('fpStatus') || {}).textContent));
      out('editor shows room with server id', !!room && ed.structures.some((s) => s.id === room.id), room ? `selected=${JSON.stringify(ed.sel)}` : '');

      // ---- wall with Shift orthogonal snapping
      ed.startDraw('WALL');
      clickWorld(ed, 12, 12);
      clickWorld(ed, 18.13, 12.37, { shift: true });   // Shift: should land on y=12, x snapped to 18
      out('wall ortho snap with Shift', ed.draft.length === 2 && ed.draft[1].y === 12 && ed.draft[1].x === 18, `draft=${JSON.stringify(ed.draft)}`);
      await ed.finishPolygon();
      await sleep(100);
      lay = await getLayout();
      const wall = (lay.structures || []).find((s) => s.kind === 'WALL' && samePoly(s.polygon, [{ x: 12, y: 12 }, { x: 18, y: 12 }]));
      if (wall) created.structures.push(wall.id);
      out('wall persisted (polyline, 2 points)', !!wall, wall ? `id=${wall.id} length=${wall.length_m} thickness=${wall.thickness_m}` : 'not found');

      // ---- move the room by dragging it, then verify on the server
      if (room) {
        ed.select({ type: 'structure', id: room.id });
        dragWorld(ed, { x: 6, y: 5 }, { x: 8, y: 6 });
        await sleep(150);
        lay = await getLayout();
        const moved = (lay.structures || []).find((s) => s.id === room.id);
        const expect = roomPts.map((p) => ({ x: p.x + 2, y: p.y + 1 }));
        out('drag-move room persisted (PUT structures/{id})', !!moved && samePoly(moved.polygon, expect), moved ? JSON.stringify(moved.polygon) : 'missing');

        // ---- vertex drag: move corner 0 from (4,3) to (5,4)
        ed.select({ type: 'structure', id: room.id });
        dragWorld(ed, { x: 4, y: 3 }, { x: 5, y: 4 });
        await sleep(150);
        lay = await getLayout();
        const v = (lay.structures || []).find((s) => s.id === room.id);
        out('vertex drag persisted', !!v && v.polygon[0].x === 5 && v.polygon[0].y === 4, v ? JSON.stringify(v.polygon[0]) : 'missing');
        // ---- undo the corner drag (PUT with the old geometry), then redo it
        await ed.undo();
        lay = await getLayout();
        const u = (lay.structures || []).find((s) => s.id === room.id);
        out('undo restores the corner on the server', !!u && u.polygon[0].x === 4 && u.polygon[0].y === 3, u ? JSON.stringify(u.polygon[0]) : 'missing');
        await ed.redo();
      }
      ed.setFreeShape(false);

      // ---- rectangle drag (the default for shelves), then resize with a handle
      ed.startDraw('SHELF');
      dragWorld(ed, { x: 12, y: 2 }, { x: 16, y: 3 });
      await sleep(200);
      lay = await getLayout();
      const shelf = (lay.structures || []).find((s) => s.kind === 'SHELF' && samePoly(s.polygon, [{ x: 12, y: 2 }, { x: 16, y: 2 }, { x: 16, y: 3 }, { x: 12, y: 3 }]));
      if (shelf) created.structures.push(shelf.id);
      out('shelf drawn by press-drag-release', !!shelf, shelf ? `id=${shelf.id} area=${shelf.area_m2}` : 'not found');
      if (shelf) {
        ed.select({ type: 'structure', id: shelf.id });
        const h = ed.frameHandles().find((q) => q.kind === 'resize' && q.sx === 1 && q.sy === 0);
        if (h) dragWorld(ed, { x: h.x, y: h.y }, { x: 18, y: 2.5 }, { screen: true });
        await sleep(200);
        lay = await getLayout();
        const rs = (lay.structures || []).find((s) => s.id === shelf.id);
        out('resize handle persisted', !!rs && Math.max(...rs.polygon.map((q) => q.x)) === 18, rs ? JSON.stringify(rs.polygon) : 'missing');
      }

      // ---- map size: add 1 m on the right, then remove it again
      const w0 = ed.layout.width_m;
      const grown = await ed.extendMap({ right_m: 1 });
      out('POST /layout/extend grows the map', !!grown && ed.layout.width_m === w0 + 1, `width ${w0} -> ${ed.layout.width_m}`);
      const shrunk = await ed.extendMap({ right_m: -1 });
      out('negative extend removes empty space', !!shrunk && ed.layout.width_m === w0, `width ${ed.layout.width_m}`);

      // ---- draw a zone (corner by corner)
      ed.setFreeShape(true);
      const zonePts = [{ x: 20, y: 3 }, { x: 26, y: 3 }, { x: 26, y: 9 }, { x: 20, y: 9 }];
      ed.startDrawZone();
      zonePts.forEach((p) => clickWorld(ed, p.x, p.y));
      await ed.finishPolygon();
      await sleep(100);
      lay = await getLayout();
      const zone = (lay.zones || []).find((z) => samePoly(z.polygon, zonePts));
      if (zone) created.zones.push(zone.id);
      ed.setFreeShape(false);
      out('zone persisted via POST /api/v1/layout/zones', !!zone, zone ? `id=${zone.id} area=${zone.area_m2}` : 'not found');
      out('zone inspector rendered', !!document.getElementById('zName'), document.getElementById('fpInspectorTitle') && document.getElementById('fpInspectorTitle').textContent);

      // ---- Escape cancels a draft
      ed.setFreeShape(true);
      ed.startDraw('SHELF');
      clickWorld(ed, 30, 3); clickWorld(ed, 33, 3);
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      out('Escape cancels drawing', ed.mode === 'view' && ed.draft.length === 0, `mode=${ed.mode}`);
      ed.setFreeShape(false);

      // ---- setup panel / hint state
      const setupPanel = document.getElementById('fpSetupPanel');
      out('setup panel hidden when configured', setupPanel && setupPanel.style.display === 'none', `configured=${ed.setup && ed.setup.configured}`);

      // ---- camera inspector, if any camera exists
      if (ed.cameras.length) {
        ed.selectCamera(ed.cameras[0].camera_id);
        out('camera inspector shows live thumbnail', !!document.getElementById('fpCamThumb') && !!document.getElementById('fpCalibrate'), ed.cameras[0].name);
        const camId = ed.cameras[0].camera_id;
        const cal = window.calibrationTool;
        const before = await (await fetch(`/api/v1/layout/cameras/${camId}/calibration`)).json();
        // Headless runs must not hold an endless MJPEG connection open (it
        // stalls the virtual clock), so the tool is pointed at the finite
        // snapshot of the same camera; pixel geometry is identical.
        cal.frameUrl = () => `/api/v1/cameras/${encodeURIComponent(camId)}/snapshot?annotate=false&_=${Date.now()}`;
        // Direct-video mode (through the online-access tunnel): the tool takes one
        // still over WebRTC (WebRtcLive.grabFrame) and ignores frameUrl.
        const direct = !!window.WebRtcLive && window.WebRtcLive.transportNow() === 'webrtc';
        out('calibration picture source', undefined, direct ? 'direct video still (grabFrame)' : 'snapshot');
        cal.open(camId);
        await sleep(400);
        out('calibration panel opened', document.getElementById('fpLayout').classList.contains('fp-cal-open') && !!document.getElementById('fpCalImg'));
        out('GET …/calibration', typeof before.has_homography === 'boolean', JSON.stringify(before).slice(0, 160));
        // Image click -> native pixel conversion on the overlay canvas.
        const ov = document.getElementById('fpCalCanvas');
        const img = document.getElementById('fpCalImg');
        await waitFor(() => img.naturalWidth > 0, direct ? 20000 : 3000);
        if (img.naturalWidth > 0 && ov.clientWidth > 0) {
          const r = ov.getBoundingClientRect();
          ov.dispatchEvent(new MouseEvent('click', { bubbles: true, clientX: r.left + r.width * 0.25, clientY: r.top + r.height * 0.5 }));
          const p = cal.pending;
          // MouseEvent clientX/Y are integers, so allow one display pixel of slack (~3 native px here).
          out('image click maps to native pixels', !!p && Math.abs(p.x - img.naturalWidth * 0.25) < 3.5 && Math.abs(p.y - img.naturalHeight * 0.5) < 3.5,
            p ? `pending=${JSON.stringify(p)} natural=${img.naturalWidth}x${img.naturalHeight} shown=${Math.round(r.width)}x${Math.round(r.height)}` : 'no pending point');
          out('plan pick armed after image click', ed.mode === 'pick-point');
          clickWorld(ed, 3, 3);
          out('plan click completes the pair (unsnapped, within 1 px)', cal.pairs.length === 1 && Math.abs(cal.pairs[0].floor.x - 3) < 1.5 / ed.scale && Math.abs(cal.pairs[0].floor.y - 3) < 1.5 / ed.scale && ed.mode === 'view', JSON.stringify(cal.pairs[0] || null));
          cal.removePair(0);
        } else {
          out('image click test skipped (no frame in headless run)', undefined, `natural=${img.naturalWidth}x${img.naturalHeight}`);
        }
        if (!before.has_homography) {
          // Solve from four synthetic pairs, verify persistence + reprojection, then clear.
          const W = cal.frameW || 1280, H = cal.frameH || 720;
          cal.pairs = [
            { image: { x: 100, y: 100 }, floor: { x: 1, y: 1 } }, { image: { x: W - 100, y: 100 }, floor: { x: 9, y: 1 } },
            { image: { x: W - 100, y: H - 100 }, floor: { x: 9, y: 7 } }, { image: { x: 100, y: H - 100 }, floor: { x: 1, y: 7 } },
          ];
          cal.render();
          await cal.solve();
          await sleep(200);
          const after = await (await fetch(`/api/v1/layout/cameras/${camId}/calibration`)).json();
          out('POST …/calibrate persisted 4 pairs + frame size', after.has_homography === true && after.calibration_points && after.calibration_points.image_points.length === 4 && after.calibration_points.frame_width === W,
            JSON.stringify({ has_homography: after.has_homography, n: after.calibration_points && after.calibration_points.image_points.length, fw: after.calibration_points && after.calibration_points.frame_width }));
          const worst = cal.reprojected ? Math.max(...cal.pairs.map((p, i) => Math.hypot(cal.reprojected[i].x - p.floor.x, cal.reprojected[i].y - p.floor.y))) : null;
          out('POST …/calibration/test reprojection', Array.isArray(cal.reprojected) && cal.reprojected.length === 4 && worst !== null && worst < 0.05, `worst error ${worst} m; overlay=${!!ed.calOverlay && ed.calOverlay.reprojected ? 'on plan' : 'none'}`);
          out('reopen loads saved pairs', undefined, `pairs in tool after solve: ${cal.pairs.length}`);
          await cal.clear();
          const cleared = await (await fetch(`/api/v1/layout/cameras/${camId}/calibration`)).json();
          out('DELETE …/calibrate clears', cleared.has_homography === false, JSON.stringify(cleared).slice(0, 120));
        } else {
          out('camera already calibrated; solve/clear round-trip skipped to preserve it', undefined);
        }
        cal.close();
        out('calibration panel closed', !document.getElementById('fpLayout').classList.contains('fp-cal-open') && ed.calOverlay === null);
        ed.select(null);
      } else {
        out('no cameras on this server; camera inspector not exercised', undefined);
      }

      // ---- live polling pauses when the tab is not visible
      if (typeof window.switchTab === 'function') {
        window.switchTab('matrix');
        out('live polling paused when tab hidden', ed.isVisible() === false);
        window.switchTab('floorplan');
        await sleep(120);
        out('visible again', ed.isVisible() === true);
      }
      // ---- fresh-state panel renders when nothing is configured (simulated)
      const realSetup = ed.setup;
      ed.setup = { configured: false, zones: 0, structures: 0, cameras: 0, cameras_calibrated: 0 };
      ed.renderSetup();
      out('fresh-state setup panel renders', setupPanel.style.display !== 'none' && !!document.getElementById('suApply') && document.getElementById('fpHint').style.display !== 'none',
        `steps=${setupPanel.querySelectorAll('.fp-setup-step').length}`);
      ed.setup = realSetup; ed.renderSetup();

      await cssClassAudit();
      const badgeHost = document.getElementById('fpCamBadges');
      out('live badge dom', undefined, badgeHost ? badgeHost.textContent.trim().slice(0, 160) : 'none');
    } catch (e) {
      out('self-test crashed', false, e.stack || String(e));
    } finally {
      for (const id of created.structures) await fetch(`/api/v1/layout/structures/${id}`, { method: 'DELETE' });
      for (const id of created.zones) await fetch(`/api/v1/layout/zones/${id}`, { method: 'DELETE' });
      const lay = await getLayout();
      const leftover = [...created.structures.filter((id) => (lay.structures || []).some((s) => s.id === id)), ...created.zones.filter((id) => lay.zones.some((z) => z.id === id))];
      out('cleanup removed test shapes', leftover.length === 0, leftover.length ? 'leftover: ' + leftover.join(',') : `${created.structures.length} structures, ${created.zones.length} zones removed`);
      if (window.blueprintEditor) window.blueprintEditor.load();
      // Visual check aid: leave the calibration panel open with two pairs.
      if (/__fpcal=1/.test(window.location.search) && window.blueprintEditor && window.blueprintEditor.cameras.length) {
        const ed = window.blueprintEditor, cal = window.calibrationTool, camId = ed.cameras[0].camera_id;
        cal.frameUrl = () => `/api/v1/cameras/${encodeURIComponent(camId)}/snapshot?annotate=false&_=${Date.now()}`;
        await cal.open(camId);
        cal.pairs = [{ image: { x: 200, y: 600 }, floor: { x: 3, y: 6 } }, { image: { x: 1100, y: 620 }, floor: { x: 12, y: 6.5 } }];
        cal.pending = { x: 640, y: 300 };
        cal.render(); cal.syncOverlay();
      }
      const summary = document.createElement('div');
      summary.id = 'selftestSummary';
      summary.textContent = `SELFTEST_SUMMARY pass=${results.pass} fail=${results.fail}`;
      log.appendChild(summary);
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    const tab = document.getElementById('tab-floorplan');
    (tab || document.body).appendChild(log);
    run();
  });
  if (document.readyState !== 'loading') {
    const tab = document.getElementById('tab-floorplan');
    (tab || document.body).appendChild(log);
    run();
  }
})();
