"""Site settings: store-specific values the operator sets in the dashboard.

``.env`` (``app/config.py``) holds generic device defaults. The values below
are specific to one store (its name, time zone, how much evidence it keeps,
which stock is high value, how often an alert may repeat), so they are set in
Settings > Store / Evidence storage / Alerts and detection and saved in the
database (``system_setup``, key ``site_settings``, JSON), the same way online
access keeps its settings. Being in the database they are part of every
backup.

How a saved value takes effect: :func:`apply` writes the effective value
(saved override, else the ``.env`` default captured when this module was first
imported) onto the shared ``settings`` object. Every consumer reads
``settings.X`` when it needs it (no value is copied at import time), so a
change applies to the next frame, alert or request without a restart. The few
consumers that cache something derived from a value are told here:

* ``THEFT_HIGH_VALUE_*``: the product-zone attribute cache and the theft
  engine's per-camera zone geometry are keyed on the shelf service's version,
  which is bumped.
* ``NIGHT_WATCH_CLIP``: the clip recorder's "keep a pre-event ring" answers
  are forgotten.
* evidence limits: one evidence-limit pass runs straight away, so a lowered
  limit deletes the oldest evidence now rather than at the next 15-minute pass.

Saved values are loaded when this module is imported (``app.main`` imports
the route at start-up, before the pipeline starts). A database restored from
a backup brings its own saved values; they apply after the next restart.
"""

from __future__ import annotations

import json
import logging
import math
import re
import shutil
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.config import settings

logger = logging.getLogger("edge.site_settings")

DB_KEY = "site_settings"
GB = 1024 ** 3
MB = 1024 ** 2

_CATEGORY_RE = re.compile(r"^[A-Z0-9][A-Z0-9_]{0,39}$")
MAX_CATEGORIES = 60


