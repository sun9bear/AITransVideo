"""Alipay helpers for website and mobile-web payments.

This module implements the provider-specific mechanics that the gateway uses:

- Build signed `alipay.trade.page.pay` and `alipay.trade.wap.pay` checkout URLs
- Verify RSA2 signatures on async notify callbacks
- Parse async notify payloads
- Query pending orders with `alipay.trade.query` when notify is delayed or lost
- Validate merchant-facing facts that the official docs require us to re-check
"""
from __future__ import annotations

import base64
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


logger = logging.getLogger(__name__)

CheckoutSurface = Literal["pc_web", "mobile_web"]

_ALIPAY_LIVE_READY: bool = True
_ALIPAY_GATEWAY_PROD = "https://openapi.alipay.com/gateway.do"
_MOBILE_USER_AGENT_MARKERS = (
    "android",
    "iphone",
    "ipad",
    "ipod",
    "mobile",
    "windows phone",
    "blackberry",
    "opera mini",
    "opera mobi",
)


def is_alipay_live_ready() -> bool:
    """Return True iff the code path and env are both ready for live traffic."""
    return _ALIPAY_LIVE_READY and AlipayConfig.from_env() is not None


@dataclass(frozen=True)
class AlipayConfig:
    app_id: str
    app_private_key: str
    alipay_public_key: str
    notify_url: str
    return_url: str
    gateway_url: str
    seller_id: str | None = None

    @classmethod
    def from_env(cls) -> "AlipayConfig | None":
        app_id = os.environ.get("AVT_ALIPAY_APP_ID", "").strip()
        app_private_key = _clean_key_env(
            os.environ.get("AVT_ALIPAY_APP_PRIVATE_KEY", "")
        )
        alipay_public_key = _clean_key_env(os.environ.get("AVT_ALIPAY_PUBLIC_KEY", ""))
        notify_url = os.environ.get("AVT_ALIPAY_NOTIFY_URL", "").strip()
        return_url = os.environ.get("AVT_ALIPAY_RETURN_URL", "").strip()
        gateway_url = os.environ.get(
            "AVT_ALIPAY_GATEWAY_URL", _ALIPAY_GATEWAY_PROD
        ).strip()
        seller_id = os.environ.get("AVT_ALIPAY_SELLER_ID", "").strip() or None

        if not (
            app_id
            and app_private_key
            and alipay_public_key
            and notify_url
            and return_url
        ):
            return None

        return cls(
            app_id=app_id,
            app_private_key=app_private_key,
            alipay_public_key=alipay_public_key,
            notify_url=notify_url,
            return_url=return_url,
            gateway_url=gateway_url or _ALIPAY_GATEWAY_PROD,
            seller_id=seller_id,
        )


def is_alipay_configured() -> bool:
    return AlipayConfig.from_env() is not None


@dataclass
class ParsedAlipayNotify:
    provider_event_id: str
    order_id: str
    trade_status: str
    raw: dict[str, str]


@dataclass
class QueryOrderResult:
    provider_order_id: str | None
    provider_status: str
    raw_payload: dict[str, str]


_STATUS_MAP: dict[str, str] = {
    "TRADE_SUCCESS": "paid",
    "TRADE_FINISHED": "paid",
    "TRADE_CLOSED": "cancelled",
    "WAIT_BUYER_PAY": "pending",
}


def map_alipay_status(provider_status: str) -> str:
    return _STATUS_MAP.get(provider_status, provider_status)


def detect_checkout_surface(
    explicit_surface: str | None, user_agent: str | None
) -> CheckoutSurface:
    if explicit_surface in {"pc_web", "mobile_web"}:
        return explicit_surface
    agent = (user_agent or "").lower()
    if any(marker in agent for marker in _MOBILE_USER_AGENT_MARKERS):
        return "mobile_web"
    return "pc_web"


def build_checkout_url(
    config: AlipayConfig,
    *,
    order_id: str,
    amount_cny: int,
    target_plan_code: str,
    billing_period: str,
    checkout_surface: CheckoutSurface = "pc_web",
) -> str:
    method, product_code = _payment_api_for_surface(checkout_surface)
    subject = _build_subject(target_plan_code, billing_period)
    return_url = _build_return_url(
        config.return_url, order_id=order_id, checkout_surface=checkout_surface
    )
    biz_content: dict[str, str] = {
        "out_trade_no": order_id,
        "total_amount": format_amount_yuan(amount_cny),
        "subject": subject,
        "product_code": product_code,
    }
    if checkout_surface == "mobile_web":
        biz_content["quit_url"] = config.return_url

    params: dict[str, str] = {
        "app_id": config.app_id,
        "method": method,
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": _alipay_timestamp(),
        "version": "1.0",
        "notify_url": config.notify_url,
        "return_url": return_url,
        "biz_content": _json_dumps(biz_content),
    }
    params["sign"] = _sign_params(config, params)
    return f"{config.gateway_url}?{urlencode(params)}"


