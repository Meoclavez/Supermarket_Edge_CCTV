// Node harness for static/js/auth.js (run by tests/test_frontend_auth_gate.py).
//
// Loads auth.js into a minimal fake browser, lets "dashboard modules" fire
// API polls immediately (as analytics.js & co. did on DOMContentLoaded), and
// records every request that actually reaches the network. Prints one JSON
// object per scenario.
//
// usage: node auth_gate_harness.js <path/to/auth.js> <scenario>
//   scenario: stale | valid | offline
'use strict';

const fs = require('fs');
const vm = require('vm');

const [, , authPath, scenario] = process.argv;
const src = fs.readFileSync(authPath, 'utf8');

function fakeElement(id) {
  const el = {
    id: id || '', style: {}, className: '', textContent: '', value: '', disabled: false,
    children: [], _html: '',
    set innerHTML(v) { this._html = String(v); },
    get innerHTML() { return this._html; },
    appendChild(c) { this.children.push(c); if (c && c.id) registry[c.id] = c; return c; },
    addEventListener() {}, removeEventListener() {}, setAttribute() {}, focus() {}, remove() {},
    querySelector(sel) {
      // Only the gate's own form fields: exist once innerHTML was rendered.
      if (!this._html) return null;
      const key = sel.replace(/^#/, '');
      return registry[`__gate_${key}`] || (registry[`__gate_${key}`] = fakeElement(key));
    },
    querySelectorAll() { return []; },
    classList: { add() {}, remove() {}, contains() { return false; }, toggle() {} },
  };
  return el;
}
const registry = {};

const storage = {};
const cookies = [];
const network = [];           // requests that left the page: {path, auth}
let settledState = null;
const listeners = {};

const statusReplies = {
  stale: { admin_exists: true, authenticated: false, setup_code_required: false, debug_bypass_active: false },
  valid: { admin_exists: true, authenticated: true, setup_code_required: false, debug_bypass_active: false },
};

async function nativeFetch(input, init) {
  const url = typeof input === 'string' ? input : input.url;
  const path = new URL(url, 'http://edge.local/dashboard').pathname;
  let auth = null;
  const h = init && init.headers;
  if (h) auth = typeof h.get === 'function' ? h.get('Authorization') : (h.Authorization || null);
  network.push({ path, auth, t: network.length });
  if (scenario === 'offline') throw new TypeError('Failed to fetch');
  if (path === '/api/v1/auth/status') {
    await new Promise((r) => setTimeout(r, 30));   // the check takes a moment
    return new Response(JSON.stringify(statusReplies[scenario]), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }
  return new Response(JSON.stringify({ ok: true }), { status: scenario === 'valid' ? 200 : 401 });
}

const document = {
  readyState: 'complete',
  body: fakeElement('body'),
  getElementById: (id) => registry[id] || null,
  createElement: () => fakeElement(),
  querySelector: () => null,
  addEventListener() {},
  set cookie(v) { cookies.push(v); },
  get cookie() { return ''; },
};

const window = {
  fetch: nativeFetch,
  location: { href: 'http://edge.local/dashboard', origin: 'http://edge.local', protocol: 'http:', reload() {} },
  addEventListener(name, fn) { (listeners[name] = listeners[name] || []).push(fn); },
  dispatchEvent(ev) {
    if (ev.type === 'edge:auth') settledState = ev.detail.state;
    (listeners[ev.type] || []).forEach((fn) => fn(ev));
    return true;
  },
};

const sandbox = {
  window, document, console,
  localStorage: {
    getItem: (k) => (k in storage ? storage[k] : null),
    setItem: (k, v) => { storage[k] = String(v); },
    removeItem: (k) => { delete storage[k]; },
  },
  setTimeout, clearTimeout, Promise, URL, Headers, Response, AbortController,
  CustomEvent: class { constructor(type, init) { this.type = type; this.detail = (init || {}).detail; } },
  JSON,
};
sandbox.self = window;
sandbox.location = window.location;
vm.createContext(sandbox);

(async () => {
  storage.edge_cctv_token = 'stored.jwt.token';
  vm.runInContext(src, sandbox, { filename: 'auth.js' });

  // Dashboard modules polling straight away, before the check has answered.
  const early = ['/api/v1/layout', '/api/v1/system/hardware', '/api/v1/system/stats', '/api/v1/layout/devices',
    '/api/v1/cameras', '/api/v1/analytics/overview', '/api/v1/layout/pipeline/status', '/api/v1/setup/status']
    .map((p) => window.fetch(p).then((r) => r.status).catch(() => 'error'));
  const beforeResolve = network.map((n) => n.path);
  const statuses = await Promise.all(early);
  const readyState = await window.edgeAuth.ready;

  process.stdout.write(JSON.stringify({
    scenario,
    sent_before_resolve: beforeResolve,
    network: network.map((n) => ({ path: n.path, auth: n.auth })),
    statuses,
    state: readyState,
    event_state: settledState,
    token_left: storage.edge_cctv_token || null,
    cookie_cleared: cookies.some((c) => /edge_cctv_token=;.*max-age=0/.test(c)),
    gate_shown: !!(registry.authGate && registry.authGate.style.display === 'flex'),
    is_authenticated: window.edgeAuth.isAuthenticated(),
    auth_url: window.edgeAuth.authUrl('/stream?camera_id=c1'),
  }) + '\n');
})().catch((e) => { console.error(e); process.exit(2); });
