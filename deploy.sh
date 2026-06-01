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

# .env
if [ ! -f .env ]; then
  cp .env.example .env
  # Генерируем случайный SECRET_KEY
  SECRET=$(openssl rand -hex 32)
  sed -i "s/change-me-please-use-strong-random-key/$SECRET/" .env
  echo ""
  echo ">>> .env создан. Установите ADMIN_PASSWORD в .env перед запуском!"
  echo ""
fi

docker compose up -d --build

echo ""
echo "=== Готово ==="
echo "Веб-панель: http://$(hostname -I | awk '{print $1}'):8000"
echo "Логин: admin / (пароль из .env ADMIN_PASSWORD)"
