# Click VPN — контекст проекта (для продолжения разработки)

> Этот файл — «передача дел». Прочитай его, чтобы продолжить работу над проектом в новом чате
> без потери контекста. Здесь архитектура, принятые решения, что сделано, что осталось и подводные камни.

---

## 1. Что это

Self-hosted веб-панель управления **OpenVPN** — аналог **Pritunl**. Задача: выдавать VPN-доступ
сотрудникам **многофилиального предприятия** (150+ удалёнщиков), с поддержкой **двух (до 4) интернет-провайдеров**
для отказоустойчивости. Сертификаты OpenVPN.

- **Репозиторий:** https://github.com/DATKAI/click_vpn
- **Развёрнуто:** Debian 13 LXC-контейнер на Proxmox (ID 104), `/opt/click-vpn`, панель на `:8080`
- **Рабочий процесс:** разработчик (ассистент) пишет код → пушит в GitHub → пользователь на сервере
  запускает `bash /opt/click-vpn/update.sh` (или кнопку в UI) → проверяет.

### SSH-ключ для пуша
Сгенерирован ключ `~/.ssh/github_vpn_manager` (ed25519), добавлен в GitHub аккаунт DATKAI.
Пуш: `GIT_SSH_COMMAND="ssh -i ~/.ssh/github_vpn_manager -o IdentitiesOnly=yes" git push`
(на машине разработчика — Windows, рабочая папка `C:\Users\admin\vpn_server`).

---

## 2. Архитектура и стек

- **Без Docker.** Изначально пробовали Docker, но внутри LXC были проблемы с `/dev/net/tun`.
  Перешли на нативный запуск через **systemd + Python venv**. Это важное решение — не возвращать Docker.
- **Backend:** FastAPI + SQLAlchemy + **SQLite** (`/var/lib/click-vpn/vpn.db`).
- **PKI:** библиотека `cryptography` (не easyrsa). CA, серверные/клиентские серты, CRL, DH-параметры — всё в Python.
- **Frontend:** **одна страница** `frontend/templates/index.html` — Alpine.js + Tailwind через **CDN**,
  без шага сборки. Вся логика в одном `<script>` (функция `app()`).
- **OpenVPN:** каждый VPN-сервер = отдельный systemd-юнит `click-vpn-server-{id}.service`.
  Конфиг генерируется в `/var/lib/click-vpn/openvpn/server_{id}.conf` со встроенными (inline)
  `<ca><cert><key><dh>` блоками.
- **NAT:** при старте сервера автоматически добавляется `iptables MASQUERADE` + включается `ip_forward`.

### Раскладка на сервере
```
/opt/click-vpn/            # git-репозиторий + venv + .env + install/update/uninstall.sh
/var/lib/click-vpn/
   vpn.db                  # SQLite — ВСЕ данные (вкл. приватные ключи CA и клиентов!)
   pki/crl_{ca_id}.pem     # CRL
   openvpn/                # конфиги серверов, status_{id}.log, pid
   backups/                # авто/ручные бэкапы .tar.gz
/etc/systemd/system/click-vpn.service
/etc/systemd/system/click-vpn-server-{id}.service
```

`.env` (генерится install.sh): `SECRET_KEY`, `ADMIN_PASSWORD`, `DATABASE_URL`, `DATA_DIR`, `TOKEN_EXPIRE_MINUTES`.

---

## 3. Модель данных (`backend/models.py`)

- **AdminUser** — администраторы панели (пока только дефолтный `admin`).
- **Settings** — singleton (id=1): isp1–isp4 (host/port/label), server_name, SMTP (host/port/user/password/from/tls),
  backup (enabled/interval_hours/keep).
- **CA** — корневой УЦ: cert_pem, key_pem (в открытом виде в БД!), serial-счётчик.
- **Organization** — name, description. M2M с VPNServer через таблицу `org_server`.
- **VPNServer** — name, ca_id, network/netmask/port/protocol, dns_servers, push_routes, status, config_path.
  M2M с организациями.
- **VPNUser** — клиент: username (CN), full_name (ФИО), email, org_id, server_id, ca_id,
  cert_pem/key_pem/cert_serial/cert_expires_at/cert_password, cert_status,
  **is_active** (вкл/выкл), **archived**, **notes**.
- **RevokedSerial** — серийники удалённых/отозванных сертов (чтобы оставались в CRL после удаления юзера).
- **ConnectionLog** — история подключений (пишет фоновый трекер).
- **AuditLog** — журнал действий админов.

### Миграции
Своих миграций нет — в `main.py` функция `_migrate_db()` делает идемпотентные `ALTER TABLE ADD COLUMN`
в `try/except`. **При добавлении нового поля в существующую таблицу — обязательно дописать строку туда.**

---

## 4. Ключевая бизнес-логика (НЕ сломать)

