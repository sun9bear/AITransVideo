"""Smart MVP §6.2.1 — auto voice review (PR#3B).

Pure-orchestration module: per main speaker decide clone vs preset, route
clone calls through the ``CloneProvider`` Protocol (NEVER through the
gateway /voice-clone HTTP endpoint per plan §5.2 + Codex F4), and return
a per-speaker decision list the PR#3C integration layer can apply to the
voice_selection_review payload.

Inputs (PR#3C integration responsibility — see ``VoiceReviewSpeakerInput``):
  - main_speakers: list of speakers that passed eligibility gate (the
    integration layer pre-filters via ``evaluate_eligibility`` and pre-
    builds the concatenated speaker audio sample at the path it points to)
  - smart_consent: Smart job's consent payload (plan §4.2 6 fields)
  - clone_provider: injected via ``services.smart_wiring.build_smart_clone_provider``
    or ``inject_for_test(...)``
  - voice_library_quota_remaining: from Gateway / MiniMax account check
    BEFORE entering this module — it's a snapshot, the module decrements
    it locally as it issues clones to model the safety water mark per
    plan §7.3

Outputs (caller applies; this module doesn't write anywhere):
  - ``VoiceReviewResult.outcome`` ∈ {AUTO_APPROVED, PAUSED}
  - ``VoiceReviewResult.decisions`` — per-speaker CLONED / PRESET / PAUSED
    with ``cloned_voice_id`` populated for CLONED, ``reason_code`` for
    every entry. PRESET decisions don't carry the actual preset voice_id
    — that's the voice_match_resolver's job (lives in
    ``src/services/tts/`` which is import-forbidden for the smart
    package per §8.2 #1; PR#3C integration layer calls it).

Decision rules per plan §6.2.1 + §7.3 + Phase 3 (2026-05-17 user-voice-
candidate-first §Consent × Admin 决策矩阵):
  0. Existing strong personal-voice match for the speaker → REUSED with
     reason ``reused_user_voice``. Reuse fires regardless of consent OR
     ``admin_clone_enabled`` — reuse doesn't call provider, doesn't
     consume clone quota, and doesn't burn account stock, so neither
     gate applies (plan §核心不变量).
  1. ``smart_consent.auto_voice_clone is False`` OR
     ``admin_clone_enabled is False`` (no existing match) → PRESET
     with one of three reason_codes (plan §审计 reason_codes):
       - ``new_clone_blocked_by_consent`` — consent denied only
       - ``new_clone_blocked_by_admin`` — admin disabled only
       - ``new_clone_blocked_by_consent_and_admin`` — both gates closed
     Defensive — Gateway create gate should reject consent denials, but
     module fails closed if anyone bypasses it. Provider is NEVER called
     when either gate is closed.
  2. ``sample_seconds < 10.0`` → PRESET with reason
     ``insufficient_sample_seconds_lt_10``. Per Codex F5: 8-10s samples
     would be 400-rejected by the existing voice-clone HTTP endpoint
     anyway, so we don't even try (matches plan §6.2.1 hard floor).
  3. ``voice_library_quota_remaining <= quota_safety_water_mark`` (plan §7.3
     N=3) → PAUSED for THIS speaker AND all subsequent speakers. The
     integration layer treats the whole VoiceReviewResult as PAUSED and
     emits a smart_state marker so the user can decide whether to retry
     later.
  4. consent OK + admin OK + sample OK + quota OK → invoke
     ``clone_provider.clone_voice`` up to ``max_clone_attempts_per_speaker``
     (plan §7.3). On success → CLONED with the returned voice_id. On
     exhausted retries → PRESET with reason ``provider_failure_max_retries_<N>``.
     On quota error mid-flight → switch to PAUSED for remaining speakers.

This module is pure orchestration:
  - No I/O (sidecar emit happens via the integration layer calling
    ``services.smart.sidecar_emitter.emit_smart_decision`` for each
    decision — separation of concerns)
  - No write to review_state (that's PR#3C)
  - No real provider import (CloneProvider protocol only — AST guard
    in tests/test_smart_skeleton_protocol_guards.py enforces)

Acceptance tests in tests/test_smart_auto_voice_review.py.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from services.smart.contracts import CloneProvider, CloneResult


logger = logging.getLogger(__name__)


# Plan §6.2.1 + Codex F5 — sample minimum aligned with the real
# voice_selection_api.py:358 endpoint floor (which would 400-reject
# anything shorter). Hard-coded; not a tunable.
MIN_SAMPLE_SECONDS = 10.0

# Plan §7.3 — voice library quota safety water mark. When remaining
# quota drops to or below this, do NOT issue any more clone calls for
# this task (pause, let the integration layer surface "稍后重试").
DEFAULT_QUOTA_SAFETY_WATER_MARK = 3

# Plan §7.3 — per-speaker clone failure budget. Hard exit to preset
# fallback after this many failed attempts on the same speaker.
DEFAULT_MAX_CLONE_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class VoiceReviewChoice(Enum):
    """Per-speaker choice. Drives the integration layer's next move."""

    CLONED = "cloned"      # voice_id from provider; integration applies directly
    REUSED = "reused_user_voice"  # existing personal voice_id; no provider call
    PRESET = "preset"      # voice_id=None; integration calls voice_match_resolver
    PAUSED = "paused"      # quota water mark hit; integration pauses task


