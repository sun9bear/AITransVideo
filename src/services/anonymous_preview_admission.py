"""APF3a anonymous Free/Express preview admission — pure contract module.

This module is the production landing of the contract pinned in
``docs/plans/2026-06-04-apf3a-preview-contract.md`` and exercised by
``tests/test_apf3a_anonymous_preview_contract.py``. It decides whether an
anonymous request for a Free/Express/Smart/Studio mode may enter the
APF3a 3-minute watermarked preview lane, and what artifact policy the
downstream preview pipeline must enforce.

The module is intentionally side-effect-free:

* no filesystem reads/writes of any kind;
* no network calls;
* no subprocess invocations;
* no calls to ASR / LLM / TTS / voice-clone / preview media / object
  storage / DB / payment / points providers;
* no imports of ``gateway``, ``frontend-next``, ``src.pipeline``,
  ``src.modules``, ``src.services.tts*``, ``src.services.voice_clone``,
  ``src.services.content_compliance``, ``src.services.usage_meter`` or
  any real provider client.

What it provides:

* contract enums (``AnonymousPreviewMode`` / ``AdmissionDecision`` /
  ``VoiceStrategy``);
* immutable configuration dataclass
  (``AnonymousPreviewAdmissionConfig``);
* immutable artifact-policy dataclass (``AnonymousPreviewArtifactPolicy``);
* the status-only admission result (``AnonymousPreviewAdmission``);
* a single fail-closed entry point
  (``evaluate_anonymous_preview_admission``);
* an explicit boundary helper
  (``raise_clone_provider_boundary``) that always raises
  ``NotImplementedError`` so any downstream caller that mistakes the
  ``EXPRESS_TEMPORARY_CLONE_GATE`` strategy for "clone is wired" fails
  loudly instead of silently calling a paid provider.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from math import isfinite
from typing import Optional


# ---------------------------------------------------------------------------
# Typed constants.
# ---------------------------------------------------------------------------

# Anonymous preview duration cap, pinned by
# ``docs/plans/2026-06-01-anonymous-preview-funnel-ux-plan.md`` §10.
DEFAULT_MAX_PREVIEW_DURATION_SECONDS: float = 180.0

# Anonymous quota: Free 1 preview per day, Express 1 preview per day.
# These are pure contract constants — the actual counter store lives in
# ``src.services.anonymous_preview_rate_limit``.
DEFAULT_FREE_PREVIEW_QUOTA_PER_ANONYMOUS: int = 1
DEFAULT_EXPRESS_PREVIEW_QUOTA_PER_ANONYMOUS: int = 1

# Status reasons exposed on the admission result. They are stable,
# implementation-detail-free strings so they can be surfaced to the
# user (after i18n) without leaking internal state.
ADMITTED_REASON: str = "anonymous preview admission ok"
LOGIN_REQUIRED_REASON: str = (
    "smart mode requires registration / login / trial entitlement"
)
NOT_ANONYMOUS_FUNNEL_REASON: str = (
    "studio mode is not part of the anonymous preview funnel"
)
# Stable, input-free reason used for any unknown / non-string ``mode``
# argument. The raw value is intentionally NOT echoed back here because
# the caller may surface ``admission.reason`` to logs / status APIs /
# user-facing channels — echoing ``repr(mode)`` would leak tokens,
# filesystem paths, or arbitrary attacker-controlled fragments.
UNKNOWN_MODE_REASON: str = "unknown anonymous preview mode (fail closed)"


# ---------------------------------------------------------------------------
# Enums.
# ---------------------------------------------------------------------------


class AnonymousPreviewMode(str, Enum):
    FREE = "free"
    EXPRESS = "express"
    SMART = "smart"
    STUDIO = "studio"


class AdmissionDecision(str, Enum):
    ADMITTED = "admitted"
    LOGIN_REQUIRED = "login_required"
    NOT_ANONYMOUS_FUNNEL = "not_anonymous_funnel"
    REJECTED = "rejected"
    FAILED = "failed"


class VoiceStrategy(str, Enum):
    """Voice-resolution strategy a downstream preview pipeline must obey.

    ``PRESET_ONLY`` is the safe default and the only strategy this
    module currently emits for happy admissions. ``EXPRESS_TEMPORARY_CLONE_GATE``
    is only emitted when the admin flag
    ``anonymous_express_cosyvoice_clone_enabled`` is explicitly True; it
    is a *boundary marker*, not an instruction to call a clone provider
    — see :func:`raise_clone_provider_boundary`.
    """

    PRESET_ONLY = "preset_only"
    EXPRESS_TEMPORARY_CLONE_GATE = "express_temporary_clone_gate"


# Field names forbidden on a status-only ``AnonymousPreviewAdmission``.
# Listed here as a module-level invariant so callers (and tests) can
# re-use it. Mirrors ``FORBIDDEN_PREVIEW_RECORD_FIELDS`` in
# ``anonymous_preview_intake``.
FORBIDDEN_ADMISSION_FIELDS: frozenset[str] = frozenset(
    {
        "preview_artifact_key",
        "preview_url",
        "download_url",
        "subtitle_export_url",
        "jianying_draft_url",
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
class AnonymousPreviewAdmissionConfig:
    """Immutable configuration consumed by the admission helper.

    ``anonymous_express_cosyvoice_clone_enabled`` is **explicitly**
    ``False`` by default — APF3a ships clone-disabled. The boundary
    helper :func:`raise_clone_provider_boundary` guarantees that even
    if a future operator flips the flag, no real provider call can
    happen from this module.
    """

    max_preview_duration_seconds: float = DEFAULT_MAX_PREVIEW_DURATION_SECONDS
    anonymous_express_cosyvoice_clone_enabled: bool = False
    free_preview_quota_per_anonymous: int = (
        DEFAULT_FREE_PREVIEW_QUOTA_PER_ANONYMOUS
    )
    express_preview_quota_per_anonymous: int = (
        DEFAULT_EXPRESS_PREVIEW_QUOTA_PER_ANONYMOUS
    )


@dataclass(frozen=True)
class AnonymousPreviewArtifactPolicy:
    """Hard policy enforced on any APF3a preview artifact.

    All fields default to the most restrictive value so a caller that
    forgets to set them still produces a watermarked, stream-only,
    non-downloadable, non-exportable preview record. The four
    ``*_required`` / ``allow_editable_assets`` markers below pin the
    APF3a contract primitives (stream-only delivery, no editable
    assets, server-side TTL enforcement, low queue priority) so a
    downstream pipeline cannot mistake "not yet wired" for "freely
    allowed". Concrete TTL seconds / queue priority numbers are
    intentionally omitted — APF3a is a local/mock contract shell and
    must not commit to storage/queue magic numbers; the markers say
    only that the downstream phase MUST set them.
    """

    watermark_required: bool = True
    allow_download_url: bool = False
    allow_subtitle_export: bool = False
    allow_jianying_draft_export: bool = False
    allow_payment_fields: bool = False
    allow_provider_voice_id: bool = False
    allow_clone_artifact: bool = False
    stream_only_required: bool = True
    allow_editable_assets: bool = False
    artifact_ttl_required: bool = True
    low_priority_required: bool = True


@dataclass(frozen=True)
class AnonymousPreviewAdmission:
    """Status-only admission record. APF3a callers MUST NOT extend this
    with preview media / download / clone / pricing / payment fields. The
    class definition is the source of truth and
    :data:`FORBIDDEN_ADMISSION_FIELDS` documents the explicit deny list
    consumed by the contract tests.
    """

    mode: AnonymousPreviewMode
    decision: AdmissionDecision
    preview_duration_seconds: float
    voice_strategy: VoiceStrategy
    artifact_policy: AnonymousPreviewArtifactPolicy
    reason: str
    next_step_hint: Optional[str] = None


# ---------------------------------------------------------------------------
# Exceptions.
# ---------------------------------------------------------------------------


class AdmissionRejected(Exception):
    """Raised by ``_admit_*`` internal helpers — carries an
    :class:`AdmissionDecision` so the entry point can render a
    status-only :class:`AnonymousPreviewAdmission`."""

    def __init__(self, decision: AdmissionDecision, reason: str):
        super().__init__(f"{decision.value}: {reason}")
        self.decision = decision
        self.reason = reason


# ---------------------------------------------------------------------------
# Pure helpers.
# ---------------------------------------------------------------------------


def _coerce_mode(mode: object) -> AnonymousPreviewMode:
    if isinstance(mode, AnonymousPreviewMode):
        return mode
    if isinstance(mode, str):
        try:
            return AnonymousPreviewMode(mode)
        except ValueError as exc:
            raise AdmissionRejected(
                AdmissionDecision.REJECTED,
                UNKNOWN_MODE_REASON,
            ) from exc
    raise AdmissionRejected(
        AdmissionDecision.REJECTED,
        UNKNOWN_MODE_REASON,
    )


def _validate_duration(source_duration_seconds: object) -> float:
    # Python's ``bool`` is a subclass of ``int``, so ``isinstance(True,
    # (int, float))`` is True. APF3a low-trust request facts must reject
    # boolean inputs before any numeric conversion, otherwise ``True`` /
    # ``False`` would silently coerce to ``1.0`` / ``0.0`` and pass as a
    # valid duration. Fail closed without echoing the raw value.
    if isinstance(source_duration_seconds, bool):
        raise AdmissionRejected(
            AdmissionDecision.FAILED,
            "source_duration_seconds must be a number (fail closed)",
        )
    if not isinstance(source_duration_seconds, (int, float)):
        raise AdmissionRejected(
            AdmissionDecision.FAILED,
            "source_duration_seconds must be a number (fail closed)",
        )
    duration = float(source_duration_seconds)
    if not isfinite(duration):
        raise AdmissionRejected(
            AdmissionDecision.FAILED,
            "source_duration_seconds must be finite (fail closed)",
        )
    if duration < 0:
        raise AdmissionRejected(
            AdmissionDecision.FAILED,
            "source_duration_seconds must be non-negative (fail closed)",
        )
    return duration


def _validate_config(
    config: Optional[AnonymousPreviewAdmissionConfig],
) -> AnonymousPreviewAdmissionConfig:
    if config is None:
        raise AdmissionRejected(
            AdmissionDecision.FAILED,
            "AnonymousPreviewAdmissionConfig is missing (fail closed)",
        )
    # Python's ``bool`` is a subclass of ``int``, so without an explicit
    # guard ``max_preview_duration_seconds=True/False`` would silently
    # coerce to ``1.0`` / ``0.0`` and either cap every preview to 1
    # second or trip the ``<= 0`` branch with a confusing reason. Reject
    # boolean configuration values up front, fail closed, and do not echo
    # the raw value back to the caller.
    if isinstance(config.max_preview_duration_seconds, bool):
        raise AdmissionRejected(
            AdmissionDecision.FAILED,
            "config.max_preview_duration_seconds must be positive (fail closed)",
        )
    if (
        not isinstance(config.max_preview_duration_seconds, (int, float))
        or not isfinite(float(config.max_preview_duration_seconds))
        or float(config.max_preview_duration_seconds) <= 0
    ):
        raise AdmissionRejected(
            AdmissionDecision.FAILED,
            "config.max_preview_duration_seconds must be positive (fail closed)",
        )
    return config


def _cap_preview_duration(
    config: AnonymousPreviewAdmissionConfig, duration: float
) -> float:
    return min(duration, float(config.max_preview_duration_seconds))


def _resolve_voice_strategy(
    config: AnonymousPreviewAdmissionConfig,
    mode: AnonymousPreviewMode,
) -> VoiceStrategy:
    if mode is AnonymousPreviewMode.EXPRESS:
        if config.anonymous_express_cosyvoice_clone_enabled is True:
            return VoiceStrategy.EXPRESS_TEMPORARY_CLONE_GATE
        return VoiceStrategy.PRESET_ONLY
    return VoiceStrategy.PRESET_ONLY


def _default_artifact_policy() -> AnonymousPreviewArtifactPolicy:
    return AnonymousPreviewArtifactPolicy()


def raise_clone_provider_boundary(mode: AnonymousPreviewMode) -> None:
    """Boundary marker — always raises ``NotImplementedError``.

    APF3a ships without any real voice-clone provider wiring. Any code
    path that reaches the express clone gate (i.e. obtains
    :attr:`VoiceStrategy.EXPRESS_TEMPORARY_CLONE_GATE` from an
    ``AnonymousPreviewAdmission``) and then attempts to call a real
    provider must invoke this function first. It always raises
    ``NotImplementedError``, guaranteeing that:

    * no paid provider is contacted from this module's call graph;
    * any silent-fallback mistake at the integration boundary fails
      loudly during local tests instead of in production.
    """

    raise NotImplementedError(
        "anonymous Express CosyVoice temporary clone is gated at boundary; "
        f"no provider wiring exists in APF3a for mode={mode.value!r}"
    )


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


def evaluate_anonymous_preview_admission(
    *,
    config: Optional[AnonymousPreviewAdmissionConfig],
    mode: object,
    source_duration_seconds: object,
) -> AnonymousPreviewAdmission:
    """Decide whether an anonymous request may enter the APF3a preview
    lane and what artifact policy / voice strategy must be enforced.

    All branches return a status-only :class:`AnonymousPreviewAdmission`
    — exceptions raised by internal helpers are caught and rendered as
    rejection / failure records. The function never calls a provider,
    never touches the filesystem, never schedules a background job and
    never logs.
    """

    artifact_policy = _default_artifact_policy()
    # Track the successfully-coerced mode so the ``except`` fallback can
    # reuse it instead of re-running ``AnonymousPreviewMode(mode)`` on a
    # low-trust object (PR #23 external P2). Re-coercing an attacker-
    # controlled non-string value triggers the Enum value-comparison
    # path, which invokes ``__eq__`` / ``__hash__`` on the value and can
    # raise arbitrary exceptions out of the fail-closed branch.
    resolved_mode: Optional[AnonymousPreviewMode] = None
    try:
        cfg = _validate_config(config)
        resolved_mode = _coerce_mode(mode)
        duration = _validate_duration(source_duration_seconds)

        if resolved_mode is AnonymousPreviewMode.STUDIO:
            raise AdmissionRejected(
                AdmissionDecision.NOT_ANONYMOUS_FUNNEL,
                NOT_ANONYMOUS_FUNNEL_REASON,
            )
        if resolved_mode is AnonymousPreviewMode.SMART:
            raise AdmissionRejected(
                AdmissionDecision.LOGIN_REQUIRED,
                LOGIN_REQUIRED_REASON,
            )

        capped = _cap_preview_duration(cfg, duration)
        strategy = _resolve_voice_strategy(cfg, resolved_mode)
        return AnonymousPreviewAdmission(
            mode=resolved_mode,
            decision=AdmissionDecision.ADMITTED,
            preview_duration_seconds=capped,
            voice_strategy=strategy,
            artifact_policy=artifact_policy,
            reason=ADMITTED_REASON,
            next_step_hint=None,
        )
    except AdmissionRejected as exc:
        # NEVER call ``AnonymousPreviewMode(mode)`` on the raw input
        # here. Trust order, all safe (no ``__eq__`` / ``__hash__``
        # invocation on a low-trust value):
        #
        # 1. ``resolved_mode`` from inside the try (only set if
        #    ``_coerce_mode`` already accepted the input).
        # 2. ``mode`` if it is already an ``AnonymousPreviewMode``
        #    instance — ``isinstance`` walks the class hierarchy and
        #    does not dispatch through dunders on the value.
        # 3. Conservative ``AnonymousPreviewMode.FREE`` fallback.
        if resolved_mode is not None:
            fallback_mode = resolved_mode
        elif isinstance(mode, AnonymousPreviewMode):
            fallback_mode = mode
        else:
            fallback_mode = AnonymousPreviewMode.FREE
        return AnonymousPreviewAdmission(
            mode=fallback_mode,
            decision=exc.decision,
            preview_duration_seconds=0.0,
            voice_strategy=VoiceStrategy.PRESET_ONLY,
            artifact_policy=artifact_policy,
            reason=exc.reason,
            next_step_hint=_next_step_hint_for(exc.decision),
        )


def _next_step_hint_for(decision: AdmissionDecision) -> Optional[str]:
    if decision is AdmissionDecision.LOGIN_REQUIRED:
        return "login_required"
    if decision is AdmissionDecision.NOT_ANONYMOUS_FUNNEL:
        return "studio_requires_login_and_entitlement"
    if decision is AdmissionDecision.REJECTED:
        return "fix_input_and_retry"
    if decision is AdmissionDecision.FAILED:
        return "retry_or_contact_support"
    return None


__all__ = [
    # constants
    "DEFAULT_MAX_PREVIEW_DURATION_SECONDS",
    "DEFAULT_FREE_PREVIEW_QUOTA_PER_ANONYMOUS",
    "DEFAULT_EXPRESS_PREVIEW_QUOTA_PER_ANONYMOUS",
    "ADMITTED_REASON",
    "LOGIN_REQUIRED_REASON",
    "NOT_ANONYMOUS_FUNNEL_REASON",
    "UNKNOWN_MODE_REASON",
    "FORBIDDEN_ADMISSION_FIELDS",
    # enums
    "AnonymousPreviewMode",
    "AdmissionDecision",
    "VoiceStrategy",
    # data classes
    "AnonymousPreviewAdmissionConfig",
    "AnonymousPreviewArtifactPolicy",
    "AnonymousPreviewAdmission",
    # exception
    "AdmissionRejected",
    # helpers
    "evaluate_anonymous_preview_admission",
    "raise_clone_provider_boundary",
]
