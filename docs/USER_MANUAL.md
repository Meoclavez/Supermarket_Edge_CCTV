# Edge AI CCTV: User Manual

Setting up, mapping and using the store dashboard

Last updated: 2026-10-04 · Describes the working tree after 5c21d13 (accounts and roles, site settings, backups, till key, area & line alerts, masked remote video) plus ignore areas, "Not a person" and static-figure memory

This manual takes a store from a freshly installed edge device to daily use. Part 1 covers the device
and the first sign-in. Part 2 is the store setup, in the order you should do it. Part 3 explains the
screens staff use every day. Part 4 covers looking after the system. Part 5 lists what the dashboard
cannot do yet.

Button and field names are written exactly as they appear on screen, in quotes, for example
"Save calibration". A path such as Settings → "Online access" means: open Settings, then go to the
"Online access" card. For a one-line summary of every feature, see `docs/FEATURES.md`. Installation
details are in `docs/DEPLOYMENT.md`.

**Who does what**

| Person | Tasks | Where |
|---|---|---|
| Installer | Installs the device, sets device-level options, connects online access on the iKorex server | Device command line, `edge_backend/.env`, iKorex server |
| Store owner or manager (Owner or Administrator account) | Creates the accounts, sets the store settings, maps the store, sets up cameras, pairs phones | Dashboard |
| Staff (Operator account) | Watch cameras, review and acknowledge alerts, record outcomes | Dashboard, phone |

**What the system does**, and nothing else:

1. **Loss prevention.** It flags suspicious behaviour for a person to check. An alert is never a
   finding of theft.
2. **Market analysis.** It measures visitors, dwell time, entrances, shelf reaches and the shopper
   journey, and turns them into recommendations.
3. **Customer heatmaps.** It shows where shoppers walk, stop and touch shelves.

**Ground rules for reading the dashboard**

- **Every figure is measured.** A value that has not been measured shows "—" with a reason, never
  `0` or a guess.
- **There is no continuous recording.** The store's NAS records. This device keeps only alert
  evidence (a still, and optionally a short clip) and deletes the oldest evidence first.
- **Store time.** "Today", schedules, alert times, the nightly backup, quiet hours and the daily
  report use the store's time zone (Settings → "Store" → "Time zone").
- **Privacy masks** are burned into every picture shown or saved, on the store network and in direct
  live video through online access (see 2.12). If a masked camera's picture cannot be masked, it is
  not shown.

---

## Part 1. The device and first sign-in

### 1.1 Requirements

- An edge PC running Linux with systemd (Ubuntu 22.04/24.04 or Arch), with at least 4 CPU cores,
  8 GB RAM (16 GB above about 12 cameras) and a 256 GB SSD.
- A wired gigabit link on the same network as the recorder (NVR) or cameras.
- A GPU is optional. The device detects what is present at start-up and uses the best option. With
  no accelerator it falls back to the processor.
- **Camera sub-streams of at least D1 (704x576).** The person detector misses distant and partly
  hidden people on CIF (352x288) streams. Step 2.5 shows how to upgrade a Dahua recorder from the
  dashboard.
- Connect cameras **as recorder channels**, not by typing each camera's own address. This gives one
  login and one place to manage stream settings, and avoids adding the same camera twice.

### 1.2 Installation (installer)

On the edge PC:

```bash
git clone <repository> Supermarket_Edge_CCTV
sudo bash Supermarket_Edge_CCTV/deploy/install.sh
# with online access: sudo EDGE_TUNNEL=1 bash Supermarket_Edge_CCTV/deploy/install.sh
```

The installer creates the `edgecctv` service user, installs the code in `/opt/edge-cctv`, downloads
and checks the models, writes `.env` from `.env.example`, installs the `edge-cctv` service and
firewall rules, then waits until the service answers. At the end it prints the **first-run setup
code**. Running the installer again is also how dependency updates are applied.

The store name and the store time zone are set in the dashboard, under Settings → "Store" (see
2.1). They apply at once, without a restart. `STORE_NAME` and `SITE_TIMEZONE` in
`/opt/edge-cctv/edge_backend/.env` are only the defaults used until someone saves a value in the
dashboard, and the values that "Reset to default" returns to. An installer who images many devices
can preset them there:

```bash
STORE_NAME="IGA Pearcedale"          # default for Settings → "Store" → "Store name"
SITE_TIMEZONE=Australia/Melbourne    # default for Settings → "Store" → "Time zone"
sudo systemctl restart edge-cctv     # needed after editing .env
```

### 1.3 Opening the dashboard

| From | Address | Notes |
|---|---|---|
| Store network | `http://<device-ip>:8000/dashboard` | If the firewall is on, allow the store network once: `sudo ufw allow from 192.168.x.0/24 to any port 8000`. The installer does not add this rule. |
| Private VPN (Tailscale) | `http://<tailscale-ip>:8000/dashboard` | The installer opens port 8000 on the VPN interface only. |
| Online access | `https://<store>-cctv.ikorex.com.au` | Only after step 2.12. First-run setup is refused on this address. |

The camera setup page is at `/dashboard/studio`. The dashboard opens it for you; see 1.6.

### 1.4 Creating the owner account (first visit only)

The first account must be created on the store network or the VPN, not through online access.

1. Open the dashboard. The "Create the operator account" form appears.
2. Enter the "Setup code" (format `XXXX-XXXX`). It is shown:
   - at the end of the installer output;
   - by `journalctl -u edge-cctv | grep -A1 'FIRST-RUN SETUP CODE'`;
   - in `storage/setup_code.txt` on the device.
3. Optionally fill in "Your name (optional)".
4. Choose a "Username": 3–64 letters, digits, dots, dashes or underscores, starting with a letter or
   digit.
5. Choose a "Password" and repeat it in "Confirm password". It must be at least 8 characters and
   different from the username. The eye button shows what you typed.
6. Press "Create account". The page reloads, signed in.

This account is the owner. To add accounts for managers and staff, see 4.6.

**Roles.** Every account has one of three roles. The server enforces them.

| Role | Can do |
|---|---|
| Owner | Everything, including setup and accounts. Only an owner can create, change, reset or remove another owner. |
| Administrator | Everything, including setup and accounts, except changing an owner account. |
| Operator | Daily use: watch cameras, review, acknowledge and record outcomes of alerts, act on recommendations, take snapshots and clips, reconnect a camera, change their own password. An operator cannot change setup. |

When an operator tries to change setup, the change is refused with "Your account can't change setup.
Ask an administrator." Setup cards such as "Store", "Evidence storage" and "Alerts and detection" are
read-only for operators, the "Accounts" card is hidden from them, and "Backups and reset" and "Till
key" show only a note that an owner or administrator is needed.

### 1.5 Signing in and out

- **Sign in:** "Username", "Password", "Sign in".
- **Lockout:** 10 wrong passwords within 5 minutes from one address blocks that address for
  5 minutes ("Too many failed attempts from this address. Wait 5 minutes and try again.").
- **Session length:** a sign-in lasts 24 hours. After that the sign-in form returns.
- **Sign out:** "Sign out" in the header. This also ends the session on the device.
- **Change your password:** Settings → "Your account" → "Change password" (see 4.6).
- **Forgot password?** Ask an owner or administrator to use "Reset password" for your account
  (Settings → "Accounts"). If nobody can sign in, the "Forgot password?" link on the sign-in form
  explains the reset command on the device. There is no e-mail recovery. See 4.6.

### 1.6 Finding your way around

