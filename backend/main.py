import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Request
from fastapi.responses import HTMLResponse

from database import engine, SessionLocal, Base
from models import AdminUser, Settings
from auth import hash_password
from routers import auth, settings, servers, users, status, organizations, logs, system, audit, backup, stats, download, modules, billing, s2s, updates, routes as routes_router

DATA_DIR = os.getenv("DATA_DIR", "./data")
os.makedirs(os.path.join(DATA_DIR, "pki"), exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, "openvpn"), exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _migrate_db()
    _encrypt_existing()
    _seed_defaults()
    _start_background()
    yield


def _encrypt_existing():
    """Шифрует ранее сохранённые в открытом виде секреты (одноразовая миграция).
    Идемпотентно: значения с префиксом enc: пропускаются. Работает на сыром SQL,
    чтобы не задеть прозрачное шифрование TypeDecorator."""
    import sqlalchemy as sa
    from services import crypto

    targets = [
        ("ca",          ["key_pem"]),
        ("vpn_users",   ["key_pem", "cert_password", "eap_password", "wg_private_key"]),
        ("vpn_servers", ["wg_private_key", "ikev2_key_pem", "tls_crypt_key"]),
        ("settings",    ["smtp_password"]),
    ]
    try:
        with engine.connect() as conn:
            for table, cols in targets:
                for col in cols:
                    try:
                        rows = conn.execute(sa.text(
                            f"SELECT id, {col} FROM {table} WHERE {col} IS NOT NULL AND {col} != ''"
                        )).fetchall()
                    except Exception:
                        continue  # колонки нет — пропускаем
                    for rid, val in rows:
                        if isinstance(val, str) and not crypto.is_encrypted(val):
                            conn.execute(
                                sa.text(f"UPDATE {table} SET {col} = :v WHERE id = :id"),
                                {"v": crypto.encrypt(val), "id": rid},
                            )
                conn.commit()
    except Exception:
        pass


def _start_background():
    """Запуск фоновых потоков: трекер подключений + автобэкап."""
    from models import VPNServer, VPNUser, ConnectionLog, TrafficSample, Settings, ConnectionAttempt
    from services import conn_tracker, backup as backup_svc
    try:
        conn_tracker.start_tracker(SessionLocal, VPNServer, VPNUser, ConnectionLog, TrafficSample, ConnectionAttempt)
    except Exception:
        pass

    def _backup_settings():
        db = SessionLocal()
        try:
            s = db.query(Settings).filter(Settings.id == 1).first()
            if s:
                return bool(s.backup_enabled), s.backup_interval_hours or 24, s.backup_keep or 7
        finally:
            db.close()
        return False, 24, 7

    try:
        backup_svc.start_auto_backup(_backup_settings)
    except Exception:
        pass

    # периодическая очистка просроченных share-ссылок
    import threading, time as _time
    def _share_cleanup_loop():
        from services import share
        while True:
            try:
                share.cleanup_expired()
            except Exception:
                pass
            _time.sleep(3600)
    threading.Thread(target=_share_cleanup_loop, daemon=True).start()

    # напоминания об истечении сертификатов
    try:
        from services import expiry
        from models import VPNUser as _VU, Settings as _S
        expiry.start_checker(SessionLocal, _VU, _S)
    except Exception:
        pass

    # биллинг: фоновая проверка лимитов (срок/трафик)
    try:
        from services import billing as billing_svc
        billing_svc.start_checker(SessionLocal)
    except Exception:
        pass

    # фоновое определение страны для попыток + ретеншн старых попыток
    def _geoip_loop():
        from datetime import datetime, timedelta
        from services import geoip
        from models import ConnectionAttempt
        ticks = 0
        while True:
            db = SessionLocal()
            try:
                rows = db.query(ConnectionAttempt).filter(
                    ConnectionAttempt.country_code.is_(None)
                ).limit(10).all()
                for r in rows:
                    country, code = geoip.lookup(r.ip)
                    r.country = country or "?"
                    r.country_code = code or "??"
                    db.commit()
                    _time.sleep(1.5)   # бережём лимит ip-api
                # ретеншн: раз в ~час чистим попытки старше 30 дней
                ticks += 1
                if ticks >= 120:
                    ticks = 0
                    cutoff = datetime.utcnow() - timedelta(days=30)
                    db.query(ConnectionAttempt).filter(
                        ConnectionAttempt.last_seen < cutoff
                    ).delete()
                    db.commit()
            except Exception:
                db.rollback()
            finally:
                db.close()
            _time.sleep(30)
    threading.Thread(target=_geoip_loop, daemon=True).start()

    # site-to-site: монитор резервных каналов (входящий failover хаб→филиал)
    try:
        from services.s2s import failover as s2s_failover
        s2s_failover.start_checker(SessionLocal)
    except Exception:
        pass


