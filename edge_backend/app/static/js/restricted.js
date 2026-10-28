/**
 * Restricted areas (high security): the Loss prevention card, the announcement
 * strip on every page, "I'm going in" / "I'm out", arming, the settings dialog,
 * restricted-area alerts with their outcome, the audit trail and the instant
 * alert feed.
 *
 *   GET  /api/v1/restricted-areas/status          the one cheap poll (3 s visible, 15 s hidden)
 *   GET  /api/v1/restricted-areas                 areas + cameras, zones, camera areas, lines (settings)
 *   GET  /api/v1/restricted-areas/catalogue       triggers, levels, outcomes, limits
 *   POST /{id}/announce  /announcements/{id}/end  operators too
 *   POST /{id}/arm (operators) /{id}/disarm /{id}/resume /{id}/test-alert /{id}/forget-still-figures (admins)
 *   POST "" / PUT /{id} / DELETE /{id}            settings (admins)
 *   GET  /events  POST /events/{id}/outcome       alerts and what happened (operators too)
 *   GET  /audit                                   the audit trail
 *   WS   /api/v1/events/ws                        instant alerts: a message with ra_area_id is handed
 *                                                 to zone_alerts.js (ingest), so loss.js raises the
 *                                                 banner and sound at once
 *
 * The server decides every permission; buttons only hide what an operator
 * cannot do. No face recognition and no identity: alerts name a camera and a
 * place, never a person. No inline handlers (CSP), no prompt/confirm/alert.
 * Uses globals from analytics.js when present: DASH, escapeHtml, showToast,
 * switchTab, canPoll, theftAuthUrl; js/help.js EdgeHelp.friendlyError.
 */
