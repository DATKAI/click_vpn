import subprocess
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from database import get_db
from models import AdminUser, VPNServer
from auth import get_current_user

router = APIRouter(prefix="/api/logs", tags=["logs"])


def _journalctl(unit: str, lines: int = 200, since: str = None) -> str:
    cmd = ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "--output=short-iso"]
    if since:
        cmd += ["--since", since]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return result.stdout or result.stderr or "Логов нет"
    except FileNotFoundError:
        return "journalctl не найден"
    except subprocess.TimeoutExpired:
        return "Timeout при чтении логов"
    except Exception as e:
        return f"Ошибка: {e}"


@router.get("/service")
def get_service_logs(
    lines: int = Query(200, ge=10, le=1000),
    _: AdminUser = Depends(get_current_user),
):
    """Логи основного сервиса click-vpn."""
    return {"unit": "click-vpn", "logs": _journalctl("click-vpn", lines)}


@router.get("/server/{server_id}")
def get_server_logs(
    server_id: int,
    lines: int = Query(200, ge=10, le=1000),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    """Логи конкретного OpenVPN сервера."""
    server = db.query(VPNServer).filter(VPNServer.id == server_id).first()
    unit = f"click-vpn-server-{server_id}"
    name = server.name if server else unit
    return {"unit": unit, "server_name": name, "logs": _journalctl(unit, lines)}


@router.get("/all")
def get_all_logs(
    lines: int = Query(100, ge=10, le=500),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    """Логи всех компонентов."""
    service_logs = _journalctl("click-vpn", lines)
    servers = db.query(VPNServer).all()
    server_logs = []
    for s in servers:
        unit = f"click-vpn-server-{s.id}"
        server_logs.append({
            "server_id": s.id,
            "server_name": s.name,
            "unit": unit,
            "logs": _journalctl(unit, lines),
        })
    return {
        "service": {"unit": "click-vpn", "logs": service_logs},
        "servers": server_logs,
    }
