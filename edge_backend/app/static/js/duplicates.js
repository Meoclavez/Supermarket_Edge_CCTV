/**
 * Duplicate camera guard: "Same camera added twice" banners and the 409 panel.
 *
 * One physical camera configured twice (a recorder channel on two streams, or
 * a recorder channel and the camera's own address) is analysed twice and every
 * person counted twice. The server keeps one camera per group in store totals;
 * these banners show each group in Cameras (#dupBannerCameras) and Store map ->
 * Cameras & devices (#dupBannerMap) with inline actions:
 *   Keep A, turn off B    PUT  /api/v1/cameras/{B}/enabled
 *   Keep A, remove B      GET  /api/v1/cameras/{B}/delete-impact, then DELETE /api/v1/layout/cameras/{B}
 *   Make B primary        POST /api/v1/cameras/duplicates/primary
 *   They're different     POST /api/v1/cameras/duplicates/dismiss
 * Add / adopt / recorder import flows use conflictHtml() for their 409 answer.
 * No prompt()/confirm()/alert(): every confirmation is an inline row.
 */
(function () {
  'use strict';

  /** Icon markup from js/icons.js (window.EdgeIcon); '' when that script is missing. Never throws. */
  const ico = (name, opts) => {
    try { return window.EdgeIcon && typeof window.EdgeIcon.svg === 'function' ? window.EdgeIcon.svg(name, opts) : ''; } catch (_) { return ''; }
  };

  const MOUNTS = ['dupBannerCameras', 'dupBannerMap'];
  let report = null;
  let inflight = null;
  // The outcome of the last action, kept visible after its group is resolved.
  let lastMsg = null;
  // Per group: { remove: <camera id>, impact, busy, msg: {text, kind} }
  const ui = {};

  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));

  function authed() {
    return !(window.edgeAuth && typeof window.edgeAuth.isAuthenticated === 'function' && !window.edgeAuth.isAuthenticated());
  }

  async function errorText(res, fallback) {
    let d = null;
    try { d = (await res.json()).detail; } catch (_) { /* not JSON */ }
    if (d && typeof d === 'object' && !Array.isArray(d) && d.message) return d.message;
    if (Array.isArray(d)) return d.map((x) => (x && x.msg) || String(x)).join('; ');
    return d || `${fallback} (HTTP ${res.status})`;
  }

  async function load() {
    if (!authed()) return report;
    if (inflight) return inflight;
    inflight = (async () => {
      try {
        const res = await fetch('/api/v1/cameras/duplicates', { cache: 'no-store' });
        if (res.ok) report = await res.json();
      } catch (_) { /* transient: keep the last report */ }
      inflight = null;
      renderAll();
      window.dispatchEvent(new CustomEvent('edge:duplicates', { detail: report }));
      return report;
    })();
    return inflight;
  }

  function camName(group, id) {
    const c = (group.cameras || []).find((x) => x.camera_id === id);
    return c ? c.name : id;
  }

  function list(items) {
    const rows = (items || []).filter((x) => x.count);
    if (!rows.length) return '<li>nothing</li>';
    return rows.map((x) => `<li><b>${esc(x.count)}</b> ${esc(x.what)}</li>`).join('');
  }

  function groupHtml(g) {
    const st = ui[g.id] || {};
    const primary = g.cameras.find((c) => c.primary) || g.cameras[0];
    const others = g.cameras.filter((c) => !c.primary);
    const A = esc(primary.name);
    let html = `<div class="dup-banner" data-dup-group="${esc(g.id)}" role="group" aria-label="Duplicate camera">
      <div class="dup-title">${ico('triangle-alert')} ${esc(g.message)}</div>
      <div class="dup-sub">Only <b>${A}</b> counts in store totals (visitors, in store now, heatmaps). ${
        others.map((c) => `<b>${esc(c.name)}</b>`).join(', ')} ${others.length === 1 ? 'is' : 'are'} marked
        <span class="dev-tag warn">duplicate — excluded from store totals</span> and still viewable.
        Connect each camera once, as its NVR channel.</div>`;
    others.forEach((b) => {
      const B = esc(b.name);
      const id = esc(b.camera_id);
      const dis = st.busy ? ' disabled' : '';
      html += `<div class="dup-actions" role="group" aria-label="Resolve ${B}">
        ${b.enabled
    ? `<button type="button" class="btn btn-xs btn-primary" data-dup="off" data-cam="${id}"${dis}
          title="Stop ${B}: no video and no analysis until it is turned on again">Keep ${A}, turn off ${B}</button>`
    : `<span class="dev-tag">${B} is turned off</span>`}
        <button type="button" class="btn btn-secondary btn-xs" data-dup="ask-remove" data-cam="${id}"${dis}>Keep ${A}, remove ${B}</button>
        <button type="button" class="btn btn-secondary btn-xs" data-dup="primary" data-cam="${id}"${dis}
          title="Count ${B} in store totals instead of ${A}">Make ${B} primary</button>
        <button type="button" class="btn btn-secondary btn-xs" data-dup="dismiss" data-cam="${id}" data-primary="${esc(primary.camera_id)}"${dis}
          title="Not the same camera: both count in store totals and this warning is not shown again">They're different cameras</button>
      </div>`;
      if (st.remove === b.camera_id) {
        const imp = st.impact;
        html += imp ? `<div class="dup-impact" role="group" aria-label="Confirm removal of ${B}">
            <div class="dup-impact-cols">
              <div><div class="dup-impact-head">Deleted with the camera</div><ul>${list(imp.deleted)}</ul></div>
              <div><div class="dup-impact-head">Kept</div><ul>${list(imp.kept)}</ul></div>
            </div>
            <div class="dev-hint">${esc(imp.note || '')}</div>
            <div class="fp-actions">
              <button type="button" class="btn btn-xs btn-danger" data-dup="do-remove" data-cam="${id}"${dis}>Remove ${B}</button>
              <button type="button" class="btn btn-secondary btn-xs" data-dup="cancel"${dis}>Cancel</button>
            </div></div>`
          : '<div class="dev-hint">Checking what removing it would delete…</div>';
      }
    });
    if (st.msg && st.msg.text) html += `<div class="fp-status fp-${esc(st.msg.kind || 'info')}" role="status">${esc(st.msg.text)}</div>`;
    return html + '</div>';
  }

  function recordersHtml() {
    const recs = (report && report.recorders) || [];
    return recs.filter((r) => !r.ok).map((r) => `<div class="dup-recorder dev-hint">Recorder ${esc(r.host)}: ${esc(r.note || '')}
      <button type="button" class="btn btn-secondary btn-xs" data-dup="refresh" data-host="${esc(r.host)}"${r.refreshing ? ' disabled' : ''}
        title="Signs in to the recorder once and reads which camera is on each channel">Read its camera list now</button></div>`).join('');
  }

  function render(host) {
    if (!host) return;
    const groups = (report && report.groups) || [];
    // The last action's outcome, once its group is gone (resolved).
    const done = lastMsg && !groups.some((g) => g.id === lastMsg.gid)
      ? `<div class="fp-status fp-${esc(lastMsg.kind)}" role="status">${esc(lastMsg.text)}${
        groups.length ? '' : ' No duplicate camera left.'}</div>` : '';
    if (!groups.length) {
      host.hidden = !done;
      host.innerHTML = done;
      return;
    }
    host.hidden = false;
    host.innerHTML = done + groups.map(groupHtml).join('') + recordersHtml();
  }

  function renderAll() { MOUNTS.forEach((id) => render($(id))); }

  function changed() {
    window.dispatchEvent(new CustomEvent('edge:cameras-changed', { detail: { from: 'duplicates' } }));
  }

  async function act(gid, fn, okText) {
    ui[gid] = { ...(ui[gid] || {}), busy: true };
    renderAll();
    try {
      const res = await fn();
      if (!res.ok) { ui[gid] = { msg: { text: await errorText(res, 'Request failed'), kind: 'error' } }; renderAll(); return false; }
      ui[gid] = { msg: { text: okText, kind: 'ok' } };
      lastMsg = { gid, text: okText, kind: 'ok' };
      changed();
      await load();
      return true;
    } catch (e) {
      ui[gid] = { msg: { text: `Request failed: ${e.message}`, kind: 'error' } };
      renderAll();
      return false;
    }
  }

  const json = (method, body) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

  async function onClick(ev) {
    const btn = ev.target.closest('[data-dup]');
    if (!btn || btn.disabled) return;
    const box = btn.closest('[data-dup-group]');
    const gid = box ? box.getAttribute('data-dup-group') : null;
    const group = gid && report ? (report.groups || []).find((g) => g.id === gid) : null;
    const a = btn.getAttribute('data-dup');
    const cid = btn.getAttribute('data-cam');
    if (a === 'refresh') {
      btn.disabled = true;
      btn.textContent = 'Reading…';
      try {
        // The outcome (or why the list could not be read) is in the next report's recorder note.
        await fetch('/api/v1/cameras/duplicates/refresh', json('POST', { host: btn.getAttribute('data-host') }));
      } catch (_) { /* shown by the next load */ }
      await load();
      return;
    }
    if (!group) return;
    const name = camName(group, cid);
    if (a === 'off') {
      await act(gid, () => fetch(`/api/v1/cameras/${encodeURIComponent(cid)}/enabled`, json('PUT', { enabled: false })),
        `${name} turned off.`);
    } else if (a === 'primary') {
      await act(gid, () => fetch('/api/v1/cameras/duplicates/primary', json('POST', { camera_id: cid })),
        `${name} now counts in store totals.`);
    } else if (a === 'dismiss') {
      await act(gid, () => fetch('/api/v1/cameras/duplicates/dismiss',
        json('POST', { camera_ids: [btn.getAttribute('data-primary'), cid] })), 'Marked as different cameras.');
    } else if (a === 'cancel') {
      ui[gid] = {};
      renderAll();
    } else if (a === 'ask-remove') {
      ui[gid] = { remove: cid, impact: null };
      renderAll();
      try {
        const res = await fetch(`/api/v1/cameras/${encodeURIComponent(cid)}/delete-impact`, { cache: 'no-store' });
        if (!res.ok) { ui[gid] = { msg: { text: await errorText(res, 'Could not check'), kind: 'error' } }; renderAll(); return; }
        const impact = await res.json();
        if ((ui[gid] || {}).remove === cid) { ui[gid] = { remove: cid, impact }; renderAll(); }
      } catch (e) {
        ui[gid] = { msg: { text: `Could not check: ${e.message}`, kind: 'error' } };
        renderAll();
      }
    } else if (a === 'do-remove') {
      const ok = await act(gid, () => fetch(`/api/v1/layout/cameras/${encodeURIComponent(cid)}`, { method: 'DELETE' }),
        `Removed ${name}.`);
      if (ok && window.blueprintEditor && typeof window.blueprintEditor.load === 'function') {
        try { await window.blueprintEditor.load(); } catch (_) { /* editor not ready */ }
      }
      if (ok && window.deviceManager) window.deviceManager.refresh();
    }
  }

  /**
   * The 409 panel of an add / adopt: the existing camera, the advice, and
   * "Use existing camera" / "Add anyway" (data-dup-conflict="use|anyway").
   */
  function conflictHtml(detail, opts) {
    const o = opts || {};
    const d = detail || {};
    const names = (d.existing || []).map((c) => `<li>${esc(c.name)} <span class="dev-hint">(${esc(c.source || c.description || '')})</span></li>`).join('');
    const chans = (d.channels || []).map((c) => `<li>Channel ${esc(c.channel)}: already added as ${
      (c.existing || []).map((x) => esc(x.name)).join(', ')}</li>`).join('');
    return `<div class="dup-conflict" role="alert">
      <div class="dup-title">${ico('triangle-alert')} Same camera already added</div>
      <div class="dup-sub">${esc(d.message || 'This camera is already configured.')}</div>
      ${names || chans ? `<ul class="dup-list">${names}${chans}</ul>` : ''}
      <div class="fp-actions">
        <button type="button" class="btn btn-xs btn-primary" data-dup-conflict="use">${esc(o.useLabel || 'Use existing camera')}</button>
        <button type="button" class="btn btn-secondary btn-xs" data-dup-conflict="anyway"
          title="Only for a genuine second stream, e.g. a main-stream close-up: it will be excluded from store totals">${esc(o.anywayLabel || 'Add anyway')}</button>
      </div></div>`;
  }

  /** Render conflictHtml into ``box`` and wire its two buttons. */
  function showConflict(box, detail, handlers, opts) {
    if (!box) return;
    box.hidden = false;
    box.innerHTML = conflictHtml(detail, opts);
    box.querySelectorAll('[data-dup-conflict]').forEach((b) => b.addEventListener('click', () => {
      const kind = b.getAttribute('data-dup-conflict');
      box.hidden = true;
      box.innerHTML = '';
      if (kind === 'use' && handlers.use) handlers.use(detail);
      if (kind === 'anyway' && handlers.anyway) handlers.anyway(detail);
    }));
  }

  /** The 409 body of a duplicate refusal, or null. Reads the response. */
  async function conflictOf(res) {
    if (res.status !== 409) return null;
    try {
      const d = (await res.clone().json()).detail;
      return d && typeof d === 'object' && d.code === 'duplicate_camera' ? d : null;
    } catch (_) { return null; }
  }

  function init() {
    document.addEventListener('click', onClick);
    window.addEventListener('edge:cameras-changed', (e) => { if (!(e.detail && e.detail.from === 'duplicates')) load(); });
    window.addEventListener('edge:tab', (e) => {
      const t = e.detail && e.detail.tab;
      if (t === 'cameras' || t === 'map' || t === 'today') load();
    });
    load();
  }

  window.edgeDuplicates = { load, report: () => report, conflictHtml, showConflict, conflictOf, errorText };

  if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') window.edgeAuth.onReady(init);
  else if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
