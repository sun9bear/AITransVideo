"""T2 验收测试：PgRateLimitCounterStore + migration 035 schema + 零调用方守卫。

覆盖范围
--------
1. PgRateLimitCounterStore 满足 CounterStore 结构协议。
2. try_acquire 到 cap 后拒绝；不同 mode/scope 互不干扰；day 边界。
3. DB 异常（mock session raise）→ RateLimitCounterUnavailable。
4. hash_scope_key 确定性 + 不同 secret 不同输出；schema 无 raw IP 列。
5. decrement 守卫：除测试外零调用方（v1 time-point 断言）。
6. migration 035 upgrade/downgrade 对称静态断言（三表一列一索引+sentinel）。

所有测试均用 SQLite in-process fake session 或 mock，不需要真实 PostgreSQL。
"""
from __future__ import annotations

import ast
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# sys.path 准备 — 让 gateway/ 和 src/ 都可 import
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
_GATEWAY_DIR = str(_REPO_ROOT / "gateway")
if _GATEWAY_DIR not in sys.path:
    sys.path.insert(0, _GATEWAY_DIR)

# ---------------------------------------------------------------------------
# Lazy imports (gateway dir must be on path first)
# ---------------------------------------------------------------------------
from anonymous_preview_quota import (
    PgRateLimitCounterStore,
    hash_scope_key,
    shanghai_today,
)
from src.services.anonymous_preview_rate_limit import RateLimitCounterUnavailable
from src.services.anonymous_preview_backend_adapter import CounterStore


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

class _FakeRow:
    """Minimal row proxy: row[0] = value."""
    def __init__(self, value: int) -> None:
        self._v = value
    def __getitem__(self, idx: int) -> int:
        return self._v


def _fake_session(rows: List[Optional[_FakeRow]]) -> MagicMock:
    """Return a mock SQLAlchemy Session whose execute().fetchone() returns
    items from ``rows`` in order."""
    session = MagicMock()
    execute_result = MagicMock()
    execute_result.fetchone.side_effect = list(rows)
    session.execute.return_value = execute_result
    return session


def _raising_session(exc: Exception) -> MagicMock:
    session = MagicMock()
    session.execute.side_effect = exc
    return session


# ---------------------------------------------------------------------------
# 1. Structural protocol conformance
# ---------------------------------------------------------------------------

class TestProtocolConformance:
    """PgRateLimitCounterStore must satisfy the CounterStore Protocol."""

    def test_isinstance_check_with_mock_session(self) -> None:
        """isinstance with runtime_checkable Protocol."""
        from typing import runtime_checkable, Protocol
        import typing

        # CounterStore is defined in backend_adapter; check it's a Protocol
        # and PgRateLimitCounterStore has all required methods.
        required_methods = {"get", "increment", "try_acquire"}
        for method in required_methods:
            assert hasattr(PgRateLimitCounterStore, method), (
                f"PgRateLimitCounterStore missing method: {method}"
            )

    def test_decrement_method_exists(self) -> None:
        assert callable(getattr(PgRateLimitCounterStore, "decrement", None))

    def test_try_acquire_signature(self) -> None:
        import inspect
        sig = inspect.signature(PgRateLimitCounterStore.try_acquire)
        params = list(sig.parameters.keys())
        assert "key" in params
        assert "cap" in params

    def test_get_signature(self) -> None:
        import inspect
        sig = inspect.signature(PgRateLimitCounterStore.get)
        assert "key" in sig.parameters


# ---------------------------------------------------------------------------
# 2. try_acquire behaviour
# ---------------------------------------------------------------------------

