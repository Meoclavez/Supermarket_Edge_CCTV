"""High-Fidelity Supermarket Seed Data & 25-Camera Simulation Generator.

Populates:
1. Store Layout Blueprint (50m x 30m, Aisles 1-8, Dairy, Produce, Bakery, Meat,
   Checkout Lanes 1-6, Entrance, Exit, Endcaps, Customer Service, Loading Dock).
2. 25 Dahua Camera Streams with calibrated 3x3 Homography matrices.
3. 500+ Customer Tracklets & Global Shopping Journeys with 128-dim Re-ID embeddings.
4. 200+ POS Transactions correlated with vision customer tracks.
5. Pre-computed Zone Conversion Funnels, Queue Metrics, and Zero-PII Demographics.
6. Automated Operational Recommendations synthesized by the Decision Engine.
"""

from __future__ import annotations

import json
import logging
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from pydantic import BaseModel, Field

from .retail_analytics_service import (
    CameraTracklet,
    DemographicsReport,
    FunnelMetrics,
    GlobalCustomerJourney,
    HomographyCalibration,
    HomographyTransformer,
    LaneStatus,
    MultiCameraJourneyStitcher,
    QueueMetrics,
    RetailFunnelCalculator,
    Waypoint,
    demographics_aggregator,
    homography_transformer,
    queue_analytics_service,
)
from .retail_decision_engine import (
    RetailAnomalyType,
    RetailDecisionEngine,
    RetailRecommendation,
    ShelfInteractionSummary,
    retail_decision_engine,
)

logger = logging.getLogger("RetailSeedData")


# ============================================================================
# 1. Supermarket Zone Layout & Geometry Definition (50m x 30m Floorplan)
# ============================================================================

STORE_WIDTH_M = 50.0   # X: 0.0 to 50.0 meters
STORE_HEIGHT_M = 30.0  # Y: 0.0 to 30.0 meters

SUPERMARKET_ZONES = {
    "zone_entrance": {
        "name": "Main Entrance Foyer",
        "category": "Entrance/Exit",
        "bounds": [0.0, 0.0, 8.0, 6.0],  # [x_min, y_min, x_max, y_max]
        "cameras": ["cam_01"],
    },
    "zone_exit": {
        "name": "Exit Lobby & Loss Prevention",
        "category": "Entrance/Exit",
        "bounds": [0.0, 24.0, 8.0, 30.0],
        "cameras": ["cam_02"],
    },
    "zone_produce": {
        "name": "Fresh Organic Produce",
        "category": "Perishables",
        "bounds": [10.0, 0.0, 22.0, 8.0],
        "cameras": ["cam_03", "cam_04"],
    },
    "zone_bakery": {
        "name": "Artisan Bakery & Delicatessen",
        "category": "Fresh Prepared",
        "bounds": [24.0, 0.0, 36.0, 8.0],
        "cameras": ["cam_05", "cam_06"],
    },
    "zone_meat": {
        "name": "Fresh Meat & Seafood Counter",
        "category": "Fresh Meat",
        "bounds": [38.0, 0.0, 50.0, 8.0],
        "cameras": ["cam_07", "cam_08"],
    },
    "zone_dairy": {
        "name": "Dairy & Chilled Beverages",
        "category": "Chilled",
        "bounds": [38.0, 10.0, 50.0, 22.0],
        "cameras": ["cam_09", "cam_10"],
    },
    "zone_aisle_1": {
        "name": "Aisle 1 - Breakfast & Hot Beverages",
        "category": "Dry Grocery",
        "bounds": [10.0, 10.0, 16.0, 16.0],
        "cameras": ["cam_11"],
    },
    "zone_aisle_2": {
        "name": "Aisle 2 - Snacks, Biscuits & Chips",
        "category": "Dry Grocery",
        "bounds": [17.0, 10.0, 23.0, 16.0],
        "cameras": ["cam_12"],
    },
    "zone_aisle_3": {
        "name": "Aisle 3 - Canned Goods, Soups & Condiments",
        "category": "Dry Grocery",
        "bounds": [24.0, 10.0, 30.0, 16.0],
        "cameras": ["cam_13"],
    },
    "zone_aisle_4": {
        "name": "Aisle 4 - Pasta, Rice, Grains & Oils",
        "category": "Dry Grocery",
        "bounds": [31.0, 10.0, 37.0, 16.0],
        "cameras": ["cam_14"],
    },
    "zone_aisle_5": {
        "name": "Aisle 5 - Soft Drinks, Juice & Waters",
        "category": "Beverages",
        "bounds": [10.0, 17.0, 16.0, 23.0],
        "cameras": ["cam_15"],
    },
    "zone_aisle_6": {
        "name": "Aisle 6 - Health, Beauty & Pharmacy",
        "category": "Non-Food",
        "bounds": [17.0, 17.0, 23.0, 23.0],
        "cameras": ["cam_16"],
    },
    "zone_aisle_7": {
        "name": "Aisle 7 - Cleaning, Laundry & Paper Goods",
        "category": "Non-Food",
        "bounds": [24.0, 17.0, 30.0, 23.0],
        "cameras": ["cam_17"],
    },
    "zone_aisle_8": {
        "name": "Aisle 8 - Baby Care & Pet Food Supplies",
        "category": "Non-Food",
        "bounds": [31.0, 17.0, 37.0, 23.0],
        "cameras": ["cam_18"],
    },
    "zone_endcap_promo_a": {
        "name": "Front Promotional Feature Endcap",
        "category": "Feature Display",
        "bounds": [8.0, 8.0, 14.0, 10.0],
        "cameras": ["cam_19"],
    },
    "zone_endcap_promo_b": {
        "name": "Rear Promotional Feature Endcap",
        "category": "Feature Display",
        "bounds": [34.0, 23.0, 40.0, 25.0],
        "cameras": ["cam_20"],
    },
    "zone_checkout_1_2": {
        "name": "Express Checkouts 1 & 2 (Assisted)",
        "category": "Checkout",
        "bounds": [8.0, 24.0, 14.0, 30.0],
        "cameras": ["cam_21"],
    },
    "zone_checkout_3_4": {
        "name": "Conveyor Checkouts 3 & 4 (Full Service)",
        "category": "Checkout",
        "bounds": [15.0, 24.0, 21.0, 30.0],
        "cameras": ["cam_22"],
    },
    "zone_checkout_5_6": {
        "name": "Self-Service Checkout Terminals 5 & 6",
        "category": "Checkout",
        "bounds": [22.0, 24.0, 28.0, 30.0],
        "cameras": ["cam_23"],
    },
    "zone_customer_service": {
        "name": "Customer Service & Click-and-Collect",
        "category": "Service",
        "bounds": [30.0, 24.0, 37.0, 30.0],
        "cameras": ["cam_24"],
    },
    "zone_loading_dock": {
        "name": "Rear Stockroom & Loading Bay",
        "category": "Staff Only",
        "bounds": [40.0, 24.0, 50.0, 30.0],
        "cameras": ["cam_25"],
    },
}


