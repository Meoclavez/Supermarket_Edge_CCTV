# 🛡️ Edge AI CCTV Surveillance & Safety Platform

An enterprise-grade, **100% on-premises edge-processed CCTV AI monitoring and safety ecosystem**. Designed for **Intel N100 Mini PCs** paired with **Hailo-8 / 8L M.2 PCIe AI modules**, executing hardware-accelerated video decoding via **Intel QuickSync (VA-API)** and sub-10ms neural inference on the **HailoRT dataflow engine**.

Connects directly to a **cross-platform Flutter client (PC Web/Desktop, Android, and iOS)** featuring **loss-prevention alerts for store staff**, **24/7 Segmented DVR Recording with 24-Hour Timeline Scrubbing**, **Interactive Visual Zone & Privacy Mask Editor**, **2-Way Audio Push-to-Talk**, and **Ultra-Low Latency (<300ms) WebRTC Streaming**.

---

## 🌟 Key Features

* **⚡ Sub-10ms Edge AI Vision (Hailo-8 M.2)**:
  * Hardware accelerated **YOLOv8n** object detection ($<4\text{ms}$) + **YOLO-Pose** 17-keypoint pose estimation ($<8\text{ms}$).
  * **Theft / loss prevention**: pose-keypoint behaviour cues (concealment, shelf sweeps, exit without checkout) raised as *suspicious behaviour for staff review* with snapshot and clip evidence, never as an accusation.
  * **Market analysis**: footfall, zone dwell, conversion funnels and queue lengths aggregated from real tracked visits.
  * **Customer heatmaps & shelf interactions**: floor-plan heatmaps from calibrated cameras and hand-to-shelf interaction events per product zone.
* **🔒 Source-Level Hardware Privacy Masking**:
  * In-place masking (`BLACKOUT`, `BLUR`, `MOSAIC`, `COLOR`) applied directly to source frames before ring-buffering, snapshot generation, or WebRTC streaming.
* **📼 24/7 Segmented NVR & 24-Hour Timeline**:
  * Continuous zero-copy H.264 stream remuxing (`-c:v copy`) consumes **$<0.5\%$ CPU per 1080p stream** on the Intel N100.
  * Fragmented keyframe moov headers prevent video file corruption during unexpected power outages.
  * Dynamic HLS (`.m3u8`) generator with gap discontinuity tags and sub-2-second lossless incident MP4 export (`/api/v1/cameras/{id}/export`).
* **🌐 Zero-Trust WebRTC & NAT Traversal**:
  * **go2rtc** media gateway ($<300\text{ms}$ latency) + **Coturn** RFC 5766 dynamic HMAC-SHA1 authenticated STUN/TURN relay for symmetric 4G/5G mobile connectivity.
  * **Push-to-Talk 2-Way Audio Backchannel**: Encodes Flutter microphone audio to Opus 48kHz and routes to camera speakers.
  * Automated 2048-bit SAN TLS certificates.
* **🔔 Loss-prevention alerts for staff**:
  * High-priority, time-sensitive pushes (APNs `time-sensitive`, FCM `priority: high`) with the normal alert sound; no critical-alert entitlement and no siren.
  * Live dashboard feed over `/api/v1/events/ws`, an acknowledgeable alert log, and per-camera alert muting (e.g. during restocking).
* **📱 Adaptive Cross-Platform Flutter Client**:
  * Responsive 3-tier layout: Mobile bottom nav ($<600\text{px}$), Tablet rail ($600-1100\text{px}$), and Desktop expanded sidebar ($>1100\text{px}$).
  * **Visual Zone Canvas**: Click and drag privacy-mask and product-shelf polygons directly on live camera snapshots.
  * **SMART Storage Health Dashboard**: NVMe wear levels, drive temperature, reallocated sectors, and per-camera quota progress bars.
  * **Biometric Gate**: Hardware Face ID / Fingerprint verification for viewing sensitive feeds and incident evidence.

---

## 🏗️ System Architecture

