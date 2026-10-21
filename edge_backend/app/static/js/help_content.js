/**
 * Help content for the store dashboard (used by js/help.js and js/onboarding.js).
 *
 * Plain language, Australian English, button and field names exactly as they
 * appear on screen. Edit the words here; no other file needs to change.
 *
 *   SUPPORT   iKorex support contact shown in the Help panel (owner to fill in).
 *   topics    short "?" explanations: { title, text: [paragraphs], list?: [items], article? }
 *   anchors   where "?" buttons go on cards that scripts build at run time
 *             (static cards use data-help / data-help-after in index.html).
 *   articles  the searchable how-to guides in the Help panel.
 *
 * Text is inserted as text (escaped), never as HTML.
 * Actions ("go") are names that js/help.js knows: today, cameras, map,
 * map-edit, map-zone, add-cameras, insights, insights-footfall, loss,
 * studio, settings:<card id>, checklist, welcome.
 */
(function () {
  'use strict';

  // ---------------------------------------------------------------------------
  // SUPPORT CONTACT: TO BE SUPPLIED BY THE OWNER.
  // Fill in iKorex's real support details here. A value left null is not shown;
  // with all of them null the panel says to ask the installer.
  // ---------------------------------------------------------------------------
  const SUPPORT = {
    name: 'iKorex support',
    phone: null,     // e.g. '1300 000 000'  (OWNER TO SUPPLY)
    email: null,     // e.g. 'support@example.com.au'  (OWNER TO SUPPLY)
    hours: null,     // e.g. 'Monday to Friday, 9 am to 5 pm (Melbourne time)'  (OWNER TO SUPPLY)
    website: null,   // e.g. 'https://example.com.au/support'  (OWNER TO SUPPLY)
  };

  // Names of the features the iKorex plan can turn off (services/entitlements.py FEATURE_LABELS).
  const FEATURE_LABELS = {
    theft_detection: 'Theft alerts',
    zone_alerts: 'Area and line alerts',
    night_watch: 'Night watch',
    phone_alerts: 'Phone alerts',
    heatmaps: 'Heatmaps',
    business_insights: 'Business insights',
    online_access: 'Online access',
    remote_live_video: 'Live video from outside the store',
    pos_integration: 'Sales data (point of sale)',
  };

  // ---------------------------------------------------------------- "?" topics
  const topics = {
    'store-setup': {
      title: 'Store setup',
      text: ['Shows how complete the setup of each camera is for the purpose you gave it, and the next step to do. "N% DONE" counts only the required steps.',
        'For the whole store (map, phones, accounts, backups), use the Getting started checklist at the top of Today.'],
      article: 'setup-order',
    },
    'visitors-by-hour': {
      title: 'Visitors by hour',
      text: ['Bars are today; the lines are yesterday and the same day last week, so you can see whether today is busier than usual.',
        'Hatched hours were not recorded (for example a camera was off). For exact counts, draw an entrance counting line in Camera setup.'],
      article: 'reading-figures',
    },
    'needs-review': {
      title: 'Needs review',
      text: ['Up to five alerts waiting for a person to check. An alert is suspicious behaviour to look at, never a finding of theft.',
        'Press Review to open it in Loss prevention and record what really happened.'],
      article: 'review-alert',
    },
    'map-tools': {
      title: 'Store map tools',
      text: ['Press "Edit map", then pick a tool. "Now:" shows what a click on the map will do.'],
      list: ['Room, Outdoor area, Shelf, Counter, Obstacle and Zone: press at one corner and drag to the opposite corner.',
        'Wall and Door: click point by point (hold Shift for straight lines).',
        'Free shape: click each corner for odd shapes, then press Finish.',
        'Undo and Redo are the two arrow buttons; Esc cancels a shape.'],
      article: 'draw-plan',
    },
    'map-size': {
      title: 'Map size',
      text: ['The map is the whole site in metres, including the car park or loading dock. You do not have to guess the final size.',
        'Add space on any side later and everything already drawn stays where it is. "Fit to content" trims the map to what is drawn, plus 2 m.'],
      article: 'draw-plan',
    },
    'inspector': {
      title: 'Details panel',
      text: ['Click an area, shelf, wall or camera on the map to see and change it here: its name, size, angle and, for store areas, the "Category" and "Products sold here".',
        'For an uncalibrated camera, "Counts into zone" counts everyone it sees as a visit to that area.'],
      article: 'zones',
    },
    'add-cameras': {
      title: 'Cameras & devices',
      text: ['Add cameras here. With a Dahua recorder, press "Dahua recorder" to add many channels at once (recommended). Otherwise use "Add camera" with the stream address, or "Scan network".',
        'Each camera then has buttons to turn it on or off, open its settings, see its checklist, calibrate it and place it on the map.'],
      article: 'add-cameras',
    },
    'camera-gallery': {
      title: 'Camera gallery',
      text: ['A card for each camera with a picture. Drag a card onto the map where the camera is mounted, or press "Place" and then drag the marker.',
        'The pictures are not live: each one is taken once and kept only in this browser. Press "Refresh all pictures" to update them.'],
      article: 'place-cameras',
    },
    'calibration': {
      title: 'Calibration',
      text: ['Calibration tells the system which spot on the map each point of the floor in the camera picture is. Without it a camera counts people but cannot put them on the map or the heatmap.',
        'Click a spot on the floor in the camera picture, then the same spot on the map. Do this for at least 4 spots spread near and far, then press "Save calibration". Good accuracy is 25 cm or better.'],
      article: 'calibrate',
    },
    'zones': {
      title: 'Store areas (zones)',
      text: ['A store area is a part of the floor where visits, time spent and how many people are there are measured: an aisle, a department, the entrance, a checkout.',
        'Draw them with the Zone tool on the Store map. Areas must not overlap. Product shelves for hand reaches are drawn on the camera picture in Camera setup.'],
      article: 'zones',
    },
    'zone-alerts': {
      title: 'Area & line alerts',
      text: ['Alerts when someone is inside a restricted area (for example the stockroom after closing) or crosses a line set to alert staff.',
        'They are set up per camera in Camera setup and are prompts to check, not theft findings. Press "Acknowledge" when someone has looked into it.'],
      article: 'area-line-alerts',
    },
    'night-watch': {
      title: 'Night watch',
      text: ['While a camera is watching at night, its normal analysis pauses and it looks for movement. A person seen then is a high alert to this dashboard and to phones.',
        'Turn it on per camera: open the camera settings and tick "Watch this camera at night", then set the times and days.'],
      article: 'night-watch',
    },
    'live-behaviour': {
      title: 'Live behaviour',
      text: ['People the cameras are watching right now, for example a reach into a shelf or a hand at a pocket. These are live cues, not findings.',
        '"Watch live" opens the camera enlarged with the person outlined.'],
      article: 'review-alert',
    },
    'review-queue': {
      title: 'Review queue',
      text: ['Each entry is suspicious behaviour for staff to check. Look at the evidence and the live camera, press "I\'m checking", then "Record outcome".',
        'Always record an outcome, including "False alarm": the false-alarm figures and the trust in each check come only from what staff record.'],
      article: 'review-alert',
    },
    'false-alarms': {
      title: 'False alarms by rule',
      text: ['How often each check was wrong, from the outcomes staff recorded. A high rate means that check needs adjusting for this store; look here before lowering the theft confidence in Settings.'],
      article: 'review-alert',
    },
    'patterns': {
      title: 'Patterns',
      text: ['Where and when the checks fire, built only from incidents recorded on this device. Use it to plan when staff should be on the floor.',
        'A burst is several incidents at one camera and area in a short time. It does not identify a person.'],
    },
    'recommendations': {
      title: 'Recommendations',
      text: ['Suggestions from the store analysis, made from measured visits, time spent, shelf reaches and heatmaps. It runs by itself every hour.',
        'Each card says why and what to do. Mark it "Done", "Mark reviewed" or "Dismiss" so the list stays useful.'],
      article: 'insights',
    },
    'entrances': {
      title: 'Entrances today',
      text: ['People counted in and out at each counting line. These give exact visitor numbers.',
        'Draw a line across each entrance in Camera setup (Tripwire tool) and tick "Counts store footfall (entrance line)".'],
      article: 'lines-areas',
    },
    'friction': {
      title: 'Areas where shoppers leave without engaging',
      text: ['Store areas where visitors seldom reach for a shelf. It needs store areas on the map, a calibrated camera and product shelves drawn in Camera setup.'],
      article: 'insights',
    },
    'product-reach': {
      title: 'Product shelf reaches',
      text: ['How many times shoppers reached into each product shelf today. Draw product shelves in Camera setup and keep "Shelf interaction" on for that camera.'],
      article: 'lines-areas',
    },
    'heatmaps': {
      title: 'Heatmap history',
      text: ['Recorded every hour from what the cameras saw: where shoppers walked, stopped or touched shelves.',
        'Hatched hours were not recorded, so they are unknown, not zero. Heatmaps need cameras that are calibrated.'],
      article: 'heatmaps',
    },
    'ai-features': {
      title: 'AI features on this camera',
      text: ['Choosing the camera\'s purpose switches the right ones on for you. Change them here only if you need to.'],
      list: ['People counting & dwell: visits, time spent, footfall and heatmaps.',
        'Shelf interaction: hand reaches into product shelves.',
        'Theft detection: suspicious-behaviour checks for staff to review.',
        'Theft evidence clip: also saves about 5 s before to 10 s after each theft alert.',
        'Ignore figures that never move: posters and mannequins are not counted.'],
      article: 'camera-purpose',
    },
    'stream-quality': {
      title: 'Stream quality',
      text: ['Keep "Auto". It picks the smallest sub-stream that is at least D1 (704 x 576), which is enough for the AI and light on this device.',
        'Very small CIF sub-streams (352 x 288) miss far-away shoppers: upgrade them in Settings > Recorder sub-streams.'],
      article: 'sub-streams',
    },
    'placement': {
      title: 'Placement on the map',
      text: ['Where the camera is mounted, in metres from the top-left corner of the map, its height and which way it points. It is easier to drag the camera on the Store map.',
        'Placement alone does not put people on the map: that needs calibration.'],
      article: 'place-cameras',
    },
    'department': {
      title: 'Department label',
      text: ['Optional free text, for example LIQUOR. It groups this camera\'s alerts in reports. What the camera measures is set by its purpose, not by this label.'],
    },
    'device': {
      title: 'Device',
      text: ['The name and ID of this CCTV box. The "Device name" is what phones see when choosing a store; the "Device ID" never changes.'],
      article: 'store-settings',
    },
    'ikorex-service': {
      title: 'iKorex service',
      text: ['Your plan with iKorex: whether the service is active, the licence end date, how many cameras are included and which features are in your plan.',
        'Losing the internet never pauses anything. Store video and pictures never leave this device.'],
      article: 'ikorex-service',
    },
    'phone-alerts': {
      title: 'Phone alerts',
      text: ['Theft alerts on staff phones from the dashboard itself, installed as an app. It works only on the store\'s public address (https), not on a private http address.',
        'Each person installs the app on their own phone and taps "Enable alerts". An owner or administrator then chooses who gets alerts first and who is the backup.'],
      article: 'phone-alerts',
    },
    'your-account': {
      title: 'Your account',
      text: ['Your username, name and role. Use "Change password" to set your own password; other browsers signed in to your account are then signed out.'],
      article: 'accounts',
    },
    'accounts-roles': {
      title: 'Accounts and roles',
      text: ['Give everyone their own account.'],
      list: ['Owner: everything, including other owner accounts.',
        'Administrator: sets up the store, cameras, phones and accounts.',
        'Operator (staff): watches cameras, reviews and acknowledges alerts, records outcomes; cannot change setup.'],
      article: 'accounts',
    },
    'store-settings': {
      title: 'Store',
      text: ['The "Store name" is shown in the page header and on the daily report.',
        'The "Time zone" decides what "today", schedules, alert times and the nightly backup mean. Pick the store\'s own zone, for example Australia/Melbourne.'],
      article: 'store-settings',
    },
    'evidence-storage': {
      title: 'Evidence storage',
      text: ['Alert pictures and short clips are kept on this device\'s own disk. The oldest are deleted first when the size limit or the "Keep evidence for (days)" age is reached.',
        'There is no continuous recording: the store\'s own recorder (NVR) does that. To keep evidence for police or insurance, open it and save it elsewhere straight away.'],
      article: 'evidence',
    },
    'alerts-detection': {
      title: 'Alerts and detection',
      text: ['How sensitive the alerts are and how soon the same alert can repeat. Every change applies at once.',
        'Check "False alarms by rule" in Loss prevention before lowering "Theft minimum confidence (%)"; lower finds more but brings more false alarms.'],
      article: 'evidence',
    },
    'theft-levels': {
      title: 'Theft alert levels',
      text: ['Each theft alert gets a level (Review, Watch, Alert or Critical) from its risk score. Here you set the score for each level and who is told at each level (sound, phones, escalation).',
        'The defaults suit most stores. Change one thing at a time and watch the false-alarm rate.'],
      article: 'review-alert',
    },
    'analysis-speed': {
      title: 'Analysis speed',
      text: ['How much of this device\'s capacity goes to analysing the cameras, and how many pictures per second each camera gets.',
        'The defaults are chosen for this hardware. Lower them if the device is slow; raise a single camera\'s priority in its own settings.'],
    },
    'online-access': {
      title: 'Online access',
      text: ['Lets you open this dashboard away from the store at the store\'s public address, without opening a port on the store\'s router. It is usually set up by the installer.',
        'Phone alerts need it. Live video goes straight from this device to your browser, only while someone is watching.'],
      article: 'online-access',
    },
    'remote-video': {
      title: 'Live video away from the store',
      text: ['Settings for direct live video when you open the dashboard through online access. Keep the defaults; press "Check remote video" from outside the store to test it.',
        'Privacy masks are applied on this device before any video leaves it.'],
      article: 'online-access',
    },
    'phone-app-backup': {
      title: 'Phone app (backup)',
      text: ['Pairing for the older native phone app, kept as a backup. Most stores use the installed web app instead (Phone alerts).'],
      article: 'phone-alerts',
    },
    'sub-streams': {
      title: 'Recorder sub-streams',
      text: ['Many recorders ship with very small (CIF) sub-streams that are too small for reliable detection. This upgrades them to D1, one channel at a time, and puts a channel back if it does not work.',
        '"Check sub-streams" only reads the recorder. Save the recorder sign-in first (Store map > Dahua recorder > "Save sign-in").'],
      article: 'sub-streams',
    },
    'sales-data': {
      title: 'Sales data (point of sale)',
      text: ['Connect the tills to see sales, the buying rate and "Bought" in the shopper journey. The till system sends each sale with a till key that opens nothing else.',
        'This is optional: everything else works without it.'],
      article: 'tills',
    },
    'backups': {
      title: 'Backups and reset',
      text: ['A backup holds the store layout, cameras, settings, accounts and recorded figures (not the evidence pictures). One is taken at every start and every night at 03:00.',
        'Backups stay on this device unless you press "Download". Download one regularly and keep it somewhere safe.'],
      article: 'backups',
    },
    'system-health': {
      title: 'System health',
      text: ['Read-only figures about this device: video decoding, the detection engine and its speed, processor, memory, how long it has been running and evidence storage use.'],
    },
  };

  // Cards built by scripts at run time: where their "?" goes.
  // where: 'append' (inside the element, at the end) or 'after' (right after it).
  const settingsTitle = (id) => `#${id} > .card-title:first-child > span:first-child`;
  const anchors = [
    { selector: settingsTitle('settings-device'), topic: 'device' },
    { selector: settingsTitle('settings-fleet'), topic: 'ikorex-service' },
    { selector: settingsTitle('settings-phone-alerts'), topic: 'phone-alerts' },
    { selector: settingsTitle('settings-account'), topic: 'your-account' },
    { selector: settingsTitle('settings-accounts'), topic: 'accounts-roles' },
    { selector: settingsTitle('settings-store'), topic: 'store-settings' },
    { selector: settingsTitle('settings-evidence'), topic: 'evidence-storage' },
    { selector: settingsTitle('settings-alerts'), topic: 'alerts-detection' },
    { selector: settingsTitle('settings-theft-alerts'), topic: 'theft-levels' },
    { selector: settingsTitle('settings-analysis'), topic: 'analysis-speed' },
    { selector: settingsTitle('settings-remote'), topic: 'online-access' },
    { selector: settingsTitle('settings-remote-video'), topic: 'remote-video' },
    { selector: settingsTitle('settings-pairing'), topic: 'phone-app-backup' },
    { selector: settingsTitle('settings-recorder-streams'), topic: 'sub-streams' },
    { selector: settingsTitle('settings-backups'), topic: 'backups' },
    { selector: '#insights-heatmap-history > .card-title:first-child > span:first-child', topic: 'heatmaps' },
    { selector: '#cameraGalleryMount .cg-head', topic: 'camera-gallery' },
    { selector: '#fpCalibrationPanel .fp-cal-title', topic: 'calibration' },
    { selector: '#nightWatchMount > .form-section-title:first-child', topic: 'night-watch' },
  ];

  // --------------------------------------------------------------- articles
  // role: 'admin' = owners and administrators only; omitted = everyone.
  const articles = [
    {
      id: 'setup-order', title: 'Set up the store, step by step', role: 'admin',
      keywords: 'start begin first commissioning install checklist order getting started',
      summary: 'The order that works best. The Getting started checklist on Today follows it and ticks steps off by itself.',
      steps: ['Settings > Store: enter the store name and pick the time zone.',
        'Store map: press "Edit map" and draw the building, shelves and checkouts.',
        'Add the cameras (a Dahua recorder adds all channels at once).',
        'Give each camera its purpose (entrance, checkout, aisle, stockroom...).',
        'Put each camera on the map, then calibrate it.',
        'Draw store areas with the Zone tool, and product shelves in Camera setup.',
        'Optional: area and line alerts, night watch, the tills.',
        'Phone alerts, staff accounts, and download a backup.'],
      go: 'checklist', goLabel: 'Open the checklist',
    },
    {
      id: 'add-cameras', title: 'Add cameras', role: 'admin',
      keywords: 'camera add new recorder nvr dahua rtsp onvif scan channel stream',
      summary: 'Cameras are best added as channels of the store\'s recorder (NVR).',
      steps: ['Open Store map > "Cameras & devices" (or "Add cameras" on the Cameras tab).',
        'Press "Dahua recorder". Enter the "Recorder IP / host", "Username" and "Password".',
        'Press "Save sign-in", then "Scan recorder channels".',
        'Check each channel\'s name and purpose, untick the ones you do not want, and press "Add selected channels".',
        'Another make of recorder or camera: press "Add camera", choose the "Source type", enter the stream address, press "Test connection" and then "Save camera".'],
      tips: ['Keep the sub-stream ("Sub-stream (recommended for AI)"): it is lighter and enough for the AI.',
        'A camera added twice shows a banner: keep one and turn the other off.'],
      go: 'add-cameras', goLabel: 'Go to Cameras & devices',
    },
    {
      id: 'camera-purpose', title: 'Choose what each camera looks at', role: 'admin',
      keywords: 'purpose role entrance exit checkout aisle stockroom high value settings features',
      summary: 'The purpose switches on the right checks for the camera and builds its setup checklist.',
      steps: ['Open the camera settings (the gear on a camera tile, or "Config" in the camera list).',
        'Change the "Camera name" to something staff recognise.',
        'Under "Camera purpose", choose what the camera looks at and press "Use this purpose".',
        'Press "Save".'],
      tips: ['Entrance cameras need a counting line; aisle cameras need product shelves; a stockroom camera needs a restricted area with a schedule.'],
      go: 'cameras', goLabel: 'Open Cameras',
    },
    {
      id: 'draw-plan', title: 'Draw the store plan', role: 'admin',
      keywords: 'map plan floor draw room wall door shelf counter outdoor size metres edit undo',
      summary: 'The plan is drawn in metres; there is no floor-plan image to upload.',
      steps: ['Store map: press "Edit map".',
        'Press "Map size" and add space so the whole site fits. You can add more later.',
        'Pick "Room" and drag out the building. Add "Outdoor area" for the car park or loading dock.',
        'Add shelves ("Shelf"), checkouts ("Counter"), doors ("Door") and pillars ("Obstacle").',
        'With "Select", click a shape to move, resize or rename it.'],
      tips: ['Everything saves as you draw. Undo is the arrow button in the toolbar (or Ctrl+Z).',
        'On a phone the map is view-only until you press "Edit map".'],
      go: 'map-edit', goLabel: 'Open the Store map',
    },
    {
      id: 'place-cameras', title: 'Put cameras on the map', role: 'admin',
      keywords: 'place camera map gallery drag position bearing field of view',
      summary: 'Show where each camera is mounted and which way it points.',
      steps: ['Store map: open the "Cameras" gallery beside the map and choose "Not placed".',
        'Drag a camera card onto the map where the camera is mounted (or press "Place" and drag the marker).',
        'Drag the tip of the wedge to point it the way the camera looks.'],
      tips: ['Placement alone does not put people on the map. Calibrate the camera next.'],
      go: 'map', goLabel: 'Open the Store map',
    },
    {
      id: 'calibrate', title: 'Calibrate a camera to the floor', role: 'admin',
      keywords: 'calibrate calibration accuracy floor points map heatmap people position',
      summary: 'Calibration puts the people a camera sees onto the map and the heatmap.',
      steps: ['Store map: select the camera and press "Calibrate".',
        'Click a spot on the floor in the camera picture: a tile corner, a shelf foot or a door threshold.',
        'Click the same spot on the map.',
        'Repeat for at least 4 spots, spread near and far across the picture.',
        'Press "Save calibration" and read the accuracy: up to 25 cm is good.'],
      tips: ['If one point is more than 1 m off, remove it with its cross and add another.',
        'Not ready to calibrate? Select the camera and pick the area it looks at under "Counts into zone" so its visits are still counted.'],
      go: 'map', goLabel: 'Open the Store map',
    },
    {
      id: 'zones', title: 'Draw store areas (zones)', role: 'admin',
      keywords: 'zone area aisle department entrance checkout category products inspector',
      summary: 'Areas are where visits, time spent and recommendations are measured.',
      steps: ['Store map: press "Edit map" and pick "Zone".',
        'Drag out one area per aisle, department, entrance and checkout.',
        'Select each area and fill in "Zone name", "Category" and "Products sold here".'],
      tips: ['Areas must not overlap.',
        'ENTRANCE, EXIT and CHECKOUT areas drive the "Exit without passing checkout" check.'],
      go: 'map-zone', goLabel: 'Draw a zone',
    },
    {
      id: 'lines-areas', title: 'Counting lines, shelves, restricted areas and masks', role: 'admin',
      keywords: 'camera setup studio tripwire line counting entrance product shelf restricted area privacy mask ignore poster mannequin',
      summary: 'These are drawn on a camera\'s own picture, on the Camera setup page.',
      steps: ['Open Camera setup (the button on a camera tile) and choose the camera.',
        'Tripwire: click both ends of a line across a doorway. Tick "Counts store footfall (entrance line)" for entrances.',
        'Product shelf: click the corners of a shelf face, save, and fill in the product details.',
        'Restricted area: click the corners and set the schedule.',
        'Privacy mask hides part of the picture; Ignore area stops the AI looking at a poster, mannequin or screen.'],
      tips: ['A poster counted as a person: click its box on the picture and press "Not a person — ignore this spot".'],
      go: 'studio', goLabel: 'Open Camera setup',
    },
    {
      id: 'area-line-alerts', title: 'Area and line alerts',
      keywords: 'restricted stockroom after hours line crossing back door acknowledge alert',
      summary: 'Alerts when someone is somewhere they should not be, or crosses a line.',
      steps: ['Set up (administrators): in Camera setup, draw a restricted area with a schedule, or tick "Alert staff on crossing" on a line.',
        'When one comes in: Loss prevention > "Area & line alerts". Look at the picture or watch the camera.',
        'Press "Acknowledge" when someone has looked into it.'],
      go: 'loss', goLabel: 'Open Loss prevention',
    },
    {
      id: 'night-watch', title: 'Night watch', role: 'admin',
      keywords: 'night after hours closed intrusion motion arm schedule',
      summary: 'Cameras that watch for people while the store is closed.',
      steps: ['Open the camera settings and scroll to "Night watch".',
        'Tick "Watch this camera at night", set "Start" and "End" in store time and tick the days.',
        'Press "Save night watch".'],
      tips: ['Night events show in Loss prevention > "Night watch" and go to phones as high alerts.'],
      go: 'cameras', goLabel: 'Open Cameras',
    },
    {
      id: 'phone-alerts', title: 'Get alerts on your phone',
      keywords: 'phone mobile push notification iphone android install app enable alerts first priority backup escalate',
      summary: 'The dashboard installs on a phone as an app and sends alerts. No app store needed.',
      steps: ['On the phone, open the store\'s public address (https://...) and sign in.',
        'Android (Chrome): tap "Install app". iPhone (Safari): tap Share, then "Add to Home Screen".',
        'Open the new app icon, tap "Enable alerts" and choose "Allow".',
        'Settings > Phone alerts: press "Send a test alert".',
        'Owners and administrators: under "Who gets alerts", set at least 2 people to "First priority".'],
      tips: ['On iPhone, alerts only work from the Home Screen app.',
        'It does not work on a private http:// address.'],
      go: 'settings:settings-phone-alerts', goLabel: 'Open Phone alerts',
    },
    {
      id: 'review-alert', title: 'Review a theft alert',
      keywords: 'alert incident review evidence outcome false alarm acknowledge checking staff sent banner',
      summary: 'An alert is suspicious behaviour for a person to check, never a finding of theft.',
      steps: ['Press "Review now" on the red banner, or open Loss prevention > "Review queue".',
        'Look at the evidence picture (click it to enlarge) and the live camera.',
        'Press "I\'m checking" so colleagues know someone has it.',
        'If someone goes to the floor, press "Mark: staff sent".',
        'When it is over, press "Record outcome", choose what happened and press "Save outcome".'],
      tips: ['Always record an outcome, including "False alarm". It keeps the false-alarm rate honest and improves the checks.',
        'Evidence is deleted oldest first. To keep it, open it with "Open in new tab" and save it elsewhere.'],
      go: 'loss', goLabel: 'Open Loss prevention',
    },
    {
      id: 'reading-figures', title: 'Reading Today and the figures',
      keywords: 'today dash not measured figures visitors dwell busiest sales numbers zero',
      summary: 'Every figure is measured by the cameras. Nothing is guessed.',
      steps: ['A dash means not measured yet; hover over it or read the line under it for the reason.',
        '"Visitors today" is exact when an entrance counting line is drawn; otherwise it is an estimate.',
        '"Sales today" needs the tills connected (Settings > Sales data).',
        'Today refreshes every 15 seconds.'],
      go: 'today', goLabel: 'Open Today',
    },
    {
      id: 'insights', title: 'Insights and recommendations',
      keywords: 'insights recommendations analysis report daily footfall journey lost sales',
      summary: 'What the analysis suggests you change, and the shopper figures behind it.',
      steps: ['Insights > Recommendations: read "Why" and "Do", then mark it "Done", "Mark reviewed" or "Dismiss".',
        'Insights > Shoppers & footfall: visitors by hour, entrances and shelf reaches.',
        'Insights > Daily report: today\'s summary; "Print / save PDF" to keep it.'],
      go: 'insights', goLabel: 'Open Insights',
    },
    {
      id: 'heatmaps', title: 'Heatmaps',
      keywords: 'heatmap walked stopped touched shelves history compare hot spots',
      summary: 'Where shoppers walk, stop and touch shelves.',
      steps: ['Store map: tick "Heatmap" and choose what to show and the period.',
        'Insights > Shoppers & footfall > "Heatmap history": choose "Where", the type and the time, or "Play day".'],
      tips: ['Heatmaps need calibrated cameras. Hatched hours were not recorded.'],
      go: 'insights-footfall', goLabel: 'Open heatmap history',
    },
    {
      id: 'accounts', title: 'Staff accounts and roles', role: 'admin',
      keywords: 'account user staff operator administrator owner role password reset add remove',
      summary: 'Give each person their own account. Staff get the Operator role.',
      steps: ['Settings > Accounts: press "Add account".',
        'Enter the "Username", choose the "Role" (Operator for staff) and an "Initial password", then "Create account".',
        'Give the person the password; they change it under Settings > "Your account".'],
      tips: ['Operators can watch cameras and review alerts but cannot change setup.',
        'Forgotten password: "Reset password" next to that account.'],
      go: 'settings:settings-accounts', goLabel: 'Open Accounts',
    },
    {
      id: 'store-settings', title: 'Store name and time zone', role: 'admin',
      keywords: 'store name time zone timezone device name header report',
      summary: 'The time zone decides what "today" and all schedules mean.',
      steps: ['Settings > Store: enter the "Store name".',
        'In "Time zone", type to search and pick the store\'s zone, for example Australia/Melbourne.',
        'Press "Save". It applies at once.'],
      go: 'settings:settings-store', goLabel: 'Open Store settings',
    },
    {
      id: 'evidence', title: 'Evidence storage and alert settings', role: 'admin',
      keywords: 'evidence storage disk limit days clip confidence repeat cooldown high value',
      summary: 'How much evidence is kept, and how sensitive alerts are.',
      steps: ['Settings > Evidence storage: the size limit and "Keep evidence for (days)".',
        'Settings > Alerts and detection: high-value categories, repeat times and the theft confidence.',
        'Each field shows its default; "Reset to default" puts it back.'],
      go: 'settings:settings-evidence', goLabel: 'Open Evidence storage',
    },
    {
      id: 'sub-streams', title: 'Upgrade recorder sub-streams', role: 'admin',
      keywords: 'cif d1 sub-stream resolution recorder dahua far away missed small picture',
      summary: 'Small CIF pictures miss far-away shoppers; D1 fixes that.',
      steps: ['Save the recorder sign-in first (Store map > "Dahua recorder" > "Save sign-in").',
        'Settings > Recorder sub-streams: choose the recorder and press "Check sub-streams".',
        'Press "Upgrade CIF sub-streams to D1" and confirm.'],
      tips: ['"Restore previous stream settings" undoes the change.'],
      go: 'settings:settings-recorder-streams', goLabel: 'Open Recorder sub-streams',
    },
    {
      id: 'online-access', title: 'Online access (away from the store)', role: 'admin',
      keywords: 'online remote access public address internet tunnel away home verify',
      summary: 'Open the dashboard and get phone alerts away from the store.',
      steps: ['Usually set up by the installer. Settings > Online access shows the status.',
        'It should read "Connected since ..." and the badge "Verified".',
        'From outside the store, open the public address and press "Check remote video".'],
      go: 'settings:settings-remote', goLabel: 'Open Online access',
    },
    {
      id: 'tills', title: 'Connect the tills (optional)', role: 'admin',
      keywords: 'pos point of sale till register sales revenue key',
      summary: 'Sales, buying rate and lost sales need till data.',
      steps: ['Settings > Sales data: read "How to connect your tills".',
        'Press "Create till key", copy it into the till system, then press "Done". It is shown only once.'],
      go: 'settings:settings-pos', goLabel: 'Open Sales data',
    },
    {
      id: 'backups', title: 'Backups', role: 'admin',
      keywords: 'backup restore download upload factory reset safe copy',
      summary: 'Backups stay on the device unless you download them.',
      steps: ['Settings > Backups and reset: press "Back up now".',
        'Press "Download" and keep the file somewhere safe, off the device.',
        'To go back to an earlier state, press "Restore" next to a backup. A safety backup is taken first.'],
      tips: ['Download a backup weekly and before any big change.'],
      go: 'settings:settings-backups', goLabel: 'Open Backups',
    },
    {
      id: 'ikorex-service', title: 'Your iKorex plan',
      keywords: 'ikorex plan licence license paused not included feature contact service',
      summary: 'What is included, and what "not included in your plan" means.',
      steps: ['Settings > iKorex service shows the plan, the licence end date, the camera limit and the features included.',
        'A feature not in your plan shows "not included in your plan". Contact iKorex to add it.',
        '"Service paused" means iKorex has paused the service; your settings and evidence are kept.'],
      tips: ['Losing the internet never pauses anything. Store video and pictures never leave this device.'],
      go: 'settings:settings-fleet', goLabel: 'Open iKorex service',
    },
    {
      id: 'troubleshooting', title: 'Something is not working',
      keywords: 'problem trouble wrong password offline no picture map empty counted twice no alerts sign in refused',
      summary: 'The most common problems and what to do.',
      steps: ['Camera tile says "WRONG PASSWORD": open the camera settings and enter the camera username and password again.',
        'Tile "OFFLINE": press "Reconnect"; check the recorder is on.',
        'People counted but the map is empty: place and calibrate the camera.',
        'Visitors counted twice: resolve the "added twice" banner on Cameras.',
        'No phone alerts: open Settings > Phone alerts and press "Send a test alert"; on iPhone use the Home Screen app.',
        '"Your account can\'t change setup": you are signed in as an Operator; ask an administrator.'],
      go: 'cameras', goLabel: 'Open Cameras',
    },
  ];

  window.EdgeHelpContent = Object.freeze({ SUPPORT, FEATURE_LABELS, topics, anchors, articles });
})();
