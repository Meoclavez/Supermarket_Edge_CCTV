/**
 * Features tab (#tab-features): every feature of the system in plain words.
 *
 * Each card: what it does, why it helps the store, a live status chip (On / Off /
 * Needs setup / Not in your plan / Admin only, from GET /api/v1/features/status,
 * one request per refresh), and two buttons:
 *   - "Easy setup": a side panel with a 2-4 step guide and the few settings most
 *     stores need. The settings are read and saved through the SAME endpoints the
 *     full controls use (site settings, camera features, night watch, phone alert
 *     roster, accounts, backups...), so nothing is stored twice. After a save the
 *     owning module is told to reload, so "Full settings" shows the new value.
 *   - "Full settings": opens the exact full control through EdgeHelp.go().
 * Operators see everything but cannot change setup (the panel says why and its
 * fields are disabled); features left out of the iKorex plan are explained, their
 * buttons are disabled and "Contact iKorex" opens the support contact.
 *
 * Refresh: once at start (for the tab badge), then every 60 s only while the tab
 * is visible and the page is shown. The panel reads its own data when opened.
 * No inline handlers (CSP): every listener is added here. Words for the Help
 * panel live in js/help_content.js.
 */
(function () {
  'use strict';

  const STATUS_URL = '/api/v1/features/status';
  const POLL_MS = 60000;
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v === null || v === undefined ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const ico = (name, size) => {
    try { return window.EdgeIcon ? window.EdgeIcon.svg(name, { size: size || 'md' }) : ''; } catch (_) { return ''; }
  };
  const account = () => window.EdgeAuth || {};
  const isAdmin = () => !!account().isAdmin;
  const help = () => window.EdgeHelp || null;
  const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || `${one}s`)}`;
  const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
  const ADMIN_ONLY_MSG = "Only an owner or administrator can change this. You can see how it is set; ask an administrator to change it.";

  function friendly(status, body) {
    const h = help();
    return h ? h.friendlyError(status, body) : 'That did not work. Try again in a moment.';
  }

  /** fetch that never throws: { ok, status, data } or { ok:false, status, body, message }. */
  async function api(url, opts) {
    const o = Object.assign({}, opts || {});
    if (o.json !== undefined) {
      o.body = JSON.stringify(o.json);
      o.headers = Object.assign({ 'Content-Type': 'application/json' }, o.headers || {});
      delete o.json;
    }
    if (help()) return help().fetchJSON(url, o);
    let res;
    try { res = await fetch(url, o); } catch (_) { return { ok: false, status: 0, message: friendly(0, null) }; }
    let body = null;
    try { body = await res.json(); } catch (_) { body = null; }
    return res.ok ? { ok: true, status: res.status, data: body } : { ok: false, status: res.status, body, message: friendly(res.status, body) };
  }

  // The guide words (steps, search keywords) live in js/help_content.js as the article
  // "fs-<card id>", so the Help panel finds the same guide the Easy setup panel shows.
  function guideOf(f) {
    const C = window.EdgeHelpContent || { articles: [] };
    return (C.articles || []).find((a) => a.id === `fs-${f.id}`) || { steps: [], keywords: '' };
  }

  function planFeatures() {
    const st = window.edgeFleet && typeof window.edgeFleet.status === 'function' ? window.edgeFleet.status() : null;
    return (st && st.features) || {};
  }

  // ======================================================================== groups
  const GROUPS = [
    { id: 'security', title: 'Security and alerts', icon: 'shield-alert', sub: 'Spot suspicious behaviour and tell the right people quickly.' },
    { id: 'insights', title: 'Store insights', icon: 'trending-up', sub: 'Understand shoppers: visitors, queues, shelves, heatmaps and sales.' },
    { id: 'cameras', title: 'Cameras and store map', icon: 'cctv', sub: 'Connect the cameras and show the system your store.' },
    { id: 'access', title: 'Access and phones', icon: 'smartphone', sub: 'Who can sign in, and using the system away from the store.' },
    { id: 'system', title: 'System', icon: 'settings', sub: 'Store details, storage, backups, help and your iKorex plan.' },
  ];

  // FEATURES: one entry per card. { id, group, icon, title, what, why, plan (iKorex plan key),
  // admin (setup is for owners/administrators), operatorUse (operators use it too: no
  // "Admin only" chip), keywords, article (Help guide), steps (the 2-4 step guide),
  // full: { go (EdgeHelp.go action, or fn(status)), label, admin }, fullFor(data) (a
  // more exact target from the panel's data), setup: { title, load(ctx), render(data, ctx),
  // save(form, data, ctx) -> { ok, message }, actions: { name(btn, data, ctx) },
  // onChange(event, data), reloads: ['edgeX' | 'edgeX.method'] } }.
  // ---------------------------------------------------------------- form helpers
  // Every control below reads and saves through an endpoint the full settings use.
  const n2 = (v) => (v === null || v === undefined || v === '' ? '' : v);
  const fid = (k) => `fs_${k}`;

  function hint(text) { return text ? `<p class="feat-hint">${esc(text)}</p>` : ''; }

  function numField(key, label, value, o) {
    const p = o || {};
    return `<div class="feat-field"><label for="${fid(key)}">${esc(label)}</label>
      <div class="feat-inline"><input class="input" type="number" id="${fid(key)}" name="${esc(key)}" value="${esc(n2(value))}"
        ${p.min !== undefined ? `min="${esc(p.min)}"` : ''} ${p.max !== undefined ? `max="${esc(p.max)}"` : ''} step="${esc(p.step || 1)}" inputmode="decimal">
        ${p.unit ? `<span class="feat-hint">${esc(p.unit)}</span>` : ''}</div>${hint(p.hint)}</div>`;
  }
  function textField(key, label, value, o) {
    const p = o || {};
    return `<div class="feat-field"><label for="${fid(key)}">${esc(label)}</label>
      <input class="input" type="${esc(p.type || 'text')}" id="${fid(key)}" name="${esc(key)}" value="${esc(n2(value))}"
        ${p.max ? `maxlength="${esc(p.max)}"` : ''} ${p.list ? `list="${esc(p.list)}"` : ''} autocomplete="${esc(p.autocomplete || 'off')}" spellcheck="false">${hint(p.hint)}</div>`;
  }
  function checkField(key, label, on, o) {
    const p = o || {};
    return `<div class="feat-field"><label class="feat-check" for="${fid(key)}"><input type="checkbox" id="${fid(key)}" name="${esc(key)}"${on ? ' checked' : ''}><span>${esc(label)}</span></label>${hint(p.hint)}</div>`;
  }
  function selectField(key, label, value, options, o) {
    const p = o || {};
    return `<div class="feat-field"><label for="${fid(key)}">${esc(label)}</label>
      <select class="select" id="${fid(key)}" name="${esc(key)}">${options.map(([v, t]) => `<option value="${esc(v)}"${String(v) === String(value) ? ' selected' : ''}>${esc(t)}</option>`).join('')}</select>${hint(p.hint)}</div>`;
  }
  /** A list of rows, each with a checkbox (name="<prefix>:<id>"). */
  function checkRows(label, prefix, rows, o) {
    const p = o || {};
    if (!rows.length) return `<div class="feat-field"><span class="feat-label">${esc(label)}</span>${hint(p.empty || 'Nothing here yet.')}</div>`;
    return `<fieldset class="feat-field"><legend class="feat-label">${esc(label)}</legend>
      <ul class="feat-rows">${rows.map((r) => `<li><label class="feat-check" for="${fid(`${prefix}_${r.id}`)}"><input type="checkbox" id="${fid(`${prefix}_${r.id}`)}" name="${esc(`${prefix}:${r.id}`)}"${r.on ? ' checked' : ''}><span>${esc(r.label)}</span></label></li>`).join('')}</ul>${hint(p.hint)}</fieldset>`;
  }
  /** A list of rows, each with a select (name="<prefix>:<id>"). */
  function selectRows(label, prefix, rows, options, o) {
    const p = o || {};
    if (!rows.length) return `<div class="feat-field"><span class="feat-label">${esc(label)}</span>${hint(p.empty || 'Nothing here yet.')}</div>`;
    return `<fieldset class="feat-field"><legend class="feat-label">${esc(label)}</legend>
      <ul class="feat-rows">${rows.map((r) => `<li><label for="${fid(`${prefix}_${r.id}`)}" class="feat-check"><span>${esc(r.label)}</span></label>
        <select class="select" id="${fid(`${prefix}_${r.id}`)}" name="${esc(`${prefix}:${r.id}`)}" aria-label="${esc(r.label)}">${options.map(([v, t]) => `<option value="${esc(v)}"${String(v) === String(r.value) ? ' selected' : ''}>${esc(t)}</option>`).join('')}</select></li>`).join('')}</ul>${hint(p.hint)}</fieldset>`;
  }
  const val = (form, key) => { const n = form.elements.namedItem(key); return n ? n.value : null; };
  const checked = (form, key) => { const n = form.elements.namedItem(key); return !!(n && n.checked); };
  const rowValues = (form, prefix, kind) => {
    const out = {};
    [...form.elements].forEach((n) => {
      if (!n.name || !n.name.startsWith(`${prefix}:`)) return;
      out[n.name.slice(prefix.length + 1)] = kind === 'check' ? n.checked : n.value;
    });
    return out;
  };
  const button = (action, label, o) => {
    const p = o || {};
    return `<button type="button" class="btn btn-sm${p.primary ? ' btn-primary' : ''}" data-fd-action="${esc(action)}"${p.operator ? ' data-operator-ok' : ''}>${p.icon ? ico(p.icon, 'sm') : ''}${esc(label)}</button>`;
  };

  // Site settings (GET /api/v1/site-settings, PUT {"values": {...}}: only the keys sent change).
  async function siteLoad() {
    const r = await api('/api/v1/site-settings');
    if (!r.ok) return { error: r.message };
    return { site: r.data.settings || {}, disk: r.data.disk || {}, storeTime: r.data.store_time || {} };
  }
  const sv = (d, key) => (d.site && d.site[key] ? d.site[key].value : null);
  const pctOf = (v) => (v === null || v === undefined || v === '' ? '' : Math.round(Number(v) * 100));
  function siteRange(d, key, pct) {
    const s = (d.site && d.site[key]) || {};
    if (s.min === undefined || s.min === null) return {};
    return pct ? { min: Math.round(s.min * 100), max: Math.round(s.max * 100), unit: '%' } : { min: s.min, max: s.max, unit: s.unit && s.unit !== 'fraction' ? s.unit : '' };
  }
  function siteHint(d, key) {
    const s = (d.site && d.site[key]) || {};
    return s.help || '';
  }
  /** PUT only the values that differ from what was loaded. */
  async function siteSave(d, values) {
    const changed = {};
    Object.entries(values).forEach(([k, v]) => {
      const old = sv(d, k);
      if (String(old === null || old === undefined ? '' : old) !== String(v === null || v === undefined ? '' : v)) changed[k] = v;
    });
    if (!Object.keys(changed).length) return { ok: true, message: 'Nothing changed.' };
    const r = await api('/api/v1/site-settings', { method: 'PUT', json: { values: changed } });
    return r.ok ? { ok: true } : { ok: false, message: r.message };
  }
  const numOrNull = (v) => (v === '' || v === null || v === undefined ? null : Number(v));

  // Cameras (GET /api/v1/cameras; PUT /api/v1/cameras/{id}/features replaces the three AI
  // flags, so every one is sent with the camera's other stored settings).
  async function camerasLoad() {
    const r = await api('/api/v1/cameras');
    if (!r.ok) return { error: r.message };
    return { cameras: (r.data && r.data.cameras) || [] };
  }
  const FLAG_DEFAULTS = { people_counting: true, shelf_interaction: true, theft_detection: true, static_figure_filter: true };
  const flagOf = (c, flag) => (c.features && c.features[flag] !== undefined ? c.features[flag] !== false : FLAG_DEFAULTS[flag] !== false);
  async function saveCameraFlag(cams, flag, wanted) {
    const failed = [];
    let done = 0;
    for (const c of cams) {
      if (!(c.id in wanted) || wanted[c.id] === flagOf(c, flag)) continue;
      const body = Object.assign({}, c.features || {});
      ['people_counting', 'shelf_interaction', 'theft_detection'].forEach((k) => { body[k] = flagOf(c, k); });
      body[flag] = wanted[c.id];
      const r = await api(`/api/v1/cameras/${encodeURIComponent(c.id)}/features`, { method: 'PUT', json: body });
      if (r.ok) done += 1; else failed.push(`${c.name}: ${r.message}`);
    }
    return failed.length ? { ok: false, message: failed.join(' ') } : { ok: true, changed: done };
  }
  const camRows = (cams, flag) => cams.map((c) => ({ id: c.id, label: c.name || c.id, on: flagOf(c, flag) }));
  const studio = (tool, linkKey) => (st) => {
    const q = new URLSearchParams({ from: 'features', tool });
    const cam = st && st.links ? st.links[linkKey || 'first_camera'] : null;
    if (cam) q.set('camera_id', cam);
    return `studio-url:/dashboard/studio?${q.toString()}`;
  };
  const camLink = (prefix, linkKey, fallback) => (st) => {
    const cam = st && st.links ? st.links[linkKey] : null;
    return cam ? `${prefix}${cam}` : fallback;
  };
  const DAYS = [['mon', 'Mon'], ['tue', 'Tue'], ['wed', 'Wed'], ['thu', 'Thu'], ['fri', 'Fri'], ['sat', 'Sat'], ['sun', 'Sun']];
  const DAY_KEYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];

  /* FEATURES:BEGIN */
  const FEATURES = [
    // ===================================================== Security and alerts
    {
      id: 'theft-alerts', group: 'security', icon: 'shield-alert', plan: 'theft_detection', admin: true,
      title: 'Theft alerts',
      what: 'Flags suspicious behaviour, such as an item slipped into a pocket, for staff to check.',
      why: 'Staff look at the right moment instead of watching every camera. An alert is never proof of theft.',
      full: { go: 'settings:settings-alerts', label: 'Alerts and detection', admin: true },
      setup: {
        title: 'Main settings',
        async load() {
          const [c, s] = await Promise.all([camerasLoad(), siteLoad()]);
          if (c.error || s.error) return { error: c.error || s.error };
          return Object.assign({}, s, c);
        },
        render(d) {
          return checkRows('Look for theft on these cameras', 'cam', camRows(d.cameras, 'theft_detection'), { empty: 'No cameras yet. Add cameras first.' })
            + numField('THEFT_MIN_CONFIDENCE', 'Alerts from confidence', pctOf(sv(d, 'THEFT_MIN_CONFIDENCE')), Object.assign(siteRange(d, 'THEFT_MIN_CONFIDENCE', true), { hint: 'Lower finds more but brings more false alarms. Check "False alarms by rule" in Loss prevention first.' }))
            + checkField('THEFT_EXIT_RULE_ENABLED', 'Check for leaving without passing a checkout', !!sv(d, 'THEFT_EXIT_RULE_ENABLED'), { hint: 'Needs ENTRANCE, EXIT and CHECKOUT store areas on the map.' });
        },
        async save(form, d) {
          const pct = numOrNull(val(form, 'THEFT_MIN_CONFIDENCE'));
          const s = await siteSave(d, { THEFT_MIN_CONFIDENCE: pct === null ? null : Math.round(pct) / 100, THEFT_EXIT_RULE_ENABLED: checked(form, 'THEFT_EXIT_RULE_ENABLED') });
          if (!s.ok) return s;
          return saveCameraFlag(d.cameras, 'theft_detection', rowValues(form, 'cam', 'check'));
        },
        reloads: ['edgeSiteSettings'],
      },
    },
    {
      id: 'alert-levels', group: 'security', icon: 'siren', plan: 'theft_detection', admin: true,
      title: 'Alert levels',
      what: 'Sorts each theft alert into Review, Watch, Alert or Critical, and decides who is told at each level.',
      why: 'Small signs wait quietly in the review list; strong ones sound the alarm and reach phones at once.',
      full: { go: 'settings:settings-theft-alerts', label: 'Theft alert levels', admin: true },
      setup: {
        title: 'Levels and who is told',
        load: siteLoad,
        render(d) {
          return numField('THEFT_TIER_WATCH_MIN', 'Watch from risk score', pctOf(sv(d, 'THEFT_TIER_WATCH_MIN')), siteRange(d, 'THEFT_TIER_WATCH_MIN', true))
            + numField('THEFT_TIER_ALERT_MIN', 'Alert from risk score', pctOf(sv(d, 'THEFT_TIER_ALERT_MIN')), siteRange(d, 'THEFT_TIER_ALERT_MIN', true))
            + numField('THEFT_TIER_CRITICAL_MIN', 'Critical from risk score', pctOf(sv(d, 'THEFT_TIER_CRITICAL_MIN')), Object.assign(siteRange(d, 'THEFT_TIER_CRITICAL_MIN', true), { hint: 'The levels must rise: Watch below Alert below Critical.' }))
            + checkField('THEFT_ROUTE_ALERT_PUSH', 'Alert: send to phones', !!sv(d, 'THEFT_ROUTE_ALERT_PUSH'))
            + checkField('THEFT_ROUTE_ALERT_ESCALATE', 'Alert: escalate when nobody acknowledges', !!sv(d, 'THEFT_ROUTE_ALERT_ESCALATE'));
        },
        save(form, d) {
          const p = (k) => { const v = numOrNull(val(form, k)); return v === null ? null : Math.round(v) / 100; };
          const w = p('THEFT_TIER_WATCH_MIN'), a = p('THEFT_TIER_ALERT_MIN'), c = p('THEFT_TIER_CRITICAL_MIN');
          if (w !== null && a !== null && c !== null && !(w < a && a < c)) return { ok: false, message: 'The levels must rise: Watch below Alert below Critical.' };
          return siteSave(d, { THEFT_TIER_WATCH_MIN: w, THEFT_TIER_ALERT_MIN: a, THEFT_TIER_CRITICAL_MIN: c,
            THEFT_ROUTE_ALERT_PUSH: checked(form, 'THEFT_ROUTE_ALERT_PUSH'), THEFT_ROUTE_ALERT_ESCALATE: checked(form, 'THEFT_ROUTE_ALERT_ESCALATE') });
        },
        reloads: ['edgeSiteSettings'],
      },
    },
    {
      id: 'area-line-alerts', group: 'security', icon: 'door-open', plan: 'zone_alerts', admin: true,
      title: 'Area and line alerts',
      what: 'Alerts when someone is in a restricted area (the stockroom after closing) or crosses a line you chose.',
      why: 'You hear about a back door or the cash office straight away, with a picture.',
      full: { go: studio('restricted'), label: 'Camera setup', admin: true },
      setup: {
        title: 'Your areas and lines',
        async load() {
          const [z, s, c] = await Promise.all([api('/api/zones'), siteLoad(), camerasLoad()]);
          if (!z.ok) return { error: z.message };
          if (s.error) return { error: s.error };
          const names = {};
          (c.cameras || []).forEach((cam) => { names[cam.id] = cam.name; });
          return Object.assign({}, s, { zones: z.data || {}, names });
        },
        render(d) {
          const cam = (x) => (d.names[x.camera_id] ? ` (${d.names[x.camera_id]})` : '');
          const lines = (d.zones.tripwires || []).map((t) => ({ id: t.id, label: `${t.name || 'Line'}${cam(t)}`, on: !!t.alert_enabled }));
          const areas = (d.zones.intrusion_zones || []).map((a) => ({ id: a.id, label: `${a.name || 'Restricted area'}${cam(a)}`, on: a.enabled !== false }));
          return checkRows('Restricted areas switched on', 'area', areas, { empty: 'No restricted area drawn yet. Use Full settings to draw one in Camera setup.' })
            + checkRows('Lines that alert staff on crossing', 'line', lines, { empty: 'No lines drawn yet.' })
            + numField('TRIPWIRE_ALERT_COOLDOWN_SEC', 'Line crossing: repeat alert after', n2(sv(d, 'TRIPWIRE_ALERT_COOLDOWN_SEC')), Object.assign(siteRange(d, 'TRIPWIRE_ALERT_COOLDOWN_SEC'), { unit: 'seconds' }))
            + numField('RESTRICTED_AREA_COOLDOWN_SEC', 'Restricted area: repeat alert after', n2(sv(d, 'RESTRICTED_AREA_COOLDOWN_SEC')), Object.assign(siteRange(d, 'RESTRICTED_AREA_COOLDOWN_SEC'), { unit: 'seconds' }));
        },
        async save(form, d) {
          const s = await siteSave(d, { TRIPWIRE_ALERT_COOLDOWN_SEC: numOrNull(val(form, 'TRIPWIRE_ALERT_COOLDOWN_SEC')), RESTRICTED_AREA_COOLDOWN_SEC: numOrNull(val(form, 'RESTRICTED_AREA_COOLDOWN_SEC')) });
          if (!s.ok) return s;
          const fails = [];
          const lines = rowValues(form, 'line', 'check');
          for (const t of d.zones.tripwires || []) {
            if (t.id in lines && lines[t.id] !== !!t.alert_enabled) {
              const r = await api(`/api/zones/tripwire/${encodeURIComponent(t.id)}`, { method: 'PATCH', json: { alert_enabled: lines[t.id] } });
              if (!r.ok) fails.push(r.message);
            }
          }
          const areas = rowValues(form, 'area', 'check');
          for (const a of d.zones.intrusion_zones || []) {
            if (a.id in areas && areas[a.id] !== (a.enabled !== false)) {
              const r = await api(`/api/zones/intrusion/${encodeURIComponent(a.id)}`, { method: 'PATCH', json: { enabled: areas[a.id] } });
              if (!r.ok) fails.push(r.message);
            }
          }
          return fails.length ? { ok: false, message: fails[0] } : { ok: true };
        },
        reloads: ['edgeSiteSettings', 'edgeZoneAlerts.refresh'],
      },
    },
    {
      id: 'restricted-areas', group: 'security', icon: 'lock', plan: 'restricted_areas', admin: true, operatorUse: true,
      title: 'Restricted areas',
      what: 'Cash office, safe room or stockroom: every movement is tracked while the area is armed. Entry without notice, running, pacing, crouching, too many people or a covered camera is flagged at once.',
      why: 'An urgent alert with an evidence clip reaches the dashboard and every store phone within a second; staff tap "I\'m going in" so their own visits are not alerts.',
      full: { go: 'restricted', label: 'Restricted areas', admin: false },
      setup: {
        title: 'Arming and the main signs',
        async load() {
          const [a, c] = await Promise.all([api('/api/v1/restricted-areas'), api('/api/v1/restricted-areas/catalogue')]);
          if (!a.ok) return { error: a.message };
          if (!c.ok) return { error: c.message };
          const areas = (a.data && a.data.areas) || [];
          return { areas, cat: c.data || {}, areaId: areas.length ? areas[0].id : null, store: (a.data && a.data.store_time) || null };
        },
        render(d) {
          if (!d.areas.length) {
            return hint('No restricted area yet. Create one with Full settings, or select a zone on the Store map and press "Restricted area settings".');
          }
          const area = d.areas.find((x) => x.id === d.areaId) || d.areas[0];
          const arm = area.arming || { mode: 'always', schedule: [] };
          const row = (arm.schedule && arm.schedule[0]) || { days: DAY_KEYS, from: '18:00', to: '07:00' };
          const days = new Set(row.days || []);
          const cat = d.cat || {};
          const tiers = (cat.tiers || []).map((t) => [t.id, t.label]);
          const byId = {};
          (cat.triggers || []).forEach((t) => { byId[t.id] = t; });
          const trig = area.triggers || {};
          const main = (cat.main_triggers || []).filter((k) => byId[k]);
          return selectField('ra_area', 'Area', area.id, d.areas.map((x) => [x.id, x.name]), { hint: 'Changing the area loads its own settings.' })
            + selectField('ra_mode', 'When it is armed', arm.mode === 'schedule' ? 'schedule' : 'always', [['always', 'Always armed'], ['schedule', 'On a schedule (store time)']])
            + `<div class="feat-field"><span class="feat-label">Armed from (store time)</span>
                <div class="feat-inline"><label class="sr-only" for="${fid('ra_from')}">From</label><input class="input" type="time" id="${fid('ra_from')}" name="ra_from" value="${esc(row.from || '18:00')}">
                <span class="feat-hint">to</span><label class="sr-only" for="${fid('ra_to')}">To</label><input class="input" type="time" id="${fid('ra_to')}" name="ra_to" value="${esc(row.to || '07:00')}"></div>
                ${hint((arm.schedule || []).length > 1 ? 'Only the first time of this area is shown here; Full settings shows all of them.' : 'A time that ends before it starts runs past midnight. Used only "On a schedule".')}</div>`
            + `<fieldset class="feat-field"><legend class="feat-label">Days</legend><div class="feat-days">${DAYS.map(([k, t]) => `<label class="feat-check" for="${fid(`rad_${k}`)}"><input type="checkbox" id="${fid(`rad_${k}`)}" name="rad:${k}"${days.has(k) ? ' checked' : ''}><span>${t}</span></label>`).join('')}</div></fieldset>`
            + `<fieldset class="feat-field"><legend class="feat-label">Signs to flag and how loudly</legend><ul class="feat-rows">${main.map((k) => {
              const cfg = trig[k] || {};
              return `<li><label class="feat-check" for="${fid(`rat_${k}`)}"><input type="checkbox" id="${fid(`rat_${k}`)}" name="rat:${k}"${cfg.enabled ? ' checked' : ''}><span>${esc(byId[k].label)}</span></label>
                <select class="select" id="${fid(`ratl_${k}`)}" name="ratl:${k}" aria-label="${esc(byId[k].label)}: alert level">${tiers.map(([v, t]) => `<option value="${esc(v)}"${v === (cfg.tier || byId[k].tier) ? ' selected' : ''}>${esc(t)}</option>`).join('')}</select></li>`;
            }).join('')}</ul>${hint('Critical: banner, alarm sound and every alert phone at once, repeated until someone records what happened. Thresholds are in Full settings.')}</fieldset>`;
        },
        async onChange(e, d) {
          if (!e.target || e.target.name !== 'ra_area' || !d) return;
          d.areaId = e.target.value;
          const fields = $('featDrawerFields');
          if (fields && drawerFeature && drawerFeature.id === 'restricted-areas') {
            fields.innerHTML = drawerFeature.setup.render(d, ctx());
            if (!canEdit(drawerFeature)) fields.querySelectorAll('input, select').forEach((n) => { if (n.name !== 'ra_area') n.disabled = true; });
            const sel = $(fid('ra_area'));
            if (sel) sel.focus();
          }
        },
        async save(form, d) {
          if (!d || !d.areas.length) return { ok: false, message: 'Create a restricted area first (Full settings).' };
          const area = d.areas.find((x) => x.id === d.areaId) || d.areas[0];
          const mode = val(form, 'ra_mode') === 'schedule' ? 'schedule' : 'always';
          const days = DAY_KEYS.filter((k) => checked(form, `rad:${k}`));
          const arm = Object.assign({ schedule_mode: 'armed_during' }, area.arming || {});
          const rows = (arm.schedule || []).slice();
          if (mode === 'schedule') {
            if (!days.length) return { ok: false, message: 'Tick at least one day.' };
            rows[0] = { days, from: val(form, 'ra_from') || '18:00', to: val(form, 'ra_to') || '07:00' };
          }
          const triggers = {};
          ((d.cat && d.cat.main_triggers) || []).forEach((k) => {
            if (!form.elements.namedItem(`rat:${k}`)) return;
            triggers[k] = { enabled: checked(form, `rat:${k}`), tier: val(form, `ratl:${k}`) };
          });
          const body = { arming: { mode, schedule: mode === 'schedule' ? rows : (arm.schedule || []), schedule_mode: arm.schedule_mode || 'armed_during' }, triggers };
          const r = await api(`/api/v1/restricted-areas/${encodeURIComponent(area.id)}`, { method: 'PUT', json: body });
          if (!r.ok) return { ok: false, message: r.message };
          const i = d.areas.findIndex((x) => x.id === area.id);
          if (i >= 0 && r.data) d.areas[i] = r.data;
          const warn = r.data && r.data.warnings && r.data.warnings.length ? ` ${r.data.warnings.join(' ')}` : '';
          return { ok: true, message: `Saved. ${area.name} is ${mode === 'always' ? 'always armed' : 'armed on its schedule'}.${warn}`, keepForm: true };
        },
        reloads: ['edgeRestricted.refresh'],
      },
    },
    {
      id: 'night-watch', group: 'security', icon: 'moon', plan: 'night_watch', admin: true,
      title: 'Night watch',
      what: 'While the store is closed, chosen cameras watch for movement; a person seen is a high alert.',
      why: 'A break-in after hours reaches your phone within seconds, with a picture.',
      full: { go: camLink('night:', 'first_night_watch', 'cameras'), label: 'Camera settings: Night watch', admin: true },
      setup: {
        title: 'Night watch for one camera',
        async load() {
          const c = await camerasLoad();
          if (c.error) return { error: c.error };
          if (!c.cameras.length) return Object.assign(c, { nw: null });
          const camId = (status && status.links && status.links.first_night_watch) || c.cameras[0].id;
          const r = await api(`/api/v1/cameras/${encodeURIComponent(camId)}/night-watch`);
          if (!r.ok) return { error: r.message };
          return Object.assign(c, { camId, nw: r.data });
        },
        render(d) {
          if (!d.cameras.length) return hint('No cameras yet. Add cameras first.');
          const cfg = (d.nw && d.nw.config) || {};
          const days = new Set(cfg.days || DAY_KEYS);
          const tz = d.nw && d.nw.store_timezone ? (d.nw.store_timezone.zone || d.nw.store_timezone.name || '') : '';
          return selectField('nw_camera', 'Camera', d.camId, d.cameras.map((c) => [c.id, c.name || c.id]), { hint: 'Changing the camera loads its own night watch.' })
            + checkField('nw_enabled', 'Watch this camera at night', !!cfg.enabled)
            + `<div class="feat-field"><span class="feat-label">Times${tz ? ` (store time, ${esc(tz)})` : ' (store time)'}</span>
                <div class="feat-inline"><label class="sr-only" for="${fid('nw_start')}">Start</label><input class="input" type="time" id="${fid('nw_start')}" name="nw_start" value="${esc(cfg.start || '22:00')}">
                <span class="feat-hint">to</span><label class="sr-only" for="${fid('nw_end')}">End</label><input class="input" type="time" id="${fid('nw_end')}" name="nw_end" value="${esc(cfg.end || '06:00')}"></div></div>`
            + `<fieldset class="feat-field"><legend class="feat-label">Nights starting on</legend><div class="feat-days">${DAYS.map(([k, t]) => `<label class="feat-check" for="${fid(`nwd_${k}`)}"><input type="checkbox" id="${fid(`nwd_${k}`)}" name="nwd:${k}"${days.has(k) ? ' checked' : ''}><span>${t}</span></label>`).join('')}</div></fieldset>`
            + checkField('nw_dark', 'Also watch whenever this camera is in IR / low light', !!cfg.when_dark);
        },
        async onChange(e, d) {
          if (!e.target || e.target.name !== 'nw_camera' || !d) return;
          const r = await api(`/api/v1/cameras/${encodeURIComponent(e.target.value)}/night-watch`);
          if (!r.ok) { setMsg(r.message, 'error'); return; }
          d.camId = e.target.value;
          d.nw = r.data;
          const fields = $('featDrawerFields');
          if (fields && drawerFeature && drawerFeature.id === 'night-watch') {
            fields.innerHTML = drawerFeature.setup.render(d, ctx());
            if (!canEdit(drawerFeature)) fields.querySelectorAll('input, select').forEach((n) => { if (n.name !== 'nw_camera') n.disabled = true; });
            const sel = $(fid('nw_camera'));
            if (sel) sel.focus();
          }
        },
        async save(form, d) {
          if (!d.cameras.length) return { ok: false, message: 'Add cameras first.' };
          const days = DAY_KEYS.filter((k) => checked(form, `nwd:${k}`));
          if (checked(form, 'nw_enabled') && !days.length) return { ok: false, message: 'Tick at least one night.' };
          const cfg = Object.assign({}, (d.nw && d.nw.config) || {}, {
            enabled: checked(form, 'nw_enabled'), start: val(form, 'nw_start') || '22:00', end: val(form, 'nw_end') || '06:00',
            days: days.length ? days : DAY_KEYS, when_dark: checked(form, 'nw_dark'),
          });
          const r = await api(`/api/v1/cameras/${encodeURIComponent(d.camId)}/night-watch`, { method: 'PUT', json: cfg });
          if (!r.ok) return { ok: false, message: r.message };
          d.nw = r.data;
          return { ok: true, message: cfg.enabled ? 'Saved. This camera watches at night from now on.' : 'Saved. Night watch is off for this camera.', keepForm: true };
        },
        reloads: ['edgeNightWatch.refresh'],
      },
      fullFor: (d) => (d && d.camId ? `night:${d.camId}` : null),
    },
    {
      id: 'phone-alerts', group: 'security', icon: 'smartphone', plan: 'phone_alerts', admin: true, operatorUse: true,
      title: 'Phone alerts',
      what: 'Sends alerts to staff phones through the dashboard installed as an app, even when the app is closed.',
      why: 'The right people hear within seconds; if nobody answers, the backup people are told.',
      lockedNote: 'An owner or administrator chooses who gets alerts. To get alerts on your own phone, open the store\'s public address on that phone, install the app and tap "Enable alerts" (Full settings > Phone alerts).',
      full: { go: 'settings:settings-phone-alerts', label: 'Phone alerts' },
      setup: {
        title: 'Who gets alerts',
        async load() {
          const [r, c] = await Promise.all([api('/api/v1/web-push/roster'), api('/api/v1/web-push/config')]);
          if (!r.ok) return { error: r.message };
          return { roster: r.data, mine: (c.ok && c.data && c.data.my_subscriptions) || [] };
        },
        render(d) {
          const people = (d.roster.people || []).filter((p) => p.is_active !== false);
          const rows = people.map((p) => ({ id: p.id, label: `${p.display_name || p.username} (${p.phones ? plural(p.phones, 'phone') : 'no phone yet'})`, value: p.tier || 'none' }));
          const warn = (d.roster.warnings || []).map((w) => `<p class="feat-hint">${ico('circle-alert', 'sm')} ${esc(w)}</p>`).join('');
          return warn + selectRows('Who is told', 'tier', rows, [['first_priority', 'First priority'], ['backup', 'Backup'], ['none', 'Not told']], { hint: `At least ${d.roster.min_first_priority || 2} people must be first priority.` })
            + numField('escalate_after_min', 'Tell the backup people after', d.roster.escalate_after_min, { min: 1, max: 60, unit: 'minutes without an acknowledgement' })
            + numField('min_confidence', 'Send theft alerts from confidence', pctOf(d.roster.min_confidence), { min: 30, max: 99, unit: '%' })
            + `<div class="feat-field"><span class="feat-label">Test</span>${d.mine.length ? button('test', 'Send a test alert to my phone', { icon: 'bell', operator: true }) : hint('This account has no phone with alerts on yet.')}</div>`;
        },
        async save(form, d) {
          const tiers = rowValues(form, 'tier');
          const body = {};
          ['first_priority', 'backup', 'escalate_after_min', 'min_confidence', 'night_intrusion', 'area_alerts', 'watchdog_enabled', 'watchdog_offline_min']
            .forEach((k) => { if (d.roster[k] !== undefined) body[k] = d.roster[k]; });
          body.first_priority = Object.keys(tiers).filter((k) => tiers[k] === 'first_priority');
          body.backup = Object.keys(tiers).filter((k) => tiers[k] === 'backup');
          body.escalate_after_min = numOrNull(val(form, 'escalate_after_min'));
          const pct = numOrNull(val(form, 'min_confidence'));
          body.min_confidence = pct === null ? d.roster.min_confidence : Math.round(pct) / 100;
          const r = await api('/api/v1/web-push/roster', { method: 'PUT', json: body });
          return r.ok ? { ok: true } : { ok: false, message: r.message };
        },
        actions: {
          async test(btn, d) {
            const sub = d.mine[0];
            btn.disabled = true;
            const r = await api(`/api/v1/web-push/subscriptions/${encodeURIComponent(sub.id)}/test`, { method: 'POST' });
            btn.disabled = false;
            setMsg(r.ok ? 'Test alert sent. It should arrive on your phone within a few seconds.' : r.message, r.ok ? 'ok' : 'error');
          },
        },
        reloads: ['edgePhoneAlerts'],
      },
    },

    // ===================================================== Store insights
    {
      id: 'footfall', group: 'insights', icon: 'footprints',
      admin: true,
      title: 'Visitors and entrances',
      what: 'Counts the people who come in and go out, by hour, and how long they stay.',
      why: 'Know your busy hours, plan staff and compare today with last week.',
      full: { go: studio('tripwire'), label: 'Camera setup: entrance lines', admin: true },
      setup: {
        title: 'Counting',
        async load() {
          const [c, z] = await Promise.all([camerasLoad(), api('/api/zones')]);
          if (c.error) return { error: c.error };
          return Object.assign(c, { lines: (z.ok && z.data && z.data.tripwires) || [] });
        },
        render(d) {
          const names = {};
          d.cameras.forEach((c) => { names[c.id] = c.name; });
          return checkRows('Count people on these cameras', 'cam', camRows(d.cameras, 'people_counting'), { empty: 'No cameras yet.' })
            + checkRows('Lines that count store visitors', 'line', d.lines.map((t) => ({ id: t.id, label: `${t.name || 'Line'}${names[t.camera_id] ? ` (${names[t.camera_id]})` : ''}`, on: t.counts_footfall !== false })),
              { empty: 'No lines drawn yet. Draw one across the entrance in Camera setup (Full settings).' });
        },
        async save(form, d) {
          const r = await saveCameraFlag(d.cameras, 'people_counting', rowValues(form, 'cam', 'check'));
          if (!r.ok) return r;
          const lines = rowValues(form, 'line', 'check');
          for (const t of d.lines) {
            if (t.id in lines && lines[t.id] !== (t.counts_footfall !== false)) {
              const p = await api(`/api/zones/tripwire/${encodeURIComponent(t.id)}`, { method: 'PATCH', json: { counts_footfall: lines[t.id] } });
              if (!p.ok) return { ok: false, message: p.message };
            }
          }
          return { ok: true };
        },
      },
    },
    {
      id: 'queues', group: 'insights', icon: 'clock', admin: true,
      title: 'Checkout queues',
      what: 'Times how long shoppers wait in line and stand at each till.',
      why: 'Open another till before the queue gets long, and see which hours need more staff.',
      full: { go: studio('checkout'), label: 'Camera setup: checkout areas', admin: true },
      setup: {
        load: siteLoad,
        render(d) {
          const secs = sv(d, 'QUEUE_CONGESTED_WAIT_SEC');
          return numField('QUEUE_CONGESTED_WAIT_SEC', 'A queue is too long after', n2(secs), Object.assign(siteRange(d, 'QUEUE_CONGESTED_WAIT_SEC'), { unit: 'seconds of waiting', hint: siteHint(d, 'QUEUE_CONGESTED_WAIT_SEC') }));
        },
        save: (form, d) => siteSave(d, { QUEUE_CONGESTED_WAIT_SEC: numOrNull(val(form, 'QUEUE_CONGESTED_WAIT_SEC')) }),
        reloads: ['edgeSiteSettings'],
      },
    },
    {
      id: 'shelf-reaches', group: 'insights', icon: 'package', admin: true,
      title: 'Shelf reaches',
      what: 'Counts how often shoppers reach into each product shelf.',
      why: 'See which products are picked up and which are only looked at.',
      full: { go: studio('product'), label: 'Camera setup: product shelves', admin: true },
      setup: {
        load: camerasLoad,
        render: (d) => checkRows('Count shelf reaches on these cameras', 'cam', camRows(d.cameras, 'shelf_interaction'), { empty: 'No cameras yet.' }),
        save: (form, d) => saveCameraFlag(d.cameras, 'shelf_interaction', rowValues(form, 'cam', 'check')),
      },
    },
    {
      id: 'heatmaps', group: 'insights', icon: 'activity', plan: 'heatmaps',
      title: 'Customer heatmaps',
      what: 'Shows where shoppers walk, stop and touch shelves, hour by hour.',
      why: 'Find the hot and cold spots of the store before moving stock or promotions.',
      full: { go: 'view:insights/footfall#insights-heatmap-history', label: 'Heatmap history' },
      setup: {
        title: 'Record now',
        load: async () => ({}),
        render: () => `<p class="feat-hint">There is nothing to switch on: heatmaps are recorded every hour from calibrated cameras.</p>
          <div class="feat-field">${button('record', 'Record this hour so far now', { icon: 'refresh-cw', operator: true })}${hint('Writes the current hour now instead of waiting for it to end.')}</div>`,
        actions: {
          async record(btn) {
            btn.disabled = true;
            const r = await api('/api/v1/analytics/heatmaps/record-now', { method: 'POST' });
            btn.disabled = false;
            setMsg(r.ok ? 'Recorded. Open Full settings to see it in the heatmap history.' : r.message, r.ok ? 'ok' : 'error');
          },
        },
      },
    },
    {
      id: 'insights', group: 'insights', icon: 'trending-up', plan: 'business_insights',
      title: 'Business insights',
      what: 'Turns what the cameras measured into plain suggestions, grouped by topic, with a daily report.',
      why: 'Each suggestion says why and what to do, so you can act on facts, not guesses.',
      full: { go: 'insights', label: 'Insights' },
      setup: {
        title: 'Run it now',
        load: async () => ({}),
        render: () => `<p class="feat-hint">There is nothing to switch on: it runs every hour by itself.</p>
          <div class="feat-field">${button('run', 'Run the analysis now', { icon: 'play', operator: true })}${hint('Takes up to a minute. It can be run again after a few minutes.')}</div>`,
        actions: {
          async run(btn) {
            btn.disabled = true;
            setMsg('Running the analysis…');
            const r = await api('/api/v1/analytics/business/analysis/run', { method: 'POST', json: { narrate: false } });
            btn.disabled = false;
            if (r.ok) { setMsg('Done. Open Full settings to read the suggestions.', 'ok'); try { if (window.edgeInsights) window.edgeInsights.refresh(); } catch (_) {} return; }
            const wait = r.body && r.body.retry_after ? ` Try again in ${Math.ceil(r.body.retry_after / 60)} min.` : '';
            setMsg(r.status === 429 ? `It ran a moment ago.${wait}` : r.status === 409 ? 'It is running already. Wait a minute.' : r.message, 'error');
          },
        },
      },
    },
    {
      id: 'sales-data', group: 'insights', icon: 'receipt', plan: 'pos_integration', admin: true,
      title: 'Sales from the tills',
      what: 'Links the tills to the system so sales, the buying rate and "Bought" in the shopper journey can be shown.',
      why: 'See how many visitors buy, and which lanes and hours sell best. Optional: everything else works without it.',
      full: { go: 'settings:settings-pos', label: 'Sales data', admin: true },
      setup: {
        title: 'Till key',
        async load() {
          const r = await api('/api/v1/analytics/pos/till-key');
          return r.ok ? { key: r.data } : { error: r.message };
        },
        render(d) {
          if (d.created) {
            return `<div class="feat-field"><span class="feat-label">Your new till key (shown only once)</span><code class="feat-secret">${esc(d.created)}</code>
              ${hint(`The till sends it in the ${d.key.header || 'X-Edge-API-Key'} header. Copy it now; afterwards only its last characters are shown.`)}${button('copy', 'Copy the key', { icon: 'copy' })}</div>`;
          }
          if (d.key && d.key.exists) {
            return `<p class="feat-hint">A till key exists (ending ${esc(d.key.hint || '…')}). To replace or revoke it, use Full settings.</p>`;
          }
          return `<p class="feat-hint">No till key yet.</p><div class="feat-field">${button('create', 'Create till key', { icon: 'plus', primary: true })}</div>`;
        },
        actions: {
          async create(btn, d) {
            btn.disabled = true;
            const r = await api('/api/v1/analytics/pos/till-key', { method: 'POST' });
            btn.disabled = false;
            if (!r.ok) { setMsg(r.message, 'error'); return; }
            d.created = r.data && (r.data.key || r.data.till_key);
            d.key = Object.assign({}, d.key, r.data);
            $('featDrawerFields').innerHTML = drawerFeature.setup.render(d, ctx());
            setMsg('Till key created. Copy it now.', 'ok');
            refresh();
          },
          async copy(btn, d) {
            try { await navigator.clipboard.writeText(d.created || ''); setMsg('Copied.', 'ok'); } catch (_) { setMsg('Select the key and copy it by hand.', 'error'); }
          },
        },
      },
    },

    // ===================================================== Cameras and store map
    {
      id: 'cameras', group: 'cameras', icon: 'cctv', admin: true,
      title: 'Cameras and recorder',
      what: 'Connects the store\'s cameras, best as channels of the recorder (NVR), and turns each one on or off.',
      why: 'Every figure and alert comes from the cameras. A camera turned off uses no processing.',
      full: { go: 'add-cameras', label: 'Cameras & devices', admin: true },
      setup: {
        title: 'Cameras turned on',
        load: camerasLoad,
        render: (d) => checkRows('Analyse these cameras', 'cam', d.cameras.map((c) => ({ id: c.id, label: `${c.name || c.id}${c.status && c.status !== 'ONLINE' ? ` (${String(c.status).toLowerCase()})` : ''}`, on: c.is_ai_enabled !== false })),
          { empty: 'No cameras yet.' }) + `<div class="feat-field">${button('add', 'Add cameras', { icon: 'plus' })}</div>`,
        async save(form, d) {
          const want = rowValues(form, 'cam', 'check');
          for (const c of d.cameras) {
            if (!(c.id in want) || want[c.id] === (c.is_ai_enabled !== false)) continue;
            const r = await api(`/api/v1/cameras/${encodeURIComponent(c.id)}/enabled`, { method: 'PUT', json: { enabled: want[c.id] } });
            if (!r.ok) return { ok: false, message: r.message };
          }
          return { ok: true };
        },
        actions: { add() { closeDrawer(false); if (help()) help().go('add-cameras'); } },
      },
    },
    {
      id: 'camera-purpose', group: 'cameras', icon: 'camera', admin: true,
      title: 'Camera purposes',
      what: 'Tells the system what each camera looks at: an entrance, a checkout, an aisle, a stockroom.',
      why: 'The purpose switches on the right checks for that camera and lists what is still missing.',
      full: { go: camLink('camera:', 'first_without_purpose', 'cameras'), label: 'Camera settings', admin: true },
      setup: {
        title: 'What each camera looks at',
        async load() {
          const [c, p] = await Promise.all([camerasLoad(), api('/api/v1/camera-roles')]);
          if (c.error) return { error: c.error };
          const presets = (p.ok && Array.isArray(p.data) ? p.data : (p.data && p.data.roles) || []);
          return Object.assign(c, { presets });
        },
        render(d) {
          const opts = [['', 'No purpose yet']].concat(d.presets.map((r) => [r.id, r.label || r.id]));
          return selectRows('Purpose', 'role', d.cameras.map((c) => ({ id: c.id, label: c.name || c.id, value: c.role || '' })), opts,
            { empty: 'No cameras yet.', hint: 'Choosing a purpose applies its default AI features to that camera.' });
        },
        async save(form, d) {
          const want = rowValues(form, 'role');
          for (const c of d.cameras) {
            if (!(c.id in want) || (want[c.id] || '') === (c.role || '')) continue;
            const r = await api(`/api/v1/cameras/${encodeURIComponent(c.id)}/role`, { method: 'PUT', json: { role: want[c.id] || null, apply_defaults: true } });
            if (!r.ok) return { ok: false, message: r.message };
          }
          return { ok: true };
        },
        reloads: ['edgeRoles.refresh'],
      },
    },
    {
      id: 'store-map', group: 'cameras', icon: 'map', admin: true,
      title: 'Store plan',
      what: 'A plan of the store in metres: rooms, shelves, checkouts and doors, plus the car park if you like.',
      why: 'Live shoppers, heatmaps and store areas are shown on it, with real sizes: m² per area, shelf lengths, figures per m² and a print-to-scale map.',
      full: { go: 'map-edit', label: 'Store map editor', admin: true },
      setup: {
        title: 'Start',
        load: async () => ({}),
        render: () => `<p class="feat-hint">The plan is drawn by dragging on the map, so it has no settings here.</p><div class="feat-field">${button('draw', 'Start drawing', { icon: 'pencil', primary: true })}</div>`,
        actions: { draw() { closeDrawer(false); if (help()) help().go('map-edit'); } },
      },
    },
    {
      id: 'placement-calibration', group: 'cameras', icon: 'scan', admin: true,
      title: 'Camera placement and calibration',
      what: 'Puts each camera on the plan with how far it sees, and matches its picture to the floor, so people appear in the right place.',
      why: 'Shows what each camera really covers (walls and shelves block it), the blind spots and which shelves it sees; calibrated cameras show live shoppers and feed the heatmaps.',
      full: { go: camLink('calibrate:', 'first_to_calibrate', 'map'), label: 'Calibrate on the Store map', admin: true },
      setup: {
        title: 'Uncalibrated cameras: counts into area',
        async load() {
          const r = await api('/api/v1/layout');
          if (!r.ok) return { error: r.message };
          return { cams: (r.data.cameras || []), zones: r.data.zones || [] };
        },
        render(d) {
          const unc = d.cams.filter((c) => !c.has_homography);
          const opts = [['', 'Not linked']].concat(d.zones.map((z) => [z.id, z.name || z.id]));
          const placed = d.cams.filter((c) => c.map_placed === false).length;
          return (placed ? hint(`${plural(placed, 'camera')} not on the map yet: drag them from the camera gallery on the Store map.`) : '')
            + (d.zones.length ? selectRows('Count everyone this camera sees as a visit to', 'zone', unc.map((c) => ({ id: c.camera_id, label: c.name || c.camera_id, value: c.watch_zone_id || '' })), opts,
              { empty: 'Every camera is calibrated.' })
              : hint('Draw store areas first (see "Store areas"); then you can link uncalibrated cameras to them here.'));
        },
        async save(form, d) {
          const want = rowValues(form, 'zone');
          for (const c of d.cams) {
            if (!(c.camera_id in want) || (want[c.camera_id] || '') === (c.watch_zone_id || '')) continue;
            const r = await api(`/api/v1/layout/cameras/${encodeURIComponent(c.camera_id)}/zone`, { method: 'PUT', json: { zone_id: want[c.camera_id] || null } });
            if (!r.ok) return { ok: false, message: r.message };
          }
          try { if (window.blueprintEditor && typeof window.blueprintEditor.load === 'function') window.blueprintEditor.load(); } catch (_) {}
          return { ok: true };
        },
      },
    },
    {
      id: 'store-areas', group: 'cameras', icon: 'layout-dashboard', admin: true,
      title: 'Store areas',
      what: 'Parts of the floor, such as an aisle, a department or the entrance, where visits and time spent are measured.',
      why: 'Figures and suggestions are given per area, with the products sold there.',
      full: { go: 'map-zone', label: 'Draw a zone', admin: true },
      setup: {
        title: 'Your areas',
        async load() {
          const r = await api('/api/v1/layout');
          return r.ok ? { zones: r.data.zones || [] } : { error: r.message };
        },
        render: (d) => (d.zones.length
          ? `<ul class="feat-rows">${d.zones.map((z) => `<li><span>${esc(z.name || z.id)}</span><span class="badge badge-neutral">${esc(z.category || 'AISLE')}</span></li>`).join('')}</ul>${hint('Change names and categories on the Store map (Full settings).')}`
          : hint('No store areas yet.')) + `<div class="feat-field">${button('draw', 'Draw an area', { icon: 'pencil' })}</div>`,
        actions: { draw() { closeDrawer(false); if (help()) help().go('map-zone'); } },
      },
    },
    {
      id: 'privacy-ignore', group: 'cameras', icon: 'eye-off', admin: true,
      title: 'Privacy masks and ignore areas',
      what: 'Privacy masks hide part of a picture everywhere it is shown. Ignore areas stop the AI looking at posters, mannequins or screens.',
      why: 'Protects privacy and stops false counts and false alerts from things that look like people.',
      full: { go: studio('mask'), label: 'Camera setup: masks', admin: true },
      setup: {
        title: 'Figures that never move',
        load: camerasLoad,
        render: (d) => checkRows('Ignore figures that never move on these cameras', 'cam', camRows(d.cameras, 'static_figure_filter'),
          { empty: 'No cameras yet.', hint: 'A figure with no movement for 60 seconds is shown as "static" and not counted.' }),
        save: (form, d) => saveCameraFlag(d.cameras, 'static_figure_filter', rowValues(form, 'cam', 'check')),
      },
    },
    {
      id: 'sub-streams', group: 'cameras', icon: 'video', admin: true,
      title: 'Recorder picture quality',
      what: 'Upgrades a recorder\'s very small sub-streams (CIF) to D1, so the AI sees far-away shoppers.',
      why: 'Small pictures miss people at the far end of an aisle. It changes one channel at a time and can be undone.',
      full: { go: 'settings:settings-recorder-streams', label: 'Recorder sub-streams', admin: true },
      setup: null,
    },

    // ===================================================== Access and phones
    {
      id: 'online-access', group: 'access', icon: 'monitor', plan: 'online_access', admin: true,
      title: 'Online access',
      what: 'Opens this dashboard away from the store at the store\'s own web address, without opening the router.',
      why: 'Check the store from home, and phone alerts work anywhere. Video never passes through iKorex.',
      full: { go: 'settings:settings-remote', label: 'Online access', admin: true },
      setup: {
        title: 'On or off',
        async load() {
          const r = await api('/api/v1/remote-access');
          return r.ok ? { ra: r.data } : { error: r.message };
        },
        render(d) {
          const ready = d.ra.server_url && d.ra.store_id && d.ra.token_configured;
          const words = { connected: 'Connected', starting: 'Connecting', error: 'Error', stopped: 'Stopped' };
          return `<p class="feat-hint">Now: <b>${esc(words[d.ra.process] || d.ra.process || 'Stopped')}</b>${d.ra.public_url ? ` at ${esc(d.ra.public_url)}` : ''}${d.ra.verified ? ' (verified)' : ''}.</p>`
            + (ready ? checkField('ra_enabled', 'Turn online access on', !!d.ra.enabled)
              : hint('The server, store ID and token are not filled in yet. Ask your installer, or use Full settings.'))
            + (d.ra.enabled ? `<div class="feat-field">${button('verify', 'Verify now', { icon: 'circle-check' })}</div>` : '');
        },
        async save(form, d) {
          if (!form.elements.namedItem('ra_enabled')) return { ok: true, message: 'Nothing changed.' };
          const on = checked(form, 'ra_enabled');
          if (on === !!d.ra.enabled) return { ok: true, message: 'Nothing changed.' };
          const r = await api('/api/v1/remote-access', { method: 'PUT', json: { enabled: on } });
          return r.ok ? { ok: true, message: on ? 'Saved. Connecting can take a few seconds.' : 'Saved. Online access is off.' } : { ok: false, message: r.message };
        },
        actions: {
          async verify(btn) {
            btn.disabled = true;
            setMsg('Checking the public address…');
            const r = await api('/api/v1/remote-access/verify', { method: 'POST' });
            btn.disabled = false;
            setMsg(r.ok && r.data && r.data.verified !== false ? 'Verified: the public address reaches this device.' : (r.ok ? (r.data && (r.data.verify_error || r.data.message)) || 'Not verified yet.' : r.message), r.ok && r.data && r.data.verified !== false ? 'ok' : 'error');
          },
        },
        reloads: ['edgeRemoteAccess'],
      },
    },
    {
      id: 'remote-video', group: 'access', icon: 'video', plan: 'remote_live_video', admin: true,
      title: 'Live video away from the store',
      what: 'Plays live camera video straight from this device to your browser when you are away, only while you watch.',
      why: 'See what is happening right now from anywhere. Privacy masks are applied on this device first.',
      full: { go: 'settings:settings-remote-video', label: 'Live video', admin: true },
      setup: {
        title: 'Live video',
        async load() {
          const r = await api('/api/v1/remote-access');
          return r.ok ? { ra: r.data } : { error: r.message };
        },
        render: (d) => numField('max_video_sessions', 'Most live videos at once', d.ra.max_video_sessions, { min: 1, max: 32, hint: 'Each one uses upload bandwidth of the store\'s internet.' })
          + selectField('live_transport_on_lan', 'In the store, cameras play', d.ra.live_transport_on_lan || 'local', [['local', 'Store-network pictures (default)'], ['webrtc', 'Direct live video, like away']]),
        async save(form, d) {
          const body = { max_video_sessions: numOrNull(val(form, 'max_video_sessions')), live_transport_on_lan: val(form, 'live_transport_on_lan') };
          if (body.max_video_sessions === d.ra.max_video_sessions && body.live_transport_on_lan === (d.ra.live_transport_on_lan || 'local')) return { ok: true, message: 'Nothing changed.' };
          const r = await api('/api/v1/remote-access', { method: 'PUT', json: body });
          return r.ok ? { ok: true } : { ok: false, message: r.message };
        },
        reloads: ['edgeRemoteVideo'],
      },
    },
    {
      id: 'accounts', group: 'access', icon: 'users', admin: true,
      title: 'Staff accounts',
      what: 'Gives each person their own sign-in. Operators use the system day to day but cannot change setup.',
      why: 'You can see who handled an alert, and staff cannot change settings by mistake.',
      full: { go: 'settings:settings-accounts', label: 'Accounts', admin: true },
      setup: {
        title: 'Add an account',
        load: async () => ({}),
        render: () => textField('acct_user', 'Username', '', { max: 64, autocomplete: 'off' })
          + textField('acct_name', 'Name', '', { max: 128 })
          + selectField('acct_role', 'Role', 'operator', [['operator', 'Operator (staff)'], ['admin', 'Administrator']].concat(account().role === 'owner' ? [['owner', 'Owner']] : []))
          + textField('acct_pw', 'First password', '', { type: 'password', autocomplete: 'new-password', hint: 'At least 8 characters. The person changes it after signing in.' }),
        async save(form) {
          const username = (val(form, 'acct_user') || '').trim();
          if (!username) return { ok: false, message: 'Enter a username.' };
          const r = await api('/api/v1/users', { method: 'POST', json: { username, display_name: (val(form, 'acct_name') || '').trim(), role: val(form, 'acct_role') || 'operator', password: val(form, 'acct_pw') || '' } });
          return r.ok ? { ok: true, message: `Account "${username}" created. Give them the password.` } : { ok: false, message: r.message };
        },
        reloads: ['edgeUsers'],
      },
    },
    {
      id: 'phone-app', group: 'access', icon: 'smartphone', plan: 'phone_alerts', admin: true,
      title: 'Phone app (backup)',
      what: 'Pairs the older native phone app with a one-time code, as a backup to phone alerts.',
      why: 'Only for stores that already use the native app; most stores use phone alerts instead.',
      full: { go: 'settings:settings-pairing', label: 'Phone app (backup)', admin: true },
      setup: null,
    },

    // ===================================================== System
    {
      id: 'store-settings', group: 'system', icon: 'store', admin: true,
      title: 'Store name and time zone',
      what: 'The store\'s name on every page and report, and the time zone all times and schedules use.',
      why: '"Today", night watch, alert times and the nightly backup all follow the store\'s own clock.',
      full: { go: 'settings:settings-store', label: 'Store', admin: true },
      setup: {
        title: 'Store',
        async load() {
          const [s, t] = await Promise.all([siteLoad(), api('/api/v1/site-settings/timezones')]);
          if (s.error) return s;
          return Object.assign(s, { zones: (t.ok && t.data && t.data.timezones) || [] });
        },
        render: (d) => textField('STORE_NAME', 'Store name', sv(d, 'STORE_NAME'), { max: 80 })
          + textField('SITE_TIMEZONE', 'Time zone', sv(d, 'SITE_TIMEZONE'), { list: 'featTzList', hint: `Empty uses this device's own zone${d.storeTime.zone ? ` (now ${d.storeTime.zone})` : ''}.` })
          + `<datalist id="featTzList">${d.zones.map((z) => `<option value="${esc(z)}"></option>`).join('')}</datalist>`,
        save(form, d) {
          const tz = (val(form, 'SITE_TIMEZONE') || '').trim();
          if (tz && d.zones.length && !d.zones.includes(tz)) return { ok: false, message: 'Pick the time zone from the list, for example Australia/Melbourne.' };
          return siteSave(d, { STORE_NAME: (val(form, 'STORE_NAME') || '').trim(), SITE_TIMEZONE: tz });
        },
        reloads: ['edgeSiteSettings'],
      },
    },
    {
      id: 'evidence', group: 'system', icon: 'film', admin: true,
      title: 'Evidence storage',
      what: 'Keeps a picture (and optionally a short clip) of each alert on this device, deleting the oldest first.',
      why: 'You can look back at what happened. The store\'s own recorder keeps the full recording.',
      full: { go: 'settings:settings-evidence', label: 'Evidence storage', admin: true },
      setup: {
        title: 'How much is kept',
        load: siteLoad,
        render: (d) => numField('EVIDENCE_MAX_GB', 'Evidence size limit', n2(sv(d, 'EVIDENCE_MAX_GB')), { min: 0, max: (d.site.EVIDENCE_MAX_GB || {}).max, step: 0.5, unit: 'GB', hint: `0 = Automatic${d.disk.auto_evidence_gb ? ` (${d.disk.auto_evidence_gb} GB on this disk)` : ' (10% of the disk)'}.` })
          + numField('STORAGE_RETENTION_DAYS', 'Keep evidence for', n2(sv(d, 'STORAGE_RETENTION_DAYS')), { min: 0, max: 730, unit: 'days', hint: '0 = no age limit.' })
          + checkField('NIGHT_WATCH_CLIP', 'Save a clip with each night-watch event', !!sv(d, 'NIGHT_WATCH_CLIP')),
        save(form, d) {
          const gb = numOrNull(val(form, 'EVIDENCE_MAX_GB'));
          const days = numOrNull(val(form, 'STORAGE_RETENTION_DAYS'));
          const oldGb = Number(sv(d, 'EVIDENCE_MAX_GB') || 0), oldDays = Number(sv(d, 'STORAGE_RETENTION_DAYS') || 0);
          const lower = (gb !== null && gb > 0 && (oldGb === 0 || gb < oldGb)) || (days !== null && days > 0 && (oldDays === 0 || days < oldDays));
          if (lower && !d.confirmed) {
            d.confirmed = true;
            return { ok: false, message: 'Lowering a limit deletes the oldest evidence at once. Press Save again to confirm.' };
          }
          d.confirmed = false;
          return siteSave(d, { EVIDENCE_MAX_GB: gb, STORAGE_RETENTION_DAYS: days, NIGHT_WATCH_CLIP: checked(form, 'NIGHT_WATCH_CLIP') });
        },
        reloads: ['edgeSiteSettings'],
      },
    },
    {
      id: 'backups', group: 'system', icon: 'save', admin: true,
      title: 'Backups',
      what: 'Copies of the layout, cameras, settings, accounts and figures, taken at every start and every night.',
      why: 'After a fault or a mistake the store is back as it was in minutes. Keep a downloaded copy off the device.',
      full: { go: 'settings:settings-backups', label: 'Backups and reset', admin: true },
      setup: {
        title: 'Back up',
        async load() {
          const r = await api('/api/v1/system/backups');
          return r.ok ? { list: (r.data && r.data.backups) || [] } : { error: r.message };
        },
        render: (d) => `<p class="feat-hint">${d.list.length ? `${plural(d.list.length, 'backup')} on this device; the newest: ${esc(d.list[0].time_label || d.list[0].time_local || d.list[0].created_at || '')} (${esc(d.list[0].kind || '')}).` : 'No backups yet.'}</p>
          <div class="feat-field">${button('backup', 'Back up now', { icon: 'save', primary: true })}</div>`,
        actions: {
          async backup(btn, d) {
            btn.disabled = true;
            setMsg('Backing up…');
            const r = await api('/api/v1/system/backup', { method: 'POST', json: { tag: 'manual' } });
            btn.disabled = false;
            if (!r.ok) { setMsg(r.message, 'error'); return; }
            const l = await api('/api/v1/system/backups');
            if (l.ok) d.list = l.data.backups || [];
            $('featDrawerFields').innerHTML = drawerFeature.setup.render(d, ctx());
            setMsg('Backed up. Now open Full settings and press "Download" to keep a copy off the device.', 'ok');
            refresh();
          },
        },
      },
    },
    {
      id: 'analysis-speed', group: 'system', icon: 'activity', admin: true,
      title: 'Analysis speed',
      what: 'How much of this device goes to analysing the cameras, and how many pictures a second each camera gets.',
      why: 'The defaults suit this hardware. Lower them if the device is slow; raise them for quicker reactions.',
      full: { go: 'settings:settings-analysis', label: 'Analysis speed', admin: true },
      setup: {
        load: siteLoad,
        render: (d) => numField('POSE_BUDGET_UTILISATION', 'Share of the device used for detection', pctOf(sv(d, 'POSE_BUDGET_UTILISATION')), Object.assign(siteRange(d, 'POSE_BUDGET_UTILISATION', true), { hint: siteHint(d, 'POSE_BUDGET_UTILISATION') }))
          + numField('ANALYTICS_MAX_DETECT_FPS', 'Most pictures analysed per second, per camera', n2(sv(d, 'ANALYTICS_MAX_DETECT_FPS')), Object.assign(siteRange(d, 'ANALYTICS_MAX_DETECT_FPS'), { step: 0.5, unit: 'per second' })),
        save(form, d) {
          const p = numOrNull(val(form, 'POSE_BUDGET_UTILISATION'));
          return siteSave(d, { POSE_BUDGET_UTILISATION: p === null ? null : Math.round(p) / 100, ANALYTICS_MAX_DETECT_FPS: numOrNull(val(form, 'ANALYTICS_MAX_DETECT_FPS')) });
        },
        reloads: ['edgeSiteSettings'],
      },
    },
    {
      id: 'support', group: 'system', icon: 'circle-help',
      title: 'Help and support',
      what: 'Short how-to guides, the iKorex support contact, and "Report a problem" from any screen.',
      why: 'Get an answer quickly, and tell iKorex what is wrong without sending any pictures.',
      full: { go: 'settings:settings-support', label: 'Help and support' },
      setup: {
        title: 'Ask for help',
        load: async () => ({}),
        render: () => `<div class="feat-inline">${button('report', 'Report a problem', { icon: 'circle-alert', primary: true, operator: true })}${button('reports', 'My reports', { operator: true })}${button('guides', 'Open the guides', { icon: 'circle-help', operator: true })}</div>`,
        actions: {
          report() { closeDrawer(false); if (window.EdgeSupport) window.EdgeSupport.openReport('features'); },
          reports() { closeDrawer(false); if (window.EdgeSupport) window.EdgeSupport.openReports('features'); },
          guides() { closeDrawer(false); if (help()) help().open(); },
        },
      },
    },
    {
      id: 'ikorex-plan', group: 'system', icon: 'shield-check',
      title: 'iKorex plan',
      what: 'Your service with iKorex: whether it is active, the licence date, the camera limit and the features included.',
      why: 'You can see at a glance what is included. Losing the internet never pauses anything.',
      full: { go: 'settings:settings-fleet', label: 'iKorex service' },
      setup: {
        title: 'Your plan',
        load: async () => {
          const st = window.edgeFleet && window.edgeFleet.status ? window.edgeFleet.status() : null;
          return { fleet: st };
        },
        render(d) {
          const f = d.fleet || {};
          const labels = f.feature_labels || {};
          const off = Object.keys(f.features || {}).filter((k) => f.features[k] === false).map((k) => labels[k] || k);
          return `<p class="feat-hint">${f.managed || f.configured ? `Plan: <b>${esc(f.paused ? 'Paused' : 'Active')}</b>${f.licence_valid_until ? `, licence until ${esc(f.licence_valid_until)}` : ''}${f.limits && f.limits.max_cameras ? `, up to ${esc(f.limits.max_cameras)} cameras` : ''}.` : 'Not connected to iKorex: every feature is on.'}</p>`
            + (off.length ? `<p class="feat-hint">Not in your plan: ${esc(off.join(', '))}.</p>` : '')
            + (isAdmin() ? `<div class="feat-field">${button('checkin', 'Check in now', { icon: 'refresh-cw' })}</div>` : '');
        },
        actions: {
          async checkin(btn) {
            btn.disabled = true;
            const r = await api('/api/v1/fleet/checkin-now', { method: 'POST' });
            btn.disabled = false;
            if (window.edgeFleet) await window.edgeFleet.reload();
            setMsg(r.ok ? (r.data && r.data.last_result === 'ok' ? 'Checked in with iKorex.' : `Not reached: ${(r.data && r.data.last_error) || 'no answer.'}`) : r.message, r.ok && r.data && r.data.last_result === 'ok' ? 'ok' : 'error');
            refresh();
          },
        },
      },
    },
  ];
  /* FEATURES:END */

  // ======================================================================== state
  let status = null;          // last GET /api/v1/features/status
  let statusError = '';
  let loading = false;
  let timer = null;
  let filter = 'all';
  let query = '';
  const byId = (id) => FEATURES.find((f) => f.id === id);

  function visible() {
    const v = $('tab-features');
    return !!v && v.classList.contains('active') && !document.hidden;
  }

  /** The state shown for a feature: plan first, then the server's state. */
  function stateOf(f) {
    const plan = planFeatures();
    const s = (status && status.features && status.features[f.id]) || null;
    if ((f.plan && plan[f.plan] === false) || (s && s.state === 'plan_off')) return { state: 'plan_off', detail: '' };
    if (!s) return { state: 'unknown', detail: statusError ? `Could not be checked: ${statusError}` : 'Checking…' };
    return { state: s.state || 'unknown', detail: s.detail || '' };
  }

  const CHIP = {
    on: ['badge-success', 'circle-check', 'On'],
    off: ['badge-neutral', 'circle', 'Off'],
    setup: ['badge-warning', 'circle-alert', 'Needs setup'],
    plan_off: ['badge-neutral', 'lock', 'Not in your plan'],
    unknown: ['badge-neutral', 'circle-help', 'Not checked'],
    admin: ['badge-info', 'lock', 'Admin only'],
  };

  function chipHtml(key) {
    const [cls, icon, word] = CHIP[key] || CHIP.unknown;
    return `<span class="badge ${cls} feat-chip">${ico(icon, 'sm')}${esc(word)}</span>`;
  }

  const STATE_WORDS = { on: 'On', off: 'Off', setup: 'Needs setup', unknown: 'Not checked' };

  /** The chip a person sees: operators see "Admin only" on cards they cannot set up. */
  function chipKey(f, st) {
    if (st.state === 'plan_off') return 'plan_off';
    if (f.admin && !f.operatorUse && !isAdmin()) return 'admin';
    return st.state;
  }

  // ======================================================================== render
  function matches(f) {
    if (filter === 'setup' && stateOf(f).state !== 'setup') return false;
    if (filter === 'on' && stateOf(f).state !== 'on') return false;
    const words = query.toLowerCase().split(/[^a-z0-9-]+/).filter((w) => w.length > 1);
    if (!words.length) return true;
    const g = guideOf(f);
    const hay = [f.title, f.what, f.why, g.keywords || '', (g.steps || []).join(' ')].join(' ').toLowerCase();
    return words.every((w) => hay.includes(w));
  }

  function fullAllowed(f) {
    if (!f.full) return false;
    if (f.full.admin && !isAdmin()) return false;
    return true;
  }

  function cardHtml(f) {
    const st = stateOf(f);
    const key = chipKey(f, st);
    const planOff = st.state === 'plan_off';
    const notes = [];
    if (planOff) {
      notes.push(`<p class="feat-note">${ico('lock', 'sm')}<span>Not included in your iKorex plan. Please contact iKorex to add it.</span></p>`);
    } else if (key === 'admin') {
      notes.push(`<p class="feat-note">${ico('info', 'sm')}<span>An owner or administrator sets this up. Now: ${esc(STATE_WORDS[st.state] || 'Not checked')}.</span></p>`);
    }
    const full = f.full || {};
    const fullOk = !planOff && fullAllowed(f);
    const fullTitle = planOff ? 'Not included in your iKorex plan'
      : (!fullAllowed(f) ? 'Only an owner or administrator can open these settings' : `Open ${full.label || 'the full settings'}`);
    return `
      <li class="feat-card is-${esc(st.state === 'plan_off' ? 'plan-off' : st.state)}" id="feature-${esc(f.id)}" data-feature="${esc(f.id)}" tabindex="-1" aria-labelledby="featT-${esc(f.id)}">
        <div class="feat-card-head">
          <h3 class="feat-card-title" id="featT-${esc(f.id)}">${ico(f.icon || 'circle')}<span>${esc(f.title)}</span></h3>
          ${chipHtml(key)}
        </div>
        <p class="feat-what">${esc(f.what)}</p>
        <p class="feat-why"><b>Why it helps:</b> ${esc(f.why)}</p>
        <p class="feat-detail">${planOff ? '' : esc(st.detail)}</p>
        ${notes.join('')}
        <div class="feat-actions">
          ${planOff
    ? `<button type="button" class="btn btn-sm btn-primary" disabled aria-disabled="true" title="Not included in your iKorex plan">${ico('settings', 'sm')}Easy setup</button>
             <button type="button" class="btn btn-sm" disabled aria-disabled="true" title="Not included in your iKorex plan">Full settings</button>
             <button type="button" class="btn btn-sm btn-ghost" data-feat-contact>${ico('circle-help', 'sm')}Contact iKorex</button>`
    : `<button type="button" class="btn btn-sm${st.state === 'setup' && key !== 'admin' ? ' btn-primary' : ''}" data-feat-setup="${esc(f.id)}" aria-haspopup="dialog">${ico('settings', 'sm')}Easy setup</button>
             <button type="button" class="btn btn-sm${fullOk ? '' : ''}" data-feat-full="${esc(f.id)}"${fullOk ? '' : ' disabled aria-disabled="true"'} title="${esc(fullTitle)}">Full settings${ico('arrow-right', 'sm')}</button>`}
        </div>
      </li>`;
  }

  function render() {
    const host = $('featureGroups');
    if (!host) return;
    if (!FEATURES.length) { host.innerHTML = '<div class="fp-empty">No features.</div>'; return; }
    let shown = 0;
    const html = GROUPS.map((g) => {
      const list = FEATURES.filter((f) => f.group === g.id && matches(f));
      shown += list.length;
      if (!list.length) return '';
      return `
        <section class="feat-group" aria-labelledby="featG-${esc(g.id)}">
          <h2 class="feat-group-title" id="featG-${esc(g.id)}">${ico(g.icon)}${esc(g.title)}</h2>
          <p class="feat-group-sub">${esc(g.sub)}</p>
          <ul class="feat-grid">${list.map(cardHtml).join('')}</ul>
        </section>`;
    }).join('');
    host.innerHTML = shown ? html : `
      <div class="empty-state feat-empty">${ico('search', 'lg')}
        <div class="empty-state-title">${filter === 'setup' && !query ? 'Nothing needs setting up' : `No feature matches${query ? ` "${esc(query)}"` : ''}`}</div>
        <div class="empty-state-text">${filter === 'setup' && !query
    ? 'Every feature in your plan is set up. Optional features that are off are under "All".'
    : 'Try one simple word such as camera, phone, alert, map or backup.'}</div>
        <button type="button" class="btn btn-sm" data-feat-reset>Show all features</button>
      </div>`;
    renderSummary();
  }

  function counts() {
    const c = { total: FEATURES.length, on: 0, off: 0, setup: 0, plan_off: 0, unknown: 0 };
    FEATURES.forEach((f) => { const s = stateOf(f).state; c[s] = (c[s] || 0) + 1; });
    return c;
  }

  function renderSummary() {
    const c = counts();
    const sum = $('featSummary');
    if (sum) {
      const parts = [`${c.total} features`, `${c.on} on`];
      if (c.setup) parts.push(`${c.setup} ${c.setup === 1 ? 'needs' : 'need'} setup`);
      if (c.off) parts.push(`${c.off} off`);
      if (c.plan_off) parts.push(`${c.plan_off} not in your plan`);
      sum.textContent = statusError ? `Status could not be checked: ${statusError}` : (status ? parts.join(' · ') : 'Checking…');
    }
    const n = $('featNeedsCount');
    if (n) n.textContent = c.setup ? String(c.setup) : '';
    const badge = $('tabFeaturesSetupBadge');
    if (badge) {
      const show = isAdmin() && c.setup > 0 && !!status;
      badge.hidden = !show;
      badge.textContent = String(c.setup);
      badge.title = `${plural(c.setup, 'feature needs', 'features need')} setting up`;
    }
  }

  // Coming back to the tab re-reads the status only when it is older than this, so
  // switching tabs quickly never adds bursts to the shared 100 requests/min budget.
  const REVISIT_MS = 15000;
  let loadedAt = 0;

  async function refresh(force) {
    if (loading) return;
    if (force === 'revisit' && status && Date.now() - loadedAt < REVISIT_MS) return;
    if (!(window.edgeAuth && window.edgeAuth.isAuthenticated && window.edgeAuth.isAuthenticated())) return;
    loading = true;
    try {
      const r = await api(STATUS_URL);
      if (r.ok && r.data) { status = r.data; statusError = ''; loadedAt = Date.now(); } else { statusError = r.message || 'no answer.'; }
    } finally {
      loading = false;
    }
    render();
    if (drawerFeature) refreshDrawerChip();
  }

  function schedule() {
    clearInterval(timer);
    timer = setInterval(() => { if (visible()) refresh(); }, POLL_MS);
  }

  // ===================================================================== drawer
  let drawer = null;
  let scrim = null;
  let opener = null;
  let drawerFeature = null;
  let drawerData = null;
  let saving = false;

  function ensureDrawer() {
    if (drawer) return drawer;
    scrim = document.createElement('div');
    scrim.className = 'feat-scrim';
    scrim.hidden = true;
    scrim.addEventListener('click', () => closeDrawer());
    drawer = document.createElement('aside');
    drawer.id = 'featureDrawer';
    drawer.className = 'feat-drawer';
    drawer.setAttribute('role', 'dialog');
    drawer.setAttribute('aria-modal', 'true');
    drawer.setAttribute('aria-labelledby', 'featDrawerTitle');
    drawer.hidden = true;
    drawer.innerHTML = `
      <div class="feat-drawer-head">
        <h2 class="feat-drawer-title" id="featDrawerTitle" tabindex="-1"></h2>
        <button type="button" class="btn btn-ghost btn-icon btn-sm" data-fd="close" aria-label="Close easy setup">${ico('x')}</button>
      </div>
      <div class="feat-drawer-body" id="featDrawerBody"></div>
      <div class="feat-drawer-foot" id="featDrawerFoot"></div>`;
    document.body.appendChild(scrim);
    document.body.appendChild(drawer);
    drawer.addEventListener('click', onDrawerClick);
    drawer.addEventListener('submit', (e) => { e.preventDefault(); save(); });
    drawer.addEventListener('change', (e) => {
      if (drawerFeature && drawerFeature.setup && typeof drawerFeature.setup.onChange === 'function') {
        try { drawerFeature.setup.onChange(e, drawerData, ctx()); } catch (_) { /* form not ready */ }
      }
    });
    drawer.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeDrawer(); return; }
      if (e.key !== 'Tab') return;
      const items = [...drawer.querySelectorAll(FOCUSABLE)].filter((n) => n.offsetParent !== null || n === document.activeElement);
      if (!items.length) return;
      const first = items[0], last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    });
    return drawer;
  }

  /** What the Easy setup forms get: helpers and who is signed in. */
  function ctx() {
    return { api, esc, ico, plural, admin: isAdmin(), canEdit: canEdit(drawerFeature), msg: setMsg, reloadStatus: refresh };
  }

  function canEdit(f) {
    if (!f) return false;
    if (stateOf(f).state === 'plan_off') return false;
    return !f.admin || isAdmin();
  }

  function setMsg(text, kind) {
    const m = $('featDrawerMsg');
    if (!m) return;
    m.textContent = text || '';
    m.className = `feat-msg${kind === 'ok' ? ' is-ok' : kind === 'error' ? ' is-error' : ''}`;
  }

  function refreshDrawerChip() {
    const slot = $('featDrawerChip');
    if (slot && drawerFeature) slot.innerHTML = `${chipHtml(chipKey(drawerFeature, stateOf(drawerFeature)))} <span class="feat-detail">${esc(stateOf(drawerFeature).detail)}</span>`;
  }

  async function openDrawer(id, from) {
    const f = byId(id);
    if (!f) return;
    ensureDrawer();
    if (drawer.hidden) opener = from || document.activeElement;
    drawerFeature = f;
    drawerData = null;
    const st = stateOf(f);
    const planOff = st.state === 'plan_off';
    const edit = canEdit(f);
    $('featDrawerTitle').innerHTML = `${ico(f.icon || 'circle')}<span>${esc(f.title)}</span>`;
    const locked = planOff
      ? `<div class="feat-locked" role="note">${ico('lock', 'sm')}<span>${esc(f.title)} is not included in your iKorex plan, so it cannot be set up. Please contact iKorex to add it.</span></div>`
      : (!edit ? `<div class="feat-locked" role="note">${ico('lock', 'sm')}<span>${esc(f.lockedNote || ADMIN_ONLY_MSG)}</span></div>` : '');
    $('featDrawerBody').innerHTML = `
      <p class="feat-what">${esc(f.what)}</p>
      <div id="featDrawerChip"></div>
      ${locked}
      <section class="feat-guide" aria-labelledby="featGuideTitle">
        <h3 class="feat-guide-title" id="featGuideTitle">${ico('info', 'sm')}How to set it up</h3>
        <ol>${(guideOf(f).steps || []).map((s) => `<li>${esc(s)}</li>`).join('')}</ol>
        <button type="button" class="link-btn" data-fd="guide">Open this guide in Help</button>
      </section>
      <form class="feat-form" id="featDrawerForm" novalidate aria-labelledby="featFormTitle">
        <h3 class="feat-form-title" id="featFormTitle">${esc((f.setup && f.setup.title) || 'Main settings')}</h3>
        <div id="featDrawerFields"><p class="feat-hint feat-loading">Loading the current settings…</p></div>
      </form>
      <p class="feat-msg" id="featDrawerMsg" role="status" aria-live="polite"></p>`;
    const full = f.full || {};
    const fullOk = !planOff && fullAllowed(f);
    const hasSave = !!(f.setup && f.setup.save) && edit;
    $('featDrawerFoot').innerHTML = `
      ${hasSave ? `<button type="submit" form="featDrawerForm" class="btn btn-sm btn-primary" id="featDrawerSave">${ico('save', 'sm')}Save</button>` : ''}
      <button type="button" class="btn btn-sm" data-fd="close">Close</button>
      ${planOff ? '<button type="button" class="btn btn-sm btn-ghost" data-feat-contact>Contact iKorex</button>' : ''}
      ${fullOk ? `<button type="button" class="btn btn-sm feat-full" data-fd="full">${esc(full.label ? `Full settings: ${full.label}` : 'Full settings')}${ico('arrow-right', 'sm')}</button>`
    : (!planOff && f.full ? '<span class="feat-hint feat-full">Full settings: owners and administrators only.</span>' : '')}`;
    refreshDrawerChip();
    drawer.hidden = false;
    scrim.hidden = false;
    document.documentElement.classList.add('help-open');
    $('featDrawerTitle').focus({ preventScroll: true });
    if (planOff) { $('featDrawerFields').innerHTML = '<p class="feat-hint">Nothing to set while it is not in your plan.</p>'; return; }
    await loadDrawer(f);
  }

  async function loadDrawer(f) {
    const fields = $('featDrawerFields');
    if (!f.setup || typeof f.setup.render !== 'function') {
      fields.innerHTML = '<p class="feat-hint">There is nothing to set here: use the guide above, or Full settings.</p>';
      return;
    }
    let data = null;
    try {
      data = typeof f.setup.load === 'function' ? await f.setup.load(ctx()) : {};
    } catch (_) { data = { error: 'Could not be read.' }; }
    if (drawerFeature !== f) return;      // another feature was opened meanwhile
    drawerData = data;
    if (data && data.error) {
      fields.innerHTML = `<p class="feat-msg is-error">${esc(data.error)}</p>`;
      const s = $('featDrawerSave');
      if (s) s.disabled = true;
      return;
    }
    let html = '';
    try { html = f.setup.render(data, ctx()); } catch (_) { html = '<p class="feat-msg is-error">The settings could not be shown.</p>'; }
    fields.innerHTML = html;
    if (!canEdit(f)) {
      fields.querySelectorAll('input, select, textarea, button').forEach((n) => {
        if (!n.hasAttribute('data-operator-ok')) n.disabled = true;
      });
    }
    if (typeof f.setup.after === 'function') { try { f.setup.after(data, ctx()); } catch (_) { /* optional */ } }
  }

  async function save() {
    const f = drawerFeature;
    if (!f || saving || !f.setup || !f.setup.save || !canEdit(f)) return;
    const form = $('featDrawerForm');
    const btn = $('featDrawerSave');
    saving = true;
    if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }
    setMsg('Saving…');
    let r;
    try { r = await f.setup.save(form, drawerData, ctx()); } catch (e) { r = { ok: false, message: 'The settings could not be saved. Try again in a moment.' }; }
    saving = false;
    if (btn && btn.isConnected) { btn.disabled = false; btn.innerHTML = `${ico('save', 'sm')}Save`; }
    if (!r || !r.ok) { setMsg((r && r.message) || 'Not saved.', 'error'); return; }
    setMsg(r.message || 'Saved. It applies at once.', 'ok');
    // Tell the module that owns the full control to read again, so "Full settings" shows the change.
    (f.setup.reloads || []).forEach((name) => {
      try {
        const [obj, method] = name.split('.');
        const m = window[obj];
        const fn = m && m[method || 'reload'];
        if (typeof fn === 'function') fn.call(m);
      } catch (_) { /* module not loaded */ }
    });
    await refresh();
    if (!r.keepForm && drawerFeature === f) {
      const keep = $('featDrawerMsg') ? $('featDrawerMsg').textContent : '';
      await loadDrawer(f);
      setMsg(keep, 'ok');
    }
  }

  function closeDrawer(restoreFocus = true) {
    if (!drawer || drawer.hidden) return;
    drawer.hidden = true;
    scrim.hidden = true;
    document.documentElement.classList.remove('help-open');
    drawerFeature = null;
    drawerData = null;
    const back = opener;
    opener = null;
    if (restoreFocus && back && back.isConnected && typeof back.focus === 'function') back.focus({ preventScroll: true });
  }

  function goFull(f, data) {
    if (!f || !f.full || !fullAllowed(f)) return;
    const exact = data && typeof f.fullFor === 'function' ? f.fullFor(data) : null;
    const target = exact || (typeof f.full.go === 'function' ? f.full.go(status) : f.full.go);
    if (help()) help().go(target);
  }

  function onDrawerClick(e) {
    const t = e.target.closest('[data-fd], [data-feat-contact], [data-fd-action]');
    if (!t) return;
    if (t.hasAttribute('data-feat-contact')) { closeDrawer(false); contact(); return; }
    if (t.hasAttribute('data-fd-action')) {
      const f = drawerFeature;
      if (f && f.setup && f.setup.actions && typeof f.setup.actions[t.getAttribute('data-fd-action')] === 'function') {
        e.preventDefault();
        Promise.resolve(f.setup.actions[t.getAttribute('data-fd-action')](t, drawerData, ctx())).catch(() => setMsg('That did not work. Try again in a moment.', 'error'));
      }
      return;
    }
    const what = t.getAttribute('data-fd');
    if (what === 'close') closeDrawer();
    else if (what === 'guide') { const f = drawerFeature; closeDrawer(false); if (help() && f) help().open(`fs-${f.id}`); }
    else if (what === 'full') { const f = drawerFeature, d = drawerData; closeDrawer(false); goFull(f, d); }
  }

  function contact() {
    if (help()) help().open('ikorex-service');
  }

  // ===================================================================== wiring
  function onMainClick(e) {
    const s = e.target.closest('[data-feat-setup]');
    if (s) { openDrawer(s.getAttribute('data-feat-setup'), s); return; }
    const full = e.target.closest('[data-feat-full]');
    if (full) { goFull(byId(full.getAttribute('data-feat-full'))); return; }
    if (e.target.closest('[data-feat-contact]')) { contact(); return; }
    const chip = e.target.closest('[data-feat-filter]');
    if (chip) { setFilter(chip.getAttribute('data-feat-filter')); return; }
    if (e.target.closest('[data-feat-reset]')) {
      query = '';
      const input = $('featureSearch');
      if (input) input.value = '';
      setFilter('all');
    }
  }

  function setFilter(v) {
    filter = v === 'setup' || v === 'on' ? v : 'all';
    document.querySelectorAll('[data-feat-filter]').forEach((b) => {
      const on = b.getAttribute('data-feat-filter') === filter;
      b.classList.toggle('active', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    render();
  }

  /** Open the Features tab at one card (Help, the checklist, empty states). */
  function show(id, opts) {
    const o = opts || {};
    if (typeof window.switchTab === 'function') window.switchTab('features');
    if (filter !== 'all' || query) {
      query = '';
      const input = $('featureSearch');
      if (input) input.value = '';
      setFilter('all');
    }
    const f = byId(id);
    setTimeout(() => {
      const card = $(`feature-${id}`);
      if (card) {
        card.scrollIntoView({ behavior: 'instant', block: 'center' });
        card.classList.add('flash-highlight');
        setTimeout(() => card.classList.remove('flash-highlight'), 1600);
        if (!o.setup) card.focus({ preventScroll: true });
      }
      if (o.setup && f && stateOf(f).state !== 'plan_off') openDrawer(id, card && card.querySelector('[data-feat-setup]'));
    }, 120);
  }

  function init() {
    const tab = $('tab-features');
    if (tab) tab.addEventListener('click', onMainClick);
    const input = $('featureSearch');
    if (input) {
      input.addEventListener('input', () => { query = input.value || ''; render(); });
      input.addEventListener('keydown', (e) => { if (e.key === 'Escape' && input.value) { e.preventDefault(); input.value = ''; query = ''; render(); } });
    }
    window.addEventListener('edge:tab', (e) => {
      if (e.detail && e.detail.tab === 'features') { render(); refresh('revisit'); }
      else closeDrawer(false);
    });
    window.addEventListener('edge:account', () => render());
    document.addEventListener('visibilitychange', () => { if (visible()) refresh('revisit'); });
    render();
    refresh();
    schedule();
  }

  window.EdgeFeatures = Object.freeze({
    show,
    open: (id) => openDrawer(id),
    close: () => closeDrawer(),
    refresh,
    list: () => FEATURES.map((f) => ({ id: f.id, group: f.group, title: f.title, state: stateOf(f).state, full: f.full ? f.full.go : null })),
  });

  if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') window.edgeAuth.onReady(init);
  else if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
