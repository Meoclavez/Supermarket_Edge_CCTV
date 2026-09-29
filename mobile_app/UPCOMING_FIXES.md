# Phone app: fixes to make before the next build

Status on 2026-09-29: **not started**. The owner builds the app only when they ask
for it. Until then, run only `flutter analyze` and `flutter test`, and never run
`flutter build` or Gradle. Current app version: `1.2.0+1` (`pubspec.yaml`).

## Why these fixes are needed

The edge box now sends live video to viewers **only over a direct peer-to-peer
WebRTC connection**. The VPS is just a connection broker and a debugging aid, and
camera video never passes through it (owner rule, 2026-09-29). The server contract
is in `docs/REMOTE_VIDEO_CONTRACT.md`, and the dashboard already works this way in
`edge_backend/app/static/js/webrtc_live.js`, which is a good reference.

The current app breaks in two ways:

| Where | What happens now | Why |
|---|---|---|
| Away from the store (public address, e.g. `https://pearcedale-cctv.ikorex.com.au`) | **No live video.** | `initialLiveTransport()` picks MJPEG for the remote URL (`lib/services/live_stream_transport.dart:25`), and `GET /stream` through the tunnel now answers `403 {"code":"video_direct_only"}`. |
| On store Wi-Fi or Tailscale | Works, through the old `/api/v1/webrtc/offer` compatibility route. | That route opens a session with no heartbeat. It ends only when the connection closes, the camera is turned off, or after 4 h. |

## Fixes

### 1. Live video through the session API (required)
Replace the one-shot `/offer` call in `lib/services/webrtc_service.dart` with the
session lifecycle the dashboard uses:

1. `GET /api/v1/webrtc/config`, **with the auth header**. Use `ice_servers`,
   `heartbeat_s`, `transport`, `max_sessions`, `available` and `reason`. Accept
   only `stun:` URLs, and never add TURN.
2. Create a recvonly video transceiver, create the offer, and **wait for ICE
   gathering to complete** (cap about 2.5 s). The server has no trickle ICE.
3. `POST /api/v1/webrtc/sessions` `{camera_id, sdp, purpose: "focus"}` with the
   auth header. This returns 201 `{session_id, sdp (answer), heartbeat_s, expires_at, codec, transcoded}`.
4. Every `heartbeat_s` while the screen is visible:
   `POST /api/v1/webrtc/sessions/{id}/heartbeat`. **On 404, close the peer connection
   immediately.** If a closed session stays connected, the box restarts its video
   server about 14 s later, and every other viewer's video drops with it.
5. On close (leaving the screen, switching camera, app paused or backgrounded, or
   logout): `DELETE /api/v1/webrtc/sessions/{id}`, then close the peer connection.
   Hook `AppLifecycleState.paused` / `hidden` so the video stops when the app goes
   to the background.
6. After connected, failed or closed, `POST .../report` with the selected
   candidate pair types from `getStats()`, plus bytes and frames. A `relay` pair
   counts as a failure: close the connection and show the error.

Handle errors honestly:
- 404 `unknown_camera`;
- 409 `camera_off`;
- 429 `video_session_limit` ("Too many live videos open at once");
- 503 `webrtc_unavailable` (show `reason`);
- 502 `negotiation_failed`.

### 2. Remove the MJPEG fallback for remote access (required)
- Delete the remote branch that falls back to MJPEG (`live_stream_transport.dart:22-31`,
  `live_view_screen.dart:123-174`) and the "HTTPS stream" / "LIVE (MJPEG / HTTPS)" labels.
- When a direct connection fails, show "Direct video not possible from this network"
  with the reason, and a "Try again" button. Never use a picture or stream through
  the tunnel instead.
- MJPEG may stay as an explicit LAN-only option if wanted. Otherwise remove
  `mjpeg_view.dart` and its tests.

### 3. Send the auth header on every WebRTC call (required)
`_fetchDynamicIceServers()` (`webrtc_service.dart:49-63`) and the offer POST
(`webrtc_service.dart:112-120`) send no `Authorization` header. The server
requires one, so use `ApiService().authHeaders()` as `live_view_screen.dart:154` does.

### 4. Remove stale defaults (required)
- `ApiConstants.defaultGo2rtcUrl` (`http://192.168.1.100:1984`): go2rtc's API now
  listens only on the box's loopback and needs a password, so the phone must never
  call it.
- `ApiConstants.rtcIceServers`: the hardcoded Google STUN fallback. Use the
  server's `ice_servers`, and if `/config` fails, show the error instead of
  guessing.
- `ApiConstants.defaultBaseUrl` (`http://192.168.1.100:8000`) is a placeholder from
  another project. Pairing should set the address, with no preset site address.
- `webrtcTokenEndpoint` if nothing uses it after fix 1.

### 5. Talk-back removed (2026-09-29)
Owner decision: talk-back was a home-security leftover outside the supermarket
scope, so live viewing is receive-only video. Removed: `lib/widgets/talkback_button.dart`
and its control-bar slot in `live_view_screen.dart`; the microphone backchannel in
`webrtc_service.dart` (`enableBackchannel`, `getUserMedia`, local audio track,
`setTalkbackActive`); the no-op volume toggle in the live view app bar;
`RECORD_AUDIO` and `MODIFY_AUDIO_SETTINGS` from `AndroidManifest.xml`; and
`NSMicrophoneUsageDescription` from `Info.plist`. `rtcMediaConstraints` now sets
`OfferToReceiveAudio: false`, so the offer carries no audio in either direction.

### 6. Detector boxes on live video (optional)
Direct video is the raw camera picture. The dashboard has the same gap. Boxes could
be drawn from `/api/v1/layout/live`, labelled as approximate because they are not
frame-synchronised.

### 7. Push alerts (pending from before)
Phone push (FCM) is still pending: the server side exists (`alert_dispatcher.py`,
Settings → phone push), but no service account is configured and the app side has
not been tested end to end.

## Tests to add or update
- `test/live_stream_fallback_test.dart`: remote must choose WebRTC and never MJPEG.
  `/config` failure → honest error.
- Session lifecycle with a fake HTTP client: create → heartbeat → 404 closes, DELETE
  on dispose, pause closes, relay pair rejected, 429 shown.
- `flutter analyze` and `flutter test` clean.

## Checks before release
- On a phone on mobile data (not store Wi-Fi, Tailscale off): video plays, and
  `GET /api/v1/webrtc/sessions` shows a `srflx`/`prflx` pair, never `relay`.
- Leave the screen or background the app: the session disappears from the list
  within a few seconds.
- Force-close the app: the server reaps the session within about 45 s.
- Bump the version in `pubspec.yaml`.
