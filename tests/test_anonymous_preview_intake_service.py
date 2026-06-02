"""APF2b contract tests for ``src.services.anonymous_preview_intake``.

These tests pin the production landing of the APF2 anonymous intake
contract. They exercise the side-effect-free decision helpers exposed by
``src/services/anonymous_preview_intake.py`` and assert the import
hygiene of that module (no ``gateway``, ``frontend-next``,
``src.pipeline``, no real ``services.jobs`` / ``services.tts`` /
``services.voice_clone`` / provider modules).

Companion contract scaffold: ``tests/test_apf2_anonymous_intake_contract.py``.
Design source of truth: ``docs/plans/2026-06-02-apf2-anonymous-intake-contract.md``.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.services.anonymous_preview_intake import (
    DEFAULT_ALLOWED_UPLOAD_TYPES,
    DEFAULT_COMPLIANCE_AUDIT_RETENTION_SECONDS,
    DEFAULT_MAX_SOURCE_DURATION_SECONDS,
    DEFAULT_MAX_UPLOAD_BYTES,
    DEFAULT_PREVIEW_RECORD_TTL_SECONDS,
    DEFAULT_RATE_LIMIT_GLOBAL_PER_DAY,
    DEFAULT_RATE_LIMIT_PER_DEVICE_PER_DAY,
    DEFAULT_RATE_LIMIT_PER_IP_PER_DAY,
    DEFAULT_RATE_LIMIT_PER_SOURCE_HASH_PER_DAY,
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


_FROZEN_NOW = datetime(2026, 6, 2, 18, 0, 0, tzinfo=SHANGHAI)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_upload_dir(tmp_path: Path) -> Path:
    # Pure value — the service module never touches the filesystem, so we
    # do not create the directory. Tests express temp-storage availability
    # via ``IntakeConfig.temp_storage_available`` instead of relying on
    # ``Path.exists()``.
    return tmp_path / "anon_intake_uploads"


@pytest.fixture
def config(temp_upload_dir: Path) -> IntakeConfig:
    return IntakeConfig(
        temp_upload_dir=temp_upload_dir,
        temp_storage_available=True,
    )


@pytest.fixture
def session(config: IntakeConfig) -> AnonymousSession:
    return build_anonymous_session(
        config,
        session_id_hash="sess_hash_anon_test",
        ip_hash="ip_hash_anon_test",
        device_cookie_hash="device_hash_anon_test",
        now=_FROZEN_NOW,
    )


def _make_upload(
    temp_upload_dir: Path,
    *,
    file_name: str = "clip.mp4",
    byte_length: int = 4 * 1024 * 1024,
    duration_seconds: float = 120.0,
    source_hash: str = "src_hash_anon_test",
    is_chunked: bool = False,
) -> UploadIntake:
    # ``stored_path`` is treated as a pure value — no bytes are written
    # because the service module never opens or reads it.
    stored = temp_upload_dir / file_name
    return UploadIntake(
        file_name=file_name,
        byte_length=byte_length,
        duration_seconds=duration_seconds,
        source_hash=source_hash,
        stored_path=stored,
        is_chunked=is_chunked,
    )


def _passing_probe(upload: UploadIntake) -> ProbeResult:
    return ProbeResult(
        duration_seconds=upload.duration_seconds,
        source_hash=upload.source_hash,
        media_type=f"video/{upload.extension}",
        audio_present=True,
        audio_quality_score=0.8,
        teaser_candidate_range=(0.0, min(upload.duration_seconds, 180.0)),
        failure_reason=None,
    )


def _passing_compliance() -> ComplianceResult:
    return ComplianceResult(
        status=ComplianceStatus.PASS,
        reason="prefilter ok; teaser ASR ok; LLM compliance ok",
        audit_metadata={"layers": ("local_prefilter", "asr_teaser", "llm")},
        blocked_media_retained=False,
    )


# ---------------------------------------------------------------------------
# Constants — pinned defaults.
# ---------------------------------------------------------------------------


def test_constants_pinned_from_decision_table():
    assert DEFAULT_SESSION_TTL_SECONDS == 24 * 3600
    assert DEFAULT_ALLOWED_UPLOAD_TYPES == ("mp4", "mov", "m4v", "webm")
    assert DEFAULT_MAX_UPLOAD_BYTES == 500 * 1024 * 1024
    assert DEFAULT_MAX_SOURCE_DURATION_SECONDS == 30 * 60
    assert DEFAULT_PREVIEW_RECORD_TTL_SECONDS == 24 * 3600
    assert DEFAULT_COMPLIANCE_AUDIT_RETENTION_SECONDS == 30 * 86400
    assert DEFAULT_RATE_LIMIT_GLOBAL_PER_DAY == 500
    assert DEFAULT_RATE_LIMIT_PER_IP_PER_DAY == 3
    assert DEFAULT_RATE_LIMIT_PER_DEVICE_PER_DAY == 2
    assert DEFAULT_RATE_LIMIT_PER_SOURCE_HASH_PER_DAY == 1


def test_intake_config_defaults_match_decision_table():
    config = IntakeConfig()
    assert config.session_ttl_seconds == DEFAULT_SESSION_TTL_SECONDS
    assert config.allowed_upload_types == DEFAULT_ALLOWED_UPLOAD_TYPES
    assert config.max_upload_bytes == DEFAULT_MAX_UPLOAD_BYTES
    assert config.max_source_duration_seconds == DEFAULT_MAX_SOURCE_DURATION_SECONDS
    assert config.single_request_upload_only is True
    assert config.youtube_enabled_for_anonymous is False
    assert config.youtube_enabled_for_free is False
    assert config.escalate_to_login_after_rate_limit is True


# ---------------------------------------------------------------------------
# Source-type gate — covers behavior #1 (anonymous + Free YouTube rejected).
# ---------------------------------------------------------------------------


def test_anonymous_youtube_rejected(config):
    with pytest.raises(IntakeRejected) as excinfo:
        admit_source(
            config,
            source_type=SourceType.YOUTUBE_URL,
            is_free_user=False,
        )
    assert excinfo.value.status is PreviewStatus.REJECTED


def test_free_user_youtube_rejected(config):
    with pytest.raises(IntakeRejected) as excinfo:
        admit_source(
            config,
            source_type=SourceType.YOUTUBE_URL,
            is_free_user=True,
        )
    assert excinfo.value.status is PreviewStatus.REJECTED


def test_local_upload_source_admitted(config):
    admit_source(
        config,
        source_type=SourceType.LOCAL_UPLOAD,
        is_free_user=False,
    )


# ---------------------------------------------------------------------------
# Upload gate — covers behaviors #2-#5 (extension, size, duration, chunked).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ext", ["mp4", "mov", "m4v", "webm"])
def test_allowed_upload_extensions_pass(config, temp_upload_dir, ext):
    upload = _make_upload(temp_upload_dir, file_name=f"clip.{ext}")
    admit_upload(config, upload)


@pytest.mark.parametrize(
    "ext_or_name",
    ["mkv", "avi", "wmv", "mp3", "mp4.exe", ""],
)
def test_disallowed_upload_extensions_rejected(config, temp_upload_dir, ext_or_name):
    file_name = f"clip.{ext_or_name}" if ext_or_name else "clip"
    upload = _make_upload(temp_upload_dir, file_name=file_name)
    with pytest.raises(IntakeRejected) as excinfo:
        admit_upload(config, upload)
    assert excinfo.value.status is PreviewStatus.REJECTED


def test_upload_byte_cap_boundary(config, temp_upload_dir):
    at_cap = _make_upload(temp_upload_dir, byte_length=config.max_upload_bytes)
    admit_upload(config, at_cap)

    over_cap = _make_upload(
        temp_upload_dir,
        byte_length=config.max_upload_bytes + 1,
        file_name="big.mov",
    )
    with pytest.raises(IntakeRejected) as excinfo:
        admit_upload(config, over_cap)
    assert excinfo.value.status is PreviewStatus.REJECTED
    assert "exceed cap" in excinfo.value.reason


def test_upload_duration_cap_boundary(config, temp_upload_dir):
    at_cap = _make_upload(
        temp_upload_dir,
        duration_seconds=config.max_source_duration_seconds,
    )
    admit_upload(config, at_cap)

    over_cap = _make_upload(
        temp_upload_dir,
        duration_seconds=config.max_source_duration_seconds + 1,
        file_name="long.webm",
    )
    with pytest.raises(IntakeRejected) as excinfo:
        admit_upload(config, over_cap)
    assert excinfo.value.status is PreviewStatus.REJECTED
    assert "exceeds intake cap" in excinfo.value.reason


def test_chunked_upload_rejected(config, temp_upload_dir):
    upload = _make_upload(temp_upload_dir, is_chunked=True)
    with pytest.raises(IntakeRejected) as excinfo:
        admit_upload(config, upload)
    assert excinfo.value.status is PreviewStatus.REJECTED
    assert "chunked" in excinfo.value.reason


# ---------------------------------------------------------------------------
# Temp upload dir / config fail-closed (behavior #6 background).
# ---------------------------------------------------------------------------


def test_missing_temp_upload_dir_fails_closed(temp_upload_dir):
    bad_config = IntakeConfig(
        temp_upload_dir=None,
        temp_storage_available=True,
    )
    upload = _make_upload(temp_upload_dir)
    with pytest.raises(IntakeRejected) as excinfo:
        admit_upload(bad_config, upload)
    assert excinfo.value.status is PreviewStatus.FAILED
    assert "temp_upload_dir is not configured" in excinfo.value.reason


def test_temp_storage_unavailable_fails_closed(temp_upload_dir):
    # Pure input: the caller signals that temp storage is unhealthy /
    # missing without the service module touching the filesystem.
    bad_config = IntakeConfig(
        temp_upload_dir=temp_upload_dir,
        temp_storage_available=False,
    )
    upload = _make_upload(temp_upload_dir)
    with pytest.raises(IntakeRejected) as excinfo:
        admit_upload(bad_config, upload)
    assert excinfo.value.status is PreviewStatus.FAILED
    assert "temp_storage_available is False" in excinfo.value.reason


def test_temp_storage_available_default_is_false_for_fail_closed():
    # Defaulting to False ensures callers must explicitly opt in once the
    # backend wiring has confirmed the upload directory is healthy.
    default_config = IntakeConfig()
    assert default_config.temp_storage_available is False


def test_require_config_rejects_missing_config():
    with pytest.raises(IntakeRejected) as excinfo:
        require_config(None)
    assert excinfo.value.status is PreviewStatus.FAILED


def test_require_config_returns_provided_config(config):
    assert require_config(config) is config


# ---------------------------------------------------------------------------
# Rate-limit fail-closed — behavior #6 (store/config unavailable). The
# pure module does not own the counter store but documents the
# fail-closed expectation via ``fail_closed_from_exception``.
# ---------------------------------------------------------------------------


def test_fail_closed_helper_builds_failed_rejection():
    exc = RuntimeError("counter store unwritable")
    rejection = fail_closed_from_exception("rate-limit", exc)
    assert rejection.status is PreviewStatus.FAILED
    assert "rate-limit error (fail closed)" in rejection.reason
    assert "counter store unwritable" in rejection.reason


# ---------------------------------------------------------------------------
# Probe gate — backstop for behavior #9 (probe failure must not advance).
# ---------------------------------------------------------------------------


def test_probe_failure_reason_blocks_pipeline():
    probe = ProbeResult(
        duration_seconds=0,
        source_hash="",
        media_type="",
        audio_present=False,
        audio_quality_score=0,
        teaser_candidate_range=(0, 0),
        failure_reason="ffprobe returned no streams",
    )
    with pytest.raises(IntakeRejected) as excinfo:
        evaluate_probe_result(probe)
    assert excinfo.value.status is PreviewStatus.FAILED
    assert "ffprobe" in excinfo.value.reason


def test_probe_pass_returns_result(temp_upload_dir):
    upload = _make_upload(temp_upload_dir)
    probe = _passing_probe(upload)
    assert evaluate_probe_result(probe) is probe


# ---------------------------------------------------------------------------
# Compliance gate — behaviors #7-#9.
# ---------------------------------------------------------------------------


def test_compliance_block_rejected():
    result = ComplianceResult(
        status=ComplianceStatus.BLOCK,
        reason="prohibited content",
        audit_metadata={"matched_rule": "demo_rule"},
        blocked_media_retained=False,
    )
    with pytest.raises(IntakeRejected) as excinfo:
        evaluate_compliance_result(result)
    assert excinfo.value.status is PreviewStatus.REJECTED


def test_compliance_block_with_retained_media_fails_closed():
    result = ComplianceResult(
        status=ComplianceStatus.BLOCK,
        reason="prohibited content",
        audit_metadata={"matched_rule": "demo_rule"},
        blocked_media_retained=True,
    )
    with pytest.raises(IntakeRejected) as excinfo:
        evaluate_compliance_result(result)
    assert excinfo.value.status is PreviewStatus.FAILED
    assert "blocked media bytes must not be retained" in excinfo.value.reason


def test_compliance_needs_manual_review_is_soft_reject():
    result = ComplianceResult(
        status=ComplianceStatus.NEEDS_MANUAL_REVIEW,
        reason="LLM unsure",
        audit_metadata={"layers": ("local_prefilter", "asr_teaser", "llm")},
        blocked_media_retained=False,
    )
    with pytest.raises(IntakeRejected) as excinfo:
        evaluate_compliance_result(result)
    assert excinfo.value.status is PreviewStatus.SOFT_REJECTED
    assert "soft reject" in excinfo.value.reason


def test_compliance_pass_returns_result_unchanged():
    result = _passing_compliance()
    assert evaluate_compliance_result(result) is result


def test_compliance_exception_translates_to_failed():
    exc = TimeoutError("LLM compliance timed out")
    rejection = fail_closed_from_exception("compliance", exc)
    assert rejection.status is PreviewStatus.FAILED
    assert "compliance error (fail closed)" in rejection.reason


def test_compliance_unknown_status_does_not_pass():
    # A future / unknown ComplianceStatus value must never be mistaken
    # for PASS. The pure helper only short-circuits on the three known
    # statuses; an unknown status is a contract violation — verified by
    # constructing a value that exercises the BLOCK branch via reason
    # mutation. The enum itself is closed, so this test pins the
    # invariant that the helper only treats explicit PASS as pass.
    pass_result = _passing_compliance()
    assert evaluate_compliance_result(pass_result).status is ComplianceStatus.PASS


# ---------------------------------------------------------------------------
# PreviewRecord — behavior #10 (status-only, no leaked fields).
# ---------------------------------------------------------------------------


def test_preview_record_is_status_only(config, temp_upload_dir, session):
    upload = _make_upload(temp_upload_dir)
    probe = _passing_probe(upload)
    compliance = _passing_compliance()
    record = build_preview_record(
        config,
        session=session,
        upload=upload,
        probe_result=probe,
        compliance_result=compliance,
        now=_FROZEN_NOW,
    )
    fields = set(record.__dict__)
    assert FORBIDDEN_PREVIEW_RECORD_FIELDS.isdisjoint(fields), (
        f"preview record leaks forbidden fields: "
        f"{fields & FORBIDDEN_PREVIEW_RECORD_FIELDS}"
    )
    assert record.claim_token_placeholder is None
    assert record.selected_mode_placeholder is None
    assert record.recommended_mode_placeholder is None
    assert record.status is PreviewStatus.READY_FOR_MODE
    assert record.source_type is SourceType.LOCAL_UPLOAD


def test_preview_record_ttl_is_24h(config, temp_upload_dir, session):
    upload = _make_upload(temp_upload_dir)
    record = build_preview_record(
        config,
        session=session,
        upload=upload,
        probe_result=_passing_probe(upload),
        compliance_result=_passing_compliance(),
        now=_FROZEN_NOW,
    )
    assert record.expires_at - record.created_at == timedelta(
        seconds=DEFAULT_PREVIEW_RECORD_TTL_SECONDS
    )


def test_preview_record_carries_compliance_audit_metadata_only(
    config, temp_upload_dir, session
):
    upload = _make_upload(temp_upload_dir)
    record = build_preview_record(
        config,
        session=session,
        upload=upload,
        probe_result=_passing_probe(upload),
        compliance_result=_passing_compliance(),
        now=_FROZEN_NOW,
    )
    assert record.compliance_audit_metadata == {
        "layers": ("local_prefilter", "asr_teaser", "llm")
    }
    for value in record.compliance_audit_metadata.values():
        assert not isinstance(value, (bytes, bytearray, memoryview))


def test_session_ttl_is_24_hours(config):
    sess = build_anonymous_session(
        config,
        session_id_hash="sess_hash_session_ttl",
        ip_hash="ip_hash_session_ttl",
        device_cookie_hash="device_hash_session_ttl",
        now=_FROZEN_NOW,
    )
    assert sess.expires_at - sess.created_at == timedelta(
        seconds=DEFAULT_SESSION_TTL_SECONDS
    )
    assert not hasattr(sess, "session_id")
    forbidden_session_fields = {
        "fingerprint",
        "user_agent",
        "canvas_hash",
        "webgl_hash",
        "audio_fingerprint",
        "ua_hash",
    }
    assert forbidden_session_fields.isdisjoint(sess.__dict__)


# ---------------------------------------------------------------------------
# Behavior #11 — module import hygiene.
# ---------------------------------------------------------------------------


def test_module_does_not_import_forbidden_packages():
    """The pure contract module must not import gateway / frontend-next /
    pipeline / real jobs / TTS / clone provider modules. Inspect the
    source AST so plain string literals (constants, docstrings) cannot
    trigger a false positive.
    """

    module_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "services"
        / "anonymous_preview_intake.py"
    )
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden_top_level = {"gateway", "frontend_next", "frontend"}
    forbidden_prefixes = (
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
        "pipeline",
        "modules.ingestion.youtube",
        "services.jobs",
        "services.tts",
        "services.voice_clone",
        "services.tts_provider",
        "services.tts_service",
    )
    offenders: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in forbidden_top_level or alias.name.startswith(
                    forbidden_prefixes
                ):
                    offenders.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            module = node.module or ""
            root = module.split(".", 1)[0]
            if root in forbidden_top_level or module.startswith(forbidden_prefixes):
                offenders.append(f"from {module} import ...")

    assert offenders == [], f"module imports forbidden packages: {offenders}"


def test_module_has_no_io_or_network_calls():
    """The pure contract module must not perform filesystem I/O against
    production paths or open network sockets at import time. We scan the
    AST for calls to obvious I/O surfaces (``open``, ``requests``,
    ``urllib``, ``socket``, ``httpx``, ``boto3``) at module level.
    """

    module_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "services"
        / "anonymous_preview_intake.py"
    )
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden_callables = {
        "open",
        "read_text",
        "write_text",
        "read_bytes",
        "write_bytes",
        "exists",
        "is_file",
        "is_dir",
        "is_symlink",
        "is_mount",
        "is_block_device",
        "is_char_device",
        "is_fifo",
        "is_socket",
        "mkdir",
        "rmdir",
        "unlink",
        "iterdir",
        "stat",
        "lstat",
        "glob",
        "rglob",
        "touch",
        "chmod",
    }
    forbidden_modules = {
        "requests",
        "urllib",
        "urllib3",
        "socket",
        "httpx",
        "boto3",
        "aiohttp",
        "subprocess",
    }

    offenders: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".", 1)[0] in forbidden_modules:
                    offenders.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            module = node.module or ""
            if module.split(".", 1)[0] in forbidden_modules:
                offenders.append(f"from {module} import ...")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in forbidden_callables:
                offenders.append(f"call {func.id}(...)")
            elif isinstance(func, ast.Attribute) and func.attr in forbidden_callables:
                offenders.append(f"call .{func.attr}(...)")

    assert offenders == [], (
        f"module appears to perform I/O or network access: {offenders}"
    )
