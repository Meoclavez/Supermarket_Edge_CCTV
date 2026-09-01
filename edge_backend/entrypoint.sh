#!/bin/bash
set -e

echo "=== Starting Universal Edge AI CCTV System ==="
python3 -c "from app.services.hardware_detector import hardware_profile; print(f'Detected Hardware: {hardware_profile.device_name} (Decoder: {hardware_profile.decoder_type}, AI: {hardware_profile.inference_backend})')"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
