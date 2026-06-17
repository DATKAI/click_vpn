"""Биллинг: фоновая проверка лимитов и авто-блокировка/разблокировка.

Раз в N минут: для клиентов с тарифом проверяет срок (paid_until) и трафик
(traffic_used vs traffic_quota). Превышено → блокирует (billing_blocked).
Оплата вернулась/продлено → разблокирует (только если блокировал биллинг).
"""
import time
import threading
from datetime import datetime


def _check_once(SessionLocal):
    from models import Module, VPNUser, CertStatus, VPNServer, Plan
    db = SessionLocal()
    try:
        m = db.query(Module).filter(Module.name == "billing").first()
        if not m or not m.enabled:
            return
        now = datetime.utcnow()
        plans = {p.id: p for p in db.query(Plan).all()}
        users = db.query(VPNUser).filter(
            VPNUser.plan_id.isnot(None), VPNUser.archived == False
        ).all()
        changed_servers = set()
        for u in users:
            plan = plans.get(u.plan_id)
            # платный тариф (есть срок) и не оплачен/просрочен → блок
            needs_payment = bool(plan and plan.duration_days)
            not_paid = needs_payment and (u.paid_until is None or u.paid_until < now)
            over_traffic = bool(u.traffic_quota and (u.traffic_used or 0) >= u.traffic_quota)
            should_block = not_paid or over_traffic

            if should_block and u.is_active:
                u.is_active = False
                u.cert_status = CertStatus.revoked
                u.billing_blocked = True
                changed_servers.add(u.server_id)
            elif (not should_block) and u.billing_blocked and not u.is_active:
                # лимиты в норме, ранее блокировал биллинг → вернуть доступ
                u.is_active = True
                u.cert_status = CertStatus.active
                u.billing_blocked = False
                changed_servers.add(u.server_id)
        db.commit()

        # применяем изменения к серверам (CRL/resync) + kill заблокированных
        for sid in changed_servers:
            server = db.query(VPNServer).filter(VPNServer.id == sid).first()
            if not server:
                continue
            try:
                from routers.users import _wg_resync, ikev2_resync, WG_KINDS
                from services.crl import rebuild_crl
                from services import ovpn_mgmt
                if server.kind in WG_KINDS:
                    _wg_resync(db, server)
                elif server.kind == "ikev2":
                    ikev2_resync(db, server)
                elif server.ca_id:
                    rebuild_crl(db, server.ca_id)
                # разрываем сессии заблокированных на этом сервере
                for u in db.query(VPNUser).filter(
                    VPNUser.server_id == sid, VPNUser.billing_blocked == True
                ).all():
                    ovpn_mgmt.kill_client(sid, u.username)
            except Exception:
                pass
    except Exception:
        db.rollback()
    finally:
        db.close()


def start_checker(SessionLocal):
    def loop():
        time.sleep(45)
        while True:
            try:
                _check_once(SessionLocal)
            except Exception:
                pass
            time.sleep(300)   # каждые 5 минут
    threading.Thread(target=loop, daemon=True).start()
