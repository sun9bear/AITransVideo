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

# Redacted status reason for any probe failure surfaced via
# ``IntakeRejected`` / ``PreviewRecord.status_reason``. ``ProbeResult.
# failure_reason`` routinely embeds ``ffprobe`` / ``ffmpeg`` stderr,
# temp paths, ``token=...`` / ``provider=...`` / ``media_id=...``
# fragments and tracebacks; the raw text must never be persisted on the
# low-trust audit field. Mirrors the R7b fail-closed redaction style
# applied to ``fail_closed_from_exception`` and the compliance
# ``failure_reason`` branch.
PROBE_FAILURE_STATUS_REASON: str = "probe failure (details redacted)"


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
    deny list.

    ``login_escalation_hint`` is a status-only signal (not preview /
    clone / pricing surface) that the caller — typically a future
    frontend or claim/UX layer — uses to decide whether to render a
    "log in to keep trying" affordance after a ``RATE_LIMITED`` reply.
    The pure intake module never reads it; the adapter populates it via
    ``IntakeRejected.login_escalation_hint`` so that the rendered
    ``PreviewRecord`` distinguishes
    ``IntakeConfig.escalate_to_login_after_rate_limit=True`` from
    ``False`` instead of only mutating a transient ``AnonymousSession``.
    APF2 contract C23 ("rate-limit overflow may escalate to login but
    must not introduce a captcha / third-party dependency") is honored
    by surfacing only this boolean — no opaque token, no redirect URL,
    no third-party challenge.
    """

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
    login_escalation_hint: Optional[bool] = None


# ---------------------------------------------------------------------------
# Exceptions.
# ---------------------------------------------------------------------------


class IntakeRejected(Exception):
    """Raised by any contract gate that fails — carries a ``PreviewStatus``
    so the caller can translate it to a status-only preview record.

    ``login_escalation_hint`` threads APF2 C23's login-escalation signal
    from the rate-limit decision site through the exception so the
    adapter's status-only ``PreviewRecord`` can carry the hint without
    relying on a transient ``AnonymousSession`` mutation. The pure
    intake helpers leave it ``None`` everywhere except the rate-limit
    branch wired by the adapter; the field stays optional so all
    existing call sites keep working unchanged.
    """

    def __init__(
        self,
        status: PreviewStatus,
        reason: str,
        *,
        login_escalation_hint: Optional[bool] = None,
    ):
        super().__init__(f"{status.value}: {reason}")
        self.status = status
        self.reason = reason
        self.login_escalation_hint = login_escalation_hint


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
    Trial/Paid YouTube — those land in a later phase.

    ``source_type`` is normalized via value comparison so that both the
    ``SourceType.YOUTUBE_URL`` enum and the raw wire-level ``"youtube_url"``
    string land on the same rejection. An unrecognized value fails closed
    via ``PreviewStatus.FAILED`` — it must not silently pass as a local
    upload.
    """

    if isinstance(source_type, SourceType):
        normalized = source_type
    else:
        try:
            normalized = SourceType(source_type)
        except (TypeError, ValueError) as exc:
            raise IntakeRejected(
                PreviewStatus.FAILED,
                f"source_type is not a recognized SourceType "
                f"({source_type!r})",
            ) from exc
    if normalized is SourceType.YOUTUBE_URL:
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
    # APF2c R8r (PR #22 external review P2 discussion_r3347449325): the
    # ``in`` membership test below silently degrades to a substring scan
    # when ``allowed_upload_types`` is mis-injected as a raw string
    # (e.g. ``"mp4,mov,m4v,webm"``), so ``clip.m`` / ``clip.web`` would
    # leak through. Verify the runtime container type before the lookup
    # and fail closed on anything other than a ``tuple`` of ``str``. The
    # raw config value is intentionally NOT interpolated into
    # ``IntakeRejected.reason`` because that string lands on
    # ``PreviewRecord.status_reason`` — a persisted, low-trust audit
    # field. Mirrors the R7b / R8l / R8o fail-closed redaction style.
    allowed_types = config.allowed_upload_types
    if not isinstance(allowed_types, tuple):
        raise IntakeRejected(
            PreviewStatus.FAILED,
            "allowed_upload_types must be a tuple (fail closed)",
        )
    for entry in allowed_types:
        if not isinstance(entry, str):
            raise IntakeRejected(
                PreviewStatus.FAILED,
                "allowed_upload_types entries must be strings (fail closed)",
            )
    if upload.extension not in allowed_types:
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
    """Fail closed when probe reported a ``failure_reason`` (C15).

    The raw ``ProbeResult.failure_reason`` is intentionally NOT
    interpolated into ``IntakeRejected.reason``. Probe wrappers
    routinely embed ``ffprobe`` / ``ffmpeg`` stderr text, temp
    filesystem paths, ``token=...`` / ``provider=...`` / ``media_id=...``
    fragments and stack tracebacks inside ``failure_reason``, and that
    string lands on ``PreviewRecord.status_reason`` — a persisted,
    low-trust audit surface that must stay free of provider payloads,
    credentials and raw media markers. The redacted constant
    ``PROBE_FAILURE_STATUS_REASON`` is the only audit fragment surfaced
    here, matching the R7b fail-closed redaction style applied to
    ``fail_closed_from_exception`` and the compliance ``failure_reason``
    branch.
    """

    if probe_result.failure_reason:
        raise IntakeRejected(
            PreviewStatus.FAILED,
            PROBE_FAILURE_STATUS_REASON,
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

    Both Mapping **keys** and values are scanned. APF2c R8zg fix #1
    (PR #22 external review P2, discussion_r3348801401): a compliance
    provider that smuggles raw media bytes via a ``Mapping`` key would
    otherwise pass the value-only scan silently and let
    ``build_preview_record`` persist the bytes inside
    ``PreviewRecord.compliance_audit_metadata``. The key is recursed
    through the same helper so nested container keys (e.g. a tuple key
    holding bytes) also fail closed.

    APF2c R8zl (PR #22 external review P2, discussion_r3348801401 /
    discussion_r3349214069): neither the key nor the value-at-key sub
    path may embed the raw mapping key. Both legs use stable generic
    markers — ``<key>`` for "we descended into the key itself" and
    ``<value>`` for "we descended into the value stored at that key"
    — so the returned ``path`` never echoes raw mapping key text,
    ``repr(key)``, filesystem paths, provider keys, bearer tokens or
    other attacker-/leak-controlled snippets into
    ``IntakeRejected.reason`` → ``PreviewRecord.status_reason``
    (a persisted low-trust audit field). Mirrors the R7b / R8l / R8o /
    R8v / R8z fail-closed redaction style applied to probe / compliance
    ``failure_reason`` / ``status`` / BLOCK ``reason``.
    """

    if isinstance(value, (bytes, bytearray, memoryview)):
        return (path or "<root>", type(value).__name__)
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_path = f"{path}.<key>" if path else "<key>"
            hit = _find_media_bytes_path(key, key_path)
            if hit is not None:
                return hit
            if isinstance(key, (bytes, bytearray, memoryview)):
                # Defensive belt-and-suspenders: the recursive call
                # above already returns the offender, but keep the
                # explicit guard so a future refactor cannot
                # accidentally regress the key-side scan. ``key_path``
                # is a literal ``<key>`` marker; the raw key bytes are
                # never embedded.
                return (key_path, type(key).__name__)
            # Generic ``<value>`` marker — never ``str(key)`` / ``{key}``
            # — so a key whose textual content carries a token /
            # filesystem path / provider key / user-supplied snippet
            # cannot leak through this branch into the returned path.
            sub_path = f"{path}.<value>" if path else "<value>"
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
    * ``failure_reason`` non-empty (even when ``status=PASS``) →
      ``FAILED``. A populated ``failure_reason`` is a contract-level
      "the compliance layer is not healthy" marker that must NEVER be
      ignored, even if the wrapper also reports ``PASS`` — fail closed.
      The actual ``failure_reason`` string is not echoed into
      ``status_reason`` to avoid leaking provider payloads / tokens /
      paths / raw media markers.

    Exceptions / timeouts raised by upstream compliance providers must be
    converted to a ``FAILED`` ``IntakeRejected`` by the caller — this
    helper is pure and only inspects the value.
    """

    if result.blocked_media_retained:
        raise IntakeRejected(
            PreviewStatus.FAILED,
            "blocked media bytes must not be retained",
        )
    if result.failure_reason:
        # Fail closed on any non-empty ``failure_reason`` regardless of
        # the reported ``status``. A compliance wrapper that returns
        # ``ComplianceResult(status=PASS, failure_reason=...)`` must
        # never advance to ``READY_FOR_MODE`` — the failure reason is a
        # contract-level signal that the upstream compliance layer hit
        # an unrecoverable problem, even if it also chose to label the
        # row ``PASS``. The raw ``failure_reason`` is intentionally NOT
        # interpolated into ``status_reason`` because compliance
        # provider payloads, raw media markers, paths, tokens or
        # exception text routinely land there, and ``status_reason`` is
        # a persisted, low-trust audit field (matches R7b's redacted
        # fail-closed style).
        raise IntakeRejected(
            PreviewStatus.FAILED,
            "compliance failure_reason present (fail closed)",
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
            # The raw ``status_input`` is intentionally NOT interpolated
            # into ``IntakeRejected.reason``. Injected compliance
            # providers can surface attacker-controlled or accidentally
            # leaky strings (bearer tokens, provider payloads, filesystem
            # paths, raw media markers) inside an unrecognized
            # ``status`` value, and that reason lands on
            # ``PreviewRecord.status_reason`` — a persisted, low-trust
            # audit field. Matches the R7b / R8l fail-closed redaction
            # style applied to probe / compliance ``failure_reason`` and
            # the unrecognized ``source_type`` branch.
            raise IntakeRejected(
                PreviewStatus.FAILED,
                "compliance status is not a recognized ComplianceStatus "
                "(fail closed)",
            ) from exc
        normalized = replace(result, status=status)
    if status is ComplianceStatus.BLOCK:
        # APF2c R8z fix #2 (PR #22 external review P2,
        # discussion_r3348103609): the upstream compliance provider may
        # embed provider payloads, bearer tokens, filesystem paths or
        # raw user / media snippets inside ``result.reason``. That
        # string lands on ``IntakeRejected.reason`` and ultimately on
        # ``PreviewRecord.status_reason`` — a persisted, low-trust
        # audit field. Surface only the stable structural marker
        # ``"compliance block (fail closed)"`` so downstream dashboards
        # keep the ``"compliance block"`` substring while the raw
        # provider payload is never echoed back. Mirrors the R7b /
        # R8l / R8o / R8v fail-closed redaction style applied to probe
        # ``failure_reason`` and the unrecognized-status branch.
        raise IntakeRejected(
            PreviewStatus.REJECTED,
            "compliance block (fail closed)",
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
    raised during probe / compliance / rate-limit evaluation. Callers
    should ``raise fail_closed_from_exception(...) from exc``.

    Only the ``stage`` label and the exception's **type name** are
    surfaced on ``IntakeRejected.reason``. The exception's ``str`` /
    ``repr`` is intentionally not included because injected dependencies
    (ffprobe wrappers, compliance providers, rate-limit stores) routinely
    embed raw secrets, bearer tokens, provider payloads, filesystem
    paths, or raw media markers inside their exception messages, and
    ``PreviewRecord.status_reason`` is a persisted, low-trust audit
    field. Surfacing only the type name keeps audit traces useful while
    failing closed without leaking dependency payload.

    The reason format ``"{stage} error (fail closed): dependency
    {ExceptionType}"`` is load-bearing: existing rate-limit and adapter
    contract tests pin the ``"{stage} error (fail closed)"`` substring,
    while ``dependency {ExceptionType}`` preserves the exception-type
    audit fragment without ever interpolating ``str(exc)`` / ``repr(exc)``.

    This is intentionally a builder (not a raiser) so the caller can
    preserve ``from exc`` chaining at the call site.
    """

    return IntakeRejected(
        PreviewStatus.FAILED,
        f"{stage} error (fail closed): dependency {type(exc).__name__}",
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
        login_escalation_hint=None,
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
    "PROBE_FAILURE_STATUS_REASON",
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
