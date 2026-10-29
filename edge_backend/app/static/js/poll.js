/**
 * One place for the dashboard's background polling (window.EdgePoll).
 *
 * Measured 2026-10-29: one idle dashboard sent 150-250 API requests a minute
 * (Pearcedale box, 155-174 a minute through the tunnel) against a limit of 100
 * a minute per address. The causes were polls that ran on every tab and with
 * the page hidden, several modules polling the same endpoint, alert lists
 * polled every 5 s although the alert websocket delivers them at once, and no
 * reaction to "429 Too Many Requests". This module fixes that for every
 * module that uses it:
 *
 *   EdgePoll.every(ms, fn, opts) -> {stop, kick}
 *       Runs fn every ms, one run at a time. Paused while the page is hidden
 *       (opts.hiddenMs: run that slowly instead), skipped while opts.when()
 *       is false, slowed to opts.wsMs while the alert websocket is connected,
 *       and paused during a 429 back-off. When the page becomes visible again
 *       an overdue poll runs at once, so nothing goes stale.
 *   EdgePoll.get(url, maxAgeMs) -> Promise<json|null>
 *       Shared GET: modules asking for the same URL within maxAgeMs (or while
 *       a request is in flight) share one request.
 *   EdgePoll.put(url, data)          seed the shared cache (e.g. after a fresh load)
 *   EdgePoll.backoffUntil() / EdgePoll.inBackoff()
 *       A 429 from /api/ (Retry-After honoured, else 30 s) pauses every poll.
 *   EdgePoll.ws                       the ONE alert websocket (/api/v1/events/ws)
 *       .connected                    true while open
 *       .on(fn)                       fn(message) for every alert; also the window
 *                                     event 'edge:alert' (detail = message)
 *       window event 'edge:alert-ws'  {connected} when it opens or closes
 *
 * Loaded right after auth.js (not deferred), so its fetch wrapper sits on top
 * of the token-adding one and every later script can use it.
 */
