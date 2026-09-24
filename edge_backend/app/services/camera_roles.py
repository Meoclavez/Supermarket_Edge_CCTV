"""Camera roles: what each camera is for, and what that implies.

``ROLE_PRESETS`` is the single source of truth. A role is a *declaration by
the operator* ("this camera watches the entrance"); it never invents data.
It does three things:

1. **Defaults.** ``PUT /api/v1/cameras/{id}/role`` with ``apply_defaults``
   writes the preset's analytics flags and person-size gate onto the camera.
   Later changes by the operator are kept until defaults are applied again.
2. **Setup checklist.** Each role lists the configuration its analytics need
   (``required_setup``) or benefit from (``optional_setup``). Whether an item
   is done is computed from real state: the stored homography, tripwires /
   restricted areas / masks / queue areas in ``ai_zone_service``, product
   zones in ``shelf_interaction_service``, blueprint zones, and
   ``pos_transactions`` rows for a linked register.
3. **Role-aware behaviour** (a camera with no role behaves exactly as before):

   * footfall prefers counting lines on door cameras (``entrance``,
     ``entrance_exit``, and ``exit``, whose "in" crossings are real entries)
     over lines elsewhere (``tripwire_engine.tripwire_entries``);
   * ``stockroom`` cameras are never customer footfall (tripwire, zone-visit
     and track fallbacks all exclude them);
   * theft-rule thresholds scale by ``theft_sensitivity``
     (``scaled_theft_thresholds``); on a ``high_value`` camera every product
     zone counts as high value and incidents are raised at least HIGH;
   * ``EXIT_WITHOUT_CHECKOUT`` is a single-camera rule (there is no
     cross-camera re-identification), so it is suppressed on cameras that by
     their role cannot see a checkout when the store has dedicated checkout
     cameras (``exit_rule_allowed``);
   * checkout / queue areas drawn in Studio on a lane camera feed queue
     metrics, and a lane's ``pos_register_id`` attributes POS rows to it.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.config import settings

logger = logging.getLogger(__name__)


# =========================================================================
# Presets
# =========================================================================

SETUP_ITEM_IDS = (
    "calibrate",
    "entrance_tripwire",
    "exit_tripwire",
    "checkout_zone",
    "queue_zone",
    "product_zones",
    "restricted_area",
    "privacy_mask",
    "pos_register_link",
)

FEATURE_KEYS = ("people_counting", "shelf_interaction", "theft_detection")


@dataclass(frozen=True)
class RolePreset:
    id: str
    label: str
    description: str
    mounting_tip: str
    features: Dict[str, bool]
    theft_sensitivity: float = 1.0
    person_max_frame_fraction: Optional[float] = None
    counts_footfall: bool = True
    # Counting lines on this role are the preferred footfall source.
    primary_footfall: bool = False
    required_setup: Tuple[str, ...] = ()
    optional_setup: Tuple[str, ...] = ()
    # Minimum severity of theft incidents raised on this camera (None = by confidence).
    alert_severity_floor: Optional[str] = None
    # Every product zone on this camera is treated as high value (loitering rule).
    all_zones_high_value: bool = False
    # What the role enables, one short phrase each (for pickers and docs).
    analytics: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["required_setup"] = list(self.required_setup)
        d["optional_setup"] = list(self.optional_setup)
        d["analytics"] = list(self.analytics)
        return d


def _f(pc: bool, si: bool, td: bool) -> Dict[str, bool]:
    return {"people_counting": pc, "shelf_interaction": si, "theft_detection": td}


ROLE_PRESETS: Dict[str, RolePreset] = {
    "entrance": RolePreset(
        id="entrance", label="Entrance",
        description="Counts shoppers coming in; the store's primary footfall source.",
        mounting_tip=("Mount above or just inside the door, looking along the walkway so every shopper "
                      "crosses the frame, then draw a counting line across the doorway."),
        features=_f(True, False, False),
        primary_footfall=True,
        required_setup=("entrance_tripwire",),
        optional_setup=("calibrate", "privacy_mask"),
        analytics=("Footfall in", "Hourly traffic", "Occupancy (with exit counts)"),
    ),
    "exit": RolePreset(
        id="exit", label="Exit",
        description="Counts shoppers leaving; can flag exits without a checkout if it also sees the lanes.",
        mounting_tip=("Mount facing the exit gates so people walk towards or away from the camera, and "
                      "draw a counting line across the exit."),
        features=_f(True, False, True),
        # Its line's "in" crossings are real entries (people walking in the
        # exit), so door lines of every kind are the preferred footfall source.
        primary_footfall=True,
        required_setup=("exit_tripwire",),
        optional_setup=("calibrate", "privacy_mask"),
        analytics=("Footfall out", "Occupancy (with entrance counts)", "Exit without checkout (same-camera)"),
    ),
    "entrance_exit": RolePreset(
        id="entrance_exit", label="Entrance / exit door",
        description="One door used both ways: counts in and out on one line.",
        mounting_tip=("Mount above the door looking down the walkway and draw one counting line across it; "
                      "the arrow marks the inside."),
        features=_f(True, False, True),
        primary_footfall=True,
        required_setup=("entrance_tripwire",),
        optional_setup=("calibrate", "privacy_mask"),
        analytics=("Footfall in and out", "Occupancy", "Hourly traffic"),
    ),
    "checkout": RolePreset(
        id="checkout", label="Checkout / cashier",
        description=("Queue length and wait, lane use, POS-linked lane figures, and theft cues at the "
                     "lane's impulse racks."),
        mounting_tip=("Mount above or behind the lane looking at the customer side and the queue. A close "
                      "mount is fine: people may fill most of the frame."),
        features=_f(True, True, True),
        theft_sensitivity=1.25,
        person_max_frame_fraction=0.6,
        required_setup=("checkout_zone",),
        optional_setup=("queue_zone", "pos_register_link", "product_zones", "calibrate", "privacy_mask"),
        analytics=("Queue length now", "Time at the lane / wait", "Lane sales per customer (POS)",
                   "Concealment at impulse racks"),
    ),
    "aisle": RolePreset(
        id="aisle", label="Product aisle / shelves",
        description="Shelf reaches, dwell, concealment and shelf sweeping.",
        mounting_tip=("Mount across the aisle facing the shelves so hands and shelf faces are visible; "
                      "draw a product area over each shelf section."),
        features=_f(True, True, True),
        person_max_frame_fraction=0.6,
        required_setup=("product_zones",),
        optional_setup=("calibrate", "privacy_mask"),
        analytics=("Shelf interactions", "Product engagement", "Concealment", "Shelf sweeping"),
    ),
    "high_value": RolePreset(
        id="high_value", label="High-value area",
        description=("Liquor, cosmetics, electronics, pharmacy: theft rules are more sensitive and "
                     "alerts are raised HIGH."),
        mounting_tip=("Mount close, facing the display, so hands and pockets are clearly visible; draw a "
                      "product area over each high-value section."),
        features=_f(True, True, True),
        theft_sensitivity=1.5,
        person_max_frame_fraction=0.6,
        required_setup=("product_zones",),
        optional_setup=("calibrate", "privacy_mask"),
        alert_severity_floor="HIGH",
        all_zones_high_value=True,
        analytics=("Shelf interactions", "Concealment", "Shelf sweeping", "Loitering at high-value stock"),
    ),
    "stockroom": RolePreset(
        id="stockroom", label="Stockroom / staff only",
        description="Scheduled restricted-area alerts; never counted as customer footfall.",
        mounting_tip=("Mount to cover the doorway and floor of the room, then draw a restricted area with "
                      "the hours it must be empty (e.g. after closing)."),
        # people_counting stays on: a camera with every analysis flag off is
        # not tracked at all, and restricted areas run on its tracks.
        features=_f(True, False, False),
        counts_footfall=False,
        required_setup=("restricted_area",),
        optional_setup=("privacy_mask",),
        analytics=("After-hours / restricted-area alerts",),
    ),
    "overview": RolePreset(
        id="overview", label="General overview",
        description="Presence and dwell heatmaps and occupancy across the floor.",
        mounting_tip=("Mount high with a wide view of the floor and calibrate it, so people are placed on "
                      "the floor plan."),
        features=_f(True, False, False),
        required_setup=("calibrate",),
        optional_setup=("privacy_mask",),
        analytics=("Presence heatmap", "Dwell heatmap", "Zone occupancy"),
    ),
}

ROLE_IDS = tuple(ROLE_PRESETS)
PRIMARY_FOOTFALL_ROLES = tuple(r for r, p in ROLE_PRESETS.items() if p.primary_footfall)
NON_FOOTFALL_ROLES = tuple(r for r, p in ROLE_PRESETS.items() if not p.counts_footfall)
# Roles that, by declaration, look at products rather than lanes or doors.
PRODUCT_AREA_ROLES = ("aisle", "high_value")

SENSITIVITY_MIN, SENSITIVITY_MAX = 0.5, 2.0


def preset(role: Optional[str]) -> Optional[RolePreset]:
    return ROLE_PRESETS.get(role) if role else None


def normalise_role(value: Any) -> Optional[str]:
    """Lower-cased role id, None for empty; ValueError for an unknown role."""
    if value is None:
        return None
    role = str(value).strip().lower()
    if not role:
        return None
    if role not in ROLE_PRESETS:
        raise ValueError(f"Unknown camera role '{value}'. Use one of: {', '.join(ROLE_IDS)}")
    return role


def presets_list() -> List[dict]:
    return [p.to_dict() for p in ROLE_PRESETS.values()]


def default_features(role: str) -> Dict[str, Any]:
    """The feature-config values a preset writes when defaults are applied."""
    p = ROLE_PRESETS[role]
    out: Dict[str, Any] = dict(p.features)
    out["person_max_frame_fraction"] = p.person_max_frame_fraction
    return out


# =========================================================================
# Theft thresholds
# =========================================================================

def theft_sensitivity(role: Optional[str]) -> float:
    p = preset(role)
    s = p.theft_sensitivity if p else 1.0
    return min(SENSITIVITY_MAX, max(SENSITIVITY_MIN, float(s)))


def scaled_theft_thresholds(sensitivity: float = 1.0) -> Dict[str, Any]:
    """Theft-rule gates for a camera, scaled by ``sensitivity`` (1.0 = config defaults).

    Higher sensitivity lowers the evidence a rule needs: the minimum
    confidence, reach counts, dwell and head turns are divided by it (counts
    rounded, never below a sane floor). Windows are not stretched, so a rule
    still needs its behaviour to happen within the same time.
    """
    s = min(SENSITIVITY_MAX, max(SENSITIVITY_MIN, float(sensitivity or 1.0)))
    return {
        "sensitivity": s,
        "min_confidence": min(0.95, float(settings.THEFT_MIN_CONFIDENCE) / s),
        "sweep_min_reaches": max(2, int(round(settings.THEFT_SWEEP_MIN_REACHES / s))),
        "conceal_min_hold_frames": max(1, int(round(settings.THEFT_CONCEAL_MIN_HOLD_FRAMES / s))),
        "loiter_min_dwell_sec": float(settings.THEFT_LOITER_MIN_DWELL_SEC) / s,
        "loiter_min_reaches": max(1, int(round(settings.THEFT_LOITER_MIN_REACHES / s))),
        "loiter_min_head_turns": max(1, int(round(settings.THEFT_LOITER_MIN_HEAD_TURNS / s))),
    }


def exit_rule_allowed(role: Optional[str], store_roles: Iterable[Optional[str]]) -> bool:
    """Whether EXIT_WITHOUT_CHECKOUT may be evaluated on a camera with ``role``.

    The rule only sees one camera's track. A product-area camera (aisle,
    high-value) in a store whose checkouts are covered by dedicated checkout
    cameras never sees the checkout visit, so it would flag every paying
    shopper whose projected path reached an EXIT/ENTRANCE zone. Stockroom
    cameras watch staff. No role = unchanged behaviour.
    """
    if role is None:
        return True
    if role == "stockroom":
        return False
    if role in PRODUCT_AREA_ROLES:
        return "checkout" not in set(store_roles)
    return True


# =========================================================================
# Role cache (pipeline threads)
# =========================================================================

class RoleCache:
    """camera_id -> role, read from the cameras table at most every TTL seconds.

    Pipeline threads call :meth:`role_of` per frame; the lookup is a dict read
    and a refresh is one small SELECT. An unreadable table (unmigrated DB)
    means "no roles", i.e. the pre-roles behaviour.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._roles: Dict[str, Optional[str]] = {}
        self._loaded_at = 0.0
        self._override: Optional[Dict[str, Optional[str]]] = None

    def set_override(self, roles: Optional[Dict[str, Optional[str]]]) -> None:
        """Tests: fix the role map (None returns to reading the database)."""
        with self._lock:
            self._override = dict(roles) if roles is not None else None

    def invalidate(self) -> None:
        with self._lock:
            self._loaded_at = 0.0

    def _load(self) -> Dict[str, Optional[str]]:
        try:
            with closing(sqlite3.connect(str(settings.DATABASE_PATH), timeout=2.0)) as conn:
                rows = conn.execute("SELECT id, role FROM cameras").fetchall()
        except sqlite3.Error:
            return {}
        return {str(cid): (r if r in ROLE_PRESETS else None) for cid, r in rows}

    def all(self) -> Dict[str, Optional[str]]:
        with self._lock:
            if self._override is not None:
                return dict(self._override)
            fresh = time.monotonic() - self._loaded_at < float(settings.CAMERA_ROLE_CACHE_TTL_SEC)
            if fresh:
                return dict(self._roles)
        roles = self._load()
        with self._lock:
            self._roles = roles
            self._loaded_at = time.monotonic()
            return dict(roles)

    def role_of(self, camera_id: str) -> Optional[str]:
        return self.all().get(str(camera_id))


