/**
 * Insights: one list of recommendations, the "Run analysis" button, and the
 * printable daily report.
 *
 * Owns #insights-recs and #tab-digest (sub-views of #tab-insights) and the nav
 * badge #tabActionCountBadge. Everything is read with plain GETs; the only
 * writes are POST /business/analysis/run (the explicit button) and the
 * per-recommendation status change. This file never calls
 * /market/llm-optimize: opening a tab must not start an analysis.
 *
 * Rules: nothing invented (empty states say why and what to do next), no
 * prompt/confirm/alert, every action shows a busy state and a result.
 */
(function () {
  'use strict';

  const API = '/api/v1/analytics';
  const BADGE_POLL_MS = 60000;
  const MODEL_REFRESH_MS = 5 * 60000;

  // ---------------------------------------------------------------- helpers
  const $ = (id) => document.getElementById(id);
  const dash = () => (typeof DASH === 'string' ? DASH : '—');
  const esc = (v) => (typeof escapeHtml === 'function'
    ? escapeHtml(v)
    : String(v === null || v === undefined ? '' : v).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const num = (v) => typeof v === 'number' && Number.isFinite(v);
  const toast = (msg, kind) => { if (typeof showToast === 'function') showToast(msg, kind); };

  async function getJ(url) {
    if (typeof getJSON === 'function') return getJSON(url, null);
    try { const r = await fetch(url); return r.ok ? await r.json() : null; } catch (_) { return null; }
  }

  function signedIn() {
    if (typeof canPoll === 'function') return canPoll();
    return !!(window.edgeAuth && window.edgeAuth.isAuthenticated && window.edgeAuth.isAuthenticated());
  }

  function toDate(ts) {
    if (!ts) return null;
    let s = String(ts);
    // Naive timestamps from the API are UTC; offset ISO strings parse as-is.
    if (/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(s)) s = `${s.replace(' ', 'T')}Z`;
    const d = new Date(s);
    return Number.isNaN(d.getTime()) ? null : d;
  }

  function clock(ts) {
    const d = toDate(ts);
    if (!d) return dash();
    const t = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    return d.toDateString() === new Date().toDateString() ? t : `${d.toLocaleDateString()} ${t}`;
  }

  function ago(ts) {
    if (typeof formatAgo === 'function') return formatAgo(ts);
    const d = toDate(ts);
    if (!d) return '';
    const s = Math.round((Date.now() - d.getTime()) / 1000);
    if (s < 60) return 'just now';
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} days ago`;
  }

  function humanKey(k) {
    const s = String(k || '').replace(/_/g, ' ').trim();
    return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
  }

  function isVisible(id) {
    const n = $(id);
    const tab = $('tab-insights');
    return !!(n && n.classList.contains('active') && (!tab || tab.classList.contains('active')) && !document.hidden);
  }

  const PRIORITY = {
    critical: { label: 'Critical', cls: 'badge-danger' },
    high: { label: 'High', cls: 'badge-warning' },
    medium: { label: 'Medium', cls: 'badge-primary' },
    low: { label: 'Low', cls: 'badge-neutral' },
    info: { label: 'Info', cls: 'badge-neutral' },
  };
  const STATUS = {
    PENDING: { label: 'Open', cls: 'badge-warning' },
    REVIEWED: { label: 'Reviewed', cls: 'badge-primary' },
    APPLIED: { label: 'Done', cls: 'badge-green' },
    DISMISSED: { label: 'Dismissed', cls: 'badge-neutral' },
    INFO: { label: 'Summary', cls: 'badge-neutral' },
  };
  const STATUS_BUTTON = {
    REVIEWED: { label: 'Mark reviewed', cls: '' },
    APPLIED: { label: 'Done', cls: 'btn-primary' },
    DISMISSED: { label: 'Dismiss', cls: '' },
    PENDING: { label: 'Reopen', cls: '' },
  };
  const STATUS_TOAST = {
    REVIEWED: 'Marked as reviewed', APPLIED: 'Marked as done', DISMISSED: 'Dismissed', PENDING: 'Reopened',
  };

  // ------------------------------------------------------------------ state
  const state = {
    filter: 'open',            // 'open' | 'all'
    showDismissed: false,
    data: null,                // last GET /recommendations
    signature: null,
    latest: null,
    modelAt: 0,
    busy: new Set(),           // recommendation ids being saved
    retryTimer: null,
    running: false,
  };

  // ------------------------------------------------------------ nav badge
  function setBadge(open) {
    const b = $('tabActionCountBadge');
    if (b) {
      if (num(open) && open > 0) { b.textContent = String(open); b.hidden = false; }
      else { b.textContent = '0'; b.hidden = true; }
    }
    const inner = $('recsOpenBadge');
    if (inner) {
      if (num(open)) {
        inner.textContent = `${open} OPEN`;
        inner.classList.remove('metric-unobserved');
        inner.classList.toggle('badge-warning', open > 0);
      } else {
        inner.textContent = dash();
        inner.classList.add('metric-unobserved');
        inner.classList.remove('badge-warning');
      }
    }
  }

  // ------------------------------------------------------- recommendations
  function recsUrl() {
    return `${API}/recommendations?days=7${state.showDismissed ? '&include_dismissed=true' : ''}`;
  }

  async function loadRecs(force) {
    if (!signedIn()) return;
    const data = await getJ(recsUrl());
    if (!data) {
      setBadge(null);
      if (isVisible('insights-recs')) {
        const list = $('recsList');
        if (list) list.innerHTML = '<div class="fp-empty">Recommendations are unavailable: the analytics service did not respond. They will load again automatically.</div>';
        state.signature = null;
      }
      return;
    }
    state.data = data;
    setBadge(data.counts ? data.counts.open : null);
    if (isVisible('insights-recs') || force) renderRecs(force);
  }

  function visibleItems() {
    const items = ((state.data && state.data.items) || []).filter((i) => i.source !== 'ai_summary');
    if (state.filter === 'open') return items.filter((i) => i.status === 'PENDING');
    return items;
  }

  function renderEvidence(item) {
    const ev = item.evidence;
    if (!item.evidence_available || ev === null || ev === undefined) {
      return '<div class="ins-evidence-none">Evidence not recorded for older findings.</div>';
    }
    if (typeof ev !== 'object') return `<div class="ins-evidence-none">${esc(ev)}</div>`;
    const rows = Object.entries(ev).map(([k, v]) => {
      let val;
      if (v === null || v === undefined) val = `<span class="metric-unobserved">${dash()}</span>`;
      else if (Array.isArray(v)) {
        if (!v.length) val = dash();
        else if (v.every((x) => x && typeof x === 'object')) {
          val = `<ul class="ins-ev-list">${v.slice(0, 8).map((x) => `<li>${esc(x.finding || x.title || x.zone || JSON.stringify(x))}${x.zone && (x.finding || x.title) ? ` <span class="ins-ev-sub">(${esc(x.zone)})</span>` : ''}</li>`).join('')}</ul>`;
        } else val = esc(v.join(', '));
      } else if (typeof v === 'object') {
        val = esc(Object.entries(v).map(([a, b]) => `${humanKey(a)}: ${b === null ? dash() : (typeof b === 'object' ? JSON.stringify(b) : b)}`).join(' · '));
      } else if (typeof v === 'number') val = esc(Number.isInteger(v) ? v.toLocaleString() : (Math.round(v * 100) / 100).toLocaleString());
      else if (typeof v === 'boolean') val = v ? 'yes' : 'no';
      else val = esc(v);
      return `<dt>${esc(humanKey(k))}</dt><dd>${val}</dd>`;
    });
    return rows.length ? `<details class="ins-evidence"><summary>Evidence (${rows.length})</summary><dl class="ins-ev-grid">${rows.join('')}</dl></details>`
      : '<div class="ins-evidence-none">No measurements were attached to this finding.</div>';
  }

  function renderItem(item) {
    const pr = PRIORITY[String(item.priority || '').toLowerCase()] || { label: item.priority || dash(), cls: 'badge-neutral' };
    const st = STATUS[item.status] || { label: item.status || dash(), cls: 'badge-neutral' };
    const busy = state.busy.has(item.id);
    const endpoint = item.status_endpoint;
    const buttons = endpoint
      ? (item.allowed_statuses || [])
        .filter((s) => s !== item.status && STATUS_BUTTON[s] && (s !== 'PENDING' || item.status !== 'PENDING'))
        .map((s) => `<button type="button" class="btn btn-sm ${STATUS_BUTTON[s].cls}" data-status="${esc(s)}" data-rec-id="${esc(item.id)}" ${busy ? 'disabled' : ''}>${busy ? 'Saving…' : esc(STATUS_BUTTON[s].label)}</button>`)
        .join('')
      : '';
    const sev = String(item.priority || 'info').toUpperCase();
    return `
      <article class="action-card ins-rec severity-${esc(sev)} status-${esc(item.status || '')}" data-rec="${esc(item.id)}">
        <div class="action-card-header ins-rec-head">
          <div class="ins-rec-badges">
            <span class="badge ${pr.cls}">${esc(pr.label)}</span>
            <span class="badge badge-neutral ins-source" title="Where this recommendation comes from">${esc(item.source_label || item.source || '')}</span>
            ${item.zone ? `<span class="ins-zone">${esc(item.zone)}</span>` : ''}
          </div>
          <span class="badge ${st.cls}">${esc(st.label)}</span>
        </div>
        <div class="action-title ins-rec-title">${esc(item.title || dash())}</div>
        <div class="action-desc ins-rec-body">
          ${item.why ? `<div><b>Why:</b> ${esc(item.why)}</div>` : ''}
          <div><b>Do:</b> ${esc(item.do || dash())}</div>
        </div>
        ${renderEvidence(item)}
        <div class="action-footer ins-rec-foot">
          <span class="action-meta">${esc(item.date || '')}${item.updated_at ? ` · updated ${esc(clock(item.updated_at))}` : ''}</span>
          <span class="action-btns">${buttons}</span>
        </div>
        <div class="ins-rec-status" data-rec-status="${esc(item.id)}" role="status" aria-live="polite"></div>
      </article>`;
  }

  function renderSummary(items) {
    const host = $('recsSummary');
    if (!host) return;
    const s = items.find((i) => i.source === 'ai_summary');
    if (!s) { host.innerHTML = ''; return; }
    const model = s.evidence && s.evidence.model;
    const based = (s.evidence && s.evidence.based_on_findings) || [];
    host.innerHTML = `
      <div class="ins-summary" data-rec="${esc(s.id)}">
        <div class="ins-summary-head"><span class="badge badge-neutral">${esc(s.source_label || 'AI summary')}</span>
          <span class="action-meta">${esc(clock(s.created_at))}${model ? ` · ${esc(model)}` : ''}</span></div>
        <div class="digest-narrative">${esc(s.do || '')}</div>
        ${based.length ? `<div class="ins-meta">Based on ${based.length} finding${based.length === 1 ? '' : 's'}: ${esc(based.map((f) => f.zone || f.category || '').filter(Boolean).join(', '))}</div>` : ''}
      </div>`;
  }

  function renderRecs(force) {
    const list = $('recsList');
    if (!list || !state.data) return;
    const all = state.data.items || [];
    const items = visibleItems();
    const sig = JSON.stringify([state.filter, state.showDismissed, [...state.busy],
      all.map((i) => [i.id, i.status, i.updated_at, i.do])]);
    // Do not rebuild identical markup: keeps open <details> and scroll position.
    if (!force && sig === state.signature && list.children.length) return;
    state.signature = sig;

    document.querySelectorAll('#insights-recs [data-rec-filter]').forEach((b) => {
      const on = b.getAttribute('data-rec-filter') === state.filter;
      b.classList.toggle('active', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    renderSummary(all);

    if (!items.length) {
      const openCount = state.data.counts ? state.data.counts.open : 0;
      let msg;
      if (state.data.empty_reason) msg = state.data.empty_reason;
      else if (state.filter === 'open' && all.some((i) => i.source !== 'ai_summary')) {
        msg = 'Nothing open: every recommendation from the last 7 days has been reviewed, done or dismissed. Choose "All" to see them.';
      } else msg = 'No recommendations in the last 7 days. The analysis found nothing that needs attention.';
      list.innerHTML = `<div class="fp-empty ins-empty">${esc(msg)}
        ${openCount ? '' : '<button type="button" class="btn btn-sm btn-primary" data-action="run-analysis">Run analysis now</button>'}</div>`;
      return;
    }
    list.innerHTML = items.map(renderItem).join('');
  }

  async function setStatus(id, status, btn) {
    const item = ((state.data && state.data.items) || []).find((i) => i.id === id);
    if (!item || !item.status_endpoint) return;
    state.busy.add(id);
    renderRecs(true);
    let ok = false;
    let msg = '';
    try {
      const res = await fetch(item.status_endpoint, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status }),
      });
      ok = res.ok;
      if (!ok) {
        const e = await res.json().catch(() => ({}));
        msg = `Could not update (HTTP ${res.status})${e.detail ? `: ${typeof e.detail === 'string' ? e.detail : JSON.stringify(e.detail)}` : ''}`;
      }
    } catch (e) {
      msg = `Could not update: ${e.message}`;
    }
    state.busy.delete(id);
    if (ok) {
      toast(`${STATUS_TOAST[status] || 'Updated'}: ${item.title || 'recommendation'}`, 'ok');
      await loadRecs(true);
    } else {
      renderRecs(true);
      const slot = document.querySelector(`[data-rec-status="${CSS.escape(id)}"]`);
      if (slot) slot.textContent = msg;
      toast(msg, 'error');
    }
    void btn;
  }

  // ------------------------------------------------------ run + last run
  function setRunStatus(text, kind) {
    const s = $('runAnalysisStatus');
    if (!s) return;
    s.textContent = text || '';
    s.className = `ins-run-status${kind ? ` ins-run-${kind}` : ''}`;
  }

  function runButtons() { return document.querySelectorAll('#insights-recs [data-action="run-analysis"]'); }

  function setRunBusy(busy, label) {
    runButtons().forEach((b) => {
      b.disabled = !!busy;
      if (b.id === 'btnRunAnalysis') b.textContent = label || (busy ? 'Analysing…' : 'Run analysis now');
    });
  }

  function startRetryCountdown(seconds, detail) {
    clearInterval(state.retryTimer);
    let left = Math.max(1, Math.ceil(seconds));
    const tick = () => {
      if (left <= 0) {
        clearInterval(state.retryTimer);
        state.retryTimer = null;
        setRunBusy(false);
        setRunStatus('You can run the analysis again now.', 'ok');
        return;
      }
      setRunBusy(true, `Try again in ${left}s`);
      setRunStatus(`${detail || 'The analysis ran moments ago.'} The button unlocks in ${left} s.`, 'warn');
      left -= 1;
    };
    tick();
    state.retryTimer = setInterval(tick, 1000);
  }

  async function runAnalysis() {
    if (state.running || state.retryTimer) return;
    const narrate = !!($('runNarrate') && $('runNarrate').checked);
    state.running = true;
    setRunBusy(true, 'Analysing…');
    setRunStatus(narrate ? 'Analysing today’s measurements… the AI summary can take up to a minute.' : 'Analysing today’s measurements…');
    try {
      const res = await fetch(`${API}/business/analysis/run`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ narrate }),
      });
      const body = await res.json().catch(() => ({}));
      if (res.status === 429) {
        const hdr = parseInt(res.headers.get('Retry-After') || '', 10);
        const wait = num(body.retry_after) ? body.retry_after : (Number.isFinite(hdr) ? hdr : 60);
        state.running = false;
        startRetryCountdown(wait, typeof body.detail === 'string' ? body.detail : null);
        return;
      }
      if (res.status === 409) {
        setRunStatus('An analysis is already running. The list updates when it finishes.', 'warn');
        toast('An analysis is already running', 'error');
        setTimeout(() => { loadLatest(); loadRecs(true); }, 5000);
        return;
      }
      if (!res.ok) {
        const d = typeof body.detail === 'string' ? body.detail : (body.detail ? JSON.stringify(body.detail) : '');
        setRunStatus(`Analysis failed (HTTP ${res.status})${d ? `: ${d}` : ''}.`, 'err');
        toast(`Analysis failed (HTTP ${res.status})`, 'error');
        return;
      }
      const n = num(body.findings_count) ? body.findings_count : null;
      const narrated = body.narrated ? ' with an AI summary' : '';
      const msg = n === null ? `Analysis finished${narrated}.` : `Analysis finished${narrated}: ${n} finding${n === 1 ? '' : 's'}.`;
      setRunStatus(msg, 'ok');
      toast(msg, 'ok');
      state.latest = { run: body, generated_at: body.generated_at, inputs_summary: body.inputs_summary, schedule: state.latest && state.latest.schedule };
      renderLatest();
      await Promise.all([loadLatest(), loadRecs(true)]);
    } catch (e) {
      setRunStatus(`Analysis failed: ${e.message}`, 'err');
      toast(`Analysis failed: ${e.message}`, 'error');
    } finally {
      state.running = false;
      if (!state.retryTimer) setRunBusy(false);
    }
  }

  async function loadLatest() {
    if (!signedIn()) return;
    const data = await getJ(`${API}/business/analysis/latest`);
    if (!data) return;
    state.latest = data;
    renderLatest();
  }

  const SOURCE_WORDS = {
    tripwire: 'counted at entrance lines',
    tripwires: 'counted at entrance lines',
    zone_visits: 'estimated from area visits',
    tracks: 'estimated from people tracked',
  };

  function renderLatest() {
    const d = state.latest;
    const last = $('recsLastRun');
    const inputs = $('recsInputs');
    if (!d) return;
    const run = d.run;
    const sched = d.schedule || {};
    const next = sched.next_run_at ? ` · next automatic run ${clock(sched.next_run_at)}` : (sched.interval_seconds ? '' : ' · automatic runs are off');
    if (last) {
      if (run && (run.generated_at || d.generated_at)) {
        const at = run.generated_at || d.generated_at;
        const who = run.trigger === 'schedule' ? 'automatic' : (run.requested_by ? `run by ${run.requested_by}` : 'run manually');
        const count = num(run.findings_count) ? ` · ${run.findings_count} finding${run.findings_count === 1 ? '' : 's'}` : '';
        last.textContent = `Last analysis ${clock(at)} (${ago(at)}, ${who})${count}${next}`;
      } else {
        last.textContent = `${d.message || 'No analysis has run yet.'}${next}`;
      }
    }
    if (inputs) {
      const s = d.inputs_summary || (run && run.inputs_summary);
      if (!s) { inputs.textContent = ''; return; }
      const parts = [];
      if (num(s.footfall)) {
        const src = SOURCE_WORDS[s.footfall_source] || (s.footfall_source ? String(s.footfall_source).replace(/_/g, ' ') : '');
        parts.push(`${s.footfall.toLocaleString()} visitors${src ? ` (${src})` : ''}`);
      } else parts.push('visitors not measured');
      if (num(s.zones_assessed) && num(s.zones_total)) parts.push(`${s.zones_assessed} of ${s.zones_total} areas assessed`);
      if (num(s.shelf_reaches)) parts.push(`${s.shelf_reaches.toLocaleString()} shelf reaches`);
      if (typeof s.pos_connected === 'boolean') parts.push(`sales data ${s.pos_connected ? 'connected' : 'not connected'}`);
      if (s.heatmap_history_used) parts.push('recorded heatmaps used');
      let txt = `Based on: ${parts.join(' · ')}.`;
      if (s.sufficient_data === false) txt += ' Not enough data yet for firm conclusions.';
      inputs.textContent = txt;
    }
  }

  async function loadModelStatus(force) {
    const host = $('recsModelStatus');
    if (!host || !signedIn()) return;
    if (!force && Date.now() - state.modelAt < MODEL_REFRESH_MS) return;
    state.modelAt = Date.now();
    const s = await getJ(`${API}/market/llm-status`);
    if (!s) { host.textContent = 'AI model status unavailable.'; return; }
    if (s.ollama_active && s.model) {
      host.textContent = `Local AI model ${s.model} ${s.generation_verified ? 'is ready (answered a test)' : 'is installed but has not answered a test yet'}. It is only used for the optional written summary.`;
    } else {
      host.textContent = `No local AI model available${s.reason ? `: ${s.reason}` : ''}. Recommendations still work; only the written summary needs a model.`;
    }
  }

  // ----------------------------------------------------------- daily report
  async function loadDigest() {
    const host = $('digestNarrative');
    if (!host || !signedIn()) return;
    const btn = $('btnRefreshDigest');
    if (btn) { btn.disabled = true; btn.textContent = 'Loading…'; }
    const data = await getJ(`${API}/digest`);
    if (btn) { btn.disabled = false; btn.textContent = 'Refresh'; }
    if (!data) {
      host.innerHTML = '<div class="fp-empty">The daily report is unavailable: the analytics service did not respond. Press Refresh to try again.</div>';
      return;
    }
    const store = $('digestStoreName');
    if (store && data.report_title) store.textContent = String(data.report_title).replace(/^Daily Intelligence Digest - /, '');

    const card = data.kpi_scorecard || {};
    const labels = data.kpi_scorecard_labels || {};
    const cov = data.coverage || {};
    const tiles = Object.entries(card).map(([k, v]) => {
      const missing = v === null || v === undefined || v === 'Not observed';
      return `<div class="stat-box">
        <span class="stat-label">${esc(labels[k] || humanKey(k))}</span>
        <span class="stat-val ins-report-val ${missing ? 'metric-unobserved' : ''}">${esc(missing ? dash() : v)}</span>
        ${missing ? '<span class="ins-report-note">not measured</span>' : ''}
      </div>`;
    }).join('');
    const findings = data.findings || [];
    const findingsHtml = findings.length
      ? findings.map((f) => `
        <div class="action-card severity-${esc(f.severity || 'INFO')}">
          <div class="action-card-header">
            <span class="badge ${f.severity === 'CRITICAL' ? 'badge-danger' : (f.severity === 'HIGH' ? 'badge-warning' : 'badge-neutral')}">${esc((PRIORITY[String(f.severity || '').toLowerCase()] || {}).label || f.severity || '')}</span>
            <span class="action-title">${esc(f.zone || 'Store')}: ${esc(f.finding || '')}</span>
          </div>
          <div class="action-desc"><b>Why:</b> ${esc(f.root_cause || dash())}<br><b>Do:</b> ${esc(f.action_item || dash())}</div>
        </div>`).join('')
      : `<div class="fp-empty">${esc(data.analysis_message || 'No findings for this day.')}</div>`;

    host.innerHTML = `
      <p class="ins-report-summary"><b>${esc(data.date || '')}</b> · ${esc(data.executive_summary || 'No summary produced.')}</p>
      <div class="overview-strip ins-report-kpis">${tiles}</div>
      <div class="action-meta">Coverage: ${esc(cov.cameras_total ?? dash())} camera(s), ${esc(cov.cameras_calibrated ?? dash())} placed on the store map · ${data.data_available ? 'shopper activity recorded' : 'no shopper activity recorded for this day'}</div>
      <h4 class="ins-report-h">Findings (${esc(data.findings_count ?? findings.length)})</h4>
      <div class="ins-list">${findingsHtml}</div>
      <div class="action-meta ins-report-gen">Report generated ${esc(clock(data.generated_at))}</div>`;
  }

  // ------------------------------------------------------------ wiring
  function onRecsShown() {
    loadRecs(true);
    loadLatest();
    loadModelStatus(false);
  }

  function bind() {
    const recs = $('insights-recs');
    if (recs) {
      recs.addEventListener('click', (e) => {
        const run = e.target.closest('[data-action="run-analysis"]');
        if (run) { e.preventDefault(); runAnalysis(); return; }
        const chip = e.target.closest('[data-rec-filter]');
        if (chip) {
          state.filter = chip.getAttribute('data-rec-filter') === 'all' ? 'all' : 'open';
          renderRecs(true);
          return;
        }
        const sb = e.target.closest('[data-status][data-rec-id]');
        if (sb && !sb.disabled) setStatus(sb.getAttribute('data-rec-id'), sb.getAttribute('data-status'), sb);
      });
      const dis = $('recsShowDismissed');
      if (dis) dis.addEventListener('change', () => { state.showDismissed = dis.checked; if (dis.checked) state.filter = 'all'; loadRecs(true); });
    }
    const pr = $('btnPrintDigest');
    if (pr) pr.addEventListener('click', () => window.print());
    const rf = $('btnRefreshDigest');
    if (rf) rf.addEventListener('click', () => loadDigest());
  }

  function onTab(detail) {
    if (!detail || detail.tab !== 'insights') return;
    const sub = detail.sub || 'recs';
    if (sub === 'recs') onRecsShown();
    else if (sub === 'report') loadDigest();
  }

  async function topRecommendations(n) {
    const data = await getJ(`${API}/recommendations?days=7`);
    if (!data) return [];
    return (data.items || [])
      .filter((i) => i.status === 'PENDING' && i.source !== 'ai_summary')
      .sort((a, b) => (a.priority_rank ?? 9) - (b.priority_rank ?? 9))
      .slice(0, n || 3);
  }

  function init() {
    bind();
    window.addEventListener('edge:tab', (e) => onTab(e.detail));
    loadRecs(false);   // badge
    if (isVisible('insights-recs')) onRecsShown();
    if (isVisible('tab-digest')) loadDigest();
    setInterval(() => {
      loadRecs(false);
      if (isVisible('insights-recs')) { loadLatest(); loadModelStatus(false); }
    }, BADGE_POLL_MS);
  }

  window.edgeInsights = {
    refresh() { loadRecs(true); loadLatest(); if (isVisible('tab-digest')) loadDigest(); },
    topRecommendations,
  };

  if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') {
    window.edgeAuth.onReady(init);
  } else if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
