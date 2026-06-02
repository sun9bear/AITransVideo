"""APF2 anonymous intake contract — fake fail-closed test scaffold.

This file is the **contract test scaffold** for the APF2 anonymous intake
flow. It does NOT exercise any real backend, gateway, frontend, upload,
probe, compliance, preview media, clone provider, pricing, payment,
migration or deployment code. Instead, it pins the Human-approved
decisions from
``docs/ai-workgroup/inbox/Human/2026-06-02T171740_from-CodeX_to-Human_type-report_task-APF2-human-gate-decision-table.md``
into a small in-file fake intake runner whose behavior the future real
implementation (APF2c) MUST match.

Design source of truth:
``docs/plans/2026-06-02-apf2-anonymous-intake-contract.md``.

Hard rules enforced by these tests (mirror of the design doc §3):

* fail-closed on missing/invalid config, counter store, probe, compliance;
* anonymous + Free YouTube source disabled at intake;
* upload allow-list ``mp4 / mov / m4v / webm``;
* 500 MB upload cap;
* 30 minute intake duration cap;
* single-request upload only;
* probe failure must not enter translation / TTS / clone;
* compliance exception / timeout / ``needs_manual_review`` must not
  produce preview media or call clone providers;
* status-only preview record contract — no preview media, no clone
  provider, no pricing/payment fields;
* rate-limit counter store on local JSON ``tmp_path`` — fail-closed when
  the store is unavailable;
* no ``skip`` / ``xfail`` markers.

The fake runner intentionally has no I/O against real services. Where a
real implementation would call ASR / LLM / TTS / clone, the runner
records the would-be call into an in-memory ledger that the tests assert
remains empty.
"""

from __future__ import annotations

import ast
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Mapping, MutableMapping, Optional, Tuple

import pytest


# ---------------------------------------------------------------------------
# Contract constants — pinned from the Human-approved decision table.
# ---------------------------------------------------------------------------

SHANGHAI = timezone(timedelta(hours=8))

DEFAULT_SESSION_TTL_SECONDS = 24 * 3600
DEFAULT_ALLOWED_UPLOAD_TYPES: Tuple[str, ...] = ("mp4", "mov", "m4v", "webm")
DEFAULT_MAX_UPLOAD_BYTES = 500 * 1024 * 1024
DEFAULT_MAX_SOURCE_DURATION_SECONDS = 30 * 60
DEFAULT_PREVIEW_RECORD_TTL_SECONDS = 24 * 3600
DEFAULT_COMPLIANCE_AUDIT_RETENTION_SECONDS = 30 * 86400

DEFAULT_RATE_LIMIT_GLOBAL = 500
DEFAULT_RATE_LIMIT_PER_IP = 3
DEFAULT_RATE_LIMIT_PER_DEVICE = 2
DEFAULT_RATE_LIMIT_PER_SOURCE_HASH = 1


# ---------------------------------------------------------------------------
# Contract data classes (design §3.1–§3.7).
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


@dataclass(frozen=True)
class IntakeConfig:
    session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS
    allowed_upload_types: Tuple[str, ...] = DEFAULT_ALLOWED_UPLOAD_TYPES
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    max_source_duration_seconds: int = DEFAULT_MAX_SOURCE_DURATION_SECONDS
    single_request_upload_only: bool = True
    temp_upload_dir: Optional[Path] = None
    temp_upload_ttl_seconds: int = 24 * 3600
    preview_record_ttl_seconds: int = DEFAULT_PREVIEW_RECORD_TTL_SECONDS
    rate_limit_global_per_day: int = DEFAULT_RATE_LIMIT_GLOBAL
    rate_limit_per_ip_per_day: int = DEFAULT_RATE_LIMIT_PER_IP
    rate_limit_per_device_per_day: int = DEFAULT_RATE_LIMIT_PER_DEVICE
    rate_limit_per_source_hash_per_day: int = DEFAULT_RATE_LIMIT_PER_SOURCE_HASH
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
    claim_token_placeholder: Optional[str] = None  # APF4 will fill — APF2 keeps None.


# ---------------------------------------------------------------------------
# Fake counter store (design §3.6) — JSON file under tmp_path.
# ---------------------------------------------------------------------------


class RateLimitUnavailable(Exception):
    """Raised when the counter store cannot be read or written."""


class CounterStore:
    """Local JSON counter store. Fail-closed when the file path is unusable."""

    def __init__(self, path: Optional[Path]):
        self._path = path

    def _load(self) -> MutableMapping[str, int]:
        if self._path is None:
            raise RateLimitUnavailable("counter store path is not configured")
        if not self._path.parent.exists():
            raise RateLimitUnavailable(
                f"counter store parent directory missing: {self._path.parent}"
            )
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise RateLimitUnavailable(f"counter store unreadable: {exc}") from exc

    def _save(self, data: Mapping[str, int]) -> None:
        if self._path is None:
            raise RateLimitUnavailable("counter store path is not configured")
        try:
            self._path.write_text(json.dumps(dict(data)), encoding="utf-8")
        except OSError as exc:
            raise RateLimitUnavailable(f"counter store unwritable: {exc}") from exc

    def get(self, key: str) -> int:
        return int(self._load().get(key, 0))

    def increment(self, key: str) -> int:
        data = self._load()
        data[key] = int(data.get(key, 0)) + 1
        self._save(data)
        return data[key]


