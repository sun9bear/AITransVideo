"""Payment provider abstraction layer.

Each provider implements PaymentProvider:
- create_checkout: create a provider-side checkout session, return checkout_url + provider_order_id
- verify_signature: verify webhook signature from raw request bytes
- parse_webhook: extract normalized event fields from raw webhook payload
- map_status: map provider-specific status string to internal order status

Sprint 1 constraint: no real external API calls.
- FakeProvider: fully operational, simulates instant payment
- StripeProvider / AlipayProvider / WechatPayProvider: stubs that raise NotImplementedError
  on real operations but define the full interface contract
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Normalized webhook event — provider-agnostic
# ---------------------------------------------------------------------------

@dataclass
class NormalizedWebhookEvent:
    """Provider-agnostic webhook event, produced by PaymentProvider.parse_webhook."""
    provider_event_id: str
    event_type: str       # e.g. "payment.success", "payment.failed"
    order_id: str         # our internal order UUID
    new_status: str       # internal status: "paid" | "failed" | "refunded"
    raw_payload: dict


# ---------------------------------------------------------------------------
# Checkout result — returned by create_checkout
# ---------------------------------------------------------------------------

@dataclass
class CheckoutResult:
    """Result of creating a provider-side checkout session."""
    checkout_url: str
    provider_order_id: str | None = None


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------

class PaymentProvider(Protocol):
    """Abstract interface for payment providers."""

    name: str
    operational: bool  # True if this provider can handle real checkouts/webhooks

    def create_checkout(
        self,
        *,
        order_id: str,
        amount_cny: int,
        target_plan_code: str,
        billing_period: str,
    ) -> CheckoutResult:
        """Create a checkout session with the provider.

        Returns a CheckoutResult with checkout_url for the user to complete payment.
        """
        ...

    def verify_signature(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        """Verify the webhook signature from raw request bytes and headers.

        Returns True if signature is valid, False otherwise.
        """
        ...

    def parse_webhook(self, raw_body: bytes) -> NormalizedWebhookEvent:
        """Parse raw webhook payload into a NormalizedWebhookEvent.

        Raises ValueError if the payload cannot be parsed.
        """
        ...

    def map_status(self, provider_status: str) -> str:
        """Map a provider-specific status string to internal order status.

        Returns one of: "paid", "failed", "refunded", "cancelled", "pending".
        """
        ...


# ---------------------------------------------------------------------------
# Fake provider — fully operational, no external calls
# ---------------------------------------------------------------------------

class FakeProvider:
    """Development/testing provider that simulates instant payment."""

    name = "fake"
    operational = True

    def create_checkout(
        self,
        *,
        order_id: str,
        amount_cny: int,
        target_plan_code: str,
        billing_period: str,
    ) -> CheckoutResult:
        return CheckoutResult(
            checkout_url=f"/api/billing/fake-pay/{order_id}",
            provider_order_id=f"fake_ord_{uuid.uuid4().hex[:12]}",
        )

    def verify_signature(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        # Fake provider has no real signature — always valid
        return True

    def parse_webhook(self, raw_body: bytes) -> NormalizedWebhookEvent:
        import json
        payload = json.loads(raw_body) if raw_body else {}
        return NormalizedWebhookEvent(
            provider_event_id=payload.get("provider_event_id", ""),
            event_type=payload.get("event_type", "unknown"),
            order_id=payload.get("order_id", ""),
            new_status=self.map_status(payload.get("status", "")),
            raw_payload=payload,
        )

    def map_status(self, provider_status: str) -> str:
        return provider_status  # fake provider uses internal status names directly


# ---------------------------------------------------------------------------
# Stub providers — define the contract, raise on real operations
# ---------------------------------------------------------------------------

class _StubProvider:
    """Base for providers not yet implemented. Records the contract."""

    operational = False  # Stubs cannot handle real checkouts until Sprint 2

    def __init__(self, name: str) -> None:
        self.name = name

    def create_checkout(self, **kwargs) -> CheckoutResult:
        raise NotImplementedError(
            f"支付渠道 {self.name} 尚未接入。请在 Sprint 2 实现 {self.name}Provider。"
        )

    def verify_signature(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        raise NotImplementedError(
            f"{self.name} 签名验证尚未实现。"
        )

    def parse_webhook(self, raw_body: bytes) -> NormalizedWebhookEvent:
        raise NotImplementedError(
            f"{self.name} webhook 解析尚未实现。"
        )

    def map_status(self, provider_status: str) -> str:
        raise NotImplementedError(
            f"{self.name} 状态映射尚未实现。"
        )


class StripeProvider(_StubProvider):
    """Stripe payment provider — stub for Sprint 1.

    Sprint 2 implementation notes:
    - create_checkout: use stripe.checkout.Session.create()
    - verify_signature: use stripe.Webhook.construct_event(raw_body, sig_header, endpoint_secret)
    - parse_webhook: extract event.type, event.data.object.metadata.order_id
    - map_status: checkout.session.completed -> "paid", payment_intent.payment_failed -> "failed"
    """

    def __init__(self) -> None:
        super().__init__("stripe")

    def map_status(self, provider_status: str) -> str:
        """Stripe status mapping — defined even in stub for test contracts."""
        mapping = {
            "checkout.session.completed": "paid",
            "payment_intent.payment_failed": "failed",
            "charge.refunded": "refunded",
        }
        return mapping.get(provider_status, provider_status)


class AlipayProvider:
    """Alipay payment provider (Task 5 integration boundary).

    This provider is **env-gated**: it reports `operational = True` only when
    the full Alipay merchant config is present in the environment. Locally, in
    tests, and in any environment that doesn't set `AVT_ALIPAY_*`, the provider
    stays non-operational so the fake path remains the default safe route.

    The heavy lifting (config loading, status mapping, checkout URL building,
    webhook parsing, signature verification) lives in
    `gateway/payment_provider_alipay.py` to keep this module small and its
    import surface stable.
    """

    name = "alipay"

    def __init__(self) -> None:
        # Defer the env read so tests that toggle `AVT_ALIPAY_*` at runtime
        # see fresh config per instance. Registry singletons are also fine
        # because the gateway process doesn't rotate merchant credentials.
        from payment_provider_alipay import AlipayConfig
        self._config: "AlipayConfig | None" = AlipayConfig.from_env()

    @property
    def operational(self) -> bool:
        # Truthfulness gate (T5 minor revision):
        #
        # Being `operational` means we can ACTUALLY take money and settle it.
        # Env presence is necessary but not sufficient — `payment_provider_alipay`
        # has a `_ALIPAY_LIVE_READY` flag that stays False until both the
        # signed-checkout path and the verified-signature path are genuinely
        # implemented. Until someone flips that flag, Alipay stays visible in
        # the provider list but marked `operational: false`, and `/api/billing/
        # checkout-config` will not make it the default provider.
        from payment_provider_alipay import is_alipay_live_ready
        return is_alipay_live_ready()

    def create_checkout(
        self,
        *,
        order_id: str,
        amount_cny: int,
        target_plan_code: str,
        billing_period: str,
    ) -> CheckoutResult:
        if self._config is None:
            raise NotImplementedError(
                "支付渠道 alipay 尚未配置,请先设置 AVT_ALIPAY_* 环境变量"
            )
        from payment_provider_alipay import build_checkout_url
        checkout_url = build_checkout_url(
            self._config,
            order_id=order_id,
            amount_cny=amount_cny,
            target_plan_code=target_plan_code,
            billing_period=billing_period,
        )
        # Alipay assigns its own trade_no asynchronously on notify; we don't
        # have it at checkout-create time, so provider_order_id stays None.
        return CheckoutResult(checkout_url=checkout_url, provider_order_id=None)

    def verify_signature(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        from payment_provider_alipay import verify_alipay_signature
        return verify_alipay_signature(self._config, raw_body, headers)

    def parse_webhook(self, raw_body: bytes) -> NormalizedWebhookEvent:
        from payment_provider_alipay import map_alipay_status, parse_alipay_notify
        parsed = parse_alipay_notify(raw_body)
        new_status = map_alipay_status(parsed.trade_status)
        # Pick a sensible event_type based on the mapped new_status so the
        # core settlement code's logging stays informative.
        if new_status == "paid":
            event_type = "payment.success"
        elif new_status == "cancelled":
            event_type = "payment.cancelled"
        elif new_status == "refunded":
            event_type = "payment.refunded"
        else:
            event_type = f"payment.{new_status}"
        return NormalizedWebhookEvent(
            provider_event_id=parsed.provider_event_id,
            event_type=event_type,
            order_id=parsed.order_id,
            new_status=new_status,
            raw_payload=dict(parsed.raw),
        )

    def map_status(self, provider_status: str) -> str:
        from payment_provider_alipay import map_alipay_status
        return map_alipay_status(provider_status)


class WechatPayProvider(_StubProvider):
    """WeChat Pay provider — stub for Sprint 1.

    Sprint 2 implementation notes:
    - create_checkout: JSAPI / Native pay, return code_url or prepay_id
    - verify_signature: WECHATPAY2-SHA256-RSA2048 header verification
    - parse_webhook: decrypt AES-256-GCM resource body
    - map_status: SUCCESS -> "paid", CLOSED -> "cancelled", REFUND -> "refunded"
    """

    def __init__(self) -> None:
        super().__init__("wechatpay")

    def map_status(self, provider_status: str) -> str:
        mapping = {
            "SUCCESS": "paid",
            "CLOSED": "cancelled",
            "REFUND": "refunded",
            "NOTPAY": "pending",
        }
        return mapping.get(provider_status, provider_status)


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

_PROVIDERS: dict[str, PaymentProvider] = {}


def _init_registry() -> None:
    global _PROVIDERS
    _PROVIDERS = {
        "fake": FakeProvider(),
        "stripe": StripeProvider(),
        "alipay": AlipayProvider(),
        "wechatpay": WechatPayProvider(),
    }


def get_provider(name: str) -> PaymentProvider:
    """Get a registered provider by name. Raises KeyError if not found."""
    if not _PROVIDERS:
        _init_registry()
    if name not in _PROVIDERS:
        raise KeyError(f"Unknown payment provider: {name}")
    return _PROVIDERS[name]


def list_providers() -> list[str]:
    """List all registered provider names."""
    if not _PROVIDERS:
        _init_registry()
    return list(_PROVIDERS.keys())


def is_provider_operational(name: str) -> bool:
    """Check if a provider can handle real checkouts. No side effects."""
    try:
        return get_provider(name).operational
    except KeyError:
        return False
