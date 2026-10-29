/**
 * Things the box sets up by itself (routes/automation.py, services/automation.py).
 *
 *   #autoSuggestions        Today: suggestions an administrator confirms or dismisses
 *                           (guard after hours, fewer wrong alerts, link cameras to areas).
 *                           Operators see them read-only. Every apply can be undone.
 *   #settings-store-hours   Settings: the learnt store hours per day (editable by
 *                           administrators), the time zone, the backup time and storage sizes.
 *   #autoTuneStatus         Settings > System status: analysis speed chosen for this box and
 *                           cameras restarted today.
 *
 * window.edgeAutomation.storeHours() -> null until every day is known, else
 *   {learnt: true, label: "Mon–Fri 08:00–21:00, Sat–Sun 09:00–18:00",
 *    rows: [{days: ["mon", ...], from: "08:00", to: "21:00"}, ...]}
 * (rows in the shape of restricted-area arming.schedule). The window event
 * 'edge:store-hours' (detail = that value) fires when the hours load or change.
 *
 * Polls through EdgePoll at 2 and 5 minutes, paused while the page is hidden.
 */
(function () {
  'use strict';

  const SUGGEST_MS = 120000;
  const STATUS_MS = 300000;
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v === null || v === undefined ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const ico = (name) => {
    try { return window.EdgeIcon ? window.EdgeIcon.svg(name, { size: 'sm' }) : ''; } catch (_) { return ''; }
  };
  const isAdmin = () => !!(window.EdgeAuth && window.EdgeAuth.isAdmin);
  const toast = (msg, kind) => { if (typeof window.showToast === 'function') window.showToast(msg, kind || 'info'); };
  const DAY = { mon: 'Monday', tue: 'Tuesday', wed: 'Wednesday', thu: 'Thursday', fri: 'Friday', sat: 'Saturday', sun: 'Sunday' };

  let hours = null;          // GET /api/v1/store-hours
  let status = null;         // GET /api/v1/automation/status
  let suggestions = null;    // {suggestions, can_apply}
  let lastViewKey = 'unset';
  let editing = false;
  let undoNote = null;       // {apply_id, title}

  // ---------------------------------------------------------------- requests
  async function send(method, url, body) {
    const res = await fetch(url, {
      method, headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    if (!res.ok) {
      const msg = (data && (typeof data.detail === 'string' ? data.detail : (data.detail && data.detail.message))) || `Request failed (${res.status})`;
      throw new Error(msg);
    }
    return data;
  }
  async function getJSON(url) {
    const res = await fetch(url);
    if (!res.ok) return null;
    try { return await res.json(); } catch (_) { return null; }
  }

  // ---------------------------------------------------------------- store hours (public API)
  function view() { return hours && hours.view ? hours.view : null; }
  function announce() {
    const v = view();
    const key = JSON.stringify(v);
    if (key === lastViewKey) return;
    lastViewKey = key;
    try { window.dispatchEvent(new CustomEvent('edge:store-hours', { detail: v })); } catch (_) { /* old browser */ }
  }

  async function loadHours() {
    const [h, s] = await Promise.all([getJSON('/api/v1/store-hours'), getJSON('/api/v1/automation/status')]);
    if (h) hours = h;
    if (s) status = s;
    announce();
    if (!editing) renderHours();
    renderTune();
  }

  async function loadSuggestions() {
    const host = $('autoSuggestions');
    if (!host) return;
    const s = await getJSON('/api/v1/automation/suggestions');
    if (s) suggestions = s;
    renderSuggestions();
  }

  // ---------------------------------------------------------------- suggestions (Today)
  function renderSuggestions() {
    const host = $('autoSuggestions');
    if (!host) return;
    const list = (suggestions && suggestions.suggestions) || [];
    const canApply = !!(suggestions && suggestions.can_apply) && isAdmin();
    let html = '';
    if (undoNote) {
      html += `<div class="auto-done" role="status">${ico('check')}<span>Done: ${esc(undoNote.title)}.</span>`
        + `<button type="button" class="btn btn-secondary btn-sm" data-auto-undo="${esc(undoNote.apply_id)}">Undo</button>`
        + '<button type="button" class="btn btn-ghost btn-sm" data-auto-close-done>OK</button></div>';
    }
    if (list.length) {
      html += `<section class="glass-card auto-card" aria-label="Suggestions"><div class="card-title"><span data-help="auto-suggestions">${ico('circle-help')}Suggestions</span>`
        + `<span class="badge">${list.length}</span></div><div class="auto-list">`;
      html += list.map((s) => item(s, canApply)).join('');
      html += '</div>';
      if (!canApply) html += '<p class="auto-note">An owner or administrator can apply these.</p>';
      html += '</section>';
    }
    host.innerHTML = html;
    host.hidden = !html;
  }

  function item(s, canApply) {
    let extra = '';
    if (s.kind === 'link_zone') {
      extra = '<ul class="auto-links">' + (s.links || []).map((x) => {
        const id = `auto-link-${esc(x.camera_id)}`;
        return `<li class="${x.ok ? '' : 'auto-link-check'}"><label for="${id}">`
          + `<input type="checkbox" id="${id}" data-auto-link="${esc(x.camera_id)}" ${x.ok ? 'checked' : 'disabled'} ${canApply ? '' : 'disabled'}>`
          + `<span><b>${esc(x.name)}</b> → ${esc(x.zone_name)} <span class="auto-muted">(${Math.round((x.share || 0) * 100)} % of its view)</span>`
          + (x.ok ? '' : `<span class="auto-muted"> · check first: ${esc(x.check)}</span>`) + '</span></label></li>';
      }).join('') + '</ul>';
    } else if (s.cameras && s.cameras.length) {
      extra = `<p class="auto-muted">Cameras: ${s.cameras.map((c) => esc(c.name)).join(', ')}</p>`;
    }
    const anyOk = s.kind !== 'link_zone' || (s.links || []).some((x) => x.ok);
    const actions = canApply
      ? `<div class="auto-actions"><button type="button" class="btn btn-primary btn-sm" data-auto-apply="${esc(s.id)}" ${anyOk ? '' : 'disabled'}>`
        + `${s.kind === 'link_zone' ? 'Link the ticked cameras' : 'Apply'}</button>`
        + `<button type="button" class="btn btn-ghost btn-sm" data-auto-dismiss="${esc(s.id)}">Not now</button></div>`
      : '';
    return `<article class="auto-item" data-auto-id="${esc(s.id)}"><h3 class="auto-title">${esc(s.title)}</h3>`
      + `<p class="auto-detail">${esc(s.detail)}</p>${extra}${actions}</article>`;
  }

  async function onSuggestionClick(e) {
    const t = e.target.closest('button');
    if (!t) return;
    if (t.hasAttribute('data-auto-close-done')) { undoNote = null; renderSuggestions(); return; }
    const undoId = t.getAttribute('data-auto-undo');
    if (undoId) {
      t.disabled = true;
      try {
        await send('POST', `/api/v1/automation/applied/${encodeURIComponent(undoId)}/undo`);
        toast('Undone. The previous setting is back.', 'success');
        undoNote = null;
      } catch (err) { toast(err.message, 'error'); t.disabled = false; }
      await loadSuggestions();
      return;
    }
    const sid = t.getAttribute('data-auto-apply') || t.getAttribute('data-auto-dismiss');
    if (!sid) return;
    t.disabled = true;
    try {
      if (t.hasAttribute('data-auto-apply')) {
        const card = t.closest('.auto-item');
        const boxes = card ? Array.from(card.querySelectorAll('input[data-auto-link]')) : [];
        const body = boxes.length ? { camera_ids: boxes.filter((b) => b.checked && !b.disabled).map((b) => b.getAttribute('data-auto-link')) } : undefined;
        if (body && !body.camera_ids.length) { toast('Tick at least one camera.', 'error'); t.disabled = false; return; }
        const rec = await send('POST', `/api/v1/automation/suggestions/${encodeURIComponent(sid)}/apply`, body);
        undoNote = { apply_id: rec.apply_id, title: rec.title };
        try { window.dispatchEvent(new CustomEvent('edge:automation-applied', { detail: rec })); } catch (_) { /* old browser */ }
      } else {
        await send('POST', `/api/v1/automation/suggestions/${encodeURIComponent(sid)}/dismiss`);
        toast('Hidden for 30 days.', 'info');
      }
    } catch (err) { toast(err.message, 'error'); t.disabled = false; return; }
    await loadSuggestions();
  }

  // ---------------------------------------------------------------- store hours (Settings)
  function sourceText(d) {
    if (d.source === 'manual') return 'set by you';
    if (d.source === 'learned') return d.estimated ? 'estimated' : 'learnt';
    return 'not known yet';
  }
  function dayText(spec) {
    if (!spec) return '—';
    if (spec.closed) return 'Closed';
    return `${spec.open}–${spec.close}`;
  }
  function tzText(tz) {
    if (!tz) return '';
    if (tz.source === 'setting') return `${esc(tz.name)} (set in Store settings)`;
    if (tz.source === 'host') return `Automatic: ${esc(tz.name)}`;
    return '<span class="auto-warn">Unknown. Set it in Store settings.</span>';
  }

  function renderHours() {
    const host = $('settings-store-hours');
    if (!host) return;
    if (!hours) { host.innerHTML = '<div class="card-title"><span data-help="store-hours">Store hours</span></div><div class="fp-empty">Loading…</div>'; return; }
    const admin = isAdmin() && hours.can_edit;
    const v = hours.view;
    const lead = v ? `<p class="auto-lead">${ico('clock')}<b>${esc(v.label)}</b></p>`
      : `<p class="auto-lead">${ico('clock')}Learning your opening hours from shoppers and till sales: `
        + `<b>${Number(hours.days_measured || 0)} of ${Number(hours.needed || 7)} days</b> so far. You can also type them in.</p>`;
    const rows = (hours.days || []).map((d) => {
      if (editing && admin) {
        const spec = d.manual || d.hours || {};
        const closed = !!spec.closed;
        return `<tr data-day="${d.day}"><th scope="row">${esc(d.label)}</th>`
          + `<td class="auto-edit"><input type="time" aria-label="${esc(d.label)} opens" data-open value="${esc(closed ? '' : (spec.open || ''))}" ${closed ? 'disabled' : ''}>`
          + `<span aria-hidden="true">–</span><input type="time" aria-label="${esc(d.label)} closes" data-close value="${esc(closed ? '' : (spec.close || ''))}" ${closed ? 'disabled' : ''}>`
          + `<label class="auto-closed"><input type="checkbox" data-closed ${closed ? 'checked' : ''}> Closed</label>`
          + (d.manual ? `<label class="auto-closed"><input type="checkbox" data-learnt> Use learnt</label>` : '') + '</td></tr>';
      }
      return `<tr><th scope="row">${esc(d.label)}</th><td>${esc(dayText(d.hours))}</td><td class="auto-muted">${esc(sourceText(d))}</td></tr>`;
    }).join('');
    const st = status || {};
    const store = st.storage || {};
    const bk = hours.backup || {};
    const facts = [
      `<li><span>Time zone</span><span>${tzText(hours.timezone)}</span></li>`,
      `<li><span>Nightly backup</span><span>${esc(bk.time || '03:00')} <span class="auto-muted">(${esc(bk.why || '')})</span></span></li>`,
      store.evidence_gb ? `<li><span>Saved pictures and clips</span><span>up to ${esc(store.evidence_gb)} GB <span class="auto-muted">(${store.evidence_source === 'setting' ? 'set by you' : 'automatic, sized to the disk'})</span></span></li>` : '',
      store.backups_budget_gb ? `<li><span>Backups</span><span>up to ${esc(store.backups_budget_gb)} GB <span class="auto-muted">(${store.backups_source === 'setting' ? 'set by you' : 'automatic, sized to the disk'})</span></span></li>` : '',
    ].join('');
    const buttons = !admin ? '<p class="auto-note">An owner or administrator can change the store hours.</p>'
      : editing
        ? '<div class="auto-actions"><button type="button" class="btn btn-primary btn-sm" data-hours-save>Save hours</button>'
          + '<button type="button" class="btn btn-ghost btn-sm" data-hours-cancel>Cancel</button></div>'
        : '<div class="auto-actions"><button type="button" class="btn btn-secondary btn-sm" data-hours-edit>Change hours</button>'
          + '<button type="button" class="btn btn-ghost btn-sm" data-hours-relearn>Learn again now</button></div>';
    host.innerHTML = `<div class="card-title"><span data-help="store-hours">Store hours</span></div>${lead}`
      + `<p class="auto-muted">Night watch and restricted areas set to "outside store hours" follow these times. They change by themselves when the hours change.</p>`
      + `<div class="auto-table-wrap"><table class="auto-hours"><tbody>${rows}</tbody></table></div>`
      + `<ul class="auto-facts">${facts}</ul>${buttons}`;
  }

  async function onHoursClick(e) {
    const t = e.target;
    if (t.matches('input[data-closed]')) {
      const tr = t.closest('tr');
      tr.querySelectorAll('input[type="time"]').forEach((i) => { i.disabled = t.checked; });
      return;
    }
    const b = t.closest('button');
    if (!b) return;
    if (b.hasAttribute('data-hours-edit')) { editing = true; renderHours(); return; }
    if (b.hasAttribute('data-hours-cancel')) { editing = false; renderHours(); return; }
    if (b.hasAttribute('data-hours-relearn')) {
      b.disabled = true;
      try { hours = Object.assign(await send('POST', '/api/v1/store-hours/relearn'), {}); toast('Store hours learnt again.', 'success'); }
      catch (err) { toast(err.message, 'error'); }
      await loadHours();
      return;
    }
    if (b.hasAttribute('data-hours-save')) {
      const days = {};
      let bad = null;
      $('settings-store-hours').querySelectorAll('tr[data-day]').forEach((tr) => {
        const day = tr.getAttribute('data-day');
        const learnt = tr.querySelector('input[data-learnt]');
        if (learnt && learnt.checked) { days[day] = null; return; }
        if (tr.querySelector('input[data-closed]').checked) { days[day] = { closed: true }; return; }
        const o = tr.querySelector('input[data-open]').value;
        const c = tr.querySelector('input[data-close]').value;
        if (!o && !c) return;                                  // left empty: unchanged
        if (!o || !c) { bad = `${DAY[day]}: give both times, or tick Closed.`; return; }
        days[day] = { open: o, close: c };
      });
      if (bad) { toast(bad, 'error'); return; }
      b.disabled = true;
      try {
        hours = await send('PUT', '/api/v1/store-hours', { days });
        editing = false;
        toast('Store hours saved.', 'success');
      } catch (err) { toast(err.message, 'error'); b.disabled = false; return; }
      announce();
      renderHours();
      loadSuggestions();
    }
  }

  // ---------------------------------------------------------------- analysis speed and restarts (System status)
  function renderTune() {
    const host = $('autoTuneStatus');
    if (!host || !status) return;
    const a = status.analysis || {};
    const cur = a.current || {};
    const src = a.source || {};
    const auto = Object.values(src).some((x) => x === 'automatic');
    const own = Object.keys(src).filter((k) => src[k] === 'setting');
    let line;
    if (a.chosen && Object.keys(a.chosen).length) {
      line = `Analysis speed: <b>${auto ? 'automatic' : 'set by you'}</b>. Up to ${esc(cur.ANALYTICS_MAX_DETECT_FPS)} checks a second on a busy camera, `
        + `${esc(cur.DECODE_MAX_FPS)} pictures a second read from each camera`
        + (a.cameras ? `, chosen for ${esc(a.cameras)} camera${a.cameras === 1 ? '' : 's'}` : '') + '.';
    } else {
      line = `Analysis speed: <b>automatic</b>. ${esc(a.note || 'Chosen once the AI accelerator has been measured.')}`;
    }
    if (own.length) line += ' <span class="auto-muted">Some speed settings were set by hand and are kept.</span>';
    const restarts = (status.restarts || []).filter((r) => r.today > 0);
    const rline = restarts.length
      ? `Restarted by itself today: ${restarts.map((r) => `${esc(r.name)} (${r.today}×)`).join(', ')}.`
      : 'No camera needed a restart today.';
    const btn = isAdmin() && status.can_edit
      ? '<button type="button" class="btn btn-ghost btn-sm" data-tune-now>Check speed again</button>' : '';
    host.innerHTML = `<div class="auto-tune"><p>${ico('activity')}${line}</p><p>${ico('refresh-cw')}${rline}</p>${btn}</div>`;
  }

  async function onTuneClick(e) {
    const b = e.target.closest('button[data-tune-now]');
    if (!b) return;
    b.disabled = true;
    try { await send('POST', '/api/v1/automation/tune'); toast('Analysis speed checked.', 'success'); }
    catch (err) { toast(err.message, 'error'); }
    await loadHours();
  }

  // ---------------------------------------------------------------- start
  let started = false;
  function init() {
    if (started) return;
    started = true;
    const sg = $('autoSuggestions');
    if (sg) sg.addEventListener('click', onSuggestionClick);
    const sh = $('settings-store-hours');
    if (sh) sh.addEventListener('click', onHoursClick);
    const ts = $('autoTuneStatus');
    if (ts) ts.addEventListener('click', onTuneClick);
    window.addEventListener('edge:account', () => { renderSuggestions(); renderHours(); renderTune(); });
    window.addEventListener('edge:signed-out', () => {
      hours = null; status = null; suggestions = null; undoNote = null;
      const host = $('autoSuggestions');
      if (host) { host.innerHTML = ''; host.hidden = true; }
    });
    renderHours();
    const P = window.EdgePoll;
    if (P && typeof P.every === 'function') {
      P.every(STATUS_MS, loadHours, { immediate: true });
      P.every(SUGGEST_MS, loadSuggestions, { immediate: true });
    } else {
      loadHours(); loadSuggestions();
      setInterval(() => { if (!document.hidden) loadHours(); }, STATUS_MS);
      setInterval(() => { if (!document.hidden) loadSuggestions(); }, SUGGEST_MS);
    }
  }

  window.edgeAutomation = Object.freeze({
    storeHours: () => view(),
    refresh: () => Promise.all([loadHours(), loadSuggestions()]),
    status: () => status,
  });

  if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') window.edgeAuth.onReady(init);
  else if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
