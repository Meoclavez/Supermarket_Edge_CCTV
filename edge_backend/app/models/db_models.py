"""SQLAlchemy database models for cameras, security events, push device tokens, DVR segments, and archives."""

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
    kinematics: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
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


class CustomerTrackModel(Base):
    __tablename__ = "customer_tracks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    track_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    camera_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    start_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    trajectory_points: Mapped[list] = mapped_column(JSON, default=list)
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
