from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from database import get_db
from models import AdminUser
from schemas import (LoginRequest, TokenResponse, AdminUserCreate, AdminUserOut,
                     AdminPasswordChange)
from auth import hash_password, verify_password, create_access_token, get_current_user
from services import audit

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
def login(data: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(AdminUser).filter(AdminUser.username == data.username).first()
    if not user or not verify_password(data.password, user.password_hash):
        audit.log(db, data.username, "login.failed", details="неверный пароль")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверный логин или пароль")
    token = create_access_token({"sub": user.username})
    audit.log(db, user.username, "login")
    return TokenResponse(access_token=token)


@router.get("/users", response_model=list[AdminUserOut])
def list_admins(db: Session = Depends(get_db), _: AdminUser = Depends(get_current_user)):
    return db.query(AdminUser).order_by(AdminUser.id.asc()).all()


@router.post("/users", response_model=AdminUserOut)
def create_admin(
    data: AdminUserCreate,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(get_current_user),
):
    if not data.username.strip() or not data.password:
        raise HTTPException(400, "Укажите логин и пароль")
    if db.query(AdminUser).filter(AdminUser.username == data.username).first():
        raise HTTPException(status_code=400, detail="Пользователь уже существует")
    user = AdminUser(username=data.username.strip(), password_hash=hash_password(data.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    audit.log(db, current.username, "admin.create", user.username)
    return user


@router.put("/users/{admin_id}/password")
def change_admin_password(
    admin_id: int,
    data: AdminPasswordChange,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(get_current_user),
):
    """Сменить пароль администратора. При смене СВОЕГО пароля нужен старый."""
    target = db.query(AdminUser).filter(AdminUser.id == admin_id).first()
    if not target:
        raise HTTPException(404, "Администратор не найден")
    if not data.new_password:
        raise HTTPException(400, "Новый пароль не указан")
    if target.id == current.id:
        if not data.old_password or not verify_password(data.old_password, current.password_hash):
            raise HTTPException(400, "Неверный текущий пароль")
    target.password_hash = hash_password(data.new_password)
    db.commit()
    audit.log(db, current.username, "admin.password", target.username)
    return {"status": "ok"}


@router.post("/users/{admin_id}/toggle", response_model=AdminUserOut)
def toggle_admin(
    admin_id: int,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(get_current_user),
):
    """Включить/выключить администратора."""
    target = db.query(AdminUser).filter(AdminUser.id == admin_id).first()
    if not target:
        raise HTTPException(404, "Администратор не найден")
    if target.id == current.id:
        raise HTTPException(400, "Нельзя отключить самого себя")
    if target.is_active and db.query(AdminUser).filter(AdminUser.is_active == True).count() <= 1:
        raise HTTPException(400, "Нельзя отключить последнего активного администратора")
    target.is_active = not target.is_active
    db.commit()
    db.refresh(target)
    audit.log(db, current.username, "admin.toggle", target.username,
              "enabled" if target.is_active else "disabled")
    return target


@router.delete("/users/{admin_id}", status_code=204)
def delete_admin(
    admin_id: int,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(get_current_user),
):
    target = db.query(AdminUser).filter(AdminUser.id == admin_id).first()
    if not target:
        raise HTTPException(404, "Администратор не найден")
    if target.id == current.id:
        raise HTTPException(400, "Нельзя удалить самого себя")
    if db.query(AdminUser).count() <= 1:
        raise HTTPException(400, "Нельзя удалить последнего администратора")
    uname = target.username
    db.delete(target)
    db.commit()
    audit.log(db, current.username, "admin.delete", uname)


@router.get("/me", response_model=AdminUserOut)
def me(current: AdminUser = Depends(get_current_user)):
    return current
