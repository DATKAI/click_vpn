"""Обратная связь разработчику (SanX / DATKAI)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db
from models import AdminUser, Settings
from auth import get_current_user
from services import audit, mailer

router = APIRouter(prefix="/api/feedback", tags=["feedback"])

DEV_EMAIL = "sanxzet@gmail.com"


class FeedbackReq(BaseModel):
    subject: str = ""
    message: str
    contact: str | None = None


@router.get("/info")
def dev_info(_: AdminUser = Depends(get_current_user)):
    return {"developer": "SanX", "company": "DATKAI", "email": DEV_EMAIL}


@router.post("")
def send_feedback(body: FeedbackReq, db: Session = Depends(get_db),
                  admin: AdminUser = Depends(get_current_user)):
    if not body.message or not body.message.strip():
        raise HTTPException(400, "Введите сообщение")
    s = db.query(Settings).filter(Settings.id == 1).first()
    if not s or not s.smtp_host:
        raise HTTPException(400, "Не настроен SMTP (Настройки → SMTP) — без него отправка невозможна")
    try:
        mailer.send_feedback(
            s.smtp_host, s.smtp_port, s.smtp_user, s.smtp_password,
            s.smtp_from or s.smtp_user, s.smtp_tls, DEV_EMAIL,
            body.subject.strip(), body.message.strip(),
            contact=(body.contact or "").strip() or None,
            admin=admin.username, server_name=(s.server_name or "Click VPN"),
        )
    except Exception as e:
        raise HTTPException(500, f"Не удалось отправить: {e}")
    audit.log(db, admin.username, "feedback.send", body.subject.strip()[:64])
    return {"ok": True}