# ============================================================================
# 2. 25 Dahua Camera Stream Configurations & Calibrated Homographies
# ============================================================================

def generate_25_camera_definitions() -> List[Dict[str, Any]]:
    """Generate 25 realistic CCTV cameras with Dahua RTSP streams and calibrated homography matrices."""
    cameras: List[Dict[str, Any]] = []

    camera_metadata = [
        ("cam_01", "Entrance Lobby & Welcome Gateway", "zone_entrance", [2.0, 2.0], [0.0, 0.0, 8.0, 6.0]),
        ("cam_02", "Exit Lobby & Loss Prevention Foyer", "zone_exit", [2.0, 27.0], [0.0, 24.0, 8.0, 30.0]),
        ("cam_03", "Produce North - Apples & Citrus Island", "zone_produce", [13.0, 3.0], [10.0, 0.0, 16.0, 8.0]),
        ("cam_04", "Produce South - Greens & Salads Wall", "zone_produce", [19.0, 3.0], [16.0, 0.0, 22.0, 8.0]),
        ("cam_05", "Artisan Bakery & Fresh Bread Ovens", "zone_bakery", [27.0, 3.0], [24.0, 0.0, 30.0, 8.0]),
        ("cam_06", "Delicatessen & Gourmet Cheese Case", "zone_bakery", [33.0, 3.0], [30.0, 0.0, 36.0, 8.0]),
        ("cam_07", "Butchery & Premium Beef Display", "zone_meat", [41.0, 3.0], [38.0, 0.0, 44.0, 8.0]),
        ("cam_08", "Seafood Counter & Poultry Wall", "zone_meat", [47.0, 3.0], [44.0, 0.0, 50.0, 8.0]),
        ("cam_09", "Dairy North - Fresh Milk & Yogurts", "zone_dairy", [44.0, 13.0], [38.0, 10.0, 50.0, 16.0]),
        ("cam_10", "Dairy South - Cheese, Butter & Eggs", "zone_dairy", [44.0, 19.0], [38.0, 16.0, 50.0, 22.0]),
        ("cam_11", "Aisle 1 - Cereals, Coffee & Tea", "zone_aisle_1", [13.0, 13.0], [10.0, 10.0, 16.0, 16.0]),
        ("cam_12", "Aisle 2 - Chips, Chocolates & Sweets", "zone_aisle_2", [20.0, 13.0], [17.0, 10.0, 23.0, 16.0]),
        ("cam_13", "Aisle 3 - Canned Goods & Marinades", "zone_aisle_3", [27.0, 13.0], [24.0, 10.0, 30.0, 16.0]),
        ("cam_14", "Aisle 4 - Pasta, Rice & Asian Foods", "zone_aisle_4", [34.0, 13.0], [31.0, 10.0, 37.0, 16.0]),
        ("cam_15", "Aisle 5 - Soft Drinks, Sodas & Water", "zone_aisle_5", [13.0, 20.0], [10.0, 17.0, 16.0, 23.0]),
        ("cam_16", "Aisle 6 - Shampoo, Oral Care & Health", "zone_aisle_6", [20.0, 20.0], [17.0, 17.0, 23.0, 23.0]),
        ("cam_17", "Aisle 7 - Laundry, Cleaners & Bleach", "zone_aisle_7", [27.0, 20.0], [24.0, 17.0, 30.0, 23.0]),
        ("cam_18", "Aisle 8 - Pet Food, Cat Litter & Baby", "zone_aisle_8", [34.0, 20.0], [31.0, 17.0, 37.0, 23.0]),
        ("cam_19", "Front Endcap A - Weekly Half-Price Specials", "zone_endcap_promo_a", [11.0, 9.0], [8.0, 8.0, 14.0, 10.0]),
        ("cam_20", "Rear Endcap B - Seasonal Holiday Feature", "zone_endcap_promo_b", [37.0, 24.0], [34.0, 23.0, 40.0, 25.0]),
        ("cam_21", "Checkout 1 & 2 - Express Lanes Queue", "zone_checkout_1_2", [11.0, 27.0], [8.0, 24.0, 14.0, 30.0]),
        ("cam_22", "Checkout 3 & 4 - Main Conveyor Belt Queue", "zone_checkout_3_4", [18.0, 27.0], [15.0, 24.0, 21.0, 30.0]),
        ("cam_23", "Checkout 5 & 6 - Self-Service POD Hub", "zone_checkout_5_6", [25.0, 27.0], [22.0, 24.0, 28.0, 30.0]),
        ("cam_24", "Customer Service Desk & Parcels", "zone_customer_service", [33.5, 27.0], [30.0, 24.0, 37.0, 30.0]),
        ("cam_25", "Loading Dock & Stock Receiving Gate", "zone_loading_dock", [45.0, 27.0], [40.0, 24.0, 50.0, 30.0]),
    ]

    for idx, (cam_id, name, zone_id, center, bounds) in enumerate(camera_metadata, start=1):
        x_min, y_min, x_max, y_max = bounds

        # Generate four reference point pairs mapping normalized image (u,v) -> blueprint (X,Y)
        # Image coordinates (0,0), (1,0), (1,1), (0,1) with slight perspective tilt
        img_pts = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
        bp_pts = [
            (x_min, y_min),
            (x_max, y_min + 0.2),
            (x_max - 0.2, y_max),
            (x_min + 0.1, y_max - 0.1),
        ]

        # Compute Homography Matrix H via DLT
        H, rmse = HomographyTransformer.estimate_homography_dlt(img_pts, bp_pts)

        # Register in global transformer
        calib = HomographyCalibration(
            camera_id=cam_id,
            matrix_3x3=H.tolist(),
            reference_points_image=img_pts,
            reference_points_blueprint=bp_pts,
            reprojection_rmse=round(rmse, 4),
            calibrated_at=datetime.now(timezone.utc),
        )
        homography_transformer.register_calibration(calib)

        camera_dict = {
            "id": cam_id,
            "name": name,
            "channel_number": idx,
            "location": SUPERMARKET_ZONES[zone_id]["name"],
            "zone_id": zone_id,
            "blueprint_center": center,
            "blueprint_bounds": bounds,
            "rtsp_url": f"rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel={idx}&subtype=1",
            "rtsp_main_url": f"rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel={idx}&subtype=0",
            "webrtc_url": f"http://127.0.0.1:8555/{cam_id}",
            "fps": 25,
            "resolution": "1280x720",
            "is_ai_enabled": True,
            "ai_models": ["yolov8x_retail", "bytetrack", "osnet_reid", "action_heuristics"],
            "homography_matrix": H.tolist(),
            "reprojection_rmse": round(rmse, 4),
        }
        cameras.append(camera_dict)

    return cameras


