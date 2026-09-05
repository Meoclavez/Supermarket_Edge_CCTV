"""Camera management, 28-channel supermarket matrix, dynamic scanner, snapshots, and feature toggles."""

import cv2
import numpy as np
import time
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, HTTPException, Response, Body, Depends
from pydantic import BaseModel
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..models.schemas import CameraFeed, CameraListResponse, CameraStatus, CameraFeatureConfig, MuteCameraRequest
from ..models.db_models import CameraModel
from ..database import get_db
from ..services.camera_network_manager import camera_network_manager
from ..services.feature_manager import feature_manager

router = APIRouter(prefix="/api/v1/cameras", tags=["Cameras"])

DEFAULT_CAMERAS = [
    # --- Entrance & Foyer (4) ---
    CameraFeed(
        id="cam_entrance_main",
        name="CAM-01: Main Entrance & Turnstiles",
        location="Foyer / Entrance",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=1&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_entrance_main",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_footfall", "bytetrack"],
        features=CameraFeatureConfig(fall_detection=True, tripwires_enabled=True, door_monitoring=True)
    ),
    CameraFeed(
        id="cam_entrance_exit",
        name="CAM-02: South Exit & Security Gates",
        location="Foyer / Exit",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=2&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_entrance_exit",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_footfall", "bytetrack"],
        features=CameraFeatureConfig(fall_detection=True, tripwires_enabled=True)
    ),
    CameraFeed(
        id="cam_cart_bay",
        name="CAM-03: Trolley & Cart Bay",
        location="Foyer / Entrance",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=3&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_cart_bay",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_cart_counter"],
        features=CameraFeatureConfig(intrusion_zones_enabled=True)
    ),
    CameraFeed(
        id="cam_foyer_promo",
        name="CAM-04: Foyer Promo Showcase",
        location="Foyer / Promotional",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=4&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_foyer_promo",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell"],
        features=CameraFeatureConfig(tripwires_enabled=True, intrusion_zones_enabled=True)
    ),

    # --- Grocery Aisles 1 to 12 (12) ---
    CameraFeed(
        id="cam_aisle_01",
        name="CAM-05: Aisle 1 - Cereals & Spreads",
        location="Aisle 1",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=5&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_aisle_01",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell"],
        features=CameraFeatureConfig(fall_detection=True, intrusion_zones_enabled=True)
    ),
    CameraFeed(
        id="cam_aisle_02",
        name="CAM-06: Aisle 2 - Coffee, Tea & Drinks",
        location="Aisle 2",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=6&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_aisle_02",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell", "spill_detector"],
        features=CameraFeatureConfig(fall_detection=True, intrusion_zones_enabled=True)
    ),
    CameraFeed(
        id="cam_aisle_03",
        name="CAM-07: Aisle 3 - Snacks & Confectionery",
        location="Aisle 3",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=7&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_aisle_03",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell", "endcap_analyzer"],
        features=CameraFeatureConfig(fall_detection=True, intrusion_zones_enabled=True)
    ),
    CameraFeed(
        id="cam_aisle_04",
        name="CAM-08: Aisle 4 - Canned Foods & Pasta",
        location="Aisle 4",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=8&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_aisle_04",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell"],
        features=CameraFeatureConfig(fall_detection=True)
    ),
    CameraFeed(
        id="cam_aisle_05",
        name="CAM-09: Aisle 5 - International & Asian",
        location="Aisle 5",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=9&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_aisle_05",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell"],
        features=CameraFeatureConfig(fall_detection=True)
    ),
    CameraFeed(
        id="cam_aisle_06",
        name="CAM-10: Aisle 6 - Oils, Sauces & Spices",
        location="Aisle 6",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=10&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_aisle_06",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell"],
        features=CameraFeatureConfig(fall_detection=True)
    ),
    CameraFeed(
        id="cam_aisle_07",
        name="CAM-11: Aisle 7 - Laundry & Detergents",
        location="Aisle 7",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=11&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_aisle_07",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell"],
        features=CameraFeatureConfig(fall_detection=True)
    ),
    CameraFeed(
        id="cam_aisle_08",
        name="CAM-12: Aisle 8 - Paper & Pet Food",
        location="Aisle 8",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=12&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_aisle_08",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell"],
        features=CameraFeatureConfig(fall_detection=True)
    ),
    CameraFeed(
        id="cam_aisle_09",
        name="CAM-13: Aisle 9 - Health & Pharmacy",
        location="Aisle 9",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=13&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_aisle_09",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell", "theft_prevention"],
        features=CameraFeatureConfig(fall_detection=True, intrusion_zones_enabled=True)
    ),
    CameraFeed(
        id="cam_aisle_10",
        name="CAM-14: Aisle 10 - Baby Care & Diapers",
        location="Aisle 10",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=14&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_aisle_10",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell"],
        features=CameraFeatureConfig(fall_detection=True)
    ),
    CameraFeed(
        id="cam_aisle_11",
        name="CAM-15: Aisle 11 - Frozen Foods",
        location="Aisle 11",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=15&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_aisle_11",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell"],
        features=CameraFeatureConfig(fall_detection=True)
    ),
    CameraFeed(
        id="cam_aisle_12",
        name="CAM-16: Aisle 12 - Chilled Dairy & Eggs",
        location="Aisle 12",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=16&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_aisle_12",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell"],
        features=CameraFeatureConfig(fall_detection=True)
    ),

    # --- Fresh & Bakery (4) ---
    CameraFeed(
        id="cam_produce_front",
        name="CAM-17: Fresh Produce - Fruits",
        location="Produce Department",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=17&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_produce_front",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell", "produce_density"],
        features=CameraFeatureConfig(fall_detection=True, intrusion_zones_enabled=True)
    ),
    CameraFeed(
        id="cam_produce_veg",
        name="CAM-18: Fresh Produce - Vegetables",
        location="Produce Department",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=18&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_produce_veg",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell"],
        features=CameraFeatureConfig(fall_detection=True)
    ),
    CameraFeed(
        id="cam_bakery_artisan",
        name="CAM-19: Artisan Bakery & Pastries",
        location="Bakery Department",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=19&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_bakery_artisan",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell", "bottleneck_flow"],
        features=CameraFeatureConfig(fall_detection=True, intrusion_zones_enabled=True)
    ),
    CameraFeed(
        id="cam_deli_meat",
        name="CAM-20: Deli Counter & Butcher",
        location="Deli & Meat",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=20&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_deli_meat",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell", "queue_wait"],
        features=CameraFeatureConfig(fall_detection=True, intrusion_zones_enabled=True)
    ),

    # --- Checkouts & POS (6) ---
    CameraFeed(
        id="cam_pos_01",
        name="CAM-21: POS Register 1 (Manned)",
        location="Checkouts",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=21&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_pos_01",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_queue", "pos_overlay"],
        features=CameraFeatureConfig(tripwires_enabled=True, intrusion_zones_enabled=True)
    ),
    CameraFeed(
        id="cam_pos_02",
        name="CAM-22: POS Register 2 (Manned)",
        location="Checkouts",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=22&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_pos_02",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_queue", "pos_overlay"],
        features=CameraFeatureConfig(tripwires_enabled=True, intrusion_zones_enabled=True)
    ),
    CameraFeed(
        id="cam_pos_03",
        name="CAM-23: POS Register 3 (Self-Checkout)",
        location="Checkouts",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=23&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_pos_03",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_queue", "scan_avoidance"],
        features=CameraFeatureConfig(tripwires_enabled=True, intrusion_zones_enabled=True)
    ),
    CameraFeed(
        id="cam_pos_04",
        name="CAM-24: POS Register 4 (Self-Checkout)",
        location="Checkouts",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=24&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_pos_04",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_queue", "scan_avoidance"],
        features=CameraFeatureConfig(tripwires_enabled=True, intrusion_zones_enabled=True)
    ),
    CameraFeed(
        id="cam_pos_05",
        name="CAM-25: POS Register 5 (Express Lane)",
        location="Checkouts",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=25&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_pos_05",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_queue"],
        features=CameraFeatureConfig(tripwires_enabled=True, intrusion_zones_enabled=True)
    ),
    CameraFeed(
        id="cam_cust_service",
        name="CAM-26: Customer Service & Tobacco",
        location="Checkouts",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=26&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_cust_service",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell", "face_anti_spoof"],
        features=CameraFeatureConfig(tripwires_enabled=True, privacy_masks_enabled=True)
    ),

    # --- High-Value & Backroom (2) ---
    CameraFeed(
        id="cam_liquor_zone",
        name="CAM-27: Liquor & Premium Spirits",
        location="Liquor Section",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=27&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_liquor_zone",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell", "loitering_alert"],
        features=CameraFeatureConfig(fall_detection=True, intrusion_zones_enabled=True, tripwires_enabled=True)
    ),
    CameraFeed(
        id="cam_dock_stock",
        name="CAM-28: Stockroom & Loading Dock",
        location="Backroom / Dock",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=28&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_dock_stock",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_forklift", "perimeter_intrusion"],
        features=CameraFeatureConfig(intrusion_zones_enabled=True, privacy_masks_enabled=True)
    ),
    CameraFeed(
        id="cam_aisle_13",
        name="CAM-29: Aisle 13 - International Foods & Spices",
        location="Aisles",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=29&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_aisle_13",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell"],
        features=CameraFeatureConfig(tripwires_enabled=True, intrusion_zones_enabled=True)
    ),
    CameraFeed(
        id="cam_aisle_14",
        name="CAM-30: Aisle 14 - Pet Care & Bulk Household",
        location="Aisles",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=30&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_aisle_14",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell"],
        features=CameraFeatureConfig(tripwires_enabled=True, intrusion_zones_enabled=True)
    ),
    CameraFeed(
        id="cam_cold_room",
        name="CAM-31: Walk-in Cold Room & Dairy Staging",
        location="Backroom / Cold Chain",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=31&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_cold_room",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["yolov5n_cold_storage"],
        features=CameraFeatureConfig(fall_detection=True, intrusion_zones_enabled=True)
    ),
    CameraFeed(
        id="cam_dock_receiving",
        name="CAM-32: Rear Receiving Dock & Compactor",
        location="Backroom / Dock",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=32&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_dock_receiving",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        is_ai_enabled=True,
        ai_models=["perimeter_intrusion", "yolov5n_forklift"],
        features=CameraFeatureConfig(intrusion_zones_enabled=True, privacy_masks_enabled=True)
    ),
]

