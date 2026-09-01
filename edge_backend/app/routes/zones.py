"""Zone, Tripwire, and Exclusion Mask API Routes."""

from typing import List, Dict, Any, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..models.schemas import ZoneConfig
from ..services.ai_zone_service import ai_zone_service

router = APIRouter(tags=["Zones & Masks"])

class TripwireReq(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    camera_id: Optional[str] = "cam_living_room"
    x1: float
    y1: float
    x2: float
    y2: float
    direction: Optional[str] = "BIDIRECTIONAL"
    allowed_classes: Optional[List[str]] = ["person", "vehicle"]
    enabled: Optional[bool] = True

class IntrusionReq(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    camera_id: Optional[str] = "cam_front_door"
    points: List[Dict[str, float]]
    allowed_classes: Optional[List[str]] = ["person", "vehicle"]
    dwell_time_seconds: Optional[float] = 0.5
    enabled: Optional[bool] = True

class ExclusionReq(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    camera_id: Optional[str] = "cam_backyard"
    points: List[Dict[str, float]]
    mask_mode: Optional[str] = "BLUR"
    enabled: Optional[bool] = True

@router.get("/api/zones")
def get_all_zones_grouped():
    return ai_zone_service.get_all_zones()

@router.get("/api/v1/cameras/{camera_id}/zones")
def get_camera_zones(camera_id: str):
    zones_data = ai_zone_service.get_all_zones(camera_id)
    combined = []
    for tw in zones_data.get("tripwires", []):
        z = dict(tw)
        z["zone_type"] = "TRIPWIRE"
        if "x1" in z and "y1" in z and "line_start" not in z:
            z["line_start"] = {"x": z["x1"], "y": z["y1"]}
        if "x2" in z and "y2" in z and "line_end" not in z:
            z["line_end"] = {"x": z["x2"], "y": z["y2"]}
        combined.append(z)
    for iz in zones_data.get("intrusion_zones", []):
        z = dict(iz)
        z["zone_type"] = "INTRUSION"
        combined.append(z)
    for ex in zones_data.get("exclusion_masks", []):
        z = dict(ex)
        z["zone_type"] = "EXCLUSION"
        combined.append(z)
    return combined

@router.post("/api/v1/cameras/{camera_id}/zones")
def create_camera_zone(camera_id: str, payload: Dict[str, Any]):
    z_type = payload.get("zone_type", "TRIPWIRE").upper()
    zone_id = payload.get("id") or f"zone_{z_type.lower()}_{camera_id}"
    payload["id"] = zone_id
    payload["camera_id"] = camera_id
    
    if z_type == "TRIPWIRE" or "line_start" in payload:
        if "line_start" in payload and isinstance(payload["line_start"], dict):
            payload["x1"] = payload["line_start"].get("x", 0.0)
            payload["y1"] = payload["line_start"].get("y", 0.0)
        if "line_end" in payload and isinstance(payload["line_end"], dict):
            payload["x2"] = payload["line_end"].get("x", 1.0)
            payload["y2"] = payload["line_end"].get("y", 1.0)
        saved = ai_zone_service.add_tripwire(payload)
        return saved
    elif z_type in ["INTRUSION", "RESTRICTED_ZONE"]:
        saved = ai_zone_service.add_intrusion(payload)
        return saved
    else:
        saved = ai_zone_service.add_exclusion(payload)
        return saved

@router.delete("/api/v1/cameras/{camera_id}/zones/{zone_id}")
def delete_camera_zone(camera_id: str, zone_id: str):
    if ai_zone_service.delete_tripwire(zone_id) or \
       ai_zone_service.delete_intrusion(zone_id) or \
       ai_zone_service.delete_exclusion(zone_id):
        return {"status": "success", "message": f"Deleted zone {zone_id}"}
    raise HTTPException(status_code=404, detail="Zone not found")

@router.post("/api/zones/tripwire")
def create_or_update_tripwire(req: TripwireReq):
    saved = ai_zone_service.add_tripwire(req.model_dump())
    return {"status": "success", "tripwire": saved}

@router.delete("/api/zones/tripwire/{tw_id}")
def delete_tripwire(tw_id: str):
    if ai_zone_service.delete_tripwire(tw_id):
        return {"status": "success", "message": f"Deleted tripwire {tw_id}"}
    raise HTTPException(status_code=404, detail="Tripwire not found")

@router.post("/api/zones/intrusion")
def create_or_update_intrusion(req: IntrusionReq):
    saved = ai_zone_service.add_intrusion(req.model_dump())
    return {"status": "success", "intrusion_zone": saved}

@router.delete("/api/zones/intrusion/{iz_id}")
def delete_intrusion(iz_id: str):
    if ai_zone_service.delete_intrusion(iz_id):
        return {"status": "success", "message": f"Deleted intrusion zone {iz_id}"}
    raise HTTPException(status_code=404, detail="Intrusion zone not found")

@router.post("/api/zones/exclusion")
def create_or_update_exclusion(req: ExclusionReq):
    saved = ai_zone_service.add_exclusion(req.model_dump())
    return {"status": "success", "exclusion_mask": saved}

@router.delete("/api/zones/exclusion/{ex_id}")
def delete_exclusion(ex_id: str):
    if ai_zone_service.delete_exclusion(ex_id):
        return {"status": "success", "message": f"Deleted exclusion mask {ex_id}"}
    raise HTTPException(status_code=404, detail="Exclusion mask not found")

@router.post("/api/zones/clear")
def clear_all_zones():
    ai_zone_service.clear_all()
    return {"status": "success", "message": "All zones cleared."}