(function () {
  'use strict';

  const DEFAULT_BACKOFF_MS = 30000;
  const MAX_BACKOFF_MS = 5 * 60 * 1000;
  let backoffUntil = 0;

  // ---------------------------------------------------------------- 429 back-off
  const realFetch = window.fetch ? window.fetch.bind(window) : null;
  function isApi(input) {
    try {
      const u = new URL(typeof input === 'string' ? input : (input && input.url) || '', location.href);
      return u.origin === location.origin && u.pathname.startsWith('/api/');
    } catch (_) { return false; }
  }
  function retryAfterMs(res) {
    const h = res.headers && res.headers.get && res.headers.get('Retry-After');
    if (!h) return DEFAULT_BACKOFF_MS;
    const s = Number(h);
    if (Number.isFinite(s)) return Math.min(MAX_BACKOFF_MS, Math.max(1000, s * 1000));
    const t = Date.parse(h);
    return Number.isFinite(t) ? Math.min(MAX_BACKOFF_MS, Math.max(1000, t - Date.now())) : DEFAULT_BACKOFF_MS;
  }
  if (realFetch) {
    window.fetch = function (input, init) {
      return realFetch(input, init).then((res) => {
        if (res && res.status === 429 && isApi(input)) {
          const until = Date.now() + retryAfterMs(res);
          if (until > backoffUntil) {
            backoffUntil = until;
            try { window.dispatchEvent(new CustomEvent('edge:backoff', { detail: { until } })); } catch (_) { /* old browser */ }
          }
        }
        return res;
      });
    };
  }
  const inBackoff = () => Date.now() < backoffUntil;

  // ---------------------------------------------------------------- shared GET
  const cache = new Map();   // url -> {at, data, pending}
  function get(url, maxAgeMs) {
    const now = Date.now();
    const hit = cache.get(url);
    if (hit && hit.pending) return hit.pending;
    if (hit && maxAgeMs && now - hit.at < maxAgeMs) return Promise.resolve(hit.data);
    if (inBackoff() && hit) return Promise.resolve(hit.data);
    const entry = hit || { at: 0, data: null, pending: null };
    entry.pending = fetch(url).then((r) => (r.ok ? r.json() : null)).catch(() => null).then((data) => {
      entry.pending = null;
      if (data !== null) { entry.at = Date.now(); entry.data = data; }
      return data !== null ? data : (entry.data || null);
    });
    cache.set(url, entry);
    return entry.pending;
  }
  function put(url, data) { cache.set(url, { at: Date.now(), data, pending: null }); }
  function forget(url) { cache.delete(url); }

  // ---------------------------------------------------------------- the alert websocket
  const ws = { connected: false, sock: null, tries: 0, timer: null, listeners: [], started: false };
  function wsUrl() { return `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/api/v1/events/ws`; }
  function setConnected(v) {
    if (ws.connected === v) return;
    ws.connected = v;
    try { window.dispatchEvent(new CustomEvent('edge:alert-ws', { detail: { connected: v } })); } catch (_) { /* old browser */ }
  }
  function wsConnect() {
    if (ws.sock || typeof WebSocket === 'undefined') return;
    let s;
    try { s = new WebSocket(wsUrl()); } catch (_) { wsLater(); return; }
    ws.sock = s;
    s.onopen = () => { ws.tries = 0; setConnected(true); };
    s.onmessage = (m) => {
      let msg = null;
      try { msg = JSON.parse(m.data); } catch (_) { return; }
      if (!msg || typeof msg !== 'object') return;
      ws.listeners.slice().forEach((fn) => { try { fn(msg); } catch (e) { console.warn('alert listener failed', e); } });
      try { window.dispatchEvent(new CustomEvent('edge:alert', { detail: msg })); } catch (_) { /* old browser */ }
    };
    s.onclose = (ev) => {
      ws.sock = null;
      setConnected(false);
      if (ev && ev.code === 1008) ws.tries = Math.max(ws.tries, 4);   // not signed in: retry slowly
      wsLater();
    };
    s.onerror = () => { try { s.close(); } catch (_) { /* closing */ } };
  }
  function wsLater() {
    clearTimeout(ws.timer);
    const wait = Math.min(60000, 1000 * Math.pow(2, ws.tries));
    ws.tries += 1;
    ws.timer = setTimeout(wsConnect, wait);
  }
  const wsApi = {
    get connected() { return ws.connected; },
    start() {
      if (ws.started) return;
      ws.started = true;
      const signedIn = !(window.edgeAuth && typeof window.edgeAuth.isAuthenticated === 'function') || window.edgeAuth.isAuthenticated();
      if (signedIn) wsConnect();
    },
    on(fn) { if (typeof fn === 'function') { ws.listeners.push(fn); wsApi.start(); } },
  };

  // ---------------------------------------------------------------- timers
  const polls = new Set();
  function every(ms, fn, opts) {
    const o = opts || {};
    const p = { ms, fn, o, timer: null, busy: false, last: 0, stopped: false };
    const interval = () => {
      if (document.hidden) return o.hiddenMs || null;
      if (o.wsMs && ws.connected) return typeof o.wsMs === 'function' ? o.wsMs() : o.wsMs;
      return typeof p.ms === 'function' ? p.ms() : p.ms;
    };
    const allowed = () => !inBackoff() && (!document.hidden || o.hiddenMs) && (!o.when || o.when());
    p.run = async (force) => {
      if (p.stopped || p.busy) return;
      if (!force && !allowed()) return;
      p.busy = true;
      p.last = Date.now();
      try { await p.fn(); } catch (e) { /* a failed poll waits for the next tick */ } finally { p.busy = false; }
    };
    p.arm = () => {
      clearTimeout(p.timer);
      if (p.stopped) return;
      const iv = interval();
      if (!iv) return;                          // hidden and paused: visibilitychange re-arms
      const wait = Math.max(250, iv - (Date.now() - p.last), inBackoff() ? backoffUntil - Date.now() : 0);
      p.timer = setTimeout(async () => { await p.run(false); p.arm(); }, wait);
    };
    p.kick = () => { p.run(true).then(p.arm); };
    p.stop = () => { p.stopped = true; clearTimeout(p.timer); polls.delete(p); };
    polls.add(p);
    if (o.immediate) p.kick(); else { p.last = Date.now(); p.arm(); }
    return { stop: p.stop, kick: p.kick };
  }
  const rearmAll = () => polls.forEach((p) => p.arm());
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { rearmAll(); return; }
    // Back on screen: anything overdue runs now, the rest keeps its rhythm.
    polls.forEach((p) => {
      const iv = p.o.wsMs && ws.connected ? (typeof p.o.wsMs === 'function' ? p.o.wsMs() : p.o.wsMs)
        : (typeof p.ms === 'function' ? p.ms() : p.ms);
      if (Date.now() - p.last >= iv && (!p.o.when || p.o.when())) p.kick(); else p.arm();
    });
  });
  window.addEventListener('edge:alert-ws', rearmAll);
  window.addEventListener('edge:backoff', rearmAll);

  window.EdgePoll = {
    every, get, put, forget, ws: wsApi,
    inBackoff, backoffUntil: () => backoffUntil,
    _test: { retryAfterMs, polls },
  };
})();
