#!/bin/sh
# Stand-in for `frpc` (fatedier/frp) in tests and local UI checks. Never
# contacts a server. Records its argv, the NAMES (never values) of its
# environment variables and a copy of the config it was given, then prints the
# log lines of a real frpc 0.71 that logs in and starts one proxy.
#   FAKE_FRPC_DIR       where args.log / envkeys.log / config.toml / count go
#   FAKE_FRPC_CRASHES      the server's store check refuses the login on the first N
#                          starts ("invalid store token"), exit 1
#   FAKE_FRPC_PROXY_ERROR  log "start error: <value>" instead of success (e.g.
#                          "router config conflict", or a plugin's rejection)
dir="${FAKE_FRPC_DIR:-${TMPDIR:-/tmp}}"
mkdir -p "$dir"
if [ "$1" = "--version" ]; then echo "0.71.0"; exit 0; fi
echo "$*" >> "$dir/args.log"
env | cut -d= -f1 | sort | tr '\n' ' ' >> "$dir/envkeys.log"; echo >> "$dir/envkeys.log"
[ "$1" = "-c" ] && [ -f "$2" ] && cp "$2" "$dir/config.toml"
n=$(cat "$dir/count" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$dir/count"
ts() { date '+%Y-%m-%d %H:%M:%S.000'; }
echo "$(ts) [I] [sub/root.go:194] start frpc service for config file [$2] with aggregated configuration"
echo "$(ts) [I] [client/service.go:312] try to connect to server..."
if [ "$n" -le "${FAKE_FRPC_CRASHES:-0}" ]; then
  echo "$(ts) [W] [client/service.go:323] connect to server error: invalid store token"
  echo "login to the server failed: invalid store token. With loginFailExit enabled, no additional retries will be attempted"
  exit 1
fi
sleep 0.2
echo "$(ts) [I] [client/service.go:332] [e54b32da31363efa] login to server success, get run id [e54b32da31363efa]"
echo "$(ts) [I] [proxy/proxy_manager.go:183] [e54b32da31363efa] proxy added: [store1-cctv]"
if [ -n "${FAKE_FRPC_PROXY_ERROR:-}" ]; then
  echo "$(ts) [W] [client/control.go:172] [e54b32da31363efa] [store1-cctv] start error: $FAKE_FRPC_PROXY_ERROR"
else
  echo "$(ts) [I] [client/control.go:174] [e54b32da31363efa] [store1-cctv] start proxy success"
fi
trap 'exit 0' TERM INT
while true; do sleep 0.2; done
