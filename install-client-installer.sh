#!/bin/bash
# Подготовка сервера к сборке Windows-установщиков клиента:
#   - ставит NSIS (makensis)
#   - скачивает OpenVPN Community MSI в $DATA_DIR/assets/
# Использование: bash /opt/click-vpn/install-client-installer.sh
set -e

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
mkdir -p /etc/apt/apt.conf.d 2>/dev/null; echo 'APT::Sandbox::User "root";' > /etc/apt/apt.conf.d/99clickvpn 2>/dev/null || true
info(){ echo -e "${GREEN}[INFO]${NC} $1"; }
warn(){ echo -e "${YELLOW}[WARN]${NC} $1"; }
err(){ echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

[ "$EUID" -ne 0 ] && err "Запустите от root"

# Версия OpenVPN Community (можно переопределить: OPENVPN_VERSION=2.6.x bash ...)
OPENVPN_VERSION="${OPENVPN_VERSION:-2.6.12-I001}"
BASE_URL="https://swupdate.openvpn.org/community/releases"
URL_AMD64="${BASE_URL}/OpenVPN-${OPENVPN_VERSION}-amd64.msi"
URL_X86="${BASE_URL}/OpenVPN-${OPENVPN_VERSION}-x86.msi"

DATA_DIR="${DATA_DIR:-/var/lib/click-vpn}"
ASSETS_DIR="${DATA_DIR}/assets"
BUNDLE_AMD64="${ASSETS_DIR}/openvpn-installer-amd64.msi"
BUNDLE_X86="${ASSETS_DIR}/openvpn-installer-x86.msi"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║  Windows-установщик клиента — подготовка  ║"
echo "╚══════════════════════════════════════════╝"
echo ""

info "Установка NSIS..."
apt-get update -qq
apt-get install -y -qq nsis curl

mkdir -p "$ASSETS_DIR"

# 64-bit (обязательно)
if [ -f "$BUNDLE_AMD64" ]; then
  info "OpenVPN 64-bit уже есть ($(du -h "$BUNDLE_AMD64" | cut -f1))"
else
  info "Скачивание OpenVPN ${OPENVPN_VERSION} (64-bit)..."
  if curl -fSL "$URL_AMD64" -o "$BUNDLE_AMD64"; then
    info "Скачано 64-bit: $(du -h "$BUNDLE_AMD64" | cut -f1)"
  else
    rm -f "$BUNDLE_AMD64"
    err "Не удалось скачать 64-bit OpenVPN с $URL_AMD64
Скачайте MSI вручную с https://openvpn.net/community-downloads/ и положите как:
  $BUNDLE_AMD64"
  fi
fi

# 32-bit (для старых машин; не критично если недоступен)
if [ -f "$BUNDLE_X86" ]; then
  info "OpenVPN 32-bit уже есть ($(du -h "$BUNDLE_X86" | cut -f1))"
else
  info "Скачивание OpenVPN ${OPENVPN_VERSION} (32-bit)..."
  if curl -fSL "$URL_X86" -o "$BUNDLE_X86"; then
    info "Скачано 32-bit: $(du -h "$BUNDLE_X86" | cut -f1)"
  else
    rm -f "$BUNDLE_X86"
    warn "Не удалось скачать 32-bit OpenVPN — установщик будет только 64-bit.
Для поддержки 32-bit скачайте x86-MSI вручную и положите как:
  $BUNDLE_X86"
  fi
fi

# Чистим старый одиночный бандл (если был от прежней версии)
[ -f "${ASSETS_DIR}/openvpn-installer.msi" ] && rm -f "${ASSETS_DIR}/openvpn-installer.msi" || true

# Проверка makensis
if command -v makensis >/dev/null 2>&1; then
  info "NSIS: $(makensis -VERSION 2>/dev/null || echo установлен)"
else
  err "makensis не найден после установки nsis"
fi

# Права на assets (сервис работает от root, но на всякий случай)
chmod 644 "$BUNDLE_AMD64" 2>/dev/null || true
chmod 644 "$BUNDLE_X86" 2>/dev/null || true

echo ""
echo "════════════════════════════════════════════"
info "Готово! Теперь в панели у OpenVPN-клиентов появится"
info "кнопка «Установщик Windows» (.exe с профилем + OpenVPN GUI)."
[ -f "$BUNDLE_X86" ] && info "Установщик универсальный: 32-bit и 64-bit." \
                     || warn "Установщик только 64-bit (нет 32-bit бандла)."
echo "════════════════════════════════════════════"
