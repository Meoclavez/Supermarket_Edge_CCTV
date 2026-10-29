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
restricted areas (stockroom, cash office, after-hours floor) are in scope:
tripwires are evaluated live by `tripwire_engine.py`, restricted areas (high
security, since 2026-10-28) by `restricted_area_engine.py` (the older
tripwire_engine evaluation only runs for camera-drawn areas while the
`restricted_areas` plan feature is off). Legacy keys such as
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
      keypoints; RTMO-s)
      -> drop detections whose foot point is in an AI_IGNORE mask
      -> ByteTrack association -> project foot point to floor metres
      -> resolve zone -> open/close zone visits
      -> pose_analytics.observe(tracks + skeletons, privacy-masked frame)
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

**RTMO only since 2026-09-28** (owner decision: no AGPL/Ultralytics component in
the product; CIF channels move to D1). `models/manifest.json` lists the only
models the product may use (sha256, IO signature, Apache-2.0 licence, body7
training-data legal check pending); attribution in `THIRD_PARTY_NOTICES.md`:

* `rtmo-s-body7-640x640-static.onnx` -- **the person model** (required;
  `POSE_MODEL_GPU` = `POSE_MODEL_CPU` default). One-stage RTMO-s (mmpose).
  `fetch_models.py` downloads the OpenMMLab zip and runs
  `scripts/export_rtmo_static.py` in an isolated uv "tools" venv (onnx +
  onnx-graphsurgeon): static 1x3x640x640, in-graph NMS replaced by constant
  TopK(300), host NMS. `build_model_spec`/`decode_output` support ONLY the
  `rtmo` layout (dets [1,K,5] + keypoints [1,K,J,3], BGR 0-255 input); other
  layouts raise. Byte-reproducible.
* `rtmpose-s-256x192.onnx` -- optional top-down keypoint refiner (RTMPose-s).
  `POSE_REFINER` default **off**: on RTMO keypoints it lowered OKS (D1 .872 ->
  .853, small persons .812 -> .758) and keypoint precision.
* Removed: all YOLO26 files, the object model (`OBJECT_*`, `detect_objects`,
  `THEFT_BAG_CLASS_IDS`) and the "hand into carried bag" concealment cue (the
  pocket/waistband cue stays); Hailo YOLO HEF defaults (Hailo probe reports "no
  compatible model for this accelerator" and falls through).
* `fetch_models.py` deletes `*.onnx` in `models/` not listed in the manifest
  (logged; `--keep-unlisted`; `--verify-only` just lists). Preflight ignores
  extra files but warns when a setting names an unlisted model; retired keys
  (`OBJECT_*`, `THEFT_BAG_CLASS_IDS`, `HAILO_YOLO_HEF_PATH`) are flagged.
  `.dockerignore` whitelists only the manifest models.
* Thresholds (eval on 500 COCO val images degraded to 704x576 / 352x288, dark
  -3 EV): `PERSON_CONF_THRESHOLD` 0.50, `_DARK` 0.45 (was 0.35: RTMO dark
  precision .914), `TRACK_LOW_CONF_THRESHOLD` 0.40 (was 0.25), keypoint gates
  0.5 / wrist 0.25 unchanged. Scripts: session scratchpad `eval/scripts/`.
* Cost: RX 9060 XT 10.9 ms/inference (trial) -> 32 cameras at budget 0.6 ~=
  1.7 analysed fps per camera; ladders empty (mechanism kept for a future
  Apache model, e.g. RTMO-m static export).

**Shadow pose-model trial** (`services/shadow_trial.py`, 2026-09-27; kept,
off by default, for future candidates): `SHADOW_POSE_MODEL` (empty = off), `SHADOW_POSE_SHARE` (0.15), `SHADOW_POSE_CAMERAS`,
`SHADOW_TRIAL_WINDOW` ("HH:MM-HH:MM"). Runs on frames the live model already
analysed (`LiveCameraWorker._analyse` -> `offer()`), paced by its own cost,
device only via `DeviceGate.try_low()` (live waits counted), not in the live
busy total -> analytics/scheduler unchanged. Aggregates only (by camera,
lighting, resolution class, live model, box-height bucket; keypoint gate;
static boxes; device ms) in `STORAGE_DIR/shadow_trial/stats.json`; <=200
masked disagreement JPEGs (evidence kind `shadow_trial`, sub-cap
`SHADOW_SAMPLES_MAX_MB`), all deleted `SHADOW_TRIAL_RETAIN_DAYS` after the last
pair. `GET /api/v1/system/shadow-trial` (+ `/samples/{name}`), card in
Settings -> System health. Installer prewarm compiles the shadow model when
set; otherwise the service compiles it in a child (`prewarm_inference.py --only-model`).

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

**Static-figure filter (2026-10-01).** A poster or mannequin is detected by RTMO
as a person, so each `Track` carries `motion_state` (`pending`/`moving`/`static`),
`is_static` and `is_human` (= confirmed and not static). Motion is judged on
keypoints and box centre/height against a 5-frame mean anchor, normalised by torso
length (`body_scale()`): a move above `STATIC_FIGURE_MOTION_FRAC` (0.15 torso) on 2
consecutive frames is motion; `STATIC_FIGURE_SECONDS` (60) without motion flags
`static`, and motion un-flags it at once. The pose compared is the per-joint
median of the last 3 observations, and each joint must also exceed its own
jitter floor (`MOTION_JITTER_K` 2.5 x p75 of its last 50 frame-to-frame steps,
used from 8 steps), because RTMO wrists on small (150-260 px) figures jitter
0.12-0.38 torso per frame; box centre/height are not floored. Real-model check
(2026-10-02, CUDA, noisy still photos at 1.0/0.5/0.33 scale, 10 and 2 fps): every
poster figure flagged at 60.0-60.7 s with no un-flags, re-spawned tracks re-flagged
after 5.0-5.4 s. Known limit: a real person whose only motion is head turns under
~0.15 torso or ~1.5 % sway for 60 s is flagged static (un-flagged on the next
real movement); reliable minimum ~0.06-0.09 torso on figures >= 250 px. Per-camera `static_memory` (TTL
`STATIC_MEMORY_TTL_SEC` 1800, IoU `STATIC_MEMORY_IOU` 0.7, same pose) flags a
re-spawned poster track after `STATIC_FIGURE_GRACE_SEC` (5). Static tracks are
excluded from live counts, pose/theft analytics, zone rules/tripwires, heatmap/path
points, `close_track` persistence (footfall, traffic) and night-watch confirmation;
the server overlay draws them thin grey labelled "static", and snapshot boxes carry
`motion_state`. Per-camera features `static_figure_filter` (default on) and
`static_figure_seconds` are set in the dashboard camera editor. Tests:
`tests/test_static_figure_filter.py`.

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
  band from shoulder/hip keypoints, inside the jacket (one hand crossing to the
  opposite chest, frontal, released within `THEFT_CHEST_MAX_HOLD_SEC`), or behind
  the back / into a worn bag (wrist occluded after a reach with face, hips, knees
  and flank elbow visible); `target` = pocket/chest/behind_back; a straight arm
  hanging below the hip is a carried basket, never concealment; holds, no return),
  `SHELF_SWEEPING` (many reaches into one zone in a window),
  `SUSPICIOUS_LOITERING` (long dwell at a high-value zone + reaches + head
  turns from head yaw), `EXIT_WITHOUT_CHECKOUT` (interacted, then
  ENTRANCE/EXIT zone without CHECKOUT; `THEFT_EXIT_RULE_ENABLED`),
  `SWEETHEARTING` (POS-linked; returns `evaluable: False` without POS data and
  is not wired into the live pipeline), `BEHAVIOUR_PATTERN` (2026-10-01: per-track
  fusion of weak cues — conceal hold below confidence, reach without return, head
  scanning, high-value dwell, partial sweep, unraised exit — weighted, capped per
  cue and decayed with `THEFT_PATTERN_HALF_LIFE_SEC` 60; fires at
  `THEFT_PATTERN_SCORE_THRESHOLD` 1.2 with >= 2 cue types when no other rule fired;
  ordinary shopping scores 1.0). Thresholds are `THEFT_*` in `config.py`. No object
  detector exists since 3096ff0: every rule is pose keypoints + operator zones.
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
* **Evidence clip (optional):** per-camera feature `theft_clip` (default off)
  keeps the in-memory pre-event ring for that camera and saves ~5 s pre + 10 s
  post as `<incident_id>.mp4` after the incident is committed (at most 2 clips
  recording at once); counted and deleted together with the still by the
  evidence storage cap. Served at `GET /incidents/{id}/clip` (range, 410 once
  expired).
* **Patterns:** `GET /api/v1/theft/patterns?days=N` — hotspots by camera and zone,
  7x24 hour/weekday matrix (store-local), rule mix with false-alarm rate per rule,
  weekly trend, bursts (>= `burst_min` incidents at one camera/zone within
  `burst_minutes`; place/time only, no person identity). Dashboard: Patterns card
  on the Loss prevention tab (`#lossPatternsCard`), clip playback in the evidence
  viewer.
* Routes (`routes/theft.py`, `/api/v1/theft`): incidents list/detail/evidence/clip,
  statistics, patterns, acknowledge / dispatch / resolve.

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

### Duplicate camera guard (`services/duplicate_cameras.py`)

`identity_for(url)`: Dahua `/cam/realmonitor?channel=N` and Hikvision
`/Streaming/Channels/NNN` -> `host:port` + channel (subtype / stream ignored,
credentials ignored); generic RTSP -> host:port + path with stream selectors
normalised; plus a `dev:<host>/<input>` alias. Dahua recorders' connected-camera
list (`getConfig RemoteDevice` + optional `RemoteChannel`, cached in
`STORAGE_DIR/recorder_devices.json`, read daily from `GET /cameras/duplicates`
or `POST /cameras/duplicates/refresh`, stop on first 401) maps a channel to the
camera's own IP, so an NVR channel and a direct-IP copy share a key. Groups ->
one primary (operator choice > enabled > NVR channel > sub-stream > oldest);
others excluded from store totals via `retail_metrics_service.duplicate_camera_ids()`
(footfall/tripwire sets, active shoppers, dwell, zone_metrics, funnel, floor
heatmap live + `heatmap_history.compute_hour` floor only, forecast, live_tracks,
live_snapshot persons). A removed duplicate stays excluded (`retired`) so its
kept history is not recounted. Dismissals / primaries in
`STORAGE_DIR/duplicate_cameras.json`. 409 `{code: duplicate_camera}` on POST/PUT
camera, layout adopt, Dahua import unless `allow_duplicate`. APIs:
`GET /api/v1/cameras/duplicates`, `POST .../duplicates/{refresh,dismiss,undismiss,primary}`,
`GET /api/v1/cameras/{id}/delete-impact`. UI: `static/js/duplicates.js` banners
(`#dupBannerCameras`, `#dupBannerMap`), Today setup card, health `setup_warnings`.

### Recorder sub-stream bit rate (`services/recorder_substreams.py` `run_bitrate`)

`POST /api/v1/recorders/{id}/substreams/bitrate {channels | all_below, kbps, allow_lower}`
(202; progress `GET .../bitrate` = `.../d1`). Writes only `ExtraFormat[0].Video.BitRate`,
never lowers unless explicit channels + `allow_lower`, clamps to caps `BitRateOptions`,
same backup file as D1 (Restore brings the old bit rate back), verifies the RTSP
sub-stream at the same size, restores on failure. UI: Settings -> Recorder sub-streams.

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

**Insight topics (2026-10-21; `services/insight_topics.py`, m0021).**
Daily findings are grouped into `insight_topics` (FK-less `ai_decision_recommendations.topic_id`):
key = issue family (rules meaning the same problem, e.g. dwell-no-reach / dead shelf /
browse-no-touch = `low_pickup`) | subject (blueprint zone id, else sorted singularised name
words) | direction; fallback merge = subject token Jaccard >= 0.5 (numbered names never match)
and 0.6*subject + 0.4*TF-IDF text cosine >= 0.55. Grouping + due analyses run inside
`recommendations_service.run()` (`_group_topics`, max 40 analyses/run, daily refresh) and a
batched startup backfill (`start_background`, 30 s delay; admin `POST
/api/v1/analytics/recommendations/topics/backfill`). Analysis: trend (worsening / improving /
stable / recurring / new), weekday/peak pattern, headline metric from findings' evidence or
zone_visits / shelf_interactions over 42 days, before/after around `action_done_at`,
structured action plan (playbooks per family). Ollama only rewords the summary when narrate is
asked (numbers not in the facts -> discarded). API under `/api/v1/analytics/recommendations/topics`
(gate `business_insights`; operator writes via `@operator_allowed`). UI: `js/insights.js`
default grouped view, "Show every insight" = old list; badge = open topics. Tests:
`tests/test_insight_topics.py`. Store reset (`routes/layout.py`) does not clear the table;
`prune_orphans` removes empty topics on the next sync.