def _migrate_db():
    """Добавляет новые колонки в существующую БД если их нет (safe migrations)."""
    migrations = [
        ("settings",  "isp3_host",      "VARCHAR(256)"),
        ("settings",  "isp3_port",      "INTEGER DEFAULT 1194"),
        ("settings",  "isp3_label",     "VARCHAR(64) DEFAULT 'ISP3'"),
        ("settings",  "isp4_host",      "VARCHAR(256)"),
        ("settings",  "isp4_port",      "INTEGER DEFAULT 1194"),
        ("settings",  "isp4_label",     "VARCHAR(64) DEFAULT 'ISP4'"),
        ("vpn_users", "org_id",         "INTEGER REFERENCES organizations(id)"),
        ("vpn_users", "cert_password",  "VARCHAR(256)"),
        ("vpn_users", "archived",       "BOOLEAN DEFAULT 0"),
        ("vpn_users", "full_name",      "VARCHAR(256)"),
        ("settings",  "smtp_host",      "VARCHAR(256)"),
        ("settings",  "smtp_port",      "INTEGER DEFAULT 587"),
        ("settings",  "smtp_user",      "VARCHAR(256)"),
        ("settings",  "smtp_password",  "VARCHAR(256)"),
        ("settings",  "smtp_from",      "VARCHAR(256)"),
        ("settings",  "smtp_tls",       "BOOLEAN DEFAULT 1"),
        ("settings",  "backup_enabled", "BOOLEAN DEFAULT 0"),
        ("settings",  "backup_interval_hours", "INTEGER DEFAULT 24"),
        ("settings",  "backup_keep",    "INTEGER DEFAULT 7"),
        ("settings",  "public_url",     "VARCHAR(256)"),
        ("settings",  "public_urls",    "TEXT"),
        ("settings",  "share_ttl_hours", "INTEGER DEFAULT 72"),
        ("settings",  "share_max_downloads", "INTEGER DEFAULT 5"),
        ("settings",  "expiry_notify_enabled", "BOOLEAN DEFAULT 0"),
        ("settings",  "autoban_enabled", "BOOLEAN DEFAULT 0"),
        ("settings",  "autoban_threshold", "INTEGER DEFAULT 10"),
        ("vpn_users", "expiry_notified", "INTEGER DEFAULT 0"),
        ("connection_attempts", "country", "VARCHAR(64)"),
        ("connection_attempts", "country_code", "VARCHAR(4)"),
        ("vpn_users", "plan_id", "INTEGER REFERENCES plans(id)"),
        ("vpn_users", "paid_until", "DATETIME"),
        ("vpn_users", "traffic_quota", "INTEGER DEFAULT 0"),
        ("vpn_users", "traffic_used", "INTEGER DEFAULT 0"),
        ("vpn_users", "billing_blocked", "BOOLEAN DEFAULT 0"),
        ("vpn_users", "notes",          "TEXT"),
        ("vpn_users", "last_connected_at", "DATETIME"),
        ("vpn_servers", "obfuscation",   "BOOLEAN DEFAULT 0"),
        ("vpn_servers", "tls_crypt_key", "TEXT"),
        ("vpn_servers", "kind",          "VARCHAR(16) DEFAULT 'openvpn'"),
        ("vpn_servers", "wg_private_key","TEXT"),
        ("vpn_servers", "wg_public_key", "TEXT"),
        ("vpn_servers", "awg_params",    "TEXT"),
        ("vpn_servers", "ikev2_cert_pem","TEXT"),
        ("vpn_servers", "ikev2_key_pem", "TEXT"),
        ("vpn_users",   "eap_password",  "VARCHAR(128)"),
        ("vpn_users",   "wg_private_key","TEXT"),
        ("vpn_users",   "wg_public_key", "TEXT"),
        ("vpn_users",   "wg_address",    "VARCHAR(64)"),
        ("s2s_sites",   "psk",           "TEXT"),
        ("s2s_sites",   "backup_of",     "INTEGER"),
        ("vpn_users",   "route_profile_id", "INTEGER REFERENCES route_profiles(id)"),
        ("plans",       "route_profile_id", "INTEGER REFERENCES route_profiles(id)"),
    ]
    import sqlalchemy as sa
    with engine.connect() as conn:
        for table, column, col_type in migrations:
            try:
                conn.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
                conn.commit()
            except Exception:
                pass  # колонка уже существует

    # Делаем vpn_users.ca_id nullable (WireGuard/IKEv2 не используют CA).
    # SQLite не умеет ALTER COLUMN — патчим схему через writable_schema.
    try:
        with engine.connect() as conn:
            info = conn.execute(sa.text("PRAGMA table_info(vpn_users)")).fetchall()
            ca_notnull = any(r[1] == "ca_id" and r[3] == 1 for r in info)
            if ca_notnull:
                conn.execute(sa.text("PRAGMA writable_schema=ON"))
                conn.execute(sa.text(
                    "UPDATE sqlite_master SET sql=REPLACE(sql,'ca_id INTEGER NOT NULL','ca_id INTEGER') "
                    "WHERE type='table' AND name='vpn_users'"
                ))
                conn.execute(sa.text("PRAGMA writable_schema=OFF"))
                conn.commit()
        engine.dispose()  # форсируем переоткрытие — SQLite перечитает схему
    except Exception:
        pass


