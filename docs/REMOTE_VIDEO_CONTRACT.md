# Remote live video: direct peer-to-peer contract (2026-09-29)

Shared contract for the backend, the dashboard and the VPS package. Owner rule
(2026-09-29): the VPS is only a connection broker and a debugging aid. Camera
video never passes through the VPS. Video is streamed only while someone is
watching, and it stops when they leave.

```
viewer ──https──> Cloudflare ──> VPS gateway ──> frps ──> box frpc ──> uvicorn   pages, JSON, SDP offer/answer
viewer <═══════════ WebRTC media, direct (host / srflx / prflx) ═══════════> box go2rtc
viewer ──UDP──> VPS STUN (coturn --stun-only, no TURN) <── box                   address discovery only
```

## Hard rules

1. **No relay, ever.** No TURN server anywhere. ICE server lists contain only `stun:` URLs.
   The backend refuses to emit a `turn:`/`turns:` URL, and the VPS STUN runs coturn with
   `--stun-only`, which disables allocation. A selected candidate pair of type `relay` is
   treated as a failure.
2. **No live pixels through the tunnel.** For a request that arrived through the tunnel
   (`public_exposure.is_remote_request()`), all live-picture/video endpoints answer
   `403 {"code": "video_direct_only", "detail": "..."}`. These are: `GET /stream`,
   `GET /api/v1/cameras/{id}/snapshot`, `POST /api/v1/cameras/{id}/actions/snapshot`,
   `POST /api/v1/cameras/test-connection` (refuse outright), the DVR HLS
   `GET /api/v1/dvr/cameras/{id}/hls/...`, and recorded DVR video
   `GET /api/v1/dvr/segments/{id}/video` and `GET /api/v1/dvr/archives/{id}/download`
   (the device is evidence-only anyway). The websocket and JSON endpoints keep working.
   Stored evidence stills and short clips (theft evidence JPEG, event snapshots/clips,
   night-watch evidence, `/api/zones/alerts/{id}/snapshot`) stay downloadable on an
   explicit click. They are small, on-demand and not a feed.
   The gateway also returns 403 for `/stream` and `*/snapshot` at the edge, as defence in depth.
3. **On demand only.** The box opens an NVR stream for remote viewing only when a session
   exists, and closes it when the last session on that camera ends. Nothing streams or polls
   to the internet in the background.
4. **Sessions end reliably.** A session ends on any of:
   - explicit close by the browser (tile rotated out, focus closed, view switched, tab
     hidden, `pagehide`);
   - a missed heartbeat (no heartbeat within `idle_timeout_s`, default 45 s);
   - go2rtc reporting the consumer gone;
   - an absolute cap (`max_session_s`, default 4 h; the page reopens it if still visible);
   - the camera being turned off or deleted.

   When a session ends and no other session uses that camera, the go2rtc stream is
   deleted, so the RTSP pull stops within a few seconds.
5. **Honest failure.** If ICE fails, the tile shows "Direct video not possible from this
   network" plus a short reason and a link to the connection check. It never falls back
   to snapshots through the tunnel.
6. **Hardware- and network-adaptive.** Codec passthrough when the browser supports the
   stream's codec, otherwise an ffmpeg H.264 transcode, VA-API/NVENC/QSV when available,
   else CPU. Direct connectivity works without any router change (STUN hole punching
   through ephemeral UDP ports). If the operator forwards a UDP port on the store router,
   a fixed-port mode advertises it.

## Box: go2rtc

- The binary is pinned by version and sha256 in `scripts/bootstrap.py` like frpc and
  installed to `/opt/edge-cctv/bin/go2rtc` (MIT licence; add to THIRD_PARTY_NOTICES.md).
  The installer fetches it by default.
- It runs as a supervised child process of the app, like frpc: restarted on crash, stopped
  on shutdown. Its config lives at `STORAGE_DIR/go2rtc/go2rtc.yaml` (0600). The API
  listens on `127.0.0.1:<port>` only (default 1984, never 0.0.0.0). go2rtc's own RTSP,
  RTMP, HLS, SRTP and web UI are all disabled. The config contains **no streams and no
  credentials**. Streams are added at session start through the go2rtc API with
  credentials held in memory, and deleted at session end.
