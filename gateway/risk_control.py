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
# Voice probe rate limit (P2-23, audit 2026-05-07; Codex follow-up)
# ---------------------------------------------------------------------------
# ``/gateway/user-voices/probe`` synthesises a short TTS sample (MiniMax /
# CosyVoice / VolcEngine — all paid by-the-call). Pre-fix the endpoint had
# no rate limit; a logged-in attacker could spam at line-rate.
#
# v0 (Codex review of 2a9c529): the "check before / record after" split
# was vulnerable to concurrent burst — N coroutines all pass the check
# while no record has stamped yet, and all N hit the paid provider before
# anyone ticks the counter. v1 closes the burst hole with a
# **reserve-before-paid-call + refund-on-failure** pattern:
#
#   1. ``reserve_voice_probe`` atomically checks + appends a timestamp.
#      The append happens inside the same lock as the check, so a second
#      concurrent caller observes the just-reserved slot and hits the
#      limit (or doesn't, if there's still capacity).
#   2. The endpoint runs the paid call only AFTER the reservation
#      lands.
#   3. On paid-call failure (provider 502 / empty audio), the endpoint
#      calls ``refund_voice_probe`` to roll back the reservation, so a
#      flaky provider doesn't consume the user's daily quota.
#
# Two windows (unchanged from v0):
#   * 10 calls / 60s per user
#   * 100 calls / 24h per user
#
# Per-user (NOT per-IP) because the endpoint requires auth.

_VOICE_PROBE_SHORT_WINDOW = 60       # seconds
_VOICE_PROBE_SHORT_LIMIT = 10
_VOICE_PROBE_DAY_WINDOW = 86_400     # seconds (24h)
_VOICE_PROBE_DAY_LIMIT = 100

_voice_probe_buf: dict[str, deque[float]] = defaultdict(deque)
_voice_probe_lock: Lock = Lock()


def reserve_voice_probe(user_id: str) -> float:
    """Atomically reserve a probe slot in the per-user window.

    Raises ``RateLimitExceeded`` when either window is at capacity.
    On success, returns the reservation timestamp; pass it to
    ``refund_voice_probe`` to roll back if the paid call fails.

    Concurrency: the check + append happens inside ``_voice_probe_lock``
    so two simultaneous reservers can't both pass the check before
    either has stamped. Without this, an asyncio caller can launch N
    parallel ``await asyncio.to_thread(synth_fn, ...)`` coroutines, all
    of which pass the v0 check (because no record has run yet), all of
    which then hit the paid TTS provider, leaving the rate limit
    bypassed for the burst window.

    Empty user_id is a defensive no-op (returns 0.0, which
    refund_voice_probe ignores). The auth layer should have rejected
    anonymous callers before reaching here.
    """
    if not user_id:
        return 0.0
    now = time.monotonic()
    with _voice_probe_lock:
        buf = _voice_probe_buf[user_id]
        _prune(buf, now, _VOICE_PROBE_DAY_WINDOW)
        if (
            _count_within(buf, now, _VOICE_PROBE_SHORT_WINDOW)
            >= _VOICE_PROBE_SHORT_LIMIT
        ):
            raise RateLimitExceeded(
                scope="voice_probe_short",
                message="试听过于频繁,请稍后再试",
            )
        if len(buf) >= _VOICE_PROBE_DAY_LIMIT:
            raise RateLimitExceeded(
                scope="voice_probe_day",
                message="今日试听次数已达上限,请明日再试",
            )
        buf.append(now)
        return now


def refund_voice_probe(user_id: str, reservation: float) -> None:
    """Remove a previously-reserved slot — call when the paid TTS
    provider returned a 5xx / empty audio so a flaky provider doesn't
    tick down the user's daily quota.

    ``reservation`` is the timestamp returned by ``reserve_voice_probe``.
    We pop the matching value (not "the most recent") so two concurrent
    failures don't refund each other's slots. Already-pruned values
    (window passed before refund could fire) silently no-op.
    """
    if not user_id or reservation <= 0:
        return
    with _voice_probe_lock:
        buf = _voice_probe_buf[user_id]
        try:
            buf.remove(reservation)
        except ValueError:
            # Reservation already pruned by the day-window cutoff or
            # cleared by reset_voice_probe_rate_limits. Defensive no-op.
            pass


def reset_voice_probe_rate_limits() -> None:
    """Test-support helper: wipe all voice-probe rate-limit buffers."""
    with _voice_probe_lock:
        _voice_probe_buf.clear()


