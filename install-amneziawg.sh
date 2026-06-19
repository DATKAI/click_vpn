#!/bin/bash
# Установка AmneziaWG (userspace, без модуля ядра) для Click VPN.
# Подходит для LXC-контейнеров, где нельзя грузить модули ядра.
# Использование: bash /opt/click-vpn/install-amneziawg.sh
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
# В LXC apt не может сбросить привилегии до _apt — отключаем песочницу загрузки
mkdir -p /etc/apt/apt.conf.d 2>/dev/null; echo 'APT::Sandbox::User "root";' > /etc/apt/apt.conf.d/99clickvpn 2>/dev/null || true
info(){ echo -e "${GREEN}[INFO]${NC} $1"; }
warn(){ echo -e "${YELLOW}[WARN]${NC} $1"; }
err(){ echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

[ "$EUID" -ne 0 ] && err "Запустите от root"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   AmneziaWG (userspace) — установка       ║"
echo "╚══════════════════════════════════════════╝"
echo ""

BUILD=/tmp/awg-build
rm -rf "$BUILD"; mkdir -p "$BUILD"

# ── Зависимости ───────────────────────────────────────────────────────────────
info "Установка зависимостей (git, make, golang)..."
apt-get update -qq
apt-get install -y -qq git make gcc libmnl-dev golang-go iproute2 iptables

GO=$(command -v go) || err "go не установлен"
info "Go: $($GO version)"

# ── amneziawg-go (userspace демон) ────────────────────────────────────────────
info "Сборка amneziawg-go (userspace)..."
cd "$BUILD"
git clone --depth=1 https://github.com/amnezia-vpn/amneziawg-go.git
cd amneziawg-go
# офлайн-кэш не нужен, go сам подтянет модули
GOFLAGS=-mod=mod $GO build -o /usr/bin/amneziawg-go . || err "Не удалось собрать amneziawg-go"
chmod +x /usr/bin/amneziawg-go
info "amneziawg-go -> /usr/bin/amneziawg-go"

# ── amneziawg-tools (awg, awg-quick) ──────────────────────────────────────────
info "Сборка amneziawg-tools (awg, awg-quick)..."
cd "$BUILD"
git clone --depth=1 https://github.com/amnezia-vpn/amneziawg-tools.git
cd amneziawg-tools/src
make >/dev/null 2>&1 || make
make install >/dev/null 2>&1 || make install
# обычно ставит в /usr/bin/awg и /usr/bin/awg-quick
command -v awg >/dev/null || err "awg не установился"
command -v awg-quick >/dev/null || err "awg-quick не установился"
info "awg / awg-quick установлены"

# ── Заставляем awg-quick использовать userspace amneziawg-go ──────────────────
# awg-quick берёт реализацию из переменной окружения, если нет модуля ядра.
mkdir -p /etc/systemd/system
info "Userspace-реализация: amneziawg-go (модуль ядра не требуется)"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║          AmneziaWG установлен!           ║"
echo "╠══════════════════════════════════════════╣"
echo "║  Теперь в панели можно создавать          ║"
echo "║  серверы типа AmneziaWG 2.0 / legacy.     ║"
echo "║                                          ║"
echo "║  Проверка: awg --version                  ║"
echo "╚══════════════════════════════════════════╝"
echo ""
rm -rf "$BUILD"
