/**
 * Edge AI CCTV - Supermarket Retail Intelligence Dashboard Controller
 * Handles Tab Navigation, Camera Matrix, Chart.js Visualizations, Action Center,
 * Loss Prevention & Theft AI, Camera Modal Config & Daily Digest
 */

let activeCameraFilter = 'ALL';
let currentDecimationFPS = 25;
let allCamerasList = [];
let allActionItems = [];
let hourlyChartInstance = null;
let demographicsChartInstance = null;
let activeTheftIncidentId = null;
let lastAlertedTheftId = null;

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

  // If switching to market_ai, trigger market predictions loader
  if (tabId === 'market_ai') {
    setTimeout(() => loadMarketPredictions(), 100);
  }

  // If switching to theft, fetch latest theft incidents
  if (tabId === 'theft') {
    setTimeout(() => fetchTheftIncidents(), 100);
  }

  // Update URL Hash without reload
  if (history.pushState) {
    history.pushState(null, null, `#${tabId}`);
  }
}

function initHashRouting() {
  const hash = window.location.hash.replace('#', '');
  const validTabs = ['matrix', 'floorplan', 'analytics', 'actions', 'market_ai', 'theft', 'digest'];
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
    const statsRes = await fetch('/api/v1/system/stats');
    const stats = await statsRes.json();

    const decoderBadge = document.getElementById('telemetryDecoderBadge');
    const inferenceBadge = document.getElementById('telemetryInferenceBadge');
    const cpuVal = document.getElementById('telemetryCpuVal');
    const ramVal = document.getElementById('telemetryRamVal');
    const uptimeVal = document.getElementById('telemetryUptimeVal');

    if (decoderBadge) decoderBadge.textContent = `${hw.decoder_type.toUpperCase()}`;
    if (inferenceBadge) inferenceBadge.textContent = `${hw.inference_backend.toUpperCase()}`;
    if (cpuVal) cpuVal.textContent = `${stats.cpu_usage_percent.toFixed(1)}%`;
    if (ramVal) ramVal.textContent = `${stats.ram_used_gb.toFixed(1)} / ${stats.ram_total_gb.toFixed(0)} GB`;
    if (uptimeVal) uptimeVal.textContent = `${Math.floor(stats.uptime_seconds / 3600)}h ${Math.floor((stats.uptime_seconds % 3600) / 60)}m`;
  } catch (e) {}
}

// ==========================================
// 3. Live Camera Matrix & FPS Decimation
// ==========================================
async function loadCamerasMatrix() {
  try {
    const res = await fetch('/api/v1/cameras');
    if (!res.ok) return;
    const data = await res.json();
    allCamerasList = data.cameras || [];

    const badge = document.getElementById('matrixCamCountBadge');
    if (badge) badge.textContent = allCamerasList.length;

    renderCameraGrid();
  } catch (e) {
    console.error('Failed to load cameras:', e);
  }
}

function filterCameras(category) {
  activeCameraFilter = category;
  document.querySelectorAll('.channel-pill').forEach(pill => {
    pill.classList.remove('active');
    if (pill.getAttribute('data-filter') === category) {
      pill.classList.add('active');
    }
  });
  renderCameraGrid();
}

function filterCamerasBySearch(query) {
  const q = (query || '').toLowerCase();
  const cards = document.querySelectorAll('.camera-card');
  cards.forEach(card => {
    const title = (card.getAttribute('data-cam-name') || '').toLowerCase();
    const loc = (card.getAttribute('data-cam-loc') || '').toLowerCase();
    if (title.includes(q) || loc.includes(q)) {
      card.style.display = 'flex';
    } else {
      card.style.display = 'none';
    }
  });
}

function setDecimationFPS(fps) {
  currentDecimationFPS = fps;
  document.querySelectorAll('.fps-btn').forEach(btn => {
    btn.classList.remove('active');
    if (parseInt(btn.getAttribute('data-fps')) === fps) {
      btn.classList.add('active');
    }
  });
  showToast(`⚡ Stream decimation set to ${fps} FPS`);
}

