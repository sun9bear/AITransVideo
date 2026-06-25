"""Baidu Pan OAuth web flow + token refresh background task.

Plan 2026-05-14 Phase 6 §T6.1-T6.5.

## Endpoints (admin-only)

  POST /api/admin/pan/connect
    → Generate state token, INSERT pan_oauth_states (TTL 10min),
      302 redirect to Baidu OAuth authorize URL with state in query.

  GET  /api/admin/pan/callback
    → Baidu redirects here after user grants permission. Verify state
      (one-shot DELETE), exchange code via BaiduPanClient.exchange_code,
      encrypt tokens, UPSERT pan_credentials, 302 to /admin/pan/dashboard.

## State token lifecycle

  - secrets.token_urlsafe(32) — 32 bytes random → ~43 char base64
  - INSERT pan_oauth_states with expires_at = now + 10min
  - DELETE on successful callback OR on expiry detection (no separate sweep
    needed; consume_state_token deletes expired rows it encounters)

## Token refresh (Phase 8 scheduler ticks this)

  pan_token_refresh_tick(): SELECT credentials where status='active' AND
  expires_at < now()+24h, decrypt → BaiduPanClient.refresh → re-encrypt
  → UPDATE. Failure → status='revoked' + notifications_service.dispatch_event.

  Baidu rotates refresh_token on EVERY successful refresh — the caller
  MUST persist the new refresh_token from the response (we do).
"""
from __future__ import annotations

import logging
import secrets
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from config import settings
from csrf import require_same_origin_state_change
from database import get_db
from models import PanCredentials, PanOauthState, User
from notifications_service import dispatch_event

from pan.baidu_pan_client import BaiduPanClient
from pan.token_crypto import decrypt_token, encrypt_token
# Phase 9 §T9.4 (CodeX 2026-05-19 P1b): pan JSONL emitter shared with
# backup_executor / restore_executor / residue_cleanup.
from pan._events import emit_pan_event_safe as _emit_pan_event_safe
# Plan 2026-05-26 postmortem P0a (Codex feedback): same feature gate as
# admin_api.py — flag must reject OAuth connect / callback when feature
# is off, otherwise an admin could re-authorize Baidu Pan in a
# "disabled" state and the system would still hold a valid token.
from pan._feature_gate import require_pan_enabled


logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/pan",
    tags=["admin-pan-auth"],
    dependencies=[
        Depends(require_pan_enabled),
        Depends(require_same_origin_state_change),
    ],
)

STATE_TTL_SECONDS = 10 * 60  # plan §6.1
BAIDU_AUTHORIZE_URL = "https://openapi.baidu.com/oauth/2.0/authorize"
BAIDU_SCOPE = "basic netdisk"
DASHBOARD_REDIRECT_URL = "/admin/pan/dashboard"


# --- admin gate (local copy: pan/ is an independent auth context) ---
# TU-05 (DRY-01) explicit exception: NOT migrated to gateway/admin_auth.py.
# Semantic equivalence with the shared helper is not yet fully verified;
# migrate in a separate PR once confirmed. See admin_auth.py BACKGROUND note.


def _is_admin(user: User) -> bool:
    return (getattr(user, "role", None) or "user") == "admin"


def _require_admin(user: User | None) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# --- state token helpers ---


def generate_state_token() -> str:
    """32-byte URL-safe random → ~43 char base64. Fits PanOauthState.token
    String(64)."""
    return secrets.token_urlsafe(32)


async def insert_state_token(
    db: AsyncSession,
    *,
    user_id: _uuid.UUID,
    ttl_seconds: int = STATE_TTL_SECONDS,
) -> str:
    """Generate state + INSERT pan_oauth_states with TTL. Returns token."""
    token = generate_state_token()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    await db.execute(
        PanOauthState.__table__.insert().values(
            token=token, user_id=user_id, expires_at=expires_at,
        )
    )
    await db.commit()
    return token


