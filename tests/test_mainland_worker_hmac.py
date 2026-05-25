"""HMAC 协议单元测试（plan §Worker API 通用请求头 + §Secret Management）。

覆盖：

1. sign / verify_signature 对称
2. SignatureMaterial → signing_string 形状（method + path + ts + nonce + key_id + sha256(body)）
3. KeyStore：active key 通过、deprecated key 在窗口外被拒
4. NonceStore：首次见到通过、重放被拒、过期自动清理
5. verify_request 完整路径：
   - 时间漂移 > 300s 拒
   - 缺头拒
   - key 不存在拒
   - 签名错误拒
   - body 过大拒
   - 顺序：签名先校验、nonce 校验后入店（攻击者乱签名不污染 nonce store）
"""
from __future__ import annotations

import hashlib

import pytest

from services.mainland_worker.hmac_auth import (
    CLOCK_SKEW_SECONDS,
    HmacKey,
    InMemoryHmacKeyStore,
    InMemoryNonceStore,
    MAX_BODY_BYTES,
    NONCE_REPLAY_WINDOW_SECONDS,
    SignatureError,
    SignatureMaterial,
    sign,
    verify_request,
    verify_signature,
)


# ---------------------------------------------------------------------------
# Sign / verify
# ---------------------------------------------------------------------------

def test_sign_verify_roundtrip_matches() -> None:
    material = SignatureMaterial(
        method="POST",
        path="/cosyvoice/clone",
        timestamp=1_700_000_000,
        nonce="abc",
        key_id="k1",
        body=b'{"job_id":"j1"}',
    )
    sig = sign(material, "secret")
    assert verify_signature(material, "secret", sig) is True


def test_sign_method_case_insensitive() -> None:
    upper = SignatureMaterial(method="POST", path="/x", timestamp=1, nonce="n", key_id="k", body=b"")
    lower = SignatureMaterial(method="post", path="/x", timestamp=1, nonce="n", key_id="k", body=b"")
    assert sign(upper, "s") == sign(lower, "s")


def test_sign_diverges_when_any_field_changes() -> None:
    base = SignatureMaterial(
        method="POST", path="/p", timestamp=10, nonce="n", key_id="k", body=b"body",
    )
    base_sig = sign(base, "s")

    variations = [
        ("method", base.__class__(method="GET", path=base.path, timestamp=base.timestamp,
                                  nonce=base.nonce, key_id=base.key_id, body=base.body)),
        ("path", base.__class__(method=base.method, path="/q", timestamp=base.timestamp,
                                nonce=base.nonce, key_id=base.key_id, body=base.body)),
        ("timestamp", base.__class__(method=base.method, path=base.path, timestamp=99,
                                     nonce=base.nonce, key_id=base.key_id, body=base.body)),
        ("nonce", base.__class__(method=base.method, path=base.path, timestamp=base.timestamp,
                                 nonce="n2", key_id=base.key_id, body=base.body)),
        ("key_id", base.__class__(method=base.method, path=base.path, timestamp=base.timestamp,
                                  nonce=base.nonce, key_id="k2", body=base.body)),
        ("body", base.__class__(method=base.method, path=base.path, timestamp=base.timestamp,
                                nonce=base.nonce, key_id=base.key_id, body=b"body2")),
        ("secret", base),  # 同 material, 不同 secret
    ]
    for label, variant in variations[:-1]:
        assert sign(variant, "s") != base_sig, f"sign should differ when {label} changes"
    assert sign(base, "s2") != base_sig


def test_signing_string_format_locks_protocol() -> None:
    """plan §签名内容 规定的签名字符串形状不能变。"""
    material = SignatureMaterial(
        method="POST",
        path="/cosyvoice/clone",
        timestamp=1700000000,
        nonce="N1",
        key_id="K1",
        body=b'{"x":1}',
    )
    expected_body_hash = hashlib.sha256(b'{"x":1}').hexdigest()
    expected = "\n".join(["POST", "/cosyvoice/clone", "1700000000", "N1", "K1", expected_body_hash])
    assert material.to_signing_string() == expected


# ---------------------------------------------------------------------------
# Key store
# ---------------------------------------------------------------------------

def test_key_store_active_key_returns_secret() -> None:
    store = InMemoryHmacKeyStore([HmacKey(key_id="k1", secret="s1")])
    assert store.get_secret("k1", now=1700000000) == "s1"


