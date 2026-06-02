#!/bin/bash
# Отключает HTTPS, возвращает панель на HTTP :8080.
# Использование: bash /opt/click-vpn/disable-https.sh
set -e

INSTALL_DIR="/opt/click-vpn"
VENV_DIR="$INSTALL_DIR/venv"
SERVICE_NAME="click-vpn"

GREEN='\033[0;32m'; RED='\033[0;31m'; NC='\033[0m'
info(){ echo -e "${GREEN}[INFO]${NC} $1"; }
err(){ echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

[ "$EUID" -ne 0 ] && err "Запустите от root"

info "Возврат на HTTP :8080..."
cat > /etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=Click VPN
After=network.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/.env
Environment=PYTHONPATH=${INSTALL_DIR}/backend
ExecStart=${VENV_DIR}/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl restart "$SERVICE_NAME"

IP=$(hostname -I | awk '{print $1}')
info "Готово. Панель снова на http://${IP}:8080"