function renderCameraGrid() {
  const grid = document.getElementById('cameraMatrixGrid');
  if (!grid) return;

  grid.innerHTML = '';
  const filtered = allCamerasList.filter(cam => {
    if (activeCameraFilter === 'ALL') return true;
    if (activeCameraFilter === 'ENTRANCE') return cam.location.includes('Foyer') || cam.location.includes('Exit') || cam.location.includes('Entrance');
    if (activeCameraFilter === 'AISLE') return cam.location.includes('Aisle');
    if (activeCameraFilter === 'PRODUCE') return cam.location.includes('Produce') || cam.location.includes('Bakery') || cam.location.includes('Deli');
    if (activeCameraFilter === 'POS') return cam.location.includes('Checkout') || cam.location.includes('POS');
    if (activeCameraFilter === 'RESTRICTED') return cam.location.includes('Liquor') || cam.location.includes('Dock') || cam.location.includes('Backroom');
    return true;
  });

  filtered.forEach(cam => {
    const card = document.createElement('div');
    card.className = 'camera-card';
    card.setAttribute('data-cam-name', cam.name);
    card.setAttribute('data-cam-loc', cam.location);

    let hudBadge = '🟢 Normal';
    let hudClass = 'cam-hud-badge';
    if (cam.id.includes('pos')) {
      hudBadge = '🛒 Queue: 2 | Wait: 1m20s';
    } else if (cam.id.includes('liquor')) {
      hudBadge = '🚨 Dwell Alert: 3m10s';
    } else if (cam.id.includes('produce')) {
      hudBadge = '🥦 Engagement: 64%';
    } else if (cam.id.includes('entrance')) {
      hudBadge = '🧍 In: 142 | Out: 98';
    }

    card.innerHTML = `
      <div class="camera-card-header">
        <div>
          <div class="camera-title">${cam.name}</div>
          <div class="cam-meta-text">${cam.location}</div>
        </div>
        <span class="badge ${cam.status === 'ONLINE' ? 'badge-green' : 'badge-danger'}">● ${cam.status}</span>
      </div>

      <div class="camera-video-container">
        <img class="camera-img" src="/api/v1/cameras/${cam.id}/snapshot" alt="${cam.name}" loading="lazy" />
        <div class="camera-overlay-top">
          <span class="cam-hud-badge">1080p @ ${cam.fps} FPS</span>
          <span class="cam-hud-badge" style="color: var(--accent-green);">WebRTC: 18ms</span>
        </div>
        <div class="camera-overlay-bottom">
          <span class="${hudClass}">${hudBadge}</span>
        </div>
      </div>

      <div class="camera-footer">
        <button class="btn btn-sm" onclick="openCameraConfigModal('${cam.id}')">⚙️ Config</button>
        <a href="/dashboard/studio?camera_id=${cam.id}" class="btn btn-primary btn-sm">🎨 Studio & Zones</a>
      </div>
    `;
    grid.appendChild(card);
  });
}

// ==========================================
// 4. Retail Analytics, Funnels & Charts
// ==========================================
async function initOrUpdateCharts() {
  try {
    const res = await fetch('/api/v1/analytics/funnels');
    if (!res.ok) return;
    const data = await res.json();

    renderHourlyChart(data.hourly_footfall);
    renderDemographicsChart(data.demographics);
    renderConversionFunnel(data.funnel_stages);
    renderFrictionZones(data.lost_sales_friction_zones);
  } catch (e) {
    console.error('Failed to load chart data:', e);
  }
}

function renderHourlyChart(hourlyData) {
  const canvas = document.getElementById('hourlyFootfallChart');
  if (!canvas || typeof Chart === 'undefined') return;

  const ctx = canvas.getContext('2d');
  if (hourlyChartInstance) {
    hourlyChartInstance.destroy();
  }

  const hours = Object.keys(hourlyData.today || {});
  const todayCounts = Object.values(hourlyData.today || {});
  const yesterdayCounts = Object.values(hourlyData.yesterday || {});
  const baselineCounts = Object.values(hourlyData.seven_day_avg || {});

  hourlyChartInstance = new Chart(ctx, {
    type: 'line',
    data: {
      labels: hours,
      datasets: [
        {
          label: "Today's Live Counts",
          data: todayCounts,
          borderColor: '#00f0ff',
          backgroundColor: 'rgba(0, 240, 255, 0.1)',
          borderWidth: 2.5,
          fill: true,
          tension: 0.35,
          pointRadius: 3
        },
        {
          label: 'Yesterday',
          data: yesterdayCounts,
          borderColor: '#8b949e',
          borderWidth: 1.5,
          borderDash: [5, 5],
          fill: false,
          tension: 0.35,
          pointRadius: 0
        },
        {
          label: '7-Day Baseline',
          data: baselineCounts,
          borderColor: 'rgba(255, 170, 0, 0.6)',
          borderWidth: 1.5,
          fill: false,
          tension: 0.35,
          pointRadius: 0
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: '#8b949e', font: { family: 'JetBrains Mono', size: 10 } } }
      },
      scales: {
        x: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#8b949e', font: { size: 9.5 } } },
        y: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#8b949e', font: { size: 9.5 } } }
      }
    }
  });
}

