"""Site-to-Site API.

Все эндпоинты защищены модулем site2site.
"""
import ipaddress
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db
from models import AdminUser, Site, SiteSubnet, AccessRule
from auth import get_current_user
from services import modules as mod
from services import audit

# Импортируем транспорты (регистрируют себя при импорте)
import services.s2s.wireguard  # noqa: F401
from services.s2s import transport as tr

router = APIRouter(prefix="/api/s2s", tags=["site2site"])


def _require_module(db: Session):
    if not mod.is_enabled(db, "site2site"):
        raise HTTPException(403, "Модуль site2site не включён")


def _hub_of_spoke(db: Session, spoke: Site) -> Site | None:
    if spoke.role == "hub":
        return spoke
    if spoke.hub_id:
        return db.query(Site).filter(Site.id == spoke.hub_id).first()
    return None


def _site_dict(site: Site, status_map: dict | None = None) -> dict:
    d = {
        "id": site.id,
        "name": site.name,
        "role": site.role,
        "hub_id": site.hub_id,
        "transport": site.transport,
        "endpoint": site.endpoint,
        "wg_public_key": site.wg_public_key,
        "tunnel_ip": site.tunnel_ip,
        "tunnel_network": site.tunnel_network,
        "tunnel_port": site.tunnel_port,
        "created_at": site.created_at.isoformat() if site.created_at else None,
        "subnets": [{"id": s.id, "cidr": s.cidr, "comment": s.comment} for s in site.subnets],
        "online": False,
        "last_handshake": 0,
    }
    if status_map and site.wg_public_key and site.wg_public_key in status_map:
        peer = status_map[site.wg_public_key]
        d["online"] = peer["online"]
        d["last_handshake"] = peer["last_handshake"]
        d["rx"] = peer.get("rx", 0)
        d["tx"] = peer.get("tx", 0)
    return d


# ──────────────────── Схемы ────────────────────

class SiteCreate(BaseModel):
    name: str
    role: str = "spoke"
    hub_id: int | None = None
    transport: str = "wireguard"
    endpoint: str | None = None
    tunnel_network: str | None = None   # обязательно для hub
    tunnel_port: int = 51900
    subnets: list[str] = []             # список CIDR


class SiteUpdate(BaseModel):
    name: str | None = None
    endpoint: str | None = None
    tunnel_network: str | None = None
    tunnel_port: int | None = None
    subnets: list[str] | None = None


# ──────────────────── Helpers ────────────────────

def _used_tunnel_ips(db: Session) -> list[str]:
    return [s.tunnel_ip for s in db.query(Site).all() if s.tunnel_ip]


def _validate_subnets(cidrs: list[str], db: Session, exclude_site_id: int | None = None):
    """Проверка уникальности подсетей между площадками."""
    existing = db.query(SiteSubnet).all()
    existing_cidrs = [s.cidr for s in existing
                      if exclude_site_id is None or s.site_id != exclude_site_id]
    for cidr in cidrs:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            raise HTTPException(400, f"Некорректный CIDR: {cidr}")
        for ex in existing_cidrs:
            try:
                ex_net = ipaddress.ip_network(ex, strict=False)
                if net.overlaps(ex_net):
                    raise HTTPException(409, f"CIDR {cidr} пересекается с {ex} другой площадки")
            except HTTPException:
                raise
            except Exception:
                pass


# ──────────────────── Эндпоинты ────────────────────

