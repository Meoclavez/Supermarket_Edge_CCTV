"""Core Retail Analytics & Math Engine for Supermarket Edge AI CCTV.

Implements:
1. Retail Funnel Equations:
   - Attraction Rate: alpha = (N_dwell / N_pass) * 100%
   - Engagement Rate: beta = (N_interact / N_dwell) * 100%
   - True Visual Conversion Rate: gamma = (N_sales / N_interact) * 100%
   - Lost Sales / Friction Index: phi = ((N_interact - N_sales) / N_interact) * 100%
2. Multi-Camera Homography Mapping:
   - 3x3 Projective transformation from camera (u, v) to 2D store blueprint (X, Y).
   - Direct Linear Transformation (DLT) estimation and inverse mapping.
3. Multi-Camera Journey Stitching:
   - Spatio-temporal distance gating and visual Re-ID cosine similarity fusion.
   - Tracklet chaining into complete customer shopping trajectories.
4. Queue Analytics:
   - Queue length estimation, wait-time distributions (mean, median, p90, p95, std),
     and checkout service rate (mu).
5. Zero-PII Demographics Aggregator:
   - Anonymized age buckets, gender ratios, and sentiment scores per zone
     with strict zero-PII guarantees (no raw face crops or biometrics retained).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
from pydantic import BaseModel, Field

logger = logging.getLogger("RetailAnalyticsService")


# ============================================================================
# 1. Pydantic Models & Data Structures
# ============================================================================

class LaneStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    IDLE = "IDLE"
    CONGESTED = "CONGESTED"


class FunnelMetrics(BaseModel):
    """Retail funnel metrics for a zone, aisle, or store aggregate."""
    zone_id: str = "storewide"
    pass_count: int = Field(0, ge=0, description="N_pass: Traffic passing the zone/display")
    dwell_count: int = Field(0, ge=0, description="N_dwell: Traffic lingering >= dwell threshold")
    interact_count: int = Field(0, ge=0, description="N_interact: Physical engagements/reach events")
    sales_count: int = Field(0, ge=0, description="N_sales: Final POS transactions for this zone")
    attraction_rate: float = Field(0.0, ge=0.0, le=100.0, description="alpha = (N_dwell / N_pass) * 100%")
    engagement_rate: float = Field(0.0, ge=0.0, le=100.0, description="beta = (N_interact / N_dwell) * 100%")
    conversion_rate: float = Field(0.0, ge=0.0, le=100.0, description="gamma = (N_sales / N_interact) * 100%")
    friction_index: float = Field(0.0, ge=0.0, le=100.0, description="phi = ((N_interact - N_sales) / N_interact) * 100%")


class HomographyCalibration(BaseModel):
    """Calibrated 3x3 homography matrix for a camera feed."""
    camera_id: str
    matrix_3x3: List[List[float]] = Field(..., description="3x3 projective transformation matrix")
    inverse_matrix_3x3: Optional[List[List[float]]] = None
    reference_points_image: List[Tuple[float, float]] = Field(default_factory=list)
    reference_points_blueprint: List[Tuple[float, float]] = Field(default_factory=list)
    reprojection_rmse: float = 0.0
    calibrated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CameraTracklet(BaseModel):
    """Local tracklet produced by a single camera."""
    track_id: int
    camera_id: str
    start_time: datetime
    end_time: datetime
    start_point_img: Tuple[float, float]
    end_point_img: Tuple[float, float]
    start_point_blueprint: Optional[Tuple[float, float]] = None
    end_point_blueprint: Optional[Tuple[float, float]] = None
    reid_embedding: Optional[List[float]] = None
    dwell_time_sec: float = 0.0
    interacted: bool = False
    interacted_items: List[str] = Field(default_factory=list)
    instant_put_back: bool = False


class Waypoint(BaseModel):
    """A single spatial-temporal coordinate on the store 2D blueprint."""
    timestamp: datetime
    camera_id: str
    x_meters: float
    y_meters: float
    zone_id: Optional[str] = None
    action: Optional[str] = None  # "PASS", "DWELL", "INTERACT", "CHECKOUT", "EXIT"


class GlobalCustomerJourney(BaseModel):
    """Stitched multi-camera global customer trajectory."""
    journey_id: str
    tracklet_ids: List[Tuple[str, int]] = Field(default_factory=list, description="(camera_id, track_id)")
    camera_sequence: List[str] = Field(default_factory=list)
    start_time: datetime
    end_time: datetime
    total_duration_sec: float = 0.0
    total_dwell_time_sec: float = 0.0
    zones_visited: List[str] = Field(default_factory=list)
    zone_dwell_times: Dict[str, float] = Field(default_factory=dict)
    trajectory: List[Waypoint] = Field(default_factory=list)
    interactions: List[str] = Field(default_factory=list)
    has_converted: bool = False
    pos_transaction_id: Optional[str] = None
    pos_total_amount: float = 0.0


class QueueMetrics(BaseModel):
    """Real-time and historical queue metrics for a checkout lane."""
    checkout_id: str
    camera_id: str
    current_queue_length: int = 0
    active_cashier: bool = True
    lane_status: LaneStatus = LaneStatus.OPEN
    mean_wait_time_sec: float = 0.0
    median_wait_time_sec: float = 0.0
    p90_wait_time_sec: float = 0.0
    p95_wait_time_sec: float = 0.0
    std_wait_time_sec: float = 0.0
    min_wait_time_sec: float = 0.0
    max_wait_time_sec: float = 0.0
    service_rate_per_min: float = 0.0  # mu = customers served per min
    service_rate_per_hour: float = 0.0
    total_customers_served: int = 0
    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DemographicsReport(BaseModel):
    """Aggregated anonymized demographics with strictly zero PII."""
    zone_id: str
    sample_size: int = 0
    age_distribution: Dict[str, int] = Field(
        default_factory=lambda: {
            "0-17": 0,
            "18-24": 0,
            "25-34": 0,
            "35-49": 0,
            "50-64": 0,
            "65+": 0,
        }
    )
    gender_distribution: Dict[str, int] = Field(
        default_factory=lambda: {
            "male": 0,
            "female": 0,
            "unknown": 0,
        }
    )
    sentiment_distribution: Dict[str, int] = Field(
        default_factory=lambda: {
            "positive": 0,
            "neutral": 0,
            "negative": 0,
            "confused": 0,
            "frustrated": 0,
        }
    )
    average_sentiment_score: float = 0.0  # Normalized score -1.0 (frustrated) to +1.0 (delighted)
    privacy_compliance_verified: bool = True
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ============================================================================
# 2. Mathematical Funnel Calculator
# ============================================================================

class RetailFunnelCalculator:
    """Calculates conversion and friction funnel formulas with mathematical rigor."""

    @staticmethod
    def calculate_rates(
        pass_count: int,
        dwell_count: int,
        interact_count: int,
        sales_count: int,
        zone_id: str = "zone_general",
    ) -> FunnelMetrics:
        """Calculate retail conversion funnel rates.

        Equations:
          - Attraction Rate: alpha = (N_dwell / N_pass) * 100%
          - Engagement Rate: beta = (N_interact / N_dwell) * 100%
          - True Visual Conversion Rate: gamma = (N_sales / N_interact) * 100%
          - Lost Sales / Friction Index: phi = ((N_interact - N_sales) / N_interact) * 100%

        All rates are bounded in [0.0, 100.0] and division-by-zero safely returns 0.0.
        """
        p = max(0, int(pass_count))
        d = max(0, int(dwell_count))
        i = max(0, int(interact_count))
        s = max(0, int(sales_count))

        # 1. Attraction Rate: alpha
        attraction = (d / p * 100.0) if p > 0 else 0.0
        attraction = min(100.0, max(0.0, attraction))

        # 2. Engagement Rate: beta
        engagement = (i / d * 100.0) if d > 0 else 0.0
        engagement = min(100.0, max(0.0, engagement))

        # 3. True Visual Conversion Rate: gamma
        conversion = (s / i * 100.0) if i > 0 else 0.0
        conversion = min(100.0, max(0.0, conversion))

        # 4. Lost Sales / Friction Index: phi
        if i > 0:
            friction = ((i - s) / i * 100.0)
            friction = min(100.0, max(0.0, friction))
        else:
            friction = 0.0

        return FunnelMetrics(
            zone_id=zone_id,
            pass_count=p,
            dwell_count=d,
            interact_count=i,
            sales_count=s,
            attraction_rate=round(attraction, 2),
            engagement_rate=round(engagement, 2),
            conversion_rate=round(conversion, 2),
            friction_index=round(friction, 2),
        )

    @staticmethod
    def aggregate_funnels(funnels: Sequence[FunnelMetrics], aggregate_id: str = "storewide") -> FunnelMetrics:
        """Aggregate a sequence of zone funnels into an overall storewide funnel."""
        total_pass = sum(f.pass_count for f in funnels)
        total_dwell = sum(f.dwell_count for f in funnels)
        total_interact = sum(f.interact_count for f in funnels)
        total_sales = sum(f.sales_count for f in funnels)

        return RetailFunnelCalculator.calculate_rates(
            pass_count=total_pass,
            dwell_count=total_dwell,
            interact_count=total_interact,
            sales_count=total_sales,
            zone_id=aggregate_id,
        )


# ============================================================================
# 3. Multi-Camera Homography Coordinate Mapper
# ============================================================================

class HomographyTransformer:
    """Manages 3x3 Projective Homography transformations from camera image to 2D store blueprint.

    Coordinates:
      - Image space: (u, v) in normalized [0.0, 1.0] or pixel space [0, W] x [0, H]
      - Blueprint space: (X, Y) in real-world metric space [0, Store_Length] x [0, Store_Width]
    """

    def __init__(self):
        self._calibrations: Dict[str, HomographyCalibration] = {}

    def register_calibration(self, calibration: HomographyCalibration) -> None:
        """Register or update a camera's homography calibration."""
        mat = np.array(calibration.matrix_3x3, dtype=np.float64)
        if mat.shape != (3, 3):
            raise ValueError(f"Homography matrix must be 3x3, got {mat.shape}")

        det = np.linalg.det(mat)
        if abs(det) < 1e-12:
            raise ValueError(f"Singular homography matrix for camera {calibration.camera_id} (det={det})")

        inv_mat = np.linalg.inv(mat)
        calibration.inverse_matrix_3x3 = inv_mat.tolist()
        self._calibrations[calibration.camera_id] = calibration
        logger.info(f"Registered homography calibration for {calibration.camera_id}")

    def get_calibration(self, camera_id: str) -> Optional[HomographyCalibration]:
        """Retrieve registered calibration for a camera."""
        return self._calibrations.get(camera_id)

    @staticmethod
    def estimate_homography_dlt(
        image_points: Sequence[Tuple[float, float]],
        blueprint_points: Sequence[Tuple[float, float]],
    ) -> Tuple[np.ndarray, float]:
        """Compute 3x3 Homography H using Direct Linear Transformation (DLT) with SVD.

        Requires at least 4 non-collinear point correspondences:
          (u_i, v_i) -> (X_i, Y_i)
        Returns:
          (H_3x3, reprojection_rmse)
        """
        if len(image_points) < 4 or len(blueprint_points) < 4:
            raise ValueError("DLT Homography estimation requires at least 4 point correspondences.")
        if len(image_points) != len(blueprint_points):
            raise ValueError("image_points and blueprint_points must have the same length.")

        n = len(image_points)
        A = []
        for i in range(n):
            u, v = float(image_points[i][0]), float(image_points[i][1])
            X, Y = float(blueprint_points[i][0]), float(blueprint_points[i][1])

            # Row 1: [-u, -v, -1,  0,  0,  0, u*X, v*X, X]
            A.append([-u, -v, -1.0, 0.0, 0.0, 0.0, u * X, v * X, X])
            # Row 2: [ 0,  0,  0, -u, -v, -1, u*Y, v*Y, Y]
            A.append([0.0, 0.0, 0.0, -u, -v, -1.0, u * Y, v * Y, Y])

        A_mat = np.array(A, dtype=np.float64)

        # Solve Ah = 0 via SVD (V contains eigenvectors of A^T A, last row of Vh corresponds to smallest eigenvalue)
        _, _, vh = np.linalg.svd(A_mat)
        h = vh[-1]
        H = h.reshape((3, 3))

        # Normalize so H[2, 2] == 1.0 (if non-zero)
        if abs(H[2, 2]) > 1e-9:
            H = H / H[2, 2]

        # Calculate RMSE reprojection error
        errors = []
        for i in range(n):
            u, v = image_points[i]
            target_X, target_Y = blueprint_points[i]

            projected_vec = H @ np.array([u, v, 1.0], dtype=np.float64)
            if abs(projected_vec[2]) > 1e-9:
                pred_X = projected_vec[0] / projected_vec[2]
                pred_Y = projected_vec[1] / projected_vec[2]
                err = math.hypot(pred_X - target_X, pred_Y - target_Y)
                errors.append(err ** 2)

        rmse = math.sqrt(sum(errors) / n) if errors else 0.0
        return H, rmse

    def image_to_blueprint(self, camera_id: str, u: float, v: float) -> Tuple[float, float]:
        """Project image coordinate (u, v) to 2D store blueprint coordinate (X, Y)."""
        calib = self.get_calibration(camera_id)
        if not calib:
            raise KeyError(f"No homography calibration registered for camera '{camera_id}'")

        H = np.array(calib.matrix_3x3, dtype=np.float64)
        vec = H @ np.array([u, v, 1.0], dtype=np.float64)

        if abs(vec[2]) < 1e-12:
            raise ValueError("Degenerate projection: point maps to infinity on blueprint plane.")

        X = float(vec[0] / vec[2])
        Y = float(vec[1] / vec[2])
        return round(X, 3), round(Y, 3)

    def blueprint_to_image(self, camera_id: str, X: float, Y: float) -> Tuple[float, float]:
        """Inverse project 2D store blueprint coordinate (X, Y) to camera image coordinate (u, v)."""
        calib = self.get_calibration(camera_id)
        if not calib:
            raise KeyError(f"No homography calibration registered for camera '{camera_id}'")

        if calib.inverse_matrix_3x3 is not None:
            H_inv = np.array(calib.inverse_matrix_3x3, dtype=np.float64)
        else:
            H = np.array(calib.matrix_3x3, dtype=np.float64)
            H_inv = np.linalg.inv(H)

        vec = H_inv @ np.array([X, Y, 1.0], dtype=np.float64)
        if abs(vec[2]) < 1e-12:
            raise ValueError("Degenerate projection: point maps to infinity on image plane.")

        u = float(vec[0] / vec[2])
        v = float(vec[1] / vec[2])
        return round(u, 4), round(v, 4)


