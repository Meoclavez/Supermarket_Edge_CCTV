# Store-offline watchdog: box <-> VPS contract (v1)

Owner decision, 2026-10-08: phones that receive alerts must also be told when a
store's box goes offline for more than 15 minutes. A box that is down cannot
send that alert itself, so the VPS sends it.

The VPS stays a connection broker. It holds **no push keys and no plaintext**:

- The box encrypts a ready-to-send "store offline" Web Push request for every
  alert phone (RFC 8291 aes128gcm, VAPID RFC 8292 signed by the box) and
  uploads the set ("bundle") every 30 minutes and whenever the phone list changes.
- When the store has not been seen on the tunnel for `offline_after_s`, the VPS
  POSTs each stored request unchanged to its push service, once per outage.
- When the box is back, it learns about the alert from the upload response and
  sends its own "back online" push.

## Upload (box -> VPS)

`POST https://<tunnel host>/watchdog/v1/bundle`. The tunnel host is the host of
the box's tunnel server URL, `wss://tunnel.ikorex.com.au` -> `tunnel.ikorex.com.au`.
Only `wss://` tunnel servers are supported.

Headers:

```
Content-Type: application/json
Authorization: Bearer <store token>      # the same token frpc logs in with
X-Store-Id: <store id>
```

Body (JSON, at most 256 KiB):

```json
{
  "version": 1,
  "store_id": "pearcedale",
  "enabled": true,
  "offline_after_s": 900,
  "messages": [
    {
      "endpoint": "https://web.push.apple.com/QH...",
      "headers": {
        "Authorization": "vapid t=<jwt>, k=<public key>",
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": "86400",
        "Urgency": "high",
        "Topic": "store-offline"
      },
      "body_b64": "<standard base64 of the encrypted body>",
      "not_after": 1791000000
    }
  ],
  "ack_alert_id": null
}
```

- `enabled: false` or an empty `messages` list switches the watchdog off for
  this store. The stored bundle is replaced.
- `offline_after_s` is clamped by the VPS to 300..86400.
- `messages` has at most 50 entries. Each decoded body is at most 4096 bytes.
- `endpoint` must be `https://` on a known push service, otherwise the whole
  upload is refused (400). Allowed hosts are `fcm.googleapis.com`,
  `*.push.apple.com`, `updates.push.services.mozilla.com`,
  `push.services.mozilla.com` and `*.notify.windows.com`. This stops the VPS
  being used to send requests anywhere else.
- `headers` may only contain `Authorization`, `Content-Encoding`,
  `Content-Type`, `TTL`, `Urgency` and `Topic` (case-insensitive). Anything
  else is refused (400).
- `not_after` is the unix time when the VAPID JWT expires (at most 24 h after
  signing). The VPS skips a message past `not_after`.
- `ack_alert_id`: the id of an offline alert the box has handled (sent its
  "back online" push for). The VPS then forgets that alert.

Responses:

| Status | Body | Meaning |
|---|---|---|
| 200 | `{"ok": true, "stored": <n>, "offline_alert": null \| {...}}` | stored |
| 400 | `{"ok": false, "error": "<reason>"}` | malformed or a rule above broken |
| 401 | `{"ok": false, "error": "unauthorized"}` | unknown store, wrong token or store disabled (one answer for all three) |
| 413 | `{"ok": false, "error": "too large"}` | body over 256 KiB |
| 503 | `{"ok": false, "error": "auth unavailable"}` | the VPS could not check the token |

`offline_alert` is the newest offline alert sent for this store that the box has
not acknowledged yet:

```json
{"id": "a1b2c3d4e5f6", "offline_since": 1790990000, "sent_at": 1790990900,
 "back_online_at": 1790994000, "sent": 2, "failed": 0}
```

`back_online_at` is null while the outage lasts. All times are unix seconds (UTC).

## Detection (VPS)

- "Last seen" is the store's last allowed Login or Ping, as `cctv-auth` reports
  it in `GET /status` (`last_seen`, `last_seen_age_s`). frpc pings every 30 s.
- The VPS checks every 30 s. A store is offline when `last_seen_age_s >=
  offline_after_s`, its bundle is enabled and has messages, and the store is
  enabled in the registry.
- Each outage alerts **once**. The outage ends when `last_seen_age_s` drops
  back under 90 s. Then `back_online_at` is set.
- No alert is sent when `cctv-auth` cannot be reached, or when the store has
  never been seen. A broken VPS must not wake everyone up.
- A push service answer of 404 or 410 drops that message from the stored
  bundle. The box re-uploads fresh subscriptions anyway.
- The state (bundles and alerts) is kept on disk, so a watchdog restart during
  an outage neither repeats nor loses the alert.

## Box side

- `edge_backend/app/services/offline_watchdog.py` builds and uploads the bundle.
  The recipients are every phone on the alert roster (first priority and
  backup), or the fallback set while the roster is incomplete.
- Settings -> Phone alerts: watchdog on/off and "offline for" minutes (default 15).
- On `offline_alert` with `back_online_at` set: send "<store> is back online
  (offline from HH:MM to HH:MM)" to the same phones, then acknowledge it with
  `ack_alert_id` on the next upload, which happens right away.
