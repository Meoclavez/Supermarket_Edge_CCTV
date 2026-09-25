"""Pydantic schemas for the supermarket edge system: camera feeds, loss-prevention alerts, zones, DVR and telemetry."""

from enum import Enum
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict, AliasChoices, field_validator, model_validator


# ---------------- Enums ----------------

class CameraStatus(str, Enum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    DEGRADED = "DEGRADED"


class EventType(str, Enum):
    """Loss-prevention alert types a store operator is notified about.

    Behavioural types describe *suspicious behaviour for staff review*, never
    a finding of guilt. ``CAMERA_OFFLINE`` is operational.
    """
    THEFT_SUSPECTED = "THEFT_SUSPECTED"
    CONCEALMENT = "CONCEALMENT"
    SHELF_SWEEP = "SHELF_SWEEP"
    EXIT_WITHOUT_CHECKOUT = "EXIT_WITHOUT_CHECKOUT"
    LOITERING = "LOITERING"
    QUEUE_ALERT = "QUEUE_ALERT"
    CAMERA_OFFLINE = "CAMERA_OFFLINE"
    # Retail zone rules (services/tripwire_engine.py): a person inside a
    # restricted area (stockroom, cash office, after-hours floor) while its
    # schedule is active, and an alerting tripwire crossed in its alert direction.
    RESTRICTED_AREA = "RESTRICTED_AREA"
    TRIPWIRE_ALERT = "TRIPWIRE_ALERT"
    # Night watch (services/night_watch.py): a person confirmed on a camera
    # while its night watch is armed (HIGH, pushed), and motion that no
    # person detection confirmed (INFO, dashboard only).
    NIGHT_INTRUSION = "NIGHT_INTRUSION"
    NIGHT_MOTION = "NIGHT_MOTION"


class EventSeverity(str, Enum):
    HIGH = "HIGH"
    WARNING = "WARNING"
    INFO = "INFO"


class ZoneType(str, Enum):
    PRIVACY_MASK = "PRIVACY_MASK"
    EXCLUSION = "EXCLUSION"
    TRIPWIRE = "TRIPWIRE"
    INTRUSION = "INTRUSION"
    RESTRICTED_ZONE = "RESTRICTED_ZONE"


class MaskMode(str, Enum):
    BLACKOUT = "BLACKOUT"
    BLUR = "BLUR"
    MOSAIC = "MOSAIC"
    COLOR = "COLOR"
    AI_IGNORE = "AI_IGNORE"


class TripwireDirection(str, Enum):
    A_TO_B = "A_TO_B"
    B_TO_A = "B_TO_A"
    BIDIRECTIONAL = "BIDIRECTIONAL"


# ---------------- Geometry & Zones ----------------

class Point2D(BaseModel):
    x: float = Field(..., ge=0.0, le=1.0, description="Normalized X coordinate (0.0 to 1.0)")
    y: float = Field(..., ge=0.0, le=1.0, description="Normalized Y coordinate (0.0 to 1.0)")


class ZoneConfig(BaseModel):
    id: str
    camera_id: str = ""
    name: str
    zone_type: ZoneType = ZoneType.TRIPWIRE
    enabled: bool = True
    is_active: bool = True
    points: List[Point2D] = Field(default_factory=list)
    polygon_points: Optional[List[Point2D]] = Field(default_factory=list)
    line_start: Optional[Point2D] = None
    line_end: Optional[Point2D] = None
    direction: Optional[TripwireDirection] = TripwireDirection.BIDIRECTIONAL
    mask_mode: Optional[MaskMode] = MaskMode.BLUR
    mask_color_bgr: Optional[Tuple[int, int, int]] = (0, 0, 0)
    blur_kernel_size: int = 51
    mosaic_scale: int = 16
    dwell_time_seconds: float = 0.5
    allowed_classes: List[str] = Field(default_factory=lambda: ["person"])
    in_count: int = 0
    out_count: int = 0


# ---------------- Vision ----------------

class BoundingBox(BaseModel):
    x_min: float
    y_min: float
    x_max: float
    y_max: float
    confidence: float
    label: str = "person"
    class_name: str = "person"


class Keypoint(BaseModel):
    id: int
    name: str
    x: float
    y: float
    confidence: float = 1.0


# ---------------- Feature Toggles & Hardware Profile ----------------

NIGHT_WATCH_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class NightWatchConfig(BaseModel):
    """One camera's night watch (services/night_watch.py). Off unless enabled.

    ``start`` / ``end`` are store-local wall-clock times (SITE_TIMEZONE); an
    ``end`` at or before ``start`` runs past midnight into the next day, and
    ``days`` are the days a window *starts* on. ``when_dark`` also arms the
    camera whenever its measured lighting is low_light or ir.
    """
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    start: str = Field("22:00", description="Store-local start, HH:MM")
    end: str = Field("06:00", description="Store-local end, HH:MM; at or before start = next day")
    days: List[str] = Field(default_factory=lambda: list(NIGHT_WATCH_DAYS),
                            description="Days a window starts on (mon..sun)")
    when_dark: bool = Field(False, description="Also armed while the camera is in IR / low light")
    sensitivity: str = Field("medium", description="low | medium | high")
    cooldown_sec: int = Field(120, ge=10, le=3600, description="Per-camera quiet time after an event")

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        try:
            hh, mm = str(v).strip().split(":")[:2]
            h, m = int(hh), int(mm)
        except (ValueError, AttributeError):
            raise ValueError("time must be HH:MM") from None
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError("time must be HH:MM between 00:00 and 23:59")
        return f"{h:02d}:{m:02d}"

    @field_validator("days", mode="before")
    @classmethod
    def _days(cls, v: Any) -> List[str]:
        out: List[str] = []
        for d in v or []:
            if isinstance(d, bool):
                raise ValueError(f"invalid day {d!r}")
            if isinstance(d, int):
                if not 0 <= d <= 6:
                    raise ValueError(f"invalid day {d!r} (0 = Monday .. 6 = Sunday)")
                key = NIGHT_WATCH_DAYS[d]
            else:
                key = str(d).strip().lower()[:3]
                if key not in NIGHT_WATCH_DAYS:
                    raise ValueError(f"invalid day {d!r}")
            if key not in out:
                out.append(key)
        return sorted(out, key=NIGHT_WATCH_DAYS.index)

    @field_validator("sensitivity")
    @classmethod
    def _sensitivity(cls, v: str) -> str:
        v = str(v).strip().lower()
        if v not in ("low", "medium", "high"):
            raise ValueError("sensitivity must be low, medium or high")
        return v


class CameraFeatureConfig(BaseModel):
    """Per-camera analytics switches.

    Only flags that a pipeline stage checks belong here (see
    ``feature_manager.is_enabled``). Stored camera rows written by older
    builds may still carry keys such as ``fall_detection`` or
    ``door_monitoring``; ``extra="ignore"`` drops them instead of failing.
    """
    model_config = ConfigDict(extra="ignore")

    people_counting: bool = Field(True, description="Zone visits, dwell, queues and footfall from tracked people")
    shelf_interaction: bool = Field(True, description="Hand-to-shelf interaction events from pose keypoints")
    theft_detection: bool = Field(True, description="Suspicious-behaviour incidents for staff review")
    # Detector tuning for this camera. None = the global PERSON_MAX_FRAME_FRACTION.
    person_max_frame_fraction: Optional[float] = Field(
        None, ge=0.05, le=1.0,
        description=("Largest person box accepted, as a fraction of the frame area. Raise it for a "
                     "close-mounted camera (e.g. facing a shelf ~2 m away); empty = server default"),
    )
    # GPU downscale limit for this camera's frames; None = DECODE_MAX_WIDTH (auto).
    decode_max_width: Optional[int] = Field(
        None, ge=0, le=8192,
        description=("Widest frame this camera is analysed and shown at, scaled down on the GPU "
                     "(0 = the stream's native size, e.g. for full-resolution evidence stills); "
                     "empty = server default (DECODE_MAX_WIDTH: 1920 with a wide-input pose model, else native). "
                     "A running camera reopens its stream within seconds of a change"),
    )
    # Night watch schedule and options; None = never configured (off).
    night_watch: Optional[NightWatchConfig] = None


class HardwareProfile(BaseModel):
    """Probed capabilities plus the inference backend the detector actually runs.

    ``decoder_type`` is kept for compatibility and always equals
    ``decoder_capability``: it says what decode hardware exists, not what is
    in use. ``inference_backend`` / ``inference_provider`` come from
    ``person_detector.status()`` and never from the presence of a GPU.
    """
    decoder_type: str
    decoder_capability: str
    # What camera capture really decodes with, and why (see hardware_detector):
    # cuda | vaapi | cpu | mixed | none (no camera streaming).
    decoder_in_use: str = "none"
    decoder_note: Optional[str] = None
    # Streaming cameras per decoder, e.g. {"vaapi": 31, "software": 2}.
    decoder_cameras: dict[str, int] = Field(default_factory=dict)
    # The start-up GPU decoder test (capture_backends.DecodeProbe); None until it ran.
    decoder_probe: Optional[dict] = None
    inference_backend: str
    inference_provider: str
    inference_available: bool
    inference_device: Optional[str] = None
    inference_error: Optional[str] = None
    # The ONNX Runtime execution provider the session actually landed on
    # (e.g. CUDAExecutionProvider) and the model file it runs.
    inference_execution_provider: Optional[str] = None
    inference_model: Optional[str] = None
    # AMD MIGraphX: "compiling" while the models are compiled for the GPU in
    # the background (inference_provider then says what runs meanwhile),
    # "done", "failed", or "idle" when no GPU compile applies.
    inference_gpu_compile: Optional[str] = None
    device_name: str
    total_ram_gb: Optional[float] = None
    available_ram_gb: Optional[float] = None
    ring_buffer_seconds: int
    cpu_cores: Optional[int] = None
    max_recommended_cameras: int


class SystemStats(BaseModel):
    """Every figure is measured at request time or null. No constants."""
    cpu_usage_percent: Optional[float] = None
    gpu_usage_percent: Optional[float] = None
    ram_used_gb: Optional[float] = None
    ram_total_gb: Optional[float] = None
    active_cameras: int
    cameras_total: int = 0
    active_features_count: int
    decoder: str
    inference_engine: str
    inference_provider: Optional[str] = None
    shm_buffer_used_mb: Optional[float] = None
    uptime_seconds: float
    # Evidence stills/clips on this device and their limit (services/evidence_storage.py).
    evidence_storage: Optional[Dict[str, Any]] = None


# ---------------- Loss-prevention alerts ----------------

class SecurityEventBase(BaseModel):
    camera_id: str
    event_type: EventType
    severity: EventSeverity = EventSeverity.HIGH
    # Only a measured model confidence; null when the producer had none.
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    description: Optional[str] = None
    bounding_box: Optional[BoundingBox] = None
    keypoints: Optional[List[Keypoint]] = None
    metadata_json: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None


class SecurityEventCreate(SecurityEventBase):
    pass


class SecurityEvent(SecurityEventBase):
    id: str
    camera_name: str = "Camera Feed"
    location: str = "Location"
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    clip_url: Optional[str] = None
    snapshot_url: Optional[str] = None
    acknowledged: bool = False
    acknowledged_at: Optional[datetime] = None
    # Set when the evidence storage limit deleted this alert's still/clip.
    evidence_expired_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class EventListResponse(BaseModel):
    events: List[SecurityEvent]
    total: int


SecurityEventListResponse = EventListResponse


# ---------------- Cameras & WebRTC ----------------

class CameraFeed(BaseModel):
    id: str
    name: str
    location: str
    channel_number: int = 1
    department: str = "GENERAL"
    rtsp_url: str = ""
    webrtc_url: str = ""
    status: Any = "ONLINE"
    fps: int = 25
    resolution: str = "1920x1080"
    is_ai_enabled: bool = True
    ai_models: List[str] = Field(default_factory=list)
    features: Any = Field(default_factory=CameraFeatureConfig)
    dvr_enabled: bool = False  # legacy: this device never records continuously
    dvr_retention_days: int = 7
    dvr_quota_gb: float = 100.0
    # Blueprint placement. All three are REAL-WORLD METRES (origin top-left,
    # x right, y down), never pixels. Left as None when the camera has not
    # been placed; the API then keeps the stored value instead of inventing one.
    floor_x: Optional[float] = Field(None, description="Camera position on the floor plan, metres from the left edge")
    floor_y: Optional[float] = Field(None, description="Camera position on the floor plan, metres from the top edge")
    floor_z: Optional[float] = Field(None, description="Mounting height in metres")
    height_z: Optional[float] = Field(None, description="Deprecated alias of floor_z (metres)")
    azimuth_deg: Optional[float] = Field(None, description="Viewing direction, degrees clockwise from +x")
    fov_deg: Optional[float] = Field(None, description="Horizontal field of view in degrees")
    homography_matrix: Optional[List[Any]] = None
    last_seen: Optional[datetime] = None
    # What the camera is for (services/camera_roles.py ROLE_PRESETS); None = no role.
    role: Optional[str] = Field(None, description="Camera role id, e.g. entrance, checkout, aisle")
    # Checkout-lane cameras: the POS register_id this lane rings sales on.
    pos_register_id: Optional[str] = Field(None, max_length=64)

    model_config = ConfigDict(from_attributes=True)


class CameraCreate(BaseModel):
    id: str
    name: str
    location: str
    rtsp_url: str
    webrtc_url: Optional[str] = ""
    status: str = "ONLINE"
    fps: int = 25
    resolution: str = "1920x1080"
    is_ai_enabled: bool = True
    ai_models: List[str] = Field(default_factory=lambda: ["yolov5n"])
    channel_number: int = 1
    department: str = "GENERAL"
    # Metres on the blueprint; None = not placed yet.
    floor_x: Optional[float] = Field(None, description="metres from the left edge of the floor plan")
    floor_y: Optional[float] = Field(None, description="metres from the top edge of the floor plan")
    floor_z: Optional[float] = Field(None, description="mounting height, metres")
    azimuth_deg: Optional[float] = None
    fov_deg: Optional[float] = None
    homography_matrix: Optional[List[Any]] = None
    features: Optional[Dict[str, Any]] = None
    dvr_enabled: bool = False  # legacy: this device never records continuously
    dvr_retention_days: int = 7
    dvr_quota_gb: float = 100.0


class CameraUpdate(BaseModel):
    name: Optional[str] = None
    location: Optional[str] = None
    rtsp_url: Optional[str] = None
    webrtc_url: Optional[str] = None
    status: Optional[str] = None
    fps: Optional[int] = None
    resolution: Optional[str] = None
    is_ai_enabled: Optional[bool] = None
    ai_models: Optional[List[str]] = None
    channel_number: Optional[int] = None
    department: Optional[str] = None
    floor_x: Optional[float] = None
    floor_y: Optional[float] = None
    floor_z: Optional[float] = None
    azimuth_deg: Optional[float] = None
    fov_deg: Optional[float] = None
    homography_matrix: Optional[List[Any]] = None
    features: Optional[Dict[str, Any]] = None
    dvr_enabled: Optional[bool] = None
    dvr_retention_days: Optional[int] = None
    dvr_quota_gb: Optional[float] = None


class CameraPositionUpdate(BaseModel):
    """Camera placement on the blueprint. All lengths are metres, never pixels."""
    floor_x: float = Field(..., description="metres from the left edge of the floor plan")
    floor_y: float = Field(..., description="metres from the top edge of the floor plan")
    floor_z: Optional[float] = Field(None, description="mounting height in metres; omit to keep the stored value")
    azimuth_deg: float = Field(..., description="viewing direction, degrees clockwise from +x")
    fov_deg: float = Field(..., description="horizontal field of view in degrees")


class CameraListResponse(BaseModel):
    cameras: List[CameraFeed]
    total: Optional[int] = None


class WebRtcOffer(BaseModel):
    camera_id: str
    sdp: str
    type: str = "offer"


class WebRtcAnswer(BaseModel):
    camera_id: str
    sdp: str
    type: str = "answer"


# ---------------- 24-Hour Timeline & DVR ----------------

class TimelineSegment(BaseModel):
    id: str
    camera_id: str
    start_time: datetime
    end_time: datetime
    duration_seconds: float
    file_size_bytes: int
    stream_url: str


class TimelineGap(BaseModel):
    start_time: datetime
    end_time: datetime
    duration_seconds: float
    reason: str = "OFFLINE_OR_STREAM_DROP"


class TimelineEventMarker(BaseModel):
    id: str
    event_type: str
    severity: str
    confidence: float
    timestamp: datetime
    snapshot_url: Optional[str] = None
    clip_url: Optional[str] = None
    bounding_box: Optional[Dict[str, Any]] = None


class CameraTimelineResponse(BaseModel):
    camera_id: str
    camera_name: str
    date: str
    total_recorded_seconds: float
    total_segments: int
    hls_master_url: str
    segments: List[TimelineSegment]
    events: List[TimelineEventMarker]
    gaps: List[TimelineGap]


# ---------------- Custom Incident Export & Archives ----------------

class DVRExportRequest(BaseModel):
    start_time: datetime = Field(..., description="ISO 8601 start timestamp")
    end_time: datetime = Field(..., description="ISO 8601 end timestamp")
    title: str = Field(..., min_length=1, max_length=256, description="Title for the archived incident")
    description: Optional[str] = Field(None, max_length=512)


class IncidentArchiveResponse(BaseModel):
    id: str
    camera_id: str
    camera_name: str
    title: str
    description: Optional[str] = None
    start_time: datetime
    end_time: datetime
    duration_seconds: float
    file_size_bytes: int
    status: str
    download_url: Optional[str] = None
    created_at: datetime


class IncidentArchiveListResponse(BaseModel):
    archives: List[IncidentArchiveResponse]
    total: int


# ---------------- Storage Health & Devices ----------------

class DiskSMARTInfo(BaseModel):
    device: str
    model: str
    serial_number: Optional[str] = None
    temperature_celsius: Optional[int] = None
    health_status: str = "PASSED"
    reallocated_sectors: Optional[int] = 0
    wear_level_percent: Optional[int] = None
    power_on_hours: Optional[int] = None
    is_ssd: bool = True


class CameraStorageQuota(BaseModel):
    camera_id: str
    camera_name: str
    used_bytes: int
    used_gb: float
    quota_gb: float
    segment_count: int
    oldest_segment: Optional[datetime] = None
    newest_segment: Optional[datetime] = None


class StorageHealthResponse(BaseModel):
    storage_root: str
    is_external_mount: bool
    total_gb: float
    used_gb: float
    free_gb: float
    used_percent: float
    smart_status: List[DiskSMARTInfo]
    camera_quotas: List[CameraStorageQuota]
    archives_used_gb: float
    # Evidence stills/clips and their limit (services/evidence_storage.py).
    evidence: Optional[Dict[str, Any]] = None


# ---------------- Device & Mute Schemas ----------------

class DeviceRegistration(BaseModel):
    device_token: str = ""
    token: str = ""
    platform: str = "android"
    device_name: Optional[str] = None
    app_version: Optional[str] = None
    user_id: Optional[str] = "admin"


DeviceTokenRegistration = DeviceRegistration


class MuteCameraRequest(BaseModel):
    """Mute loss-prevention pushes for one camera; 0 unmutes."""
    duration_minutes: int = Field(5, ge=0, le=24 * 60)


# ---------------- Retail Analytics Schemas ----------------

class ShelfActionType(str, Enum):
    REACH = "REACH"
    GRAB = "GRAB"
    INSPECT = "INSPECT"
    RETURN = "RETURN"


class DecisionSeverity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class DecisionStatus(str, Enum):
    PENDING = "PENDING"
    REVIEWED = "REVIEWED"
    APPLIED = "APPLIED"
    DISMISSED = "DISMISSED"


class PlanogramItem(BaseModel):
    sku_id: str
    name: str
    category: str
    shelf_zone_id: str
    price: float = 0.0
    facing_count: int = 1

    model_config = ConfigDict(from_attributes=True)


class PlanogramItemListResponse(BaseModel):
    items: List[PlanogramItem]
    total: int


class POSTransaction(BaseModel):
    transaction_id: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    register_id: str
    sku_id: str
    quantity: int = 1
    amount: float = 0.0

    model_config = ConfigDict(from_attributes=True)


class POSIngestRequest(BaseModel):
    transactions: List[POSTransaction] = Field(default_factory=list)


class ShelfInteraction(BaseModel):
    id: Optional[str] = None
    camera_id: str
    shelf_zone_id: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    person_track_id: str
    action_type: str = "GRAB"  # REACH, GRAB, INSPECT, RETURN
    duration_sec: float = 0.0

    model_config = ConfigDict(from_attributes=True)


class CustomerTrack(BaseModel):
    track_id: str
    camera_id: str
    start_time: datetime = Field(default_factory=datetime.utcnow)
    end_time: Optional[datetime] = None
    trajectory_points: List[Dict[str, Any]] = Field(default_factory=list)
    age_group: Optional[str] = None
    gender: Optional[str] = None
    sentiment: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class RetailAnalyticsSummary(BaseModel):
    id: Optional[str] = None
    date: str
    store_id: str = "store_main"
    total_footfall: int = 0
    avg_dwell_time: float = 0.0
    zone_metrics: Dict[str, Any] = Field(default_factory=dict)
    lost_sales_alerts: List[Dict[str, Any]] = Field(default_factory=list)
    recommendations: List[Dict[str, Any]] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class AIDecisionRecommendation(BaseModel):
    id: str
    date: str
    category: str
    severity: str = "MEDIUM"
    zone: str
    finding: str
    root_cause: str
    action_item: str
    status: str = "PENDING"

    model_config = ConfigDict(from_attributes=True)


class DecisionActionRequest(BaseModel):
    status: str = "APPLIED"  # REVIEWED, APPLIED, DISMISSED
    notes: Optional[str] = None


class TelemetrySyncRequest(BaseModel):
    store_id: str = "store_main"
    cloud_endpoint: Optional[str] = None
    include_raw_tracks: bool = False
    date: Optional[str] = None


class QueueMetric(BaseModel):
    register_id: str
    status: str = "OPEN"  # OPEN, BUSY, CLOSED
    current_queue_count: int = 0
    avg_wait_time_sec: Optional[float] = None
    service_rate_per_min: Optional[float] = None
    bottleneck_alert: bool = False


class FunnelMetric(BaseModel):
    category: str
    shelf_zone_id: str
    impressions: int = 0
    engagements: int = 0
    interactions: int = 0
    purchases: int = 0
    conversion_rate: float = 0.0
    lost_sales_estimated: float = 0.0
    abandonment_rate: float = 0.0


class StoreOverviewResponse(BaseModel):
    store_id: str = "store_main"
    total_footfall: int = 0
    avg_dwell_time_minutes: float = 0.0
    conversion_rate: float = 0.0
    active_shoppers: int = 0
    daily_revenue: float = 0.0
    queue_stats: Dict[str, Any] = Field(default_factory=dict)
    hot_zones: List[Dict[str, Any]] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class FloorplanZone(BaseModel):
    zone_id: str
    name: str
    zone_type: str
    category: Optional[str] = None
    polygon: List[Dict[str, float]] = Field(default_factory=list)


class FloorplanCamera(BaseModel):
    camera_id: str
    name: str
    position_2d: Dict[str, float]
    fov_polygon: List[Dict[str, float]] = Field(default_factory=list)
    homography_matrix: Optional[List[List[float]]] = None


class FloorplanResponse(BaseModel):
    store_id: str = "store_main"
    dimensions: Dict[str, Any] = Field(default_factory=lambda: {"width": 1000, "height": 800, "scale": "1px = 0.05m"})
    cameras: List[FloorplanCamera] = Field(default_factory=list)
    zones: List[FloorplanZone] = Field(default_factory=list)
    categories: Dict[str, str] = Field(default_factory=dict)


class HeatmapsResponse(BaseModel):
    grid_width: int = 50
    grid_height: int = 50
    density_matrix: List[List[float]] = Field(default_factory=list)
    trajectory_flows: List[Dict[str, Any]] = Field(default_factory=list)
    peak_hours: Dict[str, int] = Field(default_factory=dict)


class FunnelResponse(BaseModel):
    funnels: List[FunnelMetric] = Field(default_factory=list)
    overall_conversion_rate: float = 0.0
    total_lost_sales_estimated: float = 0.0


class QueueTelemetryResponse(BaseModel):
    registers: List[QueueMetric] = Field(default_factory=list)
    store_avg_wait_sec: float = 0.0
    max_wait_sec: float = 0.0
    recommended_open_registers: int = 2


class DecisionsResponse(BaseModel):
    decisions: List[AIDecisionRecommendation] = Field(default_factory=list)
    total: int = 0


# ---------------- System Backup & Recovery ----------------

class BackupItem(BaseModel):
    filename: str
    filepath: Optional[str] = None
    size_bytes: int = 0
    size_mb: float = 0.0
    timestamp: str
    created_at: Optional[str] = None
    tag: str = "auto"
    model_config = ConfigDict(from_attributes=True)


class BackupListResponse(BaseModel):
    backups: List[BackupItem] = Field(default_factory=list)
    total: int = 0


class BackupCreateRequest(BaseModel):
    tag: str = "manual"


class BackupCreateResponse(BaseModel):
    status: str
    filename: str
    filepath: Optional[str] = None
    size_bytes: int = 0
    size_mb: float = 0.0
    timestamp: str
    tag: str


class RestoreResponse(BaseModel):
    status: str
    message: str
    filename: str


# ---------------- Theft & Loss Prevention ----------------

class TheftType(str, Enum):
    SHELF_SWEEPING = "SHELF_SWEEPING"
    CONCEALMENT = "CONCEALMENT"
    SWEETHEARTING = "SWEETHEARTING"
    PUSHOUT_EXIT_BYPASS = "PUSHOUT_EXIT_BYPASS"


class TheftIncidentStatus(str, Enum):
    ACTIVE = "ACTIVE"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    DISPATCHED = "DISPATCHED"
    RESOLVED = "RESOLVED"
    FALSE_ALARM = "FALSE_ALARM"


class TheftIncidentBase(BaseModel):
    theft_type: str
    severity: str = "HIGH"
    status: str = "ACTIVE"
    department: str = "General"
    camera_id: str
    camera_name: str = "Camera"
    shelf_zone_id: Optional[str] = None
    confidence: float = 0.85
    estimated_loss_value: float = 0.0
    items_involved: List[Dict[str, Any]] = Field(default_factory=list)
    evidence_snapshot_url: Optional[str] = None
    evidence_clip_url: Optional[str] = None
    snapshot_path: Optional[str] = None
    clip_path: Optional[str] = None
    # Set when the evidence storage limit deleted the evidence file.
    evidence_expired_at: Optional[datetime] = None
    evidence_summary: str = ""
    officer_notes: Optional[str] = None
    bounding_box: Optional[Dict[str, Any]] = None
    wrist_trajectory: Optional[List[Dict[str, Any]]] = None
    notes: Optional[str] = None
    zone_id: Optional[str] = None


class TheftIncidentCreate(TheftIncidentBase):
    pass


class TheftIncident(TheftIncidentBase):
    id: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    person_track_id: Optional[str] = None
    guard_id: Optional[str] = None
    dispatch_details: Optional[Dict[str, Any]] = None
    resolution: Optional[str] = None
    resolved_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: Optional[datetime] = Field(default_factory=datetime.utcnow)

    model_config = ConfigDict(from_attributes=True)


class TheftIncidentListResponse(BaseModel):
    status: str = "success"
    total: int
    incidents: List[TheftIncident]


class TheftActionRequest(BaseModel):
    status: str  # ACTIVE, ACKNOWLEDGED, DISPATCHED, RESOLVED, FALSE_ALARM
    officer_notes: Optional[str] = None


class TheftStatsResponse(BaseModel):
    total_active: int = 0
    by_type: Dict[str, int] = Field(default_factory=dict)
    by_department: Dict[str, int] = Field(default_factory=dict)
    by_severity: Dict[str, int] = Field(default_factory=dict)
    high_risk_zones: List[str] = Field(default_factory=list)


class TheftOutcome(str, Enum):
    """What the reviewer found. Required to close an incident; never defaulted."""

    RECOVERED_GOODS = "RECOVERED_GOODS"            # theft confirmed, goods recovered
    THEFT_NOT_RECOVERED = "THEFT_NOT_RECOVERED"    # theft confirmed, goods not recovered
    CUSTOMER_PAID = "CUSTOMER_PAID"                # approached, customer paid for the items
    POLICE_REPORTED = "POLICE_REPORTED"            # reported to police
    SUSPECT_FLED = "SUSPECT_FLED"                  # person left before staff reached them
    NO_ACTION = "NO_ACTION"                        # customer was fine, nothing to do
    FALSE_ALARM = "FALSE_ALARM"                    # the rule fired on normal behaviour


# Older clients (mobile app) send these names.
THEFT_OUTCOME_ALIASES = {"POLICE_DISPATCHED": "POLICE_REPORTED"}

THEFT_OUTCOME_LABELS = {
    "RECOVERED_GOODS": "Theft confirmed, goods recovered",
    "THEFT_NOT_RECOVERED": "Theft confirmed, goods not recovered",
    "CUSTOMER_PAID": "Customer paid",
    "POLICE_REPORTED": "Reported to police",
    "SUSPECT_FLED": "Person left before staff arrived",
    "NO_ACTION": "Customer was fine, no action",
    "FALSE_ALARM": "False alarm",
}

# Outcomes where the flagged behaviour was real and staff acted on it: their
# item value counts as "value actioned". FALSE_ALARM and NO_ACTION never do.
THEFT_ACTIONED_OUTCOMES = ("RECOVERED_GOODS", "THEFT_NOT_RECOVERED", "CUSTOMER_PAID",
                           "POLICE_REPORTED", "SUSPECT_FLED")
# Outcomes where a recovered value makes sense.
THEFT_RECOVERY_OUTCOMES = ("RECOVERED_GOODS", "CUSTOMER_PAID")


class TheftRuleOutcomeStats(BaseModel):
    rule: str
    label: Optional[str] = None
    reviewed: int                       # incidents with a recorded outcome
    false_alarms: int
    false_alarm_rate: Optional[float]   # 0..1, null when nothing was reviewed


class TheftStatisticsResponse(BaseModel):
    active_incidents_count: int
    today_incidents_count: int
    # Kept for older clients: equals value_actioned (false alarms excluded).
    prevented_loss_estimate: float
    by_department: Dict[str, int]
    by_theft_type: Dict[str, int]
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    # Estimated item value of incidents whose recorded outcome is in
    # THEFT_ACTIONED_OUTCOMES. Excludes false alarms, "no action", incidents
    # still open (value_pending_outcome) and unverified legacy resolutions.
    value_actioned: float = 0.0
    value_pending_outcome: float = 0.0
    # Sum of reviewer-entered recovered values; null when none was entered.
    value_recovered: Optional[float] = None
    outcomes: Dict[str, int] = Field(default_factory=dict)
    reviewed_count: int = 0
    false_alarm_count: int = 0
    false_alarm_rate: Optional[float] = None
    false_alarm_rate_by_rule: List[TheftRuleOutcomeStats] = Field(default_factory=list)
    # Resolved before outcomes were required (may carry the old default).
    unverified_legacy_resolutions: int = 0


class TheftAcknowledgeRequest(BaseModel):
    # Who acknowledged. When omitted the signed-in operator is recorded.
    guard_id: Optional[str] = Field(None, max_length=64)


class TheftDispatchRequest(BaseModel):
    """Mark that staff were sent. Nothing is invented: no default unit, no deterrent."""

    model_config = ConfigDict(populate_by_name=True)

    # Who was sent, as typed by the operator (optional). ``guard_unit`` is the old name.
    staff_name: Optional[str] = Field(None, max_length=128,
                                      validation_alias=AliasChoices("staff_name", "guard_unit"))
    note: Optional[str] = Field(None, max_length=500)
    notify_phones: bool = True


class TheftResolveRequest(BaseModel):
    """Close an incident with what the reviewer found. ``outcome`` is required."""

    model_config = ConfigDict(populate_by_name=True)

    # ``resolution`` is the old name (mobile app).
    outcome: TheftOutcome = Field(validation_alias=AliasChoices("outcome", "resolution"))
    notes: Optional[str] = Field(None, max_length=1000)
    recovered_value: Optional[float] = Field(None, ge=0, le=1_000_000)

    @field_validator("outcome", mode="before")
    @classmethod
    def _normalise_outcome(cls, v):
        if isinstance(v, str):
            v = v.strip().upper()
            v = THEFT_OUTCOME_ALIASES.get(v, v)
        return v

    @model_validator(mode="after")
    def _recovered_value_needs_recovery(self):
        if self.recovered_value is not None and self.outcome.value not in THEFT_RECOVERY_OUTCOMES:
            raise ValueError("recovered_value is only accepted with RECOVERED_GOODS or CUSTOMER_PAID")
        return self


