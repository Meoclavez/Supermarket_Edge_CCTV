# Offline page, Report a problem, box issue log: contract (2026-10-23)

Shared contract for two parallel streams: **VPS** (`~/Projects-1/Server/cctv-tunnel`: `control/`,
`gateway-cctv.conf.template`, `push-gateway-conf.sh`, `frps.toml`, `docker-compose.yml`,
README) and **BOX** (this repo). Builds on `docs/FLEET_CONTROL_CONTRACT.md` (transport, store
token auth through cctv-auth `/verify`, Ed25519 signing, canonical JSON). Each stream edits only
its own tree. Do not commit; the integrator commits and deploys.

## Owner request (2026-10-23)
1. A "site unavailable" page for every edge box listed in the control portal. Anyone opening a
   store's public address (`<store>-cctv.ikorex.com.au`) while the box is offline sees it: tips
   and ways to fix the problem, and how to contact the support team.
2. "Report a problem" in the store dashboard; reports appear under **Issues** for that box in
   the control portal.
3. The box logs issues and errors (not all logs) back to the VPS; a developer clears them by
   hand. Must not flood VPS storage.

## Privacy and security rules (non-negotiable)
- No live video, pictures, or store personal data goes to the VPS. No screenshots in reports.
- The offline page is **public** (store hostnames have no gateway login). It shows only: the
  store's display name from the control record (fallback: "this store"), a coarse "iKorex last
  heard from this system" bucket (`within the last 15 minutes` / `within the last hour` /
  `today` / `more than a day ago` / `never`), the fix tips, and the support contact. No IPs,
  versions, camera counts, health numbers, error text, or exact timestamps.
- Error text from the box is redacted on the box before it leaves: `services/log_redaction.py`
  plus URLs (incl. rtsp), IPv4/IPv6, MAC, e-mail, hostnames, absolute paths outside the app,
  quoted strings, and any known camera/zone/user/store display name is replaced by its id or
  `<name>`. Opaque ids (`cam_xxxxxxxxxx`) are fine.
- Support contact shown to store users comes from the **signed policy** (never unsigned
  check-in fields: a spoofed phone number would be phishing).

## Support contact (VPS -> box, signed)
Global setting in cctv-control (admin UI "Support contact", `manage support --phone ...`),
optional per-store override. Delivered as a new policy field (changing it bumps the policy
version of the affected stores):
```json
"support": {"name": "iKorex support", "phone": "...", "email": "...", "hours": "...",
            "website": "https://...", "note": "..."} | null
```
Each value is plain text <= 120 chars (website must be https, e-mail validated). Missing/null =
unknown; the box falls back to `help_content.js` `SUPPORT` (null today). Box uses it in the Help
panel, the paused/standby page and the Report-a-problem dialog.

## 1. Offline page (VPS)
- **Detection.** frps answers for a store hostname with no registered proxy (box offline,
  frpc down, Online access off). Verify on frp v0.71.0 what frps returns (a) with no proxy for
  the Host and (b) with frpc connected but the box app not listening, and use frps
  `custom404Page` and/or nginx `proxy_intercept_errors` for *only* those responses. A real box
  response (any status, including the box's own 404/503 JSON) must pass through unchanged:
  never intercept by status alone if that can catch box responses. Document the finding.
- **Page.** Served on the store's own hostname under a reserved prefix `/__ikorex/` (nginx
  location on the store block, proxied to cctv-control's public listener, no admin cookie
  involved): `GET /__ikorex/offline` (HTML), `GET /__ikorex/offline.json`
  (`{store_name, last_heard, support, online:false}`), static assets under
  `/__ikorex/static/`. Host -> store id from the control record's hostname (no guessing;
  unknown host -> generic page without a store name). Strict CSP, no third-party assets,
  iKorex logo, light/dark, 360 px phones, Australian English, `noindex`.
- **Content.** Plain-language, ordered fixes: is the store's internet working (try another
  site on the store Wi-Fi); is the CCTV box powered (lights, fan); restart the router, then the
  box (hold power 5 s, wait 3 min); was Online access turned off in Settings; on the store
  network the dashboard still works at the box's local address (no address shown: "ask your
  manager for the local address"); if "last heard" is recent, the box is running and only the
  online link is down. Then the support contact block (or "Contact your iKorex installer" if
  none) and a store code (the store id) to quote.