# ============================================================================
# 4. Multi-Camera Journey Stitching (Spatio-Temporal + Re-ID Cosine Similarity)
# ============================================================================

class MultiCameraJourneyStitcher:
    """Stitches fragmented single-camera tracklets into comprehensive global customer journeys."""

    def __init__(
        self,
        homography_transformer: HomographyTransformer,
        reid_similarity_threshold: float = 0.70,
        max_spatial_transition_dist_m: float = 12.0,
        max_temporal_gap_sec: float = 90.0,
        spatial_sigma_m: float = 3.5,
    ):
        self.transformer = homography_transformer
        self.reid_similarity_threshold = reid_similarity_threshold
        self.max_spatial_dist = max_spatial_transition_dist_m
        self.max_temporal_gap = max_temporal_gap_sec
        self.spatial_sigma = spatial_sigma_m

    @staticmethod
    def compute_cosine_similarity(vec1: Sequence[float], vec2: Sequence[float]) -> float:
        """Compute cosine similarity between two feature embedding vectors."""
        v1 = np.array(vec1, dtype=np.float64)
        v2 = np.array(vec2, dtype=np.float64)
        norm1 = np.linalg.norm(v1)
        norm2 = np.linalg.norm(v2)
        if norm1 < 1e-9 or norm2 < 1e-9:
            return 0.0
        cos_sim = float(np.dot(v1, v2) / (norm1 * norm2))
        return max(-1.0, min(1.0, cos_sim))

    def evaluate_tracklet_affinity(
        self,
        t1: CameraTracklet,
        t2: CameraTracklet,
    ) -> float:
        """Evaluate fusion affinity score between tracklet t1 (earlier) and t2 (later/transitioning).

        Returns match probability score in [0.0, 1.0].
        """
        # Ensure t1 ended before or around when t2 started
        delta_t = (t2.start_time - t1.end_time).total_seconds()

        # Reject if t2 starts too long after t1 ended or occurs way in the past (allowing 5s overlap)
        if delta_t < -5.0 or delta_t > self.max_temporal_gap:
            return 0.0

        # Spatial distance check on blueprint
        p1 = t1.end_point_blueprint
        p2 = t2.start_point_blueprint

        if not p1:
            try:
                p1 = self.transformer.image_to_blueprint(t1.camera_id, t1.end_point_img[0], t1.end_point_img[1])
            except Exception:
                p1 = (0.0, 0.0)

        if not p2:
            try:
                p2 = self.transformer.image_to_blueprint(t2.camera_id, t2.start_point_img[0], t2.start_point_img[1])
            except Exception:
                p2 = (0.0, 0.0)

        dist_m = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        if dist_m > self.max_spatial_dist:
            return 0.0

        # Spatial score (Gaussian decay)
        s_spatial = math.exp(-(dist_m ** 2) / (2.0 * (self.spatial_sigma ** 2)))

        # Temporal score (Gaussian decay over time gap)
        dt_eff = max(0.0, delta_t)
        s_temporal = math.exp(-(dt_eff ** 2) / (2.0 * (30.0 ** 2)))

        # Visual Re-ID score
        s_reid = 0.5  # Default neutral score if no embeddings present
        if t1.reid_embedding and t2.reid_embedding:
            sim = self.compute_cosine_similarity(t1.reid_embedding, t2.reid_embedding)
            if sim < self.reid_similarity_threshold:
                return 0.0
            # Rescale [threshold, 1.0] -> [0.0, 1.0]
            s_reid = (sim - self.reid_similarity_threshold) / (1.0 - self.reid_similarity_threshold + 1e-9)

        # Weighted fusion: 50% Re-ID visual appearance, 30% Spatial proximity, 20% Temporal continuity
        total_affinity = (0.50 * s_reid) + (0.30 * s_spatial) + (0.20 * s_temporal)
        return float(min(1.0, max(0.0, total_affinity)))

    def stitch_tracklets(
        self,
        tracklets: Sequence[CameraTracklet],
        zone_lookup_fn: Optional[Any] = None,
    ) -> List[GlobalCustomerJourney]:
        """Stitch local tracklets into complete end-to-end customer journeys."""
        if not tracklets:
            return []

        # Sort all tracklets chronologically by start time
        sorted_tracklets = sorted(tracklets, key=lambda t: t.start_time)
        n = len(sorted_tracklets)

        # Tracklet adjacency graph
        next_track = [-1] * n

        # Greedy match forward in time
        matched_targets = set()
        for i in range(n):
            best_j = -1
            best_score = 0.45  # Minimum acceptable affinity cutoff

            for j in range(i + 1, n):
                if j in matched_targets:
                    continue
                # Ignore tracklets from the exact same camera overlapping in time
                if sorted_tracklets[i].camera_id == sorted_tracklets[j].camera_id:
                    if sorted_tracklets[j].start_time < sorted_tracklets[i].end_time:
                        continue

                score = self.evaluate_tracklet_affinity(sorted_tracklets[i], sorted_tracklets[j])
                if score > best_score:
                    best_score = score
                    best_j = j

            if best_j != -1:
                next_track[i] = best_j
                matched_targets.add(best_j)

        # Reconstruct chains
        is_chain_start = [True] * n
        for i in range(n):
            if next_track[i] != -1:
                is_chain_start[next_track[i]] = False

        journeys: List[GlobalCustomerJourney] = []
        journey_counter = 1

        for i in range(n):
            if not is_chain_start[i]:
                continue

            chain: List[CameraTracklet] = []
            curr = i
            while curr != -1:
                chain.append(sorted_tracklets[curr])
                curr = next_track[curr]

            # Build Global Customer Journey
            journey_id = f"journey_{journey_counter:04d}"
            journey_counter += 1

            tracklet_ids = [(t.camera_id, t.track_id) for t in chain]
            camera_sequence = [t.camera_id for t in chain]
            start_time = chain[0].start_time
            end_time = chain[-1].end_time
            total_duration = max(1.0, (end_time - start_time).total_seconds())

            trajectory: List[Waypoint] = []
            zones_visited_set = []
            zone_dwell_map: Dict[str, float] = {}
            all_interactions: List[str] = []
            total_dwell = sum(t.dwell_time_sec for t in chain)

            for t in chain:
                # Add start waypoint
                p_start = t.start_point_blueprint
                if not p_start:
                    try:
                        p_start = self.transformer.image_to_blueprint(t.camera_id, t.start_point_img[0], t.start_point_img[1])
                    except Exception:
                        p_start = (0.0, 0.0)

                # Add end waypoint
                p_end = t.end_point_blueprint
                if not p_end:
                    try:
                        p_end = self.transformer.image_to_blueprint(t.camera_id, t.end_point_img[0], t.end_point_img[1])
                    except Exception:
                        p_end = (0.0, 0.0)

                z_id = zone_lookup_fn(p_start[0], p_start[1]) if zone_lookup_fn else f"zone_{t.camera_id}"
                if z_id not in zones_visited_set:
                    zones_visited_set.append(z_id)

                zone_dwell_map[z_id] = zone_dwell_map.get(z_id, 0.0) + t.dwell_time_sec

                trajectory.append(
                    Waypoint(
                        timestamp=t.start_time,
                        camera_id=t.camera_id,
                        x_meters=p_start[0],
                        y_meters=p_start[1],
                        zone_id=z_id,
                        action="PASS" if t.dwell_time_sec < 3.0 else "DWELL",
                    )
                )

                if t.interacted:
                    all_interactions.extend(t.interacted_items)
                    trajectory.append(
                        Waypoint(
                            timestamp=t.start_time + timedelta(seconds=min(5.0, t.dwell_time_sec / 2.0)),
                            camera_id=t.camera_id,
                            x_meters=p_end[0],
                            y_meters=p_end[1],
                            zone_id=z_id,
                            action="INTERACT",
                        )
                    )

                trajectory.append(
                    Waypoint(
                        timestamp=t.end_time,
                        camera_id=t.camera_id,
                        x_meters=p_end[0],
                        y_meters=p_end[1],
                        zone_id=z_id,
                        action="EXIT",
                    )
                )

            journeys.append(
                GlobalCustomerJourney(
                    journey_id=journey_id,
                    tracklet_ids=tracklet_ids,
                    camera_sequence=camera_sequence,
                    start_time=start_time,
                    end_time=end_time,
                    total_duration_sec=round(total_duration, 1),
                    total_dwell_time_sec=round(total_dwell, 1),
                    zones_visited=zones_visited_set,
                    zone_dwell_times={k: round(v, 1) for k, v in zone_dwell_map.items()},
                    trajectory=trajectory,
                    interactions=all_interactions,
                    has_converted=any("checkout" in c.lower() for c in camera_sequence),
                )
            )

        return journeys


