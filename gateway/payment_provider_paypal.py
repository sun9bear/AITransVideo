"""PayPal Orders v2 helpers (server-side redirect checkout, capture, webhook verify).

Mirror of ``payment_provider_paddle.py`` for PayPal. Plan 2026-06-26.

PayPal is the overseas / PayPal-wallet lane. Unlike WeChat/Alipay/Paddle it is
denominated in **USD** (option c independent USD list price), so the settlement
fact-gate is anchored to USD, never to ``amount_cny``.

Provider-specific mechanics the gateway uses through ``PayPalProvider``:
- ``create_order``    : POST /v2/checkout/orders (intent=CAPTURE) → (payer-action
                        url, paypal_order_id, expected_usd_cents). The buyer is
                        redirected to the ``payer-action`` link.
- ``capture_order``   : POST /v2/checkout/orders/{id}/capture (PayPal-Request-Id
                        idempotency) → capture object.
- ``verify_paypal_signature`` : online POST /v1/notifications/verify-webhook-signature
                        (fail-closed on any non-SUCCESS / error).
- ``parse_paypal_webhook`` : normalize the webhook envelope.
- ``map_paypal_event_type`` / ``map_paypal_order_status``.
- ``query_order``     : GET /v2/checkout/orders/{id} (read-only; APPROVED→capture
                        orchestration lives in billing, NOT here — plan §7.4/S2).
- ``validate_paypal_webhook_payload`` : USD-anchored fact gate (custom_id, currency
                        == USD, captured == per-order snapshot expected_usd_cents).

IMPORTANT — settlement idempotency: the settling ``provider_event_id`` is the
PayPal **capture id** (``resource.id`` on a CAPTURE event / the capture object's
id on the capture response). Both the synchronous return-path capture and the
later PAYMENT.CAPTURE.COMPLETED webhook collapse to the same capture id, so the
``(provider, provider_event_id)`` unique index dedupes them at the event level
(the order-row FOR UPDATE lock is defense-in-depth, plan §17 G-finding).

Settlement events: PAYMENT.CAPTURE.COMPLETED. Refund/chargeback semantics:
PAYMENT.CAPTURE.REFUNDED and PAYMENT.CAPTURE.REVERSED (both → ``refunded``;
REVERSED is the chargeback path — dropping it would let a disputed buyer keep
the plan, plan §17 S4).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import httpx

logger = logging.getLogger(__name__)

SANDBOX_BASE = "https://api-m.sandbox.paypal.com"
PRODUCTION_BASE = "https://api-m.paypal.com"
DEFAULT_RETURN_URL = "https://aitrans.video/api/billing/paypal/return"
DEFAULT_CANCEL_URL = "https://aitrans.video/settings/billing?provider=paypal&status=cancelled"

_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}

# Refresh the OAuth token this many seconds before its stated expiry, so an
# in-flight request never races the boundary.
_TOKEN_SAFETY_MARGIN_S = 60

# Webhook event_type -> internal order status (settlement events only settle).
_EVENT_STATUS_MAP: dict[str, str] = {
    "PAYMENT.CAPTURE.COMPLETED": "paid",
    "PAYMENT.CAPTURE.REFUNDED": "refunded",
    # REVERSED = chargeback/reversal: funds clawed back from us → recall plan.
    "PAYMENT.CAPTURE.REVERSED": "refunded",
    "PAYMENT.CAPTURE.DENIED": "failed",
    "PAYMENT.CAPTURE.DECLINED": "failed",
}

# GET /v2/checkout/orders status -> internal status (used by the read-only query
# path). "APPROVED" is NOT settled — billing must call capture (plan §7.4/S2).
_ORDER_STATUS_MAP: dict[str, str] = {
    "COMPLETED": "paid",
    "APPROVED": "approved",
    "PAYER_ACTION_REQUIRED": "pending",
    "CREATED": "pending",
    "SAVED": "pending",
    "VOIDED": "cancelled",
}


def _env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_ENV_VALUES


@dataclass(frozen=True)
class PayPalConfig:
    client_id: str
    secret: str
    api_base: str
    webhook_id: str
    return_url: str
    cancel_url: str

    @classmethod
    def from_env(cls) -> "PayPalConfig | None":
        if not _env_flag("AVT_PAYPAL_ENABLED"):
            return None
        client_id = os.environ.get("AVT_PAYPAL_CLIENT_ID", "").strip()
        secret = os.environ.get("AVT_PAYPAL_SECRET", "").strip()
        # webhook_id is a hard gate (plan §17 M3): without it verify-webhook-
        # signature cannot run, so a misconfigured deploy must read as not-ready
        # rather than silently accepting unverifiable webhooks.
        webhook_id = os.environ.get("AVT_PAYPAL_WEBHOOK_ID", "").strip()
        if not (client_id and secret and webhook_id):
            return None
        env = (os.environ.get("AVT_PAYPAL_ENV", "sandbox") or "sandbox").strip().lower()
        api_base = PRODUCTION_BASE if env in ("live", "production") else SANDBOX_BASE
        return_url = os.environ.get("AVT_PAYPAL_RETURN_URL", "").strip() or DEFAULT_RETURN_URL
        cancel_url = os.environ.get("AVT_PAYPAL_CANCEL_URL", "").strip() or DEFAULT_CANCEL_URL
        return cls(
            client_id=client_id,
            secret=secret,
            api_base=api_base,
            webhook_id=webhook_id,
            return_url=return_url,
            cancel_url=cancel_url,
        )


def is_paypal_enabled() -> bool:
    return _env_flag("AVT_PAYPAL_ENABLED")


def is_paypal_live_ready() -> bool:
    """Operational only when enabled, fully configured, AND USD prices published.

    Mirrors ``is_paddle_live_ready`` ("all prices present"): if any priced
    plan/period lacks a USD price, ``create_order`` would 502 at click time, so
    PayPal must read as not-operational and stay hidden (plan §17 S3).
    """
    config = PayPalConfig.from_env()
    if config is None:
        return False
    try:
        from plan_catalog import get_price, get_price_usd, valid_target_plan_codes
    except Exception:  # pragma: no cover - defensive
        return False
    for plan in valid_target_plan_codes():
        for period in ("monthly", "quarterly", "annual"):
            if get_price(plan, period) is not None and get_price_usd(plan, period) is None:
                return False
    return True


# --- OAuth token (cached, thread-safe) ---------------------------------------

# key: (api_base, client_id) -> (access_token, expiry_monotonic)
_token_cache: dict[tuple[str, str], tuple[str, float]] = {}
_token_lock = threading.Lock()


def get_access_token(config: PayPalConfig) -> str:
    """Return a cached OAuth2 bearer token, fetching a new one if near expiry."""
    key = (config.api_base, config.client_id)
    now = time.monotonic()
    with _token_lock:
        cached = _token_cache.get(key)
        if cached is not None and cached[1] - _TOKEN_SAFETY_MARGIN_S > now:
            return cached[0]
    # Fetch outside the lock; a concurrent double-fetch is harmless (last wins).
    with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
        response = client.post(
            f"{config.api_base}/v1/oauth2/token",
            auth=(config.client_id, config.secret),
            headers={"Accept": "application/json"},
            data={"grant_type": "client_credentials"},
        )
    response.raise_for_status()
    data = response.json() or {}
    token = str(data.get("access_token") or "").strip()
    if not token:
        raise ValueError("PayPal token response had no access_token")
    expires_in = int(data.get("expires_in") or 0)
    expiry = time.monotonic() + max(expires_in, 60)
    with _token_lock:
        _token_cache[key] = (token, expiry)
    return token


def _auth_headers(config: PayPalConfig) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {get_access_token(config)}",
        "Content-Type": "application/json",
    }


# --- amount helpers ----------------------------------------------------------


def _usd_value_str(cents: int) -> str:
    """USD cents -> 2-decimal string (integer math; no float drift)."""
    return f"{cents // 100}.{cents % 100:02d}"


def _amount_to_usd_cents(value: object, currency: object) -> tuple[str, int | None]:
    """Return (currency_code, USD cents) from a PayPal amount {value, currency_code}."""
    cur = str(currency or "").strip().upper()
    try:
        cents = int((Decimal(str(value)) * 100).to_integral_value())
    except (InvalidOperation, ValueError, TypeError):
        cents = None
    return cur, cents


def _append_order_id(return_url: str, order_id: str) -> str:
    sep = "&" if "?" in return_url else "?"
    return f"{return_url}{sep}order_id={order_id}"


def _extract_custom_id(resource: dict) -> str:
    """custom_id is top-level on a capture resource, nested on an order resource."""
    direct = str((resource or {}).get("custom_id") or "").strip()
    if direct:
        return direct
    for unit in (resource or {}).get("purchase_units") or []:
        cid = str((unit or {}).get("custom_id") or "").strip()
        if cid:
            return cid
    return ""


def _extract_amount(resource: dict) -> tuple[str, int | None]:
    """Currency + USD cents from a capture resource or an order resource."""
    amount = (resource or {}).get("amount")
    if not amount:
        for unit in (resource or {}).get("purchase_units") or []:
            amount = (unit or {}).get("amount")
            if amount:
                break
    amount = amount or {}
    return _amount_to_usd_cents(amount.get("value"), amount.get("currency_code"))


def _extract_related_capture_id(resource: dict) -> str:
    """Capture id a refund/reversal resource references (for refund→order bind).

    A REFUNDED/REVERSED resource is a refund object whose ``custom_id`` may be
    absent (owner dashboard refunds, chargebacks don't echo the original
    capture's custom_id). It always references the original capture though — via
    ``supplementary_data.related_ids.capture_id`` or a ``links`` entry with
    ``rel == "up"`` pointing at ``/captures/{id}``. Used as the binding fallback
    (plan §17 S5) against the capture id we persist on the order at settlement.
    """
    res = resource or {}
    related = (res.get("supplementary_data") or {}).get("related_ids") or {}
    cap = str(related.get("capture_id") or "").strip()
    if cap:
        return cap
    for link in res.get("links") or []:
        if str((link or {}).get("rel") or "").strip().lower() == "up":
            href = str((link or {}).get("href") or "").strip()
            if "/captures/" in href:
                return href.rstrip("/").rsplit("/", 1)[-1]
    return ""


# --- checkout (sync — billing.create_order calls create_checkout in a thread) ---


def create_order(
    config: PayPalConfig,
    *,
    order_id: str,
    target_plan_code: str,
    billing_period: str,
) -> tuple[str, str, int]:
    """Create a PayPal order and return ``(checkout_url, paypal_order_id, expected_usd_cents)``.

    ``checkout_url`` is the ``payer-action`` link the buyer is redirected to.
    ``expected_usd_cents`` is stamped onto our order so settlement compares the
    captured amount against the price charged AT CREATE TIME, immune to a later
    admin USD price edit (plan §17 B2).

    Raises ``ValueError`` if no USD price is published or PayPal returns no
    payer-action link.
    """
    from plan_catalog import get_price_usd

    usd_cents = get_price_usd(target_plan_code, billing_period)
    if not usd_cents or usd_cents <= 0:
        raise ValueError(
            f"no PayPal USD price published for {target_plan_code}/{billing_period}"
        )
    body = {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                # custom_id binds the order to OUR id end-to-end (webhook reads
                # it back). invoice_id is intentionally omitted: PayPal enforces
                # duplicate-invoice protection on it, which would reject a
                # legitimate re-create.
                "custom_id": str(order_id),
                "amount": {"currency_code": "USD", "value": _usd_value_str(usd_cents)},
            }
        ],
        "payment_source": {
            "paypal": {
                "experience_context": {
                    "return_url": _append_order_id(config.return_url, str(order_id)),
                    "cancel_url": config.cancel_url,
                    "user_action": "PAY_NOW",
                }
            }
        },
    }
    headers = {**_auth_headers(config), "PayPal-Request-Id": f"create-{order_id}"}
    with httpx.Client(timeout=httpx.Timeout(20.0)) as client:
        response = client.post(
            f"{config.api_base}/v2/checkout/orders", headers=headers, json=body
        )
    response.raise_for_status()
    data = response.json() or {}
    paypal_order_id = str(data.get("id") or "").strip()
    if not paypal_order_id:
        raise ValueError("PayPal order create returned no id")
    checkout_url = ""
    for link in data.get("links") or []:
        if str((link or {}).get("rel") or "").strip() == "payer-action":
            checkout_url = str(link.get("href") or "").strip()
            break
    if not checkout_url:
        raise ValueError("PayPal returned no payer-action link")
    return checkout_url, paypal_order_id, int(usd_cents)


# --- capture (sync; billing wraps via anyio.to_thread in async contexts) ------


@dataclass
class PayPalCaptureResult:
    paypal_order_id: str
    order_status: str
    capture_id: str
    capture_status: str
    currency: str
    amount_usd_cents: int | None
    custom_id: str
    resource: dict  # the capture object, for the USD fact-gate


def capture_order(
    config: PayPalConfig,
    *,
    paypal_order_id: str,
    order_id: str,
) -> PayPalCaptureResult | None:
    """Capture an approved order. ``PayPal-Request-Id`` makes retries idempotent.

    Returns ``None`` if the order is gone (404). Raises on other HTTP errors.
    """
    headers = {**_auth_headers(config), "PayPal-Request-Id": f"capture-{order_id}"}
    with httpx.Client(timeout=httpx.Timeout(20.0)) as client:
        response = client.post(
            f"{config.api_base}/v2/checkout/orders/{paypal_order_id}/capture",
            headers=headers,
            json={},
        )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    data = response.json() or {}
    order_status = str(data.get("status") or "").strip()
    capture: dict = {}
    for unit in data.get("purchase_units") or []:
        captures = ((unit or {}).get("payments") or {}).get("captures") or []
        if captures:
            capture = captures[0] or {}
            break
    currency, cents = _amount_to_usd_cents(
        (capture.get("amount") or {}).get("value"),
        (capture.get("amount") or {}).get("currency_code"),
    )
    return PayPalCaptureResult(
        paypal_order_id=str(data.get("id") or paypal_order_id),
        order_status=order_status,
        capture_id=str(capture.get("id") or "").strip(),
        capture_status=str(capture.get("status") or "").strip(),
        currency=currency,
        amount_usd_cents=cents,
        custom_id=str(capture.get("custom_id") or order_id).strip(),
        resource=capture,
    )


# --- webhook signature (online verify; fail-closed) --------------------------


def _header_ci(headers: dict[str, str], name: str) -> str:
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value or "")
    return ""


def verify_paypal_signature(
    config: PayPalConfig | None,
    raw_body: bytes,
    headers: dict[str, str],
) -> bool:
    """Online verify via POST /v1/notifications/verify-webhook-signature.

    Returns ``False`` (never raises) on any missing header, non-SUCCESS result,
    or transport error — so an unverifiable webhook lands in the recorded-not-
    settled branch (fail-closed). A transient verify-API outage degrades to
    delayed settlement: the billing_reconciliation sweeper re-queries and
    settles later (plan §17 S8).
    """
    if config is None:
        return False
    try:
        transmission_id = _header_ci(headers, "paypal-transmission-id")
        transmission_time = _header_ci(headers, "paypal-transmission-time")
        transmission_sig = _header_ci(headers, "paypal-transmission-sig")
        cert_url = _header_ci(headers, "paypal-cert-url")
        auth_algo = _header_ci(headers, "paypal-auth-algo")
        if not all(
            [transmission_id, transmission_time, transmission_sig, cert_url, auth_algo]
        ):
            logger.warning("paypal webhook missing transmission headers")
            return False
        webhook_event = json.loads(raw_body) if raw_body else {}
        payload = {
            "auth_algo": auth_algo,
            "cert_url": cert_url,
            "transmission_id": transmission_id,
            "transmission_sig": transmission_sig,
            "transmission_time": transmission_time,
            "webhook_id": config.webhook_id,
            "webhook_event": webhook_event,
        }
        with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
            response = client.post(
                f"{config.api_base}/v1/notifications/verify-webhook-signature",
                headers=_auth_headers(config),
                json=payload,
            )
        response.raise_for_status()
        status = str((response.json() or {}).get("verification_status") or "").strip()
        if status != "SUCCESS":
            logger.warning("paypal webhook verification_status=%s", status)
            return False
        return True
    except Exception as exc:  # fail-closed
        logger.warning("paypal webhook verify error: %s", type(exc).__name__)
        return False


# --- webhook parsing + status mapping ----------------------------------------


@dataclass
class ParsedPayPalWebhook:
    provider_event_id: str
    event_type: str
    order_id: str
    new_status: str
    resource: dict
    raw: dict = field(default_factory=dict)


def parse_paypal_webhook(raw_body: bytes) -> ParsedPayPalWebhook:
    if not raw_body:
        raise ValueError("empty paypal webhook payload")
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid paypal webhook json: {exc}") from exc

    event_type = str(payload.get("event_type") or "").strip()
    resource = payload.get("resource") or {}
    # provider_event_id = resource.id so the synchronous return-path capture and
    # the later CAPTURE.COMPLETED webhook collapse to the same idempotency key
    # (capture id). For non-capture events resource.id is still PayPal-unique.
    provider_event_id = str(resource.get("id") or payload.get("id") or "").strip()
    return ParsedPayPalWebhook(
        provider_event_id=provider_event_id,
        event_type=event_type,
        order_id=_extract_custom_id(resource),
        new_status=map_paypal_event_type(event_type),
        resource=resource,
        raw=payload,
    )


def map_paypal_event_type(event_type: str) -> str:
    """Webhook event_type -> internal status. Non-settlement -> 'pending'."""
    return _EVENT_STATUS_MAP.get(event_type, "pending")


def map_paypal_order_status(status: str) -> str:
    """GET /v2/checkout/orders status token -> internal status."""
    return _ORDER_STATUS_MAP.get(str(status or "").strip().upper(), "pending")


# --- order query (sync read-only; APPROVED→capture orchestration in billing) --


@dataclass
class PayPalQueryResult:
    paypal_order_id: str
    order_status: str  # raw PayPal order status token (CREATED/APPROVED/COMPLETED/…)
    captures: list[dict]
    raw: dict


def query_order(
    config: PayPalConfig | None,
    *,
    paypal_order_id: str,
) -> PayPalQueryResult | None:
    if config is None or not paypal_order_id:
        return None
    with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
        response = client.get(
            f"{config.api_base}/v2/checkout/orders/{paypal_order_id}",
            headers=_auth_headers(config),
        )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    data = response.json() or {}
    status = str(data.get("status") or "").strip()
    if not status:
        return None
    captures: list[dict] = []
    for unit in data.get("purchase_units") or []:
        captures.extend(((unit or {}).get("payments") or {}).get("captures") or [])
    return PayPalQueryResult(
        paypal_order_id=str(data.get("id") or paypal_order_id),
        order_status=status,
        captures=captures,
        raw=data,
    )


# --- USD-anchored fact gate (signature only proves "from PayPal") ------------

# Captured amount may exceed the snapshot by at most this (cents) to absorb any
# PayPal buyer-side rounding; it must NEVER be below (underpayment hole).
_USD_TOLERANCE_CENTS = 1


def validate_paypal_webhook_payload(
    config: PayPalConfig | None,
    resource: dict | None,
    *,
    order_id: str,
    expected_usd_cents: int | None,
    provider_order_id: str | None = None,
) -> None:
    """Raise ``ValueError`` unless the capture resource matches our order.

    Gates: custom_id == our order_id; currency_code == "USD"; captured USD ==
    the per-order snapshot ``expected_usd_cents`` (small upward tolerance only,
    never below — anti-underpayment). The custom_id is the binding anchor; the
    capture resource carries the capture id (not the order id), so
    ``provider_order_id`` is matched only when the resource exposes it.
    """
    if config is None:
        raise ValueError("paypal config missing")
    payload = resource or {}

    custom_id = _extract_custom_id(payload)
    if custom_id != str(order_id):
        raise ValueError("custom_id mismatch")

    currency, cents = _extract_amount(payload)
    if currency != "USD":
        raise ValueError(f"currency mismatch: {currency}")

    if expected_usd_cents is None:
        raise ValueError("order has no expected_usd_cents snapshot")
    if cents is None:
        raise ValueError("capture amount unparseable")
    if cents < int(expected_usd_cents):
        raise ValueError(f"underpayment: captured {cents} < expected {expected_usd_cents}")
    if cents > int(expected_usd_cents) + _USD_TOLERANCE_CENTS:
        raise ValueError(f"overpayment: captured {cents} > expected {expected_usd_cents}")

    # Optional secondary binding: only when the resource exposes a comparable id.
    if provider_order_id:
        related = (
            ((payload.get("supplementary_data") or {}).get("related_ids") or {}).get(
                "order_id"
            )
        )
        if related and str(related).strip() != str(provider_order_id):
            raise ValueError("order id mismatch")