class VoiceReviewOutcome(Enum):
    """Top-level result outcome — what the integration layer should do
    after applying the per-speaker decisions."""

    AUTO_APPROVED = "auto_approved"  # all speakers got CLONED or PRESET
    PAUSED = "paused"                # at least one PAUSED → caller pauses task


@dataclass(frozen=True)
class VoiceReviewSpeakerInput:
    """Per-main-speaker input to the auto voice review module.

    Built by the PR#3C integration layer from S2 result + speaker
    structure profile + pre-built ffmpeg-concatenated audio sample.
    """

    speaker_id: str
    speaker_name: str
    sample_seconds: float
    source_audio_path: Path  # caller's responsibility to pre-build


@dataclass(frozen=True)
class VoiceReviewExistingMatch:
    """Existing personal voice candidate supplied by the integration layer."""

    voice_id: str
    provider_name: str | None = None
    model_name: str | None = None
    confidence: str | None = None
    reason: str | None = None
    user_voice_id: str | None = None


@dataclass(frozen=True)
class VoiceReviewDecision:
    """Per-speaker outcome.

    For CLONED: ``cloned_voice_id`` is the voice_id returned by the
    provider (e.g. ``vt_speaker_a_<timestamp>`` for MiniMax).

    For PRESET: ``cloned_voice_id`` is None — the integration layer
    calls ``voice_match_resolver`` to pick the preset voice. We don't
    do that here because voice_match_resolver lives in
    ``src/services/tts/`` which is import-forbidden for the smart
    package (§8.2 #1 AST guard).

    For PAUSED: same as PRESET (no voice_id) but ``reason_code``
    indicates the pause cause.
    """

    speaker_id: str
    speaker_name: str
    choice: VoiceReviewChoice
    cloned_voice_id: str | None
    cloned_provider_name: str | None
    cloned_model_name: str | None
    reason_code: str
    smart_decision_id: str
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VoiceReviewResult:
    """Top-level return shape — caller acts on ``outcome`` and applies
    each ``decisions`` entry to the voice_selection_review payload."""

    outcome: VoiceReviewOutcome
    decisions: tuple[VoiceReviewDecision, ...]
    pause_reason: str | None  # populated only when outcome is PAUSED


# ---------------------------------------------------------------------------
# evaluate_voice_review — main entry point
# ---------------------------------------------------------------------------


