"""Tests for email verification registration and email password reset."""

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

import auth  # noqa: E402
import auth_email  # noqa: E402
from auth_email import (  # noqa: E402
    CompleteEmailRegistrationRequest,
    ResetEmailPasswordRequest,
    SendEmailResetCodeRequest,
    VerifyEmailRegistrationCodeRequest,
    complete_email_registration_endpoint,
    reset_email_password_endpoint,
    send_email_reset_code_endpoint,
    verify_email_registration_code_endpoint,
)
from config import settings  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_request(client_ip: str | None = "127.0.0.1"):
    headers: dict[str, str] = {}
    client = SimpleNamespace(host=client_ip) if client_ip else None
    return SimpleNamespace(headers=headers, client=client)


def _make_db():
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _result_one(obj):
    result = MagicMock()
    result.scalar_one_or_none.return_value = obj
    scalars = MagicMock()
    scalars.first.return_value = obj
    result.scalars.return_value = scalars
    return result


def _challenge(
    *,
    email: str = "user@test.com",
    code: str = "123456",
    purpose: str = "registration",
    attempts: int = 0,
    password_hash: str | None = None,
):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid.uuid4(),
        email=email,
        code_hash=auth.hash_password(code),
        client_ip="1.1.1.1",
        purpose=purpose,
        password_hash=password_hash,
        display_name="新用户",
        expires_at=now + timedelta(minutes=15),
        consumed_at=None,
        attempts=attempts,
        created_at=now,
    )


def _user(email: str = "user@test.com", password: str = "oldpassword123"):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid.uuid4(),
        email=email,
        display_name="用户",
        password_hash=auth.hash_password(password),
        is_active=True,
        email_verified_at=None,
        phone_number=None,
        role="user",
        created_at=now,
    )


def setup_function():
    auth_email.clear_fake_state()
    auth_email.reset_email_rate_limits()


def teardown_function():
    auth_email.clear_fake_state()
    auth_email.reset_email_rate_limits()


def test_verify_registration_code_returns_registration_token(monkeypatch):
    monkeypatch.setattr(settings, "email_auth_provider", "fake")
    ch = _challenge(code="123456")
    db = _make_db()
    db.execute = AsyncMock(return_value=_result_one(ch))

    result = _run(
        verify_email_registration_code_endpoint(
            VerifyEmailRegistrationCodeRequest(
                email="User@Test.COM",
                code="123456",
            ),
            db,
        )
    )

    assert result["ok"] is True
    assert result["needs_password"] is True
    assert result["email"] == "user@test.com"
    assert result["registration_token"]
    assert ch.purpose == "registration_token"
    assert ch.attempts == 0
    assert ch.consumed_at is None
    assert auth.verify_password(result["registration_token"], ch.code_hash)


def test_complete_registration_creates_verified_user_and_session(monkeypatch):
    monkeypatch.setattr(settings, "email_auth_provider", "fake")
    ch = _challenge(code="registration-token", purpose="registration_token")
    db = _make_db()
    db.execute = AsyncMock(side_effect=[_result_one(ch), _result_one(None)])

    fake_announcements = types.ModuleType("system_announcements_service")
    fake_announcements.dispatch_announcements_for_new_user = AsyncMock(return_value=0)

    session_mock = AsyncMock(return_value="token")
    with patch.dict(sys.modules, {"system_announcements_service": fake_announcements}):
        with patch("auth_email.create_session", new=session_mock):
            result = _run(
                complete_email_registration_endpoint(
                    CompleteEmailRegistrationRequest(
                        email="User@Test.COM",
                        registration_token="registration-token",
                        password="password1234",
                        display_name="新用户",
                    ),
                    Response(),
                    db,
                )
            )

    assert result["is_new"] is True
    assert result["needs_email_verification"] is False
    added = [call.args[0] for call in db.add.call_args_list]
    from models import User as UserModel

    users = [obj for obj in added if isinstance(obj, UserModel)]
    assert len(users) == 1
    created = users[0]
    assert created.email == "user@test.com"
    assert created.email_verified_at is not None
    assert auth.verify_password("password1234", created.password_hash)
    assert ch.consumed_at is not None
    session_mock.assert_called_once()


def test_complete_registration_wrong_code_increments_attempts_without_consuming():
    ch = _challenge(code="123456")
    db = _make_db()
    db.execute = AsyncMock(return_value=_result_one(ch))

    with pytest.raises(HTTPException) as exc_info:
        _run(
            verify_email_registration_code_endpoint(
                VerifyEmailRegistrationCodeRequest(email="user@test.com", code="000000"),
                db,
            )
        )

    assert exc_info.value.status_code == 400
    assert ch.attempts == 1
    assert ch.consumed_at is None


def test_send_reset_code_issues_fake_email_for_existing_user(monkeypatch):
    monkeypatch.setattr(settings, "email_auth_provider", "fake")
    existing = _user()
    db = _make_db()
    db.execute = AsyncMock(return_value=_result_one(existing))

    result = _run(
        send_email_reset_code_endpoint(
            SendEmailResetCodeRequest(email="User@Test.COM", captcha_token="fake-ok"),
            _make_request(),
            db,
        )
    )

    assert result.ok is True
    added = [call.args[0] for call in db.add.call_args_list]
    from models import EmailVerificationChallenge

    challenges = [obj for obj in added if isinstance(obj, EmailVerificationChallenge)]
    assert len(challenges) == 1
    challenge = challenges[0]
    assert challenge.email == "user@test.com"
    assert challenge.purpose == "password_reset"
    sent_code = auth_email.peek_last_fake_email_code("user@test.com", "password_reset")
    assert sent_code is not None
    assert auth.verify_password(sent_code, challenge.code_hash)


def test_send_reset_code_does_not_create_challenge_for_unknown_email(monkeypatch):
    monkeypatch.setattr(settings, "email_auth_provider", "fake")
    db = _make_db()
    db.execute = AsyncMock(return_value=_result_one(None))

    result = _run(
        send_email_reset_code_endpoint(
            SendEmailResetCodeRequest(email="missing@test.com", captcha_token="fake-ok"),
            _make_request(),
            db,
        )
    )

    assert result.ok is True
    assert db.add.call_args_list == []
    assert auth_email.peek_last_fake_email_code("missing@test.com", "password_reset") is None


def test_reset_password_consumes_code_updates_password_and_logs_in():
    ch = _challenge(code="654321", purpose="password_reset")
    existing = _user(password="oldpassword123")
    db = _make_db()
    db.execute = AsyncMock(side_effect=[_result_one(ch), _result_one(existing)])

    session_mock = AsyncMock(return_value="token")
    with patch("auth_email.create_session", new=session_mock):
        result = _run(
            reset_email_password_endpoint(
                ResetEmailPasswordRequest(
                    email="USER@Test.COM",
                    code="654321",
                    new_password="newpassword123",
                ),
                Response(),
                db,
            )
        )

    assert result["ok"] is True
    assert auth.verify_password("newpassword123", existing.password_hash)
    assert existing.email_verified_at is not None
    assert ch.consumed_at is not None
    session_mock.assert_called_once()