function renderDemographicsChart(demographics) {
  const canvas = document.getElementById('demographicsChart');
  if (!canvas || typeof Chart === 'undefined') return;

  const ctx = canvas.getContext('2d');
  if (demographicsChartInstance) {
    demographicsChartInstance.destroy();
  }

  const groups = demographics.groups || [];
  const labels = groups.map(g => g.type);
  const values = groups.map(g => g.percentage);

  demographicsChartInstance = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: labels,
      datasets: [{
        data: values,
        backgroundColor: ['#00f0ff', '#00ff9d', '#ffaa00', '#ff0055'],
        borderWidth: 0
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { position: 'bottom', labels: { color: '#8b949e', font: { size: 10 } } }
      },
      cutout: '70%'
    }
  });
}

function renderConversionFunnel(stages) {
  const container = document.getElementById('funnelStagesList');
  if (!container) return;

  container.innerHTML = '';
  stages.forEach(stage => {
    const row = document.createElement('div');
    row.className = 'funnel-stage-row';
    row.innerHTML = `
      <div class="funnel-stage-info">
        <span class="funnel-stage-name">${stage.stage_name}</span>
        <span class="funnel-stage-counts">${stage.shoppers_count.toLocaleString()} shoppers (${stage.conversion_from_total_pct}%)</span>
      </div>
      <div class="funnel-bar-bg">
        <div class="funnel-bar-fill" style="width: ${stage.conversion_from_total_pct}%;"></div>
      </div>
    `;
    container.appendChild(row);
  });
}

