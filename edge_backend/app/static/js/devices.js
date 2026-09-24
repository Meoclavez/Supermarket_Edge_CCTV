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

  /** Camera purpose badge from js/camera_roles.js (empty until it has loaded). */
  function roleBadge(cameraId) {
    return window.edgeRoles && typeof window.edgeRoles.badgeHtml === 'function'
      ? window.edgeRoles.badgeHtml(cameraId, { progress: true }) : '';
  }

  function roleOptions() {
    const list = window.edgeRoles && typeof window.edgeRoles.presets === 'function' ? window.edgeRoles.presets() : [];
    return list.map((p) => `<option value="${esc(p.id)}">${esc(p.label)}</option>`).join('');
  }

  /** Department label as stored by the server: upper case, GENERAL when empty. */
  function normDept(v) {
    const s = String(v || '').toUpperCase().replace(/[^A-Z0-9 _&/-]/g, '').replace(/\s+/g, ' ').trim().slice(0, 40);
    return s || 'GENERAL';
  }

  function status(msg, kind) {
    const e = el('deviceScanStatus');
    if (!e) return;
    e.textContent = msg || '';
    e.className = `fp-status fp-${kind || 'info'}`;
  }

  // Inline messages for the Dahua NVR panel (no alert()/confirm()/prompt()).
  function dahuaStatus(msg, kind) {
    const e = el('dahuaProbeStatus');
    if (!e) return;
    e.textContent = msg || '';
    e.className = `fp-status fp-${kind || 'info'}`;
  }

  const deviceManager = {
    devices: [],
    cameras: [],

    async init() {
      this.bindAddPanel();
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
        if (btn) { btn.disabled = false; btn.textContent = 'Scan network'; }
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
              ${roleBadge(c.camera_id)}
              <span class="dev-tag ${c.frame_width ? '' : 'warn'}">${frame}</span>
              <span class="dev-tag ${c.has_homography ? 'ok' : 'warn'}">${c.has_homography ? 'calibrated' : 'uncalibrated'}</span>
            </div>
          </div>
          <div class="dev-actions">
            <button class="btn btn-xs" onclick="deviceManager.config('${id}')" title="Position, bearing, field of view">Config</button>
            <button class="btn btn-xs" data-role-action="open-checklist" data-camera="${id}" title="What is left to set up for this camera's purpose">Checklist</button>
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
          <div class="fp-field"><label for="adRole">What does it look at?</label>
            <select id="adRole" name="role">
              <option value="">Not sure yet (set later)</option>
              ${roleOptions()}
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
          role: document.getElementById('adRole').value || undefined,
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
          if (window.edgeRoles) window.edgeRoles.afterCameraAdded({ id: cam.camera_id || cam.id, name: cam.name, role: cam.role || body.role || null });
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

    // ------------------------------------------------- Add camera by address
    //
    // Manual entry for cameras a scan cannot find (other subnets, MJPEG
    // encoders, USB capture, a test video file). "Test connection" opens the
    // stream on the server and shows the frame it got; "Save camera" posts to
    // the camera create API, which validates the source again and stores any
    // credentials encrypted, never inside the URL.

    SOURCE_HELP: {
      rtsp: { label: 'Stream URL', ph: 'rtsp://192.168.1.64:554/stream1', creds: true,
        hint: 'RTSP address of the camera or NVR channel. Put the login in the fields below, not in the URL.' },
      http: { label: 'MJPEG URL', ph: 'http://192.168.1.70:8080/video.mjpg', creds: true,
        hint: 'HTTP(S) Motion-JPEG stream, e.g. an encoder or ESP32 camera.' },
      onvif: { label: 'ONVIF device address', ph: '192.168.1.64 or 192.168.1.64:8080', creds: true,
        hint: 'The server asks the device for its stream address (ONVIF Media service).' },
      usb: { label: 'USB device', ph: '0  or  /dev/video0', creds: false,
        hint: 'Capture device on this server: an index (0, 1, …) or a /dev path.' },
      file: { label: 'Video file path', ph: '/home/operator/test-footage.mp4', creds: false,
        hint: 'A video file on this server, for testing an installation without a camera. Plays in a loop.' },
    },

    _addTested: null,

    bindAddPanel() {
      const panel = el('addCameraPanel');
      if (!panel || panel.dataset.bound) return;
      panel.dataset.bound = '1';
      el('acClose').addEventListener('click', () => this.toggleAddPanel(false));
      el('acType').addEventListener('change', () => { this.applySourceType(); this.clearPreview(); });
      el('acTest').addEventListener('click', () => this.testAddCamera());
      el('acSave').addEventListener('click', () => this.saveAddCamera());
      el('acPassToggle').addEventListener('click', (ev) => {
        const input = el('acPass');
        const show = input.type === 'password';
        input.type = show ? 'text' : 'password';
        ev.currentTarget.textContent = show ? 'Hide' : 'Show';
        ev.currentTarget.setAttribute('aria-pressed', String(show));
        ev.currentTarget.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
      });
      ['acUrl', 'acUser', 'acPass'].forEach((id) => el(id).addEventListener('input', () => {
        el(id).removeAttribute('aria-invalid');
        if (this._addTested) { this._addTested = null; this.markPreviewStale(); }
      }));
      el('acName').addEventListener('input', () => el('acName').removeAttribute('aria-invalid'));
      panel.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' && ev.target.tagName === 'INPUT') { ev.preventDefault(); this.saveAddCamera(); }
        if (ev.key === 'Escape') this.toggleAddPanel(false);
      });
      this.applySourceType();
    },

    toggleAddPanel(force) {
      const panel = el('addCameraPanel');
      const btn = el('btnToggleAddCamera');
      if (!panel) return;
      this.bindAddPanel();
      const open = typeof force === 'boolean' ? force : panel.hidden;
      panel.hidden = !open;
      if (btn) btn.setAttribute('aria-expanded', String(open));
      if (open) {
        const dahua = el('dahuaNvrPanel');
        if (dahua) dahua.style.display = 'none';
        el('acName').focus();
      }
    },

    applySourceType() {
      const type = el('acType').value;
      const help = this.SOURCE_HELP[type] || this.SOURCE_HELP.rtsp;
      el('acUrlLabel').textContent = help.label;
      el('acUrl').placeholder = help.ph;
      el('acUrlHint').textContent = help.hint;
      el('acCredRow').hidden = !help.creds;
    },

    addStatus(msg, kind) {
      const e = el('acStatus');
      if (!e) return;
      e.textContent = msg || '';
      e.className = `fp-status dev-add-status fp-${kind || 'info'}`;
    },

    clearPreview() {
      const fig = el('acPreview');
      if (fig) { fig.hidden = true; fig.classList.remove('is-stale'); }
      const img = el('acPreviewImg');
      if (img) img.removeAttribute('src');
      this._addTested = null;
    },

    markPreviewStale() {
      const fig = el('acPreview');
      if (fig && !fig.hidden) {
        fig.classList.add('is-stale');
        el('acPreviewCap').textContent = 'Settings changed since this test. Test again to confirm.';
      }
    },

    /** Read the form; mark the first missing field inline. Returns null if invalid. */
    readAddForm(requireName) {
      const type = el('acType').value;
      const creds = (this.SOURCE_HELP[type] || {}).creds;
      const body = {
        name: el('acName').value.trim(),
        source_type: type,
        url: el('acUrl').value.trim(),
        department: normDept(el('acDept').value),
        role: (window.edgeRoles && window.edgeRoles.newCameraRole()) || null,
        location: el('acLoc').value.trim(),
        username: creds ? el('acUser').value.trim() : '',
        password: creds ? el('acPass').value : '',
      };
      if (requireName && !body.name) {
        el('acName').setAttribute('aria-invalid', 'true');
        el('acName').focus();
        this.addStatus('Enter a name for this camera.', 'error');
        return null;
      }
      if (!body.url) {
        el('acUrl').setAttribute('aria-invalid', 'true');
        el('acUrl').focus();
        this.addStatus(`Enter the ${(this.SOURCE_HELP[type] || {}).label || 'stream URL'}.`, 'error');
        return null;
      }
      return body;
    },

    async _errorText(res) {
      if (res.status === 401) return 'Your session has expired. Sign in again.';
      if (res.status === 403) return 'This account is not allowed to add cameras.';
      try {
        const data = await res.json();
        const d = data && data.detail;
        if (typeof d === 'string') return d;
        if (Array.isArray(d) && d.length) {
          return d.map((x) => `${(x.loc || []).slice(-1)[0] || 'field'}: ${x.msg}`).join('; ');
        }
      } catch (_) { /* not JSON */ }
      return `${res.status} ${res.statusText}`;
    },

    async testAddCamera() {
      const body = this.readAddForm(false);
      if (!body) return;
      const btn = el('acTest');
      btn.disabled = true;
      btn.textContent = 'Testing…';
      this.addStatus('Opening the stream on the server and waiting for a frame…', 'info');
      try {
        const res = await fetch('/api/v1/cameras/test-connection', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            source_type: body.source_type, url: body.url,
            username: body.username || null, password: body.password || null,
            ...(body.role ? { role: body.role } : {}),
          }),
        });
        if (!res.ok) {
          const msg = await this._errorText(res);
          if (res.status === 422) el('acUrl').setAttribute('aria-invalid', 'true');
          this.clearPreview();
          this.addStatus(msg, 'error');
          return;
        }
        const data = await res.json();
        if (!data.success) {
          this.clearPreview();
          this.addStatus(data.error || 'No video received.', 'error');
          return;
        }
        this._addTested = data;
        const img = el('acPreviewImg');
        const fig = el('acPreview');
        fig.classList.remove('is-stale');
        img.width = data.preview_width || data.width;
        img.height = data.preview_height || data.height;
        img.src = data.preview_jpeg;
        fig.hidden = false;
        const fps = data.fps != null
          ? `${data.fps} fps${data.fps_source === 'measured' ? ' (measured)' : ''}`
          : (data.fps_reported != null ? `${data.fps_reported} fps (reported by stream)` : 'fps unknown');
        const via = data.resolved_via === 'onvif' ? ` · resolved ${data.resolved_url}` : '';
        el('acPreviewCap').textContent = `${data.width}×${data.height} · ${fps} · ${data.elapsed_ms} ms${via}`;
        this.addStatus(`Connected. This is the frame the server received.${data.mounting_tip ? ` Tip for ${data.role_label}: ${data.mounting_tip}` : ''}`, 'ok');
      } catch (e) {
        this.clearPreview();
        this.addStatus(`Test failed: ${e.message}`, 'error');
      } finally {
        btn.disabled = false;
        btn.textContent = 'Test connection';
      }
    },

    async saveAddCamera() {
      const body = this.readAddForm(true);
      if (!body) return;
      const btn = el('acSave');
      btn.disabled = true;
      btn.textContent = 'Saving…';
      this.addStatus('Saving…', 'info');
      try {
        const payload = {
          name: body.name, department: body.department, location: body.location,
          source_type: body.source_type, rtsp_url: body.url,
        };
        if (body.role) payload.role = body.role;
        if (body.username) payload.username = body.username;
        if (body.password) payload.password = body.password;
        const res = await fetch('/api/v1/cameras', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (!res.ok) {
          const msg = await this._errorText(res);
          if (res.status === 422 && /name/i.test(msg) && !/url|stream|path|device|host/i.test(msg)) {
            el('acName').setAttribute('aria-invalid', 'true');
          } else if (res.status === 422) {
            el('acUrl').setAttribute('aria-invalid', 'true');
          }
          this.addStatus(`Not saved: ${msg}`, 'error');
          return;
        }
        const cam = await res.json();
        // Clear the secret fields and the form for the next camera.
        ['acName', 'acUrl', 'acUser', 'acPass', 'acLoc', 'acDept'].forEach((id) => { el(id).value = ''; });
        this.clearPreview();
        this.addStatus('', 'info');
        this.toggleAddPanel(false);
        status(`Added "${cam.name}". It streams in the Live matrix once the first frame arrives; place and calibrate it on the plan.`, 'ok');
        if (window.blueprintEditor) await window.blueprintEditor.load();
        await this.refresh();
        if (typeof window.loadCamerasMatrix === 'function') window.loadCamerasMatrix();
        window.dispatchEvent(new CustomEvent('edge:cameras-changed', { detail: { camera_id: cam.id } }));
        // Straight to the purpose's setup checklist (or a hint to set one).
        if (window.edgeRoles) window.edgeRoles.afterCameraAdded(cam);
      } catch (e) {
        this.addStatus(`Not saved: ${e.message}`, 'error');
      } finally {
        btn.disabled = false;
        btn.textContent = 'Save camera';
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
        this.toggleAddPanel(false);
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
        dahuaStatus('Enter the recorder IP address first (e.g. 192.168.1.108).', 'error');
        el('nvrHost')?.setAttribute('aria-invalid', 'true');
        el('nvrHost')?.focus();
        return;
      }
      el('nvrHost')?.removeAttribute('aria-invalid');
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
      if (listHost) listHost.innerHTML = '<div style="font-size:11px; color: var(--text-dim); padding:8px;">Probing RTSP streams, please wait…</div>';

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
          stat.textContent = `✓ Found ${activeCount} active camera feed(s) across ${probe.channel_count_scanned} channels on the recorder.`;
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
          <span style="font-size: 11px; font-weight: 700; color: var(--text-soft);">Channels on ${esc(host)}:</span>
          <button class="btn btn-xs btn-primary" id="btnAdoptDahuaBatch">Adopt Selected</button>
        </div>
        <div style="display: flex; flex-direction: column; gap: 4px;">
      `;

      channels.forEach((c) => {
        const isActive = c.active;
        html += `
          <div class="dev-row" style="padding: 5px 8px; ${isActive ? 'border-color: rgba(var(--green-rgb), 0.3); background: rgba(var(--green-rgb), 0.03);' : 'opacity: 0.6;'}">
            <input type="checkbox" class="dahua-ch-cb" data-channel="${c.channel}" ${isActive ? 'checked' : ''} style="cursor: pointer;">
            <div class="dev-main">
              <div class="dev-name" style="font-size: 11px;">Channel ${c.channel}: Dahua NVR Ch ${c.channel}</div>
              <div class="dev-sub" style="font-size: 9.5px;">
                ${isActive ? `<span style="color: var(--accent-green); font-weight:bold;">● LIVE</span> · ${c.resolution || '720p'} · ${c.fps || 25} FPS` : '<span style="color: var(--text-dim);">○ No Signal</span>'}
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
            dahuaStatus('Select at least one channel to adopt.', 'warn');
            return;
          }

          adoptBtn.disabled = true;
          adoptBtn.textContent = 'Adopting…';
          dahuaStatus(`Adopting ${checked.length} channel(s)…`, 'info');

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
            status(`Added ${data.adopted_count} recorder channel(s). Place them on the map to count people per area.`, 'ok');
            if (window.blueprintEditor) await window.blueprintEditor.load();
            await this.refresh();
            this.toggleDahuaPanel();
          } catch (e) {
            dahuaStatus(`Failed to adopt channels: ${e.message}`, 'error');
            adoptBtn.disabled = false;
            adoptBtn.textContent = 'Adopt Selected';
          }
        });
      }
    },
  };

  window.deviceManager = deviceManager;
  function initDevices() {
    if (document.getElementById('deviceManagerCard') && window.deviceManager) {
      window.deviceManager.init();
    }
  }

  // Start only after auth.js has checked the stored token (edgeAuth.onReady).
  if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') {
    window.edgeAuth.onReady(initDevices);
  } else if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initDevices);
  } else {
    initDevices();
  }
})();
