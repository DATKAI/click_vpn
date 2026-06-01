#!/bin/bash
# Полное удаление VPN Manager
# Использование: bash /opt/vpn-manager/uninstall.sh
set -e

INSTALL_DIR="/opt/vpn-manager"
SERVICE_NAME="vpn-manager"
DATA_DIR="/var/lib/vpn-manager"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║         VPN Manager — Удаление           ║"
echo "╚══════════════════════════════════════════╝"
echo ""

[ "$EUID" -ne 0 ] && error "Запустите от root: bash uninstall.sh"

warn "Будет удалено:"
echo "  - Сервис systemd vpn-manager"
echo "  - Директория $INSTALL_DIR (код, venv)"
echo "  - Директория $DATA_DIR (БД, сертификаты, ключи)"
echo "  - Конфиг IP форвардинга"
echo ""
read -p "Вы уверены? Все данные будут потеряны! [yes/N]: " CONFIRM
[ "$CONFIRM" != "yes" ] && echo "Отменено." && exit 0

echo ""

# ── Остановка сервиса ─────────────────────────────────────────────────────────
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
  info "Остановка сервиса..."
  systemctl stop "$SERVICE_NAME"
fi

if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
  info "Отключение автозапуска..."
  systemctl disable "$SERVICE_NAME" --quiet
fi

# ── Удаление systemd unit ─────────────────────────────────────────────────────
if [ -f "/etc/systemd/system/${SERVICE_NAME}.service" ]; then
  info "Удаление systemd unit..."
  rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
  systemctl daemon-reload
fi

# ── Удаление директорий ───────────────────────────────────────────────────────
if [ -d "$DATA_DIR" ]; then
  info "Удаление данных ($DATA_DIR)..."
  rm -rf "$DATA_DIR"
fi

if [ -d "$INSTALL_DIR" ]; then
  info "Удаление приложения ($INSTALL_DIR)..."
  rm -rf "$INSTALL_DIR"
fi

# ── IP форвардинг ─────────────────────────────────────────────────────────────
if [ -f /etc/sysctl.d/99-vpn-manager.conf ]; then
  info "Удаление конфига IP форвардинга..."
  rm -f /etc/sysctl.d/99-vpn-manager.conf
fi

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║           Удаление завершено!            ║"
echo "║                                          ║"
echo "║  openvpn оставлен (может использоваться  ║"
echo "║  другими сервисами).                     ║"
echo "║  Для удаления: apt remove openvpn        ║"
echo "╚══════════════════════════════════════════╝"
echo ""