- **Auto-return.** The page checks every 20 s (backoff to 2 min, stop when the tab is hidden)
  whether the box is back (`GET /api/v1/health` on the same host returns 200 from the box) and
  then returns to the page the visitor asked for (keep the original path; only same-origin
  relative paths).
- API/XHR requests (Accept JSON, `/api/` paths) for an offline box get a JSON 503
  `{"code":"box_offline","message":"..."}` instead of HTML.

## 2. Report a problem (BOX -> VPS)
- Dashboard: "Report a problem" in the header Help panel and Settings (all signed-in users).
  Dialog: what went wrong (required, 10..2000 chars), where (dashboard area select:
  Cameras, Store map, Alerts, Insights, Phone alerts, Online access, Other), how urgent
  (Normal / Urgent: store can't use the system), optional name/phone for a call-back (stored
  only if given, with a visible note). Notice: "Don't include customer details. No pictures
  or video are sent." Auto-attached diagnostics (shown to the user before sending, numbers and
  codes only): box version, uptime, cameras online/total, disk %, last 10 issue fingerprints
  with counts, current dashboard tab, browser width bucket.
- Stored on the box first (small table or JSON file in STORAGE_DIR, cap 200), status
  `queued`; sent immediately (poke the fleet loop) and retried on every check-in until
  accepted; works offline (shown as "Will send when the box is online").
- "My reports" list in the dialog: status from the VPS (`received`, `in_progress`, `fixed`,
  `closed`) plus an optional short reply from iKorex (<= 500 chars), synced back via the
  check-in response.

## 3. Box issue log (BOX -> VPS)
- A logging handler on the box captures level >= ERROR and uncaught exceptions (plus an
  explicit allowlist of important WARNING events, e.g. disk nearly full, inference fell back to
  CPU, DB locked, migration failed, backup failed, camera offline > 10 min). Not INFO/DEBUG.
- **Fingerprint** = sha256 of (logger, exception type, message template with numbers, ids,
  hex, quoted strings, timestamps normalised). Aggregate per fingerprint: first_seen,
  last_seen, count, level, logger, one redacted sample message (<= 500 chars), redacted
  traceback (last 12 frames, <= 4 KB, app paths only), affected ids count (e.g. 32 cameras).
- Stored on the box in a **separate small file/DB** (never the main 2.8 GB SQLite: no write
  lock contention), cap 500 fingerprints (evict oldest last_seen), cleared after the VPS
  acknowledges (count deltas, so a long-running error keeps one row).
- Upload: with each check-in (every 5 min) and right after a new ERROR fingerprint (debounced
  60 s): only changed fingerprints, max 50 per upload, body <= 48 KB.