async def consume_state_token(
    db: AsyncSession,
    token: str,
) -> _uuid.UUID | None:
    """Atomic one-shot validate + DELETE via `DELETE ... RETURNING`.

    CodeX P2: previous SELECT-then-DELETE had a window where two
    concurrent callbacks could both pass validation against the same
    state token. `DELETE ... RETURNING` is atomic — exactly one caller
    gets the row, the other sees no match. Works on PG natively + on
    SQLite 3.35+ (we use SQLite 3.40+ in dev / aiosqlite 0.19+).

    Returns user_id on success. Returns None if:
      - token doesn't exist (never issued, or already consumed)
      - token has expired (deletion happens here too — one-shot
        semantics drop expired rows incidentally)
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        delete(PanOauthState)
        .where(PanOauthState.token == token)
        .returning(PanOauthState.user_id, PanOauthState.expires_at)
    )
    row = result.one_or_none()
    await db.commit()
    if row is None:
        return None  # token never existed OR a concurrent caller won the race

    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        # SQLite tests store naive UTC; PG stores aware.
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        return None  # token existed but was expired; the DELETE cleaned up
    return row.user_id


# --- endpoints ---


@router.post("/connect")
async def pan_connect(
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Admin clicks "Connect Pan". Generate state → 302 to Baidu OAuth."""
    admin = _require_admin(user)
    if not settings.baidu_pan_appkey or not settings.baidu_pan_redirect_uri:
        raise HTTPException(
            status_code=503,
            detail=(
                "Pan OAuth 未配置:AVT_BAIDU_PAN_APPKEY / "
                "AVT_BAIDU_PAN_REDIRECT_URI 缺失"
            ),
        )
    token = await insert_state_token(db, user_id=admin.id)
    qs = urlencode({
        "response_type": "code",
        "client_id": settings.baidu_pan_appkey,
        "redirect_uri": settings.baidu_pan_redirect_uri,
        "scope": BAIDU_SCOPE,
        "state": token,
    })
    return RedirectResponse(
        url=f"{BAIDU_AUTHORIZE_URL}?{qs}", status_code=302,
    )


