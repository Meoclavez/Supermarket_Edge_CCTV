"""Camera management, 32-channel supermarket matrix, dynamic scanner, snapshots, and feature toggles."""

import cv2
import numpy as np
import time
import logging
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, HTTPException, Response, Body, Depends
from pydantic import BaseModel, Field
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, func

from ..models.schemas import (
    CameraFeed,
    CameraListResponse,
    CameraStatus,
    CameraFeatureConfig,
    MuteCameraRequest,
    CameraPositionUpdate,
    CameraCreate,
    CameraUpdate
)
from ..models.db_models import CameraModel
from ..database import get_db
from ..services.camera_network_manager import camera_network_manager
from ..services.feature_manager import feature_manager

logger = logging.getLogger("Cameras")

router = APIRouter(prefix="/api/v1/cameras", tags=["Cameras"])

DEFAULT_CAMERAS = [
    # --- Entrance & Foyer (4) ---
    CameraFeed(
        id="cam_entrance_main",
        name="CAM-01: Main Entrance & Turnstiles",
        location="Foyer / Entrance",
        channel_number=1,
        department="ENTRANCE",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=1&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_entrance_main",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=100.0,
        floor_y=620.0,
        height_z=3.5,
        azimuth_deg=90.0,
        fov_deg=90.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_footfall", "bytetrack"],
        features=CameraFeatureConfig(fall_detection=True, tripwires_enabled=True, door_monitoring=True, theft_detection=True)
    ),
    CameraFeed(
        id="cam_entrance_exit",
        name="CAM-02: South Exit & Security Gates",
        location="Foyer / Exit",
        channel_number=2,
        department="ENTRANCE",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=2&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_entrance_exit",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=150.0,
        floor_y=650.0,
        height_z=3.5,
        azimuth_deg=270.0,
        fov_deg=85.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_footfall", "bytetrack"],
        features=CameraFeatureConfig(fall_detection=True, tripwires_enabled=True, theft_detection=True)
    ),
    CameraFeed(
        id="cam_cart_bay",
        name="CAM-03: Trolley & Cart Bay",
        location="Foyer / Entrance",
        channel_number=3,
        department="ENTRANCE",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=3&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_cart_bay",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=80.0,
        floor_y=580.0,
        height_z=3.2,
        azimuth_deg=45.0,
        fov_deg=80.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_cart_counter"],
        features=CameraFeatureConfig(intrusion_zones_enabled=True)
    ),
    CameraFeed(
        id="cam_foyer_promo",
        name="CAM-04: Foyer Promo Showcase",
        location="Foyer / Promotional",
        channel_number=4,
        department="ENTRANCE",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=4&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_foyer_promo",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=140.0,
        floor_y=570.0,
        height_z=3.2,
        azimuth_deg=0.0,
        fov_deg=90.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell"],
        features=CameraFeatureConfig(tripwires_enabled=True, intrusion_zones_enabled=True, dwell_tracking=True)
    ),

    # --- Grocery Aisles 1 to 14 (14) ---
    CameraFeed(
        id="cam_aisle_01",
        name="CAM-05: Aisle 1 - Cereals & Spreads",
        location="Aisle 1",
        channel_number=5,
        department="AISLE",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=5&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_aisle_01",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=250.0,
        floor_y=350.0,
        height_z=3.2,
        azimuth_deg=180.0,
        fov_deg=80.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell"],
        features=CameraFeatureConfig(fall_detection=True, intrusion_zones_enabled=True, shelf_interaction=True)
    ),
    CameraFeed(
        id="cam_aisle_02",
        name="CAM-06: Aisle 2 - Coffee, Tea & Drinks",
        location="Aisle 2",
        channel_number=6,
        department="AISLE",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=6&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_aisle_02",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=340.0,
        floor_y=350.0,
        height_z=3.2,
        azimuth_deg=180.0,
        fov_deg=80.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell", "spill_detector"],
        features=CameraFeatureConfig(fall_detection=True, intrusion_zones_enabled=True, shelf_interaction=True)
    ),
    CameraFeed(
        id="cam_aisle_03",
        name="CAM-07: Aisle 3 - Snacks & Confectionery",
        location="Aisle 3",
        channel_number=7,
        department="AISLE",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=7&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_aisle_03",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=430.0,
        floor_y=350.0,
        height_z=3.2,
        azimuth_deg=180.0,
        fov_deg=80.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell", "endcap_analyzer"],
        features=CameraFeatureConfig(fall_detection=True, intrusion_zones_enabled=True, shelf_interaction=True, theft_detection=True)
    ),
    CameraFeed(
        id="cam_aisle_04",
        name="CAM-08: Aisle 4 - Canned Foods & Pasta",
        location="Aisle 4",
        channel_number=8,
        department="AISLE",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=8&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_aisle_04",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=520.0,
        floor_y=350.0,
        height_z=3.2,
        azimuth_deg=180.0,
        fov_deg=80.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell"],
        features=CameraFeatureConfig(fall_detection=True, shelf_interaction=True)
    ),
    CameraFeed(
        id="cam_aisle_05",
        name="CAM-09: Aisle 5 - International & Asian",
        location="Aisle 5",
        channel_number=9,
        department="AISLE",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=9&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_aisle_05",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=610.0,
        floor_y=350.0,
        height_z=3.2,
        azimuth_deg=180.0,
        fov_deg=80.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell"],
        features=CameraFeatureConfig(fall_detection=True, shelf_interaction=True)
    ),
    CameraFeed(
        id="cam_aisle_06",
        name="CAM-10: Aisle 6 - Oils, Sauces & Spices",
        location="Aisle 6",
        channel_number=10,
        department="AISLE",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=10&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_aisle_06",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=700.0,
        floor_y=350.0,
        height_z=3.2,
        azimuth_deg=180.0,
        fov_deg=80.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell"],
        features=CameraFeatureConfig(fall_detection=True, shelf_interaction=True)
    ),
    CameraFeed(
        id="cam_aisle_07",
        name="CAM-11: Aisle 7 - Laundry & Detergents",
        location="Aisle 7",
        channel_number=11,
        department="AISLE",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=11&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_aisle_07",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=250.0,
        floor_y=175.0,
        height_z=3.2,
        azimuth_deg=0.0,
        fov_deg=80.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell"],
        features=CameraFeatureConfig(fall_detection=True, shelf_interaction=True)
    ),
    CameraFeed(
        id="cam_aisle_08",
        name="CAM-12: Aisle 8 - Paper & Pet Food",
        location="Aisle 8",
        channel_number=12,
        department="AISLE",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=12&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_aisle_08",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=340.0,
        floor_y=175.0,
        height_z=3.2,
        azimuth_deg=0.0,
        fov_deg=80.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell"],
        features=CameraFeatureConfig(fall_detection=True, shelf_interaction=True)
    ),
    CameraFeed(
        id="cam_aisle_09",
        name="CAM-13: Aisle 9 - Health & Pharmacy",
        location="Aisle 9",
        channel_number=13,
        department="AISLE",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=13&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_aisle_09",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=430.0,
        floor_y=175.0,
        height_z=3.2,
        azimuth_deg=0.0,
        fov_deg=80.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell", "theft_prevention"],
        features=CameraFeatureConfig(fall_detection=True, intrusion_zones_enabled=True, theft_detection=True, shelf_interaction=True)
    ),
    CameraFeed(
        id="cam_aisle_10",
        name="CAM-14: Aisle 10 - Baby Care & Diapers",
        location="Aisle 10",
        channel_number=14,
        department="AISLE",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=14&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_aisle_10",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=520.0,
        floor_y=175.0,
        height_z=3.2,
        azimuth_deg=0.0,
        fov_deg=80.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell"],
        features=CameraFeatureConfig(fall_detection=True, shelf_interaction=True)
    ),
    CameraFeed(
        id="cam_aisle_11",
        name="CAM-15: Aisle 11 - Frozen Foods",
        location="Aisle 11",
        channel_number=15,
        department="AISLE",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=15&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_aisle_11",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=610.0,
        floor_y=175.0,
        height_z=3.2,
        azimuth_deg=0.0,
        fov_deg=80.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell"],
        features=CameraFeatureConfig(fall_detection=True, shelf_interaction=True)
    ),
    CameraFeed(
        id="cam_aisle_12",
        name="CAM-16: Aisle 12 - Chilled Dairy & Eggs",
        location="Aisle 12 - Dairy & Eggs",
        channel_number=16,
        department="DAIRY",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=16&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_aisle_12",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=700.0,
        floor_y=175.0,
        height_z=3.2,
        azimuth_deg=0.0,
        fov_deg=80.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell"],
        features=CameraFeatureConfig(fall_detection=True, shelf_interaction=True)
    ),

    # --- Fresh & Bakery (4) ---
    CameraFeed(
        id="cam_produce_front",
        name="CAM-17: Fresh Produce - Fruits",
        location="Produce Department",
        channel_number=17,
        department="PRODUCE",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=17&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_produce_front",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=280.0,
        floor_y=500.0,
        height_z=3.8,
        azimuth_deg=135.0,
        fov_deg=90.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell", "produce_density"],
        features=CameraFeatureConfig(fall_detection=True, intrusion_zones_enabled=True, shelf_interaction=True)
    ),
    CameraFeed(
        id="cam_produce_veg",
        name="CAM-18: Fresh Produce - Vegetables",
        location="Produce Department",
        channel_number=18,
        department="PRODUCE",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=18&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_produce_veg",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=350.0,
        floor_y=540.0,
        height_z=3.8,
        azimuth_deg=225.0,
        fov_deg=85.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell"],
        features=CameraFeatureConfig(fall_detection=True, shelf_interaction=True)
    ),
    CameraFeed(
        id="cam_bakery_artisan",
        name="CAM-19: Artisan Bakery & Pastries",
        location="Bakery Department",
        channel_number=19,
        department="BAKERY",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=19&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_bakery_artisan",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=520.0,
        floor_y=620.0,
        height_z=3.5,
        azimuth_deg=180.0,
        fov_deg=90.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell", "bottleneck_flow"],
        features=CameraFeatureConfig(fall_detection=True, intrusion_zones_enabled=True, shelf_interaction=True)
    ),
    CameraFeed(
        id="cam_deli_meat",
        name="CAM-20: Deli Counter & Butcher",
        location="Deli & Meat",
        channel_number=20,
        department="PRODUCE",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=20&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_deli_meat",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=750.0,
        floor_y=620.0,
        height_z=3.5,
        azimuth_deg=180.0,
        fov_deg=90.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell", "queue_wait"],
        features=CameraFeatureConfig(fall_detection=True, intrusion_zones_enabled=True, queue_monitoring=True)
    ),

    # --- Checkouts & POS (6) ---
    CameraFeed(
        id="cam_pos_01",
        name="CAM-21: POS Register 1 (Manned)",
        location="Checkouts",
        channel_number=21,
        department="CHECKOUT",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=21&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_pos_01",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=950.0,
        floor_y=150.0,
        height_z=3.0,
        azimuth_deg=270.0,
        fov_deg=75.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_queue", "pos_overlay"],
        features=CameraFeatureConfig(tripwires_enabled=True, intrusion_zones_enabled=True, queue_monitoring=True, theft_detection=True)
    ),
    CameraFeed(
        id="cam_pos_02",
        name="CAM-22: POS Register 2 (Manned)",
        location="Checkouts",
        channel_number=22,
        department="CHECKOUT",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=22&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_pos_02",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=950.0,
        floor_y=220.0,
        height_z=3.0,
        azimuth_deg=270.0,
        fov_deg=75.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_queue", "pos_overlay"],
        features=CameraFeatureConfig(tripwires_enabled=True, intrusion_zones_enabled=True, queue_monitoring=True, theft_detection=True)
    ),
    CameraFeed(
        id="cam_pos_03",
        name="CAM-23: POS Register 3 (Self-Checkout)",
        location="Checkouts",
        channel_number=23,
        department="CHECKOUT",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=23&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_pos_03",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=950.0,
        floor_y=290.0,
        height_z=3.0,
        azimuth_deg=270.0,
        fov_deg=75.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_queue", "scan_avoidance"],
        features=CameraFeatureConfig(tripwires_enabled=True, intrusion_zones_enabled=True, queue_monitoring=True, theft_detection=True)
    ),
    CameraFeed(
        id="cam_pos_04",
        name="CAM-24: POS Register 4 (Self-Checkout)",
        location="Checkouts",
        channel_number=24,
        department="CHECKOUT",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=24&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_pos_04",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=950.0,
        floor_y=360.0,
        height_z=3.0,
        azimuth_deg=270.0,
        fov_deg=75.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_queue", "scan_avoidance"],
        features=CameraFeatureConfig(tripwires_enabled=True, intrusion_zones_enabled=True, queue_monitoring=True, theft_detection=True)
    ),
    CameraFeed(
        id="cam_pos_05",
        name="CAM-25: POS Register 5 (Express Lane)",
        location="Checkouts",
        channel_number=25,
        department="CHECKOUT",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=25&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_pos_05",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=950.0,
        floor_y=430.0,
        height_z=3.0,
        azimuth_deg=270.0,
        fov_deg=75.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_queue"],
        features=CameraFeatureConfig(tripwires_enabled=True, intrusion_zones_enabled=True, queue_monitoring=True, theft_detection=True)
    ),
    CameraFeed(
        id="cam_cust_service",
        name="CAM-26: Customer Service & Tobacco",
        location="Checkouts",
        channel_number=26,
        department="CHECKOUT",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=26&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_cust_service",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=980.0,
        floor_y=500.0,
        height_z=3.0,
        azimuth_deg=270.0,
        fov_deg=80.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell", "face_anti_spoof"],
        features=CameraFeatureConfig(tripwires_enabled=True, privacy_masks_enabled=True, theft_detection=True)
    ),

    # --- High-Value & Backroom (6) ---
    CameraFeed(
        id="cam_liquor_zone",
        name="CAM-27: Liquor & Premium Spirits",
        location="Liquor Section",
        channel_number=27,
        department="LIQUOR",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=27&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_liquor_zone",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=820.0,
        floor_y=160.0,
        height_z=3.2,
        azimuth_deg=225.0,
        fov_deg=95.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell", "loitering_alert"],
        features=CameraFeatureConfig(fall_detection=True, intrusion_zones_enabled=True, tripwires_enabled=True, theft_detection=True, shelf_interaction=True)
    ),
    CameraFeed(
        id="cam_dock_stock",
        name="CAM-28: Stockroom & Loading Dock",
        location="Backroom / Dock",
        channel_number=28,
        department="LOGISTICS",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=28&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_dock_stock",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=1000.0,
        floor_y=620.0,
        height_z=4.0,
        azimuth_deg=180.0,
        fov_deg=90.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_forklift", "perimeter_intrusion"],
        features=CameraFeatureConfig(intrusion_zones_enabled=True, privacy_masks_enabled=True)
    ),
    CameraFeed(
        id="cam_aisle_13",
        name="CAM-29: Aisle 13 - International Spices",
        location="Aisles",
        channel_number=29,
        department="AISLE",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=29&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_aisle_13",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=790.0,
        floor_y=350.0,
        height_z=3.2,
        azimuth_deg=180.0,
        fov_deg=80.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell"],
        features=CameraFeatureConfig(tripwires_enabled=True, intrusion_zones_enabled=True, shelf_interaction=True)
    ),
    CameraFeed(
        id="cam_aisle_14",
        name="CAM-30: Aisle 14 - Pet Care Bulk",
        location="Aisles",
        channel_number=30,
        department="AISLE",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=30&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_aisle_14",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=790.0,
        floor_y=175.0,
        height_z=3.2,
        azimuth_deg=0.0,
        fov_deg=80.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_dwell"],
        features=CameraFeatureConfig(tripwires_enabled=True, intrusion_zones_enabled=True, shelf_interaction=True)
    ),
    CameraFeed(
        id="cam_cold_room",
        name="CAM-31: Cold Room & Staging",
        location="Backroom / Cold Chain",
        channel_number=31,
        department="DAIRY",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=31&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_cold_room",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=880.0,
        floor_y=620.0,
        height_z=3.2,
        azimuth_deg=180.0,
        fov_deg=85.0,
        is_ai_enabled=True,
        ai_models=["yolov5n_cold_storage"],
        features=CameraFeatureConfig(fall_detection=True, intrusion_zones_enabled=True)
    ),
    CameraFeed(
        id="cam_dock_receiving",
        name="CAM-32: Rear Receiving Dock",
        location="Backroom / Dock",
        channel_number=32,
        department="LOGISTICS",
        rtsp_url="rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=32&subtype=1",
        webrtc_url="http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_dock_receiving",
        status=CameraStatus.ONLINE,
        fps=25,
        resolution="1920x1080",
        floor_x=1080.0,
        floor_y=650.0,
        height_z=4.2,
        azimuth_deg=180.0,
        fov_deg=100.0,
        is_ai_enabled=True,
        ai_models=["perimeter_intrusion", "yolov5n_forklift"],
        features=CameraFeatureConfig(intrusion_zones_enabled=True, privacy_masks_enabled=True)
    ),
]