- The stream source is the camera's resolved RTSP URL, preferring the sub-stream
  (`subtype=1`, D1 704x576 at about 768 kbps). This is the same resolver as analysis:
  `stream_selection` + `camera_source.stream_source_for`.
- WebRTC modes:
  - `auto` (default): ephemeral UDP ports, STUN-gathered srflx candidates, no router change.
  - `fixed_port`: listen on `:<webrtc_port>` (default 8555, UDP+TCP) and advertise
    `stun:<port>` for a store router with a port forward.

## Backend API (all under dashboard auth; viewer role may watch)

`GET /api/v1/webrtc/config` returns:
```json
{"enabled": true, "available": true, "reason": null,
 "remote": true,                      // this request came through the tunnel
 "transport": "webrtc",               // what the page must use for live video: "webrtc" | "local"
 "ice_servers": [{"urls": ["stun:stun.ikorex.com.au:3478"]}],
 "ice_transport_policy": "all", "relay": false,
 "heartbeat_s": 15, "idle_timeout_s": 45, "max_session_s": 14400,
 "max_sessions": 8, "active_sessions": 2, "mode": "auto"}
```
`transport` is `webrtc` for remote requests. For LAN/Tailscale requests it is `local`
(existing snapshots and MJPEG), unless the operator sets "Live video on this network:
direct WebRTC" (a per-browser dashboard toggle sends the header `X-Live-Transport: webrtc`,
which is honoured only for authenticated users and only switches LAN to WebRTC, never
remote to local).

`POST /api/v1/webrtc/sessions` with `{"camera_id": "...", "sdp": "<offer, ICE gathering complete>", "purpose": "tile|focus|frame"}`
- 201: `{"session_id", "sdp": "<answer>", "heartbeat_s", "expires_at", "codec": "H264|H265", "transcoded": false}`
- 404 unknown camera · 409 `{"code":"camera_off"}` · 429 `{"code":"video_session_limit","max_sessions":8}`
- 503 `{"code":"webrtc_unavailable","reason":"go2rtc not running|no stream source|..."}`
- 502 `{"code":"negotiation_failed","reason":"..."}`

`POST /api/v1/webrtc/sessions/{id}/heartbeat` returns 200 `{"expires_at"}`, or 404 if the session is gone (the page must close the connection).

`DELETE /api/v1/webrtc/sessions/{id}` returns 204 (idempotent). The page sends it with
`fetch(..., {method: "DELETE", keepalive: true})` on close and `pagehide`.

`POST /api/v1/webrtc/sessions/{id}/report` with
`{"state":"connected|failed|closed","pair":{"local":"host|srflx|prflx|relay","remote":"...","protocol":"udp|tcp"},"bytes_received":0,"frames_decoded":0,"error":"..."}`.
This is stored in the diagnostics ring buffer (last 50). A `relay` pair is logged as a violation.

`GET /api/v1/webrtc/sessions` (admin) returns the active sessions: `{camera_id, purpose,
viewer_ip, remote, started_at, last_heartbeat, pair, bytes_sent}`.

`GET /api/v1/webrtc/diagnostics` (admin) returns: go2rtc (running, version, api reachable),
mode and port, the box's public address as seen by each STUN server, NAT mapping behaviour
(`endpoint_independent|endpoint_dependent|unknown`, from two STUN servers queried off the
same local port), the last 50 session outcomes, and plain-language `advice` strings.

Settings: Online access stores `stun_servers` (list, default `["stun:stun.ikorex.com.au:3478"]`),
`webrtc_mode` (`auto|fixed_port`), `webrtc_port` (8555), `max_video_sessions` (8), and
`live_transport_on_lan` (`local|webrtc`). All are editable in the dashboard, not preset per site.

## Dashboard

