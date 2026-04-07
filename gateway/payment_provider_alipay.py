"""Alipay payment provider integration boundary (Task 5).

Scope in Task 5:
- Define the real integration surface for `alipay` without requiring merchant
  credentials at import time.
- Expose `AlipayProvider` that satisfies the `PaymentProvider` protocol in
  `payment_providers.py`.
- Gate `operational` behind env-var presence so tests and local dev without
  Alipay credentials continue to run exactly as before (fake path stays green).
- Provide contract-level helpers (`create_checkout`, `verify_signature`,
  `parse_webhook`, `map_status`) that are testable end-to-end without ever
  calling the real Alipay API.

Out of scope in Task 5 (and marked TODO in the code below):
- Building a live `alipay.trade.page.pay` request against Alipay gateway.
- Downloading merchant / Alipay public keys from a key-management service.
- WeChat Pay, auto-renew, mandate lifecycle, refund UX.

Config (env vars):
- `AVT_ALIPAY_APP_ID`               app id registered on open.alipay.com
- `AVT_ALIPAY_APP_PRIVATE_KEY`      merchant's private key, PEM body (or raw)
- `AVT_ALIPAY_PUBLIC_KEY`           Alipay's public key, PEM body (or raw)
- `AVT_ALIPAY_GATEWAY_URL`          defaults to sandbox; override per env
- `AVT_ALIPAY_NOTIFY_URL`           async notify URL reachable from Alipay
- `AVT_ALIPAY_RETURN_URL`           user-facing return URL

All four of `APP_ID`, `APP_PRIVATE_KEY`, `PUBLIC_KEY`, `NOTIFY_URL` are required
for the provider to report `operational = True`. If any is missing, the
provider stays non-operational and its checkout path raises a clean error.

Task 5 deliberately does NOT add these to `gateway/config.py` — `config.py` is
not in the T5 allow-list and env-only gating keeps the change surface minimal.
A later task can migrate these reads into the pydantic settings layer.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from urllib.parse import parse_qsl


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signing readiness flag (T5 minor revision)
# ---------------------------------------------------------------------------
#
# This flag is the single source of truth for whether Alipay can actually
# complete an end-to-end payable + settleable flow. It MUST remain False until
# BOTH of these are genuinely live:
#
#   1. `build_checkout_url()` constructs a real signed `alipay.trade.page.pay`
#      request using the merchant private key, i.e. an URL the user can actually
#      complete on Alipay's side.
#   2. `verify_alipay_signature()` performs real RSA2 verification against the
#      Alipay public key so webhook callbacks can settle orders truthfully.
#
# Env presence alone is NOT sufficient. A partially implemented provider that
# looks operational but can't truthfully collect or settle money is strictly
# worse than no provider — gateway would happily make it the `default_provider`
# via `/api/billing/checkout-config` and the frontend would route real users
# into a broken flow.
#
# When both paths ship for real, flip this to True (and add the env-var gate
# on top in `is_alipay_live_ready()`). Tests can toggle it via monkeypatch to
# exercise the "fully implemented" branch in isolation.
_ALIPAY_LIVE_READY: bool = False


def is_alipay_live_ready() -> bool:
    """Return True iff Alipay is **truly** ready to take real money.

    Combines the code-level readiness flag with env-level config presence.
    Both must agree. This is the helper `AlipayProvider.operational` reads.
    """
    if not _ALIPAY_LIVE_READY:
        return False
    return AlipayConfig.from_env() is not None


# ---------------------------------------------------------------------------
# Config gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlipayConfig:
    app_id: str
    app_private_key: str
    alipay_public_key: str
    notify_url: str
    return_url: str
    gateway_url: str

    @classmethod
    def from_env(cls) -> "AlipayConfig | None":
        """Load config from env. Return None if any required field is missing."""
        app_id = os.environ.get("AVT_ALIPAY_APP_ID", "").strip()
        app_private_key = os.environ.get("AVT_ALIPAY_APP_PRIVATE_KEY", "").strip()
        alipay_public_key = os.environ.get("AVT_ALIPAY_PUBLIC_KEY", "").strip()
        notify_url = os.environ.get("AVT_ALIPAY_NOTIFY_URL", "").strip()
        return_url = os.environ.get("AVT_ALIPAY_RETURN_URL", "").strip()
        gateway_url = os.environ.get(
            "AVT_ALIPAY_GATEWAY_URL",
            "https://openapi-sandbox.dl.alipaydev.com/gateway.do",
        ).strip()

        if not (app_id and app_private_key and alipay_public_key and notify_url):
            return None
        return cls(
            app_id=app_id,
            app_private_key=app_private_key,
            alipay_public_key=alipay_public_key,
            notify_url=notify_url,
            return_url=return_url or notify_url,
            gateway_url=gateway_url,
        )


def is_alipay_configured() -> bool:
    """Light helper for introspection in tests and registry init."""
    return AlipayConfig.from_env() is not None


# ---------------------------------------------------------------------------
# Status mapping — defined even when config is absent, for contract tests.
# ---------------------------------------------------------------------------


# Alipay trade_status → internal order status.
# Mapping is intentionally total for the cases we care about; unknown values
# fall through and get logged by the core settlement code.
_STATUS_MAP: dict[str, str] = {
    "TRADE_SUCCESS": "paid",
    "TRADE_FINISHED": "paid",
    "TRADE_CLOSED": "cancelled",
    "WAIT_BUYER_PAY": "pending",
}


def map_alipay_status(provider_status: str) -> str:
    return _STATUS_MAP.get(provider_status, provider_status)


# ---------------------------------------------------------------------------
# Checkout URL construction
# ---------------------------------------------------------------------------


def build_checkout_url(
    config: AlipayConfig,
    *,
    order_id: str,
    amount_cny: int,
    target_plan_code: str,
    billing_period: str,
) -> str:
    """Return the URL a user should be redirected to in order to pay.

    Task 5 scope: build a deterministic URL shape that points at the configured
    Alipay gateway and includes the out_trade_no (our order_id) as a query
    parameter. This is NOT a signed `alipay.trade.page.pay` request yet —
    that requires the merchant private key signing flow, which we defer to a
    later task when a real SDK or a fully-vetted signing helper is wired in.

    The returned URL is still safe to treat as a "checkout URL" by the frontend:
    it changes per order, contains no secrets, and points at the configured
    Alipay environment. When the real signing path lands, the signature is the
    only thing that changes; the calling contract in `create_checkout` stays
    the same.
    """
    # fen → yuan, 2 decimal places, no trailing zeros stripped (Alipay wants "0.01")
    yuan = f"{amount_cny / 100:.2f}"
    subject = f"AIVideoTrans {target_plan_code.capitalize()} ({billing_period})"
    # TODO(task>5): replace with signed alipay.trade.page.pay request using a
    # real SDK (python-alipay-sdk or equivalent) + merchant private key signing.
    # Parameters included below reflect the subset the settlement path will
    # echo back in the async notify; keeping names in Alipay's canonical form
    # makes the eventual switch mechanical.
    from urllib.parse import urlencode

    params = {
        "app_id": config.app_id,
        "method": "alipay.trade.page.pay",
        "out_trade_no": order_id,
        "total_amount": yuan,
        "subject": subject,
        "product_code": "FAST_INSTANT_TRADE_PAY",
        "notify_url": config.notify_url,
        "return_url": config.return_url,
    }
    return f"{config.gateway_url}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Webhook parsing
# ---------------------------------------------------------------------------


@dataclass
class ParsedAlipayNotify:
    """Result of parsing an Alipay async notify body.

    Mirrors the minimum fields the core settlement code needs, without pulling
    Alipay's full form schema into our type system.
    """
    provider_event_id: str
    order_id: str
    trade_status: str
    raw: dict[str, str]


def parse_alipay_notify(raw_body: bytes) -> ParsedAlipayNotify:
    """Parse an Alipay async notify payload into structured fields.

    Alipay posts x-www-form-urlencoded bodies to the notify URL. For test
    convenience we also accept JSON encoded bodies (tests can use either).

    Raises ValueError on unparseable input or on missing `out_trade_no`.
    """
    if not raw_body:
        raise ValueError("empty alipay notify body")

    body_str = raw_body.decode("utf-8", errors="replace")
    payload: dict[str, str]
    # Accept JSON for test convenience; real Alipay sends form-encoded.
    if body_str.lstrip().startswith("{"):
        try:
            payload = {str(k): str(v) for k, v in json.loads(body_str).items()}
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid alipay notify json: {exc}")
    else:
        payload = dict(parse_qsl(body_str, keep_blank_values=True))

    order_id = payload.get("out_trade_no", "").strip()
    if not order_id:
        raise ValueError("alipay notify missing out_trade_no")

    trade_status = payload.get("trade_status", "").strip()
    # Alipay notify_id is Alipay's own event identifier, used for dedup.
    provider_event_id = payload.get("notify_id", "").strip() or payload.get(
        "trade_no", ""
    ).strip() or f"alipay_evt_{uuid.uuid4().hex[:12]}"

    return ParsedAlipayNotify(
        provider_event_id=provider_event_id,
        order_id=order_id,
        trade_status=trade_status,
        raw=payload,
    )


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def verify_alipay_signature(
    config: AlipayConfig | None,
    raw_body: bytes,
    headers: dict[str, str],
) -> bool:
    """Verify the signature of an incoming Alipay notify.

    Task 5 behavior:

    - If `config` is None (non-operational) → always return False. An
      unverifiable webhook must not be able to settle anything. The core
      settlement code already treats `signature_valid = False` as "record but
      don't settle", so this is the right fail-closed default.
    - If `config` is present but the crypto dependency is not installed or the
      signature material is malformed → return False and log a warning.
    - If `config` is present and the signature matches → return True.

    The real RSA2 verification is intentionally stubbed out here; plugging in
    a live implementation (e.g. `python-alipay-sdk` or `cryptography` directly)
    is a later-task follow-up, not the scope of Task 5. The contract this
    function commits to — fail-closed unless explicitly verified — is what the
    settlement layer relies on.
    """
    if config is None:
        return False

    # TODO(task>5): real RSA2 verification path.
    # 1. Strip `sign` and `sign_type` from the parsed payload.
    # 2. Build canonical `k=v&k=v` string, sorted lexicographically, skipping
    #    empty values and the two stripped fields.
    # 3. RSA2 verify with Alipay public key in `config.alipay_public_key`.
    #
    # Until that path lands, fail closed so an unconfigured-but-present
    # environment never silently accepts an unverified payload.
    logger.warning(
        "alipay signature verification is not yet implemented; failing closed "
        "even though AlipayConfig is present"
    )
    return False
