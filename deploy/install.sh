#!/usr/bin/env bash
# One-command install / update of Edge AI CCTV as a systemd service
# (docs/DEPLOYMENT.md sections 2-4). Safe to re-run: every step checks before
# acting, so a second run on an installed box pulls the latest code, re-checks
# the environment and restarts the service.
#
#   sudo bash deploy/install.sh
#   curl -fsSL <raw url of this file> | sudo bash
#
# Environment (all optional):
#   EDGE_REPO_URL     git remote to clone (default: the project's GitHub repo)
#   EDGE_ORT          onnxruntime flavour for bootstrap.py --ort (default: auto:
#                     NVIDIA -> gpu; AMD GPU with ROCm (/dev/kfd) -> migraphx,
#                     which also pre-compiles the models for the GPU; else gpu,
#                     whose CPU provider runs without a GPU; cpu, openvino)
#   EDGE_MODELS_FROM  directory of pre-exported *.onnx models to copy into
#                     edge_backend/models/ first; unset means bootstrap.py
#                     verifies/exports them itself (scripts/fetch_models.py)
#
# The install path is fixed: deploy/edge-cctv.service hard-codes /opt/edge-cctv.
set -euo pipefail

REPO="${EDGE_REPO_URL:-https://github.com/Meoclavez/Supermarket_Edge_CCTV.git}"
DEST=/opt/edge-cctv
SVC_USER=edgecctv
PORT=8000
MODELS_FROM="${EDGE_MODELS_FROM:-}"

[ "$(id -u)" = 0 ] || { echo "Run with sudo: sudo bash $0" >&2; exit 1; }
for cmd in git python3 curl systemctl; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "$cmd is required but not installed" >&2; exit 1; }
done

step() { printf '\n== %s\n' "$*"; }
as_svc() { sudo -u "$SVC_USER" -H "$@"; }

step "1/8 service user '$SVC_USER' (no login shell, camera + GPU groups)"
# No --create-home: skeleton dotfiles would make $DEST non-empty and git clone would refuse it.
id "$SVC_USER" >/dev/null 2>&1 \
  || useradd --system --no-create-home --home-dir "$DEST" --shell /usr/sbin/nologin "$SVC_USER"
for grp in video render; do
  if getent group "$grp" >/dev/null; then usermod -aG "$grp" "$SVC_USER"; fi
done
install -d -o "$SVC_USER" -g "$SVC_USER" "$DEST"

step "2/8 code from $REPO into $DEST"
if [ -d "$DEST/.git" ]; then
  as_svc git -C "$DEST" pull --ff-only
elif [ -z "$(ls -A "$DEST")" ]; then
  as_svc git clone "$REPO" "$DEST"
else
  echo "$DEST exists, is not empty and is not a git checkout; move it aside and re-run." >&2
  exit 1
fi
as_svc git -C "$DEST" log -1 --format='HEAD %h %s'

