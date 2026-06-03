#!/bin/bash
# Установка strongSwan (IKEv2/IPsec) для Click VPN.
# Использование: bash /opt/click-vpn/install-ikev2.sh
set -e

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info(){ echo -e "${GREEN}[INFO]${NC} $1"; }
warn(){ echo -e "${YELLOW}[WARN]${NC} $1"; }
err(){ echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

[ "$EUID" -ne 0 ] && err "Запустите от root"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║     strongSwan (IKEv2) — установка        ║"
echo "╚══════════════════════════════════════════╝"
echo ""

info "Установка пакетов..."
apt-get update -qq
apt-get install -y -qq \
  strongswan strongswan-swanctl \
  libcharon-extra-plugins libstrongswan-extra-plugins \
  iptables

# базовая директория swanctl
mkdir -p /etc/swanctl/conf.d /etc/swanctl/x509 /etc/swanctl/x509ca /etc/swanctl/private

# IP forwarding
echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-clickvpn-ikev2.conf
sysctl -p /etc/sysctl.d/99-clickvpn-ikev2.conf -q || true

systemctl enable strongswan >/dev/null 2>&1 || true
systemctl restart strongswan || true

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║          strongSwan установлен!           ║"
echo "╠══════════════════════════════════════════╣"
echo "║  Теперь в панели можно создавать          ║"
echo "║  серверы типа IKEv2.                       ║"
echo "║                                          ║"
echo "║  ВАЖНО для LXC: IPsec использует kernel   ║"
echo "║  xfrm/ESP. Если в контейнере не работает  ║"
echo "║  — на хосте Proxmox проверьте/добавьте:   ║"
echo "║    modprobe af_key esp4 xfrm4_tunnel      ║"
echo "║  и разрешите контейнеру нужные права.     ║"
echo "║                                          ║"
echo "║  Проверка: swanctl --version              ║"
echo "╚══════════════════════════════════════════╝"
echo ""
