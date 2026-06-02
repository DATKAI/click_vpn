"""Бэкап и восстановление данных (БД + PKI + конфиги OpenVPN)."""
import os
import io
import tarfile
import time
import threading
from datetime import datetime

DATA_DIR = os.getenv("DATA_DIR", "./data")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")


def _data_members():
    """Что бэкапим: vpn.db + pki/ + openvpn/ (без папки backups)."""
    items = []
    db_path = os.path.join(DATA_DIR, "vpn.db")
    if os.path.exists(db_path):
        items.append(("vpn.db", db_path))
    for sub in ("pki", "openvpn"):
        base = os.path.join(DATA_DIR, sub)
        if os.path.isdir(base):
            for root, _, files in os.walk(base):
                for f in files:
                    full = os.path.join(root, f)
                    arc = os.path.relpath(full, DATA_DIR)
                    items.append((arc, full))
    return items


def make_backup_bytes() -> bytes:
    """Создаёт tar.gz в памяти и возвращает байты."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for arc, full in _data_members():
            try:
                tar.add(full, arcname=arc)
            except Exception:
                pass
    buf.seek(0)
    return buf.read()


def save_backup_file() -> str:
    """Создаёт файл бэкапа в BACKUP_DIR, возвращает путь."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = f"clickvpn_{datetime.now().strftime('%Y%m%d_%H%M%S')}.tar.gz"
    path = os.path.join(BACKUP_DIR, name)
    with open(path, "wb") as f:
        f.write(make_backup_bytes())
    return path


def list_backups() -> list[dict]:
    if not os.path.isdir(BACKUP_DIR):
        return []
    out = []
    for f in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if f.endswith(".tar.gz"):
            p = os.path.join(BACKUP_DIR, f)
            st = os.stat(p)
            out.append({
                "name": f,
                "size": st.st_size,
                "created": datetime.fromtimestamp(st.st_mtime).isoformat(),
            })
    return out


def delete_backup(name: str) -> bool:
    if "/" in name or "\\" in name or not name.endswith(".tar.gz"):
        return False
    p = os.path.join(BACKUP_DIR, name)
    if os.path.exists(p):
        os.remove(p)
        return True
    return False


def prune_backups(keep: int):
    files = [f for f in list_backups()]
    for old in files[keep:]:
        delete_backup(old["name"])


def restore_from_bytes(data: bytes):
    """Распаковывает бэкап в DATA_DIR (перезаписывает файлы)."""
    buf = io.BytesIO(data)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        # безопасная распаковка — только внутрь DATA_DIR
        for member in tar.getmembers():
            target = os.path.realpath(os.path.join(DATA_DIR, member.name))
            if not target.startswith(os.path.realpath(DATA_DIR)):
                continue
            tar.extract(member, DATA_DIR)


# ── Авто-бэкап поток ──────────────────────────────────────────────────────────
_auto_thread = None
_auto_stop = False


def start_auto_backup(get_settings_fn):
    """Запускает фоновый поток автобэкапа. get_settings_fn() -> (enabled, interval_h, keep)."""
    global _auto_thread, _auto_stop
    if _auto_thread and _auto_thread.is_alive():
        return
    _auto_stop = False

    def loop():
        last = 0
        while not _auto_stop:
            try:
                enabled, interval_h, keep = get_settings_fn()
                if enabled:
                    now = time.time()
                    if now - last >= interval_h * 3600:
                        save_backup_file()
                        prune_backups(keep)
                        last = now
            except Exception:
                pass
            # проверяем каждую минуту
            for _ in range(60):
                if _auto_stop:
                    break
                time.sleep(1)

    _auto_thread = threading.Thread(target=loop, daemon=True)
    _auto_thread.start()
