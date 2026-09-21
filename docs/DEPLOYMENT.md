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
| GPU | Optional | Any CUDA GPU is detected and used automatically |
| Disk | 256 GB SSD + NAS | Recordings go to `STORAGE_DIR` |
| Network | Wired gigabit | Cameras and the server on the same VLAN |

**No hardware is selected at build time.** On startup the system probes for a
Hailo NPU, then TensorRT, CUDA, ROCm, OpenVINO, and finally CPU, and uses the
first that loads. Moving the install to a machine with a GPU needs no code
change — only `pip install onnxruntime-gpu`.

---

## 2. Install

```bash
sudo useradd --system --create-home --home-dir /opt/edge-cctv edgecctv
sudo usermod -aG video,render edgecctv          # camera + GPU access

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

## 4. Run

```bash
sudo cp /opt/edge-cctv/deploy/edge-cctv.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now edge-cctv
journalctl -u edge-cctv -f
```

Open `http://<server-ip>:8000/dashboard`. The first visit asks you to create
the operator account; after that it asks for sign-in.

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
