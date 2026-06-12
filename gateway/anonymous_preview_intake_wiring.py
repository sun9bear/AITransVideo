"""APF P0 — adapter wiring for anonymous preview intake (T3).

Assembles ``AnonymousPreviewBackendAdapter`` from production dependencies
and calls ``handle_intake``, then persists the resulting ``PreviewRecord``
via ``PgPreviewRecordStore``.

Injection model
---------------
The public entry point ``run_intake_and_save`` accepts:

* ``probe_fn`` and ``prescreen_fn`` as explicit keyword arguments so
  T7 can inject T4/T5 real implementations at router construction time.
  In T3 both default to ``_not_wired_*`` stubs that raise
  ``NotImplementedError``.
* ``counter_store_factory`` — a callable ``(scope: str) → CounterStore``
  that the wiring calls once per rate-limit scope.  Defaults to a factory
  that builds ``PgRateLimitCounterStore`` instances from the supplied
  SQLAlchemy session.

The wiring NEVER raises on adapter failure.  The contract guarantee is:
  * adapter failure  → status=FAILED ``PreviewRecord`` stored in DB.
  * store failure    → ``RecordStoreError`` propagated to caller (T7 logs
    it; the upload file is cleaned up by the upload handler).

Import constraints
------------------
* No ``services.jobs`` or ``src.pipeline`` (pydub guard).
* No FastAPI types — dependency injection wired at the router level.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy.orm import Session

from anonymous_preview_quota import PgRateLimitCounterStore, hash_scope_key, shanghai_today
from anonymous_preview_record_store import PgPreviewRecordStore, RecordStoreError
from config import settings

# src/ must be on sys.path (gateway container bind-mount, tests path setup).
from services.anonymous_preview_backend_adapter import (
    AnonymousPreviewBackendAdapter,
    RequestFacts,
    UploadFacts,
)
from services.anonymous_preview_intake import (
    IntakeConfig,
    PreviewRecord,
    PreviewStatus,
    ProbeResult,
    ComplianceResult,
    SourceType,
)

__all__ = [
    "run_intake_and_save",
    "build_intake_config",
    "build_scope_hasher",
    "peek_counter_keys",
    "peek_mode_counter_keys",
    "mode_scope_key",
    "express_subgate_key",
    "resolve_express_global_cap",
    "LaneAwareCounterStore",
    "ANON_PREVIEW_COUNTER_SCOPE",
    "PER_SCOPE_PER_MODE_DAILY_CAP",
]

logger = logging.getLogger(__name__)

# plan 2026-06-12 §B 第 2 层（D3）：per-scope per-mode 配额 = ip/device/
# source × {free,express} 各 1 次/日。常量而非旋钮——plan 未给 per-mode
# cap 提供 admin 字段；放宽需改 plan。
PER_SCOPE_PER_MODE_DAILY_CAP = 1

# anonymous_preview_daily_usage.scope 列的唯一合法值——权威计数器
# （run_intake_and_save → PgRateLimitCounterStore）和 AD-8 peek SELECT
# 必须用同一个常量；adapter 的维度区分（global/ip/device/source）在
# scope_key 前缀里，不在 scope 列。
ANON_PREVIEW_COUNTER_SCOPE = "anon_preview"


def build_scope_hasher(secret: str) -> Callable[[str, str], str]:
    """Return the (prefix, value) → HMAC hasher the adapter consumes.

    adapter 契约是 hasher(prefix, value) 两个位置参数；hash_scope_key 只收
    一个位置参数，所以把 prefix 并进被哈希材料：``f"{prefix}:{value}"``。
    这是权威计数器 scope_key 中哈希段的唯一推导落点——AD-8 peek 必须经
    ``peek_counter_keys`` 复用它，不得自行调 hash_scope_key（裸 IP 不带
    "ip:" 前缀哈希出来的 key 永远查不到行，cap 预检恒放行——2026-06-11
    e2e 冒烟同族 bug ⑤）。
    """

    def hasher(prefix: str, value: str) -> str:
        return hash_scope_key(f"{prefix}:{value}", secret=secret)

    return hasher


def peek_counter_keys(raw_ip: str, day_key: str, *, secret: str) -> tuple[str, str]:
    """Return ``(global_scope_key, ip_scope_key)`` exactly as the adapter writes.

    形状与 ``AnonymousPreviewBackendAdapter._enforce_rate_limits`` 的复合键
    严格对齐：``f"global:{day_key}"`` / ``f"ip:{hasher('ip', raw_ip)}:{day_key}"``。
    回归守卫：tests/test_anonymous_preview_upload_peek.py::
    TestPeekKeyDerivationConsistency 用 recording counter store 跑真实
    intake 路径，断言两侧推导逐字节一致——改任一侧形状会 red。
    """
    hasher = build_scope_hasher(secret)
    return (
        f"global:{day_key}",
        f"ip:{hasher('ip', raw_ip)}:{day_key}",
    )


def mode_scope_key(base_key: str, lane: str) -> str:
    """per-mode 计数行的 scope_key（plan 2026-06-12 §B 第 2 层）。

    形状 = 既有复合键 + ``:mode:{lane}`` 后缀；同时该行的 mode 列落 lane
    （belt & suspenders——unique index (scope, scope_key, mode, usage_date)
    单凭 mode 列已可区分，但 AD-8 bug ⑤ 的教训是 key 必须自描述、单点推导）。
    本函数是唯一推导落点：LaneAwareCounterStore 与 AD-8 peek
    （peek_mode_counter_keys）都必须经它，不得自行拼接。
    """
    return f"{base_key}:mode:{lane}"


def express_subgate_key(day_key: str) -> str:
    """express 全局子闸计数行的 scope_key（plan §B 第 3 层）。"""
    return mode_scope_key(f"global:{day_key}", "express")


def peek_mode_counter_keys(
    raw_ip: str, day_key: str, lane: str, *, secret: str
) -> tuple[str, str]:
    """AD-8 peek 的 lane 维度键：``(express 子闸 key, per-mode ip key)``。

    与权威侧（LaneAwareCounterStore._companion）共用 mode_scope_key 推导，
    保证两侧逐字节一致。回归守卫：tests/test_anonymous_express_t2_quota_layers
    ::TestKeyShapes::test_peek_mode_counter_keys_match_wrapper_derivation。
    """
    _global_key, ip_key = peek_counter_keys(raw_ip, day_key, secret=secret)
    return (express_subgate_key(day_key), mode_scope_key(ip_key, lane))


def resolve_express_global_cap() -> int:
    """express 全局子闸 cap（admin anonymous_express_daily_global_cap）。

    读取任何异常 → fail-closed 0（子闸恒拒）。正常情况下到不了这里：
    admin 不可读时 lane resolver 已 fail-closed None，根本不会有 express
    intake；这是防御纵深。
    """
    try:
        from admin_settings import load_settings

        return int(load_settings().anonymous_express_daily_global_cap)
    except Exception as exc:  # noqa: BLE001 — fail-closed
        logger.warning(
            "anon_intake: failed to read anonymous_express_daily_global_cap "
            "— fail-closed cap=0: %s", exc,
        )
        return 0


class LaneAwareCounterStore:
    """配额三层叠加 store（plan 2026-06-12 §B v4：既有不动、叠加新增）。

    包装两个 ``CounterStore``：

    * ``legacy_store`` — 既有计数器。所有 key/cap **原样透传**（key 形状、
      写入路径逐字节不变），每次 intake 无论 lane 照常累加 = 天然跨 lane
      总闸（global 500 / per-ip 3 / per-device 1 / per-source 1）。
    * ``mode_store`` — per-mode 并行计数行（scope 相同、mode 列 = lane、
      scope_key 带 ``:mode:{lane}`` 后缀）。

    叠加规则（adapter 对每个 key 调 try_acquire 时）：

    * ``global:{day}``：legacy 先过（总闸）；lane=express 再过 express
      全局子闸（cap=anonymous_express_daily_global_cap）。free 无子闸。
    * ``ip:/device:/source:``：legacy 先过（既有 per-scope cap）；再过
      per-mode 行（cap=PER_SCOPE_PER_MODE_DAILY_CAP=1）。
    * 任一层拒 → 本次已 acquire 的行回滚（拒绝不落计数）；adapter 的
      多 key 回滚（decrement）双行同步回退。

    零 SUM：每层都是独立行上的 increment-and-check（复用
    PgRateLimitCounterStore 的原子 upsert），无跨行聚合。
    """

    def __init__(
        self,
        legacy_store,
        mode_store,
        *,
        lane: str,
        express_global_cap: int,
    ) -> None:
        self._legacy = legacy_store
        self._mode = mode_store
        self._lane = lane
        self._express_cap = int(express_global_cap)
        # plan §E 配额退还需要的可退行清单：本次 intake 实际持有的
        # per-scope per-mode 行（ip/device/source × lane）。**刻意不含**
        # express 全局子闸与 legacy 总闸行（失败不退，防刷失败穿透成本闸）。
        # decrement 回滚时同步移除——清单始终反映"仍被占用"的行。
        self.acquired_mode_scope_keys: list[str] = []

    def _companion(self, key: str):
        """返回 (companion_key, cap) 或 None（无叠加层）。"""
        if key.startswith("global:"):
            if self._lane == "express":
                return (mode_scope_key(key, "express"), self._express_cap)
            return None
        return (mode_scope_key(key, self._lane), PER_SCOPE_PER_MODE_DAILY_CAP)

    def _safe_legacy_decrement(self, key: str) -> None:
        try:
            self._legacy.decrement(key)
        except Exception:  # noqa: BLE001 — 回滚是 best-effort（同 adapter 语义）
            logger.warning(
                "anon_quota: legacy decrement failed during companion "
                "rollback key=%.32s", key,
            )

    def try_acquire(self, key: str, cap: int):
        ok, count = self._legacy.try_acquire(key, cap)
        if not ok:
            return (False, count)
        comp = self._companion(key)
        if comp is None:
            return (True, count)
        comp_key, comp_cap = comp
        try:
            comp_ok, comp_count = self._mode.try_acquire(comp_key, comp_cap)
        except BaseException:
            # mode 行异常：回滚 legacy（拒绝不落计数），异常原样抛给
            # adapter → fail-closed FAILED record。
            self._safe_legacy_decrement(key)
            raise
        if not comp_ok:
            self._safe_legacy_decrement(key)
            return (False, comp_count)
        if not key.startswith("global:"):
            # 仅 per-scope per-mode 行可退（§E）；global 子闸不进清单。
            self.acquired_mode_scope_keys.append(comp_key)
        return (True, count)

    def get(self, key: str) -> int:
        return self._legacy.get(key)

    def peek(self, key: str) -> int:
        return self._legacy.get(key)

    def increment(self, key: str) -> int:
        # adapter admission 路径不走 increment（只 try_acquire）；保留协议
        # 完整性：双行同步加。
        n = self._legacy.increment(key)
        comp = self._companion(key)
        if comp is not None:
            try:
                self._mode.increment(comp[0])
            except Exception:  # noqa: BLE001
                pass
        return n

    def decrement(self, key: str) -> int:
        """adapter 多 key 回滚 / T5 配额退还路径：双行同步回退。"""
        try:
            n = self._legacy.decrement(key)
        except Exception:  # noqa: BLE001 — adapter 对 decrement 本就吞错
            n = 0
        comp = self._companion(key)
        if comp is not None:
            try:
                self._mode.decrement(comp[0])
            except Exception:  # noqa: BLE001
                pass
            if comp[0] in self.acquired_mode_scope_keys:
                self.acquired_mode_scope_keys.remove(comp[0])
        return n


# ---------------------------------------------------------------------------
# Protocol-stub placeholders (T4/T5 will inject real implementations)
# ---------------------------------------------------------------------------

def _not_wired_probe(upload_facts: UploadFacts) -> ProbeResult:  # noqa: ARG001
    """Placeholder probe fn.  Raises ``NotImplementedError``; T4 wires the
    real ffmpeg probe.  The adapter catches this and returns a FAILED record.
    """
    raise NotImplementedError(
        "_not_wired_probe: T4 probe fn not yet wired.  "
        "Pass a real probe_fn to run_intake_and_save()."
    )


def _not_wired_prescreen(probe_result: ProbeResult) -> ComplianceResult:  # noqa: ARG001
    """Placeholder compliance pre-screen fn.  Raises ``NotImplementedError``;
    T5 wires the real local-rules prescreen.  The adapter catches this and
    returns a FAILED record.
    """
    raise NotImplementedError(
        "_not_wired_prescreen: T5 compliance fn not yet wired.  "
        "Pass a real prescreen_fn to run_intake_and_save()."
    )


# ---------------------------------------------------------------------------
# Storage health check (AD-9 table: anonymous_preview_storage_health)
# ---------------------------------------------------------------------------

def _check_storage_health(upload_root: Optional[Path]) -> bool:
    """Return True if the anonymous upload root is writable.

    Probes by attempting to create ``uploads/anonymous/`` (no-op if it
    already exists) and writing a zero-byte sentinel.  Any OS error → False
    (fail-closed per AD-9).
    """
    if upload_root is None:
        return False
    try:
        probe_dir = upload_root / "uploads" / "anonymous"
        probe_dir.mkdir(parents=True, exist_ok=True)
        sentinel = probe_dir / ".health_probe"
        sentinel.touch()
        sentinel.unlink(missing_ok=True)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# IntakeConfig builder
# ---------------------------------------------------------------------------

def _resolve_project_root() -> Optional[Path]:
    import os
    raw = (
        os.environ.get("AIVIDEOTRANS_PROJECTS_DIR", "")
        or os.environ.get("AIVIDEOTRANS_PROJECT_ROOT", "")
    )
    if raw:
        return Path(raw).resolve(strict=False)
    return Path("/opt/aivideotrans/app").resolve(strict=False)


def build_intake_config(*, upload_root: Optional[Path] = None) -> IntakeConfig:
    """Build an ``IntakeConfig`` from the resolved APF limits.

    限制数值（2026-06-11 起）经 ``resolve_apf_limits()`` 取 admin 热配置、
    任何异常回落 env settings。本函数在 ``run_intake_and_save`` 内 per-request
    调用，所以 admin 改值后下一个请求即生效（热生效，无需重启）。

    ``temp_storage_available`` is determined by a live probe of the upload
    directory so the config accurately reflects filesystem state at call
    time.
    """
    from anonymous_preview_limits import resolve_apf_limits

    if upload_root is None:
        upload_root = _resolve_project_root()

    storage_ok = _check_storage_health(upload_root)
    limits = resolve_apf_limits()

    return IntakeConfig(
        max_upload_bytes=limits.anonymous_preview_max_upload_bytes,
        max_source_duration_seconds=limits.anonymous_preview_max_seconds,
        temp_upload_dir=upload_root / "uploads" / "anonymous" if upload_root else None,
        temp_storage_available=storage_ok,
        rate_limit_global_per_day=limits.anonymous_preview_cap_global_per_day,
        rate_limit_per_ip_per_day=limits.anonymous_preview_cap_per_ip,
        rate_limit_per_device_per_day=limits.anonymous_preview_cap_per_device,
        rate_limit_per_source_hash_per_day=limits.anonymous_preview_cap_per_source,
    )


# ---------------------------------------------------------------------------
# Counter-store factory
# ---------------------------------------------------------------------------

def _default_counter_store_factory(
    session: Session,
    scope: str,
    now: Optional[datetime] = None,
    mode: str = "free",
) -> PgRateLimitCounterStore:
    """mode 默认 "free"：既有（legacy/总闸）计数行的 mode 列恒为 'free'
    ——这是历史 key 形状的一部分（§B 第 1 层"逐字节不变"），express
    intake 也照常写这些行。per-mode 行才用 mode=lane（§B 第 2/3 层）。"""
    return PgRateLimitCounterStore(session, scope=scope, mode=mode, now=now)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_intake_and_save(
    *,
    db_session: Session,
    request_facts: RequestFacts,
    upload_facts: Optional[UploadFacts],
    probe_fn: Callable = _not_wired_probe,
    prescreen_fn: Callable = _not_wired_prescreen,
    counter_store_factory: Optional[Callable] = None,
    upload_root: Optional[Path] = None,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    mode: str = "free",
) -> PreviewRecord:
    """Assemble and run the adapter, then persist the resulting record.

    On adapter failure the resulting ``PreviewRecord`` has
    ``status=FAILED`` (or ``RATE_LIMITED`` / ``REJECTED``); this is
    stored and returned — no exception is raised.

    On store failure (``RecordStoreError``) the exception IS propagated
    so the upload handler can clean up the file and return a 503.

    Parameters
    ----------
    db_session:
        Open SQLAlchemy ``Session``; the caller commits/rolls back.
    request_facts:
        Constructed by the router from the incoming HTTP request.
    upload_facts:
        ``None`` if upload failed before facts were available.
    probe_fn:
        Injected probe callable (T4 will provide real implementation).
        Defaults to stub that raises ``NotImplementedError`` → FAILED record.
    prescreen_fn:
        Injected compliance pre-screen callable (T5 real implementation).
        Defaults to stub → FAILED record.
    counter_store_factory:
        ``(scope: str) → CounterStore``.  Defaults to PgRateLimitCounterStore.
    upload_root:
        Override for the project root (for tests).
    now_fn:
        Clock override (for tests).
    mode:
        lane 锁定值（plan 2026-06-12 §A）："free" / "express"。调用方在
        intake 时由 ``resolve_anonymous_lane()`` 解析（普通上传）或从
        chunked upload state 读取（init 时锁定）。持久化前经
        ``dataclasses.replace`` 写进契约 record → ``_to_orm`` 落 mode 列。
        默认 "free" 保持既有调用点行为不变。

    Returns
    -------
    ``PreviewRecord``
    """
    # Build HMAC hasher from settings secret.
    # The adapter calls hasher(prefix, value) — two positional args —
    # where prefix disambiguates the scope ("sess", "ip", "dev").
    # 推导逻辑在 build_scope_hasher（模块级共享落点）——AD-8 peek 经
    # peek_counter_keys 复用同一函数，保证两侧 key 逐字节一致。
    hasher = build_scope_hasher(settings.anonymous_preview_hash_secret)

    # Build single counter store (adapter uses all four scopes via key prefix).
    _injected_factory = counter_store_factory is not None
    if counter_store_factory is None:
        _factory = partial(
            _default_counter_store_factory,
            db_session,
            now=now_fn(),
        )
    else:
        _factory = counter_store_factory

    # The adapter's _enforce_rate_limits calls try_acquire with composite
    # key strings (e.g. "global:2026-06-10", "ip:<hash>:2026-06-10").
    # PgRateLimitCounterStore uses scope to filter rows; we build one store
    # instance that handles ALL scope prefixes by routing on the key prefix.
    # Simplest approach: build a single store with scope="anon_preview" and
    # let the key carry the discriminator.  This matches the T2 schema where
    # the unique index is (scope, scope_key, mode, usage_date) — the adapter
    # already includes scope name in the key string via "global:", "ip:", etc.
    # so using scope="anon_preview" gives distinct rows per type.
    #
    # However, the _enforce_rate_limits constructs keys like
    # f"global:{day_key}" and calls try_acquire on those keys.  The PG store
    # stores them in scope_key column while scope column = our constructor arg.
    # Using scope=ANON_PREVIEW_COUNTER_SCOPE and letting the adapter key carry
    # the discriminator is exactly right.
    legacy_store = _factory(ANON_PREVIEW_COUNTER_SCOPE)

    # plan 2026-06-12 §B：legacy 层之上叠加 per-mode 层。
    # * 生产默认路径：mode 行用独立 PgRateLimitCounterStore（mode 列 = lane，
    #   scope 同值；行靠 scope_key 的 :mode: 后缀 + mode 列双重区分）。
    # * 注入 factory（测试）：同一 store 实例承载双行（factory 只调一次，
    #   保住 TestPeekKeyDerivationConsistency 的 scope 断言；key 后缀已足够
    #   区分行）。
    if _injected_factory:
        lane_mode_store = legacy_store
    else:
        lane_mode_store = _default_counter_store_factory(
            db_session, ANON_PREVIEW_COUNTER_SCOPE, now=now_fn(), mode=mode
        )
    counter_store = LaneAwareCounterStore(
        legacy_store,
        lane_mode_store,
        lane=mode,
        express_global_cap=(
            resolve_express_global_cap() if mode == "express" else 0
        ),
    )

    # Build config.
    intake_config = build_intake_config(upload_root=upload_root)

    adapter = AnonymousPreviewBackendAdapter(
        config=intake_config,
        counter_store=counter_store,
        probe_fn=probe_fn,
        compliance_fn=prescreen_fn,
        hasher=hasher,
        now_fn=now_fn,
    )

    # Run intake — adapter NEVER raises; failure → status-only record.
    record = adapter.handle_intake(request_facts, upload_facts)

    # lane 落盘（plan 2026-06-12 §A）：纯 intake/adapter 不感知 lane，
    # 在持久化边界统一盖上 mode。失败 record 也盖——审计/监控按 lane 维度
    # 统计需要拒绝行也带 mode。
    import dataclasses as _dc

    record = _dc.replace(record, mode=mode)

    # plan §E 配额退还锚点：把本次 intake 实际持有的 per-scope per-mode
    # 行（HMAC 复合键，无原始 PII）落进 record audit——Pass 3 诚实失败时
    # gateway 终态镜像凭它精确退还（global 总闸与 express 子闸不在清单）。
    if counter_store.acquired_mode_scope_keys:
        _day_key = shanghai_today(now_fn())
        _meta = dict(record.compliance_audit_metadata or {})
        _meta["quota_mode_rows"] = [
            {"scope_key": k, "mode": mode, "day": _day_key}
            for k in counter_store.acquired_mode_scope_keys
        ]
        record = _dc.replace(record, compliance_audit_metadata=_meta)

    # Persist record — RecordStoreError propagates to caller.
    store = PgPreviewRecordStore(db_session)
    store.save_record(record)
    logger.info(
        "anon_intake_saved preview_id=%s status=%s mode=%s",
        record.record_id,
        record.status.value,
        mode,
    )
    return record
