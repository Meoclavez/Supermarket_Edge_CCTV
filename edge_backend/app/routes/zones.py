"""Per-camera overlays: tripwires, restricted areas and privacy masks.

All geometry is image coordinates normalised 0..1 to the camera's own frame.

* Tripwires (``/api/zones/tripwire``) count people crossing a line, "in" or
  "out" relative to ``in_side`` (the side of start->end, as drawn on screen,
  that a person is on after walking in). Crossings feed ``tripwire_events``
  and footfall; ``alert_enabled`` + ``alert_direction`` raise TRIPWIRE_ALERT.
* Restricted areas (``/api/zones/intrusion``) raise RESTRICTED_AREA when a
  person stays ``min_dwell_seconds`` inside while the schedule says the area
  is restricted.
* Privacy masks (``/api/zones/exclusion``): see services/privacy_mask.py.
* Checkout / queue areas (``/api/zones/queue``): a checkout-lane camera's
  lane (``kind: checkout``) or waiting line (``kind: queue``). Each person's
  time inside is recorded in ``queue_visits`` and reported by
  ``GET /api/v1/analytics/queues`` (camera roles, m0012).

Everything is validated; bad input is a 422, never stored. Evaluation lives in
services/tripwire_engine.py.
"""

import re
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..models.schemas import MaskMode
from ..services.ai_zone_service import ai_zone_service
from ..services.auth_service import auth_service

router = APIRouter(tags=["Zones & Masks"], dependencies=[Depends(auth_service.verify_api_access)])

Severity = Literal["INFO", "WARNING", "HIGH"]
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$|^24:00$")
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class NormPoint(BaseModel):
    model_config = ConfigDict(extra="ignore")
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)


def _upper(v: Any) -> Any:
    return v.upper() if isinstance(v, str) else v


def _lower(v: Any) -> Any:
    return v.lower() if isinstance(v, str) else v


# ------------------------------------------------------------------ tripwires

class TripwireReq(BaseModel):
    """A counting line. Unknown legacy fields (``allowed_classes``...) are ignored."""

    model_config = ConfigDict(extra="ignore")

    id: Optional[str] = Field(None, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    name: str = Field("Tripwire", min_length=1, max_length=80)
    camera_id: str = Field(min_length=1, max_length=64)
    x1: float = Field(ge=0.0, le=1.0)
    y1: float = Field(ge=0.0, le=1.0)
    x2: float = Field(ge=0.0, le=1.0)
    y2: float = Field(ge=0.0, le=1.0)
    in_side: Literal["left", "right"] = "right"
    counts_footfall: bool = True
    alert_enabled: bool = False
    alert_direction: Literal["in", "out", "both"] = "in"
    severity: Severity = "WARNING"
    enabled: bool = True
    # Legacy mobile field (aToB / bToA / bidirectional); mapped to in_side when in_side is absent.
    direction: Optional[str] = None

    _norm_sev = field_validator("severity", mode="before")(_upper)
    _norm_in = field_validator("in_side", "alert_direction", mode="before")(_lower)

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, v):
        return v.strip() if isinstance(v, str) else v

    @model_validator(mode="before")
    @classmethod
    def _legacy(cls, data):
        if isinstance(data, dict):
            data = dict(data)
            ls, le = data.get("line_start"), data.get("line_end")
            if isinstance(ls, dict) and "x1" not in data:
                data["x1"], data["y1"] = ls.get("x"), ls.get("y")
            if isinstance(le, dict) and "x2" not in data:
                data["x2"], data["y2"] = le.get("x"), le.get("y")
            if "in_side" not in data and data.get("direction"):
                d = str(data["direction"]).replace("_", "").lower()
                if d == "btoa":
                    data["in_side"] = "left"
                elif d == "atob":
                    data["in_side"] = "right"
            if not data.get("name"):
                data.pop("name", None)
        return data

    @model_validator(mode="after")
    def _length(self):
        if ((self.x2 - self.x1) ** 2 + (self.y2 - self.y1) ** 2) ** 0.5 < 0.02:
            raise ValueError("tripwire is too short: its two points must be at least 2% of the frame apart")
        return self


class TripwireUpdateReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = None
    x1: Optional[float] = None
    y1: Optional[float] = None
    x2: Optional[float] = None
    y2: Optional[float] = None
    in_side: Optional[str] = None
    counts_footfall: Optional[bool] = None
    alert_enabled: Optional[bool] = None
    alert_direction: Optional[str] = None
    severity: Optional[str] = None
    enabled: Optional[bool] = None


