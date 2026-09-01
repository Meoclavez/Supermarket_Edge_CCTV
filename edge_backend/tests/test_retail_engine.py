"""Comprehensive Unit Tests for Retail AI Vision, Math Engine, Decision Intelligence, and 25-Camera Seed Generator."""

import math
from datetime import datetime, timedelta
import numpy as np
import pytest

from app.services.retail_analytics_service import (
    CameraTracklet,
    CheckoutQueueAnalytics,
    DemographicsAggregator,
    FunnelMetrics,
    HomographyCalibration,
    HomographyTransformer,
    LaneStatus,
    MultiCameraJourneyStitcher,
    QueueMetrics,
    RetailFunnelCalculator,
    retail_analytics_service,
    homography_transformer,
    multi_camera_stitcher,
    queue_analytics_service,
    demographics_aggregator,
)
from app.services.retail_decision_engine import (
    RecommendationSeverity,
    RetailAnomalyType,
    RetailDecisionEngine,
    RetailRecommendation,
    ShelfInteractionSummary,
    retail_decision_engine,
)
from app.services.retail_seed_data import (
    SUPERMARKET_ZONES,
    SupermarketSeedDataManager,
    generate_25_camera_definitions,
    generate_synthetic_customer_journeys_and_tracklets,
    generate_precomputed_analytics,
    retail_seed_manager,
)


# ============================================================================
# 1. Retail Funnel Equations Tests
# ============================================================================

class TestRetailFunnelCalculator:
    def test_standard_funnel_calculations(self):
        # 1000 passed, 400 dwelled, 200 interacted, 50 bought
        funnel = RetailFunnelCalculator.calculate_rates(
            pass_count=1000,
            dwell_count=400,
            interact_count=200,
            sales_count=50,
            zone_id="zone_produce",
        )

        assert funnel.zone_id == "zone_produce"
        assert funnel.pass_count == 1000
        assert funnel.dwell_count == 400
        assert funnel.interact_count == 200
        assert funnel.sales_count == 50

        # Attraction Rate: alpha = (400 / 1000) * 100% = 40.0%
        assert math.isclose(funnel.attraction_rate, 40.0, rel_tol=1e-2)
        # Engagement Rate: beta = (200 / 400) * 100% = 50.0%
        assert math.isclose(funnel.engagement_rate, 50.0, rel_tol=1e-2)
        # Conversion Rate: gamma = (50 / 200) * 100% = 25.0%
        assert math.isclose(funnel.conversion_rate, 25.0, rel_tol=1e-2)
        # Friction Index: phi = ((200 - 50) / 200) * 100% = 75.0%
        assert math.isclose(funnel.friction_index, 75.0, rel_tol=1e-2)

    def test_zero_division_and_edge_cases(self):
        # Case A: 0 pass, 0 dwell, 0 interact, 0 sales
        zero_funnel = RetailFunnelCalculator.calculate_rates(0, 0, 0, 0, "zone_empty")
        assert zero_funnel.attraction_rate == 0.0
        assert zero_funnel.engagement_rate == 0.0
        assert zero_funnel.conversion_rate == 0.0
        assert zero_funnel.friction_index == 0.0

        # Case B: 500 pass, 0 dwell
        no_dwell = RetailFunnelCalculator.calculate_rates(500, 0, 0, 0)
        assert no_dwell.attraction_rate == 0.0
        assert no_dwell.engagement_rate == 0.0
        assert no_dwell.conversion_rate == 0.0
        assert no_dwell.friction_index == 0.0

        # Case C: 100 pass, 100 dwell, 100 interact, 100 sales (100% conversion, 0% friction)
        perfect_funnel = RetailFunnelCalculator.calculate_rates(100, 100, 100, 100)
        assert perfect_funnel.attraction_rate == 100.0
        assert perfect_funnel.engagement_rate == 100.0
        assert perfect_funnel.conversion_rate == 100.0
        assert perfect_funnel.friction_index == 0.0

        # Case D: 100 interact, 0 sales (100% friction, 0% conversion)
        full_friction = RetailFunnelCalculator.calculate_rates(200, 150, 100, 0)
        assert full_friction.conversion_rate == 0.0
        assert full_friction.friction_index == 100.0

    def test_multi_zone_aggregation(self):
        z1 = RetailFunnelCalculator.calculate_rates(200, 100, 50, 25, "zone_1")
        z2 = RetailFunnelCalculator.calculate_rates(400, 100, 50, 25, "zone_2")

        agg = RetailFunnelCalculator.aggregate_funnels([z1, z2], aggregate_id="storewide")
        assert agg.zone_id == "storewide"
        assert agg.pass_count == 600
        assert agg.dwell_count == 200
        assert agg.interact_count == 100
        assert agg.sales_count == 50
        # Attraction: 200 / 600 = 33.33%
        assert math.isclose(agg.attraction_rate, 33.33, rel_tol=1e-2)
        # Engagement: 100 / 200 = 50.0%
        assert math.isclose(agg.engagement_rate, 50.0, rel_tol=1e-2)
        # Conversion: 50 / 100 = 50.0%
        assert math.isclose(agg.conversion_rate, 50.0, rel_tol=1e-2)


