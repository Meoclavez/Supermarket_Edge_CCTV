/**
 * Edge AI CCTV - Supermarket Retail Intelligence Dashboard Controller
 * Handles Tab Navigation, Camera Matrix, Chart.js Visualizations, Action Center & Daily Digest
 */

let activeCameraFilter = 'ALL';
let currentDecimationFPS = 25;
let allCamerasList = [];
let allActionItems = [];
let hourlyChartInstance = null;
let demographicsChartInstance = null;

// ==========================================
// 1. Tab Navigation & Hash Routing
// ==========================================
function switchTab(tabId) {
  // Update Tab Buttons
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.classList.remove('active');
    if (btn.getAttribute('data-tab') === tabId) {
      btn.classList.add('active');
    }
  });

  // Update Tab Views
  document.querySelectorAll('.tab-view').forEach(view => {
    view.classList.remove('active');
  });

  const targetView = document.getElementById(`tab-${tabId}`);
  if (targetView) {
    targetView.classList.add('active');
  }

  // If switching to floorplan, trigger canvas resize
  if (tabId === 'floorplan' && window.storeFloorplanHUD) {
    setTimeout(() => window.storeFloorplanHUD.resizeCanvas(), 100);
  }

  // If switching to analytics, trigger chart update
  if (tabId === 'analytics') {
    setTimeout(() => initOrUpdateCharts(), 100);
  }

  // Update URL Hash without reload
  if (history.pushState) {
    history.pushState(null, null, `#${tabId}`);
  }
}

function initHashRouting() {
  const hash = window.location.hash.replace('#', '');
  const validTabs = ['matrix', 'floorplan', 'analytics', 'actions', 'digest'];
  if (validTabs.includes(hash)) {
    switchTab(hash);
  } else {
    switchTab('matrix');
  }
}

// ==========================================
// 2. Telemetry & Overview KPI Polling
// ==========================================
async function fetchStoreKPIs() {
  try {
    const res = await fetch('/api/v1/analytics/overview');
    if (!res.ok) return;
    const data = await res.json();

    // Update Top Strip Elements
    const footfallEl = document.getElementById('kpiFootfallToday');
    const shoppersNowEl = document.getElementById('kpiActiveShoppers');
    const dwellEl = document.getElementById('kpiAvgDwell');
    const convEl = document.getElementById('kpiConversion');
    const phiEl = document.getElementById('kpiLostSalesPhi');
    const queueEl = document.getElementById('kpiAvgQueueWait');
    const alertCountBadge = document.getElementById('tabActionCountBadge');

    if (footfallEl) footfallEl.textContent = data.today_footfall.toLocaleString();
    if (shoppersNowEl) shoppersNowEl.textContent = data.active_shoppers_now;
    if (dwellEl) dwellEl.textContent = `${data.avg_dwell_minutes}m`;
    if (convEl) convEl.textContent = `${data.conversion_rate_percent}%`;
    if (phiEl) {
      phiEl.textContent = `ϕ ${data.lost_sales_index_phi}`;
      phiEl.style.color = data.lost_sales_index_phi > 0.5 ? 'var(--accent-red)' : 'var(--accent-orange)';
    }
    if (queueEl) queueEl.textContent = `${data.queue_avg_wait_minutes}m`;
    if (alertCountBadge) alertCountBadge.textContent = data.active_ai_alerts;
  } catch (e) {}
}

async function fetchSystemTelemetry() {
  try {
    const hwRes = await fetch('/api/v1/system/hardware');
    const hw = await hwRes.json();
    const decoderEl = document.getElementById('decoderVal');
    const engineEl = document.getElementById('engineVal');
    if (decoderEl) decoderEl.textContent = `${hw.decoder_type.toUpperCase()} (${hw.device_name})`;
    if (engineEl) engineEl.textContent = hw.inference_backend.toUpperCase();

    const stRes = await fetch('/api/v1/system/stats');
    const st = await stRes.json();
    const cpuEl = document.getElementById('cpuRamVal');
    const shmEl = document.getElementById('shmVal');
    if (cpuEl) cpuEl.textContent = `${st.cpu_usage_percent}% CPU | ${st.ram_used_gb} / ${st.ram_total_gb} GB RAM`;
    if (shmEl) shmEl.textContent = `${hw.ring_buffer_seconds}s Pre-Roll (${st.shm_buffer_used_mb} MB SHM)`;
  } catch (e) {}
}

