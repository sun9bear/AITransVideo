"""Tests for gateway/pan/auth.py.

Plan 2026-05-14 Phase 6 §T6.1-T6.4. Exercises:
  - state token helpers (generate / insert / consume one-shot + expiry)
  - _pan_callback_impl with mock client_factory (UPSERT pan_credentials)
  - pan_token_refresh_tick happy path + rotation + failure → revoked +
    notification dispatch
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

# Set up sys.path + stub database before any pan imports.
from tests.pan_fixtures import (  # noqa: F401
    FakeBaiduPanClient,
    insert_sample_pan_credentials,
    pan_test_engine,
    run_async,
    setup_pan_token_env,
)


# Extend pan_test_engine with PanOauthState table for these tests.
@asynccontextmanager
async def auth_test_engine():
    """In-memory SQLite with all 4 pan-related tables."""
    from models import (
        BackupRecord, Job, PanCredentials, PanOauthState,
    )

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            for table_cls in (Job, BackupRecord, PanCredentials, PanOauthState):
                await conn.run_sync(lambda c, t=table_cls: t.__table__.create(c))
        yield engine
    finally:
        await engine.dispose()


async def _session(engine):
    """Open a SQLAlchemy AsyncSession wrapper around the engine.

    The pan/auth.py helpers call db.commit() etc. on an AsyncSession. We
    bind one fresh via sessionmaker."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# =========================================================================
# T6.1 — state token helpers
# =========================================================================


def test_generate_state_token_unique():
    from pan.auth import generate_state_token
    tokens = {generate_state_token() for _ in range(1000)}
    assert len(tokens) == 1000


def test_generate_state_token_url_safe_length():
    """secrets.token_urlsafe(32) yields ~43 chars (no '=' padding when
    nbytes is a multiple of 3). Must fit in PanOauthState.token String(64)."""
    from pan.auth import generate_state_token
    t = generate_state_token()
    assert 40 <= len(t) <= 64
    # URL-safe alphabet: A-Z a-z 0-9 - _
    assert all(c.isalnum() or c in '-_' for c in t)


def test_insert_state_token_persists_with_ttl():
    from models import PanOauthState
    from pan.auth import insert_state_token, STATE_TTL_SECONDS

    user_id = uuid.uuid4()

    async def _go():
        async with auth_test_engine() as engine:
            Session = await _session(engine)
            async with Session() as db:
                token = await insert_state_token(db, user_id=user_id)

            async with Session() as db:
                row = (await db.execute(
                    select(
                        PanOauthState.user_id, PanOauthState.expires_at,
                    ).where(PanOauthState.token == token)
                )).one()
            assert row.user_id == user_id
            # SQLite stores naive datetime — normalize for comparison.
            expires = row.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            delta_seconds = (expires - datetime.now(timezone.utc)).total_seconds()
            assert STATE_TTL_SECONDS - 60 < delta_seconds <= STATE_TTL_SECONDS

    run_async(_go())


def test_consume_state_token_happy_path():
    """Valid + non-expired state token → returns user_id and DELETEs row."""
    from models import PanOauthState
    from pan.auth import consume_state_token, insert_state_token

    user_id = uuid.uuid4()

    async def _go():
        async with auth_test_engine() as engine:
            Session = await _session(engine)
            async with Session() as db:
                token = await insert_state_token(db, user_id=user_id)

            async with Session() as db:
                returned = await consume_state_token(db, token)
            assert returned == user_id

            # Row deleted.
            async with Session() as db:
                row = (await db.execute(
                    select(PanOauthState.token)
                    .where(PanOauthState.token == token)
                )).one_or_none()
            assert row is None

    run_async(_go())


def test_consume_state_token_rejects_unknown():
    from pan.auth import consume_state_token

    async def _go():
        async with auth_test_engine() as engine:
            Session = await _session(engine)
            async with Session() as db:
                returned = await consume_state_token(db, 'never_existed')
            assert returned is None

    run_async(_go())


