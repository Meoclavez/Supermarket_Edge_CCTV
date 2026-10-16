/**
 * Camera still pictures, kept in this browser only (docs/STORE_MAP_V2_CONTRACT.md, stream B).
 *
 * One small picture per camera, captured once on demand and cached in
 * IndexedDB (database "edge-stills"). Nothing here polls, and nothing is
 * ever uploaded: the pictures come from the box and stay in this browser.
 *
 *  - Store network: GET /api/v1/cameras/{id}/snapshot?annotate=false&max_width=480
 *    through the authenticated fetch (auth.js adds the bearer token).
 *    Privacy masks are burned in by the box. A "no signal" slate is not kept.
 *  - Public address (WebRtcLive config transport 'webrtc'): one frame over a
 *    direct video connection (WebRtcLive.grabFrame). The snapshot endpoint is
 *    never called that way (the box answers 403 video_direct_only).
 *  - Strictly one capture at a time. When the page is hidden the running
 *    capture is stopped and the waiting ones are dropped.
 *  - Disabled cameras are never captured.
 *  - The cache belongs to the signed-in user: it is wiped on sign-out
 *    (event 'edge:signed-out' from auth.js), when the sign-in gate shows at
 *    start-up, and when a different user signs in.
 *
 * API (window.StillCache):
 *   ready                         Promise, resolves once the cache is loaded
 *   peek(cameraId)                -> {camera_id, url, captured_at, width, height, source, stale} | null (sync)
 *   capture(cameraId, {force, enabled, signal}) -> Promise<entry>   (rejects with err.code)
 *   pending(cameraId)             -> 'queued' | 'running' | null
 *   lastError(cameraId)           -> {code, message} | null
 *   refreshAll(cameraIds, {signal, onProgress}) -> Promise<{done, failed, stopped, reason}>
 *   transport()                   -> Promise<'local' | 'webrtc' | 'unknown'>
 *   isStale(entry), STALE_MS
 *   clear()                       -> Promise (wipes this browser's pictures)
 * Event on window: 'edge:still-updated' detail {camera_id, state: 'queued'|'running'|'done'|'error'|'cleared'}
 */
