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

    // ---------------------------------------------------- Dahua NVR Support
    dahuaConfig: null,

    toggleDahuaPanel() {
      const panel = el('dahuaNvrPanel');
      if (!panel) return;
      const isHidden = panel.style.display === 'none';
      panel.style.display = isHidden ? 'block' : 'none';
      if (isHidden) {
        this.loadNvrConfig();
      }
    },

    togglePassVis(inputId) {
      const input = el(inputId);
      if (!input) return;
      input.type = input.type === 'password' ? 'text' : 'password';
    },

    async loadNvrConfig() {
      try {
        const res = await fetch('/api/v1/dahua/credentials');
        if (!res.ok) return;
        const data = await res.json();
        if (!data.credentials) return;
        this.dahuaConfig = data.credentials;

        const u = el('nvrUser');
        if (u && !u.value) u.value = data.credentials.default_username || 'admin';
        const p = el('nvrPort');
        if (p && !p.value) p.value = data.credentials.default_port || 554;
        const ch = el('nvrChannels');
        if (ch) ch.value = data.credentials.default_channels || 16;

        const passStatus = el('nvrPassStatus');
        const passInput = el('nvrPass');
        if (data.credentials.has_password) {
          if (passStatus) passStatus.textContent = '✓ Saved on disk';
          if (passInput) passInput.placeholder = '•••••••• (Saved on disk)';
        }

        // Auto-fill host if saved NVR exists
        const hostInput = el('nvrHost');
        if (hostInput && !hostInput.value && data.credentials.nvrs) {
          const hosts = Object.keys(data.credentials.nvrs);
          if (hosts.length > 0) hostInput.value = hosts[0];
        }
      } catch (_) { /* transient */ }
    },

    async saveNvrCreds() {
      const host = (el('nvrHost')?.value || '').trim();
      const port = parseInt(el('nvrPort')?.value, 10) || 554;
      const username = (el('nvrUser')?.value || 'admin').trim();
      const password = el('nvrPass')?.value || '';
      const channels = parseInt(el('nvrChannels')?.value, 10) || 16;

      const stat = el('dahuaProbeStatus');
      if (stat) {
        stat.textContent = 'Saving credentials to disk…';
        stat.className = 'fp-status fp-info';
      }

      try {
        const res = await fetch('/api/v1/dahua/credentials', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ host, port, username, password, default_channels: channels }),
        });
        if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
        const data = await res.json();
        if (stat) {
          stat.textContent = '✓ NVR credentials saved to disk (persisted across restarts)';
          stat.className = 'fp-status fp-ok';
        }
        await this.loadNvrConfig();
      } catch (e) {
        if (stat) {
          stat.textContent = `Failed to save credentials: ${e.message}`;
          stat.className = 'fp-status fp-error';
        }
      }
    },

    async probeNvr() {
      const host = (el('nvrHost')?.value || '').trim();
      if (!host) {
        alert('Please enter the Dahua NVR IP address (e.g. 192.168.1.108)');
        el('nvrHost')?.focus();
        return;
      }
      const port = parseInt(el('nvrPort')?.value, 10) || 554;
      const username = (el('nvrUser')?.value || 'admin').trim();
      const password = el('nvrPass')?.value || '';
      const maxChannels = parseInt(el('nvrChannels')?.value, 10) || 16;
      const quality = el('nvrQuality')?.value || 'sub';

      const btn = el('btnProbeNvr');
      if (btn) { btn.disabled = true; btn.textContent = 'Probing channels…'; }

      const stat = el('dahuaProbeStatus');
      if (stat) {
        stat.textContent = `Connecting to ${host}:${port} and scanning channels 1..${maxChannels}…`;
        stat.className = 'fp-status fp-info';
      }

      const listHost = el('dahuaChannelsList');
      if (listHost) listHost.innerHTML = '<div style="font-size:11px; color:#8b949e; padding:8px;">Probing RTSP streams, please wait…</div>';

      try {
        const res = await fetch('/api/v1/dahua/probe', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            host, port, username, password, max_channels: maxChannels, save_credentials: true,
          }),
        });

        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || res.statusText);

        const probe = data.probe || {};
        if (!probe.reachable) {
          if (stat) {
            stat.textContent = `❌ ${probe.error || 'NVR is unreachable on port ' + port}`;
            stat.className = 'fp-status fp-error';
          }
          if (listHost) listHost.innerHTML = '<div class="fp-empty">No response from NVR. Check IP and network connection.</div>';
          return;
        }

        if (!probe.authenticated) {
          if (stat) {
            stat.textContent = `🔒 Authentication failed (401). Invalid username or password for ${host}`;
            stat.className = 'fp-status fp-error';
          }
          if (listHost) listHost.innerHTML = '<div class="fp-empty">Please verify NVR password and click Scan again.</div>';
          return;
        }

        const activeCount = probe.active_channels_count || 0;
        if (stat) {
          stat.textContent = `✓ Found ${activeCount} active camera feed(s) across ${probe.channel_count_scanned} channels on Dahua NVR.`;
          stat.className = activeCount > 0 ? 'fp-status fp-ok' : 'fp-status fp-warn';
        }

        this.renderDahuaChannels(host, port, username, password, probe.channels || [], quality);
      } catch (e) {
        if (stat) {
          stat.textContent = `Probe failed: ${e.message}`;
          stat.className = 'fp-status fp-error';
        }
        if (listHost) listHost.innerHTML = '';
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = '🔍 Scan NVR Channels'; }
      }
    },

    renderDahuaChannels(host, port, username, password, channels, defaultQuality) {
      const listHost = el('dahuaChannelsList');
      if (!listHost) return;

      if (!channels.length) {
        listHost.innerHTML = '<div class="fp-empty">No channels found.</div>';
        return;
      }

      let html = `
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
          <span style="font-size: 11px; font-weight: 700; color: #e6edf3;">Channels on ${esc(host)}:</span>
          <button class="btn btn-xs btn-primary" id="btnAdoptDahuaBatch">Adopt Selected</button>
        </div>
        <div style="display: flex; flex-direction: column; gap: 4px;">
      `;

      channels.forEach((c) => {
        const isActive = c.active;
        html += `
          <div class="dev-row" style="padding: 5px 8px; ${isActive ? 'border-color: rgba(0,255,157,0.3); background: rgba(0,255,157,0.03);' : 'opacity: 0.6;'}">
            <input type="checkbox" class="dahua-ch-cb" data-channel="${c.channel}" ${isActive ? 'checked' : ''} style="cursor: pointer;">
            <div class="dev-main">
              <div class="dev-name" style="font-size: 11px;">Channel ${c.channel}: Dahua NVR Ch ${c.channel}</div>
              <div class="dev-sub" style="font-size: 9.5px;">
                ${isActive ? `<span style="color:#00ff9d; font-weight:bold;">● LIVE</span> · ${c.resolution || '720p'} · ${c.fps || 25} FPS` : '<span style="color:#8b949e;">○ No Signal</span>'}
              </div>
            </div>
            <div class="dev-actions">
              <span class="dev-tag ${isActive ? 'ok' : ''}">${isActive ? (c.preferred_subtype === 1 ? 'Substream' : 'Mainstream') : 'Offline'}</span>
            </div>
          </div>
        `;
      });

      html += '</div>';
      listHost.innerHTML = html;

      const adoptBtn = listHost.querySelector('#btnAdoptDahuaBatch');
      if (adoptBtn) {
        adoptBtn.addEventListener('click', async () => {
          const checked = Array.from(listHost.querySelectorAll('.dahua-ch-cb:checked')).map((cb) => ({
            channel: parseInt(cb.dataset.channel, 10),
            name: `Dahua NVR Ch ${cb.dataset.channel}`,
            department: 'GENERAL',
            quality: defaultQuality,
          }));

          if (!checked.length) {
            alert('Please select at least one channel to adopt.');
            return;
          }

          adoptBtn.disabled = true;
          adoptBtn.textContent = 'Adopting…';

          try {
            const res = await fetch('/api/v1/dahua/adopt', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                host, port, username, password, channels: checked,
              }),
            });
            if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
            const data = await res.json();
            status(`Successfully adopted ${data.adopted_count} Dahua NVR channel(s) onto blueprint!`, 'ok');
            if (window.blueprintEditor) await window.blueprintEditor.load();
            await this.refresh();
            this.toggleDahuaPanel();
          } catch (e) {
            alert(`Failed to adopt channels: ${e.message}`);
            adoptBtn.disabled = false;
            adoptBtn.textContent = 'Adopt Selected';
          }
        });
      }
    },

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
