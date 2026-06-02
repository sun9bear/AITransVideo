"""APF2c-1 anonymous preview backend adapter — pure translation layer.

This module is the thin runtime translation layer between a future
upload handler / backend gateway and the pure contract module
``src.services.anonymous_preview_intake``.

Design source of truth:
``docs/plans/2026-06-02-apf2c-backend-adapter-boundary.md``.

The adapter is intentionally side-effect-free at the module level:

* it only imports the standard library and
  ``src.services.anonymous_preview_intake``;
* it does no filesystem reads or writes (no ``Path.exists()`` /
  ``Path.is_*()`` / ``open()`` / ``read_*()`` / ``write_*()`` /
  ``mkdir()`` / ``stat()`` / ``unlink()``); ``Path`` is forwarded as an
  opaque caller-provided value;
* it does no network calls, no subprocess invocations, no DB access,
  no provider API calls (ASR / LLM / TTS / clone / preview media), no
  pricing / payment / points logic;
* it does not read ``.env`` or any production secret;
* it does not instantiate a real counter store; rate-limit accounting
  is delegated to an injected protocol.

The adapter accepts caller-provided request + upload *facts* and
caller-injected dependencies (probe, compliance, counter store, clock,
hasher), packages them as the dataclasses defined in the intake
contract, and converts ``IntakeRejected`` into a status-only
``PreviewRecord``. It never raises ``IntakeRejected`` to its caller.

Future APF2c-2/3/4 phases are expected to replace the injected
dependencies with production implementations (real storage health
probe, real counter store, real probe / compliance providers) without
changing this adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Protocol, Sequence, Tuple

from src.services.anonymous_preview_intake import (
    DEFAULT_PREVIEW_RECORD_TTL_SECONDS,
    AnonymousSession,
    ComplianceResult,
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


# ---------------------------------------------------------------------------
# Caller-provided fact value objects.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestFacts:
    """Pure request-level facts a future upload handler already holds by
    the time it invokes the adapter.

    ``raw_session_id`` / ``raw_ip`` / ``raw_device_cookie`` must be
    hashed by the injected ``Hasher`` before they leave the adapter;
    they are never stored on the resulting ``PreviewRecord``.
    """

    raw_session_id: str
    raw_ip: str
    raw_device_cookie: str
    source_type: SourceType
    is_free_user: bool
    day_key: str
    youtube_url: Optional[str] = None


@dataclass(frozen=True)
class UploadFacts:
    """Pure upload-level facts produced by a future single-request upload
    handler after the upload completed.

    ``stored_path`` is an opaque ``Path`` value — the adapter forwards it
    to ``UploadIntake`` but never opens or inspects it.
    """

    file_name: str
    byte_length: int
    duration_seconds: float
    source_hash: str
    stored_path: Path
    is_chunked: bool = False


# ---------------------------------------------------------------------------
# Injected dependency protocols / callables.
# ---------------------------------------------------------------------------


class CounterStore(Protocol):
    """Rate-limit counter store protocol.

    Implementations may be backed by Redis, a JSON file, a DB, or an
    in-memory fake. The adapter only consumes ``get`` / ``increment`` and
    treats any raised exception as a fail-closed signal.
    """

    def get(self, key: str) -> int: ...

    def increment(self, key: str) -> int: ...


ProbeFn = Callable[[UploadFacts], ProbeResult]
ComplianceFn = Callable[[ProbeResult], ComplianceResult]
Hasher = Callable[[str, str], str]
ClockFn = Callable[[], datetime]


# ---------------------------------------------------------------------------
# Adapter.
# ---------------------------------------------------------------------------


def _default_record_id(source_hash: str) -> str:
    return f"prv_{source_hash[:12]}" if source_hash else "prv_rejected_no_upload"


@dataclass
class AnonymousPreviewBackendAdapter:
    """Thin translation layer between an upload handler and the pure
    intake contract module."""

    config: Optional[IntakeConfig]
    counter_store: Optional[CounterStore]
    probe_fn: ProbeFn
    compliance_fn: ComplianceFn
    hasher: Hasher
    now_fn: ClockFn

    # -- High-level entry point ---------------------------------------------

    def handle_intake(
        self,
        request: RequestFacts,
        upload: Optional[UploadFacts],
    ) -> PreviewRecord:
        """Translate request + upload facts into a status-only
        ``PreviewRecord``. ``IntakeRejected`` raised by any contract
        helper or injected dependency is always caught and rendered as a
        status-only record — never re-raised to the caller.
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
            self._enforce_rate_limits(
                config, session, intake, day_key=request.day_key
            )
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

    # -- Internal helpers ---------------------------------------------------

    def _build_session(
        self, config: IntakeConfig, request: RequestFacts
    ) -> AnonymousSession:
        return build_anonymous_session(
            config,
            session_id_hash=self.hasher("sess", request.raw_session_id),
            ip_hash=self.hasher("ip", request.raw_ip),
            device_cookie_hash=self.hasher("dev", request.raw_device_cookie),
            now=self.now_fn(),
        )

    def _build_upload_intake(self, upload: UploadFacts) -> UploadIntake:
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
        config: IntakeConfig,
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
        keys: Sequence[Tuple[str, int]] = (
            (f"global:{day_key}", config.rate_limit_global_per_day),
            (
                f"ip:{session.ip_hash}:{day_key}",
                config.rate_limit_per_ip_per_day,
            ),
            (
                f"device:{session.device_cookie_hash}:{day_key}",
                config.rate_limit_per_device_per_day,
            ),
            (
                f"source:{intake.source_hash}:{day_key}",
                config.rate_limit_per_source_hash_per_day,
            ),
        )
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
        except IntakeRejected:
            raise
        except Exception as exc:  # noqa: BLE001 — fail closed on any error
            raise fail_closed_from_exception("rate-limit", exc) from exc

    def _safe_probe(self, upload: UploadFacts) -> ProbeResult:
        try:
            return self.probe_fn(upload)
        except IntakeRejected:
            raise
        except Exception as exc:  # noqa: BLE001 — fail closed on any error
            raise fail_closed_from_exception("probe", exc) from exc

    def _safe_compliance(self, probe_result: ProbeResult) -> ComplianceResult:
        try:
            return self.compliance_fn(probe_result)
        except IntakeRejected:
            raise
        except Exception as exc:  # noqa: BLE001 — fail closed on any error
            raise fail_closed_from_exception("compliance", exc) from exc

    def _status_only_failure(
        self,
        request: RequestFacts,
        upload: Optional[UploadFacts],
        exc: IntakeRejected,
    ) -> PreviewRecord:
        now = self.now_fn()
        source_hash = upload.source_hash if upload is not None else ""
        session_id_hash = self.hasher("sess", request.raw_session_id)
        return PreviewRecord(
            record_id=_default_record_id(source_hash),
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


__all__ = [
    "RequestFacts",
    "UploadFacts",
    "CounterStore",
    "ProbeFn",
    "ComplianceFn",
    "Hasher",
    "ClockFn",
    "AnonymousPreviewBackendAdapter",
]