# ---------------------------------------------------------------------------
# Voice calibration rate limit (P2 voice CPS auto-calibration plan v4.3 T0-A)
# ---------------------------------------------------------------------------
# ``voice_speed_calibrator.calibrate_voice`` runs 3 paid TTS calls (T1/T2/T3
# standard texts) against MiniMax / CosyVoice / VolcEngine. Pre-fix the
# manual ``POST /gateway/user-voices/{id}/calibrate-speed`` endpoint had
# **no** rate limit (v3 plan claimed there was, codex F1 caught it).
# Auto-calibration after voice clone (T1) and review submit (T2) will
# multiply call frequency, so we need an independent budget BEFORE turning
# either on.
#
# Why a NEW budget instead of reusing reserve_voice_probe (codex v3 F4):
#   - probe is "user listened to a voice sample" — UX budget, 10/min, 100/day
#   - calibration is "system calibrating CPS for accuracy" — maintenance
#     budget. Different intent, different cap. Reusing probe would let the
#     system silently consume the user's listening allowance.
#
# Why 5/min, 30/day per user (decision §7.2):
#   - T1 clone-after enqueues 2 calibrations per clone (turbo + hd both).
#     5/min absorbs a burst of 2 clones/minute without contention.
#   - 30/day absorbs ~15 clones/day per user, well above realistic usage.
#   - Admin batch goes through internal API key, NOT through this user
#     budget — so this cap doesn't bottleneck T3 ops paths.
#   - Tighter than probe's 10/min (probe is cheaper per call: 1 short text
#     vs calibration's 3 standard texts ~30s each).
#
# Reserve / refund semantics (v4.1 codex F-v4.1-7 + v4 F-v4-4):
#   - Caller MUST call reserve BEFORE invoking calibrate_voice.
#   - Refund is ONLY for "no paid call was issued":
#       voice_not_found / unsupported_provider / rate_limited (this raise
#       happens inside reserve, no slot taken — defensive no-op refund OK).
#   - Provider 5xx, synth timeout, ffprobe failure, post-paid DB write
#     failure: DO NOT refund. paid_call_count > 0 means budget is spent.
#     Refunding these would let provider failure storms bypass the budget.
#   - The factory contract is "always returns CalibrationResult never
#     raises", so caller pure-function checks `result.paid_call_count == 0`
#     to decide refund.

_VOICE_CALIBRATION_SHORT_WINDOW = 60        # seconds
# 2026-05-10: bumped 5 → 8 after T0 prod test. User clicking 3 voices in
# quick succession (= 6 reservations: 3 voices × 2 models each) hit cap=5
# on the 6th call, leaving the third voice's HD model un-calibrated.
# 8/min still rejects sustained spam (any user > ~4 voices/min in steady
# state) but absorbs the natural "click 3 voices to test" UX. Day cap
# unchanged at 30 — the per-user "I clicked all my voices once" budget.
_VOICE_CALIBRATION_SHORT_LIMIT = 8
_VOICE_CALIBRATION_DAY_WINDOW = 86_400      # 24h
_VOICE_CALIBRATION_DAY_LIMIT = 30

_voice_calibration_buf: dict[str, deque[float]] = defaultdict(deque)
_voice_calibration_lock: Lock = Lock()


def reserve_voice_calibration(user_id: str) -> float:
    """Atomically reserve a calibration slot in the per-user window.

    Raises ``RateLimitExceeded`` when either the 5/min or 30/day cap is
    reached. On success returns the reservation timestamp; pass to
    ``refund_voice_calibration`` ONLY for ``paid_call_count == 0`` failure
    paths (see module docstring).

    Empty user_id returns 0.0 as a defensive no-op (auth layer should
    have rejected anonymous callers; this prevents an internal coding
    bug from silently disabling the limit).
    """
    if not user_id:
        return 0.0
    now = time.monotonic()
    with _voice_calibration_lock:
        buf = _voice_calibration_buf[user_id]
        _prune(buf, now, _VOICE_CALIBRATION_DAY_WINDOW)
        if (
            _count_within(buf, now, _VOICE_CALIBRATION_SHORT_WINDOW)
            >= _VOICE_CALIBRATION_SHORT_LIMIT
        ):
            raise RateLimitExceeded(
                scope="voice_calibration_short",
                message="校准请求过于频繁,请稍后再试",
            )
        if len(buf) >= _VOICE_CALIBRATION_DAY_LIMIT:
            raise RateLimitExceeded(
                scope="voice_calibration_day",
                message="今日校准次数已达上限,请明日再试",
            )
        buf.append(now)
        return now


def refund_voice_calibration(user_id: str, reservation: float) -> None:
    """Roll back a previously-reserved slot.

    Caller MUST only invoke this when no paid TTS call was issued
    (CalibrationResult.paid_call_count == 0 OR caller never reached the
    factory). For paid_call_count > 0 the budget is correctly spent,
    even if the result was not ok.

    ``reservation`` is the timestamp returned by ``reserve_voice_calibration``.
    Already-pruned values silently no-op — the user simply moved past the
    24h window.
    """
    if not user_id or reservation <= 0:
        return
    with _voice_calibration_lock:
        buf = _voice_calibration_buf[user_id]
        try:
            buf.remove(reservation)
        except ValueError:
            pass


def reset_voice_calibration_rate_limits() -> None:
    """Test-support helper: wipe all voice-calibration rate-limit buffers."""
    with _voice_calibration_lock:
        _voice_calibration_buf.clear()


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
