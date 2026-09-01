# Pearcedale Supermarket CCTV System - Deployment & Network Specification

> **Site Location:** Pearcedale, Victoria 3912, Australia  
> **Target Project:** Edge AI CCTV Surveillance Platform Integration  
> **Status:** Active / P2P Verified  
> **Last Updated:** August 30, 2026  

---

## 1. System Overview & Hardware Fingerprint

The supermarket surveillance infrastructure consists of a multi-channel Dahua DVR/NVR system connected to the store local network. It is remotely accessible via Dahua's global P2P relay (`easy4ip` / DMSS cloud) and local UPnP NAT port mappings.

* **Manufacturer / Firmware:** Dahua Technology (DevVersion `6.7.11`)
* **Device Serial Number (SN):** `8L0D384PAZ1EBB0`
* **MAC Address:** `3c:e3:6b:65:62:27` (Dahua OUI)
* **Official Mobile App:** `DMSS` (iOS & Android) / `SmartPSS` (Desktop)
* **Authentication:**
  * **Username:** `admin` (case-insensitive in some interfaces, standard: `admin`)
  * **Passwords:** `Pearcedale3912` / `Pearcedale1`

---

## 2. On-Site Network Topology (Australia LAN)

The DVR operates on a static IPv4 assignment within the supermarket's local subnet.

```
 [ Internet (Australia WAN) ]
               │
               ▼
   [ Router / Gateway: 192.168.20.254 ]  <── UPnP Port Mapping Succeeded
               │
       ┌───────┴────────────────────────┐
       │                                │
       ▼                                ▼
 [ Supermarket DVR ]            [ POS / Billing PCs ]
   192.168.20.160                 192.168.20.x
   (NIC1 - Static)
```

| Parameter | Configuration Value | Description |
| :--- | :--- | :--- |
| **Local IP Address** | `192.168.20.160` | Static IPv4 assigned to `NIC1` |
| **Subnet Mask** | `255.255.255.0` | `/24` Class C Subnet |
| **Default Gateway** | `192.168.20.254` | Main Supermarket Router |
| **Primary DNS** | `8.8.8.8` | Google Public DNS |
| **Secondary DNS** | `8.8.4.4` | Google Secondary DNS |
| **MTU** | `1500` | Standard Ethernet Frame Size |
| **DHCP Mode** | Disabled (Static) | Prevents local IP drift |

---

## 3. Port Allocation & UPnP Forwarding

The router at `192.168.20.254` has successfully mapped the DVR's core ports 1:1 via UPnP IGD:

| Service / Protocol | Internal Port | External Port | Transport | Purpose |
| :--- | :---: | :---: | :---: | :--- |
| **RTSP** | `554` | `554` | TCP / UDP | Live video feeds (H.264/H.265) |
| **HTTP (Web Admin)** | `80` | `80` | TCP | Browser web configuration interface |
| **HTTPS (Secure Web)**| `443` | `443` | TCP | Encrypted web management console |
| **Dahua Private TCP** | `37777` | `37777` | TCP | Dahua NetSDK, SmartPSS, and DMSS data |
| **Dahua Private UDP** | `37778` | `37778` | UDP | High-throughput video payload stream |
| **POS Transaction** | `38800` | `38800` | TCP | Cash register POS overlay integration |
| **SNMP** | `161` | `161` | UDP | Network monitoring & health telemetry |
| **NTP** | `123` | `123` | UDP | Time synchronization |

---

## 4. Remote Connectivity & Cloud P2P Architecture

The DVR maintains an active outbound keep-alive session to Dahua's global P2P cloud relay, allowing transcontinental access (India ↔ Australia) without manual router reconfiguration:

* **P2P Relay Server:** `152.32.156.47:8803` / `152.32.156.11:8800` (`easy4ipcloud.com`)
* **P2P Handshake State:** Verified Online (HTTP 200 OK)
* **Decrypted Telemetry:**
  ```json
  {
    "httpport": 80,
    "privport": 37777,
    "rtspport": 554,
    "tlsprivport": 37778,
    "randsalt": "d3534abe75747f69135c95b60e98d163"
  }
  ```

---

## 5. RTSP Stream URI Specifications

All camera feeds follow Dahua's standard RTSP media URL syntax:

### Sub-Streams (Recommended for AI Analytics & Multi-Camera Dashboard):
* **Format:** `rtsp://admin:<PASSWORD>@<HOST>:554/cam/realmonitor?channel=<CHANNEL_NUM>&subtype=1`
* **Resolution:** D1 / 720p @ 15–25 FPS (Low latency, minimal compute load on YOLO/Hailo AI models).
* **Examples:**
  * Camera 1 (Aisle 1): `rtsp://admin:Pearcedale3912@<HOST>:554/cam/realmonitor?channel=1&subtype=1`
  * Camera 2 (Checkout): `rtsp://admin:Pearcedale3912@<HOST>:554/cam/realmonitor?channel=2&subtype=1`
  * Camera 3 (Entrance): `rtsp://admin:Pearcedale3912@<HOST>:554/cam/realmonitor?channel=3&subtype=1`

### Main-Streams (Full HD Recording & Studio Forensics):
* **Format:** `rtsp://admin:<PASSWORD>@<HOST>:554/cam/realmonitor?channel=<CHANNEL_NUM>&subtype=0`
* **Resolution:** 1080p / 4K @ 25–30 FPS (Used for 24/7 lossless continuous recording and export clips).

---

## 6. Integration with Edge AI CCTV (`go2rtc.yaml`)

To ingest the supermarket feeds into the Edge AI CCTV pipeline with sub-300ms WebRTC streaming and VA-API hardware decoding:

```yaml
# edge_backend/go2rtc.yaml
log:
  level: info

api:
  listen: "127.0.0.1:1984"

rtsp:
  listen: ":8554"

webrtc:
  listen: ":8555"
  candidates:
    - stun:stun.l.google.com:19302

streams:
  pearcedale_cam1:
    - "rtsp://admin:Pearcedale3912@127.0.0.1:8554/cam/realmonitor?channel=1&subtype=1"
    - "ffmpeg:pearcedale_cam1#video=h264#hardware=vaapi"

  pearcedale_cam2:
    - "rtsp://admin:Pearcedale3912@127.0.0.1:8554/cam/realmonitor?channel=2&subtype=1"
    - "ffmpeg:pearcedale_cam2#video=h264#hardware=vaapi"

  pearcedale_cam3:
    - "rtsp://admin:Pearcedale3912@127.0.0.1:8554/cam/realmonitor?channel=3&subtype=1"
    - "ffmpeg:pearcedale_cam3#video=h264#hardware=vaapi"
```

---

## 7. Upcoming Project Roadmap for Supermarket CCTV

1. **Camera Feed Calibration:** Map each camera channel index (1 to N) to physical supermarket zones (e.g., Entrance, Checkout 1-3, Liquor Section, High-Value Shelves, Loading Bay).
2. **AI Feature Enablement:**
   * **Tripwire Customer Counting:** Bi-directional line crossing across Entrance/Exit doors.
   * **Aisle Dwell & Loitering Analysis:** Polygon heatmaps measuring customer engagement.
   * **Loss Prevention / Restricted Zones:** Nighttime intrusion detection polygon triggers.
   * **Slip-and-Fall Pose Kinematics:** 17-keypoint skeleton monitoring in customer aisles.
3. **Dataset Capture:** Record 15-minute sample MP4 clips for each channel during peak trading hours to build our automated test suite for YOLO model inference and ByteTrack evaluation.
