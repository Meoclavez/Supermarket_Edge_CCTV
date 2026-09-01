/**
 * Camera Studio & Zone Editor HUD Controller
 */

const canvas = document.getElementById('interactiveCanvas');
const ctx = canvas ? canvas.getContext('2d') : null;
let currentMode = 'NONE';
let drawnPoints = [];
let activeCameraId = 'cam_entrance_main';

function getUrlParameter(name) {
  const params = new URLSearchParams(window.location.search);
  return params.get(name);
}

function resizeCanvas() {
  if (!canvas || !canvas.parentElement) return;
  canvas.width = canvas.parentElement.clientWidth;
  canvas.height = canvas.parentElement.clientHeight;
  drawOverlay();
}
window.addEventListener('resize', resizeCanvas);
setTimeout(resizeCanvas, 300);

if (canvas) {
  canvas.addEventListener('click', (e) => {
    if (currentMode === 'NONE') return;
    const rect = canvas.getBoundingClientRect();
    const nx = (e.clientX - rect.left) / canvas.width;
    const ny = (e.clientY - rect.top) / canvas.height;

    if (currentMode === 'TRIPWIRE') {
      if (drawnPoints.length >= 2) drawnPoints = [];
      drawnPoints.push({ x: nx, y: ny });
    } else {
      drawnPoints.push({ x: nx, y: ny });
    }
    drawOverlay();
  });
}

function drawOverlay() {
  if (!ctx || !canvas) return;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (drawnPoints.length === 0) return;

  if (currentMode === 'TRIPWIRE') {
    ctx.strokeStyle = '#00f0ff';
    ctx.fillStyle = 'rgba(0, 240, 255, 0.2)';
  } else if (currentMode === 'INTRUSION') {
    ctx.strokeStyle = '#ffaa00';
    ctx.fillStyle = 'rgba(255, 170, 0, 0.25)';
  } else if (currentMode === 'EXCLUSION') {
    ctx.strokeStyle = '#a0aec0';
    ctx.fillStyle = 'rgba(160, 174, 192, 0.35)';
  }

  ctx.lineWidth = 2.5;
  ctx.beginPath();
  drawnPoints.forEach((pt, i) => {
    const px = pt.x * canvas.width;
    const py = pt.y * canvas.height;
    if (i === 0) ctx.moveTo(px, py);
    else ctx.lineTo(px, py);
  });

  if ((currentMode === 'INTRUSION' || currentMode === 'EXCLUSION') && drawnPoints.length >= 3) {
    ctx.closePath();
    ctx.fill();
  }
  ctx.stroke();

  drawnPoints.forEach(pt => {
    ctx.fillStyle = '#ffffff';
    ctx.beginPath();
    ctx.arc(pt.x * canvas.width, pt.y * canvas.height, 5, 0, Math.PI * 2);
    ctx.fill();
  });
}

function setDrawMode(mode) {
  currentMode = mode;
  drawnPoints = [];
  drawOverlay();
  showToast(`Mode: ${mode} - Click on video to place polygon vertices.`);
}

function clearCanvasPoints() {
  drawnPoints = [];
  currentMode = 'NONE';
  drawOverlay();
  showToast('Drawing cancelled.');
}

async function saveDrawnZone() {
  const defaultName = currentMode === 'TRIPWIRE' ? 'Tripwire' : (currentMode === 'INTRUSION' ? 'Restricted Area' : 'Exclusion Mask');
  const name = document.getElementById('zoneNameInput').value.trim() || defaultName;
  const allowed = document.getElementById('zoneClassSelect').value === 'person' ? ['person'] : (document.getElementById('zoneClassSelect').value === 'vehicle' ? ['vehicle'] : ['person', 'vehicle']);

  try {
    if (currentMode === 'TRIPWIRE' && drawnPoints.length === 2) {
      await fetch('/api/zones/tripwire', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: name,
          camera_id: activeCameraId,
          x1: drawnPoints[0].x, y1: drawnPoints[0].y,
          x2: drawnPoints[1].x, y2: drawnPoints[1].y,
          direction: 'BIDIRECTIONAL',
          allowed_classes: allowed,
          enabled: true
        })
      });
      showToast(`✅ Saved Tripwire: ${name}`);
      clearCanvasPoints();
      loadZonesList();
    } else if (currentMode === 'INTRUSION' && drawnPoints.length >= 3) {
      await fetch('/api/zones/intrusion', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: name,
          camera_id: activeCameraId,
          points: drawnPoints,
          allowed_classes: allowed,
          dwell_time_seconds: 0.5,
          enabled: true
        })
      });
      showToast(`✅ Saved Restricted Area: ${name}`);
      clearCanvasPoints();
      loadZonesList();
    } else if (currentMode === 'EXCLUSION' && drawnPoints.length >= 3) {
      await fetch('/api/zones/exclusion', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: name,
          camera_id: activeCameraId,
          points: drawnPoints,
          mask_mode: 'BLUR',
          enabled: true
        })
      });
      showToast(`✅ Saved Exclusion Mask: ${name}`);
      clearCanvasPoints();
      loadZonesList();
    } else {
      showToast('Please click points on video first (2 for tripwire, 3+ for polygons).');
    }
  } catch (err) {
    showToast('Error saving zone: ' + err.message);
  }
}

async function deleteTripwire(id) {
  await fetch(`/api/zones/tripwire/${id}`, { method: 'DELETE' });
  showToast('Tripwire removed.');
  loadZonesList();
}

