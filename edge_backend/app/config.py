"""Application Configuration with safe fallback directory resolution."""

import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

def get_storage_root() -> Path:
    """Resolve the writable root that holds the database and all media subdirs."""
    if os.path.exists("/app") and os.access("/app", os.W_OK):
        root = Path("/app")
    else:
        root = Path(__file__).resolve().parent.parent.parent / "storage"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except Exception:
        root = Path("/tmp/cctv_storage")
        root.mkdir(parents=True, exist_ok=True)
    return root


def get_default_dir(name: str) -> Path:
    """Resolve a media subdirectory under the storage root.

    Note this must never be called with "storage": the root itself is
    get_storage_root(). Doing so previously produced a nested storage/storage
    directory that split the database away from the rest of the media tree.
    """
    p = get_storage_root() / name
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        p = Path(f"/tmp/cctv_{name}")
        p.mkdir(parents=True, exist_ok=True)
    return p

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="allow")
    
    APP_NAME: str = "Universal Edge AI CCTV System"
    APP_VERSION: str = "2.1.0"
    VERSION: str = "2.1.0"
    # DEBUG only makes logging more verbose. It never weakens security: the
    # one switch that bypasses authentication is AUTH_DISABLED, which is off
    # by default and logged loudly at startup when on.
    DEBUG: bool = False
    AUTH_DISABLED: bool = False
    # Echo every SQL statement to the log (very noisy). Independent of DEBUG.
    SQL_ECHO: bool = False
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))
    EDGE_BASE_URL: str = os.getenv("EDGE_BASE_URL", "http://localhost:8000")
    ALLOWED_CORS_ORIGINS: list[str] = ["*"]
    
    # Persistent Storage Paths
    STORAGE_DIR: Path = get_storage_root()
    SNAPSHOTS_DIR: Path = get_default_dir("snapshots")
    CLIPS_DIR: Path = get_default_dir("clips")
    DVR_DIR: Path = get_default_dir("dvr")
    ARCHIVES_DIR: Path = get_default_dir("archives")
    DATA_DIR: Path = get_default_dir("data")
    SQLITE_DB_PATH: Path = get_storage_root() / "cctv_core.db"
    DATABASE_PATH: Path = get_storage_root() / "cctv_core.db"
    BACKUPS_DIR: Path = get_default_dir("backups")
    # Backup retention (app/services/backup_service.py). Applies to automatic
    # "startup"/"auto" snapshots only; pre-migration ("pre-v*") and operator
    # backups are never pruned automatically.
    BACKUP_KEEP_STARTUP: int = int(os.getenv("BACKUP_KEEP_STARTUP", "7"))
    BACKUP_KEEP_DAILY_DAYS: int = int(os.getenv("BACKUP_KEEP_DAILY_DAYS", "14"))
    BACKUP_COMPRESS: bool = os.getenv("BACKUP_COMPRESS", "true").lower() in ("1", "true", "yes")
    # Person pose estimation (YOLO26 pose: box + 17 COCO keypoints). Which
    # accelerator runs it is probed at startup (see inference_backend.py);
    # the model is then chosen for that accelerator unless POSE_MODEL_PATH
    # pins one explicitly.
    MODELS_DIR: Path = Path(
        os.getenv("MODELS_DIR", str(Path(__file__).resolve().parent.parent / "models"))
    )
    POSE_MODEL_PATH: str = os.getenv("POSE_MODEL_PATH", "")          # empty -> auto
    POSE_MODEL_GPU: str = os.getenv("POSE_MODEL_GPU", "yolo26s-pose.onnx")   # GPU/NPU EPs
    POSE_MODEL_CPU: str = os.getenv("POSE_MODEL_CPU", "yolo26n-pose.onnx")   # CPU/OpenVINO
    # Larger / higher-resolution candidates, most accurate first. At startup
    # the detector measures each on the chosen provider and keeps the first
    # whose steady latency fits the budget (POSE_MODEL_GPU/_CPU are the floor).
    # Missing files are skipped, so a box without them behaves as before.
    POSE_MODEL_LADDER_GPU: str = os.getenv(
        "POSE_MODEL_LADDER_GPU", "yolo26m-pose-544x960.onnx,yolo26s-pose-544x960.onnx"
    )
    POSE_MODEL_LADDER_CPU: str = os.getenv(
        "POSE_MODEL_LADDER_CPU", "yolo26s-pose-544x960.onnx,yolo26n-pose-544x960.onnx"
    )
    # Per-frame latency budget for the pose model. 0 = derive it from the
    # number of enabled analytics cameras (POSE_BUDGET_STREAMS overrides the
    # count), RECORDING_FPS / ANALYTICS_DETECT_EVERY_N_FRAMES analysed frames
    # per camera per second, and POSE_BUDGET_UTILISATION of the accelerator.
    POSE_LATENCY_BUDGET_MS: float = float(os.getenv("POSE_LATENCY_BUDGET_MS", "0"))
    POSE_BUDGET_STREAMS: int = int(os.getenv("POSE_BUDGET_STREAMS", "0"))
    POSE_BUDGET_UTILISATION: float = float(os.getenv("POSE_BUDGET_UTILISATION", "0.6"))
    # Optional top-down keypoint refiner (RTMPose, Apache-2.0) run on each
    # person crop after the pose model: much better wrists/elbows on small or
    # far shoppers. "auto" = only on an accelerator and only when a batch of
    # POSE_REFINER_BUDGET_PERSONS crops fits in half the latency budget;
    # "on" / "off" force it. Visibility = max(pose vis, score x SCORE_SCALE).
    POSE_REFINER: str = os.getenv("POSE_REFINER", "auto")
    POSE_REFINER_MODEL: str = os.getenv("POSE_REFINER_MODEL", "rtmpose-s-256x192.onnx")
    POSE_REFINER_MAX_PERSONS: int = int(os.getenv("POSE_REFINER_MAX_PERSONS", "8"))
    # Crops per refiner run (fixed batch: shape changes are slow on CUDA).
    POSE_REFINER_BUDGET_PERSONS: int = int(os.getenv("POSE_REFINER_BUDGET_PERSONS", "4"))
    POSE_REFINER_SCORE_SCALE: float = float(os.getenv("POSE_REFINER_SCORE_SCALE", "1.0"))
    # Optional COCO object model for retail context (bags, bottles, phones).
    # Empty string disables it.
    OBJECT_MODEL_PATH: str = os.getenv(
        "OBJECT_MODEL_PATH", str(Path(__file__).resolve().parent.parent / "models" / "yolo26n.onnx")
    )
    OBJECT_DETECT_EVERY_N: int = int(os.getenv("OBJECT_DETECT_EVERY_N", "3"))
    # Class names (as in the model metadata) or COCO ids, comma separated.
    OBJECT_CLASSES: str = os.getenv("OBJECT_CLASSES", "backpack,handbag,suitcase,bottle,cell phone")
    OBJECT_CONF_THRESHOLD: float = float(os.getenv("OBJECT_CONF_THRESHOLD", "0.35"))
    OBJECT_NMS_IOU: float = float(os.getenv("OBJECT_NMS_IOU", "0.50"))
    # Accelerator selection. INFERENCE_DISABLED_PROVIDERS takes labels such
    # as "tensorrt,rocm" to skip a provider without rebuilding anything.
    INFERENCE_DEVICE_ID: int = int(os.getenv("INFERENCE_DEVICE_ID", "0"))
    INFERENCE_DISABLED_PROVIDERS: str = os.getenv("INFERENCE_DISABLED_PROVIDERS", "")
    INFERENCE_WARMUP_RUNS: int = int(os.getenv("INFERENCE_WARMUP_RUNS", "3"))
    TRT_ENGINE_CACHE_DIR: Path = Path(
        os.getenv("TRT_ENGINE_CACHE_DIR", str(get_storage_root() / "trt_cache"))
    )
    TRT_FP16: bool = os.getenv("TRT_FP16", "1").lower() not in ("0", "false", "no")
    PERSON_CONF_THRESHOLD: float = float(os.getenv("PERSON_CONF_THRESHOLD", "0.50"))
    PERSON_NMS_IOU: float = float(os.getenv("PERSON_NMS_IOU", "0.45"))
    # A keypoint counts as seen when its visibility score reaches this.
    KEYPOINT_VISIBILITY_THRESHOLD: float = float(os.getenv("KEYPOINT_VISIBILITY_THRESHOLD", "0.5"))
    # Geometry gates that reject implausible person boxes. A small model on an
    # unusual camera angle will confidently label floor texture or shelving as
    # a person; such boxes are almost always near-square or enormous, whereas a
    # standing shopper is markedly taller than wide and occupies a modest part
    # of the frame. Without these gates the pipeline records false positives as
    # real shoppers, which is indistinguishable downstream from fabricated data.
    PERSON_MIN_ASPECT_RATIO: float = float(os.getenv("PERSON_MIN_ASPECT_RATIO", "1.2"))
    PERSON_MAX_FRAME_FRACTION: float = float(os.getenv("PERSON_MAX_FRAME_FRACTION", "0.35"))
    PERSON_MIN_BOX_PIXELS: int = int(os.getenv("PERSON_MIN_BOX_PIXELS", "24"))
    # Analytics runs on a decimated stream: detection every Nth frame is ample
    # for footfall and dwell, and leaves decode budget for the other channels.
    ANALYTICS_DETECT_EVERY_N_FRAMES: int = int(os.getenv("ANALYTICS_DETECT_EVERY_N_FRAMES", "5"))
    TRACK_MAX_AGE_FRAMES: int = int(os.getenv("TRACK_MAX_AGE_FRAMES", "30"))
    TRACK_MIN_HITS: int = int(os.getenv("TRACK_MIN_HITS", "3"))
    # ByteTrack-style two-stage association. The engine asks the detector for
    # boxes down to TRACK_LOW_CONF_THRESHOLD; those low-confidence boxes can
    # only extend an existing track (occlusion recovery), never start one.
    # New tracks need TRACK_NEW_TRACK_THRESHOLD, which defaults to the person
    # threshold so the "no false shoppers" property is unchanged.
    TRACK_LOW_CONF_THRESHOLD: float = float(os.getenv("TRACK_LOW_CONF_THRESHOLD", "0.25"))
    TRACK_NEW_TRACK_THRESHOLD: float = float(
        os.getenv("TRACK_NEW_TRACK_THRESHOLD", os.getenv("PERSON_CONF_THRESHOLD", "0.50"))
    )
    TRACK_IOU_THRESHOLD: float = float(os.getenv("TRACK_IOU_THRESHOLD", "0.3"))
    TRACK_LOW_IOU_THRESHOLD: float = float(os.getenv("TRACK_LOW_IOU_THRESHOLD", "0.5"))
    TRACK_TENTATIVE_MAX_MISSES: int = int(os.getenv("TRACK_TENTATIVE_MAX_MISSES", "2"))
    # Per-track keypoint smoothing (tracking_service.smooth_keypoints): weight
    # of the newest observation for a joint that barely moved. Elbows/wrists
    # use TRACK_KEYPOINT_EMA_ARMS. Smoothing fades out between JITTER_FRAC and
    # FAST_FRAC of the person's box height moved per detection frame, so a
    # reach to a top shelf is not delayed or averaged away.
    TRACK_KEYPOINT_EMA: float = float(os.getenv("TRACK_KEYPOINT_EMA", "0.6"))
    TRACK_KEYPOINT_EMA_ARMS: float = float(os.getenv("TRACK_KEYPOINT_EMA_ARMS", "0.8"))
    TRACK_KEYPOINT_JITTER_FRAC: float = float(os.getenv("TRACK_KEYPOINT_JITTER_FRAC", "0.02"))
    TRACK_KEYPOINT_FAST_FRAC: float = float(os.getenv("TRACK_KEYPOINT_FAST_FRAC", "0.06"))
    # A person must linger this long inside a zone before it counts as dwell
    # rather than a pass-through.
    ZONE_DWELL_MIN_SECONDS: float = float(os.getenv("ZONE_DWELL_MIN_SECONDS", "3.0"))

    # Store identity & default premises extent (metres) for a fresh blueprint.
    STORE_ID: str = os.getenv("STORE_ID", "store_main")
    STORE_NAME: str = os.getenv("STORE_NAME", "Store")
    DEFAULT_STORE_WIDTH_M: float = float(os.getenv("DEFAULT_STORE_WIDTH_M", "50.0"))
    DEFAULT_STORE_HEIGHT_M: float = float(os.getenv("DEFAULT_STORE_HEIGHT_M", "30.0"))

    OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    SHM_DIR: Path = Path(os.getenv("SHM_DIR", "/dev/shm" if os.path.exists("/dev/shm") else "/tmp"))
    
    # Storage Retention & Purging Policies
    STORAGE_RETENTION_DAYS: int = int(os.getenv("STORAGE_RETENTION_DAYS", "7"))
    STORAGE_MAX_DISK_PERCENT: float = float(os.getenv("STORAGE_MAX_DISK_PERCENT", "85.0"))
    DVR_DEFAULT_RETENTION_DAYS: int = 7
    DVR_DEFAULT_QUOTA_GB: float = 100.0
    
    # go2rtc Media Gateway
    GO2RTC_API_URL: str = os.getenv("GO2RTC_API_URL", "http://127.0.0.1:1984")
    GO2RTC_WS_URL: str = os.getenv("GO2RTC_WS_URL", "ws://127.0.0.1:1984/api/ws")
    GO2RTC_CONFIG_PATH: Path = Path(os.getenv("GO2RTC_CONFIG_PATH", "./go2rtc.yaml"))
    
    # Hardware Devices
    VAAPI_DEVICE: str = os.getenv("VAAPI_DEVICE", "/dev/dri/renderD128")
    HAILO_DEVICE: str = os.getenv("HAILO_DEVICE", "/dev/hailo0")
    HAILO_YOLO_HEF_PATH: str = os.getenv("HAILO_YOLO_HEF_PATH", "./models_hef/yolov8n.hef")
    HAILO_POSE_HEF_PATH: str = os.getenv("HAILO_POSE_HEF_PATH", "./models_hef/yolov8n_pose.hef")
    
    # Security, JWT & Service Secrets.
    # No secret has a default in code. Each one resolves from the process
    # environment, then .env, then the machine-local store
    # <STORAGE_DIR>/secrets/device_secrets.json (generated on first start,
    # mode 0600; see app/services/secret_store.py). Placeholders such as
    # CHANGE_ME are treated as unset. Resolution happens in model_post_init.
    JWT_SECRET: str = ""
    JWT_ALGORITHM: str = "HS256"
    AUTH_SECRET_KEY: str = ""
    TOKEN_EXPIRY_SECONDS: int = 86400
    STREAM_TOKEN_EXPIRE_SECONDS: int = 86400
    STREAM_TOKEN_EXPIRY_SECONDS: int = 86400
    CLIP_TOKEN_EXPIRY_SECONDS: int = 86400
    INTERNAL_SERVICE_KEY: str = ""
    # Encrypts NVR/camera passwords at rest (storage/nvr_credentials.json).
    NVR_CREDENTIAL_KEY: str = ""

    # Optional TURN relay (docker compose profile "turn"). Off by default:
    # remote viewing goes over HTTPS through the remote-access tunnel (MJPEG),
    # so WebRTC is LAN-only and the ICE endpoint returns STUN only. Set
    # TURN_ENABLED=true (with COTURN_SECRET shared with the coturn container and
    # COTURN_PUBLIC_IP reachable from phones) only when running that profile.
    TURN_ENABLED: bool = False
    COTURN_SECRET: str = ""
    COTURN_REALM: str = os.getenv("COTURN_REALM", "cctv.local")
    COTURN_PUBLIC_IP: str = os.getenv("COTURN_PUBLIC_IP", "127.0.0.1")
    COTURN_PORT: int = int(os.getenv("COTURN_PORT", "3478"))

    def model_post_init(self, __context) -> None:
        """Fill every secret left unset (or set to a placeholder) from the store."""
        super().model_post_init(__context)
        from .services.secret_store import SECRET_NAMES, resolve_secrets

        provided = {name: getattr(self, name, "") for name in SECRET_NAMES}
        for name, value in resolve_secrets(self.STORAGE_DIR, provided).items():
            setattr(self, name, value)
    
    # Loss-prevention alerts: minimum seconds between two phone pushes for the
    # same camera and alert type. Every alert is still logged and sent to the
    # dashboard websocket; only the repeat push is held back.
    CAMERA_ALERT_COOLDOWN_SEC: float = float(os.getenv("CAMERA_ALERT_COOLDOWN_SEC", "30"))
    
    # Buffer & Clip Settings
    PRE_EVENT_BUFFER_SECONDS: int = 5
    POST_EVENT_RECORD_SECONDS: int = 10
    RECORDING_FPS: int = 25
    
    # Push notifications: Firebase Cloud Messaging HTTP v1 only (APNs via FCM).
    # The service-account JSON is uploaded in Settings and stored encrypted as
    # the named secret "fcm_service_account"; there is no env/.env setting.
    GOTIFY_URL: str = os.getenv("GOTIFY_URL", "http://127.0.0.1:8080")
    GOTIFY_APP_TOKEN: str = os.getenv("GOTIFY_APP_TOKEN", "")

    # ------------------------------------------------------------------
    # Pose analytics: shelf interactions & loss-prevention rules
    # (consumed by app/services/pose_analytics.py). Every threshold the
    # rules use lives here so a site can tune them without a code change.
    # ------------------------------------------------------------------
    # Minimum COCO keypoint visibility (0..1) for a keypoint to be trusted.
    INTERACTION_MIN_KEYPOINT_VIS: float = float(os.getenv("INTERACTION_MIN_KEYPOINT_VIS", "0.5"))
    # A wrist must be inside a product-zone polygon for this many consecutive
    # analysed frames before it counts as a reach (debounces jitter).
    INTERACTION_MIN_FRAMES: int = int(os.getenv("INTERACTION_MIN_FRAMES", "2"))
    # Frames a wrist may leave the zone mid-reach without ending the reach.
    INTERACTION_EXIT_GRACE_FRAMES: int = int(os.getenv("INTERACTION_EXIT_GRACE_FRAMES", "1"))
    # Bounded per-track history (analysed frames) of skeletons and wrists.
    INTERACTION_HISTORY_FRAMES: int = int(os.getenv("INTERACTION_HISTORY_FRAMES", "150"))
    # A wrist inside the body's "rest" box (torso widened by this fraction of
    # shoulder width) is an arm at rest, not a reach, even if a zone drawn over
    # the shelf behind the shopper contains it.
    INTERACTION_REST_MARGIN_FRAC: float = float(os.getenv("INTERACTION_REST_MARGIN_FRAC", "0.15"))
    # Use floor-plan SHELF zones (metres, via the camera homography) for cameras
    # that have no image-space product zones. Approximate: see pose_analytics.
    INTERACTION_FLOOR_SHELF_FALLBACK: bool = os.getenv("INTERACTION_FLOOR_SHELF_FALLBACK", "true").lower() in ("1", "true", "yes")
    # Forget a track's state after this many seconds without an observation.
    INTERACTION_TRACK_TTL_SEC: float = float(os.getenv("INTERACTION_TRACK_TTL_SEC", "10.0"))
    # How often the background writer persists queued interactions/incidents.
    INTERACTION_FLUSH_INTERVAL_SEC: float = float(os.getenv("INTERACTION_FLUSH_INTERVAL_SEC", "2.0"))
    # --- reach mapping (pose_analytics._update_hands) ---
    # The hand extends past the wrist along the elbow->wrist direction by about
    # this fraction of the forearm length; that "hand tip" is the point tested
    # against product zones (fingers reach a top shelf before the wrist does).
    # 0 disables it (wrist only). Needs a visible elbow; otherwise the wrist.
    INTERACTION_HAND_EXTEND_FRAC: float = float(os.getenv("INTERACTION_HAND_EXTEND_FRAC", "0.35"))
    # A reach also needs this long inside the zone (in addition to
    # INTERACTION_MIN_FRAMES), so the rule behaves the same at 5 and 25 fps.
    INTERACTION_MIN_SEC: float = float(os.getenv("INTERACTION_MIN_SEC", "0.2"))
    # The same hand re-entering the same zone within this many seconds of
    # leaving continues the earlier reach (boundary jitter is one reach).
    INTERACTION_REENTRY_MERGE_SEC: float = float(os.getenv("INTERACTION_REENTRY_MERGE_SEC", "0.5"))
    # A hand that becomes invisible mid-reach (occluded by the shopper's own
    # body, or by the product) holds the reach open this long before it ends.
    INTERACTION_OCCLUSION_GRACE_SEC: float = float(os.getenv("INTERACTION_OCCLUSION_GRACE_SEC", "1.0"))
    # A shopper whose hips move faster than this many torso lengths per second
    # is walking past: a hand swinging over a shelf zone is not a reach.
    INTERACTION_MAX_BODY_SPEED: float = float(os.getenv("INTERACTION_MAX_BODY_SPEED", "1.5"))
    # An arm this straight (|shoulder->wrist| / (|shoulder->elbow| + |elbow->wrist|))
    # and not hanging down is reaching even when, foreshortened, its wrist
    # projects over the torso "rest" box.
    INTERACTION_EXTENDED_ARM_RATIO: float = float(os.getenv("INTERACTION_EXTENDED_ARM_RATIO", "0.85"))

    # Concealment: after a reach ends, the wrist must enter the lower-torso /
    # pocket region (or a detected bag) within THEFT_CONCEAL_WINDOW_SEC, stay
    # there THEFT_CONCEAL_MIN_HOLD_FRAMES frames, and not return to a shelf
    # within THEFT_CONCEAL_NO_RETURN_SEC.
    THEFT_CONCEAL_WINDOW_SEC: float = float(os.getenv("THEFT_CONCEAL_WINDOW_SEC", "4.0"))
    THEFT_CONCEAL_MIN_HOLD_FRAMES: int = int(os.getenv("THEFT_CONCEAL_MIN_HOLD_FRAMES", "3"))
    THEFT_CONCEAL_NO_RETURN_SEC: float = float(os.getenv("THEFT_CONCEAL_NO_RETURN_SEC", "2.0"))
    # Top of the concealment band as a fraction of shoulder->hip distance
    # (0 = shoulder line). 0.4 excludes the chest, where people read labels.
    THEFT_CONCEAL_REGION_TOP_FRAC: float = float(os.getenv("THEFT_CONCEAL_REGION_TOP_FRAC", "0.4"))
    # How far below the hip line the band extends (pockets), same unit.
    THEFT_CONCEAL_REGION_BOTTOM_FRAC: float = float(os.getenv("THEFT_CONCEAL_REGION_BOTTOM_FRAC", "0.3"))
    # Visibility accepted for a wrist inside the concealment band: a hand at a
    # waistband is often partly occluded, so this is lower than the general gate.
    THEFT_CONCEAL_MIN_WRIST_VIS: float = float(os.getenv("THEFT_CONCEAL_MIN_WRIST_VIS", "0.25"))
    # COCO object classes treated as bags (backpack, handbag, suitcase).
    THEFT_BAG_CLASS_IDS: str = os.getenv("THEFT_BAG_CLASS_IDS", "24,26,28")
    # Shelf sweeping: this many reaches into one zone inside the window.
    THEFT_SWEEP_WINDOW_SEC: float = float(os.getenv("THEFT_SWEEP_WINDOW_SEC", "10.0"))
    THEFT_SWEEP_MIN_REACHES: int = int(os.getenv("THEFT_SWEEP_MIN_REACHES", "4"))
    # Suspicious loitering near high-value product zones.
    THEFT_HIGH_VALUE_CATEGORIES: str = os.getenv(
        "THEFT_HIGH_VALUE_CATEGORIES",
        "ALCOHOL,SPIRITS,LIQUOR,WINE,ELECTRONICS,COSMETICS,BEAUTY,FRAGRANCE,BABY_FORMULA,RAZORS,TOBACCO,MEDICINE,PHARMACY",
    )
    # A product zone priced at or above this is high value regardless of category (0 disables).
    THEFT_HIGH_VALUE_MIN_PRICE: float = float(os.getenv("THEFT_HIGH_VALUE_MIN_PRICE", "0"))
    THEFT_LOITER_MIN_DWELL_SEC: float = float(os.getenv("THEFT_LOITER_MIN_DWELL_SEC", "60.0"))
    THEFT_LOITER_MIN_REACHES: int = int(os.getenv("THEFT_LOITER_MIN_REACHES", "2"))
    THEFT_LOITER_MIN_HEAD_TURNS: int = int(os.getenv("THEFT_LOITER_MIN_HEAD_TURNS", "6"))
    # Gap in presence near the zone that still counts as one continuous dwell.
    THEFT_LOITER_GAP_SEC: float = float(os.getenv("THEFT_LOITER_GAP_SEC", "3.0"))
    # A high-value zone within this many torso lengths of the shoulders counts as "near".
    THEFT_LOITER_REACH_TORSOS: float = float(os.getenv("THEFT_LOITER_REACH_TORSOS", "1.5"))
    # |yaw proxy| beyond this is "looking to one side"; a flip between sides is a head turn.
    THEFT_HEAD_TURN_YAW: float = float(os.getenv("THEFT_HEAD_TURN_YAW", "0.35"))
    # Exit without checkout (needs a calibrated camera and ENTRANCE/EXIT + CHECKOUT zones).
    THEFT_EXIT_RULE_ENABLED: bool = os.getenv("THEFT_EXIT_RULE_ENABLED", "true").lower() in ("1", "true", "yes")
    # Sweethearting: seconds between a checkout hand pass and a POS scan to count as matched.
    THEFT_POS_MATCH_TOLERANCE_SEC: float = float(os.getenv("THEFT_POS_MATCH_TOLERANCE_SEC", "2.0"))
    # One incident per track and rule inside this window.
    THEFT_INCIDENT_COOLDOWN_SEC: float = float(os.getenv("THEFT_INCIDENT_COOLDOWN_SEC", "120.0"))
    # Incidents below this evidence-derived confidence are not raised.
    THEFT_MIN_CONFIDENCE: float = float(os.getenv("THEFT_MIN_CONFIDENCE", "0.3"))
    # Reference durations (seconds) at which a rule's duration evidence saturates to ~63%.
    THEFT_CONFIDENCE_DURATION_REF_SEC: float = float(os.getenv("THEFT_CONFIDENCE_DURATION_REF_SEC", "1.5"))
    # Where evidence JPEGs are written; empty means <STORAGE_DIR>/theft_evidence.
    THEFT_EVIDENCE_DIR: str = os.getenv("THEFT_EVIDENCE_DIR", "")
    THEFT_EVIDENCE_JPEG_QUALITY: int = int(os.getenv("THEFT_EVIDENCE_JPEG_QUALITY", "85"))

    # ---------------- Tripwires & restricted areas (services/tripwire_engine.py) ----------------
    # Dead band either side of a tripwire, in pixels, as the larger of a
    # fraction of the frame diagonal and a fraction of the person's box height.
    # A foot point inside the band never changes side, so jitter on the line
    # cannot count; a crossing is counted only after the point is clear of it.
    TRIPWIRE_HYSTERESIS_FRAC: float = float(os.getenv("TRIPWIRE_HYSTERESIS_FRAC", "0.012"))
    TRIPWIRE_HYSTERESIS_BOX_FRAC: float = float(os.getenv("TRIPWIRE_HYSTERESIS_BOX_FRAC", "0.08"))
    # Consecutive analysed frames the foot point must stay on the new side.
    TRIPWIRE_CONFIRM_FRAMES: int = int(os.getenv("TRIPWIRE_CONFIRM_FRAMES", "2"))
    # A crossing is recorded once the person has stayed on the new side this
    # long (or left the view there); crossing straight back sooner cancels it.
    TRIPWIRE_MIN_REPEAT_SEC: float = float(os.getenv("TRIPWIRE_MIN_REPEAT_SEC", "1.0"))
    # Per (tripwire, track) cooldown between TRIPWIRE_ALERT notifications.
    TRIPWIRE_ALERT_COOLDOWN_SEC: float = float(os.getenv("TRIPWIRE_ALERT_COOLDOWN_SEC", "60"))
    # Per (area, track) cooldown between RESTRICTED_AREA alerts.
    RESTRICTED_AREA_COOLDOWN_SEC: float = float(os.getenv("RESTRICTED_AREA_COOLDOWN_SEC", "300"))
    # A foot point may leave an area this long (boundary jitter) without resetting its dwell.
    RESTRICTED_AREA_EXIT_GRACE_SEC: float = float(os.getenv("RESTRICTED_AREA_EXIT_GRACE_SEC", "1.5"))
    # IANA zone for restricted-area schedules; empty = the host's local time zone.
    SITE_TIMEZONE: str = os.getenv("SITE_TIMEZONE", "")
    # Where alert snapshots are written; empty means <STORAGE_DIR>/zone_alerts.
    ZONE_ALERT_EVIDENCE_DIR: str = os.getenv("ZONE_ALERT_EVIDENCE_DIR", "")

    # Network camera open / read timeouts for the live workers (live_analytics_engine._open).
    CAMERA_OPEN_TIMEOUT_SEC: float = float(os.getenv("CAMERA_OPEN_TIMEOUT_SEC", "8"))
    CAMERA_READ_TIMEOUT_SEC: float = float(os.getenv("CAMERA_READ_TIMEOUT_SEC", "10"))

    # ---------------- Footfall track quality (services/retail_metrics_service.py) ----------------
    # Occlusion and detector flicker split one person into many ~1 s tracks.
    # Footfall, dwell and funnel figures count a track / zone visit only when it
    # lasted this long, and (tracks) was matched on at least this many frames.
    FOOTFALL_MIN_TRACK_SECONDS: float = float(os.getenv("FOOTFALL_MIN_TRACK_SECONDS", "3.0"))
    FOOTFALL_MIN_TRACK_HITS: int = int(os.getenv("FOOTFALL_MIN_TRACK_HITS", os.getenv("TRACK_MIN_HITS", "3")))


settings = Settings()
