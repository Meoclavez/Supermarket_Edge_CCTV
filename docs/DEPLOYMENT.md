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
| Disk | 256 GB SSD + NAS | Recordings go to `STORAGE_DIR` |
| Network | Wired gigabit | Cameras and the server on the same VLAN |

**No hardware is selected at build time.** On startup the system probes for a
Hailo NPU, then TensorRT, CUDA, MIGraphX (AMD), OpenVINO, and finally CPU, and
uses the first that really takes the model. Moving the install to a machine
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
| `EDGE_MODELS_FROM` | unset | a directory of `*.onnx` files to copy into `edge_backend/models/` instead of exporting them on this machine |

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
time, about 7 min for the default set; a few seconds on later runs), so the
service starts on the GPU straight away.

If the cache is cold at service start (a new model, a changed camera count
that selects another model), the service starts on the CPU, compiles in a
separate process, and switches to the GPU when done; `/api/v1/health` shows
`inference_gpu_compile: "compiling"` and `inference_provider: "cpu"` until
then. `MIGRAPHX_COMPILE_MODE=foreground` blocks start-up instead. A compile
peaks at about 2.5 GB, within the unit's `MemoryMax=8G`.

Measured on an RX 9060 XT (fp32, same detections as the CPU within 0.002):
YOLO26m-pose 960x544 14 ms/frame vs about 315 ms on 6 CPU threads.
`--ort cpu` (or `EDGE_ORT=cpu`) keeps the small CPU-only build.

When inference does run on the CPU, ONNX Runtime is limited to
`INFERENCE_CPU_THREADS` threads in total (default: half the CPUs) so it cannot
starve video decoding and recording.

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
Setup is refused through the public remote-access hostname (section 7a), so
do this on the store network or over Tailscale.

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

### Viewing the dashboard over Tailscale

With Tailscale on the server, open `http://<tailscale-ip>:8000/dashboard` from
any device on the same tailnet (`tailscale ip -4` on the server prints the
address). Two firewall rules make this work without exposing the dashboard
anywhere else; `deploy/install.sh` adds them when `ufw` is active:

```bash
sudo ufw allow in on tailscale0 to any port 8000 proto tcp   # dashboard, tailnet only
sudo ufw allow 41641/udp                                      # Tailscale direct connections
```

Without UDP 41641 open, peers still connect but through a Tailscale DERP relay,
which is slower and adds latency to live video. `tailscale ping <peer>` shows
which you have: `via DERP(...)` is relayed, `via <ip>:<port>` is direct.

Tailscale peers count as local, not remote: the app treats a request as
remote only when it names the public remote-access hostname or arrives
through the local tunnel proxy with Cloudflare headers
(`is_remote_request` in `app/services/public_exposure.py`). A request to the
`100.x` address therefore gets the same access as one from the store LAN,
including first-run setup, so only add devices you trust to the tailnet.

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
- **Real shoppers missed** → lower `PERSON_CONF_THRESHOLD`, and check the
  camera is not so high that people appear wider than tall.
- **CPU saturated** → raise `ANALYTICS_DETECT_EVERY_N_FRAMES`, or fit a GPU.

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
pruned to 7 days. Copy that directory to the NAS on a schedule.

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

## 7a. Remote access: the dashboard on your own domain

The dashboard can be published at `https://cctv.<your-domain>` so the owner
can open it from anywhere. It is online **only** while remote access is
enabled in **Settings → Remote access** and the hostname is connected;
disable it there and the public address stops working within seconds.
Nothing moves to the cloud: footage, detection and the database stay on this
machine, and the tunnel carries only the pages and streams a signed-in
operator asks for.

### How it works

The primary provider is a **Cloudflare Tunnel** that the app runs itself
(`cloudflared tunnel run`, supervised and restarted with backoff by
`app/services/remote_access_service.py`). The tunnel is an *outbound*
connection from the store to Cloudflare, so:

- no router port forwarding and no public IP are needed, and it works behind
  carrier-grade NAT (4G/5G routers, NBN CGNAT);
- Cloudflare issues and renews the HTTPS certificate for your domain;
- the store's LAN address keeps working exactly as before.

