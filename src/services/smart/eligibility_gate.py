"""Smart MVP §6.1 — eligibility gate (PR#3A, business-logic only).

Pure-deterministic decision module: given S2 speaker stats, return whether
this job stays on the Smart auto-decision path or hands off to Studio.

Input: ``speaker_stats`` dict (subset of S2 result schema). Real S2 emits
much more; this module reads only the keys it needs so the call site can
swap the source without restructuring.

Output: ``EligibilityDecision`` dataclass — caller (process.py integration
in PR#3C) routes to handoff or auto-review based on ``approved``.

Plan §6.1 + 主方案 §2.3:
  - "Main configured speaker" = a speaker that needs dubbing/cloning
  - Excluded from main count: observers/applause, keep_original speakers,
    low-share speakers (duration_share < threshold, default 0.10)
  - Approved if main count ≤ 3, rejected otherwise
  - Reason code: ``main_speaker_count_exceeded`` on rejection (mirrors
    the smart_state.reason field consumed by the handoff path)

This module does NOT:
  - touch ReviewStateManager
  - emit smart_decisions.jsonl (sidecar_emitter does that — separation
    of concerns; same decision payload, different writers)
  - call any provider or do any I/O

Acceptance tests in tests/test_smart_business_logic.py lock the threshold
boundaries (1, 2, 3, 4 speaker counts) and the exclusion rules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


# Threshold constants. Plan §2.3: "low-share" speakers are excluded from
# the main count. P0 §6 calibration: threshold=0.10 has best signal /
# cost trade-off. Hard-coded here on purpose — runtime tuning is a P3+
# concern, not a Smart MVP knob.
DEFAULT_LOW_SHARE_THRESHOLD = 0.10
DEFAULT_MAIN_SPEAKER_LIMIT = 3

# Roles / labels excluded from the main speaker count regardless of share.
# Mirrors the categories called out in plan §6.1 step 2 + 主方案 §2.3.
_NON_DUBBED_DUBBING_MODES = frozenset({"keep_original", "mute_or_background"})
_NON_DUBBED_ROLES = frozenset({"observer", "audience", "applause", "background"})


@dataclass(frozen=True)
class EligibilityDecision:
    """Outcome of one eligibility evaluation.

    ``approved=True`` means the job stays on Smart auto-path. ``approved=False``
    means the caller (process.py integration layer) must trigger handoff
    via emit_handoff_markers, write smart_state.status="downgraded_to_studio"
    + smart_state.reason=<reason_code>, and let the user pick up via
    Studio human-review on /continue.
    """

    approved: bool
    main_speaker_count: int
    main_speaker_ids: tuple[str, ...]
    excluded_speakers: tuple[dict[str, Any], ...]
    reason_code: str | None  # populated only when approved=False
    threshold_used: float
    limit_used: int


def _is_main_speaker(
    speaker: Mapping[str, Any],
    *,
    low_share_threshold: float,
) -> tuple[bool, str | None]:
    """Return (is_main, exclusion_reason).

    A speaker counts as "main" iff:
      - dubbing_mode is dub-able (i.e. not keep_original / mute_or_background)
      - role does not fall into the non-dubbed categories above
      - duration_share >= low_share_threshold

    Exclusion reason is the first failing check (so smart_decisions can
    record WHY the speaker was excluded, not just that it was).
    """
    dubbing_mode = str(speaker.get("dubbing_mode") or "").strip().lower()
    if dubbing_mode in _NON_DUBBED_DUBBING_MODES:
        return False, f"dubbing_mode_{dubbing_mode}"

    role = str(speaker.get("role") or "").strip().lower()
    if role in _NON_DUBBED_ROLES:
        return False, f"role_{role}"

    duration_share = float(speaker.get("duration_share") or 0.0)
    if duration_share < low_share_threshold:
        return False, f"low_share_{duration_share:.3f}"

    return True, None


def evaluate_eligibility(
    speaker_stats: Mapping[str, Any],
    *,
    low_share_threshold: float = DEFAULT_LOW_SHARE_THRESHOLD,
    main_speaker_limit: int = DEFAULT_MAIN_SPEAKER_LIMIT,
) -> EligibilityDecision:
    """Evaluate Smart eligibility from S2 speaker stats.

    Args:
      speaker_stats: dict with at minimum a ``"speakers"`` key whose value
        is an iterable of speaker dicts. Each speaker dict is expected to
        carry ``speaker_id`` / ``duration_share`` / ``role`` (optional) /
        ``dubbing_mode`` (optional). Missing fields default to "include
        as main" — caller is responsible for upstream data quality.

    Returns:
      EligibilityDecision capturing the verdict, the count, the IDs of
      the main speakers (so downstream auto_voice_review knows which
      ones to consider for cloning), the excluded list with reasons
      (so smart_decisions records the rationale), and the threshold
      values actually used (audit trail; useful when admin tunes them).

    Raises: never. Bad input (no "speakers" key, non-iterable, etc.) is
    treated as "no speakers detected" → approved=False with a sentinel
    reason_code. The pipeline integration layer can decide whether to
    treat this as a hard failure or a degraded approval.
    """
    raw_speakers = speaker_stats.get("speakers") if isinstance(speaker_stats, Mapping) else None
    if not raw_speakers:
        return EligibilityDecision(
            approved=False,
            main_speaker_count=0,
            main_speaker_ids=(),
            excluded_speakers=(),
            reason_code="no_speakers_detected",
            threshold_used=low_share_threshold,
            limit_used=main_speaker_limit,
        )

    main_ids: list[str] = []
    excluded: list[dict[str, Any]] = []
    for speaker in raw_speakers:
        if not isinstance(speaker, Mapping):
            continue
        sid = str(speaker.get("speaker_id") or "").strip()
        if not sid:
            # Anonymous speaker rows can't be referenced downstream;
            # exclude with a recordable reason rather than silently drop.
            excluded.append({"speaker_id": "", "reason": "missing_speaker_id"})
            continue
        is_main, exclusion_reason = _is_main_speaker(
            speaker, low_share_threshold=low_share_threshold
        )
        if is_main:
            main_ids.append(sid)
        else:
            excluded.append({"speaker_id": sid, "reason": exclusion_reason})

    main_count = len(main_ids)
    if main_count > main_speaker_limit:
        return EligibilityDecision(
            approved=False,
            main_speaker_count=main_count,
            main_speaker_ids=tuple(main_ids),
            excluded_speakers=tuple(excluded),
            reason_code="main_speaker_count_exceeded",
            threshold_used=low_share_threshold,
            limit_used=main_speaker_limit,
        )

    return EligibilityDecision(
        approved=True,
        main_speaker_count=main_count,
        main_speaker_ids=tuple(main_ids),
        excluded_speakers=tuple(excluded),
        reason_code=None,
        threshold_used=low_share_threshold,
        limit_used=main_speaker_limit,
    )


__all__ = [
    "DEFAULT_LOW_SHARE_THRESHOLD",
    "DEFAULT_MAIN_SPEAKER_LIMIT",
    "EligibilityDecision",
    "evaluate_eligibility",
]
