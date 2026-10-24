/**
 * Support contact and "Report a problem" (docs/SUPPORT_ISSUES_CONTRACT.md).
 *
 *  - The support contact comes from the signed iKorex policy (GET /api/v1/support);
 *    without one, the built-in EdgeHelpContent.SUPPORT (js/help_content.js) is used.
 *    The Help panel (help.js), the paused / standby page (fleet.js) and the report
 *    dialog all show it through EdgeSupport.contactHtml().
 *  - "Report a problem" dialog: what went wrong, where, how urgent, an optional
 *    call-back name / phone, and the numbers-only diagnostics shown before sending.
 *    Reports are kept on this box first and sent to iKorex when it is online.
 *    "My reports" lists this box's reports with iKorex's status and reply.
 *  - Opened by any element with data-support-report (new report) or
 *    data-support-reports (My reports): Help panel, Settings > Help and support,
 *    the paused page.
 *  - Settings card #settings-support "Help and support".
 *
 * Modal: focus stays inside, Esc closes, focus goes back. No inline handlers (CSP);
 * all text is inserted escaped.
 */
(function () {
  'use strict';

  const INFO_URL = '/api/v1/support';
  const REPORTS_URL = '/api/v1/support/reports';
  const DIAG_URL = '/api/v1/support/diagnostics';
  const TEXT_MIN = 10;
  const TEXT_MAX = 2000;
  const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';
  const AREAS = [
    ['cameras', 'Cameras'], ['store_map', 'Store map'], ['alerts', 'Alerts'], ['insights', 'Insights'],
    ['phone_alerts', 'Phone alerts'], ['online_access', 'Online access'], ['other', 'Other'],
  ];
  // Dashboard tab -> the "where" the dialog suggests first.
  const TAB_AREA = { cameras: 'cameras', map: 'store_map', loss: 'alerts', insights: 'insights', today: 'other', settings: 'other' };
  const STATUS = {
    queued: ['Waiting to send', 'badge-neutral'],
    received: ['Received by iKorex', 'badge-info'],
    in_progress: ['Being worked on', 'badge-warning'],
    fixed: ['Fixed', 'badge-success'],
    closed: ['Closed', 'badge-neutral'],
  };

  let info = null;            // GET /api/v1/support (null: not signed in / not reachable)
  let reports = null;         // last GET /api/v1/support/reports
  let wrap = null;            // dialog overlay
  let opener = null;
  let view = 'form';          // form | sent | list
  let draft = null;           // form values kept while the dialog switches views
  let diag = null;
  let lastSent = null;
  let busy = false;

  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v === null || v === undefined ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const ico = (name, size) => {
    try { return window.EdgeIcon ? window.EdgeIcon.svg(name, { size: size || 'md' }) : ''; } catch (_) { return ''; }
  };
  const friendly = (status, body) => (window.EdgeHelp && window.EdgeHelp.friendlyError
    ? window.EdgeHelp.friendlyError(status, body) : 'That did not work. Try again in a moment.');

  async function getJSON(url, opts) {
    if (window.EdgeHelp && window.EdgeHelp.fetchJSON) return window.EdgeHelp.fetchJSON(url, opts);
    try {
      const res = await fetch(url, opts);
      let body = null;
      try { body = await res.json(); } catch (_) { body = null; }
      return res.ok ? { ok: true, status: res.status, data: body } : { ok: false, status: res.status, body, message: friendly(res.status, body) };
    } catch (_) { return { ok: false, status: 0, body: null, message: friendly(0, null) }; }
  }

  // ================================================================ support contact

  function builtIn() {
    const C = window.EdgeHelpContent || {};
    return C.SUPPORT || {};
  }

  /** The contact to show: the signed policy's, else the built-in one. */
  function contact() {
    const s = info && info.support;
    if (s && (s.phone || s.email || s.website || s.hours || s.note)) {
      return Object.assign({ name: 'iKorex support' }, s, { fromPolicy: true });
    }
    return Object.assign({ name: 'iKorex support' }, builtIn());
  }

  function telHref(phone) { return `tel:${String(phone).replace(/[^0-9+]/g, '')}`; }

  /** The contact as a list (class names from help.css). opts.title=false leaves the heading out. */
  function contactHtml(opts) {
    const o = opts || {};
    const s = contact();
    const rows = [];
    if (s.phone) rows.push(`<li><span class="help-support-k">Phone</span><a href="${esc(telHref(s.phone))}">${esc(s.phone)}</a></li>`);
    if (s.email) rows.push(`<li><span class="help-support-k">Email</span><a href="mailto:${esc(s.email)}">${esc(s.email)}</a></li>`);
    if (s.hours) rows.push(`<li><span class="help-support-k">Hours</span><span>${esc(s.hours)}</span></li>`);
    if (s.website && /^https:\/\//i.test(String(s.website))) rows.push(`<li><span class="help-support-k">Web</span><a href="${esc(s.website)}" target="_blank" rel="noopener">${esc(String(s.website).replace(/^https:\/\//i, ''))}</a></li>`);
    const body = rows.length
      ? `<ul class="help-support-list">${rows.join('')}</ul>${s.note ? `<p class="help-muted">${esc(s.note)}</p>` : ''}`
      : '<p class="help-muted">Contact your iKorex installer for the support phone number and email.</p>';
    const head = o.title === false ? '' : `<h3 class="help-h3"${o.titleId ? ` id="${esc(o.titleId)}"` : ''}>${ico('users', 'sm')}Contact ${esc(s.name || 'iKorex support')}</h3>`;
    return head + body;
  }

  async function loadInfo() {
    if (window.edgeAuth && window.edgeAuth.isGateOpen && window.edgeAuth.isGateOpen()) return info;
    const r = await getJSON(INFO_URL, { cache: 'no-store' });
    if (r.ok && r.data) info = r.data;
    try { window.dispatchEvent(new CustomEvent('edge:support', { detail: { support: info && info.support } })); } catch (_) {}
    renderCard();
    return info;
  }

  // ============================================================== dialog plumbing

  function currentTab() {
    if (document.documentElement.classList.contains('fleet-is-paused')) return 'paused';
    const b = document.querySelector('#mainNav [data-tab].active, [data-tab].active');
    return b ? b.getAttribute('data-tab') : 'other';
  }

  function deliveryLine(delivery) {
    switch (delivery) {
      case 'online': return '';
      case 'not_configured': return "This box isn't connected to iKorex yet, so the report stays on this box. Please also call your installer.";
      case 'not_supported': return 'Will send when iKorex can accept reports from this box. It is kept here until then.';
      default: return 'Will send when the box is online. It is kept on this box until then.';
    }
  }

  function ensureDialog() {
    if (wrap) return wrap;
    wrap = document.createElement('div');
    wrap.className = 'support-overlay';
    wrap.id = 'supportDialogWrap';
    wrap.hidden = true;
    wrap.innerHTML = `
      <div class="support-dialog" role="dialog" aria-modal="true" aria-labelledby="supportDialogTitle">
        <div class="support-head">
          <h2 class="support-title" id="supportDialogTitle">${ico('circle-alert')}<span id="supportDialogTitleText">Report a problem</span></h2>
          <button type="button" class="btn btn-ghost btn-icon btn-sm" data-sup="close" aria-label="Close">${ico('x')}</button>
        </div>
        <div class="support-switch" role="group" aria-label="Report a problem">
          <button type="button" class="btn btn-sm" data-sup="form" aria-pressed="true">New report</button>
          <button type="button" class="btn btn-sm" data-sup="list" aria-pressed="false">My reports<span class="support-count" id="supportListCount"></span></button>
        </div>
        <div class="support-body" id="supportBody"></div>
      </div>`;
    document.body.appendChild(wrap);
    wrap.addEventListener('mousedown', (e) => { if (e.target === wrap) close(); });
    wrap.addEventListener('click', onClick);
    wrap.addEventListener('keydown', onKey);
    wrap.addEventListener('input', onInput);
    wrap.addEventListener('submit', (e) => { e.preventDefault(); submit(); });
    return wrap;
  }

  function onKey(e) {
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); return; }
    if (e.key !== 'Tab') return;
    const box = wrap.querySelector('.support-dialog');
    const items = [...box.querySelectorAll(FOCUSABLE)].filter((n) => n.offsetParent !== null || n === document.activeElement);
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }

  function onClick(e) {
    const t = e.target.closest('[data-sup]');
    if (!t) return;
    const what = t.getAttribute('data-sup');
    if (what === 'close' || what === 'cancel') { close(); return; }
    if (what === 'form') { saveDraft(); show('form'); return; }
    if (what === 'list') { saveDraft(); show('list'); return; }
    if (what === 'new') { draft = null; show('form'); return; }
    if (what === 'refresh') { show('list'); }
  }

  function onInput(e) {
    if (e.target && e.target.id === 'supText') {
      const n = e.target.value.trim().length;
      const c = $('supCount');
      if (c) c.textContent = `${e.target.value.length} / ${TEXT_MAX}`;
      if (n >= TEXT_MIN) setErr('supText', '');
    }
  }

  function setErr(id, msg) {
    const input = $(id);
    const err = $(`${id}Err`);
    if (input) input.setAttribute('aria-invalid', msg ? 'true' : 'false');
    if (err) { err.textContent = msg; err.hidden = !msg; }
  }

  function setPressed() {
    if (!wrap) return;
    wrap.querySelectorAll('.support-switch [data-sup]').forEach((b) => {
      const on = b.getAttribute('data-sup') === (view === 'sent' ? 'list' : view);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      b.classList.toggle('btn-primary', on);
    });
    const t = $('supportDialogTitleText');
    if (t) t.textContent = view === 'list' ? 'My reports' : 'Report a problem';
  }

  function updateCount() {
    const el = $('supportListCount');
    if (!el) return;
    const n = reports ? reports.length : 0;
    el.textContent = n ? ` (${n})` : '';
  }

  function open(which, from) {
    ensureDialog();
    if (wrap.hidden) opener = from || document.activeElement;
    if (window.EdgeHelp && typeof window.EdgeHelp.close === 'function') {
      try { window.EdgeHelp.close(); } catch (_) {}
    }
    wrap.hidden = false;
    document.documentElement.classList.add('support-open');
    show(which === 'list' ? 'list' : 'form');
    loadInfo().then(() => { if (!wrap.hidden && view === 'form') renderContactFoot(); });
  }

  function close() {
    if (!wrap || wrap.hidden) return;
    saveDraft();
    wrap.hidden = true;
    document.documentElement.classList.remove('support-open');
    const back = opener;
    opener = null;
    const target = back && back.isConnected && back.offsetParent !== null ? back : $('navHelp');
    if (target && typeof target.focus === 'function') { try { target.focus({ preventScroll: true }); } catch (_) {} }
    renderCard();
  }

  function show(which) {
    view = which;
    setPressed();
    if (which === 'list') renderList();
    else if (which === 'sent') renderSent();
    else renderForm();
    // Focus always inside the dialog (the list view renders after its fetch).
    if (!wrap.contains(document.activeElement)) {
      const b = wrap.querySelector('.support-switch [aria-pressed="true"]');
      if (b) { try { b.focus({ preventScroll: true }); } catch (_) {} }
    }
  }

  // ===================================================================== form

  function saveDraft() {
    const t = $('supText');
    if (!t || view !== 'form') return;
    const urg = wrap.querySelector('input[name="supUrg"]:checked');
    draft = {
      text: t.value, area: ($('supArea') || {}).value || 'other', urgency: urg ? urg.value : 'normal',
      name: ($('supName') || {}).value || '', phone: ($('supPhone') || {}).value || '',
    };
  }

  function fmtDuration(sec) {
    const s = Math.max(0, Math.round(Number(sec) || 0));
    if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min`;
    const h = Math.floor(s / 3600);
    if (h < 48) return `${h} h`;
    return `${Math.floor(h / 24)} days`;
  }

  const WIDTHS = { lt600: 'phone', '600-1023': 'tablet', '1024-1439': 'laptop', ge1440: 'large screen' };

  function diagHtml(d) {
    if (!d) return '<p class="help-muted">Checking what this box will attach…</p>';
    const rows = [];
    rows.push(['Box software', d.software_version || '–']);
    rows.push(['Box running for', d.uptime_s != null ? fmtDuration(d.uptime_s) : '–']);
    rows.push(['Cameras online', d.cameras_total != null ? `${d.cameras_online != null ? d.cameras_online : '?'} of ${d.cameras_total}` : '–']);
    rows.push(['Disk used', d.disk_pct != null ? `${d.disk_pct} %` : '–']);
    const n = (d.recent_issues || []).length;
    rows.push(['Recent error codes', n ? `${n} (codes and counts only)` : 'none']);
    rows.push(['Screen', `${d.tab || 'other'}${d.width_bucket ? `, ${WIDTHS[d.width_bucket] || d.width_bucket} size` : ''}`]);
    return `<dl class="support-diag-list">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>`;
  }

  async function loadDiag() {
    const q = `?tab=${encodeURIComponent(currentTab())}&width=${Math.round(window.innerWidth || 0)}`;
    const r = await getJSON(DIAG_URL + q, { cache: 'no-store' });
    diag = r.ok ? r.data : null;
    const el = $('supDiag');
    if (el) el.innerHTML = r.ok ? diagHtml(diag) : '<p class="help-muted">The numbers are added when the report is saved.</p>';
  }

  function renderContactFoot() {
    const el = $('supContactFoot');
    if (el) el.innerHTML = contactHtml({ title: false });
  }

  function renderForm() {
    const body = $('supportBody');
    const d = draft || { text: '', area: TAB_AREA[currentTab()] || 'other', urgency: 'normal', name: '', phone: '' };
    const offline = info ? deliveryLine(info.delivery) : '';
    body.innerHTML = `
      <form id="supportForm" class="support-form" novalidate>
        <p class="support-notice">${ico('shield-check', 'sm')}<span>Don't include customer details. No pictures or video are sent.</span></p>
        <div class="field">
          <label class="field-label" for="supText">What went wrong?</label>
          <textarea id="supText" name="supText" class="input support-text" rows="5" maxlength="${TEXT_MAX}" required
            aria-describedby="supTextHint supTextErr" aria-invalid="false">${esc(d.text)}</textarea>
          <div class="support-hint" id="supTextHint"><span>What you saw, what you pressed, and when.</span><span id="supCount">${esc(d.text.length)} / ${TEXT_MAX}</span></div>
          <div class="support-err" id="supTextErr" role="alert" hidden></div>
        </div>
        <div class="support-row">
          <div class="field">
            <label class="field-label" for="supArea">Where in the dashboard?</label>
            <select id="supArea" name="supArea" class="select">
              ${AREAS.map(([v, l]) => `<option value="${v}"${v === d.area ? ' selected' : ''}>${esc(l)}</option>`).join('')}
            </select>
          </div>
          <fieldset class="support-urgency">
            <legend class="field-label">How urgent?</legend>
            <label class="support-radio"><input type="radio" name="supUrg" value="normal"${d.urgency !== 'urgent' ? ' checked' : ''}><span>Normal</span></label>
            <label class="support-radio"><input type="radio" name="supUrg" value="urgent"${d.urgency === 'urgent' ? ' checked' : ''}><span>Urgent: the store can't use the system</span></label>
          </fieldset>
        </div>
        <fieldset class="support-callback">
          <legend class="field-label">Call back (optional)</legend>
          <p class="support-hint" id="supCallbackNote">Only kept if you fill these in. iKorex uses them to call you about this report.</p>
          <div class="support-row">
            <div class="field">
              <label class="field-label" for="supName">Your name</label>
              <input id="supName" name="supName" class="input" type="text" maxlength="80" autocomplete="name" value="${esc(d.name)}" aria-describedby="supCallbackNote">
            </div>
            <div class="field">
              <label class="field-label" for="supPhone">Phone</label>
              <input id="supPhone" name="supPhone" class="input" type="tel" maxlength="32" autocomplete="tel" inputmode="tel" value="${esc(d.phone)}"
                aria-describedby="supCallbackNote supPhoneErr" aria-invalid="false">
              <div class="support-err" id="supPhoneErr" role="alert" hidden></div>
            </div>
          </div>
        </fieldset>
        <section class="support-diag" aria-labelledby="supDiagTitle">
          <h3 class="support-h3" id="supDiagTitle">${ico('info', 'sm')}Also sent with your report</h3>
          <div id="supDiag">${diagHtml(null)}</div>
          <p class="help-muted">Numbers and codes only: no pictures, video, camera names or customer details.</p>
        </section>
        <div class="support-status${offline ? ' is-offline' : ''}" id="supStatus" role="status" aria-live="polite">${esc(offline)}</div>
        <div class="support-foot">
          <button type="button" class="btn btn-secondary" data-sup="cancel">Cancel</button>
          <button type="submit" class="btn btn-primary" id="supSend">Send report${ico('arrow-right', 'sm')}</button>
        </div>
        <section class="support-contact" aria-label="Support contact">
          <p class="help-muted">Urgent? Call as well:</p>
          <div id="supContactFoot">${contactHtml({ title: false })}</div>
        </section>
      </form>`;
    loadDiag();
    const t = $('supText');
    if (t) { try { t.focus({ preventScroll: true }); } catch (_) { t.focus(); } }
  }

  async function submit() {
    if (busy) return;
    const t = $('supText');
    const text = t ? t.value.trim() : '';
    let bad = false;
    if (text.length < TEXT_MIN) { setErr('supText', `Please describe what went wrong (at least ${TEXT_MIN} characters).`); bad = true; }
    else if (text.length > TEXT_MAX) { setErr('supText', `Please keep it under ${TEXT_MAX} characters.`); bad = true; }
    else setErr('supText', '');
    const phone = (($('supPhone') || {}).value || '').trim();
    if (phone && ((phone.match(/\d/g) || []).length < 6 || /[^0-9+ ()\-.]/.test(phone))) {
      setErr('supPhone', "That phone number doesn't look right. Use digits, spaces and +.");
      bad = true;
    } else setErr('supPhone', '');
    if (bad) { const first = wrap.querySelector('[aria-invalid="true"]'); if (first) first.focus(); return; }
    saveDraft();
    const name = draft.name.trim();
    const payload = {
      text, area: draft.area, urgency: draft.urgency, tab: currentTab(), width: Math.round(window.innerWidth || 0),
      contact: name || phone ? { name, phone } : null,
    };
    busy = true;
    const btn = $('supSend');
    if (btn) btn.disabled = true;
    const st = $('supStatus');
    if (st) { st.classList.remove('is-error'); st.textContent = 'Saving your report…'; }
    const r = await getJSON(REPORTS_URL, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    busy = false;
    if (btn) btn.disabled = false;
    if (!r.ok) {
      if (st) { st.classList.add('is-error'); st.textContent = r.message || friendly(r.status, r.body); }
      return;
    }
    lastSent = r.data;
    draft = null;
    reports = null;
    if (info && r.data && r.data.delivery) info.delivery = r.data.delivery;
    show('sent');
  }

  function renderSent() {
    const body = $('supportBody');
    const delivery = lastSent && lastSent.delivery;
    const line = delivery === 'online'
      ? 'It is on its way to iKorex. You can follow it under My reports.'
      : deliveryLine(delivery);
    body.innerHTML = `
      <div class="support-sent">
        <div class="support-sent-icon" aria-hidden="true">${ico('circle-check', 'lg')}</div>
        <h3 class="support-sent-title" id="supSentTitle" tabindex="-1">Thanks, your report is saved</h3>
        <p class="support-sent-text">${esc(line)}</p>
        ${lastSent && lastSent.report && lastSent.report.urgency === 'urgent'
          ? '<p class="support-sent-text">Marked urgent. If the store cannot work at all, please also call:</p>' : ''}
        <div class="support-contact">${contactHtml({ title: false })}</div>
        <div class="support-foot">
          <button type="button" class="btn btn-secondary" data-sup="list">See my reports</button>
          <button type="button" class="btn btn-primary" data-sup="close">Done</button>
        </div>
      </div>`;
    const h = $('supSentTitle');
    if (h) h.focus({ preventScroll: true });
  }

  // ===================================================================== list

  function fmtWhen(iso) {
    if (!iso) return '';
    const t = new Date(iso);
    if (Number.isNaN(t.getTime())) return String(iso);
    return t.toLocaleString('en-AU', { day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit' });
  }

  function statusBadge(r, delivery) {
    let [label, cls] = STATUS[r.status] || STATUS.queued;
    if (r.status === 'queued' && delivery && delivery !== 'online') label = 'Will send when the box is online';
    return `<span class="badge ${cls}">${esc(label)}</span>`;
  }

  async function renderList() {
    const body = $('supportBody');
    if (!reports) body.innerHTML = '<p class="help-muted" role="status">Loading your reports…</p>';
    const r = await getJSON(REPORTS_URL, { cache: 'no-store' });
    if (view !== 'list') return;
    if (!r.ok) {
      body.innerHTML = `<div class="support-status is-error" role="alert">${esc(r.message)}</div>
        <div class="support-foot"><button type="button" class="btn btn-secondary" data-sup="refresh">${ico('refresh-cw', 'sm')}Try again</button></div>`;
      return;
    }
    reports = r.data.reports || [];
    const delivery = r.data.delivery;
    if (info) info.delivery = delivery;
    updateCount();
    if (!reports.length) {
      body.innerHTML = `<div class="empty-state support-empty">${ico('circle-check', 'lg')}
          <div class="empty-state-title">No reports yet</div>
          <div class="empty-state-text">Problems reported from this box show here with iKorex's answer.</div></div>
        <div class="support-foot"><button type="button" class="btn btn-primary" data-sup="new">${ico('plus', 'sm')}Report a problem</button></div>`;
      return;
    }
    const waiting = reports.filter((x) => x.status === 'queued').length;
    const note = waiting && delivery !== 'online' ? `<p class="support-status is-offline">${esc(deliveryLine(delivery))}</p>` : '';
    body.innerHTML = `${note}
      <ul class="support-list">${reports.map((x) => `
        <li class="support-item${x.urgency === 'urgent' ? ' is-urgent' : ''}">
          <div class="support-item-head">
            ${statusBadge(x, delivery)}
            ${x.urgency === 'urgent' ? '<span class="badge badge-danger">Urgent</span>' : ''}
            <span class="support-item-meta">${esc(x.area_label || 'Other')} · ${esc(fmtWhen(x.created_at))}</span>
          </div>
          <p class="support-item-text">${esc(x.text)}</p>
          ${x.reply ? `<div class="support-reply"><div class="support-reply-k">${ico('user', 'sm')}Reply from iKorex${x.updated_at ? ` · ${esc(fmtWhen(x.updated_at))}` : ''}</div><p>${esc(x.reply)}</p></div>` : ''}
        </li>`).join('')}
      </ul>
      <div class="support-foot">
        <button type="button" class="btn btn-secondary" data-sup="refresh">${ico('refresh-cw', 'sm')}Refresh</button>
        <button type="button" class="btn btn-primary" data-sup="new">${ico('plus', 'sm')}New report</button>
      </div>`;
  }

  // ============================================================== Settings card

  function renderCard() {
    const host = $('settings-support');
    if (!host) return;
    const waiting = info && info.queued_reports ? info.queued_reports : 0;
    const offline = waiting && info && info.delivery !== 'online' ? deliveryLine(info.delivery) : '';
    host.innerHTML = `
      <div class="card-title"><span>Help and support</span></div>
      <p class="fleet-muted support-card-lede">Something not working? Tell iKorex here. Your report is kept on this box and sent to iKorex;
        their answer shows under My reports.</p>
      <div class="fleet-actions support-card-actions">
        <button type="button" class="btn btn-primary btn-sm" data-support-report>${ico('circle-alert', 'sm')}Report a problem</button>
        <button type="button" class="btn btn-secondary btn-sm" data-support-reports>My reports${waiting ? ` (${esc(waiting)} waiting)` : ''}</button>
      </div>
      ${offline ? `<p class="support-status is-offline">${esc(offline)}</p>` : ''}
      <div class="help-support support-card-contact">${contactHtml()}</div>`;
  }

  // ===================================================================== wiring

  function init() {
    document.addEventListener('click', (e) => {
      const rep = e.target.closest('[data-support-report]');
      if (rep) { e.preventDefault(); open('form', rep); return; }
      const list = e.target.closest('[data-support-reports]');
      if (list) { e.preventDefault(); open('list', list); }
    });
    window.addEventListener('edge:tab', (ev) => { if (ev.detail && ev.detail.tab === 'settings') loadInfo(); });
    window.addEventListener('edge:account', () => { loadInfo(); });
    renderCard();
    if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') window.edgeAuth.onReady(() => loadInfo());
    else loadInfo();
  }

  window.EdgeSupport = Object.freeze({
    contact,
    contactHtml,
    openReport: (from) => open('form', from),
    openReports: (from) => open('list', from),
    close,
    reload: loadInfo,
    info: () => info,
  });

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
