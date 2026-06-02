import os
import io
import csv
import zipfile
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import AdminUser, CA, VPNServer, VPNUser, Organization, RevokedSerial, CertStatus, Settings
from schemas import UserCreate, UserUpdate, UserChangePassword, UserReissue, UserOut, UserListOut
from auth import get_current_user
from services import pki
from services.profile_builder import build_ovpn_profile
from services import mailer
from services import audit
from services import ovpn_mgmt

DATA_DIR = os.getenv("DATA_DIR", "./data")

router = APIRouter(prefix="/api/users", tags=["users"])


from services.crl import rebuild_crl  # единая пересборка CRL


def _wg_resync(db: Session, server):
    """Пересобирает список пиров WireGuard сервера (только включённые, не архив)."""
    from services import wireguard
    peers = [
        {"public_key": u.wg_public_key, "address": u.wg_address}
        for u in db.query(VPNUser).filter(
            VPNUser.server_id == server.id,
            VPNUser.is_active == True,
            VPNUser.archived == False,
            VPNUser.wg_public_key.isnot(None),
        ).all()
    ]
    try:
        wireguard.write_and_sync(server, peers)
    except Exception:
        pass


WG_KINDS = ("wireguard", "amneziawg", "amneziawg_legacy")


def _is_wg(kind) -> bool:
    return kind in WG_KINDS


def _apply_user_change(db: Session, user):
    """Применяет изменение доступа: CRL (OpenVPN) или resync пиров (WireGuard/AmneziaWG)."""
    server = db.query(VPNServer).filter(VPNServer.id == user.server_id).first()
    if server and _is_wg(server.kind):
        _wg_resync(db, server)
    elif user.ca_id:
        rebuild_crl(db, user.ca_id)