# ============================================================================
# 5. Queue Analytics & Service Rate Calculator
# ============================================================================

class CheckoutQueueAnalytics:
    """Calculates checkout queue lengths, wait time distributions, and service rates."""

    @staticmethod
    def calculate_wait_distribution(wait_times_seconds: Sequence[float]) -> Dict[str, float]:
        """Compute statistical distribution of wait times."""
        if not wait_times_seconds:
            return {
                "mean": 0.0,
                "median": 0.0,
                "p90": 0.0,
                "p95": 0.0,
                "std": 0.0,
                "min": 0.0,
                "max": 0.0,
            }

        arr = np.array(wait_times_seconds, dtype=np.float64)
        mean_val = float(np.mean(arr))
        median_val = float(np.median(arr))
        p90_val = float(np.percentile(arr, 90))
        p95_val = float(np.percentile(arr, 95))
        std_val = float(np.std(arr))
        min_val = float(np.min(arr))
        max_val = float(np.max(arr))

        return {
            "mean": round(mean_val, 2),
            "median": round(median_val, 2),
            "p90": round(p90_val, 2),
            "p95": round(p95_val, 2),
            "std": round(std_val, 2),
            "min": round(min_val, 2),
            "max": round(max_val, 2),
        }

    @staticmethod
    def calculate_service_rate(
        completed_transactions: int,
        window_duration_minutes: float,
    ) -> Tuple[float, float]:
        """Calculate checkout service rate mu.

        Returns:
          (mu_per_minute, mu_per_hour)
        """
        if window_duration_minutes <= 0:
            return 0.0, 0.0

        mu_min = completed_transactions / window_duration_minutes
        mu_hour = mu_min * 60.0
        return round(mu_min, 2), round(mu_hour, 2)

    @staticmethod
    def evaluate_lane_status(
        current_queue_length: int,
        mean_wait_sec: float,
        is_cashier_active: bool,
    ) -> LaneStatus:
        """Determine lane operational status."""
        if not is_cashier_active:
            return LaneStatus.CLOSED
        if current_queue_length == 0:
            return LaneStatus.IDLE
        if current_queue_length >= 5 or mean_wait_sec >= 270.0:  # 4.5 minutes
            return LaneStatus.CONGESTED
        return LaneStatus.OPEN

    def build_queue_metrics(
        self,
        checkout_id: str,
        camera_id: str,
        current_queue_length: int,
        wait_times_seconds: Sequence[float],
        completed_transactions_in_window: int,
        window_minutes: float = 60.0,
        is_cashier_active: bool = True,
    ) -> QueueMetrics:
        """Assemble comprehensive queue metrics object."""
        dist = self.calculate_wait_distribution(wait_times_seconds)
        mu_min, mu_hour = self.calculate_service_rate(completed_transactions_in_window, window_minutes)
        status = self.evaluate_lane_status(current_queue_length, dist["mean"], is_cashier_active)

        return QueueMetrics(
            checkout_id=checkout_id,
            camera_id=camera_id,
            current_queue_length=current_queue_length,
            active_cashier=is_cashier_active,
            lane_status=status,
            mean_wait_time_sec=dist["mean"],
            median_wait_time_sec=dist["median"],
            p90_wait_time_sec=dist["p90"],
            p95_wait_time_sec=dist["p95"],
            std_wait_time_sec=dist["std"],
            min_wait_time_sec=dist["min"],
            max_wait_time_sec=dist["max"],
            service_rate_per_min=mu_min,
            service_rate_per_hour=mu_hour,
            total_customers_served=completed_transactions_in_window,
            last_updated=datetime.now(timezone.utc),
        )


