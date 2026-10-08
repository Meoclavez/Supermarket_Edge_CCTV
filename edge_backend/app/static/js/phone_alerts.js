// Installed web app + phone alerts (Web Push). Server side: routes/push.py,
// services/push_alerts.py, services/offline_watchdog.py; worker: /sw.js.
//
// Every page:
//   - registers /sw.js (https or localhost only: browsers allow push nowhere else);
//   - header "Install app": Chrome/Edge/Android use the browser's own install
//     prompt; iPhone/iPad get the Share -> Add to Home Screen steps (Safari has
//     no install prompt);
//   - a one-time banner in the installed app: "Turn on alerts for this phone";
//   - /dashboard?incident=<id> (a tapped notification) opens that incident;
//   - keeps this phone's subscription bound to whoever is signed in.
// #settings-phone-alerts: this phone, who gets alerts (first priority / backup,
// escalation, alert level, store-offline watchdog) and every connected phone.
// No prompt()/confirm()/alert(): confirmations are inline.
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const BANNER_KEY = 'edge_cctv_alert_banner_dismissed';
  const TIER_LABEL = { first_priority: 'First priority', backup: 'Backup', none: 'No alerts' };
  const ROLE_LABEL = { owner: 'Owner', admin: 'Administrator', operator: 'Operator' };
  const PLATFORM_ICON = { ios: '📱', android: '📱', desktop: '💻', other: '🔔' };

  let installEvent = null;      // Chrome's deferred beforeinstallprompt
  let roster = null;
  let rosterError = '';
  let draft = null;             // unsaved roster edits
  let devices = [];
  let deliveries = [];
  let config = null;
  let phoneState = { supported: false, permission: 'default', subscribed: false, endpoint: null };
  let phoneMsg = { text: '', error: false };
  let rosterMsg = { text: '', error: false };
  let deviceMsg = { id: null, text: '', error: false };
  let confirmRemove = null;
  let showIosSteps = false;
  let syncedFor = null;         // account this browser's subscription was last re-bound to

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

  function me() { return window.EdgeAuth || { role: null }; }

  function fmtTime(iso) {
    if (!iso) return 'never';
    const t = new Date(/Z$|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`);
    return Number.isNaN(t.getTime()) ? String(iso) : t.toLocaleString();
  }

  function status(msg) {
    return `<div class="form-status ${msg.error ? 'form-status-error' : 'us-ok'}" role="status">${esc(msg.text || '')}</div>`;
  }

  // ============================================================ platform

  const ua = navigator.userAgent || '';
  const isIos = /iphone|ipad|ipod/i.test(ua) || (/macintosh/i.test(ua) && navigator.maxTouchPoints > 1);
  const isAndroid = /android/i.test(ua);

  function isStandalone() {
    return (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches)
      || window.navigator.standalone === true;
  }

  function platform() { return isIos ? 'ios' : isAndroid ? 'android' : 'desktop'; }

  function pushSupported() {
    return window.isSecureContext && 'serviceWorker' in navigator && 'PushManager' in window
      && 'Notification' in window;
  }

  function keyBytes(b64url) {
    const s = String(b64url).replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(s + '='.repeat((4 - (s.length % 4)) % 4));
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  }

  function sameKey(buf, b64url) {
    if (!buf) return false;
    const a = new Uint8Array(buf);
    const b = keyBytes(b64url);
    return a.length === b.length && a.every((x, i) => x === b[i]);
  }

  async function registration() {
    if (!('serviceWorker' in navigator) || !window.isSecureContext) return null;
    try {
      return await navigator.serviceWorker.register('/sw.js', { scope: '/' });
    } catch (e) {
      console.warn('Service worker not registered:', e);
      return null;
    }
  }

  async function readPhoneState() {
    phoneState = { supported: pushSupported(), permission: 'Notification' in window ? Notification.permission : 'unsupported',
      subscribed: false, endpoint: null };
    if (!phoneState.supported) return phoneState;
    try {
      const reg = await navigator.serviceWorker.getRegistration('/');
      const sub = reg && await reg.pushManager.getSubscription();
      if (sub) { phoneState.subscribed = true; phoneState.endpoint = sub.endpoint; }
    } catch (_) { /* keep defaults */ }
    return phoneState;
  }

  // Keep this browser's subscription on the box and bound to whoever is signed in.
  async function syncSubscription(label) {
    const reg = await navigator.serviceWorker.ready;
    config = await api('/api/v1/web-push/config', null, 'Could not read the alert settings');
    let sub = await reg.pushManager.getSubscription();
    if (sub && !sameKey(sub.options && sub.options.applicationServerKey, config.vapid_public_key)) {
      await sub.unsubscribe();          // made for an older key of this box
      sub = null;
    }
    if (!sub) {
      sub = await reg.pushManager.subscribe({ userVisibleOnly: true,
        applicationServerKey: keyBytes(config.vapid_public_key) });
    }
    const j = sub.toJSON();
    const body = { endpoint: j.endpoint, keys: j.keys, platform: platform() };
    if (label) body.label = label;
    return api('/api/v1/web-push/subscriptions', jsonOpts('POST', body), 'Could not register this phone');
  }

  async function enableAlerts(label) {
    if (!pushSupported()) throw new Error(notSupportedReason());
    // Must be the first await of the tap: iPhone only asks during a user gesture.
    const perm = await Notification.requestPermission();
    if (perm !== 'granted') {
      throw new Error(perm === 'denied'
        ? 'Notifications are blocked for this app. Allow them in the phone\'s Settings -> Notifications, then tap Enable again.'
        : 'Notifications were not allowed.');
    }
    await registration();
    const r = await syncSubscription(label);
    await readPhoneState();
    return r;
  }

  async function disableAlerts() {
    const reg = await navigator.serviceWorker.getRegistration('/');
    const sub = reg && await reg.pushManager.getSubscription();
    if (!sub) return;
    const endpoint = sub.endpoint;
    await sub.unsubscribe();
    await api('/api/v1/web-push/subscriptions/unsubscribe', jsonOpts('POST', { endpoint }),
      'Could not remove this phone from the box');
    await readPhoneState();
  }

  function notSupportedReason() {
    if (!window.isSecureContext) {
      return 'Phone alerts need the secure public address (https://…). This page was opened on a private '
        + 'http address, where browsers do not allow notifications.';
    }
    if (isIos && !isStandalone()) {
      return 'On iPhone and iPad, alerts work only in the installed app: tap Share, then Add to Home Screen, '
        + 'open the app from the Home Screen and enable alerts there.';
    }
    return 'This browser does not support web notifications. Use Chrome on Android or Safari on iPhone (iOS 16.4 or later).';
  }

  // ============================================================ install button + banner

  function mountInstallButton() {
    const actions = document.querySelector('.header-actions');
    if (!actions || $('paInstallBtn')) return;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.id = 'paInstallBtn';
    btn.className = 'btn btn-sm pa-install-btn';
    btn.hidden = true;
    btn.title = 'Install the dashboard as an app on this device, for alerts on your phone';
    btn.innerHTML = '<span aria-hidden="true">📲</span><span class="pa-install-label">Install app</span>';
    btn.addEventListener('click', onInstallClick);
    actions.insertBefore(btn, actions.firstChild);
    updateInstallButton();
  }

  function updateInstallButton() {
    const btn = $('paInstallBtn');
    if (!btn) return;
    btn.hidden = isStandalone() || !window.isSecureContext || !(installEvent || isIos);
  }

  async function onInstallClick() {
    if (installEvent) {
      const ev = installEvent;
      installEvent = null;
      ev.prompt();
      try { await ev.userChoice; } catch (_) { /* dismissed */ }
      updateInstallButton();
      return;
    }
    showIosSteps = true;
    if (typeof window.switchTab === 'function') window.switchTab('settings');
    renderCard();
    setTimeout(() => { const c = $('settings-phone-alerts'); if (c) c.scrollIntoView({ block: 'start' }); }, 60);
  }

  function maybeShowBanner() {
    if (!isStandalone() || !pushSupported() || phoneState.subscribed || $('paBanner')) return;
    let dismissed = false;
    try { dismissed = localStorage.getItem(BANNER_KEY) === '1'; } catch (_) { /* storage off */ }
    if (dismissed || !me().known) return;
    const bar = document.createElement('div');
    bar.id = 'paBanner';
    bar.className = 'pa-banner';
    bar.setAttribute('role', 'region');
    bar.setAttribute('aria-label', 'Phone alerts');
    bar.innerHTML = `<span aria-hidden="true">🔔</span>
      <div class="pa-banner-text"><b>Turn on theft alerts for this phone</b>
        <span>Get a notification when the cameras see something that needs a look, even with the app closed.</span>
        <span class="pa-banner-msg" id="paBannerMsg"></span></div>
      <div class="pa-banner-actions">
        <button type="button" class="btn btn-primary btn-sm" id="paBannerEnable">Enable alerts</button>
        <button type="button" class="btn btn-sm" id="paBannerLater">Not now</button></div>`;
    const shell = document.querySelector('.top-nav-header');
    if (shell && shell.parentNode) shell.parentNode.insertBefore(bar, shell.nextSibling);
    $('paBannerEnable').addEventListener('click', async (e) => {
      e.currentTarget.disabled = true;
      try {
        await enableAlerts();
        bar.remove();
        toast('Alerts are on for this phone.');
        renderCard();
      } catch (err) {
        $('paBannerMsg').textContent = err.message;
        e.currentTarget.disabled = false;
      }
    });
    $('paBannerLater').addEventListener('click', () => {
      try { localStorage.setItem(BANNER_KEY, '1'); } catch (_) { /* storage off */ }
      bar.remove();
    });
  }

  function toast(text) {
    const t = $('toast');
    if (!t) return;
    t.textContent = text;
    t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), 3500);
  }

  // ============================================================ settings card

  function phoneSection() {
    const s = phoneState;
    const mine = (config && config.my_subscriptions) || [];
    const here = s.endpoint && mine.find((d) => d.endpoint === s.endpoint);
    let state;
    let actions = '';
    if (!s.supported) {
      state = `<div class="pa-state pa-state-off">${esc(notSupportedReason())}</div>`;
      if (isIos && window.isSecureContext && !isStandalone()) showIosSteps = true;
    } else if (s.permission === 'denied') {
      state = '<div class="pa-state pa-state-off">Notifications are blocked for this app. Allow them in the phone\'s Settings → Notifications (or the browser\'s site settings), then reload.</div>';
    } else if (s.subscribed) {
      state = `<div class="pa-state pa-state-on">✔ Alerts are on for this ${isIos || isAndroid ? 'phone' : 'browser'}${here ? ` (${esc(here.label)})` : ''}.</div>`;
      actions = `<button type="button" class="btn btn-sm" id="paTestMine">Send a test alert</button>
        <button type="button" class="btn btn-sm" id="paDisable">Turn off on this ${isIos || isAndroid ? 'phone' : 'browser'}</button>`;
    } else {
      state = '<div class="pa-state">Alerts are off on this device.</div>';
      actions = `<input id="paLabel" class="form-input pa-label-input" maxlength="80" placeholder="Name this phone (optional), e.g. Sam's iPhone">
        <button type="button" class="btn btn-primary btn-sm" id="paEnable">Enable alerts on this ${isIos || isAndroid ? 'phone' : 'browser'}</button>`;
    }
    const tier = config && config.my_tier;
    const tierLine = me().known
      ? `<div class="pr-hint">You are <b>${esc(TIER_LABEL[tier || 'none'])}</b>${tier ? '' : ' (no alerts unless an administrator adds you, or no roster is saved yet)'}.</div>`
      : '';
    const steps = showIosSteps && isIos && !isStandalone() ? `
      <ol class="pa-steps">
        <li>Open this page in <b>Safari</b> (iOS 16.4 or later).</li>
        <li>Tap <b>Share</b> <span aria-hidden="true">⬆️</span>, then <b>Add to Home Screen</b>, then <b>Add</b>.</li>
        <li>Open <b>the new app icon</b> on your Home Screen and sign in.</li>
        <li>Tap <b>Enable alerts</b> and choose <b>Allow</b>.</li>
      </ol>` : '';
    const androidHint = !isStandalone() && isAndroid && window.isSecureContext
      ? '<div class="pr-hint">Tip: install the app first (📲 Install app at the top, or Chrome menu ⋮ → Install app), so alerts open it straight away.</div>' : '';
    return `<h3 class="pa-h">This ${isIos || isAndroid ? 'phone' : 'device'}</h3>
      ${state}${tierLine}${steps}${androidHint}
      ${actions ? `<div class="pr-actions pa-actions">${actions}</div>` : ''}
      ${status(phoneMsg)}`;
  }

  function rosterSection() {
    if (rosterError) return `<h3 class="pa-h">Who gets alerts</h3><div class="form-status form-status-error">${esc(rosterError)}</div>`;
    if (!roster) return '<h3 class="pa-h">Who gets alerts</h3><div class="pr-hint">Loading…</div>';
    const admin = me().isAdmin;
    const d = draft || roster;
    const dis = admin ? '' : 'disabled';
    const tierOf = (id) => (d.first_priority.includes(id) ? 'first_priority' : d.backup.includes(id) ? 'backup' : 'none');
    const rows = roster.people.map((p) => `
      <tr>
        <td><b>${esc(p.display_name)}</b>${p.username !== p.display_name ? ` <span class="pa-dim">${esc(p.username)}</span>` : ''}
          ${p.is_active ? '' : ' <span class="badge pa-badge-off">switched off</span>'}</td>
        <td>${esc(ROLE_LABEL[p.role] || p.role)}</td>
        <td><select class="form-select pa-tier" data-user="${esc(p.id)}" ${dis} ${p.is_active ? '' : 'disabled'}>
          ${Object.entries(TIER_LABEL).map(([k, v]) => `<option value="${k}" ${tierOf(p.id) === k ? 'selected' : ''}>${v}</option>`).join('')}
        </select></td>
        <td>${p.phones ? `${p.phones} ${p.phones === 1 ? 'phone' : 'phones'}` : '<span class="pa-warn">no phone yet</span>'}</td>
      </tr>`).join('');
    const firstCount = d.first_priority.length;
    const lim = roster.limits || {};
    const wd = roster.watchdog || {};
    const warn = (roster.warnings || []).map((w) => `<div class="pa-warning">⚠ ${esc(w)}</div>`).join('');
    const wdLine = wd.configured
      ? `${wd.result === 'stored' ? `✔ The VPS holds the offline alert for ${esc(wd.stored)} phone(s)` : esc(wd.result || 'not run yet')}${wd.last_ok_at ? `, updated ${esc(fmtTime(wd.last_ok_at))}` : ''}${wd.error ? `. <span class="pa-warn">${esc(wd.error)}</span>` : ''}`
      : `<span class="pa-warn">${esc(wd.problem || 'Not available')}</span>`;
    return `<h3 class="pa-h">Who gets alerts</h3>
      ${warn}
      <div class="pr-hint">An alert goes to every phone of the <b>first-priority</b> people (at least ${roster.min_first_priority}).
        If nobody acknowledges it in time, the <b>backup</b> people get it too. Acknowledge from the notification or on the Loss prevention tab.</div>
      <div class="us-table-wrap"><table class="us-table pa-table">
        <thead><tr><th>Person</th><th>Role</th><th>Alerts</th><th>Phones</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
      <div class="pa-count ${firstCount < roster.min_first_priority ? 'pa-warn' : ''}">${firstCount} first priority · ${d.backup.length} backup</div>
      <div class="pa-grid">
        <div class="fp-field"><label for="paLevel">Alert phones from confidence</label>
          <div class="pa-inline"><input id="paLevel" type="number" class="form-input" min="${Math.round((lim.min_confidence || {}).min * 100 || 30)}"
            max="${Math.round((lim.min_confidence || {}).max * 100 || 99)}" step="1" value="${Math.round(d.min_confidence * 100)}" ${dis}><span>%</span></div>
          <span class="pa-dim">75% = high-confidence incidents only. Lower ones still appear on the dashboard.</span></div>
        <div class="fp-field"><label for="paEscalate">Escalate to backup after</label>
          <div class="pa-inline"><input id="paEscalate" type="number" class="form-input" min="${(lim.escalate_after_min || {}).min || 1}"
            max="${(lim.escalate_after_min || {}).max || 60}" step="1" value="${esc(d.escalate_after_min)}" ${dis}><span>min without acknowledgement</span></div></div>
        <div class="fp-field"><label class="pa-check"><input type="checkbox" id="paNight" ${d.night_intrusion ? 'checked' : ''} ${dis}> Night watch intrusions</label>
          <label class="pa-check"><input type="checkbox" id="paArea" ${d.area_alerts ? 'checked' : ''} ${dis}> Area &amp; line alerts</label></div>
        <div class="fp-field"><label class="pa-check"><input type="checkbox" id="paWatchdog" ${d.watchdog_enabled ? 'checked' : ''} ${dis}> Alert if the store's CCTV goes offline</label>
          <div class="pa-inline"><span>after</span><input id="paWatchdogMin" type="number" class="form-input" min="${(lim.watchdog_offline_min || {}).min || 5}"
            max="${(lim.watchdog_offline_min || {}).max || 240}" step="1" value="${esc(d.watchdog_offline_min)}" ${dis}><span>min</span></div>
          <span class="pa-dim">Sent by the VPS, which notices when the box stops calling in. ${wdLine}</span></div>
      </div>
      ${admin ? `<div class="pr-actions">
          <button type="button" class="btn btn-primary btn-sm" id="paSave" ${draft ? '' : 'disabled'}>Save</button>
          ${draft ? '<button type="button" class="btn btn-sm" id="paDiscard">Discard changes</button>' : ''}
          <button type="button" class="btn btn-sm" id="paWdUpload" title="Send the current phone list to the VPS now">Update VPS now</button>
        </div>` : '<div class="pr-hint">Only an owner or administrator can change who gets alerts.</div>'}
      ${roster.updated_at ? `<div class="pa-dim">Last saved ${esc(fmtTime(roster.updated_at))}${roster.updated_by ? ` by ${esc(roster.updated_by)}` : ''}.</div>` : ''}
      ${status(rosterMsg)}`;
  }

  function devicesSection() {
    const admin = me().isAdmin;
    if (!devices.length) {
      return `<h3 class="pa-h">Connected phones</h3><div class="pr-hint">No phone has alerts turned on yet.</div>`;
    }
    const rows = devices.map((d) => {
      const last = d.last_push_status
        ? `${esc(d.last_push_status)} · ${esc(fmtTime(d.last_push_at))}${d.last_push_error ? `<div class="pa-warn pa-small">${esc(d.last_push_error)}</div>` : ''}`
        : '<span class="pa-dim">none yet</span>';
      const here = phoneState.endpoint && d.endpoint === phoneState.endpoint;
      const confirm = confirmRemove === d.id
        ? `<span class="pa-confirm">Remove? <button type="button" class="btn btn-sm btn-danger" data-act="remove-yes" data-id="${esc(d.id)}">Remove</button>
           <button type="button" class="btn btn-sm" data-act="remove-no">Keep</button></span>`
        : `<button type="button" class="btn btn-sm" data-act="test" data-id="${esc(d.id)}" ${d.usable ? '' : 'disabled'}>Test</button>
           <button type="button" class="btn btn-sm" data-act="remove" data-id="${esc(d.id)}">Remove</button>`;
      const msg = deviceMsg.id === d.id ? `<div class="form-status ${deviceMsg.error ? 'form-status-error' : 'us-ok'}">${esc(deviceMsg.text)}</div>` : '';
      return `<tr>
        ${admin ? `<td>${esc(d.person)}</td>` : ''}
        <td>${PLATFORM_ICON[d.platform] || '🔔'} ${esc(d.label)}${here ? ' <span class="badge us-you">this device</span>' : ''}
          ${d.usable ? '' : '<div class="pa-warn pa-small">Will not get alerts (account off, or set up with an old key: enable again on the phone).</div>'}</td>
        <td>${esc(fmtTime(d.created_at))}</td>
        <td>${last}</td>
        <td class="us-actions">${confirm}${msg}</td></tr>`;
    }).join('');
    return `<h3 class="pa-h">Connected phones</h3>
      <div class="us-table-wrap"><table class="us-table pa-table">
        <thead><tr>${admin ? '<th>Person</th>' : ''}<th>Device</th><th>Added</th><th>Last alert</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table></div>`;
  }

  function deliveriesSection() {
    if (!me().isAdmin || !deliveries.length) return '';
    const rows = deliveries.slice(0, 12).map((x) => `<tr>
      <td>${esc(fmtTime(x.at))}</td><td>${esc(x.kind)}${x.tier === 2 ? ' (backup)' : ''}</td>
      <td>${esc(x.title)}</td><td>${esc(x.label)}</td>
      <td>${x.status === 'sent' ? '✔ sent' : `<span class="pa-warn">${esc(x.status)}</span>`}</td></tr>`).join('');
    return `<details class="pa-details"><summary>Recent phone alerts (${deliveries.length})</summary>
      <div class="us-table-wrap"><table class="us-table pa-table">
        <thead><tr><th>When</th><th>Kind</th><th>Alert</th><th>Phone</th><th>Result</th></tr></thead>
        <tbody>${rows}</tbody></table></div></details>`;
  }

  function renderCard() {
    const host = $('settings-phone-alerts');
    if (!host) return;
    host.innerHTML = `<div class="card-title"><span>Phone alerts</span>
        <span class="badge ${roster && roster.configured ? 'pa-badge-on' : 'pa-badge-off'}">${roster && roster.configured ? 'roster saved' : 'not set up'}</span></div>
      <div class="pa-section">${phoneSection()}</div>
      <div class="pa-section">${rosterSection()}</div>
      <div class="pa-section">${devicesSection()}${deliveriesSection()}</div>`;
    bindCard(host);
  }

  function readDraftFromForm() {
    const base = draft || JSON.parse(JSON.stringify({
      first_priority: roster.first_priority, backup: roster.backup, escalate_after_min: roster.escalate_after_min,
      min_confidence: roster.min_confidence, night_intrusion: roster.night_intrusion, area_alerts: roster.area_alerts,
      watchdog_enabled: roster.watchdog_enabled, watchdog_offline_min: roster.watchdog_offline_min,
    }));
    const first = [];
    const backup = [];
    document.querySelectorAll('#settings-phone-alerts .pa-tier').forEach((sel) => {
      const id = sel.getAttribute('data-user');
      if (sel.value === 'first_priority') first.push(id);
      else if (sel.value === 'backup') backup.push(id);
    });
    // Accounts that cannot be edited here (switched off) keep their tier.
    roster.people.filter((p) => !p.is_active).forEach((p) => {
      if (base.first_priority.includes(p.id)) first.push(p.id);
      if (base.backup.includes(p.id)) backup.push(p.id);
    });
    const num = (id, fallback) => { const v = parseFloat(($(id) || {}).value); return Number.isFinite(v) ? v : fallback; };
    return {
      first_priority: first, backup,
      min_confidence: Math.round(num('paLevel', base.min_confidence * 100)) / 100,
      escalate_after_min: Math.round(num('paEscalate', base.escalate_after_min)),
      night_intrusion: !!($('paNight') || {}).checked,
      area_alerts: !!($('paArea') || {}).checked,
      watchdog_enabled: !!($('paWatchdog') || {}).checked,
      watchdog_offline_min: Math.round(num('paWatchdogMin', base.watchdog_offline_min)),
    };
  }

  function bindCard(host) {
    const on = (id, fn) => { const n = $(id); if (n) n.addEventListener('click', fn); };
    on('paEnable', async (e) => {
      e.currentTarget.disabled = true;
      const label = ($('paLabel') || {}).value || '';
      try {
        await enableAlerts(label.trim());
        phoneMsg = { text: 'Alerts are on. Tap "Send a test alert" to check.', error: false };
        const b = $('paBanner'); if (b) b.remove();
      } catch (err) {
        phoneMsg = { text: err.message, error: true };
      }
      await reload();
    });
    on('paDisable', async () => {
      try {
        await disableAlerts();
        phoneMsg = { text: 'Alerts are off on this device.', error: false };
      } catch (err) {
        phoneMsg = { text: err.message, error: true };
      }
      await reload();
    });
    on('paTestMine', async (e) => {
      e.currentTarget.disabled = true;
      const mine = ((config && config.my_subscriptions) || []).find((d) => d.endpoint === phoneState.endpoint);
      try {
        if (!mine) await syncSubscription();
        await reload();
        const again = ((config && config.my_subscriptions) || []).find((d) => d.endpoint === phoneState.endpoint);
        if (!again) throw new Error('This device is not registered on the box. Turn alerts off and on again.');
        const r = await api(`/api/v1/web-push/subscriptions/${encodeURIComponent(again.id)}/test`, { method: 'POST' }, 'Test failed');
        phoneMsg = r.status === 'sent'
          ? { text: 'Test sent. It should arrive within a few seconds.', error: false }
          : { text: `Not delivered: ${r.error || r.status}`, error: true };
      } catch (err) {
        phoneMsg = { text: err.message, error: true };
      }
      renderCard();
    });
    host.querySelectorAll('.pa-tier, #paLevel, #paEscalate, #paNight, #paArea, #paWatchdog, #paWatchdogMin').forEach((n) => {
      n.addEventListener('change', () => { draft = readDraftFromForm(); rosterMsg = { text: '', error: false }; renderCard(); });
    });
    on('paDiscard', () => { draft = null; rosterMsg = { text: '', error: false }; renderCard(); });
    on('paSave', async (e) => {
      e.currentTarget.disabled = true;
      const body = draft || readDraftFromForm();
      if (body.first_priority.length < roster.min_first_priority) {
        rosterMsg = { text: `Choose at least ${roster.min_first_priority} first-priority people.`, error: true };
        renderCard();
        return;
      }
      try {
        const saved = await api('/api/v1/web-push/roster', jsonOpts('PUT', body), 'Could not save');
        roster = Object.assign({}, roster, saved);
        draft = null;
        rosterMsg = { text: 'Saved. The VPS gets the new phone list within a few seconds.', error: false };
      } catch (err) {
        rosterMsg = { text: err.message, error: true };
      }
      await reload();
    });
    on('paWdUpload', async (e) => {
      e.currentTarget.disabled = true;
      try {
        const st = await api('/api/v1/web-push/watchdog/upload', { method: 'POST' }, 'Upload failed');
        rosterMsg = st.error ? { text: st.error, error: true } : { text: `VPS updated: ${st.result}.`, error: false };
      } catch (err) {
        rosterMsg = { text: err.message, error: true };
      }
      await reload();
    });
    host.querySelectorAll('[data-act]').forEach((b) => b.addEventListener('click', async () => {
      const act = b.getAttribute('data-act');
      const id = b.getAttribute('data-id');
      if (act === 'remove') { confirmRemove = id; renderCard(); return; }
      if (act === 'remove-no') { confirmRemove = null; renderCard(); return; }
      b.disabled = true;
      try {
        if (act === 'remove-yes') {
          await api(`/api/v1/web-push/subscriptions/${encodeURIComponent(id)}`, { method: 'DELETE' }, 'Could not remove');
          confirmRemove = null;
          deviceMsg = { id: null, text: '', error: false };
          await reload();
          return;
        }
        if (act === 'test') {
          const r = await api(`/api/v1/web-push/subscriptions/${encodeURIComponent(id)}/test`, { method: 'POST' }, 'Test failed');
          deviceMsg = r.status === 'sent' ? { id, text: 'Test sent.', error: false }
            : { id, text: `Not delivered: ${r.error || r.status}`, error: true };
        }
      } catch (err) {
        deviceMsg = { id, text: err.message, error: true };
      }
      renderCard();
    }));
  }

  async function reload() {
    await readPhoneState();
    if (!me().known) { renderCard(); return; }
    const jobs = [
      api('/api/v1/web-push/config', null, 'Could not read the alert settings').then((c) => { config = c; }),
      api('/api/v1/web-push/roster', null, 'Could not load who gets alerts').then((r) => { roster = r; rosterError = ''; })
        .catch((e) => { rosterError = e.message; }),
      api('/api/v1/web-push/subscriptions', null, 'Could not load the phones').then((r) => { devices = r.subscriptions || []; }),
    ];
    if (me().isAdmin) {
      jobs.push(api('/api/v1/web-push/deliveries', null, '').then((r) => { deliveries = r.deliveries || []; }).catch(() => {}));
    }
    try {
      await Promise.all(jobs);
    } catch (e) {
      phoneMsg = { text: e.message, error: true };
    }
    renderCard();
  }

  // ============================================================ deep links from notifications

  function openFromLink() {
    const params = new URLSearchParams(window.location.search);
    const incident = params.get('incident');
    const alertId = params.get('alert');
    if (!incident && !alertId) return;
    const clean = () => {
      params.delete('incident'); params.delete('alert'); params.delete('source');
      const q = params.toString();
      history.replaceState(null, '', `${window.location.pathname}${q ? `?${q}` : ''}#loss`);
    };
    if (typeof window.switchTab === 'function') window.switchTab('loss');
    if (!incident) { clean(); return; }
    let tries = 0;
    const attempt = async () => {
      tries += 1;
      const loss = window.edgeLoss;
      if (loss && typeof loss.focusIncident === 'function') {
        if (tries === 1 && typeof loss.refresh === 'function') { try { await loss.refresh(); } catch (_) { /* retry below */ } }
        if (loss.focusIncident(incident)) { clean(); return; }
      }
      if (tries < 20) setTimeout(attempt, 500); else clean();
    };
    attempt();
  }

  // ============================================================ boot

  window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();          // show our own button instead of the mini-infobar
    installEvent = e;
    updateInstallButton();
  });
  window.addEventListener('appinstalled', () => { installEvent = null; updateInstallButton(); toast('Installed. Open it from your home screen.'); });

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.addEventListener('message', (ev) => {
      if (ev.data && ev.data.type === 'edge-push' && window.edgeLoss && typeof window.edgeLoss.refresh === 'function') {
        window.edgeLoss.refresh();
      }
    });
  }

  window.addEventListener('edge:tab', (ev) => {
    if (ev.detail && ev.detail.tab === 'settings') reload();
  });
  window.addEventListener('edge:account', async () => {
    renderCard();
    // A different person signed in on this phone: alerts follow the new person.
    await readPhoneState();
    const who = me().id || me().username;
    if (phoneState.subscribed && phoneState.permission === 'granted' && me().known && who !== syncedFor) {
      syncedFor = who;
      try { await syncSubscription(); } catch (_) { /* shown in the card when opened */ }
    }
    maybeShowBanner();
  });

  const boot = async () => {
    mountInstallButton();
    await registration();
    await readPhoneState();
    renderCard();
    maybeShowBanner();
    openFromLink();
    const tab = $('tab-settings');
    if (tab && tab.classList.contains('active')) reload();
  };
  if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') window.edgeAuth.onReady(boot);
  else if (document.readyState !== 'loading') boot();
  else document.addEventListener('DOMContentLoaded', boot);

  window.edgePhoneAlerts = { reload, enable: enableAlerts };
})();
