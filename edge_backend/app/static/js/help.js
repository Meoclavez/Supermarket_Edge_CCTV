/**
 * In-app help for the store dashboard: "?" popovers, the Help panel, and
 * plain-language error messages. Words live in js/help_content.js.
 *
 *  - "?" buttons: any element with data-help="<topic>" gets one at its end,
 *    data-help-after="<topic>" gets one right after it, and the cards that other
 *    scripts build at run time get theirs from EdgeHelpContent.anchors (a
 *    MutationObserver puts them back when a card is re-rendered).
 *    The popover is a non-modal dialog: click / Enter / Space toggles it, Esc
 *    closes it and returns focus to the "?" button.
 *  - Help panel (header "Help" button, #navHelp): searchable how-to articles,
 *    "Do it" buttons that open the right screen, the iKorex support contact (from
 *    the signed policy via js/support.js, else help_content.js) and "Report a problem".
 *    Modal side panel: focus stays inside, Esc closes, focus goes back.
 *  - EdgeHelp.friendlyError(status, body) / EdgeHelp.fetchJSON(url): turn API
 *    refusals (401, 403 feature_disabled, 409 camera_limit, 423 service_paused
 *    / service_standby, offline) into one plain sentence.
 *
 * Shows only words from help_content.js and the dashboard's own state; it
 * sends nothing anywhere. No inline handlers (CSP): every listener is added here.
 */
