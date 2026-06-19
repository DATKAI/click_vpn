"""Публичная раздача share-ссылок из основного приложения (/s/{token}).

Дублирует логику изолированного share_service, но обслуживается панелью —
чтобы ссылки работали, даже если public_url указывает на адрес панели (:8080),
а отдельный share-сервис не развёрнут. Читает только файлы из $DATA_DIR/share/,
БД/PKI не трогает.
"""
import os
import re
import json
from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import Response, HTMLResponse

router = APIRouter(tags=["share-public"])

DATA_DIR = os.getenv("DATA_DIR", "/var/lib/click-vpn")
SHARE_DIR = os.path.join(DATA_DIR, "share")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def _page(title: str, msg: str, code: int) -> HTMLResponse:
    return HTMLResponse(
        f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title></head>
<body style="font-family:-apple-system,Segoe UI,sans-serif;background:#f1f5f9;margin:0;
display:flex;align-items:center;justify-content:center;min-height:100vh">
<div style="background:#fff;border-radius:16px;padding:40px;max-width:420px;text-align:center;
box-shadow:0 10px 40px rgba(0,0,0,.1)">
<div style="font-size:48px;margin-bottom:12px">🔗</div>
<h1 style="font-size:20px;color:#0f172a;margin:0 0 8px">{title}</h1>
<p style="color:#64748b;font-size:14px;margin:0">{msg}</p></div></body></html>""",
        status_code=code,
    )


def _resolve(token: str):
    if not TOKEN_RE.match(token):
        return "notfound", None, None
    d = os.path.realpath(os.path.join(SHARE_DIR, token))
    if not d.startswith(os.path.realpath(SHARE_DIR) + os.sep):
        return "notfound", None, None
    meta_p = os.path.join(d, "meta.json")
    file_p = os.path.join(d, "file")
    if not (os.path.isfile(meta_p) and os.path.isfile(file_p)):
        return "notfound", None, None
    try:
        with open(meta_p) as f:
            meta = json.load(f)
    except Exception:
        return "notfound", None, None
    try:
        if datetime.fromisoformat(meta["expires_at"]) < datetime.utcnow():
            return "expired", meta, d
    except Exception:
        pass
    if meta.get("download_count", 0) >= meta.get("max_downloads", 5):
        return "exhausted", meta, d
    return "ok", meta, d


def _human_size(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def _landing(token: str, meta: dict, file_size: int) -> HTMLResponse:
    fname = meta.get("filename", "файл")
    left = meta.get("max_downloads", 5) - meta.get("download_count", 0)
    try:
        exp_str = datetime.fromisoformat(meta["expires_at"]).strftime("%d.%m.%Y %H:%M UTC")
    except Exception:
        exp_str = "—"
    return HTMLResponse(f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Скачать — {fname}</title></head>
<body style="font-family:-apple-system,Segoe UI,sans-serif;background:linear-gradient(135deg,#1e1b4b,#312e81);margin:0;
display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px">
<div style="background:#fff;border-radius:20px;padding:36px;max-width:420px;width:100%;text-align:center;
box-shadow:0 20px 60px rgba(0,0,0,.3)">
  <div style="width:64px;height:64px;background:linear-gradient(135deg,#6366f1,#4f46e5);border-radius:18px;
  display:inline-flex;align-items:center;justify-content:center;margin-bottom:18px">
    <svg width="32" height="32" fill="none" stroke="#fff" viewBox="0 0 24 24"><path stroke-linecap="round"
    stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/></svg>
  </div>
  <h1 style="font-size:20px;color:#0f172a;margin:0 0 6px">Файл готов к скачиванию</h1>
  <p style="color:#64748b;font-size:14px;margin:0 0 4px;word-break:break-all"><b>{fname}</b></p>
  <p style="color:#94a3b8;font-size:13px;margin:0 0 22px">{_human_size(file_size)}</p>
  <a href="/s/{token}/dl" style="display:block;background:#4f46e5;color:#fff;text-decoration:none;font-weight:700;
  font-size:16px;padding:14px;border-radius:50px;margin-bottom:16px">⬇ Скачать</a>
  <div style="font-size:12px;color:#94a3b8;border-top:1px solid #f1f5f9;padding-top:14px;text-align:left">
    <div>Действует до: <b style="color:#475569">{exp_str}</b></div>
    <div>Осталось скачиваний: <b style="color:#475569">{left}</b></div>
  </div>
</div></body></html>""")


@router.get("/s/{token}", response_class=HTMLResponse, include_in_schema=False)
def landing(token: str):
    status, meta, _ = _resolve(token)
    if status == "notfound":
        return _page("Ссылка недействительна", "Возможно, она введена с ошибкой или удалена.", 404)
    if status == "expired":
        return _page("Срок ссылки истёк", "Запросите у администратора новую ссылку.", 410)
    if status == "exhausted":
        return _page("Лимит скачиваний исчерпан", "Запросите у администратора новую ссылку.", 410)
    file_size = os.path.getsize(os.path.join(SHARE_DIR, token, "file"))
    return _landing(token, meta, file_size)


@router.get("/s/{token}/dl", include_in_schema=False)
def download(token: str):
    status, meta, d = _resolve(token)
    if status != "ok":
        msg = {"notfound": "Возможно, она удалена.",
               "expired": "Запросите новую ссылку.",
               "exhausted": "Запросите новую ссылку."}.get(status, "")
        title = {"notfound": "Ссылка недействительна", "expired": "Срок ссылки истёк",
                 "exhausted": "Лимит скачиваний исчерпан"}.get(status, "Ошибка")
        return _page(title, msg, 404 if status == "notfound" else 410)

    with open(os.path.join(d, "file"), "rb") as f:
        content = f.read()
    try:
        meta["download_count"] = meta.get("download_count", 0) + 1
        with open(os.path.join(d, "meta.json"), "w") as f:
            json.dump(meta, f)
    except Exception:
        pass
    return Response(content=content,
                    media_type=meta.get("content_type", "application/octet-stream"),
                    headers={"Content-Disposition": f'attachment; filename="{meta.get("filename", "file")}"'})
