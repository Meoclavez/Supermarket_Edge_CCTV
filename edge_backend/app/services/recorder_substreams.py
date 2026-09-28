"""Upgrade a Dahua recorder's CIF sub-streams to D1, with a verified rollback.

The operator starts this from the dashboard (Settings -> Recorder sub-streams);
nothing here runs on its own. For each chosen channel, one at a time:

1. Read the channel's sub-stream settings (``Encode[ch-1].ExtraFormat[0]``) and
   its capabilities. A channel already at D1 or more, or whose camera does
   not list D1, is left alone and reported so.
2. Save the channel's complete current sub-stream settings to
   ``STORAGE_DIR/dahua_substreams_<host>.json`` (0600) for Restore.
3. ``setConfig`` the D1 size (704x576, or 704x480 on an NTSC recorder), and a
   D1 bit rate only if the current one is CIF-level; codec, FPS and GOP stay.
4. Re-read the settings, then open the real RTSP sub-stream (``subtype=1``)
   and check it delivers the D1 size within a bounded time.
5. Any failure (refused, did not stick, no stream, wrong size) writes the
   saved settings back. If even that fails, the channel is left as it is and
   the outcome says so.

Afterwards each affected camera's worker is restarted once, so it reopens on
the new stream instead of whatever it fell back to while the recorder switched.
A rejected login stops the whole run at once, without retries (Dahua locks
the account after about five failed logins). Every write is appended to
``STORAGE_DIR/recorder_audit.jsonl`` (who, when, channel, before/after).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional
from urllib.parse import quote, urlsplit

from app.config import settings
from app.services import dahua_config as dc
from app.services.stream_selection import parse_dahua

logger = logging.getLogger(__name__)

AUDIT_FILE = "recorder_audit.jsonl"
OUTCOME_TEXT_PENDING = "Waiting"

# Outcome codes (text is built per channel).
SWITCHED = "switched"
ALREADY = "already_d1"
UNSUPPORTED = "unsupported"
CAPS_UNKNOWN = "caps_unknown"
REFUSED = "refused"
RESTORED = "restored"
RESTORE_FAILED = "restore_failed"
NOT_FOUND = "not_found"
ERROR = "error"
STOPPED = "stopped"
NOT_ATTEMPTED = "not_attempted"
RESTORE_OK = "restore_ok"
NOTHING_TO_RESTORE = "nothing_to_restore"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_host(host: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", host or "")[:80]


def _write_private_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        pass
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def cameras_by_recorder(cameras: Iterable[dict]) -> dict[str, list[dict]]:
    """Group Dahua channel cameras by recorder host.

    ``cameras``: dicts with ``id``, ``name``, ``rtsp_url`` (credential-free),
    optionally ``enabled`` and ``stream_quality``.
    """
    out: dict[str, list[dict]] = {}
    for cam in cameras:
        url = cam.get("rtsp_url") or ""
        parsed = parse_dahua(url)
        if parsed is None:
            continue
        try:
            parts = urlsplit(url)
            host, port = parts.hostname, parts.port or 554
        except ValueError:
            continue
        if not host:
            continue
        ch, subtype = parsed
        out.setdefault(host, []).append({**cam, "channel": ch, "subtype": subtype, "rtsp_port": port})
    for cams in out.values():
        cams.sort(key=lambda c: (c["channel"], c.get("name") or ""))
    return out


ProbeFn = Callable[[str], Awaitable[dict]]
ChangedFn = Callable[[list[dict], Optional[tuple[int, int]]], Awaitable[None]]


async def _default_probe(target: str) -> dict:
    from app.services.camera_source import probe_stream_isolated

    return await probe_stream_isolated(target, "rtsp", 8.0)


async def _default_on_changed(cams: list[dict], verified_size: Optional[tuple[int, int]]) -> None:
    """Record the channel's new sub-stream size and restart its cameras' workers once."""
    from app.services.live_analytics_engine import live_engine
    from app.services.pipeline_supervisor import pipeline_supervisor
    from app.services.stream_selection import stream_selection

    for cam in cams:
        if verified_size:
            stream_selection.record(cam["id"], cam["rtsp_url"], 1, width=verified_size[0],
                                    height=verified_size[1], source="verified")
        else:
            stream_selection.forget(cam["id"], (1,))
        rt = live_engine.runtimes.get(cam["id"])
        if rt is not None and rt.enabled:
            live_engine.stop_camera(cam["id"])
    if getattr(pipeline_supervisor, "_running", False):
        try:
            await asyncio.wait_for(pipeline_supervisor.reconcile_cameras(), timeout=15)
        except Exception as exc:  # noqa: BLE001 - the periodic reconcile restarts them
            logger.debug(f"reconcile after sub-stream change: {exc}")


class RecorderBusy(RuntimeError):
    pass


class RecorderSubstreams:
    def __init__(self, storage_dir: Optional[Path] = None):
        self._storage_dir = storage_dir
        self.client_factory: Callable[..., dc.DahuaHttpClient] = dc.DahuaHttpClient
        self.probe_fn: ProbeFn = _default_probe
        self.on_channel_changed: ChangedFn = _default_on_changed
        self.channel_delay_s = 1.5
        self.read_delay_s = 0.2
        self.verify_initial_wait_s = 4.0
        self.verify_attempts = 4
        self.verify_gap_s = 4.0
        self._runs: dict[str, dict] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        # Per recorder: also try D1 on channels whose capabilities cannot be read
        # (the change is still verified on the stream and restored on failure).
        self._try_unknown: dict[str, bool] = {}

    # -- storage ---------------------------------------------------------------
    @property
    def storage_dir(self) -> Path:
        return Path(self._storage_dir or settings.STORAGE_DIR)

    def state_path(self, host: str) -> Path:
        return self.storage_dir / f"dahua_substreams_{_safe_host(host)}.json"

    def _load_state(self, host: str) -> dict:
        try:
            data = json.loads(self.state_path(host).read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("backups", {})
                return data
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            logger.warning(f"Unreadable sub-stream backup for {host}: {e}")
        return {"host": host, "backups": {}}

    def _save_state(self, host: str, data: dict) -> None:
        data["host"] = host
        _write_private_json(self.state_path(host), data)

    def backups(self, host: str) -> dict[str, dict]:
        return dict(self._load_state(host).get("backups") or {})

    def _save_backup(self, host: str, channel: int, sub: dict[str, str], actor: str) -> None:
        state = self._load_state(host)
        state["backups"][str(int(channel))] = {"saved_at": _now_iso(), "saved_by": actor, "config": dict(sub)}
        self._save_state(host, state)

    def _audit(self, host: str, channel: int, actor: str, action: str, before: dict, after: dict,
               result: str) -> None:
        entry = {"at": _now_iso(), "by": actor, "recorder": host, "channel": int(channel),
                 "action": action, "before": before, "after": after, "result": result}
        logger.info(f"Recorder change by {actor}: {host} channel {channel} {action} -> {result} "
                    f"(before {before}, after {after})")
        path = self.storage_dir / AUDIT_FILE
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, sort_keys=True) + "\n")
        except OSError as e:
            logger.error(f"Could not write the recorder audit log: {e}")

    def audit_entries(self, host: str, limit: int = 50) -> list[dict]:
        path = self.storage_dir / AUDIT_FILE
        out: list[dict] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError):
            return out
        for line in reversed(lines):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("recorder") == host:
                out.append(e)
                if len(out) >= limit:
                    break
        return out

    # -- runs --------------------------------------------------------------------
    def last_run(self, host: str) -> Optional[dict]:
        run = self._runs.get(host)
        if run is None:
            run = self._load_state(host).get("last_run")
        return json.loads(json.dumps(run)) if run else None

    def is_running(self, host: str) -> bool:
        t = self._tasks.get(host)
        return t is not None and not t.done()

    def _publish(self, host: str, run: dict) -> None:
        self._runs[host] = run
        try:
            state = self._load_state(host)
            state["last_run"] = run
            self._save_state(host, state)
        except OSError as e:
            logger.warning(f"Could not save the sub-stream run for {host}: {e}")

    def start(self, kind: str, host: str, cams: list[dict], channels: Optional[list[int]], actor: str,
              *, username: str, password: str, http_port: int = 80, try_unknown: bool = False) -> dict:
        """Start an upgrade or restore in the background. Raises RecorderBusy."""
        if self.is_running(host):
            raise RecorderBusy(f"a sub-stream change on {host} is already running")
        self._try_unknown[host] = bool(try_unknown) and kind == "upgrade"
        coro = (self.run_upgrade if kind == "upgrade" else self.run_restore)(
            host, cams, channels, actor, username=username, password=password, http_port=http_port)
        run = self._new_run(kind, host, cams, channels, actor)
        self._publish(host, run)
        self._tasks[host] = asyncio.get_running_loop().create_task(coro, name=f"recorder-{kind}-{host}")
        return run

    def _new_run(self, kind: str, host: str, cams: list[dict], channels: Optional[list[int]], actor: str) -> dict:
        return {
            "id": uuid.uuid4().hex[:12], "kind": kind, "recorder": host, "by": actor,
            "started_at": _now_iso(), "finished_at": None, "state": "running",
            "message": "Starting", "channels": [],
        }

    # -- helpers ---------------------------------------------------------------
    @staticmethod
    def _channel_entry(ch: int, cams: list[dict]) -> dict:
        on_ch = [c for c in cams if c["channel"] == ch]
        return {"channel": ch, "cameras": [c.get("name") or c["id"] for c in on_ch],
                "camera_ids": [c["id"] for c in on_ch], "outcome": "pending", "text": OUTCOME_TEXT_PENDING,
                "before": None, "after": None}

    async def _call(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    async def _standard(self, client: dc.DahuaHttpClient, enc: dict[str, str]) -> str:
        try:
            std = dc.video_standard(await self._call(client.get_config, "VideoStandard"))
            if std:
                return std
        except dc.DahuaHttpError:
            pass
        for ch in dc.channels_in_config(enc):
            size = dc.substream_size(dc.channel_substream(enc, ch))
            if size and size[1] in (120, 240, 480):
                return "NTSC"
        return "PAL"

    async def _caps(self, client: dc.DahuaHttpClient, ch: int, standard: str,
                    cache: dict) -> tuple[Optional[list[tuple[int, int]]], Optional[int], str]:
        err = ""
        try:
            caps = await self._call(client.get_encode_caps, ch)
        except dc.DahuaHttpError as e:
            caps, err = {}, str(e)
        sizes = dc.caps_resolutions(caps, ch, standard)
        if sizes is not None:
            return sizes, dc.caps_max_bitrate(caps, ch), ""
        if "encode_caps" not in cache:
            try:
                cache["encode_caps"] = await self._call(client.get_config, "EncodeCaps")
            except dc.DahuaHttpError as e:
                cache["encode_caps"] = {}
                err = err or str(e)
        sizes = dc.caps_resolutions(cache["encode_caps"], ch, standard)
        if sizes is not None:
            return sizes, dc.caps_max_bitrate(cache["encode_caps"], ch), ""
        return None, None, err or "the recorder did not report them"

    def _rtsp_target(self, host: str, port: int, ch: int, username: str, password: str) -> str:
        auth = f"{quote(username, safe='')}:{quote(password, safe='')}@" if username else ""
        return f"rtsp://{auth}{host}:{port}/cam/realmonitor?channel={ch}&subtype=1"

    async def _verify_stream(self, target: str, want: tuple[int, int]) -> tuple[bool, str]:
        if self.verify_initial_wait_s:
            await asyncio.sleep(self.verify_initial_wait_s)
        last = "no picture"
        for attempt in range(max(1, self.verify_attempts)):
            if attempt and self.verify_gap_s:
                await asyncio.sleep(self.verify_gap_s)
            try:
                res = await self.probe_fn(target)
            except Exception as exc:  # noqa: BLE001
                res = {"success": False, "error": type(exc).__name__}
            if res.get("success") and res.get("width") and res.get("height"):
                got = (int(res["width"]), int(res["height"]))
                if got == tuple(want):
                    return True, dc.size_label(got)
                last = f"stream is {dc.size_label(got)}, not D1"
            else:
                last = f"sub-stream did not come up ({res.get('error_code') or res.get('error') or 'no picture'})"
        return False, last

    async def _restore(self, client: dc.DahuaHttpClient, ch: int, saved: dict[str, str],
                       standard: str) -> tuple[bool, str, Optional[tuple[int, int]]]:
        assign = dc.restore_assignments(ch, saved)
        if not assign:
            return False, "no saved size to write back", None
        ok, reason = await self._call(client.set_config, assign)
        if not ok:
            return False, f"recorder refused the restore ({reason})", None
        try:
            enc = await self._call(client.get_config, "Encode")
        except dc.DahuaHttpError as e:
            return False, f"could not re-read after restore ({e})", None
        now = dc.substream_size(dc.channel_substream(enc, ch), standard)
        want = dc.substream_size(saved, standard)
        if want and now != want:
            return False, f"recorder reports {dc.size_label(now)} after restore", now
        return True, dc.size_label(now), now

    # -- preview (read-only) ---------------------------------------------------
    async def preview(self, host: str, cams: list[dict], *, username: str, password: str,
                      http_port: int = 80) -> dict:
        client = self.client_factory(host, username, password, port=http_port)
        backups = self.backups(host)
        channels = sorted({c["channel"] for c in cams})
        rows: list[dict] = []
        try:
            enc = await self._call(client.get_config, "Encode")
            standard = await self._standard(client, enc)
            target = dc.d1_size(standard)
            cache: dict = {}
            for i, ch in enumerate(channels):
                if i and self.read_delay_s:
                    await asyncio.sleep(self.read_delay_s)
                row = self._channel_entry(ch, cams)
                row.pop("outcome"), row.pop("text"), row.pop("before"), row.pop("after")
                sub = dc.channel_substream(enc, ch)
                size = dc.substream_size(sub, standard)
                row.update(current=dc.size_label(size) if sub else None,
                           width=size[0] if size else None, height=size[1] if size else None,
                           bitrate_kbps=_int(sub.get("Video.BitRate")), has_backup=str(ch) in backups)
                if not sub:
                    row.update(plan="not_found", plan_text="The recorder has no sub-stream settings for this channel")
                elif dc.is_d1_or_higher(size):
                    row.update(plan="already", plan_text="Already D1 or higher")
                else:
                    sizes, _, err = await self._caps(client, ch, standard, cache)
                    row["supported"] = [dc.size_label(s) for s in sizes] if sizes else None
                    if sizes is None:
                        row.update(plan="unknown", plan_text=f"Supported sizes unknown ({err}); will be skipped")
                    elif target in sizes:
                        row.update(plan="upgrade", plan_text=f"Will switch to {dc.size_label(target)}")
                    else:
                        row.update(plan="unsupported", plan_text="D1 not supported by this camera")
                rows.append(row)
        except dc.DahuaAuthError as e:
            return {"recorder": host, "ok": False, "auth_failed": True,
                    "error": f"{e}. Stopped at once to avoid locking the account; check the recorder "
                             "sign-in saved under Dahua recorder.", "channels": []}
        except dc.DahuaHttpError as e:
            return {"recorder": host, "ok": False, "auth_failed": False, "error": str(e), "channels": rows}
        finally:
            client.close()
        return {"recorder": host, "ok": True, "standard": standard, "target": dc.size_label(target),
                "channels": rows}

    # -- upgrade ---------------------------------------------------------------
    async def run_upgrade(self, host: str, cams: list[dict], channels: Optional[list[int]], actor: str, *,
                          username: str, password: str, http_port: int = 80) -> dict:
        run = self._runs.get(host) if self.is_running(host) else None
        if run is None or run.get("kind") != "upgrade" or run.get("state") != "running":
            run = self._new_run("upgrade", host, cams, channels, actor)
        wanted = sorted({c["channel"] for c in cams} if channels is None else set(int(c) for c in channels))
        run["channels"] = [self._channel_entry(ch, cams) for ch in wanted]
        run["message"] = "Reading the recorder's stream settings"
        self._publish(host, run)
        client = self.client_factory(host, username, password, port=http_port)
        try:
            await self._upgrade_all(run, client, host, cams, channels is None, actor,
                                    username=username, password=password)
        except Exception as exc:  # noqa: BLE001 - a run must end with a state
            logger.exception(f"Sub-stream upgrade on {host} failed")
            run["state"] = "failed"
            run["message"] = f"Stopped by an internal error ({type(exc).__name__}); channels not reached keep their settings"
            for e in run["channels"]:
                if e["outcome"] == "pending":
                    e.update(outcome=NOT_ATTEMPTED, text="not attempted")
        finally:
            client.close()
            run["finished_at"] = _now_iso()
            self._publish(host, run)
        return run

    async def _upgrade_all(self, run: dict, client: dc.DahuaHttpClient, host: str, cams: list[dict],
                           only_cif: bool, actor: str, *, username: str, password: str) -> None:
        try:
            enc = await self._call(client.get_config, "Encode")
            standard = await self._standard(client, enc)
        except dc.DahuaAuthError as e:
            self._stop_auth(run, str(e))
            return
        except dc.DahuaHttpError as e:
            run.update(state="failed", message=f"Could not read the recorder's stream settings: {e}. Nothing changed.")
            for entry in run["channels"]:
                entry.update(outcome=NOT_ATTEMPTED, text="not attempted: nothing changed")
            return
        target = dc.d1_size(standard)
        run["standard"] = standard
        run["target"] = dc.size_label(target)
        if only_cif:
            # "all CIF": only channels whose sub-stream is below D1 are touched.
            keep = []
            for entry in run["channels"]:
                size = dc.substream_size(dc.channel_substream(enc, entry["channel"]), standard)
                if size is None or not dc.is_d1_or_higher(size):
                    keep.append(entry)
            run["channels"] = keep
        cache: dict = {}
        http_errors = 0
        done = 0
        for i, entry in enumerate(run["channels"]):
            if i and self.channel_delay_s:
                await asyncio.sleep(self.channel_delay_s)
            ch = entry["channel"]
            run["message"] = f"Channel {ch} ({i + 1} of {len(run['channels'])})"
            entry.update(outcome="working", text="Working")
            self._publish(host, run)
            on_ch = [c for c in cams if c["channel"] == ch]
            rtsp_port = next((c["rtsp_port"] for c in on_ch), 554)
            try:
                code, text, changed, verified = await self._upgrade_channel(
                    client, host, ch, enc, standard, target, cache, actor, entry,
                    self._rtsp_target(host, rtsp_port, ch, username, password))
                http_errors = 0
            except dc.DahuaAuthError as e:
                entry.update(outcome=STOPPED, text=f"stopped: {e}")
                self._stop_auth(run, str(e))
                return
            except dc.DahuaHttpError as e:
                http_errors += 1
                code, text, changed, verified = ERROR, f"kept current settings: the recorder did not answer ({e})", False, None
            entry.update(outcome=code, text=text)
            done += 1
            if changed:
                try:
                    await self.on_channel_changed(on_ch, verified)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"Could not restart the workers of channel {ch}: {exc}")
            self._publish(host, run)
            if http_errors >= 2:
                run.update(state="failed", message="Stopped: the recorder stopped answering. Channels not reached keep their settings.")
                for e in run["channels"]:
                    if e["outcome"] == "pending":
                        e.update(outcome=NOT_ATTEMPTED, text="not attempted")
                return
        switched = sum(1 for e in run["channels"] if e["outcome"] == SWITCHED)
        run.update(state="done", message=(f"Done: {switched} of {len(run['channels'])} channel(s) switched to D1"
                                          if run["channels"] else "No channel below D1 to upgrade"))

    def _stop_auth(self, run: dict, why: str) -> None:
        run.update(state="stopped", auth_failed=True,
                   message=f"Stopped: {why}. No further logins were tried, to avoid locking the account. "
                           "Check the recorder sign-in saved under Dahua recorder.")
        for e in run["channels"]:
            if e["outcome"] in ("pending", "working"):
                e.update(outcome=NOT_ATTEMPTED, text="not attempted: the recorder rejected the sign-in")

    async def _upgrade_channel(self, client, host, ch, enc, standard, target, cache, actor, entry,
                               rtsp_target) -> tuple[str, str, bool, Optional[tuple[int, int]]]:
        """(outcome, text, recorder config changed?, verified D1 size or None)."""
        sub = dc.channel_substream(enc, ch)
        if not sub:
            return NOT_FOUND, "kept current: the recorder has no sub-stream settings for this channel", False, None
        cur = dc.substream_size(sub, standard)
        cur_txt = dc.size_label(cur)
        entry["before"] = cur_txt
        if dc.is_d1_or_higher(cur):
            entry["after"] = cur_txt
            return ALREADY, f"already D1 or higher ({cur_txt})", False, None
        sizes, max_rate, err = await self._caps(client, ch, standard, cache)
        kept = f"kept {'CIF' if cur in ((352, 288), (352, 240)) else cur_txt}"
        tried_unknown = False
        if sizes is None:
            if not self._try_unknown.get(host):
                return CAPS_UNKNOWN, f"{kept}: could not read the camera's supported resolutions ({err})", False, None
            # Operator asked to try anyway: the change is verified on the
            # stream below and restored if the camera does not deliver D1.
            tried_unknown = True
        elif target not in sizes:
            listed = ", ".join(dc.size_label(s) for s in sizes) or "none listed"
            return UNSUPPORTED, f"{kept}: D1 not supported by this camera (supports {listed})", False, None

        assign = dc.d1_assignments(ch, sub, target, max_rate)
        before = {k.split("ExtraFormat[0].", 1)[1]: sub.get(k.split("ExtraFormat[0].", 1)[1]) for k in assign}
        after = {k.split("ExtraFormat[0].", 1)[1]: v for k, v in assign.items()}
        self._save_backup(host, ch, sub, actor)
        ok, reason = await self._call(client.set_config, assign)
        self._audit(host, ch, actor, "set_substream_d1", before, after, "accepted" if ok else f"refused: {reason}")
        if not ok:
            return REFUSED, f"{kept}: NVR refused ({reason})", False, None

        failure = None
        verified: Optional[tuple[int, int]] = None
        try:
            enc2 = await self._call(client.get_config, "Encode")
            now = dc.substream_size(dc.channel_substream(enc2, ch), standard)
            if now != target:
                failure = f"value did not stick: the recorder reports {dc.size_label(now)}"
        except dc.DahuaHttpError as e:
            failure = f"could not re-read the settings ({e})"
        if failure is None:
            good, detail = await self._verify_stream(rtsp_target, target)
            if good:
                verified = target
            else:
                failure = detail
        if failure is None:
            entry["after"] = dc.size_label(target)
            rate_note = ""
            if "Video.BitRate" in after:
                rate_note = f", bit rate {before.get('Video.BitRate')}→{after['Video.BitRate']} kbps"
            unknown_note = "; supported sizes were not reported, tried and verified" if tried_unknown else ""
            return SWITCHED, f"switched to D1 ({dc.size_label(target)}, stream verified{rate_note}{unknown_note})", True, verified

        try:
            restored, rdetail, now = await self._restore(client, ch, sub, standard)
        except dc.DahuaHttpError as e:
            restored, rdetail, now = False, f"the recorder did not answer ({e})", None
        self._audit(host, ch, actor, "restore_after_failure", after, before,
                    "restored" if restored else f"restore failed: {rdetail}")
        if restored:
            entry["after"] = rdetail
            return RESTORED, f"restored after failure ({failure})", True, None
        entry["after"] = dc.size_label(now) if now else "unknown"
        return (RESTORE_FAILED, f"restore failed after failure ({failure}): {rdetail}. The channel is left as the "
                                f"recorder has it now; check it on the recorder or press Restore.", True, None)

    # -- restore ---------------------------------------------------------------
    async def run_restore(self, host: str, cams: list[dict], channels: Optional[list[int]], actor: str, *,
                          username: str, password: str, http_port: int = 80) -> dict:
        run = self._runs.get(host) if self.is_running(host) else None
        if run is None or run.get("kind") != "restore" or run.get("state") != "running":
            run = self._new_run("restore", host, cams, channels, actor)
        backups = self.backups(host)
        wanted = sorted(int(k) for k in backups) if channels is None else sorted(set(int(c) for c in channels))
        run["channels"] = [self._channel_entry(ch, cams) for ch in wanted]
        run["message"] = "Restoring saved sub-stream settings"
        self._publish(host, run)
        client = self.client_factory(host, username, password, port=http_port)
        try:
            standard: Optional[str] = None
            restored = 0
            for i, entry in enumerate(run["channels"]):
                if i and self.channel_delay_s:
                    await asyncio.sleep(self.channel_delay_s)
                ch = entry["channel"]
                saved = (backups.get(str(ch)) or {}).get("config")
                if not saved:
                    entry.update(outcome=NOTHING_TO_RESTORE, text="nothing saved to restore for this channel")
                    continue
                entry.update(outcome="working", text="Working")
                self._publish(host, run)
                try:
                    enc = await self._call(client.get_config, "Encode")
                    if standard is None:
                        standard = await self._standard(client, enc)
                    cur = dc.channel_substream(enc, ch)
                    ok, detail, now = await self._restore(client, ch, saved, standard)
                except dc.DahuaAuthError as e:
                    entry.update(outcome=STOPPED, text=f"stopped: {e}")
                    self._stop_auth(run, str(e))
                    return run
                except dc.DahuaHttpError as e:
                    entry.update(outcome=ERROR, text=f"not restored: the recorder did not answer ({e})")
                    continue
                keys = [k for k in dc.RESTORE_KEYS if k in saved]
                self._audit(host, ch, actor, "restore_saved", {k: cur.get(k) for k in keys},
                            {k: saved.get(k) for k in keys}, "restored" if ok else f"failed: {detail}")
                entry["before"] = dc.size_label(dc.substream_size(cur, standard))
                entry["after"] = dc.size_label(now) if now else None
                if ok:
                    restored += 1
                    entry.update(outcome=RESTORE_OK, text=f"restored to {detail}")
                else:
                    entry.update(outcome=RESTORE_FAILED, text=f"not restored: {detail}")
                try:
                    await self.on_channel_changed([c for c in cams if c["channel"] == ch], None)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"Could not restart the workers of channel {ch}: {exc}")
                self._publish(host, run)
            run.update(state="done", message=f"Done: {restored} channel(s) restored")
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"Sub-stream restore on {host} failed")
            run.update(state="failed", message=f"Stopped by an internal error ({type(exc).__name__})")
        finally:
            client.close()
            run["finished_at"] = _now_iso()
            self._publish(host, run)
        return run


def _int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def recorder_credentials(host: str, cams: list[dict]) -> tuple[str, str, str]:
    """(username, password, where from) for a recorder; password '' when none is saved.

    Order: the sign-in saved for this recorder, then one saved for any of its
    cameras, then the default recorder sign-in.
    """
    from app.services import camera_source
    from app.services.nvr_credential_service import nvr_credential_service

    raw = nvr_credential_service.load_credentials()
    entry = (raw.get("nvrs") or {}).get(host)
    if isinstance(entry, dict) and entry.get("password"):
        return entry.get("username") or "admin", entry["password"], "recorder sign-in"
    for cam in cams:
        creds = camera_source.load_credentials(cam["id"])
        if creds and creds[1]:
            return creds[0] or "admin", creds[1], f"camera {cam.get('name') or cam['id']}"
    if raw.get("default_password"):
        return raw.get("default_username") or "admin", raw["default_password"], "default recorder sign-in"
    return "", "", ""


recorder_substreams = RecorderSubstreams()