def test_key_store_unknown_key_returns_none() -> None:
    store = InMemoryHmacKeyStore([HmacKey(key_id="k1", secret="s1")])
    assert store.get_secret("unknown", now=1700000000) is None


def test_key_store_deprecated_key_within_window_returns_secret() -> None:
    store = InMemoryHmacKeyStore([
        HmacKey(key_id="k1", secret="s1", deprecated_at=1700000100),
    ])
    assert store.get_secret("k1", now=1700000050) == "s1"


def test_key_store_deprecated_key_after_window_returns_none() -> None:
    store = InMemoryHmacKeyStore([
        HmacKey(key_id="k1", secret="s1", deprecated_at=1700000100),
    ])
    assert store.get_secret("k1", now=1700000200) is None


def test_key_store_rotation_via_add_and_deprecate() -> None:
    store = InMemoryHmacKeyStore([HmacKey(key_id="k1", secret="s1")])
    store.add_key(HmacKey(key_id="k2", secret="s2"))
    store.deprecate("k1", deprecated_at=1700000100)

    # 在 deprecate 时间之前 k1 仍可用，k2 永久可用
    assert store.get_secret("k1", 1700000050) == "s1"
    assert store.get_secret("k2", 1700000050) == "s2"
    # 过期后 k1 不可用，k2 仍可用
    assert store.get_secret("k1", 1700000200) is None
    assert store.get_secret("k2", 1700000200) == "s2"


def test_key_store_rejects_empty_init() -> None:
    with pytest.raises(ValueError):
        InMemoryHmacKeyStore([])


# ---------------------------------------------------------------------------
# Nonce store
# ---------------------------------------------------------------------------

def test_nonce_store_first_seen_returns_false_then_true() -> None:
    store = InMemoryNonceStore()
    assert store.seen("n1", now=1000) is False
    assert store.seen("n1", now=1001) is True


def test_nonce_store_expires_after_window() -> None:
    store = InMemoryNonceStore(window_seconds=600)
    assert store.seen("n1", now=1000) is False
    # 第一次记录 1000；600s 后扫描清理；1700 时刚好过期
    assert store.seen("n1", now=1700) is False  # 视为新 nonce


def test_nonce_store_concurrent_nonces_independent() -> None:
    store = InMemoryNonceStore()
    assert store.seen("a", 100) is False
    assert store.seen("b", 100) is False
    assert store.seen("a", 100) is True
    assert store.seen("b", 100) is True


# ---------------------------------------------------------------------------
# verify_request 完整路径
# ---------------------------------------------------------------------------

def _make_signed_headers(
    *, method: str, path: str, body: bytes, ts: int, nonce: str, key_id: str, secret: str,
) -> dict[str, str]:
    material = SignatureMaterial(
        method=method, path=path, timestamp=ts, nonce=nonce, key_id=key_id, body=body,
    )
    return {
        "X-AVT-Key-Id": key_id,
        "X-AVT-Timestamp": str(ts),
        "X-AVT-Nonce": nonce,
        "X-AVT-Signature": sign(material, secret),
        "X-AVT-Job-Id": "job_test",
    }


def test_verify_request_ok_for_valid_signature() -> None:
    store = InMemoryHmacKeyStore([HmacKey(key_id="k1", secret="s1")])
    nonces = InMemoryNonceStore()
    headers = _make_signed_headers(
        method="POST", path="/cosyvoice/clone",
        body=b'{}', ts=1700000000, nonce="n1", key_id="k1", secret="s1",
    )
    verify_request(
        method="POST", path="/cosyvoice/clone",
        headers=headers, body=b'{}',
        key_store=store, nonce_store=nonces,
        now=1700000000,
    )


def test_verify_request_rejects_clock_skew_too_large() -> None:
    store = InMemoryHmacKeyStore([HmacKey(key_id="k1", secret="s1")])
    nonces = InMemoryNonceStore()
    headers = _make_signed_headers(
        method="POST", path="/p", body=b"",
        ts=1700000000, nonce="n", key_id="k1", secret="s1",
    )
    with pytest.raises(SignatureError, match="timestamp out of window"):
        verify_request(
            method="POST", path="/p",
            headers=headers, body=b"",
            key_store=store, nonce_store=nonces,
            now=1700000000 + CLOCK_SKEW_SECONDS + 1,
        )


