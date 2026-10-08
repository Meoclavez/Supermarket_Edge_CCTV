/**
 * Live behaviour: the people the cameras are watching right now.
 *
 * GET /api/v1/live/behaviour -> {tracks: [{camera_id, camera_name, track_id,
 * level: 'watch'|'alert', labels, pattern_score, incident_id, since}]}, only
 * watch/alert tracks, newest first. Polled every 2 s while the Loss
 * prevention tab (#liveBehaviourList) or the Cameras tab (#liveBehaviourStrip)
 * is on screen, never while the tab is hidden.
 *
 * A person at "alert" shows the alert level of the incident that fired
 * (critical / alert / watch / review) as a tier badge: from the row's own
 * ``tier`` when the box reports it, else from that incident in the review
 * queue (window.edgeLoss.incidentTier). No level known: no badge (never guessed).
 *
 * Clicking a person opens that camera enlarged on the Cameras tab with the
 * person outlined (window.openCameraLive, analytics.js). These are live cues
 * for staff to look at, never a finding of theft. When the box does not offer
 * this list (404) the panel says so; nothing is made up.
 */
(function () {
  'use strict';

  const API = '/api/v1/live/behaviour';
  const POLL_MS = 2000;
  const UNAVAILABLE_RETRY_MS = 60000;
  const STRIP_MAX = 6;

  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v === null || v === undefined ? '' : v).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

  const state = { tracks: [], status: 'unknown', reason: '', at: 0, timer: null, busy: false, sig: '' };

  function tabOn(id) {
    const t = $(id);
    return !!(t && t.classList.contains('active'));
  }
  function watching() {
    return !document.hidden && (tabOn('tab-theft') || tabOn('tab-matrix'));
  }
  function signedIn() {
    return typeof window.canPoll === 'function' ? window.canPoll() : true;
  }

  /** "since" as seconds ago: a unix time (s or ms) or an ISO string; null when unknown. */
  function secondsSince(since) {
    let t = null;
    if (isNum(since)) t = since > 1e12 ? since : since * 1000;
    else if (typeof since === 'string' && since) {
      let s = since.replace(' ', 'T');
      if (!/[zZ]|[+-]\d{2}:?\d{2}$/.test(s)) s += 'Z';
      const d = Date.parse(s);
      t = Number.isNaN(d) ? null : d;
    }
    return t === null ? null : Math.max(0, (Date.now() - t) / 1000);
  }

  function duration(s) {
    if (s === null) return '';
    if (typeof window.formatDuration === 'function') return window.formatDuration(s);
    return `${Math.round(s)} s`;
  }

  function levelWord(level) { return level === 'alert' ? 'Alert' : 'Watch'; }

  const TIERS = ['review', 'watch', 'alert', 'critical'];
  const TIER_LABEL = { review: 'Review', watch: 'Watch', alert: 'Alert', critical: 'Critical' };
  /** {tier, label, risk} of the incident fired on this person, or null (level "watch", or not known). */
  function tierInfo(t) {
    if (!t || t.level !== 'alert') return null;
    if (TIERS.includes(t.tier)) return { tier: t.tier, label: TIER_LABEL[t.tier], risk: isNum(t.risk_score) ? t.risk_score : null };
    const loss = window.edgeLoss;
    if (t.incident_id && loss && typeof loss.incidentTier === 'function') {
      try {
        const r = loss.incidentTier(t.incident_id);
        if (r && TIERS.includes(r.tier)) return { tier: r.tier, label: r.label || TIER_LABEL[r.tier], risk: isNum(r.risk) ? r.risk : null };
      } catch (_) { /* the review queue is not loaded */ }
    }
    return null;
  }
  function tierBadge(info) {
    return info ? `<span class="tier-badge tier-${info.tier} lb-tier" title="Alert level of the incident raised for this person">${esc(info.label)} level</span>` : '';
  }

  function rowHtml(t) {
    const level = t.level === 'alert' ? 'alert' : 'watch';
    const labels = Array.isArray(t.labels) ? t.labels.filter((x) => typeof x === 'string' && x) : [];
    const ago = secondsSince(t.since);
    const meta = [];
    if (ago !== null) meta.push(`for ${esc(duration(ago))}`);
    if (isNum(t.pattern_score)) meta.push(`pattern score ${t.pattern_score.toFixed(2)}`);
    const info = tierInfo(t);
    if (info && info.risk !== null) meta.push(`<span class="lb-risk">risk ${Math.round(info.risk * 100)}%</span>`);
    const cam = esc(t.camera_name || t.camera_id);
    const tierCls = info ? `lb-tier-${info.tier}` : '';
    return `<div class="lb-row lb-${level} ${tierCls}">
        <span class="lb-level lb-level-${level}">${levelWord(level)}</span>
        ${tierBadge(info)}
        <div class="lb-main">
          <div class="lb-title">${cam}</div>
          <div class="lb-labels">${labels.length ? labels.map(esc).join(' · ') : 'Live cue (no detail reported)'}</div>
          ${meta.length ? `<div class="lb-meta">${meta.join(' · ')}</div>` : ''}
        </div>
        <div class="lb-actions">
          ${t.incident_id ? `<button type="button" class="btn btn-secondary btn-sm" data-lb-incident="${esc(t.incident_id)}" title="An incident was raised for this person: open it in the review queue">Incident</button>` : ''}
          <button type="button" class="btn btn-sm btn-primary" data-lb-camera="${esc(t.camera_id)}" data-lb-track="${esc(t.track_id || '')}"
            title="Open ${cam} enlarged on the Cameras tab with this person outlined">Watch live</button>
        </div>
      </div>`;
  }

  function renderList() {
    const host = $('liveBehaviourList');
    if (!host) return;
    const note = $('liveBehaviourNote');
    let html;
    if (state.status === 'unavailable') {
      html = `<div class="fp-empty">Live behaviour is not available on this box: live AI data unavailable. ${esc(state.reason)}</div>`;
    } else if (state.status === 'denied') {
      html = `<div class="fp-empty">Live behaviour is not available for this sign-in. ${esc(state.reason)}</div>`;
    } else if (state.status === 'error' && !state.tracks.length) {
      html = `<div class="fp-empty">No answer from the box (${esc(state.reason)}). Retrying.</div>`;
    } else if (state.status === 'unknown') {
      html = '<div class="fp-empty">Loading…</div>';
    } else if (!state.tracks.length) {
      html = '<div class="fp-empty">Nobody is showing a watch or alert cue right now.</div>';
    } else {
      html = state.tracks.map(rowHtml).join('');
    }
    if (host.getAttribute('data-sig') !== html) {
      host.innerHTML = html;
      host.setAttribute('data-sig', html);
    }
    if (note) {
      const t = state.at ? new Date(state.at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '';
      note.textContent = state.status === 'ok' && t ? `Checked ${t}` : '';
    }
  }

  function renderStrip() {
    const host = $('liveBehaviourStrip');
    if (!host) return;
    let html = '';
    if (state.status === 'unavailable' || state.status === 'denied') {
      html = '<span class="lb-strip-title">Live behaviour</span><span class="lb-strip-off">live AI data unavailable</span>';
    } else if (state.tracks.length) {
      const chips = state.tracks.slice(0, STRIP_MAX).map((t) => {
        const level = t.level === 'alert' ? 'alert' : 'watch';
        const labels = Array.isArray(t.labels) ? t.labels.filter((x) => typeof x === 'string' && x) : [];
        const label = labels[0] || 'live cue';
        const info = tierInfo(t);
        const word = info ? `${info.label} level` : levelWord(level);
        const crit = info && info.tier === 'critical' ? 'lb-chip-critical' : '';
        const wordCls = info ? 'lb-chip-tier' : '';
        return `<button type="button" class="lb-chip lb-chip-${level} ${crit}" data-lb-camera="${esc(t.camera_id)}" data-lb-track="${esc(t.track_id || '')}"
          title="${esc(`${word}: ${labels.join(' · ') || 'live cue'} on ${t.camera_name || t.camera_id}. Click to watch.`)}">
          <span class="lb-chip-level ${wordCls}">${esc(word)}</span> ${esc(t.camera_name || t.camera_id)} · ${esc(label)}</button>`;
      }).join('');
      const more = state.tracks.length > STRIP_MAX ? `<span class="lb-strip-off">+${state.tracks.length - STRIP_MAX} more on Loss prevention</span>` : '';
      html = `<span class="lb-strip-title">Live behaviour</span>${chips}${more}`;
    }
    host.hidden = !html;
    if (host.getAttribute('data-sig') !== html) {
      host.innerHTML = html;
      host.setAttribute('data-sig', html);
    }
  }

  function render() { renderList(); renderStrip(); }

  function schedule(ms) {
    clearTimeout(state.timer);
    state.timer = watching() ? setTimeout(poll, ms) : null;
  }

  async function poll() {
    state.timer = null;
    if (!watching()) return;
    if (!signedIn()) { schedule(POLL_MS); return; }
    if ((state.status === 'unavailable' || state.status === 'denied') && Date.now() - state.at < UNAVAILABLE_RETRY_MS) {
      render();
      schedule(POLL_MS);
      return;
    }
    if (state.busy) return;
    state.busy = true;
    try {
      const res = await fetch(API, { cache: 'no-store' });
      if (res.status === 404 || res.status === 405 || res.status === 501) {
        state.tracks = [];
        state.status = 'unavailable';
        state.reason = `(GET ${API} answered HTTP ${res.status}.)`;
      } else if (res.status === 403) {
        state.tracks = [];
        state.status = 'denied';
        state.reason = '';
      } else if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      } else {
        const body = await res.json();
        state.tracks = (body && Array.isArray(body.tracks) ? body.tracks : [])
          .filter((t) => t && t.camera_id && (t.level === 'watch' || t.level === 'alert'));
        state.status = 'ok';
        state.reason = '';
      }
    } catch (e) {
      // A failed check is not "nobody": the old list is dropped and the reason shown.
      state.tracks = [];
      state.status = 'error';
      state.reason = (e && e.message) || 'no answer';
    } finally {
      state.busy = false;
      state.at = Date.now();
    }
    render();
    schedule(state.status === 'error' ? POLL_MS * 3 : POLL_MS);
  }

  function onClick(e) {
    const inc = e.target.closest('[data-lb-incident]');
    if (inc) {
      if (typeof window.switchTab === 'function') window.switchTab('loss');
      const id = inc.getAttribute('data-lb-incident');
      setTimeout(() => { if (window.edgeLoss && typeof window.edgeLoss.focusIncident === 'function') window.edgeLoss.focusIncident(id); }, 60);
      return;
    }
    const b = e.target.closest('[data-lb-camera]');
    if (!b) return;
    const cam = b.getAttribute('data-lb-camera');
    const track = b.getAttribute('data-lb-track') || null;
    if (typeof window.openCameraLive === 'function') window.openCameraLive(cam, { trackId: track });
  }

  function init() {
    ['liveBehaviourList', 'liveBehaviourStrip'].forEach((id) => { const n = $(id); if (n) n.addEventListener('click', onClick); });
    window.addEventListener('edge:tab', () => schedule(0));
    document.addEventListener('visibilitychange', () => schedule(0));
    render();
    schedule(0);
  }

  window.edgeLiveBehaviour = { refresh: () => schedule(0), _test: { secondsSince, tierInfo, rowHtml } };

  if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') window.edgeAuth.onReady(init);
  else if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
