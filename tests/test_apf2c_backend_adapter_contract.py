"""APF2c backend adapter contract — fake fail-closed scaffold.

This file is the **contract test scaffold** for the future APF2c backend
adapter. It does NOT exercise any real backend, gateway, frontend,
upload, probe, compliance, preview media, clone provider, pricing,
payment, migration or deployment code.

Design source of truth:
``docs/plans/2026-06-02-apf2c-backend-adapter-boundary.md``.
Pure contract module under test:
``src/services/anonymous_preview_intake.py``.

The fake adapter in this file embodies the boundary expectations:

* it consumes the pure helpers in ``src.services.anonymous_preview_intake``;
* it never calls ASR / LLM / TTS / clone provider / preview media /
  pricing / payment / Gateway / Job API / production counter store;
* it converts ``IntakeRejected`` into a status-only ``PreviewRecord``
  rather than leaking exceptions to its caller;
* it fails closed on missing config, unhealthy temp storage and
  unavailable counter store.

The tests run only against the pure contract module plus the in-file
fakes, with file I/O restricted to ``tmp_path``. No ``skip`` / ``xfail``
markers are used.
"""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Mapping, MutableMapping, Optional

import pytest

from src.services.anonymous_preview_intake import (
    DEFAULT_PREVIEW_RECORD_TTL_SECONDS,
    DEFAULT_SESSION_TTL_SECONDS,
    FORBIDDEN_PREVIEW_RECORD_FIELDS,
    SHANGHAI,
    AnonymousSession,
    ComplianceResult,
    ComplianceStatus,
    IntakeConfig,
    IntakeRejected,
    PreviewRecord,
    PreviewStatus,
    ProbeResult,
    SourceType,
    UploadIntake,
    admit_source,
    admit_upload,
    build_anonymous_session,
    build_preview_record,
    evaluate_compliance_result,
    evaluate_probe_result,
    fail_closed_from_exception,
    require_config,
)


_FROZEN_NOW = datetime(2026, 6, 2, 20, 0, 0, tzinfo=SHANGHAI)


# ---------------------------------------------------------------------------
# Fake supporting infrastructure — counter store, storage health probe.
# All fakes live in this file. None of them are real backends.
# ---------------------------------------------------------------------------


class FakeRateLimitUnavailable(Exception):
    """Raised by the fake counter store when its backing file is unusable."""


class FakeCounterStore:
    """Local JSON counter store under ``tmp_path``.

    Fail-closed when the path is None, the parent directory is missing,
    or the file is corrupt. The real APF2c adapter must surface the same
    fail-closed behavior, regardless of whether the production counter
    store is fake JSON / Redis-like / DB.
    """

    def __init__(self, path: Optional[Path]):
        self._path = path

    def _load(self) -> MutableMapping[str, int]:
        if self._path is None:
            raise FakeRateLimitUnavailable("counter store path is not configured")
        if not self._path.parent.exists():
            raise FakeRateLimitUnavailable(
                f"counter store parent directory missing: {self._path.parent}"
            )
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise FakeRateLimitUnavailable(
                f"counter store unreadable: {exc}"
            ) from exc

    def _save(self, data: Mapping[str, int]) -> None:
        if self._path is None:
            raise FakeRateLimitUnavailable("counter store path is not configured")
        try:
            self._path.write_text(json.dumps(dict(data)), encoding="utf-8")
        except OSError as exc:
            raise FakeRateLimitUnavailable(
                f"counter store unwritable: {exc}"
            ) from exc

    def get(self, key: str) -> int:
        return int(self._load().get(key, 0))

    def increment(self, key: str) -> int:
        data = self._load()
        data[key] = int(data.get(key, 0)) + 1
        self._save(data)
        return data[key]


# ---------------------------------------------------------------------------
# Fake request / upload / probe / compliance facts and the fake adapter.
# ---------------------------------------------------------------------------


@dataclass
class FakeRequestFacts:
    """Facts a future backend / upload handler would already hold by the
    time it called the adapter. The adapter consumes them and does the
    minimal hashing / packaging needed for the pure intake helpers.
    """

    raw_session_id: str
    raw_ip: str
    raw_device_cookie: str
    source_type: SourceType
    is_free_user: bool
    youtube_url: Optional[str] = None
    day_key: str = "2026-06-02"