role_cache = RoleCache()


async def camera_roles_map(db) -> Dict[str, Optional[str]]:
    """camera_id -> role from the request's session (fresh, not cached)."""
    from sqlalchemy import select

    from app.models.db_models import CameraModel

    try:
        rows = (await db.execute(select(CameraModel.id, CameraModel.role))).all()
    except Exception as e:  # unmigrated DB
        logger.debug(f"camera roles unavailable: {e}")
        return {}
    return {cid: (r if r in ROLE_PRESETS else None) for cid, r in rows}


async def non_footfall_camera_ids(db) -> List[str]:
    """Cameras whose role never counts customer footfall (stockroom)."""
    return [cid for cid, r in (await camera_roles_map(db)).items() if r in NON_FOOTFALL_ROLES]


# =========================================================================
# Applying a role
# =========================================================================

def apply_role(cam, role: Optional[str], apply_defaults: bool) -> Dict[str, Any]:
    """Set ``cam.role`` and, when asked, the preset's feature defaults.

    Returns the feature values written (empty when defaults were not
    applied). Keys the preset does not own (none today) are kept.
    """
    from app.models.schemas import CameraFeatureConfig

    cam.role = role
    applied: Dict[str, Any] = {}
    if role and apply_defaults:
        current = dict(cam.features or {})
        applied = default_features(role)
        current.update(applied)
        cam.features = CameraFeatureConfig.model_validate(current).model_dump()
    return applied


