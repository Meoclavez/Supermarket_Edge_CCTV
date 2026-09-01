# 🛡️ Edge AI CCTV Surveillance & Safety Platform

An enterprise-grade, **100% on-premises edge-processed CCTV AI monitoring and safety ecosystem**. Designed for **Intel N100 Mini PCs** paired with **Hailo-8 / 8L M.2 PCIe AI modules**, executing hardware-accelerated video decoding via **Intel QuickSync (VA-API)** and sub-10ms neural inference on the **HailoRT dataflow engine**.

Connects directly to a **cross-platform Flutter client (PC Web/Desktop, Android, and iOS)** featuring **Critical Emergency Alerts (bypassing DND/Silent)**, **24/7 Segmented DVR Recording with 24-Hour Timeline Scrubbing**, **Interactive Visual Zone & Privacy Mask Editor**, **2-Way Audio Push-to-Talk**, and **Ultra-Low Latency (<300ms) WebRTC Streaming**.

---

## 🌟 Key Features

* **⚡ Sub-10ms Edge AI Vision (Hailo-8 M.2)**:
  * Hardware accelerated **YOLOv8n** object detection ($<4\text{ms}$) + **YOLO-Pose** 17-keypoint pose estimation ($<8\text{ms}$).
  * **Kinematic Fall Engine**: Multi-frame EMA velocity tracking, bounding box collapse, and horizontal torso inclination angle ($<35^\circ$).
  * **Spatial AI Zones**: 2D vector cross-product directed virtual tripwires (`A_TO_B`, `B_TO_A`) and Ray-Casting Point-in-Polygon (PIP) intrusion detection.
  * **Temporal State Machines**: Door Left Open ($>5\text{ min}$) and Package Theft displacement detectors.
* **🔒 Source-Level Hardware Privacy Masking**:
  * In-place masking (`BLACKOUT`, `BLUR`, `MOSAIC`, `COLOR`) applied directly to source frames before ring-buffering, snapshot generation, or WebRTC streaming.
* **📼 24/7 Segmented NVR & 24-Hour Timeline**:
  * Continuous zero-copy H.264 stream remuxing (`-c:v copy`) consumes **$<0.5\%$ CPU per 1080p stream** on the Intel N100.
  * Fragmented keyframe moov headers prevent video file corruption during unexpected power outages.
  * Dynamic HLS (`.m3u8`) generator with gap discontinuity tags and sub-2-second lossless incident MP4 export (`/api/v1/cameras/{id}/export`).
* **🌐 Zero-Trust WebRTC & NAT Traversal**:
  * **go2rtc** media gateway ($<300\text{ms}$ latency) + **Coturn** RFC 5766 dynamic HMAC-SHA1 authenticated STUN/TURN relay for symmetric 4G/5G mobile connectivity.
  * **Push-to-Talk 2-Way Audio Backchannel**: Encodes Flutter microphone audio to Opus 48kHz and routes to camera speakers.
  * Local mDNS / Bonjour broadcaster (`_cctv-edge._tcp.local`) + automated 2048-bit SAN TLS certificates.
* **🚨 Critical Emergency Takeover & DND Bypass**:
  * **iOS**: Native `time-sensitive` priority + custom high-intensity siren + `AVAudioSession` loudspeaker override + Apple Critical Alerts entitlement support.
  * **Android**: High-priority `USAGE_ALARM` notification channel with `FLAG_TURN_SCREEN_ON` waking the display and playing alarms through DND.
  * **Lockscreen Interactive Actions**: "👁️ View Live", "🔕 Mute 5m", "📞 Call Contact" (`tel:911`).
* **📱 Adaptive Cross-Platform Flutter Client**:
  * Responsive 3-tier layout: Mobile bottom nav ($<600\text{px}$), Tablet rail ($600-1100\text{px}$), and Desktop expanded sidebar ($>1100\text{px}$).
  * **Visual Zone Canvas**: Click and drag polygon vertices and directed tripwire vectors directly on live camera snapshots.
  * **SMART Storage Health Dashboard**: NVMe wear levels, drive temperature, reallocated sectors, and per-camera quota progress bars.
  * **Biometric Gate**: Hardware Face ID / Fingerprint verification for viewing sensitive feeds and dismissing alarms.

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
 │ 3. Hailo-8 M.2 Dataflow: YOLOv8n + 17-Keypoint Pose Kinematics.             │
 │ 4. Zero-Copy 24/7 DVR remuxing saves 1-min MP4 chunks + Dynamic HLS.       │
 │ 5. go2rtc (<300ms WebRTC) + Coturn RFC 5766 dynamic HMAC-SHA1 TURN.        │
 │ 6. Caddy TLS Reverse Proxy + mDNS Zeroconf Broadcaster.                     │
 │ 7. APNs Critical Alerts & FCM USAGE_ALARM Push Dispatcher.                  │
 └──────────────────────────────────────┬──────────────────────────────────────┘
                                        │ (HTTPS / WSS / WebRTC + Opus Mic)
                                        ▼
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │             CROSS-PLATFORM FLUTTER CLIENT (DESKTOP / WEB / MOBILE)         │
 │                                                                             │
 │  • Adaptive AppShell: Mobile bottom nav, Tablet rail, Desktop sidebar.      │
 │  • Live Multi-Cam Grid Wall: 1 to 16 adaptive camera tiles.                 │
 │  • 24/7 Continuous DVR Player: 60fps timeline with pinch zoom & snapping.   │
 │  • Visual Zone & Mask Canvas: Draw & drag polygons/tripwires on snapshots.  │
 │  • System & SMART Storage Dashboard: Disk gauges, wear level, camera quota. │
 │  • AI Incident Center: Severity-filtered alerts with instant clip playback. │
 │  • Biometric Gate: FaceID / Fingerprint lock with 60s grace period.         │
 │  • WebRTC 2-Way Audio Talkback: Native Opus microphone backchannel.         │
 └─────────────────────────────────────────────────────────────────────────────┘
