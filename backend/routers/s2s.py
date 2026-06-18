"""Site-to-Site API.

Все эндпоинты защищены модулем site2site.
"""
import ipaddress
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db
from models import AdminUser, Site, SiteSubnet, AccessRule, Module
from auth import get_current_user
from services import modules as mod
from services import audit

# Импортируем транспорты (регистрируют себя при импорте)
import services.s2s.wireguard  # noqa: F401
import services.s2s.ipsec      # noqa: F401
import services.s2s.gre        # noqa: F401
from services.s2s import transport as tr
from services.s2s import netutil

router = APIRouter(prefix="/api/s2s", tags=["site2site"])


def _require_module(db: Session):
    if not mod.is_enabled(db, "site2site"):
        raise HTTPException(403, "Модуль site2site не включён")


DEFAULT_POLICY = "allow_all"   # allow_all | deny_all


def _get_config(db: Session) -> dict:
    """Конфиг модуля site2site (Module.config JSON)."""
    m = db.query(Module).filter(Module.name == "site2site").first()
    cfg = {}
    if m and m.config:
        try:
            cfg = json.loads(m.config)
        except Exception:
            cfg = {}
    cfg.setdefault("default_policy", DEFAULT_POLICY)
    return cfg


def _set_config(db: Session, cfg: dict):
    m = db.query(Module).filter(Module.name == "site2site").first()
    if not m:
        m = Module(name="site2site", enabled=True)
        db.add(m)
        db.flush()
    m.config = json.dumps(cfg)
    db.commit()


def _build_is_allowed(db: Session):
    """Возвращает callable(src_site_id, dst_site_id) -> bool по матрице доступа.

    Явное правило AccessRule переопределяет политику по умолчанию.
    """
    policy = _get_config(db).get("default_policy", DEFAULT_POLICY)
    rules = {(r.src_site_id, r.dst_site_id): r.allow for r in db.query(AccessRule).all()}
    default_allow = (policy != "deny_all")

    def is_allowed(src_id: int, dst_id: int) -> bool:
        if src_id == dst_id:
            return True
        if (src_id, dst_id) in rules:
            return rules[(src_id, dst_id)]
        return default_allow

    return is_allowed


def _hub_of_spoke(db: Session, spoke: Site) -> Site | None:
    if spoke.role == "hub":
        return spoke
    if spoke.hub_id:
        return db.query(Site).filter(Site.id == spoke.hub_id).first()
    return None


