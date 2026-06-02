#!/bin/bash
# Включает HTTPS для панели Click VPN (самоподписанный сертификат).
# Использование: bash /opt/click-vpn/enable-https.sh [порт]
set -e

INSTALL_DIR="/opt/click-vpn"
VENV_DIR="$INSTALL_DIR/venv"
SERVICE_NAME="click-vpn"
CERT_DIR="$INSTALL_DIR/certs"
PORT="${1:-8443}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info(){ echo -e "${GREEN}[INFO]${NC} $1"; }
warn(){ echo -e "${YELLOW}[WARN]${NC} $1"; }
err(){ echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

[ "$EUID" -ne 0 ] && err "Запустите от root"
[ ! -d "$INSTALL_DIR" ] && err "Click VPN не установлен"

IP=$(hostname -I | awk '{print $1}')
HOST=$(hostname)

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║         Click VPN — включение HTTPS      ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Генерация самоподписанного сертификата ────────────────────────────────────
mkdir -p "$CERT_DIR"
if [ -f "$CERT_DIR/server.crt" ]; then
  warn "Сертификат уже существует, пересоздаём..."
fi

info "Генерация сертификата для IP=$IP, host=$HOST ..."
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout "$CERT_DIR/server.key" \
  -out "$CERT_DIR/server.crt" \
  -subj "/CN=$IP" \
  -addext "subjectAltName=IP:$IP,DNS:$HOST,DNS:localhost,IP:127.0.0.1" 2>/dev/null

chmod 600 "$CERT_DIR/server.key"
info "Сертификат: $CERT_DIR/server.crt (действует 10 лет)"

# ── Переписываем systemd unit с TLS ───────────────────────────────────────────
info "Настройка systemd..."
cat > /etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=Click VPN
After=network.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/.env
Environment=PYTHONPATH=${INSTALL_DIR}/backend
ExecStart=${VENV_DIR}/bin/uvicorn backend.main:app --host 0.0.0.0 --port ${PORT} --ssl-keyfile ${CERT_DIR}/server.key --ssl-certfile ${CERT_DIR}/server.crt
Restart=always
RestartSec=5
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl restart "$SERVICE_NAME"

sleep 2
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║              HTTPS включён!              ║"
echo "╠══════════════════════════════════════════╣"
printf  "║  Адрес:  https://%-23s║\n" "${IP}:${PORT}"
echo "╠══════════════════════════════════════════╣"
echo "║  Браузер покажет предупреждение о         ║"
echo "║  самоподписанном сертификате — это норма  ║"
echo "║  для внутренней сети. Нажмите             ║"
echo "║  «Дополнительно → Перейти на сайт».       ║"
echo "║                                          ║"
echo "║  Чтобы убрать предупреждение — установите ║"
echo "║  сертификат server.crt в доверенные на    ║"
echo "║  рабочих машинах (см. инструкцию).        ║"
echo "║                                          ║"
echo "║  Откатить: bash disable-https.sh          ║"
echo "╚══════════════════════════════════════════╝"
echo ""
