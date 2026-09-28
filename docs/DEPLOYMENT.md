# Deploying Edge AI CCTV in a store

Everything runs on one machine on the store LAN. No footage, no detection and
no analytics leave the premises.

---

## 1. What the machine needs

| | Minimum | Notes |
|---|---|---|
| OS | Linux with systemd | Ubuntu 22.04/24.04 LTS or Arch |
| CPU | 4 cores | Detection runs here if no GPU is fitted |
| RAM | 8 GB | 16 GB for more than ~12 cameras |
| GPU | Optional | NVIDIA (CUDA) or AMD (ROCm, via MIGraphX) is detected and used automatically |
| Disk | 256 GB SSD | Theft evidence (stills, optional short clips) goes to `STORAGE_DIR` on this SSD. Continuous recording is the store NAS's job; this software never writes to the NAS |
| Network | Wired gigabit | Cameras and the server on the same VLAN |

**No hardware is selected at build time.** On startup the system probes
TensorRT, CUDA, MIGraphX (AMD), OpenVINO, and finally CPU, and uses the first
that really takes the model. A Hailo NPU is detected and reported, but no
compatible model ships for it (there is no RTMO HEF), so the next backend
runs the model. Moving the install to a machine
with a GPU needs no code change — re-run `deploy/install.sh` (or
`run.sh --check-only`), which installs the matching onnxruntime build.

---

## 2. Install

### Recommended: one command

```bash
git clone https://github.com/Meoclavez/Supermarket_Edge_CCTV.git
sudo bash Supermarket_Edge_CCTV/deploy/install.sh
```

`deploy/install.sh` does sections 2 to 4 below: creates the `edgecctv`
service user, clones the code into `/opt/edge-cctv` (or pulls if it is
already there), runs `edge_backend/scripts/bootstrap.py --check-only` as that
user (venv, dependencies, the onnxruntime build for this machine's hardware,
models), gates on the service's own preflight, creates `.env` from
`.env.example` if there is none, installs and restarts the systemd unit, opens
the firewall for Tailscale if `ufw` is active, waits for the health endpoint
and prints the first-run setup code (section 4). It is safe to re-run; that is
also how you update an installed box.

Optional environment, passed through `sudo`:

| Variable | Default | Meaning |
|---|---|---|
| `EDGE_REPO_URL` | the GitHub repo above | where to clone from |
| `EDGE_ORT` | `auto` | onnxruntime build (`bootstrap.py --ort`) |
| `EDGE_MODELS_FROM` | unset | a directory of `*.onnx` files to copy into `edge_backend/models/` instead of downloading them on this machine (files `models/manifest.json` does not list are deleted afterwards) |

```bash
sudo EDGE_MODELS_FROM=/home/me/models bash Supermarket_Edge_CCTV/deploy/install.sh
```

### AMD GPUs (ROCm / MIGraphX)

On a machine with an AMD GPU, `/dev/kfd` and no NVIDIA GPU, `bootstrap.py`
picks the `migraphx` flavour by itself: onnxruntime 1.29 plus AMD's ROCm,
MIGraphX and MIGraphX plugin-EP wheels from AMD's package indexes (about
3.8 GB), with the device libraries for the GPU target read from the kernel at
run time (for example `gfx1200` for an RX 9060 XT). No system ROCm install and
no `sudo` are needed beyond the amdgpu kernel driver and the `render`/`video`
groups, which `install.sh` grants. It then **pre-compiles** the models the
service will load into `storage/migraphx_cache/` (1-3 min per model the first
time; a few seconds on later runs), so the service starts on the GPU straight
away. Every rung of the pose ladder is compiled, not only the one start-up
picks, because the run-time scheduler (section 6) may step to any of them as
cameras come and go; a rung it needs that is still cold is compiled in a
separate process while the current model keeps serving. The shipped ladder
has one rung, RTMO-s (compile about 110 s, peak about 2 GB).

If the cache is cold at service start (a new model, a changed camera count
that selects another model), the service starts on the CPU, compiles in a
separate process, and switches to the GPU when done; `/api/v1/health` shows
`inference_gpu_compile: "compiling"` and `inference_provider: "cpu"` until
then. `MIGRAPHX_COMPILE_MODE=foreground` blocks start-up instead. A compile
peaks at about 2.5 GB, within the unit's `MemoryMax=8G`.

Measured on an RX 9060 XT (fp32): RTMO-s 640x640 about 11 ms per frame
(12.3 ms median standalone, 10.9 ms in the live trial), boxes within 1e-4 px
and keypoints within 6e-4 px of the CPU result.
`--ort cpu` (or `EDGE_ORT=cpu`) keeps the small CPU-only build.

