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

import math
import secrets
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
    """Generate a collision-resistant record_id for the PK of
    ``anonymous_preview_records``.

    Format: ``prv_{source_hash[:8]}_{secrets.token_urlsafe(6)}``

    The 8-char hash prefix is kept for operator readability / log
    correlation.  The 6-byte (8-char URL-safe base64) random suffix
    makes the id unique per upload attempt, so two different anonymous
    sessions uploading the same video — same ``source_hash`` — produce
    distinct PKs and the second ``save_record`` never triggers a
    UNIQUE-constraint violation.

    When ``source_hash`` is empty (no upload reached the adapter, e.g.
    a YouTube-URL early rejection) the well-known sentinel
    ``prv_rejected_no_upload_{token}`` is used so the no-upload path
    also gets a unique ID (multiple rapid YouTube rejects from the same
    session were also colliding on the old fixed sentinel value).
    """
    if source_hash:
        return f"prv_{source_hash[:8]}_{secrets.token_urlsafe(6)}"
    return f"prv_rejected_no_upload_{secrets.token_urlsafe(6)}"


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
            # Normalize the wire-level ``source_type`` once at the adapter
            # boundary so every downstream write — ``admit_source``,
            # ``build_preview_record``, and the fail-closed status-only
            # record — sees the canonical ``SourceType`` enum rather than
            # the raw wire string. ``admit_source`` internally normalizes
            # too, but it discards the result, so without this step the
            # ``PreviewRecord.source_type`` field still carried the raw
            # ``str`` for wire callers (PR #22 external review P2,
            # discussion_r3345886349).
            normalized_source_type = self._normalize_source_type(
                request.source_type
            )
            admit_source(
                config,
                source_type=normalized_source_type,
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
            # APF2c R8v fix #2 (PR #22 external review P2,
            # discussion_r3347732874): a non-int / non-positive /
            # ``bool`` ``preview_record_ttl_seconds`` must fail closed
            # **before** any rate-limit counter mutation, probe call or
            # compliance call. Otherwise a misconfigured TTL leaks
            # downstream as a ``TypeError`` from ``timedelta(...)`` or
            # produces an immediately-expired record, after the
            # adapter has already paid the cost of rate-limit
            # accounting and the expensive probe / compliance
            # round-trips. The raw config value is intentionally NOT
            # interpolated into ``status_reason`` (persisted, low-trust
            # audit field).
            self._require_preview_record_ttl_seconds(config)
            self._enforce_rate_limits(
                config, session, intake, day_key=request.day_key
            )
            probe_result = self._safe_probe(upload)
            evaluate_probe_result(probe_result)
            # APF2c R8v fix #1 (PR #22 external review P2,
            # discussion_r3347732869): the injected probe may return a
            # ``ProbeResult`` whose ``source_hash`` no longer matches
            # the upload intake — typical causes are a stale cached
            # probe entry or a temp-path mix-up across concurrent
            # uploads. If we let such a probe through, the resulting
            # ``PreviewRecord`` would attribute the probe's duration /
            # audio / (post-compliance) decision to a *different*
            # upload. Fail closed before duration cap, compliance and
            # record write. The actual hash values are intentionally
            # NOT interpolated into ``status_reason`` (persisted,
            # low-trust audit field) — only the structural marker
            # ``probe source_hash mismatch`` + ``fail closed``.
            if probe_result.source_hash != intake.source_hash:
                raise IntakeRejected(
                    PreviewStatus.FAILED,
                    "probe source_hash mismatch (fail closed)",
                )
            # APF2c R8z fix #1 (PR #22 external review P2,
            # discussion_r3348103602): the injected probe may hand back a
            # ``duration_seconds`` that is ``float("nan")`` / ``inf`` /
            # ``-inf`` / ``0`` / negative / a non-numeric type. ``nan``
            # in particular silently passes the ``>`` cap comparison
            # below (``nan > x`` is always ``False``), letting an
            # invalid-duration probe advance to ``READY_FOR_MODE`` and
            # leak a meaningless duration into ``PreviewRecord``. Fail
            # closed **before** the cap comparison, the compliance call
            # and the record write. ``bool`` is rejected because it is
            # an ``int`` subclass but carrying ``True`` / ``False`` is a
            # probe-contract violation, not a legitimate duration. The
            # raw value is intentionally NOT interpolated into
            # ``status_reason`` (persisted, low-trust audit field) —
            # injected probes can embed provider payloads / tokens /
            # paths / raw media markers in unexpected scalar types.
            duration_value = probe_result.duration_seconds
            if (
                isinstance(duration_value, bool)
                or not isinstance(duration_value, (int, float))
                or not math.isfinite(duration_value)
                or duration_value <= 0
            ):
                raise IntakeRejected(
                    PreviewStatus.FAILED,
                    "probe duration invalid (fail closed)",
                )
            source_duration_value = getattr(
                probe_result, "source_duration_seconds", None
            )
            source_duration_reason = "probed source duration"
            if source_duration_value is None:
                source_duration_value = duration_value
                source_duration_reason = "probed duration"
            elif (
                isinstance(source_duration_value, bool)
                or not isinstance(source_duration_value, (int, float))
                or not math.isfinite(source_duration_value)
                or source_duration_value <= 0
            ):
                raise IntakeRejected(
                    PreviewStatus.FAILED,
                    "probe source duration invalid (fail closed)",
                )
            if source_duration_value > config.max_source_duration_seconds:
                raise IntakeRejected(
                    PreviewStatus.REJECTED,
                    f"{source_duration_reason} {source_duration_value} "
                    f"exceeds intake cap",
                )
            compliance_result = self._safe_compliance(probe_result)
            normalized_compliance = evaluate_compliance_result(compliance_result)
            # Generate a unique record_id here (adapter boundary) so that
            # the pure ``build_preview_record`` function stays side-effect
            # free while the PK is guaranteed collision-resistant across
            # concurrent uploads of the same file by different sessions.
            unique_record_id = _default_record_id(intake.source_hash)
            return build_preview_record(
                config,
                session=session,
                upload=intake,
                probe_result=probe_result,
                compliance_result=normalized_compliance,
                source_type=normalized_source_type,
                now=self.now_fn(),
                record_id=unique_record_id,
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

    @staticmethod
    def _normalize_source_type(value: object) -> SourceType:
        """Coerce a wire-level value into the canonical ``SourceType`` enum.

        Mirrors the recognition behaviour inside ``admit_source`` so the
        adapter can normalize *once* at its boundary and thread the enum
        through every downstream write (``admit_source`` call,
        ``build_preview_record`` argument, fail-closed status-only record).
        Without this normalization a raw HTTP/wire ``str`` such as
        ``"local_upload"`` would land on ``PreviewRecord.source_type`` —
        a typed ``SourceType`` field — and downstream identity checks
        could silently misroute (PR #22 external review P2).

        Raises ``IntakeRejected`` with ``PreviewStatus.FAILED`` when the
        value is not a recognized ``SourceType`` (matching ``admit_source``).
        The raw wire value is intentionally **not** embedded in the
        rejection reason — wire callers may pass attacker-controlled
        strings containing tokens, secrets, provider payloads, raw media
        markers, or filesystem paths, and ``status_reason`` is a
        persisted low-trust audit field. The status-only ``PreviewRecord``
        built by ``_status_only_failure`` falls back to
        ``SourceType.LOCAL_UPLOAD`` so the typed contract is preserved
        without surfacing the unrecognized payload (PR #22 external
        review P2 follow-up, R8l).
        """

        if isinstance(value, SourceType):
            return value
        try:
            return SourceType(value)
        except (TypeError, ValueError) as exc:
            raise IntakeRejected(
                PreviewStatus.FAILED,
                "source_type is not a recognized SourceType (fail closed)",
            ) from exc

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
                    # Thread the login-escalation hint through the
                    # exception so the status-only ``PreviewRecord``
                    # rendered by ``_status_only_failure`` carries the
                    # signal — the caller must not have to inspect the
                    # transient ``AnonymousSession`` to decide whether
                    # to suggest login (PR #22 external review P2,
                    # APF2 C23).
                    raise IntakeRejected(
                        PreviewStatus.RATE_LIMITED,
                        f"rate limit exceeded for {key}",
                        login_escalation_hint=(
                            config.escalate_to_login_after_rate_limit
                        ),
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
        """Return a usable ``datetime`` for the status-only failure path.

        Falls back to the conservative ``_FALLBACK_NOW`` epoch when the
        injected ``now_fn`` raises **or** when it returns a value that
        is not a ``datetime`` instance. APF2c R8zg fix #2 (PR #22
        external review P2, discussion_r3348801408): without the type
        check, a misbehaving clock that returns e.g. a string / number
        / ``None`` would silently pass through here and explode in
        ``_status_only_failure`` at
        ``now + timedelta(seconds=ttl_seconds)`` with a ``TypeError``
        that escapes ``handle_intake`` — breaking the
        "always return a status-only failed record" promise.
        """

        try:
            result = self.now_fn()
        except Exception:  # noqa: BLE001 — fail closed on clock error
            return _FALLBACK_NOW
        if not isinstance(result, datetime):
            return _FALLBACK_NOW
        return result

    def _safe_hash(self, prefix: str, value: str) -> str:
        try:
            return self.hasher(prefix, value)
        except Exception:  # noqa: BLE001 — fail closed on hasher error
            return f"{prefix}_unavailable"

    @staticmethod
    def _require_preview_record_ttl_seconds(config: IntakeConfig) -> int:
        """Fail closed when ``config.preview_record_ttl_seconds`` is not a
        positive ``int``.

        Called from the success path **before** rate-limit accounting,
        probe and compliance so a misconfigured TTL never advances to
        counter mutation or expensive provider calls. ``bool`` is
        rejected explicitly because it is a subclass of ``int`` but
        carrying ``True`` / ``False`` is a configuration bug, not a
        legitimate TTL value.

        The raw config value is intentionally NOT interpolated into
        ``status_reason`` — that string lands on
        ``PreviewRecord.status_reason``, a persisted, low-trust audit
        field, and even configuration values can carry surprising
        operator strings. Mirrors the R7b / R8l / R8o redaction style.

        ``self.config is None`` is handled earlier by
        ``require_config(self.config)`` and intentionally not duplicated
        here; the per-test contract for missing config is
        ``"IntakeConfig is missing (fail closed)"``.
        """

        candidate = getattr(config, "preview_record_ttl_seconds", None)
        if (
            isinstance(candidate, bool)
            or not isinstance(candidate, int)
            or candidate <= 0
        ):
            raise IntakeRejected(
                PreviewStatus.FAILED,
                "preview_record_ttl_seconds is invalid (fail closed)",
            )
        return candidate

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
        # Status-only record must also carry the canonical ``SourceType``
        # enum, not a raw wire string. When the original value is itself
        # unrecognized (the unrecognized-source FAILED branch), fall back
        # to ``LOCAL_UPLOAD`` — the same default ``build_preview_record``
        # uses — so the persisted record keeps the typed contract intact
        # even on the failure path (PR #22 external review P2,
        # discussion_r3345886349).
        try:
            canonical_source_type = self._normalize_source_type(
                request.source_type
            )
        except IntakeRejected:
            canonical_source_type = SourceType.LOCAL_UPLOAD
        return PreviewRecord(
            record_id=_default_record_id(source_hash),
            session_id_hash=session_id_hash,
            source_hash=source_hash,
            upload_hash=source_hash,
            source_type=canonical_source_type,
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
            login_escalation_hint=getattr(
                exc, "login_escalation_hint", None
            ),
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
