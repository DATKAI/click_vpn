"""Биллинг-модуль: тарифы, назначение клиентам, продление.

Доступен только когда модуль 'billing' включён (раздел Модули).
Лимиты: срок (paid_until), трафик (traffic_quota), скорость (plan.speed_mbps).
Автоблокировка — в services/billing.py (фоновая проверка).
"""
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import AdminUser, Plan, VPNUser
from auth import get_current_user
from services import audit, modules as mod

router = APIRouter(prefix="/api/billing", tags=["billing"])


def _require_module(db: Session):
    if not mod.is_enabled(db, "billing"):
        raise HTTPException(403, "Модуль «Биллинг» выключен")


class PlanIn(BaseModel):
    name: str
    price: int = 0
    traffic_gb: int = 0
    duration_days: int = 30
    speed_mbps: int = 0
    route_profile_id: int | None = None   # профиль селективной маршрутизации


class AssignIn(BaseModel):
    plan_id: int


@router.get("/plans")
def list_plans(db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    _require_module(db)
    return [_plan_out(p) for p in db.query(Plan).order_by(Plan.id.asc()).all()]


@router.get("/summary")
def summary(db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    """Сводка по биллингу: клиенты по статусам + текущий доход (MRR)."""
    _require_module(db)
    plans = {p.id: p for p in db.query(Plan).all()}
    users = db.query(VPNUser).filter(VPNUser.plan_id.isnot(None), VPNUser.archived == False).all()
    now = datetime.utcnow()
    total = len(users)
    paid = expired = unpaid = blocked = revenue = 0
    for u in users:
        plan = plans.get(u.plan_id)
        if u.billing_blocked:
            blocked += 1
        if u.paid_until and u.paid_until > now:
            paid += 1
            revenue += (plan.price if plan else 0)
        elif u.paid_until and u.paid_until <= now:
            expired += 1
        else:
            unpaid += 1
    return {"total": total, "paid": paid, "expired": expired,
            "unpaid": unpaid, "blocked": blocked, "revenue": revenue}


def _plan_out(p: Plan) -> dict:
    return {"id": p.id, "name": p.name, "price": p.price, "traffic_gb": p.traffic_gb,
            "duration_days": p.duration_days, "speed_mbps": p.speed_mbps,
            "route_profile_id": p.route_profile_id}


@router.post("/plans")
def create_plan(data: PlanIn, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_user)):
    _require_module(db)
    p = Plan(name=data.name.strip(), price=data.price, traffic_gb=data.traffic_gb,
             duration_days=data.duration_days, speed_mbps=data.speed_mbps,
             route_profile_id=data.route_profile_id)
    db.add(p); db.commit(); db.refresh(p)
    audit.log(db, admin.username, "billing.plan_create", p.name)
    return _plan_out(p)


@router.put("/plans/{plan_id}")
def update_plan(plan_id: int, data: PlanIn, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_user)):
    _require_module(db)
    p = db.query(Plan).filter(Plan.id == plan_id).first()
    if not p:
        raise HTTPException(404, "Тариф не найден")
    p.name = data.name.strip(); p.price = data.price; p.traffic_gb = data.traffic_gb
    p.duration_days = data.duration_days; p.speed_mbps = data.speed_mbps
    p.route_profile_id = data.route_profile_id
    db.commit()
    audit.log(db, admin.username, "billing.plan_update", p.name)
    return _plan_out(p)


@router.delete("/plans/{plan_id}", status_code=204)
def delete_plan(plan_id: int, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_user)):
    _require_module(db)
    p = db.query(Plan).filter(Plan.id == plan_id).first()
    if not p:
        raise HTTPException(404, "Тариф не найден")
    if db.query(VPNUser).filter(VPNUser.plan_id == plan_id).count():
        raise HTTPException(400, "Нельзя удалить — тариф назначен клиентам")
    db.delete(p); db.commit()
    audit.log(db, admin.username, "billing.plan_delete", p.name)


@router.post("/users/{user_id}/assign")
def assign_plan(user_id: int, data: AssignIn, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_user)):
    """Назначить тариф клиенту (без активации оплаты)."""
    _require_module(db)
    user = db.query(VPNUser).filter(VPNUser.id == user_id).first()
    if not user:
        raise HTTPException(404, "Клиент не найден")
    plan = db.query(Plan).filter(Plan.id == data.plan_id).first()
    if not plan:
        raise HTTPException(404, "Тариф не найден")
    user.plan_id = plan.id
    user.traffic_quota = (plan.traffic_gb or 0) * 1024 * 1024 * 1024
    # профиль маршрутов тарифа → клиенту (если задан в тарифе)
    if getattr(plan, "route_profile_id", None):
        user.route_profile_id = plan.route_profile_id
    db.commit()
    audit.log(db, admin.username, "billing.assign", user.username, plan.name)

    # сразу применяем лимиты (не ждём фоновую проверку)
    try:
        from database import SessionLocal
        from services import billing as billing_svc
        billing_svc._check_once(SessionLocal)
    except Exception:
        pass
    return {"status": "ok"}


@router.post("/recheck")
def recheck(db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    """Принудительно применить лимиты ко всем клиентам сейчас."""
    _require_module(db)
    from database import SessionLocal
    from services import billing as billing_svc
    billing_svc._check_once(SessionLocal)
    return {"status": "ok"}


@router.post("/users/{user_id}/pay")
def pay(user_id: int, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_user)):
    """Отметить оплату: продлить срок на duration тарифа, обнулить трафик,
    разблокировать. Используется при ручном приёме оплаты."""
    _require_module(db)
    user = db.query(VPNUser).filter(VPNUser.id == user_id).first()
    if not user:
        raise HTTPException(404, "Клиент не найден")
    plan = db.query(Plan).filter(Plan.id == user.plan_id).first()
    if not plan:
        raise HTTPException(400, "Клиенту не назначен тариф")
    base = user.paid_until if (user.paid_until and user.paid_until > datetime.utcnow()) else datetime.utcnow()
    user.paid_until = base + timedelta(days=plan.duration_days) if plan.duration_days else None
    user.traffic_quota = (plan.traffic_gb or 0) * 1024 * 1024 * 1024
    user.traffic_used = 0
    user.billing_blocked = False
    # снимаем биллинг-блокировку доступа
    if not user.archived:
        user.is_active = True
        from models import CertStatus
        user.cert_status = CertStatus.active
        user.revoked_at = None
    db.commit()

    # применяем разблокировку (CRL/resync)
    try:
        from routers.users import _apply_user_change
        _apply_user_change(db, user)
    except Exception:
        pass
    audit.log(db, admin.username, "billing.pay", user.username,
              f"до {user.paid_until.date() if user.paid_until else 'бессрочно'}")
    return {"status": "ok", "paid_until": user.paid_until.isoformat() if user.paid_until else None}
