#!/usr/bin/env bash
# ==============================================================================
# Edge AI CCTV - Automated Systemd Service Installer
# Configures 24/7 auto-restart, boot startup, and direct hardware driver access
# ==============================================================================

set -e

CURRENT_USER=$(whoami)
PROJECT_DIR="/home/meoclavezz/Projects-1/Edge_AI_CCTV"
VENV_PYTHON="/home/meoclavezz/.venv/bin/python"
SERVICE_FILE="/etc/systemd/system/edge-cctv.service"

echo "🛡️ Installing Edge AI CCTV Systemd Service for user: $CURRENT_USER"

# 1. Ensure user is in necessary hardware groups for direct driver access
echo "🔧 Adding $CURRENT_USER to video, render, and dialout hardware groups..."
sudo usermod -aG video,render,dialout "$CURRENT_USER" || true

# 2. Create Systemd Service Unit
echo "📝 Writing $SERVICE_FILE..."
sudo bash -c "cat <<EOF > $SERVICE_FILE
[Unit]
Description=Edge AI CCTV Surveillance Core & Studio
After=network.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=$CURRENT_USER
Group=$CURRENT_USER
WorkingDirectory=$PROJECT_DIR/edge_backend
ExecStart=$VENV_PYTHON -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
Restart=always
RestartSec=3s
KillMode=mixed
TimeoutStopSec=10

# Hardware Acceleration & Real-time Priority
LimitNOFILE=65536
Environment=PYTHONUNBUFFERED=1
Environment=PATH=/home/meoclavezz/.venv/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=multi-user.target
EOF"

# 3. Reload Systemd and Enable Service
echo "🔄 Reloading systemd daemon..."
sudo systemctl daemon-reload
sudo systemctl enable edge-cctv.service

echo ""
echo "=========================================================================="
echo "✅ Edge AI CCTV Service Installed Successfully!"
echo "=========================================================================="
echo "Commands to manage your service:"
echo "  ▶️  Start:       sudo systemctl start edge-cctv"
echo "  ⏹️  Stop:        sudo systemctl stop edge-cctv"
echo "  🔄  Restart:     sudo systemctl restart edge-cctv"
echo "  📊  Status:      sudo systemctl status edge-cctv"
echo "  📋  Live Logs:   journalctl -u edge-cctv -f"
echo "=========================================================================="