When inference does run on the CPU, ONNX Runtime is limited to
`INFERENCE_CPU_THREADS` threads in total (default: half the CPUs) so it cannot
starve video decoding and recording.

### Models and licences

`edge_backend/models/manifest.json` lists every model the product may use,
each pinned by sha256 and with its licence:

| File | Role | Licence |
|---|---|---|
| `rtmo-s-body7-640x640-static.onnx` | the person model (box + 17 keypoints, one pass), required | Apache-2.0 (OpenMMLab mmpose / RTMO) |
| `rtmpose-s-256x192.onnx` | optional top-down keypoint refiner (`POSE_REFINER`) | Apache-2.0 (OpenMMLab mmpose / RTMPose) |

`scripts/fetch_models.py` (run by `bootstrap.py`, so by `run.sh` and
`install.sh`) downloads a missing model from OpenMMLab, rebuilds RTMO as a
static 640x640 graph with host NMS (`scripts/export_rtmo_static.py`, in an
isolated venv; byte-identical to the manifest hash), and **deletes every
`*.onnx` in `edge_backend/models/` that the manifest does not list**, logging
each file, so models of earlier releases do not linger (`--keep-unlisted`
keeps them; `--verify-only` only lists them). The service's preflight ignores
unlisted files but warns when a setting (`POSE_MODEL_*`, `SHADOW_POSE_MODEL`)
names a model that is not in the manifest. No AGPL-3.0 (e.g. Ultralytics
YOLO) or non-commercial model may be added. Both checkpoints were trained on
OpenMMLab's body7 dataset mix, part of which is published for research use
only; that is pending a legal check. Attribution: `THIRD_PARTY_NOTICES.md`.

Camera streams: RTMO-s needs about D1 (704x576) or more. In the live trial it
found 25-33 % more real shoppers than the previous model on D1 and larger
streams but only about 60 % of them on CIF (352x288) sub-streams, so set the
NVR's analytics sub-streams to D1 or better.

### By hand

```bash
# No --create-home: the skeleton dotfiles it copies would make /opt/edge-cctv
# non-empty, and git clone refuses to clone into a non-empty directory.
sudo useradd --system --no-create-home --home-dir /opt/edge-cctv \
     --shell /usr/sbin/nologin edgecctv
sudo usermod -aG video,render edgecctv          # camera + GPU access
sudo install -d -o edgecctv -g edgecctv /opt/edge-cctv

sudo -u edgecctv git clone <repo> /opt/edge-cctv
cd /opt/edge-cctv
sudo -u edgecctv python3 -m venv .venv
sudo -u edgecctv .venv/bin/pip install -r edge_backend/requirements.txt

# On a machine with an NVIDIA GPU, swap in the GPU build:
sudo -u edgecctv .venv/bin/pip uninstall -y onnxruntime
sudo -u edgecctv .venv/bin/pip install onnxruntime-gpu
```

## 3. Configure

```bash
cd /opt/edge-cctv/edge_backend
sudo -u edgecctv cp .env.example .env
sudo -u edgecctv python3 -c "import secrets; print('JWT_SECRET=' + secrets.token_urlsafe(64))"
sudo -u edgecctv nano .env
```

Three values **must** change before the system goes on a store network:

- `DEBUG=false` — `true` disables authentication on every API route.
- `JWT_SECRET` — otherwise anyone can mint a valid session.
- `INTERNAL_SERVICE_KEY` — the machine-to-machine key.

The service logs `INSECURE CONFIGURATION:` at startup for each of these that is
still at its default. Check the journal after first start.

**Where things live, and who can read them.** Code and `.env` are under
`/opt/edge-cctv`; the database (`storage/cctv_core.db`), secrets, recordings,
backups and the setup code are under `/opt/edge-cctv/storage` (or
`STORAGE_DIR`). The database holds operator password hashes and camera
credentials, so none of this is world-readable: the unit runs with
`UMask=0027`, and `deploy/install.sh` sets `storage/` to `0750`, removes
"other" access below it and sets `.env` to `0640`. Read these files as root
or with `sudo -u edgecctv`; a backup job that copies `storage/backups/` must
run as one of those too.

```bash
sudo chown edgecctv:edgecctv /opt/edge-cctv/edge_backend/.env
sudo chmod 0640 /opt/edge-cctv/edge_backend/.env
sudo chmod 0750 /opt/edge-cctv/storage && sudo chmod -R o-rwx /opt/edge-cctv/storage
```