async function deleteIntrusion(id) {
  await fetch(`/api/zones/intrusion/${id}`, { method: 'DELETE' });
  showToast('Restricted Area removed.');
  loadZonesList();
}

async function deleteExclusion(id) {
  await fetch(`/api/zones/exclusion/${id}`, { method: 'DELETE' });
  showToast('Exclusion Mask removed.');
  loadZonesList();
}

async function clearAllZones() {
  if (confirm('Clear all tripwires, areas, and masks?')) {
    await fetch('/api/zones/clear', { method: 'POST' });
    showToast('All zones cleared.');
    loadZonesList();
  }
}

async function loadZonesList() {
  try {
    const res = await fetch('/api/zones');
    const data = await res.json();

    const twContainer = document.getElementById('tripwiresListContainer');
    const tripwires = data.tripwires || [];
    twContainer.innerHTML = tripwires.length === 0 ? '<div style="font-size: 11.5px; color: var(--text-dim);">No tripwires configured.</div>' : '';
    tripwires.forEach(tw => {
      const item = document.createElement('div');
      item.className = 'zone-item';
      item.innerHTML = `
        <div class="zone-info">
          <span class="zone-name">⚡ ${tw.name}</span>
          <span class="zone-sub">In: ${tw.in_count || 0} | Out: ${tw.out_count || 0} | [${tw.direction || 'BIDIRECTIONAL'}]</span>
        </div>
        <button class="btn btn-danger btn-sm" onclick="deleteTripwire('${tw.id}')">🗑️</button>
      `;
      twContainer.appendChild(item);
    });

    const intContainer = document.getElementById('intrusionListContainer');
    const intrusion_zones = data.intrusion_zones || [];
    intContainer.innerHTML = intrusion_zones.length === 0 ? '<div style="font-size: 11.5px; color: var(--text-dim);">No restricted areas configured.</div>' : '';
    intrusion_zones.forEach(iz => {
      const item = document.createElement('div');
      item.className = 'zone-item';
      item.innerHTML = `
        <div class="zone-info">
          <span class="zone-name">🛑 ${iz.name}</span>
          <span class="zone-sub">${iz.points ? iz.points.length : 0} Vertices | Target: ${iz.allowed_classes ? iz.allowed_classes.join(',') : 'all'}</span>
        </div>
        <button class="btn btn-danger btn-sm" onclick="deleteIntrusion('${iz.id}')">🗑️</button>
      `;
      intContainer.appendChild(item);
    });

    const exContainer = document.getElementById('exclusionListContainer');
    const exclusion_masks = data.exclusion_masks || [];
    exContainer.innerHTML = exclusion_masks.length === 0 ? '<div style="font-size: 11.5px; color: var(--text-dim);">No exclusion masks configured.</div>' : '';
    exclusion_masks.forEach(ex => {
      const item = document.createElement('div');
      item.className = 'zone-item';
      item.innerHTML = `
        <div class="zone-info">
          <span class="zone-name">🌫️ ${ex.name}</span>
          <span class="zone-sub">${ex.points ? ex.points.length : 0} Vertices | [${ex.mask_mode || 'BLUR'}]</span>
        </div>
        <button class="btn btn-danger btn-sm" onclick="deleteExclusion('${ex.id}')">🗑️</button>
      `;
      exContainer.appendChild(item);
    });
  } catch (e) {}
}

async function loadStudioSources() {
  try {
    const res = await fetch('/api/v1/cameras');
    const data = await res.json();
    const cameras = data.cameras || [];
    const list = document.getElementById('cameraButtonsList');
    list.innerHTML = '';

    const paramCamId = getUrlParameter('camera_id');
    if (paramCamId) {
      activeCameraId = paramCamId;
    } else if (cameras.length > 0) {
      activeCameraId = cameras[0].id;
    }

    cameras.forEach(cam => {
      const btn = document.createElement('button');
      const isSelected = cam.id === activeCameraId;
      btn.className = `btn btn-sm ${isSelected ? 'btn-primary' : ''}`;
      btn.textContent = cam.name;
      btn.onclick = () => {
        document.querySelectorAll('#cameraButtonsList .btn').forEach(b => b.classList.remove('btn-primary'));
        btn.classList.add('btn-primary');
        activeCameraId = cam.id;
        document.getElementById('sourceBadge').textContent = cam.name;
        showToast(`Switched Studio Feed to: ${cam.name}`);
      };
      list.appendChild(btn);

      if (isSelected) {
        document.getElementById('sourceBadge').textContent = cam.name;
      }
    });
  } catch (e) {}
}

function showToast(msg) {
  const t = document.getElementById('toast');
  if (!t) return;
  t.textContent = msg;
  t.style.display = 'block';
  setTimeout(() => { t.style.display = 'none'; }, 3000);
}

async function triggerSnapshot() {
  const res = await fetch('/api/action/snapshot', { method: 'POST' });
  const data = await res.json();
  showToast(data.message);
}

async function triggerClip() {
  const res = await fetch('/api/action/clip', { method: 'POST' });
  const data = await res.json();
  showToast(data.message);
}

function rescanStudioCameras() {
  showToast('Scanning network and USB devices...');
  loadStudioSources();
}

document.addEventListener('DOMContentLoaded', () => {
  loadZonesList();
  loadStudioSources();
  setInterval(loadZonesList, 4000);
});
