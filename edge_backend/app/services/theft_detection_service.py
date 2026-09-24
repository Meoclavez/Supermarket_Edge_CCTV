"""Loss-prevention rules and incident lifecycle.

The rules in this module are pure functions over *observed* evidence: wrist
samples, reach timestamps, dwell and head-turn counts, and the floor zones a
track passed through. They are fed live by ``app.services.pose_analytics``,
which turns each confirmed pose track into those evidence records.

Nothing here is a finding of guilt. Every positive result is *suspicious
behaviour for staff review*, returned together with the evidence bullets that
triggered it so a person can check the footage and decide.

Confidence is never a constant. Each rule derives it from what was measured:

* keypoint visibility of the joints the rule depends on (a partly occluded
  wrist yields a lower score than a clearly seen one),
* how long the behaviour lasted, and
* how many independent signals agreed.

Rules
-----
1. ``detect_concealment``       wrist leaves a product zone, goes to the
                                waistband/pocket band or a detected bag, holds
                                there, and does not return to the shelf.
2. ``detect_shelf_sweeping``    many reaches into one product zone in a short
                                window.
3. ``detect_suspicious_loitering`` long dwell by a high-value product zone with
                                repeated reaches and frequent head turning.
4. ``detect_exit_without_checkout`` interacted with products, then entered an
                                ENTRANCE/EXIT zone without a CHECKOUT visit.
5. ``detect_sweethearting``     POS-linked only: checkout hand passes with no
                                matching barcode scan. Not evaluable without POS.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.models.db_models import TheftIncidentModel
from app.models.schemas import TheftIncidentStatus, TheftStatisticsResponse

logger = logging.getLogger("TheftDetectionService")

# Rule identifiers. They are stored in theft_incidents.rule and theft_type.
RULE_CONCEALMENT = "CONCEALMENT"
RULE_SHELF_SWEEPING = "SHELF_SWEEPING"
RULE_SUSPICIOUS_LOITERING = "SUSPICIOUS_LOITERING"
RULE_EXIT_WITHOUT_CHECKOUT = "EXIT_WITHOUT_CHECKOUT"
RULE_SWEETHEARTING = "SWEETHEARTING"

RULE_LABELS = {
    RULE_CONCEALMENT: "Possible concealment",
    RULE_SHELF_SWEEPING: "Possible shelf sweeping",
    RULE_SUSPICIOUS_LOITERING: "Loitering at high-value products",
    RULE_EXIT_WITHOUT_CHECKOUT: "Exit without passing checkout",
    RULE_SWEETHEARTING: "Checkout pass without POS scan",
}

EXIT_CATEGORIES = ("ENTRANCE", "EXIT")
CHECKOUT_CATEGORY = "CHECKOUT"


# ============================================================================
# Evidence -> confidence
# ============================================================================

def _clamp01(v: float) -> float:
    if v is None or not math.isfinite(v):
        return 0.0
    return max(0.0, min(1.0, float(v)))


def saturating(value: float, reference: float) -> float:
    """1 - exp(-value/reference): 0 at nothing observed, ~0.63 at the reference."""
    if reference <= 0:
        return 1.0 if value > 0 else 0.0
    return _clamp01(1.0 - math.exp(-max(0.0, value) / reference))


def evidence_confidence(visibility: float, strength_terms: Iterable[float]) -> float:
    """Combine measured evidence into a 0..1 confidence.

    ``visibility`` is the mean keypoint visibility of the joints the rule
    relied on (how sure the pose model was that it saw them). Each strength
    term is a 0..1 measure of how strongly one signal was observed (duration,
    count, agreement). The confidence is the visibility scaled by the mean
    strength: poorly seen or weakly expressed behaviour scores low.
    """
    terms = [_clamp01(t) for t in strength_terms]
    if not terms:
        return 0.0
    return round(_clamp01(visibility) * (sum(terms) / len(terms)), 3)


def _to_seconds(ts: Any) -> Optional[float]:
    if isinstance(ts, datetime):
        return ts.timestamp()
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


# ============================================================================
# 1. Concealment
# ============================================================================

def detect_concealment(
    samples: Sequence[Dict[str, Any]],
    *,
    window_sec: float,
    min_hold_frames: int,
    no_return_sec: float,
    track_ended: bool = False,
) -> Dict[str, Any]:
    """Evaluate one hand's samples since it last left a product zone.

    ``samples`` is chronological; each item has ``t`` (seconds),
    ``in_shelf`` (wrist inside a product zone), ``in_conceal`` (wrist inside
    the waistband/pocket band or a detected bag), ``vis`` (wrist visibility)
    and optionally ``target`` ("pocket_band" | "bag"). The first sample must
    be the moment the wrist left the shelf; ``samples[0]["reach_vis"]`` may
    carry the mean wrist visibility observed during the reach itself.

    Detected when the wrist enters the concealment region within
    ``window_sec`` of leaving the shelf, stays for ``min_hold_frames``
    consecutive samples, and is then not seen back in a product zone for
    ``no_return_sec`` (or the track ended first, which is reported).
    """
    if not samples:
        return {"detected": False, "state": "idle", "reason": "No samples"}

    t0 = float(samples[0]["t"])
    hold_start_idx: Optional[int] = None
    run = 0
    held_idx: Optional[int] = None

    for i, s in enumerate(samples):
        if i > 0 and s.get("in_shelf"):
            return {"detected": False, "state": "returned", "reason": "Wrist returned to the shelf"}
        if held_idx is None:
            if s.get("in_conceal"):
                if run == 0:
                    if float(s["t"]) - t0 > window_sec:
                        return {"detected": False, "state": "expired",
                                "reason": "Wrist did not reach the body region in time"}
                    hold_start_idx = i
                run += 1
                if run >= min_hold_frames:
                    held_idx = i
            else:
                run = 0
                hold_start_idx = None
                if float(s["t"]) - t0 > window_sec:
                    return {"detected": False, "state": "expired",
                            "reason": "Wrist did not reach the body region in time"}

    if held_idx is None:
        return {"detected": False, "state": "holding" if run else "carrying", "reason": "Pattern incomplete"}

    t_hold_start = float(samples[hold_start_idx]["t"])
    t_held = float(samples[held_idx]["t"])
    t_last = float(samples[-1]["t"])
    observed_no_return = t_last - t_held
    if observed_no_return < no_return_sec and not track_ended:
        return {"detected": False, "state": "held", "reason": "Waiting to confirm the item was not returned",
                "held_index": held_idx}

    hold_samples = [s for s in samples[hold_start_idx:] if s.get("in_conceal")]
    hold_frames = len(hold_samples)
    # Duration of the continuous hold run (not just the min-frames prefix).
    hold_end_t = t_held
    for s in samples[held_idx + 1:]:
        if not s.get("in_conceal"):
            break
        hold_end_t = float(s["t"])
    hold_sec = max(hold_end_t - t_hold_start, 0.0)
    transfer_sec = max(t_hold_start - t0, 0.0)

    vis_values = [float(s.get("vis", 0.0)) for s in hold_samples]
    reach_vis = samples[0].get("reach_vis")
    if reach_vis is not None:
        vis_values.append(float(reach_vis))
    visibility = sum(vis_values) / len(vis_values) if vis_values else 0.0

    targets = {s.get("target") for s in hold_samples if s.get("target")}
    target = "bag" if "bag" in targets else "pocket_band"
    no_return_verified = observed_no_return >= no_return_sec

    # Strength terms: a longer hold, a quicker shelf->body transfer, and an
    # observed (not assumed) absence of a return all strengthen the evidence.
    strength = [
        saturating(hold_sec, max(no_return_sec, 0.5)),
        _clamp01(1.0 - transfer_sec / window_sec) if window_sec > 0 else 0.0,
        1.0 if no_return_verified else 0.5,
        _clamp01(hold_frames / (2.0 * max(min_hold_frames, 1))),
    ]
    confidence = evidence_confidence(visibility, strength)

    where = "a detected bag" if target == "bag" else "the waistband/pocket region"
    evidence = [
        f"Wrist left the product zone and reached {where} {transfer_sec:.1f}s later",
        f"Held there for {hold_frames} analysed frames ({hold_sec:.1f}s)",
        (f"Not seen returning to the shelf for {observed_no_return:.1f}s"
         if no_return_verified else
         f"Track ended {observed_no_return:.1f}s after the hold; return to shelf not observed"),
        f"Mean wrist keypoint visibility {visibility:.2f}",
    ]
    return {
        "detected": True,
        "rule": RULE_CONCEALMENT,
        "state": "confirmed",
        "confidence": confidence,
        "target": target,
        "hold_sec": round(hold_sec, 2),
        "hold_frames": hold_frames,
        "transfer_sec": round(transfer_sec, 2),
        "no_return_verified": no_return_verified,
        "held_index": held_idx,
        "evidence": evidence,
    }


# ============================================================================
# 2. Shelf sweeping
# ============================================================================

def detect_shelf_sweeping(
    reaches: Sequence[Dict[str, Any]],
    *,
    window_sec: float,
    min_reaches: int,
) -> Dict[str, Any]:
    """Many reaches into the same zone inside ``window_sec``.

    Each reach is ``{"timestamp": float|datetime|iso, "zone_id": str,
    "vis": float}``. Reaches without a zone id are grouped together.
    """
    by_zone: Dict[Any, List[tuple]] = {}
    for r in reaches or []:
        t = _to_seconds(r.get("timestamp", r.get("t")))
        if t is None:
            continue
        by_zone.setdefault(r.get("zone_id"), []).append((t, r))

    best: Optional[tuple] = None  # (count, span, zone, window_items)
    for zone_id, items in by_zone.items():
        items.sort(key=lambda x: x[0])
        j = 0
        for i in range(len(items)):
            while items[i][0] - items[j][0] > window_sec:
                j += 1
            window = items[j:i + 1]
            count = len(window)
            span = window[-1][0] - window[0][0]
            if best is None or count > best[0] or (count == best[0] and span < best[1]):
                best = (count, span, zone_id, window)

    if best is None or best[0] < min_reaches:
        return {"detected": False, "count": best[0] if best else 0, "reason": "Too few reaches in the window"}

    count, span, zone_id, window = best
    vis_values = [float(r.get("vis", 0.0)) for _, r in window]
    visibility = sum(vis_values) / len(vis_values) if vis_values else 0.0
    rate = count / max(span, 1e-3)  # reaches per second actually observed
    strength = [
        _clamp01(count / (2.0 * min_reaches)),
        _clamp01(1.0 - span / window_sec) if window_sec > 0 else 0.0,
        saturating(rate, min_reaches / max(window_sec, 1e-3)),
    ]
    confidence = evidence_confidence(visibility, strength)
    return {
        "detected": True,
        "rule": RULE_SHELF_SWEEPING,
        "zone_id": zone_id,
        "count": count,
        "span_sec": round(span, 2),
        "window_sec": window_sec,
        "confidence": confidence,
        "window_reaches": [r for _, r in window],
        "evidence": [
            f"{count} separate reaches into the same product zone within {span:.1f}s",
            f"Threshold: {min_reaches} reaches in {window_sec:.0f}s",
            f"Mean wrist keypoint visibility {visibility:.2f}",
        ],
    }


# ============================================================================
# 3. Suspicious loitering at high-value products
# ============================================================================

def estimate_head_yaw(keypoints: Any, min_vis: float) -> Optional[float]:
    """A scale-free yaw proxy from nose and ear keypoints (COCO 0, 3, 4).

    0 means facing the camera; the sign says which side the face is turned
    to. With both ears visible it is the nose offset from the ear midpoint
    divided by half the ear spacing. With only one ear visible the head is
    in profile, so the proxy is +/-1 by which side of that ear the nose sits.
    Returns None when the nose or both ears are not visible.
    """
    try:
        nose, l_ear, r_ear = keypoints[0], keypoints[3], keypoints[4]
    except (IndexError, TypeError):
        return None
    if float(nose[2]) < min_vis:
        return None
    lv, rv = float(l_ear[2]) >= min_vis, float(r_ear[2]) >= min_vis
    if lv and rv:
        mid = (float(l_ear[0]) + float(r_ear[0])) / 2.0
        half = abs(float(l_ear[0]) - float(r_ear[0])) / 2.0
        if half < 1e-3:
            return None
        return max(-1.5, min(1.5, (float(nose[0]) - mid) / half))
    if lv or rv:
        ear = l_ear if lv else r_ear
        return 1.0 if float(nose[0]) > float(ear[0]) else -1.0
    return None


def detect_suspicious_loitering(
    *,
    dwell_sec: float,
    reaches: int,
    head_turns: int,
    head_samples: int,
    visibility: float,
    min_dwell_sec: float,
    min_reaches: int,
    min_head_turns: int,
    zone_label: str = "",
) -> Dict[str, Any]:
    """All three signals must be present: long dwell, repeated reaches, frequent head turning."""
    if dwell_sec < min_dwell_sec or reaches < min_reaches or head_turns < min_head_turns:
        return {"detected": False, "reason": "Not every loitering signal was observed"}
    minutes = max(dwell_sec / 60.0, 1e-3)
    turns_per_min = head_turns / minutes
    strength = [
        _clamp01(dwell_sec / (2.0 * min_dwell_sec)),
        _clamp01(reaches / (2.0 * max(min_reaches, 1))),
        _clamp01(head_turns / (2.0 * max(min_head_turns, 1))),
        # How much of the dwell the head was actually measurable in.
        _clamp01(head_samples / max(head_turns * 4, 1)),
    ]
    confidence = evidence_confidence(visibility, strength)
    where = f" '{zone_label}'" if zone_label else ""
    return {
        "detected": True,
        "rule": RULE_SUSPICIOUS_LOITERING,
        "confidence": confidence,
        "dwell_sec": round(dwell_sec, 1),
        "reaches": reaches,
        "head_turns": head_turns,
        "evidence": [
            f"Stayed by high-value product zone{where} for {dwell_sec:.0f}s",
            f"{reaches} reaches into that zone during the stay",
            f"{head_turns} head turns ({turns_per_min:.1f}/min) estimated from nose/ear keypoints",
            f"Mean head keypoint visibility {visibility:.2f}",
        ],
    }


# ============================================================================
# 4. Exit without checkout
# ============================================================================

def detect_exit_without_checkout(
    zone_sequence: Sequence[Dict[str, Any]],
    *,
    first_interaction_t: Optional[float],
    interaction_count: int,
    interaction_vis: float,
    floor_coverage: float,
) -> Dict[str, Any]:
    """Interacted with products, then entered ENTRANCE/EXIT with no CHECKOUT since.

    ``zone_sequence`` is the chronological list of floor zones the track
    entered: ``{"t": float, "zone_id": str, "category": str}``. Only one
    camera's view of the track is available (there is no cross-camera re-id),
    so a checkout visit seen by another camera is invisible to this rule;
    the evidence says so.
    """
    if first_interaction_t is None or interaction_count <= 0:
        return {"detected": False, "reason": "No product interaction observed"}
    checkout_after = False
    for z in zone_sequence:
        t = float(z.get("t", 0.0))
        cat = str(z.get("category") or "").upper()
        if t < first_interaction_t:
            continue
        if cat == CHECKOUT_CATEGORY:
            checkout_after = True
        elif cat in EXIT_CATEGORIES:
            if checkout_after:
                return {"detected": False, "reason": "Checkout visited before exit"}
            gap = t - first_interaction_t
            strength = [
                _clamp01(interaction_count / 3.0),
                _clamp01(floor_coverage),
            ]
            confidence = evidence_confidence(interaction_vis, strength)
            return {
                "detected": True,
                "rule": RULE_EXIT_WITHOUT_CHECKOUT,
                "confidence": confidence,
                "exit_zone_id": z.get("zone_id"),
                "exit_category": cat,
                "seconds_since_first_interaction": round(gap, 1),
                "evidence": [
                    f"{interaction_count} shelf interaction(s) observed on this camera",
                    f"Entered {cat} zone {gap:.0f}s after the first interaction",
                    "No CHECKOUT zone visit observed in between",
                    f"Floor position known for {floor_coverage * 100:.0f}% of the track",
                    "Single-camera track: a checkout seen only by another camera cannot be matched",
                ],
            }
    return {"detected": False, "reason": "No exit after interaction"}


# ============================================================================
# 5. Sweethearting (POS-linked only)
# ============================================================================

def detect_sweethearting(
    pos_transactions: Sequence[Dict[str, Any]],
    cashier_hand_passes: Sequence[Dict[str, Any]],
    tolerance_sec: float = 2.0,
) -> Dict[str, Any]:
    """Checkout hand passes with no POS scan within ``tolerance_sec``.

    Only evaluable with POS data: without it an unmatched pass means "we do
    not know", so the rule reports ``evaluable: False`` instead of flagging.
    Each pass may carry ``vis`` (wrist visibility during the pass).
    """
    if not pos_transactions:
        return {"detected": False, "evaluable": False, "reason": "No POS data connected",
                "unmatched_passes": [], "total_passes": len(cashier_hand_passes or [])}
    if not cashier_hand_passes:
        return {"detected": False, "evaluable": True, "unmatched_passes": [], "total_passes": 0,
                "scanned_count": len(pos_transactions)}

    scans = [t for t in (_to_seconds(tx.get("timestamp")) for tx in pos_transactions) if t is not None]
    unmatched: List[Dict[str, Any]] = []
    vis_values: List[float] = []
    for i, p in enumerate(cashier_hand_passes):
        pt = _to_seconds(p.get("timestamp"))
        if pt is None:
            continue
        if not any(abs(pt - s) <= tolerance_sec for s in scans):
            unmatched.append({
                "pass_id": p.get("id", f"pass_{i + 1}"),
                "timestamp": p.get("timestamp"),
                "register_id": p.get("register_id"),
                "cashier_id": p.get("cashier_id"),
            })
            if p.get("vis") is not None:
                vis_values.append(float(p["vis"]))

    detected = bool(unmatched)
    result: Dict[str, Any] = {
        "detected": detected,
        "evaluable": True,
        "unmatched_passes": unmatched,
        "unmatched_count": len(unmatched),
        "total_passes": len(cashier_hand_passes),
        "scanned_count": len(pos_transactions),
    }
    if detected:
        visibility = sum(vis_values) / len(vis_values) if vis_values else 0.0
        frac = len(unmatched) / max(len(cashier_hand_passes), 1)
        result.update({
            "rule": RULE_SWEETHEARTING,
            "confidence": evidence_confidence(visibility, [frac, _clamp01(len(unmatched) / 3.0)]),
            "evidence": [
                f"{len(unmatched)} of {len(cashier_hand_passes)} checkout hand passes had no POS scan within {tolerance_sec:.1f}s",
                f"Mean wrist keypoint visibility {visibility:.2f}" if vis_values else "Wrist visibility not recorded for these passes",
            ],
        })
    return result


# ============================================================================
# Incident lifecycle
# ============================================================================

class TheftDetectionService:
    """Rule entry points plus incident lifecycle management."""

    detect_concealment = staticmethod(detect_concealment)
    detect_shelf_sweeping = staticmethod(detect_shelf_sweeping)
    detect_suspicious_loitering = staticmethod(detect_suspicious_loitering)
    detect_exit_without_checkout = staticmethod(detect_exit_without_checkout)
    detect_sweethearting = staticmethod(detect_sweethearting)

    # ------------------------------------------------------------------------
    # Incident lifecycle & DB methods
    # ------------------------------------------------------------------------
    async def get_incidents(
        self,
        db: AsyncSession,
        status: Optional[str] = None,
        severity: Optional[str] = None,
        department: Optional[str] = None,
        limit: int = 50,
        rule: Optional[str] = None,
    ) -> List[TheftIncidentModel]:
        """Query theft incidents from database with filters."""
        stmt = select(TheftIncidentModel).order_by(desc(TheftIncidentModel.timestamp)).limit(limit)

        if status:
            stmt = stmt.where(TheftIncidentModel.status == status.upper())
        if severity:
            stmt = stmt.where(TheftIncidentModel.severity == severity.upper())
        if department:
            stmt = stmt.where(TheftIncidentModel.department == department)
        if rule:
            stmt = stmt.where(TheftIncidentModel.rule == rule.upper())

        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_statistics(self, db: AsyncSession) -> TheftStatisticsResponse:
        """Loss-prevention KPIs from recorded incidents and their review outcomes.

        Value is only "actioned" when a reviewer recorded an outcome in
        ``THEFT_ACTIONED_OUTCOMES``: false alarms, "no action", incidents still
        open and rows resolved before outcomes were required never count.
        The false-alarm rate per rule is false alarms / incidents reviewed for
        that rule, the feedback needed to tune the rule thresholds.
        """
        # Stored times are naive UTC; "today" is the store's local day.
        from app.models.schemas import THEFT_ACTIONED_OUTCOMES, TheftRuleOutcomeStats
        from app.services.timeutil import local_day_bounds_utc

        now = datetime.utcnow()
        start_of_today = local_day_bounds_utc()[0]
        T = TheftIncidentModel

        active_count = await db.scalar(
            select(func.count()).select_from(T).where(T.status.in_(["ACTIVE", "ACKNOWLEDGED", "DISPATCHED"]))
        ) or 0
        today_count = await db.scalar(
            select(func.count()).select_from(T).where(T.timestamp >= start_of_today)
        ) or 0

        by_dept = {row[0]: row[1] for row in (await db.execute(
            select(T.department, func.count(T.id)).group_by(T.department))).all()}
        by_type = {row[0]: row[1] for row in (await db.execute(
            select(T.theft_type, func.count(T.id)).group_by(T.theft_type))).all()}

        rows = (await db.execute(
            select(T.status, T.resolution, T.resolved_by, T.estimated_loss_value, T.recovered_value,
                   T.rule, T.theft_type)
        )).all()
        value_actioned = 0.0
        value_pending = 0.0
        recovered: list[float] = []
        outcomes: Dict[str, int] = {}
        per_rule: Dict[str, List[int]] = {}
        legacy_unverified = 0
        for status, resolution, resolved_by, est, rec_val, rule, theft_type in rows:
            status = (status or "").upper()
            outcome = (resolution or "").upper() or None
            if status == "FALSE_ALARM":
                outcome = "FALSE_ALARM"
            if status in ("ACTIVE", "ACKNOWLEDGED", "DISPATCHED"):
                value_pending += float(est or 0.0)
                continue
            if status not in ("RESOLVED", "FALSE_ALARM") or outcome is None:
                continue
            # A RESOLVED row without resolved_by predates required outcomes:
            # its outcome may be the old RECOVERED_GOODS default. Only an
            # explicit false alarm is trusted from that era.
            if resolved_by is None and outcome != "FALSE_ALARM":
                legacy_unverified += 1
                continue
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            key = rule or theft_type or "UNKNOWN"
            stat = per_rule.setdefault(key, [0, 0])
            stat[0] += 1
            if outcome == "FALSE_ALARM":
                stat[1] += 1
            if outcome in THEFT_ACTIONED_OUTCOMES:
                value_actioned += float(est or 0.0)
            if rec_val is not None:
                recovered.append(float(rec_val))

        reviewed = sum(v[0] for v in per_rule.values())
        false_alarms = sum(v[1] for v in per_rule.values())
        by_rule = [
            TheftRuleOutcomeStats(rule=r, label=RULE_LABELS.get(r, r), reviewed=n, false_alarms=fa,
                                  false_alarm_rate=round(fa / n, 4) if n else None)
            for r, (n, fa) in sorted(per_rule.items())
        ]
        return TheftStatisticsResponse(
            active_incidents_count=int(active_count),
            today_incidents_count=int(today_count),
            prevented_loss_estimate=round(value_actioned, 2),
            by_department=by_dept,
            by_theft_type=by_type,
            generated_at=now,
            value_actioned=round(value_actioned, 2),
            value_pending_outcome=round(value_pending, 2),
            value_recovered=round(sum(recovered), 2) if recovered else None,
            outcomes=outcomes,
            reviewed_count=reviewed,
            false_alarm_count=false_alarms,
            false_alarm_rate=round(false_alarms / reviewed, 4) if reviewed else None,
            false_alarm_rate_by_rule=by_rule,
            unverified_legacy_resolutions=legacy_unverified,
        )

    async def acknowledge_incident(
        self,
        incident_id: str,
        guard_id: str,
        db: AsyncSession,
    ) -> Optional[TheftIncidentModel]:
        """Record who acknowledged the incident (the caller passes the real actor)."""
        stmt = select(TheftIncidentModel).where(TheftIncidentModel.id == incident_id)
        res = await db.execute(stmt)
        incident = res.scalar_one_or_none()
        if not incident:
            return None

        incident.status = TheftIncidentStatus.ACKNOWLEDGED.value
        incident.guard_id = (guard_id or "")[:64] or None
        await db.commit()
        await db.refresh(incident)
        logger.info(f"Theft incident {incident_id} ACKNOWLEDGED by {guard_id}")
        return incident

    async def dispatch_security(
        self,
        incident_id: str,
        dispatched_by: str,
        staff_name: Optional[str] = None,
        note: Optional[str] = None,
        db: Optional[AsyncSession] = None,
    ) -> Optional[TheftIncidentModel]:
        """Record that staff were sent: who marked it and when, plus what they typed.

        Nothing is invented here. There is no default guard unit and no audio
        deterrent: this system has no way to dispatch a unit or play audio, so
        it records only what the operator did. Phones are alerted by the route
        through ``alert_dispatcher``.
        """
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

            when = datetime.utcnow()
            incident.status = TheftIncidentStatus.DISPATCHED.value
            incident.dispatched_by = (dispatched_by or "")[:128] or None
            incident.dispatched_at = when
            details = {"dispatched_by": incident.dispatched_by, "dispatched_at": when.isoformat()}
            if staff_name and staff_name.strip():
                details["staff_sent"] = staff_name.strip()[:128]
            if note and note.strip():
                details["note"] = note.strip()[:500]
            incident.dispatch_details = details
            await session.commit()
            await session.refresh(incident)
            logger.info(f"Staff sent to incident {incident_id} (marked by {dispatched_by})")
            return incident
        finally:
            if should_close and session:
                await session.close()

    async def record_dispatch_alert(self, incident_id: str, report: Dict[str, Any],
                                    db: AsyncSession) -> None:
        """Store the real delivery report of the staff-sent alert on the incident."""
        incident = await db.get(TheftIncidentModel, incident_id)
        if incident is None:
            return
        details = dict(incident.dispatch_details or {})
        details["alert"] = report
        incident.dispatch_details = details
        await db.commit()
        await db.refresh(incident)

    async def resolve_incident(
        self,
        incident_id: str,
        resolution: str,
        notes: Optional[str] = None,
        db: Optional[AsyncSession] = None,
        *,
        resolved_by: Optional[str] = None,
        recovered_value: Optional[float] = None,
    ) -> Optional[TheftIncidentModel]:
        """Close an incident with the reviewer's outcome (FALSE_ALARM sets that status).

        ``resolution`` is required: there is no default outcome. Re-resolving
        replaces the outcome, so a wrong or legacy entry can be corrected.
        """
        if not resolution:
            raise ValueError("an outcome is required")
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

            outcome = resolution.upper()
            status_val = TheftIncidentStatus.FALSE_ALARM.value if outcome == "FALSE_ALARM" else TheftIncidentStatus.RESOLVED.value
            incident.status = status_val
            incident.resolution = outcome
            incident.resolved_at = datetime.utcnow()
            incident.resolved_by = (resolved_by or "")[:128] or None
            incident.recovered_value = recovered_value
            if notes:
                incident.notes = notes

            await session.commit()
            await session.refresh(incident)
            logger.info(f"Theft incident {incident_id} marked as {status_val} ({outcome}) by {resolved_by}")
            return incident
        finally:
            if should_close and session:
                await session.close()


# Global singleton instance
theft_detection_service = TheftDetectionService()