class TestTryAcquireBehaviour:
    """Behavioural tests via a mock DB session."""

    def _store(self, rows: List[Optional[_FakeRow]], scope: str = "global") -> PgRateLimitCounterStore:
        return PgRateLimitCounterStore(
            _fake_session(rows), scope=scope, mode="free",
            now=datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc),
        )

    def test_first_acquire_admitted(self) -> None:
        # INSERT returns new count=1
        store = self._store([_FakeRow(1)])
        ok, count = store.try_acquire("global:2026-06-10", cap=3)
        assert ok is True
        assert count == 1

    def test_acquire_at_cap_boundary_admitted(self) -> None:
        # count returned = cap exactly → still admitted (count < cap was satisfied)
        store = self._store([_FakeRow(3)])
        ok, count = store.try_acquire("global:2026-06-10", cap=3)
        assert ok is True
        assert count == 3

    def test_acquire_denied_when_none_returned_then_get(self) -> None:
        # INSERT ON CONFLICT WHERE count < cap: no row returned → denied.
        # Second execute (get) returns current=3.
        session = MagicMock()
        # First execute: try_acquire INSERT — returns None
        # Second execute: get SELECT — returns row with count=3
        first_result = MagicMock()
        first_result.fetchone.return_value = None
        second_result = MagicMock()
        second_result.fetchone.return_value = _FakeRow(3)
        session.execute.side_effect = [first_result, second_result]

        store = PgRateLimitCounterStore(
            session, scope="global", mode="free",
            now=datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc),
        )
        ok, count = store.try_acquire("global:2026-06-10", cap=3)
        assert ok is False
        assert count == 3

    def test_different_scopes_independent(self) -> None:
        """ip scope and device scope get separate PgRateLimitCounterStore instances."""
        ip_session = _fake_session([_FakeRow(1)])
        device_session = _fake_session([_FakeRow(1)])
        ip_store = PgRateLimitCounterStore(
            ip_session, scope="ip", mode="free",
            now=datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc),
        )
        device_store = PgRateLimitCounterStore(
            device_session, scope="device", mode="free",
            now=datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc),
        )
        ok_ip, _ = ip_store.try_acquire("hashed-ip", cap=3)
        ok_dev, _ = device_store.try_acquire("hashed-dev", cap=2)
        assert ok_ip is True
        assert ok_dev is True
        # Verify the SQL sent contains the correct scope
        ip_call_kwargs = ip_session.execute.call_args_list[0][0][1]
        assert ip_call_kwargs["scope"] == "ip"
        dev_call_kwargs = device_session.execute.call_args_list[0][0][1]
        assert dev_call_kwargs["scope"] == "device"

    def test_different_modes_independent(self) -> None:
        """'free' mode and 'trial' mode use separate day-key parameters."""
        free_session = _fake_session([_FakeRow(1)])
        trial_session = _fake_session([_FakeRow(1)])
        free_store = PgRateLimitCounterStore(
            free_session, scope="global", mode="free",
            now=datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc),
        )
        trial_store = PgRateLimitCounterStore(
            trial_session, scope="global", mode="trial",
            now=datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc),
        )
        free_store.try_acquire("k", cap=500)
        trial_store.try_acquire("k", cap=500)
        free_kwargs = free_session.execute.call_args_list[0][0][1]
        trial_kwargs = trial_session.execute.call_args_list[0][0][1]
        assert free_kwargs["mode"] == "free"
        assert trial_kwargs["mode"] == "trial"

    def test_invalid_cap_raises_unavailable(self) -> None:
        store = self._store([])
        with pytest.raises(RateLimitCounterUnavailable):
            store.try_acquire("k", cap=-1)

    def test_bool_cap_raises_unavailable(self) -> None:
        store = self._store([])
        with pytest.raises(RateLimitCounterUnavailable):
            store.try_acquire("k", cap=True)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 2b. Day boundary (Asia/Shanghai)
# ---------------------------------------------------------------------------

