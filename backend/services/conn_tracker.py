"""Фоновое отслеживание: история подключений (ConnectionLog) + временной ряд трафика (TrafficSample)."""
import os
import time
import subprocess
import threading
from datetime import datetime, timedelta

DATA_DIR = os.getenv("DATA_DIR", "./data")

_thread = None
_stop = False
_AttemptModel = None      # модель ConnectionAttempt (передаётся в start_tracker)
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


WG_KINDS = ("wireguard", "amneziawg", "amneziawg_legacy")


def _parse_wg_dump(server_id: int, kind: str) -> list[dict]:
    """Парсит `wg/awg show wgN dump` → список онлайн-пиров с rx/tx байтами.
    Пир «онлайн» если last_handshake < 3 минуты назад."""
    import time
    wg = "awg" if kind.startswith("amnez") else "wg"
    iface = f"wg{server_id}"
    try:
        out = subprocess.check_output(
            [wg, "show", iface, "dump"],
            text=True, stderr=subprocess.DEVNULL, timeout=3
        )
    except Exception:
        return []
    now_ts = time.time()
    result = []
    for line in out.strip().splitlines()[1:]:  # пропускаем строку интерфейса
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        pub_key          = parts[0]
        endpoint         = parts[2]
        latest_handshake = int(parts[4]) if parts[4].isdigit() else 0
        rx               = int(parts[5]) if parts[5].isdigit() else 0
        tx               = int(parts[6]) if parts[6].isdigit() else 0
        is_online = latest_handshake > 0 and (now_ts - latest_handshake) <= 180
        result.append({
            "pub_key":    pub_key,
            "endpoint":   endpoint if endpoint != "(none)" else "",
            "rx":         rx,
            "tx":         tx,
            "is_online":  is_online,
        })
    return result


def _parse_undef(status_log_path: str) -> list[dict]:
    """Возвращает ВСЕ строки CLIENT LIST с CN=UNDEF (parse_status их схлопывает).
    UNDEF = TLS-аутентификация не прошла → сканер/бот."""
    if not os.path.exists(status_log_path):
        return []
    try:
        with open(status_log_path) as f:
            lines = f.readlines()
    except OSError:
        return []
    out, section = [], None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("Common Name,Real Address"):
            section = "clients"; continue
        if line.startswith(("Virtual Address,", "ROUTING TABLE", "GLOBAL STATS", "Updated,", "END")):
            section = None; continue
        if section == "clients":
            parts = line.split(",")
            if len(parts) >= 2 and parts[0] == "UNDEF":
                out.append({"common_name": parts[0], "real_address": parts[1]})
    return out


def _record_attempt(db, server_id, common_name, real_address):
    """Логирует неудачную/анонимную попытку (CN=UNDEF) в ConnectionAttempt.
    Агрегирует по (server_id, ip): инкремент счётчика + last_seen."""
    if _AttemptModel is None:
        return
    ip = (real_address or "").rsplit(":", 1)[0] or real_address or "?"
    now = datetime.utcnow()
    row = db.query(_AttemptModel).filter(
        _AttemptModel.server_id == server_id,
        _AttemptModel.ip == ip,
    ).first()
    if row:
        row.attempts = (row.attempts or 0) + 1
        row.last_seen = now
        row.common_name = common_name
    else:
        row = _AttemptModel(
            ip=ip, server_id=server_id, common_name=common_name,
            attempts=1, first_seen=now, last_seen=now,
        )
        db.add(row)
    db.commit()

    # автобан: при достижении порога — банить один раз
    try:
        _maybe_autoban(db, ip, row.attempts)
    except Exception:
        pass


def _maybe_autoban(db, ip, attempts):
    """Если включён автобан и попытки достигли порога — банит IP (один раз)."""
    from models import Settings
    s = db.query(Settings).filter(Settings.id == 1).first()
    if not s or not getattr(s, "autoban_enabled", False):
        return
    threshold = getattr(s, "autoban_threshold", 10) or 10
    # банить ровно при достижении порога, чтобы не дёргать каждый раз
    if attempts == threshold:
        from services import fail2ban
        if fail2ban.is_installed():
            fail2ban.ban(ip)