def _seed_defaults():
    """Создаёт первого admin пользователя и строку настроек если их нет."""
    db = SessionLocal()
    try:
        if not db.query(AdminUser).first():
            default_password = os.getenv("ADMIN_PASSWORD", "admin")
            admin = AdminUser(
                username="admin",
                password_hash=hash_password(default_password),
            )
            db.add(admin)
            db.commit()

        if not db.query(Settings).filter(Settings.id == 1).first():
            db.add(Settings(id=1))
            db.commit()

        # Регистрация модулей-расширений (выключены по умолчанию)
        try:
            from services import modules as _mod
            _mod.seed(db)
        except Exception:
            db.rollback()

        # Автосоздание корневого CA при первом запуске (для OpenVPN/IKEv2).
        from models import CA
        if not db.query(CA).first():
            try:
                from services import pki
                cert_pem, key_pem, expires_at = pki.create_ca(
                    common_name="VPN", country="RU", org="Click VPN", valid_days=3650,
                )
                db.add(CA(common_name="VPN", cert_pem=cert_pem, key_pem=key_pem,
                          expires_at=expires_at))
                db.commit()
            except Exception:
                db.rollback()
    finally:
        db.close()


app = FastAPI(
    title="Click VPN",
    description="Self-hosted OpenVPN management panel",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API роутеры
app.include_router(auth.router)
app.include_router(settings.router)
app.include_router(servers.router)
app.include_router(users.router)
app.include_router(status.router)
app.include_router(organizations.router)
app.include_router(logs.router)
app.include_router(system.router)
app.include_router(audit.router)
app.include_router(backup.router)
app.include_router(stats.router)
app.include_router(download.router)
app.include_router(modules.router)
app.include_router(billing.router)
app.include_router(s2s.router)
app.include_router(updates.router)
app.include_router(routes_router.router)

# Статика и шаблоны
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
templates = Jinja2Templates(directory=os.path.join(FRONTEND_DIR, "templates"))

static_dir = os.path.join(FRONTEND_DIR, "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/{full_path:path}", response_class=HTMLResponse, include_in_schema=False)
async def serve_spa(request: Request, full_path: str):
    """Все не-API маршруты отдают SPA."""
    return templates.TemplateResponse("index.html", {"request": request})
