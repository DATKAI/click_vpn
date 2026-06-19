#!/bin/bash
# Установка strongSwan (IKEv2/IPsec) для Click VPN.
# Использование: bash /opt/click-vpn/install-ikev2.sh
set -e

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
mkdir -p /etc/apt/apt.conf.d 2>/dev/null; echo 'APT::Sandbox::User "root";' > /etc/apt/apt.conf.d/99clickvpn 2>/dev/null || true
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
# charon-systemd — современный демон (strongSwan 6.x) с VICI/swanctl
apt-get install -y -qq \
  strongswan-swanctl charon-systemd \
  libcharon-extra-plugins libstrongswan-extra-plugins \
  libcharon-extauth-plugins \
  iptables || apt-get install -y -qq strongswan strongswan-swanctl libcharon-extra-plugins iptables

# базовая директория swanctl
mkdir -p /etc/swanctl/conf.d /etc/swanctl/x509 /etc/swanctl/x509ca /etc/swanctl/private

# IP forwarding
echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-clickvpn-ikev2.conf
sysctl -p /etc/sysctl.d/99-clickvpn-ikev2.conf -q || true

# современный сервис называется strongswan (charon-systemd); запускаем что есть
for svc in strongswan strongswan-starter; do
  if systemctl list-unit-files | grep -q "^${svc}.service"; then
    systemctl enable "$svc" >/dev/null 2>&1 || true
    systemctl restart "$svc" >/dev/null 2>&1 || true
    info "Сервис: $svc"
    break
  fi
done
sleep 2
swanctl --version >/dev/null 2>&1 && info "swanctl работает" || warn "swanctl не отвечает"

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
