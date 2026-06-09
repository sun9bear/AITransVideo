"""Paddle Billing helpers (MoR checkout, webhook verify, transaction query).

Mirror of ``payment_provider_alipay.py`` for Paddle. Plan 2026-06-08.

Provider-specific mechanics the gateway uses through ``PaddleProvider``:
- ``create_transaction``  : POST /transactions, return (checkout_url, txn_id)
- ``verify_paddle_signature`` : ``Paddle-Signature`` HMAC-SHA256(secret, ts:raw_body)
                                + timestamp freshness (replay protection)
- ``parse_paddle_webhook``: normalize the webhook envelope
- ``map_paddle_event_type`` / ``map_paddle_transaction_status``
- ``query_transaction``   : GET /transactions/{id} (delayed/lost-webhook fallback)
- ``validate_paddle_webhook_payload`` : 3-gate order-fact check
- ``check_price_drift``   : pull /prices, compare against ``plan_catalog`` truth

Settlement events: ``transaction.completed`` / ``transaction.paid``.

IMPORTANT — refunds in Paddle Billing are modelled as **adjustments**
(``adjustment.created`` with action ``refund``), NOT a ``transaction.refunded``
event (that is Paddle *Classic* wording, which the plan §7.4 predates).

Amount-gate design note (deviates from plan §7.3 "total == amount_cny"):
Paddle is the Merchant of Record and adds/handles tax, so ``grand_total`` can
exceed our list price. We therefore bind a webhook to its order by matching the
line-item ``price_id`` against the expected ``(plan, period) -> price_id`` map
(itself bound to ``plan_catalog`` fen via ``check_price_drift``), plus currency
== CNY. This is stronger and tax-agnostic, avoiding false rejects on tax.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

SANDBOX_BASE = "https://sandbox-api.paddle.com"
PRODUCTION_BASE = "https://api.paddle.com"
DEFAULT_NOTIFY_URL = "https://aitrans.video/api/billing/webhooks/paddle"

# Paddle's SDK enforces a 5s tolerance by default. We widen to 5 min to absorb
# clock skew + delivery/processing delay; the DB unique (provider,event_id)
# index in _process_payment_event is the *primary* replay guard, so a wider
# freshness window here is defense-in-depth, not the only protection.
_DEFAULT_SIGNATURE_MAX_AGE_S = 300

_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}

# (plan_code, billing_period) -> env var holding the Paddle price id (plan §9).
_PRICE_ENV: dict[tuple[str, str], str] = {
    ("plus", "monthly"): "AVT_PADDLE_PRICE_PLUS_M",
    ("plus", "quarterly"): "AVT_PADDLE_PRICE_PLUS_Q",
    ("plus", "annual"): "AVT_PADDLE_PRICE_PLUS_A",
    ("pro", "monthly"): "AVT_PADDLE_PRICE_PRO_M",
    ("pro", "quarterly"): "AVT_PADDLE_PRICE_PRO_Q",
    ("pro", "annual"): "AVT_PADDLE_PRICE_PRO_A",
}

# Webhook event_type -> internal order status (settlement events only settle).
_EVENT_STATUS_MAP: dict[str, str] = {
    "transaction.completed": "paid",
    "transaction.paid": "paid",
    "transaction.payment_failed": "failed",
    "transaction.canceled": "cancelled",
    "adjustment.created": "refunded",
}

# Transaction.status token (from GET /transactions/{id}) -> internal status.
_TXN_STATUS_MAP: dict[str, str] = {
    "completed": "paid",
    "paid": "paid",
    "billed": "paid",
    "canceled": "cancelled",
    "past_due": "pending",
    "ready": "pending",
    "draft": "pending",
}


def _env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_ENV_VALUES


@dataclass(frozen=True)
class PaddleConfig:
    api_key: str
    api_base: str
    webhook_secret: str
    notify_url: str
    price_map: dict[tuple[str, str], str]
    signature_max_age_s: int = _DEFAULT_SIGNATURE_MAX_AGE_S
    client_token: str | None = None

    @classmethod
    def from_env(cls) -> "PaddleConfig | None":
        if not _env_flag("AVT_PADDLE_ENABLED"):
            return None
        api_key = os.environ.get("AVT_PADDLE_API_KEY", "").strip()
        webhook_secret = os.environ.get("AVT_PADDLE_WEBHOOK_SECRET", "").strip()
        if not (api_key and webhook_secret):
            return None
        env = (os.environ.get("AVT_PADDLE_ENV", "sandbox") or "sandbox").strip().lower()
        api_base = PRODUCTION_BASE if env == "production" else SANDBOX_BASE
        notify_url = (
            os.environ.get("AVT_PADDLE_NOTIFY_URL", "").strip() or DEFAULT_NOTIFY_URL
        )
        try:
            max_age = int(os.environ.get("AVT_PADDLE_SIGNATURE_MAX_AGE_S", "").strip())
        except ValueError:
            max_age = _DEFAULT_SIGNATURE_MAX_AGE_S
        return cls(
            api_key=api_key,
            api_base=api_base,
            webhook_secret=webhook_secret,
            notify_url=notify_url,
            price_map=_load_price_map(),
            signature_max_age_s=max_age if max_age > 0 else _DEFAULT_SIGNATURE_MAX_AGE_S,
            client_token=os.environ.get("AVT_PADDLE_CLIENT_TOKEN", "").strip() or None,
        )


def _load_price_map() -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    for key, env_var in _PRICE_ENV.items():
        price_id = os.environ.get(env_var, "").strip()
        if price_id:
            result[key] = price_id
    return result


def is_paddle_enabled() -> bool:
    return _env_flag("AVT_PADDLE_ENABLED")


def is_paddle_live_ready() -> bool:
    """Operational only when enabled AND fully configured (all 6 prices)."""
    config = PaddleConfig.from_env()
    if config is None:
        return False
    return len(config.price_map) == len(_PRICE_ENV)


def expected_price_id(config: PaddleConfig, plan_code: str, billing_period: str) -> str | None:
    return config.price_map.get((plan_code, billing_period))


# --- checkout (sync — billing.create_order calls create_checkout synchronously) ---


def create_transaction(
    config: PaddleConfig,
    *,
    order_id: str,
    target_plan_code: str,
    billing_period: str,
) -> tuple[str, str]:
    """Create a Paddle transaction and return ``(checkout_url, transaction_id)``.

    Raises ``ValueError`` if the price is unmapped or Paddle returns no
    ``checkout.url`` (R11 — default payment link not configured).
    """
    price_id = expected_price_id(config, target_plan_code, billing_period)
    if not price_id:
        raise ValueError(
            f"no Paddle price mapped for {target_plan_code}/{billing_period}"
        )
    # R4: refuse to charge a price that has drifted from plan_catalog (e.g. the
    # amount was changed in the Paddle dashboard) — the settlement-time price_id
    # gate binds the price *id* but not its amount, so verify the amount here.
    # Best-effort: a transient fetch failure must not block checkout (the POST
    # below would fail too if Paddle were truly down).
    try:
        live_price = _fetch_price(config, price_id)
    except Exception:  # pragma: no cover - network blip, fall through to POST
        live_price = None
    if live_price is not None:
        from plan_catalog import get_price

        drift = _price_problems(
            target_plan_code,
            billing_period,
            price_id,
            live_price,
            get_price(target_plan_code, billing_period),
        )
        if drift:
            raise ValueError(
                "paddle price drift; refusing to create checkout: " + "; ".join(drift)
            )
    body = {
        "items": [{"price_id": price_id, "quantity": 1}],
        "custom_data": {"order_id": order_id},
    }
    with httpx.Client(timeout=httpx.Timeout(20.0)) as client:
        response = client.post(
            f"{config.api_base}/transactions",
            headers=_auth_headers(config),
            json=body,
        )
    response.raise_for_status()
    data = (response.json() or {}).get("data") or {}
    txn_id = str(data.get("id") or "").strip()
    checkout_url = str(((data.get("checkout") or {}).get("url")) or "").strip()
    if not txn_id:
        raise ValueError("Paddle transaction create returned no id")
    if not checkout_url:
        # R11: empty checkout.url == default payment link missing/unapproved.
        raise ValueError("Paddle returned empty checkout.url (default payment link?)")
    return checkout_url, txn_id


# --- webhook signature (verify BEFORE parsing) ---


def verify_paddle_signature(
    config: PaddleConfig | None,
    raw_body: bytes,
    headers: dict[str, str],
    *,
    now: float | None = None,
) -> bool:
    if config is None:
        return False
    raw_sig = headers.get("paddle-signature") or headers.get("Paddle-Signature") or ""
    ts, h1 = _parse_signature_header(raw_sig)
    if not ts or not h1:
        logger.warning("paddle webhook missing/malformed Paddle-Signature header")
        return False

    # Freshness (replay protection). ts is unix seconds.
    try:
        ts_int = int(ts)
    except ValueError:
        return False
    current = int(now if now is not None else time.time())
    if abs(current - ts_int) > config.signature_max_age_s:
        logger.warning("paddle webhook timestamp outside tolerance (replay?)")
        return False

    signed_payload = ts.encode("utf-8") + b":" + raw_body
    expected = hmac.new(
        config.webhook_secret.encode("utf-8"), signed_payload, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, h1):
        logger.warning("paddle webhook signature mismatch")
        return False
    return True


def _parse_signature_header(raw: str) -> tuple[str, str]:
    ts = ""
    h1 = ""
    for part in raw.split(";"):
        key, _, value = part.partition("=")
        key = key.strip()
        if key == "ts":
            ts = value.strip()
        elif key == "h1":
            h1 = value.strip()
    return ts, h1


# --- webhook parsing + status mapping ---


@dataclass
class ParsedPaddleWebhook:
    provider_event_id: str
    event_type: str
    order_id: str
    transaction_id: str
    new_status: str
    raw: dict


def parse_paddle_webhook(raw_body: bytes) -> ParsedPaddleWebhook:
    if not raw_body:
        raise ValueError("empty paddle webhook payload")
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid paddle webhook json: {exc}") from exc

    event_id = str(payload.get("event_id") or "").strip()
    event_type = str(payload.get("event_type") or "").strip()
    data = payload.get("data") or {}
    custom = data.get("custom_data") or {}
    return ParsedPaddleWebhook(
        provider_event_id=event_id,
        event_type=event_type,
        order_id=str(custom.get("order_id") or "").strip(),
        transaction_id=str(data.get("id") or "").strip(),
        new_status=map_paddle_event_type(event_type),
        raw=payload,
    )


def map_paddle_event_type(event_type: str) -> str:
    """Webhook event_type -> internal status. Non-settlement -> 'pending'."""
    return _EVENT_STATUS_MAP.get(event_type, "pending")


def map_paddle_transaction_status(status: str) -> str:
    """GET /transactions status token -> internal status."""
    return _TXN_STATUS_MAP.get(status, "pending")


# --- order query (async — billing awaits provider.query_order) ---


@dataclass
class PaddleQueryResult:
    transaction_id: str
    provider_status: str  # raw transaction.status token
    raw_payload: dict


async def query_transaction(
    config: PaddleConfig | None,
    *,
    transaction_id: str,
) -> PaddleQueryResult | None:
    if config is None or not transaction_id:
        return None
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        response = await client.get(
            f"{config.api_base}/transactions/{transaction_id}",
            headers=_auth_headers(config),
        )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    data = (response.json() or {}).get("data") or {}
    status = str(data.get("status") or "").strip()
    if not status:
        return None
    return PaddleQueryResult(
        transaction_id=str(data.get("id") or transaction_id),
        provider_status=status,
        raw_payload=data,
    )


# --- order-fact validation (3 gates; signature only proves "from Paddle") ---


def validate_paddle_webhook_payload(
    config: PaddleConfig | None,
    data: dict | None,
    *,
    order_id: str,
    target_plan_code: str,
    billing_period: str,
    provider_order_id: str | None = None,
) -> None:
    """Raise ``ValueError`` unless the Paddle transaction object matches our order.

    Gates: custom_data.order_id, transaction id (if known), currency == CNY,
    and the line-item price_id == the expected mapped price for this plan/period.
    """
    if config is None:
        raise ValueError("paddle config missing")
    payload = data or {}

    custom = payload.get("custom_data") or {}
    if str(custom.get("order_id") or "").strip() != str(order_id):
        raise ValueError("custom_data.order_id mismatch")

    if provider_order_id and str(payload.get("id") or "").strip() != str(provider_order_id):
        raise ValueError("transaction id mismatch")

    currency = (
        (payload.get("details") or {}).get("totals") or {}
    ).get("currency_code") or payload.get("currency_code")
    if currency != "CNY":
        raise ValueError(f"currency mismatch: {currency}")

    want_price = expected_price_id(config, target_plan_code, billing_period)
    if not want_price:
        raise ValueError(f"no mapped price for {target_plan_code}/{billing_period}")
    if want_price not in _line_item_price_ids(payload):
        raise ValueError("line-item price_id mismatch")


def _line_item_price_ids(transaction: dict) -> set[str]:
    ids: set[str] = set()
    for item in transaction.get("items") or []:
        price = item.get("price") or {}
        pid = price.get("id") or item.get("price_id")
        if pid:
            ids.add(str(pid))
    return ids


# --- price drift guard (R4): Paddle prices must match plan_catalog truth ---


def check_price_drift(config: PaddleConfig | None) -> list[str]:
    """Return a list of drift messages; empty means all 6 prices match.

    Pulls each mapped price by id and asserts currency==CNY, one-time
    (billing_cycle is null), active, and unit amount == plan_catalog fen.
    Truth source is ``plan_catalog.get_price`` (never the env / Paddle).
    """
    if config is None:
        return ["paddle config missing"]
    try:
        from plan_catalog import get_price
    except Exception as exc:  # pragma: no cover - defensive
        return [f"cannot import plan_catalog: {exc}"]

    problems: list[str] = []
    for (plan, period), env_var in _PRICE_ENV.items():
        price_id = config.price_map.get((plan, period))
        if not price_id:
            problems.append(f"{plan}/{period}: {env_var} unset")
            continue
        want_fen = get_price(plan, period)
        try:
            price = _fetch_price(config, price_id)
        except Exception as exc:
            problems.append(f"{plan}/{period} ({price_id}): fetch failed: {exc}")
            continue
        problems.extend(_price_problems(plan, period, price_id, price, want_fen))
    return problems


def _price_problems(
    plan: str, period: str, price_id: str, price: dict | None, want_fen: int | None
) -> list[str]:
    if price is None:
        return [f"{plan}/{period} ({price_id}): not found"]
    out: list[str] = []
    unit = price.get("unit_price") or {}
    if price.get("status") != "active":
        out.append(f"{plan}/{period}: status={price.get('status')}")
    if unit.get("currency_code") != "CNY":
        out.append(f"{plan}/{period}: currency={unit.get('currency_code')}")
    if price.get("billing_cycle") is not None:
        out.append(f"{plan}/{period}: recurring (want one-time)")
    if want_fen is not None and str(unit.get("amount")) != str(want_fen):
        out.append(f"{plan}/{period}: amount={unit.get('amount')} want={want_fen}")
    return out


def _fetch_price(config: PaddleConfig, price_id: str) -> dict | None:
    with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
        response = client.get(
            f"{config.api_base}/prices/{price_id}", headers=_auth_headers(config)
        )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return (response.json() or {}).get("data")


def _auth_headers(config: PaddleConfig) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