def _poll_once(SessionLocal, VPNServer, VPNUser, ConnectionLog):
    db = SessionLocal()
    try:
        servers = db.query(VPNServer).all()
        seen = set()
        global_online = 0

        # строим карту pub_key -> user (нужна для WG)
        pub_to_user: dict = {}
        for u in db.query(VPNUser).filter(VPNUser.wg_public_key.isnot(None)).all():
            pub_to_user[u.wg_public_key] = u

        for s in servers:
            # ── WireGuard / AmneziaWG ──────────────────────────────────────
            if s.kind in WG_KINDS:
                peers = _parse_wg_dump(s.id, s.kind)
                server_online    = sum(1 for p in peers if p["is_online"])
                server_rx_delta  = 0
                server_tx_delta  = 0

                for p in peers:
                    user = pub_to_user.get(p["pub_key"])
                    cn   = user.username if user else p["pub_key"][:12]
                    addr = p["endpoint"]
                    key  = (s.id, cn, addr)

                    if p["is_online"]:
                        seen.add(key)

                    last = _last_bytes.get(key)
                    if last:
                        drx = max(0, p["rx"] - last[0])
                        dtx = max(0, p["tx"] - last[1])
                    else:
                        drx, dtx = 0, 0
                    _last_bytes[key] = (p["rx"], p["tx"])
                    server_rx_delta += drx
                    server_tx_delta += dtx

                    if p["is_online"] and key not in _open_sessions:
                        now = datetime.utcnow()
                        row = ConnectionLog(
                            user_id=user.id if user else None,
                            common_name=cn,
                            server_id=s.id,
                            real_address=addr,
                            virtual_address=user.wg_address if user else None,
                            connected_at=now,
                            bytes_received=p["rx"], bytes_sent=p["tx"],
                        )
                        db.add(row)
                        if user:
                            user.last_connected_at = now
                        db.commit()
                        _open_sessions[key] = row.id
                    elif key in _open_sessions:
                        row = db.query(ConnectionLog).filter(ConnectionLog.id == _open_sessions[key]).first()
                        if row:
                            row.bytes_received = p["rx"]
                            row.bytes_sent = p["tx"]
                            db.commit()

                # закрываем отключившиеся WG-пиры
                for key in list(_open_sessions.keys()):
                    if key[0] == s.id and key not in seen:
                        rid = _open_sessions.pop(key)
                        _last_bytes.pop(key, None)
                        row = db.query(ConnectionLog).filter(ConnectionLog.id == rid).first()
                        if row and row.disconnected_at is None:
                            row.disconnected_at = datetime.utcnow()
                            db.commit()

                global_online += server_online
                _add_bucket(s.id, server_rx_delta, server_tx_delta, server_online)
                continue

            # ── IKEv2 — только онлайн-счётчик (трафик swanctl не даёт легко) ──
            if s.kind == "ikev2":
                try:
                    import subprocess as _sp
                    out = _sp.check_output(
                        ["swanctl", "--list-sas"],
                        text=True, stderr=_sp.DEVNULL, timeout=5
                    )
                    ikev2_online = out.count("ESTABLISHED")
                except Exception:
                    ikev2_online = 0
                global_online += ikev2_online
                _add_bucket(s.id, 0, 0, ikev2_online)
                continue

            # ── OpenVPN ────────────────────────────────────────────────────
            status_log = os.path.join(DATA_DIR, "openvpn", f"status_{s.id}.log")
            clients = _parse_status(status_log)

            # UNDEF = TLS не прошёл (боты/сканеры) → в ConnectionAttempt, не в статистику
            for a in _parse_undef(status_log):
                try:
                    _record_attempt(db, s.id, a["common_name"], a["real_address"])
                except Exception:
                    db.rollback()
            clients = [c for c in clients if c["common_name"] != "UNDEF"]

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
                uid = None
                if key not in _open_sessions:
                    user = db.query(VPNUser).filter(
                        VPNUser.server_id == s.id,
                        VPNUser.username == c["common_name"],
                    ).first()
                    uid = user.id if user else None
                    now = datetime.utcnow()
                    row = ConnectionLog(
                        user_id=uid,
                        common_name=c["common_name"],
                        server_id=s.id,
                        real_address=c["real_address"],
                        virtual_address=c["virtual_address"],
                        connected_at=now,
                        bytes_received=rx, bytes_sent=tx,
                    )
                    db.add(row)
                    if user:
                        user.last_connected_at = now
                    db.commit()
                    _open_sessions[key] = row.id
                else:
                    row = db.query(ConnectionLog).filter(ConnectionLog.id == _open_sessions[key]).first()
                    if row:
                        uid = row.user_id
                        row.bytes_received = rx
                        row.bytes_sent = tx

                # учёт трафика для биллинга (инкремент по дельте)
                if uid and (drx + dtx) > 0:
                    try:
                        db.query(VPNUser).filter(VPNUser.id == uid).update(
                            {VPNUser.traffic_used: (VPNUser.traffic_used + (drx + dtx))}
                        )
                    except Exception:
                        pass
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


def start_tracker(SessionLocal, VPNServer, VPNUser, ConnectionLog, TrafficSample, ConnectionAttempt=None):
    global _thread, _stop, _AttemptModel
    if _thread and _thread.is_alive():
        return
    _stop = False
    _AttemptModel = ConnectionAttempt

    def loop():
        # при старте «усыновляем» открытые сессии прошлого запуска,
        # чтобы рестарт панели не создавал ложный обрыв+переподключение в истории
        db = SessionLocal()
        try:
            for row in db.query(ConnectionLog).filter(ConnectionLog.disconnected_at.is_(None)).all():
                key = (row.server_id, row.common_name, row.real_address)
                _open_sessions[key] = row.id
                _last_bytes[key] = (row.bytes_received or 0, row.bytes_sent or 0)
        except Exception:
            db.rollback()
        finally:
            db.close()
        # реально отвалившиеся закроются на первом же опросе (их не будет в seen)

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
