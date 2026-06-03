#!/bin/bash
# Подготовка сервера к сборке Windows-установщиков клиента:
#   - ставит NSIS (makensis)
#   - скачивает OpenVPN Community MSI в $DATA_DIR/assets/
# Использование: bash /opt/click-vpn/install-client-installer.sh
set -e

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info(){ echo -e "${GREEN}[INFO]${NC} $1"; }
warn(){ echo -e "${YELLOW}[WARN]${NC} $1"; }
err(){ echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

[ "$EUID" -ne 0 ] && err "Запустите от root"

# Версия OpenVPN Community (можно переопределить: OPENVPN_VERSION=2.6.x bash ...)
OPENVPN_VERSION="${OPENVPN_VERSION:-2.6.12-I001}"
OPENVPN_URL="https://swupdate.openvpn.org/community/releases/OpenVPN-${OPENVPN_VERSION}-amd64.msi"

DATA_DIR="${DATA_DIR:-/var/lib/click-vpn}"
ASSETS_DIR="${DATA_DIR}/assets"
BUNDLE="${ASSETS_DIR}/openvpn-installer.msi"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║  Windows-установщик клиента — подготовка  ║"
echo "╚══════════════════════════════════════════╝"
echo ""

info "Установка NSIS..."
apt-get update -qq
apt-get install -y -qq nsis curl

mkdir -p "$ASSETS_DIR"

if [ -f "$BUNDLE" ]; then
  info "Бандл OpenVPN уже есть: $BUNDLE ($(du -h "$BUNDLE" | cut -f1))"
else
  info "Скачивание OpenVPN ${OPENVPN_VERSION}..."
  if curl -fSL "$OPENVPN_URL" -o "$BUNDLE"; then
    info "Скачано: $(du -h "$BUNDLE" | cut -f1)"
  else
    rm -f "$BUNDLE"
    err "Не удалось скачать OpenVPN с $OPENVPN_URL
Скачайте MSI вручную с https://openvpn.net/community-downloads/ и положите как:
  $BUNDLE"
  fi
fi

# Проверка makensis
if command -v makensis >/dev/null 2>&1; then
  info "NSIS: $(makensis -VERSION 2>/dev/null || echo установлен)"
else
  err "makensis не найден после установки nsis"
fi

# Права на assets (сервис работает от root, но на всякий случай)
chmod 644 "$BUNDLE" 2>/dev/null || true

echo ""
echo "════════════════════════════════════════════"
info "Готово! Теперь в панели у OpenVPN-клиентов появится"
info "кнопка «Установщик Windows» (.exe с профилем + OpenVPN GUI)."
echo "════════════════════════════════════════════"
