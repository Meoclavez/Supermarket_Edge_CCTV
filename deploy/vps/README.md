> **Legacy, do not use (2026-09-29).** These templates forward live video
> (`/stream`) through the VPS and have no per-store authentication, which breaks
> the owner rule that the VPS is only a connection broker and camera video goes
> directly from the box to the viewer (see `docs/REMOTE_VIDEO_CONTRACT.md`).
> The maintained VPS package is `~/Projects-1/Server/cctv-tunnel`.

# Online access through your VPS (shared frps)

Each store's edge box runs `frpc` and connects **out** to one shared `frps`
on your VPS. Your existing reverse proxy serves:

| Hostname (example) | Proxy routes to | Used by |
|---|---|---|
| `tunnel.example.com` | `edge-frps:7000`, path `/~!frp` only (WebSocket) | every store's frpc |
| `<store>-cctv.example.com` (one per store) | `edge-frps:8080` | browsers and the phone app |

For example: tunnel server `wss://tunnel.ikorex.com.au`, first store ID
`pearcedale` at `pearcedale-cctv.ikorex.com.au`. The templates below use
`example.com`; nothing in them is specific to one installation.

frps publishes no port on the VPS: only the proxy reaches it, over its Docker
network. frps routes each dashboard request by `Host` down the tunnel of the
store that registered that name. frp is Apache-2.0; the image is the official
`fatedier/frps`, pinned to v0.71.0 by digest (the boxes' frpc is the same
version).

**Store identity.** A box logs in with its **store ID** (frp `user`) and
**store token** (login metadata `metas.token`). A server plugin, the *store
check*, verifies the pair on `Login` and allows each store only its own
hostname(s) on `NewProxy`. It is **required** as soon as more than one store
connects: without it, any box that can log in could claim another store's
name. frp's shared `auth.token` is optional (*Server key* in the box's
Settings). The store check itself is a separate component; enable it in
`frps.toml` (`[[httpPlugins]]`, ops `Login` and `NewProxy`).

## Files

| File | What it is |
|---|---|
| `docker-compose.yml` | the shared frps container (non-root, read-only, no capabilities, no published ports) |
| `frps.toml` | frps config: optional shared token from the environment, store-check plugin hook, HTTP vhosts only, no dashboard |
| `frps.env.example` | proxy network, tunnel host, optional shared token, Traefik names |
| `proxies/traefik.compose.yml` | Traefik labels (one router for all store names) |
| `proxies/nginx.conf` | nginx server blocks (WebSocket upgrade for the tunnel host, regex for store names) |
| `proxies/Caddyfile` | Caddy site blocks |

## Set up the server (once)

1. **DNS:** `tunnel.<domain>` and each `<store>-cctv.<domain>` (or a wildcard
   `*.<domain>`) point at the VPS. Behind a CDN such as Cloudflare's proxy,
   keep store names one level deep (`<store>-cctv.<domain>`, not
   `cctv.<store>.<domain>`): the CDN's free certificate covers one level.
2. **Files:**

   ```bash
   sudo mkdir -p /opt/edge-frps && cd /opt/edge-frps
   # copy docker-compose.yml, frps.toml, frps.env.example and proxies/ from deploy/vps/
   cp frps.env.example frps.env && chmod 600 frps.env
   docker network ls             # PROXY_NETWORK = the network your proxy container is on
   nano frps.env                 # PROXY_NETWORK, TUNNEL_HOST (+ FRP_AUTH_TOKEN if you want one)
   nano frps.toml                # enable [[httpPlugins]] for the store check
   ```
3. **Route the hostnames** with your proxy (next section), then start frps:

   ```bash
   docker compose --env-file frps.env -p edge-frps up -d          # nginx / Caddy / NPM
   docker compose --env-file frps.env -p edge-frps \
     -f docker-compose.yml -f proxies/traefik.compose.yml up -d    # Traefik
   docker logs edge-frps         # "frps started successfully"
   ```
4. Check: `https://tunnel.<domain>/` returns 404 (only `/~!frp` is routed).

## Add a store

1. Issue a **store ID** (e.g. `store1`) and a **store token**
   (`openssl rand -hex 32`), and register them with the allowed hostname
   `store1-cctv.<domain>` in the store check.
2. DNS for `store1-cctv.<domain>` (unless a wildcard covers it). Traefik (regex
   router) and the nginx regex block need no change; Caddy needs one more site
   block; NPM one more proxy host.