def _site_dict(site: Site, wg_status: dict | None = None,
               spoke_online: dict | None = None) -> dict:
    d = {
        "id": site.id,
        "name": site.name,
        "role": site.role,
        "hub_id": site.hub_id,
        "backup_of": site.backup_of,
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
        "active_transports": [],   # для хаба: транспорты, используемые его спицами
    }
    if site.transport == "wireguard" and wg_status and site.wg_public_key in wg_status:
        peer = wg_status[site.wg_public_key]
        d["online"] = peer["online"]
        d["last_handshake"] = peer["last_handshake"]
        d["rx"] = peer.get("rx", 0)
        d["tx"] = peer.get("tx", 0)
    elif site.transport in ("ipsec", "gre", "gre_ipsec") and spoke_online and site.id in spoke_online:
        d["online"] = spoke_online[site.id]
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
    backup_of: int | None = None        # резервный канал для спицы (общая LAN)


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

    # статус туннелей хабов по всем транспортам
    wg_status: dict[str, dict] = {}       # pubkey -> peer (WireGuard)
    spoke_online: dict[int, bool] = {}    # spoke_id -> online (IPsec/GRE)
    hubs = [s for s in sites if s.role == "hub"]
    for hub in hubs:
        try:
            for peer in tr.get("wireguard").status(hub):
                wg_status[peer["pubkey"]] = peer
        except Exception:
            pass
        for tname in ("ipsec", "gre", "gre_ipsec"):
            try:
                for sa in tr.get(tname).status(hub):
                    spoke_online[sa["spoke_id"]] = sa["online"]
            except Exception:
                pass

    # активные транспорты каждого хаба = транспорты его спиц
    by_hub: dict[int, set] = {}
    for s in sites:
        if s.role == "spoke" and s.hub_id:
            by_hub.setdefault(s.hub_id, set()).add(s.transport)

    out = []
    for s in sites:
        d = _site_dict(s, wg_status, spoke_online)
        if s.role == "hub":
            d["active_transports"] = sorted(by_hub.get(s.id, set()))
        out.append(d)
    return out


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

    if body.transport not in tr.available():
        raise HTTPException(400, f"Транспорт '{body.transport}' не поддерживается")

    # Резервный канал: общая LAN с основной спицей, проверка пересечения снимается.
    primary = None
    if body.role == "spoke" and body.backup_of:
        primary = db.query(Site).filter(Site.id == body.backup_of, Site.role == "spoke").first()
        if not primary:
            raise HTTPException(404, "Основная спица для резерва не найдена")
        if primary.backup_of:
            raise HTTPException(400, "Резерв нельзя делать для резервного канала")
        if primary.hub_id != body.hub_id:
            raise HTTPException(400, "Резерв должен быть на том же хабе, что и основная спица")

    # LAN: у резерва — копия подсетей основной спицы; иначе из тела (с проверкой)
    eff_subnets = [s.cidr for s in primary.subnets] if primary else body.subnets
    if not primary and body.subnets:
        _validate_subnets(body.subnets, db)

    site = Site(
        name=body.name,
        role=body.role,
        hub_id=body.hub_id if body.role == "spoke" else None,
        # хаб — мультипротокольный концентратор, не привязан к одному транспорту
        transport="hub" if body.role == "hub" else body.transport,
        endpoint=body.endpoint,
        tunnel_network=body.tunnel_network if body.role == "hub" else None,
        tunnel_port=body.tunnel_port,
        backup_of=body.backup_of if body.role == "spoke" else None,
    )

    # Хаб всегда имеет WG-ключи (обслуживает WG-спицы) + адрес в туннельной сети.
    # Спица: WG-ключи для wireguard, PSK для ipsec.
    from services.s2s.wireguard import gen_keypair, _hub_tunnel_ip, _next_spoke_ip
    if body.role == "hub":
        site.wg_private_key, site.wg_public_key = gen_keypair()
        if body.tunnel_network:
            site.tunnel_ip = _hub_tunnel_ip(body.tunnel_network)
    else:  # spoke
        if body.transport == "wireguard":
            site.wg_private_key, site.wg_public_key = gen_keypair()
            if hub.tunnel_network:
                site.tunnel_ip = _next_spoke_ip(hub.tunnel_network, _used_tunnel_ips(db))
        elif body.transport == "ipsec":
            from services.s2s.ipsec import gen_psk
            site.psk = gen_psk()
        elif body.transport in ("gre", "gre_ipsec"):
            # GRE-эндпоинт статичен → нужен публичный IP филиала
            if not body.endpoint:
                raise HTTPException(400, "Для GRE укажите endpoint (публичный IP филиала)")
            if hub.tunnel_network:
                site.tunnel_ip = _next_spoke_ip(hub.tunnel_network, _used_tunnel_ips(db))
            if body.transport == "gre_ipsec":
                from services.s2s.ipsec import gen_psk
                site.psk = gen_psk()

    db.add(site)
    db.flush()

    for cidr in eff_subnets:
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

    # для хаба — остановить все транспорты + снять матрицу
    if site.role == "hub":
        for tname in tr.available():
            try:
                tr.get(tname).hub_teardown(site)
            except Exception:
                pass
        try:
            netutil.clear_forward_matrix(site.id)
        except Exception:
            pass

    # резервные каналы этой спицы становятся самостоятельными (не сироты)
    db.query(Site).filter(Site.backup_of == site_id).update({Site.backup_of: None})

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
    is_allowed = _build_is_allowed(db)
    results = []
    for hub in hubs:
        spokes = db.query(Site).filter(Site.hub_id == hub.id).all()
        # хаб поднимает только те транспорты, которые реально используют его спицы;
        # неиспользуемые сворачиваются (чтобы не висел пустой WG-интерфейс и т.п.)
        used = {s.transport for s in spokes}
        for tname in tr.available():
            if tname in used:
                try:
                    ok, msg = tr.get(tname).hub_apply(hub, spokes, is_allowed)
                    results.append({"hub": f"{hub.name} [{tname}]", "ok": ok, "msg": msg})
                except Exception as e:
                    results.append({"hub": f"{hub.name} [{tname}]", "ok": False, "msg": str(e)})
            else:
                try:
                    tr.get(tname).hub_teardown(hub)
                except Exception:
                    pass
        # матрица доступа — один раз на хаб, поверх всех транспортов
        try:
            netutil.apply_forward_matrix(hub, spokes, is_allowed)
        except Exception as e:
            results.append({"hub": f"{hub.name} [matrix]", "ok": False, "msg": str(e)})
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

    # peer_sites: хаб + остальные спицы того же хаба (их LAN → в AllowedIPs)
    siblings = db.query(Site).filter(Site.hub_id == hub.id, Site.id != site.id).all()
    peer_sites = [hub] + siblings

    transport = tr.get(site.transport)
    cfg = transport.site_config(hub, site, peer_sites, _build_is_allowed(db))
    ext = "conf" if site.transport == "wireguard" else "txt"
    return PlainTextResponse(cfg, media_type="text/plain",
                             headers={"Content-Disposition": f'attachment; filename="{site.name}.{ext}"'})


