// Runs the tiered-alert parts of static/js/loss.js, live_overlay.js and
// live_behaviour.js in a tiny fake browser (no server, no real browser) and
// prints one JSON line of results. Used by tests/test_theft_tier_ui.py.
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const staticDir = process.argv[2];

function makeEl(id) {
  const classes = new Set();
  return {
    id,
    style: {},
    dataset: {},
    hidden: false,
    innerHTML: '',
    textContent: '',
    title: '',
    classList: {
      toggle(c, on) { if (on === undefined ? !classes.has(c) : on) classes.add(c); else classes.delete(c); },
      add(c) { classes.add(c); },
      remove(c) { classes.delete(c); },
      contains(c) { return classes.has(c); },
      list() { return [...classes].sort(); },
    },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    addEventListener() {},
    setAttribute() {},
  };
}

function browser() {
  const els = {};
  const sounds = [];
  const sandbox = {
    console,
    setTimeout,
    clearTimeout,
    Date,
    Math,
    JSON,
    Promise,
    Set,
    Map,
    CSS: { escape: (v) => String(v) },
    AudioContext: function AudioContext() {
      this.currentTime = 0;
      this.destination = {};
      this.createOscillator = () => {
        sounds.push('beep');
        return { type: '', frequency: { setValueAtTime() {}, exponentialRampToValueAtTime() {} }, connect() {}, start() {}, stop() {} };
      };
      this.createGain = () => ({ gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} }, connect() {} });
    },
    CustomEvent: function CustomEvent(type, init) { this.type = type; this.detail = init && init.detail; },
    dispatchEvent() { return true; },
    addEventListener() {},
    requestAnimationFrame: () => 0,
    cancelAnimationFrame: () => {},
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    localStorage: { getItem() { return null; }, setItem() {} },
    fetch: () => Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({}) }),
    // Never starts polling: init waits for a sign-in that does not come here.
    edgeAuth: { onReady() {} },
    document: {
      hidden: false,
      readyState: 'complete',
      documentElement: {},
      addEventListener() {},
      getElementById: (id) => (els[id] = els[id] || makeEl(id)),
      querySelector: () => null,
      querySelectorAll: () => [],
      createElement: () => makeEl(''),
    },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  for (const f of ['icons.js', 'loss.js', 'live_overlay.js', 'live_behaviour.js']) {
    vm.runInContext(fs.readFileSync(path.join(staticDir, 'js', f), 'utf8'), sandbox, { filename: f });
  }
  return { sandbox, els, sounds };
}

const ch = (o) => Object.assign({ queue: true, banner: false, sound: false, push: false, escalate: false, repeat: false }, o);
const inc = (id, tier, minutesAgo, extra) => Object.assign({
  id,
  status: 'ACTIVE',
  rule: 'CONCEALMENT',
  rule_label: 'Possible concealment',
  camera_id: 'cam1',
  camera_name: 'Aisle 4',
  confidence: 0.5,
  timestamp: new Date(Date.now() - minutesAgo * 60000).toISOString(),
  alert_tier: tier,
  alert_tier_label: tier ? tier.charAt(0).toUpperCase() + tier.slice(1) : null,
  risk_score: tier ? 0.62 : null,
  risk_factors: tier ? [
    { factor: 'confidence', value: 0.5, effect: 'base', reason: 'The camera saw it with 50% confidence.' },
    { factor: 'high_value', value: 1.25, effect: 'x1.25', reason: 'Spirits is a high-value product area.' },
  ] : [],
  alert_channels: tier === 'review' ? ch({}) : tier === 'watch' ? ch({ banner: true })
    : tier === 'alert' ? ch({ banner: true, sound: true, push: true })
      : tier === 'critical' ? ch({ banner: true, sound: true, push: true, escalate: true, repeat: true }) : null,
}, extra || {});

