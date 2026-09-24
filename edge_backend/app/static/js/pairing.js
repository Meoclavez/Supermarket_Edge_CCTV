// Settings: mobile pairing, push notifications and paired phones. Owned by the pairing work.
//
// #settings-pairing  "Pair a phone": one-time code + QR (server-rendered SVG),
//                    countdown, regenerate / cancel, manual instructions.
//                    POST/DELETE /api/v1/pairing/sessions
// #settings-push     FCM HTTP v1 service account (write-only), test push,
//                    recent deliveries.  /api/v1/push/*
// #settings-devices  paired phones: rename, alert prefs, revoke.
//                    GET/PATCH/DELETE /api/v1/pairing/devices
//
// The service-account key is never shown again after upload; the server only
// says whether one is configured. No prompt()/confirm()/alert(): every
// confirmation and error is inline. Data loads on the edge:tab settings event.
(function () {
  'use strict';

  const POLL_MS = 3000;
  const $ = (id) => document.getElementById(id);

  let sessionInfo = null;     // last POST /pairing/sessions response
  let sessionStartedAt = 0;
  let countdownTimer = null;
  let pollTimer = null;
  let devices = [];           // non-revoked paired phones
  let eventTypes = [];
  let severities = ['INFO', 'WARNING', 'HIGH'];
  let cameras = [];           // [{id, name}]
  let pushCfg = null;
  let deliveries = [];
  let pushMode = 'idle';      // idle | upload | confirm-remove
  let testDeviceId = null;
  let knownIds = new Set();
  let justPaired = null;
  const ui = { renaming: null, prefsOpen: null, revoking: null };

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

  async function api(url, opts, fallback) {
    const res = await fetch(url, Object.assign({ cache: 'no-store' }, opts || {}));
    if (!res.ok) throw new Error(await apiError(res, fallback || 'Request failed'));
    return res.status === 204 ? null : res.json();
  }

  function jsonOpts(method, body) {
    return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  }

  function fmtTime(iso) {
    if (!iso) return '—';
    const t = new Date(iso);
    return Number.isNaN(t.getTime()) ? String(iso) : t.toLocaleString();
  }

  function fmtAgo(iso) {
    if (!iso) return 'never';
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) return String(iso);
    const s = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (s < 60) return 'just now';
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return fmtTime(iso);
  }

  function setStatus(id, text, isError) {
    const el = $(id);
    if (!el) return;
    el.textContent = text || '';
    el.classList.toggle('form-status-error', !!isError);
  }

  function label(t) { return String(t || '').replace(/_/g, ' ').toLowerCase().replace(/^\w/, (c) => c.toUpperCase()); }

  function svgDataUri(svg) {
    // btoa needs Latin-1; the SVG from segno is ASCII, but be safe.
    return 'data:image/svg+xml;base64,' + btoa(unescape(encodeURIComponent(svg)));
  }

  // ================================================================ pairing

  function secondsLeft() {
    if (!sessionInfo) return 0;
    // Relative to when the code was received, so a skewed browser clock
    // cannot show a wrong countdown.
    const exp = sessionStartedAt + (Number(sessionInfo.expires_in) || 600) * 1000;
    return Math.max(0, Math.round((exp - Date.now()) / 1000));
  }

  function renderPairing(errorText) {
    const host = $('settings-pairing');
    if (!host) return;
    const s = sessionInfo;
    const left = secondsLeft();
    const active = s && left > 0;
    let body;
    if (active) {
      const urls = (s.urls || []).map((u) => `<li><code class="pr-code-inline">${esc(u)}</code></li>`).join('')
        || '<li class="pr-hint">No network address found. Check this server\'s network connection.</li>';
      body = `
        <div class="pr-pair-grid">
          <div class="pr-qr-box">
            <img class="pr-qr" id="prQr" alt="Pairing QR code" src="${svgDataUri(s.qr_svg)}">
          </div>
          <div class="pr-pair-info">
            <div class="pr-field-label">Pairing code</div>
            <div class="pr-code" id="prCode">${esc(s.code)}</div>
            <div class="pr-countdown" id="prCountdown">Expires in ${fmtLeft(left)}</div>
            <div class="pr-field-label">Device ID</div>
            <code class="pr-code-inline" id="prDeviceId">${esc(s.device_id)}</code>
            <div class="pr-field-label">Device name</div>
            <div class="pr-value">${esc(s.device_name)}</div>
            <div class="pr-field-label">Server address</div>
            <ul class="pr-url-list">${urls}</ul>
            <div class="pr-actions">
              <button type="button" class="btn btn-sm" id="prRegenBtn">New code</button>
              <button type="button" class="btn btn-sm" id="prCancelBtn">Cancel</button>
            </div>
          </div>
        </div>`;
    } else {
      const expired = s && left <= 0;
      body = `
        <div class="pr-hint">${expired ? 'The last code expired. ' : ''}Create a one-time code, then scan the QR code
          with the Edge CCTV app. The code works once and expires after 10 minutes.</div>
        <div class="pr-actions">
          <button type="button" class="btn btn-primary btn-sm" id="prCreateBtn">${expired ? 'New code' : 'Pair a phone'}</button>
        </div>`;
    }
    const paired = justPaired
      ? `<div class="pr-success" id="prPairedMsg">Paired: ${esc(justPaired.name)} (${esc(justPaired.platform)}). It is listed under Paired phones.</div>`
      : '';
    host.innerHTML = `
      <div class="card-title"><span>Pair a phone</span></div>
      ${paired}
      ${body}
      <details class="pr-steps" id="prManual">
        <summary>No camera on the phone? Pair manually</summary>
        <ol>
          <li><b>With the code:</b> in the app choose <i>Enter manually</i> and type the server address
            ${active ? `(e.g. <code>${esc((s.urls || [])[0] || '')}</code>)` : 'shown above once a code is created'},
            the Device ID and the pairing code.</li>
          <li><b>With a password:</b> in the app choose <i>Sign in</i>, enter the server address and an operator
            username and password. The phone is registered the same way and appears under Paired phones.</li>
          <li>The Device ID stops a phone from pairing with a different Edge CCTV box on the same network by mistake.</li>
        </ol>
      </details>
      <div class="form-status ${errorText ? 'form-status-error' : ''}" id="prPairStatus">${esc(errorText || '')}</div>`;

    const bind = (id, fn) => { const el = $(id); if (el) el.addEventListener('click', fn); };
    bind('prCreateBtn', createSession);
    bind('prRegenBtn', createSession);
    bind('prCancelBtn', cancelSession);
  }

  function fmtLeft(sec) {
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return `${m}:${String(s).padStart(2, '0')}`;
  }

  function tickCountdown() {
    stopCountdown();
    countdownTimer = setInterval(() => {
      const left = secondsLeft();
      const el = $('prCountdown');
      if (left <= 0) {
        stopCountdown();
        renderPairing();
        return;
      }
      if (el) el.textContent = `Expires in ${fmtLeft(left)}`;
    }, 1000);
  }

  function stopCountdown() { if (countdownTimer) { clearInterval(countdownTimer); countdownTimer = null; } }

  async function createSession() {
    setStatus('prPairStatus', 'Creating code…');
    ['prCreateBtn', 'prRegenBtn'].forEach((id) => { const b = $(id); if (b) b.disabled = true; });
    try {
      sessionInfo = await api('/api/v1/pairing/sessions', { method: 'POST' }, 'Could not create a pairing code');
      sessionStartedAt = Date.now();
      justPaired = null;
      knownIds = new Set(devices.map((d) => d.id));
      renderPairing();
      tickCountdown();
      schedulePoll();
    } catch (e) {
      renderPairing(e.message);
    }
  }

  async function cancelSession() {
    const s = sessionInfo;
    if (!s) return;
    try {
      await api(`/api/v1/pairing/sessions/${encodeURIComponent(s.session_id)}`, { method: 'DELETE' }, 'Could not cancel');
      sessionInfo = null;
      stopCountdown();
      renderPairing();
      setStatus('prPairStatus', 'Pairing code cancelled.');
    } catch (e) {
      setStatus('prPairStatus', e.message, true);
    }
  }

  // ================================================================ push

  function renderPush(errorText) {
    const host = $('settings-push');
    if (!host) return;
    const c = pushCfg;
    const configured = !!(c && c.configured);
    const badge = !c ? '<span class="badge">…</span>'
      : configured ? '<span class="badge badge-green" id="prPushBadge">Configured</span>'
        : '<span class="badge badge-warning" id="prPushBadge">Not configured</span>';
    const details = configured
      ? `<div class="pr-kv"><span>Firebase project</span><code class="pr-code-inline">${esc(c.project_id)}</code></div>
         <div class="pr-kv"><span>Service account</span><code class="pr-code-inline">${esc(c.client_email)}</code></div>`
      : (c && c.stored
        ? '<div class="pr-hint pr-warn-text">A service account is stored but cannot be used (unreadable on this machine). Upload it again.</div>'
        : '<div class="pr-hint">Without a Firebase service account no push is sent; alerts still appear on this dashboard and in the app when it is open.</div>');

    let actions = '';
    if (pushMode === 'upload' || (!configured && pushMode !== 'confirm-remove')) {
      actions = `
        <div class="pr-upload">
          <label class="pr-field-label" for="prSaFile">Service-account key (.json from Firebase)</label>
          <input type="file" id="prSaFile" class="form-input" accept=".json,application/json">
          <div class="pr-actions">
            <button type="button" class="btn btn-primary btn-sm" id="prSaUpload">Upload</button>
            ${configured ? '<button type="button" class="btn btn-sm" id="prSaCancel">Cancel</button>' : ''}
          </div>
        </div>`;
    } else if (pushMode === 'confirm-remove') {
      actions = `
        <div class="pr-confirm" id="prSaConfirm">Remove the service account? Phones stop receiving pushes until a new one is uploaded.
          <div class="pr-actions">
            <button type="button" class="btn btn-danger btn-sm" id="prSaRemoveYes">Yes, remove</button>
            <button type="button" class="btn btn-sm" id="prSaRemoveNo">Keep</button>
          </div>
        </div>`;
    } else {
      actions = `
        <div class="pr-actions">
          <button type="button" class="btn btn-sm" id="prSaReplace">Replace</button>
          <button type="button" class="btn btn-danger btn-sm" id="prSaRemove">Remove</button>
        </div>`;
    }

    const opts = devices.map((d) => `<option value="${esc(d.id)}" ${d.id === testDeviceId ? 'selected' : ''}>${esc(d.name)} (${esc(d.platform)}${d.push.token_registered ? '' : ', no push token'})</option>`).join('');
    const test = `
      <div class="pr-section-title">Send test alert</div>
      <div class="pr-inline">
        <select class="form-select pr-grow" id="prTestDevice" ${devices.length ? '' : 'disabled'}>
          ${opts || '<option value="">No paired phones</option>'}
        </select>
        <button type="button" class="btn btn-sm" id="prTestBtn" ${devices.length ? '' : 'disabled'}>Send test alert</button>
      </div>
      <div class="form-status" id="prTestStatus"></div>`;

    host.innerHTML = `
      <div class="card-title"><span>Push notifications</span>${badge}</div>
      ${details}
      ${actions}
      <div class="form-status ${errorText ? 'form-status-error' : ''}" id="prPushStatus">${esc(errorText || '')}</div>
      ${test}
      <div class="pr-section-title">Recent deliveries</div>
      <div id="prDeliveries">${renderDeliveries()}</div>
      <details class="pr-steps">
        <summary>How to get a service-account key</summary>
        <ol>
          <li>Create (or open) a project at <code>console.firebase.google.com</code> and add the Android and iOS apps.</li>
          <li>Project settings &gt; Service accounts &gt; <i>Generate new private key</i>. Upload that JSON file here.</li>
          <li>For iPhones, upload your APNs key under Project settings &gt; Cloud Messaging. Apple delivery then goes through Firebase.</li>
        </ol>
      </details>`;

    const bind = (id, fn) => { const el = $(id); if (el) el.addEventListener('click', fn); };
    bind('prSaUpload', uploadServiceAccount);
    bind('prSaCancel', () => { pushMode = 'idle'; renderPush(); });
    bind('prSaReplace', () => { pushMode = 'upload'; renderPush(); });
    bind('prSaRemove', () => { pushMode = 'confirm-remove'; renderPush(); });
    bind('prSaRemoveNo', () => { pushMode = 'idle'; renderPush(); });
    bind('prSaRemoveYes', removeServiceAccount);
    bind('prTestBtn', sendTest);
  }

  function statusBadge(status) {
    const cls = status === 'sent' ? 'badge-green'
      : (status === 'failed' || status === 'unregistered') ? 'badge-danger'
        : status === 'not_configured' || status === 'no_token' ? 'badge-warning' : '';
    return `<span class="badge ${cls} pr-badge">${esc(label(status || 'unknown'))}</span>`;
  }

  function renderDeliveries() {
    if (!deliveries.length) return '<div class="pr-hint" id="prNoDeliveries">No pushes attempted since the server started.</div>';
    const rows = deliveries.map((d) => `
      <tr>
        <td>${esc(fmtTime(d.at))}</td>
        <td>${esc(d.name || d.paired_device_id || '')}</td>
        <td>${d.test ? 'Test' : esc(label(d.event_type))}</td>
        <td>${statusBadge(d.status)}</td>
        <td class="pr-err-cell">${esc(d.error || '')}</td>
      </tr>`).join('');
    return `<div class="pr-table-wrap"><table class="data-table" id="prDeliveryTable">
      <thead><tr><th>Time</th><th>Phone</th><th>Alert</th><th>Result</th><th>Detail</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  }

  function checkServiceAccountText(text) {
    let sa;
    try { sa = JSON.parse(text); } catch (_) { return 'That file is not valid JSON.'; }
    if (!sa || typeof sa !== 'object' || Array.isArray(sa)) return 'That file is not a JSON object.';
    if (sa.type !== 'service_account') {
      return sa.project_info
        ? 'That is google-services.json (an app config file). Upload the service-account key instead.'
        : `"type" must be "service_account" (found ${JSON.stringify(sa.type === undefined ? null : sa.type)}).`;
    }
    const missing = ['project_id', 'client_email', 'private_key'].filter((k) => !sa[k]);
    if (missing.length) return `Missing field(s): ${missing.join(', ')}.`;
    if (!String(sa.client_email).includes('@')) return 'client_email is not an e-mail address.';
    return null;
  }

  async function uploadServiceAccount() {
    const input = $('prSaFile');
    const file = input && input.files && input.files[0];
    if (!file) { setStatus('prPushStatus', 'Choose the service-account .json file first.', true); return; }
    if (file.size > 64 * 1024) { setStatus('prPushStatus', 'That file is too large to be a service-account key.', true); return; }
    let text;
    try { text = await file.text(); } catch (_) { setStatus('prPushStatus', 'Could not read the file.', true); return; }
    const problem = checkServiceAccountText(text);
    if (problem) { setStatus('prPushStatus', problem, true); return; }
    const btn = $('prSaUpload');
    if (btn) btn.disabled = true;
    setStatus('prPushStatus', 'Uploading…');
    try {
      pushCfg = await api('/api/v1/push/fcm-service-account',
        { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: text }, 'Upload failed');
      text = null;
      pushMode = 'idle';
      renderPush();
      setStatus('prPushStatus', 'Service account saved (encrypted on this server).');
    } catch (e) {
      if (btn) btn.disabled = false;
      setStatus('prPushStatus', e.message, true);
    }
  }

  async function removeServiceAccount() {
    try {
      pushCfg = await api('/api/v1/push/fcm-service-account', { method: 'DELETE' }, 'Remove failed');
      pushMode = 'idle';
      renderPush();
      setStatus('prPushStatus', 'Service account removed. Pushes are off.');
    } catch (e) {
      setStatus('prPushStatus', e.message, true);
    }
  }

  async function sendTest() {
    const sel = $('prTestDevice');
    const id = sel && sel.value;
    if (!id) return;
    testDeviceId = id;
    const btn = $('prTestBtn');
    if (btn) btn.disabled = true;
    setStatus('prTestStatus', 'Sending…');
    try {
      const r = await api('/api/v1/push/test', jsonOpts('POST', { paired_device_id: id }), 'Test failed');
      const msg = r.status === 'sent' ? 'Accepted by Firebase for delivery.'
        : r.status === 'not_configured' ? 'Not sent: no Firebase service account is configured.'
          : r.status === 'no_token' ? 'Not sent: that phone has not registered for notifications yet.'
            : r.status === 'unregistered' ? 'Firebase says that phone\'s token is no longer valid; it was cleared. Open the app on the phone to register again.'
              : `Failed: ${r.error || r.status}`;
      setStatus('prTestStatus', msg, r.status !== 'sent');
      await Promise.all([loadDeliveries(), loadDevices(true)]);
      const st = $('prTestStatus');
      if (!st || !st.textContent) setStatus('prTestStatus', msg, r.status !== 'sent');
    } catch (e) {
      setStatus('prTestStatus', e.message, true);
    } finally {
      const b = $('prTestBtn');
      if (b) b.disabled = !devices.length;
    }
  }

  // ================================================================ devices

  function pushSummary(d) {
    if (!d.push.token_registered) return '<span class="badge badge-warning pr-badge">No push token</span>';
    if (!d.push.last_status) return '<span class="badge pr-badge">Registered</span>';
    return `${statusBadge(d.push.last_status)}<div class="pr-sub">${esc(fmtAgo(d.push.last_at))}${d.push.last_error ? ' · ' + esc(d.push.last_error) : ''}</div>`;
  }

  function renderDevices(errorText) {
    const host = $('settings-devices');
    if (!host) return;
    const rows = devices.map((d) => renderDeviceRow(d)).join('');
    host.innerHTML = `
      <div class="card-title"><span>Paired phones</span><span class="badge" id="prDeviceCount">${devices.length}</span></div>
      ${devices.length ? `<div class="pr-device-list" id="prDeviceList">${rows}</div>`
        : '<div class="pr-hint" id="prNoDevices">No phones paired yet. Use “Pair a phone” above.</div>'}
      <div class="form-status ${errorText ? 'form-status-error' : ''}" id="prDevicesStatus">${esc(errorText || '')}</div>`;
    devices.forEach(bindDeviceRow);
  }

  function renderDeviceRow(d) {
    const id = esc(d.id);
    const renaming = ui.renaming === d.id;
    const revoking = ui.revoking === d.id;
    const nameCell = renaming
      ? `<form class="pr-inline" data-rename="${id}" autocomplete="off">
           <input class="form-input pr-grow" name="name" maxlength="128" value="${esc(d.name)}" aria-label="Phone name">
           <button type="submit" class="btn btn-primary btn-sm">Save</button>
           <button type="button" class="btn btn-sm" data-act="rename-cancel">Cancel</button>
         </form>`
      : `<div class="pr-device-name">${esc(d.name)}</div>`;
    const confirm = revoking
      ? `<div class="pr-confirm">Revoke ${esc(d.name)}? It is signed out immediately and stops receiving alerts. It must be paired again to reconnect.
           <div class="pr-actions">
             <button type="button" class="btn btn-danger btn-sm" data-act="revoke-yes">Yes, revoke</button>
             <button type="button" class="btn btn-sm" data-act="revoke-no">Keep</button>
           </div>
         </div>` : '';
    return `
      <div class="pr-device" data-id="${id}">
        <div class="pr-device-head">
          <div class="pr-device-main">
            ${nameCell}
            <div class="pr-sub">${esc(d.platform === 'ios' ? 'iPhone' : 'Android')} · paired ${esc(fmtTime(d.paired_at))}
              via ${esc(d.paired_via === 'password' ? 'password' : 'code')} · last seen ${esc(fmtAgo(d.last_seen_at))}</div>
          </div>
          <div class="pr-device-push">${pushSummary(d)}</div>
          <div class="pr-actions pr-device-actions">
            ${renaming ? '' : '<button type="button" class="btn btn-sm" data-act="rename">Rename</button>'}
            <button type="button" class="btn btn-sm" data-act="prefs" aria-expanded="${ui.prefsOpen === d.id}">Alert settings</button>
            <button type="button" class="btn btn-danger btn-sm" data-act="revoke">Revoke</button>
          </div>
        </div>
        ${confirm}
        ${ui.prefsOpen === d.id ? renderPrefs(d) : ''}
        <div class="form-status" data-status="${id}"></div>
      </div>`;
  }

  function renderPrefs(d) {
    const p = d.alert_prefs || {};
    const allTypes = p.event_types == null;
    const allCams = p.camera_ids == null;
    const typeBoxes = eventTypes.map((t) => `
      <label class="checkbox-label pr-check"><input type="checkbox" name="et" value="${esc(t)}"
        ${allTypes || (p.event_types || []).includes(t) ? 'checked' : ''} ${allTypes ? 'disabled' : ''}> ${esc(label(t))}</label>`).join('');
    const camBoxes = cameras.length ? cameras.map((c) => `
      <label class="checkbox-label pr-check"><input type="checkbox" name="cam" value="${esc(c.id)}"
        ${allCams || (p.camera_ids || []).includes(c.id) ? 'checked' : ''} ${allCams ? 'disabled' : ''}> ${esc(c.name || c.id)}</label>`).join('')
      : '<span class="pr-hint">No cameras configured.</span>';
    const qh = p.quiet_hours;
    const sevOpts = severities.map((s) => `<option value="${esc(s)}" ${(p.min_severity || 'INFO') === s ? 'selected' : ''}>${esc(label(s))}${s === 'INFO' ? ' (everything)' : s === 'HIGH' ? ' only' : ' and above'}</option>`).join('');
    return `
      <form class="pr-prefs" data-prefs="${esc(d.id)}" autocomplete="off">
        <div class="pr-pref-block">
          <div class="pr-field-label">Alert types</div>
          <label class="checkbox-label"><input type="checkbox" name="et_all" ${allTypes ? 'checked' : ''}> All alert types</label>
          <div class="pr-check-grid">${typeBoxes}</div>
        </div>
        <div class="pr-pref-block">
          <label class="pr-field-label" for="prSev_${esc(d.id)}">Minimum severity</label>
          <select class="form-select" name="sev" id="prSev_${esc(d.id)}">${sevOpts}</select>
        </div>
        <div class="pr-pref-block">
          <div class="pr-field-label">Cameras</div>
          <label class="checkbox-label"><input type="checkbox" name="cam_all" ${allCams ? 'checked' : ''}> All cameras</label>
          <div class="pr-check-grid">${camBoxes}</div>
        </div>
        <div class="pr-pref-block">
          <label class="checkbox-label"><input type="checkbox" name="qh_on" ${qh ? 'checked' : ''}> Quiet hours (no pushes)</label>
          <div class="pr-inline">
            <input type="time" class="form-input pr-time" name="qh_start" value="${esc(qh ? qh.start : '22:00')}" ${qh ? '' : 'disabled'} aria-label="Quiet hours start">
            <span class="pr-sub">to</span>
            <input type="time" class="form-input pr-time" name="qh_end" value="${esc(qh ? qh.end : '07:00')}" ${qh ? '' : 'disabled'} aria-label="Quiet hours end">
            <span class="pr-sub">this server's local time</span>
          </div>
        </div>
        <div class="pr-actions">
          <button type="submit" class="btn btn-primary btn-sm">Save alert settings</button>
          <button type="button" class="btn btn-sm" data-act="prefs-close">Close</button>
        </div>
      </form>`;
  }

  function rowStatus(id, text, isError) {
    const el = document.querySelector(`[data-status="${CSS.escape(id)}"]`);
    if (!el) return;
    el.textContent = text || '';
    el.classList.toggle('form-status-error', !!isError);
  }

  function bindDeviceRow(d) {
    const row = document.querySelector(`.pr-device[data-id="${CSS.escape(d.id)}"]`);
    if (!row) return;
    row.addEventListener('click', (ev) => {
      const btn = ev.target.closest('[data-act]');
      if (!btn || !row.contains(btn)) return;
      const act = btn.getAttribute('data-act');
      if (act === 'rename') { ui.renaming = d.id; renderDevices(); focusRename(d.id); }
      else if (act === 'rename-cancel') { ui.renaming = null; renderDevices(); }
      else if (act === 'prefs') { ui.prefsOpen = ui.prefsOpen === d.id ? null : d.id; renderDevices(); }
      else if (act === 'prefs-close') { ui.prefsOpen = null; renderDevices(); }
      else if (act === 'revoke') { ui.revoking = d.id; renderDevices(); }
      else if (act === 'revoke-no') { ui.revoking = null; renderDevices(); }
      else if (act === 'revoke-yes') { revoke(d); }
    });
    const rf = row.querySelector('form[data-rename]');
    if (rf) rf.addEventListener('submit', (ev) => { ev.preventDefault(); rename(d, rf.elements.name.value); });
    const pf = row.querySelector('form[data-prefs]');
    if (pf) {
      pf.addEventListener('submit', (ev) => { ev.preventDefault(); savePrefs(d, pf); });
      pf.addEventListener('change', (ev) => {
        const n = ev.target.name;
        if (n === 'et_all') pf.querySelectorAll('input[name="et"]').forEach((i) => { i.disabled = ev.target.checked; if (ev.target.checked) i.checked = true; });
        if (n === 'cam_all') pf.querySelectorAll('input[name="cam"]').forEach((i) => { i.disabled = ev.target.checked; if (ev.target.checked) i.checked = true; });
        if (n === 'qh_on') ['qh_start', 'qh_end'].forEach((k) => { pf.elements[k].disabled = !ev.target.checked; });
      });
    }
  }

  function focusRename(id) {
    const f = document.querySelector(`form[data-rename="${CSS.escape(id)}"]`);
    if (f) { f.elements.name.focus(); f.elements.name.select(); }
  }

  async function rename(d, name) {
    const clean = String(name || '').trim();
    if (!clean) { rowStatus(d.id, 'Name cannot be empty.', true); return; }
    try {
      const updated = await api(`/api/v1/pairing/devices/${encodeURIComponent(d.id)}`,
        jsonOpts('PATCH', { name: clean }), 'Rename failed');
      replaceDevice(updated);
      ui.renaming = null;
      renderDevices();
      rowStatus(d.id, 'Renamed.');
      renderPush();
    } catch (e) {
      rowStatus(d.id, e.message, true);
    }
  }

  async function savePrefs(d, form) {
    const q = (sel) => Array.from(form.querySelectorAll(sel));
    const prefs = {
      event_types: form.elements.et_all.checked ? null : q('input[name="et"]:checked').map((i) => i.value),
      min_severity: form.elements.sev.value,
      camera_ids: form.elements.cam_all.checked ? null : q('input[name="cam"]:checked').map((i) => i.value),
      quiet_hours: form.elements.qh_on.checked
        ? { start: form.elements.qh_start.value, end: form.elements.qh_end.value } : null,
    };
    if (prefs.event_types && !prefs.event_types.length) { rowStatus(d.id, 'Select at least one alert type, or “All alert types”.', true); return; }
    if (prefs.camera_ids && !prefs.camera_ids.length) { rowStatus(d.id, 'Select at least one camera, or “All cameras”.', true); return; }
    if (prefs.quiet_hours && (!prefs.quiet_hours.start || !prefs.quiet_hours.end)) { rowStatus(d.id, 'Enter both quiet-hours times.', true); return; }
    try {
      const updated = await api(`/api/v1/pairing/devices/${encodeURIComponent(d.id)}`,
        jsonOpts('PATCH', { alert_prefs: prefs }), 'Saving alert settings failed');
      replaceDevice(updated);
      renderDevices();
      rowStatus(d.id, 'Alert settings saved.');
    } catch (e) {
      rowStatus(d.id, e.message, true);
    }
  }

  async function revoke(d) {
    try {
      await api(`/api/v1/pairing/devices/${encodeURIComponent(d.id)}`, { method: 'DELETE' }, 'Revoke failed');
      devices = devices.filter((x) => x.id !== d.id);
      knownIds.delete(d.id);
      ui.revoking = null;
      if (ui.prefsOpen === d.id) ui.prefsOpen = null;
      renderDevices();
      setStatus('prDevicesStatus', `${d.name} revoked.`);
      renderPush();
    } catch (e) {
      rowStatus(d.id, e.message, true);
    }
  }

  function replaceDevice(updated) {
    devices = devices.map((x) => (x.id === updated.id ? updated : x));
  }

  function editing() { return !!(ui.renaming || ui.prefsOpen || ui.revoking); }

  // ================================================================ loading

  async function loadDevices(force) {
    try {
      const data = await api('/api/v1/pairing/devices', null, 'Paired phones unavailable');
      const next = data.devices || [];
      if (data.event_types) eventTypes = data.event_types;
      if (data.severities) severities = data.severities;
      // A phone that appeared while a code was showing just paired with it.
      if (sessionInfo && sessionStartedAt) {
        const fresh = next.find((d) => !knownIds.has(d.id));
        if (fresh) {
          justPaired = fresh;
          sessionInfo = null;
          stopCountdown();
          renderPairing();
        }
      }
      const changed = force || next.length !== devices.length || next.some((d, i) => !devices[i] || devices[i].id !== d.id);
      devices = next;
      next.forEach((d) => knownIds.add(d.id));
      if (changed && !editing()) { renderDevices(); renderPush(); }
      else if (!editing()) {
        // Refresh "last seen"/push columns without disturbing anything.
        next.forEach((d) => {
          const row = document.querySelector(`.pr-device[data-id="${CSS.escape(d.id)}"] .pr-device-push`);
          if (row) row.innerHTML = pushSummary(d);
        });
      }
    } catch (e) {
      renderDevices(e.message);
    }
  }

  async function loadCameras() {
    try {
      const data = await api('/api/v1/cameras', null, 'Cameras unavailable');
      const list = Array.isArray(data) ? data : (data.cameras || []);
      cameras = list.map((c) => ({ id: c.id, name: c.name }));
    } catch (_) {
      cameras = [];
    }
  }

  async function loadPush() {
    try {
      pushCfg = await api('/api/v1/push/config', null, 'Push status unavailable');
      renderPush();
    } catch (e) {
      pushCfg = null;
      renderPush(e.message);
    }
  }

  async function loadDeliveries() {
    try {
      const data = await api('/api/v1/push/deliveries?limit=20', null, 'Deliveries unavailable');
      deliveries = data.deliveries || [];
      const el = $('prDeliveries');
      if (el) el.innerHTML = renderDeliveries();
    } catch (_) { /* keep the last list */ }
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
      await loadDevices(false);
      if (sessionInfo && secondsLeft() > 0) schedulePoll();
      else if (settingsVisible()) { pollTimer = setTimeout(() => { pollTimer = null; if (settingsVisible()) { loadDeliveries(); schedulePoll(); } }, POLL_MS * 3); }
    }, POLL_MS);
  }

  async function openSettings() {
    if (window.edgeAuth && window.edgeAuth.isGateOpen && window.edgeAuth.isGateOpen()) return;
    pushMode = 'idle';
    Object.keys(ui).forEach((k) => { ui[k] = null; });
    renderPairing();
    await loadCameras();
    await Promise.all([loadDevices(true), loadPush(), loadDeliveries()]);
    knownIds = new Set(devices.map((d) => d.id));
    if (sessionInfo && secondsLeft() > 0) tickCountdown();
    schedulePoll();
  }

  window.addEventListener('edge:tab', (ev) => {
    if (ev.detail && ev.detail.tab === 'settings') openSettings();
    else { stopPoll(); stopCountdown(); }
  });
  document.addEventListener('visibilitychange', () => {
    if (settingsVisible() && !pollTimer) schedulePoll();
  });
  const boot = () => { if (settingsVisible()) openSettings(); };
  // Start only after auth.js has checked the stored token (edgeAuth.onReady).
  if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') window.edgeAuth.onReady(boot);
  else if (document.readyState !== 'loading') boot();
  else document.addEventListener('DOMContentLoaded', boot);

  window.edgePairing = { reload: openSettings };
})();
