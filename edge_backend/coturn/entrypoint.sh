#!/bin/sh
# Coturn Docker Entrypoint
# Templates coturn.conf with environment variables before starting the server.
# This ensures COTURN_SECRET from .env / docker-compose is injected into the
# actual Coturn config file, keeping the backend (turn_service.py) and Coturn
# in sync with the same shared secret.

set -e

TEMPLATE="/etc/coturn/coturn.conf"
RENDERED="/tmp/coturn_rendered.conf"

# Default fallback if COTURN_SECRET is not set
export COTURN_SECRET="${COTURN_SECRET:-cctv_turn_super_secret_dynamic_key_change_me_in_prod}"

# envsubst replaces ${COTURN_SECRET} in the template with the actual value
envsubst '${COTURN_SECRET}' < "$TEMPLATE" > "$RENDERED"

echo "[coturn-entrypoint] Secret injected, starting Coturn server..."

exec turnserver -c "$RENDERED" "$@"