(function () {
  'use strict';

  const API = '/api/v1/restricted-areas';
  const POLL_VISIBLE_MS = 3000;
  const POLL_HIDDEN_MS = 15000;
  const EVENTS_EVERY_MS = 30000;
  const D = (typeof DASH !== 'undefined') ? DASH : '—';
  const $ = (id) => document.getElementById(id);
  const esc = (v) => (typeof escapeHtml === 'function' ? escapeHtml(v)
    : String(v === null || v === undefined ? '' : v).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const ico = (name, size) => {
    try { return window.EdgeIcon && typeof window.EdgeIcon.svg === 'function' ? window.EdgeIcon.svg(name, { size: size || 'sm' }) : ''; } catch (_) { return ''; }
  };
  const toast = (msg, kind) => { if (typeof showToast === 'function') showToast(msg, kind); };
  const authUrl = (u) => (u && typeof theftAuthUrl === 'function' ? theftAuthUrl(u) : u);
  const account = () => window.EdgeAuth || {};
  const isAdmin = () => !!account().isAdmin;
  const meId = () => account().id || null;
  const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || `${one}s`)}`;
  const DAYS = [['mon', 'Mon'], ['tue', 'Tue'], ['wed', 'Wed'], ['thu', 'Thu'], ['fri', 'Fri'], ['sat', 'Sat'], ['sun', 'Sun']];
  const TIER_LABEL = { review: 'Review', watch: 'Watch', alert: 'Alert', critical: 'Critical' };
  const TIER_ICON = { review: 'info', watch: 'eye', alert: 'bell', critical: 'siren' };
  const ADMIN_ONLY_MSG = 'Only an owner or administrator can change these settings. You can see how they are set.';
  const ACTIONS = {
    created: 'created the area', settings_changed: 'changed the settings', deleted: 'deleted the area',
    armed: 'armed it', disarmed: 'disarmed it', schedule_resumed: 'returned it to its schedule',
    announced: 'announced entry', announcement_ended: "tapped \"I'm out\"", alert: 'alert',
    test_alert: 'sent a test alert', outcome: 'recorded what happened', upgraded: 'upgraded it from an older restricted area',
    still_figures_cleared: 'cleared the learnt still figures',
  };
  const OUTCOME_LABEL = { false_alarm: 'False alarm', authorised: 'Authorised (staff or contractor)', incident: 'Real incident' };

  const state = {
    status: null,          // GET /status
    skewMs: 0,             // server clock minus this clock
    loaded: false,
    unavailable: false,
    catalogue: null,
    events: null,
    eventsAt: 0,
    eventsSig: null,
    filters: { area: '', trigger: '', needs: 'open' },
    outcomeFor: null,
    busy: new Set(),
    errors: {},
    audit: null,
    timer: null,
    tick: null,
    ws: null,
    wsTry: 0,
    wsTimer: null,
    started: false,
    dialog: null,          // { kind, ... }
    opener: null,
    settings: null,        // { list, cat, area (draft), id, editable }
  };

  // ------------------------------------------------------------ small helpers

  function friendly(status, body) {
    if (window.EdgeHelp && typeof window.EdgeHelp.friendlyError === 'function') return window.EdgeHelp.friendlyError(status, body);
    const d = body && typeof body.detail === 'string' ? body.detail : '';
    return d || 'That did not work. Try again in a moment.';
  }

  /** fetch that never throws: { ok, status, data } or { ok:false, status, body, message }. 409/422 details are shown as they are. */
  async function api(url, opts) {
    const o = Object.assign({}, opts || {});
    if (o.json !== undefined) {
      o.method = o.method || 'POST';
      o.body = JSON.stringify(o.json);
      o.headers = Object.assign({ 'Content-Type': 'application/json' }, o.headers || {});
      delete o.json;
    }
    let res;
    try { res = await fetch(url, o); } catch (_) { return { ok: false, status: 0, message: friendly(0, null) }; }
    let body = null;
    try { body = await res.json(); } catch (_) { body = null; }
    if (res.ok) return { ok: true, status: res.status, data: body };
    const detail = body && typeof body.detail === 'string' ? body.detail : '';
    const message = (res.status === 409 || res.status === 422) && detail ? detail : friendly(res.status, body);
    return { ok: false, status: res.status, body, message };
  }

  function planOff() {
    const st = window.edgeFleet && typeof window.edgeFleet.status === 'function' ? window.edgeFleet.status() : null;
    return !!(st && st.features && st.features.restricted_areas === false);
  }

  const nowMs = () => Date.now() + state.skewMs;
  const parseTs = (iso) => { const t = Date.parse(iso); return Number.isFinite(t) ? t : null; };

  /** "12 min left" / "40 s left" / "ended". */
  function leftText(ms) {
    if (!Number.isFinite(ms) || ms <= 0) return 'ended';
    const s = Math.round(ms / 1000);
    if (s < 60) return `${s} s left`;
    const m = Math.round(s / 60);
    if (m < 60) return `${m} min left`;
    return `${Math.floor(m / 60)} h ${m % 60} min left`;
  }

  function stateLabel(a) {
    if (!a.enabled) return ['Off', 'ra-chip-off'];
    if (a.armed && a.announced) return ['Armed, someone announced', 'ra-chip-announced'];
    if (a.armed) return ['Armed', 'ra-chip-armed'];
    return ['Disarmed', 'ra-chip-disarmed'];
  }

  function occupancyText(occ) {
    const n = occ ? occ.people : null;
    if (n === null || n === undefined) return `${D} not seen right now`;
    if (n === 0) return 'Nobody inside';
    return `${plural(n, 'person', 'people')} inside`;
  }

  function sourceText(a) {
    if (!a.enabled) return 'Switched off in its settings.';
    const m = a.manual;
    if (a.armed_source === 'manual' && m) {
      const until = m.until ? ` until ${m.until.label}` : '';
      return `${m.state === 'armed' ? 'Armed' : 'Disarmed'} by hand${until}${m.by ? ` by ${m.by}` : ''}${m.reason ? ` (${m.reason})` : ''}.`;
    }
    if (a.armed_source === 'always') return 'Always armed.';
    const nx = a.next_change ? ` ${a.armed ? 'Disarms' : 'Arms'} at ${a.next_change.label}.` : '';
    return `On its schedule.${nx}`;
  }

  /** The AlertEntry shape zone_alerts.js keeps, from a websocket alert message. */
  function wsEntry(msg) {
    return {
      id: msg.id || msg.alert_id,
      event_type: 'RESTRICTED_AREA',
      severity: msg.severity || 'HIGH',
      camera_id: msg.camera_id || null,
      camera_name: msg.camera_name || msg.camera_id || null,
      timestamp: msg.timestamp || new Date().toISOString(),
      zone_name: msg.area_name || msg.zone_name || null,
      metadata: msg,
      evidence_url: msg.snapshot_url || null,
      acknowledged: false,
      store_time: null,
    };
  }

  const isPhone = () => {
    try { return window.matchMedia('(pointer: coarse)').matches || window.innerWidth < 700; } catch (_) { return false; }
  };

  const areas = () => (state.status && state.status.areas) || [];
  const areaById = (id) => areas().find((a) => a.id === id) || null;

  // ------------------------------------------------------------ data

  async function load() {
    if (planOff()) { state.status = null; render(); return; }
    const t0 = Date.now();
    const r = await api(`${API}/status`);
    if (r.ok && r.data) {
      state.status = r.data;
      const srv = parseTs(r.data.server_time);
      if (srv) state.skewMs = srv - Math.round((t0 + Date.now()) / 2);
      state.loaded = true;
      state.unavailable = false;
    } else {
      state.unavailable = true;
    }
    const sig = JSON.stringify([state.status && state.status.open_events_total,
      areas().map((a) => (a.last_events && a.last_events[0] ? a.last_events[0].id : null))]);
    if (sig !== state.eventsSig || Date.now() - state.eventsAt > EVENTS_EVERY_MS) {
      state.eventsSig = sig;
      await loadEvents();
    }
    render(true);
  }

  async function loadEvents() {
    if (!$('restrictedAreasCard') || planOff()) return;
    const f = state.filters;
    const q = new URLSearchParams({ limit: '50' });
    if (f.area) q.set('area_id', f.area);
    if (f.trigger) q.set('trigger', f.trigger);
    if (f.needs === 'open') q.set('needs_outcome', 'true');
    const r = await api(`${API}/events?${q.toString()}`);
    state.eventsAt = Date.now();
    if (r.ok && r.data) state.events = r.data.events || [];
  }

  async function loadCatalogue() {
    if (state.catalogue) return state.catalogue;
    const r = await api(`${API}/catalogue`);
    if (r.ok) state.catalogue = r.data;
    return state.catalogue;
  }

  async function loadAudit() {
    const r = await api(`${API}/audit?limit=50`);
    state.audit = r.ok ? (r.data.entries || []) : { error: r.message };
    renderAudit();
  }

  // ------------------------------------------------------------ render: card

  /** Replace a host's content only when it changed (countdowns tick on their own). */
  const sigOf = (html) => html.replace(/(<span data-ra-until="[^"]*">)[^<]*/g, '$1');
  function put(host, html, fromPoll) {
    const sig = sigOf(html);
    if (host.dataset.raSig === sig) return false;
    // A poll never rebuilds under someone choosing or typing.
    const a = document.activeElement;
    if (fromPoll && a && host.contains(a) && /^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName)) return false;
    host.dataset.raSig = sig;
    host.innerHTML = html;
    return true;
  }

  function render(fromPoll) {
    renderCard(fromPoll);
    renderStrip();
    renderSettingsCard();
  }

  function areaHtml(a) {
    const [label, cls] = stateLabel(a);
    const id = esc(a.id);
    const anns = (a.announcements || []).map((n) => `
      <div class="ra-ann">${ico('user')}<span>Announced by <b>${esc(n.user_name)}</b> until ${esc(n.until_store ? n.until_store.hhmm : D)}
        (<span data-ra-until="${esc(n.until)}">${esc(leftText(parseTs(n.until) - nowMs()))}</span>)${n.people > 1 ? `, ${esc(n.people)} people` : ''}${n.note ? `: ${esc(n.note)}` : ''}</span>
        ${n.mine || isAdmin() ? `<button type="button" class="btn btn-xs" data-ra="out" data-ra-ann="${esc(n.id)}">${ico('log-out')}I'm out</button>` : ''}</div>`).join('');
    const problems = (a.problems || []).map((p) => `<div class="ra-problem">${ico('eye-off')}<span>${esc(p.message)}</span></div>`).join('');
    const watching = (a.watching_cameras || []).length;
    const last = (a.last_events || []).slice(0, 3).map((e) => `<li><span class="tier-badge tier-${esc(e.tier)}">${esc(TIER_LABEL[e.tier] || e.tier)}</span> ${esc(e.label)}${e.test ? ' (test)' : ''}</li>`).join('');
    const b = [];
    if (a.enabled) {
      b.push(`<button type="button" class="btn btn-sm btn-primary" data-ra="announce" data-ra-area="${id}">${ico('log-in')}I'm going in</button>`);
      if (!a.armed) b.push(`<button type="button" class="btn btn-sm" data-ra="arm" data-ra-area="${id}">${ico('lock')}Arm now</button>`);
      if (isAdmin() && a.armed) b.push(`<button type="button" class="btn btn-sm" data-ra="disarm" data-ra-area="${id}">Disarm</button>`);
      if (isAdmin() && a.armed_source === 'manual') b.push(`<button type="button" class="btn btn-sm" data-ra="resume" data-ra-area="${id}">Back to schedule</button>`);
      if (isAdmin()) b.push(`<button type="button" class="btn btn-sm btn-ghost" data-ra="test" data-ra-area="${id}">${ico('bell')}Send test alert</button>`);
    }
    b.push(`<button type="button" class="btn btn-sm btn-ghost" data-ra="settings" data-ra-area="${id}">${ico('settings')}${isAdmin() ? 'Settings' : 'See settings'}</button>`);
    return `<div class="ra-area" id="ra-area-${id}" data-ra-card="${id}">
      <div class="ra-area-head">
        <span class="ra-area-name">${ico('lock')}<span>${esc(a.name)}</span></span>
        <span class="ra-chip ${cls}">${esc(label)}</span>
        ${a.open_events ? `<span class="badge badge-danger">${esc(plural(a.open_events, 'alert'))} to check</span>` : ''}
      </div>
      <div class="ra-area-meta">
        <span>${ico('users')}${esc(occupancyText(a.occupancy))}</span>
        <span>${ico('clock')}${esc(sourceText(a))}</span>
        <span>${ico('cctv')}${watching ? esc(plural(watching, 'camera')) : 'No camera watches it yet'}</span>
      </div>
      ${anns}${problems}
      ${last ? `<ul class="ra-last">${last}</ul>` : ''}
      <div class="ra-actions">${b.join('')}</div>
    </div>`;
  }

  function eventHtml(e) {
    const id = esc(e.id);
    const tier = TIER_LABEL[e.tier] ? e.tier : null;
    const m = e.measured || {};
    const facts = [];
    if (e.people_inside !== null && e.people_inside !== undefined) facts.push(`${plural(e.people_inside, 'person', 'people')} inside`);
    if (typeof m.speed === 'number') facts.push(`about ${m.speed} ${m.speed_unit === 'm/s' ? 'm/s' : 'body heights a second (estimate: camera not calibrated)'}`);
    if (typeof m.reversals === 'number') facts.push(`${m.reversals} direction changes`);
    if (typeof m.turns === 'number') facts.push(`${m.turns} sharp turns`);
    if (typeof m.inside_sec === 'number') facts.push(`inside ${Math.round(m.inside_sec)} s`);
    if (typeof m.seconds === 'number') facts.push(`for ${m.seconds} s`);
    if (m.poi_name) facts.push(`near ${m.poi_name}`);
    if (typeof m.people === 'number') facts.push(`${m.people} people, ${m.allowed} allowed`);
    if (m.tamper) facts.push({ covered: 'camera covered or blacked out', moved: 'camera turned away', offline: 'camera sent no picture' }[m.tamper] || m.tamper);
    const ev = [];
    if (e.snapshot_url) ev.push(`<button type="button" class="btn btn-xs" data-ra="still" data-ra-event="${id}">${ico('image')}Picture</button>`);
    if (e.clip_url) ev.push(`<button type="button" class="btn btn-xs" data-ra="clip" data-ra-event="${id}">${ico('film')}Clip</button>`);
    if (e.evidence_expired) ev.push('<span class="ra-muted">Evidence expired</span>');
    let outcome = '';
    if (e.outcome) {
      outcome = `<div class="ra-outcome-done">${ico('check')}<span><b>${esc(e.outcome_label || OUTCOME_LABEL[e.outcome] || e.outcome)}</b>${e.outcome_by ? ` by ${esc(e.outcome_by)}` : ''}${e.outcome_note ? `: ${esc(e.outcome_note)}` : ''}</span></div>`;
    } else if (e.needs_outcome && state.outcomeFor === e.id) {
      const busy = state.busy.has(e.id);
      outcome = `<form class="za-outcome" data-ra-outcome-form="${id}">
        <fieldset><legend>What happened?</legend>
          ${Object.entries(OUTCOME_LABEL).map(([k, l], i) => `<label class="za-outcome-opt"><input type="radio" name="outcome" value="${k}"${i === 0 ? ' required' : ''}> ${esc(l)}</label>`).join('')}
        </fieldset>
        <label class="za-outcome-note" for="ra-note-${id}">Note (optional)</label>
        <input class="input" id="ra-note-${id}" name="note" maxlength="200" autocomplete="off">
        <div class="ra-actions"><button type="submit" class="btn btn-sm btn-primary"${busy ? ' disabled' : ''}>${busy ? 'Saving…' : 'Save outcome'}</button>
          <button type="button" class="btn btn-sm" data-ra="outcome-cancel">Cancel</button></div></form>`;
    } else if (e.needs_outcome) {
      outcome = `<div class="ra-actions"><button type="button" class="btn btn-sm btn-primary" data-ra="outcome" data-ra-event="${id}">Record what happened</button>
        ${e.acknowledged ? `<span class="ra-muted">Seen${e.acknowledged_by ? ` by ${esc(e.acknowledged_by)}` : ''}; the outcome is still needed.</span>` : ''}</div>`;
    }
    const err = state.errors[e.id];
    return `<div class="incident-card loss-card ra-event${e.needs_outcome ? ' loss-card-open' : ''}" id="ra-event-${id}">
      <div class="incident-head"><div class="loss-head-main">
        ${tier ? `<span class="tier-badge tier-${tier}">${ico(TIER_ICON[tier])}${esc(TIER_LABEL[tier])}</span>` : ''}
        <span class="loss-rule">${esc(e.trigger_label || 'Restricted area alert')}: ${esc(e.area_name || D)}</span>
        ${e.test ? '<span class="badge badge-neutral">Test</span>' : ''}
      </div><div class="loss-head-side">${e.needs_outcome ? '<span class="badge badge-danger">Outcome needed</span>' : '<span class="badge badge-green">Closed</span>'}</div></div>
      <div class="ra-event-meta"><span>${ico('cctv')}${esc(e.camera_name || D)}</span><span>${ico('clock')}${esc(e.store_time ? e.store_time.label : (e.timestamp || D))}</span>
        ${facts.length ? `<span>${esc(facts.join(' · '))}</span>` : ''}</div>
      ${e.body ? `<p class="ra-event-body">${esc(e.body)}</p>` : ''}
      ${ev.length ? `<div class="ra-actions">${ev.join('')}</div>` : ''}
      ${outcome}
      ${err ? `<div class="za-error" role="alert">${esc(err)}</div>` : ''}
    </div>`;
  }

  function filtersHtml() {
    const f = state.filters;
    const cat = state.catalogue;
    const trig = cat ? cat.triggers || [] : [];
    return `<div class="ra-filter-row">
      <div class="loss-chips" role="group" aria-label="Show restricted area alerts">
        ${[['open', 'Needs outcome'], ['all', 'All']].map(([k, l]) => `<button type="button" class="loss-chip ${f.needs === k ? 'active' : ''}" data-ra="filter-needs" data-ra-value="${k}" aria-pressed="${f.needs === k}">${esc(l)}${k === 'open' && state.status ? ` <span class="loss-chip-n">${esc(state.status.open_events_total || 0)}</span>` : ''}</button>`).join('')}
      </div>
      <label class="sr-only" for="raFilterArea">Area</label>
      <select class="select ra-select" id="raFilterArea" data-ra-filter="area"><option value="">Every area</option>${areas().map((a) => `<option value="${esc(a.id)}"${f.area === a.id ? ' selected' : ''}>${esc(a.name)}</option>`).join('')}</select>
      <label class="sr-only" for="raFilterTrigger">Sign</label>
      <select class="select ra-select" id="raFilterTrigger" data-ra-filter="trigger"><option value="">Every sign</option>${trig.map((t) => `<option value="${esc(t.id)}"${f.trigger === t.id ? ' selected' : ''}>${esc(t.label)}</option>`).join('')}</select>
    </div>`;
  }

  function renderCard(fromPoll) {
    const host = $('raCardBody');
    if (!host) return;
    if (planOff()) {
      put(host, '<div class="fp-empty">Restricted areas are not included in your iKorex plan. Please contact iKorex to add them.</div>');
      return;
    }
    if (!state.loaded) {
      put(host, `<div class="fp-empty">${state.unavailable ? 'Restricted areas did not answer. Retrying every few seconds.' : 'Loading…'}</div>`);
      return;
    }
    const list = areas();
    const head = `<div class="ra-card-actions">
      ${list.some((a) => a.enabled) ? `<button type="button" class="btn btn-sm btn-primary" data-ra="announce">${ico('log-in')}I'm going in</button>` : ''}
      ${isAdmin() ? `<button type="button" class="btn btn-sm" data-ra="new">${ico('plus')}New area</button>` : ''}
      <button type="button" class="btn btn-sm btn-ghost" data-ra="refresh">${ico('refresh-cw')}Refresh</button></div>`;
    const areasHtml = list.length ? `<div class="ra-areas">${list.map(areaHtml).join('')}</div>`
      : `<div class="fp-empty">No restricted area yet. ${isAdmin() ? 'Press "New area", or select a zone on the Store map and press "Restricted area settings".' : 'An owner or administrator sets them up.'}</div>`;
    const ev = state.events;
    const evHtml = ev === null ? '<div class="fp-empty">Loading…</div>'
      : (ev.length ? ev.map(eventHtml).join('')
        : `<div class="fp-empty">${state.filters.needs === 'open' ? 'No restricted area alert is waiting for an outcome.' : 'No restricted area alerts yet.'}</div>`);
    const auditOpen = !!(host.querySelector('#raAudit') && host.querySelector('#raAudit').open);
    const changed = put(host, `${head}${areasHtml}
      <h3 class="ra-sub">Restricted area alerts</h3>
      ${filtersHtml()}
      <div class="loss-list ra-events" id="raEventList">${evHtml}</div>
      <details class="ra-audit" id="raAudit"${auditOpen ? ' open' : ''}><summary>Audit trail: who armed, announced and closed what</summary><div id="raAuditList"></div></details>`, fromPoll);
    if (changed && auditOpen) renderAudit();
  }

  function renderAudit() {
    const host = $('raAuditList');
    if (!host) return;
    const a = state.audit;
    if (a === null) { host.innerHTML = '<div class="fp-empty">Loading…</div>'; return; }
    if (a.error) { host.innerHTML = `<div class="za-error">${esc(a.error)}</div>`; return; }
    if (!a.length) { host.innerHTML = '<div class="fp-empty">Nothing recorded yet.</div>'; return; }
    host.innerHTML = `<ul class="ra-audit-list">${a.map((x) => {
      const d = x.detail || {};
      let what = ACTIONS[x.action] || x.action;
      if (x.action === 'alert' || x.action === 'test_alert') what = `${what}: ${d.title || d.trigger || ''}${typeof d.latency_ms === 'number' ? ` (sent in ${Math.round(d.latency_ms)} ms)` : ''}`;
      if (x.action === 'outcome') what = `${what}: ${OUTCOME_LABEL[d.outcome] || d.outcome}${d.note ? ` (${d.note})` : ''}`;
      if (x.action === 'announced') what = `${what} for ${d.minutes} min${d.people > 1 ? `, ${d.people} people` : ''}${d.note ? ` (${d.note})` : ''}`;
      if ((x.action === 'armed' || x.action === 'disarmed' || x.action === 'schedule_resumed') && d.reason) what = `${what}: ${d.reason}`;
      const who = x.actor ? `${x.actor}${x.actor_role ? ` (${x.actor_role})` : ''}` : 'System';
      return `<li><span class="ra-muted">${esc(x.store_time)}</span> · <b>${esc(x.area_name || D)}</b> · ${esc(who)} ${esc(what)}</li>`;
    }).join('')}</ul>`;
  }

  // ------------------------------------------------------------ render: strip + settings card

  function renderStrip() {
    const strip = $('raStrip');
    if (!strip) return;
    const list = areas().filter((a) => a.enabled);
    if (planOff() || !list.length) { strip.hidden = true; put(strip, ''); return; }
    const anns = [];
    list.forEach((a) => (a.announcements || []).forEach((n) => anns.push([a, n])));
    put(strip, `<span class="ra-strip-label">${ico('lock')}<span>Restricted areas</span></span>
      <span class="ra-strip-items">${anns.length ? anns.map(([a, n]) => `<span class="ra-strip-item">Area: <b>${esc(a.name)}</b> ${D} announced by ${esc(n.user_name)} until ${esc(n.until_store ? n.until_store.hhmm : D)}
        (<span data-ra-until="${esc(n.until)}">${esc(leftText(parseTs(n.until) - nowMs()))}</span>)
        ${n.mine ? `<button type="button" class="btn btn-xs" data-ra="out" data-ra-ann="${esc(n.id)}">I'm out</button>` : ''}</span>`).join('')
    : `<span class="ra-muted">${esc(plural(list.filter((a) => a.armed).length, 'area'))} armed, nobody announced.</span>`}</span>
      <button type="button" class="btn btn-xs btn-primary" data-ra="announce">${ico('log-in')}I'm going in</button>`);
    strip.hidden = false;
  }

  function renderSettingsCard() {
    const host = $('settings-restricted-areas');
    if (!host) return;
    const list = areas();
    const off = planOff();
    put(host, `<div class="card-title"><span>Restricted areas</span></div>
      <p class="ra-muted">Cash office, safe room or stockroom: every movement is tracked while armed, and staff tap "I'm going in" before entering. ${off ? 'Not included in your iKorex plan.' : ''}</p>
      ${off ? '' : `<ul class="ra-settings-list">${list.map((a) => `<li><span>${ico('lock')}${esc(a.name)} <span class="ra-chip ${stateLabel(a)[1]}">${esc(stateLabel(a)[0])}</span></span>
        <button type="button" class="btn btn-xs" data-ra="settings" data-ra-area="${esc(a.id)}">${isAdmin() ? 'Settings' : 'See settings'}</button></li>`).join('')}</ul>
      <div class="ra-actions"><button type="button" class="btn btn-sm" data-ra="open">Open restricted areas</button>
        ${isAdmin() ? `<button type="button" class="btn btn-sm" data-ra="new">${ico('plus')}New area</button>` : ''}</div>`}`);
  }

  function tickCountdowns() {
    document.querySelectorAll('[data-ra-until]').forEach((n) => {
      const t = parseTs(n.getAttribute('data-ra-until'));
      const txt = leftText(t - nowMs());
      if (n.textContent !== txt) n.textContent = txt;
    });
  }

  // ------------------------------------------------------------ dialog

  function ensureDialog() {
    if ($('raDialog')) return;
    const scrim = document.createElement('div');
    scrim.className = 'ra-scrim';
    scrim.id = 'raScrim';
    scrim.hidden = true;
    const dlg = document.createElement('div');
    dlg.className = 'ra-dialog';
    dlg.id = 'raDialog';
    dlg.hidden = true;
    dlg.setAttribute('role', 'dialog');
    dlg.setAttribute('aria-modal', 'true');
    dlg.setAttribute('aria-labelledby', 'raDialogTitle');
    dlg.innerHTML = `<div class="ra-dialog-head"><h2 class="ra-dialog-title" id="raDialogTitle" tabindex="-1"></h2>
      <button type="button" class="btn btn-sm btn-ghost btn-icon" data-ra="close" aria-label="Close">${ico('x', 'md')}</button></div>
      <form class="ra-dialog-body" id="raDialogForm" novalidate></form>
      <p class="ra-dialog-msg" id="raDialogMsg" role="status" aria-live="polite"></p>
      <div class="ra-dialog-foot" id="raDialogFoot"></div>`;
    document.body.appendChild(scrim);
    document.body.appendChild(dlg);
    scrim.addEventListener('click', closeDialog);
    dlg.addEventListener('click', onClick);
    dlg.addEventListener('submit', onDialogSubmit);
    dlg.addEventListener('change', onDialogChange);
    dlg.addEventListener('input', onDialogInput);
  }

  function showDialog(kind, title, body, foot, extra) {
    ensureDialog();
    if ($('raDialog').hidden) state.opener = document.activeElement;
    state.dialog = Object.assign({ kind }, extra || {});
    $('raDialogTitle').innerHTML = `${ico(kind === 'settings' ? 'settings' : 'lock', 'md')}<span>${esc(title)}</span>`;
    $('raDialogForm').innerHTML = body;
    $('raDialogFoot').innerHTML = foot;
    setMsg('');
    $('raDialog').hidden = false;
    $('raScrim').hidden = false;
    const first = $('raDialogForm').querySelector('input:not([type=hidden]):not([disabled]), select:not([disabled]), textarea');
    (first || $('raDialogTitle')).focus({ preventScroll: true });
  }

  function closeDialog() {
    const d = $('raDialog');
    if (!d || d.hidden) return;
    d.hidden = true;
    $('raScrim').hidden = true;
    state.dialog = null;
    state.settings = null;
    const back = state.opener;
    state.opener = null;
    if (back && back.isConnected && typeof back.focus === 'function') back.focus({ preventScroll: true });
  }

  function setMsg(text, kind) {
    const m = $('raDialogMsg');
    if (!m) return;
    m.textContent = text || '';
    m.className = `ra-dialog-msg${kind === 'ok' ? ' is-ok' : kind === 'error' ? ' is-error' : ''}`;
  }

  const field = (id, label, input, hint) => `<div class="ra-field"><label for="${id}">${esc(label)}</label>${input}${hint ? `<p class="ra-hint">${esc(hint)}</p>` : ''}</div>`;
  const submitBtn = (label) => `<button type="submit" form="raDialogForm" class="btn btn-sm btn-primary">${esc(label)}</button>`;
  const closeBtn = '<button type="button" class="btn btn-sm" data-ra="close">Cancel</button>';

  function openAnnounce(areaId) {
    const list = areas().filter((a) => a.enabled);
    if (!list.length) { toast('No restricted area is switched on.', 'error'); return; }
    const lim = (state.catalogue && state.catalogue.announce) || { min: 5, max: 120, default: 15, people_max: 10 };
    const pick = list.find((a) => a.id === areaId) || list[0];
    const body = field('raAnnArea', 'Area', `<select class="select" id="raAnnArea" name="area">${list.map((a) => `<option value="${esc(a.id)}"${a.id === pick.id ? ' selected' : ''}>${esc(a.name)}</option>`).join('')}</select>`)
      + field('raAnnMinutes', 'For how long', `<div class="ra-inline"><input class="input" type="number" id="raAnnMinutes" name="minutes" min="${lim.min}" max="${lim.max}" step="1" value="${lim.default}" inputmode="numeric"><span>minutes</span></div>`, `${lim.min} to ${lim.max} minutes. Staying longer than this is an alert.`)
      + field('raAnnPeople', 'How many people are going in', `<input class="input" type="number" id="raAnnPeople" name="people" min="1" max="${lim.people_max || 10}" step="1" value="1" inputmode="numeric">`)
      + field('raAnnNote', 'Note (optional)', '<input class="input" type="text" id="raAnnNote" name="note" maxlength="200" autocomplete="off" placeholder="e.g. counting the till float">')
      + '<p class="ra-hint">Everyone sees your announcement with a countdown. Tap "I\'m out" when you leave.</p>';
    showDialog('announce', "I'm going in", body, `${submitBtn('Announce')}${closeBtn}`);
  }

  function openArm(kind, areaId) {
    const a = areaById(areaId);
    if (!a) return;
    const titles = { arm: `Arm ${a.name} now`, disarm: `Disarm ${a.name}`, resume: `${a.name}: back to its schedule` };
    let body = field('raReason', 'Reason', '<input class="input" type="text" id="raReason" name="reason" maxlength="200" required autocomplete="off">',
      'Required. It is kept in the audit trail with your name.');
    if (kind === 'arm' || kind === 'disarm') {
      const opts = [['', a.next_change ? `Until the next schedule change (${a.next_change.label})` : (kind === 'arm' ? 'Up to 12 hours' : 'Choose a time')],
        ['30', '30 minutes'], ['60', '1 hour'], ['120', '2 hours'], ['240', '4 hours'], ['480', '8 hours'], ['720', '12 hours']]
        .filter(([v]) => v || kind === 'arm' || a.next_change);
      body += field('raMinutes', 'For how long', `<select class="select" id="raMinutes" name="minutes">${opts.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join('')}</select>`);
    }
    if (kind === 'disarm') body += '<p class="ra-hint">While disarmed nothing in this area is flagged. To go in briefly, "I\'m going in" is safer.</p>';
    if (kind === 'arm') body += '<p class="ra-hint">Anyone inside when it arms, without an announcement, is an alert.</p>';
    showDialog(kind, titles[kind], body, `${submitBtn({ arm: 'Arm now', disarm: 'Disarm', resume: 'Back to schedule' }[kind])}${closeBtn}`, { areaId });
  }

  function openTest(areaId) {
    const a = areaById(areaId);
    if (!a) return;
    showDialog('test', `Test alert: ${a.name}`, '<p>This sends a clearly marked test alert through the real path: the banner and sound on open dashboards and the alert phones, at the level of "Entry without notice". Nobody needs to go there.</p>',
      `${submitBtn('Send test alert')}${closeBtn}`, { areaId });
  }

  async function onDialogSubmit(e) {
    e.preventDefault();
    const d = state.dialog;
    if (!d) return;
    const form = $('raDialogForm');
    const v = (n) => { const x = form.elements.namedItem(n); return x ? x.value : ''; };
    if (d.kind === 'settings') { saveSettings(); return; }
    let r;
    setMsg('Saving…');
    if (d.kind === 'announce') {
      const id = v('area');
      r = await api(`${API}/${encodeURIComponent(id)}/announce`, { json: {
        minutes: Number(v('minutes')), people: Number(v('people') || 1), note: v('note').trim() || null, source: isPhone() ? 'phone' : 'dashboard' } });
      if (r.ok) {
        const n = r.data.announcement || {};
        toast(`Announced: ${n.area_name || ''} until ${n.until_store ? n.until_store.hhmm : ''}`, 'ok');
      }
    } else if (d.kind === 'arm' || d.kind === 'disarm' || d.kind === 'resume') {
      if (!v('reason').trim()) { setMsg('Write a reason first.', 'error'); return; }
      const body = { reason: v('reason').trim() };
      if (v('minutes')) body.minutes = Number(v('minutes'));
      r = await api(`${API}/${encodeURIComponent(d.areaId)}/${d.kind}`, { json: body });
      if (r.ok) toast({ arm: 'Armed', disarm: 'Disarmed', resume: 'Back on its schedule' }[d.kind], 'ok');
    } else if (d.kind === 'test') {
      r = await api(`${API}/${encodeURIComponent(d.areaId)}/test-alert`, { json: {} });
      if (r.ok) toast('Test alert sent', 'ok');
    } else {
      return;
    }
    if (!r.ok) { setMsg(r.message, 'error'); return; }
    closeDialog();
    await load();
  }

  async function endAnnouncement(annId, btn) {
    if (btn) btn.disabled = true;
    const r = await api(`${API}/announcements/${encodeURIComponent(annId)}/end`, { json: {} });
    if (!r.ok) { toast(r.message, 'error'); if (btn) btn.disabled = false; return; }
    toast("Thanks: you're marked as out", 'ok');
    await load();
  }

  // ------------------------------------------------------------ settings dialog

  function blankArea(zoneId, zoneName) {
    const triggers = {};
    ((state.catalogue && state.catalogue.triggers) || []).forEach((t) => {
      const cfg = { enabled: t.enabled !== false, tier: t.tier, cooldown_sec: t.cooldown, while_announced: !!t.while_announced };
      Object.entries(t.params || {}).forEach(([k, p]) => { cfg[k] = p.default; });
      if (t.id === 'wrong_door') cfg.door_line_ids = [];
      triggers[t.id] = cfg;
    });
    return { id: null, name: zoneName || '', enabled: true, floor_zone_id: zoneId || null, camera_regions: [],
      arming: { mode: 'always', schedule: [], schedule_mode: 'armed_during' }, triggers, points_of_interest: [],
      security: { confidence_delta: (state.catalogue && state.catalogue.security && state.catalogue.security.confidence_delta.default) || 0.05 },
      cameras: [] };
  }

  async function openSettings(arg) {
    ensureDialog();
    showDialog('settings', 'Restricted area settings', '<p class="ra-hint">Loading the settings…</p>', closeBtn.replace('Cancel', 'Close'));
    const [list, cat] = await Promise.all([api(API), loadCatalogue()]);
    if (!list.ok || !cat) { setMsg(list.ok ? 'The trigger list could not be read.' : list.message, 'error'); return; }
    const L = list.data;
    let area = null;
    if (typeof arg === 'string' && arg) area = (L.areas || []).find((a) => a.id === arg) || null;
    if (arg && typeof arg === 'object' && arg.zone_id) {
      area = (L.areas || []).find((a) => a.floor_zone_id === arg.zone_id) || null;
      if (!area) {
        const z = (L.zones || []).find((x) => x.id === arg.zone_id);
        area = blankArea(arg.zone_id, z ? z.name : '');
      }
    }
    if (!area) area = blankArea(null, '');
    state.settings = { list: L, cat, area: JSON.parse(JSON.stringify(area)), id: area.id, editable: !!L.can_edit, zoneCams: null, confirmDelete: false };
    if (area.floor_zone_id && !area.id) fetchZoneCameras(area.floor_zone_id);
    renderSettings();
  }

  function camName(id) {
    const s = state.settings;
    const c = s && (s.list.cameras || []).find((x) => x.id === id);
    return c ? c.name : id;
  }

  async function fetchZoneCameras(zoneId) {
    const s = state.settings;
    if (!s || !zoneId) return;
    let cams = null;
    try {
      if (window.FloorplanAPI && typeof window.FloorplanAPI.camerasSeeingZone === 'function') {
        const r = await window.FloorplanAPI.camerasSeeingZone(zoneId);
        if (Array.isArray(r)) cams = r.map((c) => ({ camera_id: c.camera_id, name: camName(c.camera_id), covered_fraction: c.covered_fraction, source: 'map' }));
      }
    } catch (_) { cams = null; }
    if (!cams) {
      const r = await api(`${API}/cameras-for-zone/${encodeURIComponent(zoneId)}`);
      cams = r.ok ? (r.data.cameras || []) : [];
    }
    if (state.settings === s && s.area.floor_zone_id === zoneId) {
      s.zoneCams = cams;
      const host = $('raWatching');
      if (host) host.innerHTML = watchingHtml();
    }
  }

  function watchingHtml() {
    const s = state.settings;
    const a = s.area;
    let cams = a.cameras || [];
    if ((!cams.length || !a.id) && s.zoneCams) cams = s.zoneCams;
    if (!cams.length) return '<p class="ra-hint">No camera watches it yet: calibrate a camera on the Store map, link a camera to the zone, or choose an area drawn on a camera.</p>';
    return `<ul class="ra-cam-list">${cams.map((c) => {
      const pct = typeof c.covered_fraction === 'number' ? ` (sees about ${Math.round(c.covered_fraction * 100)} %)` : '';
      const how = { region: 'area drawn on this camera', floor: 'calibrated', calibrated: 'calibrated', linked: 'whole view linked to the zone (not calibrated)', map: 'from the store map' }[c.source] || '';
      return `<li>${ico('cctv')}${esc(c.name || camName(c.camera_id))}${esc(pct)}${how ? ` <span class="ra-muted">${esc(how)}</span>` : ''}</li>`;
    }).join('')}</ul>`;
  }

  function num(v, d) { const n = Number(v); return Number.isFinite(n) ? n : d; }

  function triggerHtml(t, cfg, s) {
    const tiers = s.cat.tiers || [];
    const p = `t_${t.id}`;
    const params = Object.entries(t.params || {}).map(([k, sp]) => `<div class="ra-param"><label for="${p}_${k}">${esc(sp.label)}</label>
      <div class="ra-inline"><input class="input" type="number" id="${p}_${k}" name="${p}:${k}" min="${sp.min}" max="${sp.max}" step="any" value="${esc(cfg[k] !== undefined ? cfg[k] : sp.default)}" inputmode="decimal"><span>${esc(sp.unit)}</span></div></div>`).join('');
    let doors = '';
    if (t.id === 'wrong_door') {
      const camIds = new Set([...(s.area.cameras || []).map((c) => c.camera_id), ...(s.area.camera_regions || []).map((r) => r.camera_id)]);
      const lines = (s.list.lines || []).filter((l) => !camIds.size || camIds.has(l.camera_id));
      const on = new Set(cfg.door_line_ids || []);
      doors = lines.length ? `<fieldset class="ra-subfield"><legend>Door lines (drawn in Camera setup)</legend>${lines.map((l) => `<label class="ra-check" for="${p}_line_${esc(l.id)}"><input type="checkbox" id="${p}_line_${esc(l.id)}" name="${p}:line:${esc(l.id)}"${on.has(l.id) ? ' checked' : ''}> ${esc(l.name)} (${esc(camName(l.camera_id))})</label>`).join('')}</fieldset>`
        : '<p class="ra-hint">No line is drawn on these cameras yet. Draw the door line in Camera setup first.</p>';
    }
    return `<fieldset class="ra-trigger${cfg.enabled ? ' is-on' : ''}" data-ra-trigger="${esc(t.id)}">
      <legend><label class="ra-check" for="${p}_enabled"><input type="checkbox" id="${p}_enabled" name="${p}:enabled"${cfg.enabled ? ' checked' : ''}> <b>${esc(t.label)}</b></label></legend>
      <p class="ra-hint">${esc(t.explain)} <i>Example: ${esc(t.example)}</i></p>
      <div class="ra-trigger-grid">
        <div class="ra-param"><label for="${p}_tier">Alert level</label><select class="select" id="${p}_tier" name="${p}:tier">${tiers.map((x) => `<option value="${esc(x.id)}"${x.id === cfg.tier ? ' selected' : ''} title="${esc(x.help)}">${esc(x.label)}</option>`).join('')}</select></div>
        ${params}
        <div class="ra-param"><label for="${p}_cool">Repeat at most every</label><div class="ra-inline"><input class="input" type="number" id="${p}_cool" name="${p}:cooldown_sec" min="5" max="3600" step="1" value="${esc(cfg.cooldown_sec)}"><span>s</span></div></div>
      </div>
      <label class="ra-check" for="${p}_wa"><input type="checkbox" id="${p}_wa" name="${p}:while_announced"${cfg.while_announced ? ' checked' : ''}> Also while someone announced entry</label>
      ${doors}
    </fieldset>`;
  }

  function renderSettings() {
    const s = state.settings;
    if (!s) return;
    const a = s.area;
    const L = s.list;
    const arm = a.arming || { mode: 'always', schedule: [] };
    const rows = arm.schedule && arm.schedule.length ? arm.schedule : [];
    const camAreaOn = new Set((a.camera_regions || []).filter((r) => r.intrusion_zone_id).map((r) => r.intrusion_zone_id));
    const inline = (a.camera_regions || []).filter((r) => !r.intrusion_zone_id);
    const tz = L.store_time && L.store_time.label ? L.store_time.label.split(' ').slice(-1)[0] : '';
    const isFloor = !!a.floor_zone_id;
    const pois = a.points_of_interest || [];
    const sec = Math.round(num((a.security || {}).confidence_delta, 0.05) * 100);
    const cams = L.cameras || [];
    const body = `
      ${s.editable ? '' : `<div class="ra-locked" role="note">${ico('lock')}<span>${esc(ADMIN_ONLY_MSG)}</span></div>`}
      <div class="ra-field"><label for="raSetName">Name</label><input class="input" type="text" id="raSetName" name="name" maxlength="64" value="${esc(a.name)}" autocomplete="off" placeholder="e.g. Cash office"></div>
      <label class="ra-check" for="raSetEnabled"><input type="checkbox" id="raSetEnabled" name="enabled"${a.enabled !== false ? ' checked' : ''}> Switched on</label>

      <h3 class="ra-sub">Where</h3>
      <div class="ra-field"><label for="raSetZone">Zone on the store map</label><select class="select" id="raSetZone" name="floor_zone_id">
        <option value="">None</option>${(L.zones || []).map((z) => `<option value="${esc(z.id)}"${z.id === a.floor_zone_id ? ' selected' : ''}>${esc(z.name)}${z.category ? ` (${esc(String(z.category).toLowerCase())})` : ''}</option>`).join('')}</select>
        <p class="ra-hint">Works on every calibrated camera that sees the zone, in metres on the floor.</p></div>
      <fieldset class="ra-subfield"><legend>Areas drawn on a camera (for cameras that are not calibrated)</legend>
        ${(L.camera_areas || []).length ? (L.camera_areas || []).map((c) => `<label class="ra-check" for="raCa_${esc(c.id)}"><input type="checkbox" id="raCa_${esc(c.id)}" name="ca:${esc(c.id)}"${camAreaOn.has(c.id) ? ' checked' : ''}> ${esc(c.name)} (${esc(camName(c.camera_id))})</label>`).join('') : '<p class="ra-hint">None drawn yet.</p>'}
        ${inline.map((r) => `<p class="ra-hint">An area drawn by points on ${esc(camName(r.camera_id))} is kept.</p>`).join('')}
        <div class="ra-inline"><label class="sr-only" for="raDrawCam">Camera to draw on</label><select class="select" id="raDrawCam">${cams.map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('')}</select>
          <button type="button" class="btn btn-sm" data-ra="draw"${cams.length ? '' : ' disabled'} data-operator-ok>${ico('pencil')}Draw on a camera</button></div>
        <p class="ra-hint">Opens Camera setup with the restricted area tool; come back and tick the new area here.</p>
      </fieldset>
      <div class="ra-field"><span class="ra-label">Cameras that watch it</span><div id="raWatching">${watchingHtml()}</div></div>

      <h3 class="ra-sub">When it is armed${tz ? ` (store time, ${esc(tz)})` : ' (store time)'}</h3>
      <div class="ra-radios" role="radiogroup" aria-label="When it is armed">
        <label class="ra-check" for="raArmAlways"><input type="radio" id="raArmAlways" name="arm_mode" value="always"${arm.mode !== 'schedule' ? ' checked' : ''}> Always armed</label>
        <label class="ra-check" for="raArmSched"><input type="radio" id="raArmSched" name="arm_mode" value="schedule"${arm.mode === 'schedule' ? ' checked' : ''}> On a schedule</label>
      </div>
      <div class="ra-schedule"${arm.mode === 'schedule' ? '' : ' hidden'}>
        <div class="ra-field"><label for="raSchedMode">The times below are when it is</label><select class="select" id="raSchedMode" name="schedule_mode">
          <option value="armed_during"${arm.schedule_mode !== 'armed_outside' ? ' selected' : ''}>armed (armed during these times)</option>
          <option value="armed_outside"${arm.schedule_mode === 'armed_outside' ? ' selected' : ''}>open (armed outside these times)</option></select></div>
        ${rows.map((r, i) => `<div class="ra-sched-row" data-ra-row="${i}">
          <div class="ra-days">${DAYS.map(([k, t]) => `<label class="ra-check" for="raS${i}_${k}"><input type="checkbox" id="raS${i}_${k}" name="s${i}:${k}"${(r.days || []).includes(k) ? ' checked' : ''}> ${t}</label>`).join('')}</div>
          <div class="ra-inline"><label class="sr-only" for="raS${i}_from">From</label><input class="input" type="time" id="raS${i}_from" name="s${i}:from" value="${esc(r.from || '18:00')}">
            <span>to</span><label class="sr-only" for="raS${i}_to">To</label><input class="input" type="time" id="raS${i}_to" name="s${i}:to" value="${esc(r.to || '07:00')}">
            <button type="button" class="btn btn-xs btn-ghost" data-ra="sched-del" data-ra-row="${i}" aria-label="Remove this time">${ico('trash-2')}</button></div></div>`).join('')}
        <button type="button" class="btn btn-sm" data-ra="sched-add">${ico('plus')}Add a time</button>
        <p class="ra-hint">A time that ends before it starts runs past midnight (e.g. 18:00 to 07:00).</p>
      </div>

      <h3 class="ra-sub">What is flagged</h3>
      <ul class="ra-tier-help">${(s.cat.tiers || []).map((x) => `<li><span class="tier-badge tier-${esc(x.id)}">${esc(x.label)}</span> ${esc(x.help)}</li>`).join('')}</ul>
      ${(s.cat.triggers || []).map((t) => triggerHtml(t, (a.triggers || {})[t.id] || {}, s)).join('')}

      <h3 class="ra-sub">Points of interest (safe, cash drawer, key box)</h3>
      ${pois.map((p, i) => `<div class="ra-poi" data-ra-poi="${i}">
        <div class="ra-field"><label for="raP${i}_name">Name</label><input class="input" type="text" id="raP${i}_name" name="p${i}:name" maxlength="40" value="${esc(p.name || '')}"></div>
        ${p.camera_id || !isFloor ? `<div class="ra-inline"><label for="raP${i}_cam">Camera</label><select class="select" id="raP${i}_cam" name="p${i}:camera_id">${cams.map((c) => `<option value="${esc(c.id)}"${c.id === p.camera_id ? ' selected' : ''}>${esc(c.name)}</option>`).join('')}</select>
          <label for="raP${i}_u">Across</label><input class="input ra-num" type="number" id="raP${i}_u" name="p${i}:u" min="0" max="1" step="0.01" value="${esc(p.u !== undefined ? p.u : 0.5)}">
          <label for="raP${i}_v">Down</label><input class="input ra-num" type="number" id="raP${i}_v" name="p${i}:v" min="0" max="1" step="0.01" value="${esc(p.v !== undefined ? p.v : 0.5)}"></div>
          <p class="ra-hint">Position in the camera picture: 0 = left / top, 1 = right / bottom.</p>`
    : `<div class="ra-inline"><label for="raP${i}_x">x</label><input class="input ra-num" type="number" id="raP${i}_x" name="p${i}:x" step="0.1" value="${esc(p.x !== undefined ? p.x : 0)}"><span>m</span>
          <label for="raP${i}_y">y</label><input class="input ra-num" type="number" id="raP${i}_y" name="p${i}:y" step="0.1" value="${esc(p.y !== undefined ? p.y : 0)}"><span>m</span></div>
          <p class="ra-hint">Store-map metres (the map shows x across, y down).</p>`}
        <input type="hidden" name="p${i}:id" value="${esc(p.id || '')}">
        <button type="button" class="btn btn-xs btn-ghost" data-ra="poi-del" data-ra-row="${i}">${ico('trash-2')}Remove</button></div>`).join('')}
      <button type="button" class="btn btn-sm" data-ra="poi-add">${ico('plus')}Add a point</button>

      <h3 class="ra-sub">Sensitivity</h3>
      <div class="ra-field"><label for="raSens">${esc((s.cat.security && s.cat.security.confidence_delta.label) || 'Extra sensitivity inside the area')}</label>
        <div class="ra-inline"><input class="input ra-num" type="number" id="raSens" name="confidence_delta" min="0" max="20" step="1" value="${sec}"><span>points</span></div>
        <p class="ra-hint">${esc((s.cat.security && s.cat.security.confidence_delta.explain) || '')}</p></div>
      ${a.state && a.state.still_figures ? `<p class="ra-hint">${esc(plural(a.state.still_figures, 'still figure'))} (posters, mannequins) learnt while the area was not guarded.</p>` : ''}`;
    const f = $('raDialogForm');
    const keepScroll = f ? f.scrollTop : 0;
    const foot = s.editable
      ? `${submitBtn(s.id ? 'Save' : 'Create area')}
         ${s.id ? `<button type="button" class="btn btn-sm" data-ra="test" data-ra-area="${esc(s.id)}">${ico('bell')}Send test alert</button>
           <button type="button" class="btn btn-sm btn-ghost" data-ra="forget">Forget learnt still figures</button>
           <button type="button" class="btn btn-sm ${s.confirmDelete ? 'btn-danger' : 'btn-ghost'}" data-ra="delete">${ico('trash-2')}${s.confirmDelete ? 'Yes, delete it' : 'Delete'}</button>` : ''}
         <button type="button" class="btn btn-sm" data-ra="close">Close</button>`
      : '<button type="button" class="btn btn-sm" data-ra="close">Close</button>';
    $('raDialogTitle').innerHTML = `${ico('settings', 'md')}<span>${esc(s.id ? `${a.name}: settings` : 'New restricted area')}</span>`;
    f.innerHTML = body;
    f.scrollTop = keepScroll;
    $('raDialogFoot').innerHTML = foot;
    if (!s.editable) f.querySelectorAll('input, select, button').forEach((n) => { if (!n.hasAttribute('data-operator-ok')) n.disabled = true; });
  }

  /** The draft from the form (also used before a structural re-render). */
  function readSettings() {
    const s = state.settings;
    const f = $('raDialogForm');
    if (!s || !f) return null;
    const a = s.area;
    const el = (n) => f.elements.namedItem(n);
    const v = (n) => { const x = el(n); return x ? x.value : null; };
    const c = (n) => { const x = el(n); return !!(x && x.checked); };
    if (el('name')) a.name = v('name');
    if (el('enabled')) a.enabled = c('enabled');
    if (el('floor_zone_id')) a.floor_zone_id = v('floor_zone_id') || null;
    const ca = (s.list.camera_areas || []).filter((x) => c(`ca:${x.id}`)).map((x) => ({ camera_id: x.camera_id, intrusion_zone_id: x.id }));
    a.camera_regions = ca.concat((a.camera_regions || []).filter((r) => !r.intrusion_zone_id));
    const mode = (f.querySelector('input[name="arm_mode"]:checked') || {}).value || 'always';
    const rows = [];
    f.querySelectorAll('[data-ra-row]').forEach((rowEl) => {
      if (!rowEl.classList.contains('ra-sched-row')) return;
      const i = rowEl.getAttribute('data-ra-row');
      rows.push({ days: DAYS.map(([k]) => k).filter((k) => c(`s${i}:${k}`)), from: v(`s${i}:from`) || '18:00', to: v(`s${i}:to`) || '07:00' });
    });
    a.arming = { mode, schedule: rows, schedule_mode: v('schedule_mode') || 'armed_during' };
    const trig = {};
    (s.cat.triggers || []).forEach((t) => {
      const p = `t_${t.id}`;
      const cur = Object.assign({}, (a.triggers || {})[t.id] || {});
      if (!el(`${p}:tier`)) { trig[t.id] = cur; return; }
      cur.enabled = c(`${p}:enabled`);
      cur.tier = v(`${p}:tier`);
      cur.while_announced = c(`${p}:while_announced`);
      cur.cooldown_sec = num(v(`${p}:cooldown_sec`), cur.cooldown_sec);
      Object.keys(t.params || {}).forEach((k) => { cur[k] = num(v(`${p}:${k}`), cur[k]); });
      if (t.id === 'wrong_door') {
        const ids = [];
        (s.list.lines || []).forEach((l) => { if (c(`${p}:line:${l.id}`)) ids.push(l.id); });
        if (f.querySelector(`[name^="${p}:line:"]`)) cur.door_line_ids = ids;
      }
      trig[t.id] = cur;
    });
    a.triggers = trig;
    const pois = [];
    f.querySelectorAll('[data-ra-poi]').forEach((pe) => {
      const i = pe.getAttribute('data-ra-poi');
      const p = { name: v(`p${i}:name`) || '' };
      if (v(`p${i}:id`)) p.id = v(`p${i}:id`);
      if (el(`p${i}:camera_id`)) { p.camera_id = v(`p${i}:camera_id`); p.u = num(v(`p${i}:u`), 0.5); p.v = num(v(`p${i}:v`), 0.5); }
      else { p.x = num(v(`p${i}:x`), 0); p.y = num(v(`p${i}:y`), 0); }
      pois.push(p);
    });
    a.points_of_interest = pois;
    if (el('confidence_delta')) a.security = { confidence_delta: Math.min(0.2, Math.max(0, num(v('confidence_delta'), 5) / 100)) };
    return a;
  }

  function payloadOf(a) {
    return {
      name: (a.name || '').trim(), enabled: a.enabled !== false, floor_zone_id: a.floor_zone_id || null,
      camera_regions: (a.camera_regions || []).map((r) => (r.intrusion_zone_id ? { camera_id: r.camera_id, intrusion_zone_id: r.intrusion_zone_id }
        : { camera_id: r.camera_id, points: r.points })),
      arming: a.arming, triggers: a.triggers, points_of_interest: a.points_of_interest, security: a.security,
    };
  }

  async function saveSettings() {
    const s = state.settings;
    if (!s || !s.editable) return;
    const a = readSettings();
    if (!a.name || !a.name.trim()) { setMsg('Give the area a name.', 'error'); return; }
    setMsg('Saving…');
    const r = s.id ? await api(`${API}/${encodeURIComponent(s.id)}`, { method: 'PUT', json: payloadOf(a) })
      : await api(API, { method: 'POST', json: payloadOf(a) });
    if (!r.ok) { setMsg(r.message, 'error'); return; }
    s.area = r.data;
    s.id = r.data.id;
    s.confirmDelete = false;
    renderSettings();
    const warn = (r.data.warnings || []).join(' ');
    setMsg(`Saved.${warn ? ` ${warn}` : ''}`, warn ? 'error' : 'ok');
    toast('Restricted area saved', 'ok');
    load();
  }

  async function settingsAction(kind) {
    const s = state.settings;
    if (!s || !s.id) return;
    if (kind === 'forget') {
      const r = await api(`${API}/${encodeURIComponent(s.id)}/forget-still-figures`, { json: {} });
      setMsg(r.ok ? `Forgotten: ${plural(r.data.forgotten || 0, 'still figure')}.` : r.message, r.ok ? 'ok' : 'error');
      return;
    }
    if (kind === 'delete') {
      if (!s.confirmDelete) { readSettings(); s.confirmDelete = true; renderSettings(); setMsg('Press "Yes, delete it" to delete this area and stop watching it.', 'error'); return; }
      const r = await api(`${API}/${encodeURIComponent(s.id)}`, { method: 'DELETE' });
      if (!r.ok) { setMsg(r.message, 'error'); return; }
      toast('Restricted area deleted', 'ok');
      closeDialog();
      load();
    }
  }

  function onDialogChange(e) {
    const s = state.settings;
    if (!s || !state.dialog || state.dialog.kind !== 'settings') return;
    const n = e.target.name || '';
    if (n === 'arm_mode') {
      readSettings();
      if (s.area.arming.mode === 'schedule' && !s.area.arming.schedule.length) {
        s.area.arming.schedule = [{ days: ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'], from: '18:00', to: '07:00' }];
      }
      renderSettings();
    } else if (n === 'floor_zone_id') {
      readSettings();
      s.area.cameras = [];
      s.zoneCams = null;
      renderSettings();
      if (s.area.floor_zone_id) fetchZoneCameras(s.area.floor_zone_id);
    } else if (/^t_[a-z_]+:enabled$/.test(n)) {
      const fs = e.target.closest('.ra-trigger');
      if (fs) fs.classList.toggle('is-on', e.target.checked);
    }
  }

  function onDialogInput() { /* drafts are read on save; nothing to do per keystroke */ }

  // ------------------------------------------------------------ events: outcome + evidence

  async function saveOutcome(id, form) {
    const picked = form.querySelector('input[name="outcome"]:checked');
    if (!picked) { state.errors[id] = 'Choose what happened first.'; renderCard(); return; }
    const note = ((form.elements.namedItem('note') || {}).value || '').trim();
    state.busy.add(id);
    delete state.errors[id];
    renderCard();
    const r = await api(`${API}/events/${encodeURIComponent(id)}/outcome`, { json: { outcome: picked.value, note: note || null } });
    state.busy.delete(id);
    if (!r.ok) { state.errors[id] = r.message; renderCard(); return; }
    state.outcomeFor = null;
    toast(`Outcome saved: ${r.data.outcome_label}`, 'ok');
    await loadEvents();
    await load();
    if (window.edgeZoneAlerts && typeof window.edgeZoneAlerts.refresh === 'function') window.edgeZoneAlerts.refresh();
  }

  function openEvidence(id, mode) {
    const e = (state.events || []).find((x) => x.id === id);
    if (!e) return;
    const caption = `${e.trigger_label || 'Restricted area alert'}: ${e.area_name || ''} · ${e.camera_name || ''} · ${e.store_time ? e.store_time.label : ''}`;
    if (window.edgeLoss && typeof window.edgeLoss.openEvidence === 'function') {
      window.edgeLoss.openEvidence(e.snapshot_url ? authUrl(e.snapshot_url) : null, caption, e.clip_url ? authUrl(e.clip_url) : null, mode, 'restricted area alert for staff to check');
    } else {
      window.open(authUrl(mode === 'clip' ? e.clip_url : e.snapshot_url), '_blank', 'noopener');
    }
  }

  /** Show one alert in the card with its outcome form open (banner "Review now", zone alerts). */
  async function focusEvent(id) {
    if (typeof switchTab === 'function') switchTab('loss');
    let ev = (state.events || []).find((x) => x.id === id);
    if (!ev) {
      state.filters = { area: '', trigger: '', needs: 'all' };
      await loadEvents();
      ev = (state.events || []).find((x) => x.id === id);
    }
    if (ev && ev.needs_outcome) state.outcomeFor = id;
    renderCard();
    setTimeout(() => {
      const node = document.getElementById(`ra-event-${id}`) || $('restrictedAreasCard');
      if (!node) return;
      node.scrollIntoView({ behavior: 'instant', block: 'center' });
      node.classList.remove('loss-flash');
      void node.offsetWidth;
      node.classList.add('loss-flash');
      const radio = node.querySelector('input[type=radio]');
      if (radio) radio.focus({ preventScroll: true });
    }, 80);
    return !!ev;
  }

  // ------------------------------------------------------------ clicks

  function onClick(e) {
    const b = e.target.closest('[data-ra]');
    if (!b) return;
    const act = b.getAttribute('data-ra');
    const area = b.getAttribute('data-ra-area');
    const evId = b.getAttribute('data-ra-event');
    switch (act) {
      case 'announce': openAnnounce(area); break;
      case 'out': endAnnouncement(b.getAttribute('data-ra-ann'), b); break;
      case 'arm': case 'disarm': case 'resume': openArm(act, area); break;
      case 'test': openTest(area || (state.settings && state.settings.id)); break;
      case 'settings': openSettings(area); break;
      case 'new': openSettings(null); break;
      case 'open': open(); break;
      case 'refresh': load().then(() => toast('Restricted areas refreshed', 'ok')); break;
      case 'close': closeDialog(); break;
      case 'filter-needs': state.filters.needs = b.getAttribute('data-ra-value'); state.events = null; renderCard(); loadEvents().then(renderCard); break;
      case 'outcome': state.outcomeFor = evId; renderCard(); {
        const r = document.querySelector(`#ra-event-${evId.replace(/[^\w-]/g, '')} input[type=radio]`);
        if (r) r.focus();
      } break;
      case 'outcome-cancel': state.outcomeFor = null; renderCard(); break;
      case 'still': openEvidence(evId, 'still'); break;
      case 'clip': openEvidence(evId, 'clip'); break;
      case 'sched-add': readSettings(); state.settings.area.arming.schedule.push({ days: ['mon', 'tue', 'wed', 'thu', 'fri'], from: '18:00', to: '07:00' }); renderSettings(); break;
      case 'sched-del': readSettings(); state.settings.area.arming.schedule.splice(Number(b.getAttribute('data-ra-row')), 1); renderSettings(); break;
      case 'poi-add': readSettings(); state.settings.area.points_of_interest.push(state.settings.area.floor_zone_id ? { name: '', x: 0, y: 0 }
        : { name: '', camera_id: ((state.settings.area.camera_regions || [])[0] || {}).camera_id || ((state.settings.list.cameras || [])[0] || {}).id, u: 0.5, v: 0.5 }); renderSettings(); break;
      case 'poi-del': readSettings(); state.settings.area.points_of_interest.splice(Number(b.getAttribute('data-ra-row')), 1); renderSettings(); break;
      case 'draw': {
        const sel = $('raDrawCam');
        if (sel && sel.value) window.location.href = `/dashboard/studio?${new URLSearchParams({ from: 'restricted', tool: 'restricted', camera_id: sel.value }).toString()}`;
        break;
      }
      case 'forget': case 'delete': settingsAction(act); break;
      default: break;
    }
  }

  function onCardChange(e) {
    const f = e.target.getAttribute && e.target.getAttribute('data-ra-filter');
    if (!f) return;
    state.filters[f] = e.target.value;
    state.events = null;
    renderCard();
    loadEvents().then(renderCard);
  }

  function onCardSubmit(e) {
    const form = e.target.closest && e.target.closest('[data-ra-outcome-form]');
    if (!form) return;
    e.preventDefault();
    saveOutcome(form.getAttribute('data-ra-outcome-form'), form);
  }

  // ------------------------------------------------------------ instant alerts (websocket)

  function connectWs() {
    if (state.ws || planOff() || typeof WebSocket === 'undefined') return;
    let ws;
    try {
      ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/api/v1/events/ws`);
    } catch (_) { scheduleWs(); return; }
    state.ws = ws;
    ws.onopen = () => { state.wsTry = 0; };
    ws.onmessage = (m) => {
      let msg = null;
      try { msg = JSON.parse(m.data); } catch (_) { return; }
      if (!msg || !msg.ra_area_id) return;
      const za = window.edgeZoneAlerts;
      if (za && typeof za.ingest === 'function') za.ingest(wsEntry(msg));
      state.eventsSig = null;
      load();
    };
    ws.onclose = (ev) => {
      state.ws = null;
      if (ev && ev.code === 1008) { state.wsTry = Math.max(state.wsTry, 4); }   // not signed in: retry slowly
      scheduleWs();
    };
    ws.onerror = () => { try { ws.close(); } catch (_) { /* closing */ } };
  }

  function scheduleWs() {
    clearTimeout(state.wsTimer);
    if (planOff()) return;
    const wait = Math.min(60000, 1000 * Math.pow(2, state.wsTry));
    state.wsTry += 1;
    state.wsTimer = setTimeout(connectWs, wait);
  }

  // ------------------------------------------------------------ start

  function schedule() {
    clearTimeout(state.timer);
    state.timer = setTimeout(async () => {
      if (!planOff()) await load();
      schedule();
    }, document.hidden ? POLL_HIDDEN_MS : POLL_VISIBLE_MS);
  }

  function open(areaId) {
    if (typeof switchTab === 'function') switchTab('loss');
    setTimeout(() => {
      const node = (areaId && document.getElementById(`ra-area-${areaId}`)) || $('restrictedAreasCard');
      if (!node) return;
      node.scrollIntoView({ behavior: 'instant', block: 'start' });
      node.classList.remove('flash-highlight');
      void node.offsetWidth;
      node.classList.add('flash-highlight');
      setTimeout(() => node.classList.remove('flash-highlight'), 1600);
    }, 150);
  }

  function start() {
    if (state.started) return;
    state.started = true;
    loadCatalogue().then(() => renderCard());
    load();
    schedule();
    connectWs();
  }

  function init() {
    const card = $('restrictedAreasCard');
    if (card) {
      card.addEventListener('click', onClick);
      card.addEventListener('change', onCardChange);
      card.addEventListener('submit', onCardSubmit);
      const host = $('raCardBody');
      if (host) {
        host.addEventListener('toggle', (e) => { if (e.target && e.target.id === 'raAudit' && e.target.open) loadAudit(); }, true);
      }
    }
    ['raStrip', 'settings-restricted-areas'].forEach((id) => { const n = $(id); if (n) n.addEventListener('click', onClick); });
    // Settings > "Restricted areas" jump link: scroll there without changing the page address.
    document.addEventListener('click', (e) => {
      const j = e.target.closest && e.target.closest('[data-ra-jump]');
      if (!j) return;
      e.preventDefault();
      const node = $(j.getAttribute('data-ra-jump'));
      if (node) node.scrollIntoView({ behavior: 'instant', block: 'start' });
    });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && $('raDialog') && !$('raDialog').hidden) closeDialog(); });
    window.addEventListener('fp:restricted-settings', (e) => {
      const z = e && e.detail && e.detail.zone_id;
      openSettings(z ? { zone_id: z } : null);
    });
    document.addEventListener('visibilitychange', () => { if (!document.hidden && state.started) { load(); schedule(); } });
    window.addEventListener('edge:account', () => render());
    state.tick = setInterval(tickCountdowns, 1000);
    const canPollNow = typeof canPoll === 'function' ? canPoll() : true;
    if (!canPollNow) return;       // signed out: the gate reloads the page after sign-in
    const fleetKnown = !!(window.edgeFleet && typeof window.edgeFleet.status === 'function' && window.edgeFleet.status());
    window.addEventListener('edge:fleet', () => {
      if (planOff()) { render(); return; }
      start();
      connectWs();
    });
    if (fleetKnown || !window.edgeFleet) start();
    else setTimeout(() => { if (!planOff()) start(); }, 3000);
  }

  window.edgeRestricted = {
    open,
    openSettings: (arg) => openSettings(arg || null),
    refresh: () => load(),
    focusEvent,
    announce: (areaId) => openAnnounce(areaId || null),
    status: () => state.status,
    _test: { leftText, stateLabel, occupancyText, sourceText, wsEntry, payloadOf, blankArea, state },
  };

  if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') {
    window.edgeAuth.onReady(init);
  } else if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
