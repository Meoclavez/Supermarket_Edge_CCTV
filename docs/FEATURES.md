# Edge AI CCTV: Dashboard Feature Reference

Last updated: 2026-10-01 · Describes main at e451706 (RTMO-s pose model; online access live; direct peer-to-peer live video)

The Edge AI CCTV dashboard runs on the in-store edge device. It shows live cameras, counts shoppers,
maps them onto a store plan, and flags suspicious behaviour for staff to review. It also produces
recommendations from what it measured. Operators do all site-specific setup in the dashboard, while
the installer sets a small number of device-level options once in `edge_backend/.env`. This document
lists what exists today, grouped by dashboard area, followed by features that are planned or in trial.

**How to read this:** paths use the exact UI labels, e.g. Cameras → tile → "Settings". "(per camera)"
marks settings saved for one camera; "(per viewer)" marks preferences kept in that browser only; anything
else is site-wide. "Installer-level" means the setting is in `edge_backend/.env`, not in the dashboard.

> **Principles**
> - **Honest data.** Every figure is measured. When a value is not measured, the dashboard shows a dash
>   with the reason ("not recorded", "needs sales data"). It never shows an invented number or a zero.
> - **Evidence-only storage.** The device never records continuously; the store's NAS does that, and this
>   software never writes to it. The device keeps only alert evidence (a still per alert, optionally a
>   short clip) on its own disk, and deletes the oldest first when the limit is reached.
> - **Site setup in the dashboard.** Cameras, purposes, lines, areas, masks, calibration and schedules are
>   all configured in the dashboard, not in files.
> - **Store time.** Schedules and alert times are in the store's time zone. Night watch also shows the
>   viewer's own time when it differs.
> - **Privacy masks.** Masks are burned into every picture that is shown or saved, including evidence.
>   If a mask cannot be applied, the whole frame is blacked out.
>   Exception: direct live video (see "Live video" under Access and security), and pictures taken over it, show the camera's own
>   picture: privacy masks and detector boxes are not drawn on them.

## Sign-in and navigation

| Feature | What it's for | Where / how to configure |
|---|---|---|
| First-run account | Creates the owner account; needs the one-time setup code the device prints at start. | Shown automatically on first visit (store network or a private VPN only; refused through online access) |
| Sign in / "Sign out" | Every page and API call needs a signed-in operator. | Sign-in form; "Sign out" in the header |
| "Forgot password?" | Explains how to reset access on the device. | Link on the sign-in form |
| Main tabs | "Today", "Cameras", "Store map", "Insights", "Loss prevention", plus "Settings" (gear). | Header; phones get a bottom tab bar |
| Theft alert banner | Shows the newest suspicious-behaviour alert on every page. | Banner → "Review now" / "Dismiss" |
| Colour theme | Light, dark, or follow the device. | Header switch, or Settings → "Appearance" |

## Today

| Feature | What it's for | Where / how to configure |
|---|---|---|
| At-a-glance figures | "Cameras working", "In the store now", "Visitors today", "Busiest hour so far", "Average visit", "Sales today". | Automatic. Sales need Settings → "Sales data" |
| "Visitors by hour" | Today's visitors by hour against yesterday and the same day last week; unrecorded hours are hatched. | Automatic; footfall comes from entrance lines (see Camera setup) |
| "Top recommendations" | The highest-priority open recommendations. | "All insights" opens Insights |
| "Needs review" | Loss-prevention incidents waiting for a person to check. | "Open loss prevention" |
| "Store setup" | Setup score per analysis and the next step to take, as a button. Also lists any camera added twice (see "Same camera added twice"). | Follow the suggested step; "Resolve in Cameras" |

## Cameras