# ============================================================================
# 3. 500+ Customer Tracklets & Global Shopping Journeys Generator
# ============================================================================

CUSTOMER_PROFILES = [
    {"type": "QUICK_BASKET", "weight": 0.35, "zone_count": (2, 4), "dwell_scale": 1.0, "convert_prob": 0.85},
    {"type": "FULL_FAMILY_GROCERY", "weight": 0.40, "zone_count": (6, 12), "dwell_scale": 2.2, "convert_prob": 0.95},
    {"type": "FRESH_PERISHABLES_ONLY", "weight": 0.15, "zone_count": (3, 5), "dwell_scale": 1.5, "convert_prob": 0.90},
    {"type": "BROWSER_PRICE_CHECKER", "weight": 0.10, "zone_count": (4, 7), "dwell_scale": 1.8, "convert_prob": 0.25},
]

AISLE_SKUS = {
    "zone_produce": ["Honeycrisp Apples", "Cavendish Bananas", "Organic Hass Avocado", "Baby Spinach 250g", "Roma Tomatoes"],
    "zone_bakery": ["Sourdough Cob Loaf", "Butter Croissants 4pk", "Brie Cheese 200g", "Prosciutto Di Parma 100g"],
    "zone_meat": ["Angus Ribeye Steak 400g", "Free Range Chicken Breast", "Tasmanian Salmon Fillet", "Lean Pork Chops"],
    "zone_dairy": ["Full Cream Milk 2L", "Greek Style Yogurt 1kg", "Salted Butter 250g", "Cheddar Cheese Block 500g"],
    "zone_aisle_1": ["Toasted Muesli 750g", "Arabica Coffee Beans 1kg", "English Breakfast Tea 100pk"],
    "zone_aisle_2": ["Sea Salt Potato Chips 175g", "Dark Chocolate 70% 100g", "Artisan Crackers 150g"],
    "zone_aisle_3": ["Italian Diced Tomatoes 400g", "Extra Virgin Olive Oil 1L", "Spaghetti Pasta 500g"],
    "zone_aisle_4": ["Basmati Rice 5kg", "Organic Quinoa 500g", "Soy Sauce 500ml"],
    "zone_aisle_5": ["Sparkling Mineral Water 1.25L", "Zero Cola 30pk", "Pure Orange Juice 2L"],
    "zone_aisle_6": ["Hydrating Shampoo 400ml", "Total Protection Toothpaste", "Moisturizing Cream 100g"],
    "zone_aisle_7": ["Eco Laundry Liquid 2L", "Multipurpose Antibacterial Spray", "Ultra Soft Toilet Paper 12pk"],
    "zone_aisle_8": ["Grain-Free Dog Kibble 3kg", "Gourmet Cat Pouches 12pk", "Hypoallergenic Baby Wipes 80pk"],
    "zone_endcap_promo_a": ["Half-Price Hazelnut Spread 1kg", "Promo Roast Almonds 400g"],
    "zone_endcap_promo_b": ["Special Edition Holiday Treats", "Festive Biscuit Tin 500g"],
}


