// ==============================================================================
// Edge AI CCTV - ESP32-S3 High-Speed IP Camera Firmware (DMA-Optimized)
// ==============================================================================
// Features:
// 1. Dual-Port Video Streaming:
//    - Port 81 High-Speed MJPEG: http://<esp32-ip>:81/stream
//    - Port 80 Web Portal & Stream: http://<esp32-ip>/stream & http://<esp32-ip>/
//    - Single-Frame Snapshot:    http://<esp32-ip>/capture
//    - JSON Status Telemetry:    http://<esp32-ip>/status
// 2. Anti-Overflow DMA Engine:
//    - 16 MHz XCLK to stabilize PSRAM DMA bursts and eliminate FB-OVF
//    - CAMERA_GRAB_LATEST mode with cooperative task yielding (vTaskDelay)
//    - Double-buffered PSRAM with optimized single-pass chunk header transmission
// 3. mDNS Auto-Discovery: http://esp32-cctv.local
// 4. Compatible with Edge AI CCTV Core, OpenCV, go2rtc, VLC, and browsers.
// ==============================================================================

#include "esp_camera.h"
#include <WiFi.h>
#include <ESPmDNS.h>
#include <WiFiClient.h>
#include "esp_http_server.h"
#include "camera_pins.h"

// ── Wi-Fi Configuration ───────────────────────────────────────────────────────
// Set your Wi-Fi credentials here (or connect to Edge Mini PC's Wi-Fi hotspot)
const char* ssid = "YOUR_WIFI_SSID";
const char* password = "YOUR_WIFI_PASSWORD";

// Camera Device Name & mDNS Hostname
const char* hostname = "esp32-cctv";

// HTTP Stream Server Handlers
httpd_handle_t stream_httpd = NULL;
httpd_handle_t camera_httpd = NULL;

#define PART_BOUNDARY "123456789000000000000987654321"
static const char* _STREAM_CONTENT_TYPE = "multipart/x-mixed-replace;boundary=" PART_BOUNDARY;

// ── MJPEG Streaming Handler (Anti-Overflow & DMA-Paced) ────────────────────────
static esp_err_t stream_handler(httpd_req_t *req) {
  camera_fb_t * fb = NULL;
  esp_err_t res = ESP_OK;
  char part_buf[128];

  res = httpd_resp_set_type(req, _STREAM_CONTENT_TYPE);
  if (res != ESP_OK) return res;

  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  httpd_resp_set_hdr(req, "X-Framerate", "30");
  httpd_resp_set_hdr(req, "Cache-Control", "no-cache, no-store, must-revalidate");
  httpd_resp_set_hdr(req, "Pragma", "no-cache");

  while (true) {
    fb = esp_camera_fb_get();
    if (!fb) {
      Serial.println("[-] Camera capture failed, retrying...");
      vTaskDelay(pdMS_TO_TICKS(10));
      continue;
    }

    // Consolidated single boundary + header chunk to minimize TCP context switches
    size_t hlen = snprintf(part_buf, sizeof(part_buf),
      "\r\n--" PART_BOUNDARY "\r\nContent-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n",
      fb->len);
    
    res = httpd_resp_send_chunk(req, part_buf, hlen);
    if (res == ESP_OK) {
      res = httpd_resp_send_chunk(req, (const char *)fb->buf, fb->len);
    }

    // Return frame buffer immediately so DMA engine can reuse it without overflowing
    esp_camera_fb_return(fb);
    fb = NULL;

    if (res != ESP_OK) {
      // Client disconnected
      break;
    }

    // Cooperative yield to allow FreeRTOS Wi-Fi and DMA tasks to run without stalling
    vTaskDelay(pdMS_TO_TICKS(2));
  }
  return res;
}

// ── Snapshot Capture Handler ─────────────────────────────────────────────────
static esp_err_t capture_handler(httpd_req_t *req) {
  camera_fb_t * fb = esp_camera_fb_get();
  if (!fb) {
    httpd_resp_send_500(req);
    return ESP_FAIL;
  }

  httpd_resp_set_type(req, "image/jpeg");
  httpd_resp_set_hdr(req, "Content-Disposition", "inline; filename=capture.jpg");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");

  esp_err_t res = httpd_resp_send(req, (const char *)fb->buf, fb->len);
  esp_camera_fb_return(fb);
  return res;
}

