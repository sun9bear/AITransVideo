"""Phone-based auth router (A1 unified login rewrite).

Endpoints (all public, no `Depends(require_auth)`):

- `POST /auth/phone/send-code`              — issue a verification code
- `POST /auth/phone/verify-code`            — verify code; for NEW phone → returns
  registration_token (no session/trial yet); for EXISTING phone → logs in directly
- `POST /auth/phone/complete-registration`  — consume registration_token + set password
  → create user, set password, grant trial, create session
- `POST /auth/phone/reset-password`         — verify code for existing user, then
  set new password (phone-only path, no email reset)

A1 key rule: "验证码通过 ≠ 注册成功". New users must set a password before they are
considered registered. Trial is granted only after password setup.

Trial bookkeeping (frozen by H1 decision 2026-04-06):
- On registration completion (not on verify-code alone), stamps:
  - `users.trial_granted_at` = now
  - `users.trial_ends_at`    = now + TRIAL_CONFIG["days"]
- Same phone / same IP lifetime guards still apply.
- `user.plan_code` is NEVER mutated by trial bookkeeping.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth import create_session, hash_password
from config import settings
from csrf import require_same_origin_state_change
from database import get_db
from models import PhoneVerificationChallenge, User
import risk_control
import sms_provider

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/auth/phone",
    tags=["auth-phone"],
    dependencies=[Depends(require_same_origin_state_change)],
)


# ---------------------------------------------------------------------------
# P1-10a-2 / S-HIGH-4 (audit 2026-05-07): wrong-code attempt limit on phone
# challenges. Compare code FIRST; only mark ``consumed_at`` when (a) code
# matched, or (b) wrong attempts have reached this limit. Pre-019 logic
# burned the challenge on the first wrong guess, which made a per-phone
# DoS attack trivially cheap.
#
# Why 3: matches industry-standard SMS OTP UX (3 wrong → resend) and is
# tight enough that any meaningful brute-force still requires a fresh
# challenge (which the per-phone send-code rate limit already gates at
# 1/min, 5/hour).
# ---------------------------------------------------------------------------

MAX_VERIFY_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# P1-10b / S-HIGH-2 (audit 2026-05-07): the captcha "pre-verify + pass_token"
# pre-flight flow that used to live here was DEAD CODE — the frontend never
# called ``/auth/captcha/pre-verify``, ``send_code_endpoint`` always called
# ``risk_control.verify_captcha`` directly on the original captcha_token,
# and the in-memory ``_captcha_passes`` dict was never consumed. Side
# effects of leaving it in place:
#
#   * Aliyun / Turnstile single-use semantics meant the captcha chain
#     was broken in production (verify_captcha ran on a token the
#     provider had already invalidated, so re-submission failed) —
#     unless ``AVT_CAPTCHA_PROVIDER=fake``.
#   * ``_captcha_passes`` only got garbage-collected at issuance time.
#     A long-lived gateway with frontend never calling /pre-verify
#     accumulated nothing — but a hostile caller could spam /pre-verify
#     to fill memory unbounded.
#
# Fix per audit option (b): delete the entire flow. ``send_code_endpoint``
# still calls ``risk_control.verify_captcha(captcha_token)`` directly,
# matching what the frontend already sends. The per-phone send-code
# rate limit (1/min) is the upstream gate against captcha resubmission.
#
# A regression guard in tests/test_alembic_019_phone_attempts.py asserts
# the deleted symbols don't reappear by accident.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class SendCodeRequest(BaseModel):
    phone_number: str = Field(..., min_length=1, max_length=32)
    captcha_token: str = Field(..., min_length=1, max_length=4096)


class SendCodeResponse(BaseModel):
    ok: bool
    ttl_seconds: int


class VerifyCodeRequest(BaseModel):
    phone_number: str = Field(..., min_length=1, max_length=32)
    code: str = Field(..., min_length=1, max_length=16)


class CompleteRegistrationRequest(BaseModel):
    registration_token: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=12, max_length=128)


class ResetPasswordRequest(BaseModel):
    phone_number: str = Field(..., min_length=1, max_length=32)
    code: str = Field(..., min_length=1, max_length=16)
    new_password: str = Field(..., min_length=12, max_length=128)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# P1-10a-1 (audit 2026-05-07, S-HIGH-5): X-Forwarded-For trusted-proxy boundary.
# Default trusted proxies: loopback only. In production, Caddy and
# Cloudflare-Tunnel both run on localhost via the docker network, so
# the gateway always sees them as 127.0.0.1 from inside the container.
# Override via env if a different topology is in play.
_TRUSTED_PROXIES_ENV = "AVT_TRUSTED_PROXIES"
_DEFAULT_TRUSTED_PROXIES = frozenset({"127.0.0.1", "::1", "localhost"})


def _trusted_proxies() -> frozenset[str]:
    raw = os.environ.get(_TRUSTED_PROXIES_ENV, "").strip()
    if not raw:
        return _DEFAULT_TRUSTED_PROXIES
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def _client_ip(request: Request) -> str | None:
    """Resolve the requester IP with a trusted-proxy boundary.

    P1-10a-1 (audit 2026-05-07, S-HIGH-5): previously the gateway
    blindly trusted X-Forwarded-For[0] / X-Real-IP. An attacker hitting
    the gateway directly (or any path that bypasses Caddy) could spoof
    those headers to bypass per-IP rate limit + IP-based trial
    eligibility (each trial = 300 credits).

    New rule: X-Forwarded-For / X-Real-IP are only trusted when the
    immediate socket peer is in the trusted-proxy allowlist. Otherwise
    we fall back to request.client.host (the real socket IP).
    """
    socket_peer = request.client.host if request.client is not None else None
    trusted = _trusted_proxies()
    if socket_peer and socket_peer in trusted:
        # Trusted reverse proxy — use forwarded headers, in this priority:
        # 1. ``CF-Connecting-IP``  — Cloudflare edge injects this with the
        #    real end-user IP on every request and cloudflared / Caddy
        #    pass it through unchanged. This is the only header that
        #    reliably carries the Cloudflare-edge IP through the
        #    ``CF → cloudflared → Caddy → gateway`` chain on this stack;
        #    Caddy's default ``reverse_proxy`` REPLACES X-Forwarded-For
        #    with the immediate peer (cloudflared at 127.0.0.1), wiping
        #    the original chain.
        # 2. ``X-Forwarded-For``   — fallback for non-Cloudflare deploys.
        # 3. ``X-Real-IP``         — fallback for Nginx-style proxies.
        #
        # 2026-05-08 fix: pre-fix the gateway only looked at #2, which
        # arrived as 127.0.0.1 → all phone-registration trial-IP markers
        # collapsed to a single shared ``__ip_trial__`` row keyed at
        # 127.0.0.1, so every new user after the first got blocked from
        # the 7-day trial.
        cf = request.headers.get("cf-connecting-ip")
        if cf:
            return cf.strip() or socket_peer
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip() or socket_peer
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip() or socket_peer
    # Untrusted peer — never honor forwarded headers
    return socket_peer


async def _invalidate_previous_codes(db: AsyncSession, phone_number: str) -> None:
    """Retire any unexpired LOGIN challenges for ``phone_number``.

    Called from ``/send-code`` so the freshly issued OTP is the only
    valid login challenge for that phone. Registration tokens
    (``purpose='registration'``) are intentionally NOT touched here:

    P1-10a-2 follow-up² (Codex review fb6b693): pre-fix this UPDATE
    invalidated EVERY unconsumed challenge for the phone, including
    registration tokens. An unauthenticated attacker who survived the
    captcha + per-phone send-code rate limit could call ``/send-code``
    against a victim's phone and burn the victim's pending
    ``purpose='registration'`` token, blocking the legitimate
    ``/complete-registration`` POST.

    The intended semantic is "only the newest LOGIN OTP for this phone
    is valid" — registration tokens are scoped to their own consume
    path and don't conflict with login OTPs. Adding the purpose filter
    aligns this write path with the read paths in ``verify_code_endpoint``
    / ``reset_password_endpoint`` (both already scoped to
    ``purpose == 'login'``).
    """
    now = datetime.now(timezone.utc)
    await db.execute(
        update(PhoneVerificationChallenge)
        .where(
            PhoneVerificationChallenge.phone_number == phone_number,
            PhoneVerificationChallenge.purpose == "login",
            PhoneVerificationChallenge.consumed_at.is_(None),
            PhoneVerificationChallenge.expires_at > now,
        )
        .values(consumed_at=now)
    )


def _user_response_dict(user: User, is_new: bool) -> dict:
    return {
        "user": {
            "id": str(user.id),
            "email": user.email or "",
            "display_name": user.display_name,
            "role": getattr(user, "role", "user") or "user",
            "phone_number": user.phone_number,
        },
        "is_new": is_new,
    }


def _default_display_name_from_phone(phone_number: str) -> str:
    if len(phone_number) == 11:
        return f"{phone_number[:3]}****{phone_number[-4:]}"
    return f"手机用户 {phone_number[-4:]}"


# ---------------------------------------------------------------------------
# POST /auth/phone/send-code
# ---------------------------------------------------------------------------


@router.post("/send-code", response_model=SendCodeResponse)
async def send_code_endpoint(
    body: SendCodeRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SendCodeResponse:
    try:
        phone = risk_control.normalize_cn_mobile(body.phone_number)
    except risk_control.PhoneNormalizationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        risk_control.verify_captcha(body.captcha_token)
    except risk_control.CaptchaVerificationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    client_ip = _client_ip(request)

    try:
        risk_control.check_send_code_allowed(phone, client_ip)
    except risk_control.RateLimitExceeded as exc:
        raise HTTPException(status_code=429, detail=exc.message)

    code = sms_provider.generate_code()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=settings.phone_code_ttl_seconds)

    await _invalidate_previous_codes(db, phone)

    challenge = PhoneVerificationChallenge(
        phone_number=phone,
        code=code,
        client_ip=client_ip,
        purpose="login",
        expires_at=expires_at,
    )
    db.add(challenge)
    await db.commit()

    try:
        sms_provider.send_code(phone, code)
    except NotImplementedError as exc:
        logger.error("SMS provider unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="短信服务暂不可用")

    risk_control.record_send_code(phone, client_ip)

    return SendCodeResponse(ok=True, ttl_seconds=settings.phone_code_ttl_seconds)


# ---------------------------------------------------------------------------
# POST /auth/phone/verify-code
# ---------------------------------------------------------------------------


@router.post("/verify-code")
async def verify_code_endpoint(
    body: VerifyCodeRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        phone = risk_control.normalize_cn_mobile(body.phone_number)
    except risk_control.PhoneNormalizationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    code = body.code.strip()
    if not code:
        raise HTTPException(status_code=400, detail="请输入验证码")

    now = datetime.now(timezone.utc)

    # P1-10a-2 follow-up (Codex review 405d2a0): filter by
    # ``purpose == "login"``. After a new-phone verify-code succeeds,
    # we issue a separate ``purpose="registration"`` row holding the
    # registration token; that row is unconsumed and (briefly) the
    # latest for the phone. Without the purpose filter, a subsequent
    # ``/verify-code`` or ``/reset-password`` request would pick up
    # THE REGISTRATION ROW as the active challenge, increment its
    # attempts on every wrong code, and burn the user's pending
    # registration token after MAX_VERIFY_ATTEMPTS misses — a fresh
    # DoS surface that the previous compare-first fix didn't cover.
    # Registration tokens are ONLY consumable via
    # ``/complete-registration`` (which already filters
    # ``purpose == "registration"``); OTP endpoints must scope to
    # login challenges only.
    result = await db.execute(
        select(PhoneVerificationChallenge)
        .where(
            PhoneVerificationChallenge.phone_number == phone,
            PhoneVerificationChallenge.purpose == "login",
            PhoneVerificationChallenge.consumed_at.is_(None),
            PhoneVerificationChallenge.expires_at > now,
        )
        .order_by(PhoneVerificationChallenge.created_at.desc())
    )
    challenge = result.scalars().first()
    if challenge is None:
        raise HTTPException(status_code=400, detail="验证码已过期,请重新获取")

    # P1-10a-2 / S-HIGH-4 (audit 2026-05-07): compare code FIRST. Pre-019
    # the endpoint marked consumed_at on the first wrong guess, which let
    # an attacker who knew a victim's phone burn the legitimate OTP at
    # zero cost. Now we only consume on (a) correct code, or (b) wrong-
    # attempt count reaching MAX_VERIFY_ATTEMPTS.
    if challenge.code != code:
        new_attempts = (challenge.attempts or 0) + 1
        challenge.attempts = new_attempts
        if new_attempts >= MAX_VERIFY_ATTEMPTS:
            # Limit reached — retire the challenge so an online brute-
            # force can't keep trying. The user must request a fresh
            # OTP via /auth/phone/send-code.
            challenge.consumed_at = now
        await db.commit()
        if new_attempts >= MAX_VERIFY_ATTEMPTS:
            raise HTTPException(
                status_code=400,
                detail="验证码错误次数过多,请重新获取",
            )
        raise HTTPException(status_code=400, detail="验证码错误")

    # Code matched — consume the challenge. Subsequent commits for the
    # login / registration flow happen below; consumed_at gets persisted
    # alongside them atomically.
    challenge.consumed_at = now

    # Check if this phone already has a user.
    user_result = await db.execute(
        select(User).where(User.phone_number == phone)
    )
    user = user_result.scalar_one_or_none()

    if user is not None:
        # EXISTING user → direct login (same as before).
        if not user.is_active:
            raise HTTPException(status_code=403, detail="账户已禁用")
        user.phone_verified_at = now

        # Trial bookkeeping for existing users who haven't received trial yet.
        if user.trial_granted_at is None:
            client_ip = _client_ip(request)
            ip_eligible = await risk_control.check_ip_trial_eligible_db(db, client_ip)
            if not risk_control.is_virtual_segment(phone) and ip_eligible:
                from plan_catalog import TRIAL_CONFIG
                user.trial_granted_at = now
                trial_days = TRIAL_CONFIG.get("days", 7)
                user.trial_ends_at = now + timedelta(days=trial_days)
                await risk_control.record_ip_trial_grant_db(db, client_ip)
                # V3-1 shadow: create trial credits bucket (best-effort)
                try:
                    from credits_service import ensure_trial_bucket
                    await ensure_trial_bucket(db, user.id, user.trial_ends_at)
                except Exception:
                    pass  # shadow — never block auth

        await db.flush()
        await db.commit()
        await db.refresh(user)

        await create_session(db, user.id, response)
        return {**_user_response_dict(user, is_new=False), "needs_password": False}

    # NEW phone → issue a registration token instead of creating user/session.
    # The user must complete password setup before registration is finalized.
    registration_token = uuid.uuid4().hex
    reg_challenge = PhoneVerificationChallenge(
        phone_number=phone,
        code=registration_token,
        client_ip=_client_ip(request),
        purpose="registration",
        expires_at=now + timedelta(minutes=15),  # 15 min to complete registration
    )
    db.add(reg_challenge)
    await db.commit()

    return {
        "user": None,
        "is_new": True,
        "needs_password": True,
        "registration_token": registration_token,
    }


# ---------------------------------------------------------------------------
# POST /auth/phone/complete-registration
# ---------------------------------------------------------------------------


@router.post("/complete-registration")
async def complete_registration_endpoint(
    body: CompleteRegistrationRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Consume a registration token + set password → create user, grant trial,
    create session. This is the "注册成功" moment.

    A1 rule: only after this endpoint succeeds is the user considered registered.
    """
    now = datetime.now(timezone.utc)

    # Find the registration challenge.
    result = await db.execute(
        select(PhoneVerificationChallenge)
        .where(
            PhoneVerificationChallenge.code == body.registration_token,
            PhoneVerificationChallenge.purpose == "registration",
            PhoneVerificationChallenge.consumed_at.is_(None),
            PhoneVerificationChallenge.expires_at > now,
        )
    )
    reg = result.scalar_one_or_none()
    if reg is None:
        raise HTTPException(
            status_code=400,
            detail="注册令牌无效或已过期,请重新开始注册",
        )

    phone = reg.phone_number

    # Consume the token immediately.
    reg.consumed_at = now
    await db.commit()

    # Guard: if someone raced and created the user between verify and now.
    existing = await db.execute(select(User).where(User.phone_number == phone))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="该手机号已注册,请直接登录")

    if len(body.password) < 12:
        raise HTTPException(status_code=400, detail="密码至少 12 位")

    # Create the user with password.
    user = User(
        phone_number=phone,
        email=None,
        password_hash=hash_password(body.password),
        display_name=_default_display_name_from_phone(phone),
        phone_verified_at=now,
    )
    db.add(user)

    # Trial bookkeeping — only at registration completion, not at verify-code.
    client_ip = reg.client_ip
    ip_eligible = await risk_control.check_ip_trial_eligible_db(db, client_ip)
    trial_granted = False
    if not risk_control.is_virtual_segment(phone) and ip_eligible:
        from plan_catalog import TRIAL_CONFIG
        user.trial_granted_at = now
        trial_days = TRIAL_CONFIG.get("days", 7)
        user.trial_ends_at = now + timedelta(days=trial_days)
        await risk_control.record_ip_trial_grant_db(db, client_ip)
        trial_granted = True

    await db.flush()

    # V3-1 shadow: create free + trial credits buckets (best-effort)
    try:
        from credits_service import ensure_free_bucket, ensure_trial_bucket
        await ensure_free_bucket(db, user.id)
        if trial_granted:
            await ensure_trial_bucket(db, user.id, user.trial_ends_at)
    except Exception:
        pass  # shadow — never block registration

    # Plan 2026-05-08 §16.7 follow-up §"新注册用户" — fan out every
    # active live announcement to this user. Best-effort; failure
    # logs but never blocks registration.
    try:
        from system_announcements_service import (
            dispatch_announcements_for_new_user,
        )
        await dispatch_announcements_for_new_user(db, user_id=user.id)
    except Exception:
        pass

    await db.commit()
    await db.refresh(user)

    # Create session — this is the "注册成功" moment.
    await create_session(db, user.id, response)

    return {**_user_response_dict(user, is_new=True), "needs_password": False}