@dataclass
class FakeUploadFacts:
    """Facts a future single-request upload handler would have after the
    upload completed (file name, byte length, ffprobe duration, streaming
    source hash, stored path on the temp upload disk).
    """

    file_name: str
    byte_length: int
    duration_seconds: float
    source_hash: str
    stored_path: Path
    is_chunked: bool = False


def _hash_token(prefix: str, value: str) -> str:
    """Adapter-side hash helper. Production adapter would use a keyed hash
    with a server-side secret; the scaffold uses sha256 for determinism.
    """

    return f"{prefix}_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"


@dataclass
class FakeBackendAdapter:
    """The future APF2c backend adapter — fake, in-file, no I/O against
    real backends. Embodies the design boundary in
    ``docs/plans/2026-06-02-apf2c-backend-adapter-boundary.md``.
    """

    config: Optional[IntakeConfig]
    counter_store: Optional[FakeCounterStore]
    probe_fn: Callable[[FakeUploadFacts], ProbeResult]
    compliance_fn: Callable[[ProbeResult], ComplianceResult]
    now_fn: Callable[[], datetime] = field(default=lambda: _FROZEN_NOW)
    # Ledger of *forbidden* call sites — APF2 must never append to this.
    # Tests assert the ledger remains empty.
    forbidden_calls: List[str] = field(default_factory=list)

    # -- High-level entry point ---------------------------------------------

    def handle_intake(
        self,
        request: FakeRequestFacts,
        upload: Optional[FakeUploadFacts],
    ) -> PreviewRecord:
        """Translate request + upload facts into a status-only
        ``PreviewRecord``. ``IntakeRejected`` is *always* caught and
        rendered as a status-only failed/rejected/soft_rejected/
        rate_limited record — never re-raised to the caller.
        """

        try:
            config = require_config(self.config)
            admit_source(
                config,
                source_type=request.source_type,
                is_free_user=request.is_free_user,
            )
            if upload is None:
                raise IntakeRejected(
                    PreviewStatus.FAILED,
                    "upload facts missing for local_upload (fail closed)",
                )
            session = self._build_session(config, request)
            intake = self._build_upload_intake(upload)
            admit_upload(config, intake)
            self._enforce_rate_limits(session, intake, day_key=request.day_key)
            probe_result = self._safe_probe(upload)
            evaluate_probe_result(probe_result)
            compliance_result = self._safe_compliance(probe_result)
            evaluate_compliance_result(compliance_result)
            return build_preview_record(
                config,
                session=session,
                upload=intake,
                probe_result=probe_result,
                compliance_result=compliance_result,
                source_type=request.source_type,
                now=self.now_fn(),
            )
        except IntakeRejected as exc:
            return self._status_only_failure(request, upload, exc)

    # -- Adapter-internal helpers -------------------------------------------

    def _build_session(
        self, config: IntakeConfig, request: FakeRequestFacts
    ) -> AnonymousSession:
        return build_anonymous_session(
            config,
            session_id_hash=_hash_token("sess", request.raw_session_id),
            ip_hash=_hash_token("ip", request.raw_ip),
            device_cookie_hash=_hash_token("dev", request.raw_device_cookie),
            now=self.now_fn(),
        )

    def _build_upload_intake(self, upload: FakeUploadFacts) -> UploadIntake:
        return UploadIntake(
            file_name=upload.file_name,
            byte_length=upload.byte_length,
            duration_seconds=upload.duration_seconds,
            source_hash=upload.source_hash,
            stored_path=upload.stored_path,
            is_chunked=upload.is_chunked,
        )

    def _enforce_rate_limits(
        self,
        session: AnonymousSession,
        intake: UploadIntake,
        *,
        day_key: str,
    ) -> None:
        if self.counter_store is None:
            raise IntakeRejected(
                PreviewStatus.FAILED,
                "rate-limit counter store unavailable (fail closed)",
            )
        config = self.config
        assert config is not None  # require_config already ran
        keys = [
            (f"global:{day_key}", config.rate_limit_global_per_day),
            (f"ip:{session.ip_hash}:{day_key}", config.rate_limit_per_ip_per_day),
            (
                f"device:{session.device_cookie_hash}:{day_key}",
                config.rate_limit_per_device_per_day,
            ),
            (
                f"source:{intake.source_hash}:{day_key}",
                config.rate_limit_per_source_hash_per_day,
            ),
        ]
        try:
            for key, cap in keys:
                if self.counter_store.get(key) >= cap:
                    session.escalated_to_login = (
                        config.escalate_to_login_after_rate_limit
                    )
                    raise IntakeRejected(
                        PreviewStatus.RATE_LIMITED,
                        f"rate limit exceeded for {key}",
                    )
            for key, _cap in keys:
                self.counter_store.increment(key)
        except FakeRateLimitUnavailable as exc:
            raise fail_closed_from_exception("rate-limit", exc) from exc

    def _safe_probe(self, upload: FakeUploadFacts) -> ProbeResult:
        try:
            return self.probe_fn(upload)
        except Exception as exc:  # noqa: BLE001 — fail closed on any error
            raise fail_closed_from_exception("probe", exc) from exc

    def _safe_compliance(self, probe_result: ProbeResult) -> ComplianceResult:
        try:
            return self.compliance_fn(probe_result)
        except Exception as exc:  # noqa: BLE001 — fail closed on any error
            raise fail_closed_from_exception("compliance", exc) from exc

    def _status_only_failure(
        self,
        request: FakeRequestFacts,
        upload: Optional[FakeUploadFacts],
        exc: IntakeRejected,
    ) -> PreviewRecord:
        """Render an ``IntakeRejected`` as a status-only ``PreviewRecord``.

        The record carries the rejection ``PreviewStatus`` and reason, but
        none of the preview / clone / pricing / payment surface — the pure
        ``PreviewRecord`` dataclass itself prevents that.
        """

        now = self.now_fn()
        source_hash = upload.source_hash if upload is not None else ""
        # ``record_id`` follows the pure module's convention but adapts to
        # a stub when no upload exists (e.g. YouTube reject before any
        # upload happened).
        record_id = (
            f"prv_{source_hash[:12]}" if source_hash else "prv_rejected_no_upload"
        )
        # Pull a session_id_hash from the request even on failure so the
        # frontend can correlate retries to the same anonymous session.
        session_id_hash = _hash_token("sess", request.raw_session_id)
        return PreviewRecord(
            record_id=record_id,
            session_id_hash=session_id_hash,
            source_hash=source_hash,
            upload_hash=source_hash,
            source_type=request.source_type,
            status=exc.status,
            status_reason=exc.reason,
            duration_seconds=0.0,
            audio_present=False,
            compliance_status=None,
            compliance_audit_metadata={},
            created_at=now,
            expires_at=now
            + timedelta(seconds=DEFAULT_PREVIEW_RECORD_TTL_SECONDS),
            selected_mode_placeholder=None,
            recommended_mode_placeholder=None,
            claim_token_placeholder=None,
        )


