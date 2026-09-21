# Edge AI CCTV Architecture & File Index Map

## High-Level Architecture Overview

The Edge AI CCTV System is an end-to-end, decentralised, 100% on-premise AI
surveillance and retail-analytics system.

**Core principle: nothing on the dashboard is fabricated.** Every figure is an
aggregate over observations the live pipeline recorded. A metric with no
supporting observation is returned as `null` and rendered as an em dash with an
explanation -- never as `0`, and never as a plausible constant. An earlier
build of this system displayed hardcoded values (footfall pinned at 3,420,
`Math.random()` shoppers, synthetic snapshots captioned as live AI inference);
all of it has been removed.

### The live pipeline (`services/live_analytics_engine.py`)

One worker thread per enabled camera runs:

    capture frame -> (every Nth frame) detect people -> associate into tracks
      -> project foot point to floor metres -> resolve which zone that is
      -> open/close zone visits -> persist to zone_visits + customer_tracks

`services/pipeline_supervisor.py` starts this at application startup, drains the
workers' buffers into the database every 5s, and reconciles the running worker
set against the `cameras` table every 15s so adding a camera or redrawing a zone
takes effect without a restart.

### Hardware auto-detection (`services/inference_backend.py`)

The deployment machine is not the development machine, so the accelerator is
probed at runtime, never chosen at build time. Priority chain:
HailoRT NPU -> TensorRT -> CUDA -> ROCm -> OpenVINO -> CPU -> unavailable.
The final state produces **no detections at all** rather than synthetic ones.

### One coordinate system

The blueprint used to be defined three times in three incompatible coordinate
systems, none persisted. There is now exactly one, stored in `store_layouts` /
`store_zones` / `store_structures`: **real-world metres**, origin top-left,
x right, y down. Pixels exist only inside the renderer.

`store_structures` holds the purely geometric drawing of the building -- rooms,
walls, shelves, counters, doors and obstacles (`kind` = `ROOM|WALL|SHELF|COUNTER|
DOOR|OBSTACLE`; walls and doors are polylines with a `thickness_m`, the rest are
polygons). Structures carry no analytics; zones remain the regions that visits
are counted against. `GET /api/v1/layout` returns both, plus a `setup` block
(`configured`, `zones`, `structures`, `cameras`, `cameras_calibrated`) the UI
uses to decide whether to show first-run guidance.

### Store blueprint editor (`routes/layout.py`, `static/js/floorplan.js`)

Draw, move, rename, recategorise and delete rooms, walls, shelves, doors and
zones; place, aim, calibrate and remove cameras -- all persisted. Cameras enter
the system only by adopting a device that genuinely responded to a scan
(`services/camera_discovery.py`: USB V4L2, ONVIF WS-Discovery, mDNS, and a full
subnet RTSP sweep with a real RTSP OPTIONS handshake). Vendor URL grammars live
in `services/camera_drivers.py` (Dahua, Hikvision, generic ONVIF, V4L2, ESP32).
The device list and adopt form are `static/js/devices.js`; the calibration
tool is `static/js/calibration.js`.

The plan also shows live people. `GET /api/v1/layout/live` returns `persons[]`
(track id, camera, `x_m`/`y_m`, zone, age, confidence) **only for confirmed
tracks from calibrated cameras** -- nothing is placed for a camera without a
homography -- plus per-camera `detections[]` with image-pixel boxes for every
camera, calibrated or not. `GET /stream?camera_id=<id>&overlay=1` draws those
same boxes and ids onto the MJPEG feed from the worker's snapshot, with no
extra inference.

### Camera calibration

A camera without a homography detects people but cannot place them on the floor
plan, so it contributes counts but no positions and is reported as
*uncalibrated* rather than dropping its shoppers at the origin. Calibration is
four or more (image pixel, floor metre) correspondences solved by DLT.

The point pairs are persisted (`cameras.calibration_points`, with the frame
size they were clicked in) so a calibration can be reviewed or redone.
`POST /api/v1/layout/cameras/{id}/calibrate` stores points and solves;
`GET .../calibration` returns the homography, points and last known frame
size; `POST .../calibration/test` projects arbitrary image points through the
stored homography so the operator can see where they land on the plan;
`DELETE .../calibrate` clears it. Image points are in the camera's native
pixel space (`naturalWidth` x `naturalHeight`), never the displayed size.

### Honest hardware reporting (`routes/system.py`)

`GET /api/v1/system/hardware` reports `decoder_capability` (what decode
hardware the box has, from a probe) separately from `inference_backend` /
`inference_provider` (what the person detector is actually running, read from
`person_detector.status()`). `GET /api/v1/system/stats` takes `active_cameras`
from the live engine, `gpu_usage_percent` from a real `nvidia-smi` query or
`null`, and `shm_buffer_used_mb` is `null` because it is not measured. No
constants.

