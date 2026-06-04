"""Создание временных ссылок для скачивания (сторона панели).

Файлы предгенерируются и кладутся в $DATA_DIR/share/<token>/ вместе с
meta.json. Раздаёт их отдельный изолированный микросервис (share_service.py),
который НЕ имеет доступа к БД/ключам — только к этой папке.
"""
import os
import json
import shutil
import secrets
from datetime import datetime, timedelta

DATA_DIR = os.getenv("DATA_DIR", "./data")
SHARE_DIR = os.path.join(DATA_DIR, "share")


def create_share(content: bytes, filename: str, content_type: str,
                 ttl_hours: int = 72, max_downloads: int = 5,
                 label: str = "") -> str:
    """Сохраняет файл под одноразовым токеном, возвращает токен."""
    os.makedirs(SHARE_DIR, exist_ok=True)
    token = secrets.token_urlsafe(24)
    d = os.path.join(SHARE_DIR, token)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "file"), "wb") as f:
        f.write(content)
    meta = {
        "filename": filename,
        "content_type": content_type,
        "label": label,
        "expires_at": (datetime.utcnow() + timedelta(hours=ttl_hours)).isoformat(),
        "max_downloads": int(max_downloads),
        "download_count": 0,
        "created_at": datetime.utcnow().isoformat(),
    }
    with open(os.path.join(d, "meta.json"), "w") as f:
        json.dump(meta, f)
    # права: сервис раздачи (группа clickvpn-share) должен читать файлы.
    # Каталог share/ обычно setgid → группа наследуется; задаём групповое чтение.
    try:
        os.chmod(d, 0o770)                                   # сервис создаёт/удаляет в каталоге
        os.chmod(os.path.join(d, "file"), 0o640)             # файл — только чтение группой
        os.chmod(os.path.join(d, "meta.json"), 0o660)        # счётчик — запись группой
    except Exception:
        pass
    try:
        shutil.chown(d, group="clickvpn-share")
        for fn in ("file", "meta.json"):
            shutil.chown(os.path.join(d, fn), group="clickvpn-share")
    except Exception:
        pass  # группы может не быть, если сервис ещё не установлен
    return token


def list_shares() -> list[dict]:
    """Активные ссылки (для управления из панели)."""
    out = []
    if not os.path.isdir(SHARE_DIR):
        return out
    now = datetime.utcnow()
    for token in os.listdir(SHARE_DIR):
        meta_p = os.path.join(SHARE_DIR, token, "meta.json")
        try:
            with open(meta_p) as f:
                m = json.load(f)
        except Exception:
            continue
        try:
            expired = datetime.fromisoformat(m["expires_at"]) < now
        except Exception:
            expired = False
        out.append({
            "token": token,
            "filename": m.get("filename"),
            "label": m.get("label", ""),
            "expires_at": m.get("expires_at"),
            "max_downloads": m.get("max_downloads"),
            "download_count": m.get("download_count", 0),
            "created_at": m.get("created_at"),
            "expired": expired,
        })
    out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return out


def revoke_share(token: str) -> bool:
    """Удаляет ссылку (отзыв)."""
    if not token or "/" in token or "\\" in token:
        return False
    d = os.path.join(SHARE_DIR, token)
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
        return True
    return False


def cleanup_expired() -> int:
    """Удаляет просроченные ссылки. Возвращает число удалённых."""
    if not os.path.isdir(SHARE_DIR):
        return 0
    now = datetime.utcnow()
    removed = 0
    for token in os.listdir(SHARE_DIR):
        d = os.path.join(SHARE_DIR, token)
        meta_p = os.path.join(d, "meta.json")
        try:
            with open(meta_p) as f:
                m = json.load(f)
            if datetime.fromisoformat(m["expires_at"]) < now:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        except Exception:
            pass
    return removed
