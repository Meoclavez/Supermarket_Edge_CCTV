"""Application Configuration with safe fallback directory resolution."""

import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

def get_default_dir(name: str) -> Path:
    if os.path.exists("/app") and os.access("/app", os.W_OK):
        p = Path(f"/app/{name}")
    else:
        p = Path(__file__).resolve().parent.parent.parent / "storage" / name
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
    DEBUG: bool = False
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))
    EDGE_BASE_URL: str = os.getenv("EDGE_BASE_URL", "http://localhost:8000")
    ALLOWED_CORS_ORIGINS: list[str] = ["*"]
    
    # Persistent Storage Paths
    STORAGE_DIR: Path = get_default_dir("storage")
    SNAPSHOTS_DIR: Path = get_default_dir("snapshots")
    CLIPS_DIR: Path = get_default_dir("clips")
    DVR_DIR: Path = get_default_dir("dvr")
    ARCHIVES_DIR: Path = get_default_dir("archives")
    DATA_DIR: Path = get_default_dir("data")
    SQLITE_DB_PATH: Path = get_default_dir("storage") / "cctv_core.db"
    DATABASE_PATH: Path = get_default_dir("storage") / "cctv_core.db"
    BACKUPS_DIR: Path = get_default_dir("backups")
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
    
    # Security, JWT & Service Secrets
    JWT_SECRET: str = os.getenv("JWT_SECRET", "super_secret_edge_cctv_key_change_in_prod")
    JWT_ALGORITHM: str = "HS256"
    AUTH_SECRET_KEY: str = os.getenv("AUTH_SECRET_KEY", "super_secret_edge_cctv_key_change_in_prod")
    TOKEN_EXPIRY_SECONDS: int = 86400
    STREAM_TOKEN_EXPIRE_SECONDS: int = 86400
    STREAM_TOKEN_EXPIRY_SECONDS: int = 86400
    CLIP_TOKEN_EXPIRY_SECONDS: int = 86400
    INTERNAL_SERVICE_KEY: str = os.getenv("INTERNAL_SERVICE_KEY", "edge_ai_vision_internal_secret")
    
    # Coturn TURN/STUN Relay Credentials
    COTURN_SECRET: str = os.getenv("COTURN_SECRET", "cctv_turn_super_secret_dynamic_key_change_me_in_prod")
    COTURN_REALM: str = os.getenv("COTURN_REALM", "cctv.local")
    COTURN_PUBLIC_IP: str = os.getenv("COTURN_PUBLIC_IP", "127.0.0.1")
    COTURN_PORT: int = int(os.getenv("COTURN_PORT", "3478"))
    
    # Kinematics & AI Event Thresholds
    CAMERA_ALERT_COOLDOWN_SEC: float = 30.0
    FALL_TRANSITION_MAX_MS: float = 800.0
    FALL_TORSO_HORIZONTAL_ANGLE: float = 35.0
    FALL_ASPECT_RATIO_END: float = 0.8
    FALL_VELOCITY_THRESHOLD_Y: float = 1.8
    FALL_IMMOBILITY_SECONDS: float = 5.0
    FALL_VELOCITY_THRESHOLD: float = 1.8
    FALL_ASPECT_RATIO_THRESHOLD: float = 0.8
    FALL_IMMOBILITY_TIME_SEC: float = 5.0
    FALL_TORSO_ANGLE_THRESHOLD: float = 35.0
    DOOR_OPEN_ALERT_TIMEOUT_SEC: float = 300.0
    
    # Buffer & Clip Settings
    PRE_EVENT_BUFFER_SECONDS: int = 5
    POST_EVENT_RECORD_SECONDS: int = 10
    RECORDING_FPS: int = 25
    
    # Push Notification Credentials
    FCM_SERVER_KEY: str = os.getenv("FCM_SERVER_KEY", "")
    APNS_KEY_ID: str = os.getenv("APNS_KEY_ID", "")
    APNS_TEAM_ID: str = os.getenv("APNS_TEAM_ID", "")
    APNS_BUNDLE_ID: str = os.getenv("APNS_BUNDLE_ID", "com.cctv.edgeAiCctv")
    GOTIFY_URL: str = os.getenv("GOTIFY_URL", "http://127.0.0.1:8080")
    GOTIFY_APP_TOKEN: str = os.getenv("GOTIFY_APP_TOKEN", "")

settings = Settings()