- `static/js/webrtc_live.js` exposes:
  - `WebRtcLive.config()`, cached;
  - `WebRtcLive.open(cameraId, videoEl, {purpose})`, which returns a handle with
    `close()` and a state event;
  - `WebRtcLive.grabFrame(cameraId, {maxWidth})`, which returns a Blob by opening a
    session, drawing the first decoded frame and closing;
  - `WebRtcLive.closeAll(reason)`.
- Heartbeats run only while the page is visible.
- Camera grid, remote mode: each visible tile is a `<video muted playsinline>` backed by
  one session. Rotation closes the outgoing session before opening the incoming one.
  Focusing a tile closes the other tiles' sessions and reopens them on unfocus.
  Hidden tab, view switch or `pagehide` runs `closeAll`.
- Other views that show camera pixels (floor-plan thumbnail, heatmap background,
  calibration, zone studio, self-test) use `grabFrame` once when opened in remote mode
  (refresh on click, no timers). Studio and calibration live views use `open()`.
- Failure states are honest and specific. The tile's LIVE badge shows "Direct" plus the
  pair type.
- Connection check: Settings → Online access → "Check remote video". It shows
  browser-side STUN results (srflx found or not), the box diagnostics, and advice.

## VPS (`~/Projects-1/Server/cctv-tunnel`)

- New service `cctv-stun`: coturn pinned by digest, `--stun-only --no-cli --no-tls
  --no-dtls --listening-port=3478 --log-file=stdout`, read-only, `cap_drop ALL`,
  `mem_limit 64m`, publishes `3478/udp` (+`3478/tcp`). DNS: `stun.ikorex.com.au` as a
  **DNS-only** A record (grey cloud; Cloudflare does not proxy UDP).
- cctv-auth records the last Login/Ping per store (`last_seen`, `last_ip`, `frpc_version`,
  `last_refusal`). `manage status` shows it, and a `GET /status` JSON (internal network
  only) serves diagnostics.
- The gateway returns 403 for live-video paths (`/stream`, `~ /snapshot$`) on
  `*-cctv.ikorex.com.au`.

## Deviations (frontend)

Clarifications the dashboard relies on, where the contract left a choice open:

- **Settings storage:** Settings → Online access · Live video (`static/js/remote_video.js`, card
  `#settings-remote-video`) reads `stun_servers`, `webrtc_mode`, `webrtc_port`, `max_video_sessions`
  and `live_transport_on_lan` from `GET /api/v1/remote-access` and saves them with a partial
  `PUT /api/v1/remote-access` (only those keys). If GET has none of them, the form is shown disabled
  with "does not offer live video settings yet".
- **Diagnostics shape read** (as the backend returns it; raw JSON is also shown): `go2rtc{running, version,
  api_reachable}`, `mode`, `port`, `nat{servers[{server, mapped, error}]}`, `public_address`, `nat_mapping`,
  `relay_violations`, `max_sessions`/`active_sessions`, `sessions[]` (outcomes: `at, camera_id, state|report,
  end_reason, error, pair, viewer_ip, remote`), `advice[]`. `GET /api/v1/webrtc/sessions` is read as
  `{"sessions": [...]}` (a bare list also works). 403 is shown as "administrator only".
- **Focus:** an enlarged tile keeps the session it already has (purpose stays `tile`); a `focus`
  session is opened only if it had none. The zone studio opens `focus`; stills use `frame`.
- **Config:** a 404 from `/api/v1/webrtc/config` (older box) means `transport: local`. `relay: true` or
  `ice_transport_policy: relay` makes direct video unavailable. `X-Live-Transport: webrtc` is sent only
  on the config request.