def _generate_synthetic_embedding(base_seed: int, dimension: int = 128) -> List[float]:
    """Generate normalized synthetic 128-dim appearance embedding for Re-ID."""
    rng = np.random.default_rng(base_seed)
    raw = rng.standard_normal(dimension)
    norm = np.linalg.norm(raw)
    normalized = (raw / norm).tolist() if norm > 1e-9 else raw.tolist()
    return [round(x, 5) for x in normalized]


def generate_synthetic_customer_journeys_and_tracklets(
    total_customers: int = 500,
    base_time: Optional[datetime] = None,
) -> Tuple[List[CameraTracklet], List[GlobalCustomerJourney], List[Dict[str, Any]]]:
    """Generate 500+ realistic customer tracks, camera tracklets, and POS transactions."""
    if base_time is None:
        # Fixed benchmark trading day: 8:00 AM to 8:00 PM
        base_time = datetime(2026, 9, 1, 8, 0, 0)

    tracklets: List[CameraTracklet] = []
    global_journeys: List[GlobalCustomerJourney] = []
    pos_transactions: List[Dict[str, Any]] = []

    profile_types = [p["type"] for p in CUSTOMER_PROFILES]
    profile_weights = [p["weight"] for p in CUSTOMER_PROFILES]

    aisle_zones = [k for k in SUPERMARKET_ZONES.keys() if k not in ["zone_entrance", "zone_exit", "zone_customer_service", "zone_loading_dock"]]
    checkout_zones = ["zone_checkout_1_2", "zone_checkout_3_4", "zone_checkout_5_6"]

    track_id_counter = 1000
    pos_id_counter = 5000

    for cust_idx in range(total_customers):
        # Pick profile
        prof_name = random.choices(profile_types, weights=profile_weights, k=1)[0]
        prof = next(p for p in CUSTOMER_PROFILES if p["type"] == prof_name)

        # Stagger entrance timestamp throughout the trading day (12 hours = 43200 seconds)
        trip_start_sec = random.uniform(0, 41400)
        cust_start_time = base_time + timedelta(seconds=trip_start_sec)
        current_time = cust_start_time

        # Consistent appearance embedding per customer (slight perturbation for cross-camera robustness)
        customer_base_embedding = _generate_synthetic_embedding(10000 + cust_idx)

        # Build zone sequence
        num_zones = random.randint(prof["zone_count"][0], prof["zone_count"][1])
        if prof_name == "FRESH_PERISHABLES_ONLY":
            candidate_zones = ["zone_produce", "zone_bakery", "zone_meat", "zone_dairy"]
            visited_zones = random.sample(candidate_zones, k=min(num_zones, len(candidate_zones)))
        else:
            visited_zones = random.sample(aisle_zones, k=min(num_zones, len(aisle_zones)))

        # Journey route: Entrance -> Aisle Sequence -> Checkout (if converted) -> Exit
        route_zones = ["zone_entrance"] + visited_zones
        converts = (random.random() <= prof["convert_prob"])
        chosen_checkout = random.choice(checkout_zones) if converts else None

        if converts:
            route_zones.append(chosen_checkout)
        route_zones.append("zone_exit")

        customer_tracklets: List[CameraTracklet] = []
        purchased_items: List[str] = []
        interacted_items_all: List[str] = []
        waypoints_all: List[Waypoint] = []
        zone_dwell_map: Dict[str, float] = {}

        for z_id in route_zones:
            zone_info = SUPERMARKET_ZONES[z_id]
            cam_id = random.choice(zone_info["cameras"])
            x_min, y_min, x_max, y_max = zone_info["bounds"]

            # Dwell duration
            if z_id in ["zone_entrance", "zone_exit"]:
                dwell_sec = random.uniform(3.0, 10.0)
            elif "checkout" in z_id:
                dwell_sec = random.uniform(60.0, 240.0)
            else:
                dwell_sec = random.uniform(15.0, 90.0) * prof["dwell_scale"]

            tracklet_end_time = current_time + timedelta(seconds=dwell_sec)

            # Image & Blueprint Coordinates
            u_start, v_start = random.uniform(0.1, 0.4), random.uniform(0.1, 0.4)
            u_end, v_end = random.uniform(0.6, 0.9), random.uniform(0.6, 0.9)

            bp_start = homography_transformer.image_to_blueprint(cam_id, u_start, v_start)
            bp_end = homography_transformer.image_to_blueprint(cam_id, u_end, v_end)

            # Shelf interactions & put-backs
            interacted = False
            instant_putback = False
            track_items: List[str] = []

            if z_id in AISLE_SKUS and random.random() < 0.65:
                interacted = True
                possible_skus = AISLE_SKUS[z_id]
                chosen_sku = random.choice(possible_skus)
                track_items.append(chosen_sku)
                interacted_items_all.append(chosen_sku)

                # Special condition: In Aisle 3 (Canned Goods) or Dead Zone, simulate stockout or pricing friction
                if z_id == "zone_aisle_3" and random.random() < 0.80:
                    instant_putback = True  # High put-back anomaly
                elif prof_name == "BROWSER_PRICE_CHECKER" and random.random() < 0.70:
                    instant_putback = True
                else:
                    if converts:
                        purchased_items.append(chosen_sku)

            # Add slight noise to Re-ID embedding to simulate realistic camera lighting variance
            cam_reid_vec = np.array(customer_base_embedding) + np.random.normal(0, 0.02, 128)
            cam_reid_vec = (cam_reid_vec / np.linalg.norm(cam_reid_vec)).tolist()

            t_obj = CameraTracklet(
                track_id=track_id_counter,
                camera_id=cam_id,
                start_time=current_time,
                end_time=tracklet_end_time,
                start_point_img=(round(u_start, 3), round(v_start, 3)),
                end_point_img=(round(u_end, 3), round(v_end, 3)),
                start_point_blueprint=bp_start,
                end_point_blueprint=bp_end,
                reid_embedding=[round(x, 5) for x in cam_reid_vec],
                dwell_time_sec=round(dwell_sec, 1),
                interacted=interacted,
                interacted_items=track_items,
                instant_put_back=instant_putback,
            )
            track_id_counter += 1
            customer_tracklets.append(t_obj)
            tracklets.append(t_obj)

            zone_dwell_map[z_id] = zone_dwell_map.get(z_id, 0.0) + dwell_sec

            # Record demographics observation
            age_bucket = random.choices(["18-24", "25-34", "35-49", "50-64", "65+"], weights=[0.15, 0.35, 0.25, 0.15, 0.10], k=1)[0]
            gender_val = random.choice(["male", "female"])
            sentiment_val = "frustrated" if instant_putback else random.choices(["positive", "neutral", "negative"], weights=[0.60, 0.35, 0.05], k=1)[0]
            valence = -0.7 if sentiment_val == "frustrated" else (0.8 if sentiment_val == "positive" else 0.1)
            demographics_aggregator.record_observation(z_id, age_bucket, gender_val, sentiment_val, valence)

            # Waypoints
            waypoints_all.append(
                Waypoint(
                    timestamp=current_time,
                    camera_id=cam_id,
                    x_meters=bp_start[0],
                    y_meters=bp_start[1],
                    zone_id=z_id,
                    action="ENTER",
                )
            )
            if interacted:
                waypoints_all.append(
                    Waypoint(
                        timestamp=current_time + timedelta(seconds=dwell_sec / 2.0),
                        camera_id=cam_id,
                        x_meters=bp_end[0],
                        y_meters=bp_end[1],
                        zone_id=z_id,
                        action="INTERACT",
                    )
                )

            # Transit time to next zone: 5 to 15 seconds
            current_time = tracklet_end_time + timedelta(seconds=random.uniform(5.0, 15.0))

        # Build Global Journey
        journey_id = f"journey_sim_{cust_idx + 1:04d}"
        total_trip_dur = (current_time - cust_start_time).total_seconds()
        total_dwell_all = sum(t.dwell_time_sec for t in customer_tracklets)

        pos_tx_id = None
        pos_total = 0.0
        if converts and purchased_items:
            pos_id_counter += 1
            pos_tx_id = f"TX_POS_{pos_id_counter}"
            item_prices = [round(random.uniform(3.50, 24.99), 2) for _ in purchased_items]
            pos_total = round(sum(item_prices), 2)

            pos_record = {
                "transaction_id": pos_tx_id,
                "customer_journey_id": journey_id,
                "checkout_lane": chosen_checkout,
                "timestamp": current_time.isoformat(),
                "items": [{"name": item, "price": pr} for item, pr in zip(purchased_items, item_prices)],
                "total_amount": pos_total,
                "payment_method": random.choice(["EFT_CONTACTLESS", "CREDIT_CARD", "APPLE_PAY", "CASH"]),
            }
            pos_transactions.append(pos_record)

        g_journey = GlobalCustomerJourney(
            journey_id=journey_id,
            tracklet_ids=[(t.camera_id, t.track_id) for t in customer_tracklets],
            camera_sequence=[t.camera_id for t in customer_tracklets],
            start_time=cust_start_time,
            end_time=current_time,
            total_duration_sec=round(total_trip_dur, 1),
            total_dwell_time_sec=round(total_dwell_all, 1),
            zones_visited=route_zones,
            zone_dwell_times={k: round(v, 1) for k, v in zone_dwell_map.items()},
            trajectory=waypoints_all,
            interactions=interacted_items_all,
            has_converted=converts,
            pos_transaction_id=pos_tx_id,
            pos_total_amount=pos_total,
        )
        global_journeys.append(g_journey)

    return tracklets, global_journeys, pos_transactions


