"""Фоновое отслеживание: история подключений (ConnectionLog) + временной ряд трафика (TrafficSample)."""
import os
import time
import threading
from datetime import datetime, timedelta

DATA_DIR = os.getenv("DATA_DIR", "./data")

_thread = None
_stop = False
# (server_id, common_name, real_address) -> log row id
_open_sessions: dict = {}
# (server_id, common_name, real_address) -> (rx, tx) последнее измерение для дельт
_last_bytes: dict = {}
# server_id -> {"rx":int, "tx":int, "online":int}
_buckets: dict = {}

POLL_SEC = 20
FLUSH_EVERY = 15          # 15 опросов * 20с = 5 минут на сэмпл
RETENTION_DAYS = 90


def _parse_status(path: str) -> list[dict]:
    from services.ovpn_manager import parse_status
    return parse_status(path)


def _add_bucket(server_id, rx_delta, tx_delta, online):
    for key in (server_id, None):  # на сервер и в глобальный
        b = _buckets.setdefault(key, {"rx": 0, "tx": 0, "online": 0})
        b["rx"] += rx_delta
        b["tx"] += tx_delta
    # online считаем по серверу и глобально отдельно — установим максимум позже
    bs = _buckets.setdefault(server_id, {"rx": 0, "tx": 0, "online": 0})
    bs["online"] = max(bs["online"], online)


def _poll_once(SessionLocal, VPNServer, VPNUser, ConnectionLog):
    db = SessionLocal()
    try:
        servers = db.query(VPNServer).all()
        seen = set()
        global_online = 0
        for s in servers:
            status_log = os.path.join(DATA_DIR, "openvpn", f"status_{s.id}.log")
            clients = _parse_status(status_log)
            server_online = len(clients)
            global_online += server_online
            server_rx_delta = 0
            server_tx_delta = 0
            for c in clients:
                key = (s.id, c["common_name"], c["real_address"])
                seen.add(key)
                rx = c.get("bytes_received", 0)
                tx = c.get("bytes_sent", 0)

                # дельта трафика
                last = _last_bytes.get(key)
                if last:
                    drx = rx - last[0]
                    dtx = tx - last[1]
                    if drx < 0:  # сессия пересоздалась — счётчик сбросился
                        drx = rx
                    if dtx < 0:
                        dtx = tx
                else:
                    drx, dtx = 0, 0  # первое появление — базовая точка
                _last_bytes[key] = (rx, tx)
                server_rx_delta += drx
                server_tx_delta += dtx

                # история подключений
                if key not in _open_sessions:
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
                        bytes_received=rx, bytes_sent=tx,
                    )
                    db.add(row); db.commit()
                    _open_sessions[key] = row.id
                else:
                    row = db.query(ConnectionLog).filter(ConnectionLog.id == _open_sessions[key]).first()
                    if row:
                        row.bytes_received = rx
                        row.bytes_sent = tx
                        db.commit()

            _add_bucket(s.id, server_rx_delta, server_tx_delta, server_online)

        # глобальный online
        gb = _buckets.setdefault(None, {"rx": 0, "tx": 0, "online": 0})
        gb["online"] = max(gb["online"], global_online)

        # закрываем отключившиеся
        for key in list(_open_sessions.keys()):
            if key not in seen:
                rid = _open_sessions.pop(key)
                _last_bytes.pop(key, None)
                row = db.query(ConnectionLog).filter(ConnectionLog.id == rid).first()
                if row and row.disconnected_at is None:
                    row.disconnected_at = datetime.utcnow()
                    db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _flush(SessionLocal, TrafficSample):
    if not _buckets:
        return
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        for server_id, b in _buckets.items():
            db.add(TrafficSample(
                timestamp=now, server_id=server_id,
                rx=int(b["rx"]), tx=int(b["tx"]), online=int(b["online"]),
            ))
        db.commit()
        # retention
        cutoff = now - timedelta(days=RETENTION_DAYS)
        db.query(TrafficSample).filter(TrafficSample.timestamp < cutoff).delete()
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
    _buckets.clear()


def start_tracker(SessionLocal, VPNServer, VPNUser, ConnectionLog, TrafficSample):
    global _thread, _stop
    if _thread and _thread.is_alive():
        return
    _stop = False

    def loop():
        # при старте закрываем «висящие» сессии прошлого запуска
        db = SessionLocal()
        try:
            for row in db.query(ConnectionLog).filter(ConnectionLog.disconnected_at.is_(None)).all():
                row.disconnected_at = datetime.utcnow()
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()

        ticks = 0
        while not _stop:
            _poll_once(SessionLocal, VPNServer, VPNUser, ConnectionLog)
            ticks += 1
            if ticks >= FLUSH_EVERY:
                _flush(SessionLocal, TrafficSample)
                ticks = 0
            for _ in range(POLL_SEC):
                if _stop:
                    break
                time.sleep(1)

    _thread = threading.Thread(target=loop, daemon=True)
    _thread.start()