def test_consume_state_token_rejects_expired():
    """Token past expires_at → consume returns None AND deletes the row."""
    from models import PanOauthState
    from pan.auth import consume_state_token, insert_state_token

    user_id = uuid.uuid4()

    async def _go():
        async with auth_test_engine() as engine:
            Session = await _session(engine)
            # Insert with NEGATIVE ttl → already expired.
            async with Session() as db:
                token = await insert_state_token(
                    db, user_id=user_id, ttl_seconds=-1,
                )

            async with Session() as db:
                returned = await consume_state_token(db, token)
            assert returned is None

            # Expired row also cleaned up.
            async with Session() as db:
                row = (await db.execute(
                    select(PanOauthState.token)
                    .where(PanOauthState.token == token)
                )).one_or_none()
            assert row is None

    run_async(_go())


def test_consume_state_token_is_one_shot():
    """A second consume with the same token after success returns None."""
    from pan.auth import consume_state_token, insert_state_token

    user_id = uuid.uuid4()

    async def _go():
        async with auth_test_engine() as engine:
            Session = await _session(engine)
            async with Session() as db:
                token = await insert_state_token(db, user_id=user_id)

            async with Session() as db:
                first = await consume_state_token(db, token)
            assert first == user_id

            async with Session() as db:
                second = await consume_state_token(db, token)
            assert second is None

    run_async(_go())


def test_consume_state_token_concurrent_callbacks_only_one_wins():
    """CodeX P2: two callbacks racing on the same state token must see
    EXACTLY ONE win and one rejection. The SELECT-then-DELETE pattern
    would have allowed both to pass validation, then both to exchange
    code (Baidu would reject the second, but only AFTER an unnecessary
    round-trip + state confusion). DELETE ... RETURNING makes the
    consume atomic at the DB layer."""
    import asyncio as _asyncio
    from pan.auth import consume_state_token, insert_state_token

    user_id = uuid.uuid4()

    async def _go():
        async with auth_test_engine() as engine:
            Session = await _session(engine)
            async with Session() as db:
                token = await insert_state_token(db, user_id=user_id)

            # Two concurrent consume calls in separate sessions.
            async def _attempt():
                async with Session() as db:
                    return await consume_state_token(db, token)

            results = await _asyncio.gather(_attempt(), _attempt())

            wins = [r for r in results if r == user_id]
            losses = [r for r in results if r is None]
            assert len(wins) == 1, (
                f"exactly one consume must win, got results={results}"
            )
            assert len(losses) == 1

    run_async(_go())


# =========================================================================
# T6.2 — callback flow
# =========================================================================


def test_callback_exchanges_code_and_inserts_credentials(monkeypatch):
    from models import PanCredentials
    from pan.auth import _pan_callback_impl, insert_state_token
    from pan.token_crypto import decrypt_token

    setup_pan_token_env(monkeypatch)
    monkeypatch.setattr(
        'config.settings.baidu_pan_appkey', 'test_appkey', raising=False,
    )
    monkeypatch.setattr(
        'config.settings.baidu_pan_appsecret', 'test_secret', raising=False,
    )
    monkeypatch.setattr(
        'config.settings.baidu_pan_redirect_uri',
        'https://aitrans.video/api/admin/pan/callback',
        raising=False,
    )

    user_id = uuid.uuid4()
    fake_client = FakeBaiduPanClient(appkey='test_appkey', appsecret='test_secret')

    async def _go():
        async with auth_test_engine() as engine:
            Session = await _session(engine)
            async with Session() as db:
                state = await insert_state_token(db, user_id=user_id)

            async with Session() as db:
                response = await _pan_callback_impl(
                    code='auth_code_xyz',
                    state=state,
                    db=db,
                    client_factory=lambda: fake_client,
                )

            # 302 to dashboard.
            assert response.status_code == 302
            assert response.headers['location'] == '/admin/pan/dashboard'

            # PanCredentials INSERTed with encrypted tokens.
            async with Session() as db:
                row = (await db.execute(
                    select(
                        PanCredentials.user_id,
                        PanCredentials.provider,
                        PanCredentials.access_token_encrypted,
                        PanCredentials.refresh_token_encrypted,
                        PanCredentials.status,
                        PanCredentials.access_token_expires_at,
                    ).where(PanCredentials.user_id == user_id)
                )).one()

            assert row.user_id == user_id
            assert row.provider == 'baidu_pan'
            assert row.status == 'active'
            # Encrypted tokens decrypt back to FakeBaiduPanClient.exchange_code
            # canned response.
            assert decrypt_token(row.access_token_encrypted) == 'fake_access'
            assert decrypt_token(row.refresh_token_encrypted) == 'fake_refresh'
            # exchange_code returns expires_in=2592000 (~30d).
            expires = row.access_token_expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            delta = (expires - datetime.now(timezone.utc)).total_seconds()
            # 30 days = 2,592,000 s. Allow 5 min slop.
            assert 2_591_500 < delta < 2_592_500

            # exchange_code called with the right args.
            assert fake_client.exchange_code_calls == [{
                'code': 'auth_code_xyz',
                'redirect_uri': 'https://aitrans.video/api/admin/pan/callback',
            }]

    run_async(_go())


