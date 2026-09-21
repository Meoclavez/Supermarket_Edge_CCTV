/**
 * Sign-in gate for the dashboard.
 *
 * Every analytics endpoint requires a bearer token, but nothing in the
 * frontend ever sent one -- the dashboard worked only because the backend was
 * started with DEBUG=true, which bypasses authentication entirely. That is an
 * open API on a store network.
 *
 * This module closes the gap:
 *   - wraps window.fetch so every same-origin API call carries the token,
 *   - shows a first-run account form when no operator account exists yet,
 *   - shows a sign-in form otherwise, and re-opens it on any 401.
 *
 * Loaded before every other script so no request escapes unauthenticated.
 */
(function () {
  'use strict';

  const TOKEN_KEY = 'edge_cctv_token';

  function getToken() {
    try { return localStorage.getItem(TOKEN_KEY); } catch (_) { return null; }
  }
  function setToken(t) {
    try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch (_) {}
  }

  // --- authenticated fetch -------------------------------------------------

  const nativeFetch = window.fetch.bind(window);

  window.fetch = async function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const isApi = url.startsWith('/api/');
    const opts = Object.assign({}, init);
    const token = getToken();

    if (isApi && token) {
      opts.headers = new Headers(opts.headers || (typeof input !== 'string' ? input.headers : undefined) || {});
      opts.headers.set('Authorization', `Bearer ${token}`);
    }

    const res = await nativeFetch(input, opts);

    // A 401 means the session ended or never existed. Re-open the gate rather
    // than letting every panel silently render an empty state.
    if (res.status === 401 && isApi && !url.includes('/auth/')) {
      setToken(null);
      showGate();
    }
    return res;
  };

  // --- gate UI -------------------------------------------------------------

  function gateMarkup(adminExists) {
    const first = !adminExists;
    return `
      <div class="auth-card">
        <div class="auth-brand">EDGE AI CCTV</div>
        <div class="auth-title">${first ? 'Create the operator account' : 'Sign in'}</div>
        <div class="auth-sub">${first
          ? 'This is the first run. Choose the credentials that will control this store’s system.'
          : 'Enter your operator credentials to view the store dashboard.'}</div>
        <form id="authForm" autocomplete="on">
          ${first ? `
          <div class="fp-field"><label for="auDisplay">Your name</label>
            <input id="auDisplay" name="displayName" type="text" required value="Store Manager"></div>` : ''}
          <div class="fp-field"><label for="auUser">Username</label>
            <input id="auUser" name="username" type="text" required autocomplete="username" value=""></div>
          <div class="fp-field"><label for="auPass">Password</label>
            <input id="auPass" name="password" type="password" required
                   autocomplete="${first ? 'new-password' : 'current-password'}"></div>
          ${first ? `
          <div class="fp-field"><label for="auPass2">Confirm password</label>
            <input id="auPass2" name="confirmPassword" type="password" required autocomplete="new-password"></div>` : ''}
          <div class="auth-error" id="authError"></div>
          <button class="btn btn-primary" type="submit" id="authSubmit" style="width:100%">
            ${first ? 'Create account' : 'Sign in'}
          </button>
        </form>
      </div>`;
  }

  function showGate(adminExists) {
    let gate = document.getElementById('authGate');
    if (!gate) {
      gate = document.createElement('div');
      gate.id = 'authGate';
      gate.className = 'auth-gate';
      document.body.appendChild(gate);
    }
    gate.style.display = 'flex';

    if (adminExists === undefined) {
      nativeFetch('/api/v1/auth/status')
        .then((r) => r.json())
        .then((s) => renderGate(gate, s.admin_exists))
        .catch(() => renderGate(gate, true));
    } else {
      renderGate(gate, adminExists);
    }
  }

  function renderGate(gate, adminExists) {
    gate.innerHTML = gateMarkup(adminExists);
    const form = gate.querySelector('#authForm');
    const err = gate.querySelector('#authError');

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      err.textContent = '';
      const btn = gate.querySelector('#authSubmit');
      btn.disabled = true;

      const username = gate.querySelector('#auUser').value.trim();
      const password = gate.querySelector('#auPass').value;

      try {
        if (!adminExists) {
          const confirm = gate.querySelector('#auPass2').value;
          if (password !== confirm) throw new Error('Passwords do not match.');
          if (password.length < 8) throw new Error('Use at least 8 characters.');
          const r = await nativeFetch('/api/v1/setup/admin', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              username, password,
              display_name: gate.querySelector('#auDisplay').value.trim() || username,
              role: 'owner',
            }),
          });
          if (!r.ok) throw new Error((await r.json()).detail || 'Could not create the account.');
        }

        const r = await nativeFetch('/api/v1/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, password }),
        });
        if (!r.ok) throw new Error((await r.json()).detail || 'Sign-in failed.');
        const data = await r.json();
        if (!data.access_token) throw new Error('No session token was returned.');

        setToken(data.access_token);
        gate.style.display = 'none';
        // Reload so every panel refetches with the session attached.
        window.location.reload();
      } catch (ex) {
        err.textContent = ex.message;
        btn.disabled = false;
      }
    });
  }

  window.edgeAuth = {
    signOut() { setToken(null); window.location.reload(); },
    token: getToken,
  };

  // --- boot ----------------------------------------------------------------
  async function initAuth() {
    try {
      const res = await nativeFetch('/api/v1/auth/status', {
        headers: getToken() ? { Authorization: `Bearer ${getToken()}` } : {},
      });
      const s = await res.json();

      if (s.authenticated) return;                 // valid session, carry on
      if (s.debug_bypass_active && s.admin_exists === false) {
        // Development convenience only: the backend is not enforcing auth and
        // no account has been created yet, so do not block the dashboard.
        // A deployed instance must run without DEBUG, which makes this false.
        console.warn('DEBUG bypass active: the API is currently unauthenticated.');
        return;
      }
      showGate(s.admin_exists);
    } catch (e) {
      console.error('Could not determine auth state:', e);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAuth);
  } else {
    initAuth();
  }
})();