# ============================================================================
# 2. Multi-Camera Homography Mapping Tests
# ============================================================================

class TestHomographyMapping:
    def test_dlt_homography_estimation_and_projection(self):
        # 4 ground truth correspondences: normalized image (u, v) -> blueprint (X, Y) in meters
        src_img_pts = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
        dst_bp_pts = [(10.0, 10.0), (20.0, 10.0), (20.0, 20.0), (10.0, 20.0)]

        H, rmse = HomographyTransformer.estimate_homography_dlt(src_img_pts, dst_bp_pts)
        assert H.shape == (3, 3)
        assert rmse < 1e-4

        transformer = HomographyTransformer()
        calib = HomographyCalibration(
            camera_id="cam_test_01",
            matrix_3x3=H.tolist(),
            reference_points_image=src_img_pts,
            reference_points_blueprint=dst_bp_pts,
            reprojection_rmse=rmse,
        )
        transformer.register_calibration(calib)

        # Test forward projection (u, v) -> (X, Y)
        X_center, Y_center = transformer.image_to_blueprint("cam_test_01", 0.5, 0.5)
        assert math.isclose(X_center, 15.0, abs_tol=1e-2)
        assert math.isclose(Y_center, 15.0, abs_tol=1e-2)

        # Test inverse projection (X, Y) -> (u, v)
        u_back, v_back = transformer.blueprint_to_image("cam_test_01", 15.0, 15.0)
        assert math.isclose(u_back, 0.5, abs_tol=1e-2)
        assert math.isclose(v_back, 0.5, abs_tol=1e-2)

    def test_invalid_homography_handling(self):
        transformer = HomographyTransformer()
        # Non-existent camera
        with pytest.raises(KeyError):
            transformer.image_to_blueprint("cam_unknown", 0.5, 0.5)

        # Singular matrix error
        singular_mat = [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]
        with pytest.raises(ValueError):
            transformer.register_calibration(
                HomographyCalibration(camera_id="cam_singular", matrix_3x3=singular_mat)
            )


# ============================================================================
# 3. Multi-Camera Journey Stitching Tests
# ============================================================================