**Header:** the store name and a camera summary (for example "3 of 4 cameras working · 1 turned
off"), then the "⚙️ Settings" gear, the Light / Dark / System theme switch, and "Sign out".

**Main tabs**

| Tab | Use it for |
|---|---|
| "🏠 Today" | The day at a glance, what needs review, and the setup progress |
| "📹 Cameras" | Live video, camera settings, turning cameras on or off |
| "🗺️ Store map" | Drawing the store, adding, placing and calibrating cameras, live people, heatmaps |
| "📈 Insights" | Recommendations, footfall, heatmap history, the daily report |
| "🚨 Loss prevention" | The review queue, area & line alerts, evidence, patterns, night watch |
| "⚙️ Settings" (header gear) | Accounts, store settings, evidence storage, alert settings, online access, phones, sales data, backups, system health |

On phones and narrow windows the tabs become a bottom bar ("Map", "Alerts"). The Store map is
view-only on a phone until you press "Edit map".

**Camera setup** is a second page used to draw lines and areas on a camera's picture. Open it from a
camera tile → "Camera setup", or from a setup checklist button. When you come from a checklist, use
"← Back to setup checklist" to return.

---

## Part 2. Setting up and mapping the store

Do these steps in order. The "Store setup" card on Today always shows the next step and a button for
it, so you can stop and continue later.

| Step | What | Where |
|---|---|---|
| 2.1 | Name the store and the device; set the time zone | Settings → "Store", "Device" |
| 2.2 | Draw the store plan | Store map |
| 2.3 | Add the cameras | Store map → "Cameras & devices" |
| 2.4 | Name each camera and choose its purpose | Camera "Settings" |
| 2.5 | Upgrade recorder sub-streams to D1 | Settings → "Recorder sub-streams" |
| 2.6 | Place cameras on the plan | Store map |
| 2.7 | Calibrate each camera to the floor | Store map → "Calibrate" |
| 2.8 | Draw lines and areas on the camera pictures | Camera setup |
| 2.9 | Night watch | Camera "Settings" |
| 2.10 | Connect the tills (optional) | Settings → "Sales data" |
| 2.11 | Phone alerts: install the web app, choose who gets alerts | Settings → "Phone alerts" |
| 2.12 | Online access and remote live video (optional) | Settings → "Online access" |
| 2.13 | Check that everything is complete | Today → "Store setup" |

### 2.1 Name the store and the device; set the time zone

There are two names. They do different jobs:

| Name | Where | Used for |
|---|---|---|
| "Store name" | Settings → "Store" | The page header, the browser tab title and the daily report. Seen by everyone using the dashboard. |
| "Device name" | Settings → "Device" | The name of this edge device as paired phones see it, for example when choosing which store to open. |

Usually both are the store's name, for example "IGA Pearcedale". If a store has more than one device,
keep the same "Store name" and give each device its own "Device name".

1. Settings → "Store". Enter the "Store name".
2. In "Time zone", type to search and pick the store's zone, for example `Australia/Melbourne`. The
   line below shows "Store time now: …" so you can check it. Leave it empty to use the device's own
   time zone. "Today", hourly charts, schedules, alert times and the nightly backup use this zone.
3. Press "Save". The message "Saved. Applies now." confirms it; no restart is needed.
4. Settings → "Device". Enter the "Device name" and press "Rename". "Copy" copies the "Device ID",
   which never changes.

Each setting shows its default (from the device's `.env`, see 1.2). After a change it shows "Changed
from the default (…)" and a "Reset to default" button.

### 2.2 Draw the store plan

The plan is drawn in metres; there is no option to upload a floor-plan image. A new store shows a
"Set up your store" guide.

1. **Set the size.** Store map → "Store size". Enter "Floor name", "Width (m)" and "Depth (m)", then
   press "Apply". Shapes outside a smaller size are moved inside it.
2. **Draw the building** with the toolbar:

   | Tool | Key | Draws |
   |---|---|---|
   | "Select" | V | Selects and moves shapes |
   | "Room" | R | The outline of a room |
   | "Wall" | W | A wall; hold Shift for straight lines |
   | "Shelf" | S | A shelf or gondola |
   | "Door" | D | A doorway |
   | "Store area" | Z | A measured floor area (see step 3) |

   Click to place each corner. To finish, press Enter, double-click, right-click or press "Finish".
   Backspace or "Undo point" removes the last corner; Escape or "Cancel" abandons the shape.
   "Snap 0.5 m" keeps corners on a half-metre grid, and "+", "−" and "Fit" zoom.
3. **Draw store areas.** Store areas are where visits, unique shoppers, dwell time and occupancy are
   measured, and where the floor heatmap is summarised. Draw one per aisle, department, entrance,
   checkout and so on. Then select each area and set, in the inspector:
   - "Zone name", for example "Aisle 3 – Confectionery";
   - "Category": ENTRANCE, EXIT, AISLE, DEPARTMENT, SHELF, CHECKOUT, STOCKROOM or EXCLUDED;
   - "Colour";
   - "Products sold here", comma-separated, for example "Chips, Lollies, Chocolate". Insights and the
     AI summary name these products when they report on the area, so a finding says which shelves
     are meant.

   Areas must not overlap: a person standing where two areas overlap is counted in the first one only.
   Shelf names drawn with "Shelf" turn along long, narrow shelves and are hidden when they do not fit
   at the current zoom; zoom in ("+") or select the shelf to read them.

   The categories matter. ENTRANCE, EXIT and CHECKOUT areas drive the "Exit without passing checkout"
   check. EXCLUDED areas are never counted.
4. **Edit shapes.** Each shape is saved as soon as you finish it, with a placeholder name such as
   "Zone 4". Drag a shape to move it, drag a corner to reshape it, and double-click an edge to add a
   corner. Del removes the selected corner or shape; deleting a shape asks for confirmation
   ("Yes, delete" / "Keep"), and recorded visits are kept. A selected structure also has "Kind",
   which includes Counter and Obstacle (these have no toolbar button), "Thickness (m)" and
   "Height (m, optional)".

### 2.3 Add the cameras

Store map → "Cameras & devices" (also reached from Settings → "Cameras & recorders" or
"+ Add cameras" on the Cameras tab). There are three ways to add cameras.

#### A. From a Dahua recorder (recommended)

1. Press "Dahua recorder".
2. Fill in "Recorder IP / host" (for example `192.168.20.160`), "RTSP port" (554), "Username" and
   "Password".
3. Choose "Channels to scan" (4, 8, 16, 32 or 64) and "Stream": keep "Sub-stream (recommended for
   AI)".
4. Press "Save sign-in", so the recorder upgrade in step 2.5 can use the login. The password field
   then shows "✓ Saved on disk".
5. Press "Scan recorder channels". The result says how many live feeds were found, or "🔒
   Authentication failed (401)" for a wrong login, or that the recorder is unreachable.
6. Each channel is listed as "Channel N", followed by the name set on the recorder when the recorder
   reports one. Channels with a picture show "● LIVE" with the stream size and frame rate the
   recorder reported ("—" where it reported none) and are ticked; channels without one show "○ No
   Signal".
7. For each ticked channel, check the two fields under it:
   - the camera name, pre-filled with the recorder's channel name. Change it to something staff
     recognise. Left empty, the camera is named "Recorder channel N".
   - the purpose ("No purpose yet" or one of the purposes in 2.4). Choosing it here switches the
     right analysis on and creates the camera's setup checklist, the same as "Use this purpose".

   If the names could not be read, a note says "Channel names were not read from the recorder (…).
   Type a name for each camera." The names are read from the recorder's web port, which is 80 and
   cannot be changed in this panel.
8. Untick any channel you don't want, then press "Add selected channels".

Added channels are placed in a row on the plan; step 2.6 puts them in the right place. A channel added
without a purpose shows "No purpose set" until you choose one in 2.4.

#### B. One camera by address

1. Press "+ Add camera".
2. Enter a "Camera name", for example "Aisle 3 north".
3. Choose the "Source type": "RTSP stream", "HTTP MJPEG stream", "ONVIF device (address)",
   "USB camera (index or /dev path)" or "Local video file (testing)".
4. Enter the "Stream URL". Put the login in "Username (optional)" and "Password (optional)", not in
   the URL. Dahua channel addresses look like
   `rtsp://<recorder>:554/cam/realmonitor?channel=N&subtype=1`; Hikvision ones like
   `rtsp://<recorder>:554/Streaming/Channels/N02` (sub-stream).
5. Optionally fill in "Department label (optional)" (for example LIQUOR, used to group alerts in
   reports), the purpose under "What does this camera look at?" (see 2.4), and "Location
   (optional)".
6. Press "Test connection". The frame the device received appears, with its size and frame rate and
   a mounting tip for the chosen purpose. If you change a field afterwards, test again.
7. Press "Save camera".

#### C. Scan the network

Press "Scan network". The device looks for USB, ONVIF, mDNS and RTSP devices. Results appear under
"Found on the network, not added yet". Press "Add" on a result, fill in the name, purpose, login,
"Channel" and "Stream" ("Sub (recommended for analytics)"), then "Add camera".

#### The same camera added twice

If a camera is already configured, for example as a recorder channel, adding it again stops with "⚠
Same camera already added". Press "Use existing camera". Use "Add anyway" only for a genuine second
stream; that copy is then left out of store totals. In the recorder panel the choice is "Leave
those channels out". The same check runs when you change a camera's stream address in its
"⚙️ Settings": "Use existing camera" cancels the address change and saves the other changes, and
"Save anyway" saves the duplicate.

If duplicates already exist, a banner on Cameras and on Store map says which one counts in store
totals:

- "Keep A, turn off B" keeps the second entry but stops it.
- "Keep A, remove B" first shows what would be deleted with it (lines, areas, masks, shelves,
  calibration, placement) and what is kept, then "Remove B".
- "Make B primary" swaps which entry counts.
- "They're different cameras" stops the warning. This cannot be undone from the dashboard.

#### Camera list buttons

Each camera in "Cameras & devices" has "Turn on" / "Turn off", "Reconnect", "Config" (the camera
settings), "Checklist", "Calibrate", "Place" and "Remove". "Remove" asks first; recorded
observations are kept.

### 2.4 Name each camera and choose its purpose

Open the camera settings: Cameras → tile → "⚙️ Settings", or "Config" in the camera list.

1. **Name.** If the "Camera name" is still "Recorder channel N" (or a recorder name staff don't
   recognise), change it, and fill in "Location description". "Channel number" is only the camera's
   order in the dashboard, not the recorder channel; the recorder channel is in the stream address.
2. **Purpose.** If you did not choose one when adding the channel, under "Camera purpose" choose
   what the camera looks at and press "Use this purpose". This switches the right analysis on and
   creates the camera's setup checklist. Tick
   "Keep my current analysis switches" first if you have already set the switches by hand.

   | Purpose | Analysis switched on | Required setup step | Effect |
   |---|---|---|---|
   | Entrance | Counting | Counting line across the entrance | Main source of visitor counts |
   | Exit | Counting, theft | Counting line across the exit | Counts out; feeds "Exit without passing checkout" |
   | Entrance / exit door | Counting, theft | Counting line | In and out on one line |
   | Checkout / cashier | Counting, shelf, theft | Checkout lane area | More sensitive theft checks; optional queue area and till link |
   | Product aisle / shelves | Counting, shelf, theft | Product shelf areas | Shelf reaches and theft checks |
   | High-value area | Counting, shelf, theft | Product shelf areas | Most sensitive; alerts at least High |
   | Stockroom / staff only | Counting | Restricted area with a schedule | Never counted as customer footfall; left out of heatmap history |
   | General overview | Counting | Calibrate to the floor plan | Presence and dwell heatmaps |

   For a checkout camera, enter the till ID under "Till / register for this lane" (for example
   `REG-1`) and press "Link register", so its sales are matched to its shoppers.
3. **Analysis switches.** "AI features on this camera":
   - "People counting & dwell": visits, dwell time, footfall and heatmaps.
   - "Shelf interaction": hand reaches into product shelves.
   - "Theft detection": suspicious-behaviour checks.
   - "Theft evidence clip": also saves a clip from about 5 s before to 10 s after each theft alert.
     Needs theft detection. Clips count towards the evidence storage limit.
   - "Ignore figures that never move" (on by default): drops mannequins, posters and cut-outs that
     look like people. A figure with no body or limb movement for "Treat a figure as static after
     (seconds without movement)" (default 60) is shown as "static" and is not counted or analysed.
     A figure that never moved creates no visits, footfall or heatmap points, even before it is
     marked static. The device remembers each static figure, also across restarts. When a figure
     appears again at a remembered spot, or mostly inside an ignore area (2.8), it is not counted
     while it is checked for about 5 s, then it is static again unless it moved. Shoppers walking
     past in front of a static figure, and short gaps in its detection, do not restart its
     60 s clock. Anyone who really moves is counted as a person at once.
   - "Largest person size (% of the frame)": rejects oversized false detections. Raise it for
     cameras mounted close to shoppers; the default is 35 %.
4. **Stream quality.** "Stream quality (analysis)": keep "Auto". It picks the smallest sub-stream of
   at least D1. "Check sub-streams again" measures again after you change the recorder.
5. Press "💾 Save".

The dialog saves the camera, its login, its switches and its placement as separate steps. If a
message says one part failed (for example "Camera saved, but feature toggles failed…"), correct it
and save again. The purpose is saved separately, by "Use this purpose".

### 2.5 Upgrade recorder sub-streams to D1 (Dahua)

Many recorders ship with CIF (352x288) sub-streams, which are too small for reliable detection.

1. Make sure the recorder login is saved (step 2.3 A, "Save sign-in").
2. Settings → "Recorder sub-streams". Choose the "Recorder" and, if needed, its "Recorder web port".
3. Press "Check sub-streams". This only reads the recorder and lists what would change.
4. Press "Upgrade CIF sub-streams to D1" and confirm ("Yes, upgrade N"). Channels are changed one at
   a time. Each channel's settings are saved first and put back if the recorder or camera does not
   deliver D1. The result is shown per channel, for example "switched to D1" or "kept CIF: D1 not
   supported by this camera".
5. Optionally, under "Sub-stream bit rate", set the "Target bit rate (kbps)" (default 768) and press
   "Raise to N kbps".

"Restore previous stream settings" or a channel's "Restore" undoes the change. Every change is
logged with who made it and when.

### 2.6 Place cameras on the plan

Store map. For each camera:

1. Press "Place" in "Cameras & devices", then click the plan where the camera is mounted
   ("Place by click"). You can also drag the camera marker.
2. Drag the tip of the wedge to point it, or enter "Bearing (°)" and "FOV (°)" (field of view) in
   the inspector, with "X (metres)", "Y (metres)" and "Height (m)" (mounting height).

The same values are under "Placement on the blueprint" in the camera settings. Placement alone does
not put people on the map; that needs calibration.

**Before calibrating: "Counts into zone".** Select an uncalibrated camera on the map and pick, under
"Counts into zone", the store area its picture shows (for example the aisle it looks down). From then
on every person that camera sees is counted as a visit to that area, so the area's visits, dwell
time and occupancy, and the Insights rules, work before the camera is calibrated. It does not place
people on the map or the floor heatmap. Pick "— none —" to remove the link. Once the camera is
calibrated, the link is no longer used: each person is counted in the area they stand in. If two
linked cameras see the same aisle, a shopper seen by both is counted by each.

### 2.7 Calibrate each camera to the floor

Calibration tells the system which spot on the plan each point of the floor in the camera picture
is. Without it a camera counts people but cannot place them on the plan, so the map, heatmaps and
store areas stay empty for that camera.

1. Press "Calibrate" (camera list or inspector, or "Calibrate on the map" in the checklist).
2. "1 · Click a floor spot in the camera picture." Pick a mark that is on the floor and also on the
   plan: a tile corner, a shelf foot, a door threshold.
3. "2 · Click the same spot on the map." Escape cancels the current point.
4. Repeat for **at least 4 points**, spread across the whole picture, near and far. More points give
   better accuracy.
5. Press "Save calibration". It is greyed out until 4 points exist, and while the camera has no
   picture.
6. Read the accuracy test that runs automatically: "Accuracy about X cm on average", rated good
   (up to 25 cm), fair (up to 50 cm) or poor. Each point gets a coloured tag, and orange crosses
   show where each point lands. If the worst point is more than 1 m off, remove it with its "×",
   or add more spread-out points, and save again.

"Test accuracy" re-runs the test. "Clear calibration" removes it. Saved points come back when you
reopen the tool. Through online access the camera picture is a single still; press "Take a new
picture" to refresh it.

### 2.8 Draw lines and areas on the camera pictures (Camera setup)

Open Camera setup from the camera tile, or with the checklist's "Draw counting line", "Draw shelf
area", "Draw restricted area" and similar buttons. Choose the camera from the "📷 Camera:" buttons
and type a name in "Label for the zone you are drawing".

Pick a tool, then click points on the video. "Undo" removes the last point and "✕ Cancel" starts
over. Shapes can't be dragged after saving: to change a tripwire, restricted area or checkout area
press "✎ Edit" and click new points; to change a privacy mask or product shelf, delete it and draw
it again. An ignore area's "✎ Edit" changes its name and coverage only; for a new shape, delete it
and draw it again. Changes take effect without a restart.

The picture also shows the AI's live boxes: a solid green box is a moving person, a dashed amber box
marked "confirming" is a new detection still being checked (first seconds), and a dotted grey box
marked "static" is a figure that never moves and is already left out of counts. Click a box to see
what it is and, if it is not a person, to ignore it (see "A poster or mannequin is counted as a
person" below).

#### 🚪 Tripwire: counting lines (2 points)

Use one across every entrance to get exact visitor counts.

1. Click the start and end of the line. A third click starts the line again.
2. Check the arrow. "Side that is "in"" shows which side counts as inside; press "⇅ Flip" if it is
   wrong.
3. Tick "Counts store footfall (entrance line)" for store entrances.
4. Optionally tick "Alert staff on crossing" and choose "Alert when crossing": In, Out or Either way,
   with an "Alert severity". These alerts appear in Loss prevention → "Area & line alerts" (3.5) and
   on paired phones.
5. Press "💾 Save tripwire".

Very short lines (under 2 % of the picture) are refused.

#### ⛔ Restricted area (3+ points)

Alerts when someone is inside during set hours. Use it for the stockroom, cash office, staff-only
doors, or the shop floor after closing.

1. Click the corners of the area.
2. "Schedule": "Restricted during these times", or "Allowed during these times (restricted
   otherwise)".
3. "＋ Add time window" for each window. Tick the days and set "From" and "to" (default 22:00 to
   07:00). With no windows the area is restricted at all times.
4. "Minimum dwell (seconds)": how long someone must be inside before an alert (default 3).
5. "Alert severity" (default High) and "Time zone". Leave "Time zone" empty to use the store's time
   zone (Settings → "Store"); fill it in only for an area that must follow a different zone.
6. Save.

Restricted-area alerts appear in Loss prevention → "Area & line alerts" (3.5), in the alert banner
and on paired phones. How soon the same person in the same area can alert again is set in Settings →
"Alerts and detection" (4.2).

#### 🧾 Checkout / queue area (3+ points)

Times how long people stand at a till or wait in line. No calibration needed. Enter a "Name" (for
example "Till 1") and choose the "Kind": "Checkout lane (time at the till)" or "Queue line (waiting
time)". "Turn off" / "Turn on" in the list pauses an area.

