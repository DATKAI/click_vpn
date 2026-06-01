#!/bin/bash
# Обновление VPN Manager с GitHub
# Использование: bash /opt/vpn-manager/update.sh
set -e

INSTALL_DIR="/opt/vpn-manager"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║         VPN Manager — Обновление         ║"
echo "╚══════════════════════════════════════════╝"
echo ""

[ "$EUID" -ne 0 ] && error "Запустите от root: sudo bash update.sh"
[ ! -d "$INSTALL_DIR" ] && error "Не найдена директория $INSTALL_DIR. Сначала запустите install.sh"

cd "$INSTALL_DIR"

# ── Версия до обновления ──────────────────────────────────────────────────────
OLD_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
info "Текущая версия: $OLD_COMMIT"

# ── Сохраняем .env ────────────────────────────────────────────────────────────
if [ -f .env ]; then
  cp .env /tmp/vpn-manager-env.bak
  info ".env сохранён в /tmp/vpn-manager-env.bak"
fi

# ── Pull ──────────────────────────────────────────────────────────────────────
info "Получение обновлений с GitHub..."
git fetch origin -q
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/master)

if [ "$LOCAL" = "$REMOTE" ]; then
  info "Уже последняя версия. Обновление не требуется."
  exit 0
fi

info "Изменения:"
git log --oneline HEAD..origin/master

git pull origin master -q

# ── Восстанавливаем .env ──────────────────────────────────────────────────────
if [ -f /tmp/vpn-manager-env.bak ]; then
  cp /tmp/vpn-manager-env.bak .env
  chmod 600 .env
  info ".env восстановлен"
fi

# ── Пересборка контейнера ─────────────────────────────────────────────────────
info "Пересборка и перезапуск контейнера..."
docker compose pull --quiet 2>/dev/null || true
docker compose up -d --build

# ── Ждём поднятия ─────────────────────────────────────────────────────────────
info "Ожидание запуска..."
for i in $(seq 1 30); do
  if curl -sf http://localhost:8000 &>/dev/null; then break; fi
  sleep 1
done

# ── Удаляем старые образы ─────────────────────────────────────────────────────
docker image prune -f -q

NEW_COMMIT=$(git rev-parse --short HEAD)
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║          Обновление завершено!           ║"
printf  "║  Было:    %-30s║\n" "$OLD_COMMIT"
printf  "║  Стало:   %-30s║\n" "$NEW_COMMIT"
echo "╚══════════════════════════════════════════╝"
echo ""
