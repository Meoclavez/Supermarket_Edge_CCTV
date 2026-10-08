// Settings: "Your account" (change password) and "Accounts" (owners and
// administrators only). Owned by the accounts work.
//
// #settings-account   who is signed in, and an inline "Change password" form.
//                     POST /api/v1/auth/change-password
// #settings-accounts  accounts table: add, change role, reset password, remove.
//                     GET/POST /api/v1/users, PATCH/DELETE /api/v1/users/{id},
//                     POST /api/v1/users/{id}/reset-password
//
// Who is signed in comes from window.EdgeAuth (auth.js). The server is the
// authority on roles; hiding the Accounts card for operators is a courtesy.
// No prompt()/confirm()/alert(): every form and confirmation is inline.
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);

  const ROLE_LABEL = { owner: 'Owner', admin: 'Administrator', operator: 'Operator' };
  const ROLE_HELP = {
    owner: 'everything, including setup and accounts',
    admin: 'everything, including setup and accounts',
    operator: 'daily use: cameras, alerts, insights; can’t change setup',
  };

  let accounts = [];
  let accountsError = '';
  // Which inline panel is open: {kind: 'add'|'role'|'reset'|'remove', id}
  let open = null;
  let pwOpen = false;
  let pwMessage = { text: '', error: false };
  let rowMessage = { id: null, text: '', error: false };
  let listMessage = { text: '', error: false };

  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  async function apiError(res, fallback) {
    let data = {};
    try { data = await res.json(); } catch (_) { /* not JSON */ }
    let d = data && data.detail;
    if (Array.isArray(d)) d = d.map((x) => (x && x.msg) || String(x)).join('; ');
    return d || `${fallback} (HTTP ${res.status})`;
  }

  async function api(url, opts, fallback) {
    const res = await fetch(url, Object.assign({ cache: 'no-store' }, opts || {}));
    if (!res.ok) throw new Error(await apiError(res, fallback || 'Request failed'));
    return res.status === 204 ? null : res.json();
  }

  function jsonOpts(method, body) {
    return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  }

  function me() { return window.EdgeAuth || { role: null }; }

  function fmtTime(iso) {
    if (!iso) return 'never';
    const t = new Date(iso);
    return Number.isNaN(t.getTime()) ? String(iso) : t.toLocaleString();
  }

  function status(msg) {
    return `<div class="form-status ${msg.error ? 'form-status-error' : 'us-ok'}" role="status">${esc(msg.text || '')}</div>`;
  }

  // ============================================================ your account

  function renderAccount() {
    const host = $('settings-account');
    if (!host) return;
    const a = me();
    if (!a.known) {
      host.innerHTML = `<div class="card-title"><span>Your account</span></div>
        <div class="pr-hint">Signed-in account details are not available from this server.</div>`;
      return;
    }
    const role = a.role;
    const who = a.username
      ? `<div class="us-kv"><span class="us-k">Username</span><span class="us-v">${esc(a.username)}</span></div>
         ${a.displayName && a.displayName !== a.username
           ? `<div class="us-kv"><span class="us-k">Name</span><span class="us-v">${esc(a.displayName)}</span></div>` : ''}`
      : '<div class="pr-hint">This dashboard is not signed in to a named account.</div>';
    const form = pwOpen ? `
      <form class="us-form" id="usPwForm" autocomplete="off" novalidate>
        <div class="fp-field"><label for="usPwCurrent">Current password</label>
          <input id="usPwCurrent" type="password" class="form-input" autocomplete="current-password" required></div>
        <div class="fp-field"><label for="usPwNew">New password</label>
          <input id="usPwNew" type="password" class="form-input" autocomplete="new-password" required
                 placeholder="At least 8 characters"></div>
        <div class="fp-field"><label for="usPwConfirm">Confirm new password</label>
          <input id="usPwConfirm" type="password" class="form-input" autocomplete="new-password" required></div>
        <div class="pr-actions">
          <button type="submit" class="btn btn-primary btn-sm" id="usPwSave">Save new password</button>
          <button type="button" class="btn btn-secondary btn-sm" id="usPwCancel">Cancel</button>
        </div>
      </form>` : '';
    host.innerHTML = `
      <div class="card-title"><span>Your account</span>
        ${role ? `<span class="badge us-role us-role-${esc(role)}" id="usMyRole">${esc(ROLE_LABEL[role] || role)}</span>` : ''}</div>
      ${who}
      ${role ? `<div class="us-kv"><span class="us-k">Role</span><span class="us-v">${esc(ROLE_LABEL[role] || role)}: ${esc(ROLE_HELP[role] || '')}</span></div>` : ''}
      ${a.username && !pwOpen ? '<div class="pr-actions"><button type="button" class="btn btn-secondary btn-sm" id="usPwOpen">Change password</button></div>' : ''}
      ${form}
      ${status(pwMessage)}`;
    const openBtn = $('usPwOpen');
    if (openBtn) openBtn.addEventListener('click', () => {
      pwOpen = true; pwMessage = { text: '', error: false }; renderAccount();
      const f = $('usPwCurrent'); if (f) f.focus();
    });
    const cancel = $('usPwCancel');
    if (cancel) cancel.addEventListener('click', () => { pwOpen = false; pwMessage = { text: '', error: false }; renderAccount(); });
    const f = $('usPwForm');
    if (f) f.addEventListener('submit', onChangePassword);
  }

  async function onChangePassword(ev) {
    ev.preventDefault();
    const cur = $('usPwCurrent').value || '';
    const pw = $('usPwNew').value || '';
    const pw2 = $('usPwConfirm').value || '';
    const show = (text, error) => {
      pwMessage = { text, error };
      const host = $('settings-account');
      const st = host && host.querySelector('.form-status');
      if (st) { st.textContent = text; st.className = `form-status ${error ? 'form-status-error' : 'us-ok'}`; }
    };
    if (!cur) return show('Enter your current password.', true);
    if (pw.length < 8) return show('The new password must be at least 8 characters.', true);
    if (pw !== pw2) return show('The new passwords do not match.', true);
    if (pw === cur) return show('The new password must be different from the current one.', true);
    const btn = $('usPwSave');
    if (btn) btn.disabled = true;
    try {
      const r = await api('/api/v1/auth/change-password',
        jsonOpts('POST', { old_password: cur, new_password: pw }), 'Could not change the password');
      if (r && r.access_token && window.edgeAuth && window.edgeAuth.replaceSession) {
        window.edgeAuth.replaceSession(r.access_token);
      }
      pwOpen = false;
      pwMessage = {
        text: r && r.other_sessions_signed_out
          ? 'Password changed. Any other browser signed in to this account was signed out; this one stays signed in.'
          : 'Password changed.',
        error: false,
      };
      renderAccount();
    } catch (e) {
      show(e.message, true);
      if (btn) btn.disabled = false;
    }
  }

  // ============================================================== accounts

  function roleOptions(selected) {
    const roles = me().role === 'owner' ? ['owner', 'admin', 'operator'] : ['admin', 'operator'];
    if (selected && !roles.includes(selected)) roles.unshift(selected);
    return roles.map((r) => `<option value="${r}" ${r === selected ? 'selected' : ''}>${ROLE_LABEL[r]}</option>`).join('');
  }

  function canTouch(acc) {
    return !acc.is_you && (acc.role !== 'owner' || me().role === 'owner');
  }

  function renderAccounts() {
    const host = $('settings-accounts');
    if (!host) return;
    if (!me().isAdmin) { host.innerHTML = ''; host.hidden = true; return; }
    host.hidden = false;
    const rows = accounts.map(renderRow).join('');
    const addForm = open && open.kind === 'add' ? `
      <form class="us-form" id="usAddForm" autocomplete="off" novalidate>
        <div class="us-grid">
          <div class="fp-field"><label for="usAddUser">Username</label>
            <input id="usAddUser" class="form-input" maxlength="64" required placeholder="e.g. sam"
                   autocomplete="off" autocapitalize="off" spellcheck="false"></div>
          <div class="fp-field"><label for="usAddName">Name (optional)</label>
            <input id="usAddName" class="form-input" maxlength="128" placeholder="e.g. Sam Lee" autocomplete="off"></div>
          <div class="fp-field"><label for="usAddRole">Role</label>
            <select id="usAddRole" class="form-select">${roleOptions('operator')}</select></div>
          <div class="fp-field"><label for="usAddPw">Initial password</label>
            <input id="usAddPw" type="password" class="form-input" required placeholder="At least 8 characters"
                   autocomplete="new-password"></div>
          <div class="fp-field"><label for="usAddPw2">Confirm password</label>
            <input id="usAddPw2" type="password" class="form-input" required autocomplete="new-password"></div>
        </div>
        <div class="pr-hint">Give the person this password; they can change it under “Your account”.</div>
        <div class="pr-actions">
          <button type="submit" class="btn btn-primary btn-sm" id="usAddSave">Create account</button>
          <button type="button" class="btn btn-secondary btn-sm" data-act="close">Cancel</button>
        </div>
        <div class="form-status" id="usAddStatus"></div>
      </form>` : '';
    host.innerHTML = `
      <div class="card-title"><span>Accounts</span><span class="badge badge-neutral" id="usCount">${accounts.length}</span></div>
      <div class="us-roles">
        <div><b>Owner / Administrator</b>: everything, including setup and accounts.</div>
        <div><b>Operator</b>: daily use: cameras, alerts, insights; can’t change setup.</div>
      </div>
      ${accountsError ? `<div class="form-status form-status-error">${esc(accountsError)}</div>` : ''}
      ${accounts.length ? `<div class="us-table-wrap"><table class="us-table" id="usTable">
        <thead><tr><th>Name</th><th>Username</th><th>Role</th><th>Last sign-in</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table></div>` : (accountsError ? '' : '<div class="fp-empty">Loading…</div>')}
      ${open && open.kind === 'add' ? addForm
        : '<div class="pr-actions"><button type="button" class="btn btn-sm btn-primary" id="usAddOpen">Add account</button></div>'}
      ${status(listMessage)}`;
    bindAccounts(host);
  }

  function renderRow(acc) {
    const id = esc(acc.id);
    const isOpen = (kind) => open && open.id === acc.id && open.kind === kind;
    const touch = canTouch(acc);
    let panel = '';
    if (isOpen('role')) {
      panel = `<form class="us-inline" data-form="role" data-id="${id}">
          <label class="us-inline-label" for="usRole-${id}">New role</label>
          <select class="form-select" id="usRole-${id}" name="role">${roleOptions(acc.role)}</select>
          <button type="submit" class="btn btn-primary btn-sm">Save role</button>
          <button type="button" class="btn btn-secondary btn-sm" data-act="close">Cancel</button>
        </form>`;
    } else if (isOpen('reset')) {
      panel = `<form class="us-inline" data-form="reset" data-id="${id}" autocomplete="off" novalidate>
          <input type="password" class="form-input" name="pw" placeholder="New password" aria-label="New password"
                 autocomplete="new-password">
          <input type="password" class="form-input" name="pw2" placeholder="Confirm new password"
                 aria-label="Confirm new password" autocomplete="new-password">
          <button type="submit" class="btn btn-primary btn-sm">Save password</button>
          <button type="button" class="btn btn-secondary btn-sm" data-act="close">Cancel</button>
          <div class="pr-sub us-full">${esc(acc.display_name)} is signed out of every browser and signs in again with the new password.</div>
        </form>`;
    } else if (isOpen('remove')) {
      panel = `<div class="pr-confirm">Remove ${esc(acc.display_name)}? They are signed out at once, including phones paired to this account, and can no longer sign in.
          <div class="pr-actions">
            <button type="button" class="btn btn-danger btn-sm" data-act="remove-yes" data-id="${id}">Yes, remove</button>
            <button type="button" class="btn btn-secondary btn-sm" data-act="close">Keep</button>
          </div>
        </div>`;
    }
    const actions = acc.is_you
      ? '<span class="pr-sub">Use “Change password” above</span>'
      : touch ? `
          <button type="button" class="btn btn-secondary btn-sm" data-act="role" data-id="${id}">Change role</button>
          <button type="button" class="btn btn-secondary btn-sm" data-act="reset" data-id="${id}">Reset password</button>
          <button type="button" class="btn btn-danger btn-sm" data-act="remove" data-id="${id}">Remove</button>`
        : '<span class="pr-sub">Only an owner can change an owner</span>';
    const msg = rowMessage.id === acc.id && rowMessage.text
      ? `<div class="form-status ${rowMessage.error ? 'form-status-error' : 'us-ok'}">${esc(rowMessage.text)}</div>` : '';
    return `
      <tr data-id="${id}">
        <td>${esc(acc.display_name)} ${acc.is_you ? '<span class="badge us-you">You</span>' : ''}
          ${acc.is_active ? '' : '<span class="badge badge-warning">Disabled</span>'}</td>
        <td class="us-mono">${esc(acc.username)}</td>
        <td><span class="badge us-role us-role-${esc(acc.role)}">${esc(acc.role_label || ROLE_LABEL[acc.role] || acc.role)}</span></td>
        <td>${esc(fmtTime(acc.last_login))}</td>
        <td class="us-actions">${actions}</td>
      </tr>
      ${panel || msg ? `<tr class="us-panel-row"><td colspan="5">${panel}${msg}</td></tr>` : ''}`;
  }

  function findAcc(id) { return accounts.find((a) => a.id === id); }

  function bindAccounts(host) {
    const addOpen = $('usAddOpen');
    if (addOpen) addOpen.addEventListener('click', () => {
      open = { kind: 'add', id: null }; listMessage = { text: '', error: false }; renderAccounts();
      const f = $('usAddUser'); if (f) f.focus();
    });
    const addForm = $('usAddForm');
    if (addForm) addForm.addEventListener('submit', onAdd);
    host.querySelectorAll('[data-act]').forEach((b) => b.addEventListener('click', () => {
      const act = b.getAttribute('data-act');
      const id = b.getAttribute('data-id');
      if (act === 'close') { open = null; renderAccounts(); return; }
      if (act === 'remove-yes') { onRemove(id); return; }
      open = { kind: act, id };
      rowMessage = { id: null, text: '', error: false };
      listMessage = { text: '', error: false };
      renderAccounts();
      const first = host.querySelector(`[data-form="${act}"] input, [data-form="${act}"] select`);
      if (first) first.focus();
    }));
    host.querySelectorAll('form[data-form="role"]').forEach((f) => f.addEventListener('submit', onRole));
    host.querySelectorAll('form[data-form="reset"]').forEach((f) => f.addEventListener('submit', onReset));
  }

  async function onAdd(ev) {
    ev.preventDefault();
    const st = $('usAddStatus');
    const fail = (t) => { if (st) { st.textContent = t; st.className = 'form-status form-status-error'; } };
    const username = ($('usAddUser').value || '').trim();
    const pw = $('usAddPw').value || '';
    if (!username) return fail('Enter a username.');
    if (pw.length < 8) return fail('The password must be at least 8 characters.');
    if (pw !== ($('usAddPw2').value || '')) return fail('The passwords do not match.');
    const btn = $('usAddSave');
    if (btn) btn.disabled = true;
    try {
      const acc = await api('/api/v1/users', jsonOpts('POST', {
        username, password: pw, role: $('usAddRole').value,
        display_name: ($('usAddName').value || '').trim(),
      }), 'Could not create the account');
      open = null;
      listMessage = { text: `Account ${acc.username} created (${ROLE_LABEL[acc.role] || acc.role}).`, error: false };
      await loadAccounts();
    } catch (e) {
      fail(e.message);
      if (btn) btn.disabled = false;
    }
  }

  async function onRole(ev) {
    ev.preventDefault();
    const id = ev.currentTarget.getAttribute('data-id');
    const role = ev.currentTarget.querySelector('select').value;
    const acc = findAcc(id);
    try {
      await api(`/api/v1/users/${encodeURIComponent(id)}`, jsonOpts('PATCH', { role }), 'Could not change the role');
      open = null;
      rowMessage = { id, text: `${acc ? acc.display_name : 'Account'} is now ${ROLE_LABEL[role] || role}.`, error: false };
      await loadAccounts();
    } catch (e) {
      rowMessage = { id, text: e.message, error: true };
      renderAccounts();
    }
  }

  async function onReset(ev) {
    ev.preventDefault();
    const form = ev.currentTarget;
    const id = form.getAttribute('data-id');
    const pw = form.querySelector('[name="pw"]').value || '';
    const pw2 = form.querySelector('[name="pw2"]').value || '';
    const acc = findAcc(id);
    const fail = (t) => { rowMessage = { id, text: t, error: true }; renderAccounts(); };
    if (pw.length < 8) return fail('The new password must be at least 8 characters.');
    if (pw !== pw2) return fail('The new passwords do not match.');
    try {
      await api(`/api/v1/users/${encodeURIComponent(id)}/reset-password`,
        jsonOpts('POST', { new_password: pw }), 'Could not reset the password');
      open = null;
      rowMessage = { id, text: `Password reset. ${acc ? acc.display_name : 'The account'} was signed out and signs in with the new password.`, error: false };
      renderAccounts();
    } catch (e) {
      fail(e.message);
    }
  }

  async function onRemove(id) {
    const acc = findAcc(id);
    try {
      await api(`/api/v1/users/${encodeURIComponent(id)}`, { method: 'DELETE' }, 'Could not remove the account');
      open = null;
      listMessage = { text: `${acc ? acc.display_name : 'Account'} removed.`, error: false };
      await loadAccounts();
    } catch (e) {
      rowMessage = { id, text: e.message, error: true };
      open = null;
      renderAccounts();
    }
  }

  async function loadAccounts() {
    if (!me().isAdmin) { renderAccounts(); return; }
    try {
      const data = await api('/api/v1/users', null, 'Could not load the accounts');
      accounts = (data && data.accounts) || [];
      accountsError = '';
    } catch (e) {
      accountsError = e.message;
    }
    renderAccounts();
  }

  async function openSettings() {
    if (window.edgeAuth && window.edgeAuth.refreshAccount) await window.edgeAuth.refreshAccount();
    renderAccount();
    await loadAccounts();
  }

  function settingsVisible() {
    const tab = $('tab-settings');
    return !!(tab && tab.classList.contains('active') && !document.hidden);
  }

  window.addEventListener('edge:tab', (ev) => {
    if (ev.detail && ev.detail.tab === 'settings') openSettings();
  });
  window.addEventListener('edge:account', () => { renderAccount(); renderAccounts(); });

  const boot = () => {
    renderAccount();
    renderAccounts();
    if (settingsVisible()) openSettings();
  };
  if (window.edgeAuth && typeof window.edgeAuth.onReady === 'function') window.edgeAuth.onReady(boot);
  else if (document.readyState !== 'loading') boot();
  else document.addEventListener('DOMContentLoaded', boot);

  window.edgeUsers = { reload: openSettings };
})();
