# Store map v2 + branding: implementation contract (2026-10-16)

Shared contract for three parallel work streams. Each stream owns its files; do not edit
another stream's files. Use targeted edits in shared files (index.html, studio.html),
never whole-file rewrites.

## Goals (owner request, 2026-10-16)
Normal store staff must be able to map a store without help:
- grow/shrink the map in any direction, add separate rooms and outdoor areas (car park,
  loading dock, side lane), place cameras outside the building;
- edit anything already placed (rooms, walls, shelves, doors, counters...);
- a clear scale bar (and a measure tool);
- a camera gallery: pick a camera and drop it on the map; hovering shows a still picture
  of that camera, captured once from the live feed and cached in the browser;
- iKorex logo in the header and an intro/loading screen with the logo.

## Coordinate model (unchanged + "site")
Metres, origin top-left, x right, y down. `store_layouts.width_m/height_m` is now called the
**site** in the UI ("Map size"): it is the whole drawable canvas, which can include outdoor
areas. The building itself is just a ROOM structure. Nothing is clamped to "the building",
only to the site.

## Stream A — map editor core + backend (owner: A)
Files: `edge_backend/app/static/js/floorplan.js`, `edge_backend/app/static/css/floorplan.css`,
`edge_backend/app/routes/layout.py`, `edge_backend/app/services/store_layout_service.py`,
tests under `edge_backend/tests/` (new file `test_store_map_v2.py`), and ONLY the store-map
toolbar/panel region of `index.html` (the `#floorplan`/store map section).

Backend:
- `POST /api/v1/layout/extend` body `{left_m, top_m, right_m, bottom_m}` (each 0..500, at least
  one > 0; negative values = shrink that side, allowed only if nothing would fall outside,
  otherwise 409 `{code:"items_outside", items:[...]}`). Adding on left/top translates every
  zone, structure, camera position AND camera calibration (floor points + homography, i.e.
  compose a translation; a calibrated camera must still project to the same physical spot)
  by (+left, +top). Atomic (one transaction). Returns the full layout payload as `GET /layout`.
- `PUT /api/v1/layout` shrink: must no longer silently leave items outside; reject with the
  same 409 `items_outside` unless `{"clamp": true}` is passed.
- `POST /api/v1/layout/fit` -> shrink-wrap the site to the bounding box of all items plus a
  margin (default 2 m), using the same translate logic.
- New structure kind `OUTDOOR` (polygon; e.g. car park, loading dock). Default colour a
  muted green/grey. `properties.area_type` optional free text label.
- Existing kinds unchanged. Validation still clamps to the site.

Frontend (floorplan.js):
- Tool palette with clear labels + icons: Select, Room, Outdoor area, Wall, Door, Shelf,
  Counter, Obstacle, Zone, Measure. Rooms/outdoor/shelf/counter/obstacle default to
  **rectangle drag** drawing (press-drag-release); hold a "Free shape" toggle for polygons.
- Room presets in the inspector name field (Sales floor, Storeroom, Cool room, Office,
  Staff room, Loading dock, Car park).
- Editing placed items: bounding box with 8 resize handles + rotate handle for polygons;
  numeric Width / Depth / Angle / X / Y fields in the inspector; Duplicate (Ctrl/Cmd+D +
  button); arrow-key nudge (0.1 m, Shift = 1 m); Undo/Redo (Ctrl+Z / Ctrl+Shift+Z / Ctrl+Y)
  for create/move/resize/rotate/vertex/delete/rename, persisted to the server.
- Pointer Events (mouse + touch + pen); one-finger pan in Select mode on empty space,
  two-finger pinch zoom; `touch-action: none` on the canvas.
- "Map size" panel: shows W x D, buttons to add space on each side (+5 m presets and a number
  field) using `/layout/extend`, and "Fit to content". Dropping or dragging an item past the
  edge offers "Extend the map to fit?" (one click) instead of silently clamping.
- Scale bar: always visible bottom-left, with background plate, readable in light and dark
  theme, adapts (0.5/1/2/5/10/20/50 m). Measure tool: click two points, shows distance;
  Esc clears.
