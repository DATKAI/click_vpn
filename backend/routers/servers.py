import os
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models import AdminUser, CA, VPNServer, VPNUser, Organization, CertStatus, ServerStatus
from schemas import ServerCreate, ServerUpdate, ServerOut, CACreate, CAOut
from auth import get_current_user
from services import pki, ovpn_manager
from services.profile_builder import build_server_config

DATA_DIR = os.getenv("DATA_DIR", "./data")

router = APIRouter(prefix="/api", tags=["servers"])


# ── CA ────────────────────────────────────────────────────────────────────────

@router.post("/ca", response_model=CAOut)
def create_ca(
    data: CACreate,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    cert_pem, key_pem, expires_at = pki.create_ca(
        common_name=data.common_name,
        country=data.country,
        org=data.org,
        valid_days=data.valid_days,
    )
    ca = CA(
        common_name=data.common_name,
        cert_pem=cert_pem,
        key_pem=key_pem,
        expires_at=expires_at,
    )
    db.add(ca)
    db.commit()
    db.refresh(ca)
    return ca


@router.get("/ca", response_model=list[CAOut])
def list_ca(db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    return db.query(CA).all()


@router.delete("/ca/{ca_id}", status_code=204)
def delete_ca(ca_id: int, db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    ca = db.query(CA).filter(CA.id == ca_id).first()
    if not ca:
        raise HTTPException(404, "CA не найден")
    if db.query(VPNServer).filter(VPNServer.ca_id == ca_id).count():
        raise HTTPException(400, "Нельзя удалить CA — есть привязанные серверы")
    db.delete(ca)
    db.commit()


# ── VPN Servers ───────────────────────────────────────────────────────────────

@router.post("/servers", response_model=ServerOut)
def create_server(
    data: ServerCreate,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    ca = db.query(CA).filter(CA.id == data.ca_id).first()
    if not ca:
        raise HTTPException(404, "CA не найден")

    # Серверный сертификат
    ca.serial += 1
    srv_cert_pem, srv_key_pem, _ = pki.create_server_cert(
        ca_cert_pem=ca.cert_pem,
        ca_key_pem=ca.key_pem,
        serial=ca.serial,
        common_name=f"server-{data.name}",
    )

    server = VPNServer(
        name=data.name,
        ca_id=data.ca_id,
        network=data.network,
        netmask=data.netmask,
        port=data.port,
        protocol=data.protocol,
        dns_servers=data.dns_servers,
        push_routes=data.push_routes,
    )
    db.add(server)
    db.commit()
    db.refresh(server)

    # CRL (пустой изначально)
    crl_path = os.path.join(DATA_DIR, "pki", f"crl_{ca.id}.pem")
    os.makedirs(os.path.dirname(crl_path), exist_ok=True)
    crl_pem = pki.build_crl(ca.cert_pem, ca.key_pem, [])
    with open(crl_path, "w") as f:
        f.write(crl_pem)

    # DH параметры (2048 бит, генерируется ~5-10 сек)
    dh_pem = pki.generate_dh_params(2048)

    # OpenVPN config
    config_content = build_server_config(
        server_id=server.id,
        ca_cert_pem=ca.cert_pem,
        server_cert_pem=srv_cert_pem,
        server_key_pem=srv_key_pem,
        dh_pem=dh_pem,
        network=server.network,
        netmask=server.netmask,
        port=server.port,
        protocol=server.protocol,
        dns_servers=server.dns_servers,
        push_routes=server.push_routes,
        crl_path=crl_path,
        data_dir=DATA_DIR,
    )
    config_path = os.path.join(DATA_DIR, "openvpn", f"server_{server.id}.conf")
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, "w") as f:
        f.write(config_content)

    server.config_path = config_path
    db.commit()
    db.refresh(server)
    return server


def _server_out(s: VPNServer, db: Session, running: bool = None) -> dict:
    from schemas import ServerOut as SO
    if running is None:
        running = ovpn_manager.is_running(s.id, DATA_DIR)
    return SO(
        id=s.id, name=s.name, ca_id=s.ca_id,
        network=s.network, netmask=s.netmask,
        port=s.port, protocol=s.protocol,
        dns_servers=s.dns_servers, push_routes=s.push_routes,
        status="running" if running else "stopped",
        org_ids=[o.id for o in s.organizations],
        created_at=s.created_at,
    )


@router.put("/servers/{server_id}")
def update_server(
    server_id: int,
    data: ServerUpdate,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    server = db.query(VPNServer).filter(VPNServer.id == server_id).first()
    if not server:
        raise HTTPException(404, "Сервер не найден")

    if data.name is not None:
        server.name = data.name
    if data.dns_servers is not None:
        server.dns_servers = data.dns_servers
    if data.push_routes is not None:
        server.push_routes = data.push_routes
    if data.org_ids is not None:
        server.organizations = db.query(Organization).filter(
            Organization.id.in_(data.org_ids)
        ).all()

    db.commit()
    db.refresh(server)
    return _server_out(server, db)


@router.get("/servers")
def list_servers(db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    servers = db.query(VPNServer).all()
    result = []
    for s in servers:
        running = ovpn_manager.is_running(s.id, DATA_DIR)
        if running:
            s.status = ServerStatus.running
        else:
            s.status = ServerStatus.stopped
    db.commit()
    return [_server_out(s, db) for s in servers]


@router.post("/servers/{server_id}/start")
def start_server(server_id: int, db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    server = db.query(VPNServer).filter(VPNServer.id == server_id).first()
    if not server:
        raise HTTPException(404, "Сервер не найден")
    if not server.config_path or not os.path.exists(server.config_path):
        raise HTTPException(400, "Конфиг не найден")

    # Запускаем (юнит с NAT/forward создаётся внутри start_server)
    ok = ovpn_manager.start_server(
        server_id, server.config_path, DATA_DIR,
        network=server.network, netmask=server.netmask
    )
    if not ok:
        raise HTTPException(500, "Не удалось запустить OpenVPN. Проверьте: journalctl -u click-vpn-server-" + str(server_id))

    server.status = ServerStatus.running
    db.commit()
    return {"status": "started"}


@router.post("/servers/{server_id}/stop")
def stop_server(server_id: int, db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    server = db.query(VPNServer).filter(VPNServer.id == server_id).first()
    if not server:
        raise HTTPException(404, "Сервер не найден")

    ovpn_manager.stop_server(server_id, DATA_DIR,
                              network=server.network, netmask=server.netmask)
    server.status = ServerStatus.stopped
    db.commit()
    return {"status": "stopped"}


@router.delete("/servers/{server_id}", status_code=204)
def delete_server(server_id: int, db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    server = db.query(VPNServer).filter(VPNServer.id == server_id).first()
    if not server:
        raise HTTPException(404, "Сервер не найден")
    ovpn_manager.remove_unit(server_id, DATA_DIR,
                              network=server.network, netmask=server.netmask)
    db.delete(server)
    db.commit()
