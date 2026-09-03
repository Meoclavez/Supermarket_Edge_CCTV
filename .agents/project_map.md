# Edge AI CCTV Architecture & File Index Map

## High-Level Architecture Overview
The Edge AI CCTV System is an end-to-end, decentralized, 100% on-premise AI surveillance system. It provides:
1. **Multi-Camera Home Feed & Retail Intelligence Dashboard (`/dashboard` & `/dashboard/analytics`):**
   - **Live Camera Matrix (28 Cameras):** Responsive multi-channel grid with category filtering (Entrance/Exit, Aisles 1-12, Fresh Produce & Bakery, Checkouts 1-6, Stockroom & Loading Dock), FPS decimation controls (1 to 25 FPS), name search, and stream telemetry.
   - **2D Interactive Store Blueprint & Heatmap HUD:** HTML5 Canvas / SVG layered floorplan renderer displaying supermarket aisles, shelf zones, real-time dynamic customer particles, Gaussian density heatmaps, flow vectors, and clickable zone inspection.
   - **Retail Analytics & Funnels:** Visual charts comparing Hourly Footfall, 5-stage Conversion Funnel, Lost Sales Index ($\phi$), Checkout Queue Latency, and Demographics.
   - **AI Decision Action Center:** Prioritized recommendations (Merchandising, Staffing, Store Layout, Loss Prevention) with status mutations (Mark Done, Schedule Review).
   - **Executive Daily Digest:** One-click executive summary briefing with print/PDF layout styling.
2. **Camera Studio & Zone Editor Sub-Page (`/dashboard/studio?camera_id=...`):** Full interactive HTML5 Canvas HUD over live video feeds for drawing Virtual Tripwires (with Direction A->B, B->A, bidirectional and counting), Restricted Intrusion Polygons, and Privacy & AI Exclusion Masks with persistence in `storage/zones_config.json`.
3. **Universal Hardware & Decoder Auto-Detection:** Automatically probes host hardware for NVIDIA NVDEC (CUDA/TensorRT), Intel QuickSync VA-API (OpenVINO GPU), AMD Radeon Mesa (OpenVINO CPU), or CPU SIMD (AVX2/AVX-512) and dynamically adapts shared memory ring buffers (`/dev/shm`).
4. **Sub-300ms WebRTC Streaming:** Integration with `go2rtc` and WebSocket signaling for minimal latency.
5. **Critical Mobile Alerts:** Flutter mobile app on Android & iOS bypassing Silent and Do-Not-Disturb modes for life-critical emergencies.

---

## File & API Reference