- Public JS API for stream B (must exist and stay stable):
  ```js
  window.FloorplanAPI = {
    ready: Promise<void>,                 // resolves after the first layout load
    getCameras(): Array<{camera_id, name, placed:boolean, x_m, y_m, azimuth_deg, fov_deg, calibrated:boolean, enabled:boolean}>,
    clientToWorld(clientX, clientY): {x_m, y_m, inside:boolean} | null, // null if not over the canvas
    canvasElement(): HTMLCanvasElement,
    placeCamera(cameraId, x_m, y_m): Promise<void>,  // persists via PATCH .../placement, offers extend if outside
    focusCamera(cameraId): void,          // select + pan to it, opens inspector
    setDropPreview(x_m, y_m | null): void,// ghost marker while dragging from the gallery; null clears
  };
  // Events dispatched on window:
  //   'fp:layout-changed'   detail: {cameras}
  //   'fp:camera-hover'     detail: {camera_id, clientX, clientY} | {camera_id:null}
  //   'fp:camera-selected'  detail: {camera_id}
  ```
  Stream A must NOT build a picture tooltip; it only dispatches `fp:camera-hover`.

## Stream B — camera gallery + still cache (owner: B)
Files (new): `edge_backend/app/static/js/camera_gallery.js`,
`edge_backend/app/static/js/still_cache.js`, `edge_backend/app/static/css/camera_gallery.css`.
In `index.html` add only the `<link>`/`<script defer>` tags (right after floorplan's tags) and
a single empty mount `<div id="cameraGalleryMount"></div>` inside the store map section,
next to the canvas container. All gallery DOM is built from JS.
- Gallery panel beside the map (collapsible; bottom sheet on phones): card per camera with
  cached still, name, status chips (Placed / Not placed, Calibrated, Off), search box,
  filter (Not placed / All). Drag a card onto the map (pointer events, works on touch) to
  place it there; a ghost marker follows via `FloorplanAPI.setDropPreview`. Click a card ->
  `FloorplanAPI.focusCamera`. "Place" button on unplaced cards = place at the map centre.
- Hover preview: on `fp:camera-hover` (and on card hover) show a floating picture card
  (still + name + "captured 5 min ago"), positioned to stay on screen.
- `StillCache` (still_cache.js): IndexedDB store `edge-stills` (Blob + captured_at +
  camera_id + width/height). Capture once per camera on demand; never poll.
  - Store network: `GET /api/v1/cameras/{id}/snapshot?annotate=false&max_width=480` via
    `edgeAuth.authUrl` / authenticated fetch.
  - Public address (`window.WebRtcLive && WebRtcLive.mode() === 'webrtc'`): one frame through
    `WebRtcLive.loadStill`/grabFrame, strictly ONE capture at a time, and stop if the page is
    hidden. Never call the snapshot endpoint over the tunnel (403 video_direct_only).
  - Stale after 24 h -> shown with "old picture" badge; refresh button per card and
    "Refresh all pictures" (sequential, cancellable, shows progress).
  - Privacy: the cache is per browser; clear it on sign-out (listen for the auth sign-out
    path in auth.js: add a tiny hook `window.dispatchEvent(new Event('edge:signed-out'))`
    in auth.js at the sign-out point — the only auth.js change allowed) and when the
    signed-in user changes.
  - Disabled cameras: no capture.

## Stream C — iKorex branding + intro screen (owner: C)
Files: `edge_backend/app/static/brand/` (new: optimised logo assets),
`edge_backend/app/static/js/splash.js` (new), `edge_backend/app/static/css/brand.css` (new),
and in `index.html` / `studio.html` only the `<head>`, the brand group inside the header,
and a splash block as the first child of `<body>`. Also the sign-in gate's logo area in
auth.js if it renders the login card (logo markup only).
- Sources: `/home/meoclavezz/Projects-1/Ikorex logo left aligned black.png` (3655x1294, dark
  text, for light theme), `/home/meoclavezz/Projects-1/ikorex logo left aligned white.png`
  (5334x5334 square canvas, white text, needs trimming, for dark theme),
  `/home/meoclavezz/Projects-1/ikorex-logo-white-top-aligned.png` and
  `/home/meoclavezz/Projects-1/Ikorex logo black top aligned .png` (stacked, for splash).
  Produce trimmed, resized PNG + WebP (header ~2x of 32 px height, splash ~2x of 160 px
  wide), small files (<40 KB each). Use ImageMagick (`magick`).
- Header: iKorex logo (theme-aware: light/dark swap following the existing theme system,
  `data-theme` + prefers-color-scheme) before the product name; on phones logo mark only if
  space is tight. Keep the existing title/subtitle.
- Intro: full-screen splash with stacked logo, a subtle animation (fade/scale + progress
  shimmer), shown immediately (inline critical CSS so it paints before deferred JS), hidden
  when the app is ready (`edgeAuth.onReady` + first paint) or after a max 8 s, minimum
  ~900 ms on first visit, ~300 ms on later visits in the same session (sessionStorage),
  respects `prefers-reduced-motion`, never blocks the sign-in gate (fade out as soon as the
  gate shows). Studio page too.
- Must pass the CSP (check `services/web_delivery.py`/main.py for the CSP: no inline script
  if the CSP forbids it; use a small external script + CSS that can paint first).

## Shared rules
- Labels in plain language for non-technical staff. Australian English.
- No new third-party dependencies. Icons from the existing Lucide sprite (`icons.js`).
- `?v=` query on new assets like the others (server rewrites it to a content hash).
- Phone width 360 px must work; dark and light themes.
- Verification per stream: `node --check` on changed JS; stream A runs
  `cd edge_backend && ../.venv/bin/python -m pytest tests/ -q -x` (or the relevant subset plus
  the new tests). Do NOT start uvicorn or a browser; the integrator runs one combined
  headless-Chrome check afterwards (16 GB RAM machine).
- Do not commit; the integrator commits.

## Camera coverage, reach and real sizes (2026-10-28)

Owner request: the camera cone had a fixed length; make the reach per camera, connect cameras to
the shelves and zones they really see, and make good use of the real dimensions (metres).
Floorplan.js stays owned by the map stream; other modules use only the API and events below.

### Data (m0022, additive)
- `cameras.range_m` FLOAT NULL: reach in metres (0.5..100). NULL = lens default
  `coverage_geometry.default_range_m(fov)` = `10 * tan(42.5°) / tan(fov/2)`, clamped 3..30 m
  (85° -> 10 m; >= 179° -> 6 m).
- `cameras.coverage_mode` VARCHAR(16) NULL: NULL/"auto" (calibrated footprint when there is one,
  else the manual cone) | "calibrated" | "manual".

### API
- `PATCH /api/v1/layout/cameras/{id}/placement` also takes `range_m` (left out = unchanged, `null` =
  back to the default, outside 0.5..100 -> 422) and `coverage_mode` (auto|calibrated|manual); the answer
  adds `range_m`, `range_default_m`, `coverage_mode`, `calibrated_footprint`.
- `GET /api/v1/layout` cameras add `range_m`, `range_default_m`, `coverage_mode`,
  `calibrated_footprint` = `{polygon:[{x,y}], near_m, far_m, area_m2}` | null.
- `GET /api/v1/layout/coverage` (new): `cameras[]` {camera_id, mode, counted, enabled, calibrated,
  footprint_available, occlusion_applied, range_m, range_is_default, near_m, far_m, polygon, area_m2,
  floor_seen_m2, zones[{zone_id,name,category,covered_fraction,seen_m2}],
  shelves[{structure_id,name,kind,seen_front_m,front_m,length_m}], overlaps[{camera_id,m2}], watch_zone_id};
  `zones[]` {zone_id, area_m2, covered_fraction, calibrated_covered_fraction, cameras[{camera_id,
  covered_fraction, calibrated}]}; `shelves[]` {structure_id, length_m, front_m, seen_front_m,
  cameras[...]}; `store` {floor_source rooms|zones|rooms+zones|site, floor_m2, covered_m2, uncovered_m2,
  coverage_pct, overlap_m2, cameras_counted, gaps[{area_m2,x,y,bbox,zone_id,zone_name,zone_names}]}.
- `GET /api/v1/analytics/space?from&to` (new): `sales_floor_m2`, `sales_floor_source`, `revenue`,
  `sales_per_m2`, `transactions`, `footfall`, `footfall_per_100m2`, `walking` {paths_measured,
  avg_path_m, median_path_m, p90_path_m, total_walked_m}, `zones[]` per-m² figures. Null when not observed.
- Zone metrics (`/analytics/floorplan` zones[].metrics, `/analytics/space`) add `area_m2`,
  `visits_per_m2`, `dwell_seconds_per_m2`, `people_per_m2_now`, `shelf_length_m` (SHELF zones),
  `reaches` (shelf_interactions with that shelf_zone_id), `reaches_per_shelf_m`; null when not observed.
- `GET /api/v1/analytics/heatmaps` (observed) adds `cell_area_m2`, `peak_per_m2`.

### Coverage algorithm (services/coverage_geometry.py; same in floorplan.js)
1. Blockers: WALL centre lines; ROOM / SHELF / OBSTACLE outlines; DOOR segments cut the wall / room
   edge they lie on (both ends within max(0.3 m, door thickness), < 15° apart); OUTDOOR and DOOR never
   block; COUNTER does not block by default; a fixture with `properties.height_m` < 1.2 m does not
   block; `properties.blocks_view` (bool) overrides. Hits closer than 0.15 m to the camera are ignored
   (wall-mounted cameras); a fixture the camera sits inside is ignored.
2. Manual: rays every degree of the FOV plus ±1e-4 rad around every blocker end point in range; each
   ray stops at the first blocker or the reach; polygon = camera + ray ends. Bearing 0 = up, clockwise.
3. Calibrated: image rectangle (calibration frame size) clipped to projective depth
   `w >= w(farthest calibration point) / 3` (drops the horizon and the unreliable band near it),
   projected by the homography (convex), limited to 60 m; then intersected (Sutherland-Hodgman) with
   the visibility polygon over the footprint's angular span from the camera's map spot. Not placed ->
   footprint without occlusion. Near/far = nearest/farthest point of the footprint part on the map.
4. Everything is clipped to the site rectangle. Store numbers rasterise the site (cell = max(0.25 m,
   sqrt(W*H/200000))): floor = ROOM ∪ countable zones (else the site) minus SHELF/COUNTER/OBSTACLE;
   coverage % = floor cells seen by >= 1 counted camera (switched on and placed); overlap = >= 2;
   gaps = 4-connected uncovered floor components >= 1 m². Shelf fronts = polygon edges within 30° of
   the oriented box's long side, sampled every 0.25 m, 3 cm outside; a sample is seen when inside the
   camera's polygon.
5. Shelf attribution (pose_analytics floor fallback): when the projected wrist lands in no SHELF zone
   and the camera is placed, the hand lies on the sight-line segment `C + (P - C)(H - h)/H` for
   h = 0.4..2.0 m; the first SHELF zone it crosses from the camera is used
   (`INTERACTION_FLOOR_SIGHT_LINE`, default on).

### Frontend (floorplan.js)
- Camera inspector: "Reach (m)" + "Default (… m)", square reach handle on the cone tip (round handle
  turns; Shift = whole metres; undoable), "Use calibrated coverage" / "Manual cone", "What it sees"
  (zones, shelf fronts, overlaps, "Count this camera into <zone>" via `PUT .../cameras/{id}/zone`).
- Shelf / counter inspector: length, depth, front, metres seen per camera, "Blocks camera view".
- Zone inspector: size, share in view per camera, per-m² figures; restricted zones (category
  `RESTRICTED`, or `kind`/`restricted`/`properties.restricted`) are drawn with a red cross-hatch and get
  a "Restricted area settings" button.
- Empty inspector: "Store coverage" (%, blind spots with zoom, floor per camera, per-m² store figures,
  "Print or save the map": PNG at 1:50..1:500 or fit A4/A3 at 150 dpi on light paper colours with title
  and scale bar, or print with the sheet sized in mm).
- Layers "Coverage" and "Blind spots" (localStorage `edge_cctv_map_coverage`).

### Public API additions (backward compatible)
```js
FloorplanAPI.camerasSeeingZone(zoneId) // -> [{camera_id, covered_fraction}] (switched-on, placed cameras)
FloorplanAPI.getCoverage()             // -> store numbers (as GET /layout/coverage store) | null
FloorplanAPI.exportMap(kind, scale)    // kind 'png'|'print', scale '50'|'100'|'200'|'500'|'fit'|'fitA3'
FloorplanAPI.getCameras()              // entries add range_m, range_is_default, coverage_mode, covered_m2
// Events on window:
//   'fp:coverage-changed'     detail: {store}
//   'fp:restricted-settings'  detail: {zone_id}   (the restricted-areas module builds its own panel)
```

### Insights
`business_analysis_service._detect` low-traffic rule (STORE_LAYOUT): with >= 2 AISLE/DEPARTMENT zones
that have an area, compares visits per m² against their average (evidence `visits_per_m2`,
`store_avg_visits_per_m2`, `area_m2`, `basis: per_m2`); without areas the old count rule
(`basis: visits`). A zone the calibrated cameras see < 50% of (and no linked uncalibrated camera) is
not judged; the reason is listed in `suppressed_rules`. Insight topics' headline uses the per-m² ratio
when present.
