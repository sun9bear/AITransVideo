"""WeChat Pay v3 Native helpers (own-merchant QR checkout, Rail 1).

Plan 2026-05-22 (wechatpay-native-integration-plan). Mechanics are ported from
the battle-tested AiPlay.video implementation (same mchid 1745928268), NOT the
wechatpayv3 SDK — the forerunner validated hand-rolled signing/verify/decrypt
on the `cryptography` primitives this gateway already ships for Alipay.

Provider-specific mechanics the gateway uses through ``WechatPayProvider``:
- ``create_native_order``       : POST /v3/pay/transactions/native -> code_url
- ``verify_wechat_signature``   : Wechatpay-Signature RSA-SHA256 + ts freshness
- ``parse_wechat_webhook``      : envelope -> AES-256-GCM decrypt -> normalized
- ``map_wechat_trade_state``    : SUCCESS/NOTPAY/... -> internal status
- ``query_transaction``         : GET /v3/pay/transactions/out-trade-no/{otn}
- ``validate_wechat_webhook_payload`` : order-fact gates (amount/otn/attach)

Cross-project invariants (mchid is shared with AiPlay.video):
- ``notify_url`` is injected per request — NEVER rely on the merchant-portal
  default (only one global slot, the other project may own it).
- ``out_trade_no`` carries the ``AVT_`` prefix (AiPlay uses ``wv_``/``ws_``),
  32 chars max per WeChat spec, derived deterministically from our order UUID.
- ``attach`` carries the full order UUID and is echoed back in callbacks —
  it is the webhook -> order binding (mirrors Paddle's custom_data.order_id).

Settlement event: ``TRANSACTION.SUCCESS`` with decrypted ``trade_state``.
Money is CNY fen end-to-end (``amount.total`` == plan_catalog fen).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

API_BASE = "https://api.mch.weixin.qq.com"
DEFAULT_NOTIFY_URL = "https://aitrans.video/api/billing/webhooks/wechatpay"
DEFAULT_ORDER_PREFIX = "AVT_"

# WeChat spec: out_trade_no is 6-32 chars. Prefix "AVT_" (4) + 28 hex chars
# of the order UUID = 32. 112 bits of the UUID is collision-safe at our scale.
_OUT_TRADE_NO_HEX_CHARS = 28

# Callback timestamp tolerance (spec recommends 5 minutes), mirrors Paddle.
_SIGNATURE_MAX_AGE_S = 300

_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}

# Decrypted transaction.trade_state -> internal order status.
_TRADE_STATE_MAP: dict[str, str] = {
    "SUCCESS": "paid",
    "NOTPAY": "pending",
    "USERPAYING": "pending",
    "ACCEPT": "pending",
    "CLOSED": "cancelled",
    "REVOKED": "cancelled",
    "PAYERROR": "failed",
    "REFUND": "refunded",
}


def _env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_ENV_VALUES


@dataclass(frozen=True)
class WechatPayConfig:
    mchid: str
    apiv3_key: bytes  # 32 bytes, AES-256-GCM
    private_key_pem: bytes  # merchant private key (request signing)
    cert_serial: str  # merchant certificate serial (Authorization header)
    pub_key_id: str  # WeChat platform public key id (callback Wechatpay-Serial)
    platform_pub_key_pem: bytes  # WeChat platform public key (callback verify)
    notify_url: str
    order_prefix: str
    appid: str | None = None
    api_base: str = API_BASE

    @classmethod
    def from_env(cls) -> "WechatPayConfig | None":
        if not _env_flag("AVT_WECHATPAY_ENABLED"):
            return None
        mchid = os.environ.get("WECHATPAY_MCHID", "").strip()
        apiv3_key = os.environ.get("WECHATPAY_APIV3_KEY", "").strip()
        cert_serial = os.environ.get("WECHATPAY_CERT_SERIAL", "").strip()
        pub_key_id = os.environ.get("WECHATPAY_PUB_KEY_ID", "").strip()
        private_key_path = os.environ.get("WECHATPAY_PRIVATE_KEY_PATH", "").strip()
        platform_pub_key_path = os.environ.get(
            "WECHATPAY_PLATFORM_PUB_KEY_PATH", ""
        ).strip()
        if not (
            mchid.isdigit()
            and len(apiv3_key) == 32
            and cert_serial
            and pub_key_id
            and private_key_path
            and platform_pub_key_path
        ):
            return None
        try:
            private_key_pem = Path(private_key_path).read_bytes()
            platform_pub_key_pem = Path(platform_pub_key_path).read_bytes()
        except OSError as exc:
            logger.warning("wechatpay key file unreadable: %s", type(exc).__name__)
            return None
        notify_url = (
            os.environ.get("WECHATPAY_NOTIFY_URL", "").strip() or DEFAULT_NOTIFY_URL
        )
        return cls(
            mchid=mchid,
            apiv3_key=apiv3_key.encode("utf-8"),
            private_key_pem=private_key_pem,
            cert_serial=cert_serial,
            pub_key_id=pub_key_id,
            platform_pub_key_pem=platform_pub_key_pem,
            notify_url=notify_url,
            order_prefix=os.environ.get("WECHATPAY_ORDER_PREFIX", "").strip()
            or DEFAULT_ORDER_PREFIX,
            appid=os.environ.get("WECHATPAY_APPID", "").strip() or None,
        )


def is_wechatpay_enabled() -> bool:
    return _env_flag("AVT_WECHATPAY_ENABLED")


def is_wechatpay_live_ready() -> bool:
    """Operational only when enabled AND fully configured (keys readable)."""
    return WechatPayConfig.from_env() is not None


def build_out_trade_no(config: WechatPayConfig, order_id: str) -> str:
    """Deterministic, <=32-char merchant order number with the AVT_ prefix."""
    compact = str(order_id).replace("-", "")
    return f"{config.order_prefix}{compact[:_OUT_TRADE_NO_HEX_CHARS]}"


# --- request signing (WECHATPAY2-SHA256-RSA2048) ---


def _build_authorization(
    config: WechatPayConfig, method: str, url_path: str, body: str
) -> str:
    """Sign ``METHOD\\nPATH\\nTIMESTAMP\\nNONCE\\nBODY\\n`` per the v3 spec.

    ``url_path`` must include the query string (if any); ``body`` must be the
    EXACT string sent on the wire (GETs sign an empty body).
    """
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    canonical = f"{method}\n{url_path}\n{timestamp}\n{nonce}\n{body}\n".encode("utf-8")
    private_key = serialization.load_pem_private_key(
        config.private_key_pem, password=None
    )
    signature = private_key.sign(canonical, padding.PKCS1v15(), hashes.SHA256())
    signature_b64 = base64.b64encode(signature).decode("ascii")
    return (
        'WECHATPAY2-SHA256-RSA2048 '
        f'mchid="{config.mchid}",'
        f'nonce_str="{nonce}",'
        f'signature="{signature_b64}",'
        f'timestamp="{timestamp}",'
        f'serial_no="{config.cert_serial}"'
    )


def _request_headers(config: WechatPayConfig, method: str, url_path: str, body: str) -> dict[str, str]:
    return {
        "Authorization": _build_authorization(config, method, url_path, body),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# --- checkout (sync — billing.create_order runs providers in a worker thread) ---


def create_native_order(
    config: WechatPayConfig,
    *,
    order_id: str,
    amount_fen: int,
    description: str,
) -> tuple[str, str]:
    """Create a Native transaction and return ``(code_url, out_trade_no)``.

    ``code_url`` is a ``weixin://wxpay/bizpayurl?...`` string the frontend
    renders as a QR code. Raises ``ValueError`` on a missing code_url.
    """
    out_trade_no = build_out_trade_no(config, order_id)
    body_obj: dict = {
        "mchid": config.mchid,
        "out_trade_no": out_trade_no,
        "description": description[:127],
        # Per-request notify_url override — the merchant-portal default is a
        # single global slot shared with AiPlay.video. Do not depend on it.
        "notify_url": config.notify_url,
        "amount": {"total": int(amount_fen), "currency": "CNY"},
        # Echoed back verbatim in payment callbacks; binds webhook -> order.
        "attach": str(order_id),
    }
    if config.appid:
        body_obj["appid"] = config.appid
    # Serialize ONCE and sign exactly the bytes that go on the wire — letting
    # httpx re-serialize a dict would break the signature.
    body = json.dumps(body_obj, ensure_ascii=False, separators=(",", ":"))
    url_path = "/v3/pay/transactions/native"
    with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
        response = client.post(
            f"{config.api_base}{url_path}",
            headers=_request_headers(config, "POST", url_path, body),
            content=body.encode("utf-8"),
        )
    if response.status_code >= 400:
        raise ValueError(
            f"wechatpay native order failed: HTTP {response.status_code} "
            f"{_error_code(response)}"
        )
    code_url = str(((response.json() or {}).get("code_url")) or "").strip()
    if not code_url:
        raise ValueError("wechatpay returned empty code_url")
    return code_url, out_trade_no


def _error_code(response: httpx.Response) -> str:
    try:
        payload = response.json() or {}
    except Exception:
        return ""
    # Only the machine code — error `message` may echo request fields.
    return str(payload.get("code") or "")


# --- callback signature verification (verify BEFORE trusting anything) ---


def verify_wechat_signature(
    config: WechatPayConfig | None,
    raw_body: bytes,
    headers: dict[str, str],
    *,
    now: float | None = None,
) -> bool:
    if config is None:
        return False
    lowered = {k.lower(): v for k, v in headers.items()}
    timestamp = (lowered.get("wechatpay-timestamp") or "").strip()
    nonce = (lowered.get("wechatpay-nonce") or "").strip()
    signature_b64 = (lowered.get("wechatpay-signature") or "").strip()
    serial = (lowered.get("wechatpay-serial") or "").strip()
    if not (timestamp and nonce and signature_b64 and serial):
        logger.warning("wechatpay webhook missing signature headers")
        return False

    # Freshness (replay protection) BEFORE any crypto work.
    try:
        ts_int = int(timestamp)
    except ValueError:
        return False
    current = int(now if now is not None else time.time())
    if abs(current - ts_int) > _SIGNATURE_MAX_AGE_S:
        logger.warning("wechatpay webhook timestamp outside tolerance (replay?)")
        return False

    # The platform key that signed this callback must be the one we hold.
    if serial != config.pub_key_id:
        logger.warning("wechatpay webhook serial mismatch (key rotation?)")
        return False

    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except Exception:
        return False

    # Spec §: canonical is timestamp\n nonce\n RAW body bytes \n — never
    # decode/re-encode the body (whitespace differences break the signature).
    canonical = (
        timestamp.encode("ascii") + b"\n" + nonce.encode("ascii") + b"\n" + raw_body + b"\n"
    )
    try:
        public_key = serialization.load_pem_public_key(config.platform_pub_key_pem)
        public_key.verify(signature, canonical, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        logger.warning("wechatpay webhook signature mismatch")
        return False
    except Exception as exc:
        logger.warning("wechatpay webhook verify error: %s", type(exc).__name__)
        return False
    return True


# --- resource decryption + webhook parsing ---


def decrypt_resource(config: WechatPayConfig, resource: dict) -> dict:
    """AEAD-AES-256-GCM decrypt of the callback ``resource`` envelope.

    An auth-tag failure (InvalidTag) means a wrong key or tampering — let it
    raise; callers treat any exception as an unparseable webhook (400).
    """
    ciphertext_b64 = str(resource.get("ciphertext") or "")
    nonce = str(resource.get("nonce") or "")
    associated_data = resource.get("associated_data")
    aead = AESGCM(config.apiv3_key)
    plaintext = aead.decrypt(
        nonce.encode("utf-8"),
        base64.b64decode(ciphertext_b64),
        str(associated_data).encode("utf-8") if associated_data is not None else None,
    )
    return json.loads(plaintext)


@dataclass
class ParsedWechatWebhook:
    provider_event_id: str
    event_type: str
    order_id: str
    out_trade_no: str
    new_status: str
    transaction: dict  # decrypted transaction object
    raw: dict  # envelope WITHOUT the (now decrypted) resource secrets


def parse_wechat_webhook(
    config: WechatPayConfig | None, raw_body: bytes
) -> ParsedWechatWebhook:
    if config is None:
        raise ValueError("wechatpay config missing")
    if not raw_body:
        raise ValueError("empty wechatpay webhook payload")
    try:
        envelope = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid wechatpay webhook json: {exc}") from exc

    event_type = str(envelope.get("event_type") or "").strip()
    resource = envelope.get("resource") or {}
    try:
        transaction = decrypt_resource(config, resource)
    except Exception as exc:
        raise ValueError(f"wechatpay resource decrypt failed: {type(exc).__name__}") from exc

    trade_state = str(transaction.get("trade_state") or "").strip()
    out_trade_no = str(transaction.get("out_trade_no") or "").strip()
    # transaction_id (WeChat's platform-unique id) is the idempotency key for
    # settlement events (plan §8.3); fall back to the envelope id for events
    # that carry no transaction (e.g. future refund notifications).
    provider_event_id = (
        str(transaction.get("transaction_id") or "").strip()
        or str(envelope.get("id") or "").strip()
    )
    return ParsedWechatWebhook(
        provider_event_id=provider_event_id,
        event_type=event_type or "unknown",
        order_id=str(transaction.get("attach") or "").strip(),
        out_trade_no=out_trade_no,
        new_status=map_wechat_trade_state(trade_state),
        transaction=transaction,
        raw={
            "id": envelope.get("id"),
            "event_type": event_type,
            "transaction": transaction,
        },
    )


def map_wechat_trade_state(trade_state: str) -> str:
    """trade_state -> internal status. Unknown states stay pending (no settle)."""
    return _TRADE_STATE_MAP.get(trade_state, "pending")


# --- order-fact validation (signature only proves "from WeChat") ---


def validate_wechat_webhook_payload(
    config: WechatPayConfig | None,
    transaction: dict | None,
    *,
    order_id: str,
    amount_cny: int,
    provider_order_id: str | None = None,
) -> None:
    """Raise ``ValueError`` unless the transaction matches our order.

    Gates: mchid, attach == order id, out_trade_no (when we recorded one),
    and amount.total fen == the order amount. WeChat charges exactly our list
    price (no MoR tax markup), so the strict amount gate is correct here —
    unlike Paddle where the gate is the price_id binding.
    """
    if config is None:
        raise ValueError("wechatpay config missing")
    txn = transaction or {}

    mchid = str(txn.get("mchid") or "").strip()
    if mchid and mchid != config.mchid:
        raise ValueError("mchid mismatch")

    if str(txn.get("attach") or "").strip() != str(order_id):
        raise ValueError("attach order_id mismatch")

    if provider_order_id:
        if str(txn.get("out_trade_no") or "").strip() != str(provider_order_id):
            raise ValueError("out_trade_no mismatch")

    total = ((txn.get("amount") or {}).get("total"))
    if total is None or int(total) != int(amount_cny):
        raise ValueError(f"amount mismatch: {total}")


# --- order query (async — billing awaits provider.query_order) ---


@dataclass
class WechatQueryResult:
    out_trade_no: str
    trade_state: str
    raw_payload: dict


async def query_transaction(
    config: WechatPayConfig | None,
    *,
    out_trade_no: str,
) -> WechatQueryResult | None:
    if config is None or not out_trade_no:
        return None
    url_path = f"/v3/pay/transactions/out-trade-no/{out_trade_no}?mchid={config.mchid}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        response = await client.get(
            f"{config.api_base}{url_path}",
            headers=_request_headers(config, "GET", url_path, ""),
        )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    transaction = response.json() or {}
    trade_state = str(transaction.get("trade_state") or "").strip()
    if not trade_state:
        return None
    return WechatQueryResult(
        out_trade_no=str(transaction.get("out_trade_no") or out_trade_no),
        trade_state=trade_state,
        raw_payload=transaction,
    )
