from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from models import AdminUser, TrafficSample, ConnectionLog, VPNServer, VPNUser, Organization
from auth import get_current_user

router = APIRouter(prefix="/api/stats", tags=["stats"])

RANGES = {
    "24h": (timedelta(hours=24), timedelta(hours=1),  "%H:00"),
    "7d":  (timedelta(days=7),   timedelta(days=1),   "%d.%m"),
    "30d": (timedelta(days=30),  timedelta(days=1),   "%d.%m"),
}


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

    online_now = sum(
        len(_status(s.id)) for s in db.query(VPNServer).all()
    )
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


def _status(server_id: int):
    import os
    from services.ovpn_manager import parse_status
    DATA_DIR = os.getenv("DATA_DIR", "./data")
    return parse_status(os.path.join(DATA_DIR, "openvpn", f"status_{server_id}.log"))


@router.get("/timeseries")
def timeseries(
    range: str = Query("24h"),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    span, bucket, label_fmt = RANGES.get(range, RANGES["24h"])
    now = datetime.utcnow()
    start = now - span

    rows = (
        db.query(TrafficSample)
        .filter(TrafficSample.server_id.is_(None), TrafficSample.timestamp >= start)
        .order_by(TrafficSample.timestamp.asc())
        .all()
    )

    # раскладываем по корзинам
    buckets = {}
    n = int(span / bucket)
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
        result.append({"name": s.name, "rx": int(agg[0]), "tx": int(agg[1]), "total": int(agg[0] + agg[1])})
    result.sort(key=lambda x: x["total"], reverse=True)
    return result


@router.get("/top-clients")
def top_clients(
    range: str = Query("7d"),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    span = RANGES.get(range, RANGES["7d"])[0]
    start = datetime.utcnow() - span
    # суммируем трафик по сессиям за период (по common_name)
    rows = (
        db.query(
            ConnectionLog.common_name,
            func.coalesce(func.sum(ConnectionLog.bytes_received), 0).label("rx"),
            func.coalesce(func.sum(ConnectionLog.bytes_sent), 0).label("tx"),
        )
        .filter(ConnectionLog.connected_at >= start)
        .group_by(ConnectionLog.common_name)
        .all()
    )
    data = [{"name": r[0], "rx": int(r[1]), "tx": int(r[2]), "total": int(r[1] + r[2])} for r in rows]
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
    # маппинг common_name -> org через VPNUser
    user_org = {}
    for u in db.query(VPNUser).all():
        user_org[u.username] = u.org_id
    org_names = {o.id: o.name for o in db.query(Organization).all()}

    totals = {}
    rows = (
        db.query(
            ConnectionLog.common_name,
            func.coalesce(func.sum(ConnectionLog.bytes_received + ConnectionLog.bytes_sent), 0),
        )
        .filter(ConnectionLog.connected_at >= start)
        .group_by(ConnectionLog.common_name)
        .all()
    )
    for cn, total in rows:
        oid = user_org.get(cn)
        name = org_names.get(oid, "Без организации")
        totals[name] = totals.get(name, 0) + int(total)

    return sorted(
        [{"name": k, "total": v} for k, v in totals.items()],
        key=lambda x: x["total"], reverse=True,
    )
