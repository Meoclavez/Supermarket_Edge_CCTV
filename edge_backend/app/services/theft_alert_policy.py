"""Theft alert levels: risk score, tier and routing of each incident.

Every loss-prevention incident (``pose_analytics._raise``) is scored here::

    risk = clamp(confidence x place weight x case weight)     0..1

* **confidence** -- the rule's own evidence confidence (keypoint visibility,
  duration, agreeing signals; see ``theft_detection_service``).
* **place weight** -- where it happened, multiplied and capped at
  :data:`PLACE_WEIGHT_CAP`: the product area's value (high-value category or
  price, or value tier PREMIUM / LOW), an EXIT/ENTRANCE floor zone or a door
  camera (the larger of the two, never both), a checkout camera.
* **case weight** -- what kind of behaviour it was (rule, and for
  concealment where the hand went).

The score gives a tier from the thresholds (``THEFT_TIER_*_MIN``); then:

* concealment followed by exit-without-checkout on the same track within
  ``THEFT_COMBO_WINDOW_SEC`` is ``critical``;
* any two different rules on the same track within that window: +1 tier;
* at least ``THEFT_BURST_MIN_INCIDENTS`` incidents in the same zone within
  ``THEFT_BURST_WINDOW_MIN``: +1 tier;
* the camera role's ``alert_severity_floor`` is a minimum tier
  (``camera_roles.min_alert_tier``) for incidents whose risk reached the
  watch level; weaker evidence stays in review (the reason says so).

Every step is recorded in ``risk_factors`` as ``{factor, value, effect,
reason}`` with a plain-language reason for staff. Nothing here is a finding
of theft: the tier only decides who is told and how loudly.

Tiers and routing (``routing()``; the store may change the ``THEFT_ROUTE_*``
site settings, the rest is fixed):

=========  ===========  ======================  ===========================
tier       severity     always                   switchable
=========  ===========  ======================  ===========================
review     LOW          review list only         --
watch      MEDIUM       dashboard banner         sound, phone push
alert      HIGH         banner + sound           phone push (on), escalation
critical   HIGH         banner + sound + push +  --
                        escalation + repeats
=========  ===========  ======================  ===========================
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.config import settings

logger = logging.getLogger("edge.theft_alert_policy")

TIERS: Tuple[str, ...] = ("review", "watch", "alert", "critical")
TIER_LABELS = {"review": "Review", "watch": "Watch", "alert": "Alert", "critical": "Critical"}
TIER_HELP = {
    "review": "Kept in the review list only. Nobody is interrupted.",
    "watch": "Shown in a banner on the dashboard. No sound and no phone unless switched on.",
    "alert": "Banner and alarm sound on the dashboard, and sent to the first-priority phones.",
    "critical": ("As Alert, and escalated to the backup people and repeated until someone "
                 "acknowledges it."),
}
# Incident severity kept for older clients and filters.
TIER_SEVERITY = {"review": "LOW", "watch": "MEDIUM", "alert": "HIGH", "critical": "HIGH"}
# Alert-log / phone severity (EventSeverity, per-phone min_severity prefs).
TIER_EVENT_SEVERITY = {"review": "INFO", "watch": "WARNING", "alert": "HIGH", "critical": "HIGH"}
# Title prefix of the dashboard/phone message.
TIER_TITLE = {"review": "Review", "watch": "Watch", "alert": "Alert", "critical": "Urgent"}

DEFAULT_THRESHOLDS = {"watch": 0.35, "alert": 0.55, "critical": 0.80}
PLACE_WEIGHT_CAP = 1.5
# Most repeats of an unacknowledged critical alert (push_alerts escalation worker).
CRITICAL_MAX_REPEATS = 3

# Rule ids (theft_detection_service), repeated here to keep this module import-light.
RULE_CONCEALMENT = "CONCEALMENT"
RULE_SHELF_SWEEPING = "SHELF_SWEEPING"
RULE_SUSPICIOUS_LOITERING = "SUSPICIOUS_LOITERING"
RULE_EXIT_WITHOUT_CHECKOUT = "EXIT_WITHOUT_CHECKOUT"
RULE_SWEETHEARTING = "SWEETHEARTING"
RULE_BEHAVIOUR_PATTERN = "BEHAVIOUR_PATTERN"
CONCEAL_BEHIND_BACK = "behind_back"
EXIT_FLOOR_CATEGORIES = ("EXIT", "ENTRANCE")

RULE_LABELS = {
    RULE_CONCEALMENT: "Possible concealment",
    RULE_SHELF_SWEEPING: "Possible shelf sweeping",
    RULE_SUSPICIOUS_LOITERING: "Loitering at high-value products",
    RULE_EXIT_WITHOUT_CHECKOUT: "Exit without passing checkout",
    RULE_SWEETHEARTING: "Checkout pass without POS scan",
    RULE_BEHAVIOUR_PATTERN: "Suspicious behaviour pattern",
}

# (case id, site setting, rule, label)
CASES: Tuple[Tuple[str, str, str, str], ...] = (
    ("concealment", "THEFT_CASE_WEIGHT_CONCEALMENT", RULE_CONCEALMENT,
     "Hiding an item in a pocket or inside a jacket"),
    ("concealment_behind_back", "THEFT_CASE_WEIGHT_CONCEAL_BEHIND_BACK", RULE_CONCEALMENT,
     "Hand hidden behind the back or into a worn bag"),
    ("shelf_sweeping", "THEFT_CASE_WEIGHT_SHELF_SWEEPING", RULE_SHELF_SWEEPING,
     "Shelf sweeping (many quick grabs from one shelf)"),
    ("exit_without_checkout", "THEFT_CASE_WEIGHT_EXIT_WITHOUT_CHECKOUT", RULE_EXIT_WITHOUT_CHECKOUT,
     "Leaving without passing a checkout"),
    ("loitering", "THEFT_CASE_WEIGHT_LOITERING", RULE_SUSPICIOUS_LOITERING,
     "Loitering at high-value stock"),
    ("pattern", "THEFT_CASE_WEIGHT_PATTERN", RULE_BEHAVIOUR_PATTERN,
     "Combination of weaker signs"),
)
CASE_BY_ID = {c[0]: c for c in CASES}

# (place id, site setting, label)
PLACES: Tuple[Tuple[str, str, str], ...] = (
    ("high_value", "THEFT_PLACE_WEIGHT_HIGH_VALUE", "High-value products"),
    ("low_value", "THEFT_PLACE_WEIGHT_LOW_VALUE", "Low-value products"),
    ("exit_zone", "THEFT_PLACE_WEIGHT_EXIT_ZONE", "At an exit or entrance area"),
    ("door_camera", "THEFT_PLACE_WEIGHT_DOOR_CAMERA", "Door camera (Entrance / Exit role)"),
    ("checkout_camera", "THEFT_PLACE_WEIGHT_CHECKOUT_CAMERA", "Checkout camera"),
)


# --------------------------------------------------------------------------- helpers

def _f(key: str, default: float) -> float:
    try:
        v = float(getattr(settings, key))
    except (AttributeError, TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _clamp01(v: float) -> float:
    if v is None or not math.isfinite(v):
        return 0.0
    return max(0.0, min(1.0, float(v)))


def _pct(v: float) -> str:
    return f"{v * 100:.0f}%"


def normalise_tier(value: Any) -> Optional[str]:
    t = str(value or "").strip().lower()
    return t if t in TIERS else None


def tier_index(tier: Optional[str]) -> int:
    return TIERS.index(tier) if tier in TIERS else -1


def bump(tier: str, steps: int = 1) -> str:
    return TIERS[max(0, min(len(TIERS) - 1, tier_index(tier) + int(steps)))]


def max_tier(a: Optional[str], b: Optional[str]) -> Optional[str]:
    return a if tier_index(a) >= tier_index(b) else b


def thresholds() -> Dict[str, float]:
    """Risk at which each tier starts. Out-of-order values fall back to the defaults."""
    w = _f("THEFT_TIER_WATCH_MIN", DEFAULT_THRESHOLDS["watch"])
    a = _f("THEFT_TIER_ALERT_MIN", DEFAULT_THRESHOLDS["alert"])
    c = _f("THEFT_TIER_CRITICAL_MIN", DEFAULT_THRESHOLDS["critical"])
    if not 0.0 < w < a < c <= 1.0:
        logger.warning("Theft tier thresholds out of order (%s, %s, %s); using the defaults", w, a, c)
        return dict(DEFAULT_THRESHOLDS)
    return {"watch": w, "alert": a, "critical": c}


def tier_for_score(score: float, thr: Optional[Dict[str, float]] = None) -> str:
    thr = thr or thresholds()
    s = _clamp01(score)
    if s >= thr["critical"] - 1e-9:
        return "critical"
    if s >= thr["alert"] - 1e-9:
        return "alert"
    if s >= thr["watch"] - 1e-9:
        return "watch"
    return "review"


def routing() -> Dict[str, Dict[str, Any]]:
    """Channels per tier: queue (review list), banner, sound, push, escalate, repeat."""
    alert_push = bool(getattr(settings, "THEFT_ROUTE_ALERT_PUSH", True))
    return {
        "review": {"queue": True, "banner": False, "sound": False, "push": False, "escalate": False,
                   "repeat": False, "editable": []},
        "watch": {"queue": True, "banner": True, "sound": bool(getattr(settings, "THEFT_ROUTE_WATCH_SOUND", False)),
                  "push": bool(getattr(settings, "THEFT_ROUTE_WATCH_PUSH", False)), "escalate": False,
                  "repeat": False, "editable": ["THEFT_ROUTE_WATCH_SOUND", "THEFT_ROUTE_WATCH_PUSH"]},
        "alert": {"queue": True, "banner": True, "sound": True, "push": alert_push,
                  "escalate": alert_push and bool(getattr(settings, "THEFT_ROUTE_ALERT_ESCALATE", False)),
                  "repeat": False, "editable": ["THEFT_ROUTE_ALERT_PUSH", "THEFT_ROUTE_ALERT_ESCALATE"]},
        "critical": {"queue": True, "banner": True, "sound": True, "push": True, "escalate": True,
                     "repeat": True, "editable": []},
    }


def channels(tier: Optional[str]) -> Dict[str, bool]:
    """The channels of one tier (no ``editable``); review's for an unknown tier."""
    r = routing().get(normalise_tier(tier) or "review")
    return {k: v for k, v in r.items() if k != "editable"}