# ------------------------------------------------------------ restricted areas

class ScheduleRow(BaseModel):
    """Days + a local time window. ``to`` earlier than ``from`` runs past midnight."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    days: List[str] = Field(min_length=1, max_length=7)
    start: str = Field(alias="from")
    end: str = Field(alias="to")

    @field_validator("days", mode="before")
    @classmethod
    def _days(cls, v):
        if not isinstance(v, list):
            raise ValueError("days must be a list such as [\"mon\", \"tue\"]")
        out = []
        for d in v:
            if isinstance(d, bool):
                raise ValueError(f"invalid day {d!r}")
            if isinstance(d, int):
                if not 0 <= d <= 6:
                    raise ValueError(f"invalid day {d!r} (0=Monday..6=Sunday)")
                key = _DAYS[d]
            else:
                key = str(d).strip().lower()[:3]
                if key not in _DAYS:
                    raise ValueError(f"invalid day {d!r}")
            if key not in out:
                out.append(key)
        return sorted(out, key=_DAYS.index)

    @field_validator("start", "end")
    @classmethod
    def _time(cls, v):
        if not isinstance(v, str) or not _HHMM.match(v.strip()):
            raise ValueError("time must be HH:MM (00:00-23:59, or 24:00 as an end)")
        return v.strip()

    def dump(self) -> dict:
        return {"days": self.days, "from": self.start, "to": self.end}


class RestrictedAreaReq(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: Optional[str] = Field(None, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    name: str = Field("Restricted area", min_length=1, max_length=80)
    camera_id: str = Field(min_length=1, max_length=64)
    points: List[NormPoint] = Field(min_length=3, max_length=64)
    schedule: List[ScheduleRow] = Field(default_factory=list, max_length=21)
    schedule_mode: Literal["restricted_during", "allowed_during"] = "restricted_during"
    min_dwell_seconds: float = Field(3.0, ge=0.0, le=3600.0)
    severity: Severity = "HIGH"
    cooldown_seconds: Optional[float] = Field(None, ge=10.0, le=86400.0)
    timezone: Optional[str] = Field(None, max_length=64)
    enabled: bool = True

    _norm_sev = field_validator("severity", mode="before")(_upper)

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, v):
        return v.strip() if isinstance(v, str) else v

    @model_validator(mode="before")
    @classmethod
    def _legacy(cls, data):
        if isinstance(data, dict):
            data = dict(data)
            if "points" not in data and isinstance(data.get("polygon_points"), list):
                data["points"] = data["polygon_points"]
            if "min_dwell_seconds" not in data and data.get("dwell_time_seconds") is not None:
                data["min_dwell_seconds"] = data["dwell_time_seconds"]
            if not data.get("name"):
                data.pop("name", None)
            if data.get("timezone") in ("", None):
                data.pop("timezone", None)
        return data

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v):
        if v is None:
            return v
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(v)
        except Exception:
            raise ValueError(f"unknown time zone {v!r} (use an IANA name such as Australia/Melbourne)")
        return v

    def record(self) -> dict:
        out = self.model_dump(exclude={"schedule", "points"}, exclude_none=True)
        out["points"] = [{"x": round(p.x, 4), "y": round(p.y, 4)} for p in self.points]
        out["schedule"] = [row.dump() for row in self.schedule]
        return out


class RestrictedAreaUpdateReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = None
    points: Optional[List[Dict[str, Any]]] = None
    schedule: Optional[List[Dict[str, Any]]] = None
    schedule_mode: Optional[str] = None
    min_dwell_seconds: Optional[float] = None
    severity: Optional[str] = None
    cooldown_seconds: Optional[float] = None
    timezone: Optional[str] = None
    enabled: Optional[bool] = None


# --------------------------------------------------------------- privacy masks
# Mask modes (see services/privacy_mask.py): BLUR / MOSAIC / BLACKOUT / COLOR
# are burned into every frame shown or saved; AI_IGNORE leaves the picture
# alone and drops people standing in the area from analysis. Unknown modes are
# rejected with 422 rather than stored.
BGRColour = List[int]


# ``ignore_box_fraction`` (AI_IGNORE only, 0.1..1.0, default 0.6 =
# settings.IGNORE_BOX_FRACTION): a detection is dropped when its foot point is
# inside the area OR at least this share of its box lies inside it. Stored
# for other modes but returned as null there (the rule does not apply).
IgnoreFraction = Optional[float]


class ExclusionReq(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    camera_id: Optional[str] = None
    points: List[Dict[str, float]]
    mask_mode: MaskMode = MaskMode.BLUR
    mask_color_bgr: Optional[BGRColour] = Field(None, min_length=3, max_length=3)
    enabled: Optional[bool] = True
    ignore_box_fraction: IgnoreFraction = Field(None, ge=0.1, le=1.0)


class ExclusionUpdateReq(BaseModel):
    name: Optional[str] = None
    mask_mode: Optional[MaskMode] = None
    mask_color_bgr: Optional[BGRColour] = Field(None, min_length=3, max_length=3)
    enabled: Optional[bool] = None
    ignore_box_fraction: IgnoreFraction = Field(None, ge=0.1, le=1.0)


class ExclusionFromBoxReq(BaseModel):
    """An ignore area around one detection box (e.g. a poster picked on the live view)."""

    model_config = ConfigDict(extra="ignore")
    camera_id: str = Field(min_length=1, max_length=64)
    # [x1, y1, x2, y2] normalised 0..1 to the analysed frame.
    box: List[float] = Field(min_length=4, max_length=4)
    # Added on each side, as a share of the box's width / height.
    pad: float = Field(0.1, ge=0.0, le=0.5)
    name: Optional[str] = Field(None, max_length=80)
    ignore_box_fraction: IgnoreFraction = Field(None, ge=0.1, le=1.0)

    @field_validator("box")
    @classmethod
    def _box(cls, v):
        import math

        if any(not isinstance(c, (int, float)) or isinstance(c, bool) or not math.isfinite(float(c)) for c in v):
            raise ValueError("box must be four numbers [x1, y1, x2, y2]")
        x1, y1, x2, y2 = (float(c) for c in v)
        if not (-0.5 <= x1 <= 1.5 and -0.5 <= x2 <= 1.5 and -0.5 <= y1 <= 1.5 and -0.5 <= y2 <= 1.5):
            raise ValueError("box must be normalised 0..1 to the frame")
        if x2 <= x1 or y2 <= y1:
            raise ValueError("box must have x2 > x1 and y2 > y1")
        return [x1, y1, x2, y2]


def _mask_record(data: Dict[str, Any]) -> Dict[str, Any]:
    """JSON-ready mask: enum as its string value, colour clamped to 0..255."""
    out = dict(data)
    if out.get("ignore_box_fraction") is None:
        out.pop("ignore_box_fraction", None)
    else:
        out["ignore_box_fraction"] = round(float(out["ignore_box_fraction"]), 3)
    mode = out.get("mask_mode")
    if isinstance(mode, MaskMode):
        out["mask_mode"] = mode.value
    colour = out.get("mask_color_bgr")
    if colour is not None:
        out["mask_color_bgr"] = [max(0, min(255, int(c))) for c in colour]
    else:
        out.pop("mask_color_bgr", None)
    return out


# ------------------------------------------------------- checkout / queue areas

class QueueAreaReq(BaseModel):
    """A checkout lane (``checkout``) or waiting line (``queue``) on one camera."""

    model_config = ConfigDict(extra="ignore")

    id: Optional[str] = Field(None, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    name: str = Field("Checkout", min_length=1, max_length=80)
    camera_id: str = Field(min_length=1, max_length=64)
    kind: Literal["checkout", "queue"] = "checkout"
    points: List[NormPoint] = Field(min_length=3, max_length=64)
    enabled: bool = True

    _norm_kind = field_validator("kind", mode="before")(_lower)

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, v):
        return v.strip() if isinstance(v, str) else v

    @model_validator(mode="before")
    @classmethod
    def _legacy(cls, data):
        if isinstance(data, dict):
            data = dict(data)
            if "points" not in data and isinstance(data.get("polygon_points"), list):
                data["points"] = data["polygon_points"]
            if not data.get("name"):
                data.pop("name", None)
        return data


class QueueAreaUpdateReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = None
    kind: Optional[str] = None
    points: Optional[List[Dict[str, Any]]] = None
    enabled: Optional[bool] = None


# ------------------------------------------------------------------ helpers

def _errors(e: ValidationError) -> list:
    return [{"loc": list(err.get("loc", ())), "msg": err.get("msg"), "type": err.get("type")}
            for err in e.errors()]


def _tripwire_record(payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        req = TripwireReq.model_validate(payload)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=_errors(e))
    rec = req.model_dump(exclude_none=True)
    for k in ("x1", "y1", "x2", "y2"):
        rec[k] = round(rec[k], 4)
    return rec


def _area_record(payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        req = RestrictedAreaReq.model_validate(payload)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=_errors(e))
    return req.record()


def _queue_record(payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        req = QueueAreaReq.model_validate(payload)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=_errors(e))
    out = req.model_dump(exclude={"points"}, exclude_none=True)
    out["points"] = [{"x": round(p.x, 4), "y": round(p.y, 4)} for p in req.points]
    return out


def _with_legacy_geometry(z: Dict[str, Any]) -> Dict[str, Any]:
    """Aliases the mobile zone editor reads (``line_start``/``polygon_points``...)."""
    z = dict(z)
    if "x1" in z and "line_start" not in z:
        z["line_start"] = {"x": z["x1"], "y": z["y1"]}
        z["line_end"] = {"x": z["x2"], "y": z["y2"]}
    if "points" in z and "polygon_points" not in z:
        z["polygon_points"] = z["points"]
    if "min_dwell_seconds" in z and "dwell_time_seconds" not in z:
        z["dwell_time_seconds"] = z["min_dwell_seconds"]
    return z


# ------------------------------------------------------------------ routes

@router.get("/api/zones")
def get_all_zones_grouped():
    return ai_zone_service.get_all_zones()


@router.get("/api/v1/cameras/{camera_id}/zones")
def get_camera_zones(camera_id: str):
    zones_data = ai_zone_service.get_all_zones(camera_id)
    combined = []
    for tw in zones_data.get("tripwires", []):
        z = _with_legacy_geometry(tw)
        z["zone_type"] = "TRIPWIRE"
        combined.append(z)
    for iz in zones_data.get("intrusion_zones", []):
        z = _with_legacy_geometry(iz)
        z["zone_type"] = "RESTRICTED_ZONE"
        combined.append(z)
    for ex in zones_data.get("exclusion_masks", []):
        z = _with_legacy_geometry(ex)
        z["zone_type"] = "EXCLUSION"
        combined.append(z)
    for qz in zones_data.get("queue_zones", []):
        z = _with_legacy_geometry(qz)
        z["zone_type"] = "QUEUE_AREA"
        combined.append(z)
    return combined


@router.post("/api/v1/cameras/{camera_id}/zones")
def create_camera_zone(camera_id: str, payload: Dict[str, Any]):
    z_type = str(payload.get("zone_type", "TRIPWIRE")).upper()
    payload = dict(payload)
    payload["camera_id"] = camera_id

    if z_type == "TRIPWIRE" or "line_start" in payload:
        return ai_zone_service.add_tripwire(_tripwire_record(payload))
    if z_type in ("INTRUSION", "RESTRICTED_ZONE", "RESTRICTED_AREA"):
        return ai_zone_service.add_intrusion(_area_record(payload))
    if z_type in ("QUEUE_AREA", "CHECKOUT", "QUEUE"):
        if "kind" not in payload and z_type in ("CHECKOUT", "QUEUE"):
            payload["kind"] = z_type.lower()
        return ai_zone_service.add_queue_zone(_queue_record(payload))
    payload["id"] = payload.get("id") or f"zone_{z_type.lower()}_{camera_id}"
    if "points" not in payload and isinstance(payload.get("polygon_points"), list):
        payload["points"] = payload["polygon_points"]
    try:
        payload["mask_mode"] = MaskMode(str(payload.get("mask_mode") or "BLUR").upper()).value
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown mask_mode '{payload.get('mask_mode')}'. Use one of: {', '.join(m.value for m in MaskMode)}",
        )
    frac = payload.get("ignore_box_fraction")
    if frac is not None:
        try:
            ok = 0.1 <= float(frac) <= 1.0
        except (TypeError, ValueError):
            ok = False
        if not ok:
            raise HTTPException(status_code=422, detail="ignore_box_fraction must be between 0.1 and 1.0")
    return ai_zone_service.add_exclusion(_mask_record(payload))


@router.delete("/api/v1/cameras/{camera_id}/zones/{zone_id}")
def delete_camera_zone(camera_id: str, zone_id: str):
    if ai_zone_service.delete_tripwire(zone_id) or \
       ai_zone_service.delete_intrusion(zone_id) or \
       ai_zone_service.delete_exclusion(zone_id) or \
       ai_zone_service.delete_queue_zone(zone_id):
        return {"status": "success", "message": f"Deleted zone {zone_id}"}
    raise HTTPException(status_code=404, detail="Zone not found")


@router.post("/api/zones/tripwire")
def create_or_update_tripwire(payload: Dict[str, Any]):
    saved = ai_zone_service.add_tripwire(_tripwire_record(payload))
    return {"status": "success", "tripwire": saved}


@router.patch("/api/zones/tripwire/{tw_id}")
def update_tripwire(tw_id: str, req: TripwireUpdateReq):
    """Change a tripwire's settings (and optionally its line) in place."""
    current = ai_zone_service.get_tripwire(tw_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Tripwire not found")
    changes = req.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=422, detail="Nothing to update")
    merged = {**current, **changes, "id": tw_id}
    merged.pop("direction", None)
    saved = ai_zone_service.update_tripwire(tw_id, _tripwire_record(merged))
    return {"status": "success", "tripwire": saved}


