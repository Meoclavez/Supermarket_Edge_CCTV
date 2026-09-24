#!/usr/bin/env bash
# Start the edge CCTV backend: fixes the Python environment if needed, verifies
# inference on the best available accelerator, then execs uvicorn.
# All logic lives in edge_backend/scripts/bootstrap.py (`./run.sh --help`).
#
#   ./run.sh                     fix + verify + start on 0.0.0.0:8000
#   ./run.sh --check-only        fix + verify, do not start
#   ./run.sh --port 8766 -- --reload   (args after -- go to uvicorn)
#
# Venv: $EDGE_VENV or --venv; otherwise the existing .venv_test if present,
# else .venv (created on first run).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -z "${EDGE_VENV:-}" ]]; then
  if [[ -d "$ROOT/.venv_test" ]]; then EDGE_VENV="$ROOT/.venv_test"; else EDGE_VENV="$ROOT/.venv"; fi
  export EDGE_VENV
fi

# bootstrap.py needs only the standard library: any Python 3.10+ runs it.
PY="${EDGE_BOOTSTRAP_PYTHON:-}"
if [[ -z "$PY" ]]; then
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
  done
fi
if [[ -z "$PY" && -x "$EDGE_VENV/bin/python" ]]; then PY="$EDGE_VENV/bin/python"; fi
if [[ -z "$PY" ]]; then
  echo "python3 not found. Install it: sudo pacman -S python (Arch) / sudo apt install python3 python3-venv (Debian/Ubuntu)" >&2
  exit 1
fi

exec "$PY" "$ROOT/edge_backend/scripts/bootstrap.py" "$@"