- **Close:** `report` (`closed`/`failed`) then `DELETE`, both sent at once with `keepalive` (so a rotated-out
  camera is deleted before the incoming one's offer). The report carries the pair and byte/frame counts
  sampled every 5 s while connected (the same sampling closes a connection whose pair turns `relay`).
- **Tiles** open a session only for cameras whose pipeline status is `ONLINE` (otherwise the badge shows
  the status). Retries after failure: 15 s for `video_session_limit`, 60 s for `camera_off`/unknown
  camera/unavailable, 15 s doubling to 120 s for ICE failures, else 30 s; "Try again" retries at once.
- **403 `video_direct_only`** from snapshot, `actions/snapshot` or `test-connection` is shown as a plain
  message and makes the page re-read the config; nothing falls back.
- **Detector boxes** are not drawn on direct video (raw camera video). Follow-up: draw them client-side
  from `/api/v1/layout/live` boxes; they are not frame-synchronised with the video.

## Deviations (VPS)

Status 2026-09-29: prepared in `~/Projects-1/Server/cctv-tunnel` and tested locally. **Not deployed.**

- **coturn flags.**
  - Image: `coturn/coturn:4.18.0-alpine`, pinned by its linux/amd64 digest
    `sha256:1db5d53b12e65098879946913813c075f0cf0f8f3ae9daebcbcd70ce518aaf7d`.
  - `--no-dtls` is **not** passed: coturn 4.18 rejects it as an unrecognized option and exits.
  - `--no-cli` is **not** passed either: 4.18 logs it as a deprecated ERROR.
  - DTLS and the CLI are off by default in 4.18 (enabled only by `--dtls` and `--cli`), and `--no-tls` stays.
  - Added: `-n` (no config file) and `--denied-peer-ip` over all of IPv4 and IPv6, as a second lock should
    `--stun-only` ever be dropped. Also `--relay-threads=1`, and `--pidfile` on a tmpfs.
  - `--no-udp-relay` together with `--no-tcp-relay` is refused by coturn, so it is not used.
- **Capabilities.** `cap_drop: ALL` **plus `cap_add: NET_BIND_SERVICE`**. The image's `turnserver` binary
  carries that file capability, and with an empty capability set the kernel refuses to exec it
  (`operation not permitted`, tested). PIDs are limited to 32.
- **Gateway 403 list.** The gateway blocks exact paths rather than a generic `~ /snapshot$`, because that
  would also catch stored evidence (`/api/zones/alerts/{id}/snapshot`). It blocks `= /stream`,
  `= /api/v1/cameras/test-connection`, `~ ^/api/v1/cameras/[^/]+/(actions/)?snapshot$` and
  `~ ^/api/v1/dvr/cameras/.+/hls/`. The body is
  `{"code":"video_direct_only","detail":"Live video is direct only; it never passes through the tunnel."}`
  (`application/json`).
- **`last_ip`.** Boxes connect over wss, and over WebSocket frps 0.71 passes the WebSocket Origin
  (e.g. `http://tunnel.ikorex.com.au:443`) as the plugin's `client_address`, not a peer address. This
  was verified with real frps/frpc. So `last_ip` is recorded only when it is a real IP, which in practice
  means it stays empty. A box's public IP is in the gateway log (CF-Connecting-IP on
  `GET /~!frp`). `last_refusal` is stored as `"<Op>: <reason>"`, and for a registered store a bad token
  reads `wrong token`.
- **Not blocked, for the owner to decide.** `GET /api/v1/dvr/segments/{id}/video` (recorded DVR
  segments) is not in the contract's list and still passes the tunnel. Blocking the HLS playlist
  prevents playback, but a direct segment URL would still download.

## Deviations (backend)

Status 2026-09-29: implemented in `edge_backend` (not deployed). go2rtc **v1.9.14**, pinned in
`edge_backend/scripts/bootstrap.py` by the release asset digests (`go2rtc_linux_amd64`
`32d616af226bd731678ffde328b94cfb94e30339bfefc469cfb76323144615a6`, `go2rtc_linux_arm64`
`359fabade8a7a51e81a55fe6df6b0ef81764a5e1d63179577534eaaa71904b50`, `go2rtc_linux_arm`
`4d7e1639af5a2722a28e864468fd8099b3c1682565446c798bf9e3b38fde12e4`). Linux assets are plain
executables. They are fetched by default; skip with `--no-go2rtc` or `EDGE_GO2RTC=0`.

