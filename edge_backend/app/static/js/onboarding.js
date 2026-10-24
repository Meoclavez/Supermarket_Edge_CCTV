/**
 * Getting started checklist (Today) and the first-run welcome.
 *
 * Checklist (#gettingStartedMount on Today): the store's commissioning steps in
 * the order of docs/USER_MANUAL.md Part 2, each with a one-line why, a live
 * status read from the existing APIs and a "Do it" button that opens the right
 * screen. Owners and administrators see the setup steps; operators see only
 * what they can do themselves (alerts on their own phone, how to review an
 * alert). Steps for features the iKorex plan leaves out (GET /api/v1/fleet/status
 * via window.edgeFleet) are not shown. A status that cannot be read says so;
 * nothing is assumed done. Hide it with the cross; reopen it from Help.
 *
 * Welcome: on an administrator's first sign-in in this browser, four short
 * screens about the main areas, ending at the checklist. Skippable (Esc),
 * reopened from Help > "Welcome tour".
 *
 * Per-person flags live in localStorage (try/catch; without storage the
 * checklist simply shows and the welcome shows once per page load at most).
 */
(function () {
  'use strict';

  const POLL_MS = 60000;
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v === null || v === undefined ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const ico = (name, size) => {
    try { return window.EdgeIcon ? window.EdgeIcon.svg(name, { size: size || 'md' }) : ''; } catch (_) { return ''; }
  };
  const account = () => window.EdgeAuth || {};
  const userKey = () => account().id || account().username || 'anon';
  const help = () => window.EdgeHelp || null;
  const call = (name, ...args) => (typeof window[name] === 'function' ? window[name](...args) : undefined);
  const later = (fn, ms) => setTimeout(() => { try { fn(); } catch (_) { /* screen not ready */ } }, ms || 150);
  const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || `${one}s`)}`;

  // ------------------------------------------------------------ stored flags
  function readFlags() {
    try { return JSON.parse(localStorage.getItem(`edge_cctv_getting_started:${userKey()}`) || '{}') || {}; } catch (_) { return {}; }
  }
  function writeFlags(f) {
    try { localStorage.setItem(`edge_cctv_getting_started:${userKey()}`, JSON.stringify(f)); } catch (_) { /* storage blocked */ }
  }
  let welcomeShownThisLoad = false;
  function welcomeSeen() {
    try { return localStorage.getItem(`edge_cctv_welcome_seen:${userKey()}`) === '1'; } catch (_) { return welcomeShownThisLoad; }
  }
  function setWelcomeSeen() {
    welcomeShownThisLoad = true;
    try { localStorage.setItem(`edge_cctv_welcome_seen:${userKey()}`, '1'); } catch (_) { /* storage blocked */ }
  }

  // --------------------------------------------------------------- the plan
  function featureOff(key) {
    const st = window.edgeFleet && typeof window.edgeFleet.status === 'function' ? window.edgeFleet.status() : null;
    return !!(st && st.features && st.features[key] === false);
  }

  async function get(url) {
    const h = help();
    if (h) return h.fetchJSON(url);
    try {
      const r = await fetch(url);
      return r.ok ? { ok: true, data: await r.json() } : { ok: false, status: r.status, message: 'Could not be checked.' };
    } catch (_) { return { ok: false, status: 0, message: 'Could not be checked.' }; }
  }

  /** Everything the steps need, read in parallel; a feature left out of the plan is not asked for. */
  async function loadData(admin) {
    const want = {
      push: !featureOff('phone_alerts') ? '/api/v1/web-push/config' : null,
    };
    if (admin) {
      Object.assign(want, {
        pipe: '/api/v1/layout/pipeline/status',
        layout: '/api/v1/layout',
        setup: '/api/v1/store/setup',
        cameras: '/api/v1/cameras',
        site: '/api/v1/site-settings',
        products: '/api/v1/analytics/products/zones',
        zones: !featureOff('zone_alerts') ? '/api/zones' : null,
        night: !featureOff('night_watch') ? '/api/v1/night-watch' : null,
        subs: !featureOff('phone_alerts') ? '/api/v1/web-push/subscriptions' : null,
        users: '/api/v1/users',
        remote: !featureOff('online_access') ? '/api/v1/remote-access' : null,
        pos: !featureOff('pos_integration') ? '/api/v1/analytics/pos/status' : null,
        backups: '/api/v1/system/backups',
      });
    }
    const keys = Object.keys(want).filter((k) => want[k]);
    const res = await Promise.all(keys.map((k) => get(want[k])));
    const out = {};
    keys.forEach((k, i) => { out[k] = res[i]; });
    return out;
  }

  // A step: { id, title, why, optional, feature, check(d) -> { state, detail, go, goLabel } }
  // state: 'done' | 'partial' | 'todo' | 'unknown'.
  const unknown = (r) => ({ state: 'unknown', detail: `Could not be checked: ${(r && r.message) || 'no answer.'}` });
  const ok = (r) => r && r.ok && r.data;
  const camerasOf = (d) => (ok(d.layout) && d.layout.data.cameras) || [];
  const firstCamera = (d) => {
    const c = camerasOf(d)[0];
    return c ? c.camera_id : null;
  };
  const studioFor = (camId, tool) => {
    const q = new URLSearchParams({ from: 'checklist' });
    if (camId) q.set('camera_id', camId);
    if (tool) q.set('tool', tool);
    return `studio-url:/dashboard/studio?${q.toString()}`;
  };

  const ADMIN_STEPS = [
    {
      id: 'store', title: 'Name the store and set its time zone',
      why: 'The name is on every page and the daily report; the time zone decides what "today", schedules and alert times mean.',
      check(d) {
        if (!ok(d.site)) return unknown(d.site);
        const s = d.site.data.settings || {};
        const name = s.STORE_NAME && String(s.STORE_NAME.value || '').trim();
        const tz = s.SITE_TIMEZONE && String(s.SITE_TIMEZONE.value || '').trim();
        if (name && tz) return { state: 'done', detail: `${name} · ${tz}` };
        return { state: name || tz ? 'partial' : 'todo', detail: name ? 'Pick the store\'s time zone.' : 'Enter the store name.', go: 'settings:settings-store', goLabel: 'Open Store settings' };
      },
      go: 'settings:settings-store',
    },
    {
      id: 'plan', title: 'Draw the store plan',
      why: 'The map is where live shoppers, heatmaps and store areas are shown.',
      check(d) {
        if (!ok(d.layout)) return unknown(d.layout);
        const n = (d.layout.data.setup || {}).structures || 0;
        return n ? { state: 'done', detail: `${plural(n, 'room, wall or shelf', 'rooms, walls and shelves')} drawn.` }
          : { state: 'todo', detail: 'Nothing drawn yet.' };
      },
      go: 'map-edit', goLabel: 'Open the Store map',
    },
    {
      id: 'cameras', title: 'Add your cameras',
      why: 'Every figure and alert comes from the cameras. A recorder (NVR) adds all its channels at once.',
      check(d) {
        if (!ok(d.pipe)) return unknown(d.pipe);
        const p = d.pipe.data;
        const total = p.cameras_total || 0;
        if (!total) return { state: 'todo', detail: 'No cameras yet.' };
        const bad = (p.cameras || []).filter((c) => c.status !== 'ONLINE' && c.status !== 'DISABLED' && c.enabled !== false).length;
        if (bad) return { state: 'partial', detail: `${plural(total, 'camera')} added; ${bad} not sending pictures.`, go: 'cameras', goLabel: 'Check cameras' };
        return { state: 'done', detail: `${plural(total, 'camera')} added and working.` };
      },
      go: 'add-cameras', goLabel: 'Add cameras',
    },
    {
      id: 'purpose', title: 'Choose what each camera looks at',
      why: 'The purpose (entrance, checkout, aisle, stockroom...) switches on the right checks for that camera.',
      check(d) {
        if (!ok(d.setup)) return unknown(d.setup);
        const cams = d.setup.data.cameras || [];
        if (!cams.length) return { state: 'todo', detail: 'Add cameras first.', go: 'add-cameras', goLabel: 'Add cameras' };
        const missing = cams.filter((c) => !c.role);
        if (!missing.length) return { state: 'done', detail: 'Every camera has a purpose.' };
        return { state: missing.length < cams.length ? 'partial' : 'todo',
          detail: `${missing.length} of ${cams.length} still need a purpose, starting with "${missing[0].name}".`,
          go: `camera:${missing[0].camera_id}`, goLabel: 'Choose purpose' };
      },
    },
    {
      id: 'place', title: 'Put each camera on the map',
      why: 'So everyone can see where a camera is mounted and what it covers.',
      check(d) {
        if (!ok(d.layout)) return unknown(d.layout);
        const cams = camerasOf(d);
        if (!cams.length) return { state: 'todo', detail: 'Add cameras first.', go: 'add-cameras', goLabel: 'Add cameras' };
        const left = cams.filter((c) => c.map_placed === false).length;
        if (!left) return { state: 'done', detail: 'Every camera is on the map.' };
        return { state: left < cams.length ? 'partial' : 'todo', detail: `${left} of ${cams.length} not placed yet. Drag them from the camera gallery beside the map.` };
      },
      go: 'map', goLabel: 'Open the Store map',
    },
    {
      id: 'calibrate', title: 'Calibrate cameras, or link them to an area',
      why: 'Calibration puts shoppers on the map and heatmap; linking an uncalibrated camera to an area still counts its visits.',
      check(d) {
        if (!ok(d.layout)) return unknown(d.layout);
        const cams = camerasOf(d);
        if (!cams.length) return { state: 'todo', detail: 'Add cameras first.', go: 'add-cameras', goLabel: 'Add cameras' };
        const s = d.layout.data.setup || {};
        const cal = s.cameras_calibrated || 0, linked = s.cameras_zone_linked || 0;
        const left = cams.filter((c) => !c.has_homography && !c.watch_zone_id);
        if (!left.length) return { state: 'done', detail: `${cal} calibrated${linked ? `, ${linked} linked to an area` : ''}.` };
        return { state: cal + linked ? 'partial' : 'todo',
          detail: `${cal} calibrated, ${linked} linked, ${left.length} still to do, starting with "${left[0].name}".`,
          go: `calibrate:${left[0].camera_id}`, goLabel: 'Calibrate' };
      },
    },
    {
      id: 'zones', title: 'Draw store areas (aisles, entrance, checkout)',
      why: 'Visits, time spent and the recommendations are measured per area.',
      check(d) {
        if (!ok(d.layout)) return unknown(d.layout);
        const n = (d.layout.data.setup || {}).zones || 0;
        return n ? { state: 'done', detail: `${plural(n, 'area')} drawn.` } : { state: 'todo', detail: 'No store areas yet. Use the Zone tool.' };
      },
      go: 'map-zone', goLabel: 'Draw a zone',
    },
    {
      id: 'shelves', title: 'Draw product shelves on the camera pictures',
      why: 'Shelf reaches and the theft checks need to know where the products are.',
      check(d) {
        if (!ok(d.products)) return unknown(d.products);
        const n = d.products.data.total_zones || 0;
        const cams = ok(d.cameras) ? (d.cameras.data.cameras || []) : [];
        const theftOn = cams.filter((c) => !c.features || c.features.theft_detection !== false).length;
        const theft = ok(d.cameras) && cams.length && !featureOff('theft_detection') ? ` Theft checks are on for ${theftOn} of ${cams.length} cameras.` : '';
        return n ? { state: 'done', detail: `${plural(n, 'product shelf', 'product shelves')} drawn.${theft}` }
          : { state: 'todo', detail: `No product shelves yet.${theft}`, go: studioFor(firstCamera(d), 'product'), goLabel: 'Open Camera setup' };
      },
      go: 'studio', goLabel: 'Open Camera setup',
    },
    {
      id: 'zone-alerts', title: 'Set up area and line alerts', optional: true, feature: 'zone_alerts',
      why: 'Alerts when someone is in the stockroom after hours or crosses a back-door line.',
      check(d) {
        if (!ok(d.zones)) return unknown(d.zones);
        const z = d.zones.data || {};
        const areas = (z.intrusion_zones || []).length;
        const lines = (z.tripwires || []).filter((t) => t.alert_enabled).length;
        if (areas + lines) return { state: 'done', detail: `${plural(areas, 'restricted area')}, ${plural(lines, 'alert line')}.` };
        return { state: 'todo', detail: 'None yet.', go: studioFor(firstCamera(d), 'restricted'), goLabel: 'Open Camera setup' };
      },
      go: 'studio', goLabel: 'Open Camera setup',
    },
    {
      id: 'night', title: 'Turn on night watch for after-hours cameras', optional: true, feature: 'night_watch',
      why: 'While the store is closed, a person seen on a watched camera is a high alert.',
      check(d) {
        if (!ok(d.night)) return unknown(d.night);
        const n = d.night.data.cameras_enabled || 0;
        const cam = firstCamera(d);
        return n ? { state: 'done', detail: `${plural(n, 'camera')} watching at night.` }
          : { state: 'todo', detail: 'No camera watches at night yet. Turn it on in a camera\'s settings.', go: cam ? `night:${cam}` : 'cameras', goLabel: 'Open camera settings' };
      },
      go: 'cameras', goLabel: 'Open Cameras',
    },
    {
      id: 'phones', title: 'Get alerts on staff phones', feature: 'phone_alerts',
      why: 'Staff are told within seconds, even away from this screen.',
      check(d) {
        if (!ok(d.subs)) return unknown(d.subs);
        const n = (d.subs.data.subscriptions || []).length;
        const roster = ok(d.push) ? !!d.push.data.roster_configured : null;
        if (!n) return { state: 'todo', detail: 'No phone has alerts turned on yet. Each person installs the app on their phone and taps "Enable alerts".' };
        if (roster === false) return { state: 'partial', detail: `${plural(n, 'phone')} connected. Under "Who gets alerts", choose at least 2 first-priority people.` };
        return { state: 'done', detail: `${plural(n, 'phone')} connected.` };
      },
      go: 'settings:settings-phone-alerts', goLabel: 'Open Phone alerts',
    },
    {
      id: 'accounts', title: 'Create accounts for your staff', optional: true,
      why: 'Everyone signs in as themselves. Operators can review alerts but cannot change setup.',
      check(d) {
        if (!ok(d.users)) return unknown(d.users);
        const list = d.users.data.accounts || [];
        const n = list.length;
        return n > 1 ? { state: 'done', detail: `${plural(n, 'account')}.` } : { state: 'todo', detail: 'Only your own account so far.' };
      },
      go: 'settings:settings-accounts', goLabel: 'Open Accounts',
    },
    {
      id: 'online', title: 'Turn on online access', optional: true, feature: 'online_access',
      why: 'Open the dashboard away from the store. Phone alerts need it.',
      check(d) {
        if (!ok(d.remote)) return unknown(d.remote);
        const r = d.remote.data;
        if (!r.enabled) return { state: 'todo', detail: 'Off. Usually set up by the installer.' };
        if (r.process === 'connected') return { state: 'done', detail: r.verified ? 'Connected and verified.' : 'Connected. Press "Verify now" to check the public address.' };
        return { state: 'partial', detail: 'Turned on but not connected right now.' };
      },
      go: 'settings:settings-remote', goLabel: 'Open Online access',
    },
    {
      id: 'tills', title: 'Connect the tills', optional: true, feature: 'pos_integration',
      why: 'Sales today, the buying rate and lost sales need till data.',
      check(d) {
        if (!ok(d.pos)) return unknown(d.pos);
        return d.pos.data.connected ? { state: 'done', detail: 'Sales are arriving.' } : { state: 'todo', detail: 'No sales received yet.' };
      },
      go: 'settings:settings-pos', goLabel: 'Open Sales data',
    },
    {
      id: 'backup', title: 'Back up and download a copy',
      why: 'Backups stay on this device unless you download one. Keep a copy somewhere safe.',
      check(d) {
        if (!ok(d.backups)) return unknown(d.backups);
        const list = d.backups.data.backups || [];
        const manual = list.filter((b) => /^(manual|uploaded)/i.test(String(b.kind || '')) || /^(manual|upload)/i.test(String(b.tag || ''))).length;
        if (manual) return { state: 'done', detail: `${plural(manual, 'manual or uploaded backup')} on the device. Download one regularly.` };
        return { state: list.length ? 'partial' : 'todo',
          detail: list.length ? `${plural(list.length, 'automatic backup')} on the device. Press "Back up now", then "Download".` : 'No backups yet.' };
      },
      go: 'settings:settings-backups', goLabel: 'Open Backups',
    },
  ];

  const OPERATOR_STEPS = [
    {
      id: 'my-phone', title: 'Turn on alerts on your phone', feature: 'phone_alerts',
      why: 'You are told about an alert within seconds, even away from this screen.',
      check(d) {
        if (!ok(d.push)) return unknown(d.push);
        const n = (d.push.data.my_subscriptions || []).length;
        return n ? { state: 'done', detail: `${plural(n, 'phone')} of yours get alerts.` }
          : { state: 'todo', detail: 'On your phone, open the store\'s public address, install the app and tap "Enable alerts".' };
      },
      go: 'settings:settings-phone-alerts', goLabel: 'Open Phone alerts',
    },
    {
      id: 'learn-review', title: 'Learn how to review an alert', feature: 'theft_detection',
      why: 'An alert is something to check, never proof of theft. Recording what happened keeps alerts accurate.',
      check() {
        const read = help() && help().hasRead('review-alert');
        return read ? { state: 'done', detail: 'Guide read.' } : { state: 'todo', detail: 'A one-minute guide.' };
      },
      go: 'help:review-alert', goLabel: 'Read the guide',
    },
  ];

  // --------------------------------------------------------- "Do it" actions
  function run(action) {
    const a = String(action || '');
    if (a.startsWith('camera:')) { call('openCameraConfigModal', a.slice(7)); return; }
    if (a.startsWith('night:')) {
      Promise.resolve(call('openCameraConfigModal', a.slice(6))).then(() => later(() => {
        const m = $('nightWatchMount');
        if (m) m.scrollIntoView({ behavior: 'instant', block: 'start' });
      }, 300));
      return;
    }
    if (a.startsWith('calibrate:')) {
      const id = a.slice(10);
      call('switchTab', 'map');
      later(() => { if (window.calibrationTool) window.calibrationTool.open(id); }, 200);
      return;
    }
    if (a.startsWith('studio-url:')) { window.location.href = a.slice(11); return; }
    if (a.startsWith('help:')) { if (help()) help().open(a.slice(5)); return; }
    if (help()) help().go(a);
  }

  // ---------------------------------------------------------------- render
  let timer = null;
  let loading = false;
  let lastSteps = [];
  let fleetRetried = false;

  const STATE_ICON = { done: 'circle-check', partial: 'circle-alert', todo: 'circle', unknown: 'circle-help' };
  const STATE_WORD = { done: 'Done', partial: 'Partly done', todo: 'To do', unknown: 'Not checked' };

  function visibleToday() {
    const v = $('tab-today');
    return !!v && v.classList.contains('active') && !document.hidden;
  }

  function mount() { return $('gettingStartedMount'); }

  async function refresh(force) {
    const host = mount();
    if (!host || loading) return;
    if (!force && !visibleToday()) return;
    if (!(window.edgeAuth && window.edgeAuth.isAuthenticated && window.edgeAuth.isAuthenticated())) return;
    const flags = readFlags();
    if (flags.dismissed && !force) { host.hidden = true; return; }
    loading = true;
    try {
      const admin = !!account().isAdmin;
      const defs = (admin ? ADMIN_STEPS : OPERATOR_STEPS).filter((s) => !s.feature || !featureOff(s.feature));
      const data = await loadData(admin);
      lastSteps = defs.map((s) => {
        let r;
        try { r = s.check(data) || unknown(null); } catch (_) { r = unknown(null); }
        return { ...s, ...r, go: r.go || s.go, goLabel: r.goLabel || s.goLabel || 'Do it' };
      });
      render(host, lastSteps, admin, readFlags());
      // The iKorex plan (fleet.js) may not have loaded yet: look again shortly so
      // steps for features left out of the plan disappear.
      if (!fleetRetried && window.edgeFleet && typeof window.edgeFleet.status === 'function' && !window.edgeFleet.status()) {
        fleetRetried = true;
        later(() => refresh(), 4000);
      }
    } finally {
      loading = false;
    }
  }

  function render(host, steps, admin, flags) {
    if (!steps.length) { host.hidden = true; return; }
    const required = steps.filter((s) => !s.optional);
    const doneReq = required.filter((s) => s.state === 'done').length;
    const pct = required.length ? Math.round((doneReq / required.length) * 100) : 100;
    const allDone = doneReq === required.length;
    const next = steps.find((s) => !s.optional && s.state !== 'done') || steps.find((s) => s.state !== 'done');
    const collapsed = !!flags.collapsed;
    const hiddenByPlan = (admin ? ADMIN_STEPS : OPERATOR_STEPS).filter((s) => s.feature && featureOff(s.feature)).length;

    const rows = steps.map((s, i) => {
      const isNext = next && s.id === next.id;
      const btnCls = s.state === 'done' ? 'btn btn-sm btn-ghost' : isNext ? 'btn btn-sm btn-primary' : 'btn btn-sm';
      const label = s.state === 'done' ? 'Open' : s.goLabel;
      return `
        <li class="gs-step gs-${esc(s.state)}${isNext ? ' gs-next' : ''}">
          <span class="gs-icon" aria-hidden="true">${ico(STATE_ICON[s.state] || 'circle', 'md')}</span>
          <div class="gs-main">
            <div class="gs-step-title"><span class="gs-num">${i + 1}.</span> ${esc(s.title)}
              ${s.optional ? '<span class="badge badge-neutral">Optional</span>' : ''}
              <span class="sr-only">: ${esc(STATE_WORD[s.state] || '')}</span></div>
            <div class="gs-why">${esc(s.why)}</div>
            <div class="gs-detail">${esc(s.detail || '')}</div>
          </div>
          ${s.go ? `<button type="button" class="${btnCls} gs-go" data-gs-go="${esc(s.id)}">${esc(label)}</button>` : ''}
        </li>`;
    }).join('');

    host.innerHTML = `
      <section class="glass-card gs-card" aria-labelledby="gsTitle">
        <div class="card-title">
          <span id="gsTitle">${ico('circle-check')}${admin ? 'Getting started' : 'Getting started for staff'}</span>
          <span class="gs-head-actions">
            <button type="button" class="btn btn-sm btn-ghost" data-gs="collapse" aria-expanded="${collapsed ? 'false' : 'true'}" aria-controls="gsList">${collapsed ? 'Show steps' : 'Hide steps'}</button>
            <button type="button" class="btn btn-sm btn-ghost btn-icon" data-gs="dismiss" aria-label="Close the Getting started checklist" title="Close (reopen it from Help)">${ico('x')}</button>
          </span>
        </div>
        <p class="gs-lede">${allDone
    ? 'Setup is complete. The optional steps below add more; you can close this checklist.'
    : admin ? 'Do these in order. Each step checks itself, so you can stop and come back any time.'
      : 'Two things to do once, so you never miss an alert.'}</p>
        <div class="gs-progress">
          <div class="gs-progress-text"><b>${doneReq} of ${required.length}</b> ${required.length === 1 ? 'step' : 'steps'} done${steps.length > required.length ? ` · ${steps.length - required.length} optional` : ''}</div>
          <div class="gs-bar" role="progressbar" aria-label="Setup progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}" aria-valuetext="${doneReq} of ${required.length} steps done"><span class="gs-bar-fill"></span></div>
        </div>
        <ol class="gs-list" id="gsList"${collapsed ? ' hidden' : ''}>${rows}</ol>
        <div class="gs-foot">
          ${hiddenByPlan ? `<span class="gs-note">${plural(hiddenByPlan, 'step')} hidden: not included in your iKorex plan.</span>` : ''}
          <button type="button" class="link-btn" data-gs="help">Open Help</button>
        </div>
      </section>`;
    const fill = host.querySelector('.gs-bar-fill');
    if (fill) fill.style.width = `${pct}%`;
    host.hidden = false;
  }

  function onClick(e) {
    const host = mount();
    if (!host) return;
    const goBtn = e.target.closest('[data-gs-go]');
    if (goBtn) {
      const s = lastSteps.find((x) => x.id === goBtn.getAttribute('data-gs-go'));
      if (s) run(s.go);
      return;
    }
    const b = e.target.closest('[data-gs]');
    if (!b) return;
    const what = b.getAttribute('data-gs');
    const flags = readFlags();
    if (what === 'collapse') {
      flags.collapsed = !flags.collapsed;
      writeFlags(flags);
      const list = $('gsList');
      if (list) list.hidden = flags.collapsed;
      b.setAttribute('aria-expanded', flags.collapsed ? 'false' : 'true');
      b.textContent = flags.collapsed ? 'Show steps' : 'Hide steps';
    } else if (what === 'dismiss') {
      flags.dismissed = true;
      writeFlags(flags);
      host.hidden = true;
      if (typeof window.showToast === 'function') window.showToast('Getting started closed. Reopen it any time from Help.', 'ok');
      const target = $('navHelp');
      if (target) target.focus({ preventScroll: true });
    } else if (what === 'help') {
      if (help()) help().open();
    }
  }

  /** Show the checklist again (Help > "Getting started checklist"). */
  function showChecklist() {
    const flags = readFlags();
    flags.dismissed = false;
    flags.collapsed = false;
    writeFlags(flags);
    call('switchTab', 'today');
    refresh(true).then(() => later(() => {
      const card = mount();
      if (!card || card.hidden) return;
      card.scrollIntoView({ behavior: 'instant', block: 'start' });
      const t = $('gsTitle');
      const first = card.querySelector('.gs-next .gs-go') || card.querySelector('.gs-go');
      (first || t || card).focus({ preventScroll: true });
    }, 60));
  }

  // --------------------------------------------------------------- welcome
  const WELCOME = [
    {
      title: 'Welcome to your store dashboard',
      body: ['This system does three things, all on this device in your store:'],
      list: ['Loss prevention: it flags suspicious behaviour for staff to check. An alert is never proof of theft.',
        'Store analysis: visitors, time spent, entrances and shelf reaches, turned into recommendations.',
        'Customer heatmaps: where shoppers walk, stop and touch shelves.'],
      after: 'A dash means "not measured yet". Every figure comes from what the cameras saw; nothing is guessed.',
    },
    {
      title: 'Where things are',
      body: ['The tabs along the top (on a phone, along the bottom):'],
      list: ['Today: the day at a glance and what needs you.',
        'Cameras: live video and each camera\'s settings.',
        'Store map: the plan of the store, cameras and live shoppers.',
        'Insights: recommendations, footfall and heatmap history.',
        'Loss prevention: alerts to review, area and line alerts, night watch.',
        'Settings (the gear): phones, accounts, store settings, backups.'],
    },
    {
      title: 'Help is always one click away',
      body: ['Press Help at the top right to search short how-to guides, find iKorex support or report a problem.',
        'A small question mark next to a title explains that card in plain words. Press it again, or Esc, to close it.'],
    },
    {
      title: 'Set up in order',
      body: ['The Getting started checklist on Today lists the setup steps in the right order. Each one checks itself and has a button that takes you to the right place.',
        'You do not have to do it all at once; the checklist remembers where you are.'],
    },
  ];

  let wIndex = 0;
  let wOverlay = null;
  let wOpener = null;

  function ensureWelcome() {
    if (wOverlay) return wOverlay;
    wOverlay = document.createElement('div');
    wOverlay.className = 'modal-overlay help-welcome-overlay';
    wOverlay.id = 'edgeWelcome';
    wOverlay.innerHTML = `
      <div class="modal-card help-welcome" role="dialog" aria-modal="true" aria-labelledby="welcomeTitle" aria-describedby="welcomeBody">
        <div class="modal-header">
          <span class="help-welcome-step" id="welcomeStep"></span>
          <button type="button" class="btn btn-ghost btn-sm" data-w="skip">Skip</button>
        </div>
        <h2 class="help-welcome-title" id="welcomeTitle" tabindex="-1"></h2>
        <div class="help-welcome-body" id="welcomeBody"></div>
        <div class="help-welcome-dots" aria-hidden="true"></div>
        <div class="help-welcome-foot">
          <button type="button" class="btn btn-sm" data-w="back">${ico('arrow-left', 'sm')}Back</button>
          <button type="button" class="btn btn-sm btn-primary" data-w="next"></button>
        </div>
      </div>`;
    document.body.appendChild(wOverlay);
    wOverlay.addEventListener('click', (e) => {
      const b = e.target.closest('[data-w]');
      if (!b) return;
      const what = b.getAttribute('data-w');
      if (what === 'skip') closeWelcome(true);
      else if (what === 'back' && wIndex > 0) { wIndex -= 1; renderWelcome(); }
      else if (what === 'next') {
        if (wIndex < WELCOME.length - 1) { wIndex += 1; renderWelcome(); }
        else { closeWelcome(false); showChecklist(); }
      }
    });
    wOverlay.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeWelcome(true); return; }
      if (e.key !== 'Tab') return;
      const items = [...wOverlay.querySelectorAll('button:not([disabled]), [tabindex="-1"]#welcomeTitle')].filter((n) => n.offsetParent !== null);
      const btns = items.filter((n) => n.tagName === 'BUTTON');
      if (!btns.length) return;
      const first = btns[0], last = btns[btns.length - 1];
      if (e.shiftKey && (document.activeElement === first || document.activeElement.id === 'welcomeTitle')) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    });
    return wOverlay;
  }

  function renderWelcome() {
    const s = WELCOME[wIndex];
    const last = wIndex === WELCOME.length - 1;
    $('welcomeStep').textContent = `Step ${wIndex + 1} of ${WELCOME.length}`;
    $('welcomeTitle').textContent = s.title;
    $('welcomeBody').innerHTML = (s.body || []).map((p) => `<p>${esc(p)}</p>`).join('')
      + (s.list ? `<ul>${s.list.map((i) => `<li>${esc(i)}</li>`).join('')}</ul>` : '')
      + (s.after ? `<p class="help-muted">${esc(s.after)}</p>` : '');
    wOverlay.querySelector('.help-welcome-dots').innerHTML = WELCOME.map((_, i) => `<span class="help-dot${i === wIndex ? ' is-on' : ''}"></span>`).join('');
    const back = wOverlay.querySelector('[data-w="back"]');
    back.disabled = wIndex === 0;
    const next = wOverlay.querySelector('[data-w="next"]');
    next.innerHTML = last ? `Show me the checklist${ico('arrow-right', 'sm')}` : `Next${ico('arrow-right', 'sm')}`;
    $('welcomeTitle').focus({ preventScroll: true });
  }

  function showWelcome() {
    ensureWelcome();
    setWelcomeSeen();
    wOpener = document.activeElement;
    wIndex = 0;
    wOverlay.style.display = 'flex';
    document.documentElement.classList.add('help-open');
    renderWelcome();
  }

  function closeWelcome(restoreFocus) {
    if (!wOverlay) return;
    wOverlay.style.display = 'none';
    document.documentElement.classList.remove('help-open');
    if (restoreFocus) {
      const back = wOpener && wOpener.isConnected ? wOpener : $('navHelp');
      if (back && typeof back.focus === 'function') back.focus({ preventScroll: true });
    }
    wOpener = null;
  }

  function maybeWelcome() {
    const a = account();
    if (!a.known || !a.isAdmin || welcomeSeen()) return;
    if (!(window.edgeAuth && window.edgeAuth.isAuthenticated && window.edgeAuth.isAuthenticated())) return;
    // Not over another open dialog (camera settings, evidence viewer, Help).
    if (document.querySelector('.modal-overlay[style*="flex"], .help-centre:not([hidden])')) return;
    showWelcome();
  }

  // ------------------------------------------------------------------ boot
  function start() {
    clearInterval(timer);
    refresh();
    timer = setInterval(() => refresh(), POLL_MS);
  }

  function init() {
    const host = mount();
    if (host) host.addEventListener('click', onClick);
    window.addEventListener('edge:tab', (e) => { if (e.detail && e.detail.tab === 'today') refresh(); });
    window.addEventListener('edge:account', () => refresh());
    window.addEventListener('edge:help-read', () => { if (!account().isAdmin) refresh(); });
    document.addEventListener('visibilitychange', () => { if (visibleToday()) refresh(); });
    start();
    // After the intro screen and the first data load.
    later(maybeWelcome, 1800);
  }

  window.EdgeOnboarding = Object.freeze({ refresh: () => refresh(true), showChecklist, showWelcome, steps: () => lastSteps.slice() });

  if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') window.edgeAuth.onReady(init);
  else if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
