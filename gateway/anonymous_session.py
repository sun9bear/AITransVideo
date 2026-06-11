"""APF P0 — anonymous session cookie dependency (AD-4, T7).

Two FastAPI dependencies:

* ``get_or_create_anonymous_session`` — used by ``POST /upload``.
  Reads cookie ``avt_anon``; if valid (HMAC hash found in DB and not
  expired) returns the existing session context.  Otherwise generates a
  new raw token, hashes it, stores the hash in ``anonymous_sessions``,
  and sets the ``avt_anon`` HttpOnly/Secure/SameSite=Lax cookie.

* ``require_anonymous_session`` — used by ``GET /{id}/status`` and
  ``GET /{id}/stream``.  Same lookup path, but returns 401 instead of
  issuing a new session on miss/invalid.  Callers must NOT accept an
  auto-issued cookie on these endpoints (user must already have a session
  from a prior upload).

Both dependencies check the env flag (``settings.enable_anonymous_preview``)
and the admin toggle (``admin_settings.anonymous_free_preview_enabled``)
**before** any DB interaction:
  - env flag False → 404  (feature not deployed)
  - admin toggle False or unreadable → 403  (fail-closed, F9)

Design constraints
------------------
* No ``services.jobs`` import (pydub guard).
* HMAC hash uses ``anonymous_preview_quota.hash_scope_key`` (same secret,
  same function as rate-limit scope key hashing) so there is a single HMAC
  path in this module.
* Cookie name ``avt_anon`` (distinct from the login cookie ``avt_session``).
* TTL = 24h (86400s Max-Age).
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Cookie, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from anonymous_preview_quota import hash_scope_key
from config import settings
from database import get_db
from models import AnonymousSession

logger = logging.getLogger(__name__)

_COOKIE_NAME = "avt_anon"
_COOKIE_MAX_AGE = 86400  # 24h
_SESSION_TTL = timedelta(seconds=_COOKIE_MAX_AGE)


# ---------------------------------------------------------------------------
# Output type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AnonymousSessionContext:
    """Resolved anonymous session for use in route handlers."""

    session_id_hash: str
    """HMAC hash of the raw token — safe to store, never the raw token."""

    raw_token: Optional[str]
    """Raw token; present only for newly-created sessions (used to set cookie).
    ``None`` for existing sessions (raw token is not stored and we don't
    re-derive it here)."""

    is_new: bool
    """True if this session was just created (cookie must be set by caller)."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_admin_flag() -> bool:
    """Return ``anonymous_free_preview_enabled`` from admin settings.

    Fail-closed (F9): any exception reading admin settings → return False.
    """
    try:
        from admin_settings import load_settings as _load
        admin = _load()
        return bool(admin.anonymous_free_preview_enabled)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "anonymous_session: failed to read admin_settings, defaulting to closed: %s",
            exc,
        )
        return False


def _hash_token(raw: str) -> str:
    """HMAC-SHA256 hash of *raw* using the anonymous preview secret."""
    return hash_scope_key(raw, secret=settings.anonymous_preview_hash_secret)


