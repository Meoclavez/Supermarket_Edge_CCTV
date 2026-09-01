"""Pydantic schemas for Edge CCTV AI data validation, camera feeds, events, zones, DVR, and telemetry."""

from enum import Enum
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict


# ---------------- Enums ----------------

class CameraStatus(str, Enum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    DEGRADED = "DEGRADED"


class EventType(str, Enum):
    FALL_DETECTED = "FALL_DETECTED"
    INTRUSION_DETECTED = "INTRUSION_DETECTED"
    ZONE_INTRUSION = "ZONE_INTRUSION"
    TRIPWIRE_CROSSED = "TRIPWIRE_CROSSED"
    WEAPON_DETECTED = "WEAPON_DETECTED"
    PERIMETER_BREACH = "PERIMETER_BREACH"
    DOOR_LEFT_OPEN = "DOOR_LEFT_OPEN"
    PACKAGE_INTERACTION = "PACKAGE_INTERACTION"
    PACKAGE_THEFT = "PACKAGE_THEFT"
    INACTIVITY_ALARM = "INACTIVITY_ALARM"
    TAMPERING_DETECTED = "TAMPERING_DETECTED"
    HUMAN_DETECTED = "HUMAN_DETECTED"
    VEHICLE_DETECTED = "VEHICLE_DETECTED"
    MOTION = "MOTION"


class EventSeverity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    WARNING = "WARNING"
    INFO = "INFO"


class ZoneType(str, Enum):
    PRIVACY_MASK = "PRIVACY_MASK"
    EXCLUSION = "EXCLUSION"
    TRIPWIRE = "TRIPWIRE"
    INTRUSION = "INTRUSION"
    RESTRICTED_ZONE = "RESTRICTED_ZONE"
    DOOR = "DOOR"
    PACKAGE = "PACKAGE"
    DOOR_MONITOR = "DOOR_MONITOR"
    PACKAGE_ZONE = "PACKAGE_ZONE"


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
    camera_id: str = "cam_living_room"
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
    allowed_classes: List[str] = Field(default_factory=lambda: ["person", "vehicle"])
    in_count: int = 0
    out_count: int = 0


# ---------------- Vision & Kinematics ----------------

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


class KinematicTelemetry(BaseModel):
    hip_descent_velocity: float
    aspect_ratio_initial: float
    aspect_ratio_final: float
    transition_duration_ms: float
    immobility_duration_sec: float
    floor_proximity_score: float
    torso_angle_deg: Optional[float] = None


KinematicMetrics = KinematicTelemetry


# ---------------- Feature Toggles & Hardware Profile ----------------

class CameraFeatureConfig(BaseModel):
    motion_tracking: bool = True
    fall_detection: bool = True
    door_monitoring: bool = False
    package_theft_tracking: bool = False
    inactivity_alerts: bool = False
    tripwires_enabled: bool = True
    intrusion_zones_enabled: bool = True
    privacy_masks_enabled: bool = True
    dvr_recording_24_7: bool = True
    sub_stream_fps: int = 5
    main_stream_fps: int = 25


class HardwareProfile(BaseModel):
    decoder_type: str
    inference_backend: str
    device_name: str
    total_ram_gb: float
    available_ram_gb: float
    ring_buffer_seconds: int
    cpu_cores: int
    max_recommended_cameras: int


class SystemStats(BaseModel):
    cpu_usage_percent: float
    gpu_usage_percent: Optional[float] = None
    ram_used_gb: float
    ram_total_gb: float
    active_cameras: int
    active_features_count: int
    decoder: str
    inference_engine: str
    shm_buffer_used_mb: float
    uptime_seconds: float


# ---------------- Security Events ----------------

class SecurityEventBase(BaseModel):
    camera_id: str
    event_type: EventType
    severity: EventSeverity
    confidence: float
    description: Optional[str] = None
    bounding_box: Optional[BoundingBox] = None
    keypoints: Optional[List[Keypoint]] = None
    kinematics: Optional[KinematicTelemetry] = None
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
    rtsp_url: str = ""
    webrtc_url: str = ""
    status: Any = "ONLINE"
    fps: int = 25
    resolution: str = "1920x1080"
    is_ai_enabled: bool = True
    ai_models: List[str] = Field(default_factory=lambda: ["yolov5n", "kinematic_pose"])
    features: CameraFeatureConfig = Field(default_factory=CameraFeatureConfig)
    dvr_enabled: bool = True
    dvr_retention_days: int = 7
    dvr_quota_gb: float = 100.0
    last_seen: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


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
    duration_minutes: int = 5


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
    avg_wait_time_sec: float = 0.0
    service_rate_per_min: float = 1.2
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

