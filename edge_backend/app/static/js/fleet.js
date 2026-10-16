// iKorex service (fleet control), dashboard side. docs/FLEET_CONTROL_CONTRACT.md
//
// GET /api/v1/fleet/status (any signed-in person) drives:
//   * the full-screen "Service paused" page (also opened by any API 423 service_paused)
//   * the licence warning banner (licence ended, within the grace days)
//   * hiding the cards/tabs of features not in the plan, with a short
//     "Not included in your plan" note where a person would look for them
//   * the Settings card #settings-fleet "iKorex service": plan status, licence date,
//     features included, last contact, control key fingerprint; administrators can
//     check in now and reset the control key pin (POST /api/v1/fleet/checkin-now, /reset-key).
// The server decides everything; this only shows it. No prompt()/confirm()/alert().
(function () {
  'use strict';

  const STATUS_URL = '/api/v1/fleet/status';
  const POLL_MS = 60000;
  const POLL_PAUSED_MS = 15000;
  const BANNER_KEY = 'edge_fleet_licence_banner_hidden';

  let status = null;
  let pollTimer = null;
  let resetMode = 'idle';     // idle | confirm
  let busy = false;
  let cardMessage = '';
  let cardError = false;

  const $ = (id) => document.getElementById(id);

  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function icon(name, size) {
    if (window.EdgeIcon && typeof window.EdgeIcon.svg === 'function') return window.EdgeIcon.svg(name, { size: size || 'md' });
    return `<svg class="icon icon-${size || 'md'}" aria-hidden="true" focusable="false"><use href="/static/icons/sprite.svg#i-${name}"></use></svg>`;
  }

  function fmtDay(day) {
    if (!day) return '';
    const t = new Date(`${day}T00:00:00`);
    if (Number.isNaN(t.getTime())) return String(day);
    return t.toLocaleDateString('en-AU', { day: 'numeric', month: 'short', year: 'numeric' });
  }

  function fmtTime(iso) {
    if (!iso) return '';
    const t = new Date(iso);
    if (Number.isNaN(t.getTime())) return String(iso);
    return t.toLocaleString('en-AU', { day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit' });
  }

  function isAdmin() {
    return !!(window.EdgeAuth && window.EdgeAuth.isAdmin);
  }

  // ------------------------------------------------------------------ 423 from any API call

  const prevFetch = window.fetch;
  window.fetch = async function (input, init) {
    const res = await prevFetch.call(this, input, init);
    if (res && res.status === 423) {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      if (url.indexOf('/api/') !== -1) {
        res.clone().json().then((d) => {
          if (d && d.code === 'service_paused') {
            showPaused(Object.assign({}, status || {}, { paused: true, message: d.message || (status && status.message) }));
            schedule(2000);
          }
        }).catch(() => {});
      }
    }
    return res;
  };

  // ------------------------------------------------------------------ paused page

  function pausedMarkup(st) {
    const expired = st.paused_reason === 'licence_expired';
    const title = expired ? 'Service paused: licence ended' : 'Service paused';
    const msg = st.message || 'Service paused. Please contact iKorex.';
    // The stacked iKorex logo (white text on the dark theme, dark text on the light one).
    const logo = `
      <img class="fleet-logo fleet-logo-on-dark" src="/static/brand/ikorex-stacked-light-text.png" alt="iKorex" width="160" height="96">
      <img class="fleet-logo fleet-logo-on-light" src="/static/brand/ikorex-stacked-dark-text.png" alt="iKorex" width="160" height="96">`;
    return `
      <div class="fleet-paused-box" role="alertdialog" aria-modal="true" aria-labelledby="fleetPausedTitle" aria-describedby="fleetPausedMsg">
        <div class="fleet-paused-brand">${logo}</div>
        <div class="fleet-paused-icon" aria-hidden="true">${icon('pause', 'lg')}</div>
        <h2 class="fleet-paused-title" id="fleetPausedTitle">${esc(title)}</h2>
        <p class="fleet-paused-msg" id="fleetPausedMsg">${esc(msg)}</p>
        <p class="fleet-paused-note">Please contact iKorex to turn the service back on.
          Your recordings, evidence and settings are kept.</p>
        <div class="fleet-paused-actions">
          <button type="button" class="btn btn-primary" id="fleetPausedRetry">${icon('refresh-cw')}Check again</button>
          <button type="button" class="btn btn-secondary" id="fleetPausedSignOut">${icon('log-out')}Sign out</button>
        </div>
        <div class="fleet-paused-status" id="fleetPausedStatus" role="status" aria-live="polite"></div>
      </div>`;
  }

  function showPaused(st) {
    let el = $('fleetPaused');
    if (!el) {
      el = document.createElement('div');
      el.id = 'fleetPaused';
      el.className = 'fleet-paused';
      document.body.appendChild(el);
    }
    const sig = `${st.paused_reason || ''}|${st.message || ''}`;
    if (!el.hidden && el.dataset.sig === sig && el.firstElementChild) return;  // unchanged: keep focus and status line
    el.dataset.sig = sig;
    el.innerHTML = pausedMarkup(st);
    // No logo file: the word mark as text (no inline handlers: the page CSP forbids them).
    el.querySelectorAll('.fleet-logo').forEach((img) => img.addEventListener('error', () => {
      const brand = el.querySelector('.fleet-paused-brand');
      if (brand && !brand.querySelector('.fleet-logo-text')) brand.innerHTML = '<div class="fleet-logo-text">iKOREX</div>';
    }));
    el.hidden = false;
    document.documentElement.classList.add('fleet-is-paused');
    const retry = $('fleetPausedRetry');
    if (retry) retry.addEventListener('click', onRetry);
    const out = $('fleetPausedSignOut');
    if (out) out.addEventListener('click', () => { if (window.edgeAuth) window.edgeAuth.signOut(); });
    if (retry && !el.contains(document.activeElement)) { try { retry.focus(); } catch (_) {} }
  }

  function hidePaused() {
    const el = $('fleetPaused');
    if (!el || el.hidden) return;
    el.hidden = true;
    document.documentElement.classList.remove('fleet-is-paused');
    // The app restarts when the service resumes: reload so every panel loads again.
    window.location.reload();
  }

  async function onRetry() {
    const line = $('fleetPausedStatus');
    if (line) line.textContent = 'Checking…';
    if (isAdmin()) {
      try { await fetch('/api/v1/fleet/checkin-now', { method: 'POST' }); } catch (_) { /* offline */ }
    }
    const st = await load();
    if (line && st && st.paused) line.textContent = `Still paused (checked ${fmtTime(new Date().toISOString())}).`;
    else if (line && !st) line.textContent = 'Could not reach this system. Try again shortly.';
  }

  // ------------------------------------------------------------------ licence banner

  function bannerHidden(day) {
    try { return sessionStorage.getItem(BANNER_KEY) === day; } catch (_) { return false; }
  }

  function renderBanner(st) {
    let el = $('fleetLicenceBanner');
    const show = st && !st.paused && st.licence_status === 'warning' && !bannerHidden(st.licence_valid_until);
    if (!show) { if (el) el.hidden = true; return; }
    if (!el) {
      el = document.createElement('div');
      el.id = 'fleetLicenceBanner';
      el.className = 'fleet-banner';
      el.setAttribute('role', 'status');
      const header = document.querySelector('.top-nav-header');
      if (header && header.parentNode) header.parentNode.insertBefore(el, header.nextSibling);
      else document.body.insertBefore(el, document.body.firstChild);
    }
    el.innerHTML = `
      <span class="fleet-banner-icon" aria-hidden="true">${icon('triangle-alert')}</span>
      <span class="fleet-banner-text">Your iKorex service licence ended on ${esc(fmtDay(st.licence_valid_until))}.
        The system keeps working until ${esc(fmtDay(st.licence_grace_until))}, then it pauses. Please contact iKorex.</span>
      <button type="button" class="btn btn-sm btn-ghost fleet-banner-close" aria-label="Hide this message">${icon('x', 'sm')}</button>`;
    el.hidden = false;
    el.querySelector('.fleet-banner-close').addEventListener('click', () => {
      try { sessionStorage.setItem(BANNER_KEY, st.licence_valid_until || ''); } catch (_) {}
      el.hidden = true;
    });
  }

  // ------------------------------------------------------------------ features not in the plan

  const closest = (id, sel) => { const n = $(id); return n ? n.closest(sel) : null; };
  // feature -> { label, hide(): elements, note(): [anchor, 'before'|'inside'] }
  const GATES = {
    theft_detection: {
      hide: () => [$('settings-theft-alerts'), $('liveBehaviourCard'), closest('lossKpiOpen', '.overview-strip, section, .loss-kpis'),
        $('lossTierKpis'), closest('theftIncidentsList', '.glass-card')],
      note: () => [$('liveBehaviourCard') || $('settings-theft-alerts'), 'before'],
    },
    zone_alerts: { hide: () => [$('zoneAlertsCard')], note: () => [$('zoneAlertsCard'), 'before'] },
    night_watch: { hide: () => [$('nightWatchCard'), $('nightWatchMount')], note: () => [$('nightWatchCard'), 'before'] },
    phone_alerts: {
      hide: () => [$('settings-phone-alerts'), $('settings-pairing'), $('settings-push'), $('settings-devices')],
      note: () => [$('settings-phone-alerts'), 'before'],
    },
    heatmaps: {
      hide: () => [$('insights-heatmap-history'), closest('layerHeatmap', 'label')],
      note: () => [$('insights-heatmap-history'), 'before'],
    },
    business_insights: {
      hide: () => {
        const recs = $('insights-recs');
        const out = recs ? Array.from(recs.children).filter((n) => !n.classList.contains('fleet-off-note')) : [];
        return out.concat(Array.from(document.querySelectorAll('.ins-report-card')));
      },
      note: () => [$('insights-recs'), 'inside'],
    },
    // The Online access card stays: its server, store ID and token are also what
    // this box uses to reach iKorex. Only the note says the tunnel will not run.
    online_access: { hide: () => [$('settings-remote-video')], note: () => [$('settings-remote'), 'before'] },
    remote_live_video: { hide: () => [$('settings-remote-video')], note: () => [$('settings-remote-video'), 'before'] },
    pos_integration: { hide: () => [$('settings-pos')], note: () => [$('settings-pos'), 'before'] },
  };

  function noteEl(feature, label) {
    const n = document.createElement('div');
    n.className = 'glass-card fleet-off-note';
    n.dataset.fleetNote = feature;
    n.innerHTML = `<span class="fleet-off-icon" aria-hidden="true">${icon('lock', 'sm')}</span>
      <span><b>${esc(label)}</b>: not included in your plan. Please contact iKorex to add it.</span>`;
    return n;
  }

  function applyFeatures(st) {
    const features = (st && st.features) || {};
    const labels = (st && st.feature_labels) || {};
    Object.keys(GATES).forEach((key) => {
      const off = features[key] === false;
      const gate = GATES[key];
      gate.hide().filter(Boolean).forEach((el) => {
        if (off) { el.classList.add('fleet-off'); el.dataset.fleetOff = (el.dataset.fleetOff || '').split(' ').concat(key).filter(Boolean).filter((v, i, a) => a.indexOf(v) === i).join(' '); }
        else if (el.dataset.fleetOff) {
          const rest = el.dataset.fleetOff.split(' ').filter((k) => k && k !== key);
          if (rest.length) el.dataset.fleetOff = rest.join(' ');
          else { delete el.dataset.fleetOff; el.classList.remove('fleet-off'); }
        }
      });
      const existing = document.querySelector(`[data-fleet-note="${key}"]`);
      if (!off) { if (existing) existing.remove(); return; }
      if (existing) return;
      const [anchor, where] = gate.note();
      if (!anchor) return;
      const n = noteEl(key, labels[key] || key);
      if (where === 'inside') anchor.insertBefore(n, anchor.firstChild);
      else if (anchor.parentNode) anchor.parentNode.insertBefore(n, anchor);
    });
    // The Loss prevention tab when none of its alerts are in the plan.
    const lossOff = ['theft_detection', 'zone_alerts', 'night_watch'].every((k) => features[k] === false);
    document.querySelectorAll('#mainNav [data-tab="loss"]').forEach((b) => b.classList.toggle('fleet-off', lossOff));
    // Insights > Recommendations / Daily report sub-tabs.
    document.querySelectorAll('#insightsSubNav [data-sub="recs"], #insightsSubNav [data-sub="report"]')
      .forEach((b) => b.classList.toggle('fleet-off', features.business_insights === false));
  }

  // ------------------------------------------------------------------ Settings card

  function planBadge(st) {
    if (!st.managed && !st.configured) return '<span class="badge badge-neutral">Not connected</span>';
    if (st.paused) return '<span class="badge badge-danger">Paused</span>';
    if (st.licence_status === 'warning') return '<span class="badge badge-warning">Licence ended</span>';
    if (st.key_changed) return '<span class="badge badge-warning">Check needed</span>';
    return '<span class="badge badge-success">Active</span>';
  }

  function planText(st) {
    if (st.paused && st.paused_reason === 'licence_expired') return 'Paused: the licence has ended.';
    if (st.paused) return 'Paused by iKorex.';
    if (!st.managed) return 'Standard service (no plan received from iKorex yet). Everything is included.';
    return 'Active.';
  }

  function licenceText(st) {
    if (!st.licence_valid_until) return 'No end date.';
    if (st.licence_status === 'ok') return `Valid until ${fmtDay(st.licence_valid_until)}.`;
    if (st.licence_status === 'warning') {
      return `Ended ${fmtDay(st.licence_valid_until)}. Keeps working until ${fmtDay(st.licence_grace_until)}.`;
    }
    return `Ended ${fmtDay(st.licence_valid_until)}.`;
  }

  function contactText(st) {
    if (!st.configured) return st.problem || 'Not set up.';
    if (!st.last_checkin_at) return 'Not yet (first contact about 20 seconds after start).';
    if (st.last_result === 'ok') return `Last contact ${fmtTime(st.last_ok_at)}.`;
    const ok = st.last_ok_at ? ` Last good contact ${fmtTime(st.last_ok_at)}.` : '';
    return `Could not reach iKorex at ${fmtTime(st.last_checkin_at)}.${ok} The system keeps running as before.`;
  }

  function renderCard() {
    const host = $('settings-fleet');
    if (!host) return;
    const st = status;
    if (!st) {
      host.innerHTML = '<div class="card-title"><span>iKorex service</span></div><div class="fp-empty">Loading…</div>';
      return;
    }
    const labels = st.feature_labels || {};
    const features = Object.keys(st.features || {}).map((k) => {
      const on = st.features[k] !== false;
      return `<li class="fleet-feature ${on ? 'is-on' : 'is-off'}">${icon(on ? 'circle-check' : 'circle-x', 'sm')}
        <span>${esc(labels[k] || k)}</span>${on ? '' : '<span class="fleet-feature-note">not included</span>'}</li>`;
    }).join('');
    const limit = st.limits && st.limits.max_cameras != null ? `Up to ${esc(st.limits.max_cameras)} cameras.` : 'No camera limit.';
    const admin = isAdmin();
    let keyBlock = st.control_key_fingerprint
      ? `<code class="fleet-code">${esc(st.control_key_fingerprint)}</code>` : '<span class="fleet-muted">Not received yet.</span>';
    if (st.control_key_source === 'env') keyBlock += ' <span class="fleet-muted">(set by the installer)</span>';
    let keyWarn = '';
    if (st.key_changed) {
      keyWarn = `<div class="fleet-warn" role="alert">${icon('shield-alert', 'sm')}
        <span><b>Control key changed — ask iKorex.</b> The iKorex server now presents a different key
        (${esc(st.offered_key_fingerprint || '')}). Changes from iKorex are not accepted until this is checked.</span></div>`;
    }
    let resetBlock = '';
    if (admin && st.control_key_source !== 'env' && st.control_key_fingerprint) {
      resetBlock = resetMode === 'confirm'
        ? `<div class="fleet-confirm"><span>Trust the key the iKorex server presents next time? Only do this when iKorex has told you the key changed.</span>
             <div class="fleet-actions"><button type="button" class="btn btn-danger btn-sm" id="fleetResetYes">Reset the key</button>
             <button type="button" class="btn btn-secondary btn-sm" id="fleetResetNo">Cancel</button></div></div>`
        : '<button type="button" class="btn btn-secondary btn-sm" id="fleetResetKey">Reset control key</button>';
    }
    host.innerHTML = `
      <div class="card-title"><span>iKorex service</span>${planBadge(st)}</div>
      <dl class="fleet-grid">
        <dt>Plan</dt><dd>${esc(planText(st))}${st.paused && st.message ? `<div class="fleet-muted">${esc(st.message)}</div>` : ''}</dd>
        <dt>Licence</dt><dd>${esc(licenceText(st))}</dd>
        <dt>Cameras</dt><dd>${limit}</dd>
        <dt>Last contact</dt><dd>${esc(contactText(st))}</dd>
        <dt>Control key</dt><dd>${keyBlock}</dd>
      </dl>
      ${keyWarn}
      <div class="fleet-sub">Included in your plan</div>
      <ul class="fleet-features">${features}</ul>
      <p class="fleet-muted fleet-privacy">iKorex sees only health and usage numbers from this system (for example disk space and
        how many alerts there were). It never sees video, pictures, camera names or any store data.</p>
      ${admin ? `<div class="fleet-actions">
        <button type="button" class="btn btn-primary btn-sm" id="fleetCheckin" ${busy || !st.configured ? 'disabled' : ''}>${icon('refresh-cw', 'sm')}Check in now</button>
        ${resetBlock}</div>` : ''}
      <div class="form-status ${cardError ? 'form-status-error' : ''}" id="fleetCardStatus" role="status" aria-live="polite">${esc(cardMessage)}</div>`;
    const on = (id, fn) => { const el = $(id); if (el) el.addEventListener('click', fn); };
    on('fleetCheckin', onCheckin);
    on('fleetResetKey', () => { resetMode = 'confirm'; renderCard(); });
    on('fleetResetNo', () => { resetMode = 'idle'; renderCard(); });
    on('fleetResetYes', onReset);
  }

  async function errorText(res, fallback) {
    let d = {};
    try { d = await res.json(); } catch (_) { /* not JSON */ }
    return (d && (d.detail || d.message)) || `${fallback} (HTTP ${res.status})`;
  }

  async function onCheckin() {
    busy = true; cardMessage = 'Contacting iKorex…'; cardError = false; renderCard();
    try {
      const res = await fetch('/api/v1/fleet/checkin-now', { method: 'POST' });
      if (res.ok) {
        status = await res.json();
        cardError = status.last_result !== 'ok';
        cardMessage = cardError ? (status.last_error || 'Could not reach iKorex.') : 'Checked in with iKorex.';
      } else { cardError = true; cardMessage = await errorText(res, 'Could not check in'); }
    } catch (_) { cardError = true; cardMessage = 'Could not reach this system.'; }
    busy = false;
    apply();
  }

  async function onReset() {
    busy = true; renderCard();
    try {
      const res = await fetch('/api/v1/fleet/reset-key', { method: 'POST' });
      if (res.ok) { status = await res.json(); cardError = false; cardMessage = 'Control key reset. The next contact with iKorex sets it again.'; }
      else { cardError = true; cardMessage = await errorText(res, 'Could not reset the key'); }
    } catch (_) { cardError = true; cardMessage = 'Could not reach this system.'; }
    busy = false; resetMode = 'idle';
    apply();
  }

  // ------------------------------------------------------------------ load + poll

  function apply() {
    if (!status) return;
    if (status.paused) showPaused(status);
    else hidePaused();
    renderBanner(status);
    applyFeatures(status);
    renderCard();
  }

  async function load() {
    if (window.edgeAuth && window.edgeAuth.isGateOpen && window.edgeAuth.isGateOpen()) return null;
    try {
      const res = await fetch(STATUS_URL, { cache: 'no-store' });
      if (!res.ok) return null;
      status = await res.json();
    } catch (_) {
      return null;
    }
    apply();
    return status;
  }

  function schedule(ms) {
    if (pollTimer) clearTimeout(pollTimer);
    const delay = ms != null ? ms : (status && status.paused ? POLL_PAUSED_MS : POLL_MS);
    pollTimer = setTimeout(async () => { pollTimer = null; await load(); schedule(); }, delay);
  }

  function boot() {
    renderCard();
    load().then(() => schedule());
  }

  window.addEventListener('edge:tab', (ev) => {
    if (ev.detail && ev.detail.tab === 'settings') { cardMessage = ''; resetMode = 'idle'; load(); }
    else if (status) applyFeatures(status);   // panels render lazily: re-apply hiding
  });
  window.addEventListener('edge:account', () => { if (status) renderCard(); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });

  if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') window.edgeAuth.onReady(boot);
  else if (document.readyState !== 'loading') boot();
  else document.addEventListener('DOMContentLoaded', boot);

  window.edgeFleet = { reload: load, status: () => status };
})();