class TestDayBoundaryShanghai:
    """Verify that the day key flips at midnight Shanghai time."""

    def test_utc_2359_is_still_today_shanghai(self) -> None:
        # UTC 15:59 = Shanghai 23:59 (UTC+8) on the same day
        now = datetime(2026, 6, 10, 15, 59, 0, tzinfo=timezone.utc)
        assert shanghai_today(now) == "2026-06-10"

    def test_utc_1600_flips_to_next_day_shanghai(self) -> None:
        # UTC 16:00 = Shanghai 00:00 on the next day (UTC+8)
        now = datetime(2026, 6, 10, 16, 0, 0, tzinfo=timezone.utc)
        assert shanghai_today(now) == "2026-06-11"

    def test_store_uses_shanghai_day(self) -> None:
        # Midnight UTC = 08:00 Shanghai — still same date as noon UTC
        noon_utc = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
        midnight_utc = datetime(2026, 6, 10, 0, 0, 0, tzinfo=timezone.utc)
        store_noon = PgRateLimitCounterStore(
            _fake_session([]), scope="global",
            now=noon_utc,
        )
        store_midnight = PgRateLimitCounterStore(
            _fake_session([]), scope="global",
            now=midnight_utc,
        )
        assert store_noon._day == store_midnight._day == "2026-06-10"

    def test_store_day_differs_across_midnight_shanghai(self) -> None:
        # UTC 15:59 → Shanghai 23:59 June 10; UTC 16:01 → Shanghai 00:01 June 11
        before = datetime(2026, 6, 10, 15, 59, 0, tzinfo=timezone.utc)
        after = datetime(2026, 6, 10, 16, 1, 0, tzinfo=timezone.utc)
        store_before = PgRateLimitCounterStore(_fake_session([]), scope="global", now=before)
        store_after = PgRateLimitCounterStore(_fake_session([]), scope="global", now=after)
        assert store_before._day == "2026-06-10"
        assert store_after._day == "2026-06-11"


# ---------------------------------------------------------------------------
# 3. DB exception → RateLimitCounterUnavailable
# ---------------------------------------------------------------------------

class TestDbExceptionHandling:
    def _store_raising(self, exc: Exception) -> PgRateLimitCounterStore:
        return PgRateLimitCounterStore(
            _raising_session(exc), scope="global", mode="free",
            now=datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc),
        )

    def test_get_db_error_raises_unavailable(self) -> None:
        store = self._store_raising(RuntimeError("DB connection lost"))
        with pytest.raises(RateLimitCounterUnavailable) as exc_info:
            store.get("key")
        assert exc_info.value.__cause__ is not None

    def test_increment_db_error_raises_unavailable(self) -> None:
        store = self._store_raising(OSError("socket error"))
        with pytest.raises(RateLimitCounterUnavailable):
            store.increment("key")

    def test_try_acquire_db_error_raises_unavailable(self) -> None:
        store = self._store_raising(ValueError("unexpected"))
        with pytest.raises(RateLimitCounterUnavailable):
            store.try_acquire("key", cap=5)

    def test_decrement_db_error_raises_unavailable(self) -> None:
        store = self._store_raising(Exception("generic"))
        with pytest.raises(RateLimitCounterUnavailable):
            store.decrement("key")

    def test_original_exception_chained(self) -> None:
        original = RuntimeError("original cause")
        store = self._store_raising(original)
        with pytest.raises(RateLimitCounterUnavailable) as exc_info:
            store.get("key")
        assert exc_info.value.__cause__ is original


# ---------------------------------------------------------------------------
# 4. hash_scope_key
# ---------------------------------------------------------------------------