@router.delete("/api/zones/tripwire/{tw_id}")
def delete_tripwire(tw_id: str):
    if ai_zone_service.delete_tripwire(tw_id):
        return {"status": "success", "message": f"Deleted tripwire {tw_id}"}
    raise HTTPException(status_code=404, detail="Tripwire not found")


@router.post("/api/zones/intrusion")
def create_or_update_intrusion(payload: Dict[str, Any]):
    saved = ai_zone_service.add_intrusion(_area_record(payload))
    return {"status": "success", "intrusion_zone": saved}


@router.patch("/api/zones/intrusion/{iz_id}")
def update_intrusion(iz_id: str, req: RestrictedAreaUpdateReq):
    """Change a restricted area's settings (and optionally its polygon) in place."""
    current = ai_zone_service.get_intrusion(iz_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Restricted area not found")
    changes = req.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="Nothing to update")
    merged = {**current, **changes, "id": iz_id}
    merged.pop("dwell_time_seconds", None)
    saved = ai_zone_service.update_intrusion(iz_id, _area_record(merged))
    return {"status": "success", "intrusion_zone": saved}


@router.delete("/api/zones/intrusion/{iz_id}")
def delete_intrusion(iz_id: str):
    if ai_zone_service.delete_intrusion(iz_id):
        return {"status": "success", "message": f"Deleted intrusion zone {iz_id}"}
    raise HTTPException(status_code=404, detail="Intrusion zone not found")


