"""Управление версиями приложения: просмотр, обновление, откат."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db
from models import AdminUser
from auth import get_current_user
from services import audit, updater

router = APIRouter(prefix="/api/updates", tags=["updates"])


@router.get("/current")
def get_current(_: AdminUser = Depends(get_current_user)):
    return updater.current()


@router.get("/versions")
def get_versions(_: AdminUser = Depends(get_current_user)):
    try:
        updater.fetch()
    except Exception:
        pass   # офлайн — покажем что есть локально
    return updater.list_versions()


@router.get("/changelog")
def get_changelog(ref: str, _: AdminUser = Depends(get_current_user)):
    try:
        return {"commits": updater.changelog(ref)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/status")
def get_status(_: AdminUser = Depends(get_current_user)):
    return updater.status()


class ApplyReq(BaseModel):
    ref: str   # тег, коммит или "latest"


@router.post("/apply")
def apply_version(body: ApplyReq, db: Session = Depends(get_db),
                  admin: AdminUser = Depends(get_current_user)):
    try:
        res = updater.apply(body.ref)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Не удалось запустить обновление: {e}")
    audit.log(db, admin.username, "update.apply", body.ref)
    return res
