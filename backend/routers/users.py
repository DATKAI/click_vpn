import os
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from database import get_db
from models import AdminUser, CA, VPNServer, VPNUser, CertStatus, Settings
from schemas import UserCreate, UserOut, UserListOut
from auth import get_current_user
from services import pki
from services.profile_builder import build_ovpn_profile

DATA_DIR = os.getenv("DATA_DIR", "./data")

router = APIRouter(prefix="/api/users", tags=["users"])


@router.post("", response_model=UserOut)
def create_user(
    data: UserCreate,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    server = db.query(VPNServer).filter(VPNServer.id == data.server_id).first()
    if not server:
        raise HTTPException(404, "Сервер не найден")

    # Проверка уникальности username на сервере
    exists = db.query(VPNUser).filter(
        VPNUser.server_id == data.server_id,
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
    )
    db.commit()

    user = VPNUser(
        username=data.username,
        email=data.email,
        ca_id=ca.id,
        server_id=data.server_id,
        cert_pem=cert_pem,
        key_pem=key_pem,
        cert_serial=ca.serial,
        cert_expires_at=expires_at,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.get("", response_model=UserListOut)
def list_users(
    server_id: int | None = None,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    q = db.query(VPNUser)
    if server_id:
        q = q.filter(VPNUser.server_id == server_id)
    users = q.order_by(VPNUser.created_at.desc()).all()
    return UserListOut(users=users, total=len(users))


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


@router.post("/{user_id}/revoke", response_model=UserOut)
def revoke_user(
    user_id: int,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    user = db.query(VPNUser).filter(VPNUser.id == user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")

    user.cert_status = CertStatus.revoked
    user.revoked_at = datetime.utcnow()
    user.is_active = False
    db.commit()

    # Обновляем CRL
    ca = db.query(CA).filter(CA.id == user.ca_id).first()
    revoked_serials = [
        u.cert_serial for u in db.query(VPNUser).filter(
            VPNUser.ca_id == ca.id,
            VPNUser.cert_status == CertStatus.revoked,
            VPNUser.cert_serial.isnot(None),
        ).all()
    ]
    crl_pem = pki.build_crl(ca.cert_pem, ca.key_pem, revoked_serials)
    crl_path = os.path.join(DATA_DIR, "pki", f"crl_{ca.id}.pem")
    os.makedirs(os.path.dirname(crl_path), exist_ok=True)
    with open(crl_path, "w") as f:
        f.write(crl_pem)

    db.refresh(user)
    return user


@router.delete("/{user_id}", status_code=204)
def delete_user(user_id: int, db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    user = db.query(VPNUser).filter(VPNUser.id == user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    db.delete(user)
    db.commit()