```
 [ IP Cameras (RTSP H.264/H.265) ]
                │
                ▼
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │                  INTEL N100 + HAILO M.2 EDGE MINI PC                        │
 │                                                                             │
 │ 1. Intel QuickSync (VA-API /dev/dri/renderD128) decodes raw RTSP feeds.     │
 │ 2. In-Place Privacy Masking (Blackout / Blur / Mosaic).                     │
 │ 3. Pose inference (Hailo / TensorRT / CUDA / CPU, probed at runtime).       │
 │ 4. Zero-Copy 24/7 DVR remuxing saves 1-min MP4 chunks + Dynamic HLS.       │
 │ 5. go2rtc (<300ms WebRTC) + Coturn RFC 5766 dynamic HMAC-SHA1 TURN.        │
 │ 6. Caddy TLS Reverse Proxy.                                                 │
 │ 7. Loss-prevention alert fan-out: APNs / FCM pushes + dashboard websocket.  │
 └──────────────────────────────────────┬──────────────────────────────────────┘
                                        │ (HTTPS / WSS / WebRTC + Opus Mic)
                                        ▼
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │             CROSS-PLATFORM FLUTTER CLIENT (DESKTOP / WEB / MOBILE)         │
 │                                                                             │
 │  • Adaptive AppShell: Mobile bottom nav, Tablet rail, Desktop sidebar.      │
 │  • Live Multi-Cam Grid Wall: 1 to 16 adaptive camera tiles.                 │
 │  • 24/7 Continuous DVR Player: 60fps timeline with pinch zoom & snapping.   │
 │  • Visual Zone & Mask Canvas: Draw & drag masks/shelf areas on snapshots.   │
 │  • System & SMART Storage Dashboard: Disk gauges, wear level, camera quota. │
 │  • AI Incident Center: Severity-filtered alerts with instant clip playback. │
 │  • Biometric Gate: FaceID / Fingerprint lock with 60s grace period.         │
 │  • WebRTC 2-Way Audio Talkback: Native Opus microphone backchannel.         │
 └─────────────────────────────────────────────────────────────────────────────┘
```

---

## 🖥️ Store Dashboard & First-Run Setup

