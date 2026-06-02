from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models import AdminUser, Settings
from schemas import SettingsUpdate, SettingsOut, TestEmailRequest
from auth import get_current_user
from services import mailer

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _get_or_create(db: Session) -> Settings:
    s = db.query(Settings).filter(Settings.id == 1).first()
    if not s:
        s = Settings(id=1)
        db.add(s)
        db.commit()
        db.refresh(s)
    return s


@router.get("", response_model=SettingsOut)
def get_settings(db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    return _get_or_create(db)


@router.put("", response_model=SettingsOut)
def update_settings(
    data: SettingsUpdate,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    s = _get_or_create(db)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(s, field, value)
    s.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(s)
    return s


@router.post("/test-email")
def test_email(
    data: TestEmailRequest,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    s = _get_or_create(db)
    if not s.smtp_host or not s.smtp_from:
        raise HTTPException(400, "Заполните SMTP хост и адрес отправителя")
    try:
        mailer.send_test_email(
            smtp_host=s.smtp_host,
            smtp_port=s.smtp_port or 587,
            smtp_user=s.smtp_user,
            smtp_password=s.smtp_password,
            smtp_from=s.smtp_from,
            smtp_tls=s.smtp_tls if s.smtp_tls is not None else True,
            to_email=data.to_email,
            server_name=s.server_name or "VPN",
        )
    except Exception as e:
        raise HTTPException(500, f"Ошибка: {e}")
    return {"status": "sent", "to": data.to_email}