## Transport (new endpoint)
`POST https://<tunnel host>/control/v1/issues` (gateway location on the tunnel block,
`client_max_body_size 64k`, same auth as check-in: Bearer store token + `X-Store-Id`, verified
via cctv-auth `/verify`; rate limit 30/min per store). Request:
```json
{"store_id": "pearcedale", "software_version": "2.7.0", "sent_at": "...",
 "reports": [{"id": "<uuid from box>", "created_at": "...", "area": "store_map",
              "urgency": "normal|urgent", "text": "...", "contact": {"name": "", "phone": ""} | null,
              "diagnostics": {...}}],
 "issues": [{"fingerprint": "<64 hex>", "level": "error|critical|warning", "logger": "edge.x",
             "message": "...", "traceback": "..." | null, "first_seen": "...", "last_seen": "...",
             "count_delta": 3, "affected": 32 | null}],
 "dropped": 0}
```
Response 200: `{"accepted_reports": ["<id>"...], "accepted_issues": ["<fp>"...]}`. 413 when
too big, 429 when rate limited (box backs off). Report status/replies go box-ward in the
**check-in response** (`report_updates: [{"id", "status", "reply", "updated_at"}]`, last 50
changed since the box's `report_cursor`, which the box sends in the check-in body).

Refinements agreed 2026-10-23 (both streams implement exactly this):
- **Diagnostics** keys (anything else is dropped by the VPS): `software_version`, `uptime_s`,
  `app_uptime_s`, `cameras_online`, `cameras_total`, `disk_pct`,
  `recent_issues: [{"fingerprint": "<64 hex>", "count": n}]` (<= 10), `tab`
  (`today|cameras|map|insights|loss|settings|studio|paused|other|null`), `width_bucket`
  (`lt600|600-1023|1024-1439|ge1440|null`). `contact` = `{"name","phone"}` (either may be `""`)
  or null. The box sends at most 10 reports and 50 issues per upload, urgent first, <= 48 KB.
- **Acknowledgement.** Only ids/fingerprints listed in a 200 `accepted_*` are acknowledged.
  A new fingerprint over the VPS caps is dropped, counted in `dropped_total` and still listed
  (so the box clears it instead of resending). A new report over the 1000-report cap is *not*
  listed: it stays queued on the box. The box resets its `dropped` after any valid 200; the VPS
  adds each upload's `dropped` to the store's box-side dropped total.
- **429** carries `Retry-After` (seconds; currently 60). Unknown/invalid report ids or
  fingerprints are skipped (not acknowledged); other bad fields get defaults (area `other`,
  urgency `normal`, level `error`, logger `unknown`, count_delta 1).
- **report_cursor** = the greatest `updated_at` string the box has received (null before
  any). `updated_at` is ISO 8601 UTC with fixed millisecond precision and `Z`
  (`2026-10-23T10:00:00.123Z`), strictly increasing per store, so string order = time order.
  The VPS sends updates with `updated_at` > cursor, oldest first, at most 50 (no cursor: the
  50 most recent). Statuses `received|in_progress|fixed|closed`; `reply` <= 500 chars or null.
  `report_updates` is always present (an empty list when nothing changed).
- **Signed replies.** Replies are shown to store staff, so they are authenticated like the
  policy: when `report_updates` is not empty, the signed check-in `ack` payload carries
  `"report_updates_sha256": "<hex sha256 of canonical JSON of the exact report_updates list in
  this response>"` (canonical JSON as for signing: `json.dumps(list, sort_keys=True,
  separators=(",", ":"))`, `ensure_ascii` left at its default `True`, UTF-8 bytes). The key is
  omitted when the list is empty, and there is no ack at all without a nonce (older boxes). The
  box applies `report_updates` only when the hash matches a verified ack.
- **Offline page host mapping** uses cctv-auth's registry hostnames (the list frps routes by,
  exact match); the control record supplies only the display name. "today" = within the last
  24 hours. `manage issues <store> clear` deletes resolved rows, `--all` every row (and resets
  the dropped counters), `--fingerprint FP` one row; the last clear time is kept per store
  (`issue_stats.cleared_at`).

## VPS storage and admin UI
- Tables: `reports` (store_id, box report id unique, fields above, status, reply, timestamps),
  `issues` (store_id + fingerprint unique, counts summed, first/last seen, version last seen,
  status `open|resolved`, `cleared_at`). Caps: 500 open issues and 1000 reports per store; text
  fields truncated server-side to the limits above; over the cap new fingerprints are dropped and
  counted (`dropped_total` shown in the UI). Nothing is deleted automatically.
- Admin UI, per box: **Issues** tab with two lists, "Reports" (newest first, urgent pinned;
  set status, write reply) and "Errors" (sort by last seen / count; filter open/resolved;
  Resolve, Clear (delete row), Clear all for this box). A resolved fingerprint that occurs again
  reopens it. Fleet overview shows per box: open reports, urgent reports, open errors. Audit log
  entries for status changes and clears. `manage issues <store> list|clear [--all]`.
- Offline page settings per store (display name already exists) and the global support contact.

## Verification
VPS: unit tests for every endpoint, caps, auth, host mapping, gateway render (`--dry-run`
diff), and a local frps v0.71.0 test of the offline detection. BOX: unit tests for redaction,
fingerprinting, caps, queue/retry, report dialog API; `node --check`. Do not start long-lived
servers or Chrome beyond what a stream's own tests need (16 GB RAM machine; cap anything you
run with `systemd-run --user --scope -p MemoryMax=3G -p MemorySwapMax=0`); the integrator runs
one combined end-to-end check afterwards.