# =========================================================================
# Setup checklist
# =========================================================================

ITEM_LABELS = {
    "calibrate": "Calibrate to the floor plan",
    "entrance_tripwire": "Counting line across the entrance",
    "exit_tripwire": "Counting line across the exit",
    "checkout_zone": "Checkout lane area",
    "queue_zone": "Queue (waiting line) area",
    "product_zones": "Product shelf areas",
    "restricted_area": "Restricted area with a schedule",
    "privacy_mask": "Privacy masks",
    "pos_register_link": "Link the lane's POS register",
}

ITEM_ACTIONS = {
    "calibrate": {"type": "calibrate"},
    "entrance_tripwire": {"type": "studio", "tool": "tripwire"},
    "exit_tripwire": {"type": "studio", "tool": "tripwire"},
    "checkout_zone": {"type": "studio", "tool": "checkout", "kind": "checkout"},
    "queue_zone": {"type": "studio", "tool": "checkout", "kind": "queue"},
    "product_zones": {"type": "studio", "tool": "product"},
    "restricted_area": {"type": "studio", "tool": "restricted"},
    "privacy_mask": {"type": "studio", "tool": "mask"},
    "pos_register_link": {"type": "config", "field": "pos_register_id"},
}


@dataclass
class SetupContext:
    """Everything the checklist needs, loaded once per request."""

    overlays: Dict[str, Dict[str, list]]            # camera_id -> tripwires / intrusion_zones / exclusion_masks / queue_zones
    product_zones: Dict[str, list]                  # camera_id -> enabled product zones
    floor_categories: set                           # blueprint zone categories present
    pos_registers: Dict[str, Tuple[int, Optional[datetime]]]   # register_id -> (rows, last timestamp)

    def cam(self, camera_id: str) -> Dict[str, list]:
        return self.overlays.get(camera_id) or {}


