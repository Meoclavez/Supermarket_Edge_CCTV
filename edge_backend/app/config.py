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

# Paths that live under STORAGE_DIR unless set on their own: field -> path
# relative to STORAGE_DIR. With STORAGE_DIR unset (or set to the default root)
# they keep the defaults above, so the default layout is unchanged.
STORAGE_DERIVED_PATHS = {
    "SNAPSHOTS_DIR": "snapshots",
    "CLIPS_DIR": "clips",
    "DVR_DIR": "dvr",
    "ARCHIVES_DIR": "archives",
    "DATA_DIR": "data",
    "DATABASE_PATH": "cctv_core.db",
    "BACKUPS_DIR": "backups",
    "TRT_ENGINE_CACHE_DIR": "trt_cache",
    "MIGRAPHX_CACHE_DIR": "migraphx_cache",
}
_STORAGE_DERIVED_FILES = {"DATABASE_PATH"}


class Settings(BaseSettings):
    # Absolute path: a relative ".env" was only found when the process started in
    # edge_backend/, and a launch from anywhere else silently fell back to the
    # machine-local secret store with a different JWT_SECRET, invalidating
    # every existing session.
    model_config = SettingsConfigDict(env_file=str(Path(__file__).resolve().parent.parent / ".env"), extra="allow")
    
    APP_NAME: str = "Universal Edge AI CCTV System"
    APP_VERSION: str = "2.1.0"
    VERSION: str = "2.1.0"
    # DEBUG makes logging more verbose and serves the API docs (/docs, /redoc,
    # /openapi.json) on the local address; never through the public hostname.
    # It never disables authentication: the one switch that does is
    # AUTH_DISABLED, which is off by default and logged loudly when on.
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
    # Person pose estimation: RTMO-s (one-stage, mmpose, Apache-2.0; box + 17
    # COCO keypoints, static 640x640 export, see models/manifest.json). Which
    # accelerator runs it is probed at startup (see inference_backend.py);
    # the model is then chosen for that accelerator unless POSE_MODEL_PATH
    # pins one explicitly. Any model with the RTMO output layout (dets
    # [1,K,5] + keypoints [1,K,17,3]) can be named here.
    MODELS_DIR: Path = Path(
        os.getenv("MODELS_DIR", str(Path(__file__).resolve().parent.parent / "models"))
    )
    POSE_MODEL_PATH: str = os.getenv("POSE_MODEL_PATH", "")          # empty -> auto
    POSE_MODEL_GPU: str = os.getenv("POSE_MODEL_GPU", "rtmo-s-body7-640x640-static.onnx")   # GPU/NPU EPs
    POSE_MODEL_CPU: str = os.getenv("POSE_MODEL_CPU", "rtmo-s-body7-640x640-static.onnx")   # CPU/OpenVINO
    # Optional larger / higher-resolution candidates (file names under
    # MODELS_DIR, comma separated), most accurate first. At startup the
    # detector measures each on the chosen provider and keeps the first whose
    # steady latency fits the budget (POSE_MODEL_GPU/_CPU are the floor); the
    # scheduler steps between them at run time. Empty by default: only RTMO-s
    # ships. Missing files are skipped.
    POSE_MODEL_LADDER_GPU: str = os.getenv("POSE_MODEL_LADDER_GPU", "")
    POSE_MODEL_LADDER_CPU: str = os.getenv("POSE_MODEL_LADDER_CPU", "")
    # Per-frame latency budget for the pose model. 0 = derive it from the
    # number of enabled analytics cameras (POSE_BUDGET_STREAMS overrides the
    # count), the analysed frames per camera per second (RECORDING_FPS /
    # ANALYTICS_DETECT_EVERY_N_FRAMES, capped at ANALYTICS_TARGET_DETECT_FPS
    # while the scheduler is on), and POSE_BUDGET_UTILISATION: the share of
    # the accelerator's measured capacity that inference may plan for. The
    # scheduler (ANALYTICS_SCHEDULER) holds the live load to the same share.
    POSE_LATENCY_BUDGET_MS: float = float(os.getenv("POSE_LATENCY_BUDGET_MS", "0"))
    POSE_BUDGET_STREAMS: int = int(os.getenv("POSE_BUDGET_STREAMS", "0"))
    POSE_BUDGET_UTILISATION: float = float(os.getenv("POSE_BUDGET_UTILISATION", "0.6"))
    # Optional top-down keypoint refiner (RTMPose, Apache-2.0) run on each
    # person crop after the pose model. "auto" = only on an accelerator and
    # only when a batch of POSE_REFINER_BUDGET_PERSONS crops fits in half the
    # latency budget; "on" / "off" force it. Visibility = max(pose vis, score
    # x SCORE_SCALE). Off by default: on RTMO keypoints it does not help
    # (COCO persons at 704x576, mean OKS 0.872 -> 0.853, persons < 100 px
    # 0.812 -> 0.758, dark -3 EV 0.854 -> 0.826) and it lowers keypoint
    # precision at the 0.5 visibility gate (0.879 -> 0.804).
    POSE_REFINER: str = os.getenv("POSE_REFINER", "off")
    POSE_REFINER_MODEL: str = os.getenv("POSE_REFINER_MODEL", "rtmpose-s-256x192.onnx")
    POSE_REFINER_MAX_PERSONS: int = int(os.getenv("POSE_REFINER_MAX_PERSONS", "8"))
    # Crops per refiner run (fixed batch: shape changes are slow on CUDA).
    POSE_REFINER_BUDGET_PERSONS: int = int(os.getenv("POSE_REFINER_BUDGET_PERSONS", "4"))
    POSE_REFINER_SCORE_SCALE: float = float(os.getenv("POSE_REFINER_SCORE_SCALE", "1.0"))
    # A joint takes the refiner's position only when its refiner score
    # (x POSE_REFINER_SCORE_SCALE) reaches this; otherwise the pose model's
    # joint stands. 0 = every joint (measured best: gating at 0.3-0.7 cost
    # part of the refiner's keypoint accuracy gain).
    POSE_REFINER_MIN_SCORE: float = float(os.getenv("POSE_REFINER_MIN_SCORE", "0"))
    # A refined skeleton whose shoulder-to-hip axis is turned more than this
    # from the pose model's is discarded (the refiner turned a blurred or
    # dark person on its side); 0 disables the check.
    POSE_REFINER_MAX_TURN_DEG: float = float(os.getenv("POSE_REFINER_MAX_TURN_DEG", "45"))
    # Shadow pose-model trial (services/shadow_trial.py), a developer
    # experiment: a second pose model runs on frames the live model already
    # analysed and only aggregate agreement figures are kept. It never feeds
    # tracking, counts or alerts. SHADOW_POSE_MODEL: file under MODELS_DIR
    # (empty = off). SHADOW_POSE_SHARE: fraction of wall time the shadow model
    # may keep the accelerator busy. SHADOW_POSE_CAMERAS: camera ids (empty =
    # all). SHADOW_TRIAL_WINDOW: local "HH:MM-HH:MM" (may cross midnight;
    # empty = always). Review samples (disagreements only, privacy masks
    # burned in) live in STORAGE_DIR/shadow_trial, capped by count and
    # SHADOW_SAMPLES_MAX_MB inside the evidence limit, and the whole directory
    # is deleted SHADOW_TRIAL_RETAIN_DAYS after the trial last recorded data.
    SHADOW_POSE_MODEL: str = os.getenv("SHADOW_POSE_MODEL", "")
    SHADOW_POSE_SHARE: float = float(os.getenv("SHADOW_POSE_SHARE", "0.15"))
    SHADOW_POSE_CAMERAS: str = os.getenv("SHADOW_POSE_CAMERAS", "")
    SHADOW_TRIAL_WINDOW: str = os.getenv("SHADOW_TRIAL_WINDOW", "")
    SHADOW_SAMPLES_MAX: int = int(os.getenv("SHADOW_SAMPLES_MAX", "200"))
    SHADOW_SAMPLES_MAX_MB: float = float(os.getenv("SHADOW_SAMPLES_MAX_MB", "40"))
    SHADOW_SAMPLE_EVERY_SEC: float = float(os.getenv("SHADOW_SAMPLE_EVERY_SEC", "120"))
    SHADOW_STATIC_MINUTES: float = float(os.getenv("SHADOW_STATIC_MINUTES", "10"))
    SHADOW_TRIAL_RETAIN_DAYS: float = float(os.getenv("SHADOW_TRIAL_RETAIN_DAYS", "7"))
    # Accelerator selection. INFERENCE_DISABLED_PROVIDERS takes labels such
    # as "tensorrt,rocm" to skip a provider without rebuilding anything.
    INFERENCE_DEVICE_ID: int = int(os.getenv("INFERENCE_DEVICE_ID", "0"))
    INFERENCE_DISABLED_PROVIDERS: str = os.getenv("INFERENCE_DISABLED_PROVIDERS", "")
    INFERENCE_WARMUP_RUNS: int = int(os.getenv("INFERENCE_WARMUP_RUNS", "3"))
    TRT_ENGINE_CACHE_DIR: Path = Path(
        os.getenv("TRT_ENGINE_CACHE_DIR", str(get_storage_root() / "trt_cache"))
    )
    TRT_FP16: bool = os.getenv("TRT_FP16", "1").lower() not in ("0", "false", "no")
    # AMD GPUs (ONNX Runtime MIGraphX plugin EP, app/services/amd_migraphx.py).
    # Compiled GPU programs are cached here, one subdirectory per GPU target,
    # library versions and precision; scripts/bootstrap.py pre-compiles the
    # models the service will load. On a cold cache the service starts on the
    # CPU and compiles for the GPU in the background ("background"), or blocks
    # start-up until compiled ("foreground").
    MIGRAPHX_CACHE_DIR: Path = Path(
        os.getenv("MIGRAPHX_CACHE_DIR", str(get_storage_root() / "migraphx_cache"))
    )
    MIGRAPHX_FP16: bool = os.getenv("MIGRAPHX_FP16", "0").lower() in ("1", "true", "yes")
    MIGRAPHX_COMPILE_MODE: str = os.getenv("MIGRAPHX_COMPILE_MODE", "background")
    # Let the thread that waits for the GPU sleep instead of spinning on a CPU
    # core for the whole inference (HIP blocking sync): session.run cost 5.8
    # ms of CPU per 5.9 ms run spinning, 2.8 ms blocking, same speed (RX 9060
    # XT, a 640x640 pose model) - about 25 % of a core at 90 analysed frames/s.
    MIGRAPHX_BLOCKING_SYNC: bool = os.getenv("MIGRAPHX_BLOCKING_SYNC", "1").lower() in ("1", "true", "yes")
    # ONNX Runtime threads for sessions on the CPU provider, shared by the pose
    # model, the keypoint refiner and a shadow-trial model (0 = half the CPUs
    # this process may run on), so CPU inference cannot starve camera
    # decoding and recording. Idle worker
    # threads do not spin unless INFERENCE_CPU_SPINNING is on.
    INFERENCE_CPU_THREADS: int = int(os.getenv("INFERENCE_CPU_THREADS", "0"))
    INFERENCE_CPU_SPINNING: bool = os.getenv("INFERENCE_CPU_SPINNING", "0").lower() in ("1", "true", "yes")
    PERSON_CONF_THRESHOLD: float = float(os.getenv("PERSON_CONF_THRESHOLD", "0.50"))
    # A person whose own box is dark (mean luminance below PERSON_DARK_LUMA,
    # 0-255) needs only PERSON_CONF_THRESHOLD_DARK: the model's confidence
    # falls with the light, and a box-local rule lifts recall in shade and at
    # night without lowering the bar in the lit parts of the same frame.
    # RTMO-s, COCO persons at 704x576 (-3 EV + noise for dark): 0.50 / 0.45
    # gives precision/recall 0.961/0.459 lit and 0.952/0.436 dark; a 0.35 dark
    # threshold dropped dark precision to 0.914. Set equal to
    # PERSON_CONF_THRESHOLD to disable.
    PERSON_CONF_THRESHOLD_DARK: float = float(os.getenv("PERSON_CONF_THRESHOLD_DARK", "0.45"))
    PERSON_DARK_LUMA: float = float(os.getenv("PERSON_DARK_LUMA", "60"))
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
    # A box above PERSON_MAX_FRAME_FRACTION (or the camera's own
    # person_max_frame_fraction) is still kept when its keypoints form a
    # coherent skeleton (shoulders + hips or head), up to this fraction.
    PERSON_MAX_FRAME_FRACTION_WITH_SKELETON: float = float(
        os.getenv("PERSON_MAX_FRAME_FRACTION_WITH_SKELETON", "0.9"))
    # Minimum box height in pixels; the width floor is separate
    # (PERSON_MIN_BOX_WIDTH_PIXELS): a distant shopper on a 352x288 sub-stream
    # is ~60 px tall but only 16-24 px wide.
    PERSON_MIN_BOX_PIXELS: int = int(os.getenv("PERSON_MIN_BOX_PIXELS", "24"))
    PERSON_MIN_BOX_WIDTH_PIXELS: int = int(os.getenv("PERSON_MIN_BOX_WIDTH_PIXELS", "16"))
    # Measured lighting per camera (services/low_light.py): every inferred
    # frame's luminance and colour saturation give a state (day | mixed |
    # low_light | ir), with hysteresis, never from the clock; reported in the
    # detector status. LOW_LIGHT_ENHANCE additionally applies CLAHE on the
    # luminance of the model input in non-day states (auto), on every frame
    # (always), or never (off, the default): measured on COCO persons it
    # LOWERED recall in every dark condition (e.g. -3 EV 0.36 -> 0.26, shade
    # 0.48 -> 0.46), because it amplifies sensor noise and JPEG blocking.
    LOW_LIGHT_ENHANCE: str = os.getenv("LOW_LIGHT_ENHANCE", "off")
    LOW_LIGHT_DARK_LEVEL: float = float(os.getenv("LOW_LIGHT_DARK_LEVEL", "50"))        # luma 0-255
    LOW_LIGHT_MEAN_LEVEL: float = float(os.getenv("LOW_LIGHT_MEAN_LEVEL", "70"))        # mean luma below -> low_light
    LOW_LIGHT_MIXED_DARK_FRACTION: float = float(os.getenv("LOW_LIGHT_MIXED_DARK_FRACTION", "0.30"))
    LOW_LIGHT_IR_SATURATION: float = float(os.getenv("LOW_LIGHT_IR_SATURATION", "12"))  # mean saturation 0-255
    LOW_LIGHT_DEAD_BAND: float = float(os.getenv("LOW_LIGHT_DEAD_BAND", "0.15"))       # relative
    LOW_LIGHT_HYSTERESIS_FRAMES: int = int(os.getenv("LOW_LIGHT_HYSTERESIS_FRAMES", "5"))
    LOW_LIGHT_CLAHE_CLIP: float = float(os.getenv("LOW_LIGHT_CLAHE_CLIP", "2.0"))
    LOW_LIGHT_CLAHE_TILES: int = int(os.getenv("LOW_LIGHT_CLAHE_TILES", "8"))
    # Analytics runs on a decimated stream: detection every Nth frame is ample
    # for footfall and dwell, and leaves decode budget for the other channels.
    ANALYTICS_DETECT_EVERY_N_FRAMES: int = int(os.getenv("ANALYTICS_DETECT_EVERY_N_FRAMES", "5"))
    # Global inference budget (services/inference_scheduler.py). Every Nth
    # frame of every camera needs more accelerator time than one GPU has once
    # there are dozens of cameras (33 x 5/s x 14 ms = 2.3 s of GPU per second),
    # so the scheduler shares POSE_BUDGET_UTILISATION of the measured capacity
    # fairly between the cameras that are analysing: a frame that arrives while
    # its camera has no token is simply not inferred (video and recording are
    # unaffected). Each camera gets at least ANALYTICS_MIN_DETECT_FPS and never
    # more than its fps / ANALYTICS_DETECT_EVERY_N_FRAMES. The pose model (and
    # the "auto" keypoint refiner, shed first) is re-fitted so every camera can
    # get ANALYTICS_TARGET_DETECT_FPS, ANALYTICS_REFIT_DEBOUNCE_SEC after the
    # camera set or the load last changed. Off = the old every-Nth-frame rule.
    ANALYTICS_SCHEDULER: bool = os.getenv("ANALYTICS_SCHEDULER", "1").lower() not in ("0", "false", "no", "off")
    ANALYTICS_MIN_DETECT_FPS: float = float(os.getenv("ANALYTICS_MIN_DETECT_FPS", "1.0"))
    ANALYTICS_TARGET_DETECT_FPS: float = float(os.getenv("ANALYTICS_TARGET_DETECT_FPS", "2.0"))
    ANALYTICS_REFIT_DEBOUNCE_SEC: float = float(os.getenv("ANALYTICS_REFIT_DEBOUNCE_SEC", "30"))
    # Analysed frames in flight at once across all cameras (one on the
    # accelerator, the rest in pre/post-processing); a camera whose turn comes
    # while this many are in flight skips that frame instead of queueing.
    ANALYTICS_MAX_INFLIGHT: int = int(os.getenv("ANALYTICS_MAX_INFLIGHT", "4"))
    TRACK_MAX_AGE_FRAMES: int = int(os.getenv("TRACK_MAX_AGE_FRAMES", "30"))
    TRACK_MIN_HITS: int = int(os.getenv("TRACK_MIN_HITS", "3"))
    # ByteTrack-style two-stage association. The engine asks the detector for
    # boxes down to TRACK_LOW_CONF_THRESHOLD; those low-confidence boxes can
    # only extend an existing track (occlusion recovery), never start one.
    # New tracks need TRACK_NEW_TRACK_THRESHOLD, which defaults to the person
    # threshold so the "no false shoppers" property is unchanged. RTMO-s
    # scores low boxes differently from the earlier model: its precision at
    # 0.40 (0.94 on 704x576 COCO persons) is what that model had at 0.25
    # (RTMO at 0.25: 0.84), so 0.40 keeps the extension boxes as clean.
    TRACK_LOW_CONF_THRESHOLD: float = float(os.getenv("TRACK_LOW_CONF_THRESHOLD", "0.40"))
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
    # Static-figure filter (tracking_service.ByteTracker): a poster, cut-out or
    # mannequin the pose model keeps recognising is a "static" track, shown
    # grey on the live view but excluded from counts, footfall, heatmaps,
    # zone rules and theft analytics. Per camera it can be switched off and
    # STATIC_FIGURE_SECONDS overridden (features static_figure_filter /
    # static_figure_seconds); these are the server defaults.
    #
    # Motion is measured per joint against an anchor pose, normalised by the
    # torso length (shoulder-mid to hip-mid; 0.3 x box height when the torso
    # is not visible), and must exceed STATIC_FIGURE_MOTION_FRAC on two
    # consecutive detection frames to count. Why 0.15: RTMO keypoints on an
    # unchanging image (a poster) still jitter with sensor/compression noise
    # by a few pixels, about TRACK_KEYPOINT_JITTER_FRAC (2 %) of the box
    # height, i.e. ~0.07 of the torso (torso ~ 0.3 x height). The anchor is
    # the mean of 5 still frames and the pose is jitter-smoothed, so 0.15 is
    # ~3.5-4 sigma per joint for a 1 %-of-height jitter; with the two-frame
    # rule a simulated poster (250 px box, Gaussian jitter sigma 2.5 px per
    # axis) was flagged at 60 s and never un-flagged in 10 x 10 min. A real person standing still (cashier, someone reading a label)
    # turns the head (nose/eyes move 10-15 cm, 0.2-0.3 torso) or moves a hand
    # (wrist 10-30 cm) many times a minute, well above 0.15 torso (~7-8 cm).
    # Measured with the real RTMO model (noisy JPEG frames of a still photo,
    # 2026-10-02), small figures (box 150-230 px) can have a wrist that flips
    # by 0.3-0.45 torso between frames: each joint therefore also has its own
    # jitter floor (tracking_service.MOTION_JITTER_K x the 75th percentile
    # of its frame-to-frame step) and is compared as the median of its last
    # 3 positions. With it every poster figure at 1.0/0.5/0.33 scale went static
    # at ~60 s; without it one never did.
    # A figure whose jitter still clears the threshold fails open: it
    # stays "moving" and is counted as before, never silently dropped.
    STATIC_FIGURE_SECONDS: float = float(os.getenv("STATIC_FIGURE_SECONDS", "60"))
    STATIC_FIGURE_MOTION_FRAC: float = float(os.getenv("STATIC_FIGURE_MOTION_FRAC", "0.15"))
    # A new track on a remembered static box (same box, IoU >= STATIC_MEMORY_IOU,
    # and the same pose) is static after only this long without motion:
    # poster tracks flicker and come back with new ids.
    STATIC_FIGURE_GRACE_SEC: float = float(os.getenv("STATIC_FIGURE_GRACE_SEC", "5"))
    STATIC_MEMORY_IOU: float = float(os.getenv("STATIC_MEMORY_IOU", "0.7"))
    # A remembered static box is forgotten after this long unseen.
    STATIC_MEMORY_TTL_SEC: float = float(os.getenv("STATIC_MEMORY_TTL_SEC", "1800"))
    # Remembered static figures are saved per camera (STORAGE_DIR/static_memory/
    # <camera_id>.json, boxes normalised 0..1) so a restart does not make the
    # poster a "person" again; expiry uses the wall-clock last_seen above.
    # Saved at most every STATIC_MEMORY_SAVE_SEC (an added or dropped figure:
    # on the next analysed frame).
    STATIC_MEMORY_PERSIST: bool = os.getenv("STATIC_MEMORY_PERSIST", "1").strip().lower() not in ("0", "false", "no", "off")
    STATIC_MEMORY_SAVE_SEC: float = float(os.getenv("STATIC_MEMORY_SAVE_SEC", "30"))
    # A new track that matches a remembered static figure (box IoU >=
    # STATIC_MEMORY_IOU and the same pose), or whose box lies at least
    # STATIC_PENDING_IGNORE_FRACTION inside an ignore area, is "pending static":
    # not counted, analysed or alerted on until it moves (then a person at
    # once) or STATIC_FIGURE_GRACE_SEC passes without motion (then static).
    STATIC_PENDING_IGNORE_FRACTION: float = float(os.getenv("STATIC_PENDING_IGNORE_FRACTION", "0.5"))
    # Box centre / height motion has a jitter floor like the joints: K x the
    # 75th percentile of its own frame-to-frame step (tracking_service), but
    # never above this many torso lengths, so a walk just before stopping
    # cannot raise it to where real sways are missed.
    STATIC_BOX_JITTER_MAX_FRAC: float = float(os.getenv("STATIC_BOX_JITTER_MAX_FRAC", "0.35"))
    # A static (or remembered) figure whose box still overlaps its anchor box
    # by IoU >= STATIC_OCCLUSION_IOU while its joints "move" is being occluded
    # by a passer-by: its stillness clock is kept, for at most
    # STATIC_OCCLUSION_MAX_SEC in a row (then the motion counts, so a real
    # person who stepped into the figure's place is a person again).
    STATIC_OCCLUSION_IOU: float = float(os.getenv("STATIC_OCCLUSION_IOU", "0.8"))
    STATIC_OCCLUSION_MAX_SEC: float = float(os.getenv("STATIC_OCCLUSION_MAX_SEC", "10"))
    # A detection gap longer than MOTION_MAX_OBS_GAP_SEC (10 s) restarts a
    # track's stillness clock, except on a remembered static box: there the
    # gap counts as still time up to this long.
    STATIC_MEMORY_MAX_GAP_SEC: float = float(os.getenv("STATIC_MEMORY_MAX_GAP_SEC", "60"))
    # AI_IGNORE areas drop a detection whose foot point is inside, or whose box
    # has at least this share of its area inside (per area: ignore_box_fraction).
    IGNORE_BOX_FRACTION: float = float(os.getenv("IGNORE_BOX_FRACTION", "0.6"))
    # A person must linger this long inside a zone before it counts as dwell
    # rather than a pass-through.
    ZONE_DWELL_MIN_SECONDS: float = float(os.getenv("ZONE_DWELL_MIN_SECONDS", "3.0"))

    # Site settings. These 15 values are only DEFAULTS: the dashboard
    # (Settings > Store / Evidence storage / Alerts and detection) saves the
    # store's own values in the database and services/site_settings.py writes
    # them onto this object at start-up and on every change, so they apply
    # without a restart. Read them as settings.X when needed, never copy one
    # into a module-level constant: STORE_NAME, SITE_TIMEZONE,
    # EVIDENCE_MAX_GB, STORAGE_RETENTION_DAYS, STORAGE_MAX_DISK_PERCENT,
    # NIGHT_WATCH_EVIDENCE_MAX_MB, NIGHT_WATCH_CLIP, THEFT_HIGH_VALUE_CATEGORIES,
    # THEFT_HIGH_VALUE_MIN_PRICE, TRIPWIRE_ALERT_COOLDOWN_SEC,
    # RESTRICTED_AREA_COOLDOWN_SEC, THEFT_INCIDENT_COOLDOWN_SEC,
    # THEFT_MIN_CONFIDENCE, THEFT_EXIT_RULE_ENABLED, QUEUE_CONGESTED_WAIT_SEC.
    #
    # Store identity & default premises extent (metres) for a fresh blueprint.
    STORE_ID: str = os.getenv("STORE_ID", "store_main")
    STORE_NAME: str = os.getenv("STORE_NAME", "Store")
    DEFAULT_STORE_WIDTH_M: float = float(os.getenv("DEFAULT_STORE_WIDTH_M", "50.0"))
    DEFAULT_STORE_HEIGHT_M: float = float(os.getenv("DEFAULT_STORE_HEIGHT_M", "30.0"))

    OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    SHM_DIR: Path = Path(os.getenv("SHM_DIR", "/dev/shm" if os.path.exists("/dev/shm") else "/tmp"))
    
    # Storage Retention & Purging Policies
    # This device keeps loss-prevention evidence only (stills, optional short
    # clips); it never records continuously (the store NAS does). Evidence is
    # aged out by services/evidence_storage.py, oldest first across all kinds:
    # older than STORAGE_RETENTION_DAYS (0 = no age limit), above
    # EVIDENCE_MAX_GB in total (0 = automatic: 10% of the disk, 1-50 GB), or
    # when the disk passes STORAGE_MAX_DISK_PERCENT. Defaults only: Settings >
    # Evidence storage overrides these (services/site_settings.py).
    STORAGE_RETENTION_DAYS: int = int(os.getenv("STORAGE_RETENTION_DAYS", "7"))
    STORAGE_MAX_DISK_PERCENT: float = float(os.getenv("STORAGE_MAX_DISK_PERCENT", "85.0"))
    EVIDENCE_MAX_GB: float = float(os.getenv("EVIDENCE_MAX_GB", "0"))
    # Seconds between background passes (each write also triggers one when over the limit).
    EVIDENCE_CLEANUP_INTERVAL_SEC: float = float(os.getenv("EVIDENCE_CLEANUP_INTERVAL_SEC", "900"))
    DVR_DEFAULT_RETENTION_DAYS: int = 7
    DVR_DEFAULT_QUOTA_GB: float = 100.0
    
    # go2rtc (live video gateway for direct peer-to-peer WebRTC; see
    # services/go2rtc_manager.py and docs/REMOTE_VIDEO_CONTRACT.md). It runs as
    # a child of this service, started on the first live-video session only.
    # Its API listens on 127.0.0.1:GO2RTC_API_PORT, never on another address.
    # The binary: GO2RTC_PATH, PATH, then <repo>/bin/go2rtc (bootstrap.py).
    # Its config is STORAGE_DIR/go2rtc/go2rtc.yaml (generated, no streams, no
    # credentials). Mode/port/STUN are Settings -> Online access.
    GO2RTC_API_PORT: int = int(os.getenv("GO2RTC_API_PORT", "1984"))
    GO2RTC_START_TIMEOUT_S: float = float(os.getenv("GO2RTC_START_TIMEOUT_S", "10"))
    # Live video sessions (the page heartbeats every WEBRTC_HEARTBEAT_S).
    WEBRTC_HEARTBEAT_S: int = int(os.getenv("WEBRTC_HEARTBEAT_S", "15"))
    WEBRTC_IDLE_TIMEOUT_S: int = int(os.getenv("WEBRTC_IDLE_TIMEOUT_S", "45"))
    WEBRTC_MAX_SESSION_S: int = int(os.getenv("WEBRTC_MAX_SESSION_S", "14400"))
    # How often the reaper checks go2rtc consumers, heartbeats and cameras
    # (runs only while a session exists).
    WEBRTC_REAP_INTERVAL_S: float = float(os.getenv("WEBRTC_REAP_INTERVAL_S", "3"))
    # H.264 encoder for a browser that cannot play the camera's codec:
    # auto (probe VA-API, NVENC, QSV, then libx264) or one of those names.
    WEBRTC_TRANSCODE_ENCODER: str = os.getenv("WEBRTC_TRANSCODE_ENCODER", "auto")
    
    # Hardware Devices
    VAAPI_DEVICE: str = os.getenv("VAAPI_DEVICE", "/dev/dri/renderD128")
    HAILO_DEVICE: str = os.getenv("HAILO_DEVICE", "/dev/hailo0")
    # A compiled HEF of the pose model for a Hailo NPU. None ships: there is no
    # RTMO HEF, and YOLO pose HEFs are AGPL-3.0 derivatives, which the product
    # must not contain. A fitted Hailo device is reported ("no compatible
    # model for this accelerator") and the next backend is used.
    HAILO_POSE_HEF_PATH: str = os.getenv("HAILO_POSE_HEF_PATH", "")
    
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

    # TURN is never used: remote live video is direct peer-to-peer WebRTC and
    # the ICE lists contain stun: URLs only (docs/REMOTE_VIDEO_CONTRACT.md).
    # TURN_ENABLED is ignored (a warning is logged); COTURN_SECRET stays a
    # managed secret only so existing secret stores keep their layout.
    def _derive_storage_paths(self) -> None:
        """STORAGE_DIR set to another place moves the database, backups and media with it.

        Before, only STORAGE_DIR moved: the database, backups, snapshots and
        clips stayed under the default root although the docs say STORAGE_DIR
        holds the evidence and the database. A path set on its own (e.g.
        DATABASE_PATH in .env) still wins.
        """
        explicit = set(self.model_fields_set)
        storage = Path(self.STORAGE_DIR)
        if "STORAGE_DIR" in explicit:
            try:
                moved = storage.resolve() != get_storage_root().resolve()
            except OSError:
                moved = True
            if moved:
                for key, rel in STORAGE_DERIVED_PATHS.items():
                    if key in explicit:
                        continue
                    path = storage / rel
                    try:
                        (path.parent if key in _STORAGE_DERIVED_FILES else path).mkdir(parents=True, exist_ok=True)
                    except OSError:
                        pass     # reported by preflight / the service that uses it
                    setattr(self, key, path)
        if "SQLITE_DB_PATH" not in explicit:
            # One database: the legacy name follows DATABASE_PATH.
            self.SQLITE_DB_PATH = Path(self.DATABASE_PATH)

    TURN_ENABLED: bool = False
    COTURN_SECRET: str = ""
    COTURN_REALM: str = os.getenv("COTURN_REALM", "cctv.local")
    COTURN_PUBLIC_IP: str = os.getenv("COTURN_PUBLIC_IP", "127.0.0.1")
    COTURN_PORT: int = int(os.getenv("COTURN_PORT", "3478"))

    def model_post_init(self, __context) -> None:
        """Paths under a moved STORAGE_DIR; every secret left unset (or a placeholder) from the store."""
        super().model_post_init(__context)
        self._derive_storage_paths()
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
    # Pre-event clip ring (services/clip_recorder.py): ~5 JPEG-encoded frames a
    # second per camera, so a clip can start PRE_EVENT_BUFFER_SECONDS before
    # the event. "auto": kept only for cameras that can raise a clip on their
    # own (night watch armed with NIGHT_WATCH_CLIP on, or the camera feature
    # theft_clip with theft detection on: a clip per theft incident); an operator's clip
    # export or an /events/trigger clip then records its post-roll only.
    # "on": every streaming camera (the old behaviour; the encoding was ~27 %
    # of the camera threads' CPU on the store box). "off": never, post-roll only.
    CLIP_PRE_EVENT_BUFFER: str = os.getenv("CLIP_PRE_EVENT_BUFFER", "auto")
    
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
    # Two tracks holding a reach in the same zone with contact points closer
    # than this fraction of the frame diagonal are one physical hand that the
    # pose model gave to two overlapping people: only the most plausible arm
    # keeps the reach. 0 disables the dedupe.
    INTERACTION_DEDUPE_DIST_FRAC: float = float(os.getenv("INTERACTION_DEDUPE_DIST_FRAC", "0.03"))
    # Plausible forearm / upper-arm length ratio in the image (foreshortening
    # included), and maximum arm length in torso lengths, for that decision.
    INTERACTION_DEDUPE_FOREARM_RATIO_MIN: float = float(os.getenv("INTERACTION_DEDUPE_FOREARM_RATIO_MIN", "0.45"))
    INTERACTION_DEDUPE_FOREARM_RATIO_MAX: float = float(os.getenv("INTERACTION_DEDUPE_FOREARM_RATIO_MAX", "1.8"))
    INTERACTION_DEDUPE_MAX_ARM_TORSOS: float = float(os.getenv("INTERACTION_DEDUPE_MAX_ARM_TORSOS", "2.2"))

    # Concealment: after a reach ends, the wrist must enter the lower-torso /
    # pocket region within THEFT_CONCEAL_WINDOW_SEC, stay
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
    # A straight arm hanging down with the wrist below the hip line is how a
    # basket (or a bag) is carried, not a hand in a pocket: such a wrist never
    # counts as in the pocket band. "Straight" = |shoulder->wrist| /
    # (|shoulder->elbow| + |elbow->wrist|) at least this (a hand pushed into a
    # front pocket bends the elbow outwards, typically 0.7-0.85).
    THEFT_HANGING_ARM_STRAIGHT_RATIO: float = float(os.getenv("THEFT_HANGING_ARM_STRAIGHT_RATIO", "0.9"))
    # Pose-only concealment targets beyond the pocket band. Both need a body
    # seen from the front: nose and an eye visible and the shoulder width at
    # least this fraction of the shoulder->hip length (profile ~0.1-0.3,
    # frontal ~0.6-0.8). In profile or from behind, left/right and
    # "behind the body" cannot be told apart from 2D keypoints.
    THEFT_FRONTAL_MIN_SHOULDER_TORSO: float = float(os.getenv("THEFT_FRONTAL_MIN_SHOULDER_TORSO", "0.45"))
    # Inside-jacket / chest: the wrist crosses the body midline to the
    # opposite side of the upper torso. Crossing is measured from the
    # shoulder midpoint (0) to the opposite shoulder (1); the wrist must be
    # at least THEFT_CHEST_MIN_CROSS across (a hand holding a product up to
    # read stays near the middle or on its own side) and at most
    # THEFT_CHEST_MAX_CROSS (beyond that it is reaching past the body).
    THEFT_CHEST_MIN_CROSS: float = float(os.getenv("THEFT_CHEST_MIN_CROSS", "0.3"))
    THEFT_CHEST_MAX_CROSS: float = float(os.getenv("THEFT_CHEST_MAX_CROSS", "1.3"))
    # Vertical extent of the chest region, as fractions of shoulder->hip
    # length below the shoulder line (the pocket band starts at
    # THEFT_CONCEAL_REGION_TOP_FRAC and wins where they overlap).
    THEFT_CHEST_TOP_FRAC: float = float(os.getenv("THEFT_CHEST_TOP_FRAC", "0.05"))
    THEFT_CHEST_BOTTOM_FRAC: float = float(os.getenv("THEFT_CHEST_BOTTOM_FRAC", "0.55"))
    # A hand that stays at the chest longer than this is examining a product
    # or using a phone, not slipping something inside a jacket (which takes
    # about a second): such a hold is never concealment.
    THEFT_CHEST_MAX_HOLD_SEC: float = float(os.getenv("THEFT_CHEST_MAX_HOLD_SEC", "3.0"))
    # Both wrists closer than this many shoulder widths at the chest = a
    # product handled with two hands (opening, reading), not concealment.
    THEFT_CHEST_TWO_HAND_DIST: float = float(os.getenv("THEFT_CHEST_TWO_HAND_DIST", "0.6"))
    # Behind the back / into a worn bag: after a shelf reach the wrist, seen
    # leaving the shelf at waist height at the side of the body, drops below
    # THEFT_OCCLUDED_WRIST_MAX_VIS (out of sight) within
    # THEFT_OCCLUDED_ONSET_SEC of last being seen, while both shoulders, both
    # hips, both knees (no trolley or display in front) and that arm's elbow
    # (at the flank, at waist height) stay visible and the face looks at the
    # camera. It must stay hidden THEFT_OCCLUDED_MIN_HOLD_SEC (an arm swing
    # while walking hides the wrist for ~0.3 s). Being an inference from
    # occlusion, its confidence is scaled by THEFT_OCCLUDED_CONFIDENCE_FACTOR.
    THEFT_OCCLUDED_WRIST_MAX_VIS: float = float(os.getenv("THEFT_OCCLUDED_WRIST_MAX_VIS", "0.2"))
    THEFT_OCCLUDED_ONSET_SEC: float = float(os.getenv("THEFT_OCCLUDED_ONSET_SEC", "1.0"))
    THEFT_OCCLUDED_MIN_HOLD_SEC: float = float(os.getenv("THEFT_OCCLUDED_MIN_HOLD_SEC", "0.8"))
    THEFT_OCCLUDED_CONFIDENCE_FACTOR: float = float(os.getenv("THEFT_OCCLUDED_CONFIDENCE_FACTOR", "0.8"))
    # Shelf sweeping: this many reaches into one zone inside the window.
    THEFT_SWEEP_WINDOW_SEC: float = float(os.getenv("THEFT_SWEEP_WINDOW_SEC", "10.0"))
    THEFT_SWEEP_MIN_REACHES: int = int(os.getenv("THEFT_SWEEP_MIN_REACHES", "4"))
    # Suspicious loitering near high-value product zones. Default only:
    # Settings > Alerts and detection overrides it (and the four THEFT_ values
    # below marked "dashboard").
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
    # Behaviour pattern (BEHAVIOUR_PATTERN): weak cues of one track that no
    # single rule fired on are fused into a score. Each cue event adds its
    # weight, decayed with THEFT_PATTERN_HALF_LIFE_SEC; each cue type's total
    # is capped, so many ordinary pickups or glances cannot add up alone. An
    # incident needs the score to reach THEFT_PATTERN_SCORE_THRESHOLD (divided
    # by the camera role's sensitivity) from at least THEFT_PATTERN_MIN_CUE_TYPES
    # distinct cue types, and no other rule fired on the track within
    # THEFT_INCIDENT_COOLDOWN_SEC. Defaults: hidden-hand holds and an exit
    # without checkout weigh most; a pickup that was not put back weighs
    # least (every purchase is one). A shopper who dwells at high-value
    # stock, looks around and picks items scores 1.0, below the threshold.
    THEFT_PATTERN_ENABLED: bool = os.getenv("THEFT_PATTERN_ENABLED", "true").lower() in ("1", "true", "yes")
    THEFT_PATTERN_SCORE_THRESHOLD: float = float(os.getenv("THEFT_PATTERN_SCORE_THRESHOLD", "1.2"))
    THEFT_PATTERN_MIN_CUE_TYPES: int = int(os.getenv("THEFT_PATTERN_MIN_CUE_TYPES", "2"))
    THEFT_PATTERN_HALF_LIFE_SEC: float = float(os.getenv("THEFT_PATTERN_HALF_LIFE_SEC", "60.0"))
    # cue:weight per event, and cue:cap on that cue type's decayed total.
    THEFT_PATTERN_WEIGHTS: str = os.getenv(
        "THEFT_PATTERN_WEIGHTS",
        "conceal_hold:0.5,reach_no_return:0.1,head_scan:0.35,high_value_dwell:0.35,"
        "sweep_partial:0.4,exit_no_checkout:0.5",
    )
    THEFT_PATTERN_CAPS: str = os.getenv(
        "THEFT_PATTERN_CAPS",
        "conceal_hold:0.8,reach_no_return:0.3,head_scan:0.35,high_value_dwell:0.35,"
        "sweep_partial:0.4,exit_no_checkout:0.5",
    )
    # head_scan cue: this many head turns within the window (12/min by default).
    THEFT_PATTERN_HEAD_WINDOW_SEC: float = float(os.getenv("THEFT_PATTERN_HEAD_WINDOW_SEC", "30.0"))
    THEFT_PATTERN_HEAD_TURNS: int = int(os.getenv("THEFT_PATTERN_HEAD_TURNS", "6"))
    # high_value_dwell cue: dwell by a high-value zone of this fraction of
    # THEFT_LOITER_MIN_DWELL_SEC (the loitering rule needs all of it).
    THEFT_PATTERN_DWELL_FRAC: float = float(os.getenv("THEFT_PATTERN_DWELL_FRAC", "0.5"))
    # Exit without checkout (needs a calibrated camera and ENTRANCE/EXIT + CHECKOUT zones). Dashboard.
    THEFT_EXIT_RULE_ENABLED: bool = os.getenv("THEFT_EXIT_RULE_ENABLED", "true").lower() in ("1", "true", "yes")
    # Sweethearting: seconds between a checkout hand pass and a POS scan to count as matched.
    THEFT_POS_MATCH_TOLERANCE_SEC: float = float(os.getenv("THEFT_POS_MATCH_TOLERANCE_SEC", "2.0"))
    # One incident per track and rule inside this window. Dashboard.
    THEFT_INCIDENT_COOLDOWN_SEC: float = float(os.getenv("THEFT_INCIDENT_COOLDOWN_SEC", "120.0"))
    # Incidents below this evidence-derived confidence are not raised. Dashboard.
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
    # Per (tripwire, track) cooldown between TRIPWIRE_ALERT notifications. Dashboard.
    TRIPWIRE_ALERT_COOLDOWN_SEC: float = float(os.getenv("TRIPWIRE_ALERT_COOLDOWN_SEC", "60"))
    # Per (area, track) cooldown between RESTRICTED_AREA alerts. Dashboard.
    RESTRICTED_AREA_COOLDOWN_SEC: float = float(os.getenv("RESTRICTED_AREA_COOLDOWN_SEC", "300"))
    # A foot point may leave an area this long (boundary jitter) without resetting its dwell.
    RESTRICTED_AREA_EXIT_GRACE_SEC: float = float(os.getenv("RESTRICTED_AREA_EXIT_GRACE_SEC", "1.5"))
    # The store's IANA time zone (e.g. Australia/Melbourne): "today", hourly
    # curves, restricted-area and night-watch schedules and alert times are
    # store-local. Empty = the host's own time zone. Default only: Settings >
    # Store > Time zone overrides it.
    SITE_TIMEZONE: str = os.getenv("SITE_TIMEZONE", "")
    # Where alert snapshots are written; empty means <STORAGE_DIR>/zone_alerts.
    ZONE_ALERT_EVIDENCE_DIR: str = os.getenv("ZONE_ALERT_EVIDENCE_DIR", "")

    # ---------------- Night watch (services/night_watch.py) ----------------
    # Per camera (Settings > camera > Night watch): while armed and nothing
    # moves, the pose model does not run for that camera; a cheap motion check
    # on a small grayscale copy runs instead, and motion triggers a short
    # person-detection burst to confirm.
    # Motion samples per second per armed camera.
    NIGHT_WATCH_MOTION_FPS: float = float(os.getenv("NIGHT_WATCH_MOTION_FPS", "4"))
    # Width of the grayscale copy the motion check works on (pixels).
    NIGHT_WATCH_MOTION_WIDTH: int = int(os.getenv("NIGHT_WATCH_MOTION_WIDTH", "320"))
    # A sample where more than this share of the picture changes at once is a
    # lighting event (IR switching, lights on/off, headlights), not motion.
    NIGHT_WATCH_LIGHTING_FRACTION: float = float(os.getenv("NIGHT_WATCH_LIGHTING_FRACTION", "0.45"))
    # Seconds of person detection after motion, and the detections needed to confirm.
    NIGHT_WATCH_CONFIRM_SEC: float = float(os.getenv("NIGHT_WATCH_CONFIRM_SEC", "4"))
    NIGHT_WATCH_CONFIRM_HITS: int = int(os.getenv("NIGHT_WATCH_CONFIRM_HITS", "2"))
    # After a burst found nobody, motion must continue this long before the next burst.
    NIGHT_WATCH_RECONFIRM_SEC: float = float(os.getenv("NIGHT_WATCH_RECONFIRM_SEC", "10"))
    # At most this many night-watch events (alerts + motion) per camera per hour.
    NIGHT_WATCH_MAX_EVENTS_PER_HOUR: int = int(os.getenv("NIGHT_WATCH_MAX_EVENTS_PER_HOUR", "12"))
    # Save a short clip (pre-event buffer + NIGHT_WATCH_CLIP_POST_SEC) with a person alert. Dashboard.
    NIGHT_WATCH_CLIP: bool = os.getenv("NIGHT_WATCH_CLIP", "false").lower() in ("1", "true", "yes", "on")
    NIGHT_WATCH_CLIP_POST_SEC: float = float(os.getenv("NIGHT_WATCH_CLIP_POST_SEC", "5"))
    # Evidence stills/clips; empty = <STORAGE_DIR>/night_watch. Oldest deleted first above the
    # cap, a sub-cap inside the total evidence limit (EVIDENCE_MAX_GB, services/evidence_storage.py).
    # NIGHT_WATCH_EVIDENCE_MAX_MB: dashboard (Settings > Evidence storage).
    NIGHT_WATCH_EVIDENCE_DIR: str = os.getenv("NIGHT_WATCH_EVIDENCE_DIR", "")
    NIGHT_WATCH_EVIDENCE_MAX_MB: float = float(os.getenv("NIGHT_WATCH_EVIDENCE_MAX_MB", "1024"))

    # Camera roles (services/camera_roles.py). Pipeline threads re-read the
    # cameras' roles at most this often.
    CAMERA_ROLE_CACHE_TTL_SEC: float = float(os.getenv("CAMERA_ROLE_CACHE_TTL_SEC", "5"))
    # Studio checkout / queue areas: a foot point may leave the area this long
    # (boundary jitter, brief occlusion) without ending the person's visit.
    QUEUE_AREA_EXIT_GRACE_SEC: float = float(os.getenv("QUEUE_AREA_EXIT_GRACE_SEC", "2.0"))
    # Average time at a lane above which it is reported CONGESTED. Dashboard.
    QUEUE_CONGESTED_WAIT_SEC: float = float(os.getenv("QUEUE_CONGESTED_WAIT_SEC", "270"))

    # Network camera open / read timeouts for the live workers (live_analytics_engine._open).
    CAMERA_OPEN_TIMEOUT_SEC: float = float(os.getenv("CAMERA_OPEN_TIMEOUT_SEC", "8"))
    CAMERA_READ_TIMEOUT_SEC: float = float(os.getenv("CAMERA_READ_TIMEOUT_SEC", "10"))
    # FFmpeg (CPU) decode threads per camera. 0 = by stream resolution: 1 up to
    # 1280x720, 2 up to 1920x1080, 4 above. OpenCV's default is one thread per
    # CPU (plus helpers, ~31 threads per stream on a 16-CPU box); a 352x288
    # sub-stream decodes at hundreds of fps on one.
    CAMERA_DECODE_THREADS: int = int(os.getenv("CAMERA_DECODE_THREADS", "0"))
    # Camera video decoding (services/capture_backends.py). "auto" probes at
    # start-up: NVIDIA NVDEC, then VA-API on each /dev/dri render node (AMD /
    # Intel), each checked by decoding a test clip through the real filter
    # chain; software (OpenCV on the CPU, as before) when none works or no
    # ffmpeg binary is installed. "cuda" / "vaapi" insist on one (and fall back
    # to software, logged as an error, if it fails); "software" never uses the
    # GPU. A camera whose stream the GPU cannot decode falls back on its own.
    # Only RTSP cameras are GPU-decoded. DECODE_DEVICE picks the render node
    # (/dev/dri/renderD129) or CUDA device index; empty = first that works.
    DECODE_BACKEND: str = os.getenv("DECODE_BACKEND", "auto")
    DECODE_DEVICE: str = os.getenv("DECODE_DEVICE", "")
    # RTSP cameras deliver at most this many frames a second (0 = all). On the
    # GPU the rest are dropped before they cost any CPU; in software they are
    # still decoded but not converted to BGR (grab without retrieve: 17-33 %
    # less CPU than reading every frame, measured 704x576..3072x2048). A
    # camera's detection ceiling stays native fps / ANALYTICS_DETECT_EVERY_N_FRAMES
    # (25 / 5 = 5 fps) as long as this is at least that; the enlarged live
    # view and clips show at most this rate.
    # Default 5: every consumer but the enlarged live view is satisfied by it
    # (analysis ~2-3 fps per camera and never capped by it, see
    # CameraWorker._clip_every; clip ring 5 fps; the dashboard's "Normal"
    # live view asks for 5 fps), and it halves the per-frame capture cost:
    # 8 test cameras 67 -> 58 % of a core (720p camera 8 -> 6 %, 2560x1440 at
    # native 15 -> 10 %). 10 gives Studio and the "High" live view smoother
    # motion for about 1 % of a core per camera.
    DECODE_MAX_FPS: float = float(os.getenv("DECODE_MAX_FPS", "5"))
    # GPU-decoded frames wider than this are scaled down on the GPU, before
    # they are downloaded, keeping the aspect ratio (0 = native size); a
    # camera's own ``decode_max_width`` setting overrides it. "auto" = 1920
    # while the pose model takes a wide input (a 544x960 export sees at most
    # 960 px anyway; same people found at 1920 as at native on 2560x1440 and
    # 3072x2048 test streams), and native size while a 640x640 model runs
    # (RTMO-s, the default): a 640x640 model lost 10-50 % of far people
    # (< 150 px tall) on any GPU-scaled stream in the same test. A running camera follows a model change within seconds.
    # At 1920 a 2560x1440 camera costs 24.5 -> 8 % of a core and a 3072x2048
    # one 39 -> 10 % (ffmpeg + reader, store box, 10 fps). Calibrations and
    # masks keep working (pixel geometry is scaled, services/frame_geometry.py).
    # Trade-off: evidence stills and clips of such a camera are 1920 wide.
    DECODE_MAX_WIDTH: str = os.getenv("DECODE_MAX_WIDTH", "auto")
    # With DECODE_BACKEND=auto, only streams of at least this many pixels
    # (width x height) are decoded on the GPU, smaller ones in software.
    # 0 = every RTSP camera on the GPU (the default). Measured on the store
    # box (i5-14400F, RX 9060 XT VA-API, 8 cameras per size, 25 fps H.264 and
    # H.265, % of one core per camera incl. ffmpeg): GPU path at 10 fps vs
    # software at 10 fps (grab-skip) -> 352x288 2.2 vs 2.2 (H.265 2.1 vs 2.9),
    # 704x576 3.9 vs 5.6, 1280x720 7.6 vs 12, 2560x1440 23-27 vs 37-40,
    # 3072x2048 33-37 vs 51-60. The GPU path was never dearer, so nothing
    # moves off it here; raise this on a box where small streams are cheaper
    # in software (e.g. a weak iGPU). A camera's size is remembered
    # (STORAGE_DIR/decode_stream_sizes.json); one of unknown size is opened
    # as a 352x288 stream would be and moves once, after its first frame.
    DECODE_GPU_MIN_PIXELS: int = int(os.getenv("DECODE_GPU_MIN_PIXELS", "0"))
    # OpenCV's own thread pool (resize, colour conversion, blur). Every camera
    # already has its own worker thread, so the pool only added spinning
    # threads: 8 test cameras cost 188 % of a core with OpenCV's default (one
    # thread per CPU) and 97 % with 1. 0 = OpenCV's default.
    OPENCV_THREADS: int = int(os.getenv("OPENCV_THREADS", "1"))

    # ---------------- Stream quality per camera (services/stream_selection.py) ----------------
    # A camera on "Stream quality: Auto" (the default) is analysed from the
    # smallest Dahua sub-stream (subtype 1 or 2) of at least D1 (704x576); if
    # none reaches D1 it keeps its current sub-stream, never the main stream.
    # The sub-streams' sizes are measured once per camera (one frame each,
    # read-only, one camera at a time), starting this many seconds after
    # start-up. STREAM_AUTO_SELECT=0 stops the automatic measuring (cameras
    # then stay on their configured stream unless re-measured from Settings).
    STREAM_AUTO_SELECT: bool = os.getenv("STREAM_AUTO_SELECT", "1").strip().lower() in ("1", "true", "yes", "on")
    STREAM_AUTO_SELECT_DELAY_S: float = float(os.getenv("STREAM_AUTO_SELECT_DELAY_S", "90"))

    # ---------------- Footfall track quality (services/retail_metrics_service.py) ----------------
    # Occlusion and detector flicker split one person into many ~1 s tracks.
    # Footfall, dwell and funnel figures count a track / zone visit only when it
    # lasted this long, and (tracks) was matched on at least this many frames.
    FOOTFALL_MIN_TRACK_SECONDS: float = float(os.getenv("FOOTFALL_MIN_TRACK_SECONDS", "3.0"))
    FOOTFALL_MIN_TRACK_HITS: int = int(os.getenv("FOOTFALL_MIN_TRACK_HITS", os.getenv("TRACK_MIN_HITS", "3")))

    # ---------------- Recorded heatmap history (services/heatmap_history.py) ----------------
    # Persisted track path: one sample (normalised image foot point, plus floor
    # metres when calibrated) at most every TRAJECTORY_SAMPLE_SEC, at most
    # TRAJECTORY_MAX_POINTS per track row (0.5 s x 3600 = 30 min).
    TRAJECTORY_SAMPLE_SEC: float = float(os.getenv("TRAJECTORY_SAMPLE_SEC", "0.5"))
    TRAJECTORY_MAX_POINTS: int = int(os.getenv("TRAJECTORY_MAX_POINTS", "3600"))
    HEATMAP_RECORDING_ENABLED: bool = os.getenv("HEATMAP_RECORDING_ENABLED", "true").lower() in ("1", "true", "yes")
    # Floor grid cell size (metres) and per-axis cap; image grid per camera.
    HEATMAP_FLOOR_CELL_M: float = float(os.getenv("HEATMAP_FLOOR_CELL_M", "0.5"))
    HEATMAP_FLOOR_MAX_CELLS: int = int(os.getenv("HEATMAP_FLOOR_MAX_CELLS", "120"))
    HEATMAP_IMAGE_GRID_W: int = int(os.getenv("HEATMAP_IMAGE_GRID_W", "64"))
    HEATMAP_IMAGE_GRID_H: int = int(os.getenv("HEATMAP_IMAGE_GRID_H", "36"))
    # An hour is recorded this long after it closes (finished tracks reach the
    # DB first); the two hours before it are re-recorded at the same time.
    HEATMAP_SETTLE_SEC: float = float(os.getenv("HEATMAP_SETTLE_SEC", "300"))
    HEATMAP_TICK_SEC: float = float(os.getenv("HEATMAP_TICK_SEC", "30"))
    # Startup backfill of missing past hours from existing rows (bounded).
    HEATMAP_BACKFILL_DAYS: int = int(os.getenv("HEATMAP_BACKFILL_DAYS", "14"))
    # Retention: hourly snapshots / daily roll-ups.
    HEATMAP_HOURLY_RETENTION_DAYS: int = int(os.getenv("HEATMAP_HOURLY_RETENTION_DAYS", "35"))
    HEATMAP_DAILY_RETENTION_DAYS: int = int(os.getenv("HEATMAP_DAILY_RETENTION_DAYS", "400"))
    # Heatmap-trend rules (business_analysis_service). Below the minimum
    # history a rule reports "not enough recorded history" instead of firing.
    HEATMAP_TREND_DAYS: int = int(os.getenv("HEATMAP_TREND_DAYS", "7"))
    HEATMAP_TREND_MIN_DAYS: int = int(os.getenv("HEATMAP_TREND_MIN_DAYS", "5"))
    # Dead space: zone visitor density below this share of the median covered
    # zone on at least HEATMAP_DEAD_SPACE_DAY_SHARE of the recorded days.
    HEATMAP_DEAD_SPACE_RATIO: float = float(os.getenv("HEATMAP_DEAD_SPACE_RATIO", "0.15"))
    HEATMAP_DEAD_SPACE_DAY_SHARE: float = float(os.getenv("HEATMAP_DEAD_SPACE_DAY_SHARE", "0.8"))
    HEATMAP_MIN_ZONE_COVERAGE: float = float(os.getenv("HEATMAP_MIN_ZONE_COVERAGE", "0.5"))
    # Hot-spot shift: centroid of the busiest cells moved at least this far
    # week over week, each week with HEATMAP_SHIFT_MIN_DAYS recorded days.
    HEATMAP_SHIFT_MIN_M: float = float(os.getenv("HEATMAP_SHIFT_MIN_M", "3.0"))
    HEATMAP_SHIFT_MIN_DAYS: int = int(os.getenv("HEATMAP_SHIFT_MIN_DAYS", "4"))
    # Minimum visitor-cell passes (sum of the presence grid) in each compared week.
    HEATMAP_SHIFT_MIN_PASSES: int = int(os.getenv("HEATMAP_SHIFT_MIN_PASSES", "200"))
    # Congestion: people standing in a CHECKOUT/ENTRANCE/EXIT zone at its
    # peak hour, above its quietest recorded hour (staff baseline).
    HEATMAP_CONGESTION_MIN_PEOPLE: float = float(os.getenv("HEATMAP_CONGESTION_MIN_PEOPLE", "3.0"))
    HEATMAP_CONGESTION_MIN_DAYS_PER_HOUR: int = int(os.getenv("HEATMAP_CONGESTION_MIN_DAYS_PER_HOUR", "3"))
    # Browsed, not touched: shopper-minutes in front of a product zone vs reaches.
    HEATMAP_BROWSE_MIN_SHOPPER_MIN: float = float(os.getenv("HEATMAP_BROWSE_MIN_SHOPPER_MIN", "30"))
    HEATMAP_BROWSE_MAX_REACHES_PER_MIN: float = float(os.getenv("HEATMAP_BROWSE_MAX_REACHES_PER_MIN", "0.05"))


settings = Settings()
