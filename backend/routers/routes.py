"""Селективная маршрутизация: профили маршрутов + источники. Модуль selective_routing."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db
from models import AdminUser, RouteProfile, RouteSource, VPNUser
from auth import get_current_user
from services import audit, modules as mod, routelists

router = APIRouter(prefix="/api/routes", tags=["selective_routing"])


def _require(db: Session):
    if not mod.is_enabled(db, "selective_routing"):
        raise HTTPException(403, "Модуль «Селективная маршрутизация» не включён")


def _profile_dict(p: RouteProfile) -> dict:
    return {
        "id": p.id, "name": p.name, "mode": p.mode,
        "dns_through_tunnel": p.dns_through_tunnel, "dns_server": p.dns_server,
        "prefix_count": p.prefix_count,
        "compiled_at": p.compiled_at.isoformat() if p.compiled_at else None,
        "sources": [{"id": s.id, "kind": s.kind, "value": s.value, "enabled": s.enabled}
                    for s in p.sources],
    }


class ProfileReq(BaseModel):
    name: str
    mode: str = "selective"
    dns_through_tunnel: bool = False
    dns_server: str | None = None


class SourceReq(BaseModel):
    kind: str            # provider|asn|url_list|domain|cidr
    value: str
    enabled: bool = True


@router.get("/profiles")
def list_profiles(db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    _require(db)
    return [_profile_dict(p) for p in db.query(RouteProfile).order_by(RouteProfile.id).all()]


@router.post("/profiles")
def create_profile(body: ProfileReq, db: Session = Depends(get_db),
                   admin: AdminUser = Depends(get_current_user)):
    _require(db)
    if body.mode not in ("selective", "full", "exclude"):
        raise HTTPException(400, "mode: selective|full|exclude")
    p = RouteProfile(name=body.name, mode=body.mode,
                     dns_through_tunnel=body.dns_through_tunnel, dns_server=body.dns_server)
    db.add(p)
    db.commit()
    db.refresh(p)
    audit.log(db, admin.username, "routes.profile.create", body.name)
    return _profile_dict(p)


@router.put("/profiles/{pid}")
def update_profile(pid: int, body: ProfileReq, db: Session = Depends(get_db),
                   admin: AdminUser = Depends(get_current_user)):
    _require(db)
    p = db.query(RouteProfile).filter(RouteProfile.id == pid).first()
    if not p:
        raise HTTPException(404, "Профиль не найден")
    p.name = body.name
    p.mode = body.mode
    p.dns_through_tunnel = body.dns_through_tunnel
    p.dns_server = body.dns_server
    db.commit()
    audit.log(db, admin.username, "routes.profile.update", body.name)
    return _profile_dict(p)


@router.delete("/profiles/{pid}")
def delete_profile(pid: int, db: Session = Depends(get_db),
                   admin: AdminUser = Depends(get_current_user)):
    _require(db)
    p = db.query(RouteProfile).filter(RouteProfile.id == pid).first()
    if not p:
        raise HTTPException(404, "Профиль не найден")
    # отвязать у клиентов
    db.query(VPNUser).filter(VPNUser.route_profile_id == pid).update({VPNUser.route_profile_id: None})
    db.delete(p)
    db.commit()
    audit.log(db, admin.username, "routes.profile.delete", p.name)
    return {"ok": True}


@router.post("/profiles/{pid}/sources")
def add_source(pid: int, body: SourceReq, db: Session = Depends(get_db),
               admin: AdminUser = Depends(get_current_user)):
    _require(db)
    if body.kind not in ("provider", "asn", "url_list", "domain", "cidr"):
        raise HTTPException(400, "kind: provider|asn|url_list|domain|cidr")
    p = db.query(RouteProfile).filter(RouteProfile.id == pid).first()
    if not p:
        raise HTTPException(404, "Профиль не найден")
    db.add(RouteSource(profile_id=pid, kind=body.kind, value=body.value.strip(),
                       enabled=body.enabled))
    db.commit()
    return _profile_dict(db.query(RouteProfile).filter(RouteProfile.id == pid).first())


@router.delete("/sources/{sid}")
def del_source(sid: int, db: Session = Depends(get_db),
               _: AdminUser = Depends(get_current_user)):
    _require(db)
    s = db.query(RouteSource).filter(RouteSource.id == sid).first()
    if not s:
        raise HTTPException(404, "Источник не найден")
    pid = s.profile_id
    db.delete(s)
    db.commit()
    return _profile_dict(db.query(RouteProfile).filter(RouteProfile.id == pid).first())


@router.post("/profiles/{pid}/compile")
def compile_profile(pid: int, db: Session = Depends(get_db),
                    admin: AdminUser = Depends(get_current_user)):
    _require(db)
    p = db.query(RouteProfile).filter(RouteProfile.id == pid).first()
    if not p:
        raise HTTPException(404, "Профиль не найден")
    n = routelists.compile_profile(db, p)
    audit.log(db, admin.username, "routes.compile", f"{p.name}: {n} CIDR")
    return {"ok": True, "prefix_count": n}


@router.get("/profiles/{pid}/preview")
def preview(pid: int, db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    _require(db)
    return {"cidrs": routelists.load_cidrs(pid)[:500]}


class AssignReq(BaseModel):
    user_id: int
    profile_id: int | None = None   # None — снять профиль (full tunnel по умолчанию)


@router.post("/assign")
def assign_to_user(body: AssignReq, db: Session = Depends(get_db),
                   admin: AdminUser = Depends(get_current_user)):
    _require(db)
    user = db.query(VPNUser).filter(VPNUser.id == body.user_id).first()
    if not user:
        raise HTTPException(404, "Клиент не найден")
    if body.profile_id is not None:
        if not db.query(RouteProfile).filter(RouteProfile.id == body.profile_id).first():
            raise HTTPException(404, "Профиль не найден")
    user.route_profile_id = body.profile_id
    db.commit()
    audit.log(db, admin.username, "routes.assign", user.username, str(body.profile_id))
    return {"ok": True}
