#!/bin/bash
# Обновление / переключение версий Click VPN.
# Использование:
#   bash update.sh                — обновить до последней (origin/master)
#   bash update.sh --latest       — то же
#   bash update.sh --ref <tag|commit>  — перейти к конкретной версии (откат/выбор)
#
# При неудачном старте новой версии автоматически откатывается на предыдущую.
set -e

INSTALL_DIR="/opt/click-vpn"
SERVICE_NAME="click-vpn"
VENV_DIR="$INSTALL_DIR/venv"
DATA_DIR="${DATA_DIR:-/var/lib/click-vpn}"
STATUS_FILE="$DATA_DIR/update-status.json"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; _status "error" "$1"; exit 1; }

_status() {  # state, message
  mkdir -p "$DATA_DIR" 2>/dev/null || true
  printf '{"state":"%s","message":"%s","old":"%s","new":"%s","ts":"%s"}\n' \
    "$1" "${2//\"/\'}" "${OLD_COMMIT:-}" "$(git -C "$INSTALL_DIR" rev-parse --short HEAD 2>/dev/null)" \
    "$(date -Iseconds)" > "$STATUS_FILE" 2>/dev/null || true
}

# ── Аргументы ──────────────────────────────────────────────────────────────────
MODE="latest"; TARGET=""
while [ $# -gt 0 ]; do
  case "$1" in
    --latest) MODE="latest"; shift ;;
    --ref) MODE="ref"; TARGET="$2"; shift 2 ;;
    *) shift ;;
  esac
done

[ "$EUID" -ne 0 ] && error "Запустите от root"
[ ! -d "$INSTALL_DIR" ] && error "Не найдена $INSTALL_DIR"

cd "$INSTALL_DIR"
OLD_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)
info "Текущая версия: $OLD_COMMIT"
_status "running" "fetch"

# ── Бэкап БД перед переключением ────────────────────────────────────────────────
if [ -f "$DATA_DIR/vpn.db" ]; then
  mkdir -p "$DATA_DIR/backups"
  cp "$DATA_DIR/vpn.db" "$DATA_DIR/backups/vpn-pre-update-$(date +%Y%m%d-%H%M%S).db" || warn "не удалось сделать бэкап БД"
fi

git fetch origin --tags -q

if [ "$MODE" = "latest" ]; then
  TARGET="origin/master"
fi

NEW_REF=$(git rev-parse --short "$TARGET" 2>/dev/null) || error "Версия не найдена: $TARGET"

if [ "$MODE" = "latest" ]; then
  info "Изменения:"; git log --oneline HEAD..origin/master 2>/dev/null || true
  git checkout -q master 2>/dev/null || git checkout -q -B master
  git reset --hard origin/master -q
else
  info "Переключение на версию: $TARGET ($NEW_REF)"
  git checkout -q "$TARGET"   # detached HEAD — допустимо для отката/выбора
fi

# ── Функция применения (зависимости + рестарт) ──────────────────────────────────
apply_and_check() {
  "$VENV_DIR/bin/pip" install -r "$INSTALL_DIR/backend/requirements.txt" --timeout 30 -q 2>/dev/null \
    || "$VENV_DIR/bin/pip" install -r "$INSTALL_DIR/backend/requirements.txt" --timeout 60 -q \
       -i https://pypi.tuna.tsinghua.edu.cn/simple || true
  systemctl daemon-reload
  systemctl restart "$SERVICE_NAME"
  for i in $(seq 1 30); do
    curl -sf http://localhost:8080 &>/dev/null && return 0
    sleep 1
  done
  systemctl is-active --quiet "$SERVICE_NAME"
}

info "Применение версии..."
_status "running" "applying"
if apply_and_check; then
  NEW_COMMIT=$(git rev-parse --short HEAD)
  info "Готово. Версия: $NEW_COMMIT"
  _status "ok" "updated"
else
  warn "Новая версия не поднялась — откат на $OLD_COMMIT"
  _status "running" "rollback"
  git checkout -q "$OLD_COMMIT"
  apply_and_check && _status "rolled_back" "auto-rollback to $OLD_COMMIT" \
                  || _status "error" "rollback failed"
  error "Откат выполнен на $OLD_COMMIT (новая версия не запустилась)"
fi