CURRENT_ACTIVE_SOURCE = {"url": "synthetic", "name": "CAM-01: Main Entrance (Live)"}

@router.get("", response_model=CameraListResponse)
def list_cameras():
    for cam in DEFAULT_CAMERAS:
        cam.features = feature_manager.get_camera_features(cam.id)
    return CameraListResponse(cameras=DEFAULT_CAMERAS, total=len(DEFAULT_CAMERAS))

@router.get("/{camera_id}", response_model=CameraFeed)
def get_camera(camera_id: str):
    for cam in DEFAULT_CAMERAS:
        if cam.id == camera_id:
            cam.features = feature_manager.get_camera_features(cam.id)
            return cam
    raise HTTPException(status_code=404, detail="Camera not found")

@router.put("/{camera_id}/features", response_model=CameraFeatureConfig)
def update_camera_features(camera_id: str, config: CameraFeatureConfig):
    feature_manager.set_camera_features(camera_id, config)
    return config

@router.get("/{camera_id}/snapshot")
def get_camera_snapshot(camera_id: str):
    # Lookup camera name & channel
    cam_name = camera_id.replace("cam_", "").replace("_", " ").upper()
    location = "Zone Monitored"
    for c in DEFAULT_CAMERAS:
        if c.id == camera_id:
            cam_name = c.name
            location = c.location
            break

    frame = np.zeros((480, 854, 3), dtype=np.uint8)
    
    # Background subtle grid pattern
    for y in range(0, 480, 40):
        cv2.line(frame, (0, y), (854, y), (18, 24, 36), 1)
    for x in range(0, 854, 40):
        cv2.line(frame, (x, 0), (x, 480), (18, 24, 36), 1)

    # Simulated AI bounding boxes tailored to zone type
    t = time.time()
    seed = hash(camera_id) % 100
    cx = int(400 + 150 * np.sin(t * 0.8 + seed))
    cy = int(240 + 70 * np.cos(t * 0.6 + seed))

    if "entrance" in camera_id or "cart" in camera_id:
        cv2.rectangle(frame, (cx - 35, cy - 80), (cx + 35, cy + 80), (0, 255, 157), 2)
        cv2.putText(frame, "PERSON 0.96 [IN: 142]", (cx - 35, cy - 90), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 157), 1)
        cv2.line(frame, (100, 380), (754, 380), (0, 240, 255), 2)
        cv2.putText(frame, "TRIPWIRE A<->B", (110, 370), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 240, 255), 1)
    elif "pos" in camera_id:
        cv2.rectangle(frame, (200, 180), (280, 380), (0, 255, 157), 2)
        cv2.putText(frame, "CASHIER 0.98", (200, 170), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 157), 1)
        cv2.rectangle(frame, (cx - 30, cy - 60), (cx + 30, cy + 60), (255, 170, 0), 2)
        cv2.putText(frame, "SHOPPER 0.92 [WAIT: 1m20s]", (cx - 30, cy - 70), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 170, 0), 1)
    elif "liquor" in camera_id:
        cv2.rectangle(frame, (cx - 40, cy - 85), (cx + 40, cy + 85), (255, 0, 85), 2)
        cv2.putText(frame, "HIGH DWELL ALERT 0.94 [3m10s]", (cx - 40, cy - 95), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 0, 85), 1)
    else:
        cv2.rectangle(frame, (cx - 30, cy - 70), (cx + 30, cy + 70), (0, 255, 157), 2)
        cv2.putText(frame, "PERSON 0.94", (cx - 30, cy - 80), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 157), 1)

    # Top HUD Bar
    cv2.putText(frame, f"FEED: {cam_name.upper()}", (25, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 240, 255), 2)
    cv2.putText(frame, f"LOC: {location} | 1080p @ 25 FPS | H.265 VA-API", (25, 70), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (139, 148, 158), 1)
    
    # Bottom HUD Status
    cv2.putText(frame, "ZERO-CLOUD EDGE AI INFERENCE ACTIVE (100% LOCAL)", (25, 455), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 157), 1)
    cv2.putText(frame, f"UTC: {time.strftime('%Y-%m-%d %H:%M:%S')}", (620, 455), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 240, 255), 1)

    ret, jpeg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    return Response(content=jpeg.tobytes(), media_type="image/jpeg")

@router.post("/scan")
def scan_network_and_devices():
    sources = camera_network_manager.discover_all()
    return {"status": "success", "count": len(sources), "sources": sources}

@router.post("/api/rescan")
def studio_rescan():
    sources = camera_network_manager.discover_all()
    return {"status": "success", "sources": sources}

@router.post("/{camera_id}/mute")
async def mute_camera(
    camera_id: str,
    req: MuteCameraRequest = Body(default_factory=MuteCameraRequest),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(CameraModel).where(CameraModel.id == camera_id)
    res = await db.execute(stmt)
    cam = res.scalar_one_or_none()
    if cam:
        cam.muted_until = datetime.utcnow() + timedelta(minutes=req.duration_minutes)
        await db.commit()
    return {"status": "success", "camera_id": camera_id, "muted_minutes": req.duration_minutes}

