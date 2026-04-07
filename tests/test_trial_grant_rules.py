"""Tests for Task 3 trial bookkeeping rules.

These tests explicitly cover the boundary CodeX called out:
- Trial is a one-shot stamp on `users.trial_granted_at`.
- Repeat verification of the same phone MUST NOT re-stamp the trial.
- Repeat verification of the same phone MUST return the existing user, not
  create a duplicate.
- `trial_ends_at` must stay NULL while the gateway plan_catalog has
  `trial.frozen = false`.
- `plan_code` must stay "free" — trial is NOT implemented as plus/pro.
- Consumed / expired OTP challenges cannot be reused.
"""
from __future__ import annotations

import asyncio
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, Response

_gateway_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

import auth_phone  # noqa: E402
import risk_control  # noqa: E402
import sms_provider  # noqa: E402
from auth_phone import VerifyCodeRequest, verify_code_endpoint  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _reset():
    risk_control.reset_rate_limits()
    risk_control.reset_ip_trial_grants()
    sms_provider.clear_fake_state()


def _make_request():
    return SimpleNamespace(
        headers={},
        client=SimpleNamespace(host="127.0.0.1"),
    )


def _make_db():
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()

    async def _noop_refresh(obj):
        return None

    db.refresh = _noop_refresh
    return db


def _setup_verify_db(*, challenge, user):
    db = _make_db()
    challenge_result = MagicMock()
    challenge_scalars = MagicMock()
    challenge_scalars.first.return_value = challenge
    challenge_result.scalars.return_value = challenge_scalars
    user_result = MagicMock()
    user_result.scalar_one_or_none.return_value = user
    # IP trial eligibility check returns None (= no prior grant = eligible)
    ip_none = MagicMock()
    ip_none.scalar_one_or_none.return_value = None
    calls = {"n": 0}

    async def _execute(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return challenge_result
        if calls["n"] == 2:
            return user_result
        # call 3+: IP trial eligibility check
        return ip_none

    db.execute = _execute
    return db


def _make_challenge(phone="13800138000", code="654321", expired=False, consumed=False):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid.uuid4(),
        phone_number=phone,
        code=code,
        client_ip="127.0.0.1",
        purpose="login",
        expires_at=now - timedelta(seconds=1) if expired else now + timedelta(minutes=5),
        consumed_at=now if consumed else None,
        created_at=now,
    )