### Backend Core (`edge_backend/app/`)
* **[`app/main.py`](file:///home/meoclavezz/Projects-1/Edge_AI_CCTV/edge_backend/app/main.py):** FastAPI application entrypoint, static file mounts, `/dashboard` (Master Dashboard), `/dashboard/analytics` (Retail Hub), `/dashboard/studio` (Studio sub-page), `/stream` (Live MJPEG generator), and API router registrations.
* **[`app/config.py`](file:///home/meoclavezz/Projects-1/Edge_AI_CCTV/edge_backend/app/config.py):** Settings, storage paths, retention policies, JWT keys, go2rtc URLs, and hardware device paths with graceful local fallback directory resolution.
* **[`app/models/schemas.py`](file:///home/meoclavezz/Projects-1/Edge_AI_CCTV/edge_backend/app/models/schemas.py):** Pydantic schemas for `ZoneConfig`, `Point2D`, `TripwireDirection`, `MaskMode`, `BoundingBox`, `Keypoint`, `CameraFeed`, `CameraFeatureConfig`, `HardwareProfile`, `SystemStats`, and `SecurityEvent`.
* **[`app/services/hardware_detector.py`](file:///home/meoclavezz/Projects-1/Edge_AI_CCTV/edge_backend/app/services/hardware_detector.py):** Hardware probe for NVIDIA NVDEC, Intel VA-API, AMD Mesa, CPU SIMD, and dynamic RAM sizing.
* **[`app/services/feature_manager.py`](file:///home/meoclavezz/Projects-1/Edge_AI_CCTV/edge_backend/app/services/feature_manager.py):** In-memory thread-safe feature flags manager with hot-reload support.
* **[`app/services/ai_zone_service.py`](file:///home/meoclavezz/Projects-1/Edge_AI_CCTV/edge_backend/app/services/ai_zone_service.py):** Raycasting polygon geometry, tripwire line crossing with multi-point spine tracking, privacy masking, and persistence to `storage/zones_config.json`.
* **[`app/services/camera_network_manager.py`](file:///home/meoclavezz/Projects-1/Edge_AI_CCTV/edge_backend/app/services/camera_network_manager.py):** Concurrent `ThreadPoolExecutor` scanner for USB `/dev/video*`, mDNS `esp32-cctv.local`, and subnet RTSP hosts.
* **[`app/services/retail_analytics_service.py`](file:///home/meoclavezz/Projects-1/Edge_AI_CCTV/edge_backend/app/services/retail_analytics_service.py):** Core retail analytics & math engine (Funnel equations: Attraction $\alpha$, Engagement $\beta$, True Visual Conversion $\gamma$, Lost Sales / Friction $\phi$; 3x3 DLT Homography coordinate projection; Multi-Camera Re-ID journey stitching; Checkout queue wait distributions & service rates; Zero-PII demographics aggregator).
* **[`app/services/retail_decision_engine.py`](file:///home/meoclavezz/Projects-1/Edge_AI_CCTV/edge_backend/app/services/retail_decision_engine.py):** Automated decision & reasoning engine with anomaly detectors (High-Interest/Low-Conversion $\phi > 75\%$ / $\gamma < 10\%$, Chronic Dead-Zone $< 25\%$ traffic, Checkout Queue Bottlenecks $> 4.5$ min wait, Shelf Stockout put-back detection, Promotional star opportunities) and generative operational action synthesizer.
* **[`app/services/retail_seed_data.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/retail_seed_data.py):** High-fidelity supermarket simulation dataset generator (50m x 30m store blueprint, 25 Dahua camera streams with calibrated homography matrices, 500+ customer tracks, 200+ POS transactions, pre-computed analytics).
* **[`app/services/shelf_interaction_service.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/shelf_interaction_service.py):** Product shelf mapping and hand-to-shelf tracking engine (17-keypoint skeleton wrist tracking, APPROACH -> REACH_IN -> DWELL_INSPECT -> ITEM_PICK vs PUT_BACK state machine, study metrics options, and persistent storage in `storage/shelf_products_config.json`).
* **[`app/services/market_predictor.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/market_predictor.py):** Machine learning predictive market engine (24-hour predictive footfall curve with rush hour modeling, shelf stockout timeline estimator $T_{stockout} = \text{Stock} / V_{pick}$, and shelf placement tier elasticity simulator).
* **[`app/services/llm_market_agent.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/llm_market_agent.py):** LLM multimodal market reasoning and optimization agent (synthesizes visual interaction metrics with POS receipts into actionable merchandising, dynamic pricing, and staff replenishment directives).
* **[`tests/test_shelf_interaction.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/tests/test_shelf_interaction.py):** Unit tests for product shelf zone CRUD, wrist pose entry/dwell/exit state machine, and friction calculations.
* **[`tests/test_market_ai.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/tests/test_market_ai.py):** Unit tests for hourly footfall predictions, inventory stockout velocity, placement tier elasticity simulations, and LLM market agent reasoning.
* **[`tests/test_retail_engine.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/tests/test_retail_engine.py):** Comprehensive 18-test suite validating retail funnel math, homography projections, journey stitching, queue analytics, zero-PII privacy, decision engine anomalies, and 25-camera seed dataset integrity.
* **[`app/routes/cameras.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/routes/cameras.py):** 28-channel supermarket camera matrix feeds, snapshot rendering, `/api/v1/cameras/scan`, and feature toggle endpoints.
* **[`app/routes/analytics.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/routes/analytics.py):** Retail intelligence analytics endpoints for store KPIs, interactive floorplan data, conversion funnels, checkout queue metrics, AI action center recommendations, product shelf ROI mapping (`/products/zones`), hand interactions (`/products/interactions`), predictive market forecasts (`/market/predictions`), and LLM optimization triggers (`/market/llm-optimize`).
* **[`app/routes/zones.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/routes/zones.py):** REST endpoints for adding, listing, and deleting tripwires, intrusion polygons, and exclusion masks.
* **[`app/routes/system.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/routes/system.py):** Hardware profiling and real-time CPU/GPU/RAM telemetry.

### Web Studio & Dashboard (`edge_backend/app/static/`)
* **[`app/static/index.html`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/index.html):** Multi-Tab Master Cyber-Glassmorphism Dashboard with Live Camera Matrix, 2D Store Blueprint HUD, Retail Analytics & Funnels, AI Action Center, ML Market Intelligence & Suggestions, and Executive Daily Digest.
* **[`app/static/analytics.html`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/analytics.html):** Dedicated Standalone Retail Intelligence Hub with full Market AI tab.
* **[`app/static/studio.html`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/studio.html):** Interactive Canvas Studio sub-page with Tripwires, Restricted Intrusion Polygons, Privacy Masks, and interactive Product Shelf Area mapping modal (linking SKU, Category, Price, Shelf Tier, and study options).
* **[`app/static/js/floorplan.js`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/js/floorplan.js):** HTML5 Canvas & Layered SVG store blueprint HUD engine with Gaussian heatmap interpolation, animated shoppers, flow vectors, and clickable zone inspector.
* **[`app/static/js/analytics.js`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/js/analytics.js):** Dashboard controller for live polling, Chart.js graphs, 28-camera matrix filtering, FPS decimation, ML stockout timelines, 24h predictive footfall histogram, placement elasticity simulator, and LLM market optimizations.
* **[`app/static/js/studio.js`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/js/studio.js):** Interactive canvas drawing with 4+ point Product Shelf ROI mode, product modal configuration, and live zone REST client.
* **[`app/static/js/app.js`](file:///home/meoclavezz/Projects-1/Edge_AI_CCTV/edge_backend/app/static/js/app.js):** Legacy home feed controller.
* **[`app/static/css/style.css`](file:///home/meoclavezz/Projects-1/Edge_AI_CCTV/edge_backend/app/static/css/style.css):** Glassmorphism cyber-HUD stylesheet with full mobile (<768px), tablet (768-1024px), desktop (>1024px), and print media query support.

### Mobile Client (`mobile_app/lib/`)
* **`lib/screens/dashboard_screen.dart`:** Multi-camera live grid view.
* **`lib/screens/zone_editor_screen.dart`:** Touchscreen zone and tripwire canvas editor.
* **`lib/screens/camera_settings_screen.dart`:** Granular AI feature toggle switches.
* **`lib/screens/emergency_alert_screen.dart`:** Fullscreen takeover siren during critical emergencies.
* **`android/.../MainActivity.kt` & `ios/.../AppDelegate.swift`:** Native Android `USAGE_ALARM` notification channel and iOS `criticalAlert` entitlement handler.

### Deployment & Integration Specifications (`docs/`)
* **[`docs/supermarket_cctv/supermarket_cctv_deployment_spec.md`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/docs/supermarket_cctv/supermarket_cctv_deployment_spec.md):** Complete hardware, network, port mapping (UPnP), and Dahua P2P cloud specification for the Pearcedale Supermarket CCTV System (Australia) including RTSP streaming endpoints and go2rtc integration.
* **[`docs/supermarket_cctv/hardware_sizing_and_procurement_guide_30_cameras.md`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/docs/supermarket_cctv/hardware_sizing_and_procurement_guide_30_cameras.md):** Complete hardware sizing, workload throughput mathematics, multi-model TensorRT VRAM sizing, itemized Bill of Materials (BOM), and procurement options for 30+ camera supermarket installations.