async def load_context(db) -> SetupContext:
    from sqlalchemy import func, select

    from app.models.db_models import POSTransactionModel, StoreZoneModel
    from app.services.ai_zone_service import ai_zone_service
    from app.services.shelf_interaction_service import shelf_interaction_service

    overlays: Dict[str, Dict[str, list]] = {}
    try:
        data = ai_zone_service.get_all_zones()
    except Exception:
        data = {}
    for kind in ("tripwires", "intrusion_zones", "exclusion_masks", "queue_zones"):
        for item in data.get(kind, []) or []:
            cid = str(item.get("camera_id", "cam_main"))
            overlays.setdefault(cid, {}).setdefault(kind, []).append(item)

    products: Dict[str, list] = {}
    try:
        for z in shelf_interaction_service.get_zones():
            if getattr(z, "enabled", True):
                products.setdefault(z.camera_id, []).append(z)
    except Exception:
        pass

    try:
        cats = {c for (c,) in (await db.execute(select(StoreZoneModel.category).distinct())).all() if c}
    except Exception:
        cats = set()

    registers: Dict[str, Tuple[int, Optional[datetime]]] = {}
    try:
        rows = (await db.execute(
            select(POSTransactionModel.register_id, func.count(POSTransactionModel.id),
                   func.max(POSTransactionModel.timestamp))
            .group_by(POSTransactionModel.register_id))).all()
        registers = {str(r): (int(n or 0), ts) for r, n, ts in rows if r}
    except Exception:
        pass
    return SetupContext(overlays=overlays, product_zones=products,
                        floor_categories={str(c).upper() for c in cats}, pos_registers=registers)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _counting_lines(ctx: SetupContext, camera_id: str) -> Tuple[list, list]:
    wires = [w for w in ctx.cam(camera_id).get("tripwires", []) if w.get("enabled", True)]
    counting = [w for w in wires if w.get("counts_footfall", True)]
    return wires, counting


