// Settings: site settings set in the dashboard (services/site_settings.py).
//
// #settings-store     Store name, Time zone (searchable IANA list, store time preview)
// #settings-evidence  evidence size limit, age limit, disk limit, night watch limit and clip
// #settings-alerts    high-value stock, repeat-alert quiet times, theft confidence,
//                     exit-without-checkout, queue congestion
// #settings-analysis  analysis speed (group "analysis"): GPU share for the pose model, decode
//                     cap, per-camera ceiling, idle rate, motion wake; plus what the box can
//                     sustain now (GET /api/v1/system/analysis-capacity, per camera allocated
//                     vs measured frames per second). Keys the server does not offer are not shown.
// #settings-theft-alerts  Theft alert levels (group "theft_alerts"), shown by section with the
//                     server's sub-headings (sections): when each level starts (THEFT_TIER_*_MIN,
//                     checked inline: they must rise; the server refuses otherwise with 422), who
//                     is told at each level (THEFT_ROUTE_*), case and place weights, combined and
//                     repeated signs. Above the form, the policy explainer from
//                     GET /api/v1/theft/alert-policy: the score in plain words, the four levels with
//                     what each does (their channels), worked examples and the camera-role floors.
//
// GET /api/v1/site-settings, GET /api/v1/site-settings/timezones (signed in),
// PUT /api/v1/site-settings, DELETE /api/v1/site-settings/{key} (owner / admin).
// Current evidence usage comes from GET /api/v1/system/stats (evidence_storage).
// Every change applies at once, without a restart. No prompt()/confirm()/alert():
// lowering an evidence limit asks inline ("Yes, save" / "Keep editing"), and so
// does a reset or a switch to "Automatic" that is lower (disk.auto_evidence_cap_bytes).
// A refused value (422) stays in its field with the reason next to it.
(function () {
  'use strict';

  const API = '/api/v1/site-settings';
  const DASH = '—';
  let data = null;           // last GET
  let evidence = null;       // /system/stats evidence_storage
  let zones = null;          // IANA names
  let pending = {};          // card -> values awaiting "Yes, save"
  let previewTimer = null;
  let capacity = null;       // GET /system/analysis-capacity, or {error}

  const $ = (id) => document.getElementById(id);

  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function fmtBytes(n) {
    if (n == null || !Number.isFinite(Number(n))) return DASH;
    const v = Number(n);
    if (v >= 1024 ** 3) return `${(v / 1024 ** 3).toFixed(1)} GB`;
    if (v >= 1024 ** 2) return `${(v / 1024 ** 2).toFixed(0)} MB`;
    return `${Math.round(v / 1024)} KB`;
  }

  // window.EdgeAuth (auth.js): operators may read these cards but not change
  // them. The server decides (owner / admin only); this only hides the controls.
  function canEdit() {
    const a = window.EdgeAuth;
    return !(a && a.known && !a.canChangeSetup);
  }

  async function errorOf(res, fallback) {
    let body = {};
    try { body = await res.json(); } catch (_) { /* not JSON */ }
    let d = body && body.detail;
    if (d && typeof d === 'object' && !Array.isArray(d)) return { message: d.message || fallback, errors: d.errors || {} };
    if (Array.isArray(d)) d = d.map((x) => (x && x.msg) || String(x)).join('; ');
    if (res.status === 403) d = d || 'Only an owner or admin can change these settings.';
    return { message: d || `${fallback} (HTTP ${res.status})`, errors: {} };
  }

  // ------------------------------------------------------------------ fields

  // How each setting is shown. Values stay in API units except where noted.
  const CARDS = {
    store: {
      host: 'settings-store', title: 'Store',
      keys: ['STORE_NAME', 'SITE_TIMEZONE'],
    },
    evidence: {
      host: 'settings-evidence', title: 'Evidence storage',
      intro: 'This device keeps only evidence for alerts (stills and short clips); the store recorder keeps the continuous video. When a limit is passed, the oldest evidence is deleted first.',
      keys: ['EVIDENCE_MAX_GB', 'STORAGE_RETENTION_DAYS', 'STORAGE_MAX_DISK_PERCENT', 'NIGHT_WATCH_EVIDENCE_MAX_MB', 'NIGHT_WATCH_CLIP'],
    },
    alerts: {
      host: 'settings-alerts', title: 'Alerts and detection',
      keys: ['THEFT_HIGH_VALUE_CATEGORIES', 'THEFT_HIGH_VALUE_MIN_PRICE', 'TRIPWIRE_ALERT_COOLDOWN_SEC',
        'RESTRICTED_AREA_COOLDOWN_SEC', 'THEFT_INCIDENT_COOLDOWN_SEC', 'THEFT_MIN_CONFIDENCE',
        'THEFT_EXIT_RULE_ENABLED', 'QUEUE_CONGESTED_WAIT_SEC'],
    },
    analysis: {
      host: 'settings-analysis', title: 'Analysis speed', group: 'analysis',
      intro: 'How many camera frames per second the AI analyses. The box shares its capacity between the cameras: '
        + 'cameras with people or movement get more, empty ones are checked slowly until something moves. '
        + 'A camera\'s own priority and frame-rate cap are in its Settings.',
      keys: ['POSE_BUDGET_UTILISATION', 'DECODE_MAX_FPS', 'ANALYTICS_MAX_DETECT_FPS', 'ANALYTICS_IDLE_DETECT_FPS',
        'ANALYTICS_MOTION_WAKE'],
    },
    theftAlerts: {
      host: 'settings-theft-alerts', title: 'Theft alert levels', group: 'theft_alerts', sections: true,
      intro: 'Every possible theft gets a risk score and one of four levels. The level decides who is told and how '
        + 'loudly: Review (review list only), Watch (dashboard banner), Alert (banner, alarm sound and phones) and '
        + 'Critical (as Alert, escalated and repeated until someone acknowledges it). A level is a prompt to check, '
        + 'never a finding of theft.',
      keys: [],
    },
  };
  const TIER_KEYS = ['THEFT_TIER_WATCH_MIN', 'THEFT_TIER_ALERT_MIN', 'THEFT_TIER_CRITICAL_MIN'];
  const TIER_LABEL = { review: 'Review', watch: 'Watch', alert: 'Alert', critical: 'Critical' };
  let policy = null;         // GET /api/v1/theft/alert-policy, or {error}
  // Stored as a fraction (0..1), edited and shown as a percentage.
  const PERCENT_KEYS = ['THEFT_MIN_CONFIDENCE', 'POSE_BUDGET_UTILISATION'];
  function isPct(s) {
    return !!s && (PERCENT_KEYS.includes(s.key) || s.unit === 'fraction') && (s.max == null || Number(s.max) <= 1);
  }
  /** The keys a card shows: its own list, plus any other key the server puts in its group. */
  function cardKeys(card) {
    const extra = card.group && data && data.groups && Array.isArray(data.groups[card.group]) ? data.groups[card.group] : [];
    return [...card.keys, ...extra.filter((k) => !card.keys.includes(k))];
  }
  // Lowering one of these deletes evidence straight away.
  const LOWER_DELETES = ['EVIDENCE_MAX_GB', 'STORAGE_RETENTION_DAYS', 'STORAGE_MAX_DISK_PERCENT', 'NIGHT_WATCH_EVIDENCE_MAX_MB'];

  const fid = (key) => `ss_${key}`;

  function shown(s, v) {
    // A value as the operator reads it.
    if (v == null) return DASH;
    if (s.type === 'bool') return v ? 'On' : 'Off';
    if (s.type === 'categories') return v.length ? v.join(', ') : 'None';
    if (s.type === 'str') return v === '' ? DASH : String(v);
    if (s.type === 'timezone') return v || "This device's own time zone";
    if (isPct(s)) return `${Math.round(v * 100)} %`;
    if (s.zero_means && Number(v) === 0) return s.zero_means;
    if (s.unit === 'x') return `x${Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    const unit = s.unit === 'GB' ? ' GB' : s.unit === 'MB' ? ' MB' : s.unit === '%' ? ' %' : s.unit === 's' ? ' s'
      : s.unit === 'days' ? ' days' : s.unit === 'fps' ? ' frames/s' : s.unit === 'min' ? ' min' : '';
    return `${Number(v).toLocaleString()}${unit}`;
  }

  function resetLine(s) {
    if (!s.overridden) return `<div class="ss-default">Default: ${esc(shown(s, s.default))}</div>`;
    return `<div class="ss-default">Changed from the default (${esc(shown(s, s.default))})${s.changed_by ? ` by ${esc(s.changed_by)}` : ''}.
      ${canEdit() ? `<button type="button" class="link-btn" data-ss-reset="${esc(s.key)}">Reset to default</button>` : ''}</div>`;
  }

  function rangeHint(s) {
    if (s.type !== 'int' && s.type !== 'float') return '';
    if (isPct(s)) return `Allowed: ${Math.round(s.min * 100)}-${Math.round(s.max * 100)} %.`;
    const lo = s.min != null ? Number(s.min).toLocaleString() : '';
    const hi = s.max != null ? Number(s.max).toLocaleString() : '';
    return `Allowed: ${s.zero_means ? '0 or ' : ''}${lo}-${hi}${s.unit && s.unit !== 'fraction' && s.unit !== 's' && s.unit !== 'days' ? ' ' + s.unit : ''}.`;
  }

  function control(s, dis) {
    const id = fid(s.key);
    const d = dis ? 'disabled' : '';
    if (s.type === 'bool') {
      return `<label class="checkbox-label"><input type="checkbox" id="${id}" ${s.value ? 'checked' : ''} ${d}> ${esc(s.label)}</label>`;
    }
    const label = `<label for="${id}">${esc(s.label)}</label>`;
    if (s.type === 'str') {
      return `${label}<input class="form-input" id="${id}" maxlength="${s.max || 80}" value="${esc(s.value)}" ${d} autocomplete="off">`;
    }
    if (s.type === 'timezone') {
      return `${label}<input class="form-input" id="${id}" list="ssZoneList" value="${esc(s.value)}" ${d}
          placeholder="Type to search, e.g. Australia/Melbourne (empty = this device's own zone)" autocomplete="off" spellcheck="false">
        <datalist id="ssZoneList">${(zones || []).map((z) => `<option value="${esc(z)}"></option>`).join('')}</datalist>
        <div class="ss-preview" id="ssZonePreview" aria-live="polite"></div>`;
    }
    if (s.type === 'categories') {
      return `${label}<input class="form-input" id="${id}" value="${esc((s.value || []).join(', '))}" ${d}
          placeholder="e.g. SPIRITS, COSMETICS, BABY_FORMULA" autocomplete="off" spellcheck="false">`;
    }
    if (s.key === 'EVIDENCE_MAX_GB') {
      const auto = Number(s.value) === 0;
      const most = data && data.disk && data.disk.max_evidence_gb;
      return `${label}
        <div class="ss-inline">
          <select class="form-select" id="${id}_mode" ${d}>
            <option value="auto" ${auto ? 'selected' : ''}>Automatic (10% of disk)</option>
            <option value="fixed" ${auto ? '' : 'selected'}>Fixed size</option>
          </select>
          <span class="ss-num-unit"><input class="form-input ss-num" id="${id}" type="number" min="${s.min}" ${most != null ? `max="${most}"` : ''} step="0.5"
                 value="${auto ? '' : esc(s.value)}" ${auto || dis ? 'disabled' : ''} aria-label="Evidence size limit in GB"> <span class="ss-unit">GB</span></span>
        </div>`;
    }
    if (isPct(s)) {
      return `${label}<div class="ss-inline"><input class="form-input ss-num" id="${id}" type="number" min="${Math.round(s.min * 100)}"
          max="${Math.round(s.max * 100)}" step="1" value="${s.value == null ? '' : Math.round(s.value * 100)}" ${d}> <span class="ss-unit">%</span></div>`;
    }
    const step = s.type === 'int' ? 1 : (s.unit === '%' ? 1 : s.unit === 'x' ? 0.05 : 'any');
    // The labels name their unit ("(days)", "(%)", "(seconds)", "(MB)"); a weight says "x" (times).
    const unit = s.unit === 'x' ? '<span class="ss-unit">x (1 = as measured)</span>' : '';
    return `${label}<div class="ss-inline"><input class="form-input ss-num" id="${id}" type="number"
        min="${s.zero_means ? 0 : s.min}" max="${s.max}" step="${step}" value="${esc(s.value)}" ${d}> ${unit}</div>`;
  }

  function field(s, dis) {
    return `<div class="ss-field" data-ss-key="${esc(s.key)}">
      ${control(s, dis)}
      <div class="ra-hint">${esc(s.help)} ${esc(rangeHint(s))}</div>
      ${resetLine(s)}
      <div class="form-status form-status-error ss-err" id="${fid(s.key)}_err"></div>
    </div>`;
  }

  function usageHtml() {
    const disk = (data && data.disk) || {};
    const ev = evidence || {};
    const used = ev.used_bytes != null ? fmtBytes(ev.used_bytes) : DASH;
    const cap = ev.effective_cap_bytes != null ? fmtBytes(ev.effective_cap_bytes)
      : (ev.cap_bytes != null ? fmtBytes(ev.cap_bytes) : DASH);
    const why = ev.used_bytes == null ? ' (not measured yet: the first check runs shortly after start-up)' : '';
    const limited = ev.limited_by && ev.limited_by !== 'cap' ? ` Limited now by the disk: ${esc(ev.limited_by)}.` : '';
    return `<div class="ss-usage">
      <div><span class="telemetry-label">Evidence now</span> <b>${used}</b> of ${cap}${why}${limited}</div>
      <div><span class="telemetry-label">Disk</span> ${fmtBytes(disk.free_bytes)} free of ${fmtBytes(disk.total_bytes)}</div>
    </div>`;
  }

  function renderCard(name, statusText, isError) {
    const card = CARDS[name];
    const host = $(card.host);
    if (!host) return;
    if (!data) {
      host.innerHTML = `<div class="card-title"><span>${esc(card.title)}</span></div>
        <div class="form-status ${isError ? 'form-status-error' : ''}">${esc(statusText || 'Loading…')}</div>`;
      return;
    }
    const dis = !canEdit();
    const items = cardKeys(card).map((k) => data.settings[k]).filter(Boolean);
    const explain = name === 'theftAlerts' ? policyHtml() : '';
    if (!items.length) {
      // This server does not offer these settings (older build): say so, show no empty form.
      host.innerHTML = `
        <div class="card-title"><span>${esc(card.title)}</span></div>
        ${card.intro ? `<p class="ra-hint ss-intro">${esc(card.intro)}</p>` : ''}
        <div class="form-status">This box does not offer these settings yet.</div>
        ${name === 'analysis' ? capacityHtml() : ''}${explain}`;
      bindCapacity(host);
      return;
    }
    host.innerHTML = `
      <div class="card-title"><span>${esc(card.title)}</span></div>
      ${card.intro ? `<p class="ra-hint ss-intro">${esc(card.intro)}</p>` : ''}
      ${name === 'evidence' ? usageHtml() : ''}
      ${name === 'analysis' ? capacityHtml() : ''}
      ${explain}
      <form class="ss-form" id="ss_form_${name}" autocomplete="off" novalidate>
        ${card.sections ? sectionedFields(items, dis) : items.map((s) => field(s, dis)).join('')}
        ${dis ? '<div class="ra-hint">Only an owner or admin can change these settings.</div>' : `
        <div class="ss-actions" id="ss_actions_${name}">
          <button type="submit" class="btn btn-primary btn-sm">Save</button>
        </div>`}
        <div class="form-status ${isError ? 'form-status-error' : ''}" id="ss_status_${name}" aria-live="polite">${esc(statusText || '')}</div>
      </form>`;
    const form = $(`ss_form_${name}`);
    form.addEventListener('submit', (ev) => { ev.preventDefault(); onSave(name); });
    form.addEventListener('input', (ev) => {
      setStatus(name, '');
      if (name === 'theftAlerts' && ev.target && TIER_KEYS.some((k) => ev.target.id === fid(k))) checkTierOrder(true);
    });
    host.querySelectorAll('[data-ss-reset]').forEach((b) => b.addEventListener('click', () => onReset(name, b.dataset.ssReset)));
    bindCapacity(host);
    if (name === 'store') {
      const tz = $(fid('SITE_TIMEZONE'));
      if (tz) tz.addEventListener('input', updatePreview);
      updatePreview();
    }
    if (name === 'evidence') {
      const mode = $(`${fid('EVIDENCE_MAX_GB')}_mode`);
      const num = $(fid('EVIDENCE_MAX_GB'));
      if (mode && num) {
        mode.addEventListener('change', () => {
          num.disabled = mode.value === 'auto' || dis;
          if (mode.value === 'fixed' && !num.value) num.value = data.disk && data.disk.max_evidence_gb ? Math.min(20, data.disk.max_evidence_gb) : 20;
          if (mode.value === 'fixed') num.focus();
        });
      }
    }
  }

  // ------------------------------------------------------------------ theft alert levels

  /** Fields grouped by their section (server sub-headings, in the server's order). */
  function sectionedFields(items, dis) {
    const labels = (data && data.sections) || {};
    const order = [...Object.keys(labels), ...items.map((s) => s.section || '')];
    const seen = new Set();
    return order.filter((sec) => { if (seen.has(sec)) return false; seen.add(sec); return true; }).map((sec) => {
      const group = items.filter((s) => (s.section || '') === sec);
      if (!group.length) return '';
      const title = labels[sec] || (sec ? sec.charAt(0).toUpperCase() + sec.slice(1) : 'Other');
      return `<div class="ss-section" data-ss-section="${esc(sec)}"><h4 class="ss-section-title">${esc(title)}</h4>
          ${SECTION_NOTES[sec] ? `<p class="ra-hint ss-section-note">${esc(SECTION_NOTES[sec])}</p>` : ''}</div>
        ${group.map((s) => field(s, dis)).join('')}`;
    }).join('');
  }
  const SECTION_NOTES = {
    levels: 'Risk scores run from 0 to 100 %. Each level starts at its threshold; the three must rise (Watch below Alert below Critical).',
    routing: 'Review never interrupts anyone, and Critical always uses every channel. These switches change Watch and Alert.',
    cases: 'Multiplies the confidence by what kind of behaviour it was. Lower a weight when a check raises too many false alarms in this store.',
    places: 'Multiplies the confidence by where it happened. Several places multiply together, capped (see above).',
    combos: 'Raise the level when the same person shows several signs, or when incidents repeat at one shelf.',
  };

  /** The three thresholds as typed (fractions), or null where empty or not a number. */
  function typedTiers() {
    return TIER_KEYS.map((k) => {
      const el = $(fid(k));
      const s = data && data.settings[k];
      if (!el || !s) return s ? s.value : null;
      const v = readValue(s);
      return typeof v === 'number' && Number.isFinite(v) ? v : null;
    });
  }

  /** Inline check that the levels rise; shows the reason under the field that breaks it. True when fine. */
  function checkTierOrder(live) {
    if (!data || !TIER_KEYS.every((k) => data.settings[k])) return true;
    const [w, a, c] = typedTiers();
    TIER_KEYS.forEach((k) => { const e = $(`${fid(k)}_err`); if (e && e.dataset.order) { e.textContent = ''; delete e.dataset.order; } });
    if (w === null || a === null || c === null) return true;      // "enter a number" comes from the server
    let bad = null;
    if (!(a > w)) bad = 'THEFT_TIER_ALERT_MIN';
    else if (!(c > a)) bad = 'THEFT_TIER_CRITICAL_MIN';
    if (!bad) return true;
    const e = $(`${fid(bad)}_err`);
    if (e) {
      e.textContent = `The levels must rise: Watch (${Math.round(w * 100)} %) below Alert (${Math.round(a * 100)} %) below Critical (${Math.round(c * 100)} %).`;
      e.dataset.order = '1';
    }
    if (!live) setStatus('theftAlerts', 'Not saved: the levels must rise.', true);
    return false;
  }

  async function loadPolicy() {
    try {
      const res = await fetch('/api/v1/theft/alert-policy', { cache: 'no-store' });
      if (res.ok) policy = await res.json();
      else policy = { error: res.status === 404 || res.status === 405 ? 'missing' : `HTTP ${res.status}` };
    } catch (_) {
      policy = { error: 'no answer' };
    }
  }

  const pctText = (v) => (Number.isFinite(Number(v)) ? `${Math.round(Number(v) * 100)} %` : DASH);
  const tierBadgeHtml = (t, label) => (TIER_LABEL[t]
    ? `<span class="tier-badge tier-${t}">${esc(label || TIER_LABEL[t])}</span>` : esc(label || DASH));
  const yesNo = (v, what) => (v ? `<span class="ta-yes">Yes</span>` : `<span class="ta-no">No</span>`) + (what ? `<span class="sr-only"> ${esc(what)}</span>` : '');

  /** The explainer from GET /theft/alert-policy: plain-words score, the levels, examples, role floors. */
  function policyHtml() {
    if (!policy) return '<div class="ta-explain"><div class="ra-hint">Loading how the levels work…</div></div>';
    if (policy.error) {
      const why = policy.error === 'missing' ? 'This box does not explain its alert levels yet.' : `The explanation of the levels is not available (${esc(policy.error)}).`;
      return `<div class="ta-explain"><div class="ra-hint">${why}</div></div>`;
    }
    const p = policy;
    const cap = Number.isFinite(Number(p.place_weight_cap)) ? `x${Number(p.place_weight_cap).toFixed(2)}` : DASH;
    const combos = p.combos || {};
    const plain = `<p class="ta-plain">Each possible theft gets a <b>risk score</b>: how clearly the camera saw it
        (its confidence) <b>times</b> where it happened (place weight, together at most ${esc(cap)}) <b>times</b> what kind of
        behaviour it was (case weight). The score decides the level. Then the level can go up: hiding an item and
        then leaving without paying within ${esc(Number.isFinite(Number(combos.window_sec)) ? `${Math.round(combos.window_sec)} s` : DASH)} is always Critical;
        two different signs from the same person in that time, or ${esc(String(combos.burst_min_incidents != null ? combos.burst_min_incidents : DASH))} or more incidents at the
        same shelf within ${esc(Number.isFinite(Number(combos.burst_window_min)) ? `${Math.round(combos.burst_window_min)} min` : DASH)}, raise it by one; and some camera roles set a minimum level.
        ${Number.isFinite(Number(p.min_confidence)) ? `Below ${esc(pctText(p.min_confidence))} confidence nothing is raised at all (Alerts and detection).` : ''}</p>
      ${p.formula ? `<div class="ra-hint ta-formula">${esc(p.formula)}</div>` : ''}`;
    const tiers = Array.isArray(p.tiers) ? p.tiers : [];
    const tierRows = tiers.slice().reverse().map((t) => {
      const ch = t.channels || {};
      const range = t.id === 'review' ? `below ${pctText(t.max_risk)}` : `from ${pctText(t.min_risk)}`;
      return `<tr>
          <td>${tierBadgeHtml(t.id, t.label)}</td>
          <td class="num">${esc(range)}</td>
          <td>${yesNo(ch.banner, 'dashboard banner')}</td>
          <td>${yesNo(ch.sound, 'alarm sound')}</td>
          <td>${yesNo(ch.push, 'phones')}</td>
          <td>${yesNo(ch.escalate, 'escalation')}${ch.repeat ? ', repeated' : ''}</td>
          <td class="ta-desc">${esc(t.description || '')}${(t.editable_settings || []).length ? ' <span class="ta-note">Switchable below.</span>' : ''}</td>
        </tr>`;
    }).join('');
    const tiersTable = tiers.length ? `<div class="ss-cap-wrap"><table class="table table-compact ta-table">
        <thead><tr><th>Level</th><th class="num">Risk</th><th>Banner</th><th>Sound</th><th>Phones</th><th>Escalate</th><th>What happens</th></tr></thead>
        <tbody>${tierRows}</tbody></table></div>` : '';
    const ex = Array.isArray(p.examples) ? p.examples : [];
    const exTable = ex.length ? `<div class="ta-sub">Worked examples with the current settings</div>
      <div class="ss-cap-wrap"><table class="table table-compact ta-table">
        <thead><tr><th>Example</th><th class="num">Confidence</th><th class="num">Risk</th><th>Level</th></tr></thead>
        <tbody>${ex.map((e) => `<tr><td>${esc(e.label)}</td><td class="num">${esc(pctText(e.confidence))}</td>
          <td class="num">${esc(pctText(e.risk_score))}</td><td>${tierBadgeHtml(e.alert_tier)}</td></tr>`).join('')}</tbody></table></div>
      <div class="ra-hint">Illustrations worked out by the box with today's settings, not recorded incidents.</div>` : '';
    const roles = Array.isArray(p.roles) ? p.roles : [];
    const roleRows = roles.map((r) => `<tr><td>${esc(r.label || r.role)}</td>
        <td class="num">${Number.isFinite(Number(r.place_weight)) ? `x${Number(r.place_weight).toFixed(2)}` : DASH}</td>
        <td>${r.min_tier ? tierBadgeHtml(r.min_tier) : '<span class="ta-no">none</span>'}</td>
        <td class="num">${Number.isFinite(Number(r.min_confidence)) ? esc(pctText(r.min_confidence)) : DASH}</td></tr>`).join('');
    const rolesTable = roles.length ? `<div class="ta-sub">Camera roles</div>
      <div class="ss-cap-wrap"><table class="table table-compact ta-table">
        <thead><tr><th>Role</th><th class="num">Place weight</th><th>Minimum level</th><th class="num">Raised from confidence</th></tr></thead>
        <tbody>${roleRows}</tbody></table></div>
      <div class="ra-hint">A minimum level applies only to incidents whose risk reached Watch; weaker evidence stays in Review. A camera's role is set in its Settings.</div>` : '';
    const rules = Array.isArray(combos.rules) ? combos.rules : [];
    const rulesList = rules.length ? `<div class="ta-sub">Combined and repeated signs</div>
      <ul class="ta-rules">${rules.map((r) => `<li>${esc(r)}</li>`).join('')}</ul>
      ${Number.isFinite(Number(combos.critical_max_repeats)) ? `<div class="ra-hint">A Critical alert nobody acknowledges is sent again at most ${esc(String(combos.critical_max_repeats))} times.</div>` : ''}` : '';
    const ph = p.phone_alerts || {};
    const phone = ph.min_confidence_applies_to
      ? `<div class="ra-hint">Phone alert level in Phone alerts (${esc(pctText(ph.min_confidence))}): applies to ${esc(ph.min_confidence_applies_to)}.</div>` : '';
    return `<div class="ta-explain">
        <div class="ta-sub">How the level is worked out</div>
        ${plain}
        ${tiersTable}
        <details class="ta-more"><summary>Examples, camera roles and combined signs</summary>
          ${exTable}${rolesTable}${rulesList}
        </details>
        ${phone}
      </div>`;
  }

  function renderAll(statusText, isError) {
    Object.keys(CARDS).forEach((n) => renderCard(n, statusText, isError));
  }

  function setStatus(name, text, isError) {
    const el = $(`ss_status_${name}`);
    if (!el) return;
    el.textContent = text || '';
    el.classList.toggle('form-status-error', !!isError);
  }

  function clearErrors(name) {
    cardKeys(CARDS[name]).forEach((k) => { const e = $(`${fid(k)}_err`); if (e) e.textContent = ''; });
  }

  function showErrors(name, errors) {
    let shownAny = false;
    Object.entries(errors || {}).forEach(([k, msg]) => {
      const e = $(`${fid(k)}_err`);
      if (e) { e.textContent = msg; shownAny = true; }
    });
    return shownAny;
  }

  // ------------------------------------------------------------------ time preview

  function zoneTime(zone) {
    try {
      return new Intl.DateTimeFormat(undefined, {
        weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
        timeZone: zone || undefined, timeZoneName: 'short',
      }).format(new Date());
    } catch (_) {
      return null;
    }
  }

  function updatePreview() {
    const el = $('ssZonePreview');
    const input = $(fid('SITE_TIMEZONE'));
    if (!el || !input) return;
    const v = input.value.trim();
    const st = (data && data.store_time) || {};
    if (!v) {
      // This device's own zone: the server knows which one that is.
      const host = st.source === 'host' ? st.zone : null;
      const t = zoneTime(host || undefined);
      el.textContent = `This device's own time zone${host ? ` (${host})` : ''}. Store time now: ${t || DASH}`;
      el.classList.remove('form-status-error');
      return;
    }
    const known = !zones || zones.includes(v);
    const t = known ? zoneTime(v) : null;
    el.textContent = t ? `Store time now: ${t}` : 'Not a known time zone. Pick one from the list.';
    el.classList.toggle('form-status-error', !t);
  }

  // ------------------------------------------------------------------ save / reset

  function readValue(s) {
    const el = $(fid(s.key));
    if (s.type === 'bool') return !!(el && el.checked);
    if (!el) return s.value;
    const raw = el.value.trim();
    if (s.type === 'str' || s.type === 'timezone') return raw;
    if (s.type === 'categories') {
      return raw.split(/[\s,;]+/).map((w) => w.trim().toUpperCase().replace(/-/g, '_')).filter(Boolean);
    }
    if (s.key === 'EVIDENCE_MAX_GB') {
      const mode = $(`${fid(s.key)}_mode`);
      if (mode && mode.value === 'auto') return 0;
    }
    if (raw === '') return null;   // refused below as "enter a number"
    const n = Number(raw);
    if (isPct(s)) return Number.isFinite(n) ? Math.round(n) / 100 : raw;
    return Number.isFinite(n) ? n : raw;
  }

  function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

  // What "Automatic (10% of disk)" is on this disk, in GB (GET /site-settings
  // disk.auto_evidence_cap_bytes), or null when the disk size is unreadable.
  function autoGb() {
    const d = data && data.disk;
    if (d && Number(d.auto_evidence_cap_bytes) > 0) return Number(d.auto_evidence_cap_bytes) / 1024 ** 3;
    const cur = data && data.settings.EVIDENCE_MAX_GB;
    if (cur && Number(cur.value) === 0 && evidence && Number(evidence.cap_bytes) > 0) return Number(evidence.cap_bytes) / 1024 ** 3;
    return null;
  }

  // Effective limit for "is this lower?": 0 means unlimited / automatic.
  function lowers(key, oldV, newV) {
    const o = Number(oldV), n = Number(newV);
    if (key === 'EVIDENCE_MAX_GB') {
      if (n === o) return false;
      const auto = autoGb();
      const oldGb = o === 0 ? auto : o;
      const newGb = n === 0 ? auto : n;
      // Automatic of unknown size: ask, since it may be lower than a fixed limit.
      if (newGb == null) return oldGb != null;
      if (oldGb == null) return false;
      return newGb < oldGb;
    }
    if (key === 'STORAGE_RETENTION_DAYS' || key === 'NIGHT_WATCH_EVIDENCE_MAX_MB') {
      return n > 0 && (o === 0 || n < o);
    }
    return n < o;
  }

  function askConfirm(name, values, keys) {
    pending[name] = values;
    const box = $(`ss_actions_${name}`);
    if (!box) return;
    const labels = keys.map((k) => {
      const s = data.settings[k];
      return k === 'EVIDENCE_MAX_GB' && Number(values[k]) === 0 ? `${s.label}: ${autoLabel()}` : s.label;
    }).join(', ');
    box.innerHTML = `<div class="ss-confirm" role="group" aria-label="Confirm lower limit">
        <span>Lower limit (${esc(labels)}): the oldest evidence above it is deleted straight away.</span>
        <button type="button" class="btn btn-primary btn-sm" id="ss_yes_${name}">Yes, save</button>
        <button type="button" class="btn btn-secondary btn-sm" id="ss_no_${name}">Keep editing</button>
      </div>`;
    $(`ss_yes_${name}`).addEventListener('click', () => send(name, pending[name]));
    $(`ss_no_${name}`).addEventListener('click', () => { delete pending[name]; restoreActions(name); });
  }

  function onSave(name) {
    if (!data) return;
    clearErrors(name);
    if (name === 'theftAlerts' && !checkTierOrder(false)) return;
    const values = {};
    cardKeys(CARDS[name]).forEach((k) => {
      const s = data.settings[k];
      if (!s) return;
      const v = readValue(s);
      if (!same(v, s.value)) values[k] = v === null ? '' : v;
    });
    if (!Object.keys(values).length) { setStatus(name, 'Nothing changed.'); return; }
    const lowered = Object.keys(values).filter((k) => LOWER_DELETES.includes(k) && lowers(k, data.settings[k].value, values[k]));
    if (lowered.length) { askConfirm(name, values, lowered); return; }
    send(name, values);
  }

  async function send(name, values, method) {
    setStatus(name, 'Saving…');
    try {
      const res = method === 'DELETE'
        ? await fetch(`${API}/${encodeURIComponent(values)}`, { method: 'DELETE' })
        : await fetch(API, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ values }) });
      if (!res.ok) {
        const err = await errorOf(res, 'Not saved');
        delete pending[name];
        // Keep what the person typed: no re-render, the reason goes next to the field.
        restoreActions(name);
        clearErrors(name);
        const onField = showErrors(name, err.errors);
        setStatus(name, onField ? 'Not saved: see the highlighted fields.' : err.message, true);
        return;
      }
      data = await res.json();
      delete pending[name];
      await loadEvidence();
      if (name === 'analysis') await loadCapacity();
      if (name === 'theftAlerts') await loadPolicy();
      renderCard(name);
      setStatus(name, (data.changed || []).length ? 'Saved. Applies now.' : 'Nothing changed.');
      applyStoreName();
      window.dispatchEvent(new CustomEvent('edge:site-settings', { detail: data }));
    } catch (e) {
      setStatus(name, `Not saved: ${e.message}`, true);
    }
  }

  function autoLabel() {
    const a = autoGb();
    return a != null ? `Automatic (10% of disk) = ${a.toFixed(1)} GB` : 'Automatic (10% of disk), size not readable';
  }

  // Put the Save button back after an inline confirmation, keeping what was typed.
  function restoreActions(name) {
    const box = $(`ss_actions_${name}`);
    if (box && canEdit()) box.innerHTML = '<button type="submit" class="btn btn-primary btn-sm">Save</button>';
  }

  function onReset(name, key) {
    const s = data && data.settings[key];
    if (!s) return;
    clearErrors(name);
    if (LOWER_DELETES.includes(key) && lowers(key, s.value, s.default)) {
      // Same inline confirmation as Save, then the reset itself.
      pending[name] = key;
      const box = $(`ss_actions_${name}`);
      if (box) {
        box.innerHTML = `<div class="ss-confirm" role="group" aria-label="Confirm reset">
            <span>The default (${esc(key === 'EVIDENCE_MAX_GB' && Number(s.default) === 0 ? autoLabel() : shown(s, s.default))}) is lower: the oldest evidence above it is deleted straight away.</span>
            <button type="button" class="btn btn-primary btn-sm" id="ss_yes_${name}">Yes, reset</button>
            <button type="button" class="btn btn-secondary btn-sm" id="ss_no_${name}">Keep editing</button>
          </div>`;
        $(`ss_yes_${name}`).addEventListener('click', () => send(name, key, 'DELETE'));
        $(`ss_no_${name}`).addEventListener('click', () => { delete pending[name]; restoreActions(name); });
        return;
      }
    }
    send(name, key, 'DELETE');
  }

  // The header and the daily report show the store name.
  function applyStoreName() {
    const s = data && data.settings.STORE_NAME;
    if (!s) return;
    const name = s.value;
    const custom = name && name !== 'Store';
    const title = $('brandTitle');
    if (title) title.textContent = custom ? name : 'Store dashboard';
    document.title = custom ? `${name} - Store dashboard` : 'Store dashboard';
    const digest = $('digestStoreName');
    if (digest) digest.textContent = name || 'Store';
  }

  // ------------------------------------------------------------------ load

  async function loadEvidence() {
    try {
      const res = await fetch('/api/v1/system/stats', { cache: 'no-store' });
      evidence = res.ok ? ((await res.json()).evidence_storage || null) : null;
    } catch (_) {
      evidence = null;
    }
  }

  // ------------------------------------------------------------------ analysis capacity

  async function loadCapacity() {
    try {
      const res = await fetch('/api/v1/system/analysis-capacity', { cache: 'no-store' });
      if (res.ok) capacity = await res.json();
      else capacity = { error: res.status === 404 || res.status === 405 ? 'missing' : `HTTP ${res.status}` };
    } catch (e) {
      capacity = { error: 'no answer' };
    }
  }

  function fpsText(v) {
    if (v == null || !Number.isFinite(Number(v))) return DASH;
    const n = Number(v);
    return n < 10 ? n.toFixed(1) : String(Math.round(n));
  }

  /** What the box can sustain now, per camera: given (allocated) vs really analysed (measured). */
  function capacityHtml() {
    const refresh = '<button type="button" class="btn btn-secondary btn-xs" data-ss-capacity="refresh">Refresh</button>';
    if (!capacity) return `<div class="ss-cap"><div class="ss-cap-head"><span class="ss-cap-title">Capacity now</span>${refresh}</div><div class="ra-hint">Loading…</div></div>`;
    if (capacity.error) {
      const why = capacity.error === 'missing' ? 'This box does not report its analysis capacity yet.' : `Capacity not available (${esc(capacity.error)}).`;
      return `<div class="ss-cap"><div class="ss-cap-head"><span class="ss-cap-title">Capacity now</span>${refresh}</div><div class="ra-hint">${why}</div></div>`;
    }
    const c = capacity;
    const cams = Array.isArray(c.cameras) ? c.cameras : [];
    const prio = { low: 'Low', normal: 'Normal', high: 'High' };
    const rows = cams.map((r) => `<tr>
        <td>${esc(r.name || r.camera_id)}</td>
        <td>${esc(prio[r.priority] || (r.priority ? r.priority : 'Normal'))}</td>
        <td class="ss-cap-num">${fpsText(r.max_fps)}</td>
        <td class="ss-cap-num">${fpsText(r.allocated_fps)}</td>
        <td class="ss-cap-num">${fpsText(r.measured_fps)}</td>
        <td>${r.active === false ? '<span class="ss-cap-idle">idle</span>' : (r.active === true ? 'busy' : DASH)}</td>
      </tr>`).join('');
    const cost = c.cost_ms != null && Number.isFinite(Number(c.cost_ms)) ? `${Number(c.cost_ms).toFixed(1)} ms per frame` : `${DASH} (not measured yet)`;
    const budget = c.budget_per_sec != null && Number.isFinite(Number(c.budget_per_sec)) ? `${fpsText(c.budget_per_sec)} frames/s` : `${DASH} (not measured yet)`;
    const total = c.total_allocated != null && Number.isFinite(Number(c.total_allocated)) ? `${fpsText(c.total_allocated)} frames/s` : DASH;
    return `<div class="ss-cap">
      <div class="ss-cap-head"><span class="ss-cap-title">Capacity now</span>${refresh}</div>
      <div class="ss-usage">
        <div><span class="telemetry-label">Pose model</span> ${esc(cost)}</div>
        <div><span class="telemetry-label">Can sustain</span> ${esc(budget)}</div>
        <div><span class="telemetry-label">Given out</span> ${esc(total)}</div>
      </div>
      ${cams.length ? `<div class="ss-cap-wrap"><table class="ss-cap-table">
        <thead><tr><th>Camera</th><th>Priority</th><th class="ss-cap-num">Cap</th><th class="ss-cap-num">Given</th><th class="ss-cap-num">Analysed</th><th>Now</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
        <div class="ra-hint">Frames per second. "Given" is this camera's share of the AI; "Analysed" is what it really managed (measured).
          Idle cameras (no people, no movement) are checked slowly and wake at once when something moves.</div>`
        : '<div class="ra-hint">No camera is being analysed now.</div>'}
    </div>`;
  }

  function bindCapacity(host) {
    const b = host.querySelector('[data-ss-capacity="refresh"]');
    if (!b) return;
    b.addEventListener('click', async () => {
      b.disabled = true;
      await loadCapacity();
      // Re-render only the readout, so values being typed in the form are kept.
      const box = b.closest('.ss-cap');
      if (box) {
        const tmp = document.createElement('div');
        tmp.innerHTML = capacityHtml();
        box.replaceWith(tmp.firstElementChild);
        bindCapacity(host);
      }
    });
  }

  async function loadZones() {
    if (zones) return;
    try {
      const res = await fetch(`${API}/timezones`, { cache: 'no-store' });
      if (res.ok) zones = (await res.json()).timezones || null;
    } catch (_) { /* the field still accepts a typed name; the server checks it */ }
  }

  async function load() {
    if (!data) renderAll();
    try {
      const [res] = await Promise.all([fetch(API, { cache: 'no-store' }), loadEvidence(), loadZones(), loadCapacity(), loadPolicy()]);
      if (!res.ok) throw new Error((await errorOf(res, 'Site settings unavailable')).message);
      data = await res.json();
      pending = {};
      renderAll();
    } catch (e) {
      data = null;
      renderAll(e.message, true);
    }
  }

  function settingsVisible() {
    const tab = $('tab-settings');
    return !!(tab && tab.classList.contains('active') && !document.hidden);
  }

  function openSettings() {
    if (window.edgeAuth && window.edgeAuth.isGateOpen && window.edgeAuth.isGateOpen()) return;
    load();
    if (previewTimer) clearInterval(previewTimer);
    previewTimer = setInterval(() => { if (settingsVisible()) updatePreview(); }, 30000);
  }

  window.addEventListener('edge:account', () => { if (data) renderAll(); });
  window.addEventListener('edge:tab', (ev) => {
    if (ev.detail && ev.detail.tab === 'settings') openSettings();
    else if (previewTimer) { clearInterval(previewTimer); previewTimer = null; }
  });
  const boot = () => { if (settingsVisible()) openSettings(); };
  if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') window.edgeAuth.onReady(boot);
  else if (document.readyState !== 'loading') boot();
  else document.addEventListener('DOMContentLoaded', boot);

  window.edgeSiteSettings = { reload: load, _test: { CARDS, TIER_KEYS } };
})();