def test_callback_rejects_invalid_state(monkeypatch):
    """Unknown state token → HTTP 400."""
    from fastapi import HTTPException
    from pan.auth import _pan_callback_impl

    setup_pan_token_env(monkeypatch)
    monkeypatch.setattr(
        'config.settings.baidu_pan_appkey', 'ak', raising=False,
    )
    monkeypatch.setattr(
        'config.settings.baidu_pan_appsecret', 'as', raising=False,
    )

    fake_client = FakeBaiduPanClient()

    async def _go():
        async with auth_test_engine() as engine:
            Session = await _session(engine)
            async with Session() as db:
                with pytest.raises(HTTPException) as exc_info:
                    await _pan_callback_impl(
                        code='some_code',
                        state='never_issued',
                        db=db,
                        client_factory=lambda: fake_client,
                    )
            assert exc_info.value.status_code == 400
            # exchange_code must NOT have been called for an invalid state.
            assert fake_client.exchange_code_calls == []

    run_async(_go())


def test_callback_replaces_existing_credentials(monkeypatch):
    """Re-connect (admin runs OAuth flow again) UPDATEs existing row in place,
    flips status='revoked' back to 'active', keeps same row id."""
    from models import PanCredentials
    from pan.auth import _pan_callback_impl, insert_state_token
    from pan.token_crypto import decrypt_token

    setup_pan_token_env(monkeypatch)
    monkeypatch.setattr(
        'config.settings.baidu_pan_appkey', 'ak', raising=False,
    )
    monkeypatch.setattr(
        'config.settings.baidu_pan_appsecret', 'as', raising=False,
    )
    monkeypatch.setattr(
        'config.settings.baidu_pan_redirect_uri', 'https://x/cb', raising=False,
    )

    user_id = uuid.uuid4()

    async def _go():
        async with auth_test_engine() as engine:
            Session = await _session(engine)
            # Pre-existing revoked credentials for this user.
            async with Session() as db:
                pass  # use insert_sample_pan_credentials below
            await insert_sample_pan_credentials(
                engine, user_id=user_id,
                access_token='OLD_access', refresh_token='OLD_refresh',
                status='revoked',
            )

            async with Session() as db:
                row_before = (await db.execute(
                    select(PanCredentials.id).where(
                        PanCredentials.user_id == user_id,
                    )
                )).scalar_one()
                state = await insert_state_token(db, user_id=user_id)

            fake_client = FakeBaiduPanClient()
            async with Session() as db:
                await _pan_callback_impl(
                    code='new_code', state=state, db=db,
                    client_factory=lambda: fake_client,
                )

            # Same row id, updated to active with new tokens.
            async with Session() as db:
                row_after = (await db.execute(
                    select(
                        PanCredentials.id,
                        PanCredentials.status,
                        PanCredentials.access_token_encrypted,
                    ).where(PanCredentials.user_id == user_id)
                )).one()
            assert row_after.id == row_before
            assert row_after.status == 'active'
            assert decrypt_token(row_after.access_token_encrypted) == 'fake_access'

    run_async(_go())


# =========================================================================
# T6.3 + T6.4 — refresh tick
# =========================================================================


