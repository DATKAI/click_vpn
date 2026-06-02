import os
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from database import get_db
from models import AdminUser, CA, VPNServer, VPNUser, Organization, RevokedSerial, CertStatus, Settings
from schemas import UserCreate, UserUpdate, UserChangePassword, UserOut, UserListOut
from auth import get_current_user
from services import pki
from services.profile_builder import build_ovpn_profile

DATA_DIR = os.getenv("DATA_DIR", "./data")

router = APIRouter(prefix="/api/users", tags=["users"])


def rebuild_crl(db: Session, ca_id: int):
    """Пересобирает CRL: блокирует выключенных, архивных и удалённых клиентов."""
    ca = db.query(CA).filter(CA.id == ca_id).first()
    if not ca:
        return
    serials = set()
    # Выключенные или архивные активные пользователи
    for u in db.query(VPNUser).filter(VPNUser.ca_id == ca_id, VPNUser.cert_serial.isnot(None)).all():
        if not u.is_active or u.archived:
            serials.add(u.cert_serial)
    # Удалённые (постоянно отозванные)
    for r in db.query(RevokedSerial).filter(RevokedSerial.ca_id == ca_id).all():
        serials.add(r.serial)

    crl_pem = pki.build_crl(ca.cert_pem, ca.key_pem, list(serials))
    crl_path = os.path.join(DATA_DIR, "pki", f"crl_{ca_id}.pem")
    os.makedirs(os.path.dirname(crl_path), exist_ok=True)
    with open(crl_path, "w") as f:
        f.write(crl_pem)


@router.post("", response_model=UserOut)
def create_user(
    data: UserCreate,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
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
        VPNUser.cert_status == CertStatus.active,
    ).first()
    if exists:
        raise HTTPException(400, f"Пользователь '{data.username}' уже существует на этом сервере")

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
        email=data.email,
        ca_id=ca.id,
        server_id=data.server_id,
        org_id=data.org_id,
        cert_pem=cert_pem,
        key_pem=key_pem,
        cert_serial=ca.serial,
        cert_expires_at=expires_at,
        cert_password=data.password or None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


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
        rebuild_crl(db, user.ca_id)
    db.refresh(user)
    return user


@router.get("/{user_id}", response_model=UserOut)
def get_user(user_id: int, db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    user = db.query(VPNUser).filter(VPNUser.id == user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    return user


@router.get("/{user_id}/profile")
def download_profile(
    user_id: int,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    """Скачать .ovpn файл для клиента."""
    user = db.query(VPNUser).filter(VPNUser.id == user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    if user.cert_status == CertStatus.revoked:
        raise HTTPException(400, "Сертификат отозван")

    settings = db.query(Settings).filter(Settings.id == 1).first()
    if not settings or not settings.isp1_host:
        raise HTTPException(400, "Не настроены хосты провайдеров в настройках")

    server = db.query(VPNServer).filter(VPNServer.id == user.server_id).first()
    ca = db.query(CA).filter(CA.id == user.ca_id).first()

    # Собираем все активные провайдеры
    isps = []
    for n in range(1, 5):
        host = getattr(settings, f"isp{n}_host", None)
        if host:
            isps.append({
                "host": host,
                "port": getattr(settings, f"isp{n}_port", 1194),
                "label": getattr(settings, f"isp{n}_label", f"ISP{n}"),
            })

    ovpn = build_ovpn_profile(
        ca_cert_pem=ca.cert_pem,
        client_cert_pem=user.cert_pem,
        client_key_pem=user.key_pem,
        isps=isps,
        protocol=server.protocol,
    )

    filename = f"{user.username}.ovpn"
    return Response(
        content=ovpn,
        media_type="application/x-openvpn-profile",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{user_id}/enable", response_model=UserOut)
def enable_user(user_id: int, db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    """Включить доступ клиенту."""
    user = db.query(VPNUser).filter(VPNUser.id == user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    user.is_active = True
    user.cert_status = CertStatus.active
    user.revoked_at = None
    db.commit()
    rebuild_crl(db, user.ca_id)
    db.refresh(user)
    return user


@router.post("/{user_id}/disable", response_model=UserOut)
def disable_user(user_id: int, db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    """Выключить доступ клиенту (обратимо)."""
    user = db.query(VPNUser).filter(VPNUser.id == user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    user.is_active = False
    user.cert_status = CertStatus.revoked
    user.revoked_at = datetime.utcnow()
    db.commit()
    rebuild_crl(db, user.ca_id)
    db.refresh(user)
    return user


@router.post("/{user_id}/archive", response_model=UserOut)
def archive_user(user_id: int, db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    """Архивировать клиента (скрыть + заблокировать доступ)."""
    user = db.query(VPNUser).filter(VPNUser.id == user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    user.archived = True
    db.commit()
    rebuild_crl(db, user.ca_id)
    db.refresh(user)
    return user


@router.post("/{user_id}/unarchive", response_model=UserOut)
def unarchive_user(user_id: int, db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    """Вернуть клиента из архива."""
    user = db.query(VPNUser).filter(VPNUser.id == user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    user.archived = False
    db.commit()
    rebuild_crl(db, user.ca_id)
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
def delete_user(user_id: int, db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    user = db.query(VPNUser).filter(VPNUser.id == user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    ca_id = user.ca_id
    # Серийник навсегда в CRL чтобы удалённый сертификат не работал
    if user.cert_serial:
        already = db.query(RevokedSerial).filter(
            RevokedSerial.ca_id == ca_id, RevokedSerial.serial == user.cert_serial
        ).first()
        if not already:
            db.add(RevokedSerial(ca_id=ca_id, serial=user.cert_serial))
    db.delete(user)
    db.commit()
    rebuild_crl(db, ca_id)
