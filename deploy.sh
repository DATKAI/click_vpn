#!/bin/bash
# Скрипт первичного деплоя на Debian 13 (Proxmox LXC)
set -e

echo "=== VPN Manager Deploy ==="

# Docker
if ! command -v docker &>/dev/null; then
  apt-get update
  apt-get install -y ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi

# .env — генерируем автоматически при первом запуске
if [ ! -f .env ]; then
  SECRET_KEY=$(openssl rand -hex 32)
  ADMIN_PASSWORD=$(openssl rand -base64 12 | tr -d '/+=')

  cat > .env <<EOF
SECRET_KEY=${SECRET_KEY}
ADMIN_PASSWORD=${ADMIN_PASSWORD}
TOKEN_EXPIRE_MINUTES=480
DATABASE_URL=sqlite:////data/vpn.db
DATA_DIR=/data
EOF

  echo ""
  echo "┌─────────────────────────────────────────┐"
  echo "│         .env сгенерирован автоматом     │"
  echo "│                                         │"
  echo "│  Логин:  admin                          │"
  echo "│  Пароль: ${ADMIN_PASSWORD}              │"
  echo "│                                         │"
  echo "│  Сохраните пароль — он больше не        │"
  echo "│  будет показан!                         │"
  echo "└─────────────────────────────────────────┘"
  echo ""
fi

docker compose up -d --build

IP=$(hostname -I | awk '{print $1}')
echo ""
echo "=== Готово ==="
echo "Веб-панель: http://${IP}:8000"
if [ -f .env ]; then
  PASS=$(grep ADMIN_PASSWORD .env | cut -d= -f2)
  echo "Логин:      admin"
  echo "Пароль:     ${PASS}"
fi