## 4. Run

```bash
sudo cp /opt/edge-cctv/deploy/edge-cctv.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now edge-cctv
journalctl -u edge-cctv -f
```

Open `http://<server-ip>:8000/dashboard`. The first visit asks you to create
the operator account; after that it asks for sign-in.

### The first-run setup code

Creating the operator account needs a one-time **setup code**, so that
whoever reaches the dashboard first on the store LAN cannot make themselves
the owner. While no operator account exists, the service issues one at every
start. Find it in any of:

- the end of the `deploy/install.sh` output;
- the journal: `journalctl -u edge-cctv | grep -A1 'FIRST-RUN SETUP CODE'`;
- the file `/opt/edge-cctv/storage/setup_code.txt` (mode `0600`, owned by
  `edgecctv`): `sudo cat /opt/edge-cctv/storage/setup_code.txt`.

The code is single use and the file is removed once the account exists.
Setup is refused through the public online-access address (section 7a), so
do this on the store network (or over a private VPN into it).

To get a code again:

- **no account created yet:** `sudo systemctl restart edge-cctv` logs the
  current code again (and issues a new one if the file was deleted);
- **locked out of an existing account:** reset to first-run. This deletes
  every operator account (after backing up the database), signs out all
  sessions and phones, and prints a new code; cameras, zones and analytics
  are kept:

  ```bash
  sudo -u edgecctv -H bash -c 'cd /opt/edge-cctv/edge_backend && \
      ../.venv/bin/python scripts/manage_operator.py reset-setup'
  ```

  `manage_operator.py reset-password --username <name>` changes one password
  instead, and `list` shows the accounts.

### Viewing the dashboard from off-site

- **Public, for anyone you give an account:** `https://cctv.<your-domain>`
  through your own VPS (section 7a).
- **Private VPN:** a VPN such as Tailscale reaches the plain address,
  `http://<vpn-ip>:8000/dashboard`, encrypted by the VPN itself.
  `deploy/install.sh` keeps port 8000 open on `tailscale0` only (plus UDP
  41641 for direct peer connections) when `ufw` is active and `HOST` is not
  `127.0.0.1`. VPN requests count as local (like the store LAN, first-run
  setup included), so only admit devices you trust.

A fresh install is empty: no zones, no rooms or walls, no cameras and no
metrics. The dashboard shows a data-state banner and the **Blueprint** tab
shows a "Set up your store" panel until you have configured something. Nothing
is pre-seeded; everything you see afterwards is what you drew or what the
cameras observed.

---

## 5. Commission the store

Do these in order — later steps depend on earlier ones. In short: sign in →
Blueprint tab → Store size → draw rooms / walls / zones → Scan for cameras →
Add → place the camera → Calibrate → people appear on the plan.

### 5.1 Set the floor size
**Blueprint tab → Store size.** Enter the real width and depth in metres.
Every coordinate in the system is in metres, so this must match the building
or nothing downstream is to scale.

### 5.1a Draw the building (optional but recommended)
**Blueprint tab → Room / Wall / Shelf / Door.** Trace the rooms, walls,
shelving runs and door openings so the plan reads like the store. These are
geometry only (`store_structures`); they carry no metrics. Zones (5.5) are the
regions that are counted.

### 5.2 Add the cameras
**Blueprint tab → Scan for cameras.** This probes USB devices, ONVIF WS-Discovery,
mDNS, and sweeps the local subnet for RTSP, verifying each with a real RTSP
handshake. Only devices that actually answered are listed.

Press **Add** on a device and fill in the form. For an IP camera or NVR that
means the username, password, and — on an NVR — the channel number. Choose the
**sub** stream: analytics gains nothing from decoding 1080p, and the main
stream costs several times the CPU.

Nothing is invented here. If the list is empty, no camera replied; check power,
cabling and that the cameras are on this VLAN.

**Connect cameras as NVR channels.** Where the store has a recorder, add each
camera once, as its recorder channel (**Dahua recorder** → scan → adopt, on the
sub-stream): one login, one place to manage streams. Do not also add the same
camera by its own IP address or by typing the channel's main-stream URL. The
dashboard refuses such a second copy with the existing camera named ("Add
anyway" only for a genuine second stream, which is then left out of store
totals), and warns about copies already configured ("Same camera added twice"
in Cameras and Store map), so nobody is counted twice. Save the recorder's
sign-in so the device can read the recorder's connected-camera list (once a
day) and recognise a camera added by its own IP as the same as its channel.