def _set_expires(engine, *, user_id, expires_at):
    """Helper: directly mutate the PanCredentials.access_token_expires_at
    field to simulate "about to expire" scenarios."""
    from models import PanCredentials
    from sqlalchemy import update as _update

    async def _go():
        async with engine.begin() as conn:
            await conn.execute(
                _update(PanCredentials)
                .where(PanCredentials.user_id == user_id)
                .values(access_token_expires_at=expires_at)
            )

    return _go()


def test_refresh_tick_skips_credentials_far_from_expiry(monkeypatch):
    """access_token_expires_at > now + 24h → NOT refreshed."""
    from pan.auth import pan_token_refresh_tick

    setup_pan_token_env(monkeypatch)

    user_id = uuid.uuid4()

    async def _go():
        async with auth_test_engine() as engine:
            await insert_sample_pan_credentials(
                engine, user_id=user_id, status='active',
            )
            # Default insert sets expires_at to now + 30 days — well beyond
            # the 24h window.

            Session = await _session(engine)
            fake_client = FakeBaiduPanClient()
            async with Session() as db:
                stats = await pan_token_refresh_tick(
                    db, client_factory=lambda: fake_client,
                )

            assert stats['checked'] == 0
            assert stats['refreshed'] == 0
            assert stats['revoked'] == 0
            assert fake_client.refresh_calls == []

    run_async(_go())


def test_refresh_tick_refreshes_within_window(monkeypatch):
    """access_token_expires_at within 24h → refresh + UPDATE rotated tokens."""
    from models import PanCredentials
    from pan.auth import pan_token_refresh_tick
    from pan.token_crypto import decrypt_token

    setup_pan_token_env(monkeypatch)

    user_id = uuid.uuid4()

    async def _go():
        async with auth_test_engine() as engine:
            await insert_sample_pan_credentials(
                engine, user_id=user_id, status='active',
                access_token='OLD_access', refresh_token='OLD_refresh',
            )
            # Push expires_at to 1h from now → inside the 24h window.
            await _set_expires(
                engine, user_id=user_id,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )

            Session = await _session(engine)
            fake_client = FakeBaiduPanClient()
            async with Session() as db:
                stats = await pan_token_refresh_tick(
                    db, client_factory=lambda: fake_client,
                )

            assert stats['checked'] == 1
            assert stats['refreshed'] == 1
            assert stats['revoked'] == 0
            assert stats.get('skipped_race', 0) == 0
            # Baidu.refresh called with OLD refresh token.
            assert fake_client.refresh_calls == [{'refresh_token': 'OLD_refresh'}]

            # New rotated tokens persisted (FakeBaiduPanClient.refresh
            # canned response: NEW_access / NEW_refresh).
            async with Session() as db:
                row = (await db.execute(
                    select(
                        PanCredentials.access_token_encrypted,
                        PanCredentials.refresh_token_encrypted,
                        PanCredentials.status,
                        PanCredentials.last_refreshed_at,
                    ).where(PanCredentials.user_id == user_id)
                )).one()
            assert decrypt_token(row.access_token_encrypted) == 'new_access'
            assert decrypt_token(row.refresh_token_encrypted) == 'new_refresh'
            assert row.status == 'active'
            assert row.last_refreshed_at is not None

    run_async(_go())


