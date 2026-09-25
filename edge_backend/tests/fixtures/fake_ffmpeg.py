#!/usr/bin/env python3
"""Stands in for the system ffmpeg in tests/test_capture_backends.py.

Speaks just enough of ffmpeg's behaviour for FfmpegHwCapture: reads the
concat list from the ``pipe:N`` input, writes the stream dump lines to stderr,
then raw bgr24 frames (frame k filled with k % 256) to stdout. FAKE_FFMPEG_MODE:

    frames   stream at FAKE_FFMPEG_FPS until killed
    burst    FAKE_FFMPEG_BURST frames at once, then silence
    auth     a 401 like a camera rejecting the login, exit 8
    refused  connection refused, exit 1
    decode   the stream opens, then the hardware filter chain fails, exit 1
    stall    the stream opens, then nothing
"""

import os
import sys
import time

W, H = 64, 48
mode = os.environ.get("FAKE_FFMPEG_MODE", "frames")
record = os.environ.get("FAKE_FFMPEG_RECORD")
argv = sys.argv[1:]
list_url = argv[argv.index("-i") + 1]
fd = int(list_url.split(":", 1)[1])
with os.fdopen(fd, "rb") as f:
    listing = f.read().decode()
if record:
    with open(record, "w") as f:
        f.write(listing + "\n--argv--\n" + " ".join(argv))
url = listing.split("file '", 1)[1].split("'\n", 1)[0]
err = sys.stderr

if mode == "auth":
    err.write("[rtsp @ 0x55d0] method DESCRIBE failed: 401 (Unauthorized)\n")
    err.write(f"[in#0 @ 0x55d1] Impossible to open '{url}'\n")
    err.write("[in#0 @ 0x55d1] Error opening input: Server returned 401 Unauthorized (authorization failed)\n")
    sys.exit(8)
if mode == "refused":
    err.write(f"[in#0 @ 0x55d1] Impossible to open '{url}'\n")
    err.write("[in#0 @ 0x55d1] Error opening input: Connection refused\n")
    sys.exit(1)

err.write("Input #0, concat, from 'pipe:3':\n")
err.write("  Stream #0:0: Video: h264 (High), yuv420p(progressive), 1280x720 [SAR 1:1 DAR 16:9], 25 fps, "
          "25 tbr, 90k tbn\n")
err.flush()
if mode == "decode":
    err.write("[Parsed_scale_vaapi_1 @ 0x7f2c] Impossible to convert between the formats supported by the "
              "filter 'Parsed_select_0' and the filter 'auto_scale_0'\n")
    err.write("[vf#0:0 @ 0x5639] Error reinitializing filters!\n")
    sys.exit(1)
if mode == "stall":
    time.sleep(3600)

err.write("Output #0, rawvideo, to 'pipe:1':\n")
err.write(f"  Stream #0:0: Video: rawvideo (BGR[24] / 0x18524742), bgr24(pc, gbr/unknown/unknown, progressive), "
          f"{W}x{H} [SAR 1:1 DAR 4:3], q=2-31, 73 kb/s, 25 fps, 25 tbn\n")
err.flush()
out = sys.stdout.buffer
k = 0
if mode == "burst":
    for k in range(int(os.environ.get("FAKE_FFMPEG_BURST", "20"))):
        out.write(bytes([k % 256]) * (W * H * 3))
    out.flush()
    time.sleep(3600)
fps = float(os.environ.get("FAKE_FFMPEG_FPS", "25"))
while True:
    out.write(bytes([k % 256]) * (W * H * 3))
    out.flush()
    k += 1
    time.sleep(1.0 / fps)
