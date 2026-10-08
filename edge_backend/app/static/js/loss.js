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
 * The Patterns card renders GET /api/v1/theft/patterns (hotspots, day x hour,
 * checks that fired with their false-alarm rate, weekly trend, bursts at one
 * place). It shows only recorded incidents and never claims who a person is.
 * The evidence viewer plays the incident's clip when the camera saved one.
 *
 * Alert levels (services/theft_alert_policy.py): every incident recorded since
 * levels exist carries alert_tier (review | watch | alert | critical), a
 * risk_score 0..1, the risk_factors with a reason each ("Why this level") and
 * alert_channels {queue, banner, sound, push, escalate, repeat}. The banner is
 * raised only for incidents whose channels say banner, and the alarm sound
 * plays only for those whose channels say sound. Older incidents have no level
 * (alert_tier null, channels null): they are labelled "No level" (never
 * re-scored here) and keep the old behaviour (banner and sound while ACTIVE).
 * The level chips filter the queue with GET /theft/incidents?tier=<level>.
 *
 * Area & line alerts (zone_alerts.js, window.edgeZoneAlerts) share the banner,
 * the nav badge and the evidence viewer: an unacknowledged restricted-area or
 * line-crossing alert raises the banner as a prompt to check, never as a
 * theft finding, and counts in the badge.
 *
 * Uses globals from analytics.js: DASH, escapeHtml, isNum, getJSON, showToast,
 * formatTimestamp, formatAgo, theftAuthUrl, switchTab. No prompt/confirm/alert.
 */
