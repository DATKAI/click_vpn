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
from routers import auth, settings, servers, users, status, organizations

DATA_DIR = os.getenv("DATA_DIR", "./data")
os.makedirs(os.path.join(DATA_DIR, "pki"), exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, "openvpn"), exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _migrate_db()
    _seed_defaults()
    yield


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
    ]
    with engine.connect() as conn:
        for table, column, col_type in migrations:
            try:
                conn.execute(
                    __import__("sqlalchemy").text(
                        f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
                    )
                )
                conn.commit()
            except Exception:
                pass  # колонка уже существует


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
