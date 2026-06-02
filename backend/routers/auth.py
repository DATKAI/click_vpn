from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from database import get_db
from models import AdminUser
from schemas import LoginRequest, TokenResponse, AdminUserCreate, AdminUserOut
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


@router.post("/users", response_model=AdminUserOut)
def create_admin(
    data: AdminUserCreate,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(get_current_user),
):
    if db.query(AdminUser).filter(AdminUser.username == data.username).first():
        raise HTTPException(status_code=400, detail="Пользователь уже существует")
    user = AdminUser(username=data.username, password_hash=hash_password(data.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.get("/me", response_model=AdminUserOut)
def me(current: AdminUser = Depends(get_current_user)):
    return current
