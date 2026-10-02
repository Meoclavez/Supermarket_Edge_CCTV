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
1. ``detect_concealment``       wrist leaves a product zone, goes to the body
                                -- the waistband/pocket band, the opposite side
                                of the chest (inside a jacket), or out of sight
                                behind the body -- holds there, and does not
                                return to the shelf.
2. ``detect_shelf_sweeping``    many reaches into one product zone in a short
                                window.
3. ``detect_suspicious_loitering`` long dwell by a high-value product zone with
                                repeated reaches and frequent head turning.
4. ``detect_exit_without_checkout`` interacted with products, then entered an
                                ENTRANCE/EXIT zone without a CHECKOUT visit.
5. ``detect_sweethearting``     POS-linked only: checkout hand passes with no
                                matching barcode scan. Not evaluable without POS.
6. ``detect_behaviour_pattern`` several weak cues of one person (unconfirmed
                                concealment holds, pickups not put back, head
                                scanning, dwell at high-value stock, quick
                                repeated reaches, exit without checkout),
                                weighted, time-decayed and capped per cue type;
                                needs at least two distinct cue types. Raised
                                only when no single rule fired on the person.
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
RULE_BEHAVIOUR_PATTERN = "BEHAVIOUR_PATTERN"

RULE_LABELS = {
    RULE_CONCEALMENT: "Possible concealment",
    RULE_SHELF_SWEEPING: "Possible shelf sweeping",
    RULE_SUSPICIOUS_LOITERING: "Loitering at high-value products",
    RULE_EXIT_WITHOUT_CHECKOUT: "Exit without passing checkout",
    RULE_SWEETHEARTING: "Checkout pass without POS scan",
    RULE_BEHAVIOUR_PATTERN: "Suspicious behaviour pattern",
}

