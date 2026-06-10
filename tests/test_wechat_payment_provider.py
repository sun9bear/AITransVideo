"""Tests for the WeChat Pay v3 Native provider (plan 2026-05-22 T9).

Covers: config gating, out_trade_no shape, native order creation (signed
request body facts: notify_url injection / AVT_ prefix / fen amount / attach
binding), callback signature verification with a real RSA keypair (valid /
tampered / stale-ts replay / serial mismatch), AES-256-GCM resource decrypt
roundtrip via parse, trade_state mapping, order-fact validation gates, and the
transaction query. Provider-agnostic settlement + idempotency are covered by
tests/test_billing*.py (WeChat reuses _process_payment_event unchanged).
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import sys
import time
import types
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_gateway_dir = str(
    __import__("pathlib").Path(__file__).resolve().parent.parent / "gateway"
)
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

import payment_provider_wechat as wechat  # noqa: E402
from payment_provider_wechat import (  # noqa: E402
    WechatPayConfig,
    build_out_trade_no,
    create_native_order,
    is_wechatpay_live_ready,
    map_wechat_trade_state,
    parse_wechat_webhook,
    query_transaction,
    validate_wechat_webhook_payload,
    verify_wechat_signature,
)
from payment_providers import WechatPayProvider  # noqa: E402

_APIV3_KEY = "0123456789abcdef0123456789abcdef"  # exactly 32 chars
_MCHID = "1745928268"

_WECHAT_ENV_VARS = (
    "AVT_WECHATPAY_ENABLED",
    "WECHATPAY_MCHID",
    "WECHATPAY_APIV3_KEY",
    "WECHATPAY_PRIVATE_KEY_PATH",
    "WECHATPAY_CERT_SERIAL",
    "WECHATPAY_PUB_KEY_ID",
    "WECHATPAY_PLATFORM_PUB_KEY_PATH",
    "WECHATPAY_NOTIFY_URL",
    "WECHATPAY_ORDER_PREFIX",
    "WECHATPAY_APPID",
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _gen_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_key, private_pem, public_pem


@pytest.fixture
def clean_wechat_env(monkeypatch):
    for var in _WECHAT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture
def wechat_setup(monkeypatch, tmp_path, clean_wechat_env):
    """Returns (config, platform_private_key) with env fully configured."""
    _, merchant_priv_pem, _ = _gen_keypair()
    platform_key, _, platform_pub_pem = _gen_keypair()
    priv_path = tmp_path / "apiclient_key.pem"
    pub_path = tmp_path / "pub_key.pem"
    priv_path.write_bytes(merchant_priv_pem)
    pub_path.write_bytes(platform_pub_pem)

    monkeypatch.setenv("AVT_WECHATPAY_ENABLED", "1")
    monkeypatch.setenv("WECHATPAY_MCHID", _MCHID)
    monkeypatch.setenv("WECHATPAY_APIV3_KEY", _APIV3_KEY)
    monkeypatch.setenv("WECHATPAY_PRIVATE_KEY_PATH", str(priv_path))
    monkeypatch.setenv("WECHATPAY_CERT_SERIAL", "ABCDEF0123456789")
    monkeypatch.setenv("WECHATPAY_PUB_KEY_ID", "PUB_KEY_ID_TEST_01")
    monkeypatch.setenv("WECHATPAY_PLATFORM_PUB_KEY_PATH", str(pub_path))
    config = WechatPayConfig.from_env()
    assert config is not None
    return config, platform_key


def _txn(order_id="ord-uuid-1", otn="AVT_abc", total=9900, state="SUCCESS"):
    return {
        "mchid": _MCHID,
        "out_trade_no": otn,
        "transaction_id": "4200001234202606100000000001",
        "trade_state": state,
        "attach": order_id,
        "amount": {"total": total, "payer_total": total, "currency": "CNY"},
    }


def _signed_envelope(config, platform_key, txn, *, ts=None, nonce="abc123nonce0"):
    """Build an encrypted+signed callback (body bytes, headers dict)."""
    plaintext = json.dumps(txn).encode("utf-8")
    resource_nonce = "n0n0n0n0n0n0"
    aead = AESGCM(config.apiv3_key)
    ciphertext = aead.encrypt(
        resource_nonce.encode("utf-8"), plaintext, b"transaction"
    )
    envelope = {
        "id": "evt-uuid-1",
        "event_type": "TRANSACTION.SUCCESS",
        "resource_type": "encrypt-resource",
        "resource": {
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            "nonce": resource_nonce,
            "associated_data": "transaction",
        },
    }
    body = json.dumps(envelope).encode("utf-8")
    timestamp = str(int(ts if ts is not None else time.time()))
    canonical = (
        timestamp.encode() + b"\n" + nonce.encode() + b"\n" + body + b"\n"
    )
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    signature = platform_key.sign(canonical, padding.PKCS1v15(), hashes.SHA256())
    headers = {
        "Wechatpay-Timestamp": timestamp,
        "Wechatpay-Nonce": nonce,
        "Wechatpay-Signature": base64.b64encode(signature).decode("ascii"),
        "Wechatpay-Serial": config.pub_key_id,
    }
    return body, headers


# --- config gating ---


class TestConfig:
    def test_disabled_returns_none(self, clean_wechat_env):
        assert WechatPayConfig.from_env() is None
        assert is_wechatpay_live_ready() is False

    def test_full_env_is_operational(self, wechat_setup):
        assert is_wechatpay_live_ready() is True
        assert WechatPayProvider().operational is True

    def test_bad_apiv3_key_length_not_ready(self, monkeypatch, wechat_setup):
        monkeypatch.setenv("WECHATPAY_APIV3_KEY", "tooshort")
        assert is_wechatpay_live_ready() is False

    def test_missing_key_file_not_ready(self, monkeypatch, wechat_setup, tmp_path):
        monkeypatch.setenv(
            "WECHATPAY_PRIVATE_KEY_PATH", str(tmp_path / "nope.pem")
        )
        assert is_wechatpay_live_ready() is False


# --- out_trade_no ---


class TestOutTradeNo:
    def test_prefix_and_length(self, wechat_setup):
        config, _ = wechat_setup
        otn = build_out_trade_no(config, "0c5984f8-3d3a-4a09-9d20-1234567890ab")
        assert otn.startswith("AVT_")
        assert len(otn) <= 32
        # deterministic — same order id, same otn (reconciliation key)
        assert otn == build_out_trade_no(config, "0c5984f8-3d3a-4a09-9d20-1234567890ab")


# --- native order creation ---


class _SyncResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _patch_sync_client(monkeypatch, payload, status_code=200, capture=None):
    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, content=None):
            if capture is not None:
                capture["url"] = url
                capture["headers"] = headers
                capture["content"] = content
            return _SyncResp(payload, status_code)

    monkeypatch.setattr(wechat.httpx, "Client", _Client)


class TestCreateNativeOrder:
    def test_request_facts(self, monkeypatch, wechat_setup):
        config, _ = wechat_setup
        capture = {}
        _patch_sync_client(
            monkeypatch, {"code_url": "weixin://wxpay/bizpayurl?pr=xyz"}, capture=capture
        )
        code_url, otn = create_native_order(
            config, order_id="ord-uuid-1", amount_fen=9900, description="AITrans 套餐 plus/monthly"
        )
        assert code_url.startswith("weixin://")
        body = json.loads(capture["content"])
        assert body["mchid"] == _MCHID
        assert body["out_trade_no"] == otn and otn.startswith("AVT_")
        # notify_url is injected per request (shared-mchid invariant)
        assert body["notify_url"] == config.notify_url
        assert "aitrans.video" in body["notify_url"]
        assert body["amount"] == {"total": 9900, "currency": "CNY"}
        assert body["attach"] == "ord-uuid-1"
        assert "appid" not in body  # not configured
        auth = capture["headers"]["Authorization"]
        assert re.match(
            r'^WECHATPAY2-SHA256-RSA2048 mchid="\d+",nonce_str="[0-9a-f]+",'
            r'signature="[A-Za-z0-9+/=]+",timestamp="\d+",serial_no=".+"$',
            auth,
        )

    def test_empty_code_url_raises(self, monkeypatch, wechat_setup):
        config, _ = wechat_setup
        _patch_sync_client(monkeypatch, {"code_url": ""})
        with pytest.raises(ValueError):
            create_native_order(
                config, order_id="o", amount_fen=100, description="x"
            )

    def test_http_error_raises_without_echoing_message(
        self, monkeypatch, wechat_setup
    ):
        config, _ = wechat_setup
        _patch_sync_client(
            monkeypatch,
            {"code": "PARAM_ERROR", "message": "secret-ish detail"},
            status_code=400,
        )
        with pytest.raises(ValueError) as exc_info:
            create_native_order(
                config, order_id="o", amount_fen=100, description="x"
            )
        assert "PARAM_ERROR" in str(exc_info.value)
        assert "secret-ish" not in str(exc_info.value)


# --- callback signature verification ---


class TestSignatureVerification:
    def test_valid_signature(self, wechat_setup):
        config, platform_key = wechat_setup
        body, headers = _signed_envelope(config, platform_key, _txn())
        assert verify_wechat_signature(config, body, headers) is True

    def test_tampered_body_rejected(self, wechat_setup):
        config, platform_key = wechat_setup
        body, headers = _signed_envelope(config, platform_key, _txn())
        assert verify_wechat_signature(config, body + b"x", headers) is False

    def test_stale_timestamp_rejected(self, wechat_setup):
        config, platform_key = wechat_setup
        body, headers = _signed_envelope(
            config, platform_key, _txn(), ts=time.time() - 3600
        )
        assert verify_wechat_signature(config, body, headers) is False

    def test_serial_mismatch_rejected(self, wechat_setup):
        config, platform_key = wechat_setup
        body, headers = _signed_envelope(config, platform_key, _txn())
        headers["Wechatpay-Serial"] = "PUB_KEY_ID_OTHER"
        assert verify_wechat_signature(config, body, headers) is False

    def test_missing_headers_rejected(self, wechat_setup):
        config, _ = wechat_setup
        assert verify_wechat_signature(config, b"{}", {}) is False

    def test_no_config_rejected(self):
        assert verify_wechat_signature(None, b"{}", {}) is False

    def test_lowercase_headers_accepted(self, wechat_setup):
        # FastAPI's dict(request.headers) lower-cases header names.
        config, platform_key = wechat_setup
        body, headers = _signed_envelope(config, platform_key, _txn())
        lowered = {k.lower(): v for k, v in headers.items()}
        assert verify_wechat_signature(config, body, lowered) is True


# --- decrypt + parse roundtrip ---


class TestParseWebhook:
    def test_roundtrip_success_event(self, wechat_setup):
        config, platform_key = wechat_setup
        body, _ = _signed_envelope(
            config, platform_key, _txn(order_id="ord-9", otn="AVT_xyz", total=29900)
        )
        parsed = parse_wechat_webhook(config, body)
        assert parsed.provider_event_id == "4200001234202606100000000001"
        assert parsed.event_type == "TRANSACTION.SUCCESS"
        assert parsed.order_id == "ord-9"
        assert parsed.out_trade_no == "AVT_xyz"
        assert parsed.new_status == "paid"
        assert parsed.transaction["amount"]["total"] == 29900

    def test_wrong_key_raises(self, wechat_setup, monkeypatch, tmp_path):
        config, platform_key = wechat_setup
        body, _ = _signed_envelope(config, platform_key, _txn())
        bad = WechatPayConfig(
            mchid=config.mchid,
            apiv3_key=b"x" * 32,
            private_key_pem=config.private_key_pem,
            cert_serial=config.cert_serial,
            pub_key_id=config.pub_key_id,
            platform_pub_key_pem=config.platform_pub_key_pem,
            notify_url=config.notify_url,
            order_prefix=config.order_prefix,
        )
        with pytest.raises(ValueError):
            parse_wechat_webhook(bad, body)

    def test_no_config_raises(self):
        with pytest.raises(ValueError):
            parse_wechat_webhook(None, b"{}")


# --- status mapping (event filtering) ---


class TestStatusMapping:
    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            ("SUCCESS", "paid"),
            ("NOTPAY", "pending"),
            ("USERPAYING", "pending"),
            ("CLOSED", "cancelled"),
            ("REVOKED", "cancelled"),
            ("PAYERROR", "failed"),
            ("REFUND", "refunded"),
            ("SOMETHING_NEW", "pending"),  # unknown never settles
            ("", "pending"),
        ],
    )
    def test_map(self, state, expected):
        assert map_wechat_trade_state(state) == expected


# --- order-fact validation gates ---


class TestPayloadValidation:
    def test_ok(self, wechat_setup):
        config, _ = wechat_setup
        validate_wechat_webhook_payload(
            config,
            _txn(order_id="ord-1", otn="AVT_a", total=9900),
            order_id="ord-1",
            amount_cny=9900,
            provider_order_id="AVT_a",
        )

    @pytest.mark.parametrize(
        ("mutate", "kwargs"),
        [
            (lambda t: t.update(attach="other"), {}),
            (lambda t: t.update(mchid="999"), {}),
            (lambda t: t["amount"].update(total=1), {}),
            (lambda t: t.update(out_trade_no="AVT_other"), {"provider_order_id": "AVT_a"}),
        ],
    )
    def test_mismatch_rejected(self, wechat_setup, mutate, kwargs):
        config, _ = wechat_setup
        txn = _txn(order_id="ord-1", otn="AVT_a", total=9900)
        mutate(txn)
        with pytest.raises(ValueError):
            validate_wechat_webhook_payload(
                config, txn, order_id="ord-1", amount_cny=9900, **kwargs
            )


# --- query ---


def _patch_async_client(monkeypatch, payload, status_code=200):
    code = status_code

    class _Resp:
        status_code = code

        def raise_for_status(self):
            if code >= 400:
                raise wechat.httpx.HTTPStatusError(
                    "e", request=MagicMock(), response=MagicMock()
                )

        def json(self):
            return payload

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return _Resp()

    monkeypatch.setattr(wechat.httpx, "AsyncClient", _Client)


class TestQueryTransaction:
    def test_success_flip(self, monkeypatch, wechat_setup):
        config, _ = wechat_setup
        _patch_async_client(monkeypatch, _txn(otn="AVT_q", state="SUCCESS"))
        result = _run(query_transaction(config, out_trade_no="AVT_q"))
        assert result is not None
        assert result.trade_state == "SUCCESS"
        assert map_wechat_trade_state(result.trade_state) == "paid"

    def test_404_returns_none(self, monkeypatch, wechat_setup):
        config, _ = wechat_setup
        _patch_async_client(monkeypatch, {}, status_code=404)
        assert _run(query_transaction(config, out_trade_no="AVT_x")) is None

    def test_no_otn_returns_none(self, wechat_setup):
        config, _ = wechat_setup
        assert _run(query_transaction(config, out_trade_no="")) is None


# --- provider adapter ---


class TestWechatPayProvider:
    def test_create_checkout_returns_qrcode_result(self, monkeypatch, wechat_setup):
        monkeypatch.setattr(
            wechat,
            "create_native_order",
            lambda cfg, *, order_id, amount_fen, description: (
                "weixin://wxpay/bizpayurl?pr=t",
                "AVT_t",
            ),
        )
        result = WechatPayProvider().create_checkout(
            order_id="o",
            amount_cny=9900,
            target_plan_code="plus",
            billing_period="monthly",
        )
        assert result.display_mode == "qrcode"
        assert result.qr_code_url == "weixin://wxpay/bizpayurl?pr=t"
        assert result.provider_order_id == "AVT_t"
        assert result.checkout_url == ""

    def test_create_checkout_unconfigured_raises(self, clean_wechat_env):
        with pytest.raises(NotImplementedError):
            WechatPayProvider().create_checkout(
                order_id="o",
                amount_cny=9900,
                target_plan_code="plus",
                billing_period="monthly",
            )

    def test_parse_webhook_via_provider(self, wechat_setup):
        config, platform_key = wechat_setup
        body, _ = _signed_envelope(config, platform_key, _txn(order_id="ord-2"))
        event = WechatPayProvider().parse_webhook(body)
        assert event.order_id == "ord-2"
        assert event.new_status == "paid"
        assert event.raw_payload["transaction"]["attach"] == "ord-2"