# ============================================================================
# 4. Pre-computed Analytics & Anomaly Synthesizer
# ============================================================================

def generate_precomputed_analytics(
    tracklets: List[CameraTracklet],
    journeys: List[GlobalCustomerJourney],
    pos_transactions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Calculate zone funnels, queue metrics, and decision recommendations."""
    # 1. Calculate Zone Funnels
    zone_pass_counts: Dict[str, int] = {z: 0 for z in SUPERMARKET_ZONES}
    zone_dwell_counts: Dict[str, int] = {z: 0 for z in SUPERMARKET_ZONES}
    zone_interact_counts: Dict[str, int] = {z: 0 for z in SUPERMARKET_ZONES}
    zone_sales_counts: Dict[str, int] = {z: 0 for z in SUPERMARKET_ZONES}

    for j in journeys:
        for z in j.zones_visited:
            zone_pass_counts[z] = zone_pass_counts.get(z, 0) + 1
            if j.zone_dwell_times.get(z, 0.0) >= 10.0:
                zone_dwell_counts[z] = zone_dwell_counts.get(z, 0) + 1

    for t in tracklets:
        # Find zone for camera
        z_id = next((z for z, info in SUPERMARKET_ZONES.items() if t.camera_id in info["cameras"]), None)
        if z_id and t.interacted:
            zone_interact_counts[z_id] = zone_interact_counts.get(z_id, 0) + 1

    # Map POS sales to zones
    for tx in pos_transactions:
        for itm in tx.get("items", []):
            item_name = itm["name"]
            for z, skus in AISLE_SKUS.items():
                if item_name in skus:
                    zone_sales_counts[z] = zone_sales_counts.get(z, 0) + 1

    # Inject realistic deliberate anomalies for Decision Engine validation:
    # Anomaly A: Aisle 3 (Canned Foods) High-Interest Low-Conversion / Friction
    zone_pass_counts["zone_aisle_3"] = 180
    zone_dwell_counts["zone_aisle_3"] = 95
    zone_interact_counts["zone_aisle_3"] = 48
    zone_sales_counts["zone_aisle_3"] = 4  # Friction = (48 - 4)/48 = 91.7%

    # Anomaly B: Aisle 8 (Baby & Pet) Chronic Dead-Zone (< 25% average traffic)
    zone_pass_counts["zone_aisle_8"] = 28  # Average is ~220
    zone_dwell_counts["zone_aisle_8"] = 12
    zone_interact_counts["zone_aisle_8"] = 6
    zone_sales_counts["zone_aisle_8"] = 5

    zone_funnels: Dict[str, FunnelMetrics] = {}
    for z_id in SUPERMARKET_ZONES:
        f_obj = RetailFunnelCalculator.calculate_rates(
            pass_count=zone_pass_counts[z_id],
            dwell_count=zone_dwell_counts[z_id],
            interact_count=zone_interact_counts[z_id],
            sales_count=zone_sales_counts[z_id],
            zone_id=z_id,
        )
        zone_funnels[z_id] = f_obj

    storewide_funnel = RetailFunnelCalculator.aggregate_funnels(list(zone_funnels.values()))

    # 2. Queue Analytics for Checkout Lanes 1-6
    checkout_lanes = [
        ("checkout_01_02", "cam_21", 2, [45.0, 60.0, 90.0, 110.0, 80.0], 48, True),
        ("checkout_03_04", "cam_22", 6, [280.0, 310.0, 350.0, 290.0, 420.0, 380.0], 35, True),  # Bottleneck!
        ("checkout_05_06", "cam_23", 1, [30.0, 40.0, 50.0, 35.0], 85, True),
    ]

    queue_metrics_list: List[QueueMetrics] = []
    for c_id, cam_id, q_len, wait_samples, tx_count, active in checkout_lanes:
        qm = queue_analytics_service.build_queue_metrics(
            checkout_id=c_id,
            camera_id=cam_id,
            current_queue_length=q_len,
            wait_times_seconds=wait_samples,
            completed_transactions_in_window=tx_count,
            window_minutes=60.0,
            is_cashier_active=active,
        )
        queue_metrics_list.append(qm)

    # 3. Shelf Stockout Summaries
    shelf_summaries = [
        ShelfInteractionSummary(
            zone_id="zone_produce",
            shelf_id="shelf_organic_berries",
            product_category="Organic Hass Avocado",
            reach_count=18,
            instant_put_back_count=15,  # 83% put-backs, 0 sales -> stockout!
            total_sales=0,
            avg_touch_duration_sec=1.8,
        ),
        ShelfInteractionSummary(
            zone_id="zone_bakery",
            shelf_id="shelf_fresh_sourdough",
            product_category="Sourdough Cob Loaf",
            reach_count=25,
            instant_put_back_count=2,
            total_sales=22,
            avg_touch_duration_sec=8.5,
        ),
    ]

    # 4. Synthesize Operational Recommendations
    zone_names_map = {z: info["name"] for z, info in SUPERMARKET_ZONES.items()}
    recommendations = retail_decision_engine.synthesize_store_recommendations(
        zone_funnels=zone_funnels,
        zone_names=zone_names_map,
        queue_metrics=queue_metrics_list,
        shelf_summaries=shelf_summaries,
    )

    # 5. Demographics per Zone
    demographics_reports: Dict[str, DemographicsReport] = {}
    for z_id in SUPERMARKET_ZONES:
        demographics_reports[z_id] = demographics_aggregator.get_zone_report(z_id)

    return {
        "zone_funnels": {k: (v.model_dump() if hasattr(v, "model_dump") else v.dict()) for k, v in zone_funnels.items()},
        "storewide_funnel": (storewide_funnel.model_dump() if hasattr(storewide_funnel, "model_dump") else storewide_funnel.dict()),
        "queue_metrics": [(q.model_dump() if hasattr(q, "model_dump") else q.dict()) for q in queue_metrics_list],
        "shelf_summaries": [(s.model_dump() if hasattr(s, "model_dump") else s.dict()) for s in shelf_summaries],
        "recommendations": [(r.model_dump() if hasattr(r, "model_dump") else r.dict()) for r in recommendations],
        "demographics": {k: (v.model_dump() if hasattr(v, "model_dump") else v.dict()) for k, v in demographics_reports.items()},
    }


# ============================================================================
# 5. Master Seed Data Orchestrator & Persistence
# ============================================================================

class SupermarketSeedDataManager:
    """Central manager for loading, seeding, and retrieving realistic supermarket simulation data."""

    def __init__(self, storage_path: Optional[Path] = None):
        self.storage_path = storage_path or Path("/home/meoclavezz/Projects-1/Edge_AI_CCTV/storage/retail_simulation_dataset.json")
        self._cached_dataset: Optional[Dict[str, Any]] = None

    def generate_full_dataset(self, total_customers: int = 500) -> Dict[str, Any]:
        """Generate full dataset containing layout, cameras, tracklets, journeys, POS, and precomputed analytics."""
        logger.info(f"Generating full supermarket seed data ({total_customers} simulated customers, 25 cameras)...")
        cameras = generate_25_camera_definitions()
        tracklets, journeys, pos_txs = generate_synthetic_customer_journeys_and_tracklets(total_customers)
        analytics = generate_precomputed_analytics(tracklets, journeys, pos_txs)

        dataset = {
            "metadata": {
                "store_name": "Pearcedale Supermarket (Australia)",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "store_dimensions_meters": {"width": STORE_WIDTH_M, "height": STORE_HEIGHT_M},
                "total_cameras": len(cameras),
                "total_tracklets": len(tracklets),
                "total_customer_journeys": len(journeys),
                "total_pos_transactions": len(pos_txs),
                "privacy_enforcement": "100% Zero-PII Aggregated Edge Vision",
            },
            "zones": SUPERMARKET_ZONES,
            "cameras": cameras,
            "tracklets": [(t.model_dump() if hasattr(t, "model_dump") else t.dict()) for t in tracklets],
            "customer_journeys": [(j.model_dump() if hasattr(j, "model_dump") else j.dict()) for j in journeys],
            "pos_transactions": pos_txs,
            "analytics": analytics,
        }

        self._cached_dataset = dataset
        return dataset

    def export_dataset_to_file(self, file_path: Optional[Path] = None) -> Path:
        """Export dataset JSON to file."""
        target = file_path or self.storage_path
        target.parent.mkdir(parents=True, exist_ok=True)

        if not self._cached_dataset:
            self.generate_full_dataset()

        # Handle datetime serialization cleanly
        def json_serial(obj):
            if isinstance(obj, (datetime, timedelta)):
                return str(obj)
            raise TypeError(f"Type {type(obj)} not serializable")

        with open(target, "w") as f:
            json.dump(self._cached_dataset, f, default=json_serial, indent=2)

        logger.info(f"Exported supermarket simulation dataset to {target} ({target.stat().st_size / 1024:.1f} KB)")
        return target

    def get_dataset(self) -> Dict[str, Any]:
        """Get dataset from cache or generate."""
        if self._cached_dataset is None:
            if self.storage_path.exists():
                try:
                    with open(self.storage_path, "r") as f:
                        self._cached_dataset = json.load(f)
                        return self._cached_dataset
                except Exception as e:
                    logger.warning(f"Failed to read existing simulation dataset: {e}")
            self.generate_full_dataset()
        return self._cached_dataset


# Global singleton instance
retail_seed_manager = SupermarketSeedDataManager()
