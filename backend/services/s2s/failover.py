"""Резервные каналы (failover) site-to-site.

Спица может быть помечена как резервный канал другой (Site.backup_of). У группы
«основная спица + её резервы» общая LAN филиала и разные туннели (можно разные
транспорты, напр. WG основной + GRE резерв).

Этот монитор отвечает за ВХОДЯЩЕЕ направление (хаб → филиал): периодически
определяет, какой канал группы жив (через transport.status), и направляет маршрут
к LAN филиала в активный канал с наивысшим приоритетом (основной → резервы по id).
Исходящий failover (филиал → хаб) делает роутер филиала сам.

Транспорты без маршрутизируемого интерфейса (policy-based IPsec, link_iface=None)
не могут быть участником маршрутного failover — пропускаются.
"""
import subprocess
import threading
import time


def _online_map(hub, tr):
    """Возвращает функцию is_online(spoke) для спиц данного хаба."""
    wg = {}      # pubkey -> online
    by_id = {}   # spoke_id -> online
    for tname in tr.available():
        try:
            for st in tr.get(tname).status(hub):
                if "pubkey" in st:
                    wg[st["pubkey"]] = st["online"]
                elif "spoke_id" in st:
                    by_id[st["spoke_id"]] = st["online"]
        except Exception:
            pass

    def is_online(spoke) -> bool:
        if spoke.transport == "wireguard":
            return bool(spoke.wg_public_key and wg.get(spoke.wg_public_key))
        return bool(by_id.get(spoke.id))

    return is_online


def run_once(SessionLocal):
    from models import Site
    from services.s2s import transport as tr

    db = SessionLocal()
    try:
        hubs = db.query(Site).filter(Site.role == "hub").all()
        for hub in hubs:
            spokes = db.query(Site).filter(Site.hub_id == hub.id).all()
            primaries = [s for s in spokes if not s.backup_of]
            is_online = None
            for primary in primaries:
                backups = sorted([s for s in spokes if s.backup_of == primary.id],
                                 key=lambda x: x.id)
                if not backups:
                    continue   # нет резерва — нечего переключать
                if is_online is None:
                    is_online = _online_map(hub, tr)
                # порядок приоритета: основной, затем резервы по id
                group = [primary] + backups
                best = None
                for link in group:
                    ifc = tr.get(link.transport).link_iface(hub, link)
                    if ifc and is_online(link):
                        best = (link, ifc)
                        break
                if not best:
                    continue
                _link, ifc = best
                # LAN филиала берём у основной спицы (общая для группы)
                for sn in primary.subnets:
                    subprocess.run(["ip", "route", "replace", sn.cidr, "dev", ifc],
                                   capture_output=True)
    except Exception:
        pass
    finally:
        db.close()


def start_checker(SessionLocal, interval: int = 30):
    def _loop():
        while True:
            run_once(SessionLocal)
            time.sleep(interval)
    threading.Thread(target=_loop, daemon=True).start()
