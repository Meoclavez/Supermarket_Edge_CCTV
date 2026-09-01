#!/usr/bin/env bash
# ==============================================================================
# Edge AI CCTV - One-Click Local PC Test Runner
# ==============================================================================
# Runs the full Edge AI CCTV pipeline locally on your computer using:
# • Your ESP32-S3 IP Camera (Wi-Fi or USB)
# • Local USB Webcam (/dev/video0)
# • Or Built-In Synthetic Benchmark Stream
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
VENV_DIR="$PROJECT_ROOT/.venv_test"

echo "======================================================="
echo "   🛡️ Edge AI CCTV Local Pipeline Benchmark & Test"
echo "======================================================="

cd "$PROJECT_ROOT"

# 1. Check or Create Virtual Environment
if [ ! -d "$VENV_DIR" ]; then
  echo "[+] Creating local Python virtual environment in $VENV_DIR..."
  python3 -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

# 2. Install / Verify Dependencies
echo "[+] Checking Python test dependencies..."
pip install --quiet --upgrade pip
pip install --quiet \
    opencv-python \
    numpy \
    pydantic \
    pydantic-settings \
    sqlalchemy \
    aiosqlite \
    fastapi \
    uvicorn \
    pyjwt \
    passlib \
    bcrypt \
    httpx || pip install --quiet opencv-python-headless

# 3. Parse Custom Stream URL or GUI Flag
IS_GUI=false
STREAM_URL=""

for arg in "$@"; do
  if [ "$arg" == "--gui" ] || [ "$arg" == "-g" ]; then
    IS_GUI=true
  else
    STREAM_URL="$arg"
  fi
done

if [ "$IS_GUI" = true ]; then
  echo "[+] Launching Real-Time Live AI Vision Monitor & Web HUD..."
  echo "    🌐 Access the Live CCTV Dashboard at: http://localhost:8080"
  if [ -n "$STREAM_URL" ]; then
    python3 "$PROJECT_ROOT/scripts/monitor_live_ai.py" --stream "$STREAM_URL"
  else
    python3 "$PROJECT_ROOT/scripts/monitor_live_ai.py"
  fi
else
  if [ -n "$STREAM_URL" ]; then
    echo "[+] Testing pipeline with custom stream: $STREAM_URL"
    python3 "$PROJECT_ROOT/scripts/test_local_system.py" --stream "$STREAM_URL" --duration 10
  else
    echo "[+] Testing pipeline with USB webcam / Synthetic generator..."
    echo "    (Tip: Run './scripts/run_local_test.sh --gui' for real-time visual monitor)"
    python3 "$PROJECT_ROOT/scripts/test_local_system.py" --duration 10
  fi
fi

echo "======================================================="
echo " ✅ Test completed! Recorded clips are saved in:"
echo "    • $PROJECT_ROOT/storage/clips/"
echo "    • $PROJECT_ROOT/storage/dvr/"
echo "======================================================="
