"""Where each camera is in the "place it, then calibrate it" setup.

One source of truth for the three camera states every setup screen talks
about, so the Today banner, the Getting started checklist, the Features tab
and the Store map never disagree or mix them up:

* **not placed**  the camera is not on the store map yet (``map_placed`` is not
  True). It still counts people, but the map cannot show where it hangs or
  what it covers.
* **placed, not calibrated**  the camera is on the map but its picture is not
  matched to the floor (no homography). Its coverage is an estimate from the
  lens angle and reach, nobody it sees appears on the live map or heatmap, and
  measurements are in body heights instead of floor metres (speed and other
  restricted-area triggers, queue length, walking distance).
* **calibrated**  the picture is matched to the floor: people are placed on the
  map in metres.

Linking an uncalibrated camera to a store area ("Counts into zone") counts its
visits but is NOT calibration: it is reported separately and never makes the
calibration step complete.

The state is pure (dicts in, dict out) so tests can pin it, and the text is
written here once, in plain Australian English, with the counts taken from the
same rows.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

# Machine names of the next setup step, in the order the operator does them.
STEP_ADD = "add_cameras"
STEP_PLACE = "place"
STEP_CALIBRATE = "calibrate"
STEP_DONE = "done"

# What an uncalibrated camera cannot do; shown with the calibration warning.
UNCALIBRATED_LIMITS = ("the coverage drawn is an estimate from the lens angle, people it sees are not shown "
                       "on the live map or heatmap, and sizes, distances and speeds are guessed in body "
                       "heights instead of measured in floor metres")


def _plural(n: int, one: str, many: Optional[str] = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def camera_row(cam: Any) -> dict:
    """The four facts the state needs from a camera model row or a serialised camera dict."""
    get = (lambda k, d=None: cam.get(k, d)) if isinstance(cam, Mapping) else (lambda k, d=None: getattr(cam, k, d))
    cid = get("camera_id") or get("id")
    calibrated = get("has_homography")
    if calibrated is None:
        calibrated = get("calibrated")
    if calibrated is None:
        calibrated = bool(get("homography_matrix"))
    placed = get("placed")
    if placed is None:
        placed = get("map_placed")
    return {"id": cid, "name": get("name") or cid, "placed": placed is True,
            "calibrated": bool(calibrated), "watch_zone_id": get("watch_zone_id")}


def camera_setup_state(cameras: Iterable[Any], zone_ids: Optional[Iterable[str]] = None) -> dict:
    """Counts, next step and plain-language text for placing and calibrating cameras.

    ``cameras``: model rows or dicts (``camera_id``/``id``, ``name``, ``map_placed``
    or ``placed``, ``has_homography``/``calibrated``/``homography_matrix``,
    ``watch_zone_id``). ``zone_ids``: ids of the store areas that exist, to tell a
    live "Counts into zone" link from a dangling one; None counts any link.
    """
    rows = [camera_row(c) for c in cameras]
    zones = None if zone_ids is None else set(zone_ids)
    total = len(rows)
    unplaced = [r for r in rows if not r["placed"]]
    placed = [r for r in rows if r["placed"]]
    calibrated = [r for r in rows if r["calibrated"]]
    uncal = [r for r in rows if not r["calibrated"]]
    placed_uncal = [r for r in placed if not r["calibrated"]]
    linked = [r for r in uncal if r["watch_zone_id"] and (zones is None or r["watch_zone_id"] in zones)]

    def ref(r: Optional[dict]) -> Optional[dict]:
        return {"camera_id": r["id"], "name": r["name"]} if r else None

    # Calibrate a camera that is already on the map first: the calibration tool
    # pairs picture spots with map spots, which is easiest once it sits in place.
    first_uncal = (placed_uncal or uncal or [None])[0]
    counts = f"{_plural(len(placed), 'camera')} placed, {len(calibrated)} calibrated"
    if total and len(placed) < total:
        counts = f"{len(placed)} of {_plural(total, 'camera')} placed, {len(calibrated)} calibrated"

    if not total:
        step, level = STEP_ADD, "todo"
        headline = "No cameras yet."
        detail = "Add cameras first, then place each one on the store map and calibrate it."
        action = {"go": "add-cameras", "label": "Add cameras"}
    elif unplaced:
        step, level = STEP_PLACE, "todo" if not placed else "partial"
        headline = (f"{_plural(len(unplaced), 'camera')} not on the store map yet"
                    if placed else "No camera is on the store map yet")
        detail = (f"{counts}. Place each camera on the store map first (drag it from the camera gallery "
                  f"beside the map), starting with \"{unplaced[0]['name']}\". Then calibrate it: placing alone "
                  "does not put people on the map.")
        action = {"go": "place-cameras", "label": "Place cameras"}
    elif uncal:
        step, level = STEP_CALIBRATE, "todo" if not calibrated else "partial"
        headline = (f"{counts}: placing is done, calibration is not" if not calibrated
                    else f"{counts}: {_plural(len(uncal), 'camera')} still to calibrate")
        tail = (f" {_plural(len(linked), 'uncalibrated camera')} {'counts' if len(linked) == 1 else 'count'} "
                "visits into a linked area, but that is not calibration." if linked else "")
        which = ("none is calibrated yet" if not calibrated
                 else f"{len(uncal)} {'is' if len(uncal) == 1 else 'are'} not calibrated yet")
        detail = (f"Every camera is on the map, but {which}. Until a camera is calibrated {UNCALIBRATED_LIMITS}. Calibrate each camera by "
                  f"matching 4 floor spots in its picture to the map, starting with \"{first_uncal['name']}\".{tail}")
        action = {"go": f"calibrate:{first_uncal['id']}", "label": "Calibrate a camera"}
    else:
        step, level = STEP_DONE, "done"
        headline = f"{counts}."
        detail = "Every camera is on the store map and calibrated, so people are placed on the floor in metres."
        action = None

    return {
        "step": step,
        "level": level,             # "todo" | "partial" | "done"
        "total": total,
        "placed": len(placed),
        "unplaced": len(unplaced),
        "calibrated": len(calibrated),
        "uncalibrated": len(uncal),
        "placed_uncalibrated": len(placed_uncal),
        "zone_linked": len(linked),
        "first_unplaced": ref(unplaced[0] if unplaced else None),
        "first_uncalibrated": ref(first_uncal),
        "counts": counts if total else "No cameras yet",
        "headline": headline,
        "detail": detail,
        "action": action,
    }
