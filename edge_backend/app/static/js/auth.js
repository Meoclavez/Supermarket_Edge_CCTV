/**
 * Sign-in gate for the dashboard.
 *
 * Every API endpoint requires a bearer token. (The server's AUTH_DISABLED
 * switch is the only way to turn that off; DEBUG no longer does.)
 *
 * This module closes the gap:
 *   - wraps window.fetch so every same-origin API call carries the token,
 *   - shows a first-run account form when no operator account exists yet;
 *     it needs the one-time setup code the server prints at startup, so a
 *     stranger on the store LAN cannot claim the system first,
 *   - shows a sign-in form otherwise, and re-opens it on any 401.
 *
 * Loaded before every other script so no request escapes unauthenticated.
 *
 * A stored token is validated once, by GET /api/v1/auth/status, before any
 * other API request may leave the page: every other /api/ fetch waits for
 * that answer (``edgeAuth.ready``, event ``edge:auth``). A stale token used to
 * be sprayed by ~10 dashboard pollers at once; the server counted each as a
 * failed sign-in and locked the operator's own address out. An invalid token
 * is now cleared (localStorage + cookie) and never sent anywhere else.
 */
(function () {
  'use strict';

  const TOKEN_KEY = 'edge_cctv_token';

  // Over https (e.g. the remote-access tunnel) the session cookie must never be
  // sent on a plain-http request, so it is marked Secure there.
  const COOKIE_ATTRS = `path=/; SameSite=Strict; max-age=604800${location.protocol === 'https:' ? '; Secure' : ''}`;

  function getToken() {
    try { return localStorage.getItem(TOKEN_KEY); } catch (_) { return null; }
  }
  // The session cookie's token, as the browser will send it (null if none).
  function cookieToken() {
    try {
      const hit = document.cookie.split(';').map((c) => c.trim()).find((c) => c.startsWith(`${TOKEN_KEY}=`));
      return hit ? decodeURIComponent(hit.slice(TOKEN_KEY.length + 1)) || null : null;
    } catch (_) { return null; }
  }
  function setToken(t) {
    try {
      if (t) {
        localStorage.setItem(TOKEN_KEY, t);
        document.cookie = `${TOKEN_KEY}=${encodeURIComponent(t)}; ${COOKIE_ATTRS}`;
      } else {
        localStorage.removeItem(TOKEN_KEY);
        document.cookie = `${TOKEN_KEY}=; path=/; max-age=0`;
      }
    } catch (_) {}
  }

  // --- auth state ------------------------------------------------------------
  //
  // 'pending'  stored token not checked yet: API requests wait
  // 'valid'    the server accepted the stored token
  // 'invalid'  no usable token (cleared); the gate is shown
  // 'bypass'   AUTH_DISABLED on the server and no account yet
  // 'error'    the status check itself failed (server unreachable)
  let authState = 'pending';
  let resolveReady;
  const ready = new Promise((r) => { resolveReady = r; });

  // The stored token once it may be used: accepted by the server, or the
  // check could not reach the server (requests then carry it as before).
  function usableToken() {
    return authState === 'valid' || authState === 'error' ? getToken() : null;
  }

  function settleAuth(state) {
    if (authState !== 'pending') return;
    authState = state;
    resolveReady(state);
    try { window.dispatchEvent(new CustomEvent('edge:auth', { detail: { state } })); } catch (_) {}
  }

  // Requests allowed out before the stored token has been checked.
  const PRE_AUTH_PATHS = ['/api/v1/auth/status', '/api/v1/auth/refresh'];
  function pathOf(url) {
    try { return new URL(url, window.location.href).pathname; } catch (_) { return url; }
  }

  // --- authenticated fetch -------------------------------------------------

  const nativeFetch = window.fetch.bind(window);
  let isGateRendering = false;

  window.fetch = async function (input, init) {
    const rawUrl = typeof input === 'string' ? input : (input && input.url) || '';
    const path = pathOf(rawUrl);
    const sameOrigin = (() => {
      try { return new URL(rawUrl, window.location.href).origin === window.location.origin; } catch (_) { return false; }
    })();
    const url = sameOrigin ? path : rawUrl;
    const isApi = sameOrigin && url.startsWith('/api/');
    const isAuthOrSetup = url.includes('/auth/') || url.includes('/setup/');

    // Nothing but the token check itself leaves before the check has answered.
    if (isApi && authState === 'pending' && !PRE_AUTH_PATHS.includes(path)) {
      await ready;
    }

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

  function escapeHtml(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  // Turn an API error response into one readable sentence for the inline
  // error line. Never uses alert(): the operator must be able to correct the
  // field and resubmit without the page being blocked.
  async function apiError(res, fallback) {
    let data = {};
    try { data = await res.json(); } catch (_) { /* non-JSON body */ }
    let detail = data && data.detail;
    if (Array.isArray(detail)) {
      detail = detail.map((d) => (d && d.msg) ? `${(d.loc || []).slice(-1)[0] || 'field'}: ${d.msg}` : String(d)).join('; ');
    }
    if (res.status === 429) {
      return detail || 'Too many failed attempts from this device. Wait a few minutes and try again.';
    }
    return detail || fallback;
  }

  const SETUP_CODE_HINT = `
    The one-time setup code is printed by the server when it starts with no operator account.
    Find it in the terminal running <code>./run.sh</code>, in
    <code>journalctl -u edge-cctv</code> on a systemd install, or in the file
    <code>storage/setup_code.txt</code> on the server.`;

  const FORGOT_HELP = `
    There is no e-mail recovery on an offline edge box. On the server itself, from the
    <code>edge_backend</code> folder, run:
    <pre>../.venv/bin/python scripts/manage_operator.py reset-password --username YOUR_NAME</pre>
    To see the account names: <code>scripts/manage_operator.py list</code>.
    To start over with a new owner account (all operator accounts are removed, cameras and
    data are kept): <code>scripts/manage_operator.py reset-setup</code>, then use the setup
    code it prints.`;

  function gateMarkup(adminExists) {
    const first = !adminExists;
    return `
      <div class="auth-card">
        <div class="auth-brand">EDGE AI CCTV</div>
        <div class="auth-title">${first ? 'Create the operator account' : 'Sign in'}</div>
        <div class="auth-sub">${first
          ? 'No operator account exists yet. Enter the setup code shown on the server, then choose the credentials that will control this store’s system.'
          : 'Enter your operator credentials to view the store dashboard.'}</div>
        <form id="authForm" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" novalidate>
          ${first ? `
          <div class="fp-field"><label for="auCode">Setup code</label>
            <input id="auCode" name="op_setup_code" type="text" required maxlength="16"
                   class="auth-code-input" placeholder="XXXX-XXXX"
                   autocomplete="off" autocorrect="off" autocapitalize="characters" spellcheck="false"
                   data-lpignore="true" data-1p-ignore="true" data-form-type="other">
            <div class="auth-hint" id="auCodeHint">${SETUP_CODE_HINT}</div></div>
          <div class="fp-field"><label for="auDisplay">Your name (optional)</label>
            <input id="auDisplay" name="op_display" type="text" value="" placeholder="e.g. Store Manager"
                   autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" data-lpignore="true"></div>` : ''}
          <div class="fp-field"><label for="auUser">Username</label>
            <input id="auUser" name="op_user" type="text" required value=""
                   autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false"
                   data-lpignore="true" placeholder="e.g. manager"></div>
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
          <div class="auth-helper-note">Use the eye button to show the password while typing.</div>
          <div class="auth-error" id="authError" role="alert" aria-live="assertive"></div>
          <button class="btn btn-primary" type="submit" id="authSubmit" style="width:100%">
            ${first ? 'Create account' : 'Sign in'}
          </button>
          ${first ? '' : `
          <details class="auth-forgot" id="authForgot">
            <summary>Forgot password?</summary>
            <div class="auth-hint">${FORGOT_HELP}</div>
          </details>`}
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
      const target = gate.querySelector('#auCode') || gate.querySelector('#auUser') || gate.querySelector('#auPass');
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
        if (!username) throw new Error('Enter a username.');
        if (!password) throw new Error('Enter a password.');
        let data = null;
        if (!adminExists) {
          const code = (gate.querySelector('#auCode').value || '').trim();
          const confirmPw = (gate.querySelector('#auPass2') ? gate.querySelector('#auPass2').value : '') || '';
          if (!code) throw new Error('Enter the setup code shown on the server.');
          if (password.length < 8) throw new Error('Password must be at least 8 characters.');
          if (password !== confirmPw) throw new Error('Passwords do not match.');
          const r = await nativeFetch('/api/v1/setup/admin', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              username, password,
              setup_code: code,
              display_name: (gate.querySelector('#auDisplay') ? gate.querySelector('#auDisplay').value.trim() : '') || username,
            }),
          });
          if (!r.ok) throw new Error(await apiError(r, 'Could not create the account.'));
          data = await r.json();
        }

        if (!data || !data.access_token) {
          // Ordinary sign-in (or an older server that did not return a session).
          const r = await nativeFetch('/api/v1/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password }),
          });
          if (!r.ok) {
            throw new Error(await apiError(r, r.status === 401
              ? 'Username or password is incorrect.'
              : 'Sign-in failed.'));
          }
          data = await r.json();
        }
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

  // A sign-out control in the page header, shown only with a live session.
  function mountSignOut() {
    if (document.getElementById('authSignOut')) return;
    const host = document.querySelector('.header-actions') || document.querySelector('header');
    if (!host) return;
    const b = document.createElement('button');
    b.type = 'button';
    b.id = 'authSignOut';
    b.className = 'btn btn-sm';
    b.textContent = 'Sign out';
    b.title = 'Sign out of the dashboard';
    b.addEventListener('click', () => window.edgeAuth.signOut());
    host.appendChild(b);
  }

  let signingOut = false;
  window.edgeAuth = {
    // Revoke the session on the server first (POST /api/v1/auth/logout), then
    // clear it here. A failed or slow call still signs out locally: the
    // server-side revocation is best effort from the browser's point of view.
    async signOut() {
      if (signingOut) return;
      signingOut = true;
      const btn = document.getElementById('authSignOut');
      if (btn) { btn.disabled = true; btn.textContent = 'Signing out…'; }
      const t = getToken();
      if (t) {
        try {
          const ctl = typeof AbortController === 'function' ? new AbortController() : null;
          const timer = ctl ? setTimeout(() => ctl.abort(), 4000) : null;
          await nativeFetch('/api/v1/auth/logout', {
            method: 'POST',
            headers: { Authorization: `Bearer ${t}`, 'Content-Type': 'application/json' },
            body: '{}',
            signal: ctl ? ctl.signal : undefined,
          });
          if (timer) clearTimeout(timer);
        } catch (_) { /* offline or refused: still sign out locally */ }
      }
      setToken(null);
      window.location.reload();
    },
    // The stored token, only once the server has accepted it.
    token: usableToken,
    state() { return authState; },
    ready,
    // Run fn once the DOM is parsed and the stored token has been checked.
    onReady(fn) {
      const run = () => ready.then(() => { try { fn(); } catch (e) { console.error(e); } });
      if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run);
      else run();
    },
    isAuthenticated() {
      if (authState === 'pending' || authState === 'invalid') return false;
      const gate = document.getElementById('authGate');
      if (gate && gate.style.display !== 'none') return false;
      return !!getToken();
    },
    isGateOpen() {
      const gate = document.getElementById('authGate');
      return !!(gate && gate.style.display !== 'none');
    },
    // For <img>/<a> URLs, which cannot send the Authorization header. The
    // server also accepts the session cookie (verify_api_access), so while
    // the cookie carries this same token the URL stays clean: a token in the
    // query string ends up in browser history and proxy logs. Only without
    // that cookie (blocked, or a stale one) is the token appended.
    authUrl(url) {
      const t = usableToken();
      if (!t) return url;
      if (cookieToken() === t) return url;
      const sep = url.includes('?') ? '&' : '?';
      return `${url}${sep}token=${encodeURIComponent(t)}`;
    },
  };

  // --- boot ----------------------------------------------------------------
  async function fetchStatus(currentToken) {
    // A few quick retries: a server that is still starting must not sign the
    // operator out, and nothing else may be sent until this answers.
    let lastErr = null;
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        const res = await nativeFetch('/api/v1/auth/status', {
          headers: currentToken ? { Authorization: `Bearer ${currentToken}` } : {},
          credentials: 'omit',   // judge the stored token alone, not a stale cookie
        });
        if (res.ok) return await res.json();
        lastErr = new Error(`HTTP ${res.status}`);
      } catch (e) {
        lastErr = e;
      }
      await new Promise((r) => setTimeout(r, 800 * (attempt + 1)));
    }
    throw lastErr || new Error('auth status unavailable');
  }

  async function initAuth() {
    const currentToken = getToken();
    let s;
    try {
      s = await fetchStatus(currentToken);
    } catch (e) {
      console.error('Could not determine auth state:', e);
      settleAuth('error');
      return;
    }
    if (s.authenticated) {
      // Valid session: refresh the cookie (MJPEG <img> and websocket use it).
      try { document.cookie = `${TOKEN_KEY}=${encodeURIComponent(currentToken)}; ${COOKIE_ATTRS}`; } catch (_) {}
      settleAuth('valid');
      mountSignOut();
      return;
    }
    // A stored token the server no longer accepts (expired, signed out, or the
    // operator accounts were reset on the server): discard it everywhere.
    setToken(null);
    if (s.debug_bypass_active && s.admin_exists === false) {
      // Development convenience only: the backend is not enforcing auth and
      // no account has been created yet, so do not block the dashboard.
      // Driven by the server's AUTH_DISABLED switch (never by DEBUG).
      console.warn('AUTH_DISABLED is set on the server: the API is currently unauthenticated.');
      settleAuth('bypass');
      return;
    }
    // Show the gate before releasing the waiting requests, so they are
    // answered locally (401) instead of reaching the server.
    showGate(s.admin_exists);
    settleAuth('invalid');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAuth);
  } else {
    initAuth();
  }
})();
