import io
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse, FileResponse
from sqlalchemy.orm import Session

from database import get_db
from models import AdminUser
from auth import get_current_user
from services import backup as backup_svc
import os

router = APIRouter(prefix="/api/backup", tags=["backup"])


@router.get("/download")
def download_backup(_: AdminUser = Depends(get_current_user)):
    data = backup_svc.make_backup_bytes()
    name = f"clickvpn_{datetime.now().strftime('%Y%m%d_%H%M%S')}.tar.gz"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post("/create")
def create_backup(_: AdminUser = Depends(get_current_user)):
    path = backup_svc.save_backup_file()
    return {"status": "created", "name": os.path.basename(path)}


@router.get("/list")
def list_backups(_: AdminUser = Depends(get_current_user)):
    return backup_svc.list_backups()


@router.get("/file/{name}")
def get_backup_file(name: str, _: AdminUser = Depends(get_current_user)):
    if "/" in name or "\\" in name or not name.endswith(".tar.gz"):
        raise HTTPException(400, "Некорректное имя")
    path = os.path.join(backup_svc.BACKUP_DIR, name)
    if not os.path.exists(path):
        raise HTTPException(404, "Файл не найден")
    return FileResponse(path, media_type="application/gzip", filename=name)


@router.delete("/file/{name}", status_code=204)
def del_backup(name: str, _: AdminUser = Depends(get_current_user)):
    if not backup_svc.delete_backup(name):
        raise HTTPException(404, "Файл не найден")


@router.post("/restore")
async def restore_backup(
    file: UploadFile = File(...),
    _: AdminUser = Depends(get_current_user),
):
    data = await file.read()
    try:
        backup_svc.restore_from_bytes(data)
    except Exception as e:
        raise HTTPException(400, f"Ошибка восстановления: {e}")
    return {"status": "restored", "note": "Перезапустите сервис: systemctl restart click-vpn"}