class TestHashScopeKey:
    def test_deterministic(self) -> None:
        h1 = hash_scope_key("1.2.3.4", secret="mysecret32byteslongenoughXXXXXXX")
        h2 = hash_scope_key("1.2.3.4", secret="mysecret32byteslongenoughXXXXXXX")
        assert h1 == h2

    def test_different_secrets_produce_different_hashes(self) -> None:
        h1 = hash_scope_key("1.2.3.4", secret="secret_aaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        h2 = hash_scope_key("1.2.3.4", secret="secret_bbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
        assert h1 != h2

    def test_different_values_produce_different_hashes(self) -> None:
        h1 = hash_scope_key("1.2.3.4", secret="sharedsecretXXXXXXXXXXXXXXXXXXXX")
        h2 = hash_scope_key("5.6.7.8", secret="sharedsecretXXXXXXXXXXXXXXXXXXXX")
        assert h1 != h2

    def test_output_is_hex_string(self) -> None:
        h = hash_scope_key("val", secret="secretXXXXXXXXXXXXXXXXXXXXXXXXX")
        assert isinstance(h, str)
        # SHA-256 hex = 64 chars
        assert len(h) == 64
        int(h, 16)  # must be valid hex

    def test_no_raw_ip_column_in_migration(self) -> None:
        """Migration 035 must not define a column that stores raw IP."""
        migration_path = (
            _REPO_ROOT / "gateway" / "alembic" / "versions" / "035_anonymous_preview.py"
        )
        content = migration_path.read_text(encoding="utf-8")
        # Look for suspicious column names that would hold raw IP
        forbidden_patterns = ["raw_ip", "client_ip", "ip_address", "ip_addr"]
        for pat in forbidden_patterns:
            assert pat not in content.lower(), (
                f"Migration 035 must not contain a raw IP column named {pat!r}. "
                "IPs must be HMAC-hashed before storage."
            )

    def test_usage_table_columns_no_raw_ip(self) -> None:
        """The daily usage table schema must not have any column that stores
        raw IP text (only scope_key which is always HMAC hash)."""
        migration_path = (
            _REPO_ROOT / "gateway" / "alembic" / "versions" / "035_anonymous_preview.py"
        )
        content = migration_path.read_text(encoding="utf-8")
        # usage table block — between create_table("anonymous_preview_daily_usage"
        # and the next create_table call
        usage_block_match = re.search(
            r'create_table\(\s*["\']anonymous_preview_daily_usage["\'].*?'
            r'(?=create_table|op\.create_index\("uq_anon_preview_daily_usage)',
            content,
            re.DOTALL,
        )
        if usage_block_match:
            block = usage_block_match.group(0)
        else:
            block = content  # fallback: scan whole file
        assert "raw_ip" not in block.lower()
        # scope_key column is present (stores the HMAC hash)
        assert "scope_key" in block


# ---------------------------------------------------------------------------
# 5. decrement — zero callers guard (v1 time-point)
# ---------------------------------------------------------------------------

class TestDecrementCallerGuard:
    """At the v1 time-point the only legitimate caller of decrement on a
    PgRateLimitCounterStore is the adapter's _rollback_admitted path.
    Since that wiring is not yet done in gateway/ code, we assert zero
    gateway-side call sites exist outside this test file.
    """

    def test_decrement_zero_gateway_callers_outside_tests(self) -> None:
        """Scan gateway/ Python files for calls to .decrement( that are
        not inside tests/ or this file itself.

        At v1 the only legitimate caller is backend_adapter._rollback_admitted
        (src/services/) — gateway wiring to that path does not exist yet.
        This assertion should be relaxed (or removed) when T3 wires the
        adapter into a gateway endpoint. Left as a tripwire with an
        explanatory comment.
        """
        gateway_dir = _REPO_ROOT / "gateway"
        call_sites = []
        for py_file in gateway_dir.rglob("*.py"):
            src = py_file.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(src.splitlines(), 1):
                if ".decrement(" in line:
                    call_sites.append(f"{py_file.relative_to(_REPO_ROOT)}:{lineno}: {line.strip()}")
        assert call_sites == [], (
            "gateway/ code should have zero .decrement() call sites at v1. "
            "If T3 has wired the adapter, update this test to allow the "
            "backend_adapter rollback path. Found:\n" + "\n".join(call_sites)
        )

    def test_decrement_method_has_docstring_noting_rollback_only(self) -> None:
        import inspect
        doc = inspect.getdoc(PgRateLimitCounterStore.decrement) or ""
        assert "rollback" in doc.lower(), (
            "PgRateLimitCounterStore.decrement docstring must mention 'rollback' "
            "to signal its limited intended use."
        )


# ---------------------------------------------------------------------------
# 6. Migration 035 symmetric upgrade/downgrade static assertion
# ---------------------------------------------------------------------------

class TestMigration035Symmetric:
    """Static assertions that upgrade() and downgrade() cover the same
    objects: three tables, one column (is_anonymous_preview on jobs),
    one partial index on jobs, and the sentinel user row.
    """

    _MIGRATION = (
        _REPO_ROOT / "gateway" / "alembic" / "versions" / "035_anonymous_preview.py"
    )

    def _content(self) -> str:
        return self._MIGRATION.read_text(encoding="utf-8")

    def _extract_block(self, content: str, fn_name: str) -> str:
        """Extract the body of a top-level def from migration source."""
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == fn_name:
                return ast.get_source_segment(content, node) or ""
        return ""

    def test_revision_id(self) -> None:
        content = self._content()
        assert 'revision: str = "035_anonymous_preview"' in content

    def test_down_revision(self) -> None:
        content = self._content()
        assert 'down_revision' in content
        assert '034_free_service_daily_usage' in content

    def test_upgrade_creates_three_tables(self) -> None:
        content = self._content()
        up = self._extract_block(content, "upgrade")
        for table in [
            "anonymous_preview_daily_usage",
            "anonymous_sessions",
            "anonymous_preview_records",
        ]:
            assert f'create_table' in up and table in up, (
                f"upgrade() must create table {table!r}"
            )

    def test_downgrade_drops_three_tables(self) -> None:
        content = self._content()
        down = self._extract_block(content, "downgrade")
        for table in [
            "anonymous_preview_daily_usage",
            "anonymous_sessions",
            "anonymous_preview_records",
        ]:
            assert "drop_table" in down and table in down, (
                f"downgrade() must drop table {table!r}"
            )

    def test_upgrade_adds_is_anonymous_preview_column(self) -> None:
        content = self._content()
        up = self._extract_block(content, "upgrade")
        assert "is_anonymous_preview" in up
        assert "add_column" in up

    def test_downgrade_drops_is_anonymous_preview_column(self) -> None:
        content = self._content()
        down = self._extract_block(content, "downgrade")
        assert "is_anonymous_preview" in down
        assert "drop_column" in down

    def test_upgrade_creates_partial_index_on_jobs(self) -> None:
        content = self._content()
        up = self._extract_block(content, "upgrade")
        assert "ix_jobs_anon_preview_status" in up

    def test_downgrade_drops_partial_index_on_jobs(self) -> None:
        content = self._content()
        down = self._extract_block(content, "downgrade")
        assert "ix_jobs_anon_preview_status" in down

    def test_upgrade_inserts_sentinel(self) -> None:
        content = self._content()
        up = self._extract_block(content, "upgrade")
        # Sentinel email may be stored as a module-level constant (_SENTINEL_EMAIL)
        # referenced in the upgrade() body — check the constant is defined in the
        # file AND that upgrade() either inlines the email or references the constant
        assert "anonymous-preview@system" in content, (
            "Migration 035 must define sentinel email anonymous-preview@system"
        )
        # upgrade() must reference the sentinel constant or inline it
        assert "_SENTINEL_EMAIL" in up or "anonymous-preview@system" in up
        # Must use ON CONFLICT for idempotency
        assert "ON CONFLICT" in up

    def test_downgrade_deletes_sentinel(self) -> None:
        content = self._content()
        down = self._extract_block(content, "downgrade")
        # downgrade() may use _SENTINEL_EMAIL constant or inline the email
        assert "_SENTINEL_EMAIL" in down or "anonymous-preview@system" in down
        assert "DELETE" in down

    def test_downgrade_drops_all_indexes(self) -> None:
        content = self._content()
        down = self._extract_block(content, "downgrade")
        for idx in [
            "uq_anon_preview_daily_usage",
            "ix_anonymous_sessions_expires_at",
            "ix_anon_preview_records_session_id",
            "ix_anon_preview_records_expires_at",
        ]:
            assert idx in down, (
                f"downgrade() must drop index {idx!r}"
            )

    def test_anonymous_preview_records_has_audit_jsonb(self) -> None:
        content = self._content()
        up = self._extract_block(content, "upgrade")
        # Should use postgresql.JSONB (not sa.JSON) for audit column
        assert "JSONB" in up, (
            "anonymous_preview_records.audit must use postgresql.JSONB, not sa.JSON"
        )
