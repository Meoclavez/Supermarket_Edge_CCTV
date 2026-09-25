#!/bin/sh
# Container / service entrypoint: read-only preflight, then uvicorn.
# The preflight logs every problem with its fix command. It aborts the start
# only on errors (missing packages, missing required model, unwritable
# storage); a GPU that is present but unusable is a warning and the service
# runs on the CPU. Set EDGE_PREFLIGHT_STRICT=0 to start even with errors.
set -eu
cd "$(dirname "$0")"
PY="${EDGE_PYTHON:-python3}"

echo "=== Edge AI CCTV: preflight ==="
if ! "$PY" -m app.services.preflight --session-probe; then
    if [ "${EDGE_PREFLIGHT_STRICT:-1}" = "1" ]; then
        echo "Preflight reported errors; not starting (EDGE_PREFLIGHT_STRICT=0 overrides)." >&2
        exit 1
    fi
    echo "Preflight reported errors; starting anyway (EDGE_PREFLIGHT_STRICT=0)." >&2
fi

exec "$PY" -m uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}" --workers 1 \
    --timeout-graceful-shutdown 5 --no-server-header