class TestMultiCameraJourneyStitching:
    def test_cosine_similarity(self):
        vec_a = [1.0, 0.0, 0.0, 0.0]
        vec_b = [1.0, 0.0, 0.0, 0.0]
        vec_c = [0.0, 1.0, 0.0, 0.0]

        sim_identical = MultiCameraJourneyStitcher.compute_cosine_similarity(vec_a, vec_b)
        assert math.isclose(sim_identical, 1.0, abs_tol=1e-4)

        sim_orthogonal = MultiCameraJourneyStitcher.compute_cosine_similarity(vec_a, vec_c)
        assert math.isclose(sim_orthogonal, 0.0, abs_tol=1e-4)

    def test_tracklet_affinity_and_stitching(self):
        transformer = HomographyTransformer()

        # Register simple scaling calibrations for 2 adjacent cameras
        H1 = [[10.0, 0.0, 0.0], [0.0, 10.0, 0.0], [0.0, 0.0, 1.0]]  # Cam 1: (0..10, 0..10)
        H2 = [[10.0, 0.0, 10.0], [0.0, 10.0, 0.0], [0.0, 0.0, 1.0]] # Cam 2: (10..20, 0..10)

        transformer.register_calibration(HomographyCalibration(camera_id="cam_01", matrix_3x3=H1))
        transformer.register_calibration(HomographyCalibration(camera_id="cam_02", matrix_3x3=H2))

        stitcher = MultiCameraJourneyStitcher(transformer, reid_similarity_threshold=0.70)

        t_base = datetime(2026, 9, 1, 10, 0, 0)
        reid_person_a = [1.0, 0.0, 0.0, 0.0]
        reid_person_b = [0.0, 1.0, 0.0, 0.0]

        # Tracklet 1: Person A exiting Cam 1 at (9.0, 5.0) at 10:00:30
        tracklet_1 = CameraTracklet(
            track_id=1,
            camera_id="cam_01",
            start_time=t_base,
            end_time=t_base + timedelta(seconds=30),
            start_point_img=(0.2, 0.5),
            end_point_img=(0.9, 0.5),  # bp: (9.0, 5.0)
            reid_embedding=reid_person_a,
            dwell_time_sec=30.0,
        )

        # Tracklet 2: Person A entering Cam 2 at (10.5, 5.0) at 10:00:35 (5s gap, 1.5m dist)
        tracklet_2 = CameraTracklet(
            track_id=2,
            camera_id="cam_02",
            start_time=t_base + timedelta(seconds=35),
            end_time=t_base + timedelta(seconds=70),
            start_point_img=(0.05, 0.5), # bp: (10.5, 5.0)
            end_point_img=(0.8, 0.5),
            reid_embedding=reid_person_a,
            dwell_time_sec=35.0,
        )

        # Tracklet 3: Different person (Person B) in Cam 2
        tracklet_3 = CameraTracklet(
            track_id=3,
            camera_id="cam_02",
            start_time=t_base + timedelta(seconds=35),
            end_time=t_base + timedelta(seconds=70),
            start_point_img=(0.05, 0.5),
            end_point_img=(0.8, 0.5),
            reid_embedding=reid_person_b,
            dwell_time_sec=35.0,
        )

        # Evaluate affinity
        affinity_match = stitcher.evaluate_tracklet_affinity(tracklet_1, tracklet_2)
        affinity_non_match = stitcher.evaluate_tracklet_affinity(tracklet_1, tracklet_3)

        assert affinity_match > 0.65
        assert affinity_non_match == 0.0  # Rejected by Re-ID threshold

        # Stitch tracklets
        journeys = stitcher.stitch_tracklets([tracklet_1, tracklet_2, tracklet_3])
        # Should produce 2 distinct journeys (Person A stitched into 1 journey, Person B as another)
        assert len(journeys) == 2

        person_a_journey = next(j for j in journeys if ("cam_01", 1) in j.tracklet_ids)
        assert ("cam_02", 2) in person_a_journey.tracklet_ids
        assert person_a_journey.camera_sequence == ["cam_01", "cam_02"]
        assert person_a_journey.total_duration_sec >= 70.0


# ============================================================================
# 4. Checkout Queue Analytics Tests
# ============================================================================

class TestCheckoutQueueAnalytics:
    def test_wait_time_distribution_statistics(self):
        samples = [60.0, 120.0, 180.0, 240.0, 300.0]  # mean=180, median=180, min=60, max=300
        dist = CheckoutQueueAnalytics.calculate_wait_distribution(samples)

        assert math.isclose(dist["mean"], 180.0, abs_tol=1e-2)
        assert math.isclose(dist["median"], 180.0, abs_tol=1e-2)
        assert math.isclose(dist["min"], 60.0, abs_tol=1e-2)
        assert math.isclose(dist["max"], 300.0, abs_tol=1e-2)
        assert dist["p90"] > 240.0

    def test_service_rate_and_lane_status(self):
        analytics = CheckoutQueueAnalytics()

        # 60 transactions in 30 minutes -> mu = 2.0 cust/min = 120.0 cust/hr
        mu_min, mu_hr = analytics.calculate_service_rate(60, 30.0)
        assert math.isclose(mu_min, 2.0, abs_tol=1e-2)
        assert math.isclose(mu_hr, 120.0, abs_tol=1e-2)

        # Status: Closed
        assert analytics.evaluate_lane_status(0, 0.0, is_cashier_active=False) == LaneStatus.CLOSED
        # Status: Idle
        assert analytics.evaluate_lane_status(0, 0.0, is_cashier_active=True) == LaneStatus.IDLE
        # Status: Open (normal)
        assert analytics.evaluate_lane_status(3, 120.0, is_cashier_active=True) == LaneStatus.OPEN
        # Status: Congested (length >= 5 or wait >= 270s)
        assert analytics.evaluate_lane_status(6, 120.0, is_cashier_active=True) == LaneStatus.CONGESTED
        assert analytics.evaluate_lane_status(2, 300.0, is_cashier_active=True) == LaneStatus.CONGESTED


# ============================================================================
# 5. Zero-PII Demographics Aggregator Tests
# ============================================================================

