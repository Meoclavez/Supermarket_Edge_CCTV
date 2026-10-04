/**
 * Area & line alerts (Loss prevention tab).
 *
 * Restricted-area (RESTRICTED_AREA) and tripwire (TRIPWIRE_ALERT) alerts are
 * stored in the alert log and pushed to paired phones. This card shows them
 * on the dashboard too, so they are seen when phone push is not set up:
 *
 *   GET  /api/v1/events?types=RESTRICTED_AREA,TRIPWIRE_ALERT&acknowledged=false|true
 *   POST /api/v1/events/{id}/acknowledge      (any signed-in person, operators included)
 *
 * Only lines the operator set to "Alert staff on crossing" raise an alert;
 * nothing is invented here. Night watch events and theft incidents have their
 * own cards. Times are the store's (store_time from the server).
 *
 * loss.js reads window.edgeZoneAlerts for the top-of-page banner and the tab
 * badge, and lends its evidence viewer (window.edgeLoss.openEvidence).
 * Uses globals from analytics.js: DASH, escapeHtml, getJSON, showToast,
 * formatAgo, theftAuthUrl, canPoll. No prompt/confirm/alert.
 */
(function () {
  'use strict';

  const URL_BASE = '/api/v1/events';
  const TYPES = 'RESTRICTED_AREA,TRIPWIRE_ALERT';
  const POLL_MS = 5000;
  const LIMIT = 200;

  const D = (typeof DASH !== 'undefined') ? DASH : '—';
  const esc = (v) => (typeof escapeHtml === 'function' ? escapeHtml(v)
    : String(v === null || v === undefined ? '' : v).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const $ = (id) => document.getElementById(id);
  const toast = (msg, kind) => { if (typeof showToast === 'function') showToast(msg, kind); };
  const authUrl = (u) => (typeof theftAuthUrl === 'function' ? theftAuthUrl(u) : u);
  // The server stores evidence links with its configured base URL
  // (EDGE_BASE_URL, e.g. http://localhost:8000), which is wrong when the
  // dashboard is opened on another address or through online access. The
  // still is served by this same API, so keep only its path and query.
  const evidencePath = (u) => {
    try {
      const x = new URL(u, window.location.href);
      return x.pathname.startsWith('/api/') ? `${x.pathname}${x.search}` : u;
    } catch (_) { return u; }
  };
  const get = (url) => (typeof getJSON === 'function' ? getJSON(url, null)
    : fetch(url).then((r) => (r.ok ? r.json() : null)).catch(() => null));

  const FILTERS = [['open', 'Needs attention'], ['acked', 'Acknowledged'], ['all', 'All']];
  const SEVERITY = {
    HIGH: ['High', 'badge-danger'],
    WARNING: ['Warning', 'badge-warning'],
    INFO: ['Info', 'badge-neutral'],
  };

  const state = {
    pending: [],          // unacknowledged, newest first (banner, badge, "Needs attention")
    pendingCount: 0,      // every unacknowledged one, not only the newest LIMIT
    view: null,           // rows of the "Acknowledged" or "All" filter
    viewCount: null,
    filter: 'open',
    loaded: false,
    unavailable: false,
    busy: new Set(),      // ids being acknowledged
    errors: {},           // id -> inline error text
    sig: null,
    timer: null,
  };

  // ------------------------------------------------------------ wording

  const meta = (ev) => ev.metadata || {};
  const isArea = (ev) => ev.event_type === 'RESTRICTED_AREA';

  function zoneName(ev) {
    const m = meta(ev);
    return ev.zone_name || (isArea(ev) ? m.area_name : m.tripwire_name) || m.zone_name || null;
  }

  function lineLabel(name) {
    if (!name) return 'a line';
    return /\bline$/i.test(name) ? name : `${name} line`;
  }

  /** What happened, in plain words. */
  function headline(ev) {
    const name = zoneName(ev);
    if (isArea(ev)) return `Someone in ${name || 'a restricted area'} while it is restricted`;
    const dir = meta(ev).direction;
    return `Crossed ${lineLabel(name)}${dir ? ` (${dir})` : ''}`;
  }

  /** Banner wording: a prompt to check, never a theft finding. */
  function bannerHeadline(ev) {
    const cam = ev.camera_name || 'a camera';
    if (isArea(ev)) return `Please check: someone in ${zoneName(ev) || 'a restricted area'} on ${cam}`;
    const dir = meta(ev).direction;
    return `Please check: ${lineLabel(zoneName(ev))} crossed${dir ? ` (${dir})` : ''} on ${cam}`;
  }

  function storeDay(st) {
    if (!st || !st.date) return '';
    try {
      return new Intl.DateTimeFormat(undefined, { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' })
        .format(new Date(`${st.date}T12:00:00Z`));
    } catch (_) { return st.date; }
  }

  /** "Sat 3 Oct, 14:05 AEST" in store time. */
  function storeWhen(st, fallbackTs) {
    if (st && st.label) return `${storeDay(st)}, ${st.label}`;
    return fallbackTs ? String(fallbackTs) : D;
  }

  function whenLine(ev) {
    const ago = typeof formatAgo === 'function' && ev.timestamp ? formatAgo(ev.timestamp) : '';
    return `${storeWhen(ev.store_time, ev.timestamp)}${ago && ago !== D ? ` (${ago})` : ''}`;
  }

  // ------------------------------------------------------------ data

  const listUrl = (ack, limit) => `${URL_BASE}?types=${TYPES}&limit=${limit}${ack === null ? '' : `&acknowledged=${ack}`}`;

  async function load(force) {
    const wantView = state.filter !== 'open';
    const [pend, view] = await Promise.all([
      get(listUrl(false, LIMIT)),
      wantView ? get(listUrl(state.filter === 'acked' ? true : null, LIMIT)) : Promise.resolve(null),
    ]);
    if (pend) {
      state.pending = pend.events || [];
      state.pendingCount = typeof pend.matching === 'number' ? pend.matching : state.pending.length;
      state.loaded = true;
      state.unavailable = false;
    } else {
      state.unavailable = true;
    }
    if (wantView && view) {
      state.view = view.events || [];
      state.viewCount = typeof view.matching === 'number' ? view.matching : state.view.length;
    }
    // Drop errors for alerts that are no longer pending.
    Object.keys(state.errors).forEach((id) => { if (!state.pending.some((e) => e.id === id)) delete state.errors[id]; });
    render(force);
    try { window.dispatchEvent(new CustomEvent('edge:zone-alerts')); } catch (_) { /* old browser */ }
  }

  function rows() {
    if (state.filter === 'open') return state.pending;
    return state.view;
  }

  // ------------------------------------------------------------ render

  function chipsHtml() {
    const counts = { open: state.loaded ? state.pendingCount : null };
    if (state.filter !== 'open' && state.viewCount !== null) counts[state.filter] = state.viewCount;
    return FILTERS.map(([k, label]) => {
      const on = state.filter === k;
      const n = counts[k];
      return `<button type="button" class="loss-chip ${on ? 'active' : ''}" data-za-filter="${k}" aria-pressed="${on}">
        ${esc(label)}${typeof n === 'number' ? ` <span class="loss-chip-n">${n}</span>` : ''}</button>`;
    }).join('');
  }

  function evidenceHtml(ev) {
    if (ev.evidence_url) {
      const u = esc(authUrl(evidencePath(ev.evidence_url)));
      return `<a class="loss-thumb-link" href="${u}" target="_blank" rel="noopener" data-za-evidence="${esc(ev.id)}" title="Open the evidence image">
          <img src="${u}" alt="Evidence image for this alert" loading="lazy" class="evidence-thumb"></a>`;
    }
    if (ev.evidence_expired_at) {
      return '<div class="evidence-thumb-empty" title="Deleted by the evidence storage limit (oldest first)">Evidence expired</div>';
    }
    return '<div class="evidence-thumb-empty">No evidence image saved</div>';
  }

  function detailsHtml(ev) {
    const m = meta(ev);
    const items = [];
    const name = zoneName(ev);
    items.push(`${isArea(ev) ? 'Area' : 'Line'}: <b>${esc(name || D)}</b>`);
    items.push(`Camera: <b>${esc(ev.camera_name || D)}</b>`);
    if (isArea(ev) && typeof m.dwell_seconds === 'number') {
      items.push(`Inside for about ${esc(Math.round(m.dwell_seconds))} s before the alert`);
    }
    if (!isArea(ev) && m.direction) items.push(`Direction: ${esc(m.direction)}`);
    return `<ul class="loss-evidence-list">${items.map((t) => `<li>${t}</li>`).join('')}</ul>`;
  }

  function ackHtml(ev) {
    if (!ev.acknowledged) return '';
    const who = ev.acknowledged_by ? ` by <b>${esc(ev.acknowledged_by)}</b>` : '';
    const at = ev.acknowledged_store_time ? ` · ${esc(storeWhen(ev.acknowledged_store_time))}` : '';
    return `<div class="za-acked">Acknowledged${who}${at}</div>`;
  }

  function actionsHtml(ev) {
    const b = [];
    const id = esc(ev.id);
    if (!ev.acknowledged) {
      const busy = state.busy.has(ev.id);
      b.push(`<button type="button" class="btn btn-sm btn-primary" data-za-ack="${id}" ${busy ? 'disabled' : ''}
        title="Tell other staff this alert has been seen">${busy ? 'Acknowledging…' : 'Acknowledge'}</button>`);
    }
    if (ev.camera_id) {
      b.push(`<a class="btn btn-sm" href="/dashboard/studio?camera_id=${encodeURIComponent(ev.camera_id)}" target="_blank" rel="noopener">Watch camera now</a>`);
    }
    return b.join('');
  }

  function cardHtml(ev) {
    const [sevLabel, sevClass] = SEVERITY[ev.severity] || [ev.severity || D, 'badge-neutral'];
    const status = ev.acknowledged
      ? '<span class="badge badge-green">Acknowledged</span>'
      : '<span class="badge badge-danger">Needs attention</span>';
    const err = state.errors[ev.id];
    return `
      <div class="incident-head">
        <div class="loss-head-main">
          <span class="loss-rule">${esc(headline(ev))}</span>
          <span class="badge badge-neutral loss-small-badge">${esc(ev.camera_name || D)}</span>
          <span class="badge ${sevClass} loss-small-badge" title="Severity set for this ${isArea(ev) ? 'area' : 'line'} in Camera setup">${esc(sevLabel)}</span>
        </div>
        <div class="loss-head-side">${status}</div>
      </div>
      <div class="loss-body">
        ${evidenceHtml(ev)}
        <div class="loss-evidence">
          <div class="loss-evidence-label">${isArea(ev) ? 'Restricted area' : 'Line crossing'}</div>
          ${detailsHtml(ev)}
        </div>
      </div>
      ${ackHtml(ev)}
      <div class="incident-foot">
        <span class="loss-meta">${esc(whenLine(ev))}</span>
        <div class="loss-actions">${actionsHtml(ev)}</div>
      </div>
      ${err ? `<div class="za-error" role="alert">${esc(err)}</div>` : ''}`;
  }

  function signature(list) {
    return JSON.stringify([state.filter, state.loaded, state.unavailable, state.pendingCount, state.viewCount,
      [...state.busy], state.errors,
      (list || []).map((e) => [e.id, e.acknowledged, e.acknowledged_by, e.evidence_url, e.evidence_expired_at, e.camera_name])]);
  }

  function render(force) {
    const host = $('zoneAlertsList');
    const chips = $('zoneAlertsChips');
    if (chips) chips.innerHTML = chipsHtml();
    if (!host) return;
    const list = rows();
    const sig = signature(list);
    if (!force && sig === state.sig && host.children.length) return;
    state.sig = sig;

    if (!state.loaded || (state.filter !== 'open' && list === null)) {
      host.innerHTML = `<div class="fp-empty">${state.unavailable
        ? 'Area and line alerts are not available right now: the alert log did not answer. It retries every few seconds.'
        : 'Loading…'}</div>`;
      return;
    }
    if (!list.length) {
      const msg = {
        open: 'No area or line alerts need attention. They appear when someone enters a restricted area during its hours, or crosses a line set to alert staff.',
        acked: 'No acknowledged area or line alerts yet.',
        all: 'No area or line alerts. They appear when someone enters a restricted area during its hours, or crosses a line set to alert staff.',
      }[state.filter];
      host.innerHTML = `<div class="fp-empty">${esc(msg)}</div>`;
      return;
    }
    const more = state.filter === 'open' ? state.pendingCount - list.length : (state.viewCount || 0) - list.length;
    host.innerHTML = list.map((ev) =>
      `<div class="incident-card loss-card za-card ${ev.acknowledged ? '' : 'loss-card-open'}" id="zone-alert-${esc(ev.id)}" data-za-card="${esc(ev.id)}">${cardHtml(ev)}</div>`).join('')
      + (more > 0 ? `<div class="za-more">Showing the newest ${list.length}; ${more} older not shown.</div>` : '');
  }

  // ------------------------------------------------------------ actions

  async function acknowledge(id) {
    if (state.busy.has(id)) return;
    state.busy.add(id);
    delete state.errors[id];
    render(true);
    try {
      const res = await fetch(`${URL_BASE}/${encodeURIComponent(id)}/acknowledge`, { method: 'POST' });
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try { const d = (await res.json()).detail; if (typeof d === 'string') detail = d; } catch (_) { /* not JSON */ }
        state.errors[id] = res.status === 401 ? 'Your session has expired. Sign in again.' : `Could not acknowledge: ${detail}`;
        return;
      }
      toast('Alert acknowledged', 'ok');
    } catch (e) {
      state.errors[id] = 'Could not acknowledge: the device did not answer. Try again.';
    } finally {
      state.busy.delete(id);
      await load(true);
    }
  }

  function findAlert(id) {
    return state.pending.find((e) => e.id === id) || (state.view || []).find((e) => e.id === id) || null;
  }

  function openEvidence(id) {
    const ev = findAlert(id);
    if (!ev || !ev.evidence_url) return false;
    const caption = `${headline(ev)} · ${ev.camera_name || ''} · ${storeWhen(ev.store_time, ev.timestamp)}`;
    if (window.edgeLoss && typeof window.edgeLoss.openEvidence === 'function') {
      window.edgeLoss.openEvidence(evidencePath(ev.evidence_url), caption, null, 'still', 'area or line alert for staff to check');
      return true;
    }
    return false;
  }

  /** Scroll to one alert (used by the banner's "Review now"). */
  function focus(id) {
    const ev = findAlert(id);
    if (ev && !ev.acknowledged && state.filter !== 'open') {
      state.filter = 'open';
      render(true);
    }
    const node = document.getElementById(`zone-alert-${id}`);
    if (!node) return false;
    node.scrollIntoView({ behavior: 'instant', block: 'center' });
    node.classList.remove('loss-flash');
    void node.offsetWidth;
    node.classList.add('loss-flash');
    setTimeout(() => node.classList.remove('loss-flash'), 2200);
    return true;
  }

  function onClick(e) {
    const chip = e.target.closest('[data-za-filter]');
    if (chip) {
      const f = chip.dataset.zaFilter;
      if (f !== state.filter) {
        state.filter = f;
        state.view = null;
        state.viewCount = null;
        render(true);
        load(true);
      }
      return;
    }
    const thumb = e.target.closest('[data-za-evidence]');
    if (thumb) {
      if (openEvidence(thumb.dataset.zaEvidence)) e.preventDefault();   // else the link opens a new tab
      return;
    }
    const ack = e.target.closest('[data-za-ack]');
    if (ack) { acknowledge(ack.dataset.zaAck); return; }
    const btn = e.target.closest('[data-za="refresh"]');
    if (btn) {
      btn.disabled = true;
      load(true).finally(() => { btn.disabled = false; toast('Area and line alerts refreshed'); });
    }
  }

  function schedule() {
    clearTimeout(state.timer);
    state.timer = setTimeout(async () => {
      if (!document.hidden) await load(false);
      schedule();
    }, POLL_MS);
  }

  function init() {
    const card = $('zoneAlertsCard');
    if (card) card.addEventListener('click', onClick);
    window.addEventListener('edge:tab', (e) => { if (e.detail && e.detail.tab === 'loss') load(false); });
    document.addEventListener('visibilitychange', () => { if (!document.hidden) load(false); });
    const canPollNow = typeof canPoll === 'function' ? canPoll() : true;
    if (!canPollNow) return;       // signed out: the gate reloads the page after sign-in
    load(true);
    schedule();
  }

  window.edgeZoneAlerts = {
    /** Unacknowledged alerts, newest first (copies). */
    pending: () => state.pending.slice(),
    /** Every unacknowledged alert, not only the ones loaded. */
    pendingCount: () => (state.loaded ? state.pendingCount : 0),
    bannerHeadline,
    headline,
    focus,
    refresh: () => load(true),
  };

  if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') {
    window.edgeAuth.onReady(init);
  } else if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