### 5.3 Place and aim each camera
Drag its marker on the plan, or type exact values in the side panel (X, Y,
bearing, field of view, mount height). This is saved immediately and survives
restarts.

### 5.4 Calibrate each camera — the step that matters most
An uncalibrated camera **detects people but cannot place them on the floor
plan**, so it contributes nothing to zone metrics or the heatmap. The panel
labels it `uncalibrated` in orange.

Calibration needs four points on the floor whose real positions you know —
corners of a tile, ends of a shelf run, a pallet footprint. Mark each in the
camera view and on the plan:

```
POST /api/v1/layout/cameras/{camera_id}/calibrate
{
  "image_points": [{"x":320,"y":700}, {"x":980,"y":700},
                   {"x":900,"y":420}, {"x":400,"y":420}],
  "floor_points": [{"x":2,"y":14}, {"x":18,"y":14},
                   {"x":18,"y":6},  {"x":2,"y":6}]
}
```

Pick four well-spread, non-collinear points; the request is rejected if they
are degenerate.

The **Calibrate** button on the camera's panel does this without hand-writing
JSON: click a floor mark on the live frame, click the same spot on the plan,
repeat four or more times, then solve. The pairs are stored with the camera
(`GET /api/v1/layout/cameras/{camera_id}/calibration`) and
`POST .../calibration/test` reprojects image points so you can check where they
land. Once a camera is calibrated, the people it sees appear on the plan
(`GET /api/v1/layout/live`); until then it only reports boxes on its own feed.

### 5.5 Draw the zones
**Blueprint tab → Draw zone.** Click the corners, then **Finish zone**. Name it
and pick a category in the panel. Categories carry meaning:

- `ENTRANCE` / `EXIT` — store entry and departure
- `AISLE` / `DEPARTMENT` — ordinary dwell regions
- `SHELF` — may bind a SKU for product-level work
- `CHECKOUT` — drives queue length and wait time
- `STOCKROOM`
- `EXCLUDED` — never counted (staff areas, offices)

Zones store geometry only. Every metric shown against a zone is computed from
recorded visits at query time.

### 5.6 Connect the POS (optional)
Without it, footfall and dwell are live but revenue and conversion stay blank —
the dashboard says so rather than showing zero. Post real transactions to
`POST /api/v1/analytics/pos/ingest`.

---

## 6. Tuning detection against your cameras

The defaults are conservative. After a day of real footage, check
`GET /api/v1/layout/pipeline/status`:

- **People counted who were not there** → raise `PERSON_CONF_THRESHOLD`. A
  small model on an unusual angle will label floor texture or shelving as a
  person. The geometry gates (`PERSON_MIN_ASPECT_RATIO`,
  `PERSON_MAX_FRAME_FRACTION`) reject boxes that cannot be an upright person;
  `implausible_boxes_rejected` in the status shows how often they fire.
- Defaults for RTMO-s (measured on COCO persons degraded to 704x576):
  `PERSON_CONF_THRESHOLD=0.50`, `PERSON_CONF_THRESHOLD_DARK=0.45` (precision
  0.96 lit / 0.95 at -3 EV), `TRACK_LOW_CONF_THRESHOLD=0.40` (boxes that may
  only extend an existing track), keypoint visibility gate 0.5. A `.env` that
  still sets the earlier model's `PERSON_CONF_THRESHOLD_DARK=0.35` or
  `TRACK_LOW_CONF_THRESHOLD=0.25` lets more false boxes through with RTMO.
- **Real shoppers missed** → lower `PERSON_CONF_THRESHOLD`, check the
  camera is not so high that people appear wider than tall, and that its
  analytics stream is at least D1 (704x576): on CIF (352x288) the model
  misses far and partly hidden people.
- **CPU saturated** → raise `ANALYTICS_DETECT_EVERY_N_FRAMES`, or fit a GPU.

### Many cameras: the inference budget

Detection does not run on every Nth frame of every camera any more: one
accelerator cannot do that for dozens of streams (33 cameras x 5 frames/s x
14 ms for a 960x544 pose model kept an RX 9060 XT 97 % busy, and detections
went stale). The service measures what each analysed frame really costs on the
device and plans for `POSE_BUDGET_UTILISATION` (default 0.6) of that capacity,
split fairly between the cameras that are analysing:

| Setting | Default | Meaning |
|---|---|---|
| `ANALYTICS_SCHEDULER` | `1` | `0` restores the fixed every-Nth-frame rule |
| `POSE_BUDGET_UTILISATION` | `0.6` | share of the measured device capacity inference may use |
| `ANALYTICS_MIN_DETECT_FPS` | `1.0` | every camera gets at least this, even over budget |
| `ANALYTICS_DETECT_EVERY_N_FRAMES` | `5` | ceiling: never more often than every Nth frame |
| `ANALYTICS_TARGET_DETECT_FPS` | `2.0` | the per-camera rate the pose model is chosen for |
| `ANALYTICS_REFIT_DEBOUNCE_SEC` | `30` | how long a new camera set / load must hold before the model changes |
| `ANALYTICS_MAX_INFLIGHT` | `4` | analysed frames in flight; a camera whose turn comes beyond that skips the frame |
| `CAMERA_DECODE_THREADS` | `0` | FFmpeg (CPU) decode threads per camera; 0 = 1 up to 720p, 2 up to 1080p, 4 above |

A frame that is not analysed is still shown live and recorded. When the
cameras change, or the device stays over the target, the model is re-fitted
down (or back up) the ladder: the `auto` keypoint refiner is dropped first,
then the next model on `POSE_MODEL_LADDER_GPU` / `_CPU`. Only RTMO-s ships, so
the ladder has one rung; a larger model with the same output layout (for
example an RTMO-m static export, Apache-2.0) can be listed there, most
accurate first, once it is in `models/manifest.json`.