// ── Status Telemetry Handler ─────────────────────────────────────────────────
static esp_err_t status_handler(httpd_req_t *req) {
  char json_buf[320];
  snprintf(json_buf, sizeof(json_buf),
    "{\"status\":\"online\",\"device\":\"ESP32-S3-CAM\",\"ip\":\"%s\","
    "\"free_heap\":%u,\"free_psram\":%u,\"rssi\":%d,\"mjpeg_port\":81,"
    "\"stream_url\":\"http://%s:81/stream\",\"snapshot_url\":\"http://%s/capture\"}",
    WiFi.localIP().toString().c_str(),
    ESP.getFreeHeap(),
    ESP.getFreePsram(),
    WiFi.RSSI(),
    WiFi.localIP().toString().c_str(),
    WiFi.localIP().toString().c_str()
  );

  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, json_buf, strlen(json_buf));
}

// ── Web Portal Landing Page Handler ──────────────────────────────────────────
static esp_err_t index_handler(httpd_req_t *req) {
  static const char index_html[] =
    "<!DOCTYPE html><html><head><meta charset='utf-8'><title>ESP32-S3 Edge CCTV Camera</title>"
    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
    "<style>"
    "body{margin:0;background:#0d1117;color:#e6edf3;font-family:system-ui,-apple-system,sans-serif;display:flex;flex-direction:column;align-items:center;padding:20px;}"
    "h1{color:#58a6ff;margin-bottom:8px;font-size:24px;}"
    ".badge{background:#238636;color:#fff;padding:3px 8px;border-radius:12px;font-size:12px;font-weight:bold;margin-left:8px;}"
    ".card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:16px;max-width:840px;width:100%;box-shadow:0 8px 24px rgba(0,0,0,0.5);margin-top:16px;}"
    "img{width:100%;border-radius:8px;background:#010409;border:1px solid #30363d;}"
    ".links{display:flex;gap:12px;margin-top:16px;flex-wrap:wrap;}"
    "a{background:#21262d;color:#58a6ff;padding:8px 16px;border-radius:6px;text-decoration:none;border:1px solid #30363d;font-size:14px;font-weight:600;}"
    "a:hover{background:#30363d;border-color:#8b949e;}"
    "</style></head><body>"
    "<h1>🛡️ ESP32-S3 CCTV Camera <span class='badge'>ONLINE</span></h1>"
    "<div class='card'>"
    "<img src='/stream' alt='Live Video Stream' />"
    "<div class='links'>"
    "<a href='/stream' target='_blank'>🎥 Open Stream (Port 80)</a>"
    "<a href=':81/stream' target='_blank'>⚡ High-Speed Stream (Port 81)</a>"
    "<a href='/capture' target='_blank'>📸 Capture Snapshot</a>"
    "<a href='/status' target='_blank'>📊 Device Telemetry</a>"
    "</div></div></body></html>";

  httpd_resp_set_type(req, "text/html");
  return httpd_resp_send(req, index_html, strlen(index_html));
}

// ── HTTP Server Initializer ──────────────────────────────────────────────────
void startCameraServer() {
  // Main Web & Snapshot Server on Port 80
  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  config.server_port = 80;
  config.ctrl_port = 32768;
  config.lru_purge_enable = true;
  config.send_wait_timeout = 2;
  config.recv_wait_timeout = 2;

  httpd_uri_t index_uri   = { .uri = "/",        .method = HTTP_GET, .handler = index_handler,   .user_ctx = NULL };
  httpd_uri_t stream80_uri= { .uri = "/stream",  .method = HTTP_GET, .handler = stream_handler,  .user_ctx = NULL };
  httpd_uri_t capture_uri = { .uri = "/capture", .method = HTTP_GET, .handler = capture_handler, .user_ctx = NULL };
  httpd_uri_t status_uri  = { .uri = "/status",  .method = HTTP_GET, .handler = status_handler,  .user_ctx = NULL };

  if (httpd_start(&camera_httpd, &config) == ESP_OK) {
    httpd_register_uri_handler(camera_httpd, &index_uri);
    httpd_register_uri_handler(camera_httpd, &stream80_uri);
    httpd_register_uri_handler(camera_httpd, &capture_uri);
    httpd_register_uri_handler(camera_httpd, &status_uri);
  }

  // Dedicated High-Speed Stream Server on Port 81
  config.server_port = 81;
  config.ctrl_port = 32769;

  httpd_uri_t stream81_uri = { .uri = "/stream", .method = HTTP_GET, .handler = stream_handler, .user_ctx = NULL };

  if (httpd_start(&stream_httpd, &config) == ESP_OK) {
    httpd_register_uri_handler(stream_httpd, &stream81_uri);
  }
}