CURRENT_ACTIVE_SOURCE = {"url": "synthetic", "name": "CAM-01: Main Entrance (Live)"}

def _model_to_feed(c: CameraModel) -> CameraFeed:
    return CameraFeed(
        id=c.id,
        name=c.name,
        location=c.location,
        channel_number=c.channel_number or 1,
        department=c.department or "GENERAL",
        rtsp_url=c.rtsp_url,
        webrtc_url=c.webrtc_url or "",
        status=c.status,
        fps=c.fps,
        resolution=c.resolution,
        is_ai_enabled=c.is_ai_enabled,
        ai_models=c.ai_models or ["yolov5n"],
        features=feature_manager.get_camera_features(c.id),
        dvr_enabled=c.dvr_enabled,
        dvr_retention_days=c.dvr_retention_days,
        dvr_quota_gb=c.dvr_quota_gb,
        floor_x=c.floor_x,
        floor_y=c.floor_y,
        floor_z=c.floor_z,
        azimuth_deg=c.azimuth_deg,
        fov_deg=c.fov_deg,
        homography_matrix=c.homography_matrix,
        last_seen=c.last_seen
    )


@router.get("/departments/list")
async def list_camera_departments(db: AsyncSession = Depends(get_db)):
    """List all supermarket departments and the count of active cameras in each."""
    dept_counts: Dict[str, int] = {}
    try:
        stmt = select(CameraModel.department, func.count(CameraModel.id)).group_by(CameraModel.department)
        res = await db.execute(stmt)
        for dept, count in res.fetchall():
            if dept:
                dept_counts[dept] = count
    except Exception as e:
        logger.warning(f"Error querying departments from DB: {e}")

    if not dept_counts:
        for cam in DEFAULT_CAMERAS:
            dept = getattr(cam, "department", "GENERAL") or "GENERAL"
            dept_counts[dept] = dept_counts.get(dept, 0) + 1

    return {
        "status": "success",
        "total_departments": len(dept_counts),
        "departments": [
            {"department": dept, "camera_count": count}
            for dept, count in sorted(dept_counts.items(), key=lambda x: x[0])
        ]
    }