// ==========================================
// 3. Tab 1: Live Camera Matrix (28 Channels)
// ==========================================
async function loadCamerasMatrix() {
  try {
    const res = await fetch('/api/v1/cameras');
    const data = await res.json();
    allCamerasList = data.cameras || [];

    const countBadge = document.getElementById('matrixCamCountBadge');
    if (countBadge) countBadge.textContent = allCamerasList.length;

    renderCameraGrid();
  } catch (e) {}
}

function filterCameras(category, btnElement) {
  activeCameraFilter = category;
  document.querySelectorAll('#cameraFilterChips .filter-chip').forEach(c => c.classList.remove('active'));
  if (btnElement) btnElement.classList.add('active');
  renderCameraGrid();
}

function searchCameras(searchTerm) {
  renderCameraGrid(searchTerm.toLowerCase().trim());
}

function setDecimationFPS(fps, btnElement) {
  currentDecimationFPS = fps;
  document.querySelectorAll('#fpsFilterChips .filter-chip').forEach(c => c.classList.remove('active'));
  if (btnElement) btnElement.classList.add('active');
  showToast(`Stream FPS decimation set to: ${fps} FPS`);
  renderCameraGrid();
}

function renderCameraGrid(query = '') {
  const container = document.getElementById('cameraMatrixContainer');
  if (!container) return;

  const filtered = allCamerasList.filter(cam => {
    // Category match
    let catMatch = true;
    if (activeCameraFilter === 'ENTRANCE') catMatch = cam.id.includes('entrance') || cam.id.includes('cart') || cam.id.includes('foyer');
    else if (activeCameraFilter === 'AISLES') catMatch = cam.id.includes('aisle');
    else if (activeCameraFilter === 'FRESH') catMatch = cam.id.includes('produce') || cam.id.includes('bakery') || cam.id.includes('deli');
    else if (activeCameraFilter === 'POS') catMatch = cam.id.includes('pos') || cam.id.includes('cust_service');
    else if (activeCameraFilter === 'BACKROOM') catMatch = cam.id.includes('dock') || cam.id.includes('liquor');

    // Query match
    const queryMatch = !query || cam.name.toLowerCase().includes(query) || cam.location.toLowerCase().includes(query) || cam.id.toLowerCase().includes(query);
    return catMatch && queryMatch;
  });

  container.innerHTML = '';
  if (filtered.length === 0) {
    container.innerHTML = '<div style="grid-column: 1/-1; padding: 40px; text-align: center; color: var(--text-dim);">No matching camera feeds found.</div>';
    return;
  }

  filtered.forEach(cam => {
    const card = document.createElement('div');
    card.className = 'camera-card';

    // Tailor synthetic AI detection tags per camera
    let aiTag = '🧍 1 Person';
    if (cam.id.includes('entrance')) aiTag = '⚡ Footfall: In 142 / Out 118';
    else if (cam.id.includes('pos')) aiTag = '🛒 Queue: 2 | Wait: 1m15s';
    else if (cam.id.includes('liquor')) aiTag = '🚨 Dwell Alert: 3m45s';
    else if (cam.id.includes('produce')) aiTag = '👥 4 Persons Browsing';

    card.innerHTML = `
      <div class="camera-header">
        <span class="camera-title" title="${cam.name}">📹 ${cam.name}</span>
        <span class="badge badge-green" style="font-size: 9.5px;">ONLINE</span>
      </div>
      <div class="camera-viewport">
        <img src="/api/v1/cameras/${cam.id}/snapshot" alt="${cam.name}" loading="lazy" />
        <div class="camera-overlay-top">
          <span class="cam-hud-badge">${cam.location}</span>
          <span class="cam-hud-badge" style="color: var(--accent-green);">${currentDecimationFPS} FPS</span>
        </div>
        <div class="camera-overlay-bottom">
          <span class="cam-hud-badge" style="background: rgba(0,240,255,0.25); border-color: var(--accent-cyan);">${aiTag}</span>
          <span class="cam-hud-badge">18ms WebRTC</span>
        </div>
      </div>
      <div class="camera-footer">
        <span class="cam-meta-text">H.265 VA-API | 2.4 Mbps</span>
        <a href="/dashboard/studio?camera_id=${cam.id}" class="btn btn-primary btn-sm">🎨 Studio & Zones</a>
      </div>
    `;
    container.appendChild(card);
  });
}