- **ICE in `auto` mode (checked in the go2rtc 1.9.14 and pion ice v4.2.0 source, and end to end).**
  - `webrtc.listen: ""` creates no UDP/TCP mux. Each PeerConnection gets its own ephemeral UDP
    sockets per interface.
  - pion gathers **one srflx candidate per `stun:` server on a new ephemeral socket**, in both
    modes: go2rtc never sets `UDPMuxSrflx`. So srflx does not depend on `listen: ""`. What
    `listen: ""` changes is that it avoids advertising a fixed host port.
  - go2rtc does **not** use ICE-lite. As the answerer it is the controlled full agent and sends
    its own binding requests to every remote candidate from the offer. Those outbound checks
    open the store router's mapping and the box's UFW conntrack entry, so it hole-punches
    without a port forward behind an endpoint-independent NAT.
  - `POST /api/webrtc` returns the complete answer after gathering (non-trickle; the wait is
    bounded by pion's 5 s STUN timeout). The browser must likewise send its offer with
    gathering complete, because the HTTP API has no trickle.
  - `fixed_port` is `listen: ":<port>"` plus `candidates: ["stun:<port>"]`. go2rtc then
    advertises `<address from its STUN query>:<port>` as a host candidate. That address can be
    IPv6 on a dual-stack box (seen on the dev machine), and the port is assumed, not mapped.
    Use `fixed_port` only with a real port forward and a firewall rule
    (`sudo ufw allow <port>/udp`). The diagnostics advice says so.
- **One go2rtc stream per session** (`ws_<session id>`), not one per camera.
  - go2rtc cannot close a single consumer, and its `DELETE /api/streams` only unlinks the name.
    A per-session stream makes "consumer gone" identify the session exactly, and the RTSP pull
    stops synchronously when that session's consumer leaves.
  - Cost: two viewers of one camera pull it twice. This is bounded by `max_video_sessions`.
- **Server-side end (missed heartbeat, cap, camera off/deleted/source changed).**
  - The session leaves the registry at once, so heartbeat answers **404** and the page must
    close. The stream is deleted as soon as its consumer is gone.
  - If a consumer is still attached `heartbeat_s + 10 s` after its session ended, go2rtc is
    **restarted** to cut it. That also ends every other session, with reason `gateway_restart`,
    and their pages reopen.
  - E2E: a viewer that ignored the 404 was cut 15 s after its session ended.
- **go2rtc RTSP server stays on**, bound to `127.0.0.1:<GO2RTC_API_PORT+16570>` (18554 by
  default). Its `ffmpeg:` transcode source publishes back to go2rtc over RTSP and fails with the
  RTSP module off.
  - RTMP, SRTP, HLS, MJPEG, MP4, HomeKit and the rest are not loaded. Loaded modules:
    `api, http, rtsp, webrtc, exec, ffmpeg`.
  - The API exposes only `/api`, `/api/streams` and `/api/webrtc` (`allow_paths`). It requires
    Basic auth even from loopback (`local_auth`), with a random per-start password passed in
    go2rtc's environment via `${EDGE_GO2RTC_API_PASSWORD}`.
- **Credentials.** Streams are added with `PATCH /api/streams`, which is in memory only; go2rtc's
  `PUT` would write the source, credentials included, into the config file.
  - The config file (`STORAGE_DIR/go2rtc/go2rtc.yaml`, JSON syntax, 0600) never contains
    streams or passwords.
  - When transcoding, go2rtc passes the camera URL (with its login) to its ffmpeg child's
    command line. Under the unit's `ProtectProc=invisible` only the service user can see it.
- **Codec.** Passthrough is tried first. On go2rtc's `codecs not matched` the stream is re-pointed
  to `ffmpeg:<url>#video=h264#hardware`, where go2rtc probes NVENC, then VA-API, else libx264,
  and the offer is negotiated again.
  - The camera's codec is cached per source, so later sessions skip the failed try.
  - `WEBRTC_TRANSCODE_ENCODER=auto|vaapi|nvenc|qsv|libx264` forces an engine. qsv maps to VA-API
    on Linux, since go2rtc has no QSV engine there.
  - E2E: an H.265 camera to headless Chrome 153 (which offers no H.265) was transcoded by
    `h264_nvenc`. First answer in 3.8 s, frames decoded.
- **Response shapes.**
  - Session errors are `{"code", "reason", "detail"}`. An unknown camera is 404
    `{"code": "unknown_camera"}`. Heartbeat 404 is `{"code": "session_gone"}`.
  - `GET /sessions` returns `{"sessions": [...], "active_sessions", "max_sessions"}`. Each session
    also carries `session_id`, `codec`, `transcoded`, `encoder`, `connected_to` and
    `heartbeat_required`.
  - Diagnostics: `?nat=false` skips the STUN check. `nat.servers[]` carries
    `{server, server_ip, mapped, error, configured, rtt_ms}`. When only one STUN server is
    configured, one public server is added for the diagnostic only (Google first, else
    Cloudflare; `configured: false`).
  - Config: `enabled` means go2rtc is installed; `available` means installed and not in an
    error state.
- **Roles.** Everyone with dashboard access may watch. A stream token may open only its own
    camera, and clip tokens are refused. "Admin" (sessions list, diagnostics) is a dashboard
    `user_session` whose role is not `viewer` and that is not a phone token (`pd`). The internal
    API key also passes.
- **Tunnel refusal list** follows rule 2 as amended: it also covers
    `/api/v1/dvr/segments/{id}/video` and `/api/v1/dvr/archives/{id}/download`. Any method is
    refused, including HEAD.
- **Phone app compatibility.**
  - `GET /ice-servers` returns STUN only: `turn_enabled: false`, `webrtc_scope: "internet"`,
    `remote_fallback: null`.
  - `POST /offer` opens a session **without heartbeats**. It ends when the connection closes,
    the camera is turned off or deleted, or after `max_session_s`. It also returns `session_id`.
  - The current app still picks MJPEG when it talks to the remote URL, and that is now 403
    `video_direct_only`. **The phone app needs an update** (session API with heartbeat and
    DELETE) before remote live video works there.
- **Compose and health (cleaned up in the integration run):** `edge_backend/docker-compose.yml` no longer
  has the coturn `turn` profile or a separate go2rtc container (go2rtc runs inside `edge_api`, started by the
  first live-video session). `routes/health.py` reports go2rtc as NOT_CHECKED ("starts on the first live-video
  session") until a session starts it, then HEALTHY, FAILED or NOT_PRESENT with the reason, and NOT_CHECKED
  again once it is stopped.

## Integration run (2026-09-29)

Real dashboard in headless Chrome through a real frps/frpc pair (`Host: <store>-cctv...`), seven RTSP test
cameras (one H.265), auth on. Fixed on the way:

- **Rate limits.** The webrtc router shared the dashboard's general limit (100 requests/min per address). One
  viewer's rotating grid alone exceeded it (30+ refusals a minute), so DELETEs were refused and opens were
  shown as "too many people watching". Now heartbeat, report and DELETE are not limited, opening a session
  has its own budget (240/min per address), and the page shows a bare 429 as "slow down", not as the video
  limit.
- **A tile closed while its offer was being answered** (rotation, focus or a view switch during
  negotiation) left a go2rtc consumer that never connected. It pulled the camera for about 30 s, and 25 s
  after the close go2rtc was restarted, cutting every other viewer. The page now finishes the handshake and
  closes cleanly (the camera is released in under 1 s), and the box restarts go2rtc only for a consumer that
  is connected (has a peer address). A consumer that never connected is waited for, up to 90 s.
- **go2rtc's ffmpeg transcoder outlived go2rtc** after SIGTERM (NVDEC `-hwaccel cuda`). It kept pulling the
  camera, held the GPU context and made `systemctl stop` wait 90 s. The manager now ends what is left of
  go2rtc's process group (SIGTERM, 2 s, SIGKILL) whenever go2rtc exits.
- **Tunnel 404.** frps answers 404 (HTML) while the box is unreachable. The page now shows "The box did not
  answer" and retries in 15 s instead of "Camera not found" (60 s). It also no longer takes that as an older
  box without direct video.
- Diagnostics rows for a closing report that arrived after its session ended now carry the camera. Chrome's
  STUN error 701 "host lookup" is explained as a DNS name problem, not as a network block.
