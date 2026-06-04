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

WG_KINDS = ("wireguard", "amneziawg", "amneziawg_legacy")

def _is_wg(kind: str) -> bool:
    return kind in WG_KINDS


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

def _eff_proto(kind: str, protocol: str, obfuscation: bool) -> str:
    if kind in WG_KINDS:
        return "udp"
    if kind == "ikev2":
        return "ikev2"
    return "tcp" if obfuscation else (protocol or "udp")


def _check_port_conflict(db, port: int, proto: str, exclude_id: int = None):
    if proto == "ikev2":
        return  # strongSwan слушает 500/4500 — отдельный демон
    q = db.query(VPNServer).filter(VPNServer.port == port)
    if exclude_id:
        q = q.filter(VPNServer.id != exclude_id)
    for s in q.all():
        if _eff_proto(s.kind, s.protocol, bool(s.obfuscation)) == proto:
            raise HTTPException(
                400,
                f"Порт {port}/{proto.upper()} уже занят сервером «{s.name}». "
                f"Выберите другой порт."
            )


@router.post("/servers", response_model=ServerOut)
def create_server(
    data: ServerCreate,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    # Проверка конфликта порт+протокол с существующими серверами
    eff_port = 443 if (data.kind == "openvpn" and data.obfuscation) else \
               (51820 if (data.kind in WG_KINDS and data.port in (1194,)) else data.port)
    _check_port_conflict(db, eff_port, _eff_proto(data.kind, data.protocol, data.obfuscation))

    # ── WireGuard / AmneziaWG ──────────────────────────────────────────────
    if data.kind in ("wireguard", "amneziawg", "amneziawg_legacy"):
        from services import wireguard
        import json
        try:
            priv, pub = wireguard.gen_keypair(data.kind)
        except Exception as e:
            raise HTTPException(500, f"WireGuard/AmneziaWG не установлен: {e}")

        awg = None
        if data.kind == "amneziawg":
            awg = json.dumps(wireguard.gen_awg_params("2"))
        elif data.kind == "amneziawg_legacy":
            awg = json.dumps(wireguard.gen_awg_params("legacy"))

        server = VPNServer(
            name=data.name, kind=data.kind, ca_id=None,
            network=data.network, netmask=data.netmask,
            port=(data.port if data.port not in (1194,) else 51820),
            protocol="udp",
            dns_servers=data.dns_servers, push_routes=data.push_routes,
            wg_private_key=priv, wg_public_key=pub, awg_params=awg,
        )
        db.add(server)
        db.commit()
        db.refresh(server)
        wireguard.write_and_sync(server, [])
        server.config_path = wireguard._conf_path(server.id)
        db.commit()
        db.refresh(server)
        return _server_out(server, db)

    # ── IKEv2 / IPsec ──────────────────────────────────────────────────────
    if data.kind == "ikev2":
        from models import Settings
        from services import ikev2
        ca = db.query(CA).filter(CA.id == data.ca_id).first()
        if not ca:
            raise HTTPException(404, "Для IKEv2 нужен CA")
        s = db.query(Settings).filter(Settings.id == 1).first()
        if not s or not s.isp1_host:
            raise HTTPException(400, "Сначала настройте хост провайдера (он попадёт в серверный сертификат)")
        sans = [getattr(s, f"isp{n}_host") for n in range(1, 5) if getattr(s, f"isp{n}_host", None)]

        ca.serial += 1
        cert_pem, key_pem, _ = pki.create_ikev2_server_cert(
            ca.cert_pem, ca.key_pem, ca.serial, f"ikev2-{data.name}", sans
        )
        server = VPNServer(
            name=data.name, kind="ikev2", ca_id=data.ca_id,
            network=data.network, netmask=data.netmask,
            port=(data.port if data.port not in (1194,) else 500),
            protocol="udp",
            dns_servers=data.dns_servers, push_routes=data.push_routes,
            ikev2_cert_pem=cert_pem, ikev2_key_pem=key_pem,
        )
        db.add(server)
        db.commit()
        db.refresh(server)
        return _server_out(server, db)

    # ── OpenVPN ────────────────────────────────────────────────────────────
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

    # Обфускация: TCP + tls-crypt ключ
    tls_crypt_key = pki.generate_tls_crypt_key() if data.obfuscation else None

    server = VPNServer(
        name=data.name,
        ca_id=data.ca_id,
        network=data.network,
        netmask=data.netmask,
        port=data.port,
        protocol=("tcp" if data.obfuscation else data.protocol),
        dns_servers=data.dns_servers,
        push_routes=data.push_routes,
        obfuscation=data.obfuscation,
        tls_crypt_key=tls_crypt_key,
    )
    db.add(server)
    db.commit()
    db.refresh(server)

    # CRL — ВАЖНО: включаем все ранее отозванные/удалённые серийники этого CA,
    # иначе старые сертификаты снова заработают на пересозданном сервере
    from services.crl import rebuild_crl
    crl_path = os.path.join(DATA_DIR, "pki", f"crl_{ca.id}.pem")
    rebuild_crl(db, ca.id)

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
        tls_crypt_key=server.tls_crypt_key,
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
        if _is_wg(s.kind):
            from services import wireguard
            running = wireguard.is_running(s.id)
        elif s.kind == "ikev2":
            from services import ikev2
            running = ikev2.is_running()
        else:
            running = ovpn_manager.is_running(s.id, DATA_DIR)
    user_count = db.query(VPNUser).filter(
        VPNUser.server_id == s.id, VPNUser.archived == False
    ).count()
    return SO(
        id=s.id, name=s.name, kind=s.kind or "openvpn", ca_id=s.ca_id,
        network=s.network, netmask=s.netmask,
        port=s.port, protocol=s.protocol,
        dns_servers=s.dns_servers, push_routes=s.push_routes,
        status="running" if running else "stopped",
        org_ids=[o.id for o in s.organizations],
        user_count=user_count,
        obfuscation=bool(s.obfuscation),
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

    routes_changed = False
    if data.name is not None:
        server.name = data.name
    if data.dns_servers is not None and data.dns_servers != server.dns_servers:
        server.dns_servers = data.dns_servers
        routes_changed = True
    if data.push_routes is not None and data.push_routes != server.push_routes:
        server.push_routes = data.push_routes
        routes_changed = True
    if data.org_ids is not None:
        server.organizations = db.query(Organization).filter(
            Organization.id.in_(data.org_ids)
        ).all()

    db.commit()
    db.refresh(server)

    # Применяем изменённые маршруты/DNS к рабочему конфигу сервера
    applied = False
    if routes_changed:
        if _is_wg(server.kind):
            from services import wireguard
            try:
                peers = [
                    {"public_key": u.wg_public_key, "address": u.wg_address}
                    for u in db.query(VPNUser).filter(
                        VPNUser.server_id == server.id, VPNUser.is_active == True,
                        VPNUser.archived == False, VPNUser.wg_public_key.isnot(None),
                    ).all()
                ]
                wireguard.write_and_sync(server, peers)
                applied = True
            except Exception:
                pass
        elif server.kind == "ikev2":
            from routers.users import ikev2_resync
            from services import ikev2
            try:
                ikev2_resync(db, server)
                applied = True
            except Exception:
                pass
        else:
            # OpenVPN: патчим конфиг и перезапускаем сервер, если он работает
            from services.profile_builder import rewrite_pushes
            if rewrite_pushes(server.config_path, server.dns_servers, server.push_routes):
                if ovpn_manager.is_running(server.id, DATA_DIR):
                    from services.crl import rebuild_crl
                    rebuild_crl(db, server.ca_id)
                    ovpn_manager.start_server(
                        server.id, server.config_path, DATA_DIR,
                        network=server.network, netmask=server.netmask
                    )
                applied = True

    return _server_out(server, db)


@router.get("/servers")
def list_servers(db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    servers = db.query(VPNServer).all()
    return [_server_out(s, db) for s in servers]


@router.post("/servers/{server_id}/start")
def start_server(server_id: int, db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    server = db.query(VPNServer).filter(VPNServer.id == server_id).first()
    if not server:
        raise HTTPException(404, "Сервер не найден")

    if _is_wg(server.kind):
        from services import wireguard
        ok, msg = wireguard.start(server_id, server.kind)
        if not ok:
            raise HTTPException(500, f"Не удалось запустить: {msg}")
        return {"status": "started"}

    if server.kind == "ikev2":
        from routers.users import ikev2_resync
        from services import ikev2
        ikev2.start()
        ikev2_resync(db, server)   # загрузит конфиг сервера + пользователей
        return {"status": "started"}

    if not server.config_path or not os.path.exists(server.config_path):
        raise HTTPException(400, "Конфиг не найден")
    from services.crl import rebuild_crl
    rebuild_crl(db, server.ca_id)
    ok = ovpn_manager.start_server(
        server_id, server.config_path, DATA_DIR,
        network=server.network, netmask=server.netmask
    )
    if not ok:
        raise HTTPException(500, "Не удалось запустить OpenVPN. journalctl -u click-vpn-server-" + str(server_id))
    server.status = ServerStatus.running
    db.commit()
    return {"status": "started"}


@router.post("/servers/{server_id}/stop")
def stop_server(server_id: int, db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    server = db.query(VPNServer).filter(VPNServer.id == server_id).first()
    if not server:
        raise HTTPException(404, "Сервер не найден")

    if _is_wg(server.kind):
        from services import wireguard
        wireguard.stop(server_id)
    elif server.kind == "ikev2":
        from services import ikev2
        ikev2.stop_conn(server_id)
    else:
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

    # WireGuard/AmneziaWG — снимаем интерфейс/юнит, удаляем клиентов без CRL
    if _is_wg(server.kind):
        from services import wireguard
        wireguard.remove(server_id)
        db.query(VPNUser).filter(VPNUser.server_id == server_id).delete()
        server.organizations = []
        db.flush()
        db.delete(server)
        db.commit()
        return

    # IKEv2 — снимаем swanctl-конфиг
    if server.kind == "ikev2":
        from services import ikev2
        ikev2.stop_conn(server_id)
        db.query(VPNUser).filter(VPNUser.server_id == server_id).delete()
        server.organizations = []
        db.flush()
        db.delete(server)
        db.commit()
        return

    # Останавливаем процесс и снимаем юнит
    ovpn_manager.remove_unit(server_id, DATA_DIR,
                             network=server.network, netmask=server.netmask)

    # Удаляем клиентов сервера (их сертификаты теряют смысл)
    from models import RevokedSerial
    users = db.query(VPNUser).filter(VPNUser.server_id == server_id).all()
    for u in users:
        if u.cert_serial:
            exists = db.query(RevokedSerial).filter(
                RevokedSerial.ca_id == u.ca_id, RevokedSerial.serial == u.cert_serial
            ).first()
            if not exists:
                db.add(RevokedSerial(ca_id=u.ca_id, serial=u.cert_serial))
        db.delete(u)

    # Снимаем привязки к организациям (M2M)
    server.organizations = []
    db.flush()

    ca_id = server.ca_id
    db.delete(server)
    db.commit()

    # Пересобираем CRL чтобы отозванные серийники попали в него
    from services.crl import rebuild_crl
    rebuild_crl(db, ca_id)