@router.post("/api/zones/exclusion")
def create_or_update_exclusion(req: ExclusionReq):
    saved = ai_zone_service.add_exclusion(_mask_record(req.model_dump()))
    return {"status": "success", "exclusion_mask": saved}


@router.post("/api/zones/exclusion/from-box")
def create_exclusion_from_box(req: ExclusionFromBoxReq):
    """Create an AI_IGNORE rectangle around a box (padded, clamped to the frame).

    Admin only (a setup write: not on auth_service's operator list). Returns
    the same shape as ``POST /api/zones/exclusion``.
    """
    x1, y1, x2, y2 = req.box
    px, py = (x2 - x1) * req.pad, (y2 - y1) * req.pad
    x1, x2 = max(0.0, x1 - px), min(1.0, x2 + px)
    y1, y2 = max(0.0, y1 - py), min(1.0, y2 + py)
    if x2 - x1 < 0.005 or y2 - y1 < 0.005:
        raise HTTPException(status_code=422, detail="box lies outside the frame (nothing left after clamping to 0..1)")
    pts = [{"x": round(x, 4), "y": round(y, 4)} for x, y in ((x1, y1), (x2, y1), (x2, y2), (x1, y2))]
    record = {
        "name": (req.name or "").strip() or "Ignore area",
        "camera_id": req.camera_id,
        "points": pts,
        "mask_mode": MaskMode.AI_IGNORE.value,
        "enabled": True,
    }
    if req.ignore_box_fraction is not None:
        record["ignore_box_fraction"] = round(float(req.ignore_box_fraction), 3)
    saved = ai_zone_service.add_exclusion(record)
    return {"status": "success", "exclusion_mask": saved}


