"""Публичный роут скачивания установщика по одноразовому токену.

ВАЖНО: это ЕДИНСТВЕННЫЙ роут, который нужно открывать наружу (через nginx).
Вся остальная панель (/api, /) должна оставаться закрытой. Роут не требует
авторизации, но токен неугадываем, имеет срок жизни и лимит скачиваний.
"""
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response, HTMLResponse
from sqlalchemy.orm import Session

from database import get_db
from models import DownloadToken, VPNUser, VPNServer

router = APIRouter(tags=["download"])

DEFAULT_TTL_HOURS = 72
DEFAULT_MAX_DOWNLOADS = 5


def create_token(db: Session, user_id: int, ttl_hours: int = DEFAULT_TTL_HOURS,
                 max_downloads: int = DEFAULT_MAX_DOWNLOADS) -> str:
    """Создаёт токен скачивания, возвращает его строку."""
    tok = secrets.token_urlsafe(32)
    db.add(DownloadToken(
        token=tok, user_id=user_id, kind="installer",
        download_count=0, max_downloads=max_downloads,
        expires_at=datetime.utcnow() + timedelta(hours=ttl_hours),
    ))
    db.commit()
    return tok


def _error_page(title: str, msg: str, code: int) -> HTMLResponse:
    html = f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title></head>
<body style="font-family:-apple-system,Segoe UI,sans-serif;background:#f1f5f9;margin:0;
display:flex;align-items:center;justify-content:center;min-height:100vh">
<div style="background:#fff;border-radius:16px;padding:40px;max-width:420px;text-align:center;
box-shadow:0 10px 40px rgba(0,0,0,.1)">
<div style="font-size:48px;margin-bottom:12px">⏱️</div>
<h1 style="font-size:20px;color:#0f172a;margin:0 0 8px">{title}</h1>
<p style="color:#64748b;font-size:14px;margin:0">{msg}</p>
</div></body></html>"""
    return HTMLResponse(html, status_code=code)


@router.get("/d/{token}")
def download_installer_by_token(token: str, db: Session = Depends(get_db)):
    """Отдаёт .exe установщик по токену (публично, без авторизации)."""
    t = db.query(DownloadToken).filter(DownloadToken.token == token).first()
    if not t:
        return _error_page("Ссылка недействительна", "Возможно, она была введена с ошибкой.", 404)
    if t.expires_at < datetime.utcnow():
        return _error_page("Срок ссылки истёк", "Запросите у администратора новую ссылку.", 410)
    if t.download_count >= t.max_downloads:
        return _error_page("Лимит скачиваний исчерпан", "Запросите у администратора новую ссылку.", 410)

    user = db.query(VPNUser).filter(VPNUser.id == t.user_id).first()
    if not user:
        return _error_page("Клиент не найден", "Обратитесь к администратору.", 404)
    server = db.query(VPNServer).filter(VPNServer.id == user.server_id).first()
    if not server or server.kind != "openvpn":
        return _error_page("Установщик недоступен", "Для этого клиента установщик не предусмотрен.", 400)

    from services import win_installer
    ok, msg = win_installer.is_available()
    if not ok:
        return _error_page("Сборка недоступна", msg, 503)

    from routers.users import _build_user_ovpn
    try:
        ovpn = _build_user_ovpn(db, user)
        exe = win_installer.build_installer(user.username, ovpn)
    except Exception as e:
        return _error_page("Ошибка сборки", str(e), 500)

    t.download_count += 1
    db.commit()

    return Response(
        content=exe, media_type="application/vnd.microsoft.portable-executable",
        headers={"Content-Disposition": f'attachment; filename="ClickVPN-{user.username}-setup.exe"'},
    )
