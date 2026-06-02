import subprocess
import threading
import os
import re
from fastapi import APIRouter, Depends

from models import AdminUser
from auth import get_current_user


def _strip_ansi(text: str) -> str:
    return re.sub(r'\x1b\[[0-9;]*m|\x1b\[[0-9;]*[a-zA-Z]', '', text)

router = APIRouter(prefix="/api/system", tags=["system"])

INSTALL_DIR = "/opt/click-vpn"
_update_state = {"running": False, "output": "", "success": None}


def _run_update():
    _update_state["running"] = True
    _update_state["output"] = ""
    _update_state["success"] = None
    try:
        proc = subprocess.Popen(
            ["bash", f"{INSTALL_DIR}/update.sh"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=INSTALL_DIR,
        )
        for line in proc.stdout:
            _update_state["output"] += _strip_ansi(line)
        proc.wait()
        _update_state["success"] = proc.returncode == 0
    except Exception as e:
        _update_state["output"] += f"\nОшибка: {e}"
        _update_state["success"] = False
    finally:
        _update_state["running"] = False


@router.post("/update")
def start_update(_: AdminUser = Depends(get_current_user)):
    if _update_state["running"]:
        return {"status": "already_running"}
    _update_state["output"] = ""
    _update_state["success"] = None
    t = threading.Thread(target=_run_update, daemon=True)
    t.start()
    return {"status": "started"}


@router.post("/restart")
def restart_service(_: AdminUser = Depends(get_current_user)):
    """Перезапускает сервис click-vpn (отложенно, чтобы ответ успел вернуться)."""
    try:
        subprocess.Popen(
            ["bash", "-c", "sleep 1; systemctl restart click-vpn"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(500, f"Не удалось перезапустить: {e}")
    return {"status": "restarting"}


@router.get("/update/status")
def update_status(_: AdminUser = Depends(get_current_user)):
    return {
        "running": _update_state["running"],
        "output": _update_state["output"],
        "success": _update_state["success"],
    }


@router.get("/version")
def get_version(_: AdminUser = Depends(get_current_user)):
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=INSTALL_DIR, text=True, stderr=subprocess.DEVNULL
        ).strip()
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=INSTALL_DIR, text=True, stderr=subprocess.DEVNULL
        ).strip()
        date = subprocess.check_output(
            ["git", "log", "-1", "--format=%ci"],
            cwd=INSTALL_DIR, text=True, stderr=subprocess.DEVNULL
        ).strip()
        return {"commit": commit, "branch": branch, "date": date}
    except Exception:
        return {"commit": "unknown", "branch": "unknown", "date": ""}
