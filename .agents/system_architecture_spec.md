# Edge AI CCTV Surveillance System - System Architecture Specification

## 1. Hardware & Acceleration Architecture

### 1.1 Host Hardware Platform
* **CPU / Platform**: Intel Processor N100 (4 Cores, 4 Threads, up to 3.4 GHz, 6W TDP) with Intel UHD Graphics.
* **AI Accelerator**: Hailo-8 / Hailo-8L M.2 2280 PCIe AI Acceleration Module (up to 26 TOPS / 13 TOPS).
* **Memory**: 16 GB DDR5 4800MHz.
* **Host Operating System**: Ubuntu 22.04 / 24.04 LTS (Linux Kernel 5.15+ / 6.x).

### 1.2 Hardware Passthrough & Decoding Pipeline
* **Intel QuickSync Video (VA-API)**:
  * Hardware device: `/dev/dri/renderD128`.
  * Stream Ingestion: Non-blocking threaded RTSP grabber in `video_ingest_service.py` decoding H.264/H.265 sub-streams via VA-API.
* **HailoRT PCIe Dataflow Acceleration**:
  * PCIe device: `/dev/hailo0`.
  * Model 1: `yolov8n.hef` (object detection, bounding boxes, class scoring, $<4\text{ms}$ latency).
  * Model 2: `yolov8n_pose.hef` (17-keypoint human pose estimation, $<8\text{ms}$ latency).
  * Multi-Stream Credit Scheduler: Dynamic token-bucket scheduler throttling camera FPS based on state (`IDLE`: 2 FPS @ 320x320, `MOTION`: 10 FPS @ 640x640, `ALERT_ACTIVE`: 25 FPS @ 640x640).

---

## 2. Edge Vision, Kinematics & Privacy Engine

### 2.1 Hardware Source-Level Privacy Masking
* Applied in-place on raw OpenCV frames inside `ThreadedVideoIngestWorker._worker_loop` before ring buffer buffering, snapshot saving, or streaming.
* Modes supported:
  * `BLACKOUT`: Solid RGB fill.
  * `BLUR`: High-intensity Gaussian blur ($51\times 51$ kernel).
  * `MOSAIC`: Downscale/upscale pixelation ($16\times$ downscale factor).
  * `COLOR`: Custom solid tint fill.

### 2.2 Spatial Geometry & Zone Engine
* **Polygon Intrusion & Dwell Timing**:
  * Ray-Casting & Winding Number Point-in-Polygon (PIP) algorithms.
  * Uses ground-plane footprint coordinates $P_{feet} = (\frac{x_{min}+x_{max}}{2}, y_{max})$ to eliminate false positives from tall upper-body projections.
* **Virtual Tripwires & Directional Line Crossing**:
  * 2D vector cross-product math calculating directed crossings:
    $$V_w = P_{end} - P_{start}, \quad V_1 = P_{prev} - P_{start}, \quad V_2 = P_{curr} - P_{start}$$
    $$C_1 = V_w \times V_1, \quad C_2 = V_w \times V_2$$
  * Sign change ($C_1 \cdot C_2 < 0$) combined with ray intersection determines crossing and direction (`A_TO_B` vs `B_TO_A` vs `BIDIRECTIONAL`).
* **Temporal Security State Machines**:
  * **Door Left Open**: Hysteresis debounce timer with a 5-minute timeout alert.
  * **Package Theft**: Anchored coordinate displacement tracker during stranger proximity.

### 2.3 Kinematic Fall Detection Algorithm
* Evaluates 17 body keypoints:
  1. EMA-smoothed vertical hip descent velocity ($v_y > 1.8\text{ m/s}$).
  2. Bounding box aspect ratio collapse ($\frac{\text{width}}{\text{height}} < 0.8$).
  3. Torso horizontal inclination angle ($< 35^\circ$ relative to ground plane).
  4. Post-fall immobility verification ($\ge 5.0\text{ seconds}$).

---

## 3. Storage, NVR & 24/7 Timeline Pipeline

