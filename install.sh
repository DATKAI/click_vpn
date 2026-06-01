#!/bin/bash
# Установка VPN Manager на Debian
# Использование: bash install.sh
set -e

REPO="https://github.com/DATKAI/click_vpn.git"
INSTALL_DIR="/opt/vpn-manager"
SERVICE_NAME="vpn-manager"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
error()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║         VPN Manager — Установка          ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# Root check
[ "$EUID" -ne 0 ] && error "Запустите от root: sudo bash install.sh"

# Debian check
[ ! -f /etc/debian_version ] && error "Скрипт только для Debian"

# ── Зависимости ───────────────────────────────────────────────────────────────
info "Обновление пакетов..."
apt-get update -qq

info "Установка зависимостей..."
apt-get install -y -qq git curl ca-certificates gnupg openssl iptables

# ── Docker ────────────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
  info "Установка Docker..."
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
  systemctl enable docker --quiet
  systemctl start docker
  info "Docker установлен: $(docker --version)"
else
  info "Docker уже установлен: $(docker --version)"
fi

# ── Клонирование репозитория ──────────────────────────────────────────────────
if [ -d "$INSTALL_DIR" ]; then
  warn "Директория $INSTALL_DIR уже существует."
  warn "Для обновления используйте: bash $INSTALL_DIR/update.sh"
  exit 0
fi

info "Клонирование репозитория..."
git clone "$REPO" "$INSTALL_DIR" -q
cd "$INSTALL_DIR"

# ── Генерация .env ────────────────────────────────────────────────────────────
if [ ! -f .env ]; then
  info "Генерация .env..."
  SECRET_KEY=$(openssl rand -hex 32)
  ADMIN_PASSWORD=$(openssl rand -base64 12 | tr -d '/+=')

  cat > .env <<EOF
SECRET_KEY=${SECRET_KEY}
ADMIN_PASSWORD=${ADMIN_PASSWORD}
TOKEN_EXPIRE_MINUTES=480
DATABASE_URL=sqlite:////data/vpn.db
DATA_DIR=/data
EOF
  chmod 600 .env
fi

# Считываем пароль для вывода в конце
ADMIN_PASSWORD=$(grep ADMIN_PASSWORD .env | cut -d= -f2)

# ── Запуск ────────────────────────────────────────────────────────────────────
info "Сборка и запуск контейнера..."
docker compose up -d --build

# ── Ждём поднятия ─────────────────────────────────────────────────────────────
info "Ожидание запуска сервиса..."
for i in $(seq 1 30); do
  if curl -sf http://localhost:8080 &>/dev/null; then
    break
  fi
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
echo "║  Пароль сохранён в /opt/vpn-manager/.env║"
echo "║  Обновление: bash /opt/vpn-manager/      ║"
echo "║              update.sh                   ║"
echo "╚══════════════════════════════════════════╝"
echo ""
