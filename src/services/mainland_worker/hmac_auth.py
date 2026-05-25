"""Mainland Worker HMAC 签名协议（US 主机和武汉 worker 共享）。

plan §Worker API 通用请求头规范：

    X-AVT-Key-Id    HMAC key id（支持 key 轮换）
    X-AVT-Timestamp Unix seconds
    X-AVT-Nonce     随机 UUID
    X-AVT-Signature HMAC-SHA256
    X-AVT-Job-Id    job id（无 job 上下文的请求如 /healthz 可省）

签名材料（plan §签名内容）::

    method + "\\n" + path + "\\n" + timestamp + "\\n" + nonce + "\\n" + key_id + "\\n" + sha256(body_bytes)

Worker 端必须拒绝：

- 未知或已过期 ``X-AVT-Key-Id``
- 时间偏移超过 ``CLOCK_SKEW_SECONDS``（默认 300s）
- 15 分钟内重复 nonce（``NONCE_REPLAY_WINDOW_SECONDS``）
- body 超过 ``MAX_BODY_BYTES`` 上限
- 未配置 / 未启用对应 provider

KeyStore 协议（``HmacKeyStore``）允许新旧 key 短期并存：

- ``get_secret(key_id)`` 在 deprecated 窗口内仍返回 secret
- 超过窗口 ``get_secret()`` 返 ``None`` → verify 失败

NonceStore 协议是 in-memory（Phase 1）；Phase 4 多副本部署时可换成
Redis 后端（不在本方案范围内）。

设计约束：

- 本模块只依赖 stdlib（``hashlib`` / ``hmac`` / ``time``），不引入 fastapi /
  pydantic / httpx — 这样 worker 端和 client 端 import 不互相污染。
- ``SignatureMaterial`` 是冻结 dataclass，单元测试可以直接用一份 material
  在两端断言"同输入 → 同签名"。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from typing import Protocol


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 协议常量
# ---------------------------------------------------------------------------

# 请求头名（小写匹配，FastAPI / httpx 都不区分大小写但保留原 case 便于阅读）
HEADER_KEY_ID = "X-AVT-Key-Id"
HEADER_TIMESTAMP = "X-AVT-Timestamp"
HEADER_NONCE = "X-AVT-Nonce"
HEADER_SIGNATURE = "X-AVT-Signature"
HEADER_JOB_ID = "X-AVT-Job-Id"

# 时间偏移容忍（plan §Worker 必须拒绝）
CLOCK_SKEW_SECONDS = 300

# Nonce 重放窗口（15 分钟）
NONCE_REPLAY_WINDOW_SECONDS = 15 * 60

# Body 上限：clone sample 通过 URL 引用（不打进 body），TTS batch 文本量
# 即使 200 段 × 500 字也只是几百 KB。上限给 1 MB 已经远超实际需要，
# 防止误把 raw audio bytes 塞进 body。
MAX_BODY_BYTES = 1 * 1024 * 1024


class SignatureError(Exception):
    """签名验证失败。Worker 端转 4xx 返给 client。"""


# ---------------------------------------------------------------------------
# 签名计算
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SignatureMaterial:
    """签名输入。

    单元测试用同一份 material 在 sign/verify 两侧断言"同输入 → 同签名"。
    """
    method: str  # 大写：GET / POST / DELETE
    path: str    # 完整 path（不含 query），如 ``/cosyvoice/clone``
    timestamp: int
    nonce: str
    key_id: str
    body: bytes  # 原始 body bytes（GET / DELETE 无 body 时传 b""）

    def to_signing_string(self) -> str:
        body_hash = hashlib.sha256(self.body).hexdigest()
        return "\n".join([
            self.method.upper(),
            self.path,
            str(self.timestamp),
            self.nonce,
            self.key_id,
            body_hash,
        ])


def sign(material: SignatureMaterial, secret: str) -> str:
    """计算 HMAC-SHA256 签名（hex）。

    client 端调用此函数生成 ``X-AVT-Signature`` 头。
    """
    signing_string = material.to_signing_string()
    mac = hmac.new(
        secret.encode("utf-8"),
        signing_string.encode("utf-8"),
        hashlib.sha256,
    )
    return mac.hexdigest()


def verify_signature(material: SignatureMaterial, secret: str, provided: str) -> bool:
    """常量时间比较签名是否匹配。

    使用 ``hmac.compare_digest`` 防 timing attack。
    """
    expected = sign(material, secret)
    return hmac.compare_digest(expected, provided)


# ---------------------------------------------------------------------------
# Key store（plan §Secret Management 的 ``{key_id: hmac_secret}`` 表）
# ---------------------------------------------------------------------------

class HmacKeyStore(Protocol):
    """Worker 端持有的 key_id → secret 表。

    支持轮换语义：``deprecated_at`` 之前 ``get_secret()`` 返 secret，
    之后返 ``None`` → verify 失败。
    """

    def get_secret(self, key_id: str, now: int) -> str | None:
        """返回当前可用 secret；不存在或已过期返 None。"""
        ...


@dataclass(frozen=True, slots=True)
class HmacKey:
    """一条 HMAC key 记录。"""
    key_id: str
    secret: str
    deprecated_at: int | None = None  # Unix seconds；None 表示永不过期

    def is_usable_at(self, now: int) -> bool:
        if self.deprecated_at is None:
            return True
        return now < self.deprecated_at


class InMemoryHmacKeyStore:
    """In-memory ``HmacKeyStore`` 实现（Phase 1 默认）。

    Worker 启动时从 env 或配置文件加载 keys。轮换时调用 ``add_key()``
    增 + ``deprecate()`` 标记旧 key。
    """

    def __init__(self, keys: list[HmacKey]) -> None:
        if not keys:
            raise ValueError("InMemoryHmacKeyStore requires at least one key")
        self._keys: dict[str, HmacKey] = {k.key_id: k for k in keys}

    def get_secret(self, key_id: str, now: int) -> str | None:
        record = self._keys.get(key_id)
        if record is None:
            return None
        if not record.is_usable_at(now):
            return None
        return record.secret

    def add_key(self, key: HmacKey) -> None:
        self._keys[key.key_id] = key

    def deprecate(self, key_id: str, deprecated_at: int) -> None:
        existing = self._keys.get(key_id)
        if existing is None:
            return
        self._keys[key_id] = HmacKey(
            key_id=existing.key_id,
            secret=existing.secret,
            deprecated_at=deprecated_at,
        )


# ---------------------------------------------------------------------------
# Nonce store（防重放）
# ---------------------------------------------------------------------------

class NonceStore(Protocol):
    """Nonce 重放保护存储。"""

    def seen(self, nonce: str, now: int) -> bool:
        """返回 True 表示该 nonce 已在 ``NONCE_REPLAY_WINDOW_SECONDS`` 内
        被记录过；返回 False 表示首次见到（并已记录）。"""
        ...


class InMemoryNonceStore:
    """In-memory NonceStore（Phase 1）。

    维护 ``{nonce: first_seen_ts}``，每次 ``seen()`` 调用时顺手清理
    过期条目，避免内存无限增长。
    """

    def __init__(self, window_seconds: int = NONCE_REPLAY_WINDOW_SECONDS) -> None:
        self._window = window_seconds
        self._records: dict[str, int] = {}

    def seen(self, nonce: str, now: int) -> bool:
        # 顺手清理过期项
        if self._records:
            cutoff = now - self._window
            expired = [n for n, ts in self._records.items() if ts < cutoff]
            for n in expired:
                self._records.pop(n, None)

        if nonce in self._records:
            return True
        self._records[nonce] = now
        return False

    def __len__(self) -> int:
        return len(self._records)


# ---------------------------------------------------------------------------
# 完整的请求验证（worker 端中间件用）
# ---------------------------------------------------------------------------

def verify_request(
    *,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
    key_store: HmacKeyStore,
    nonce_store: NonceStore,
    now: int | None = None,
    max_body_bytes: int = MAX_BODY_BYTES,
    clock_skew_seconds: int = CLOCK_SKEW_SECONDS,
) -> None:
    """完整校验一条 worker 请求；不通过抛 ``SignatureError``。

    校验顺序（每条 plan §Worker 必须拒绝 列表对应一行）：

    1. body 不超 ``max_body_bytes``
    2. 必需头齐全：Key-Id / Timestamp / Nonce / Signature
    3. Timestamp 在 ``[now - skew, now + skew]`` 内
    4. Key-Id 在 store 内且未过期
    5. Signature 匹配
    6. Nonce 在 ``NONCE_REPLAY_WINDOW_SECONDS`` 内未见过

    顺序设计原因：先做廉价校验（size / 头齐全 / 时间窗），再做需要
    secret 的签名校验和需要写入的 nonce 校验。这样攻击者高频投递无效
    请求时不会污染 nonce store。
    """
    if now is None:
        now = int(time.time())

    if len(body) > max_body_bytes:
        raise SignatureError(
            f"body too large: {len(body)} > {max_body_bytes}"
        )

    # 头规范化（HTTP 头大小写不敏感；但传进来的字典假定 caller 已经
    # 用小写或保留原 case，verifier 两种都试）
    def _get(name: str) -> str | None:
        return headers.get(name) or headers.get(name.lower()) or headers.get(name.title())

    key_id = _get(HEADER_KEY_ID)
    ts_raw = _get(HEADER_TIMESTAMP)
    nonce = _get(HEADER_NONCE)
    signature = _get(HEADER_SIGNATURE)

    if not (key_id and ts_raw and nonce and signature):
        missing = [
            name for name, val in [
                (HEADER_KEY_ID, key_id),
                (HEADER_TIMESTAMP, ts_raw),
                (HEADER_NONCE, nonce),
                (HEADER_SIGNATURE, signature),
            ] if not val
        ]
        raise SignatureError(f"missing required headers: {missing}")

    try:
        ts = int(ts_raw)
    except (TypeError, ValueError) as exc:
        raise SignatureError(f"invalid timestamp: {ts_raw!r}") from exc

    if abs(ts - now) > clock_skew_seconds:
        raise SignatureError(
            f"timestamp out of window: ts={ts} now={now} skew={clock_skew_seconds}"
        )

    secret = key_store.get_secret(key_id, now)
    if secret is None:
        raise SignatureError(f"unknown or expired key_id: {key_id!r}")

    material = SignatureMaterial(
        method=method,
        path=path,
        timestamp=ts,
        nonce=nonce,
        key_id=key_id,
        body=body,
    )
    if not verify_signature(material, secret, signature):
        raise SignatureError("signature mismatch")

    # 注意：nonce 校验放到最后，签名通过后才记录，避免攻击者用乱签名
    # 投递大量 nonce 撑爆 store
    if nonce_store.seen(nonce, now):
        raise SignatureError(f"nonce replayed within window: {nonce!r}")