# ---------------------------------------------------------------------------
# Default fake probe / compliance fixtures.
# ---------------------------------------------------------------------------


def _passing_probe(upload: FakeUploadFacts) -> ProbeResult:
    return ProbeResult(
        duration_seconds=upload.duration_seconds,
        source_hash=upload.source_hash,
        media_type=f"video/{Path(upload.file_name).suffix.lstrip('.').lower()}",
        audio_present=True,
        audio_quality_score=0.8,
        teaser_candidate_range=(0.0, min(upload.duration_seconds, 180.0)),
        failure_reason=None,
    )


def _passing_compliance(_probe: ProbeResult) -> ComplianceResult:
    return ComplianceResult(
        status=ComplianceStatus.PASS,
        reason="prefilter ok; teaser ASR ok; LLM compliance ok",
        audit_metadata={"layers": ("local_prefilter", "asr_teaser", "llm")},
        blocked_media_retained=False,
    )


def _frozen_now() -> datetime:
    return _FROZEN_NOW


# ---------------------------------------------------------------------------
# Pytest fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_upload_dir(tmp_path: Path) -> Path:
    target = tmp_path / "apf2c_upload"
    target.mkdir(parents=True, exist_ok=True)
    return target


@pytest.fixture
def counter_path(tmp_path: Path) -> Path:
    return tmp_path / "apf2c_counters.json"


@pytest.fixture
def counter_store(counter_path: Path) -> FakeCounterStore:
    return FakeCounterStore(counter_path)


@pytest.fixture
def config(temp_upload_dir: Path) -> IntakeConfig:
    # Adapter receives ``temp_storage_available=True`` from an external
    # storage health probe. The pure module never touches the filesystem.
    return IntakeConfig(
        temp_upload_dir=temp_upload_dir,
        temp_storage_available=True,
    )


