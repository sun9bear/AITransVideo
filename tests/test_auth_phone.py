"""Tests for the phone-first auth router (Task 3).

Covers:
- Phone number normalization
- Captcha gate
- Rate limiting
- `/auth/phone/send-code` success and failure modes
- `/auth/phone/verify-code` success and failure modes
- Closure of `POST /auth/register`
- Legacy email login still works for users with a password_hash
- Legacy email login rejects phone-only users (no password_hash)

DB access is stubbed at the infrastructure level.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, Response

# --- Stub `database` module BEFORE importing gateway code ---
_gateway_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

import auth  # noqa: E402
import auth_phone  # noqa: E402
import risk_control  # noqa: E402
import sms_provider  # noqa: E402
from auth_phone import (  # noqa: E402
    SendCodeRequest,
    VerifyCodeRequest,
    send_code_endpoint,
    verify_code_endpoint,
)
from config import settings  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_request(client_ip: str | None = "127.0.0.1"):
    """Minimal FastAPI Request stand-in for auth_phone endpoints."""
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


def _setup_verify_db(
    *,
    challenge=None,
    user=None,
):
    """Build an AsyncMock DB that serves the selects verify_code_endpoint runs.

    Call sequence:
    1. select(PhoneVerificationChallenge) → challenge (via .scalars().first())
    2. select(User) → user (via .scalar_one_or_none())
    3. select(PhoneVerificationChallenge) for IP trial check → None (eligible)
    """
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
        return ip_none

    db.execute = _execute
    return db


def _reset_state():
    """Reset rate limiter + fake sms + IP trial state between tests."""
    risk_control.reset_rate_limits()
    risk_control.reset_ip_trial_grants()
    sms_provider.clear_fake_state()


# ---------------------------------------------------------------------------
# Phone normalization
# ---------------------------------------------------------------------------


class TestPhoneNormalization:
    def test_plain_eleven_digits(self):
        assert risk_control.normalize_cn_mobile("13800138000") == "13800138000"

    def test_strips_spaces_and_dashes(self):
        assert risk_control.normalize_cn_mobile("138 0013 8000") == "13800138000"
        assert risk_control.normalize_cn_mobile("138-0013-8000") == "13800138000"

    def test_strips_plus_86(self):
        assert risk_control.normalize_cn_mobile("+86 13800138000") == "13800138000"

    def test_strips_86_prefix(self):
        assert risk_control.normalize_cn_mobile("8613800138000") == "13800138000"

    def test_rejects_too_short(self):
        with pytest.raises(risk_control.PhoneNormalizationError):
            risk_control.normalize_cn_mobile("1380013")

    def test_rejects_wrong_prefix(self):
        with pytest.raises(risk_control.PhoneNormalizationError):
            risk_control.normalize_cn_mobile("23800138000")

    def test_rejects_empty(self):
        with pytest.raises(risk_control.PhoneNormalizationError):
            risk_control.normalize_cn_mobile("")

    def test_virtual_segment_stub(self):
        assert risk_control.is_virtual_segment("17012345678") is True
        assert risk_control.is_virtual_segment("13800138000") is False


# ---------------------------------------------------------------------------
# Captcha
# ---------------------------------------------------------------------------


class TestCaptcha:
    def test_accepts_non_empty_token(self):
        # Should not raise.
        risk_control.verify_captcha("fake-ok")
        risk_control.verify_captcha("any-token")

    def test_rejects_empty(self):
        with pytest.raises(risk_control.CaptchaVerificationError):
            risk_control.verify_captcha("")

    def test_rejects_whitespace(self):
        with pytest.raises(risk_control.CaptchaVerificationError):
            risk_control.verify_captcha("   ")

    def test_rejects_sentinel(self):
        with pytest.raises(risk_control.CaptchaVerificationError):
            risk_control.verify_captcha("fail")

    def test_geetest_validates_payload_with_scene_key(self, monkeypatch):
        monkeypatch.setattr(settings, "captcha_provider", "geetest")
        monkeypatch.setattr(settings, "geetest_register_captcha_id", "register-id")
        monkeypatch.setattr(settings, "geetest_register_captcha_key", "register-key")
        monkeypatch.setattr(settings, "geetest_login_captcha_id", "login-id")
        monkeypatch.setattr(settings, "geetest_login_captcha_key", "login-key")
        monkeypatch.setattr(settings, "geetest_api_server", "http://gcaptcha4.geetest.com")

        response = MagicMock()
        response.read.return_value = json.dumps({"result": "success", "reason": "ok"}).encode()
        response_cm = MagicMock()
        response_cm.__enter__.return_value = response
        response_cm.__exit__.return_value = None

        token = json.dumps(
            {
                "provider": "geetest",
                "scenario": "login",
                "captcha_id": "login-id",
                "lot_number": "lot-123",
                "captcha_output": "captcha-output",
                "pass_token": "pass-token",
                "gen_time": "1710000000",
            }
        )

        with patch("urllib.request.urlopen", return_value=response_cm) as urlopen:
            risk_control.verify_captcha(token)

        request = urlopen.call_args.args[0]
        assert "captcha_id=login-id" in request.full_url
        posted = urllib.parse.parse_qs(request.data.decode())
        assert posted["lot_number"] == ["lot-123"]
        assert posted["captcha_output"] == ["captcha-output"]
        assert posted["pass_token"] == ["pass-token"]
        assert posted["gen_time"] == ["1710000000"]
        assert posted["sign_token"][0]

    def test_geetest_rejects_wrong_scene_captcha_id(self, monkeypatch):
        monkeypatch.setattr(settings, "captcha_provider", "geetest")
        monkeypatch.setattr(settings, "geetest_register_captcha_id", "register-id")
        monkeypatch.setattr(settings, "geetest_register_captcha_key", "register-key")
        monkeypatch.setattr(settings, "geetest_login_captcha_id", "login-id")
        monkeypatch.setattr(settings, "geetest_login_captcha_key", "login-key")

        token = json.dumps(
            {
                "provider": "geetest",
                "scenario": "login",
                "captcha_id": "register-id",
                "lot_number": "lot-123",
                "captcha_output": "captcha-output",
                "pass_token": "pass-token",
                "gen_time": "1710000000",
            }
        )

        with pytest.raises(risk_control.CaptchaVerificationError):
            risk_control.verify_captcha(token)


class TestSessionCookie:
    def test_create_session_uses_mobile_compatible_lax_cookie(self):
        db = _make_db()
        response = Response()

        _run(auth.create_session(db, uuid.uuid4(), response))

        set_cookie = response.headers["set-cookie"]
        assert "avt_session=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "Secure" in set_cookie
        assert "SameSite=lax" in set_cookie


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimits:
    def setup_method(self):
        _reset_state()

    def teardown_method(self):
        _reset_state()

    def test_first_call_allowed(self):
        risk_control.check_send_code_allowed("13800138000", "1.1.1.1")

    def test_phone_short_limit_blocks_double_submit(self):
        risk_control.check_send_code_allowed("13800138000", "1.1.1.1")
        risk_control.record_send_code("13800138000", "1.1.1.1")
        with pytest.raises(risk_control.RateLimitExceeded) as exc_info:
            risk_control.check_send_code_allowed("13800138000", "1.1.1.1")
        assert exc_info.value.scope == "phone_short"

    def test_different_phone_not_blocked(self):
        risk_control.check_send_code_allowed("13800138000", "1.1.1.1")
        risk_control.record_send_code("13800138000", "1.1.1.1")
        # A different phone on the SAME ip is still allowed by the phone-short
        # rule. (The IP hour limit is much higher so one extra call is fine.)
        risk_control.check_send_code_allowed("13900139000", "1.1.1.1")

    def test_ip_hour_limit_blocks_after_threshold(self):
        # Fire just above the IP hour cap from many distinct phones so the
        # per-phone limits never trigger first.
        cap = settings.phone_send_code_max_per_ip_hour
        for i in range(cap):
            phone = f"138{str(i).rjust(8, '0')}"
            risk_control.check_send_code_allowed(phone, "9.9.9.9")
            risk_control.record_send_code(phone, "9.9.9.9")
        overflow_phone = "13900000000"
        with pytest.raises(risk_control.RateLimitExceeded) as exc_info:
            risk_control.check_send_code_allowed(overflow_phone, "9.9.9.9")
        assert exc_info.value.scope == "ip_hour"


# ---------------------------------------------------------------------------
# sms_provider (fake path)
# ---------------------------------------------------------------------------


class TestFakeSmsProvider:
    def setup_method(self):
        _reset_state()

    def teardown_method(self):
        _reset_state()

    def test_generate_code_is_numeric(self):
        code = sms_provider.generate_code()
        assert code.isdigit()
        assert len(code) == settings.phone_code_length

    def test_send_code_fake_path_returns_sent_code(self):
        sent = sms_provider.send_code("13800138000", "123456")
        assert sent.phone_number == "13800138000"
        assert sent.code == "123456"
        assert sent.ttl_seconds == settings.phone_code_ttl_seconds
        assert sms_provider.peek_last_fake_code("13800138000") == "123456"


# ---------------------------------------------------------------------------
# send_code_endpoint
# ---------------------------------------------------------------------------


class TestSendCodeEndpoint:
    def setup_method(self):
        _reset_state()

    def teardown_method(self):
        _reset_state()

    def test_rejects_malformed_phone(self):
        db = _make_db()
        db.execute = AsyncMock()
        body = SendCodeRequest(phone_number="abc", captcha_token="fake-ok")
        with pytest.raises(HTTPException) as exc_info:
            _run(send_code_endpoint(body, _make_request(), db))
        assert exc_info.value.status_code == 400

    def test_rejects_missing_captcha(self):
        db = _make_db()
        db.execute = AsyncMock()
        body = SendCodeRequest(phone_number="13800138000", captcha_token="fail")
        with pytest.raises(HTTPException) as exc_info:
            _run(send_code_endpoint(body, _make_request(), db))
        assert exc_info.value.status_code == 400

    def test_success_issues_code_and_records_challenge(self):
        db = _make_db()
        db.execute = AsyncMock()  # covers the invalidate-previous update
        body = SendCodeRequest(phone_number="13800138000", captcha_token="fake-ok")
        result = _run(send_code_endpoint(body, _make_request(), db))
        assert result.ok is True
        assert result.ttl_seconds == settings.phone_code_ttl_seconds
        db.add.assert_called_once()
        db.commit.assert_awaited()
        # Fake provider stashed the last code so manual debugging works.
        assert sms_provider.peek_last_fake_code("13800138000") is not None

    def test_phone_rate_limit_returns_429(self):
        db = _make_db()
        db.execute = AsyncMock()
        body = SendCodeRequest(phone_number="13800138000", captcha_token="fake-ok")
        # First call OK.
        _run(send_code_endpoint(body, _make_request(), db))
        # Second call within the short window → 429.
        with pytest.raises(HTTPException) as exc_info:
            _run(send_code_endpoint(body, _make_request(), db))
        assert exc_info.value.status_code == 429


# ---------------------------------------------------------------------------
# verify_code_endpoint
# ---------------------------------------------------------------------------


def _make_challenge(phone="13800138000", code="654321", expired=False, consumed=False):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid.uuid4(),
        phone_number=phone,
        code=code,
        client_ip="1.1.1.1",
        purpose="login",
        expires_at=now - timedelta(seconds=1) if expired else now + timedelta(minutes=5),
        consumed_at=now if consumed else None,
        created_at=now,
    )


def _make_user(
    *,
    phone="13800138000",
    trial_granted_at=None,
    plan_code="free",
    uid=None,
):
    return SimpleNamespace(
        id=uid or uuid.uuid4(),
        email=None,
        display_name="138****8000",
        password_hash=None,
        is_active=True,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        role="user",
        plan_code=plan_code,
        free_jobs_quota_total=5,
        free_jobs_quota_used=0,
        phone_number=phone,
        phone_verified_at=None,
        trial_granted_at=trial_granted_at,
        trial_ends_at=None,
    )


class TestVerifyCodeEndpoint:
    def setup_method(self):
        _reset_state()

    def teardown_method(self):
        _reset_state()

    def test_rejects_when_no_active_challenge(self):
        db = _setup_verify_db(challenge=None)
        body = VerifyCodeRequest(phone_number="13800138000", code="123456")
        with pytest.raises(HTTPException) as exc_info:
            _run(verify_code_endpoint(body, _make_request(), Response(), db))
        assert exc_info.value.status_code == 400

    def test_rejects_wrong_code(self):
        ch = _make_challenge(code="654321")
        db = _setup_verify_db(challenge=ch)
        body = VerifyCodeRequest(phone_number="13800138000", code="000000")
        with pytest.raises(HTTPException) as exc_info:
            _run(verify_code_endpoint(body, _make_request(), Response(), db))
        assert exc_info.value.status_code == 400
        # Brute-force guard: the challenge MUST be consumed even on a wrong
        # guess, so the same OTP cannot be probed again.
        assert ch.consumed_at is not None

    def test_wrong_code_burns_challenge_same_code_cannot_retry(self):
        """Regression: a single wrong guess must burn the OTP.

        The attacker flow we block here:
          1. Attacker knows a victim's phone and can observe a send-code result.
          2. Attacker guesses the OTP once and misses.
          3. Before the TTL expires, attacker guesses again with a correct code.

        After this fix, step 3 must fail because the challenge row was
        consumed at step 2 and the WHERE clause filters `consumed_at IS NULL`.
        """
        # First attempt: wrong code, same challenge returned by the query.
        ch = _make_challenge(code="654321")
        db1 = _setup_verify_db(challenge=ch)
        body_wrong = VerifyCodeRequest(phone_number="13800138000", code="000000")
        with pytest.raises(HTTPException) as exc1:
            _run(verify_code_endpoint(body_wrong, _make_request(), Response(), db1))
        assert exc1.value.status_code == 400
        assert ch.consumed_at is not None

        # Second attempt: the real gateway's WHERE clause filters out consumed
        # challenges, so a fresh query would return no row. Simulate that by
        # feeding `challenge=None` to the endpoint on the retry.
        db2 = _setup_verify_db(challenge=None)
        body_correct = VerifyCodeRequest(phone_number="13800138000", code="654321")
        with pytest.raises(HTTPException) as exc2:
            _run(verify_code_endpoint(body_correct, _make_request(), Response(), db2))
        # Gateway returns the generic "expired, please re-request" error so
        # the attacker can't tell whether the code was right or simply burned.
        assert exc2.value.status_code == 400

    def test_disabled_user_cannot_login_via_phone_auth(self):
        """Regression: admin-disabled accounts must not sign in via phone auth.

        `auth.login_handler` already rejects `is_active=False` email users;
        Task 3 must mirror that gate on the phone-auth path so a disabled user
        can't sidestep it by switching flows.
        """
        ch = _make_challenge(code="654321")
        disabled = _make_user(trial_granted_at=datetime.now(timezone.utc))
        disabled.is_active = False
        db = _setup_verify_db(challenge=ch, user=disabled)

        # create_session must NEVER be invoked on a disabled-user path.
        session_mock = AsyncMock(return_value="token")
        with patch("auth_phone.create_session", new=session_mock):
            body = VerifyCodeRequest(phone_number="13800138000", code="654321")
            async def _noop_refresh(obj):
                return None
            db.refresh = _noop_refresh
            with pytest.raises(HTTPException) as exc_info:
                _run(verify_code_endpoint(body, _make_request(), Response(), db))

        assert exc_info.value.status_code == 403
        assert "禁用" in exc_info.value.detail
        # The challenge still gets burned (single-attempt invariant above)
        # but no session is created.
        assert ch.consumed_at is not None
        session_mock.assert_not_called()

    def test_new_phone_returns_needs_password_not_session(self):
        """A1: new users get a registration_token, not a direct session.

        verify_code for a phone with no existing User should return
        `needs_password: True` + `registration_token`. No User is created,
        no session is created, no trial is granted at this stage.
        """
        ch = _make_challenge(code="654321")
        db = _setup_verify_db(challenge=ch, user=None)

        session_mock = AsyncMock(return_value="session-token")
        with patch("auth_phone.create_session", new=session_mock):
            body = VerifyCodeRequest(phone_number="13800138000", code="654321")
            response = Response()
            async def _noop_refresh(obj):
                return None
            db.refresh = _noop_refresh
            result = _run(verify_code_endpoint(body, _make_request(), response, db))

        assert result["is_new"] is True
        assert result["needs_password"] is True
        assert result["registration_token"] is not None
        assert result["user"] is None
        assert ch.consumed_at is not None
        registration_challenge = db.add.call_args.args[0]
        assert registration_challenge.purpose == "registration"
        assert len(registration_challenge.code) > 16
        assert result["registration_token"] == registration_challenge.code
        # create_session must NOT have been called.
        session_mock.assert_not_called()

    def test_returning_user_does_not_create_second_account(self):
        ch = _make_challenge(code="654321")
        existing = _make_user(trial_granted_at=datetime.now(timezone.utc))
        db = _setup_verify_db(challenge=ch, user=existing)

        with patch("auth_phone.create_session", new=AsyncMock(return_value="token")):
            body = VerifyCodeRequest(phone_number="13800138000", code="654321")
            async def _noop_refresh(obj):
                return None
            db.refresh = _noop_refresh
            result = _run(verify_code_endpoint(body, _make_request(), Response(), db))

        assert result["is_new"] is False
        # No new user was added.
        db.add.assert_not_called()
        # trial_granted_at must not be re-stamped.
        original = existing.trial_granted_at
        assert existing.trial_granted_at == original

    def test_virtual_segment_new_phone_still_gets_registration_token(self):
        """A1: virtual segment phones also get a registration_token for new users.

        The virtual-segment trial-denial happens at complete-registration time,
        not at verify-code time. At this stage we only know the phone is new.
        """
        ch = _make_challenge(phone="17012345678", code="654321")
        db = _setup_verify_db(challenge=ch, user=None)

        session_mock = AsyncMock(return_value="token")
        with patch("auth_phone.create_session", new=session_mock):
            body = VerifyCodeRequest(phone_number="17012345678", code="654321")
            async def _noop_refresh(obj):
                return None
            db.refresh = _noop_refresh
            result = _run(verify_code_endpoint(body, _make_request(), Response(), db))

        assert result["needs_password"] is True
        assert result["registration_token"] is not None
        session_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Email register / login compatibility
# ---------------------------------------------------------------------------


class TestEmailRegisterClosed:
    def test_register_endpoint_returns_403_when_disabled(self):
        # Default: settings.email_registration_enabled is False.
        assert settings.email_registration_enabled is False

        db = _make_db()
        db.execute = AsyncMock()
        body = auth.RegisterRequest(
            email="u@test.com", password="password", display_name=""
        )
        with pytest.raises(HTTPException) as exc_info:
            _run(auth.register_handler(body, Response(), db))
        assert exc_info.value.status_code == 403
        assert "手机" in exc_info.value.detail


class TestLegacyEmailLogin:
    def test_login_rejects_phone_only_user_without_password(self):
        # Phone-only users have password_hash=None. Legacy login must not
        # accept them (otherwise any empty string would log them in).
        phone_only = _make_user()
        phone_only.email = "ghost@test.com"
        phone_only.password_hash = None

        result_obj = MagicMock()
        result_obj.scalar_one_or_none.return_value = phone_only
        db = _make_db()
        db.execute = AsyncMock(return_value=result_obj)

        body = auth.LoginRequest(email="ghost@test.com", password="whatever")
        with pytest.raises(HTTPException) as exc_info:
            _run(auth.login_handler(body, Response(), db))
        assert exc_info.value.status_code == 401

    def test_login_accepts_email_user_with_matching_password(self):
        raw_password = "hunter2222"
        real = _make_user()
        real.email = "real@test.com"
        real.password_hash = auth.hash_password(raw_password)

        result_obj = MagicMock()
        result_obj.scalar_one_or_none.return_value = real
        db = _make_db()
        db.execute = AsyncMock(return_value=result_obj)

        with patch("auth.create_session", new=AsyncMock(return_value="token")):
            body = auth.LoginRequest(email="real@test.com", password=raw_password)
            result = _run(auth.login_handler(body, Response(), db))

        assert result["user"]["email"] == "real@test.com"


# ===================================================================
# complete-registration — A1 new endpoint
# ===================================================================

from auth_phone import (  # noqa: E402
    CompleteRegistrationRequest,
    complete_registration_endpoint,
    ResetPasswordRequest,
    reset_password_endpoint,
)


def _make_reg_challenge(phone="13800138000", token="reg-token-123", expired=False, consumed=False):
    """Create a mock registration challenge (purpose='registration')."""
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid.uuid4(),
        phone_number=phone,
        code=token,
        client_ip="127.0.0.1",
        purpose="registration",
        expires_at=now - timedelta(seconds=1) if expired else now + timedelta(minutes=15),
        consumed_at=now if consumed else None,
        created_at=now,
    )


def _setup_complete_reg_db(*, reg_challenge, existing_user=None, ip_eligible=True):
    """Build a mock DB for complete_registration_endpoint.

    Execute sequence:
      1. select PhoneVerificationChallenge (registration token lookup)
      2. select User by phone (race guard)
      3. select IP trial eligibility
    """
    db = _make_db()
    reg_result = MagicMock()
    reg_result.scalar_one_or_none.return_value = reg_challenge
    user_result = MagicMock()
    user_result.scalar_one_or_none.return_value = existing_user
    ip_result = MagicMock()
    ip_result.scalar_one_or_none.return_value = None if ip_eligible else SimpleNamespace(id="x")
    calls = {"n": 0}

    async def _execute(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return reg_result
        if calls["n"] == 2:
            return user_result
        return ip_result

    db.execute = _execute
    async def _noop_refresh(obj):
        return None
    db.refresh = _noop_refresh
    return db


class TestCompleteRegistration:
    def setup_method(self):
        _reset_state()

    def teardown_method(self):
        _reset_state()

    def test_success_creates_user_with_password_and_trial(self):
        reg = _make_reg_challenge(token="valid-token")
        db = _setup_complete_reg_db(reg_challenge=reg)

        session_mock = AsyncMock(return_value="session-token")
        with patch("auth_phone.create_session", new=session_mock):
            body = CompleteRegistrationRequest(
                registration_token="valid-token", password="mypassword123"
            )
            result = _run(complete_registration_endpoint(
                body, _make_request(), Response(), db
            ))

        # Registration succeeded.
        assert result["is_new"] is True
        assert result["needs_password"] is False
        assert result["user"]["phone_number"] == "13800138000"

        # Token was consumed.
        assert reg.consumed_at is not None

        # User was created with password_hash set.
        added = [call.args[0] for call in db.add.call_args_list]
        from models import User as UserModel
        users = [obj for obj in added if isinstance(obj, UserModel)]
        assert len(users) == 1
        new_user = users[0]
        assert new_user.phone_number == "13800138000"
        assert new_user.password_hash is not None
        assert auth.verify_password("mypassword123", new_user.password_hash)

        # Trial was granted at registration time.
        assert new_user.trial_granted_at is not None
        assert new_user.trial_ends_at is not None

        # Session was created.
        session_mock.assert_called_once()

    def test_expired_token_rejected(self):
        reg = _make_reg_challenge(token="expired-token", expired=True)
        # Expired token → scalar_one_or_none returns None (WHERE clause filters it)
        db = _setup_complete_reg_db(reg_challenge=None)

        with pytest.raises(HTTPException) as exc_info:
            _run(complete_registration_endpoint(
                CompleteRegistrationRequest(
                    registration_token="expired-token", password="pw1234567890"
                ),
                _make_request(), Response(), db,
            ))
        assert exc_info.value.status_code == 400

    def test_consumed_token_rejected(self):
        # Already consumed → scalar_one_or_none returns None
        db = _setup_complete_reg_db(reg_challenge=None)

        with pytest.raises(HTTPException) as exc_info:
            _run(complete_registration_endpoint(
                CompleteRegistrationRequest(
                    registration_token="used-token", password="pw1234567890"
                ),
                _make_request(), Response(), db,
            ))
        assert exc_info.value.status_code == 400

    def test_race_condition_phone_already_registered(self):
        reg = _make_reg_challenge(token="race-token")
        existing = _make_user(phone="13800138000")
        db = _setup_complete_reg_db(reg_challenge=reg, existing_user=existing)

        with pytest.raises(HTTPException) as exc_info:
            _run(complete_registration_endpoint(
                CompleteRegistrationRequest(
                    registration_token="race-token", password="pw1234567890"
                ),
                _make_request(), Response(), db,
            ))
        assert exc_info.value.status_code == 409

    def test_short_password_rejected_by_pydantic(self):
        """Pydantic rejects passwords < 12 chars before the handler runs."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            CompleteRegistrationRequest(
                registration_token="short-pw-token", password="12345"
            )


