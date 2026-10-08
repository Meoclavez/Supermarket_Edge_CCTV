/**
 * Direct peer-to-peer live video (WebRTC) for the dashboard.
 *
 * Contract: docs/REMOTE_VIDEO_CONTRACT.md. Owner rule: camera video never
 * passes through the online-access server (VPS / tunnel). A remote viewer
 * gets live pictures only over a direct WebRTC connection to the box; the
 * tunnel carries nothing but the SDP offer/answer and small JSON calls.
 *
 *  - ICE servers come only from GET /api/v1/webrtc/config, and only `stun:`
 *    URLs are ever used. A selected candidate pair of type `relay` is a
 *    failure: the connection is closed and reported, never used.
 *  - A session exists only while a picture is on screen. Every handle is
 *    closed on close(), on closeAll() (view switch, hidden tab, pagehide),
 *    when its heartbeat is refused (404: the box ended it), or on failure.
 *    The box is told with DELETE (fetch keepalive, so it survives unload).
 *  - Heartbeats are sent only while the page is visible.
 *  - Failures are honest and specific; there is no snapshot fallback.
 *
 * API (window.WebRtcLive):
 *   config({refresh})            -> Promise<config> (cached)
 *   configNow()                  -> cached config or null
 *   transportNow()               -> 'webrtc' | 'local' | null (not known yet)
 *   mode()                       -> Promise<'webrtc' | 'local'>
 *   open(cameraId, videoEl, {purpose}) -> handle
 *       handle.state   'connecting' | 'connected' | 'failed' | 'closed'
 *       handle.pair    {local, remote, protocol} once connected
 *       handle.error   {code, message, detail, network} after a failure
 *       handle.closeReason, handle.on('state', fn) -> unsubscribe, handle.close(reason)
 *   grabFrame(cameraId, {maxWidth, timeoutMs, signal, type, quality}) -> Promise<Blob>
 *   loadStill(img, cameraId, opts) -> Promise<{ok, error}>; releaseStill(img)
 *   watchFrames(videoEl, fn)     -> stop(); fn on every decoded frame
 *   closeAll(reason)
 *   probeStun({iceServers, timeoutMs}) -> Promise<result>  (browser-side check)
 *   lanPreference() / setLanPreference('auto' | 'webrtc')
 *   pairLabel(pair), describe(error)
 */
