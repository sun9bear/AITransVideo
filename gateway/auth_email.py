"""Email verification and password-reset auth flows."""

from __future__ import annotations

import html
import logging
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth import create_session, hash_password, verify_password
from config import settings
from csrf import require_same_origin_state_change
from database import get_db
from models import EmailVerificationChallenge, User

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/auth/email",
    tags=["auth-email"],
    dependencies=[Depends(require_same_origin_state_change)],
)

MAX_VERIFY_ATTEMPTS = 3
REGISTRATION_PURPOSE = "registration"
REGISTRATION_TOKEN_PURPOSE = "registration_token"
PASSWORD_RESET_PURPOSE = "password_reset"


class EmailCodeResponse(BaseModel):
    ok: bool
    ttl_seconds: int
    email: str | None = None


class CompleteEmailRegistrationRequest(BaseModel):
    email: EmailStr
    registration_token: str = Field(..., min_length=1, max_length=256)
    password: str = Field(..., min_length=12, max_length=128)
    display_name: str = Field(default="", max_length=128)


class VerifyEmailRegistrationCodeRequest(BaseModel):
    email: EmailStr
    code: str = Field(..., min_length=1, max_length=32)


class SendEmailResetCodeRequest(BaseModel):
    email: EmailStr
    captcha_token: str = Field(..., min_length=1, max_length=4096)


class ResetEmailPasswordRequest(BaseModel):
    email: EmailStr
    code: str = Field(..., min_length=1, max_length=32)
    new_password: str = Field(..., min_length=12, max_length=128)


@dataclass(frozen=True)
class SentEmailCode:
    email: str
    code: str
    purpose: str
    ttl_seconds: int


_fake_codes: dict[tuple[str, str], SentEmailCode] = {}
_fake_lock = Lock()


@dataclass
class _EmailRateState:
    email_recent: dict[str, deque[float]]
    ip_recent: dict[str, deque[float]]
    lock: Lock


_rate_state = _EmailRateState(
    email_recent=defaultdict(deque),
    ip_recent=defaultdict(deque),
    lock=Lock(),
)


class EmailRateLimitExceeded(Exception):
    def __init__(self, scope: str, message: str):
        super().__init__(message)
        self.scope = scope
        self.message = message


def generate_email_code() -> str:
    digits = max(4, min(10, int(settings.email_code_length or 6)))
    upper = 10**digits
    return f"{secrets.randbelow(upper):0{digits}d}"


def clear_fake_state() -> None:
    with _fake_lock:
        _fake_codes.clear()


def peek_last_fake_email_code(email: str, purpose: str | None = None) -> str | None:
    normalized = _normalize_email(email)
    with _fake_lock:
        if purpose is not None:
            sent = _fake_codes.get((normalized, purpose))
            return sent.code if sent else None
        newest = None
        for (stored_email, _stored_purpose), sent in _fake_codes.items():
            if stored_email == normalized:
                newest = sent
        return newest.code if newest else None


def reset_email_rate_limits() -> None:
    with _rate_state.lock:
        _rate_state.email_recent.clear()
        _rate_state.ip_recent.clear()


def _normalize_email(raw: str) -> str:
    return str(raw or "").strip().lower()


def _normalize_code(raw: str) -> str:
    return str(raw or "").strip()


def _prune(buffer: deque[float], now: float, window_seconds: int) -> None:
    cutoff = now - window_seconds
    while buffer and buffer[0] < cutoff:
        buffer.popleft()


def _count_within(buffer: deque[float], now: float, window_seconds: int) -> int:
    cutoff = now - window_seconds
    return sum(1 for ts in buffer if ts >= cutoff)


def _check_email_code_allowed(email: str, client_ip: str | None) -> None:
    now = time.monotonic()
    with _rate_state.lock:
        email_buf = _rate_state.email_recent[email]
        _prune(email_buf, now, settings.email_send_code_hour_window_seconds)
        if (
            _count_within(email_buf, now, settings.email_send_code_window_seconds)
            >= settings.email_send_code_max_per_email_window
        ):
            raise EmailRateLimitExceeded(
                scope="email_short",
                message="验证码发送过于频繁，请稍后再试",
            )
        if (
            _count_within(email_buf, now, settings.email_send_code_hour_window_seconds)
            >= settings.email_send_code_max_per_email_hour
        ):
            raise EmailRateLimitExceeded(
                scope="email_hour",
                message="该邮箱请求次数过多，请稍后再试",
            )
        if client_ip:
            ip_buf = _rate_state.ip_recent[client_ip]
            _prune(ip_buf, now, settings.email_send_code_hour_window_seconds)
            if (
                _count_within(ip_buf, now, settings.email_send_code_hour_window_seconds)
                >= settings.email_send_code_max_per_ip_hour
            ):
                raise EmailRateLimitExceeded(
                    scope="ip_hour",
                    message="请求过于频繁，请稍后再试",
                )