def test_refresh_tick_marks_revoked_on_failure_and_dispatches_real_notification(
    monkeypatch,
):
    """Refresh failure → status='revoked' + a REAL UserNotification row
    in user_notifications. CodeX P1-2: the old test monkeypatched
    dispatch_event and only verified "called" — but dispatch_event
    silently drops unknown event_types, so a misnamed event_type would
    have masked the real bug (notification never landed in DB).

    This test exercises the actual notifications_service.dispatch_event
    code path and asserts the persisted UserNotification row carries
    the registered recipe's title/body/severity."""
    from models import PanCredentials, UserNotification
    from pan.auth import pan_token_refresh_tick

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()

    class FailingClient(FakeBaiduPanClient):
        def refresh(self, refresh_token):
            raise RuntimeError('synthetic refresh failure')

    async def _go():
        # Extend engine with UserNotification table for real dispatch.
        async with auth_test_engine() as engine:
            async with engine.begin() as conn:
                await conn.run_sync(
                    lambda c: UserNotification.__table__.create(c),
                )
            await insert_sample_pan_credentials(
                engine, user_id=user_id, status='active',
            )
            await _set_expires(
                engine, user_id=user_id,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )

            Session = await _session(engine)
            client = FailingClient()
            async with Session() as db:
                stats = await pan_token_refresh_tick(
                    db, client_factory=lambda: client,
                )

            assert stats['checked'] == 1
            assert stats['refreshed'] == 0
            assert stats['revoked'] == 1
            assert stats['skipped_race'] == 0

            # status='revoked' persisted.
            async with Session() as db:
                status = (await db.execute(
                    select(PanCredentials.status)
                    .where(PanCredentials.user_id == user_id)
                )).scalar_one()
            assert status == 'revoked'

            # Real UserNotification row landed in DB (not silently dropped
            # by an unknown-event_type code path).
            async with Session() as db:
                notif = (await db.execute(
                    select(
                        UserNotification.title,
                        UserNotification.body,
                        UserNotification.severity,
                        UserNotification.action_url,
                        UserNotification.topic,
                    ).where(UserNotification.user_id == user_id)
                )).one()
            assert notif.title == "网盘授权已失效"
            assert "重新连接" in notif.body
            assert notif.severity == 'warning'
            assert notif.action_url == '/admin/pan/dashboard'
            assert notif.topic == 'account'

    run_async(_go())


def test_refresh_tick_dispatch_event_failure_does_not_block_revoke(monkeypatch):
    """If dispatch_event itself raises, the credential is still marked revoked.
    Notification is best-effort; PG is source of truth for status."""
    from models import PanCredentials
    from pan import auth as auth_mod
    from pan.auth import pan_token_refresh_tick

    setup_pan_token_env(monkeypatch)

    user_id = uuid.uuid4()

    async def failing_dispatch(db, **kwargs):
        raise RuntimeError('synthetic notification failure')

    monkeypatch.setattr(auth_mod, 'dispatch_event', failing_dispatch)

    class FailingClient(FakeBaiduPanClient):
        def refresh(self, refresh_token):
            raise RuntimeError('refresh boom')

    async def _go():
        async with auth_test_engine() as engine:
            await insert_sample_pan_credentials(
                engine, user_id=user_id, status='active',
            )
            await _set_expires(
                engine, user_id=user_id,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )

            Session = await _session(engine)
            async with Session() as db:
                stats = await pan_token_refresh_tick(
                    db, client_factory=lambda: FailingClient(),
                )

            assert stats['revoked'] == 1
            # Even though dispatch_event raised, status is revoked.
            async with Session() as db:
                status = (await db.execute(
                    select(PanCredentials.status)
                    .where(PanCredentials.user_id == user_id)
                )).scalar_one()
            assert status == 'revoked'

    run_async(_go())