function renderFrictionZones(frictionList) {
  const tbody = document.getElementById('frictionZonesTableBody');
  if (!tbody) return;

  tbody.innerHTML = '';
  frictionList.forEach(item => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td style="font-weight: 700; color: #fff;">${item.zone_name}</td>
      <td style="font-family: var(--font-mono); color: var(--accent-red); font-weight: 700;">ϕ ${item.phi_index}</td>
      <td style="font-family: var(--font-mono); color: var(--text-dim);">${item.avg_dwell}</td>
      <td style="font-size: 11px; color: #8b949e;">${item.root_cause}</td>
      <td style="font-family: var(--font-mono); color: var(--accent-orange); font-weight: 700;">$${item.est_revenue_loss_daily} AUD</td>
    `;
    tbody.appendChild(tr);
  });
}

// ==========================================
// 5. Loss Prevention & Theft AI Engine
// ==========================================
function playTheftAlertSound() {
  try {
    const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = 'sawtooth';
    osc.frequency.setValueAtTime(880, audioCtx.currentTime); // A5
    osc.frequency.exponentialRampToValueAtTime(440, audioCtx.currentTime + 0.3);
    gain.gain.setValueAtTime(0.15, audioCtx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.01, audioCtx.currentTime + 0.3);
    osc.connect(gain);
    gain.connect(audioCtx.destination);
    osc.start();
    osc.stop(audioCtx.currentTime + 0.35);
  } catch (e) {}
}

async function fetchTheftIncidents() {
  try {
    const res = await fetch('/api/v1/theft/incidents');
    if (!res.ok) return;
    const data = await res.json();
    const incidents = data.incidents || [];
    const kpis = data.kpis || {};

    // Update KPIs
    const activeAlertsEl = document.getElementById('kpiTheftActiveAlerts');
    const attemptsEl = document.getElementById('kpiTheftAttemptsToday');
    const preventedEl = document.getElementById('kpiTheftPreventedLoss');
    const riskDeptEl = document.getElementById('kpiTheftHighRiskDept');
    const theftTabBadge = document.getElementById('tabTheftCountBadge');

    if (activeAlertsEl) activeAlertsEl.textContent = kpis.active_alerts || 0;
    if (attemptsEl) attemptsEl.textContent = kpis.shoplifting_attempts_today || 0;
    if (preventedEl) preventedEl.textContent = `$${(kpis.prevented_loss_dollars || 0).toLocaleString()} AUD`;
    if (riskDeptEl) riskDeptEl.textContent = kpis.top_high_risk_department || 'LIQUOR';
    if (theftTabBadge) theftTabBadge.textContent = kpis.active_alerts || 0;

    // Check for ACTIVE theft alert to display in top floating banner
    const activeInc = incidents.find(i => i.status === 'ACTIVE');
    const banner = document.getElementById('theftAlertBanner');

    if (activeInc) {
      activeTheftIncidentId = activeInc.id;
      if (banner) {
        banner.style.display = 'flex';
        document.getElementById('theftBannerHeadline').textContent = `THEFT ALERT: ${activeInc.theft_type} detected on ${activeInc.camera_name} (${activeInc.department})`;
        document.getElementById('theftBannerConfidence').textContent = `Confidence: ${activeInc.confidence}%`;
        document.getElementById('theftBannerTime').textContent = `Time: ${activeInc.timestamp}`;
        document.getElementById('theftBannerDetails').textContent = activeInc.details;
      }

      // Beep if new active incident
      if (lastAlertedTheftId !== activeInc.id) {
        lastAlertedTheftId = activeInc.id;
        playTheftAlertSound();
      }
    } else {
      if (banner && !banner.getAttribute('data-sticky')) {
        banner.style.display = 'none';
      }
    }

    renderTheftIncidentsList(incidents);
  } catch (e) {
    console.error('Failed to fetch theft incidents:', e);
  }
}

function renderTheftIncidentsList(incidents) {
  const container = document.getElementById('theftIncidentsList');
  if (!container) return;

  container.innerHTML = '';
  incidents.forEach(inc => {
    const card = document.createElement('div');
    card.id = `incident-${inc.id}`;
    card.style.cssText = 'background: rgba(16, 22, 36, 0.85); border: 1px solid rgba(255,255,255,0.08); border-radius: var(--radius-md); padding: 14px; display: flex; flex-direction: column; gap: 8px; position: relative;';

    let statusPill = `<span class="badge badge-green">● RESOLVED</span>`;
    if (inc.status === 'ACTIVE') {
      statusPill = `<span class="badge badge-danger" style="animation: bounceAlert 1s infinite alternate;">🚨 ACTIVE</span>`;
    } else if (inc.status === 'ACKNOWLEDGED') {
      statusPill = `<span class="badge badge-warning">👁️ ACKNOWLEDGED</span>`;
    } else if (inc.status === 'DISPATCHED') {
      statusPill = `<span class="badge" style="background: #9d4edd; color:#fff;">🚔 GUARD DISPATCHED</span>`;
    }

    card.innerHTML = `
      <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
        <div style="display: flex; align-items: center; gap: 8px;">
          <span style="font-size: 14px; font-weight: 800; color: #fff;">${inc.theft_type}</span>
          <span class="badge" style="font-size: 10px;">${inc.department}</span>
          <span style="font-family: var(--font-mono); font-size: 11px; color: var(--accent-cyan); font-weight: 700;">${inc.camera_name}</span>
        </div>
        <div style="display: flex; align-items: center; gap: 8px;">
          <span style="font-family: var(--font-mono); font-size: 11px; color: #ffaa00;">${inc.confidence}% Conf</span>
          ${statusPill}
        </div>
      </div>

      <div style="font-size: 11.5px; color: #cbd5e1; line-height: 1.4;">${inc.details}</div>

      <div style="display: flex; justify-content: space-between; align-items: center; border-top: 1px solid rgba(255,255,255,0.05); padding-top: 8px; margin-top: 4px;">
        <span style="font-family: var(--font-mono); font-size: 10.5px; color: var(--text-dim);">Logged: ${inc.timestamp} | Value at Risk: <b style="color: var(--accent-green);">$${inc.prevented_value} AUD</b></span>
        <div style="display: flex; gap: 6px;">
          ${inc.status === 'ACTIVE' ? `<button class="btn btn-sm" onclick="acknowledgeTheft('${inc.id}')">Acknowledge</button>` : ''}
          ${inc.status !== 'DISPATCHED' && inc.status !== 'RESOLVED' ? `<button class="btn btn-danger btn-sm" onclick="dispatchGuard('${inc.id}')">🚨 Dispatch Guard</button>` : ''}
          ${inc.status !== 'RESOLVED' ? `<button class="btn btn-primary btn-sm" onclick="resolveTheft('${inc.id}')">✅ Resolve</button>` : ''}
        </div>
      </div>
    `;
    container.appendChild(card);
  });
}

async function acknowledgeTheft(id) {
  try {
    const res = await fetch(`/api/v1/theft/incidents/${id}/acknowledge`, { method: 'POST' });
    if (res.ok) {
      showToast(`👁️ Acknowledged theft incident ${id}`);
      fetchTheftIncidents();
    }
  } catch (e) {}
}

async function dispatchGuard(id) {
  try {
    const res = await fetch(`/api/v1/theft/incidents/${id}/dispatch`, { method: 'POST' });
    if (res.ok) {
      showToast(`🚔 Security Guard dispatched for incident ${id}`);
      fetchTheftIncidents();
    }
  } catch (e) {}
}

async function resolveTheft(id) {
  try {
    const res = await fetch(`/api/v1/theft/incidents/${id}/resolve`, { method: 'POST' });
    if (res.ok) {
      showToast(`✅ Marked incident ${id} as resolved`);
      fetchTheftIncidents();
    }
  } catch (e) {}
}

async function simulateTheft(type) {
  try {
    showToast(`⚡ Simulating theft scenario: ${type}...`);
    const res = await fetch('/api/v1/theft/simulate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scenario_type: type })
    });
    if (res.ok) {
      playTheftAlertSound();
      fetchTheftIncidents();
      showToast(`🚨 Shoplifting scenario generated!`);
    }
  } catch (e) {
    console.error('Failed to simulate theft:', e);
  }
}

function dismissTheftBanner() {
  const banner = document.getElementById('theftAlertBanner');
  if (banner) banner.style.display = 'none';
}

function dispatchGuardFromBanner() {
  if (activeTheftIncidentId) {
    dispatchGuard(activeTheftIncidentId);
    dismissTheftBanner();
  }
}

function scrollToTheftIncident() {
  if (activeTheftIncidentId) {
    const el = document.getElementById(`incident-${activeTheftIncidentId}`);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
}

// ==========================================
// 6. Camera Configuration Modal
// ==========================================
async function openCameraConfigModal(cameraId) {
  const modal = document.getElementById('modalCameraConfig');
  if (!modal) return;

  let cam = allCamerasList.find(c => c.id === cameraId);
  if (!cam && window.storeFloorplanHUD) {
    cam = window.storeFloorplanHUD.cameras.find(c => c.camera_id === cameraId || c.id === cameraId);
  }

  if (!cam) {
    try {
      const res = await fetch(`/api/v1/cameras/${cameraId}`);
      if (res.ok) cam = await res.json();
    } catch (e) {}
  }

  if (!cam) return;

  document.getElementById('configCameraId').value = cam.id || cam.camera_id;
  document.getElementById('configCameraName').value = cam.name || '';
  document.getElementById('configChannelNumber').value = cam.channel_number || 1;
  document.getElementById('configDepartment').value = cam.department || 'GENERAL';
  document.getElementById('configLocation').value = cam.location || '';
  document.getElementById('configRtspUrl').value = cam.rtsp_url || '';
  document.getElementById('configFloorX').value = Math.round(cam.floor_x || 100);
  document.getElementById('configFloorY').value = Math.round(cam.floor_y || 100);
  document.getElementById('configHeightZ').value = cam.height_z || 3.2;
  document.getElementById('configFovDeg').value = cam.fov_deg || 85;

  const azimuth = Math.round(cam.azimuth_deg || 0);
  document.getElementById('configAzimuthSlider').value = azimuth;
  document.getElementById('configAzimuthVal').textContent = `${azimuth}°`;

  document.getElementById('configFps').value = cam.fps || 25;
  document.getElementById('configResolution').value = cam.resolution || '1920x1080';

  const feats = cam.features || {};
  document.getElementById('featDwellTracking').checked = feats.dwell_tracking !== false;
  document.getElementById('featShelfInteraction').checked = feats.shelf_interaction !== false;
  document.getElementById('featTheftDetection').checked = feats.theft_detection !== false;
  document.getElementById('featFallDetection').checked = feats.fall_detection !== false;
  document.getElementById('featQueueMonitoring').checked = feats.queue_monitoring !== false;

  modal.style.display = 'flex';
}

function closeCameraConfigModal() {
  const modal = document.getElementById('modalCameraConfig');
  if (modal) modal.style.display = 'none';
}

async function handleCameraConfigSubmit(event) {
  event.preventDefault();
  const camId = document.getElementById('configCameraId').value;
  
  const payload = {
    id: camId,
    name: document.getElementById('configCameraName').value,
    channel_number: parseInt(document.getElementById('configChannelNumber').value) || 1,
    department: document.getElementById('configDepartment').value,
    location: document.getElementById('configLocation').value,
    rtsp_url: document.getElementById('configRtspUrl').value,
    webrtc_url: `http://localhost:8000/api/v1/webrtc/offer?camera_id=${camId}`,
    status: 'ONLINE',
    fps: parseInt(document.getElementById('configFps').value) || 25,
    resolution: document.getElementById('configResolution').value,
    floor_x: parseFloat(document.getElementById('configFloorX').value),
    floor_y: parseFloat(document.getElementById('configFloorY').value),
    height_z: parseFloat(document.getElementById('configHeightZ').value),
    azimuth_deg: parseFloat(document.getElementById('configAzimuthSlider').value),
    fov_deg: parseFloat(document.getElementById('configFovDeg').value),
    is_ai_enabled: true,
    features: {
      dwell_tracking: document.getElementById('featDwellTracking').checked,
      shelf_interaction: document.getElementById('featShelfInteraction').checked,
      theft_detection: document.getElementById('featTheftDetection').checked,
      fall_detection: document.getElementById('featFallDetection').checked,
      queue_monitoring: document.getElementById('featQueueMonitoring').checked,
      tripwires_enabled: true,
      intrusion_zones_enabled: true
    }
  };

  try {
    const res = await fetch(`/api/v1/cameras/${camId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (res.ok) {
      showToast(`💾 Saved changes for ${payload.name}`);
      closeCameraConfigModal();
      loadCamerasMatrix();
      if (window.storeFloorplanHUD) {
        window.storeFloorplanHUD.fetchFloorplanCameras();
      }
    }
  } catch (e) {
    console.error('Failed to update camera:', e);
  }
}

async function handleDeleteCurrentCamera() {
  const camId = document.getElementById('configCameraId').value;
  if (!confirm(`Are you sure you want to delete camera ${camId}?`)) return;

  try {
    const res = await fetch(`/api/v1/cameras/${camId}`, { method: 'DELETE' });
    if (res.ok) {
      showToast(`🗑️ Deleted camera ${camId}`);
      closeCameraConfigModal();
      loadCamerasMatrix();
      if (window.storeFloorplanHUD) {
        window.storeFloorplanHUD.fetchFloorplanCameras();
      }
    }
  } catch (e) {
    console.error('Failed to delete camera:', e);
  }
}

// ==========================================
// 7. AI Decision Action Center & Digest
// ==========================================
async function loadActionCenter() {
  try {
    const res = await fetch('/api/v1/analytics/actions');
    if (!res.ok) return;
    const data = await res.json();
    allActionItems = data.actions || [];
    renderActionCenter();
  } catch (e) {}
}

function renderActionCenter() {
  const list = document.getElementById('actionCardsList');
  if (!list) return;

  list.innerHTML = '';
  allActionItems.forEach(item => {
    const card = document.createElement('div');
    card.className = 'action-card';

    let severityClass = 'badge-danger';
    if (item.priority === 'HIGH') severityClass = 'badge-warning';
    if (item.priority === 'MEDIUM') severityClass = 'badge-primary';

    card.innerHTML = `
      <div class="action-card-header">
        <div style="display:flex; gap: 8px; align-items: center;">
          <span class="badge ${severityClass}">${item.priority}</span>
          <span class="badge" style="font-size: 10px;">${item.category}</span>
          <span class="action-title">${item.title}</span>
        </div>
        <span class="badge" style="background: rgba(255,255,255,0.1);">${item.status}</span>
      </div>

      <div class="action-desc">${item.description}</div>

      <div class="action-footer">
        <div class="action-meta">Impact: <b style="color: var(--accent-green);">${item.expected_impact}</b></div>
        <div class="action-btns">
          <button class="btn btn-sm" onclick="updateActionStatus('${item.id}', 'SCHEDULED')">📅 Schedule</button>
          <button class="btn btn-primary btn-sm" onclick="updateActionStatus('${item.id}', 'DONE')">✅ Mark Done</button>
        </div>
      </div>
    `;
    list.appendChild(card);
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
      showToast(`Updated action status to ${newStatus}`);
      loadActionCenter();
    }
  } catch (e) {}
}

async function loadExecutiveDigest() {
  try {
    const res = await fetch('/api/v1/analytics/digest');
    if (!res.ok) return;
    const data = await res.json();

    const narrativeEl = document.getElementById('digestNarrative');
    if (narrativeEl) narrativeEl.textContent = data.executive_narrative;
  } catch (e) {}
}

function printDailyDigest() {
  window.print();
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
  fetchTheftIncidents();

  // Polling Intervals
  setInterval(fetchSystemTelemetry, 3000);
  setInterval(fetchStoreKPIs, 4000);
  setInterval(fetchTheftIncidents, 4000);
});
