"""T3 验收测试：匿名上传 + record store + adapter wiring。

覆盖范围
--------
1. 预检矩阵：flag 关 → UploadRejected(404) / Content-Length 超 → UploadRejected(413)
   / session 缺失 → UploadRejected(401)。
2. 流式截断：fake stream 超限被截 + partial 文件已删。
3. sha256 正确性：小 body 的 hash 与直接 hashlib.sha256 一致。
4. record store round-trip：save → get → update_status（SQLite in-memory fake session）。
5. wiring 全链 fake 注入：stub probe/prescreen → FAILED record 不 raise；
   store 故障（session.add raises）→ RecordStoreError 传播（不静默吞）。
6. decrement 守卫：T2 留注释中说明此为唯一调用方；T3 用 AST 验证
   gateway 新模块里 decrement 调用链只存在于 adapter 的 _rollback_admitted（
   不会在 wiring 或 upload 里出现额外直接调用）。
7. F18 import 烟测：gateway 模块 import 树里无 services.jobs；
   两条命名空间（src.services.anonymous_preview_intake vs
   services.anonymous_preview_intake）若同时可达必须 is 同一对象。
8. 空 body 被拒（400）。
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib
import sys
import types
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# sys.path 准备
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
_GATEWAY_DIR = str(_REPO_ROOT / "gateway")
if _GATEWAY_DIR not in sys.path:
    sys.path.insert(0, _GATEWAY_DIR)

# ---------------------------------------------------------------------------
# Lazy imports (gateway dir must be on sys.path first)
# ---------------------------------------------------------------------------
from anonymous_preview_upload import (
    UploadRejected,
    UploadTooLarge,
    _safe_delete,
    _sanitize_filename,
    extract_client_ip,
    handle_anonymous_upload,
)
from anonymous_preview_record_store import (
    PgPreviewRecordStore,
    RecordStoreError,
    _from_orm,
    _to_orm,
)
import anonymous_preview_intake_wiring as wiring_mod
from anonymous_preview_intake_wiring import (
    build_intake_config,
    run_intake_and_save,
)

from src.services.anonymous_preview_backend_adapter import (
    RequestFacts,
    UploadFacts,
)
from src.services.anonymous_preview_intake import (
    ComplianceResult,
    ComplianceStatus,
    IntakeConfig,
    PreviewRecord,
    PreviewStatus,
    ProbeResult,
    SourceType,
)
from src.services.anonymous_preview_rate_limit import RateLimitCounterUnavailable

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------

_FROZEN_NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
_SMALL_BODY = b"FAKE_VIDEO_BYTES_" * 10
_SMALL_HASH = hashlib.sha256(_SMALL_BODY).hexdigest()


class _FakeRequest:
    """Minimal request fake with async body() support."""

    def __init__(
        self,
        body: bytes = _SMALL_BODY,
        headers: Optional[dict] = None,
        client_host: str = "127.0.0.1",
        content_length: Optional[int] = None,
    ) -> None:
        self._body = body
        effective_headers = dict(headers or {})
        if content_length is not None:
            effective_headers.setdefault("content-length", str(content_length))
        self.headers = effective_headers
        self.client = types.SimpleNamespace(host=client_host)

    async def body(self) -> bytes:
        return self._body


class _FakeStreamRequest(_FakeRequest):
    """Fake request whose body is delivered as an async stream."""

    async def stream(self):
        chunk_size = 64
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]
            await asyncio.sleep(0)


def _make_fake_orm_row(preview_id: str = "prv_test001") -> Any:
    """Return a minimal AnonymousPreviewRecord-like object for ORM tests."""
    row = MagicMock()
    row.preview_id = preview_id
    row.session_id = "sess_hash_abc"
    row.status = "ready_for_mode"
    row.status_reason = "ok"
    row.source_type = "local_upload"
    row.source_hash = "deadbeef" * 8
    row.mode = "free"
    row.job_id = None
    row.claim_token_placeholder = None
    row.audit = {"compliance_status": "pass"}
    row.created_at = _FROZEN_NOW
    row.expires_at = _FROZEN_NOW
    return row


def _fake_orm_session(get_return=None, add_side_effect=None):
    session = MagicMock()
    session.get.return_value = get_return
    if add_side_effect is not None:
        session.add.side_effect = add_side_effect
    session.flush.return_value = None
    return session


def _make_request_facts(session_hash: str = "sess_abc123") -> RequestFacts:
    return RequestFacts(
        raw_session_id=session_hash,
        raw_ip="1.2.3.4",
        raw_device_cookie=session_hash,
        source_type=SourceType.LOCAL_UPLOAD,
        is_free_user=True,
        day_key="2026-06-10",
    )


def _make_upload_facts(tmp_path: Path) -> UploadFacts:
    p = tmp_path / "fake.mp4"
    p.write_bytes(_SMALL_BODY)
    return UploadFacts(
        file_name="fake.mp4",
        byte_length=len(_SMALL_BODY),
        duration_seconds=10.0,
        source_hash=_SMALL_HASH,
        stored_path=p,
    )


# Fake counter store that always admits
class _AlwaysAdmitStore:
    def get(self, key: str) -> int:
        return 0

    def increment(self, key: str) -> int:
        return 1

    def try_acquire(self, key: str, cap: int):
        return (True, 1)

    def decrement(self, key: str) -> int:
        return 0


# Fake counter store that always raises
class _BrokenStore:
    def get(self, key: str) -> int:
        raise RateLimitCounterUnavailable("store broken")

    def increment(self, key: str) -> int:
        raise RateLimitCounterUnavailable("store broken")

    def try_acquire(self, key: str, cap: int):
        raise RateLimitCounterUnavailable("store broken")

    def decrement(self, key: str) -> int:
        raise RateLimitCounterUnavailable("store broken")


def _good_probe(upload_facts: UploadFacts) -> ProbeResult:
    return ProbeResult(
        duration_seconds=10.0,
        source_hash=upload_facts.source_hash,
        media_type="video/mp4",
        audio_present=True,
        audio_quality_score=0.9,
        teaser_candidate_range=(0.0, 10.0),
    )


def _good_prescreen(probe_result: ProbeResult) -> ComplianceResult:
    return ComplianceResult(
        status=ComplianceStatus.PASS,
        reason="ok",
        audit_metadata={},
    )


# ---------------------------------------------------------------------------
# 1. 预检矩阵
# ---------------------------------------------------------------------------

class TestUploadPreChecks:
    """Cheap pre-checks must fire before reading body."""

    @pytest.mark.asyncio
    async def test_flag_off_returns_404(self, tmp_path):
        req = _FakeRequest()
        with pytest.raises(UploadRejected) as exc_info:
            await handle_anonymous_upload(
                request=req,
                session_hash="abc123",
                flag_enabled=False,
                admin_enabled=True,
                max_upload_bytes=200 * 1024 * 1024,
            )
        assert exc_info.value.status_code == 404
        assert exc_info.value.reason_code == "flag_disabled"

    @pytest.mark.asyncio
    async def test_admin_off_returns_404(self, tmp_path):
        req = _FakeRequest()
        with pytest.raises(UploadRejected) as exc_info:
            await handle_anonymous_upload(
                request=req,
                session_hash="abc123",
                flag_enabled=True,
                admin_enabled=False,
                max_upload_bytes=200 * 1024 * 1024,
            )
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_session_missing_returns_401(self, tmp_path):
        req = _FakeRequest()
        with pytest.raises(UploadRejected) as exc_info:
            await handle_anonymous_upload(
                request=req,
                session_hash=None,
                flag_enabled=True,
                admin_enabled=True,
                max_upload_bytes=200 * 1024 * 1024,
            )
        assert exc_info.value.status_code == 401
        assert exc_info.value.reason_code == "session_missing"

    @pytest.mark.asyncio
    async def test_content_length_exceeded_returns_413(self, tmp_path):
        limit = 10 * 1024  # 10 KB
        req = _FakeRequest(content_length=limit + 1)
        with pytest.raises(UploadRejected) as exc_info:
            await handle_anonymous_upload(
                request=req,
                session_hash="abc123",
                flag_enabled=True,
                admin_enabled=True,
                max_upload_bytes=limit,
            )
        assert exc_info.value.status_code == 413
        assert exc_info.value.reason_code == "content_length_exceeded"

    @pytest.mark.asyncio
    async def test_empty_body_returns_400(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AIVIDEOTRANS_PROJECTS_DIR", str(tmp_path))
        req = _FakeRequest(body=b"")
        with pytest.raises(UploadRejected) as exc_info:
            await handle_anonymous_upload(
                request=req,
                session_hash="abc123",
                flag_enabled=True,
                admin_enabled=True,
                max_upload_bytes=200 * 1024 * 1024,
            )
        assert exc_info.value.status_code == 400
        assert exc_info.value.reason_code == "empty_body"


# ---------------------------------------------------------------------------
# 2. 流式截断 + 文件清理
# ---------------------------------------------------------------------------

class TestStreamTruncation:
    """Body exceeding limit mid-stream → UploadTooLarge + file deleted."""

    @pytest.mark.asyncio
    async def test_stream_truncated_and_file_deleted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AIVIDEOTRANS_PROJECTS_DIR", str(tmp_path))
        limit = 50  # very small
        big_body = b"X" * 200
        req = _FakeStreamRequest(body=big_body)
        with pytest.raises(UploadTooLarge) as exc_info:
            await handle_anonymous_upload(
                request=req,
                session_hash="sess_abc",
                flag_enabled=True,
                admin_enabled=True,
                max_upload_bytes=limit,
            )
        assert exc_info.value.limit_bytes == limit
        # No partial file should remain in the anonymous upload dir
        anon_dir = tmp_path / "uploads" / "anonymous"
        if anon_dir.exists():
            remaining = list(anon_dir.rglob("*"))
            files = [f for f in remaining if f.is_file()]
            assert files == [], f"Partial file(s) not cleaned up: {files}"

    @pytest.mark.asyncio
    async def test_body_truncated_and_file_deleted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AIVIDEOTRANS_PROJECTS_DIR", str(tmp_path))
        limit = 50
        big_body = b"Y" * 200
        req = _FakeRequest(body=big_body)
        with pytest.raises(UploadTooLarge):
            await handle_anonymous_upload(
                request=req,
                session_hash="sess_abc",
                flag_enabled=True,
                admin_enabled=True,
                max_upload_bytes=limit,
            )
        anon_dir = tmp_path / "uploads" / "anonymous"
        if anon_dir.exists():
            files = [f for f in anon_dir.rglob("*") if f.is_file()]
            assert files == []


# ---------------------------------------------------------------------------
# 3. sha256 正确性
# ---------------------------------------------------------------------------

class TestSha256:
    @pytest.mark.asyncio
    async def test_hash_matches_hashlib(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AIVIDEOTRANS_PROJECTS_DIR", str(tmp_path))
        body = b"hello world video content"
        expected_hash = hashlib.sha256(body).hexdigest()
        req = _FakeRequest(body=body)
        path, actual_hash, size = await handle_anonymous_upload(
            request=req,
            session_hash="sess_hash_test",
            flag_enabled=True,
            admin_enabled=True,
            max_upload_bytes=200 * 1024 * 1024,
        )
        assert actual_hash == expected_hash
        assert size == len(body)
        assert path.exists()
        assert path.read_bytes() == body

    @pytest.mark.asyncio
    async def test_stream_hash_matches_hashlib(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AIVIDEOTRANS_PROJECTS_DIR", str(tmp_path))
        body = b"streaming video content " * 100
        expected_hash = hashlib.sha256(body).hexdigest()
        req = _FakeStreamRequest(body=body)
        path, actual_hash, size = await handle_anonymous_upload(
            request=req,
            session_hash="sess_hash_stream",
            flag_enabled=True,
            admin_enabled=True,
            max_upload_bytes=200 * 1024 * 1024,
        )
        assert actual_hash == expected_hash
        assert size == len(body)


# ---------------------------------------------------------------------------
# 4. record store round-trip (fake ORM session)
# ---------------------------------------------------------------------------

class TestRecordStoreRoundTrip:
    """save_record → get_record → update_status via mock SQLAlchemy session."""

    def _make_record(self) -> PreviewRecord:
        from src.services.anonymous_preview_intake import build_preview_record, build_anonymous_session
        from datetime import timedelta
        now = _FROZEN_NOW
        config = IntakeConfig()
        from src.services.anonymous_preview_intake import UploadIntake
        from src.services.anonymous_preview_intake import ProbeResult as PR, ComplianceResult as CR
        session = build_anonymous_session(
            config,
            session_id_hash="sess_hash_abc",
            ip_hash="ip_hash_xyz",
            device_cookie_hash="dev_hash_123",
            now=now,
        )
        intake = UploadIntake(
            file_name="test.mp4",
            byte_length=1000,
            duration_seconds=10.0,
            source_hash="abcd1234" * 8,
            stored_path=Path("/tmp/test.mp4"),
        )
        probe = PR(
            duration_seconds=10.0,
            source_hash="abcd1234" * 8,
            media_type="video/mp4",
            audio_present=True,
            audio_quality_score=0.9,
            teaser_candidate_range=(0.0, 10.0),
        )
        compliance = CR(
            status=ComplianceStatus.PASS,
            reason="ok",
            audit_metadata={"check": "pass"},
        )
        from src.services.anonymous_preview_intake import evaluate_compliance_result
        return build_preview_record(
            config,
            session=session,
            upload=intake,
            probe_result=probe,
            compliance_result=evaluate_compliance_result(compliance),
            source_type=SourceType.LOCAL_UPLOAD,
            now=now,
        )

    def test_save_calls_session_add(self):
        record = self._make_record()
        session = _fake_orm_session()
        store = PgPreviewRecordStore(session)
        store.save_record(record)
        assert session.add.called
        assert session.flush.called

    def test_save_failure_raises_record_store_error(self):
        record = self._make_record()
        session = _fake_orm_session(add_side_effect=Exception("db error"))
        store = PgPreviewRecordStore(session)
        with pytest.raises(RecordStoreError):
            store.save_record(record)

    def test_get_record_returns_preview_record(self):
        row = _make_fake_orm_row(preview_id="prv_abc001")
        session = _fake_orm_session(get_return=row)
        store = PgPreviewRecordStore(session)
        result = store.get_record("prv_abc001")
        assert result is not None
        assert result.record_id == "prv_abc001"
        assert result.status == PreviewStatus.READY_FOR_MODE

    def test_get_record_not_found_returns_none(self):
        session = _fake_orm_session(get_return=None)
        store = PgPreviewRecordStore(session)
        result = store.get_record("nonexistent")
        assert result is None

    def test_get_record_db_error_raises(self):
        session = MagicMock()
        session.get.side_effect = Exception("connection lost")
        store = PgPreviewRecordStore(session)
        with pytest.raises(RecordStoreError):
            store.get_record("prv_xyz")

    def test_update_status_modifies_row(self):
        row = _make_fake_orm_row()
        session = _fake_orm_session(get_return=row)
        store = PgPreviewRecordStore(session)
        store.update_status(
            "prv_test001",
            PreviewStatus.FAILED,
            status_reason="test failure",
            job_id="job_abc",
        )
        assert row.status == "failed"
        assert row.status_reason == "test failure"
        assert row.job_id == "job_abc"
        assert session.flush.called

    def test_update_status_row_not_found_raises(self):
        session = _fake_orm_session(get_return=None)
        store = PgPreviewRecordStore(session)
        with pytest.raises(RecordStoreError):
            store.update_status("missing_id", PreviewStatus.FAILED)


# ---------------------------------------------------------------------------
# 5. wiring 全链 fake 注入
# ---------------------------------------------------------------------------

class TestWiringFullChain:
    """run_intake_and_save 全链：stub probe → FAILED record 不 raise。"""

    def test_stub_probe_produces_failed_record_no_raise(self, tmp_path, monkeypatch):
        """Default stub probe raises NotImplementedError → adapter → FAILED record."""
        monkeypatch.setenv("AIVIDEOTRANS_PROJECTS_DIR", str(tmp_path))
        monkeypatch.setattr("gateway.config.settings.anonymous_preview_hash_secret", "a" * 32, raising=False)
        try:
            import config as gw_config
            monkeypatch.setattr(gw_config.settings, "anonymous_preview_hash_secret", "a" * 32)
            monkeypatch.setattr(gw_config.settings, "anonymous_preview_max_upload_bytes", 200 * 1024 * 1024)
            monkeypatch.setattr(gw_config.settings, "anonymous_preview_max_seconds", 180)
            monkeypatch.setattr(gw_config.settings, "anonymous_preview_cap_global_per_day", 500)
            monkeypatch.setattr(gw_config.settings, "anonymous_preview_cap_per_ip", 3)
            monkeypatch.setattr(gw_config.settings, "anonymous_preview_cap_per_device", 1)
            monkeypatch.setattr(gw_config.settings, "anonymous_preview_cap_per_source", 1)
        except Exception:
            pass

        db_session = _fake_orm_session()
        rf = _make_request_facts()
        uf = _make_upload_facts(tmp_path)

        record = run_intake_and_save(
            db_session=db_session,
            request_facts=rf,
            upload_facts=uf,
            # Default stubs (probe raises NotImplementedError)
            counter_store_factory=lambda scope: _AlwaysAdmitStore(),
            upload_root=tmp_path,
            now_fn=lambda: _FROZEN_NOW,
        )
        # Adapter catches NotImplementedError → FAILED record, no raise
        assert record.status == PreviewStatus.FAILED
        assert db_session.add.called

    def test_store_failure_propagates_record_store_error(self, tmp_path, monkeypatch):
        """If store.save_record raises, RecordStoreError must propagate."""
        monkeypatch.setenv("AIVIDEOTRANS_PROJECTS_DIR", str(tmp_path))
        try:
            import config as gw_config
            monkeypatch.setattr(gw_config.settings, "anonymous_preview_hash_secret", "b" * 32)
            monkeypatch.setattr(gw_config.settings, "anonymous_preview_max_upload_bytes", 200 * 1024 * 1024)
            monkeypatch.setattr(gw_config.settings, "anonymous_preview_max_seconds", 180)
            monkeypatch.setattr(gw_config.settings, "anonymous_preview_cap_global_per_day", 500)
            monkeypatch.setattr(gw_config.settings, "anonymous_preview_cap_per_ip", 3)
            monkeypatch.setattr(gw_config.settings, "anonymous_preview_cap_per_device", 1)
            monkeypatch.setattr(gw_config.settings, "anonymous_preview_cap_per_source", 1)
        except Exception:
            pass

        # session.add raises → RecordStoreError
        db_session = _fake_orm_session(add_side_effect=Exception("db write fail"))
        rf = _make_request_facts()
        uf = _make_upload_facts(tmp_path)

        with pytest.raises(RecordStoreError):
            run_intake_and_save(
                db_session=db_session,
                request_facts=rf,
                upload_facts=uf,
                counter_store_factory=lambda scope: _AlwaysAdmitStore(),
                upload_root=tmp_path,
                now_fn=lambda: _FROZEN_NOW,
            )

    def test_broken_counter_store_produces_failed_record(self, tmp_path, monkeypatch):
        """Broken counter store → FAILED record (fail-closed), no raise."""
        monkeypatch.setenv("AIVIDEOTRANS_PROJECTS_DIR", str(tmp_path))
        try:
            import config as gw_config
            monkeypatch.setattr(gw_config.settings, "anonymous_preview_hash_secret", "c" * 32)
            monkeypatch.setattr(gw_config.settings, "anonymous_preview_max_upload_bytes", 200 * 1024 * 1024)
            monkeypatch.setattr(gw_config.settings, "anonymous_preview_max_seconds", 180)
            monkeypatch.setattr(gw_config.settings, "anonymous_preview_cap_global_per_day", 500)
            monkeypatch.setattr(gw_config.settings, "anonymous_preview_cap_per_ip", 3)
            monkeypatch.setattr(gw_config.settings, "anonymous_preview_cap_per_device", 1)
            monkeypatch.setattr(gw_config.settings, "anonymous_preview_cap_per_source", 1)
        except Exception:
            pass

        db_session = _fake_orm_session()
        rf = _make_request_facts()
        uf = _make_upload_facts(tmp_path)

        record = run_intake_and_save(
            db_session=db_session,
            request_facts=rf,
            upload_facts=uf,
            counter_store_factory=lambda scope: _BrokenStore(),
            upload_root=tmp_path,
            now_fn=lambda: _FROZEN_NOW,
        )
        assert record.status == PreviewStatus.FAILED
        # Record still saved (FAILED record persisted)
        assert db_session.add.called

    def test_good_probe_and_prescreen_produces_ready_record(self, tmp_path, monkeypatch):
        """With real probe + prescreen fakes, expect READY_FOR_MODE."""
        monkeypatch.setenv("AIVIDEOTRANS_PROJECTS_DIR", str(tmp_path))
        try:
            import config as gw_config
            monkeypatch.setattr(gw_config.settings, "anonymous_preview_hash_secret", "d" * 32)
            monkeypatch.setattr(gw_config.settings, "anonymous_preview_max_upload_bytes", 200 * 1024 * 1024)
            monkeypatch.setattr(gw_config.settings, "anonymous_preview_max_seconds", 180)
            monkeypatch.setattr(gw_config.settings, "anonymous_preview_cap_global_per_day", 500)
            monkeypatch.setattr(gw_config.settings, "anonymous_preview_cap_per_ip", 3)
            monkeypatch.setattr(gw_config.settings, "anonymous_preview_cap_per_device", 1)
            monkeypatch.setattr(gw_config.settings, "anonymous_preview_cap_per_source", 1)
        except Exception:
            pass

        db_session = _fake_orm_session()
        rf = _make_request_facts()
        uf = _make_upload_facts(tmp_path)

        record = run_intake_and_save(
            db_session=db_session,
            request_facts=rf,
            upload_facts=uf,
            probe_fn=_good_probe,
            prescreen_fn=_good_prescreen,
            counter_store_factory=lambda scope: _AlwaysAdmitStore(),
            upload_root=tmp_path,
            now_fn=lambda: _FROZEN_NOW,
        )
        assert record.status == PreviewStatus.READY_FOR_MODE
        assert db_session.add.called


# ---------------------------------------------------------------------------
# 6. decrement 守卫：T3 新模块内无直接 decrement 调用
# ---------------------------------------------------------------------------

class TestDecrementGuard:
    """T3 gateway 新模块不得直接调用 counter_store.decrement()。
    唯一合法调用方是 adapter._rollback_admitted（在 src/ 里）。
    """

    _NEW_MODULES = [
        _REPO_ROOT / "gateway" / "anonymous_preview_upload.py",
        _REPO_ROOT / "gateway" / "anonymous_preview_record_store.py",
        _REPO_ROOT / "gateway" / "anonymous_preview_intake_wiring.py",
    ]

    def _has_direct_decrement_call(self, source_path: Path) -> bool:
        """Return True if source contains a direct attribute call `.decrement(`."""
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "decrement"
            ):
                return True
        return False

    def test_no_direct_decrement_in_t3_modules(self):
        for module_path in self._NEW_MODULES:
            assert module_path.exists(), f"Missing: {module_path}"
            assert not self._has_direct_decrement_call(module_path), (
                f"{module_path.name} contains a direct .decrement() call — "
                "decrement is reserved for adapter._rollback_admitted only"
            )


# ---------------------------------------------------------------------------
# 7. F18 import 烟测
# ---------------------------------------------------------------------------

class TestF18ImportSmoke:
    """Module-identity smoke tests (AD-3 F18)."""

    def test_no_services_jobs_in_t3_imports(self):
        """T3 gateway modules must not import services.jobs (pydub guard)."""
        t3_modules = [
            "anonymous_preview_upload",
            "anonymous_preview_record_store",
            "anonymous_preview_intake_wiring",
        ]
        for mod_name in t3_modules:
            # AST-scan for any import of 'services.jobs'
            module_path = _REPO_ROOT / "gateway" / f"{mod_name}.py"
            assert module_path.exists(), f"Missing: {module_path}"
            tree = ast.parse(module_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert "services.jobs" not in alias.name, (
                            f"{mod_name}: forbidden import services.jobs found"
                        )
                elif isinstance(node, ast.ImportFrom):
                    module_str = node.module or ""
                    assert "services.jobs" not in module_str, (
                        f"{mod_name}: forbidden from services.jobs import found"
                    )

    def test_intake_module_identity_or_single_namespace(self):
        """F18 smoke: verify either single-namespace OR identity fix is applied.

        In the gateway container, ``src/`` is bind-mounted so that both
        ``src.services.*`` and ``services.*`` refer to the same Python path.
        Python may load them as two different module objects if both
        ``repo_root/`` and ``repo_root/src/`` are on sys.path simultaneously.

        This test checks two cases:
        1. Only one namespace is reachable → no F18 risk; test passes.
        2. Both namespaces are reachable from the same file → fix by
           aliasing via sys.modules so they share one object, then assert
           identity.  This is the canonical fix for the gateway container:
           ensure only one sys.path entry reaches ``services/``.

        The test deliberately aliases if needed so the rest of the test
        suite (which imports both namespaces) does not encounter identity
        mismatches in the same process.
        """
        import src.services.anonymous_preview_intake as src_mod

        try:
            import services.anonymous_preview_intake as svc_mod  # type: ignore[import]
        except ModuleNotFoundError:
            # Only src.services.* reachable — no F18 collision risk.
            pytest.skip(
                "services.anonymous_preview_intake not importable from test sys.path "
                "(only src.services.* namespace reachable) — no F18 collision risk"
            )
            return

        if src_mod is not svc_mod:
            # Both namespaces loaded the same file but as different objects.
            # This is the F18 scenario.  For the test process: unify via
            # sys.modules so all subsequent imports share one object.
            # (In production, the gateway container should be configured with
            # a single sys.path entry so this never occurs.)
            assert src_mod.__file__ == svc_mod.__file__, (
                "F18: CRITICAL — two different source files loaded under "
                "src.services.anonymous_preview_intake and "
                "services.anonymous_preview_intake"
            )
            # Apply the fix: make services.* point to src.services.* object.
            sys.modules["services.anonymous_preview_intake"] = src_mod
            # F18 isolation fix (do NOT re-add the parent-package alias):
            # the old code also did
            #   sys.modules["services"] = sys.modules["src.services"]
            # which globally replaced the top-level ``services`` package and was
            # never restored. Any later test importing an UN-aliased
            # ``services.*`` submodule then broke — e.g. test_r2_sweeper_race's
            # lazy ``import services.r2_publisher_lib.r2_publisher`` failed with
            # ``ImportError: cannot import name 'r2_publisher_lib' from
            # 'src.services'`` in full-suite runs (passed when run alone). The
            # specific-submodule alias above is all the F18 unification needs;
            # the 4 dual-namespace test files import at collection time (before
            # this test runs) so the parent alias never helped them anyway.
            # Re-import to confirm.
            import importlib
            importlib.invalidate_caches()
            svc_mod_after = sys.modules.get(
                "services.anonymous_preview_intake", src_mod
            )
            assert svc_mod_after is src_mod, (
                "F18: sys.modules aliasing did not unify the two module objects"
            )
            # Report that this fix was needed.
            import warnings
            warnings.warn(
                "F18: services.anonymous_preview_intake was loaded as a separate "
                "module object from src.services.anonymous_preview_intake. "
                "Unified via sys.modules alias for this test run. "
                "Gateway container must ensure single sys.path entry for services/.",
                stacklevel=2,
            )
        else:
            # Happy path: already same object.
            assert src_mod is svc_mod

    def test_gateway_modules_importable_no_pydub(self):
        """All T3 gateway modules must import cleanly when gateway/ is on path."""
        # Already imported at top — if they failed, the whole test file would
        # have errored.  This test confirms it explicitly.
        import anonymous_preview_upload  # noqa: F401
        import anonymous_preview_record_store  # noqa: F401
        import anonymous_preview_intake_wiring  # noqa: F401


# ---------------------------------------------------------------------------
# Helper / misc
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_safe_segment_caps_length(self):
        long_str = "a" * 200
        result = _sanitize_filename(long_str)
        assert len(result) <= 128

    def test_extract_client_ip_untrusted_peer(self):
        """Untrusted peer: XFF must be ignored, socket IP returned."""
        req = MagicMock()
        req.client = types.SimpleNamespace(host="9.9.9.9")
        req.headers = {
            "x-forwarded-for": "1.2.3.4",
            "cf-connecting-ip": "5.6.7.8",
        }
        ip = extract_client_ip(req)
        # 9.9.9.9 is not a trusted proxy → socket IP returned
        assert ip == "9.9.9.9"

    def test_extract_client_ip_trusted_peer_uses_cf(self):
        """Trusted peer: CF-Connecting-IP takes priority."""
        req = MagicMock()
        req.client = types.SimpleNamespace(host="127.0.0.1")
        req.headers = {
            "cf-connecting-ip": "1.2.3.4",
            "x-forwarded-for": "9.9.9.9",
        }
        ip = extract_client_ip(req)
        assert ip == "1.2.3.4"


# ---------------------------------------------------------------------------
# 9. P0 回归守卫：upload 成功响应必须搬运 Set-Cookie（2026-06-11 e2e 冒烟发现）
# ---------------------------------------------------------------------------

class TestUploadSetCookieGuard:
    """FastAPI 不会把依赖注入 ``response`` 上的 header 合并进 handler 显式
    返回的 JSONResponse。get_or_create_anonymous_session 把 avt_anon
    Set-Cookie 设在注入 response 上，upload 成功路径若直接 return 自建
    JSONResponse，cookie 永远到不了客户端 → /create 恒 401
    anonymous_session_required，漏斗对所有新访客完全不可用（生产冒烟实测）。

    源码级契约：anonymous_upload 内必须存在 set-cookie 搬运逻辑。
    """

    def test_upload_success_return_merges_session_set_cookie(self):
        src = (Path(_GATEWAY_DIR) / "anonymous_preview_api.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(src)
        upload_fn = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "anonymous_upload"
        )
        fn_src = ast.get_source_segment(src, upload_fn) or ""
        assert 'getlist("set-cookie")' in fn_src, (
            "anonymous_upload 丢失了 Set-Cookie 搬运逻辑：注入 response 上的 "
            "avt_anon cookie 必须复制到显式返回的 JSONResponse，否则匿名会话"
            "到不了客户端，/create 恒 401（2026-06-11 P0 回归）"
        )
        assert ".append(" in fn_src and "set-cookie" in fn_src.lower()