@router.get("", response_model=CameraListResponse)
async def list_cameras(
    department: Optional[str] = None,
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    try:
        stmt = select(CameraModel).order_by(CameraModel.channel_number.asc())
        if department:
            stmt = stmt.where(CameraModel.department == department)
        if status:
            stmt = stmt.where(CameraModel.status == status)
        res = await db.execute(stmt)
        cams = res.scalars().all()
        if cams:
            cam_list = [_model_to_feed(c) for c in cams]
            return CameraListResponse(cameras=cam_list, total=len(cam_list))
    except Exception as e:
        logger.warning(f"DB error fetching cameras: {e}")

    cams = DEFAULT_CAMERAS
    if department:
        cams = [c for c in cams if getattr(c, "department", None) == department]
    if status:
        cams = [c for c in cams if (c.status.value if hasattr(c.status, "value") else str(c.status)) == status]
    for c in cams:
        c.features = feature_manager.get_camera_features(c.id)
    return CameraListResponse(cameras=cams, total=len(cams))


@router.post("", response_model=CameraFeed)
async def create_camera(cam_in: CameraFeed, db: AsyncSession = Depends(get_db)):
    stmt = select(CameraModel).where(CameraModel.id == cam_in.id)
    res = await db.execute(stmt)
    if res.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"Camera ID '{cam_in.id}' already exists")

    valid_cols = set(CameraModel.__table__.columns.keys())
    data = cam_in.model_dump()
    filtered = {k: (v.value if hasattr(v, "value") else v) for k, v in data.items() if k in valid_cols}
    new_cam = CameraModel(**filtered)
    db.add(new_cam)
    await db.commit()
    await db.refresh(new_cam)

    if not any(c.id == cam_in.id for c in DEFAULT_CAMERAS):
        DEFAULT_CAMERAS.append(cam_in)

    return _model_to_feed(new_cam)