class SiteSettingsError(ValueError):
    """One or more values were refused. ``errors`` maps setting key -> reason."""

    def __init__(self, errors: Dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


@dataclass(frozen=True)
class Spec:
    key: str
    kind: str                    # str | timezone | int | float | bool | categories
    group: str                   # store | evidence | alerts
    label: str
    help: str
    min: Optional[float] = None
    max: Optional[float] = None
    unit: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


SPECS: Tuple[Spec, ...] = (
    # ---------------------------------------------------------------- store
    Spec("STORE_NAME", "str", "store", "Store name",
         "Shown in the page header and on the daily report.", 1, 80),
    Spec("SITE_TIMEZONE", "timezone", "store", "Time zone",
         "\"Today\", hourly charts, schedules and alert times use this zone. "
         "Empty means this device's own time zone."),
    # ------------------------------------------------------------- evidence
    Spec("EVIDENCE_MAX_GB", "float", "evidence", "Evidence size limit",
         "The most space evidence stills and clips may use. 0 means automatic: 10% of the disk, "
         "at least 1 GB and at most 50 GB. Above the limit the oldest evidence is deleted first.",
         0, 100_000, "GB", {"zero_means": "Automatic (10% of disk)", "min_nonzero": 0.5}),
    Spec("STORAGE_RETENTION_DAYS", "int", "evidence", "Keep evidence for (days)",
         "Evidence older than this is deleted. 0 means no age limit (only the size limits apply).",
         0, 730, "days", {"zero_means": "No age limit"}),
    Spec("STORAGE_MAX_DISK_PERCENT", "float", "evidence", "Never fill the disk beyond (%)",
         "Evidence is also trimmed so the whole disk stays below this. The database and the "
         "system need the rest.", 50, 95, "%"),
    Spec("NIGHT_WATCH_EVIDENCE_MAX_MB", "float", "evidence", "Night watch evidence limit (MB)",
         "Night watch stills and clips may use at most this much, inside the evidence size limit. "
         "0 means no separate limit.", 0, 1_000_000, "MB", {"zero_means": "No separate limit"}),
    Spec("NIGHT_WATCH_CLIP", "bool", "evidence", "Save a clip with each night-watch event",
         "A short clip (a few seconds before and after) is saved with each person alert. "
         "Clips use more space than stills."),
    # --------------------------------------------------------------- alerts
    Spec("THEFT_HIGH_VALUE_CATEGORIES", "categories", "alerts", "High-value categories",
         "Product areas with one of these categories count as high value: lingering there with "
         "repeated reaching raises a theft alert. Use the category names given to product areas."),
    Spec("THEFT_HIGH_VALUE_MIN_PRICE", "float", "alerts", "High value from price",
         "A product area priced at or above this counts as high value, whatever its category. "
         "0 means price is not used.", 0, 1_000_000, None, {"zero_means": "Price not used"}),
    Spec("TRIPWIRE_ALERT_COOLDOWN_SEC", "float", "alerts", "Line crossing: repeat alert after (seconds)",
         "After a person crosses an alert line, the same person on the same line does not alert "
         "again for this long. A line's own setting overrides it.", 5, 3600, "s"),
    Spec("RESTRICTED_AREA_COOLDOWN_SEC", "float", "alerts", "Restricted area: repeat alert after (seconds)",
         "After a person is seen in a restricted area, the same person in the same area does not "
         "alert again for this long. An area's own setting overrides it.", 10, 7200, "s"),
    Spec("THEFT_INCIDENT_COOLDOWN_SEC", "float", "alerts", "Theft: repeat alert after (seconds)",
         "After a theft alert, the same person does not raise the same kind of alert again for "
         "this long.", 10, 3600, "s"),
    Spec("THEFT_MIN_CONFIDENCE", "float", "alerts", "Theft minimum confidence (%)",
         "A possible theft below this confidence is not raised. Lower finds more but brings more "
         "false alerts. Camera roles with higher sensitivity lower it for their cameras.",
         0.1, 0.95, "fraction"),
    Spec("THEFT_EXIT_RULE_ENABLED", "bool", "alerts", "Check for exit without passing checkout",
         "Alert when a person who picked up stock walks out without passing a checkout. Needs a "
         "calibrated camera with entrance/exit and checkout areas."),
    Spec("QUEUE_CONGESTED_WAIT_SEC", "float", "alerts", "Queue congested after (seconds)",
         "A checkout lane is shown as congested when the average wait passes this.", 30, 1800, "s"),
)
SPECS_BY_KEY: Dict[str, Spec] = {s.key: s for s in SPECS}
GROUP_KEYS: Dict[str, List[str]] = {}
for _s in SPECS:
    GROUP_KEYS.setdefault(_s.group, []).append(_s.key)
EVIDENCE_KEYS = frozenset(GROUP_KEYS["evidence"]) - {"NIGHT_WATCH_CLIP"}
HIGH_VALUE_KEYS = frozenset({"THEFT_HIGH_VALUE_CATEGORIES", "THEFT_HIGH_VALUE_MIN_PRICE"})


def parse_categories(value: Any) -> List[str]:
    """A list or comma/space separated string -> unique uppercase words, in order."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,\s;]+", value)
    elif isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        raise ValueError("must be a list of category names")
    out: List[str] = []
    for p in parts:
        w = p.strip().upper().replace("-", "_").replace(" ", "_")
        if w and w not in out:
            out.append(w)
    return out


def _settings_form(spec: Spec, value: Any) -> Any:
    """The value as ``settings`` holds it (categories: a comma string, as consumers parse)."""
    if spec.kind == "categories":
        return ",".join(value)
    return value


def _api_form(spec: Spec, value: Any) -> Any:
    if spec.kind == "categories":
        return parse_categories(value)
    if spec.kind == "timezone" or spec.kind == "str":
        return (value or "").strip() if isinstance(value, str) else value
    return value


# ENV defaults: what app.config produced from the environment / .env, captured
# before any saved value is applied.
ENV_DEFAULTS: Dict[str, Any] = {s.key: _api_form(s, getattr(settings, s.key)) for s in SPECS}


def available_timezones() -> List[str]:
    try:
        from zoneinfo import available_timezones as _avail

        names = _avail()
    except Exception:  # noqa: BLE001
        return []
    return sorted(n for n in names if "/" in n and not n.startswith(("Etc/", "SystemV/", "posix/", "right/"))) + ["UTC"]


def disk_info() -> Dict[str, Optional[int]]:
    """Disk holding STORAGE_DIR plus the evidence on it (last evidence pass), in bytes."""
    out: Dict[str, Optional[int]] = {"total_bytes": None, "free_bytes": None, "used_bytes": None,
                                     "evidence_bytes": None}
    try:
        du = shutil.disk_usage(str(Path(settings.STORAGE_DIR)))
        out.update(total_bytes=du.total, free_bytes=du.free, used_bytes=du.used)
    except OSError:
        pass
    try:
        from app.services.evidence_storage import evidence_storage

        used = (evidence_storage.status() or {}).get("used_bytes")
        out["evidence_bytes"] = int(used) if isinstance(used, (int, float)) else None
    except Exception:  # noqa: BLE001
        pass
    return out


def max_evidence_gb(disk: Optional[Dict[str, Optional[int]]] = None) -> Optional[float]:
    """Largest sensible evidence limit: free space plus what evidence already uses."""
    disk = disk if disk is not None else disk_info()
    free = disk.get("free_bytes")
    if free is None:
        return None
    return round((free + (disk.get("evidence_bytes") or 0)) / GB, 1)


def _auto_cap_bytes(disk: Dict[str, Optional[int]]) -> Optional[int]:
    from app.services.evidence_storage import AUTO_CAP_FRACTION, AUTO_CAP_MAX_BYTES, AUTO_CAP_MIN_BYTES

    total = disk.get("total_bytes")
    if not total:
        return None
    return int(min(AUTO_CAP_MAX_BYTES, max(AUTO_CAP_MIN_BYTES, total * AUTO_CAP_FRACTION)))


# --------------------------------------------------------------------------- validation

def _number(spec: Spec, raw: Any) -> float:
    if isinstance(raw, bool) or raw is None or (isinstance(raw, str) and not raw.strip()):
        raise ValueError("enter a number")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ValueError("enter a number") from None
    if not math.isfinite(v):
        raise ValueError("enter a number")
    if spec.kind == "int":
        if v != int(v):
            raise ValueError("enter a whole number")
    lo, hi = spec.min, spec.max
    zero_ok = "zero_means" in spec.extra
    nz_min = spec.extra.get("min_nonzero")
    if zero_ok and v == 0:
        return 0 if spec.kind == "int" else 0.0
    floor = nz_min if nz_min is not None else lo
    if (floor is not None and v < floor) or (hi is not None and v > hi):
        u = f" {spec.unit}" if spec.unit and spec.unit not in ("fraction",) else ""
        rng = f"{_fmt(floor)}-{_fmt(hi)}{u}"
        raise ValueError(f"must be 0 or {rng}" if zero_ok else f"must be {rng}")
    return int(v) if spec.kind == "int" else v


def _fmt(v: Optional[float]) -> str:
    if v is None:
        return "?"
    return f"{v:g}"


def validate_one(key: str, raw: Any) -> Any:
    """Normalised API value for one setting, or ValueError with a plain reason."""
    spec = SPECS_BY_KEY.get(key)
    if spec is None:
        raise KeyError(key)
    if spec.kind == "str":
        if not isinstance(raw, str):
            raise ValueError("enter a name")
        v = " ".join(raw.split())
        if not v:
            raise ValueError("enter a name")
        if len(v) > int(spec.max or 80):
            raise ValueError(f"at most {int(spec.max or 80)} characters")
        if any(ord(c) < 32 for c in v):
            raise ValueError("contains control characters")
        return v
    if spec.kind == "timezone":
        if raw is None:
            return ""
        if not isinstance(raw, str):
            raise ValueError("enter a time zone name such as Australia/Melbourne")
        v = raw.strip()
        if not v:
            return ""
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(v)
        except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError, ValueError for odd input
            raise ValueError(f"{v!r} is not a known time zone (e.g. Australia/Melbourne)") from None
        if v not in available_timezones() and v != "UTC":
            raise ValueError(f"{v!r} is not a known time zone (e.g. Australia/Melbourne)")
        return v
    if spec.kind == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.strip().lower() in ("true", "false"):
            return raw.strip().lower() == "true"
        raise ValueError("must be on or off")
    if spec.kind == "categories":
        cats = parse_categories(raw)
        bad = [c for c in cats if not _CATEGORY_RE.match(c)]
        if bad:
            raise ValueError("use single words of letters, digits and _ (e.g. SPIRITS, BABY_FORMULA): "
                             + ", ".join(bad[:5]))
        if len(cats) > MAX_CATEGORIES:
            raise ValueError(f"at most {MAX_CATEGORIES} categories")
        return cats
    return _number(spec, raw)


def _cross_check(effective: Dict[str, Any], changed: Dict[str, Any], errors: Dict[str, str]) -> None:
    """Checks between values and against the disk."""
    if not ({"EVIDENCE_MAX_GB", "NIGHT_WATCH_EVIDENCE_MAX_MB"} & set(changed)):
        return
    disk = disk_info()
    gb = float(effective.get("EVIDENCE_MAX_GB") or 0)
    if "EVIDENCE_MAX_GB" in changed and gb > 0:
        most = max_evidence_gb(disk)
        if most is not None and gb > most:
            errors["EVIDENCE_MAX_GB"] = (
                f"this disk has room for at most {most:g} GB of evidence (free space plus the evidence "
                "already kept); choose less or Automatic")
    total_cap = int(gb * GB) if gb > 0 else _auto_cap_bytes(disk)
    nw = float(effective.get("NIGHT_WATCH_EVIDENCE_MAX_MB") or 0)
    if nw > 0 and total_cap is not None and nw * MB > total_cap and "NIGHT_WATCH_EVIDENCE_MAX_MB" not in errors:
        key = "NIGHT_WATCH_EVIDENCE_MAX_MB" if "NIGHT_WATCH_EVIDENCE_MAX_MB" in changed else "EVIDENCE_MAX_GB"
        errors[key] = (f"the night watch limit ({nw:g} MB) cannot be above the evidence size limit "
                       f"({total_cap / MB:,.0f} MB)")


# --------------------------------------------------------------------------- store

class SiteSettingsStore:
    """Saved overrides (database) applied onto ``settings``."""

    def __init__(self, db_path_fn: Callable[[], Path] = lambda: Path(settings.DATABASE_PATH)) -> None:
        self._db_path_fn = db_path_fn
        self._lock = threading.RLock()
        self.overrides: Dict[str, Any] = {}
        self.changed: Dict[str, Dict[str, Any]] = {}   # key -> {"at", "by"}
        self.loaded = False

    # ---------------------------------------------------------------- persistence

    def _read(self) -> Optional[dict]:
        path = self._db_path_fn()
        if not path.exists():
            return None
        try:
            conn = sqlite3.connect(str(path), timeout=5.0)
            try:
                row = conn.execute("SELECT value FROM system_setup WHERE key = ?", (DB_KEY,)).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as exc:   # no system_setup yet (fresh database): nothing saved
            logger.debug(f"site settings not read: {exc}")
            return None
        if not row:
            return None
        try:
            data = json.loads(row[0])
        except ValueError:
            logger.warning("Saved site settings are not valid JSON; using the .env defaults")
            return None
        return data if isinstance(data, dict) else None

    def _write(self) -> None:
        payload = json.dumps({"values": self.overrides, "changed": self.changed})
        conn = sqlite3.connect(str(self._db_path_fn()), timeout=10.0)
        try:
            conn.execute(
                "INSERT INTO system_setup (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (DB_KEY, payload, datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")),
            )
            conn.commit()
        finally:
            conn.close()

    def load(self) -> Dict[str, Any]:
        """Read the saved overrides and apply them. A saved value that no longer validates is ignored."""
        with self._lock:
            data = self._read() or {}
            values = data.get("values") if isinstance(data.get("values"), dict) else {}
            changed = data.get("changed") if isinstance(data.get("changed"), dict) else {}
            good: Dict[str, Any] = {}
            for k, raw in values.items():
                if k not in SPECS_BY_KEY:
                    continue
                try:
                    good[k] = validate_one(k, raw)
                except (ValueError, KeyError) as exc:
                    logger.warning(f"Saved site setting {k}={raw!r} ignored ({exc}); using the .env default")
            self.overrides = good
            self.changed = {k: v for k, v in changed.items() if k in good and isinstance(v, dict)}
            self.loaded = True
            self.apply()
            if good:
                logger.info(f"Site settings from the dashboard: {', '.join(sorted(good))}")
            return dict(good)

    # ---------------------------------------------------------------- effect

    def effective(self, key: str) -> Any:
        return self.overrides[key] if key in self.overrides else ENV_DEFAULTS[key]

    def apply(self, changed_keys: Optional[set] = None) -> None:
        """Write every effective value onto ``settings``; tell caching consumers about changes."""
        for spec in SPECS:
            setattr(settings, spec.key, _settings_form(spec, self.effective(spec.key)))
        if changed_keys:
            _after_change(set(changed_keys))

    # ---------------------------------------------------------------- changes

    def update(self, values: Dict[str, Any], actor: str = "unknown") -> Dict[str, Any]:
        """Validate and save ``values`` (key -> new value; None = back to the default).

        All or nothing: any refused value refuses the whole change
        (:class:`SiteSettingsError`). Returns {key: (old, new)} for what changed.
        """
        errors: Dict[str, str] = {}
        clean: Dict[str, Any] = {}
        resets: List[str] = []
        for k, raw in (values or {}).items():
            if k not in SPECS_BY_KEY:
                errors[k] = "not a site setting"
                continue
            if raw is None:
                resets.append(k)
                continue
            try:
                clean[k] = validate_one(k, raw)
            except ValueError as exc:
                errors[k] = str(exc)
        with self._lock:
            effective = {s.key: self.effective(s.key) for s in SPECS}
            effective.update(clean)
            for k in resets:
                effective[k] = ENV_DEFAULTS[k]
            if not errors:
                _cross_check(effective, {**clean, **{k: None for k in resets}}, errors)
            if errors:
                raise SiteSettingsError(errors)
            before = {s.key: self.effective(s.key) for s in SPECS}
            new_over = dict(self.overrides)
            for k, v in clean.items():
                # A value equal to the default is still kept as an override when
                # set explicitly: the .env default can change later.
                new_over[k] = v
            for k in resets:
                new_over.pop(k, None)
            # Changed = a different effective value, or the override was added / removed.
            diff: Dict[str, Tuple[Any, Any]] = {}
            for k in set(clean) | set(resets):
                new = new_over[k] if k in new_over else ENV_DEFAULTS[k]
                if before[k] != new or (k in new_over) != (k in self.overrides):
                    diff[k] = (before[k], new)
            if not diff:
                return {}
            now = datetime.now(timezone.utc).isoformat()
            old_over, old_changed = self.overrides, self.changed
            self.overrides = new_over
            self.changed = {k: v for k, v in self.changed.items() if k in new_over}
            for k in clean:
                self.changed[k] = {"at": now, "by": actor}
            try:
                self._write()
            except sqlite3.Error:
                self.overrides, self.changed = old_over, old_changed
                raise
            self.apply({k for k, (a, b) in diff.items() if a != b})
        for k, (a, b) in sorted(diff.items()):
            how = "reset to the .env default" if k in resets else "set"
            logger.info(f"Site setting {k} {how} by {actor}: {a!r} -> {b!r}")
        return diff

    def reset(self, key: str, actor: str = "unknown") -> Dict[str, Any]:
        if key not in SPECS_BY_KEY:
            raise KeyError(key)
        return self.update({key: None}, actor)

    # ---------------------------------------------------------------- reporting

    def describe(self) -> Dict[str, Any]:
        disk = disk_info()
        most = max_evidence_gb(disk)
        items: Dict[str, Any] = {}
        for spec in SPECS:
            mx = spec.max
            if spec.key == "EVIDENCE_MAX_GB" and most is not None:
                mx = min(spec.max or most, most)
            items[spec.key] = {
                "key": spec.key, "group": spec.group, "label": spec.label, "help": spec.help,
                "type": spec.kind, "value": self.effective(spec.key), "default": ENV_DEFAULTS[spec.key],
                "overridden": spec.key in self.overrides,
                "min": spec.extra.get("min_nonzero", spec.min), "max": mx, "unit": spec.unit,
                "zero_means": spec.extra.get("zero_means"),
                "applies": "live",
                "changed_at": (self.changed.get(spec.key) or {}).get("at"),
                "changed_by": (self.changed.get(spec.key) or {}).get("by"),
            }
        # What "Automatic (10% of disk)" means on this disk, so the dashboard can
        # tell whether switching to it (or resetting to it) lowers the limit.
        auto = _auto_cap_bytes(disk)
        return {"settings": items, "groups": GROUP_KEYS, "store_time": store_time(),
                "disk": {**disk, "max_evidence_gb": most, "auto_evidence_cap_bytes": auto,
                         "auto_evidence_gb": round(auto / GB, 2) if auto else None}}


def store_time() -> Dict[str, Any]:
    from app.services.timeutil import local_now

    now = local_now()
    name = (settings.SITE_TIMEZONE or "").strip()
    source = "site"
    if not name:
        source = "host"
        try:
            from app.services.night_watch import _host_zone_name

            name = _host_zone_name() or now.tzname() or ""
        except Exception:  # noqa: BLE001
            name = now.tzname() or ""
    off = now.utcoffset()
    return {"zone": name, "source": source, "now": now.replace(microsecond=0).isoformat(),
            "abbreviation": now.tzname(),
            "utc_offset_min": int(off.total_seconds() // 60) if off is not None else None}


# --------------------------------------------------------------------------- side effects

def _after_change(keys: set) -> None:
    if keys & HIGH_VALUE_KEYS:
        try:
            from app.services.shelf_interaction_service import shelf_interaction_service as sis

            with sis.lock:
                sis.version += 1    # attribute cache + theft engine zone geometry rebuild
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not refresh product-zone values after a high-value change: {exc}")
    if "NIGHT_WATCH_CLIP" in keys:
        try:
            from app.services.clip_recorder import clip_recorder_service

            clip_recorder_service.invalidate()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not refresh clip buffers after a night-watch clip change: {exc}")
    if keys & EVIDENCE_KEYS:
        run_evidence_pass()


def run_evidence_pass() -> None:
    """One evidence-limit pass in the background with the new limits."""
    try:
        from app.services.evidence_storage import evidence_storage
    except Exception:  # noqa: BLE001
        return
    threading.Thread(target=evidence_storage._background_enforce,
                     name="evidence-limit-settings", daemon=True).start()


site_settings = SiteSettingsStore()
try:
    site_settings.load()
except Exception as _exc:  # noqa: BLE001 - never block start-up; the .env defaults stand
    logger.error(f"Could not load the saved site settings: {_exc}; using the .env defaults")