def parse_alipay_notify(raw_body: bytes) -> ParsedAlipayNotify:
    payload = _parse_payload(raw_body)
    order_id = payload.get("out_trade_no", "").strip()
    if not order_id:
        raise ValueError("alipay notify missing out_trade_no")

    trade_status = payload.get("trade_status", "").strip()
    provider_event_id = (
        payload.get("notify_id", "").strip()
        or payload.get("trade_no", "").strip()
        or f"alipay_evt_{uuid.uuid4().hex[:12]}"
    )
    return ParsedAlipayNotify(
        provider_event_id=provider_event_id,
        order_id=order_id,
        trade_status=trade_status,
        raw=payload,
    )


def verify_alipay_signature(
    config: AlipayConfig | None,
    raw_body: bytes,
    headers: dict[str, str],
) -> bool:
    del headers  # notify verification is payload-based; headers are unused here.
    if config is None:
        return False

    payload = _parse_payload(raw_body)
    sign = payload.get("sign", "").strip()
    if not sign:
        logger.warning("alipay notify missing sign field")
        return False

    signed_fields = {
        key: value
        for key, value in payload.items()
        if key not in {"sign", "sign_type"} and value not in ("", None)
    }
    try:
        return _verify_signature(
            config.alipay_public_key,
            _canonicalize_params(signed_fields),
            sign,
        )
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.warning("alipay signature verification failed: %s", exc)
        return False


