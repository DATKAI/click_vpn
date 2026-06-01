#!/bin/bash
# Установка VPN Manager на Debian (без Docker)
# Использование: bash install.sh
set -e

REPO="https://github.com/DATKAI/click_vpn.git"
INSTALL_DIR="/opt/click-vpn"
SERVICE_NAME="click-vpn"
DATA_DIR="/var/lib/click-vpn"
VENV_DIR="$INSTALL_DIR/venv"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║          Click VPN — Установка           ║"
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

# Пробуем основной PyPI, при таймауте — зеркало
PIP="$VENV_DIR/bin/pip"
PIP_MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"

info "Установка Python пакетов..."
"$PIP" install --upgrade pip --timeout 30 -q 2>/dev/null || \
  "$PIP" install --upgrade pip --timeout 60 -q -i "$PIP_MIRROR"

"$PIP" install -r "$INSTALL_DIR/backend/requirements.txt" --timeout 30 -q || {
  warn "PyPI недоступен, используем зеркало..."
  "$PIP" install -r "$INSTALL_DIR/backend/requirements.txt" --timeout 60 -q -i "$PIP_MIRROR"
}

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
DATABASE_URL=sqlite:////var/lib/click-vpn/vpn.db
DATA_DIR=/var/lib/click-vpn
EOF
chmod 600 "$INSTALL_DIR/.env"

# ── systemd сервис ────────────────────────────────────────────────────────────
info "Создание systemd сервиса..."
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
# Права для управления OpenVPN и сетью
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW

[Install]
WantedBy=multi-user.target
EOF

# ── IP форвардинг ─────────────────────────────────────────────────────────────
info "Включение IP форвардинга..."
echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-click-vpn.conf
sysctl -p /etc/sysctl.d/99-click-vpn.conf -q

# ── Освобождаем порт 8080 если занят ─────────────────────────────────────────
if ss -tlnp | grep -q ':8080'; then
  warn "Порт 8080 занят — освобождаем..."
  PIDS=$(ss -tlnp | grep ':8080' | grep -oP 'pid=\K[0-9]+' | sort -u)
  for PID in $PIDS; do
    kill "$PID" 2>/dev/null && info "Завершён процесс $PID"
  done
  sleep 1
fi

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
echo "║  /opt/click-vpn/.env                     ║"
echo "╚══════════════════════════════════════════╝"
echo ""
