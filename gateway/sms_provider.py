"""SMS delivery adapters for phone-auth verification codes.

Kept intentionally tiny — no registry, no DI, no provider catalog. Task 3 only
ships the fake provider; real providers (Aliyun SMS / Tencent SMS / etc.) are
explicitly out of scope for this milestone. When a real provider ships later,
replace `_send_fake` with an equivalent adapter gated on `settings.sms_provider`.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class SentCode:
    """Result returned by `send_code`. Fields are intentionally minimal."""

    phone_number: str
    code: str
    ttl_seconds: int


# ---------------------------------------------------------------------------
# Fake provider — the only provider Task 3 actually wires up.
# ---------------------------------------------------------------------------
#
# The fake provider logs the generated code at INFO level (so developers can
# copy it from the gateway logs during local preview) and keeps a small
# in-memory record of the last code per phone number. Tests and the phone-auth
# router never read from this record — they read the code from the
# `phone_verification_challenges` DB row — but the record is useful for manual
# debugging and for exposing the last code via a diagnostic helper if needed.


@dataclass
class _FakeProviderState:
    last_codes: dict[str, str] = field(default_factory=dict)


_fake_state = _FakeProviderState()


def generate_code() -> str:
    """Generate a numeric verification code whose length follows settings."""
    length = max(4, min(10, settings.phone_code_length))
    # Uniform numeric code. Uses secrets for non-guessability even though the
    # fake path is development-only.
    digits = [str(secrets.randbelow(10)) for _ in range(length)]
    return "".join(digits)


def _send_fake(phone_number: str, code: str) -> SentCode:
    ttl = settings.phone_code_ttl_seconds
    _fake_state.last_codes[phone_number] = code
    # Use a clearly-labelled log line so reviewers do not mistake the fake path
    # for a real outbound SMS in production logs.
    logger.info(
        "fake-sms: phone=%s code=%s ttl=%ss (do not use in production)",
        phone_number,
        code,
        ttl,
    )
    return SentCode(phone_number=phone_number, code=code, ttl_seconds=ttl)


def _send_aliyun(phone_number: str, code: str) -> SentCode:
    """Send a verification code via Aliyun SMS through an FC relay function.

    The China-mainland SMS API endpoint (dysmsapi.aliyuncs.com) is unreachable
    from non-CN hosted servers. We relay through an Alibaba Cloud Function
    Compute (FC) function deployed in cn-hangzhou, which can reach the domestic
    API over the internal network.

    Architecture:
      US gateway → HTTPS → FC function (cn-hangzhou) → dysmsapi.aliyuncs.com

    The FC function URL and Basic Auth credentials are configured via env vars.
    The actual Aliyun SMS AccessKey / sign name / template are configured inside
    the FC function's own environment, not here.

    Requires env vars:
    - AVT_ALIYUN_SMS_RELAY_URL       FC function public URL
    - AVT_ALIYUN_SMS_RELAY_USER      Basic Auth username for FC trigger
    - AVT_ALIYUN_SMS_RELAY_PASS      Basic Auth password for FC trigger
    """
    import base64
    import json
    import os
    import urllib.request

    relay_url = os.environ.get("AVT_ALIYUN_SMS_RELAY_URL", "").strip()
    relay_user = os.environ.get("AVT_ALIYUN_SMS_RELAY_USER", "").strip()
    relay_pass = os.environ.get("AVT_ALIYUN_SMS_RELAY_PASS", "").strip()

    if not relay_url:
        raise RuntimeError(
            "短信中转服务未配置,请设置 AVT_ALIYUN_SMS_RELAY_URL"
        )

    url = relay_url.rstrip("/") + "/send"
    payload = json.dumps({"phone_number": phone_number, "code": code}).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    if relay_user and relay_pass:
        cred = base64.b64encode(f"{relay_user}:{relay_pass}".encode()).decode()
        req.add_header("Authorization", f"Basic {cred}")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        logger.error("SMS relay HTTP %s: %s", exc.code, body_text)
        raise RuntimeError(f"短信发送失败 (HTTP {exc.code}): {body_text[:300]}")
    except Exception as exc:
        logger.error("SMS relay request failed: %s", exc)
        raise RuntimeError(f"短信发送失败: {exc}")

    if not body.get("ok"):
        error = body.get("error", "unknown")
        logger.error("SMS relay error: %s", error)
        raise RuntimeError(f"短信发送失败: {error}")

    ttl = settings.phone_code_ttl_seconds
    logger.info(
        "aliyun-sms-relay: phone=%s ttl=%ss biz_id=%s",
        phone_number,
        ttl,
        body.get("biz_id", ""),
    )
    return SentCode(phone_number=phone_number, code=code, ttl_seconds=ttl)


def send_code(phone_number: str, code: str) -> SentCode:
    """Dispatch a verification code via the configured SMS provider."""
    provider = (settings.sms_provider or "fake").strip().lower()
    if provider == "fake":
        return _send_fake(phone_number, code)
    if provider == "aliyun":
        return _send_aliyun(phone_number, code)
    raise NotImplementedError(
        f"SMS provider {provider!r} is not wired up. "
        "Supported: fake, aliyun."
    )


def peek_last_fake_code(phone_number: str) -> str | None:
    """Return the last code issued to `phone_number` via the fake provider.

    Only the fake path populates this. Exists for manual debugging and for
    tests that want to bypass the DB challenge table entirely; the phone-auth
    router itself still validates codes against the DB.
    """
    return _fake_state.last_codes.get(phone_number)


def clear_fake_state() -> None:
    """Reset the in-memory fake provider state. Intended for test teardown."""
    _fake_state.last_codes.clear()