### First-run setup

A new install is empty: no zones, no structures, no cameras, no metrics. The
dashboard shows a data-state banner and the Blueprint tab shows an inline
"Set up your store" panel until `layout.setup.configured` becomes true. The
operator flow is:

1. Create the operator account on first visit, then sign in.
2. **Blueprint** tab -> **Store size**: floor name, width and depth in metres.
3. Draw the building with **Room** / **Wall** / **Shelf** / **Door**, then draw
   analytics **Zone**s (entrance, aisles, checkout, ...).
4. **Scan for cameras** -> **Add** the device that answered -> place its marker
   on the plan and set bearing / field of view.
5. **Calibrate**: pair four or more floor points between the camera image and
   the plan. Once the homography is stored, people detected by that camera
   appear on the plan and start feeding zone metrics.

`POST /api/v1/layout/reset` with body `{"confirm": "RESET"}` restores the
fresh state: it deletes zones, structures, cameras (stopping their workers),
discovered devices, visits, tracks, recommendations, theft incidents, shelf
interactions, planogram items and POS rows, and returns the layout to the
`DEFAULT_STORE_WIDTH_M` x `DEFAULT_STORE_HEIGHT_M` defaults named "Store
Floor". `POST /api/v1/layout/purge-seed-data` is a legacy alias that performs
the same full reset.

### Business analysis (`services/business_analysis_service.py`)

Two separated layers: deterministic threshold rules over real zone metrics
produce the findings (persisted to `ai_decision_recommendations`), and a local
Ollama model narrates them. The model never invents a finding or a number, and
a rule with no supporting signal is suppressed with an explicit reason rather
than firing on empty data.

---

## File & API Reference