async def query_order_status(
    config: AlipayConfig | None,
    *,
    order_id: str,
    provider_order_id: str | None = None,
) -> QueryOrderResult | None:
    if config is None:
        return None

    biz_content: dict[str, str] = {}
    if provider_order_id:
        biz_content["trade_no"] = provider_order_id
    else:
        biz_content["out_trade_no"] = order_id

    params: dict[str, str] = {
        "app_id": config.app_id,
        "method": "alipay.trade.query",
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": _alipay_timestamp(),
        "version": "1.0",
        "biz_content": _json_dumps(biz_content),
    }
    params["sign"] = _sign_params(config, params)

    timeout = httpx.Timeout(15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(config.gateway_url, data=params)
    response.raise_for_status()

    payload = response.json()
    wrapper = payload.get("alipay_trade_query_response") or {}
    code = str(wrapper.get("code", "")).strip()
    if code != "10000":
        sub_code = str(wrapper.get("sub_code", "")).strip()
        if sub_code == "ACQ.TRADE_NOT_EXIST":
            return None
        logger.warning("alipay.trade.query returned non-success: %s %s", code, sub_code)
        return None

    provider_status = str(wrapper.get("trade_status", "")).strip()
    if not provider_status:
        return None

    raw_payload = {str(k): str(v) for k, v in wrapper.items() if v is not None}
    return QueryOrderResult(
        provider_order_id=raw_payload.get("trade_no") or provider_order_id,
        provider_status=provider_status,
        raw_payload=raw_payload,
    )


def validate_alipay_notify_payload(
    config: AlipayConfig | None,
    raw_payload: dict[str, str] | None,
    *,
    order_id: str,
    amount_cny: int,
) -> None:
    if config is None:
        raise ValueError("alipay config missing")
    payload = raw_payload or {}
    if payload.get("out_trade_no", "").strip() != order_id:
        raise ValueError("out_trade_no mismatch")
    if payload.get("app_id", "").strip() != config.app_id:
        raise ValueError("app_id mismatch")
    total_amount = payload.get("total_amount", "").strip()
    if total_amount != format_amount_yuan(amount_cny):
        raise ValueError("total_amount mismatch")
    if config.seller_id:
        seller_id = payload.get("seller_id", "").strip()
        if seller_id != config.seller_id:
            raise ValueError("seller_id mismatch")


def validate_alipay_query_payload(
    config: AlipayConfig | None,
    raw_payload: dict[str, str] | None,
    *,
    order_id: str,
    amount_cny: int,
) -> None:
    if config is None:
        raise ValueError("alipay config missing")
    payload = raw_payload or {}
    if payload.get("out_trade_no", "").strip() != order_id:
        raise ValueError("out_trade_no mismatch")
    total_amount = payload.get("total_amount", "").strip()
    if total_amount and total_amount != format_amount_yuan(amount_cny):
        raise ValueError("total_amount mismatch")
    if config.seller_id:
        seller_id = payload.get("seller_id", "").strip()
        if seller_id and seller_id != config.seller_id:
            raise ValueError("seller_id mismatch")


def format_amount_yuan(amount_cny: int) -> str:
    amount = (Decimal(amount_cny) / Decimal("100")).quantize(
        Decimal("0.00"), rounding=ROUND_HALF_UP
    )
    return f"{amount:.2f}"


def _payment_api_for_surface(
    checkout_surface: CheckoutSurface,
) -> tuple[str, str]:
    if checkout_surface == "mobile_web":
        return "alipay.trade.wap.pay", "QUICK_WAP_WAY"
    return "alipay.trade.page.pay", "FAST_INSTANT_TRADE_PAY"


def _build_subject(target_plan_code: str, billing_period: str) -> str:
    plan_label = {"plus": "Plus", "pro": "Pro"}.get(
        target_plan_code.lower(), target_plan_code.capitalize()
    )
    period_label = {
        "monthly": "monthly",
        "quarterly": "quarterly",
        "annual": "annual",
    }.get(billing_period.lower(), billing_period)
    return f"AIVideoTrans {plan_label} ({period_label})"


def _build_return_url(
    base_url: str,
    *,
    order_id: str,
    checkout_surface: CheckoutSurface,
) -> str:
    return _append_query_params(
        base_url,
        {
            "order_id": order_id,
            "provider": "alipay",
            "surface": checkout_surface,
            "status": "processing",
        },
    )


def _append_query_params(base_url: str, extra_params: dict[str, str]) -> str:
    split = urlsplit(base_url)
    existing = dict(parse_qsl(split.query, keep_blank_values=True))
    existing.update(extra_params)
    query = urlencode(existing)
    return urlunsplit((split.scheme, split.netloc, split.path, query, split.fragment))


def _parse_payload(raw_body: bytes) -> dict[str, str]:
    if not raw_body:
        raise ValueError("empty alipay payload")

    body_str = raw_body.decode("utf-8", errors="replace")
    if body_str.lstrip().startswith("{"):
        try:
            payload = json.loads(body_str)
        except json.JSONDecodeError as exc:  # pragma: no cover - surfaced in tests
            raise ValueError(f"invalid alipay notify json: {exc}") from exc
        return {str(k): str(v) for k, v in payload.items()}
    return {str(k): str(v) for k, v in parse_qsl(body_str, keep_blank_values=True)}


def _json_dumps(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _alipay_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _sign_params(config: AlipayConfig, params: dict[str, str]) -> str:
    return _sign_with_private_key(config.app_private_key, _canonicalize_params(params))


def _canonicalize_params(params: dict[str, str]) -> str:
    parts: list[str] = []
    for key in sorted(params):
        value = params[key]
        if value in ("", None):
            continue
        parts.append(f"{key}={value}")
    return "&".join(parts)


def _sign_with_private_key(private_key_material: str, content: str) -> str:
    private_key = _load_private_key(private_key_material)
    signature = private_key.sign(
        content.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def _verify_signature(
    public_key_material: str,
    content: str,
    signature_base64: str,
) -> bool:
    public_key = _load_public_key(public_key_material)
    try:
        public_key.verify(
            base64.b64decode(signature_base64),
            content.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except InvalidSignature:
        return False


@lru_cache(maxsize=8)
def _load_private_key(private_key_material: str):
    pem = _ensure_private_key_pem(private_key_material).encode("utf-8")
    return serialization.load_pem_private_key(pem, password=None)


@lru_cache(maxsize=8)
def _load_public_key(public_key_material: str):
    pem = _ensure_public_key_pem(public_key_material).encode("utf-8")
    return serialization.load_pem_public_key(pem)


def _clean_key_env(raw_value: str) -> str:
    return raw_value.replace("\\n", "\n").strip()


def _ensure_private_key_pem(raw_value: str) -> str:
    if "BEGIN" in raw_value:
        return raw_value
    return _wrap_pem_body("PRIVATE KEY", raw_value)


def _ensure_public_key_pem(raw_value: str) -> str:
    if "BEGIN" in raw_value:
        return raw_value
    return _wrap_pem_body("PUBLIC KEY", raw_value)


def _wrap_pem_body(label: str, raw_value: str) -> str:
    body = "".join(raw_value.split())
    chunks = [body[i : i + 64] for i in range(0, len(body), 64)]
    return "\n".join(
        [f"-----BEGIN {label}-----", *chunks, f"-----END {label}-----"]
    )
