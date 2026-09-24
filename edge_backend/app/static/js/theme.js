/**
 * Light / dark / system colour theme, shared by the dashboard and Studio.
 *
 * The choice is stored per browser in localStorage ('edge_cctv_theme') and is
 * applied before first paint by a tiny inline script in each page's <head>,
 * which sets <html data-theme="light|dark|system">. No stored choice means
 * the original dark theme. "system" follows prefers-color-scheme through the
 * @media block in style.css.
 *
 * This file adds the header control and keeps everything in sync: whenever the
 * effective theme can have changed it dispatches a window 'edge:theme' event
 * ({ mode, theme }) so canvases and charts re-read their colour tokens.
 */
(function () {
  'use strict';

  const KEY = 'edge_cctv_theme';
  const MODES = ['light', 'dark', 'system'];
  const LABEL = { light: 'Light', dark: 'Dark', system: 'System' };
  const ICON = {
    light: '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" focusable="false"><circle cx="12" cy="12" r="4.2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M12 2.5v2.6M12 18.9v2.6M4.6 4.6l1.8 1.8M17.6 17.6l1.8 1.8M2.5 12h2.6M18.9 12h2.6M4.6 19.4l1.8-1.8M17.6 6.4l1.8-1.8" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    dark: '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" focusable="false"><path d="M20 14.6A8.2 8.2 0 0 1 9.4 4a8.2 8.2 0 1 0 10.6 10.6Z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>',
    system: '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" focusable="false"><rect x="3" y="4" width="18" height="12.5" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M8.5 20.5h7M12 16.5v4" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
  };

  const root = document.documentElement;
  const mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null;

  function readStored() {
    try {
      const v = localStorage.getItem(KEY);
      return MODES.includes(v) ? v : null;
    } catch (_) { return null; }
  }

  function writeStored(mode) {
    try { localStorage.setItem(KEY, mode); } catch (_) { /* storage blocked: applies to this page only */ }
  }

  /** The operator's choice: 'light' | 'dark' | 'system'. */
  function mode() {
    const a = root.getAttribute('data-theme');
    return MODES.includes(a) ? a : (readStored() || 'dark');
  }

  /** The theme actually shown: 'light' | 'dark'. */
  function resolved(m) {
    const cur = m || mode();
    if (cur === 'system') return mq && mq.matches ? 'light' : 'dark';
    return cur;
  }

  function announce() {
    window.dispatchEvent(new CustomEvent('edge:theme', { detail: { mode: mode(), theme: resolved() } }));
  }

  function apply(m, persist) {
    const next = MODES.includes(m) ? m : 'dark';
    const before = root.getAttribute('data-theme');
    root.setAttribute('data-theme', next);
    if (persist) writeStored(next);
    syncControls();
    if (before !== next) announce();
  }

  function syncControls() {
    const cur = mode();
    document.querySelectorAll('.theme-toggle').forEach((group) => {
      group.querySelectorAll('button[data-theme-mode]').forEach((b) => {
        const on = b.getAttribute('data-theme-mode') === cur;
        b.setAttribute('aria-checked', on ? 'true' : 'false');
        b.tabIndex = on ? 0 : -1;
      });
      group.title = `Colour theme: ${LABEL[cur]}${cur === 'system' ? ` (now ${resolved(cur)})` : ''}`;
    });
  }

  function buildControl() {
    const group = document.createElement('div');
    group.className = 'theme-toggle';
    group.id = 'themeToggle';
    group.setAttribute('role', 'radiogroup');
    group.setAttribute('aria-label', 'Colour theme');
    MODES.forEach((m) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'theme-toggle-btn';
      b.setAttribute('role', 'radio');
      b.setAttribute('data-theme-mode', m);
      b.setAttribute('aria-label', `${LABEL[m]} theme`);
      b.title = m === 'system' ? 'Follow the system setting' : `${LABEL[m]} theme`;
      b.innerHTML = ICON[m];
      b.addEventListener('click', () => apply(m, true));
      group.appendChild(b);
    });
    // Arrow keys move between the options, as in any radio group.
    group.addEventListener('keydown', (e) => {
      const i = MODES.indexOf(mode());
      let j = -1;
      if (e.key === 'ArrowRight' || e.key === 'ArrowDown') j = (i + 1) % MODES.length;
      else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') j = (i + MODES.length - 1) % MODES.length;
      if (j < 0) return;
      e.preventDefault();
      apply(MODES[j], true);
      const btn = group.querySelector(`[data-theme-mode="${MODES[j]}"]`);
      if (btn) btn.focus();
    });
    return group;
  }

  /** Put the control in the page header, just before Sign out when present. */
  function mount() {
    if (document.getElementById('themeToggle')) return;
    const host = document.querySelector('.header-actions') || document.querySelector('header');
    if (!host) return;
    const control = buildControl();
    const signOut = document.getElementById('authSignOut');
    if (signOut && signOut.parentNode === host) host.insertBefore(control, signOut);
    else host.appendChild(control);
    syncControls();
  }

  // Keep the attribute honest if the inline <head> script could not run.
  if (!MODES.includes(root.getAttribute('data-theme'))) root.setAttribute('data-theme', readStored() || 'dark');

  if (mq) {
    const onSystemChange = () => { if (mode() === 'system') { syncControls(); announce(); } };
    if (mq.addEventListener) mq.addEventListener('change', onSystemChange);
    else if (mq.addListener) mq.addListener(onSystemChange);
  }

  // Dashboard and Studio share the setting: a change in another tab applies here.
  window.addEventListener('storage', (e) => {
    if (e.key === KEY) apply(MODES.includes(e.newValue) ? e.newValue : 'dark', false);
  });

  window.edgeTheme = { mode, resolved, set: (m) => apply(m, true), MODES: MODES.slice() };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
  else mount();
})();
