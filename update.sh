#!/bin/bash
# Обновление VPN Manager с GitHub
# Использование: bash /opt/vpn-manager/update.sh
set -e

INSTALL_DIR="/opt/click-vpn"
SERVICE_NAME="click-vpn"
VENV_DIR="$INSTALL_DIR/venv"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║         Click VPN — Обновление         ║"
echo "╚══════════════════════════════════════════╝"
echo ""

[ "$EUID" -ne 0 ] && error "Запустите от root: bash update.sh"
[ ! -d "$INSTALL_DIR" ] && error "Не найдена директория $INSTALL_DIR. Сначала запустите install.sh"

cd "$INSTALL_DIR"

OLD_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
info "Текущая версия: $OLD_COMMIT"

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

# ── Обновление зависимостей ───────────────────────────────────────────────────
info "Обновление Python зависимостей..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$INSTALL_DIR/backend/requirements.txt"

# ── Перезапуск сервиса ────────────────────────────────────────────────────────
info "Перезапуск сервиса..."
systemctl restart "$SERVICE_NAME"

# ── Проверка ──────────────────────────────────────────────────────────────────
info "Ожидание запуска..."
for i in $(seq 1 30); do
  if curl -sf http://localhost:8080 &>/dev/null; then break; fi
  sleep 1
done

if ! systemctl is-active --quiet "$SERVICE_NAME"; then
  error "Сервис не запустился! Проверьте: journalctl -u click-vpn -n 50"
fi

NEW_COMMIT=$(git rev-parse --short HEAD)
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║          Обновление завершено!           ║"
printf  "║  Было:    %-30s║\n" "$OLD_COMMIT"
printf  "║  Стало:   %-30s║\n" "$NEW_COMMIT"
echo "╚══════════════════════════════════════════╝"
echo ""