Projection for the store box (RX 9060 XT, 32 cameras, defaults): RTMO-s costs
about 10.9 ms of device time per analysed frame, so the budget is
0.6 x 1000 / 10.9 ≈ 55 analysed frames/s, about **1.7 frames/s per camera**
(1.5/s if the device cost is the 12.3 ms measured standalone). That is below
the 2.0/s target (the keypoint refiner is off by default: it did not improve
RTMO's keypoints); the 1.0/s floor is met with room to spare (32 x 1.0 x 10.9 ms = 35 % of the device). The measured cost, not
these figures, drives the scheduler. `detector.load_control` in
`GET /api/v1/layout/pipeline/status` shows the target and measured
utilisation, the measured ms per frame, the rate each camera is allocated and
really gets (`cameras[].analysis_rate`), the ladder with its predicted load,
and every re-fit with its reason; the service log has one
`Inference re-fit:` line per change. Tracker timings are counted in analysed
frames (`TRACK_MAX_AGE_FRAMES`, `TRACK_MIN_HITS`), so at a lower rate a lost
person is kept for longer and a new one takes longer to confirm.

A false positive is indistinguishable from a real shopper once it is recorded,
so it is better to miss a few people than to invent them.

---

## 7. Operating

```bash
systemctl status edge-cctv
journalctl -u edge-cctv -f
curl -H "Authorization: Bearer $TOKEN" localhost:8000/api/v1/layout/pipeline/status
```

`pipeline/status` is the health view that matters: per camera it reports
`status`, `fps`, `frames_read`, `live_tracks` and `calibrated`, plus the
detector backend and its latency. `GET /api/v1/layout/live` shows who is on
the floor right now (positions only from calibrated cameras), and
`GET /stream?camera_id=<id>&overlay=1` draws the tracker's boxes on a feed so
you can see what the detector is doing.

**Backups.** A snapshot is taken at every startup into `storage/backups/`,
pruned to 7 days. To keep copies, pull that directory to another machine;
do not point the service at the store NAS (it must not write there).

**Updating a running device.** Install the new release and restart the
service: on start the database migrates itself (versioned migrations in
`edge_backend/app/migrations/`, then new model columns/tables are added
automatically), after a `storage/backups/*_pre-vNNNN.db` snapshot. It refuses
to start on a database written by a newer release. Inspect with
`cd edge_backend && python -m app.migrations status` (or `check` / `upgrade`).

**Resetting to a fresh install.** To wipe the store and start again — for
example after a misconfigured camera recorded false shoppers, or to hand a
machine to a different store:

```bash
curl -X POST localhost:8000/api/v1/layout/reset \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"confirm":"RESET"}'
```

This permanently deletes zones, rooms and walls, cameras (their workers are
stopped), discovered devices and every recorded observation and incident, and
returns the layout to its default size. The operator account and `.env`
settings are kept. Any value other than the literal `"RESET"` is refused.
`POST /api/v1/layout/purge-seed-data` (`{"confirm":true}`) is a legacy alias
that now performs the same full reset; it no longer keeps cameras or zones.

---

## 7a. Online access through your own server (VPS)

The dashboard is published at `https://cctv.<your-domain>` through a server
you already run (a VPS with Docker and a reverse proxy). Nothing moves to the
cloud: footage, detection and the database stay on this machine; the VPS only
relays connections.

```
browser --HTTPS--> VPS reverse proxy --> frps (Docker) ==tunnel==> frpc (this machine) --> 127.0.0.1:8000
```

- **frpc** (frp, Apache-2.0) runs on this machine as a child of the service
  (`app/services/remote_access_service.py`: started, supervised, restarted
  with backoff, status read from its log). It dials **out** to the VPS, so
  the store needs no port forwarding and works behind carrier-grade NAT.
- **frps** runs on the VPS as one Docker container shared by all stores, on
  the reverse proxy's network, with no published ports (`deploy/vps/`). Each
  box logs in with its **store ID** and **store token**; a server plugin (the
  store check) verifies them and allows each store only its own hostname(s).
- The tunnel itself goes over **WebSocket + TLS through the VPS's existing
  reverse proxy on 443**, on a hostname of its own (`wss://tunnel.<your-domain>`),
  so no new port is opened on the VPS. `tcp://<vps>:7000` (frp's own TLS on a
  published port) is the fallback for proxies that cannot pass WebSockets.
- The VPS proxy terminates HTTPS for `cctv.<your-domain>` with its own
  certificates (Let's Encrypt) and hands requests to frps, which routes them
  by name down this store's tunnel.

It is online **only** while enabled in **Settings → Online access**; disable
it there and the address stops working within seconds.

**VPS side:** `deploy/vps/README.md` (compose file, `frps.toml`, snippets for
Traefik, nginx, Caddy and Nginx Proxy Manager, security checklist, adding a
store). Store names are one level deep, `<store>-cctv.<domain>`, so a CDN's
free certificate covers them.

**Edge box side:**

1. Fetch frpc (pinned frp version, SHA-256 checked against the value pinned in
   `bootstrap.py` and the release's checksum list, x86_64/arm64/armv7):

   ```bash
   sudo EDGE_TUNNEL=1 bash /opt/edge-cctv/deploy/install.sh
   ```

   (`./run.sh --with-tunnel` does the same for a manual install; once online
   access is enabled, every later bootstrap keeps frpc in place by itself.)
2. **Settings → Online access**: **Public address** `<store>-cctv.<your-domain>`,
   **Tunnel server** `wss://tunnel.<your-domain>`, **Store ID** and **Store
   token** (issued for this store; e.g. `pearcedale-cctv.ikorex.com.au`,
   `wss://tunnel.ikorex.com.au`, `pearcedale`), **Server key** only if the server sets
   frp's shared token, **Proxies in front of your server** = *One* if a CDN
   proxy (e.g. Cloudflare's orange cloud) sits in front of the VPS, tick
   **Enable online access**, **Save**. The status goes *Connecting… →
   Connected since …*.
3. Press **Verify now**. The app fetches
   `https://cctv.<your-domain>/api/v1/device/identity` and checks that the
   device id is *this* machine's, so an address routed to another store's box
   is caught. The badge shows *Verified* with the time. It is checked again
   a few seconds after every (re)connection, and every 10 minutes while the
   Settings page is open.
4. From a phone on mobile data, open `https://cctv.<your-domain>/dashboard`.

What the status says when it is not connected (`error_kind` in
`GET /api/v1/remote-access`): the store login was refused (check Store ID and
token), the address is not allowed for this store, another connection already
serves the address, the shared server key does not match, the store check is
not answering, the certificate could not be verified, the server is not
reachable, it answered but not as a tunnel (WebSocket handshake refused: proxy
route for the tunnel host missing), the connection was lost (reconnecting), or
the tunnel program (frpc) is not installed.

**Secrets and files.** The store token and the optional server key are
stored encrypted under `storage/secrets/named/` and are write-only in the
dashboard. frpc gets them in environment variables that its generated config
(`storage/tunnel/frpc.toml`, directory `0700`, file `0600`: `user = <store
id>`, `metadatas.token`, one `http` proxy for the public address to
`127.0.0.1:<PORT>`, heartbeat 30 s / timeout 90 s) references as templates, so
they are never on a command line, in a file, in the logs or in an API
response. frpc's environment holds nothing else of the service's.
The tunnel server's certificate is always verified (system CA bundle;
`EDGE_TUNNEL_CA_FILE` for a private CA). Binary lookup: `FRPC_PATH`, `PATH`,
`<repo>/bin/frpc`. The systemd unit needs no change for it (`bin/` is only
read, the config is under `storage/`, connections are outbound).

Nothing assumes a fixed domain: the address, tunnel server, store ID and
secrets are settings and can change at any time (test domain now, the
customer's at handover). Any change restarts frpc; changing the address
clears *Verified* and moves the CORS origin and remote rules to the new name.

### Close the plain-HTTP port (HOST=127.0.0.1)

frpc connects to the app over loopback, so if nothing on the network needs
the unencrypted `http://<ip>:8000`:

```bash
sudoedit /opt/edge-cctv/edge_backend/.env        # set HOST=127.0.0.1 (add the line if missing)
sudo bash /opt/edge-cctv/deploy/install.sh     # re-applies the unit, removes the tailscale0:8000 ufw rule
ss -ltnp | grep :8000                           # expect 127.0.0.1:8000 only
```

This also ends access over a VPN IP address. To keep store PCs or a VPN on
the plain address, leave `HOST=0.0.0.0` and limit the port instead, e.g.
`sudo ufw allow from 192.168.1.0/24 to any port 8000 proto tcp` (your
subnet). Online access needs `HOST` to be `0.0.0.0` or `127.0.0.1` (frpc
forwards to `127.0.0.1`; the status says so otherwise).

### Trusted proxies: who is believed about the client

frps appends the VPS proxy's own address to `X-Forwarded-For` after the
visitor's (which the VPS proxy appended), and overwrites `X-Forwarded-Proto`
with `http`; `X-Real-IP` passes through unchanged. Each proxy in front of the
VPS (the *Proxies in front of your server* setting, e.g. a CDN) moves the
visitor one entry further left. So:

| Arrives as | Classified | Client address (lockouts, rate limits, audit) | HTTPS (HSTS) |
|---|---|---|---|
| Peer 127.0.0.1/::1 with `Host` = the configured public address (the tunnel) | **remote** | 2nd entry from the right of `X-Forwarded-For`, 3rd with one proxy in front (never `X-Real-IP`) | yes |
| `Host` = the public address from any other peer | **remote** | the TCP peer | only if the connection was TLS |
| Peer loopback, other `Host`, with `X-Forwarded-For` (another local proxy) | private | right-most `X-Forwarded-For` | `X-Forwarded-Proto: https` |
| Any other peer (store LAN, VPN address) | private | the TCP peer; forwarding headers ignored | only if the connection was TLS |

The launchers (unit, `entrypoint.sh`, `run.sh`) start uvicorn with
`--no-proxy-headers` so the app sees the real peer; with uvicorn's own
rewriting on, visitors would all appear as the VPS proxy (still remote, just
less precise lockouts; the log warns once). Being classified remote only adds
restrictions, so a LAN client sending the public `Host` gains nothing. The VPS
proxy must connect straight to frps and set `X-Forwarded-For` (nginx, Traefik,
Caddy and NPM do by default), and the proxies-in-front setting must not be
higher than the real count, or a visitor could choose the address used for
lockouts. (A hop that replaces the header instead of appending is safe: the
left-most entry, written by a trusted hop, is then used.)

### What is hardened when the dashboard is public

- First-run setup (`/api/v1/setup/*`, which creates the owner account from the
  one-time code) is refused for remote requests. Create the operator account
  on the store network first.
- `/docs`, `/redoc` and `/openapi.json` exist only with `DEBUG=true`, and even
  then never through the public address.
- Online access cannot be enabled, frpc is not started, and remote requests
  are refused, while `AUTH_DISABLED=true`.
- Failed sign-ins and bad tokens are counted per visitor address (above), so
  one visitor cannot lock out the others.
- Security headers on every response: a Content-Security-Policy that allows
  scripts only from this origin plus the pages' own inline blocks by SHA-256
  (no CDN: Chart.js is served from `static/vendor/`), `frame-ancestors
  'self'`, `object-src 'none'`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: same-origin`, and HSTS only when the request really came
  over HTTPS (the tunnel), never on plain HTTP. Inline `onclick=` handlers are
  still allowed (`script-src-attr`).
- No `server: uvicorn` banner (`--no-server-header`).
- The session cookie is `SameSite=Strict` and `Secure` when the page is HTTPS.
- CORS is limited to this device's own origins (the public address,
  `EDGE_BASE_URL`, explicit `ALLOWED_CORS_ORIGINS` entries; `*` is ignored).
- API responses carry `Cache-Control: no-store`.
- On the VPS: rate limits, optional basic-auth/SSO in front, no published frps
  ports, token per store (`deploy/vps/README.md`, security checklist).

### Speed over a slow uplink

The box is often on store Wi-Fi behind a home-grade uplink. Measured from
outside: ~360 ms round trip and ~55 KB/s per request. What keeps page loads
short (`app/services/web_delivery.py`):

- gzip for HTML, CSS, JS, JSON and SVG above 1 KB (~740 KB → ~270 KB for a
  first dashboard load, Chart.js included). The MJPEG stream, images, video
  and event streams are never compressed or buffered.
- Asset URLs carry a hash of the file (`?v=<hash>`, computed by the server),
  served with `Cache-Control: public, max-age=31536000, immutable`: a repeat
  visit downloads only the ~13 KB page (or a 304). Editing a file changes its
  hash, so nothing stale is ever used.
- Scripts are deferred, so the page renders before Chart.js and the modules
  arrive.
- The VPS proxy speaks HTTP/2 to browsers; frp multiplexes the requests over
  the one tunnel connection.

### Handover to a customer

When the box moves from the installer's test domain to the customer's own:

1. On the VPS that will serve the customer (theirs or the installer's): add
   the store as in `deploy/vps/README.md` ("Add a store") with a **new store
   token** and the customer's name, e.g. `store1-cctv.<customer-domain>`.
2. **Settings → Online access**: enter the new public address, tunnel server
   and store ID, **Replace token** with the new one, keep *Enable* ticked,
   **Save**. frpc restarts with them; the *Verified* badge resets.
3. Wait for *Connected since …*, press **Verify now**, confirm *Verified*.
4. Revoke the old store ID/token in the test VPS's store check.
5. Dashboard: **change the operator password** (and remove installer
   accounts); the customer sets their own.
6. **Rotate phone pairing**: revoke every paired phone under **Settings →
   Paired phones** and pair the customer's phones afresh with new codes.
7. Remove the installer's VPN access to the box, if any.
8. From a phone on mobile data: open `https://cctv.<customer-domain>/dashboard`,
   and check the old test URL no longer loads.

### Why there is no TURN server (coturn) by default

TURN relays WebRTC media when a phone (for example on 4G) cannot reach the
store's video gateway directly. That needs a relay with public UDP ports
(3478 and 49152–49252) open to the internet and a shared secret — more
exposed surface, more to configure, and more bandwidth through the store's
uplink.

It is no longer needed: remote viewing goes over HTTPS through the tunnel.
The dashboard's live view and Camera Studio use MJPEG (`/stream`, same
origin, token in the query string, which the tunnel passes through
unbuffered), so they work unchanged through the public hostname. Only the
app's port is published; go2rtc's own port (1984) never is. No public UDP
ports and no TURN server are required.

WebRTC therefore stays **LAN-only**: `GET /api/v1/webrtc/ice-servers`
returns STUN only with `"turn_enabled": false, "webrtc_scope": "lan_only"`,
and off-site clients use `/stream`. Coturn is kept as an **opt-in compose
profile** for sites that specifically want WebRTC off-site:

```bash
# in edge_backend/.env
TURN_ENABLED=true
COTURN_SECRET=<python3 -c "import secrets; print(secrets.token_urlsafe(64))">
COTURN_PUBLIC_IP=<the store's public IP>
# then
docker compose --profile turn up -d
```

`docker compose up -d` without the profile does not start coturn and does
not need `COTURN_SECRET`; the coturn container itself refuses to start when
the secret is empty or a placeholder.

---

## 8. What this system does not do

Stated plainly so nobody plans around a capability that is not there:

- **No demographics.** No age or gender classifier is included; that panel says
  so rather than showing invented percentages.
- **No proof of theft.** The live pipeline flags suspicious behaviour
  (concealment, shelf sweeping, loitering at high-value products, exit without
  passing checkout) for a person to review. An incident is a prompt to check,
  never a finding; its outcome is recorded by staff.
- **No continuous recording.** The store's NAS records video; this device keeps
  only alert evidence (stills, optional short clips) on its own disk, capped and
  deleted oldest first, and never writes to the NAS.
- **No face recognition or identities.** Tracks are anonymous and per camera.
- **No cross-camera re-identification.** A shopper crossing between cameras is
  currently counted once per camera.
- **Forecasting needs history.** The hourly forecast is the per-hour mean of
  this store's own recorded days and returns `sufficient_history: false` until
  at least three days exist.

Every one of these is reported by the API as unavailable rather than filled
with a plausible-looking number.