def _queue_areas(ctx: SetupContext, camera_id: str, kind: str) -> list:
    return [a for a in ctx.cam(camera_id).get("queue_zones", [])
            if a.get("enabled", True) and str(a.get("kind", "queue")) == kind]


def evaluate_item(item: str, cam, ctx: SetupContext) -> Tuple[bool, str]:
    """(done, hint) for one checklist item on one camera, from real state."""
    cid = cam.id
    calibrated = bool(cam.homography_matrix)
    if item == "calibrate":
        return (calibrated, "Calibrated: people from this camera are placed on the floor plan."
                if calibrated else
                "Pair 4 or more floor points between the camera image and the plan so people are "
                "placed on the floor plan and in heatmaps.")
    if item in ("entrance_tripwire", "exit_tripwire"):
        wires, counting = _counting_lines(ctx, cid)
        if counting:
            names = ", ".join(str(w.get("name") or w.get("id")) for w in counting[:3])
            return True, f"{_plural(len(counting), 'counting line')}: {names}."
        if wires:
            return False, ("A line exists but is not set to count footfall. Edit it in Studio and turn on "
                           "'counts footfall'.")
        where = "entrance" if item == "entrance_tripwire" else "exit"
        return False, (f"Draw a tripwire across the {where} in Studio. The arrow marks the inside, so "
                       "crossings are counted as in or out.")
    if item == "checkout_zone":
        areas = _queue_areas(ctx, cid, "checkout")
        if areas:
            return True, f"{_plural(len(areas), 'checkout area')} drawn on this camera."
        if calibrated and "CHECKOUT" in ctx.floor_categories:
            return True, ("Calibrated, and the blueprint has a CHECKOUT zone. Make sure that zone is in "
                          "this camera's view, or draw a checkout area in Studio.")
        return False, ("Draw a checkout area over the lane in Studio (works without calibration), or "
                       "calibrate this camera and draw a CHECKOUT zone on the blueprint.")
    if item == "queue_zone":
        areas = _queue_areas(ctx, cid, "queue")
        return ((True, f"{_plural(len(areas), 'queue area')} drawn on this camera.") if areas else
                (False, "Draw the area where customers wait in line; its dwell is reported as wait time."))
    if item == "product_zones":
        zones = ctx.product_zones.get(cid) or []
        return ((True, f"{_plural(len(zones), 'product area')} drawn on this camera.") if zones else
                (False, "Draw a product area over each shelf section in Studio so reaches can be counted."))
    if item == "restricted_area":
        areas = [a for a in ctx.cam(cid).get("intrusion_zones", []) if a.get("enabled", True)]
        scheduled = [a for a in areas if a.get("schedule")]
        if scheduled:
            return True, f"{_plural(len(scheduled), 'scheduled restricted area')} on this camera."
        if areas:
            return False, ("The restricted area has no schedule, so it alerts around the clock, including "
                           "on staff during trading hours. Add schedule rows (for example after closing).")
        return False, "Draw a restricted area in Studio and set the hours it must be empty."
    if item == "privacy_mask":
        masks = [m for m in ctx.cam(cid).get("exclusion_masks", []) if m.get("enabled", True)]
        return ((True, f"{_plural(len(masks), 'mask')} on this camera.") if masks else
                (False, "Optional: mask screens, keypads or neighbouring property in Studio."))
    if item == "pos_register_link":
        reg = getattr(cam, "pos_register_id", None)
        if not reg:
            known = sorted(ctx.pos_registers)
            tail = f" Registers seen in POS data: {', '.join(known[:8])}." if known else \
                " No POS data has been received yet."
            return False, "Set the POS register id this lane rings sales on." + tail
        rows, last = ctx.pos_registers.get(str(reg), (0, None))
        if rows:
            when = last.isoformat(timespec="minutes") if isinstance(last, datetime) else "unknown"
            return True, f"Register '{reg}' linked; {_plural(rows, 'POS row')} received (latest {when} UTC)."
        return False, (f"Register '{reg}' is linked, but no POS rows with that register_id have been "
                       "received yet. Check the POS feed (POST /api/v1/analytics/pos/ingest).")
    raise KeyError(item)


