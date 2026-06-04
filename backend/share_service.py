"""Изолированный микросервис раздачи временных ссылок.

Запускается ОТДЕЛЬНЫМ systemd-юнитом под непривилегированным пользователем
(clickvpn-share) с доступом ТОЛЬКО к $DATA_DIR/share/. НЕ импортирует
БД/PKI/модели панели — даже при компрометации не даёт доступа к ключам.

Запуск:  uvicorn share_service:app --host 127.0.0.1 --port 8081
Наружу проксируется nginx'ом (location /s/).
"""
import os
import re
import json
from datetime import datetime

from fastapi import FastAPI
from fastapi.responses import Response, HTMLResponse

DATA_DIR = os.getenv("DATA_DIR", "/var/lib/click-vpn")
SHARE_DIR = os.path.join(DATA_DIR, "share")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")

app = FastAPI(title="Click VPN Share", docs_url=None, redoc_url=None, openapi_url=None)


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


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/s/{token}")
@app.get("/{token}")
def download(token: str):
    if not TOKEN_RE.match(token):
        return _page("Неверная ссылка", "Проверьте адрес.", 404)

    d = os.path.realpath(os.path.join(SHARE_DIR, token))
    # защита от path traversal: путь обязан быть внутри SHARE_DIR
    if not d.startswith(os.path.realpath(SHARE_DIR) + os.sep):
        return _page("Неверная ссылка", "Проверьте адрес.", 404)

    meta_p = os.path.join(d, "meta.json")
    file_p = os.path.join(d, "file")
    if not (os.path.isfile(meta_p) and os.path.isfile(file_p)):
        return _page("Ссылка недействительна", "Возможно, срок истёк или файл удалён.", 404)

    try:
        with open(meta_p) as f:
            meta = json.load(f)
    except Exception:
        return _page("Ошибка", "Не удалось прочитать ссылку.", 500)

    try:
        if datetime.fromisoformat(meta["expires_at"]) < datetime.utcnow():
            return _page("Срок ссылки истёк", "Запросите у администратора новую.", 410)
    except Exception:
        pass

    if meta.get("download_count", 0) >= meta.get("max_downloads", 5):
        return _page("Лимит скачиваний исчерпан", "Запросите у администратора новую ссылку.", 410)

    with open(file_p, "rb") as f:
        content = f.read()

    # инкремент счётчика (best-effort)
    try:
        meta["download_count"] = meta.get("download_count", 0) + 1
        with open(meta_p, "w") as f:
            json.dump(meta, f)
    except Exception:
        pass

    fname = meta.get("filename", "file")
    return Response(
        content=content,
        media_type=meta.get("content_type", "application/octet-stream"),
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
