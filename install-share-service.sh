#!/bin/bash
# Установка изолированного микросервиса раздачи временных ссылок.
# Запускается под отдельным непривилегированным пользователем clickvpn-share
# с доступом ТОЛЬКО к каталогу share/ — без доступа к БД/ключам.
# Использование: bash /opt/click-vpn/install-share-service.sh
set -e

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info(){ echo -e "${GREEN}[INFO]${NC} $1"; }
warn(){ echo -e "${YELLOW}[WARN]${NC} $1"; }
err(){ echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

[ "$EUID" -ne 0 ] && err "Запустите от root"

INSTALL_DIR="/opt/click-vpn"
DATA_DIR="/var/lib/click-vpn"
SHARE_DIR="${DATA_DIR}/share"
SHARE_USER="clickvpn-share"
PORT="${SHARE_PORT:-8081}"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   Микросервис раздачи ссылок — установка  ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Отдельный системный пользователь ──────────────────────────────────────────
if id "$SHARE_USER" >/dev/null 2>&1; then
  info "Пользователь $SHARE_USER уже существует"
else
  info "Создание пользователя $SHARE_USER..."
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SHARE_USER"
fi

# ── Каталог share/ с правами для сервиса ─────────────────────────────────────
mkdir -p "$SHARE_DIR"
chgrp "$SHARE_USER" "$SHARE_DIR"
chmod 2750 "$SHARE_DIR"     # setgid: новые файлы наследуют группу

# ── systemd-юнит ─────────────────────────────────────────────────────────────
info "Создание systemd-юнита click-vpn-share..."
cat > /etc/systemd/system/click-vpn-share.service <<EOF
[Unit]
Description=Click VPN — изолированный сервис раздачи ссылок
After=network.target

[Service]
Type=simple
User=${SHARE_USER}
Group=${SHARE_USER}
Environment=DATA_DIR=${DATA_DIR}
WorkingDirectory=${INSTALL_DIR}/backend
ExecStart=${INSTALL_DIR}/venv/bin/uvicorn share_service:app --host 127.0.0.1 --port ${PORT}
Restart=always
RestartSec=3

# Жёсткая изоляция: доступ только к share/, остальное только чтение
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadOnlyPaths=${INSTALL_DIR}
ReadWritePaths=${SHARE_DIR}
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable click-vpn-share >/dev/null 2>&1 || true
systemctl restart click-vpn-share
sleep 2

if systemctl is-active --quiet click-vpn-share; then
  info "Сервис click-vpn-share работает на 127.0.0.1:${PORT} ✓"
else
  err "Сервис не запустился — проверьте: journalctl -u click-vpn-share -n 30"
fi

# Проверка доступности
if curl -fs "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
  info "Health-check OK"
fi

echo ""
echo "════════════════════════════════════════════"
info "Готово! Микросервис раздачи изолирован:"
echo "  • пользователь:  ${SHARE_USER} (nologin, без доступа к БД/ключам)"
echo "  • доступ только к: ${SHARE_DIR}"
echo "  • слушает:        127.0.0.1:${PORT}"
echo ""
warn "Осталось пробросить наружу через nginx (только путь /s/):"
echo "  см. nginx-share.conf.example"
echo "  и укажите «Публичный адрес» в настройках панели."
echo "════════════════════════════════════════════"
