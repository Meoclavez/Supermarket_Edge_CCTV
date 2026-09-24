#!/usr/bin/env bash
# ==============================================================================
# Edge AI CCTV - local test runner
# ==============================================================================
# 1. ./run.sh --check-only fixes and verifies the Python environment (deps,
#    onnxruntime flavour, models, GPU provider) exactly as production startup does.
# 2. Then, depending on the arguments:
#      ./scripts/run_local_test.sh                 pipeline smoke test (USB webcam /dev/video0)
#      ./scripts/run_local_test.sh rtsp://...      pipeline smoke test against that stream
#      ./scripts/run_local_test.sh --serve         start the server + dashboard on :8000
#      ./scripts/run_local_test.sh --pytest        run the backend test suite
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Same venv choice as run.sh.
if [[ -z "${EDGE_VENV:-}" ]]; then
  if [[ -d "$PROJECT_ROOT/.venv_test" ]]; then EDGE_VENV="$PROJECT_ROOT/.venv_test"; else EDGE_VENV="$PROJECT_ROOT/.venv"; fi
  export EDGE_VENV
fi
PY="$EDGE_VENV/bin/python"

MODE=smoke
STREAM_URL=""
for arg in "$@"; do
  case "$arg" in
    --serve|--gui|-g) MODE=serve ;;
    --pytest)         MODE=pytest ;;
    *)                STREAM_URL="$arg" ;;
  esac
done

if [[ "$MODE" == "serve" ]]; then
  echo "[+] Dashboard: http://localhost:${PORT:-8000}/dashboard"
  exec "$PROJECT_ROOT/run.sh"
fi

"$PROJECT_ROOT/run.sh" --check-only $([[ "$MODE" == "pytest" ]] && echo --dev)

cd "$PROJECT_ROOT/edge_backend"
if [[ "$MODE" == "pytest" ]]; then
  exec "$PY" -m pytest tests -q
fi

SMOKE="$PROJECT_ROOT/scripts/test_local_system.py"
if [[ ! -f "$SMOKE" ]]; then
  echo "[!] $SMOKE not found; running the backend test suite instead."
  exec "$PY" -m pytest tests -q
fi
if [[ -n "$STREAM_URL" ]]; then
  exec "$PY" "$SMOKE" --stream "$STREAM_URL" --duration 10
fi
exec "$PY" "$SMOKE" --duration 10
