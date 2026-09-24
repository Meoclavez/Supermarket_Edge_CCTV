/**
 * Camera purpose (roles), the per-camera setup checklist and the POS register
 * link for checkout lanes.
 *
 * APIs (routes/camera_roles.py):
 *   GET  /api/v1/camera-roles              presets for the pickers
 *   PUT  /api/v1/cameras/{id}/role         {role, apply_defaults}
 *   GET  /api/v1/cameras/{id}/setup        checklist, `done` computed server-side
 *   PUT  /api/v1/cameras/{id}/pos-register {register_id}
 *   GET  /api/v1/store/setup               per-camera progress for badges
 *   GET  /api/v1/store/pos-registers       register ids seen in POS rows
 *
 * Mount points left by the dashboard shell: #acRoleMount (Add camera form),
 * #cameraRoleMount (camera settings modal), .cam-role-slot[data-role-slot]
 * on each camera tile. Device rows call edgeRoles.badgeHtml().
 *
 * Nothing here invents a state: a checklist item is ticked only when the
 * server says it is done, and a camera without a role says so.
 */
(function () {
  'use strict';

  const ROLE_ICON = {
    entrance: '🚪', exit: '🏃', entrance_exit: '↔️', checkout: '🧾',
    aisle: '🛒', high_value: '💎', stockroom: '📦', overview: '👁️',
  };
  const FEATURE_LABEL = {
    people_counting: 'People counting',
    shelf_interaction: 'Shelf reaches',
    theft_detection: 'Theft cues',
  };
  const POLL_MS = 30000;
  // The old "Area" / "Department" values a camera may still carry, mapped to
  // the purpose they meant. Used only to pre-select a suggestion; nothing is
  // saved until the operator presses "Use".
  const LEGACY_AREA_ROLE = {
    ENTRANCE: 'entrance', EXIT: 'exit', CHECKOUT: 'checkout', AISLE: 'aisle', SHELF: 'aisle',
    DEPARTMENT: 'aisle', STOCKROOM: 'stockroom', GENERAL: 'overview',
  };

  const state = {
    presets: [],            // GET /camera-roles
    byId: {},               // role id -> preset
    cameras: {},            // camera id -> {role, name, pos_register_id, features}
    progress: {},           // camera id -> {required_done, required_total, complete, missing}
    loadedAt: 0,
    newRole: null,          // selection in the Add camera form
    editRole: null,         // selection in the settings modal
    editCameraId: null,
    registers: null,
  };
  let timer = null;

  const $ = (id) => document.getElementById(id);
  const esc = (v) => (typeof escapeHtml === 'function' ? escapeHtml(v) : String(v == null ? '' : v));
  const toast = (m, k) => { if (typeof showToast === 'function') showToast(m, k); };
  const authed = () => (typeof canPoll === 'function' ? canPoll() : true);

  async function getJ(url, fallback) {
    if (typeof getJSON === 'function') return getJSON(url, fallback);
    try { const r = await fetch(url); return r.ok ? await r.json() : fallback; } catch (_) { return fallback; }
  }

  async function send(method, url, body) {
    const res = await fetch(url, {
      method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    if (!res.ok) {
      const d = data && data.detail;
      const msg = typeof d === 'string' ? d
        : Array.isArray(d) ? d.map((x) => x.msg).join('; ')
        : `HTTP ${res.status}`;
      const err = new Error(res.status === 401 ? 'Your session has expired. Sign in again.' : msg);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  // ------------------------------------------------------------- data
  async function loadPresets() {
    if (state.presets.length) return state.presets;
    const list = await getJ('/api/v1/camera-roles', null);
    if (Array.isArray(list)) {
      state.presets = list;
      state.byId = {};
      list.forEach((p) => { state.byId[p.id] = p; });
    }
    return state.presets;
  }

  async function loadCameras() {
    if (!authed()) return;
    const [cams, setup] = await Promise.all([
      getJ('/api/v1/cameras', null),
      getJ('/api/v1/store/setup', null),
    ]);
    if (cams && Array.isArray(cams.cameras)) {
      const next = {};
      cams.cameras.forEach((c) => {
        next[c.id] = { role: c.role || null, name: c.name, pos_register_id: c.pos_register_id || null, features: c.features || {} };
      });
      state.cameras = next;
    }
    if (setup && Array.isArray(setup.cameras)) {
      const p = {};
      setup.cameras.forEach((c) => { p[c.camera_id] = c; });
      state.progress = p;
    }
    state.loadedAt = Date.now();
    fillTileSlots();
    if (window.deviceManager && typeof window.deviceManager.renderCameras === 'function') {
      try { window.deviceManager.renderCameras(); } catch (_) { /* optional */ }
    }
  }

  function roleOf(cameraId) {
    const c = state.cameras[cameraId];
    return c ? c.role : null;
  }

  function roleLabel(role) {
    const p = state.byId[role];
    return p ? p.label : role;
  }

  // ------------------------------------------------------------ badges
  /** Small role badge (device rows, tiles). Empty string until data has loaded. */
  function badgeHtml(cameraId, opts) {
    const o = opts || {};
    if (!state.loadedAt || !state.cameras[cameraId]) return '';
    const role = roleOf(cameraId);
    if (!role) {
      return `<span class="role-badge role-badge-none" data-role-badge="${esc(cameraId)}" title="No purpose set: choose what this camera looks at in its settings">No purpose set</span>`;
    }
    const p = state.byId[role] || {};
    const pr = state.progress[cameraId];
    const prog = o.progress && pr && pr.required_total
      ? ` <span class="role-badge-prog">${pr.required_done}/${pr.required_total}</span>` : '';
    return `<span class="role-badge role-badge-set" data-role-badge="${esc(cameraId)}" data-role="${esc(role)}" title="${esc(p.description || '')}">`
      + `<span aria-hidden="true">${ROLE_ICON[role] || '•'}</span> ${esc(p.label || role)}${prog}</span>`;
  }

  function progressText(cameraId) {
    const pr = state.progress[cameraId];
    if (!pr || !pr.role) return null;
    if (!pr.required_total) return 'Setup';
    return pr.complete ? 'Setup done' : `Setup ${pr.required_done}/${pr.required_total}`;
  }

  /** Fill every empty or stale .cam-role-slot on the camera tiles. */
  function fillTileSlots() {
    document.querySelectorAll('.cam-role-slot[data-role-slot]').forEach((slot) => {
      const id = slot.getAttribute('data-role-slot');
      if (!state.loadedAt || !state.cameras[id]) return;
      const role = roleOf(id);
      const pr = state.progress[id];
      const sig = `${role}|${pr ? `${pr.required_done}/${pr.required_total}` : ''}`;
      if (slot.getAttribute('data-role-sig') === sig) return;
      slot.setAttribute('data-role-sig', sig);
      if (!role) {
        slot.innerHTML = `
          <span class="role-slot-text">What does this camera look at?</span>
          <button type="button" class="btn btn-xs btn-primary" data-role-action="set-purpose" data-camera="${esc(id)}">Set purpose</button>`;
        return;
      }
      const done = pr && pr.complete;
      const missing = pr && pr.missing && pr.missing.length ? `Still to do: ${pr.missing.join(', ')}` : 'Every required step is done';
      slot.innerHTML = `
        ${badgeHtml(id)}
        <button type="button" class="btn btn-xs role-setup-btn ${done ? 'is-done' : ''}" data-role-action="open-checklist" data-camera="${esc(id)}" title="${esc(missing)}">${done ? '✓ ' : ''}${esc(progressText(id) || 'Setup')}</button>`;
    });
  }

  // ---------------------------------------------------------- picker
  function previewHtml(p, currentFeatures) {
    if (!p) return '';
    const cur = currentFeatures || null;
    const feats = Object.keys(FEATURE_LABEL).map((k) => {
      const on = !!(p.features || {})[k];
      let change = '';
      if (cur && typeof cur[k] === 'boolean' && cur[k] !== on) change = ` <span class="role-change">(now ${cur[k] ? 'on' : 'off'})</span>`;
      return `<li class="${on ? 'is-on' : 'is-off'}"><span aria-hidden="true">${on ? '✓' : '–'}</span> ${esc(FEATURE_LABEL[k])}: <b>${on ? 'on' : 'off'}</b>${change}</li>`;
    }).join('');
    const extras = [];
    if (isFinite(p.theft_sensitivity) && p.theft_sensitivity !== 1) {
      extras.push(p.theft_sensitivity > 1
        ? `Theft cues are more sensitive here (×${p.theft_sensitivity}).`
        : `Theft cues are less sensitive here (×${p.theft_sensitivity}).`);
    }
    if (p.alert_severity_floor) extras.push(`Alerts from this camera are at least ${String(p.alert_severity_floor).toLowerCase()} priority.`);
    if (p.person_max_frame_fraction) extras.push(`Close-up shoppers are allowed (up to ${Math.round(p.person_max_frame_fraction * 100)} % of the picture).`);
    if (p.counts_footfall === false) extras.push('People seen here are not counted as shoppers.');
    else if (p.primary_footfall) extras.push('Its counting line is the store\'s main visitor count.');
    const measures = (p.analytics || []).map((a) => `<li>${esc(a)}</li>`).join('');
    return `
      <div class="role-desc">${esc(p.description)}</div>
      <div class="role-tip"><b>Mounting tip:</b> ${esc(p.mounting_tip)}</div>
      <div class="role-preview-cols">
        <div><div class="role-preview-h">Switches on</div><ul class="role-feat-list">${feats}</ul>
          ${extras.length ? `<div class="role-extras">${extras.map(esc).join(' ')}</div>` : ''}</div>
        ${measures ? `<div><div class="role-preview-h">Measures</div><ul class="role-measure-list">${measures}</ul></div>` : ''}
      </div>`;
  }

  function optionsHtml(name, selected) {
    return state.presets.map((p) => `
      <label class="role-option ${p.id === selected ? 'is-selected' : ''}" data-role-option="${esc(p.id)}" title="${esc(p.description)}">
        <input type="radio" name="${esc(name)}" value="${esc(p.id)}" ${p.id === selected ? 'checked' : ''}>
        <span class="role-ico" aria-hidden="true">${ROLE_ICON[p.id] || '•'}</span>
        <span class="role-opt-label">${esc(p.label)}</span>
      </label>`).join('');
  }

  function syncOptionClasses(host, selected) {
    host.querySelectorAll('.role-option').forEach((o) => {
      o.classList.toggle('is-selected', o.getAttribute('data-role-option') === selected);
    });
  }

  /** Add camera form: pick a role; it is sent with POST /api/v1/cameras. */
  async function mountNew() {
    const host = $('acRoleMount');
    if (!host) return;
    await loadPresets();
    if (!state.presets.length) {
      host.innerHTML = '<div class="role-picker"><div class="fp-empty">Camera purposes could not be loaded. You can set one later in the camera\'s settings.</div></div>';
      return;
    }
    host.innerHTML = `
      <fieldset class="role-picker role-picker-new" data-role-picker="new">
        <legend class="role-picker-title">What does this camera look at?</legend>
        <div class="role-picker-sub">This decides what it measures. You can change it later.</div>
        <div class="role-options" role="radiogroup" aria-label="Camera purpose">${optionsHtml('acRole', state.newRole)}</div>
        <div class="role-detail" id="acRoleDetail" aria-live="polite">${state.newRole ? previewHtml(state.byId[state.newRole]) : '<div class="role-detail-empty">Pick one to see what it switches on and where to mount it.</div>'}</div>
      </fieldset>`;
    host.querySelectorAll('input[name="acRole"]').forEach((r) => r.addEventListener('change', () => {
      state.newRole = r.value;
      syncOptionClasses(host, r.value);
      $('acRoleDetail').innerHTML = previewHtml(state.byId[r.value]);
    }));
  }

  /** The Add form's chosen role (null = not chosen). */
  function newCameraRole() { return state.newRole; }

  function resetNew() {
    state.newRole = null;
    mountNew();
  }

  /** Called by devices.js once POST /api/v1/cameras succeeded. */
  async function afterCameraAdded(cam) {
    state.newRole = null;
    await loadCameras();
    mountNew();
    if (cam && cam.id) {
      if (cam.role) openChecklist(cam.id, { justAdded: true });
      else toast(`Added "${cam.name}". Set its purpose in its settings to get a setup checklist.`, 'ok');
    }
  }

  /** Settings modal: current role, picker, preview, apply, register link, checklist. */
  async function mountEdit(cameraId, cam) {
    const host = $('cameraRoleMount');
    if (!host) return;
    state.editCameraId = cameraId;
    await loadPresets();
    const current = (cam && cam.role) || roleOf(cameraId) || null;
    const legacy = !current && cam ? String(cam.department || '').toUpperCase() : '';
    const suggested = legacy && state.byId[LEGACY_AREA_ROLE[legacy]] ? LEGACY_AREA_ROLE[legacy] : null;
    state.editRole = current || suggested;
    if (!state.presets.length) {
      host.innerHTML = '<div class="role-picker"><div class="fp-empty">Camera purposes could not be loaded. Try again in a moment.</div></div>';
      return;
    }
    const curLabel = current ? `${ROLE_ICON[current] || ''} ${esc(roleLabel(current))}` : 'not set';
    host.innerHTML = `
      <fieldset class="role-picker role-picker-edit" data-role-picker="edit">
        <legend class="role-picker-title">Camera purpose</legend>
        <div class="role-picker-sub">What this camera looks at. Currently: <b id="roleCurrentLabel">${curLabel}</b></div>
        ${suggested ? `<div class="role-suggest" id="cfgRoleSuggest">Suggested from this camera's old area setting (${esc(legacy)}): <b>${esc(roleLabel(suggested))}</b>. Check it and press Use, or pick another.</div>` : ''}
        <div class="role-options" role="radiogroup" aria-label="Camera purpose">${optionsHtml('cfgRole', current || suggested)}</div>
        <div class="role-detail" id="cfgRoleDetail" aria-live="polite"></div>
        <div class="role-apply-row" id="cfgRoleApplyRow" hidden>
          <button type="button" class="btn btn-sm btn-primary" id="btnApplyRole" data-role-action="apply-role">Use this purpose</button>
          <label class="role-keep"><input type="checkbox" id="roleKeepSwitches"> Keep my current analysis switches</label>
          ${current ? '<button type="button" class="btn btn-sm" id="btnClearRole" data-role-action="clear-role">Remove purpose</button>' : ''}
        </div>
        <div class="form-status" id="cfgRoleStatus" role="status" aria-live="polite"></div>
        <div class="role-register" id="cfgRoleRegister" hidden></div>
        <div class="role-checklist-inline" id="cfgRoleChecklist"></div>
      </fieldset>`;
    host.querySelectorAll('input[name="cfgRole"]').forEach((r) => r.addEventListener('change', () => {
      state.editRole = r.value;
      syncOptionClasses(host, r.value);
      renderEditDetail();
    }));
    renderEditDetail();
    if (current) {
      const setup = await getJ(`/api/v1/cameras/${encodeURIComponent(cameraId)}/setup`, null);
      if (state.editCameraId === cameraId) renderInlineSetup(setup);
    }
  }

  function modalCamera() {
    const modal = $('modalCameraConfig');
    if (!modal) return {};
    try { return JSON.parse(modal.getAttribute('data-camera-object') || '{}'); } catch (_) { return {}; }
  }

  function setModalCamera(patch) {
    const modal = $('modalCameraConfig');
    if (!modal) return;
    const cam = Object.assign(modalCamera(), patch);
    modal.setAttribute('data-camera-object', JSON.stringify(cam));
  }

  function renderEditDetail() {
    const detail = $('cfgRoleDetail');
    const row = $('cfgRoleApplyRow');
    if (!detail) return;
    const cam = modalCamera();
    const current = cam.role || roleOf(state.editCameraId) || null;
    const chosen = state.editRole;
    detail.innerHTML = chosen ? previewHtml(state.byId[chosen], cam.features) : '<div class="role-detail-empty">Pick what this camera looks at. It decides what it measures and gives you a short setup checklist.</div>';
    if (row) {
      const changed = chosen && chosen !== current;
      row.hidden = !changed && !current;
      const btn = $('btnApplyRole');
      if (btn) {
        btn.hidden = !changed;
        btn.textContent = changed ? `Use "${roleLabel(chosen)}"` : 'Use this purpose';
      }
      const keep = $('roleKeepSwitches');
      if (keep && keep.closest('label')) keep.closest('label').hidden = !changed;
    }
  }

  function setEditStatus(msg, isError) {
    const s = $('cfgRoleStatus');
    if (!s) return;
    s.textContent = msg || '';
    s.classList.toggle('form-status-error', !!isError);
  }

  /** Reflect the applied defaults in the modal's switches so Save does not undo them. */
  function syncModalSwitches(features) {
    if (!features) return;
    const set = (id, v) => { const n = $(id); if (n && typeof v === 'boolean') n.checked = v; };
    set('featPeopleCounting', features.people_counting);
    set('featShelfInteraction', features.shelf_interaction);
    set('featTheftDetection', features.theft_detection);
    const frac = $('configPersonMaxFrac');
    if (frac && 'person_max_frame_fraction' in features) {
      const v = features.person_max_frame_fraction;
      frac.value = typeof v === 'number' && isFinite(v) ? Math.round(v * 1000) / 10 : '';
    }
  }

  async function applyRole(clear) {
    const camId = state.editCameraId;
    if (!camId) return;
    const role = clear ? null : state.editRole;
    const keep = $('roleKeepSwitches');
    const btn = clear ? $('btnClearRole') : $('btnApplyRole');
    if (btn) { btn.disabled = true; btn.textContent = clear ? 'Removing…' : 'Saving…'; }
    setEditStatus(clear ? 'Removing the purpose…' : 'Saving the purpose…', false);
    try {
      const res = await send('PUT', `/api/v1/cameras/${encodeURIComponent(camId)}/role`, {
        role, apply_defaults: !clear && !(keep && keep.checked),
      });
      const cam = modalCamera();
      const patch = { role: res.role, pos_register_id: res.pos_register_id };
      if (res.applied_defaults && Object.keys(res.applied_defaults).length && res.features) {
        patch.features = Object.assign({}, cam.features || {}, res.features);
        syncModalSwitches(res.features);
      }
      setModalCamera(patch);
      if (state.cameras[camId]) Object.assign(state.cameras[camId], { role: res.role, pos_register_id: res.pos_register_id });
      const label = $('roleCurrentLabel');
      if (label) label.textContent = res.role ? `${ROLE_ICON[res.role] || ''} ${res.role_label}` : 'not set';
      // Re-mount so the Remove button and preview match the new state.
      await mountEdit(camId, modalCamera());
      renderInlineSetup(res.setup);
      setEditStatus(res.role
        ? `Purpose saved: ${res.role_label}.${res.applied_defaults && Object.keys(res.applied_defaults).length ? ' Its analysis switches were applied below.' : ''} The setup checklist is below.`
        : 'Purpose removed. The analysis switches were left as they were.', false);
      toast(res.role ? `Purpose set: ${res.role_label}` : 'Purpose removed', 'ok');
      const list = $('cfgRoleChecklist');
      if (list && res.role) list.scrollIntoView({ behavior: 'instant', block: 'nearest' });
      loadCameras();
      refreshOthers();
    } catch (e) {
      setEditStatus(`Not saved: ${e.message}`, true);
      if (btn) { btn.disabled = false; btn.textContent = clear ? 'Remove purpose' : `Use "${roleLabel(state.editRole)}"`; }
    }
  }

  function refreshOthers() {
    if (window.edgeToday && typeof window.edgeToday.refresh === 'function') {
      try { window.edgeToday.refresh(); } catch (_) { /* optional */ }
    }
  }

  // ------------------------------------------------------ POS register
  async function renderRegister(setup) {
    const host = $('cfgRoleRegister');
    if (!host) return;
    const cam = modalCamera();
    const role = (setup && setup.role) || cam.role;
    if (role !== 'checkout') { host.hidden = true; host.innerHTML = ''; return; }
    host.hidden = false;
    const regs = await getJ('/api/v1/store/pos-registers', null);
    state.registers = regs;
    const current = (setup && setup.pos_register_id) || cam.pos_register_id || '';
    const list = (regs && regs.registers) || [];
    const opts = list.map((r) => `<option value="${esc(r.register_id)}">${esc(r.rows ? `${r.rows} sales seen` : 'no sales seen yet')}</option>`).join('');
    const connected = regs && regs.pos_connected;
    host.innerHTML = `
      <div class="role-register-h">Till / register for this lane</div>
      <div class="role-register-sub">${connected
        ? 'Pick the register id your point-of-sale system uses for this lane, so its sales are matched with the shoppers seen here.'
        : 'No sales data has been received yet. You can still type the register id your till uses; it is matched once sales arrive.'}</div>
      <div class="role-register-row">
        <label class="sr-only" for="roleRegisterInput">Register id</label>
        <input type="text" class="form-input" id="roleRegisterInput" list="roleRegisterList" maxlength="64" autocomplete="off" placeholder="e.g. REG-1" value="${esc(current)}">
        <datalist id="roleRegisterList">${opts}</datalist>
        <button type="button" class="btn btn-sm btn-primary" data-role-action="save-register">${current ? 'Update' : 'Link register'}</button>
        ${current ? '<button type="button" class="btn btn-sm" data-role-action="unlink-register">Unlink</button>' : ''}
      </div>
      <div class="form-status" id="roleRegisterStatus" role="status" aria-live="polite">${current ? esc(registerSeenText(current, list)) : ''}</div>`;
  }

  function registerSeenText(reg, list) {
    const r = (list || []).find((x) => x.register_id === reg);
    if (!r || !r.rows) return `Linked to ${reg}. No sales from this register have been received yet.`;
    return `Linked to ${reg}. ${r.rows} ${r.rows === 1 ? 'sale' : 'sales'} received, the latest ${typeof formatAgo === 'function' ? formatAgo(r.last_seen) : r.last_seen}.`;
  }

  async function saveRegister(unlink) {
    const camId = state.editCameraId;
    const input = $('roleRegisterInput');
    const status = $('roleRegisterStatus');
    const value = unlink ? null : (input ? input.value.trim() : '');
    if (!unlink && !value) {
      if (status) { status.textContent = 'Type or pick a register id first.'; status.classList.add('form-status-error'); }
      if (input) input.focus();
      return;
    }
    if (status) { status.textContent = 'Saving…'; status.classList.remove('form-status-error'); }
    try {
      const res = await send('PUT', `/api/v1/cameras/${encodeURIComponent(camId)}/pos-register`, { register_id: value });
      setModalCamera({ pos_register_id: res.pos_register_id });
      if (state.cameras[camId]) state.cameras[camId].pos_register_id = res.pos_register_id;
      renderInlineSetup(res.setup);
      const s2 = $('roleRegisterStatus');
      if (s2) {
        s2.textContent = res.pos_register_id
          ? (res.pos_rows_seen ? `Linked to ${res.pos_register_id}. ${res.pos_rows_seen} ${res.pos_rows_seen === 1 ? 'sale' : 'sales'} received.` : `Linked to ${res.pos_register_id}. No sales from this register have been received yet, so the checklist item stays open until they arrive.`)
          : 'Register unlinked.';
      }
      toast(res.pos_register_id ? `Register ${res.pos_register_id} linked` : 'Register unlinked', 'ok');
      loadCameras();
      refreshOthers();
    } catch (e) {
      const s2 = $('roleRegisterStatus');
      if (s2) { s2.textContent = `Not saved: ${e.message}`; s2.classList.add('form-status-error'); }
    }
  }

  // --------------------------------------------------------- checklist
  function studioUrl(cameraId, action) {
    const q = new URLSearchParams({ camera_id: cameraId });
    if (action.tool) q.set('tool', action.tool);
    if (action.kind) q.set('kind', action.kind);
    q.set('from', 'checklist');
    return `/dashboard/studio?${q.toString()}`;
  }

  function actionLabel(item) {
    const a = item.action || {};
    if (a.type === 'calibrate') return item.done ? 'Recalibrate' : 'Calibrate on the map';
    if (a.type === 'config') return item.done ? 'Change register' : 'Link register';
    if (a.type === 'studio') {
      const tool = { tripwire: 'counting line', product: 'shelf area', restricted: 'restricted area', mask: 'privacy mask', checkout: a.kind === 'queue' ? 'queue area' : 'checkout area' }[a.tool] || 'shape';
      return item.done ? `Edit ${tool}` : `Draw ${tool}`;
    }
    return 'Open';
  }

  function checklistHtml(setup, where) {
    if (!setup) return '<div class="fp-empty">The checklist could not be loaded. Try again in a moment.</div>';
    const items = setup.items || [];
    if (!setup.role || !items.length) {
      return `<div class="fp-empty">${esc(setup.message || 'Choose a purpose for this camera to get its setup checklist.')}</div>`;
    }
    const head = setup.required_total
      ? `<div class="rc-head"><span class="rc-score ${setup.complete ? 'is-done' : ''}">${setup.complete ? '✓ Ready' : `${setup.required_done} of ${setup.required_total} required steps done`}</span></div>`
      : '';
    const rows = items.map((it) => `
      <li class="rc-item ${it.done ? 'is-done' : 'is-todo'}" data-rc-item="${esc(it.id)}" data-done="${it.done ? '1' : '0'}">
        <span class="rc-mark" aria-hidden="true">${it.done ? '✓' : ''}</span>
        <span class="rc-body">
          <span class="rc-label">${esc(it.label)} <span class="rc-tag ${it.required ? 'is-req' : ''}">${it.required ? 'required' : 'optional'}</span>
            <span class="sr-only">${it.done ? 'done' : 'to do'}</span></span>
          ${it.hint ? `<span class="rc-hint">${esc(it.hint)}</span>` : ''}
        </span>
        <button type="button" class="btn btn-xs ${it.done ? '' : (it.required ? 'btn-primary' : '')} rc-action" data-role-action="rc-action" data-where="${esc(where)}" data-camera="${esc(setup.camera_id)}" data-item="${esc(it.id)}">${esc(actionLabel(it))}</button>
      </li>`).join('');
    return `${head}<ul class="rc-list">${rows}</ul>`;
  }

  const lastSetup = {};

  function renderInlineSetup(setup) {
    const host = $('cfgRoleChecklist');
    if (!host) return;
    if (setup && setup.camera_id) lastSetup[setup.camera_id] = setup;
    host.innerHTML = setup && setup.role
      ? `<div class="role-preview-h">Setup checklist</div>${checklistHtml(setup, 'modal')}`
      : '';
    renderRegister(setup);
  }

  function runAction(cameraId, itemId, where) {
    const setup = lastSetup[cameraId];
    const item = setup && (setup.items || []).find((i) => i.id === itemId);
    if (!item) return;
    const a = item.action || {};
    if (a.type === 'studio') {
      window.location.href = studioUrl(cameraId, a);
      return;
    }
    if (a.type === 'calibrate') {
      closeChecklist();
      if (typeof closeCameraConfigModal === 'function') closeCameraConfigModal();
      if (typeof switchTab === 'function') switchTab('map');
      setTimeout(() => { if (window.calibrationTool) window.calibrationTool.open(cameraId); }, 150);
      return;
    }
    if (a.type === 'config') {
      const focusField = () => {
        const input = $('roleRegisterInput');
        if (input) { input.scrollIntoView({ behavior: 'instant', block: 'center' }); input.focus(); }
      };
      if (where === 'modal') { focusField(); return; }
      closeChecklist();
      openSettings(cameraId).then(() => setTimeout(focusField, 250));
    }
  }

  async function openSettings(cameraId) {
    if (typeof openCameraConfigModal === 'function') await openCameraConfigModal(cameraId);
  }

  // --------------------------------------------------- checklist modal
  function ensureModal() {
    let m = $('roleChecklistModal');
    if (m) return m;
    m = document.createElement('div');
    m.className = 'modal-overlay';
    m.id = 'roleChecklistModal';
    m.setAttribute('role', 'dialog');
    m.setAttribute('aria-modal', 'true');
    m.setAttribute('aria-labelledby', 'rcTitle');
    m.innerHTML = `
      <div class="modal-card rc-modal-card">
        <div class="rc-modal-head">
          <h3 class="rc-title" id="rcTitle">Camera setup</h3>
          <button type="button" class="btn btn-sm" data-role-action="close-checklist">✕ Close</button>
        </div>
        <div class="rc-sub" id="rcSub"></div>
        <div id="rcBody"><div class="fp-empty">Loading…</div></div>
        <div class="rc-foot">
          <button type="button" class="btn btn-sm" data-role-action="rc-refresh">Check again</button>
          <button type="button" class="btn btn-sm" data-role-action="rc-settings">Change purpose</button>
        </div>
      </div>`;
    m.addEventListener('click', (e) => { if (e.target === m) closeChecklist(); });
    document.body.appendChild(m);
    return m;
  }

  let checklistCamera = null;

  async function openChecklist(cameraId, opts) {
    const o = opts || {};
    const m = ensureModal();
    checklistCamera = cameraId;
    m.setAttribute('data-camera', cameraId);
    m.style.display = 'flex';
    await renderChecklistModal(o);
  }

  async function renderChecklistModal(o) {
    const cameraId = checklistCamera;
    if (!cameraId) return;
    const body = $('rcBody');
    const setup = await getJ(`/api/v1/cameras/${encodeURIComponent(cameraId)}/setup`, null);
    if (checklistCamera !== cameraId) return;
    if (setup) lastSetup[cameraId] = setup;
    const title = $('rcTitle');
    if (title) title.textContent = setup ? `${setup.name || cameraId}: setup` : 'Camera setup';
    const sub = $('rcSub');
    if (sub) {
      const p = setup && setup.role ? state.byId[setup.role] : null;
      sub.innerHTML = setup && setup.role
        ? `${(o && o.justAdded) ? '<b>Camera added.</b> ' : ''}Purpose: <b>${ROLE_ICON[setup.role] || ''} ${esc(setup.role_label || setup.role)}</b>${p ? `. ${esc(p.mounting_tip)}` : ''}`
        : 'This camera has no purpose yet.';
    }
    if (body) body.innerHTML = checklistHtml(setup, 'checklist');
    const modal = $('roleChecklistModal');
    if (modal) modal.setAttribute('data-complete', setup && setup.complete ? '1' : '0');
  }

  function closeChecklist() {
    const m = $('roleChecklistModal');
    if (m) m.style.display = 'none';
    checklistCamera = null;
  }

  // ------------------------------------------------- Today setup card
  /** Per-camera rows for the Today "Store setup" card. */
  function storeCamerasHtml(setup) {
    const cams = (setup && setup.cameras) || [];
    if (!cams.length) return '';
    const rows = cams.map((c) => {
      const role = c.role;
      const state_ = !role ? 'No purpose set'
        : !c.required_total ? 'Nothing required'
        : c.complete ? 'Ready' : `${c.required_done}/${c.required_total} done`;
      const action = role
        ? `<button type="button" class="btn btn-xs ${c.complete ? '' : 'btn-primary'}" data-role-action="open-checklist" data-camera="${esc(c.camera_id)}">${c.complete ? 'Checklist' : 'Continue setup'}</button>`
        : `<button type="button" class="btn btn-xs btn-primary" data-role-action="set-purpose" data-camera="${esc(c.camera_id)}">Set purpose</button>`;
      return `<li class="rc-cam-row ${c.complete ? 'is-done' : ''}" data-setup-camera="${esc(c.camera_id)}">
          <span class="rc-cam-name">${esc(c.name)}</span>
          <span class="rc-cam-role">${role ? `${ROLE_ICON[role] || ''} ${esc(c.role_label || role)}` : '—'}</span>
          <span class="rc-cam-state">${esc(state_)}</span>
          ${action}
        </li>`;
    }).join('');
    return `<div class="rc-cams"><div class="rc-cams-h">Cameras</div><ul class="rc-cam-list">${rows}</ul></div>`;
  }

  // ------------------------------------------------------------- events
  document.addEventListener('click', (e) => {
    const t = e.target.closest('[data-role-action]');
    if (!t) return;
    const act = t.getAttribute('data-role-action');
    const cam = t.getAttribute('data-camera');
    if (act === 'open-checklist') { e.preventDefault(); openChecklist(cam); }
    else if (act === 'set-purpose') { e.preventDefault(); openSettings(cam).then(() => { const m = $('cameraRoleMount'); if (m) m.scrollIntoView({ behavior: 'instant', block: 'start' }); }); }
    else if (act === 'close-checklist') closeChecklist();
    else if (act === 'rc-refresh') { t.disabled = true; renderChecklistModal({}).finally(() => { t.disabled = false; toast('Checklist updated', 'ok'); }); }
    else if (act === 'rc-settings') { const c = checklistCamera; closeChecklist(); openSettings(c); }
    else if (act === 'rc-action') runAction(cam, t.getAttribute('data-item'), t.getAttribute('data-where'));
    else if (act === 'apply-role') applyRole(false);
    else if (act === 'clear-role') applyRole(true);
    else if (act === 'save-register') saveRegister(false);
    else if (act === 'unlink-register') saveRegister(true);
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && checklistCamera) closeChecklist();
  });

  // Coming back from Studio (or another tab): the checklist may have changed.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible' || !authed()) return;
    loadCameras();
    if (checklistCamera) renderChecklistModal({});
  });

  function watchTiles() {
    const grid = $('cameraMatrixGrid');
    if (!grid || typeof MutationObserver === 'undefined') return;
    new MutationObserver(() => fillTileSlots()).observe(grid, { childList: true });
  }

  /** Patch the camera settings modal: mount the picker whenever it opens. */
  function hookSettingsModal() {
    const orig = window.openCameraConfigModal;
    if (typeof orig !== 'function' || orig.__roles) return;
    const wrapped = async function (cameraId) {
      const out = await orig.apply(this, arguments);
      const modal = $('modalCameraConfig');
      let cam = {};
      try { cam = JSON.parse((modal && modal.getAttribute('data-camera-object')) || '{}'); } catch (_) { cam = {}; }
      await mountEdit(cameraId, cam);
      return out;
    };
    wrapped.__roles = true;
    window.openCameraConfigModal = wrapped;
    // Inline onclick handlers resolve the global binding at click time.
    try { openCameraConfigModal = wrapped; } catch (_) { /* declared as function: window binding suffices */ }
  }

  function openFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const cam = params.get('checklist');
    if (!cam) return;
    params.delete('checklist');
    const qs = params.toString();
    history.replaceState(null, '', `${window.location.pathname}${qs ? `?${qs}` : ''}${window.location.hash}`);
    openChecklist(cam);
  }

  async function init() {
    hookSettingsModal();
    watchTiles();
    await loadPresets();
    mountNew();
    await loadCameras();
    openFromUrl();
    clearInterval(timer);
    timer = setInterval(() => { if (document.visibilityState === 'visible') loadCameras(); }, POLL_MS);
    window.addEventListener('edge:cameras-changed', () => loadCameras());
    window.addEventListener('edge:tab', (e) => { if (e.detail && e.detail.tab === 'cameras') loadCameras(); });
  }

  window.edgeRoles = {
    badgeHtml, newCameraRole, resetNew, afterCameraAdded, openChecklist, closeChecklist,
    storeCamerasHtml, refresh: loadCameras, presets: () => state.presets.slice(),
    roleOf, roleLabel, icon: (r) => ROLE_ICON[r] || '',
  };

  if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') window.edgeAuth.onReady(init);
  else if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
