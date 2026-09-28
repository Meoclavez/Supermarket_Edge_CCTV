// Settings: "Recorder sub-streams" (#settings-recorder-streams).
//
// Upgrades a Dahua recorder's CIF sub-streams to D1 and puts saved settings
// back. Everything is started by the operator here; nothing runs by itself.
//   GET  /api/v1/recorders                         recorders the cameras use
//   GET  /api/v1/recorders/{id}/substreams         read-only check: size now + plan per channel
//   POST /api/v1/recorders/{id}/substreams/d1      start the upgrade for the ticked channels
//   GET  /api/v1/recorders/{id}/substreams/d1      progress / last result
//   POST /api/v1/recorders/{id}/substreams/restore put saved settings back (one channel or all)
//
// No prompt()/confirm()/alert(): confirmations are inline rows.
(function () {
  'use strict';

  const POLL_MS = 1500;
  let recorders = [];
  let current = null;        // selected recorder id
  let preview = null;        // last GET .../substreams
  let result = null;         // last GET .../substreams/d1
  let confirming = null;     // null | 'upgrade' | 'restore-all' | 'restore:<ch>'
  let busy = false;
  let pollTimer = null;
  let message = { text: '', kind: '' };

  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));

  async function apiError(res, fallback) {
    let data = {};
    try { data = await res.json(); } catch (_) { /* not JSON */ }
    let d = data && data.detail;
    if (Array.isArray(d)) d = d.map((x) => (x && x.msg) || String(x)).join('; ');
    return d || `${fallback} (HTTP ${res.status})`;
  }

  function fmtTime(iso) {
    if (!iso) return '';
    const t = new Date(iso);
    return Number.isNaN(t.getTime()) ? String(iso) : t.toLocaleString();
  }

  function httpPort() {
    const v = parseInt(($('rsHttpPort') || {}).value, 10);
    return Number.isFinite(v) && v > 0 && v < 65536 ? v : 80;
  }

  function rec() { return recorders.find((r) => r.id === current) || null; }

  function say(text, kind) { message = { text: text || '', kind: kind || '' }; }

  // ------------------------------------------------------------------ render

  const OUTCOME_CLASS = {
    switched: 'fp-ok', already_d1: 'fp-info', restore_ok: 'fp-ok',
    unsupported: 'fp-warn', caps_unknown: 'fp-warn', refused: 'fp-warn', restored: 'fp-warn',
    not_found: 'fp-warn', nothing_to_restore: 'fp-info',
    restore_failed: 'fp-error', error: 'fp-error', stopped: 'fp-error', not_attempted: 'fp-info',
    working: 'fp-info', pending: 'fp-info',
  };

  function render() {
    const host = $('settings-recorder-streams');
    if (!host) return;
    const r = rec();
    const running = !!(result && result.running);
    let html = `<div class="card-title"><span>Recorder sub-streams</span>${running
      ? '<span class="badge badge-warning">Working…</span>' : ''}</div>`;
    if (!recorders.length) {
      host.innerHTML = html + `<div class="fp-empty">No camera uses a Dahua recorder channel
        (<code>/cam/realmonitor?channel=N</code>), so there is nothing to upgrade here.</div>`;
      return;
    }
    html += `<div class="dev-hint" style="margin-bottom:8px;">Analysis sees far shoppers much better at D1 (704×576)
      than at CIF (352×288). This switches the recorder's sub-stream of the channels you tick to D1, checks the
      camera really sends D1, and puts the previous settings back on any failure. The recorder's own recording
      (main stream) is not changed.</div>`;
    html += '<div class="fp-field-row">';
    html += `<div class="fp-field"><label for="rsRecorder">Recorder</label><select id="rsRecorder">${
      recorders.map((x) => `<option value="${esc(x.id)}"${x.id === current ? ' selected' : ''}>${esc(x.host)}
        (${x.cameras.length} camera${x.cameras.length === 1 ? '' : 's'})</option>`).join('')}</select></div>`;
    html += `<div class="fp-field"><label for="rsHttpPort">Recorder web port</label>
      <input id="rsHttpPort" type="number" min="1" max="65535" value="${httpPort()}"></div></div>`;
    if (r && !r.has_credentials) {
      html += `<div class="fp-status fp-warn">No sign-in is saved for ${esc(r.host)}. Save it under
        Cameras &amp; devices → Dahua recorder → Save sign-in first.</div>`;
    }
    html += `<div class="fp-actions">
      <button type="button" class="btn btn-sm btn-secondary" data-rs="check"${busy || running || !r || !r.has_credentials ? ' disabled' : ''}
        title="Reads each channel's sub-stream settings from the recorder. Changes nothing.">Check sub-streams</button>
      ${r && r.saved_channels && r.saved_channels.length ? `<button type="button" class="btn btn-sm" data-rs="ask-restore-all"${busy || running ? ' disabled' : ''}>
        Restore previous stream settings (${r.saved_channels.length})</button>` : ''}
    </div>`;
    if (confirming === 'restore-all') {
      html += confirmRow(`Put the saved sub-stream settings back on ${r.saved_channels.length} channel(s)?`,
        'restore-all', 'Yes, restore all');
    }
    if (message.text) html += `<div class="fp-status ${esc(message.kind || 'fp-info')}" role="status">${esc(message.text)}</div>`;
    html += renderPreview();
    html += renderResult();
    host.innerHTML = html;
  }

  function confirmRow(text, action, yes) {
    return `<div class="fp-actions" role="group" aria-label="Confirm">
      <span style="font-size:11.5px; align-self:center;">${esc(text)}</span>
      <button type="button" class="btn btn-sm btn-primary" data-rs="do-${esc(action)}">${esc(yes)}</button>
      <button type="button" class="btn btn-sm" data-rs="cancel">Cancel</button></div>`;
  }

  function renderPreview() {
    if (!preview) return '';
    if (!preview.ok) {
      return `<div class="fp-status fp-error">${esc(preview.error || 'Could not read the recorder.')}</div>`;
    }
    const rows = preview.channels || [];
    // 'unknown' = the camera did not report its sizes: offered unticked, tried
    // only on request (still verified on the stream and put back on failure).
    const upgradable = rows.filter((x) => x.plan === 'upgrade' || x.plan === 'unknown');
    let html = `<div class="dev-hint" style="margin-top:10px;">${esc(preview.standard || '')} recorder: D1 is
      ${esc(preview.target || '')}. Ticked channels will be switched.</div>
      <div class="table-scroll"><table class="data-table"><thead><tr><th></th><th>Channel</th><th>Camera</th>
      <th>Sub-stream now</th><th>Plan</th></tr></thead><tbody>`;
    rows.forEach((x) => {
      const can = x.plan === 'upgrade';
      const tryable = x.plan === 'unknown';
      html += `<tr><td><input type="checkbox" data-rs-ch="${x.channel}" data-rs-unknown="${tryable ? 1 : 0}"
        aria-label="Upgrade channel ${x.channel}" ${can ? 'checked' : (tryable ? '' : 'disabled')}></td><td>${x.channel}</td><td>${esc((x.cameras || []).join(', '))}</td>
        <td>${esc(x.current || '—')}${x.bitrate_kbps ? ` · ${x.bitrate_kbps} kbps` : ''}</td>
        <td>${esc(x.plan_text || '')}${x.supported && x.plan === 'unsupported'
          ? `<div class="dev-hint">Supports ${esc(x.supported.join(', '))}</div>` : ''}${tryable
          ? '<div class="dev-hint">Tick to try D1 anyway: it is checked on the live stream and put back if the camera does not deliver it.</div>' : ''}</td></tr>`;
    });
    html += '</tbody></table></div>';
    if (upgradable.length) {
      html += `<div class="fp-actions"><button type="button" class="btn btn-sm btn-primary" data-rs="ask-upgrade"
        ${busy || (result && result.running) ? 'disabled' : ''}>Upgrade CIF sub-streams to D1</button></div>`;
      if (confirming === 'upgrade') {
        const n = selectedChannels().length;
        html += confirmRow(`Change the sub-stream of ${n} channel(s) on the recorder to D1? Each channel's current
          settings are saved first and put back if D1 does not work. Channels are changed one at a time; each
          camera's picture pauses for a few seconds.`, 'upgrade', `Yes, upgrade ${n}`);
      }
    } else {
      html += '<div class="fp-empty">No channel can be upgraded: each is already D1 or more, or its camera does not offer D1.</div>';
    }
    return html;
  }

  function renderResult() {
    const run = result && result.run;
    if (!run) return '';
    const saved = new Set((result.saved_channels || []).map(Number));
    const kind = run.kind === 'restore' ? 'Restore' : 'Upgrade to D1';
    const cls = run.state === 'done' ? 'fp-ok' : (run.state === 'running' ? 'fp-info' : 'fp-error');
    let html = `<div class="card-title" style="margin-top:14px;"><span>Last change: ${esc(kind)}</span></div>
      <div class="fp-status ${cls}">${esc(run.message || run.state)}</div>
      <div class="dev-hint">Started ${esc(fmtTime(run.started_at))} by ${esc(run.by || 'unknown')}${
        run.finished_at ? ` · finished ${esc(fmtTime(run.finished_at))}` : ''}</div>`;
    if (!(run.channels || []).length) return html;
    html += `<div class="table-scroll"><table class="data-table"><thead><tr><th>Channel</th><th>Camera</th>
      <th>Before</th><th>After</th><th>Outcome</th><th></th></tr></thead><tbody>`;
    run.channels.forEach((e) => {
      const ask = confirming === `restore:${e.channel}`;
      const canRestore = saved.has(Number(e.channel)) && !result.running;
      html += `<tr><td>${e.channel}</td><td>${esc((e.cameras || []).join(', '))}</td><td>${esc(e.before || '—')}</td>
        <td>${esc(e.after || '—')}</td><td><span class="fp-status ${OUTCOME_CLASS[e.outcome] || 'fp-info'}">${esc(e.text)}</span></td>
        <td>${canRestore ? (ask
          ? `<button type="button" class="btn btn-xs btn-primary" data-rs="do-restore:${e.channel}">Yes, restore</button>
             <button type="button" class="btn btn-xs" data-rs="cancel">Cancel</button>`
          : `<button type="button" class="btn btn-xs" data-rs="ask-restore:${e.channel}" ${busy ? 'disabled' : ''}
             title="Put this channel's saved sub-stream settings back">Restore</button>`) : ''}</td></tr>`;
    });
    return html + '</tbody></table></div>';
  }

  function selectedChannels() {
    return Array.from(document.querySelectorAll('#settings-recorder-streams input[data-rs-ch]'))
      .filter((b) => b.checked && !b.disabled).map((b) => parseInt(b.getAttribute('data-rs-ch'), 10));
  }

  // ------------------------------------------------------------------ actions

  async function loadRecorders() {
    try {
      const res = await fetch('/api/v1/recorders', { cache: 'no-store' });
      if (!res.ok) { say(await apiError(res, 'Could not list recorders'), 'fp-error'); render(); return; }
      recorders = (await res.json()).recorders || [];
      if (!recorders.some((r) => r.id === current)) current = recorders.length ? recorders[0].id : null;
      await loadResult();
    } catch (e) {
      say(`Could not list recorders: ${e.message}`, 'fp-error');
    }
    render();
  }

  async function loadResult() {
    if (!current) { result = null; return; }
    try {
      const res = await fetch(`/api/v1/recorders/${encodeURIComponent(current)}/substreams/d1`, { cache: 'no-store' });
      result = res.ok ? await res.json() : null;
    } catch (_) { result = null; }
    const r = rec();
    if (r && result) r.saved_channels = result.saved_channels || [];
    if (result && result.running) schedulePoll();
  }

  function schedulePoll() {
    if (pollTimer) return;
    pollTimer = setTimeout(async () => {
      pollTimer = null;
      const wasRunning = result && result.running;
      await loadResult();
      if (wasRunning && result && !result.running) {
        say('Finished. Cameras on changed channels reconnect within a few seconds.', 'fp-ok');
        preview = null;
      }
      render();
    }, POLL_MS);
  }

  async function check() {
    busy = true; confirming = null; preview = null;
    say('Reading the recorder\'s sub-stream settings (read-only)…', 'fp-info'); render();
    try {
      const res = await fetch(`/api/v1/recorders/${encodeURIComponent(current)}/substreams?http_port=${httpPort()}`,
        { cache: 'no-store' });
      if (!res.ok) say(await apiError(res, 'Check failed'), 'fp-error');
      else { preview = await res.json(); say('', ''); }
    } catch (e) { say(`Check failed: ${e.message}`, 'fp-error'); }
    busy = false; render();
  }

  async function post(path, body, okText) {
    busy = true; confirming = null; render();
    try {
      const res = await fetch(`/api/v1/recorders/${encodeURIComponent(current)}/${path}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      if (!res.ok) say(await apiError(res, 'Request failed'), 'fp-error');
      else { say(okText, 'fp-info'); preview = null; }
    } catch (e) { say(`Request failed: ${e.message}`, 'fp-error'); }
    busy = false;
    await loadResult();
    render();
  }

  function onClick(ev) {
    const btn = ev.target.closest('[data-rs]');
    if (!btn || btn.disabled) return;
    const a = btn.getAttribute('data-rs');
    if (a === 'check') check();
    else if (a === 'cancel') { confirming = null; render(); }
    else if (a === 'ask-upgrade') {
      if (!selectedChannels().length) { say('Tick at least one channel.', 'fp-warn'); render(); return; }
      const keep = selectedChannels();
      confirming = 'upgrade'; render();
      document.querySelectorAll('#settings-recorder-streams input[data-rs-ch]').forEach((b) => {
        if (!b.disabled) b.checked = keep.includes(parseInt(b.getAttribute('data-rs-ch'), 10));
      });
    } else if (a === 'do-upgrade') {
      const channels = selectedChannels();
      const tryUnknown = channels.some((ch) => {
        const b = document.querySelector(`#settings-recorder-streams input[data-rs-ch="${ch}"]`);
        return b && b.getAttribute('data-rs-unknown') === '1';
      });
      post('substreams/d1', { channels, http_port: httpPort(), try_when_caps_unknown: tryUnknown },
        `Upgrading ${channels.length} channel(s), one at a time…`);
    } else if (a === 'ask-restore-all') { confirming = 'restore-all'; render(); }
    else if (a === 'do-restore-all') post('substreams/restore', { all: true, http_port: httpPort() }, 'Restoring…');
    else if (a.startsWith('ask-restore:')) { confirming = `restore:${a.split(':')[1]}`; render(); }
    else if (a.startsWith('do-restore:')) {
      post('substreams/restore', { channels: [parseInt(a.split(':')[1], 10)], http_port: httpPort() }, 'Restoring…');
    }
  }

  function onChange(ev) {
    if (ev.target && ev.target.id === 'rsRecorder') {
      current = ev.target.value; preview = null; confirming = null; say('', '');
      loadResult().then(render);
    }
  }

  function settingsVisible() {
    const tab = $('tab-settings');
    return !!(tab && tab.classList.contains('active'));
  }

  function open() {
    if (window.edgeAuth && window.edgeAuth.isGateOpen && window.edgeAuth.isGateOpen()) return;
    loadRecorders();
  }

  function init() {
    const host = $('settings-recorder-streams');
    if (!host) return;
    host.addEventListener('click', onClick);
    host.addEventListener('change', onChange);
    if (settingsVisible()) open();
  }

  window.addEventListener('edge:tab', (ev) => {
    if (ev.detail && ev.detail.tab === 'settings') open();
  });
  if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') window.edgeAuth.onReady(init);
  else if (document.readyState !== 'loading') init();
  else document.addEventListener('DOMContentLoaded', init);

  window.edgeRecorderStreams = { reload: loadRecorders };
})();
