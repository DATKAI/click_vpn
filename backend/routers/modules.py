"""Управление модулями: список, включение/выключение."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models import AdminUser, Module
from auth import get_current_user
from services import audit, modules as mod

router = APIRouter(prefix="/api/modules", tags=["modules"])


@router.get("")
def list_all(db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    return mod.list_modules(db)


@router.post("/{name}/toggle")
def toggle(name: str, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_user)):
    if name not in mod.REGISTRY:
        raise HTTPException(404, "Модуль не найден")
    m = db.query(Module).filter(Module.name == name).first()
    if not m:
        m = Module(name=name, enabled=False)
        db.add(m)
        db.flush()
    m.enabled = not m.enabled
    db.commit()
    audit.log(db, admin.username, "module.toggle", name, "enabled" if m.enabled else "disabled")
    return {"name": name, "enabled": m.enabled}