The web dashboard at `http://<server-ip>:8000/dashboard` starts **empty**: no
zones, rooms, cameras or metrics are pre-seeded, and nothing shown is
simulated. The operator builds the store up in the **Blueprint** tab: sign in →
Store size → draw rooms / walls / zones → Scan for cameras → Add → place the
camera → Calibrate (4+ floor points, image ↔ plan) → people appear on the plan.
`POST /api/v1/layout/reset` (`{"confirm":"RESET"}`) returns the install to that
empty state. The full step-by-step is in [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

---

## 📦 Project Structure

```
Supermarket_Edge_CCTV/
├── .agents/
│   ├── project_map.md                        # Complete project architecture & API index
│   ├── system_architecture_spec.md           # Exhaustive hardware & algorithm specification
│   └── api_and_integration_reference.md      # REST API, WebRTC signaling & Docker schemas
│
├── edge_backend/                             # Edge Mini PC Backend Services
│   ├── app/
│   │   ├── main.py                           # FastAPI app, SQLite lifecycle, 24/7 DVR
│   │   ├── config.py                         # Settings, retention policies, Coturn keys
│   │   ├── database.py                       # SQLAlchemy async SQLite session factory
│   │   ├── models/
│   │   │   ├── schemas.py                    # Pydantic schemas (Zones, DVR, Timeline, Storage)
│   │   │   └── db_models.py                  # SQLAlchemy ORM models
│   │   ├── routes/
│   │   │   ├── health.py                     # Hardware & telemetry monitoring (/api/v1/health)
│   │   │   ├── cameras.py                    # Camera CRUD, snapshots, device registration
│   │   │   ├── events.py                     # Loss-prevention alerts: log, ack, websocket, devices
│   │   │   ├── webrtc.py                     # WebRTC SDP signaling & dynamic ICE servers
│   │   │   ├── dvr.py                        # 24h timeline, dynamic HLS, incident exports
│   │   │   └── zones.py                      # Per-camera privacy masks
│   │   └── services/
│   │       ├── inference_backend.py          # Pose detector with runtime accelerator probing
│   │       ├── ai_zone_service.py            # Privacy masks & polygon geometry
│   │       ├── dvr_recorder.py               # 24/7 continuous segmenter, HLS, stitcher & SMART
│   │       ├── clip_recorder.py              # In-memory JPEG ring-buffer MP4 generator
│   │       ├── notification_service.py       # Loss-prevention pushes (APNs/FCM) + websocket hub
│   │       ├── turn_service.py               # RFC 5766 dynamic ephemeral TURN credentials
│   │       └── auth_service.py               # JWT session manager & path traversal sanitizer
│   ├── tests/
│   │   ├── test_api.py                       # REST API, auth, ICE, zones, timeline tests
│   │   └── test_features.py                  # Per-camera retail feature flags
│   ├── coturn/coturn.conf                    # Coturn TURN/STUN relay configuration
│   ├── Caddyfile                             # Caddy reverse proxy config (TLS termination)
│   ├── scripts/generate_certs.py             # Automated local TLS certificate generator
│   ├── go2rtc.yaml                           # go2rtc Media Gateway config
│   ├── requirements.txt                      # Backend dependencies
│   ├── Dockerfile                            # Multi-stage container with HailoRT & VA-API
│   └── docker-compose.yml                    # Stack: coturn, go2rtc, edge_api, caddy, tailscale
│
└── mobile_app/                               # Cross-Platform Flutter Client (Desktop, Web, Mobile)
    ├── pubspec.yaml                          # Dependencies (local_auth, webrtc, notifications)
    └── lib/
        ├── main.dart                         # Bootstrap with AppShell & background FCM isolate
        ├── core/theme/app_theme.dart         # Glassmorphism dark theme with reusable components
        ├── models/zone_model.dart            # Zone and polygon configuration models
        ├── services/
        │   ├── api_service.dart              # REST client with auto-failover & base URL switching
        │   ├── discovery_service.dart        # Universal mDNS discovery + subnet sweep
        │   ├── biometric_auth_service.dart   # FaceID / Fingerprint manager with 60s grace
        │   ├── webrtc_service.dart           # WebRTC manager with H.264/Opus SDP prioritization
        │   └── notification_service.dart     # Push handler with lockscreen interactive actions
        ├── widgets/
        │   ├── biometric_gate.dart           # Biometric authentication screen wrapper
        │   ├── talkback_button.dart          # Push-to-Talk 2-way audio button
        │   ├── timeline_scrubber_widget.dart # Gesture scrubber with pinch zoom 1h-24h
        │   └── zone_canvas_painter.dart      # Interactive canvas painter for polygon/tripwire drawing
        └── screens/
            ├── app_shell.dart                # Master adaptive responsive AppShell
            ├── multi_cam_grid_screen.dart    # Adaptive 1 to 16 camera live grid wall
            ├── live_view_screen.dart         # WebRTC live player with talkback & 24h timeline
            ├── dvr_playback_screen.dart      # 24/7 continuous DVR timeline player
            ├── events_center_screen.dart     # Filtered AI incident center
            ├── zone_editor_screen.dart       # Visual zone & privacy mask drawing canvas
            ├── clip_archives_screen.dart     # Incident video clip archives & export manager
            ├── storage_health_screen.dart    # System telemetry & SMART health gauges dashboard
            └── emergency_alert_screen.dart   # Fullscreen emergency takeover modal
```

---

## 🚀 Production Deployment Guide

### 0. Running the backend (`./run.sh`)

```bash
./run.sh                    # fix the environment if needed, verify inference, start on 0.0.0.0:8000
./run.sh --check-only       # same fixing and verification, but do not start the server
./run.sh --port 8766 -- --reload   # arguments after -- go to uvicorn
```

`run.sh` is a thin wrapper around `edge_backend/scripts/bootstrap.py`. Each step
checks first and only acts when something is wrong, so a second run on a healthy
machine changes nothing:

1. **venv**: `$EDGE_VENV` or `--venv`; otherwise the existing `.venv_test` if it
   exists, else `.venv`, which is created on first run. A venv whose interpreter
   no longer runs (for example after a system Python upgrade) is recreated.
2. **requirements**: installed with `uv` when available, else with pip.
3. **accelerator + onnxruntime flavour**: probes `nvidia-smi` and `/dev/nvidia*`,
   `/dev/kfd` (ROCm), `/dev/dri` and `/dev/hailo0`. On x86_64 the flavour is
   `onnxruntime-gpu[cuda,cudnn]`: the CUDA and cuDNN libraries come as pip
   wheels, so only the NVIDIA driver is needed, and the same package runs on the
   CPU when there is no GPU. `onnxruntime` and `onnxruntime-gpu` install the same
   Python package, so if both are present the plain one is removed and
   `onnxruntime-gpu` is reinstalled so that its files win. Use `--ort cpu` for the
   lean CPU-only build, or `--ort openvino` on Intel.
4. **models**: `edge_backend/models/*.onnx` are checked against
   `models/manifest.json` (sha256). A missing or damaged model is re-exported
   from its Ultralytics `.pt` in an isolated Python 3.12 venv under
   `~/.cache/edge-cctv/model-export` (override with `--model-cache-dir`), so
   ultralytics and torch never enter the app venv.
5. **verify**: creates a real ONNX Runtime session and reports
   `get_providers()[0]`. On an NVIDIA machine this must say
   `CUDAExecutionProvider`.
6. **preflight** (`python -m app.services.preflight`): read-only checks, then
   `exec uvicorn`.

The script never uses `sudo`. When the fix is at the system level (for example
a GPU with no driver loaded) it prints the exact command for you to run.

**Database updates** are automatic: every start applies pending schema migrations
(`edge_backend/app/migrations/`, backed up first) and adds new model columns, so a
feature update on a running device only needs a restart; see
`python -m app.migrations status|check|upgrade`.

The same checks run at every server start and are served at
`GET /api/v1/system/preflight` (`?probe=true` also runs a real session probe).
A summary is included in `GET /api/v1/system/hardware`.

**GPU notes**
- NVIDIA: the CUDA 13 wheels need driver >= 580. `nvidia-smi` shows the driver
  version. TensorRT is listed by onnxruntime but its libraries are not installed,
  so the backend falls through to CUDA.
- Docker on NVIDIA hosts: install `nvidia-container-toolkit`, then run
  `docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d`.
  CPU-only, Intel and Hailo hosts use plain `docker compose up -d`.
- systemd: `deploy/edge-cctv.service` runs the preflight as `ExecStartPre` and
  allows the NVIDIA, ROCm and Hailo device nodes.
- Model licence: the YOLO26 weights and exports are AGPL-3.0 (see `models/manifest.json`).

### 1. Edge Mini PC Backend Deployment (Intel N100 + Hailo M.2)

#### System Prerequisites:
```bash
# 1. Install Intel VA-API QuickSync drivers
sudo apt update && sudo apt install -y intel-media-va-driver-non-free vainfo
vainfo

# 2. Install HailoRT PCIe Driver (for Hailo-8 M.2)
sudo dpkg -i hailort-pcie-driver_*.deb
sudo dpkg -i hailort_*.deb
hailortcli scan
```

#### Launch with Docker Compose:
```bash
cd edge_backend

# 1. Generate local 2048-bit SAN certificates
python3 scripts/generate_certs.py

# 2. Launch microservices (CPU / Intel / Hailo host)
docker compose up -d --build
# ...or on an NVIDIA host with nvidia-container-toolkit:
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

#### Auto-Start on Boot (`systemd`):
Create `/etc/systemd/system/cctv-edge.service`:
```ini
[Unit]
Description=Edge AI CCTV Surveillance Core
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/edge-cctv/edge_backend
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload
sudo systemctl enable cctv-edge.service
```

---

### 2. Android App Deployment

```bash
cd mobile_app

# 1. Fetch Flutter packages
flutter pub get

# 2. Build direct-install standalone release APK
flutter build apk --release --split-per-abi

# Generated APK: build/app/outputs/flutter-apk/app-arm64-v8a-release.apk
```
* **Install**: Transfer `.apk` directly to Android devices via USB or local browser.

---

### 3. iOS App Deployment (TestFlight / Ad-Hoc)

1. Open in Xcode:
   ```bash
   cd mobile_app/ios
   open Runner.xcworkspace
   ```
2. Select your **Development Team** in **Signing & Capabilities**.
3. **Option A: TestFlight (Zero Review Wait)**:
   * Build target: **Any iOS Device (arm64)** $\rightarrow$ **Product > Archive** $\rightarrow$ **Distribute App > App Store Connect > Upload**.
   * Add internal testers in App Store Connect; install via the TestFlight app.
4. **Option B: Direct USB Sideload**:
   * Connect iPhone via USB $\rightarrow$ Select device in Xcode $\rightarrow$ Click **Run ▶**.

> [!NOTE]
> **iOS alert level**: loss-prevention pushes use the **`time-sensitive`** interruption level with the default sound. No critical-alert entitlement is requested.

---

### 4. PC Web & Desktop Dashboard Deployment

Build and host the surveillance dashboard directly from the Edge Mini PC:

```bash
cd mobile_app

# 1. Build Flutter Web
flutter build web --release

# 2. Deploy to backend web directory
cp -r build/web/* ../edge_backend/web_dashboard/
```
Open `https://edge-cctv.local` or `http://192.168.1.100:8000` in any desktop browser (Chrome, Edge, Safari) for the full surveillance dashboard.

---

## 🧪 Local Testing & Verification

```bash
# Run backend API tests (./run.sh --check-only --dev installs pytest)
cd edge_backend
python -m pytest tests/ -v

# Test WebRTC ICE servers
curl -s http://localhost:8000/api/v1/webrtc/ice-servers | jq .

# Post a test loss-prevention alert for a camera you have already added
# (there are no built-in demo cameras; use an id from GET /api/v1/cameras)
curl -X POST http://localhost:8000/api/v1/events/trigger \
  -H "Content-Type: application/json" \
  -H "X-Edge-API-Key: $INTERNAL_SERVICE_KEY" \
  -d '{
    "camera_id": "<camera_id>",
    "event_type": "THEFT_SUSPECTED",
    "severity": "HIGH",
    "description": "Test alert: please ignore."
  }' | jq .
```

---

## 📄 License
Private and Confidential — Edge AI CCTV Surveillance Core.