@pytest.fixture
def adapter(
    config: IntakeConfig, counter_store: FakeCounterStore
) -> FakeBackendAdapter:
    return FakeBackendAdapter(
        config=config,
        counter_store=counter_store,
        probe_fn=_passing_probe,
        compliance_fn=_passing_compliance,
        now_fn=_frozen_now,
    )


def _make_request(
    *,
    source_type: SourceType = SourceType.LOCAL_UPLOAD,
    is_free_user: bool = False,
    youtube_url: Optional[str] = None,
    raw_session_id: str = "anon-session-001",
    raw_ip: str = "203.0.113.7",
    raw_device_cookie: str = "device-cookie-007",
    day_key: str = "2026-06-02",
) -> FakeRequestFacts:
    return FakeRequestFacts(
        raw_session_id=raw_session_id,
        raw_ip=raw_ip,
        raw_device_cookie=raw_device_cookie,
        source_type=source_type,
        is_free_user=is_free_user,
        youtube_url=youtube_url,
        day_key=day_key,
    )


def _make_upload(
    temp_upload_dir: Path,
    *,
    file_name: str = "clip.mp4",
    byte_length: int = 4 * 1024 * 1024,
    duration_seconds: float = 120.0,
    source_hash: str = "src_hash_apf2c_test",
    is_chunked: bool = False,
) -> FakeUploadFacts:
    # The adapter and the pure module never read this file; we still
    # write a tiny byte payload to ``tmp_path`` so that any stray attempt
    # to open it would be visible during local debugging.
    stored = temp_upload_dir / file_name
    stored.write_bytes(b"apf2c-fake-bytes")
    return FakeUploadFacts(
        file_name=file_name,
        byte_length=byte_length,
        duration_seconds=duration_seconds,
        source_hash=source_hash,
        stored_path=stored,
        is_chunked=is_chunked,
    )


# ---------------------------------------------------------------------------
# Contract tests — adapter boundary behavior.
# ---------------------------------------------------------------------------


# (1) Adapter happy path turns legal local upload facts into a status-only
# PreviewRecord.
def test_adapter_local_upload_returns_status_only_preview_record(
    adapter, temp_upload_dir
):
    request = _make_request()
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_happy_path")

    record = adapter.handle_intake(request, upload)

    assert isinstance(record, PreviewRecord)
    assert record.status is PreviewStatus.READY_FOR_MODE
    assert record.source_type is SourceType.LOCAL_UPLOAD
    assert record.source_hash == "src_hash_happy_path"
    assert record.upload_hash == "src_hash_happy_path"
    assert record.compliance_status is ComplianceStatus.PASS
    # session_id_hash must be a hash, not the raw session id.
    assert "anon-session-001" not in record.session_id_hash
    assert record.session_id_hash.startswith("sess_")
    # TTL must be 24h, matching the pinned default.
    assert record.expires_at - record.created_at == timedelta(
        seconds=DEFAULT_PREVIEW_RECORD_TTL_SECONDS
    )
    # Forbidden call ledger must remain empty.
    assert adapter.forbidden_calls == []


# (2) IntakeRejected is translated into a status-only PreviewRecord rather
# than leaking out as an exception.
def test_adapter_compliance_block_translated_to_status_only_record(
    adapter, temp_upload_dir
):
    def blocking_compliance(_probe: ProbeResult) -> ComplianceResult:
        return ComplianceResult(
            status=ComplianceStatus.BLOCK,
            reason="prohibited content",
            audit_metadata={"matched_rule": "demo_rule"},
            blocked_media_retained=False,
        )

    blocked_adapter = replace(adapter, compliance_fn=blocking_compliance)
    request = _make_request()
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_blocked")

    record = blocked_adapter.handle_intake(request, upload)

    assert isinstance(record, PreviewRecord)
    assert record.status is PreviewStatus.REJECTED
    assert "compliance block" in record.status_reason
    assert blocked_adapter.forbidden_calls == []


def test_adapter_soft_reject_translated_to_status_only_record(
    adapter, temp_upload_dir
):
    def manual_review_compliance(_probe: ProbeResult) -> ComplianceResult:
        return ComplianceResult(
            status=ComplianceStatus.NEEDS_MANUAL_REVIEW,
            reason="LLM unsure",
            audit_metadata={"layers": ("local_prefilter", "asr_teaser", "llm")},
            blocked_media_retained=False,
        )

    soft_adapter = replace(adapter, compliance_fn=manual_review_compliance)
    request = _make_request()
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_soft")

    record = soft_adapter.handle_intake(request, upload)

    assert record.status is PreviewStatus.SOFT_REJECTED
    assert soft_adapter.forbidden_calls == []


