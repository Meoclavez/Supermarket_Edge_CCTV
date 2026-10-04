"""Privacy masks in direct (WebRTC) live video: services/privacy_video.py + webrtc_sessions.

A camera with privacy masks is never passed through: its go2rtc source is an
ffmpeg pipeline that burns the masks in, and when that cannot be built the
session is refused. go2rtc is the in-process fake from test_webrtc_p2p; the
last test runs the real ffmpeg (skipped without it) and checks pixels.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from app.services import ai_zone_service as zone_module
from app.services import go2rtc_manager as g2
from app.services import privacy_video as pv
from app.services.ai_zone_service import ai_zone_service
from app.services.frame_geometry import frame_sizes
from app.services.remote_access_service import remote_access_service
from app.services.webrtc_sessions import SessionError, WebRtcSessions
from tests.test_webrtc_p2p import CAM, CAM2, OFFER_H264, FakeGo2Rtc, _add_cameras, _schema, run  # noqa: F401 (_schema: autouse)

W, H = 704, 576
VP8_ONLY = OFFER_H264.replace("a=rtpmap:98 H264/90000\r\n", "")


def sq(x0, y0, x1, y1):
    return [{"x": x0, "y": y0}, {"x": x1, "y": y0}, {"x": x1, "y": y1}, {"x": x0, "y": y1}]


def mask(mode, pts, **kw):
    return {"id": f"m_{mode}_{pts[0]['x']}", "camera_id": CAM, "mask_mode": mode, "points": pts, **kw}


# --------------------------------------------------------------------------- #
# Mask images and filter graph, per mode
# --------------------------------------------------------------------------- #

def test_plan_and_images_per_mode():
    masks = [mask("BLACKOUT", sq(.1, .1, .3, .3)), mask("COLOR", sq(.6, .1, .9, .3), mask_color_bgr=[10, 20, 250]),
             mask("BLUR", sq(.1, .6, .4, .9)), mask("MOSAIC", sq(.6, .6, .9, .9), mosaic_scale=12),
             mask("SOMETHING_NEW", sq(.4, .4, .5, .5))]
    plan = pv.plan_for(masks, W, H)
    assert plan.kinds == ["fill", "fill", "blur", "mosaic", "fill"], "an unknown mode is a fill (black), fail closed"
    for it in plan.items:
        assert it.x % 2 == 0 and it.y % 2 == 0 and it.w % 2 == 0 and it.h % 2 == 0, "4:2:0 needs even boxes"
        assert 0 <= it.x and it.x + it.w <= W and 0 <= it.y and it.y + it.h <= H
        assert it.image.shape[:2] == (it.h, it.w), "each image is the size of its box, not of the frame"
    black, colour, blur, mosaic, unknown = plan.items
    assert (black.x, black.y) == (70, 58)
    assert black.image[black.h // 2, black.w // 2].tolist() == [0, 0, 0, 255]
    assert colour.image[colour.h // 2, colour.w // 2].tolist() == [10, 20, 250, 255]
    assert unknown.image[unknown.h // 2, unknown.w // 2].tolist() == [0, 0, 0, 255]
    assert blur.image.ndim == 2 and blur.image[blur.h // 2, blur.w // 2] == 255
    assert mosaic.strength == 12 and mosaic.small_size == (mosaic.w // 12, mosaic.h // 12)
    # privacy_mask's rule: the blur grows with the region (a large face is not merely soft).
    assert blur.strength >= max(51, (min(blur.w, blur.h) // 4) | 1)


def test_triangle_alpha_is_the_polygon_not_its_box():
    tri = mask("BLUR", [{"x": .1, "y": .1}, {"x": .5, "y": .1}, {"x": .1, "y": .5}])
    it = pv.plan_for([tri], W, H).items[0]
    assert it.image[2, 2] == 255 and it.image[it.h - 3, it.w - 3] == 0


def test_legacy_pixel_polygon_is_scaled_to_the_frame():
    legacy = mask("BLACKOUT", [{"x": 100, "y": 100}, {"x": 300, "y": 100}, {"x": 300, "y": 300}, {"x": 100, "y": 300}],
                  frame_width=1408, frame_height=1152)
    it = pv.plan_for([legacy], W, H).items[0]
    assert (it.x, it.y, it.w, it.h) == (50, 50, 102, 102)


def test_filter_graph_per_mode_and_engine():
    masks = [mask("BLACKOUT", sq(.1, .1, .3, .3)), mask("BLUR", sq(.1, .6, .4, .9)), mask("MOSAIC", sq(.6, .6, .9, .9))]
    plan = pv.plan_for(masks, W, H)
    g = pv.filter_graph(plan, "software")
    assert g.startswith(f"[0:v]scale={W}:{H}:") and "split=3[base][src1][src2]" in g
    assert "[1:v]format=rgba[img0]" in g and "overlay=70:58:" in g                    # BLACKOUT: RGBA overlay at its box
    b, m = plan.items[1], plan.items[2]
    assert f"[src1]crop={b.w}:{b.h}:{b.x}:{b.y},scale={b.w // 4}:{b.h // 4}:flags=area,boxblur=" in g
    assert f"[src2]crop={m.w}:{m.h}:{m.x}:{m.y},scale={m.w // 16}:{m.h // 16}:flags=area,scale={m.w}:{m.h}:flags=neighbor" in g
    assert "[2:v]format=gray[alpha1]" in g and "[reg1][alpha1]alphamerge[img1]" in g
    assert g.endswith("format=yuv420p[v]")
    assert pv.filter_graph(plan, "vaapi").endswith("format=nv12,hwupload[v]")
    assert not any(c.isspace() for c in g) and "#" not in g
    only_fill = pv.filter_graph(pv.plan_for(masks[:1], W, H), "software")
    assert "split" not in only_fill and "[0:v]scale=704:576:flags=bilinear,setsar=1,format=yuv420p[base]" in only_fill


def test_ffmpeg_args_and_go2rtc_source(tmp_path):
    masks = [mask("BLACKOUT", sq(.1, .1, .3, .3)), mask("BLUR", sq(.1, .6, .4, .9))]
    plan = pv.plan_for(masks, W, H)
    images = pv.write_images(CAM, masks, plan, tmp_path)
    assert [p.name.split("_", 2)[2] for p in images] == ["0_fill.png", "1_blur.png"]
    assert all(oct(p.stat().st_mode & 0o777) == "0o600" for p in images)
    for engine, encoder in (("software", "libx264"), ("vaapi", "h264_vaapi"), ("cuda", "h264_nvenc")):
        args = pv.ffmpeg_args(plan, engine, images)
        assert args[args.index("-c:v") + 1] == encoder
        assert "auto" not in args, "go2rtc 1.9.14 drops the ffmpeg arguments when one equals 'auto'"
        assert args.count("-i") == 2 and args[args.index("-map") + 1] == "[v]"
        assert ("-filter_hw_device" in args) == (engine == "vaapi")
    src = pv.go2rtc_source("rtsp://u:p@10.0.0.5:554/cam?channel=1&subtype=1", pv.ffmpeg_args(plan, "software", images))
    assert src.startswith("ffmpeg:rtsp://u:p@10.0.0.5:554/cam?channel=1&subtype=1#video=-an#raw=-i#raw=/")
    assert not any(c.isspace() for c in src), "go2rtc refuses API-added sources with whitespace"
    with pytest.raises(pv.MaskedVideoUnavailable):
        pv.go2rtc_source("rtsp://10.0.0.5/a b", ["-an"])
    images[1].unlink()
    with pytest.raises(pv.MaskedVideoUnavailable, match="missing"):
        pv.ffmpeg_args(plan, "software", images)


def test_new_masks_replace_the_old_images(tmp_path):
    first = [mask("BLACKOUT", sq(.1, .1, .3, .3))]
    a = pv.write_images(CAM, first, pv.plan_for(first, W, H), tmp_path)
    second = [mask("BLACKOUT", sq(.2, .2, .4, .4))]
    b = pv.write_images(CAM, second, pv.plan_for(second, W, H), tmp_path)
    assert a[0] != b[0] and not a[0].exists() and b[0].exists()


def test_signature_follows_what_the_picture_depends_on():
    base = mask("BLUR", sq(.1, .1, .3, .3))
    sig = pv.signature([base], (W, H))
    assert pv.signature([], (W, H)) == ""
    assert pv.signature([{**base, "name": "renamed", "id": "other"}], (W, H)) == sig, "a rename does not change the picture"
    assert pv.signature([{**base, "mask_mode": "MOSAIC"}], (W, H)) != sig
    assert pv.signature([{**base, "points": sq(.1, .1, .35, .3)}], (W, H)) != sig
    assert pv.signature([base], (1280, 720)) != sig


def test_privacy_masks_skip_ai_ignore_and_fail_closed(monkeypatch):
    zones = {"exclusion_masks": [mask("AI_IGNORE", sq(.1, .1, .3, .3)), mask("BLACKOUT", sq(.5, .5, .6, .6)),
                                 {**mask("BLUR", sq(.7, .7, .8, .8)), "enabled": False}]}
    monkeypatch.setattr(ai_zone_service, "get_all_zones", lambda cam=None: zones)
    assert [m["mask_mode"] for m in pv.privacy_masks(CAM)] == ["BLACKOUT"]

    def broken(cam=None):
        raise OSError("disk")
    monkeypatch.setattr(ai_zone_service, "get_all_zones", broken)
    with pytest.raises(pv.MaskedVideoUnavailable, match="could not be read"):
        pv.privacy_masks(CAM)
    assert pv.current_signature(CAM, (W, H)) == "unreadable"


def test_masked_source_fails_closed_and_falls_back_to_software(tmp_path):
    masks = [mask("BLACKOUT", sq(.1, .1, .3, .3))]
    url = "rtsp://u:p@10.0.0.5/cam"
    with pytest.raises(pv.MaskedVideoUnavailable, match="ffmpeg is not installed"):
        pv.masked_source(CAM, url, masks, ffmpeg_bin="", size=(W, H), directory=tmp_path)
    frame_sizes.forget("cam_never_seen")
    with pytest.raises(pv.MaskedVideoUnavailable, match="picture size is not known"):
        pv.masked_source("cam_never_seen", url, masks, ffmpeg_bin="ffmpeg", directory=tmp_path)
    tried = []

    def vaapi_broken(ffmpeg_bin, args, plan):
        tried.append(pv._engine_of(args))
        return "h264_vaapi" not in args
    ms = pv.masked_source(CAM, url, masks, ffmpeg_bin="ffmpeg", size=(W, H), engine="vaapi", directory=tmp_path,
                          prober=vaapi_broken)
    assert tried == ["vaapi", "software"] and ms.encoder == "libx264" and "#raw=libx264" in ms.source
    with pytest.raises(pv.MaskedVideoUnavailable, match="could not run"):
        pv.masked_source(CAM, url, masks, ffmpeg_bin="ffmpeg", size=(W, H), engine="cuda", directory=tmp_path,
                         prober=lambda *a: False)
    with pytest.raises(pv.MaskedVideoUnavailable, match="characters"):
        pv.masked_source(CAM, url, masks, ffmpeg_bin="ffmpeg", size=(W, H), directory=tmp_path / "with space",
                         prober=lambda *a: True)


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #

class RecordingGo2Rtc(FakeGo2Rtc):
    def __init__(self):
        super().__init__()
        self.added: list[str] = []

    async def add_stream(self, name, source):
        self.added.append(source)
        await super().add_stream(name, source)


RAW = {CAM: "rtsp://admin:pw@10.9.8.7:554/cam/realmonitor?channel=1&subtype=1",
       CAM2: "rtsp://admin:pw@10.9.8.7:554/cam/realmonitor?channel=2&subtype=1"}


@pytest.fixture
def masked(monkeypatch, tmp_path):
    run(_add_cameras())
    fake = RecordingGo2Rtc()
    svc = WebRtcSessions(manager=fake)
    monkeypatch.setattr(WebRtcSessions, "_source_for", staticmethod(lambda cam: RAW[cam.id]))
    monkeypatch.setattr(svc, "_ensure_reaper", lambda: None)
    monkeypatch.setattr(g2, "detect_transcode_engine", lambda ffmpeg_bin=None: "cuda")
    monkeypatch.setattr("app.services.capture_backends.ffmpeg_binary", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(pv, "probe", lambda ffmpeg_bin, args, plan: True)
    monkeypatch.setattr(pv, "masks_dir", lambda: tmp_path / "masks")
    monkeypatch.setitem(remote_access_service._settings, "max_video_sessions", 8)
    zones: dict[str, list] = {CAM: [], CAM2: []}
    monkeypatch.setattr(ai_zone_service, "get_all_zones",
                        lambda cam=None: {"exclusion_masks": [dict(m) for m in zones.get(cam, [])]})
    for cam in (CAM, CAM2):
        frame_sizes.set(cam, (W, H))
    svc.fake, svc.zones = fake, zones
    return svc


def _open(svc, cam=CAM, sdp=OFFER_H264):
    return run(svc.create(camera_id=cam, offer_sdp=sdp, purpose="tile", viewer_ip="203.0.113.9", remote=True,
                          user="op"))


def test_masked_camera_gets_the_masked_pipeline_never_the_raw_stream(masked):
    masked.zones[CAM] = [mask("BLACKOUT", sq(.1, .1, .3, .3))]
    sess, _ = _open(masked)
    src = masked.fake.added[-1]
    assert src.startswith("ffmpeg:" + RAW[CAM] + "#video=-an#raw=") and "#raw=-filter_complex#raw=" in src
    assert "#raw=h264_nvenc" in src, "the probed engine (here NVENC) encodes"
    assert RAW[CAM] not in masked.fake.added, "the raw camera stream is never a source"
    assert sess.masked and sess.transcoded and sess.codec == "H264" and sess.encoder == "h264_nvenc"
    assert sess.privacy_sig == pv.signature(masked.zones[CAM], (W, H)) and sess.public()["privacy_masked"]


def test_cameras_without_privacy_masks_keep_passthrough(masked):
    masked.zones[CAM2] = [{**mask("AI_IGNORE", sq(.1, .1, .3, .3)), "camera_id": CAM2}]
    sess, _ = _open(masked, cam=CAM2)
    assert masked.fake.added == [RAW[CAM2]] and not sess.masked and not sess.transcoded and sess.privacy_sig == ""


def test_refused_when_the_masked_pipeline_cannot_be_built(masked, monkeypatch):
    masked.zones[CAM] = [mask("BLUR", sq(.1, .1, .3, .3))]
    monkeypatch.setattr(pv, "probe", lambda ffmpeg_bin, args, plan: False)
    with pytest.raises(SessionError) as e:
        _open(masked)
    assert e.value.status == 503 and e.value.code == "privacy_mask_unavailable"
    assert e.value.reason.startswith(pv.REFUSAL) and "could not run" in e.value.reason
    assert masked.fake.added == [] and not masked.sessions
    assert masked.outcomes()[-1]["end_reason"] == "privacy_mask_unavailable"


def test_refused_when_go2rtc_cannot_start_the_masked_source(masked):
    masked.zones[CAM] = [mask("BLACKOUT", sq(.1, .1, .3, .3))]
    masked.fake.fail_answer = "streams: exec/rtsp\nError opening input files"
    with pytest.raises(SessionError) as e:
        _open(masked)
    assert e.value.code == "privacy_mask_unavailable" and "did not start" in e.value.reason
    assert len(masked.fake.added) == 1 and masked.fake.added[0].startswith("ffmpeg:"), "no second try with raw video"
    assert not masked.fake.streams_, "the failed stream is deleted"


def test_refused_when_masks_cannot_be_read_or_size_unknown(masked, monkeypatch):
    def broken(cam=None):
        raise ValueError("corrupt")
    monkeypatch.setattr(ai_zone_service, "get_all_zones", broken)
    with pytest.raises(SessionError) as e:
        _open(masked)
    assert e.value.code == "privacy_mask_unavailable" and masked.fake.added == []
    monkeypatch.setattr(ai_zone_service, "get_all_zones",
                        lambda cam=None: {"exclusion_masks": [mask("BLACKOUT", sq(.1, .1, .3, .3))]})
    frame_sizes.forget(CAM)
    with pytest.raises(SessionError) as e:
        _open(masked)
    assert "picture size" in e.value.reason and masked.fake.added == []


def test_masked_camera_and_a_browser_without_h264(masked):
    masked.zones[CAM] = [mask("BLACKOUT", sq(.1, .1, .3, .3))]
    with pytest.raises(SessionError) as e:
        _open(masked, sdp=VP8_ONLY)
    assert e.value.code == "negotiation_failed" and "H.264" in e.value.reason and masked.fake.added == []


def test_mask_change_ends_and_cuts_sessions_and_new_ones_use_the_new_masks(masked):
    masked.zones[CAM] = [mask("BLACKOUT", sq(.1, .1, .3, .3))]

    async def go():
        masked._loop = asyncio.get_running_loop()
        a, _ = await masked.create(camera_id=CAM, offer_sdp=OFFER_H264, purpose="tile", viewer_ip="x", remote=True,
                                   user="op")
        b, _ = await masked.create(camera_id=CAM2, offer_sdp=OFFER_H264, purpose="tile", viewer_ip="x", remote=True,
                                   user="op")
        old_src = masked.fake.added[0]
        # A rename only: the picture is the same, nobody is cut.
        masked.zones[CAM] = [{**masked.zones[CAM][0], "name": "Office door"}]
        pv.notify_masks_changed()
        await asyncio.sleep(0.05)
        assert a.id in masked.sessions and masked.fake.stops == 0
        # The mask moves: the zone service notifies, sessions end and go2rtc is restarted now.
        masked.zones[CAM] = [mask("BLACKOUT", sq(.2, .2, .4, .4))]
        pv.notify_masks_changed()
        await asyncio.sleep(0.05)
        assert a.id not in masked.sessions and masked.fake.stops == 1
        assert b.id not in masked.sessions, "the restart ends every connection; those pages reopen"
        reasons = {o["session_id"]: o["end_reason"] for o in masked.outcomes() if o.get("end_reason")}
        assert reasons[a.id] == "privacy_masks_changed" and reasons[b.id] == "gateway_restart"
        c, _ = await masked.create(camera_id=CAM, offer_sdp=OFFER_H264, purpose="tile", viewer_ip="x", remote=True,
                                   user="op")
        assert masked.fake.added[-1] != old_src and c.privacy_sig == pv.signature(masked.zones[CAM], (W, H))
        masked._loop = None
    run(go())


def test_masks_added_to_a_passthrough_camera_end_it_on_the_reaper_pass(masked):
    sess, _ = _open(masked, cam=CAM2)
    assert not sess.masked
    masked.zones[CAM2] = [{**mask("MOSAIC", sq(.1, .1, .3, .3)), "camera_id": CAM2}]
    run(masked.reap_once())   # the backstop: no notification needed
    assert sess.id not in masked.sessions and masked.fake.stops == 1
    assert any(o.get("end_reason") == "privacy_masks_changed" for o in masked.outcomes())


def test_zone_service_notifies_on_every_privacy_mask_edit(monkeypatch):
    calls = []
    monkeypatch.setattr(pv, "notify_masks_changed", lambda: calls.append(1))
    monkeypatch.setattr(ai_zone_service, "_save_persistent_zones", lambda: None)
    saved = ai_zone_service.add_exclusion({"camera_id": "cam_notify", "mask_mode": "BLUR", "points": sq(.1, .1, .2, .2)})
    ai_zone_service.update_exclusion(saved["id"], {"mask_mode": "BLACKOUT"})
    assert ai_zone_service.delete_exclusion(saved["id"])
    assert not ai_zone_service.delete_exclusion(saved["id"])
    assert len(calls) == 3
    assert zone_module._privacy_masks_changed is not None


# --------------------------------------------------------------------------- #
# Real ffmpeg: a pixel inside a BLACKOUT polygon is black, one outside is unchanged
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_ffmpeg_burns_a_blackout_polygon_in(tmp_path):
    import cv2

    masks = [mask("BLACKOUT", sq(.1, .1, .4, .4)), mask("COLOR", sq(.6, .1, .9, .4), mask_color_bgr=[0, 0, 255])]
    ms = pv.masked_source("cam_ffmpeg_it", "rtsp://unused/x", masks, ffmpeg_bin="ffmpeg", size=(W, H),
                          engine="software", directory=tmp_path)   # real probe on a synthetic input
    args = ms.source.split("#raw=")[1:]
    src = ["-f", "lavfi", "-i", f"testsrc2=size={W}x{H}:rate=25"]
    common = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    subprocess.run([*common, *src, *args, "-frames:v", "1", str(tmp_path / "masked.mp4")], check=True, timeout=60)
    subprocess.run([*common, "-i", str(tmp_path / "masked.mp4"), "-frames:v", "1", str(tmp_path / "masked.png")],
                   check=True, timeout=60)
    subprocess.run([*common, *src, "-frames:v", "1", str(tmp_path / "ref.png")], check=True, timeout=60)
    out, ref = cv2.imread(str(tmp_path / "masked.png")), cv2.imread(str(tmp_path / "ref.png"))
    assert out.shape == ref.shape == (H, W, 3)

    def px(img, x, y):
        return img[int(y * H), int(x * W)].astype(int)
    assert px(out, .25, .25).max() <= 16, f"inside BLACKOUT: {px(out, .25, .25)}"
    b, g, r = px(out, .75, .25)
    assert r >= 230 and b <= 25 and g <= 25, "inside COLOR: the mask colour"
    assert np.abs(px(out, .5, .7) - px(ref, .5, .7)).max() <= 12, "outside the masks: unchanged"
    assert np.abs(px(ref, .25, .25)).max() > 100, "the reference really had picture there"
