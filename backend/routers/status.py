import os
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database import get_db
from models import AdminUser, VPNServer, VPNUser, CertStatus
from schemas import ServerStatusOut, ConnectedClient
from auth import get_current_user
from services import ovpn_manager

DATA_DIR = os.getenv("DATA_DIR", "./data")

router = APIRouter(prefix="/api/status", tags=["status"])


@router.get("", response_model=list[ServerStatusOut])
def get_status(db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    servers = db.query(VPNServer).all()
    result = []
    for s in servers:
        running = ovpn_manager.is_running(s.id, DATA_DIR)
        status_log = os.path.join(DATA_DIR, "openvpn", f"status_{s.id}.log")
        clients_raw = ovpn_manager.parse_status(status_log) if running else []
        clients = [ConnectedClient(**c) for c in clients_raw]
        result.append(ServerStatusOut(
            server_id=s.id,
            server_name=s.name,
            status="running" if running else "stopped",
            connected_clients=clients,
        ))
    return result


@router.get("/summary")
def summary(db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    total_users = db.query(VPNUser).filter(VPNUser.cert_status == CertStatus.active).count()
    total_servers = db.query(VPNServer).count()
    running_servers = sum(
        1 for s in db.query(VPNServer).all()
        if ovpn_manager.is_running(s.id, DATA_DIR)
    )
    return {
        "total_users": total_users,
        "total_servers": total_servers,
        "running_servers": running_servers,
    }