### Backend Core (`edge_backend/app/`)
* **[`app/main.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/main.py):** FastAPI application entrypoint, static file mounts, `/dashboard` and `/dashboard/analytics` (both serve `index.html`; the separate analytics page was removed), `/dashboard/studio` (Studio sub-page), `/stream?camera_id=&fps=&overlay=1` (live MJPEG of real frames, optional tracker overlay), and API router registrations.
* **[`app/config.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/config.py):** Settings, storage paths, retention policies, JWT keys, go2rtc URLs, and hardware device paths with graceful local fallback directory resolution.
* **[`app/models/schemas.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/models/schemas.py):** Pydantic schemas for `ZoneConfig`, `Point2D`, `TripwireDirection`, `MaskMode`, `BoundingBox`, `Keypoint`, `CameraFeed`, `CameraFeatureConfig`, `HardwareProfile`, `SystemStats`, and `SecurityEvent`.
* **[`app/services/hardware_detector.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/hardware_detector.py):** Runtime probe for decode capability (NVIDIA NVDEC, Intel VA-API, AMD Mesa, CPU SIMD) and RAM; the inference fields are read from the detector that is actually loaded, not guessed.
* **[`app/services/feature_manager.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/feature_manager.py):** In-memory thread-safe feature flags manager with hot-reload support.
* **[`app/services/ai_zone_service.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/ai_zone_service.py):** Raycasting polygon geometry, tripwire line crossing with multi-point spine tracking, privacy masking, and persistence to `storage/zones_config.json`.
* **[`app/services/inference_backend.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/inference_backend.py):** Person detector with runtime backend auto-detection (HailoRT -> TensorRT -> CUDA -> ROCm -> OpenVINO -> CPU -> unavailable); `person_detector.status()` is what `/system/hardware` reports.
* **[`app/services/live_analytics_engine.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/live_analytics_engine.py):** One `CameraWorker` thread per enabled camera (capture -> detect -> track -> project -> zone visits), thread-safe per-camera snapshots for `/layout/live`, and `render_overlay()` for `/stream?overlay=1`.
* **[`app/services/pipeline_supervisor.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/pipeline_supervisor.py):** Starts the engine at startup, drains worker buffers to SQLite every 5s, reconciles workers against the `cameras` table every 15s.
* **[`app/services/tracking_service.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/tracking_service.py):** `CentroidTracker` (IoU association) and `FloorProjector` (foot point -> metres via the camera homography; declines when uncalibrated).
* **[`app/services/store_layout_service.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/store_layout_service.py):** Single source of truth for the blueprint: layout, zones and structures in metres, polygon validation and clamping, serialisation with the `setup` block.
* **[`app/services/camera_discovery.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/camera_discovery.py):** Device scan (USB V4L2, ONVIF WS-Discovery, mDNS, subnet RTSP sweep with a real OPTIONS handshake); only devices that answered are listed.
* **[`app/services/camera_drivers.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/camera_drivers.py):** Vendor stream-URL grammars (Dahua, Hikvision, generic ONVIF, V4L2, ESP32).
* **[`app/services/retail_metrics_service.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/retail_metrics_service.py):** Zone metrics, funnels, queues, heatmaps and the hourly forecast, all aggregated from `zone_visits` / `customer_tracks` / `pos_transactions`; returns `null` (never 0 or a constant) when nothing was observed.
* **[`app/services/business_analysis_service.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/business_analysis_service.py):** Deterministic threshold rules over real zone metrics, persisted to `ai_decision_recommendations`, optionally narrated by a local Ollama model that never invents a figure.
* **[`app/services/shelf_interaction_service.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/shelf_interaction_service.py):** Product shelf mapping and hand-to-shelf tracking engine (17-keypoint skeleton wrist tracking, APPROACH -> REACH_IN -> DWELL_INSPECT -> ITEM_PICK vs PUT_BACK state machine, study metrics options, and persistent storage in `storage/shelf_products_config.json`).
* **[`tests/test_shelf_interaction.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/tests/test_shelf_interaction.py):** Unit tests for product shelf zone CRUD, wrist pose entry/dwell/exit state machine, and friction calculations.
* **[`tests/test_layout_api.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/tests/test_layout_api.py):** Layout `setup` block and structures, structure CRUD and validation, `/layout/live` shape, `calibrated` flag in pipeline status, calibration round trip (`/calibrate`, `/calibration`, `/calibration/test`), and `/reset` confirmation + fresh state.
* **[`tests/test_live_engine.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/tests/test_live_engine.py):** Worker runtime records frame size and flags; live snapshot is empty with no cameras; uncalibrated cameras report boxes but no persons; calibrated cameras place confirmed tracks on the floor; lost tracks drop out; `render_overlay` draws on a copy.
* **[`tests/test_camera_helpers.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/tests/test_camera_helpers.py):** `/cameras/{id}/live-status`, `actions/snapshot` (409 without a frame, saves the real frame), `actions/clip` (501 without a real buffer), legacy fabricated routes removed, no fallback fleet, system stats/hardware without constants, feature manager and shelf service start empty.
* **[`tests/test_market_routes.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/tests/test_market_routes.py):** Asserts the deleted fabricating modules cannot be imported and that `/analytics/market/*` reports `sufficient_history` / `days_observed` instead of a synthetic forecast.
* **[`app/routes/cameras.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/routes/cameras.py):** Camera CRUD backed only by the `cameras` table (no fallback fleet), `/{id}/snapshot` (real frame, `X-Frame-Source: live|no-signal`), `/{id}/live-status` (this camera's pipeline entry + `calibrated`, frame size), `POST /{id}/actions/snapshot` (saves the current real frame, 409 if none), `POST /{id}/actions/clip` (501 unless a real buffered clip can be produced), `/scan`, and feature toggle endpoints.
* **[`app/routes/analytics.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/routes/analytics.py):** Retail analytics endpoints over recorded observations: overview KPIs, `/floorplan` (zones with metrics + coverage), `/heatmaps`, funnels, queues, decisions/actions, daily digest, POS ingest, product shelf ROI mapping (`/products/zones`), hand interactions (`/products/interactions`), `/market/predictions` (per-hour mean of this store's own recorded days; `sufficient_history: false` until enough days exist), `/market/llm-status` (Ollama status verified by a real generation), `/market/llm-optimize` (runs the business analysis and narrates it), and `/business/*`.
* **[`app/routes/layout.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/routes/layout.py):** Store blueprint API: `GET/PUT /api/v1/layout` (with `structures[]` and `setup`), zones CRUD, structures CRUD (`/structures`), camera placement, calibration (`/calibrate`, `/calibration`, `/calibration/test`), device discovery/adopt (`/discover`, `/devices`, `/devices/adopt`), `/pipeline/status`, `/live` (live person positions from calibrated cameras only), and `POST /reset` (`{"confirm":"RESET"}`) which restores the fresh state.
* **[`app/routes/zones.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/routes/zones.py):** REST endpoints for adding, listing, and deleting tripwires, intrusion polygons, and exclusion masks.
* **[`app/routes/system.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/routes/system.py):** `/api/v1/system/hardware` (probed `decoder_capability` + the inference backend the detector really runs) and `/api/v1/system/stats` (CPU, real GPU utilisation or `null`, RAM, `active_cameras` from the live engine, `shm_buffer_used_mb` = `null`).

### Web Studio & Dashboard (`edge_backend/app/static/`)
* **[`app/static/index.html`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/index.html):** The only dashboard page (served at `/dashboard` and `/dashboard/analytics`): Live Camera Matrix (built from the cameras actually registered), Store Blueprint editor with live person map, Retail Analytics & Funnels, AI Action Center, Market forecast, Loss Prevention, and Daily Digest. Shows a data-state banner and the Blueprint "Set up your store" panel until `layout.setup.configured` is true.
* **[`app/static/studio.html`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/studio.html):** Interactive Canvas Studio sub-page with Tripwires, Restricted Intrusion Polygons, Privacy Masks, and interactive Product Shelf Area mapping modal (linking SKU, Category, Price, Shelf Tier, and study options).
* **[`app/static/js/floorplan.js`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/js/floorplan.js):** Canvas blueprint editor in metres: draw/move/edit rooms, walls, shelves, doors and zones (snap to 0.5 m), place and aim cameras, render the live person map from `/api/v1/layout/live`, zone inspector, and the first-run "Set up your store" panel. Loads `selftest.js` only when the URL carries `?__fptest=1`.
* **[`app/static/js/analytics.js`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/js/analytics.js):** Dashboard controller: auth-aware polling, header telemetry and data-state banner from `/api/v1/layout` `setup`, camera matrix built from registered cameras with department chips, Chart.js analytics, actions, market forecast (renders `sufficient_history: false` honestly), theft and digest tabs. Opt-in in-page self test with `?__selftest=1`.
* **[`app/static/js/studio.js`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/js/studio.js):** Interactive canvas drawing with 4+ point Product Shelf ROI mode, product modal configuration, and live zone REST client. Opt-in self test with `?__selftest=1`.
* **[`app/static/js/devices.js`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/js/devices.js):** Device manager: Scan for cameras, list of devices that actually answered, inline Add form (credentials, channel, sub/main stream, placement), remove camera.
* **[`app/static/js/calibration.js`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/js/calibration.js):** Camera-to-floor calibration tool: pair 4+ points between the live frame (native pixel space) and the plan, solve via `/calibrate`, reproject via `/calibration/test` to check the result, clear via `DELETE /calibrate`.
* **[`app/static/js/auth.js`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/js/auth.js):** Sign-in gate loaded first: wraps `fetch` to attach the bearer token, shows the first-run account form or sign-in form, re-opens it on 401.
* **[`app/static/js/selftest.js`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/js/selftest.js):** In-page blueprint self test loaded only with `?__fptest=1`: drives the real canvas with mouse events, checks server state, prints PASS/FAIL lines into `#selftestLog` for a headless `--dump-dom` run, and deletes everything it created.
* **[`app/static/css/floorplan.css`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/css/floorplan.css):** Styles for the blueprint editor, live person map, device manager and calibration tool (loaded after `style.css`).
* **[`app/static/css/style.css`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/css/style.css):** Glassmorphism cyber-HUD stylesheet with full mobile (<768px), tablet (768-1024px), desktop (>1024px), and print media query support.

### Mobile Client (`mobile_app/lib/`)
* **`lib/screens/dashboard_screen.dart`:** Multi-camera live grid view.
* **`lib/screens/zone_editor_screen.dart`:** Touchscreen zone and tripwire canvas editor.
* **`lib/screens/camera_settings_screen.dart`:** Granular AI feature toggle switches.
* **`lib/screens/emergency_alert_screen.dart`:** Fullscreen takeover siren during critical emergencies.
* **`android/.../MainActivity.kt` & `ios/.../AppDelegate.swift`:** Native Android `USAGE_ALARM` notification channel and iOS `criticalAlert` entitlement handler.

### Deployment & Integration Specifications (`docs/`)
* **[`docs/supermarket_cctv/supermarket_cctv_deployment_spec.md`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/docs/supermarket_cctv/supermarket_cctv_deployment_spec.md):** Complete hardware, network, port mapping (UPnP), and Dahua P2P cloud specification for the Pearcedale Supermarket CCTV System (Australia) including RTSP streaming endpoints and go2rtc integration.
* **[`docs/supermarket_cctv/hardware_sizing_and_procurement_guide_30_cameras.md`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/docs/supermarket_cctv/hardware_sizing_and_procurement_guide_30_cameras.md):** Complete hardware sizing, workload throughput mathematics, multi-model TensorRT VRAM sizing, itemized Bill of Materials (BOM), and procurement options for 32-camera supermarket installations.
* **[`docs/supermarket_cctv/Supermarket_Edge_AI_CCTV_Hardware_and_Services_Guide.pdf`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/docs/supermarket_cctv/Supermarket_Edge_AI_CCTV_Hardware_and_Services_Guide.pdf):** Print-ready formal PDF document (5 pages, A4) detailing 32-camera deterministic mathematical sizing, single NVDEC decode throughput, 10-Pillar Foolproof Engineering Validation Matrix, itemized BOM, 32-channel store layout, and procurement checklist.
* **[`docs/supermarket_cctv/Supermarket_Edge_AI_CCTV_Hardware_and_Services_Guide.html`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/docs/supermarket_cctv/Supermarket_Edge_AI_CCTV_Hardware_and_Services_Guide.html):** Paged-media HTML source for regenerating the 32-camera PDF specification document.