@router.get("/status")
def get_status(db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    _require_module(db)
    hubs = db.query(Site).filter(Site.role == "hub").all()
    result = {}
    for hub in hubs:
        result[hub.id] = {}
        for tname in tr.available():
            try:
                result[hub.id][tname] = tr.get(tname).status(hub)
            except Exception as e:
                result[hub.id][tname] = {"error": str(e)}
    return result


# ──────────────────── Конфиг модуля + матрица доступа (S2) ────────────────────

class ConfigUpdate(BaseModel):
    default_policy: str   # allow_all | deny_all


@router.get("/config")
def get_config(db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    _require_module(db)
    return _get_config(db)


@router.put("/config")
def update_config(body: ConfigUpdate, db: Session = Depends(get_db),
                  admin: AdminUser = Depends(get_current_user)):
    _require_module(db)
    if body.default_policy not in ("allow_all", "deny_all"):
        raise HTTPException(400, "default_policy: allow_all | deny_all")
    cfg = _get_config(db)
    cfg["default_policy"] = body.default_policy
    _set_config(db, cfg)
    audit.log(db, admin.username, "s2s.config", f"default_policy={body.default_policy}")
    return cfg


class AccessRuleItem(BaseModel):
    src_site_id: int
    dst_site_id: int
    allow: bool


class AccessUpdate(BaseModel):
    rules: list[AccessRuleItem]


@router.get("/access")
def get_access(db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    """Матрица доступа: политика по умолчанию + явные правила + список площадок."""
    _require_module(db)
    sites = db.query(Site).order_by(Site.role.desc(), Site.id).all()
    rules = db.query(AccessRule).all()
    return {
        "default_policy": _get_config(db).get("default_policy", DEFAULT_POLICY),
        "sites": [{"id": s.id, "name": s.name, "role": s.role} for s in sites],
        "rules": [{"src_site_id": r.src_site_id, "dst_site_id": r.dst_site_id,
                   "allow": r.allow} for r in rules],
    }


@router.put("/access")
def update_access(body: AccessUpdate, db: Session = Depends(get_db),
                  admin: AdminUser = Depends(get_current_user)):
    """Перезаписать явные правила матрицы (полная замена)."""
    _require_module(db)
    site_ids = {s.id for s in db.query(Site.id).all()}
    db.query(AccessRule).delete()
    for r in body.rules:
        if r.src_site_id not in site_ids or r.dst_site_id not in site_ids:
            continue
        if r.src_site_id == r.dst_site_id:
            continue
        db.add(AccessRule(src_site_id=r.src_site_id, dst_site_id=r.dst_site_id, allow=r.allow))
    db.commit()
    audit.log(db, admin.username, "s2s.access", f"{len(body.rules)} правил")
    return {"ok": True, "count": len(body.rules)}