### Организация ↔ Сервер ↔ Клиент
- **Серверу** назначаются организации (кнопка «Организации» на карточке сервера).
- **Организация** — просто имя/описание, серверы НЕ выбирает у себя.
- При создании **клиента** выбирается **организация**; сервер определяется автоматически:
  - 1 сервер у орг → берётся он;
  - несколько → показывается выбор сервера;
  - 0 → ошибка/предупреждение.
- В форме создания клиента блок сервера скрыт, пока организация не выбрана (`availableServers` пуст по умолчанию).

### Состояния клиента и CRL
- **Включён/Выключен** (`is_active`) — обратимая блокировка.
- **Архив** (`archived`) — скрыт из списка + заблокирован.
- **Удалён** — строка удаляется, но серийник кладётся в `RevokedSerial`.
- Все три состояния → серийник попадает в CRL. Функция `rebuild_crl(db, ca_id)` в `routers/users.py`
  собирает CRL из: (не is_active ИЛИ archived) пользователей + всех `RevokedSerial`.
- OpenVPN перечитывает CRL при каждом новом TLS-хендшейке → блокировка применяется к новым подключениям.
  **Существующие сессии живут до ренегоциации (~1 час).** Мгновенный разрыв пока НЕ реализован
  (нужен management-интерфейс OpenVPN — см. «Осталось»).

### `.ovpn` профиль
- Собирается в `services/profile_builder.build_ovpn_profile()` из всех активных ISP-хостов
  (`remote` строки) + inline `<ca><cert><key>`. Helper `_build_user_ovpn(db, user)` в `routers/users.py`
  переиспользуется для скачивания/email/ZIP.

### Скачивание/email
- Кнопки `.ovpn` и email показываются для всех **не архивных** клиентов (вкл. выключенных).
- Скачивание `.ovpn` через **fetch + blob с JWT** (не `<a href>` — иначе 401).

---

## 5. Роутеры (`backend/routers/`)

| Файл | Префикс | Что |
|---|---|---|
| auth | /api/auth | login (+аудит), создание админа, /me |
| settings | /api/settings | get/put настроек, POST /test-email |
| servers | /api | CA CRUD, серверы CRUD, start/stop, org_ids у сервера. Генерация DH при создании сервера |
| users | /api/users | создание/импорт/bulk-download/edit/enable/disable/archive/unarchive/delete/reissue/change-password/send-email/profile |
| status | /api/status | онлайн-клиенты (парсинг status-логов), /summary |
| organizations | /api/organizations | CRUD (серверы НЕ тут — со стороны сервера) |
| logs | /api/logs | journalctl сервиса и серверов |
| system | /api/system | update (запуск update.sh + поллинг вывода), version, **restart** |
| audit | /api | /audit, /users/{id}/connections, /connections/recent |
| backup | /api/backup | download/create/list/file/{name}/restore/restore/{name}/delete |

### Фоновые потоки (стартуют в `main.py` lifespan → `_start_background()`)
- `services/conn_tracker.py` — каждые 20с читает status-логи, пишет connect/disconnect в ConnectionLog.
- `services/backup.py` `start_auto_backup` — каждую минуту проверяет настройки, делает бэкап по интервалу, чистит старые.

---

## 6. Особенности фронтенда (`index.html`)

- Всё в `app()` (Alpine). Данные кешируются в `localStorage` (`cvpn_cache`) → мгновенный показ при F5,
  потом тихое обновление с API.
- **Важно про Alpine `x-show`:** он ставит `display:block`, ломая `display:flex`. Поэтому корневые
  контейнеры (`#app`, страница логина) имеют CSS-классы/`!important`. Если что-то «съезжает» — это причина.
- **Tailwind CDN не поддерживает `@apply`** — все классы-компоненты переписаны на обычный CSS в `<style>`.
- Настройки сделаны **вкладками** (`settingsTab`): general / providers / email / backup / update.
- Разделы (страницы): dashboard, servers, users, orgs, ca, logs, audit, settings.

---

## 7. Установка/обновление/удаление

- `install.sh` — apt-пакеты, venv, pip (с фолбэком на зеркало Tsinghua при таймауте),
  генерация `.env`, systemd-юнит, ip_forward, освобождение порта 8080. Считает «установленным» по наличию `venv`.
- `update.sh` — git pull, pip install, `systemctl daemon-reload && restart`, ждёт подъёма.
- `uninstall.sh` — стоп/удаление сервиса, директорий, sysctl-конфига.
- Веб-порт **8080** (8000 был занят на сервере).

---

## 8. История важных багфиксов (чтобы не повторять)

1. **bcrypt/passlib** — `passlib 1.7.4` несовместим с `bcrypt 4.x`. Убрали passlib, используем `bcrypt` напрямую,
   пароль обрезаем до 72 байт.
