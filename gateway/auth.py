"""Authentication: register, login, logout, session management."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Cookie, Depends, HTTPException, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from config import settings
from database import get_db
from models import Session, User


# --- Request/Response models ---

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    display_name: str = ""


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    id: str
    email: str
    display_name: str
    created_at: str

    model_config = {"from_attributes": True}


# --- Password hashing ---

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


# --- Session management ---

def generate_session_token() -> str:
    return secrets.token_urlsafe(64)


async def create_session(db: AsyncSession, user_id, response: Response) -> str:
    # Opportunistic cleanup: purge expired sessions (all users)
    try:
        await db.execute(
            delete(Session).where(Session.expires_at <= datetime.now(timezone.utc))
        )
        await db.flush()
    except Exception:
        logger.debug("Failed to purge expired sessions", exc_info=True)
        await db.rollback()

    token = generate_session_token()
    expires_at = datetime.now(timezone.utc) + timedelta(days=settings.session_expire_days)
    session = Session(user_id=user_id, token=token, expires_at=expires_at)
    db.add(session)
    await db.commit()

    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        httponly=True,
        samesite="lax",
        secure=True,
        max_age=settings.session_expire_days * 86400,
        path="/",
    )
    return token


async def get_current_user(
    db: AsyncSession = Depends(get_db),
    session_token: str | None = Cookie(None, alias="avt_session"),
) -> User | None:
    """Get current user from session cookie. Returns None if not authenticated."""
    if not session_token:
        return None

    result = await db.execute(
        select(Session).where(
            Session.token == session_token,
            Session.expires_at > datetime.now(timezone.utc),
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        return None

    result = await db.execute(
        select(User).where(User.id == session.user_id, User.is_active.is_(True))
    )
    return result.scalar_one_or_none()


async def require_auth(
    user: User | None = Depends(get_current_user),
) -> User | None:
    """Dependency that requires authentication when AUTH_REQUIRED=true.

    Returns the User if authenticated, or None if auth is not required
    and no session is present. Raises 401 when auth is required but
    no valid session exists.
    """
    if settings.auth_required and user is None:
        raise HTTPException(status_code=401, detail="未登录")
    return user


# --- Auth endpoints ---

async def register_handler(
    body: RegisterRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict:
    # Check if email already exists
    result = await db.execute(select(User).where(User.email == body.email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="该邮箱已注册")

    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")

    user = User(
        email=body.email,
        display_name=body.display_name or body.email.split("@")[0],
        password_hash=hash_password(body.password),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    await create_session(db, user.id, response)

    return {
        "user": {
            "id": str(user.id),
            "email": user.email,
            "display_name": user.display_name,
        }
    }


async def login_handler(
    body: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="邮箱或密码错误")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="账户已禁用")

    await create_session(db, user.id, response)

    return {
        "user": {
            "id": str(user.id),
            "email": user.email,
            "display_name": user.display_name,
        }
    }


async def logout_handler(
    response: Response,
    db: AsyncSession = Depends(get_db),
    session_token: str | None = Cookie(None, alias="avt_session"),
) -> dict:
    if session_token:
        result = await db.execute(
            select(Session).where(Session.token == session_token)
        )
        session = result.scalar_one_or_none()
        if session:
            await db.delete(session)
            await db.commit()

    response.delete_cookie(settings.session_cookie_name, path="/")
    return {"success": True}


async def me_handler(
    user: User | None = Depends(get_current_user),
) -> dict:
    if user is None:
        return {"user": None}
    return {
        "user": {
            "id": str(user.id),
            "email": user.email,
            "display_name": user.display_name,
            "created_at": user.created_at.isoformat(),
        }
    }
