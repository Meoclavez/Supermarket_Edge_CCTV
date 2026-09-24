#!/bin/sh
# Coturn Docker Entrypoint
# Templates coturn.conf with environment variables before starting the server.
# COTURN_SECRET comes from edge_backend/.env through docker-compose and is also
# passed to the edge_api container, so the backend (turn_service.py) and Coturn
# always use the same shared secret.
#
# There is deliberately no fallback value: a shared default secret would let
# anyone mint TURN credentials for every install. Generate one with:
#   python3 -c "import secrets; print(secrets.token_urlsafe(64))"

set -e

TEMPLATE="/etc/coturn/coturn.conf"
RENDERED="/tmp/coturn_rendered.conf"

if [ -z "${COTURN_SECRET}" ]; then
    echo "[coturn-entrypoint] COTURN_SECRET is not set; refusing to start. Set it in edge_backend/.env." >&2
    exit 1
fi
case "$(printf '%s' "${COTURN_SECRET}" | tr '[:upper:]' '[:lower:]')" in
    *change_me*|*changeme*|*change_in_prod*|*placeholder*)
        echo "[coturn-entrypoint] COTURN_SECRET is still a placeholder; refusing to start." >&2
        exit 1
        ;;
esac
export COTURN_SECRET

# Render to a file only this user can read.
umask 077
# envsubst replaces ${COTURN_SECRET} in the template with the actual value
envsubst '${COTURN_SECRET}' < "$TEMPLATE" > "$RENDERED"

echo "[coturn-entrypoint] Secret injected, starting Coturn server..."

exec turnserver -c "$RENDERED" "$@"