| Feature | What it's for | Where / how to configure |
|---|---|---|
| Live grid | Four live tiles; click a tile to enlarge it with smoother live video. Opened through online access, each tile plays direct live video from the device (badge "LIVE · Direct"), only while it is on screen. | Cameras |
| Rotation and pins | With more than four cameras, tiles rotate. "Pin" keeps a camera in its tile. | "Previous" / "Pause" / "Next", "Change every" (seconds), tile "Pin" (per viewer) |
| "Area:" filter and search | Filters tiles by camera purpose, or finds a camera by name. | Cameras toolbar |
| "Video smoothness" | Tile refresh (Low 5 s, Normal 2 s, High 1 s) and enlarged-view frame rate, for store-network pictures. | Cameras toolbar (per viewer) |
| Tile status | Shows online status, picture size, people seen, picture age, and wrong-password warnings. | Automatic; "Reconnect" on an offline tile |
| Camera on/off | "Turn off" stops a camera's video, analysis and network traffic until it is turned on again. | Tile "Turn off"; bar under the grid: "Turn on", or turn off all offline cameras |
| Camera settings | Sets the name, channel, stream address, sign-in, capture FPS, resolution and placement. | Cameras → tile → "Settings" (per camera) |
| "Camera purpose" | Eight presets, e.g. "Entrance / exit door", "Checkout / cashier", "High-value area"; sets default features and a checklist. | Tile → "Settings" → "Camera purpose" → "Use this purpose" (per camera) |
| "AI features on this camera" | Turns "People counting & dwell", "Shelf interaction" and "Theft detection" on or off. | Tile → "Settings" (per camera) |
| "Largest person size" | Rejects oversized false boxes; raise it for cameras mounted close to shoppers. | Tile → "Settings" (per camera); default is installer-level |
| "Stream quality" | Which recorder stream a Dahua channel is analysed from. "Auto" (default) picks the smallest sub-stream of at least D1 (704x576) among sub-stream 1 and 2, measured once per camera (read-only); if none reaches D1 it keeps the current sub-stream and says "D1 not available". It never picks the main stream by itself. "Sub-stream" / "Main stream" force one. The chosen stream, its size and the reason show in the dialog and on the tile's picture-size badge. | Tile → "Settings" → "Stream quality", "Check sub-streams again" (per camera); automatic measuring is installer-level (`STREAM_AUTO_SELECT`) |
| "Till / register for this lane" | Links a checkout camera to its POS register so its sales match its shoppers. | Tile → "Settings" (Checkout purpose) → "Link register" |
| "Department label" | Groups a camera's alerts in reports. | Tile → "Settings" (per camera) |
| "Remove camera" | Removes the camera and stops its pipeline, after an inline confirmation. | Tile → "Settings" → "Remove camera" |
| "Same camera added twice" | Warns when one physical camera is configured more than once: the same recorder channel on two streams (e.g. typed as a main-stream URL and added again from the recorder's channel list), or a recorder channel and the camera's own IP address (matched through the Dahua recorder's connected-camera list, read once a day or on request). A setup warning, not a failure. One camera per group counts in store totals (visitors, in store now, dwell, areas, funnel, floor heatmaps, forecast); the others show "Duplicate — excluded from store totals" and stay viewable. The primary is the NVR-channel sub-stream (an enabled camera first), else the oldest. Actions: "Keep A, turn off B", "Keep A, remove B" (shows what is deleted and kept, with counts, before the inline confirm), "Make B primary", "They're different cameras" (remembered). | Banner in Cameras and in Store map → "Cameras & devices"; "Read its camera list now" when a recorder's list is not read |
| Duplicate check on add | Adding a camera that is already configured (by address, scan, or Dahua recorder channels) is stopped before anything is stored, naming the existing camera and recommending its NVR channel. "Use existing camera" keeps things as they are; "Add anyway" is for a genuine second stream (e.g. a main-stream close-up), which is then excluded from store totals. | "+ Add camera", scan "Add", "Dahua recorder" → "Adopt Selected" ("Leave those channels out" / "Add anyway") |

### Camera setup (per camera drawing tools)

Opened from Cameras → tile → "Camera setup". All shapes are drawn on the camera picture and saved per camera.

| Feature | What it's for | Where / how to configure |
|---|---|---|
| "Tripwire" | Counts people crossing a line in or out; an entrance line counts store footfall. | In side ("Flip"), "Counts store footfall (entrance line)", optional "Alert staff on crossing" |
| "Restricted area" | Alerts when someone is inside during set hours, e.g. after closing. | "Schedule", time windows, "Minimum dwell", "Alert severity", "Time zone" |
| "Checkout / queue area" | Times how long people stand at a till or wait in line; no calibration needed. | "Kind": "Checkout lane" or "Queue line" |
| "Product shelf" | Records hand reaches into a shelf and links them to a product. | Product name, SKU, price, "Shelf level", "Value tier", "Placement", study metrics |
| "Privacy mask" | Hides part of the picture, or excludes it from analysis. | "Mask mode": Blur, Mosaic, Blackout, Solid colour, "Ignore for analysis (video unchanged)" |
| "Save snapshot" / "Export incident clip" | Saves a still or a short clip to the device's evidence storage. | Camera setup, below the video |
| "Pipeline telemetry" | Shows capture FPS, detections, live tracks, frame size, frame age and calibration state. | Camera setup side panel (read-only) |

## Store map and calibration

| Feature | What it's for | Where / how to configure |
|---|---|---|
| Blueprint editor | Draws the building in metres: "Room", "Wall", "Shelf", "Door". | Store map → "Edit map" (on phones) → tools; "Store size", "Snap 0.5 m" |
| "Store area" | Floor areas where visits, unique people, dwell and occupancy are measured. | Store map → "Store area"; name, "Category" and colour in the inspector |
| Live people | Positions of tracked people from calibrated cameras. | Store map → "Show:" "People" |
| Heatmap | Where people "Walked", "Stopped" or "Touched shelves", for "Today", "Yesterday", "Last 7 days" or "Last 30 days". | Store map → "Heatmap" bar (per viewer) |
| Layers | Shows or hides walls and shelves, areas, cameras, people, labels and grid. | Store map → "Show:" |
| Camera placement | Sets camera position, bearing, field of view and mounting height on the plan. | "Cameras & devices" → "Place" / "Config", or drag on the map |
| Calibration | Matches 4+ floor points in the camera picture to the map so people can be placed on it. | "Cameras & devices" → "Calibrate" → "Save calibration", "Test accuracy" |
| Setup checklist | Lists what is still missing for a camera's purpose, with links to the right tool. | "Cameras & devices" → "Checklist" |
| Add cameras | Finds and adds cameras by network scan, by address (RTSP, MJPEG, ONVIF, USB, file) or from a Dahua recorder. | "Scan network", "+ Add camera" ("Test connection" first), "Dahua recorder" |

## Loss prevention and alerts

| Feature | What it's for | Where / how to configure |
|---|---|---|
| "Review queue" | Suspicious behaviour for staff to check, with an evidence image. It is not a finding of theft. | Loss prevention; filter chips |
| Checks (rules) | "Possible concealment" (a hand goes from a shelf to the pocket / waistband area and stays), "Possible shelf sweeping", "Loitering at high-value products", "Exit without passing checkout". | Per camera: "Theft detection" feature and camera purpose |
| Incident actions | Records who handled it and what really happened. | "I'm checking", "Mark: staff sent", "Record outcome", "False alarm", "Watch camera now" |
| Figures | "Waiting for review", "Flagged today", "Value in confirmed incidents", "False-alarm rate". | Automatic, from recorded outcomes |
| "False alarms by rule" | Shows which checks are often wrong and need tuning. | Automatic |
| Evidence viewer | Full-size annotated frame and person crop; expired evidence is labelled. | Click an evidence image |
| Phone alerts | Sends alerts to paired phones, including tripwire and restricted-area alerts. | Settings → "Phones" (see Settings) |

## Night watch

| Feature | What it's for | Where / how to configure |
|---|---|---|
| "Watch this camera at night" | Pauses normal analysis and watches for movement. A person seen is a high alert. | Cameras → tile → "Settings" → "Night watch" (per camera) |
| Schedule | Night window in store time; an end before the start runs into the next morning. | "Start", "End", "Nights starting on" |
| Low-light trigger | Also watches whenever the camera is in IR or low light. | "Also watch whenever this camera is in IR / low light" |
| Tuning | Controls how much movement counts and the quiet time after an alert. | "Motion sensitivity", "Quiet time after an alert (minutes)" |
| Night watch card | Shows cameras watching now, the next window, and recent night events with image or clip. | Loss prevention → "Night watch" |

## Insights

| Feature | What it's for | Where / how to configure |
|---|---|---|
| "Recommendations" | Suggested changes from measured visits, dwell, reaches and heatmaps. Runs hourly. | Insights → "Run analysis now"; "Mark reviewed", "Done", "Dismiss", "Reopen" |
| AI summary | Optional short written summary by the local AI model. | "Add AI summary (slower)"; model address is installer-level |
| "Visitors by hour" | Hourly visitors against yesterday and the same day last week. | Insights → "Shoppers & footfall" |
| "Shopper journey" | Stages from visit to purchase; "BOUGHT" needs sales data. | Insights → "Shoppers & footfall" |
| "Entrances today" | In and out per counting line, and an estimate of people inside now. | Tripwires in Camera setup |
| "Areas where shoppers leave without engaging" | Areas with visits but few shelf reaches. | Needs store areas, calibration and product shelves |
| "Product shelf reaches today" | Reaches per product and shelf level; conversion appears only with sales data. | Product shelves in Camera setup |
| "Heatmap history" | Recorded hourly heatmaps: play a day, compare periods, record the current hour now. | Insights → "Shoppers & footfall" → "Play day", "Compare with…", "Record now" |
| "Shopper profile" | States that no age or basket classifier is enabled; no figures are shown. | Not available (see Principles) |
| "Daily report" | Printable summary of the store's day. | Insights → "Daily report" → "Print / save PDF" |

## Settings and system

| Feature | What it's for | Where / how to configure |
|---|---|---|
| "Cameras & recorders" | Shortcut to camera management on the Store map. | Settings → "Cameras & recorders" |
| "Recorder sub-streams" | Upgrades a Dahua recorder's CIF (352x288) sub-streams to D1 (704x480 on NTSC) so analysis sees far shoppers. "Check sub-streams" reads each channel (read-only) and lists what would change; ticked channels are switched one at a time after an inline confirmation. Each channel's settings are saved first; the change is kept only if the recorder accepts it and the camera really sends D1, otherwise they are put back. Per channel it reports "switched to D1", "already D1 or higher", "kept CIF: D1 not supported by this camera", "kept CIF: NVR refused (…)" or "restored after failure (…)". A rejected recorder sign-in stops at once (no retries). Every change is logged (who, when, before/after). | Settings → "Recorder sub-streams" → "Check sub-streams" → "Upgrade CIF sub-streams to D1"; "Restore" per channel or "Restore previous stream settings" |
| "Sub-stream bit rate" | Raises the recorder sub-stream bit rate (default 768 kbps) of channels below the target, e.g. D1 channels left at a CIF-era 512 kbps. Only the bit rate changes (size, codec, frame rate and GOP stay); channels at or above the target are left alone; the camera's maximum is respected ("capped at N kbps by the camera"). Same safety as the D1 upgrade: settings saved first, re-read, the live sub-stream must still deliver video at the same size, put back on any failure, stop on a rejected sign-in, one channel at a time, logged. Per channel: "raised 512→768 kbps (stream verified)", "already ≥ 768 kbps", "kept 512 kbps: NVR refused (…)", "restored after failure (…)". "Restore previous stream settings" puts the old bit rate back. | Settings → "Recorder sub-streams" → "Check sub-streams" → "Sub-stream bit rate" → "Raise to N kbps" (inline confirm) |
| Device | Shows the device ID and sets the device name. | Settings → "Device" → "Rename" |
| "Sales data (point of sale)" | Shows whether till data is arriving and how to connect the tills. | Settings → "Sales data" → "How to connect your tills" |
| "System health" | Video decoding, detection engine and speed, processor, memory, uptime, evidence storage use. | Settings → "System health" (read-only) |
| Person detection model | RTMO-s (Apache-2.0) finds each person and 17 body points in one pass. Needs camera streams of D1 (704x576) or larger; on CIF (352x288) it misses far and partly hidden people. | Automatic; model and speed shown in Settings → "System health" |
| "Pose model trial (shadow only)" | Compares a candidate pose model with the live one; never changes counts or alerts. Off unless the installer sets one. | Settings → "System health"; trial is installer-level |
| "Appearance" | Colour theme for this browser. | Settings → "Appearance" |

## Access and security

| Feature | What it's for | Where / how to configure |
|---|---|---|
| "Online access" | Publishes the dashboard at `https://<store>-cctv.ikorex.com.au` (e.g. pearcedale-cctv) through the iKorex server: the device dials out, so no router port forward. The server only passes pages, data and video set-up; it never carries live video. Live status Stopped / Connecting / Connected since / Error. | Settings → "Online access": "Public address", "Tunnel server" (`wss://tunnel.ikorex.com.au`), "Store ID", "Store token" and optional "Server key" (write-only), "Proxies in front of your server" = One (Cloudflare), "Enable online access". Installer: `sudo EDGE_TUNNEL=1 bash deploy/install.sh` fetches the tunnel program. Server side: `~/Projects-1/Server/cctv-tunnel` (DEPLOYMENT.md §7a) |
| Access check | Confirms the public address really reaches this device. | Settings → "Online access" → "Verify now" |
| "Live video" (direct) | Remote viewers get live video straight from the device to their browser (peer-to-peer WebRTC), never through the server. Video runs only while a tile is on screen and stops on rotate, enlarging another tile, view switch, hidden tab or page close (a vanished viewer is cut within about 45 s). H.265 cameras are converted to H.264 on the device's GPU (about 3% of a core per tile). Pictures needed by other remote views (floor-map thumbnail, heatmap background, calibration) are taken once over a direct connection; press "Take a new picture" to refresh. Stored evidence still opens on click. | Settings → "Online access · Live video": "STUN servers" (default `stun:stun.ikorex.com.au:3478`), "Connection mode" (Automatic: no router change / Fixed port: forwarded on the store router), "Most live videos at once" (8), "Live video on the store network (every viewer)", "Live video on this network" (per viewer), "Watching now" |
| "Check remote video" | Tests the browser's and the device's STUN results, the store router's NAT type and recent connections, and says in plain words whether direct video works and what to change. | Settings → "Online access · Live video" → "Check remote video" (administrators) |
| "Pair a phone" | Pairs the mobile app with a one-time code and QR. | Settings → "Phones" → "Pair a phone" |
| "Push notifications" | Firebase key for phone pushes; test sends and delivery log. | Settings → "Push notifications" → "Upload", "Send test alert" |
| "Paired phones" | Controls which alerts each phone receives, or removes a phone. | "Alert settings" (types, "Minimum severity", cameras, "Quiet hours"), "Rename", "Revoke" |
| Public-mode hardening | Blocks first-run setup and API docs remotely; counts failed sign-ins per visitor; adds strict security headers. | Automatic (docs/DEPLOYMENT.md §7a) |

## Available through the API only

These features have no dashboard screen. They are listed so they are not rebuilt by mistake.

| Feature | What it's for | Access |
|---|---|---|
| Sales ingest | Till systems push each sale to the device. | `POST` endpoint shown in Settings → "Sales data" |
| Queue and checkout times | Wait and service times from checkout / queue areas. | `GET /api/v1/analytics/queues` |
| Backups | A database snapshot is taken at every start (kept 7 days); on-demand backups are also possible. | `GET /api/v1/system/backups`, `POST /api/v1/system/backup` |
| Factory reset | Deletes all layout, cameras and recorded data; keeps the account and `.env`. | `POST /api/v1/layout/reset` with `{"confirm":"RESET"}` |
| Pose trial detail | Trial figures by camera, lighting and person size. | `GET /api/v1/system/shadow-trial` |

## Upcoming

| Feature | Purpose | Status | How it will be configured |
|---|---|---|---|
| False-detection checks | Rejects detections with too few visible joints (keypoint-count gate) and fixed objects mistaken for people. | Planned | Automatic, with installer-level thresholds |
| Focus crops (ROI) | Finds distant people on high-resolution cameras by analysing chosen regions at full detail. | Planned | Drawn per camera in the dashboard |
| Staff areas and uniform recognition | Separates staff from shoppers: staff / not staff / undetermined. Runs one week in shadow mode before alerts. IR at night falls back to schedule rules. | Planned | Draw staff-only areas; teach a uniform by clicking a staff member on a live tile |
| Outside camera positions | Street front, rear / loading dock and car park positions, each with suggested feature presets. | Planned | Selected per camera in the dashboard |
| Outdoor features | Perimeter lines and areas for night watch, loitering, dock activity log, camera tamper alerts, vehicle dwell (exterior only), left objects (experimental). | Planned | Drawn and switched on per camera in the dashboard |
| Shopper attention ("eye-sight beam") | Phase 0: full camera calibration. Phase 1: aisle-side / 1 m shelf-bay attention from the skeleton, attention heatmap and looked→reached funnel. | Planned | Calibration and shelf bays in the dashboard |
| Shopper attention, later phases | Head-pose model and planogram. Product-level attention is not expected from CCTV. | Planned | In the dashboard |
| Detector boxes on direct video | Draw the detector's boxes over remote (direct) live video; today it shows the raw camera picture. | Planned | Automatic |
| Faster remote rotation | Converted (H.265) cameras take about 4 s to show a first frame, so with the default 10 s rotation remote tiles show video about half the time. Options: a longer rotation for remote viewers, or opening the next camera before the switch. | Owner to decide | "Change every" on the Cameras toolbar |
| Phone app live video away from the store | The app still asks for the old picture stream, which online access refuses; it needs the direct-video session API (fixes listed in `mobile_app/UPCOMING_FIXES.md`). Talk-back was removed on 2026-09-29. | Planned (not built until the owner asks) | Phone app |
| Licence inventory | Third-party notices for every bundled component before commercial sale. | Started: models and key libraries in `THIRD_PARTY_NOTICES.md`; body7 training-data legal check pending | Not a setting |

## Installer-level settings

Set once in `edge_backend/.env`; restart the service to apply. The full list with explanations is in
`edge_backend/.env.example`.

| Setting | What it controls | Default |
|---|---|---|
| `STORE_NAME`, `SITE_TIMEZONE` | Store name in the header; store time zone for "today", schedules and alerts. | Host's time zone |
| `DECODE_BACKEND`, `DECODE_DEVICE` | GPU video decoding (NVDEC, VA-API) with software fallback. | `auto` |
| `DECODE_MAX_FPS`, `DECODE_MAX_WIDTH` | Decoded frame rate and width per camera; lowers CPU use. | `5`, `auto` |
| `PERSON_CONF_THRESHOLD` (`_DARK`) | Detection confidence; lower in dark boxes. Tune against real footage. | `0.50` (`0.45`) |
| `PERSON_MAX_FRAME_FRACTION` | Default largest person size (overridable per camera). | `0.35` |
| `POSE_BUDGET_UTILISATION` | Share of the accelerator the live analysis plans to use. | `0.6` |
| `STORAGE_DIR` | Evidence and database location; must stay on the device's own disk. | `<project>/storage` |
| `EVIDENCE_MAX_GB`, `STORAGE_RETENTION_DAYS` | Evidence size limit and maximum age; oldest deleted first. | Automatic (10% of disk), 7 days |
| `NIGHT_WATCH_CLIP` | Also save a short clip with each night-watch event. | `false` |
| `HOST`, `PORT` | Network address the dashboard listens on. | `0.0.0.0`, `8000` |
| `OLLAMA_BASE_URL` | Local AI model used for the optional written summary. | `http://localhost:11434` |
