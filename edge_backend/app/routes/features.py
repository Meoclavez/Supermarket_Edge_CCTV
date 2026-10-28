"""The dashboard's Features tab: one cheap read of every feature's state.

GET /api/v1/features/status   any signed-in person (rate limited like the analytics reads)

One request instead of the dozen the tab would otherwise need (cameras, layout,
zones, night watch, phone roster, accounts, online access, sales, backups...).
It only reads what is already in memory or in the database; it never writes,
never probes a camera, a recorder, the tunnel or the AI model, and it does not
read the data of a feature the iKorex plan leaves out (that feature is
``plan_off`` and nothing else). The full controls keep their own endpoints;
this endpoint is only for the status chips.

Each feature: ``{"state": "on" | "off" | "setup" | "plan_off", "detail": str}``
("setup" = something the store still has to do; the detail says what). Facts
from owner/administrator-only screens (accounts, backups, online access, the
till key) are summarised as a state only for operators: their detail is empty.
``links`` names the cameras the dashboard's "Full settings" buttons should open
(the first one that still needs the step).

While the service is paused this answers 423 like every other store API
(entitlements.FleetGateMiddleware): the paused page replaces the dashboard.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.db_models import (AdminUserModel, AnalysisRunModel, CameraModel, InsightTopicModel,
                                  PairedDeviceModel, POSTransactionModel, StoreLayoutModel, StoreStructureModel,
                                  StoreZoneModel, WebPushSubscriptionModel)
from app.models.schemas import CameraFeatureConfig
from app.routes import ResilientRoute
from app.services import entitlements
from app.services.auth_service import auth_service, effective_role, general_rate_limiter, is_admin_role

logger = logging.getLogger("edge.features")

router = APIRouter(
    prefix="/api/v1/features",
    tags=["Features"],
    dependencies=[Depends(auth_service.verify_api_access), Depends(general_rate_limiter)],
    route_class=ResilientRoute,
)

# Feature card id -> the iKorex plan key that can leave it out (None: always in the plan).
PLAN_KEYS: Dict[str, Optional[str]] = {
    "theft-alerts": "theft_detection", "alert-levels": "theft_detection",
    "area-line-alerts": "zone_alerts", "restricted-areas": "restricted_areas", "night-watch": "night_watch", "phone-alerts": "phone_alerts",
    "footfall": None, "queues": None, "shelf-reaches": None, "heatmaps": "heatmaps",
    "insights": "business_insights", "sales-data": "pos_integration",
    "cameras": None, "camera-purpose": None, "store-map": None, "placement-calibration": None,
    "store-areas": None, "privacy-ignore": None, "sub-streams": None,
    "online-access": "online_access", "remote-video": "remote_live_video", "accounts": None,
    "phone-app": "phone_alerts",
    "store-settings": None, "evidence": None, "backups": None, "analysis-speed": None,
    "support": None, "ikorex-plan": None,
}
# Cards whose detail comes from an owner/administrator-only screen.
ADMIN_DETAIL = frozenset({"accounts", "backups", "online-access", "remote-video", "sales-data", "sub-streams"})


def _plural(n: int, one: str, many: Optional[str] = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _pct(v: Any) -> str:
    try:
        return f"{round(float(v) * 100)} %"
    except (TypeError, ValueError):
        return "-"


def _eff(key: str) -> Any:
    """A site setting as it applies now (dashboard value, else the .env default)."""
    from app.services.site_settings import site_settings

    try:
        return site_settings.effective(key)
    except Exception:  # noqa: BLE001
        return getattr(settings, key, None)


def _st(state: str, detail: str = "") -> Dict[str, str]:
    return {"state": state, "detail": detail}


def _caller_is_admin(request: Request) -> bool:
    user = getattr(request.state, "user", None)
    if not isinstance(user, dict):
        return True  # auth disabled (developer mode): the whole dashboard is open
    return is_admin_role(effective_role(user))


async def _collect(db: AsyncSession, plan: Dict[str, bool]) -> Dict[str, Any]:
    """Everything the states need, read once. Feature data left out of the plan is not read."""
    from app.services.ai_zone_service import ai_zone_service, is_ignore_mask
    from app.services.live_analytics_engine import live_engine
    from app.services.shelf_interaction_service import shelf_interaction_service

    d: Dict[str, Any] = {}
    cams = (await db.execute(select(CameraModel).order_by(CameraModel.name))).scalars().all()
    rows = []
    for c in cams:
        try:
            feats = CameraFeatureConfig.model_validate(c.features or {})
        except Exception:  # noqa: BLE001 - one bad row must not hide every status
            feats = CameraFeatureConfig()
        rt = live_engine.runtimes.get(c.id)
        rows.append({
            "id": c.id, "name": c.name, "on": bool(c.is_ai_enabled), "role": c.role,
            "online": bool(rt is not None and getattr(rt, "status", None) == "ONLINE"),
            "calibrated": bool(c.homography_matrix), "watch_zone_id": c.watch_zone_id,
            "placed": c.map_placed is not False, "features": feats,
        })
    d["cameras"] = rows

    layout = (await db.execute(select(StoreLayoutModel).where(StoreLayoutModel.is_active.is_(True)).limit(1))
              ).scalar_one_or_none()
    if layout is None:
        layout = (await db.execute(select(StoreLayoutModel).limit(1))).scalar_one_or_none()
    zone_ids: set = set()
    structures = 0
    if layout is not None:
        zone_ids = {z for (z,) in (await db.execute(
            select(StoreZoneModel.id).where(StoreZoneModel.layout_id == layout.id))).all()}
        structures = int(await db.scalar(select(func.count(StoreStructureModel.id)).where(
            StoreStructureModel.layout_id == layout.id)) or 0)
    d["zone_ids"], d["structures"] = zone_ids, structures

    overlay = ai_zone_service.get_all_zones()
    d["tripwires"] = overlay.get("tripwires") or []
    d["restricted"] = overlay.get("intrusion_zones") or []
    d["queue_areas"] = overlay.get("queue_zones") or []
    masks = overlay.get("exclusion_masks") or []
    d["ignore_areas"] = sum(1 for m in masks if is_ignore_mask(m))
    d["privacy_masks"] = len(masks) - d["ignore_areas"]
    d["shelves"] = len(shelf_interaction_service.get_zones(None))

    if plan.get("restricted_areas", True):
        from app.services.restricted_areas import restricted_areas

        now = restricted_areas.clock()
        snap = restricted_areas.snapshot
        d["ra"] = {"total": len(snap.areas), "on": [a for a in snap.areas if a.enabled],
                   "armed": [a for a in snap.areas if a.armed_state(now)[0]],
                   "unwatched": [a for a in snap.areas if a.enabled and not a.cameras()]}

    accounts = (await db.execute(select(AdminUserModel))).scalars().all()
    d["accounts"] = [a for a in accounts if a.is_active is None or a.is_active]

    if plan.get("phone_alerts", True):
        from app.services import push_alerts, web_push

        d["roster"] = await asyncio.to_thread(push_alerts.load_roster)
        key = web_push.vapid_key_id()
        d["phones"] = int(await db.scalar(select(func.count(WebPushSubscriptionModel.id)).where(
            WebPushSubscriptionModel.vapid_key_id == key)) or 0)
        d["paired"] = int(await db.scalar(select(func.count(PairedDeviceModel.id)).where(
            PairedDeviceModel.revoked_at.is_(None))) or 0)
    if plan.get("pos_integration", True):
        from app.services.till_key import till_key_store

        d["sales"] = int(await db.scalar(select(func.count(POSTransactionModel.id))) or 0)
        d["till_key"] = await asyncio.to_thread(till_key_store.status)
    if plan.get("business_insights", True):
        d["runs"] = int(await db.scalar(select(func.count(AnalysisRunModel.id))) or 0)
        d["open_topics"] = int(await db.scalar(select(func.count(InsightTopicModel.id)).where(
            InsightTopicModel.status == "open")) or 0)
    if plan.get("heatmaps", True):
        from app.services.heatmap_history import heatmap_recorder

        d["heatmap"] = heatmap_recorder.status()

    from app.services.remote_access_service import CONNECTED, remote_access_service

    ra = remote_access_service.settings
    d["remote"] = {"enabled": bool(ra.get("enabled")), "connected": remote_access_service.state == CONNECTED,
                   "configured": bool(ra.get("server_url") and ra.get("store_id")),
                   "verified": bool(ra.get("verified")) and ra.get("verified_hostname") == (ra.get("hostname") or "")}

    from app.services.backup_service import backup_service

    try:
        d["backups"] = await asyncio.to_thread(backup_service.list_backups)
    except Exception:  # noqa: BLE001
        d["backups"] = None
    from app.services.evidence_storage import evidence_storage

    d["evidence"] = evidence_storage.status()
    from app.services.fleet_control import fleet_control

    d["fleet"] = await asyncio.to_thread(fleet_control.status)
    try:
        from app.routes.recorders import _recorders
        from app.services.recorder_substreams import recorder_credentials

        groups = await _recorders(db)
        d["recorders"] = [bool(recorder_credentials(h, c)[1]) for h, c in groups.items()]
    except Exception:  # noqa: BLE001
        d["recorders"] = None
    return d


# --------------------------------------------------------------------------- states

def _theft(d) -> Dict[str, str]:
    cams = d["cameras"]
    if not cams:
        return _st("setup", "Add cameras first.")
    on = [c for c in cams if c["features"].theft_detection and c["on"]]
    if not on:
        return _st("off", "Theft checks are off on every camera.")
    conf = _pct(_eff("THEFT_MIN_CONFIDENCE"))
    if not d["shelves"]:
        return _st("setup", f"On for {len(on)} of {len(cams)} cameras, but no product shelves are drawn yet, "
                            "so reaches into shelves cannot be checked.")
    return _st("on", f"On for {len(on)} of {len(cams)} cameras. Alerts from {conf} confidence.")


def _levels(d) -> Dict[str, str]:
    w, a, c = (_eff(k) for k in
               ("THEFT_TIER_WATCH_MIN", "THEFT_TIER_ALERT_MIN", "THEFT_TIER_CRITICAL_MIN"))
    push = "phones are told from Alert" if _eff("THEFT_ROUTE_ALERT_PUSH") is not False else "phones are not told at Alert"
    return _st("on", f"Watch from {_pct(w)}, Alert from {_pct(a)}, Critical from {_pct(c)}; {push}.")


def _area_lines(d) -> Dict[str, str]:
    areas = [z for z in d["restricted"] if z.get("enabled", True)]
    lines = [t for t in d["tripwires"] if t.get("alert_enabled") and t.get("enabled", True)]
    if not areas and not lines:
        return _st("off", "No restricted area or alert line drawn yet.")
    return _st("on", f"{_plural(len(areas), 'restricted area')} and {_plural(len(lines), 'alert line')} on.")


def _restricted(d) -> Dict[str, str]:
    r = d.get("ra") or {}
    if not r.get("total"):
        return _st("off", "No restricted area set up yet.")
    if r.get("unwatched"):
        return _st("setup", f"{r['unwatched'][0].name}: no camera watches it yet.")
    if not r.get("on"):
        return _st("off", f"{_plural(r['total'], 'restricted area')}, all switched off.")
    armed = r.get("armed") or []
    return _st("on", f"{_plural(len(r['on']), 'restricted area')} on, {len(armed)} armed now.")


def _night(d) -> Dict[str, str]:
    on = [c for c in d["cameras"] if c["features"].night_watch and c["features"].night_watch.enabled]
    if not on:
        return _st("off", "No camera watches at night yet.")
    nw = on[0]["features"].night_watch
    return _st("on", f"{_plural(len(on), 'camera')} watching, for example {on[0]['name']} {nw.start}-{nw.end}.")


def _phones(d) -> Dict[str, str]:
    roster = d.get("roster") or {}
    n = d.get("phones", 0)
    if not n:
        return _st("setup", "No phone has alerts turned on yet. Each person installs the app and taps "
                            "\"Enable alerts\".")
    if not roster.get("configured"):
        return _st("setup", f"{_plural(n, 'phone')} connected. Choose at least 2 first-priority people.")
    return _st("on", f"{_plural(n, 'phone')} connected; {_plural(len(roster.get('first_priority') or []), 'person', 'people')} "
                     f"told first, backup after {roster.get('escalate_after_min')} min.")


def _footfall(d) -> Dict[str, str]:
    cams = d["cameras"]
    if not cams:
        return _st("setup", "Add cameras first.")
    counting = [c for c in cams if c["features"].people_counting and c["on"]]
    entrances = [t for t in d["tripwires"] if t.get("counts_footfall", True) and t.get("enabled", True)]
    if not counting:
        return _st("off", "People counting is off on every camera.")
    if not entrances:
        return _st("setup", f"Counting on {_plural(len(counting), 'camera')}. Draw an entrance line for exact "
                            "visitor numbers; until then visitors are estimated.")
    return _st("on", f"{_plural(len(entrances), 'entrance line')}; counting on {_plural(len(counting), 'camera')}.")


def _queues(d) -> Dict[str, str]:
    n = len(d["queue_areas"])
    wait = round(float(_eff("QUEUE_CONGESTED_WAIT_SEC") or 0) / 60, 1)
    if not n:
        return _st("off", "No checkout or queue area drawn yet.")
    return _st("on", f"{_plural(n, 'checkout or queue area')}; a queue counts as long after {wait:g} min.")


def _shelves(d) -> Dict[str, str]:
    on = [c for c in d["cameras"] if c["features"].shelf_interaction and c["on"]]
    if not d["shelves"]:
        return _st("setup", "No product shelves drawn yet.")
    if not on:
        return _st("off", f"{_plural(d['shelves'], 'product shelf', 'product shelves')} drawn, but shelf "
                          "interaction is off on every camera.")
    return _st("on", f"{_plural(d['shelves'], 'product shelf', 'product shelves')} on {_plural(len(on), 'camera')}.")


def _heatmaps(d) -> Dict[str, str]:
    cal = sum(1 for c in d["cameras"] if c["calibrated"])
    rec = d.get("heatmap") or {}
    if not cal:
        return _st("setup", "Calibrate at least one camera so shoppers can be placed on the map.")
    if rec and rec.get("enabled") is False:
        return _st("off", "Hourly recording is turned off on this device (installer setting).")
    return _st("on", f"{_plural(cal, 'calibrated camera')} feed the heatmaps.")


def _insights(d) -> Dict[str, str]:
    if not d["zone_ids"]:
        return _st("setup", "Draw store areas on the map; recommendations are made per area.")
    topics = d.get("open_topics", 0)
    if not d.get("runs"):
        return _st("on", "Runs by itself every hour; no analysis has run yet.")
    return _st("on", f"{_plural(topics, 'open recommendation')}.")


def _sales(d) -> Dict[str, str]:
    if d.get("sales"):
        return _st("on", f"{_plural(d['sales'], 'sale')} received from the tills.")
    if (d.get("till_key") or {}).get("exists"):
        return _st("setup", "A till key is made, but no sales have arrived yet. Enter it in the till system.")
    return _st("off", "Optional. Not connected to the tills.")


def _cameras(d) -> Dict[str, str]:
    cams = d["cameras"]
    if not cams:
        return _st("setup", "No cameras yet.")
    on = [c for c in cams if c["on"]]
    bad = [c for c in on if not c["online"]]
    off = len(cams) - len(on)
    tail = f" {off} turned off." if off else ""
    if bad:
        return _st("setup", f"{len(on) - len(bad)} of {len(on)} cameras sending pictures; check \"{bad[0]['name']}\".{tail}")
    return _st("on", f"{_plural(len(on), 'camera')} working.{tail}")


def _purpose(d) -> Dict[str, str]:
    cams = d["cameras"]
    if not cams:
        return _st("setup", "Add cameras first.")
    missing = [c for c in cams if not c["role"]]
    if missing:
        return _st("setup", f"{len(missing)} of {len(cams)} still need a purpose, starting with \"{missing[0]['name']}\".")
    return _st("on", "Every camera has a purpose.")


def _map(d) -> Dict[str, str]:
    n = d["structures"]
    return _st("on", f"{_plural(n, 'room, wall or shelf', 'rooms, walls and shelves')} drawn.") if n \
        else _st("setup", "Nothing drawn yet.")


def _placement(d) -> Dict[str, str]:
    cams = d["cameras"]
    if not cams:
        return _st("setup", "Add cameras first.")
    unplaced = [c for c in cams if not c["placed"]]
    linked = [c for c in cams if not c["calibrated"] and c["watch_zone_id"] in d["zone_ids"]]
    cal = [c for c in cams if c["calibrated"]]
    left = [c for c in cams if not c["calibrated"] and c["watch_zone_id"] not in d["zone_ids"]]
    if unplaced:
        return _st("setup", f"{len(unplaced)} of {len(cams)} not on the map yet, starting with \"{unplaced[0]['name']}\".")
    if left:
        return _st("setup", f"{len(cal)} calibrated, {len(linked)} linked to an area, {len(left)} still to do.")
    return _st("on", f"All on the map: {len(cal)} calibrated, {len(linked)} linked to an area.")


def _areas(d) -> Dict[str, str]:
    n = len(d["zone_ids"])
    return _st("on", f"{_plural(n, 'store area')} drawn.") if n else _st("setup", "No store areas yet.")


def _privacy(d) -> Dict[str, str]:
    static_off = sum(1 for c in d["cameras"] if not c["features"].static_figure_filter)
    tail = f" Still-figure filter off on {_plural(static_off, 'camera')}." if static_off else \
        " Figures that never move are ignored on every camera."
    if not d["privacy_masks"] and not d["ignore_areas"]:
        return _st("off", "No privacy mask or ignore area drawn." + tail)
    return _st("on", f"{_plural(d['privacy_masks'], 'privacy mask')}, {_plural(d['ignore_areas'], 'ignore area')}." + tail)


def _substreams(d) -> Dict[str, str]:
    recs = d.get("recorders")
    if recs is None:
        return _st("off", "")
    if not recs:
        return _st("off", "No recorder cameras. Only needed for cameras added from a recorder.")
    if not all(recs):
        return _st("setup", f"{sum(1 for r in recs if not r)} of {len(recs)} recorders have no saved sign-in, "
                            "so their sub-streams cannot be checked.")
    return _st("on", f"{_plural(len(recs), 'recorder')} with a saved sign-in. Check the sub-streams once.")


def _online(d) -> Dict[str, str]:
    r = d["remote"]
    if not r["enabled"]:
        return _st("off", "Off. Usually set up by the installer.")
    if r["connected"]:
        return _st("on", "Connected and verified." if r["verified"] else "Connected. Press \"Verify now\" once.")
    return _st("setup", "Turned on but not connected right now.")


def _remote_video(d) -> Dict[str, str]:
    r = d["remote"]
    if not r["enabled"]:
        return _st("off", "Needs online access.")
    return _st("on", "Live video goes straight from this device to the viewer, only while someone watches.")


def _accounts(d) -> Dict[str, str]:
    n = len(d["accounts"])
    ops = sum(1 for a in d["accounts"] if a.role == "operator")
    if n <= 1:
        return _st("setup", "Only one account. Give each person their own.")
    return _st("on", f"{_plural(n, 'account')}, {_plural(ops, 'operator')}.")


def _phone_app(d) -> Dict[str, str]:
    n = d.get("paired", 0)
    return _st("on", f"{_plural(n, 'phone')} paired.") if n else \
        _st("off", "Optional backup app. Most stores use phone alerts instead.")


def _store(d) -> Dict[str, str]:
    name = str(_eff("STORE_NAME") or "").strip()
    tz = str(_eff("SITE_TIMEZONE") or "").strip()
    if name and name != "Store" and tz:
        return _st("on", f"{name} · {tz}")
    return _st("setup", "Pick the store's time zone." if name and name != "Store" else "Enter the store name.")


def _evidence(d) -> Dict[str, str]:
    ev = d.get("evidence") or {}
    used = ev.get("used_bytes")
    cap = ev.get("effective_cap_bytes") or ev.get("cap_bytes")
    days = ev.get("retention_days")
    if ev.get("status") == "refused":
        return _st("setup", "The evidence folder could not be checked. Open the full settings.")
    parts = []
    if isinstance(used, (int, float)) and isinstance(cap, (int, float)) and cap:
        parts.append(f"{used / 1024 ** 3:.1f} of {cap / 1024 ** 3:.1f} GB used")
    if days:
        parts.append(f"kept {days} days")
    return _st("on", ("; ".join(parts) + ".") if parts else "Pictures of alerts are kept on this device.")


def _backups(d) -> Dict[str, str]:
    lst = d.get("backups")
    if lst is None:
        return _st("on", "")
    manual = [b for b in lst if str(b.get("tag") or "").lower().startswith(("manual", "upload"))]
    if manual:
        return _st("on", f"{_plural(len(lst), 'backup')} on the device. Download one regularly.")
    return _st("setup", f"{_plural(len(lst), 'automatic backup')} on the device. Press \"Back up now\", then \"Download\".")


def _speed(d) -> Dict[str, str]:
    return _st("on", f"Uses up to {_pct(_eff("POSE_BUDGET_UTILISATION"))} of the detection hardware.")


def _support(d) -> Dict[str, str]:
    s = (d.get("fleet") or {}).get("support") or {}
    who = s.get("name") or "iKorex support"
    return _st("on", f"Contact: {who}. Report a problem from any screen through Help.")


def _plan(d) -> Dict[str, str]:
    f = d.get("fleet") or {}
    if not f.get("managed") and not f.get("configured"):
        return _st("off", "Not connected to iKorex. Every feature is on.")
    if f.get("paused"):
        return _st("setup", "Paused by iKorex. Settings and evidence are kept.")
    if f.get("licence_status") in ("grace", "expired"):
        return _st("setup", "The licence date has passed. Contact iKorex.")
    off = sum(1 for v in (f.get("features") or {}).values() if v is False)
    return _st("on", "Active." + (f" {_plural(off, 'feature')} not in the plan." if off else " Every feature is included."))


STATES: Dict[str, Callable[[Dict[str, Any]], Dict[str, str]]] = {
    "theft-alerts": _theft, "alert-levels": _levels, "area-line-alerts": _area_lines,
    "restricted-areas": _restricted, "night-watch": _night,
    "phone-alerts": _phones, "footfall": _footfall, "queues": _queues, "shelf-reaches": _shelves,
    "heatmaps": _heatmaps, "insights": _insights, "sales-data": _sales, "cameras": _cameras,
    "camera-purpose": _purpose, "store-map": _map, "placement-calibration": _placement, "store-areas": _areas,
    "privacy-ignore": _privacy, "sub-streams": _substreams, "online-access": _online,
    "remote-video": _remote_video, "accounts": _accounts, "phone-app": _phone_app, "store-settings": _store,
    "evidence": _evidence, "backups": _backups, "analysis-speed": _speed, "support": _support,
    "ikorex-plan": _plan,
}
assert set(STATES) == set(PLAN_KEYS)


def _links(d: Dict[str, Any]) -> Dict[str, Optional[str]]:
    cams = d["cameras"]
    first = lambda pred: next((c["id"] for c in cams if pred(c)), None)  # noqa: E731
    return {
        "first_camera": cams[0]["id"] if cams else None,
        "first_without_purpose": first(lambda c: not c["role"]),
        "first_to_calibrate": first(lambda c: not c["calibrated"]),
        "first_night_watch": first(lambda c: c["features"].night_watch and c["features"].night_watch.enabled)
        or (cams[0]["id"] if cams else None),
    }


@router.get("/status")
async def features_status(request: Request, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    plan = {k: entitlements.feature_enabled(k) for k in entitlements.FEATURES}
    admin = _caller_is_admin(request)
    data = await _collect(db, plan)
    out: Dict[str, Any] = {}
    for fid, fn in STATES.items():
        key = PLAN_KEYS[fid]
        if key and not plan.get(key, True):
            out[fid] = _st("plan_off")
            continue
        try:
            st = fn(data)
        except Exception:  # noqa: BLE001 - one broken reading must not hide the others
            logger.warning("feature status %s could not be read", fid, exc_info=True)
            st = _st("unknown", "Could not be checked.")
        if not admin and fid in ADMIN_DETAIL:
            st = {"state": st["state"], "detail": ""}
        out[fid] = st
    return {"features": out, "plan": plan, "links": _links(data), "admin": admin}
