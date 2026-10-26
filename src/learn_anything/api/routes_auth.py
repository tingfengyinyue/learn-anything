from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import ApiToken, User
from ..security import hash_password, hash_token, new_token, verify_password
from .deps import admin_user, current_user
from .schemas import Credentials, NewUser
from .serialize import user_out

router = APIRouter(tags=["auth"])


def _issue(db: Session, user: User) -> dict:
    token = new_token()
    db.add(ApiToken(user_id=user.id, token_hash=hash_token(token)))
    db.commit()
    return {"token": token, "user": user_out(user)}


@router.post("/auth/register")
def register(body: Credentials, db: Session = Depends(get_db)):
    count = db.scalar(select(func.count()).select_from(User))
    if count and not get_settings().allow_signup:
        raise HTTPException(403, "服务端已关闭注册，请联系管理员创建账号")
    if db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(409, "用户名已存在")
    user = User(username=body.username, password_hash=hash_password(body.password), is_admin=count == 0)
    db.add(user)
    db.flush()
    return _issue(db, user)


@router.post("/auth/login")
def login(body: Credentials, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == body.username))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "用户名或密码错误")
    return _issue(db, user)


@router.post("/auth/logout")
def logout(authorization: str = Header(), user: User = Depends(current_user), db: Session = Depends(get_db)):
    token = authorization.split(" ", 1)[1].strip()
    row = db.scalar(select(ApiToken).where(ApiToken.token_hash == hash_token(token)))
    if row:
        db.delete(row)
        db.commit()
    return {"ok": True}


@router.get("/auth/me")
def me(user: User = Depends(current_user)):
    return user_out(user)


@router.get("/admin/users")
def list_users(_: User = Depends(admin_user), db: Session = Depends(get_db)):
    return [user_out(u) for u in db.scalars(select(User).order_by(User.id))]


@router.post("/admin/users")
def create_user(body: NewUser, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    if db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(409, "用户名已存在")
    user = User(username=body.username, password_hash=hash_password(body.password), is_admin=body.is_admin)
    db.add(user)
    db.commit()
    return user_out(user)
