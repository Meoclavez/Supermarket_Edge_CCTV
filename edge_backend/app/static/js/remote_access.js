// Settings: device identity + remote access panels. Owned by the remote-access work.
//
// #settings-device  device id (copy) and store/device name (rename)
//                   GET/PUT /api/v1/device/identity
// #settings-remote  online dashboard on the operator's own domain
//                   GET/PUT /api/v1/remote-access, POST /api/v1/remote-access/verify
//
// The tunnel token is write-only: the server only ever says whether one is
// configured. No prompt()/confirm()/alert(): every confirmation and error is
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
    stopped: 'Stopped', starting: 'Connecting…', connected: 'Connected', error: 'Error', external: 'External proxy',
  };

  function statusDotClass(p) {
    return { connected: 'ra-dot-ok', starting: 'ra-dot-wait', error: 'ra-dot-err', external: 'ra-dot-wait' }[p] || 'ra-dot-off';
  }

  function stepsHtml(port) {
    const origin = esc(`http://localhost:${port}`);
    return `
      <details class="ra-steps" id="raSteps">
        <summary>How to connect your domain with Cloudflare (step by step)</summary>
        <ol>
          <li>Your domain must use Cloudflare DNS: in the Cloudflare dashboard choose <b>Add a domain</b> and
              change the nameservers at your registrar to the two Cloudflare shows. Wait until the domain is <b>Active</b>.</li>
          <li>Open <b>Zero Trust</b> (one.dash.cloudflare.com) → <b>Networks</b> → <b>Tunnels</b> →
              <b>Create a tunnel</b>. Choose <b>Cloudflared</b>, name it after this store, and save.</li>
          <li>On the <b>Install and run connector</b> page, copy the command shown for any operating system.
              Do <b>not</b> run it. Paste it (or just the long value starting with <code>eyJ</code>) into
              <b>Tunnel token</b> below — this device runs the connector itself.</li>
          <li>Click <b>Next</b> to <b>Route traffic</b> / <b>Public hostnames</b>: pick a subdomain and your domain
              (for example <code>cctv</code> . <code>yourstore.com.au</code>), set <b>Service type</b> to
              <code>HTTP</code> and <b>URL</b> to <code>${origin.replace('http://', '')}</code>, then save.</li>
          <li>Enter the same hostname below, tick <b>Enable remote access</b> and press <b>Save</b>.
              The status turns <b>Connected</b> within a few seconds; then press <b>Verify now</b>.</li>
          <li>Recommended: in Zero Trust → <b>Access</b> → <b>Applications</b>, add a self-hosted application for the
              hostname so Cloudflare asks for an e-mail one-time PIN before the dashboard sign-in.
              (Leave <code>/api/*</code> unprotected there if the phone app must connect through it.)</li>
        </ol>
        <div class="ra-hint">No router port forwarding is needed: the tunnel is an outbound connection, and
          Cloudflare provides the HTTPS certificate for your domain. First-run account setup is refused over
          the internet and only works on the store network.</div>
      </details>`;
  }

  function renderRemote(errorText) {
    const host = $('settings-remote');
    if (!host) return;
    const s = state || {};
    const port = (s.local_origin || '').split(':').pop() || location.port || '8000';
    const proc = s.process || 'stopped';
    const provider = s.provider || 'cloudflare_tunnel';
    const isTunnel = provider === 'cloudflare_tunnel';
    const tokenBadge = s.token_configured
      ? '<span class="badge badge-green" id="raTokenState">configured</span>'
      : '<span class="badge badge-warning" id="raTokenState">not configured</span>';
    const showTokenInput = !s.token_configured || tokenMode === 'replace';
    const verifiedBadge = s.verified
      ? `<span class="badge badge-green" id="raVerified">Verified ${esc(fmtTime(s.verified_at))}</span>`
      : '<span class="badge badge-warning" id="raVerified">Not verified</span>';

    host.innerHTML = `
      <div class="card-title"><span>Remote access — online dashboard</span></div>
      <div class="ra-hint">Publish this dashboard at <b>https://</b> on your own domain. It is online only while
        this is enabled and the hostname is connected. Everything still runs on this machine.</div>

      <div class="ra-statusbar" id="raStatusBar">
        <span class="ra-dot ${statusDotClass(proc)}" id="raDot"></span>
        <span class="ra-status-text" id="raProcess" data-process="${esc(proc)}">${esc(PROCESS_LABEL[proc] || proc)}</span>
        ${s.enabled && s.public_url ? `<a class="ra-link" id="raPublicUrl" href="${esc(s.public_url)}" target="_blank" rel="noopener">${esc(s.public_url)}</a>` : ''}
        <span class="ra-spacer"></span>
        ${verifiedBadge}
        <button type="button" class="btn btn-sm" id="raVerifyBtn" ${s.hostname ? '' : 'disabled'}>Verify now</button>
      </div>
      <div class="form-status form-status-error" id="raProcessError">${esc(s.last_error || '')}</div>
      <div class="form-status ${s.verified ? '' : 'form-status-error'}" id="raVerifyStatus">${s.verified ? '' : esc(s.verify_error || '')}</div>
      ${s.auth_disabled ? '<div class="form-status form-status-error">AUTH_DISABLED is on: remote access cannot be enabled until authentication is turned back on.</div>' : ''}
      ${isTunnel && s.cloudflared_found === false ? '<div class="form-status form-status-error" id="raNoBinary">cloudflared is not installed on this machine. Run <code>./run.sh --with-tunnel</code> once (downloads it into bin/), then Save again.</div>' : ''}

      <form id="raForm" autocomplete="off">
        <label class="checkbox-label ra-toggle">
          <input type="checkbox" id="raEnabled" ${s.enabled ? 'checked' : ''}> Enable remote access
        </label>
        <div class="form-grid-2col ra-fields">
          <div class="form-group">
            <label for="raProvider">Provider</label>
            <select class="form-select" id="raProvider">
              <option value="cloudflare_tunnel" ${isTunnel ? 'selected' : ''}>Cloudflare Tunnel (recommended)</option>
              <option value="direct" ${!isTunnel ? 'selected' : ''}>Direct (own public IP + reverse proxy)</option>
            </select>
          </div>
          <div class="form-group">
            <label for="raHostname">Public hostname</label>
            <input class="form-input" id="raHostname" value="${esc(s.hostname || '')}" placeholder="cctv.yourstore.com.au"
                   spellcheck="false" autocapitalize="off">
          </div>
        </div>

        <div class="form-group ra-token" id="raTokenGroup" ${isTunnel ? '' : 'hidden'}>
          <label for="raToken">Tunnel token ${tokenBadge}</label>
          ${showTokenInput ? `
            <input class="form-input" id="raToken" type="password" autocomplete="new-password" spellcheck="false"
                   placeholder="${s.token_configured ? 'Paste the new token' : 'Paste the token or the whole install command'}">
            ${tokenMode === 'replace' ? '<button type="button" class="btn btn-sm ra-mt" id="raTokenCancel">Keep current token</button>' : ''}`
          : tokenMode === 'confirm-remove' ? `
            <div class="ra-inline ra-confirm" id="raRemoveConfirm">
              <span>Remove the stored token? The tunnel stops.</span>
              <button type="button" class="btn btn-danger btn-sm" id="raTokenRemoveYes">Remove token</button>
              <button type="button" class="btn btn-sm" id="raTokenRemoveNo">Cancel</button>
            </div>`
          : `
            <div class="ra-inline">
              <span class="ra-hint">Stored encrypted on this device. It is never shown again.</span>
              <button type="button" class="btn btn-sm" id="raTokenReplace">Replace</button>
              <button type="button" class="btn btn-danger btn-sm" id="raTokenRemove">Remove</button>
            </div>`}
        </div>

        ${!isTunnel ? `
          <div class="form-group" id="raCaddy">
            <label>Caddy site block (run Caddy yourself; forward TCP 80 and 443 to this machine)</label>
            <pre class="ra-pre" id="raCaddyBlock">${esc(s.caddy_site_block || '')}</pre>
          </div>` : ''}

        <div class="ra-inline ra-mt">
          <button type="submit" class="btn btn-primary btn-sm" id="raSave">Save</button>
          <span class="ra-hint">Changes apply when you press Save.</span>
        </div>
        <div class="form-status ${errorText ? 'form-status-error' : ''}" id="raFormStatus">${esc(errorText || '')}</div>
      </form>
      ${isTunnel ? stepsHtml(port) : ''}`;

    bindRemote();
  }

  function bindRemote() {
    const form = $('raForm');
    if (!form) return;
    form.addEventListener('submit', onSave);
    form.addEventListener('input', () => { dirty = true; });
    $('raProvider').addEventListener('change', () => {
      dirty = true;
      const keep = snapshotForm();
      state = Object.assign({}, state, { provider: $('raProvider').value,
        caddy_site_block: state && state.caddy_site_block });
      renderRemote();
      restoreForm(keep);
      if ($('raProvider').value === 'direct' && !state.caddy_site_block) {
        setStatus('raFormStatus', 'Save to generate the Caddy site block for your hostname.');
      }
    });
    const on = (id, fn) => { const el = $(id); if (el) el.addEventListener('click', fn); };
    on('raVerifyBtn', onVerify);
    on('raTokenReplace', () => { const k = snapshotForm(); tokenMode = 'replace'; renderRemote(); restoreForm(k); const t = $('raToken'); if (t) t.focus(); });
    on('raTokenCancel', () => { const k = snapshotForm(); tokenMode = 'idle'; renderRemote(); restoreForm(k); });
    on('raTokenRemove', () => { const k = snapshotForm(); tokenMode = 'confirm-remove'; renderRemote(); restoreForm(k); });
    on('raTokenRemoveNo', () => { const k = snapshotForm(); tokenMode = 'idle'; renderRemote(); restoreForm(k); });
    on('raTokenRemoveYes', onRemoveToken);
  }

  function snapshotForm() {
    return {
      enabled: $('raEnabled') ? $('raEnabled').checked : undefined,
      provider: $('raProvider') ? $('raProvider').value : undefined,
      hostname: $('raHostname') ? $('raHostname').value : undefined,
      token: $('raToken') ? $('raToken').value : '',
      stepsOpen: $('raSteps') ? $('raSteps').open : false,
    };
  }

  function restoreForm(k) {
    if (!k) return;
    if ($('raEnabled') && k.enabled !== undefined) $('raEnabled').checked = k.enabled;
    if ($('raProvider') && k.provider) $('raProvider').value = k.provider;
    if ($('raHostname') && k.hostname !== undefined) $('raHostname').value = k.hostname;
    if ($('raToken') && k.token) $('raToken').value = k.token;
    if ($('raSteps')) $('raSteps').open = !!k.stepsOpen;
  }

  async function putRemote(body, statusId, okText) {
    setStatus(statusId, 'Saving…');
    const res = await fetch('/api/v1/remote-access', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(await apiError(res, 'Could not save remote access settings'));
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
    const body = { enabled: f.enabled, provider: f.provider, hostname: (f.hostname || '').trim() };
    if (f.token && f.token.trim()) body.token = f.token.trim();
    btn.disabled = true;
    try {
      await putRemote(body, 'raFormStatus',
        body.enabled ? 'Saved. Remote access is enabled.' : 'Saved. Remote access is disabled.');
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
        'Token removed. Remote access is disabled.');
    } catch (e) {
      tokenMode = 'idle';
      renderRemote(e.message);
      restoreForm(keep);
    }
  }

  async function onVerify() {
    const btn = $('raVerifyBtn');
    btn.disabled = true;
    setStatus('raVerifyStatus', 'Checking that the public hostname reaches this device…');
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
    const structural = !prev.provider || prev.provider !== s.provider || prev.token_configured !== s.token_configured
      || prev.enabled !== s.enabled || prev.hostname !== s.hostname || prev.verified !== s.verified
      || prev.cloudflared_found !== s.cloudflared_found || prev.caddy_site_block !== s.caddy_site_block;
    if (structural && !dirty && tokenMode === 'idle') {
      const keep = { stepsOpen: $('raSteps') ? $('raSteps').open : false };
      renderRemote();
      restoreForm(keep);
      return;
    }
    const dot = $('raDot');
    if (dot) dot.className = `ra-dot ${statusDotClass(s.process)}`;
    const p = $('raProcess');
    if (p) { p.textContent = PROCESS_LABEL[s.process] || s.process; p.dataset.process = s.process; }
    const err = $('raProcessError');
    if (err) err.textContent = s.last_error || '';
  }

  async function loadRemote(initial) {
    try {
      const res = await fetch('/api/v1/remote-access', { cache: 'no-store' });
      if (res.status === 401) { stopPoll(); return; }
      if (!res.ok) throw new Error(await apiError(res, 'Remote access status unavailable'));
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
  document.addEventListener('DOMContentLoaded', () => { if (settingsVisible()) openSettings(); });
  if (document.readyState !== 'loading' && settingsVisible()) openSettings();

  window.edgeRemoteAccess = { reload: openSettings };
})();