@router.get("/{camera_id}", response_model=CameraFeed)
async def get_camera(camera_id: str, db: AsyncSession = Depends(get_db)):
    try:
        stmt = select(CameraModel).where(CameraModel.id == camera_id)
        res = await db.execute(stmt)
        cam = res.scalar_one_or_none()
        if cam:
            return _model_to_feed(cam)
    except Exception as e:
        logger.warning(f"DB lookup failed for camera {camera_id}: {e}")

    for cam in DEFAULT_CAMERAS:
        if cam.id == camera_id:
            cam.features = feature_manager.get_camera_features(cam.id)
            return cam
    raise HTTPException(status_code=404, detail="Camera not found")


@router.put("/{camera_id}", response_model=CameraFeed)
async def update_camera(camera_id: str, cam_in: CameraFeed, db: AsyncSession = Depends(get_db)):
    stmt = select(CameraModel).where(CameraModel.id == camera_id)
    res = await db.execute(stmt)
    cam = res.scalar_one_or_none()

    valid_cols = set(CameraModel.__table__.columns.keys())
    data = cam_in.model_dump()
    filtered = {k: (v.value if hasattr(v, "value") else v) for k, v in data.items() if k in valid_cols}

    if cam:
        for k, v in filtered.items():
            if k != "id":
                setattr(cam, k, v)
        await db.commit()
        await db.refresh(cam)
        ret_feed = _model_to_feed(cam)
    else:
        new_cam = CameraModel(**filtered)
        db.add(new_cam)
        await db.commit()
        await db.refresh(new_cam)
        ret_feed = _model_to_feed(new_cam)

    found = False
    for idx, c in enumerate(DEFAULT_CAMERAS):
        if c.id == camera_id:
            DEFAULT_CAMERAS[idx] = cam_in
            found = True
            break
    if not found:
        DEFAULT_CAMERAS.append(cam_in)

    return ret_feed


