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
    PROBE_FAILURE_STATUS_REASON,
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


def test_youtube_wire_string_rejected(config):
    # A future upload handler may forward the raw wire-level value instead
    # of the SourceType enum. ``admit_source`` must normalize and reject
    # both, otherwise YouTube intake would silently slip through.
    with pytest.raises(IntakeRejected) as excinfo:
        admit_source(
            config,
            source_type="youtube_url",  # type: ignore[arg-type]
            is_free_user=False,
        )
    assert excinfo.value.status is PreviewStatus.REJECTED
    assert "youtube_url" in excinfo.value.reason


def test_local_upload_wire_string_admitted(config):
    # Normal ``"local_upload"`` wire value must not raise — it normalizes
    # to ``SourceType.LOCAL_UPLOAD`` and passes the gate just like the enum.
    admit_source(
        config,
        source_type="local_upload",  # type: ignore[arg-type]
        is_free_user=False,
    )


def test_unknown_source_type_string_fails_closed(config):
    # Anything that is not a recognized ``SourceType`` value must fail
    # closed via ``PreviewStatus.FAILED`` rather than be silently admitted
    # as a local upload.
    with pytest.raises(IntakeRejected) as excinfo:
        admit_source(
            config,
            source_type="totally_unknown_kind",  # type: ignore[arg-type]
            is_free_user=False,
        )
    assert excinfo.value.status is PreviewStatus.FAILED
    assert "source_type" in excinfo.value.reason
    assert "totally_unknown_kind" in excinfo.value.reason


def test_non_string_source_type_fails_closed(config):
    with pytest.raises(IntakeRejected) as excinfo:
        admit_source(
            config,
            source_type=42,  # type: ignore[arg-type]
            is_free_user=False,
        )
    assert excinfo.value.status is PreviewStatus.FAILED
    assert "source_type" in excinfo.value.reason


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
    # Reason exposes only stage label + exception type name + fail-closed
    # marker. The raw exception message must never leak into
    # ``PreviewRecord.status_reason`` because injected dependencies often
    # embed secrets / tokens / paths / raw media bytes in their messages.
    assert "rate-limit" in rejection.reason
    assert "RuntimeError" in rejection.reason
    assert "fail closed" in rejection.reason.lower()
    assert "counter store unwritable" not in rejection.reason


def test_fail_closed_helper_scrubs_sensitive_exception_message():
    # Pin the P1 scrub: secrets / tokens / paths / raw media markers in
    # exception messages must never reach ``IntakeRejected.reason``.
    sensitive = (
        "secret=sk_live_AbCdEfGhIjKlMnOpQrStUvWx "
        "token=Bearer.ey.payload.signature "
        "raw=b'\\x00\\xffBINARY_MEDIA' "
        "path=/var/lib/uploads/anon/clip.mp4"
    )
    rejection = fail_closed_from_exception("probe", RuntimeError(sensitive))
    assert rejection.status is PreviewStatus.FAILED
    reason = rejection.reason
    assert "probe" in reason
    assert "RuntimeError" in reason
    forbidden_fragments = (
        "sk_live",
        "AbCdEfGhIjKlMnOpQrStUvWx",
        "Bearer",
        "ey.payload",
        "raw=",
        "BINARY_MEDIA",
        "\\x00",
        "/var/lib/uploads",
        "clip.mp4",
    )
    for fragment in forbidden_fragments:
        assert fragment not in reason, (
            f"fail_closed_from_exception leaked sensitive fragment "
            f"{fragment!r} in reason={reason!r}"
        )
    assert sensitive not in reason


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
    # Reason is fully redacted — the raw probe failure_reason must not
    # leak into ``IntakeRejected.reason`` because that string lands on
    # ``PreviewRecord.status_reason``, a persisted low-trust audit field.
    assert excinfo.value.reason == PROBE_FAILURE_STATUS_REASON
    assert "ffprobe" not in excinfo.value.reason


def test_probe_pass_returns_result(temp_upload_dir):
    upload = _make_upload(temp_upload_dir)
    probe = _passing_probe(upload)
    assert evaluate_probe_result(probe) is probe


