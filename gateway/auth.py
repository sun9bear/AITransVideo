"""Authentication: register, login, logout, session management."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Cookie, Depends, HTTPException, Request, Response
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
    # A1: "account" field accepts phone number OR email address.
    # Field kept named "email" for backward compat with legacy clients,
    # but the handler resolves it against both User.email and User.phone_number.
    email: str
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
    """Public email registration is CLOSED as of Task 3.

    The phone-first flow at `POST /auth/phone/send-code` +
    `POST /auth/phone/verify-code` is now the only supported public
    registration path. We keep this handler in place (and keep accepting a
    well-formed request body) so legacy clients get an actionable error
    instead of a 404 or a confusing schema-validation crash.

    `AVT_EMAIL_REGISTRATION_ENABLED=true` can re-open this path for emergency
    operator use, but the default is `false` and production must leave it so.
    """
    if not settings.email_registration_enabled:
        raise HTTPException(
            status_code=403,
            detail="邮箱注册已关闭,请使用手机号验证码注册",
        )

    # --- Legacy fallback path (only reachable when explicitly re-enabled) ---
    result = await db.execute(select(User).where(User.email == body.email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="该邮箱已注册")

    if len(body.password) < 12:
        raise HTTPException(status_code=400, detail="密码至少 12 位")

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
            "email": user.email or "",
            "display_name": user.display_name,
        }
    }


async def login_handler(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Unified account + password login (A1 rewrite).

    The "email" field now serves as a generic "account" field that accepts:
    - Phone number (11-digit CN mobile) → queries User.phone_number
    - Email address → queries User.email (legacy accounts)

    This keeps backward compat for old email users while letting phone-
    registered users log in with phone + password from the same form.
    """
    account = (body.email or "").strip()
    if not account:
        raise HTTPException(status_code=400, detail="请输入账号")

    # P1-10a-1 (audit 2026-05-07, S-HIGH-3): rate limit before bcrypt
    # to prevent credential stuffing. _client_ip respects the trusted-
    # proxy boundary added in the same audit.
    from auth_phone import _client_ip  # late import to avoid cycle
    from risk_control import (
        RateLimitExceeded,
        check_login_allowed,
        record_login_failure,
    )
    client_ip = _client_ip(request)
    try:
        check_login_allowed(account, client_ip)
    except RateLimitExceeded as exc:
        raise HTTPException(status_code=429, detail=exc.message)

    # Determine if the account looks like a phone number or email.
    import re
    normalized_account = re.sub(r"[\s\-\(\)]+", "", account)
    if normalized_account.startswith("+86"):
        normalized_account = normalized_account[3:]
    elif normalized_account.startswith("86") and len(normalized_account) == 13:
        normalized_account = normalized_account[2:]

    is_phone = bool(re.match(r"^1[3-9]\d{9}$", normalized_account))

    if is_phone:
        result = await db.execute(
            select(User).where(User.phone_number == normalized_account)
        )
    else:
        result = await db.execute(
            select(User).where(User.email == account)
        )
    user = result.scalar_one_or_none()

    if not user or not user.password_hash or not verify_password(body.password, user.password_hash):
        record_login_failure(account, client_ip)
        raise HTTPException(status_code=401, detail="账号或密码错误")

    if not user.is_active:
        record_login_failure(account, client_ip)
        raise HTTPException(status_code=403, detail="账户已禁用")

    await create_session(db, user.id, response)

    return {
        "user": {
            "id": str(user.id),
            "email": user.email or "",
            "display_name": user.display_name,
            "phone_number": getattr(user, "phone_number", None),
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
            # Phone-only users have no email on file. Return "" so existing
            # frontend consumers that expect a string don't crash.
            "email": user.email or "",
            "display_name": user.display_name,
            "role": getattr(user, "role", "user") or "user",
            "phone_number": getattr(user, "phone_number", None),
            "plan_code": getattr(user, "plan_code", "free") or "free",
            "created_at": user.created_at.isoformat(),
        }
    }


# --- Change password ---

class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


async def change_password_handler(
    body: ChangePasswordRequest,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user),
) -> dict:
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    if not body.new_password or len(body.new_password) < 12:
        raise HTTPException(status_code=400, detail="新密码长度至少 12 位")
    # If user has a password, verify the old one
    if user.password_hash:
        if not body.old_password:
            raise HTTPException(status_code=400, detail="请输入当前密码")
        if not verify_password(body.old_password, user.password_hash):
            raise HTTPException(status_code=400, detail="当前密码错误")
    # Set new password
    user.password_hash = hash_password(body.new_password)
    await db.commit()
    return {"success": True}


# --- Bind email ---

class BindEmailRequest(BaseModel):
    email: EmailStr


async def bind_email_handler(
    body: BindEmailRequest,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user),
) -> dict:
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    email = body.email.strip().lower()
    # Check if email is already taken by another user
    existing = await db.execute(
        select(User).where(User.email == email, User.id != user.id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="该邮箱已被其他账户使用")
    user.email = email
    await db.commit()
    return {"success": True, "email": email}