// ==========================================
// 4. Tab 3: Retail Analytics & Funnels
// ==========================================
async function initOrUpdateCharts() {
  try {
    const res = await fetch('/api/v1/analytics/funnels');
    if (!res.ok) return;
    const data = await res.json();

    // 1. Render Hourly Footfall Chart (Chart.js)
    const hourlyCanvas = document.getElementById('hourlyFootfallChart');
    if (hourlyCanvas && typeof Chart !== 'undefined') {
      if (hourlyChartInstance) hourlyChartInstance.destroy();
      hourlyChartInstance = new Chart(hourlyCanvas.getContext('2d'), {
        type: 'line',
        data: {
          labels: data.hourly_footfall.labels,
          datasets: [
            {
              label: 'Today (Live CCTV)',
              data: data.hourly_footfall.today,
              borderColor: '#00f0ff',
              backgroundColor: 'rgba(0, 240, 255, 0.12)',
              fill: true,
              tension: 0.35,
              borderWidth: 2.5,
              pointBackgroundColor: '#00f0ff'
            },
            {
              label: 'Yesterday',
              data: data.hourly_footfall.yesterday,
              borderColor: 'rgba(139, 148, 158, 0.6)',
              borderDash: [5, 5],
              fill: false,
              tension: 0.35,
              borderWidth: 1.5,
              pointRadius: 0
            },
            {
              label: '7-Day Average',
              data: data.hourly_footfall.seven_day_avg,
              borderColor: '#00ff9d',
              fill: false,
              tension: 0.35,
              borderWidth: 1.5,
              pointRadius: 0
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { labels: { color: '#f0f6fc', font: { family: 'Plus Jakarta Sans', size: 11 } } },
            tooltip: { mode: 'index', intersect: false }
          },
          scales: {
            x: { grid: { color: 'rgba(255, 255, 255, 0.05)' }, ticks: { color: '#8b949e', font: { family: 'JetBrains Mono', size: 10 } } },
            y: { grid: { color: 'rgba(255, 255, 255, 0.05)' }, ticks: { color: '#8b949e', font: { family: 'JetBrains Mono', size: 10 } } }
          }
        }
      });
    }

    // 2. Render Demographics Doughnut Chart
    const demoCanvas = document.getElementById('demographicsChart');
    if (demoCanvas && typeof Chart !== 'undefined') {
      if (demographicsChartInstance) demographicsChartInstance.destroy();
      demographicsChartInstance = new Chart(demoCanvas.getContext('2d'), {
        type: 'doughnut',
        data: {
          labels: data.demographics.groups.map(g => g.type),
          datasets: [{
            data: data.demographics.groups.map(g => g.percentage),
            backgroundColor: ['#00f0ff', '#00ff9d', '#ffaa00', '#9d4edd'],
            borderColor: '#0e1422',
            borderWidth: 2
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { position: 'right', labels: { color: '#f0f6fc', font: { family: 'Plus Jakarta Sans', size: 11 } } }
          }
        }
      });
    }

    // 3. Render Conversion Funnel HTML Bars
    const funnelContainer = document.getElementById('funnelStagesContainer');
    if (funnelContainer) {
      funnelContainer.innerHTML = data.funnel_stages.map(st => `
        <div class="funnel-step">
          <div class="funnel-step-header">
            <span>${st.stage}</span>
            <span style="font-family: var(--font-mono); color: var(--accent-cyan);">${st.count.toLocaleString()} (${st.percentage}%)</span>
          </div>
          <div class="funnel-bar-track">
            <div class="funnel-bar-fill" style="width: ${st.percentage}%;">
              ${st.percentage > 20 ? st.percentage + '%' : ''}
            </div>
          </div>
        </div>
      `).join('');
    }

    // 4. Render Lost Sales Top 5 Table
    const phiTableBody = document.getElementById('lostSalesTableBody');
    if (phiTableBody) {
      phiTableBody.innerHTML = data.lost_sales_index_phi_top5.map(item => `
        <tr>
          <td style="font-weight: 700; color: #fff;">${item.zone}</td>
          <td><span class="badge badge-red">ϕ ${item.phi}</span></td>
          <td style="font-family: var(--font-mono); font-weight: 800; color: var(--accent-orange);">${item.est_lost_revenue}</td>
          <td style="color: var(--text-dim);">${item.reason}</td>
        </tr>
      `).join('');
    }

    // 5. Load POS Queues
    loadQueueMetrics();
  } catch (e) {}
}

async function loadQueueMetrics() {
  try {
    const res = await fetch('/api/v1/analytics/queues');
    if (!res.ok) return;
    const data = await res.json();
    const container = document.getElementById('checkoutQueuesContainer');
    if (!container) return;

    container.innerHTML = data.registers.map(r => `
      <div class="queue-card ${r.status === 'SURGE_ALERT' ? 'surge' : ''}">
        <div style="display: flex; justify-content: space-between; align-items: center;">
          <span style="font-size: 12px; font-weight: 700; color: #fff;">${r.name}</span>
          <span class="badge ${r.status === 'SURGE_ALERT' ? 'badge-red' : (r.status === 'STANDBY' ? 'badge-orange' : 'badge-green')}" style="font-size: 9px;">${r.status}</span>
        </div>
        <div style="display: flex; justify-content: space-between; font-size: 11px;">
          <span style="color: var(--text-dim);">Queue Depth:</span>
          <span style="font-family: var(--font-mono); font-weight: 800; color: var(--accent-cyan);">${r.queue_count} Shoppers</span>
        </div>
        <div style="display: flex; justify-content: space-between; font-size: 11px;">
          <span style="color: var(--text-dim);">Avg Wait Time:</span>
          <span style="font-family: var(--font-mono); font-weight: 700; color: ${r.avg_wait_sec > 180 ? 'var(--accent-red)' : 'var(--accent-green)'};">${Math.round(r.avg_wait_sec / 60 * 10) / 10} mins</span>
        </div>
        <div style="font-size: 10px; color: var(--text-dim); border-top: 1px solid rgba(255,255,255,0.05); padding-top: 4px;">
          Staff: ${r.cashier} | ${r.speed_items_per_min} items/min
        </div>
      </div>
    `).join('');
  } catch (e) {}
}

// ==========================================
// 5. Tab 4: AI Decision Action Center
// ==========================================
async function loadActionCenter() {
  try {
    const res = await fetch('/api/v1/analytics/actions');
    if (!res.ok) return;
    const data = await res.json();
    allActionItems = data.actions || [];
    renderActionCards('ALL');
  } catch (e) {}
}

function filterActionCards(category, btnElement) {
  document.querySelectorAll('#actionFilterChips .filter-chip').forEach(c => c.classList.remove('active'));
  if (btnElement) btnElement.classList.add('active');
  renderActionCards(category);
}

function renderActionCards(category) {
  const container = document.getElementById('actionCardsListContainer');
  if (!container) return;

  const filtered = allActionItems.filter(act => {
    if (category === 'ALL') return true;
    if (category === 'OPEN') return act.status === 'OPEN';
    if (category === 'SCHEDULED') return act.status === 'SCHEDULED';
    if (category === 'DONE') return act.status === 'DONE';
    return act.category === category || act.severity === category;
  });

  container.innerHTML = '';
  if (filtered.length === 0) {
    container.innerHTML = '<div style="padding: 30px; text-align: center; color: var(--text-dim);">No action recommendations in this category.</div>';
    return;
  }

  filtered.forEach(act => {
    const card = document.createElement('div');
    card.className = `action-card severity-${act.severity} status-${act.status}`;

    card.innerHTML = `
      <div class="action-main">
        <div class="action-header-line">
          <span class="badge ${act.severity === 'CRITICAL' ? 'badge-red' : (act.severity === 'HIGH' ? 'badge-orange' : 'badge-green')}" style="font-size: 9.5px;">${act.severity}</span>
          <span class="badge badge-purple" style="font-size: 9.5px;">${act.category}</span>
          <span class="action-title">${act.title}</span>
          <span style="font-size: 10px; color: var(--text-dim); margin-left: auto; font-family: var(--font-mono);">${act.timestamp}</span>
        </div>
        <div class="action-metric-badge">${act.metric}</div>
        <div class="action-desc">${act.description}</div>
        <div class="action-rec">💡 Recommendation: ${act.recommendation}</div>
      </div>
      <div class="action-buttons-group">
        ${act.status !== 'DONE' ? `
          <button class="btn btn-success btn-sm" onclick="updateActionStatus('${act.id}', 'DONE')">✅ Mark Done</button>
          <button class="btn btn-sm" onclick="updateActionStatus('${act.id}', 'SCHEDULED')">📅 Schedule</button>
        ` : `
          <span class="badge badge-green">COMPLETED</span>
          <button class="btn btn-sm" onclick="updateActionStatus('${act.id}', 'OPEN')">🔄 Reopen</button>
        `}
        <a href="/dashboard/studio?camera_id=${act.camera_id}" class="btn btn-primary btn-sm">👁️ Camera</a>
      </div>
    `;
    container.appendChild(card);
  });
}

async function updateActionStatus(actionId, newStatus) {
  try {
    const res = await fetch(`/api/v1/analytics/actions/${actionId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: newStatus })
    });
    if (res.ok) {
      showToast(`Action updated to: ${newStatus}`);
      loadActionCenter();
      fetchStoreKPIs();
    }
  } catch (e) {
    showToast('Failed to update action.');
  }
}

// ==========================================
// 6. Tab 5: Executive Daily Digest
// ==========================================
async function loadExecutiveDigest() {
  try {
    const res = await fetch('/api/v1/analytics/digest');
    if (!res.ok) return;
    const data = await res.json();

    const titleEl = document.getElementById('digestReportTitle');
    const dateEl = document.getElementById('digestReportDate');
    const summaryEl = document.getElementById('digestSummaryText');
    const kpiContainer = document.getElementById('digestKpiGridContainer');
    const topZonesEl = document.getElementById('digestTopZonesList');
    const frictionZonesEl = document.getElementById('digestFrictionZonesList');
    const safetyEl = document.getElementById('digestSafetySummary');

    if (titleEl) titleEl.textContent = data.report_title;
    if (dateEl) dateEl.textContent = `${data.date} | Store: ${data.store_id}`;
    if (summaryEl) summaryEl.textContent = data.executive_summary;

    if (kpiContainer) {
      kpiContainer.innerHTML = Object.entries(data.kpi_scorecard).map(([k, v]) => `
        <div class="stat-box">
          <span class="stat-label">${k.replace(/_/g, ' ')}</span>
          <span class="stat-val" style="font-size: 14px;">${v}</span>
        </div>
      `).join('');
    }

    if (topZonesEl) {
      topZonesEl.innerHTML = data.top_performing_zones.map(z => `
        <li style="margin-bottom: 6px;"><strong>${z.zone}</strong>: ${z.notes} (Dwell: ${z.dwell}, Revenue: ${z.revenue_contribution})</li>
      `).join('');
    }

    if (frictionZonesEl) {
      frictionZonesEl.innerHTML = data.underperforming_friction_zones.map(z => `
        <li style="margin-bottom: 6px;"><strong>${z.zone}</strong>: <span style="color:var(--accent-red)">ϕ ${z.phi}</span> - ${z.notes}</li>
      `).join('');
    }

    if (safetyEl) {
      safetyEl.innerHTML = `
        <div>• <strong>Spill Hazards Cleared:</strong> ${data.safety_and_loss_prevention.spill_hazards_cleared}</div>
        <div>• <strong>Elderly Falls Detected:</strong> ${data.safety_and_loss_prevention.elderly_falls_prevented}</div>
        <div>• <strong>Night Intrusion Breaches:</strong> ${data.safety_and_loss_prevention.restricted_night_intrusions}</div>
        <div>• <strong>Hardware Tampering Alarms:</strong> ${data.safety_and_loss_prevention.tamper_alerts}</div>
      `;
    }
  } catch (e) {}
}

function printExecutiveDigest() {
  switchTab('digest');
  setTimeout(() => window.print(), 300);
}

// ==========================================
// 7. Modals & Scanner
// ==========================================
function openScannerModal() {
  const m = document.getElementById('scannerModal');
  if (m) m.style.display = 'flex';
  executeNetworkScan();
}

function closeScannerModal() {
  const m = document.getElementById('scannerModal');
  if (m) m.style.display = 'none';
}

async function executeNetworkScan() {
  const container = document.getElementById('discoveredSourcesList');
  if (!container) return;
  container.innerHTML = '<div style="padding: 10px; text-align: center; color: var(--accent-cyan);">Scanning subnet for RTSP, Dahua P2P & USB feeds...</div>';
  try {
    const res = await fetch('/api/v1/cameras/scan', { method: 'POST' });
    const data = await res.json();
    const sources = data.sources || [];
    container.innerHTML = '';

    if (sources.length === 0) {
      container.innerHTML = '<div style="padding: 10px; text-align: center; color: var(--text-dim);">No new devices discovered.</div>';
      return;
    }

    sources.forEach(src => {
      const item = document.createElement('div');
      item.style.cssText = 'background: rgba(0,0,0,0.35); border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 10px; display: flex; justify-content: space-between; align-items: center;';
      item.innerHTML = `
        <div>
          <div style="font-weight: 700; font-size: 12px; color: #fff;">${src.name}</div>
          <div style="font-size: 10.5px; color: var(--text-dim); font-family: var(--font-mono);">${src.url} [${src.type}]</div>
        </div>
        <a href="/dashboard/studio?camera_id=${src.id}" class="btn btn-primary btn-sm">Connect in Studio</a>
      `;
      container.appendChild(item);
    });
  } catch (e) {
    container.innerHTML = '<div style="padding: 10px; text-align: center; color: var(--accent-red);">Scan failed.</div>';
  }
}

function showToast(msg) {
  const t = document.getElementById('toast');
  if (!t) return;
  t.textContent = msg;
  t.style.display = 'block';
  setTimeout(() => { t.style.display = 'none'; }, 3000);
}

// ==========================================
// 8. Initialization & Real-Time Loops
// ==========================================
document.addEventListener('DOMContentLoaded', () => {
  initHashRouting();
  fetchSystemTelemetry();
  fetchStoreKPIs();
  loadCamerasMatrix();
  loadActionCenter();
  loadExecutiveDigest();

  // Polling Intervals
  setInterval(fetchSystemTelemetry, 3000);
  setInterval(fetchStoreKPIs, 4000);
});
