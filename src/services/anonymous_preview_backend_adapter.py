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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional, Protocol, Sequence, Tuple

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
    in-memory fake. The adapter consumes the atomic ``try_acquire`` method
    on the admission path so a single check-and-increment cannot race
    between concurrent callers. ``get`` / ``increment`` remain on the
    protocol for diagnostics and best-effort rollback; the adapter treats
    any raised exception as a fail-closed signal.

    ``try_acquire(key, cap)`` MUST atomically:

    * return ``(False, current)`` without mutating state when
      ``current >= cap``;
    * otherwise increment the counter and return
      ``(True, new_value)`` where ``new_value == current + 1``.

    Optional ``decrement(key)`` is consulted by the adapter for best-effort
    multi-key rollback when a later key denies after earlier keys were
    admitted.
    """

    def get(self, key: str) -> int: ...

    def increment(self, key: str) -> int: ...

    def try_acquire(self, key: str, cap: int) -> Tuple[bool, int]: ...


ProbeFn = Callable[[UploadFacts], ProbeResult]
ComplianceFn = Callable[[ProbeResult], ComplianceResult]
Hasher = Callable[[str, str], str]
ClockFn = Callable[[], datetime]


# ---------------------------------------------------------------------------
# Adapter.
# ---------------------------------------------------------------------------


def _default_record_id(source_hash: str) -> str:
    return f"prv_{source_hash[:12]}" if source_hash else "prv_rejected_no_upload"


_FALLBACK_NOW = datetime(1970, 1, 1, tzinfo=timezone.utc)


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
            if probe_result.duration_seconds > config.max_source_duration_seconds:
                raise IntakeRejected(
                    PreviewStatus.REJECTED,
                    f"probed duration {probe_result.duration_seconds} "
                    f"exceeds intake cap",
                )
            compliance_result = self._safe_compliance(probe_result)
            normalized_compliance = evaluate_compliance_result(compliance_result)
            return build_preview_record(
                config,
                session=session,
                upload=intake,
                probe_result=probe_result,
                compliance_result=normalized_compliance,
                source_type=request.source_type,
                now=self.now_fn(),
            )
        except IntakeRejected as exc:
            return self._status_only_failure(request, upload, exc)
        except Exception as exc:  # noqa: BLE001 — adapter-owned dependency/config/runtime errors must fail closed
            # Translate any unexpected adapter-owned dependency / config /
            # runtime exception into a status-only ``FAILED`` record. We
            # only surface the exception **type name** — never ``str(exc)``
            # or ``repr(exc)`` — because injected dependencies may embed
            # raw secrets, tokens, provider payloads, file paths, or raw
            # media bytes in their exception messages, and ``status_reason``
            # is a persisted, low-trust audit field. The type name alone
            # gives audit traces a stable, low-sensitivity failure source.
            failure = IntakeRejected(
                PreviewStatus.FAILED,
                f"adapter error (fail closed): dependency {type(exc).__name__}",
            )
            return self._status_only_failure(request, upload, failure)

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
        """Atomic rate-limit admission.

        Each key is checked-and-incremented via the store's ``try_acquire``
        method so a single call cannot race between the read and the
        write. If a later key denies after earlier keys were admitted, the
        earlier admissions are rolled back best-effort via the optional
        ``decrement`` method so a denied request never leaves counters
        over-claimed. Stores that do not implement ``try_acquire`` raise
        ``AttributeError`` here — caught and translated to a fail-closed
        ``FAILED`` record so non-atomic shared-store behavior is never
        silently allowed.
        """

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
        admitted: List[str] = []
        try:
            for key, cap in keys:
                ok, _count = self.counter_store.try_acquire(key, cap)
                if not ok:
                    self._rollback_admitted(admitted)
                    session.escalated_to_login = (
                        config.escalate_to_login_after_rate_limit
                    )
                    raise IntakeRejected(
                        PreviewStatus.RATE_LIMITED,
                        f"rate limit exceeded for {key}",
                    )
                admitted.append(key)
        except IntakeRejected:
            raise
        except Exception as exc:  # noqa: BLE001 — fail closed on any error
            self._rollback_admitted(admitted)
            raise fail_closed_from_exception("rate-limit", exc) from exc

    def _rollback_admitted(self, admitted: Sequence[str]) -> None:
        """Best-effort rollback of previously admitted keys.

        Looks up ``decrement`` on the counter store and calls it for each
        admitted key. Any exception raised by ``decrement`` is swallowed:
        the alternative is to surface a confusing secondary failure on
        what is already a denied admission. Stores that do not provide a
        ``decrement`` skip rollback — a deliberate reservation/decrement
        is the documented strategy, callers that need stricter semantics
        must implement ``decrement``.
        """

        store = self.counter_store
        if store is None:
            return
        decrement = getattr(store, "decrement", None)
        if decrement is None:
            return
        for key in admitted:
            try:
                decrement(key)
            except Exception:  # noqa: BLE001 — rollback is best-effort
                pass

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

    def _safe_now(self) -> datetime:
        try:
            return self.now_fn()
        except Exception:  # noqa: BLE001 — fail closed on clock error
            return _FALLBACK_NOW

    def _safe_hash(self, prefix: str, value: str) -> str:
        try:
            return self.hasher(prefix, value)
        except Exception:  # noqa: BLE001 — fail closed on hasher error
            return f"{prefix}_unavailable"

    def _safe_preview_record_ttl_seconds(self) -> int:
        """Return a validated positive ``int`` TTL, falling back to the
        pinned ``DEFAULT_PREVIEW_RECORD_TTL_SECONDS`` whenever
        ``self.config`` is missing or its ``preview_record_ttl_seconds``
        field is missing / ``None`` / non-int / non-positive / ``bool``.

        Prevents ``TypeError`` from ``timedelta(seconds=...)`` during
        the fail-closed status-only path when a misconfigured caller
        sets ``preview_record_ttl_seconds`` to ``None`` or to a value of
        a type the standard library refuses to accept. ``bool`` is
        rejected explicitly because it is a subclass of ``int`` but
        carrying ``True`` / ``False`` as a TTL is a configuration bug,
        not a legitimate value.
        """

        config = self.config
        if config is None:
            return DEFAULT_PREVIEW_RECORD_TTL_SECONDS
        candidate = getattr(config, "preview_record_ttl_seconds", None)
        if isinstance(candidate, bool):
            return DEFAULT_PREVIEW_RECORD_TTL_SECONDS
        if not isinstance(candidate, int) or candidate <= 0:
            return DEFAULT_PREVIEW_RECORD_TTL_SECONDS
        return candidate

    def _status_only_failure(
        self,
        request: RequestFacts,
        upload: Optional[UploadFacts],
        exc: IntakeRejected,
    ) -> PreviewRecord:
        now = self._safe_now()
        source_hash = upload.source_hash if upload is not None else ""
        try:
            raw_session_id = request.raw_session_id
        except Exception:  # noqa: BLE001
            raw_session_id = ""
        session_id_hash = self._safe_hash("sess", raw_session_id)
        ttl_seconds = self._safe_preview_record_ttl_seconds()
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
            expires_at=now + timedelta(seconds=ttl_seconds),
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