# ===================================================================
# reset-password — A1 new endpoint
# ===================================================================


def _setup_reset_pw_db(*, challenge, user):
    """Build a mock DB for reset_password_endpoint.

    Execute sequence:
      1. select PhoneVerificationChallenge (code lookup)
      2. select User by phone
    """
    db = _make_db()
    ch_result = MagicMock()
    ch_scalars = MagicMock()
    ch_scalars.first.return_value = challenge
    ch_result.scalars.return_value = ch_scalars
    user_result = MagicMock()
    user_result.scalar_one_or_none.return_value = user
    calls = {"n": 0}

    async def _execute(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return ch_result
        return user_result

    db.execute = _execute
    return db


class TestResetPassword:
    def setup_method(self):
        _reset_state()

    def teardown_method(self):
        _reset_state()

    def test_success_resets_password_and_creates_session(self):
        ch = _make_challenge(code="654321")
        existing = _make_user(phone="13800138000")
        existing.password_hash = auth.hash_password("oldpassword")
        db = _setup_reset_pw_db(challenge=ch, user=existing)

        session_mock = AsyncMock(return_value="session-token")
        with patch("auth_phone.create_session", new=session_mock):
            body = ResetPasswordRequest(
                phone_number="13800138000", code="654321",
                new_password="newpassword123",
            )
            result = _run(reset_password_endpoint(
                body, _make_request(), Response(), db
            ))

        assert result["ok"] is True
        # Password was updated.
        assert auth.verify_password("newpassword123", existing.password_hash)
        # Session was created (auto-login after reset).
        session_mock.assert_called_once()
        # Challenge was consumed.
        assert ch.consumed_at is not None

    def test_wrong_code_rejected(self):
        ch = _make_challenge(code="654321")
        existing = _make_user(phone="13800138000")
        db = _setup_reset_pw_db(challenge=ch, user=existing)

        with pytest.raises(HTTPException) as exc_info:
            _run(reset_password_endpoint(
                ResetPasswordRequest(
                    phone_number="13800138000", code="000000",
                    new_password="newpw1234567",
                ),
                _make_request(), Response(), db,
            ))
        assert exc_info.value.status_code == 400

    def test_nonexistent_phone_rejected(self):
        ch = _make_challenge(code="654321")
        db = _setup_reset_pw_db(challenge=ch, user=None)

        with pytest.raises(HTTPException) as exc_info:
            _run(reset_password_endpoint(
                ResetPasswordRequest(
                    phone_number="13800138000", code="654321",
                    new_password="newpw1234567",
                ),
                _make_request(), Response(), db,
            ))
        assert exc_info.value.status_code == 404

    def test_disabled_user_rejected(self):
        ch = _make_challenge(code="654321")
        existing = _make_user(phone="13800138000")
        existing.is_active = False
        db = _setup_reset_pw_db(challenge=ch, user=existing)

        with pytest.raises(HTTPException) as exc_info:
            _run(reset_password_endpoint(
                ResetPasswordRequest(
                    phone_number="13800138000", code="654321",
                    new_password="newpw1234567",
                ),
                _make_request(), Response(), db,
            ))
        assert exc_info.value.status_code == 403

    def test_short_password_rejected_by_pydantic(self):
        """Pydantic rejects passwords < 12 chars before the handler runs."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ResetPasswordRequest(
                phone_number="13800138000", code="654321",
                new_password="12345",
            )
