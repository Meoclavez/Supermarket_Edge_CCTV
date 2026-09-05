"""Loss Prevention & Retail Theft Detection Engine for Supermarket Edge AI CCTV.

Implements:
1. `detect_shelf_sweeping`: Bulk item sweeping (>= 3 high-value picks in <= 5 seconds).
2. `detect_concealment`: Direct shelf-to-torso/pocket/bag kinematic trajectory without cart/basket deposit.
3. `detect_sweethearting`: Cashier scanning bypass (item pass without matching POS barcode event).
4. `detect_pushout_exit_bypass`: Unpaid cart exit bypass without valid checkout dwell.
5. Incident Management: Acknowledge, Dispatch Guard/Audio Deterrent, Resolve, Statistics, and Live Simulation.
"""

from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from sqlalchemy import select, func, desc, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.models.db_models import TheftIncidentModel
from app.models.schemas import (
    TheftType,
    TheftIncidentStatus,
    TheftIncident,
    TheftStatisticsResponse,
)

logger = logging.getLogger("TheftDetectionService")


# ============================================================================
# Geometry & Kinematic Helpers
# ============================================================================

def _normalize_roi(roi: Union[Tuple[float, float, float, float], Dict[str, float]]) -> Tuple[float, float, float, float]:
    """Normalize bounding box to (x_min, y_min, x_max, y_max)."""
    if isinstance(roi, dict):
        if "x_min" in roi:
            return float(roi["x_min"]), float(roi["y_min"]), float(roi["x_max"]), float(roi["y_max"])
        elif "x" in roi and "w" in roi:
            return float(roi["x"]), float(roi["y"]), float(roi["x"] + roi["w"]), float(roi["y"] + roi["h"])
    return float(roi[0]), float(roi[1]), float(roi[2]), float(roi[3])


def _point_in_box(x: float, y: float, box: Tuple[float, float, float, float]) -> bool:
    """Check if point (x, y) is inside bounding box (x_min, y_min, x_max, y_max)."""
    x_min, y_min, x_max, y_max = box
    return x_min <= x <= x_max and y_min <= y <= y_max


