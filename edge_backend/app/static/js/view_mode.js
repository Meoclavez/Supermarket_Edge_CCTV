/*
 * Simple / All settings (window.edgeMode).
 *
 * Simple is the default for everyone: anything marked data-advanced (cards, fields, notes,
 * long help lines) is hidden by css/style.css under <html data-mode="simple">. Owners and
 * admins can switch to "All settings" with the header switch; the choice is remembered per
 * account in this browser (localStorage "edge_view_mode:<account id>"). Operators always
 * see Simple: they do not change setup.
 *
 * Deep links never land on something hidden: edgeMode.reveal(target) shows that element
 * (and the advanced parts inside it) until the person moves to another tab, with a note
 * offering "Show all settings". help.js (go), analytics.js (jumpTo, openDeviceManager) call
 * it; as a safety net, scrollIntoView() on a hidden advanced element reveals it too, so
 * every guide, checklist step and Features "Full settings" link keeps working.
 *
 * Loaded as a classic script in <head> so the mode is set before the first paint.
 */
(function () {
  'use strict';

  const root = document.documentElement;
  const KEY = 'edge_view_mode';
  const LAST = `${KEY}:last`;
  const MODES = ['simple', 'all'];

  function read(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }
  function write(key, v) { try { localStorage.setItem(key, v); } catch (_) { /* private mode */ } }

  function account() { return window.EdgeAuth || {}; }
  function canSwitch() { const a = account(); return !a.known || !!a.isAdmin; }
  function userKey() { const a = account(); return a.id || a.username ? `${KEY}:${a.id || a.username}` : null; }

  /** The stored choice for the signed-in account (Simple when none, and always for operators). */
  function stored() {
    const a = account();
    if (a.known && !a.isAdmin) return 'simple';
    const k = userKey();
    const v = (k && read(k)) || read(LAST);
    return MODES.includes(v) ? v : 'simple';
  }

  function mode() { return root.getAttribute('data-mode') === 'all' ? 'all' : 'simple'; }

  function set(m, remember) {
    const next = MODES.includes(m) ? m : 'simple';
    if (next === 'all' && !canSwitch()) return;
    root.setAttribute('data-mode', next);
    if (remember) {
      const k = userKey();
      if (k) write(k, next);
      write(LAST, next);
    }
    if (next === 'all') clearReveal();
    syncControl();
    try { window.dispatchEvent(new CustomEvent('edge:view-mode', { detail: { mode: next } })); } catch (_) { /* old browser */ }
  }

  // ------------------------------------------------------------------ reveal

  let revealTab = null;

  function isHidden(node) {
    if (mode() !== 'simple' || !node || !node.closest) return false;
    const adv = node.closest('[data-advanced]');
    return !!adv && !adv.closest('[data-revealed]') && !adv.hasAttribute('data-revealed');
  }

  /** Show one element (an id or a node) and the advanced parts inside it until the tab changes. */
  function reveal(target) {
    const node = typeof target === 'string' ? document.getElementById(target.replace(/^#/, '')) : target;
    if (!node || mode() !== 'simple') return node || null;
    let marked = false;
    if (node.hasAttribute('data-advanced') || node.querySelector('[data-advanced]')) { node.setAttribute('data-revealed', ''); marked = true; }
    for (let p = node.parentElement; p; p = p.parentElement) {
      if (p.hasAttribute('data-advanced')) { p.setAttribute('data-revealed', ''); marked = true; }
    }
    if (marked) {
      revealTab = currentTab();
      showNote();
    }
    return node;
  }

  function clearReveal() {
    document.querySelectorAll('[data-revealed]').forEach((n) => n.removeAttribute('data-revealed'));
    revealTab = null;
    hideNote();
  }

  function currentTab() {
    const b = document.querySelector('[data-tab].active');
    return b ? b.getAttribute('data-tab') : null;
  }

  // Leaving the tab hides what a link revealed again.
  window.addEventListener('edge:tab', (e) => {
    const tab = e && e.detail ? e.detail.tab : null;
    if (revealTab && tab !== revealTab) clearReveal();
  });

  // Safety net for links that scroll to an element without calling reveal().
  const nativeScroll = Element.prototype.scrollIntoView;
  Element.prototype.scrollIntoView = function scrollIntoViewRevealing(...args) {
    if (isHidden(this)) reveal(this);
    return nativeScroll.apply(this, args);
  };

  // --------------------------------------------------------------- the note

  let note = null;
  function showNote() {
    if (!document.body) return;
    if (!note) {
      note = document.createElement('div');
      note.id = 'modeRevealNote';
      note.className = 'mode-reveal-note';
      note.setAttribute('role', 'status');
      note.innerHTML = '<span>Showing a setting from <b>All settings</b>.</span>'
        + '<button type="button" class="btn btn-xs" data-mode-act="all">Show all settings</button>'
        + '<button type="button" class="btn btn-xs btn-ghost" data-mode-act="hide">Hide it again</button>';
      note.addEventListener('click', (e) => {
        const b = e.target.closest('[data-mode-act]');
        if (!b) return;
        if (b.getAttribute('data-mode-act') === 'all') set('all', true);
        else clearReveal();
      });
      document.body.appendChild(note);
    }
    const allBtn = note.querySelector('[data-mode-act="all"]');
    if (allBtn) allBtn.hidden = !canSwitch();
    note.hidden = false;
  }
  function hideNote() { if (note) note.hidden = true; }

  // ------------------------------------------------------------ the switch

  function syncControl() {
    const b = document.getElementById('viewModeToggle');
    if (!b) return;
    const all = mode() === 'all';
    b.setAttribute('aria-pressed', all ? 'true' : 'false');
    b.hidden = !canSwitch();
    const label = b.querySelector('.view-mode-label');
    if (label) label.textContent = all ? 'All settings' : 'Simple';
    b.title = all ? 'Showing every setting. Click for the simple view.'
      : 'Simple view: the everyday settings. Click to show every setting.';
  }

  function bindControl() {
    const b = document.getElementById('viewModeToggle');
    if (!b || b.dataset.bound) return;
    b.dataset.bound = '1';
    b.addEventListener('click', () => set(mode() === 'all' ? 'simple' : 'all', true));
    syncControl();
  }

  // First paint: the last choice made in this browser (operators corrected once known).
  root.setAttribute('data-mode', stored());

  window.addEventListener('edge:account', () => { set(stored(), false); });
  window.addEventListener('edge:signed-out', () => { clearReveal(); root.setAttribute('data-mode', 'simple'); syncControl(); });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bindControl);
  else bindControl();

  window.edgeMode = Object.freeze({
    mode,
    set: (m) => set(m, true),
    reveal,
    isHidden,
    canSwitch,
  });
}());
