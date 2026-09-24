# Edge AI CCTV Architecture & File Index Map

## High-Level Architecture Overview

The Edge AI CCTV System is an end-to-end, decentralised, 100% on-premise AI
system for **supermarkets only**.

**Product scope (fixed).** Three things: (1) theft / loss prevention,
(2) market analysis (zone metrics, funnels, queues, forecasts, business
analysis) and (3) customer heatmaps. Home-security features -- fall detection,
kinematic/emergency alerts, the emergency siren screen, residential intrusion
alarms and door monitoring -- were removed on purpose and **must not be
reintroduced**. Retail tripwires (entrance in/out counting) and scheduled
restricted areas (stockroom, cash office, after-hours floor) are in scope and
are evaluated live by `tripwire_engine.py`. Legacy keys such as
`fall_detection` / `door_monitoring` in stored camera rows are ignored
(`CameraFeatureConfig` is `extra="ignore"`), and migration m0004 dropped
`security_events.kinematics`.

**Core principle: nothing on the dashboard is fabricated.** Every figure is an
aggregate over observations the live pipeline recorded. A metric with no
supporting observation is returned as `null` and rendered as an em dash with an
explanation -- never as `0`, and never as a plausible constant. An earlier
build of this system displayed hardcoded values (footfall pinned at 3,420,
`Math.random()` shoppers, synthetic snapshots captioned as live AI inference);
all of it has been removed.

### The live pipeline (`services/live_analytics_engine.py`)

One worker thread per enabled camera runs:

    capture frame -> (every Nth frame) pose-estimate people (box + 17 COCO
      keypoints) [+ every OBJECT_DETECT_EVERY_N frames: retail objects]
      -> drop detections whose foot point is in an AI_IGNORE mask
      -> ByteTrack association -> project foot point to floor metres
      -> resolve zone -> open/close zone visits
      -> pose_analytics.observe(tracks + skeletons, bags, privacy-masked frame)
      -> persist to zone_visits + customer_tracks

Per-camera feature flags (`CameraFeatureConfig` in `models/schemas.py`,
durable in `cameras.features`, cached by `services/feature_manager.py`):
`people_counting` (zone visits/footfall persisted), `shelf_interaction`,
`theft_detection`. The engine reads them per frame via `camera_flag()`
(unreadable store -> treated as on); pose_analytics sets
`interactions_on` / `theft_on` from them.

`services/pipeline_supervisor.py` starts this at application startup, drains the
workers' buffers into the database every 5s, and reconciles the running worker
set against the `cameras` table every 15s so adding a camera or redrawing a zone
takes effect without a restart.

### Inference and hardware auto-detection (`services/inference_backend.py`)

Models are Ultralytics **YOLO26** ONNX exports in `edge_backend/models/`,
listed in `models/manifest.json` (file, sha256, size, task, required, ONNX IO
signature, exporter pin, AGPL-3.0 licence note):

* `yolo26s-pose.onnx` -- person + 17 keypoints, default on accelerator EPs
  (`POSE_MODEL_GPU`);
* `yolo26n-pose.onnx` -- small pose model, default on CPU / OpenVINO
  (`POSE_MODEL_CPU`); the other one is the fallback candidate;
* `yolo26n.onnx` -- optional 80-class COCO object model (bags, phones) used as
  theft context; loaded on the pose model's provider, then CPU
  (`OBJECT_MODEL_PATH`, `OBJECT_CLASSES`, disabled if `OBJECT_DETECT_EVERY_N <= 0`);
* `yolo26m-pose-544x960.onnx`, `yolo26s-pose-544x960.onnx` -- optional larger /
  16:9 higher-resolution pose exports forming the GPU **model ladder**
  (`POSE_MODEL_LADDER_GPU`, most accurate first; `POSE_MODEL_LADDER_CPU` on CPU);
* `rtmpose-s-256x192.onnx` -- optional **top-down keypoint refiner** (RTMPose-s,
  Apache-2.0, downloaded by `fetch_models.py` from the OpenMMLab release zip).

**Measured model/resolution choice** (`_fit_to_budget`, after warm-up): the
detector walks the ladder on the chosen provider and keeps the first model
whose steady latency fits `latency_budget()` = `POSE_LATENCY_BUDGET_MS`, or by
default `1000 x POSE_BUDGET_UTILISATION / (enabled analytics cameras x
RECORDING_FPS / ANALYTICS_DETECT_EVERY_N_FRAMES)` clamped to 5..150 ms (cameras
counted read-only from the DB; `POSE_BUDGET_STREAMS` overrides). Nothing fits
-> fastest measured. `POSE_MODEL_PATH` pins a model. The refiner
(`POSE_REFINER=auto|on|off`) runs on each person crop after the pose model
(top `POSE_REFINER_MAX_PERSONS`), visibility = max(pose vis, RTMPose score x
`POSE_REFINER_SCORE_SCALE`); `auto` = accelerator only and only when a batch of
`POSE_REFINER_BUDGET_PERSONS` crops costs <= half the budget (its cost is then
subtracted from the ladder budget). Both decisions are in
`status()["model_selection"]` / `["keypoint_refiner"]`. Plausibility gate:
a box wider than `PERSON_MIN_ASPECT_RATIO` passes only with keypoint evidence
(`_has_coherent_torso`: torso with shoulders above hips, **or** head above the
shoulders + both shoulders + an elbow -- shoppers reaching to a shelf with
hips hidden).

`scripts/fetch_models.py` restores/exports missing models; a re-export is
accepted when its IO signature matches (hash recorded in the gitignored
`models/.local_exports.json`).