# ---------------------------------------------------------------------------
# POST /auth/phone/reset-password
# ---------------------------------------------------------------------------


@router.post("/reset-password")
async def reset_password_endpoint(
    body: ResetPasswordRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Phone-based password reset. Only for users that already have a phone number.
    Old email-only accounts cannot self-reset (handled manually by admin).
    """
    try:
        phone = risk_control.normalize_cn_mobile(body.phone_number)
    except risk_control.PhoneNormalizationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    code = body.code.strip()
    if not code:
        raise HTTPException(status_code=400, detail="请输入验证码")

    now = datetime.now(timezone.utc)

    # Find active LOGIN challenge for this phone. P1-10a-2 follow-up
    # (Codex review 405d2a0): scope to ``purpose == "login"`` so the
    # endpoint can't accidentally pick up an unconsumed registration
    # token (purpose='registration') and burn it via the wrong-code
    # attempts counter. Registration tokens are only consumable via
    # /complete-registration.
    result = await db.execute(
        select(PhoneVerificationChallenge)
        .where(
            PhoneVerificationChallenge.phone_number == phone,
            PhoneVerificationChallenge.purpose == "login",
            PhoneVerificationChallenge.consumed_at.is_(None),
            PhoneVerificationChallenge.expires_at > now,
        )
        .order_by(PhoneVerificationChallenge.created_at.desc())
    )
    challenge = result.scalars().first()
    if challenge is None:
        raise HTTPException(status_code=400, detail="验证码已过期,请重新获取")

    # P1-10a-2 / S-HIGH-4 (audit 2026-05-07): compare code FIRST, just
    # like verify_code_endpoint above. Pre-019 the reset-password path
    # had the same DoS — first wrong guess consumed the OTP, locking
    # legitimate users out of password reset.
    if challenge.code != code:
        new_attempts = (challenge.attempts or 0) + 1
        challenge.attempts = new_attempts
        if new_attempts >= MAX_VERIFY_ATTEMPTS:
            challenge.consumed_at = now
        await db.commit()
        if new_attempts >= MAX_VERIFY_ATTEMPTS:
            raise HTTPException(
                status_code=400,
                detail="验证码错误次数过多,请重新获取",
            )
        raise HTTPException(status_code=400, detail="验证码错误")

    # Code matched — consume the challenge. The password write below
    # commits in the same transaction.
    challenge.consumed_at = now

    # Find the user by phone.
    user_result = await db.execute(
        select(User).where(User.phone_number == phone)
    )
    user = user_result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="该手机号尚未注册")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="账户已禁用")

    if len(body.new_password) < 12:
        raise HTTPException(status_code=400, detail="密码至少 12 位")

    user.password_hash = hash_password(body.new_password)
    await db.commit()

    # Optionally log the user in after reset.
    await create_session(db, user.id, response)

    return {"ok": True, "message": "密码重置成功"}
