#!/bin/bash
# Защита Click VPN от ботов/сканеров: fail2ban банит IP с неудачными
# TLS-попытками (CN=UNDEF) к OpenVPN-серверам.
# Использование: bash /opt/click-vpn/install-fail2ban.sh
set -e

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info(){ echo -e "${GREEN}[INFO]${NC} $1"; }
warn(){ echo -e "${YELLOW}[WARN]${NC} $1"; }
err(){ echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

[ "$EUID" -ne 0 ] && err "Запустите от root"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   fail2ban — защита от ботов/сканеров     ║"
echo "╚══════════════════════════════════════════╝"
echo ""

info "Установка fail2ban..."
apt-get update -qq
apt-get install -y -qq fail2ban

# ── Фильтр: ловим неудачные TLS-попытки OpenVPN ────────────────────────────────
# OpenVPN при verb>=3 пишет строки вида:
#   1.2.3.4:5678 TLS Error: TLS handshake failed
#   1.2.3.4:5678 TLS Error: TLS key negotiation failed ...
#   1.2.3.4:5678 VERIFY ERROR: ...
info "Создание фильтра click-vpn-openvpn..."
cat > /etc/fail2ban/filter.d/click-vpn-openvpn.conf <<'EOF'
[Definition]
failregex = ^<HOST>:\d+ TLS Error: TLS (handshake|key negotiation) failed
            ^<HOST>:\d+ VERIFY ERROR
            ^<HOST>:\d+ TLS Error: TLS object -> incoming plaintext read error
            ^<HOST>:\d+ Connection reset, restarting
ignoreregex =
EOF

# ── Jail: читает journald всех OpenVPN-юнитов Click VPN ────────────────────────
# Юниты называются click-vpn-server-{id}.service. journalmatch с wildcard нельзя,
# поэтому читаем весь journal (backend=systemd) — фильтр отсекает лишнее.
info "Создание jail..."
cat > /etc/fail2ban/jail.d/click-vpn.conf <<'EOF'
[click-vpn-openvpn]
enabled  = true
backend  = systemd
filter   = click-vpn-openvpn
maxretry = 5
findtime = 600
bantime  = 3600
# банхаммер по всем портам через iptables
action   = iptables-allports[name=click-vpn]
EOF

info "Перезапуск fail2ban..."
systemctl enable fail2ban >/dev/null 2>&1 || true
systemctl restart fail2ban
sleep 2

if systemctl is-active --quiet fail2ban; then
  info "fail2ban работает ✓"
  echo ""
  fail2ban-client status click-vpn-openvpn 2>/dev/null || warn "jail ещё инициализируется"
else
  err "fail2ban не запустился — проверьте: journalctl -u fail2ban -n 30"
fi

echo ""
echo "════════════════════════════════════════════"
info "Готово. Правила:"
echo "  • 5 неудачных TLS-попыток за 10 мин → бан на 1 час"
echo ""
echo "Полезные команды:"
echo "  fail2ban-client status click-vpn-openvpn   # статус + забаненные IP"
echo "  fail2ban-client set click-vpn-openvpn unbanip <IP>   # разбанить"
echo "  fail2ban-client unban --all                # разбанить всех"
echo ""
warn "Совет: включите обфускацию (tls-crypt) на OpenVPN-сервере в панели —"
warn "тогда боты вообще не смогут начать TLS-handshake (пакеты дропаются молча)."
echo "════════════════════════════════════════════"