def camera_setup(cam, ctx: SetupContext) -> dict:
    role = cam.role if getattr(cam, "role", None) in ROLE_PRESETS else None
    p = preset(role)
    items: List[dict] = []
    if p is not None:
        for item, required in [(i, True) for i in p.required_setup] + [(i, False) for i in p.optional_setup]:
            done, hint = evaluate_item(item, cam, ctx)
            items.append({"id": item, "label": ITEM_LABELS[item], "done": bool(done), "required": required,
                          "hint": hint, "action": dict(ITEM_ACTIONS[item])})
    req = [i for i in items if i["required"]]
    return {
        "camera_id": cam.id,
        "name": cam.name,
        "role": role,
        "role_label": p.label if p else None,
        "pos_register_id": getattr(cam, "pos_register_id", None),
        "items": items,
        "required_done": sum(1 for i in req if i["done"]),
        "required_total": len(req),
        "complete": bool(p) and all(i["done"] for i in req),
        "message": None if p else "No role set. Pick what this camera watches to get a setup checklist.",
    }


# =========================================================================
# Store-level coverage
# =========================================================================

def _status(ok: bool, limited: bool = False) -> str:
    return "available" if ok else ("limited" if limited else "blocked")


def store_setup(cameras: list, ctx: SetupContext) -> dict:
    """Which roles exist, what analytics that makes possible, and a setup score."""
    from app.models.schemas import CameraFeatureConfig
    from app.services.feature_manager import feature_manager

    per_cam = [camera_setup(c, ctx) for c in cameras]
    roles: Dict[str, Optional[str]] = {c.id: s["role"] for c, s in zip(cameras, per_cam)}
    present: Dict[str, int] = {}
    for r in roles.values():
        if r:
            present[r] = present.get(r, 0) + 1

    def flags(c) -> CameraFeatureConfig:
        try:
            return feature_manager.get_camera_features(c.id, stored=c.features)
        except Exception:
            return CameraFeatureConfig()

    def cams(*want: str) -> list:
        return [c for c in cameras if roles.get(c.id) in want]

    def has_line(c) -> bool:
        return bool(_counting_lines(ctx, c.id)[1])

    counting_cams = [c for c in cameras if roles.get(c.id) not in NON_FOOTFALL_ROLES and flags(c).people_counting]
    analytics: List[dict] = []

    # Footfall
    entrance_lines = [c for c in cams("entrance", "entrance_exit") if has_line(c) and flags(c).people_counting]
    other_lines = [c for c in counting_cams if has_line(c)]
    if entrance_lines:
        msg = "Footfall: counted on the entrance counting line(s)."
    elif other_lines:
        msg = ("Footfall: counted on lines of cameras not marked Entrance. Set the door camera's role to "
               "Entrance (or Entrance / exit door) so aisle lines are not taken for entries.")
    elif counting_cams:
        msg = ("Footfall: estimated from tracked people, which over-counts when tracks fragment. Needs an "
               "Entrance camera with a counting line.")
    else:
        msg = "Footfall: needs an Entrance camera with a counting line."
    analytics.append({"id": "footfall", "label": "Footfall",
                      "status": _status(bool(entrance_lines), bool(other_lines or counting_cams)), "message": msg})

    # Occupancy (in minus out)
    door = [c for c in cams("entrance_exit") if has_line(c)]
    ins = [c for c in cams("entrance") if has_line(c)]
    outs = [c for c in cams("exit") if has_line(c)]
    occ_ok = bool(door or (ins and outs))
    analytics.append({
        "id": "occupancy", "label": "Occupancy (in minus out)", "status": _status(occ_ok),
        "message": ("Occupancy: estimated from entries minus exits on the door lines." if occ_ok else
                    "Occupancy: needs an Entrance / exit door camera with a counting line, or Entrance and "
                    "Exit cameras that both have one."),
    })

    # Queues
    lane_cams = [c for c in cams("checkout")
                 if _queue_areas(ctx, c.id, "checkout") or _queue_areas(ctx, c.id, "queue")]
    floor_queue = "CHECKOUT" in ctx.floor_categories and any(c.homography_matrix for c in cameras)
    analytics.append({
        "id": "queue_times", "label": "Queue length and wait",
        "status": _status(bool(lane_cams or floor_queue)),
        "message": ("Queue times: measured on the checkout / queue areas." if lane_cams else
                    "Queue times: measured on the blueprint CHECKOUT zone(s) via calibrated cameras."
                    if floor_queue else
                    "Queue times: needs a Checkout camera with a checkout or queue area."),
    })

    # POS-linked lane figures
    linked = [c for c in cams("checkout") if getattr(c, "pos_register_id", None)]
    fed = [c for c in linked if ctx.pos_registers.get(str(c.pos_register_id), (0, None))[0]]
    analytics.append({
        "id": "lane_pos", "label": "Lane sales per customer (POS)",
        "status": _status(bool(fed), bool(linked)),
        "message": ("Lane POS figures: POS rows are attributed to the linked lanes." if fed else
                    "Lane POS figures: a register is linked but no POS rows for it have been received."
                    if linked else
                    "Lane POS figures: needs a Checkout camera linked to its POS register, and POS data."),
    })

    # Shelf engagement and shelf theft
    shelf_cams = [c for c in cameras if ctx.product_zones.get(c.id)]
    engage = [c for c in shelf_cams if flags(c).shelf_interaction]
    analytics.append({
        "id": "shelf_engagement", "label": "Shelf interactions",
        "status": _status(bool(engage)),
        "message": ("Shelf interactions: measured on the product areas." if engage else
                    "Shelf interactions: needs an Aisle, High-value or Checkout camera with product areas "
                    "and shelf interaction switched on."),
    })
    theft_cams = [c for c in shelf_cams if flags(c).theft_detection]
    analytics.append({
        "id": "shelf_theft", "label": "Concealment and shelf sweeping",
        "status": _status(bool(theft_cams)),
        "message": ("Concealment / sweeping: evaluated on the product areas." if theft_cams else
                    "Concealment / sweeping: needs a camera with product areas and theft detection on."),
    })
    hv = [c for c in theft_cams if roles.get(c.id) == "high_value"]
    analytics.append({
        "id": "high_value_watch", "label": "Loitering at high-value stock",
        "status": _status(bool(hv), bool(theft_cams)),
        "message": ("High-value watch: all product areas on High-value cameras count as high value." if hv else
                    "High-value watch: only product areas whose category or price marks them high value. "
                    "Set a High-value role on the cameras covering liquor, cosmetics or electronics."
                    if theft_cams else
                    "High-value watch: needs a High-value camera with product areas."),
    })

    # Exit without checkout: one camera must see shelves, a checkout and an exit.
    store_roles = list(roles.values())
    floor_ok = "CHECKOUT" in ctx.floor_categories and bool({"EXIT", "ENTRANCE"} & ctx.floor_categories)
    ewc = [c for c in theft_cams if c.homography_matrix and exit_rule_allowed(roles.get(c.id), store_roles)]
    ewc_ok = bool(settings.THEFT_EXIT_RULE_ENABLED and floor_ok and ewc)
    analytics.append({
        "id": "exit_without_checkout", "label": "Exit without checkout",
        "status": _status(ewc_ok),
        "message": ("Exit without checkout: evaluated on calibrated cameras that see the shelves, a CHECKOUT "
                    "zone and an EXIT/ENTRANCE zone. A checkout seen only by another camera is not matched."
                    if ewc_ok else
                    "Exit without checkout: disabled in settings (THEFT_EXIT_RULE_ENABLED)."
                    if not settings.THEFT_EXIT_RULE_ENABLED else
                    "Exit without checkout: needs one calibrated camera that sees product areas, a CHECKOUT "
                    "zone and an EXIT/ENTRANCE zone on the blueprint. There is no cross-camera matching, so "
                    "separate Exit and Checkout cameras cannot be linked into one path."),
    })

    # Restricted areas
    restricted = [c for c in cameras
                  if any(a.get("enabled", True) and a.get("schedule")
                         for a in ctx.cam(c.id).get("intrusion_zones", []))]
    analytics.append({
        "id": "restricted_areas", "label": "Stockroom / after-hours alerts",
        "status": _status(bool(restricted)),
        "message": ("Restricted areas: scheduled alerts are active." if restricted else
                    "Restricted areas: needs a Stockroom camera with a scheduled restricted area."),
    })

    # Floor heatmap
    calibrated = [c for c in cameras if c.homography_matrix and roles.get(c.id) not in NON_FOOTFALL_ROLES]
    analytics.append({
        "id": "floor_heatmap", "label": "Floor heatmap",
        "status": _status(bool(calibrated)),
        "message": ("Floor heatmap: built from the calibrated cameras." if calibrated else
                    "Floor heatmap: needs at least one calibrated camera (an Overview camera is ideal)."),
    })

    # Sweethearting: not evaluated by this build.
    analytics.append({
        "id": "sweethearting", "label": "Sweethearting (scan vs hand passes)",
        "status": "unsupported",
        "message": ("Sweethearting: not evaluated. It needs per-item scan times from the POS and detection "
                    "of items passing the scanner; this build records neither."),
    })

    roled = [s for s in per_cam if s["role"]]
    done = sum(s["required_done"] for s in roled)
    total = sum(s["required_total"] for s in roled)
    return {
        "cameras": [
            {"camera_id": s["camera_id"], "name": s["name"], "role": s["role"], "role_label": s["role_label"],
             "required_done": s["required_done"], "required_total": s["required_total"],
             "complete": s["complete"],
             "missing": [i["label"] for i in s["items"] if i["required"] and not i["done"]]}
            for s in per_cam
        ],
        "roles_present": present,
        "roles_missing": [r for r in ("entrance", "checkout", "aisle") if r not in present
                          and not (r == "entrance" and "entrance_exit" in present)],
        "cameras_without_role": sum(1 for s in per_cam if not s["role"]),
        "analytics": analytics,
        "blocked": [a["message"] for a in analytics if a["status"] == "blocked"],
        "score": {"done": done, "total": total,
                  "percent": round(done / total * 100.0) if total else None},
    }