# ---------------------------------------------------------------------------
# Fake intake runner — encodes the contract.
# ---------------------------------------------------------------------------


class IntakeRejected(Exception):
    """Raised when intake rejects an upload at any contract gate."""

    def __init__(self, status: PreviewStatus, reason: str):
        super().__init__(f"{status.value}: {reason}")
        self.status = status
        self.reason = reason


@dataclass
class IntakeAttempt:
    session: AnonymousSession
    is_free_user: bool
    source_type: SourceType
    upload: Optional[UploadIntake]
    youtube_url: Optional[str]
    day_key: str  # ``YYYY-MM-DD`` in Asia/Shanghai.


class FakeIntakeRunner:
    """Encodes the APF2 anonymous intake contract.

    The runner is deliberately closed-world: it does NOT call any real
    ASR, LLM, TTS or clone provider. Probe and compliance are dependency-
    injected functions so a test can simulate exceptions, timeouts,
    ``needs_manual_review`` and block decisions.
    """

    def __init__(
        self,
        config: Optional[IntakeConfig],
        counter_store: Optional[CounterStore],
        probe_fn: Callable[[UploadIntake], ProbeResult],
        compliance_fn: Callable[[ProbeResult], ComplianceResult],
        now: Optional[Callable[[], datetime]] = None,
    ):
        self._config = config
        self._counter_store = counter_store
        self._probe_fn = probe_fn
        self._compliance_fn = compliance_fn
        self._now = now or (lambda: datetime.now(SHANGHAI))
        # Side-effect ledger: any test asserting "no clone / no TTS / no
        # preview media" reads this. The runner itself never appends to
        # it — these are *forbidden* call sites in APF2.
        self.forbidden_calls: list[str] = []

    # -- Session lifecycle (C1 / C2 / C3) ------------------------------------

    def create_session(
        self,
        *,
        session_id_hash: str,
        ip_hash: str,
        device_cookie_hash: str,
    ) -> AnonymousSession:
        config = self._require_config()
        created = self._now()
        return AnonymousSession(
            session_id_hash=session_id_hash,
            created_at=created,
            expires_at=created + timedelta(seconds=config.session_ttl_seconds),
            ip_hash=ip_hash,
            device_cookie_hash=device_cookie_hash,
        )

    # -- Source-type gate (C19) ----------------------------------------------

    def admit_source(self, attempt: IntakeAttempt) -> None:
        config = self._require_config()
        if attempt.source_type is SourceType.YOUTUBE_URL:
            if (
                not config.youtube_enabled_for_anonymous
                or (attempt.is_free_user and not config.youtube_enabled_for_free)
            ):
                raise IntakeRejected(
                    PreviewStatus.REJECTED,
                    "youtube_url is not available to anonymous or free users",
                )
            # Trial/Paid YouTube is explicitly out of scope for APF2.
            raise IntakeRejected(
                PreviewStatus.REJECTED,
                "youtube_url path is out of scope for APF2",
            )

    # -- Upload gate (C4–C9) -------------------------------------------------

    def admit_upload(self, upload: UploadIntake) -> None:
        config = self._require_config()
        if config.temp_upload_dir is None:
            raise IntakeRejected(
                PreviewStatus.FAILED,
                "temp_upload_dir is not configured (fail closed)",
            )
        if not config.temp_upload_dir.exists():
            raise IntakeRejected(
                PreviewStatus.FAILED,
                "temp_upload_dir does not exist (fail closed)",
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

    # -- Rate limit gate (C12 / C13 / C14 / C23) -----------------------------

    def enforce_rate_limits(
        self,
        *,
        session: AnonymousSession,
        upload: UploadIntake,
        day_key: str,
    ) -> None:
        store = self._counter_store
        if store is None:
            raise IntakeRejected(
                PreviewStatus.FAILED,
                "rate-limit counter store unavailable (fail closed)",
            )
        config = self._require_config()
        keys = [
            (f"global:{day_key}", config.rate_limit_global_per_day),
            (f"ip:{session.ip_hash}:{day_key}", config.rate_limit_per_ip_per_day),
            (
                f"device:{session.device_cookie_hash}:{day_key}",
                config.rate_limit_per_device_per_day,
            ),
            (
                f"source:{upload.source_hash}:{day_key}",
                config.rate_limit_per_source_hash_per_day,
            ),
        ]
        try:
            for key, cap in keys:
                if store.get(key) >= cap:
                    session.escalated_to_login = (
                        config.escalate_to_login_after_rate_limit
                    )
                    raise IntakeRejected(
                        PreviewStatus.RATE_LIMITED,
                        f"rate limit exceeded for {key}",
                    )
            for key, _cap in keys:
                store.increment(key)
        except RateLimitUnavailable as exc:
            raise IntakeRejected(
                PreviewStatus.FAILED,
                f"rate-limit counter store error (fail closed): {exc}",
            ) from exc

    # -- Probe gate (C15) ----------------------------------------------------

    def probe(self, upload: UploadIntake) -> ProbeResult:
        try:
            result = self._probe_fn(upload)
        except Exception as exc:  # noqa: BLE001 — fail closed on any error
            raise IntakeRejected(
                PreviewStatus.FAILED,
                f"probe error (fail closed): {exc}",
            ) from exc
        if result.failure_reason:
            raise IntakeRejected(
                PreviewStatus.FAILED,
                f"probe failure: {result.failure_reason}",
            )
        return result

    # -- Compliance gate (C15–C18) -------------------------------------------

    def evaluate_compliance(self, probe_result: ProbeResult) -> ComplianceResult:
        try:
            result = self._compliance_fn(probe_result)
        except Exception as exc:  # noqa: BLE001 — fail closed on any error
            raise IntakeRejected(
                PreviewStatus.FAILED,
                f"compliance error (fail closed): {exc}",
            ) from exc
        if result.blocked_media_retained:
            # Defensive: contract forbids retaining blocked source bytes.
            raise IntakeRejected(
                PreviewStatus.FAILED,
                "blocked media bytes must not be retained",
            )
        if result.status is ComplianceStatus.BLOCK:
            raise IntakeRejected(
                PreviewStatus.REJECTED,
                f"compliance block: {result.reason}",
            )
        if result.status is ComplianceStatus.NEEDS_MANUAL_REVIEW:
            # Anonymous path soft-rejects manual review.
            raise IntakeRejected(
                PreviewStatus.SOFT_REJECTED,
                "anonymous needs_manual_review treated as soft reject",
            )
        return result

    # -- Preview record assembly (C10 / C11 / C22 / C24) ---------------------

    def build_preview_record(
        self,
        *,
        attempt: IntakeAttempt,
        probe_result: ProbeResult,
        compliance_result: ComplianceResult,
    ) -> PreviewRecord:
        config = self._require_config()
        upload = attempt.upload
        if upload is None:
            raise IntakeRejected(
                PreviewStatus.FAILED,
                "preview record requires a completed upload",
            )
        created = self._now()
        return PreviewRecord(
            record_id=f"prv_{upload.source_hash[:12]}",
            session_id_hash=attempt.session.session_id_hash,
            source_hash=upload.source_hash,
            upload_hash=upload.source_hash,  # upload + source hash collapse in APF2.
            source_type=attempt.source_type,
            status=PreviewStatus.READY_FOR_MODE,
            status_reason="intake complete; awaiting APF3 preview pipeline",
            duration_seconds=probe_result.duration_seconds,
            audio_present=probe_result.audio_present,
            compliance_status=compliance_result.status,
            compliance_audit_metadata=dict(compliance_result.audit_metadata),
            created_at=created,
            expires_at=created
            + timedelta(seconds=config.preview_record_ttl_seconds),
            selected_mode_placeholder=None,
            recommended_mode_placeholder=None,
            claim_token_placeholder=None,
        )

    # -- Internal -----------------------------------------------------------

    def _require_config(self) -> IntakeConfig:
        if self._config is None:
            raise IntakeRejected(
                PreviewStatus.FAILED,
                "IntakeConfig is missing (fail closed)",
            )
        return self._config


# ---------------------------------------------------------------------------
# Pytest fixtures.
# ---------------------------------------------------------------------------


_FROZEN_NOW = datetime(2026, 6, 2, 18, 0, 0, tzinfo=SHANGHAI)


def _frozen_now() -> datetime:
    return _FROZEN_NOW


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


def _passing_compliance(_probe: ProbeResult) -> ComplianceResult:
    return ComplianceResult(
        status=ComplianceStatus.PASS,
        reason="prefilter ok; teaser ASR ok; LLM compliance ok",
        audit_metadata={"layers": ("local_prefilter", "asr_teaser", "llm")},
        blocked_media_retained=False,
        failure_reason=None,
    )


@pytest.fixture
def temp_upload_dir(tmp_path: Path) -> Path:
    target = tmp_path / "anon_intake_uploads"
    target.mkdir(parents=True, exist_ok=True)
    return target


@pytest.fixture
def counter_path(tmp_path: Path) -> Path:
    return tmp_path / "anon_intake_counters.json"


@pytest.fixture
def counter_store(counter_path: Path) -> CounterStore:
    return CounterStore(counter_path)


@pytest.fixture
def config(temp_upload_dir: Path) -> IntakeConfig:
    return IntakeConfig(temp_upload_dir=temp_upload_dir)


@pytest.fixture
def session(config: IntakeConfig) -> AnonymousSession:
    runner = FakeIntakeRunner(
        config=config,
        counter_store=CounterStore(None),
        probe_fn=_passing_probe,
        compliance_fn=_passing_compliance,
        now=_frozen_now,
    )
    return runner.create_session(
        session_id_hash="sess_hash_anon_test",
        ip_hash="ip_hash_anon_test",
        device_cookie_hash="device_hash_anon_test",
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
    stored = temp_upload_dir / file_name
    stored.write_bytes(b"fake-bytes")
    return UploadIntake(
        file_name=file_name,
        byte_length=byte_length,
        duration_seconds=duration_seconds,
        source_hash=source_hash,
        stored_path=stored,
        is_chunked=is_chunked,
    )


def _runner(
    config: Optional[IntakeConfig],
    counter_store: Optional[CounterStore],
    probe_fn: Callable[[UploadIntake], ProbeResult] = _passing_probe,
    compliance_fn: Callable[[ProbeResult], ComplianceResult] = _passing_compliance,
) -> FakeIntakeRunner:
    return FakeIntakeRunner(
        config=config,
        counter_store=counter_store,
        probe_fn=probe_fn,
        compliance_fn=compliance_fn,
        now=_frozen_now,
    )


def _attempt(
    session: AnonymousSession,
    upload: Optional[UploadIntake],
    *,
    is_free_user: bool = False,
    source_type: SourceType = SourceType.LOCAL_UPLOAD,
    youtube_url: Optional[str] = None,
    day_key: str = "2026-06-02",
) -> IntakeAttempt:
    return IntakeAttempt(
        session=session,
        is_free_user=is_free_user,
        source_type=source_type,
        upload=upload,
        youtube_url=youtube_url,
        day_key=day_key,
    )


# ---------------------------------------------------------------------------
# Contract tests (numbering matches design doc §2).
# ---------------------------------------------------------------------------


# C1 — session TTL is exactly 24 hours.
def test_c1_session_ttl_is_24_hours(config, session):
    assert session.expires_at - session.created_at == timedelta(
        seconds=config.session_ttl_seconds
    )
    assert config.session_ttl_seconds == DEFAULT_SESSION_TTL_SECONDS


# C2 — only ``session_id_hash`` is persisted; AnonymousSession has no raw id.
def test_c2_session_persists_hash_only(session):
    assert hasattr(session, "session_id_hash")
    assert not hasattr(session, "session_id")
    # cookie / IP / device are also hashed.
    assert session.ip_hash.startswith("ip_hash")
    assert session.device_cookie_hash.startswith("device_hash")


# C3 — no fingerprint surface on session record.
def test_c3_no_fingerprint_fields_on_session(session):
    forbidden = {
        "fingerprint",
        "user_agent",
        "canvas_hash",
        "webgl_hash",
        "audio_fingerprint",
        "ua_hash",
    }
    fields = set(session.__dict__)
    assert forbidden.isdisjoint(fields), (
        f"anonymous session leaks fingerprint surface: {fields & forbidden}"
    )


# C4 — only mp4/mov/m4v/webm pass; everything else rejects.
@pytest.mark.parametrize("ext", ["mp4", "mov", "m4v", "webm"])
def test_c4_allowed_upload_types_pass(config, counter_store, temp_upload_dir, ext):
    runner = _runner(config, counter_store)
    upload = _make_upload(temp_upload_dir, file_name=f"clip.{ext}")
    runner.admit_upload(upload)


@pytest.mark.parametrize("ext", ["mkv", "avi", "wmv", "mp3", "mp4.exe", ""])
def test_c4_disallowed_upload_types_reject(
    config, counter_store, temp_upload_dir, ext
):
    runner = _runner(config, counter_store)
    file_name = f"clip.{ext}" if ext else "clip"
    upload = _make_upload(temp_upload_dir, file_name=file_name)
    with pytest.raises(IntakeRejected) as excinfo:
        runner.admit_upload(upload)
    assert excinfo.value.status is PreviewStatus.REJECTED


# C5 — upload bytes > 500 MB reject; boundary accepts.
def test_c5_upload_size_boundary(config, counter_store, temp_upload_dir):
    runner = _runner(config, counter_store)
    at_cap = _make_upload(
        temp_upload_dir,
        byte_length=config.max_upload_bytes,
    )
    runner.admit_upload(at_cap)

    over_cap = _make_upload(
        temp_upload_dir,
        byte_length=config.max_upload_bytes + 1,
        file_name="big.mov",
    )
    with pytest.raises(IntakeRejected) as excinfo:
        runner.admit_upload(over_cap)
    assert "exceed cap" in excinfo.value.reason


# C6 — duration > 30 minutes reject; boundary accepts.
def test_c6_intake_duration_boundary(config, counter_store, temp_upload_dir):
    runner = _runner(config, counter_store)
    at_cap = _make_upload(
        temp_upload_dir,
        duration_seconds=config.max_source_duration_seconds,
    )
    runner.admit_upload(at_cap)

    over_cap = _make_upload(
        temp_upload_dir,
        duration_seconds=config.max_source_duration_seconds + 1,
        file_name="long.webm",
    )
    with pytest.raises(IntakeRejected) as excinfo:
        runner.admit_upload(over_cap)
    assert "exceeds intake cap" in excinfo.value.reason


# C7 — chunked upload rejected in APF2.
def test_c7_chunked_upload_rejected(config, counter_store, temp_upload_dir):
    runner = _runner(config, counter_store)
    upload = _make_upload(temp_upload_dir, is_chunked=True)
    with pytest.raises(IntakeRejected) as excinfo:
        runner.admit_upload(upload)
    assert "chunked" in excinfo.value.reason


# C8 — temp upload dir missing or unconfigured fails closed.
def test_c8_missing_temp_upload_dir_fails_closed(counter_store, temp_upload_dir):
    config = IntakeConfig(temp_upload_dir=None)
    runner = _runner(config, counter_store)
    upload = _make_upload(temp_upload_dir)
    with pytest.raises(IntakeRejected) as excinfo:
        runner.admit_upload(upload)
    assert excinfo.value.status is PreviewStatus.FAILED


def test_c8_nonexistent_temp_upload_dir_fails_closed(
    counter_store, temp_upload_dir, tmp_path
):
    missing = tmp_path / "does_not_exist"
    config = IntakeConfig(temp_upload_dir=missing)
    runner = _runner(config, counter_store)
    upload = _make_upload(temp_upload_dir)
    with pytest.raises(IntakeRejected) as excinfo:
        runner.admit_upload(upload)
    assert excinfo.value.status is PreviewStatus.FAILED


# C9 — compliance block carries blocked_media_retained=False and the runner
# refuses to proceed; future cleanup is the implementer's contract obligation.
def test_c9_compliance_block_does_not_retain_media(
    config, counter_store, temp_upload_dir
):
    def blocking_compliance(_probe: ProbeResult) -> ComplianceResult:
        return ComplianceResult(
            status=ComplianceStatus.BLOCK,
            reason="prohibited content",
            audit_metadata={"matched_rule": "demo_rule"},
            blocked_media_retained=False,
        )

    runner = _runner(config, counter_store, compliance_fn=blocking_compliance)
    upload = _make_upload(temp_upload_dir)
    probe = runner.probe(upload)
    with pytest.raises(IntakeRejected) as excinfo:
        runner.evaluate_compliance(probe)
    assert excinfo.value.status is PreviewStatus.REJECTED
    # The forbidden ledger remains empty — no preview media / clone calls.
    assert runner.forbidden_calls == []


def test_c9_retaining_blocked_media_is_a_contract_violation(
    config, counter_store, temp_upload_dir
):
    def malformed_compliance(_probe: ProbeResult) -> ComplianceResult:
        return ComplianceResult(
            status=ComplianceStatus.BLOCK,
            reason="prohibited content",
            audit_metadata={"matched_rule": "demo_rule"},
            blocked_media_retained=True,
        )

    runner = _runner(config, counter_store, compliance_fn=malformed_compliance)
    upload = _make_upload(temp_upload_dir)
    probe = runner.probe(upload)
    with pytest.raises(IntakeRejected) as excinfo:
        runner.evaluate_compliance(probe)
    assert "blocked media bytes must not be retained" in excinfo.value.reason


# C10 — PreviewRecord must be status-only: no preview media / artifact / clone fields.
def test_c10_preview_record_is_status_only(config, counter_store, temp_upload_dir, session):
    runner = _runner(config, counter_store)
    upload = _make_upload(temp_upload_dir)
    probe = runner.probe(upload)
    compliance = runner.evaluate_compliance(probe)
    record = runner.build_preview_record(
        attempt=_attempt(session, upload),
        probe_result=probe,
        compliance_result=compliance,
    )
    forbidden_fields = {
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
    fields = set(record.__dict__)
    assert forbidden_fields.isdisjoint(fields), (
        f"preview record leaks forbidden fields: {fields & forbidden_fields}"
    )


# C11 — preview record TTL = 24h.
def test_c11_preview_record_ttl_is_24h(config, counter_store, temp_upload_dir, session):
    runner = _runner(config, counter_store)
    upload = _make_upload(temp_upload_dir)
    probe = runner.probe(upload)
    compliance = runner.evaluate_compliance(probe)
    record = runner.build_preview_record(
        attempt=_attempt(session, upload),
        probe_result=probe,
        compliance_result=compliance,
    )
    assert record.expires_at - record.created_at == timedelta(
        seconds=DEFAULT_PREVIEW_RECORD_TTL_SECONDS
    )


# C12 / C13 — all four rate-limit dimensions enforced with the documented caps.
def test_c12_c13_rate_limit_caps_pinned():
    config = IntakeConfig()
    assert config.rate_limit_global_per_day == 500
    assert config.rate_limit_per_ip_per_day == 3
    assert config.rate_limit_per_device_per_day == 2
    assert config.rate_limit_per_source_hash_per_day == 1


def test_c13_per_source_hash_cap_enforced(
    config, counter_store, temp_upload_dir, session
):
    runner = _runner(config, counter_store)
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_repeat")
    runner.enforce_rate_limits(session=session, upload=upload, day_key="2026-06-02")
    with pytest.raises(IntakeRejected) as excinfo:
        runner.enforce_rate_limits(
            session=session, upload=upload, day_key="2026-06-02"
        )
    assert excinfo.value.status is PreviewStatus.RATE_LIMITED
    assert "source:src_hash_repeat" in excinfo.value.reason


def test_c13_per_device_cap_enforced(
    config, counter_store, temp_upload_dir, session
):
    runner = _runner(config, counter_store)
    # device cap is 2 — distinct source hashes to bypass the source-hash cap.
    for i in range(config.rate_limit_per_device_per_day):
        upload = _make_upload(
            temp_upload_dir, source_hash=f"src_hash_device_{i}"
        )
        runner.enforce_rate_limits(
            session=session, upload=upload, day_key="2026-06-02"
        )
    overflow = _make_upload(temp_upload_dir, source_hash="src_hash_device_overflow")
    with pytest.raises(IntakeRejected) as excinfo:
        runner.enforce_rate_limits(
            session=session, upload=overflow, day_key="2026-06-02"
        )
    assert excinfo.value.status is PreviewStatus.RATE_LIMITED
    assert "device:" in excinfo.value.reason


def test_c13_per_ip_cap_enforced(config, counter_store, temp_upload_dir):
    runner = _runner(config, counter_store)
    sessions = [
        AnonymousSession(
            session_id_hash=f"sess_ip_{i}",
            created_at=_FROZEN_NOW,
            expires_at=_FROZEN_NOW + timedelta(hours=24),
            ip_hash="ip_hash_shared",
            device_cookie_hash=f"device_hash_{i}",
        )
        for i in range(config.rate_limit_per_ip_per_day + 1)
    ]
    # First N pass.
    for idx in range(config.rate_limit_per_ip_per_day):
        upload = _make_upload(
            temp_upload_dir, source_hash=f"src_hash_ip_{idx}"
        )
        runner.enforce_rate_limits(
            session=sessions[idx], upload=upload, day_key="2026-06-02"
        )
    overflow = _make_upload(temp_upload_dir, source_hash="src_hash_ip_overflow")
    with pytest.raises(IntakeRejected) as excinfo:
        runner.enforce_rate_limits(
            session=sessions[-1], upload=overflow, day_key="2026-06-02"
        )
    assert excinfo.value.status is PreviewStatus.RATE_LIMITED
    assert "ip:ip_hash_shared" in excinfo.value.reason


def test_c13_global_cap_enforced(temp_upload_dir, counter_store):
    # Use a tiny global cap so this test stays cheap.
    config = IntakeConfig(
        temp_upload_dir=temp_upload_dir,
        rate_limit_global_per_day=2,
        rate_limit_per_ip_per_day=10,
        rate_limit_per_device_per_day=10,
        rate_limit_per_source_hash_per_day=10,
    )
    runner = _runner(config, counter_store)
    for i in range(config.rate_limit_global_per_day):
        session = AnonymousSession(
            session_id_hash=f"sess_g_{i}",
            created_at=_FROZEN_NOW,
            expires_at=_FROZEN_NOW + timedelta(hours=24),
            ip_hash=f"ip_hash_g_{i}",
            device_cookie_hash=f"device_hash_g_{i}",
        )
        upload = _make_upload(temp_upload_dir, source_hash=f"src_hash_g_{i}")
        runner.enforce_rate_limits(
            session=session, upload=upload, day_key="2026-06-02"
        )
    overflow_session = AnonymousSession(
        session_id_hash="sess_g_overflow",
        created_at=_FROZEN_NOW,
        expires_at=_FROZEN_NOW + timedelta(hours=24),
        ip_hash="ip_hash_g_overflow",
        device_cookie_hash="device_hash_g_overflow",
    )
    overflow_upload = _make_upload(
        temp_upload_dir, source_hash="src_hash_g_overflow"
    )
    with pytest.raises(IntakeRejected) as excinfo:
        runner.enforce_rate_limits(
            session=overflow_session,
            upload=overflow_upload,
            day_key="2026-06-02",
        )
    assert excinfo.value.status is PreviewStatus.RATE_LIMITED
    assert "global:2026-06-02" in excinfo.value.reason


# C14 — counter store unavailable fails closed.
def test_c14_counter_store_missing_fails_closed(config, temp_upload_dir, session):
    runner = _runner(config, counter_store=None)
    upload = _make_upload(temp_upload_dir)
    with pytest.raises(IntakeRejected) as excinfo:
        runner.enforce_rate_limits(
            session=session, upload=upload, day_key="2026-06-02"
        )
    assert excinfo.value.status is PreviewStatus.FAILED


def test_c14_counter_store_unreadable_fails_closed(
    config, temp_upload_dir, session, tmp_path
):
    bad_path = tmp_path / "no_parent_here" / "counters.json"
    runner = _runner(config, CounterStore(bad_path))
    upload = _make_upload(temp_upload_dir)
    with pytest.raises(IntakeRejected) as excinfo:
        runner.enforce_rate_limits(
            session=session, upload=upload, day_key="2026-06-02"
        )
    assert excinfo.value.status is PreviewStatus.FAILED
    assert "rate-limit counter store" in excinfo.value.reason


def test_c14_counter_store_unreadable_when_file_corrupt_fails_closed(
    config, temp_upload_dir, session, tmp_path
):
    bad_file = tmp_path / "corrupt_counters.json"
    bad_file.write_text("{not-json", encoding="utf-8")
    runner = _runner(config, CounterStore(bad_file))
    upload = _make_upload(temp_upload_dir)
    with pytest.raises(IntakeRejected) as excinfo:
        runner.enforce_rate_limits(
            session=session, upload=upload, day_key="2026-06-02"
        )
    assert excinfo.value.status is PreviewStatus.FAILED


# C15 — probe failure prevents translation / TTS / clone.
def test_c15_probe_failure_blocks_translation_tts_clone(
    config, counter_store, temp_upload_dir, session
):
    def failing_probe(_upload: UploadIntake) -> ProbeResult:
        return ProbeResult(
            duration_seconds=0,
            source_hash="",
            media_type="",
            audio_present=False,
            audio_quality_score=0,
            teaser_candidate_range=(0, 0),
            failure_reason="ffprobe returned no streams",
        )

    runner = _runner(config, counter_store, probe_fn=failing_probe)
    upload = _make_upload(temp_upload_dir)
    with pytest.raises(IntakeRejected) as excinfo:
        runner.probe(upload)
    assert excinfo.value.status is PreviewStatus.FAILED
    # No expensive downstream call was recorded.
    assert runner.forbidden_calls == []


def test_c15_probe_exception_fails_closed(
    config, counter_store, temp_upload_dir
):
    def crashing_probe(_upload: UploadIntake) -> ProbeResult:
        raise RuntimeError("ffmpeg segfaulted")

    runner = _runner(config, counter_store, probe_fn=crashing_probe)
    upload = _make_upload(temp_upload_dir)
    with pytest.raises(IntakeRejected) as excinfo:
        runner.probe(upload)
    assert excinfo.value.status is PreviewStatus.FAILED
    assert "ffmpeg" in excinfo.value.reason


# C16 — anonymous needs_manual_review is treated as soft reject.
def test_c16_anonymous_needs_manual_review_is_soft_reject(
    config, counter_store, temp_upload_dir
):
    def manual_review_compliance(_probe: ProbeResult) -> ComplianceResult:
        return ComplianceResult(
            status=ComplianceStatus.NEEDS_MANUAL_REVIEW,
            reason="LLM unsure",
            audit_metadata={"layers": ("local_prefilter", "asr_teaser", "llm")},
            blocked_media_retained=False,
        )

    runner = _runner(config, counter_store, compliance_fn=manual_review_compliance)
    upload = _make_upload(temp_upload_dir)
    probe = runner.probe(upload)
    with pytest.raises(IntakeRejected) as excinfo:
        runner.evaluate_compliance(probe)
    assert excinfo.value.status is PreviewStatus.SOFT_REJECTED
    # Soft reject must not invoke any TTS / clone.
    assert runner.forbidden_calls == []


# C17 — compliance exception / timeout fails closed.
def test_c17_compliance_exception_fails_closed(
    config, counter_store, temp_upload_dir
):
    def crashing_compliance(_probe: ProbeResult) -> ComplianceResult:
        raise TimeoutError("LLM compliance timed out")

    runner = _runner(config, counter_store, compliance_fn=crashing_compliance)
    upload = _make_upload(temp_upload_dir)
    probe = runner.probe(upload)
    with pytest.raises(IntakeRejected) as excinfo:
        runner.evaluate_compliance(probe)
    assert excinfo.value.status is PreviewStatus.FAILED
    assert "compliance error" in excinfo.value.reason


# C18 — compliance audit metadata is retained (30d) and free of media bytes.
def test_c18_compliance_audit_retention_pinned():
    config = IntakeConfig()
    assert (
        config.compliance_audit_retention_seconds
        == DEFAULT_COMPLIANCE_AUDIT_RETENTION_SECONDS
        == 30 * 86400
    )


def test_c18_audit_metadata_preserved_no_media_bytes(
    config, counter_store, temp_upload_dir, session
):
    runner = _runner(config, counter_store)
    upload = _make_upload(temp_upload_dir)
    probe = runner.probe(upload)
    compliance = runner.evaluate_compliance(probe)
    record = runner.build_preview_record(
        attempt=_attempt(session, upload),
        probe_result=probe,
        compliance_result=compliance,
    )
    assert record.compliance_audit_metadata == {
        "layers": ("local_prefilter", "asr_teaser", "llm")
    }
    # Ensure no raw media bytes leaked into the audit metadata.
    for value in record.compliance_audit_metadata.values():
        assert not isinstance(value, (bytes, bytearray, memoryview))


# C19 — anonymous and Free YouTube source rejected at intake.
def test_c19_anonymous_youtube_rejected(config, counter_store, session):
    runner = _runner(config, counter_store)
    attempt = _attempt(
        session,
        upload=None,
        source_type=SourceType.YOUTUBE_URL,
        youtube_url="https://example.invalid/anything",
    )
    with pytest.raises(IntakeRejected) as excinfo:
        runner.admit_source(attempt)
    assert excinfo.value.status is PreviewStatus.REJECTED


def test_c19_free_youtube_rejected(config, counter_store, session):
    runner = _runner(config, counter_store)
    attempt = _attempt(
        session,
        upload=None,
        is_free_user=True,
        source_type=SourceType.YOUTUBE_URL,
        youtube_url="https://example.invalid/anything",
    )
    with pytest.raises(IntakeRejected) as excinfo:
        runner.admit_source(attempt)
    assert excinfo.value.status is PreviewStatus.REJECTED


# C22 — claim token field remains a placeholder; APF2 never fills it.
def test_c22_claim_token_remains_placeholder_only(
    config, counter_store, temp_upload_dir, session
):
    runner = _runner(config, counter_store)
    upload = _make_upload(temp_upload_dir)
    probe = runner.probe(upload)
    compliance = runner.evaluate_compliance(probe)
    record = runner.build_preview_record(
        attempt=_attempt(session, upload),
        probe_result=probe,
        compliance_result=compliance,
    )
    assert record.claim_token_placeholder is None


# C23 — rate-limit overflow escalates to login.
def test_c23_rate_limit_escalates_to_login(
    config, counter_store, temp_upload_dir, session
):
    runner = _runner(config, counter_store)
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_escalate")
    runner.enforce_rate_limits(session=session, upload=upload, day_key="2026-06-02")
    with pytest.raises(IntakeRejected):
        runner.enforce_rate_limits(
            session=session, upload=upload, day_key="2026-06-02"
        )
    assert session.escalated_to_login is True


# C24 — APF2 output is status-only; no preview media / clone calls happened.
def test_c24_apf2_output_is_status_only(
    config, counter_store, temp_upload_dir, session
):
    runner = _runner(config, counter_store)
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_status_only")
    runner.admit_source(_attempt(session, upload))
    runner.admit_upload(upload)
    runner.enforce_rate_limits(
        session=session, upload=upload, day_key="2026-06-02"
    )
    probe = runner.probe(upload)
    compliance = runner.evaluate_compliance(probe)
    record = runner.build_preview_record(
        attempt=_attempt(session, upload),
        probe_result=probe,
        compliance_result=compliance,
    )
    assert record.status is PreviewStatus.READY_FOR_MODE
    assert runner.forbidden_calls == []


# C25 — clone providers are not exercised anywhere in the contract.
def test_c25_clone_provider_never_called(
    config, counter_store, temp_upload_dir, session
):
    runner = _runner(config, counter_store)
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_no_clone")
    runner.admit_source(_attempt(session, upload))
    runner.admit_upload(upload)
    runner.enforce_rate_limits(
        session=session, upload=upload, day_key="2026-06-02"
    )
    probe = runner.probe(upload)
    compliance = runner.evaluate_compliance(probe)
    runner.build_preview_record(
        attempt=_attempt(session, upload),
        probe_result=probe,
        compliance_result=compliance,
    )
    assert runner.forbidden_calls == []


# Scaffolding guards — keep the test file self-contained and APF2-safe.


def test_scaffold_source_imports_only_stdlib_and_pytest():
    """The contract scaffold must not import from src/, gateway/ or frontend-next/.

    The guard walks the file's own AST and only inspects ``ast.Import`` /
    ``ast.ImportFrom`` nodes. Plain string literals — this docstring, the
    forbidden-roots set below, and any assertion message — cannot trigger
    a false positive, so a substring scan over the whole source is
    deliberately avoided.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden_roots = {"src", "gateway", "frontend", "frontend_next"}
    offenders: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in forbidden_roots:
                    offenders.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # Relative imports cannot reach the forbidden top-level
                # packages from a tests/ module — skip.
                continue
            module = node.module or ""
            root = module.split(".", 1)[0]
            if root in forbidden_roots:
                offenders.append(f"from {module} import ...")

    assert offenders == [], f"scaffold imports forbidden modules: {offenders}"


def test_scaffold_counter_store_round_trip(counter_path):
    store = CounterStore(counter_path)
    assert store.get("any") == 0
    assert store.increment("any") == 1
    assert store.increment("any") == 2
    assert store.get("any") == 2