def evaluate_voice_review(
    *,
    main_speakers: Sequence[VoiceReviewSpeakerInput],
    smart_consent: Mapping[str, Any],
    clone_provider: CloneProvider,
    voice_library_quota_remaining: int,
    smart_decision_id_factory: Callable[[], str],
    existing_voice_matches_by_speaker_id: Mapping[str, VoiceReviewExistingMatch] | None = None,
    possible_voice_matches_by_speaker_id: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    quota_safety_water_mark: int = DEFAULT_QUOTA_SAFETY_WATER_MARK,
    min_sample_seconds: float = MIN_SAMPLE_SECONDS,
    max_clone_attempts_per_speaker: int = DEFAULT_MAX_CLONE_ATTEMPTS,
    admin_clone_enabled: bool = True,
    admin_pause_on_possible_match: bool = False,
) -> VoiceReviewResult:
    """Orchestrate the per-main-speaker auto voice decision.

    See module docstring for input/output contract and decision rules.

    Args:
      main_speakers: from ``evaluate_eligibility`` filtered + augmented
        with sample_seconds + source_audio_path
      smart_consent: parsed smart_consent payload (plan §4.2)
      clone_provider: injected CloneProvider Protocol implementation
      voice_library_quota_remaining: snapshot from Gateway / MiniMax
        account check at task entry; module decrements locally as it
        issues clones (the snapshot may go stale if other tasks burn
        quota concurrently — that's not a Smart MVP concern)
      smart_decision_id_factory: callable producing fresh decision IDs
        (e.g. ``uuid4().hex``); injected so tests can pin IDs
      admin_clone_enabled: Phase 3 (plan 2026-05-17-user-voice-candidate-
        first §后台策略字段) admin policy switch. When False, no new clone
        is issued — speakers without an existing strong match fall to
        PRESET. Defaults to True to preserve legacy 1-axis behavior for
        callers that don't pass the kwarg. Strong-match REUSED decisions
        are unaffected (plan §核心不变量).
      possible_voice_matches_by_speaker_id: Phase 4 (plan 2026-05-17 §Smart
        弱匹配暂停 + §推荐决策顺序 step 3) — per-speaker list of
        non-strong personal-voice candidate dicts (medium / weak /
        cross-source-named). Each dict should carry at least
        ``voice_id`` / ``label`` / ``match_scope`` / ``confidence`` —
        used for the audit metrics on pause decisions. Defaults to
        empty so Phase 3 callers see no behavior change.
      admin_pause_on_possible_match: Phase 4 admin policy switch. When
        True AND a speaker has any entry in
        ``possible_voice_matches_by_speaker_id``, that speaker (and all
        subsequent ones via propagation) pauses with reason_code
        ``possible_user_voice_match_requires_confirmation`` so the user
        can confirm reuse vs new clone. Defaults to False so existing
        Smart users don't get surprise pauses (matches admin_settings
        default ``smart_pause_on_possible_user_voice_match=False``).
        Strong-match REUSED decisions still run BEFORE this check —
        the pause only fires for speakers without a strong match.

    Returns:
      VoiceReviewResult capturing top-level outcome + per-speaker
      decisions. The integration layer is responsible for:
        - Calling sidecar_emitter.emit_smart_decision for each decision
        - Calling voice_match_resolver to pick voice_id for PRESET decisions
        - Applying the result to voice_selection_review.payload
        - Pausing the task / emitting handoff markers if outcome is PAUSED

    Raises: never. All provider exceptions caught + recorded as
    decisions per plan §6.4 sidecar discipline (failures must not
    block the user-facing pipeline).
    """
    # Codex 第十三轮 P1: strict identity check, NOT bool() coercion.
    # bool("false") / bool("0") / bool(1) all evaluate truthy and would
    # let a stringly-typed upstream payload bypass the consent guard
    # and burn paid clone API. Only exact ``True`` (the Pydantic-validated
    # bool that Gateway should deliver) is allowed through. Anything
    # else — None, missing, "true" string, 1 int, etc. — falls to PRESET.
    consent_allows_clone = smart_consent.get("auto_voice_clone") is True
    # Phase 3 (plan 2026-05-17 §Consent × Admin 决策矩阵): admin policy
    # is the second independent axis. Default True to keep legacy 1-axis
    # callers working unchanged. Strict bool() coercion is fine here
    # because the call site (process.py) reads from a Pydantic-validated
    # AdminSettings.smart_auto_clone_enabled (bool), not a stringly-typed
    # payload — but we still pin the type defensively.
    admin_allows_new_clone = bool(admin_clone_enabled)
    # Pre-compute the new-clone-blocked reason_code so the loop below
    # doesn't repeat the branch. Three reasons distinguish which gate
    # closed (plan §审计 reason_codes).
    new_clone_blocked_reason = _new_clone_blocked_reason(
        consent_allows_clone=consent_allows_clone,
        admin_allows_new_clone=admin_allows_new_clone,
    )
    decisions: list[VoiceReviewDecision] = []
    quota_remaining = voice_library_quota_remaining
    paused_after_speaker: bool = False
    pause_reason: str | None = None
    # Track what kind of pause first fired so the propagation cascade
    # can use a reason_code that links each downstream speaker to the
    # triggering cause (mirrors plan §Phase 4 — distinct propagation
    # reason for possible-match pause vs the original quota pause).
    pause_propagation_reason: str = "paused_after_prior_quota_exhaust"
    existing_voice_matches = dict(existing_voice_matches_by_speaker_id or {})
    # Phase 4 (plan 2026-05-17 §推荐决策顺序 step 3) — normalize the
    # possible-candidates map: copy + skip falsy entries so an upstream
    # ``{"a": []}`` doesn't masquerade as "speaker_a has candidates".
    possible_voice_matches: dict[str, list[Mapping[str, Any]]] = {}
    if possible_voice_matches_by_speaker_id:
        for _spk_id, _candidates in possible_voice_matches_by_speaker_id.items():
            if not _candidates:
                continue
            _filtered = [c for c in _candidates if c]
            if _filtered:
                possible_voice_matches[_spk_id] = _filtered

    for speaker in main_speakers:
        # Once we hit a hard pause condition, all remaining speakers
        # get PAUSED too — don't keep trying to clone after a quota
        # exhaustion in mid-flight. Use a distinct propagation reason
        # so the sidecar can differentiate "this speaker triggered the
        # pause" from "this speaker was caught in the propagation".
        # The top-level VoiceReviewResult.pause_reason still carries
        # the original triggering cause for the integration layer.
        if paused_after_speaker:
            decisions.append(_paused_decision(
                speaker, pause_propagation_reason,
                smart_decision_id_factory()
            ))
            continue

        # Strong-match reuse runs BEFORE the new-clone gates. Plan
        # §核心不变量: reuse doesn't consume clone quota, doesn't call
        # provider, doesn't burn account stock — so neither consent nor
        # admin_clone_enabled apply. A speaker with an existing strong
        # match reuses regardless of consent / admin policy.
        existing_match = existing_voice_matches.get(speaker.speaker_id)
        if existing_match is not None and str(existing_match.voice_id or "").strip():
            decisions.append(_reused_decision(
                speaker,
                existing_match,
                smart_decision_id_factory(),
            ))
            continue

        # Phase 4 (plan 2026-05-17 §推荐决策顺序 step 3) — possible-match
        # pause runs AFTER strong-match REUSED but BEFORE the new-clone
        # gates and any provider call. When admin enables
        # smart_pause_on_possible_user_voice_match AND this speaker has
        # any non-strong candidate (medium / weak / cross-source-named),
        # pause the WHOLE job to voice review so the user can confirm
        # reuse vs new clone. Cascade to subsequent speakers via the
        # same propagation mechanism as the quota water mark.
        #
        # Critical invariants:
        #   - Strong REUSED still wins (the ``existing_match`` block
        #     above ``continue``s before we reach here).
        #   - Provider is NEVER called on this path — pause means the
        #     user decides. CLAUDE.md §付费 API 不能自动调用 satisfied
        #     by skipping the clone attempt entirely.
        #   - admin_pause_on_possible_match defaults False so existing
        #     Smart users see no behavior change.
        if admin_pause_on_possible_match:
            speaker_possible = possible_voice_matches.get(speaker.speaker_id)
            if speaker_possible:
                top_candidate = speaker_possible[0] or {}
                pause_metrics: dict[str, Any] = {
                    "possible_match_count": len(speaker_possible),
                    "top_candidate_voice_id": top_candidate.get("voice_id"),
                    "top_candidate_label": top_candidate.get("label"),
                    "top_candidate_match_scope": top_candidate.get("match_scope"),
                    "top_candidate_confidence": top_candidate.get("confidence"),
                }
                paused_after_speaker = True
                pause_reason = (
                    "possible_user_voice_match_requires_confirmation"
                )
                # Distinct propagation reason so audit can tell cascade
                # entries from the triggering speaker (parallels the
                # quota-exhaust propagation pattern below).
                pause_propagation_reason = (
                    "paused_after_prior_possible_match_confirmation"
                )
                decisions.append(_paused_decision(
                    speaker,
                    pause_reason,
                    smart_decision_id_factory(),
                    metrics=pause_metrics,
                ))
                continue

        # Rule 1 (Phase 3): new clone blocked by consent and/or admin.
        # No existing match means we'd need a new clone, which is what
        # either gate blocks. Provider is NEVER called here.
        if new_clone_blocked_reason is not None:
            decisions.append(_preset_decision(
                speaker, new_clone_blocked_reason, smart_decision_id_factory()
            ))
            continue

        # Rule 2: sample insufficient or anomalous — never call clone provider.
        #
        # Codex 第十二轮 P1-2: guard against NaN / inf / non-finite values.
        # Codex 第十三轮 P2: also guard against None / non-numeric strings
        # — ``float(None)`` raises TypeError, ``float("bad")`` raises
        # ValueError, ``float(2**10000)`` raises OverflowError on some
        # impls. Module docstring promises ``Raises: never``, so wrap
        # the coerce in try/except and route any bad input to PRESET.
        try:
            sample_seconds = float(speaker.sample_seconds)
        except (TypeError, ValueError, OverflowError):
            decisions.append(_preset_decision(
                speaker,
                f"invalid_sample_seconds_{type(speaker.sample_seconds).__name__}",
                smart_decision_id_factory(),
                metrics={"sample_seconds_raw": repr(speaker.sample_seconds)},
            ))
            continue
        if not (math.isfinite(sample_seconds) and sample_seconds >= min_sample_seconds):
            # Distinguish "non-finite anomaly" from "below threshold" in
            # the reason_code so admin / sidecar audit can spot data-
            # quality issues separately from genuine short samples.
            reason_code = (
                f"insufficient_sample_seconds_lt_{int(min_sample_seconds)}"
                if math.isfinite(sample_seconds)
                else f"non_finite_sample_seconds_{sample_seconds}"
            )
            decisions.append(_preset_decision(
                speaker,
                reason_code,
                smart_decision_id_factory(),
                metrics={"sample_seconds": sample_seconds},
            ))
            continue

        # Rule 3: voice library quota at safety water mark — pause
        # this speaker AND all remaining (don't burn the last few units
        # on partial completion).
        if quota_remaining <= quota_safety_water_mark:
            paused_after_speaker = True
            pause_reason = (
                f"voice_library_quota_at_safety_water_mark_"
                f"{quota_remaining}_le_{quota_safety_water_mark}"
            )
            decisions.append(_paused_decision(
                speaker, pause_reason, smart_decision_id_factory(),
                metrics={
                    "voice_library_quota_remaining": quota_remaining,
                    "quota_safety_water_mark": quota_safety_water_mark,
                },
            ))
            continue

        # Rule 4: clone with bounded retries
        decision = _attempt_clone_with_retries(
            speaker=speaker,
            clone_provider=clone_provider,
            max_attempts=max_clone_attempts_per_speaker,
            smart_decision_id_factory=smart_decision_id_factory,
        )
        decisions.append(decision)

        # If the clone path tripped a quota error mid-flight, switch
        # remaining speakers to PAUSED. The CloneProvider Protocol
        # doesn't carry a typed quota signal — adapter implementations
        # raise quota-marked exceptions that we identify by name pattern
        # (matches FakeCloneQuotaError / production MiniMax quota
        # errors which surface a message containing "quota").
        if decision.choice is VoiceReviewChoice.PAUSED:
            paused_after_speaker = True
            pause_reason = decision.reason_code
        elif decision.choice is VoiceReviewChoice.CLONED:
            # Successful clone consumed one unit.
            quota_remaining -= 1

    outcome = (
        VoiceReviewOutcome.PAUSED if paused_after_speaker
        else VoiceReviewOutcome.AUTO_APPROVED
    )
    return VoiceReviewResult(
        outcome=outcome,
        decisions=tuple(decisions),
        pause_reason=pause_reason if outcome is VoiceReviewOutcome.PAUSED else None,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _attempt_clone_with_retries(
    *,
    speaker: VoiceReviewSpeakerInput,
    clone_provider: CloneProvider,
    max_attempts: int,
    smart_decision_id_factory: Callable[[], str],
) -> VoiceReviewDecision:
    """Bounded-retry wrapper around ``clone_provider.clone_voice``.

    Mid-flight quota errors short-circuit to PAUSED; other errors
    consume an attempt and retry until exhausted. Generic exception
    catch is intentional — CloneProvider Protocol doesn't constrain
    error types and adapter implementations may raise provider-specific
    errors we don't want to leak shape knowledge of into this module.
    """
    smart_decision_id = smart_decision_id_factory()
    last_error_repr: str | None = None

    for attempt_num in range(1, max_attempts + 1):
        try:
            result: CloneResult = clone_provider.clone_voice(
                speaker_id=speaker.speaker_id,
                speaker_name=speaker.speaker_name,
                source_audio_path=speaker.source_audio_path,
            )
        except Exception as exc:
            error_repr = f"{type(exc).__name__}: {exc}"
            last_error_repr = error_repr
            # Quota exhaustion — distinguish from generic failure
            # because integration layer must pause task, not fall
            # through to preset (plan §7.3 hard rule).
            if _looks_like_quota_error(exc):
                logger.warning(
                    "auto_voice_review: speaker_id=%s clone PAUSED on quota "
                    "(attempt %d/%d, error=%s)",
                    speaker.speaker_id, attempt_num, max_attempts, error_repr,
                )
                return VoiceReviewDecision(
                    speaker_id=speaker.speaker_id,
                    speaker_name=speaker.speaker_name,
                    choice=VoiceReviewChoice.PAUSED,
                    cloned_voice_id=None,
                    cloned_provider_name=None,
                    cloned_model_name=None,
                    reason_code="provider_quota_exhausted_mid_flight",
                    smart_decision_id=smart_decision_id,
                    metrics={
                        "attempts_made": attempt_num,
                        "last_error": error_repr,
                    },
                )
            logger.info(
                "auto_voice_review: speaker_id=%s clone attempt %d/%d failed (%s)",
                speaker.speaker_id, attempt_num, max_attempts, error_repr,
            )
            continue

        # Success.
        return VoiceReviewDecision(
            speaker_id=speaker.speaker_id,
            speaker_name=speaker.speaker_name,
            choice=VoiceReviewChoice.CLONED,
            cloned_voice_id=result.voice_id,
            cloned_provider_name=result.provider_name,
            cloned_model_name=result.model_name,
            reason_code="clone_succeeded",
            smart_decision_id=smart_decision_id,
            metrics={"attempts_made": attempt_num},
        )

    # Retries exhausted — fall through to preset.
    logger.warning(
        "auto_voice_review: speaker_id=%s clone FAILED after %d attempts; "
        "falling through to preset (last_error=%s)",
        speaker.speaker_id, max_attempts, last_error_repr,
    )
    return VoiceReviewDecision(
        speaker_id=speaker.speaker_id,
        speaker_name=speaker.speaker_name,
        choice=VoiceReviewChoice.PRESET,
        cloned_voice_id=None,
        cloned_provider_name=None,
        cloned_model_name=None,
        reason_code=f"provider_failure_max_retries_{max_attempts}",
        smart_decision_id=smart_decision_id,
        metrics={
            "attempts_made": max_attempts,
            "last_error": last_error_repr,
        },
    )


def _preset_decision(
    speaker: VoiceReviewSpeakerInput,
    reason_code: str,
    smart_decision_id: str,
    *,
    metrics: dict[str, Any] | None = None,
) -> VoiceReviewDecision:
    return VoiceReviewDecision(
        speaker_id=speaker.speaker_id,
        speaker_name=speaker.speaker_name,
        choice=VoiceReviewChoice.PRESET,
        cloned_voice_id=None,
        cloned_provider_name=None,
        cloned_model_name=None,
        reason_code=reason_code,
        smart_decision_id=smart_decision_id,
        metrics=metrics or {},
    )


def _reused_decision(
    speaker: VoiceReviewSpeakerInput,
    match: VoiceReviewExistingMatch,
    smart_decision_id: str,
) -> VoiceReviewDecision:
    return VoiceReviewDecision(
        speaker_id=speaker.speaker_id,
        speaker_name=speaker.speaker_name,
        choice=VoiceReviewChoice.REUSED,
        cloned_voice_id=str(match.voice_id or "").strip(),
        cloned_provider_name=match.provider_name,
        cloned_model_name=match.model_name,
        reason_code="reused_user_voice",
        smart_decision_id=smart_decision_id,
        metrics={
            "match_confidence": match.confidence,
            "match_reason": match.reason,
            "matched_user_voice_id": match.user_voice_id,
        },
    )


def _paused_decision(
    speaker: VoiceReviewSpeakerInput,
    reason_code: str,
    smart_decision_id: str,
    *,
    metrics: dict[str, Any] | None = None,
) -> VoiceReviewDecision:
    return VoiceReviewDecision(
        speaker_id=speaker.speaker_id,
        speaker_name=speaker.speaker_name,
        choice=VoiceReviewChoice.PAUSED,
        cloned_voice_id=None,
        cloned_provider_name=None,
        cloned_model_name=None,
        reason_code=reason_code,
        smart_decision_id=smart_decision_id,
        metrics=metrics or {},
    )


def _new_clone_blocked_reason(
    *,
    consent_allows_clone: bool,
    admin_allows_new_clone: bool,
) -> str | None:
    """Phase 3 (plan 2026-05-17 §审计 reason_codes) — return a reason
    code identifying which gate(s) blocked new clone, or None if both
    gates allow it.

    Three distinct codes so the audit log can trace whether the user
    revoked consent, an admin disabled the policy switch, or both —
    a key signal for support / billing dispute traceability.
    """
    if not consent_allows_clone and not admin_allows_new_clone:
        return "new_clone_blocked_by_consent_and_admin"
    if not consent_allows_clone:
        return "new_clone_blocked_by_consent"
    if not admin_allows_new_clone:
        return "new_clone_blocked_by_admin"
    return None


def _looks_like_quota_error(exc: BaseException) -> bool:
    """Heuristic — treat any exception whose class name OR message
    mentions "quota" as a quota-exhaustion signal so the orchestrator
    pauses rather than treating it as a generic clone failure.

    Matches FakeCloneQuotaError (used by tests) and the production
    MiniMax error messages which surface "quota" / "quota_low" /
    "quota_exceeded" in the body. Conservative: false positives just
    mean we pause earlier than necessary, which is the safe direction
    (plan §7.3 — quota exhaustion is the one error class where
    fall-through to preset is wrong).
    """
    name = type(exc).__name__.lower()
    if "quota" in name:
        return True
    return "quota" in str(exc).lower()


__all__ = [
    "DEFAULT_MAX_CLONE_ATTEMPTS",
    "DEFAULT_QUOTA_SAFETY_WATER_MARK",
    "MIN_SAMPLE_SECONDS",
    "VoiceReviewChoice",
    "VoiceReviewDecision",
    "VoiceReviewOutcome",
    "VoiceReviewResult",
    "VoiceReviewSpeakerInput",
    "evaluate_voice_review",
]