def _record_email_code_sent(email: str, client_ip: str | None) -> None:
    now = time.monotonic()
    with _rate_state.lock:
        _rate_state.email_recent[email].append(now)
        if client_ip:
            _rate_state.ip_recent[client_ip].append(now)


def _client_ip(request: Request) -> str | None:
    from auth_phone import _client_ip as resolve_client_ip

    return resolve_client_ip(request)


def _email_html(*, code: str, purpose: str) -> str:
    safe_code = html.escape(code)
    if purpose == REGISTRATION_PURPOSE:
        title = "确认邮箱注册"
        body = "你正在注册 AIVideoTrans 账号，请在页面中输入下面的验证码完成注册。"
    else:
        title = "重置登录密码"
        body = "你正在重置 AIVideoTrans 账号密码，请在页面中输入下面的验证码。"
    ttl_minutes = max(1, settings.email_code_ttl_seconds // 60)
    return (
        "<div style='font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:24px'>"
        f"<h2 style='margin:0 0 16px;color:#111827'>{html.escape(title)}</h2>"
        f"<p style='color:#374151;line-height:1.7'>{html.escape(body)}</p>"
        "<div style='margin:24px 0;padding:18px 20px;background:#f3f4f6;"
        "border-radius:8px;font-size:28px;font-weight:700;letter-spacing:6px;"
        f"text-align:center;color:#111827'>{safe_code}</div>"
        f"<p style='color:#6b7280;font-size:13px'>验证码 {ttl_minutes} 分钟内有效。"
        "如果不是你本人操作，请忽略这封邮件。</p>"
        "<p style='color:#9ca3af;font-size:12px;margin-top:28px'>AIVideoTrans</p>"
        "</div>"
    )


async def send_email_code(email: str, code: str, purpose: str) -> bool:
    provider = (settings.email_auth_provider or "fake").strip().lower()
    if provider in {"fake", "mock", "stub"}:
        with _fake_lock:
            _fake_codes[(email, purpose)] = SentEmailCode(
                email=email,
                code=code,
                purpose=purpose,
                ttl_seconds=settings.email_code_ttl_seconds,
            )
        logger.info("fake email auth code issued email=%s purpose=%s", email, purpose)
        return True

    if provider == "resend":
        from notifications import send_email

        subject = "AIVideoTrans 邮箱验证码"
        return await send_email(email, subject, _email_html(code=code, purpose=purpose))

    raise NotImplementedError(f"Unsupported email auth provider: {provider}")


async def _invalidate_previous_codes(
    db: AsyncSession,
    *,
    email: str,
    purpose: str,
) -> None:
    now = datetime.now(timezone.utc)
    await db.execute(
        update(EmailVerificationChallenge)
        .where(
            EmailVerificationChallenge.email == email,
            EmailVerificationChallenge.purpose == purpose,
            EmailVerificationChallenge.consumed_at.is_(None),
            EmailVerificationChallenge.expires_at > now,
        )
        .values(consumed_at=now)
    )


async def _active_challenge(
    db: AsyncSession,
    *,
    email: str,
    purpose: str,
) -> EmailVerificationChallenge | None:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(EmailVerificationChallenge)
        .where(
            EmailVerificationChallenge.email == email,
            EmailVerificationChallenge.purpose == purpose,
            EmailVerificationChallenge.consumed_at.is_(None),
            EmailVerificationChallenge.expires_at > now,
        )
        .order_by(EmailVerificationChallenge.created_at.desc())
    )
    return result.scalars().first()


async def _reject_wrong_code(db: AsyncSession, challenge: EmailVerificationChallenge) -> None:
    now = datetime.now(timezone.utc)
    new_attempts = (challenge.attempts or 0) + 1
    challenge.attempts = new_attempts
    if new_attempts >= MAX_VERIFY_ATTEMPTS:
        challenge.consumed_at = now
    await db.commit()
    if new_attempts >= MAX_VERIFY_ATTEMPTS:
        raise HTTPException(status_code=400, detail="验证码错误次数过多，请重新获取")
    raise HTTPException(status_code=400, detail="验证码错误")


