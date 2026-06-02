"""Единая пересборка CRL: блокирует выключенных, архивных и удалённых клиентов."""
import os
from sqlalchemy.orm import Session

from services import pki
from models import CA, VPNUser, RevokedSerial

DATA_DIR = os.getenv("DATA_DIR", "./data")


def rebuild_crl(db: Session, ca_id: int):
    ca = db.query(CA).filter(CA.id == ca_id).first()
    if not ca:
        return
    serials = set()
    # выключенные / архивные активные пользователи
    for u in db.query(VPNUser).filter(
        VPNUser.ca_id == ca_id, VPNUser.cert_serial.isnot(None)
    ).all():
        if not u.is_active or u.archived:
            serials.add(u.cert_serial)
    # удалённые / отозванные навсегда
    for r in db.query(RevokedSerial).filter(RevokedSerial.ca_id == ca_id).all():
        serials.add(r.serial)

    crl_pem = pki.build_crl(ca.cert_pem, ca.key_pem, list(serials))
    path = os.path.join(DATA_DIR, "pki", f"crl_{ca_id}.pem")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(crl_pem)