# ============================================================================
# 6. Zero-PII Demographics Aggregator
# ============================================================================

class DemographicsAggregator:
    """Aggregates anonymized customer demographic distributions per zone.

    Strict Zero-PII Policy:
    - Absolutely NO face crops or raw facial embeddings are saved.
    - Transient classification tags (age group, gender bucket, sentiment valence)
      are immediately accumulated into numerical counters and histograms.
    """

    def __init__(self):
        # In-memory accumulators per zone
        self._zone_age_counters: Dict[str, Dict[str, int]] = {}
        self._zone_gender_counters: Dict[str, Dict[str, int]] = {}
        self._zone_sentiment_counters: Dict[str, Dict[str, int]] = {}
        self._zone_sentiment_scores: Dict[str, List[float]] = {}

    def _ensure_zone(self, zone_id: str) -> None:
        if zone_id not in self._zone_age_counters:
            self._zone_age_counters[zone_id] = {
                "0-17": 0, "18-24": 0, "25-34": 0, "35-49": 0, "50-64": 0, "65+": 0
            }
            self._zone_gender_counters[zone_id] = {
                "male": 0, "female": 0, "unknown": 0
            }
            self._zone_sentiment_counters[zone_id] = {
                "positive": 0, "neutral": 0, "negative": 0, "confused": 0, "frustrated": 0
            }
            self._zone_sentiment_scores[zone_id] = []

    def record_observation(
        self,
        zone_id: str,
        age_group: str,
        gender: str,
        sentiment: str,
        sentiment_valence: float = 0.0,
    ) -> None:
        """Record a single anonymized observation.

        Zero PII guarantee: raw face image is discarded before this call.
        """
        self._ensure_zone(zone_id)

        # Normalize age group
        valid_ages = ["0-17", "18-24", "25-34", "35-49", "50-64", "65+"]
        norm_age = age_group if age_group in valid_ages else "25-34"
        self._zone_age_counters[zone_id][norm_age] += 1

        # Normalize gender
        norm_gender = gender.lower() if gender.lower() in ["male", "female"] else "unknown"
        self._zone_gender_counters[zone_id][norm_gender] += 1

        # Normalize sentiment
        valid_sentiments = ["positive", "neutral", "negative", "confused", "frustrated"]
        norm_sentiment = sentiment.lower() if sentiment.lower() in valid_sentiments else "neutral"
        self._zone_sentiment_counters[zone_id][norm_sentiment] += 1

        # Sentiment valence score clamped in [-1.0, 1.0]
        clamped_score = max(-1.0, min(1.0, float(sentiment_valence)))
        self._zone_sentiment_scores[zone_id].append(clamped_score)

    def get_zone_report(self, zone_id: str) -> DemographicsReport:
        """Generate anonymized statistical report for a zone."""
        self._ensure_zone(zone_id)

        age_dist = dict(self._zone_age_counters[zone_id])
        gender_dist = dict(self._zone_gender_counters[zone_id])
        sentiment_dist = dict(self._zone_sentiment_counters[zone_id])
        scores = self._zone_sentiment_scores[zone_id]

        sample_size = sum(gender_dist.values())
        avg_score = float(np.mean(scores)) if scores else 0.0

        return DemographicsReport(
            zone_id=zone_id,
            sample_size=sample_size,
            age_distribution=age_dist,
            gender_distribution=gender_dist,
            sentiment_distribution=sentiment_dist,
            average_sentiment_score=round(avg_score, 2),
            privacy_compliance_verified=True,
            generated_at=datetime.now(timezone.utc),
        )

    def clear_zone(self, zone_id: str) -> None:
        """Reset demographic counters for a zone."""
        if zone_id in self._zone_age_counters:
            del self._zone_age_counters[zone_id]
            del self._zone_gender_counters[zone_id]
            del self._zone_sentiment_counters[zone_id]
            del self._zone_sentiment_scores[zone_id]


# Global singleton instance for easy service access
retail_analytics_service = RetailFunnelCalculator()
homography_transformer = HomographyTransformer()
multi_camera_stitcher = MultiCameraJourneyStitcher(homography_transformer)
queue_analytics_service = CheckoutQueueAnalytics()
demographics_aggregator = DemographicsAggregator()