@router.patch("/api/zones/exclusion/{ex_id}")
def update_exclusion(ex_id: str, req: ExclusionUpdateReq):
    """Change an existing mask's mode, colour, name or enabled flag in place."""
    changes = _mask_record(req.model_dump(exclude_none=True))
    if not changes:
        raise HTTPException(status_code=422, detail="Nothing to update")
    saved = ai_zone_service.update_exclusion(ex_id, changes)
    if saved is None:
        raise HTTPException(status_code=404, detail="Exclusion mask not found")
    return {"status": "success", "exclusion_mask": saved}


@router.delete("/api/zones/exclusion/{ex_id}")
def delete_exclusion(ex_id: str):
    if ai_zone_service.delete_exclusion(ex_id):
        return {"status": "success", "message": f"Deleted exclusion mask {ex_id}"}
    raise HTTPException(status_code=404, detail="Exclusion mask not found")


@router.post("/api/zones/queue")
def create_or_update_queue_area(payload: Dict[str, Any]):
    """Draw a checkout lane (``kind: checkout``) or waiting line (``kind: queue``) on a camera."""
    saved = ai_zone_service.add_queue_zone(_queue_record(payload))
    return {"status": "success", "queue_area": saved}


@router.patch("/api/zones/queue/{qz_id}")
def update_queue_area(qz_id: str, req: QueueAreaUpdateReq):
    current = ai_zone_service.get_queue_zone(qz_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Queue area not found")
    changes = req.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="Nothing to update")
    saved = ai_zone_service.update_queue_zone(qz_id, _queue_record({**current, **changes, "id": qz_id}))
    return {"status": "success", "queue_area": saved}


