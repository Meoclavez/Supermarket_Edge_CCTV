// Settings > "Backups and reset" (#settings-backups). Owner/admin only.
//
//   GET  /api/v1/system/backups                     list (kind, store time, size)
//   POST /api/v1/system/backup                      "Back up now"
//   GET  /api/v1/system/backups/{file}/download     "Download"
//   POST /api/v1/system/backups/{file}/restore      "Restore" (safety backup first, then restart)
//   POST /api/v1/system/backups/upload?filename=    "Upload a backup" (checked, then listed)
//   POST /api/v1/system/factory-reset {confirm}     "Factory reset" (backup first)
//
// No prompt()/confirm()/alert(): every confirmation is inline. Loads on the
// edge:tab settings event and when the signed-in account becomes known.
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const DASH = '—';
  const state = {
    data: null,          // last GET /system/backups
    error: null,
    busy: false,
    confirmRestore: null, // filename awaiting "Yes, restore"
    status: '',
    statusError: false,
    resetStatus: '',
    resetError: false,
    restarting: false,
    resetText: '',      // what is typed in the RESET field (survives re-renders)
  };

  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  async function apiError(res, fallback) {
    let data = {};
    try { data = await res.json(); } catch (_) { /* not JSON */ }
    let d = data && data.detail;
    if (Array.isArray(d)) d = d.map((x) => (x && x.msg) || String(x)).join('; ');
    return d || `${fallback} (HTTP ${res.status})`;
  }

  async function api(url, opts, fallback) {
    const res = await fetch(url, Object.assign({ cache: 'no-store' }, opts || {}));
    if (!res.ok) throw new Error(await apiError(res, fallback || 'Request failed'));
    return res.json();
  }

  function jsonOpts(method, body) {
    return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) };
  }

  function fmtSize(bytes) {
    if (typeof bytes !== 'number' || !Number.isFinite(bytes)) return DASH;
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
    if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  }

  function isAdmin() {
    const a = window.EdgeAuth;
    return !(a && a.known && !a.isAdmin);
  }

  function settingsVisible() {
    const tab = $('tab-settings');
    return !!(tab && tab.classList.contains('active') && !document.hidden);
  }

  // ------------------------------------------------------------------ render
  function restartSentence() {
    return state.data && state.data.restart_supported
      ? 'The service then restarts to load it, which takes about a minute.'
      : 'The server must then be restarted by hand to finish.';
  }

  function renderList() {
    const d = state.data;
    if (state.error) return `<div class="fp-empty">${esc(state.error)}</div>`;
    if (!d) return '<div class="fp-empty">Loading…</div>';
    if (!d.backups.length) return '<div class="fp-empty">No backups yet. One is taken each time the device starts.</div>';
    const rows = d.backups.map((b) => {
      const confirming = state.confirmRestore === b.filename;
      const row = `<tr>
          <td>${esc(b.time_label || DASH)}</td>
          <td>${esc(b.kind || DASH)}</td>
          <td>${esc(fmtSize(b.size_bytes))}</td>
          <td class="bk-actions">
            <button type="button" class="btn btn-secondary btn-sm" data-bk="download" data-file="${esc(b.filename)}">Download</button>
            <button type="button" class="btn btn-secondary btn-sm" data-bk="restore" data-file="${esc(b.filename)}"${state.busy ? ' disabled' : ''}>Restore</button>
          </td>
        </tr>`;
      if (!confirming) return row;
      return `${row}<tr class="bk-confirm-row"><td colspan="4"><div class="bk-confirm">
          <p>Restore the backup from ${esc(b.time_label || b.filename)}? First a safety backup of the current database is taken.
          Then the layout, cameras, settings, user accounts and recorded figures go back to how they were at that time;
          anything recorded since is kept only in the safety backup. ${esc(restartSentence())} You may need to sign in again.</p>
          <div class="bk-confirm-actions">
            <button type="button" class="btn btn-danger btn-sm" data-bk="restore-yes" data-file="${esc(b.filename)}"${state.busy ? ' disabled' : ''}>Yes, restore</button>
            <button type="button" class="btn btn-secondary btn-sm" data-bk="restore-no">Keep</button>
          </div></div></td></tr>`;
    }).join('');
    return `<div class="bk-table-wrap"><table class="data-table bk-table">
        <thead><tr><th>When (store time)</th><th>Kind</th><th>Size</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table></div>`;
  }

  function render() {
    const card = $('settings-backups');
    if (!card) return;
    if (!isAdmin()) {
      card.innerHTML = `<div class="card-title"><span>Backups and reset</span></div>
        <p class="ra-hint">Only an owner or administrator can back up, restore or reset this device.</p>`;
      return;
    }
    const resetReady = state.resetText === 'RESET';
    card.innerHTML = `
      <div class="card-title"><span>Backups and reset</span>
        <button type="button" class="btn btn-secondary btn-sm" data-bk="refresh"${state.busy ? ' disabled' : ''}>Refresh</button></div>
      <p class="ra-hint">Backups hold the database: store layout, cameras, settings, user accounts and recorded figures.
        Evidence images and clips are not included. The device backs up each time it starts. A downloaded backup contains
        this store's accounts and camera settings, so keep it somewhere safe.</p>
      <div class="bk-toolbar">
        <button type="button" class="btn btn-primary btn-sm" data-bk="backup"${state.busy ? ' disabled' : ''}>Back up now</button>
        <label class="btn btn-secondary btn-sm bk-upload${state.busy ? ' bk-disabled' : ''}">Upload a backup
          <input type="file" id="bkUploadInput" accept=".db,.gz,.sqlite,.sqlite3,application/gzip,application/vnd.sqlite3"${state.busy ? ' disabled' : ''} hidden>
        </label>
        <span class="bk-note">An uploaded file is checked first and then appears in the list; restoring it is a separate step.</span>
      </div>
      <div class="form-status${state.statusError ? ' form-status-error' : ''}" id="bkStatus" role="status" aria-live="polite">${esc(state.status)}</div>
      <div id="bkList">${renderList()}</div>
      <div class="bk-danger">
        <div class="bk-danger-title">Factory reset</div>
        <p><b>Deletes:</b> the store layout (zones, shelves, walls, doors), all cameras and found devices with their
          camera passwords and overlays, and every recorded figure: visits, tracks, shelf reaches, theft incidents,
          sales rows, line crossings, queue visits, heatmap history and recommendations.</p>
        <p><b>Keeps:</b> user accounts and passwords, site settings, saved recorder passwords, paired phones, online access,
          device settings (.env), saved evidence images and clips (removed by the storage limit as usual), and all backups.</p>
        <p>A backup is taken first, so a reset can be undone by restoring that backup.</p>
        <div class="bk-reset-row">
          <label for="bkResetInput">Type RESET to confirm</label>
          <input type="text" id="bkResetInput" class="form-input bk-reset-input" autocomplete="off" spellcheck="false" value="${esc(state.resetText)}"${state.busy ? ' disabled' : ''}>
          <button type="button" class="btn btn-danger btn-sm" data-bk="reset"${resetReady && !state.busy ? '' : ' disabled'}>Factory reset</button>
        </div>
        <div class="form-status${state.resetError ? ' form-status-error' : ''}" id="bkResetStatus" role="status" aria-live="polite">${esc(state.resetStatus)}</div>
      </div>`;
  }

  function setStatus(text, isError) {
    state.status = text || '';
    state.statusError = !!isError;
    const el = $('bkStatus');
    if (el) {
      el.textContent = state.status;
      el.classList.toggle('form-status-error', state.statusError);
    }
  }

  // ------------------------------------------------------------------ actions
  async function load() {
    if (!isAdmin()) { render(); return; }
    try {
      state.data = await api('/api/v1/system/backups', null, 'Could not load backups');
      state.error = null;
    } catch (e) {
      state.error = `Backups are unavailable: ${e.message}`;
    }
    render();
  }

  async function backupNow() {
    state.busy = true; setStatus('Backing up…'); render();
    try {
      const r = await api('/api/v1/system/backup', jsonOpts('POST', { tag: 'manual' }), 'Backup failed');
      state.status = `Backup saved (${fmtSize(r.size_bytes)}).`; state.statusError = false;
    } catch (e) {
      state.status = e.message; state.statusError = true;
    }
    state.busy = false;
    await load();
  }

  async function download(file) {
    setStatus('Preparing download…');
    try {
      const res = await fetch(`/api/v1/system/backups/${encodeURIComponent(file)}/download`, { cache: 'no-store' });
      if (!res.ok) throw new Error(await apiError(res, 'Download failed'));
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = file; a.rel = 'noopener';
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      setStatus(`Downloaded ${file}.`);
    } catch (e) {
      setStatus(e.message, true);
    }
  }

  async function waitForRestart() {
    // The service stops about two seconds after answering, then comes back.
    const started = Date.now();
    await new Promise((r) => setTimeout(r, 5000));
    while (Date.now() - started < 180000) {
      try {
        const res = await fetch('/api/v1/health', { cache: 'no-store' });
        if (res.ok) {
          state.restarting = false;
          state.busy = false;
          state.status = 'The service is back with the restored database. Reload the page to see it.';
          state.statusError = false;
          render();
          const el = $('bkStatus');
          if (el) {
            const b = document.createElement('button');
            b.type = 'button'; b.className = 'btn btn-secondary btn-sm bk-reload'; b.textContent = 'Reload page';
            b.addEventListener('click', () => window.location.reload());
            el.appendChild(b);
          }
          return;
        }
      } catch (_) { /* still restarting */ }
      await new Promise((r) => setTimeout(r, 3000));
    }
    state.restarting = false; state.busy = false;
    setStatus('The service has not come back after three minutes. Check the device.', true);
    render();
  }

  async function restore(file) {
    state.busy = true; state.confirmRestore = null;
    setStatus('Taking a safety backup, then restoring…'); render();
    try {
      const r = await api(`/api/v1/system/backups/${encodeURIComponent(file)}/restore`, jsonOpts('POST', {}), 'Restore failed');
      const safety = r.safety_backup ? ` Safety backup: ${r.safety_backup}.` : '';
      state.status = `${r.message}${safety}`; state.statusError = false;
      if (r.restart === 'scheduled') {
        state.restarting = true;
        render();
        waitForRestart();
        return;
      }
    } catch (e) {
      state.status = e.message; state.statusError = true;
    }
    state.busy = false;
    await load();
  }

  async function upload(file) {
    if (!file) return;
    const max = state.data && state.data.max_upload_bytes;
    if (max && file.size > max) { setStatus(`${file.name} is larger than the ${fmtSize(max)} limit.`, true); return; }
    state.busy = true; setStatus(`Uploading and checking ${file.name}…`); render();
    try {
      const r = await api(`/api/v1/system/backups/upload?filename=${encodeURIComponent(file.name)}`,
        { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file }, 'Upload failed');
      state.status = `${file.name} passed the checks and is listed as an uploaded backup (${r.time_label || r.filename}). Use Restore on it to put it back.`;
      state.statusError = false;
    } catch (e) {
      state.status = `${file.name} was not added: ${e.message}`; state.statusError = true;
    }
    state.busy = false;
    await load();
  }

  async function factoryReset() {
    const input = $('bkResetInput');
    if (!input || input.value !== 'RESET') return;
    state.resetText = '';
    state.busy = true; state.resetStatus = 'Taking a backup, then resetting…'; state.resetError = false; render();
    try {
      const r = await api('/api/v1/system/factory-reset', jsonOpts('POST', { confirm: 'RESET' }), 'Factory reset failed');
      state.resetStatus = `Factory reset done: ${Number(r.total_rows || 0).toLocaleString()} rows deleted. Backup taken first: ${r.backup || DASH}.`;
      state.resetError = false;
    } catch (e) {
      state.resetStatus = e.message; state.resetError = true;
    }
    state.busy = false;
    await load();
  }

  // ------------------------------------------------------------------ wiring
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('#settings-backups [data-bk]');
    if (!btn || btn.disabled) return;
    const act = btn.getAttribute('data-bk');
    const file = btn.getAttribute('data-file');
    if (act === 'refresh') { setStatus(''); load(); }
    else if (act === 'backup') backupNow();
    else if (act === 'download') download(file);
    else if (act === 'restore') { state.confirmRestore = file; render(); }
    else if (act === 'restore-no') { state.confirmRestore = null; render(); }
    else if (act === 'restore-yes') restore(file);
    else if (act === 'reset') factoryReset();
  });
  document.addEventListener('input', (e) => {
    if (e.target && e.target.id === 'bkResetInput') {
      state.resetText = e.target.value;
      const b = document.querySelector('#settings-backups [data-bk="reset"]');
      if (b) b.disabled = state.busy || e.target.value !== 'RESET';
    }
  });
  document.addEventListener('change', (e) => {
    if (e.target && e.target.id === 'bkUploadInput') {
      const f = e.target.files && e.target.files[0];
      e.target.value = '';
      upload(f);
    }
  });

  window.addEventListener('edge:tab', (ev) => {
    if (ev.detail && ev.detail.tab === 'settings' && !state.restarting) load();
  });
  window.addEventListener('edge:account', () => { if (settingsVisible() && !state.restarting) load(); });
  const boot = () => { if (settingsVisible()) load(); else render(); };
  if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') window.edgeAuth.onReady(boot);
  else if (document.readyState !== 'loading') boot();
  else document.addEventListener('DOMContentLoaded', boot);
})();
