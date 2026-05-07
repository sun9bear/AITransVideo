"""Risk control for phone-auth / trial-grant flows.

Keeps three capabilities intentionally minimal:

1. Phone number normalization (mainland China format).
2. Per-phone / per-IP rate limiting for `POST /auth/phone/send-code`.
3. Virtual-number-segment hook — stubbed so Task 3 does not need to call any
   external risk vendor, but the call site is already in place for a future
   real integration.
4. Fake captcha verification.

No Redis, no registry, no global identity platform. Rate-limit state is
in-process; restarting the gateway resets counters, which is acceptable for
the current milestone because real limits ultimately live at a CDN / WAF tier.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from threading import Lock

from config import settings


# ---------------------------------------------------------------------------
# Phone normalization
# ---------------------------------------------------------------------------

# Matches a valid mainland-CN mobile number after normalization: 11 digits,
# starts with 1, second digit 3-9.
_CN_MOBILE_RE = re.compile(r"^1[3-9]\d{9}$")

# Virtual-number segments (MVNO / OTT) that tend to be used by Trial abuse.
# Kept deliberately small — this is a stub list, not a real fraud feed. A
# production deployment should replace it with a vendor-maintained list.
_VIRTUAL_PREFIXES: frozenset[str] = frozenset(
    {
        "170",  # 虚拟运营商段
        "171",
        "162",
        "165",
        "167",
    }
)


class PhoneNormalizationError(ValueError):
    """Raised when a caller-provided phone number cannot be normalized."""


def normalize_cn_mobile(raw: str) -> str:
    """Normalize a user-typed phone number to the canonical 11-digit form.

    - Strips spaces, hyphens, parentheses.
    - Removes a leading `+86` or `86` country code.
    - Requires exactly 11 digits, starting with `1`, second digit 3-9.

    Raises PhoneNormalizationError on any violation. Error messages are kept
    generic so API responses don't leak which step rejected the input.
    """
    if not raw or not isinstance(raw, str):
        raise PhoneNormalizationError("手机号为空")

    stripped = re.sub(r"[\s\-\(\)]+", "", raw)
    if stripped.startswith("+86"):
        stripped = stripped[3:]
    elif stripped.startswith("86") and len(stripped) == 13:
        stripped = stripped[2:]

    if not _CN_MOBILE_RE.match(stripped):
        raise PhoneNormalizationError("手机号格式不正确")
    return stripped


def is_virtual_segment(phone_number: str) -> bool:
    """Return True if `phone_number` is on the stub virtual-segment block list.

    Callers should treat a True result as soft-deny (Trial-ineligible), not as
    "this number does not exist". Task 3 only wires the hook; a real virtual-
    segment feed is a later-milestone concern.
    """
    if len(phone_number) < 3:
        return False
    return phone_number[:3] in _VIRTUAL_PREFIXES


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


@dataclass
class _RateLimiterState:
    phone_recent: dict[str, deque[float]]
    ip_recent: dict[str, deque[float]]
    lock: Lock


_state = _RateLimiterState(
    phone_recent=defaultdict(deque),
    ip_recent=defaultdict(deque),
    lock=Lock(),
)


class RateLimitExceeded(Exception):
    """Raised when a phone-auth caller trips any configured rate limit."""

    def __init__(self, scope: str, message: str):
        super().__init__(message)
        self.scope = scope
        self.message = message


def _prune(buffer: deque[float], now: float, window_seconds: int) -> None:
    cutoff = now - window_seconds
    while buffer and buffer[0] < cutoff:
        buffer.popleft()


def _count_within(buffer: deque[float], now: float, window_seconds: int) -> int:
    cutoff = now - window_seconds
    return sum(1 for ts in buffer if ts >= cutoff)


def check_send_code_allowed(phone_number: str, client_ip: str | None) -> None:
    """Raise RateLimitExceeded if a fresh `send-code` call should be rejected.

    Applies three independent limits:
      * per-phone short window (prevents double-submits from a jumpy button)
      * per-phone hour window (blunts retry loops)
      * per-IP hour window (blunts bulk-spray from one origin)
    """
    now = time.monotonic()
    with _state.lock:
        phone_buf = _state.phone_recent[phone_number]
        _prune(phone_buf, now, settings.phone_send_code_hour_window_seconds)

        if (
            _count_within(phone_buf, now, settings.phone_send_code_window_seconds)
            >= settings.phone_send_code_max_per_phone_window
        ):
            raise RateLimitExceeded(
                scope="phone_short",
                message="验证码发送过于频繁,请稍后再试",
            )
        if (
            _count_within(phone_buf, now, settings.phone_send_code_hour_window_seconds)
            >= settings.phone_send_code_max_per_phone_hour
        ):
            raise RateLimitExceeded(
                scope="phone_hour",
                message="该手机号今日请求次数过多,请稍后再试",
            )

        if client_ip:
            ip_buf = _state.ip_recent[client_ip]
            _prune(ip_buf, now, settings.phone_send_code_hour_window_seconds)
            if (
                _count_within(
                    ip_buf, now, settings.phone_send_code_hour_window_seconds
                )
                >= settings.phone_send_code_max_per_ip_hour
            ):
                raise RateLimitExceeded(
                    scope="ip_hour",
                    message="请求过于频繁,请稍后再试",
                )


def record_send_code(phone_number: str, client_ip: str | None) -> None:
    """Stamp a successful `send-code` issuance into the rate-limit buffers.

    Callers should invoke this only after `check_send_code_allowed` passed AND
    the SMS adapter has been asked to deliver the code, so a failed adapter
    attempt doesn't count against the user.
    """
    now = time.monotonic()
    with _state.lock:
        _state.phone_recent[phone_number].append(now)
        if client_ip:
            _state.ip_recent[client_ip].append(now)


def reset_rate_limits() -> None:
    """Wipe all in-process rate-limit buffers. Test-support helper only."""
    with _state.lock:
        _state.phone_recent.clear()
        _state.ip_recent.clear()


# ---------------------------------------------------------------------------
# Login rate limit (P1-10a-1, audit 2026-05-07, S-HIGH-3)
# ---------------------------------------------------------------------------
# Two independent buckets:
#   - per-IP:      5 failed logins per IP per minute
#   - per-account: 5 failed logins per account per 15 minutes (catches
#                  distributed brute-force across many IPs targeting one user)
# Successful login does NOT reset — once you trip the limit you wait it out.
# This is intentional: a successful login under heavy attack is suspicious.

_LOGIN_IP_WINDOW = 60          # seconds
_LOGIN_IP_LIMIT = 5
_LOGIN_ACCOUNT_WINDOW = 900    # 15 minutes
_LOGIN_ACCOUNT_LIMIT = 5

_login_ip_failures: dict[str, deque[float]] = defaultdict(deque)
_login_account_failures: dict[str, deque[float]] = defaultdict(deque)


def check_login_allowed(account: str, client_ip: str | None) -> None:
    """Raise RateLimitExceeded when a fresh /auth/login should be rejected.

    Called BEFORE password verification. Records the failure timestamp on
    record_login_failure() AFTER an actual auth failure.
    """
    now = time.monotonic()
    # Per-IP
    if client_ip:
        _prune(_login_ip_failures[client_ip], now, _LOGIN_IP_WINDOW)
        if len(_login_ip_failures[client_ip]) >= _LOGIN_IP_LIMIT:
            raise RateLimitExceeded(
                scope="login_ip",
                message="登录尝试过于频繁,请稍后再试",
            )
    # Per-account
    if account:
        key = account.strip().lower()
        _prune(_login_account_failures[key], now, _LOGIN_ACCOUNT_WINDOW)
        if len(_login_account_failures[key]) >= _LOGIN_ACCOUNT_LIMIT:
            raise RateLimitExceeded(
                scope="login_account",
                message="该账号登录失败次数过多,请稍后再试",
            )


def record_login_failure(account: str, client_ip: str | None) -> None:
    """Stamp a failed /auth/login attempt. Call AFTER detecting auth failure."""
    now = time.monotonic()
    if client_ip:
        _login_ip_failures[client_ip].append(now)
    if account:
        _login_account_failures[account.strip().lower()].append(now)


# ---------------------------------------------------------------------------
# Captcha
# ---------------------------------------------------------------------------

# Tokens that the fake captcha provider treats as "passed". We accept any
# non-empty token by default so local development and Playwright-style end-to-
# end tests do not need a shared secret. Treat any whitespace-only token as a
# failure so empty-string regressions are caught.
_FAKE_CAPTCHA_REJECT_SENTINEL = "fail"


class CaptchaVerificationError(Exception):
    """Raised when captcha verification rejects a token."""


# ---------------------------------------------------------------------------
# IP lifetime Trial grant (frozen rule H1: same IP can grant trial only once)
# ---------------------------------------------------------------------------
#
# Uses a module-level set for in-process dedup + a DB query for durability.
# The DB check reads `phone_verification_challenges` rows with
# `purpose = 'trial_ip_grant'` — we reuse the existing table to avoid a new
# migration. Each successful trial grant records one row with the client IP;
# subsequent attempts from the same IP are denied.
#
# This is intentionally simple. A full fraud system is out of scope.

_ip_trial_granted: set[str] = set()


def check_ip_trial_eligible(client_ip: str | None) -> bool:
    """Return True if this IP has not yet been used to grant a trial.

    Checks the in-process cache first (fast path), then falls back to a
    synchronous flag. The authoritative DB check happens in
    `check_ip_trial_eligible_db` which must be called from an async context.
    For the sync path used by the auth_phone handler, we rely on the in-process
    set plus the DB-backed `record_ip_trial_grant` / `check_ip_trial_eligible_db`.
    """
    if not client_ip:
        return True
    if client_ip in _ip_trial_granted:
        return False
    return True


def record_ip_trial_grant(client_ip: str | None) -> None:
    """Record that this IP has been used to grant a trial (in-process cache)."""
    if client_ip:
        _ip_trial_granted.add(client_ip)


async def check_ip_trial_eligible_db(db, client_ip: str | None) -> bool:
    """Durable DB check: has this IP ever granted a trial?

    Queries `phone_verification_challenges` for a row with
    `purpose = 'trial_ip_grant'` and the given `client_ip`.
    """
    if not client_ip:
        return True
    # Fast path: in-process cache
    if client_ip in _ip_trial_granted:
        return False
    from sqlalchemy import select
    from models import PhoneVerificationChallenge
    result = await db.execute(
        select(PhoneVerificationChallenge).where(
            PhoneVerificationChallenge.purpose == "trial_ip_grant",
            PhoneVerificationChallenge.client_ip == client_ip,
        )
    )
    if result.scalar_one_or_none() is not None:
        _ip_trial_granted.add(client_ip)
        return False
    return True


async def record_ip_trial_grant_db(db, client_ip: str | None) -> None:
    """Durably record an IP trial grant in the DB."""
    if not client_ip:
        return
    _ip_trial_granted.add(client_ip)
    from datetime import datetime, timezone
    from models import PhoneVerificationChallenge
    row = PhoneVerificationChallenge(
        phone_number="__ip_trial__",
        code="",
        client_ip=client_ip,
        purpose="trial_ip_grant",
        expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),  # never expires
        consumed_at=datetime.now(timezone.utc),
    )
    db.add(row)


def reset_ip_trial_grants() -> None:
    """Reset in-process IP trial cache. Test-support only."""
    _ip_trial_granted.clear()


def _geetest_credentials(scenario: str) -> tuple[str, str]:
    normalized = scenario if scenario in {"register", "login"} else "register"
    if normalized == "login":
        captcha_id = settings.geetest_login_captcha_id or settings.geetest_register_captcha_id
        captcha_key = settings.geetest_login_captcha_key or settings.geetest_register_captcha_key
    else:
        captcha_id = settings.geetest_register_captcha_id or settings.geetest_login_captcha_id
        captcha_key = settings.geetest_register_captcha_key or settings.geetest_login_captcha_key
    return captcha_id.strip(), captcha_key.strip()


def _verify_geetest(token: str) -> None:
    """Server-side GeeTest CAPTCHA v4 secondary validation."""
    import hashlib
    import hmac
    import json
    import logging
    import urllib.parse
    import urllib.request

    _log = logging.getLogger(__name__)

    try:
        payload = json.loads(token)
    except json.JSONDecodeError:
        raise CaptchaVerificationError("请重新完成人机验证")

    if not isinstance(payload, dict) or payload.get("provider") != "geetest":
        raise CaptchaVerificationError("请重新完成人机验证")

    scenario = str(payload.get("scenario") or "register")
    captcha_id, captcha_key = _geetest_credentials(scenario)
    if not captcha_id or not captcha_key:
        _log.error("AVT_CAPTCHA_PROVIDER=geetest but GeeTest credentials are missing for scenario=%s.", scenario)
        raise CaptchaVerificationError("人机验证服务配置异常,请稍后重试")

    client_captcha_id = str(payload.get("captcha_id") or "")
    if client_captcha_id and client_captcha_id != captcha_id:
        _log.warning("GeeTest captcha_id mismatch: scenario=%s client=%s expected=%s", scenario, client_captcha_id, captcha_id)
        raise CaptchaVerificationError("人机验证未通过,请重试")

    lot_number = str(payload.get("lot_number") or "")
    captcha_output = str(payload.get("captcha_output") or "")
    pass_token = str(payload.get("pass_token") or "")
    gen_time = str(payload.get("gen_time") or "")
    if not all([lot_number, captcha_output, pass_token, gen_time]):
        raise CaptchaVerificationError("请重新完成人机验证")

    sign_token = hmac.new(
        captcha_key.encode("utf-8"),
        lot_number.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()
    data = urllib.parse.urlencode(
        {
            "lot_number": lot_number,
            "captcha_output": captcha_output,
            "pass_token": pass_token,
            "gen_time": gen_time,
            "sign_token": sign_token,
        }
    ).encode("utf-8")
    url = (
        settings.geetest_api_server.rstrip("/")
        + "/validate?captcha_id="
        + urllib.parse.quote(captcha_id)
    )
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        _log.error("GeeTest verify failed: %s", exc)
        raise CaptchaVerificationError("人机验证服务异常,请重试")

    _log.info("GeeTest response: result=%s reason=%s", result.get("result"), result.get("reason"))
    if result.get("result") != "success":
        _log.warning("GeeTest rejected: reason=%s", result.get("reason"))
        raise CaptchaVerificationError("人机验证未通过,请重试")


def _verify_turnstile(token: str) -> None:
    """Server-side verification via Cloudflare Turnstile.

    A single POST to https://challenges.cloudflare.com/turnstile/v0/siteverify.
    No complex signing, no FC relay needed — works directly from the US server.

    Requires env var:
    - AVT_TURNSTILE_SECRET_KEY     Cloudflare Turnstile secret key
    """
    import json
    import logging
    import os
    import urllib.parse
    import urllib.request

    _log = logging.getLogger(__name__)

    secret_key = os.environ.get("AVT_TURNSTILE_SECRET_KEY", "").strip()
    if not secret_key:
        _log.error("AVT_CAPTCHA_PROVIDER=turnstile but AVT_TURNSTILE_SECRET_KEY is missing.")
        raise CaptchaVerificationError("人机验证服务配置异常,请稍后重试")

    url = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    payload = urllib.parse.urlencode({
        "secret": secret_key,
        "response": token,
    }).encode("utf-8")

    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        _log.error("Turnstile verify failed: %s", exc)
        raise CaptchaVerificationError("人机验证服务异常,请重试")

    _log.info("Turnstile response: success=%s", result.get("success"))

    if not result.get("success"):
        error_codes = result.get("error-codes", [])
        _log.warning("Turnstile rejected: %s", error_codes)
        raise CaptchaVerificationError("人机验证未通过,请重试")


def verify_captcha(token: str | None) -> None:
    """Verify a captcha token. Raises CaptchaVerificationError on rejection.

    Supports three providers:
    - "fake": accepts any non-empty token except "fail" (local dev / tests)
    - "geetest": validates GeeTest CAPTCHA v4 payloads
    - "turnstile": validates Cloudflare Turnstile tokens
    """
    provider = (settings.captcha_provider or "fake").strip().lower()

    if not token or not token.strip():
        raise CaptchaVerificationError("请完成人机验证")

    if provider == "fake":
        if token.strip().lower() == _FAKE_CAPTCHA_REJECT_SENTINEL:
            raise CaptchaVerificationError("人机验证未通过,请重试")
        return

    if provider == "turnstile":
        _verify_turnstile(token.strip())
        return

    if provider == "geetest":
        _verify_geetest(token.strip())
        return

    if provider == "aliyun":
        # Legacy — kept for reference but no longer recommended (cross-border issues)
        _verify_turnstile(token.strip())
        return

    raise NotImplementedError(
        f"Captcha provider {provider!r} is not supported. Use 'fake', 'geetest', or 'turnstile'."
    )
