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
time; a few seconds on later runs), so the service starts on the GPU straight
away. Every rung of the pose ladder is compiled, not only the one start-up
picks, because the run-time scheduler (section 6) may step to any of them as
cameras come and go; a rung it needs that is still cold is compiled in a
separate process while the current model keeps serving.

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

**Recommended: private HTTPS with `tailscale serve`.** The owner and staff
open `https://<machine>.<tailnet>.ts.net/dashboard` from any device signed in
to the tailnet. Tailscale provides a real certificate and HTTP/2 (many files
over one connection, which matters on a slow store uplink), and nothing is
published outside the tailnet. Section 7a has the steps; in short:

1. Tailscale admin console → **DNS** (https://login.tailscale.com/admin/dns):
   turn on **MagicDNS** and **HTTPS Certificates**.
2. On the server: `sudo EDGE_TAILSCALE_SERVE=1 bash deploy/install.sh`
   (or by hand: `sudo tailscale serve --bg --https=443 http://127.0.0.1:8000`).
3. **Settings → Online access** shows the address and whether it is published.

**Plain HTTP on the tailnet address** keeps working as before:
`http://<tailscale-ip>:8000/dashboard` (`tailscale ip -4` prints the address).
Two firewall rules allow it without exposing the dashboard anywhere else;
`deploy/install.sh` adds them when `ufw` is active and `HOST` is not
`127.0.0.1`:

```bash
sudo ufw allow in on tailscale0 to any port 8000 proto tcp   # dashboard, tailnet only
sudo ufw allow 41641/udp                                      # Tailscale direct connections
```

Without UDP 41641 open, peers still connect but through a Tailscale DERP relay,
which is slower and adds latency to live video. `tailscale ping <peer>` shows
which you have: `via DERP(...)` is relayed, `via <ip>:<port>` is direct.

Tailnet requests count as local, not remote, whether they use the `.ts.net`
HTTPS name or the `100.x` address: they get the same access as the store LAN,
including first-run setup, so only add devices you trust to the tailnet. (A
request through **Tailscale Funnel**, which is public, is treated as remote.)
See "Trusted proxies" in section 7a.

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

### Many cameras: the inference budget

Detection does not run on every Nth frame of every camera any more: one
accelerator cannot do that for dozens of streams (33 cameras x 5 frames/s x
14 ms for YOLO26m-pose kept an RX 9060 XT 97 % busy, and detections went
stale). The service measures what each analysed frame really costs on the
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
then `yolo26m-pose-544x960` → `yolo26s-pose-544x960` → `yolo26s-pose` → ….
For 33 cameras with the defaults that means about 2 frames/s per camera on
the smaller 960x544 model instead of 1.2/s on the medium one; set
`ANALYTICS_TARGET_DETECT_FPS` equal to `ANALYTICS_MIN_DETECT_FPS` to prefer
the larger model. `detector.load_control` in
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

## 7a. Online access: private (Tailscale) and public (your domain)

Two routes, which can run side by side. Both are configured and monitored in
**Settings → Online access**; nothing moves to the cloud: footage, detection
and the database stay on this machine.

| Route | Address | Who can reach it | TLS |
|---|---|---|---|
| Private: `tailscale serve` | `https://<machine>.<tailnet>.ts.net` | devices signed in to the owner's tailnet | Tailscale certificate, HTTP/2 |
| Public: Cloudflare Tunnel | `https://cctv.<your-domain>` | anyone on the internet (put Cloudflare Access in front) | Cloudflare certificate, HTTP/2 / HTTP/3 |

Both proxies run on this machine and connect to the app over loopback
(`http://127.0.0.1:8000`), so once they are in place the app itself can stop
listening on the network (`HOST=127.0.0.1`, below).

### Private HTTPS for the owner and staff (tailscale serve)

1. Tailscale admin console → **DNS** (https://login.tailscale.com/admin/dns):
   turn on **MagicDNS** and **HTTPS Certificates** (once per tailnet).
2. On the server:

   ```bash
   sudo EDGE_TAILSCALE_SERVE=1 bash deploy/install.sh
   ```

   The installer checks that the tailnet has HTTPS certificates (and says what
   to turn on if not), then runs, only if not already configured,
   `tailscale serve --bg --https=443 http://127.0.0.1:<PORT>`. The setting is
   kept by tailscaled across reboots. To do it by hand:
   `sudo tailscale serve --bg --https=443 http://127.0.0.1:8000`;
   `tailscale serve status` shows it, `sudo tailscale serve --https=443 off`
   removes it.
3. **Settings → Online access** shows *Published on your tailnet* and the
   `https://<machine>.<tailnet>.ts.net/dashboard` link (read from
   `tailscale status --json`; the page says so when Tailscale is missing,
   stopped, or HTTPS certificates are off).
4. Staff install Tailscale on their phone/PC and are invited to the tailnet;
   they sign in to the dashboard with their operator account as usual.

Do **not** enable Tailscale Funnel for this port: that publishes it on the
internet. If it is on, the page warns and the app treats those requests as
remote (the public rules below apply).

### Public dashboard on your domain (Cloudflare Tunnel)

The app runs the tunnel itself (`cloudflared tunnel run`, supervised and
restarted with backoff by `app/services/remote_access_service.py`). The
tunnel is an *outbound* connection from the store to Cloudflare, so no router
port forwarding or public IP is needed, it works behind carrier-grade NAT
(4G/5G routers, NBN CGNAT), and Cloudflare issues and renews the certificate.
It is online **only** while enabled in **Settings → Online access**; disable
it there and the public address stops working within seconds.

The tunnel token is stored encrypted under `storage/secrets/named/`, passed
to cloudflared in an environment variable (never on its command line), never
logged and never returned by the API: the dashboard only shows
"configured / not configured". `cloudflared` is found on `PATH`, else in
`<repo>/bin/`; `./run.sh --with-tunnel` downloads the official release into
`bin/` and checks its published SHA-256 (also done automatically on start when
remote access is enabled). No sudo is used.

Nothing assumes a fixed domain: the hostname is a setting in the dashboard and
can change at any time (test domain now, the customer's domain at handover).
Changing it clears the *Verified* badge, the old name stops being treated as
this device's public name (CORS, remote rules), and a new token restarts
cloudflared with it.

#### Go live (step by step)

`<your-domain>` below is a placeholder: the owner's test domain now, the
customer's domain at handover.

1. Put the domain on Cloudflare (free plan is enough): **Add a domain**, then
   change the nameservers at your registrar to the two Cloudflare shows. Wait
   until the domain shows **Active**.
2. **Zero Trust** (one.dash.cloudflare.com) → **Networks → Tunnels → Create a
   tunnel** → **Cloudflared**. Name it after the store.
3. On *Install and run connector*, copy the command for any OS — **do not run
   it**. Paste the whole command (or just the long value starting `eyJ`) into
   **Settings → Online access → Tunnel token** on the dashboard.
4. **Public hostname**: subdomain `cctv`, domain `<your-domain>`, **Service
   type** `HTTP`, **URL** `127.0.0.1:8000`. Save. (Use `127.0.0.1`, not
   `localhost`: with `HOST=127.0.0.1` the app listens on IPv4 only.)
5. In the dashboard enter `cctv.<your-domain>`, tick **Enable remote access**,
   press **Save**. The status goes *Connecting… → Connected*.
6. Press **Verify now**. The app fetches
   `https://cctv.<your-domain>/api/v1/device/identity` and checks that the
   device id is *this* machine's, so a hostname routed to another store's box
   (or a stale tunnel) is caught. The badge shows *Verified* with the time.
7. **Strongly recommended — Cloudflare Access (e-mail one-time PIN):**
   Zero Trust → **Access → Applications → Add an application → Self-hosted**.
   Application domain `cctv.<your-domain>`, session duration e.g. 24 h.
   Policy *Allow* → *Include* → **Emails** → the owner's and staff addresses
   (or *Emails ending in* `@<customer-domain>`). Under **Authentication**
   enable **One-time PIN**. Cloudflare then challenges every visitor before
   the dashboard's own sign-in, so the app's login page is not exposed to the
   whole internet. The phone app cannot answer the e-mail challenge: if it
   must connect through the public hostname, add a second application for
   the paths `cctv.<your-domain>/api/*` and `cctv.<your-domain>/stream` with a
   **Bypass** policy (those still require the app's token), or keep the
   phone on Tailscale instead.
8. Open `https://cctv.<your-domain>/dashboard` from a phone on mobile data:
   Cloudflare's PIN page, then the dashboard sign-in.

**Direct provider** (sites with a public IP and port forwarding): choose
*Direct* in Settings; the panel shows a Caddy site block for the hostname
(`reverse_proxy 127.0.0.1:8000`, `flush_interval -1` for the MJPEG streams).
Run Caddy yourself, forward TCP 80/443 to this machine, then *Verify now*.
The app does not run Caddy.

### Close the plain-HTTP port (HOST=127.0.0.1)

Once the dashboard is reached through `tailscale serve` and/or cloudflared,
nothing needs the unencrypted `http://<ip>:8000` any more:

```bash
sudoedit /opt/edge-cctv/edge_backend/.env        # set HOST=127.0.0.1 (add the line if missing)
sudo bash /opt/edge-cctv/deploy/install.sh     # re-applies the unit, removes the tailscale0:8000 ufw rule
ss -ltnp | grep :8000                           # expect 127.0.0.1:8000 only
```

The systemd unit passes `--host ${HOST} --port ${PORT}` from `.env`
(default `0.0.0.0:8000`). If store PCs must keep using the LAN address, leave
`HOST=0.0.0.0` and limit the port to the LAN instead, e.g.
`sudo ufw allow from 192.168.1.0/24 to any port 8000 proto tcp` (your
subnet), with no rule for any other interface. First-run setup then needs the
LAN or the tailnet HTTPS address; it is never allowed through the public
hostname.

### Trusted proxies: who is believed about the client

| Arrives as | Classified | Client address (lockouts, rate limits, audit) | HTTPS (HSTS) |
|---|---|---|---|
| Peer 127.0.0.1/::1, `Host` `*.ts.net` (`tailscale serve`) | private (like the LAN) | `X-Forwarded-For` (set by tailscaled, never appended) | `X-Forwarded-Proto: https` |
| … plus `Tailscale-Funnel-Request` (Funnel) | **remote** | `X-Forwarded-For` | as above |
| Peer loopback with `CF-Connecting-IP` / `CF-Ray` (cloudflared) | **remote** | `CF-Connecting-IP` | `X-Forwarded-Proto` / `CF-Visitor` |
| `Host` = the configured public hostname (any peer) | **remote** | as per the row that applies | |
| Any other peer (store LAN, `100.x` tailnet address) | private | the TCP peer; forwarding headers ignored | only if the connection itself was TLS |

tailscaled deletes client-sent `Tailscale-User-*` / `Tailscale-Funnel-Request`
and replaces `X-Forwarded-*`; other client headers pass through, so a
`.ts.net` request carrying `CF-*` headers is still treated as Tailscale.
uvicorn's own proxy-header handling (on by default, trusting only
127.0.0.1/::1) may already have rewritten the peer and scheme;
`app/services/public_exposure.py` gives the same answers either way. Being
classified remote only adds restrictions, so a direct client forging
Cloudflare or Funnel headers gains nothing.

### What is hardened when the dashboard is public

- First-run setup (`/api/v1/setup/*`, which creates the owner account from the
  one-time code) is refused for remote requests. Create the operator account
  on the store network or the tailnet first.
- `/docs`, `/redoc` and `/openapi.json` exist only with `DEBUG=true`, and even
  then never through the public hostname. The dashboard and phone app do not
  use them.
- Remote access cannot be enabled, and remote requests are refused, while
  `AUTH_DISABLED=true`.
- Security headers on every response: a Content-Security-Policy that allows
  scripts only from this origin plus the pages' own inline blocks by SHA-256
  (no CDN: Chart.js is served from `static/vendor/`), `frame-ancestors
  'self'`, `object-src 'none'`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: same-origin`, and HSTS only when the request really came
  over HTTPS (Cloudflare or `tailscale serve`), never on plain HTTP. Inline
  `onclick=` handlers are still allowed (`script-src-attr`); moving them to
  `addEventListener` would allow removing that too.
- No `server: uvicorn` banner (`--no-server-header` in the unit,
  `entrypoint.sh` and `run.sh`).
- The session cookie is `SameSite=Lax` and `Secure` when the page is HTTPS.
- CORS is limited to this device's own origins (the public hostname,
  `EDGE_BASE_URL`, explicit `ALLOWED_CORS_ORIGINS` entries; `*` is ignored).
- API responses carry `Cache-Control: no-store`, so neither the browser nor
  Cloudflare keeps store data.

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
- HTTP/2 through `tailscale serve` or Cloudflare multiplexes all files over
  one connection; plain `http://<ip>:8000` is HTTP/1.1 (six connections).

### Handover to a customer

When the box moves from the installer's test domain to the customer's own:

1. In the **customer's** Cloudflare account (their domain on Cloudflare,
   step 1 above): create a **new tunnel** and a public hostname
   `cctv.<customer-domain>` → `HTTP` `127.0.0.1:8000`. Do not reuse the
   test tunnel.
2. **Settings → Online access**: enter `cctv.<customer-domain>`, paste the
   customer tunnel token (**Replace**), keep *Enable* ticked, **Save**. The
   old token is overwritten and cloudflared restarts with the new one; the
   *Verified* badge resets.
3. Wait for *Connected*, press **Verify now**, confirm *Verified*.
4. Customer Cloudflare account: **Access** application for
   `cctv.<customer-domain>` with an e-mail one-time-PIN policy listing the
   store's staff e-mails (step 7 above).
5. In the **test** Cloudflare account: delete the test tunnel and its public
   hostname / DNS record, so the old name no longer reaches this box.
6. Dashboard: **change the operator password** (and remove installer
   accounts); the customer sets their own.
7. **Rotate phone pairing**: revoke every paired phone under **Settings →
   Paired phones** and pair the customer's phones afresh with new codes.
8. Tailscale: move the box to the customer's tailnet (`sudo tailscale
   logout`, `sudo tailscale up` with their account, enable MagicDNS + HTTPS
   Certificates there, then `sudo EDGE_TAILSCALE_SERVE=1 bash
   deploy/install.sh`), or remove the installer's devices from the tailnet.
9. From a phone on mobile data: open `https://cctv.<customer-domain>/dashboard`
   (PIN, then sign-in), and check the old test URL no longer loads.

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