def test_adapter_probe_failure_translated_to_status_only_record(
    adapter, temp_upload_dir
):
    def failing_probe(upload: FakeUploadFacts) -> ProbeResult:
        return ProbeResult(
            duration_seconds=0,
            source_hash=upload.source_hash,
            media_type="",
            audio_present=False,
            audio_quality_score=0,
            teaser_candidate_range=(0, 0),
            failure_reason="ffprobe returned no streams",
        )

    failing_adapter = replace(adapter, probe_fn=failing_probe)
    request = _make_request()
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_probe_fail")

    record = failing_adapter.handle_intake(request, upload)

    assert record.status is PreviewStatus.FAILED
    assert "probe failure" in record.status_reason
    assert failing_adapter.forbidden_calls == []


def test_adapter_probe_exception_translated_to_status_only_record(
    adapter, temp_upload_dir
):
    def crashing_probe(_upload: FakeUploadFacts) -> ProbeResult:
        raise RuntimeError("ffmpeg segfaulted")

    crash_adapter = replace(adapter, probe_fn=crashing_probe)
    request = _make_request()
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_probe_crash")

    record = crash_adapter.handle_intake(request, upload)

    assert record.status is PreviewStatus.FAILED
    # P1: ``fail_closed_from_exception`` exposes only stage label +
    # exception type name; the raw provider message is scrubbed.
    assert "probe" in record.status_reason
    assert "RuntimeError" in record.status_reason
    assert "fail closed" in record.status_reason.lower()
    assert "ffmpeg segfaulted" not in record.status_reason


def test_adapter_compliance_exception_translated_to_status_only_record(
    adapter, temp_upload_dir
):
    def crashing_compliance(_probe: ProbeResult) -> ComplianceResult:
        raise TimeoutError("LLM compliance timed out")

    crash_adapter = replace(adapter, compliance_fn=crashing_compliance)
    request = _make_request()
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_comp_crash")

    record = crash_adapter.handle_intake(request, upload)

    assert record.status is PreviewStatus.FAILED
    # P1: same scrub guarantee on the compliance branch.
    assert "compliance" in record.status_reason
    assert "TimeoutError" in record.status_reason
    assert "fail closed" in record.status_reason.lower()
    assert "LLM compliance timed out" not in record.status_reason


# (3) Missing config / temp storage unavailable / counter unavailable fail
# closed via the fake adapter.
def test_adapter_missing_config_fails_closed(counter_store, temp_upload_dir):
    bad_adapter = FakeBackendAdapter(
        config=None,
        counter_store=counter_store,
        probe_fn=_passing_probe,
        compliance_fn=_passing_compliance,
        now_fn=_frozen_now,
    )
    request = _make_request()
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_no_config")

    record = bad_adapter.handle_intake(request, upload)

    assert record.status is PreviewStatus.FAILED
    assert "IntakeConfig is missing" in record.status_reason
    assert bad_adapter.forbidden_calls == []


def test_adapter_temp_upload_dir_unconfigured_fails_closed(
    counter_store, temp_upload_dir
):
    bad_config = IntakeConfig(
        temp_upload_dir=None,
        temp_storage_available=True,
    )
    bad_adapter = FakeBackendAdapter(
        config=bad_config,
        counter_store=counter_store,
        probe_fn=_passing_probe,
        compliance_fn=_passing_compliance,
        now_fn=_frozen_now,
    )
    request = _make_request()
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_no_temp_dir")

    record = bad_adapter.handle_intake(request, upload)

    assert record.status is PreviewStatus.FAILED
    assert "temp_upload_dir is not configured" in record.status_reason


def test_adapter_temp_storage_unavailable_fails_closed(
    counter_store, temp_upload_dir
):
    bad_config = IntakeConfig(
        temp_upload_dir=temp_upload_dir,
        temp_storage_available=False,
    )
    bad_adapter = FakeBackendAdapter(
        config=bad_config,
        counter_store=counter_store,
        probe_fn=_passing_probe,
        compliance_fn=_passing_compliance,
        now_fn=_frozen_now,
    )
    request = _make_request()
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_storage_down")

    record = bad_adapter.handle_intake(request, upload)

    assert record.status is PreviewStatus.FAILED
    assert "temp_storage_available is False" in record.status_reason


