"""Журнал действий администраторов."""
from sqlalchemy.orm import Session
from models import AuditLog


def log(db: Session, admin: str | None, action: str, target: str = None, details: str = None):
    try:
        db.add(AuditLog(admin=admin, action=action, target=target, details=details))
        db.commit()
    except Exception:
        db.rollback()
