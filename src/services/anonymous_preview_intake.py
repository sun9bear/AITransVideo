"""APF2b anonymous preview intake — pure contract module.

This module is the production landing of the contract pinned in
``docs/plans/2026-06-02-apf2-anonymous-intake-contract.md`` and exercised
by ``tests/test_apf2_anonymous_intake_contract.py``. It is intentionally
side-effect-free:

* no filesystem reads/writes of any kind (no ``Path.exists()`` /
  ``Path.is_*()`` / ``open()`` / ``read_*()`` / ``write_*()``);
* no network calls;
* no calls to ASR / LLM / TTS / clone / preview media providers;
* no imports of ``gateway``, ``frontend-next``, ``src.pipeline`` or any
  real ``services.jobs`` / ``services.tts`` / ``services.voice_clone``
  module.

What it provides:

* contract dataclasses, enums and typed constants;
* pure validation helpers for source-type / upload / probe / compliance
  decisions;
* fail-closed decision helpers (``IntakeRejected`` exception + status);
* a status-only ``PreviewRecord`` builder.

The real APF2c backend wiring must consume this module — counter stores,
upload handlers, probe and compliance providers stay outside.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple


# ---------------------------------------------------------------------------
# Typed constants — pinned from the Human-approved decision table.
# ---------------------------------------------------------------------------

SHANGHAI = timezone(timedelta(hours=8))

DEFAULT_SESSION_TTL_SECONDS: int = 24 * 3600
DEFAULT_ALLOWED_UPLOAD_TYPES: Tuple[str, ...] = ("mp4", "mov", "m4v", "webm")
DEFAULT_MAX_UPLOAD_BYTES: int = 500 * 1024 * 1024
DEFAULT_MAX_SOURCE_DURATION_SECONDS: int = 30 * 60
DEFAULT_PREVIEW_RECORD_TTL_SECONDS: int = 24 * 3600
DEFAULT_COMPLIANCE_AUDIT_RETENTION_SECONDS: int = 30 * 86400

DEFAULT_RATE_LIMIT_GLOBAL_PER_DAY: int = 500
DEFAULT_RATE_LIMIT_PER_IP_PER_DAY: int = 3
DEFAULT_RATE_LIMIT_PER_DEVICE_PER_DAY: int = 2
DEFAULT_RATE_LIMIT_PER_SOURCE_HASH_PER_DAY: int = 1


# ---------------------------------------------------------------------------
# Enums.
# ---------------------------------------------------------------------------


class SourceType(str, Enum):
    LOCAL_UPLOAD = "local_upload"
    YOUTUBE_URL = "youtube_url"


class PreviewStatus(str, Enum):
    CREATED = "created"
    SOURCE_UPLOADING = "source_uploading"
    SOURCE_READY = "source_ready"
    PROBING = "probing"
    COMPLIANCE_CHECKING = "compliance_checking"
    READY_FOR_MODE = "ready_for_mode"
    REJECTED = "rejected"
    RATE_LIMITED = "rate_limited"
    SOFT_REJECTED = "soft_rejected"
    FAILED = "failed"
    EXPIRED = "expired"


class ComplianceStatus(str, Enum):
    PASS = "pass"
    BLOCK = "block"
    NEEDS_MANUAL_REVIEW = "needs_manual_review"


# Field names forbidden on a status-only PreviewRecord. Listed here as a
# module-level invariant so callers (and tests) can re-use it.
FORBIDDEN_PREVIEW_RECORD_FIELDS: frozenset[str] = frozenset(
    {
        "preview_artifact_key",
        "preview_url",
        "download_url",
        "clone_provider_voice_id",
        "clone_reservation_id",
        "voice_clone_voice_id",
        "payment_token",
        "pricing_quote",
        "credit_reservation_id",
    }
)


# ---------------------------------------------------------------------------
# Contract value objects.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntakeConfig:
    """Immutable configuration consumed by the intake decision helpers.

    Any field missing or set to an invalid value MUST fail closed at the
    call site — helpers in this module check the relevant subset.
    """

    session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS
    allowed_upload_types: Tuple[str, ...] = DEFAULT_ALLOWED_UPLOAD_TYPES
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    max_source_duration_seconds: int = DEFAULT_MAX_SOURCE_DURATION_SECONDS
    single_request_upload_only: bool = True
    temp_upload_dir: Optional[Path] = None
    temp_storage_available: bool = False
    temp_upload_ttl_seconds: int = 24 * 3600
    preview_record_ttl_seconds: int = DEFAULT_PREVIEW_RECORD_TTL_SECONDS
    rate_limit_global_per_day: int = DEFAULT_RATE_LIMIT_GLOBAL_PER_DAY
    rate_limit_per_ip_per_day: int = DEFAULT_RATE_LIMIT_PER_IP_PER_DAY
    rate_limit_per_device_per_day: int = DEFAULT_RATE_LIMIT_PER_DEVICE_PER_DAY
    rate_limit_per_source_hash_per_day: int = DEFAULT_RATE_LIMIT_PER_SOURCE_HASH_PER_DAY
    compliance_audit_retention_seconds: int = DEFAULT_COMPLIANCE_AUDIT_RETENTION_SECONDS
    youtube_enabled_for_anonymous: bool = False
    youtube_enabled_for_free: bool = False
    escalate_to_login_after_rate_limit: bool = True


@dataclass
class AnonymousSession:
    session_id_hash: str
    created_at: datetime
    expires_at: datetime
    ip_hash: str
    device_cookie_hash: str
    source_hash: Optional[str] = None
    escalated_to_login: bool = False


@dataclass
class UploadIntake:
    file_name: str
    byte_length: int
    duration_seconds: float
    source_hash: str
    stored_path: Path
    is_chunked: bool = False

    @property
    def extension(self) -> str:
        return Path(self.file_name).suffix.lstrip(".").lower()


@dataclass
class ProbeResult:
    duration_seconds: float
    source_hash: str
    media_type: str
    audio_present: bool
    audio_quality_score: float
    teaser_candidate_range: Tuple[float, float]
    failure_reason: Optional[str] = None


@dataclass
class ComplianceResult:
    status: ComplianceStatus
    reason: str
    audit_metadata: Mapping[str, object]
    blocked_media_retained: bool = False
    failure_reason: Optional[str] = None


@dataclass
class PreviewRecord:
    """Status-only intake record. APF2 callers must NOT extend this with
    preview media / download / clone / pricing / payment fields. The
    ``FORBIDDEN_PREVIEW_RECORD_FIELDS`` set above documents the explicit
    deny list."""

    record_id: str
    session_id_hash: str
    source_hash: str
    upload_hash: str
    source_type: SourceType
    status: PreviewStatus
    status_reason: str
    duration_seconds: float
    audio_present: bool
    compliance_status: Optional[ComplianceStatus]
    compliance_audit_metadata: Mapping[str, object]
    created_at: datetime
    expires_at: datetime
    selected_mode_placeholder: Optional[str] = None
    recommended_mode_placeholder: Optional[str] = None
    claim_token_placeholder: Optional[str] = None


# ---------------------------------------------------------------------------
# Exceptions.
# ---------------------------------------------------------------------------


class IntakeRejected(Exception):
    """Raised by any contract gate that fails — carries a ``PreviewStatus``
    so the caller can translate it to a status-only preview record."""

    def __init__(self, status: PreviewStatus, reason: str):
        super().__init__(f"{status.value}: {reason}")
        self.status = status
        self.reason = reason


# ---------------------------------------------------------------------------
# Pure helpers — config / source / upload / compliance / record builder.
# ---------------------------------------------------------------------------


def require_config(config: Optional[IntakeConfig]) -> IntakeConfig:
    """Fail closed when ``IntakeConfig`` is not injected."""

    if config is None:
        raise IntakeRejected(
            PreviewStatus.FAILED, "IntakeConfig is missing (fail closed)"
        )
    return config


def admit_source(
    config: IntakeConfig,
    *,
    source_type: SourceType,
    is_free_user: bool,
) -> None:
    """Reject YouTube for anonymous and Free paths (C19). APF2 also rejects
    Trial/Paid YouTube — those land in a later phase."""

    if source_type is SourceType.YOUTUBE_URL:
        if (
            not config.youtube_enabled_for_anonymous
            or (is_free_user and not config.youtube_enabled_for_free)
        ):
            raise IntakeRejected(
                PreviewStatus.REJECTED,
                "youtube_url is not available to anonymous or free users",
            )
        raise IntakeRejected(
            PreviewStatus.REJECTED,
            "youtube_url path is out of scope for APF2",
        )


def admit_upload(config: IntakeConfig, upload: UploadIntake) -> None:
    """Enforce upload allow-list, size, duration, chunked-upload and temp
    storage gates (C4–C9).

    Real temp storage availability is a pure caller-provided signal via
    ``IntakeConfig.temp_storage_available``. This module never touches the
    filesystem; the caller (APF2c backend wiring) is responsible for
    probing storage health and threading the boolean through the config.
    """

    if config.temp_upload_dir is None:
        raise IntakeRejected(
            PreviewStatus.FAILED,
            "temp_upload_dir is not configured (fail closed)",
        )
    if not config.temp_storage_available:
        raise IntakeRejected(
            PreviewStatus.FAILED,
            "temp_storage_available is False (fail closed)",
        )
    if upload.is_chunked and config.single_request_upload_only:
        raise IntakeRejected(
            PreviewStatus.REJECTED,
            "chunked upload is not supported in APF2",
        )
    if upload.extension not in config.allowed_upload_types:
        raise IntakeRejected(
            PreviewStatus.REJECTED,
            f"upload extension {upload.extension!r} is not allowed",
        )
    if upload.byte_length > config.max_upload_bytes:
        raise IntakeRejected(
            PreviewStatus.REJECTED,
            f"upload bytes {upload.byte_length} exceed cap",
        )
    if upload.duration_seconds > config.max_source_duration_seconds:
        raise IntakeRejected(
            PreviewStatus.REJECTED,
            f"upload duration {upload.duration_seconds} exceeds intake cap",
        )


def evaluate_probe_result(probe_result: ProbeResult) -> ProbeResult:
    """Fail closed when probe reported a ``failure_reason`` (C15)."""

    if probe_result.failure_reason:
        raise IntakeRejected(
            PreviewStatus.FAILED,
            f"probe failure: {probe_result.failure_reason}",
        )
    return probe_result


def _find_media_bytes_path(
    value: Any, path: str = ""
) -> Optional[Tuple[str, str]]:
    """Recursively scan a value for ``bytes`` / ``bytearray`` / ``memoryview``
    inside ``Mapping`` / ``list`` / ``tuple`` / ``set`` / ``frozenset``
    containers. Returns ``(path, type_name)`` for the first offender or
    ``None`` when the value tree is clean.

    Strings are intentionally treated as scalars even though they are
    iterable — only raw media byte types are forbidden.
    """

    if isinstance(value, (bytes, bytearray, memoryview)):
        return (path or "<root>", type(value).__name__)
    if isinstance(value, Mapping):
        for key, child in value.items():
            sub_path = f"{path}.{key}" if path else str(key)
            hit = _find_media_bytes_path(child, sub_path)
            if hit is not None:
                return hit
    elif isinstance(value, (list, tuple, set, frozenset)):
        for index, child in enumerate(value):
            sub_path = f"{path}[{index}]" if path else f"[{index}]"
            hit = _find_media_bytes_path(child, sub_path)
            if hit is not None:
                return hit
    return None


def evaluate_compliance_result(result: ComplianceResult) -> ComplianceResult:
    """Convert a ComplianceResult to a fail-closed decision (C15–C18).

    Rules:

    * ``blocked_media_retained=True`` is a contract violation → ``FAILED``;
    * ``audit_metadata`` containing ``bytes`` / ``bytearray`` / ``memoryview``
      anywhere in the value tree (including nested ``Mapping`` / ``list`` /
      ``tuple`` / ``set`` / ``frozenset`` containers) is a contract
      violation → ``FAILED``;
    * ``status`` that does not coerce to ``ComplianceStatus`` is a contract
      violation → ``FAILED`` (wire-string ``"block"`` is normalized to the
      enum so identity checks below cannot silently misroute it to ``PASS``);
    * ``BLOCK`` → ``REJECTED``;
    * ``NEEDS_MANUAL_REVIEW`` → ``SOFT_REJECTED`` (anonymous soft reject);
    * ``PASS`` → return a ``ComplianceResult`` whose ``status`` is the
      ``ComplianceStatus.PASS`` enum (the caller's object is not mutated;
      when the input was already the enum the original instance is
      returned unchanged).

    Exceptions / timeouts raised by upstream compliance providers must be
    converted to a ``FAILED`` ``IntakeRejected`` by the caller — this
    helper is pure and only inspects the value.
    """

    if result.blocked_media_retained:
        raise IntakeRejected(
            PreviewStatus.FAILED,
            "blocked media bytes must not be retained",
        )
    hit = _find_media_bytes_path(result.audit_metadata)
    if hit is not None:
        offending_path, type_name = hit
        raise IntakeRejected(
            PreviewStatus.FAILED,
            f"compliance audit metadata must not retain raw media bytes "
            f"(path={offending_path!r}, type={type_name})",
        )
    status_input = result.status
    if isinstance(status_input, ComplianceStatus):
        status = status_input
        normalized = result
    else:
        try:
            status = ComplianceStatus(status_input)
        except (TypeError, ValueError) as exc:
            raise IntakeRejected(
                PreviewStatus.FAILED,
                f"compliance status is not a recognized ComplianceStatus "
                f"({status_input!r})",
            ) from exc
        normalized = replace(result, status=status)
    if status is ComplianceStatus.BLOCK:
        raise IntakeRejected(
            PreviewStatus.REJECTED,
            f"compliance block: {result.reason}",
        )
    if status is ComplianceStatus.NEEDS_MANUAL_REVIEW:
        raise IntakeRejected(
            PreviewStatus.SOFT_REJECTED,
            "anonymous needs_manual_review treated as soft reject",
        )
    return normalized


def fail_closed_from_exception(
    stage: str, exc: BaseException
) -> IntakeRejected:
    """Build a ``FAILED`` ``IntakeRejected`` for upstream exceptions
    raised during probe or compliance evaluation. Callers should
    ``raise fail_closed_from_exception(...) from exc``.

    This is intentionally a builder (not a raiser) so the caller can
    preserve ``from exc`` chaining at the call site.
    """

    return IntakeRejected(
        PreviewStatus.FAILED,
        f"{stage} error (fail closed): {exc}",
    )


def build_anonymous_session(
    config: IntakeConfig,
    *,
    session_id_hash: str,
    ip_hash: str,
    device_cookie_hash: str,
    now: datetime,
) -> AnonymousSession:
    """Construct a hash-only ``AnonymousSession`` with a 24h TTL (C1–C3)."""

    return AnonymousSession(
        session_id_hash=session_id_hash,
        created_at=now,
        expires_at=now + timedelta(seconds=config.session_ttl_seconds),
        ip_hash=ip_hash,
        device_cookie_hash=device_cookie_hash,
    )


def build_preview_record(
    config: IntakeConfig,
    *,
    session: AnonymousSession,
    upload: UploadIntake,
    probe_result: ProbeResult,
    compliance_result: ComplianceResult,
    source_type: SourceType = SourceType.LOCAL_UPLOAD,
    now: datetime,
) -> PreviewRecord:
    """Assemble a status-only ``PreviewRecord`` (C10 / C11 / C22 / C24).

    The record carries no preview media key, download URL, clone voice
    id, pricing or payment fields; the class definition is the source of
    truth and ``FORBIDDEN_PREVIEW_RECORD_FIELDS`` documents the deny list
    for downstream contract tests.
    """

    return PreviewRecord(
        record_id=f"prv_{upload.source_hash[:12]}",
        session_id_hash=session.session_id_hash,
        source_hash=upload.source_hash,
        upload_hash=upload.source_hash,
        source_type=source_type,
        status=PreviewStatus.READY_FOR_MODE,
        status_reason="intake complete; awaiting APF3 preview pipeline",
        duration_seconds=probe_result.duration_seconds,
        audio_present=probe_result.audio_present,
        compliance_status=compliance_result.status,
        compliance_audit_metadata=dict(compliance_result.audit_metadata),
        created_at=now,
        expires_at=now + timedelta(seconds=config.preview_record_ttl_seconds),
        selected_mode_placeholder=None,
        recommended_mode_placeholder=None,
        claim_token_placeholder=None,
    )


__all__ = [
    # constants
    "SHANGHAI",
    "DEFAULT_SESSION_TTL_SECONDS",
    "DEFAULT_ALLOWED_UPLOAD_TYPES",
    "DEFAULT_MAX_UPLOAD_BYTES",
    "DEFAULT_MAX_SOURCE_DURATION_SECONDS",
    "DEFAULT_PREVIEW_RECORD_TTL_SECONDS",
    "DEFAULT_COMPLIANCE_AUDIT_RETENTION_SECONDS",
    "DEFAULT_RATE_LIMIT_GLOBAL_PER_DAY",
    "DEFAULT_RATE_LIMIT_PER_IP_PER_DAY",
    "DEFAULT_RATE_LIMIT_PER_DEVICE_PER_DAY",
    "DEFAULT_RATE_LIMIT_PER_SOURCE_HASH_PER_DAY",
    "FORBIDDEN_PREVIEW_RECORD_FIELDS",
    # enums
    "SourceType",
    "PreviewStatus",
    "ComplianceStatus",
    # data classes
    "IntakeConfig",
    "AnonymousSession",
    "UploadIntake",
    "ProbeResult",
    "ComplianceResult",
    "PreviewRecord",
    # exception
    "IntakeRejected",
    # helpers
    "require_config",
    "admit_source",
    "admit_upload",
    "evaluate_probe_result",
    "evaluate_compliance_result",
    "fail_closed_from_exception",
    "build_anonymous_session",
    "build_preview_record",
]
