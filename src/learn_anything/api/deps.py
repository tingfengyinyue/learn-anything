from datetime import timedelta

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ApiToken, Enrollment, LearningSession, User, utcnow
from ..security import hash_token

TOUCH_INTERVAL = timedelta(minutes=10)


def current_user(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "未登录：请先运行 `learn login`")
    token = authorization.split(" ", 1)[1].strip()
    row = db.scalar(select(ApiToken).where(ApiToken.token_hash == hash_token(token)))
    if row is None:
        raise HTTPException(401, "登录已失效，请重新 `learn login`")
    # 节流：每 10 分钟最多写一次，否则所有请求（包括只读的 GET）都要抢 SQLite 写锁
    now = utcnow()
    if row.last_used_at is None or now - row.last_used_at > TOUCH_INTERVAL:
        row.last_used_at = now
        db.commit()
    return row.user


def admin_user(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "需要管理员权限")
    return user


def own_enrollment(enrollment_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Enrollment:
    enr = db.get(Enrollment, enrollment_id)
    if enr is None or enr.user_id != user.id:
        raise HTTPException(404, "学习记录不存在")
    return enr


def own_session(session_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> LearningSession:
    sess = db.get(LearningSession, session_id)
    if sess is None or sess.enrollment.user_id != user.id:
        raise HTTPException(404, "会话不存在")
    return sess