def test_adapter_counter_store_missing_fails_closed(config, temp_upload_dir):
    bad_adapter = FakeBackendAdapter(
        config=config,
        counter_store=None,
        probe_fn=_passing_probe,
        compliance_fn=_passing_compliance,
        now_fn=_frozen_now,
    )
    request = _make_request()
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_no_counter")

    record = bad_adapter.handle_intake(request, upload)

    assert record.status is PreviewStatus.FAILED
    assert "counter store unavailable" in record.status_reason


def test_adapter_counter_store_unreadable_fails_closed(
    config, temp_upload_dir, tmp_path
):
    # Counter store path lives under a missing parent directory.
    broken_store = FakeCounterStore(tmp_path / "missing_parent" / "counters.json")
    bad_adapter = FakeBackendAdapter(
        config=config,
        counter_store=broken_store,
        probe_fn=_passing_probe,
        compliance_fn=_passing_compliance,
        now_fn=_frozen_now,
    )
    request = _make_request()
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_counter_broken")

    record = bad_adapter.handle_intake(request, upload)

    assert record.status is PreviewStatus.FAILED
    # P1: stage label + exception type name only, no raw provider text.
    assert "rate-limit" in record.status_reason
    assert "FakeRateLimitUnavailable" in record.status_reason
    assert "fail closed" in record.status_reason.lower()


def test_adapter_rate_limit_overflow_returns_rate_limited(
    adapter, temp_upload_dir
):
    request = _make_request()
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_rate_overflow")

    first = adapter.handle_intake(request, upload)
    second = adapter.handle_intake(request, upload)

    assert first.status is PreviewStatus.READY_FOR_MODE
    assert second.status is PreviewStatus.RATE_LIMITED
    assert "source:src_hash_rate_overflow" in second.status_reason


# (4) YouTube source for anonymous + free fails closed.
def test_adapter_youtube_anonymous_rejected(adapter):
    request = _make_request(
        source_type=SourceType.YOUTUBE_URL,
        is_free_user=False,
        youtube_url="https://example.invalid/anything",
    )

    record = adapter.handle_intake(request, upload=None)

    assert record.status is PreviewStatus.REJECTED
    assert "youtube_url" in record.status_reason
    assert adapter.forbidden_calls == []


def test_adapter_youtube_free_rejected(adapter):
    request = _make_request(
        source_type=SourceType.YOUTUBE_URL,
        is_free_user=True,
        youtube_url="https://example.invalid/anything",
    )

    record = adapter.handle_intake(request, upload=None)

    assert record.status is PreviewStatus.REJECTED
    assert "youtube_url" in record.status_reason
    assert adapter.forbidden_calls == []


# (5) PreviewRecord must not carry preview_url / download_url / clone /
# pricing / payment / credit fields.
def test_adapter_preview_record_omits_forbidden_fields(
    adapter, temp_upload_dir
):
    request = _make_request()
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_no_forbidden")

    record = adapter.handle_intake(request, upload)

    fields = set(record.__dict__)
    assert FORBIDDEN_PREVIEW_RECORD_FIELDS.isdisjoint(fields), (
        f"preview record leaks forbidden fields: "
        f"{fields & FORBIDDEN_PREVIEW_RECORD_FIELDS}"
    )
    # Placeholders stay None — APF2 does not fill mode / claim token.
    assert record.selected_mode_placeholder is None
    assert record.recommended_mode_placeholder is None
    assert record.claim_token_placeholder is None


def test_adapter_failure_preview_record_also_omits_forbidden_fields(
    adapter
):
    # YouTube reject bypasses the upload happy path; the failure response
    # must still respect the status-only contract.
    request = _make_request(
        source_type=SourceType.YOUTUBE_URL,
        is_free_user=False,
        youtube_url="https://example.invalid/anything",
    )

    record = adapter.handle_intake(request, upload=None)

    fields = set(record.__dict__)
    assert FORBIDDEN_PREVIEW_RECORD_FIELDS.isdisjoint(fields)


