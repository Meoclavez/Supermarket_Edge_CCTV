"""SQLAlchemy database models for cameras, loss-prevention alerts, push device tokens, DVR segments, and archives."""

from datetime import datetime
from typing import Optional
from sqlalchemy import String, Float, Boolean, DateTime, Integer, JSON, ForeignKey, BigInteger, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class AdminUserModel(Base):
    __tablename__ = "admin_users"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class SystemSetupModel(Base):
    __tablename__ = "system_setup"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(String(4096), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CameraModel(Base):
    __tablename__ = "cameras"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    location: Mapped[str] = mapped_column(String(128), nullable=False)
    rtsp_url: Mapped[str] = mapped_column(String(512), nullable=False)
    webrtc_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="ONLINE")
    fps: Mapped[int] = mapped_column(Integer, default=30)
    resolution: Mapped[str] = mapped_column(String(32), default="1920x1080")
    is_ai_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    ai_models: Mapped[list] = mapped_column(JSON, default=list)

    # 24/7 DVR Configuration
    dvr_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    dvr_retention_days: Mapped[int] = mapped_column(Integer, default=7)
    dvr_quota_gb: Mapped[float] = mapped_column(Float, default=100.0)

    # State & Timestamps
    muted_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_seen: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Spatial, Floorplan & Channel Mapping
    channel_number: Mapped[int] = mapped_column(Integer, default=1)
    department: Mapped[str] = mapped_column(String(64), default="GENERAL")
    floor_x: Mapped[float] = mapped_column(Float, default=0.0)  # metres
    floor_y: Mapped[float] = mapped_column(Float, default=0.0)  # metres
    floor_z: Mapped[float] = mapped_column(Float, default=3.2)
    azimuth_deg: Mapped[float] = mapped_column(Float, default=0.0)
    fov_deg: Mapped[float] = mapped_column(Float, default=85.0)
    homography_matrix: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    # The operator's raw calibration input, kept so the UI can show and re-edit
    # the point pairs the homography was solved from:
    # {"image_points": [{x,y}], "floor_points": [{x,y}], "frame_width", "frame_height", "saved_at"}
    calibration_points: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    features: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)

    # Relationships
    events: Mapped[list["SecurityEventModel"]] = relationship(
        "SecurityEventModel", back_populates="camera", cascade="all, delete-orphan"
    )
    dvr_segments: Mapped[list["DVRSegmentModel"]] = relationship(
        "DVRSegmentModel", back_populates="camera", cascade="all, delete-orphan"
    )
    archives: Mapped[list["IncidentArchiveModel"]] = relationship(
        "IncidentArchiveModel", back_populates="camera", cascade="all, delete-orphan"
    )


class SecurityEventModel(Base):
    """Loss-prevention alert log (theft alerts, camera offline).

    Each row is one alert fanned out to staff phones and the dashboard
    websocket; ``acknowledged`` records that a staff member has seen it. The
    evidence for a theft alert lives in ``theft_incidents``; this table is the
    notification record. The table name is kept for existing databases.
    """
    __tablename__ = "security_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    camera_id: Mapped[str] = mapped_column(String(64), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False)
    camera_name: Mapped[str] = mapped_column(String(128), nullable=False)
    location: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    clip_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    snapshot_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    bounding_box: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    keypoints: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    # Older builds also had a ``kinematics`` column (fall-detection telemetry);
    # migration m0004 drops it. Schema changes: see app/migrations/__init__.py.
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    camera: Mapped["CameraModel"] = relationship("CameraModel", back_populates="events")


class DVRSegmentModel(Base):
    __tablename__ = "dvr_segments"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    camera_id: Mapped[str] = mapped_column(String(64), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False, index=True)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    duration_seconds: Mapped[float] = mapped_column(Float, default=60.0)
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    is_corrupted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    camera: Mapped["CameraModel"] = relationship("CameraModel", back_populates="dvr_segments")

    __table_args__ = (
        Index("ix_dvr_camera_time", "camera_id", "start_time", "end_time"),
    )


class IncidentArchiveModel(Base):
    __tablename__ = "incident_archives"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    camera_id: Mapped[str] = mapped_column(String(64), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False, index=True)
    camera_name: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(32), default="COMPLETED")
    download_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    is_protected: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    camera: Mapped["CameraModel"] = relationship("CameraModel", back_populates="archives")