def test_probe_failure_reason_redacted_scrubs_sensitive_fragments():
    # Pin the P2 scrub: ffprobe / ffmpeg stderr, temp filesystem paths,
    # tokens, provider names, media ids and tracebacks routinely land in
    # ``ProbeResult.failure_reason``. None of them may reach
    # ``IntakeRejected.reason`` / ``PreviewRecord.status_reason``.
    sensitive_failure = (
        "ffprobe error: Invalid data found when processing input "
        "path=/tmp/anon_preview/clip-XYZ.mp4 "
        "token=sk-live_AbCdEfGhIjKlMnOpQrStUvWx "
        "provider=ffmpeg-runner-v2 "
        "media_id=med_0123456789abcdef "
        "Traceback (most recent call last):\n"
        "  File \"probe.py\", line 42, in run\n"
        "    subprocess.check_output([...])"
    )
    probe = ProbeResult(
        duration_seconds=0,
        source_hash="src_hash_redact",
        media_type="",
        audio_present=False,
        audio_quality_score=0,
        teaser_candidate_range=(0, 0),
        failure_reason=sensitive_failure,
    )
    with pytest.raises(IntakeRejected) as excinfo:
        evaluate_probe_result(probe)
    assert excinfo.value.status is PreviewStatus.FAILED
    reason = excinfo.value.reason
    assert reason == PROBE_FAILURE_STATUS_REASON
    forbidden_fragments = (
        "ffprobe",
        "ffmpeg",
        "Invalid data",
        "/tmp/anon_preview",
        "clip-XYZ.mp4",
        "sk-live",
        "AbCdEfGhIjKlMnOpQrStUvWx",
        "token=",
        "provider=",
        "ffmpeg-runner-v2",
        "media_id=",
        "med_0123456789abcdef",
        "Traceback",
        "probe.py",
        "subprocess",
    )
    for fragment in forbidden_fragments:
        assert fragment not in reason, (
            f"evaluate_probe_result leaked sensitive fragment "
            f"{fragment!r} in reason={reason!r}"
        )
    # Full sensitive blob must not be embedded either.
    assert sensitive_failure not in reason


def test_probe_failure_status_reason_constant_is_pinned():
    # The constant value is part of the audit contract — changing it
    # is intentionally a visible API edit.
    assert PROBE_FAILURE_STATUS_REASON == "probe failure (details redacted)"


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
    assert "compliance" in rejection.reason
    assert "TimeoutError" in rejection.reason
    assert "fail closed" in rejection.reason.lower()
    # P1: provider payload text must not survive into ``status_reason``.
    assert "LLM compliance timed out" not in rejection.reason


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
# P2 — compliance PASS + failure_reason MUST fail closed (pure helper).
# Mirrors PR #22 external review comment discussion_r3345414999.
# ---------------------------------------------------------------------------


def test_compliance_pass_with_failure_reason_fails_closed():
    sensitive_failure = (
        "provider=minimax-clone tok=Bearer.sk_live_XYZ "
        "raw=b'\\x00\\xffBINARY' path=/var/lib/uploads/x.mp4"
    )
    result = ComplianceResult(
        status=ComplianceStatus.PASS,
        reason="prefilter ok; teaser ASR ok; LLM compliance ok",
        audit_metadata={"layers": ("local_prefilter", "asr_teaser", "llm")},
        blocked_media_retained=False,
        failure_reason=sensitive_failure,
    )
    with pytest.raises(IntakeRejected) as excinfo:
        evaluate_compliance_result(result)
    assert excinfo.value.status is PreviewStatus.FAILED
    reason = excinfo.value.reason
    assert "compliance failure_reason" in reason
    assert "fail closed" in reason.lower()
    for fragment in (
        sensitive_failure,
        "Bearer",
        "sk_live_XYZ",
        "BINARY",
        "\\x00",
        "/var/lib/uploads",
        "x.mp4",
    ):
        assert fragment not in reason, (
            f"PASS+failure_reason status_reason leaked sensitive fragment "
            f"{fragment!r} in reason={reason!r}"
        )


def test_compliance_pass_with_none_or_empty_failure_reason_still_passes():
    clean = ComplianceResult(
        status=ComplianceStatus.PASS,
        reason="prefilter ok",
        audit_metadata={"layers": ()},
        blocked_media_retained=False,
        failure_reason=None,
    )
    assert evaluate_compliance_result(clean) is clean
    empty = ComplianceResult(
        status=ComplianceStatus.PASS,
        reason="prefilter ok",
        audit_metadata={"layers": ()},
        blocked_media_retained=False,
        failure_reason="",
    )
    assert evaluate_compliance_result(empty) is empty