(function () {
  'use strict';

  /** Icon markup from js/icons.js (window.EdgeIcon); '' when that script is missing. Never throws. */
  const ico = (name, opts) => {
    try { return window.EdgeIcon && typeof window.EdgeIcon.svg === 'function' ? window.EdgeIcon.svg(name, opts) : ''; } catch (_) { return ''; }
  };

  const API = '/api/v1/theft';
  const OPEN = ['ACTIVE', 'ACKNOWLEDGED', 'DISPATCHED'];
  const HANDLED = ['RESOLVED', 'FALSE_ALARM'];
  const POLL_MS = 5000;
  const PATTERNS_MS = 60000;      // patterns change slowly: refreshed once a minute

  const D = (typeof DASH !== 'undefined') ? DASH : '—';
  const esc = (v) => (typeof escapeHtml === 'function' ? escapeHtml(v)
    : String(v === null || v === undefined ? '' : v).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const num = (v) => typeof v === 'number' && Number.isFinite(v);
  const $ = (id) => document.getElementById(id);
  const toast = (msg, kind) => { if (typeof showToast === 'function') showToast(msg, kind); };
  const authUrl = (u) => (typeof theftAuthUrl === 'function' ? theftAuthUrl(u) : u);
  const money = (v) => `$${Number(v).toFixed(2)}`;

  // ------------------------------------------------------------ alert levels
  const TIERS = ['review', 'watch', 'alert', 'critical'];          // ascending
  const TIER_LABEL = { review: 'Review', watch: 'Watch', alert: 'Alert', critical: 'Critical' };
  const TIER_ICON = { review: 'info', watch: 'eye', alert: 'bell', critical: 'siren' };
  const TIER_CHIPS = ['all', 'critical', 'alert', 'watch', 'review'];
  const UNCLASSIFIED = 'unclassified';
  /** The incident's level, or null for rows recorded before levels existed. */
  const tierOf = (inc) => (inc && TIERS.includes(inc.alert_tier) ? inc.alert_tier : null);
  /** Rank for sorting: critical 3 .. review 0; no level -1. */
  const tierRank = (inc) => TIERS.indexOf(tierOf(inc));
  const channelsOf = (inc) => (inc && inc.alert_channels && typeof inc.alert_channels === 'object' ? inc.alert_channels : null);
  /** Banner: only when the incident's channels say so; no level (null channels) keeps the old behaviour. */
  function bannerAllowed(inc) {
    const ch = channelsOf(inc);
    return ch ? ch.banner === true : true;
  }
  /** Sound: only when the incident's channels say so; no level (null channels) keeps the old behaviour. */
  function soundAllowed(inc) {
    const ch = channelsOf(inc);
    return ch ? ch.sound === true : true;
  }
  function tierBadge(inc, extraCls) {
    const t = tierOf(inc);
    const cls = extraCls ? ` ${extraCls}` : '';
    if (!t) {
      return `<span class="tier-badge loss-tier-none${cls}" title="Recorded before alert levels existed. It was not re-scored, so it has no level.">No level</span>`;
    }
    const label = inc.alert_tier_label || TIER_LABEL[t];
    return `<span class="tier-badge tier-${t}${cls}" title="Alert level: ${esc(label)}">${ico(TIER_ICON[t], { size: 'sm' })}${esc(label)}</span>`;
  }
  const riskText = (inc) => (num(inc && inc.risk_score) ? `risk ${Math.round(inc.risk_score * 100)}%` : null);

  const state = {
    incidents: [],
    stats: null,
    outcomes: [],
    loaded: false,
    unavailable: false,
    filter: 'open',
    tierFilter: 'all',      // 'all' | review | watch | alert | critical | unclassified
    tierIncidents: null,    // GET /incidents?tier=<tierFilter> (null until loaded)
    tierFailed: false,
    heard: new Set(),       // ACTIVE incident ids already considered for the alarm sound
    form: null,             // {id, kind: 'resolve'|'dispatch'}
    busy: false,
    listSig: null,
    lastAlertedId: null,
    dismissedBannerId: null,
    timer: null,
    patterns: null,
    patternDays: 28,
    patternsAt: 0,
    patternsFailed: false,
    patternsBusy: false,
    viewer: null,           // {still, clip, caption, mode}
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

  /** The dashboard alarm: one falling tone; a critical incident sounds it three times. */
  function playSound(urgent) {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const beeps = urgent ? 3 : 1;
      for (let i = 0; i < beeps; i += 1) {
        const t0 = ctx.currentTime + i * 0.45;
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = 'sawtooth';
        osc.frequency.setValueAtTime(880, t0);
        osc.frequency.exponentialRampToValueAtTime(440, t0 + 0.3);
        gain.gain.setValueAtTime(0.15, t0);
        gain.gain.exponentialRampToValueAtTime(0.01, t0 + 0.3);
        osc.connect(gain); gain.connect(ctx.destination);
        osc.start(t0); osc.stop(t0 + 0.35);
      }
    } catch (_) { /* audio blocked until the page is interacted with */ }
  }

  function tabVisible() {
    const t = $('tab-theft');
    return !!t && t.classList.contains('active') && !document.hidden;
  }

  // ------------------------------------------------------------ data

  async function load(force) {
    const get = (url) => (typeof getJSON === 'function' ? getJSON(url, null) : fetch(url).then((r) => (r.ok ? r.json() : null)).catch(() => null));
    const tf = state.tierFilter;
    const [data, stats, tierData] = await Promise.all([
      get(`${API}/incidents?limit=200`), get(`${API}/statistics`),
      tf !== 'all' ? get(tierUrl(tf)) : Promise.resolve(null),
    ]);
    if (tf !== 'all' && tf === state.tierFilter) {
      state.tierFailed = !tierData;
      if (tierData) state.tierIncidents = sortByTime(tierData.incidents || []);
    }
    if (!data && !stats && !state.loaded) {
      // Not signed in yet or the service did not answer.
      state.unavailable = true;
    }
    if (data) {
      state.incidents = sortByTime(data.incidents || []);
      state.unavailable = false;
      state.loaded = true;
    } else if (state.loaded) {
      state.unavailable = true;
    }
    if (stats) state.stats = stats;
    renderBadge();
    ringForNew();
    renderBanner();
    renderKpis();
    renderTierKpis();
    renderTierChips();
    renderRuleTable();
    renderList(force);
    // Patterns only while someone looks at the page (or on an explicit refresh).
    if (force || (tabVisible() && Date.now() - state.patternsAt > PATTERNS_MS)) loadPatterns();
  }

  /** Newest first. */
  function sortByTime(list) {
    return list.slice().sort((a, b) => {
      const da = toDate(a.timestamp), db = toDate(b.timestamp);
      return (db ? db.getTime() : 0) - (da ? da.getTime() : 0);
    });
  }

  /** The review queue of one alert level: the server filters (?tier=), so it is not limited to the newest 200 of all. */
  const tierUrl = (tier) => `${API}/incidents?limit=200&tier=${encodeURIComponent(tier)}`;

  async function loadTier() {
    const tf = state.tierFilter;
    if (tf === 'all') return;
    const data = typeof getJSON === 'function'
      ? await getJSON(tierUrl(tf), null)
      : await fetch(tierUrl(tf)).then((r) => (r.ok ? r.json() : null)).catch(() => null);
    if (tf !== state.tierFilter) return;              // the level changed meanwhile
    state.tierFailed = !data;
    if (data) state.tierIncidents = sortByTime(data.incidents || []);
    renderList(true);
  }

  async function loadPatterns() {
    if (state.patternsBusy) return;
    state.patternsBusy = true;
    const days = state.patternDays;
    try {
      const data = typeof getJSON === 'function'
        ? await getJSON(`${API}/patterns?days=${days}`, null)
        : await fetch(`${API}/patterns?days=${days}`).then((r) => (r.ok ? r.json() : null)).catch(() => null);
      if (days !== state.patternDays) return;          // the period changed meanwhile
      state.patternsAt = Date.now();
      state.patternsFailed = !data;
      if (data) state.patterns = data;
    } finally {
      state.patternsBusy = false;
    }
    renderPatterns();
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

  const zoneAlerts = () => (window.edgeZoneAlerts && typeof window.edgeZoneAlerts.pending === 'function'
    ? window.edgeZoneAlerts : null);

  function renderBadge() {
    const b = $('tabTheftCountBadge');
    if (!b) return;
    const theft = openCount();
    const za = zoneAlerts();
    const zones = za ? za.pendingCount() : 0;
    const n = theft + zones;
    b.textContent = String(n);
    b.hidden = !n;
    const parts = [`${theft} incident${theft === 1 ? '' : 's'} waiting for review`];
    if (zones) parts.push(`${zones} area or line alert${zones === 1 ? '' : 's'} not acknowledged`);
    b.title = parts.join(', ');
  }

  const timeOf = (x) => { const d = x && toDate(x.timestamp); return d ? d.getTime() : 0; };
  /** Banner rank: an incident with no level keeps the old behaviour and ranks with Alert. */
  const bannerRank = (inc) => (tierOf(inc) ? tierRank(inc) : TIERS.indexOf('alert'));

  /**
   * What the banner shows: among ACTIVE incidents whose channels allow a banner
   * (bannerAllowed), the highest level, newest first; else the newest
   * unacknowledged area/line alert when that is newer (a Critical incident is
   * never pushed aside by an area alert). Review-level incidents never qualify.
   */
  function bannerItem() {
    const active = state.incidents.filter((i) => i.status === 'ACTIVE' && bannerAllowed(i))
      .sort((a, b) => (bannerRank(b) - bannerRank(a)) || (timeOf(b) - timeOf(a)))[0] || null;
    const za = zoneAlerts();
    const zone = za ? za.pending()[0] : null;
    if (zone && (!active || (tierOf(active) !== 'critical' && timeOf(zone) > timeOf(active)))) return { kind: 'zone', id: zone.id, item: zone };
    return active ? { kind: 'theft', id: active.id, item: active } : null;
  }

  /**
   * The alarm sound, once per new item: every ACTIVE incident not heard before
   * whose channels allow sound (soundAllowed; a Watch incident is silent unless
   * the store switched its sound on; Review never sounds), and a new
   * unacknowledged area/line alert (as before). A new Critical sounds three times.
   */
  function ringForNew() {
    if (!state.loaded) return;
    const fresh = state.incidents.filter((i) => i.status === 'ACTIVE' && !state.heard.has(i.id));
    fresh.forEach((i) => state.heard.add(i.id));
    const ring = fresh.filter(soundAllowed);
    const za = zoneAlerts();
    const zone = za ? za.pending()[0] : null;
    let zoneRing = false;
    if (zone && !state.heard.has(`zone:${zone.id}`)) {
      state.heard.add(`zone:${zone.id}`);
      zoneRing = true;
    }
    if (state.heard.size > 2000) {
      // Forget ids no longer in the queue so the set stays small.
      const keep = new Set(state.incidents.map((i) => i.id));
      state.heard.forEach((id) => { if (!keep.has(id) && !String(id).startsWith('zone:')) state.heard.delete(id); });
    }
    if (ring.length || zoneRing) playSound(ring.some((i) => tierOf(i) === 'critical'));
  }

  const BANNER_TIER_CLASSES = ['theft-banner-critical', 'theft-banner-alert', 'theft-banner-watch'];

  function renderBanner() {
    const banner = $('theftAlertBanner');
    if (!banner) return;
    const pick = bannerItem();
    if (!pick) { banner.style.display = 'none'; return; }
    state.lastAlertedId = pick.id;
    if (state.dismissedBannerId === pick.id) { banner.style.display = 'none'; return; }
    // Banner look by level: Critical solid danger, Alert danger outline, Watch info; no level and
    // area/line alerts keep the old look.
    const tier = pick.kind === 'theft' ? tierOf(pick.item) : null;
    const look = tier && tier !== 'review' ? `theft-banner-${tier}` : '';
    BANNER_TIER_CLASSES.forEach((c) => banner.classList.toggle(c, c === look));
    banner.dataset.tier = tier || '';
    const iconHost = banner.querySelector('.theft-banner-icon');
    if (iconHost) {
      const name = tier ? TIER_ICON[tier] : 'siren';
      const svg = iconHost.dataset.icon !== name ? ico(name, { size: 'lg' }) : '';
      if (svg) { iconHost.dataset.icon = name; iconHost.innerHTML = svg; }
    }
    const set = (id, t) => { const n = $(id); if (n) n.textContent = t; };
    const it = pick.item;
    if (pick.kind === 'zone') {
      const restricted = it.event_type === 'RESTRICTED_AREA';
      set('theftBannerHeadline', zoneAlerts().bannerHeadline(it));
      set('theftBannerConfidence', restricted ? 'Restricted area alert' : 'Line alert');
      set('theftBannerTime', when(it.timestamp));
      set('theftBannerDetails', restricted
        ? 'Someone is in an area set as restricted at this time. A prompt to check, not a theft finding.'
        : 'A line set to alert staff was crossed. A prompt to check, not a theft finding.');
    } else {
      const lvl = tier ? (it.alert_tier_label || TIER_LABEL[tier]) : null;
      set('theftBannerHeadline', `${lvl ? `${lvl} · ` : ''}Please check: ${ruleLabel(it)} on ${it.camera_name || it.camera_id}`);
      const conf = num(it.confidence) ? `Confidence ${Math.round(it.confidence * 100)}%` : `Confidence ${D}`;
      set('theftBannerConfidence', lvl && num(it.risk_score) ? `Risk ${Math.round(it.risk_score * 100)}% · ${conf}` : conf);
      set('theftBannerTime', when(it.timestamp));
      set('theftBannerDetails', 'Suspicious behaviour for staff review, not a finding of theft.');
    }
    banner.dataset.incident = pick.id;
    banner.dataset.kind = pick.kind;
    // "Watch live": that camera enlarged on the Cameras tab, the person outlined while still tracked.
    banner.dataset.camera = it.camera_id || '';
    banner.dataset.track = (pick.kind === 'theft' && it.person_track_id) || '';
    const live = banner.querySelector('[data-banner="live"]');
    const canLive = !!it.camera_id && typeof window.openCameraLive === 'function';
    if (live) live.hidden = !canLive;
    const left = banner.querySelector('.theft-banner-left');
    if (left) {
      left.classList.toggle('theft-banner-live', canLive);
      left.title = canLive ? 'Click to watch this camera live' : '';
    }
    banner.style.display = 'flex';
  }

  function bannerLive() {
    const banner = $('theftAlertBanner');
    const cam = banner && banner.dataset.camera;
    if (!cam || typeof window.openCameraLive !== 'function') return;
    window.openCameraLive(cam, { trackId: banner.dataset.track || null });
  }

  function bannerReview() {
    const banner = $('theftAlertBanner');
    const id = banner && banner.dataset.incident;
    const kind = banner && banner.dataset.kind;
    if (typeof switchTab === 'function') switchTab('loss');
    if (!id) return;
    setTimeout(() => {
      const za = zoneAlerts();
      if (kind === 'zone' && za) za.focus(id);
      else focusIncident(id);
    }, 60);
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
          <button type="button" class="btn btn-secondary btn-sm" data-filter="legacy">Record their outcomes</button>`;
      } else {
        legacy.hidden = true;
        legacy.innerHTML = '';
      }
    }
  }

  /**
   * One KPI per level (#lossTierKpis), from GET /theft/statistics: waiting now
   * (active_by_tier), flagged today (today_by_tier), in total (by_tier) and the
   * false-alarm rate of the reviewed ones (false_alarm_rate_by_tier). Incidents
   * recorded before levels are counted apart, never folded into a level.
   */
  function renderTierKpis() {
    const host = $('lossTierKpis');
    if (!host) return;
    const s = state.stats;
    if (!s || !s.by_tier || typeof s.by_tier !== 'object') {
      host.innerHTML = `<div class="fp-empty">${s ? 'This box does not report incidents by alert level yet.' : 'Statistics unavailable right now.'}</div>`;
      return;
    }
    const n = (o, k) => (o && num(o[k]) ? o[k] : 0);
    const html = TIERS.slice().reverse().map((t) => {
      const waiting = n(s.active_by_tier, t);
      const rate = s.false_alarm_rate_by_tier ? s.false_alarm_rate_by_tier[t] : null;
      const fa = num(rate) ? `${Math.round(rate * 100)}% false alarms among reviewed` : 'no outcomes recorded yet';
      return `<div class="kpi loss-tier-kpi" data-tier-kpi="${t}">
          <span class="kpi-label">${tierBadge({ alert_tier: t })}</span>
          <span class="kpi-value">${waiting}</span>
          <span class="kpi-delta">waiting · ${n(s.today_by_tier, t)} today · ${n(s.by_tier, t)} in total</span>
          <span class="kpi-delta">${esc(fa)}</span>
        </div>`;
    }).join('');
    const old = n(s.by_tier, UNCLASSIFIED);
    const note = old
      ? `<div class="loss-kpi-note loss-tier-note">${old} older incident${old === 1 ? ' was' : 's were'} recorded before alert levels and ${old === 1 ? 'has' : 'have'} no level (${n(s.active_by_tier, UNCLASSIFIED)} waiting). They are not re-scored.</div>`
      : '';
    host.innerHTML = html + note;
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

  /** An incident by id, from the queue or the level view. */
  function findInc(id) {
    return state.incidents.find((i) => i.id === id) || (state.tierIncidents || []).find((i) => i.id === id) || null;
  }

  /** The incidents of the chosen level (server-filtered), or every incident. */
  function source() {
    return state.tierFilter === 'all' ? state.incidents : (state.tierIncidents || []);
  }

  function filtered() {
    const f = state.filter;
    const list = source().filter((i) => {
      if (f === 'open') return isOpen(i);
      if (f === 'handled') return HANDLED.includes(i.status);
      if (f === 'legacy') return isLegacy(i);
      return true;
    });
    // Waiting for review: Critical first, then Alert, Watch, Review, no level; newest first within each.
    if (f === 'open') list.sort((a, b) => (tierRank(b) - tierRank(a)) || (timeOf(b) - timeOf(a)));
    return list;
  }

  /** Level chips: counts from GET /theft/statistics for the chosen status (open = waiting, else all). */
  function renderTierChips() {
    const host = $('lossTierChips');
    if (!host) return;
    const s = state.stats || {};
    const by = s.by_tier && typeof s.by_tier === 'object' ? s.by_tier : null;
    const open = state.filter === 'open';
    const src = open ? s.active_by_tier : by;
    const count = (t) => {
      if (!src) return null;
      if (t === 'all') return Object.values(src).reduce((a, v) => a + (num(v) ? v : 0), 0);
      return num(src[t]) ? src[t] : 0;
    };
    const chips = TIER_CHIPS.slice();
    if ((by && num(by[UNCLASSIFIED]) && by[UNCLASSIFIED] > 0) || state.tierFilter === UNCLASSIFIED) chips.push(UNCLASSIFIED);
    const label = (t) => (t === 'all' ? 'All levels' : t === UNCLASSIFIED ? 'No level' : TIER_LABEL[t]);
    const what = open ? 'waiting for review' : 'recorded';
    host.innerHTML = `<span class="loss-chips-label">Level</span>${chips.map((t) => {
      const n = count(t);
      const on = state.tierFilter === t;
      const dot = TIERS.includes(t) ? `<span class="loss-chip-dot loss-dot-${t}" aria-hidden="true"></span>` : '';
      return `<button type="button" class="loss-chip ${on ? 'active' : ''}" data-tier-filter="${t}" aria-pressed="${on}"
          title="${esc(`${label(t)}: ${n === null ? 'count not available' : `${n} ${what}`}`)}">${dot}${esc(label(t))}${n === null ? '' : ` <span class="loss-chip-n">${n}</span>`}</button>`;
    }).join('')}`;
  }

  function renderChips() {
    const host = $('lossFilterChips');
    if (!host) return;
    const src = source();
    const counts = {
      open: src.filter(isOpen).length,
      handled: src.filter((i) => HANDLED.includes(i.status)).length,
      all: src.length,
      legacy: src.filter(isLegacy).length,
    };
    const chips = [['open', 'Needs review'], ['handled', 'Handled'], ['all', 'All']];
    if (counts.legacy || state.filter === 'legacy') chips.push(['legacy', 'No outcome recorded']);
    host.innerHTML = chips.map(([k, label]) => `
      <button type="button" class="loss-chip ${state.filter === k ? 'active' : ''}" data-filter="${k}" aria-pressed="${state.filter === k}">
        ${esc(label)} <span class="loss-chip-n">${counts[k]}</span></button>`).join('');
    renderTierChips();
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
    if (inc.status === 'ACTIVE') b.push(`<button type="button" class="btn btn-secondary btn-sm" data-action="acknowledge" data-incident="${id}" title="Tell other staff you are looking at it">I'm checking</button>`);
    if (inc.status === 'ACTIVE' || inc.status === 'ACKNOWLEDGED') b.push(`<button type="button" class="btn btn-sm btn-secondary" data-action="dispatch" data-incident="${id}">Mark: staff sent</button>`);
    if (isOpen(inc)) {
      b.push(`<button type="button" class="btn btn-sm btn-primary" data-action="resolve" data-incident="${id}">Record outcome</button>`);
      b.push(`<button type="button" class="btn btn-secondary btn-sm" data-action="false-alarm" data-incident="${id}" title="Nothing suspicious happened">False alarm</button>`);
    } else if (isLegacy(inc)) {
      b.push(`<button type="button" class="btn btn-sm btn-primary" data-action="resolve" data-incident="${id}">Record outcome</button>`);
    } else {
      b.push(`<button type="button" class="btn btn-secondary btn-sm" data-action="resolve" data-incident="${id}" title="Correct the recorded outcome">Change outcome</button>`);
    }
    if (inc.clip_url) b.push(`<button type="button" class="btn btn-secondary btn-sm" data-action="play-clip" data-incident="${id}" title="About 5 s before to 10 s after">${ico('play', { size: 'sm' })} Play clip</button>`);
    if (isOpen(inc) && inc.camera_id && typeof window.openCameraLive === 'function') {
      b.push(`<button type="button" class="btn btn-secondary btn-sm" data-action="watch-live" data-incident="${id}" title="This camera's live video, enlarged, with the person outlined while the box still tracks them">Watch live</button>`);
    }
    if (inc.studio_url) b.push(`<a class="btn btn-secondary btn-sm" href="${esc(inc.studio_url)}" target="_blank" rel="noopener">Watch camera now</a>`);
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
    const clipTag = inc.clip_url ? '<span class="badge loss-small-badge loss-clip-badge" title="A short clip was saved with this incident">clip</span>' : '';
    const risk = riskText(inc);
    return `
      <div class="incident-head">
        <div class="loss-head-main">
          ${tierBadge(inc)}
          <span class="loss-rule">${esc(ruleLabel(inc))}</span>
          <span class="badge badge-neutral loss-small-badge">${esc(inc.camera_name || inc.camera_id)}</span>
          ${inc.department ? `<span class="badge loss-small-badge">${esc(inc.department)}</span>` : ''}
          ${clipTag}
        </div>
        <div class="loss-head-side">
          ${risk ? `<span class="loss-risk" title="Risk score: confidence x where it happened x what kind of behaviour, plus combined or repeated signs. It sets the alert level.">${esc(risk)}</span>` : ''}
          <span class="loss-conf" title="How clearly the camera saw it: keypoint visibility, duration and how many signals agreed">confidence ${conf}</span>
          ${statusPill(inc)}
        </div>
      </div>
      <div class="loss-body">
        ${thumb}
        <div class="loss-evidence">
          <div class="loss-evidence-label">What the camera saw</div>
          ${plain.length ? `<ul class="loss-evidence-list">${plain.map((e) => `<li>${esc(e)}</li>`).join('')}</ul>` : '<div class="fp-empty">No plain-language evidence recorded.</div>'}
          ${whyHtml(inc)}
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

  /** "Why this level": the recorded risk factors, each with its reason. Nothing for rows without a level. */
  function whyHtml(inc) {
    const t = tierOf(inc);
    if (!t) return '';
    const factors = Array.isArray(inc.risk_factors) ? inc.risk_factors.filter((f) => f && (f.reason || f.factor)) : [];
    const ch = channelsOf(inc);
    const told = ch ? [ch.banner && 'dashboard banner', ch.sound && 'alarm sound', ch.push && 'phones',
      ch.escalate && 'escalation to backup people', ch.repeat && 'repeated until acknowledged'].filter(Boolean) : [];
    const route = ch ? `<div class="loss-why-route">At this level: ${told.length ? esc(told.join(', ')) : 'review list only, nobody interrupted'}.</div>` : '';
    const items = factors.map((f) => `<li class="loss-why-item">
        ${f.effect ? `<span class="loss-why-effect">${esc(f.effect)}</span>` : ''}
        <span class="loss-why-reason">${esc(f.reason || f.factor)}</span></li>`).join('');
    return `<details class="loss-why">
        <summary>Why this level${num(inc.risk_score) ? ` (risk ${Math.round(inc.risk_score * 100)}%)` : ''}</summary>
        ${items ? `<ul class="loss-why-list">${items}</ul>` : '<div class="loss-why-route">No reasons were recorded for this level.</div>'}
        ${route}
      </details>`;
  }

  function formatWhen(ts) {
    const full = typeof formatTimestamp === 'function' ? formatTimestamp(ts) : String(ts || D);
    const ago = typeof formatAgo === 'function' ? formatAgo(ts) : '';
    return ago ? `${full} (${ago})` : full;
  }

  function listSignature() {
    return JSON.stringify([state.filter, state.tierFilter, state.tierFailed, state.unavailable, filtered().map((i) => [i.id, i.status, i.alert_tier, i.risk_score, i.outcome, i.resolved_by,
      i.recovered_value, i.notes, i.confidence, i.snapshot_url, i.clip_url || '', (i.evidence || []).length, i.dispatch_details ? JSON.stringify(i.dispatch_details) : ''])]);
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
    if (state.tierFilter !== 'all' && !state.tierIncidents) {
      host.innerHTML = `<div class="fp-empty">${state.tierFailed ? 'Incidents of this level could not be loaded. It retries every few seconds.' : 'Loading…'}</div>`;
      return;
    }
    const list = filtered();
    if (!list.length && state.tierFilter !== 'all') {
      const lvl = state.tierFilter === UNCLASSIFIED ? 'without a level' : `at ${TIER_LABEL[state.tierFilter]} level`;
      host.innerHTML = `<div class="fp-empty">${esc(`No incidents ${lvl} in this view. Choose All levels to see the rest.`)}</div>`;
      return;
    }
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
    // Waiting for review is grouped by level (Critical first); the other views by day.
    const groupOf = state.filter === 'open'
      ? (inc) => (tierOf(inc) ? `${TIER_LABEL[tierOf(inc)]} level` : 'No level (recorded before levels)')
      : (inc) => dayLabel(inc.timestamp);
    list.forEach((inc) => {
      const day = groupOf(inc);
      if (day !== lastDay) {
        parts.push(`<div class="loss-day">${esc(day)}</div>`);
        lastDay = day;
      }
      const lvl = tierOf(inc);
      parts.push(`<div class="incident-card loss-card ${isOpen(inc) ? 'loss-card-open' : ''} ${isOpen(inc) && lvl ? `loss-card-${lvl}` : ''}" id="incident-${esc(inc.id)}" data-incident-card="${esc(inc.id)}" data-tier="${lvl || ''}">${cardHtml(inc)}</div>`);
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
    const inc = findInc(id);
    const slot = slotFor(id);
    if (!inc || !slot) return;
    state.form = { id, kind: 'resolve' };
    slot.innerHTML = '<div class="fp-empty">Loading outcomes…</div>';
    const outcomes = await loadOutcomes();
    if (!state.form || state.form.id !== id) return;
    if (!outcomes.length) {
      slot.innerHTML = '<div class="loss-form"><div class="form-status form-status-error">The list of outcomes could not be loaded. Try again in a moment.</div><button type="button" class="btn btn-secondary btn-sm" data-action="cancel" data-incident="' + esc(id) + '">Close</button></div>';
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
          <button type="button" class="btn btn-secondary btn-sm" data-action="cancel" data-incident="${esc(id)}">Cancel</button>
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
          <button type="submit" class="btn btn-sm btn-primary" data-action="save-dispatch" data-incident="${esc(id)}">Mark: staff sent</button>
          <button type="button" class="btn btn-secondary btn-sm" data-action="cancel" data-incident="${esc(id)}">Cancel</button>
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

  /** Full-size evidence: the image and, when the camera saved one, the clip. */
  function openEvidence(url, caption, clipUrl, mode, note) {
    const viewer = $('theftEvidenceViewer');
    if (!viewer || (!url && !clipUrl)) return;
    state.viewer = { still: url || null, clip: clipUrl || null, caption: caption || '',
      note: note || 'suspicious behaviour for staff review' };
    // A newly opened viewer loads the clip afresh, so it plays from the start
    // instead of resuming (or sitting ended) where it was last closed.
    const video = $('theftEvidenceVideo');
    if (video) delete video.dataset.src;
    const sw = $('theftEvidenceSwitch');
    if (sw) sw.hidden = !(url && clipUrl);
    showEvidence(mode === 'clip' && clipUrl ? 'clip' : (url ? 'still' : 'clip'));
    viewer.style.display = 'flex';
    const close = $('theftEvidenceClose');
    if (close) close.focus({ preventScroll: true });
  }

  function showEvidence(mode) {
    const v = state.viewer;
    if (!v) return;
    v.mode = mode;
    const img = $('theftEvidenceFull');
    const video = $('theftEvidenceVideo');
    const err = $('theftEvidenceVideoError');
    const open = $('theftEvidenceOpen');
    const cap = $('theftEvidenceCaption');
    const clip = mode === 'clip';
    if (err) { err.hidden = true; err.textContent = ''; }
    if (img) {
      img.hidden = clip;
      if (!clip && v.still) img.src = authUrl(v.still);
    }
    if (video) {
      video.hidden = !clip;
      if (clip) {
        const src = authUrl(v.clip);
        // Reload after a failed load too: otherwise Image -> Clip shows an empty
        // player with no message, because no new error event fires.
        if (video.dataset.src !== src || video.error) { video.dataset.src = src; video.src = src; }
        const p = video.play();
        if (p && typeof p.catch === 'function') p.catch(() => { /* autoplay blocked: the controls remain */ });
      } else if (!video.paused) {
        video.pause();
      }
    }
    if (open) open.href = authUrl(clip ? v.clip : v.still);
    const st = $('theftEvidenceShowStill');
    const cl = $('theftEvidenceShowClip');
    if (st) st.setAttribute('aria-pressed', String(!clip));
    if (cl) cl.setAttribute('aria-pressed', String(clip));
    if (st) st.classList.toggle('active', !clip);
    if (cl) cl.classList.toggle('active', clip);
    if (cap) cap.textContent = `${v.caption}${clip ? ' · clip, about 5 s before to 10 s after' : ''} · ${v.note}`;
  }

  function closeEvidence() {
    const viewer = $('theftEvidenceViewer');
    if (viewer) viewer.style.display = 'none';
    const video = $('theftEvidenceVideo');
    if (video && !video.paused) video.pause();
    state.viewer = null;
  }

  function onVideoError() {
    const video = $('theftEvidenceVideo');
    const err = $('theftEvidenceVideoError');
    if (!video || video.hidden || !err) return;
    err.hidden = false;
    err.textContent = 'The clip could not be played. It may have been deleted by the evidence storage limit; refresh the review queue to check.';
  }

  function incidentCaption(inc) {
    return inc ? `${ruleLabel(inc)} · ${inc.camera_name || ''} · ${formatWhen(inc.timestamp)}` : '';
  }

  // ------------------------------------------------------------ patterns

  const pct = (v) => `${Math.round(v * 100)}%`;
  const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || `${one}s`)}`;

  function rateCell(r) {
    if (!num(r.false_alarm_rate)) {
      return `<span class="fp-dash" title="No outcome recorded yet">${D}</span>`;
    }
    return `<span class="loss-rate ${r.false_alarm_rate >= 0.5 ? 'loss-rate-high' : ''}" title="${r.false_alarms} of ${r.reviewed} reviewed were false alarms">${pct(r.false_alarm_rate)}</span>`;
  }

  /** A single-series bar: length = share of the largest value. */
  function bar(value, max, label) {
    const w = max > 0 ? Math.max(2, Math.round((value / max) * 100)) : 0;
    return `<span class="loss-bar" role="img" aria-label="${esc(label)}"><span class="loss-bar-fill" style="width:${w}%"></span></span>`;
  }

  /**
   * Alert-level mix of a group of incidents (patterns: tiers{}), as labelled
   * counts, highest level first; levels with none are left out. '' when the
   * box does not report levels. Never colour alone: each count names its level.
   */
  function tierMix(tiers, compact) {
    if (!tiers || typeof tiers !== 'object') return '';
    const order = ['critical', 'alert', 'watch', 'review', UNCLASSIFIED];
    const parts = order.filter((t) => num(tiers[t]) && tiers[t] > 0).map((t) => {
      const label = t === UNCLASSIFIED ? 'No level' : TIER_LABEL[t];
      const cls = t === UNCLASSIFIED ? 'loss-tier-none' : `tier-${t}`;
      const tip = `${tiers[t]} ${label}${t === UNCLASSIFIED ? ' (recorded before levels)' : ''}`;
      return `<span class="tier-badge ${cls} loss-mix-badge" title="${esc(tip)}">${compact ? '' : `${esc(label)} `}<span class="loss-mix-n">${tiers[t]}</span>${compact ? `<span class="sr-only"> ${esc(label)}</span>` : ''}</span>`;
    });
    return parts.length ? `<span class="loss-mix">${parts.join('')}</span>` : '';
  }
  const hasTiers = (rows) => rows.some((r) => r && r.tiers && typeof r.tiers === 'object');

  function hotspotTable(rows, kind) {
    if (!rows.length) {
      return `<div class="fp-empty">${kind === 'zone'
        ? 'No incident was tied to a drawn area in this period. Areas come from the product zones and floor zones drawn in Camera setup.'
        : 'No incidents in this period.'}</div>`;
    }
    const max = Math.max(...rows.map((r) => r.incidents));
    const top = rows.slice(0, 8);
    const levels = hasTiers(top);
    const body = top.map((r) => {
      const name = kind === 'zone'
        ? `${esc(r.zone_name || r.zone_id)}<div class="loss-pattern-sub">${esc(r.camera_name || r.camera_id)}</div>`
        : esc(r.camera_name || r.camera_id);
      return `<tr>
        <td>${name}</td>
        <td class="loss-num">${r.incidents}</td>
        <td class="loss-bar-cell">${bar(r.incidents, max, `${r.incidents} of ${max}`)}</td>
        ${levels ? `<td>${tierMix(r.tiers) || D}</td>` : ''}
        <td class="loss-num">${rateCell(r)}</td>
        <td>${r.top_rule_label ? esc(r.top_rule_label) : D}</td>
      </tr>`;
    }).join('');
    const more = rows.length > top.length ? `<div class="loss-pattern-note">${rows.length - top.length} more not shown.</div>` : '';
    return `<div class="loss-table-wrap"><table class="friction-table loss-pattern-table">
      <thead><tr><th>${kind === 'zone' ? 'Area' : 'Camera'}</th><th>Incidents</th><th><span class="sr-only">Share</span></th>${levels ? '<th>Levels</th>' : ''}<th>False alarms</th><th>Most common check</th></tr></thead>
      <tbody>${body}</tbody></table></div>${more}`;
  }

  function heatHtml(h) {
    const m = (h && h.matrix) || [];
    if (!m.length) return '<div class="fp-empty">No incidents in this period.</div>';
    const max = h.max || 0;
    const days = h.days || ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    const head = `<div class="loss-heat-corner"></div>${Array.from({ length: 24 }, (_, i) => `<div class="loss-heat-hour">${i % 3 === 0 ? String(i).padStart(2, '0') : ''}</div>`).join('')}`;
    const rows = m.map((row, d) => `<div class="loss-heat-day">${esc(days[d])}</div>${row.map((n, hr) => {
      const a = n > 0 && max > 0 ? (0.18 + 0.82 * (n / max)).toFixed(2) : 0;
      const tip = `${days[d]} ${String(hr).padStart(2, '0')}:00-${String(hr).padStart(2, '0')}:59 · ${plural(n, 'incident')}`;
      return `<div class="loss-heat-cell ${n ? 'has-value' : ''}" style="--a:${a}" title="${esc(tip)}" aria-label="${esc(tip)}" role="img"></div>`;
    }).join('')}`).join('');
    const peak = h.peak ? `Busiest: <b>${esc(h.peak.day)} ${String(h.peak.hour).padStart(2, '0')}:00</b> with ${plural(h.peak.incidents, 'incident')}.` : '';
    return `<div class="loss-heat-scroll"><div class="loss-heat" role="group" aria-label="Incidents by day of week and hour">${head}${rows}</div></div>
      <div class="loss-pattern-note">${peak} Darker = more incidents. Hover a square for the count.</div>`;
  }

  function rulesHtml(rows) {
    if (!rows.length) return '<div class="fp-empty">No incidents in this period.</div>';
    const max = Math.max(...rows.map((r) => r.incidents));
    const levels = hasTiers(rows);
    return `<div class="loss-table-wrap"><table class="friction-table loss-pattern-table">
      <thead><tr><th>Check</th><th>Incidents</th><th><span class="sr-only">Share</span></th>${levels ? '<th>Levels</th>' : ''}<th>Reviewed</th><th>False alarms</th></tr></thead>
      <tbody>${rows.map((r) => `<tr>
        <td>${esc(r.label || r.rule)}</td>
        <td class="loss-num">${r.incidents}</td>
        <td class="loss-bar-cell">${bar(r.incidents, max, `${pct(r.share)} of incidents`)}</td>
        ${levels ? `<td>${tierMix(r.tiers) || D}</td>` : ''}
        <td class="loss-num">${r.reviewed}</td>
        <td class="loss-num">${rateCell(r)}</td>
      </tr>`).join('')}</tbody></table></div>
      <div class="loss-pattern-note">False alarms = outcomes recorded as false alarm, out of incidents reviewed.</div>`;
  }

  function weeklyHtml(rows) {
    if (!rows.length) return '<div class="fp-empty">No incidents in this period.</div>';
    const max = Math.max(...rows.map((w) => w.incidents));
    const levels = hasTiers(rows);
    return `<div class="loss-table-wrap"><table class="friction-table loss-pattern-table">
      <thead><tr><th>Week of</th><th>Incidents</th><th><span class="sr-only">Trend</span></th>${levels ? '<th>Levels</th>' : ''}<th>False alarms</th></tr></thead>
      <tbody>${rows.map((w) => {
        const d = new Date(`${w.week_start}T00:00:00`);
        const label = Number.isNaN(d.getTime()) ? w.week_start : d.toLocaleDateString([], { day: 'numeric', month: 'short' });
        return `<tr>
          <td>${esc(label)}</td>
          <td class="loss-num">${w.incidents}</td>
          <td class="loss-bar-cell">${bar(w.incidents, max, plural(w.incidents, 'incident'))}</td>
          ${levels ? `<td>${tierMix(w.tiers) || D}</td>` : ''}
          <td class="loss-num">${rateCell(w)}</td>
        </tr>`;
      }).join('')}</tbody></table></div>`;
  }

  function burstsHtml(rows, p) {
    if (!rows.length) {
      return `<div class="fp-empty">No bursts: no camera and area had ${num(p.burst_min_incidents) ? p.burst_min_incidents : 3} or more incidents each within ${num(p.burst_window_minutes) ? p.burst_window_minutes : 30} minutes of the last.</div>`;
    }
    return `<ul class="loss-bursts">${rows.slice(0, 12).map((b) => {
      const place = `${esc(b.camera_name || b.camera_id)}${b.zone_id ? ` · ${esc(b.zone_name || b.zone_id)}` : ''}`;
      const span = b.duration_minutes >= 1 ? ` over ${Math.round(b.duration_minutes)} min` : '';
      const rules = (b.rules || []).map((r) => `${esc(r.label || r.rule)} ×${r.incidents}`).join(', ');
      const review = b.reviewed ? ` · ${b.false_alarms} of ${b.reviewed} reviewed were false alarms` : (b.open ? ` · ${b.open} waiting for review` : '');
      const links = (b.incident_ids || []).slice(0, 6).map((id, i) =>
        `<button type="button" class="btn btn-secondary btn-xs" data-focus-incident="${esc(id)}">#${i + 1}</button>`).join('');
      return `<li class="loss-burst">
        <div><b>${plural(b.incidents, 'incident')}</b> at ${place}${span}, ${esc(formatWhen(b.start))}</div>
        <div class="loss-pattern-sub">${rules}${review}</div>
        ${tierMix(b.tiers) ? `<div class="loss-burst-tiers">${tierMix(b.tiers)}</div>` : ''}
        <div class="loss-burst-links" aria-label="Show these incidents">${links}</div>
      </li>`;
    }).join('')}</ul>`;
  }

  function renderPatterns() {
    const sum = $('lossPatternsSummary');
    if (!sum) return;
    document.querySelectorAll('#lossPatternDays [data-pattern-days]').forEach((b) => {
      const on = Number(b.dataset.patternDays) === state.patternDays;
      b.classList.toggle('active', on);
      b.setAttribute('aria-pressed', String(on));
    });
    const p = state.patterns;
    const ids = ['lossPatternCameras', 'lossPatternZones', 'lossPatternHeat', 'lossPatternRules', 'lossPatternWeekly', 'lossPatternBursts'];
    if (!p || p.days !== state.patternDays) {
      sum.innerHTML = `<div class="fp-empty">${state.patternsFailed ? 'Patterns are not available right now: the loss-prevention service did not answer. They are retried every minute.' : 'Loading…'}</div>`;
      ids.forEach((id) => { const n = $(id); if (n) n.innerHTML = ''; });
      return;
    }
    const s = p.summary || {};
    if (!s.incidents) {
      sum.innerHTML = `<div class="fp-empty">No incidents recorded in the last ${plural(p.days, 'day')}, so there are no patterns to show.</div>`;
    } else {
      const fa = num(s.false_alarm_rate) ? `${pct(s.false_alarm_rate)} false alarms among ${s.reviewed} reviewed` : 'no outcomes recorded yet';
      const mix = tierMix(s.tiers);
      sum.innerHTML = `<b>${plural(s.incidents, 'incident')}</b> in the last ${plural(p.days, 'day')} · ${s.open} waiting for review · ${fa}${p.timezone ? ` · times in ${esc(p.timezone)}` : ''}`
        + (mix ? `<div class="loss-pattern-levels"><span class="loss-chips-label">By level</span>${mix}</div>` : '');
    }
    const hs = p.hotspots || {};
    const set = (id, html) => { const n = $(id); if (n) n.innerHTML = html; };
    set('lossPatternCameras', hotspotTable(hs.cameras || [], 'camera'));
    set('lossPatternZones', hotspotTable(hs.zones || [], 'zone')
      + (hs.unzoned_incidents ? `<div class="loss-pattern-note">${plural(hs.unzoned_incidents, 'incident')} not tied to an area.</div>` : ''));
    set('lossPatternHeat', heatHtml(p.hour_dow));
    set('lossPatternRules', rulesHtml(p.rules || []));
    set('lossPatternWeekly', weeklyHtml(p.weekly || []));
    set('lossPatternBursts', burstsHtml(p.bursts || [], p));
  }

  // ------------------------------------------------------------ public

  function focusIncident(id) {
    const inc = findInc(id);
    if (inc && state.tierFilter !== 'all' && !source().some((i) => i.id === id)) {
      // Not of the chosen level: show every level.
      state.tierFilter = 'all';
      state.tierIncidents = null;
    }
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
    if (!node) {
      if (!inc) toast('That incident is older than the review queue shows (the newest 200).');
      return false;
    }
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
    const tierChip = e.target.closest('[data-tier-filter]');
    if (tierChip && tierChip.closest('#tab-theft')) {
      const t = tierChip.dataset.tierFilter;
      if (t !== state.tierFilter) {
        closeForm();
        state.tierFilter = t;
        state.tierIncidents = null;
        state.tierFailed = false;
        renderList(true);
        loadTier();
      }
      return;
    }
    const thumb = e.target.closest('[data-evidence-url]');
    if (thumb && thumb.closest('#theftIncidentsList')) {
      e.preventDefault();
      const card = thumb.closest('[data-incident-card]');
      const inc = card && findInc(card.dataset.incidentCard);
      openEvidence(thumb.dataset.evidenceUrl, incidentCaption(inc), inc && inc.clip_url, 'still');
      return;
    }
    const days = e.target.closest('[data-pattern-days]');
    if (days && days.closest('#lossPatternDays')) {
      const n = Number(days.dataset.patternDays);
      if (n && n !== state.patternDays) {
        state.patternDays = n;
        state.patterns = null;
        renderPatterns();
        loadPatterns();
      }
      return;
    }
    const jump = e.target.closest('[data-focus-incident]');
    if (jump) {
      focusIncident(jump.dataset.focusIncident);
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
    else if (action === 'watch-live') {
      const inc = findInc(id);
      if (inc && inc.camera_id && typeof window.openCameraLive === 'function') window.openCameraLive(inc.camera_id, { trackId: inc.person_track_id || null });
    }
    else if (action === 'play-clip') {
      const inc = findInc(id);
      if (inc && inc.clip_url) openEvidence(inc.snapshot_url || inc.evidence_snapshot_url || null, incidentCaption(inc), inc.clip_url, 'clip');
    }
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
        if (!b) {
          if (e.target.closest('.theft-banner-live')) bannerLive();
          return;
        }
        if (b.dataset.banner === 'review') bannerReview();
        else if (b.dataset.banner === 'dismiss') bannerDismiss();
        else if (b.dataset.banner === 'live') bannerLive();
      });
    }
    const viewer = $('theftEvidenceViewer');
    if (viewer) viewer.addEventListener('click', (e) => { if (e.target === viewer) closeEvidence(); });
    const close = $('theftEvidenceClose');
    if (close) close.addEventListener('click', closeEvidence);
    const showStill = $('theftEvidenceShowStill');
    if (showStill) showStill.addEventListener('click', () => showEvidence('still'));
    const showClip = $('theftEvidenceShowClip');
    if (showClip) showClip.addEventListener('click', () => showEvidence('clip'));
    const video = $('theftEvidenceVideo');
    if (video) video.addEventListener('error', onVideoError);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeEvidence(); });
    window.addEventListener('edge:tab', (e) => {
      if (e.detail && e.detail.tab === 'loss') load(false);
    });
    // zone_alerts.js polls the area/line alerts; they share the badge and banner.
    window.addEventListener('edge:zone-alerts', () => { renderBadge(); ringForNew(); renderBanner(); });
    document.addEventListener('visibilitychange', () => { if (!document.hidden && tabVisible()) load(false); });

    const canPollNow = typeof canPoll === 'function' ? canPoll() : true;
    if (!canPollNow) return;       // signed out: the gate reloads the page after sign-in
    load(true);
    schedule();
  }

  /** The alert level of a loaded incident ({tier, label, risk}) or null; used by live_behaviour.js. */
  function incidentTier(id) {
    const inc = id ? findInc(id) : null;
    const t = tierOf(inc);
    return t ? { tier: t, label: inc.alert_tier_label || TIER_LABEL[t], risk: num(inc.risk_score) ? inc.risk_score : null } : null;
  }

  window.edgeLoss = {
    refresh, focusIncident, closeEvidence, openEvidence, incidentTier,
    // Pure helpers for tests (tests/fixtures/theft_tier_harness.js).
    _test: { tierOf, bannerAllowed, soundAllowed, bannerItem, ringForNew, renderBanner, renderTierKpis, cardHtml, tierMix, filtered, tierUrl, state },
  };

  if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') {
    window.edgeAuth.onReady(init);
  } else if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
