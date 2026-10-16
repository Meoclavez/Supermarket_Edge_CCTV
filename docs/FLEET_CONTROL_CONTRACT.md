# iKorex Fleet Control: contract (v1, 2026-10-16)

iKorex owns and rents out the edge boxes. The **master control dashboard** on the VPS lets
iKorex admins see every box's health and start/stop/restrict features remotely. It never
sees video, pictures, people, alerts' content, camera names, zone names or any other store
data. The box stays 100 % on-premise for store data.

## Owner decisions (2026-10-16)
1. **Offline rule: keep running, grace.** Losing contact with the VPS never stops anything.
   The box keeps its last valid signed policy. Only an explicit signed *Suspend* or a
   licence end date stops features. Past `licence.valid_until` the box shows a warning
   banner for `licence.grace_days` (default 14), then pauses. Licence dates are evaluated
   on the box's own clock, so they work offline.
2. **Suspend = pause AI + lock dashboard.** Analysis, alerts, phone alerts and night watch
   stop; store users see "Service paused — please contact iKorex" (with the admin's message);
   evidence, settings and the DB are kept; Resume restores instantly (app restart is fine).
   Health check-ins continue while paused.
3. **Admin site:** `https://control.ikorex.com.au`, own admin accounts, password (scrypt)
   + TOTP (RFC 6238), rate-limited, full audit log. Cloudflare Access may be added later.
4. **Telemetry: health + usage counts** (numbers only). Allowed fields are listed below;
   anything not listed must not be sent.

## Transport
Box -> VPS only (box dials out; no inbound port; works without frpc/online access):
`POST https://tunnel.ikorex.com.au/control/v1/checkin`
- Headers: `Authorization: Bearer <store token>`, `X-Store-Id: <store id>`,
  `Content-Type: application/json`. Body <= 32 KiB.
- Same credentials as the frp tunnel and offline watchdog (`remote_access` settings +
  secret `vps_tunnel_store_token`). The VPS verifies them via cctv-auth `POST /verify`.
- Cadence: 20 s after start, then every 300 s ± 30 s jitter; immediately after applying a
  policy or finishing a command; backoff 60 s -> 15 min on failure. Requires `server_url`
  (wss://host -> https://host) and a store id/token; otherwise idle (shown in Settings).
- Developer / QA only: env `FLEET_CONTROL_DEV_URL=http://127.0.0.1:<port>` sends the check-in to a
  local cctv-control instead of `https://<tunnel host>`. Honoured only for a loopback host
  (127.0.0.1, ::1, localhost); anything else is ignored with a warning. Store id/token and a
  `wss://` server address are still required.
- Gateway route: `location = /control/v1/checkin` on `tunnel.ikorex.com.au` -> `cctv-control:9200`,
  POST only, body 32k, same Cloudflare-only + rate limits as the watchdog route.

### Check-in request body
```json
{
  "version": 1,
  "store_id": "pearcedale",
  "sent_at": "2026-10-16T10:00:00Z",
  "box": {"software_version": "2.6.0", "git_commit": "3f7581e", "boot_id": "…",
          "uptime_s": 12345, "app_uptime_s": 600, "hostname_hash": "sha256-12hex",
          "os": "Ubuntu 26.04", "python": "3.14.0"},
  "health": {"cpu_pct": 41.2, "ram_pct": 55.0, "disk_pct": 38.1, "disk_free_gb": 812.4,
             "gpu_pct": 57.0, "gpu_mem_pct": 22.0, "cpu_temp_c": 61.0, "gpu_temp_c": 58.0,
             "inference_provider": "MIGraphXExecutionProvider", "inference_fps": 2.7,
             "cameras_total": 32, "cameras_enabled": 31, "cameras_online": 30,
             "pipeline_state": "running|paused|degraded|stopped",
             "services": {"pipeline": "ok", "online_access": "ok|off|error",
                          "phone_alerts": "ok|off", "backups": "ok|error",
                          "night_watch": "ok|off"},
             "last_backup_at": "…", "errors": ["inference_fallback_cpu"]},
  "usage": {"alerts_24h": {"low": 3, "medium": 1, "high": 0}, "incidents_24h": 4,
            "users_total": 3, "phones_registered": 2},
  "policy": {"version_applied": 7, "state": "active", "licence_status": "ok|warning|expired"},
  "control_key_fingerprint": "sha256:…|null",
  "command_results": [{"id": "c_…", "status": "done|failed|rejected", "detail": "…",
                       "finished_at": "…"}]
}
```
`errors` are fixed machine codes only (no free text, no paths, no names). No IP addresses
of cameras, no camera/zone/user names, no images, no counts of people.

### Check-in response body
```json
{
  "server_time": "…",
  "control_public_key": "base64 raw Ed25519 public key (32 bytes)",
  "policy": {"payload": "<base64url of canonical JSON>", "sig": "<base64url Ed25519>"} | null,
  "commands": [{"payload": "<base64url JSON>", "sig": "<base64url>"}],
  "next_checkin_s": 300
}
```
`policy` is returned when the VPS's version > `version_applied` (or always; box ignores
versions <= applied).

## Signing (Ed25519)
- The VPS holds one control signing key (`/data/control_ed25519.key`, 0600, generated on
  first start). Every policy and command is signed; payload = canonical JSON
  (`json.dumps(obj, sort_keys=True, separators=(",", ":"))`), base64url without padding.
- Box trust: **pin on first use**. The first successful authenticated check-in stores the
  public key in `system_setup` key `fleet_control` (`control_public_key`, fingerprint
  `sha256:` + first 16 hex of sha256(raw key)). A different key later is rejected and shown
  in Settings ("Control key changed — ask iKorex"); a local admin can reset the pin from
  Settings (admin only, audited locally). `.env` `FLEET_CONTROL_PUBLIC_KEY` (base64) overrides
  TOFU when set.
- Box rejects: bad signature, wrong `store_id`, policy `version` <= applied, command
  `not_after` passed or command id already seen (keep last 200 ids).

### Policy payload
```json
{
  "type": "policy", "store_id": "pearcedale", "version": 7, "issued_at": "…",
  "state": "active|suspended",
  "message": "optional text shown to store users when suspended (<= 300 chars)",
  "licence": {"valid_until": "2027-10-31" | null, "grace_days": 14},
  "features": {
    "theft_detection": true, "zone_alerts": true, "night_watch": true,
    "phone_alerts": true, "heatmaps": true, "business_insights": true,
    "online_access": true, "remote_live_video": true, "pos_integration": true
  },
  "limits": {"max_cameras": null}
}
```
Missing feature keys = enabled (forward compatible). Default policy before any contact:
everything enabled, state active, no licence date (a box never configured for fleet
control behaves exactly as today).

### Command payload
```json
{"type": "command", "id": "c_<random>", "store_id": "pearcedale",
 "action": "restart_app|checkin_now|run_health_check",
 "issued_at": "…", "not_after": "… (issued + 15 min)"}
```
Allow-list only. No shell, no file access, no data export, no reboot.
`restart_app`: reply via the next check-in after restart (persist pending result), then
exit so systemd (`Restart=always`) starts it again.

## Box effective state
`licence_status`: `ok` | `warning` (today > valid_until but within grace) | `expired`.
Effective paused = `state == "suspended"` or `licence_status == "expired"`.
- Paused: pipeline/analysis, pose analytics, push alerts, night watch, heatmap recorder,
  recommendations not started (or stopped); the API returns 423
  `{"code":"service_paused","message":…}` to store users for everything except auth,
  `/api/v1/fleet/status`, health and static assets; the dashboard shows a full-screen
  paused page with the iKorex logo and message. Online access tunnel stays up (so remote
  users see the paused page) but remote live video is refused.
- Feature off: the corresponding service is not started / is stopped, its API routes return
  403 `{"code":"feature_disabled","feature":"…"}`, and the UI hides its tab/card with a short
  "Not included in your plan" note where a user would look for it.
- `max_cameras`: enabling/adopting more cameras than the limit is refused (409
  `camera_limit`), existing extra cameras are not deleted but the newest beyond the limit are
  not analysed.
- Applying a changed policy: update gates live where cheap; otherwise restart the app
  (graceful, debounced 5 s). Persist policy + results in `system_setup` key `fleet_control`.

Box endpoints (session auth): `GET /api/v1/fleet/status` (any signed-in user: state,
message, licence status/date, features, last check-in time, fingerprint; no token)
and `POST /api/v1/fleet/reset-key` (admin), `POST /api/v1/fleet/checkin-now` (admin).
Settings card "iKorex service": plan status, licence date, features included, last contact,
control key fingerprint.

## VPS: `cctv-control` container (~/Projects-1/Server/cctv-tunnel/control/)
Python 3.13 alpine like cctv-auth, stdlib `http.server` + `cryptography` (pinned, for
Ed25519). Port 9200 on networks `cctv-edge` (gateway) and `cctv-internal` (to call cctv-auth
`/verify` and `/status`). Volume `cctv-control-data` -> `/data/control.db` (SQLite WAL).
- Tables: `admins` (username, scrypt hash, totp_secret, created_at, last_login_at,
  failed_count, locked_until), `sessions` (sha256(token), admin, created/last_seen/expires,
  ip), `stores` (store_id, display_name, customer, notes, created_at), `policies`
  (store_id, version, json, updated_by, updated_at), `checkins_latest` (store_id, json,
  received_at, ip), `checkins_history` (store_id, ts, compact health json; keep 14 days),
  `commands` (id, store_id, action, issued_by, issued_at, not_after, status, result,
  finished_at), `audit` (ts, admin, ip, action, store_id, detail).
- Admin web UI served by the container under `control.ikorex.com.au`: sign-in
  (password + TOTP, 5 failures -> 15 min lock, per-IP limiter), Fleet overview (online from
  cctv-auth `/status` + last check-in age, version, health chips, licence, state), store
  detail (health now + 24 h sparkline, feature toggles, licence date, suspend/resume with
  typed confirmation and message, commands with results, audit), Audit page, Admins page.
  iKorex logo, light/dark, phone width. Session cookie `__Host-ctl` (Secure, HttpOnly,
  SameSite=Strict, 12 h absolute / 30 min idle), CSRF header `X-CSRF` from a per-session token,
  strict CSP (no inline script), `X-Frame-Options DENY`.
- CLI `manage`: `add-admin <user>` (prompts password, prints TOTP secret + otpauth URI),
  `reset-totp`, `disable-admin`, `list-admins`, `show-key` (public key + fingerprint),
  `policy <store> [--suspend|--resume] [--feature k=on|off] [--valid-until D]`.
- No store data stored or displayed beyond the telemetry fields above.