2. **DH file** — OpenVPN требует `--dh`. Генерируем 2048-bit DH при создании сервера (5–10с), inline в конфиг.
3. **`server_id` при создании клиента** — был баг: брался `data.server_id` (мог быть None). Теперь `server.id`.
4. **ANSI-коды** в выводе update.sh — чистим регуляркой в `system.py`.
5. **Вёрстка вкладок** — `x-html` криво рендерил SVG, переписали на явные кнопки + CSS-классы `.stab`.
6. **`.ovpn` 401** — скачивание через fetch+blob с токеном.

---

## 9. Что сделано (galочки)

PKI/CA, серверы (systemd+NAT+DH), организации (M2M), клиенты (ФИО/email/notes,
вкл/выкл/архив/удаление/перевыпуск/смена пароля), до 4 провайдеров в .ovpn,
CSV-импорт, массовый ZIP, email (SMTP)+тест, история подключений, аудит-лог,
бэкап/восстановление+автобэкап, просмотр логов, самообновление+перезапуск из UI,
дашборд с авто-обновлением, вкладки настроек,
**статистика трафика** (страница «Статистика» + Chart.js): счётчики, графики трафика/онлайн
по времени (24h/7d/30d), по серверам/организациям (doughnut), топ клиентов.
Графики **закрепляются на дашборд** (📌, хранится в localStorage `cvpn_pinned`).
Данные временного ряда — таблица `TrafficSample`, заполняется фоновым трекером
(`conn_tracker.py`) дельтами байт, сэмпл раз в 5 мин, retention 90 дней.
**Важно про Chart.js + Alpine:** инстансы графиков хранятся в нереактивной переменной
`const _charts = {}` внутри `app()` (НЕ в возвращаемом объекте — иначе Alpine проксирует и ломает canvas).

**WireGuard** (второй протокол). `VPNServer.kind` = openvpn|wireguard. Для WG: серверные
ключи `wg_private_key/wg_public_key`, клиент — `wg_private_key/wg_public_key/wg_address`.
`services/wireguard.py`: gen_keypair (через `wg`), build_server_conf (с PostUp NAT),
systemd-юнит `click-vpn-wg-{id}` через `wg-quick up`, peer sync через `wg syncconf`.
Клиент скачивает `.conf` (не .ovpn). Включение/выключение/удаление → `_wg_resync` пересобирает
пиров и применяет на лету. Требует `wireguard-tools` + модуль ядра wireguard на ХОСТЕ Proxmox.
Ограничение: дашборд «онлайн» и статистика трафика пока только для OpenVPN (парсинг status-логов);
для WG статус есть через `wg show` (не выведен в UI).

## 10. Что осталось (бэклог, по приоритету)

**Критично для прода:**
- [ ] **HTTPS** (reverse-proxy nginx + Let's Encrypt/self-signed; или uvicorn с TLS).
- [ ] **Управление администраторами** из UI + смена пароля admin (API создания админа есть, UI нет).
- [ ] Шифрование приватных ключей в БД (сейчас в открытом виде).

**Полезное для 150+ юзеров:**
- [ ] Уведомления об истечении сертификатов (30/7/1 день) + массовое продление.
- [ ] Массовые операции: отозвать/продлить всю организацию.
- [ ] Мгновенный разрыв активной сессии (OpenVPN management interface: `kill CN`).
- [ ] QR-код для мобильного OpenVPN Connect.

**Протоколы/интеграции:**
- [ ] **WireGuard** (другой механизм — ключи, не серты; отдельный тип сервера).
- [ ] LDAP/Active Directory (вход админов / синк юзеров).
- [ ] REST API + Webhook (HR увольняет → автоотзыв).
- [ ] Site-to-site между офисами.

**UX:**
- [ ] Тёмная тема, Telegram-уведомления, личный кабинет клиента, графики трафика.

---

## 11. Как продолжить в новом чате

Дай ассистенту такой контекст:
> «Продолжаем проект Click VPN (репозиторий github.com/DATKAI/click_vpn, развёрнут на Debian 13 LXC,
> /opt/click-vpn). Прочитай PROJECT_CONTEXT.md в корне репо — там вся история и решения.
> Рабочий процесс: ты пишешь код и пушишь в master, я на сервере делаю `bash /opt/click-vpn/update.sh`.
> Не возвращай Docker, фронт — один index.html на Alpine+Tailwind CDN. Дальше хочу сделать <ЗАДАЧА>.»

Полезные команды на сервере для диагностики:
```bash
systemctl status click-vpn
journalctl -u click-vpn -n 50 --no-pager
journalctl -u click-vpn-server-1 -n 50 --no-pager
cat /opt/click-vpn/.env          # пароль админа
sqlite3 /var/lib/click-vpn/vpn.db .tables
```
