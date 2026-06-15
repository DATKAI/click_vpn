import subprocess
import re
import time
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from models import (AdminUser, TrafficSample, ConnectionLog, VPNServer, VPNUser,
                    Organization, ConnectionAttempt)
from auth import get_current_user
from services import audit

router = APIRouter(prefix="/api/stats", tags=["stats"])


class IPBody(BaseModel):
    ip: str


class Fail2banConfig(BaseModel):
    maxretry: int = 5
    findtime: int = 600
    bantime: int = 3600
    ignoreip: str = ""

RANGES = {
    "24h": (timedelta(hours=24), timedelta(hours=1),  "%H:00"),
    "7d":  (timedelta(days=7),   timedelta(days=1),   "%d.%m"),
    "30d": (timedelta(days=30),  timedelta(days=1),   "%d.%m"),
}

PROTO_LABELS = {
    "openvpn":         "OpenVPN",
    "wireguard":       "WireGuard",
    "amneziawg":       "AmneziaWG 2.0",
    "amneziawg_legacy":"AmneziaWG legacy",
    "ikev2":           "IKEv2",
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _status(server_id: int) -> list[dict]:
    """Реальные OpenVPN-клиенты онлайн (без UNDEF — это боты/неуд. TLS)."""
    import os
    from services.ovpn_manager import parse_status
    DATA_DIR = os.getenv("DATA_DIR", "./data")
    clients = parse_status(os.path.join(DATA_DIR, "openvpn", f"status_{server_id}.log"))
    return [c for c in clients if c.get("common_name") != "UNDEF"]


def _wg_online(db: Session) -> list[dict]:
    """Онлайн-клиенты WireGuard/AmneziaWG через `wg show <iface> dump`."""
    WG_KINDS = ("wireguard", "amneziawg", "amneziawg_legacy")
    now_ts = time.time()
    result = []
    servers = db.query(VPNServer).filter(VPNServer.kind.in_(WG_KINDS)).all()
    if not servers:
        return []
    pub_to_user: dict = {}
    for u in db.query(VPNUser).filter(VPNUser.wg_public_key.isnot(None)).all():
        pub_to_user[u.wg_public_key] = u

    for s in servers:
        wg = "awg" if s.kind.startswith("amnez") else "wg"
        iface = f"wg{s.id}"
        try:
            out = subprocess.check_output(
                [wg, "show", iface, "dump"],
                text=True, stderr=subprocess.DEVNULL, timeout=3
            )
        except Exception:
            continue
        lines = out.strip().splitlines()
        for line in lines[1:]:         # первая строка — интерфейс
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            pub_key            = parts[0]
            endpoint           = parts[2]
            latest_handshake   = int(parts[4]) if parts[4].isdigit() else 0
            rx                 = int(parts[5]) if parts[5].isdigit() else 0
            tx                 = int(parts[6]) if parts[6].isdigit() else 0
            # считаем онлайн: хендшейк < 3 минут назад
            if latest_handshake == 0 or (now_ts - latest_handshake) > 180:
                continue
            user = pub_to_user.get(pub_key)
            result.append({
                "username":    user.username if user else pub_key[:12] + "…",
                "full_name":   user.full_name if user else None,
                "server_id":   s.id,
                "server_name": s.name,
                "protocol":    s.kind,
                "real_address": endpoint if endpoint != "(none)" else "—",
                "vpn_address": user.wg_address if user else "—",
                "bytes_rx":    rx,
                "bytes_tx":    tx,
                "connected_since":    None,
                "last_handshake_ago": int(now_ts - latest_handshake),
            })
    return result


def _ikev2_online(db: Session) -> list[dict]:
    """Онлайн-клиенты IKEv2 через `swanctl --list-sas`."""
    servers = db.query(VPNServer).filter(VPNServer.kind == "ikev2").all()
    if not servers:
        return []
    srv_map  = {s.id: s for s in servers}
    user_map = {}
    for u in db.query(VPNUser).filter(VPNUser.server_id.in_([s.id for s in servers])).all():
        user_map[u.username] = u

    try:
        out = subprocess.check_output(
            ["swanctl", "--list-sas"],
            text=True, stderr=subprocess.DEVNULL, timeout=5
        )
    except Exception:
        return []

    result = []
    cur_conn = cur_remote = cur_ip = None
    established_ago = None

    for raw in out.splitlines():
        line = raw.strip()
        # "clickvpn-1: #N, ESTABLISHED, IKEv2, ..."
        m = re.match(r"^(clickvpn-(\d+)):", line)
        if m:
            cur_conn = m.group(1)
            cur_remote = cur_ip = None
            established_ago = None
            if "ESTABLISHED" not in line:
                cur_conn = None
            continue
        if not cur_conn:
            continue
        # "remote 'user' @ 1.2.3.4[port]"
        m2 = re.match(r"remote\s+'([^']+)'\s+@\s+([\d.]+)\[", line)
        if m2:
            cur_remote = m2.group(1)
            cur_ip = m2.group(2)
            continue
        # "established 12s ago, ..."
        m3 = re.match(r"established\s+(\d+)s ago", line)
        if m3:
            established_ago = int(m3.group(1))
            m_id = re.match(r"clickvpn-(\d+)", cur_conn)
            srv_id = int(m_id.group(1)) if m_id else None
            srv = srv_map.get(srv_id)
            user = user_map.get(cur_remote)
            result.append({
                "username":    cur_remote or "unknown",
                "full_name":   user.full_name if user else None,
                "server_id":   srv_id,
                "server_name": srv.name if srv else "IKEv2",
                "protocol":    "ikev2",
                "real_address": cur_ip or "—",
                "vpn_address": "—",
                "bytes_rx":    0,
                "bytes_tx":    0,
                "connected_since":    None,
                "last_handshake_ago": established_ago,
            })
    return result


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.get("/summary")
def summary(db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    now = datetime.utcnow()
    day_ago = now - timedelta(hours=24)

    total = db.query(
        func.coalesce(func.sum(TrafficSample.rx), 0),
        func.coalesce(func.sum(TrafficSample.tx), 0),
    ).filter(TrafficSample.server_id.is_(None)).first()

    today = db.query(
        func.coalesce(func.sum(TrafficSample.rx), 0),
        func.coalesce(func.sum(TrafficSample.tx), 0),
    ).filter(TrafficSample.server_id.is_(None), TrafficSample.timestamp >= day_ago).first()

    # онлайн по всем протоколам
    online_now = 0
    for s in db.query(VPNServer).all():
        if s.kind in ("wireguard", "amneziawg", "amneziawg_legacy"):
            pass  # считается через wg_online ниже
        elif s.kind == "ikev2":
            pass
        else:
            online_now += len(_status(s.id))
    # добавляем WG и IKEv2
    try:
        online_now += len(_wg_online(db))
    except Exception:
        pass
    try:
        online_now += len(_ikev2_online(db))
    except Exception:
        pass

    sessions_today = db.query(ConnectionLog).filter(ConnectionLog.connected_at >= day_ago).count()
    active_clients = db.query(VPNUser).filter(
        VPNUser.is_active == True, VPNUser.archived == False
    ).count()

    return {
        "total_rx": int(total[0]), "total_tx": int(total[1]),
        "total_bytes": int(total[0] + total[1]),
        "today_rx": int(today[0]), "today_tx": int(today[1]),
        "today_bytes": int(today[0] + today[1]),
        "online_now": online_now,
        "sessions_today": sessions_today,
        "active_clients": active_clients,
    }


@router.get("/online")
def online_now_endpoint(db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    """Все клиенты онлайн прямо сейчас (все протоколы)."""
    result = []

    # OpenVPN
    for s in db.query(VPNServer).filter(VPNServer.kind == "openvpn").all():
        clients = _status(s.id)
        for c in clients:
            user = db.query(VPNUser).filter(
                VPNUser.server_id == s.id,
                VPNUser.username == c["common_name"],
            ).first()
            result.append({
                "username":    c["common_name"],
                "full_name":   user.full_name if user else None,
                "server_id":   s.id,
                "server_name": s.name,
                "protocol":    "openvpn",
                "real_address": c.get("real_address", "—"),
                "vpn_address": c.get("virtual_address", "—"),
                "bytes_rx":    c.get("bytes_received", 0),
                "bytes_tx":    c.get("bytes_sent", 0),
                "connected_since": c.get("connected_since"),
                "last_handshake_ago": None,
            })

    # WireGuard / AmneziaWG
    try:
        result += _wg_online(db)
    except Exception:
        pass

    # IKEv2
    try:
        result += _ikev2_online(db)
    except Exception:
        pass

    result.sort(key=lambda x: x["username"])
    return result


@router.get("/timeseries")
def timeseries(
    range_: str = Query("24h", alias="range"),
    server_id: int = Query(None),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    span, bucket, label_fmt = RANGES.get(range_, RANGES["24h"])
    now = datetime.utcnow()
    start = now - span

    q = db.query(TrafficSample).filter(TrafficSample.timestamp >= start)
    if server_id:
        q = q.filter(TrafficSample.server_id == server_id)
    else:
        q = q.filter(TrafficSample.server_id.is_(None))
    rows = q.order_by(TrafficSample.timestamp.asc()).all()

    n = int(span / bucket)
    buckets = {}
    for i in range(n + 1):
        t = start + bucket * i
        k = int(t.timestamp() // bucket.total_seconds())
        buckets[k] = {"t": t, "rx": 0, "tx": 0, "online": 0}

    for r in rows:
        k = int(r.timestamp.timestamp() // bucket.total_seconds())
        if k in buckets:
            buckets[k]["rx"] += r.rx or 0
            buckets[k]["tx"] += r.tx or 0
            buckets[k]["online"] = max(buckets[k]["online"], r.online or 0)

    labels, rx, tx, online = [], [], [], []
    for k in sorted(buckets):
        b = buckets[k]
        labels.append(b["t"].strftime(label_fmt))
        rx.append(int(b["rx"]))
        tx.append(int(b["tx"]))
        online.append(int(b["online"]))

    return {"labels": labels, "rx": rx, "tx": tx, "online": online}


@router.get("/by-server")
def by_server(
    range: str = Query("7d"),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    span = RANGES.get(range, RANGES["7d"])[0]
    start = datetime.utcnow() - span
    result = []
    for s in db.query(VPNServer).all():
        agg = db.query(
            func.coalesce(func.sum(TrafficSample.rx), 0),
            func.coalesce(func.sum(TrafficSample.tx), 0),
        ).filter(TrafficSample.server_id == s.id, TrafficSample.timestamp >= start).first()
        result.append({
            "id": s.id,
            "name": s.name,
            "protocol": s.kind or "openvpn",
            "rx": int(agg[0]),
            "tx": int(agg[1]),
            "total": int(agg[0] + agg[1]),
        })
    result.sort(key=lambda x: x["total"], reverse=True)
    return result


@router.get("/by-protocol")
def by_protocol(
    range: str = Query("7d"),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    """Трафик разбитый по протоколам (kind сервера)."""
    span = RANGES.get(range, RANGES["7d"])[0]
    start = datetime.utcnow() - span
    totals: dict = {}
    for s in db.query(VPNServer).all():
        kind = s.kind or "openvpn"
        agg = db.query(
            func.coalesce(func.sum(TrafficSample.rx), 0),
            func.coalesce(func.sum(TrafficSample.tx), 0),
        ).filter(TrafficSample.server_id == s.id, TrafficSample.timestamp >= start).first()
        totals[kind] = totals.get(kind, 0) + int(agg[0] + agg[1])
    return sorted(
        [{"protocol": k, "label": PROTO_LABELS.get(k, k), "total": v} for k, v in totals.items()],
        key=lambda x: -x["total"],
    )


@router.get("/top-clients")
def top_clients(
    range: str = Query("7d"),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    span = RANGES.get(range, RANGES["7d"])[0]
    start = datetime.utcnow() - span
    rows = (
        db.query(
            ConnectionLog.common_name,
            func.coalesce(func.sum(ConnectionLog.bytes_received), 0).label("rx"),
            func.coalesce(func.sum(ConnectionLog.bytes_sent), 0).label("tx"),
            func.count(ConnectionLog.id).label("sessions"),
        )
        .filter(ConnectionLog.connected_at >= start, ConnectionLog.common_name != "UNDEF")
        .group_by(ConnectionLog.common_name)
        .all()
    )
    data = [{"name": r[0], "rx": int(r[1]), "tx": int(r[2]),
             "total": int(r[1] + r[2]), "sessions": int(r[3])} for r in rows]
    data.sort(key=lambda x: x["total"], reverse=True)
    return data[:limit]


@router.get("/by-org")
def by_org(
    range: str = Query("7d"),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    span = RANGES.get(range, RANGES["7d"])[0]
    start = datetime.utcnow() - span
    user_org = {u.username: u.org_id for u in db.query(VPNUser).all()}
    org_names = {o.id: o.name for o in db.query(Organization).all()}

    totals: dict = {}
    rows = (
        db.query(
            ConnectionLog.common_name,
            func.coalesce(func.sum(ConnectionLog.bytes_received + ConnectionLog.bytes_sent), 0),
        )
        .filter(ConnectionLog.connected_at >= start, ConnectionLog.common_name != "UNDEF")
        .group_by(ConnectionLog.common_name)
        .all()
    )
    for cn, total in rows:
        oid  = user_org.get(cn)
        name = org_names.get(oid, "Без организации")
        totals[name] = totals.get(name, 0) + int(total)

    return sorted(
        [{"name": k, "total": v} for k, v in totals.items()],
        key=lambda x: -x["total"],
    )


@router.get("/sessions")
def sessions(
    server_id: int = Query(None),
    limit: int   = Query(50, ge=1, le=200),
    offset: int  = Query(0, ge=0),
    db: Session  = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    """История сессий из ConnectionLog (с пагинацией)."""
    q = db.query(ConnectionLog).filter(ConnectionLog.common_name != "UNDEF")
    if server_id:
        q = q.filter(ConnectionLog.server_id == server_id)
    total = q.count()
    rows  = q.order_by(ConnectionLog.connected_at.desc()).offset(offset).limit(limit).all()

    srv_names = {s.id: (s.name, s.kind or "openvpn") for s in db.query(VPNServer).all()}
    items = []
    for r in rows:
        sname, skind = srv_names.get(r.server_id, (f"#{r.server_id}", "openvpn"))
        dur = None
        if r.disconnected_at and r.connected_at:
            dur = int((r.disconnected_at - r.connected_at).total_seconds())
        items.append({
            "id":             r.id,
            "username":       r.common_name,
            "server_id":      r.server_id,
            "server_name":    sname,
            "protocol":       skind,
            "real_address":   r.real_address or "—",
            "vpn_address":    r.virtual_address or "—",
            "connected_at":   r.connected_at.isoformat() if r.connected_at else None,
            "disconnected_at":r.disconnected_at.isoformat() if r.disconnected_at else None,
            "duration_sec":   dur,
            "bytes_rx":       r.bytes_received or 0,
            "bytes_tx":       r.bytes_sent or 0,
        })
    return {"items": items, "total": total}


@router.get("/attempts")
def attempts(
    limit: int  = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    """Неудачные/анонимные попытки подключения (CN=UNDEF) — боты/сканеры.
    Сгруппировано по IP, отсортировано по последней активности."""
    srv_names = {s.id: s.name for s in db.query(VPNServer).all()}
    rows = (
        db.query(ConnectionAttempt)
        .order_by(ConnectionAttempt.last_seen.desc())
        .limit(limit)
        .all()
    )
    items = [{
        "id":          r.id,
        "ip":          r.ip,
        "server_id":   r.server_id,
        "server_name": srv_names.get(r.server_id, f"#{r.server_id}"),
        "common_name": r.common_name or "UNDEF",
        "attempts":    r.attempts or 1,
        "first_seen":  r.first_seen.isoformat() if r.first_seen else None,
        "last_seen":   r.last_seen.isoformat() if r.last_seen else None,
    } for r in rows]

    total_attempts = db.query(func.coalesce(func.sum(ConnectionAttempt.attempts), 0)).scalar() or 0
    unique_ips     = db.query(func.count(func.distinct(ConnectionAttempt.ip))).scalar() or 0
    return {"items": items, "unique_ips": int(unique_ips), "total_attempts": int(total_attempts)}


@router.delete("/attempts")
def clear_attempts(db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    """Очистить журнал попыток подключения."""
    db.query(ConnectionAttempt).delete()
    db.commit()
    return {"status": "cleared"}


# ── fail2ban: чёрный список ───────────────────────────────────────────────────

@router.get("/bans")
def list_bans(_: AdminUser = Depends(get_current_user)):
    """Статус fail2ban + список забаненных IP."""
    from services import fail2ban
    return fail2ban.status()


@router.get("/fail2ban-config")
def get_fail2ban_config(_: AdminUser = Depends(get_current_user)):
    """Текущие параметры jail fail2ban."""
    from services import fail2ban
    return {"installed": fail2ban.is_installed(), **fail2ban.get_config()}


@router.post("/fail2ban-config")
def set_fail2ban_config(
    data: Fail2banConfig,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_user),
):
    """Сохранить параметры jail fail2ban и перезагрузить."""
    from services import fail2ban
    ok, msg = fail2ban.set_config(data.maxretry, data.findtime, data.bantime, data.ignoreip)
    if not ok:
        raise HTTPException(400, msg)
    audit.log(db, admin.username, "security.fail2ban_config",
              details=f"maxretry={data.maxretry} bantime={data.bantime}")
    return {"status": "ok", "message": msg}


@router.post("/ban")
def ban_ip(
    data: IPBody,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_user),
):
    """Вручную забанить IP через fail2ban."""
    from services import fail2ban
    ip = (data.ip or "").strip()
    if not ip:
        raise HTTPException(400, "IP не указан")
    ok, msg = fail2ban.ban(ip)
    if not ok:
        raise HTTPException(400, msg)
    audit.log(db, admin.username, "security.ban", ip)
    return {"status": "ok", "message": msg}


@router.post("/unban")
def unban_ip(
    data: IPBody,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_user),
):
    """Снять бан с IP."""
    from services import fail2ban
    ip = (data.ip or "").strip()
    if not ip:
        raise HTTPException(400, "IP не указан")
    ok, msg = fail2ban.unban(ip)
    if not ok:
        raise HTTPException(400, msg)
    audit.log(db, admin.username, "security.unban", ip)
    return {"status": "ok", "message": msg}


class BanThreshold(BaseModel):
    min_attempts: int = 3


@router.post("/ban-threshold")
def ban_by_threshold(
    data: BanThreshold,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_user),
):
    """Разово забанить все IP с количеством попыток >= порога."""
    from services import fail2ban
    if not fail2ban.is_installed():
        raise HTTPException(400, "fail2ban не установлен")
    thr = max(1, int(data.min_attempts))
    rows = db.query(ConnectionAttempt).filter(ConnectionAttempt.attempts >= thr).all()
    banned = 0
    already = set(fail2ban.status().get("banned_ips", []))
    for r in rows:
        if r.ip in already:
            continue
        ok, _ = fail2ban.ban(r.ip)
        if ok:
            banned += 1
    audit.log(db, admin.username, "security.ban_threshold", f">={thr}", f"{banned} IP")
    return {"status": "ok", "banned": banned}


# ── Здоровье системы + статус серверов ────────────────────────────────────────

def _online_counts(db: Session) -> dict:
    """{server_id: число клиентов онлайн} по всем протоколам."""
    counts = {}
    for s in db.query(VPNServer).filter(VPNServer.kind == "openvpn").all():
        counts[s.id] = len(_status(s.id))
    try:
        for c in _wg_online(db):
            counts[c["server_id"]] = counts.get(c["server_id"], 0) + 1
    except Exception:
        pass
    try:
        for c in _ikev2_online(db):
            counts[c["server_id"]] = counts.get(c["server_id"], 0) + 1
    except Exception:
        pass
    return counts


def _server_running(s) -> bool:
    """Запущен ли VPN-сервис данного сервера."""
    import os as _os
    from services import ovpn_manager
    try:
        if s.kind in ("wireguard", "amneziawg", "amneziawg_legacy"):
            from services import wireguard
            return wireguard.is_running(s.id)
        if s.kind == "ikev2":
            from services import ikev2
            return ikev2.is_running()
        return ovpn_manager.is_running(s.id, _os.getenv("DATA_DIR", "./data"))
    except Exception:
        return False


@router.get("/system-health")
def system_health(db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    """Метрики сервера (CPU/RAM/диск/uptime) + статус каждого VPN-сервера."""
    import os as _os
    from services import syshealth
    from database import DATABASE_URL

    db_path = None
    if DATABASE_URL.startswith("sqlite"):
        db_path = DATABASE_URL.split("///", 1)[-1]
    data_dir = _os.getenv("DATA_DIR", "./data")

    health = syshealth.collect(db_path=db_path, data_dir=data_dir)

    online = _online_counts(db)
    servers = []
    for s in db.query(VPNServer).all():
        servers.append({
            "id":       s.id,
            "name":     s.name,
            "kind":     s.kind or "openvpn",
            "protocol": PROTO_LABELS.get(s.kind or "openvpn", s.kind),
            "port":     s.port,
            "running":  _server_running(s),
            "online":   online.get(s.id, 0),
        })
    servers.sort(key=lambda x: (not x["running"], x["name"]))
    health["servers"] = servers
    return health


# ── Аналитика: длительность сессий + пиковые часы ─────────────────────────────

@router.get("/session-stats")
def session_stats(
    range_: str = Query("7d", alias="range"),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    """Статистика длительности завершённых сессий за период."""
    span = RANGES.get(range_, RANGES["7d"])[0]
    start = datetime.utcnow() - span

    rows = (
        db.query(ConnectionLog.connected_at, ConnectionLog.disconnected_at)
        .filter(
            ConnectionLog.connected_at >= start,
            ConnectionLog.disconnected_at.isnot(None),
            ConnectionLog.common_name != "UNDEF",
        )
        .all()
    )
    durs = []
    for con, dis in rows:
        if con and dis:
            d = (dis - con).total_seconds()
            if d >= 0:
                durs.append(d)

    # распределение по корзинам длительности
    bins = [
        ("< 5 мин",   0,        300),
        ("5–30 мин",  300,      1800),
        ("30 мин–2 ч",1800,     7200),
        ("2–8 ч",     7200,     28800),
        ("> 8 ч",     28800,    float("inf")),
    ]
    dist = []
    for label, lo, hi in bins:
        dist.append({"label": label, "count": sum(1 for d in durs if lo <= d < hi)})

    durs_sorted = sorted(durs)
    n = len(durs_sorted)
    median = durs_sorted[n // 2] if n else 0
    avg = (sum(durs_sorted) / n) if n else 0

    # активные (не закрытые) сессии для контекста
    active = (
        db.query(func.count(ConnectionLog.id))
        .filter(ConnectionLog.disconnected_at.is_(None),
                ConnectionLog.common_name != "UNDEF")
        .scalar() or 0
    )

    return {
        "total_sessions": n,
        "active_sessions": int(active),
        "avg_sec":    int(avg),
        "median_sec": int(median),
        "max_sec":    int(durs_sorted[-1]) if n else 0,
        "distribution": dist,
    }


@router.get("/heatmap")
def heatmap(
    range_: str = Query("30d", alias="range"),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    """Тепловая карта активности: день недели (0=Пн) × час (0–23).
    Значение = число начатых сессий. Для планирования окон обслуживания."""
    span = RANGES.get(range_, RANGES["30d"])[0]
    start = datetime.utcnow() - span

    rows = (
        db.query(ConnectionLog.connected_at)
        .filter(ConnectionLog.connected_at >= start,
                ConnectionLog.common_name != "UNDEF")
        .all()
    )
    grid = [[0] * 24 for _ in range(7)]   # [день][час]
    peak_hour_totals = [0] * 24
    for (con,) in rows:
        if not con:
            continue
        wd = con.weekday()    # 0=Пн … 6=Вс
        hr = con.hour
        grid[wd][hr] += 1
        peak_hour_totals[hr] += 1

    busiest_hour = max(range(24), key=lambda h: peak_hour_totals[h]) if rows else None
    return {
        "grid": grid,
        "total": len(rows),
        "busiest_hour": busiest_hour,
        "hour_totals": peak_hour_totals,
    }