@router.get("/sites")
def list_sites(db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    _require_module(db)
    sites = db.query(Site).order_by(Site.role.desc(), Site.id).all()

    # статус туннелей хабов
    status_map: dict[str, dict] = {}
    hubs = [s for s in sites if s.role == "hub"]
    for hub in hubs:
        try:
            transport = tr.get(hub.transport)
            for peer in transport.status(hub):
                status_map[peer["pubkey"]] = peer
        except Exception:
            pass

    return [_site_dict(s, status_map) for s in sites]


@router.post("/sites")
def create_site(body: SiteCreate, db: Session = Depends(get_db),
                admin: AdminUser = Depends(get_current_user)):
    _require_module(db)

    if body.role == "hub" and not body.tunnel_network:
        raise HTTPException(400, "Для хаба укажите tunnel_network (напр. 10.100.0.0/24)")
    if body.role == "spoke":
        if not body.hub_id:
            raise HTTPException(400, "Для спицы укажите hub_id")
        hub = db.query(Site).filter(Site.id == body.hub_id, Site.role == "hub").first()
        if not hub:
            raise HTTPException(404, "Хаб не найден")

    if body.subnets:
        _validate_subnets(body.subnets, db)

    # генерация ключевой пары WG
    from services.s2s.wireguard import gen_keypair
    priv, pub = gen_keypair()

    site = Site(
        name=body.name,
        role=body.role,
        hub_id=body.hub_id if body.role == "spoke" else None,
        transport=body.transport,
        endpoint=body.endpoint,
        wg_private_key=priv,
        wg_public_key=pub,
        tunnel_network=body.tunnel_network if body.role == "hub" else None,
        tunnel_port=body.tunnel_port,
    )

    # назначение tunnel_ip
    if body.role == "hub" and body.tunnel_network:
        from services.s2s.wireguard import _hub_tunnel_ip
        site.tunnel_ip = _hub_tunnel_ip(body.tunnel_network)
    elif body.role == "spoke" and hub.tunnel_network:
        from services.s2s.wireguard import _next_spoke_ip
        site.tunnel_ip = _next_spoke_ip(hub.tunnel_network, _used_tunnel_ips(db))

    db.add(site)
    db.flush()

    for cidr in body.subnets:
        db.add(SiteSubnet(site_id=site.id, cidr=cidr))

    db.commit()
    db.refresh(site)
    audit.log(db, admin.username, "s2s.site.create", site.name)
    return _site_dict(site)


@router.put("/sites/{site_id}")
def update_site(site_id: int, body: SiteUpdate, db: Session = Depends(get_db),
                admin: AdminUser = Depends(get_current_user)):
    _require_module(db)
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        raise HTTPException(404, "Площадка не найдена")

    if body.name is not None:
        site.name = body.name
    if body.endpoint is not None:
        site.endpoint = body.endpoint
    if body.tunnel_network is not None and site.role == "hub":
        site.tunnel_network = body.tunnel_network
    if body.tunnel_port is not None:
        site.tunnel_port = body.tunnel_port

    if body.subnets is not None:
        _validate_subnets(body.subnets, db, exclude_site_id=site_id)
        db.query(SiteSubnet).filter(SiteSubnet.site_id == site_id).delete()
        for cidr in body.subnets:
            db.add(SiteSubnet(site_id=site_id, cidr=cidr))

    db.commit()
    db.refresh(site)
    audit.log(db, admin.username, "s2s.site.update", site.name)
    return _site_dict(site)


@router.delete("/sites/{site_id}")
def delete_site(site_id: int, db: Session = Depends(get_db),
                admin: AdminUser = Depends(get_current_user)):
    _require_module(db)
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        raise HTTPException(404, "Площадка не найдена")

    # для хаба — сначала остановить туннель
    if site.role == "hub":
        try:
            transport = tr.get(site.transport)
            transport.hub_teardown(site)
        except Exception:
            pass

    db.query(SiteSubnet).filter(SiteSubnet.site_id == site_id).delete()
    db.query(AccessRule).filter(
        (AccessRule.src_site_id == site_id) | (AccessRule.dst_site_id == site_id)
    ).delete()
    db.delete(site)
    db.commit()
    audit.log(db, admin.username, "s2s.site.delete", site.name)
    return {"ok": True}


@router.post("/apply")
def apply_topology(db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_user)):
    """Применить топологию: поднять/обновить туннели на всех хабах."""
    _require_module(db)
    hubs = db.query(Site).filter(Site.role == "hub").all()
    results = []
    for hub in hubs:
        spokes = db.query(Site).filter(Site.hub_id == hub.id).all()
        try:
            transport = tr.get(hub.transport)
            ok, msg = transport.hub_apply(hub, spokes)
            results.append({"hub": hub.name, "ok": ok, "msg": msg})
        except Exception as e:
            results.append({"hub": hub.name, "ok": False, "msg": str(e)})
    audit.log(db, admin.username, "s2s.apply", f"{len(hubs)} hub(s)")
    return {"results": results}


@router.get("/sites/{site_id}/config", response_class=PlainTextResponse)
def get_site_config(site_id: int, db: Session = Depends(get_db),
                    _: AdminUser = Depends(get_current_user)):
    """Скачать WG-конфиг для роутера площадки."""
    _require_module(db)
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        raise HTTPException(404, "Площадка не найдена")
    if site.role == "hub":
        raise HTTPException(400, "Конфиг площадки не нужен для хаба")

    hub = db.query(Site).filter(Site.id == site.hub_id).first()
    if not hub:
        raise HTTPException(404, "Хаб площадки не найден")

    transport = tr.get(hub.transport)
    cfg = transport.site_config(hub, site)
    return PlainTextResponse(cfg, media_type="text/plain",
                             headers={"Content-Disposition": f'attachment; filename="{site.name}.conf"'})


@router.get("/status")
def get_status(db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    _require_module(db)
    hubs = db.query(Site).filter(Site.role == "hub").all()
    result = {}
    for hub in hubs:
        try:
            transport = tr.get(hub.transport)
            result[hub.id] = transport.status(hub)
        except Exception as e:
            result[hub.id] = {"error": str(e)}
    return result