def _user_response_dict(user: User, is_new: bool) -> dict:
    return {
        "user": {
            "id": str(user.id),
            "email": user.email or "",
            "display_name": user.display_name,
            "role": getattr(user, "role", "user") or "user",
            "phone_number": getattr(user, "phone_number", None),
        },
        "is_new": is_new,
    }


async def start_email_registration(
    *,
    body,
    request: Request,
    db: AsyncSession,
) -> dict:
    """Start email registration by sending a verification code.

    The user row is created only after the email code is verified and the user
    sets a password. That keeps email registration aligned with the phone-first
    rule: verification must pass before registration is complete.
    """
    if not settings.email_registration_enabled:
        raise HTTPException(
            status_code=403,
            detail="邮箱注册已关闭，请使用手机号验证码注册",
        )

    import risk_control

    try:
        risk_control.verify_captcha(body.captcha_token)
    except risk_control.CaptchaVerificationError as exc:
        detail = str(exc) or "请完成人机验证"
        raise HTTPException(status_code=400, detail=detail)
    except NotImplementedError as exc:
        logger.error("Email registration captcha provider unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="人机验证服务暂不可用")

    email = _normalize_email(body.email)
    result = await db.execute(select(User).where(func.lower(User.email) == email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="该邮箱已注册")

    client_ip = _client_ip(request)
    try:
        _check_email_code_allowed(email, client_ip)
    except EmailRateLimitExceeded as exc:
        raise HTTPException(status_code=429, detail=exc.message)

    now = datetime.now(timezone.utc)
    code = generate_email_code()
    await _invalidate_previous_codes(db, email=email, purpose=REGISTRATION_PURPOSE)
    challenge = EmailVerificationChallenge(
        email=email,
        code_hash=hash_password(code),
        client_ip=client_ip,
        purpose=REGISTRATION_PURPOSE,
        expires_at=now + timedelta(seconds=settings.email_code_ttl_seconds),
    )
    db.add(challenge)
    await db.commit()

    try:
        sent = await send_email_code(email, code, REGISTRATION_PURPOSE)
    except NotImplementedError as exc:
        logger.error("Email auth provider unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="邮件服务暂不可用")

    if not sent:
        raise HTTPException(status_code=503, detail="邮件发送失败，请稍后再试")

    _record_email_code_sent(email, client_ip)
    return {
        "ok": True,
        "needs_email_verification": True,
        "email": email,
        "ttl_seconds": settings.email_code_ttl_seconds,
    }


@router.post("/verify-registration-code")
async def verify_email_registration_code_endpoint(
    body: VerifyEmailRegistrationCodeRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    email = _normalize_email(body.email)
    code = _normalize_code(body.code)
    if not code:
        raise HTTPException(status_code=400, detail="请输入验证码")

    challenge = await _active_challenge(
        db,
        email=email,
        purpose=REGISTRATION_PURPOSE,
    )
    if challenge is None:
        raise HTTPException(status_code=400, detail="验证码已过期，请重新获取")
    if not verify_password(code, challenge.code_hash):
        await _reject_wrong_code(db, challenge)

    now = datetime.now(timezone.utc)
    registration_token = secrets.token_urlsafe(48)
    challenge.code_hash = hash_password(registration_token)
    challenge.purpose = REGISTRATION_TOKEN_PURPOSE
    challenge.attempts = 0
    challenge.expires_at = now + timedelta(seconds=settings.email_code_ttl_seconds)
    await db.commit()

    return {
        "ok": True,
        "needs_password": True,
        "email": email,
        "registration_token": registration_token,
        "ttl_seconds": settings.email_code_ttl_seconds,
    }


@router.post("/complete-registration")
async def complete_email_registration_endpoint(
    body: CompleteEmailRegistrationRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict:
    email = _normalize_email(body.email)
    token = _normalize_code(body.registration_token)
    if not token:
        raise HTTPException(status_code=400, detail="注册令牌无效")

    challenge = await _active_challenge(
        db,
        email=email,
        purpose=REGISTRATION_TOKEN_PURPOSE,
    )
    if challenge is None:
        raise HTTPException(status_code=400, detail="注册令牌已过期，请重新验证邮箱")
    if not verify_password(token, challenge.code_hash):
        await _reject_wrong_code(db, challenge)

    now = datetime.now(timezone.utc)
    challenge.consumed_at = now

    result = await db.execute(select(User).where(func.lower(User.email) == email))
    if result.scalar_one_or_none():
        await db.commit()
        raise HTTPException(status_code=409, detail="该邮箱已注册，请直接登录")

    display_name = (body.display_name or "").strip()
    user = User(
        email=email,
        display_name=display_name or email.split("@")[0],
        password_hash=hash_password(body.password),
        email_verified_at=now,
    )
    db.add(user)
    await db.flush()

    try:
        from system_announcements_service import dispatch_announcements_for_new_user

        await dispatch_announcements_for_new_user(db, user_id=user.id)
    except Exception:
        pass

    await db.commit()
    await db.refresh(user)
    await create_session(db, user.id, response)
    return {**_user_response_dict(user, is_new=True), "needs_email_verification": False}


@router.post("/send-reset-code", response_model=EmailCodeResponse)
async def send_email_reset_code_endpoint(
    body: SendEmailResetCodeRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> EmailCodeResponse:
    import risk_control

    try:
        risk_control.verify_captcha(body.captcha_token)
    except risk_control.CaptchaVerificationError as exc:
        detail = str(exc) or "请完成人机验证"
        raise HTTPException(status_code=400, detail=detail)
    except NotImplementedError as exc:
        logger.error("Email reset captcha provider unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="人机验证服务暂不可用")

    email = _normalize_email(body.email)
    client_ip = _client_ip(request)
    try:
        _check_email_code_allowed(email, client_ip)
    except EmailRateLimitExceeded as exc:
        raise HTTPException(status_code=429, detail=exc.message)

    result = await db.execute(
        select(User).where(
            func.lower(User.email) == email,
            User.is_active.is_(True),
        )
    )
    user = result.scalar_one_or_none()
    if user is None:
        _record_email_code_sent(email, client_ip)
        return EmailCodeResponse(
            ok=True,
            ttl_seconds=settings.email_code_ttl_seconds,
            email=email,
        )

    now = datetime.now(timezone.utc)
    code = generate_email_code()
    await _invalidate_previous_codes(db, email=email, purpose=PASSWORD_RESET_PURPOSE)
    challenge = EmailVerificationChallenge(
        email=email,
        code_hash=hash_password(code),
        client_ip=client_ip,
        purpose=PASSWORD_RESET_PURPOSE,
        expires_at=now + timedelta(seconds=settings.email_code_ttl_seconds),
    )
    db.add(challenge)
    await db.commit()

    try:
        sent = await send_email_code(email, code, PASSWORD_RESET_PURPOSE)
    except NotImplementedError as exc:
        logger.error("Email auth provider unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="邮件服务暂不可用")
    if not sent:
        raise HTTPException(status_code=503, detail="邮件发送失败，请稍后再试")

    _record_email_code_sent(email, client_ip)
    return EmailCodeResponse(ok=True, ttl_seconds=settings.email_code_ttl_seconds, email=email)


@router.post("/reset-password")
async def reset_email_password_endpoint(
    body: ResetEmailPasswordRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict:
    email = _normalize_email(body.email)
    code = _normalize_code(body.code)
    if not code:
        raise HTTPException(status_code=400, detail="请输入验证码")

    challenge = await _active_challenge(
        db,
        email=email,
        purpose=PASSWORD_RESET_PURPOSE,
    )
    if challenge is None:
        raise HTTPException(status_code=400, detail="验证码已过期，请重新获取")
    if not verify_password(code, challenge.code_hash):
        await _reject_wrong_code(db, challenge)

    now = datetime.now(timezone.utc)
    challenge.consumed_at = now

    result = await db.execute(
        select(User).where(
            func.lower(User.email) == email,
            User.is_active.is_(True),
        )
    )
    user = result.scalar_one_or_none()
    if user is None:
        await db.commit()
        raise HTTPException(status_code=404, detail="该邮箱尚未注册")

    user.password_hash = hash_password(body.new_password)
    user.email_verified_at = user.email_verified_at or now
    await db.commit()
    await create_session(db, user.id, response)
    return {"ok": True, "message": "密码重置成功"}
