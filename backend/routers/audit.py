from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database import get_db
from models import AdminUser, AuditLog, ConnectionLog, VPNUser
from auth import get_current_user

router = APIRouter(prefix="/api", tags=["audit"])


@router.get("/audit")
def list_audit(
    limit: int = Query(50, ge=10, le=1000),
    offset: int = Query(0, ge=0),
    search: str = Query(""),
    admin_f: str = Query("", alias="admin"),
    action_f: str = Query("", alias="action"),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    q = db.query(AuditLog)
    if admin_f.strip():
        q = q.filter(AuditLog.admin == admin_f.strip())
    if action_f.strip():
        q = q.filter(AuditLog.action == action_f.strip())
    if search.strip():
        s = f"%{search.strip()}%"
        q = q.filter((AuditLog.target.like(s)) | (AuditLog.details.like(s)) | (AuditLog.action.like(s)))

    total = q.count()
    rows = q.order_by(AuditLog.timestamp.desc()).offset(offset).limit(limit).all()

    # уникальные значения для выпадающих фильтров
    admins = [a[0] for a in db.query(AuditLog.admin).distinct().all() if a[0]]
    actions = [a[0] for a in db.query(AuditLog.action).distinct().all() if a[0]]

    items = [
        {
            "id": r.id,
            "timestamp": r.timestamp.isoformat() if r.timestamp else None,
            "admin": r.admin,
            "action": r.action,
            "target": r.target,
            "details": r.details,
        }
        for r in rows
    ]
    return {"items": items, "total": total, "admins": sorted(admins), "actions": sorted(actions)}


@router.get("/users/{user_id}/connections")
def user_connections(
    user_id: int,
    limit: int = Query(50, ge=5, le=500),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    rows = (
        db.query(ConnectionLog)
        .filter(ConnectionLog.user_id == user_id)
        .order_by(ConnectionLog.connected_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "real_address": r.real_address,
            "virtual_address": r.virtual_address,
            "connected_at": r.connected_at.isoformat() if r.connected_at else None,
            "disconnected_at": r.disconnected_at.isoformat() if r.disconnected_at else None,
            "online": r.disconnected_at is None,
            "bytes_received": r.bytes_received or 0,
            "bytes_sent": r.bytes_sent or 0,
        }
        for r in rows
    ]


@router.get("/connections/recent")
def recent_connections(
    limit: int = Query(100, ge=10, le=1000),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    rows = (
        db.query(ConnectionLog)
        .order_by(ConnectionLog.connected_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "common_name": r.common_name,
            "real_address": r.real_address,
            "virtual_address": r.virtual_address,
            "connected_at": r.connected_at.isoformat() if r.connected_at else None,
            "disconnected_at": r.disconnected_at.isoformat() if r.disconnected_at else None,
            "online": r.disconnected_at is None,
        }
        for r in rows
    ]
