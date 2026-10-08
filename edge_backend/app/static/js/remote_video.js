// Settings > Online access > Live video (#settings-remote-video).
//
// Live video never passes through the online-access server: a remote viewer
// gets it over a direct WebRTC connection to this box, and the server only
// helps both ends learn their public address (STUN). See
// docs/REMOTE_VIDEO_CONTRACT.md and js/webrtc_live.js.
//
// This card holds:
//  - this browser's choice "Live video on this network: automatic / direct
//    WebRTC" (localStorage; sends X-Live-Transport: webrtc on the config call),
//  - the box's settings, saved with PUT /api/v1/remote-access: stun_servers,
//    webrtc_mode (auto | fixed_port), webrtc_port, max_video_sessions,
//    live_transport_on_lan (local | webrtc),
//  - "Check remote video": a STUN gathering test in this browser, the box's
//    GET /api/v1/webrtc/diagnostics, the active sessions
//    (GET /api/v1/webrtc/sessions) and plain-language advice.
//
// No prompt()/confirm()/alert(); nothing polls. Data loads when Settings opens.
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const FIELDS = ['stun_servers', 'webrtc_mode', 'webrtc_port', 'max_video_sessions', 'live_transport_on_lan'];

  let settings = null;        // the video fields of GET /api/v1/remote-access (null: not offered)
  let settingsError = '';
  let check = null;           // last check result {browser, diag, sessions, at}
  let checking = false;
  let dirty = false;

  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  async function apiError(res, fallback) {
    let data = {};
    try { data = await res.json(); } catch (_) { /* not JSON */ }
    let d = data && (data.detail || data.reason);
    if (Array.isArray(d)) d = d.map((x) => (x && x.msg) || String(x)).join('; ');
    if (d && typeof d === 'object') d = d.message || JSON.stringify(d);
    return d || `${fallback} (HTTP ${res.status})`;
  }

  function fmtTime(v) {
    if (!v) return '—';
    const t = typeof v === 'number' ? new Date(v * (v < 1e12 ? 1000 : 1)) : new Date(v);
    return Number.isNaN(t.getTime()) ? String(v) : t.toLocaleString();
  }

  function fmtBytes(n) {
    if (!Number.isFinite(Number(n))) return '—';
    const v = Number(n);
    if (v < 1024) return `${v} B`;
    if (v < 1024 * 1024) return `${(v / 1024).toFixed(0)} KB`;
    if (v < 1024 ** 3) return `${(v / 1024 / 1024).toFixed(1)} MB`;
    return `${(v / 1024 ** 3).toFixed(2)} GB`;
  }

  function cameraLabel(id) {
    try {
      // analytics.js keeps the camera list as a global binding.
      // eslint-disable-next-line no-undef
      const list = typeof allCamerasList !== 'undefined' ? allCamerasList : [];
      const c = list.find((x) => x.id === id);
      return c ? c.name || id : id;
    } catch (_) { return id; }
  }

  function setStatus(id, text, isError) {
    const n = $(id);
    if (!n) return;
    n.textContent = text || '';
    n.classList.toggle('form-status-error', !!isError);
  }

  // ------------------------------------------------------------ render

  function transportLine() {
    const W = window.WebRtcLive;
    if (!W) return { text: 'Direct video is not loaded on this page.', kind: 'err' };
    const c = W.configNow();
    if (!c) return { text: 'Asking the box how live video reaches this page…', kind: 'wait' };
    if (c.transport === 'webrtc') {
      if (!c.available) return { text: `This page needs direct video, but it is not available: ${c.reason || 'no reason given'}`, kind: 'err' };
      return { text: `This page gets live video over a direct connection (WebRTC)${c.remote ? ', because it was opened through online access' : ''}.`, kind: 'ok' };
    }
    return { text: c.unsupported ? `This page uses store-network pictures. ${c.reason || ''}` : 'This page uses store-network pictures (snapshots and the enlarged live stream).', kind: 'wait' };
  }

  function renderCard(errorText) {
    const host = $('settings-remote-video');
    if (!host) return;
    const W = window.WebRtcLive;
    const pref = W ? W.lanPreference() : 'auto';
    const t = transportLine();
    const s = settings || {};
    const stun = Array.isArray(s.stun_servers) ? s.stun_servers.join('\n') : '';
    const mode = s.webrtc_mode === 'fixed_port' ? 'fixed_port' : 'auto';
    const off = !settings;
    host.innerHTML = `
      <div class="card-title"><span>Online access · Live video</span>
        <span class="badge ${t.kind === 'ok' ? 'badge-green' : t.kind === 'err' ? 'badge-danger' : 'badge-warning'}" id="rvTransportBadge">${t.kind === 'ok' ? 'Direct video' : t.kind === 'err' ? 'Unavailable' : 'Store network'}</span></div>
      <div class="ra-hint">Camera video never passes through your online-access server. A viewer away from the store gets it over a
        direct connection from this box (WebRTC); your server only helps both ends find each other (STUN). Video is sent only while
        a camera is on screen, and stops when the viewer looks away.</div>
      <div class="form-status" id="rvTransport">${esc(t.text)}</div>

      <div class="rv-section">
        <div class="rv-section-title">This browser</div>
        <div class="ra-row">
          <div class="form-group ra-grow">
            <label for="rvLanPref">Live video on this network</label>
            <select class="form-select" id="rvLanPref">
              <option value="auto" ${pref === 'auto' ? 'selected' : ''}>Automatic (store-network pictures here, direct video through online access)</option>
              <option value="webrtc" ${pref === 'webrtc' ? 'selected' : ''}>Direct WebRTC (for testing the remote path on the store network or Tailscale)</option>
            </select>
            <div class="ra-hint">Applies to this browser only.</div>
          </div>
        </div>
      </div>

      <form id="rvForm" class="rv-section" autocomplete="off">
        <div class="rv-section-title">This box</div>
        ${off ? `<div class="form-status form-status-error" id="rvUnsupported">${esc(settingsError || 'This box does not offer live video settings yet.')}</div>` : ''}
        <fieldset class="rv-fieldset" ${off ? 'disabled' : ''}>
          <div class="form-grid-2col ra-fields">
            <div class="form-group">
              <label for="rvStun">STUN servers</label>
              <textarea class="form-input rv-textarea" id="rvStun" rows="3" spellcheck="false" autocapitalize="off"
                placeholder="stun:stun.example.com:3478">${esc(stun)}</textarea>
              <div class="ra-hint">One per line (up to 4), each starting with <code>stun:</code>. They only tell each side its public address;
                relays (<code>turn:</code>) are never used.</div>
            </div>
            <div class="form-group">
              <label for="rvMode">Connection mode</label>
              <select class="form-select" id="rvMode">
                <option value="auto" ${mode === 'auto' ? 'selected' : ''}>Automatic: no router change</option>
                <option value="fixed_port" ${mode === 'fixed_port' ? 'selected' : ''}>Fixed port: forwarded on the store router</option>
              </select>
              <div class="rv-port" id="rvPortRow" ${mode === 'fixed_port' ? '' : 'hidden'}>
                <label for="rvPort">UDP port forwarded to this box</label>
                <input class="form-input" id="rvPort" type="number" min="1024" max="65535" step="1" value="${esc(s.webrtc_port || 8555)}">
              </div>
              <div class="ra-hint">Use fixed port only if direct video fails from outside and your installer forwarded a UDP port to this box.</div>
            </div>
          </div>
          <div class="form-grid-2col ra-fields">
            <div class="form-group">
              <label for="rvMax">Most live videos at once</label>
              <input class="form-input" id="rvMax" type="number" min="1" max="32" step="1" value="${esc(s.max_video_sessions || 8)}">
              <div class="ra-hint">Each camera tile on screen is one video. Four tiles on two phones is eight.</div>
            </div>
            <div class="form-group">
              <label for="rvLanAll">Live video on the store network (every viewer)</label>
              <select class="form-select" id="rvLanAll">
                <option value="local" ${s.live_transport_on_lan !== 'webrtc' ? 'selected' : ''}>Store-network pictures (default)</option>
                <option value="webrtc" ${s.live_transport_on_lan === 'webrtc' ? 'selected' : ''}>Direct WebRTC</option>
              </select>
              <div class="ra-hint">Viewers through online access always get direct video.</div>
            </div>
          </div>
          <div class="ra-inline ra-mt">
            <button type="submit" class="btn btn-primary btn-sm" id="rvSave">Save</button>
            <span class="ra-hint">Changes apply when you press Save.</span>
          </div>
        </fieldset>
        <div class="form-status ${errorText ? 'form-status-error' : ''}" id="rvFormStatus">${esc(errorText || '')}</div>
      </form>

      <div class="rv-section" id="rvCheckSection">
        <div class="rv-section-title">Check remote video</div>
        <div class="ra-inline">
          <button type="button" class="btn btn-sm btn-primary" id="rvCheckBtn" ${checking ? 'disabled' : ''}>${checking ? 'Checking…' : 'Check remote video'}</button>
          <span class="ra-hint">Tests this browser's network, asks the box what it sees, and lists who is watching now. No video is sent.</span>
        </div>
        <div id="rvCheckResult">${renderCheck()}</div>
      </div>`;
    bind();
  }

  function bind() {
    const pref = $('rvLanPref');
    if (pref) pref.addEventListener('change', onPrefChange);
    const form = $('rvForm');
    if (form) {
      form.addEventListener('submit', onSave);
      form.addEventListener('input', () => { dirty = true; });
    }
    const mode = $('rvMode');
    if (mode) mode.addEventListener('change', () => { const r = $('rvPortRow'); if (r) r.hidden = mode.value !== 'fixed_port'; });
    const btn = $('rvCheckBtn');
    if (btn) btn.addEventListener('click', runCheck);
  }
  // "Refresh" in the sessions table is re-rendered with it: one delegated listener.
  document.addEventListener('click', (e) => {
    if (e.target && e.target.closest && e.target.closest('#rvSessionsRefresh')) refreshSessions();
  });

  // ------------------------------------------------------------ check rendering

  function list(items) {
    return items.length ? `<ul class="rv-advice">${items.map((a) => `<li>${esc(a)}</li>`).join('')}</ul>` : '';
  }

  function browserAdvice(b, cfg) {
    const out = [];
    if (!b) return out;
    if (!b.ok) { out.push(b.error || 'The browser check could not run.'); return out; }
    if (b.rejected && b.rejected.length) out.push(`Ignored addresses that are not STUN servers: ${b.rejected.join(', ')}.`);
    if (!b.servers.length) {
      out.push('No STUN server is set, so this browser cannot learn its public address. Only viewers on the same network as the box can get direct video.');
    } else if (b.srflx) {
      out.push(`This browser's network lets direct video through: its public address ${b.public_addresses.join(', ') || '(hidden)'} was found over ${b.udp_srflx ? 'UDP' : 'TCP'}.`);
    } else {
      // Chrome reports a name that does not resolve as 701 "STUN host lookup
      // received error": the server's name is wrong or not in DNS, not a network block.
      const lookup = b.errors.filter((e) => e.code === 701 && /lookup/i.test(e.text || ''));
      const unreachable = b.errors.some((e) => e.code === 701 && !/lookup/i.test(e.text || ''));
      if (lookup.length) {
        const names = [...new Set(lookup.map((e) => e.url).filter(Boolean))];
        out.push(`The STUN server name ${names.join(', ') || ''} could not be looked up (DNS): it does not exist yet or is misspelled. Check the STUN servers below.`.replace('  ', ' '));
      }
      if (unreachable) {
        out.push('This browser could not reach the STUN server: this network blocks it (common on guest Wi-Fi and company networks). Direct video from here will probably fail; mobile data usually works.');
      } else if (!lookup.length) {
        out.push('No public address was found from this browser. Direct video from this network may fail; try mobile data.');
      }
    }
    if (b.relay) out.push('A relay address appeared. Relays are never used for camera video.');
    if (cfg && cfg.transport === 'local') out.push('This page is on the store network, so its camera tiles use store-network pictures. The check above still tests the remote path.');
    return out;
  }

  function diagBlock(d) {
    if (!d) return '';
    if (d.error) return `<div class="form-status form-status-error">${esc(d.error)}</div>`;
    const x = d.data || {};
    const g = x.go2rtc || {};
    const rows = [];
    const yes = (v) => (v === true ? 'yes' : v === false ? 'no' : '—');
    rows.push(['Video service (go2rtc)', `running: ${yes(g.running)} · reachable: ${yes(g.api_reachable ?? g.api)}${g.version ? ` · ${g.version}` : ''}`]);
    const mode = x.mode || (x.webrtc && x.webrtc.mode);
    const port = x.webrtc_port ?? x.port ?? (x.webrtc && x.webrtc.port);
    rows.push(['Connection mode', mode ? `${mode === 'fixed_port' ? 'fixed port' : 'automatic'}${mode === 'fixed_port' && port ? ` · UDP ${port}` : ''}` : '—']);
    const stun = (x.nat && Array.isArray(x.nat.servers)) ? x.nat.servers
      : Array.isArray(x.stun) ? x.stun : [];
    if (stun.length) {
      stun.forEach((e) => {
        const server = e.server || e.url || '';
        const addr = e.mapped || e.public_address || e.address || null;
        rows.push([`Public address via ${server}`, addr || (e.error ? `not found: ${e.error}` : 'not found')]);
      });
    } else {
      rows.push(['Public address', x.public_address || 'not reported']);
    }
    const nat = x.nat_mapping || (x.nat && x.nat.mapping);
    const natText = { endpoint_independent: 'same public port for every destination (good for direct video)',
      endpoint_dependent: 'a different public port per destination (direct video may fail from some networks)', unknown: 'unknown' }[nat] || nat || '—';
    rows.push(['Store router (NAT)', natText]);
    if (Number.isFinite(Number(x.relay_violations)) && Number(x.relay_violations) > 0) {
      rows.push(['Relayed connections refused', String(x.relay_violations)]);
    }
    if (x.max_sessions != null) rows.push(['Live videos now / most at once', `${x.active_sessions ?? '—'} / ${x.max_sessions}`]);
    const outcomes = Array.isArray(x.sessions) ? x.sessions : Array.isArray(x.outcomes) ? x.outcomes : Array.isArray(x.recent) ? x.recent : [];
    const advice = Array.isArray(x.advice) ? x.advice.map(String) : [];
    const recent = outcomes.slice(-10).reverse();
    return `
      <table class="data-table rv-kv"><tbody>
        ${rows.map(([k, v]) => `<tr><th scope="row">${esc(k)}</th><td>${esc(v)}</td></tr>`).join('')}
      </tbody></table>
      ${advice.length ? `<div class="rv-sub">What the box suggests</div>${list(advice)}` : ''}
      <div class="rv-sub">Last connections (${outcomes.length} kept)</div>
      ${recent.length ? `<div class="rv-table-wrap"><table class="data-table"><thead><tr><th>When</th><th>Camera</th><th>Result</th><th>Path</th><th>Viewer</th></tr></thead><tbody>
        ${recent.map((o) => `<tr><td>${esc(fmtTime(o.at || o.time || o.ended_at || o.started_at))}</td><td>${esc(cameraLabel(o.camera_id || ''))}</td>
          <td>${esc(o.report || o.state || o.outcome || '—')}${o.end_reason ? ` (${esc(o.end_reason)})` : ''}${o.error ? ` · ${esc(o.error)}` : ''}</td>
          <td>${esc(o.pair ? `${o.pair.local || '?'}/${o.pair.remote || '?'} ${o.pair.protocol || ''}` : '—')}</td>
          <td>${esc(o.viewer_ip || '—')}${o.remote === true ? ' (online access)' : o.remote === false ? ' (store network)' : ''}</td></tr>`).join('')}
      </tbody></table></div>` : '<div class="ra-hint">No connections recorded yet.</div>'}
      <details class="rv-raw"><summary>Raw diagnostics</summary><pre class="ra-pre">${esc(JSON.stringify(x, null, 2))}</pre></details>`;
  }

  function sessionsBlock(s) {
    if (!s) return '';
    const head = `<div class="ra-inline rv-sub"><span>Watching now</span><button type="button" class="btn btn-secondary btn-xs" id="rvSessionsRefresh">Refresh</button></div>`;
    if (s.error) return `${head}<div class="form-status form-status-error">${esc(s.error)}</div>`;
    const rows = s.rows || [];
    if (!rows.length) return `${head}<div class="ra-hint">Nobody is watching live video right now.</div>`;
    return `${head}<div class="rv-table-wrap"><table class="data-table"><thead><tr><th>Camera</th><th>For</th><th>Viewer</th><th>Since</th><th>Last heartbeat</th><th>Path</th><th>Sent</th></tr></thead><tbody>
      ${rows.map((r) => `<tr><td>${esc(cameraLabel(r.camera_id || ''))}</td><td>${esc({ tile: 'grid tile', focus: 'enlarged', frame: 'one picture' }[r.purpose] || r.purpose || '—')}</td>
        <td>${esc(r.viewer_ip || '—')}${r.remote === true ? ' (online access)' : r.remote === false ? ' (store network)' : ''}</td>
        <td>${esc(fmtTime(r.started_at))}</td><td>${esc(fmtTime(r.last_heartbeat))}</td>
        <td>${esc(r.pair ? `${r.pair.local || '?'}/${r.pair.remote || '?'} ${r.pair.protocol || ''}` : 'connecting')}</td>
        <td>${esc(fmtBytes(r.bytes_sent))}</td></tr>`).join('')}
    </tbody></table></div>`;
  }

  function renderCheck() {
    if (!check) return '';
    const b = check.browser;
    const cfg = window.WebRtcLive ? window.WebRtcLive.configNow() : null;
    const types = b && b.ok ? Object.entries(b.types).map(([k, n]) => `${k} × ${n}`).join(', ') || 'none' : '—';
    return `
      <div class="rv-sub">This browser (checked ${esc(fmtTime(check.at))})</div>
      ${b && b.ok ? `<table class="data-table rv-kv"><tbody>
        <tr><th scope="row">STUN servers tried</th><td>${esc(b.servers.join(', ') || 'none')}</td></tr>
        <tr><th scope="row">Addresses found</th><td>${esc(types)}</td></tr>
        <tr><th scope="row">Public address (srflx)</th><td>${b.srflx ? `yes · ${esc(b.public_addresses.join(', ') || 'hidden')}` : 'no'}</td></tr>
        ${b.errors.length ? `<tr><th scope="row">STUN errors</th><td>${esc(b.errors.map((e) => `${e.url} ${e.code || ''} ${e.text}`.trim()).join('; '))}</td></tr>` : ''}
      </tbody></table>` : ''}
      ${list(browserAdvice(b, cfg))}
      <div class="rv-sub">The box</div>
      ${diagBlock(check.diag)}
      ${sessionsBlock(check.sessions)}`;
  }

  // ------------------------------------------------------------ actions

  async function onPrefChange(ev) {
    const v = ev.target.value;
    if (!window.WebRtcLive) return;
    setStatus('rvTransport', 'Asking the box again…');
    await window.WebRtcLive.setLanPreference(v);
    const keep = dirty ? snapshotForm() : null;
    renderCard();
    if (keep) restoreForm(keep);
  }

  function snapshotForm() {
    return {
      stun: $('rvStun') ? $('rvStun').value : '', mode: $('rvMode') ? $('rvMode').value : 'auto',
      port: $('rvPort') ? $('rvPort').value : '', max: $('rvMax') ? $('rvMax').value : '', lan: $('rvLanAll') ? $('rvLanAll').value : 'local',
    };
  }

  function restoreForm(k) {
    if ($('rvStun')) $('rvStun').value = k.stun;
    if ($('rvMode')) $('rvMode').value = k.mode;
    if ($('rvPortRow')) $('rvPortRow').hidden = k.mode !== 'fixed_port';
    if ($('rvPort')) $('rvPort').value = k.port;
    if ($('rvMax')) $('rvMax').value = k.max;
    if ($('rvLanAll')) $('rvLanAll').value = k.lan;
  }

  /** Validate the form; returns {body} or {error}. */
  function readForm() {
    const f = snapshotForm();
    const stun = [];
    for (const raw of f.stun.split(/[\n,]+/).map((x) => x.trim()).filter(Boolean)) {
      if (/^turns?:/i.test(raw)) return { error: `"${raw}" is a relay (TURN) address. Relays are never used; enter stun: addresses only.`, field: 'rvStun' };
      const url = /^stun:/i.test(raw) ? raw : `stun:${raw}`;
      if (!/^stun:[A-Za-z0-9.\-[\]:]+$/.test(url)) return { error: `"${raw}" is not a STUN address (expected stun:host:port).`, field: 'rvStun' };
      stun.push(url);
    }
    if (stun.length > 4) return { error: 'Enter at most 4 STUN servers.', field: 'rvStun' };
    const port = Number(f.port);
    if (f.mode === 'fixed_port' && !(Number.isInteger(port) && port >= 1024 && port <= 65535)) {
      return { error: 'Enter a UDP port from 1024 to 65535.', field: 'rvPort' };
    }
    const max = Number(f.max);
    if (!(Number.isInteger(max) && max >= 1 && max <= 32)) return { error: 'Enter how many live videos at once, from 1 to 32.', field: 'rvMax' };
    const body = { stun_servers: stun, webrtc_mode: f.mode === 'fixed_port' ? 'fixed_port' : 'auto', max_video_sessions: max,
      live_transport_on_lan: f.lan === 'webrtc' ? 'webrtc' : 'local' };
    if (f.mode === 'fixed_port') body.webrtc_port = port;
    return { body };
  }

  async function onSave(ev) {
    ev.preventDefault();
    ['rvStun', 'rvPort', 'rvMax'].forEach((id) => { const n = $(id); if (n) n.removeAttribute('aria-invalid'); });
    const r = readForm();
    if (r.error) {
      setStatus('rvFormStatus', r.error, true);
      const n = $(r.field);
      if (n) { n.setAttribute('aria-invalid', 'true'); n.focus(); }
      return;
    }
    const btn = $('rvSave');
    if (btn) btn.disabled = true;
    setStatus('rvFormStatus', 'Saving…');
    try {
      const res = await fetch('/api/v1/remote-access', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(r.body),
      });
      if (!res.ok) throw new Error(await apiError(res, 'Could not save the live video settings'));
      const data = await res.json();
      adopt(data);
      dirty = false;
      if (window.WebRtcLive) await window.WebRtcLive.config({ refresh: true }).catch(() => null);
      renderCard();
      setStatus('rvFormStatus', 'Saved. New video connections use these settings.');
    } catch (e) {
      setStatus('rvFormStatus', e.message, true);
      if (btn) btn.disabled = false;
    }
  }

  function adopt(data) {
    if (data && FIELDS.some((k) => k in data)) {
      settings = {};
      FIELDS.forEach((k) => { settings[k] = data[k]; });
      settingsError = '';
    } else {
      settings = null;
      settingsError = 'This box does not offer live video settings yet (no stun_servers in GET /api/v1/remote-access).';
    }
  }

  async function getJson(url) {
    const res = await fetch(url, { cache: 'no-store' });
    if (res.status === 403) return { error: 'Only an administrator can see this.' };
    if (res.status === 404) return { error: 'This box does not offer this check yet.' };
    if (!res.ok) return { error: await apiError(res, 'Not available') };
    return { data: await res.json() };
  }

  async function loadSessions() {
    const r = await getJson('/api/v1/webrtc/sessions');
    if (r.error) return { error: r.error };
    const d = r.data;
    return { rows: Array.isArray(d) ? d : (d && (d.sessions || d.items)) || [] };
  }

  async function refreshSessions() {
    if (!check) return;
    check.sessions = await loadSessions();
    const n = $('rvCheckResult');
    if (n) n.innerHTML = renderCheck();
  }

  async function runCheck() {
    if (checking || !window.WebRtcLive) return;
    checking = true;
    const btn = $('rvCheckBtn');
    if (btn) { btn.disabled = true; btn.textContent = 'Checking…'; }
    const W = window.WebRtcLive;
    await W.config({ refresh: true }).catch(() => null);
    const [browser, diag, sessions] = await Promise.all([
      W.probeStun().catch((e) => ({ ok: false, error: e.detail || e.message, candidates: [], errors: [], servers: [] })),
      getJson('/api/v1/webrtc/diagnostics').catch((e) => ({ error: e.message })),
      loadSessions().catch((e) => ({ error: e.message })),
    ]);
    check = { browser, diag, sessions, at: Date.now() };
    checking = false;
    const keep = dirty ? snapshotForm() : null;
    renderCard();
    if (keep) restoreForm(keep);
  }

  async function load() {
    try {
      const res = await fetch('/api/v1/remote-access', { cache: 'no-store' });
      if (res.status === 401) return;
      if (!res.ok) throw new Error(await apiError(res, 'Online access settings unavailable'));
      adopt(await res.json());
    } catch (e) {
      settings = null;
      settingsError = e.message;
    }
    if (window.WebRtcLive) await window.WebRtcLive.config().catch(() => null);
    dirty = false;
    renderCard();
  }

  function settingsVisible() {
    const tab = $('tab-settings');
    return !!(tab && tab.classList.contains('active'));
  }

  /** From a tile's "Check connection": show this card and run the check. */
  function openCheck() {
    const go = () => {
      const sec = $('rvCheckSection') || $('settings-remote-video');
      if (sec) sec.scrollIntoView({ behavior: 'instant', block: 'start' });
      runCheck();
    };
    if ($('rvCheckBtn')) go(); else load().then(go);
  }

  window.addEventListener('edge:tab', (ev) => {
    if (ev.detail && ev.detail.tab === 'settings') load();
  });
  // The transport became known or changed: refresh the line that says so.
  window.addEventListener('edge:live-transport', () => {
    if (!$('rvTransport')) return;
    const t = transportLine();
    setStatus('rvTransport', t.text, t.kind === 'err');
  });
  const boot = () => { if (settingsVisible()) load(); };
  // Start only after auth.js has checked the stored token (edgeAuth.onReady).
  if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') window.edgeAuth.onReady(boot);
  else if (document.readyState !== 'loading') boot();
  else document.addEventListener('DOMContentLoaded', boot);

  window.edgeRemoteVideo = { reload: load, openCheck };
})();
