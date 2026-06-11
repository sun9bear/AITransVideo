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

from anonymous_preview_quota import PgRateLimitCounterStore, hash_scope_key
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
    "ANON_PREVIEW_COUNTER_SCOPE",
]

logger = logging.getLogger(__name__)

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
    """Build an ``IntakeConfig`` from current ``settings``.

    ``temp_storage_available`` is determined by a live probe of the upload
    directory so the config accurately reflects filesystem state at call
    time.
    """
    if upload_root is None:
        upload_root = _resolve_project_root()

    storage_ok = _check_storage_health(upload_root)

    return IntakeConfig(
        max_upload_bytes=settings.anonymous_preview_max_upload_bytes,
        max_source_duration_seconds=settings.anonymous_preview_max_seconds,
        temp_upload_dir=upload_root / "uploads" / "anonymous" if upload_root else None,
        temp_storage_available=storage_ok,
        rate_limit_global_per_day=settings.anonymous_preview_cap_global_per_day,
        rate_limit_per_ip_per_day=settings.anonymous_preview_cap_per_ip,
        rate_limit_per_device_per_day=settings.anonymous_preview_cap_per_device,
        rate_limit_per_source_hash_per_day=settings.anonymous_preview_cap_per_source,
    )


# ---------------------------------------------------------------------------
# Counter-store factory
# ---------------------------------------------------------------------------

def _default_counter_store_factory(
    session: Session,
    scope: str,
    now: Optional[datetime] = None,
) -> PgRateLimitCounterStore:
    return PgRateLimitCounterStore(session, scope=scope, mode="free", now=now)


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
    counter_store = _factory(ANON_PREVIEW_COUNTER_SCOPE)

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

    # Persist record — RecordStoreError propagates to caller.
    store = PgPreviewRecordStore(db_session)
    store.save_record(record)
    logger.info(
        "anon_intake_saved preview_id=%s status=%s",
        record.record_id,
        record.status.value,
    )
    return record