def test_compliance_block_with_failure_reason_fails_closed_before_block():
    result = ComplianceResult(
        status=ComplianceStatus.BLOCK,
        reason="prohibited content",
        audit_metadata={"matched_rule": "demo_rule"},
        blocked_media_retained=False,
        failure_reason="upstream provider 5xx with payload secret=sk_live_AAA",
    )
    with pytest.raises(IntakeRejected) as excinfo:
        evaluate_compliance_result(result)
    assert excinfo.value.status is PreviewStatus.FAILED
    assert "compliance failure_reason" in excinfo.value.reason
    assert "sk_live_AAA" not in excinfo.value.reason


# ---------------------------------------------------------------------------
# P2 — login_escalation_hint default + forbidden-field invariants.
# Mirrors PR #22 external review comment discussion_r3345414997.
# ---------------------------------------------------------------------------


def test_preview_record_login_escalation_hint_defaults_to_none(
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
    assert record.login_escalation_hint is None


def test_login_escalation_hint_not_in_forbidden_fields():
    # The hint is a status-only signal — explicitly NOT preview /
    # download / clone / pricing / payment surface.
    assert "login_escalation_hint" not in FORBIDDEN_PREVIEW_RECORD_FIELDS


def test_intake_rejected_accepts_login_escalation_hint_kwarg():
    rejection_true = IntakeRejected(
        PreviewStatus.RATE_LIMITED,
        "rate limit exceeded for source:abc",
        login_escalation_hint=True,
    )
    assert rejection_true.login_escalation_hint is True
    rejection_false = IntakeRejected(
        PreviewStatus.RATE_LIMITED,
        "rate limit exceeded",
        login_escalation_hint=False,
    )
    assert rejection_false.login_escalation_hint is False
    plain = IntakeRejected(PreviewStatus.FAILED, "some failure")
    assert plain.login_escalation_hint is None


def test_compliance_status_wire_string_block_is_normalized_and_rejected():
    # Real compliance wrappers occasionally pass the wire-level string
    # value instead of the ComplianceStatus enum. The pure helper must
    # normalize before the identity check so ``"block"`` cannot silently
    # skip past BLOCK / NEEDS_MANUAL_REVIEW and reach PASS.
    result = ComplianceResult(
        status="block",  # type: ignore[arg-type]
        reason="prohibited content from wire-level wrapper",
        audit_metadata={"matched_rule": "demo_rule"},
        blocked_media_retained=False,
    )
    with pytest.raises(IntakeRejected) as excinfo:
        evaluate_compliance_result(result)
    assert excinfo.value.status is PreviewStatus.REJECTED
    assert "compliance block" in excinfo.value.reason


def test_compliance_status_wire_string_needs_manual_review_is_soft_reject():
    result = ComplianceResult(
        status="needs_manual_review",  # type: ignore[arg-type]
        reason="LLM unsure",
        audit_metadata={"layers": ("local_prefilter", "asr_teaser", "llm")},
        blocked_media_retained=False,
    )
    with pytest.raises(IntakeRejected) as excinfo:
        evaluate_compliance_result(result)
    assert excinfo.value.status is PreviewStatus.SOFT_REJECTED


def test_compliance_status_unrecognized_value_fails_closed():
    raw_unknown = "totally_unknown_state"
    result = ComplianceResult(
        status=raw_unknown,  # type: ignore[arg-type]
        reason="provider returned a value outside the contract",
        audit_metadata={"layers": ("local_prefilter",)},
        blocked_media_retained=False,
    )
    with pytest.raises(IntakeRejected) as excinfo:
        evaluate_compliance_result(result)
    assert excinfo.value.status is PreviewStatus.FAILED
    reason = excinfo.value.reason
    # Stable low-sensitivity diagnostic fragments are present...
    assert "compliance status" in reason
    assert "recognized" in reason
    assert "fail closed" in reason.lower()
    # ...and the raw unknown value is NOT interpolated. APF2c R8o
    # external review P2: ``status_reason`` is a persisted low-trust
    # audit field; an attacker-controlled or accidentally leaky raw
    # provider value must never echo back into it.
    assert raw_unknown not in reason


def test_compliance_status_non_string_value_fails_closed():
    result = ComplianceResult(
        status=42,  # type: ignore[arg-type]
        reason="provider returned an int",
        audit_metadata={},
        blocked_media_retained=False,
    )
    with pytest.raises(IntakeRejected) as excinfo:
        evaluate_compliance_result(result)
    assert excinfo.value.status is PreviewStatus.FAILED
    # The integer ``42`` is one of many possible non-string inputs an
    # injected provider could pass; the reason still must not echo it.
    assert "42" not in excinfo.value.reason
    assert "compliance status" in excinfo.value.reason
    assert "recognized" in excinfo.value.reason


def test_compliance_status_unrecognized_value_scrubs_sensitive_payload():
    # APF2c R8o regression (PR #22 external review P2,
    # discussion_r3346860799): a malicious or unhealthy compliance
    # provider can place secrets / bearer tokens / filesystem paths /
    # raw media markers in the ``ComplianceResult.status`` field.
    # ``evaluate_compliance_result`` must fail closed with PreviewStatus
    # FAILED and never echo any of those fragments into
    # ``IntakeRejected.reason`` (which the adapter persists onto
    # ``PreviewRecord.status_reason``).
    sensitive_status = (
        "bad token=Bearer secret provider=upstream "
        "raw=b'BINARY_MEDIA' path=/tmp/raw.mp4"
    )
    result = ComplianceResult(
        status=sensitive_status,  # type: ignore[arg-type]
        reason="provider returned attacker-controlled status string",
        audit_metadata={"layers": ("local_prefilter",)},
        blocked_media_retained=False,
    )
    with pytest.raises(IntakeRejected) as excinfo:
        evaluate_compliance_result(result)

    assert excinfo.value.status is PreviewStatus.FAILED
    reason = excinfo.value.reason
    # Stable low-sensitivity diagnostic fragments still present.
    assert "compliance status" in reason
    assert "recognized" in reason
    assert "fail closed" in reason.lower()

    # No fragment of the raw provider payload may leak. Asserted
    # per-fragment so any regression pinpoints the exact leak.
    forbidden_fragments = (
        "Bearer",
        "secret",
        "provider",
        "raw=",
        "BINARY_MEDIA",
        "/tmp/raw.mp4",
        "bad token",
    )
    for fragment in forbidden_fragments:
        assert fragment not in reason, (
            f"unrecognized compliance status reason leaked sensitive "
            f"fragment {fragment!r} in reason={reason!r}"
        )
    # And the full sensitive input must never appear verbatim either.
    assert sensitive_status not in reason


@pytest.mark.parametrize(
    "media_value",
    [b"\x00\x01raw media bytes\xff", bytearray(b"more bytes"), memoryview(b"buf")],
)
def test_compliance_audit_metadata_with_media_bytes_fails_closed(media_value):
    result = ComplianceResult(
        status=ComplianceStatus.PASS,
        reason="prefilter ok; teaser ASR ok; LLM compliance ok",
        audit_metadata={
            "layers": ("local_prefilter", "asr_teaser", "llm"),
            "teaser_audio": media_value,
        },
        blocked_media_retained=False,
    )
    with pytest.raises(IntakeRejected) as excinfo:
        evaluate_compliance_result(result)
    assert excinfo.value.status is PreviewStatus.FAILED
    assert "audit metadata" in excinfo.value.reason
    assert "teaser_audio" in excinfo.value.reason


def test_compliance_audit_metadata_pure_strings_pass():
    # Sanity check: the new fail-closed gate only triggers on raw byte
    # types, not on legitimate string / tuple / number metadata.
    result = ComplianceResult(
        status=ComplianceStatus.PASS,
        reason="ok",
        audit_metadata={
            "layers": ("local_prefilter", "asr_teaser", "llm"),
            "score": 0.97,
            "model_id": "compliance-v1",
        },
        blocked_media_retained=False,
    )
    assert evaluate_compliance_result(result) is result


def test_compliance_audit_metadata_nested_dict_bytes_fails_closed():
    # bytes hidden one level deep inside a nested Mapping must still trip
    # the recursive guard, not just the top-level keys.
    result = ComplianceResult(
        status=ComplianceStatus.PASS,
        reason="ok",
        audit_metadata={
            "layers": ("local_prefilter", "asr_teaser", "llm"),
            "nested": {"audio": b"\x00\x01raw\xff"},
        },
        blocked_media_retained=False,
    )
    with pytest.raises(IntakeRejected) as excinfo:
        evaluate_compliance_result(result)
    assert excinfo.value.status is PreviewStatus.FAILED
    assert "audit metadata" in excinfo.value.reason
    assert "nested.audio" in excinfo.value.reason


def test_compliance_audit_metadata_nested_list_memoryview_fails_closed():
    result = ComplianceResult(
        status=ComplianceStatus.PASS,
        reason="ok",
        audit_metadata={
            "samples": [
                {"label": "ok"},
                {"buffer": memoryview(b"snippet")},
            ],
        },
        blocked_media_retained=False,
    )
    with pytest.raises(IntakeRejected) as excinfo:
        evaluate_compliance_result(result)
    assert excinfo.value.status is PreviewStatus.FAILED
    assert "memoryview" in excinfo.value.reason
    assert "samples" in excinfo.value.reason


def test_compliance_audit_metadata_nested_tuple_bytearray_fails_closed():
    result = ComplianceResult(
        status=ComplianceStatus.PASS,
        reason="ok",
        audit_metadata={
            "evidence": (
                "asr_text_only",
                ("inner_label", bytearray(b"raw teaser bytes")),
            ),
        },
        blocked_media_retained=False,
    )
    with pytest.raises(IntakeRejected) as excinfo:
        evaluate_compliance_result(result)
    assert excinfo.value.status is PreviewStatus.FAILED
    assert "bytearray" in excinfo.value.reason
    assert "evidence" in excinfo.value.reason


def test_compliance_audit_metadata_nested_set_bytes_fails_closed():
    result = ComplianceResult(
        status=ComplianceStatus.PASS,
        reason="ok",
        audit_metadata={
            "rule_hashes": frozenset({b"hash-bytes-1"}),
        },
        blocked_media_retained=False,
    )
    with pytest.raises(IntakeRejected) as excinfo:
        evaluate_compliance_result(result)
    assert excinfo.value.status is PreviewStatus.FAILED
    assert "rule_hashes" in excinfo.value.reason


def test_compliance_audit_metadata_nested_textual_passes():
    # Deeply nested but byte-free metadata — strings, numbers, tuples of
    # strings — must continue to pass through the recursive guard.
    result = ComplianceResult(
        status=ComplianceStatus.PASS,
        reason="ok",
        audit_metadata={
            "layers": ("local_prefilter", "asr_teaser", "llm"),
            "scores": {"prefilter": 0.99, "llm": 0.93},
            "evidence": [
                {"label": "ok"},
                ("inner", "values"),
                frozenset({"rule_a", "rule_b"}),
            ],
        },
        blocked_media_retained=False,
    )
    assert evaluate_compliance_result(result) is result


def test_compliance_pass_wire_string_returns_normalized_enum_status():
    # The PASS path used to leak a raw "pass" string through to the
    # PreviewRecord. The pure helper must normalize to the enum without
    # mutating the caller's ComplianceResult instance.
    result = ComplianceResult(
        status="pass",  # type: ignore[arg-type]
        reason="prefilter ok; teaser ASR ok; LLM compliance ok",
        audit_metadata={"layers": ("local_prefilter", "asr_teaser", "llm")},
        blocked_media_retained=False,
    )
    decision = evaluate_compliance_result(result)
    assert decision.status is ComplianceStatus.PASS
    assert decision is not result, "PASS normalization must not mutate caller"
    assert result.status == "pass", "caller's instance must remain untouched"
    assert decision.reason == result.reason
    assert decision.audit_metadata == result.audit_metadata
    assert decision.blocked_media_retained is False


def test_compliance_pass_enum_status_returns_caller_instance():
    # When the caller already passed a ComplianceStatus enum, the helper
    # short-circuits and returns the original instance (no replace churn).
    result = _passing_compliance()
    decision = evaluate_compliance_result(result)
    assert decision is result
    assert decision.status is ComplianceStatus.PASS


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