def case_for(rule: str, target: Optional[str] = None) -> Dict[str, Any]:
    """Case id, setting, label and current weight of one rule (and concealment target)."""
    rule = str(rule or "").upper()
    if rule == RULE_CONCEALMENT:
        cid = "concealment_behind_back" if target == CONCEAL_BEHIND_BACK else "concealment"
    else:
        cid = next((c[0] for c in CASES if c[2] == rule and c[0] != "concealment_behind_back"), None)
    if cid is None:
        return {"case": rule.lower() or "unknown", "setting": None, "weight": 1.0,
                "label": RULE_LABELS.get(rule, rule.replace("_", " ").capitalize() or "Unknown rule")}
    _cid, key, _rule, label = CASE_BY_ID[cid]
    return {"case": cid, "setting": key, "weight": _f(key, 1.0), "label": label}


def _high_value_reason(zone: Dict[str, Any], role: Optional[str]) -> str:
    name = zone.get("name") or zone.get("id") or "this product area"
    cat = str(zone.get("category") or "").upper()
    cats = {p.strip().upper() for p in str(getattr(settings, "THEFT_HIGH_VALUE_CATEGORIES", "") or "").split(",")
            if p.strip()}
    min_price = _f("THEFT_HIGH_VALUE_MIN_PRICE", 0.0)
    price = float(zone.get("price") or 0.0)
    if cat and cat in cats:
        return f"'{name}' holds {cat}, a high-value category"
    if min_price > 0 and price >= min_price:
        return f"'{name}' is priced {price:g}, at or above the high-value price {min_price:g}"
    if str(zone.get("value_tier") or "").upper() == "PREMIUM":
        return f"'{name}' is marked Premium value"
    try:
        from app.services.camera_roles import preset

        p = preset(role)
        if p is not None and p.all_zones_high_value:
            return f"'{name}' is on a {p.label} camera, where every product area counts as high value"
    except Exception:  # noqa: BLE001
        pass
    return f"'{name}' counts as high value"