class TestDemographicsAggregator:
    def test_zero_pii_aggregation_and_reporting(self):
        agg = DemographicsAggregator()
        zone = "zone_produce"

        # Record multiple anonymized observations
        agg.record_observation(zone, "25-34", "female", "positive", 0.8)
        agg.record_observation(zone, "25-34", "male", "positive", 0.6)
        agg.record_observation(zone, "50-64", "female", "neutral", 0.0)
        agg.record_observation(zone, "18-24", "male", "frustrated", -0.7)

        report = agg.get_zone_report(zone)
        assert report.zone_id == zone
        assert report.sample_size == 4
        assert report.privacy_compliance_verified is True
        assert report.age_distribution["25-34"] == 2
        assert report.age_distribution["50-64"] == 1
        assert report.gender_distribution["female"] == 2
        assert report.gender_distribution["male"] == 2
        assert report.sentiment_distribution["positive"] == 2
        assert report.sentiment_distribution["frustrated"] == 1

        # Average sentiment: (0.8 + 0.6 + 0.0 - 0.7) / 4 = 0.7 / 4 = 0.175
        assert math.isclose(report.average_sentiment_score, 0.18, abs_tol=1e-2)

        # Clear zone
        agg.clear_zone(zone)
        cleared_report = agg.get_zone_report(zone)
        assert cleared_report.sample_size == 0


# ============================================================================
# 6. Retail Decision & Reasoning Engine Tests
# ============================================================================

class TestRetailDecisionEngine:
    def test_detect_high_interest_low_conversion_anomaly(self):
        engine = RetailDecisionEngine(friction_threshold_pct=75.0, low_conversion_threshold_pct=10.0)

        # Case A: Anomaly present - 100 dwelled, 40 interacted, only 2 converted (Friction: 95%, Conversion: 5%)
        problem_funnel = FunnelMetrics(
            zone_id="zone_aisle_3",
            pass_count=200,
            dwell_count=100,
            interact_count=40,
            sales_count=2,
            attraction_rate=50.0,
            engagement_rate=40.0,
            conversion_rate=5.0,
            friction_index=95.0,
        )

        rec = engine.detect_high_interest_low_conversion(problem_funnel, "Aisle 3 - Canned Goods", "Canned Foods")
        assert rec is not None
        assert rec.anomaly_type == RetailAnomalyType.HIGH_INTEREST_LOW_CONVERSION
        assert rec.severity == RecommendationSeverity.CRITICAL
        assert len(rec.operational_actions) >= 3
        assert "Aisle 3 - Canned Goods" in rec.root_cause_hypothesis
        assert rec.metric_evidence["friction_index"] == 95.0

        # Case B: Normal zone - 40 interacted, 20 converted (Friction: 50%, Conversion: 50%)
        normal_funnel = FunnelMetrics(
            zone_id="zone_bakery",
            pass_count=200,
            dwell_count=100,
            interact_count=40,
            sales_count=20,
            attraction_rate=50.0,
            engagement_rate=40.0,
            conversion_rate=50.0,
            friction_index=50.0,
        )
        assert engine.detect_high_interest_low_conversion(normal_funnel) is None

    def test_detect_chronic_dead_zone_anomaly(self):
        engine = RetailDecisionEngine(dead_zone_traffic_ratio_threshold=0.25)

        all_funnels = [
            FunnelMetrics(zone_id="z1", pass_count=300),
            FunnelMetrics(zone_id="z2", pass_count=280),
            FunnelMetrics(zone_id="z3", pass_count=320),
            FunnelMetrics(zone_id="dead_z", pass_count=40),  # 40 / 235 = 17% of avg (< 25%)
        ]

        dead_rec = engine.detect_chronic_dead_zone(all_funnels[3], all_funnels, "Aisle 8 - Pet Care")
        assert dead_rec is not None
        assert dead_rec.anomaly_type == RetailAnomalyType.CHRONIC_DEAD_ZONE
        assert dead_rec.severity in [RecommendationSeverity.HIGH, RecommendationSeverity.MEDIUM]
        assert "Aisle 8 - Pet Care" in dead_rec.root_cause_hypothesis

        # Normal zone check
        assert engine.detect_chronic_dead_zone(all_funnels[0], all_funnels) is None

    def test_detect_checkout_bottleneck_anomaly(self):
        engine = RetailDecisionEngine(queue_bottleneck_wait_sec=270.0, queue_bottleneck_length=5)

        bottleneck_metric = QueueMetrics(
            checkout_id="checkout_03_04",
            camera_id="cam_22",
            current_queue_length=6,
            active_cashier=True,
            lane_status=LaneStatus.CONGESTED,
            mean_wait_time_sec=320.0,
            p90_wait_time_sec=410.0,
            service_rate_per_min=1.2,
        )

        rec = engine.detect_checkout_bottleneck(bottleneck_metric, "Checkout 3 & 4")
        assert rec is not None
        assert rec.anomaly_type == RetailAnomalyType.CHECKOUT_BOTTLENECK
        assert rec.severity in [RecommendationSeverity.HIGH, RecommendationSeverity.CRITICAL]
        assert any("Immediately open" in action for action in rec.operational_actions)

    def test_detect_shelf_stockout_anomaly(self):
        engine = RetailDecisionEngine(stockout_putback_ratio_threshold=0.70)

        # 10 reaches, 9 instant put-backs (90%), 0 sales -> Phantom stockout
        stockout_summary = ShelfInteractionSummary(
            zone_id="zone_produce",
            shelf_id="shelf_avocados",
            product_category="Organic Hass Avocado",
            reach_count=10,
            instant_put_back_count=9,
            total_sales=0,
            avg_touch_duration_sec=1.5,
        )

        rec = engine.detect_shelf_stockout(stockout_summary, "Produce Island")
        assert rec is not None
        assert rec.anomaly_type == RetailAnomalyType.SHELF_STOCKOUT
        assert "Organic Hass Avocado" in rec.root_cause_hypothesis
        assert any("replenishment" in action.lower() for action in rec.operational_actions)

    def test_synthesize_storewide_recommendations(self):
        engine = RetailDecisionEngine()

        zone_funnels = {
            "zone_aisle_3": FunnelMetrics(zone_id="zone_aisle_3", pass_count=100, dwell_count=50, interact_count=30, sales_count=1, friction_index=96.7, conversion_rate=3.3),
            "zone_dairy": FunnelMetrics(zone_id="zone_dairy", pass_count=300, dwell_count=200, interact_count=100, sales_count=60, friction_index=40.0, conversion_rate=60.0),
        }

        queue_list = [
            QueueMetrics(checkout_id="checkout_1", camera_id="cam_21", current_queue_length=7, mean_wait_time_sec=310.0),
        ]

        recs = engine.synthesize_store_recommendations(
            zone_funnels=zone_funnels,
            queue_metrics=queue_list,
        )

        assert len(recs) >= 2
        # Priority sort: CRITICAL first
        assert recs[0].severity in [RecommendationSeverity.CRITICAL, RecommendationSeverity.HIGH]