@router.get("/callback")
async def pan_callback(
    code: str = Query(..., description="OAuth authorization code"),
    state: str = Query(..., description="State token we issued at /connect"),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Baidu redirects here after user authorizes. Verify state, exchange
    code, UPSERT pan_credentials, 302 to UI dashboard."""
    return await _pan_callback_impl(
        code=code, state=state, db=db,
        client_factory=_default_client_factory,
    )


async def _pan_callback_impl(
    *,
    code: str,
    state: str,
    db: AsyncSession,
    client_factory: Callable[[], Any],
) -> RedirectResponse:
    """Real callback logic with client_factory injection seam (tests)."""
    user_id = await consume_state_token(db, state)
    if user_id is None:
        raise HTTPException(status_code=400, detail="state 无效或已过期")
    if not settings.baidu_pan_appkey or not settings.baidu_pan_appsecret:
        raise HTTPException(status_code=503, detail="Pan OAuth 未配置")

    client = client_factory()
    # exchange_code is sync (requests-based); for now we call inline. If
    # backup_executor's asyncio.to_thread pattern needs to apply here it's
    # a one-shot — OAuth bandwidth dominates over thread overhead anyway.
    tokens = client.exchange_code(code, settings.baidu_pan_redirect_uri)

    access_enc = encrypt_token(tokens['access_token'])
    refresh_enc = encrypt_token(tokens['refresh_token'])
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=int(tokens['expires_in']),
    )
    scope = tokens.get('scope', '')

    # UPSERT (user_id, provider) — re-connecting overwrites stale tokens
    # and clears any 'revoked' status.
    existing_id = (await db.execute(
        select(PanCredentials.id).where(
            PanCredentials.user_id == user_id,
            PanCredentials.provider == 'baidu_pan',
        )
    )).scalar_one_or_none()

    if existing_id is None:
        await db.execute(PanCredentials.__table__.insert().values(
            id=_uuid.uuid4(),
            user_id=user_id,
            provider='baidu_pan',
            access_token_encrypted=access_enc,
            refresh_token_encrypted=refresh_enc,
            access_token_expires_at=expires_at,
            scope=scope,
            status='active',
            connected_at=datetime.now(timezone.utc),
        ))
    else:
        await db.execute(
            update(PanCredentials)
            .where(PanCredentials.id == existing_id)
            .values(
                access_token_encrypted=access_enc,
                refresh_token_encrypted=refresh_enc,
                access_token_expires_at=expires_at,
                scope=scope,
                status='active',
                last_refreshed_at=datetime.now(timezone.utc),
            )
        )
    await db.commit()
    return RedirectResponse(url=DASHBOARD_REDIRECT_URL, status_code=302)


def _default_client_factory() -> BaiduPanClient:
    return BaiduPanClient(
        appkey=settings.baidu_pan_appkey,
        appsecret=settings.baidu_pan_appsecret,
    )


# --- T6.3: token refresh tick ---


async def pan_token_refresh_tick(
    db: AsyncSession,
    *,
    client_factory: Callable[[], Any] | None = None,
    window_hours: int = 24,
) -> dict:
    """One iteration of the refresh background task.

    Refreshes pan_credentials whose access_token_expires_at is within
    `window_hours` from now. On Baidu refresh success: UPDATE all four
    fields (access/refresh/expires/scope). On failure: status='revoked'
    + dispatch user notification.

    Returns: {'checked': int, 'refreshed': int, 'revoked': int}
    """
    factory = client_factory or _default_client_factory
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(hours=window_hours)

    rows = (await db.execute(
        select(
            PanCredentials.id,
            PanCredentials.user_id,
            PanCredentials.refresh_token_encrypted,
        ).where(
            PanCredentials.status == 'active',
            PanCredentials.access_token_expires_at < deadline,
        )
    )).all()
    # Close the read transaction so each per-row UPDATE below sees fresh
    # committed state. CodeX P1-1: without this, SQLite (test) snapshot
    # isolation can mask the conditional WHERE refresh_token_encrypted
    # match — the UPDATE would fire against the snapshot's stale value
    # and we'd revoke a credential another tick had already rotated.
    # PG READ COMMITTED already gets fresh state per statement, so this
    # commit is a harmless no-op there; it normalizes both backends.
    await db.commit()

    stats = {'checked': len(rows), 'refreshed': 0, 'revoked': 0,
             'skipped_race': 0}
    client = factory()

    for row in rows:
        # Capture the OLD encrypted bytes so we can do a race-safe
        # conditional update on either branch (CodeX P1-1).
        old_refresh_encrypted = row.refresh_token_encrypted
        try:
            old_refresh = decrypt_token(old_refresh_encrypted)
            new_tokens = client.refresh(old_refresh)
            # ⚠️ Baidu rotates refresh_token — must persist the NEW one.
            # CodeX P1: success path needs the SAME stale-token guard as
            # the failure path below. If two ticks both reach Baidu and
            # both succeed (Baidu grace window, or admin reconnect between
            # our SELECT and our refresh), the later-finishing tick would
            # otherwise overwrite the newer credential. Conditional UPDATE
            # → rowcount=0 means another tick already rotated this row;
            # our tokens are stale even though Baidu accepted them.
            success_result = await db.execute(
                update(PanCredentials)
                .where(
                    PanCredentials.id == row.id,
                    PanCredentials.refresh_token_encrypted == old_refresh_encrypted,
                    PanCredentials.status == 'active',
                )
                .values(
                    access_token_encrypted=encrypt_token(
                        new_tokens['access_token'],
                    ),
                    refresh_token_encrypted=encrypt_token(
                        new_tokens['refresh_token'],
                    ),
                    access_token_expires_at=datetime.now(timezone.utc)
                    + timedelta(seconds=int(new_tokens['expires_in'])),
                    scope=new_tokens.get('scope', ''),
                    last_refreshed_at=datetime.now(timezone.utc),
                )
            )
            await db.commit()
            if success_result.rowcount == 0:
                stats['skipped_race'] += 1
                logger.info(
                    "pan_token_refresh: skip stale-success write cred=%s — "
                    "row already rotated by concurrent tick or admin "
                    "reconnect (race-safe guard)", row.id,
                )
                continue
            stats['refreshed'] += 1
            logger.info(
                "pan_token_refresh: refreshed cred=%s user=%s",
                row.id, row.user_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "pan_token_refresh failed cred=%s user=%s err=%s",
                row.id, row.user_id, exc,
            )
            # CodeX P1-1: conditional revoke. If another tick already
            # rotated this credential's refresh_token, our exception is
            # because we used a stale token — NOT because the credential
            # is dead. The WHERE guard makes the UPDATE a no-op in that
            # case; rowcount=0 → skip the notification too.
            result = await db.execute(
                update(PanCredentials)
                .where(
                    PanCredentials.id == row.id,
                    PanCredentials.refresh_token_encrypted == old_refresh_encrypted,
                    PanCredentials.status == 'active',
                )
                .values(status='revoked')
            )
            if result.rowcount == 0:
                # Another worker won the race. The credential is fine.
                await db.commit()
                stats['skipped_race'] += 1
                logger.info(
                    "pan_token_refresh: skip revoke cred=%s — refresh_token "
                    "already rotated by concurrent tick (race-safe guard)",
                    row.id,
                )
                continue

            try:
                await dispatch_event(
                    db,
                    event_type='pan.token_revoked',
                    user_id=row.user_id,
                    payload={
                        'cred_id': str(row.id),
                        'reason': str(exc)[:200],
                    },
                )
            except Exception as note_exc:  # noqa: BLE001
                logger.error(
                    "pan_token_refresh: dispatch_event failed cred=%s: %s",
                    row.id, note_exc,
                )
            # CodeX 2026-05-19 P1c: commit BEFORE writing the JSONL +
            # bumping the counter. If the commit fails the credential
            # remains 'active' in PG, the notification row is rolled
            # back, and we MUST NOT leave a `pan.token_revoked` line in
            # r2_observability or count it in stats — that would be a
            # false-positive observation of a revocation that didn't
            # actually happen. On commit failure, swallow + continue
            # so the rest of the tick still processes other rows.
            try:
                await db.commit()
            except Exception as commit_exc:  # noqa: BLE001
                logger.error(
                    "pan_token_refresh: commit revoke failed cred=%s: %s "
                    "(no JSONL emit, no stats counter)",
                    row.id, commit_exc,
                )
                try:
                    await db.rollback()
                except Exception:  # noqa: BLE001
                    pass
                continue

            # Phase 9 §T9.4 (CodeX 2026-05-19 P1b): emit JSONL AFTER
            # commit so dashboards only see real revocations. The event
            # is user-scoped not job-scoped, so we use a synthetic
            # filename ``pan-cred-{cred_id}.events.jsonl`` — clearly
            # distinguishable from real job_id files (which are short
            # hashes), still picked up by the dashboard's
            # ``*.events.jsonl`` glob.
            _emit_pan_event_safe(
                job_id=f"pan-cred-{row.id}",
                event_type='pan.token_revoked',
                message=(
                    f"pan token revoked: user={row.user_id} "
                    f"cred={row.id} reason={str(exc)[:100]}"
                ),
                payload={
                    'user_id': str(row.user_id),
                    'cred_id': str(row.id),
                    'reason': str(exc)[:200],
                },
                level='warn',
            )
            stats['revoked'] += 1

    return stats
