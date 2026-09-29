# Third-party notices

This product includes or downloads the third-party components listed below.
Licences were read from each project's repository or from the installed
package metadata on 2026-09-28. This is not yet a complete inventory of every
transitive dependency; that is required before commercial sale (see
`docs/FEATURES.md`, "Licence inventory").

Rule for this product: no AGPL-3.0 component (for example Ultralytics YOLO
weights, their ONNX/HEF exports, or the `ultralytics` package) and no
non-commercial component may be shipped or downloaded by the installer.

## AI models

Both models are listed with their sha256 in `edge_backend/models/manifest.json`
and are downloaded by `edge_backend/scripts/fetch_models.py` from OpenMMLab's
download server. The installer deletes any other `*.onnx` in that directory.

| File | Upstream | Licence |
|---|---|---|
| `rtmo-s-body7-640x640-static.onnx` | RTMO-s, OpenMMLab mmpose (`projects/rtmo`), ONNX SDK archive `rtmo-s_8xb32-600e_body7-640x640-dac2bf74_20231211.zip` | Apache-2.0 |
| `rtmpose-s-256x192.onnx` | RTMPose-s, OpenMMLab mmpose (`projects/rtmposev1`), ONNX SDK archive `rtmpose-s_simcc-body7_pt-body7_420e-256x192-acd4a1ef_20230504.zip` (file `end2end.onnx`, unmodified) | Apache-2.0 |

Attribution (from the mmpose `LICENSE`): Copyright 2018-2020 Open-MMLab. All
rights reserved. Licensed under the Apache License, Version 2.0
(https://www.apache.org/licenses/LICENSE-2.0). The mmpose repository has no
`NOTICE` file (checked 2026-09-28). Its `LICENSES.md` lists one algorithm
under a different licence (EDPose, IDEA License 1.0); this product does not
use it.

**Modification notice (Apache-2.0 section 4(b)).** `rtmo-s-body7-640x640-static.onnx`
is a modified version of the `end2end.onnx` in the archive above, rebuilt by
`edge_backend/scripts/export_rtmo_static.py`: the batch size is fixed to 1,
the in-graph NonMaxSuppression is replaced by a constant TopK(300) over the
anchor scores (NMS then runs in this product's code), and shape arithmetic is
constant-folded. The weights are unchanged.

**Training data (open question).** Both checkpoints were trained by OpenMMLab
on the "body7" mix (COCO, AI Challenger, CrowdPose, MPII, sub-JHMDB, Halpe,
PoseTrack18). Some of these datasets are published for research use only.
Whether that restricts commercial use of the trained weights is pending a
legal check. A COCO-only RTMO-s checkpoint exists (about 1 AP lower) but
OpenMMLab publishes no ONNX for it.

## Python packages (application venv)

| Package | Licence |
|---|---|
| onnxruntime / onnxruntime-gpu | MIT |
| NVIDIA CUDA / cuDNN runtime wheels (`onnxruntime-gpu[cuda,cudnn]`, NVIDIA machines only) | NVIDIA proprietary (redistribution under NVIDIA's licence terms) |
| AMD ROCm / MIGraphX wheels (AMD machines only, `bootstrap.py --ort migraphx`) | per AMD package (MIGraphX: MIT); review before sale |
| opencv-python-headless | Apache-2.0 (the wheel bundles FFmpeg, LGPL-2.1, and other libraries listed in its `LICENSE-3RD-PARTY.txt`) |
| numpy | BSD-3-Clause (with 0BSD, MIT, Zlib, CC0-1.0 parts) |
| scipy | BSD-3-Clause |
| fastapi | MIT |
| uvicorn | BSD-3-Clause |
| pydantic, pydantic-core, pydantic-settings | MIT |
| SQLAlchemy | MIT |
| aiosqlite | MIT |
| cryptography | Apache-2.0 OR BSD-3-Clause |
| bcrypt | Apache-2.0 |
| PyJWT | MIT |
| httpx | BSD-3-Clause |
| python-multipart | Apache-2.0 |
| aiofiles | Apache-2.0 |
| psutil | BSD-3-Clause |
| segno | BSD-3-Clause |
| zeroconf | LGPL-2.1-or-later (used unmodified as a separate package) |

## Install-time tools (not part of the running service)

| Component | Licence |
|---|---|
| onnx, onnx-graphsurgeon (isolated venv used only to rebuild the RTMO file) | Apache-2.0 |
| uv | Apache-2.0 OR MIT |

## System components

| Component | Licence |
|---|---|
| go2rtc (live video gateway for direct WebRTC; official release v1.9.14 from `AlexxIT/go2rtc`, unmodified, pinned by SHA-256 in `edge_backend/scripts/bootstrap.py`; run as a separate process) | MIT |
| FFmpeg (installed from the operating system's packages) | LGPL-2.1-or-later, or GPL-2.0-or-later for GPL-enabled builds such as Debian/Ubuntu's |
| frp: frpc on the edge box, frps on the owner's VPS (optional online access; official release v0.71.0 / `fatedier/frps` image, unmodified) | Apache-2.0 |
