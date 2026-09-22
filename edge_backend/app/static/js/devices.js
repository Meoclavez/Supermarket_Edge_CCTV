/**
 * Camera and device manager.
 *
 * Cameras enter the system here, one at a time, by adopting a device that
 * actually responded to a scan. Previously the fleet was a fixed list of 32
 * fabricated entries re-seeded into the database on every boot, with no way to
 * add a real camera or remove one.
 */
(function () {
  'use strict';

  const API = '/api/v1/layout';

  function el(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  function status(msg, kind) {
    const e = el('deviceScanStatus');
    if (!e) return;
    e.textContent = msg || '';
    e.className = `fp-status fp-${kind || 'info'}`;
  }

  const deviceManager = {
    devices: [],
    cameras: [],

    async init() {
      await this.refresh();
      // Keep camera status fresh so an operator sees a feed drop out.
      setInterval(() => this.refreshCameras(), 6000);
    },

    async refresh() {
      if (window.edgeAuth && typeof window.edgeAuth.isAuthenticated === 'function' && !window.edgeAuth.isAuthenticated()) return;
      await Promise.all([this.refreshCameras(), this.refreshDevices()]);
    },

    async refreshCameras() {
      if (window.edgeAuth && typeof window.edgeAuth.isAuthenticated === 'function' && !window.edgeAuth.isAuthenticated()) return;
      try {
        const res = await fetch(API);
        if (!res.ok) return;
        const data = await res.json();
        this.cameras = data.cameras || [];
        this.renderCameras();
        // Keep the editor's view of status/calibration current without
        // disturbing a placement the operator may be dragging right now.
        const ed = window.blueprintEditor;
        if (ed && Array.isArray(ed.cameras)) {
          this.cameras.forEach((c) => {
            const e = ed.cameras.find((x) => x.camera_id === c.camera_id);
            if (e) Object.assign(e, {
              status: c.status, has_homography: c.has_homography, frame_width: c.frame_width,
              frame_height: c.frame_height, calibration_points: c.calibration_points, name: c.name,
            });
          });
        }
      } catch (_) { /* transient */ }
    },

    async refreshDevices() {
      try {
        const res = await fetch(`${API}/devices`);
        if (!res.ok) return;
        const data = await res.json();
        this.devices = data.devices || [];
        this.renderDevices();
      } catch (_) { /* transient */ }
    },

    async scan() {
      const btn = el('btnScanDevices');
      if (btn) { btn.disabled = true; btn.textContent = 'Scanning…'; }
      status('Scanning USB, ONVIF, mDNS and the local subnet. This takes a few seconds.');
      try {
        const res = await fetch(`${API}/discover`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ include_usb: true, include_network: true }),
        });
        if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
        const data = await res.json();
        await this.refreshDevices();
        status(
          data.found
            ? `Found ${data.found} device(s).`
            : 'No cameras responded. Check they are powered and on this network.',
          data.found ? 'ok' : 'warn'
        );
      } catch (e) {
        status(`Scan failed: ${e.message}`, 'error');
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Scan for cameras'; }
      }
    },

    renderCameras() {
      const host = el('activeCameraList');
      if (!host) return;
      if (!this.cameras.length) {
        host.innerHTML = `
          <div class="dev-cta">
            <div class="dev-cta-title">No cameras yet</div>
            <div class="dev-cta-sub">Nothing can be measured until a camera is added. Scan the network and USB ports, then add a device that responds.</div>
            <button class="btn btn-sm btn-primary" onclick="deviceManager.scan()">Scan for cameras</button>
          </div>`;
        return;
      }
      const selected = window.blueprintEditor && window.blueprintEditor.sel && window.blueprintEditor.sel.type === 'camera'
        ? window.blueprintEditor.sel.id : null;
      host.innerHTML = this.cameras.map((c) => {
        const online = c.status === 'ONLINE';
        const frame = c.frame_width && c.frame_height ? `${c.frame_width}×${c.frame_height}` : 'no frame yet';
        const id = esc(c.camera_id);
        return `
        <div class="dev-row dev-row-cam ${c.camera_id === selected ? 'is-selected' : ''}" data-cam-row="${id}">
          <span class="dev-dot ${online ? 'dot-on' : 'dot-off'}"></span>
          <div class="dev-main">
            <div class="dev-name">${esc(c.name)}</div>
            <div class="dev-sub">${esc(c.status)} · ${(+c.floor_x || 0).toFixed(1)}, ${(+c.floor_y || 0).toFixed(1)} m · ${Math.round(c.azimuth_deg || 0)}°</div>
            <div class="dev-meta">
              <span class="dev-tag ${c.frame_width ? '' : 'warn'}">${frame}</span>
              <span class="dev-tag ${c.has_homography ? 'ok' : 'warn'}">${c.has_homography ? 'calibrated' : 'uncalibrated'}</span>
            </div>
          </div>
          <div class="dev-actions">
            <button class="btn btn-xs" onclick="deviceManager.config('${id}')" title="Position, bearing, field of view">Config</button>
            <button class="btn btn-xs ${c.has_homography ? '' : 'btn-primary'}" onclick="deviceManager.calibrate('${id}')" title="Map this camera's image onto the plan">Calibrate</button>
            <button class="btn btn-xs" onclick="deviceManager.place('${id}')" title="Click the plan to reposition">Place</button>
            <button class="btn btn-xs btn-danger" onclick="deviceManager.remove('${id}')">Remove</button>
          </div>
        </div>`;
      }).join('');
    },

    config(cameraId) {
      if (window.blueprintEditor) window.blueprintEditor.selectCamera(cameraId);
    },

    calibrate(cameraId) {
      if (window.calibrationTool) window.calibrationTool.open(cameraId);
    },

    highlight(cameraId) {
      document.querySelectorAll('[data-cam-row]').forEach((row) => {
        row.classList.toggle('is-selected', row.dataset.camRow === cameraId);
      });
    },

    renderDevices() {
      const host = el('discoveredList');
      if (!host) return;
      const unadopted = this.devices.filter((d) => !d.adopted_camera_id);
      if (!unadopted.length) {
        host.innerHTML = '<div class="fp-empty">Nothing new. Run a scan to look again.</div>';
        return;
      }
      host.innerHTML = unadopted.map((d) => `
        <div class="dev-row">
          <span class="dev-dot ${d.reachable ? 'dot-on' : 'dot-off'}"></span>
          <div class="dev-main">
            <div class="dev-name">${esc(d.model_name || d.host || d.device_path)}</div>
            <div class="dev-sub">${esc(d.driver)} · ${esc(d.transport)}${d.host ? ' · ' + esc(d.host) : ''}
              ${d.requires_credentials ? '· needs login' : ''}</div>
          </div>
          <div class="dev-actions">
            <button class="btn btn-xs btn-primary" onclick="deviceManager.adopt('${esc(d.id)}')">Add</button>
          </div>
        </div>`).join('');
    },

    /**
     * Open an inline form to add a discovered device as a camera.
     *
     * Adoption needs a name, an area, and for a network camera the
     * credentials and NVR channel. A chain of blocking prompts made this feel
     * broken and offered no way to review or correct an entry before
     * submitting, so the form is rendered in place instead.
     */
    adopt(deviceId) {
      const device = this.devices.find((d) => d.id === deviceId);
      if (!device) return;
      const host = el('discoveredList');
      if (!host) return;

      const needsAuth = device.requires_credentials;
      host.innerHTML = `
        <div class="dev-form">
          <div class="dev-form-title">Add ${esc(device.model_name || device.host || device.device_path)}</div>
          <div class="fp-field"><label for="adName">Camera name</label>
            <input id="adName" name="cameraName" type="text" value="${esc(device.model_name || device.host || 'Camera')}"></div>
          <div class="fp-field"><label for="adDept">Area</label>
            <select id="adDept" name="department">
              ${['ENTRANCE','EXIT','AISLE','DEPARTMENT','CHECKOUT','STOCKROOM','GENERAL']
                .map((d) => `<option value="${d}">${d}</option>`).join('')}
            </select></div>
          ${needsAuth ? `
          <div class="fp-field-row">
            <div class="fp-field"><label for="adUser">Username</label>
              <input id="adUser" name="username" type="text" value="admin" autocomplete="off"></div>
            <div class="fp-field"><label for="adPass">Password</label>
              <input id="adPass" name="password" type="password" value="" autocomplete="new-password"></div>
          </div>
          <div class="fp-field-row">
            <div class="fp-field"><label for="adCh">Channel</label>
              <input id="adCh" name="channel" type="number" min="1" max="64" value="1"></div>
            <div class="fp-field"><label for="adQual">Stream</label>
              <select id="adQual" name="streamQuality">
                <option value="sub">Sub (recommended for analytics)</option>
                <option value="main">Main (full resolution)</option>
              </select></div>
          </div>` : ''}
          <div class="fp-field-row">
            <div class="fp-field"><label for="adX">Position X (m)</label>
              <input id="adX" name="positionX" type="number" step="0.5" min="0" value="1"></div>
            <div class="fp-field"><label for="adY">Position Y (m)</label>
              <input id="adY" name="positionY" type="number" step="0.5" min="0" value="1"></div>
          </div>
          <div class="fp-actions">
            <button class="btn btn-sm btn-primary" id="adSubmit">Add camera</button>
            <button class="btn btn-sm" id="adCancel">Cancel</button>
          </div>
        </div>`;

      host.querySelector('#adCancel').addEventListener('click', () => this.renderDevices());
      host.querySelector('#adSubmit').addEventListener('click', async (ev) => {
        const btn = ev.currentTarget;
        btn.disabled = true;
        btn.textContent = 'Connecting…';
        const body = {
          device_id: deviceId,
          name: document.getElementById('adName').value.trim() || 'Camera',
          department: document.getElementById('adDept').value,
          floor_x: parseFloat(document.getElementById('adX').value) || 1,
          floor_y: parseFloat(document.getElementById('adY').value) || 1,
        };
        if (needsAuth) {
          body.username = document.getElementById('adUser').value;
          body.password = document.getElementById('adPass').value;
          body.channel = parseInt(document.getElementById('adCh').value, 10) || 1;
          body.quality = document.getElementById('adQual').value;
        }
        try {
          const res = await fetch(`${API}/devices/adopt`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          });
          if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
          const cam = await res.json();
          status(`Added "${cam.name}". Drag it into position on the plan, then calibrate it.`, 'ok');
          if (window.blueprintEditor) await window.blueprintEditor.load();
          await this.refresh();
          if (window.blueprintEditor) window.blueprintEditor.selectCamera(cam.camera_id || cam.id);
        } catch (e) {
          status(`Could not add device: ${e.message}`, 'error');
          btn.disabled = false;
          btn.textContent = 'Add camera';
        }
      });
    },

    place(cameraId) {
      window.__pendingCameraPlacement = cameraId;
      if (window.blueprintEditor) window.blueprintEditor.startPlaceCamera();
      status('Click the plan where this camera is mounted.');
    },

    /**
     * Two-step removal rendered in place: in the camera row by default, or in
     * the element passed by the inspector. Never a confirm() dialog.
     */
    remove(cameraId, name, host) {
      const cam = this.cameras.find((c) => c.camera_id === cameraId);
      name = name || (cam ? cam.name : cameraId);
      const row = host || document.querySelector(`[data-cam-row="${cameraId}"]`);
      if (!row) return this._doRemove(cameraId, name);
      const restore = row.innerHTML;
      row.innerHTML = `
        <div class="dev-main"><div class="dev-sub">Remove "${esc(name)}"? Recorded observations are kept.</div></div>
        <div class="dev-actions">
          <button class="btn btn-xs btn-danger" id="rmYes">Remove</button>
          <button class="btn btn-xs" id="rmNo">Keep</button>
        </div>`;
      row.querySelector('#rmNo').addEventListener('click', () => {
        if (host) row.innerHTML = restore; else this.renderCameras();
        if (host && window.blueprintEditor) window.blueprintEditor.emitInspector();
      });
      row.querySelector('#rmYes').addEventListener('click', () => this._doRemove(cameraId, name));
    },

    async _doRemove(cameraId, name) {
      try {
        const res = await fetch(`${API}/cameras/${cameraId}`, { method: 'DELETE' });
        if (!res.ok) throw new Error(res.statusText);
        status(`Removed "${name}".`, 'ok');
        if (window.calibrationTool && window.calibrationTool.cameraId === cameraId) window.calibrationTool.close();
        if (window.blueprintEditor) { window.blueprintEditor.select(null); await window.blueprintEditor.load(); }
        await this.refresh();
      } catch (e) {
        status(`Could not remove camera: ${e.message}`, 'error');
      }
    },
  };

  window.deviceManager = deviceManager;
  function initDevices() {
    if (document.getElementById('deviceManagerCard') && window.deviceManager) {
      window.deviceManager.init();
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initDevices);
  } else {
    initDevices();
  }
})();
