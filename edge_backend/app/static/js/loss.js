/**
 * Loss prevention: the review queue, its KPIs, the evidence viewer, the
 * theft alert banner at the top of every page and the nav badge.
 *
 * Every entry is suspicious behaviour for a person to review, never a finding
 * of theft. The reviewer records what actually happened (an outcome from
 * GET /api/v1/theft/outcomes); only those outcomes feed the value and
 * false-alarm statistics. "Mark: staff sent" records who sent staff and shows
 * the real delivery report of the phone alert; nothing is invented.
 *
 * Uses globals from analytics.js: DASH, escapeHtml, isNum, getJSON, showToast,
 * formatTimestamp, formatAgo, theftAuthUrl, switchTab. No prompt/confirm/alert.
 */
(function () {
  'use strict';

  const API = '/api/v1/theft';
  const OPEN = ['ACTIVE', 'ACKNOWLEDGED', 'DISPATCHED'];
  const HANDLED = ['RESOLVED', 'FALSE_ALARM'];
  const POLL_MS = 5000;

  const D = (typeof DASH !== 'undefined') ? DASH : '—';
  const esc = (v) => (typeof escapeHtml === 'function' ? escapeHtml(v)
    : String(v === null || v === undefined ? '' : v).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const num = (v) => typeof v === 'number' && Number.isFinite(v);
  const $ = (id) => document.getElementById(id);
  const toast = (msg, kind) => { if (typeof showToast === 'function') showToast(msg, kind); };
  const authUrl = (u) => (typeof theftAuthUrl === 'function' ? theftAuthUrl(u) : u);
  const money = (v) => `$${Number(v).toFixed(2)}`;

  const state = {
    incidents: [],
    stats: null,
    outcomes: [],
    loaded: false,
    unavailable: false,
    filter: 'open',
    form: null,             // {id, kind: 'resolve'|'dispatch'}
    busy: false,
    listSig: null,
    lastAlertedId: null,
    dismissedBannerId: null,
    timer: null,
  };

  // ------------------------------------------------------------ helpers

  /** Naive UTC ("2026-09-24T06:33:17") -> Date; offset ISO strings pass through. */
  function toDate(ts) {
    if (!ts) return null;
    let s = String(ts).replace(' ', 'T');
    if (!/[zZ]|[+-]\d{2}:?\d{2}$/.test(s)) s += 'Z';
    const d = new Date(s);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  function when(ts) {
    const d = toDate(ts);
    if (!d) return D;
    const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const ago = typeof formatAgo === 'function' ? formatAgo(ts) : '';
    return ago ? `${time} (${ago})` : time;
  }
  function dayLabel(ts) {
    const d = toDate(ts);
    if (!d) return 'Unknown date';
    const key = (x) => `${x.getFullYear()}-${x.getMonth()}-${x.getDate()}`;
    const now = new Date();
    const y = new Date(now); y.setDate(now.getDate() - 1);
    if (key(d) === key(now)) return 'Today';
    if (key(d) === key(y)) return 'Yesterday';
    return d.toLocaleDateString([], { weekday: 'long', day: 'numeric', month: 'short', year: 'numeric' });
  }
  const ruleLabel = (inc) => inc.rule_label || inc.rule || inc.theft_type || 'Suspicious behaviour';
  const isOpen = (inc) => OPEN.includes(inc.status);
  const isLegacy = (inc) => inc.status === 'RESOLVED' && !inc.resolved_by && inc.outcome !== 'FALSE_ALARM';

  async function errorText(res) {
    try {
      const d = (await res.json()).detail;
      if (typeof d === 'string') return d;
      if (Array.isArray(d)) return d.map((x) => x.msg || JSON.stringify(x)).join('; ');
      return JSON.stringify(d);
    } catch (_) { return `HTTP ${res.status}`; }
  }

  function playSound() {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sawtooth';
      osc.frequency.setValueAtTime(880, ctx.currentTime);
      osc.frequency.exponentialRampToValueAtTime(440, ctx.currentTime + 0.3);
      gain.gain.setValueAtTime(0.15, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.3);
      osc.connect(gain); gain.connect(ctx.destination);
      osc.start(); osc.stop(ctx.currentTime + 0.35);
    } catch (_) { /* audio blocked until the page is interacted with */ }
  }

  function tabVisible() {
    const t = $('tab-theft');
    return !!t && t.classList.contains('active') && !document.hidden;
  }

  // ------------------------------------------------------------ data

  async function load(force) {
    const get = (url) => (typeof getJSON === 'function' ? getJSON(url, null) : fetch(url).then((r) => (r.ok ? r.json() : null)).catch(() => null));
    const [data, stats] = await Promise.all([get(`${API}/incidents?limit=200`), get(`${API}/statistics`)]);
    if (!data && !stats && !state.loaded) {
      // Not signed in yet or the service did not answer.
      state.unavailable = true;
    }
    if (data) {
      state.incidents = (data.incidents || []).slice().sort((a, b) => {
        const da = toDate(a.timestamp), db = toDate(b.timestamp);
        return (db ? db.getTime() : 0) - (da ? da.getTime() : 0);
      });
      state.unavailable = false;
      state.loaded = true;
    } else if (state.loaded) {
      state.unavailable = true;
    }
    if (stats) state.stats = stats;
    renderBadge();
    renderBanner();
    renderKpis();
    renderRuleTable();
    renderList(force);
  }

  async function loadOutcomes() {
    if (state.outcomes.length) return state.outcomes;
    const data = typeof getJSON === 'function' ? await getJSON(`${API}/outcomes`, null) : null;
    state.outcomes = (data && data.outcomes) || [];
    return state.outcomes;
  }

  // ------------------------------------------------------------ badge + banner

  function openCount() {
    const s = state.stats;
    if (s && num(s.active_incidents_count)) return s.active_incidents_count;
    return state.incidents.filter(isOpen).length;
  }

  function renderBadge() {
    const b = $('tabTheftCountBadge');
    if (!b) return;
    const n = openCount();
    b.textContent = String(n);
    b.hidden = !n;
  }

  function renderBanner() {
    const banner = $('theftAlertBanner');
    if (!banner) return;
    const active = state.incidents.find((i) => i.status === 'ACTIVE');
    if (!active) { banner.style.display = 'none'; return; }
    if (state.lastAlertedId !== active.id) {
      state.lastAlertedId = active.id;
      playSound();
    }
    if (state.dismissedBannerId === active.id) { banner.style.display = 'none'; return; }
    const set = (id, t) => { const n = $(id); if (n) n.textContent = t; };
    set('theftBannerHeadline', `Please check: ${ruleLabel(active)} on ${active.camera_name || active.camera_id}`);
    set('theftBannerConfidence', num(active.confidence) ? `Confidence ${Math.round(active.confidence * 100)}%` : `Confidence ${D}`);
    set('theftBannerTime', when(active.timestamp));
    set('theftBannerDetails', 'Suspicious behaviour for staff review, not a finding of theft.');
    banner.dataset.incident = active.id;
    banner.style.display = 'flex';
  }

  function bannerReview() {
    const banner = $('theftAlertBanner');
    const id = banner && banner.dataset.incident;
    if (typeof switchTab === 'function') switchTab('loss');
    if (id) setTimeout(() => focusIncident(id), 60);
  }

  function bannerDismiss() {
    const banner = $('theftAlertBanner');
    if (banner) {
      state.dismissedBannerId = banner.dataset.incident || null;
      banner.style.display = 'none';
    }
  }

  // ------------------------------------------------------------ KPIs + rule table

  function setKpi(id, text, note) {
    const n = $(id);
    if (!n) return;
    const missing = text === null || text === undefined;
    n.textContent = missing ? D : text;
    n.classList.toggle('metric-unobserved', missing);
    const noteEl = $(`${id}Note`);
    if (noteEl) noteEl.textContent = note || '';
  }

  function renderKpis() {
    const s = state.stats;
    if (!s) {
      ['lossKpiOpen', 'lossKpiToday', 'lossKpiValue', 'lossKpiFalseRate'].forEach((id) => setKpi(id, null, 'Statistics unavailable right now.'));
      return;
    }
    setKpi('lossKpiOpen', num(s.active_incidents_count) ? String(s.active_incidents_count) : null,
      s.active_incidents_count ? 'waiting for a person to check' : 'nothing waiting');
    setKpi('lossKpiToday', num(s.today_incidents_count) ? String(s.today_incidents_count) : null, 'flagged since midnight');
    const reviewed = num(s.reviewed_count) ? s.reviewed_count : 0;
    setKpi('lossKpiValue', reviewed > 0 && num(s.value_actioned) ? money(s.value_actioned) : null,
      reviewed > 0
        ? `item value where theft was confirmed${num(s.value_recovered) ? ` · ${money(s.value_recovered)} recovered` : ''}`
        : 'no outcomes recorded yet');
    setKpi('lossKpiFalseRate', num(s.false_alarm_rate) ? `${Math.round(s.false_alarm_rate * 100)}%` : null,
      reviewed > 0 ? `${s.false_alarm_count || 0} of ${reviewed} reviewed` : 'record outcomes to measure this');

    const legacy = $('lossLegacyNotice');
    if (legacy) {
      const n = num(s.unverified_legacy_resolutions) ? s.unverified_legacy_resolutions : 0;
      if (n > 0) {
        legacy.hidden = false;
        legacy.innerHTML = `<span>${n} older incident${n === 1 ? ' was' : 's were'} closed before outcomes were recorded, so ${n === 1 ? 'it is' : 'they are'} left out of the statistics.</span>
          <button type="button" class="btn btn-sm" data-filter="legacy">Record their outcomes</button>`;
      } else {
        legacy.hidden = true;
        legacy.innerHTML = '';
      }
    }
  }

  function renderRuleTable() {
    const body = $('lossRuleTableBody');
    if (!body) return;
    const rows = (state.stats && state.stats.false_alarm_rate_by_rule) || [];
    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="4"><div class="fp-empty">No outcomes recorded yet. Resolve incidents with an outcome (or mark them as false alarms) and each rule's false-alarm rate appears here.</div></td></tr>`;
      return;
    }
    body.innerHTML = rows.map((r) => `
      <tr>
        <td>${esc(r.label || r.rule)}</td>
        <td>${num(r.reviewed) ? r.reviewed : D}</td>
        <td>${num(r.false_alarms) ? r.false_alarms : D}</td>
        <td>${num(r.false_alarm_rate) ? `<span class="loss-rate ${r.false_alarm_rate >= 0.5 ? 'loss-rate-high' : ''}">${Math.round(r.false_alarm_rate * 100)}%</span>` : `<span class="fp-dash">${D}</span>`}</td>
      </tr>`).join('');
  }

  // ------------------------------------------------------------ list

  function filtered() {
    const f = state.filter;
    return state.incidents.filter((i) => {
      if (f === 'open') return isOpen(i);
      if (f === 'handled') return HANDLED.includes(i.status);
      if (f === 'legacy') return isLegacy(i);
      return true;
    });
  }

  function renderChips() {
    const host = $('lossFilterChips');
    if (!host) return;
    const counts = {
      open: state.incidents.filter(isOpen).length,
      handled: state.incidents.filter((i) => HANDLED.includes(i.status)).length,
      all: state.incidents.length,
      legacy: state.incidents.filter(isLegacy).length,
    };
    const chips = [['open', 'Needs review'], ['handled', 'Handled'], ['all', 'All']];
    if (counts.legacy || state.filter === 'legacy') chips.push(['legacy', 'No outcome recorded']);
    host.innerHTML = chips.map(([k, label]) => `
      <button type="button" class="loss-chip ${state.filter === k ? 'active' : ''}" data-filter="${k}" aria-pressed="${state.filter === k}">
        ${esc(label)} <span class="loss-chip-n">${counts[k]}</span></button>`).join('');
  }

  function statusPill(inc) {
    switch (inc.status) {
      case 'ACTIVE': return '<span class="badge badge-danger">Needs review</span>';
      case 'ACKNOWLEDGED': return '<span class="badge badge-warning">Being checked</span>';
      case 'DISPATCHED': return '<span class="badge badge-dispatched">Staff sent</span>';
      case 'FALSE_ALARM': return '<span class="badge badge-neutral">False alarm</span>';
      default: return isLegacy(inc)
        ? '<span class="badge badge-neutral">Closed, no outcome</span>'
        : '<span class="badge badge-green">Handled</span>';
    }
  }

  function dispatchSummary(inc) {
    const d = inc.dispatch_details;
    if (!d || (!d.dispatched_by && !inc.dispatched_by)) return '';
    const by = d.dispatched_by || inc.dispatched_by;
    const at = d.dispatched_at || inc.dispatched_at;
    const parts = [`Staff sent · marked by <b>${esc(by)}</b> ${esc(when(at))}`];
    if (d.staff_sent) parts.push(`staff: ${esc(d.staff_sent)}`);
    if (d.note) parts.push(`note: ${esc(d.note)}`);
    const a = d.alert;
    let alertLine = '';
    if (a && a.error) {
      alertLine = `<span class="loss-alert-bad">Phone alert failed: ${esc(a.error)}</span>`;
    } else if (a) {
      const sent = num(a.phones_sent) ? a.phones_sent : 0;
      const paired = num(a.phones_paired) ? a.phones_paired : null;
      if (sent > 0) {
        alertLine = `<span class="loss-alert-ok">Sent to ${sent}${paired !== null ? ` of ${paired}` : ''} phone${(paired || sent) === 1 ? '' : 's'}</span>`;
        if (num(a.phones_failed) && a.phones_failed > 0) alertLine += ` <span class="loss-alert-bad">(${a.phones_failed} failed)</span>`;
      } else {
        const why = a.skipped ? String(a.skipped) : (paired === 0 ? 'no paired phones' : 'no phone accepted it');
        alertLine = `<span class="loss-alert-warn">No phones alerted: ${esc(why)}</span>`;
      }
      if (num(a.websocket_clients) && a.websocket_clients > 0) {
        alertLine += ` · shown on ${a.websocket_clients} open dashboard${a.websocket_clients === 1 ? '' : 's'}`;
      }
    } else {
      alertLine = '<span class="loss-alert-warn">Phones were not alerted.</span>';
    }
    return `<div class="loss-dispatch">${parts.join(' · ')}<br>${alertLine}</div>`;
  }

  function outcomeSummary(inc) {
    if (isLegacy(inc)) {
      return `<div class="loss-outcome loss-outcome-legacy">Closed before outcomes were recorded (stored as “${esc(inc.outcome_label || inc.outcome || 'resolved')}”, not verified). Record what really happened.</div>`;
    }
    if (!HANDLED.includes(inc.status)) return '';
    const bits = [`Outcome: <b>${esc(inc.outcome_label || (inc.status === 'FALSE_ALARM' ? 'False alarm' : 'Resolved'))}</b>`];
    if (inc.resolved_by) bits.push(`by ${esc(inc.resolved_by)}`);
    if (inc.resolved_at) bits.push(esc(when(inc.resolved_at)));
    if (num(inc.recovered_value)) bits.push(`recovered ${money(inc.recovered_value)}`);
    let html = `<div class="loss-outcome">${bits.join(' · ')}</div>`;
    if (inc.notes) html += `<div class="loss-outcome-notes">Notes: ${esc(inc.notes)}</div>`;
    return html;
  }

  function actionsHtml(inc) {
    const id = esc(inc.id);
    const b = [];
    if (inc.status === 'ACTIVE') b.push(`<button type="button" class="btn btn-sm" data-action="acknowledge" data-incident="${id}" title="Tell other staff you are looking at it">I'm checking</button>`);
    if (inc.status === 'ACTIVE' || inc.status === 'ACKNOWLEDGED') b.push(`<button type="button" class="btn btn-sm btn-warning" data-action="dispatch" data-incident="${id}">Mark: staff sent</button>`);
    if (isOpen(inc)) {
      b.push(`<button type="button" class="btn btn-sm btn-primary" data-action="resolve" data-incident="${id}">Record outcome</button>`);
      b.push(`<button type="button" class="btn btn-sm" data-action="false-alarm" data-incident="${id}" title="Nothing suspicious happened">False alarm</button>`);
    } else if (isLegacy(inc)) {
      b.push(`<button type="button" class="btn btn-sm btn-primary" data-action="resolve" data-incident="${id}">Record outcome</button>`);
    } else {
      b.push(`<button type="button" class="btn btn-sm" data-action="resolve" data-incident="${id}" title="Correct the recorded outcome">Change outcome</button>`);
    }
    if (inc.studio_url) b.push(`<a class="btn btn-sm" href="${esc(inc.studio_url)}" target="_blank" rel="noopener">Watch camera now</a>`);
    return b.join('');
  }

  function cardHtml(inc) {
    const conf = num(inc.confidence) ? `${Math.round(inc.confidence * 100)}%` : D;
    const value = num(inc.estimated_loss_value) && inc.estimated_loss_value > 0 ? money(inc.estimated_loss_value) : null;
    const evidence = Array.isArray(inc.evidence) && inc.evidence.length ? inc.evidence : (inc.evidence_summary ? [inc.evidence_summary] : []);
    const technical = (e) => /track|keypoint|visibility|wrist|confidence|px\b|trk_/i.test(e);
    const plain = evidence.filter((e) => !technical(e));
    const tech = evidence.filter(technical);
    const thumbUrl = inc.snapshot_url || inc.evidence_snapshot_url || null;
    const thumb = thumbUrl
      ? `<a class="loss-thumb-link" href="${esc(authUrl(thumbUrl))}" target="_blank" rel="noopener" data-evidence-url="${esc(thumbUrl)}" title="Open the evidence image">
           <img src="${esc(authUrl(thumbUrl))}" alt="Evidence image for this incident" loading="lazy" class="evidence-thumb"></a>`
      : (inc.evidence_expired_at
        ? '<div class="evidence-thumb-empty" title="Deleted by the evidence storage limit (oldest first)">Evidence expired</div>'
        : '<div class="evidence-thumb-empty">No evidence image recorded</div>');
    return `
      <div class="incident-head">
        <div class="loss-head-main">
          <span class="loss-rule">${esc(ruleLabel(inc))}</span>
          <span class="badge badge-neutral loss-small-badge">${esc(inc.camera_name || inc.camera_id)}</span>
          ${inc.department ? `<span class="badge loss-small-badge">${esc(inc.department)}</span>` : ''}
        </div>
        <div class="loss-head-side">
          <span class="loss-conf" title="How clearly the camera saw it: keypoint visibility, duration and how many signals agreed">confidence ${conf}</span>
          ${statusPill(inc)}
        </div>
      </div>
      <div class="loss-body">
        ${thumb}
        <div class="loss-evidence">
          <div class="loss-evidence-label">What the camera saw</div>
          ${plain.length ? `<ul class="loss-evidence-list">${plain.map((e) => `<li>${esc(e)}</li>`).join('')}</ul>` : '<div class="fp-empty">No plain-language evidence recorded.</div>'}
          <details class="loss-tech">
            <summary>Technical details</summary>
            <div class="loss-tech-body">
              Rule: ${esc(inc.rule || inc.theft_type || D)} · Track: ${esc(inc.person_track_id || D)} · Incident: ${esc(inc.id)}
              ${tech.length ? `<ul class="loss-evidence-list">${tech.map((e) => `<li>${esc(e)}</li>`).join('')}</ul>` : ''}
            </div>
          </details>
        </div>
      </div>
      ${dispatchSummary(inc)}
      ${outcomeSummary(inc)}
      <div class="incident-foot">
        <span class="loss-meta">${esc(formatWhen(inc.timestamp))}${value ? ` · item value ${value}` : ''}</span>
        <div class="loss-actions">${actionsHtml(inc)}</div>
      </div>
      <div class="loss-form-slot" data-form-for="${esc(inc.id)}"></div>`;
  }

  function formatWhen(ts) {
    const full = typeof formatTimestamp === 'function' ? formatTimestamp(ts) : String(ts || D);
    const ago = typeof formatAgo === 'function' ? formatAgo(ts) : '';
    return ago ? `${full} (${ago})` : full;
  }

  function listSignature() {
    return JSON.stringify([state.filter, state.unavailable, filtered().map((i) => [i.id, i.status, i.outcome, i.resolved_by,
      i.recovered_value, i.notes, i.confidence, i.snapshot_url, (i.evidence || []).length, i.dispatch_details ? JSON.stringify(i.dispatch_details) : ''])]);
  }

  function renderList(force) {
    const host = $('theftIncidentsList');
    if (!host) return;
    // Never rebuild under an operator who is filling in a form.
    if (state.form && !force) { renderChips(); return; }
    const sig = listSignature();
    if (!force && sig === state.listSig && host.children.length) return;
    state.listSig = sig;
    renderChips();

    if (!state.loaded) {
      host.innerHTML = `<div class="fp-empty">${state.unavailable ? 'The incident log is not available: the loss-prevention service did not answer. It retries every few seconds.' : 'Loading…'}</div>`;
      return;
    }
    const list = filtered();
    if (!list.length) {
      const msg = {
        open: 'Nothing waiting for review. The cameras raise an incident when they see concealment, shelf sweeping, loitering at high-value stock or leaving without paying; checked incidents move to Handled.',
        handled: 'No handled incidents yet. Incidents appear here once someone records an outcome.',
        legacy: 'Every older incident now has an outcome.',
        all: 'No incidents recorded. Loss-prevention checks run on cameras with theft detection switched on and product areas drawn in Camera setup.',
      }[state.filter];
      host.innerHTML = `<div class="fp-empty">${esc(msg)}</div>`;
      return;
    }
    let lastDay = null;
    const parts = [];
    list.forEach((inc) => {
      const day = dayLabel(inc.timestamp);
      if (day !== lastDay) {
        parts.push(`<div class="loss-day">${esc(day)}</div>`);
        lastDay = day;
      }
      parts.push(`<div class="incident-card loss-card ${isOpen(inc) ? 'loss-card-open' : ''}" id="incident-${esc(inc.id)}" data-incident-card="${esc(inc.id)}">${cardHtml(inc)}</div>`);
    });
    host.innerHTML = parts.join('');
  }

  // ------------------------------------------------------------ forms

  function slotFor(id) {
    return document.querySelector(`.loss-form-slot[data-form-for="${CSS.escape(id)}"]`);
  }

  function closeForm() {
    if (state.form) {
      const slot = slotFor(state.form.id);
      if (slot) slot.innerHTML = '';
    }
    state.form = null;
  }

  async function openResolveForm(id) {
    closeForm();
    const inc = state.incidents.find((i) => i.id === id);
    const slot = slotFor(id);
    if (!inc || !slot) return;
    state.form = { id, kind: 'resolve' };
    slot.innerHTML = '<div class="fp-empty">Loading outcomes…</div>';
    const outcomes = await loadOutcomes();
    if (!state.form || state.form.id !== id) return;
    if (!outcomes.length) {
      slot.innerHTML = '<div class="loss-form"><div class="form-status form-status-error">The list of outcomes could not be loaded. Try again in a moment.</div><button type="button" class="btn btn-sm" data-action="cancel" data-incident="' + esc(id) + '">Close</button></div>';
      return;
    }
    const current = isLegacy(inc) ? null : inc.outcome;
    const name = `outcome-${id}`;
    slot.innerHTML = `
      <form class="loss-form" data-resolve-form="${esc(id)}" novalidate>
        <div class="loss-form-title">What happened?</div>
        <div class="loss-outcomes" role="radiogroup" aria-label="Outcome">
          ${outcomes.map((o) => `
            <label class="loss-outcome-opt ${o.is_false_alarm ? 'loss-outcome-false' : ''}">
              <input type="radio" name="${esc(name)}" value="${esc(o.id)}" ${current === o.id ? 'checked' : ''}
                     data-accepts-value="${o.accepts_recovered_value ? '1' : '0'}">
              <span>${esc(o.label)}</span>
            </label>`).join('')}
        </div>
        <div class="loss-form-row">
          <label class="loss-field loss-field-grow">
            <span>Notes (optional)</span>
            <textarea class="form-input" name="notes" rows="2" maxlength="1000" placeholder="e.g. items returned at the door">${esc(inc.notes || '')}</textarea>
          </label>
          <label class="loss-field loss-value-field" hidden>
            <span>Value recovered (optional)</span>
            <input class="form-input" type="number" name="recovered_value" min="0" step="0.01" inputmode="decimal" placeholder="0.00"
                   value="${num(inc.recovered_value) ? inc.recovered_value : ''}">
          </label>
        </div>
        <div class="loss-form-actions">
          <button type="submit" class="btn btn-sm btn-primary" data-action="save-outcome" data-incident="${esc(id)}">Save outcome</button>
          <button type="button" class="btn btn-sm" data-action="cancel" data-incident="${esc(id)}">Cancel</button>
          <span class="form-status loss-form-status" role="status" aria-live="polite"></span>
        </div>
      </form>`;
    syncValueField(slot);
    const first = slot.querySelector('input[type=radio]:checked') || slot.querySelector('input[type=radio]');
    if (first) first.focus({ preventScroll: true });
  }

  function syncValueField(slot) {
    const picked = slot.querySelector('input[type=radio]:checked');
    const field = slot.querySelector('.loss-value-field');
    if (field) field.hidden = !(picked && picked.dataset.acceptsValue === '1');
  }

  function openDispatchForm(id) {
    closeForm();
    const slot = slotFor(id);
    if (!slot) return;
    state.form = { id, kind: 'dispatch' };
    slot.innerHTML = `
      <form class="loss-form" data-dispatch-form="${esc(id)}" novalidate>
        <div class="loss-form-title">Record that staff were sent to check</div>
        <div class="loss-form-row">
          <label class="loss-field">
            <span>Who went (optional)</span>
            <input class="form-input" type="text" name="staff_name" maxlength="120" autocomplete="off" placeholder="e.g. Sam">
          </label>
          <label class="loss-field loss-field-grow">
            <span>Note (optional)</span>
            <input class="form-input" type="text" name="note" maxlength="200" autocomplete="off" placeholder="e.g. aisle 4, blue jacket">
          </label>
        </div>
        <label class="loss-check"><input type="checkbox" name="notify_phones" checked> Alert paired phones</label>
        <div class="loss-form-actions">
          <button type="submit" class="btn btn-sm btn-warning" data-action="save-dispatch" data-incident="${esc(id)}">Mark: staff sent</button>
          <button type="button" class="btn btn-sm" data-action="cancel" data-incident="${esc(id)}">Cancel</button>
          <span class="form-status loss-form-status" role="status" aria-live="polite"></span>
        </div>
      </form>`;
    const f = slot.querySelector('input[name=staff_name]');
    if (f) f.focus({ preventScroll: true });
  }

  function setFormStatus(form, msg, isError) {
    const s = form && form.querySelector('.loss-form-status');
    if (!s) return;
    s.textContent = msg || '';
    s.classList.toggle('form-status-error', !!isError);
  }

  async function post(id, action, body) {
    const init = { method: 'POST' };
    if (body !== undefined) {
      init.headers = { 'Content-Type': 'application/json' };
      init.body = JSON.stringify(body);
    }
    return fetch(`${API}/incidents/${encodeURIComponent(id)}/${action}`, init);
  }

  async function submitResolve(form) {
    const id = form.dataset.resolveForm;
    const picked = form.querySelector('input[type=radio]:checked');
    if (!picked) { setFormStatus(form, 'Choose what happened first.', true); return; }
    const body = { outcome: picked.value };
    const notes = (form.querySelector('[name=notes]') || {}).value;
    if (notes && notes.trim()) body.notes = notes.trim();
    const valueField = form.querySelector('.loss-value-field');
    if (valueField && !valueField.hidden) {
      const raw = (form.querySelector('[name=recovered_value]') || {}).value;
      if (raw !== undefined && String(raw).trim() !== '') {
        const v = parseFloat(raw);
        if (!Number.isFinite(v) || v < 0) { setFormStatus(form, 'The recovered value must be 0 or more.', true); return; }
        body.recovered_value = Math.round(v * 100) / 100;
      }
    }
    const btn = form.querySelector('[data-action=save-outcome]');
    if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }
    setFormStatus(form, '');
    try {
      const res = await post(id, 'resolve', body);
      if (!res.ok) throw new Error(await errorText(res));
      state.form = null;
      toast(picked.value === 'FALSE_ALARM' ? 'Marked as false alarm' : `Outcome saved: ${picked.parentElement.textContent.trim()}`, 'ok');
      await load(true);
    } catch (e) {
      setFormStatus(form, `Could not save: ${e.message}`, true);
      if (btn) { btn.disabled = false; btn.textContent = 'Save outcome'; }
    }
  }

  async function submitDispatch(form) {
    const id = form.dataset.dispatchForm;
    const body = { notify_phones: !!(form.querySelector('[name=notify_phones]') || {}).checked };
    const staff = (form.querySelector('[name=staff_name]') || {}).value;
    const note = (form.querySelector('[name=note]') || {}).value;
    if (staff && staff.trim()) body.staff_name = staff.trim();
    if (note && note.trim()) body.note = note.trim();
    const btn = form.querySelector('[data-action=save-dispatch]');
    if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }
    try {
      const res = await post(id, 'dispatch', body);
      if (!res.ok) throw new Error(await errorText(res));
      const inc = await res.json();
      state.form = null;
      const a = inc && inc.dispatch_details && inc.dispatch_details.alert;
      let msg = 'Marked: staff sent';
      if (a && a.error) msg += ' · phone alert failed';
      else if (a && num(a.phones_sent) && a.phones_sent > 0) msg += ` · sent to ${a.phones_sent} phone${a.phones_sent === 1 ? '' : 's'}`;
      else if (body.notify_phones) msg += ' · no phones alerted';
      toast(msg, a && a.error ? 'error' : 'ok');
      await load(true);
    } catch (e) {
      setFormStatus(form, `Could not save: ${e.message}`, true);
      if (btn) { btn.disabled = false; btn.textContent = 'Mark: staff sent'; }
    }
  }

  async function quickAction(btn, id, action) {
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Saving…';
    try {
      const res = action === 'false-alarm'
        ? await post(id, 'resolve', { outcome: 'FALSE_ALARM' })
        : await post(id, 'acknowledge');
      if (!res.ok) throw new Error(await errorText(res));
      if (state.form && state.form.id === id) state.form = null;
      toast(action === 'false-alarm' ? 'Marked as false alarm' : 'Marked as being checked', 'ok');
      await load(true);
    } catch (e) {
      toast(`Could not save: ${e.message}`, 'error');
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  // ------------------------------------------------------------ evidence viewer

  function openEvidence(url, caption) {
    const viewer = $('theftEvidenceViewer');
    const img = $('theftEvidenceFull');
    if (!viewer || !img || !url) return;
    const full = authUrl(url);
    img.src = full;
    const open = $('theftEvidenceOpen');
    if (open) open.href = full;
    const cap = $('theftEvidenceCaption');
    if (cap) cap.textContent = `${caption || ''} · suspicious behaviour for staff review`;
    viewer.style.display = 'flex';
    const close = $('theftEvidenceClose');
    if (close) close.focus();
  }

  function closeEvidence() {
    const viewer = $('theftEvidenceViewer');
    if (viewer) viewer.style.display = 'none';
  }

  // ------------------------------------------------------------ public

  function focusIncident(id) {
    const inc = state.incidents.find((i) => i.id === id);
    if (inc) {
      const inFilter = (state.filter === 'open' && isOpen(inc)) || (state.filter === 'handled' && HANDLED.includes(inc.status))
        || state.filter === 'all' || (state.filter === 'legacy' && isLegacy(inc));
      if (!inFilter) {
        closeForm();
        state.filter = isOpen(inc) ? 'open' : 'all';
        renderList(true);
      }
    }
    const node = document.getElementById(`incident-${id}`);
    if (!node) return false;
    node.scrollIntoView({ behavior: 'instant', block: 'center' });
    node.classList.remove('loss-flash');
    void node.offsetWidth;          // restart the highlight animation
    node.classList.add('loss-flash');
    setTimeout(() => node.classList.remove('loss-flash'), 2200);
    return true;
  }

  function refresh() { return load(true); }

  // ------------------------------------------------------------ events

  function onClick(e) {
    const chip = e.target.closest('[data-filter]');
    if (chip && chip.closest('#tab-theft')) {
      closeForm();
      state.filter = chip.dataset.filter;
      renderList(true);
      return;
    }
    const thumb = e.target.closest('[data-evidence-url]');
    if (thumb && thumb.closest('#theftIncidentsList')) {
      e.preventDefault();
      const card = thumb.closest('[data-incident-card]');
      const inc = card && state.incidents.find((i) => i.id === card.dataset.incidentCard);
      openEvidence(thumb.dataset.evidenceUrl, inc ? `${ruleLabel(inc)} · ${inc.camera_name || ''} · ${formatWhen(inc.timestamp)}` : '');
      return;
    }
    const btn = e.target.closest('[data-action]');
    if (!btn || !btn.closest('#tab-theft')) return;
    const id = btn.dataset.incident;
    const action = btn.dataset.action;
    if (action === 'resolve') openResolveForm(id);
    else if (action === 'dispatch') openDispatchForm(id);
    else if (action === 'cancel') { closeForm(); renderList(true); }
    else if (action === 'false-alarm' || action === 'acknowledge') quickAction(btn, id, action);
    else if (action === 'refresh') { btn.disabled = true; load(true).finally(() => { btn.disabled = false; toast('Review queue refreshed'); }); }
  }

  function onSubmit(e) {
    const form = e.target;
    if (!form.closest || !form.closest('#tab-theft')) return;
    e.preventDefault();
    if (form.dataset.resolveForm) submitResolve(form);
    else if (form.dataset.dispatchForm) submitDispatch(form);
  }

  function onChange(e) {
    if (e.target.matches && e.target.matches('.loss-form input[type=radio]')) syncValueField(e.target.closest('.loss-form'));
  }

  function schedule() {
    clearTimeout(state.timer);
    state.timer = setTimeout(async () => {
      if (!document.hidden) await load(false);
      schedule();
    }, POLL_MS);
  }

  function init() {
    const tab = $('tab-theft');
    if (tab) {
      tab.addEventListener('click', onClick);
      tab.addEventListener('submit', onSubmit);
      tab.addEventListener('change', onChange);
    }
    const banner = $('theftAlertBanner');
    if (banner) {
      banner.addEventListener('click', (e) => {
        const b = e.target.closest('[data-banner]');
        if (!b) return;
        if (b.dataset.banner === 'review') bannerReview();
        else if (b.dataset.banner === 'dismiss') bannerDismiss();
      });
    }
    const viewer = $('theftEvidenceViewer');
    if (viewer) viewer.addEventListener('click', (e) => { if (e.target === viewer) closeEvidence(); });
    const close = $('theftEvidenceClose');
    if (close) close.addEventListener('click', closeEvidence);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeEvidence(); });
    window.addEventListener('edge:tab', (e) => {
      if (e.detail && e.detail.tab === 'loss') load(false);
    });
    document.addEventListener('visibilitychange', () => { if (!document.hidden && tabVisible()) load(false); });

    const canPollNow = typeof canPoll === 'function' ? canPoll() : true;
    if (!canPollNow) return;       // signed out: the gate reloads the page after sign-in
    load(true);
    schedule();
  }

  window.edgeLoss = { refresh, focusIncident, closeEvidence };

  if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') {
    window.edgeAuth.onReady(init);
  } else if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