@router.post("", response_model=UserOut)
def create_user(
    data: UserCreate,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_user),
):
    # Получаем организацию
    org = db.query(Organization).filter(Organization.id == data.org_id).first()
    if not org:
        raise HTTPException(404, "Организация не найдена")
    if not org.servers:
        raise HTTPException(400, f"Организация '{org.name}' не привязана ни к одному серверу")

    # Определяем сервер: явно указан или единственный у организации
    if data.server_id:
        server = db.query(VPNServer).filter(VPNServer.id == data.server_id).first()
        if not server or server not in org.servers:
            raise HTTPException(400, "Указанный сервер не принадлежит организации")
    elif len(org.servers) == 1:
        server = org.servers[0]
    else:
        raise HTTPException(400, f"У организации несколько серверов — укажите server_id явно")

    # Проверка уникальности
    exists = db.query(VPNUser).filter(
        VPNUser.server_id == server.id,
        VPNUser.username == data.username,
        VPNUser.archived == False,
    ).first()
    if exists:
        raise HTTPException(400, f"Пользователь '{data.username}' уже существует на этом сервере")

    # ── WireGuard / AmneziaWG клиент ───────────────────────────────────────
    if _is_wg(server.kind):
        from services import wireguard
        priv, pub = wireguard.gen_keypair(server.kind)
        used = [u.wg_address for u in db.query(VPNUser).filter(
            VPNUser.server_id == server.id, VPNUser.wg_address.isnot(None)
        ).all() if u.wg_address]
        addr = wireguard.next_client_ip(server.network, server.netmask, used)
        user = VPNUser(
            username=data.username, full_name=data.full_name, email=data.email,
            server_id=server.id, org_id=data.org_id,
            wg_private_key=priv, wg_public_key=pub, wg_address=addr,
            notes=data.notes,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        _wg_resync(db, server)
        audit.log(db, admin.username, "user.create", user.username, f"wg org={org.name}")
        return user

    ca = db.query(CA).filter(CA.id == server.ca_id).first()
    ca.serial += 1

    cert_pem, key_pem, expires_at = pki.create_client_cert(
        ca_cert_pem=ca.cert_pem,
        ca_key_pem=ca.key_pem,
        serial=ca.serial,
        common_name=data.username,
        valid_days=data.valid_days,
        password=data.password or None,
    )
    db.commit()

    user = VPNUser(
        username=data.username,
        full_name=data.full_name,
        email=data.email,
        ca_id=ca.id,
        server_id=server.id,
        org_id=data.org_id,
        cert_pem=cert_pem,
        key_pem=key_pem,
        cert_serial=ca.serial,
        cert_expires_at=expires_at,
        cert_password=data.password or None,
        notes=data.notes,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    audit.log(db, admin.username, "user.create", user.username, f"org={org.name}")
    return user


def _create_one(db: Session, username: str, full_name: str | None, email: str | None,
                org: Organization, server: VPNServer, valid_days: int, password: str | None) -> VPNUser:
    """Создаёт одного клиента (для импорта). Бросает ValueError при проблеме."""
    exists = db.query(VPNUser).filter(
        VPNUser.server_id == server.id,
        VPNUser.username == username,
        VPNUser.archived == False,
    ).first()
    if exists:
        raise ValueError(f"'{username}' уже существует на сервере {server.name}")

    ca = db.query(CA).filter(CA.id == server.ca_id).first()
    ca.serial += 1
    cert_pem, key_pem, expires_at = pki.create_client_cert(
        ca_cert_pem=ca.cert_pem, ca_key_pem=ca.key_pem, serial=ca.serial,
        common_name=username, valid_days=valid_days, password=password or None,
    )
    user = VPNUser(
        username=username, full_name=full_name, email=email,
        ca_id=ca.id, server_id=server.id, org_id=org.id,
        cert_pem=cert_pem, key_pem=key_pem, cert_serial=ca.serial,
        cert_expires_at=expires_at, cert_password=password or None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/import")
async def import_users(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    """Массовый импорт клиентов из CSV.
    Колонки: username, full_name, email, org, valid_days, password
    """
    content = (await file.read()).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames or "username" not in [f.strip().lower() for f in reader.fieldnames]:
        raise HTTPException(400, "CSV должен содержать колонку 'username'")

    # нормализуем имена колонок
    def norm(row):
        return {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}

    created, errors = [], []
    for i, raw in enumerate(reader, start=2):  # строка 1 — заголовок
        r = norm(raw)
        username = r.get("username", "")
        if not username:
            continue
        org_name = r.get("org", "")
        try:
            org = db.query(Organization).filter(Organization.name == org_name).first()
            if not org:
                raise ValueError(f"организация '{org_name}' не найдена")
            if not org.servers:
                raise ValueError(f"у организации '{org_name}' нет серверов")
            server = org.servers[0]  # берём первый сервер организации
            valid_days = int(r["valid_days"]) if r.get("valid_days", "").isdigit() else 365
            u = _create_one(
                db, username=username,
                full_name=r.get("full_name") or None,
                email=r.get("email") or None,
                org=org, server=server, valid_days=valid_days,
                password=r.get("password") or None,
            )
            created.append({"username": username, "id": u.id})
        except Exception as e:
            errors.append({"row": i, "username": username, "error": str(e)})

    return {"created": len(created), "errors": errors, "created_list": created}


class BulkIds(BaseModel):
    ids: list[int]


class DisconnectReq(BaseModel):
    server_id: int
    common_name: str


@router.post("/disconnect")
def disconnect_cn(
    data: DisconnectReq,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_user),
):
    """Разорвать сессию по серверу и common name (для дашборда)."""
    ok, msg = ovpn_mgmt.kill_client(data.server_id, data.common_name)
    if not ok:
        raise HTTPException(400, msg)
    audit.log(db, admin.username, "user.kill", data.common_name)
    return {"status": "ok", "message": msg}


@router.post("/bulk-download")
def bulk_download(
    data: BulkIds,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    """Скачать .ovpn профили нескольких клиентов одним ZIP."""
    users = db.query(VPNUser).filter(VPNUser.id.in_(data.ids)).all()
    if not users:
        raise HTTPException(404, "Клиенты не найдены")

    buf = io.BytesIO()
    used = {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for u in users:
            srv = db.query(VPNServer).filter(VPNServer.id == u.server_id).first()
            try:
                if srv and _is_wg(srv.kind):
                    content = _build_wg_conf(db, u, srv); ext = "conf"
                else:
                    if not u.cert_pem:
                        continue
                    content = _build_user_ovpn(db, u); ext = "ovpn"
            except Exception:
                continue
            name = u.username
            used[name] = used.get(name, 0) + 1
            fname = f"{name}.{ext}" if used[name] == 1 else f"{name}_{used[name]}.{ext}"
            zf.writestr(fname, content)
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="vpn_profiles.zip"'},
    )


@router.get("", response_model=UserListOut)
def list_users(
    server_id: int | None = None,
    org_id: int | None = None,
    archived: bool = False,
    sort: str = "created",   # created | username | org
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    q = db.query(VPNUser).filter(VPNUser.archived == archived)
    if server_id:
        q = q.filter(VPNUser.server_id == server_id)
    if org_id:
        q = q.filter(VPNUser.org_id == org_id)

    if sort == "username":
        q = q.order_by(VPNUser.username.asc())
    elif sort == "org":
        q = q.order_by(VPNUser.org_id.asc(), VPNUser.username.asc())
    else:
        q = q.order_by(VPNUser.created_at.desc())

    users = q.all()
    return UserListOut(users=users, total=len(users))


@router.put("/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    data: UserUpdate,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    user = db.query(VPNUser).filter(VPNUser.id == user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    fields = data.model_dump(exclude_unset=True)
    crl_dirty = "is_active" in fields
    for field, value in fields.items():
        setattr(user, field, value)
        if field == "is_active":
            user.cert_status = CertStatus.active if value else CertStatus.revoked
    db.commit()
    if crl_dirty:
        _apply_user_change(db, user)
    db.refresh(user)
    return user


@router.get("/{user_id}", response_model=UserOut)
def get_user(user_id: int, db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    user = db.query(VPNUser).filter(VPNUser.id == user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    return user


def _build_wg_conf(db: Session, user: VPNUser, server) -> str:
    from services import wireguard
    settings = db.query(Settings).filter(Settings.id == 1).first()
    if not settings or not settings.isp1_host:
        raise HTTPException(400, "Не настроены хосты провайдеров в настройках")
    # AllowedIPs: push_routes если заданы, иначе сеть сервера
    routes = [r.strip() for r in (server.push_routes or "").splitlines() if r.strip()]
    if not routes:
        import ipaddress
        routes = [str(ipaddress.ip_network(f"{server.network}/{server.netmask}", strict=False))]
    allowed = ", ".join(routes)
    awg = None
    if server.awg_params:
        import json
        try:
            awg = json.loads(server.awg_params)
        except Exception:
            awg = None
    return wireguard.build_client_conf(
        client_priv=user.wg_private_key,
        client_addr=user.wg_address,
        server_pub=server.wg_public_key,
        endpoint_host=settings.isp1_host,
        endpoint_port=server.port,
        dns=server.dns_servers,
        allowed_ips=allowed,
        awg_params=awg,
    )


def _build_user_ovpn(db: Session, user: VPNUser) -> str:
    """Собирает .ovpn профиль для пользователя."""
    settings = db.query(Settings).filter(Settings.id == 1).first()
    if not settings or not settings.isp1_host:
        raise HTTPException(400, "Не настроены хосты провайдеров в настройках")

    server = db.query(VPNServer).filter(VPNServer.id == user.server_id).first()
    ca = db.query(CA).filter(CA.id == user.ca_id).first()

    # Порт берём из СЕРВЕРА (провайдер = только публичный хост/IP)
    isps = []
    for n in range(1, 5):
        host = getattr(settings, f"isp{n}_host", None)
        if host:
            isps.append({
                "host": host,
                "port": server.port,
                "label": getattr(settings, f"isp{n}_label", f"ISP{n}"),
            })

    return build_ovpn_profile(
        ca_cert_pem=ca.cert_pem,
        client_cert_pem=user.cert_pem,
        client_key_pem=user.key_pem,
        isps=isps,
        protocol=server.protocol,
        tls_crypt_key=server.tls_crypt_key,
    )


@router.get("/{user_id}/profile")
def download_profile(
    user_id: int,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    """Скачать профиль клиента (.ovpn или .conf для WireGuard)."""
    user = db.query(VPNUser).filter(VPNUser.id == user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")

    server = db.query(VPNServer).filter(VPNServer.id == user.server_id).first()
    if server and _is_wg(server.kind):
        conf = _build_wg_conf(db, user, server)
        return Response(
            content=conf, media_type="text/plain",
            headers={"Content-Disposition": f'attachment; filename="{user.username}.conf"'},
        )

    if not user.cert_pem:
        raise HTTPException(400, "Сертификат не найден")
    ovpn = _build_user_ovpn(db, user)
    return Response(
        content=ovpn, media_type="application/x-openvpn-profile",
        headers={"Content-Disposition": f'attachment; filename="{user.username}.ovpn"'},
    )


@router.post("/{user_id}/send-email")
def send_profile_email(
    user_id: int,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    """Отправить .ovpn на email клиента."""
    user = db.query(VPNUser).filter(VPNUser.id == user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    if not user.email:
        raise HTTPException(400, "У клиента не указан email")
    if not user.cert_pem:
        raise HTTPException(400, "Сертификат не найден")

    settings = db.query(Settings).filter(Settings.id == 1).first()
    if not settings or not settings.smtp_host or not settings.smtp_from:
        raise HTTPException(400, "Не настроен SMTP в настройках")

    ovpn = _build_user_ovpn(db, user)
    try:
        mailer.send_ovpn_email(
            smtp_host=settings.smtp_host,
            smtp_port=settings.smtp_port or 587,
            smtp_user=settings.smtp_user,
            smtp_password=settings.smtp_password,
            smtp_from=settings.smtp_from,
            smtp_tls=settings.smtp_tls if settings.smtp_tls is not None else True,
            to_email=user.email,
            client_name=user.full_name or user.username,
            ovpn_content=ovpn,
            server_name=settings.server_name or "VPN",
        )
    except Exception as e:
        raise HTTPException(500, f"Ошибка отправки: {e}")

    return {"status": "sent", "to": user.email}


@router.post("/{user_id}/enable", response_model=UserOut)
def enable_user(user_id: int, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_user)):
    """Включить доступ клиенту."""
    user = db.query(VPNUser).filter(VPNUser.id == user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    user.is_active = True
    user.cert_status = CertStatus.active
    user.revoked_at = None
    db.commit()
    _apply_user_change(db, user)
    audit.log(db, admin.username, "user.enable", user.username)
    db.refresh(user)
    return user


@router.post("/{user_id}/disable", response_model=UserOut)
def disable_user(user_id: int, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_user)):
    """Выключить доступ клиенту (обратимо) + мгновенно разорвать сессию."""
    user = db.query(VPNUser).filter(VPNUser.id == user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    user.is_active = False
    user.cert_status = CertStatus.revoked
    user.revoked_at = datetime.utcnow()
    db.commit()
    _apply_user_change(db, user)
    ovpn_mgmt.kill_client(user.server_id, user.username)  # разрыв сейчас (OpenVPN)
    audit.log(db, admin.username, "user.disable", user.username)
    db.refresh(user)
    return user


@router.post("/{user_id}/kill")
def kill_session(user_id: int, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_user)):
    """Мгновенно разорвать активную сессию клиента (не меняя статус)."""
    user = db.query(VPNUser).filter(VPNUser.id == user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    ok, msg = ovpn_mgmt.kill_client(user.server_id, user.username)
    if not ok:
        raise HTTPException(400, msg)
    audit.log(db, admin.username, "user.kill", user.username)
    return {"status": "ok", "message": msg}


@router.post("/{user_id}/archive", response_model=UserOut)
def archive_user(user_id: int, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_user)):
    """Архивировать клиента (скрыть + заблокировать доступ) + разорвать сессию."""
    user = db.query(VPNUser).filter(VPNUser.id == user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    user.archived = True
    db.commit()
    _apply_user_change(db, user)
    ovpn_mgmt.kill_client(user.server_id, user.username)
    audit.log(db, admin.username, "user.archive", user.username)
    db.refresh(user)
    return user


@router.post("/{user_id}/unarchive", response_model=UserOut)
def unarchive_user(user_id: int, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_user)):
    """Вернуть клиента из архива."""
    user = db.query(VPNUser).filter(VPNUser.id == user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    user.archived = False
    db.commit()
    _apply_user_change(db, user)
    audit.log(db, admin.username, "user.unarchive", user.username)
    db.refresh(user)
    return user


@router.post("/{user_id}/reissue", response_model=UserOut)
def reissue_cert(
    user_id: int,
    data: UserReissue,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_user),
):
    """Перевыпустить сертификат: старый отзывается, выдаётся новый с новым серийником."""
    user = db.query(VPNUser).filter(VPNUser.id == user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")

    ca = db.query(CA).filter(CA.id == user.ca_id).first()
    if not ca:
        raise HTTPException(404, "CA не найден")

    # Старый серийник — навсегда в отозванные
    if user.cert_serial:
        already = db.query(RevokedSerial).filter(
            RevokedSerial.ca_id == ca.id, RevokedSerial.serial == user.cert_serial
        ).first()
        if not already:
            db.add(RevokedSerial(ca_id=ca.id, serial=user.cert_serial))

    # Новый сертификат
    ca.serial += 1
    cert_pem, key_pem, expires_at = pki.create_client_cert(
        ca_cert_pem=ca.cert_pem,
        ca_key_pem=ca.key_pem,
        serial=ca.serial,
        common_name=user.username,
        valid_days=data.valid_days,
        password=data.password or None,
    )

    user.cert_pem = cert_pem
    user.key_pem = key_pem
    user.cert_serial = ca.serial
    user.cert_expires_at = expires_at
    user.cert_password = data.password or None
    user.cert_status = CertStatus.active
    user.is_active = True
    user.revoked_at = None
    db.commit()

    rebuild_crl(db, ca.id)
    ovpn_mgmt.kill_client(user.server_id, user.username)  # старая сессия со старым сертом — разорвать
    audit.log(db, admin.username, "user.reissue", user.username)
    db.refresh(user)
    return user


@router.post("/{user_id}/change-password", response_model=UserOut)
def change_password(
    user_id: int,
    data: UserChangePassword,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    """Сменить пароль приватного ключа сертификата."""
    user = db.query(VPNUser).filter(VPNUser.id == user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    if not user.cert_pem or not user.key_pem:
        raise HTTPException(400, "Сертификат не найден")

    # Расшифровываем ключ старым паролем
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend
    old_password = user.cert_password.encode() if user.cert_password else None
    try:
        key = serialization.load_pem_private_key(
            user.key_pem.encode(),
            password=old_password,
            backend=default_backend(),
        )
    except Exception:
        raise HTTPException(400, "Не удалось расшифровать ключ")

    # Перешифровываем новым паролем
    new_pwd = data.new_password or None
    if new_pwd:
        encryption = serialization.BestAvailableEncryption(new_pwd.encode())
    else:
        encryption = serialization.NoEncryption()

    new_key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=encryption,
    ).decode()

    user.key_pem = new_key_pem
    user.cert_password = new_pwd
    db.commit()
    db.refresh(user)
    return user


@router.delete("/{user_id}", status_code=204)
def delete_user(user_id: int, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_user)):
    user = db.query(VPNUser).filter(VPNUser.id == user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    ca_id = user.ca_id
    uname = user.username
    srv_id = user.server_id
    server = db.query(VPNServer).filter(VPNServer.id == srv_id).first()

    # OpenVPN: серийник навсегда в CRL
    if user.cert_serial:
        already = db.query(RevokedSerial).filter(
            RevokedSerial.ca_id == ca_id, RevokedSerial.serial == user.cert_serial
        ).first()
        if not already:
            db.add(RevokedSerial(ca_id=ca_id, serial=user.cert_serial))

    db.delete(user)
    db.commit()

    if server and _is_wg(server.kind):
        _wg_resync(db, server)              # убираем пир — клиент отваливается
    else:
        rebuild_crl(db, ca_id)
        ovpn_mgmt.kill_client(srv_id, uname)
    audit.log(db, admin.username, "user.delete", uname)