`PersonDetector.initialise()` (exposed as `initialise_inference()`, called
once from the lifespan; `detect()` also triggers it lazily as a safety net)
walks `_PROVIDER_PRIORITY`: HailoRT is probed and reported honestly but never
selected (no HEF runner in this build) -> TensorRT (fp16, engine cache under
storage) -> CUDA -> ROCm -> MIGraphX -> OpenVINO -> DirectML -> CoreML -> CPU
-> **unavailable**. Providers missing from the ORT build or listed in
`INFERENCE_DISABLED_PROVIDERS` are skipped. **Verification:** a provider only
counts when `session.get_providers()[0]` equals it -- ORT silently falls back
(e.g. TensorRT -> CUDA without libnvinfer), so the captured ORT banner and the
reason are recorded per provider in `status()["provider_attempts"]`. The
chosen session is warmed up (`INFERENCE_WARMUP_RUNS`) before any camera
starts. Unavailable produces **no detections at all**, never synthetic ones.

### Tracking (`services/tracking_service.py`)

`ByteTracker` (also exported as `CentroidTracker`): ByteTrack-style two-stage
association with a constant-velocity Kalman filter per track; high-confidence
detections match first, leftover tracks get a second pass against
low-confidence ones (low-confidence boxes can extend but never start a track).
Hungarian assignment when scipy imports, greedy IoU otherwise. Each `Track`
carries the latest pose keypoints (`keypoints_fresh`), smoothed by a
velocity-aware EMA (`smooth_keypoints`): `TRACK_KEYPOINT_EMA` (0.6) for
head/torso/legs, `TRACK_KEYPOINT_EMA_ARMS` (0.8) for elbows/wrists, fading to
no smoothing between `TRACK_KEYPOINT_JITTER_FRAC` and `TRACK_KEYPOINT_FAST_FRAC`
of the box height moved per detection frame, so a reach is never delayed;
a joint not visible now is never carried forward.
`FloorProjector` maps foot points to metres through the camera homography and
declines when uncalibrated.

### Pose analytics and loss prevention (`services/pose_analytics.py`, `services/theft_detection_service.py`)

`pose_analytics.observe()` runs on the camera worker thread (cheap arithmetic
on bounded per-track state); all I/O (JPEGs, DB writes, notifications) is
queued to a background writer thread. `pose_analytics.start()` binds the app
event loop in the lifespan so alerts go out immediately.

* **Shelf interactions:** a hand inside a product-zone polygon (normalised
  image coords, drawn in Studio) for `INTERACTION_MIN_FRAMES` and
  `INTERACTION_MIN_SEC`. The point tested first is the *hand tip* (wrist +
  `INTERACTION_HAND_EXTEND_FRAC` x elbow->wrist; needs a visible elbow), then
  the wrist. One zone per hand (active zone kept, else deepest penetration);
  rest box ignored for an extended arm; no reach while the hips move faster
  than `INTERACTION_MAX_BODY_SPEED`; occluded hands hold a reach for
  `INTERACTION_OCCLUSION_GRACE_SEC`; re-entry within
  `INTERACTION_REENTRY_MERGE_SEC` continues the same reach. Each reach is one
  `shelf_interactions` row with SKU, category, shelf level, value tier and
  contact point copied at reach time (m0010). Optional fallback to floor-plan
  SHELF zones through the homography (`INTERACTION_FLOOR_SHELF_FALLBACK`,
  approximate). Ground truth: `tests/test_reach_mapping.py`.
* **Rules** (pure functions in `theft_detection_service.py`, stored as
  `theft_incidents.rule`): `CONCEALMENT` (wrist leaves shelf -> waistband/pocket
  band from shoulder/hip keypoints or a detected bag, holds, no return),
  `SHELF_SWEEPING` (many reaches into one zone in a window),
  `SUSPICIOUS_LOITERING` (long dwell at a high-value zone + reaches + head
  turns from head yaw), `EXIT_WITHOUT_CHECKOUT` (interacted, then
  ENTRANCE/EXIT zone without CHECKOUT; `THEFT_EXIT_RULE_ENABLED`),
  `SWEETHEARTING` (POS-linked; returns `evaluable: False` without POS data and
  is not wired into the live pipeline). Thresholds are `THEFT_*` in `config.py`.
* **Confidence is never a constant:** `evidence_confidence(visibility, terms)`
  = mean keypoint visibility of the joints used x mean of 0..1 strength terms
  (duration/count via `saturating(v, ref) = 1 - exp(-v/ref)`, signal
  agreement). Incidents below `THEFT_MIN_CONFIDENCE` or within
  `THEFT_INCIDENT_COOLDOWN_SEC` per rule/track are dropped. Every result is
  "suspicious behaviour for staff review" with evidence bullets, never a verdict.
* **Evidence image:** `render_evidence()` draws box + skeleton on the
  privacy-masked frame, adds the person crop and a caption band, writes
  `<THEFT_EVIDENCE_DIR or storage/theft_evidence>/<incident_id>.jpg`, served at
  `GET /api/v1/theft/incidents/{id}/evidence`.
* **Notification:** `notification_service.notify_loss_prevention(title, body,
  data)` (logs a security event, websocket broadcast, device push) scheduled
  onto the bound loop with a 15 s timeout.
* Routes (`routes/theft.py`, `/api/v1/theft`): incidents list/detail/evidence,
  statistics, acknowledge / dispatch / resolve.

### Privacy masks (`services/privacy_mask.py`)

Per-camera `exclusion_masks` from `ai_zone_service` (drawn in Studio, served by
`routes/zones.py`, points normalised 0..1). `MaskMode`:
`BLUR` / `MOSAIC` / `BLACKOUT` / `COLOR` are privacy masks burned into every
frame shown or saved (MJPEG, overlay, calibration feed, snapshots, clip
pre-buffer, theft evidence) while the detector still sees the raw frame;
unknown mode -> BLACKOUT, and a mask that fails to apply blacks out the whole
frame (fail closed). `AI_IGNORE` leaves the picture alone but drops any
detection whose foot point is inside it before tracking. Not covered: video
played directly from the camera via go2rtc/WebRTC.