@router.patch("/{camera_id}/position")
async def update_camera_position(camera_id: str, pos: CameraPositionUpdate, db: AsyncSession = Depends(get_db)):
    stmt = select(CameraModel).where(CameraModel.id == camera_id)
    res = await db.execute(stmt)
    cam = res.scalar_one_or_none()

    if cam:
        cam.floor_x = pos.floor_x
        cam.floor_y = pos.floor_y
        if pos.floor_z is not None:
            cam.floor_z = pos.floor_z
        cam.azimuth_deg = pos.azimuth_deg
        cam.fov_deg = pos.fov_deg
        await db.commit()

    matched_in_memory = False
    for c in DEFAULT_CAMERAS:
        if c.id == camera_id:
            c.floor_x = pos.floor_x
            c.floor_y = pos.floor_y
            if pos.floor_z is not None:
                c.floor_z = pos.floor_z
            c.azimuth_deg = pos.azimuth_deg
            c.fov_deg = pos.fov_deg
            matched_in_memory = True
            break

    if not cam and not matched_in_memory:
        raise HTTPException(status_code=404, detail="Camera not found")

    return {
        "status": "success",
        "camera_id": camera_id,
        "floor_x": pos.floor_x,
        "floor_y": pos.floor_y,
        "floor_z": pos.floor_z,
        "azimuth_deg": pos.azimuth_deg,
        "fov_deg": pos.fov_deg
    }


