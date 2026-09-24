// Product shelf reaches (Retail Analytics tab): per-product and per-shelf-level
// hand reaches recorded by the pose pipeline, from
// GET /api/v1/analytics/products/summary (shelf_interactions rows, so the
// figures survive restarts). Conversion is shown only when POS rows exist;
// otherwise the column says "needs POS data". Nothing here is estimated.
// Loads on the edge:tab "analytics" event and refreshes while visible.
(function () {
  'use strict';

  const REFRESH_MS = 15000;
  const LEVELS = { TOP: 'Top shelf', MIDDLE: 'Eye level', BOTTOM: 'Bottom shelf', UNKNOWN: 'Level not set' };
  const $ = (id) => document.getElementById(id);
  let timer = null;

  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }
  const num = (v) => typeof v === 'number' && Number.isFinite(v);
  const DASHC = '—';

  async function fetchSummary() {
    if (typeof window.getJSON === 'function') return window.getJSON('/api/v1/analytics/products/summary', null);
    try {
      const r = await fetch('/api/v1/analytics/products/summary');
      return r.ok ? await r.json() : null;
    } catch (e) { return null; }
  }

  function levelLabel(p) {
    const base = LEVELS[p.shelf_level || 'UNKNOWN'] || p.shelf_level;
    return p.shelf_level && p.shelf_level_source === 'derived' ? `${base} (auto)` : base;
  }

  function render(data) {
    const host = $('productReachBody');
    const badge = $('productReachBadge');
    if (!host) return;
    if (!data) {
      host.innerHTML = '<div class="fp-empty">Product reach figures unavailable: the analytics service did not respond.</div>';
      if (badge) { badge.textContent = `REACHES: ${DASHC}`; badge.className = 'badge metric-unobserved'; }
      return;
    }
    const t = data.totals || {};
    if (badge) {
      badge.textContent = `REACHES TODAY: ${data.observed ? t.reaches : DASHC}`;
      badge.className = data.observed ? 'badge badge-green' : 'badge metric-unobserved';
    }
    const products = data.products || [];
    if (!products.length) {
      host.innerHTML = `<div class="fp-empty">${esc(data.message || 'No product shelf areas are mapped.')} `
        + 'Map shelves in <a href="/dashboard/studio">Camera Studio</a> with the Product shelf tool.</div>';
      return;
    }
    const levels = (data.shelf_levels || []).map((l) => `
      <div class="stat-box" data-level="${esc(l.level)}">
        <span class="stat-label">${esc(LEVELS[l.level] || l.level)} · ${l.zones} product${l.zones === 1 ? '' : 's'}</span>
        <span class="stat-val ${data.observed ? '' : 'metric-unobserved'}" style="font-size:18px">${data.observed ? esc(l.reaches) : DASHC}</span>
        <span class="stat-label">${num(l.reaches_per_zone) && data.observed ? `${l.reaches_per_zone} per product · ${num(l.share_pct) ? l.share_pct + '%' : DASHC}` : 'no reaches recorded'}</span>
      </div>`).join('');
    const rows = products.map((p) => {
      let conv;
      if (!data.pos_connected) conv = '<span class="metric-unobserved">needs POS data</span>';
      else if (num(p.units_per_reaching_shopper)) conv = `${p.pos_units_sold} sold · ${p.units_per_reaching_shopper.toFixed(2)} / reaching shopper`;
      else conv = `${num(p.pos_units_sold) ? p.pos_units_sold + ' sold' : DASHC} · no reaches`;
      return `
      <tr data-zone-id="${esc(p.zone_id)}">
        <td>${esc(p.name)}${p.configured ? '' : ' <span class="metric-unobserved">(zone deleted)</span>'}</td>
        <td>${esc(p.sku_id || DASHC)}</td>
        <td>${esc(levelLabel(p))}</td>
        <td data-col="reaches">${esc(p.reaches)}</td>
        <td>${esc(p.shoppers)}</td>
        <td>${num(p.avg_duration_sec) ? p.avg_duration_sec.toFixed(1) + ' s' : DASHC}</td>
        <td>${p.left_hand} / ${p.right_hand}</td>
        <td>${conv}</td>
      </tr>`;
    }).join('');
    host.innerHTML = `
      <div class="overview-strip" style="margin:10px 0">${levels}</div>
      <div style="overflow-x:auto;">
        <table class="friction-table" id="productReachTable">
          <thead><tr><th>Product</th><th>SKU</th><th>Shelf level</th><th>Reaches</th><th>Shoppers</th>
            <th>Avg hold</th><th>Left / right</th><th>Conversion (POS)</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      <div class="fp-empty" style="margin-top:8px">A reach is a hand entering a mapped shelf area; it cannot tell a pick from a put-back.
        ${data.observed ? '' : esc(data.message || '')}</div>`;
  }

  let retry = null;
  const tabActive = () => { const v = $('tab-analytics'); return !!(v && v.classList.contains('active')); };

  async function load() {
    // Signed-out (or still signing in): wait for the auth gate instead of
    // reporting the service as unavailable.
    if (window.edgeAuth && typeof window.edgeAuth.isAuthenticated === 'function' && !window.edgeAuth.isAuthenticated()) {
      clearTimeout(retry);
      retry = setTimeout(() => { if (tabActive()) load(); }, 1000);
      return;
    }
    render(await fetchSummary());
  }

  function onTab(tab) {
    clearInterval(timer);
    timer = null;
    if (tab !== 'analytics') return;
    load();
    timer = setInterval(() => {
      if (!tabActive() || document.hidden) return;
      load();
    }, REFRESH_MS);
  }

  // Insights > Shoppers & footfall (the old 'analytics' tab).
  window.addEventListener('edge:tab', (e) => onTab(e.detail && (e.detail.tab === 'insights' && e.detail.sub === 'footfall' ? 'analytics' : e.detail.tab)));
  window.productReach = { load, render };
  const boot = () => { if (tabActive() && !timer) onTab('analytics'); };
  // Start only after auth.js has checked the stored token (edgeAuth.onReady).
  if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') window.edgeAuth.onReady(boot);
  else document.addEventListener('DOMContentLoaded', boot);
})();
