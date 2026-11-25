#!/usr/bin/env bash
# Install the gateway modules as systemd services (AIR-22).
#   sudo ./install.sh            # runs as the user who called sudo
#   sudo ./install.sh eiclite    # or name the user
# Then: systemctl status 'airraikordee@*'   journalctl -u airraikordee@brain.decision -f
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "run with sudo" >&2
  exit 1
fi
USER_NAME="${1:-${SUDO_USER:-}}"
if [ -z "$USER_NAME" ] || ! id "$USER_NAME" >/dev/null 2>&1; then
  echo "usage: sudo $0 <user that owns the gateway folder>" >&2
  exit 1
fi
HERE="$(cd "$(dirname "$0")" && pwd)"
GATEWAY_DIR="$(cd "$HERE/.." && pwd)"
UNIT_DIR=/etc/systemd/system

sed -e "s|@USER@|$USER_NAME|g" -e "s|@GATEWAY_DIR@|$GATEWAY_DIR|g" "$HERE/airraikordee@.service" \
  > "$UNIT_DIR/airraikordee@.service"
cp "$HERE/airraikordee.target" "$UNIT_DIR/airraikordee.target"

# serial (Node 1), I2C (BME280), camera, speaker
for g in dialout i2c video audio; do
  if getent group "$g" >/dev/null; then usermod -aG "$g" "$USER_NAME"; fi
done
mkdir -p /var/lib/airraikordee /var/log/airraikordee
chown "$USER_NAME" /var/lib/airraikordee /var/log/airraikordee

if [ -f /etc/airraikordee/.env ] && ! sudo -u "$USER_NAME" test -r /etc/airraikordee/.env; then
  echo "WARNING: $USER_NAME cannot read /etc/airraikordee/.env; for example:"
  echo "  sudo chgrp $USER_NAME /etc/airraikordee/.env && sudo chmod 640 /etc/airraikordee/.env"
fi

systemctl daemon-reload
enabled=()
while read -r module; do
  module="${module%%#*}"
  module="$(echo "$module" | tr -d '[:space:]')"
  [ -z "$module" ] && continue
  if [ ! -f "$GATEWAY_DIR/${module//.//}.py" ]; then
    echo "skip $module (no ${module//.//}.py in this checkout)"
    continue
  fi
  systemctl enable "airraikordee@$module.service" >/dev/null
  enabled+=("$module")
done < "$HERE/modules.txt"
systemctl enable airraikordee.target >/dev/null
systemctl restart airraikordee.target

echo "enabled: ${enabled[*]}"
echo "check:   systemctl status 'airraikordee@*' --no-pager"