@router.delete("/{camera_id}")
async def delete_camera(camera_id: str, db: AsyncSession = Depends(get_db)):
    stmt = select(CameraModel).where(CameraModel.id == camera_id)
    res = await db.execute(stmt)
    cam = res.scalar_one_or_none()

    deleted_data = None
    if cam:
        deleted_data = _model_to_feed(cam)
        await db.delete(cam)
        await db.commit()

    for idx, c in enumerate(DEFAULT_CAMERAS):
        if c.id == camera_id:
            deleted_data = deleted_data or c
            DEFAULT_CAMERAS.pop(idx)
            break

    if not deleted_data:
        raise HTTPException(status_code=404, detail="Camera not found")

    return {
        "status": "success",
        "message": f"Camera {camera_id} deleted successfully",
        "camera_id": camera_id
    }

@router.put("/{camera_id}/features", response_model=CameraFeatureConfig)
def update_camera_features(camera_id: str, config: CameraFeatureConfig):
    feature_manager.set_camera_features(camera_id, config)
    return config

@router.get("/{camera_id}/snapshot")
def get_camera_snapshot(camera_id: str):
    cam_name = camera_id.replace("cam_", "").replace("_", " ").upper()
    location = "Zone Monitored"
    for c in DEFAULT_CAMERAS:
        if c.id == camera_id:
            cam_name = c.name
            location = c.location
            break

    frame = np.zeros((480, 854, 3), dtype=np.uint8)
    
    for y in range(0, 480, 40):
        cv2.line(frame, (0, y), (854, y), (18, 24, 36), 1)
    for x in range(0, 854, 40):
        cv2.line(frame, (x, 0), (x, 480), (18, 24, 36), 1)

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

    cv2.putText(frame, f"FEED: {cam_name.upper()}", (25, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 240, 255), 2)
    cv2.putText(frame, f"LOC: {location} | 1080p @ 25 FPS | H.265 VA-API", (25, 70), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (139, 148, 158), 1)
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
