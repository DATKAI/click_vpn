#!/bin/bash
# Полное удаление VPN Manager
# Использование: bash /opt/vpn-manager/uninstall.sh
set -e

INSTALL_DIR="/opt/vpn-manager"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║         VPN Manager — Удаление           ║"
echo "╚══════════════════════════════════════════╝"
echo ""

[ "$EUID" -ne 0 ] && error "Запустите от root: sudo bash uninstall.sh"

# ── Подтверждение ─────────────────────────────────────────────────────────────
warn "Будет удалено:"
echo "  - Docker контейнер vpn-manager"
echo "  - Docker образ vpn-manager"
echo "  - Docker volume с данными (БД, сертификаты, ключи)"
echo "  - Директория $INSTALL_DIR"
echo ""
read -p "Вы уверены? Все данные будут потеряны! [yes/N]: " CONFIRM
[ "$CONFIRM" != "yes" ] && echo "Отменено." && exit 0

echo ""

# ── Остановка и удаление контейнера ──────────────────────────────────────────
if [ -d "$INSTALL_DIR" ]; then
  cd "$INSTALL_DIR"
  if docker compose ps -q 2>/dev/null | grep -q .; then
    info "Остановка контейнера..."
    docker compose down
  else
    info "Контейнер уже остановлен"
  fi
else
  # Попытка остановить по имени если директория удалена
  docker stop vpn-manager 2>/dev/null && docker rm vpn-manager 2>/dev/null || true
fi

# ── Удаление образа ───────────────────────────────────────────────────────────
info "Удаление Docker образа..."
docker rmi vpn-manager-vpn-manager 2>/dev/null || true
docker rmi vpn-manager 2>/dev/null || true

# ── Удаление volume (данные: БД, PKI, сертификаты) ───────────────────────────
info "Удаление данных (volume)..."
docker volume rm vpn-manager_vpn-data 2>/dev/null || true

# ── Удаление директории ───────────────────────────────────────────────────────
if [ -d "$INSTALL_DIR" ]; then
  info "Удаление директории $INSTALL_DIR..."
  rm -rf "$INSTALL_DIR"
fi

# ── Чистка Docker ─────────────────────────────────────────────────────────────
info "Очистка неиспользуемых Docker ресурсов..."
docker system prune -f -q

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║           Удаление завершено!            ║"
echo "║                                          ║"
echo "║  Docker оставлен — он может              ║"
echo "║  использоваться другими сервисами.       ║"
echo "║  Для удаления Docker:                    ║"
echo "║  apt-get remove -y docker-ce             ║"
echo "╚══════════════════════════════════════════╝"
echo ""
