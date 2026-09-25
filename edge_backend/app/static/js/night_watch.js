/**
 * Night watch: the per-camera section in the camera settings modal and the
 * "Night watch" card (who is armed now, the next window, recent night
 * events) on the Loss prevention view.
 *
 * APIs (routes/night_watch.py):
 *   GET  /api/v1/cameras/{id}/night-watch   settings, store time zone, live state
 *   PUT  /api/v1/cameras/{id}/night-watch   {enabled, start, end, days, when_dark, sensitivity, cooldown_sec}
 *   GET  /api/v1/night-watch                every camera set up: state, next arm / disarm
 *   GET  /api/v1/night-watch/events         NIGHT_INTRUSION / NIGHT_MOTION, newest first
 *
 * Times: schedules are in the STORE's time (the server's SITE_TIMEZONE), and
 * every instant arrives with its UTC value and the store-local label. The
 * viewer's own time is added from the browser's time zone (Intl) whenever it
 * differs from the store's, e.g. "02:13 AEST · 21:43 IST your time".
 *
 * Nothing is invented: a camera is shown as watching only when the server
 * says it is armed, and one that should be but has no picture says so.
 */
(function () {
  'use strict';

  const DAYS = [['mon', 'Mon'], ['tue', 'Tue'], ['wed', 'Wed'], ['thu', 'Thu'], ['fri', 'Fri'], ['sat', 'Sat'], ['sun', 'Sun']];
  const SENSITIVITY = [['low', 'Low (large movement only)'], ['medium', 'Medium'], ['high', 'High (small movement)']];
  const STATE = {
    disarmed: ['Off now', 'nw-off'],
    armed_idle: ['Watching', 'nw-on'],
    motion: ['Movement: checking for a person', 'nw-motion'],
    person_confirmed: ['Person detected', 'nw-alert'],
    cooldown: ['Watching (quiet after an alert)', 'nw-on'],
    unavailable: ['Not watching: no picture', 'nw-warn'],
  };
  const POLL_MS = 30000;

  const $ = (id) => document.getElementById(id);
  const esc = (v) => (typeof escapeHtml === 'function' ? escapeHtml(v)
    : String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const toast = (m, k) => { if (typeof showToast === 'function') showToast(m, k); };
  const authed = () => (typeof canPoll === 'function' ? canPoll() : true);
  const authUrl = (u) => ((window.edgeAuth && typeof window.edgeAuth.authUrl === 'function') ? window.edgeAuth.authUrl(u) : u);

  const state = { cameraId: null, loaded: null, dirty: false, timer: null };

  async function getJ(url) {
    if (typeof getJSON === 'function') return getJSON(url, null);
    try { const r = await fetch(url); return r.ok ? await r.json() : null; } catch (_) { return null; }
  }

  async function send(method, url, body) {
    const res = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    if (!res.ok) {
      const d = data && data.detail;
      const msg = typeof d === 'string' ? d : Array.isArray(d) ? d.map((x) => x.msg).join('; ') : `HTTP ${res.status}`;
      throw new Error(res.status === 401 ? 'Your session has expired. Sign in again.' : msg);
    }
    return data;
  }

  // ------------------------------------------------------------- time

  /** The viewer's clock at a UTC instant: "21:43", "IST" (or "GMT+5:30"), offset minutes. */
  function viewerTime(utcIso) {
    const d = new Date(utcIso);
    if (Number.isNaN(d.getTime())) return null;
    let hhmm = '';
    let abbr = '';
    let day = '';
    try {
      hhmm = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(d);
      const part = new Intl.DateTimeFormat(undefined, { timeZoneName: 'short' }).formatToParts(d)
        .find((p) => p.type === 'timeZoneName');
      abbr = part ? part.value : '';
      day = new Intl.DateTimeFormat(undefined, { weekday: 'short' }).format(d);
    } catch (_) {
      hhmm = d.toTimeString().slice(0, 5);
    }
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    return { hhmm, abbr, day, date: `${y}-${m}-${dd}`, offset: -d.getTimezoneOffset() };
  }

  function storeDay(at) {
    const d = new Date(`${at.date}T12:00:00Z`);
    try { return new Intl.DateTimeFormat(undefined, { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' }).format(d); } catch (_) { return at.date; }
  }

  /** "02:13 AEST · 21:43 IST your time" (the viewer part only when their offset differs). */
  function whenHtml(at, withDay) {
    if (!at) return '';
    const day = withDay ? `${esc(storeDay(at))}, ` : '';
    let html = `<span class="nw-store-time">${day}${esc(at.label)}</span>`;
    const v = viewerTime(at.utc);
    if (v && v.offset !== at.utc_offset_min) {
      const vday = v.date !== at.date ? `${esc(v.day)} ` : '';
      html += ` <span class="nw-viewer-time">· ${vday}${esc(v.hhmm)}${v.abbr ? ` ${esc(v.abbr)}` : ''} your time</span>`;
    }
    return html;
  }

  function zoneLabel(z) {
    if (!z) return 'store time';
    const name = z.name || (z.source === 'host' ? "this device's zone" : '');
    return [z.abbreviation, name].filter(Boolean).join(' · ');
  }

  // ------------------------------------------------------------- settings section

  function sectionHtml() {
    return `
      <div class="form-section-title">Night watch</div>
      <div class="nw-settings" id="nwSettings">
        <label class="checkbox-label" for="nwEnabled"><input type="checkbox" id="nwEnabled" /> Watch this camera at night</label>
        <p class="dept-hint">While armed and nothing moves, the pose model is paused on this camera (its share of the
          GPU goes to the other cameras) and a light motion check runs instead. Movement starts a short person check:
          a person sends a <b>high alert</b> to this dashboard and paired phones; movement with nobody is only listed
          under Loss prevention. People counting and theft cues pause on this camera while it is armed.</p>
        <div class="form-grid-2col">
          <div class="form-group">
            <label for="nwStart">Start <span class="nw-zone" data-nw-zone>(store time)</span></label>
            <input type="time" class="form-input" id="nwStart" step="60" />
          </div>
          <div class="form-group">
            <label for="nwEnd">End <span class="nw-zone" data-nw-zone>(store time)</span></label>
            <input type="time" class="form-input" id="nwEnd" step="60" />
            <div class="dept-hint">An end at or before the start is the next morning.</div>
          </div>
        </div>
        <div class="form-group">
          <span class="nw-label" id="nwDaysLabel">Nights starting on</span>
          <div class="nw-days" role="group" aria-labelledby="nwDaysLabel">
            ${DAYS.map(([k, l]) => `<label class="nw-day" for="nwDay_${k}"><input type="checkbox" id="nwDay_${k}" data-nw-day="${k}" /> ${l}</label>`).join('')}
          </div>
        </div>
        <label class="checkbox-label" for="nwDark"><input type="checkbox" id="nwDark" /> Also watch whenever this camera is in IR / low light</label>
        <div class="form-grid-2col">
          <div class="form-group">
            <label for="nwSensitivity">Motion sensitivity</label>
            <select class="form-input" id="nwSensitivity">
              ${SENSITIVITY.map(([k, l]) => `<option value="${k}">${l}</option>`).join('')}
            </select>
          </div>
          <div class="form-group">
            <label for="nwCooldown">Quiet time after an alert (minutes)</label>
            <input type="number" class="form-input" id="nwCooldown" min="0.5" max="60" step="0.5" />
          </div>
        </div>
        <div class="nw-status" id="nwStatus" aria-live="polite"></div>
        <div class="nw-actions">
          <button type="button" class="btn btn-sm btn-primary" id="nwSave">Save night watch</button>
          <span class="form-status" id="nwSaveStatus" aria-live="polite"></span>
        </div>
      </div>`;
  }

  function statusHtml(st) {
    if (!st) return '';
    const [label, cls] = STATE[st.state] || [st.state, 'nw-off'];
    const parts = [`<span class="nw-state ${cls}">${esc(label)}</span>`];
    if (st.armed && st.armed_by === 'dark') parts.push('<span class="nw-meta">armed because the picture is dark</span>');
    if (st.enabled && st.next_disarm && (st.armed || st.in_window)) parts.push(`<span class="nw-meta">off at ${whenHtml(st.next_disarm, true)}</span>`);
    if (st.enabled && st.next_arm && !st.in_window) parts.push(`<span class="nw-meta">next on ${whenHtml(st.next_arm, true)}</span>`);
    if (st.enabled && st.in_window && !st.next_disarm) parts.push('<span class="nw-meta">every day, all day</span>');
    let html = parts.join(' ');
    if (st.note) html += `<div class="nw-note">${esc(st.note)}</div>`;
    return html;
  }

  function fill(view) {
    const c = view.config;
    $('nwEnabled').checked = !!c.enabled;
    $('nwStart').value = c.start;
    $('nwEnd').value = c.end;
    DAYS.forEach(([k]) => { $(`nwDay_${k}`).checked = (c.days || []).includes(k); });
    $('nwDark').checked = !!c.when_dark;
    $('nwSensitivity').value = c.sensitivity;
    $('nwCooldown').value = Math.round((c.cooldown_sec / 60) * 10) / 10;
    const zone = `(${zoneLabel(view.store_timezone)})`;
    document.querySelectorAll('[data-nw-zone]').forEach((n) => { n.textContent = zone; });
    $('nwStatus').innerHTML = view.configured || c.enabled ? statusHtml(view.status) : '<span class="nw-meta">Off: never set up on this camera.</span>';
    setDisabled(!c.enabled);
  }

  function setDisabled(off) {
    const box = $('nwSettings');
    if (box) box.classList.toggle('nw-disabled', off);
  }

  function readForm() {
    const start = $('nwStart').value;
    const end = $('nwEnd').value;
    if (!/^\d{2}:\d{2}$/.test(start) || !/^\d{2}:\d{2}$/.test(end)) throw new Error('Enter a start and an end time.');
    const days = DAYS.map(([k]) => k).filter((k) => $(`nwDay_${k}`).checked);
    const enabled = $('nwEnabled').checked;
    if (enabled && !days.length && !$('nwDark').checked) throw new Error('Pick at least one night, or turn on the IR / low light option.');
    const minutes = parseFloat($('nwCooldown').value);
    if (!Number.isFinite(minutes) || minutes < 0.5 || minutes > 60) throw new Error('Quiet time must be 0.5 to 60 minutes.');
    return {
      enabled, start, end, days,
      when_dark: $('nwDark').checked,
      sensitivity: $('nwSensitivity').value,
      cooldown_sec: Math.round(minutes * 60),
    };
  }

  function setSaveStatus(msg, error) {
    const s = $('nwSaveStatus');
    if (!s) return;
    s.textContent = msg;
    s.classList.toggle('form-status-error', !!error);
  }

  async function save(cameraId, quiet) {
    let body;
    try { body = readForm(); } catch (e) { setSaveStatus(e.message, true); if (quiet) toast(`Night watch not saved: ${e.message}`); return false; }
    const btn = $('nwSave');
    if (btn) btn.disabled = true;
    setSaveStatus('Saving…', false);
    try {
      const view = await send('PUT', `/api/v1/cameras/${encodeURIComponent(cameraId)}/night-watch`, body);
      state.loaded = view;
      state.dirty = false;
      if (state.cameraId === cameraId && $('nwSettings')) fill(view);
      setSaveStatus(body.enabled ? 'Saved. The camera follows it from its next frame.' : 'Saved: night watch is off on this camera.', false);
      refreshCard();
      return true;
    } catch (e) {
      setSaveStatus(`Not saved: ${e.message}`, true);
      if (quiet) toast(`Night watch not saved: ${e.message}`);
      return false;
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function mount(cameraId) {
    const host = $('nightWatchMount');
    if (!host) return;
    state.cameraId = cameraId;
    state.loaded = null;
    state.dirty = false;
    host.innerHTML = sectionHtml();
    $('nwStatus').innerHTML = '<span class="nw-meta">Loading…</span>';
    const view = await getJ(`/api/v1/cameras/${encodeURIComponent(cameraId)}/night-watch`);
    if (state.cameraId !== cameraId) return;
    if (!view) {
      // Nothing is saved from a form that never loaded.
      $('nwStatus').innerHTML = '<span class="nw-note">Night watch settings could not be loaded.</span>';
      $('nwSave').disabled = true;
      return;
    }
    state.loaded = view;
    fill(view);
    host.querySelectorAll('input, select').forEach((n) => {
      n.addEventListener('input', () => { state.dirty = true; setSaveStatus('Not saved yet.', false); });
      n.addEventListener('change', () => { state.dirty = true; });
    });
    $('nwEnabled').addEventListener('change', () => setDisabled(!$('nwEnabled').checked));
    $('nwSave').addEventListener('click', () => save(cameraId, false));
  }

  function hookSettingsModal() {
    const orig = window.openCameraConfigModal;
    if (typeof orig !== 'function' || orig.__nightWatch) return;
    const wrapped = async function (cameraId) {
      const out = await orig.apply(this, arguments);
      await mount(cameraId);
      return out;
    };
    wrapped.__nightWatch = true;
    Object.keys(orig).forEach((k) => { wrapped[k] = orig[k]; });
    window.openCameraConfigModal = wrapped;
    // Inline onclick handlers resolve the global binding at click time.
    try { openCameraConfigModal = wrapped; } catch (_) { /* declared as function: window binding suffices */ }
    // The dialog's own Save also saves an edited night watch section.
    const form = $('formCameraConfig');
    if (form) {
      form.addEventListener('submit', () => {
        if (state.dirty && state.loaded && state.cameraId) save(state.cameraId, true);
      });
    }
  }

  // ------------------------------------------------------------- card (Loss prevention)

  function cardVisible() {
    const view = $('tab-theft');
    return !!(view && view.classList.contains('active') && document.visibilityState === 'visible');
  }

  function summaryHtml(s) {
    const z = s.store_timezone || {};
    const now = s.now ? `<div class="nw-clock">Store time now: ${whenHtml(s.now, true)} <span class="nw-meta">(${esc(zoneLabel(z))})</span></div>` : '';
    if (!s.cameras || !s.cameras.length) {
      return `${now}<div class="fp-empty">Night watch is off on every camera. Turn it on in a camera's Settings (Cameras, then the camera's ⚙).</div>`;
    }
    const armed = s.cameras.filter((c) => (s.armed_now || []).includes(c.camera_id));
    let head;
    if (armed.length) {
      head = `<div class="nw-headline"><b>${armed.length}</b> of ${s.cameras.length} camera${s.cameras.length === 1 ? '' : 's'} watched now.</div>`;
    } else {
      head = `<div class="nw-headline">No camera is being watched right now.${s.next_arm ? ` Next: ${whenHtml(s.next_arm, true)}.` : ''}</div>`;
    }
    const rows = s.cameras.map((c) => {
      const [label, cls] = STATE[c.state] || [c.state, 'nw-off'];
      const next = c.armed || c.in_window
        ? (c.next_disarm ? `off at ${whenHtml(c.next_disarm, false)}` : 'all day')
        : (c.next_arm ? `on at ${whenHtml(c.next_arm, true)}` : '');
      const when = c.days && c.days.length === 7 ? 'every night' : (c.days || []).map((d) => d.slice(0, 1).toUpperCase() + d.slice(1)).join(', ');
      return `<li class="nw-row">
          <span class="nw-cam">${esc(c.camera_name)}</span>
          <span class="nw-state ${cls}">${esc(label)}</span>
          <span class="nw-meta">${esc(c.window || '')}${when ? `, ${esc(when)}` : ''}${c.when_dark ? ', and when dark' : ''}</span>
          ${next ? `<span class="nw-meta">${next}</span>` : ''}
          ${c.note ? `<span class="nw-note">${esc(c.note)}</span>` : ''}
        </li>`;
    }).join('');
    return `${now}${head}<ul class="nw-list">${rows}</ul>`;
  }

  function eventsHtml(list) {
    if (!list || !list.length) return '<div class="fp-empty">No night events recorded.</div>';
    return `<ul class="nw-list">${list.map((e) => {
      const person = e.event_type === 'NIGHT_INTRUSION';
      const badge = person ? '<span class="badge badge-danger">Person</span>' : '<span class="badge badge-neutral">Motion</span>';
      const thumb = e.snapshot_url
        ? `<a class="nw-thumb-link" href="${esc(authUrl(e.snapshot_url))}" target="_blank" rel="noopener" title="Open the evidence image"><img class="nw-thumb" src="${esc(authUrl(e.snapshot_url))}" alt="Evidence image" loading="lazy"></a>`
        : (e.evidence_expired
          ? '<span class="nw-thumb nw-thumb-empty" title="Deleted by the evidence storage limit (oldest first)">Expired</span>'
          : '<span class="nw-thumb nw-thumb-empty">No image</span>');
      const clip = e.clip_url ? ` <a class="btn btn-sm" href="${esc(authUrl(e.clip_url))}" target="_blank" rel="noopener">Clip</a>` : '';
      return `<li class="nw-event ${person ? 'nw-event-person' : ''}">
          ${thumb}
          <div class="nw-event-body">
            <div class="nw-event-head">${badge} <span class="nw-cam">${esc(e.camera_name || e.camera_id)}</span></div>
            <div class="nw-event-time">${whenHtml(e.at, true)}</div>
            ${e.body ? `<div class="nw-meta">${esc(e.body)}</div>` : ''}${clip}
          </div>
        </li>`;
    }).join('')}</ul>`;
  }

  async function refreshCard(force) {
    const box = $('nwSummary');
    const evBox = $('nwEvents');
    if (!box || !evBox || !authed()) return;
    if (!force && !cardVisible()) return;
    const [summary, events] = await Promise.all([getJ('/api/v1/night-watch'), getJ('/api/v1/night-watch/events?limit=20')]);
    box.innerHTML = summary ? summaryHtml(summary) : '<div class="fp-empty">Night watch status is not available.</div>';
    evBox.innerHTML = events ? eventsHtml(events.events) : '<div class="fp-empty">Night events could not be loaded.</div>';
  }

  function init() {
    hookSettingsModal();
    const card = $('nightWatchCard');
    if (card) {
      card.addEventListener('click', (e) => {
        const b = e.target.closest('[data-nw="refresh"]');
        if (b) { e.preventDefault(); refreshCard(true); }
      });
    }
    window.addEventListener('edge:tab', (e) => { if (e.detail && e.detail.tab === 'loss') refreshCard(true); });
    document.addEventListener('visibilitychange', () => { if (cardVisible()) refreshCard(); });
    clearInterval(state.timer);
    state.timer = setInterval(() => refreshCard(), POLL_MS);
    if (cardVisible()) refreshCard(true);
  }

  window.edgeNightWatch = { mount, save, refresh: () => refreshCard(true), whenHtml, viewerTime };

  if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') window.edgeAuth.onReady(init);
  else if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