def test_refresh_tick_success_path_does_not_overwrite_newer_token(
    monkeypatch, tmp_path,
):
    """CodeX 2026-05-18 P1 (round 2): the SUCCESS path must also be
    race-safe. If two ticks both reach Baidu and Baidu accepts both
    (grace window for the previous refresh_token, or admin reconnect
    between our SELECT and our successful refresh), the later-finishing
    tick must NOT overwrite the newer credential.

    Simulation mirrors the failure-path race test: file-based SQLite,
    inside refresh() rotate the row via sync sqlite3 to simulate
    "winner already wrote NEWER tokens", then let refresh() succeed.
    Conditional UPDATE rowcount=0 → skipped_race incremented, the
    winner's NEWER row stays intact."""
    from models import (
        BackupRecord, Job, PanCredentials, PanOauthState,
    )
    from pan.auth import pan_token_refresh_tick
    from pan.token_crypto import decrypt_token, encrypt_token
    from sqlalchemy.ext.asyncio import (
        async_sessionmaker, create_async_engine, AsyncSession,
    )

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    db_file = tmp_path / 'race_success.db'

    class RaceWinnerSucceedClient(FakeBaiduPanClient):
        """Tick A already rotated the row + we then return success too.
        The conditional UPDATE must catch us as stale."""

        def __init__(self, db_path):
            super().__init__()
            self._db_path = db_path
            self.winner_access = encrypt_token('WINNER_access')
            self.winner_refresh = encrypt_token('WINNER_refresh')

        def refresh(self, refresh_token):
            self.refresh_calls.append({'refresh_token': refresh_token})
            # Tick A wrote NEW tokens. Our async tick still has the
            # OLD refresh_token_encrypted captured.
            import sqlite3
            conn = sqlite3.connect(self._db_path)
            try:
                rc = conn.execute(
                    "UPDATE pan_credentials SET refresh_token_encrypted = ?, "
                    "access_token_encrypted = ? WHERE user_id = ?",
                    (self.winner_refresh, self.winner_access, user_id.hex),
                ).rowcount
                conn.commit()
                assert rc == 1
            finally:
                conn.close()
            # Baidu happens to accept our (now stale) token in a grace
            # window — return canned success.
            return {
                'access_token': 'OUR_stale_access',
                'refresh_token': 'OUR_stale_refresh',
                'expires_in': 2592000,
                'scope': 'basic netdisk',
            }

    async def _go():
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_file}",
            connect_args={"check_same_thread": False},
        )
        try:
            async with engine.begin() as conn:
                for t in (Job, BackupRecord, PanCredentials, PanOauthState):
                    await conn.run_sync(
                        lambda c, _t=t: _t.__table__.create(c),
                    )

            await insert_sample_pan_credentials(
                engine, user_id=user_id, status='active',
                refresh_token='STALE_old_refresh',
            )
            await _set_expires(
                engine, user_id=user_id,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )

            Session = async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False,
            )
            client = RaceWinnerSucceedClient(str(db_file))
            async with Session() as db:
                stats = await pan_token_refresh_tick(
                    db, client_factory=lambda: client,
                )

            # We "succeeded" at Baidu but our row write was a no-op.
            assert stats['checked'] == 1
            assert stats['refreshed'] == 0
            assert stats['revoked'] == 0
            assert stats['skipped_race'] == 1

            # Winner's tokens preserved — NOT overwritten by our stale
            # success.
            async with Session() as db:
                row = (await db.execute(
                    select(
                        PanCredentials.access_token_encrypted,
                        PanCredentials.refresh_token_encrypted,
                    ).where(PanCredentials.user_id == user_id)
                )).one()
            assert decrypt_token(row.access_token_encrypted) == 'WINNER_access'
            assert decrypt_token(row.refresh_token_encrypted) == 'WINNER_refresh'
        finally:
            await engine.dispose()

    run_async(_go())


