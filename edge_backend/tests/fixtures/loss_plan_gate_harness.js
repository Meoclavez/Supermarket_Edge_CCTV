// Runs static/js/loss.js in a tiny fake browser and counts its /api/v1/theft
// requests for an iKorex plan with and without theft alerts. Prints one JSON line.
// Used by tests/test_loss_plan_gate.py.
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const staticDir = process.argv[2];
const src = fs.readFileSync(path.join(staticDir, 'js', 'loss.js'), 'utf8');

function makeEl(id) {
  return {
    id, style: {}, dataset: {}, hidden: false, innerHTML: '', textContent: '',
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    querySelector() { return null; }, querySelectorAll() { return []; },
    addEventListener() {}, setAttribute() {}, closest() { return null; },
  };
}

// fleet: undefined (no fleet.js on the page) or { status } where status may be null (not loaded yet).
function run(fleet) {
  const theft = [];
  const listeners = {};
  const els = {};
  const sandbox = {
    console, setTimeout, clearTimeout, Date, Math, JSON, Promise, Set, Map,
    CSS: { escape: (v) => String(v) },
    CustomEvent: function CustomEvent(type, init) { this.type = type; this.detail = init && init.detail; },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    dispatchEvent(ev) { (listeners[ev.type] || []).forEach((fn) => fn(ev)); return true; },
    requestAnimationFrame: () => 0,
    localStorage: { getItem() { return null; }, setItem() {} },
    canPoll: () => true,
    getJSON(url) { if (String(url).includes('/api/v1/theft')) theft.push(url); return Promise.resolve(null); },
    fetch(url) {
      if (String(url).includes('/api/v1/theft')) theft.push(url);
      return Promise.resolve({ ok: false, status: 403, json: () => Promise.resolve({}) });
    },
    edgeAuth: { onReady(fn) { fn(); } },
    document: {
      hidden: false, readyState: 'complete', documentElement: {},
      addEventListener() {},
      getElementById: (id) => (els[id] = els[id] || makeEl(id)),
      querySelector: () => null, querySelectorAll: () => [],
      createElement: () => makeEl(''),
    },
  };
  sandbox.window = sandbox;
  if (fleet) sandbox.edgeFleet = { status: () => fleet.status };
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: 'loss.js' });
  return {
    theft,
    plan(features) {
      if (fleet) fleet.status = { features };
      sandbox.dispatchEvent(new sandbox.CustomEvent('edge:fleet', { detail: fleet && fleet.status }));
    },
    refresh() { return sandbox.edgeLoss.refresh(); },
  };
}

const tick = () => new Promise((r) => setTimeout(r, 30));

async function main() {
  const out = {};

  // Plan known before init, theft alerts left out: nothing, even on a manual refresh.
  const a = run({ status: { features: { theft_detection: false } } });
  await tick();
  await a.refresh();
  await tick();
  out.off_known = a.theft.length;
  // The plan adds theft alerts back: loads straight away.
  a.plan({ theft_detection: true });
  await tick();
  out.off_then_on = a.theft.length;

  // Plan not loaded yet: waits; the plan then arrives without theft alerts.
  const b = run({ status: null });
  await tick();
  out.unknown_before = b.theft.length;
  b.plan({ theft_detection: false });
  await tick();
  out.unknown_then_off = b.theft.length;

  // Plan not loaded yet, then arrives with theft alerts.
  const c = run({ status: null });
  c.plan({ theft_detection: true });
  await tick();
  out.unknown_then_on = c.theft.length;

  // No fleet.js on the page (old pages, tests): loads at once as before.
  const d = run(undefined);
  await tick();
  out.no_fleet = d.theft.length;

  process.stdout.write(JSON.stringify(out) + '\n');
  process.exit(0);
}

main().catch((e) => { process.stderr.write(String(e && e.stack || e) + '\n'); process.exit(1); });