3. On the box: `sudo EDGE_TUNNEL=1 bash /opt/edge-cctv/deploy/install.sh`, then
   **Settings → Online access**: Public address `store1-cctv.<domain>`, Tunnel
   server `wss://tunnel.<domain>`, Store ID, Store token, Server key (only if
   `FRP_AUTH_TOKEN` is set), *Proxies in front of your server* = One if a CDN
   proxy sits in front of the VPS, Enable, Save. Expect *Connected since …*,
   then press **Verify now**.

The box reports a refused store login, a refused hostname, a wrong server key
and connection problems as different errors.

## Reverse proxy

Requirements for any proxy: HTTPS with your usual certificates; the dashboard
names forward `Host` unchanged, **append the visitor to `X-Forwarded-For`**
(the box takes the visitor's address from that position, for lockouts; with a
CDN in front, set *Proxies in front of your server* on each box), pass
WebSocket upgrades, and do not buffer responses (live video is a stream). The
proxy must connect straight to frps.

- **Traefik** (v3, Docker provider): `proxies/traefik.compose.yml`. Set
  `PUBLIC_HOST_REGEX`, `TRAEFIK_ENTRYPOINT`, `TRAEFIK_CERTRESOLVER` in
  `frps.env`. Per-name certificates need an HTTP-01/TLS-ALPN resolver (not
  possible behind a CDN proxy) or a wildcard via a DNS-challenge resolver.
  WebSockets and `X-Forwarded-For` work by default; a rate limit is included.
- **nginx** (container or host): adapt `proxies/nginx.conf`; one server block
  matches every `<store>-cctv` name. Certificate: a wildcard (certbot with a
  DNS plugin) or the CDN's origin certificate. `nginx -t && nginx -s reload`.
- **Caddy**: `proxies/Caddyfile`; one block per store (Caddy obtains each
  certificate), or a wildcard site with a DNS-challenge module.
- **Nginx Proxy Manager**: frps must be on NPM's network (`PROXY_NETWORK`).
  Proxy host `tunnel.<domain>` → `http` `edge-frps` `7000`, **Websockets
  Support** on, SSL: Let's Encrypt, **Force SSL**, HTTP/2. Per store: proxy
  host `<store>-cctv.<domain>` → `http` `edge-frps` `8080`, **Websockets
  Support** and **Block Common Exploits** on, same SSL; *Advanced*:
  `proxy_buffering off; proxy_read_timeout 1h; client_max_body_size 50m;`

If nginx or Caddy runs on the host instead of in Docker, publish frps on
loopback only and use `127.0.0.1:7000` / `127.0.0.1:8080` as upstreams:

```yaml
# docker-compose.override.yml
services:
  frps:
    ports: ["127.0.0.1:7000:7000", "127.0.0.1:8080:8080"]
```

## Security checklist

- [ ] The store check is enabled before a second store connects, and locks
      each store ID to its own hostname(s).
- [ ] Store tokens: `openssl rand -hex 32`, one per store; stored encrypted on
      the box (write-only in Settings). Revoke a store in the store check.
- [ ] Optional shared `FRP_AUTH_TOKEN` only in `frps.env` (`chmod 600`).
- [ ] `docker ps` shows no published ports for `edge-frps` (unless you chose
      the loopback variant above).
- [ ] frps is only on the proxy network; its dashboard stays off (no
      `webServer`); TCP/UDP forwarding is disabled (`allowPorts`).
- [ ] Rate limits on the dashboard names (Traefik middleware / nginx
      `limit_req` included; Caddy needs a rate-limit plugin). The box also
      locks out repeated failed sign-ins per visitor address.
- [ ] Optional second factor in front: basic-auth or your SSO (Authelia,
      Authentik, oauth2-proxy) on the dashboard names. Leave `/api/*` and
      `/stream` without it if the phone app must connect through them (they
      still need the app's token).
- [ ] fail2ban: use the proxy's access log (frps behind the proxy does not
      see visitor addresses). Ban on repeated 401/403/429 for dashboard names
      and on floods of `/~!frp` requests to the tunnel host.
- [ ] First-run setup never works through these names; create the operator
      account on the store network.
- [ ] Keep frps and the boxes' frpc on the same frp version (pinned in
      `docker-compose.yml` and `edge_backend/scripts/bootstrap.py`).
