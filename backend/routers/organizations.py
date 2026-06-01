from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models import AdminUser, Organization, VPNServer, VPNUser
from schemas import OrgCreate, OrgUpdate, OrgOut
from auth import get_current_user

router = APIRouter(prefix="/api/organizations", tags=["organizations"])


def _org_to_out(org: Organization, db: Session) -> OrgOut:
    return OrgOut(
        id=org.id,
        name=org.name,
        description=org.description,
        created_at=org.created_at,
        server_ids=[s.id for s in org.servers],
        user_count=db.query(VPNUser).filter(VPNUser.org_id == org.id).count(),
    )


@router.get("", response_model=list[OrgOut])
def list_orgs(db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    orgs = db.query(Organization).order_by(Organization.name).all()
    return [_org_to_out(o, db) for o in orgs]


@router.post("", response_model=OrgOut)
def create_org(
    data: OrgCreate,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    if db.query(Organization).filter(Organization.name == data.name).first():
        raise HTTPException(400, "Организация с таким именем уже существует")

    org = Organization(name=data.name, description=data.description)

    if data.server_ids:
        servers = db.query(VPNServer).filter(VPNServer.id.in_(data.server_ids)).all()
        org.servers = servers

    db.add(org)
    db.commit()
    db.refresh(org)
    return _org_to_out(org, db)


@router.put("/{org_id}", response_model=OrgOut)
def update_org(
    org_id: int,
    data: OrgUpdate,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(404, "Организация не найдена")

    if data.name is not None:
        org.name = data.name
    if data.description is not None:
        org.description = data.description
    if data.server_ids is not None:
        org.servers = db.query(VPNServer).filter(VPNServer.id.in_(data.server_ids)).all()

    db.commit()
    db.refresh(org)
    return _org_to_out(org, db)


@router.delete("/{org_id}", status_code=204)
def delete_org(
    org_id: int,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_user),
):
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(404, "Организация не найдена")
    if db.query(VPNUser).filter(VPNUser.org_id == org_id).count():
        raise HTTPException(400, "Нельзя удалить организацию — есть привязанные пользователи")
    db.delete(org)
    db.commit()
