"""Фоновое отслеживание подключений: пишет историю в ConnectionLog."""
import os
import time
import threading
from datetime import datetime

DATA_DIR = os.getenv("DATA_DIR", "./data")

_thread = None
_stop = False
# (server_id, common_name, real_address) -> log row id
_open_sessions: dict = {}


def _parse_status(path: str) -> list[dict]:
    from services.ovpn_manager import parse_status
    return parse_status(path)


def _poll_once(SessionLocal, VPNServer, VPNUser, ConnectionLog):
    db = SessionLocal()
    try:
        servers = db.query(VPNServer).all()
        seen = set()
        for s in servers:
            status_log = os.path.join(DATA_DIR, "openvpn", f"status_{s.id}.log")
            clients = _parse_status(status_log)
            for c in clients:
                key = (s.id, c["common_name"], c["real_address"])
                seen.add(key)
                if key not in _open_sessions:
                    # Новое подключение
                    user = db.query(VPNUser).filter(
                        VPNUser.server_id == s.id,
                        VPNUser.username == c["common_name"],
                    ).first()
                    row = ConnectionLog(
                        user_id=user.id if user else None,
                        common_name=c["common_name"],
                        server_id=s.id,
                        real_address=c["real_address"],
                        virtual_address=c["virtual_address"],
                        connected_at=datetime.utcnow(),
                        bytes_received=c.get("bytes_received", 0),
                        bytes_sent=c.get("bytes_sent", 0),
                    )
                    db.add(row)
                    db.commit()
                    _open_sessions[key] = row.id
                else:
                    # Обновляем трафик
                    rid = _open_sessions[key]
                    row = db.query(ConnectionLog).filter(ConnectionLog.id == rid).first()
                    if row:
                        row.bytes_received = c.get("bytes_received", 0)
                        row.bytes_sent = c.get("bytes_sent", 0)
                        db.commit()

        # Отключившиеся
        for key in list(_open_sessions.keys()):
            if key not in seen:
                rid = _open_sessions.pop(key)
                row = db.query(ConnectionLog).filter(ConnectionLog.id == rid).first()
                if row and row.disconnected_at is None:
                    row.disconnected_at = datetime.utcnow()
                    db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def start_tracker(SessionLocal, VPNServer, VPNUser, ConnectionLog):
    global _thread, _stop
    if _thread and _thread.is_alive():
        return
    _stop = False

    def loop():
        # При старте закрываем «висящие» сессии прошлого запуска
        db = SessionLocal()
        try:
            for row in db.query(ConnectionLog).filter(ConnectionLog.disconnected_at.is_(None)).all():
                row.disconnected_at = datetime.utcnow()
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()

        while not _stop:
            _poll_once(SessionLocal, VPNServer, VPNUser, ConnectionLog)
            for _ in range(20):  # каждые 20 сек
                if _stop:
                    break
                time.sleep(1)

    _thread = threading.Thread(target=loop, daemon=True)
    _thread.start()