(function () {
  'use strict';

  const DB_NAME = 'edge-stills';
  const STORE = 'stills';
  const DB_VERSION = 1;
  const OWNER_KEY = 'edge-stills-owner';
  const WIPE_KEY = 'edge-stills-wipe';
  const MAX_WIDTH = 480;
  const STALE_MS = 24 * 60 * 60 * 1000;
  const REMOTE_TIMEOUT_MS = 20000;

  // ------------------------------------------------------------ small helpers

  function coded(code, message, detail) {
    const e = new Error(message);
    e.code = code;
    e.detail = detail || '';
    return e;
  }

  function lsGet(k) { try { return window.localStorage.getItem(k); } catch (_) { return null; } }
  function lsSet(k, v) { try { window.localStorage.setItem(k, v); } catch (_) { /* storage blocked */ } }
  function lsDel(k) { try { window.localStorage.removeItem(k); } catch (_) { /* storage blocked */ } }

  function emit(cameraId, state) {
    try { window.dispatchEvent(new CustomEvent('edge:still-updated', { detail: { camera_id: cameraId, state } })); } catch (_) { /* old browser */ }
  }

  // ------------------------------------------------------------ IndexedDB (memory fallback)

  let dbPromise = null;
  let memoryOnly = typeof window.indexedDB === 'undefined';

  function openDb() {
    if (memoryOnly) return Promise.reject(coded('no_idb', 'IndexedDB unavailable'));
    if (dbPromise) return dbPromise;
    dbPromise = new Promise((resolve, reject) => {
      let req;
      try { req = window.indexedDB.open(DB_NAME, DB_VERSION); } catch (e) { reject(e); return; }
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE, { keyPath: 'key' });
      };
      req.onsuccess = () => {
        const db = req.result;
        db.onversionchange = () => { try { db.close(); } catch (_) {} dbPromise = null; };
        resolve(db);
      };
      req.onerror = () => reject(req.error || coded('idb', 'IndexedDB open failed'));
      req.onblocked = () => reject(coded('idb_blocked', 'IndexedDB is blocked'));
    }).catch((e) => {
      // Private mode or blocked storage: keep pictures for this visit only.
      memoryOnly = true;
      dbPromise = null;
      throw e;
    });
    return dbPromise;
  }

  function txDone(tx) {
    return new Promise((resolve, reject) => {
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error || coded('idb_abort', 'IndexedDB transaction aborted'));
    });
  }

  async function dbGetAll() {
    if (memoryOnly) return [];
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readonly');
      const req = tx.objectStore(STORE).getAll();
      req.onsuccess = () => resolve(req.result || []);
      req.onerror = () => reject(req.error);
    });
  }

  async function dbPut(rec) {
    if (memoryOnly) return;
    const db = await openDb();
    const tx = db.transaction(STORE, 'readwrite');
    tx.objectStore(STORE).put(rec);
    await txDone(tx);
  }

  async function dbDelete(key) {
    if (memoryOnly) return;
    const db = await openDb();
    const tx = db.transaction(STORE, 'readwrite');
    tx.objectStore(STORE).delete(key);
    await txDone(tx);
  }

  // Delete the whole database. Issued synchronously so it is already queued
  // by the browser when auth.js reloads the page right after signing out.
  function dbWipe() {
    return new Promise((resolve) => {
      const finish = () => { lsDel(WIPE_KEY); resolve(); };
      const doDelete = () => {
        let req;
        try { req = window.indexedDB.deleteDatabase(DB_NAME); } catch (_) { finish(); return; }
        req.onsuccess = finish;
        req.onerror = () => resolve();      // flag stays: retried on the next load
        req.onblocked = () => { /* waits for the open connection to close */ };
      };
      if (typeof window.indexedDB === 'undefined') { finish(); return; }
      if (dbPromise) {
        const p = dbPromise;
        dbPromise = null;
        p.then((db) => { try { db.close(); } catch (_) {} doDelete(); }, doDelete);
      } else {
        doDelete();
      }
    });
  }

  // ------------------------------------------------------------ in-memory index

  // camera_id -> {rec, url}
  const entries = new Map();
  const errors = new Map();
  let owner = null;

  function keyFor(cameraId) { return `${owner}|${cameraId}`; }

  function revoke(cameraId) {
    const e = entries.get(cameraId);
    if (e && e.url) { try { URL.revokeObjectURL(e.url); } catch (_) {} }
  }

  function dropAllMemory() {
    entries.forEach((_, id) => revoke(id));
    const ids = Array.from(entries.keys());
    entries.clear();
    errors.clear();
    ids.forEach((id) => emit(id, 'cleared'));
  }

  function isStale(entry) {
    return !!entry && (Date.now() - Number(entry.captured_at || 0)) > STALE_MS;
  }

  function peek(cameraId) {
    const e = entries.get(String(cameraId));
    if (!e) return null;
    if (!e.url) { try { e.url = URL.createObjectURL(e.rec.blob); } catch (_) { return null; } }
    return {
      camera_id: e.rec.camera_id,
      url: e.url,
      captured_at: e.rec.captured_at,
      width: e.rec.width,
      height: e.rec.height,
      source: e.rec.source,
      stale: isStale(e.rec),
    };
  }

  // ------------------------------------------------------------ owner (who is signed in)

  function currentOwner() {
    const auth = window.edgeAuth;
    const st = auth && typeof auth.state === 'function' ? auth.state() : null;
    const acc = window.EdgeAuth;
    if (acc && (acc.id || acc.username)) return `u:${acc.id || acc.username}`;
    if (st === 'bypass') return 'u:local';
    if (st === 'error') return lsGet(OWNER_KEY);     // server unreachable: keep the last user's pictures
    return null;
  }

  async function adoptOwner(next) {
    if (!next) return;
    if (owner === next) return;
    const stored = lsGet(OWNER_KEY);
    if (stored && stored !== next) {
      // A different user signed in on this browser: their pictures are not ours to show.
      cancelQueue('signed-out');
      dropAllMemory();
      await dbWipe();
    }
    owner = next;
    lsSet(OWNER_KEY, next);
  }

  async function signedOut() {
    lsSet(WIPE_KEY, '1');
    lsDel(OWNER_KEY);
    owner = null;
    cancelQueue('signed-out');
    dropAllMemory();
    await dbWipe();
  }

  // ------------------------------------------------------------ transport (store network or public address)

  async function remoteConfig() {
    const W = window.WebRtcLive;
    if (!W || typeof W.config !== 'function') return null;
    try {
      return await W.config();
    } catch (e) {
      // Unknown network: never guess, never call the snapshot endpoint blind.
      throw coded('unknown_network', "Can't reach the box right now", (e && (e.detail || e.message)) || '');
    }
  }

  async function transport() {
    try {
      const c = await remoteConfig();
      if (!c) return 'local';
      return c.transport === 'webrtc' ? 'webrtc' : 'local';
    } catch (_) {
      return 'unknown';
    }
  }

  // The box told us (403 video_direct_only) that this address is the public one.
  let forceRemote = false;

  async function decodeSize(blob) {
    if (typeof window.createImageBitmap === 'function') {
      try {
        const bmp = await window.createImageBitmap(blob);
        const out = { width: bmp.width, height: bmp.height };
        if (typeof bmp.close === 'function') bmp.close();
        return out;
      } catch (_) { /* fall through */ }
    }
    return { width: null, height: null };
  }

  async function captureLocal(cameraId, signal) {
    const url = `/api/v1/cameras/${encodeURIComponent(cameraId)}/snapshot?annotate=false&max_width=${MAX_WIDTH}&_=${Date.now()}`;
    let res;
    try {
      res = await fetch(url, { cache: 'no-store', credentials: 'same-origin', signal });
    } catch (e) {
      if (signal && signal.aborted) throw coded('aborted', 'Cancelled');
      throw coded('network', "Can't reach the box right now", e && e.message);
    }
    if (res.status === 403) {
      let data = {};
      try { data = await res.clone().json(); } catch (_) { /* not JSON */ }
      if (data && data.code === 'video_direct_only') throw coded('direct_only', 'Pictures need direct video on this address');
      throw coded('forbidden', "You can't see this camera's picture", data && data.detail);
    }
    if (res.status === 401) throw coded('auth', 'Sign in again to see pictures');
    if (!res.ok) throw coded('http', "Couldn't get a picture", `HTTP ${res.status}`);
    if ((res.headers.get('X-Frame-Source') || '') === 'no-signal') {
      throw coded('no_signal', 'No picture from this camera right now');
    }
    const blob = await res.blob();
    if (!blob || !blob.size) throw coded('empty', "Couldn't get a picture");
    let width = null;
    let height = null;
    const m = /^(\d+)x(\d+)$/.exec(res.headers.get('X-Frame-Size') || '');
    if (m) { width = Number(m[1]); height = Number(m[2]); } else { ({ width, height } = await decodeSize(blob)); }
    return { blob, width, height, source: 'snapshot' };
  }

  async function captureRemote(cameraId, signal, cfg) {
    const W = window.WebRtcLive;
    if (!W || typeof W.grabFrame !== 'function') throw coded('direct_unavailable', 'Direct video is not available in this browser');
    const c = cfg || (await remoteConfig());
    if (c && c.available === false) {
      throw coded('direct_unavailable', 'Direct video is off, so no picture can be taken', c.reason || '');
    }
    // Respect the box's live-video limit: do not take the last free slot from a live view.
    const max = c && Number.isFinite(c.max_sessions) ? c.max_sessions : null;
    const active = typeof W.activeCount === 'function' ? W.activeCount() : 0;
    if (max !== null && max > 0 && active >= max) {
      throw coded('session_limit', 'Live video limit reached', 'Close a live view, then try again.');
    }
    if (document.hidden) throw coded('hidden', 'Stopped because the page was hidden');
    let blob;
    try {
      blob = await W.grabFrame(cameraId, { maxWidth: MAX_WIDTH, timeoutMs: REMOTE_TIMEOUT_MS, signal, type: 'image/jpeg', quality: 0.82 });
    } catch (e) {
      const code = (e && e.code) || 'error';
      if (code === 'aborted' || (signal && signal.aborted)) throw coded(document.hidden ? 'hidden' : 'aborted', document.hidden ? 'Stopped because the page was hidden' : 'Cancelled');
      throw coded(code, (e && e.message) || "Couldn't get a picture", e && e.detail);
    }
    const size = await decodeSize(blob);
    return { blob, width: size.width, height: size.height, source: 'webrtc' };
  }

  async function captureNow(cameraId, signal) {
    const cfg = await remoteConfig();
    const remote = forceRemote || !!(cfg && cfg.transport === 'webrtc');
    if (remote) return captureRemote(cameraId, signal, cfg);
    try {
      return await captureLocal(cameraId, signal);
    } catch (e) {
      if (e && e.code === 'direct_only') {
        forceRemote = true;
        return captureRemote(cameraId, signal, cfg);
      }
      throw e;
    }
  }

  // ------------------------------------------------------------ the queue (one capture at a time)

  const queue = [];          // {id, resolve, reject, signal}
  const inflight = new Map(); // id -> promise
  let running = null;        // {id, ctl}

  function pending(cameraId) {
    const id = String(cameraId);
    if (running && running.id === id) return 'running';
    return queue.some((j) => j.id === id) ? 'queued' : null;
  }

  function cancelQueue(code) {
    const msg = code === 'hidden' ? 'Stopped because the page was hidden' : 'Cancelled';
    while (queue.length) {
      const j = queue.shift();
      j.reject(coded(code, msg));
      emit(j.id, 'error');
    }
    if (running) { try { running.ctl.abort(); } catch (_) {} }
  }

  async function pump() {
    if (running || !queue.length) return;
    const job = queue.shift();
    if (job.signal && job.signal.aborted) {
      job.reject(coded('aborted', 'Cancelled'));
      emit(job.id, 'error');
      pump();
      return;
    }
    if (document.hidden) {
      job.reject(coded('hidden', 'Stopped because the page was hidden'));
      emit(job.id, 'error');
      pump();
      return;
    }
    const ctl = new AbortController();
    const onAbort = () => ctl.abort();
    if (job.signal) job.signal.addEventListener('abort', onAbort, { once: true });
    running = { id: job.id, ctl };
    emit(job.id, 'running');
    try {
      const shotOwner = owner;
      const shot = await captureNow(job.id, ctl.signal);
      if (!owner || owner !== shotOwner) throw coded('signed-out', 'Signed out');
      const rec = {
        key: keyFor(job.id),
        owner,
        camera_id: job.id,
        blob: shot.blob,
        captured_at: Date.now(),
        width: shot.width,
        height: shot.height,
        source: shot.source,
      };
      revoke(job.id);
      entries.set(job.id, { rec, url: null });
      errors.delete(job.id);
      try { await dbPut(rec); } catch (_) { /* kept in memory for this visit */ }
      emit(job.id, 'done');
      job.resolve(peek(job.id));
    } catch (e) {
      let err = e && e.code ? e : coded('error', (e && e.message) || "Couldn't get a picture");
      if (err.code === 'aborted' && document.hidden) err = coded('hidden', 'Stopped because the page was hidden');
      if (err.code !== 'aborted' && err.code !== 'signed-out' && err.code !== 'hidden') errors.set(job.id, { code: err.code, message: err.message, detail: err.detail || '' });
      emit(job.id, 'error');
      job.reject(err);
    } finally {
      if (job.signal) job.signal.removeEventListener('abort', onAbort);
      running = null;
      pump();
    }
  }

  function capture(cameraId, opts) {
    const o = opts || {};
    const id = String(cameraId);
    if (o.enabled === false) return Promise.reject(coded('off', 'Camera is turned off'));
    if (!owner) return Promise.reject(coded('signed-out', 'Sign in to see pictures'));
    if (!o.force) {
      const have = peek(id);
      if (have) return Promise.resolve(have);
    }
    if (inflight.has(id)) return inflight.get(id);
    const p = new Promise((resolve, reject) => {
      queue.push({ id, resolve, reject, signal: o.signal || null });
    }).finally(() => { inflight.delete(id); });
    inflight.set(id, p);
    emit(id, 'queued');
    pump();
    return p;
  }

  /**
   * Refresh several cameras one after the other. Stops at the first
   * "page hidden", sign-out or live-video-limit error, or when signal aborts.
   */
  async function refreshAll(cameraIds, opts) {
    const o = opts || {};
    const ids = (cameraIds || []).map(String);
    let done = 0;
    let failed = 0;
    const total = ids.length;
    const report = (current) => { if (typeof o.onProgress === 'function') { try { o.onProgress({ done, failed, total, current }); } catch (_) {} } };
    report(null);
    for (const id of ids) {
      if (o.signal && o.signal.aborted) return { done, failed, total, stopped: true, reason: 'aborted' };
      report(id);
      try {
        await capture(id, { force: true, signal: o.signal });
        done += 1;
      } catch (e) {
        const code = e && e.code;
        if (code === 'aborted') return { done, failed, total, stopped: true, reason: 'aborted' };
        if (code === 'hidden' || code === 'signed-out' || code === 'session_limit' || code === 'unknown_network' || code === 'direct_unavailable') {
          failed += 1;
          report(null);
          return { done, failed, total, stopped: true, reason: code, message: e.message, detail: e.detail || '' };
        }
        failed += 1;
      }
      report(null);
    }
    return { done, failed, total, stopped: false, reason: null };
  }

  function lastError(cameraId) {
    return errors.get(String(cameraId)) || null;
  }

  async function clear() {
    cancelQueue('aborted');
    dropAllMemory();
    if (memoryOnly) return;
    const recs = await dbGetAll().catch(() => []);
    await Promise.all(recs.map((r) => dbDelete(r.key).catch(() => {})));
  }

  // ------------------------------------------------------------ lifecycle

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) cancelQueue('hidden');
  });
  window.addEventListener('edge:signed-out', () => { signedOut(); });
  window.addEventListener('edge:account', () => {
    const next = currentOwner();
    if (next) adoptOwner(next);
  });

  async function load() {
    const recs = await dbGetAll().catch(() => []);
    recs.forEach((r) => {
      if (!r || r.owner !== owner || !r.blob || !r.camera_id) return;
      if (!entries.has(r.camera_id)) entries.set(String(r.camera_id), { rec: r, url: null });
    });
    entries.forEach((_, id) => emit(id, 'done'));
  }

  async function init() {
    // A sign-out whose wipe the page reload interrupted: finish it first.
    if (lsGet(WIPE_KEY) === '1') await dbWipe();
    const auth = window.edgeAuth;
    if (auth && auth.ready) {
      const st = await auth.ready;
      if (st === 'invalid') {
        // Nobody is signed in (signed out, expired, or another person's
        // browser session ended): no pictures may stay behind.
        await signedOut();
        return;
      }
    }
    const next = currentOwner();
    if (!next) return;
    await adoptOwner(next);
    await load();
  }

  const ready = init().catch((e) => { try { console.warn('StillCache: could not load cached pictures', e); } catch (_) {} });

  window.StillCache = {
    ready,
    peek,
    capture,
    pending,
    lastError,
    refreshAll,
    transport,
    isStale,
    clear,
    STALE_MS,
    MAX_WIDTH,
  };
})();
