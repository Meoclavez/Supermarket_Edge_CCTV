/**
 * Entrances card (Analytics tab): today's in/out per Camera Studio tripwire,
 * from GET /api/v1/analytics/footfall/tripwires. A tripwire with no crossing
 * today shows a dash, never 0. "Inside now" is entries minus exits on
 * footfall-counting lines since store-local midnight and is labelled as an
 * estimate: a missed or doubled crossing shifts it.
 */
(function () {
  'use strict';

  const DASH = '—';
  let timer = null;

  function el(id) { return document.getElementById(id); }
  function esc(v) {
    return String(v === null || v === undefined ? '' : v)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function signedOn() {
    return !(window.edgeAuth && typeof window.edgeAuth.isAuthenticated === 'function') || window.edgeAuth.isAuthenticated();
  }
  function setVal(id, v) {
    const n = el(id);
    if (!n) return;
    const has = typeof v === 'number' && Number.isFinite(v);
    n.textContent = has ? v.toLocaleString() : DASH;
    n.classList.toggle('metric-unobserved', !has);
  }
  function cell(v) {
    return typeof v === 'number' ? v.toLocaleString() : `<span class="metric-unobserved">${DASH}</span>`;
  }

  const SOURCE_LABEL = {
    tripwire: 'FOOTFALL: ENTRANCE LINES',
    zone_visits: 'FOOTFALL: ZONE VISITS',
    tracks: 'FOOTFALL: TRACKS',
  };

  async function refresh() {
    const card = el('entrancesCard');
    if (!card || !signedOn()) return;
    let data = null;
    let status = 0;
    try {
      const r = await fetch('/api/v1/analytics/footfall/tripwires?bucket=hour');
      status = r.status;
      data = r.ok ? await r.json() : null;
    } catch (e) { data = null; }

    const body = el('entrancesTableBody');
    const note = el('entrancesNote');
    const badge = el('entrancesSourceBadge');
    if (!data) {
      setVal('entrancesTotalIn', null); setVal('entrancesTotalOut', null); setVal('entrancesOccupancy', null);
      if (body) body.innerHTML = `<tr><td colspan="5"><div class="fp-empty">Entrance counts unavailable${status ? ` (HTTP ${status})` : ''}.</div></td></tr>`;
      if (note) note.textContent = '';
      return;
    }
    setVal('entrancesTotalIn', data.totals && data.totals.in);
    setVal('entrancesTotalOut', data.totals && data.totals.out);
    setVal('entrancesOccupancy', data.net_occupancy_estimate);
    if (badge) {
      badge.textContent = SOURCE_LABEL[data.footfall_source] || 'FOOTFALL: NOT OBSERVED';
      badge.classList.toggle('badge-green', data.footfall_source === 'tripwire');
    }
    const lines = data.tripwires || [];
    if (body) {
      body.innerHTML = lines.length
        ? lines.map((t) => `
            <tr data-tripwire-id="${esc(t.tripwire_id)}">
              <td>${esc(t.name || t.tripwire_id)}${t.counts_footfall ? '' : ' <span class="badge">not footfall</span>'}${t.configured === false ? ' <span class="badge">deleted</span>' : ''}${t.enabled === false ? ' <span class="badge">off</span>' : ''}</td>
              <td>${esc(t.camera_id || DASH)}</td>
              <td class="entrances-in">${cell(t.in)}</td>
              <td class="entrances-out">${cell(t.out)}</td>
              <td>${cell(t.net)}</td>
            </tr>`).join('')
        : '<tr><td colspan="5"><div class="fp-empty">No tripwires yet. Draw one across each entrance in Camera Studio (Tripwire tool) to count people in and out.</div></td></tr>';
    }
    if (note) {
      note.textContent = data.observed
        ? `"Inside now" is an estimate: ${data.estimate_note}`
        : (lines.length ? 'No crossings recorded today on footfall-counting lines.' : '');
    }
  }

  function start() {
    refresh();
    clearInterval(timer);
    timer = setInterval(() => {
      const tab = el('tab-analytics');
      if (tab && tab.classList.contains('active') && !document.hidden) refresh();
    }, 10000);
  }

  window.addEventListener('edge:tab', (e) => { if (e.detail && e.detail.tab === 'analytics') start(); });
  window.entrancesCard = { refresh };
  const boot = () => {
    const tab = el('tab-analytics');
    if (tab && tab.classList.contains('active')) start();
  };
  // Start only after auth.js has checked the stored token (edgeAuth.onReady).
  if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') window.edgeAuth.onReady(boot);
  else if (document.readyState !== 'loading') boot();
  else document.addEventListener('DOMContentLoaded', boot);
})();