```

---

## 📦 Project Structure

```
Edge_AI_CCTV/
├── .agents/
│   ├── project_map.md                        # Complete project architecture & API index
│   ├── system_architecture_spec.md           # Exhaustive hardware & algorithm specification
│   └── api_and_integration_reference.md      # REST API, WebRTC signaling & Docker schemas
│
├── edge_backend/                             # Edge Mini PC Backend Services
│   ├── app/
│   │   ├── main.py                           # FastAPI app, SQLite lifecycle, mDNS, 24/7 DVR
│   │   ├── config.py                         # Settings, retention policies, Coturn keys
│   │   ├── database.py                       # SQLAlchemy async SQLite session factory
│   │   ├── models/
│   │   │   ├── schemas.py                    # Pydantic schemas (Zones, DVR, Timeline, Storage)
│   │   │   └── db_models.py                  # SQLAlchemy ORM models
│   │   ├── routes/
│   │   │   ├── health.py                     # Hardware & telemetry monitoring (/api/v1/health)
│   │   │   ├── cameras.py                    # Camera CRUD, snapshots, device registration
│   │   │   ├── events.py                     # AI event ingestion, history, clip streaming
│   │   │   ├── webrtc.py                     # WebRTC SDP signaling & dynamic ICE servers
│   │   │   ├── dvr.py                        # 24h timeline, dynamic HLS, incident exports
│   │   │   └── zones.py                      # Privacy masks, tripwires & alert muting
│   │   └── services/
│   │       ├── hailo_inference_service.py    # HailoRT PCIe runner & kinematic fall engine
│   │       ├── ai_zone_service.py            # Privacy masks, tripwires, PIP & state machines
│   │       ├── dvr_recorder.py               # 24/7 continuous segmenter, HLS, stitcher & SMART
│   │       ├── video_ingest_service.py       # Threaded QuickSync VA-API grabber with masking
│   │       ├── clip_recorder.py              # In-memory JPEG ring-buffer MP4 generator
│   │       ├── notification_service.py       # Dual-mode APNs & FCM push dispatcher
│   │       ├── turn_service.py               # RFC 5766 dynamic ephemeral TURN credentials
│   │       ├── mdns_service.py               # Bonjour/Zeroconf mDNS advertiser
│   │       └── auth_service.py               # JWT session manager & path traversal sanitizer
│   ├── tests/
│   │   ├── test_api.py                       # REST API, auth, ICE, zones, timeline tests
│   │   └── test_kinematics.py                # Kinematics, polygon PIP & tripwire unit tests
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

# 2. Launch microservices
docker compose up -d --build
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
WorkingDirectory=/home/meoclavezz/Projects-1/Edge_AI_CCTV/edge_backend
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
* **Install**: Transfer `.apk` directly to Android devices via USB or local browser. Android automatically configures `USAGE_ALARM` channels and `FLAG_TURN_SCREEN_ON`.

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
> **iOS DND Bypass Fallback**: While awaiting Apple's Critical Alerts entitlement review, the system automatically uses **`time-sensitive`** priority + custom siren audio + `AVAudioSession` loudspeaker override, functioning immediately without review delay.

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
# Run backend API and kinematics tests
cd edge_backend
pytest tests/ -v

# Test WebRTC ICE servers
curl -s http://localhost:8000/api/v1/webrtc/ice-servers | jq .

# Simulate critical fall event
curl -X POST http://localhost:8000/api/v1/events/trigger \
  -H "Content-Type: application/json" \
  -H "X-Edge-API-Key: edge_ai_vision_internal_secret" \
  -d '{
    "camera_id": "cam_living_room",
    "event_type": "FALL_DETECTED",
    "severity": "CRITICAL",
    "confidence": 0.96,
    "bounding_box": {"x_min": 0.2, "y_min": 0.6, "x_max": 0.8, "y_max": 0.95, "confidence": 0.96, "label": "person_fallen"},
    "kinematics": {"hip_descent_velocity": 2.3, "aspect_ratio_initial": 1.7, "aspect_ratio_final": 0.52, "transition_duration_ms": 390, "immobility_duration_sec": 5.2, "floor_proximity_score": 0.92, "torso_angle_deg": 18.5}
  }' | jq .
```

---

## 📄 License
Private and Confidential — Edge AI CCTV Surveillance Core.