def test_refresh_tick_concurrent_rotation_does_not_revoke_winner(
    monkeypatch, tmp_path,
):
    """CodeX P1-1: two refresh ticks both select the same row. Tick A
    wins (Baidu rotates refresh_token + PG row updated). Tick B has the
    stale refresh_token in memory; Baidu rejects it. The conditional
    UPDATE guard MUST detect refresh_token_encrypted no longer matches
    the captured old value and SKIP the revoke — the credential is
    healthy.

    Implementation: file-based SQLite so a SYNC sqlite3 connection from
    inside client.refresh() can mutate the same DB the async tick reads.
    Inside refresh(): simulate "winner rotated the row" then raise."""
    from models import (
        BackupRecord, Job, PanCredentials, PanOauthState,
    )
    from pan.auth import pan_token_refresh_tick
    from pan.token_crypto import encrypt_token
    from sqlalchemy.ext.asyncio import (
        async_sessionmaker, create_async_engine, AsyncSession,
    )

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()
    db_file = tmp_path / 'race_test.db'

    class RaceWinnerClient(FakeBaiduPanClient):
        """When called, simulate "Tick A already rotated this row in the
        DB" via a SYNC sqlite3 connection, then raise to enter our
        failure path."""

        def __init__(self, db_path):
            super().__init__()
            self._db_path = db_path

        def refresh(self, refresh_token):
            self.refresh_calls.append({'refresh_token': refresh_token})
            # Tick A's effect: rotate the row's refresh_token_encrypted
            # so the row no longer matches what we captured.
            import sqlite3
            new_enc = encrypt_token('NEW_after_race')
            conn = sqlite3.connect(self._db_path)
            try:
                # SQLAlchemy's UUID-as-CHAR(36) compiles to hex storage
                # WITHOUT hyphens on SQLite. Use .hex (32 chars) to match
                # what the row actually stores.
                rc = conn.execute(
                    "UPDATE pan_credentials SET refresh_token_encrypted = ? "
                    "WHERE user_id = ?",
                    (new_enc, user_id.hex),
                ).rowcount
                conn.commit()
                assert rc == 1, (
                    f"sync mutation must affect 1 row to simulate the race "
                    f"correctly; got rowcount={rc}"
                )
            finally:
                conn.close()
            # Now Baidu rejects our stale token.
            raise RuntimeError('synthetic stale token rejection from Baidu')

    async def _go():
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_file}",
            connect_args={"check_same_thread": False},
        )
        try:
            async with engine.begin() as conn:
                for t in (Job, BackupRecord, PanCredentials, PanOauthState):
                    await conn.run_sync(
                        lambda c, _t=t: _t.__table__.create(c),
                    )

            await insert_sample_pan_credentials(
                engine, user_id=user_id, status='active',
                refresh_token='STALE_old_refresh',
            )
            await _set_expires(
                engine, user_id=user_id,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )

            Session = async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False,
            )
            client = RaceWinnerClient(str(db_file))
            async with Session() as db:
                stats = await pan_token_refresh_tick(
                    db, client_factory=lambda: client,
                )

            # Losing tick's revoke was a no-op via conditional UPDATE.
            assert stats['checked'] == 1
            assert stats['refreshed'] == 0
            assert stats['revoked'] == 0
            assert stats['skipped_race'] == 1

            # Credential is STILL active (winner's row preserved).
            async with Session() as db:
                status = (await db.execute(
                    select(PanCredentials.status)
                    .where(PanCredentials.user_id == user_id)
                )).scalar_one()
            assert status == 'active'
        finally:
            await engine.dispose()

    run_async(_go())


# =========================================================================
# T6.5 — router registration in gateway/main.py
# =========================================================================


def test_pan_auth_router_registered_in_main():
    """T6.5: gateway/main.py imports pan.auth.router and calls
    app.include_router(pan_auth_router). Without both lines the
    endpoints aren't reachable from production. AST scan rather than
    full main.py import (which would require DB engine wired)."""
    import ast
    from pathlib import Path

    main_py = (
        Path(__file__).resolve().parent.parent / 'gateway' / 'main.py'
    )
    text = main_py.read_text(encoding='utf-8')
    tree = ast.parse(text)

    # 1. Import: `from pan.auth import router as pan_auth_router` (or
    #    re-binding it under any name).
    imported_as: str | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == 'pan.auth':
            for alias in node.names:
                if alias.name == 'router':
                    imported_as = alias.asname or 'router'
                    break
    assert imported_as is not None, (
        "gateway/main.py must import the pan auth router: "
        "`from pan.auth import router as pan_auth_router`"
    )

    # 2. app.include_router(imported_as) call must exist.
    found_include = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'include_router'
            and len(node.args) >= 1
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == imported_as
        ):
            found_include = True
            break
    assert found_include, (
        f"gateway/main.py must call app.include_router({imported_as}). "
        "Without this, /api/admin/pan/connect and /callback are "
        "unreachable in production."
    )


def test_refresh_tick_skips_already_revoked_credentials(monkeypatch):
    """Credentials with status='revoked' must NOT be refreshed (would
    re-activate a credential admin had marked dead)."""
    from pan.auth import pan_token_refresh_tick

    setup_pan_token_env(monkeypatch)
    user_id = uuid.uuid4()

    async def _go():
        async with auth_test_engine() as engine:
            await insert_sample_pan_credentials(
                engine, user_id=user_id, status='revoked',
            )
            await _set_expires(
                engine, user_id=user_id,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )

            Session = await _session(engine)
            fake_client = FakeBaiduPanClient()
            async with Session() as db:
                stats = await pan_token_refresh_tick(
                    db, client_factory=lambda: fake_client,
                )

            assert stats['checked'] == 0
            assert stats['refreshed'] == 0
            assert stats['revoked'] == 0
            assert fake_client.refresh_calls == []

    run_async(_go())
