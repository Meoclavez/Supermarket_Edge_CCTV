// Settings: device identity + online access panels.
//
// #settings-device  device id (copy) and store/device name (rename)
//                   GET/PUT /api/v1/device/identity
// #settings-remote  online access: the dashboard at https://<public address>
//                   through the owner's own server (VPS reverse tunnel)
//                   GET/PUT /api/v1/remote-access, POST /api/v1/remote-access/verify
//
// The store token and the optional server key are write-only: the server only
// ever says whether one is configured. No prompt()/confirm()/alert(): every confirmation and error is
// inline. Data loads when the Settings tab is opened (window event edge:tab).
(function () {
  'use strict';

  const POLL_MS = 3000;
  let pollTimer = null;
  let state = null;          // last GET /api/v1/remote-access
  let identity = null;       // last GET /api/v1/device/identity
  let tokenMode = 'idle';    // idle | replace | confirm-remove
  let dirty = false;         // form edited since the last load

  const $ = (id) => document.getElementById(id);

  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  async function apiError(res, fallback) {
    let data = {};
    try { data = await res.json(); } catch (_) { /* not JSON */ }
    let d = data && data.detail;
    if (Array.isArray(d)) d = d.map((x) => (x && x.msg) || String(x)).join('; ');
    return d || `${fallback} (HTTP ${res.status})`;
  }

  function fmtTime(iso) {
    if (!iso) return '';
    const t = new Date(iso);
    return Number.isNaN(t.getTime()) ? String(iso) : t.toLocaleString();
  }

  function setStatus(id, text, isError) {
    const el = $(id);
    if (!el) return;
    el.textContent = text || '';
    el.classList.toggle('form-status-error', !!isError);
  }

  // ------------------------------------------------------------------ device

  function renderDevice(errorText) {
    const host = $('settings-device');
    if (!host) return;
    const id = identity && identity.device_id;
    const name = identity && identity.device_name;
    host.innerHTML = `
      <div class="card-title"><span>Device</span></div>
      <div class="ra-row">
        <div class="form-group ra-grow">
          <label for="raDeviceId">Device ID</label>
          <div class="ra-inline">
            <code class="ra-code" id="raDeviceId">${id ? esc(id) : '—'}</code>
            <button type="button" class="btn btn-sm" id="raCopyId" ${id ? '' : 'disabled'}>Copy</button>
          </div>
          <div class="ra-hint">Permanent for this installation. Phones use it to confirm they are talking to this store.</div>
        </div>
      </div>
      <form id="raRenameForm" class="ra-row" autocomplete="off">
        <div class="form-group ra-grow">
          <label for="raDeviceName">Device name</label>
          <input class="form-input" id="raDeviceName" maxlength="80" value="${esc(name || '')}"
                 placeholder="e.g. IGA Pearcedale" ${identity ? '' : 'disabled'}>
        </div>
        <button type="submit" class="btn btn-primary btn-sm ra-align-end" id="raRenameBtn" ${identity ? '' : 'disabled'}>Rename</button>
      </form>
      <div class="form-status ${errorText ? 'form-status-error' : ''}" id="raDeviceStatus">${esc(errorText || '')}</div>`;

    const copy = $('raCopyId');
    if (copy) copy.addEventListener('click', () => copyText(id, 'raDeviceStatus'));
    const form = $('raRenameForm');
    if (form) form.addEventListener('submit', onRename);
  }

  async function copyText(text, statusId) {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setStatus(statusId, 'Device ID copied.');
    } catch (_) {
      // Clipboard API needs a secure context (https or localhost): select the
      // text so the operator can copy it with Ctrl+C instead.
      const code = $('raDeviceId');
      if (code && window.getSelection) {
        const r = document.createRange();
        r.selectNodeContents(code);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(r);
      }
      setStatus(statusId, 'Selected — press Ctrl+C to copy.');
    }
  }

  async function loadIdentity() {
    try {
      const res = await fetch('/api/v1/device/identity', { cache: 'no-store' });
      if (!res.ok) throw new Error(await apiError(res, 'Device identity unavailable'));
      identity = await res.json();
      renderDevice();
    } catch (e) {
      identity = null;
      renderDevice(e.message);
    }
  }

  async function onRename(ev) {
    ev.preventDefault();
    const input = $('raDeviceName');
    const btn = $('raRenameBtn');
    const name = (input.value || '').trim();
    if (!name) { setStatus('raDeviceStatus', 'Enter a name.', true); return; }
    btn.disabled = true;
    setStatus('raDeviceStatus', 'Saving…');
    try {
      const res = await fetch('/api/v1/device/identity', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_name: name }),
      });
      if (!res.ok) throw new Error(await apiError(res, 'Rename failed'));
      identity = Object.assign({}, identity, await res.json());
      renderDevice();
      setStatus('raDeviceStatus', 'Device name saved.');
    } catch (e) {
      setStatus('raDeviceStatus', e.message, true);
      btn.disabled = false;
    }
  }

  // ------------------------------------------------------------------ remote

  const PROCESS_LABEL = {
    stopped: 'Stopped', starting: 'Connecting…', connected: 'Connected', error: 'Error',
  };

  function statusDotClass(p) {
    return { connected: 'ra-dot-ok', starting: 'ra-dot-wait', error: 'ra-dot-err' }[p] || 'ra-dot-off';
  }

  function processLabel(s) {
    const p = (s && s.process) || 'stopped';
    if (p === 'connected' && s.connected_since) return `Connected since ${fmtTime(s.connected_since)}`;
    return PROCESS_LABEL[p] || p;
  }

  function processError(s) {
    if (!s || !s.last_error) return '';
    return s.process === 'error' ? `Error: ${s.last_error}` : s.last_error;
  }

  function renderRemote(errorText) {
    const host = $('settings-remote');
    if (!host) return;
    const s = state || {};
    const proc = s.process || 'stopped';
    const tokenBadge = s.token_configured
      ? '<span class="badge badge-green" id="raTokenState">saved</span>'
      : '<span class="badge badge-warning" id="raTokenState">not set</span>';
    const showTokenInput = !s.token_configured || tokenMode === 'replace';
    const verifiedBadge = s.verified
      ? `<span class="badge badge-green" id="raVerified">Verified ${esc(fmtTime(s.verified_at))}</span>`
      : '<span class="badge badge-warning" id="raVerified">Not verified</span>';

    // Verifying is only meaningful once the tunnel is up.
    const canVerify = !!s.hostname && proc === 'connected';
    const verifyTitle = !s.hostname ? 'Enter the public address first'
      : (canVerify ? 'Check that the public address reaches this device' : 'Verify once the status shows Connected');

    host.innerHTML = `
      <div class="card-title"><span>Online access</span></div>
      <div class="ra-hint">Open this dashboard from anywhere at <b>https://</b> your own address, through your own
        server. Everything still runs on this machine, and it is online only while this is enabled and connected.</div>

      <div class="ra-statusbar" id="raStatusBar">
        <span class="ra-dot ${statusDotClass(proc)}" id="raDot"></span>
        <span class="ra-status-text" id="raProcess" data-process="${esc(proc)}">${esc(processLabel(s))}</span>
        ${s.enabled && s.public_url ? `<a class="ra-link" id="raPublicUrl" href="${esc(s.public_url)}" target="_blank" rel="noopener">${esc(s.public_url)}</a>` : ''}
        <span class="ra-spacer"></span>
        ${verifiedBadge}
        <button type="button" class="btn btn-sm" id="raVerifyBtn" ${canVerify ? '' : 'disabled'} title="${esc(verifyTitle)}">Verify now</button>
      </div>
      <div class="form-status form-status-error" id="raProcessError">${esc(processError(s))}</div>
      <div class="form-status ${s.verified ? '' : 'form-status-error'}" id="raVerifyStatus">${s.verified ? '' : esc(s.verify_error || '')}</div>
      ${s.auth_disabled ? '<div class="form-status form-status-error">AUTH_DISABLED is on: online access cannot be enabled until sign-in is turned back on.</div>' : ''}
      ${s.tunnel_client_found === false ? '<div class="form-status form-status-error" id="raNoBinary">The tunnel program is not installed on this machine yet. Ask your installer to re-run the installer with <code>EDGE_TUNNEL=1</code>.</div>' : ''}

      <form id="raForm" autocomplete="off">
        <label class="checkbox-label ra-toggle">
          <input type="checkbox" id="raEnabled" ${s.enabled ? 'checked' : ''}> Enable online access
        </label>
        <div class="form-grid-2col ra-fields">
          <div class="form-group">
            <label for="raHostname">Public address</label>
            <input class="form-input" id="raHostname" value="${esc(s.hostname || '')}" placeholder="e.g. pearcedale-cctv.ikorex.com.au"
                   spellcheck="false" autocapitalize="off">
            <div class="ra-hint">The address you will open in the browser.</div>
          </div>
          <div class="form-group">
            <label for="raServer">Tunnel server</label>
            <input class="form-input" id="raServer" value="${esc(s.server_url || '')}" placeholder="e.g. wss://tunnel.ikorex.com.au"
                   spellcheck="false" autocapitalize="off">
            <div class="ra-hint">Where this machine connects to your server.</div>
          </div>
        </div>

        <div class="form-grid-2col ra-fields">
          <div class="form-group">
            <label for="raStoreId">Store ID</label>
            <input class="form-input" id="raStoreId" value="${esc(s.store_id || '')}" placeholder="e.g. pearcedale"
                   spellcheck="false" autocapitalize="off" maxlength="40">
            <div class="ra-hint">Identifies this store on your server.</div>
          </div>
          <div class="form-group ra-token">
            <label for="raServerKey">Server key <span class="ra-hint">(optional)</span> ${s.server_key_configured
              ? '<span class="badge badge-green" id="raServerKeyState">saved</span>' : ''}</label>
            ${s.server_key_configured ? `
              <div class="ra-inline">
                <span class="ra-hint">Stored encrypted on this device.</span>
                <button type="button" class="btn btn-sm" id="raServerKeyRemove">Remove</button>
              </div>` : `
              <input class="form-input" id="raServerKey" type="password" autocomplete="new-password" spellcheck="false"
                     placeholder="Only if your installer gave you one">`}
          </div>
        </div>

        <div class="form-group">
          <label for="raExtraProxies">Proxies in front of your server</label>
          <select class="form-select" id="raExtraProxies">
            ${[[0, 'None: visitors connect to your server directly'], [1, 'One (a CDN in front of your server)'],
               [2, 'Two']].map(([v, t]) => `<option value="${v}" ${Number(s.extra_proxies || 0) === v ? 'selected' : ''}>${t}</option>`).join('')}
          </select>
          <div class="ra-hint">Used to tell visitors apart for sign-in lockouts. Only change it if your installer says so.</div>
        </div>

        <div class="form-group ra-token" id="raTokenGroup">
          <label for="raToken">Store token ${tokenBadge}</label>
          ${showTokenInput ? `
            <input class="form-input" id="raToken" type="password" autocomplete="new-password" spellcheck="false"
                   placeholder="${s.token_configured ? 'Enter the new token' : 'Enter the token'}">
            ${tokenMode === 'replace' ? '<button type="button" class="btn btn-sm ra-mt" id="raTokenCancel">Keep current token</button>' : ''}`
          : tokenMode === 'confirm-remove' ? `
            <div class="ra-inline ra-confirm" id="raRemoveConfirm">
              <span>Remove the stored store token? Online access stops.</span>
              <button type="button" class="btn btn-danger btn-sm" id="raTokenRemoveYes">Remove token</button>
              <button type="button" class="btn btn-sm" id="raTokenRemoveNo">Cancel</button>
            </div>`
          : `
            <div class="ra-inline">
              <span class="ra-hint">Stored encrypted on this device. It is never shown again.</span>
              <button type="button" class="btn btn-sm" id="raTokenReplace">Replace token</button>
              <button type="button" class="btn btn-danger btn-sm" id="raTokenRemove">Remove</button>
            </div>`}
        </div>

        <div class="ra-inline ra-mt">
          <button type="submit" class="btn btn-primary btn-sm" id="raSave">Save</button>
          <span class="ra-hint">Changes apply when you press Save.</span>
        </div>
        <div class="form-status ${errorText ? 'form-status-error' : ''}" id="raFormStatus">${esc(errorText || '')}</div>
      </form>
      <div class="ra-hint ra-mt" id="raHelp">Ask your installer for the tunnel server, store ID and store token. After saving, the
        status turns <b>Connected</b> within a few seconds; then press <b>Verify now</b>. Creating the first
        operator account only works on the store network, never through this address.</div>`;

    bindRemote();
  }

  function bindRemote() {
    const form = $('raForm');
    if (!form) return;
    form.addEventListener('submit', onSave);
    form.addEventListener('input', () => { dirty = true; });
    const on = (id, fn) => { const el = $(id); if (el) el.addEventListener('click', fn); };
    on('raVerifyBtn', onVerify);
    on('raTokenReplace', () => { const k = snapshotForm(); tokenMode = 'replace'; renderRemote(); restoreForm(k); const t = $('raToken'); if (t) t.focus(); });
    on('raTokenCancel', () => { const k = snapshotForm(); tokenMode = 'idle'; renderRemote(); restoreForm(k); });
    on('raTokenRemove', () => { const k = snapshotForm(); tokenMode = 'confirm-remove'; renderRemote(); restoreForm(k); });
    on('raTokenRemoveNo', () => { const k = snapshotForm(); tokenMode = 'idle'; renderRemote(); restoreForm(k); });
    on('raTokenRemoveYes', onRemoveToken);
    on('raServerKeyRemove', onRemoveServerKey);
  }

  function snapshotForm() {
    return {
      enabled: $('raEnabled') ? $('raEnabled').checked : undefined,
      hostname: $('raHostname') ? $('raHostname').value : undefined,
      server: $('raServer') ? $('raServer').value : undefined,
      storeId: $('raStoreId') ? $('raStoreId').value : undefined,
      extra: $('raExtraProxies') ? $('raExtraProxies').value : undefined,
      token: $('raToken') ? $('raToken').value : '',
      serverKey: $('raServerKey') ? $('raServerKey').value : '',
    };
  }

  function restoreForm(k) {
    if (!k) return;
    if ($('raEnabled') && k.enabled !== undefined) $('raEnabled').checked = k.enabled;
    if ($('raHostname') && k.hostname !== undefined) $('raHostname').value = k.hostname;
    if ($('raServer') && k.server !== undefined) $('raServer').value = k.server;
    if ($('raStoreId') && k.storeId !== undefined) $('raStoreId').value = k.storeId;
    if ($('raExtraProxies') && k.extra !== undefined) $('raExtraProxies').value = k.extra;
    if ($('raToken') && k.token) $('raToken').value = k.token;
    if ($('raServerKey') && k.serverKey) $('raServerKey').value = k.serverKey;
  }

  async function putRemote(body, statusId, okText) {
    setStatus(statusId, 'Saving…');
    const res = await fetch('/api/v1/remote-access', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(await apiError(res, 'Could not save the online access settings'));
    state = await res.json();
    dirty = false;
    tokenMode = 'idle';
    renderRemote();
    setStatus('raFormStatus', okText);
    return state;
  }

  async function onSave(ev) {
    ev.preventDefault();
    const btn = $('raSave');
    const f = snapshotForm();
    const body = {
      enabled: f.enabled, hostname: (f.hostname || '').trim(), server_url: (f.server || '').trim(),
      store_id: (f.storeId || '').trim(), extra_proxies: Number(f.extra || 0),
    };
    if (f.token && f.token.trim()) body.token = f.token.trim();
    if (f.serverKey && f.serverKey.trim()) body.server_key = f.serverKey.trim();
    btn.disabled = true;
    try {
      await putRemote(body, 'raFormStatus',
        body.enabled ? 'Saved. Online access is enabled.' : 'Saved. Online access is disabled.');
      schedulePoll();
    } catch (e) {
      setStatus('raFormStatus', e.message, true);
      btn.disabled = false;
    }
  }

  async function onRemoveToken() {
    const keep = snapshotForm();
    try {
      await putRemote({ clear_token: true, enabled: false }, 'raFormStatus',
        'Token removed. Online access is disabled.');
    } catch (e) {
      tokenMode = 'idle';
      renderRemote(e.message);
      restoreForm(keep);
    }
  }

  async function onRemoveServerKey() {
    const keep = snapshotForm();
    try {
      await putRemote({ clear_server_key: true }, 'raFormStatus', 'Server key removed.');
      restoreForm(Object.assign(keep, { serverKey: '' }));
    } catch (e) {
      renderRemote(e.message);
      restoreForm(keep);
    }
  }

  async function onVerify() {
    const btn = $('raVerifyBtn');
    btn.disabled = true;
    setStatus('raVerifyStatus', 'Checking that the public address reaches this device…');
    try {
      const res = await fetch('/api/v1/remote-access/verify', { method: 'POST' });
      if (!res.ok) throw new Error(await apiError(res, 'Verification failed'));
      const keep = dirty ? snapshotForm() : null;
      state = await res.json();
      renderRemote();
      if (keep) restoreForm(keep);
      setStatus('raVerifyStatus', state.verified ? `Verified: ${state.public_url || state.hostname} reaches this device.`
        : (state.verify_error || 'Not verified.'), !state.verified);
    } catch (e) {
      setStatus('raVerifyStatus', e.message, true);
      btn.disabled = false;
    }
  }

  // Refresh only the live parts while the operator is editing, so typing is
  // never wiped by the poll.
  function applyLiveStatus(s) {
    const prev = state || {};
    state = s;
    const structural = prev.token_configured !== s.token_configured || prev.enabled !== s.enabled
      || prev.hostname !== s.hostname || prev.server_url !== s.server_url || prev.verified !== s.verified
      || prev.store_id !== s.store_id || prev.server_key_configured !== s.server_key_configured
      || prev.extra_proxies !== s.extra_proxies
      || prev.tunnel_client_found !== s.tunnel_client_found || prev.auth_disabled !== s.auth_disabled
      || (prev.process === 'connected') !== (s.process === 'connected');
    if (structural && !dirty && tokenMode === 'idle') {
      renderRemote();
      return;
    }
    const dot = $('raDot');
    if (dot) dot.className = `ra-dot ${statusDotClass(s.process)}`;
    const p = $('raProcess');
    if (p) { p.textContent = processLabel(s); p.dataset.process = s.process; }
    const err = $('raProcessError');
    if (err) err.textContent = processError(s);
    const vb = $('raVerifyBtn');
    if (vb) vb.disabled = !(s.hostname && s.process === 'connected');
  }

  async function loadRemote(initial) {
    try {
      const res = await fetch('/api/v1/remote-access', { cache: 'no-store' });
      if (res.status === 401) { stopPoll(); return; }
      if (!res.ok) throw new Error(await apiError(res, 'Online access status unavailable'));
      const s = await res.json();
      if (initial || !state) { state = s; renderRemote(); } else { applyLiveStatus(s); }
    } catch (e) {
      if (initial || !state) renderRemote(e.message);
      else setStatus('raProcessError', e.message, true);
    }
  }

  function settingsVisible() {
    const tab = $('tab-settings');
    return !!(tab && tab.classList.contains('active') && !document.hidden);
  }

  function stopPoll() { if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; } }

  function schedulePoll() {
    stopPoll();
    pollTimer = setTimeout(async () => {
      pollTimer = null;
      if (!settingsVisible()) return;
      await loadRemote(false);
      schedulePoll();
    }, POLL_MS);
  }

  function openSettings() {
    if (window.edgeAuth && window.edgeAuth.isGateOpen && window.edgeAuth.isGateOpen()) return;
    dirty = false;
    tokenMode = 'idle';
    loadIdentity();
    loadRemote(true).then(schedulePoll);
  }

  window.addEventListener('edge:tab', (ev) => {
    if (ev.detail && ev.detail.tab === 'settings') openSettings();
    else stopPoll();
  });
  document.addEventListener('visibilitychange', () => {
    if (settingsVisible() && !pollTimer) schedulePoll();
  });
  // The page may already be on #settings before this script registered.
  const boot = () => { if (settingsVisible()) openSettings(); };
  // Start only after auth.js has checked the stored token (edgeAuth.onReady).
  if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') window.edgeAuth.onReady(boot);
  else if (document.readyState !== 'loading') boot();
  else document.addEventListener('DOMContentLoaded', boot);

  window.edgeRemoteAccess = { reload: openSettings };
})();
