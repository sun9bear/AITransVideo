"""Smart MVP §6.1 — eligibility gate (PR#3A, business-logic only).

Pure-deterministic decision module: given S2 speaker stats, return whether
this job stays on the Smart auto-decision path or hands off to Studio.

Input shapes (Codex 第九轮 P1-1 fix):

  - **canonical** ``{"speakers": [{"speaker_id": ..., "duration_share": ...,
    "dubbing_mode": ..., "role": ...}, ...]}`` — what this module emits and
    what tests build directly.

  - **process.py profile** — a dict ``{<speaker_id>: {"speaker_role": ...,
    "speaker_duration_share": ..., ...}, ...}`` produced by
    ``src/pipeline/process.py`` (see ``_compute_speaker_structure_profiles``
    around line 4013). Field names differ (``speaker_role`` /
    ``speaker_duration_share``) and the structure is dict-of-profiles
    not list — the integration layer in PR#3C passes this shape verbatim.

  - **simulator** ``{"speaker_count_by_threshold": {"0.10": <int>, ...}}``
    — what shadow simulator (scripts/smart_shadow_sim_simulator.py:121)
    consumes. Already pre-aggregated counts at multiple thresholds, no
    per-speaker access needed.

``normalize_speaker_stats(raw)`` accepts any of the three shapes (the
list shape carries TWO sub-flavours per Codex 第十轮 P1: canonical and
process-prefixed) and emits the canonical form so ``evaluate_eligibility``
only deals with one shape. Tests cover all three input forms + the
list-with-prefixes flavour.

**PR#3C integration contract** (Codex 第十轮 P1 末段):

The voice_selection_review payload form (process.py:4320-4353) does
NOT carry ``dubbing_mode`` at the speaker level — that field is
segment-level state. The PR#3C integration layer MUST aggregate
``segment.dubbing_mode`` → speaker-level BEFORE calling
``evaluate_eligibility``; otherwise ``keep_original`` speakers will
count toward the main-speaker limit (because the default is "dub").

Suggested aggregation (PR#3C reference impl):

  speaker_dubbing_mode = (
      "keep_original" if all(seg.dubbing_mode == "keep_original" for seg in speaker_segments)
      else "mute_or_background" if all(seg.dubbing_mode == "mute_or_background" for seg in speaker_segments)
      else "dub"
  )

Then either pass an enriched canonical-shape dict to evaluate_eligibility,
or write the aggregated value into the voice_selection_review payload
entries before forwarding.

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
boundaries (1, 2, 3, 4 speaker counts), the exclusion rules, and the
three input shapes.
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


def normalize_speaker_stats(
    raw: Mapping[str, Any],
    *,
    low_share_threshold: float = DEFAULT_LOW_SHARE_THRESHOLD,
) -> Mapping[str, Any]:
    """Convert any of the three supported input shapes into the canonical
    ``{"speakers": [...]}`` form expected by ``evaluate_eligibility``.

    Codex 第九轮 P1-1: the production integration layer in PR#3C feeds
    ``_compute_speaker_structure_profiles()`` output (process.py:4013)
    which uses ``speaker_role`` / ``speaker_duration_share`` field names
    and a dict-of-profiles structure. Without normalisation,
    ``evaluate_eligibility`` would see no ``role`` / ``duration_share``
    fields and silently treat every speaker as 0-share, approving
    multi-speaker jobs that should hand off.

    Recognition order:
      1. ``raw["speakers"]`` is a list → already canonical, copied through
      2. ``raw["speaker_count_by_threshold"]`` is a dict → simulator
         shape; emit a synthetic single-bucket "speakers" list whose
         length matches the count at the requested threshold (for
         downstream limit comparison only — the synthetic speakers don't
         carry real IDs since the simulator shape doesn't expose them)
      3. raw looks like ``{<speaker_id>: {speaker_role: ..., ...}, ...}``
         → process.py profile shape; renormalise field names
         (``speaker_role`` → ``role``, ``speaker_duration_share`` →
         ``duration_share``) and emit list-of-speakers

    Returns the canonical shape; preserves the input on path (1) without
    copying entries; never raises (returns ``{"speakers": []}`` on
    unknown shapes so the caller's "no_speakers_detected" sentinel
    does the right thing).
    """
    if not isinstance(raw, Mapping):
        return {"speakers": []}

    # Shape 1: canonical / list-with-prefixes (Codex 第十轮 P1 fix)
    #
    # ``raw["speakers"]`` is a list — could be either:
    #   (a) canonical: each entry has ``speaker_id`` + ``duration_share``
    #       + ``role`` + ``dubbing_mode`` (what tests build directly)
    #   (b) process.py voice_selection_review payload: each entry has
    #       ``speaker_id`` + ``speaker_duration_share`` + ``speaker_role``
    #       (the prefixed field names, see process.py:4320-4353
    #       _build_voice_selection_review_payload)
    #
    # First fix attempt (PR#3A-fix v1) only checked dict-of-profiles
    # shape — list-with-prefixes was silently pass-through, so
    # ``speaker.get("duration_share")`` returned 0.0 for every speaker
    # and main_count was always 0. Codex 第十轮 P1 caught this on a
    # real voice_selection_review payload.
    raw_speakers = raw.get("speakers")
    if isinstance(raw_speakers, list):
        # Detect: any entry uses prefixed field names → renormalise.
        # Otherwise pass-through (real canonical input from tests).
        needs_renorm = any(
            isinstance(s, Mapping)
            and ("speaker_role" in s or "speaker_duration_share" in s)
            for s in raw_speakers
        )
        if not needs_renorm:
            return raw  # canonical — pass-through
        normalised = []
        for s in raw_speakers:
            if not isinstance(s, Mapping):
                continue
            entry: dict[str, Any] = {"speaker_id": str(s.get("speaker_id") or "")}
            # Field-name renormalisation, prefixed wins when both present
            # (real production data carries the prefixed form).
            if "speaker_duration_share" in s:
                entry["duration_share"] = float(s.get("speaker_duration_share") or 0.0)
            elif "duration_share" in s:
                entry["duration_share"] = float(s.get("duration_share") or 0.0)
            else:
                entry["duration_share"] = 0.0
            if "speaker_role" in s:
                entry["role"] = s["speaker_role"]
            elif "role" in s:
                entry["role"] = s["role"]
            # voice_selection_review payload doesn't carry dubbing_mode
            # at the speaker level — that's segment-level state in
            # process.py. Default "dub"; the PR#3C integration layer
            # MUST aggregate segment.dubbing_mode → speaker level
            # (e.g. "all keep_original segments → speaker is keep_original")
            # BEFORE calling evaluate_eligibility, otherwise keep_original
            # speakers will count toward the main-speaker limit. See the
            # module docstring "PR#3C integration contract" for details.
            entry["dubbing_mode"] = s.get("dubbing_mode", "dub")
            normalised.append(entry)
        return {
            "speakers": normalised,
            "_normalize_source": "process_voice_selection_review_speakers_list",
        }

    # Shape 2: simulator pre-aggregated counts
    sct = raw.get("speaker_count_by_threshold")
    if isinstance(sct, Mapping):
        key = f"{low_share_threshold:.2f}"
        count = sct.get(key)
        if not isinstance(count, int):
            return {"speakers": [], "_normalize_source": "simulator_missing_count"}
        # Synthesise N speakers at exactly the threshold so downstream
        # share + limit comparison works. We don't have real IDs in this
        # shape, so use index-based synthetic IDs the integration layer
        # is responsible for replacing if it needs them downstream.
        return {
            "speakers": [
                {
                    "speaker_id": f"sim_speaker_{i}",
                    "duration_share": low_share_threshold,
                    "dubbing_mode": "dub",
                }
                for i in range(count)
            ],
            "_normalize_source": "simulator_speaker_count_by_threshold",
        }

    # Shape 3: process.py profile dict (dict[speaker_id → profile])
    # Heuristic: every value is a dict that has ``speaker_role`` or
    # ``speaker_duration_share``. Reject if any value isn't a profile-
    # shaped dict so we don't pick up unrelated dict shapes.
    items = list(raw.items())
    if items and all(
        isinstance(v, Mapping)
        and ("speaker_role" in v or "speaker_duration_share" in v)
        for k, v in items
    ):
        speakers = []
        for sid, profile in items:
            entry: dict[str, Any] = {"speaker_id": str(sid)}
            # Field-name renormalisation
            entry["duration_share"] = float(profile.get("speaker_duration_share", 0.0) or 0.0)
            role = profile.get("speaker_role")
            if role is not None:
                entry["role"] = role
            # process.py uses dubbing_mode at segment level not profile,
            # so this field is typically absent here. Default "dub" so
            # the canonical-form _is_main_speaker check doesn't exclude.
            dubbing_mode = profile.get("dubbing_mode", "dub")
            entry["dubbing_mode"] = dubbing_mode
            speakers.append(entry)
        return {
            "speakers": speakers,
            "_normalize_source": "process_speaker_structure_profile",
        }

    # Unknown shape — fall through to caller's no_speakers_detected.
    return {"speakers": [], "_normalize_source": "unknown_shape"}


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

    Codex 第九轮 P1-1: ``normalize_speaker_stats`` is invoked first so
    real process.py / simulator inputs are accepted without callers
    needing to pre-massage them.
    """
    speaker_stats = normalize_speaker_stats(
        speaker_stats, low_share_threshold=low_share_threshold
    )
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
    "normalize_speaker_stats",
]