### Heatmaps (`services/retail_metrics_service.py`, `GET /api/v1/analytics/heatmaps`)

`HEATMAP_KINDS = ("presence", "dwell", "interaction")`: presence = count per
recorded floor point; dwell = seconds per cell (gaps >
`HEATMAP_DWELL_MAX_GAP_SEC` not credited); interaction = pose shelf
interactions binned at the shopper's floor position (calibrated cameras only).
Normalised 0-1 with `peak_value` / `unit`; with nothing observed the matrix is
omitted. Optional `from`/`to` and `presence_weighting=tracks` (visitors per
cell). Binning is the shared `bin_heatmap_paths` / `bin_heatmap_points`.

**Recorded history** (`services/heatmap_history.py`, table `heatmap_snapshots`,
m0013): `heatmap_recorder` (asyncio task from the lifespan, stopped before the
pipeline) records every store-local hour `HEATMAP_SETTLE_SEC` after it closes
(re-recording the two before it), rolls hours up into days (1440) after local
midnight, backfills `HEATMAP_BACKFILL_DAYS` from rows on startup, and prunes
(`HEATMAP_HOURLY_RETENTION_DAYS` / `HEATMAP_DAILY_RETENTION_DAYS`). Spaces:
`floor` (0.5 m cells, calibrated `x/y`) and `image` per camera (64x36,
normalised foot point `u/v` the engine samples into `Track.path_points` every
`TRAJECTORY_SAMPLE_SEC`, plus interaction contact points), so uncalibrated
cameras get heatmaps. Presence is track-weighted (visitors per cell), dwell
time-weighted, interaction per reach. Cells: base64(zlib(uint16 LE)) x `scale`.
Empty hours are stored only with measured `uptime_seconds` (camera off = no
row). API (`/api/v1/analytics/heatmaps/`): `history`, `snapshot/{id}`,
`aggregate`, `compare`, `hour-profile`, `POST record-now`.
`business_analysis_service.heatmap_trends` adds HEATMAP_DEAD_SPACE,
HEATMAP_HOTSPOT_SHIFT, HEATMAP_CONGESTION and HEATMAP_BROWSE_NO_TOUCH findings
citing snapshot ids and periods (else "not enough recorded history") and a
compact summary for the Ollama narration. Tests: `tests/test_heatmap_history.py`.

### Camera roles (`services/camera_roles.py`, `routes/camera_roles.py`, m0012)

`ROLE_PRESETS` (entrance, exit, entrance_exit, checkout, aisle, high_value,
stockroom, overview): label, mounting tip, default flags + person-size gate,
`theft_sensitivity`, `required_setup` / `optional_setup` checklist ids.
`cameras.role` / `cameras.pos_register_id` (m0012). APIs: `GET /api/v1/camera-roles`,
`PUT /api/v1/cameras/{id}/role` (`apply_defaults`), `GET .../setup` (items computed
from real config), `PUT .../pos-register`, `GET /api/v1/store/setup` (analytics
available/limited/blocked + score), `GET /api/v1/store/pos-registers`.
Behaviour: door-camera tripwires preferred for footfall, stockroom never footfall,
theft thresholds scaled per role (pose_analytics, 5 s role cache), high_value =
all zones high value + HIGH severity, EXIT_WITHOUT_CHECKOUT suppressed on
product-area cameras when checkout cameras exist (single-camera rule, no re-id).
Studio queue areas (`/api/zones/queue`, kind checkout|queue, image space) are
evaluated by `tripwire_engine` into `queue_visits` and reported by
`/analytics/queues` with POS register attribution. No role = old behaviour.

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
constants. `GET /api/v1/system/preflight[?probe=true]` re-runs the read-only
preflight (probe = real ORT session in a child process) and folds in the
loaded detector's live provider. All `/system` routes require auth.

### Startup (`run.sh`, `scripts/bootstrap.py`, `services/preflight.py`, `app/main.py`)

* `./run.sh` (repo root) picks the venv (`EDGE_VENV`, else `.venv_test`, else
  `.venv`) and execs `edge_backend/scripts/bootstrap.py` (stdlib only).
  Bootstrap **fixes then verifies**, idempotently, never using sudo: venv ->
  installer (uv or pip) -> requirements (ORT lines excluded) -> accelerator
  probe (nvidia-smi, `/dev/nvidia*`, `/dev/kfd`, `/dev/dri`, `/dev/hailo0`)
  -> onnxruntime flavour (`gpu`/`cpu`/`openvino`, conflicting ones removed)
  -> models via `fetch_models.py` -> real ORT session, checks
  `get_providers()[0]` -> preflight -> exec uvicorn. Flags: `--check-only`,
  `--ort`, `--dev`, `--skip-models`, `--force`, `--port`, `-- <uvicorn args>`.
  System-level fixes (e.g. NVIDIA driver) are printed, not run.
* `services/preflight.py` is read-only: requirements import, ORT distribution
  conflicts / providers / GPU-but-CPU-only, models vs `manifest.json`
  (size + sha256), writable storage. Each issue carries its fix command.
  Standalone: `python -m app.services.preflight [--json]`.
