<div align="center">

# 🔐 Click VPN

**Самостоятельная панель управления OpenVPN — аналог Pritunl**

Управление VPN-доступом для многофилиального предприятия: организации, клиенты, сертификаты, до 4 провайдеров.

[![Debian](https://img.shields.io/badge/Debian-13-A81D33?logo=debian&logoColor=white)](https://www.debian.org/)
[![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![OpenVPN](https://img.shields.io/badge/OpenVPN-EA7E20?logo=openvpn&logoColor=white)](https://openvpn.net/)

</div>

---

## ✨ Возможности

| | |
|---|---|
| 🏢 **Организации** | Филиалы/отделы привязываются к серверам, клиент выбирает организацию |
| 👥 **Клиенты** | ФИО, логин, email, комментарии. Вкл/выкл/архив/удаление |
| 📜 **PKI** | Свой CA, выпуск/перевыпуск/отзыв сертификатов, CRL |
| 🌐 **До 4 провайдеров** | Все активные хосты автоматически попадают в `.ovpn` (failover) |
| 🔑 **Пароль сертификата** | Опциональное шифрование приватного ключа |
| 📥 **CSV импорт** | Массовое создание клиентов из файла |
| 📦 **Массовое скачивание** | Все профили выбранных клиентов одним ZIP |
| ✉️ **Email рассылка** | Отправка `.ovpn` клиенту на почту (SMTP) |
| 📊 **Мониторинг** | Кто онлайн, реальный/VPN IP, трафик. Авто-обновление |
| 🕓 **История подключений** | Журнал заходов каждого клиента |
| 📋 **Аудит-лог** | Кто что делал: создал/удалил/включил/выключил |
| 💾 **Бэкап** | Ручной + автоматический, восстановление одной кнопкой |
| 📜 **Логи в панели** | Просмотр journalctl сервиса и OpenVPN серверов |
| 🔄 **Самообновление** | Кнопка «Обновить» и «Перезапустить» прямо в UI |

---

## 🚀 Установка (Debian 13)

> Устанавливать **внутри** LXC-контейнера / VM, не на гипервизоре.

```bash
apt-get install -y git
git clone https://gitverse.ru/DATKAI/click_vpn.git /opt/click-vpn
bash /opt/click-vpn/install.sh
```

Скрипт сам:
- установит Python, OpenVPN, зависимости
- создаст systemd-сервис `click-vpn`
- сгенерирует пароль администратора (покажет в конце)
- включит IP-форвардинг

Панель: `http://<IP>:8080` · Логин: `admin` · Пароль — из вывода скрипта (или в `/opt/click-vpn/.env`).

### Proxmox LXC — включить TUN
На **хосте** Proxmox (замените `104` на ID контейнера):
```bash
echo "lxc.cgroup2.devices.allow: c 10:200 rwm" >> /etc/pve/lxc/104.conf
echo "lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file" >> /etc/pve/lxc/104.conf
pct reboot 104
```

---

## 🔄 Обновление

```bash
bash /opt/click-vpn/update.sh
```
Или кнопкой **«Обновить Click VPN»** в Настройки → Обновление.

## 🗑 Удаление

```bash
bash /opt/click-vpn/uninstall.sh
```

---

## 🧭 Порядок настройки

1. **Настройки → Провайдеры** — вписать внешние IP провайдеров (ISP1–ISP4)
2. **Сертификаты** — создать CA
3. **Серверы** — создать OpenVPN-сервер, запустить
4. **Организации** — создать, затем на карточке сервера привязать организации
5. **Клиенты** — создать (выбрать организацию → сервер определится сам), скачать `.ovpn`

---

## 🏗 Архитектура

```
Debian (LXC/VM)
 ├── systemd: click-vpn.service        → FastAPI (uvicorn :8080)
 ├── systemd: click-vpn-server-N       → процессы OpenVPN (по серверу)
 ├── /opt/click-vpn                     → код + venv + .env
 └── /var/lib/click-vpn                 → vpn.db (SQLite), pki/, openvpn/, backups/
```

**Стек:** FastAPI · SQLAlchemy · SQLite · `cryptography` (PKI) · Alpine.js + Tailwind (CDN, без сборки) · OpenVPN via systemd · iptables MASQUERADE.

---

## 📁 Структура

```
backend/
 ├── main.py              # инициализация, миграции, фоновые потоки
 ├── models.py            # ORM-модели
 ├── schemas.py           # Pydantic-схемы
 ├── auth.py              # JWT + bcrypt
 ├── database.py
 ├── routers/             # auth, settings, servers, users, status,
 │                        # organizations, logs, system, audit, backup
 └── services/            # pki, profile_builder, ovpn_manager, mailer,
                          # audit, backup, conn_tracker
frontend/templates/index.html   # вся SPA в одном файле
install.sh / update.sh / uninstall.sh
```

Подробности по решениям и истории разработки — в [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md).

---

## ⚠️ Статус

Рабочий MVP в активной разработке. Перед продакшеном рекомендуется:
- настроить **HTTPS** (reverse-proxy + сертификат)
- ограничить доступ к панели по сети
- включить **автобэкап**

---

<div align="center">
Сделано для управления корпоративным VPN многофилиального предприятия.
</div>