(function () {
  'use strict';

  const C = window.EdgeHelpContent || { SUPPORT: {}, FEATURE_LABELS: {}, topics: {}, anchors: [], articles: [] };
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v === null || v === undefined ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const ico = (name, size) => {
    try { return window.EdgeIcon ? window.EdgeIcon.svg(name, { size: size || 'md' }) : ''; } catch (_) { return ''; }
  };
  const account = () => window.EdgeAuth || {};
  const isAdmin = () => !!account().isAdmin;
  const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

  // ===================================================================== errors

  const ROLE_DENIED = "Your account can't change setup. Ask an administrator.";

  function detailText(body) {
    if (!body) return '';
    const d = body.detail !== undefined ? body.detail : body.message;
    if (typeof d === 'string') return d;
    if (d && typeof d.message === 'string') return d.message;
    return '';
  }

  /**
   * One plain sentence for a failed request. status 0 = no answer at all.
   * Never shows codes or stack traces; uses the server's own words only where
   * they are written for people (camera limit, paused message, validation).
   */
  function friendlyError(status, body) {
    const code = body && typeof body.code === 'string' ? body.code : '';
    const detail = detailText(body);
    const offline = typeof navigator !== 'undefined' && navigator.onLine === false;
    if (!status || offline) {
      return offline
        ? 'This computer or phone is offline. Check its Wi-Fi or network connection, then try again.'
        : "Can't reach the store's CCTV device. Check your network connection; if it keeps happening, the device may be off or restarting.";
    }
    if (status === 401) return 'Your sign-in has ended. Please sign in again.';
    if (status === 403) {
      if (code === 'feature_disabled') {
        const label = (body.feature && C.FEATURE_LABELS[body.feature]) || 'This feature';
        return `${label} is not included in your iKorex plan. Please contact iKorex to add it.`;
      }
      if (detail === ROLE_DENIED || /setup/i.test(detail)) return ROLE_DENIED;
      return "Your account isn't allowed to do this. Ask an owner or administrator.";
    }
    if (status === 423) {
      const msg = body && (body.message || detail) ? ` iKorex says: "${body.message || detail}"` : '';
      if (code === 'service_standby') return `This system is on standby, so this can't be done right now.${msg}`;
      return `The iKorex service is paused, so this can't be done right now. Settings and evidence are kept.${msg}`;
    }
    if (status === 409 && code === 'camera_limit') {
      return detail || 'Your iKorex plan\'s camera limit is reached. Turn another camera off first, or contact iKorex to add more.';
    }
    if (status === 404) return 'That item is no longer there. Refresh the page and try again.';
    if (status === 413) return 'That file is too large.';
    if (status === 429) return 'Too many attempts. Wait a few minutes and try again.';
    if (status === 400 || status === 409 || status === 422) {
      return detail && detail.length <= 240 ? detail : "Some details weren't accepted. Check them and try again.";
    }
    if (status >= 500) return 'The CCTV device had a problem doing this. Try again in a minute; if it keeps happening, contact iKorex support.';
    return 'That did not work. Try again in a moment.';
  }

  /** GET (or any fetch) that never throws: { ok, status, data } or { ok:false, status, body, message }. */
  async function fetchJSON(url, opts) {
    let res;
    try { res = await fetch(url, opts); } catch (_) { return { ok: false, status: 0, body: null, message: friendlyError(0, null) }; }
    let body = null;
    try { body = await res.json(); } catch (_) { body = null; }
    if (!res.ok) return { ok: false, status: res.status, body, message: friendlyError(res.status, body) };
    return { ok: true, status: res.status, data: body };
  }

  // ==================================================================== actions

  const later = (fn, ms) => setTimeout(() => { try { fn(); } catch (_) { /* screen not ready */ } }, ms || 120);
  const call = (name, ...args) => (typeof window[name] === 'function' ? window[name](...args) : undefined);

  function flash(node) {
    if (!node) return;
    node.classList.add('flash-highlight');
    setTimeout(() => node.classList.remove('flash-highlight'), 1600);
  }

  function openSettingsCard(id) {
    call('switchTab', 'settings');
    later(() => {
      const card = $(id);
      if (!card) return;
      card.scrollIntoView({ behavior: 'instant', block: 'start' });
      flash(card);
    }, 150);
  }

  /** Open the screen an article or checklist step points to. Unknown names do nothing. */
  function go(action) {
    const a = String(action || '');
    if (a.startsWith('settings:')) { openSettingsCard(a.slice(9)); return; }
    switch (a) {
      case 'today': case 'cameras': case 'map': case 'insights': case 'loss':
        call('switchTab', a); break;
      case 'insights-footfall': call('switchTab', 'insights/footfall'); break;
      case 'add-cameras': call('openDeviceManager'); break;
      case 'map-edit':
        call('switchTab', 'map');
        later(() => call('toggleMapEditing', true), 150);
        break;
      case 'map-zone':
        call('switchTab', 'map');
        later(() => {
          call('toggleMapEditing', true);
          if (window.blueprintEditor && typeof window.blueprintEditor.setTool === 'function') window.blueprintEditor.setTool('ZONE');
        }, 200);
        break;
      case 'studio': window.location.href = '/dashboard/studio'; break;
      case 'checklist': if (window.EdgeOnboarding) window.EdgeOnboarding.showChecklist(); break;
      case 'report': if (window.EdgeSupport) window.EdgeSupport.openReport(); break;
      case 'welcome': if (window.EdgeOnboarding) window.EdgeOnboarding.showWelcome(); break;
      default: break;
    }
  }

  // =================================================================== popover

  let pop = null;
  let popBtn = null;

  function tipButton(topicId) {
    const t = C.topics[topicId];
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'help-tip';
    b.setAttribute('data-help-topic', topicId);
    b.setAttribute('aria-label', `What is ${t ? t.title : 'this'}?`);
    b.setAttribute('aria-expanded', 'false');
    b.setAttribute('aria-controls', 'edgeHelpPopover');
    b.title = 'What is this?';
    b.innerHTML = ico('circle-help', 'sm');
    return b;
  }

  function ensurePop() {
    if (pop) return pop;
    pop = document.createElement('div');
    pop.id = 'edgeHelpPopover';
    pop.className = 'help-pop';
    pop.setAttribute('role', 'dialog');
    pop.setAttribute('aria-modal', 'false');
    pop.setAttribute('aria-labelledby', 'edgeHelpPopTitle');
    pop.tabIndex = -1;
    pop.hidden = true;
    pop.innerHTML = `
      <div class="help-pop-head">
        <span class="help-pop-title" id="edgeHelpPopTitle"></span>
        <button type="button" class="btn btn-ghost btn-icon btn-xs help-pop-close" aria-label="Close this help">${ico('x', 'sm')}</button>
      </div>
      <div class="help-pop-body"></div>
      <div class="help-pop-foot"></div>`;
    document.body.appendChild(pop);
    pop.querySelector('.help-pop-close').addEventListener('click', () => closePop(true));
    pop.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closePop(true); }
    });
    pop.addEventListener('focusout', (e) => {
      const to = e.relatedTarget;
      if (to && !pop.contains(to) && to !== popBtn) closePop(false);
    });
    pop.addEventListener('click', (e) => {
      const more = e.target.closest('[data-help-article]');
      if (more) { const id = more.getAttribute('data-help-article'); closePop(false); openCentre(id); }
    });
    return pop;
  }

  function topicBodyHtml(t) {
    const paras = (t.text || []).map((p) => `<p>${esc(p)}</p>`).join('');
    const list = t.list && t.list.length ? `<ul>${t.list.map((i) => `<li>${esc(i)}</li>`).join('')}</ul>` : '';
    return paras + list;
  }

  function positionPop() {
    if (!pop || pop.hidden || !popBtn) return;
    if (!popBtn.isConnected) { closePop(false); return; }
    const vw = window.innerWidth, vh = window.innerHeight;
    if (vw < 560) {           // phones: a sheet along the bottom, clear of the tab bar
      pop.classList.add('help-pop-sheet');
      pop.style.left = ''; pop.style.top = ''; pop.style.width = '';
      return;
    }
    pop.classList.remove('help-pop-sheet');
    const r = popBtn.getBoundingClientRect();
    const w = Math.min(340, vw - 32);
    pop.style.width = `${w}px`;
    const h = pop.offsetHeight;
    let left = Math.min(Math.max(16, r.left + r.width / 2 - w / 2), vw - w - 16);
    let top = r.bottom + 8;
    if (top + h > vh - 16) top = Math.max(16, r.top - h - 8);
    pop.style.left = `${Math.round(left)}px`;
    pop.style.top = `${Math.round(top)}px`;
  }

  function openPop(btn) {
    const id = btn.getAttribute('data-help-topic');
    const t = C.topics[id];
    if (!t) return;
    ensurePop();
    if (popBtn && popBtn !== btn) popBtn.setAttribute('aria-expanded', 'false');
    popBtn = btn;
    pop.querySelector('.help-pop-title').textContent = t.title;
    pop.querySelector('.help-pop-body').innerHTML = topicBodyHtml(t);
    const art = t.article && C.articles.find((a) => a.id === t.article);
    pop.querySelector('.help-pop-foot').innerHTML = art
      ? `<button type="button" class="link-btn" data-help-article="${esc(art.id)}">Read the guide: ${esc(art.title)}</button>` : '';
    pop.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
    positionPop();
    pop.focus({ preventScroll: true });
  }

  function closePop(restoreFocus) {
    if (!pop || pop.hidden) return;
    pop.hidden = true;
    const b = popBtn;
    popBtn = null;
    if (b) {
      b.setAttribute('aria-expanded', 'false');
      if (restoreFocus && b.isConnected) b.focus({ preventScroll: true });
    }
  }

  /** Popover for a topic next to any element (used by other scripts). */
  function openTopic(topicId, anchorEl) {
    if (!anchorEl) return;
    const b = anchorEl.matches && anchorEl.matches('[data-help-topic]') ? anchorEl : null;
    if (b) { openPop(b); return; }
    const tmp = tipButton(topicId);
    anchorEl.insertAdjacentElement('afterend', tmp);
    openPop(tmp);
  }

  // ================================================================= decorating

  function wantsAfter(node) {
    const tag = node.tagName;
    if (tag === 'BUTTON' || tag === 'INPUT' || tag === 'SELECT' || tag === 'A') return true;
    // A label tied to a control: a button inside it would move focus to the control.
    return tag === 'LABEL' && (node.htmlFor || node.querySelector('input, select, textarea'));
  }

  function addTip(node, topicId, where) {
    if (!node || !C.topics[topicId]) return;
    if (where === 'after' || wantsAfter(node)) {
      const next = node.nextElementSibling;
      if (next && next.classList.contains('help-tip') && next.getAttribute('data-help-topic') === topicId) return;
      node.insertAdjacentElement('afterend', tipButton(topicId));
      return;
    }
    for (const child of node.children) {
      if (child.classList.contains('help-tip')) return;
    }
    node.appendChild(tipButton(topicId));
  }

  function decorate() {
    document.querySelectorAll('[data-help]').forEach((n) => addTip(n, n.getAttribute('data-help'), 'append'));
    document.querySelectorAll('[data-help-after]').forEach((n) => addTip(n, n.getAttribute('data-help-after'), 'after'));
    (C.anchors || []).forEach((a) => {
      try { document.querySelectorAll(a.selector).forEach((n) => addTip(n, a.topic, a.where || 'append')); } catch (_) { /* bad selector */ }
    });
    if (popBtn && !popBtn.isConnected) closePop(false);
  }

  let decorateTimer = null;
  function scheduleDecorate() {
    if (decorateTimer) return;
    decorateTimer = setTimeout(() => { decorateTimer = null; decorate(); }, 250);
  }

  // Busy live regions re-render many times a second; their changes never add a card title.
  const NOISY = '#cameraMatrixGrid, .fp-hud-bottom, #fpCamBadges, #liveBehaviourList, .toast, #edgeHelpPopover, #edgeHelpCentre';
  function watch() {
    if (typeof MutationObserver !== 'function') return;
    const mo = new MutationObserver((records) => {
      for (const r of records) {
        if (r.target.nodeType !== 1 || (r.target.closest && r.target.closest(NOISY))) continue;
        for (const n of r.addedNodes) {
          if (n.nodeType === 1 && !n.classList.contains('help-tip')) { scheduleDecorate(); return; }
        }
      }
    });
    mo.observe(document.body, { childList: true, subtree: true });
  }

  // ================================================================ help panel

  let centre = null;
  let scrim = null;
  let opener = null;
  let currentArticle = null;
  const READ_KEY = 'edge_cctv_help_read';

  function markRead(id) {
    try {
      const k = `${READ_KEY}:${account().id || account().username || 'anon'}`;
      const set = new Set(JSON.parse(localStorage.getItem(k) || '[]'));
      set.add(id);
      localStorage.setItem(k, JSON.stringify([...set]));
    } catch (_) { /* storage blocked: nothing to remember */ }
    try { window.dispatchEvent(new CustomEvent('edge:help-read', { detail: { article: id } })); } catch (_) {}
  }

  function hasRead(id) {
    try {
      const k = `${READ_KEY}:${account().id || account().username || 'anon'}`;
      return JSON.parse(localStorage.getItem(k) || '[]').includes(id);
    } catch (_) { return false; }
  }

  const visibleArticles = () => C.articles.filter((a) => a.role !== 'admin' || isAdmin());

  // The contact comes from the signed iKorex policy when there is one (js/support.js),
  // else from help_content.js SUPPORT.
  function supportHtml() {
    let contact = '';
    try { contact = window.EdgeSupport ? window.EdgeSupport.contactHtml({ titleId: 'helpSupportTitle' }) : ''; } catch (_) { contact = ''; }
    if (!contact) {
      const s = C.SUPPORT || {};
      const rows = [];
      if (s.phone) rows.push(`<li><span class="help-support-k">Phone</span><a href="tel:${esc(String(s.phone).replace(/[^0-9+]/g, ''))}">${esc(s.phone)}</a></li>`);
      if (s.email) rows.push(`<li><span class="help-support-k">Email</span><a href="mailto:${esc(s.email)}">${esc(s.email)}</a></li>`);
      if (s.hours) rows.push(`<li><span class="help-support-k">Hours</span><span>${esc(s.hours)}</span></li>`);
      if (s.website) rows.push(`<li><span class="help-support-k">Web</span><a href="${esc(s.website)}" target="_blank" rel="noopener">${esc(s.website)}</a></li>`);
      const body = rows.length
        ? `<ul class="help-support-list">${rows.join('')}</ul>`
        : '<p class="help-muted">Ask your installer for the iKorex support phone number and email.</p>';
      contact = `<h3 class="help-h3" id="helpSupportTitle">${ico('users', 'sm')}Contact ${esc(s.name || 'iKorex support')}</h3>${body}`;
    }
    return `<section class="help-support" aria-labelledby="helpSupportTitle">
      ${contact}
      <div class="help-quick help-support-actions">
        <button type="button" class="btn btn-sm btn-primary" data-support-report>${ico('circle-alert', 'sm')}Report a problem</button>
        <button type="button" class="btn btn-sm" data-support-reports>My reports</button>
      </div>
      <p class="help-muted">When you call, say which screen you are on and what you pressed. Never send camera pictures or passwords by email.</p>
    </section>`;
  }

  function ensureCentre() {
    if (centre) return centre;
    scrim = document.createElement('div');
    scrim.className = 'help-scrim';
    scrim.hidden = true;
    scrim.addEventListener('click', () => closeCentre());
    centre = document.createElement('aside');
    centre.id = 'edgeHelpCentre';
    centre.className = 'help-centre';
    centre.setAttribute('role', 'dialog');
    centre.setAttribute('aria-modal', 'true');
    centre.setAttribute('aria-labelledby', 'helpCentreTitle');
    centre.hidden = true;
    centre.innerHTML = `
      <div class="help-centre-head">
        <h2 class="help-centre-title" id="helpCentreTitle">${ico('circle-help')}Help</h2>
        <button type="button" class="btn btn-ghost btn-icon btn-sm" data-hc="close" aria-label="Close help">${ico('x')}</button>
      </div>
      <div class="help-centre-search">
        <label for="helpSearch" class="sr-only">Search help</label>
        <span class="search-field help-search-field">${ico('search')}<input type="search" id="helpSearch" name="helpSearch" class="search-input help-search-input"
          placeholder="Search help, for example calibrate" autocomplete="off" spellcheck="false"></span>
      </div>
      <div class="help-centre-body" id="helpCentreBody"></div>`;
    document.body.appendChild(scrim);
    document.body.appendChild(centre);

    const search = centre.querySelector('#helpSearch');
    search.addEventListener('input', () => renderList(search.value));
    search.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        const first = centre.querySelector('[data-hc-article]');
        if (first) { e.preventDefault(); showArticle(first.getAttribute('data-hc-article')); }
      }
    });
    centre.addEventListener('click', (e) => {
      const t = e.target.closest('[data-hc], [data-hc-article], [data-hc-go]');
      if (!t) return;
      if (t.hasAttribute('data-hc-article')) { showArticle(t.getAttribute('data-hc-article')); return; }
      if (t.hasAttribute('data-hc-go')) { const g = t.getAttribute('data-hc-go'); closeCentre(false); go(g); return; }
      const what = t.getAttribute('data-hc');
      if (what === 'close') closeCentre();
      else if (what === 'back') { renderList(search.value); search.focus(); }
    });
    centre.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeCentre(); return; }
      if (e.key !== 'Tab') return;
      const items = [...centre.querySelectorAll(FOCUSABLE)].filter((n) => n.offsetParent !== null || n === document.activeElement);
      if (!items.length) return;
      const first = items[0], last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    });
    return centre;
  }

  function matches(a, words) {
    if (!words.length) return 1;
    const title = a.title.toLowerCase();
    const hay = [a.title, a.keywords, a.summary, ...(a.steps || []), ...(a.tips || [])].join(' ').toLowerCase();
    if (!words.every((w) => hay.includes(w))) return 0;
    return words.some((w) => title.includes(w)) ? 2 : 1;
  }

  function renderList(query) {
    currentArticle = null;
    const body = centre.querySelector('#helpCentreBody');
    const words = String(query || '').toLowerCase().split(/[^a-z0-9-]+/).filter((w) => w.length > 1);
    const found = visibleArticles()
      .map((a, i) => ({ a, score: matches(a, words), i }))
      .filter((x) => x.score)
      .sort((x, y) => (y.score - x.score) || (x.i - y.i));
    const quick = words.length ? '' : `
      <div class="help-quick">
        <button type="button" class="btn btn-sm" data-hc-go="checklist">${ico('circle-check', 'sm')}Getting started checklist</button>
        <button type="button" class="btn btn-sm" data-hc-go="welcome">${ico('play', 'sm')}Welcome tour</button>
      </div>`;
    const list = found.length
      ? `<ul class="help-list">${found.map(({ a }) => `
          <li><button type="button" class="help-item" data-hc-article="${esc(a.id)}">
            <span class="help-item-title">${esc(a.title)}${hasRead(a.id) ? '<span class="help-item-read">Read</span>' : ''}</span>
            <span class="help-item-sum">${esc(a.summary)}</span>
          </button></li>`).join('')}</ul>`
      : `<div class="empty-state help-empty">${ico('search', 'lg')}
          <div class="empty-state-title">No guide matches "${esc(query)}"</div>
          <div class="empty-state-text">Try one simple word such as camera, map, alert, phone or backup, or contact iKorex support below.</div></div>`;
    body.innerHTML = `${quick}
      <p class="help-count" role="status" aria-live="polite">${words.length ? `${found.length} guide${found.length === 1 ? '' : 's'} found` : 'How-to guides'}</p>
      ${list}
      ${supportHtml()}`;
  }

  function showArticle(id) {
    const a = visibleArticles().find((x) => x.id === id);
    if (!a) { renderList(''); return; }
    currentArticle = id;
    markRead(id);
    const body = centre.querySelector('#helpCentreBody');
    const canGo = a.go && (isAdmin() || !String(a.go).startsWith('map-') && a.go !== 'add-cameras' && a.go !== 'studio');
    body.innerHTML = `
      <button type="button" class="btn btn-ghost btn-sm help-back" data-hc="back">${ico('arrow-left', 'sm')}All guides</button>
      <article class="help-article" aria-labelledby="helpArticleTitle">
        <h3 class="help-article-title" id="helpArticleTitle" tabindex="-1">${esc(a.title)}</h3>
        <p class="help-article-sum">${esc(a.summary)}</p>
        ${a.steps && a.steps.length ? `<ol class="help-steps">${a.steps.map((s) => `<li>${esc(s)}</li>`).join('')}</ol>` : ''}
        ${a.tips && a.tips.length ? `<div class="help-tips"><div class="help-tips-title">${ico('info', 'sm')}Good to know</div><ul>${a.tips.map((s) => `<li>${esc(s)}</li>`).join('')}</ul></div>` : ''}
        ${canGo ? `<button type="button" class="btn btn-primary btn-sm help-go" data-hc-go="${esc(a.go)}">${esc(a.goLabel || 'Take me there')}${ico('arrow-right', 'sm')}</button>` : ''}
      </article>
      ${supportHtml()}`;
    const h = body.querySelector('#helpArticleTitle');
    if (h) h.focus({ preventScroll: false });
    body.scrollTop = 0;
  }

  function openCentre(articleId) {
    closePop(false);
    ensureCentre();
    if (!centre.hidden && !articleId) { centre.querySelector('#helpSearch').focus(); return; }
    if (centre.hidden) opener = document.activeElement;
    const search = centre.querySelector('#helpSearch');
    centre.hidden = false;
    scrim.hidden = false;
    document.documentElement.classList.add('help-open');
    const btn = $('navHelp');
    if (btn) btn.setAttribute('aria-expanded', 'true');
    if (articleId) showArticle(articleId);
    else { search.value = ''; renderList(''); search.focus(); }
  }

  function closeCentre(restoreFocus = true) {
    if (!centre || centre.hidden) return;
    centre.hidden = true;
    scrim.hidden = true;
    document.documentElement.classList.remove('help-open');
    const btn = $('navHelp');
    if (btn) btn.setAttribute('aria-expanded', 'false');
    const back = opener;
    opener = null;
    if (restoreFocus && back && back.isConnected && typeof back.focus === 'function') back.focus({ preventScroll: true });
  }

  // ===================================================================== wiring

  function init() {
    document.addEventListener('click', (e) => {
      const tip = e.target.closest('[data-help-topic]');
      if (tip) {
        e.preventDefault();
        if (popBtn === tip && pop && !pop.hidden) closePop(true);
        else openPop(tip);
        return;
      }
      if (e.target.closest('[data-help-centre]')) { e.preventDefault(); openCentre(); return; }
      // Hooks for empty states in other modules (no inline handlers needed):
      // data-help-go="<action>" opens a screen, data-help-open="<article>" a guide.
      const goEl = e.target.closest('[data-help-go]');
      if (goEl && !goEl.closest('#edgeHelpCentre')) { e.preventDefault(); go(goEl.getAttribute('data-help-go')); return; }
      const artEl = e.target.closest('[data-help-open]');
      if (artEl) { e.preventDefault(); openCentre(artEl.getAttribute('data-help-open')); return; }
      if (pop && !pop.hidden && !pop.contains(e.target)) closePop(false);
    });
    window.addEventListener('resize', positionPop);
    window.addEventListener('scroll', () => { if (pop && !pop.hidden) positionPop(); }, true);
    // The popover belongs to the screen it was opened on.
    window.addEventListener('edge:tab', () => closePop(false));
    decorate();
    watch();
  }

  window.EdgeHelp = Object.freeze({
    open: (articleId) => openCentre(articleId),
    close: () => closeCentre(),
    openTopic,
    tipButton,
    decorate,
    go,
    hasRead,
    friendlyError,
    fetchJSON,
  });

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
