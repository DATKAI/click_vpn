#!/bin/bash
# Установка VPN Manager на Debian (без Docker)
# Использование: bash install.sh
set -e

REPO="https://github.com/DATKAI/click_vpn.git"
INSTALL_DIR="/opt/vpn-manager"
SERVICE_NAME="vpn-manager"
DATA_DIR="/var/lib/vpn-manager"
VENV_DIR="$INSTALL_DIR/venv"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║         VPN Manager — Установка          ║"
echo "╚══════════════════════════════════════════╝"
echo ""

[ "$EUID" -ne 0 ] && error "Запустите от root: bash install.sh"
[ ! -f /etc/debian_version ] && error "Скрипт только для Debian"

# Считаем установленным только если есть venv (не просто директория)
if [ -d "$VENV_DIR" ]; then
  warn "Уже установлен. Для обновления: bash $INSTALL_DIR/update.sh"
  exit 0
fi

# ── Пакеты ────────────────────────────────────────────────────────────────────
info "Обновление пакетов..."
apt-get update -qq

info "Установка зависимостей..."
apt-get install -y -qq \
  python3 python3-venv python3-pip \
  openvpn \
  git curl openssl \
  iptables iproute2

# ── Клонирование (если директории нет) ───────────────────────────────────────
if [ ! -d "$INSTALL_DIR" ]; then
  info "Клонирование репозитория..."
  git clone "$REPO" "$INSTALL_DIR" -q
else
  info "Директория уже существует, пропускаем клонирование"
fi

# ── Python venv ───────────────────────────────────────────────────────────────
info "Создание Python окружения..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$INSTALL_DIR/backend/requirements.txt"

# ── Директории данных ─────────────────────────────────────────────────────────
info "Создание директорий..."
mkdir -p "$DATA_DIR/pki" "$DATA_DIR/openvpn"

# ── .env ──────────────────────────────────────────────────────────────────────
info "Генерация конфигурации..."
SECRET_KEY=$(openssl rand -hex 32)
ADMIN_PASSWORD=$(openssl rand -base64 12 | tr -d '/+=')

cat > "$INSTALL_DIR/.env" <<EOF
SECRET_KEY=${SECRET_KEY}
ADMIN_PASSWORD=${ADMIN_PASSWORD}
TOKEN_EXPIRE_MINUTES=480
DATABASE_URL=sqlite:////var/lib/vpn-manager/vpn.db
DATA_DIR=/var/lib/vpn-manager
EOF
chmod 600 "$INSTALL_DIR/.env"

# ── systemd сервис ────────────────────────────────────────────────────────────
info "Создание systemd сервиса..."
cat > /etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=VPN Manager
After=network.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/.env
Environment=PYTHONPATH=${INSTALL_DIR}/backend
ExecStart=${VENV_DIR}/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# ── IP форвардинг ─────────────────────────────────────────────────────────────
info "Включение IP форвардинга..."
echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-vpn-manager.conf
sysctl -p /etc/sysctl.d/99-vpn-manager.conf -q

# ── Запуск ────────────────────────────────────────────────────────────────────
info "Запуск сервиса..."
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" --quiet
systemctl start "$SERVICE_NAME"

# ── Проверка ──────────────────────────────────────────────────────────────────
info "Ожидание запуска..."
for i in $(seq 1 30); do
  if curl -sf http://localhost:8080 &>/dev/null; then break; fi
  sleep 1
done

# ── Итог ──────────────────────────────────────────────────────────────────────
IP=$(hostname -I | awk '{print $1}')
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║          Установка завершена!            ║"
echo "╠══════════════════════════════════════════╣"
printf  "║  Адрес:   http://%-23s║\n" "${IP}:8080"
printf  "║  Логин:   %-30s║\n" "admin"
printf  "║  Пароль:  %-30s║\n" "${ADMIN_PASSWORD}"
echo "╠══════════════════════════════════════════╣"
echo "║  Пароль сохранён в:                      ║"
echo "║  /opt/vpn-manager/.env                   ║"
echo "╚══════════════════════════════════════════╝"
echo ""