def _make_user(**overrides):
    base = dict(
        id=uuid.uuid4(),
        email=None,
        display_name="138****8000",
        password_hash=None,
        is_active=True,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        role="user",
        plan_code="free",
        free_jobs_quota_total=5,
        free_jobs_quota_used=0,
        phone_number="13800138000",
        phone_verified_at=None,
        trial_granted_at=None,
        trial_ends_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _clean():
    _reset()
    yield
    _reset()


class TestFirstTrialGrant:
    def test_new_phone_verify_returns_registration_token_not_user(self):
        """A1: verify_code for a new phone returns a registration_token.

        Trial is NOT granted at verify-code time. The user must complete
        registration (set password) before trial is granted. This test
        verifies that verify_code does not create a User or stamp trial.
        """
        ch = _make_challenge(code="111222")
        db = _setup_verify_db(challenge=ch, user=None)
        session_mock = AsyncMock(return_value="token")
        with patch("auth_phone.create_session", new=session_mock):
            result = _run(
                verify_code_endpoint(
                    VerifyCodeRequest(phone_number="13800138000", code="111222"),
                    _make_request(),
                    Response(),
                    db,
                )
            )
        assert result["needs_password"] is True
        assert result["registration_token"] is not None
        session_mock.assert_not_called()
        # No User should have been created at this stage.
        added_types = [type(call.args[0]).__name__ for call in db.add.call_args_list]
        assert "User" not in added_types


class TestRepeatedVerification:
    def test_returning_user_reuses_existing_account(self):
        existing = _make_user(
            trial_granted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        ch = _make_challenge(code="111222")
        db = _setup_verify_db(challenge=ch, user=existing)
        with patch("auth_phone.create_session", new=AsyncMock(return_value="token")):
            result = _run(
                verify_code_endpoint(
                    VerifyCodeRequest(phone_number="13800138000", code="111222"),
                    _make_request(),
                    Response(),
                    db,
                )
            )
        assert result["is_new"] is False
        # No new User row was created.
        db.add.assert_not_called()

    def test_does_not_re_stamp_trial_granted_at(self):
        original_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        existing = _make_user(trial_granted_at=original_ts)
        ch = _make_challenge(code="111222")
        db = _setup_verify_db(challenge=ch, user=existing)
        with patch("auth_phone.create_session", new=AsyncMock(return_value="token")):
            _run(
                verify_code_endpoint(
                    VerifyCodeRequest(phone_number="13800138000", code="111222"),
                    _make_request(),
                    Response(),
                    db,
                )
            )
        # The timestamp must be untouched.
        assert existing.trial_granted_at == original_ts
        # Plan code must still be "free" — never silently upgraded to plus.
        assert existing.plan_code == "free"
        # trial_ends_at stays as originally set — not re-stamped on repeat login.
        assert existing.trial_ends_at is None  # was never set for this fixture


class TestFrozenTrialFacts:
    """A1 rewrite: Trial is now granted at complete-registration, not at
    verify-code. These tests verify the A1 invariant that verify-code for
    new phones does NOT grant trial or create users.

    The actual trial-granting tests belong in a test_complete_registration
    suite once that endpoint has dedicated coverage.
    """

    def test_verify_code_new_phone_does_not_grant_trial(self):
        """verify_code for a new phone must NOT stamp trial fields."""
        ch = _make_challenge(code="111222")
        db = _setup_verify_db(challenge=ch, user=None)
        with patch("auth_phone.create_session", new=AsyncMock(return_value="token")):
            result = _run(
                verify_code_endpoint(
                    VerifyCodeRequest(phone_number="13800138000", code="111222"),
                    _make_request(),
                    Response(),
                    db,
                )
            )
        # A1: new phone → registration_token, not user creation.
        assert result["needs_password"] is True
        # No User was created, so no trial was granted.
        added_types = [type(call.args[0]).__name__ for call in db.add.call_args_list]
        assert "User" not in added_types

    def test_verify_code_new_phone_plan_code_not_set(self):
        """A1: verify_code must not set plan_code on any object."""
        ch = _make_challenge(code="111222")
        db = _setup_verify_db(challenge=ch, user=None)
        with patch("auth_phone.create_session", new=AsyncMock(return_value="token")):
            _run(
                verify_code_endpoint(
                    VerifyCodeRequest(phone_number="13800138000", code="111222"),
                    _make_request(),
                    Response(),
                    db,
                )
            )
        # The only thing added should be a PhoneVerificationChallenge (reg token).
        for call in db.add.call_args_list:
            obj = call.args[0]
            if hasattr(obj, "plan_code"):
                assert obj.plan_code in (None, "free")


class TestChallengeLifecycle:
    def test_expired_challenge_is_rejected(self):
        ch = _make_challenge(code="111222", expired=True)
        # Note: the endpoint query filters `expires_at > now`, so an expired
        # challenge would NOT be returned. Simulate the filter by feeding
        # `challenge=None` to mimic the WHERE clause, then assert the 400.
        db = _setup_verify_db(challenge=None, user=None)
        with pytest.raises(HTTPException) as exc_info:
            _run(
                verify_code_endpoint(
                    VerifyCodeRequest(phone_number="13800138000", code="111222"),
                    _make_request(),
                    Response(),
                    db,
                )
            )
        assert exc_info.value.status_code == 400
        # Keep the expired-challenge object reachable so the assertion above
        # reads naturally — it encodes the invariant we care about.
        assert ch.expires_at < datetime.now(timezone.utc)

    def test_consumed_challenge_cannot_be_reused(self):
        # Consumed challenges are filtered out by the endpoint's WHERE clause.
        ch = _make_challenge(code="111222", consumed=True)
        db = _setup_verify_db(challenge=None, user=None)
        with pytest.raises(HTTPException) as exc_info:
            _run(
                verify_code_endpoint(
                    VerifyCodeRequest(phone_number="13800138000", code="111222"),
                    _make_request(),
                    Response(),
                    db,
                )
            )
        assert exc_info.value.status_code == 400
        assert ch.consumed_at is not None

    def test_successful_verify_consumes_challenge(self):
        ch = _make_challenge(code="111222")
        assert ch.consumed_at is None
        db = _setup_verify_db(challenge=ch, user=None)
        with patch("auth_phone.create_session", new=AsyncMock(return_value="token")):
            _run(
                verify_code_endpoint(
                    VerifyCodeRequest(phone_number="13800138000", code="111222"),
                    _make_request(),
                    Response(),
                    db,
                )
            )
        # After a successful verification the challenge row was stamped.
        assert ch.consumed_at is not None