The tunnel token is stored encrypted under `storage/secrets/named/`, passed
to cloudflared in an environment variable (never on its command line), never
logged and never returned by the API — the dashboard only shows
"configured / not configured".

`cloudflared` is found on `PATH`, else in `<repo>/bin/`. `./run.sh
--with-tunnel` downloads the official release for this OS and CPU into `bin/`
and checks it against the SHA-256 Cloudflare publishes (this also happens
automatically on start when remote access is enabled). No sudo is used.

### Go live (step by step)

1. Put the domain on Cloudflare (free plan is enough): **Add a domain**, then
   change the nameservers at your registrar to the two Cloudflare shows. Wait
   until the domain shows **Active**.
2. In **Zero Trust** (one.dash.cloudflare.com) → **Networks → Tunnels →
   Create a tunnel** → **Cloudflared**. Name it after the store.
3. On *Install and run connector*, copy the command for any OS — **do not run
   it**. Paste the whole command (or just the long value starting `eyJ`) into
   **Settings → Remote access → Tunnel token** on the local dashboard.
4. **Public hostname**: subdomain `cctv`, your domain, **Service type** `HTTP`,
   **URL** `localhost:8000` (the port the app listens on). Save.
5. In the dashboard enter the same hostname, tick **Enable remote access**,
   press **Save**. The status goes *Connecting… → Connected*.
6. Press **Verify now**. The app fetches
   `https://<hostname>/api/v1/device/identity` and checks that the device id
   is *this* machine's, so a hostname routed to another store's box (or a
   stale tunnel) is caught. The badge shows *Verified* with the time.
7. Recommended: **Zero Trust → Access → Applications → Add a self-hosted
   application** for the hostname with an e-mail one-time-PIN policy, so
   Cloudflare challenges visitors before the dashboard's own sign-in. If the
   phone app must use the remote URL, add a bypass policy for `/api/*` and
   `/stream*` (those still require the app's token).

**Direct provider** (sites with a public IP and port forwarding): choose
*Direct* in Settings; the panel shows a Caddy site block for the hostname
(`reverse_proxy 127.0.0.1:8000`, `flush_interval -1` for the MJPEG streams).
Run Caddy yourself, forward TCP 80/443 to this machine, then *Verify now*.
The app does not run Caddy.

### What is hardened when the dashboard is public

- Requests from the internet all arrive from `cloudflared` on 127.0.0.1, so
  the real client address comes from `CF-Connecting-IP` / `X-Forwarded-For` —
  trusted **only** from a loopback peer. Lockouts and rate limits apply per
  remote user; a LAN client cannot spoof those headers.
- First-run setup (`/api/v1/setup/*`, which creates the owner account from the
  one-time code) and `/docs` / `/openapi.json` are refused through the public
  hostname. Create the operator account on the store network first.
- Remote access cannot be enabled, and remote requests are refused, while
  `AUTH_DISABLED=true`.
- Security headers on every response: `X-Content-Type-Options: nosniff`,
  `Content-Security-Policy: frame-ancestors 'self'` (Camera Studio's own
  iframes keep working), `Referrer-Policy: same-origin`, and HSTS only on
  responses served through the HTTPS hostname.
- CORS is limited to this device's own origins (the public hostname,
  `EDGE_BASE_URL`, explicit `ALLOWED_CORS_ORIGINS` entries; `*` is ignored).

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
- **No shelf-interaction tracking.** The engine and API exist but nothing
  produces the hand/pose events, so engagement and friction metrics stay blank
  and the merchandising rules are suppressed with a stated reason.
- **No theft detection from video.** The detection algorithms and incident
  lifecycle exist, but no live pipeline calls them. Incidents are only created
  by an explicit API call.
- **No cross-camera re-identification.** A shopper crossing between cameras is
  currently counted once per camera.
- **Forecasting needs history.** The hourly forecast is the per-hour mean of
  this store's own recorded days and returns `sufficient_history: false` until
  at least three days exist.

Every one of these is reported by the API as unavailable rather than filled
with a plausible-looking number.
