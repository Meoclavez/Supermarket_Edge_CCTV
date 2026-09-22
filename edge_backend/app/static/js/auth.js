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
    try {
      if (t) {
        localStorage.setItem(TOKEN_KEY, t);
        document.cookie = `${TOKEN_KEY}=${encodeURIComponent(t)}; path=/; SameSite=Lax; max-age=604800`;
      } else {
        localStorage.removeItem(TOKEN_KEY);
        document.cookie = `${TOKEN_KEY}=; path=/; max-age=0`;
      }
    } catch (_) {}
  }

  // --- authenticated fetch -------------------------------------------------

  const nativeFetch = window.fetch.bind(window);
  let isGateRendering = false;

  window.fetch = async function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const isApi = url.startsWith('/api/');
    const isAuthOrSetup = url.includes('/auth/') || url.includes('/setup/');
    const token = getToken();
    const gate = document.getElementById('authGate');
    const isGateActive = gate && gate.style.display !== 'none';

    // If an unauthenticated background script attempts to poll the backend while
    // the auth gate is displayed, do NOT send network requests that will flood the
    // server and generate 401 terminal logs. Short-circuit immediately.
    if (isApi && !isAuthOrSetup && !token && isGateActive) {
      return new Response(JSON.stringify({ detail: 'Authentication required' }), {
        status: 401,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    const opts = Object.assign({}, init);

    if (isApi && token) {
      opts.headers = new Headers(opts.headers || (typeof input !== 'string' ? input.headers : undefined) || {});
      opts.headers.set('Authorization', `Bearer ${token}`);
    }

    const res = await nativeFetch(input, opts);

    // A 401 means the session ended or never existed. Re-open the gate rather
    // than letting every panel silently render an empty state.
    if (res.status === 401 && isApi && !isAuthOrSetup) {
      setToken(null);
      showGate();
    }
    return res;
  };

  // --- gate UI -------------------------------------------------------------

  const EYE_OPEN_ICON = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>`;
  const EYE_CLOSED_ICON = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"></path><line x1="1" y1="1" x2="23" y2="23"></line></svg>`;

  function gateMarkup(adminExists) {
    const first = !adminExists;
    return `
      <div class="auth-card">
        <div class="auth-brand">EDGE AI CCTV</div>
        <div class="auth-title">${first ? 'Create the operator account' : 'Sign in'}</div>
        <div class="auth-sub">${first
          ? 'This is the first run. Choose the credentials that will control this store’s system.'
          : 'Enter your operator credentials to view the store dashboard.'}</div>
        <form id="authForm" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false">
          ${first ? `
          <div class="fp-field"><label for="auDisplay">Your name</label>
            <input id="auDisplay" name="op_display" type="text" required value="Store Manager"
                   autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" data-lpignore="true"></div>` : ''}
          <div class="fp-field"><label for="auUser">Username</label>
            <input id="auUser" name="op_user" type="text" required
                   autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false"
                   data-lpignore="true" value="${first ? 'admin' : ''}" placeholder="e.g. admin"></div>
          <div class="fp-field">
            <label for="auPass">Password</label>
            <div class="auth-input-group">
              <input id="auPass" name="op_key" type="password" required
                     autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false"
                     data-lpignore="true" data-1p-ignore="true" data-form-type="other"
                     placeholder="${first ? 'At least 8 characters' : 'Enter password'}">
              <button type="button" class="auth-eye-btn" data-target="auPass" title="Show password" tabindex="-1">
                ${EYE_OPEN_ICON}
              </button>
            </div>
          </div>
          ${first ? `
          <div class="fp-field">
            <label for="auPass2">Confirm password</label>
            <div class="auth-input-group">
              <input id="auPass2" name="op_key_confirm" type="password" required
                     autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false"
                     data-lpignore="true" data-1p-ignore="true" data-form-type="other"
                     placeholder="Re-enter password">
              <button type="button" class="auth-eye-btn" data-target="auPass2" title="Show password" tabindex="-1">
                ${EYE_OPEN_ICON}
              </button>
            </div>
          </div>` : ''}
          <div class="auth-helper-note">💡 Click 👁️ to view password while typing</div>
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

    // CRITICAL: If the form is already rendered in the DOM, NEVER re-render!
    // Re-rendering wipes out any characters the user has typed into the password inputs!
    if (gate.querySelector('#authForm') || isGateRendering) {
      return;
    }

    isGateRendering = true;

    if (adminExists === undefined) {
      nativeFetch('/api/v1/auth/status')
        .then((r) => r.json())
        .then((s) => {
          if (!gate.querySelector('#authForm')) {
            renderGate(gate, s.admin_exists);
          }
        })
        .catch(() => {
          if (!gate.querySelector('#authForm')) {
            renderGate(gate, true);
          }
        })
        .finally(() => {
          isGateRendering = false;
        });
    } else {
      renderGate(gate, adminExists);
      isGateRendering = false;
    }
  }

  function renderGate(gate, adminExists) {
    gate.innerHTML = gateMarkup(adminExists);
    const form = gate.querySelector('#authForm');
    const err = gate.querySelector('#authError');

    // Password visibility toggles
    gate.querySelectorAll('.auth-eye-btn').forEach((btn) => {
      btn.addEventListener('click', (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        const targetId = btn.getAttribute('data-target');
        const input = gate.querySelector('#' + targetId);
        if (!input) return;
        const isPassword = input.type === 'password';
        input.type = isPassword ? 'text' : 'password';
        btn.innerHTML = isPassword ? EYE_CLOSED_ICON : EYE_OPEN_ICON;
        btn.title = isPassword ? 'Hide password' : 'Show password';
        input.focus();
      });
    });

    // Auto-focus the first appropriate field
    setTimeout(() => {
      const target = gate.querySelector('#auUser') || gate.querySelector('#auPass');
      if (target) {
        target.focus();
      }
    }, 80);

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      err.textContent = '';
      const btn = gate.querySelector('#authSubmit');
      btn.disabled = true;

      const username = (gate.querySelector('#auUser').value || '').trim();
      const password = gate.querySelector('#auPass').value || '';

      try {
        if (!adminExists) {
          const confirm = (gate.querySelector('#auPass2') ? gate.querySelector('#auPass2').value : '') || '';
          if (password !== confirm) throw new Error('Passwords do not match.');
          if (password.length < 8) throw new Error('Password must be at least 8 characters.');
          const r = await nativeFetch('/api/v1/setup/admin', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              username, password,
              display_name: (gate.querySelector('#auDisplay') ? gate.querySelector('#auDisplay').value.trim() : '') || username,
              role: 'owner',
            }),
          });
          if (!r.ok) {
            const errData = await r.json().catch(() => ({}));
            throw new Error(errData.detail || 'Could not create the account.');
          }
        }

        const r = await nativeFetch('/api/v1/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, password }),
        });
        if (!r.ok) {
          const errData = await r.json().catch(() => ({}));
          throw new Error(errData.detail || 'Sign-in failed. Please check credentials.');
        }
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
    isAuthenticated() {
      const gate = document.getElementById('authGate');
      if (gate && gate.style.display !== 'none') return false;
      return !!getToken();
    },
    isGateOpen() {
      const gate = document.getElementById('authGate');
      return !!(gate && gate.style.display !== 'none');
    },
    authUrl(url) {
      const t = getToken();
      if (!t) return url;
      const sep = url.includes('?') ? '&' : '?';
      return `${url}${sep}token=${encodeURIComponent(t)}`;
    },
  };

  // --- boot ----------------------------------------------------------------
  async function initAuth() {
    try {
      const currentToken = getToken();
      if (currentToken) {
        try { document.cookie = `${TOKEN_KEY}=${encodeURIComponent(currentToken)}; path=/; SameSite=Lax; max-age=604800`; } catch (_) {}
      }
      const res = await nativeFetch('/api/v1/auth/status', {
        headers: currentToken ? { Authorization: `Bearer ${currentToken}` } : {},
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