// ── Arduino Setup ────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  Serial.setDebugOutput(false); // Reduce UART flooding
  Serial.println();
  Serial.println("=================================================");
  Serial.println("   Edge AI CCTV - ESP32-S3 IP Camera Initializing ");
  Serial.println("=================================================");

  #if defined(LED_GPIO_NUM) && LED_GPIO_NUM >= 0
    pinMode(LED_GPIO_NUM, OUTPUT);
    digitalWrite(LED_GPIO_NUM, LOW);
  #endif

  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  
  // 16 MHz XCLK frequency stabilizes PSRAM DMA bus timing and eliminates FB-OVF
  config.xclk_freq_hz = 16000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.grab_mode = CAMERA_GRAB_LATEST;

  // Frame size & buffer allocation based on PSRAM availability
  if (psramFound()) {
    Serial.printf("[+] PSRAM Detected: %d bytes free\n", ESP.getFreePsram());
    config.frame_size = FRAMESIZE_SVGA; // 800x600 (ideal for Edge AI kinematics)
    config.jpeg_quality = 12;           // 10-14 gives crisp detail with zero DMA overflow
    config.fb_count = 2;                // Double buffering
    config.fb_location = CAMERA_FB_IN_PSRAM;
  } else {
    Serial.println("[-] No PSRAM detected. Falling back to DRAM VGA mode.");
    config.frame_size = FRAMESIZE_VGA;  // 640x480
    config.jpeg_quality = 14;
    config.fb_count = 1;
    config.fb_location = CAMERA_FB_IN_DRAM;
  }

  // Camera Sensor Initialization
  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("[-] Camera init failed with error 0x%x\n", err);
    return;
  }
  Serial.println("[+] Camera sensor initialized successfully.");

  // Sensor Image Tuning
  sensor_t * s = esp_camera_sensor_get();
  if (s != NULL) {
    s->set_brightness(s, 1);     // -2 to 2
    s->set_contrast(s, 1);       // -2 to 2
    s->set_saturation(s, 0);     // -2 to 2
    s->set_whitebal(s, 1);       // Auto White Balance
    s->set_awb_gain(s, 1);       // Auto WB Gain
    s->set_wb_mode(s, 0);        // Auto Mode
    s->set_exposure_ctrl(s, 1);  // Auto Exposure
    s->set_aec2(s, 1);           // Auto Exposure Calc
    s->set_gain_ctrl(s, 1);      // Auto Gain
    s->set_agc_gain(s, 0);       // AGC
    s->set_gainceiling(s, (gainceiling_t)2);
    s->set_bpc(s, 1);            // Bad Pixel Correction
    s->set_wpc(s, 1);            // White Pixel Correction
    s->set_raw_gma(s, 1);        // Gamma Correction
    s->set_lenc(s, 1);           // Lens Correction
    s->set_hmirror(s, 0);        // Horizontal Mirror
    s->set_vflip(s, 0);          // Vertical Flip
  }

  // Connect to Wi-Fi
  Serial.printf("[+] Connecting to Wi-Fi: %s", ssid);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false); // Disable Wi-Fi sleep for lowest latency streaming
  WiFi.begin(ssid, password);

  int retry = 0;
  while (WiFi.status() != WL_CONNECTED && retry < 30) {
    delay(500);
    Serial.print(".");
    #if defined(LED_GPIO_NUM) && LED_GPIO_NUM >= 0
      digitalWrite(LED_GPIO_NUM, !digitalRead(LED_GPIO_NUM));
    #endif
    retry++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println();
    Serial.println("=================================================");
    Serial.println("  ✅ ESP32-S3 Camera Connected to Network! ");
    Serial.println("=================================================");
    Serial.printf("  • Web Portal:   http://%s\n", WiFi.localIP().toString().c_str());
    Serial.printf("  • MJPEG Stream: http://%s:81/stream\n", WiFi.localIP().toString().c_str());
    Serial.printf("  • Alt Stream:   http://%s/stream\n", WiFi.localIP().toString().c_str());
    Serial.printf("  • Snapshot URL: http://%s/capture\n", WiFi.localIP().toString().c_str());
    Serial.printf("  • mDNS Address: http://%s.local\n", hostname);
    Serial.println("=================================================");

    // Register mDNS
    if (MDNS.begin(hostname)) {
      MDNS.addService("http", "tcp", 80);
      MDNS.addService("cctv-stream", "tcp", 81);
      Serial.println("[+] mDNS responder started: esp32-cctv.local");
    }

    #if defined(LED_GPIO_NUM) && LED_GPIO_NUM >= 0
      digitalWrite(LED_GPIO_NUM, HIGH); // Solid ON
    #endif

    // Start Streaming Web Server
    startCameraServer();
    Serial.println("[+] Video stream server active and ready for Edge AI ingestion.");
  } else {
    Serial.println("\n[-] Wi-Fi connection timed out. Check SSID and password.");
  }
}

// ── Arduino Loop ─────────────────────────────────────────────────────────────
void loop() {
  vTaskDelay(pdMS_TO_TICKS(1000)); // FreeRTOS server daemon handles streaming
}