* **Lifespan order** (`app/main.py`): log redaction installed at import ->
  deployment-safety warnings (`AUTH_DISABLED`, weak/unsaved secrets) ->
  `preflight.run_preflight()` (never blocks) -> `init_db()` (migrations) ->
  `_announce_setup_code()` (first run only) -> `initialise_inference()` (live
  provider folded into the preflight report) -> `pipeline_supervisor.start()`
  -> `pose_analytics.start()` -> `remote_access_service.start()`. Shutdown stops the pipeline, then pose_analytics, then the tunnel.
  `services/shutdown_signal.py` chains a SIGINT/SIGTERM handler (installed at
  lifespan start) that sets `shutdown_requested`; the endless `/stream` MJPEG
  generator polls it and ends, because uvicorn runs the lifespan shutdown only
  after all connections close (it used to hang until SIGKILL). The launchers
  also pass `--timeout-graceful-shutdown 5` as a backstop, and
  `live_engine.stop_all(timeout)` joins the camera workers (bounded) off the
  loop so their closed tracks reach the final flush.

### DB migrations (`app/migrations/`)

`init_db()` (`app/database.py`) calls `run_migrations(DATABASE_PATH)`
(`runner.py`): exclusive `<db>.migrate.lock`, refuses a DB migrated by newer
code (`SchemaTooNewError`), backs up any DB holding data via `BackupService`
(`storage/backups/*pre-v*.db`) before applying, runs each pending
`mNNNN_<name>.py` `upgrade(conn)` in its own transaction (FKs off,
`foreign_key_check` before commit), records version/checksum in
`schema_migrations`, then runs `reconcile.py` (additive only: missing
tables/columns/indexes from SQLAlchemy metadata; drift is reported, never
dropped/retyped). `rebuild.py` `rebuild_table()` = SQLite 12-step rebuild for
what ALTER cannot do. Migrations: m0001 baseline (frozen DDL), m0002 legacy
ad-hoc columns, m0003 pose/theft/interaction columns, m0004 drop
`security_events.kinematics`, m0005 reset `setup_completed` with no admin.
CLI: `python -m app.migrations status|upgrade|check [--db PATH] [--json]`.
Never edit a shipped migration.

### Auth (`services/auth_service.py`, `routes/setup.py`, `services/setup_service.py`)

* **First-run setup code:** while no active admin exists, a one-time
  `XXXX-XXXX` code (unambiguous alphabet) is printed to the server log and
  written to `<STORAGE_DIR>/setup_code.txt` (0600). `POST
  /api/v1/setup/admin` requires it (`setup_code` or `X-Setup-Code`, compared
  constant-time, failures count toward IP lockout), creates the owner, and
  invalidates the code.
* `AUTH_DISABLED=true` is the **only** auth bypass (loud startup warning; dev
  only). `DEBUG` only raises log verbosity and never touches auth.
* `scripts/manage_operator.py` (run from `edge_backend/` with the app's
  Python): `list`, `create`, `reset-password`, `reset-setup` (backs up the DB,
  issues a new setup code the running server accepts). Resets sign out every
  session. `--db PATH` targets another database.

### Secrets (`services/secret_store.py`, `services/nvr_credential_service.py`, `services/log_redaction.py`)

* `JWT_SECRET`, `AUTH_SECRET_KEY`, `INTERNAL_SERVICE_KEY`, `COTURN_SECRET`,
  `NVR_CREDENTIAL_KEY` resolve env -> `edge_backend/.env` ->
  `<STORAGE_DIR>/secrets/device_secrets.json` (dir 0700, file 0600). Empty,
  placeholder (`CHANGE_ME`, `<...>`) or legacy public defaults count as unset;
  missing ones are generated per machine (`secrets.token_urlsafe(64)`).
* NVR passwords in `storage/nvr_credentials.json` (0600) are Fernet-encrypted
  as `enc:v1:<token>` with `NVR_CREDENTIAL_KEY`; legacy plaintext is
  re-encrypted on first read.
* `install_log_redaction()` scrubs JWTs, `?token=`-style query secrets,
  Authorization headers, API keys and `rtsp://user:pass@` from every log
  record (including `uvicorn.access`).
* Gitignored: `.env*` (except `.env.example`), everything under `storage/` and
  `edge_backend/storage/` except `backups/.gitkeep` (secrets, setup code, NVR
  credentials, DBs, theft evidence, TRT cache, logs), `models/.local_exports.json`,
  certs/keys, mobile signing and push credentials.

### First-run setup

A new install is empty: no zones, no structures, no cameras, no metrics. The
dashboard shows a data-state banner and the Blueprint tab shows an inline
"Set up your store" panel until `layout.setup.configured` becomes true. The
operator flow is:

