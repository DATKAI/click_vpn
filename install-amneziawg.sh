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
info "Установка зависимостей (git, make, gcc, libmnl)..."
apt-get update -qq
apt-get install -y -qq git make gcc libmnl-dev iproute2 iptables curl ca-certificates

# ── Go нужной версии (amneziawg-go требует Go >= 1.24) ────────────────────────
# Дистрибутивный golang часто слишком старый — ставим официальный с go.dev.
GO_VER="1.24.4"
ensure_go() {
  local cur=""
  if command -v go >/dev/null 2>&1; then
    cur=$(go version | grep -oE 'go[0-9]+\.[0-9]+' | head -1 | sed 's/go//')
  fi
  local need_minor=24
  local cur_minor=$(echo "${cur:-0.0}" | cut -d. -f2)
  if [ -n "$cur" ] && [ "$(echo "$cur" | cut -d. -f1)" = "1" ] && [ "$cur_minor" -ge "$need_minor" ] 2>/dev/null; then
    GO=$(command -v go); return
  fi
  info "Устанавливаю Go $GO_VER с go.dev (системный Go отсутствует или устарел)..."
  local arch; arch=$(uname -m)
  case "$arch" in
    x86_64|amd64) arch=amd64 ;;
    aarch64|arm64) arch=arm64 ;;
    armv7l) arch=armv6l ;;
    *) err "Неподдерживаемая архитектура: $arch" ;;
  esac
  local tgz="/tmp/go${GO_VER}.tar.gz"
  curl -fSL "https://go.dev/dl/go${GO_VER}.linux-${arch}.tar.gz" -o "$tgz" \
    || err "Не удалось скачать Go $GO_VER"
  rm -rf /usr/local/go
  tar -C /usr/local -xzf "$tgz"
  rm -f "$tgz"
  GO=/usr/local/go/bin/go
  export PATH="/usr/local/go/bin:$PATH"
}
ensure_go
info "Go: $($GO version)"
export GOTOOLCHAIN=local   # не пытаться качать другую версию тулчейна

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