async def _lookup_session(db: AsyncSession, session_id_hash: str) -> Optional[AnonymousSession]:
    """Return a non-expired ``AnonymousSession`` row for *session_id_hash*, or None."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(AnonymousSession).where(
            AnonymousSession.session_id_hash == session_id_hash,
            AnonymousSession.expires_at > now,
        )
    )
    return result.scalar_one_or_none()


async def _create_session(
    db: AsyncSession,
    response: Response,
    *,
    set_cookie: bool,
) -> AnonymousSessionContext:
    """Create a new anonymous session, persist it, optionally set the cookie."""
    raw_token = secrets.token_urlsafe(32)
    session_id_hash = _hash_token(raw_token)
    expires_at = datetime.now(timezone.utc) + _SESSION_TTL

    row = AnonymousSession(
        session_id_hash=session_id_hash,
        expires_at=expires_at,
        claim_user_id=None,
    )
    db.add(row)
    await db.commit()

    if set_cookie:
        response.set_cookie(
            key=_COOKIE_NAME,
            value=raw_token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=_COOKIE_MAX_AGE,
            path="/",
        )
        logger.info(
            "anonymous_session: new session created hash=%.8s expires_at=%s",
            session_id_hash,
            expires_at.isoformat(),
        )

    return AnonymousSessionContext(
        session_id_hash=session_id_hash,
        raw_token=raw_token,
        is_new=True,
    )


# ---------------------------------------------------------------------------
# Public dependencies
# ---------------------------------------------------------------------------

async def get_or_create_anonymous_session(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> AnonymousSessionContext | Response:
    """FastAPI dependency for ``POST /upload``.

    Returns an ``AnonymousSessionContext`` on success.
    Returns a ``JSONResponse`` (404 or 403) when the feature is disabled —
    callers must check ``isinstance(ctx, Response)`` and return it early.

    Flow:
      1. env flag off → 404
      2. admin off or unreadable → 403
      3. cookie present and valid → return existing ctx
      4. cookie absent or invalid → create new session, set cookie, return ctx
    """
    # --- gate 1: env flag ---
    if not settings.enable_anonymous_preview:
        return JSONResponse(
            status_code=404,
            content={"error": "feature_not_available"},
        )

    # --- gate 2: admin hot-switch (fail-closed) ---
    if not _get_admin_flag():
        return JSONResponse(
            status_code=403,
            content={"error": "anonymous_preview_disabled"},
        )

    # --- try to resolve existing session ---
    # 对抗审核 P0 修复：本函数被端点手动 await 调用（非 Depends 注入），
    # Cookie() 参数在手动调用下不会被 FastAPI 填充（拿到的是 Param 对象）。
    # 必须直接从 request.cookies 读，两种调用方式行为才一致。
    avt_anon = request.cookies.get(_COOKIE_NAME)
    if avt_anon:
        try:
            session_id_hash = _hash_token(avt_anon)
            row = await _lookup_session(db, session_id_hash)
            if row is not None:
                return AnonymousSessionContext(
                    session_id_hash=session_id_hash,
                    raw_token=None,
                    is_new=False,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "anonymous_session: hash/lookup failed, will create new: %s", exc
            )

    # --- create new session ---
    return await _create_session(db, response, set_cookie=True)


async def require_anonymous_session(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> AnonymousSessionContext | Response:
    """FastAPI dependency for ``GET /{id}/status`` and ``GET /{id}/stream``.

    Returns an ``AnonymousSessionContext`` on success.
    Returns a ``JSONResponse`` (404 / 403 / 401) on failure — callers must
    check ``isinstance(ctx, Response)`` and return it early.

    Does NOT create a new session on miss — 401 instead.
    """
    # --- gate 1: env flag ---
    if not settings.enable_anonymous_preview:
        return JSONResponse(
            status_code=404,
            content={"error": "feature_not_available"},
        )

    # --- gate 2: admin hot-switch (fail-closed) ---
    if not _get_admin_flag():
        return JSONResponse(
            status_code=403,
            content={"error": "anonymous_preview_disabled"},
        )

    # --- require existing valid session ---
    # 对抗审核 P0 修复：同 get_or_create —— 手动调用下 Cookie() 参数不被
    # 注入，必须直接读 request.cookies。
    avt_anon = request.cookies.get(_COOKIE_NAME)
    if not avt_anon:
        return JSONResponse(
            status_code=401,
            content={"error": "anonymous_session_required"},
        )

    try:
        session_id_hash = _hash_token(avt_anon)
        row = await _lookup_session(db, session_id_hash)
    except Exception as exc:  # noqa: BLE001
        logger.warning("anonymous_session: lookup error: %s", exc)
        return JSONResponse(
            status_code=401,
            content={"error": "anonymous_session_invalid"},
        )

    if row is None:
        return JSONResponse(
            status_code=401,
            content={"error": "anonymous_session_invalid_or_expired"},
        )

    return AnonymousSessionContext(
        session_id_hash=session_id_hash,
        raw_token=None,
        is_new=False,
    )