(function () {
  'use strict';

  const CONFIG_URL = '/api/v1/webrtc/config';
  const SESSIONS_URL = '/api/v1/webrtc/sessions';
  const PREF_KEY = 'edge.liveTransport.v1';
  const ICE_GATHER_CAP_MS = 2500;       // offer is sent with what was gathered by then
  const CONNECT_TIMEOUT_MS = 15000;     // answer received -> connected
  const DISCONNECT_GRACE_MS = 6000;     // ICE 'disconnected' may recover by itself
  const CONFIG_RETRY_MS = 10000;        // a failed config fetch is retried at most this often
  const STATS_EVERY_MS = 5000;          // pair / byte counts sampled while connected (for the close report)

  let cfg = null;
  let cfgPromise = null;
  let cfgFailedAt = 0;
  let cfgError = null;
  let lastTransport = null;
  const handles = new Set();

  // ------------------------------------------------------------ helpers

  function codedError(code, message, detail, extra) {
    const e = new Error(message);
    e.code = code;
    e.detail = detail || '';
    Object.assign(e, extra || {});
    return e;
  }

  async function bodyOf(res) {
    try { return await res.json(); } catch (_) { return {}; }
  }

  function detailText(data) {
    if (!data) return '';
    let d = data.reason || data.detail || '';
    if (Array.isArray(d)) d = d.map((x) => (x && x.msg) || String(x)).join('; ');
    if (d && typeof d === 'object') d = d.reason || d.detail || JSON.stringify(d);
    return String(d || '');
  }

  function hasWebRtc() {
    return typeof window.RTCPeerConnection === 'function';
  }

  // ------------------------------------------------------------ per-browser preference

  /** 'webrtc': ask for direct video even on the store network / Tailscale (testing). */
  function lanPreference() {
    try { return window.localStorage.getItem(PREF_KEY) === 'webrtc' ? 'webrtc' : 'auto'; } catch (_) { return 'auto'; }
  }

  function setLanPreference(value) {
    const v = value === 'webrtc' ? 'webrtc' : 'auto';
    try {
      if (v === 'webrtc') window.localStorage.setItem(PREF_KEY, 'webrtc');
      else window.localStorage.removeItem(PREF_KEY);
    } catch (_) { /* storage blocked: applies to this visit only */ }
    memoPref = v;
    closeAll('transport-changed');
    invalidate();
    return config({ refresh: true }).catch(() => null);
  }
  // When localStorage is blocked the choice still holds for this visit.
  let memoPref = null;
  function effectivePref() { return memoPref || lanPreference(); }

  // ------------------------------------------------------------ config

  /** Keep only stun: URLs. Anything else (turn:, turns:, junk) is rejected and listed. */
  function onlyStun(list) {
    const servers = [];
    const rejected = [];
    (Array.isArray(list) ? list : []).forEach((s) => {
      if (!s) return;
      const urls = (Array.isArray(s.urls) ? s.urls : [s.urls || s.url]).filter(Boolean).map(String);
      const ok = urls.filter((u) => /^stun:[^\s]+$/i.test(u.trim()));
      urls.filter((u) => !ok.includes(u)).forEach((u) => rejected.push(u));
      if (ok.length) servers.push({ urls: ok.map((u) => u.trim()) });
    });
    return { servers, rejected };
  }

  function num(v, def, lo, hi) {
    const n = Number(v);
    if (!Number.isFinite(n)) return def;
    return Math.min(hi, Math.max(lo, n));
  }

  function normalize(d) {
    const { servers, rejected } = onlyStun(d.ice_servers);
    const relayOffered = d.relay === true || String(d.ice_transport_policy || 'all').toLowerCase() === 'relay';
    let available = d.available !== false && d.enabled !== false;
    let reason = d.reason || null;
    if (relayOffered) {
      available = false;
      reason = 'The box asked for a relayed connection. This dashboard never relays video, so direct video is off.';
    }
    return {
      enabled: d.enabled !== false,
      available,
      reason,
      remote: !!d.remote,
      transport: d.transport === 'webrtc' ? 'webrtc' : 'local',
      ice_servers: servers,
      rejected_ice_servers: rejected,
      ice_transport_policy: 'all',
      relay: false,
      heartbeat_s: num(d.heartbeat_s, 15, 3, 120),
      idle_timeout_s: num(d.idle_timeout_s, 45, 5, 3600),
      max_session_s: num(d.max_session_s, 14400, 60, 86400),
      max_sessions: Number.isFinite(Number(d.max_sessions)) ? Number(d.max_sessions) : null,
      active_sessions: Number.isFinite(Number(d.active_sessions)) ? Number(d.active_sessions) : null,
      mode: d.mode || null,
      unsupported: false,
      fetched_at: Date.now(),
    };
  }

  async function fetchConfig() {
    const headers = {};
    if (effectivePref() === 'webrtc') headers['X-Live-Transport'] = 'webrtc';
    let res;
    try {
      res = await fetch(CONFIG_URL, { headers, cache: 'no-store' });
    } catch (e) {
      throw codedError('network', 'Could not reach the box', e.message);
    }
    // A box without direct video answers the unknown route with JSON; frps
    // answers 404 (HTML) while the box is unreachable, which is not "older box".
    if (res.status === 404 && !/json/i.test(res.headers.get('Content-Type') || '')) {
      throw codedError('box_unreachable', 'Could not reach the box', 'The online-access server could not reach the box (HTTP 404).');
    }
    if (res.status === 404) {
      // A box without direct video: the page keeps the store-network behaviour.
      // (Through the tunnel the box refuses live pictures anyway, honestly.)
      return Object.assign(normalize({ transport: 'local', available: false, enabled: false }), {
        reason: 'This box does not offer direct video yet (no /api/v1/webrtc/config).', unsupported: true,
      });
    }
    if (!res.ok) {
      const data = await bodyOf(res);
      throw codedError(res.status === 401 ? 'auth' : 'config', 'Direct video settings unavailable',
        detailText(data) || `HTTP ${res.status}`);
    }
    return normalize(await res.json());
  }

  function announce() {
    const t = cfg ? cfg.transport : null;
    if (t === lastTransport) return;
    const prev = lastTransport;
    lastTransport = t;
    try { window.dispatchEvent(new CustomEvent('edge:live-transport', { detail: { transport: t, previous: prev, config: cfg } })); } catch (_) {}
  }

  function config(opts) {
    const refresh = !!(opts && opts.refresh);
    if (cfg && !refresh) return Promise.resolve(cfg);
    if (cfgPromise) return cfgPromise;
    if (!refresh && !cfg && cfgFailedAt && Date.now() - cfgFailedAt < CONFIG_RETRY_MS) return Promise.reject(cfgError);
    cfgPromise = fetchConfig().then((c) => {
      cfg = c;
      cfgFailedAt = 0;
      cfgError = null;
      cfgPromise = null;
      announce();
      return c;
    }, (e) => {
      cfgPromise = null;
      cfgFailedAt = Date.now();
      cfgError = e;
      announce();
      throw e;
    });
    return cfgPromise;
  }

  function invalidate() {
    cfg = null;
    cfgFailedAt = 0;
    cfgError = null;
  }

  function configNow() { return cfg; }

  /**
   * 'webrtc' | 'local' once known, null before the first answer. A failed
   * config fetch counts as 'local' (retried later): through the tunnel the
   * box refuses live pictures itself, so nothing leaks either way.
   */
  function transportNow() {
    if (cfg) return cfg.transport;
    if (cfgFailedAt) {
      if (Date.now() - cfgFailedAt >= CONFIG_RETRY_MS && !cfgPromise) config().catch(() => {});
      return 'local';
    }
    if (!cfgPromise) config().catch(() => {});
    return null;
  }

  function mode() {
    return config().then((c) => c.transport, () => 'local');
  }

  // ------------------------------------------------------------ errors in plain words

  const NETWORK_CODES = new Set(['ice_failed', 'ice_timeout', 'ice_disconnected', 'relay_pair']);

  function describe(err) {
    if (!err) return null;
    const code = err.code || 'error';
    const out = { code, message: err.message || 'Direct video failed', detail: err.detail || '', network: NETWORK_CODES.has(code) };
    return out;
  }

  function errorForResponse(status, data) {
    const code = data && data.code;
    const why = detailText(data);
    // The box always answers with a JSON code. A bare 404/502/503/504 comes from
    // the online-access server (frps answers 404 while the box is unreachable):
    // it is not "camera not found" and not a video-service failure.
    if (code === 'unknown_camera') return codedError('not_found', 'Camera not found on the box', why);
    // The box refuses rather than send a camera's raw picture when its privacy masks cannot be burned in.
    if (code === 'privacy_mask_unavailable') {
      return codedError('privacy_hidden', 'Hidden: privacy masks', why || 'Hidden: this camera has privacy masks and the masked live view could not be started.');
    }
    if (code === 'camera_off' || status === 409) return codedError('camera_off', 'Camera is turned off', why || 'Turn it on to watch it.');
    if (code === 'video_session_limit') {
      const max = data && Number.isFinite(Number(data.max_sessions)) ? Number(data.max_sessions) : null;
      return codedError('session_limit', 'Live video limit reached', `The box sends at most ${max === null ? 'a limited number of' : max} live videos at once. Close another viewer, or raise the limit in Settings > Online access.`);
    }
    // A 429 without that code is the box's request-rate guard, not the video limit.
    if (status === 429) return codedError('rate_limited', 'The box asked this browser to slow down', why && why !== 'Too Many Requests' ? why : 'Too many requests from this address in the last minute.');
    if (code === 'webrtc_unavailable') return codedError('webrtc_unavailable', 'Direct video is not available on the box', why || 'The video service is not running.');
    if (code === 'negotiation_failed') return codedError('negotiation_failed', 'The box could not start the video', why || 'Negotiation failed.');
    if (status === 401) return codedError('auth', 'Signed out', 'Sign in again to watch.');
    if (status === 403) return codedError('forbidden', 'Not allowed to watch', why);
    if (!code && (status === 404 || status === 502 || status === 503 || status === 504)) {
      return codedError('box_unreachable', 'The box did not answer', `The online-access server could not reach the box (HTTP ${status}); it may be restarting or offline.`);
    }
    if (status === 404) return codedError('not_found', 'Camera not found on the box', why);
    return codedError('error', 'Direct video failed', why || `HTTP ${status}`);
  }

  // ------------------------------------------------------------ stats

  /** The selected candidate pair (types + protocol) and received bytes / frames. */
  async function readStats(pc) {
    let stats;
    try { stats = await pc.getStats(); } catch (_) { return null; }
    const byId = new Map();
    stats.forEach((s) => byId.set(s.id, s));
    let pair = null;
    stats.forEach((s) => {
      if (s.type === 'transport' && s.selectedCandidatePairId && byId.get(s.selectedCandidatePairId)) pair = byId.get(s.selectedCandidatePairId);
    });
    if (!pair) {
      stats.forEach((s) => {
        if (!pair && s.type === 'candidate-pair' && (s.selected || (s.nominated && s.state === 'succeeded'))) pair = s;
      });
    }
    let inbound = null;
    stats.forEach((s) => {
      if (s.type === 'inbound-rtp' && (s.kind || s.mediaType) === 'video') inbound = s;
    });
    const local = pair ? byId.get(pair.localCandidateId) : null;
    const remote = pair ? byId.get(pair.remoteCandidateId) : null;
    return {
      pair: pair ? {
        local: (local && local.candidateType) || null,
        remote: (remote && remote.candidateType) || null,
        protocol: (local && local.protocol) || (remote && remote.protocol) || null,
      } : null,
      bytes_received: inbound ? inbound.bytesReceived || 0 : (pair ? pair.bytesReceived || 0 : 0),
      frames_decoded: inbound ? inbound.framesDecoded || 0 : 0,
    };
  }

  function isRelay(pair) {
    return !!pair && (pair.local === 'relay' || pair.remote === 'relay');
  }

  function pairLabel(pair) {
    if (!pair) return '';
    const l = pair.local || '?';
    const r = pair.remote || '?';
    return l === r ? l : `${l}/${r}`;
  }

  // ------------------------------------------------------------ session messages to the box

  function sendReport(h, state, stats, error) {
    if (!h.sessionId) return;
    const s = stats || h.lastStats || {};
    const body = {
      state,
      pair: s.pair || h.pair || null,
      bytes_received: s.bytes_received || 0,
      frames_decoded: s.frames_decoded || 0,
    };
    if (error) body.error = `${error.code}: ${error.detail || error.message}`.slice(0, 500);
    try {
      fetch(`${SESSIONS_URL}/${encodeURIComponent(h.sessionId)}/report`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), keepalive: true,
      }).catch(() => {});
    } catch (_) { /* unload in progress */ }
  }

  function sendDelete(sessionId) {
    if (!sessionId) return;
    try {
      fetch(`${SESSIONS_URL}/${encodeURIComponent(sessionId)}`, { method: 'DELETE', keepalive: true }).catch(() => {});
    } catch (_) { /* unload in progress */ }
  }

  // ------------------------------------------------------------ ICE gathering

  function waitIceGathering(pc, capMs) {
    if (pc.iceGatheringState === 'complete') return Promise.resolve(true);
    return new Promise((resolve) => {
      let done = false;
      const finish = (complete) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        pc.removeEventListener('icegatheringstatechange', onChange);
        pc.removeEventListener('icecandidate', onCand);
        resolve(complete);
      };
      const onChange = () => { if (pc.iceGatheringState === 'complete') finish(true); };
      const onCand = (e) => { if (!e.candidate) finish(true); };
      const timer = setTimeout(() => finish(false), capMs);
      pc.addEventListener('icegatheringstatechange', onChange);
      pc.addEventListener('icecandidate', onCand);
    });
  }

  // ------------------------------------------------------------ open

  /**
   * A viewer closed while its offer was being answered. Finish the handshake
   * and close at once, so the box sees a clean close (a DTLS close_notify)
   * instead of a peer that never connected. Nothing is shown; bounded wait.
   */
  async function closeAfterAnswer(pc, sdp) {
    try {
      if (sdp && pc.signalingState !== 'closed') {
        await pc.setRemoteDescription({ type: 'answer', sdp });
        await new Promise((resolve) => {
          const done = () => ['connected', 'failed', 'closed'].includes(pc.connectionState);
          if (done()) { resolve(); return; }
          const t = setTimeout(resolve, 4000);
          pc.addEventListener('connectionstatechange', () => { if (done()) { clearTimeout(t); resolve(); } });
        });
      }
    } catch (_) { /* closing anyway */ }
    try { pc.close(); } catch (_) {}
  }

  function open(cameraId, videoEl, opts) {
    const purpose = (opts && opts.purpose) || 'tile';
    const listeners = [];
    const h = {
      cameraId,
      purpose,
      video: videoEl || null,
      state: 'connecting',
      pair: null,
      error: null,
      closeReason: null,
      sessionId: null,
      codec: null,
      transcoded: null,
      startedAt: Date.now(),
      connectedAt: 0,
      lastStats: null,
      on(event, fn) {
        if (event !== 'state' || typeof fn !== 'function') return () => {};
        listeners.push(fn);
        return () => { const i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i, 1); };
      },
      close(reason) { end('closed', reason || 'closed'); },
    };
    let pc = null;
    let negotiating = false;   // the offer is at the box, its answer not back yet
    let hbTimer = null;
    let statsTimer = null;
    if (leaving) {
      // The page is being left: nothing new may start. Ends as closed, not failed.
      h.state = 'closed';
      h.closeReason = 'pagehide';
      Promise.resolve().then(() => listeners.slice().forEach((fn) => { try { fn(h.state, h); } catch (_) {} }));
      return h;
    }
    let connectTimer = null;
    let graceTimer = null;
    handles.add(h);

    function emit() {
      listeners.slice().forEach((fn) => { try { fn(h.state, h); } catch (e) { setTimeout(() => { throw e; }); } });
    }

    function setState(s) {
      if (h.state === s) return;
      h.state = s;
      emit();
    }

    const finished = () => h.state === 'closed' || h.state === 'failed';

    /** Tear down (once). 'failed' keeps h.error; both tell the box and free the video. */
    function end(finalState, reason, error) {
      if (finished()) return;
      handles.delete(h);
      clearInterval(hbTimer);
      clearInterval(statsTimer);
      clearTimeout(connectTimer);
      clearTimeout(graceTimer);
      h.closeReason = reason || null;
      if (error) h.error = describe(error);
      if (h.video && h.video.srcObject && h.video.srcObject === h.stream) {
        try { h.video.srcObject = null; } catch (_) {}
      }
      // Synchronous on purpose: the box learns at once (a rotation's incoming
      // camera must not open before the outgoing one is gone). The report
      // carries the counts sampled last (every few seconds while connected).
      sendReport(h, finalState === 'failed' ? 'failed' : 'closed', null, finalState === 'failed' ? h.error : null);
      sendDelete(h.sessionId);
      // Closed while the box is answering: keep the connection until the answer
      // is in and close it cleanly then (see start()). Closed now, the box's end
      // of it never connects and holds the camera stream until its ICE checks
      // give up (about 30 s, integration run 2026-09-29).
      if (pc && !(negotiating && !leaving)) { try { pc.close(); } catch (_) {} }
      setState(finalState);
    }

    function fail(err) { end('failed', err.code, err); }

    async function checkPair() {
      if (!pc || finished()) return;
      const stats = await readStats(pc);
      if (!stats || finished()) return;
      h.lastStats = stats;
      if (stats.pair) h.pair = stats.pair;
      if (isRelay(stats.pair)) {
        fail(codedError('relay_pair', 'Direct video not possible from this network',
          'Only a relayed path was offered. Relays are never used for camera video.'));
      }
    }

    async function onConnected() {
      if (h.state !== 'connecting') return;
      clearTimeout(connectTimer);
      clearTimeout(graceTimer);
      const stats = await readStats(pc);
      if (finished()) return;
      h.lastStats = stats;
      h.pair = stats && stats.pair ? stats.pair : null;
      if (isRelay(h.pair)) {
        fail(codedError('relay_pair', 'Direct video not possible from this network',
          'Only a relayed path was offered. Relays are never used for camera video.'));
        return;
      }
      h.connectedAt = Date.now();
      statsTimer = setInterval(() => { if (!document.hidden) checkPair(); }, STATS_EVERY_MS);
      setState('connected');
      sendReport(h, 'connected', stats, null);
    }

    function onIceState() {
      if (!pc || finished()) return;
      const s = pc.iceConnectionState;
      if (s === 'connected' || s === 'completed') {
        clearTimeout(graceTimer);
        graceTimer = null;
        if (h.state === 'connecting') onConnected();
        else checkPair();
      } else if (s === 'failed') {
        fail(codedError(h.state === 'connected' ? 'ice_disconnected' : 'ice_failed', 'Direct video not possible from this network',
          h.state === 'connected' ? 'The direct connection was lost.' : 'No direct path between this browser and the store was found.'));
      } else if (s === 'disconnected') {
        clearTimeout(graceTimer);
        graceTimer = setTimeout(() => {
          if (pc && pc.iceConnectionState === 'disconnected') {
            fail(codedError('ice_disconnected', 'Direct video connection lost', 'The network path to the store stopped working.'));
          }
        }, DISCONNECT_GRACE_MS);
      }
    }

    async function heartbeat() {
      if (finished() || !h.sessionId || document.hidden) return;
      try {
        const res = await fetch(`${SESSIONS_URL}/${encodeURIComponent(h.sessionId)}/heartbeat`, { method: 'POST', cache: 'no-store' });
        if (res.status === 404) { end('closed', 'expired'); return; }
        if (res.status === 401) { end('closed', 'signed-out'); return; }
      } catch (_) { /* a missed beat: the box's idle timeout decides */ }
    }

    async function start() {
      if (!hasWebRtc()) {
        throw codedError('no_webrtc', 'This browser cannot play direct video', 'WebRTC is not available in this browser.');
      }
      let c;
      try {
        c = await config();
      } catch (e) {
        throw codedError('config', 'Direct video settings unavailable', e.detail || e.message);
      }
      if (finished()) return;
      if (!c.available) {
        throw codedError('unavailable', 'Direct video is not available', c.reason || 'The box has direct video turned off.');
      }
      pc = new RTCPeerConnection({ iceServers: c.ice_servers, iceTransportPolicy: 'all', bundlePolicy: 'max-bundle' });
      pc.addTransceiver('video', { direction: 'recvonly' });
      pc.ontrack = (e) => {
        if (finished()) return;
        const stream = (e.streams && e.streams[0]) || new MediaStream([e.track]);
        h.stream = stream;
        const v = h.video;
        if (v) {
          v.muted = true;
          v.defaultMuted = true;
          v.playsInline = true;
          v.autoplay = true;
          v.srcObject = stream;
          const p = v.play();
          if (p && typeof p.catch === 'function') p.catch(() => { /* autoplay retried by the element */ });
        }
      };
      pc.addEventListener('iceconnectionstatechange', onIceState);
      pc.addEventListener('connectionstatechange', () => {
        if (pc && pc.connectionState === 'failed' && !finished()) {
          fail(codedError('ice_failed', 'Direct video not possible from this network', 'The secure connection to the store could not be set up.'));
        }
      });

      const offer = await pc.createOffer();
      if (finished()) return;
      await pc.setLocalDescription(offer);
      await waitIceGathering(pc, ICE_GATHER_CAP_MS);
      if (finished()) return;

      let res;
      negotiating = true;
      try {
        res = await fetch(SESSIONS_URL, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ camera_id: cameraId, sdp: pc.localDescription.sdp, purpose }),
          cache: 'no-store',
        });
      } catch (e) {
        negotiating = false;
        throw codedError('network', 'Could not reach the box', e.message);
      }
      const data = await bodyOf(res);
      negotiating = false;
      if (!res.ok) {
        if (res.status === 401) invalidate();
        if (finished()) { try { pc.close(); } catch (_) {} return; }
        throw errorForResponse(res.status, data);
      }
      h.sessionId = data.session_id || null;
      if (finished()) {                  // closed while the box was answering
        sendDelete(h.sessionId);
        await closeAfterAnswer(pc, res.ok ? data.sdp : null);
        return;
      }
      h.codec = data.codec || null;
      h.transcoded = typeof data.transcoded === 'boolean' ? data.transcoded : null;
      const beat = num(data.heartbeat_s, c.heartbeat_s, 3, 120) * 1000;
      hbTimer = setInterval(heartbeat, beat);
      if (!data.sdp) throw codedError('negotiation_failed', 'The box could not start the video', 'The answer had no SDP.');
      await pc.setRemoteDescription({ type: 'answer', sdp: data.sdp });
      if (finished()) return;
      connectTimer = setTimeout(() => {
        if (h.state === 'connecting') {
          fail(codedError('ice_timeout', 'Direct video not possible from this network',
            `No direct path to the store was found within ${Math.round(CONNECT_TIMEOUT_MS / 1000)} s.`));
        }
      }, CONNECT_TIMEOUT_MS);
      onIceState();
    }

    start().catch((e) => {
      if (finished()) return;
      fail(e && e.code ? e : codedError('error', 'Direct video failed', (e && e.message) || String(e)));
    });
    return h;
  }

  function closeAll(reason) {
    [...handles].forEach((h) => h.close(reason || 'closed'));
  }

  // ------------------------------------------------------------ frames

  /** Call fn on every decoded frame (requestVideoFrameCallback, else timeupdate). Returns stop(). */
  function watchFrames(video, fn) {
    let stopped = false;
    if (video && typeof video.requestVideoFrameCallback === 'function') {
      let id = 0;
      const step = (now, meta) => {
        if (stopped) return;
        try { fn(meta || null); } catch (_) {}
        id = video.requestVideoFrameCallback(step);
      };
      id = video.requestVideoFrameCallback(step);
      return () => {
        stopped = true;
        try { video.cancelVideoFrameCallback(id); } catch (_) {}
      };
    }
    if (!video) return () => {};
    const on = () => { if (!stopped) { try { fn(null); } catch (_) {} } };
    video.addEventListener('timeupdate', on);
    return () => { stopped = true; video.removeEventListener('timeupdate', on); };
  }

  /**
   * One still picture over direct video: open a session (purpose 'frame'),
   * wait for the first decoded frame, draw it, close. Resolves a Blob
   * (JPEG by default) with frameWidth / frameHeight / pair set on it.
   */
  function grabFrame(cameraId, opts) {
    const o = opts || {};
    return new Promise((resolve, reject) => {
      const video = document.createElement('video');
      video.className = 'webrtc-grab-video';
      video.muted = true;
      video.playsInline = true;
      video.autoplay = true;
      video.setAttribute('aria-hidden', 'true');
      document.body.appendChild(video);
      let settled = false;
      let poll = null;
      let stopFrames = null;
      const h = open(cameraId, video, { purpose: 'frame' });
      const cleanup = (reason) => {
        clearTimeout(timer);
        clearInterval(poll);
        if (stopFrames) stopFrames();
        h.close(reason);
        video.remove();
      };
      const settle = (err, blob) => {
        if (settled) return;
        settled = true;
        cleanup(err ? 'frame-failed' : 'frame-done');
        if (err) reject(err); else resolve(blob);
      };
      const timer = setTimeout(() => settle(codedError('frame_timeout', 'No picture arrived',
        `The direct video connection opened but no picture came within ${Math.round((o.timeoutMs || 20000) / 1000)} s.`)), o.timeoutMs || 20000);
      if (o.signal) {
        if (o.signal.aborted) { settle(codedError('aborted', 'Cancelled')); return; }
        o.signal.addEventListener('abort', () => settle(codedError('aborted', 'Cancelled')), { once: true });
      }
      h.on('state', (s) => {
        if (s === 'failed') {
          const e = h.error || {};
          settle(codedError(e.code || 'error', e.message || 'Direct video failed', e.detail || '', { network: !!e.network }));
        } else if (s === 'closed' && !settled) {
          settle(codedError('closed', 'Direct video closed', h.closeReason === 'hidden' ? 'The page was hidden.' : 'The picture was cancelled.'));
        }
      });
      const capture = () => {
        if (settled) return;
        const vw = video.videoWidth;
        const vh = video.videoHeight;
        if (!(vw > 0 && vh > 0) || video.readyState < 2) return;
        const scale = o.maxWidth && vw > o.maxWidth ? o.maxWidth / vw : 1;
        const w = Math.max(1, Math.round(vw * scale));
        const hgt = Math.max(1, Math.round(vh * scale));
        const canvas = document.createElement('canvas');
        canvas.width = w;
        canvas.height = hgt;
        try {
          canvas.getContext('2d').drawImage(video, 0, 0, w, hgt);
        } catch (e) {
          settle(codedError('draw_failed', 'Could not read the picture', e.message));
          return;
        }
        const pair = h.pair;
        clearInterval(poll);
        canvas.toBlob((blob) => {
          if (!blob) { settle(codedError('encode_failed', 'Could not encode the picture')); return; }
          blob.frameWidth = vw;
          blob.frameHeight = vh;
          blob.pair = pair;
          settle(null, blob);
        }, o.type || 'image/jpeg', o.quality || 0.9);
      };
      stopFrames = watchFrames(video, capture);
      video.addEventListener('loadeddata', capture);
      poll = setInterval(capture, 150);   // rVFC may not fire for a tiny element; readyState still does
    });
  }

  /** Put one direct-video still into an <img> (blob URL; the previous one is freed). */
  async function loadStill(img, cameraId, opts) {
    try {
      const blob = await grabFrame(cameraId, opts);
      if (!img) return { ok: true, blob };
      const url = URL.createObjectURL(blob);
      releaseStill(img);
      img._rtcStillUrl = url;
      img.src = url;
      return { ok: true, blob };
    } catch (e) {
      return { ok: false, error: describe(e) };
    }
  }

  function releaseStill(img) {
    if (img && img._rtcStillUrl) {
      try { URL.revokeObjectURL(img._rtcStillUrl); } catch (_) {}
      img._rtcStillUrl = null;
    }
  }

  // ------------------------------------------------------------ browser-side STUN check

  function candidateInfo(c) {
    const s = c.candidate || '';
    const m = / typ (\w+)/.exec(s);
    const parts = s.split(' ');
    return {
      type: c.type || (m ? m[1] : 'unknown'),
      protocol: (c.protocol || parts[2] || '').toLowerCase(),
      address: c.address || c.ip || parts[4] || '',
      port: c.port || Number(parts[5]) || null,
      url: c.url || null,
    };
  }

  /**
   * Gather ICE candidates against the configured (or given) STUN servers.
   * Nothing is sent to the box; this only shows what this browser's network
   * allows: a srflx candidate means a public address was learned over UDP.
   */
  async function probeStun(opts) {
    const o = opts || {};
    if (!hasWebRtc()) return { ok: false, error: 'WebRTC is not available in this browser.', candidates: [], errors: [], servers: [] };
    let servers = o.iceServers;
    let rejected = [];
    if (!servers) {
      const c = await config();
      servers = c.ice_servers;
      rejected = c.rejected_ice_servers || [];
    } else {
      const f = onlyStun(servers);
      servers = f.servers;
      rejected = f.rejected;
    }
    const candidates = [];
    const errors = [];
    const pc = new RTCPeerConnection({ iceServers: servers, iceTransportPolicy: 'all' });
    pc.addEventListener('icecandidate', (e) => { if (e.candidate && e.candidate.candidate) candidates.push(candidateInfo(e.candidate)); });
    pc.addEventListener('icecandidateerror', (e) => {
      errors.push({ url: e.url || '', code: e.errorCode || null, text: e.errorText || '' });
    });
    const t0 = Date.now();
    let complete = false;
    try {
      pc.createDataChannel('probe');
      await pc.setLocalDescription(await pc.createOffer());
      complete = await waitIceGathering(pc, o.timeoutMs || 6000);
    } finally {
      try { pc.close(); } catch (_) {}
    }
    const types = {};
    candidates.forEach((c) => { types[c.type] = (types[c.type] || 0) + 1; });
    const srflx = candidates.filter((c) => c.type === 'srflx');
    return {
      ok: true,
      complete,
      elapsed_ms: Date.now() - t0,
      servers: servers.map((s) => s.urls).flat(),
      rejected,
      candidates,
      types,
      srflx: srflx.length > 0,
      public_addresses: [...new Set(srflx.map((c) => c.address).filter(Boolean))],
      udp_srflx: srflx.some((c) => c.protocol === 'udp'),
      relay: candidates.some((c) => c.type === 'relay'),
      errors,
    };
  }

  // ------------------------------------------------------------ page lifecycle

  let leaving = false;
  window.addEventListener('pagehide', () => { leaving = true; closeAll('pagehide'); });
  window.addEventListener('pageshow', () => { leaving = false; });
  document.addEventListener('visibilitychange', () => { if (document.hidden) closeAll('hidden'); });
  document.addEventListener('freeze', () => closeAll('freeze'));
  // Signing in / out changes what the config says (and who may watch).
  window.addEventListener('edge:auth', () => { closeAll('signed-out'); invalidate(); });

  window.WebRtcLive = {
    config,
    configNow,
    transportNow,
    mode,
    open,
    grabFrame,
    loadStill,
    releaseStill,
    watchFrames,
    closeAll,
    probeStun,
    lanPreference: effectivePref,
    setLanPreference,
    pairLabel,
    describe,
    activeCount: () => handles.size,
  };
})();
