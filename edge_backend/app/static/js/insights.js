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
 * Default view: findings grouped into recurring issues ("topics", GET
 * /recommendations/topics) with one unified analysis and action plan each;
 * "Show every insight" brings back the flat list. Topic writes are the
 * acknowledge / snooze / resolve / reopen / action-done / split / merge /
 * re-analyse POSTs. The badges count open topics, not raw findings.
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
    REVIEWED: { label: 'Mark reviewed', cls: 'btn-secondary' },
    APPLIED: { label: 'Done', cls: 'btn-primary' },
    DISMISSED: { label: 'Dismiss', cls: 'btn-secondary' },
    PENDING: { label: 'Reopen', cls: 'btn-secondary' },
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
    view: 'topics',            // 'topics' | 'flat' ("Show every insight")
    topics: null,              // last GET /recommendations/topics
    topicsOk: false,           // the topics endpoint answered
    topicSig: null,
    topicBusy: new Set(),      // topic ids being saved
    members: {},               // topic id -> { loading, data }
    openMembers: new Set(),    // topic ids whose daily findings are expanded
    mergeOpen: null,           // topic id showing the merge picker
  };
  try { if (localStorage.getItem('edge.insights.view') === 'flat') state.view = 'flat'; } catch (_) { /* private mode */ }

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
    if (!state.topicsOk) setBadge(data.counts ? data.counts.open : null);
    if (isVisible('insights-recs') || force) renderRecs(force);
  }

  // ------------------------------------------------------------ topics
  const TREND = {
    worsening: { icon: 'trending-up', cls: 'ins-trend-worse' },
    improving: { icon: 'trending-up', cls: 'ins-trend-better ins-trend-down' },
    stable: { icon: 'minus', cls: 'ins-trend-stable' },
    recurring: { icon: 'refresh-cw', cls: 'ins-trend-recurring' },
    new: { icon: 'circle-alert', cls: 'ins-trend-new' },
  };
  const TOPIC_STATUS = {
    open: { label: 'Open', cls: 'badge-warning' },
    acknowledged: { label: 'Acknowledged', cls: 'badge-primary' },
    snoozed: { label: 'Snoozed', cls: 'badge-neutral' },
    resolved: { label: 'Resolved', cls: 'badge-green' },
  };
  const TOPIC_TOAST = {
    acknowledge: 'Acknowledged', snooze: 'Snoozed for 7 days', resolve: 'Resolved', reopen: 'Reopened',
    'action-done': 'Action marked done', split: 'Finding moved to its own topic', merge: 'Topics merged',
    reanalyse: 'Analysis refreshed',
  };
  const icon = (name, cls) => (window.EdgeIcon && typeof window.EdgeIcon.svg === 'function'
    ? window.EdgeIcon.svg(name, { size: 'sm', cls: cls || '' }) : '');

  function shortDay(iso) {
    if (!iso) return dash();
    const d = new Date(`${String(iso).slice(0, 10)}T12:00:00`);
    if (Number.isNaN(d.getTime())) return esc(iso);
    const opts = { day: 'numeric', month: 'short' };
    if (d.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
    return d.toLocaleDateString('en-AU', opts);
  }

  function ensureTopicDom() {
    const list = $('recsList');
    if (!list) return null;
    let host = $('topicsList');
    if (!host) {
      host = document.createElement('div');
      host.id = 'topicsList';
      host.className = 'ins-list ins-topics';
      list.parentNode.insertBefore(host, $('recsSummary') || list);
    }
    const row = document.querySelector('#insights-recs .ins-filter-row');
    if (row && !$('recsShowEvery')) {
      const lab = document.createElement('label');
      lab.className = 'ins-check ins-every';
      lab.setAttribute('for', 'recsShowEvery');
      lab.title = 'Show each daily finding on its own instead of grouped issues';
      lab.innerHTML = '<input type="checkbox" id="recsShowEvery" name="recsShowEvery"> Show every insight';
      row.appendChild(lab);
      const cb = lab.querySelector('input');
      cb.checked = state.view === 'flat';
      cb.addEventListener('change', () => {
        state.view = cb.checked ? 'flat' : 'topics';
        try { localStorage.setItem('edge.insights.view', state.view); } catch (_) { /* ignore */ }
        applyView();
        if (state.view === 'flat') renderRecs(true); else renderTopics(true);
      });
    }
    return host;
  }

  function topicsMode() { return state.view === 'topics' && state.topicsOk; }

  function applyView() {
    const host = ensureTopicDom();
    const on = topicsMode();
    if (host) host.hidden = !on;
    const list = $('recsList');
    if (list) list.hidden = on;
    const sum = $('recsSummary');
    if (sum) sum.hidden = on;
    const dis = $('recsShowDismissed');
    const disLab = dis && dis.closest('label');
    if (disLab) disLab.hidden = on;
    const every = $('recsShowEvery');
    if (every) every.checked = state.view === 'flat';
    const lab = every && every.closest('label');
    if (lab) lab.hidden = !state.topicsOk;
  }

  async function loadTopics(force) {
    if (!signedIn()) return;
    let data = null;
    try {
      const r = await fetch(`${API}/recommendations/topics${state.filter === 'all' ? '?include_resolved=true' : ''}`);
      data = r.ok ? await r.json() : null;
    } catch (_) { data = null; }
    const was = state.topicsOk;
    state.topicsOk = !!(data && Array.isArray(data.topics));
    if (state.topicsOk) {
      state.topics = data;
      setBadge(data.counts ? data.counts.badge : null);
    } else if (state.data) setBadge(state.data.counts ? state.data.counts.open : null);
    if (was !== state.topicsOk) applyView();
    if (isVisible('insights-recs') || force) {
      applyView();
      if (topicsMode()) renderTopics(force);
      else if (!was && !state.topicsOk) renderRecs(force);
    }
  }

  function visibleTopics() {
    const all = (state.topics && state.topics.topics) || [];
    if (state.filter === 'open') return all.filter((t) => t.status === 'open' || t.status === 'acknowledged');
    return all;
  }

  function sparkline(t) {
    const tl = t.timeline || [];
    if (!tl.length) return '';
    const w = 6;
    const seen = tl.filter((d) => d.seen).length;
    const bars = tl.map((d, i) => {
      const h = d.seen ? 16 : (d.analysed ? 4 : 2);
      const cls = d.seen ? 'ins-spark-seen' : (d.analysed ? 'ins-spark-clear' : 'ins-spark-none');
      return `<rect class="${cls}" x="${i * w}" y="${18 - h}" width="${w - 2}" height="${h}" rx="1"><title>${esc(shortDay(d.date))}: ${d.seen ? 'found' : (d.analysed ? 'not found' : 'no analysis')}</title></rect>`;
    }).join('');
    return `<div class="ins-spark-wrap"><svg class="ins-spark" viewBox="0 0 ${tl.length * w} 18" preserveAspectRatio="none" role="img" aria-label="Found on ${seen} of the last ${tl.length} days">${bars}</svg>
      <span class="ins-spark-axis"><span>${esc(shortDay(tl[0].date))}</span><span>today</span></span></div>`;
  }

  function renderPlan(t) {
    const acts = t.actions || [];
    if (!acts.length) return '';
    const busy = state.topicBusy.has(t.id);
    const items = acts.map((a) => {
      const meta = [
        a.why ? `<span><b>Why:</b> ${esc(a.why)}</span>` : '',
        a.expected_effect ? `<span><b>Expect:</b> ${esc(a.expected_effect)}</span>` : '',
        `<span><b>Effort:</b> ${esc(a.effort || dash())}</span>`,
        a.owner_role ? `<span><b>Who:</b> ${esc(a.owner_role)}</span>` : '',
        a.check_after ? `<span><b>Check:</b> ${esc(shortDay(a.check_after))}</span>` : '',
      ].filter(Boolean).join('');
      const ctl = a.done
        ? `<span class="ins-plan-done">${icon('check')} Done${a.done_at ? ` ${esc(shortDay(a.done_at))}` : ''}</span>`
        : `<button type="button" class="btn btn-sm btn-secondary" data-topic-act="action-done" data-topic-id="${esc(t.id)}" data-action-id="${esc(a.id)}" aria-label="Mark done: ${esc(a.action)}" ${busy ? 'disabled' : ''}>${icon('check')} Mark done</button>`;
      return `<li class="ins-plan-item${a.done ? ' is-done' : ''}">
          <div class="ins-plan-main"><span class="ins-plan-n" aria-hidden="true">${esc(a.priority || '')}</span><span class="ins-plan-text">${esc(a.action)}</span>${ctl}</div>
          <div class="ins-plan-meta">${meta}</div>
        </li>`;
    }).join('');
    return `<div class="ins-plan"><div class="ins-plan-h">Action plan</div><ol class="ins-plan-list">${items}</ol></div>`;
  }

  function renderMembers(t) {
    const m = state.members[t.id];
    if (!m || m.loading) return '<div class="ins-meta">Loading the daily findings…</div>';
    if (!m.data) return '<div class="ins-meta">The daily findings could not be loaded. Close and open this again to retry.</div>';
    const rows = m.data.insights || [];
    const canSplit = rows.length > 1;
    const busy = state.topicBusy.has(t.id);
    return rows.map((r) => {
      const pr = PRIORITY[String(r.severity || '').toLowerCase()] || { label: r.severity || '', cls: 'badge-neutral' };
      const st = STATUS[r.status] || { label: r.status || '', cls: 'badge-neutral' };
      return `<div class="ins-member">
          <div class="ins-member-head"><b>${esc(shortDay(r.date))}</b>
            <span class="badge ${pr.cls}">${esc(pr.label)}</span><span class="badge ${st.cls}">${esc(st.label)}</span>
            ${r.zone ? `<span class="ins-zone">${esc(r.zone)}</span>` : ''}</div>
          <div class="ins-member-text">${esc(r.title || '')}</div>
          ${r.do ? `<div class="ins-member-do"><b>Do:</b> ${esc(r.do)}</div>` : ''}
          ${canSplit ? `<button type="button" class="btn btn-sm btn-ghost ins-member-split" data-topic-act="split" data-topic-id="${esc(t.id)}" data-insight-id="${esc(r.id)}" ${busy ? 'disabled' : ''}>${icon('x')} Not the same issue</button>` : ''}
        </div>`;
    }).join('') || '<div class="ins-meta">No findings left in this topic.</div>';
  }

  function renderBeforeAfter(t) {
    const ba = t.before_after;
    if (!ba || !ba.text) return '';
    const cls = { helped: 'ins-ba-good', worse: 'ins-ba-bad', no_clear_change: 'ins-ba-flat', too_early: 'ins-ba-flat' }[ba.verdict] || 'ins-ba-flat';
    return `<div class="ins-ba ${cls}"><b>Before and after:</b> ${esc(ba.text)}</div>`;
  }

  function renderTopic(t) {
    const pr = PRIORITY[t.priority] || { label: t.priority || dash(), cls: 'badge-neutral' };
    const tr = TREND[t.trend] || TREND.stable;
    const st = TOPIC_STATUS[t.status] || TOPIC_STATUS.open;
    const busy = state.topicBusy.has(t.id);
    const dis = busy ? 'disabled' : '';
    const btn = (act, label, ic, cls) => `<button type="button" class="btn btn-sm ${cls || 'btn-secondary'}" data-topic-act="${act}" data-topic-id="${esc(t.id)}" ${dis}>${icon(ic)} ${esc(label)}</button>`;
    const where = [t.subject && t.subject !== t.title ? t.subject : null, t.zone_name && t.zone_name !== t.subject ? t.zone_name : null]
      .filter(Boolean).join(' · ');
    const prods = (t.products || []).length ? `Sells: ${(t.products || []).slice(0, 6).join(', ')}` : '';
    const snooze = t.status === 'snoozed' && t.snooze_until ? ` until ${shortDay(t.snooze_until)}` : '';
    const newSince = num(t.new_since_acknowledged) && t.new_since_acknowledged > 0
      ? ` · ${t.new_since_acknowledged} new since acknowledged` : '';
    const buttons = [
      t.status === 'open' ? btn('acknowledge', 'Acknowledge', 'eye', 'btn-primary') : '',
      t.status !== 'snoozed' && t.status !== 'resolved' ? btn('snooze', 'Snooze 7 days', 'clock') : '',
      t.status !== 'resolved' ? btn('resolve', 'Resolve', 'circle-check') : '',
      t.status !== 'open' ? btn('reopen', 'Reopen', 'undo-2') : '',
      btn('merge-open', 'Merge…', 'minimize-2', 'btn-ghost'),
      btn('reanalyse', 'Re-analyse', 'refresh-cw', 'btn-ghost'),
    ].join('');
    let merge = '';
    if (state.mergeOpen === t.id) {
      const others = ((state.topics && state.topics.topics) || []).filter((o) => o.id !== t.id);
      merge = others.length
        ? `<div class="ins-merge"><label class="ins-merge-label" for="merge-${esc(t.id)}">Merge into this issue:</label>
            <select id="merge-${esc(t.id)}" class="ins-merge-select" data-merge-select="${esc(t.id)}">${others.map((o) => `<option value="${esc(o.id)}">${esc(o.title)} (${esc(o.occurrences)} day${o.occurrences === 1 ? '' : 's'})</option>`).join('')}</select>
            ${btn('merge', 'Merge', 'minimize-2', 'btn-primary')}${btn('merge-cancel', 'Cancel', 'x', 'btn-ghost')}</div>`
        : `<div class="ins-merge"><span class="ins-meta">There is no other issue to merge with.</span>${btn('merge-cancel', 'Close', 'x', 'btn-ghost')}</div>`;
    }
    const source = t.summary_source === 'local_model'
      ? `<div class="ins-meta">Summary worded by the local AI model${t.model ? ` (${esc(t.model)})` : ''} from the measured facts.</div>`
      : (t.narration_note ? `<div class="ins-meta">${esc(t.narration_note)}</div>` : '');
    const extra = [t.pattern, t.time_of_day].filter(Boolean).join(' ');
    const analysed = t.analysed_at ? `Analysed ${clock(t.analysed_at)}` : 'Not analysed yet';
    const win = t.data_window && t.data_window.from ? ` · data ${shortDay(t.data_window.from)} to ${shortDay(t.data_window.to)}` : '';
    const open = state.openMembers.has(t.id);
    return `
      <article class="action-card ins-topic severity-${esc(t.severity || 'MEDIUM')} topic-${esc(t.status)}" data-topic="${esc(t.id)}" aria-labelledby="topic-h-${esc(t.id)}">
        <div class="ins-topic-head">
          <div class="ins-rec-badges">
            <span class="badge ${pr.cls}">${esc(pr.label)}</span>
            <span class="ins-trend ${tr.cls}" title="${esc(t.trend_reason || '')}">${icon(tr.icon)}<span>${esc(t.trend_label || t.trend || '')}</span></span>
            ${t.status !== 'open' ? `<span class="badge ${st.cls}">${esc(st.label)}${esc(snooze)}</span>` : ''}
          </div>
          <span class="ins-topic-seen">${esc(t.seen_text || '')}${t.last_seen ? ` · last ${esc(shortDay(t.last_seen))}` : ''}${esc(newSince)}</span>
        </div>
        <h3 class="ins-topic-title" id="topic-h-${esc(t.id)}">${esc(t.title || dash())}</h3>
        ${where || prods ? `<div class="ins-topic-where">${esc([where, prods].filter(Boolean).join(' · '))}</div>` : ''}
        ${sparkline(t)}
        <p class="ins-topic-summary">${esc(t.unified_summary || 'The unified analysis is being prepared; it appears after the next analysis run.')}</p>
        ${extra && !(t.unified_summary || '').includes(extra) ? `<div class="ins-meta">${esc(extra)}</div>` : ''}
        ${source}
        ${renderBeforeAfter(t)}
        ${renderPlan(t)}
        <div class="action-footer ins-topic-foot">
          <span class="action-meta">${esc(analysed)}${esc(win)}</span>
          <span class="action-btns">${buttons}</span>
        </div>
        ${merge}
        <details class="ins-topic-members" data-topic-members="${esc(t.id)}" ${open ? 'open' : ''}>
          <summary>Daily findings (${esc(t.occurrences)} day${t.occurrences === 1 ? '' : 's'})</summary>
          <div class="ins-members-body">${open ? renderMembers(t) : ''}</div>
        </details>
        <div class="ins-rec-status" data-topic-status="${esc(t.id)}" role="status" aria-live="polite"></div>
      </article>`;
  }

  function renderTopics(force) {
    const host = ensureTopicDom();
    if (!host || !state.topics || !topicsMode()) return;
    const items = visibleTopics();
    const sig = JSON.stringify([state.filter, state.mergeOpen, [...state.topicBusy], [...state.openMembers],
      Object.keys(state.members).map((k) => [k, !!(state.members[k] && state.members[k].loading)]),
      (state.topics.topics || []).map((t) => [t.id, t.status, t.analysed_at, t.occurrences, t.trend, (t.actions || []).map((a) => a.done)])]);
    if (!force && sig === state.topicSig && host.children.length) return;
    state.topicSig = sig;
    document.querySelectorAll('#insights-recs [data-rec-filter]').forEach((b) => {
      const on = b.getAttribute('data-rec-filter') === state.filter;
      b.classList.toggle('active', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    if (!items.length) {
      const all = state.topics.topics || [];
      const waiting = state.topics.unassigned_insights || 0;
      let msg;
      if (waiting) msg = `Grouping ${waiting} finding${waiting === 1 ? '' : 's'} into issues… this list fills in by itself. Tick "Show every insight" to see them now.`;
      else if (state.filter === 'open' && all.length) msg = 'Nothing open: every issue is snoozed or resolved. Choose "All" to see them.';
      else if (state.data && state.data.empty_reason) msg = state.data.empty_reason;
      else msg = 'No issues found. The analysis found nothing that needs attention.';
      host.innerHTML = `<div class="fp-empty ins-empty">${esc(msg)}
        ${all.length ? '' : '<button type="button" class="btn btn-sm btn-primary" data-action="run-analysis">Run analysis now</button> <button type="button" class="link-btn" data-help-go="feature:insights">What it needs (Features)</button>'}</div>`;
      return;
    }
    const counts = state.topics.counts || {};
    const head = `<div class="ins-meta ins-topics-count">${esc(counts.open || 0)} open · ${esc(counts.acknowledged || 0)} acknowledged · ${esc(counts.snoozed || 0)} snoozed${state.filter === 'all' ? ` · ${esc(counts.resolved || 0)} resolved` : ''}. Each card groups the same finding across days.</div>`;
    host.innerHTML = head + items.map(renderTopic).join('');
  }

  async function loadMembers(id) {
    state.members[id] = { loading: true, data: null };
    renderTopics(true);
    const data = await getJ(`${API}/recommendations/topics/${encodeURIComponent(id)}`);
    state.members[id] = { loading: false, data };
    renderTopics(true);
  }

  async function topicAction(btn) {
    const id = btn.getAttribute('data-topic-id');
    const act = btn.getAttribute('data-topic-act');
    if (!id || !act) return;
    if (act === 'merge-open') { state.mergeOpen = id; renderTopics(true); return; }
    if (act === 'merge-cancel') { state.mergeOpen = null; renderTopics(true); return; }
    let body = {};
    if (act === 'snooze') body = { days: 7 };
    else if (act === 'action-done') body = { action_id: btn.getAttribute('data-action-id') };
    else if (act === 'split') body = { insight_id: btn.getAttribute('data-insight-id') };
    else if (act === 'merge') {
      const sel = document.querySelector(`[data-merge-select="${CSS.escape(id)}"]`);
      if (!sel || !sel.value) return;
      body = { other_topic_id: sel.value };
    }
    const t = ((state.topics && state.topics.topics) || []).find((x) => x.id === id);
    state.topicBusy.add(id);
    renderTopics(true);
    let ok = false;
    let msg = '';
    try {
      const res = await fetch(`${API}/recommendations/topics/${encodeURIComponent(id)}/${act}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      ok = res.ok;
      if (!ok) {
        const e = await res.json().catch(() => ({}));
        msg = `Could not update (HTTP ${res.status})${e.detail ? `: ${typeof e.detail === 'string' ? e.detail : JSON.stringify(e.detail)}` : ''}`;
      }
    } catch (e) {
      msg = `Could not update: ${e.message}`;
    }
    state.topicBusy.delete(id);
    if (ok) {
      toast(`${TOPIC_TOAST[act] || 'Updated'}: ${(t && t.title) || 'issue'}`, 'ok');
      if (act === 'merge') state.mergeOpen = null;
      if (act === 'split' || act === 'merge') state.members = {};
      else delete state.members[id];
      await Promise.all([loadTopics(true), loadRecs(false)]);
      state.openMembers.forEach((mid) => { if (!state.members[mid]) loadMembers(mid); });
      const card = document.querySelector(`[data-topic="${CSS.escape(id)}"]`);
      const again = card && card.querySelector(`[data-topic-act="${CSS.escape(act)}"]`);
      if (again && !again.disabled) again.focus();
    } else {
      renderTopics(true);
      const slot = document.querySelector(`[data-topic-status="${CSS.escape(id)}"]`);
      if (slot) slot.textContent = msg;
      toast(msg, 'error');
    }
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
        setTimeout(() => { loadLatest(); loadRecs(true); loadTopics(true); }, 5000);
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
      await Promise.all([loadLatest(), loadRecs(true), loadTopics(true)]);
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
      <div class="action-meta">Coverage: ${esc(cov.cameras_total ?? dash())} camera(s), ${isFinite(cov.cameras_placed) ? `${esc(cov.cameras_placed)} placed on the store map, ` : ''}${esc(cov.cameras_calibrated ?? dash())} calibrated · ${data.data_available ? 'shopper activity recorded' : 'no shopper activity recorded for this day'}</div>
      <h4 class="ins-report-h">Findings (${esc(data.findings_count ?? findings.length)})</h4>
      <div class="ins-list">${findingsHtml}</div>
      <div class="action-meta ins-report-gen">Report generated ${esc(clock(data.generated_at))}</div>`;
  }

  // ------------------------------------------------------------ wiring
  function onRecsShown() {
    ensureTopicDom();
    applyView();
    loadRecs(true);
    loadTopics(true);
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
          if (state.topicsOk) loadTopics(true);
          return;
        }
        const tb = e.target.closest('[data-topic-act][data-topic-id]');
        if (tb && !tb.disabled) { e.preventDefault(); topicAction(tb); return; }
        const sb = e.target.closest('[data-status][data-rec-id]');
        if (sb && !sb.disabled) setStatus(sb.getAttribute('data-rec-id'), sb.getAttribute('data-status'), sb);
      });
      // <details> toggle does not bubble: listen in the capture phase.
      recs.addEventListener('toggle', (e) => {
        const d = e.target;
        if (!d || !d.matches || !d.matches('[data-topic-members]')) return;
        const id = d.getAttribute('data-topic-members');
        if (d.open) {
          // A re-render that keeps the panel open fires toggle again: nothing to do then.
          if (state.openMembers.has(id) && state.members[id]) return;
          state.openMembers.add(id);
          const m = state.members[id];
          if (!m || (!m.loading && !m.data)) loadMembers(id);
          else renderTopics(true);
        } else state.openMembers.delete(id);
      }, true);
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
    loadRecs(false);   // flat list (badge only when topics are unavailable)
    loadTopics(false); // badge: open topics
    if (isVisible('insights-recs')) onRecsShown();
    if (isVisible('tab-digest')) loadDigest();
    setInterval(() => {
      if (document.hidden) return;
      loadRecs(false);
      loadTopics(false);
      if (isVisible('insights-recs')) { loadLatest(); loadModelStatus(false); }
    }, BADGE_POLL_MS);
  }

  window.edgeInsights = {
    refresh() { loadRecs(true); loadTopics(true); loadLatest(); if (isVisible('tab-digest')) loadDigest(); },
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
