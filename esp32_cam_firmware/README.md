# 📷 ESP32-S3 IP Camera Firmware & Setup Guide

This firmware turns any **ESP32-S3 Camera module** (OV2640 / OV5640 / OV3660) into a dedicated high-performance RTSP & MJPEG IP security camera for the **Edge AI CCTV Surveillance System**.

---

## 🔌 Supported ESP32-S3 Camera Boards

* **Freenove ESP32-S3 WROOM CAM** (Default)
* **Seeed Studio XIAO ESP32S3 Sense**
* **AI-Thinker ESP32-S3 CAM**
* **ESP32-S3-EYE / LilyGO T-Camera S3**
* **Standard ESP32-CAM (AI Thinker OV2640)**

*(To select your board, uncomment the matching line in [`camera_pins.h`](./camera_pins.h))*

---

## ⚡ Method 1: Flashing via Arduino IDE

### 1. Install Arduino ESP32 Board Support
1. Open **Arduino IDE** $\rightarrow$ **File** $\rightarrow$ **Preferences**.
2. Add this URL to *Additional Board Manager URLs*:
   ```
   https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
   ```
3. Go to **Tools** $\rightarrow$ **Board** $\rightarrow$ **Boards Manager** and install **esp32 by Espressif Systems** (version 2.0.14+ or 3.0.0+).

### 2. Configure Board Settings
Open [`esp32_s3_cctv_cam.ino`](./esp32_s3_cctv_cam.ino) and select:
* **Board**: `ESP32S3 Dev Module` (or `XIAO_ESP32S3` / `Freenove ESP32-S3`)
* **USB CDC On Boot**: `Enabled`
* **CPU Frequency**: `240MHz (WiFi)`
* **Flash Mode**: `QIO 80MHz`
* **Flash Size**: `8MB` (or `16MB` depending on your module)
* **Partition Scheme**: `Huge APP (3MB No OTA/1MB SPIFFS)` or `8M with spiffs`
* **PSRAM**: `OPI PSRAM` ⚠️ *(Critical: Enables double-buffered 30 FPS SVGA/HD streaming)*
* **Upload Speed**: `921600`

### 3. Set Wi-Fi Credentials & Upload
1. Edit line 23-24 of `esp32_s3_cctv_cam.ino`:
   ```cpp
   const char* ssid = "YOUR_WIFI_SSID";
   const char* password = "YOUR_WIFI_PASSWORD";
   ```
2. Plug your ESP32-S3 into your PC via USB-C cable.
3. Select your serial port (e.g. `/dev/ttyUSB0` or `/dev/ttyACM0` on Linux, `COM3` on Windows).
4. Click **Upload**.

---

## ⚡ Method 2: Flashing via PlatformIO (VS Code / CLI)

From your terminal:
```bash
cd esp32_cam_firmware
pio run -t upload
```

---

## 📡 Live Stream Endpoints

Once powered on and connected to Wi-Fi, the ESP32-S3 will print its IP address to the Serial Monitor (115200 baud) and broadcast:

| Stream Type | Endpoint URL | Description |
| :--- | :--- | :--- |
| **High-Speed MJPEG Stream** | `http://<esp32-ip>:81/stream` | Continuous 25-30 FPS video feed |
| **mDNS Stream (Zero-Conf)** | `http://esp32-cctv.local:81/stream` | Auto-discovery address |
| **Single Frame Snapshot** | `http://<esp32-ip>/capture` | High-res JPEG snapshot capture |
| **Status Telemetry** | `http://<esp32-ip>/status` | JSON telemetry (FPS, heap, sensor PID) |

---

## 🧪 Testing the ESP32 Stream Locally on PC

Run the automated test runner and pass your ESP32's stream URL:

```bash
./scripts/run_local_test.sh http://<esp32-ip>:81/stream
```

The system will ingest the ESP32 video, run YOLOv8 object detection, execute the kinematic fall detection engine, evaluate virtual tripwires/intrusion zones, and record verified MP4 clips in `storage/clips/`.
