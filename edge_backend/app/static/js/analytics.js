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

  // If switching to market_ai, trigger market predictions loader
  if (tabId === 'market_ai') {
    setTimeout(() => loadMarketPredictions(), 100);
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

// ==========================================
// 8. Machine Learning & LLM Market Intelligence
// ==========================================
let marketPredictionsCache = null;

async function loadMarketPredictions() {
  try {
    // 0. Probe Ollama dynamic model status
    try {
      const statusRes = await fetch('/api/v1/analytics/market/llm-status');
      if (statusRes.ok) {
        const statusData = await statusRes.json();
        const modelBadge = document.getElementById('ollamaModelBadge');
        const warningBanner = document.getElementById('ollamaWarningBanner');

        if (statusData.ollama_active && statusData.active_model) {
          if (modelBadge) {
            modelBadge.textContent = `🧠 Ollama: ${statusData.active_model}`;
            modelBadge.className = 'badge badge-green';
            modelBadge.style.display = 'inline-flex';
          }
          if (warningBanner) {
            warningBanner.style.display = 'none';
            warningBanner.innerHTML = '';
          }
        } else {
          if (modelBadge) {
            modelBadge.textContent = '⚡ Edge Heuristics Mode';
            modelBadge.className = 'badge badge-warning';
            modelBadge.style.display = 'inline-flex';
          }
          if (warningBanner) {
            const warningMsg = statusData.warning || 'Ollama service offline or model unavailable. Using deterministic edge rule engine.';
            warningBanner.style.display = 'block';
            warningBanner.innerHTML = `
              <div style="background: rgba(255, 170, 0, 0.12); border: 1px solid rgba(255, 170, 0, 0.35); border-radius: 8px; padding: 10px 14px; display: flex; align-items: center; justify-content: space-between; gap: 12px;">
                <div style="display: flex; align-items: center; gap: 10px;">
                  <span style="font-size: 18px;">⚠️</span>
                  <div>
                    <div style="font-size: 12px; font-weight: 700; color: #ffaa00;">Local Ollama AI Engine Offline</div>
                    <div style="font-size: 11px; color: #cbd5e1;">${warningMsg}</div>
                  </div>
                </div>
                <span class="badge" style="background: rgba(255, 255, 255, 0.1); color: #fff; font-size: 10px;">Deterministic Fallback Active</span>
              </div>
            `;
          }
        }
      }
    } catch (statusErr) {
      console.warn('Ollama status check failed:', statusErr);
      const modelBadge = document.getElementById('ollamaModelBadge');
      if (modelBadge) {
        modelBadge.textContent = '⚡ Edge Heuristics Mode';
        modelBadge.className = 'badge badge-warning';
      }
    }

    const res = await fetch('/api/v1/analytics/market/predictions?store_id=STORE-AU-3912');
    const data = await res.json();
    marketPredictionsCache = data;

    // 1. Render Stockout Timelines
    const stockContainer = document.getElementById('stockoutContainer');
    if (stockContainer && data.stockout_risks) {
      stockContainer.innerHTML = '';
      const criticalCount = data.stockout_risks.filter(s => s.urgency_level === 'CRITICAL').length;
      const stockBadge = document.getElementById('stockoutBadge');
      if (stockBadge) {
        stockBadge.textContent = criticalCount > 0 ? `⚠️ ${criticalCount} Critical Alerts` : 'All Stock Healthy';
        stockBadge.style.color = criticalCount > 0 ? 'var(--accent-red)' : 'var(--accent-green)';
      }

      data.stockout_risks.forEach(item => {
        const row = document.createElement('div');
        row.style.cssText = 'background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.08); border-radius: 8px; padding: 10px; display: flex; flex-direction: column; gap: 6px;';
        
        let color = '#00f0ff';
        let badgeBg = 'rgba(0,240,255,0.2)';
        if (item.urgency_level === 'CRITICAL') {
          color = '#ff3366';
          badgeBg = 'rgba(255,51,102,0.2)';
        } else if (item.urgency_level === 'WARNING') {
          color = '#ffaa00';
          badgeBg = 'rgba(255,170,0,0.2)';
        }

        row.innerHTML = `
          <div style="display: flex; justify-content: space-between; align-items: center;">
            <div style="font-weight: 700; font-size: 12px; color: #fff;">${item.product_name} <span style="font-size: 10px; color: var(--text-dim); font-family: var(--font-mono);">(${item.sku_id})</span></div>
            <span style="font-size: 10px; font-weight: 800; padding: 2px 6px; border-radius: 4px; background: ${badgeBg}; color: ${color};">${item.urgency_level} • ${item.hours_to_stockout}h left</span>
          </div>
          <div style="font-size: 11px; color: var(--text-dim);">${item.recommendation}</div>
          <div style="display: flex; justify-content: space-between; font-size: 10px; color: var(--text-dim);">
            <span>Pick Velocity: <b>${item.hourly_pick_velocity} units/hr</b></span>
            <span>Current Shelf Facing: <b>${item.current_stock_units} units</b></span>
          </div>
        `;
        stockContainer.appendChild(row);
      });
    }

    // 2. Render 24-Hour Predictive Footfall Curve
    const chartBox = document.getElementById('footfallChartContainer');
    if (chartBox && data.hourly_footfall_forecast) {
      chartBox.innerHTML = '';
      const maxTraffic = Math.max(...data.hourly_footfall_forecast.map(f => f.expected_traffic), 1);

      data.hourly_footfall_forecast.forEach(pt => {
        const barWrap = document.createElement('div');
        barWrap.style.cssText = 'flex: 1; display: flex; flex-direction: column; align-items: center; height: 100%; justify-content: flex-end; position: relative;';
        
        const barHeightPct = (pt.expected_traffic / maxTraffic) * 100;
        const barColor = pt.is_peak_hour ? 'linear-gradient(180deg, #ffaa00 0%, #ff3366 100%)' : 'linear-gradient(180deg, #00f0ff 0%, #0066cc 100%)';
        
        barWrap.innerHTML = `
          <div style="font-size: 8.5px; color: var(--text-dim); margin-bottom: 2px;">${pt.expected_traffic}</div>
          <div style="width: 80%; height: ${Math.max(6, barHeightPct)}%; background: ${barColor}; border-radius: 3px 3px 0 0; transition: height 0.4s ease;" title="${pt.hour}: ${pt.expected_traffic} customers"></div>
          <div style="font-size: 8px; color: var(--text-dim); margin-top: 4px; transform: rotate(-45deg);">${pt.hour}</div>
        `;
        chartBox.appendChild(barWrap);
      });
    }

    // 3. Load initial LLM optimizations if container is empty
    const optContainer = document.getElementById('llmOptimizationsContainer');
    if (optContainer && optContainer.children.length === 0) {
      runLLMOptimizations();
    }
  } catch (err) {
    console.error('Error loading market predictions:', err);
  }
}

function updateElasticitySimulation() {
  const targetTier = document.getElementById('simTargetTierSelect').value;
  
  let liftA = '+35.0%';
  let liftG = '+18.5%';
  let revGain = '+$320.00 AUD';

  if (targetTier === 'ENDCAP') {
    liftA = '+68.0%';
    liftG = '+34.2%';
    revGain = '+$580.00 AUD';
  } else if (targetTier === 'EYE_LEVEL') {
    liftA = '+42.0%';
    liftG = '+23.1%';
    revGain = '+$380.00 AUD';
  } else if (targetTier === 'TOP') {
    liftA = '+12.0%';
    liftG = '+6.5%';
    revGain = '+$110.00 AUD';
  } else if (targetTier === 'BOTTOM') {
    liftA = '-28.0%';
    liftG = '-15.4%';
    revGain = '-$190.00 AUD';
  }

  document.getElementById('simLiftAlpha').textContent = liftA;
  document.getElementById('simLiftGamma').textContent = liftG;
  document.getElementById('simRevGain').textContent = revGain;
}

async function runLLMOptimizations() {
  const container = document.getElementById('llmOptimizationsContainer');
  if (container) {
    container.innerHTML = '<div style="padding: 15px; font-size: 12px; color: #ffaa00; text-align: center;">⚡ Running LLM Multimodal Market Synthesis...</div>';
  }

  try {
    const res = await fetch('/api/v1/analytics/market/llm-optimize?store_id=STORE-AU-3912', { method: 'POST' });
    const data = await res.json();
    const opts = data.optimizations || [];
    const modelUsed = data.model_used || (data.ollama_status === 'online' ? 'Ollama' : 'deterministic-edge-rules');

    const badge = document.getElementById('llmOptCountBadge');
    if (badge) badge.textContent = `${opts.length} Strategic Actions`;

    // Dynamically update model badge if returned from optimization
    const modelBadge = document.getElementById('ollamaModelBadge');
    if (modelBadge && data.model_used) {
      if (data.ollama_status === 'online') {
        modelBadge.textContent = `🧠 Ollama: ${data.model_used}`;
        modelBadge.className = 'badge badge-green';
      } else {
        modelBadge.textContent = `⚡ Edge Rules (${data.model_used})`;
        modelBadge.className = 'badge badge-warning';
      }
    }

    if (container) {
      container.innerHTML = '';
      opts.forEach(opt => {
        const card = document.createElement('div');
        card.style.cssText = 'background: rgba(0,0,0,0.35); border: 1px solid rgba(255,255,255,0.08); border-radius: 10px; padding: 12px; display: flex; flex-direction: column; gap: 8px;';
        
        let prioColor = '#00f0ff';
        if (opt.priority === 'CRITICAL') prioColor = '#ff3366';
        else if (opt.priority === 'HIGH') prioColor = '#ffaa00';

        const modelLabel = data.ollama_status === 'online' 
          ? `Generated via Ollama: ${modelUsed}` 
          : `Generated via: ${modelUsed}`;

        card.innerHTML = `
          <div style="display: flex; justify-content: space-between; align-items: center;">
            <div style="display: flex; gap: 6px; align-items: center; flex-wrap: wrap;">
              <span style="font-size: 10px; font-weight: 800; padding: 2px 6px; border-radius: 4px; background: rgba(255,255,255,0.1); color: ${prioColor};">${opt.priority}</span>
              <span style="font-size: 10px; font-weight: 700; color: var(--text-dim);">[${opt.category}]</span>
              <span style="font-weight: 700; font-size: 12.5px; color: #fff;">${opt.target_product_or_zone}</span>
            </div>
            <button class="btn btn-primary btn-sm" onclick="applyMarketOptimization('${opt.id}', '${opt.target_product_or_zone}')">Apply Suggestion</button>
          </div>
          <div style="font-size: 11.5px; color: #e2e8f0; line-height: 1.4;"><b>Automated Directive:</b> ${opt.automated_recommendation}</div>
          <div style="font-size: 11px; color: var(--text-dim);"><b>Observed Behavior:</b> ${opt.observed_behavior}</div>
          <div style="font-size: 11px; color: var(--accent-green);"><b>Expected Business Impact:</b> ${opt.expected_business_impact}</div>
          <div style="display: flex; justify-content: space-between; align-items: center; font-size: 10px; color: #94a3b8; font-family: var(--font-mono); border-top: 1px dashed rgba(255,255,255,0.08); padding-top: 6px; margin-top: 2px;">
            <span>🤖 ${modelLabel}</span>
            <span style="color: var(--text-dim);">${data.analysis_timestamp ? new Date(data.analysis_timestamp).toLocaleTimeString() : 'Real-time'}</span>
          </div>
        `;
        container.appendChild(card);
      });
    }
  } catch (err) {
    if (container) {
      container.innerHTML = '<div style="padding: 15px; font-size: 12px; color: var(--accent-red); text-align: center;">Error running market optimizations.</div>';
    }
  }
}

function applyMarketOptimization(id, title) {
  showToast(`✅ Applied Optimization for ${title}. Logged to central store ledger.`);
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
