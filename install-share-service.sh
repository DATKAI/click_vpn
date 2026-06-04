#!/bin/bash
# Установка изолированного микросервиса раздачи временных ссылок.
# Запускается под отдельным непривилегированным пользователем clickvpn-share
# с доступом ТОЛЬКО к каталогу share/ — без доступа к БД/ключам.
# Использование: bash /opt/click-vpn/install-share-service.sh
set -e

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info(){ echo -e "${GREEN}[INFO]${NC} $1"; }
warn(){ echo -e "${YELLOW}[WARN]${NC} $1"; }
err(){ echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

[ "$EUID" -ne 0 ] && err "Запустите от root"

INSTALL_DIR="/opt/click-vpn"
DATA_DIR="/var/lib/click-vpn"
SHARE_DIR="${DATA_DIR}/share"
SHARE_USER="clickvpn-share"
PORT="${SHARE_PORT:-8081}"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   Микросервис раздачи ссылок — установка  ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Отдельный системный пользователь ──────────────────────────────────────────
if id "$SHARE_USER" >/dev/null 2>&1; then
  info "Пользователь $SHARE_USER уже существует"
else
  info "Создание пользователя $SHARE_USER..."
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SHARE_USER"
fi

# ── Каталог share/ с правами для сервиса ─────────────────────────────────────
mkdir -p "$SHARE_DIR"
chgrp "$SHARE_USER" "$SHARE_DIR"
chmod 2750 "$SHARE_DIR"     # setgid: новые файлы наследуют группу

# ── systemd-юнит ─────────────────────────────────────────────────────────────
info "Создание systemd-юнита click-vpn-share..."
cat > /etc/systemd/system/click-vpn-share.service <<EOF
[Unit]
Description=Click VPN — изолированный сервис раздачи ссылок
After=network.target

[Service]
Type=simple
User=${SHARE_USER}
Group=${SHARE_USER}
Environment=DATA_DIR=${DATA_DIR}
WorkingDirectory=${INSTALL_DIR}/backend
ExecStart=${INSTALL_DIR}/venv/bin/uvicorn share_service:app --host 127.0.0.1 --port ${PORT}
Restart=always
RestartSec=3

# Жёсткая изоляция: доступ только к share/, остальное только чтение
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadOnlyPaths=${INSTALL_DIR}
ReadWritePaths=${SHARE_DIR}
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable click-vpn-share >/dev/null 2>&1 || true
systemctl restart click-vpn-share
sleep 2

if systemctl is-active --quiet click-vpn-share; then
  info "Сервис click-vpn-share работает на 127.0.0.1:${PORT} ✓"
else
  err "Сервис не запустился — проверьте: journalctl -u click-vpn-share -n 30"
fi

# Проверка доступности
if curl -fs "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
  info "Health-check OK"
fi

# ── Self-signed TLS-сертификат на WAN IP (для доступа без домена) ─────────────
CERT_DIR="/etc/click-vpn"
CERT="${CERT_DIR}/share-cert.pem"
KEY="${CERT_DIR}/share-key.pem"
mkdir -p "$CERT_DIR"

# WAN IP: из переменной WAN_IP или автоопределение
if [ -z "$WAN_IP" ]; then
  WAN_IP="$(curl -fs --max-time 5 https://api.ipify.org 2>/dev/null || curl -fs --max-time 5 https://ifconfig.me 2>/dev/null || true)"
fi

if [ -n "$WAN_IP" ]; then
  if [ -f "$CERT" ]; then
    info "TLS-сертификат уже есть: $CERT"
  else
    info "Генерация self-signed сертификата на IP ${WAN_IP}..."
    openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
      -keyout "$KEY" -out "$CERT" \
      -subj "/CN=${WAN_IP}" -addext "subjectAltName=IP:${WAN_IP}" 2>/dev/null \
      && chmod 600 "$KEY" \
      && info "Сертификат создан: $CERT (SAN: IP:${WAN_IP})" \
      || warn "Не удалось сгенерировать сертификат"
  fi
else
  warn "Не удалось определить WAN IP. Задайте вручную: WAN_IP=1.2.3.4 bash $0"
fi

# ── Настройка nginx (проброс только /s/ наружу) ──────────────────────────────
if [ "${SKIP_NGINX:-0}" = "1" ]; then
  warn "Пропуск настройки nginx (SKIP_NGINX=1)"
elif [ ! -f "$CERT" ]; then
  warn "Нет TLS-сертификата — пропускаю настройку nginx.
Задайте WAN_IP и перезапустите: WAN_IP=1.2.3.4 bash $0"
else
  info "Установка и настройка nginx..."
  if ! command -v nginx >/dev/null 2>&1; then
    apt-get update -qq
    # postinstall пытается запустить nginx с дефолтным конфигом и может упасть
    # (занятый порт и т.п.) — это не страшно, мы сейчас применим свой конфиг
    apt-get install -y -qq nginx || warn "nginx установлен, но дефолтный автозапуск не удался — продолжаем"
  fi

  # Debian-структура sites-available/enabled
  mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled
  if ! grep -q "sites-enabled" /etc/nginx/nginx.conf; then
    # на некоторых сборках include отсутствует — добавим в http-блок
    sed -i '/http {/a \    include /etc/nginx/sites-enabled/*;' /etc/nginx/nginx.conf
  fi

  # убираем дефолтный сайт (конфликт по 80)
  rm -f /etc/nginx/sites-enabled/default

  cp "${INSTALL_DIR}/nginx-share.conf.example" /etc/nginx/sites-available/clickvpn-share
  ln -sf /etc/nginx/sites-available/clickvpn-share /etc/nginx/sites-enabled/clickvpn-share

  if nginx -t 2>/tmp/nginx-test.log; then
    systemctl enable nginx >/dev/null 2>&1 || true
    systemctl restart nginx
    info "nginx настроен и запущен ✓"
  else
    warn "nginx -t выдал ошибку:"
    cat /tmp/nginx-test.log
    warn "Проверьте конфиг /etc/nginx/sites-available/clickvpn-share"
  fi
fi

echo ""
echo "════════════════════════════════════════════"
info "Готово! Микросервис раздачи изолирован:"
echo "  • пользователь:  ${SHARE_USER} (nologin, без доступа к БД/ключам)"
echo "  • доступ только к: ${SHARE_DIR}"
echo "  • слушает:        127.0.0.1:${PORT}"
[ -f "$CERT" ] && echo "  • TLS-сертификат: ${CERT} / ${KEY}"
echo ""
if [ -n "$WAN_IP" ]; then
  info "Укажите в настройках панели «Публичный адрес»:"
  echo "      https://${WAN_IP}"
  echo ""
  info "Проверка снаружи (с другого устройства):"
  echo "      откройте https://${WAN_IP}/s/test — должно показать «Неверная ссылка»"
  echo "      (это значит сервис доступен; предупреждение браузера о self-signed — норма)"
fi
echo ""
warn "Не забудьте пробросить порты 80 и 443 на этот сервер (роутер/firewall)."
echo "════════════════════════════════════════════"
