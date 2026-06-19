"""Установка опциональных компонентов сервера из панели."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models import AdminUser
from auth import get_current_user
from services import audit, components

router = APIRouter(prefix="/api/components", tags=["components"])


@router.get("")
def list_components(_: AdminUser = Depends(get_current_user)):
    return components.status()


@router.post("/{cid}/install")
def install_component(cid: str, db: Session = Depends(get_db),
                      admin: AdminUser = Depends(get_current_user)):
    try:
        res = components.install(cid)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except FileNotFoundError as e:
        raise HTTPException(500, str(e))
    audit.log(db, admin.username, "component.install", cid)
    return res


@router.get("/{cid}/log")
def component_log(cid: str, _: AdminUser = Depends(get_current_user)):
    return {"log": components.log(cid)}