class DeviceTokenModel(Base):
    __tablename__ = "device_tokens"

    device_token: Mapped[str] = mapped_column(String(256), primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    device_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    app_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    last_registered: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ---------------- Retail Analytics Database Models ----------------

class PlanogramItemModel(Base):
    __tablename__ = "planogram_items"

    sku_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    shelf_zone_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    facing_count: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class POSTransactionModel(Base):
    __tablename__ = "pos_transactions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    transaction_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    register_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sku_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ShelfInteractionModel(Base):
    __tablename__ = "shelf_interactions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    camera_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    shelf_zone_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    person_track_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(String(32), default="GRAB")  # REACH, GRAB, INSPECT, RETURN
    duration_sec: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Written by the live pose pipeline (app/services/pose_analytics.py).
    hand: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)            # "left" | "right"
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)       # mean wrist visibility
    zone_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    zone_space: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)    # "image" | "floor"
    image_x: Mapped[Optional[float]] = mapped_column(Float, nullable=True)          # wrist, 0..1 of frame width
    image_y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    floor_x: Mapped[Optional[float]] = mapped_column(Float, nullable=True)          # shopper position, metres
    floor_y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Product attribution copied at reach time (m0010); NULL on older rows.
    sku_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    product_category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    shelf_level: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)      # TOP | MIDDLE | BOTTOM
    value_tier: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)      # LOW | STANDARD | PREMIUM
    contact_point: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)   # hand_tip | wrist

    __table_args__ = (Index("ix_shelf_interactions_zone_ts", "shelf_zone_id", "timestamp"),)


class CustomerTrackModel(Base):
    __tablename__ = "customer_tracks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    track_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    camera_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    start_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    trajectory_points: Mapped[list] = mapped_column(JSON, default=list)
    # Detection frames the tracker matched (m0008); NULL on older rows.
    hits: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    age_group: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    gender: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    sentiment: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class RetailAnalyticsSummaryModel(Base):
    __tablename__ = "retail_analytics_summaries"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    date: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), default="store_main", nullable=False)
    total_footfall: Mapped[int] = mapped_column(Integer, default=0)
    avg_dwell_time: Mapped[float] = mapped_column(Float, default=0.0)
    zone_metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    lost_sales_alerts: Mapped[list] = mapped_column(JSON, default=list)
    recommendations: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AIDecisionRecommendationModel(Base):
    __tablename__ = "ai_decision_recommendations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    date: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), default="MEDIUM")
    zone: Mapped[str] = mapped_column(String(64), nullable=False)
    finding: Mapped[str] = mapped_column(String(512), nullable=False)
    root_cause: Mapped[str] = mapped_column(String(512), nullable=False)
    action_item: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="PENDING")  # PENDING, REVIEWED, APPLIED, DISMISSED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class TheftIncidentModel(Base):
    __tablename__ = "theft_incidents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    camera_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    camera_name: Mapped[str] = mapped_column(String(128), default="Camera")
    department: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    shelf_zone_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    theft_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)  # SHELF_SWEEPING, CONCEALMENT, SWEETHEARTING, PUSHOUT_EXIT_BYPASS, HIGH_RISK_CASING
    severity: Mapped[str] = mapped_column(String(32), default="HIGH", index=True)  # CRITICAL, HIGH, MEDIUM
    confidence: Mapped[float] = mapped_column(Float, default=0.85)
    person_track_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    evidence_summary: Mapped[str] = mapped_column(String(1024), default="")
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", index=True)  # ACTIVE, ACKNOWLEDGED, DISPATCHED, RESOLVED, FALSE_ALARM
    snapshot_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    clip_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    officer_notes: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    
    # Ancillary / Detailed telemetry fields
    zone_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    estimated_loss_value: Mapped[float] = mapped_column(Float, default=0.0)
    items_involved: Mapped[list] = mapped_column(JSON, default=list)
    evidence_snapshot_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    evidence_clip_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    bounding_box: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    wrist_trajectory: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    # Which loss-prevention rule raised this, and the evidence bullets behind it.
    rule: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    evidence: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    guard_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    dispatch_details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    resolution: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)