# ============================================================================
# 7. Supermarket Seed Data & 25-Camera Generator Tests
# ============================================================================

class TestSupermarketSeedDataGenerator:
    def test_camera_definitions_and_calibrations(self):
        cameras = generate_25_camera_definitions()
        assert len(cameras) == 25

        for cam in cameras:
            assert cam["id"].startswith("cam_")
            assert cam["channel_number"] >= 1 and cam["channel_number"] <= 25
            assert "Pearcedale" in cam["rtsp_url"]
            assert len(cam["homography_matrix"]) == 3
            assert len(cam["homography_matrix"][0]) == 3
            assert cam["reprojection_rmse"] < 0.1

    def test_synthetic_simulation_generation(self):
        tracklets, journeys, pos_txs = generate_synthetic_customer_journeys_and_tracklets(total_customers=50)

        assert len(journeys) == 50
        assert len(tracklets) > 100
        assert len(pos_txs) > 10

        # Verify journey properties
        for j in journeys:
            assert j.journey_id.startswith("journey_sim_")
            assert len(j.camera_sequence) > 0
            assert len(j.trajectory) > 0
            assert j.total_duration_sec > 0.0

    def test_master_seed_data_manager_dataset_export(self, tmp_path):
        target_json = tmp_path / "retail_dataset_test.json"
        mgr = SupermarketSeedDataManager(storage_path=target_json)

        dataset = mgr.generate_full_dataset(total_customers=30)
        assert dataset["metadata"]["total_cameras"] == 25
        assert dataset["metadata"]["total_customer_journeys"] == 30
        assert "analytics" in dataset
        assert "zone_funnels" in dataset["analytics"]
        assert "recommendations" in dataset["analytics"]

        exported_path = mgr.export_dataset_to_file(target_json)
        assert exported_path.exists()
        assert exported_path.stat().st_size > 5000