def _point_in_polygon(x: float, y: float, polygon: Sequence[Tuple[float, float]]) -> bool:
    """Raycasting algorithm to determine if point is inside polygon."""
    if len(polygon) < 3:
        return False
    inside = False
    n = len(polygon)
    p1x, p1y = polygon[0]
    for i in range(1, n + 1):
        p2x, p2y = polygon[i % n]
        if y > min(p1y, p2y):
            if y <= max(p1y, p2y):
                if x <= max(p1x, p2x):
                    if p1y != p2y:
                        xinters = (y - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
                    if p1x == p2x or x <= xinters:
                        inside = not inside
        p1x, p1y = p2x, p2y
    return inside


# ============================================================================
# Loss Prevention & Theft Detection Service
# ============================================================================

class TheftDetectionService:
    """Core Loss Prevention detection and incident lifecycle management."""

    # ------------------------------------------------------------------------
    # 1. Shelf Sweeping Detection
    # ------------------------------------------------------------------------
    @staticmethod
    def detect_shelf_sweeping(
        interactions: Sequence[Dict[str, Any]],
        window_sec: float = 5.0,
        min_picks: int = 3,
    ) -> Dict[str, Any]:
        """Detect rapid bulk removal of items (>= min_picks within <= window_sec).

        Args:
            interactions: List of pick events with timestamp (datetime or float) and details.
            window_sec: Time window threshold in seconds (default 5.0s).
            min_picks: Minimum number of items picked (default 3).
        """
        if not interactions or len(interactions) < min_picks:
            return {"detected": False, "count": 0, "window_interactions": []}

        # Parse timestamps into float seconds
        parsed_events = []
        for item in interactions:
            ts = item.get("timestamp")
            if isinstance(ts, datetime):
                t_sec = ts.timestamp()
            elif isinstance(ts, (int, float)):
                t_sec = float(ts)
            elif isinstance(ts, str):
                try:
                    t_sec = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except Exception:
                    t_sec = 0.0
            else:
                t_sec = 0.0
            parsed_events.append((t_sec, item))

        parsed_events.sort(key=lambda x: x[0])

        # Sliding window search
        max_picks = 0
        best_window: List[Dict[str, Any]] = []

        n = len(parsed_events)
        for i in range(n):
            current_window = [parsed_events[i][1]]
            t_start = parsed_events[i][0]

            for j in range(i + 1, n):
                if parsed_events[j][0] - t_start <= window_sec:
                    current_window.append(parsed_events[j][1])
                else:
                    break

            if len(current_window) > max_picks:
                max_picks = len(current_window)
                best_window = current_window

        if max_picks >= min_picks:
            duration = 0.0
            if len(best_window) > 1:
                t_first = parsed_events[0][0]
                t_last = parsed_events[-1][0]
                duration = round(t_last - t_first, 2)

            return {
                "detected": True,
                "theft_type": TheftType.SHELF_SWEEPING.value,
                "count": max_picks,
                "duration_sec": duration,
                "window_sec": window_sec,
                "window_interactions": best_window,
                "confidence": min(0.98, 0.70 + (max_picks * 0.08)),
            }

        return {"detected": False, "count": max_picks, "window_interactions": []}

    # ------------------------------------------------------------------------
    # 2. Concealment Kinematics Detection
    # ------------------------------------------------------------------------
    @staticmethod
    def detect_concealment(
        wrist_trajectory: Sequence[Any],
        shelf_roi: Union[Tuple[float, float, float, float], Dict[str, float]],
        body_bbox: Union[Tuple[float, float, float, float], Dict[str, float]],
        cart_bbox: Optional[Union[Tuple[float, float, float, float], Dict[str, float]]] = None,
    ) -> Dict[str, Any]:
        """Detect concealment motion (wrist moving from shelf directly to torso/pocket/jacket without cart placement)."""
        if not wrist_trajectory or len(wrist_trajectory) < 2:
            return {"detected": False, "confidence": 0.0, "reason": "Insufficient trajectory points"}

        shelf_box = _normalize_roi(shelf_roi)
        body_box = _normalize_roi(body_bbox)
        cart_box = _normalize_roi(cart_bbox) if cart_bbox else None

        touched_shelf = False
        touched_cart = False
        touched_body_after_shelf = False

        shelf_idx = -1
        body_idx = -1
        cart_idx = -1

        for idx, pt in enumerate(wrist_trajectory):
            if isinstance(pt, dict):
                x, y = float(pt.get("x", 0.0)), float(pt.get("y", 0.0))
            elif isinstance(pt, (tuple, list)):
                x, y = float(pt[0]), float(pt[1])
            else:
                continue

            if _point_in_box(x, y, shelf_box):
                touched_shelf = True
                shelf_idx = idx

            if cart_box and _point_in_box(x, y, cart_box):
                touched_cart = True
                cart_idx = idx

            if touched_shelf and _point_in_box(x, y, body_box):
                # Ensure it occurred after shelf touch
                if idx > shelf_idx:
                    touched_body_after_shelf = True
                    body_idx = idx

        # If item went to cart before body, not concealment
        if touched_cart and cart_idx > shelf_idx and (body_idx == -1 or cart_idx < body_idx):
            return {
                "detected": False,
                "confidence": 0.1,
                "reason": "Item placed in shopping cart/basket",
            }

        if touched_shelf and touched_body_after_shelf:
            return {
                "detected": True,
                "theft_type": TheftType.CONCEALMENT.value,
                "confidence": 0.92,
                "shelf_touch_index": shelf_idx,
                "body_touch_index": body_idx,
                "bypassed_cart": True if cart_box else None,
                "reason": "Direct hand kinematic trajectory from shelf ROI to torso/pocket area without cart placement",
            }

        return {
            "detected": False,
            "confidence": 0.0,
            "reason": "Kinematic pattern did not match concealment",
        }

    # ------------------------------------------------------------------------
    # 3. Sweethearting / Cashier Bypass Detection
    # ------------------------------------------------------------------------
    @staticmethod
    def detect_sweethearting(
        pos_transactions: Sequence[Dict[str, Any]],
        cashier_hand_passes: Sequence[Dict[str, Any]],
        tolerance_sec: float = 2.0,
    ) -> Dict[str, Any]:
        """Detect sweethearting (cashier passes item over scanner without barcode scan event)."""
        if not cashier_hand_passes:
            return {"detected": False, "unmatched_passes": [], "total_passes": 0, "scanned_count": len(pos_transactions)}

        # Parse scan timestamps
        scan_timestamps: List[float] = []
        for tx in pos_transactions:
            ts = tx.get("timestamp")
            if isinstance(ts, datetime):
                scan_timestamps.append(ts.timestamp())
            elif isinstance(ts, (int, float)):
                scan_timestamps.append(float(ts))
            elif isinstance(ts, str):
                try:
                    scan_timestamps.append(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
                except Exception:
                    pass

        unmatched: List[Dict[str, Any]] = []

        for pass_event in cashier_hand_passes:
            p_ts = pass_event.get("timestamp")
            if isinstance(p_ts, datetime):
                p_time = p_ts.timestamp()
            elif isinstance(p_ts, (int, float)):
                p_time = float(p_ts)
            elif isinstance(p_ts, str):
                try:
                    p_time = datetime.fromisoformat(p_ts.replace("Z", "+00:00")).timestamp()
                except Exception:
                    p_time = 0.0
            else:
                p_time = 0.0

            # Match against nearest POS scan
            has_match = any(abs(p_time - s_time) <= tolerance_sec for s_time in scan_timestamps)
            if not has_match:
                unmatched.append({
                    "pass_id": pass_event.get("id", f"pass_{len(unmatched)+1}"),
                    "timestamp": pass_event.get("timestamp"),
                    "item_description": pass_event.get("item_description", "Unscanned Item"),
                    "cashier_id": pass_event.get("cashier_id", "cashier_01"),
                    "register_id": pass_event.get("register_id", "register_01"),
                })

        detected = len(unmatched) > 0
        return {
            "detected": detected,
            "theft_type": TheftType.SWEETHEARTING.value,
            "unmatched_passes": unmatched,
            "unmatched_count": len(unmatched),
            "total_passes": len(cashier_hand_passes),
            "scanned_count": len(pos_transactions),
            "confidence": 0.88 if detected else 0.0,
            "reason": f"{len(unmatched)} visual item passes lacked corresponding POS barcode scans" if detected else "All passes matched valid scans",
        }

    # ------------------------------------------------------------------------
    # 4. Pushout / Exit Bypass Detection
    # ------------------------------------------------------------------------
    @staticmethod
    def detect_pushout_exit_bypass(
        track_trajectory: Sequence[Any],
        exit_zone_polygon: Union[Sequence[Tuple[float, float]], Tuple[float, float, float, float], Dict[str, float]],
        checkout_visit_duration: float = 0.0,
    ) -> Dict[str, Any]:
        """Detect pushout theft (cart or person bypassing checkout and heading directly through exit)."""
        if not track_trajectory:
            return {"detected": False, "reason": "No trajectory waypoints"}

        entered_exit = False
        exit_time = None

        is_polygon = isinstance(exit_zone_polygon, (list, tuple)) and len(exit_zone_polygon) > 2 and isinstance(exit_zone_polygon[0], (list, tuple))

        for pt in track_trajectory:
            if isinstance(pt, dict):
                x, y = float(pt.get("x", 0.0)), float(pt.get("y", 0.0))
                ts = pt.get("timestamp")
            elif isinstance(pt, (tuple, list)):
                x, y = float(pt[0]), float(pt[1])
                ts = pt[2] if len(pt) > 2 else None
            else:
                continue

            if is_polygon:
                if _point_in_polygon(x, y, exit_zone_polygon):  # type: ignore
                    entered_exit = True
                    exit_time = ts
                    break
            else:
                box = _normalize_roi(exit_zone_polygon)  # type: ignore
                if _point_in_box(x, y, box):
                    entered_exit = True
                    exit_time = ts
                    break

        # If entered exit and spent < 15s in checkout (or 0s)
        if entered_exit and checkout_visit_duration < 15.0:
            return {
                "detected": True,
                "theft_type": TheftType.PUSHOUT_EXIT_BYPASS.value,
                "confidence": 0.94 if checkout_visit_duration == 0.0 else 0.86,
                "checkout_duration_sec": checkout_visit_duration,
                "exit_timestamp": str(exit_time) if exit_time else None,
                "reason": f"Cart/shopper entered exit boundary with only {checkout_visit_duration:.1f}s checkout dwell (bypassed register)",
            }

        return {
            "detected": False,
            "checkout_duration_sec": checkout_visit_duration,
            "reason": "Normal exit with adequate checkout dwell or outside exit zone",
        }

    # ------------------------------------------------------------------------
    # 5. Incident Lifecycle & DB Methods
    # ------------------------------------------------------------------------
    async def get_incidents(
        self,
        db: AsyncSession,
        status: Optional[str] = None,
        severity: Optional[str] = None,
        department: Optional[str] = None,
        limit: int = 50,
    ) -> List[TheftIncidentModel]:
        """Query theft incidents from database with filters."""
        stmt = select(TheftIncidentModel).order_by(desc(TheftIncidentModel.timestamp)).limit(limit)

        if status:
            stmt = stmt.where(TheftIncidentModel.status == status.upper())
        if severity:
            stmt = stmt.where(TheftIncidentModel.severity == severity.upper())
        if department:
            stmt = stmt.where(TheftIncidentModel.department == department)

        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_statistics(self, db: AsyncSession) -> TheftStatisticsResponse:
        """Compute live theft & loss prevention dashboard statistics."""
        now = datetime.utcnow()
        start_of_today = datetime(now.year, now.month, now.day)

        # Active incidents
        active_stmt = select(func.count()).select_from(TheftIncidentModel).where(TheftIncidentModel.status.in_(["ACTIVE", "ACKNOWLEDGED", "DISPATCHED"]))
        active_res = await db.execute(active_stmt)
        active_count = active_res.scalar_one_or_none() or 0

        # Today's incidents
        today_stmt = select(func.count()).select_from(TheftIncidentModel).where(TheftIncidentModel.timestamp >= start_of_today)
        today_res = await db.execute(today_stmt)
        today_count = today_res.scalar_one_or_none() or 0

        # Prevented loss sum
        prevented_stmt = select(func.sum(TheftIncidentModel.estimated_loss_value)).where(TheftIncidentModel.status.in_(["DISPATCHED", "RESOLVED"]))
        prevented_res = await db.execute(prevented_stmt)
        prevented_val = prevented_res.scalar_one_or_none() or 0.0

        # By Department
        dept_stmt = select(TheftIncidentModel.department, func.count(TheftIncidentModel.id)).group_by(TheftIncidentModel.department)
        dept_res = await db.execute(dept_stmt)
        by_dept = {row[0]: row[1] for row in dept_res.all()}

        # By Theft Type
        type_stmt = select(TheftIncidentModel.theft_type, func.count(TheftIncidentModel.id)).group_by(TheftIncidentModel.theft_type)
        type_res = await db.execute(type_stmt)
        by_type = {row[0]: row[1] for row in type_res.all()}

        return TheftStatisticsResponse(
            active_incidents_count=active_count,
            today_incidents_count=today_count,
            prevented_loss_estimate=round(float(prevented_val), 2),
            by_department=by_dept,
            by_theft_type=by_type,
            generated_at=now,
        )

    async def acknowledge_incident(
        self,
        incident_id: str,
        guard_id: str,
        db: AsyncSession,
    ) -> Optional[TheftIncidentModel]:
        """Acknowledge theft incident by security guard."""
        stmt = select(TheftIncidentModel).where(TheftIncidentModel.id == incident_id)
        res = await db.execute(stmt)
        incident = res.scalar_one_or_none()
        if not incident:
            return None

        incident.status = TheftIncidentStatus.ACKNOWLEDGED.value
        incident.guard_id = guard_id
        await db.commit()
        await db.refresh(incident)
        logger.info(f"Theft incident {incident_id} ACKNOWLEDGED by {guard_id}")
        return incident

    async def dispatch_security(
        self,
        incident_id: str,
        guard_unit: str,
        audio_deterrent: bool = True,
        announcement_type: str = "CUSTOMER_ASSISTANCE_GREETING",
        db: Optional[AsyncSession] = None,
    ) -> Optional[TheftIncidentModel]:
        """Dispatch security floor guard and/or trigger automated smart audio greeting deterrence."""
        session = db
        should_close = False
        if session is None:
            session = async_session_factory()
            should_close = True

        try:
            stmt = select(TheftIncidentModel).where(TheftIncidentModel.id == incident_id)
            res = await session.execute(stmt)
            incident = res.scalar_one_or_none()
            if not incident:
                return None

            incident.status = TheftIncidentStatus.DISPATCHED.value
            incident.dispatch_details = {
                "guard_unit": guard_unit,
                "audio_deterrent_triggered": audio_deterrent,
                "announcement_type": announcement_type,
                "dispatched_at": datetime.utcnow().isoformat(),
            }
            await session.commit()
            await session.refresh(incident)
            logger.info(f"Security dispatched to incident {incident_id} (Unit: {guard_unit}, Audio: {audio_deterrent})")
            return incident
        finally:
            if should_close and session:
                await session.close()

    async def resolve_incident(
        self,
        incident_id: str,
        resolution: str,
        notes: Optional[str] = None,
        db: Optional[AsyncSession] = None,
    ) -> Optional[TheftIncidentModel]:
        """Resolve an incident (or mark as FALSE_ALARM)."""
        session = db
        should_close = False
        if session is None:
            session = async_session_factory()
            should_close = True

        try:
            stmt = select(TheftIncidentModel).where(TheftIncidentModel.id == incident_id)
            res = await session.execute(stmt)
            incident = res.scalar_one_or_none()
            if not incident:
                return None

            status_val = TheftIncidentStatus.FALSE_ALARM.value if resolution.upper() == "FALSE_ALARM" else TheftIncidentStatus.RESOLVED.value
            incident.status = status_val
            incident.resolution = resolution
            incident.resolved_at = datetime.utcnow()
            if notes:
                incident.notes = notes

            await session.commit()
            await session.refresh(incident)
            logger.info(f"Theft incident {incident_id} marked as {status_val} ({resolution})")
            return incident
        finally:
            if should_close and session:
                await session.close()

    async def simulate_theft_incident(
        self,
        theft_type: str,
        camera_id: str = "cam_liquor_zone",
        department: str = "Liquor & Spirits",
        estimated_loss_value: Optional[float] = None,
        db: Optional[AsyncSession] = None,
    ) -> TheftIncidentModel:
        """Simulate a high-fidelity theft incident and persist to database."""
        session = db
        should_close = False
        if session is None:
            session = async_session_factory()
            should_close = True

        try:
            norm_type = theft_type.upper()
            inc_id = f"theft_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"

            # Default attributes based on theft type
            if norm_type == TheftType.SHELF_SWEEPING.value:
                loss_val = estimated_loss_value or 280.0
                items = [
                    {"sku": "LQR_WHISKY_01", "name": "Single Malt Scotch Whisky 700ml", "price": 85.0, "qty": 2},
                    {"sku": "LQR_GIN_02", "name": "Artisan Botanical Gin 700ml", "price": 55.0, "qty": 2},
                ]
                severity = "CRITICAL"
                notes = "Rapid bulk sweeping: 4 high-value liquor bottles removed within 3.2 seconds."
            elif norm_type == TheftType.CONCEALMENT.value:
                loss_val = estimated_loss_value or 95.0
                items = [
                    {"sku": "HLT_CREAM_09", "name": "Premium Anti-Aging Serum 50ml", "price": 95.0, "qty": 1},
                ]
                severity = "HIGH"
                notes = "Concealment kinematics: Product moved directly from shelf ROI into jacket inner pocket without cart placement."
            elif norm_type == TheftType.SWEETHEARTING.value:
                loss_val = estimated_loss_value or 65.0
                items = [
                    {"sku": "MEA_STEAK_04", "name": "Wagyu Ribeye Steak 500g", "price": 65.0, "qty": 1},
                ]
                severity = "HIGH"
                notes = "Cashier scan bypass: Cashier passed premium meat item around scanner without barcode scan event."
            else:  # PUSHOUT_EXIT_BYPASS
                loss_val = estimated_loss_value or 450.0
                items = [
                    {"sku": "CART_BULK_01", "name": "Full Shopping Cart (Baby formula & detergent)", "price": 450.0, "qty": 1},
                ]
                severity = "CRITICAL"
                notes = "Pushout exit bypass: Full cart passed directly through exit threshold with 0.0s checkout dwell."

            incident = TheftIncidentModel(
                id=inc_id,
                theft_type=norm_type,
                severity=severity,
                status=TheftIncidentStatus.ACTIVE.value,
                department=department,
                camera_id=camera_id,
                zone_id=f"zone_{camera_id.replace('cam_', '')}",
                timestamp=datetime.utcnow(),
                person_track_id=f"track_{uuid.uuid4().hex[:4]}",
                confidence=0.91,
                estimated_loss_value=loss_val,
                items_involved=items,
                evidence_snapshot_url=f"/api/v1/cameras/{camera_id}/snapshot",
                evidence_clip_url=f"/api/v1/dvr/{camera_id}/clip?t={int(datetime.utcnow().timestamp())}",
                bounding_box={"x_min": 0.35, "y_min": 0.20, "x_max": 0.65, "y_max": 0.85},
                wrist_trajectory=[{"x": 0.45, "y": 0.30}, {"x": 0.50, "y": 0.60}],
                notes=notes,
            )

            session.add(incident)
            await session.commit()
            await session.refresh(incident)
            logger.info(f"Simulated live theft incident: {inc_id} ({norm_type}, ${loss_val:.2f})")
            return incident
        finally:
            if should_close and session:
                await session.close()


# Global singleton instance
theft_detection_service = TheftDetectionService()