#### 🛒 Product shelf (3+ points)

Records hand reaches into a shelf and links them to a product. Click at least 3 corners around the
shelf face, then press "💾 Save" and fill in "Map product shelf area":

- "Product / display name", "SKU identifier" and "Product category (e.g. LIQUOR)", all required.
  For the category, type one word for what is on the shelf; the list suggests words already used and
  the high-value ones. Categories listed under "High-value categories" (Settings → "Alerts and
  detection") mark the shelf as high value for theft cues.
- "Price ($)" and "Facing count".
- "Shelf level" (Auto, Top, Middle, Bottom), "Value tier" (Auto, Low, Standard, Premium) and
  "Placement" (Regular shelf or Promo endcap).

"✏️" edits the product details without redrawing. Under "Study metrics" only "Hand reaches" is
measured. The other options ("Dwell inspection", "Put-back / friction", "Correlate with POS", "A/B
placement test") are greyed out and marked "Not measured yet".

#### 🌫️ Privacy mask (3+ points)

Hides part of the picture for privacy. "Mask mode": Blur, Mosaic (pixelate), Blackout or Solid
colour (with "Colour"). People inside a privacy mask are still counted. To stop the AI seeing a
poster, mannequin or screen, use "🚫 Ignore area (no AI)" instead (below). Masks are applied to every
picture shown and saved, including evidence and direct live video through online access (2.12).

Adding, changing or deleting a mask briefly interrupts every remote (direct) live video, on all
cameras, for a few seconds while the device restarts its video service; the tiles reconnect by
themselves with the new masks.

**Deleting:** the 🗑️ button on a mask or product shelf asks "Delete?" first; press "Delete" to
remove it or "Keep" to cancel. "Clear all" in the "🌫️ Privacy masks" list (every privacy mask on
every camera) also asks: "Yes, clear" or "Keep". It leaves ignore areas alone.

#### 🚫 Ignore area (no AI) (3+ points)

The AI does not look for people inside an ignore area. Use it for posters, mannequins, TV screens or
a mirror. The video is not changed, and adding or changing an ignore area does not interrupt remote
live video.

1. Press "🚫 Ignore area (no AI)" and click at least three points around the poster, mannequin or
   screen.
2. Under "New ignore area" give it a "Name" (for example "Poster by the door").
3. Choose "How much of a person must be inside":
   - "Most of them (60%)" (default): right for most posters and mannequins.
   - "Half (50%)".
   - "Any part touching (10%)": for a screen or mirror right next to where shoppers walk; it also
     drops real people who brush past it, so keep the area tight.
4. Press "💾 Save ignore area".

A detection is ignored when its feet are inside the area, or when at least the chosen share of its
box is inside. Everything else on the camera is analysed as before.

The "🚫 Ignore areas" list (side panel) shows each area of this camera:

- "Person inside": change the coverage straight from the list.
- "Enabled": untick to switch an area off without deleting it ("Turned off: people here are
  detected again."); tick to turn it on again.
- "✎ Edit": change the name or coverage. The shape can't be moved; delete it and draw a new one.
- 🗑️: asks "Delete?" first ("Delete" / "Keep").
- "Clear all" (every ignore area on every camera) asks "Delete every ignore area on every camera?":
  "Yes, clear" or "Keep". It leaves privacy masks alone.

Masks made earlier with the old "Ignore for analysis" mask mode appear in this list as ignore areas.

#### A poster or mannequin is counted as a person

Fastest way, from the live picture in Camera setup:

1. Click the box around the poster or mannequin. The pop-up says what the AI thinks it is (for
   example "Detected: moving person · 82% sure").
2. Press "Not a person — ignore this spot". An ignore area is made around the box (slightly larger
   than it), named "Not a person" with the time, and "Ignore area added. People are no longer
   detected there." appears.
3. Made a mistake? Press "Undo" in the same pop-up. Otherwise press "Close". You can rename the area
   or change its coverage later in the "🚫 Ignore areas" list.

Other ways:

- Draw an ignore area yourself with "🚫 Ignore area (no AI)" (above), for example to cover a whole
  screen or a row of mannequins.
- Leave "Ignore figures that never move" on for the camera (2.4, on by default). A figure that has
  not moved for 60 s is then shown as "static" and not counted, and the device remembers it.

#### Also on this page

- "📸 Save snapshot" saves a still to evidence storage.
- "🎥 Export incident clip" saves a short clip from the camera's recent buffer.
- "📊 Pipeline telemetry" shows frame rate, detections, tracks and calibration state, and how the
  false-detection filters are working:
  - "Waiting to confirm (first seconds)": new detections not counted yet.
  - "Static figures ignored": figures in view now that never move.
  - "Remembered static figures": spots the device remembers as posters or mannequins.
  - "Detections dropped by ignore areas": detections dropped in the last analysed picture.
- On the store network the analysed picture marks static figures "static", figures still being checked "pending" or
  "pending static", and ignore areas "ignored". Ignore areas are also outlined on the drawing layer
  as "🚫 <name> · no AI" ("(off)" when switched off).

### 2.9 Night watch

Per camera, in the camera settings → "Night watch". While watching, normal analysis pauses and the
camera looks for movement; a person seen is a high alert.

1. Tick "Watch this camera at night".
2. Set "Start" and "End" in store time. An end at or before the start means the next morning.
3. Tick the "Nights starting on" days.
4. Optionally tick "Also watch whenever this camera is in IR / low light".
5. Choose "Motion sensitivity" (Low, Medium, High) and "Quiet time after an alert (minutes)".
6. Press "Save night watch". It applies from the next frame.

Night events appear in Loss prevention → "Night watch". To also save a short clip with each night
event, tick "Save a clip with each night-watch event" in Settings → "Evidence storage" (4.2).

### 2.10 Connect the tills (optional)

Settings → "Sales data (point of sale)". Without till data, "Sales today", buying rate, "BOUGHT" in
the shopper journey and "Lost sales" show "—" or "needs sales data". "How to connect your tills"
shows the address, method and an example message for the till system to send each sale.

The till system needs a **till key**. It only lets the till send sales; it opens nothing else.
Owners and administrators manage it under "Till key" in the same card:

1. Press "Create till key". The key appears once, with "Shown once. Store it in the till system now."
2. Press "Copy" (on the store network the browser may block copying; the key is then selected, press
   Ctrl+C), put it into the till system, then press "Done". The key cannot be shown again.
3. The till sends it in the `X-Edge-API-Key` header with each sale.

The card then shows "A till key is set (ends in …)", when it was created and when it was last used.
"Replace till key" issues a new key ("Yes, replace" / "Keep"); tills using the old key stop sending
sales until they are updated. "Revoke" ("Yes, revoke" / "Keep") stops the key at once.

Older till set-ups that send the device's internal service key still work, but give new tills a till
key instead.

### 2.11 Phone alerts (installed web app)

Phones get theft alerts from the dashboard itself, installed as an app. No app store and no
Firebase account are needed. It works only on the store's **public address**
(`https://<store>-cctv.ikorex.com.au`), not on a private `http://` address.

**Install the app and turn on alerts (each person, on their own phone)**

- **Android (Chrome):** open the public address and sign in. Tap "📲 Install app" at the top, or
  Chrome menu ⋮ → "Install app". Open the app, then tap "Enable alerts" and choose "Allow".
- **iPhone / iPad (iOS 16.4 or later, Safari):** open the public address in Safari. Tap Share →
  "Add to Home Screen" → "Add". Open the new icon on the Home Screen and sign in, then tap
  "Enable alerts" and choose "Allow". On iPhone, alerts only work in the Home Screen app.
- "Send a test alert" under Settings → "Phone alerts" → "This phone" checks the whole path.

**Choose who gets alerts (owner or administrator).** Settings → "Phone alerts" → "Who gets alerts":

- Set each person to "First priority", "Backup" or "No alerts". You need at least **2 first-priority**
  people before you can save. Until then, alerts go to every owner and administrator, with no
  escalation.
- An alert goes to every phone of the first-priority people. If nobody acknowledges it within
  "Escalate to backup after" (default 5 min), the backup people get it as well, titled
  "Not acknowledged". If no first-priority phone can be reached at all, it goes to backup at once.
- "Alert phones from confidence" (default 75 %): only incidents at or above this level wake phones.
  Lower ones are still recorded and shown on the Loss prevention tab.
- "Night watch intrusions" and "Area & line alerts" can be switched on or off for phones.
- "Alert if the store's CCTV goes offline" (default on, after 15 min): the VPS sends this when
  the box stops calling in (power cut, internet down). The box sends "back online" when it returns.
  It needs Online access (2.12).

**Acknowledge** an alert with the notification's "Acknowledge" button (Android) or in the app
(Loss prevention → the incident → "Acknowledge"). Everyone who got it then sees
"Acknowledged: …", and escalation stops.

"Connected phones" lists every phone with alerts on, with "Test" and "Remove". An operator sees and
removes only their own. Removing an account stops its phones' alerts.

### 2.11a Native phone app (backup)

The Flutter phone app is kept as a backup. It uses pairing and Firebase:

**Pair a phone**

1. Settings → "Pair a phone". A QR code and a "Pairing code" appear, valid for 10 minutes.
2. In the phone app, scan the QR code or enter the code. The page shows "Paired: <name>".
3. "No camera on the phone? Pair manually" explains entering the code by hand.

**Turn on push notifications** (needed for phone alerts)

1. Create a Firebase project and download a service-account key (`.json`).
2. Settings → "Push notifications" → "Service-account key (.json from Firebase)" → "Upload".
3. Choose a phone and press "Send test alert". Check "Recent deliveries".

**Choose what each phone receives.** Settings → "Paired phones" → "Alert settings": alert types,
"Minimum severity", cameras, and "Quiet hours (no pushes)". "Rename" and "Revoke" are on the same
row.

The phone app currently shows live video only on the store network. Away from the store it cannot
play live video until the app is updated.

Alerts reach phones and the dashboard independently: restricted-area and line-crossing alerts are
also listed in Loss prevention → "Area & line alerts", so they are seen even without push. How often
the same alert can repeat is set in Settings → "Alerts and detection" (4.2). Quiet hours use store
time.

### 2.12 Online access and remote live video (optional)

Online access publishes the dashboard at `https://<store>-cctv.ikorex.com.au` without opening a port
on the store router. The iKorex server only passes pages, data and the video connection set-up. Live
video goes straight from the device to the viewer's browser, and only while someone is watching.

**Installer, once per store:** on the iKorex server run `onboard-store.sh <store>` (it prints the
store token once) and add the Cloudflare DNS records. Install the device with
`sudo EDGE_TUNNEL=1 bash deploy/install.sh`. Details: `docs/DEPLOYMENT.md` §7a.

**In the dashboard** (on the store network):

1. Settings → "Online access". Fill in "Public address", "Tunnel server"
   (`wss://tunnel.ikorex.com.au`), "Store ID", "Store token", optionally "Server key (optional)", and
   set "Proxies in front of your server" to One (Cloudflare).
2. Tick "Enable online access" and press "Save". The status goes from Stopped to "Connecting…" to
   "Connected since …".
3. Press "Verify now". The badge should read "Verified".
4. Settings → "Online access · Live video": keep the default "STUN servers"
   (`stun:stun.ikorex.com.au:3478`) and "Connection mode" Automatic, and set "Most live videos at
   once" (default 8).
5. From outside the store, open the public address and press "Check remote video". It says in plain
   words whether direct video works and what to change.

"Replace token" changes the store token; "Remove" deletes it.

> **Privacy note:** privacy masks **are** burned into direct live video on the device, before the
> video leaves it, and into the stills that remote views take over a direct connection (floor-map
> thumbnail, calibration picture). A camera with privacy masks is always sent this way; if the
> device cannot build the masked picture, the tile shows "Hidden: privacy masks" instead of the
> camera's own picture. Masking costs about 6 to 9 % of one processor core per masked tile with
> hardware video encoding. Changing a mask briefly restarts every remote viewer (see 2.8).
> Detector boxes are still not drawn on direct video.

### 2.13 Check that everything is complete

- Today → "Store setup" shows a percentage ("N% DONE") and the "Next step". Each camera shows "No
  purpose set", "n/m done" or "Ready".
- Store map → "Cameras & devices" → "Checklist" lists what is missing for that camera's purpose,
  with a button to each tool. It reads "✓ Ready" when the required steps are done. Optional items,
  such as "Ignore areas (posters, mannequins, screens)" (button "Draw ignore area", or "Edit ignore
  area" once one exists) and "Privacy masks", do not hold "Ready" back.
- The per-analysis rows show Working, Partly, Needs setup or Not possible.

---

## Part 3. Daily use

### 3.1 Today

Refreshes every 15 seconds.

- **Banner:** appears only when something blocks the system, for example no cameras, no camera
  sending pictures, or no camera placed on the map. It has a button to the fix.
- **Figures:** "Cameras working", "In the store now", "Visitors today", "Busiest hour so far",
  "Average visit", "Sales today". "—" means not measured yet; hover for the reason.
- **"Visitors by hour":** today against yesterday and the same day last week. Hatched hours were not
  recorded.
- **"Top recommendations"** and **"Needs review"**: the three most important open recommendations
  and up to five incidents waiting for review.
- **"Store setup":** see 2.13.

### 3.2 Cameras: live view

- The grid shows **four tiles**. Click a tile to enlarge it with smoother video; click again or press
  Esc to shrink it.
- **More than four cameras:** tiles rotate. Use "‹ Previous", "Pause" / "Resume", "Next ›" and
  "Change every [10] s" (3 to 600). "Pin" keeps a camera in its tile (saved in this browser).
- **"Area:"** filters by camera purpose; **"🔍 Search cameras..."** finds a camera by name or
  location.
- **"Video smoothness:"** Low, Normal or High sets how often store-network tiles refresh (every 5, 2
  or 1 s).
- **Tile badges:** "● WORKING", "WRONG PASSWORD", "OFF" or "OFFLINE"; the live badge shows "● LIVE"
  or the picture's age; the resolution badge shows the stream size (for example "Sub 704x576").
- **Tile status line:** people in view now, for example "3 people in view · 1 static ignored ·
  on the map". "· N static ignored" appears when the camera sees figures that never move (posters,
  mannequins); they are not counted (2.4).
- **Tile buttons:** "⚙️ Settings", "Reconnect" (offline cameras only), "Turn off", "Camera setup".
  "Turn off" stops the camera's video, analysis and network traffic until it is turned on again.
- **Under the grid:** "Turned off (n)" with "Turn on" per camera, "Turn all N on", and "Turn off N
  offline cameras" (asks first).

**Through online access** each visible tile plays direct live video ("● LIVE · Direct"). Opening a
camera there takes about 1.5 s for H.264 cameras and about 4 s for H.265 ones. If a tile says
"Direct video not possible from this network", press "Check connection".

### 3.3 Store map

- **"Show:"** turns layers on or off: walls and shelves, areas, cameras, people, labels, grid.
- **People:** live positions from calibrated cameras.
- **Heatmap:** "Walked", "Stopped" or "Touched shelves", for "Today", "Yesterday", "Last 7 days" or
  "Last 30 days".

### 3.4 Insights

**"Recommendations"**

- The analysis runs every hour. "Run analysis now" runs it at once; "Add AI summary (slower)" adds a
  short written summary from the local AI model.
- Each card has a priority, a "Why:" and a "Do:" line, and its evidence. Record what you did with
  "Mark reviewed", "Done" or "Dismiss" ("Reopen" brings it back).
- Filters: "Open", "All", "Show dismissed".

**"Shoppers & footfall"**

- "Visitors by hour"; "Shopper journey" (stages from visit to purchase; "Bought" needs till data).
- "Entrances today": in, out and an estimate of people inside, per counting line.
- "Areas where shoppers leave without engaging": needs store areas, calibration and product shelves.
  The "Lost sales" column shows "needs sales data" without till data. With till data it shows "—";
  hover for the reason: till receipts are not linked to store areas or shoppers, so lost sales are
  not estimated.
- "Product shelf reaches today".
- "Shopper profile" always says no demographic classifier is enabled. This is deliberate.
- **"Heatmap history":** choose "Where", the type ("Walked", "Stopped", "Touched shelves") and
  period (Hour, Day, Week), then the date and hour. "Play day" animates a day. "Compare with…"
  compares two periods (for example this week against last week, or "Custom…"). "Record now" saves
  the current hour.

**"Daily report"**

Today's summary. "Refresh" updates it, "Print / save PDF" prints it or saves it as a PDF. It always
shows today; there is no date choice.

### 3.5 Loss prevention: reviewing alerts

**When an alert comes in,** a red banner appears on every page with a short tone: "Please check:
<check> on <camera>", the confidence and the time. Press "Review now", or "Dismiss" to hide it until
the next new incident. Alerts appear within about 5 seconds. The banner also shows the newest
restricted-area or line alert that nobody has acknowledged yet (for example "Please check: someone in
Stockroom on <camera>"), and the Loss prevention tab badge counts both incidents waiting for review
and area or line alerts not acknowledged.

An alert is **suspicious behaviour for a person to check, not a finding of theft.** The checks are:

- "Possible concealment": a hand moves from a shelf to a pocket, chest or behind the back and stays.
- "Possible shelf sweeping".
- "Loitering at high-value products".
- "Exit without passing checkout".
- A combined behaviour pattern, raised when several weaker cues of different types add up.

**Reviewing an incident** in the "Review queue" (filters "Needs review", "Handled", "All"):

1. Look at the evidence image and "What the camera saw". Click the image for the full-size viewer
   ("Image" / "Clip", "Open in new tab" to save it, Esc to close). "▶ Play clip" plays the clip if
   one was saved.
2. Press "I'm checking" so colleagues know someone has it.
3. If someone goes to the floor, press "Mark: staff sent", optionally fill in "Who went (optional)"
   and "Note (optional)", and tick "Alert paired phones" to notify phones.
4. "Watch camera now" opens the camera in a new tab.
5. When it is over, press "Record outcome" and choose what happened: "Theft confirmed, goods
   recovered", "Theft confirmed, goods not recovered", "Customer paid", "Reported to police", "Person
   left before staff arrived", "Customer was fine, no action" or "False alarm". Add notes and, where
   offered, the value recovered. Press "Save outcome". "False alarm" is also a one-click button.

Always record an outcome. The figures "Value in confirmed incidents" and "False-alarm rate", and
the "False alarms by rule" table, are built only from recorded outcomes.

**"Area & line alerts"** lists restricted-area and line-crossing alerts: someone inside a restricted
area during its hours, or crossing a line set to "Alert staff on crossing" (2.8). Like the review
queue, these are prompts to check, not theft findings. Times are store time.

1. Choose a filter: "Needs attention" (not acknowledged yet, the default), "Acknowledged" or "All".
2. Each alert shows what happened (for example "Someone in Stockroom while it is restricted" or
   "Crossed Back door line (in)"), the camera, the severity set in Camera setup, how long the person
   was inside before the alert (areas) or the direction (lines), and the time.
3. Click the evidence image to open it in the full-size viewer. "Evidence expired" means the storage
   limit has already deleted it.
4. "Watch camera now" opens the camera in Camera setup in a new tab.
5. When someone has looked into it, press "Acknowledge". The alert moves to "Acknowledged" with who
   acknowledged it and when, and leaves the banner and the badge. Operators may acknowledge.

Only the newest 200 alerts of a filter are listed; a note says how many older ones are not shown.
"Refresh" reloads the list, which otherwise updates every 5 seconds.

**"Patterns"** (7 days, 4 weeks, 3 months) shows hotspots by camera and area, day and hour, which
checks fired, the weekly trend, and bursts at one place. Use it to plan staff presence and spot
checks that need tuning.

**"Night watch"** shows the store time, each watched camera's state ("Watching", "Movement: checking
for a person", "Person detected", …) and "Recent night events" with image and "Clip".

Evidence is deleted oldest-first when the storage limit or the maximum age is reached; the incident
then shows "Evidence expired". To keep evidence for police or insurance, open it with "Open in new
tab" and save it elsewhere straight away. There is no bulk export.

---

## Part 4. Looking after the system

### 4.1 System health

Settings → "System health" (read-only): video decoding, detection engine and speed, processor,
memory, uptime and evidence storage use ("x of y · n files").

### 4.2 Evidence storage and alert settings

Owners and administrators set these in the dashboard. Every change applies at once, without a
restart. Operators can read the cards but not change them. Each field shows its default (taken from
the device's `.env`), and after a change "Changed from the default (…) by <account>" with a "Reset to
default" button.

**Settings → "Evidence storage"** shows "Evidence now" (used of the limit) and the disk's free
space, then:

| Field | What it does | Default |
|---|---|---|
| "Evidence size limit" | "Automatic (10% of disk)" (at least 1 GB, at most 50 GB) or "Fixed size" in GB | Automatic |
| "Keep evidence for (days)" | Evidence older than this is deleted; 0 means no age limit | 7 days |
| "Never fill the disk beyond (%)" | Evidence is also trimmed so the whole disk stays below this (50–95 %) | 85 % |
| "Night watch evidence limit (MB)" | Night watch stills and clips, inside the evidence size limit; 0 means no separate limit | 1024 MB |
| "Save a clip with each night-watch event" | A short clip with each night-watch person alert | Off |

Lowering a limit deletes the oldest evidence above it straight away, so "Save" first asks "Lower
limit (…): the oldest evidence above it is deleted straight away." with "Yes, save" / "Keep editing".

**Settings → "Alerts and detection"**

| Field | What it does | Default |
|---|---|---|
| "High-value categories" | Product shelf categories that count as high value (comma separated, for example SPIRITS, COSMETICS, BABY_FORMULA). Use the words given in "Product category (e.g. LIQUOR)" (2.8). | ALCOHOL, SPIRITS, LIQUOR, WINE, ELECTRONICS, COSMETICS, BEAUTY, FRAGRANCE, BABY_FORMULA, RAZORS, TOBACCO, MEDICINE, PHARMACY |
| "High value from price" | A product shelf priced at or above this counts as high value, whatever its category; 0 means price is not used | 0 |
| "Line crossing: repeat alert after (seconds)" | The same person on the same alert line does not alert again for this long. A line's own setting overrides it. | 60 s |
| "Restricted area: repeat alert after (seconds)" | The same person in the same restricted area does not alert again for this long. An area's own setting overrides it. | 300 s |
| "Theft: repeat alert after (seconds)" | The same person does not raise the same kind of theft alert again for this long | 120 s |
| "Theft minimum confidence (%)" | A possible theft below this confidence is not raised; lower finds more but brings more false alerts | 30 % |
| "Check for exit without passing checkout" | Turns the "Exit without passing checkout" check on or off | On |
| "Queue congested after (seconds)" | A checkout lane counts as congested when the average wait passes this | 270 s |

Use "False alarms by rule" (3.5) before lowering the theft confidence.

Evidence must stay on the device's own disk. On a network drive nothing is deleted and System health
reports an error.

### 4.3 Service, logs and updates (installer)

```bash
sudo systemctl status edge-cctv          # is it running?
sudo systemctl restart edge-cctv         # restart (needed after editing .env)
journalctl -u edge-cctv -f               # live log (the only log)
# code-only update:
sudo -u edgecctv git -C /opt/edge-cctv pull --ff-only && sudo systemctl restart edge-cctv
# update with new dependencies or service changes:
sudo bash /opt/edge-cctv/deploy/install.sh
```

Database upgrades run by themselves after a backup is taken.

### 4.4 Backups and factory reset

A backup holds the database: the store layout, cameras, settings (including the dashboard settings in
4.2), user accounts and recorded figures. Evidence images and clips are not included.

**Automatic backups**

- one at every start of the service;
- one every night at 03:00 store time.

An automatic backup is skipped when nothing changed since the last one. The device keeps the 7 newest
automatic backups plus the newest one of each day for 14 days, and deletes older automatic ones. It
never deletes manual, uploaded, "Before restore", "Before reset" or "Before upgrade" backups.

**Settings → "Backups and reset"** (owners and administrators). The list shows "When (store time)",
"Kind" ("At start", "Automatic", "Manual", "Uploaded", "Before restore", "Before reset", "Before
upgrade") and "Size".

- "Back up now" takes a manual backup.
- "Download" saves a backup to the computer you are using. **Backups stay on the device unless you
  download them**, so download one regularly (for example weekly, and before any big change) and
  keep it somewhere safe off the device. A backup contains the store's accounts and camera settings.
- "Restore" asks first: a safety backup of the current database is taken, then the layout, cameras,
  settings, accounts and recorded figures go back to that time; anything recorded since is kept only
  in the safety backup. Press "Yes, restore" or "Keep". The service then restarts by itself (about a
  minute). When it is back, press "Reload page"; you may need to sign in again. If the device is not
  run as a service, the message says to restart it by hand.
- "Upload a backup" adds a backup file from your computer (for example one downloaded earlier, or
  from a replacement device). The file is checked first and then listed as "Uploaded"; restoring it
  is a separate step, with "Restore".

**Factory reset** (in the same card). It deletes the store layout (zones, shelves, walls, doors), all
cameras and found devices with their passwords and overlays, and every recorded figure (visits,
tracks, shelf reaches, theft incidents, sales rows, line crossings, queue visits, heatmap history and
recommendations). It keeps user accounts and passwords, the dashboard settings, saved recorder
passwords, paired phones, online access, the device's `.env`, saved evidence and all backups. A backup
is taken first, so a reset can be undone with "Restore".

1. Type `RESET` in "Type RESET to confirm".
2. Press "Factory reset". The result says how many rows were deleted and which backup was taken.

### 4.5 Changing device-level settings

Most settings are in the dashboard. Only device-level settings remain in
`/opt/edge-cctv/edge_backend/.env`; edit the file and restart the service. These are mainly: the
listening address and port, secrets (normally generated by the device), video decoding and the GPU,
the detection model and its thresholds (`PERSON_CONF_THRESHOLD`, `PERSON_MAX_FRAME_FRACTION` …), the
analysis budget, the storage location (`STORAGE_DIR`), night-watch motion tuning, live-video timing
and encoder, and the local AI model address. The full list, with explanations, is in
`edge_backend/.env.example` and in `docs/FEATURES.md` ("Installer-level settings").

`STORE_NAME`, `SITE_TIMEZONE`, the evidence limits and the alert settings of 4.2 are also in `.env`,
but only as defaults: a value saved in the dashboard wins, and "Reset to default" returns to the `.env`
value.

### 4.6 Accounts and passwords

**Your own password.** Settings → "Your account" shows your "Username", name and "Role". Press
"Change password", fill in "Current password", "New password" (at least 8 characters, different from
the current one) and "Confirm new password", then "Save new password". Any other browser signed in to
your account is signed out; this one stays signed in, and paired phones stay paired.

**Other accounts** (owners and administrators). Settings → "Accounts" lists each account's name,
username, role and "Last sign-in".

- **Add an account:** "Add account", then "Username", "Name (optional)", "Role" (Operator for staff),
  "Initial password" and "Confirm password", then "Create account". Give the person the password;
  they change it under "Your account".
- **Change a role:** "Change role", choose the "New role", "Save role".
- **Reset a forgotten password:** "Reset password", type the new password twice, "Save password". That
  person is signed out of every browser and signs in with the new password.
- **Remove an account:** "Remove", then "Yes, remove" or "Keep". The person is signed out at once,
  including phones paired to that account.

You cannot change your own role or remove yourself. Only an owner can create, change, reset or remove
an owner account, and the last owner cannot be removed or demoted ("This is the only owner. Make
another account an owner first.").

**On the device (fallback).** If nobody can sign in, or the only owner forgot the password, use the
command line on the device, in `/opt/edge-cctv/edge_backend`:

```bash
sudo -u edgecctv ../.venv/bin/python scripts/manage_operator.py list
sudo -u edgecctv ../.venv/bin/python scripts/manage_operator.py reset-password --username sam
sudo -u edgecctv ../.venv/bin/python scripts/manage_operator.py create --username sam --role operator
sudo -u edgecctv ../.venv/bin/python scripts/manage_operator.py reset-setup   # removes all accounts, new setup code
```

`reset-password` and `reset-setup` sign out every session and unpair every phone.

### 4.7 Handing the system over to the store

1. Issue a new store token on the iKorex server and press "Replace token" in Settings → "Online
   access". Press "Verify now" again.
2. Create the store's own accounts in Settings → "Accounts": an Owner or Administrator for the
   manager, Operator accounts for staff.
3. Have the store owner sign in and change the password under Settings → "Your account" → "Change
   password". Then remove the installer's accounts under "Accounts".
4. If the tills were connected during installation, press "Replace till key" (Settings → "Sales
   data") and put the new key into the till system.
5. Re-pair the phones.
6. Press "Back up now" and "Download" the backup, and give the store a copy.
7. Remove the installer's VPN access.

---

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| Tile says "WRONG PASSWORD" | Camera or recorder login changed | Camera "Settings" → "Camera username" / "Camera password", or the recorder panel → "Save sign-in" |
| Tile "OFFLINE" or "● NO PICTURE" | Network or recorder problem | "Reconnect"; check the recorder |
| People counted but the map is empty | Camera not placed or not calibrated | Steps 2.6 and 2.7 |
| "Visitors today" is an estimate | No entrance counting line | Draw a tripwire with "Counts store footfall (entrance line)" |
| Far-away shoppers are missed | CIF sub-stream | Step 2.5; check the tile's resolution badge |
| A poster, mannequin or TV screen is counted as a person | The AI sees a person-shaped figure | In Camera setup click its box → "Not a person — ignore this spot", or draw a "🚫 Ignore area (no AI)"; keep "Ignore figures that never move" on (2.4, 2.8) |
| Visitors counted twice | Same camera added twice | Resolve the duplicate banner (2.3) |
| Sales figures show "—" | No till data | Step 2.10 |
| No phone alerts | Alerts not enabled on the phone, person set to "No alerts", incident below the alert level, or the page was opened on a private http address | Step 2.11, "Send a test alert"; on iPhone, use the Home Screen app |
| iPhone shows no "Enable alerts" | Opened in Safari, not from the Home Screen | Share → "Add to Home Screen", open the icon (2.11) |
| "Direct video not possible from this network" | Viewer's network blocks direct video | "Check connection" / "Check remote video" |
| Sign-in refused for 5 minutes | Too many wrong passwords | Wait 5 minutes, or ask an administrator to "Reset password" (4.6) |
| "Your account can't change setup. Ask an administrator." | Signed in as an Operator | Ask an Owner or Administrator, or have them change your role (4.6) |
| Tile says "Hidden: privacy masks" | The camera has privacy masks and the device could not build the masked live view (for example the camera has sent no picture yet, or the video tools failed) | Check the camera works on the store network; press "Try again" on the tile. The raw picture is never shown instead |
| All remote tiles drop for a few seconds | Someone added, changed or deleted a privacy mask | None; the tiles reconnect by themselves (2.8) |
| No restricted-area or line alerts in "Area & line alerts" | The line is not set to "Alert staff on crossing", the area's schedule is not active, or the repeat time has not passed | Check the line or area in Camera setup (2.8) and the repeat times in Settings → "Alerts and detection" |
| Till sales stopped arriving | The till key was replaced or revoked | Put the current key into the till system, or "Create till key" (2.10) |
| Times, "Today" or the nightly backup are an hour or more off | Wrong store time zone | Settings → "Store" → "Time zone" (2.1) |
| "First-run setup is only available on the store network" | Setup tried through online access | Create the account on the store network or the VPN |

---

## Part 5. Not available yet

These are things a store set-up would normally need that the dashboard does not provide yet. Until
they exist, use the workaround.

| Missing | Effect today | Workaround |
|---|---|---|
| Muting one camera's alerts (for example during restocking) | Exists in the API only | Turn the camera's "Theft detection" off temporarily |
| Queue and checkout waiting times on screen | Measured, but not shown; only the "Queue congested after (seconds)" threshold is in the dashboard | None in the dashboard |
| Floor-plan image upload | The plan must be drawn | Draw it with the tools (2.2) |
| Staff areas and uniform recognition; street front, loading dock and car-park camera positions; store trading hours and delivery windows | Planned | Use restricted areas with schedules and the "Stockroom / staff only" purpose |
| Bulk export of incidents and evidence; daily report for a past date | One item at a time; report is today only | "Open in new tab" and save |
| Multi-channel import for recorders other than Dahua | Add each channel by address | 2.3 B |
| Undoing "They're different cameras" | Warning stays hidden | Ask the installer |
| Live video in the phone app away from the store | App shows video on the store network only | Use the dashboard through online access |
| Detector boxes on direct remote video | Remote viewers see the masked picture without boxes | None |
| The recorder's web (HTTP) port in the "Dahua recorder" panel | Channel names are read from port 80; a recorder on another web port gives "Channel names were not read from the recorder" | Type the names by hand (2.3 A) |