### 3.1 24/7 Continuous Segmented Recording (`dvr_recorder.py`)
* Zero-copy remuxing via FFmpeg:
  ```bash
  ffmpeg -y -rtsp_transport tcp -i rtsp://... -c:v copy -c:a aac \
    -f segment -segment_time 60 -reset_timestamps 1 -strftime 1 \
    -movflags +faststart+frag_keyframe+empty_moov %Y%m%d_%H%M%S.mp4
  ```
* Consumes $<0.5\%$ CPU per 1080p stream on Intel N100.
* Fragmented keyframe moov headers prevent video file corruption in the event of unexpected power loss.

### 3.2 Dynamic HLS & 24-Hour Timeline Generation
* Dynamic playlist generator at `/api/v1/dvr/cameras/{id}/hls/{date}/index.m3u8` queries SQLite segment metadata and outputs valid `#EXT-X-DISCONTINUITY` tags across stream dropouts without SSD wear.
* Lossless incident export at `/api/v1/cameras/{id}/export` stitches custom time windows into standalone MP4s in $<2\text{ seconds}$ via FFmpeg concat demuxers.

### 3.3 Storage Health & FIFO Auto-Purge
* `/api/v1/storage/health` monitors NVMe SMART health, wear percentages, temperatures, and per-camera quotas.
* Background cleaner checks disk capacity every 30 seconds, automatically purging oldest segments via FIFO when disk usage exceeds $85\%$.

---

## 4. Zero-Trust Networking & WebRTC Signaling

### 4.1 Coturn TURN/STUN Relay (RFC 5766)
* Coturn service integrated in `docker-compose.yml` with REST API dynamic shared secret authentication (`turn_service.py`).
* Ephemeral time-limited HMAC-SHA1 tokens returned on `/api/v1/webrtc/ice-servers` ensure WebRTC connects reliably over symmetric 4G/5G cellular networks without open router ports.

### 4.2 Auto-Discovery & TLS Reverse Proxy
* `mdns_service.py` advertises `_cctv-edge._tcp.local` via Zeroconf on LAN.
* Automated 2048-bit SAN TLS cert generator (`generate_certs.py`) with Caddy reverse proxy terminating HTTPS/WSS on port 443.
* Tailscale mesh VPN integration blueprint for zero-configuration encrypted remote access.

---

## 5. Cross-Platform Flutter Client (Desktop / Web / Mobile)

### 5.1 Adaptive App Shell (`AppShell`)
* Responsive 3-tier adaptive navigation:
  * **Mobile ($<600\text{px}$)**: Bottom navigation bar with alert badges.
  * **Tablet ($600-1100\text{px}$)**: Navigation rail with icon tooltips.
  * **Desktop / PC Web ($>1100\text{px}$)**: Expandable sidebar navigation.

### 5.2 Core Client Views
1. **Live Multi-Cam Grid Wall (`MultiCamGridScreen`)**: Adaptive 1 to 16 camera tiles with WebRTC live feeds.
2. **24/7 DVR Playback Player (`DVRPlaybackScreen`)**: 60fps horizontal scrubber with pinch-to-zoom (1h $\leftrightarrow$ 24h) and magnetic snapping.
3. **Visual Zone & Mask Editor (`ZoneEditorScreen` & `ZoneCanvasPainter`)**: Interactive tool to click and drag vertices on live camera snapshots.
4. **SMART Storage Health Dashboard (`StorageHealthScreen`)**: Animated circular gauges, wear level indicators, and per-camera quota progress bars.
5. **AI Incident Center (`EventsCenterScreen`)**: Filtered event history with immediate MP4 clip playback.
6. **Incident Archives & Export Manager (`ClipArchivesScreen`)**: Incident video downloading and custom time window export triggers.

### 5.3 WebRTC 2-Way Audio Talkback & Biometric Gate
* `WebRtcService` dynamically munges SDP offers to prioritize H.264 video and Opus audio, capturing microphone input on a `SendRecv` transceiver for Push-to-Talk audio talkback to camera speakers.
* `BiometricGate` enforces hardware Face ID / Fingerprint verification before accessing live camera streams or dismissing critical emergency alarms.