# Where a concealing hand went (detect_concealment ``target``).
CONCEAL_POCKET = "pocket"            # waistband / pocket band
CONCEAL_CHEST = "chest"              # opposite side of the upper torso (inside a jacket)
CONCEAL_BEHIND_BACK = "behind_back"  # out of sight behind the body / into a worn bag
_VISIBLE_TARGETS = (CONCEAL_POCKET, CONCEAL_CHEST)
_TARGET_WHERE = {
    CONCEAL_POCKET: "the waistband/pocket region",
    CONCEAL_CHEST: "the opposite side of the chest (inside-jacket position)",
    CONCEAL_BEHIND_BACK: "out of sight behind the body (back or a worn bag)",
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

def _sample_target(s: Dict[str, Any]) -> str:
    """The concealment target of one in-region sample.

    "pocket_band" (the earlier label) and anything unlabelled is the pocket
    band: a stale "bag" from the removed object cue is never reported.
    """
    t = s.get("target")
    if t in (CONCEAL_CHEST, CONCEAL_BEHIND_BACK):
        return t
    return CONCEAL_POCKET


def _run_target(run: Sequence[Dict[str, Any]]) -> str:
    """Label of a hold run: the most frequent *seen* target, else behind_back.

    A run with any sample of the wrist actually seen at the body is labelled
    by where it was seen; only a run in which the wrist was never seen (all
    occluded) is "behind_back".
    """
    counts: Dict[str, int] = {}
    for s in run:
        t = _sample_target(s)
        if t in _VISIBLE_TARGETS:
            counts[t] = counts.get(t, 0) + 1
    if not counts:
        return CONCEAL_BEHIND_BACK
    return max(counts.items(), key=lambda kv: (kv[1], kv[0] == CONCEAL_POCKET))[0]


def detect_concealment(
    samples: Sequence[Dict[str, Any]],
    *,
    window_sec: float,
    min_hold_frames: int,
    no_return_sec: float,
    track_ended: bool = False,
    occluded_min_hold_sec: float = 0.0,
    occluded_confidence_factor: float = 1.0,
    chest_max_hold_sec: Optional[float] = None,
) -> Dict[str, Any]:
    """Evaluate one hand's samples since it last left a product zone.

    ``samples`` is chronological; each item has ``t`` (seconds),
    ``in_shelf`` (wrist inside a product zone), ``in_conceal`` (hand at a
    concealment target), ``vis`` (wrist visibility) and ``target``:

    * ``"pocket"`` -- wrist seen in the waistband/pocket band (the default
      for an in-region sample without a target; "pocket_band" is accepted),
    * ``"chest"`` -- wrist seen on the opposite side of the upper torso,
    * ``"behind_back"`` -- wrist out of sight behind the body. The caller
      marks it only for a wrist seen leaving the shelf and then hidden while
      the torso stayed visible; such samples carry ``body_vis`` (mean
      visibility of the torso and elbow keypoints that were seen instead).

    The first sample must be the moment the wrist left the shelf;
    ``samples[0]["reach_vis"]`` may carry the mean wrist visibility observed
    during the reach itself.

    Detected when the hand reaches a target within ``window_sec`` of leaving
    the shelf, stays for ``min_hold_frames`` consecutive samples, and is then
    not seen back in a product zone for ``no_return_sec`` (or the track ended
    first, which is reported). Stricter for the weaker targets:

    * a hold in which the wrist was never seen (behind_back) must also last
      ``occluded_min_hold_sec`` (an arm swing hides a wrist only briefly),
      and its confidence is scaled by ``occluded_confidence_factor``;
    * a chest hold must end (hand withdrawn) before it is confirmed, and one
      longer than ``chest_max_hold_sec`` is examining a product or a phone,
      never concealment (state ``"examining"``).
    """
    if not samples:
        return {"detected": False, "state": "idle", "reason": "No samples"}

    t0 = float(samples[0]["t"])
    hold_start_idx: Optional[int] = None
    run = 0
    held_idx: Optional[int] = None

    def _qualifies(start: int, end: int) -> bool:
        part = samples[start:end + 1]
        if _run_target(part) != CONCEAL_BEHIND_BACK:
            return True
        return float(part[-1]["t"]) - float(part[0]["t"]) >= occluded_min_hold_sec - 1e-6

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
                if run >= min_hold_frames and _qualifies(hold_start_idx, i):
                    held_idx = i
            else:
                run = 0
                hold_start_idx = None
                if float(s["t"]) - t0 > window_sec:
                    return {"detected": False, "state": "expired",
                            "reason": "Wrist did not reach the body region in time"}

    if held_idx is None:
        return {"detected": False, "state": "holding" if run else "carrying", "reason": "Pattern incomplete"}

    # The continuous hold run (not just the min-frames prefix).
    run_end_idx = held_idx
    for j in range(held_idx + 1, len(samples)):
        if not samples[j].get("in_conceal"):
            break
        run_end_idx = j
    run_samples = list(samples[hold_start_idx:run_end_idx + 1])
    target = _run_target(run_samples)
    t_hold_start = float(samples[hold_start_idx]["t"])
    t_held = float(samples[held_idx]["t"])
    hold_end_t = float(samples[run_end_idx]["t"])
    hold_sec = max(hold_end_t - t_hold_start, 0.0)
    run_ongoing = run_end_idx == len(samples) - 1

    if target == CONCEAL_CHEST:
        if chest_max_hold_sec is not None and hold_sec > chest_max_hold_sec:
            return {"detected": False, "state": "examining",
                    "reason": "Hand stayed at the chest (examining a product or a phone), not a quick concealment"}
        if run_ongoing and not track_ended:
            return {"detected": False, "state": "held", "reason": "Waiting for the hand to leave the chest",
                    "held_index": held_idx}

    t_last = float(samples[-1]["t"])
    observed_no_return = t_last - t_held
    if observed_no_return < no_return_sec and not track_ended:
        return {"detected": False, "state": "held", "reason": "Waiting to confirm the item was not returned",
                "held_index": held_idx}

    hold_samples = [s for s in samples[hold_start_idx:] if s.get("in_conceal")]
    hold_frames = len(hold_samples)
    transfer_sec = max(t_hold_start - t0, 0.0)

    # Visibility of what was actually seen: the wrist where it was seen at
    # the body, the torso and elbow where the wrist was hidden behind it.
    vis_values = []
    for s in hold_samples:
        if _sample_target(s) == CONCEAL_BEHIND_BACK:
            vis_values.append(float(s.get("body_vis", 0.0)))
        else:
            vis_values.append(float(s.get("vis", 0.0)))
    reach_vis = samples[0].get("reach_vis")
    if reach_vis is not None:
        vis_values.append(float(reach_vis))
    visibility = sum(vis_values) / len(vis_values) if vis_values else 0.0
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
    occluded = target == CONCEAL_BEHIND_BACK
    if occluded:
        confidence = round(confidence * _clamp01(occluded_confidence_factor), 3)

    where = _TARGET_WHERE[target]
    if occluded:
        evidence = [
            f"Wrist seen leaving the product zone, then went {where} {transfer_sec:.1f}s later",
            (f"Hand stayed hidden for {hold_frames} analysed frames ({hold_sec:.1f}s) while the "
             "shoulders, hips, knees and that arm's elbow stayed visible and the face looked at the camera"),
        ]
    else:
        evidence = [
            f"Wrist left the product zone and reached {where} {transfer_sec:.1f}s later",
            f"Held there for {hold_frames} analysed frames ({hold_sec:.1f}s)",
        ]
    evidence.append(
        f"Not seen returning to the shelf for {observed_no_return:.1f}s"
        if no_return_verified else
        f"Track ended {observed_no_return:.1f}s after the hold; return to shelf not observed")
    if occluded:
        evidence.append(f"Mean torso/elbow keypoint visibility {visibility:.2f}; inferred from the hand "
                        f"going out of sight, so confidence is scaled by {occluded_confidence_factor:.2f}")
    else:
        evidence.append(f"Mean wrist keypoint visibility {visibility:.2f}")
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
# 6. Behaviour pattern (fusion of weak cues)
# ============================================================================

# Cue types the live engine records per track, with the wording staff see.
CUE_CONCEAL_HOLD = "conceal_hold"
CUE_REACH_NO_RETURN = "reach_no_return"
CUE_HEAD_SCAN = "head_scan"
CUE_HIGH_VALUE_DWELL = "high_value_dwell"
CUE_SWEEP_PARTIAL = "sweep_partial"
CUE_EXIT_NO_CHECKOUT = "exit_no_checkout"

PATTERN_CUE_LABELS = {
    CUE_CONCEAL_HOLD: "Hand held at the body after a shelf reach (concealment not confirmed on its own)",
    CUE_REACH_NO_RETURN: "Product picked up and not put back",
    CUE_HEAD_SCAN: "Frequent head turning (looking around)",
    CUE_HIGH_VALUE_DWELL: "Long stay by high-value products",
    CUE_SWEEP_PARTIAL: "Several quick reaches into one product zone",
    CUE_EXIT_NO_CHECKOUT: "Went to the exit with no checkout visit seen",
}


def parse_cue_map(raw: str) -> Dict[str, float]:
    """``"cue:0.5,other:0.1"`` -> ``{"cue": 0.5, "other": 0.1}``; bad entries are skipped."""
    out: Dict[str, float] = {}
    for part in str(raw or "").split(","):
        name, _, val = part.partition(":")
        name = name.strip()
        try:
            v = float(val)
        except ValueError:
            continue
        if name and math.isfinite(v) and v >= 0:
            out[name] = v
    return out


def detect_behaviour_pattern(
    cues: Sequence[Dict[str, Any]],
    *,
    now: float,
    half_life_sec: float,
    threshold: float,
    min_cue_types: int,
    weights: Dict[str, float],
    caps: Dict[str, float],
) -> Dict[str, Any]:
    """Fuse one person's weak cues into a suspicion score.

    Each cue is ``{"t": seconds, "cue": type, "vis": keypoint visibility of
    what was measured, "detail": optional evidence text}``. An event adds its
    type's weight decayed by ``0.5 ** (age / half_life_sec)``; each type's
    total is capped (``caps``, default: its weight), so repeating one ordinary
    action -- many pickups, many glances -- cannot reach the threshold alone.
    A type counts as *present* while its decayed total is at least half its
    weight (roughly: an event within the last half-life).

    Detected when the summed score reaches ``threshold`` and at least
    ``min_cue_types`` distinct types are present. Cue types without a weight
    are ignored. The caller must not evaluate this for a person on whom a
    single rule already fired.
    """
    hl = max(float(half_life_sec), 1e-3)
    per_type: Dict[str, Dict[str, Any]] = {}
    for c in cues or []:
        kind = c.get("cue")
        w = float(weights.get(kind, 0.0) or 0.0)
        t = _to_seconds(c.get("t"))
        if w <= 0 or t is None or t > now + 1e-6:
            continue
        age = max(now - t, 0.0)
        info = per_type.setdefault(kind, {"raw": 0.0, "count": 0, "last_t": t, "last": c, "vis": []})
        info["raw"] += w * (0.5 ** (age / hl))
        info["count"] += 1
        if t >= info["last_t"]:
            info["last_t"], info["last"] = t, c
        if c.get("vis") is not None:
            info["vis"].append(float(c["vis"]))

    score = 0.0
    present: List[str] = []
    for kind, info in per_type.items():
        w = float(weights[kind])
        cap = float(caps.get(kind, w))
        info["contribution"] = min(info["raw"], cap)
        score += info["contribution"]
        if info["contribution"] >= 0.5 * w - 1e-9:
            present.append(kind)
    score = round(score, 3)
    n_types = len(present)
    if score < threshold or n_types < max(1, int(min_cue_types)):
        return {"detected": False, "score": score, "cue_types": sorted(present),
                "reason": ("Fused score below the threshold" if score < threshold
                           else "Too few distinct kinds of behaviour")}

    vis_values = [v for k in present for v in per_type[k]["vis"]]
    visibility = sum(vis_values) / len(vis_values) if vis_values else 0.0
    strength = [
        _clamp01(score / (2.0 * max(threshold, 1e-3))),
        _clamp01(n_types / (max(int(min_cue_types), 1) + 2.0)),
    ]
    confidence = evidence_confidence(visibility, strength)

    evidence = [
        f"{n_types} different kinds of behaviour added up to a score of {score:.2f} "
        f"(threshold {threshold:.2f}; each cue fades with a {hl:.0f}s half-life)",
    ]
    ordered = sorted(present, key=lambda k: -per_type[k]["contribution"])
    for kind in ordered:
        info = per_type[kind]
        line = (f"{PATTERN_CUE_LABELS.get(kind, kind)}: {info['count']}x, last {now - info['last_t']:.0f}s ago "
                f"(adds {info['contribution']:.2f})")
        detail = info["last"].get("detail")
        if detail:
            line += f" - {detail}"
        evidence.append(line)
    evidence.append(f"Mean keypoint visibility of these observations {visibility:.2f}")
    evidence.append("No single loss-prevention rule fired for this person; this combination of weaker "
                    "signals is for staff review")
    return {
        "detected": True,
        "rule": RULE_BEHAVIOUR_PATTERN,
        "confidence": confidence,
        "score": score,
        "threshold": threshold,
        "cue_types": ordered,
        "contributions": {k: round(per_type[k]["contribution"], 3) for k in ordered},
        "evidence": evidence,
    }


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
    detect_behaviour_pattern = staticmethod(detect_behaviour_pattern)

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
            # A RESOLVED row without resolved_by predates required outcomes:
            # its outcome may be the old RECOVERED_GOODS default. Only an
            # explicit false alarm is trusted from that era (review_outcome).
            kind, outcome = review_outcome(status, resolution, resolved_by)
            if kind == "open":
                value_pending += float(est or 0.0)
                continue
            if kind == "legacy":
                legacy_unverified += 1
                continue
            if kind != "reviewed":
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


# ============================================================================
# Review outcomes and pattern analysis (reporting)
# ============================================================================

OPEN_STATUSES = ("ACTIVE", "ACKNOWLEDGED", "DISPATCHED")
WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def review_outcome(status: Optional[str], resolution: Optional[str],
                   resolved_by: Optional[str]) -> tuple[str, Optional[str]]:
    """Classify an incident for the statistics: (kind, outcome).

    kind is "open" (still waiting), "reviewed" (a trusted outcome),
    "legacy" (resolved before outcomes were required: its outcome may be the
    old RECOVERED_GOODS default, so only an explicit false alarm is trusted)
    or "other" (no usable outcome).
    """
    status = (status or "").upper()
    outcome = (resolution or "").upper() or None
    if status == "FALSE_ALARM":
        outcome = "FALSE_ALARM"
    if status in OPEN_STATUSES:
        return "open", None
    if status not in ("RESOLVED", "FALSE_ALARM") or outcome is None:
        return "other", None
    if resolved_by is None and outcome != "FALSE_ALARM":
        return "legacy", outcome
    return "reviewed", outcome


def _iso_utc(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() + "Z" if dt is not None else None


async def _zone_names(db: AsyncSession, zone_ids: Iterable[str]) -> Dict[str, str]:
    """Operator-given names for zone ids: floor zones, then product zones seen in reaches."""
    ids = sorted({z for z in zone_ids if z})
    if not ids:
        return {}
    from app.models.db_models import ShelfInteractionModel, StoreZoneModel

    names: Dict[str, str] = {}
    try:
        for zid, zname in (await db.execute(
                select(ShelfInteractionModel.shelf_zone_id, ShelfInteractionModel.zone_name)
                .where(ShelfInteractionModel.shelf_zone_id.in_(ids),
                       ShelfInteractionModel.zone_name.is_not(None))
                .order_by(ShelfInteractionModel.timestamp))).all():
            names[zid] = zname                      # latest name wins
    except Exception as e:  # noqa: BLE001 - names are a nicety
        logger.debug(f"zone names from shelf interactions unavailable: {e}")
    try:
        for zid, zname in (await db.execute(
                select(StoreZoneModel.id, StoreZoneModel.name).where(StoreZoneModel.id.in_(ids)))).all():
            names[zid] = zname
    except Exception as e:  # noqa: BLE001
        logger.debug(f"floor zone names unavailable: {e}")
    try:
        from app.services.shelf_interaction_service import shelf_interaction_service as sis

        for z in sis.get_zones():                   # current product zones (all cameras)
            if getattr(z, "id", None) in ids and getattr(z, "name", None):
                names[z.id] = z.name
    except Exception:  # noqa: BLE001 - the in-memory product zones are optional here
        pass
    return names


async def get_patterns(db: AsyncSession, days: int = 28, burst_minutes: int = 30,
                       burst_min: int = 3) -> Dict[str, Any]:
    """Where and when incidents happen, from recorded rows only.

    * ``hotspots.cameras`` / ``hotspots.zones``: incident counts per camera and
      per (camera, zone), with each one's reviewed count, false alarms and
      false-alarm rate (false alarms / reviewed, null when nothing reviewed).
    * ``hour_dow``: a 7 x 24 matrix of incidents by store-local weekday
      (Mon first) and hour.
    * ``rules``: the rule mix with each rule's false-alarm rate.
    * ``weekly``: incidents per store-local week (Monday start), zero weeks
      inside the range included.
    * ``bursts``: runs of at least ``burst_min`` incidents on the same camera
      and zone, each within ``burst_minutes`` of the previous one. A burst is
      repeated behaviour at one place; it does not identify a person (there is
      no re-identification), so it may be one shopper or several.

    Empty arrays (and an empty matrix) when there are no incidents in range.
    """
    from datetime import timedelta

    from app.models.db_models import CameraModel
    from app.models.schemas import THEFT_ACTIONED_OUTCOMES
    from app.services.timeutil import site_tz, to_local, utcnow

    days = max(1, int(days))
    burst_minutes = max(1, int(burst_minutes))
    burst_min = max(2, int(burst_min))
    now = utcnow()
    local_today = to_local(now).date()
    # Whole store-local days: today plus the days-1 before it.
    from app.services.timeutil import local_midnight_utc

    since = local_midnight_utc(local_today - timedelta(days=days - 1))
    T = TheftIncidentModel
    rows = (await db.execute(
        select(T.id, T.timestamp, T.camera_id, T.camera_name, T.zone_id, T.shelf_zone_id,
               T.rule, T.theft_type, T.severity, T.status, T.resolution, T.resolved_by)
        .where(T.timestamp >= since)
        .order_by(T.timestamp)
    )).all()

    cam_names: Dict[str, str] = {}
    try:
        cam_names = {cid: name for cid, name in (await db.execute(
            select(CameraModel.id, CameraModel.name))).all() if name}
    except Exception as e:  # noqa: BLE001
        logger.debug(f"camera names unavailable: {e}")

    tz = site_tz()
    tz_name = getattr(tz, "key", None) or str(to_local(now).tzinfo)
    base: Dict[str, Any] = {
        "days": days,
        "since": _iso_utc(since),
        "generated_at": _iso_utc(now),
        "timezone": tz_name,
        "burst_window_minutes": burst_minutes,
        "burst_min_incidents": burst_min,
        "note": ("Patterns are built from recorded incidents only. Bursts group incidents by camera, "
                 "zone and time; they do not identify a person."),
    }
    if not rows:
        return {**base,
                "summary": {"incidents": 0, "open": 0, "reviewed": 0, "false_alarms": 0,
                            "confirmed": 0, "false_alarm_rate": None, "unverified_legacy": 0},
                "hotspots": {"cameras": [], "zones": [], "unzoned_incidents": 0},
                "hour_dow": {"days": list(WEEKDAY_NAMES), "hours": list(range(24)), "matrix": [],
                             "max": 0, "peak": None},
                "rules": [], "weekly": [], "bursts": []}

    zone_names = await _zone_names(db, (r.zone_id or r.shelf_zone_id for r in rows))

    def tally() -> Dict[str, Any]:
        return {"incidents": 0, "reviewed": 0, "false_alarms": 0, "confirmed": 0, "open": 0,
                "rules": {}, "last": None}

    def add(t: Dict[str, Any], rule: str, kind: str, outcome: Optional[str], ts: datetime) -> None:
        t["incidents"] += 1
        t["rules"][rule] = t["rules"].get(rule, 0) + 1
        t["last"] = ts if t["last"] is None or ts > t["last"] else t["last"]
        if kind == "open":
            t["open"] += 1
        elif kind == "reviewed":
            t["reviewed"] += 1
            if outcome == "FALSE_ALARM":
                t["false_alarms"] += 1
            elif outcome in THEFT_ACTIONED_OUTCOMES:
                t["confirmed"] += 1

    def rate(t: Dict[str, Any]) -> Optional[float]:
        return round(t["false_alarms"] / t["reviewed"], 4) if t["reviewed"] else None

    def top_rule(t: Dict[str, Any]) -> Optional[str]:
        return max(sorted(t["rules"]), key=lambda r: t["rules"][r]) if t["rules"] else None

    total = tally()
    by_cam: Dict[str, Dict[str, Any]] = {}
    by_zone: Dict[tuple, Dict[str, Any]] = {}
    by_rule: Dict[str, Dict[str, Any]] = {}
    matrix = [[0] * 24 for _ in range(7)]
    weeks: Dict[Any, Dict[str, Any]] = {}
    legacy = 0
    unzoned = 0
    places: Dict[tuple, List[tuple]] = {}
    cam_label: Dict[str, str] = {}

    for r in rows:
        rule = r.rule or r.theft_type or "UNKNOWN"
        kind, outcome = review_outcome(r.status, r.resolution, r.resolved_by)
        if kind == "legacy":
            legacy += 1
        cam_label[r.camera_id] = cam_names.get(r.camera_id) or r.camera_name or r.camera_id
        zone = r.zone_id or r.shelf_zone_id
        add(total, rule, kind, outcome, r.timestamp)
        add(by_cam.setdefault(r.camera_id, tally()), rule, kind, outcome, r.timestamp)
        add(by_rule.setdefault(rule, tally()), rule, kind, outcome, r.timestamp)
        if zone:
            add(by_zone.setdefault((r.camera_id, zone), tally()), rule, kind, outcome, r.timestamp)
        else:
            unzoned += 1
        local = to_local(r.timestamp)
        matrix[local.weekday()][local.hour] += 1
        week = local.date() - timedelta(days=local.weekday())
        add(weeks.setdefault(week, tally()), rule, kind, outcome, r.timestamp)
        places.setdefault((r.camera_id, zone), []).append((r.timestamp, r.id, rule, kind, outcome))

    n = total["incidents"]

    def summary_of(t: Dict[str, Any]) -> Dict[str, Any]:
        tr = top_rule(t)
        return {"incidents": t["incidents"], "share": round(t["incidents"] / n, 4),
                "open": t["open"], "reviewed": t["reviewed"], "false_alarms": t["false_alarms"],
                "confirmed": t["confirmed"], "false_alarm_rate": rate(t),
                "top_rule": tr, "top_rule_label": RULE_LABELS.get(tr, tr) if tr else None,
                "last_at": _iso_utc(t["last"])}

    cameras = sorted(({"camera_id": cid, "camera_name": cam_label[cid], **summary_of(t)}
                      for cid, t in by_cam.items()),
                     key=lambda x: (-x["incidents"], x["camera_name"] or ""))
    zones = sorted(({"camera_id": cid, "camera_name": cam_label[cid], "zone_id": zid,
                     "zone_name": zone_names.get(zid), **summary_of(t)}
                    for (cid, zid), t in by_zone.items()),
                   key=lambda x: (-x["incidents"], x["camera_name"] or "", x["zone_id"]))
    rules_out = sorted(({"rule": rule, "label": RULE_LABELS.get(rule, rule),
                         **{k: v for k, v in summary_of(t).items() if not k.startswith("top_rule")}}
                        for rule, t in by_rule.items()),
                       key=lambda x: (-x["incidents"], x["rule"]))

    # Weekly trend: every week from the first incident's week to this week.
    weekly = []
    if weeks:
        wk = min(weeks)
        this_week = local_today - timedelta(days=local_today.weekday())
        while wk <= this_week:
            t = weeks.get(wk) or tally()
            weekly.append({"week_start": wk.isoformat(), "incidents": t["incidents"],
                           "reviewed": t["reviewed"], "false_alarms": t["false_alarms"],
                           "confirmed": t["confirmed"], "false_alarm_rate": rate(t)})
            wk += timedelta(days=7)

    # Bursts: runs on one camera + zone, each incident within the window of the previous.
    window = timedelta(minutes=burst_minutes)
    bursts = []
    for (cid, zone), items in places.items():
        run: List[tuple] = []
        for item in items + [None]:
            if item is not None and run and item[0] - run[-1][0] <= window:
                run.append(item)
                continue
            if len(run) >= burst_min:
                t = tally()
                for ts, _iid, rule, kind, outcome in run:
                    add(t, rule, kind, outcome, ts)
                start, end = run[0][0], run[-1][0]
                bursts.append({
                    "camera_id": cid, "camera_name": cam_label[cid],
                    "zone_id": zone, "zone_name": zone_names.get(zone) if zone else None,
                    "start": _iso_utc(start), "end": _iso_utc(end),
                    "duration_minutes": round((end - start).total_seconds() / 60.0, 1),
                    "incidents": len(run),
                    "rules": [{"rule": k, "label": RULE_LABELS.get(k, k), "incidents": v}
                              for k, v in sorted(t["rules"].items(), key=lambda kv: (-kv[1], kv[0]))],
                    "incident_ids": [x[1] for x in run],
                    "open": t["open"], "reviewed": t["reviewed"], "false_alarms": t["false_alarms"],
                    "confirmed": t["confirmed"],
                })
            run = [item] if item is not None else []
    bursts.sort(key=lambda b: b["start"], reverse=True)

    peak = None
    mx = max(max(row) for row in matrix)
    if mx > 0:
        d, h = max(((d, h) for d in range(7) for h in range(24)), key=lambda dh: (matrix[dh[0]][dh[1]], -dh[0], -dh[1]))
        peak = {"day": WEEKDAY_NAMES[d], "day_index": d, "hour": h, "incidents": matrix[d][h]}

    return {
        **base,
        "summary": {"incidents": n, "open": total["open"], "reviewed": total["reviewed"],
                    "false_alarms": total["false_alarms"], "confirmed": total["confirmed"],
                    "false_alarm_rate": rate(total), "unverified_legacy": legacy},
        "hotspots": {"cameras": cameras, "zones": zones, "unzoned_incidents": unzoned},
        "hour_dow": {"days": list(WEEKDAY_NAMES), "hours": list(range(24)), "matrix": matrix,
                     "max": mx, "peak": peak},
        "rules": rules_out,
        "weekly": weekly,
        "bursts": bursts,
    }


TheftDetectionService.get_patterns = staticmethod(get_patterns)
