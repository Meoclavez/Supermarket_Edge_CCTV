#!/bin/sh
# Stand-in for `cloudflared` in tests and local UI checks. Never contacts
# Cloudflare. Records its argv and the NAMES (never values) of its environment
# variables, then behaves like a tunnel that registers one connection.
#   FAKE_CF_DIR      where args.log / envkeys.log / count are written
#   FAKE_CF_CRASHES  exit with an error on the first N starts
dir="${FAKE_CF_DIR:-${TMPDIR:-/tmp}}"
mkdir -p "$dir"
echo "$*" >> "$dir/args.log"
env | cut -d= -f1 | sort | tr '\n' ' ' >> "$dir/envkeys.log"; echo >> "$dir/envkeys.log"
if [ "$1" = "--version" ]; then echo "cloudflared version fake"; exit 0; fi
n=$(cat "$dir/count" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$dir/count"
if [ "$n" -le "${FAKE_CF_CRASHES:-0}" ]; then
  echo "2026-09-23T00:00:00Z ERR Provided Tunnel token is not valid (fake crash $n)"
  exit 1
fi
echo "2026-09-23T00:00:00Z INF Starting tunnel tunnelID=00000000-fake"
sleep 0.3
echo "2026-09-23T00:00:01Z INF Registered tunnel connection connIndex=0 connection=fake location=syd01 protocol=quic"
trap 'echo "2026-09-23T00:00:02Z INF Unregistered tunnel connection connIndex=0"; exit 0' TERM INT
while true; do sleep 0.2; done
