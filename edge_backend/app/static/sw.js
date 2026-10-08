// Service worker for the installed dashboard web app (served at /sw.js, scope /).
//
// It does two things only:
//  1. Phone alerts (Web Push): shows each message the box sends
//     (services/push_alerts.py payload: kind, title, body, tag, url, ack...),
//     and handles taps: "Acknowledge" POSTs /api/v1/web-push/ack with the per-phone
//     token in the message; any other tap opens the dashboard at the incident.
//  2. A plain "can't reach the store" page when a navigation fails offline.
// It caches nothing: every page and figure always comes live from the box.
'use strict';

const VERSION = 'edge-sw-1';
const ICON = '/static/icons/icon-192.png';
const BADGE = '/static/icons/badge-96.png';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

function parse(event) {
  if (!event.data) return null;
  try { return event.data.json(); } catch (_) {
    try { return { title: 'Store alert', body: event.data.text() }; } catch (__) { return null; }
  }
}

function options(msg) {
  const urgent = msg.kind === 'alert' || msg.kind === 'escalation' || msg.kind === 'offline';
  const opts = {
    body: msg.body || '',
    tag: msg.tag || msg.alert_id || 'edge-alert',
    icon: ICON,
    badge: BADGE,
    data: { url: msg.url || '/dashboard', alert_id: msg.alert_id || null, ack: msg.ack || null, kind: msg.kind },
    timestamp: msg.ts ? msg.ts * 1000 : Date.now(),
    // An alert stays until someone acts on it; notices (handled, back online,
    // test) replace or sit quietly.
    requireInteraction: urgent,
    renotify: urgent,
    silent: msg.kind === 'handled',
  };
  if (urgent) opts.vibrate = [400, 150, 400, 150, 400];
  if (msg.ack && msg.ack.token && (msg.kind === 'alert' || msg.kind === 'escalation')) {
    opts.actions = [{ action: 'ack', title: 'Acknowledge' }, { action: 'open', title: 'Open' }];
  }
  return opts;
}

self.addEventListener('push', (event) => {
  // Safari ends the subscription if a push shows nothing, so always show one.
  const msg = parse(event) || { title: 'Store alert', body: 'Open the dashboard for details.' };
  event.waitUntil((async () => {
    await self.registration.showNotification(msg.title || 'Store alert', options(msg));
    // Tell open dashboards (they refresh their lists); badge the app icon.
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    wins.forEach((w) => w.postMessage({ type: 'edge-push', kind: msg.kind, alert_id: msg.alert_id }));
    if (self.navigator && self.navigator.setAppBadge && (msg.kind === 'alert' || msg.kind === 'escalation')) {
      try { await self.navigator.setAppBadge(); } catch (_) { /* not supported */ }
    }
  })());
});

async function openApp(url) {
  const target = new URL(url || '/dashboard', self.location.origin).href;
  const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  for (const w of wins) {
    if (new URL(w.url).origin === self.location.origin && 'focus' in w) {
      try { await w.navigate(target); } catch (_) { /* navigate refused: focus is enough */ }
      return w.focus();
    }
  }
  return self.clients.openWindow(target);
}

async function acknowledge(data) {
  const res = await fetch('/api/v1/web-push/ack', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ alert_id: data.alert_id, sid: data.ack.sid, token: data.ack.token }),
    credentials: 'omit',
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

self.addEventListener('notificationclick', (event) => {
  const n = event.notification;
  const data = n.data || {};
  n.close();
  event.waitUntil((async () => {
    if (self.navigator && self.navigator.clearAppBadge) {
      try { await self.navigator.clearAppBadge(); } catch (_) { /* not supported */ }
    }
    if (event.action === 'ack' && data.ack && data.alert_id) {
      try {
        const r = await acknowledge(data);
        await self.registration.showNotification('Acknowledged', {
          body: `${r.acknowledged_by || 'You'} acknowledged: ${n.title}`,
          tag: n.tag, icon: ICON, badge: BADGE, silent: true, data: { url: data.url },
        });
        return;
      } catch (_) {
        // No connection to the store, or the alert is gone: open it instead.
      }
    }
    return openApp(data.url);
  })());
});

const OFFLINE_PAGE = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Store not reachable</title>
<style>body{font-family:system-ui,sans-serif;background:#0b0f18;color:#e6edf6;display:flex;min-height:100vh;
align-items:center;justify-content:center;margin:0;padding:24px;text-align:center}
main{max-width:420px}h1{font-size:20px}p{color:#9fb0c8;line-height:1.5}
button{margin-top:12px;padding:10px 18px;border-radius:8px;border:0;background:#00b7c7;color:#001;font-weight:700}</style>
</head><body><main><h1>Can't reach the store</h1>
<p>This phone has no internet connection, or the store's CCTV box is offline.
Alerts already sent to this phone stay in your notifications.</p>
<button onclick="location.reload()">Try again</button></main></body></html>`;

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.mode !== 'navigate' || req.method !== 'GET') return;  // everything else: straight to the network
  event.respondWith(fetch(req).catch(() => new Response(OFFLINE_PAGE, {
    status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' },
  })));
});

self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'edge-version' && event.ports && event.ports[0]) {
    event.ports[0].postMessage({ version: VERSION });
  }
});