1. Create the operator account on first visit with the one-time setup code
   from the server log / `storage/setup_code.txt` (see Auth), then sign in.
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
* **[`app/main.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/main.py):** FastAPI entrypoint: installs log redaction at import, lifespan (safety warnings -> preflight -> `init_db` -> setup code -> `initialise_inference` -> pipeline -> `pose_analytics.start`), static mounts, `/dashboard` and `/dashboard/analytics` (both serve `index.html`), `/dashboard/studio` (Camera Studio), `/stream?camera_id=&fps=&overlay=1` (live MJPEG of real, privacy-masked frames; a NO SIGNAL slate otherwise), and router registrations.
* **[`app/config.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/config.py):** Settings, storage paths, retention policies, JWT keys, go2rtc URLs, and hardware device paths with graceful local fallback directory resolution.
* **[`app/models/schemas.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/models/schemas.py):** Pydantic schemas incl. `EventType` (loss-prevention types only: THEFT_SUSPECTED, CONCEALMENT, SHELF_SWEEP, EXIT_WITHOUT_CHECKOUT, LOITERING, QUEUE_ALERT, CAMERA_OFFLINE), `MaskMode` (BLACKOUT/BLUR/MOSAIC/COLOR/AI_IGNORE), `CameraFeatureConfig` (`people_counting`, `shelf_interaction`, `theft_detection`; `extra="ignore"`), `ZoneConfig`, `Keypoint`, `HardwareProfile`, `SystemStats`, `SecurityEvent`, camera/DVR/storage models.
* **[`app/services/hardware_detector.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/hardware_detector.py):** Runtime probe for decode capability (NVIDIA NVDEC, Intel VA-API, AMD Mesa, CPU SIMD) and RAM; the inference fields are read from the detector that is actually loaded, not guessed.
* **[`app/services/feature_manager.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/feature_manager.py):** Thread-safe in-process cache of per-camera feature flags; durable copy is `cameras.features`, hydrated on first API read. Starts empty.
* **[`app/services/ai_zone_service.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/ai_zone_service.py):** Per-camera zone store in `storage/zones_config.json`: `exclusion_masks` (privacy masks, applied by `privacy_mask.py`), plus tripwires and restricted areas (image-normalised 0..1 per camera) evaluated live by `app/services/tripwire_engine.py`.
* **[`app/services/inference_backend.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/inference_backend.py):** YOLO26 pose (+ optional yolo26n object) detector with runtime provider probe verified by `get_providers()[0]`; `initialise_inference()` from the lifespan; `person_detector.status()` (provider attempts, warm-up, model) is what `/system/hardware` and preflight report.
* **[`app/services/live_analytics_engine.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/live_analytics_engine.py):** One `CameraWorker` thread per enabled camera (capture -> pose detect -> AI_IGNORE filter -> ByteTrack -> project -> zone visits -> `pose_analytics.observe`), per-camera feature flags (`camera_flag`), thread-safe snapshots for `/layout/live`, and `render_overlay()` for `/stream?overlay=1`.
* **[`app/services/pipeline_supervisor.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/pipeline_supervisor.py):** Starts the engine at startup, drains worker buffers to SQLite every 5s, reconciles workers against the `cameras` table every 15s.
* **[`app/services/tracking_service.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/tracking_service.py):** `ByteTracker` (alias `CentroidTracker`; Kalman + two-stage ByteTrack association, tracks carry smoothed keypoints) and `FloorProjector` (foot point -> metres via the camera homography; declines when uncalibrated).
* **[`app/services/store_layout_service.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/store_layout_service.py):** Single source of truth for the blueprint: layout, zones and structures in metres, polygon validation and clamping, serialisation with the `setup` block.
* **[`app/services/camera_discovery.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/camera_discovery.py):** Device scan (USB V4L2, ONVIF WS-Discovery, mDNS, subnet RTSP sweep with a real OPTIONS handshake); only devices that answered are listed.
* **[`app/services/camera_drivers.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/camera_drivers.py):** Vendor stream-URL grammars (Dahua, Hikvision, generic ONVIF, V4L2, ESP32).
* **[`app/services/retail_metrics_service.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/retail_metrics_service.py):** Zone metrics, funnels, queues, heatmaps and the hourly forecast, all aggregated from `zone_visits` / `customer_tracks` / `pos_transactions`; returns `null` (never 0 or a constant) when nothing was observed.
* **[`app/services/business_analysis_service.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/business_analysis_service.py):** Deterministic threshold rules over real zone metrics, persisted to `ai_decision_recommendations`, optionally narrated by a local Ollama model that never invents a figure.
* **[`app/services/shelf_interaction_service.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/shelf_interaction_service.py):** Product shelf zones (normalised image coords, `storage/shelf_products_config.json`) with operator-set or derived shelf level (TOP/MIDDLE/BOTTOM from the polygon's position in its shelf unit) and value tier; `product_summary()` aggregates per-product / per-level reaches from `shelf_interactions` rows (no in-memory counters) plus POS units per SKU (conversion only with POS rows, else "needs POS data"). Served by `/analytics/products/summary` and `/products/{id}/stats`; rendered by `js/product_reach.js` on the Analytics tab. `process_person_pose` backs the diagnostic `POST /analytics/products/interactions` and records nothing.
* **[`app/services/camera_roles.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/camera_roles.py):** Camera role presets, role cache, scaled theft thresholds, exit-rule gate, per-camera setup checklist and store coverage (`tests/test_camera_roles.py`).
* **[`app/services/pose_analytics.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/pose_analytics.py):** Per-(camera, track) pose state -> shelf interactions and loss-prevention incidents; background writer for `shelf_interactions` / `theft_incidents`, `render_evidence()` JPEGs, `notify_loss_prevention` dispatch.
* **[`app/services/theft_detection_service.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/theft_detection_service.py):** Pure rule functions (concealment, shelf sweeping, loitering, exit without checkout, POS-only sweethearting), `evidence_confidence` / `saturating`, and the incident lifecycle service.
* **[`app/services/privacy_mask.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/privacy_mask.py):** `apply_privacy_masks()` for BLUR/MOSAIC/BLACKOUT/COLOR (fail closed) and the AI_IGNORE foot-point filter.
* **[`app/services/notification_service.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/notification_service.py):** `notify_loss_prevention(title, body, data)`: security-event log, websocket broadcast, per-device push; returns real outcomes.
* **[`app/services/preflight.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/preflight.py):** Read-only installation checks (deps, ORT flavour/providers, models vs manifest, storage); shared by bootstrap and fetch_models; `python -m app.services.preflight`.
* **[`app/services/setup_service.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/setup_service.py):** `SetupCodeManager` (one-time first-run code in `storage/setup_code.txt`), `ensure_setup_code_if_needed`, setup-completed rule (requires an active admin).
* **[`app/services/remote_access_service.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/remote_access_service.py):** Online dashboard on the operator's domain. Settings in `system_setup.remote_access` (`enabled`, `provider` cloudflare_tunnel|direct, `hostname`, last verification); token = named secret `cloudflare_tunnel_token`. Supervises `cloudflared tunnel --no-autoupdate run` (token via env `TUNNEL_TOKEN`, never argv/logs), state stopped/starting/connected/error from its log lines, exponential backoff; only runs when enabled + hostname + token + auth on. `verify()` fetches `https://<host>/api/v1/device/identity` and compares device_id. Binary: `CLOUDFLARED_PATH`, PATH, `<repo>/bin/cloudflared` (bootstrap `--with-tunnel`). `public_url()` for pairing. Routes `app/routes/remote_access.py`: GET/PUT `/api/v1/remote-access`, POST `/verify`. UI: `static/js/remote_access.js` (`#settings-device`, `#settings-remote`).
* **[`app/services/public_exposure.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/public_exposure.py):** `client_ip()` (CF-Connecting-IP / X-Forwarded-For trusted only from a loopback peer; used by auth lockout, rate limiter, setup), `is_remote_request()`, pure-ASGI middleware refusing `/api/v1/setup/*` (except GET status) and `/docs` via the public hostname and everything remote while AUTH_DISABLED, security headers (nosniff, `frame-ancestors 'self'`, HSTS only via remote HTTPS), and own-origin CORS (`*` ignored). TURN is opt-in (`TURN_ENABLED`, compose profile `turn`); the ICE endpoint returns STUN only + `turn_enabled:false` by default (WebRTC LAN-only; remote video = MJPEG over the tunnel).
* **[`app/services/secret_store.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/secret_store.py):** Per-machine secret resolution/generation into `storage/secrets/device_secrets.json`.
* **[`app/services/nvr_credential_service.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/nvr_credential_service.py):** NVR credentials in `storage/nvr_credentials.json`, passwords Fernet-encrypted (`enc:v1:`).
* **[`app/services/log_redaction.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/log_redaction.py):** `install_log_redaction()` log-record scrubbing of tokens and credentials.
* **[`app/migrations/runner.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/migrations/runner.py):** Versioned migrations + lock + backup + reconcile; see `reconcile.py`, `rebuild.py`, `__main__.py` (CLI) and `m0001`-`m0005`.
* **Scripts (`edge_backend/scripts/`):** `bootstrap.py` (fix + verify + start, via `../run.sh`), `fetch_models.py` (verify/restore models against the manifest), `manage_operator.py` (operator recovery CLI), `generate_certs.py`, `install_systemd_service.sh`, `setup_cctv_network.sh`.
* **[`tests/test_shelf_interaction.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/tests/test_shelf_interaction.py):** Unit tests for product shelf zone CRUD, wrist pose entry/dwell/exit state machine, and friction calculations.
* **[`tests/test_layout_api.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/tests/test_layout_api.py):** Layout `setup` block and structures, structure CRUD and validation, `/layout/live` shape, `calibrated` flag in pipeline status, calibration round trip (`/calibrate`, `/calibration`, `/calibration/test`), and `/reset` confirmation + fresh state.
* **[`tests/test_live_engine.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/tests/test_live_engine.py):** Worker runtime records frame size and flags; live snapshot is empty with no cameras; uncalibrated cameras report boxes but no persons; calibrated cameras place confirmed tracks on the floor; lost tracks drop out; `render_overlay` draws on a copy.
* **[`tests/test_camera_helpers.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/tests/test_camera_helpers.py):** `/cameras/{id}/live-status`, `actions/snapshot` (409 without a frame, saves the real frame), `actions/clip` (501 without a real buffer), legacy fabricated routes removed, no fallback fleet, system stats/hardware without constants, feature manager and shelf service start empty.
* **[`tests/test_market_routes.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/tests/test_market_routes.py):** Asserts the deleted fabricating modules cannot be imported and that `/analytics/market/*` reports `sufficient_history` / `days_observed` instead of a synthetic forecast.
* **[`tests/test_pose_extended_arm.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/tests/test_pose_extended_arm.py):** Raised/extended-arm fixes: upper-body plausibility gate, velocity-aware keypoint smoothing through a reach, latency-budget model ladder (timed fake ORT), RTMPose crop/decode and a real refined run on `bus.jpg`.
* **[`tests/test_shutdown.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/tests/test_shutdown.py):** A real uvicorn with two open `/stream` connections exits within 5 s of SIGTERM; `live_engine.stop_all(timeout)` joins workers and bounds a stuck one.
* **[`tests/test_pose_analytics.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/tests/test_pose_analytics.py)**, **[`tests/test_theft_detection.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/tests/test_theft_detection.py):** Pose interaction/incident pipeline and rule/confidence unit tests.
* **[`tests/test_migrations.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/tests/test_migrations.py)**, **[`tests/test_preflight.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/tests/test_preflight.py)**, **[`tests/test_secret_store.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/tests/test_secret_store.py)**, **[`tests/test_auth_setup.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/tests/test_auth_setup.py):** Migrations/reconcile/rebuild, preflight checks, secret resolution, setup-code and auth flows.
* **[`app/routes/cameras.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/routes/cameras.py):** Camera CRUD backed only by the `cameras` table (no fallback fleet), `/{id}/snapshot` (real frame, `X-Frame-Source: live|no-signal`), `/{id}/live-status` (this camera's pipeline entry + `calibrated`, frame size), `POST /{id}/actions/snapshot` (saves the current real frame, 409 if none), `POST /{id}/actions/clip` (501 unless a real buffered clip can be produced), `/scan`, and feature toggle endpoints.
* **[`app/routes/analytics.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/routes/analytics.py):** Retail analytics endpoints over recorded observations: overview KPIs, `/floorplan` (zones with metrics + coverage), `/heatmaps`, funnels, queues, decisions/actions, daily digest, POS ingest, product shelf ROI mapping (`/products/zones`), hand interactions (`/products/interactions`), `/market/predictions` (per-hour mean of this store's own recorded days; `sufficient_history: false` until enough days exist), `/market/llm-status` (Ollama status verified by a real generation), `/market/llm-optimize` (runs the business analysis and narrates it), and `/business/*`.
* **[`app/routes/layout.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/routes/layout.py):** Store blueprint API: `GET/PUT /api/v1/layout` (with `structures[]` and `setup`), zones CRUD, structures CRUD (`/structures`), camera placement, calibration (`/calibrate`, `/calibration`, `/calibration/test`), device discovery/adopt (`/discover`, `/devices`, `/devices/adopt`), `/pipeline/status`, `/live` (live person positions from calibrated cameras only), and `POST /reset` (`{"confirm":"RESET"}`) which restores the fresh state.
* **[`app/routes/zones.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/routes/zones.py):** Per-camera zone endpoints and exclusion (privacy) mask CRUD incl. `PATCH /api/zones/exclusion/{id}` (mode/colour); `/api/zones/tripwire` and `/api/zones/intrusion` (validated, PATCH-able, auth required) feed `tripwire_engine.py`; crossings persist to `tripwire_events` (m0008), restricted-area and tripwire alerts go through `alert_dispatcher`.
* **[`app/routes/system.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/routes/system.py):** `/api/v1/system/hardware` (probed `decoder_capability` + the inference backend the detector really runs), `/api/v1/system/preflight[?probe=true]`, and `/api/v1/system/stats` (CPU, real GPU utilisation or `null`, RAM, `active_cameras` from the live engine, `shm_buffer_used_mb` = `null`). Auth required.

### Web Studio & Dashboard (`edge_backend/app/static/`)
* **[`app/static/index.html`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/index.html):** The only dashboard page (served at `/dashboard` and `/dashboard/analytics`): Live Camera Matrix (built from the cameras actually registered), Store Blueprint editor with live person map, Retail Analytics & Funnels, AI Action Center, Market forecast, Loss Prevention, and Daily Digest. Shows a data-state banner and the Blueprint "Set up your store" panel until `layout.setup.configured` is true.
* **[`app/static/studio.html`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/studio.html):** Camera Studio (`/dashboard/studio`): one camera's live overlay feed and telemetry, **privacy masks** (3+ pts, mode + colour) and **product shelf areas** (4+ pts, SKU/category/price/tier modal). Tripwire (in-side, alert direction) and restricted-area (schedule rows, min dwell, severity) tools draw and edit inline.
* **[`app/static/js/floorplan.js`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/js/floorplan.js):** Canvas blueprint editor in metres: draw/move/edit rooms, walls, shelves, doors and zones (snap to 0.5 m), place and aim cameras, render the live person map from `/api/v1/layout/live`, zone inspector, and the first-run "Set up your store" panel. Loads `selftest.js` only when the URL carries `?__fptest=1`.
* **[`app/static/js/analytics.js`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/js/analytics.js):** Dashboard controller: auth-aware polling, header telemetry and data-state banner from `/api/v1/layout` `setup`, camera matrix built from registered cameras with department chips, Chart.js analytics, actions, market forecast (renders `sufficient_history: false` honestly), theft and digest tabs. Opt-in in-page self test with `?__selftest=1`.
* **[`app/static/js/studio.js`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/js/studio.js):** Studio controller: letterbox-aware click mapping to normalised coords, privacy-mask and product-shelf drawing, zone REST client. Opt-in self test with `?__selftest=1`.
* **[`app/static/js/devices.js`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/js/devices.js):** Device manager: Scan for cameras, list of devices that actually answered, inline Add form (credentials, channel, sub/main stream, placement), remove camera.
* **[`app/static/js/calibration.js`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/js/calibration.js):** Camera-to-floor calibration tool: pair 4+ points between the live frame (native pixel space) and the plan, solve via `/calibrate`, reproject via `/calibration/test` to check the result, clear via `DELETE /calibrate`.
* **[`app/static/js/auth.js`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/js/auth.js):** Sign-in gate loaded first: wraps `fetch` to attach the bearer token, shows the first-run account form or sign-in form, re-opens it on 401.
* **[`app/static/js/theme.js`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/js/theme.js):** Light / Dark / System theme control for the dashboard, Studio and the sign-in gate. Choice stored per browser in localStorage and applied by an inline `<head>` script before first paint (default dark when nothing is stored). Colours are CSS custom properties in `css/style.css` (`:root` dark values, `[data-theme="light"]` and a system-follow block); switching dispatches `window` event `edge:theme` so the floor plan, heatmap and Chart.js charts redraw. Overlays drawn on video keep fixed colours.
* **[`app/static/js/selftest.js`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/js/selftest.js):** In-page blueprint self test loaded only with `?__fptest=1`: drives the real canvas with mouse events, checks server state, prints PASS/FAIL lines into `#selftestLog` for a headless `--dump-dom` run, and deletes everything it created.
* **[`app/static/css/floorplan.css`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/css/floorplan.css):** Styles for the blueprint editor, live person map, device manager and calibration tool (loaded after `style.css`).
* **[`app/static/css/style.css`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/static/css/style.css):** Glassmorphism cyber-HUD stylesheet with full mobile (<768px), tablet (768-1024px), desktop (>1024px), and print media query support.

### Mobile Client (`mobile_app/lib/`)
* **`lib/screens/loss_prevention_screen.dart`:** Incident list from `GET /api/v1/theft/incidents` (model: `lib/models/theft_incident.dart`, `rule` falling back to `theft_type`).
* **`lib/screens/loss_prevention_alert_screen.dart`:** Single-incident detail (opened from a push with only the id, or from the list): evidence image, evidence bullets, acknowledge and inline resolve form. Framed as suspicious behaviour for staff review, never a verdict.
* **`lib/services/notification_service.dart`:** `loss_prevention_alerts` channel ("Loss prevention", normal high importance, created from Dart) and `LOSS_PREVENTION_ALERT` category; routes taps to the alert screen. iOS uses the time-sensitive interruption level (`ios/Runner/AppDelegate.swift`), not critical alerts; Android `MainActivity.kt` has no alarm channel. The emergency siren screen was deleted.
* **`lib/screens/setup_wizard_screen.dart`:** First-run wizard following `routes/setup.py`: `GET /api/v1/auth/status` -> `POST /api/v1/setup/admin` with username, password, display name and the one-time **setup code** (inline errors, no dialogs; tokens stored for later steps) -> hardware-scan, camera-scan, add-cameras, complete. `lib/core/operator_policy.dart` mirrors the server's username/password rules and `normalise_setup_code`.
* **`lib/screens/camera_settings_screen.dart`:** Per-camera retail analytics toggles (`PUT /api/v1/cameras/{id}/features`: people counting, shelf interaction, theft detection).
* **`lib/screens/zone_editor_screen.dart`:** Touch zone and privacy-mask editor. Offers tripwire / restricted-area / privacy-mask types (`lib/models/zone_model.dart`), all evaluated by the backend.
* **`lib/screens/app_shell.dart`, `dashboard_screen.dart`, `multi_cam_grid_screen.dart`, `live_view_screen.dart`, `dvr_playback_screen.dart`, `events_center_screen.dart`, `clip_archives_screen.dart`, `storage_health_screen.dart`, `settings_screen.dart`, `login_screen.dart`:** Navigation shell, live grid, DVR, incident centre, archives, storage health, settings and sign-in.

### Deployment & Integration Specifications (`docs/`)
* **[`docs/supermarket_cctv/supermarket_cctv_deployment_spec.md`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/docs/supermarket_cctv/supermarket_cctv_deployment_spec.md):** Complete hardware, network, port mapping (UPnP), and Dahua P2P cloud specification for the Pearcedale Supermarket CCTV System (Australia) including RTSP streaming endpoints and go2rtc integration.
* **[`docs/supermarket_cctv/hardware_sizing_and_procurement_guide_30_cameras.md`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/docs/supermarket_cctv/hardware_sizing_and_procurement_guide_30_cameras.md):** Complete hardware sizing, workload throughput mathematics, multi-model TensorRT VRAM sizing, itemized Bill of Materials (BOM), and procurement options for 32-camera supermarket installations.
* **[`docs/supermarket_cctv/Supermarket_Edge_AI_CCTV_Hardware_and_Services_Guide.pdf`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/docs/supermarket_cctv/Supermarket_Edge_AI_CCTV_Hardware_and_Services_Guide.pdf):** Print-ready formal PDF document (5 pages, A4) detailing 32-camera deterministic mathematical sizing, single NVDEC decode throughput, 10-Pillar Foolproof Engineering Validation Matrix, itemized BOM, 32-channel store layout, and procurement checklist.
* **[`docs/supermarket_cctv/Supermarket_Edge_AI_CCTV_Hardware_and_Services_Guide.html`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/docs/supermarket_cctv/Supermarket_Edge_AI_CCTV_Hardware_and_Services_Guide.html):** Paged-media HTML source for regenerating the 32-camera PDF specification document.

## Dashboard shell and operator workflow (2026-09-24)

* **Navigation:** five tabs plus a Settings gear: **Today** (default after sign-in), **Cameras**, **Store map**, **Insights**, **Loss prevention**. Old hashes redirect (`LEGACY_ROUTES` / `resolveRoute` in `static/js/analytics.js`): `#matrix`→`#cameras`, `#floorplan`→`#map`, `#analytics`→`#insights/footfall`, `#actions`/`#market_ai`→`#insights`, `#digest`→`#insights/report`, `#theft`→`#loss`. Phones get a bottom tab bar.
* **`static/js/today.js`:** Today screen: cameras working, people now, visitors today with source, visitors by hour (today vs yesterday vs same weekday last week; unrecorded hours striped, never zero), needs-review incidents, top recommendations, store-setup card.
* **`static/js/camera_roles.js` + `css/camera_roles.css`:** camera purpose picker (Add camera and camera settings), role badges, till/register link for checkout cameras, per-camera setup checklist modal with deep links into Studio tools (`?camera_id&tool&kind&from=checklist`) and calibration; `?checklist=<id>` opens it.
* **`static/js/insights.js` + `css/insights.css`:** one consolidated recommendations list (`GET /api/v1/analytics/recommendations`), explicit Run analysis (`POST /business/analysis/run`, 429 countdown). Nothing is generated on page open.
* **`static/js/loss.js` + `css/loss.css`:** resolve with an explicit outcome (`GET /theft/outcomes`), one-click False alarm, "staff sent" with the real delivery report, false-alarm rate per rule, re-resolving legacy incidents.
* **`static/js/heatmap_history.js`:** Insights heatmap history (floor or per-camera view, Walked / Stopped / Touched shelves, hour strip observed/quiet/camera off, playback, compare with diff legend, hour-of-day profile, Record now, cited HEATMAP_* findings). Store map heatmap has Today / Yesterday / 7 d / 30 d ranges.
* **Backend behind these:** `services/hourly_traffic.py` (`GET /analytics/footfall/hourly`), `services/recommendations_service.py` + `routes/insights.py` (consolidated list, analysis runs, m0014), `services/camera_roles.py` + `routes/camera_roles.py` (m0012), `services/heatmap_history.py` (m0013). No GET route writes to the database (enforced by `tests/test_ux_backend.py`).
