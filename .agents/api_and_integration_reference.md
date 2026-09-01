# Edge AI CCTV - API & Integration Reference

## 1. REST API Specification

### 1.1 WebRTC & Zero-Trust Signaling
* `GET /api/v1/webrtc/ice-servers?client_id={id}`
  * **Response**:
    ```json
    {
      "iceServers": [
        {"urls": "stun:coturn:3478"},
        {
          "urls": ["turn:192.168.1.100:3478?transport=udp", "turn:192.168.1.100:3478?transport=tcp"],
          "username": "1724250000:client_mobile",
          "credential": "HMAC_SHA1_BASE64_TOKEN"
        }
      ],
      "ttl": 86400
    }
    ```
* `POST /api/v1/webrtc/offer`
  * **Payload**: `{"camera_id": "cam_living_room", "sdp": "v=0...", "type": "offer"}`
  * **Response**: `{"sdp": "v=0...", "type": "answer"}`
* `GET /api/v1/webrtc/token?camera_id={id}`
  * **Response**: `{"token": "JWT_STREAM_TOKEN", "expires_in": 86400}`

---

### 1.2 24/7 Segmented DVR & Timeline
* `GET /api/v1/cameras/{id}/timeline?date=YYYY-MM-DD`
  * **Response**:
    ```json
    {
      "camera_id": "cam_living_room",
      "camera_name": "Living Room",
      "date": "2026-08-21",
      "total_recorded_seconds": 86400.0,
      "total_segments": 1440,
      "hls_master_url": "/api/v1/dvr/cameras/cam_living_room/hls/2026-08-21/index.m3u8",
      "segments": [
        {
          "id": "dvr_seg_01",
          "camera_id": "cam_living_room",
          "start_time": "2026-08-21T00:00:00Z",
          "end_time": "2026-08-21T00:01:00Z",
          "duration_seconds": 60.0,
          "file_size_bytes": 15728640,
          "stream_url": "/api/v1/dvr/segments/dvr_seg_01/video"
        }
      ],
      "events": [
        {
          "id": "evt_fall_01",
          "event_type": "FALL_DETECTED",
          "severity": "CRITICAL",
          "confidence": 0.95,
          "timestamp": "2026-08-21T14:23:10Z",
          "snapshot_url": "/api/v1/events/snapshots/snap_01.jpg",
          "clip_url": "/api/v1/events/clips/clip_01.mp4"
        }
      ],
      "gaps": []
    }
    ```
* `GET /api/v1/dvr/cameras/{id}/hls/{date}/index.m3u8`
  * Dynamic HLS playlist with `#EXT-X-DISCONTINUITY` across stream gap intervals.
* `POST /api/v1/cameras/{id}/export`
  * **Payload**: `{"start_time": "2026-08-21T14:02:00Z", "end_time": "2026-08-21T14:07:00Z", "title": "Suspicious Intrusion"}`
  * **Response**: `{"id": "arch_01", "status": "COMPLETED", "download_url": "/api/v1/dvr/archives/arch_01/download"}`
* `GET /api/v1/storage/health`
  * Returns total GB, used GB, wear percentage, temperatures, and per-camera quotas.

---

### 1.3 Zones, Privacy Masks & Muting
* `GET /api/v1/cameras/{id}/zones`
  * Returns configured privacy masks, tripwires, and intrusion zones.
* `POST /api/v1/cameras/{id}/zones`
  * **Payload**:
    ```json
    {
      "id": "zone_driveway",
      "camera_id": "cam_living_room",
      "name": "Driveway Intrusion Polygon",
      "zone_type": "INTRUSION",
      "enabled": true,
      "polygon_points": [{"x": 0.1, "y": 0.3}, {"x": 0.8, "y": 0.3}, {"x": 0.8, "y": 0.8}, {"x": 0.1, "y": 0.8}],
      "dwell_time_seconds": 3.0,
      "allowed_classes": ["person", "car"]
    }
    ```
* `POST /api/v1/cameras/{id}/mute`
  * **Payload**: `{"duration_minutes": 5}`

---

### 1.4 Events & Device Registration
* `POST /api/v1/events/trigger`
  * Header: `X-Edge-API-Key: edge_ai_vision_internal_secret`
  * Ingests vision events and dispatches APNs/FCM emergency alerts.
* `POST /api/v1/cameras/register-device`
  * **Payload**: `{"device_token": "FCM_OR_APNS_TOKEN", "platform": "android", "device_name": "Pixel 8"}`
* `POST /api/v1/events/{id}/acknowledge`
  * Acknowledges emergency alarms.

---

## 2. Docker Compose Deployment Reference

```yaml
version: "3.8"

services:
  coturn:
    image: coturn/coturn:latest
    container_name: cctv_coturn
    restart: unless-stopped
    network_mode: host
    volumes:
      - ./coturn/coturn.conf:/etc/coturn/coturn.conf:ro

  go2rtc:
    image: alexxit/go2rtc:latest
    container_name: cctv_go2rtc
    restart: unless-stopped
    network_mode: host
    volumes:
      - ./go2rtc.yaml:/config/go2rtc.yaml:ro

  edge_api:
    build: .
    container_name: cctv_edge_api
    restart: unless-stopped
    network_mode: host
    devices:
      - /dev/dri/renderD128:/dev/dri/renderD128
      - /dev/hailo0:/dev/hailo0
    volumes:
      - ./storage:/app/storage
    environment:
      - PORT=8000
      - STORAGE_DIR=/app/storage
      - COTURN_SECRET=cctv_turn_super_secret_dynamic_key_change_me_in_prod

  caddy:
    image: caddy:2-alpine
    container_name: cctv_caddy
    restart: unless-stopped
    network_mode: host
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - ./certs:/etc/caddy/certs:ro
```
