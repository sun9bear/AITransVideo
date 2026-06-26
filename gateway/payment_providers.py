"""Payment provider abstraction layer."""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from typing import Protocol


@dataclass
class NormalizedWebhookEvent:
    provider_event_id: str
    event_type: str
    order_id: str
    new_status: str
    raw_payload: dict


@dataclass
class CheckoutResult:
    checkout_url: str
    provider_order_id: str | None = None
    # "redirect" (default) keeps Alipay/Paddle/fake behavior; "qrcode" tells the
    # frontend to render qr_code_url in an in-page dialog instead of navigating
    # (WeChat Native returns a weixin:// string, not a web URL).
    display_mode: str = "redirect"
    qr_code_url: str | None = None
    # PayPal lane: the USD cents charged at create time, surfaced so billing can
    # stamp it onto the order as the settlement snapshot (plan 2026-06-26 B2).
    expected_usd_cents: int | None = None


@dataclass
class ProviderOrderQueryResult:
    provider_event_id: str
    provider_order_id: str | None
    provider_status: str
    raw_payload: dict


class PaymentProvider(Protocol):
    name: str
    operational: bool

    def create_checkout(
        self,
        *,
        order_id: str,
        amount_cny: int,
        target_plan_code: str,
        billing_period: str,
        checkout_surface: str = "pc_web",
        customer_email: str | None = None,
    ) -> CheckoutResult:
        ...

    def verify_signature(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        ...

    def parse_webhook(self, raw_body: bytes) -> NormalizedWebhookEvent:
        ...

    def map_status(self, provider_status: str) -> str:
        ...

    async def query_order(
        self,
        *,
        order_id: str,
        provider_order_id: str | None = None,
    ) -> ProviderOrderQueryResult | None:
        ...


class FakeProvider:
    name = "fake"

    @property
    def operational(self) -> bool:
        return is_fake_payment_enabled()

    def create_checkout(
        self,
        *,
        order_id: str,
        amount_cny: int,
        target_plan_code: str,
        billing_period: str,
        checkout_surface: str = "pc_web",
        customer_email: str | None = None,
    ) -> CheckoutResult:
        del amount_cny, target_plan_code, billing_period, checkout_surface
        del customer_email
        if not is_fake_payment_enabled():
            raise RuntimeError("fake payment provider is disabled")
        return CheckoutResult(
            checkout_url=f"/api/billing/fake-pay/{order_id}",
            provider_order_id=f"fake_ord_{uuid.uuid4().hex[:12]}",
        )

    def verify_signature(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        del raw_body, headers
        return True

    def parse_webhook(self, raw_body: bytes) -> NormalizedWebhookEvent:
        payload = json.loads(raw_body) if raw_body else {}
        return NormalizedWebhookEvent(
            provider_event_id=payload.get("provider_event_id", ""),
            event_type=payload.get("event_type", "unknown"),
            order_id=payload.get("order_id", ""),
            new_status=self.map_status(payload.get("status", "")),
            raw_payload=payload,
        )

    def map_status(self, provider_status: str) -> str:
        return provider_status

    async def query_order(
        self,
        *,
        order_id: str,
        provider_order_id: str | None = None,
    ) -> ProviderOrderQueryResult | None:
        del order_id, provider_order_id
        return None


class _StubProvider:
    operational = False

    def __init__(self, name: str) -> None:
        self.name = name

    def create_checkout(self, **kwargs) -> CheckoutResult:
        del kwargs
        raise NotImplementedError(f"payment provider {self.name} is not implemented")

    def verify_signature(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        del raw_body, headers
        raise NotImplementedError(f"{self.name} signature verification is not implemented")

    def parse_webhook(self, raw_body: bytes) -> NormalizedWebhookEvent:
        del raw_body
        raise NotImplementedError(f"{self.name} webhook parsing is not implemented")

    def map_status(self, provider_status: str) -> str:
        del provider_status
        raise NotImplementedError(f"{self.name} status mapping is not implemented")

    async def query_order(
        self,
        *,
        order_id: str,
        provider_order_id: str | None = None,
    ) -> ProviderOrderQueryResult | None:
        del order_id, provider_order_id
        raise NotImplementedError(f"{self.name} order query is not implemented")


class StripeProvider(_StubProvider):
    def __init__(self) -> None:
        super().__init__("stripe")

    def map_status(self, provider_status: str) -> str:
        mapping = {
            "checkout.session.completed": "paid",
            "payment_intent.payment_failed": "failed",
            "charge.refunded": "refunded",
        }
        return mapping.get(provider_status, provider_status)


class AlipayProvider:
    name = "alipay"

    def __init__(self) -> None:
        from payment_provider_alipay import AlipayConfig

        self._config = AlipayConfig.from_env()

    @property
    def operational(self) -> bool:
        from payment_provider_alipay import is_alipay_live_ready

        return is_alipay_live_ready()

    def create_checkout(
        self,
        *,
        order_id: str,
        amount_cny: int,
        target_plan_code: str,
        billing_period: str,
        checkout_surface: str = "pc_web",
        customer_email: str | None = None,
    ) -> CheckoutResult:
        del customer_email  # Alipay checkout collects no buyer email
        if self._config is None:
            raise NotImplementedError(
                "payment provider alipay is not configured; set AVT_ALIPAY_* env vars first"
            )
        from payment_provider_alipay import build_checkout_url

        checkout_url = build_checkout_url(
            self._config,
            order_id=order_id,
            amount_cny=amount_cny,
            target_plan_code=target_plan_code,
            billing_period=billing_period,
            checkout_surface=checkout_surface,
        )
        return CheckoutResult(checkout_url=checkout_url, provider_order_id=None)

    def verify_signature(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        from payment_provider_alipay import verify_alipay_signature

        return verify_alipay_signature(self._config, raw_body, headers)

    def parse_webhook(self, raw_body: bytes) -> NormalizedWebhookEvent:
        from payment_provider_alipay import map_alipay_status, parse_alipay_notify

        parsed = parse_alipay_notify(raw_body)
        new_status = map_alipay_status(parsed.trade_status)
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

    async def query_order(
        self,
        *,
        order_id: str,
        provider_order_id: str | None = None,
    ) -> ProviderOrderQueryResult | None:
        from payment_provider_alipay import query_order_status

        result = await query_order_status(
            self._config,
            order_id=order_id,
            provider_order_id=provider_order_id,
        )
        if result is None:
            return None
        provider_event_id = (
            f"alipay_query_{result.provider_order_id or order_id}_{result.provider_status}"
        )
        return ProviderOrderQueryResult(
            provider_event_id=provider_event_id,
            provider_order_id=result.provider_order_id,
            provider_status=result.provider_status,
            raw_payload=dict(result.raw_payload),
        )


class WechatPayProvider:
    """Own-merchant WeChat Pay v3 Native (QR). Mechanics in payment_provider_wechat.py."""

    name = "wechatpay"

    @property
    def operational(self) -> bool:
        from payment_provider_wechat import is_wechatpay_live_ready

        return is_wechatpay_live_ready()

    def create_checkout(
        self,
        *,
        order_id: str,
        amount_cny: int,
        target_plan_code: str,
        billing_period: str,
        checkout_surface: str = "pc_web",
        customer_email: str | None = None,
    ) -> CheckoutResult:
        # Native is QR-based and collects nothing from us beyond the amount;
        # surface routing happens upstream (checkout-config recommendation).
        del checkout_surface, customer_email
        from payment_provider_wechat import WechatPayConfig, create_native_order

        config = WechatPayConfig.from_env()
        if config is None:
            raise NotImplementedError(
                "payment provider wechatpay is not configured; set WECHATPAY_* env vars first"
            )
        code_url, out_trade_no = create_native_order(
            config,
            order_id=order_id,
            amount_fen=amount_cny,
            description=f"AITrans 套餐 {target_plan_code}/{billing_period}",
        )
        return CheckoutResult(
            checkout_url="",
            provider_order_id=out_trade_no,
            display_mode="qrcode",
            qr_code_url=code_url,
        )

    def verify_signature(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        from payment_provider_wechat import WechatPayConfig, verify_wechat_signature

        return verify_wechat_signature(WechatPayConfig.from_env(), raw_body, headers)

    def parse_webhook(self, raw_body: bytes) -> NormalizedWebhookEvent:
        from payment_provider_wechat import WechatPayConfig, parse_wechat_webhook

        parsed = parse_wechat_webhook(WechatPayConfig.from_env(), raw_body)
        return NormalizedWebhookEvent(
            provider_event_id=parsed.provider_event_id,
            event_type=parsed.event_type,
            order_id=parsed.order_id,
            new_status=parsed.new_status,
            raw_payload=dict(parsed.raw),
        )

    def map_status(self, provider_status: str) -> str:
        from payment_provider_wechat import map_wechat_trade_state

        return map_wechat_trade_state(provider_status)

    async def query_order(
        self,
        *,
        order_id: str,
        provider_order_id: str | None = None,
    ) -> ProviderOrderQueryResult | None:
        del order_id  # WeChat is queried by out_trade_no (provider_order_id)
        if not provider_order_id:
            return None
        from payment_provider_wechat import WechatPayConfig, query_transaction

        result = await query_transaction(
            WechatPayConfig.from_env(), out_trade_no=provider_order_id
        )
        if result is None:
            return None
        provider_event_id = (
            f"wechat_query_{result.out_trade_no}_{result.trade_state}"
        )
        return ProviderOrderQueryResult(
            provider_event_id=provider_event_id,
            provider_order_id=result.out_trade_no,
            provider_status=result.trade_state,
            raw_payload=dict(result.raw_payload),
        )


class PaddleProvider:
    """Paddle Billing (MoR). Mechanics live in payment_provider_paddle.py."""

    name = "paddle"

    @property
    def operational(self) -> bool:
        from payment_provider_paddle import is_paddle_live_ready

        return is_paddle_live_ready()

    def create_checkout(
        self,
        *,
        order_id: str,
        amount_cny: int,
        target_plan_code: str,
        billing_period: str,
        checkout_surface: str = "pc_web",
        customer_email: str | None = None,
    ) -> CheckoutResult:
        # Paddle charges by price_id; which methods show (Alipay / WeChat / card)
        # is decided by Paddle from buyer geo + currency, so amount_cny and
        # checkout_surface are not needed to build the transaction.
        del amount_cny, checkout_surface
        from payment_provider_paddle import PaddleConfig, create_transaction

        config = PaddleConfig.from_env()
        if config is None:
            raise NotImplementedError(
                "payment provider paddle is not configured; set AVT_PADDLE_* env vars first"
            )
        checkout_url, txn_id = create_transaction(
            config,
            order_id=order_id,
            target_plan_code=target_plan_code,
            billing_period=billing_period,
            customer_email=customer_email,
        )
        return CheckoutResult(checkout_url=checkout_url, provider_order_id=txn_id)

    def verify_signature(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        from payment_provider_paddle import PaddleConfig, verify_paddle_signature

        return verify_paddle_signature(PaddleConfig.from_env(), raw_body, headers)

    def parse_webhook(self, raw_body: bytes) -> NormalizedWebhookEvent:
        from payment_provider_paddle import parse_paddle_webhook

        parsed = parse_paddle_webhook(raw_body)
        return NormalizedWebhookEvent(
            provider_event_id=parsed.provider_event_id,
            event_type=parsed.event_type,
            order_id=parsed.order_id,
            new_status=parsed.new_status,
            raw_payload=dict(parsed.raw),
        )

    def map_status(self, provider_status: str) -> str:
        # Used by the order-query refresh path, which passes a transaction.status
        # token (not an event_type). Webhook path uses parse_webhook's new_status.
        from payment_provider_paddle import map_paddle_transaction_status

        return map_paddle_transaction_status(provider_status)

    async def query_order(
        self,
        *,
        order_id: str,
        provider_order_id: str | None = None,
    ) -> ProviderOrderQueryResult | None:
        del order_id  # Paddle is queried by transaction id (provider_order_id)
        if not provider_order_id:
            return None
        from payment_provider_paddle import PaddleConfig, query_transaction

        result = await query_transaction(
            PaddleConfig.from_env(), transaction_id=provider_order_id
        )
        if result is None:
            return None
        provider_event_id = (
            f"paddle_query_{result.transaction_id}_{result.provider_status}"
        )
        return ProviderOrderQueryResult(
            provider_event_id=provider_event_id,
            provider_order_id=result.transaction_id,
            provider_status=result.provider_status,
            raw_payload=dict(result.raw_payload),
        )


class PayPalProvider:
    """PayPal Orders v2 (overseas USD lane). Mechanics in payment_provider_paypal.py."""

    name = "paypal"

    @property
    def operational(self) -> bool:
        from payment_provider_paypal import is_paypal_live_ready

        return is_paypal_live_ready()

    def create_checkout(
        self,
        *,
        order_id: str,
        amount_cny: int,
        target_plan_code: str,
        billing_period: str,
        checkout_surface: str = "pc_web",
        customer_email: str | None = None,
    ) -> CheckoutResult:
        # PayPal charges the independent USD list price; amount_cny / surface /
        # email are not needed to build the order. expected_usd_cents is surfaced
        # so billing stamps it onto the order for the settlement snapshot (B2).
        del amount_cny, checkout_surface, customer_email
        from payment_provider_paypal import PayPalConfig, create_order

        config = PayPalConfig.from_env()
        if config is None:
            raise NotImplementedError(
                "payment provider paypal is not configured; set AVT_PAYPAL_* env vars first"
            )
        checkout_url, paypal_order_id, expected_usd_cents = create_order(
            config,
            order_id=order_id,
            target_plan_code=target_plan_code,
            billing_period=billing_period,
        )
        return CheckoutResult(
            checkout_url=checkout_url,
            provider_order_id=paypal_order_id,
            expected_usd_cents=expected_usd_cents,
        )

    def verify_signature(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        from payment_provider_paypal import PayPalConfig, verify_paypal_signature

        return verify_paypal_signature(PayPalConfig.from_env(), raw_body, headers)

    def parse_webhook(self, raw_body: bytes) -> NormalizedWebhookEvent:
        from payment_provider_paypal import parse_paypal_webhook

        parsed = parse_paypal_webhook(raw_body)
        return NormalizedWebhookEvent(
            provider_event_id=parsed.provider_event_id,
            event_type=parsed.event_type,
            order_id=parsed.order_id,
            new_status=parsed.new_status,
            raw_payload=dict(parsed.raw),
        )

    def map_status(self, provider_status: str) -> str:
        from payment_provider_paypal import map_paypal_order_status

        return map_paypal_order_status(provider_status)

    async def query_order(
        self,
        *,
        order_id: str,
        provider_order_id: str | None = None,
    ) -> ProviderOrderQueryResult | None:
        # Read-only order status. The APPROVED→capture money-moving action lives
        # in billing's dedicated paypal refresh branch (plan §7.4/S2), not here.
        del order_id
        if not provider_order_id:
            return None
        import anyio

        from payment_provider_paypal import PayPalConfig, query_order

        result = await anyio.to_thread.run_sync(
            lambda: query_order(PayPalConfig.from_env(), paypal_order_id=provider_order_id)
        )
        if result is None:
            return None
        provider_event_id = f"paypal_query_{result.paypal_order_id}_{result.order_status}"
        return ProviderOrderQueryResult(
            provider_event_id=provider_event_id,
            provider_order_id=result.paypal_order_id,
            provider_status=result.order_status,
            raw_payload=dict(result.raw),
        )


_PROVIDERS: dict[str, PaymentProvider] = {}


_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}
_KNOWN_ENVS = {"dev", "test", "staging", "prod", "production"}
_PRODUCTION_ENVS = {"prod", "production"}


def _env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_ENV_VALUES


def is_fake_payment_enabled() -> bool:
    """Return whether the local fake payment provider may settle orders.

    Fake checkout is useful for local and test loops, but production should not
    expose a provider that settles by order id alone. Production can still opt in
    deliberately for a controlled smoke test with AVT_ENABLE_FAKE_PAYMENT=true.
    """
    env = (os.environ.get("AVT_ENV") or "dev").strip().lower()
    if env not in _KNOWN_ENVS:
        return False
    if env in _PRODUCTION_ENVS:
        return _env_flag("AVT_ENABLE_FAKE_PAYMENT", default=False)
    return True


def _init_registry() -> None:
    global _PROVIDERS
    _PROVIDERS = {
        "fake": FakeProvider(),
        "stripe": StripeProvider(),
        "alipay": AlipayProvider(),
        "wechatpay": WechatPayProvider(),
        "paddle": PaddleProvider(),
        "paypal": PayPalProvider(),
    }


def get_provider(name: str) -> PaymentProvider:
    if not _PROVIDERS:
        _init_registry()
    if name not in _PROVIDERS:
        raise KeyError(f"Unknown payment provider: {name}")
    return _PROVIDERS[name]


def list_providers() -> list[str]:
    if not _PROVIDERS:
        _init_registry()
    return list(_PROVIDERS.keys())


def is_provider_operational(name: str) -> bool:
    try:
        return get_provider(name).operational
    except KeyError:
        return False