---

## File & API Reference

### Backend Core (`edge_backend/app/`)
* **[`app/main.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/main.py):** FastAPI entrypoint: installs log redaction at import, lifespan (safety warnings -> preflight -> `init_db` -> setup code -> `initialise_inference` -> pipeline -> `pose_analytics.start`), static mounts, `/dashboard` and `/dashboard/analytics` (both serve `index.html`), `/dashboard/studio` (Camera Studio), `/stream?camera_id=&fps=&overlay=1` (live MJPEG of real, privacy-masked frames for the store network/VPN; a NO SIGNAL slate otherwise; refused 403 `video_direct_only` through the tunnel), and router registrations.
* **[`app/config.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/config.py):** Settings, storage paths, retention policies, JWT keys, go2rtc URLs, and hardware device paths with graceful local fallback directory resolution.
* **[`app/models/schemas.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/models/schemas.py):** Pydantic schemas incl. `EventType` (loss-prevention types only: THEFT_SUSPECTED, CONCEALMENT, SHELF_SWEEP, EXIT_WITHOUT_CHECKOUT, LOITERING, QUEUE_ALERT, CAMERA_OFFLINE), `MaskMode` (BLACKOUT/BLUR/MOSAIC/COLOR/AI_IGNORE), `CameraFeatureConfig` (`people_counting`, `shelf_interaction`, `theft_detection`, plus options `static_figure_filter`, `static_figure_seconds`, `theft_clip`; `extra="ignore"`), `ZoneConfig`, `Keypoint`, `HardwareProfile`, `SystemStats`, `SecurityEvent`, camera/DVR/storage models.
* **[`app/services/hardware_detector.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/hardware_detector.py):** Runtime probe for decode capability (NVIDIA NVDEC, Intel VA-API, AMD Mesa, CPU SIMD) and RAM; the inference fields are read from the detector that is actually loaded, not guessed.
* **[`app/services/feature_manager.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/feature_manager.py):** Thread-safe in-process cache of per-camera feature flags; durable copy is `cameras.features`, hydrated on first API read. Starts empty.
* **[`app/services/ai_zone_service.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/ai_zone_service.py):** Per-camera zone store in `storage/zones_config.json`: `exclusion_masks` (privacy masks, applied by `privacy_mask.py`), plus tripwires and restricted areas (image-normalised 0..1 per camera) evaluated live by `app/services/tripwire_engine.py`.
* **[`app/services/inference_backend.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/inference_backend.py):** RTMO pose detector (+ optional RTMPose refiner) with runtime provider probe verified by `get_providers()[0]`; `initialise_inference()` from the lifespan; `person_detector.status()` (provider attempts, warm-up, model) is what `/system/hardware` and preflight report.
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
* **[`app/services/remote_access_service.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/remote_access_service.py):** Online access through the owner's VPS (provider `vps_tunnel`, frp v0.71.0 Apache-2.0; Cloudflare/Tailscale code removed 2026-09-28). Settings in `system_setup.remote_access` (`enabled`, `hostname`, `server_url` wss://host[:port] or tcp://host:port, `store_id`, `extra_proxies` 0-2, last verification). Secrets (named, encrypted): `vps_tunnel_store_token`, optional `vps_tunnel_server_key` (frp shared auth.token); legacy `cloudflare_tunnel_token` deleted on start. Supervises `frpc -c <STORAGE_DIR>/tunnel/frpc.toml` (dir 0700/file 0600; `user=<store id>`, `metadatas.token` + optional `auth.token` as `{{ .Envs.* }}` templates, secrets only in frpc's minimal env; one http proxy `<store>-cctv`, customDomains=[hostname] -> 127.0.0.1:PORT; wss with TLS verified against system CA / `EDGE_TUNNEL_CA_FILE`; heartbeat 30/90 s; loginFailExit + app backoff). Status from frpc log lines: stopped/starting/connected(`connected_since`)/error + `error_kind` (login_rejected, address_rejected, address_in_use, server_key_rejected, auth_service_unavailable, unreachable, certificate, not_a_tunnel, connection_lost, setup, exited). Requires HOST 0.0.0.0/127.0.0.1. `verify()` fetches `https://<host>/api/v1/device/identity`. Binary: `FRPC_PATH`, PATH, `<repo>/bin/frpc` (bootstrap `--with-tunnel`/`EDGE_TUNNEL=1`, pinned SHA-256). Routes `app/routes/remote_access.py`: GET/PUT `/api/v1/remote-access`, POST `/verify`. UI: `static/js/remote_access.js`. VPS side: `~/Projects-1/Server/cctv-tunnel` (`deploy/vps/` in this repo is legacy, do not use; `deploy/install.sh`, `bootstrap.py` and `.env.example` comments still point at it). Deployed state: see "Online access + remote live video" under Live deployment. Tests: `tests/test_remote_access.py` (fake `fixtures/fake_frpc.sh`), `tests/test_online_access.py`.
* **Remote live video (direct WebRTC; committed 05e6215, 6557217, e451706):** `routes/webrtc.py` (session API; heartbeat/report/DELETE not rate-limited, opens have their own 240/min budget), `services/webrtc_sessions.py` (one go2rtc stream per session, reaper; go2rtc restarted only for a *connected* consumer that outlives its session), `services/go2rtc_manager.py` (supervised child, reaps its ffmpeg process group; H.265 -> H.264 via go2rtc ffmpeg template `edge_vaapi_device` = `-init_hw_device vaapi`, source `#raw=edge_vaapi_device`), dashboard `static/js/webrtc_live.js` + `remote_video.js` (Settings -> Online access · Live video card). Deployed state, measurements and pending items: "Online access + remote live video" under Live deployment. Integration harness (scratch only): real frps/frpc + headless Chrome `--host-resolver-rules` + source go2rtc with ffmpeg testsrc cameras; results in the contract's "Integration run" section.
* **[`app/services/public_exposure.py`](file:///home/meoclavezz/Projects-1/Supermarket_Edge_CCTV/edge_backend/app/services/public_exposure.py):** Tunnel request = loopback peer + Host == public hostname -> remote, HTTPS (frps rewrites X-Forwarded-Proto to http), client IP = X-Forwarded-For entry `1 + extra_proxies` from the right (frps appends the VPS proxy; X-Real-IP never trusted); other loopback proxies: right-most XFF; non-loopback peers: forwarding headers ignored. uvicorn runs with `--no-proxy-headers` (unit, entrypoint.sh, bootstrap exec) so the real peer is seen. Middleware refuses `/api/v1/setup/*` (except GET status) and `/docs` remotely and everything remote while AUTH_DISABLED; security headers (HSTS only via HTTPS); own-origin CORS. Live pixels through the tunnel are refused (403 `video_direct_only`); remote viewers get direct peer-to-peer WebRTC via the in-app go2rtc, STUN only, never TURN (docs/REMOTE_VIDEO_CONTRACT.md).
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
* **Live video (pending fixes):** `lib/services/webrtc_service.dart` still uses the one-shot `/api/v1/webrtc/offer` and picks MJPEG for the remote URL, which the tunnel refuses (403 `video_direct_only`), so remote live video in the app does not work until the fixes in `mobile_app/UPCOMING_FIXES.md` (session API with heartbeat + DELETE, auth header, no MJPEG remote fallback, stale defaults). Talk-back removed 2026-09-29. Build only when the owner asks.
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

## Simple / All settings (2026-10-29, SIMPLE MODE stream, not committed)

Owner decision (brain #431): Simple is the default for everyone; owners/admins switch to "All settings" in the header.
* **Mechanism**: `static/js/view_mode.js` (classic script in `<head>` after poll.js, sets `<html data-mode>` before first
  paint; `window.edgeMode` = mode / set / reveal / isHidden / canSwitch; `localStorage edge_view_mode:<account id>` +
  `:last`; operators forced to simple; `edge:view-mode` event). CSS (style.css): `html[data-mode="simple"] [data-advanced]
  :not([data-revealed]):not([data-revealed] [data-advanced]) {display:none!important}`; `[data-simple-only]` hidden in All.
  Mark anything advanced with `data-advanced` (static in index.html, or in a module's template).
* **Deep links**: `edgeMode.reveal(id|node)` marks the target and its advanced ancestors `data-revealed` until the next
  tab change (`edge:tab`), with a bottom note "Show all settings" / "Hide it again". Called by help.js go()
  (openSettingsCard / openOnView / openCameraPart), analytics.js jumpTo + openDeviceManager; safety net:
  `Element.prototype.scrollIntoView` reveals a hidden advanced element first. Every go() action (66 incl. Features
  "Full settings") swept visible in Simple on a scratch box.
* **What Simple hides**: Today (active shoppers, avg visit, notes, legend; cameras tile only when one is not working,
  sales only when POS connected; top 2 recs; checklist hides done/optional steps; setup card = next step, hidden at
  100%); Map (Walls/Labels/Grid/Blind spots switches, `deviceManagerCard`; inspector details under `<details class=
  "fp-more">` via `moreHtml`, open in All); Insights (footfall sub-nav, AI summary, dismissed); Security (KPIs, tier
  KPIs/chips, Live activity, Wrong alerts, Patterns, intros, Refresh, RA filters/audit); Settings: 13 cards
  `data-advanced` (device, evidence, alerts, restricted areas, analysis, remote, remote video, pairing, push, devices,
  recorder streams, backups, POS once connected); camera dialog: stream/credentials/fps/placement/analysis/etc.
* **Theft preset** (site_settings.js `PRESETS`): Fewer 0.45/0.65/0.88, Balanced = null (box defaults), More
  0.25/0.45/0.72; writes only THEFT_TIER_*_MIN. Simple shows only the preset row ("How many theft alerts").
* **Restricted dialog** (restricted.js): "When it is guarded" Always / Outside store hours / Custom times (store hours
  -> `arming {mode:'schedule', schedule_mode:'armed_outside', schedule: edgeAutomation.storeHours().rows,
  source:'store_hours'}`); "How strict" Standard / High / Maximum (`STRICT`, Custom when hand-ticked); triggers under
  `<details id="raFineTune">`; POIs and sensitivity `data-advanced`.
* **Renames**: Loss prevention -> Security (tab id stays `loss`), Area & line alerts -> Area alerts, False alarms by rule
  -> Wrong alerts, Live behaviour -> Live activity, Recorder sub-streams -> Camera picture quality, Evidence storage ->
  Saved pictures and clips, Analysis speed -> Performance, System health -> System status, Appearance -> Colour theme
  (phones only, <=760 px). Removed: Shopper profile card, dead selftest CSS.
* **Automation contract**: mounts `#autoSuggestions`, `#settings-store-hours`, `#autoTuneStatus`; automation.css/js tags.
* Tests: `tests/test_view_mode_static.py`; script-order tests in test_ui_design_system / test_web_delivery allow the sync
  view_mode.js. Help: topic + guide `view-mode`. Docs: USER_MANUAL 1.6a, 2.8a; FEATURES.md.

## Automations: store hours, self-tuning, confirm-first suggestions (2026-10-29, AUTOMATION stream, not committed)

Owner decisions: brain #431. Service `app/services/automation.py`, routes `app/routes/automation.py`
(prefix `/api/v1`), restart counter `app/services/camera_restarts.py`, UI `static/js/automation.js` +
`static/css/automation.css` rendering into `#autoSuggestions` (Today), `#settings-store-hours`,
`#autoTuneStatus` (System status); containers and script tags are in index.html (SIMPLE stream). No migration:
state in `system_setup` keys `store_hours` ({learned, manual}) and `automation_state` ({dismissed, applied[50] with
undo payloads, auto_tune}); audit rows in `restricted_area_audit` (actions store_hours_set, hours_rewrite,
auto_apply, auto_dismiss, auto_undo, auto_tune; area NULL unless an area changed).
* **Store hours (A2):** `observe_days` = per store-local hour, people first seen (customer_tracks >= 3 s), till
  sales (distinct transaction_id), watched hours (heatmap_snapshots uptime >= 600 s/hour); `classify_day`: >= 5
  sales -> hours with >= 5 % of the busiest sales hour; else needs >= 20 watched hours (when uptime is recorded),
  busiest hour < 10 people = closed, open hours >= max(3, 15 % of peak). `aggregate`: last 14 days, >= 7 usable
  = "learned", median per weekday (open floored / close ceiled to 30 min), closed only if every sample closed,
  missing weekday estimated from all open days. Manual day wins; `store_hours_minutes()` is None until all 7 days
  are known. Relearn every 6 h (AutomationService loop: 2 min after start, then every 10 min tick).
  `window.edgeAutomation.storeHours()` -> null | {learnt, label "Mon–Fri 08:00–21:00, Sat–Sun …", rows (arming
  schedule shape)}; event `edge:store-hours`.
* **Store-hours arming (A3):** restricted.js saves `arming = {mode:"schedule", schedule_mode:"armed_outside",
  schedule:rows, source:"store_hours"}`; `validate_arming` keeps `source` (only "store_hours"). On any change of
  effective hours `on_hours_changed` rewrites every such area (closed every day -> `{mode:"always"}`), audit
  `hours_rewrite`. Night watch: `NightWatchConfig.follow_store_hours`; `night_watch.windows()` = `closed_windows()`
  (DST-correct wall times) when hours are known, else the camera's own start/end. PUT night-watch without the
  field keeps the stored value (older dashboards / phone).
* **Backup hour (A4):** `quietest_hour`: middle of the longest run of hours closed on every day, never 04:00
  (db_maintenance), else the quietest footfall hour, else 03:00; DailyBackupScheduler reads `backup_hour()` each night.
* **Storage (A5):** already automatic (EVIDENCE_MAX_GB 0 = 10 % of disk; backup budget 15 %); shown only.
* **Analysis speed (A7):** `choose_rates(budget_per_sec, cameras, cpu, cpu_only, current_decode)`: busy = 40 % of
  cameras, max fps = budget/busy rounded to 0.5 in [max(3, RESTRICTED_MIN_ANALYSIS_FPS), 10], idle = 10 % of the
  budget per camera in [0.3, 1], decode = ceil(max(max fps, 5)) <= 10 and only moved by >= 2 fps (reconnects every
  camera), pose share 0.6 (0.5 CPU-only). Applied via `site_settings.set_auto` (new auto layer: saved override >
  automatic > .env; describe() has `auto`/`auto_value`/`auto_source`; reset goes back to automatic). Daily, 10 min
  after the streaming-camera count changes, or until the first measurement. POST /automation/tune (admin).
* **Stuck cameras (A9):** pipeline_supervisor restarts an alive worker ONLINE with no frame for 2x
  `stall_seconds()`, at most 6/h and 5 min apart; dead and stuck restarts counted per store day (memory only).
* **Suggestions** (GET /automation/suggestions; apply/dismiss/undo admin-only via require_admin; dismiss/undo
  hide for 30 days): guard_hours (night watch cameras -> follow_store_hours; no night watch -> enable on
  entrance/exit role cameras; restricted areas on a schedule with >= 70 % of armed time while closed, never
  always-armed or day-guarded areas), less_sensitive (theft rule per camera from `review_outcome`: >= 5 reviewed,
  >= 70 % FALSE_ALARM, factor x0.8 down to 0.5 in `CameraFeatureConfig.theft_rule_sensitivity`, pose_analytics
  `_raise` divides that rule's min confidence by it; restricted triggers via `RA_STEPS`, never `RA_NEVER`
  (unannounced_entry, tamper, people_count, wrong_door, announced_overstay) or tier critical), link_zone
  (coverage_report: uncalibrated, placed, counted, enabled, top countable zone >= 60 % of its seen floor, zone not
  linked, best camera per zone; others "check first"). Plan-gated per feature (night_watch, restricted_areas,
  theft_detection).
* Tests: `tests/test_automation.py` (20); updated test_night_watch, test_daily_backup, test_camera_roles, test_features for the new fields. Docs: USER_MANUAL 2.1a, 2.9, 3.1, 4.1; FEATURES Today / Night watch /
  Settings rows.

## Restricted areas, high security (2026-10-28, BOX stream, not committed / not deployed)

Owner request: restricted areas where movements are tracked and flagged instantly; entry without notice,
movement pattern and speed triggers configurable. Owner decisions 2026-10-28: authorised entry =
announce ("I'm going in", 5-120 min) + schedule (always or weekly); alerts to store phones (highest
priority, quiet hours and cooldowns bypassed) + dashboard (banner + alarm); evidence clip; no face
recognition / identity; the VPS gets nothing new (plan key `restricted_areas` only).
User docs: `docs/USER_MANUAL.md` 2.8a + 3.5a, `docs/FEATURES.md` "Restricted areas (high security)",
`docs/FLEET_CONTROL_CONTRACT.md` (`restricted_areas` key).

* **Data (m0023, `restricted_areas`, `restricted_area_announcements`, `restricted_area_audit`;
  models at the end of `db_models.py`).** Area = name, enabled, `floor_zone_id` (store-map zone, category
  `RESTRICTED` added to `store_layout_service.ZONE_CATEGORIES`, any zone works) and/or `camera_regions`
  `[{camera_id, intrusion_zone_id}]` (Studio "Restricted area" polygons, read live) or `[{camera_id, points}]`,
  `arming` {mode always|schedule, schedule rows, schedule_mode armed_during|armed_outside, timezone?},
  `triggers` (merged over `restricted_areas.TRIGGERS` defaults), `points_of_interest`, `security`
  {confidence_delta}, manual override (`manual_state` armed|disarmed until `manual_until`, reason, by),
  `legacy_intrusion_id`. Alerts are `security_events` rows (`event_type RESTRICTED_AREA`, metadata
  `ra_area_id`, `ra_trigger`, `alert_tier`, `ra_requires_outcome`, outcome `ra_outcome` false_alarm|
  authorised|incident + note/by/at). Evidence: `<alert_id>.jpg/.mp4` in the zone_alerts evidence dir
  (evidence storage limit ages them out and nulls the links by event id).
* **`services/restricted_areas.py`:** trigger catalogue (labels/explanations/examples/defaults/ranges),
  validation (`validate_area`: privacy-mask overlap >= `RESTRICTED_MASK_REFUSE_FRACTION` 0.5 refused with
  a plain message, partial overlap = warning; floor zones checked per calibrated camera), arming
  (`schedule_armed`, `next_schedule_change`: zoneinfo, DST-correct), `CompiledArea` / `Snapshot` (what the
  workers read: regions per camera, floor polygon + calibrated cameras with covered fraction from
  `floor_coverage` (cached), linked uncalibrated cameras by `watch_zone_id`, door lines, announcements
  incl. recently ended), audit writer, `adopt_legacy()` (every zone-store intrusion zone not linked becomes
  an area once: schedule, min dwell -> min presence, severity -> tier HIGH=alert/WARNING=watch/INFO=review,
  cooldown; only "Entry without notice" on), background loop (lifespan `main.py` 4a): adopt every 30 s,
  refresh every `RESTRICTED_REFRESH_SEC` (5) and after every API write, engine `tick()` every 1 s.
* **`services/restricted_area_engine.py`:** called from `CameraWorker._analyse` with ALL live tracks
  (hits >= 2) whenever an area covers the camera (`covers()`); calibrated = floor metres, uncalibrated =
  pixels / box height ("body heights", labelled less accurate). Triggers: unannounced_entry (min presence
  0.5 s, >= 2 obs, exit grace 1 s; Critical), announced_overstay (after `until` + grace 2 min, or 15 s after
  "I'm out"; people inside before the end are covered until then, newcomers are not), dwell (60 s, Watch),
  speed (2.5 m/s or 1.5 bh/s over 0.6 s: min(net, median step) so a track jump is not running), pacing
  (`count_reversals`, 3 in 30 s), erratic (`zigzag_turns`, 4 in 12 s), crouch (hip-knee drop < 0.40 torso,
  2 s), reach_up (wrist above head, elbow above shoulder, 1 s), poi_approach (1 m / 0.6 bh, 1 s),
  boundary_linger (outside within 1 m / 0.5 bh for 8 s, Watch), people_count (max over cameras of the
  per-camera count, held 1 s; announced people allowed), wrong_door (door lines on the same camera,
  checked once per entry), tamper (`frame_health` 1 Hz on decoded frames of armed cameras: covered = thumb
  std < 6 for 10 s with a textured reference; moved = ZNCC < 0.35 for 15 s, one alert per move, gain
  changes ignored; offline = no frame 20 s, from `tick()`; Critical). Each fires once per (area, track) and
  then per-area cooldown. Still figures: static/pending-static tracks inside a guarded area count unless
  they match a figure learnt while the area was NOT guarded (`STORAGE_DIR/restricted_areas/still_figures.json`,
  IoU >= STATIC_MEMORY_IOU + poses_match; "Forget learnt still figures"). `boost_detections`: inside armed
  areas a detection within `confidence_delta` (0.05) below the new-track threshold is raised to it (after
  AI_IGNORE). Alert path: queued on the worker thread -> writer thread woken at once -> masked still ->
  `alert_dispatcher.dispatch(..., bypass_cooldown=True)` (tier routing of theft alerts) -> audit "alert" with
  `latency_ms` (stats `latency_ms`, `latency_max_ms`) -> clip thread (`record_incident_clip`, <= 2 at once)
  -> `security_events.clip_url`. `qa_feed()` behind `EDGE_RESTRICTED_QA_HOOK=1` only.
* **Pipeline hooks:** `live_analytics_engine` (frame heartbeat/tamper per decoded frame, night watch never
  holds a secured camera, analysis keeps running when every flag is off but the camera is secured, boost,
  evaluate), `inference_scheduler.set_secured` (weight `RESTRICTED_PRIORITY_WEIGHT` 3, quiet rate
  `RESTRICTED_MIN_ANALYSIS_FPS` 3; `camera_status().restricted_area`), `clip_recorder.keeps_ring`
  (pre-event ring for secured cameras), `tripwire_engine._rules_for` skips intrusion zones handled here
  (plan on), `alert_dispatcher` (FCM quiet hours bypassed via `ra_bypass_quiet`, camera mute ignored for
  `ra_area_id`), `push_alerts` (roster key `restricted_alerts` default on; tiered like theft; Critical
  `ra_all_phones` = first priority + backup at once, no separate escalation, repeats), `pairing_service.
  prefs_filter_reason(ignore_quiet_hours)`, `entitlements` (`restricted_areas` feature + label,
  `alert_feature` by `ra_area_id`, `/api/v1/restricted-areas` gated, `/api/zones/intrusion` open when either
  zone_alerts or restricted_areas is on), `routes/events.py` acknowledge needs `{outcome}` for
  `ra_requires_outcome` events, `routes/features.py` card `restricted-areas`.
* **API `routes/restricted_areas.py` (`/api/v1/restricted-areas`):** GET `/status` (cheap poll), `/catalogue`,
  ``, `/{id}`, `/events?area_id&trigger&needs_outcome`, `/events/{id}/snapshot|clip`, `/audit`,
  `/cameras-for-zone/{zone_id}`; POST `` / PUT `/{id}` / DELETE (admin), `/{id}/arm` (operator, reason),
  `/{id}/disarm` + `/{id}/resume` (admin, reason, <= 12 h), `/{id}/announce` (operator, 5-120 min, people,
  note), `/announcements/{id}/end` (own or admin), `/events/{id}/outcome` (operator), `/{id}/test-alert` +
  `/{id}/forget-still-figures` (admin); QA only: `/qa/feed`, `/qa/health`.
* **Dashboard:** `static/js/restricted.js` (`window.edgeRestricted.{open, openSettings(id|{zone_id}), refresh,
  focusEvent, announce, status}`; Loss tab card `#restrictedAreasCard` before `#zoneAlertsCard`, strip `#raStrip`
  after BANNER:END on every page ("announced by X until 14:20", countdown, "I'm out"), dialog `#raDialog` for
  announce / arm / disarm / resume / test / settings, events with outcome form, audit `details#raAudit`,
  Settings card `#settings-restricted-areas`; polls `/status` 3 s visible / 15 s hidden (not rate limited:
  `status_router`), opens `/api/v1/events/ws` and hands `ra_area_id` messages to `edgeZoneAlerts.ingest` so
  loss.js raises banner + sound at once; listens for floorplan's `fp:restricted-settings` {zone_id}, uses
  `FloorplanAPI.camerasSeeingZone` when present, else `/cameras-for-zone`), `static/css/restricted.css`;
  `zone_alerts.js` (`ingest`, `askOutcome`, kind chips, outcome form), `loss.js` (tier look, Critical never
  pushed aside by a lower theft level, 3 beeps, area alerts ring without theft data), `features.js` card
  `restricted-areas` (Easy setup: area, always/schedule, main triggers + levels), `help.js` go `restricted` /
  `restricted:<id>`, `help_content.js` articles `restricted-*` + `fs-restricted-areas`, `onboarding.js`
  optional step, `phone_alerts.js` roster checkbox `restricted_alerts`.
* Browser-verified 2026-10-29 on a scratch box (owner + 2 operators, fake Web Push endpoint, QA hook):
  banner 31-64 ms after the detection request, 3 beeps, push at the fake endpoint 102-187 ms, server
  detection->dispatch 206-337 ms; announce / I'm out / arm / disarm / resume / outcome / settings / Easy setup /
  fp:restricted-settings / operator read-only; 1366 + 360 px, light + dark, no CSP or console errors. Known:
  several dashboards behind ONE address share `general_rate_limiter` (100/min); one idle Loss tab sent
  144 API requests/min (2/min restricted-area ones besides the exempt status poll).
* Tests: `tests/test_restricted_area_engine.py` (synthetic tracks, fake clock), `tests/test_restricted_areas_api.py`.

## Audit fixes: disk safety, data loss, event loop (2026-10-29, DATA/PERF stream, not committed)
Fixes from the 2026-10-29 whole-system audit (finding ids L/M in the audit report).
- **Upgrades on a full disk (L2)**: `migrations/runner.py` checks free space >= 1.2 x (DB + WAL) before the
  pre-vNNNN copy. Short of space (or the copy failed < 30 min ago for the same target) a pending set that is
  all `ADDITIVE = True` (module attribute; m0024 is) runs without the copy (ERROR log, marker
  `<db>.backup-status.json`, shown in the storage report `other_data.migration_backup`); a non-additive set
  refuses to start without copying again on every restart. A later successful backup clears a "skipped" marker.
- **Backups (L3, M10)**: dedupe by a cheap change key (`backup_service.change_key`: schema, every table's
  count + max rowid, full rows of tables <= 50k rows) stored as `<name>.key.json`; unchanged and < 24 h old =
  no copy at all. Retention keeps 2 per other kind (pre-v*, manual, ...), compresses all but the newest and
  the newest pre-v*, and holds a byte budget `BACKUP_MAX_GB` (0 = 15 % of the disk, >= 2.2 DB copies).
  Free-space check before every copy. The startup backup runs in the background 30 s after start
  (skipped when the start already made a pre-upgrade copy). Upload limit follows the DB size.
  The 03:00 backup is DST-correct (UTC subtraction).
- **Evidence never wiped by DB/backups (L3)**: `evidence_storage` trims for the disk limit only down to
  `evidence_floor_bytes(cap)` (25 % of the cap, >= 512 MB) when non-evidence data alone is over the limit,
  and reports `disk_warning` (health DEGRADED). Expiry UPDATEs commit per 200 keys.
- **Nightly upkeep (L10, M20)**: `services/db_maintenance.py`, 04:00 store time: `trajectory_points` emptied
  (`[]`) after `TRACK_POINTS_RETENTION_DAYS` (21, never inside the heatmap backfill window), track rows
  deleted after `TRACK_ROWS_RETENTION_DAYS` (396), batches of 300 in short transactions, 20 min per night;
  then `PRAGMA optimize` + `wal_checkpoint(TRUNCATE)`. Connection PRAGMAs: journal_size_limit 64 MB,
  cache 8/16 MB by RAM, temp_store MEMORY (`database.apply_connection_pragmas`).
- **m0024 `hot_query_indexes`** (additive): security_events (event_type, timestamp) and (camera_id,
  timestamp), partial `zone_visits(zone_id) WHERE exited_at IS NULL`, theft_incidents (status, timestamp).
  1.6 s on a 1.67 GB synthetic DB with 200k events.
- **Lost writes (L4, L5, M1, M9)**: insight narration commits before the model call; business narration
  runs in a thread; a failed pipeline flush keeps its batch (newest 20k visits / 5k tracks) for the next
  try; theft evidence stills are written before the DB session opens and incidents get 30 flush tries.
- **Live video (L6)**: `/stream` encodes each new frame once per (camera, overlay, width) on a worker
  thread, shared by every viewer (`main.shared_mjpeg_frame`, `MJPEG_STATS`).
- **Clips (L7, M6, opt 6)**: clips are muxed from the ring's JPEG bytes piped to ffmpeg (image2pipe), each
  ring frame once, at the measured capture rate (no more 3x-fast pre-roll); `record_event_clip` runs off
  the loop and returns "" when nothing was written; ring frames wider than `CLIP_MAX_WIDTH` (1920) are
  scaled before the encode.
- **Cameras (M2-M5)**: ONLINE with no frame for 6 read timeouts (>= 30 s) reports `STALLED`; a dead worker
  thread is restarted by the supervisor; analysis exceptions no longer drop the stream (rate-limited
  ERROR, `rt.analysis_errors`); a replaced worker no longer wipes its successor's scheduler/night-watch/
  pose state; `Track.floor_points` is a deque(500).
- **Other**: offline watchdog bundle disabled when phone alerts are not in the plan or the store is paused
  (M14); `site_tz()` resolves the host IANA zone when `SITE_TIMEZONE` is empty (M18; night-watch zone info
  carries a warning when neither is known); restricted alert counts in SQL (`restricted_areas.
  open_outcome_counts` / `event_ids`; the route must call them); restricted engine file writes and the
  1 s tamper tick run outside its lock / off the loop; frame_health gated per camera; inference and pose
  per-frame errors log once a minute; feature flags are not cached after a failed DB read; stream sizes
  keyed by redacted URL; OpenCV env written once; insights/heatmap loops idle outside the plan.
- **Heatmaps / walking distance**: path JSON is fetched as text and decoded + binned on a worker thread;
  windows reaching past the raw-path retention are clipped and say so (`paths_from`, `note`); older
  periods come from the recorded heatmap history. `/digest?date=` uses that day for every figure
  (`overview(day=)`); `/decisions` and `/actions` take `limit` (200, max 1000).
- **Time zone**: `timeutil.configured_tz()` = the Settings value or None ("automatic");
  `site_tz()` = the resolved zone (host IANA zone when automatic); `site_tz_source()` for the UI.
- Not done (reasons): opt 7 per-camera config snapshot (a stale cache could leave privacy-mask edits
  unapplied), opt 8 NV12 letterbox (detection-accuracy risk without on-box A/B), heatmaps from
  snapshots for past hours (different grid; clipped + pointed at history instead), layout extend/fit
  batching (atomic by design; retention shrinks the rewrite), dvr_recorder day bounds (continuous-
  recording leftover; remove in the clean-up batch).

## Place-then-calibrate states, POI picker, queue metres, request rate (2026-10-29, not committed)

Owner report: Pearcedale has every camera placed but none calibrated; the Today banner said "no camera
is placed" (it tested `cameras_calibrated == 0`) with "Place a camera", so calibration was missed.

* **`services/camera_setup_state.py`** (pure, the one source of truth): per camera placed = `map_placed is True`,
  calibrated = homography; `camera_setup_state(cameras, zone_ids)` -> `step` add_cameras | place | calibrate | done,
  `level`, counts (`placed`, `unplaced`, `calibrated`, `placed_uncalibrated`, `zone_linked`), `first_unplaced`,
  `first_uncalibrated` (a placed one first), `counts` ("32 cameras placed, 0 calibrated" / "31 of 32 cameras placed, ..."),
  `headline`, `detail` (why: estimated coverage, nobody on map/heatmap, body heights not metres), `action`
  {go: place-cameras | calibrate:<id> | add-cameras}. A "Counts into zone" link is NOT calibration (never done).
  Exposed as `setup.camera_setup` + `setup.cameras_placed` (`GET /api/v1/layout`) and `coverage.camera_setup` +
  `coverage.cameras_placed` (`GET /api/v1/analytics/overview`); `routes/features.py _placement` uses it (setup until all
  calibrated; `links.first_unplaced`, `first_to_calibrate` prefers placed). Tests: `tests/test_camera_setup_state.py`.
* **Dashboard:** today.js banner (separate place / calibrate texts, btn-primary, `data-help-go`, operators get
  "An administrator does this."); onboarding steps "Put each camera on the map" (go place-cameras) and "Calibrate
  each camera to the floor" (linked != done); features.js card full link place-cameras or calibrate:<id>, Easy
  setup "Place, then calibrate" with the open step's button; floorplan.js side box `#fpCamStatePanel`
  (`renderCameraState`, counts from memory, "Hide" per browser until counts change) and `#fpStats` "(N placed, M
  calibrated)"; help.js go `place-cameras` (map + `EdgeCameraGallery.showUnplaced()`, camera_gallery.js) and
  `calibrate-next` (waits for FloorplanAPI.ready); help_content guides say place first THEN calibrate and why.
  Wrong texts fixed: camera tile HUD "on the map" -> "calibrated", insights report "N placed" (was calibrated count),
  heatmap empty text, troubleshooting line.
* **POI click-to-place (restricted.js `pickerHtml` / `wirePickers`):** floor POIs on an SVG mini-map of the area's
  zone (+ structures via new `FloorplanAPI.getZone(id)` / `structuresIn(box)`), camera POIs on the camera snapshot
  with the area's regions; click, drag (pointer capture), arrow keys (0.1 m / 0.01, Shift x5); numbers stay as the
  fallback and follow. New floor POI starts mid-zone. CSS in `restricted.css` (`.ra-pick*`).
* **Queue length (m):** `services/queue_length.py` (min-area-rectangle long axis, spread first..last person,
  spacing, `length_estimate_m` = n x spacing; null + reason when calibrated cameras see < 50% of the zone).
  `retail_metrics_service.queue_lengths(zones)` (live_snapshot persons, coverage = max `restricted_areas.floor_coverage`
  over calibrated cameras) -> ZoneMetrics `queue` / `queue_length_m` (CHECKOUT zones), lanes `queue_length_m`,
  `/analytics/space` `queues`. UI: zone inspector "Queue now" pill + hint; "Store coverage" "Checkout queues now".
  Tests: `tests/test_queue_length.py`.
* **Sales per m² per zone: NOT implemented.** `pos_transactions` = line items (transaction_id, register_id,
  sku_id, quantity, amount), no department/category; `planogram_items` (sku -> category/shelf_zone_id) has no UI
  and is empty; zone `products` is free text. `/analytics/space` returns `zone_sales_per_m2: null` + reason. Needs
  a department/category per line (or a SKU -> department catalogue) whose names match the zones' "Products sold here".
* **Request rate (`static/js/poll.js`, window.EdgePoll, loaded sync right after auth.js):** `every(ms|fn, fn,
  {hiddenMs, wsMs, when})` (paused while hidden, slower while the alert websocket is up, paused during 429 back-off,
  overdue polls run on visibilitychange), `get(url, maxAge)` shared/deduped GETs, fetch wrapper: 429 on /api/ ->
  back-off for Retry-After (else 30 s), the ONE alert websocket `EdgePoll.ws.on` (restricted.js, loss.js,
  zone_alerts.js listen; `edge:alert`, `edge:alert-ws`). analytics.js pipeline 3 s cameras tab / 10 s else (shared,
  `edge:pipeline` event), /cameras 10/30 s, system health 15 s on Settings; devices.js no 6 s full /layout poll
  (status from `edge:pipeline`, 60 s refresh on the Map tab); today.js shared reads (layout/setup/recs/pos/hourly
  60 s); loss.js 5 s without ws, 15 s (Loss tab) / 30 s with ws + immediate load on ws alerts; zone_alerts 5/30 s;
  restricted `/status` 5 s / 10 s with ws / 30 s hidden; floorplan live 1 s only with a calibrated ONLINE camera
  (else 5 s), metrics 15 s; live_behaviour 2 s when someone is flagged else 6 s; remote-access 10 s; pairing idle
  30 s; camera_roles shared reads. Icon sprite: `web_delivery._ASSET_ATTR_RE` keeps `#fragment` after `?v=`,
  `<template id="edgeIconSprite">` gives icons.js / view_zoom.js the versioned URL (immutable, no refetch).
  Measured idle /api per minute (scratch box, 32 cameras, headless): before Today 154, Cameras 249 (incl. ~110 tile
  pictures), Map 197, Insights 108, Loss 142, Settings 197; after: Today 46, Cameras 168 (54 without ~114 tile
  pictures), Map 47, Insights 27, Loss 45, Settings 48 (limited bucket 5-16/min idle, a page load adds 9-29;
  sprite 14-20/min -> 0; all include the exempt restricted /status 6-7/min). Server Retry-After header on
  429 (`auth_service.RateLimiter`) is a pending change owned by another stream (patch handed to the integrator).

## Camera coverage, reach and real sizes (2026-10-28, MAP stream, not committed / not deployed)

Contract: `docs/STORE_MAP_V2_CONTRACT.md` "Camera coverage, reach and real sizes". User docs: manual 2.6 step 3, 2.6a, 2.6b, 3.3; FEATURES "Store map and calibration".

* **m0022_camera_coverage**: `cameras.range_m` (NULL = lens default 10*tan(42.5°)/tan(fov/2), 3..30 m) and `cameras.coverage_mode` (NULL/auto | calibrated | manual). Placement PATCH takes both (`range_m: null` resets, 0.5..100 else 422).
* **`services/coverage_geometry.py`** (pure, numpy): blockers (WALL lines, ROOM/SHELF/OBSTACLE outlines, doors cut walls/room edges, COUNTER and fixtures < 1.2 m high do not block, `properties.blocks_view` overrides), ray-cast visibility polygon, calibrated footprint (homography of the image clipped at w >= w_far/3, 60 m cap), Sutherland-Hodgman clip, site clip, raster stats (floor = ROOM ∪ countable zones minus fixtures; coverage %, overlap, gaps), shelf fronts (long edges sampled 0.25 m), `sales_floor_area`, `shelf_on_sight_line`. `store_layout_service.coverage_report` -> `GET /api/v1/layout/coverage`; `serialize_cameras` adds `range_m`, `range_default_m`, `coverage_mode`, `calibrated_footprint`.
* **floorplan.js** runs the same algorithm (client/server parity verified to 0.1 m² per camera): reach field + square tip handle, calibrated/manual choice, "What it sees", "Count this camera into <zone>" (existing zone link API), shelf / zone coverage boxes, empty-inspector "Store coverage" (blind spots, per-m² store figures, PNG/print to scale), layers Coverage / Blind spots, restricted zones red hatch + `fp:restricted-settings`, `FloorplanAPI.camerasSeeingZone/getCoverage/exportMap`, `fp:coverage-changed`.
* **Metrics**: ZoneMetrics `area_m2`, `visits_per_m2`, `dwell_seconds_per_m2`, `people_per_m2_now`, `shelf_length_m`, `reaches`, `reaches_per_shelf_m`; `GET /api/v1/analytics/space` (sales per m², footfall per 100 m², walking distance from customer_tracks floor points, jumps > 3 m/s dropped); heatmaps `cell_area_m2`, `peak_per_m2`. All null when not observed.
* **Insights**: low-traffic rule per m² (>= 2 AISLE/DEPARTMENT zones with area) and skipped for zones < 50% in view of calibrated cameras (`_zone_coverage`, reason in suppressed_rules).
* **Shelf attribution**: pose_analytics floor fallback follows the sight line to the first SHELF zone when the projected wrist misses (`INTERACTION_FLOOR_SIGHT_LINE`, camera geometry via `set_camera_geometry` from pipeline_supervisor reconcile and the placement route).
* Tests: `tests/test_camera_coverage.py`. Browser-checked on a scratch Pearcedale 36x21 box (5 cameras, 1 synthetic calibration).
* Parallel stream: restricted areas (other agent) listens for `fp:restricted-settings` and uses `camerasSeeingZone`; it owns the RESTRICTED category.

## Features tab (2026-10-28; commit b2b71ec + Loss plan-gate fix 3256786, deployed to Pearcedale 2026-10-28)

User docs: `docs/USER_MANUAL.md` 1.8 (card -> Easy setup controls -> Full settings target table), `docs/FEATURES.md` "Features tab".

* **`static/js/features.js`** (`window.EdgeFeatures.{show(id,{setup}), open, close, refresh, list}`): 28 cards (`FEATURES`, ids = backend ids) in 5 groups; status chip from `GET /api/v1/features/status` (once at start for the tab badge `#tabFeaturesSetupBadge`, then every 60 s only while `#tab-features` is shown; a tab revisit re-reads only if older than 15 s). "Easy setup" = shared side panel `#featureDrawer` (role=dialog, aria-modal, focus trap, Esc, focus back) whose `setup.load/render/save/actions` use the EXISTING endpoints (site settings PUT only changed keys; camera features PUT with all three flags; night watch PUT full config; web-push roster PUT full roster; zones PATCH; users POST; backups POST; till key POST; remote-access PUT; camera role PUT; layout camera zone PUT); after a save `setup.reloads` calls the owning module (`edgeSiteSettings.reload`, `edgeNightWatch.refresh`, `edgePhoneAlerts.reload`...). "Full settings" = `EdgeHelp.go(...)`. Operators: "Admin only" chip, fields disabled (except `data-operator-ok`), Full settings greyed for admin screens. Plan off (fleet status or `plan_off`): buttons disabled + "Contact iKorex". Guide words (steps, search keywords) live in `help_content.js` articles `fs-<id>` (found by Help search, `go: feature-setup:<id>`).
* **`routes/features.py`** `GET /api/v1/features/status` (verify_api_access + general_rate_limiter; 423 while paused like neighbours; not behind a plan feature): in-process reads only (cameras table, active layout counts, `ai_zone_service`, shelf zones, roster/subscriptions, till key, analysis runs/topics, heatmap recorder, remote-access settings/state, backup list, evidence status, fleet status, recorders); never writes, never probes; plan-off features return only `plan_off` and their data is not read; `ADMIN_DETAIL` details blank for operators; `links` = first camera ids for Full settings. Tests: `tests/test_features_status.py` (+ the GET-never-writes sweep covers it).
* **help.js `go()`** extended: `view:<route>#<id>`, `camera:`, `night:`, `camera-part:<cam>#<id>`, `calibrate:`, `studio-url:` (same-site only), `feature:` / `feature-setup:`, `help:`, `features`; `html { scroll-padding-top }` keeps scrolled cards clear of the sticky header. Help panel quick button "All features". studio.js `?from=features` -> "Back to Features".
* Onboarding: checklist steps carry `card` -> "Easy setup" link (`feature-setup:<card>`); welcome tour 5 screens (Features screen). Empty states (zone alerts, night watch card, phone alerts, insights) link to the matching card.
* Header: 6 tabs; at 1280-1599 px the tabs keep full width (padding 10 px, no gaps, header gap 16 px) and "Edge AI CCTV /" is hidden at 761-1599 px so nothing is clipped at 1366; phones: bottom bar `grid-auto-flow: column` (6 x 60 px at 360 px).
* Browser-verified 2026-10-28 on a scratch box (owner, operator, plan with theft/heatmaps off, 1366 + 360, light + dark). Idle on the tab the whole dashboard sends ~86 /api requests/min, 15 in the 100/min limited bucket, 1 from the Features tab.

## Report a problem + box issue log (2026-10-23, BOX stream, not committed / not deployed)

Contract: `docs/SUPPORT_ISSUES_CONTRACT.md` (VPS side built in parallel in `~/Projects-1/Server/cctv-tunnel/control/`).

* **`services/issue_log.py`:** `IssueCaptureHandler` on the root + `uvicorn` loggers (installed in `main.py` lifespan right after the edge log handler; log_redaction runs first at import) + wrapped `sys.excepthook` / `threading.excepthook`. Captures >= ERROR, uncaught exceptions (asyncio "Task exception was never retrieved" arrives via the `asyncio` logger) and `WARNING_RULES` (disk low, inference CPU fallback / unavailable, DB locked, migration / backup failed, `Camera <id> error|login failed:` only after 10 min of the same camera failing). `redact_text` (URLs incl. rtsp creds, e-mail, MAC, IPv4/6, paths -> `app/...`/`<lib>/...`/`<path>`, hostnames by TLD, known names -> id/`<name>` from `NameCache`, quoted strings -> `<q>`), `message_template` + `fingerprint` (sha256 logger/exc type/template; camera errors collapse to one per reason, `affected` = distinct ids). `IssueStore`: memory under a lock, background thread `issue-log-writer` writes `STORAGE_DIR/support.db` (tables issues/meta/reports, WAL), cap 500 (evict oldest last_seen, `dropped`), `sent_count` = acknowledged count. Own loggers `edge.support*` never captured; thread-local reentrancy guard; handler never raises.
* **`services/support_reports.py`:** `ReportStore` (cap 200, 20/hour, text 10..2000, secrets redacted, statuses queued -> received/in_progress/fixed/closed, `report_cursor` in meta), `collect_diagnostics`, `SupportUploader` (task `support-uploader`, `POST <server>/control/v1/issues` via `fleet_control.target()` incl. `FLEET_CONTROL_DEV_URL`; <= 10 reports + <= 50 issues, <= 48 KB; ack only on 200 `accepted_*`; not-accepted issues retry after 6 h, reports after 10 min; 429/413/5xx/offline backoff 60 s -> 15 min, 413 halves the issue batch, 404/405/501 wait 1 h; nothing sent while fleet control is not configured). `fleet_control`: check-in body `report_cursor`; answer `report_updates` applied ONLY when this check-in's signed ack verified (`_check_ack`, same rules as standby) and its `report_updates_sha256` = sha256 hex of `canonical_json(report_updates)` matches (`support_reports.report_updates_verified`; otherwise ignored at INFO, cursor not advanced; an older VPS without the field: ignored); every check-in requests an upload; `status()` adds `support`.
* **`entitlements.normalise_support`:** policy `support` {name, phone, email, hours, website (https), note}, each <= 120 chars, invalid values dropped one by one (never the whole policy), unknown keys ignored; `effective_state()["support"]`. `/api/v1/support*` is open while paused (`_PAUSE_EXEMPT`); operators may POST reports (`_OPERATOR_ONLY`).
* **`routes/support.py`:** `GET /api/v1/support`, `GET /api/v1/support/diagnostics`, `GET|POST /api/v1/support/reports` (session users only; stream tokens 403).
* **Dashboard:** `static/js/support.js` (`window.EdgeSupport`: contact from policy else `help_content.js SUPPORT`, dialog with focus trap/Esc, My reports, Settings card `#settings-support`; opened by `data-support-report` / `data-support-reports`), `static/css/support.css` (z 9100, above the paused page). Help panel and the paused / standby page (fleet.js) show the contact + "Report a problem"; Help article `report-problem` (`go: 'report'`).
* Tests: `tests/test_support_issues.py`.

## In-app help and onboarding (2026-10-21)

Plan and research: `docs/UX_SUPPORT_PLAN.md`; user-facing description: `docs/USER_MANUAL.md` 1.7.

* **`static/js/help_content.js`:** all help words: `SUPPORT` (iKorex support phone/email/hours/web, **null until the owner supplies them**), `topics` ("?" popover text), `anchors` (where "?" goes on cards other scripts render), `articles` (Help panel guides, `role: 'admin'` hides from operators).
* **`static/js/help.js`:** `window.EdgeHelp`: "?" buttons from `data-help` / `data-help-after` attributes and `anchors` (MutationObserver re-adds them after re-renders), non-modal popover (Esc, aria-expanded), header Help panel `#navHelp` (search, guides, "Do it" via `go(action)`), `friendlyError(status, body)` / `fetchJSON(url)` for 401/403 feature_disabled/409 camera_limit/423 service_paused|standby/offline. Global click hooks for empty states: `data-help-go="<action>"`, `data-help-open="<article>"`.
* **`static/js/onboarding.js`:** `window.EdgeOnboarding`: Getting started checklist in `#gettingStartedMount` (Today; manual Part 2 order, live status from layout/store setup/cameras/site-settings/product zones/`/api/zones`/night-watch/web-push/users/remote-access/pos/backups; operators get 2 own steps; steps for features off in the iKorex plan hidden), first-run welcome for admins (localStorage `edge_cctv_welcome_seen:<user id>`; checklist flags `edge_cctv_getting_started:<user id>`).
* **`static/css/help.css`:** styles for all of the above (z: welcome 1400, panel 1500, popover 1600).
* Loaded in index.html after fleet.js, before theme.js (theme.js must stay last). Not on studio.html yet.

## Store map v2, camera gallery, iKorex branding, fleet control (2026-10-16)

Contracts: `docs/STORE_MAP_V2_CONTRACT.md`, `docs/FLEET_CONTROL_CONTRACT.md`. Built and QA'd
end-to-end on a scratch box (headless Chrome, all checks PASS); commit c206a32. VPS side
deployed 2026-10-21 (key sha256:3ee9e4f81f48d587); box deployed 2026-10-21 20:43 AEDT at 3ec190d
(m0020 applied with pre-v0020 backup, MIGraphX inference, first check-in 20:45:25 pinned the key; check-ins
every ~5 min, telemetry numbers only). The 2026-10-16/17 outage was the store's internet (DNS
failures), the box stayed up.
**2026-10-21 follow-up build f423bbf (DEPLOYED 2026-10-22 ~02:30 AEDT, VPS control + box; verified: 2.7.0, signed acks, topics backfilled, map 40x25 aligned to backup+(2,2)):**
APP_VERSION 2.7.0; offline standby (contract decision 5); atomic site changes (below); in-app
help/onboarding; insight topics (m0021). Integration QA on a copy of the real 03:00 backup: all
checks PASS, full suite 1510 passed, VPS control 43 passed. Open: SUPPORT contact in
static/js/help_content.js is null (owner to supply). Incident: brain #388.
* **Atomic site changes** (`store_layout_service._site_change`): extend/fit/resize run in their
  own session behind a per-loop asyncio lock, `BEGIN IMMEDIATE` before the first read, one
  all-or-nothing txn, whole-op retry x3 then `SiteBusyError` -> 503 Retry-After; fit bounds are
  computed inside the lock (idempotent); `_prescan_tracks` before the lock. `database.get_db`
  yields once (the old retry loop re-yielded -> FastAPI `athrow()` 500s). `floorplan.js`
  `_runSiteChange` disables site buttons while one runs. Tests: `tests/test_store_map_atomic.py`.
* **Offline standby**: policy `licence.offline_grace_hours` (1..2160 or null = never); every
  check-in sends a nonce, the VPS returns a signed `ack`; only a verified ack counts as contact
  (`last_verified_contact_at`, CLOCK_BOOTTIME + persisted floor). Past the limit -> state
  `standby`, API 423 `service_standby`, standby page; resumes only after a verified ack.
* **Map = "site"**: `store_layouts.width_m/height_m` is the whole canvas (building = a ROOM).
  `POST /api/v1/layout/extend {left_m,top_m,right_m,bottom_m}` (left/top translate zones,
  structures, cameras, calibration points, homography `T@H`, shelf_interactions floor x/y,
  customer_tracks `{x,y}`; heatmap_snapshots NOT moved), `POST /layout/fit {margin_m}`,
  `PUT /layout` shrink -> 409 `items_outside` unless `clamp:true`. New kind `OUTDOOR`.
  m0020 `cameras.map_placed` (unplaced cameras are excluded from fit and kept inside).
* **Editor** (`floorplan.js`): tool palette (Room, Outdoor area, Wall, Door, Shelf, Counter,
  Obstacle, Zone, Measure), drag-rectangle drawing + Free shape, 8 resize handles + rotate
  (`properties.rotation_deg`), X/Y/Angle/Width/Depth, Duplicate (Ctrl+D), nudge, Undo/Redo,
  Pointer Events (touch pan/pinch), "Extend the map to fit?" bar, Map size panel, scale-bar
  plate. `window.FloorplanAPI` + `fp:*` events for the gallery. Self test `?__fptest=1`.
* **Camera gallery** (`camera_gallery.js`, `still_cache.js`): drag cards onto the map; stills
  cached per user in IndexedDB `edge-stills`, wiped on sign-out (`edge:signed-out` in auth.js);
  LAN = snapshot fetch, public = one `WebRtcLive.grabFrame` at a time, never polls.
* **Branding**: `static/brand/` (WebP used, PNG shipped), `brand.css`, `splash.js` intro.
* **Fleet control, box side**: `services/entitlements.py` (policy cache, `FleetGateMiddleware`
  423 service_paused / 403 feature_disabled / 409 camera_limit), `services/fleet_control.py`
  (check-in every 300 s to `https://<tunnel host>/control/v1/checkin`, Ed25519 verify,
  key pinned on first use or `.env FLEET_CONTROL_PUBLIC_KEY`; QA-only
  `FLEET_CONTROL_DEV_URL` for 127.0.0.1), `routes/fleet.py`, `static/js/fleet.js` (Settings
  "iKorex service" card, licence banner, paused page, also in studio.html). Pause/resume,
  heatmaps, business_insights, phone_alerts changes restart the app (5 s debounce).
  Licence end date judged on the box clock + grace days; offline standby only when the VPS
  sets `offline_grace_hours`.
* **Fleet control, VPS side**: `~/Projects-1/Server/cctv-tunnel/control` (cctv-control,
  port 9200, `control.ikorex.com.au`), deploy runbook in that README "Fleet control".
* Known gaps: older recorded heatmaps offset after extend-left/top; undo of a zone delete gives
  a new id; edge API rate limiter (100/min/IP) can 429 under heavy page reloads.

## Enterprise UI and tiered theft alerts (2026-10-08)

* **Design system** (`static/css/style.css`): flat slate theme, dark default + light (`data-theme`,
  prefers-color-scheme), system font stack, one blue accent (#4c8dff dark / #2563eb light), semantic
  success/warning/danger/info/neutral tokens (AA), tier tokens `--tier-*`, spacing 4-32, radius 4/6/8 (12
  modals). Components: `.btn` (+primary/secondary/ghost/danger/success/warning, xs/sm/lg, `.btn-icon`),
  `.badge-*`, `.status-dot.is-*`, `.card*`, `.table`, `.field`/`.input`/`.select`, `.kpi*`, `.empty-state`,
  `.tier-badge.tier-*`; old neon class/variable names are aliases. No glows/gradients/blur/web fonts.
* **Icons:** Lucide `lucide-static@1.53.0` (ISC) as `static/icons/sprite.svg` (80 symbols `i-NAME`),
  `js/icons.js` `window.EdgeIcon.svg(name,{size,label})` / `.el()`; rebuild with
  `edge_backend/scripts/build_icon_sprite.py`; licence in `static/icons/LICENSE-lucide.txt` and
  THIRD_PARTY_NOTICES.md. `tests/test_ui_design_system.py` fails on any emoji/pictograph in static/.
* **Tiered theft alerts** (`services/theft_alert_policy.py`, migration `m0019_theft_alert_tiers` adds
  `alert_tier`, `risk_score`, `risk_factors` to theft_incidents): risk = confidence x place (high value
  x1.25, low value x0.85, exit zone x1.25 or door camera x1.15, checkout camera x1.1; cap x1.5) x case
  (pocket/chest 1.0, behind back 0.85, sweeping 0.9, exit 0.9, loitering 0.55, pattern 0.65). Levels:
  review < 0.35 <= watch < 0.55 <= alert < 0.80 <= critical; concealment then exit (same track, 300 s) =
  critical; two rules on a track or >= 3 incidents per camera+zone in 30 min = +1 level; High-value role
  floor = alert only if already >= watch. Routing: review = queue only; watch = banner (sound/push
  switchable, off); alert = banner + sound + push (escalate switchable, off); critical = everything +
  repeats until acknowledged, bypasses the camera cooldown. Severity kept: critical/alert HIGH, watch
  MEDIUM, review LOW. Site settings group `theft_alerts` (Settings > Theft alert levels,
  `#settings-theft-alerts`); GET `/api/v1/theft/alert-policy`; incidents `?tier=` / `?min_tier=`;
  statistics `by_tier`/`active_by_tier`/`false_alarm_rate_by_tier`; `/live/tracks` and `/live/behaviour`
  carry `tier` + `risk_score`. Tier routing supersedes the phone roster `min_confidence` for theft
  incidents (it still gates untiered theft alerts). Dashboard: level KPIs `#lossTierKpis`, chips
  `#lossTierChips`, "Why this level" on cards, banner/sound gated by `alert_channels`.
* **Audit (owner decisions 2026-10-08, kept as recommended):** exit-without-checkout only fires on one
  camera seeing shelves, checkout and exit (no cross-camera re-id), so concealment-then-exit is rare;
  sweethearting unwired (needs POS per-item scans); critical respects phone quiet hours and camera mute;
  behind-back discounted x0.8 x 0.85. Dead settings: THEFT_POS_MATCH_TOLERANCE_SEC,
  THEFT_CONFIDENCE_DURATION_REF_SEC. Fixed: phone severity ignored role floors; FCM pushed every incident
  while Web Push used confidence >= 0.75; Web Push escalated every theft alert.
* **Deployed** 2026-10-09 01:57 AEDT (f9c288c): journal "backup before migrating to v19" then "applied migration
  0019_theft_alert_tiers" (grep the journal for `0019`, not `m0019`); 0 errors; box had 0 theft incidents at deploy.
  Live read-only UI check after deploy passed (all pages, 80 icons, no JS errors, stale tokens, Ch 12 cut-out
  grey "static"); phone fixes in the follow-up commit: Studio camera picker one scrollable row (was 4,171 px
  wide), enlarged-tile zoom tools moved under the top badges, empty `.cam-hud-badge` hidden, tile footer wraps
  (8767156, deployed 2026-10-09 02:45 AEDT; box serves the fixed CSS, 0 errors).
  Known minor: icons/sprite.svg is served no-cache from an unversioned URL (revalidated ~every 4 s, 304s).
* Tests: test_theft_tiers.py, test_theft_end_to_end.py, test_theft_tier_ui.py (+ fixtures/theft_tier_harness.js),
  test_ui_design_system.py.

## Live skeleton/behaviour overlay, analysis speed and zoom (2026-10-08)

* **Model:** RTMO-s pose only (box + 17 keypoints per person); boxes without a skeleton were a
  display gap (remote WebRTC had a box-only canvas; coasting tracks kept a frozen box), not a model change.
* **Live data API** (`routes/live.py`, auth as `/layout/live`, allowed through the tunnel):
  `GET /api/v1/live/tracks?cameras=a,b` (<= 16 ids) -> per camera `seq`, `analysed_at`, frame size,
  measured `analysis_fps`, tracks with normalised `box`, `keypoints`, `velocity` (per s), `fresh`,
  `age_sec`, `motion_state`, `behaviour` {level normal/watch/alert, labels, reaching_zone, conceal,
  head_turns, pattern_score/threshold, incident_id/rule}; `GET /api/v1/live/behaviour` lists watch/alert
  tracks. Behaviour comes from `pose_analytics.live_state()` (50 ms try-lock, 0.2 s cache per camera).
* **Overlay:** `static/js/live_overlay.js` draws box + skeleton (joints vis >= 0.3) + behaviour chip on
  continuous views (WebRTC tiles, enlarged MJPEG, Studio) with the stream at `overlay=0`; extrapolates by
  velocity minus a 0.2 s picture delay (cap 0.3 s). Local grid tiles (stills every 1-5 s) keep the server
  overlay (`render_overlay`), which now colours watch amber / alert red, adds labels, and draws coasting
  tracks dashed at the Kalman-predicted box (skeleton only <= 1 s after the last match). Toggle
  `#aiOverlayToggle`, stored in localStorage `edge.aiOverlay.v1`.
* **Live behaviour:** `#liveBehaviourCard` (Loss prevention) and `#liveBehaviourStrip` (Cameras),
  `static/js/live_behaviour.js`; click / theft banner "Watch live" -> `openCameraLive(cam, {trackId})`.
* **Zoom:** `static/js/view_zoom.js` on the enlarged tile and Studio: wheel at cursor, pinch, drag-pan,
  - / 1x / + buttons, double-click 2x (tiles), fullscreen; picture + overlay + Studio drawing canvas share
  one transformed stage so clicks and overlays stay aligned.
* **Analysis speed** (`services/inference_scheduler.py`, `services/motion_gate.py`): budget =
  `POSE_BUDGET_UTILISATION x 1000 / measured cost_ms`; quiet cameras (no people, no motion for
  `ANALYTICS_ACTIVE_HOLD_SEC` 4) run at `ANALYTICS_IDLE_DETECT_FPS` (0.5); a 64x48 luma frame-difference
  gate (~56-107 us/frame, lighting-compensated, AI_IGNORE excluded) wakes them at once
  (`ANALYTICS_MOTION_WAKE`); the rest is split max-min by priority weight (low 0.5 / normal 1 / high 2)
  up to `ANALYTICS_MAX_DETECT_FPS` (10) or the camera's `max_analysis_fps`. `DECODE_MAX_FPS` default 10
  (change reopens captures). Settings -> Analysis speed (`#settings-analysis`, site_settings group
  `analysis`), camera editor `#configAnalysisPriority` / `#configMaxAnalysisFps`, capacity
  `GET /api/v1/system/analysis-capacity`. Simulated at 10.9 ms, 32 cameras, util 0.6: 4 busy cameras
  10 fps each, 8 -> 5.4, 16 -> 2.9 (was 1.7 for all).
* **Tracker:** Kalman predicts with real dt (noise scaled per 0.2 s), `velocity_px` is px/s,
  `Track.predicted_bbox` / `predict_bbox(at)`, coasting predicted <= 3 s, stage-3 centre-distance match
  (gate max(0.25, 2.0 x dt) + half predicted travel box heights, cap 1.5, height ratio 1.5) keeps a fast
  walker's id, `TRACK_MAX_AGE_SEC` 5. Local UI check with real RTMO: 0 id switches over three fast pans.
* **Deployed** 2026-10-08 21:10 AEDT (27f2347). Live at closing time: GPU 55-59 % (unchanged), cost 11.07 ms,
  budget 54.2/s; two issues found live, fixed in 545bb11 (deployed 22:49 AEDT; live check after the 5 min
  learning period: 0 of 32 empty cameras active, idle 0.5 fps, Ch 12 cut-out static in 51/51 samples):
  (1) motion gate false-woke ~17 of 32 empty cameras: burnt-in OSD clock pixels + local lighting (street
  light through doors, cycling sign/TV, shop window). Fix: per-pixel compare after 5x5 local brightness
  ratio, learnt flicker map (duty > 8 % or >= 6 changes in 300 s, widened +-2 cols / +-1 row), motion needs
  a 4-pixel 8-connected group; steady-state false wakes 1.51 -> 0.17 per camera-minute, active share
  95 % -> 1 %, 120/120 synthetic walk-ins woken (median 0.1 s). (2) Ch 12 cut-out flipped because its
  feet are hidden by a cooler: box bottom edge jitters (p90 0.12 torso, 2 % jumps of ~1 torso when RTMO
  hallucinates knees/ankle). Fix: box motion measured on `box_reference()` (top-centre + width, not the
  bottom edge); a joint counts only if seen on its last 3 observations and >= 50 % of recent ones
  (`MOTION_JOINT_MIN_SEEN`); replay: 0 flips at 1.7 / 3 / 10 fps (was 4-11). Tests:
  test_motion_gate_store.py, test_static_cutout_replay.py (fixtures from store-closed frames).
* Tests: test_live_tracks_api.py, test_analysis_rate_settings.py, test_tracking_fast_motion.py,
  test_live_view_frontend.py (Node harness tests/fixtures/live_view_harness.js).

## Dashboard shell and operator workflow (2026-09-24)

* **Navigation:** five tabs plus a Settings gear: **Today** (default after sign-in), **Cameras**, **Store map**, **Insights**, **Loss prevention**. Old hashes redirect (`LEGACY_ROUTES` / `resolveRoute` in `static/js/analytics.js`): `#matrix`→`#cameras`, `#floorplan`→`#map`, `#analytics`→`#insights/footfall`, `#actions`/`#market_ai`→`#insights`, `#digest`→`#insights/report`, `#theft`→`#loss`. Phones get a bottom tab bar.
* **`static/js/today.js`:** Today screen: cameras working, people now, visitors today with source, visitors by hour (today vs yesterday vs same weekday last week; unrecorded hours striped, never zero), needs-review incidents, top recommendations, store-setup card.
* **`static/js/camera_roles.js` + `css/camera_roles.css`:** camera purpose picker (Add camera and camera settings), role badges, till/register link for checkout cameras, per-camera setup checklist modal with deep links into Studio tools (`?camera_id&tool&kind&from=checklist`) and calibration; `?checklist=<id>` opens it.
* **`static/js/insights.js` + `css/insights.css`:** one consolidated recommendations list (`GET /api/v1/analytics/recommendations`), explicit Run analysis (`POST /business/analysis/run`, 429 countdown). Nothing is generated on page open.
* **`static/js/loss.js` + `css/loss.css`:** resolve with an explicit outcome (`GET /theft/outcomes`), one-click False alarm, "staff sent" with the real delivery report, false-alarm rate per rule, re-resolving legacy incidents.
* **`static/js/heatmap_history.js`:** Insights heatmap history (floor or per-camera view, Walked / Stopped / Touched shelves, hour strip observed/quiet/camera off, playback, compare with diff legend, hour-of-day profile, Record now, cited HEATMAP_* findings). Store map heatmap has Today / Yesterday / 7 d / 30 d ranges.
* **Backend behind these:** `services/hourly_traffic.py` (`GET /analytics/footfall/hourly`), `services/recommendations_service.py` + `routes/insights.py` (consolidated list, analysis runs, m0014), `services/camera_roles.py` + `routes/camera_roles.py` (m0012), `services/heatmap_history.py` (m0013). No GET route writes to the database (enforced by `tests/test_ux_backend.py`).

## Live deployment: Ubuntu edge box (updated 2026-10-01)

**Running version (2026-10-29 18:17 AEDT):** 2.10.0, commit `c99a87b`: Simple mode by default, learnt store hours, approved automations. Earlier that day: 2.9.0 `7bc882c` (audit fixes; m0024 indexes) and 2.8.0 `7cd4af9` (restricted areas, camera coverage; m0022/m0023). Full suite 1723 passed. Live checks: dashboard 28-46 requests/min idle (was 155-174), calibration banner "32 placed, 0 calibrated", box clock Australia/Melbourne. `.env` has FLEET_CONTROL_PUBLIC_KEY (the line appears twice; dedupe optional). The VPS audit fixes are deployed too (Server project map). Brain #427, #429-#438. Not done: clean-up batch (dead code, CSS, docs, deps, duplicate root docs, repo site data) and the phone app rebuild (fake DVR screen removed in source).

**Disk unlock (2026-10-28):** full-disk LUKS2 unlocks by TPM2 (PCR 7) at boot; passphrase + offline recovery key remain. Details, rebuild command and re-enrol after BIOS/Secure Boot updates: brain #424. Box release 95fc122 (Report a problem + issue log) deployed 2026-10-28 13:22 AEDT, reboot-tested 13:52. Pending on site: BIOS 'Restore after AC Power Loss' = Power On, ErP Ready Disabled; UPS.

**Box:** `securitypc-MS-7D90`, Ubuntu 26.04, i5-14400F (no iGPU), RX 9060 XT 16 GB (gfx1200),
16 GB RAM, Wi-Fi 192.168.20.239, Tailscale 100.78.122.93 (tailnet `tail9fc52a`), TZ
Australia/Melbourne (store: IGA Pearcedale). SSH from the dev PC: `ssh aus` (user `securitypc`,
key `~/.ssh/id_ed25519`, needs `SSH_AUTH_SOCK=$XDG_RUNTIME_DIR/ssh-agent.socket ssh-add` after a
reboot). No passwordless sudo: every deploy is run by the owner.

**Layout:** `/opt/edge-cctv` (git clone of GitHub main, owner `edgecctv`, system user, groups
video/render), venv `/opt/edge-cctv/.venv` (Python 3.14, onnxruntime 1.29 + MIGraphX plugin,
ROCm 10 wheels), config `edge_backend/.env` (0640), data `storage/` (0750: DB, secrets/,
evidence, migraphx_cache/), unit `/etc/systemd/system/edge-cctv.service` (from
`deploy/edge-cctv.service`: `--host ${HOST} --port ${PORT} --no-server-header`, MemoryHigh 7G /
MemoryMax 9G, ProtectProc=invisible). Logs: `journalctl -u edge-cctv` (securitypc is in `adm`).

**Deploy (owner runs):**
- code-only change: `ssh -t aus 'sudo -u edgecctv git -C /opt/edge-cctv pull --ff-only && sudo systemctl restart edge-cctv'`
- unit/installer/dependency change (check `git diff --stat <live>..HEAD -- deploy/ edge_backend/requirements.txt edge_backend/scripts/bootstrap.py`):
  `ssh -t aus 'sudo -u edgecctv git -C /opt/edge-cctv pull --ff-only && sudo bash /opt/edge-cctv/deploy/install.sh'`
  (stops the service, bootstrap + MIGraphX pre-compile of every ladder model, installs the unit,
  prints the first-run setup code). `EDGE_TUNNEL=1` also fetches frpc for online access.

**Access:** owner privately via Tailscale `http://100.78.122.93:8000/dashboard` (ufw: 8000 on
tailscale0 only; plain HTTP, WireGuard encrypts; no Tailscale text in the dashboard). Public:
`https://pearcedale-cctv.ikorex.com.au`, live since 2026-10-01 (see next block).

**Online access + remote live video (deployed; public path verified 2026-10-01 on e451706):**
- Path: viewer -> Cloudflare -> VPS `web-gateway` (nginx) -> `cctv-frps` (frp 0.71.0) -> box
  frpc -> uvicorn; per-store token + hostname lock in `cctv-auth`. VPS package
  `~/Projects-1/Server/cctv-tunnel` (README: apply plan, `manage status`, debugging,
  `onboard-store.sh`, `stun-selftest.py`); `deploy/vps/` is legacy, do not use. Naming
  `<store>-<service>.ikorex.com.au`; tunnel `wss://tunnel.ikorex.com.au`; Cloudflare DNS:
  `tunnel` and `<store>-cctv` proxied, `stun` DNS-only.
- Box settings (Settings -> Online access): Public address, Tunnel server, Store ID, Store
  token, optional Server key, Proxies in front = 1 (Cloudflare). frpc via
  `sudo EDGE_TUNNEL=1 bash deploy/install.sh`; go2rtc 1.9.14 fetched by default.
- Owner rule: the VPS is only a connection broker + debugging aid. Live video is direct P2P
  WebRTC box go2rtc <-> browser, STUN only (`stun.ikorex.com.au:3478`, coturn `--stun-only`),
  never TURN/relay; sessions only while a tile is on screen (rotate, enlarge-other, view
  switch, hidden tab, page close end them; vanished viewers reaped within ~45 s). Live
  pictures through the tunnel get 403 `video_direct_only` from the gateway and the box;
  stored evidence stills/clips still open on click. Remote stills (floor-map thumbnail,
  heatmap background, calibration) are one frame over a direct connection ("Take a new
  picture"). Store NAT is endpoint-independent, so mode `auto` needs no port forward.
- Builds: a38b391 (2026-10-03: static-figure filter, pose-only theft, clips, patterns), 05e6215 (direct P2P video), 6557217 (VA-API device up front), e451706 (go2rtc
  ffmpeg template fix; H.265 resolved). 30/31 NVR sub-streams are H.265 -> h264_vaapi, ~3 %
  of a core per tile, first frame ~4 s (H.264 ch25 passthrough ~1.5 s).
- Measured on the public path: pair srflx<->srflx (box 58.179.142.243), 4 tiles ≈ 3.2 Mbit/s
  direct; VPS ≈ 0.44 MB/min cctv-frps while watching, ~3 kB/min idle; sessions end 0.64 s
  after a view switch, 9 s after a killed browser. Evidence: contract "Public path check".
- Dashboard: Settings -> "Online access · Live video" card (STUN servers, mode/port, most
  live videos at once = 8, store-network transport, per-browser toggle, Check remote video
  with STUN test, diagnostics, Watching now, advice).
- Pending: Cloudflare/VPS security hardening -> `~/Projects-1/Server/SECURITY_NEXT_WORK.md`;
  rotation delay for remote tiles (10 s default shows converted tiles ~half the time; owner
  to decide longer remote rotation or overlap); phone app remote live video
  (`mobile_app/UPCOMING_FIXES.md`; talk-back removed 2026-09-29); detector boxes on direct
  video (also no privacy masks on it); Cloudflare Web Analytics beacon blocked by the CSP
  (owner to disable automatic injection, do not widen the CSP); no AAAA record for `stun`
  (one harmless IPv6 STUN lookup error in browsers).

**Live verification pattern:** owner saves the dashboard password to
`/tmp/claude-1000/<project>/<session>/scratchpad/.dash_pw` (0600) with `read -rs`; one sign-in,
`shred -u` right after, read-only checks, sign out. Never fabricate/stub server responses.

**Site facts (2026-09-28, build 2240fc1):** NVR 192.168.20.160, 32 cameras = NVR channels
1-32 on subtype=1, all D1 704x576 (21 CIF channels upgraded, verified), sub-stream bit rate
768 kbps (ch25 1024), 31 on / 1 turned off. Duplicate typed-URL ch1 camera cam_b4fbe4dbd6
removed (history kept, excluded from totals); no camera is calibrated now (ch1 =
cam_1c7b1fe3a2 needs calibration for the floor map). Load after changes: ~193% CPU
(service 94% + 31 ffmpeg 99%), GPU mean 56%. Ch 3/9/13 near-black at night (check IR).

**State at e3bdc30 (pushed; live box at 5f2dc0f or e3bdc30 depending on last owner deploy):**
camera On/Off + rotating 4-feed grid with pins (4f52fc6); GPU decode VA-API/NVDEC via ffmpeg
(capture_backends, 4ffc298); per-camera decoder choice, scaled tile snapshots, gzip/immutable
caching, CSP, docs off, Online access card (4be2417); night watch (store-local schedules,
motion-gated inference, NIGHT_INTRUSION alerts) + lighting state (5f2dc0f); evidence-only
storage with oldest-first cap, DVR recorder removed (da529fc); CPU -47% (OpenCV pool,
NV12 reader, DECODE_MAX_FPS=5, DECODE_MAX_WIDTH=auto, resolution-independent geometry) (e3bdc30).
Measured at 5f2dc0f: CPU ~478% (box ~30%), GPU ~57% (budget 0.6), yolo26n-pose ~2.7 fps/camera.

**Fix round 2026-10-04 (commit 987d122, deployed to the box 2026-10-04 18:20 AEDT; follow-up 0c09615 "Default: NaN" fix pushed, deploy pending):** closes the important gaps from docs/USER_MANUAL.md. Area & line alerts card (zone_alerts.js, routes/events.py); accounts + enforced roles + change password (routes/users.py, users.js; auth_service.py `require_admin`, operator writes allowlisted in `_OPERATOR_ONLY` / `@operator_allowed`); 15 site settings in the dashboard, live (services/site_settings.py, system_setup key `site_settings`); backups/restore/upload/factory reset UI + nightly 03:00 store-time backup (DailyBackupScheduler) + till key (till_key.py, pos_ingest.py, header X-Edge-API-Key); privacy masks burned into direct WebRTC video, fail closed, 503 privacy_mask_unavailable (services/privacy_video.py); evidence links stored as paths, absolute only per caller (evidence_urls.py); file test cameras loop; STORAGE_DIR now also moves db/backups/media when set to a non-default dir; UI bug fixes; store-time-zone date fixes. Assets v=2.5.0. Full pytest 1217 passed; headless-Chrome check as owner, operator and stale-token states all PASS on a scratch copy. Docs updated (USER_MANUAL, FEATURES, REMOTE_VIDEO_CONTRACT, DEPLOYMENT). Live check after deploy (owner account, read-only): sign-in/stale tokens, all tabs, new Settings cards, Area & line alerts, recorder panel, sign-out PASS; storage stays /opt/edge-cctv/storage; direct video over the public address uses h264_vaapi (~5 % of a core per converted tile). Not yet exercised live: masked direct video (no privacy masks configured), "Add selected channels" (needs a scan). Open: go2rtc logs a codecs-not-matched WARNING+ERROR per H.265 session before transcoding (log noise); startup backup grew to 935 MB (was 103 MB) - investigate DB growth.

**Posters / ignore areas (9ffae53 + logger fix 22daf62, deployed 2026-10-04 21:13 AEDT; live: 2 remembered static figures loaded and suppressed within 3-8 s of restart on cam_a36424087a (human-sized, box 391,151-518,368) and cam_d5a39d8900):** AI_IGNORE areas drop a detection when its foot point is inside OR >= ignore_box_fraction (default 0.6, per area) of its box is inside (privacy_mask.outside_ignore_regions, polygon clipping, ~0.5 ms/frame). Camera setup has a separate "🚫 Ignore area (no AI)" tool + "🚫 Ignore areas" card; AI_IGNORE removed from the privacy Mask mode list; clicking a live box offers "Not a person — ignore this spot" (POST /api/zones/exclusion/from-box, Undo); /api/zones/clear?kinds=privacy|ignore. Static filter: memory persisted per camera in STORAGE_DIR/static_memory/<cam>.json (normalised, wall-clock TTL), a new track at a remembered spot or >=50 % inside an ignore area is "pending static" (not human) for the 5 s grace; a track that never moved writes no zone visit, customer_tracks row or heatmap points; box jitter floor, occlusion tolerance, gap bridging on memory hits. live-status/pipeline status add boxes, static_tracks, pending_tracks, static_memory_count, ignored_detections_last; tile HUD "· N static ignored". Known trade-off: a never-seen poster still counts live for its first 60 s.

**Store map + zone products + camera→zone links (2303db5, deployed by the owner and applied live 2026-10-07 ~23:00 AEDT):** m0017 adds `store_zones.products` (zone inspector "Products sold here"; named in findings, recommendations list and the Ollama prompt as `zone_sells`) and `cameras.watch_zone_id` (`PUT /api/v1/layout/cameras/{id}/zone`; an uncalibrated camera counts every person it sees into that zone via `live_engine.linked_zone`; ignored once calibrated; zone delete unlinks; `setup.cameras_zone_linked`). Zones must not overlap (first match wins). Store-map labels fit narrow shapes (`drawFittedLabel`). pytest 1250 passed; headless-Chrome scratch check PASS. Pearcedale map from the owner's hand sketch (2026-10-06 photo): 36 x 21 m estimate (gondolas 1.2 m, aisles 2 m, 12 m long; sketch not to scale), 91 structures, 16 non-overlapping zones (Produce wall aisle, Aisles 1-6, Back aisle Frozen/Dairy, Checkouts, Front walkway, Entrance, Ready meals/Hot chickens, Deli, Bottle shop, Cool room); generator `docs/supermarket_cctv/pearcedale_store_map/store_map.py`, applied with `apply_map.py` (signs in, saves the old layout, `--replace`). Live: replaced the old 50x30 m "Store Floor" (only a placeholder "Room 1"; backup JSON kept in the session scratchpad), now "IGA Pearcedale" 36x21 m, 16 zones (13 with products), 91 structures; floorplan zone metrics carry products. Open: all 32 cameras uncalibrated, none linked to a zone yet (camera stills could not be reviewed by the agent: owner links each camera via Store map -> camera -> "Counts into zone", or tells the agent channel->aisle); camera markers still at old placeholder positions, several outside the new 36x21 plan (use "Place"). Placeholders to confirm with the owner: right of aisle 6 and left of the bottle shop not on the photo, one produce-island label unreadable, real dimensions.

**Installed web app + phone alerts (Web Push), deployed 2026-10-08 17:07 AEDT (box c88f1d7; m0018 applied, pre-v0018 backup taken; store-name fix live 17:18 AEDT, box at 1d75536, manifest "IGA Pearcedale CCTV"):** owner decision 2026-10-08: the dashboard installed as a web app replaces the Flutter app for alerts; the Flutter app (`mobile_app/`) is kept as a backup, not deleted.
- Box: `services/web_push.py` (RFC 8291 aes128gcm + RFC 8292 VAPID with `cryptography` only, no new dependency; VAPID key = named secret `web_push_vapid`; endpoint allowlist fcm.googleapis.com / *.push.apple.com / mozilla / *.notify.windows.com); `services/push_alerts.py` (roster in system_setup `push_alert_roster`: >= 2 first priority, backup, escalate_after_min 5, min_confidence 0.75, night/area toggles, watchdog on + 15 min; until saved, fallback = all owners/admins, no escalation; escalation worker every 15 s; per-phone HMAC ack token so the notification's Acknowledge works without a session; "Acknowledged by" notice to everyone who got it); hooked into `alert_dispatcher.dispatch()` (report key `web_push`); m0018 tables `web_push_subscriptions`, `push_alerts`.
- API `routes/push.py` under **`/api/v1/web-push/*`**: `/api/v1/push/*` belongs to the Flutter FCM router in `routes/pairing.py`. They collided at first; found by the headless-Chrome run, covered by a test now.
- Web app: `/sw.js` and `/manifest.webmanifest` are root routes in `main.py` (manifest named after the store), `static/js/phone_alerts.js` + `css/phone_alerts.css` (header "📲 Install app": Chrome prompt / iOS steps; installed-app banner; Settings -> "Phone alerts" card: this phone, who gets alerts, connected phones, recent deliveries; `/dashboard?incident=<id>` deep link), icons in `static/icons/`.
- Store-offline watchdog: `services/offline_watchdog.py` uploads pre-encrypted, pre-signed "store offline" pushes to the VPS every 30 min and on change; contract `docs/OFFLINE_WATCHDOG_CONTRACT.md`; VPS side `~/Projects-1/Server/cctv-tunnel` (`cctv-watchdog` container, `POST https://tunnel.ikorex.com.au/watchdog/v1/bundle`, auth = store id + store token).
- Verified: pytest `tests/test_web_push_alerts.py` 20 passed (RFC 8291 vector, VAPID JWT, roster rules, threshold, escalation, ack token, 410 cleanup, permissions, watchdog bundle contract, back-online). Headless Chrome (persistent profile; incognito contexts refuse push) against a scratch box on 127.0.0.1: 15/15 PASS, including a real FCM round trip. Path: subscribe -> box -> fcm.googleapis.com -> Chrome -> service-worker notification, with Acknowledge/Open actions on a 0.86 incident, nothing for 0.60.
- Only the HTTPS public address can install the app and receive push. Tailscale/LAN http cannot. iPhone: Safari -> Add to Home Screen, iOS 16.4+, no install prompt, no custom sound, no time-sensitive level.
- Live checks 2026-10-08: box journal shows v17 -> v18 and "Created this box's Web Push (VAPID) key". Inference is MIGraphX. Public `/sw.js` returns 200 with `no-cache` and `Service-Worker-Allowed: /`. The manifest and icons return 200, and `/api/v1/web-push/config` returns 401 without a session. The VPS `cctv-watchdog` was applied at 17:10-17:12 and the box's first bundle arrived at 17:13:54 (`stored=0 enabled=no`: no phone has enabled alerts yet).
- Still to do: set up the phones (Android Chrome + iPhone): enable alerts, send a test, save the roster with 2 first-priority people. Optional drill: watchdog at 5 min, Online access off for 6 min.

**Pending/owner decisions:** gaze/attention beam plan; online-access items listed in the
block above; store manager switches
the 16 CIF channels to D1 sub-stream (needed by RTMO); body7 training-data legal check for
the RTMO/RTMPose weights; (done 2026-10-03 00:30 AEST: a38b391 static-filter / pose-only theft / patterns build deployed; box verified RTMO-only: models/ holds manifest + rtmo-s + rtmpose-s (refiner, off), startup logs `provider=MIGraphXExecutionProvider model=rtmo-s-body7-640x640-static.onnx`, no YOLO; first live static flag on cam_a36424087a within ~2 min); phone push (FCM)
not configured; skeleton-rotation report parked until reproduced.

**Client docs:** `docs/client/Edge_AI_CCTV_Features.pdf` (plain-language feature overview for customers, A4, 3 pages; last rebuilt 2026-10-01 with "Secure online access" + "Private live video" rows; rebuild with `uv run --no-project --with reportlab python docs/client/build_features_pdf.py`). Operator reference with configuration paths: `docs/FEATURES.md`. Step-by-step user manual (install, first sign-in, store mapping/commissioning order, daily use, maintenance, troubleshooting, "Not available yet" gap list): `docs/USER_MANUAL.md` (written 2026-10-03 against a38b391; UI labels checked against static/). Keep it in step with UI label changes.

**RTMO trial result (2026-09-28, brain #324):** RTMO-s better on D1/720p/>=1440p cameras (keeps 96-98% of YOLO's people, +25-33% more, fewer fixture false positives); worse on CIF 352x288 (keeps ~60%); IR/low-light inconclusive (night review samples were overwritten: sampler keeps only the newest 200). Decided 2026-09-28: switch to RTMO-s only (YOLO removed, CIF channels to D1); see the "RTMO only" block under Inference.

**Streams (brain #328):** Settings -> "Recorder sub-streams" upgrades Dahua CIF sub-streams to D1 (app/services/recorder_substreams.py, dahua_config.py; saves the previous config to STORAGE_DIR/dahua_substreams_<host>.json, verifies 704x576 on RTSP, restores on any failure, audit log recorder_audit.jsonl; API POST /api/v1/recorders/<host>/substreams/d1 {"all_cif":true}, restore .../restore). Per-camera "Stream quality" Auto|Sub-stream|Main (stream_selection.py): Auto = smallest sub-stream >= D1, else keep CIF; never auto-picks main.

**Models:** RTMO-s only (Apache-2.0); YOLO and the object model removed; refiner off by default; thresholds 0.50 / dark 0.45, TRACK_LOW_CONF_THRESHOLD 0.40. THIRD_PARTY_NOTICES.md lists model/library licences.

**Online access:** live and verified 2026-10-01; see "Online access + remote live video" above (brain #326/#327 for the VPS package history).