@router.delete("/api/zones/queue/{qz_id}")
def delete_queue_area(qz_id: str):
    if ai_zone_service.delete_queue_zone(qz_id):
        return {"status": "success", "message": f"Deleted queue area {qz_id}"}
    raise HTTPException(status_code=404, detail="Queue area not found")


_CLEAR_KINDS = {"tripwires", "intrusion_zones", "exclusion_masks", "queue_zones",
                # mask subsets: privacy masks only / ignore (AI_IGNORE) areas only; everything
                "privacy", "ignore", "all"}


class ClearReq(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kinds: Optional[Any] = None
    camera_id: Optional[str] = Field(None, min_length=1, max_length=64)


def _kinds_list(kinds: Any) -> Optional[List[str]]:
    if kinds is None or kinds == "" or kinds == []:
        return None
    if isinstance(kinds, str):
        items = kinds.split(",")
    elif isinstance(kinds, list):
        items = [str(k) for k in kinds]
    else:
        raise HTTPException(status_code=422, detail="kinds must be a string or a list of strings")
    wanted = [k.strip().lower() for k in items if k.strip()]
    bad = [k for k in wanted if k not in _CLEAR_KINDS]
    if bad:
        raise HTTPException(status_code=422, detail=f"Unknown kind(s): {', '.join(bad)}")
    return wanted or None


@router.post("/api/zones/clear")
def clear_all_zones(kinds: Optional[str] = Query(None, description="Comma-separated subset of "
                                                 "tripwires,intrusion_zones,exclusion_masks,queue_zones, or the "
                                                 "mask subsets privacy (all masks but AI_IGNORE) / ignore "
                                                 "(AI_IGNORE only), or all; default all"),
                    camera_id: Optional[str] = Query(None, description="Only this camera's overlays"),
                    body: Optional[ClearReq] = None):
    """Delete overlays. ``kinds`` / ``camera_id`` as query parameters or a JSON body."""
    wanted = _kinds_list(kinds if kinds else (body.kinds if body is not None else None))
    cam = camera_id or (body.camera_id if body is not None else None)
    n = ai_zone_service.clear_all(wanted, camera_id=cam)
    return {"status": "success", "removed": n, "message": "Zones cleared."}


@router.get("/api/zones/alerts/{alert_id}/snapshot")
def zone_alert_snapshot(alert_id: str):
    """Privacy-masked snapshot saved with a RESTRICTED_AREA / TRIPWIRE_ALERT."""
    from ..services.tripwire_engine import tripwire_engine

    path = tripwire_engine.snapshot_path(alert_id)
    if path is None:
        raise HTTPException(status_code=404, detail="No snapshot for this alert")
    return FileResponse(str(path), media_type="image/jpeg")