def test_verify_request_rejects_missing_headers() -> None:
    store = InMemoryHmacKeyStore([HmacKey(key_id="k1", secret="s1")])
    nonces = InMemoryNonceStore()
    with pytest.raises(SignatureError, match="missing required headers"):
        verify_request(
            method="POST", path="/p",
            headers={"X-AVT-Key-Id": "k1"},  # 缺 timestamp / nonce / signature
            body=b"",
            key_store=store, nonce_store=nonces,
            now=1700000000,
        )


def test_verify_request_rejects_unknown_key_id() -> None:
    store = InMemoryHmacKeyStore([HmacKey(key_id="k1", secret="s1")])
    nonces = InMemoryNonceStore()
    headers = _make_signed_headers(
        method="POST", path="/p", body=b"",
        ts=1700000000, nonce="n", key_id="kX", secret="s1",
    )
    with pytest.raises(SignatureError, match="unknown or expired key_id"):
        verify_request(
            method="POST", path="/p",
            headers=headers, body=b"",
            key_store=store, nonce_store=nonces,
            now=1700000000,
        )


def test_verify_request_rejects_signature_mismatch() -> None:
    store = InMemoryHmacKeyStore([HmacKey(key_id="k1", secret="s1")])
    nonces = InMemoryNonceStore()
    headers = _make_signed_headers(
        method="POST", path="/p", body=b"good",
        ts=1700000000, nonce="n", key_id="k1", secret="s1",
    )
    # 篡改 body
    with pytest.raises(SignatureError, match="signature mismatch"):
        verify_request(
            method="POST", path="/p",
            headers=headers, body=b"tampered",
            key_store=store, nonce_store=nonces,
            now=1700000000,
        )


def test_verify_request_rejects_nonce_replay() -> None:
    store = InMemoryHmacKeyStore([HmacKey(key_id="k1", secret="s1")])
    nonces = InMemoryNonceStore()
    headers = _make_signed_headers(
        method="POST", path="/p", body=b"",
        ts=1700000000, nonce="n1", key_id="k1", secret="s1",
    )
    verify_request(
        method="POST", path="/p", headers=headers, body=b"",
        key_store=store, nonce_store=nonces, now=1700000000,
    )
    # 重放同一 nonce
    with pytest.raises(SignatureError, match="nonce replayed"):
        verify_request(
            method="POST", path="/p", headers=headers, body=b"",
            key_store=store, nonce_store=nonces, now=1700000000,
        )


def test_verify_request_rejects_body_too_large() -> None:
    store = InMemoryHmacKeyStore([HmacKey(key_id="k1", secret="s1")])
    nonces = InMemoryNonceStore()
    huge_body = b"x" * (MAX_BODY_BYTES + 1)
    with pytest.raises(SignatureError, match="body too large"):
        verify_request(
            method="POST", path="/p",
            headers={"X-AVT-Key-Id": "k1", "X-AVT-Timestamp": "0",
                     "X-AVT-Nonce": "n", "X-AVT-Signature": "x"},
            body=huge_body,
            key_store=store, nonce_store=nonces,
            now=1700000000,
        )


def test_verify_request_nonce_store_not_polluted_by_bad_signature() -> None:
    """关键安全属性：签名错误时，nonce **不应**被记录。

    plan §verify_request 顺序设计：先校验廉价字段 + 签名，最后才 touch
    nonce store。这样攻击者乱投签名不会撑爆 nonce 表。
    """
    store = InMemoryHmacKeyStore([HmacKey(key_id="k1", secret="s1")])
    nonces = InMemoryNonceStore()
    headers = _make_signed_headers(
        method="POST", path="/p", body=b"good",
        ts=1700000000, nonce="attacker_nonce", key_id="k1", secret="s1",
    )
    with pytest.raises(SignatureError):
        verify_request(
            method="POST", path="/p",
            headers=headers, body=b"tampered",  # 触发签名 mismatch
            key_store=store, nonce_store=nonces,
            now=1700000000,
        )
    assert len(nonces) == 0


def test_verify_request_invalid_timestamp_is_signature_error() -> None:
    store = InMemoryHmacKeyStore([HmacKey(key_id="k1", secret="s1")])
    nonces = InMemoryNonceStore()
    headers = {
        "X-AVT-Key-Id": "k1",
        "X-AVT-Timestamp": "not_a_number",
        "X-AVT-Nonce": "n",
        "X-AVT-Signature": "x",
    }
    with pytest.raises(SignatureError, match="invalid timestamp"):
        verify_request(
            method="POST", path="/p",
            headers=headers, body=b"",
            key_store=store, nonce_store=nonces,
            now=1700000000,
        )
