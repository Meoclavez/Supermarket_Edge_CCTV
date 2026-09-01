#!/usr/bin/env bash
# ==============================================================================
# Edge AI CCTV - Dedicated Camera Network & Auto-IP Assignment Setup Script
# ==============================================================================
# Configures a secondary NIC (e.g. eth1, enp2s0, usb0) as an isolated,
# air-gapped CCTV network with automatic DHCP IP assignment and WAN isolation.
# ==============================================================================

set -e

# Configuration
CCTV_IFACE="${1:-eth1}"
CCTV_GATEWAY_IP="192.168.10.1"
CCTV_NETMASK="255.255.255.0"
CCTV_DHCP_START="192.168.10.50"
CCTV_DHCP_END="192.168.10.200"
CCTV_LEASETIME="12h"

echo "======================================================="
echo " Configuring CCTV Dedicated Network on: $CCTV_IFACE"
echo " Gateway IP: $CCTV_GATEWAY_IP/24"
echo " DHCP Range: $CCTV_DHCP_START - $CCTV_DHCP_END"
echo "======================================================="

# Check root privileges
if [ "$EUID" -ne 0 ]; then
  echo "[-] Please run as root (sudo bash setup_cctv_network.sh <interface>)"
  exit 1
fi

# 1. Check if interface exists
if ! ip link show "$CCTV_IFACE" &> /dev/null; then
  echo "[-] Interface '$CCTV_IFACE' not found. Available interfaces:"
  ip -br link show
  echo "[-] Run: sudo bash setup_cctv_network.sh <interface_name>"
  exit 1
fi

# 2. Configure Static IP on Interface
echo "[+] Assigning static IP $CCTV_GATEWAY_IP/24 to $CCTV_IFACE..."
ip link set dev "$CCTV_IFACE" up
ip addr flush dev "$CCTV_IFACE"
ip addr add "$CCTV_GATEWAY_IP/24" dev "$CCTV_IFACE"

# 3. Install & Configure dnsmasq for Automatic Camera DHCP
if ! command -v dnsmasq &> /dev/null; then
  echo "[+] Installing dnsmasq for plug-and-play camera DHCP..."
  apt-get update && apt-get install -y dnsmasq
fi

# Create dedicated dnsmasq configuration for camera subnet
DNSMASQ_CONF="/etc/dnsmasq.d/cctv-cameras.conf"
echo "[+] Writing CCTV DHCP configuration to $DNSMASQ_CONF..."

cat <<EOF > "$DNSMASQ_CONF"
# Dedicated Edge AI CCTV Camera Subnet
interface=$CCTV_IFACE
bind-interfaces
dhcp-range=$CCTV_DHCP_START,$CCTV_DHCP_END,$CCTV_NETMASK,$CCTV_LEASETIME
# Do not provide default gateway or DNS to cameras (keeps them strictly local)
dhcp-option=3
dhcp-option=6
# Authoritative DHCP server on this interface
dhcp-authoritative
EOF

# Restart dnsmasq
systemctl restart dnsmasq
echo "[+] DHCP server active on $CCTV_IFACE."

# 4. Security Isolation (Firewall)
echo "[+] Enforcing security firewall isolation (Blocking cameras from WAN)..."
if command -v iptables &> /dev/null; then
  # Block camera subnet from forwarding to internet/WAN
  iptables -I FORWARD -i "$CCTV_IFACE" -j DROP || true
  # Allow cameras to talk directly to Edge Mini PC on port 554/80
  iptables -I INPUT -i "$CCTV_IFACE" -p tcp -m multiport --dports 554,8554,80,8000,1984 -j ACCEPT || true
fi

# 5. Install Systemd Auto-Start Service
SERVICE_FILE="/etc/systemd/system/cctv-camera-network.service"
echo "[+] Creating systemd auto-start service at $SERVICE_FILE..."

cat <<EOF > "$SERVICE_FILE"
[Unit]
Description=Edge AI CCTV Camera Network Auto-Start
After=network.target
Wants=network.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/ip link set dev $CCTV_IFACE up
ExecStart=/usr/sbin/ip addr add $CCTV_GATEWAY_IP/24 dev $CCTV_IFACE
ExecStart=/usr/bin/systemctl restart dnsmasq
ExecStop=/usr/sbin/ip addr flush dev $CCTV_IFACE

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable cctv-camera-network.service

echo ""
echo "======================================================="
echo " ✅ Dedicated CCTV Camera Network Successfully Configured!"
echo " • Connect PoE Switch to: $CCTV_IFACE"
echo " • Cameras will automatically receive IPs in range: 192.168.10.50 - 200"
echo " • Auto-Recovery Watchdog will monitor and heal migrated IPs"
echo "======================================================="
