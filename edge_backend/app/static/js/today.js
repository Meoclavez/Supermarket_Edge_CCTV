/**
 * Today: the home screen after sign-in.
 *
 * One glance answers "is everything working, how busy is it, what needs me":
 * cameras working, people in the store now, visitors today by hour (against
 * yesterday and the same weekday last week), incidents waiting for review,
 * the top recommendations, and how complete the store setup is with the next
 * step as a button. Every figure comes from the API; an unmeasured value is
 * an em dash with a sentence saying what to do, never a zero.
 *
 * Globals used from analytics.js: DASH, el, escapeHtml, isNum, getJSON,
 * setMetric, emptyState, formatAgo, formatDuration, theftAuthUrl, switchTab,
 * renderHourlyVisitors, openDeviceManager, openCameraConfigModal, jumpTo.
 */
(function () {
  'use strict';

  const POLL_MS = 15000;
  const OPEN_STATUSES = ['ACTIVE', 'ACKNOWLEDGED', 'DISPATCHED'];
  const PRIORITY_BADGE = { critical: 'badge-danger', high: 'badge-warning', medium: 'badge-primary', low: 'badge-neutral', info: 'badge-neutral' };
  const STATUS_ICON = { available: '✓', limited: '◐', blocked: '✕', unsupported: '–' };
  const STATUS_WORD = { available: 'Working', limited: 'Partly', blocked: 'Needs setup', unsupported: 'Not possible' };

  let timer = null;
  let loading = false;
  let lastNextStep = null;

  const visible = () => {
    const v = el('tab-today');
    return !!v && v.classList.contains('active') && !document.hidden;
  };

  // ---------------------------------------------------------------- KPIs
  /** Short reason a camera is not sending pictures, from its worker status and error. */
  function problemKind(c) {
    const err = String(c.last_error || '');
    if (c.status === 'AUTH_FAILED') return /none is saved/i.test(err) ? 'no password saved' : 'wrong password';
    if (c.status === 'DISABLED') return 'switched off';
    if (c.status === 'STARTING') return 'starting';
    if (/stream ended|frame read failed/i.test(err)) return 'picture stopped';
    return "can't connect";
  }

  /**
   * "3 wrong password, 1 can't connect": every camera counted under its own
   * reason (most common first), never all of them under the first one's error.
   */
  function problemSummary(off) {
    const counts = new Map();
    off.forEach((c) => { const k = problemKind(c); counts.set(k, (counts.get(k) || 0) + 1); });
    return [...counts.entries()].sort((a, b) => b[1] - a[1]).map(([k, n]) => `${n} ${k}`).join(', ');
  }

  /** One line per camera for a tooltip: its name and its own error. */
  function problemDetail(off) {
    return off.map((c) => `${c.name || c.camera_id}: ${c.last_error || problemKind(c)}`).join('\n');
  }

  function renderCameras(pipe) {
    const note = el('kpiCamerasNote');
    if (!pipe) { setMetric('kpiCameras', null); if (note) note.textContent = 'Camera status unavailable.'; return; }
    const tot = pipe.cameras_total, on = pipe.cameras_online;
    if (!tot) {
      setMetric('kpiCameras', null);
      if (note) note.innerHTML = 'No cameras yet. <button type="button" class="link-btn" onclick="openDeviceManager()">Add a camera</button>';
      return;
    }
    setMetric('kpiCameras', `${on ?? DASH} / ${tot}`);
    const off = (pipe.cameras || []).filter((c) => c.status !== 'ONLINE');
    const box = el('kpiCamerasBox');
    if (box) box.classList.toggle('stat-box-alert', off.length > 0);
    if (!note) return;
    if (!off.length) {
      note.textContent = 'All cameras sending pictures';
      note.title = '';
    } else {
      note.innerHTML = `${off.length} not working: ${escapeHtml(problemSummary(off))}
        <button type="button" class="link-btn" onclick="switchTab('cameras')">Fix →</button>`;
      note.title = problemDetail(off);
    }
  }

  function renderOverview(overview, hourly, pos) {
    const now = overview ? overview.active_shoppers_now : null;
    setMetric('kpiActiveShoppers', now);
    const nowNote = el('kpiActiveShoppersNote');
    if (nowNote) nowNote.textContent = isNum(now) ? 'people the cameras see right now' : 'no camera is counting people right now';

    const t = hourly && hourly.today;
    const visitors = t && t.totals ? t.totals.visitors : (overview ? overview.today_footfall : null);
    setMetric('kpiFootfallToday', visitors);
    const src = el('kpiFootfallSource');
    if (src) {
      if (!t) src.textContent = '';
      else if (t.source === 'tripwire') src.textContent = t.source_label;
      else {
        const cam = (typeof allCamerasList !== 'undefined' ? allCamerasList : [])[0];
        const href = cam ? `/dashboard/studio?camera_id=${encodeURIComponent(cam.id)}&tool=tripwire` : '/dashboard/studio';
        src.innerHTML = `${escapeHtml(t.source_label || 'Not measured yet')}. <a class="link-btn" href="${href}">Draw an entrance line for exact counts →</a>`;
      }
    }

    const busy = hourly && hourly.busiest_hour;
    setMetric('kpiBusiestHour', busy ? busy.label : null);
    const bn = el('kpiBusiestHourNote');
    if (bn) bn.textContent = busy ? `${Number(busy.visitors).toLocaleString()} visitors in that hour` : 'no hour measured yet today';

    const dwell = overview ? overview.avg_dwell_minutes : null;
    setMetric('kpiAvgDwell', isNum(dwell) ? formatDuration(dwell * 60) : null);

    const connected = pos ? !!pos.connected : !!(overview && overview.pos_connected);
    const salesNote = el('kpiSalesNote');
    if (!connected) {
      setMetric('kpiRevenue', 'Not connected');
      const v = el('kpiRevenue');
      if (v) v.classList.add('metric-unobserved', 'stat-val-text');
      if (salesNote) salesNote.innerHTML = 'Revenue and buying rate need your tills. <button type="button" class="link-btn" onclick="switchTab(\'settings\'); setTimeout(() => jumpTo(\'settings-pos\'), 80)">Set up →</button>';
    } else {
      const v = el('kpiRevenue');
      if (v) v.classList.remove('stat-val-text');
      setMetric('kpiRevenue', overview ? overview.daily_revenue : null, { prefix: '$', digits: 2 });
      const conv = overview ? overview.conversion_rate_pct : null;
      if (salesNote) salesNote.textContent = isNum(conv) ? `${conv.toFixed(1)} % of visitors bought something` : 'buying rate not measured yet';
    }
  }

  /** "Not set up", "no cameras", "none working", "none calibrated" are different situations. */
  function renderDataStateBanner(layout, overview, pipe) {
    const banner = el('dataStateBanner');
    if (!banner) return;
    let html = '';
    const setup = layout && layout.setup;
    const configured = setup ? !!setup.configured
      : !!(layout && ((layout.zones || []).length || (layout.cameras || []).length || (layout.structures || []).length));
    const c = (overview && overview.coverage) || {};
    if (layout && !configured) {
      html = 'Your store is not set up yet: draw the floor and add cameras on the Store map.' +
        ' <button type="button" class="btn btn-sm btn-primary" onclick="switchTab(\'map\')">Open Store map</button>';
    } else if (overview && !c.cameras_total) {
      html = 'No cameras yet. Add one to start counting shoppers.' +
        ' <button type="button" class="btn btn-sm btn-primary" onclick="openDeviceManager()">Add cameras</button>';
    } else if (pipe && pipe.cameras_total && pipe.cameras_online === 0) {
      const off = pipe.cameras || [];
      html = `None of your ${pipe.cameras_total} camera(s) is sending pictures${off.length ? ': ' + escapeHtml(problemSummary(off)) : ''}.` +
        ' <button type="button" class="btn btn-sm" onclick="switchTab(\'cameras\')">Check cameras</button>';
    } else if (overview && c.cameras_total && !c.cameras_calibrated) {
      html = 'People are counted, but no camera is placed on the store map yet, so the map and heatmap stay empty.' +
        ' <button type="button" class="btn btn-sm" onclick="switchTab(\'map\')">Place a camera</button>';
    }
    banner.innerHTML = html;
    banner.style.display = html ? 'flex' : 'none';
  }

  // ------------------------------------------------------------ incidents
  function renderIncidents(data) {
    const host = el('todayIncidents');
    if (!host) return;
    if (!data) { host.innerHTML = emptyState('Incident list unavailable: the server did not respond.'); return; }
    const open = (data.incidents || [])
      .filter((i) => OPEN_STATUSES.includes(i.status))
      .sort((a, b) => String(b.timestamp).localeCompare(String(a.timestamp)));
    if (!open.length) {
      host.innerHTML = emptyState('Nothing waiting for review. Alerts appear here when a camera with theft detection on sees concealment, shelf sweeping or loitering at high-value stock.',
        'Check camera settings', "switchTab('cameras')");
      return;
    }
    host.innerHTML = open.slice(0, 5).map((i) => {
      const thumb = i.snapshot_url
        ? `<img class="today-thumb" loading="lazy" src="${escapeHtml(theftAuthUrl(i.snapshot_url))}" alt="Evidence picture">`
        : '<span class="today-thumb today-thumb-empty" aria-hidden="true">🚨</span>';
      const state = i.status === 'ACTIVE' ? 'New' : i.status === 'ACKNOWLEDGED' ? 'Seen' : 'Staff sent';
      return `
        <div class="today-row" data-incident-row="${escapeHtml(i.id)}">
          ${thumb}
          <div class="today-row-main">
            <div class="today-row-title">${escapeHtml(i.rule_label || i.rule || 'Suspicious behaviour')}</div>
            <div class="today-row-sub">${escapeHtml(i.camera_name || i.camera_id || '')} · ${escapeHtml(formatAgo(i.timestamp))} · <span class="badge ${i.status === 'ACTIVE' ? 'badge-danger' : 'badge-warning'}">${state}</span></div>
          </div>
          <button type="button" class="btn btn-sm btn-primary" data-review="${escapeHtml(i.id)}">Review</button>
        </div>`;
    }).join('') + (open.length > 5 ? `<div class="fp-empty">${open.length - 5} more waiting in Loss prevention.</div>` : '');
    host.querySelectorAll('[data-review]').forEach((b) => b.addEventListener('click', () => {
      switchTab('loss');
      if (window.edgeLoss && typeof window.edgeLoss.focusIncident === 'function') window.edgeLoss.focusIncident(b.dataset.review);
    }));
  }

  // ------------------------------------------------------ recommendations
  function renderRecs(data) {
    const host = el('todayRecs');
    if (!host) return;
    if (!data) { host.innerHTML = emptyState('Recommendations unavailable: the server did not respond.'); return; }
    const items = (data.items || []).filter((r) => r.source !== 'ai_summary' && ['PENDING', 'REVIEWED'].includes(r.status)).slice(0, 3);
    if (!items.length) {
      host.innerHTML = emptyState(data.empty_reason || 'No open recommendations. The analysis runs every hour on measured visits, dwell and queues.',
        'Open Insights', "switchTab('insights')");
      return;
    }
    host.innerHTML = items.map((r) => `
      <div class="today-row today-rec">
        <div class="today-row-main">
          <div class="today-row-title"><span class="badge ${PRIORITY_BADGE[r.priority] || 'badge-neutral'}">${escapeHtml(String(r.priority || '').toUpperCase())}</span>
            ${escapeHtml(r.zone ? `${r.zone}: ` : '')}${escapeHtml(r.title || '')}</div>
          <div class="today-row-sub"><b>Do:</b> ${escapeHtml(r.do || DASH)}</div>
        </div>
      </div>`).join('');
  }

  // ------------------------------------------------------------ setup card
  async function nextStepFor(setup) {
    const cams = setup.cameras || [];
    if (!cams.length) return { text: 'Add your first camera.', label: 'Add cameras', run: () => openDeviceManager() };
    const noRole = cams.find((c) => !c.role);
    if (noRole) {
      return {
        text: `Tell the system what "${noRole.name}" looks at (entrance, checkout, aisle…). That decides what it measures.`,
        label: 'Choose camera role',
        run: () => openCameraConfigModal(noRole.camera_id),
      };
    }
    const todo = cams.find((c) => !c.complete && c.required_total);
    if (todo) {
      const detail = await getJSON(`/api/v1/cameras/${encodeURIComponent(todo.camera_id)}/setup`, null);
      const item = detail && (detail.items || []).find((i) => i.required && !i.done);
      if (item) {
        const a = item.action || {};
        const text = `${todo.name}: ${item.label}.${item.hint ? ` ${item.hint}` : ''}`;
        if (a.type === 'calibrate') {
          return { text, label: 'Place on the map', run: () => { switchTab('map'); setTimeout(() => window.calibrationTool && window.calibrationTool.open(todo.camera_id), 120); } };
        }
        if (a.type === 'studio') {
          const q = new URLSearchParams({ camera_id: todo.camera_id, tool: a.tool || '' });
          if (a.kind) q.set('kind', a.kind);
          q.set('from', 'checklist');
          return { text, label: 'Open camera setup', run: () => { window.location.href = `/dashboard/studio?${q.toString()}`; } };
        }
        return { text, label: 'Open checklist', run: () => (window.edgeRoles ? window.edgeRoles.openChecklist(todo.camera_id) : openCameraConfigModal(todo.camera_id)) };
      }
    }
    const blocked = (setup.analytics || []).find((a) => a.status === 'blocked');
    if (blocked) return { text: blocked.message, label: null, run: null };
    return { text: 'Setup is complete for every camera role in use.', label: null, run: null };
  }

  async function renderSetup(setup) {
    const host = el('todaySetup');
    const badge = el('todaySetupScore');
    if (!host) return;
    if (!setup) { host.innerHTML = emptyState('Store setup status unavailable: the server did not respond.'); return; }
    const sc = setup.score || {};
    if (badge) {
      badge.textContent = isNum(sc.percent) ? `${sc.percent}% DONE` : 'ROLES NOT SET';
      badge.classList.toggle('metric-unobserved', !isNum(sc.percent));
      badge.classList.toggle('badge-green', isNum(sc.percent) && sc.percent >= 100);
      badge.title = isNum(sc.percent) ? `${sc.done} of ${sc.total} required setup steps done` : 'Give each camera a role to see what is left to set up';
    }
    const step = await nextStepFor(setup);
    lastNextStep = step;
    const rows = (setup.analytics || []).map((a) => `
      <li class="setup-item setup-${escapeHtml(a.status)}" title="${escapeHtml(a.message || '')}">
        <span class="setup-icon" aria-hidden="true">${STATUS_ICON[a.status] || '?'}</span>
        <span class="setup-label">${escapeHtml(a.label)}</span>
        <span class="setup-state">${escapeHtml(STATUS_WORD[a.status] || a.status)}</span>
        ${a.status !== 'available' ? `<span class="setup-msg">${escapeHtml(a.message || '')}</span>` : ''}
      </li>`).join('');
    host.innerHTML = `
      <div class="setup-next">
        <div class="setup-next-text"><b>Next step:</b> ${escapeHtml(step.text)}</div>
        ${step.label ? `<button type="button" class="btn btn-sm btn-primary" id="todayNextStep">${escapeHtml(step.label)}</button>` : ''}
      </div>
      ${window.edgeRoles ? window.edgeRoles.storeCamerasHtml(setup) : ''}
      <ul class="setup-list">${rows}</ul>`;
    const btn = el('todayNextStep');
    if (btn && step.run) btn.addEventListener('click', () => step.run());
  }

  // ----------------------------------------------------------------- load
  async function refresh() {
    if (loading || !visible()) return;
    loading = true;
    try {
      const [pipe, overview, hourly, incidents, recs, setup, layout, pos] = await Promise.all([
        getJSON('/api/v1/layout/pipeline/status', null),
        getJSON('/api/v1/analytics/overview', null),
        getJSON('/api/v1/analytics/footfall/hourly', null),
        getJSON('/api/v1/theft/incidents', null),
        getJSON('/api/v1/analytics/recommendations?days=7', null),
        getJSON('/api/v1/store/setup', null),
        getJSON('/api/v1/layout', null),
        getJSON('/api/v1/analytics/pos/status', null),
      ]);
      renderCameras(pipe);
      renderOverview(overview, hourly, pos);
      renderDataStateBanner(layout, overview, pipe);
      renderHourlyVisitors({ canvas: 'todayHourlyChart', badge: 'todayHourlySource', note: 'todayHourlyNote', legend: 'todayHourlyLegend' }, hourly);
      renderIncidents(incidents);
      renderRecs(recs);
      await renderSetup(setup);
    } finally {
      loading = false;
    }
  }

  function start() {
    clearInterval(timer);
    refresh();
    timer = setInterval(refresh, POLL_MS);
  }

  function init() {
    window.addEventListener('edge:tab', (e) => { if (e.detail && e.detail.tab === 'today') start(); });
    document.addEventListener('visibilitychange', () => { if (visible()) refresh(); });
    if (visible()) start();
  }

  window.edgeToday = { refresh: () => refresh(), nextStep: () => lastNextStep };

  if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') window.edgeAuth.onReady(init);
  else if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