class StoreLayoutModel(Base):
    """Physical extent of the premises. The single source of truth for blueprint geometry.

    All blueprint coordinates in this system are real-world METRES with the origin
    at the top-left of the floor plan, x increasing right and y increasing down.
    Pixel coordinates exist only inside the renderer.
    """
    __tablename__ = "store_layouts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    store_id: Mapped[str] = mapped_column(String(64), index=True, default="store_main")
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    width_m: Mapped[float] = mapped_column(Float, default=50.0)
    height_m: Mapped[float] = mapped_column(Float, default=30.0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    zones: Mapped[list["StoreZoneModel"]] = relationship(
        "StoreZoneModel", back_populates="layout", cascade="all, delete-orphan"
    )


class StoreZoneModel(Base):
    """An operator-drawn region of the store (aisle, department, checkout, shelf bay).

    `polygon` is a JSON list of {"x": <metres>, "y": <metres>} vertices. Zone
    records hold identity and geometry only -- never metrics. Footfall, dwell and
    conversion for a zone are always derived from zone_visits at query time.
    """
    __tablename__ = "store_zones"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    layout_id: Mapped[str] = mapped_column(String(64), ForeignKey("store_layouts.id"), index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(64), default="AISLE", index=True)
    polygon: Mapped[list] = mapped_column(JSON, default=list)
    color: Mapped[str] = mapped_column(String(16), default="#00d4ff")
    # Zones of kind SHELF may carry a planogram SKU association.
    sku_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    layout: Mapped["StoreLayoutModel"] = relationship("StoreLayoutModel", back_populates="zones")


class StoreStructureModel(Base):
    """A purely geometric element of the blueprint: room, wall, shelf, counter, door, obstacle.

    Structures are what the operator draws so the plan looks like their store.
    They carry no analytics meaning -- zones remain the regions tracks are
    attributed to. ``polygon`` is a JSON list of {"x", "y"} vertices in metres;
    for WALL and DOOR it is a polyline (>=2 points) with ``thickness_m``, for
    every other kind a closed polygon (>=3 points).
    """
    __tablename__ = "store_structures"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    layout_id: Mapped[str] = mapped_column(String(64), ForeignKey("store_layouts.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32), default="ROOM", index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    polygon: Mapped[list] = mapped_column(JSON, default=list)
    thickness_m: Mapped[float] = mapped_column(Float, default=0.2)
    color: Mapped[str] = mapped_column(String(16), default="#8b93a7")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    properties: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ZoneVisitModel(Base):
    """One person's dwell inside one zone, produced by the live tracker.

    This is the atomic fact the entire retail funnel is computed from:
    pass-by, dwell, interaction and conversion all aggregate from these rows.
    """
    __tablename__ = "zone_visits"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    zone_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    track_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    camera_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    entered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    exited_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    dwell_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    # True once the tracker observed a reach-to-shelf inside this visit.
    interacted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (Index("ix_zone_visits_zone_entered", "zone_id", "entered_at"),)


class DiscoveredDeviceModel(Base):
    """A camera-capable device found by a network or USB scan.

    Persisted so the 'available cameras' picker survives a restart and so the
    operator can see what was seen last scan without re-running discovery.
    Being listed here does NOT make it an active camera -- the operator adopts
    a device explicitly, which creates the corresponding cameras row.
    """
    __tablename__ = "discovered_devices"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    transport: Mapped[str] = mapped_column(String(16), default="rtsp")  # rtsp | usb | mjpeg
    driver: Mapped[str] = mapped_column(String(32), default="generic")  # dahua | hikvision | onvif | v4l2
    host: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    port: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    device_path: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    model_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    manufacturer: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    stream_urls: Mapped[list] = mapped_column(JSON, default=list)
    channels: Mapped[int] = mapped_column(Integer, default=1)
    requires_credentials: Mapped[bool] = mapped_column(Boolean, default=False)
    reachable: Mapped[bool] = mapped_column(Boolean, default=True)
    adopted_camera_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# --- Pairing & push (migration m0007) ----------------------------------------


class PairedDeviceModel(Base):
    """A staff phone paired with this edge device (QR/code or password login).

    Tokens issued to the phone carry ``dev`` (this device's id) and ``pd``
    (this row's id). Revoking the row makes every token it holds unusable.
    Only a SHA-256 of the phone's current refresh token is stored.
    """
    __tablename__ = "paired_devices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)  # android | ios
    app_instance_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    user_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    paired_via: Mapped[str] = mapped_column(String(16), nullable=False)  # code | password
    push_provider: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # fcm | None
    push_token: Mapped[Optional[str]] = mapped_column(String(4096), nullable=True)
    push_token_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    refresh_token_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    alert_prefs: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_push_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    last_push_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_push_error: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)


class PairingSessionModel(Base):
    """A one-time pairing code shown on the dashboard (only its HMAC is stored)."""
    __tablename__ = "pairing_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    paired_device_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class RevokedTokenModel(Base):
    """A signed-out session token or login session (migration m0011).

    ``jti`` is the revocation key (``jti:<jti>``, ``h:<hash>`` or
    ``sid:<session>``); the row is kept until ``expires_at`` (unix seconds).
    Written and read by services/token_revocation.py.
    """
    __tablename__ = "revoked_tokens"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    token_type: Mapped[str] = mapped_column(String(16), nullable=False)
    expires_at: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    revoked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class TripwireEventModel(Base):
    """One person crossing a Studio tripwire (migration m0008).

    Written by services/tripwire_engine.py while the camera's
    ``people_counting`` flag is on. ``direction`` is "in" or "out" relative to
    the tripwire's configured in-side. ``tripwire_name`` and
    ``counts_footfall`` are copied at crossing time so history stays correct
    after the line is renamed, reconfigured or deleted.
    """
    __tablename__ = "tripwire_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tripwire_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tripwire_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    camera_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    track_id: Mapped[str] = mapped_column(String(64), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    counts_footfall: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (Index("ix_tripwire_events_tripwire_ts", "tripwire_id", "ts"),)
