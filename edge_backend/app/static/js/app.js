
async function fetchSystemTelemetry() {
  try {
    const hwRes = await fetch('/api/v1/system/hardware');
    const hw = await hwRes.json();
    document.getElementById('decoderVal').textContent = `${hw.decoder_type.toUpperCase()} (${hw.device_name})`;
    document.getElementById('engineVal').textContent = `${hw.inference_backend.toUpperCase()}`;

    const stRes = await fetch('/api/v1/system/stats');
    const st = await stRes.json();
    document.getElementById('cpuRamVal').textContent = `${st.cpu_usage_percent}% CPU | ${st.ram_used_gb} / ${st.ram_total_gb} GB RAM`;
    document.getElementById('shmVal').textContent = `${hw.ring_buffer_seconds}s Pre-Roll (${st.shm_buffer_used_mb} MB SHM)`;
  } catch (e) {}
}

async function loadCamerasGrid() {
  try {
    const res = await fetch('/api/v1/cameras');
    const data = await res.json();
    const cameras = data.cameras || [];
    document.getElementById('camCountBadge').textContent = cameras.length;

    const container = document.getElementById('cameraGridContainer');
    container.innerHTML = '';

    cameras.forEach(cam => {
      const card = document.createElement('div');
      card.className = 'camera-card';
      const feats = cam.features || {};

      card.innerHTML = `
        <div class="camera-header">
          <span class="camera-title">📷 ${cam.name}</span>
          <span class="badge ${cam.status === 'ONLINE' ? 'badge-green' : 'badge-red'}">${cam.status}</span>
        </div>
        <div class="camera-viewport">
          <img src="/api/v1/cameras/${cam.id}/snapshot" alt="${cam.name}">
          <div class="camera-overlay-badge">${cam.resolution} | ${cam.fps} FPS</div>
        </div>
        <div class="camera-actions">
          <div class="feature-matrix">
            <label class="switch-item">
              <span>Elderly Fall</span>
              <input type="checkbox" ${feats.fall_detection ? 'checked' : ''} onchange="toggleFeature('${cam.id}', 'fall_detection', this.checked)">
            </label>
            <label class="switch-item">
              <span>Door State</span>
              <input type="checkbox" ${feats.door_monitoring ? 'checked' : ''} onchange="toggleFeature('${cam.id}', 'door_monitoring', this.checked)">
            </label>
            <label class="switch-item">
              <span>Package Theft</span>
              <input type="checkbox" ${feats.package_theft_tracking ? 'checked' : ''} onchange="toggleFeature('${cam.id}', 'package_theft_tracking', this.checked)">
            </label>
            <label class="switch-item">
              <span>24/7 DVR</span>
              <input type="checkbox" ${feats.dvr_recording_24_7 ? 'checked' : ''} onchange="toggleFeature('${cam.id}', 'dvr_recording_24_7', this.checked)">
            </label>
          </div>
          <div style="display: flex; gap: 8px;">
            <a href="/dashboard/studio?camera_id=${cam.id}" class="btn btn-primary" style="flex: 1; justify-content: center;">🎨 Studio & Zones</a>
          </div>
        </div>
      `;
      container.appendChild(card);
    });
  } catch (e) {}
}

async function toggleFeature(camId, featureName, isEnabled) {
  try {
    const camRes = await fetch(`/api/v1/cameras/${camId}`);
    const cam = await camRes.json();
    const config = cam.features || {};
    config[featureName] = isEnabled;

    await fetch(`/api/v1/cameras/${camId}/features`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config)
    });
    showToast(`Updated ${featureName} -> ${isEnabled ? 'ON' : 'OFF'}`);
  } catch (e) {
    showToast('Failed to update feature');
  }
}

function openScannerModal() {
  document.getElementById('scannerModal').style.display = 'flex';
  executeNetworkScan();
}

function closeScannerModal() {
  document.getElementById('scannerModal').style.display = 'none';
}

async function executeNetworkScan() {
  const container = document.getElementById('discoveredSourcesList');
  container.innerHTML = '<div style="padding: 10px; text-align: center; color: var(--accent-cyan);">Scanning network subnets & USB interfaces...</div>';
  try {
    const res = await fetch('/api/v1/cameras/scan', { method: 'POST' });
    const data = await res.json();
    const sources = data.sources || [];
    container.innerHTML = '';
    
    if (sources.length === 0) {
      container.innerHTML = '<div style="padding: 10px; text-align: center; color: var(--text-dim);">No new cameras found.</div>';
      return;
    }

    sources.forEach(src => {
      const item = document.createElement('div');
      item.style.cssText = 'background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 10px; display: flex; justify-content: space-between; align-items: center;';
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
    container.innerHTML = '<div style="padding: 10px; text-align: center; color: var(--accent-red);">Scan error.</div>';
  }
}

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.display = 'block';
  setTimeout(() => { t.style.display = 'none'; }, 3000);
}

fetchSystemTelemetry();
loadCamerasGrid();
setInterval(fetchSystemTelemetry, 3000);