# (6) Fake adapter must not call preview media / clone / pricing / payment
# / Gateway / API / production counter store. The forbidden_calls ledger
# stays empty across all happy and failure paths.
def test_adapter_never_records_forbidden_calls(adapter, temp_upload_dir):
    happy_request = _make_request()
    happy_upload = _make_upload(temp_upload_dir, source_hash="src_hash_ledger_a")
    adapter.handle_intake(happy_request, happy_upload)

    youtube_request = _make_request(
        source_type=SourceType.YOUTUBE_URL,
        is_free_user=False,
    )
    adapter.handle_intake(youtube_request, upload=None)

    rate_request = _make_request()
    rate_upload = _make_upload(temp_upload_dir, source_hash="src_hash_ledger_a")
    adapter.handle_intake(rate_request, rate_upload)  # second hit → rate limited

    assert adapter.forbidden_calls == []


def test_adapter_source_module_has_no_preview_or_clone_callables():
    """The pure intake module deliberately exports no preview media /
    clone / pricing / payment helpers. Scan its public surface to keep
    that invariant explicit.
    """

    module_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "services"
        / "anonymous_preview_intake.py"
    )
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden_symbol_substrings = (
        "preview_url",
        "download_url",
        "preview_artifact",
        "clone_voice",
        "voice_clone",
        "pricing",
        "payment",
        "credit_reservation",
    )
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for fragment in forbidden_symbol_substrings:
                if fragment in node.name:
                    offenders.append(f"function {node.name}")
    assert offenders == [], (
        f"pure intake module exposes forbidden surface: {offenders}"
    )


# ---------------------------------------------------------------------------
# Scaffold self-guards.
# ---------------------------------------------------------------------------


def test_scaffold_does_not_import_forbidden_modules():
    """The scaffold must only import the pure intake module and standard
    library / pytest. No Gateway, frontend, real provider, network,
    process, or DB imports are allowed.
    """

    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    allowed_src_prefix = "src.services.anonymous_preview_intake"
    forbidden_top_level = {
        "gateway",
        "frontend",
        "frontend_next",
        "requests",
        "urllib",
        "urllib3",
        "socket",
        "httpx",
        "boto3",
        "aiohttp",
        "subprocess",
    }
    forbidden_src_prefixes = (
        "src.pipeline",
        "src.modules.ingestion.youtube",
        "src.services.jobs",
        "src.services.tts",
        "src.services.voice_clone",
        "src.services.tts_provider",
        "src.services.tts_service",
        "src.services.voice_registry",
        "src.services.voice",
        "src.services.voice_asset",
        "src.services.mainland_worker",
        "src.services.express",
        "src.services.gemini",
        "src.services.llm",
        "src.services.llm_registry",
        "src.services.llm_service",
        "src.services.assemblyai",
        "src.services.whisper_align",
        "src.services.content_compliance",
        "src.modules.output",
        "src.modules.draft",
    )
    offenders: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                root = name.split(".", 1)[0]
                if root in forbidden_top_level:
                    offenders.append(f"import {name}")
                elif root == "src" and not name.startswith(allowed_src_prefix):
                    offenders.append(f"import {name}")
                elif name.startswith(forbidden_src_prefixes):
                    offenders.append(f"import {name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            module = node.module or ""
            root = module.split(".", 1)[0]
            if root in forbidden_top_level:
                offenders.append(f"from {module} import ...")
            elif root == "src" and not module.startswith(allowed_src_prefix):
                offenders.append(f"from {module} import ...")
            elif module.startswith(forbidden_src_prefixes):
                offenders.append(f"from {module} import ...")

    assert offenders == [], (
        f"scaffold imports forbidden modules: {offenders}"
    )


def test_scaffold_does_not_use_skip_or_xfail():
    """No skipped / xfailed contract tests allowed in this scaffold."""

    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders: list[str] = []
    forbidden = {"skip", "skipif", "xfail"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            if isinstance(node.value, ast.Attribute) and node.value.attr == "mark":
                offenders.append(f"pytest.mark.{node.attr}")
            elif isinstance(node.value, ast.Name) and node.value.id == "pytest":
                offenders.append(f"pytest.{node.attr}")
    assert offenders == [], (
        f"scaffold uses forbidden skip/xfail markers: {offenders}"
    )


def test_scaffold_counter_store_round_trip(counter_path):
    store = FakeCounterStore(counter_path)
    assert store.get("any") == 0
    assert store.increment("any") == 1
    assert store.increment("any") == 2
    assert store.get("any") == 2
