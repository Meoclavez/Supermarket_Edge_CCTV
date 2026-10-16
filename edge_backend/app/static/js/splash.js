/**
 * Intro screen (#edgeSplash, the first child of <body> on the dashboard and
 * Studio; styled by css/brand.css so it paints before any script runs).
 *
 * It fades out once the page is ready: the DOM is parsed and the sign-in
 * check in auth.js has settled (edgeAuth.onReady), then one frame is painted.
 * It stays at least ~900 ms on the first page of a browser session and
 * ~300 ms on later pages (sessionStorage), never longer than 8 s, and goes
 * at once when the sign-in gate opens (the gate also sits above it).
 * It is aria-hidden and holds nothing focusable, so it never traps focus.
 * Without this script the CSS failsafe animation hides it after 8 s.
 */
(function () {
  'use strict';

  const el = document.getElementById('edgeSplash');
  if (!el) return;

  const KEY = 'edge_cctv_splash_seen';
  const MAX_MS = 8000;
  const t0 = Date.now();
  const elapsed = () => (window.performance && typeof performance.now === 'function'
    ? performance.now() : Date.now() - t0);

  let seen = false;
  try {
    seen = sessionStorage.getItem(KEY) === '1';
    sessionStorage.setItem(KEY, '1');
  } catch (_) { /* storage blocked: treat every page as a first visit */ }
  const MIN_MS = seen ? 300 : 900;

  let done = false;
  let maxTimer = null;

  function remove() {
    if (el.parentNode) el.parentNode.removeChild(el);
  }

  function hide() {
    if (done) return;
    done = true;
    if (maxTimer) clearTimeout(maxTimer);
    el.classList.add('is-leaving');
    el.addEventListener('transitionend', remove, { once: true });
    setTimeout(remove, 600); // no transition (hidden tab, reduced motion): still removed
  }

  function hideAfterMinimum() {
    const wait = Math.max(0, MIN_MS - elapsed());
    setTimeout(() => {
      if (typeof requestAnimationFrame === 'function') requestAnimationFrame(hide);
      else hide();
    }, wait);
  }

  maxTimer = setTimeout(hide, Math.max(0, MAX_MS - elapsed()));

  const auth = window.edgeAuth;
  if (auth && typeof auth.onReady === 'function') {
    auth.onReady(() => {
      if (typeof auth.isGateOpen === 'function' && auth.isGateOpen()) hide();
      else hideAfterMinimum();
    });
  } else if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', hideAfterMinimum, { once: true });
  } else {
    hideAfterMinimum();
  }

  // The gate can also open later (session expired during the intro).
  window.addEventListener('edge:auth', () => {
    if (auth && typeof auth.isGateOpen === 'function' && auth.isGateOpen()) hide();
  });
})();