def _product_place(zone: Optional[Dict[str, Any]], role: Optional[str],
                   picked_from: bool = False) -> Optional[Tuple[str, float, str]]:
    if not zone:
        return None
    name = zone.get("name") or zone.get("id") or "this product area"
    lead = f"Picked up from '{name}' before leaving: " if picked_from else ""
    tier = str(zone.get("value_tier") or "").upper()
    if zone.get("high_value") or tier == "PREMIUM":
        return "place_high_value", _f("THEFT_PLACE_WEIGHT_HIGH_VALUE", 1.25), lead + _high_value_reason(zone, role)
    if tier == "LOW":
        return "place_low_value", _f("THEFT_PLACE_WEIGHT_LOW_VALUE", 0.85), lead + f"'{name}' holds low-value products"
    return "place_standard_value", 1.0, lead + f"'{name}' holds standard-value products"


def _best_zone(zones: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The highest-valued of the product areas a person reached into."""
    def rank(z: Dict[str, Any]) -> Tuple[int, float]:
        tier = str(z.get("value_tier") or "").upper()
        level = 2 if (z.get("high_value") or tier == "PREMIUM") else (0 if tier == "LOW" else 1)
        return level, float(z.get("price") or 0.0)
    zones = [z for z in zones or [] if z]
    return max(zones, key=rank) if zones else None


# --------------------------------------------------------------------------- scoring

def assess(rule: str, confidence: float, *, target: Optional[str] = None,
           zone: Optional[Dict[str, Any]] = None, reached_zones: Optional[Sequence[Dict[str, Any]]] = None,
           role: Optional[str] = None, floor_category: Optional[str] = None,
           other_rules: Optional[Iterable[Tuple[str, float]]] = None,
           zone_burst: Optional[int] = None, zone_label: Optional[str] = None) -> Dict[str, Any]:
    """Risk score, tier, severity, channels and the factors behind them.

    ``zone`` is the product area of the incident (``{id, name, category,
    price, high_value, value_tier}``); for an exit without checkout, where the
    incident's zone is the floor exit, ``reached_zones`` are the product
    areas the person reached into and the most valuable one is used.
    ``floor_category`` is the floor zone the person was in (EXIT/ENTRANCE
    adds the exit weight). ``other_rules`` are ``(rule, seconds ago)`` of
    earlier incidents on the same track; ``zone_burst`` the number of
    incidents (this one included) in the incident's zone within the burst
    window.
    """
    rule = str(rule or "").upper()
    thr = thresholds()
    factors: List[Dict[str, Any]] = []
    conf = _clamp01(float(confidence or 0.0))
    factors.append({"factor": "confidence", "value": round(conf, 3), "effect": "base",
                    "reason": (f"Evidence confidence {_pct(conf)}: how clearly the camera saw it (body-point "
                               "visibility, how long it lasted, how many signs agreed)")})

    case = case_for(rule, target)
    factors.append({"factor": "case", "value": round(case["weight"], 3), "effect": f"x{case['weight']:.2f}",
                    "reason": f"{case['label']}: counts x{case['weight']:.2f}", "setting": case["setting"]})

    # ---- place
    parts: List[Tuple[str, float, str, Optional[str]]] = []
    if rule == RULE_EXIT_WITHOUT_CHECKOUT:
        prod = _product_place(_best_zone(reached_zones or ([zone] if zone else [])), role, picked_from=True)
    else:
        prod = _product_place(zone, role)
    if prod is not None:
        setting = {"place_high_value": "THEFT_PLACE_WEIGHT_HIGH_VALUE",
                   "place_low_value": "THEFT_PLACE_WEIGHT_LOW_VALUE"}.get(prod[0])
        parts.append((prod[0], prod[1], prod[2], setting))
    door: Optional[Tuple[str, float, str, Optional[str]]] = None
    fcat = str(floor_category or "").upper()
    if fcat in EXIT_FLOOR_CATEGORIES and rule != RULE_EXIT_WITHOUT_CHECKOUT:
        door = ("place_exit_zone", _f("THEFT_PLACE_WEIGHT_EXIT_ZONE", 1.25),
                f"Happened in an {fcat.lower()} area of the floor plan", "THEFT_PLACE_WEIGHT_EXIT_ZONE")
    role_label = None
    try:
        from app.services.camera_roles import place_weight as role_place_weight, preset

        key, w = role_place_weight(role)
        p = preset(role)
        role_label = p.label if p else None
    except Exception:  # noqa: BLE001
        key, w = None, None
    if key == "THEFT_PLACE_WEIGHT_DOOR_CAMERA" and w is not None and rule != RULE_EXIT_WITHOUT_CHECKOUT:
        cand = ("place_door_camera", w, f"Seen on a {role_label} camera, at the store's door", key)
        if door is None or cand[1] > door[1]:
            door = cand
    elif key == "THEFT_PLACE_WEIGHT_CHECKOUT_CAMERA" and w is not None:
        parts.append(("place_checkout_camera", w, f"Seen on a {role_label} camera (impulse racks at the lane)",
                      key))
    if door is not None:
        parts.append(door)
    place = 1.0
    for name, w, reason, setting in parts:
        place *= w
        factors.append({"factor": name, "value": round(w, 3), "effect": f"x{w:.2f}", "reason": reason,
                        "setting": setting})
    if place > PLACE_WEIGHT_CAP:
        factors.append({"factor": "place_cap", "value": PLACE_WEIGHT_CAP, "effect": f"place x{PLACE_WEIGHT_CAP:.2f}",
                        "reason": (f"Place weights multiply to x{place:.2f}; capped at x{PLACE_WEIGHT_CAP:.2f} "
                                   "so one place cannot outweigh the evidence")})
        place = PLACE_WEIGHT_CAP

    risk = round(_clamp01(conf * place * case["weight"]), 3)
    tier = tier_for_score(risk, thr)
    if tier == "review":
        why = f"Risk score {_pct(risk)} is below the Watch level ({_pct(thr['watch'])})"
    else:
        why = f"Risk score {_pct(risk)} reaches the {TIER_LABELS[tier]} level ({_pct(thr[tier])})"
    factors.append({"factor": "level_from_score", "value": tier, "effect": TIER_LABELS[tier], "reason": why})

    # ---- combinations on the same person
    window = _f("THEFT_COMBO_WINDOW_SEC", 300.0)
    recent = [(str(r).upper(), float(ago)) for r, ago in (other_rules or [])
              if str(r).upper() != rule and 0.0 <= float(ago) <= window]
    conceal = [ago for r, ago in recent if r == RULE_CONCEALMENT]
    if rule == RULE_EXIT_WITHOUT_CHECKOUT and conceal:
        before = tier
        tier = "critical"
        factors.append({"factor": "combo_conceal_then_exit", "value": "critical", "effect": "Critical",
                        "reason": (f"The same person was seen hiding an item {min(conceal):.0f}s earlier and "
                                   f"then left without passing a checkout (was {TIER_LABELS[before]})")})
    elif recent:
        before = tier
        tier = bump(tier, 1)
        names = sorted({RULE_LABELS.get(r, r) for r, _ in recent})
        factors.append({"factor": "combo_two_signs", "value": 1, "effect": "+1 level",
                        "reason": (f"The same person also showed: {', '.join(names)} within the last "
                                   f"{max(a for _, a in recent):.0f}s ({TIER_LABELS[before]} -> "
                                   f"{TIER_LABELS[tier]})")})

    burst_min = int(_f("THEFT_BURST_MIN_INCIDENTS", 3))
    if zone_burst is not None and zone_burst >= max(2, burst_min):
        before = tier
        tier = bump(tier, 1)
        where = f"'{zone_label}'" if zone_label else "this area"
        factors.append({"factor": "zone_burst", "value": int(zone_burst), "effect": "+1 level",
                        "reason": (f"{int(zone_burst)} incidents at {where} within "
                                   f"{_f('THEFT_BURST_WINDOW_MIN', 30.0):.0f} min (repeated from {burst_min}) "
                                   f"({TIER_LABELS[before]} -> {TIER_LABELS[tier]})")})

    # ---- camera role floor
    floor = None
    try:
        from app.services.camera_roles import min_alert_tier

        floor = min_alert_tier(role)
    except Exception:  # noqa: BLE001
        floor = None
    if floor and tier_index(tier) < tier_index(floor):
        if tier == "review":
            factors.append({"factor": "role_floor_not_applied", "value": floor, "effect": "none",
                            "reason": (f"A {role_label or role} camera raises incidents to at least "
                                       f"{TIER_LABELS[floor]}, but this one's evidence is below the Watch "
                                       "level, so it stays in review")})
        else:
            before = tier
            tier = floor
            factors.append({"factor": "role_floor", "value": floor, "effect": TIER_LABELS[floor],
                            "reason": (f"A {role_label or role} camera raises incidents to at least "
                                       f"{TIER_LABELS[floor]} (was {TIER_LABELS[before]})")})

    return {
        "alert_tier": tier,
        "risk_score": risk,
        "risk_factors": factors,
        "severity": TIER_SEVERITY[tier],
        "event_severity": TIER_EVENT_SEVERITY[tier],
        "channels": channels(tier),
    }


# --------------------------------------------------------------------------- policy (API)

def examples() -> List[Dict[str, Any]]:
    """Worked examples with the current settings (illustrations, not recorded incidents)."""
    aisle = {"id": "example_aisle", "name": "Biscuits", "category": "SNACKS", "price": 3.0,
             "high_value": False, "value_tier": "STANDARD"}
    spirits = {"id": "example_spirits", "name": "Spirits", "category": "SPIRITS", "price": 45.0,
               "high_value": True, "value_tier": "PREMIUM"}
    cases = [
        ("Pocket concealment, 45% confidence, normal aisle", RULE_CONCEALMENT, 0.45, {"target": "pocket", "zone": aisle}),
        ("Pocket concealment, 45% confidence, spirits", RULE_CONCEALMENT, 0.45, {"target": "pocket", "zone": spirits}),
        ("Pocket concealment, 45% confidence, normal aisle, at the exit area", RULE_CONCEALMENT, 0.45,
         {"target": "pocket", "zone": aisle, "floor_category": "EXIT"}),
        ("Hand behind the back, 45% confidence, spirits", RULE_CONCEALMENT, 0.45,
         {"target": CONCEAL_BEHIND_BACK, "zone": spirits}),
        ("Shelf sweeping, 60% confidence, normal aisle", RULE_SHELF_SWEEPING, 0.60, {"zone": aisle}),
        ("Loitering at spirits, 60% confidence", RULE_SUSPICIOUS_LOITERING, 0.60, {"zone": spirits}),
        ("Weaker signs combined, 50% confidence, normal aisle", RULE_BEHAVIOUR_PATTERN, 0.50, {"zone": aisle}),
        ("Exit without checkout, 50% confidence, picked up snacks", RULE_EXIT_WITHOUT_CHECKOUT, 0.50,
         {"reached_zones": [aisle]}),
        ("Concealment then exit without checkout (same person, 40 s apart)", RULE_EXIT_WITHOUT_CHECKOUT, 0.50,
         {"reached_zones": [aisle], "other_rules": [(RULE_CONCEALMENT, 40.0)]}),
    ]
    out = []
    for label, rule, conf, kw in cases:
        a = assess(rule, conf, **kw)
        out.append({"label": label, "rule": rule, "confidence": conf, "risk_score": a["risk_score"],
                    "alert_tier": a["alert_tier"], "risk_factors": a["risk_factors"]})
    return out


def policy() -> Dict[str, Any]:
    """Everything the dashboard needs to show and explain the alert levels."""
    thr = thresholds()
    route = routing()
    bounds = [thr["watch"], thr["alert"], thr["critical"], 1.0]
    tiers = []
    for i, t in enumerate(TIERS):
        lo = 0.0 if i == 0 else bounds[i - 1]
        hi = bounds[i]
        tiers.append({"id": t, "label": TIER_LABELS[t], "description": TIER_HELP[t],
                      "min_risk": lo, "max_risk": hi if t != "critical" else 1.0,
                      "severity": TIER_SEVERITY[t], "event_severity": TIER_EVENT_SEVERITY[t],
                      "channels": {k: v for k, v in route[t].items() if k != "editable"},
                      "editable_settings": list(route[t]["editable"])})
    roles = []
    try:
        from app.services import camera_roles as cr

        for rid, p in cr.ROLE_PRESETS.items():
            key, w = cr.place_weight(rid)
            roles.append({"role": rid, "label": p.label, "place_weight_setting": key, "place_weight": w,
                          "min_tier": cr.min_alert_tier(rid), "theft_sensitivity": cr.theft_sensitivity(rid),
                          "min_confidence": round(cr.scaled_theft_thresholds(cr.theft_sensitivity(rid))
                                                  ["min_confidence"], 3)})
    except Exception as exc:  # noqa: BLE001
        logger.debug("camera roles unavailable for the alert policy: %s", exc)
    phone: Dict[str, Any] = {}
    try:
        from app.services.push_alerts import load_roster

        r = load_roster()
        phone = {"configured": bool(r.get("configured")), "escalate_after_min": r.get("escalate_after_min"),
                 "backup_people": len(r.get("backup") or []), "first_priority_people": len(r.get("first_priority") or []),
                 "min_confidence": r.get("min_confidence"),
                 "min_confidence_applies_to": ("alerts without a level only (e.g. older clients); theft "
                                               "incidents follow their level")}
    except Exception as exc:  # noqa: BLE001
        logger.debug("phone roster unavailable for the alert policy: %s", exc)
    try:
        from app.services.site_settings import GROUP_KEYS

        keys = list(GROUP_KEYS.get("theft_alerts") or [])
    except Exception:  # noqa: BLE001
        keys = []
    return {
        "formula": ("risk = confidence x place weight x case weight (place weights multiply, capped at "
                    f"x{PLACE_WEIGHT_CAP:g}); the level comes from the thresholds, then combined signs, "
                    "repeated incidents and the camera role can raise it"),
        "tiers": tiers,
        "thresholds": thr,
        "min_confidence": _f("THEFT_MIN_CONFIDENCE", 0.3),
        "min_confidence_note": "Below this confidence a possible theft is not raised at all (Alerts and detection).",
        "case_weights": [{"case": cid, "setting": key, "rule": rule, "label": label, "weight": _f(key, 1.0)}
                         for cid, key, rule, label in CASES],
        "place_weights": [{"place": pid, "setting": key, "label": label, "weight": _f(key, 1.0)}
                          for pid, key, label in PLACES],
        "place_weight_cap": PLACE_WEIGHT_CAP,
        "combos": {
            "window_sec": _f("THEFT_COMBO_WINDOW_SEC", 300.0),
            "burst_window_min": _f("THEFT_BURST_WINDOW_MIN", 30.0),
            "burst_min_incidents": int(_f("THEFT_BURST_MIN_INCIDENTS", 3)),
            "rules": [
                "Concealment then exit without checkout by the same person within the window: Critical",
                "Two different signs from the same person within the window: one level higher",
                "Repeated incidents in the same area within the burst window: one level higher",
                "A High-value camera raises incidents that reach Watch to at least Alert",
            ],
            "critical_max_repeats": CRITICAL_MAX_REPEATS,
        },
        "roles": roles,
        "phone_alerts": phone,
        "settings_group": "theft_alerts",
        "settings_keys": keys,
        "examples": examples(),
    }