step "3/8 models"
if [ -n "$MODELS_FROM" ]; then
  shopt -s nullglob
  staged=("$MODELS_FROM"/*.onnx)
  shopt -u nullglob
  [ ${#staged[@]} -gt 0 ] || { echo "EDGE_MODELS_FROM=$MODELS_FROM contains no *.onnx files" >&2; exit 1; }
  install -o "$SVC_USER" -g "$SVC_USER" -m 0644 "${staged[@]}" "$DEST/edge_backend/models/"
  echo "copied ${#staged[@]} model(s) from $MODELS_FROM"
else
  echo "EDGE_MODELS_FROM not set: bootstrap verifies and exports the models (step 4)"
fi

step "4/8 venv + dependencies (onnxruntime flavour: ${EDGE_ORT:-auto})"
cd "$DEST"
# Stop the running service first: bootstrap may swap the onnxruntime build in
# the venv that process is using (e.g. onnxruntime-gpu -> the AMD MIGraphX
# stack), and on an AMD GPU it pre-compiles the models (1-3 min and ~2.5 GB per
# model the first time), which must not race a restarting service compiling
# the same models. If anything below fails, the trap starts it again.
was_active=false
if systemctl is-active --quiet edge-cctv; then
  was_active=true
  systemctl stop edge-cctv
fi
trap 'if [ "$was_active" = true ] && ! systemctl is-active --quiet edge-cctv; then systemctl start edge-cctv; fi' EXIT
# bootstrap exits 1 when a GPU is present but inference runs on the CPU, which is
# expected until the matching GPU build is installed. The gate is the service's
# own read-only preflight (its ExecStartPre), which treats that case as a warning.
if ! as_svc python3 edge_backend/scripts/bootstrap.py --check-only --ort "${EDGE_ORT:-auto}"; then
  echo "bootstrap reported NOT READY; running the service preflight as $SVC_USER to decide:"
  (cd "$DEST/edge_backend" && as_svc "$DEST/.venv/bin/python" -m app.services.preflight) \
    || { echo "preflight failed: stopping before the service is installed" >&2; exit 1; }
  echo "preflight passed (warnings only): continuing"
fi

step "5/8 .env (defaults: DEBUG=false, AUTH_DISABLED=false, secrets generated on first start)"
ENV_FILE="$DEST/edge_backend/.env"
[ -f "$ENV_FILE" ] || as_svc cp "$DEST/edge_backend/.env.example" "$ENV_FILE"
# STORAGE_DIR may be moved in .env (e.g. to a NAS); the setup code lives there.
STORAGE="$(sed -n 's/^[[:space:]]*STORAGE_DIR[[:space:]]*=[[:space:]]*//p' "$ENV_FILE" | tail -1 | tr -d "\"'")"
STORAGE="${STORAGE:-$DEST/storage}"

step "6/8 file permissions (database, secrets and .env are not world-readable)"
# The database holds operator password hashes and camera credentials. The unit's
# UMask=0027 covers new files; this tightens what earlier installs left 0644/0755.
install -d -o "$SVC_USER" -g "$SVC_USER" -m 0750 "$STORAGE"
for dir in "$DEST/storage" "$STORAGE"; do
  [ -d "$dir" ] || continue
  chmod 0750 "$dir"
  chmod -R o-rwx "$dir"
done
chown "$SVC_USER:$SVC_USER" "$ENV_FILE"
chmod 0640 "$ENV_FILE"

step "7/8 systemd service"
install -m 0644 "$DEST/deploy/edge-cctv.service" /etc/systemd/system/edge-cctv.service
systemctl daemon-reload
systemctl enable edge-cctv
systemctl restart edge-cctv

step "8/8 firewall"
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
  if ip link show tailscale0 >/dev/null 2>&1; then
    ufw allow in on tailscale0 to any port "$PORT" proto tcp comment 'edge-cctv dashboard via tailscale'
  else
    echo "no tailscale0 interface: dashboard rule not added (the store LAN needs its own rule for port $PORT)"
  fi
  if command -v tailscale >/dev/null 2>&1; then
    # Lets Tailscale peers connect directly instead of through a DERP relay.
    ufw allow 41641/udp comment 'tailscale direct connections'
  fi
else
  echo "ufw not installed or not active: no firewall rules changed"
fi

step "waiting for the service to answer"
code=none
for _ in $(seq 1 90); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/v1/health" || true)
  [ "$code" = 200 ] && break
  sleep 2
done
systemctl --no-pager --lines=0 status edge-cctv | head -5 || true
echo "health: HTTP $code"
journalctl -u edge-cctv -b --no-pager | grep -E 'INSECURE CONFIGURATION|ERROR|provider' | tail -10 || true

# First-run setup code: issued at service start while no operator account
# exists, written to <STORAGE_DIR>/setup_code.txt (0600, owned by the service
# user; readable here because this runs as root) and logged to the journal.
admin_exists=unknown
status_json=$(curl -s "http://127.0.0.1:$PORT/api/v1/auth/status" || true)
if [ -n "$status_json" ]; then
  admin_exists=$(printf '%s' "$status_json" \
    | python3 -c 'import json,sys; print(str(json.load(sys.stdin).get("admin_exists")).lower())' 2>/dev/null \
    || echo unknown)
fi

setup_code=""
if [ "$admin_exists" != true ]; then
  code_file="$STORAGE/setup_code.txt"
  for _ in $(seq 1 30); do
    [ -s "$code_file" ] && break
    sleep 1
  done
  [ -s "$code_file" ] && setup_code=$(tr -d '[:space:]' < "$code_file")
  if [ -z "$setup_code" ]; then
    setup_code=$(journalctl -u edge-cctv -b --no-pager 2>/dev/null \
      | grep -oE 'FIRST-RUN SETUP CODE: +[A-Z0-9]{4}-[A-Z0-9]{4}' | tail -1 | awk '{print $NF}' || true)
  fi
fi

MANAGE="sudo -u $SVC_USER -H bash -c 'cd $DEST/edge_backend && ../.venv/bin/python scripts/manage_operator.py reset-setup'"
echo
if [ -n "$setup_code" ]; then
  bar='================================================================'
  printf '%s\n  FIRST-RUN SETUP CODE:  %s\n%s\n' "$bar" "$setup_code" "$bar"
  echo "  No operator account exists yet. Open the dashboard on the store network"
  echo "  and enter this code in 'Create the operator account'. It is single use."
  echo "  Stored in:  $STORAGE/setup_code.txt (mode 0600, $SVC_USER only)"
  echo "  Journal:    journalctl -u edge-cctv | grep -A1 'FIRST-RUN SETUP CODE'"
  echo "  Lost it?    sudo systemctl restart edge-cctv   (logs it again; a new one if the file was deleted)"
elif [ "$admin_exists" = true ]; then
  echo "An operator account already exists; no setup code is needed."
  echo "Locked out? $MANAGE"
  echo "  (deletes all operator accounts and prints a new setup code)"
else
  echo "Could not read the first-run setup code (service not answering yet?). Look for it with:"
  echo "  sudo cat $STORAGE/setup_code.txt"
  echo "  journalctl -u edge-cctv | grep -A1 'FIRST-RUN SETUP CODE'"
fi

echo
echo "Dashboard:"
for ip in $(hostname -I 2>/dev/null); do
  case "$ip" in *:*) continue ;; esac
  echo "  http://$ip:$PORT/dashboard"
done
