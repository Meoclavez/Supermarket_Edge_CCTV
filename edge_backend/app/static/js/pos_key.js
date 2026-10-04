// Settings > Sales data (point of sale) > "Till key" (#posTillKey). Owner/admin only.
//
//   GET    /api/v1/analytics/pos/till-key   status: exists, created, last used, last 4 characters
//   POST   /api/v1/analytics/pos/till-key   create or replace; the key is in the response once
//   DELETE /api/v1/analytics/pos/till-key   revoke
//
// The till sends the key in the X-Edge-API-Key header to POST /api/v1/analytics/pos/ingest.
// No prompt()/confirm()/alert(): confirmations are inline ("Yes, replace" / "Keep").
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const DASH = '—';
  const state = { status: null, error: null, busy: false, confirm: null, newKey: null, msg: '', msgError: false };

  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  async function api(url, opts, fallback) {
    const res = await fetch(url, Object.assign({ cache: 'no-store' }, opts || {}));
    if (!res.ok) {
      let d = null;
      try { d = (await res.json()).detail; } catch (_) { /* not JSON */ }
      throw new Error(typeof d === 'string' ? d : `${fallback} (HTTP ${res.status})`);
    }
    return res.json();
  }

  function fmtTime(iso) {
    if (!iso) return null;
    const t = new Date(iso);
    return Number.isNaN(t.getTime()) ? String(iso) : t.toLocaleString();
  }

  function isAdmin() {
    const a = window.EdgeAuth;
    return !(a && a.known && !a.isAdmin);
  }

  function settingsVisible() {
    const tab = $('tab-settings');
    return !!(tab && tab.classList.contains('active') && !document.hidden);
  }

  function render() {
    const box = $('posTillKey');
    if (!box) return;
    const header = (state.status && state.status.header) || 'X-Edge-API-Key';
    if (!isAdmin()) {
      box.innerHTML = `<div class="tk-title">Till key</div>
        <p class="ra-hint">Only an owner or administrator can see and manage the till key.</p>`;
      return;
    }
    if (state.error) {
      box.innerHTML = `<div class="tk-title">Till key</div><p class="ra-hint">${esc(state.error)}</p>`;
      return;
    }
    const s = state.status;
    if (!s) { box.innerHTML = '<div class="tk-title">Till key</div><div class="fp-empty">Loading…</div>'; return; }
    const dis = state.busy ? ' disabled' : '';
    const statusLine = s.exists
      ? `A till key is set (ends in <code>${esc(s.hint || DASH)}</code>). Created ${esc(fmtTime(s.created_at) || DASH)}. Last used ${esc(fmtTime(s.last_used_at) || 'never')}.`
      : 'No till key has been created yet.';
    let actions;
    if (state.confirm === 'replace') {
      actions = `<div class="tk-confirm">Replace the till key? Tills using the current key stop sending sales until they are updated with the new one.
          <button type="button" class="btn btn-danger btn-sm" data-tk="replace-yes"${dis}>Yes, replace</button>
          <button type="button" class="btn btn-sm" data-tk="keep">Keep</button></div>`;
    } else if (state.confirm === 'revoke') {
      actions = `<div class="tk-confirm">Revoke the till key? Tills using it stop sending sales immediately.
          <button type="button" class="btn btn-danger btn-sm" data-tk="revoke-yes"${dis}>Yes, revoke</button>
          <button type="button" class="btn btn-sm" data-tk="keep">Keep</button></div>`;
    } else if (s.exists) {
      actions = `<div class="tk-actions">
          <button type="button" class="btn btn-sm" data-tk="replace"${dis}>Replace till key</button>
          <button type="button" class="btn btn-danger btn-sm" data-tk="revoke"${dis}>Revoke</button></div>`;
    } else {
      actions = `<div class="tk-actions"><button type="button" class="btn btn-primary btn-sm" data-tk="create"${dis}>Create till key</button></div>`;
    }
    const fresh = state.newKey ? `<div class="tk-new">
        <div class="tk-new-row"><code class="tk-key" id="tkNewKey">${esc(state.newKey)}</code>
          <button type="button" class="btn btn-sm" data-tk="copy">Copy</button>
          <button type="button" class="btn btn-sm" data-tk="hide">Done</button></div>
        <p class="tk-warn">Shown once. Store it in the till system now.</p>
        <p class="ra-hint">The till sends it in the <code>${esc(header)}</code> header with each sale.</p>
      </div>` : '';
    box.innerHTML = `<div class="tk-title">Till key</div>
      <p class="ra-hint">The till system sends this key in the <code>${esc(header)}</code> header. It only lets the till send sales; it opens nothing else.</p>
      <p class="tk-status">${statusLine}</p>
      ${fresh}
      ${actions}
      <div class="form-status${state.msgError ? ' form-status-error' : ''}" role="status" aria-live="polite">${esc(state.msg)}</div>`;
  }

  async function load() {
    if (!isAdmin()) { render(); return; }
    try {
      state.status = await api('/api/v1/analytics/pos/till-key', null, 'Could not load the till key status');
      state.error = null;
    } catch (e) {
      state.error = `Till key status is unavailable: ${e.message}`;
    }
    render();
  }

  async function create() {
    state.busy = true; state.confirm = null; state.msg = ''; render();
    try {
      const r = await api('/api/v1/analytics/pos/till-key', { method: 'POST' }, 'Could not create the till key');
      state.newKey = r.key || null;
      state.status = r;
      state.msg = ''; state.msgError = false;
    } catch (e) {
      state.msg = e.message; state.msgError = true;
    }
    state.busy = false;
    render();
  }

  async function revoke() {
    state.busy = true; state.confirm = null; render();
    try {
      state.status = await api('/api/v1/analytics/pos/till-key', { method: 'DELETE' }, 'Could not revoke the till key');
      state.newKey = null;
      state.msg = 'Till key revoked.'; state.msgError = false;
    } catch (e) {
      state.msg = e.message; state.msgError = true;
    }
    state.busy = false;
    render();
  }

  async function copyKey() {
    const key = state.newKey;
    if (!key) return;
    let ok = false;
    try {
      if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(key); ok = true; }
    } catch (_) { ok = false; }
    if (!ok) {
      // Plain http on the store network: select the key so Ctrl+C copies it.
      const el = $('tkNewKey');
      if (el && window.getSelection) {
        const r = document.createRange(); r.selectNodeContents(el);
        const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r);
        try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
      }
    }
    state.msg = ok ? 'Copied.' : 'Copy is blocked by the browser here: the key is selected, press Ctrl+C.';
    state.msgError = !ok;
    const keep = !ok;
    render();
    if (keep) {
      const el = $('tkNewKey');
      if (el && window.getSelection) {
        const r = document.createRange(); r.selectNodeContents(el);
        const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r);
      }
    }
  }

  document.addEventListener('click', (e) => {
    const btn = e.target.closest('#posTillKey [data-tk]');
    if (!btn || btn.disabled) return;
    const act = btn.getAttribute('data-tk');
    if (act === 'create' || act === 'replace-yes') create();
    else if (act === 'replace') { state.confirm = 'replace'; state.msg = ''; render(); }
    else if (act === 'revoke') { state.confirm = 'revoke'; state.msg = ''; render(); }
    else if (act === 'revoke-yes') revoke();
    else if (act === 'keep') { state.confirm = null; render(); }
    else if (act === 'copy') copyKey();
    else if (act === 'hide') { state.newKey = null; state.msg = ''; render(); }
  });

  window.addEventListener('edge:tab', (ev) => {
    if (ev.detail && ev.detail.tab === 'settings') load();
    else if (state.newKey) { state.newKey = null; render(); } // never leave the key on screen
  });
  window.addEventListener('edge:account', () => { if (settingsVisible()) load(); });
  const boot = () => { if (settingsVisible()) load(); else render(); };
  if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') window.edgeAuth.onReady(boot);
  else if (document.readyState !== 'loading') boot();
  else document.addEventListener('DOMContentLoaded', boot);
})();