function main() {
  const out = {};
  const b = browser();
  const L = b.sandbox.edgeLoss._test;
  const st = L.state;
  st.loaded = true;

  // ---- gating per level
  out.gates = ['review', 'watch', 'alert', 'critical', null].map((t) => {
    const i = inc(`g-${t}`, t, 1);
    return { tier: t, banner: L.bannerAllowed(i), sound: L.soundAllowed(i) };
  });

  // ---- a Review incident alone: no banner, no sound
  st.incidents = [inc('r1', 'review', 1)];
  L.ringForNew();
  L.renderBanner();
  out.review_only = { sounds: b.sounds.length, banner: b.els.theftAlertBanner.style.display };

  // ---- a Watch incident: banner, still no sound, watch look
  st.incidents = [inc('w1', 'watch', 1), inc('r1', 'review', 1)];
  L.ringForNew();
  L.renderBanner();
  out.watch = {
    sounds: b.sounds.length,
    banner: b.els.theftAlertBanner.style.display,
    classes: b.els.theftAlertBanner.classList.list(),
    headline: b.els.theftBannerHeadline.textContent,
    meta: b.els.theftBannerConfidence.textContent,
  };

  // ---- an older Critical and a newer Alert: Critical leads the banner; sounds for both, three beeps for critical
  st.incidents = [inc('a1', 'alert', 1), inc('c1', 'critical', 5), inc('w1', 'watch', 1), inc('r1', 'review', 1)];
  const before = b.sounds.length;
  L.ringForNew();
  L.renderBanner();
  out.critical = {
    new_beeps: b.sounds.length - before,
    banner_incident: b.els.theftAlertBanner.dataset.incident,
    classes: b.els.theftAlertBanner.classList.list(),
    headline: b.els.theftBannerHeadline.textContent,
  };
  // The same queue again: nothing new, no sound.
  const again = b.sounds.length;
  L.ringForNew();
  out.repeat_poll_beeps = b.sounds.length - again;

  // ---- an incident without a level (older row, null channels): banner and sound as before
  const b2 = browser();
  const L2 = b2.sandbox.edgeLoss._test;
  L2.state.loaded = true;
  L2.state.incidents = [inc('old1', null, 1)];
  L2.ringForNew();
  L2.renderBanner();
  out.untiered = {
    sounds: b2.sounds.length,
    banner: b2.els.theftAlertBanner.style.display,
    classes: b2.els.theftAlertBanner.classList.list(),
    headline: b2.els.theftBannerHeadline.textContent,
  };

  // ---- queue: waiting for review sorts Critical first; level filter URL
  st.filter = 'open';
  st.tierFilter = 'all';
  out.open_order = L.filtered().map((i) => i.id);
  out.tier_url = L.tierUrl('critical');

  // ---- card: tier badge, risk, "Why this level"; old row says "No level"
  const card = L.cardHtml(inc('c1', 'critical', 5));
  const oldCard = L.cardHtml(inc('old1', null, 1));
  out.card = {
    badge: card.includes('tier-badge tier-critical'),
    risk: card.includes('risk 62%'),
    why: card.includes('Why this level') && card.includes('Spirits is a high-value product area.') && card.includes('x1.25'),
    route: card.includes('repeated until acknowledged'),
    old_badge: oldCard.includes('loss-tier-none') && oldCard.includes('No level'),
    old_why: oldCard.includes('Why this level'),
  };

  // ---- KPI row by level
  st.stats = {
    by_tier: { review: 4, watch: 3, alert: 2, critical: 1, unclassified: 6 },
    today_by_tier: { review: 1, watch: 1, alert: 1, critical: 1, unclassified: 0 },
    active_by_tier: { review: 2, watch: 1, alert: 1, critical: 1, unclassified: 3 },
    false_alarm_rate_by_tier: { review: 0.5, watch: null, alert: 0.25, critical: null, unclassified: 0.1 },
  };
  L.renderTierKpis();
  const k = b.els.lossTierKpis.innerHTML;
  out.kpis = {
    order: [...k.matchAll(/data-tier-kpi="(\w+)"/g)].map((m) => m[1]),
    alert_rate: k.includes('25% false alarms among reviewed'),
    no_outcomes: k.includes('no outcomes recorded yet'),
    unclassified: k.includes('6 older incidents were recorded before alert levels'),
  };

  // ---- pattern mix
  out.mix = L.tierMix({ review: 0, watch: 2, alert: 0, critical: 1, unclassified: 3 });
  out.mix_none = L.tierMix(null);

  // ---- live overlay: tier colour and chip
  const T = b.sandbox.EdgeLiveOverlay._test;
  const trk = (level, tier) => ({ motion_state: 'moving', behaviour: { level, tier, labels: ['Hand at pocket'] } });
  out.overlay = {
    tiers: [T.tierOf(trk('alert', 'critical')), T.tierOf(trk('alert', null)), T.tierOf(trk('watch', 'alert'))],
    colors: [T.colorKey(trk('alert', 'critical')), T.colorKey(trk('alert', 'watch')), T.colorKey(trk('alert', null)), T.colorKey(trk('watch', null))],
    chips: [T.chipText(trk('alert', 'critical')), T.chipText(trk('alert', 'watch')), T.chipText(trk('alert', null))],
  };

  // ---- live behaviour: tier from the row, else from the review queue; none at "watch"
  const B = b.sandbox.edgeLiveBehaviour._test;
  st.incidents = [inc('c1', 'critical', 5)];
  out.behaviour = {
    own: B.tierInfo({ level: 'alert', tier: 'alert', risk_score: 0.6 }),
    from_queue: B.tierInfo({ level: 'alert', incident_id: 'c1' }),
    watch: B.tierInfo({ level: 'watch', tier: 'alert' }),
    unknown: B.tierInfo({ level: 'alert', incident_id: 'nope' }),
    row: B.rowHtml({ camera_id: 'cam1', camera_name: 'Aisle 4', track_id: 't1', level: 'alert', incident_id: 'c1', labels: ['Possible concealment'] }),
  };

  console.log(JSON.stringify(out));
  process.exit(0);
}

try { main(); } catch (e) { console.error(e && e.stack || e); process.exit(1); }
