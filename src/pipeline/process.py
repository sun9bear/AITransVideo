from __future__ import annotations

from dataclasses import asdict as _dc_asdict, dataclass, fields as _dc_fields, replace as _dc_replace
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import time

# Load .env file if present (for API keys not in container env)
_ENV_FILE = Path(os.environ.get("AIVIDEOTRANS_CONFIG_DIR", "/opt/aivideotrans/config")) / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _, _val = _line.partition("=")
            if _key.strip() and _key.strip() not in os.environ:
                os.environ[_key.strip()] = _val.strip()
from typing import Any, Callable, TypeVar

from core.enums import OutputTarget, StageStatus
from services.jobs.models import STAGE_ALIGNMENT, STAGE_LEGACY_PROCESS_OUTPUT
from core.models import SemanticBlock, SubtitleLine
from modules.ingestion.youtube.downloader import (
    DownloadRequest,
    DownloadResult,
    YouTubeDownloader,
    load_youtube_download_config,
)
from modules.output.output_dispatcher import OutputDispatcher
from modules.output.output_models import OutputBundleResult, OutputRequest
from modules.output.project_output import AlignedSegment
from modules.workflow.project_builder import ProjectBuilder
from modules.workflow.project_shape_helpers import (
    build_canonical_source_info,
    build_core_media_artifact_entries,
)
from modules.workflow.workflow_result import WorkflowBuildResult
from modules.workflow.stage_helpers import build_artifacts_payload
from services import config_loader
from services.alignment.aligner import PostTTSBudgetTracker, SegmentAligner
from services.audio.separator import AudioSeparationError, AudioSeparationResult
from services.audio.source_audio_preparation import (
    SourceAudioPreparationRequest,
    SourceAudioPreparationService,
)
from services.content_compliance import (
    ContentPolicyViolationError,
    DEFAULT_REPORT_RELATIVE_PATH,
    ContentComplianceResult,
    LLMContentComplianceReviewer,
    MainlandChinaContentComplianceReviewer,
    combine_content_compliance_results,
    is_content_compliance_enabled,
    is_content_compliance_llm_enabled,
    is_content_compliance_llm_fail_closed,
    load_content_compliance_prompt_template,
    make_content_compliance_llm_error,
    validate_content_compliance_llm_response,
)
from services.assemblyai.transcriber import (
    AssemblyAITranscriber,
    TranscriptLine,
    TranscriptResult,
    TranscriptionError,
    load_assemblyai_config,
)
from services.language_registry import (
    DEFAULT_LANGUAGE_PAIR_PROFILE,
    DEFAULT_SOURCE_LANGUAGE,
    DEFAULT_TARGET_LANGUAGE,
    LANG_EN,
    LANG_ZH_CN,
    SCRIPT_CJK,
    SCRIPT_LATIN,
    LanguagePairProfile,
    get_language_descriptor,
    normalize_language,
    resolve_language_pair,
)
from services.gemini.rewriter import GeminiRewriter
from services.gemini.translator import (
    DEFAULT_ESTIMATED_TTS_CHARS_PER_SECOND,
    DUBBING_MODE_DUB,
    DUBBING_MODE_KEEP_ORIGINAL,
    DubbingSegment,
    GeminiTranslator,
    TranslationResult,
    is_keep_original_dubbing_mode,
    load_gemini_config,
    normalize_dubbing_mode,
)
from services.review_state import (
    REVIEW_STAGE_TAB_MAP,
    REVIEW_STATUS_APPROVED,
    REVIEW_STATUS_PENDING,
    SPEAKER_REVIEW_STAGE,
    TRANSLATION_CONFIG_REVIEW_STAGE,
    TRANSLATION_REVIEW_STAGE,
    VOICE_REVIEW_STAGE,
    VOICE_SELECTION_REVIEW_STAGE,
    ReviewStateManager,
)
from services.llm import LLMRouter, load_llm_fallback_config
from services.llm_registry import (
    get_peer_model_candidates as _get_peer_model_candidates,
    get_prompt_model as _get_prompt_model,
)
from services.state_manager import StateManager
from services.tts.duration_estimator import TTSDurationEstimator, count_spoken_chars
from services.tts.tts_generator import TTSConfig, TTSGenerator, load_tts_config
from services.usage_meter import (
    TTS_BUCKET_FIRST,
    TTS_BUCKET_POST_TTS_RESYNTH,
    TTS_BUCKET_PROBE,
    UsageMeter,
)
from services.voice.auto_clone import AutoCloneError, AutoVoiceCloner
from services.voice_clone import VoiceCloneConfig
from services.voice.sample_extractor import (
    MIN_SAMPLE_DURATION_SECONDS,
    SampleExtractionError,
    VoiceSampleExtractor,
)
from services.voice.voice_lookup import VoiceLookupError, lookup_voice_ids
from utils.audio_utils import measure_duration_ms as _ffprobe_duration_ms
from utils.audio_fit import fit_audio_to_slot as _fit_audio_to_slot


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROJECTS_ROOT_ENV_VAR = "AIVIDEOTRANS_PROJECTS_DIR"
DEFAULT_SPEAKER_TTS_CALIBRATION_MIN_SAMPLES = 3
DEFAULT_PLACEHOLDER_SPEAKER_NAMES = {
    "speaker_a": {"speaker a", "speaker_a"},
    "speaker_b": {"speaker b", "speaker_b"},
}
FAILED_SEGMENT_SEMANTIC_SPLIT_MIN_TARGET_MS = 45_000
FAILED_SEGMENT_SEMANTIC_SPLIT_MIN_RATIO = 0.28
PRE_TTS_REWRITE_MIN_TARGET_MS = 8_000
PRE_TTS_REWRITE_OVERSHOOT_RATIO = 0.20
PRE_TTS_REWRITE_UNDERSHOOT_RATIO = 0.25
PRE_TTS_REWRITE_SHORT_MIN_TARGET_MS = 2_000
PRE_TTS_REWRITE_SHORT_OVERSHOOT_RATIO = 0.30
PRE_TTS_REWRITE_NEAR_SHORT_TARGET_MS = 12_000
PRE_TTS_REWRITE_NEAR_SHORT_OVERSHOOT_RATIO = 0.30
PRE_TTS_REWRITE_SHORT_DECISION_ESTIMATE_MARGIN = 1.15
PRE_TTS_REWRITE_MAX_BASE_CHANGE_RATIO = 0.35
PRE_TTS_REWRITE_REQUIRED_CHANGE_MARGIN = 0.05
PRE_TTS_REWRITE_MAX_CHANGE_CAP = 0.60
# P1-d: short, aggressive shrink requests have higher estimator variance.
# Keep more source text so a fast TTS realization does not flip into undershoot
# and trigger a post-TTS rewrite immediately after the pre-TTS shrink.
PRE_TTS_REWRITE_HIGH_SHRINK_RISK_TARGET_MS = 12_000
PRE_TTS_REWRITE_HIGH_SHRINK_RISK_REQUIRED_SHRINK = 0.45
PRE_TTS_REWRITE_HIGH_SHRINK_RISK_MAX_CHANGE_RATIO = 0.40
PRE_TTS_REWRITE_HIGH_SHRINK_RISK_UPPER_SLACK = 0.05
# P1-g: 8-20s overshoot shrink has shown the highest "shrink then undershoot"
# risk in production. Keep more text than the raw CPS target so a fast TTS
# realization does not flip the direction after pre-TTS rewrite.
PRE_TTS_REWRITE_MID_UNDERSHOOT_RISK_MIN_TARGET_MS = 8_000
PRE_TTS_REWRITE_MID_UNDERSHOOT_RISK_MAX_TARGET_MS = 20_000
PRE_TTS_REWRITE_MID_UNDERSHOOT_RISK_REQUIRED_SHRINK = 0.20
PRE_TTS_REWRITE_MID_UNDERSHOOT_RISK_MAX_CHANGE_RATIO = 0.25
PRE_TTS_REWRITE_MID_UNDERSHOOT_RISK_MIN_TARGET_MULTIPLIER = 1.10
PRE_TTS_REWRITE_MID_UNDERSHOOT_RISK_MAX_TARGET_MULTIPLIER = 1.18
# P1-h: for 20s+ segments, a large pre-TTS shrink can still produce a fast
# first pass and flip into undershoot/force-DSP. Keep a wider safety floor for
# long overshoot only; this does not expand the rewrite trigger set.
PRE_TTS_REWRITE_LONG_UNDERSHOOT_RISK_MIN_TARGET_MS = 20_000
PRE_TTS_REWRITE_LONG_UNDERSHOOT_RISK_REQUIRED_SHRINK = 0.20
PRE_TTS_REWRITE_LONG_UNDERSHOOT_RISK_MAX_CHANGE_RATIO = 0.20
PRE_TTS_REWRITE_LONG_UNDERSHOOT_RISK_MIN_TARGET_MULTIPLIER = 1.15
PRE_TTS_REWRITE_LONG_UNDERSHOOT_RISK_MAX_TARGET_MULTIPLIER = 1.28
# P1-k: long overshoot is expensive to leave until post-TTS. If the cheap
# rewrite misses guardrails, allow one strict retry on the stronger rewrite
# route, but keep the same deterministic char bounds before accepting it.
PRE_TTS_REWRITE_STRICT_RETRY_MIN_TARGET_MS = 20_000
PRE_TTS_REWRITE_STRICT_RETRY_TASK = "s5_rewrite_strict"
SHORT_MERGE_CANDIDATE_MAX_TARGET_MS = 2_000
SHORT_MERGE_CANDIDATE_MAX_SPOKEN_CHARS = 18
SHORT_MERGE_MAX_GAP_MS = 650
SHORT_MERGE_MAX_COMBINED_TARGET_MS = 10_000
SPEAKER_STRUCTURE_SHORT_SEGMENT_MS = 8_000
SPEAKER_STRUCTURE_INCIDENTAL_MAX_SHARE = 0.08
SPEAKER_STRUCTURE_INCIDENTAL_MAX_DURATION_MS = 60_000
SPEAKER_STRUCTURE_INCIDENTAL_MAX_SINGLE_SEGMENT_MS = 20_000
SPEAKER_STRUCTURE_INCIDENTAL_MAX_SEGMENTS = 8
SPEAKER_STRUCTURE_INCIDENTAL_MIN_SHORT_RATE = 0.75
SPEAKER_STRUCTURE_FRAGMENTED_MAX_SHARE = 0.15
SPEAKER_STRUCTURE_FRAGMENTED_MIN_SEGMENTS = 3
SPEAKER_STRUCTURE_FRAGMENTED_MIN_SHORT_RATE = 0.60
SPEAKER_STRUCTURE_NON_SPEECH_MARKERS = (
    "non_speech",
    "non-speech",
    "not speech",
    "background music",
    "music",
    "song",
    "singing",
    "chant",
    "chanting",
    "cheering",
    "applause",
    "crowd noise",
    "背景音乐",
    "背景音",
    "非对白",
    "非语音",
    "音乐",
    "歌曲",
    "唱歌",
    "合唱",
    "欢呼",
    "喝彩",
    "掌声",
    "噪声",
)
LOW_INFORMATION_UNDERFLOW_KEEP_ORIGINAL_MIN_TARGET_MS = 4_000
LOW_INFORMATION_UNDERFLOW_KEEP_ORIGINAL_MIN_STRETCH_RATIO = 2.5
LOW_INFORMATION_UNDERFLOW_KEEP_ORIGINAL_MAX_SOURCE_WORDS = 8
LOW_INFORMATION_UNDERFLOW_KEEP_ORIGINAL_MAX_SPOKEN_CHARS = 18
LOW_INFORMATION_CUE_TOKENS = frozenset({
    "ah",
    "alright",
    "break",
    "done",
    "er",
    "exercise",
    "final",
    "go",
    "half",
    "halfway",
    "hmm",
    "huh",
    "last",
    "left",
    "minute",
    "minutes",
    "next",
    "ok",
    "okay",
    "ready",
    "rest",
    "right",
    "second",
    "seconds",
    "start",
    "stop",
    "switch",
    "uh",
    "um",
    "yeah",
    "yes",
})
SHORT_CONTENT_COMPACT_TASK = "s5_short_content_compact"
SHORT_CONTENT_COMPACT_MIN_TARGET_MS = 2_000
SHORT_CONTENT_COMPACT_MAX_TARGET_MS = 8_000
SHORT_CONTENT_COMPACT_MIN_OVERSHOOT_RATIO = 0.30
SHORT_CONTENT_COMPACT_MIN_SOURCE_WORDS = 3
SHORT_CONTENT_COMPACT_MIN_PRE_CHARS_OVER_UPPER = 2
SHORT_CONTENT_COMPACT_CHARS_PER_SECOND_LOWER = 2.6
SHORT_CONTENT_COMPACT_LONG_TARGET_MIN_MS = 5_000
SHORT_CONTENT_COMPACT_LONG_CHARS_PER_SECOND_LOWER = 3.0
SHORT_CONTENT_COMPACT_CHARS_PER_SECOND_UPPER = 4.0
CONTENT_COMPLIANCE_LLM_RETRY_DELAY_SECONDS = 5.0
CONTENT_COMPLIANCE_LLM_PEER_COST_RANK_DELTA = 1
CONTENT_COMPLIANCE_ADMIN_OVERRIDE_EVENT = "job.content_compliance_admin_override"
SHORT_CONTENT_COMPACT_QUESTION_STARTERS = frozenset({
    "am",
    "are",
    "can",
    "could",
    "did",
    "do",
    "does",
    "had",
    "has",
    "have",
    "how",
    "is",
    "may",
    "might",
    "should",
    "was",
    "were",
    "what",
    "when",
    "where",
    "who",
    "whom",
    "whose",
    "why",
    "will",
    "would",
})
SHORT_CONTENT_COMPACT_FILLER_TOKENS = frozenset({
    "a",
    "actually",
    "and",
    "basically",
    "but",
    "i",
    "just",
    "kind",
    "like",
    "mean",
    "of",
    "okay",
    "right",
    "so",
    "sort",
    "that",
    "the",
    "uh",
    "um",
    "well",
    "you",
    "know",
})
SHORT_CONTENT_COMPACT_NON_SPEECH_TOKENS = frozenset({
    "applause",
    "chant",
    "cheering",
    "cheers",
    "crowd",
    "laughs",
    "laughter",
    "music",
    "noise",
    "singing",
    "song",
})
# Plan-C+ (2026-04-15): when TTS speed can absorb the drift safely
# (within both admin speed clamp AND a listen-comfort guardrail),
# skip the pre-TTS rewrite — it would just be a wasted LLM call.
# The listen-limit floor/ceiling protects against unlimited-mode making
# TTS sound rushed or sluggish; ratios beyond it still rewrite.
PRE_TTS_REWRITE_LISTEN_LIMIT_HIGH = 1.30
PRE_TTS_REWRITE_LISTEN_LIMIT_LOW = 0.80
# Providers with per-segment TTS speed wired up. CodeX P1-2: pre-rewrite
# can only be safely skipped when the segment will go through one of these.
# VolcEngine joined 2026-04-15 after scripts/test_volcengine_speech_rate.py
# confirmed audio_params.speech_rate is honored within |err|<5% across
# seed-tts-{1.0,2.0}. CosyVoice joined same day — DashScope SDK inspect
# confirmed SpeechSynthesizer(..., speech_rate=1.0) is a first-class param;
# its semantics match MiniMax voice_setting.speed directly (0.5-2.0 float),
# so no numeric mapping is needed.
SPEED_AWARE_TTS_PROVIDERS: frozenset[str] = frozenset({"minimax", "volcengine", "cosyvoice"})
PRE_ALIGNMENT_SEMANTIC_SPLIT_OVERSHOOT_RATIO = 0.30
SEVERE_PRE_ALIGNMENT_SEMANTIC_SPLIT_MIN_TARGET_MS = 30_000
SEVERE_PRE_ALIGNMENT_SEMANTIC_SPLIT_OVERSHOOT_RATIO = 0.35
FAILED_SEGMENT_SEMANTIC_SPLIT_PATTERN = re.compile(r"(?<=[。！？!?；;])\s*")
FAILED_SEGMENT_SOURCE_SPLIT_PATTERN = re.compile(r"(?<=[.!?;])\s+")
# Script-aware split patterns for failed-segment repair. CJK text splits on
# full-width sentence punctuation (no required trailing space); Latin text splits
# on ASCII sentence punctuation followed by whitespace. The GA default pair
# (target=CJK cn_text, source=Latin) maps to exactly the two legacy constants
# above, so default-pair splitting is byte-identical.
_FAILED_SEGMENT_SPLIT_PATTERN_BY_SCRIPT = {
    SCRIPT_CJK: FAILED_SEGMENT_SEMANTIC_SPLIT_PATTERN,
    SCRIPT_LATIN: FAILED_SEGMENT_SOURCE_SPLIT_PATTERN,
}

T = TypeVar("T")

_ADMIN_SETTINGS_PATH = str(
    Path(os.environ.get("AIVIDEOTRANS_CONFIG_DIR", "/opt/aivideotrans/config"))
    / "admin_settings.json"
)
_SPEAKER_ID_PATTERN = re.compile(r"^speaker_[a-z0-9_]+$")


# _should_skip_translation_config / _should_skip_all_reviews removed —
# decision now comes from the job record's snapshot fields
# (job_requires_review, job_service_mode).  See run() below.


def _build_b2_not_wired_clone_provider():
    """Fail-closed CloneProvider stub for PR#3C-b2 smart inline auto-approve.

    Codex 第十八轮 P0-2: PR#3C-b2's first cut called
    ``services.smart_wiring.build_smart_clone_provider()`` directly, which
    returns the real ``_MiniMaxCloneAdapter``. Combined with stub
    ``source_audio_path`` (whole-file rather than per-speaker concat)
    and stub ``voice_library_quota_remaining=100``, that meant every
    Smart job whose ``smart_consent.auto_voice_clone=True`` AND whose
    ``total_duration_s >= 10s`` would burn real MiniMax clone API calls
    — violating CLAUDE.md "付费 API 不能自动调用 / fallback / 兜底".

    The fix routes Smart through a Protocol-conforming stub that raises
    ``RuntimeError`` on every ``clone_voice()`` call. The retry
    loop inside ``evaluate_voice_review`` catches the exception, exhausts
    the per-speaker retry budget, and falls through to PRESET. Net effect:
    Smart auto-approve happy path works end-to-end on PRESET decisions,
    no paid API call leaves the box.

    Codex 第十九轮 P1: the raised exception class + message MUST NOT
    contain the substring "quota" anywhere. auto_voice_review's
    ``_looks_like_quota_error`` heuristic substring-matches that token
    in the type name and ``str(exc)``; a stub that accidentally
    triggered it would route smart to ``PAUSED`` (= handoff) instead
    of the intended PRESET fall-through, sending every normal smart
    job through Studio human-review.

    PR#3C-b3 replaces this stub with the real
    ``build_smart_clone_provider()`` invocation ONLY when the matching
    per-speaker ffmpeg sample + real ``voice_library_quota`` snapshot
    are wired alongside (i.e. all three move together so the safety
    invariant is preserved).
    """
    from pathlib import Path as _Path

    from services.smart.contracts import CloneResult as _CloneResult  # noqa: F401

    class _B2NotWiredCloneProvider:
        """Always raises — auto_voice_review retry loop catches and
        falls to PRESET. See _build_b2_not_wired_clone_provider docstring."""

        def clone_voice(
            self,
            *,
            speaker_id: str,
            speaker_name: str,
            source_audio_path: _Path,
        ) -> "_CloneResult":  # type: ignore[name-defined]
            # Codex 第十九轮 P1: exception type AND message must NOT
            # contain the substring "quota" — auto_voice_review's
            # ``_looks_like_quota_error`` heuristic substring-matches
            # the exception name + str(exc) and would route here to
            # ``PAUSED/provider_quota_exhausted_mid_flight`` instead of
            # ``PRESET/provider_failure_max_retries_N``. Use RuntimeError
            # (no "quota" in class name) + message wording that talks
            # about account snapshot wiring without ever saying
            # "quota". Confirmed by
            # test_b2_stub_clone_provider_routes_to_preset_not_quota_pause
            # in test_smart_studio_gate_acceptance.py.
            raise RuntimeError(
                "Smart CloneProvider intentionally not wired in PR#3C-b2; "
                "per-speaker sample and account snapshot land in PR#3C-b3."
            )

    return _B2NotWiredCloneProvider()


def _fetch_smart_user_voice_quota_remaining(user_id: str) -> int | None:
    """Query Gateway internal endpoint for the per-user voice library quota.

    Returns ``remaining`` (int) on success, ``None`` on any failure.

    Codex 第二十七轮 P0 atomic contract (PR#3C-b3e): smart auto-clone
    only fires when this returns a real integer. ``None`` triggers
    fail-closed handoff in the caller — the real provider must never
    see a placeholder value.

    Implementation:
      - GET http://127.0.0.1:8880/api/internal/user-voices/quota?user_id=<uuid>
      - X-Internal-Key from AVT_INTERNAL_API_KEY env (must be set; the
        Caddyfile @internal_block blocks unauthenticated calls)
      - 3s timeout (matches other internal lookups in
        ``services/tts/voice_speed_catalog.py``)
      - Catches ANY exception (network / auth / parse) → returns None
        so the caller routes through fail-closed handoff
      - On HTTP 200, reads ``remaining`` from JSON body. Validates it's
        a non-negative int.

    The Gateway endpoint (``user_voice_api.internal_user_voice_quota``)
    computes ``remaining = max(0, smart_user_voice_clone_cap - used)``
    where ``used`` is the count of non-expired UserVoice rows for the
    user. Both halves are admin-tunable (cap via admin_settings.json,
    used via the natural CRUD).
    """
    import os
    import requests  # type: ignore[import-not-found]

    user_id = (user_id or "").strip()
    if not user_id:
        return None
    api_key = os.environ.get("AVT_INTERNAL_API_KEY", "").strip()
    if not api_key:
        # No internal key → can't authenticate. Treat as quota
        # unavailable; caller will fail-closed handoff.
        return None
    try:
        resp = requests.get(
            "http://127.0.0.1:8880/api/internal/user-voices/quota",
            params={"user_id": user_id},
            headers={"X-Internal-Key": api_key},
            timeout=3.0,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:
        return None

    remaining = data.get("remaining")
    if not isinstance(remaining, int) or remaining < 0:
        return None
    return remaining


def _match_smart_user_voice(
    *,
    user_id: str,
    source_content_hash: str | None,
    speaker_id: str,
    speaker_name: str | None = None,
    include_possible: bool = False,
) -> dict | None:
    """Return Smart's personal-voice match envelope for one speaker.

    Phase 3 contract (``include_possible=False``, default): returns the
    legacy single-strong-match shape — dict with keys ``voice_id`` /
    ``provider`` / ``tts_provider`` / ``user_voice_id`` / ``confidence``
    / ``reason``, or None when no strong match exists.

    Phase 4 extension (``include_possible=True``, plan 2026-05-17 §Phase
    1 unified candidate endpoint): switches to the
    ``/api/internal/user-voices/candidates`` endpoint and returns a
    structured envelope::

        {
          "auto_reuse": <legacy single-strong-match dict or None>,
          "possible":   <list of non-strong candidate dicts>,
        }

    The auto-reuse subdict mirrors the legacy shape so the Phase 3
    REUSED path code is identical. ``possible`` carries the
    non-strong personal-voice candidates (medium / weak / cross-source-
    named) for the Phase 4 pause decision. Both fields may be empty
    (``auto_reuse=None``, ``possible=[]``) on an empty library.

    Best-effort optimization only: failures return None so the caller
    can continue with the existing new-clone path. The function MUST
    NOT raise — Smart pipeline relies on safe fail-open behavior.
    """
    import os
    import requests  # type: ignore[import-not-found]

    user_id = (user_id or "").strip()
    source_content_hash = (source_content_hash or "").strip()
    speaker_id = (speaker_id or "").strip()
    if not user_id or not speaker_id:
        return None
    # Same-source matching requires a content hash; cross-source named
    # matching (Phase 1 ``include_cross_source=True``) tolerates an
    # empty hash, but only the candidates endpoint supports it. For the
    # legacy strong-match path, hash is still required so we don't
    # waste an HTTP call.
    if not include_possible and not source_content_hash:
        return None
    api_key = os.environ.get("AVT_INTERNAL_API_KEY", "").strip()
    if not api_key:
        return None
    endpoint = (
        "/api/internal/user-voices/candidates"
        if include_possible
        else "/api/internal/user-voices/match"
    )
    payload: dict[str, Any] = {
        "user_id": user_id,
        "source_content_hash": source_content_hash or None,
        "speaker_id": speaker_id,
        "speaker_name": speaker_name or "",
        "provider": "minimax_voice_clone",
        "tts_provider": "minimax_tts",
        "platform": "minimax_domestic",
        "limit": 3,
    }
    if include_possible:
        # candidates endpoint defaults include_cross_source=True; we
        # set it explicitly so the contract is obvious at the call site.
        payload["include_cross_source"] = True
    try:
        resp = requests.post(
            f"http://127.0.0.1:8880{endpoint}",
            json=payload,
            headers={"X-Internal-Key": api_key},
            timeout=3.0,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:
        return None

    if not include_possible:
        # Legacy /user-voices/match shape — preserve Phase 3 behavior.
        if not data.get("matched") or data.get("confidence") != "strong":
            return None
        voice = data.get("voice")
        if not isinstance(voice, dict) or not str(voice.get("voice_id") or "").strip():
            return None
        return {
            "voice_id": str(voice.get("voice_id") or "").strip(),
            "provider": str(voice.get("provider") or "") or None,
            "tts_provider": str(voice.get("tts_provider") or "") or None,
            "user_voice_id": str(voice.get("id") or "") or None,
            "confidence": str(data.get("confidence") or ""),
            "reason": str(data.get("reason") or ""),
        }

    # Phase 4: candidates endpoint envelope.
    auto_reuse_voice = data.get("auto_reuse_voice")
    auto_reuse: dict | None = None
    if isinstance(auto_reuse_voice, dict):
        v_id = str(auto_reuse_voice.get("voice_id") or "").strip()
        if v_id:
            auto_reuse = {
                "voice_id": v_id,
                # The candidates endpoint normalizes provider/tts_provider
                # at match time; the public envelope only carries voice_id
                # / label / confidence / scope / reason. Provider/tts
                # default to the request triplet because the matcher
                # restricts to it (provider=minimax_voice_clone +
                # tts_provider=minimax_tts).
                "provider": "minimax_voice_clone",
                "tts_provider": "minimax_tts",
                "user_voice_id": (
                    str(auto_reuse_voice.get("user_voice_id") or "") or None
                ),
                "confidence": str(auto_reuse_voice.get("confidence") or ""),
                "reason": str(auto_reuse_voice.get("reason") or ""),
            }

    raw_candidates = data.get("personal_voice_candidates") or []
    possible: list[dict] = []
    if isinstance(raw_candidates, list):
        # 2026-05-21: ``strong_named`` joins ``strong`` as auto-reuse-
        # tier (cross-source unique-in-library named match). Both go to
        # ``auto_reuse``, neither belongs in the ``possible`` (pause-
        # for-confirm) list. Without this filter strong_named would
        # appear in BOTH, double-counted in admin UI + audit.
        _auto_reuse_confidences = {"strong", "strong_named"}
        for cand in raw_candidates:
            if not isinstance(cand, dict):
                continue
            if str(cand.get("confidence") or "") in _auto_reuse_confidences:
                continue
            v_id = str(cand.get("voice_id") or "").strip()
            if not v_id:
                continue
            possible.append({
                "voice_id": v_id,
                "user_voice_id": cand.get("user_voice_id"),
                "label": cand.get("label"),
                "confidence": cand.get("confidence"),
                "match_scope": cand.get("match_scope"),
                "reason": cand.get("reason"),
            })
    return {"auto_reuse": auto_reuse, "possible": possible}


def _apply_smart_reused_voice_decision(
    *,
    speaker_entry: dict,
    decision,
    usage_meter,
    project_dir: Path,
    job_id: str,
    user_id: str,
) -> None:
    speaker_entry["voice_id"] = decision.cloned_voice_id
    speaker_entry["clone_provider"] = decision.cloned_provider_name
    speaker_entry["auto_decision"] = "reused_user_voice"
    speaker_entry["voice_reuse"] = True
    speaker_entry["matched_user_voice_id"] = decision.metrics.get("matched_user_voice_id")
    speaker_entry["match_confidence"] = decision.metrics.get("match_confidence")
    speaker_entry["match_reason"] = decision.metrics.get("match_reason")
    try:
        usage_meter.record_voice_reuse(
            provider=decision.cloned_provider_name or "minimax_voice_clone",
            voice_id=decision.cloned_voice_id or "",
            speaker_id=decision.speaker_id,
            source_voice_id=decision.cloned_voice_id or "",
            match_confidence=str(decision.metrics.get("match_confidence") or ""),
            match_reason=str(decision.metrics.get("match_reason") or ""),
            extra={
                "event_id": (
                    f"smart_voice_reuse:{job_id}:"
                    f"{decision.speaker_id}:{decision.cloned_voice_id or ''}"
                ),
                "source": "smart_auto_voice_review",
                "matched_user_voice_id": decision.metrics.get("matched_user_voice_id"),
            },
        )
    except Exception:
        print(
            f"[smart] voice reuse usage audit failed speaker={decision.speaker_id}",
            flush=True,
        )
    # Task #27 (prereq for Task #26) — 2026-05-24 audit contract fix:
    # Don't hardcode reason_code or drop Phase 5 metrics. Three tiers
    # of REUSED decisions land here (strong same-source, strong_named,
    # Phase 5 possible_user_voice_match_auto_reused) and analytics
    # downstream needs to tell them apart from the on-disk JSONL.
    #
    # Before this fix:
    #   - reason_code was always "reused_user_voice" (overwriting Phase 5's
    #     own reason_code "possible_user_voice_match_auto_reused")
    #   - evidence missed auto_reused_from_possible_match /
    #     possible_match_count / top_candidate_* fields that Phase 5 set
    #     in decision.metrics
    #   - extra.voice_clone_decision was also hardcoded
    #
    # After:
    #   - reason_code = decision.reason_code (passthrough)
    #   - evidence carries Phase 5 fields (None for non-Phase-5 paths)
    #   - extra.voice_reuse_reason_code = decision.reason_code for
    #     readers that look at the extra block instead of evidence
    _is_possible_auto_reuse = bool(
        decision.metrics.get("auto_reused_from_possible_match", False)
    )
    _evidence: dict[str, Any] = {
        "voice_id": decision.cloned_voice_id,
        "clone_provider": decision.cloned_provider_name,
        "match_confidence": decision.metrics.get("match_confidence"),
        "match_reason": decision.metrics.get("match_reason"),
        "matched_user_voice_id": decision.metrics.get("matched_user_voice_id"),
    }
    if _is_possible_auto_reuse:
        # Only emit Phase 5 fields on the Phase 5 path so strong /
        # strong_named records don't accidentally carry None values
        # that downstream might interpret as "Phase 5 hit" (anything
        # not absent could be misread).
        _evidence["auto_reused_from_possible_match"] = True
        _evidence["possible_match_count"] = decision.metrics.get("possible_match_count")
        _evidence["top_candidate_voice_id"] = decision.metrics.get("top_candidate_voice_id")
        _evidence["top_candidate_label"] = decision.metrics.get("top_candidate_label")
        _evidence["top_candidate_match_scope"] = decision.metrics.get("top_candidate_match_scope")
        _evidence["top_candidate_confidence"] = decision.metrics.get("top_candidate_confidence")
    _emit_smart_audit(
        project_dir,
        decision_type="voice_clone",
        decision="approved",
        reason_code=decision.reason_code,
        evidence=_evidence,
        smart_decision_id=decision.smart_decision_id,
        extra={
            "speaker_id": decision.speaker_id,
            "job_id": job_id,
            "user_id": user_id,
            "voice_clone_decision": "reused_user_voice",
            "voice_reuse_reason_code": decision.reason_code,
        },
    )


def _build_quality_report_voice_decisions(
    decisions, per_speaker_seconds: dict[str, float] | None
) -> list[dict]:
    """Build the ``voice_decisions`` list for smart_quality_report.json.

    Task #27 (2026-05-24): extracted from inline builder so the
    reason_code passthrough + Phase 5 field preservation can be unit-
    tested. Symmetric to ``_apply_smart_reused_voice_decision``'s
    smart_decisions.jsonl emit — both audit files must surface the
    same distinguishing fields so Task #26 analytics can tell the
    three REUSED tiers (strong / strong_named / Phase 5
    possible_user_voice_match_auto_reused) apart.

    Schema:
      [{
        "speaker_id": str,
        "choice": "cloned" | "reused_user_voice" | "preset",
        "voice_id": str | None,
        "clone_provider": str | None,
        "sample_seconds": float | None,
        "smart_decision_id": str | None,
        # REUSED branch:
        "reason_code": str,         # passthrough from decision
        "match_confidence": str | None,
        "match_reason": str | None,
        "matched_user_voice_id": str | None,
        # Phase 5 REUSED only (auto_reused_from_possible_match=True):
        "auto_reused_from_possible_match": True,
        "possible_match_count": int | None,
        "top_candidate_confidence": str | None,
        "top_candidate_match_scope": str | None,
        # PRESET branch:
        "fallback_reason": str,     # decision.reason_code
      }]
    """
    from services.smart.auto_voice_review import VoiceReviewChoice

    per_speaker_seconds = per_speaker_seconds or {}
    out: list[dict] = []
    for dec in decisions:
        if dec.choice == VoiceReviewChoice.CLONED:
            choice = "cloned"
        elif dec.choice == VoiceReviewChoice.REUSED:
            choice = "reused_user_voice"
        else:
            choice = "preset"
        entry: dict[str, Any] = {
            "speaker_id": dec.speaker_id,
            "choice": choice,
            "voice_id": dec.cloned_voice_id,
            "clone_provider": dec.cloned_provider_name,
            "sample_seconds": per_speaker_seconds.get(dec.speaker_id),
            "smart_decision_id": dec.smart_decision_id,
        }
        if dec.choice not in (VoiceReviewChoice.CLONED, VoiceReviewChoice.REUSED):
            entry["fallback_reason"] = dec.reason_code
        if dec.choice == VoiceReviewChoice.REUSED:
            # Task #27: reason_code passthrough so strong / strong_named
            # / possible_auto can be told apart on disk.
            entry["reason_code"] = dec.reason_code
            entry["match_confidence"] = dec.metrics.get("match_confidence")
            entry["match_reason"] = dec.metrics.get("match_reason")
            entry["matched_user_voice_id"] = dec.metrics.get("matched_user_voice_id")
            if dec.metrics.get("auto_reused_from_possible_match"):
                # Phase 5 only: preserve metrics that let analytics
                # distinguish auto_reuse from strong.
                entry["auto_reused_from_possible_match"] = True
                entry["possible_match_count"] = dec.metrics.get("possible_match_count")
                entry["top_candidate_confidence"] = dec.metrics.get("top_candidate_confidence")
                entry["top_candidate_match_scope"] = dec.metrics.get("top_candidate_match_scope")
        out.append(entry)
    return out


def _emit_smart_quality_report(
    project_dir: Path,
    *,
    job_id: str,
    user_id: str,
    service_mode: str,
    smart_state_final: dict,
    speaker_summary: dict,
    voice_decisions: list[dict],
    translation_review: dict | None,
    retry_summary: dict,
    handoff_history: list[dict],
) -> bool:
    """Best-effort wrapper around
    ``services.smart.sidecar_emitter.write_smart_quality_report``.

    PR#3C-P3-a: collects the per-job decision data into the v1 payload
    shape (locked by docs/plans/2026-05-15-smart-mvp-p3-decisions.md
    §1) and delegates to the sidecar emitter for atomic write +
    schema_version stamping.

    Plan §6.4 末段: emit failure must NOT block the user-facing
    pipeline. Returns ``True`` on successful write, ``False`` on any
    failure (logged via print, mirroring _emit_smart_audit).

    Caller convention (see decision log §P3-a "Acceptance"):
      - Pass freshly-built sections; helper does not introspect
        process.py state.
      - For early-handoff jobs (eligibility / sample / quota),
        ``voice_decisions=[]`` and ``translation_review=None``.
      - ``handoff_history=[]`` for happy-path; non-empty when the job
        hit a downgrade.
      - ``retry_summary`` always populated; zeros when no retries
        occurred (P3-d will populate real numbers; P3-a writes zeros).
    """
    from datetime import datetime, timezone

    from services.smart.sidecar_emitter import write_smart_quality_report

    payload: dict[str, object] = {
        "job_id": job_id,
        "user_id": user_id,
        "service_mode": service_mode,
        "smart_state_final": dict(smart_state_final),
        "speaker_summary": dict(speaker_summary),
        "voice_decisions": list(voice_decisions),
        "translation_review": (
            dict(translation_review) if translation_review is not None
            else None
        ),
        "retry_summary": dict(retry_summary),
        "handoff_history": list(handoff_history),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        return bool(write_smart_quality_report(project_dir, payload))
    except Exception as _exc:
        print(
            f"[smart] quality_report emit failed (non-blocking): "
            f"{type(_exc).__name__}: {_exc}",
            flush=True,
        )
        return False


def _emit_smart_cost_summary(
    project_dir: Path,
    *,
    job_id: str,
    service_mode: str,
    minutes_processed: float,
    pending_credits_charged: int | None,
    credits_policy: str,
    asr_seconds: float,
    llm_translation_chars: int,
    tts_chars: int,
    voice_clone_calls: int,
    pending_minimax_quota_used_after: int | None,
) -> bool:
    """Best-effort wrapper around
    ``services.smart.sidecar_emitter.write_smart_cost_summary``.

    PR#3C-P3-b: per-job admin-only cost summary (decision log §2).
    Counterpart to quality_report — same audit/ directory, same
    schema_version stamping, same fail-safe semantics. Admin-only
    display per Codex Q2; user-facing workspace MUST NOT show this
    data (Gateway endpoint at ``/api/admin/jobs/{id}/cost`` is the
    single authoritative read path).

    Plan §6.4 末段 contract (mirrors quality_report): emit failure
    must NOT block the user-facing pipeline. Returns ``True`` on
    successful write, ``False`` on any failure (logged via print).

    Settle-dependent fields (``pending_credits_charged`` +
    ``pending_minimax_quota_used_after``) are unknown at pipeline
    terminal — they're set by Gateway's settle_job_credit_ledger
    AFTER pipeline completes. Pipeline writes ``None`` for these;
    a follow-up Gateway hook (P3-b-follow-up / Phase 2 backfill)
    updates the file post-settle.

    Codex 第三十六轮 P2: explicit ``pending_`` prefix prevents the
    admin UI from misreading ``credits_charged=None`` as "no credits
    charged"; the field name itself signals "settle hasn't happened
    yet". Renderer treats ``None`` value as "pending" UX state.
    """
    from datetime import datetime, timezone

    from services.smart.sidecar_emitter import write_smart_cost_summary

    payload: dict[str, object] = {
        "job_id": job_id,
        "service_mode": service_mode,
        "minutes_processed": minutes_processed,
        "pending_credits_charged": pending_credits_charged,
        "credits_policy": credits_policy,
        "cost_breakdown_internal_only": {
            "asr_seconds": asr_seconds,
            "llm_translation_chars": llm_translation_chars,
            "tts_chars": tts_chars,
            "voice_clone_calls": voice_clone_calls,
            "pending_minimax_quota_used_after": pending_minimax_quota_used_after,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        return bool(write_smart_cost_summary(project_dir, payload))
    except Exception as _exc:
        print(
            f"[smart] cost_summary emit failed (non-blocking): "
            f"{type(_exc).__name__}: {_exc}",
            flush=True,
        )
        return False


def _emit_smart_cost_summary_from_meter(
    project_dir: Path,
    *,
    job_id: str,
    usage_meter: object,
    minutes_processed: float,
    credits_policy: str,
) -> bool:
    """Codex 第三十六轮 P1: convenience wrapper that probes UsageMeter +
    delegates to ``_emit_smart_cost_summary``. Used at smart handoff
    return sites where the verbose meter-probe + emit boilerplate would
    repeat 7 times.

    Decision log §2 (post-fix wording): cost_summary is written for
    every smart job, regardless of completion — so handoff jobs get
    written here. ``pending_credits_charged`` /
    ``pending_minimax_quota_used_after`` stay ``None`` (Gateway settle
    runs after pipeline returns); ``credits_policy`` at handoff time
    is "pending_settle" until Gateway determines refund/capture/partial.

    Best-effort: meter probe failures + emit failures both swallow
    silently (print only). Cannot block the paused-result return.
    """
    _cs_asr_seconds = 0.0
    _cs_llm_chars = 0
    _cs_tts_chars = 0
    _cs_voice_clone_calls = 0
    try:
        _meter_summary = usage_meter.summarize()  # type: ignore[attr-defined]
        _cs_asr_seconds = float(
            _meter_summary.get("llm_audio_input_seconds") or 0.0
        )
        _cs_llm_chars = int(
            _meter_summary.get("s3_translation_llm_input_tokens") or 0
        )
        _cs_tts_chars = int(
            _meter_summary.get("tts_billed_chars") or 0
        )
        _cs_voice_clone_calls = int(
            _meter_summary.get("voice_clone_success_call_count") or 0
        )
    except Exception as _exc:
        print(
            f"[smart] cost_summary handoff meter probe failed "
            f"(non-blocking): {type(_exc).__name__}: {_exc}",
            flush=True,
        )

    return _emit_smart_cost_summary(
        project_dir,
        job_id=job_id,
        service_mode="smart",
        minutes_processed=round(minutes_processed, 3),
        pending_credits_charged=None,
        credits_policy=credits_policy,
        asr_seconds=round(_cs_asr_seconds, 3),
        llm_translation_chars=_cs_llm_chars,
        tts_chars=_cs_tts_chars,
        voice_clone_calls=_cs_voice_clone_calls,
        pending_minimax_quota_used_after=None,
    )


def _aggregate_speaker_dubbing_modes(segments) -> dict:
    """Codex 第四十轮 P2.4: aggregate segment-level ``dubbing_mode`` to
    speaker-level (the "PR#3C integration contract" flagged in
    ``services.smart.eligibility_gate`` docstring).

    Rule (per master plan §6.1 + eligibility_gate guidance):
      - all segments == keep_original → speaker is keep_original
      - all segments == mute_or_background → speaker is mute_or_background
      - otherwise (mixed, including any "dub") → speaker is "dub"

    Defensive:
      - segments without speaker_id are skipped
      - segments without dubbing_mode default to "dub" (most permissive
        — undershooting would leave audio holes)

    Returns: ``{speaker_id: aggregated_dubbing_mode}``. Consumed by
    ``_resolve_smart_minor_speaker_voices`` and (future) by the
    smart inline branch's eligibility evaluation.

    Pure function — no I/O, no side effects.
    """
    by_speaker: dict[str, set[str]] = {}
    for seg in segments or []:
        sp_id = getattr(seg, "speaker_id", None)
        if not sp_id:
            continue
        mode = getattr(seg, "dubbing_mode", None)
        if not mode:
            mode = "dub"
        by_speaker.setdefault(sp_id, set()).add(str(mode))

    result: dict[str, str] = {}
    for sp_id, modes in by_speaker.items():
        if modes == {"keep_original"}:
            result[sp_id] = "keep_original"
        elif modes == {"mute_or_background"}:
            result[sp_id] = "mute_or_background"
        else:
            result[sp_id] = "dub"
    return result


def _resolve_smart_minor_speaker_voices(
    *,
    speakers,
    main_speaker_ids,
    dubbing_mode_by_speaker=None,
) -> dict:
    """Smart MVP (2026-05-16, hardened Codex 第四十轮 P2.4): resolve
    preset voice IDs for non-main speakers via the existing
    ``auto_matched_voice`` field.

    Background: ``_smart_voice_review.decisions`` only covers main
    speakers (filtered by eligibility_gate). Without this helper,
    non-main speakers (low_share / rank > 3 / etc.) end up with
    ``segment.voice_id = None`` at TTS time — process.py:7113's
    "leave voice_id as-is (auto-match)" comment is aspirational, no
    actual auto-match runs at TTS time.

    Solution: ``vs_payload.speakers`` already carries
    ``auto_matched_voice`` for ALL speakers (computed at vs_payload
    build time by ``_build_voice_selection_review_payload`` calling
    ``services.tts.voice_match_resolver.resolve_voice_match`` per
    speaker, regardless of main/minor status). This helper extracts
    those for non-main speakers using the same strict-string
    contract as ``_resolve_preset_voice_id`` (Codex 第三十七轮 P2).

    Excluded:
      - Main speakers (already handled by voice_review.decisions)
      - ``dubbing_mode_by_speaker[sp_id] in {keep_original,
        mute_or_background}`` — user explicitly kept original;
        injecting a preset would override their choice. The
        aggregated dubbing_mode comes from ``_aggregate_speaker_dubbing_modes``
        (Codex 40 P2.4: cannot rely on ``sp.get("dubbing_mode")`` —
        ``_build_voice_selection_review_payload`` doesn't put that
        field on speaker entries).
      - Missing/empty ``auto_matched_voice`` — degraded shape, skip
        without crash; downstream TTS will see voice_id=None and
        use provider default

    Backward-compat: when ``dubbing_mode_by_speaker`` is None or
    missing a key, defaults to "dub" (= speaker gets a voice). This
    is intentional — undershooting (skipping a speaker that needed
    voice) leaves audio holes; overshooting (giving voice to a
    keep_original speaker) is corrected by TTS-stage
    ``is_keep_original_dubbing_mode`` filter that skips those
    segments anyway. The aggregation only fixes the audit /
    _speaker_voices pollution problem flagged by Codex 40.

    Returns: ``{speaker_id: voice_id_string}`` for each non-main,
    dub-able speaker with a valid auto-matched voice.

    Pure function — no I/O, no side effects.
    """
    _NON_DUBBED = {"keep_original", "mute_or_background"}
    main_set = set(main_speaker_ids or [])
    modes_map = dubbing_mode_by_speaker or {}
    result: dict[str, str] = {}
    for sp in speakers or []:
        if not isinstance(sp, dict):
            continue
        sp_id = sp.get("speaker_id")
        if not sp_id:
            continue
        if sp_id in main_set:
            continue
        # Codex 40 P2.4: read aggregated dubbing_mode from the dict
        # built by _aggregate_speaker_dubbing_modes. Missing → default
        # to "dub" (give voice; harmless if downstream TTS skips).
        aggregated_mode = modes_map.get(sp_id, "dub")
        if aggregated_mode in _NON_DUBBED:
            continue
        voice_id = _resolve_preset_voice_id(sp.get("auto_matched_voice"))
        if voice_id:
            result[sp_id] = voice_id
    return result


def _resolve_preset_voice_id(auto_matched_voice) -> str:
    """Codex 第三十七轮 Test Gap + P2: extract the bare ``voice_id``
    STRING from a PRESET decision's ``auto_matched_voice`` value.

    ``_auto_match_for_provider`` returns a dict
    ``{"voice_id": str, "label": str, "match_confidence": str,
    "backup_voices": [...]}`` (process.py:6509). The PRESET branch
    needs the bare string for ``_speaker_voices[speaker_id]`` so
    downstream TTS / voice-validation code can do
    ``voice_id.startswith("vt_")`` without crashing.

    Strict-string contract (Codex 第三十七轮 P2): only accept inner
    ``voice_id`` values that are ACTUAL strings — int / list / dict /
    arbitrary object all return "". Silent ``str()`` coercion would
    feed invalid voice IDs (``"123"`` / ``"['vt_x']"`` / etc.) into
    TTS, which then either errors cryptically or — worse — silently
    picks a fallback voice the user didn't choose.

    Defensive: dict / str / None / unknown all return str — never
    raises. Pure function so unit tests can validate behavior without
    standing up the full smart inline branch.
    """
    if isinstance(auto_matched_voice, dict):
        _vid = auto_matched_voice.get("voice_id")
        return _vid if isinstance(_vid, str) else ""
    if isinstance(auto_matched_voice, str):
        return auto_matched_voice
    return ""


def _aggregate_smart_retry_stats(
    *,
    segments,
    post_tts_budget_tracker,
    source_minutes: float,
) -> dict:
    """PR#3C-P3-d: build the smart quality_report ``retry_summary`` payload
    from real alignment-stage data (decision log §P3-d revised scope).

    Replaces the always-zero placeholder. Inputs come from the smart
    inline branch state at terminal time:

      - ``segments``: iterable of segment objects with optional
        ``pre_tts_rewrite_retry_attempted`` boolean attr. Pre-TTS
        rewrite retries (the obvious-overshoot one-pass guard) are
        counted here.
      - ``post_tts_budget_tracker``: optional ``PostTTSBudgetTracker``
        from aligner. Total re-TTS attempts come from
        ``tracker.usage_summary()['total_consumed']``.
      - ``source_minutes``: source duration; feeds
        ``retry_budget.compute_total_budget_minutes`` for the
        budget_remaining calculation.

    Output shape (matches decision log §1 ``retry_summary``)::

        {
            "rewrite_attempts_used": int,
            "retts_attempts_used": int,
            "budget_remaining_minutes": float (rounded 2dp),
        }

    ``budget_remaining_minutes`` is approximated: total budget formula
    (min(1.5 * minutes, minutes + 30)) minus consumed-minutes estimate.
    Since the tracker counts re-TTS COUNTS not durations, conservatively
    estimate 0.5 minutes per re-TTS — matches Smart MVP §6.3's "avg
    per-retry cost" intuition. Phase 2 (P3-d deep wire) can compute
    actual audio durations from tracker if needed.

    Pure: no I/O, no side effects.
    """
    from services.smart.retry_budget import compute_total_budget_minutes

    rewrite_attempts_used = 0
    for seg in (segments or []):
        if getattr(seg, "pre_tts_rewrite_retry_attempted", False):
            rewrite_attempts_used += 1

    retts_attempts_used = 0
    if post_tts_budget_tracker is not None:
        try:
            _summary = post_tts_budget_tracker.usage_summary()
            retts_attempts_used = int(_summary.get("total_consumed") or 0)
        except Exception:
            # Defensive: tracker shape issue must not crash the
            # quality_report emit. Fall back to zero.
            retts_attempts_used = 0

    total_budget_minutes = compute_total_budget_minutes(source_minutes)
    # Conservative consumption estimate: 0.5 minutes per re-TTS attempt.
    consumed_estimate_minutes = retts_attempts_used * 0.5
    budget_remaining_minutes = max(
        0.0, total_budget_minutes - consumed_estimate_minutes
    )

    return {
        "rewrite_attempts_used": rewrite_attempts_used,
        "retts_attempts_used": retts_attempts_used,
        "budget_remaining_minutes": round(budget_remaining_minutes, 2),
    }


def _emit_smart_budget_exhausted_events(
    *,
    project_dir: Path,
    post_tts_budget_tracker,
    job_id: str,
    user_id: str,
) -> int:
    """PR#3C-P3-d: emit one ``budget_exhausted`` sidecar event per
    exhausted root segment in the alignment-stage tracker.

    Decision log §6.3 + §P3-d revised scope: when a segment hits its
    per-root re-TTS cap, smart's audit trail must record which segment
    exhausted budget so admin diagnostics + P3-c renderer's "retry
    history" panel can show what happened.

    Each event is one line in ``audit/smart_decisions.jsonl`` with::

        decision_type    = "budget_exhausted"
        decision         = "rejected"         # cap reached — further retries rejected
        reason_code      = "post_tts_per_segment_cap_exhausted"
        evidence         = {"root_segment_id": ..., "consumed": N, "cap": N}
        extra            = {"job_id": ..., "user_id": ..., "stage": "alignment"}

    Returns count of events emitted (0 when no roots exhausted /
    tracker is None / emit fails).

    Best-effort: failures swallow silently (plan §6.4 末段).
    """
    if post_tts_budget_tracker is None:
        return 0

    try:
        summary = post_tts_budget_tracker.usage_summary()
    except Exception as _exc:
        print(
            f"[smart] budget_exhausted scan tracker.usage_summary() "
            f"failed (non-blocking): {type(_exc).__name__}: {_exc}",
            flush=True,
        )
        return 0

    exhausted = summary.get("exhausted_root_ids") or []
    if not exhausted:
        return 0

    cap = int(summary.get("cap") or 0)
    consumed_roots = summary.get("consumed_roots") or {}

    emitted = 0
    for root_id in exhausted:
        try:
            _emit_smart_audit(
                project_dir,
                decision_type="budget_exhausted",
                decision="rejected",
                reason_code="post_tts_per_segment_cap_exhausted",
                evidence={
                    "root_segment_id": int(root_id),
                    "consumed": int(consumed_roots.get(root_id) or 0),
                    "cap": cap,
                },
                extra={
                    "job_id": job_id,
                    "user_id": user_id,
                    "stage": "alignment",
                },
            )
            emitted += 1
        except Exception as _exc:
            print(
                f"[smart] budget_exhausted emit for root={root_id} "
                f"failed (non-blocking): "
                f"{type(_exc).__name__}: {_exc}",
                flush=True,
            )
    return emitted


def _emit_smart_audit(
    project_dir: Path,
    *,
    decision_type: str,
    decision: str,
    reason_code: str | None = None,
    evidence: dict | None = None,
    smart_decision_id: str | None = None,
    extra: dict | None = None,
) -> None:
    """Best-effort wrapper around ``services.smart.sidecar_emitter.emit_smart_decision``.

    PR#3C-b3f sidecar instrumentation: every smart decision point (eligibility
    gate, translation auto-review, voice review batch, per-speaker clone
    decisions, downgrade handoffs) appends one line to
    ``{project_dir}/audit/smart_decisions.jsonl`` so the QA report renderer +
    admin tooling can reconstruct WHAT smart decided and WHY.

    Plan §6.4 末段 contract: sidecar emit failures MUST NOT block the
    user-facing pipeline. ``emit_smart_decision`` itself returns False on
    I/O failure (logged internally). This wrapper additionally catches
    ValueError (enum typo / missing required arg) so a programmer bug at
    a call site can't crash a smart job — the bug surfaces in test
    output instead.

    Auto-generates ``smart_decision_id`` (uuid4 hex) when caller doesn't
    pass one. Sets ``created_at`` to current UTC ISO 8601.

    Args:
      project_dir: project root; sidecar lives at audit/smart_decisions.jsonl
      decision_type: one of services.smart.sidecar_emitter._ALLOWED_DECISION_TYPES
        (speaker_gate / voice_clone / voice_selection_auto_approve /
        translation_auto_approve / tts_retry / split_proposal /
        downgrade_handoff / budget_exhausted)
      decision: "approved" / "rejected" / "deferred"
      reason_code: required when decision != "approved"; None otherwise
      evidence: free-form dict of metrics that drove the decision
        (main_speaker_count, glossary_rate, sample_duration_s, …)
      smart_decision_id: optional explicit id (e.g. when piping through
        a per-speaker VoiceReviewDecision.smart_decision_id from
        auto_voice_review for audit linkage)
      extra: extra top-level fields (e.g. speaker_id, job_id, user_id,
        handoff_stage). Won't clobber required schema fields.
    """
    import uuid as _uuid_mod
    from datetime import datetime, timezone

    from services.smart.sidecar_emitter import emit_smart_decision

    try:
        emit_smart_decision(
            project_dir,
            decision_type=decision_type,
            decision=decision,
            reason_code=reason_code,
            evidence=evidence or {},
            smart_decision_id=smart_decision_id or _uuid_mod.uuid4().hex,
            created_at=datetime.now(timezone.utc).isoformat(),
            auto_approved=(decision == "approved"),
            extra=extra,
        )
    except Exception as _exc:
        # ValueError on enum typo OR any other programming bug. Log
        # but don't crash — audit sidecar is informational.
        # process.py uses ``print`` for diagnostic output throughout
        # (no module-level logger configured); follow the convention.
        print(
            f"[smart] sidecar emit failed (call-site bug?): "
            f"decision_type={decision_type!r} decision={decision!r} "
            f"err={type(_exc).__name__}: {_exc}",
            flush=True,
        )


def _register_smart_clone_in_user_voices(
    *,
    user_id: str,
    voice_id: str,
    label: str,
    source_speaker_id: str | None = None,
    source_job_id: str | None = None,
    source_type: str | None = None,
    source_ref: str | None = None,
    source_content_hash: str | None = None,
    source_video_title: str | None = None,
    source_published_at: str | None = None,
    source_content_summary: str | None = None,
    source_content_era: str | None = None,
    source_content_tags: object | None = None,
    source_speaker_name: str | None = None,
    clone_sample_seconds: float | None = None,
    clone_sample_segment_ids: object | None = None,
    smart_clone_reservation_id: str | None = None,
    created_from: str | None = "smart_auto",
    notes: str | None = None,
    reservation_id: str | None = None,
    task_id: str | None = None,
) -> bool:
    """Mirror a smart-path clone into the Gateway UserVoice table.

    P3e §5: when ``reservation_id`` (+ ``task_id``) is present (智能版预览
    克隆，已预扣 600 reservation），POST 到 **register-billed** endpoint
    （原子写 durable billing event + 入库，钱-正确性核心）而非 register-smart。
    无 reservation（既有 full smart auto-clone / flag off）→ 仍走 register-smart
    （只入库、不计费）。两端点字段 parity（rich source_* 全透传）。

    Returns ``True`` on success (HTTP 200, ok:true), ``False`` on
    any failure.

    Codex 第二十九轮 P0: Smart inline auto-approve uses the Protocol-
    based ``_MiniMaxCloneAdapter`` which only calls MiniMax — it does
    NOT write to Gateway's UserVoice table the way Studio's manual
    voice-clone path does. Without this mirror, the
    ``/api/internal/user-voices/quota`` endpoint sees stale ``used``
    counts across jobs and §7.3 water mark stops protecting against
    voice library overflow.

    This helper closes the loop: after a successful Smart CLONED
    decision, the caller invokes us to register the new voice_id
    in UserVoice. Subsequent quota lookups see the updated count.

    Failure semantics — caller (process.py smart branch) treats
    False as "mirror failed, can't trust quota":
      - The MiniMax voice already exists (and was paid for)
      - Gateway hasn't seen it, so next job's quota will be stale
      - Process.py escalates to handoff so the user is aware

    NEVER raises — failures are returned as False so the caller's
    aggregation logic stays simple.
    """
    import os
    import requests  # type: ignore[import-not-found]

    user_id = (user_id or "").strip()
    voice_id = (voice_id or "").strip()
    if not user_id or not voice_id:
        return False
    reservation_id = (smart_clone_reservation_id or "").strip()
    task_id = (source_job_id or "").strip()
    if reservation_id and not task_id:
        return False
    api_key = os.environ.get("AVT_INTERNAL_API_KEY", "").strip()
    if not api_key:
        return False
    payload: dict[str, object] = {
        "user_id": user_id,
        "voice_id": voice_id,
        "label": label or voice_id,
    }
    if reservation_id:
        payload["task_id"] = task_id
        payload["reservation_id"] = reservation_id
    if source_speaker_id:
        payload["source_speaker_id"] = source_speaker_id
    if source_job_id:
        payload["source_job_id"] = source_job_id
    if source_type:
        payload["source_type"] = source_type
    if source_ref:
        payload["source_ref"] = source_ref
    if source_content_hash:
        payload["source_content_hash"] = source_content_hash
    if source_video_title:
        payload["source_video_title"] = source_video_title
    if source_published_at:
        payload["source_published_at"] = source_published_at
    if source_content_summary:
        payload["source_content_summary"] = source_content_summary
    if source_content_era:
        payload["source_content_era"] = source_content_era
    if source_content_tags is not None:
        payload["source_content_tags"] = source_content_tags
    if source_speaker_name:
        payload["source_speaker_name"] = source_speaker_name
    if clone_sample_seconds is not None:
        payload["clone_sample_seconds"] = clone_sample_seconds
    if clone_sample_segment_ids is not None:
        payload["clone_sample_segment_ids"] = clone_sample_segment_ids
    if created_from:
        payload["created_from"] = created_from
    if notes:
        payload["notes"] = notes
    # P3e §5：reservation 在场 → register-billed（原子写 billing event + 入库，
    # 钱-正确性核心，含 task_id + reservation_id 供 gateway 行锁校验 reservation
    # status=reserved 且属本 task）；否则 register-smart（既有，只入库不计费）。
    # rich source_* 字段两端点都透传（字段 parity）。
    _reservation_id = (reservation_id or "").strip()
    _task_id = (task_id or "").strip()
    _is_billed = bool(_reservation_id and _task_id)
    if _is_billed:
        _endpoint = "http://127.0.0.1:8880/api/internal/smart-clone/register-billed"
        payload["reservation_id"] = _reservation_id
        payload["task_id"] = _task_id
    else:
        _endpoint = "http://127.0.0.1:8880/api/internal/user-voices/register-smart"
        # 钱-可见性（对抗性复核 V2）：reservation_id 在场但 task_id 缺失（不一致
        # 状态）→ 静默降级到 register-smart（不写 billing event）→ finalizer release
        # → 业务白克隆。这种本不该发生（reservation 在场必有 job_id），loud log
        # 供 ops 排查。注意：fail-safe 方向（用户**不**被扣，只是业务漏收）。
        if _reservation_id and not _task_id:
            print(
                f"[smart][MONEY] reservation present ({_reservation_id}) but task_id "
                f"empty — routing to register-smart (NO billing event). voice={voice_id}. "
                f"finalizer will release; business eats clone cost. Needs reconcile.",
                flush=True,
            )
    try:
        resp = requests.post(
            _endpoint,
            json=payload,
            headers={"X-Internal-Key": api_key},
            timeout=5.0,
        )
        if resp.status_code != 200:
            # 钱-可见性（对抗性复核 V2）：register-billed 失败（如 409
            # no_active_reservation：reservation 失效/不属本 task）→ MiniMax 已克隆
            # 但未写 billing event → finalizer release（用户**不**被扣，fail-safe）
            # → 业务白克隆 + 留下孤儿 MiniMax voice。loud log 供 ops 对账 +
            # best-effort 清理（自动删 voice 待后续接 MiniMax client，本期 follow-up）。
            if _is_billed:
                print(
                    f"[smart][MONEY] register-billed FAILED status={resp.status_code} "
                    f"task={_task_id} reservation={_reservation_id} voice={voice_id} "
                    f"— clone done but NOT billed; finalizer will release. Needs reconcile.",
                    flush=True,
                )
            return False
        data = resp.json()
    except Exception as _reg_exc:
        if _is_billed:
            print(
                f"[smart][MONEY] register-billed EXCEPTION task={_task_id} "
                f"reservation={_reservation_id} voice={voice_id}: "
                f"{type(_reg_exc).__name__} — clone done but NOT billed; finalizer "
                f"will release. Needs reconcile.",
                flush=True,
            )
        return False
    return bool(data.get("ok"))


def _check_smart_clone_reservation_active(
    *,
    user_id: str,
    task_id: str,
    reservation_id: str,
) -> bool:
    """Return True only when Gateway confirms this reservation can fund a clone."""
    import os
    import requests  # type: ignore[import-not-found]

    user_id = (user_id or "").strip()
    task_id = (task_id or "").strip()
    reservation_id = (reservation_id or "").strip()
    if not user_id or not task_id or not reservation_id:
        return False
    api_key = os.environ.get("AVT_INTERNAL_API_KEY", "").strip()
    if not api_key:
        return False
    try:
        resp = requests.post(
            "http://127.0.0.1:8880/api/internal/smart-clone/reservations/check-active",
            json={
                "user_id": user_id,
                "task_id": task_id,
                "reservation_id": reservation_id,
            },
            headers={"X-Internal-Key": api_key},
            timeout=5.0,
        )
        if resp.status_code != 200:
            return False
        data = resp.json()
    except Exception:
        return False
    return bool(data.get("ok") and data.get("active"))


def _smart_reservation_gate_open(
    requires_reservation: bool, reservation_id: object
) -> bool:
    """P3e §2 智能版克隆 reservation 收紧闸（纯函数，钱-critical，可单测）。

    - ``requires_reservation=False``（admin 默认）→ **恒 open**：既有 smart
      auto-clone 行为完全不变（不要求 reservation）。
    - ``True`` → 仅当带**非空** ``reservation_id`` 才 open；否则闸关 →
      pipeline 选 provider 时不接真 MiniMax（只 preset / reuse），封死现在
      full smart 无 reservation 调付费 provider 的漏收（plan §2）。

    注意：本函数只判 reservation_id **是否存在**；其「真有效（status=reserved
    且属本 task）」由 gateway register-billed endpoint 写 billing event 时**原子
    再校验**（pipeline 不碰钱）。
    """
    if not requires_reservation:
        return True
    return bool(str(reservation_id or "").strip())


def _is_valid_speaker_id(value: object) -> bool:
    return isinstance(value, str) and _SPEAKER_ID_PATTERN.match(value.strip()) is not None


def _default_speaker_display_name(speaker_id: str) -> str:
    normalized_speaker_id = speaker_id.strip().lower()
    if normalized_speaker_id == "speaker_a":
        return "Speaker A"
    if normalized_speaker_id == "speaker_b":
        return "Speaker B"
    if normalized_speaker_id.startswith("speaker_"):
        suffix = normalized_speaker_id.replace("speaker_", "")
        if len(suffix) == 1 and suffix.isalpha():
            return f"Speaker {suffix.upper()}"
    return speaker_id


def _build_cloned_voice_label(speaker_name: str | None) -> str:
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    name = (speaker_name or "").strip() or "Speaker"
    when = datetime.now(timezone.utc)
    try:
        when = when.astimezone(ZoneInfo("Asia/Shanghai"))
    except Exception:
        when = when.astimezone(timezone(timedelta(hours=8)))
    timestamp = when.strftime("%Y-%m-%d %H:%M")
    max_name_len = max(1, 200 - len(" · ") - len(timestamp))
    if len(name) > max_name_len:
        name = name[:max_name_len].rstrip()
    return f"{name} · {timestamp}"


def _merge_speaker_name_map(
    review_speaker_names: dict[str, str] | None,
    speaker_name_a: str,
    speaker_name_b: str,
) -> dict[str, str]:
    merged = dict(review_speaker_names or {})
    # S2 review names take precedence; speaker_name_a/b are fallbacks only.
    # In multi-speaker (>2) mode, speaker_name_b_is_placeholder is always
    # False so speaker_name_b stays at "Speaker B" — using setdefault
    # ensures the S2-identified name is not overwritten.
    merged.setdefault("speaker_a", speaker_name_a)
    merged.setdefault("speaker_b", speaker_name_b)
    return merged


def _internal_request_headers() -> dict[str, str]:
    """Build HTTP headers for pipeline → gateway internal callbacks.

    Gateway endpoints under /job-api/jobs/{id}/source-metadata, /metering,
    and /internal/user-voices/* are protected by X-Internal-Key (P0-1, P0-2a
    audit fixes, 2026-05-07). All pipeline callers must inject the key.

    If AVT_INTERNAL_API_KEY is unset (dev / misconfig), fall back to
    Content-Type only — the request will 403 but the caller's outer
    try/except already swallows it and prints a warning. Job API has its
    own startup gate (P0-2c) so this only fires if env is set on Job API
    but missing on the pipeline subprocess.
    """
    headers = {"Content-Type": "application/json"}
    internal_key = os.environ.get("AVT_INTERNAL_API_KEY", "").strip()
    if internal_key:
        headers["X-Internal-Key"] = internal_key
    return headers


def _report_source_metadata(
    job_id: str,
    duration_seconds: float | None = None,
    title: str | None = None,
    display_name: str | None = None,
    *,
    stage_label: str = "S0",
) -> None:
    """Best-effort callback to Gateway /job-api/jobs/{job_id}/source-metadata."""
    import urllib.error
    import urllib.request
    gateway_base = os.environ.get("AVT_GATEWAY_URL", "http://localhost:8880")
    url = f"{gateway_base}/job-api/jobs/{job_id}/source-metadata"
    body: dict = {}
    if duration_seconds is not None:
        body["source_duration_seconds"] = duration_seconds
    if title:
        body["title"] = title
    if display_name:
        body["display_name"] = display_name
    if not body:
        return
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=_internal_request_headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            print(f"[{stage_label}] Reported source metadata to gateway: {resp.status}", flush=True)
    except urllib.error.HTTPError as e:
        if e.code == 402 and duration_seconds is not None:
            try:
                error_body = json.loads(e.read().decode("utf-8", errors="replace"))
                message = error_body.get("message") or error_body.get("error")
            except Exception:
                message = None
            raise RuntimeError(message or "点数不足，任务已停止。") from e
        print(f"[{stage_label}] Warning: failed to report source metadata: {e}", flush=True)
    except Exception as e:
        print(f"[{stage_label}] Warning: failed to report source metadata: {e}", flush=True)


def _build_job_metering_payload(
    segments: list,
    *,
    tts_billed_chars: int | None = None,
    glossary: dict[str, str] | None = None,
    extra_metering: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build Gateway job metering fields from real segment objects.

    Supports both ``DubbingSegment`` (real pipeline path) and ``SemanticBlock``
    (legacy/alternative path) by checking for available text fields.

    Text field: ``cn_text`` (DubbingSegment) or ``merged_cn_text`` (SemanticBlock).

    Reports (V3-4 baseline + V3-5 partial + Phase 2 Task 0 metrics):
    - final_cn_chars: total Chinese characters in final translated text
    - rewrite_triggered: whether any segment had rewrite_count > 0
    - rewrite_count: total rewrite operations across all segments
    - tts_billed_chars: total chars submitted to TTS provider (from TTSResult.billed_chars)
    - **Phase 2 Task 0**:
      - total_segments: int
      - catalog_hit_count: how many segments had a voice with catalog cps lookup
      - catalog_hit_rate: catalog_hit_count / total_segments
      - skip_probe: whether the entire job skipped probe TTS calibration (all-or-nothing)
      - first_pass_error_pct_avg: avg of |first_pass_error_pct| across segments with valid value
      - first_pass_error_pct_p50/p90: percentiles of |first_pass_error_pct|
      - needs_review_count: segments with needs_review=True (post-alignment)
      - needs_review_rate: needs_review_count / total_segments
      - alignment_method_distribution: counts by method (direct/dsp/rewrite/force_dsp)
      - speed_param_distribution: counts by speed param bucket (1.0 / [0.92,1.08] / outside) — Task 1 will populate
      - term_preservation_rate: glossary terms appearing in final translation / total terms
      - missing_glossary_terms: list (≤20) of Chinese terms that were dropped
      - speaker_*: deterministic P2 speaker-structure summary for incidental
        and fragmented low-share speakers
    """
    total_cn_chars = 0
    total_rewrite_count = 0
    total_segments = 0
    catalog_hit_count = 0
    needs_review_count = 0
    first_pass_errors_abs: list[float] = []
    method_counts: dict[str, int] = {}
    speed_counts: dict[str, int] = {"1.0": 0, "in_range": 0, "outside": 0}
    pre_tts_rewrite_events: list[dict[str, object]] = []
    pre_tts_rewrite_rejected_events: list[dict[str, object]] = []
    pre_tts_rewrite_rejected_reason_counts: dict[str, int] = {}
    micro_segment_count = 0
    short_segment_count = 0
    short_segment_needs_review_count = 0
    short_segment_force_dsp_count = 0
    capped_dsp_overflow_count = 0
    capped_dsp_underflow_count = 0
    dsp_silence_pad_segment_count = 0
    dsp_silence_padded_total_ms = 0
    dsp_silence_padded_max_ms = 0
    short_segment_capped_dsp_overflow_count = 0
    force_dsp_severity_counts: dict[str, int] = {}
    force_dsp_review_suppressed_count = 0
    short_merge_candidate_count = 0
    short_merge_blocked_cross_speaker_count = 0
    short_merge_applied_count = 0
    short_merge_absorbed_count = 0
    auto_keep_original_count = 0
    auto_keep_original_reason_counts: dict[str, int] = {}
    short_content_compact_attempted_count = 0
    short_content_compact_accepted_count = 0
    short_content_compact_rejected_count = 0
    short_content_compact_rejected_reason_counts: dict[str, int] = {}
    short_content_compact_class_counts: dict[str, int] = {}
    speaker_structure_profiles: dict[str, dict[str, object]] = {}

    for seg in segments:
        total_segments += 1
        speaker_id = str(getattr(seg, "speaker_id", "") or "")
        if speaker_id:
            role = str(getattr(seg, "speaker_role", "") or "")
            existing_profile = speaker_structure_profiles.get(speaker_id)
            if existing_profile is None or (
                not str(existing_profile.get("speaker_role", "") or "") and role
            ):
                speaker_structure_profiles[speaker_id] = {
                    "speaker_role": role,
                    "speaker_role_label": str(getattr(seg, "speaker_role_label", "") or ""),
                    "duration_ms": int(getattr(seg, "speaker_duration_ms", 0) or 0),
                    "duration_share": round(
                        float(getattr(seg, "speaker_duration_share", 0.0) or 0.0),
                        4,
                    ),
                    "segment_count": int(getattr(seg, "speaker_segment_count", 0) or 0),
                    "short_segment_count": int(
                        getattr(seg, "speaker_short_segment_count", 0) or 0
                    ),
                    "short_segment_rate": round(
                        float(getattr(seg, "speaker_short_segment_rate", 0.0) or 0.0),
                        4,
                    ),
                    "reason": str(getattr(seg, "speaker_structure_reason", "") or ""),
                    "review_hint": str(getattr(seg, "speaker_review_hint", "") or ""),
                }
        text = getattr(seg, "cn_text", "") or ""
        if not text:
            text = getattr(seg, "merged_cn_text", "") or ""
        total_cn_chars += len(text)
        total_rewrite_count += getattr(seg, "rewrite_count", 0)

        # Phase 2 Task 0 — per-segment metric collection (best-effort:
        # missing attributes are treated as defaults so that legacy paths
        # like SemanticBlock continue to work without raising).
        if getattr(seg, "catalog_hit", False):
            catalog_hit_count += 1
        if getattr(seg, "needs_review", False):
            needs_review_count += 1

        method = getattr(seg, "alignment_method", "") or ""
        if method:
            method_counts[method] = method_counts.get(method, 0) + 1
        if method == "capped_dsp_overflow":
            capped_dsp_overflow_count += 1
        if method == "capped_dsp_underflow":
            capped_dsp_underflow_count += 1
        pad_ms = int(getattr(seg, "dsp_silence_padded_ms", 0) or 0)
        if pad_ms > 0:
            dsp_silence_pad_segment_count += 1
            dsp_silence_padded_total_ms += pad_ms
            dsp_silence_padded_max_ms = max(dsp_silence_padded_max_ms, pad_ms)
        if method in {"force_dsp", "capped_dsp_overflow", "capped_dsp_underflow"}:
            severity = getattr(seg, "force_dsp_severity", "") or "unknown"
            force_dsp_severity_counts[severity] = (
                force_dsp_severity_counts.get(severity, 0) + 1
            )
            if getattr(seg, "force_dsp_review_suppressed", False):
                force_dsp_review_suppressed_count += 1
        if getattr(seg, "short_merge_candidate", False):
            short_merge_candidate_count += 1
        if getattr(seg, "short_merge_blocked_reason", "") == "cross_speaker_adjacent":
            short_merge_blocked_cross_speaker_count += 1
        if getattr(seg, "short_merge_applied", False):
            short_merge_applied_count += 1
            short_merge_absorbed_count += len(
                ProcessPipeline._parse_short_merge_absorbed_segment_ids(seg)
            )
        auto_keep_reason = str(
            getattr(seg, "auto_keep_original_reason", "") or ""
        )
        if auto_keep_reason:
            auto_keep_original_count += 1
            auto_keep_original_reason_counts[auto_keep_reason] = (
                auto_keep_original_reason_counts.get(auto_keep_reason, 0) + 1
            )
        if getattr(seg, "short_content_compact_attempted", False):
            short_content_compact_attempted_count += 1
            compact_class = str(
                getattr(seg, "short_content_compact_class", "") or "unknown"
            )
            short_content_compact_class_counts[compact_class] = (
                short_content_compact_class_counts.get(compact_class, 0) + 1
            )
            if getattr(seg, "short_content_compact_accepted", False):
                short_content_compact_accepted_count += 1
            else:
                short_content_compact_rejected_count += 1
                compact_reason = str(
                    getattr(seg, "short_content_compact_rejected_reason", "")
                    or "unknown"
                )
                short_content_compact_rejected_reason_counts[compact_reason] = (
                    short_content_compact_rejected_reason_counts.get(compact_reason, 0)
                    + 1
                )

        target_duration_ms = int(getattr(seg, "target_duration_ms", 0) or 0)
        if 0 < target_duration_ms < 1_000:
            micro_segment_count += 1
        if PRE_TTS_REWRITE_SHORT_MIN_TARGET_MS <= target_duration_ms < PRE_TTS_REWRITE_MIN_TARGET_MS:
            short_segment_count += 1
            if getattr(seg, "needs_review", False):
                short_segment_needs_review_count += 1
            if method == "force_dsp":
                short_segment_force_dsp_count += 1
            if method == "capped_dsp_overflow":
                short_segment_capped_dsp_overflow_count += 1

        err = getattr(seg, "first_pass_error_pct", None)
        if err is not None and err != 0.0:
            first_pass_errors_abs.append(abs(float(err)))

        speed = getattr(seg, "dsp_speed_param", 1.0) or 1.0
        speed = float(speed)
        if abs(speed - 1.0) < 1e-6:
            speed_counts["1.0"] += 1
        elif 0.92 <= speed <= 1.08:
            speed_counts["in_range"] += 1
        else:
            speed_counts["outside"] += 1

        pre_tts_direction = getattr(seg, "pre_tts_rewrite_direction", "") or ""
        if pre_tts_direction:
            pre_tts_rewrite_events.append({
                "segment_id": getattr(seg, "segment_id", None),
                "direction": pre_tts_direction,
                "task": getattr(seg, "pre_tts_rewrite_task", "") or "s5_rewrite",
                "estimate_ms": getattr(seg, "pre_tts_estimate_ms", 0) or 0,
                "target_ms": getattr(seg, "pre_tts_target_ms", 0) or 0,
                "pre_chars": getattr(seg, "pre_tts_pre_chars", 0) or 0,
                "post_chars": getattr(seg, "pre_tts_post_chars", 0) or 0,
                "post_tts_first_pass_ms": (
                    getattr(seg, "pre_tts_post_tts_first_pass_ms", 0) or 0
                ),
                "contradiction": bool(getattr(seg, "pre_tts_contradiction", False)),
                "harmful_contradiction": bool(
                    getattr(seg, "pre_tts_harmful_contradiction", False)
                ),
                "retry_attempted": bool(
                    getattr(seg, "pre_tts_rewrite_retry_attempted", False)
                ),
                "retry_accepted": bool(
                    getattr(seg, "pre_tts_rewrite_retry_accepted", False)
                ),
                "initial_rejected_reason": (
                    getattr(seg, "pre_tts_rewrite_initial_rejected_reason", "") or ""
                ),
            })
        if getattr(seg, "pre_tts_rewrite_rejected", False):
            reason = str(
                getattr(seg, "pre_tts_rewrite_rejected_reason", "") or "unknown"
            )
            pre_tts_rewrite_rejected_reason_counts[reason] = (
                pre_tts_rewrite_rejected_reason_counts.get(reason, 0) + 1
            )
            pre_tts_rewrite_rejected_events.append({
                "segment_id": getattr(seg, "segment_id", None),
                "direction": (
                    getattr(seg, "pre_tts_rewrite_rejected_direction", "") or ""
                ),
                "reason": reason,
                "estimate_ms": (
                    getattr(seg, "pre_tts_rewrite_rejected_estimate_ms", 0) or 0
                ),
                "target_ms": (
                    getattr(seg, "pre_tts_rewrite_rejected_target_ms", 0) or 0
                ),
                "pre_chars": (
                    getattr(seg, "pre_tts_rewrite_rejected_pre_chars", 0) or 0
                ),
                "post_chars": (
                    getattr(seg, "pre_tts_rewrite_rejected_post_chars", 0) or 0
                ),
                "lower_chars": (
                    getattr(seg, "pre_tts_rewrite_rejected_lower_chars", 0) or 0
                ),
                "upper_chars": (
                    getattr(seg, "pre_tts_rewrite_rejected_upper_chars", 0) or 0
                ),
                "retry_attempted": bool(
                    getattr(seg, "pre_tts_rewrite_retry_attempted", False)
                ),
            })

    body: dict = {
        "final_cn_chars": total_cn_chars,
        "rewrite_triggered": total_rewrite_count > 0,
        "rewrite_count": total_rewrite_count,
        # --- Phase 2 Task 0 fields ---
        "total_segments": total_segments,
        "catalog_hit_count": catalog_hit_count,
        "catalog_hit_rate": (
            round(catalog_hit_count / total_segments, 4)
            if total_segments > 0 else 0.0
        ),
        # all-or-nothing: skip_probe is true iff every segment hit the catalog
        "skip_probe": (total_segments > 0 and catalog_hit_count == total_segments),
        "needs_review_count": needs_review_count,
        "needs_review_rate": (
            round(needs_review_count / total_segments, 4)
            if total_segments > 0 else 0.0
        ),
        "micro_segment_count": micro_segment_count,
        "short_segment_count": short_segment_count,
        "short_segment_needs_review_count": short_segment_needs_review_count,
        "short_segment_force_dsp_count": short_segment_force_dsp_count,
        "capped_dsp_overflow_count": capped_dsp_overflow_count,
        "capped_dsp_underflow_count": capped_dsp_underflow_count,
        "dsp_silence_pad_segment_count": dsp_silence_pad_segment_count,
        "dsp_silence_padded_total_ms": dsp_silence_padded_total_ms,
        "dsp_silence_padded_max_ms": dsp_silence_padded_max_ms,
        "short_segment_capped_dsp_overflow_count": short_segment_capped_dsp_overflow_count,
        "force_dsp_severity_distribution": force_dsp_severity_counts,
        "force_dsp_review_suppressed_count": force_dsp_review_suppressed_count,
        "short_merge_candidate_count": short_merge_candidate_count,
        "short_merge_blocked_cross_speaker_count": short_merge_blocked_cross_speaker_count,
        "short_merge_applied_count": short_merge_applied_count,
        "short_merge_absorbed_count": short_merge_absorbed_count,
        "auto_keep_original_count": auto_keep_original_count,
        "auto_keep_original_reason_distribution": auto_keep_original_reason_counts,
        "short_content_compact_attempted_count": short_content_compact_attempted_count,
        "short_content_compact_accepted_count": short_content_compact_accepted_count,
        "short_content_compact_rejected_count": short_content_compact_rejected_count,
        "short_content_compact_rejected_reason_distribution": (
            short_content_compact_rejected_reason_counts
        ),
        "short_content_compact_class_distribution": short_content_compact_class_counts,
        "alignment_method_distribution": method_counts,
        "speed_param_distribution": speed_counts,
    }
    if speaker_structure_profiles:
        role_counts: dict[str, int] = {}
        incidental_share_total = 0.0
        for profile in speaker_structure_profiles.values():
            role = str(profile.get("speaker_role", "") or "unknown")
            role_counts[role] = role_counts.get(role, 0) + 1
            if role == "incidental":
                incidental_share_total += float(profile.get("duration_share", 0.0) or 0.0)
        body["speaker_count"] = len(speaker_structure_profiles)
        body["speaker_role_distribution"] = role_counts
        body["speaker_primary_count"] = role_counts.get("primary", 0)
        body["speaker_incidental_count"] = role_counts.get("incidental", 0)
        body["speaker_fragmented_count"] = role_counts.get("fragmented", 0)
        body["speaker_non_speech_count"] = role_counts.get("non_speech", 0)
        body["speaker_incidental_duration_share"] = round(incidental_share_total, 4)
        body["speaker_structure_profiles"] = speaker_structure_profiles

    # voice_speed_mismatch_rate: fraction of segments whose voice cps
    # deviates >15% from target (= source_english_wps × 1.8). Segments
    # without target or voice cps are excluded from the denominator.
    try:
        mismatch_count = 0
        mismatch_denom = 0
        for seg in segments:
            target_cps_val = float(getattr(seg, "target_chars_per_second", 0) or 0)
            if target_cps_val <= 0:
                continue
            # Use the voice's catalog/user_voices cps (via the probe-calibrated
            # per-speaker value that was piped into the segment).
            voice_cps_text = seg.cn_text if hasattr(seg, "cn_text") else ""
            actual_dur = float(getattr(seg, "actual_duration_ms", 0) or 0)
            speed_param = float(getattr(seg, "dsp_speed_param", 1.0) or 1.0)
            if actual_dur <= 0 or not voice_cps_text:
                continue
            # Compute voice's natural cps (normalize out speed adjustment)
            natural_dur_s = (actual_dur * max(0.01, speed_param)) / 1000.0
            spoken = sum(1 for ch in voice_cps_text if 0x4E00 <= ord(ch) <= 0x9FFF)
            if spoken <= 0 or natural_dur_s <= 0:
                continue
            voice_cps = spoken / natural_dur_s
            mismatch_denom += 1
            deviation = abs(voice_cps - target_cps_val) / target_cps_val
            if deviation > 0.15:
                mismatch_count += 1
        if mismatch_denom > 0:
            body["voice_speed_mismatch_rate"] = round(mismatch_count / mismatch_denom, 4)
            body["voice_speed_mismatch_count"] = mismatch_count
            body["voice_speed_mismatch_segments"] = mismatch_denom
    except Exception:
        pass  # best-effort metric

    # First-pass duration error stats (only when at least one segment has it).
    if first_pass_errors_abs:
        sorted_err = sorted(first_pass_errors_abs)
        n = len(sorted_err)
        p50 = sorted_err[n // 2]
        p90_idx = max(0, int(n * 0.9) - 1) if n > 1 else 0
        p90 = sorted_err[min(p90_idx, n - 1)]
        body["first_pass_error_pct_avg"] = round(sum(sorted_err) / n, 4)
        body["first_pass_error_pct_p50"] = round(p50, 4)
        body["first_pass_error_pct_p90"] = round(p90, 4)
        body["first_pass_error_pct_n"] = n

    # V3-5: include tts_billed_chars only if truthfully available from TTS layer
    if tts_billed_chars is not None:
        body["tts_billed_chars"] = tts_billed_chars

    if extra_metering:
        body.update(extra_metering)

    if pre_tts_rewrite_events:
        contradiction_count = sum(
            1 for event in pre_tts_rewrite_events if event["contradiction"]
        )
        harmful_contradiction_count = sum(
            1 for event in pre_tts_rewrite_events if event["harmful_contradiction"]
        )
        body["pre_tts_rewrite_count"] = len(pre_tts_rewrite_events)
        body["pre_tts_contradiction_count"] = contradiction_count
        body["pre_tts_contradiction_rate"] = round(
            contradiction_count / len(pre_tts_rewrite_events),
            4,
        )
        body["harmful_pre_tts_contradiction_count"] = harmful_contradiction_count
        body["harmful_pre_tts_contradiction_rate"] = round(
            harmful_contradiction_count / len(pre_tts_rewrite_events),
            4,
        )
        body["pre_tts_rewrite_events"] = pre_tts_rewrite_events
    retry_attempt_count = sum(
        1 for event in pre_tts_rewrite_rejected_events
        if event["retry_attempted"]
    ) + sum(
        1 for event in pre_tts_rewrite_events
        if event["retry_attempted"]
    )
    retry_accepted_count = sum(
        1 for event in pre_tts_rewrite_events
        if event["retry_accepted"]
    )
    if pre_tts_rewrite_rejected_events:
        body["pre_tts_rewrite_rejected_count"] = len(
            pre_tts_rewrite_rejected_events
        )
        body["pre_tts_rewrite_rejected_reason_distribution"] = (
            pre_tts_rewrite_rejected_reason_counts
        )
        body["pre_tts_rewrite_rejected_events"] = pre_tts_rewrite_rejected_events
    if retry_attempt_count:
        body["pre_tts_rewrite_retry_attempt_count"] = retry_attempt_count
        body["pre_tts_rewrite_retry_accepted_count"] = retry_accepted_count

    # Phase 2 Task 0 — glossary preservation check (best-effort)
    if glossary:
        try:
            from services.gemini.translator import check_glossary_preservation
            gloss = check_glossary_preservation(segments, glossary)
            total_terms = int(gloss.get("total_terms", 0))
            preserved = int(gloss.get("preserved_terms", 0))
            body["glossary_total_terms"] = total_terms
            body["glossary_preserved_terms"] = preserved
            body["term_preservation_rate"] = (
                round(preserved / total_terms, 4) if total_terms > 0 else 1.0
            )
            missing = gloss.get("missing_terms", []) or []
            if missing:
                body["missing_glossary_terms"] = missing
        except Exception as gx:
            print(f"[metering] glossary check failed (non-fatal): {gx}", flush=True)

    return body


def _report_job_metering(
    job_id: str,
    segments: list,
    *,
    tts_billed_chars: int | None = None,
    glossary: dict[str, str] | None = None,
    extra_metering: dict[str, object] | None = None,
) -> None:
    """Best-effort callback to Gateway /job-api/jobs/{job_id}/metering."""
    import urllib.request

    gateway_base = os.environ.get("AVT_GATEWAY_URL", "http://localhost:8880")
    url = f"{gateway_base}/job-api/jobs/{job_id}/metering"

    try:
        body = _build_job_metering_payload(
            segments,
            tts_billed_chars=tts_billed_chars,
            glossary=glossary,
            extra_metering=extra_metering,
        )
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=_internal_request_headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            print(f"[metering] Reported job metering to gateway: {resp.status}", flush=True)
    except Exception as e:
        print(f"[metering] Warning: failed to report job metering: {e}", flush=True)


def _report_job_smart_state(job_id: str, smart_state: dict[str, object]) -> bool:
    """Best-effort callback that only updates Gateway ``Job.smart_state``."""
    import urllib.request

    normalized_job_id = str(job_id or "").strip()
    if not normalized_job_id or not smart_state:
        return False
    gateway_base = os.environ.get("AVT_GATEWAY_URL", "http://localhost:8880")
    url = f"{gateway_base}/job-api/jobs/{normalized_job_id}/metering"

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps({"smart_state": smart_state}).encode("utf-8"),
            headers=_internal_request_headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            ok = 200 <= int(getattr(resp, "status", 0) or 0) < 300
        if ok:
            print("[smart] Reported smart_state to gateway", flush=True)
        return ok
    except Exception as e:
        print(f"[smart] Warning: failed to report smart_state: {e}", flush=True)
        return False


def _persist_smart_clone_register_failed_state(
    *,
    job_id: str,
    reservation_id: str | None,
    failed_speakers: list[str],
    handoff_stage: str,
) -> bool:
    """Persist register-failed clone handoff into Gateway DB before UI handoff.

    The stdout ``[SMART_STATE]`` marker updates the Job API store. This direct
    Gateway write closes the gap where the TTL sweeper can run before any later
    list/get mirror imports that marker into ``Job.smart_state``.
    """
    state: dict[str, object] = {
        "status": "downgraded_to_studio",
        "reason": "clone_library_register_failed",
        "handoff_stage": handoff_stage,
        "failed_speakers": list(failed_speakers or []),
    }
    if reservation_id:
        state["smart_clone_credit_reserved"] = True
        state["smart_clone_reservation_id"] = reservation_id
    return _report_job_smart_state(job_id, state)


def _dispatch_content_compliance_admin_override_notification(
    *,
    job_id: str | None,
    user_id: str | None,
    display_name: str | None,
    summary: str,
) -> bool:
    """Best-effort task notification when admin content override is applied."""
    normalized_job_id = str(job_id or "").strip()
    normalized_user_id = str(user_id or "").strip()
    if not normalized_job_id or not normalized_user_id:
        return False

    import urllib.error
    import urllib.request

    gateway_base = os.environ.get("AVT_GATEWAY_URL", "http://localhost:8880").rstrip("/")
    url = f"{gateway_base}/internal/notifications/dispatch"
    body = {
        "event_type": CONTENT_COMPLIANCE_ADMIN_OVERRIDE_EVENT,
        "user_id": normalized_user_id,
        "job_id": normalized_job_id,
        "payload": {
            "display_name": str(display_name or normalized_job_id).strip() or normalized_job_id,
            "job_id": normalized_job_id,
            "summary": _compact_notification_summary(summary),
        },
        "dedupe_key": f"{CONTENT_COMPLIANCE_ADMIN_OVERRIDE_EVENT}:{normalized_job_id}",
        "related_id": "content_compliance",
    }
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=_internal_request_headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            print(
                f"[S2] 已发送管理员内容合规旁路任务通知：{resp.status}",
                flush=True,
            )
            return 200 <= int(resp.status) < 300
    except urllib.error.HTTPError as exc:
        print(f"[S2] Warning: 管理员内容合规旁路通知发送失败：{exc}", flush=True)
    except Exception as exc:
        print(f"[S2] Warning: 管理员内容合规旁路通知发送失败：{exc}", flush=True)
    return False


def _compact_notification_summary(summary: str, *, limit: int = 180) -> str:
    normalized = re.sub(r"\s+", " ", str(summary or "")).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(1, limit - 1)].rstrip() + "…"


def _content_compliance_admin_override_message(result: object) -> str:
    findings = getattr(result, "findings", ()) or ()
    labels = [
        str(getattr(finding, "label", "") or "").strip()
        for finding in list(findings)[:3]
    ]
    label_text = "、".join(label for label in labels if label)
    if label_text:
        return (
            "视频内容审核未通过，疑似包含中国大陆法律法规禁止传播的内容"
            f"（{label_text}）。管理员特权已允许该任务继续翻译流程；"
            "请自行确认后续使用和发布风险。"
        )
    return (
        "视频内容审核未通过。管理员特权已允许该任务继续翻译流程；"
        "请自行确认后续使用和发布风险。"
    )


def _is_pre_tts_rewrite_enabled() -> bool:
    """Check if pre-TTS rewrite is enabled from admin settings."""
    try:
        if os.path.exists(_ADMIN_SETTINGS_PATH):
            with open(_ADMIN_SETTINGS_PATH) as f:
                settings = json.load(f)
            return bool(settings.get("enable_pre_tts_rewrite", True))
    except Exception:
        pass
    return True  # Default: enabled


def _count_spoken_chars_for_metering(text: str) -> int:
    """Count chars with the same spoken-char filter as TTSDurationEstimator."""
    return count_spoken_chars(text)


def _set_usage_meter_if_supported(target: object, usage_meter: UsageMeter | None) -> None:
    setter = getattr(target, "set_usage_meter", None)
    if callable(setter):
        try:
            setter(usage_meter)
        except Exception as exc:
            print(f"[metering] set_usage_meter skipped: {exc}", flush=True)


def _write_usage_summary(usage_meter: UsageMeter | None) -> dict[str, object]:
    if usage_meter is None:
        return {}
    try:
        return usage_meter.write_summary()
    except Exception as exc:
        print(f"[metering] usage summary skipped: {exc}", flush=True)
        return {}


def _line_span_seconds_for_metering(lines: list | None) -> float:
    starts: list[int] = []
    ends: list[int] = []
    for line in lines or []:
        try:
            starts.append(int(getattr(line, "start_ms", 0) or 0))
            ends.append(int(getattr(line, "end_ms", 0) or 0))
        except (TypeError, ValueError):
            continue
    if not starts or not ends:
        return 0.0
    return max(0.0, (max(ends) - min(starts)) / 1000.0)


def _generate_tts_all_with_bucket(
    tts_generator: object,
    segments: list[DubbingSegment],
    output_dir: str,
    *,
    usage_bucket: str,
) -> list:
    generate_all = getattr(tts_generator, "generate_all")
    try:
        return generate_all(segments, output_dir, usage_bucket=usage_bucket)
    except TypeError as exc:
        if "usage_bucket" not in str(exc):
            raise
        return generate_all(segments, output_dir)


def _generate_tts_one_with_bucket(
    tts_generator: object,
    segment: DubbingSegment,
    output_dir: str,
    *,
    usage_bucket: str,
) -> Any:
    """Force one TTS synthesis pass for a segment.

    The batch ``generate_all`` path skips when the target wav already exists.
    Post-split child rewrites reuse the same segment id and output filename, so
    they must bypass that cache after mutating ``cn_text``.
    """
    generate_one = getattr(tts_generator, "_generate_one", None)
    if callable(generate_one):
        try:
            return generate_one(segment, output_dir, usage_bucket=usage_bucket)
        except TypeError as exc:
            if "usage_bucket" not in str(exc):
                raise
            return generate_one(segment, output_dir)

    results = _generate_tts_all_with_bucket(
        tts_generator,
        [segment],
        output_dir,
        usage_bucket=usage_bucket,
    )
    if not results:
        raise RuntimeError(f"TTS generation returned no result for segment_{segment.segment_id}")
    return results[0]


# Plan-based max duration (minutes).  Mirrors PLAN_CATALOG in gateway.
# The pipeline uses these only as a hard safety-net; the primary check
# is done by Gateway at job-creation time.
_PLAN_MAX_DURATION_MINUTES = {
    "free": 10,
    "plus": 60,
    "pro": 180,
}

VOICE_SPEED_PROFILE_MIN_SAMPLES = 2
VOICE_SPEED_PROFILE_MIN_SPOKEN_CHARS = 80
VOICE_SPEED_PROFILE_MIN_NATURAL_DURATION_MS = 10_000
VOICE_SPEED_PROFILE_MIN_SAMPLE_CHARS = 6
VOICE_SPEED_PROFILE_MAX_PROFILES_PER_JOB = 20


def _resolve_projects_root() -> Path:
    configured_root = os.environ.get(PROJECTS_ROOT_ENV_VAR, "").strip()
    if configured_root:
        return Path(configured_root).expanduser().resolve(strict=False)
    return (PROJECT_ROOT / "projects").resolve(strict=False)


ANONYMOUS_PREVIEW_MIN_TRANSCRIPT_CHARS = 20

# plan 2026-06-12 anonymous-express-preview §E：匿名 express 的 Pass 3
# 调用预算（秒）。超时按失败处理（诚实失败 → 任务 terminal failed），
# 超时机制落在 pipeline 调用点（concurrent.futures 包裹），不改
# review_pass3_voice_profiles 内部。
ANON_EXPRESS_PASS3_TIMEOUT_SECONDS = 120


class AnonymousExpressPass3Failed(RuntimeError):
    """匿名 express 任务 Pass 3 诚实失败（plan 2026-06-12 §E）。

    artifact 判定不过（s2_pass3_result.json 缺失 / profiles 为空）或
    120s 超时 → 抛出本异常使任务 terminal failed。**不降级出片**：
    Pass 3 失败时 CosyVoice 无 gender 全员回落 longanyang，把坏预览
    交给用户违背"免费触点必须真实管线效果"最高指导原则。
    """


def _should_run_pass3(
    review_speaker_styles: object,
    anonymous_preview: bool,
    service_mode: str = "",
) -> bool:
    """S2.5 Pass 3 gate（APF P0 T5 + plan 2026-06-12 §E 按 lane 分流）。

    * 登录任务：行为不变（有 speaker styles 即跑）。
    * 匿名 free：跳过不变——音色画像是付费多模态调用，对预设音色预览
      不必要（MiMo style 回落 Pass 1/2 已有字段或默认音色）。
    * 匿名 express（D6 必选）：必须跑——CosyVoice 选音依赖 gender，
      无画像会全员回落 longanyang（US prod 实证音色错配）。
    """
    if not review_speaker_styles:
        return False
    if not anonymous_preview:
        return True
    return str(service_mode or "").strip() == "express"


def _anon_express_pass3_artifact_ok(
    pass3_cache_path: Path,
    expected_speaker_ids=(),
) -> bool:
    """匿名 express Pass 3 的 artifact 判定（plan §E 唯一可信信号）。

    ``review_pass3_voice_profiles()`` 内部 retry+fallback 会吞错
    （transcript_reviewer.py:2161/:2208），成功 artifact 只在 :2189 写——
    所以只认 ``s2_pass3_result.json`` 存在且 ``speaker_profiles`` 非空。

    CodeX 第三轮 P2 收紧：多说话人任务的**部分**产物（如只有 speaker_a）
    也必须判失败——merge 只更新产物里有的说话人，缺失者无 gender/style
    进音色匹配会回落错误默认音色（正是本计划要消灭的坏预览路径）。
    ``expected_speaker_ids`` 全部出现在 profiles 中才算成功。

    任何读取/解析异常 → False（fail-closed）。
    """
    try:
        if not pass3_cache_path.is_file():
            return False
        data = json.loads(pass3_cache_path.read_text(encoding="utf-8"))
        profiles = data.get("speaker_profiles")
        if not isinstance(profiles, dict) or len(profiles) == 0:
            return False
        return set(expected_speaker_ids).issubset(profiles.keys())
    except Exception:  # noqa: BLE001 — fail-closed
        return False


def _anonymous_compliance_fail_closed_result(
    *,
    reason_code: str,
    source_type: str,
    source_ref: str,
    video_title: str,
) -> ContentComplianceResult:
    """构造匿名严格 lane 的 fail-closed blocked 结果（APF P0 T5 / AD-2 v2）。

    reason_code 只允许固定枚举串（compliance_disabled / llm_unavailable /
    transcript_near_empty），不嵌入任何运行时细节，防泄漏。
    """
    from datetime import datetime, timezone

    return ContentComplianceResult(
        status="blocked",
        policy_code="anonymous_preview_fail_closed",
        policy_version="apf-p0-t5",
        checked_at=datetime.now(timezone.utc).isoformat(),
        source_type=source_type,
        source_ref=source_ref,
        video_title=video_title,
        message=f"匿名预览合规 fail-closed：{reason_code}",
        findings=(),
        layers={"anonymous_fail_closed": reason_code},
    )


def _check_duration_limit(
    duration_ms: int,
    *,
    plan_code_snapshot: str = "free",
    role_snapshot: str = "user",
    service_mode_snapshot: str = "",
) -> None:
    """Check video duration against the plan / service-mode limit from snapshot.

    Admin users bypass the check entirely.  The primary validation is done by
    Gateway at creation time; this is a hard safety-net inside the pipeline,
    AFTER the authoritative ffprobe and BEFORE the expensive ASR/LLM/TTS stages.

    Phase 2a Task 7 (gate #7): ``service_mode == "free"`` is FAIL-CLOSED — the
    10-min cap holds even when ffprobe cannot be trusted (unknown / zero duration
    REJECTS, never proceeds, so the cost gate closes). Paid modes keep the legacy
    lenient plan-based check (a 0 there means "unknown" and is allowed).
    """
    if role_snapshot == "admin":
        return

    if service_mode_snapshot == "free":
        from utils.free_duration_gate import (
            FREE_DURATION_CAP_MINUTES,
            REJECT_OVER_CAP,
            REJECT_UNTRUSTED,
            evaluate_free_duration_cap,
        )

        reason = evaluate_free_duration_cap(
            duration_ms, max_minutes=FREE_DURATION_CAP_MINUTES
        )
        if reason == REJECT_UNTRUSTED:
            raise RuntimeError(
                "无法确认视频时长（探测失败或文件损坏）。免费版要求可信时长，"
                "请重新上传清晰完整的视频文件，或升级套餐。"
            )
        if reason == REJECT_OVER_CAP:
            raise RuntimeError(
                f"免费版视频时长上限 {FREE_DURATION_CAP_MINUTES:.0f} 分钟，"
                f"当前 {duration_ms / 60_000:.1f} 分钟。请使用更短的视频，或升级套餐。"
            )
        print(
            f"[S1] 免费版时长 {duration_ms / 60_000:.1f} 分钟，"
            f"限制 {FREE_DURATION_CAP_MINUTES:.0f} 分钟内。"
        )
        return

    max_minutes = _PLAN_MAX_DURATION_MINUTES.get(plan_code_snapshot, 10)
    actual_minutes = duration_ms / 60_000
    if actual_minutes > max_minutes:
        raise RuntimeError(
            f"视频时长 {actual_minutes:.1f} 分钟超出套餐上限（{max_minutes:.0f} 分钟）。"
            f"请使用更短的视频，或升级套餐。"
        )
    print(f"[S1] 视频时长 {actual_minutes:.1f} 分钟，套餐限制 {max_minutes:.0f} 分钟内。")


def _check_disk_space(project_dir: Path, estimated_duration_minutes: float) -> None:
    """Check if enough disk space for processing."""
    estimated_gb = estimated_duration_minutes * 0.035  # ~35 MB/min
    free_gb = shutil.disk_usage(project_dir).free / (1024**3)
    if free_gb < estimated_gb * 1.5:
        raise RuntimeError(
            f"磁盘空间不足：需要约 {estimated_gb:.1f}GB，当前可用 {free_gb:.1f}GB"
        )
    print(f"[S1] 磁盘空间检查: 需要约 {estimated_gb:.1f}GB, 可用 {free_gb:.1f}GB ✓")


def _cleanup_upload_mp3(project_dir: Path) -> None:
    """Delete the temporary MP3 created for AssemblyAI upload."""
    upload_mp3 = project_dir / "audio" / "original_upload.mp3"
    if upload_mp3.exists():
        upload_mp3.unlink()
        print("[S1] 清理临时上传文件 original_upload.mp3")


@dataclass(slots=True)
class ProcessConfig:
    youtube_url: str = ""
    voice_a: str | None = None
    speaker_a_name: str = "Speaker A"
    speakers: int | str = "auto"
    project_dir: str | None = None
    resume_from: str | None = None
    voice_b: str | None = None
    speaker_b_name: str = "Speaker B"
    skip_review: bool = False
    wait_for_review: bool = False
    transcription_method: str = "assemblyai"
    job_id: str | None = None  # Job API job_id（由 process_runner 传入）
    job_record: object | None = None  # DB job row with policy snapshot fields
    source_type: str = ""
    source_ref: str = ""

    def __post_init__(self) -> None:
        """Normalize source fields for backward compatibility.

        Rules:
        - If only ``youtube_url`` is given (legacy callers), derive
          ``source_type="youtube_url"`` and ``source_ref=youtube_url``.
        - If ``source_type``/``source_ref`` are given explicitly, back-fill
          ``youtube_url`` when the source is a YouTube URL (so existing
          pipeline code that reads ``config.youtube_url`` keeps working).
        - ``source_type``/``source_ref`` always take precedence over the
          positional ``youtube_url`` when both are provided.
        """
        st = (self.source_type or "").strip()
        sr = (self.source_ref or "").strip()
        yt = (self.youtube_url or "").strip()

        if st and sr:
            # Explicit source wins — always override youtube_url
            self.source_type = st
            self.source_ref = sr
            if st == "youtube_url":
                self.youtube_url = sr
            else:
                self.youtube_url = ""
        elif yt:
            # Legacy caller only gave youtube_url
            self.source_type = "youtube_url"
            self.source_ref = yt
            self.youtube_url = yt
        # else: both empty — will be caught by pipeline validation


@dataclass(slots=True)
class ProcessResult:
    project_dir: str
    dubbed_audio_path: str
    ambient_audio_path: str
    subtitles_path: str
    segments_dir: str
    alignment_report_path: str
    background_sounds_path: str
    total_segments: int
    needs_review_count: int
    status: str = "completed"
    paused_review_stage: str | None = None
    paused_review_message: str | None = None


class VoiceReviewRequiredError(AutoCloneError):
    def __init__(
        self,
        *,
        speaker_id: str,
        speaker_label: str,
        speaker_name: str,
        voice_arg_name: str,
        sample_path: str,
        sample_metrics: dict[str, object],
        message: str,
    ) -> None:
        super().__init__(message)
        self.speaker_id = speaker_id
        self.speaker_label = speaker_label
        self.speaker_name = speaker_name
        self.voice_arg_name = voice_arg_name
        self.sample_path = sample_path
        self.sample_metrics = sample_metrics


def _truncate_at_sentence(words: list[str], target_count: int) -> str:
    """Truncate a word list at a sentence boundary near *target_count*.

    Looks for sentence-ending punctuation (. ? !) working backward from
    target_count. Falls back to comma/semicolon, then hard cut.
    """
    if len(words) <= target_count:
        return " ".join(words)
    # Look backward for sentence boundary
    for i in range(target_count - 1, max(target_count // 2, 4), -1):
        if words[i].endswith((".", "?", "!", "。", "？", "！")):
            return " ".join(words[: i + 1])
    # Fallback: comma / semicolon
    for i in range(target_count - 1, max(target_count // 2, 4), -1):
        if words[i].endswith((",", ";", "，", "；")):
            return " ".join(words[: i + 1])
    # Hard cut
    return " ".join(words[:target_count])


def _call_content_compliance_llm_with_retry(
    translator: object,
    prompt: str,
    *,
    primary_model: str,
    retry_delay_seconds: float | None = None,
    peer_cost_rank_delta: int | None = None,
) -> str:
    primary = str(primary_model or "gemini")
    delay_seconds = (
        _content_compliance_retry_delay_seconds()
        if retry_delay_seconds is None
        else max(0.0, float(retry_delay_seconds))
    )
    rank_delta = (
        _content_compliance_peer_cost_rank_delta()
        if peer_cost_rank_delta is None
        else max(0, int(peer_cost_rank_delta))
    )
    peer_models = _get_peer_model_candidates(
        primary,
        "content_compliance",
        cost_rank_delta=rank_delta,
    )
    attempts: list[tuple[str, str]] = [
        (primary, "primary"),
        (primary, "primary_retry"),
    ]
    attempts.extend(
        (model_name, f"peer_fallback_{index}")
        for index, model_name in enumerate(peer_models, start=1)
    )

    last_error: Exception | None = None
    for attempt_index, (model_name, attempt_label) in enumerate(attempts):
        if attempt_index == 1 and delay_seconds > 0:
            print(f"[S2] 内容合规大模型审核失败，暂停 {delay_seconds:g} 秒后重试同模型...")
            time.sleep(delay_seconds)
        try:
            print(f"[S2] 内容合规大模型审核使用 {model_name} ({attempt_label})")
            response_text = translator._call_by_model(  # type: ignore[attr-defined]
                model_name,
                prompt,
                json_mode=True,
            )
            _record_content_compliance_llm_usage(
                translator,
                model_name=model_name,
                prompt=prompt,
                response_text=response_text,
                attempt_label=attempt_label,
            )
            validate_content_compliance_llm_response(response_text)
            return response_text
        except Exception as exc:
            last_error = exc
            if attempt_index < len(attempts) - 1:
                next_model = attempts[attempt_index + 1][0]
                print(
                    f"[S2] 内容合规大模型审核 {model_name} ({attempt_label}) 失败，"
                    f"准备尝试 {next_model}：{exc}"
                )
            else:
                print(
                    f"[S2] 内容合规大模型审核 {model_name} ({attempt_label}) 失败，"
                    f"已无同级别备用模型：{exc}"
                )
    raise RuntimeError(f"内容合规大模型审核多次失败：{last_error}") from last_error


def _record_content_compliance_llm_usage(
    translator: object,
    *,
    model_name: str,
    prompt: str,
    response_text: str,
    attempt_label: str,
) -> None:
    record = getattr(translator, "_record_llm_usage", None)
    if not callable(record):
        return
    record(
        task="content_compliance",
        model_name=model_name,
        prompt=prompt,
        response_text=response_text,
        attempt_label=attempt_label,
    )


def _content_compliance_retry_delay_seconds() -> float:
    raw_value = os.environ.get("AVT_CONTENT_COMPLIANCE_LLM_RETRY_DELAY_SECONDS")
    if raw_value is None:
        return CONTENT_COMPLIANCE_LLM_RETRY_DELAY_SECONDS
    try:
        return max(0.0, float(raw_value))
    except ValueError:
        return CONTENT_COMPLIANCE_LLM_RETRY_DELAY_SECONDS


def _content_compliance_peer_cost_rank_delta() -> int:
    raw_value = os.environ.get("AVT_CONTENT_COMPLIANCE_LLM_PEER_COST_RANK_DELTA")
    if raw_value is None:
        return CONTENT_COMPLIANCE_LLM_PEER_COST_RANK_DELTA
    try:
        return max(0, int(raw_value))
    except ValueError:
        return CONTENT_COMPLIANCE_LLM_PEER_COST_RANK_DELTA


def _is_english_language_code(language_code: str) -> bool:
    normalized = str(language_code or "").strip().lower().replace("_", "-")
    return normalized == "en" or normalized.startswith("en-")


def _minimax_language_matches_target(voice_language: str, target_language: str) -> bool:
    """Does a MiniMax voice's catalog ``language`` tag match the job's target?

    Byte-identical to the legacy hard-coded Chinese filter for the GA default
    target (``zh-CN`` → Mandarin/Cantonese). ``en`` → MiniMax's ``"英语"`` tag.
    This provider-specific tag mapping is a PR-W de-Chinese seam; PR-E will
    replace it with ``voice_catalog.compatible_target_languages``.
    """
    if target_language == LANG_ZH_CN:
        return voice_language in ("中文-普通话", "中文-粤语")
    if target_language == LANG_EN:
        return voice_language == "英语"
    return False


class ProcessPipeline:
    """Legacy compatibility pipeline: YouTube URL -> editor-facing dubbing bundle."""

    def __init__(self, project_builder: ProjectBuilder | None = None) -> None:
        self.project_builder = project_builder or ProjectBuilder()

    def run(self, config: ProcessConfig) -> ProcessResult:
        # Commit copy_as_new / overwrite routes here with
        # resume_from='alignment' to skip S0-S3 (D28). All context the
        # alignment+publish block needs is rebuilt from the project_dir
        # artifacts the commit step already placed on disk.
        if config.resume_from == STAGE_ALIGNMENT:
            return self._run_alignment_and_publish_only(config)

        source_type = config.source_type or "youtube_url"
        source_ref = config.source_ref or config.youtube_url or ""
        normalized_url = config.youtube_url.strip()
        normalized_voice_a = config.voice_a.strip() if isinstance(config.voice_a, str) else None
        normalized_voice_b = config.voice_b.strip() if isinstance(config.voice_b, str) else None
        normalized_speakers = self._normalize_speakers(config.speakers)

        if not source_ref.strip():
            raise ValueError("source_ref 不能为空。")

        assemblyai_config = self._load_stage_config("AssemblyAI", load_assemblyai_config)
        gemini_config = self._load_stage_config("Gemini", load_gemini_config)
        llm_fallback_config = self._load_stage_config("LLM fallback", load_llm_fallback_config)
        tts_config = self._load_stage_config("MiniMax TTS", load_tts_config)
        youtube_download_config = load_youtube_download_config()
        llm_router = LLMRouter(llm_fallback_config)

        # --- Read job policy snapshot --------------------------------
        _jr = config.job_record
        # If no job_record passed, load precisely by job_id from Job API store
        if _jr is None and config.job_id:
            try:
                from services.jobs.store import JobStore
                _store = JobStore(PROJECT_ROOT / "jobs")
                _job_record = _store.load_job(config.job_id)
                if _job_record is not None:
                    _jr = _job_record.to_dict()
                    config.job_record = _jr
                    print(f"[PIPELINE] Loaded job snapshot for {config.job_id}: service_mode={_jr.get('service_mode')}, tts_provider={_jr.get('tts_provider')}", flush=True)
                    print(f"[PIPELINE] Snapshot: OK (job_id={config.job_id}, service_mode={_jr.get('service_mode')}, tts_provider={_jr.get('tts_provider')})", flush=True)
                else:
                    print(f"[PIPELINE] Warning: job {config.job_id} not found in store — snapshot unavailable, using defaults", flush=True)
                    print(f"[PIPELINE] Snapshot: MISSING (job_id={config.job_id}, reason=not_found_in_store)", flush=True)
            except Exception as e:
                print(f"[PIPELINE] Warning: failed to load job {config.job_id}: {type(e).__name__}: {e}", flush=True)
                print(f"[PIPELINE] Snapshot: FAILED (job_id={config.job_id}, reason={type(e).__name__})", flush=True)
        elif _jr is None:
            print("[PIPELINE] Warning: no job_id provided, snapshot unavailable — using defaults", flush=True)
            print("[PIPELINE] Snapshot: UNAVAILABLE (no job_id provided)", flush=True)

        def _snap(key, default=None):
            if isinstance(_jr, dict):
                v = _jr.get(key)
            else:
                v = getattr(_jr, key, None)
            return v if v is not None else default

        job_service_mode = _snap('service_mode', 'express')
        job_tts_provider = _snap('tts_provider', 'cosyvoice')
        job_requires_review = _snap('requires_review', False)
        job_voice_strategy = _snap('voice_strategy', 'preset_mapping')
        job_plan_code = _snap('plan_code_snapshot', 'free')
        job_role = _snap('role_snapshot', 'user')
        # APF P0 T5（AD-7/G3）：匿名预览标记，严格 is True gate；
        # 驱动匿名严格合规 lane + Pass 3 跳过。登录任务恒 False。
        job_anonymous_preview = _snap('anonymous_preview', False) is True
        # P3e-3：smart 预览标记（在 ``smart_state`` 字典，create 经 preview_mode
        # stamp）。驱动 3min teaser + 水印 + 跳分钟结算。**区别 is_anonymous_
        # preview**：smart 预览是**登录**用户、且**保留**克隆 600 结算（只跳分钟），
        # 故另立标记不复用匿名字段。create 未接线前 smart_state 无此键 → False
        # （inert，既有 smart 行为不变）。
        _job_smart_state_snap = _snap('smart_state')
        job_smart_preview = bool(
            isinstance(_job_smart_state_snap, dict)
            and _job_smart_state_snap.get('smart_preview_mode') is True
        )
        if job_anonymous_preview and job_voice_strategy != 'preset_mapping':
            # 防 clone 第三道防线（对抗审核加固）：匿名任务的 voice_strategy
            # 只允许 preset_mapping——gateway payload 硬编码是第一道、
            # 白名单校验是第二道，这里是 pipeline 侧独立兜底，任何上游
            # 改动都不可能让匿名任务踏入 voiceclone 分支。
            print(
                f"[PIPELINE] 匿名预览任务 voice_strategy={job_voice_strategy} 非法，"
                "强制回 preset_mapping",
                flush=True,
            )
            job_voice_strategy = 'preset_mapping'
        self._current_service_mode = job_service_mode  # for recovery paths

        # --- Multilingual language pair (PR-W) ------------------------
        # Resolve the job's source/target language to a registry profile and stash
        # it on the instance so the language-aware helpers (source gate,
        # failed-segment split, voice pool builder, probe counting) can dispatch on
        # it. Those helpers derive per-side script descriptors from this profile
        # on demand (single source of truth) rather than from cached instance
        # fields, so they stay correct even when called directly (bypassing run()).
        # The create-path Gateway gate (PR-A) already rejected unsupported /
        # not-yet-ready pairs with 4xx, so anything reaching here is the GA default
        # (en->zh-CN) or a ``pipeline_ready`` pair. ``_resolve_job_language_profile``
        # is fail-closed: absent fields → GA default (byte-identical), present but
        # unsupported → ValueError (never silently run a foreign source as en->zh).
        self._language_profile = self._resolve_job_language_profile(
            _snap('source_language'), _snap('target_language')
        )
        if not self._language_profile.is_default:
            print(
                f"[PIPELINE] Language pair: {self._language_profile.language_pair} "
                f"(source={self._language_profile.source_language}, "
                f"target={self._language_profile.target_language})",
                flush=True,
            )

        # Smart MVP P2 (plan §6.0.6 / Codex 第八轮 F3) — derive the
        # effective pipeline mode that smart-aware branches should
        # consult. ``job_service_mode`` (raw from JobRecord) stays the
        # audit fact and is what Gateway routing / billing / quota
        # queries continue to use. ``job_effective_pipeline_mode``
        # is the per-frame value that smart-aware branches in this
        # function (auto-review trigger, Studio gate flips, handoff
        # short-circuit on /continue) MUST read instead of
        # ``job_service_mode`` — otherwise a smart job whose
        # smart_state.status is "downgraded_to_studio" will re-enter
        # the auto layer on /continue and loop the same failure.
        # The variable is derived here even though the smart-aware
        # branches that consume it land in subsequent PRs; landing
        # the variable now lets reviewers / future implementers point
        # at the canonical reading of "effective mode" rather than
        # re-derive it inline at each call site.
        from services.smart.state import derive_effective_pipeline_mode
        job_effective_pipeline_mode = derive_effective_pipeline_mode(_jr) if _jr else job_service_mode
        # -------------------------------------------------------------

        projects_root = _resolve_projects_root()
        projects_root.mkdir(parents=True, exist_ok=True)

        explicit_project_dir = (
            Path(config.project_dir).expanduser().resolve(strict=False)
            if config.project_dir is not None
            else None
        )

        # Workspace selection: explicit project_dir wins; otherwise create fresh dir.
        # No longer reusing old project directories based on URL match.
        if explicit_project_dir is not None:
            working_project_dir = explicit_project_dir
            final_project_dir = explicit_project_dir
        else:
            working_project_dir = Path(
                tempfile.mkdtemp(prefix="_process_", dir=projects_root)
            ).resolve(strict=False)
            final_project_dir = working_project_dir
        usage_meter = UsageMeter(final_project_dir, job_id=config.job_id)
        review_state_manager: ReviewStateManager | None = None
        state_manager: StateManager | None = None
        current_stage_name: str | None = None

        try:
            current_project_dir = final_project_dir

            if source_type in ("local_video", "local_audio"):
                # --- Local source ingest ---
                download_result, video_path, source_audio_path, ingestion_execution_mode = (
                    self._ingest_local_source(
                        source_type=source_type,
                        source_ref=source_ref,
                        project_dir=final_project_dir,
                    )
                )
            else:
                # --- YouTube ingest (existing logic) ---
                video_path = (current_project_dir / "video" / "original.mp4").resolve(strict=False)
                source_audio_path = (current_project_dir / "audio" / "original.wav").resolve(strict=False)

                if video_path.exists() and source_audio_path.exists():
                    print("[S0] 已有下载缓存，跳过下载")
                    ingestion_execution_mode = "cache_restore_full"
                    download_result = self._load_download_result(
                        current_project_dir,
                        fallback_url=normalized_url,
                    )
                else:
                    print("[S0] 下载视频...")
                    ingestion_execution_mode = "fresh_run"
                    download_result = YouTubeDownloader().download(
                        DownloadRequest(
                            url=normalized_url,
                            output_dir=str(working_project_dir),
                            cookies_from_browser=_normalize_optional_text(
                                youtube_download_config.get("cookies_from_browser")
                            ),
                            cookie_file=_normalize_optional_text(
                                youtube_download_config.get("cookie_file")
                            ),
                            max_retries=_coerce_int(
                                youtube_download_config.get("max_retries"),
                                default=2,
                            ),
                            retry_backoff_seconds=_coerce_float(
                                youtube_download_config.get("retry_backoff_seconds"),
                                default=1.5,
                            ),
                        )
                    )

                    if explicit_project_dir is None and working_project_dir.name.startswith("_process_"):
                        resolved_project_dir = Path(
                            self._resolve_project_dir(config, download_result.video_title)
                        ).resolve(strict=False)
                        if resolved_project_dir != working_project_dir:
                            if resolved_project_dir.exists():
                                # Slug dir already taken — keep the temp dir to avoid sharing
                                print(
                                    f"[S0] 目标目录已被占用，保留临时目录："
                                    f"{working_project_dir}"
                                )
                                resolved_project_dir = working_project_dir
                            else:
                                resolved_project_dir.parent.mkdir(parents=True, exist_ok=True)
                                shutil.move(str(working_project_dir), str(resolved_project_dir))
                            final_project_dir = resolved_project_dir
                        else:
                            final_project_dir = working_project_dir
                    else:
                        final_project_dir = current_project_dir

                    download_result = self._load_download_result(
                        final_project_dir,
                        fallback_url=normalized_url,
                        fallback_title=download_result.video_title,
                        fallback_duration_ms=download_result.duration_ms,
                    )

                video_path = (final_project_dir / "video" / "original.mp4").resolve(strict=False)
                source_audio_path = (final_project_dir / "audio" / "original.wav").resolve(strict=False)

            # --- P3e-3b：智能版 3min 预览 teaser 裁剪（consumer 侧 / 钱-关键耦合）---
            # smart 预览（job_smart_preview）把源**音频**在 separation/ASR/clone/TTS
            # 等付费阶段**之前**裁到 ~180s teaser。音频 teaser 同时有界两条边界：
            # ① 付费 AI 阶段全读 teaser 派生音频（分离→ASR→克隆→TTS 限 3 分钟）；
            # ② 下方 actual_duration_ms = _ffprobe_duration_ms(source_audio_path) →
            #    EditorPackageWriter 据此造 base silence（成片音轨长度）+ render
            #    -shortest → 成片收口到 ~3 分钟。**视频不裁**：原 original.mp4 保留 +
            #    render 经 -shortest 收口（artifact index source.original_video 仍指真
            #    原视频，供 P3e-3c-2 转完整复用，不被 teaser 污染）。
            # 使后续 P3e-3c 跳分钟安全（否则 pipeline 跑完整视频却不收分钟 = 免费完整
            # 任务，漏收全部分钟点 ≫ 克隆 600）。本步**仍扣分钟**（create reserve 不变 →
            # settle 照常 = 无漏收）。默认 inert：create 未 stamp smart_preview_mode 前
            # job_smart_preview=False → 不触发。
            if job_smart_preview:
                from utils.smart_preview_teaser import (
                    SmartPreviewTeaserError,
                    apply_smart_preview_teaser,
                    smart_preview_gemini_url_unbounded,
                )

                # 防御：gemini-on-URL 转录读 normalized_url（原始 URL）做多模态转录，
                # 本地 teaser 无法有界它 → fail-closed（绝不对未有界的完整源跑付费转录/
                # TTS）。smart 预览默认 assemblyai + 本地上传（normalized_url 空 → 走本地
                # speech_audio_path，已被 teaser 有界），本守卫只在误配 gemini+URL 时触发。
                if smart_preview_gemini_url_unbounded(
                    getattr(config, "transcription_method", None), normalized_url
                ):
                    raise SmartPreviewTeaserError(
                        "smart 预览不支持 gemini-on-URL 转录（本地 teaser 无法有界源），"
                        "fail-closed"
                    )

                # 只裁音频并 repoint source_audio_path：下游付费阶段（分离/ASR/克隆/
                # TTS）+ actual_duration_ms 改读 3min teaser；video_path **保持原值**。
                # fail-closed 异常向外层 try 传播 → 任务 terminal failed（绝不退回处理
                # 完整源）。
                source_audio_path = apply_smart_preview_teaser(
                    source_audio_path=source_audio_path,
                    project_dir=final_project_dir,
                )
                print(
                    "[S0.5] 智能版预览：源音频已裁剪至 3min teaser"
                    "（原视频/音频保留供转完整复用，成片经 -shortest + 3min 基线静音有界）",
                    flush=True,
                )

            review_state_manager = ReviewStateManager(final_project_dir / "review_state.json")
            state_manager = StateManager(str(final_project_dir / "project_state.json"))
            state_manager.set_project(final_project_dir.name)
            state_manager.set_stage(
                "ingestion",
                StageStatus.DONE,
                self._build_ingestion_stage_payload(
                    final_project_dir=final_project_dir,
                    download_result=download_result,
                    video_path=video_path,
                    source_audio_path=source_audio_path,
                    execution_mode=ingestion_execution_mode,
                    source_type=source_type,
                ),
            )
            current_stage_name = "audio_preparation"
            state_manager.set_stage(
                current_stage_name,
                StageStatus.RUNNING,
                {
                    "execution_mode": "legacy_process",
                },
            )
            separated_audio = self._ensure_separated_audio_assets(
                project_dir=final_project_dir,
                source_audio_path=source_audio_path,
            )
            speech_audio_path = Path(separated_audio.speech_audio_path).resolve(strict=False)
            ambient_audio_path = Path(separated_audio.ambient_audio_path).resolve(strict=False)
            state_manager.set_stage(
                current_stage_name,
                StageStatus.DONE,
                self._build_audio_preparation_stage_payload(
                    source_audio_path=source_audio_path,
                    separated_audio=separated_audio,
                ),
            )
            current_stage_name = None
            self._refresh_download_metadata(
                final_project_dir=final_project_dir,
                video_path=video_path,
                source_audio_path=source_audio_path,
                video_title=download_result.video_title,
                duration_ms=download_result.duration_ms,
                url=download_result.url,
                description=download_result.description,
                speech_audio_path=speech_audio_path,
                ambient_audio_path=ambient_audio_path,
            )

            actual_duration_ms = _ffprobe_duration_ms(source_audio_path)
            print(
                f"[S0] 完成：标题={download_result.video_title}，"
                f"时长={round(download_result.duration_ms / 1000, 2)}秒"
            )
            print(
                f"[S0] 音频实际时长：{round(actual_duration_ms / 1000, 2)}秒"
                f"（yt-dlp报告：{round(download_result.duration_ms / 1000, 2)}秒）"
            )
            self._enforce_source_language(download_result)

            # --- 套餐时长限制 (snapshot-based, Gateway 主检查的安全网) ---
            _check_duration_limit(
                actual_duration_ms,
                plan_code_snapshot=job_plan_code,
                role_snapshot=job_role,
                service_mode_snapshot=job_service_mode,
            )

            # --- Report actual duration to Gateway (best-effort) ---
            if config.job_id:
                _report_source_metadata(config.job_id, actual_duration_ms / 1000, download_result.video_title)

            # --- 磁盘空间预检 ---
            _check_disk_space(final_project_dir, actual_duration_ms / 60_000)

            transcriber = AssemblyAITranscriber(
                str(assemblyai_config["api_key"]),
                http_timeout_seconds=float(
                    assemblyai_config.get("http_timeout_seconds", 900.0)
                ),
            )
            translator_kwargs = {
                "api_key": str(gemini_config["api_key"]),
                "model_name": str(gemini_config["model_name"]),
                "temperature": float(gemini_config["temperature"]),
                "max_output_tokens": int(gemini_config["max_output_tokens"]),
                "sdk_backend": str(gemini_config.get("sdk_backend", "google-genai")),
                "llm_router": llm_router,
            }
            custom_translation_prompt_template = gemini_config.get("translation_prompt_template")
            custom_speaker_infer_prompt_template = gemini_config.get("speaker_infer_prompt_template")
            if custom_speaker_infer_prompt_template is not None:
                translator_kwargs["speaker_infer_prompt_template"] = str(
                    custom_speaker_infer_prompt_template
                )
            if custom_translation_prompt_template is not None:
                translator_kwargs["translation_prompt_template"] = str(
                    custom_translation_prompt_template
                )
            translator = GeminiTranslator(**translator_kwargs)
            translator._service_mode = job_service_mode  # enables llm_registry model selection
            _set_usage_meter_if_supported(translator, usage_meter)

            transcript_path = (final_project_dir / "transcript" / "transcript.json").resolve(strict=False)
            transcription_method = getattr(config, "transcription_method", None) or "assemblyai"

            if transcript_path.exists():
                print("[S1] 已有转录结果，跳过转录")
                media_execution_mode = "cache_restore_full"
                transcript_result = self._load_transcript_result(transcript_path)
            elif transcription_method == "gemini":
                print("[S1] 使用 Gemini 多模态转录...")
                media_execution_mode = "fresh_run"
                from services.gemini.transcriber import GeminiTranscriber
                gemini_transcriber = GeminiTranscriber(
                    api_key=str(gemini_config.get("api_key", "") or os.environ.get("GEMINI_API_KEY", "")),
                )
                transcript_result = gemini_transcriber.transcribe(
                    normalized_url,
                    str(final_project_dir / "transcript"),
                    speaker_labels=normalized_speakers == "auto" or (isinstance(normalized_speakers, int) and normalized_speakers >= 2),
                    speakers_expected=normalized_speakers if isinstance(normalized_speakers, int) and normalized_speakers >= 2 else None,
                )
                usage_meter.record_llm(
                    task="s1_gemini_transcribe",
                    provider="gemini",
                    model=str(gemini_config.get("model_name", "gemini")),
                    model_id=str(gemini_config.get("model_name", "gemini")),
                    input_text=normalized_url,
                    output_text="\n".join(line.source_text for line in transcript_result.lines),
                    audio_input_seconds=_line_span_seconds_for_metering(transcript_result.lines),
                    attempt_label="legacy_gemini_transcription",
                )
                print(f"[S1] Gemini 转录完成：共 {len(transcript_result.lines)} 条")
            else:
                print("[S1] 转录音频...")
                media_execution_mode = "fresh_run"
                transcript_result = transcriber.transcribe(
                    str(speech_audio_path),
                    str(final_project_dir / "transcript"),
                    speaker_labels=normalized_speakers == "auto" or (isinstance(normalized_speakers, int) and normalized_speakers >= 2),
                    speakers_expected=normalized_speakers if isinstance(normalized_speakers, int) and normalized_speakers >= 2 else None,
                )
                print(f"[S1] 完成：共 {len(transcript_result.lines)} 条转录")

            if transcript_result.lines:
                self._enforce_transcript_language(transcript_result)

            if transcript_path.exists() and not transcript_result.lines:
                print(f"[S1] 完成：共 {len(transcript_result.lines)} 条转录")

            # --- 清理转录上传临时文件 ---
            _cleanup_upload_mp3(final_project_dir)

            if normalized_speakers == "auto":
                detected_speaker_ids = self._detect_speaker_ids(transcript_result.lines)
                detected_count = len(detected_speaker_ids)
                detected_summary = ", ".join(detected_speaker_ids) or "speaker_a"
                print(f"[S1] 自动识别到 {detected_count} 位说话人：{detected_summary}")
                if detected_count <= 1:
                    effective_speakers = 1
                else:
                    effective_speakers = detected_count
            else:
                effective_speakers = normalized_speakers

            current_stage_name = "media_understanding"
            state_manager.set_stage(
                current_stage_name,
                StageStatus.RUNNING,
                {
                    "execution_mode": media_execution_mode,
                },
            )
            content_compliance_llm = None
            content_compliance_model = ""
            if is_content_compliance_llm_enabled():
                content_compliance_model = _get_prompt_model(
                    job_service_mode,
                    "content_compliance",
                )

                def content_compliance_llm(prompt: str) -> str:
                    return _call_content_compliance_llm_with_retry(
                        translator,
                        prompt,
                        primary_model=content_compliance_model,
                    )

            content_compliance_payload = self._run_content_compliance_review(
                final_project_dir=final_project_dir,
                transcript_result=transcript_result,
                download_result=download_result,
                source_type=source_type,
                source_ref=source_ref,
                llm_generate_json=content_compliance_llm,
                llm_model_name=content_compliance_model,
                admin_override=str(job_role or "").strip().lower() == "admin",
                job_id=config.job_id,
                user_id=str(_snap("user_id") or ""),
                display_name=str(_snap("display_name") or download_result.video_title or ""),
                anonymous_strict=job_anonymous_preview,
            )

            speaker_name_a = config.speaker_a_name
            speaker_name_b = config.speaker_b_name
            voice_id_a = normalized_voice_a
            voice_id_b = normalized_voice_b
            segments_path = (final_project_dir / "translation" / "segments.json").resolve(strict=False)
            s3_cache_hit = segments_path.exists()
            speaker_name_a_is_placeholder = self._is_default_placeholder_speaker_name(
                speaker_id="speaker_a",
                speaker_name=speaker_name_a,
            )
            speaker_name_b_is_placeholder = effective_speakers == 2 and self._is_default_placeholder_speaker_name(
                speaker_id="speaker_b",
                speaker_name=speaker_name_b,
            )

            # --- Unified LLM transcript review (replaces 4 separate calls) ---
            _review_glossary: dict[str, str] = {}
            _review_speaker_styles: dict[str, dict] = {}
            _review_speaker_names: dict[str, str] = {}  # all speakers including c+
            _review_display_title_zh: str | None = None

            # S2 cache: if review already ran (e.g. pipeline resumed after
            # translation_config_review), restore results instead of re-running.
            s2_result_path = (final_project_dir / "transcript" / "s2_review_result.json").resolve(strict=False)
            s2_cache_hit = s2_result_path.exists() and not s3_cache_hit

            if s3_cache_hit:
                print("[S2] Translation cache hit, skipping review.")
            elif s2_cache_hit:
                try:
                    import json as _json_s2
                    _s2_cached = _json_s2.loads(s2_result_path.read_text(encoding="utf-8"))
                    _review_speaker_styles = _s2_cached.get("speakers", {})
                    _review_glossary = _s2_cached.get("glossary", {})
                    _review_display_title_zh = _s2_cached.get("display_title_zh") or None
                    for spk_id, spk_info in _review_speaker_styles.items():
                        name = spk_info.get("name", "")
                        if name:
                            _review_speaker_names[spk_id] = name
                            if spk_id == "speaker_a" and speaker_name_a_is_placeholder:
                                speaker_name_a = name
                            elif spk_id == "speaker_b" and speaker_name_b_is_placeholder:
                                speaker_name_b = name
                    print(f"[S2] 已有审校结果，跳过重新审校（{len(_review_speaker_names)} 位说话人，{len(_review_glossary)} 条术语）")
                    if _review_display_title_zh and config.job_id:
                        _report_source_metadata(
                            config.job_id,
                            display_name=_review_display_title_zh,
                            stage_label="S2",
                        )
                except Exception as _s2_err:
                    print(f"[S2] 加载缓存审校结果失败 ({_s2_err})，将重新审校...")
                    s2_cache_hit = False
            elif config.skip_review:
                print("[S2] Skipping review (--skip-review).")

            if not s3_cache_hit and not s2_cache_hit and not config.skip_review:
                print("[S2] Running unified LLM transcript review (audio + text)...")
                try:
                    from services.transcript_reviewer import review_transcript

                    # Load words data for split point estimation
                    _words_data: list[dict] | None = None
                    try:
                        import json as _json
                        raw_path = transcript_result.raw_response_path
                        if raw_path and Path(raw_path).exists():
                            with open(raw_path) as _f:
                                _raw = _json.load(_f)
                            _words_data = _raw.get("words")
                    except Exception:
                        pass

                    # Express mode: skip Pass 1 (speaker identification)
                    _is_express = job_service_mode == "express"
                    review_result = review_transcript(
                        transcript_result.lines,
                        audio_path=source_audio_path if source_audio_path.exists() else None,
                        video_title=download_result.video_title,
                        video_url=normalized_url,
                        words_data=_words_data,
                        debug_output_dir=final_project_dir / "transcript",
                        mode=job_service_mode,
                        skip_pass1=False,
                        usage_meter=usage_meter,
                    )

                    if review_result is not None:
                        if review_result.debug_artifacts:
                            print("[S2] Debug artifacts:")
                            raw_debug_path = review_result.debug_artifacts.get("raw_response_path")
                            diff_debug_path = review_result.debug_artifacts.get("speaker_diff_path")
                            if raw_debug_path:
                                print(f"  raw_response -> {raw_debug_path}")
                            if diff_debug_path:
                                print(f"  speaker_diff -> {diff_debug_path}")
                        # Update speaker names from review (all speakers, not just A/B)
                        _review_speaker_names: dict[str, str] = {}
                        for spk_id, spk_info in review_result.speakers.items():
                            name = spk_info.get("name", "")
                            if name:
                                _review_speaker_names[spk_id] = name
                                if spk_id == "speaker_a" and speaker_name_a_is_placeholder:
                                    speaker_name_a = name
                                elif spk_id == "speaker_b" and speaker_name_b_is_placeholder:
                                    speaker_name_b = name

                        print("[S2] Speaker identity result:")
                        for spk_id in sorted(review_result.speakers.keys()):
                            spk_name = _review_speaker_names.get(spk_id, spk_id)
                            print(f"  {spk_id.replace('speaker_', 'Speaker ').replace('_', ' ').title()} -> {spk_name}")

                        # Apply corrections to transcript
                        if review_result.corrections_applied > 0:
                            print(f"[S2] Applied {review_result.corrections_applied} correction(s).")
                            for orig, rev in zip(transcript_result.lines, review_result.lines):
                                if orig.speaker_id != rev.speaker_id:
                                    duration_seconds = round((orig.end_ms - orig.start_ms) / 1000, 1)
                                    time_minutes = orig.start_ms // 60000
                                    time_seconds = (orig.start_ms % 60000) // 1000
                                    print(
                                        f"    #{orig.index} ({time_minutes:02d}:{time_seconds:02d}, "
                                        f"{duration_seconds}s) {orig.speaker_id} -> {rev.speaker_id}: "
                                        f"\"{orig.source_text[:50]}\""
                                    )
                            transcript_result = TranscriptResult(
                                lines=review_result.lines,
                                total_duration_ms=transcript_result.total_duration_ms,
                                language=transcript_result.language,
                                raw_response_path=transcript_result.raw_response_path,
                                structured_transcript_path=transcript_result.structured_transcript_path,
                            )
                            self._write_transcript_result(transcript_result)

                        # Save glossary and styles for translation stage
                        _review_glossary = review_result.glossary
                        _review_speaker_styles = review_result.speakers
                        _review_display_title_zh = getattr(review_result, "display_title_zh", None)
                        if _review_display_title_zh:
                            print(f"[S2] 中文任务名：{_review_display_title_zh}")
                            if config.job_id:
                                _report_source_metadata(
                                    config.job_id,
                                    display_name=_review_display_title_zh,
                                    stage_label="S2",
                                )

                        print(f"[S2] Glossary: {len(_review_glossary)} terms")
                        print(f"[S2] Lines: {len(transcript_result.lines)} (was {len(review_result.lines)})")
                    else:
                        print("[S2] Unified review returned None, falling back to legacy...")
                        # Fallback: use old separate calls
                        transcript_result, speaker_name_a, speaker_name_b = (
                            self._legacy_speaker_inference_and_review(
                                translator, transcript_result, effective_speakers,
                                speaker_name_a, speaker_name_b, speaker_name_a_is_placeholder,
                                speaker_name_b_is_placeholder, download_result, normalized_url,
                            )
                        )
                        # Minimal speaker profiling — fill gender/age so matcher doesn't blind-fallback
                        if not _review_speaker_styles:
                            _review_speaker_styles = self._fallback_minimal_speaker_styles(
                                effective_speakers=effective_speakers,
                                speaker_name_a=speaker_name_a,
                                speaker_name_b=speaker_name_b,
                                speaker_ids=self._detect_speaker_ids(transcript_result.lines),
                            )
                except Exception as exc:
                    print(f"[S2] Unified review failed ({exc}), falling back to legacy...")
                    transcript_result, speaker_name_a, speaker_name_b = (
                        self._legacy_speaker_inference_and_review(
                            translator, transcript_result, effective_speakers,
                            speaker_name_a, speaker_name_b, speaker_name_a_is_placeholder,
                            speaker_name_b_is_placeholder, download_result, normalized_url,
                        )
                    )
                    # Minimal speaker profiling — fill gender/age so matcher doesn't blind-fallback
                    if not _review_speaker_styles:
                        _review_speaker_styles = self._fallback_minimal_speaker_styles(
                            effective_speakers=effective_speakers,
                            speaker_name_a=speaker_name_a,
                            speaker_name_b=speaker_name_b,
                            speaker_ids=self._detect_speaker_ids(transcript_result.lines),
                        )

            approved_speaker_review = self._get_approved_review_payload(
                review_state_manager,
                SPEAKER_REVIEW_STAGE,
            )
            if approved_speaker_review is not None:
                transcript_result = self._apply_speaker_review_payload(
                    transcript_result=transcript_result,
                    payload=approved_speaker_review,
                )
                speaker_name_a, speaker_name_b = self._resolve_speaker_names_from_review_payload(
                    payload=approved_speaker_review,
                    fallback_speaker_a=speaker_name_a,
                    fallback_speaker_b=speaker_name_b,
                )
                self._write_transcript_result(transcript_result)
                print("[S2] Applied approved speaker review overrides.")
            elif config.wait_for_review:
                # Unified review: skip speaker review pause, auto-approve
                # Speaker adjustments merged into translation review
                self._write_transcript_result(transcript_result)
                print("[S2] 说话人审核已合并到统一审核，自动跳过。")

            _speaker_structure_profiles = self._build_speaker_structure_profiles(
                transcript_result.lines,
                speaker_styles=_review_speaker_styles,
            )
            self._log_speaker_structure_profiles(_speaker_structure_profiles)

            if s3_cache_hit:
                print("[S3] 已有翻译结果，跳过翻译")
                translation_execution_mode = "cache_restore_full"
                translation_result = self._load_translation_result(segments_path)
                dubbing_modes_synced = self._apply_transcript_dubbing_modes_to_segments(
                    translation_result.segments,
                    transcript_result.lines,
                )
                speaker_name_a, speaker_name_b = self._resolve_cached_display_names(
                    translation_result,
                    fallback_speaker_a=speaker_name_a,
                    fallback_speaker_b=speaker_name_b,
                )
                if self._segments_missing_review_speaker_styles(translation_result.segments):
                    _review_speaker_styles = self._recover_review_speaker_styles(
                        transcript_result=transcript_result,
                        source_audio_path=source_audio_path,
                        video_title=download_result.video_title,
                        video_url=normalized_url,
                        cached_segments=translation_result.segments,
                    )
                    self._apply_review_speaker_styles_to_segments(
                        translation_result.segments,
                        _review_speaker_styles,
                    )
                    self._apply_speaker_structure_profiles_to_segments(
                        translation_result.segments,
                        _speaker_structure_profiles,
                    )
                if _review_speaker_styles or _speaker_structure_profiles or dubbing_modes_synced:
                    self._write_segments_snapshot(translation_result)
            else:
                translation_execution_mode = "fresh_run"
                translation_result = None

            voice_registry_path = self._resolve_voice_registry_path()

            # --- voice_review gate (always triggered) ---
            approved_voice_review = self._get_approved_review_payload(
                review_state_manager,
                VOICE_REVIEW_STAGE,
            )
            if approved_voice_review is not None:
                # Apply approved voice selections
                approved_voice_a = _normalize_optional_text(approved_voice_review.get("voice_id_a"))
                approved_voice_b = _normalize_optional_text(approved_voice_review.get("voice_id_b"))
                if approved_voice_a:
                    voice_id_a = approved_voice_a
                    print(f"[S2] 用户确认 Speaker A 音色: {voice_id_a}")
                if approved_voice_b:
                    voice_id_b = approved_voice_b
                    print(f"[S2] 用户确认 Speaker B 音色: {voice_id_b}")
            else:
                # Express (non-interactive) mode: skip registry lookup and auto-clone.
                # Leave voice_id_a / voice_id_b as None — downstream TTS voice
                # matcher will auto-select from the preset catalog based on S2
                # speaker profiles (gender, age_group, voice_description).
                print("[S2] 快捷模式：跳过音色库查找和自动克隆，由下游自动匹配音色。")

            # --- translation_config_review gate ---
            # Previously this gate paused the pipeline and waited for user
            # approval, but the frontend always auto-approved immediately.
            # This caused the entire pipeline to restart from scratch (S0→S1→S2)
            # just to process the auto-approval, wasting LLM API calls.
            # Now we always auto-resolve with defaults and continue directly.
            approved_translation_config = self._get_approved_review_payload(
                review_state_manager,
                TRANSLATION_CONFIG_REVIEW_STAGE,
            )
            if approved_translation_config is None and not s3_cache_hit:
                default_model = _get_prompt_model(job_service_mode, "translate")
                print(f"[S3] 自动使用默认翻译模型: {default_model} (service_mode={job_service_mode})")
                approved_translation_config = {
                    "selected_model": default_model,
                    "prompt_template": None,
                }

            # Apply translation config from approved review if available
            if approved_translation_config is not None:
                selected_model = approved_translation_config.get("selected_model")
                custom_prompt = approved_translation_config.get("prompt_template")
                if selected_model:
                    print(f"[S3] 用户选择翻译模型：{selected_model}")
                if custom_prompt:
                    print("[S3] 用户提供了自定义翻译提示词。")

            # Build speaker_voices dict for N-speaker support
            _speaker_voices: dict[str, str] = {}
            if voice_id_a:
                _speaker_voices["speaker_a"] = voice_id_a
            if voice_id_b:
                _speaker_voices["speaker_b"] = voice_id_b
            # For speakers beyond a/b, "auto" lets TTS matcher choose
            if effective_speakers > 2:
                for i in range(2, effective_speakers):
                    spk_id = f"speaker_{chr(ord('a') + i)}"
                    _speaker_voices.setdefault(spk_id, "auto")

            # --- Pass 3: voice profiling (before voice selection, before translation) ---
            _pass3_cache_path = (final_project_dir / "transcript" / "s2_pass3_result.json").resolve(strict=False)
            # plan 2026-06-12 §E：匿名按 lane 分流——free 跳过不变；express
            # 必跑 + artifact 判定 + 120s 超时 + 诚实失败（不降级出片）。
            _anon_express_pass3 = (
                job_anonymous_preview
                and str(job_service_mode or "").strip() == "express"
            )
            # CodeX 第四轮 P2：超时是硬失败信号（plan §E"超时按失败处理"
            # 的字面语义）。daemon 线程超时后仍可能在 artifact 判定前恰好
            # 写出 s2_pass3_result.json——但 profiles 没有 merge 回
            # _review_speaker_styles，放行会出错误音色预览。
            _anon_express_pass3_timed_out = False
            if _review_speaker_styles and job_anonymous_preview and not _anon_express_pass3:
                print("[S2.5] 匿名预览任务：跳过 Pass 3 音色画像（付费多模态调用，APF P0 T5）", flush=True)
            if _should_run_pass3(_review_speaker_styles, job_anonymous_preview, job_service_mode):
                _pass3_profiles: dict | None = None
                if _pass3_cache_path.exists():
                    try:
                        import json as _json
                        _pass3_data = _json.loads(_pass3_cache_path.read_text(encoding="utf-8"))
                        _pass3_profiles = _pass3_data.get("speaker_profiles", {})
                        print(f"[S2.5] Pass 3 cache hit: {len(_pass3_profiles)} speaker profiles restored", flush=True)
                    except Exception as exc:
                        print(f"[S2.5] Pass 3 cache read failed: {exc}", flush=True)
                if not _pass3_profiles:
                    try:
                        from services.transcript_reviewer import review_pass3_voice_profiles

                        print("[S2.5] Running Pass 3: voice profiling...", flush=True)
                        _pass3_kwargs = dict(
                            source_audio_path=source_audio_path if source_audio_path.exists() else None,
                            speakers=_review_speaker_styles,
                            video_title=download_result.video_title,
                            debug_output_dir=final_project_dir / "transcript",
                            mode=job_service_mode,
                            usage_meter=usage_meter,
                        )
                        if _anon_express_pass3:
                            # §E：超时落在调用点（不改 review_pass3 内部签名
                            # transcript_reviewer.py:1945）。CodeX 复审 P2：
                            # 不用 ThreadPoolExecutor——超时后其非 daemon
                            # worker 既停不下来，进程退出时 concurrent.futures
                            # 的 atexit 钩子还会 join 它，挂死的 Pass 3 调用
                            # 会拖住整个 pipeline 进程不退出。改用 daemon
                            # 线程 + join(timeout)：超时即放弃，任务失败后
                            # 进程退出时 daemon 线程随进程终止，挂死的付费
                            # 调用不再延命（残余窗口=失败到进程退出的秒级
                            # 间隙，成本上界为单次调用）。
                            import threading as _threading

                            _pass3_box: dict = {}

                            def _pass3_worker(_box=_pass3_box, _kwargs=_pass3_kwargs):
                                try:
                                    _box["profiles"] = review_pass3_voice_profiles(
                                        transcript_result.lines, **_kwargs
                                    )
                                except BaseException as _exc:  # noqa: BLE001
                                    _box["error"] = _exc
                            _t = _threading.Thread(
                                target=_pass3_worker,
                                daemon=True,
                                name="anon-express-pass3",
                            )
                            _t.start()
                            _t.join(timeout=ANON_EXPRESS_PASS3_TIMEOUT_SECONDS)
                            if _t.is_alive():
                                _anon_express_pass3_timed_out = True
                                print(
                                    "[S2.5] 匿名 express Pass 3 超时"
                                    f"（>{ANON_EXPRESS_PASS3_TIMEOUT_SECONDS}s）"
                                    "——放弃等待，按失败处理",
                                    flush=True,
                                )
                            elif "error" in _pass3_box:
                                raise _pass3_box["error"]
                            else:
                                _pass3_profiles = _pass3_box.get("profiles")
                        else:
                            _pass3_profiles = review_pass3_voice_profiles(
                                transcript_result.lines,
                                **_pass3_kwargs,
                            )
                    except Exception as exc:
                        # 匿名 express 不在此处抛——成功与否统一由下方
                        # artifact 判定裁决（内部 retry+fallback 会吞错，
                        # 异常缺席不等于成功，artifact 才是唯一可信信号）。
                        print(f"[S2.5] Pass 3 failed (non-fatal): {exc}", flush=True)
                if _pass3_profiles:
                    for spk_id, profile in _pass3_profiles.items():
                        if spk_id in _review_speaker_styles:
                            existing = _review_speaker_styles[spk_id]
                            for key in (
                                "style",
                                "voice_description",
                                "gender",
                                "age_group",
                                "persona_style",
                                "energy_level",
                                "is_non_speech",
                                "non_speech_reason",
                            ):
                                val = profile.get(key, "")
                                if val is not None and val != "":
                                    existing[key] = val
                    _speaker_structure_profiles = self._build_speaker_structure_profiles(
                        transcript_result.lines,
                        speaker_styles=_review_speaker_styles,
                    )
                    self._log_speaker_structure_profiles(_speaker_structure_profiles)
                    print(f"[S2.5] Pass 3 voice profiles: {len(_pass3_profiles)} speakers", flush=True)

            # --- 匿名 express Pass 3 诚实失败判定（plan 2026-06-12 §E）---
            # 失败条件（任一命中 → terminal failed，绝不降级出片）：
            # ① 120s 超时（CodeX 第四轮 P2：硬失败——超时后 daemon 线程
            #    可能在本判定前恰好写出 artifact，但 profiles 没有 merge 回
            #    _review_speaker_styles，放行会出错误音色预览）；
            # ② artifact 缺失 / profiles 为空（含 speaker styles 为空导致
            #    根本没跑 Pass 3 的情形）；
            # ③ 未覆盖全部说话人（CodeX 第三轮 P2：部分产物=部分说话人无
            #    gender 进音色匹配，全员回落 longanyang）。
            if _anon_express_pass3 and (
                _anon_express_pass3_timed_out
                or not _anon_express_pass3_artifact_ok(
                    _pass3_cache_path,
                    expected_speaker_ids=(
                        _review_speaker_styles.keys() if _review_speaker_styles else ()
                    ),
                )
            ):
                from services.smart.state import emit_smart_state_marker

                # gateway 终态镜像（mirror_job_terminal_state 单一结算入口）
                # 凭此 marker 退还 per-scope per-mode 配额（global 总闸与
                # express 子闸不退，防刷失败穿透成本闸）。
                emit_smart_state_marker({"anon_pass3_failed": True})
                print(
                    "[S2.5] 匿名 express Pass 3 诚实失败：artifact 判定不过"
                    "（s2_pass3_result.json 缺失/为空或 120s 超时）——任务终止",
                    flush=True,
                )
                raise AnonymousExpressPass3Failed(
                    "预览生成失败：声音画像分析未完成，请稍后点击「重试」"
                )

            # --- S4-probe Phase 1: 预翻译（音色确认前） ---
            _probe_segments: list[DubbingSegment] = []
            if not s3_cache_hit:
                try:
                    _probe_segments = self._run_probe_translation(
                        transcript_result.lines,
                        translator,
                        cache_dir=final_project_dir / "translation",
                        video_title=download_result.video_title,
                        youtube_url=normalized_url,
                        glossary=_review_glossary or None,
                        speaker_voices=_speaker_voices if effective_speakers > 2 else None,
                        voice_id_a=voice_id_a,
                        display_name_a=speaker_name_a,
                        voice_id_b=voice_id_b,
                        display_name_b=speaker_name_b if effective_speakers >= 2 else None,
                    )
                except Exception as exc:
                    print(f"[S4-probe] 探针翻译异常（非致命）：{exc}")
                    # logger removed — process.py uses print() for logging

            # B': probe 段补说话人画像（gender/age_group/persona_style/energy_level +
            # voice_description + structure 角色）。translator.translate_probe 创建的、以及
            # probe cache 加载回来的 DubbingSegment 这些字段默认空，导致 probe TTS 校准用空
            # 画像匹配音色并反复刷 "[CosyVoice] empty gender" 警告，cps 估计失真。
            # 此处套用与正式段相同的两个 apply（在 _run_probe_translation 返回后、probe TTS
            # 之前），同时覆盖 fresh 与 cache-restore 两条路径，无需再改 probe cache schema。
            # 两个 apply 对空 dict 都是安全 no-op：Pass 3 未跑 / styles 为空时自动跳过；
            # _review_speaker_styles 在 :3086 初始化、_speaker_structure_profiles 在 :3276
            # 无条件赋值（Pass 3 enrichment 在 :3485 之前已 merge），故此处二者必已定义。
            # 仅影响 probe 字/秒校准准确度——不改最终音色 / 克隆 / 人工审核语义。
            if _probe_segments:
                self._apply_review_speaker_styles_to_segments(
                    _probe_segments,
                    _review_speaker_styles,
                )
                self._apply_speaker_structure_profiles_to_segments(
                    _probe_segments,
                    _speaker_structure_profiles,
                )

            # --- voice_selection_review gate (Studio mode, BEFORE translation) ---
            approved_voice_selection = self._get_approved_review_payload(
                review_state_manager,
                VOICE_SELECTION_REVIEW_STAGE,
            )
            _speaker_providers: dict[str, str] = {}
            # Phase 4.1 E.4 (Codex 2026-05-25 三签字版本)：从 approved payload
            # 提取 worker routing 元数据（由 gateway/job_intercept.py 的
            # _enrich_speakers_with_clone_routing 在 approve 时注入）。
            # Schema: {speaker_id: {requires_worker, worker_target_model}}
            _speaker_voice_routing: dict[str, dict[str, object]] = {}

            # --- Phase 4.3a PR2-F: Express auto-clone (CosyVoice via mainland worker) ---
            # Single entry; ALL gates (admin master switch default-OFF / worker
            # env / user consent / allowlist) live inside
            # maybe_run_express_auto_clone — orchestrator logic stays in
            # services.express.*, this call site is intentionally tiny. With the
            # admin switch off (production default) it is a no-op, so Express
            # behaves exactly as before PR2. On success it injects the clone
            # voice_id + worker routing into _speaker_voices /
            # _speaker_voice_routing in place; any failure leaves them untouched
            # so downstream falls back to a CosyVoice preset (never MiniMax).
            # PR3 wires the frontend consent UI; until then most jobs skip here.
            if job_service_mode == "express" and approved_voice_selection is None:
                try:
                    from services.express.pipeline_clients import (
                        maybe_run_express_auto_clone as _maybe_express_auto_clone,
                    )

                    _maybe_express_auto_clone(
                        user_id=_snap("user_id", "") or "",
                        job_id=config.job_id or "",
                        project_dir=final_project_dir,
                        source_audio_path=source_audio_path,
                        transcript_lines=transcript_result.lines,
                        speaker_voices=_speaker_voices,
                        speaker_routing=_speaker_voice_routing,
                        express_consent=_snap("express_consent", None),
                        # plan 2026-06-14 §3.4：匿名 express 走匿名主开关 + 全局 cap
                        # 分支（L1'/L3'）；登录态 express 不变。
                        anonymous_preview=job_anonymous_preview,
                    )
                    # Mirror any injected clone voice into the a/b vars the
                    # translation/display path reads (matches the Studio
                    # approved-selection sync below).
                    voice_id_a = _speaker_voices.get("speaker_a", voice_id_a)
                    voice_id_b = _speaker_voices.get("speaker_b", voice_id_b)
                except Exception as _express_exc:  # never fatal — preset fallback
                    print(
                        f"[S2.5] Express auto-clone 异常（非致命，回预设音色）："
                        f"{type(_express_exc).__name__}: {_express_exc}",
                        flush=True,
                    )

            if approved_voice_selection is not None:
                sel_speakers = approved_voice_selection.get("speakers")
                if isinstance(sel_speakers, list):
                    for sp in sel_speakers:
                        sp_id = sp.get("speaker_id", "")
                        sp_voice = sp.get("voice_id", "")
                        sp_prov = sp.get("tts_provider", "")
                        if sp_id and sp_voice:
                            _speaker_voices[sp_id] = sp_voice
                        if sp_id and sp_prov:
                            _speaker_providers[sp_id] = sp_prov
                        # E.4: 提取 routing 字段（仅当存在 + requires_worker=True）
                        if sp_id and sp.get("requires_worker") is True:
                            target_model = str(
                                sp.get("worker_target_model", "") or ""
                            ).strip()
                            if target_model:
                                _speaker_voice_routing[sp_id] = {
                                    "requires_worker": True,
                                    "worker_target_model": target_model,
                                }
                    voice_id_a = _speaker_voices.get("speaker_a", voice_id_a)
                    voice_id_b = _speaker_voices.get("speaker_b", voice_id_b)
                    print(f"[S2.5] 用户确认音色：{_speaker_voices}")
                    if _speaker_providers:
                        print(f"[S2.5] 用户选择引擎：{_speaker_providers}")
                    if _speaker_voice_routing:
                        print(
                            f"[S2.5] worker routing：{list(_speaker_voice_routing.keys())} "
                            f"→ mainland CosyVoice worker (Phase 4.1 D + E)"
                        )

                    # Lazy migration: legacy approved payloads (created before
                    # review_actions.py merge-instead-of-replace fix) lack the
                    # `auto_matched_by_provider` recommendation context. The
                    # frontend Smart-Recommendation dropdown can't render
                    # backups without it.  Detect & rebuild once, then preserve
                    # the user's choices on top.
                    needs_refresh = (
                        config.wait_for_review
                        and job_service_mode == "studio"
                        and not any(
                            sp.get("auto_matched_by_provider")
                            for sp in sel_speakers
                            if isinstance(sp, dict)
                        )
                    )
                    if needs_refresh:
                        try:
                            refreshed_payload = self._build_voice_selection_review_payload(
                                transcript_result=transcript_result,
                                tts_provider=job_tts_provider,
                                service_mode=job_service_mode,
                                source_audio_path=source_audio_path,
                                effective_speakers=effective_speakers,
                                speaker_names=_merge_speaker_name_map(
                                    _review_speaker_names,
                                    speaker_name_a,
                                    speaker_name_b,
                                ),
                                speaker_styles=_review_speaker_styles,
                                probe_segments=_probe_segments or None,
                                speaker_structure_profiles=_speaker_structure_profiles,
                            )
                            user_choice_by_sid = {
                                str(sp.get("speaker_id", "")): sp
                                for sp in sel_speakers
                                if isinstance(sp, dict)
                            }
                            refreshed_speakers = []
                            for sp in (refreshed_payload.get("speakers") or []):
                                if not isinstance(sp, dict):
                                    continue
                                sid = str(sp.get("speaker_id", ""))
                                merged = dict(sp)
                                if sid in user_choice_by_sid:
                                    user_sp = user_choice_by_sid[sid]
                                    if user_sp.get("voice_id"):
                                        merged["voice_id"] = user_sp.get("voice_id")
                                    merged["voice_source"] = user_sp.get(
                                        "voice_source", merged.get("voice_source", "auto_matched"),
                                    )
                                    if user_sp.get("tts_provider"):
                                        merged["tts_provider"] = user_sp.get("tts_provider")
                                refreshed_speakers.append(merged)
                            refreshed_payload["speakers"] = refreshed_speakers
                            review_state_manager.set_stage(
                                VOICE_SELECTION_REVIEW_STAGE,
                                status=REVIEW_STATUS_APPROVED,
                                payload=refreshed_payload,
                            )
                            print(
                                "[S2.5] Legacy payload migrated: rebuilt "
                                "auto_matched_by_provider for smart-recommendation UI"
                            )
                        except Exception as exc:
                            # Migration is best-effort; pipeline continues with
                            # legacy payload (frontend just won't show backups).
                            print(f"[S2.5] payload migration skipped: {exc}")
            elif config.wait_for_review and job_requires_review and job_effective_pipeline_mode in {"studio", "smart"}:
                # Plan §6.0.5 + §6.2.1 + §6.0.6 + Codex 第七轮 F2 +
                # 第十六轮 PR#3C-b2 + 第十八轮 P1-2: this is a pipeline-
                # control branch so it must read job_effective_pipeline_mode,
                # not raw job_service_mode. After handoff the effective
                # mode flips to "studio" so /continue routes through the
                # Studio human-review pause-return path; raw service_mode
                # stays "smart" for billing/audit/payload purposes only.
                #
                # Smart MUST NOT pause-return here — it inline auto-approves
                # voice selection via evaluate_voice_review, applying the
                # per-speaker decision to _speaker_voices in this same frame,
                # then falls through. Only on PAUSED outcome (consent denial
                # / quota exhaust mid-flight / provider failure exhausted)
                # does smart emit handoff markers + pause-return.
                vs_payload = self._build_voice_selection_review_payload(
                    transcript_result=transcript_result,
                    tts_provider=job_tts_provider,
                    service_mode=job_service_mode,
                    source_audio_path=source_audio_path,
                    effective_speakers=effective_speakers,
                    speaker_names=_merge_speaker_name_map(
                        _review_speaker_names,
                        speaker_name_a,
                        speaker_name_b,
                    ),
                    speaker_styles=_review_speaker_styles,
                    probe_segments=_probe_segments or None,
                    speaker_structure_profiles=_speaker_structure_profiles,
                )

                if job_effective_pipeline_mode == "smart":
                    # --- Smart inline auto-approve path ---
                    import uuid as _smart_uuid
                    from services.smart.auto_voice_review import (
                        VoiceReviewChoice,
                        VoiceReviewExistingMatch,
                        VoiceReviewOutcome,
                        VoiceReviewSpeakerInput,
                        evaluate_voice_review,
                    )
                    from services.smart.eligibility_gate import (
                        aggregate_segment_dubbing_modes_to_speaker,
                        evaluate_eligibility,
                    )
                    from services.smart.handoff import emit_handoff_markers
                    from services.smart.state import emit_smart_state_marker

                    smart_consent = _snap("smart_consent", {}) or {}

                    # PR#3C-b3b: eligibility gate runs BEFORE voice
                    # auto-approve. Inputs:
                    #   - speaker_structure_profiles from S2 (carries
                    #     speaker_role + speaker_duration_share)
                    #   - line-level dubbing_mode aggregated to speaker
                    #     level via fail-closed reducer (mixed / missing →
                    #     "dub" so the speaker counts toward
                    #     main_speaker_count limit; Codex 第二十二轮 warning).
                    # ``evaluate_eligibility`` accepts the
                    # speaker_structure_profiles dict shape directly via
                    # ``normalize_speaker_stats`` (PR#3A-fix2 P1-1) and
                    # picks up the per-speaker ``dubbing_mode`` overlay
                    # we attach below.
                    #
                    # Codex 第二十三轮 P1: input is ``transcript_result.lines``
                    # not ``.segments`` — TranscriptResult only carries
                    # ``lines: list[TranscriptLine]`` (see
                    # ``src/services/assemblyai/transcriber.py``) and
                    # each TranscriptLine has ``speaker_id`` +
                    # ``dubbing_mode``. The earlier ``.segments`` getattr
                    # always returned None → aggregation was {} → every
                    # speaker overlay defaulted to "dub" → keep_original
                    # / mute_or_background exclusions silently disabled.
                    # Matches the existing _build_speaker_structure_profiles
                    # call convention at process.py:2055.
                    _smart_speaker_dubbing_modes = (
                        aggregate_segment_dubbing_modes_to_speaker(
                            getattr(transcript_result, "lines", None) or []
                        )
                    )
                    # Overlay dubbing_mode onto the speaker_structure_profiles
                    # dict so normalize_speaker_stats's process.py shape
                    # branch picks it up (default "dub" otherwise).
                    _smart_eligibility_input: dict[str, dict[str, object]] = {}
                    for _sid, _profile in (_speaker_structure_profiles or {}).items():
                        if not isinstance(_profile, dict):
                            continue
                        _enriched = dict(_profile)
                        _enriched["dubbing_mode"] = (
                            _smart_speaker_dubbing_modes.get(_sid, "dub")
                        )
                        _smart_eligibility_input[_sid] = _enriched

                    _smart_eligibility = evaluate_eligibility(
                        _smart_eligibility_input
                    )

                    if not _smart_eligibility.approved:
                        # Eligibility rejection → handoff. Common
                        # reason_codes: ``main_speaker_count_exceeded``
                        # (> 3 main speakers) or ``no_speakers_detected``
                        # (upstream data hole; defensive).
                        _emit_smart_audit(
                            final_project_dir,
                            decision_type="speaker_gate",
                            decision="rejected",
                            reason_code=_smart_eligibility.reason_code,
                            evidence={
                                "main_speaker_count": (
                                    _smart_eligibility.main_speaker_count
                                ),
                                "main_speaker_ids": list(
                                    _smart_eligibility.main_speaker_ids
                                ),
                                "excluded_speakers": list(
                                    _smart_eligibility.excluded_speakers
                                ),
                                "threshold_used": (
                                    _smart_eligibility.threshold_used
                                ),
                                "limit_used": _smart_eligibility.limit_used,
                            },
                            extra={
                                "job_id": str(_snap("job_id") or ""),
                                "user_id": str(_snap("user_id") or ""),
                                "handoff_stage": VOICE_SELECTION_REVIEW_STAGE,
                            },
                        )
                        emit_handoff_markers(
                            review_state_manager=review_state_manager,
                            review_stage=VOICE_SELECTION_REVIEW_STAGE,
                            review_payload=vs_payload,
                            review_pending_status=REVIEW_STATUS_PENDING,
                            smart_state_update={
                                "status": "downgraded_to_studio",
                                "reason": _smart_eligibility.reason_code
                                or "eligibility_rejected",
                                "handoff_stage": VOICE_SELECTION_REVIEW_STAGE,
                                "main_speaker_count": (
                                    _smart_eligibility.main_speaker_count
                                ),
                            },
                            project_dir=final_project_dir,
                            user_message=(
                                "智能版主要说话人超出上限或数据异常,请人工接管:"
                                f" {_smart_eligibility.reason_code}"
                            ),
                            web_review_marker_builder=self._build_web_review_marker,
                        )
                        state_manager.set_stage(
                            "voice_selection",
                            StageStatus.RUNNING,
                            {"execution_mode": "smart_handoff_eligibility"},
                        )
                        current_stage_name = None
                        _write_usage_summary(usage_meter)
                        # Codex 第三十六轮 P1: write cost_summary at smart
                        # handoff returns too (decision log §2 contract).
                        _emit_smart_cost_summary_from_meter(
                            final_project_dir,
                            job_id=config.job_id,
                            usage_meter=usage_meter,
                            minutes_processed=(
                                # Codex 第三十七轮 P1: prefer ffprobe
                                # actual_duration_ms over unreliable _snap.
                                float(actual_duration_ms) / 60000.0
                                if actual_duration_ms
                                else float(
                                    _snap("source_duration_seconds") or 0.0
                                ) / 60.0
                            ),
                            credits_policy="pending_settle",
                        )
                        return self._build_paused_result(
                            project_dir=final_project_dir,
                            stage=VOICE_SELECTION_REVIEW_STAGE,
                            message="智能版资格检查未通过,请人工接管",
                        )

                    # Eligibility approved: use the eligibility-vetted
                    # main_speaker_ids as the basis for voice_review,
                    # filtered down from vs_payload entries. The earlier
                    # PR#3C-b2 took vs_payload speakers verbatim, but
                    # that bypassed the keep_original / low_share /
                    # role-based exclusion the gate enforces — main
                    # speakers from vs_payload could include speakers
                    # the gate would have excluded, so smart would burn
                    # clone retry budget on speakers Studio-human-review
                    # would never have offered to clone.
                    _smart_main_speaker_ids = set(_smart_eligibility.main_speaker_ids)

                    # Sidecar audit: eligibility approved.
                    _emit_smart_audit(
                        final_project_dir,
                        decision_type="speaker_gate",
                        decision="approved",
                        evidence={
                            "main_speaker_count": (
                                _smart_eligibility.main_speaker_count
                            ),
                            "main_speaker_ids": list(
                                _smart_eligibility.main_speaker_ids
                            ),
                            "excluded_speakers": list(
                                _smart_eligibility.excluded_speakers
                            ),
                            "threshold_used": (
                                _smart_eligibility.threshold_used
                            ),
                            "limit_used": _smart_eligibility.limit_used,
                        },
                        extra={
                            "job_id": str(_snap("job_id") or ""),
                            "user_id": str(_snap("user_id") or ""),
                        },
                    )

                    # Construct main_speakers candidates filtered through
                    # _smart_main_speaker_ids — only speakers the
                    # eligibility gate marked "main configured dubbing
                    # speakers" per plan §6.1 reach evaluate_voice_review.
                    #
                    # Codex 第二十轮 three-piece contract, fully landed
                    # at PR#3C-b3e:
                    # 1. Real per-speaker ffmpeg sample (VoiceSampleExtractor)
                    #    + validate_sample() + duration ≥10s gate (b3d)
                    # 2. Real voice_library_quota_remaining snapshot
                    #    (Gateway internal endpoint, b3e)
                    # 3. Real CloneProvider (build_smart_clone_provider, b3e)
                    #
                    # All three are now wired. Safety chain for any real
                    # MiniMax clone API call:
                    #   - consent.auto_voice_clone is True (strict identity)
                    #   - _smart_sample_extraction_error is None (real
                    #     per-speaker WAV ≥10s on disk)
                    #   - _smart_quota_remaining is a real int from
                    #     Gateway, ≥ §7.3 water mark (default 3)
                    # Each layer fail-closed handoffs to Studio when not
                    # satisfied — the real provider only sees clean inputs.
                    #
                    # b3e atomic invariant (Codex 第二十七轮 P0): the real
                    # quota helper call AND build_smart_clone_provider()
                    # must coexist in the smart inline branch. The
                    # regression test
                    # test_b3e_atomic_invariant_quota_and_provider_move_together
                    # locks this so a partial revert fails the test.

                    # ── Piece 1: extract per-speaker clone sample ──
                    #
                    # Only attempt when consent.auto_voice_clone is True
                    # AND admin policy allows new clone. Otherwise
                    # evaluate_voice_review routes all (no-match) speakers
                    # to PRESET (no clone call), making sample extraction
                    # wasted work. Fail-closed: if ANY main speaker's
                    # sample extraction raises, route the WHOLE smart
                    # job to handoff so the real provider never receives
                    # a stub / missing audio path.
                    _smart_consent_allows_clone = (
                        smart_consent.get("auto_voice_clone") is True
                    )
                    # Phase 3 (plan 2026-05-17-user-voice-candidate-first
                    # §后台策略字段): read admin policy switches. Defaults
                    # preserve legacy behavior (reuse + clone both allowed)
                    # so missing / malformed admin_settings.json doesn't
                    # surprise existing Smart users.
                    # 2026-05-18 cleanup: previously did ``from admin_settings
                    # import load_settings``, which only resolves inside the
                    # gateway container (gateway/admin_settings.py defines a
                    # FastAPI router + pydantic model; that module is not on
                    # the app container's sys.path). Every fresh-clone Smart
                    # job on app side logged ``ModuleNotFoundError: No
                    # module named 'admin_settings'`` and fell through to
                    # the same defaults below — wasted noise + a misleading
                    # warning that suggested admin policy wasn't loading
                    # when in reality it just couldn't.
                    #
                    # Switch to ``services.admin_settings.read_admin_setting``,
                    # which is the canonical app-side reader (fail-safe,
                    # bind-mount-aware, no pydantic dep). Reads the SAME
                    # JSON file the gateway writes via load_settings /
                    # save_settings, so admin UI changes are picked up
                    # next call (the helper re-reads on every call —
                    # no module-level cache).
                    from services.admin_settings import read_admin_setting
                    _smart_admin_clone_enabled = bool(
                        read_admin_setting(
                            "smart_auto_clone_enabled", default=True
                        )
                    )
                    _smart_admin_reuse_enabled = bool(
                        read_admin_setting(
                            "smart_reuse_user_voice_enabled", default=True
                        )
                    )
                    # P3e §2：reservation marker 落在 ``smart_state`` 字典里
                    # （create/Option C 经 submit_job 预供 → JobRecord.smart_state；
                    # 与 finalizer marker-gate `_smart_clone_settle_needed` 读的
                    # 同一位置一致）。保留顶层 `_snap` 作防御 fallback。create 未
                    # 接线前 smart_state 无此键 → None（inert，gate 恒按"无 reservation"）。
                    _smart_state_snap = _snap("smart_state")
                    _smart_state_dict = (
                        _smart_state_snap if isinstance(_smart_state_snap, dict) else {}
                    )
                    # P3e reservation gate:
                    # - explicit rollout flag can still force the old global
                    #   "require reservation for any new Smart clone" behavior.
                    # - preview tasks require a reservation only when the
                    #   server-stamped smart_preview_mode marker is present.
                    # - full Smart create also writes smart_clone_credit_reserved
                    #   or smart_clone_reservation_deny_reason, so unpaid /
                    #   failed full-Smart clone attempts stay fail-closed without
                    #   letting the admin preview flag globally disable full Smart.
                    _smart_requires_reservation = (
                        read_admin_setting(
                            "smart_clone_requires_reservation", default=False
                        )
                        is True
                    ) or (
                        _smart_state_dict.get("smart_preview_mode") is True
                    ) or (
                        _smart_state_dict.get("smart_clone_credit_reserved") is True
                    ) or isinstance(
                        _smart_state_dict.get("smart_clone_reservation_deny_reason"),
                        str,
                    )
                    _smart_clone_reservation_id = (
                        str(
                            _smart_state_dict.get("smart_clone_reservation_id")
                            or _snap("smart_clone_reservation_id")
                            or ""
                        ).strip()
                        or None
                    )
                    _smart_reservation_gate_open_result = (
                        _smart_reservation_gate_open(
                            _smart_requires_reservation,
                            _smart_clone_reservation_id,
                        )
                    )
                    # CodeX P3e-1a P1：把 reservation 闸**折进** effective clone-enabled，
                    # 使「闸关」与「admin 关克隆」语义**完全一致**=evaluate_voice_review
                    # PRESET fall-through。否则只塞进 _smart_needs_new_clone 会让闸关时
                    # 仍 admin_clone_enabled=True + quota=0 → 触发 PAUSED 水线/样本抽取
                    # handoff（不是退预设）。**所有** clone 决策点（样本抽取 / needs_new
                    # _clone / 外层 if / evaluate_voice_review 入参）一律改用本 effective 值。
                    _smart_effective_clone_enabled = bool(
                        _smart_admin_clone_enabled
                        and _smart_reservation_gate_open_result
                    )
                    # Phase 4 (plan 2026-05-17 §Smart 弱匹配暂停): admin
                    # toggle. Default False so existing users see no
                    # behavior change; admin must opt in to get pauses
                    # when possible (non-strong) candidates exist.
                    _smart_admin_pause_on_possible = bool(
                        read_admin_setting(
                            "smart_pause_on_possible_user_voice_match",
                            default=False,
                        )
                    )
                    # Phase 5 (2026-05-24 P5 follow-up): auto-reuse the top
                    # possible candidate instead of pausing. Wins over
                    # admin_pause_on_possible_match when both are True.
                    # Default True so the 3 production handoffs observed in
                    # 90-day analytics stop reoccurring.
                    _smart_admin_auto_reuse_on_possible = bool(
                        read_admin_setting(
                            "smart_auto_reuse_on_possible_user_voice_match",
                            default=True,
                        )
                    )
                    _smart_existing_voice_matches: dict[str, VoiceReviewExistingMatch] = {}
                    # Phase 4 (plan 2026-05-17 §Phase 4): possible (non-strong)
                    # candidates per speaker, used by evaluate_voice_review
                    # to pause Smart when admin pause toggle is on. Stays
                    # empty when the toggle is off — no waste of HTTP cycles
                    # parsing candidates we'd ignore. Plan §候选源优先级:
                    # reuse-disabled admin policy short-circuits the whole
                    # candidate query, so this dict only fills when admin
                    # reuse AND admin pause are both True.
                    _smart_possible_voice_matches: dict[str, list[dict]] = {}
                    # Phase 3 §Consent × Admin 矩阵: reuse query is gated
                    # by admin policy ALONE, NOT consent. Consent gates
                    # new-clone (which DOES call paid provider); reuse
                    # doesn't call provider or consume quota, so consent
                    # doesn't apply. Admin can still disable the reuse
                    # path entirely via smart_reuse_user_voice_enabled
                    # (e.g. for debugging or per-library lockdown).
                    if _smart_admin_reuse_enabled:
                        _smart_user_id_for_match = str(_snap("user_id") or "")
                        _smart_source_hash_for_match = (
                            str(_snap("source_content_hash") or "") or None
                        )
                        # Phase 4: when admin enabled pause on possible,
                        # switch to the /candidates envelope which gives
                        # us BOTH the strong match (auto_reuse) AND the
                        # non-strong list (possible) in one HTTP call.
                        # When pause is off, stay on the legacy /match
                        # endpoint to keep the wire shape minimal — we'd
                        # ignore the possible list anyway.
                        # Phase 5 (2026-05-24): also trigger candidates
                        # endpoint when admin enables auto_reuse — the
                        # auto-reuse decision needs the possible list
                        # populated to pick a top candidate.
                        _smart_use_candidates_endpoint = (
                            _smart_admin_pause_on_possible
                            or _smart_admin_auto_reuse_on_possible
                        )
                        for _sp in (vs_payload.get("speakers") or []):
                            if not isinstance(_sp, dict):
                                continue
                            _sid = str(_sp.get("speaker_id") or "").strip()
                            if not _sid or _sid not in _smart_main_speaker_ids:
                                continue
                            _matched_voice = _match_smart_user_voice(
                                user_id=_smart_user_id_for_match,
                                source_content_hash=_smart_source_hash_for_match,
                                speaker_id=_sid,
                                speaker_name=str(_sp.get("speaker_name") or "") or None,
                                include_possible=_smart_use_candidates_endpoint,
                            )
                            if _matched_voice is None:
                                continue
                            # Normalize envelope: legacy /match path returns
                            # the strong-match dict directly; /candidates
                            # returns {"auto_reuse": ..., "possible": [...]}.
                            if _smart_use_candidates_endpoint:
                                _strong = _matched_voice.get("auto_reuse")
                                _possible_list = _matched_voice.get("possible") or []
                                if _possible_list:
                                    _smart_possible_voice_matches[_sid] = (
                                        list(_possible_list)
                                    )
                            else:
                                _strong = _matched_voice
                            if not isinstance(_strong, dict):
                                continue
                            if not str(_strong.get("voice_id") or "").strip():
                                continue
                            _smart_existing_voice_matches[_sid] = VoiceReviewExistingMatch(
                                voice_id=str(_strong.get("voice_id") or ""),
                                provider_name=(
                                    str(_strong.get("provider") or "")
                                    or None
                                ),
                                model_name=(
                                    str(_strong.get("tts_provider") or "")
                                    or None
                                ),
                                confidence=(
                                    str(_strong.get("confidence") or "")
                                    or None
                                ),
                                reason=(
                                    str(_strong.get("reason") or "")
                                    or None
                                ),
                                user_voice_id=(
                                    str(_strong.get("user_voice_id") or "")
                                    or None
                                ),
                            )
                    _smart_per_speaker_samples: dict[str, Path] = {}
                    _smart_per_speaker_sample_seconds: dict[str, float] = {}
                    _smart_per_speaker_segment_ids: dict[str, list[int]] = {}
                    _smart_sample_extraction_error: str | None = None
                    _smart_speaker_ids_requiring_clone = [
                        _sid for _sid in _smart_main_speaker_ids
                        if _sid not in _smart_existing_voice_matches
                    ]
                    # P3e §5 limit-1：reservation 收紧（requires_reservation 且
                    # 带 reservation_id）时，600 点只覆盖 **1 个**克隆 → 只为
                    # 「按 vs_payload 稳定顺序的第 1 个待克隆 main speaker」新建
                    # 克隆；其余待克隆 main speaker 经 evaluate_voice_review 的
                    # ``clone_allowed_speaker_ids`` cap 退 PRESET（不克隆=不漏收、
                    # 不 handoff=不打断自动化）。默认（flag off / 无 reservation）
                    # → ``clone_allowed=None`` → 不限制（既有多说话人克隆行为
                    # 字节级不变）。用 vs_payload 顺序选首个（**不**用 set 迭代
                    # 序，CodeX：稳定可复现）。截断同时收窄样本抽取/quota gate。
                    _smart_clone_allowed_speaker_ids: set[str] | None = None
                    if _smart_requires_reservation and _smart_clone_reservation_id:
                        _smart_requiring_set = set(
                            _smart_speaker_ids_requiring_clone
                        )
                        _smart_ordered_requiring = [
                            str(_sp.get("speaker_id") or "").strip()
                            for _sp in (vs_payload.get("speakers") or [])
                            if isinstance(_sp, dict)
                            and str(_sp.get("speaker_id") or "").strip()
                            in _smart_requiring_set
                        ]
                        _smart_speaker_ids_requiring_clone = (
                            _smart_ordered_requiring[:1]
                        )
                        _smart_clone_allowed_speaker_ids = set(
                            _smart_speaker_ids_requiring_clone
                        )
                    _smart_needs_new_clone = bool(
                        _smart_consent_allows_clone
                        and _smart_effective_clone_enabled
                        and _smart_speaker_ids_requiring_clone
                    )
                    _smart_user_id_for_mirror = str(_snap("user_id") or "")
                    _smart_job_id_for_mirror = str(_snap("job_id") or "")
                    _smart_state_for_mirror = _snap("smart_state", {}) or {}
                    if not isinstance(_smart_state_for_mirror, dict):
                        _smart_state_for_mirror = {}
                    _smart_clone_reservation_id_for_mirror = (
                        str(
                            _snap("smart_clone_reservation_id")
                            or _smart_state_for_mirror.get(
                                "smart_clone_reservation_id"
                            )
                            or ""
                        ).strip()
                        or None
                    )
                    _smart_reservation_active_for_clone = bool(
                        _smart_needs_new_clone
                        and _smart_clone_reservation_id_for_mirror
                        and _check_smart_clone_reservation_active(
                            user_id=_smart_user_id_for_mirror,
                            task_id=_smart_job_id_for_mirror,
                            reservation_id=_smart_clone_reservation_id_for_mirror,
                        )
                    )
                    _smart_max_new_clones_for_review = (
                        1 if _smart_reservation_active_for_clone else 0
                    )
                    _smart_speaker_ids_to_extract_clone_samples = (
                        _smart_speaker_ids_requiring_clone[
                            :_smart_max_new_clones_for_review
                        ]
                    )
                    # Phase 3: sample extraction is wasted work when EITHER
                    # consent/admin OR paid reservation state disallows new clone.
                    # Skip the ffmpeg concat work so no-reservation Smart jobs
                    # can fall through to preset instead of pausing before review.
                    if _smart_reservation_active_for_clone:
                        _smart_sample_root = (
                            final_project_dir / "smart_clone_samples"
                        )
                        try:
                            _smart_sample_root.mkdir(parents=True, exist_ok=True)
                            _smart_extractor = VoiceSampleExtractor()
                        except Exception as _setup_exc:
                            _smart_sample_extraction_error = (
                                f"sample_root_setup_error: {str(_setup_exc)[:160]}"
                            )
                            _smart_extractor = None  # type: ignore[assignment]

                        if _smart_extractor is not None:
                            for _candidate_sid in (
                                _smart_speaker_ids_to_extract_clone_samples
                            ):
                                _speaker_lines = [
                                    ln for ln in (
                                        getattr(transcript_result, "lines", None) or []
                                    )
                                    if getattr(ln, "speaker_id", None) == _candidate_sid
                                ]
                                if not _speaker_lines:
                                    _smart_sample_extraction_error = (
                                        f"no_lines_for_speaker_{_candidate_sid}"
                                    )
                                    break
                                _sample_path = (
                                    _smart_sample_root
                                    / f"{_candidate_sid}.wav"
                                )
                                try:
                                    _smart_extractor.extract_sample(
                                        audio_path=str(source_audio_path),
                                        speaker_lines=_speaker_lines,
                                        output_path=str(_sample_path),
                                    )
                                except SampleExtractionError as _se:
                                    _smart_sample_extraction_error = (
                                        f"sample_extract_failed_{_candidate_sid}:"
                                        f" {str(_se)[:120]}"
                                    )
                                    break
                                except Exception as _exc:
                                    _smart_sample_extraction_error = (
                                        f"sample_extract_unexpected_{_candidate_sid}:"
                                        f" {type(_exc).__name__}"
                                    )
                                    break
                                if not _sample_path.exists():
                                    _smart_sample_extraction_error = (
                                        f"sample_missing_post_extract_{_candidate_sid}"
                                    )
                                    break

                                # Codex 第二十七轮 P1: VoiceSampleExtractor's
                                # under-10s case only emits a WARNING and
                                # returns the (short) wav anyway. Without
                                # an explicit validate_sample() check, a
                                # < 10s sample (a speaker who only spoke
                                # briefly) would silently flow into the
                                # clone provider — wasting paid API on a
                                # sample MiniMax would 400-reject.
                                try:
                                    _validation = (
                                        _smart_extractor.validate_sample(
                                            str(_sample_path)
                                        )
                                    )
                                except Exception as _val_exc:
                                    _smart_sample_extraction_error = (
                                        f"sample_validate_error_{_candidate_sid}:"
                                        f" {type(_val_exc).__name__}"
                                    )
                                    break
                                _val_duration_s = float(
                                    _validation.get("duration_s") or 0.0
                                )
                                if _val_duration_s < MIN_SAMPLE_DURATION_SECONDS:
                                    _smart_sample_extraction_error = (
                                        f"sample_too_short_{_candidate_sid}_"
                                        f"{_val_duration_s:.1f}s"
                                    )
                                    break
                                # is_valid combines duration + silence +
                                # rms checks. We tolerate non-is_valid as
                                # long as duration ≥10s — silence/rms
                                # warnings don't 400-reject from MiniMax,
                                # they just produce lower-quality clones.
                                # But the duration floor is a hard
                                # paid-API safety constraint.
                                _smart_per_speaker_samples[_candidate_sid] = (
                                    _sample_path
                                )
                                _smart_per_speaker_sample_seconds[_candidate_sid] = (
                                    _val_duration_s
                                )
                                _smart_segment_ids: list[int] = []
                                for _line in _speaker_lines:
                                    try:
                                        _smart_segment_ids.append(
                                            int(getattr(_line, "index"))
                                        )
                                    except (TypeError, ValueError):
                                        continue
                                _smart_per_speaker_segment_ids[_candidate_sid] = (
                                    _smart_segment_ids
                                )

                    if _smart_sample_extraction_error is not None:
                        # Fail-closed: route handoff without invoking
                        # real provider. Plan §6.5 three-tuple. Cloning
                        # with whole-file or missing audio would waste
                        # MiniMax quota on the wrong sample.
                        _emit_smart_audit(
                            final_project_dir,
                            decision_type="downgrade_handoff",
                            decision="rejected",
                            reason_code="clone_sample_extraction_failed",
                            evidence={
                                "sample_error": _smart_sample_extraction_error,
                                "successful_samples_count": len(
                                    _smart_per_speaker_samples
                                ),
                            },
                            extra={
                                "job_id": str(_snap("job_id") or ""),
                                "user_id": str(_snap("user_id") or ""),
                                "handoff_stage": VOICE_SELECTION_REVIEW_STAGE,
                            },
                        )
                        emit_handoff_markers(
                            review_state_manager=review_state_manager,
                            review_stage=VOICE_SELECTION_REVIEW_STAGE,
                            review_payload=vs_payload,
                            review_pending_status=REVIEW_STATUS_PENDING,
                            smart_state_update={
                                "status": "downgraded_to_studio",
                                "reason": "clone_sample_extraction_failed",
                                "handoff_stage": VOICE_SELECTION_REVIEW_STAGE,
                                "sample_error": _smart_sample_extraction_error,
                            },
                            project_dir=final_project_dir,
                            user_message=(
                                "智能版克隆样本提取失败,请人工接管:"
                                f" {_smart_sample_extraction_error}"
                            ),
                            web_review_marker_builder=self._build_web_review_marker,
                        )
                        state_manager.set_stage(
                            "voice_selection",
                            StageStatus.RUNNING,
                            {"execution_mode": "smart_handoff_sample_failed"},
                        )
                        current_stage_name = None
                        _write_usage_summary(usage_meter)
                        # Codex 第三十六轮 P1: cost_summary at smart handoff.
                        _emit_smart_cost_summary_from_meter(
                            final_project_dir,
                            job_id=config.job_id,
                            usage_meter=usage_meter,
                            minutes_processed=(
                                # Codex 第三十七轮 P1: prefer ffprobe
                                # actual_duration_ms over unreliable _snap.
                                float(actual_duration_ms) / 60000.0
                                if actual_duration_ms
                                else float(
                                    _snap("source_duration_seconds") or 0.0
                                ) / 60.0
                            ),
                            credits_policy="pending_settle",
                        )
                        return self._build_paused_result(
                            project_dir=final_project_dir,
                            stage=VOICE_SELECTION_REVIEW_STAGE,
                            message="智能版克隆样本提取失败",
                        )

                    # ── _smart_main_speakers with per-speaker sample paths ──
                    #
                    # If consent.auto_voice_clone is False:
                    #   source_audio_path defaults to whole-file (placeholder)
                    #   AND sample_seconds defaults to vs_payload's speaker
                    #   total_duration_s. evaluate_voice_review routes to
                    #   PRESET on consent=False BEFORE reading either, so
                    #   the placeholders are never actually consumed.
                    # If consent.auto_voice_clone is True:
                    #   - source_audio_path = _smart_per_speaker_samples[sid]
                    #     (Piece 1 real ffmpeg-extracted clone sample)
                    #   - sample_seconds = _smart_per_speaker_sample_seconds[sid]
                    #     (Codex 第二十七轮 P1: the VALIDATED duration of
                    #     the actual sample file, NOT the speaker's total
                    #     transcript duration. They diverge when a speaker
                    #     spoke 5min total but VoiceSampleExtractor only
                    #     produced a 20s concatenated sample — passing
                    #     5min would make evaluate_voice_review think the
                    #     sample is plenty long and skip the per-speaker
                    #     ≥10s floor check.)
                    def _smart_sample_seconds_for(_sp_dict):
                        sid = _sp_dict.get("speaker_id")
                        val = _smart_per_speaker_sample_seconds.get(sid)
                        if val is not None:
                            return float(val)
                        # No validated sample (consent=False path) →
                        # fall back to vs_payload total. evaluate_voice_review
                        # won't actually read this in the consent=False
                        # branch but we still want a defensible value.
                        return float(_sp_dict.get("total_duration_s") or 0.0)

                    _smart_main_speakers = [
                        VoiceReviewSpeakerInput(
                            speaker_id=sp.get("speaker_id", ""),
                            speaker_name=sp.get("speaker_name", "") or sp.get("speaker_id", ""),
                            sample_seconds=_smart_sample_seconds_for(sp),
                            source_audio_path=_smart_per_speaker_samples.get(
                                sp.get("speaker_id"), source_audio_path
                            ),
                        )
                        for sp in (vs_payload.get("speakers") or [])
                        if isinstance(sp, dict)
                        and sp.get("speaker_id")
                        and sp.get("speaker_id") in _smart_main_speaker_ids
                    ]

                    # ── Pieces 2 + 3: quota snapshot + real CloneProvider ──
                    #
                    # PR#3C-b3e (Codex 第二十七轮 P0 atomic): pieces 2+3
                    # MUST move together. PR#3C-b3e-fix (Codex 第二十九轮
                    # P1): both ALSO must be gated on
                    # ``_smart_consent_allows_clone``.
                    # PR#3C-b3e-fix2 (Codex 第三十轮 P1): the gate is
                    # ALSO conditioned on ``_smart_main_speakers``
                    # being non-empty. evaluate_voice_review with
                    # main_speakers=[] returns AUTO_APPROVED + empty
                    # decisions WITHOUT reading quota or invoking
                    # provider (locked by
                    # tests/test_smart_auto_voice_review.py:597). So
                    # when eligibility excluded every speaker (all
                    # keep_original, all role-excluded, all low-share),
                    # consent doesn't matter — there's nothing to
                    # clone, and a transient Gateway quota failure
                    # would still incorrectly handoff a job that
                    # would happily auto-approve as empty.
                    #
                    # Phase 3 (plan 2026-05-17 §Consent × Admin 决策矩阵):
                    # admin policy adds a second AND gate. When
                    # smart_auto_clone_enabled=False, we must NOT wire
                    # the real CloneProvider (= MiniMax adapter). Routing
                    # the smart job through the not-wired stub means
                    # evaluate_voice_review's PRESET fall-through fires
                    # without ever calling provider, mirroring the
                    # consent=False path.
                    #
                    # evaluate_voice_review short-circuits to PRESET
                    # when EITHER gate is closed OR when main_speakers
                    # is empty. The gate below mirrors all conditions.
                    _smart_quota_remaining = 0
                    _smart_clone_provider = _build_b2_not_wired_clone_provider()
                    # P3e §2: reservation 闸已折进 _smart_effective_clone_enabled
                    # （= admin_clone_enabled AND gate）。闸关 → effective=False →
                    # needs_new_clone=False → not-wired stub → PRESET fall-through，
                    # 与 admin 关克隆字节级一致（绝不接真 provider）。
                    _smart_needs_new_clone = bool(
                        _smart_consent_allows_clone
                        and _smart_effective_clone_enabled
                        and _smart_speaker_ids_requiring_clone
                    )
                    if (
                        _smart_consent_allows_clone
                        and _smart_effective_clone_enabled
                        and _smart_main_speakers
                    ):
                        if (
                            _smart_needs_new_clone
                            and _smart_reservation_active_for_clone
                        ):
                            # 2026-05-16: admin role bypasses voice
                            # library cap (admin_settings.smart_user_voice_clone_cap).
                            # Smart §7.3 water-mark brake is a safety
                            # net for end users; admin accounts test /
                            # demo and should not be quota-blocked.
                            # Effective infinite quota = max int (well
                            # above the cap+water-mark threshold).
                            _smart_role = str(_snap("role_snapshot") or "").lower()
                            if _smart_role == "admin":
                                _smart_quota_remaining = 999_999
                            else:
                                _smart_quota_remaining = (
                                    _fetch_smart_user_voice_quota_remaining(
                                        str(_snap("user_id") or "")
                                    )
                                )
                            if _smart_quota_remaining is None:
                                # 2026-05-20 spec change (smart 全自动化原则):
                                # 之前是 fail-closed handoff —— quota 查询失败
                                # 就停下来让用户接管。但 quota 查询失败几乎全是
                                # transient infra（DB 暂时不通 / Gateway 临时
                                # 错误），不应该卡用户。改为 log warning +
                                # 按「无配额限制」继续。
                                #
                                # 风险分析：极端情况下可能让用户超 admin 配的
                                # smart_user_voice_clone_cap 一两个 clone。
                                # MiniMax 账户那边仍然有自己的硬上限，所以不会
                                # 真无限。下一次任务读 quota 成功后会重新生效
                                # safety water-mark 阻挡。
                                print(
                                    "[smart] voice library quota lookup "
                                    "failed (transient) — treating as "
                                    "unlimited and continuing pipeline; "
                                    "MiniMax provider quota still enforces "
                                    "real upper bound. Audit:",
                                    flush=True,
                                )
                                _emit_smart_audit(
                                    final_project_dir,
                                    decision_type="quota_lookup_degraded",
                                    decision="approved",
                                    reason_code="quota_lookup_failed_continuing",
                                    evidence={
                                        "main_speakers_pending": len(
                                            _smart_main_speakers
                                        ),
                                        "fallback_quota_remaining": 999_999,
                                    },
                                    extra={
                                        "job_id": str(_snap("job_id") or ""),
                                        "user_id": str(_snap("user_id") or ""),
                                    },
                                )
                                # Safe fallback: pretend quota is unlimited
                                # so evaluate_voice_review proceeds. Provider
                                # quota error mid-flight (real MiniMax limit
                                # hit) is still handled via the PAUSED
                                # decision in _attempt_clone_with_retries.
                                _smart_quota_remaining = 999_999

                            # Piece 3: real CloneProvider.
                            from services.smart_wiring import (
                                build_smart_clone_provider,
                            )
                            _smart_clone_provider = build_smart_clone_provider()
                        else:
                            _smart_quota_remaining = 0
                            _smart_clone_provider = (
                                _build_b2_not_wired_clone_provider()
                            )
                    else:
                        # consent=False OR admin_clone_enabled=False
                        # OR empty main_speakers path:
                        # evaluate_voice_review never reads quota or
                        # invokes provider — it short-circuits to PRESET
                        # for any speaker without a strong reuse match.
                        # Pass sentinel values that won't be consumed but
                        # satisfy the type contract. When every main
                        # speaker already has a strong personal-voice
                        # match, evaluate_voice_review also never reads
                        # quota/provider because it emits REUSED first.
                        # NEVER reach the real provider here.
                        _smart_quota_remaining = 0
                        _smart_clone_provider = (
                            _build_b2_not_wired_clone_provider()
                        )

                    _smart_voice_review = evaluate_voice_review(
                        main_speakers=_smart_main_speakers,
                        smart_consent=smart_consent,
                        clone_provider=_smart_clone_provider,
                        voice_library_quota_remaining=_smart_quota_remaining,
                        smart_decision_id_factory=lambda: _smart_uuid.uuid4().hex,
                        existing_voice_matches_by_speaker_id=_smart_existing_voice_matches,
                        possible_voice_matches_by_speaker_id=_smart_possible_voice_matches,
                        admin_clone_enabled=_smart_effective_clone_enabled,
                        admin_pause_on_possible_match=_smart_admin_pause_on_possible,
                        admin_auto_reuse_on_possible_match=_smart_admin_auto_reuse_on_possible,
                        max_new_clones=_smart_max_new_clones_for_review,
                        # P3e §5 limit-1：reservation 收紧时只允许首个待克隆
                        # main speaker 新建克隆，其余退 PRESET。None=不限制（默认）。
                        clone_allowed_speaker_ids=_smart_clone_allowed_speaker_ids,
                    )

                    if _smart_voice_review.outcome == VoiceReviewOutcome.PAUSED:
                        # Failure: emit_handoff_markers three-tuple — plan
                        # §6.0.5 + §6.5 + Codex 第七轮 F1/F2.
                        _emit_smart_audit(
                            final_project_dir,
                            decision_type="voice_selection_auto_approve",
                            decision="rejected",
                            reason_code=(
                                _smart_voice_review.pause_reason
                                or "voice_review_paused"
                            ),
                            evidence={
                                "decisions_count": len(
                                    _smart_voice_review.decisions
                                ),
                                "main_speakers_count": len(
                                    _smart_main_speakers
                                ),
                            },
                            extra={
                                "job_id": str(_snap("job_id") or ""),
                                "user_id": str(_snap("user_id") or ""),
                                "handoff_stage": VOICE_SELECTION_REVIEW_STAGE,
                            },
                        )
                        # Phase 4 (plan 2026-05-17 §Phase 4 验收 #4):
                        # per-speaker audit for the possible-match
                        # pause path. Only emits for the new Phase 4
                        # reason codes — quota pauses already have
                        # their own audit trail via the clone-attempt
                        # branch above, so don't double-log those.
                        _phase4_pause_reasons = {
                            "possible_user_voice_match_requires_confirmation",
                            "paused_after_prior_possible_match_confirmation",
                        }
                        for _paused_dec in _smart_voice_review.decisions:
                            if _paused_dec.choice is not VoiceReviewChoice.PAUSED:
                                continue
                            if _paused_dec.reason_code not in _phase4_pause_reasons:
                                continue
                            _emit_smart_audit(
                                final_project_dir,
                                decision_type="voice_clone_possible_match_pause",
                                decision="rejected",
                                reason_code=_paused_dec.reason_code,
                                evidence=dict(_paused_dec.metrics or {}),
                                smart_decision_id=_paused_dec.smart_decision_id,
                                extra={
                                    "speaker_id": _paused_dec.speaker_id,
                                    "job_id": str(_snap("job_id") or ""),
                                    "user_id": str(_snap("user_id") or ""),
                                },
                            )

                        # Phase 4 (plan 2026-05-17 §Smart 弱匹配暂停 +
                        # §计费和审计 ``smart_possible_user_voice_match_
                        # rejected``): write the per-speaker offered
                        # candidate list into vs_payload BEFORE
                        # emit_handoff_markers so that:
                        #   - frontend can render "AI offered these
                        #     candidates" hint banner (Phase 2 panel
                        #     reads the candidate API directly, but a
                        #     small marker in payload lets the UI show
                        #     "AI 暂停了决策" hint without an extra
                        #     fetch)
                        #   - Gateway approve handler can detect
                        #     rejection (user picks something else)
                        #     and write the
                        #     ``voice_candidate_rejected`` audit.
                        # Only fires for the triggering speaker (the
                        # one that holds the actual offered list);
                        # cascade entries don't have candidates of
                        # their own.
                        if _smart_possible_voice_matches:
                            vs_payload["smart_paused_reason"] = (
                                _smart_voice_review.pause_reason
                                or "voice_review_paused"
                            )
                            for _sp in (vs_payload.get("speakers") or []):
                                if not isinstance(_sp, dict):
                                    continue
                                _sid = str(_sp.get("speaker_id") or "").strip()
                                if not _sid:
                                    continue
                                _offered = _smart_possible_voice_matches.get(_sid)
                                if not _offered:
                                    continue
                                # Drop into the speaker entry under a
                                # clearly Smart-namespaced key so the
                                # legacy voice_match payload stays the
                                # source of truth for picker UI.
                                _sp["smart_offered_candidates"] = [
                                    {
                                        "voice_id": _c.get("voice_id"),
                                        "user_voice_id": _c.get("user_voice_id"),
                                        "label": _c.get("label"),
                                        "confidence": _c.get("confidence"),
                                        "match_scope": _c.get("match_scope"),
                                        "reason": _c.get("reason"),
                                    }
                                    for _c in _offered
                                    if isinstance(_c, dict) and _c.get("voice_id")
                                ]
                        emit_handoff_markers(
                            review_state_manager=review_state_manager,
                            review_stage=VOICE_SELECTION_REVIEW_STAGE,
                            review_payload=vs_payload,
                            review_pending_status=REVIEW_STATUS_PENDING,
                            smart_state_update={
                                "status": "downgraded_to_studio",
                                "reason": _smart_voice_review.pause_reason
                                or "voice_review_paused",
                                "handoff_stage": VOICE_SELECTION_REVIEW_STAGE,
                            },
                            project_dir=final_project_dir,
                            user_message=(
                                "智能版自动音色决策需要人工接管:"
                                f" {_smart_voice_review.pause_reason or 'paused'}"
                            ),
                            web_review_marker_builder=self._build_web_review_marker,
                        )
                        state_manager.set_stage(
                            "voice_selection",
                            StageStatus.RUNNING,
                            {"execution_mode": "smart_handoff_voice_review"},
                        )
                        current_stage_name = None
                        _write_usage_summary(usage_meter)
                        # Codex 第三十六轮 P1: cost_summary at smart handoff.
                        _emit_smart_cost_summary_from_meter(
                            final_project_dir,
                            job_id=config.job_id,
                            usage_meter=usage_meter,
                            minutes_processed=(
                                # Codex 第三十七轮 P1: prefer ffprobe
                                # actual_duration_ms over unreliable _snap.
                                float(actual_duration_ms) / 60000.0
                                if actual_duration_ms
                                else float(
                                    _snap("source_duration_seconds") or 0.0
                                ) / 60.0
                            ),
                            credits_policy="pending_settle",
                        )
                        return self._build_paused_result(
                            project_dir=final_project_dir,
                            stage=VOICE_SELECTION_REVIEW_STAGE,
                            message="智能版自动音色决策需要人工接管",
                        )

                    # Auto-approved: apply per-speaker decisions to
                    # local _speaker_voices AND the approved payload so
                    # set_stage(APPROVED) snapshots the final state.
                    # Plan §6.0.5 末段: set_stage alone is insufficient —
                    # downstream pipeline reads the local dicts, not the
                    # review state, so we MUST apply here in-frame.
                    #
                    # Codex 第十八轮 P1-1: cloned_provider_name (e.g.
                    # "minimax_voice_clone") IS NOT a TTS provider — it
                    # identifies the clone-API vendor for audit. The
                    # voice_id from CloneProvider is consumed by the
                    # configured tts_provider (smart locks to MiniMax
                    # per §5.0), so we write voice_id only and stash
                    # the clone-vendor name into a separate audit
                    # field. ``_speaker_providers`` (the per-speaker
                    # TTS provider override dict) stays untouched —
                    # smart jobs ride the job-level tts_provider.
                    _smart_approved_payload = dict(vs_payload)
                    _smart_approved_payload["auto_approved"] = True
                    _smart_approved_speakers = list(
                        _smart_approved_payload.get("speakers") or []
                    )
                    _smart_speakers_by_id = {
                        sp.get("speaker_id"): sp
                        for sp in _smart_approved_speakers
                        if isinstance(sp, dict)
                    }
                    # Codex 第二十九轮 P0: track clone mirror failures.
                    # A CLONED decision means MiniMax has a new voice_id
                    # but Gateway's UserVoice table doesn't know about
                    # it yet. We must mirror via the internal endpoint
                    # so the quota signal stays consistent across jobs.
                    # If ANY mirror fails, we escalate to handoff so
                    # the user is aware (and so subsequent jobs that
                    # would have hit §7.3 brake don't silently miss it).
                    # Handoff reason below: clone_library_register_failed.
                    _smart_clone_mirror_failures: list[str] = []
                    _smart_reserved_clone_register_failures: list[str] = []

                    for _dec in _smart_voice_review.decisions:
                        _sp_entry = _smart_speakers_by_id.get(_dec.speaker_id)
                        if not _sp_entry:
                            continue
                        if _dec.choice == VoiceReviewChoice.CLONED:
                            _sp_entry["voice_id"] = _dec.cloned_voice_id
                            # AUDIT FIELD ONLY — not a TTS provider.
                            _sp_entry["clone_provider"] = _dec.cloned_provider_name
                            _sp_entry["auto_decision"] = "cloned"

                            # Sidecar audit: per-speaker CLONED success.
                            # Use the auto_voice_review-generated
                            # ``smart_decision_id`` so this audit line
                            # links back to the in-process decision
                            # record (Codex 第三十二轮 P0: the field is
                            # ``smart_decision_id`` not ``decision_id``;
                            # the wrong attribute name would raise
                            # AttributeError BEFORE _emit_smart_audit
                            # is called, so the try/except inside the
                            # helper cannot rescue it — the entire job
                            # would crash after a real MiniMax clone
                            # had already succeeded, reopening the
                            # mirror P0 from 第二十九轮.)
                            _emit_smart_audit(
                                final_project_dir,
                                decision_type="voice_clone",
                                decision="approved",
                                evidence={
                                    "voice_id": _dec.cloned_voice_id,
                                    "clone_provider": (
                                        _dec.cloned_provider_name
                                    ),
                                    "sample_seconds": (
                                        _smart_per_speaker_sample_seconds.get(
                                            _dec.speaker_id
                                        )
                                    ),
                                },
                                smart_decision_id=_dec.smart_decision_id,
                                extra={
                                    "speaker_id": _dec.speaker_id,
                                    "job_id": str(_snap("job_id") or ""),
                                    "user_id": str(_snap("user_id") or ""),
                                },
                            )

                            # 2026-05-18 cost-accounting fix: record the
                            # NEW clone to UsageMeter so the admin cost
                            # view (/admin/jobs/{id}/cost) and metering
                            # rollups reflect the real MiniMax charge
                            # (¥9.9/clone per pricing_runtime). Previously
                            # only the REUSED path (record_voice_reuse,
                            # billable=False) recorded anything; fresh
                            # auto-clones silently incurred provider
                            # charges that never showed in admin cost
                            # data → admin saw inflated margins (e.g.
                            # job_14989c5e... reported 86.7% margin but
                            # missing 2 × ¥9.9 = ¥19.80 of clone cost).
                            #
                            # Three sinks now all record the same clone:
                            #   1. audit/smart_decisions.jsonl (above)
                            #   2. metering/usage_events.jsonl (NEW here)
                            #   3. user_voices DB mirror (below)
                            try:
                                usage_meter.record_voice_clone(
                                    provider=str(
                                        _dec.cloned_provider_name or ""
                                    ),
                                    model=str(
                                        _dec.cloned_model_name
                                        or "voice_clone"
                                    ),
                                    voice_id=str(
                                        _dec.cloned_voice_id or ""
                                    ),
                                    speaker_id=_dec.speaker_id,
                                    source_audio_seconds=float(
                                        _smart_per_speaker_sample_seconds.get(
                                            _dec.speaker_id
                                        ) or 0.0
                                    ),
                                    clone_count=1,
                                    billable=True,
                                    success=True,
                                    extra={
                                        "smart_decision_id": _dec.smart_decision_id,
                                        "source": "smart_auto_voice_review",
                                        "job_id": str(_snap("job_id") or ""),
                                        "user_id": str(_snap("user_id") or ""),
                                    },
                                )
                            except Exception as _meter_exc:
                                # Metering failure must NOT block the
                                # pipeline (cost view is admin-only,
                                # user-facing delivery is the priority).
                                print(
                                    f"[smart] voice_clone usage record "
                                    f"failed speaker={_dec.speaker_id}: "
                                    f"{type(_meter_exc).__name__}: "
                                    f"{_meter_exc}",
                                    flush=True,
                                )

                            # Codex 第二十九轮 P0: mirror to UserVoice.
                            # MUST happen on every CLONED decision so
                            # next job's quota sees the up-to-date
                            # ``used`` count. Field shape mirrors the
                            # Studio manual-clone path
                            # (voice_selection_api.py:503) so the two
                            # clone origins are indistinguishable
                            # downstream.
                            _mirror_label = (
                                _sp_entry.get("speaker_name", "")
                                or _dec.speaker_id
                            )
                            _mirror_ok = _register_smart_clone_in_user_voices(
                                user_id=_smart_user_id_for_mirror,
                                voice_id=_dec.cloned_voice_id or "",
                                label=_build_cloned_voice_label(_mirror_label),
                                source_speaker_id=_dec.speaker_id,
                                source_job_id=_smart_job_id_for_mirror or None,
                                source_type=str(_snap("source_type") or "") or None,
                                source_ref=str(_snap("source_ref") or "") or None,
                                source_content_hash=str(_snap("source_content_hash") or "") or None,
                                source_video_title=(
                                    str(
                                        _snap("display_name")
                                        or _snap("source_video_title")
                                        or _snap("title")
                                        or ""
                                    )
                                    or None
                                ),
                                source_published_at=(
                                    str(_snap("source_published_at") or "")
                                    or None
                                ),
                                source_content_summary=(
                                    str(_snap("source_content_summary") or "")
                                    or None
                                ),
                                source_content_era=(
                                    str(_snap("source_content_era") or "")
                                    or None
                                ),
                                source_content_tags=(
                                    _snap("source_content_tags")
                                    if isinstance(_snap("source_content_tags"), (dict, list))
                                    else None
                                ),
                                source_speaker_name=str(_mirror_label or "") or None,
                                clone_sample_seconds=(
                                    _smart_per_speaker_sample_seconds.get(
                                        _dec.speaker_id
                                    )
                                ),
                                clone_sample_segment_ids=(
                                    _smart_per_speaker_segment_ids.get(
                                        _dec.speaker_id
                                    )
                                ),
                                smart_clone_reservation_id=(
                                    _smart_clone_reservation_id_for_mirror
                                ),
                                created_from="smart_auto",
                                notes=(
                                    f"Smart auto-clone from job "
                                    f"{_smart_job_id_for_mirror}"
                                    if _smart_job_id_for_mirror
                                    else "Smart auto-clone"
                                ),
                                # P3e §5：reservation 在场 → 改走 register-billed
                                # （原子写 billing event）。limit-1 保证本 run 最多
                                # 1 个 CLONED → 最多 1 次 bill；register-billed 以
                                # reservation_id 唯一约束幂等，重试不双计费。无
                                # reservation → 二者皆 None → 仍走 register-smart。
                                reservation_id=_smart_clone_reservation_id,
                                task_id=(
                                    _smart_job_id_for_mirror
                                    or str(_snap("job_id") or "")
                                    or None
                                ),
                            )
                            if not _mirror_ok:
                                _smart_clone_mirror_failures.append(
                                    _dec.speaker_id
                                )
                                if _smart_clone_reservation_id_for_mirror:
                                    _smart_reserved_clone_register_failures.append(
                                        _dec.speaker_id
                                    )
                                    _sp_entry.pop("voice_id", None)
                                    _sp_entry["auto_decision"] = (
                                        "clone_register_failed"
                                    )
                                    _sp_entry["smart_clone_skipped_reason"] = (
                                        "clone_register_failed_reserved_handoff"
                                    )
                        elif _dec.choice == VoiceReviewChoice.REUSED:
                            _apply_smart_reused_voice_decision(
                                speaker_entry=_sp_entry,
                                decision=_dec,
                                usage_meter=usage_meter,
                                project_dir=final_project_dir,
                                job_id=str(_snap("job_id") or ""),
                                user_id=str(_snap("user_id") or ""),
                            )
                        elif _dec.choice == VoiceReviewChoice.PRESET:
                            # ``auto_matched_voice`` is a DICT shaped
                            # by ``_auto_match_for_provider`` (see
                            # process.py:6509 return). Extract bare
                            # voice_id string via the dedicated helper
                            # (Codex 第三十七轮 Test Gap: pure function,
                            # unit-tested for dict / str / None /
                            # unknown shapes). The original b2 stub
                            # assigned the dict directly to
                            # _sp_entry["voice_id"], crashing downstream
                            # at ``voice_id.startswith("vt_")``.
                            _sp_entry["voice_id"] = _resolve_preset_voice_id(
                                _sp_entry.get("auto_matched_voice")
                            )
                            _sp_entry["auto_decision"] = "preset"
                            _sp_entry["smart_clone_skipped_reason"] = _dec.reason_code
                        _sp_id = _dec.speaker_id
                        _sp_voice = _sp_entry.get("voice_id")
                        if _sp_id and _sp_voice:
                            _speaker_voices[_sp_id] = _sp_voice
                        # NB: deliberately do NOT touch _speaker_providers.
                        # Smart auto-decision doesn't override the job-level
                        # TTS provider per-speaker; the clone vendor name
                        # is recorded on _sp_entry["clone_provider"] for audit
                        # but never flows into segment.tts_provider routing.

                    # Smart MVP (2026-05-16): non-main speakers also get a
                    # voice via the existing auto_matched_voice from
                    # vs_payload (same voice_match_resolver Studio UI uses
                    # for "推荐音色"). Without this, speaker_c+ ends up
                    # with segment.voice_id=None at TTS dispatch time
                    # (process.py:7113 "auto-match" comment is aspirational).
                    #
                    # Codex 第四十轮 P2.4: aggregate segment-level
                    # dubbing_mode to speaker-level FIRST. The vs_payload
                    # speaker entries don't carry dubbing_mode at the
                    # speaker level (it's segment-level state). Without
                    # the aggregation, helper's keep_original /
                    # mute_or_background exclusion is dead code (the
                    # condition can never match). Pass the aggregated
                    # dict explicitly so the helper can honor the
                    # exclusion contract.
                    #
                    # 2026-05-16 P0 fix (job_134ee34a... crash): smart inline
                    # branch runs BEFORE the S3 translate call at line ~4623,
                    # so on the fresh-run path ``translation_result`` is still
                    # ``None`` (initialised at line ~2952 for fresh runs;
                    # only the s3_cache_hit branch fills it in earlier).
                    # Reading ``translation_result.segments`` crashed with
                    # ``'NoneType' object has no attribute 'segments'`` after
                    # the user's S2 review + voice clone work was already
                    # billed. Source of dubbing_mode at this point is
                    # ``transcript_result.lines`` (TranscriptLine carries
                    # ``dubbing_mode: str = "dub"`` since S1+S2 review;
                    # propagated to translation_result.segments later via
                    # ``_apply_transcript_dubbing_modes_to_segments``).
                    # Prefer translation_result.segments on cache-hit re-runs
                    # (segment-level may have been edited via post-edit), fall
                    # back to transcript_result.lines on fresh runs.
                    if _smart_reserved_clone_register_failures:
                        print(
                            f"[smart] reserved clone registration failed for "
                            f"{len(_smart_reserved_clone_register_failures)} "
                            f"speaker(s): "
                            f"{','.join(_smart_reserved_clone_register_failures)}; "
                            "stopping for handoff so reserved credits are not "
                            "released after a paid provider clone.",
                            flush=True,
                        )
                        _emit_smart_audit(
                            final_project_dir,
                            decision_type="downgrade_handoff",
                            decision="rejected",
                            reason_code="clone_register_failed_reserved_handoff",
                            evidence={
                                "failed_speakers": list(
                                    _smart_reserved_clone_register_failures
                                ),
                                "reservation_id": (
                                    _smart_clone_reservation_id_for_mirror
                                ),
                            },
                            extra={
                                "job_id": str(_snap("job_id") or ""),
                                "user_id": str(_snap("user_id") or ""),
                                "handoff_stage": VOICE_SELECTION_REVIEW_STAGE,
                            },
                        )
                        _persist_smart_clone_register_failed_state(
                            job_id=str(_snap("job_id") or config.job_id or ""),
                            reservation_id=_smart_clone_reservation_id_for_mirror,
                            failed_speakers=list(
                                _smart_reserved_clone_register_failures
                            ),
                            handoff_stage=VOICE_SELECTION_REVIEW_STAGE,
                        )
                        emit_handoff_markers(
                            review_state_manager=review_state_manager,
                            review_stage=VOICE_SELECTION_REVIEW_STAGE,
                            review_payload=vs_payload,
                            review_pending_status=REVIEW_STATUS_PENDING,
                            smart_state_update={
                                "status": "downgraded_to_studio",
                                "reason": "clone_library_register_failed",
                                "handoff_stage": VOICE_SELECTION_REVIEW_STAGE,
                            },
                            project_dir=final_project_dir,
                            user_message="智能版克隆计费登记失败，需要人工接管。",
                            web_review_marker_builder=self._build_web_review_marker,
                        )
                        state_manager.set_stage(
                            "voice_selection",
                            StageStatus.RUNNING,
                            {"execution_mode": "smart_handoff_voice_review"},
                        )
                        current_stage_name = None
                        _write_usage_summary(usage_meter)
                        _emit_smart_cost_summary_from_meter(
                            final_project_dir,
                            job_id=config.job_id,
                            usage_meter=usage_meter,
                            minutes_processed=(
                                float(actual_duration_ms) / 60000.0
                                if actual_duration_ms
                                else float(
                                    _snap("source_duration_seconds") or 0.0
                                ) / 60.0
                            ),
                            credits_policy="pending_settle",
                        )
                        return self._build_paused_result(
                            project_dir=final_project_dir,
                            stage=VOICE_SELECTION_REVIEW_STAGE,
                            message="智能版克隆计费登记失败，需要人工接管。",
                        )

                    _dub_source = (
                        translation_result.segments
                        if translation_result is not None
                        else transcript_result.lines
                    )
                    _smart_dubbing_modes = _aggregate_speaker_dubbing_modes(
                        _dub_source
                    )
                    _smart_minor_voice_assignments = (
                        _resolve_smart_minor_speaker_voices(
                            speakers=_smart_approved_speakers,
                            main_speaker_ids=_smart_main_speaker_ids,
                            dubbing_mode_by_speaker=_smart_dubbing_modes,
                        )
                    )
                    for _minor_sid, _minor_vid in (
                        _smart_minor_voice_assignments.items()
                    ):
                        _speaker_voices[_minor_sid] = _minor_vid
                        _minor_entry = _smart_speakers_by_id.get(_minor_sid)
                        if _minor_entry is not None:
                            _minor_entry["voice_id"] = _minor_vid
                            _minor_entry["auto_decision"] = "preset_minor_speaker"

                    # 2026-05-20 spec change (smart 全自动化原则):
                    # 之前是 fail-closed handoff —— MiniMax 克隆成功但
                    # Gateway 音色库登记失败就停下来让用户接管。
                    # 现在改为 log warning + 继续 pipeline，让用户拿到
                    # 配音视频。Gateway DB 同步的最终一致性靠后台
                    # sweeper 在下次任务/手动 admin 操作时补登记。
                    #
                    # Codex 第二十九轮 P0 关注的"下次任务配额 stale"
                    # 风险仍然存在，但用户体感 > infra 同步精度。
                    # 后台 sweeper（待补 P5 follow-up）扫
                    # smart_decisions.jsonl 的 voice_clone 决策与
                    # user_voices DB 差异，自动补录。
                    if _smart_clone_mirror_failures:
                        print(
                            f"[smart] clone library register failed for "
                            f"{len(_smart_clone_mirror_failures)} speaker(s): "
                            f"{','.join(_smart_clone_mirror_failures)} — "
                            "continuing pipeline; voice already exists at "
                            "MiniMax provider, Gateway DB sync will need "
                            "manual reconciliation.",
                            flush=True,
                        )
                        _emit_smart_audit(
                            final_project_dir,
                            decision_type="clone_mirror_degraded",
                            decision="approved",
                            reason_code="clone_register_failed_continuing",
                            evidence={
                                "failed_speakers": list(
                                    _smart_clone_mirror_failures
                                ),
                                "successful_clones": [
                                    _dec.speaker_id
                                    for _dec in _smart_voice_review.decisions
                                    if _dec.choice == VoiceReviewChoice.CLONED
                                    and _dec.speaker_id
                                    not in _smart_clone_mirror_failures
                                ],
                            },
                            extra={
                                "job_id": str(_snap("job_id") or ""),
                                "user_id": str(_snap("user_id") or ""),
                                "reconcile_needed": True,
                            },
                        )
                        # Continue with the clone — voice_id is already
                        # set on _sp_entry / _speaker_voices earlier in
                        # the CLONED branch; TTS dispatch will use the
                        # vt_speaker_* IDs successfully because MiniMax
                        # provider has the voice. Only the cross-job
                        # quota tracker is stale.

                    review_state_manager.set_stage(
                        VOICE_SELECTION_REVIEW_STAGE,
                        status=REVIEW_STATUS_APPROVED,
                        payload=_smart_approved_payload,
                        activate=True,
                    )
                    # Codex 第十八轮 P0-1: do NOT emit
                    # smart_state.status = "voice_review_auto_approved" —
                    # the editable-state predicate
                    # (services.smart.state._SMART_STATE_EDITABLE_STATUSES)
                    # only accepts ``completed`` and ``downgraded_to_studio``.
                    # Setting an intermediate status here would merge-overwrite
                    # the top-level ``status`` on JobRecord.smart_state via
                    # process_runner's marker handler; if the pipeline then
                    # crashed before emitting the terminal
                    # ``{"status": "completed", ...}`` marker, the
                    # editing.py / jianying gates would refuse to enter
                    # editing AND the settle dispatcher would fall back to
                    # the legacy succeeded-branch (skipping
                    # ``capture_full`` per the smart credits_policy table).
                    # b3's terminal-finalize step writes the editable
                    # status; this intermediate marker only stamps audit
                    # metadata.
                    emit_smart_state_marker(
                        {
                            "voice_review": {
                                "auto_approved": True,
                                "decisions_count": len(_smart_voice_review.decisions),
                            },
                        }
                    )

                    # Sidecar audit: voice_selection batch auto-approved.
                    # Per-speaker CLONED decisions already wrote their
                    # own voice_clone events above; this event captures
                    # the batch-level verdict.
                    _smart_cloned_count = sum(
                        1
                        for _dec in _smart_voice_review.decisions
                        if _dec.choice == VoiceReviewChoice.CLONED
                    )
                    _smart_reused_count = sum(
                        1
                        for _dec in _smart_voice_review.decisions
                        if _dec.choice == VoiceReviewChoice.REUSED
                    )
                    _smart_preset_count = sum(
                        1
                        for _dec in _smart_voice_review.decisions
                        if _dec.choice == VoiceReviewChoice.PRESET
                    )
                    # 2026-05-16: ``minor_preset_count`` captures non-main
                    # speakers that got auto-matched preset (low_share /
                    # rank > 3). Admin-visible in the sidecar JSONL so
                    # rollout audit can answer "how many speakers in this
                    # job got the minor-auto-match treatment?".
                    _smart_minor_preset_count = len(
                        _smart_minor_voice_assignments
                    )
                    _emit_smart_audit(
                        final_project_dir,
                        decision_type="voice_selection_auto_approve",
                        decision="approved",
                        evidence={
                            "decisions_count": len(
                                _smart_voice_review.decisions
                            ),
                            "cloned_count": _smart_cloned_count,
                            "reused_count": _smart_reused_count,
                            "preset_count": _smart_preset_count,
                            "minor_preset_count": _smart_minor_preset_count,
                            "main_speakers_count": len(
                                _smart_main_speakers
                            ),
                        },
                        extra={
                            "job_id": str(_snap("job_id") or ""),
                            "user_id": str(_snap("user_id") or ""),
                        },
                    )

                    print(
                        f"[S2.5] Smart 自动批准 voice_selection_review:"
                        f" {len(_smart_voice_review.decisions)} 决策"
                    )

                    # 2026-05-16 P0 fix (job_887984b... bug): smart
                    # auto-approve must propagate the per-speaker voice
                    # decisions into the ``voice_id_a`` / ``voice_id_b``
                    # local vars that flow into ``translator.translate()``
                    # at line ~4623. The Studio path does this at line
                    # ~3107-3108 (``voice_id_a = _speaker_voices.get(
                    # "speaker_a", voice_id_a)``); without the parallel
                    # update here, smart auto-approve stamps
                    # ``voice_id="auto"`` on every segment and TTS
                    # silently falls back to preset voices (Wise_Woman /
                    # Chinese_radio_host_male_vv1 etc.) instead of the
                    # cloned vt_speaker_* IDs the smart decisions chose.
                    #
                    # Real incident: job_887984b... 124-segment Musk
                    # interview, audit log said reused
                    # vt_speaker_a_1778930206296 + vt_speaker_b_1778930225912,
                    # actual TTS dispatched 176 calls with voice_id="auto"
                    # → all preset voices; user reported "感觉并没有
                    # 克隆音色". Cloned voice IDs were correctly registered
                    # in user_voices and counted in metering (reuse
                    # billing_policy), but never reached the segments
                    # because of this missing propagation.
                    #
                    # NOTE: the >2-speaker path uses ``_speaker_voices``
                    # via translator's ``speaker_voices=`` kwarg
                    # (line ~4633), but the 2-speaker default path only
                    # uses ``voice_id`` / ``voice_id_b`` — that's the
                    # branch that was completely broken.
                    voice_id_a = _speaker_voices.get("speaker_a", voice_id_a)
                    voice_id_b = _speaker_voices.get("speaker_b", voice_id_b)

                    # Fall through to next pipeline stage — NO paused-return.
                else:
                    # --- Studio path: original pending-pause behaviour ---
                    review_state_manager.set_stage(
                        VOICE_SELECTION_REVIEW_STAGE,
                        status=REVIEW_STATUS_PENDING,
                        payload=vs_payload,
                        activate=True,
                    )
                    review_message = "请为每位说话人选择或克隆配音音色"
                    print(f"[S2.5] {review_message}")
                    state_manager.set_stage(
                        "voice_selection",
                        StageStatus.RUNNING,
                        {"execution_mode": "waiting_for_voice_selection"},
                    )
                    current_stage_name = None
                    print(
                        self._build_web_review_marker(
                            stage=VOICE_SELECTION_REVIEW_STAGE,
                            project_dir=final_project_dir,
                            message=review_message,
                        )
                    )
                    _write_usage_summary(usage_meter)
                    return self._build_paused_result(
                        project_dir=final_project_dir,
                        stage=VOICE_SELECTION_REVIEW_STAGE,
                        message=review_message,
                    )

            # --- Pre-TTS voice validation (cloned voices, before translation) ---
            #
            # Plan §4.3 末段 row 4. PR#3C-b2 widens this gate to also
            # cover smart jobs — the auto_voice_review-picked cloned
            # voice could have expired between trigger time and TTS.
            # When smart hits the expired branch we route through
            # emit_handoff_markers() three-tuple (Codex 第七轮 F1/F2 +
            # 第十六轮 P1) so JobRecord.smart_state is mirrored to
            # Gateway DB before billing reads it.
            # Codex 第十八轮 P1-2: pipeline-control branch reads
            # job_effective_pipeline_mode, not raw job_service_mode.
            # Handoff-state smart jobs (effective=studio) fall into the
            # studio branch — that's what /continue after handoff expects.
            if config.wait_for_review and job_effective_pipeline_mode in {"studio", "smart"}:
                expired_voices = self._validate_cloned_voices(_speaker_voices)
                if expired_voices:
                    for ev_id in expired_voices:
                        self._notify_voice_expired(config.job_id, ev_id)
                    vs_payload = self._build_voice_selection_review_payload(
                        transcript_result=transcript_result,
                        tts_provider=job_tts_provider,
                        service_mode=job_service_mode,
                        source_audio_path=source_audio_path,
                        effective_speakers=effective_speakers,
                        speaker_names=_merge_speaker_name_map(
                            _review_speaker_names,
                            speaker_name_a,
                            speaker_name_b,
                        ),
                        speaker_styles=_review_speaker_styles,
                        probe_segments=_probe_segments or None,
                        speaker_structure_profiles=_speaker_structure_profiles,
                    )
                    vs_payload["expired_voice_ids"] = expired_voices
                    vs_payload["validation_error"] = f"检测到 {len(expired_voices)} 个音色已失效，请重新选择"
                    review_message = f"检测到 {len(expired_voices)} 个音色已失效，请重新选择"

                    if job_effective_pipeline_mode == "smart":
                        # Smart expiry → handoff: smart can't auto-recover
                        # from an externally-revoked clone (admin cleanup,
                        # account quota turnover). User picks fresh voices
                        # via Studio human-review.
                        from services.smart.handoff import emit_handoff_markers

                        _emit_smart_audit(
                            final_project_dir,
                            decision_type="downgrade_handoff",
                            decision="rejected",
                            reason_code="cloned_voice_expired",
                            evidence={
                                "expired_voice_ids": list(expired_voices),
                                "expired_count": len(expired_voices),
                            },
                            extra={
                                "job_id": str(_snap("job_id") or ""),
                                "user_id": str(_snap("user_id") or ""),
                                "handoff_stage": VOICE_SELECTION_REVIEW_STAGE,
                            },
                        )
                        emit_handoff_markers(
                            review_state_manager=review_state_manager,
                            review_stage=VOICE_SELECTION_REVIEW_STAGE,
                            review_payload=vs_payload,
                            review_pending_status=REVIEW_STATUS_PENDING,
                            smart_state_update={
                                "status": "downgraded_to_studio",
                                "reason": "cloned_voice_expired",
                                "handoff_stage": VOICE_SELECTION_REVIEW_STAGE,
                                "expired_voice_ids": list(expired_voices),
                            },
                            project_dir=final_project_dir,
                            user_message=review_message,
                            web_review_marker_builder=self._build_web_review_marker,
                        )
                    else:
                        # Studio: original behaviour — set_stage + web review marker
                        review_state_manager.set_stage(
                            VOICE_SELECTION_REVIEW_STAGE,
                            status=REVIEW_STATUS_PENDING,
                            payload=vs_payload,
                            activate=True,
                        )
                        print(f"[S2.5] {review_message}")
                        print(
                            self._build_web_review_marker(
                                stage=VOICE_SELECTION_REVIEW_STAGE,
                                project_dir=final_project_dir,
                                message=review_message,
                            )
                        )
                    _write_usage_summary(usage_meter)
                    # Codex 第三十六轮 P1: cost_summary at smart handoff.
                    # Site reached by both smart (handoff) and studio
                    # (wait-for-review) paths — gate on smart explicitly.
                    if self._current_service_mode == "smart":
                        _emit_smart_cost_summary_from_meter(
                            final_project_dir,
                            job_id=config.job_id,
                            usage_meter=usage_meter,
                            minutes_processed=(
                                # Codex 第三十七轮 P1: prefer ffprobe
                                # actual_duration_ms over unreliable _snap.
                                float(actual_duration_ms) / 60000.0
                                if actual_duration_ms
                                else float(
                                    _snap("source_duration_seconds") or 0.0
                                ) / 60.0
                            ),
                            credits_policy="pending_settle",
                        )
                    return self._build_paused_result(
                        project_dir=final_project_dir,
                        stage=VOICE_SELECTION_REVIEW_STAGE,
                        message=review_message,
                    )

            # --- S4-probe Phase 2: TTS 校准（音色确认后） ---
            # Runs for both Studio and Express modes — probe cost (~$0.02) is
            # justified by downstream savings (fewer rewrites + force_dsp).
            _probe_chars_per_second = DEFAULT_ESTIMATED_TTS_CHARS_PER_SECOND
            _probe_chars_per_second_by_speaker: dict[str, float] = {}
            _catalog_cps_used = False
            # Phase 2 Task 0 — track which speakers had a catalog hit, even
            # when the all-or-nothing policy ends up falling back to probe.
            # This tells us how often the partial-hit case occurs.
            _catalog_hit_speakers: set[str] = set()

            # --- Speed catalog lookup (Phase 1 of translation-duration-alignment) ---
            # Studio mode only, voice_selection already confirmed, all speakers
            # have concrete voice_ids (not "auto"). On hit, skip probe entirely.
            #
            # NOTE: we intentionally DO NOT guard on `not s3_cache_hit` — the
            # catalog lookup is a cheap in-memory-cached call and we need the
            # result to stamp `segment.catalog_hit` even on cache-hit re-runs
            # (Studio reruns AFTER translation_review have s3_cache_hit=True
            # but still need correct metering flags).
            # The actual "skip probe" decision below stays gated on cache.
            # Plan §4.3 末段 row 5 + Codex 第二轮 F5: smart jobs also
            # benefit from the speed catalog hit path. auto_voice_review
            # picks concrete voice_ids per main speaker (cloned vt_* or
            # preset), so the precondition "all voice_ids are concrete
            # not 'auto'" is satisfied for smart just like studio.
            # Widening here skips an unnecessary $0.02 probe for smart
            # jobs that have catalog coverage.
            if (
                job_service_mode in {"studio", "smart"}
                and _speaker_voices
                and all(v and v != "auto" for v in _speaker_voices.values())
            ):
                try:
                    from services.tts.voice_speed_catalog import lookup_per_speaker

                    _catalog_global, _catalog_by_speaker = lookup_per_speaker(
                        _speaker_voices,
                        default_provider=job_tts_provider or "minimax",
                        speaker_providers=_speaker_providers or None,
                        tts_model=str(_snap('tts_model') or getattr(tts_config, "model", "") or ""),
                        # Scopes the user_voices fallback to this job's owner.
                        # Without this, a cloned voice_id could match rows from
                        # a different user and leak their cps into this pipeline.
                        user_id=_snap('user_id'),
                    )
                    if _catalog_by_speaker:
                        # Always record per-speaker catalog hits so the metric
                        # reflects partial-hit reality, even when fallback to probe
                        # is triggered by the all-or-nothing policy.
                        _catalog_hit_speakers = set(_catalog_by_speaker.keys())
                    # skip-probe optimisation only makes sense on fresh translation;
                    # cache-hit runs don't translate again and probe gating is moot.
                    if (not s3_cache_hit
                            and _catalog_by_speaker
                            and len(_catalog_by_speaker) == len(_speaker_voices)):
                        _probe_chars_per_second = _catalog_global
                        _probe_chars_per_second_by_speaker = _catalog_by_speaker
                        _catalog_cps_used = True
                        print(f"[S4-catalog] 使用预标定音色语速，跳过 probe TTS 校准")
                        print(f"[S4-catalog]   global: {_catalog_global:.3f} 字/秒")
                        for _spk, _cps in _catalog_by_speaker.items():
                            print(f"[S4-catalog]   {_spk}: {_cps:.3f} 字/秒")
                    elif not s3_cache_hit and _catalog_by_speaker:
                        _covered = len(_catalog_by_speaker)
                        _total = len(_speaker_voices)
                        print(
                            f"[S4-catalog] 部分预标定命中（{_covered}/{_total}），"
                            f"回退到 probe TTS 校准"
                        )
                    elif _catalog_by_speaker:
                        # cache-hit 情况下记录一下 metric 命中数，但不打印长篇日志
                        print(f"[S4-catalog] cache-hit 重跑：记录 catalog 命中 "
                              f"{len(_catalog_by_speaker)}/{len(_speaker_voices)}（不参与 probe 决策）")
                        # Phase 2 Task 1 fix: even on cache-hit re-runs, propagate
                        # the catalog cps values into the variables that feed
                        # tts_generator.set_speaker_chars_per_second(). Without this
                        # the MiniMax per-segment speed_decision permanently sees
                        # chars_per_second=None and returns "missing_inputs" → 1.0,
                        # which silently kills Task 1 for every cache-hit run.
                        if _catalog_global and _catalog_global > 0:
                            _probe_chars_per_second = _catalog_global
                        for _spk, _cps in _catalog_by_speaker.items():
                            if _cps and _cps > 0:
                                _probe_chars_per_second_by_speaker[_spk] = _cps
                except Exception as exc:
                    print(f"[S4-catalog] 目录查询异常（回退 probe）：{exc}")

            if not _catalog_cps_used and not s3_cache_hit and _probe_segments:
                try:
                    _probe_tts_generator = TTSGenerator(tts_config, job_record=config.job_record)
                    _set_usage_meter_if_supported(_probe_tts_generator, usage_meter)
                    _probe_tts_dir = (final_project_dir / "tts").resolve(strict=False)
                    _probe_tts_dir.mkdir(parents=True, exist_ok=True)
                    _probe_chars_per_second, _probe_chars_per_second_by_speaker = (
                        self._run_probe_tts_and_calibrate(
                            _probe_segments,
                            _probe_tts_generator,
                            _probe_tts_dir,
                            voice_id_a=voice_id_a,
                            display_name_a=speaker_name_a,
                            voice_id_b=voice_id_b,
                            display_name_b=speaker_name_b,
                            speaker_voices=_speaker_voices if effective_speakers > 2 else None,
                            speaker_providers=_speaker_providers or None,
                            # Phase 4.1 E.5: probe TTS 也走 worker routing
                            speaker_voice_routing=_speaker_voice_routing or None,
                        )
                    )
                    # Persist probe result so the next pipeline entry (cache-hit
                    # rerun after translation_review approval) can reload the
                    # calibrated cps instead of falling back to the 4.5 default.
                    # Without this, cloned voices (which never hit voice_catalog)
                    # poison pre-rewrite + Phase 2 speed_decision on every rerun.
                    # Bug analysed in Job job_6673fdf6cb4d4cc6aedc70bc48f8828e
                    # (2026-04-15): 17 spurious pre-rewrites + 30 S5 rewrites.
                    try:
                        from services.calibration_persistence import persist_probe_calibration
                        persist_probe_calibration(
                            (final_project_dir / "audio").resolve(strict=False),
                            cps_global=_probe_chars_per_second,
                            cps_by_speaker=_probe_chars_per_second_by_speaker,
                            speaker_voices=_speaker_voices,
                        )
                    except Exception as _persist_exc:
                        print(f"[S4-probe] 持久化校准结果失败（非致命）：{_persist_exc}")
                except Exception as exc:
                    print(f"[S4-probe] 探针校准整体异常（回退 {DEFAULT_ESTIMATED_TTS_CHARS_PER_SECOND}）：{exc}")

            # Cache-hit reload path: when re-entering after translation_review
            # approval we skip both catalog lookup (clone voices not present)
            # and probe TTS (skipped under s3_cache_hit). Reload the persisted
            # calibration so pre-rewrite + Phase 2 speed see real cps values.
            if (
                not _catalog_cps_used
                and s3_cache_hit
                and _probe_chars_per_second == DEFAULT_ESTIMATED_TTS_CHARS_PER_SECOND
            ):
                try:
                    from services.calibration_persistence import load_probe_calibration
                    _loaded_global, _loaded_by_speaker = load_probe_calibration(
                        (final_project_dir / "audio").resolve(strict=False),
                        expected_voices=_speaker_voices,
                    )
                    if _loaded_global is not None:
                        _probe_chars_per_second = _loaded_global
                        _probe_chars_per_second_by_speaker = _loaded_by_speaker
                        print(
                            f"[S4-probe] cache-hit 加载持久化校准："
                            f"global={_loaded_global:.3f} 字/秒，"
                            f"speakers={list(_loaded_by_speaker.keys())}"
                        )
                except Exception as _load_exc:
                    print(f"[S4-probe] 持久化校准加载失败（保持默认）：{_load_exc}")

            # --- S3 Translation (voice already confirmed above) ---
            if s3_cache_hit:
                self._apply_runtime_voice_overrides(
                    translation_result.segments,
                    voice_id_a=voice_id_a,
                    display_name_a=speaker_name_a,
                    voice_id_b=voice_id_b,
                    display_name_b=speaker_name_b,
                    speaker_voices=_speaker_voices if effective_speakers > 2 else None,
                    speaker_providers=_speaker_providers or None,
                    # Phase 4.1 E.5: cache-hit 路径也透传 worker routing
                    speaker_voice_routing=_speaker_voice_routing or None,
                )
            else:
                print("[S3] 翻译文本...")
                translation_result = translator.translate(
                    transcript_result.lines,
                    str(final_project_dir / "translation"),
                    voice_id=voice_id_a,
                    display_name=speaker_name_a,
                    voice_id_b=voice_id_b,
                    display_name_b=speaker_name_b if effective_speakers >= 2 else None,
                    video_title=download_result.video_title,
                    youtube_url=normalized_url,
                    glossary=_review_glossary or None,
                    speaker_voices=_speaker_voices if effective_speakers > 2 else None,
                    chars_per_second=_probe_chars_per_second,
                    chars_per_second_by_speaker=_probe_chars_per_second_by_speaker or None,
                    source_language=self._language_profile.source_language,
                    target_language=self._language_profile.target_language,
                )
                # Phase 4.1 E.5 (Codex 2026-05-25 三签字版本 HC#5)：translate()
                # 创建的 fresh DubbingSegment 没有 tts_provider / worker routing
                # 字段。原 manual loop 只设了 tts_provider，**不覆盖** worker
                # routing → cloned voice fresh path 会漂到国际 DashScope endpoint
                # 而非武汉 worker。
                #
                # 统一改成 ``_apply_runtime_voice_overrides()`` 调用，让
                # ``tts_provider`` + ``requires_worker`` + ``worker_target_model``
                # 三字段一次性按 routing dict 落到 segment 上。
                self._apply_runtime_voice_overrides(
                    translation_result.segments,
                    voice_id_a=voice_id_a,
                    display_name_a=speaker_name_a,
                    voice_id_b=voice_id_b,
                    display_name_b=speaker_name_b,
                    speaker_voices=_speaker_voices if effective_speakers > 2 else None,
                    speaker_providers=_speaker_providers or None,
                    speaker_voice_routing=_speaker_voice_routing or None,
                )
                print(f"[S3] 完成：共 {translation_result.total_segments} 段")

            # Phase 2 Task 0 — stamp catalog_hit per segment for downstream metering.
            # Applied to BOTH cache-hit and fresh-translation paths so the metric
            # is consistent regardless of S3 cache state.  Note that translate()
            # writes segments.json BEFORE returning, so when we mark catalog_hit
            # here on the fresh-translation path it's not yet in the persisted
            # JSON; we re-serialise below so subsequent cache-hit reruns keep it.
            if _catalog_hit_speakers:
                _segments_changed = False
                for seg in translation_result.segments:
                    if seg.speaker_id in _catalog_hit_speakers and not seg.catalog_hit:
                        seg.catalog_hit = True
                        _segments_changed = True
                # Persist updated segments.json so next cache-hit run sees the flag.
                if _segments_changed:
                    try:
                        from dataclasses import asdict
                        _segments_path = (
                            final_project_dir / "translation" / "segments.json"
                        ).resolve(strict=False)
                        if _segments_path.exists():
                            _dump = {
                                "segments": [asdict(s) for s in translation_result.segments],
                                "total_segments": translation_result.total_segments,
                                "output_path": translation_result.output_path,
                            }
                            _segments_path.write_text(
                                json.dumps(_dump, ensure_ascii=False, indent=2),
                                encoding="utf-8",
                            )
                    except Exception as _exc:
                        print(f"[S4-catalog] segments.json 重写失败（非致命）：{_exc}")

            self._apply_review_speaker_styles_to_segments(
                translation_result.segments,
                _review_speaker_styles,
            )
            self._apply_speaker_structure_profiles_to_segments(
                translation_result.segments,
                _speaker_structure_profiles,
            )
            self._log_review_speaker_styles(_review_speaker_styles)

            state_manager.set_stage(
                current_stage_name,
                StageStatus.DONE,
                self._build_media_understanding_stage_payload(
                    transcript_result=transcript_result,
                    effective_speakers=effective_speakers,
                    execution_mode=media_execution_mode,
                    content_compliance=content_compliance_payload,
                ),
            )
            current_stage_name = "translation"
            state_manager.set_stage(
                current_stage_name,
                StageStatus.RUNNING,
                {
                    "execution_mode": translation_execution_mode,
                },
            )

            reuse_approved_translation_review = self._should_reuse_approved_translation_review(
                explicit_project_dir=explicit_project_dir,
                wait_for_review=config.wait_for_review,
            )
            approved_translation_review = self._get_approved_review_payload(
                review_state_manager,
                TRANSLATION_REVIEW_STAGE,
            )
            if (
                approved_translation_review is not None
                and segments_path.exists()
                and reuse_approved_translation_review
            ):
                translation_result = self._load_translation_result(segments_path)
                dubbing_modes_synced = self._apply_transcript_dubbing_modes_to_segments(
                    translation_result.segments,
                    transcript_result.lines,
                )
                # Re-apply the full voice-selection override set. Older
                # snapshots may predate tts_provider and CosyVoice worker
                # routing fields, or may have been written before voice
                # selection was approved.
                self._apply_runtime_voice_overrides(
                    translation_result.segments,
                    voice_id_a=voice_id_a,
                    display_name_a=speaker_name_a,
                    voice_id_b=voice_id_b,
                    display_name_b=speaker_name_b,
                    speaker_voices=_speaker_voices if effective_speakers > 2 else None,
                    speaker_providers=_speaker_providers or None,
                    speaker_voice_routing=_speaker_voice_routing or None,
                )
                self._apply_speaker_structure_profiles_to_segments(
                    translation_result.segments,
                    _speaker_structure_profiles,
                )
                if dubbing_modes_synced or _speaker_providers or _speaker_voice_routing:
                    self._write_segments_snapshot(translation_result)
                print("[S3] Applied approved translation review snapshot.")
            elif config.wait_for_review:
                if approved_translation_review is not None and not reuse_approved_translation_review:
                    print("[S3] Ignoring stale approved translation review from a previous run.")
                # Check if we should skip translation review (express / no-review mode)
                if not job_requires_review:
                    self._write_segments_snapshot(translation_result)
                    print("[S3] Express 模式（无需审核），跳过翻译审核。")
                else:
                    self._write_segments_snapshot(translation_result)
                    _all_speaker_names = _merge_speaker_name_map(
                        _review_speaker_names,
                        speaker_name_a,
                        speaker_name_b,
                    )
                    _unified_review_payload = self._build_translation_review_payload(translation_result, speaker_names=_all_speaker_names)
                    _unified_review_payload["speaker_name_a"] = speaker_name_a
                    _unified_review_payload["speaker_name_b"] = speaker_name_b
                    _unified_review_payload["effective_speakers"] = effective_speakers

                    if job_effective_pipeline_mode == "smart":
                        # --- Smart inline auto-translation-review path ---
                        #
                        # Plan §6.2.2 + Codex F6: deterministic 6-check
                        # decision auto-approves OR hands off to Studio.
                        # In-frame: no paused-return on approved; pipeline
                        # continues to alignment directly (matches the
                        # voice_selection_review smart branch contract).
                        #
                        # Codex 第十八轮 P1-2: gate on
                        # ``job_effective_pipeline_mode`` not raw
                        # ``job_service_mode`` so downgraded smart jobs
                        # don't re-enter the smart branch on resume.
                        from services.smart.auto_translation_review import (
                            TranslationReviewDecision,
                            evaluate_translation_review,
                        )
                        from services.smart.handoff import emit_handoff_markers
                        from services.smart.state import emit_smart_state_marker
                        from services.gemini.translator import (
                            check_glossary_preservation,
                        )

                        # Glossary stats: re-compute locally (the metering
                        # helper at process.py:938 emits into the metering
                        # body, not onto translation_result, so we call
                        # the underlying helper directly).
                        #
                        # Codex 第二十五轮 P1-2: don't fail-open on helper
                        # exception. When _review_glossary is empty/None
                        # → vacuous-pass (total=0) is correct (the gate
                        # treats glossary as not configured). But when
                        # _review_glossary is non-empty AND the helper
                        # raises (field drift, regex bug, …), silently
                        # writing total=0 would equally vacuous-pass —
                        # the gate cannot distinguish "no glossary" from
                        # "glossary check broken". Smart MUST handoff
                        # in the latter case. We track the failure in
                        # ``_smart_glossary_check_failed`` and short-
                        # circuit to handoff below before calling the
                        # deterministic gate.
                        _smart_glossary_check_failed = False
                        _smart_glossary_check_error: str | None = None
                        if _review_glossary:
                            try:
                                _smart_gloss = check_glossary_preservation(
                                    translation_result.segments,
                                    _review_glossary,
                                )
                            except Exception as _gloss_exc:
                                _smart_glossary_check_failed = True
                                _smart_glossary_check_error = str(_gloss_exc)[:200]
                                _smart_gloss = {
                                    "total_terms": 0,
                                    "preserved_terms": 0,
                                }
                        else:
                            # No glossary configured — gate vacuous-passes.
                            _smart_gloss = {
                                "total_terms": 0,
                                "preserved_terms": 0,
                            }

                        # length_overflow / final_spoken_text checksum are
                        # post-TTS signals. Pass None — auto_translation_review's
                        # _check_length_budget / _check_text_audio_checksum
                        # vacuous-pass when None. b3d / future may plumb
                        # the post-TTS rewind on resume.
                        _smart_translation_input: dict[str, object] = {
                            "glossary_total_terms": int(
                                _smart_gloss.get("total_terms", 0) or 0
                            ),
                            "glossary_preserved_terms": int(
                                _smart_gloss.get("preserved_terms", 0) or 0
                            ),
                            "length_overflow_rate": None,
                            "rewrite_attempted": False,
                            "subtitle_source_text_sha256": None,
                            "final_spoken_text_sha256": None,
                            "segments": [
                                {
                                    "segment_id": str(seg.segment_id),
                                    "speaker_id": seg.speaker_id,
                                }
                                for seg in translation_result.segments
                            ],
                        }

                        # speaker_stats — derived from
                        # _speaker_structure_profiles (S2 output).
                        # uncertain_speaker_duration_share := sum of
                        # ``fragmented``-role speakers' duration_share
                        # (matches the simulator definition of
                        # "uncertain" speakers).
                        _smart_profiles = _speaker_structure_profiles or {}
                        _smart_uncertain_share = 0.0
                        for _p in _smart_profiles.values():
                            if not isinstance(_p, dict):
                                continue
                            if str(_p.get("speaker_role") or "").lower() == "fragmented":
                                _smart_uncertain_share += float(
                                    _p.get("speaker_duration_share") or 0.0
                                )
                        _smart_speaker_stats: dict[str, object] = {
                            # Canonical speakers list (consumed by gate's
                            # main-speaker derivation, not by translation
                            # review per se, but kept here so any future
                            # check that references it has a real list).
                            "speakers": [
                                {
                                    "speaker_id": sid,
                                    "role": p.get("speaker_role"),
                                    "duration_share": p.get(
                                        "speaker_duration_share"
                                    ),
                                }
                                for sid, p in _smart_profiles.items()
                                if isinstance(p, dict)
                            ],
                            "uncertain_speaker_duration_share": (
                                _smart_uncertain_share
                            ),
                            "asr_speaker_count": len(_smart_profiles),
                        }

                        # clone_sample_stats.eligible_speakers — heuristic
                        # at b3c: count speakers with ≥10s sample (the
                        # MIN_CLONE_SAMPLE_SECONDS floor in auto_voice_review).
                        # b3d will replace this with the real Gateway /
                        # MiniMax account quota + per-speaker ffmpeg
                        # snapshot (Codex 第二十轮 three-piece contract).
                        _smart_eligible_count = sum(
                            1
                            for p in _smart_profiles.values()
                            if isinstance(p, dict)
                            and int(p.get("speaker_duration_ms") or 0) >= 10_000
                        )
                        _smart_clone_sample_stats: dict[str, object] = {
                            "eligible_speakers": _smart_eligible_count,
                        }

                        # Codex 第二十五轮 P1-2: glossary helper failure
                        # short-circuit. The gate cannot distinguish a
                        # broken helper from "no glossary" — synthesize
                        # the handoff decision directly so smart never
                        # auto-approves a job whose glossary check is
                        # silently bypassed.
                        if _smart_glossary_check_failed:
                            _smart_translation_decision = TranslationReviewDecision(
                                auto_approved=False,
                                reason_code="glossary_check_error",
                                failed_check="glossary_preservation",
                                metrics={
                                    "glossary_check_error": (
                                        _smart_glossary_check_error
                                        or "unknown"
                                    ),
                                    "glossary_configured_terms": len(
                                        _review_glossary or {}
                                    ),
                                },
                            )
                        else:
                            # Codex 第二十五轮 P1-1: derive compliance_block
                            # from content_compliance_payload.
                            # ContentComplianceResult.status="blocked"
                            # is the canonical signal (see
                            # ``src/services/content_compliance.py:118``).
                            # We treat any "blocked" status as
                            # auto-approve-unsafe regardless of
                            # ``admin_override`` — admin override is for
                            # the legacy human gate; smart must still
                            # defer translation review to Studio so the
                            # user re-confirms the bypass in context.
                            _smart_compliance_block = bool(
                                isinstance(content_compliance_payload, dict)
                                and content_compliance_payload.get("status")
                                == "blocked"
                            )
                            _smart_translation_decision = evaluate_translation_review(
                                translation_result=_smart_translation_input,
                                speaker_stats=_smart_speaker_stats,
                                clone_sample_stats=_smart_clone_sample_stats,
                                compliance_block=_smart_compliance_block,
                            )

                        if not _smart_translation_decision.auto_approved:
                            # Handoff: plan §6.5 three-tuple
                            # (set_stage + smart_state + web_review_marker).
                            _emit_smart_audit(
                                final_project_dir,
                                decision_type="translation_auto_approve",
                                decision="rejected",
                                reason_code=(
                                    _smart_translation_decision.reason_code
                                ),
                                evidence=dict(
                                    _smart_translation_decision.metrics or {}
                                ),
                                extra={
                                    "failed_check": (
                                        _smart_translation_decision.failed_check
                                    ),
                                    "job_id": str(_snap("job_id") or ""),
                                    "user_id": str(_snap("user_id") or ""),
                                    "handoff_stage": TRANSLATION_REVIEW_STAGE,
                                },
                            )
                            emit_handoff_markers(
                                review_state_manager=review_state_manager,
                                review_stage=TRANSLATION_REVIEW_STAGE,
                                review_payload=_unified_review_payload,
                                review_pending_status=REVIEW_STATUS_PENDING,
                                smart_state_update={
                                    "status": "downgraded_to_studio",
                                    "reason": (
                                        _smart_translation_decision.reason_code
                                        or "translation_review_auto_rejected"
                                    ),
                                    "handoff_stage": TRANSLATION_REVIEW_STAGE,
                                    "failed_check": (
                                        _smart_translation_decision.failed_check
                                    ),
                                },
                                project_dir=final_project_dir,
                                user_message=(
                                    "智能版自动翻译审核需要人工接管:"
                                    f" {_smart_translation_decision.reason_code}"
                                ),
                                web_review_marker_builder=self._build_web_review_marker,
                            )
                            state_manager.set_stage(
                                current_stage_name,
                                StageStatus.DONE,
                                self._build_translation_stage_payload(
                                    translation_result=translation_result,
                                    execution_mode=translation_execution_mode,
                                ),
                            )
                            current_stage_name = None
                            _write_usage_summary(usage_meter)
                            # Codex 第三十六轮 P1: cost_summary at smart handoff.
                            _emit_smart_cost_summary_from_meter(
                                final_project_dir,
                                job_id=config.job_id,
                                usage_meter=usage_meter,
                                minutes_processed=(
                                    # Codex 第三十七轮 P1: prefer ffprobe
                                    # actual_duration_ms over unreliable _snap.
                                    float(actual_duration_ms) / 60000.0
                                    if actual_duration_ms
                                    else float(
                                        _snap("source_duration_seconds") or 0.0
                                    ) / 60.0
                                ),
                                credits_policy="pending_settle",
                            )
                            return self._build_paused_result(
                                project_dir=final_project_dir,
                                stage=TRANSLATION_REVIEW_STAGE,
                                message="智能版自动翻译审核需要人工接管",
                            )

                        # Auto-approved: set_stage(APPROVED) + intermediate
                        # smart_state marker + fall through to alignment.
                        _smart_approved_translation_payload = dict(
                            _unified_review_payload
                        )
                        _smart_approved_translation_payload["auto_approved"] = True
                        review_state_manager.set_stage(
                            TRANSLATION_REVIEW_STAGE,
                            status=REVIEW_STATUS_APPROVED,
                            payload=_smart_approved_translation_payload,
                            activate=True,
                        )
                        # Codex 第十八轮 P0-1: intermediate marker MUST NOT
                        # set top-level ``status`` — only the terminal
                        # marker (helper _emit_smart_terminal_completion_marker
                        # at happy-path exit) writes editable status.
                        emit_smart_state_marker(
                            {
                                "auto_translation_review": {
                                    "auto_approved": True,
                                    "failed_check": None,
                                    "metrics": (
                                        _smart_translation_decision.metrics
                                    ),
                                },
                            }
                        )

                        # Sidecar audit: translation auto-approved.
                        _emit_smart_audit(
                            final_project_dir,
                            decision_type="translation_auto_approve",
                            decision="approved",
                            evidence=dict(
                                _smart_translation_decision.metrics or {}
                            ),
                            extra={
                                "job_id": str(_snap("job_id") or ""),
                                "user_id": str(_snap("user_id") or ""),
                            },
                        )

                        print(
                            "[S3] Smart 自动翻译审核通过,继续 TTS。"
                        )
                        # No paused-return — pipeline falls through to
                        # the legacy state_manager.set_stage(DONE) +
                        # alignment block immediately below.
                    else:
                        # --- Legacy Studio path: pending + paused-return ---
                        review_state_manager.set_stage(
                            TRANSLATION_REVIEW_STAGE,
                            status=REVIEW_STATUS_PENDING,
                            payload=_unified_review_payload,
                            activate=True,
                        )
                        review_message = "等待在 Web UI 确认翻译稿，再继续 TTS 和对齐。"
                        print(f"[S3] {review_message}")
                        state_manager.set_stage(
                            current_stage_name,
                            StageStatus.DONE,
                            self._build_translation_stage_payload(
                                translation_result=translation_result,
                                execution_mode=translation_execution_mode,
                            ),
                        )
                        current_stage_name = None
                        print(
                            self._build_web_review_marker(
                                stage=TRANSLATION_REVIEW_STAGE,
                                project_dir=final_project_dir,
                                message=review_message,
                            )
                        )
                        _write_usage_summary(usage_meter)
                        return self._build_paused_result(
                            project_dir=final_project_dir,
                            stage=TRANSLATION_REVIEW_STAGE,
                            message=review_message,
                        )

            state_manager.set_stage(
                current_stage_name,
                StageStatus.DONE,
                self._build_translation_stage_payload(
                    translation_result=translation_result,
                    execution_mode=translation_execution_mode,
                ),
            )

            current_stage_name = "alignment"
            state_manager.set_stage(
                current_stage_name,
                StageStatus.RUNNING,
                {
                    "execution_mode": "legacy_process",
                },
            )
            # Inject voice metadata from LLM review into segments for TTS voice selection
            if _review_speaker_styles:
                from services.tts.cosyvoice_voice_selector import infer_persona_style, infer_energy_level
                for segment in translation_result.segments:
                    spk_info = _review_speaker_styles.get(segment.speaker_id, {})
                    vd = spk_info.get("voice_description", "")
                    segment.voice_description = vd
                    segment.gender = spk_info.get("gender", "")
                    segment.age_group = spk_info.get("age_group", "")
                    # Inject persona_style / energy_level: prefer reviewer output, fallback to local inference
                    segment.persona_style = spk_info.get("persona_style", "") or infer_persona_style(vd)
                    segment.energy_level = spk_info.get("energy_level", "") or infer_energy_level(vd)
                print(f"[S4] 注入音色描述：{len(_review_speaker_styles)} 个说话人", flush=True)
                for spk_id, spk_info in _review_speaker_styles.items():
                    name = spk_info.get("name", "")
                    vd = spk_info.get("voice_description", "")
                    gender = spk_info.get("gender", "")
                    age = spk_info.get("age_group", "")
                    ps = spk_info.get("persona_style", "") or infer_persona_style(vd)
                    el = spk_info.get("energy_level", "") or infer_energy_level(vd)
                    print(f"  {spk_id} ({name}, {gender}/{age}, persona={ps}, energy={el}): {vd[:80]}", flush=True)

            tts_generator = TTSGenerator(tts_config, job_record=config.job_record)
            _set_usage_meter_if_supported(tts_generator, usage_meter)
            tts_results = []
            # Phase 2 Task 1 — pipe per-speaker chars/sec into TTS so MiniMax
            # can compute per-segment voice_setting.speed (feature-flagged).
            try:
                tts_generator.set_speaker_chars_per_second(
                    _probe_chars_per_second_by_speaker or None,
                    global_cps=_probe_chars_per_second
                    if _probe_chars_per_second != DEFAULT_ESTIMATED_TTS_CHARS_PER_SECOND
                    else None,
                )
            except Exception as _exc:
                # Defensive: if the new method is missing (rolling deploy), continue.
                print(f"[S4] set_speaker_chars_per_second skipped: {_exc}", flush=True)
            # Phase 2a free tier — tell the generator the job's voice_strategy
            # (gates the MiMo voiceclone dispatch) and, for free_voiceclone jobs,
            # stamp each segment with a per-speaker reference clip cut from the
            # clean speech_for_asr.wav. Best-effort: any failure leaves segments
            # unstamped → dispatch falls back to the base MiMo preset (free).
            try:
                tts_generator.set_voice_strategy(job_voice_strategy)
                if job_voice_strategy == "free_voiceclone":
                    from services.tts.voiceclone_reference import stamp_segment_references
                    _vc_stamped = stamp_segment_references(
                        translation_result.segments,
                        final_project_dir / "audio" / "speech_for_asr.wav",
                        final_project_dir / "audio" / "voiceclone_ref",
                    )
                    print(
                        f"[S4] free voiceclone: stamped {_vc_stamped} segment(s) "
                        "with per-speaker reference",
                        flush=True,
                    )
            except Exception as _vc_exc:
                # Never block the pipeline; unstamped segments use the base MiMo
                # preset (free) via the tts_generator dispatch.
                print(f"[S4] free voiceclone setup skipped: {_vc_exc}", flush=True)
            tts_dir = (final_project_dir / "tts").resolve(strict=False)
            rewriter_kwargs: dict[str, object] = {}
            custom_rewrite_prompt_template = gemini_config.get("rewrite_prompt_template")
            if custom_rewrite_prompt_template is not None:
                rewriter_kwargs["rewrite_prompt_template"] = str(custom_rewrite_prompt_template)
            keep_original_count = self._materialize_keep_original_segments(
                translation_result.segments,
                source_audio_path=source_audio_path,
                tts_dir=tts_dir,
            )
            if keep_original_count:
                print(f"[S4] 保留原音：已准备 {keep_original_count} 个原音片段")
            short_merge_summary = self._apply_short_segment_merges_before_tts(
                translation_result
            )
            if (
                short_merge_summary.get("applied_count", 0)
                or short_merge_summary.get("blocked_cross_speaker_count", 0)
            ):
                print(
                    "[S4] Short-segment merge: "
                    f"applied={short_merge_summary.get('applied_count', 0)}, "
                    f"absorbed={short_merge_summary.get('absorbed_count', 0)}, "
                    f"cross-speaker blocked={short_merge_summary.get('blocked_cross_speaker_count', 0)}"
                )
            if short_merge_summary.get("applied_count", 0):
                cleared_cache_count = self._clear_short_merge_tts_cache(
                    translation_result.segments,
                    tts_dir,
                )
                if cleared_cache_count:
                    print(
                        f"[S4] Cleared {cleared_cache_count} stale short-merge TTS cache file(s)."
                    )
            auto_keep_original_count = self._materialize_empty_text_keep_original_segments(
                translation_result.segments,
                source_audio_path=source_audio_path,
                tts_dir=tts_dir,
            )
            if auto_keep_original_count:
                print(
                    "[S4] Empty-text guard: "
                    f"auto-kept {auto_keep_original_count} short/non-speech segment(s) as original audio"
                )
            if s3_cache_hit:
                cached_segments, segments_needing_tts = self._hydrate_cached_tts_segments(
                    translation_result.segments,
                    tts_dir,
                )
                pre_tts_chars_per_second = (
                    _probe_chars_per_second
                    if _probe_chars_per_second and _probe_chars_per_second > 0
                    else DEFAULT_ESTIMATED_TTS_CHARS_PER_SECOND
                )
                pre_tts_chars_per_second_by_speaker: dict[str, float] = dict(
                    _probe_chars_per_second_by_speaker or {}
                )
                if cached_segments:
                    cached_chars_per_second, cached_chars_per_second_by_speaker = (
                        self._calibrate_tts_duration(cached_segments)
                    )
                    if (
                        cached_chars_per_second
                        and cached_chars_per_second > 0
                        and pre_tts_chars_per_second == DEFAULT_ESTIMATED_TTS_CHARS_PER_SECOND
                    ):
                        pre_tts_chars_per_second = cached_chars_per_second
                    for _spk, _cps in (cached_chars_per_second_by_speaker or {}).items():
                        if _cps and _cps > 0:
                            pre_tts_chars_per_second_by_speaker[_spk] = _cps
                pre_tts_rewriter = GeminiRewriter(
                    translator,
                    chars_per_second=pre_tts_chars_per_second,
                    chars_per_second_by_speaker=pre_tts_chars_per_second_by_speaker,
                    usage_phase="pre_tts_rewrite",
                    **rewriter_kwargs,
                )
                pre_tts_rewrite_count = self._pre_rewrite_obvious_overshoot_segments_before_tts(
                    segments=segments_needing_tts,
                    rewriter=pre_tts_rewriter,
                    chars_per_second=pre_tts_chars_per_second,
                    chars_per_second_by_speaker=pre_tts_chars_per_second_by_speaker,
                    job_provider=getattr(tts_generator, "_job_provider", None),
                )
                if pre_tts_rewrite_count > 0:
                    print(
                        f"[S4] Pre-rewrote {pre_tts_rewrite_count} obvious long segment(s) "
                        "before TTS generation."
                    )
                    cleared_pre_rewrite_cache_count = self._clear_pre_tts_rewrite_audio_cache(
                        segments_needing_tts,
                        tts_dir,
                    )
                    if cleared_pre_rewrite_cache_count:
                        print(
                            "[S4] Cleared "
                            f"{cleared_pre_rewrite_cache_count} stale pre-rewrite audio cache file(s)."
                        )
                if segments_needing_tts:
                    print(
                        "[S4] 生成TTS音频..."
                        f"（{len(cached_segments)}段已缓存，{len(segments_needing_tts)}段需生成）"
                    )
                    tts_results = _generate_tts_all_with_bucket(
                        tts_generator,
                        segments_needing_tts,
                        str(tts_dir),
                        usage_bucket=TTS_BUCKET_FIRST,
                    )
                    print(
                        f"[S4] 完成：复用 {len(cached_segments)} 段缓存，"
                        f"新生成 {len(segments_needing_tts)} 段"
                    )
                else:
                    print("[S4] 所有TTS音频已缓存，跳过生成")
            else:
                segments_needing_tts = [
                    segment for segment in translation_result.segments
                    if not is_keep_original_dubbing_mode(
                        getattr(segment, "dubbing_mode", DUBBING_MODE_DUB)
                    )
                ]
                if _is_pre_tts_rewrite_enabled():
                    pre_tts_rewriter = GeminiRewriter(
                        translator,
                        usage_phase="pre_tts_rewrite",
                        **rewriter_kwargs,
                    )
                    pre_tts_rewrite_count = self._pre_rewrite_obvious_overshoot_segments_before_tts(
                        segments=segments_needing_tts,
                        rewriter=pre_tts_rewriter,
                        chars_per_second=pre_tts_rewriter.chars_per_second,
                        chars_per_second_by_speaker=pre_tts_rewriter.chars_per_second_by_speaker,
                        job_provider=getattr(tts_generator, "_job_provider", None),
                    )
                    if pre_tts_rewrite_count > 0:
                        print(
                            f"[S4] Pre-rewrote {pre_tts_rewrite_count} obvious long segment(s) "
                            "before TTS generation."
                        )
                        cleared_pre_rewrite_cache_count = self._clear_pre_tts_rewrite_audio_cache(
                            segments_needing_tts,
                            tts_dir,
                        )
                        if cleared_pre_rewrite_cache_count:
                            print(
                                "[S4] Cleared "
                                f"{cleared_pre_rewrite_cache_count} stale pre-rewrite audio cache file(s)."
                            )
                else:
                    print("[S4] Pre-TTS 预重写已关闭（管理员设置）")
                if segments_needing_tts:
                    print("[S4] 生成TTS音频...")
                    tts_results = _generate_tts_all_with_bucket(
                        tts_generator,
                        segments_needing_tts,
                        str(tts_dir),
                        usage_bucket=TTS_BUCKET_FIRST,
                    )
                    print(f"[S4] 完成：生成 {len(tts_results)} 个音频片段")
                else:
                    print("[S4] 所有片段均保留原音，跳过TTS生成")

            chars_per_second, chars_per_second_by_speaker = self._calibrate_tts_duration(
                translation_result.segments
            )
            print(f"[S4] TTS时长标定：global chars_per_second = {chars_per_second:.2f}")
            speaker_display_names: dict[str, str] = {}
            for segment in translation_result.segments:
                speaker_display_names.setdefault(segment.speaker_id, segment.display_name)
            for speaker_id, speaker_chars_per_second in chars_per_second_by_speaker.items():
                speaker_label = speaker_display_names.get(speaker_id, speaker_id)
                print(f"[S4] {speaker_label} chars_per_second = {speaker_chars_per_second:.2f}")

            post_tts_budget_tracker = PostTTSBudgetTracker()
            presplit_count = self._presplit_long_overshoot_segments_before_alignment(
                translation_result=translation_result,
                tts_generator=tts_generator,
                tts_dir=tts_dir,
                post_tts_budget_tracker=post_tts_budget_tracker,
            )
            if presplit_count > 0:
                chars_per_second, chars_per_second_by_speaker = self._calibrate_tts_duration(
                    translation_result.segments
                )
                print(f"[S4] Pre-split {presplit_count} long overshoot segment(s) before alignment.")
                print(f"[S4] Recalibrated global chars_per_second = {chars_per_second:.2f}")
                speaker_display_names = {}
                for segment in translation_result.segments:
                    speaker_display_names.setdefault(segment.speaker_id, segment.display_name)
                for speaker_id, speaker_chars_per_second in chars_per_second_by_speaker.items():
                    speaker_label = speaker_display_names.get(speaker_id, speaker_id)
                    print(
                        f"[S4] {speaker_label} recalibrated chars_per_second = "
                        f"{speaker_chars_per_second:.2f}"
                    )

            print("[S5] 对齐时间轴...")
            rewriter = GeminiRewriter(
                translator,
                chars_per_second=chars_per_second,
                chars_per_second_by_speaker=chars_per_second_by_speaker,
                usage_phase="post_tts_rewrite",
                **rewriter_kwargs,
            )
            aligned_segments = SegmentAligner(
                rewriter=rewriter,
                tts_generator=tts_generator,
                post_tts_budget_tracker=post_tts_budget_tracker,
            ).align_all(
                translation_result.segments,
                str(tts_dir),
            )
            repaired_count = self._repair_failed_long_segments(
                translation_result=translation_result,
                tts_generator=tts_generator,
                rewriter=rewriter,
                tts_dir=tts_dir,
                post_tts_budget_tracker=post_tts_budget_tracker,
            )
            if repaired_count > 0:
                aligned_segments = self._build_aligned_segments(translation_result.segments)
                print(f"[S5] Long-segment semantic split repaired {repaired_count} segment(s).")
            low_info_keep_count = self._auto_keep_low_information_underflow_segments(
                translation_result.segments,
                source_audio_path=source_audio_path,
                tts_dir=tts_dir,
            )
            if low_info_keep_count:
                aligned_segments = self._build_aligned_segments(translation_result.segments)
                print(
                    "[S5] Low-information underflow route: "
                    f"kept {low_info_keep_count} segment(s) as original audio."
                )
            if (
                short_merge_summary.get("candidate_count", 0)
                or short_merge_summary.get("blocked_cross_speaker_count", 0)
            ):
                print(
                    "[S5] Short-segment merge audit: "
                    f"same-speaker candidates={short_merge_summary.get('candidate_count', 0)}, "
                    f"cross-speaker blocked={short_merge_summary.get('blocked_cross_speaker_count', 0)}"
                )
            sync_repairs = _sync_tts_text_audio_for_publish(translation_result.segments)
            if sync_repairs:
                print(
                    "[S5] TTS text/audio sync repaired: "
                    + "; ".join(sync_repairs[:8])
                    + ("" if len(sync_repairs) <= 8 else f"; +{len(sync_repairs) - 8} more"),
                    flush=True,
                )
            self._write_segments_snapshot(translation_result)
            needs_review_count = sum(1 for segment in aligned_segments if segment.needs_review)
            print(
                f"[S5] 完成：共 {len(aligned_segments)} 段，"
                f"需要人工检查 {needs_review_count} 段"
            )
            state_manager.set_stage(
                current_stage_name,
                StageStatus.DONE,
                self._build_alignment_stage_payload(translation_result.segments),
            )
            current_stage_name = "legacy_process_output"
            state_manager.set_stage(
                current_stage_name,
                StageStatus.RUNNING,
                {
                    "execution_mode": "legacy_process_output_dispatch",
                },
            )

            print("[S6] 合成输出...")
            build_result = self._build_process_workflow_build_result(
                project_dir=final_project_dir,
                youtube_url=normalized_url,
                download_result=download_result,
                video_path=video_path,
                source_audio_path=source_audio_path,
                separated_audio=separated_audio,
                transcript_result=transcript_result,
                translation_result=translation_result,
                total_duration_ms=actual_duration_ms,
                segments=translation_result.segments,
                stage_snapshot=state_manager.load().get("stages", {}),
                source_type=source_type,
            )
            # Phase 2a Task 8 (gate #8): free service-mode jobs get a burned-in
            # watermark on publish.dubbed_video (paid modes ship clean).
            # plan 2026-06-12 §C ①：策略档经 effective_policy_mode——匿名
            # 预览（含匿名 express）恒水印，不看 service_mode 字面量。
            from services.r2_publisher_lib.downloadable_keys import effective_policy_mode
            from utils.free_watermark import free_watermark_text_for
            output_bundle = self._dispatch_process_output_bundle(
                project_dir=final_project_dir,
                build_result=build_result,
                watermark_text=free_watermark_text_for(
                    effective_policy_mode(
                        job_service_mode,
                        job_anonymous_preview,
                        smart_preview=job_smart_preview,
                    )
                ),
            )
            assert output_bundle.editor_result is not None
            output_result = output_bundle.editor_result
            state_manager.set_stage(
                current_stage_name,
                StageStatus.DONE,
                self._build_legacy_process_output_stage_payload(
                    output_bundle=output_bundle,
                ),
            )
            current_stage_name = None
            print(f"[S6] 完成：输出目录 {final_project_dir / 'output'}")

            # Phase 2 Task 0 hotfix v2 — force-rewrite segments.json with the
            # full DubbingSegment schema (including catalog_hit / dsp_speed_param /
            # first_pass_duration_ms / first_pass_error_pct) RIGHT BEFORE we
            # report metering. This guarantees:
            #   1. the persisted JSON matches the in-memory metric values
            #   2. the next run's cache-hit path can read these fields back
            #   3. we don't depend on every upstream JSON writer (translator,
            #      translation_review, speaker_review) preserving the schema
            try:
                from dataclasses import asdict
                _segments_path = (
                    final_project_dir / "translation" / "segments.json"
                ).resolve(strict=False)
                if _segments_path.exists() and hasattr(translation_result, "segments"):
                    _dump = {
                        "segments": [asdict(s) for s in translation_result.segments],
                        "total_segments": getattr(translation_result, "total_segments",
                                                  len(translation_result.segments)),
                        "output_path": getattr(translation_result, "output_path", str(_segments_path)),
                    }
                    _segments_path.write_text(
                        json.dumps(_dump, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    print(f"[S6] segments.json final-rewrite OK ({len(_dump['segments'])} segs with full metric schema)")
            except Exception as _exc:
                print(f"[S6] segments.json final-rewrite skipped (non-fatal): {_exc}")

            # Studio post-edit baseline — snapshot translation/segments.json
            # (the authoritative DubbingSegment dump just rewritten above)
            # into editor/segments.json so that enter_editing has a baseline
            # on its very first call. Without this, the editing layer would
            # have to lazy-seed on demand from translation/, which works
            # but means the first user to click "修改" pays the seed cost
            # and any mismatch between translation/ shape today vs. the day
            # the seed lands is impossible to diagnose from the disk snapshot.
            # See services.jobs.editor_baseline for the shared helper.
            # Non-fatal by design: if this write fails (e.g. permission error
            # or translation/ just got removed by a parallel cleanup), the
            # legacy lazy-seed fallback in editing.enter_editing still works.
            try:
                from services.jobs.editor_baseline import (
                    write_editor_segments_from_translation,
                )
                _baseline_path = write_editor_segments_from_translation(
                    final_project_dir
                )
                print(
                    f"[S6] editor/segments.json baseline written: {_baseline_path}"
                )
            except Exception as _exc:
                print(
                    f"[S6] editor/segments.json baseline skipped "
                    f"(non-fatal, lazy seed will cover): {_exc}"
                )

            _voice_profile_metering: dict[str, object] = {}
            try:
                _voice_profiles, _voice_profile_skips = (
                    self._build_user_voice_speed_profiles(
                        translation_result.segments,
                        default_provider=job_tts_provider or "minimax",
                        tts_model=str(_snap("tts_model") or getattr(tts_config, "model", "") or ""),
                    )
                )
                _voice_profile_metering = self._persist_user_voice_speed_profiles(
                    job_id=config.job_id,
                    user_id=str(_snap("user_id") or ""),
                    profiles=_voice_profiles,
                    skipped_reasons=_voice_profile_skips,
                )
            except Exception as _exc:
                print(
                    f"[P1-l] Voice speed profile persistence skipped "
                    f"(non-fatal): {_exc}",
                    flush=True,
                )

            # V3-4/V3-5: report pipeline metering to Gateway (best-effort)
            if config.job_id and hasattr(translation_result, "segments"):
                _usage_summary = _write_usage_summary(usage_meter)
                _usage_summary["transcription_method"] = transcription_method
                _usage_summary["asr_provider"] = (
                    "gemini" if transcription_method == "gemini" else "assemblyai"
                )
                _usage_summary["asr_provider_cost_status"] = (
                    "legacy_guard" if transcription_method == "gemini" else "ignored_low_cost"
                )
                # Fallback for older tests/fakes that bypass TTSGenerator's usage hook.
                try:
                    _legacy_tts_billed = sum(getattr(r, "billed_chars", 0) for r in tts_results)
                except Exception:
                    _legacy_tts_billed = 0
                if _legacy_tts_billed and not _usage_summary.get("tts_billed_chars"):
                    _usage_summary["first_tts_billed_chars"] = _legacy_tts_billed
                    _usage_summary["tts_billed_chars"] = _legacy_tts_billed

                _extra_metering = dict(_usage_summary)
                _extra_metering.update(_voice_profile_metering or {})
                _report_job_metering(
                    config.job_id, translation_result.segments,
                    tts_billed_chars=None,
                    glossary=_review_glossary or None,
                    extra_metering=_extra_metering,
                )
        except Exception as exc:
            stage_label = _classify_failed_stage(exc)
            if state_manager is not None and current_stage_name is not None:
                state_manager.set_stage(
                    current_stage_name,
                    StageStatus.FAILED,
                    {
                        "execution_mode": "legacy_process",
                        "error_type": exc.__class__.__name__,
                    },
                    error_message=str(exc),
                )
            _write_usage_summary(usage_meter)
            print(f"[{stage_label}] 失败：{exc}")
            raise

        # PR#3C-b3a: terminal smart_state marker (see
        # _emit_smart_terminal_completion_marker docstring). No-op for
        # non-smart jobs; for smart jobs, flips status → "completed" +
        # credits_policy → "capture_full" so editing / jianying gates
        # admit + settle dispatcher routes through smart_capture_full.
        # service_mode passed explicitly per Codex 第二十一轮 P0.
        self._emit_smart_terminal_completion_marker(
            service_mode=self._current_service_mode,
        )

        # PR#3C-P3-a: terminal quality_report write for smart jobs.
        # Only happy-path mains-run terminal — resume publish-only
        # path skips this (preserves original audit; pinned by
        # tests/test_smart_quality_report_writer.py terminal-wiring
        # anchor test).
        # Local-var access via ``locals().get(...)`` defensively
        # because smart inline branches (eligibility / voice review /
        # translation review) only run when ``requires_review=True``
        # AND ``effective_pipeline_mode==smart``. A smart job with
        # requires_review=False reaches this terminal without those
        # vars being bound — locals().get() returns None gracefully.
        #
        # Codex 第三十五轮 P1: dual-gate on BOTH raw service_mode AND
        # job_effective_pipeline_mode. A smart job that hit handoff
        # earlier (smart_state.status="downgraded_to_studio") and was
        # resumed via Studio /continue still satisfies raw
        # service_mode=="smart" at terminal, but its
        # ``job_effective_pipeline_mode`` is "studio" (derived from
        # smart_state.status via ``derive_effective_pipeline_mode``).
        # Single-gate would write an EMPTY quality_report.json on that
        # path (no smart inline locals were populated on the continue
        # run), and the P3-c renderer would misread the empty file
        # as a clean happy-path. Per decision log §P3-a scope-down,
        # handoff-after-continue audits live in JSONL events only.
        if (
            self._current_service_mode == "smart"
            and job_effective_pipeline_mode == "smart"
        ):
            _local_elig = locals().get("_smart_eligibility")
            _local_vr = locals().get("_smart_voice_review")
            _local_tr_dec = locals().get("_smart_translation_decision")
            _local_mirror_fail = locals().get(
                "_smart_clone_mirror_failures"
            ) or []
            _local_per_speaker_seconds = locals().get(
                "_smart_per_speaker_sample_seconds"
            ) or {}

            # Build speaker_summary from eligibility (when available)
            if _local_elig is not None:
                _qr_speaker_summary = {
                    "main_speaker_count": _local_elig.main_speaker_count,
                    "main_speaker_ids": list(
                        _local_elig.main_speaker_ids
                    ),
                    "excluded_speakers": list(
                        _local_elig.excluded_speakers
                    ),
                }
            else:
                _qr_speaker_summary = {
                    "main_speaker_count": 0,
                    "main_speaker_ids": [],
                    "excluded_speakers": [],
                }

            # Build voice_decisions from voice review (when available).
            # Task #27 (2026-05-24): extracted to module-level helper
            # ``_build_quality_report_voice_decisions`` so the
            # reason_code passthrough + Phase 5 field preservation is
            # unit-testable. Symmetric to _apply_smart_reused_voice_decision.
            _qr_voice_decisions: list[dict] = (
                _build_quality_report_voice_decisions(
                    _local_vr.decisions, _local_per_speaker_seconds,
                )
                if _local_vr is not None
                else []
            )

            # Build translation_review from decision (when available)
            _qr_translation_review = None
            if _local_tr_dec is not None:
                _qr_translation_review = {
                    "auto_approved": _local_tr_dec.auto_approved,
                    "failed_check": _local_tr_dec.failed_check,
                    "metrics": dict(_local_tr_dec.metrics or {}),
                }

            # Retry summary — PR#3C-P3-d wires the real aggregator
            # (replaces the always-zero placeholder). Sources:
            #   - segments[*].pre_tts_rewrite_retry_attempted
            #   - post_tts_budget_tracker.usage_summary().total_consumed
            #   - retry_budget.compute_total_budget_minutes(source_minutes)
            # Best-effort: aggregator failure falls back to zeros via
            # try/except so emit can't block terminal return.
            #
            # Codex 第三十七轮 P1: minutes source MUST be the reliable
            # ``actual_duration_ms`` (ffprobe-derived at line ~2243),
            # NOT the unreliable ``_snap("source_duration_seconds")``
            # which observes as 0 at terminal time. Without this fix
            # the user-visible retry_summary.budget_remaining_minutes
            # falsely shows 0.0 on every smart job. _snap kept only as
            # last-resort fallback if actual_duration_ms isn't bound.
            try:
                _local_post_tts_tracker = locals().get(
                    "post_tts_budget_tracker"
                )
                _local_translation_result = locals().get(
                    "translation_result"
                )
                _qr_segments_source = (
                    list(_local_translation_result.segments)
                    if _local_translation_result is not None
                    else []
                )
                _local_actual_ms = locals().get("actual_duration_ms")
                if _local_actual_ms:
                    _qr_source_minutes = float(_local_actual_ms) / 60000.0
                else:
                    _qr_source_minutes = (
                        float(_snap("source_duration_seconds") or 0.0) / 60.0
                    )
                _qr_retry_summary = _aggregate_smart_retry_stats(
                    segments=_qr_segments_source,
                    post_tts_budget_tracker=_local_post_tts_tracker,
                    source_minutes=_qr_source_minutes,
                )
            except Exception as _exc:
                print(
                    f"[smart] retry_summary aggregation failed "
                    f"(non-blocking): {type(_exc).__name__}: {_exc}",
                    flush=True,
                )
                _qr_retry_summary = {
                    "rewrite_attempts_used": 0,
                    "retts_attempts_used": 0,
                    "budget_remaining_minutes": 0.0,
                }

            # Emit one budget_exhausted sidecar event per exhausted root.
            # Renderer (P3-c) reads JSONL for "retry history" panel;
            # admin diagnostics can see which segments exhausted cap.
            _emit_smart_budget_exhausted_events(
                project_dir=final_project_dir,
                post_tts_budget_tracker=locals().get(
                    "post_tts_budget_tracker"
                ),
                job_id=config.job_id,
                user_id=str(_snap("user_id") or ""),
            )

            _emit_smart_quality_report(
                final_project_dir,
                job_id=config.job_id,
                user_id=str(_snap("user_id") or ""),
                service_mode="smart",
                smart_state_final={
                    "status": "completed",
                    "credits_policy": "capture_full",
                },
                speaker_summary=_qr_speaker_summary,
                voice_decisions=_qr_voice_decisions,
                translation_review=_qr_translation_review,
                retry_summary=_qr_retry_summary,
                handoff_history=[],  # happy-path: no handoffs occurred
            )

            # PR#3C-P3-b: admin-only cost summary. Same dual-gate as
            # quality_report. Settle-dependent fields
            # (credits_charged + minimax_quota_used_after) are unknown
            # at pipeline terminal — Gateway sets them post-settle
            # (P3-b follow-up); pipeline writes None for now.
            #
            # UsageMeter-derived values (asr / llm / tts / voice_clone):
            # the meter is in scope as ``usage_meter`` for the smart
            # branch (created early in run()). Defensive: wrap the
            # summarize() call in try/except so a meter-internal bug
            # can't block the user-facing return.
            _cs_asr_seconds = 0.0
            _cs_llm_chars = 0
            _cs_tts_chars = 0
            _cs_voice_clone_calls = 0
            try:
                _meter_summary = usage_meter.summarize()
                _cs_asr_seconds = float(
                    _meter_summary.get("llm_audio_input_seconds") or 0.0
                )
                # Translation char-count proxy: tts_billed_chars in the
                # "first" bucket = first-pass-finalized TTS, which is
                # roughly proportional to translated chars. Decision
                # log §2 lists "llm_translation_chars" — the closest
                # proxy at terminal time.
                _cs_llm_chars = int(
                    _meter_summary.get("s3_translation_llm_input_tokens") or 0
                )
                _cs_tts_chars = int(
                    _meter_summary.get("tts_billed_chars") or 0
                )
                _cs_voice_clone_calls = int(
                    _meter_summary.get("voice_clone_success_call_count") or 0
                )
            except Exception as _exc:
                print(
                    f"[smart] cost_summary meter probe failed: "
                    f"{type(_exc).__name__}: {_exc}",
                    flush=True,
                )

            # Minutes processed — Codex 第三十七轮 P1: prefer the
            # reliable ffprobe-derived ``actual_duration_ms`` over the
            # unreliable ``_snap("source_duration_seconds")`` which
            # observes as 0 at terminal time. Without this fix admin
            # sees ``minutes_processed=0`` on every smart cost_summary,
            # making the file useless for diagnostics. _snap kept only
            # as last-resort fallback if actual_duration_ms isn't bound
            # (e.g. resume-publish-only path that re-enters terminal
            # without going through S0).
            _local_actual_ms_cs = locals().get("actual_duration_ms")
            if _local_actual_ms_cs:
                _cs_minutes = float(_local_actual_ms_cs) / 60000.0
            else:
                _cs_minutes = float(_snap("source_duration_seconds") or 0.0) / 60.0

            _emit_smart_cost_summary(
                final_project_dir,
                job_id=config.job_id,
                service_mode="smart",
                minutes_processed=round(_cs_minutes, 3),
                pending_credits_charged=None,  # settled by Gateway post-pipeline
                credits_policy="capture_full",
                asr_seconds=round(_cs_asr_seconds, 3),
                llm_translation_chars=_cs_llm_chars,
                tts_chars=_cs_tts_chars,
                voice_clone_calls=_cs_voice_clone_calls,
                pending_minimax_quota_used_after=None,  # queried by Gateway post-pipeline
            )

        return ProcessResult(
            project_dir=str(final_project_dir.resolve(strict=False)),
            dubbed_audio_path=output_result.dubbed_audio_path,
            ambient_audio_path=output_result.ambient_audio_path,
            subtitles_path=output_result.subtitles_path,
            segments_dir=output_result.segments_dir,
            alignment_report_path=output_result.alignment_report_path,
            background_sounds_path=output_result.background_sounds_path,
            total_segments=output_result.segment_count,
            needs_review_count=output_result.needs_review_count,
        )

    def _load_segments_with_source_ids_for_publish_resume(
        self,
        editor_segments_path: Path,
        translation_segments_path: Path,
    ) -> tuple[list[DubbingSegment], list[str]]:
        """Read the post-commit segment list for the γ publish-only path.

        Priority:

        1. ``editor/segments.json`` — canonical post-commit state. The
           commit layer (``editing_commit._apply_editing_to_baseline`` for
           overwrite, ``copy_service.prepare_copy_project_dir`` for
           copy_as_new) writes user text edits + voice_map overrides
           here. ``segment_id`` is stored as str per
           ``editor_baseline.normalise_segment_record``.
        2. ``translation/segments.json`` — fallback for legacy tasks that
           never went through editing. On copy_as_new, the target's
           translation/ holds the source's pre-edit text (just path-
           rewritten), so reading this file silently drops user edits.
           Only used when editor/segments.json is missing.

        editor/segments.json may carry keys outside the DubbingSegment
        dataclass (e.g. ``provider`` from ``_apply_voice_map``); filter
        to known fields before constructing the dataclass, else
        ``DubbingSegment(**seg)`` raises TypeError for unexpected kwargs.

        Editing can create transient split IDs such as ``"11_a"`` and
        ``"11_b"``. The publish pipeline still needs integer
        ``DubbingSegment.segment_id`` values for downstream output builders,
        but the reviewed wavs are stored under the source editor IDs. When
        any record has a non-int-castable ID, renumber the whole batch by
        display order and return the original source IDs as the wav lookup
        keys.
        """
        dubbing_fields = {f.name for f in _dc_fields(DubbingSegment)}

        raw_records: list[dict[str, object]] = []
        if editor_segments_path.is_file():
            payload = json.loads(editor_segments_path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                raw_records = [r for r in payload if isinstance(r, dict)]
            elif isinstance(payload, dict):
                inner = payload.get("segments")
                if isinstance(inner, list):
                    raw_records = [r for r in inner if isinstance(r, dict)]
        if not raw_records and translation_segments_path.is_file():
            payload = json.loads(translation_segments_path.read_text(encoding="utf-8"))
            inner = payload.get("segments") if isinstance(payload, dict) else payload
            if isinstance(inner, list):
                raw_records = [r for r in inner if isinstance(r, dict)]

        if not raw_records:
            raise ValueError(
                f"resume_from=alignment: no segments found in "
                f"{editor_segments_path} or {translation_segments_path}"
            )

        source_segment_ids: list[str] = []
        parsed_segment_ids: list[int | None] = []
        needs_sequential_ids = False
        for rec in raw_records:
            sid = rec.get("segment_id")
            if sid is None:
                raise ValueError(
                    f"segment lacks 'segment_id': {rec.get('cn_text', '')[:30]!r}"
                )
            source_segment_ids.append(str(sid))
            try:
                parsed_segment_ids.append(int(str(sid)))
            except (TypeError, ValueError):
                parsed_segment_ids.append(None)
                needs_sequential_ids = True

        segments: list[DubbingSegment] = []
        for index, rec in enumerate(raw_records, start=1):
            kwargs = {k: v for k, v in rec.items() if k in dubbing_fields}
            if needs_sequential_ids:
                kwargs["segment_id"] = index
            else:
                parsed_id = parsed_segment_ids[index - 1]
                if parsed_id is None:
                    raise ValueError(
                        f"segment_id must be int-castable, got "
                        f"{source_segment_ids[index - 1]!r}"
                    )
                kwargs["segment_id"] = parsed_id
            seg = DubbingSegment(**kwargs)
            _backfill_legacy_tts_input_cn_text(seg)
            segments.append(seg)
        return segments, source_segment_ids

    def _load_segments_for_publish_resume(
        self,
        editor_segments_path: Path,
        translation_segments_path: Path,
    ) -> list[DubbingSegment]:
        (
            segments,
            _source_segment_ids,
        ) = self._load_segments_with_source_ids_for_publish_resume(
            editor_segments_path=editor_segments_path,
            translation_segments_path=translation_segments_path,
        )
        return segments

    @staticmethod
    def _publish_resume_slot_duration_ms(segment: DubbingSegment) -> int:
        try:
            start_ms = int(getattr(segment, "start_ms", 0) or 0)
            end_ms = int(getattr(segment, "end_ms", 0) or 0)
        except (TypeError, ValueError):
            start_ms = 0
            end_ms = 0
        slot_duration_ms = max(0, end_ms - start_ms)
        if slot_duration_ms > 0:
            return slot_duration_ms
        try:
            return max(0, int(getattr(segment, "target_duration_ms", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _populate_publish_resume_audio_paths(
        self,
        *,
        segments: list[DubbingSegment],
        source_segment_ids: list[str],
        tts_segments_dir: Path,
    ) -> None:
        missing_sids: list[str] = []
        for index, segment in enumerate(segments):
            sid_str = str(segment.segment_id)
            source_sid_str = (
                source_segment_ids[index]
                if index < len(source_segment_ids)
                else sid_str
            )
            wav_candidates = [tts_segments_dir / f"{source_sid_str}.wav"]
            numeric_wav_path = tts_segments_dir / f"{sid_str}.wav"
            if numeric_wav_path not in wav_candidates:
                wav_candidates.append(numeric_wav_path)
            wav_path = next((p for p in wav_candidates if p.is_file()), None)
            if wav_path is None:
                missing_sids.append(source_sid_str)
                continue
            wav_str = str(wav_path.resolve(strict=False))
            raw_duration_ms = _ffprobe_duration_ms(wav_path)
            slot_duration_ms = self._publish_resume_slot_duration_ms(segment)
            segment.tts_audio_path = wav_str
            segment.aligned_audio_path = wav_str
            segment.actual_duration_ms = (
                slot_duration_ms if slot_duration_ms > 0 else raw_duration_ms
            )
            if slot_duration_ms > 0:
                segment.alignment_ratio = raw_duration_ms / slot_duration_ms
            if not segment.alignment_method:
                segment.alignment_method = "direct"
            segment.needs_review = False
        if missing_sids:
            shown = missing_sids[:10]
            ellipsis = "..." if len(missing_sids) > 10 else ""
            raise ValueError(
                f"resume_from=alignment: {len(missing_sids)} segment(s) "
                f"missing wavs in editor/tts_segments/: {shown}{ellipsis}. "
                "Commit must have placed every segment's wav on disk — "
                "this is a commit/copy_service bug."
            )

    def _normalize_editor_tts_segment_ids_for_publish_resume(
        self,
        *,
        tts_segments_dir: Path,
        segments: list[DubbingSegment],
    ) -> None:
        """Materialize reviewed wavs under the integer IDs used for publish.

        Split editor IDs are only an editing-time addressing detail. After a
        successful commit/publish pass, the next editing session should see a
        normal integer-ID baseline again. Stage every source wav first so a
        shifted target like ``12.wav`` cannot clobber the source for another
        segment before that source has been copied.
        """
        staged: list[tuple[Path, Path, DubbingSegment]] = []
        materialized: list[tuple[Path, DubbingSegment]] = []
        tts_segments_dir.mkdir(parents=True, exist_ok=True)
        try:
            for index, segment in enumerate(segments):
                source_text = segment.aligned_audio_path or segment.tts_audio_path
                if not source_text:
                    raise ValueError(
                        f"segment {segment.segment_id} has no reviewed wav path"
                    )
                source_path = Path(source_text)
                if not source_path.is_file():
                    raise ValueError(
                        f"segment {segment.segment_id} reviewed wav is missing: "
                        f"{source_path}"
                    )
                target_path = tts_segments_dir / f"{segment.segment_id}.wav"
                if source_path.resolve(strict=False) == target_path.resolve(strict=False):
                    materialized.append((target_path, segment))
                    continue
                tmp_path = tts_segments_dir / (
                    f".{target_path.name}.renumber-{os.getpid()}-{index}.tmp"
                )
                shutil.copy2(source_path, tmp_path)
                staged.append((tmp_path, target_path, segment))

            for tmp_path, target_path, segment in staged:
                os.replace(tmp_path, target_path)
                materialized.append((target_path, segment))

            for target_path, segment in materialized:
                slot_duration_ms = self._publish_resume_slot_duration_ms(segment)
                fit_result = _fit_audio_to_slot(
                    target_path,
                    slot_duration_ms=slot_duration_ms,
                )
                wav_str = str(target_path.resolve(strict=False))
                segment.tts_audio_path = wav_str
                segment.aligned_audio_path = wav_str
                if fit_result is not None:
                    segment.actual_duration_ms = fit_result.final_duration_ms
                    segment.dsp_speed_ratio_used = fit_result.speed_ratio_used
                    segment.dsp_silence_padded_ms = fit_result.silence_padded_ms
                    segment.dsp_truncated_ms = fit_result.truncated_ms
                    segment.dsp_initial_duration_ms = fit_result.initial_duration_ms
                    segment.dsp_trimmed_duration_ms = fit_result.trimmed_duration_ms
                    segment.dsp_stretched_duration_ms = fit_result.stretched_duration_ms
        finally:
            for tmp_path, _target_path, _segment in staged:
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass

    def _load_raw_service_mode_for_resume(self, config: ProcessConfig) -> str | None:
        """Codex 第二十一轮 P0: resume-publish-only path never traverses
        the main run() ``self._current_service_mode = ...`` assignment
        around line 1520, so the terminal smart_state helper needs an
        explicit lookup here.

        Order of precedence:
          1. ``config.job_record`` if pre-loaded (test paths typically
             pass this)
          2. ``JobStore.load_job(config.job_id).service_mode`` if job_id
             is present
          3. ``None`` (caller treats as non-smart no-op)

        Never raises — any load failure returns None so the helper
        skips emission rather than crashing the resume publish path.
        """
        jr = getattr(config, "job_record", None)
        if jr:
            if isinstance(jr, dict):
                value = jr.get("service_mode")
            else:
                value = getattr(jr, "service_mode", None)
            if isinstance(value, str) and value:
                return value
        if config.job_id:
            try:
                from services.jobs.store import JobStore

                store = JobStore(PROJECT_ROOT / "jobs")
                record = store.load_job(config.job_id)
                if record is not None:
                    value = getattr(record, "service_mode", None)
                    if isinstance(value, str) and value:
                        return value
            except Exception:
                # Best-effort: never block resume publish on JobStore
                # lookup glitches. None falls through to non-smart noop.
                return None
        return None

    def _run_alignment_and_publish_only(self, config: ProcessConfig) -> ProcessResult:
        """Resume pipeline at publish — no TTS, no alignment, no Gemini (γ).

        Called from ``run()`` when ``config.resume_from == STAGE_ALIGNMENT``.
        Triggered by commit copy_as_new / overwrite (plan D28, Phase 1 γ).

        γ contract: the user reviewed per-segment duration in the editing
        UI and decided whether to re-TTS each segment. Commit promoted the
        accepted drafts to ``editor/tts_segments/{sid}.wav``. This path
        trusts those wavs as-is and only does publish-side muxing —
        4 artifacts the user cares about:

        1. 配音音频 — dubbed audio stitched from per-segment wavs
        2. 配音视频 — original video + dubbed audio + ambient
        3. 预览图 — poster for the dubbed video
        4. 字幕 ×3 — zh.srt / en.srt / bilingual.srt

        NO Gemini call (rewrite), NO (re-)TTS, NO alignment DSP.

        Preconditions (fail-fast):
        - ``config.project_dir`` set + exists
        - ``translation/segments.json`` on disk (retained for backward
          compat and copy_service sanity check; γ prefers editor/)
        - ``audio/speech_for_asr.wav`` + ``audio/ambient.wav`` on disk
        - For every segment ``s``: ``editor/tts_segments/{s.segment_id}.wav``
          on disk. Missing wavs → raise ValueError (commit bug).
        """
        if not config.project_dir:
            raise ValueError(
                "resume_from=alignment requires ProcessConfig.project_dir"
            )
        final_project_dir = Path(config.project_dir).resolve(strict=False)
        if not final_project_dir.is_dir():
            raise ValueError(
                f"resume_from=alignment: project_dir does not exist: {final_project_dir}"
            )
        usage_meter = UsageMeter(final_project_dir, job_id=config.job_id)

        trans_path = (final_project_dir / "translation" / "segments.json").resolve(strict=False)
        if not trans_path.is_file():
            raise ValueError(
                f"resume_from=alignment: {trans_path} missing — commit step "
                "must have placed translation/segments.json on disk"
            )
        editor_segments_path = (
            final_project_dir / "editor" / "segments.json"
        ).resolve(strict=False)
        # Canonical demucs output name — ``services.audio.separator.speech_filename``.
        speech_path = (final_project_dir / "audio" / "speech_for_asr.wav").resolve(strict=False)
        ambient_path = (final_project_dir / "audio" / "ambient.wav").resolve(strict=False)
        source_audio_path = (final_project_dir / "audio" / "original.wav").resolve(strict=False)
        video_path = (final_project_dir / "video" / "original.mp4").resolve(strict=False)
        # P3e-3b 钱-关键耦合（CodeX/对抗性 P1）：smart 预览 teaser 音频若在盘上，
        # resume/publish 必须用**它的时长**（而非完整 original.wav）造 base silence。
        # 否则 Studio 编辑-提交一个预览任务会走本路径，用完整时长造满长 base silence
        # （EditorPackageWriter）+ 完整视频出片，-shortest 因满长静音不收口 = 完整
        # 任务（漏收分钟点，P3e-3c 跳分钟后尤甚）。preview_teaser.wav 存在 = 该任务是
        # 预览（仅 smart 预览 teaser 创建），优先复用之。视频仍用 original.mp4 —
        # 经 3min base silence + -shortest 收口成 3 分钟，不漏完整片。
        _preview_teaser_audio = (
            final_project_dir / "audio" / "preview_teaser.wav"
        ).resolve(strict=False)
        if _preview_teaser_audio.is_file():
            source_audio_path = _preview_teaser_audio
        for missing_rel, p in (
            ("audio/speech_for_asr.wav", speech_path),
            ("audio/ambient.wav", ambient_path),
        ):
            if not p.is_file():
                raise ValueError(
                    f"resume_from=alignment: {missing_rel} missing at {p}; "
                    "copy_service.hardlink_media_artifacts should have placed it"
                )

        # --- Load segments + populate wav paths (γ: no TTS / alignment) ---
        segments, source_segment_ids = self._load_segments_with_source_ids_for_publish_resume(
            editor_segments_path=editor_segments_path,
            translation_segments_path=trans_path,
        )
        tts_segments_dir = final_project_dir / "editor" / "tts_segments"
        self._populate_publish_resume_audio_paths(
            segments=segments,
            source_segment_ids=source_segment_ids,
            tts_segments_dir=tts_segments_dir,
        )

        # --- Rebuild publish context from disk ---
        download_result = self._load_download_result(
            final_project_dir,
            fallback_url=(config.source_ref or config.youtube_url or ""),
        )
        transcript_path = final_project_dir / "transcript" / "transcript.json"
        if transcript_path.is_file():
            transcript_result = self._load_transcript_result(transcript_path)
        else:
            transcript_result = TranscriptResult(
                lines=[], total_duration_ms=0, language="",
                raw_response_path="",
                structured_transcript_path=str(transcript_path),
            )
        translation_result = TranslationResult(
            segments=segments,
            total_segments=len(segments),
            output_path=str(trans_path.resolve(strict=False)),
        )
        separated_audio = AudioSeparationResult(
            source_audio_path=str(source_audio_path),
            speech_audio_path=str(speech_path),
            ambient_audio_path=str(ambient_path),
            reused_cache=True,
        )
        actual_total_duration_ms = _ffprobe_duration_ms(source_audio_path)
        source_type = config.source_type or "youtube_url"
        normalized_url = (config.youtube_url or "").strip()

        # s2 glossary cache for metering (best-effort).
        _review_glossary: dict[str, str] = {}
        s2_cache_path = final_project_dir / "transcript" / "s2_review_result.json"
        if s2_cache_path.is_file():
            try:
                _s2 = json.loads(s2_cache_path.read_text(encoding="utf-8"))
                _review_glossary = _s2.get("glossary", {}) or {}
            except Exception as _exc:
                print(
                    f"[RESUME] s2_review_result load failed (non-fatal): {_exc}",
                    flush=True,
                )

        state_manager = StateManager(str(final_project_dir / "project_state.json"))

        current_stage_name: str | None = None
        try:
            # --- Emit STAGE_ALIGNMENT: DONE immediately (γ does no alignment) ---
            # We still write the stage transition so the job record / stage
            # reporting stays consistent; payload makes it clear γ skipped
            # alignment — grep for execution_mode=resume_publish_only.
            current_stage_name = STAGE_ALIGNMENT
            state_manager.set_stage(
                current_stage_name, StageStatus.RUNNING,
                {"execution_mode": "resume_publish_only"},
            )
            needs_review_count = sum(1 for s in segments if s.needs_review)
            print(
                f"[RESUME/S5] \u8df3\u8fc7\u5bf9\u9f50\uff08\u03b3 \u8def\u5f84\uff09\uff1a"
                f"\u5171 {len(segments)} \u6bb5\uff0c\u9700\u8981\u4eba\u5de5\u68c0\u67e5 "
                f"{needs_review_count} \u6bb5"
            )
            state_manager.set_stage(
                current_stage_name, StageStatus.DONE,
                {
                    "execution_mode": "resume_publish_only",
                    "block_count": len(segments),
                    "needs_review_count": needs_review_count,
                    "cn_text_produced": any(bool(s.cn_text.strip()) for s in segments),
                    "artifacts": build_artifacts_payload(
                        kind="aligned_audio",
                        file_paths=[s.aligned_audio_path for s in segments],
                    ),
                },
            )

            # --- Publish: audio mux / video mux / poster / subtitles ---
            current_stage_name = STAGE_LEGACY_PROCESS_OUTPUT
            state_manager.set_stage(
                current_stage_name, StageStatus.RUNNING,
                {"execution_mode": "resume_publish_only"},
            )
            print(
                "[RESUME/S6] \u5408\u6210\u914d\u97f3\u97f3\u9891/\u914d\u97f3\u89c6\u9891/"
                "\u9884\u89c8\u56fe/\u5b57\u5e55..."
            )
            build_result = self._build_process_workflow_build_result(
                project_dir=final_project_dir,
                youtube_url=normalized_url,
                download_result=download_result,
                video_path=video_path,
                source_audio_path=source_audio_path,
                separated_audio=separated_audio,
                transcript_result=transcript_result,
                translation_result=translation_result,
                total_duration_ms=actual_total_duration_ms,
                segments=segments,
                stage_snapshot=state_manager.load().get("stages", {}),
                source_type=source_type,
            )
            output_bundle = self._dispatch_process_output_bundle(
                project_dir=final_project_dir,
                build_result=build_result,
            )
            assert output_bundle.editor_result is not None
            output_result = output_bundle.editor_result
            state_manager.set_stage(
                current_stage_name, StageStatus.DONE,
                self._build_legacy_process_output_stage_payload(output_bundle=output_bundle),
            )
            current_stage_name = None
            print(
                f"[RESUME/S6] \u5b8c\u6210\uff1a\u8f93\u51fa\u76ee\u5f55 "
                f"{final_project_dir / 'output'}"
            )

            self._normalize_editor_tts_segment_ids_for_publish_resume(
                tts_segments_dir=tts_segments_dir,
                segments=segments,
            )
            # Keep translation/segments.json + editor baseline in sync with
            # the published state. asdict() serialises every DubbingSegment
            # field including the wav paths we just populated.
            try:
                _dump = {
                    "segments": [_dc_asdict(s) for s in segments],
                    "total_segments": len(segments),
                    "output_path": str(trans_path),
                }
                trans_path.write_text(
                    json.dumps(_dump, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception as _exc:
                print(
                    f"[RESUME/S6] segments.json rewrite skipped: {_exc}",
                    flush=True,
                )

            try:
                from services.jobs.editor_baseline import (
                    write_editor_segments_from_translation,
                )
                write_editor_segments_from_translation(final_project_dir)
            except Exception as _exc:
                print(
                    f"[RESUME/S6] editor baseline skipped: {_exc}",
                    flush=True,
                )

            if config.job_id:
                _usage_summary = _write_usage_summary(usage_meter)
                _report_job_metering(
                    config.job_id, segments,
                    tts_billed_chars=None,
                    glossary=_review_glossary or None,
                    extra_metering=_usage_summary or None,
                )
        except Exception as exc:
            stage_label = _classify_failed_stage(exc)
            if current_stage_name is not None:
                state_manager.set_stage(
                    current_stage_name, StageStatus.FAILED,
                    {
                        "execution_mode": "resume_publish_only",
                        "error_type": exc.__class__.__name__,
                    },
                    error_message=str(exc),
                )
            _write_usage_summary(usage_meter)
            print(f"[{stage_label}] \u5931\u8d25\uff1a{exc}")
            raise

        # PR#3C-b3a: terminal smart_state marker for the resume-publish-
        # only happy-path return (covers commit copy_as_new + overwrite
        # publish stages). Same rationale as the main run() terminal \u2014
        # see _emit_smart_terminal_completion_marker docstring.
        # Codex \u7b2c\u4e8c\u5341\u4e00\u8f6e P0: ``self._current_service_mode`` is NOT
        # initialized on this code path (resume doesn't traverse main
        # run()'s assignment), so load raw service_mode explicitly via
        # JobStore lookup and pass it in. Helper is a no-op when None
        # is returned, keeping non-smart resume paths safe.
        _resume_service_mode = self._load_raw_service_mode_for_resume(config)
        self._emit_smart_terminal_completion_marker(
            service_mode=_resume_service_mode,
        )
        return ProcessResult(
            project_dir=str(final_project_dir.resolve(strict=False)),
            dubbed_audio_path=output_result.dubbed_audio_path,
            ambient_audio_path=output_result.ambient_audio_path,
            subtitles_path=output_result.subtitles_path,
            segments_dir=output_result.segments_dir,
            alignment_report_path=output_result.alignment_report_path,
            background_sounds_path=output_result.background_sounds_path,
            total_segments=output_result.segment_count,
            needs_review_count=output_result.needs_review_count,
        )

    def _emit_smart_terminal_completion_marker(
        self,
        *,
        service_mode: str | None,
    ) -> None:
        """Plan §4.3 mapping table + §6.0.5 + Codex 第二十/二十一轮.

        When a smart pipeline reaches the happy-path succeeded terminal
        return, emit a terminal ``smart_state`` marker so:

          - ``JobRecord.smart_state.status`` flips from any intermediate
            value (e.g. PR#3C-b2 left an unspecified status after voice
            review auto-approve; or ``downgraded_to_studio`` after user
            takeover manually approved Studio human-review) to the
            editable terminal ``"completed"``.
          - The runner-side marker handler merges it into JobRecord
            and the mirror chain (PR#1 F1) propagates to Gateway DB
            ``Job.smart_state`` before settle runs.
          - ``editing.py`` / ``jianying_draft_runner.py`` /
            ``jobs/api.py:1458`` gates see ``status="completed"`` and
            admit the job into Studio post-edit / Jianying draft (per
            ``EDITABLE_SERVICE_MODES`` + ``is_editable_smart_state``).
          - ``credits_service.settle_job_credit_ledger``'s smart
            dispatcher reads ``credits_policy="capture_full"`` and
            routes through ``smart_capture_full`` (smart-distinct
            reason_code for audit fidelity; amount currently mirrors
            the legacy succeeded branch but the routing carries the
            smart policy semantics).

        This function ONLY fires on the raw ``service_mode == "smart"``
        audit fact (NOT effective mode — failed paths handle their own
        terminal marker elsewhere). Failed-terminal paths
        (``fail_and_refunded`` for budget-exhausted users who chose
        ``fail_and_refund``) emit their own terminal markers via the
        ``smart_consent.on_budget_exhausted`` path in subsequent PRs
        — they are out of scope for PR#3C-b3a.

        Handoff-then-manual-success case: when a smart job hit handoff
        (smart_state.status="downgraded_to_studio"), user took over via
        Studio human-review, manually approved, and pipeline ran through
        to publish → terminal here. The handoff status gets overwritten
        with "completed" + credits_policy="capture_full". This is
        intentional and matches plan §6.3 fail-safe ladder row 4
        ("已开始 clone / TTS, 用户选择 degraded_delivery_with_report
        且交付当前最佳价本 → 按智能版固定价 100 credits/min capture").
        The "曾经 handoff 过" audit detail lives in smart_decisions.jsonl
        (PR#3C-b3e) — not on the terminal smart_state status.

        Codex 第二十一轮 P0: ``service_mode`` is now passed explicitly
        rather than read from ``self._current_service_mode``. The main
        run() path sets that attribute around line 1520, but the
        resume-publish-only path (commit copy_as_new / overwrite, entered
        at line 1465) never traverses that assignment. A fresh
        ``ProcessPipeline()`` invoked for commit publish would have
        thrown AttributeError on access (or silently inherited a stale
        value on a re-used instance). Making service_mode an explicit
        param forces callers to declare what they know — caller responsibility.
        """
        if service_mode != "smart":
            return
        # Lazy import — top-level import order in process.py is fragile
        # and smart pipeline integration intentionally minimises module-
        # load coupling.
        from services.smart.state import emit_smart_state_marker

        emit_smart_state_marker(
            {
                "status": "completed",
                "credits_policy": "capture_full",
            }
        )

    def _build_paused_result(self, *, project_dir: Path, stage: str, message: str) -> ProcessResult:
        return ProcessResult(
            project_dir=str(project_dir.resolve(strict=False)),
            dubbed_audio_path="",
            ambient_audio_path="",
            subtitles_path="",
            segments_dir="",
            alignment_report_path="",
            background_sounds_path="",
            total_segments=0,
            needs_review_count=0,
            status="waiting_for_review",
            paused_review_stage=stage,
            paused_review_message=message,
        )

    def _build_web_review_marker(self, *, stage: str, project_dir: Path, message: str) -> str:
        marker_payload = {
            "stage": stage,
            "tab": REVIEW_STAGE_TAB_MAP.get(stage),
            "project_dir": str(project_dir.resolve(strict=False)),
            "message": message,
        }
        return f"[WEB_REVIEW] {json.dumps(marker_payload, ensure_ascii=False)}"

    def _get_approved_review_payload(
        self,
        review_state_manager: ReviewStateManager,
        stage_name: str,
    ) -> dict[str, object] | None:
        stage_payload = review_state_manager.get_stage(stage_name)
        if not stage_payload or stage_payload.get("status") != REVIEW_STATUS_APPROVED:
            return None
        payload = stage_payload.get("payload")
        return payload if isinstance(payload, dict) else None

    def _should_reuse_approved_translation_review(
        self,
        *,
        explicit_project_dir: Path | None,
        wait_for_review: bool,
    ) -> bool:
        # When running via Web UI (wait_for_review=True) without an explicit
        # project dir, always require fresh human review.
        if wait_for_review and explicit_project_dir is None:
            return False
        return True

    def _build_speaker_review_payload(
        self,
        *,
        transcript_result: TranscriptResult,
        speaker_name_a: str,
        speaker_name_b: str,
        effective_speakers: int,
    ) -> dict[str, object]:
        detected_speaker_ids = self._detect_speaker_ids(transcript_result.lines)
        speaker_names: dict[str, str] = {}
        for speaker_id in detected_speaker_ids:
            if speaker_id == "speaker_a":
                speaker_names[speaker_id] = speaker_name_a or _default_speaker_display_name(speaker_id)
            elif speaker_id == "speaker_b":
                speaker_names[speaker_id] = speaker_name_b or _default_speaker_display_name(speaker_id)
            else:
                speaker_names[speaker_id] = _default_speaker_display_name(speaker_id)
        speaker_options = [
            {"speaker_id": speaker_id, "display_name": display_name}
            for speaker_id, display_name in speaker_names.items()
            if display_name
        ]
        segment_speakers = {
            str(line.index): str(line.speaker_id).strip() or "speaker_a"
            for line in transcript_result.lines
        }
        return {
            "speaker_names": speaker_names,
            "speaker_options": speaker_options,
            "segment_speakers": segment_speakers,
            "segment_count": len(transcript_result.lines),
        }

    def _apply_speaker_review_payload(
        self,
        *,
        transcript_result: TranscriptResult,
        payload: dict[str, object],
    ) -> TranscriptResult:
        reviewed_speaker_map = payload.get("segment_speakers", {})
        normalized_speaker_map = (
            {
                str(segment_id): str(speaker_id).strip()
                for segment_id, speaker_id in reviewed_speaker_map.items()
                if _is_valid_speaker_id(str(speaker_id).strip())
            }
            if isinstance(reviewed_speaker_map, dict)
            else {}
        )
        updated_lines = [
            TranscriptLine(
                index=line.index,
                start_ms=line.start_ms,
                end_ms=line.end_ms,
                speaker_id=normalized_speaker_map.get(str(line.index), line.speaker_id),
                speaker_label=line.speaker_label,
                source_text=line.source_text,
            )
            for line in transcript_result.lines
        ]
        return TranscriptResult(
            lines=updated_lines,
            total_duration_ms=transcript_result.total_duration_ms,
            language=transcript_result.language,
            raw_response_path=transcript_result.raw_response_path,
            structured_transcript_path=transcript_result.structured_transcript_path,
        )

    def _resolve_speaker_names_from_review_payload(
        self,
        *,
        payload: dict[str, object],
        fallback_speaker_a: str,
        fallback_speaker_b: str,
    ) -> tuple[str, str]:
        speaker_names = payload.get("speaker_names", {})
        if not isinstance(speaker_names, dict):
            return fallback_speaker_a, fallback_speaker_b
        speaker_name_a = _normalize_optional_text(speaker_names.get("speaker_a")) or fallback_speaker_a
        speaker_name_b = _normalize_optional_text(speaker_names.get("speaker_b")) or fallback_speaker_b
        return speaker_name_a, speaker_name_b

    def _build_translation_config_review_payload(
        self,
        transcript_result: TranscriptResult,
        translator: GeminiTranslator,
    ) -> dict[str, object]:
        available_models = []
        llm_router = getattr(translator, "llm_router", None)
        if llm_router is not None:
            model_configs = getattr(llm_router, "model_configs", {})
            if isinstance(model_configs, dict):
                for alias, config in model_configs.items():
                    provider = config.get("provider", "") if isinstance(config, dict) else ""
                    model_name = config.get("model_name", alias) if isinstance(config, dict) else alias
                    available_models.append({
                        "alias": alias,
                        "provider": provider,
                        "model_name": model_name,
                    })

        current_model = getattr(translator, "model_name", None) or "unknown"
        current_prompt = getattr(translator, "_effective_translation_prompt_template", None)
        if current_prompt is None:
            from services.gemini.translator import get_effective_translation_prompt_template
            current_prompt = get_effective_translation_prompt_template()

        return {
            "segment_count": len(transcript_result.lines),
            "available_models": available_models,
            "current_model": current_model,
            "current_prompt_template": current_prompt,
        }

    def _validate_cloned_voices(self, speaker_voices: dict[str, str]) -> list[str]:
        """Validate cloned voice IDs (vt_ prefix) via quick TTS test. Returns expired IDs."""
        expired: list[str] = []
        for spk_id, voice_id in speaker_voices.items():
            if not voice_id.startswith("vt_"):
                continue
            try:
                from services.voice_asset import VoiceAssetVerifier
                verifier = VoiceAssetVerifier.from_env()
                verifier.verify_voice(
                    speaker_id="validate",
                    voice_id=voice_id,
                    sample_text="测试。",
                )
            except Exception as exc:
                err_msg = str(exc).lower()
                if "2054" in err_msg or "voice id not exist" in err_msg or "voice_id not exist" in err_msg:
                    print(f"[S5] 音色 {voice_id} ({spk_id}) 已失效", flush=True)
                    expired.append(voice_id)
                else:
                    # Non-expiry error (network, rate limit, etc.) — don't block
                    print(f"[S5] 音色 {voice_id} ({spk_id}) 验证异常（非过期）: {str(exc)[:100]}", flush=True)
        return expired

    def _notify_voice_expired(self, job_id: str | None, voice_id: str) -> None:
        """Best-effort notify gateway to mark voice as expired in user's library.

        Headers come from the shared ``_internal_request_headers()`` helper
        (P0-2a audit follow-up, 2026-05-07) so this stays in sync if the
        protocol ever evolves (trace-id, signed timestamps, etc.).
        """
        if not job_id:
            return
        try:
            from urllib import request as urllib_request
            import json as _json
            req = urllib_request.Request(
                # P0-2b (audit 2026-05-07): /internal/* → /api/internal/* so
                # Caddyfile's @internal_block actually shields these endpoints.
                "http://127.0.0.1:8880/api/internal/user-voices/expire",
                data=_json.dumps({"job_id": job_id, "voice_id": voice_id}).encode(),
                headers=_internal_request_headers(),
                method="POST",
            )
            urllib_request.urlopen(req, timeout=5)
        except Exception:
            pass

    @staticmethod
    def _is_generic_speaker_name(name: str, speaker_id: str) -> bool:
        normalized = str(name or "").strip().lower()
        normalized_sid = str(speaker_id or "").strip().lower()
        if not normalized:
            return True
        generic_names = {
            normalized_sid,
            normalized_sid.replace("_", " "),
            _default_speaker_display_name(normalized_sid).lower(),
        }
        if normalized in generic_names:
            return True
        if re.fullmatch(r"speaker\s+[a-z0-9]+", normalized):
            return True
        if normalized.startswith("unknown speaker") or normalized.startswith("未知说话人"):
            return True
        return False

    @staticmethod
    def _speaker_role_label(role: str) -> str:
        if role == "non_speech":
            return "背景音/非对白"
        if role == "incidental":
            return "短互动/低占比"
        if role == "fragmented":
            return "低占比分散"
        if role == "primary":
            return "主说话人"
        return ""

    @staticmethod
    def _speaker_review_hint(role: str) -> str:
        if role == "non_speech":
            return "疑似背景音乐、人群欢呼、掌声或其他非对白；不建议克隆音色，建议确认是否需要配音。"
        if role == "incidental":
            return "低占比短互动说话人；建议使用通用音色，通常不建议克隆。"
        if role == "fragmented":
            return "低占比分散说话人；建议抽查是否为真实多人或误分裂。"
        return ""

    @staticmethod
    def _review_flag_truthy(value: object) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        return text in {"1", "true", "yes", "y", "on", "是", "对", "非对白", "non_speech"}

    @staticmethod
    def _speaker_metadata_is_non_speech(profile: dict[str, object] | None) -> bool:
        if not profile:
            return False
        if ProcessPipeline._review_flag_truthy(profile.get("is_non_speech")):
            return True
        profile_text = " ".join(
            str(profile.get(key, "") or "")
            for key in (
                "name",
                "role",
                "style",
                "voice_description",
                "non_speech_reason",
            )
        ).lower()
        return any(marker in profile_text for marker in SPEAKER_STRUCTURE_NON_SPEECH_MARKERS)

    def _build_speaker_structure_profiles(
        self,
        lines: list[TranscriptLine],
        speaker_styles: dict[str, dict[str, object]] | None = None,
    ) -> dict[str, dict[str, object]]:
        speaker_styles = speaker_styles or {}
        per_speaker: dict[str, dict[str, int]] = {}
        total_duration_ms = 0
        for line in lines:
            speaker_id = str(line.speaker_id or "speaker_a").strip() or "speaker_a"
            duration_ms = max(0, int(line.end_ms) - int(line.start_ms))
            if duration_ms <= 0:
                continue
            total_duration_ms += duration_ms
            profile = per_speaker.setdefault(
                speaker_id,
                {
                    "duration_ms": 0,
                    "segment_count": 0,
                    "short_segment_count": 0,
                    "max_segment_ms": 0,
                },
            )
            profile["duration_ms"] += duration_ms
            profile["segment_count"] += 1
            profile["max_segment_ms"] = max(profile["max_segment_ms"], duration_ms)
            if duration_ms <= SPEAKER_STRUCTURE_SHORT_SEGMENT_MS:
                profile["short_segment_count"] += 1

        if not per_speaker or total_duration_ms <= 0:
            return {}

        sorted_speakers = sorted(
            per_speaker.items(),
            key=lambda item: item[1]["duration_ms"],
            reverse=True,
        )
        top_speaker_id = sorted_speakers[0][0]
        result: dict[str, dict[str, object]] = {}
        multi_speaker = len(per_speaker) > 1

        for speaker_id, raw_profile in sorted_speakers:
            duration_ms = raw_profile["duration_ms"]
            segment_count = raw_profile["segment_count"]
            short_segment_count = raw_profile["short_segment_count"]
            max_segment_ms = raw_profile["max_segment_ms"]
            duration_share = duration_ms / total_duration_ms
            short_segment_rate = (
                short_segment_count / segment_count if segment_count > 0 else 0.0
            )

            role = "primary"
            reason = "single_speaker"
            if self._speaker_metadata_is_non_speech(speaker_styles.get(speaker_id)):
                role = "non_speech"
                reason = "review_profile_non_speech"
            elif multi_speaker:
                incidental = (
                    speaker_id != top_speaker_id
                    and duration_share <= SPEAKER_STRUCTURE_INCIDENTAL_MAX_SHARE
                    and duration_ms <= SPEAKER_STRUCTURE_INCIDENTAL_MAX_DURATION_MS
                    and max_segment_ms <= SPEAKER_STRUCTURE_INCIDENTAL_MAX_SINGLE_SEGMENT_MS
                    and (
                        segment_count <= SPEAKER_STRUCTURE_INCIDENTAL_MAX_SEGMENTS
                        or short_segment_rate >= SPEAKER_STRUCTURE_INCIDENTAL_MIN_SHORT_RATE
                    )
                )
                fragmented = (
                    speaker_id != top_speaker_id
                    and duration_share <= SPEAKER_STRUCTURE_FRAGMENTED_MAX_SHARE
                    and segment_count >= SPEAKER_STRUCTURE_FRAGMENTED_MIN_SEGMENTS
                    and short_segment_rate >= SPEAKER_STRUCTURE_FRAGMENTED_MIN_SHORT_RATE
                )
                if incidental:
                    role = "incidental"
                    reason = "low_share_short_interactions"
                elif fragmented:
                    role = "fragmented"
                    reason = "low_share_fragmented"
                elif speaker_id == top_speaker_id:
                    role = "primary"
                    reason = "top_duration_speaker"
                elif duration_share >= 0.25:
                    role = "primary"
                    reason = "balanced_main_speaker"
                else:
                    role = "fragmented"
                    reason = "low_share_secondary"

            result[speaker_id] = {
                "speaker_role": role,
                "speaker_role_label": self._speaker_role_label(role),
                "speaker_duration_ms": duration_ms,
                "speaker_duration_share": round(duration_share, 4),
                "speaker_segment_count": segment_count,
                "speaker_short_segment_count": short_segment_count,
                "speaker_short_segment_rate": round(short_segment_rate, 4),
                "speaker_structure_reason": reason,
                "speaker_review_hint": self._speaker_review_hint(role),
            }
        return result

    def _apply_speaker_structure_profiles_to_segments(
        self,
        segments: list[DubbingSegment],
        speaker_structure_profiles: dict[str, dict[str, object]] | None,
    ) -> None:
        if not speaker_structure_profiles:
            return
        for segment in segments:
            profile = speaker_structure_profiles.get(segment.speaker_id)
            if not profile:
                continue
            segment.speaker_role = str(profile.get("speaker_role", "") or "")
            segment.speaker_role_label = str(profile.get("speaker_role_label", "") or "")
            segment.speaker_duration_ms = int(profile.get("speaker_duration_ms", 0) or 0)
            segment.speaker_duration_share = float(
                profile.get("speaker_duration_share", 0.0) or 0.0
            )
            segment.speaker_segment_count = int(profile.get("speaker_segment_count", 0) or 0)
            segment.speaker_short_segment_count = int(
                profile.get("speaker_short_segment_count", 0) or 0
            )
            segment.speaker_short_segment_rate = float(
                profile.get("speaker_short_segment_rate", 0.0) or 0.0
            )
            segment.speaker_structure_reason = str(
                profile.get("speaker_structure_reason", "") or ""
            )
            segment.speaker_review_hint = str(profile.get("speaker_review_hint", "") or "")

    def _log_speaker_structure_profiles(
        self,
        speaker_structure_profiles: dict[str, dict[str, object]],
    ) -> None:
        if not speaker_structure_profiles:
            return
        summary = []
        for speaker_id in sorted(speaker_structure_profiles):
            profile = speaker_structure_profiles[speaker_id]
            share = float(profile.get("speaker_duration_share", 0.0) or 0.0)
            role = str(profile.get("speaker_role", "") or "unknown")
            segments = int(profile.get("speaker_segment_count", 0) or 0)
            summary.append(f"{speaker_id}:{role}:{share:.1%}/{segments}段")
        print(f"[S2-P2] speaker structure: {', '.join(summary)}", flush=True)

    def _build_voice_selection_review_payload(
        self,
        *,
        transcript_result: TranscriptResult,
        translation_result: TranslationResult | None = None,
        tts_provider: str,
        service_mode: str,
        source_audio_path: str,
        effective_speakers: int,
        speaker_names: dict[str, str],
        speaker_styles: dict[str, dict[str, str]] | None = None,
        probe_segments: list["DubbingSegment"] | None = None,
        speaker_structure_profiles: dict[str, dict[str, object]] | None = None,
    ) -> dict[str, object]:
        """Build pending payload for voice_selection_review stage."""
        # Target-language-aware voice pool (PR-W de-Chinese). For the GA default
        # (zh-CN target, CJK) every provider filter below stays byte-identical;
        # a non-CJK target (e.g. en) gets target-language voices instead of being
        # hard-restricted to Chinese (finding A: a foreign-target review page /
        # Smart auto-match must not see only Chinese voices).
        _profile = getattr(self, "_language_profile", DEFAULT_LANGUAGE_PAIR_PROFILE)
        _target_language = _profile.target_language
        # Derive descriptors from the PROFILE (single source of truth), not the
        # per-run instance fields — so a direct helper call (e.g. a future Studio
        # enter-edit refresh, or a unit test) that bypasses run() still gets the
        # correct script for the default pair. Using the instance field's None
        # fallback here would flip the default zh-CN target to "not CJK" and
        # return empty Chinese pools (regression caught by both reviewers).
        _target_desc = (
            get_language_descriptor(_target_language)
            or get_language_descriptor(DEFAULT_TARGET_LANGUAGE)
        )
        _target_is_cjk = _target_desc is not None and _target_desc.script_family == SCRIPT_CJK
        _src_desc = (
            get_language_descriptor(_profile.source_language)
            or get_language_descriptor(DEFAULT_SOURCE_LANGUAGE)
        )
        _src_script = _src_desc.script_family if _src_desc is not None else SCRIPT_LATIN
        speaker_structure_profiles = (
            speaker_structure_profiles
            or self._build_speaker_structure_profiles(
                transcript_result.lines,
                speaker_styles=speaker_styles,
            )
        )
        # Build probe translation lookup: speaker_id -> list of probe texts
        _probe_texts_by_speaker: dict[str, list[dict[str, str]]] = {}
        if probe_segments:
            for seg in probe_segments:
                _probe_texts_by_speaker.setdefault(seg.speaker_id, []).append({
                    "segment_id": seg.segment_id,
                    "source_text": (seg.source_text or "")[:200],
                    "cn_text": seg.cn_text or "",
                })

        # Collect per-speaker segment info from transcript
        speaker_segments: dict[str, list[dict[str, object]]] = {}
        for line in transcript_result.lines:
            sid = line.speaker_id
            speaker_segments.setdefault(sid, []).append({
                "segment_id": line.index,
                "start_ms": line.start_ms,
                "end_ms": line.end_ms,
                "duration_s": round((line.end_ms - line.start_ms) / 1000, 1),
                "source_text": (line.source_text or "")[:200],
            })

        # ---------------------------------------------------------------
        # Build available_voices for ALL three providers (three-engine)
        # ---------------------------------------------------------------
        _PROVIDER_LABELS = {
            "minimax": "MiniMax Speech 2.8",
            "cosyvoice": "CosyVoice（阿里百炼）",
            "volcengine": "豆包 2.0",
        }

        def _build_provider_voices(prov: str) -> tuple[list[dict[str, object]], dict[str, str]]:
            """Build available_voices list + display_name map for a single provider."""
            voices: list[dict[str, object]] = []
            display_map: dict[str, str] = {}

            def _voice_dict(v: dict, vid: str, lbl: str) -> dict[str, object]:
                # Carry chars_per_second + speed_calibrated_at so the frontend
                # dropdown can show "X.X 字/秒(慢/中/快)" badges (Phase 1 + Task 2).
                return {
                    "voice_id": vid,
                    "label": lbl,
                    "gender": str(v.get("gender", "")),
                    "provider": prov,
                    "chars_per_second": v.get("chars_per_second"),
                    "speed_calibrated_at": v.get("speed_calibrated_at"),
                }

            if prov == "volcengine":
                # VolcEngine non-Chinese-target voices need the
                # compatible_target_languages + matchable migration (PR-E). Until
                # then a non-CJK target gets no VolcEngine candidates rather than
                # Chinese ones (de-Chinese: no contamination, no broken voice).
                if not _target_is_cjk:
                    return voices, display_map
                from services.tts.volcengine_voice_catalog import get_voices_for_resource, RESOURCE_ID_1_0, RESOURCE_ID_2_0
                rid = RESOURCE_ID_2_0 if service_mode == "studio" else RESOURCE_ID_1_0
                pool = get_voices_for_resource(rid)
                zh_pfx = ("ICL_zh_", "zh_") if rid == RESOURCE_ID_1_0 else ("zh_", "saturn_zh_")
                for v in pool:
                    vid = str(v.get("voice_id", ""))
                    if not vid.startswith(zh_pfx):
                        continue
                    lbl = str(v.get("display_name", vid))
                    display_map[vid] = lbl
                    voices.append(_voice_dict(v, vid, lbl))

            elif prov == "cosyvoice":
                # CosyVoice is a Chinese-only pool; it cannot read a non-CJK
                # target. Offer no candidates for such targets (PR-E decides if
                # any cross-lingual CosyVoice voice is ever eligible).
                if not _target_is_cjk:
                    return voices, display_map
                from services.tts.cosyvoice_endpoint_config import get_runtime_endpoint_mode, is_voice_available
                from services.tts.cosyvoice_voice_catalog import list_matchable_cosyvoice_voices
                ep_mode = get_runtime_endpoint_mode()
                for v in list_matchable_cosyvoice_voices():
                    vid = str(v.get("voice_id", ""))
                    if not is_voice_available(vid, ep_mode):
                        continue
                    lbl = str(v.get("display_name", v.get("name", vid)))
                    display_map[vid] = lbl
                    voices.append(_voice_dict(v, vid, lbl))

            elif prov == "minimax":
                from services.tts.minimax_voice_selector import _load_minimax_pool
                for v in _load_minimax_pool():
                    if not _minimax_language_matches_target(
                        str(v.get("language", "")), _target_language
                    ):
                        continue
                    vid = str(v.get("voice_id", ""))
                    lbl = str(v.get("display_name", v.get("name", vid)))
                    display_map[vid] = lbl
                    voices.append(_voice_dict(v, vid, lbl))

            return voices, display_map

        # Build all three providers
        # ``supports_clone`` 决定前端是否对该 provider 渲染"克隆音色"按钮。
        # MiniMax: 一直支持。CosyVoice: Phase 4.1 起通过 mainland worker 支持，
        # 由 ``AVT_MAINLAND_VOICE_WORKER_ENABLED`` 控制（env 字面读取集中在
        # ``services.mainland_worker.client_factory`` 内，F.6 守卫强制）。
        # VolcEngine: 无 user-clone 实现。
        from services.mainland_worker.client_factory import is_worker_enabled_in_env
        _cosyvoice_clone_enabled = is_worker_enabled_in_env()
        all_providers: dict[str, dict[str, object]] = {}
        all_display_maps: dict[str, dict[str, str]] = {}
        for prov in ("minimax", "cosyvoice", "volcengine"):
            try:
                voices, dmap = _build_provider_voices(prov)
            except Exception:
                voices, dmap = [], {}
            all_display_maps[prov] = dmap
            if prov == "minimax":
                supports_clone = True
            elif prov == "cosyvoice":
                supports_clone = _cosyvoice_clone_enabled
            else:
                supports_clone = False
            all_providers[prov] = {
                "label": _PROVIDER_LABELS.get(prov, prov),
                "available_voices": voices,
                "supports_clone": supports_clone,
            }

        # Default provider voices (backward compat)
        default_voices = all_providers.get(tts_provider, {}).get("available_voices", [])
        default_display_map = all_display_maps.get(tts_provider, {})

        # ---------------------------------------------------------------
        # Auto-match helper — works for any provider
        # ---------------------------------------------------------------
        def _auto_match_for_provider(
            prov: str, gender: str, age_group: str, persona: str, energy: str,
            target_chars_per_second: float | None = None,
        ) -> dict[str, object] | None:
            # Suppress auto-match when this provider contributes no voices for the
            # job's target language (e.g. VolcEngine/CosyVoice for a non-CJK
            # target). Otherwise the resolver could return a voice that is not in
            # the (de-Chinese-filtered) available_voices, and the frontend would
            # preselect a voice the user can't see / that mismatches the target.
            if not all_providers.get(prov, {}).get("available_voices"):
                return None
            try:
                from services.tts.voice_match_resolver import resolve_voice_match
                from services.tts.voice_match_types import VoiceMatchRequest
                # VolcEngine needs resource_id
                rid = None
                if prov == "volcengine":
                    from services.tts.volcengine_tts_provider import RESOURCE_ID_1_0, RESOURCE_ID_2_0
                    rid = RESOURCE_ID_2_0 if service_mode == "studio" else RESOURCE_ID_1_0
                result = resolve_voice_match(VoiceMatchRequest(
                    tts_provider=prov,
                    resource_id=rid,
                    mode="auto",
                    gender=gender,
                    age_group=age_group,
                    persona_style=persona,
                    energy_level=energy,
                    target_chars_per_second=target_chars_per_second,
                    target_language=_target_language,
                ))
                dmap = all_display_maps.get(prov, {})
                matched_name = dmap.get(result.voice_id, result.voice_id)
                # Task 2 UX: surface top backups so the dropdown can pin
                # "smart recommendations" (top + backups) at the top.
                backups: list[dict[str, str]] = []
                for backup_vid in (result.backup_voices or [])[:5]:
                    backups.append({
                        "voice_id": backup_vid,
                        "label": dmap.get(backup_vid, backup_vid),
                    })
                return {
                    "voice_id": result.voice_id,
                    "label": matched_name,
                    "match_confidence": result.match_confidence,
                    "backup_voices": backups,
                }
            except Exception:
                return None

        # --- Task 2: per-speaker English words/sec for speed-aware voice matching ---
        # Aggregate across the entire transcript so each speaker gets a single,
        # robust estimate.  target_chars_per_second = words_per_second × 1.8
        # (empirical EN→CN word-to-hanzi ratio).  Speakers with no valid data
        # (0 words or 0 duration) fall through with target=None, disabling the
        # speed dimension in the reranker (graceful degradation).
        _speaker_word_totals: dict[str, int] = {}
        _speaker_dur_ms_totals: dict[str, int] = {}
        for _line in transcript_result.lines:
            _w = self._count_source_words(_line.source_text or "", _src_script)
            _d = max(0, int(_line.end_ms - _line.start_ms))
            if _w > 0 and _d > 0:
                _speaker_word_totals[_line.speaker_id] = (
                    _speaker_word_totals.get(_line.speaker_id, 0) + _w
                )
                _speaker_dur_ms_totals[_line.speaker_id] = (
                    _speaker_dur_ms_totals.get(_line.speaker_id, 0) + _d
                )
        speaker_target_cps: dict[str, float] = {}
        # The voice-catalog cps is calibrated in Chinese hanzi/sec, so the speed
        # dimension is only comparable when the TARGET is CJK. For a non-CJK
        # target (e.g. en) leave cps empty → reranker speed dimension disabled
        # (plan §Phase 4.1: zh->en relies on DSP/rewrite for duration matching,
        # not speed scoring). Default pair (zh-CN target, ratio 1.8) is
        # byte-identical to the legacy ``round(_wps * 1.8, 2)``.
        if _target_is_cjk:
            for _sid, _words in _speaker_word_totals.items():
                _dur_s = _speaker_dur_ms_totals.get(_sid, 0) / 1000.0
                if _dur_s > 0:
                    _wps = _words / _dur_s
                    speaker_target_cps[_sid] = round(_wps * _profile.natural_length_ratio, 2)

        # Get speaker profiles: prefer explicit speaker_styles, fallback to segment attributes
        speaker_profiles: dict[str, dict[str, str]] = {}
        if speaker_styles:
            for sid, style in speaker_styles.items():
                speaker_profiles[sid] = {
                    "gender": style.get("gender", ""),
                    "age_group": style.get("age_group", ""),
                    "persona_style": style.get("persona_style", ""),
                    "energy_level": style.get("energy_level", ""),
                }
        if not speaker_profiles and translation_result is not None:
            # Fallback: read from segments (already injected by _apply_review_speaker_styles_to_segments)
            for seg in translation_result.segments:
                if seg.speaker_id not in speaker_profiles:
                    g = getattr(seg, "gender", "") or ""
                    if g:
                        speaker_profiles[seg.speaker_id] = {
                            "gender": g,
                            "age_group": getattr(seg, "age_group", "") or "",
                            "persona_style": getattr(seg, "persona_style", "") or "",
                            "energy_level": getattr(seg, "energy_level", "") or "",
                        }

        speakers_payload: list[dict[str, object]] = []
        for sid in sorted(speaker_segments.keys()):
            segs = speaker_segments[sid]
            total_dur = sum(float(s.get("duration_s", 0)) for s in segs)
            segs_sorted = sorted(segs, key=lambda s: float(s.get("duration_s", 0)), reverse=True)
            structure_profile = speaker_structure_profiles.get(sid, {})
            speaker_role = str(structure_profile.get("speaker_role", "") or "")
            speaker_name = speaker_names.get(sid, sid)
            if speaker_role == "non_speech" and self._is_generic_speaker_name(speaker_name, sid):
                speaker_name = "背景音/非对白"
            if speaker_role == "incidental" and self._is_generic_speaker_name(speaker_name, sid):
                speaker_name = "短互动/观众"
            can_clone = total_dur >= 10 and speaker_role not in {"incidental", "non_speech"}

            # Auto-match: default provider (backward compat)
            profile = speaker_profiles.get(sid, {})
            g = profile.get("gender", "")
            ag = profile.get("age_group", "")
            ps = profile.get("persona_style", "")
            el = profile.get("energy_level", "")
            target_cps = speaker_target_cps.get(sid)
            auto_matched = _auto_match_for_provider(
                tts_provider, g, ag, ps, el, target_chars_per_second=target_cps,
            )

            # Auto-match: all three providers
            auto_matched_by_provider: dict[str, object] = {}
            for prov in ("minimax", "cosyvoice", "volcengine"):
                auto_matched_by_provider[prov] = _auto_match_for_provider(
                    prov, g, ag, ps, el, target_chars_per_second=target_cps,
                )

            speakers_payload.append({
                "speaker_id": sid,
                "speaker_name": speaker_name,
                "segment_count": len(segs),
                "total_duration_s": round(total_dur, 1),
                "speaker_role": speaker_role,
                "speaker_role_label": str(structure_profile.get("speaker_role_label", "") or ""),
                "speaker_duration_ms": int(structure_profile.get("speaker_duration_ms", 0) or 0),
                "speaker_duration_share": float(
                    structure_profile.get("speaker_duration_share", 0.0) or 0.0
                ),
                "speaker_short_segment_count": int(
                    structure_profile.get("speaker_short_segment_count", 0) or 0
                ),
                "speaker_short_segment_rate": float(
                    structure_profile.get("speaker_short_segment_rate", 0.0) or 0.0
                ),
                "speaker_structure_reason": str(
                    structure_profile.get("speaker_structure_reason", "") or ""
                ),
                "speaker_review_hint": str(
                    structure_profile.get("speaker_review_hint", "") or ""
                ),
                "auto_matched_voice": auto_matched,
                "auto_matched_by_provider": auto_matched_by_provider,
                "can_clone": can_clone,
                "segments": segs_sorted,
                "probe_texts": _probe_texts_by_speaker.get(sid, []),
                # Phase 4 UX: target cps for this speaker, derived from
                # source_english_words_per_second × 1.8. The frontend uses
                # this to warn users when their chosen voice's cps deviates
                # >30% from the target (likely to cause heavy DSP stretching).
                "target_chars_per_second": speaker_target_cps.get(sid),
            })

        return {
            "message": "请为每位说话人选择或克隆配音音色",
            "tts_provider": tts_provider,
            "speakers": speakers_payload,
            "available_voices": default_voices,
            "all_providers": all_providers,
            "clone_cost_credits": self._get_clone_cost_credits(),
        }

    @staticmethod
    def _get_clone_cost_credits() -> int:
        """Read clone cost from pricing runtime snapshot (shared config file).

        The pipeline (app container) cannot import gateway modules directly,
        so we read the same JSON file that the gateway writes.
        """
        try:
            import json as _json
            from pathlib import Path as _Path
            import os as _os
            runtime_file = _Path(
                _os.environ.get("AIVIDEOTRANS_CONFIG_DIR", "/opt/aivideotrans/config")
            ) / "pricing_runtime.json"
            if runtime_file.exists():
                data = _json.loads(runtime_file.read_text(encoding="utf-8"))
                # plan 2026-06-14 §4.2：默认 fallback 500→600（生产恒读 runtime
                # 实际值；此默认仅 missing-key/缺文件路径用，与新 canonical 一致）。
                return data.get("credits", {}).get("voice_clone_cost_credits", 600)
        except Exception:
            pass
        return 600

    def _build_translation_review_payload(
        self,
        translation_result: TranslationResult,
        speaker_names: dict[str, str] | None = None,
    ) -> dict[str, object]:
        # Use reviewer names to override placeholder display_name in payload
        resolved_names = speaker_names or {}
        return {
            "segments": {
                str(segment.segment_id): {
                    "segment_id": segment.segment_id,
                    "speaker_id": segment.speaker_id,
                    "display_name": resolved_names.get(segment.speaker_id, "") or segment.display_name,
                    "source_text": segment.source_text,
                    "cn_text": segment.cn_text,
                    "target_duration_ms": segment.target_duration_ms,
                    "rewrite_count": segment.rewrite_count,
                    "needs_review": segment.needs_review,
                    "dubbing_mode": normalize_dubbing_mode(
                        getattr(segment, "dubbing_mode", DUBBING_MODE_DUB)
                    ),
                }
                for segment in translation_result.segments
            },
            "segment_count": translation_result.total_segments,
            "speaker_names": resolved_names,
        }

    def _normalize_speakers(self, value: int | str) -> int | str:
        if isinstance(value, int):
            if 1 <= value <= 10:
                return value
            raise ValueError("说话人数量范围为 1-10。")

        normalized_value = str(value).strip().lower()
        if normalized_value == "auto":
            return "auto"
        try:
            int_val = int(normalized_value)
            if 1 <= int_val <= 10:
                return int_val
        except ValueError:
            pass
        raise ValueError("说话人数量范围为 1-10 或 auto。")

    @staticmethod
    def _resolve_job_language_profile(
        raw_source_language: object,
        raw_target_language: object,
    ) -> "LanguagePairProfile":
        """Resolve a job's raw source/target language snapshot to a *runnable* profile.

        * **Both fields absent** (None / empty / whitespace — legacy job or no
          snapshot) → the GA default ``en->zh-CN`` profile, byte-identical to the
          pre-multilingual pipeline.
        * **At least one field present but the resulting pair is unsupported** →
          fail-closed ``ValueError``. We never silently run a foreign source
          through the English pipeline (e.g. a job stamped ``source_language='fr'``
          must NOT default to ``en->zh-CN``).
        * **Supported but ``pipeline_ready=False``** (e.g. ``zh-CN->en`` while
          PR-CD/E/F are not yet landed) → fail-closed ``ValueError``. The Gateway
          create-path returns 409 for such pairs, but a direct Job API /
          JobService submission (or a ``copy_as_new`` of a pre-existing row)
          could persist a supported-but-not-ready pair without that check. Since
          replacing the English-only gate made the source/transcript gates accept
          the not-ready source language, the pipeline MUST itself refuse here —
          otherwise it would feed e.g. Chinese into the still English->Chinese
          translator/TTS and burn paid APIs on a half-adapted path. ``pipeline_ready``
          is a code constant (only a pipeline PR flips it), so this is the
          last-line code gate that an ops mistake can never bypass.
        """
        has_source = bool(raw_source_language and str(raw_source_language).strip())
        has_target = bool(raw_target_language and str(raw_target_language).strip())
        if not has_source and not has_target:
            return DEFAULT_LANGUAGE_PAIR_PROFILE
        resolved = resolve_language_pair(
            raw_source_language if has_source else DEFAULT_SOURCE_LANGUAGE,
            raw_target_language if has_target else DEFAULT_TARGET_LANGUAGE,
        )
        if resolved is None:
            raise ValueError(
                "不支持的语言对："
                f"source_language={raw_source_language!r}, "
                f"target_language={raw_target_language!r}。"
                "请确认任务的源/目标语言为受支持的组合。"
            )
        if not resolved.pipeline_ready:
            raise ValueError(
                f"语言对 {resolved.language_pair} 尚未就绪（pipeline_ready=False），"
                "管线拒绝执行以避免半适配的错误产出。"
            )
        return resolved

    def _enforce_source_language(self, download_result: DownloadResult) -> None:
        """Validate the video's source-language metadata against the job's
        expected source language.

        Fail-open when metadata is absent (local uploads carry none) — preserved
        from the legacy gate. For the GA default pair (``en->zh-CN``) this is
        byte-identical to the legacy English-only gate, including the exact log
        line and error message.
        """
        profile = getattr(self, "_language_profile", DEFAULT_LANGUAGE_PAIR_PROFILE)
        source_language = str(getattr(download_result, "language", "") or "").strip()
        if not source_language:
            return
        if profile.is_default:
            if _is_english_language_code(source_language):
                print(f"[S0] 视频源语言元数据：{source_language}")
                return
            raise ValueError(
                "当前只支持英文视频翻译。"
                f"视频源语言元数据为 {source_language!r}，请确认输入的视频是英文内容。"
            )
        expected = profile.source_language
        if normalize_language(source_language) == expected:
            print(f"[S0] 视频源语言元数据：{source_language}（期望源语言 {expected}）")
            return
        raise ValueError(
            f"任务源语言为 {expected}，但视频源语言元数据为 {source_language!r}，"
            "请确认输入视频与所选源语言一致。"
        )

    def _enforce_transcript_language(
        self,
        transcript_result: TranscriptResult,
    ) -> None:
        """Validate the transcript's language against the job's expected source
        language. Byte-identical to the legacy English-only gate for the GA
        default pair; script-aware for non-default pairs.
        """
        profile = getattr(self, "_language_profile", DEFAULT_LANGUAGE_PAIR_PROFILE)
        explicit_language = str(getattr(transcript_result, "language", "") or "").strip()
        _provider_skip = {"auto", "unknown", "und", "undefined"}

        if profile.is_default:
            if explicit_language and not _is_english_language_code(explicit_language):
                if explicit_language.lower() not in _provider_skip:
                    raise ValueError(
                        "当前只支持英文视频翻译。"
                        f"转录服务检测到语言为 {explicit_language!r}，请确认输入的视频是英文内容。"
                    )
            detected_language = self._detect_transcript_language(transcript_result.lines)
            if detected_language != "en":
                raise ValueError(
                    "当前只支持英文视频翻译。检测到转录稿语言为非英文"
                    "（英文字符占比过低）。请确认输入的视频是英文内容。"
                )
            return

        # --- source-aware path (non-default pair) ---
        expected = profile.source_language
        if explicit_language and explicit_language.lower() not in _provider_skip:
            if normalize_language(explicit_language) != expected:
                raise ValueError(
                    f"任务源语言为 {expected}，但转录服务检测到语言为 {explicit_language!r}，"
                    "请确认输入视频与所选源语言一致。"
                )
        detected_language = self._detect_transcript_language(transcript_result.lines)
        if detected_language != expected:
            raise ValueError(
                f"检测到转录稿语言与任务源语言 {expected} 不一致（脚本字符占比过低）。"
                "请确认输入视频与所选源语言一致。"
            )

    def _detect_transcript_language(
        self,
        lines: list[TranscriptLine],
        sample_limit: int = 20,
        english_threshold: float = 0.6,
    ) -> str:
        """Detect whether early transcript lines match the job's source script.

        Returns the matched canonical source language (``"en"`` for the GA
        default) or ``"unknown"``. For a Latin-script source (including the GA
        default) this is byte-identical to the legacy English-ratio detection,
        including the exact log line. For a CJK-script source it measures the
        CJK character ratio instead.
        """
        profile = getattr(self, "_language_profile", DEFAULT_LANGUAGE_PAIR_PROFILE)
        # Derive from the profile (SoT), robust to direct helper calls that bypass
        # run(); for the default en source this resolves to the Latin descriptor.
        descriptor = get_language_descriptor(profile.source_language)
        sample_lines = lines[:sample_limit]
        combined_text = " ".join(
            str(line.source_text).strip() for line in sample_lines if line.source_text
        )
        if not combined_text:
            # Empty transcript, let downstream handle it (no false rejection).
            return "en" if profile.is_default else profile.source_language

        if descriptor is not None and descriptor.script_family == SCRIPT_CJK:
            cjk_chars = sum(1 for ch in combined_text if "一" <= ch <= "鿿")
            # Denominator = letter-like chars only (CJK ideographs + Latin
            # letters; both are ``isalpha``), excluding digits / punctuation /
            # whitespace. A Chinese interview peppered with English names and
            # numbers ("OpenAI GPT-5 2026 Q2 ARR …") must not be falsely judged
            # non-Chinese because those tokens inflate the denominator. Mirrors
            # the Latin branch's ascii_letters/total_letters ratio.
            total_letters = sum(1 for ch in combined_text if ch.isalpha())
            if total_letters == 0:
                return profile.source_language  # no scorable chars, skip detection
            cjk_ratio = cjk_chars / total_letters
            print(
                f"[S1] 语言检测：中文字符占比 {cjk_ratio:.0%}（阈值 {english_threshold:.0%}）"
            )
            return profile.source_language if cjk_ratio >= english_threshold else "unknown"

        # Latin-script source (GA default) — byte-identical legacy logic.
        ascii_letters = sum(1 for ch in combined_text if ch.isascii() and ch.isalpha())
        total_letters = sum(1 for ch in combined_text if ch.isalpha())
        if total_letters == 0:
            return "en"  # No letters at all, skip detection

        english_ratio = ascii_letters / total_letters
        print(f"[S1] 语言检测：英文字符占比 {english_ratio:.0%}（阈值 {english_threshold:.0%}）")
        return "en" if english_ratio >= english_threshold else "unknown"

    def _detect_speaker_ids(self, lines: list[TranscriptLine]) -> list[str]:
        speaker_ids: list[str] = []
        for line in lines:
            speaker_id = str(line.speaker_id).strip() or "speaker_a"
            if speaker_id not in speaker_ids:
                speaker_ids.append(speaker_id)
        return speaker_ids

    def _load_download_metadata(self, project_dir: Path) -> dict[str, object]:
        metadata_path = project_dir / "download_metadata.json"
        if not metadata_path.exists():
            return {}
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _load_download_result(
        self,
        project_dir: Path,
        *,
        fallback_url: str,
        fallback_title: str | None = None,
        fallback_duration_ms: int = 0,
    ) -> DownloadResult:
        metadata = self._load_download_metadata(project_dir)
        # Prefer persisted real paths; fall back to legacy fixed names
        video_path_str = _normalize_optional_text(metadata.get("video_path"))
        audio_path_str = _normalize_optional_text(metadata.get("audio_path"))
        video_path = (
            Path(video_path_str).resolve(strict=False)
            if video_path_str
            else (project_dir / "video" / "original.mp4").resolve(strict=False)
        )
        audio_path = (
            Path(audio_path_str).resolve(strict=False)
            if audio_path_str
            else (project_dir / "audio" / "original.wav").resolve(strict=False)
        )
        return DownloadResult(
            video_path=str(video_path),
            audio_path=str(audio_path),
            video_title=str(metadata.get("video_title") or fallback_title or project_dir.name),
            duration_ms=_coerce_int(metadata.get("duration_ms"), default=fallback_duration_ms),
            url=str(metadata.get("url") or fallback_url),
            description=str(metadata.get("description") or ""),
        )

    def _load_transcript_result(self, transcript_path: Path) -> TranscriptResult:
        payload = json.loads(transcript_path.read_text(encoding="utf-8"))
        lines = [_deserialize_transcript_line_payload(line_payload) for line_payload in payload.get("lines", [])]
        return TranscriptResult(
            lines=lines,
            total_duration_ms=_coerce_int(payload.get("total_duration_ms"), default=0),
            language=str(payload.get("language") or ""),
            raw_response_path=str(payload.get("raw_response_path") or ""),
            structured_transcript_path=str(transcript_path.resolve(strict=False)),
        )

    def _load_translation_result(self, segments_path: Path) -> TranslationResult:
        payload = json.loads(segments_path.read_text(encoding="utf-8"))
        segments: list[DubbingSegment] = []
        dubbing_fields = {f.name for f in _dc_fields(DubbingSegment)}
        for segment_payload in payload.get("segments", []):
            if not isinstance(segment_payload, dict):
                continue
            normalized_payload = dict(segment_payload)
            normalized_payload["dubbing_mode"] = normalize_dubbing_mode(
                normalized_payload.get("dubbing_mode")
            )
            # Defensive: filter to known fields so legacy translation JSONs
            # carrying obsolete schema keys don't crash construction.
            filtered = {k: v for k, v in normalized_payload.items() if k in dubbing_fields}
            seg = DubbingSegment(**filtered)
            _backfill_legacy_tts_input_cn_text(seg)
            segments.append(seg)
        return TranslationResult(
            segments=segments,
            total_segments=_coerce_int(payload.get("total_segments"), default=len(segments)),
            output_path=str(segments_path.resolve(strict=False)),
        )

    @staticmethod
    def _apply_transcript_dubbing_modes_to_segments(
        segments: list[DubbingSegment],
        transcript_lines: list[TranscriptLine],
    ) -> bool:
        mode_by_segment_id = {
            int(line.index): normalize_dubbing_mode(getattr(line, "dubbing_mode", DUBBING_MODE_DUB))
            for line in transcript_lines
        }
        changed = False
        for segment in segments:
            mode = mode_by_segment_id.get(int(segment.segment_id))
            if mode is None:
                segment.dubbing_mode = normalize_dubbing_mode(
                    getattr(segment, "dubbing_mode", DUBBING_MODE_DUB)
                )
                continue
            if normalize_dubbing_mode(getattr(segment, "dubbing_mode", DUBBING_MODE_DUB)) != mode:
                segment.dubbing_mode = mode
                changed = True
        return changed

    @staticmethod
    def _is_placeholder_display_name(display_name: str, speaker_id: str) -> bool:
        """Return True if display_name is a default placeholder (not user-set)."""
        if not display_name:
            return True
        dn = display_name.strip()
        # "Speaker A", "Speaker B", "Speaker C", ... are placeholders from translator
        if dn.startswith("Speaker ") and len(dn) <= len("Speaker ZZ"):
            return True
        # Same as speaker_id itself
        if dn == speaker_id:
            return True
        return False

    def _apply_review_speaker_styles_to_segments(
        self,
        segments: list[DubbingSegment],
        speaker_styles: dict[str, dict[str, object]],
    ) -> None:
        if not speaker_styles:
            return
        from services.tts.cosyvoice_voice_selector import infer_energy_level, infer_persona_style

        for segment in segments:
            speaker_info = speaker_styles.get(segment.speaker_id, {})
            if not isinstance(speaker_info, dict) or not speaker_info:
                continue
            voice_description = str(speaker_info.get("voice_description", "") or "").strip()
            if voice_description:
                segment.voice_description = voice_description
            effective_voice_description = voice_description or str(segment.voice_description or "")
            gender = str(speaker_info.get("gender", "") or "").strip()
            if gender:
                segment.gender = gender
            age_group = str(speaker_info.get("age_group", "") or "").strip()
            if age_group:
                segment.age_group = age_group
            # Propagate reviewer name to display_name for all speakers (including c+).
            # Overwrite default placeholders ("Speaker B", "Speaker C", etc.) but
            # do NOT overwrite a user-confirmed custom name.
            speaker_name = str(speaker_info.get("name", "") or "").strip()
            if speaker_name and self._is_placeholder_display_name(segment.display_name, segment.speaker_id):
                segment.display_name = speaker_name
            persona_style = str(
                speaker_info.get("persona_style", "") or infer_persona_style(effective_voice_description)
            ).strip()
            if persona_style:
                segment.persona_style = persona_style
            energy_level = str(
                speaker_info.get("energy_level", "") or infer_energy_level(effective_voice_description)
            ).strip()
            if energy_level:
                segment.energy_level = energy_level

    def _log_review_speaker_styles(
        self,
        speaker_styles: dict[str, dict[str, object]],
    ) -> None:
        if not speaker_styles:
            return
        from services.tts.cosyvoice_voice_selector import infer_energy_level, infer_persona_style

        print(f"[S4] 注入音色描述：{len(speaker_styles)} 个说话人", flush=True)
        for speaker_id, speaker_info in speaker_styles.items():
            name = str(speaker_info.get("name", "") or "")
            voice_description = str(speaker_info.get("voice_description", "") or "")
            gender = str(speaker_info.get("gender", "") or "")
            age_group = str(speaker_info.get("age_group", "") or "")
            persona_style = str(
                speaker_info.get("persona_style", "") or infer_persona_style(voice_description)
            )
            energy_level = str(
                speaker_info.get("energy_level", "") or infer_energy_level(voice_description)
            )
            print(
                f"  {speaker_id} ({name}, {gender}/{age_group}, persona={persona_style}, "
                f"energy={energy_level}): {voice_description[:80]}",
                flush=True,
            )

    def _segments_missing_review_speaker_styles(
        self,
        segments: list[DubbingSegment],
    ) -> bool:
        for segment in segments:
            if not any(
                (
                    getattr(segment, "voice_description", ""),
                    getattr(segment, "gender", ""),
                    getattr(segment, "age_group", ""),
                    getattr(segment, "persona_style", ""),
                    getattr(segment, "energy_level", ""),
                )
            ):
                return True
        return False

    def _recover_review_speaker_styles(
        self,
        *,
        transcript_result: TranscriptResult,
        source_audio_path: Path,
        video_title: str,
        video_url: str,
        cached_segments: list[DubbingSegment] | None = None,
    ) -> dict[str, dict[str, object]]:
        # Restore persisted review facts without re-running S2.
        # On translation cache hits Pass 3 may not run again, so merge Pass 3
        # artifacts and existing segment metadata before falling back to names.
        del source_audio_path, video_title, video_url

        def _merge_non_empty(
            target: dict[str, dict[str, object]],
            speaker_id: str,
            values: dict[str, object],
            *,
            overwrite: bool = True,
        ) -> None:
            if not speaker_id:
                return
            entry = target.setdefault(speaker_id, {})
            for key, value in values.items():
                if value is None:
                    continue
                if isinstance(value, str):
                    value = value.strip()
                if value == "":
                    continue
                if not overwrite and entry.get(key):
                    continue
                entry[key] = value

        transcript_dir = Path(transcript_result.structured_transcript_path).parent
        styles: dict[str, dict[str, object]] = {}

        s2_cache = transcript_dir / "s2_review_result.json"
        if s2_cache.exists():
            try:
                cached = json.loads(s2_cache.read_text(encoding="utf-8"))
                speakers = cached.get("speakers", {})
                if isinstance(speakers, dict):
                    for speaker_id, speaker_info in speakers.items():
                        if isinstance(speaker_info, dict):
                            _merge_non_empty(styles, str(speaker_id), speaker_info)
            except Exception as exc:
                print(f"[S2] Failed to load cached s2_review_result.json: {exc}", flush=True)

        pass3_cache = transcript_dir / "s2_pass3_result.json"
        if pass3_cache.exists():
            try:
                cached = json.loads(pass3_cache.read_text(encoding="utf-8"))
                profiles = cached.get("speaker_profiles", {})
                if isinstance(profiles, dict):
                    for speaker_id, profile in profiles.items():
                        if isinstance(profile, dict):
                            _merge_non_empty(styles, str(speaker_id), profile)
            except Exception as exc:
                print(f"[S2] Failed to load cached s2_pass3_result.json: {exc}", flush=True)

        for segment in cached_segments or []:
            segment_values = {
                "name": getattr(segment, "display_name", ""),
                "voice_description": getattr(segment, "voice_description", ""),
                "gender": getattr(segment, "gender", ""),
                "age_group": getattr(segment, "age_group", ""),
                "persona_style": getattr(segment, "persona_style", ""),
                "energy_level": getattr(segment, "energy_level", ""),
            }
            if self._is_placeholder_display_name(
                str(segment_values["name"] or ""),
                getattr(segment, "speaker_id", ""),
            ):
                segment_values.pop("name", None)
            _merge_non_empty(
                styles,
                getattr(segment, "speaker_id", ""),
                segment_values,
                overwrite=False,
            )

        if styles:
            print(f"[S2] Restored speaker styles from persisted cache ({len(styles)} speakers).", flush=True)
            return styles

        # Fallback: build minimal styles from transcript speaker IDs.
        print("[S2] No cached speaker style result; using minimal speaker names.", flush=True)
        speaker_ids = list({line.speaker_id for line in transcript_result.lines if line.speaker_id})
        return {
            sid: {"name": sid.replace("speaker_", "Speaker ").replace("_", " ").title()}
            for sid in sorted(speaker_ids)
        }

    def _resolve_cached_display_names(
        self,
        translation_result: TranslationResult,
        *,
        fallback_speaker_a: str,
        fallback_speaker_b: str,
    ) -> tuple[str, str]:
        speaker_a_name = fallback_speaker_a
        speaker_b_name = fallback_speaker_b
        for segment in translation_result.segments:
            if segment.speaker_id == "speaker_a" and fallback_speaker_a == "Speaker A":
                speaker_a_name = segment.display_name
            if segment.speaker_id == "speaker_b" and fallback_speaker_b == "Speaker B":
                speaker_b_name = segment.display_name
        return speaker_a_name, speaker_b_name

    def _apply_runtime_voice_overrides(
        self,
        segments: list[DubbingSegment],
        *,
        voice_id_a: str,
        display_name_a: str,
        voice_id_b: str | None,
        display_name_b: str,
        speaker_voices: dict[str, str] | None = None,
        speaker_providers: dict[str, str] | None = None,
        speaker_voice_routing: dict[str, dict[str, object]] | None = None,
    ) -> None:
        """Apply user-confirmed voice / provider / worker-routing overrides
        onto runtime segments.

        Phase 4.1 E.3 (Codex 2026-05-25 三签字版本) 加 ``speaker_voice_routing``
        kwarg。每个条目 schema：

        ::

            {
                speaker_id: {
                    "requires_worker": True,
                    "worker_target_model": "cosyvoice-v3.5-flash",
                }
            }

        若条目存在 → 对该 speaker 的 segment 设
        ``requires_worker / worker_target_model / tts_provider="cosyvoice"``
        三字段。``tts_provider`` 是 defense-in-depth：Gateway E.2 已经强制写入
        approved payload，这里再写一次防 routing 字典与 ``speaker_providers``
        漂移。

        缺 routing 条目的 speaker → segment 字段保持默认（``False / ""``），
        legacy 任务向后兼容。
        """
        for segment in segments:
            # N-speaker: use speaker_voices dict if available
            if speaker_voices and segment.speaker_id in speaker_voices:
                segment.voice_id = speaker_voices[segment.speaker_id]
            elif segment.speaker_id == "speaker_b":
                if voice_id_b is not None:
                    segment.voice_id = voice_id_b
                segment.display_name = display_name_b
            elif segment.speaker_id == "speaker_a":
                segment.voice_id = voice_id_a
                segment.display_name = display_name_a
            # speaker_c+ without speaker_voices: leave voice_id as-is (auto-match)

            # Per-speaker TTS provider override (three-engine voice selection)
            if speaker_providers and segment.speaker_id in speaker_providers:
                segment.tts_provider = speaker_providers[segment.speaker_id]

            # Phase 4.1 E.3: worker routing (CosyVoice clone path)
            # PR #7 Codex review (2026-05-25): reset first. Segments restored
            # from cached snapshots may already carry requires_worker=True from
            # a previous approve; if the user later switches this speaker to a
            # non-worker voice, stale flags must not survive.
            segment.requires_worker = False
            segment.worker_target_model = ""
            if speaker_voice_routing and segment.speaker_id in speaker_voice_routing:
                routing = speaker_voice_routing[segment.speaker_id]
                segment.requires_worker = bool(routing.get("requires_worker", False))
                segment.worker_target_model = str(
                    routing.get("worker_target_model", "") or ""
                )
                # Defense-in-depth：worker 路径必须搭配 cosyvoice provider；
                # D.4 P1 fix 会兜底，这里显式 set 让 segment snapshot 也一致
                if segment.requires_worker:
                    segment.tts_provider = "cosyvoice"

    def _hydrate_cached_tts_segments(
        self,
        segments: list[DubbingSegment],
        tts_dir: Path,
    ) -> tuple[list[DubbingSegment], list[DubbingSegment]]:
        cached_segments: list[DubbingSegment] = []
        segments_needing_tts: list[DubbingSegment] = []

        for segment in segments:
            if is_keep_original_dubbing_mode(getattr(segment, "dubbing_mode", DUBBING_MODE_DUB)):
                if segment.tts_audio_path and Path(segment.tts_audio_path).exists():
                    cached_segments.append(segment)
                continue
            if getattr(segment, "short_merge_applied", False):
                segments_needing_tts.append(segment)
                continue
            expected_path = tts_dir / f"segment_{segment.segment_id:03d}_{segment.speaker_id}.wav"
            cached_path: Path | None = None
            if expected_path.exists():
                cached_path = expected_path
            elif segment.tts_audio_path and Path(segment.tts_audio_path).exists():
                cached_path = Path(segment.tts_audio_path).resolve(strict=False)

            if cached_path is None:
                segments_needing_tts.append(segment)
                continue

            segment.tts_audio_path = str(cached_path.resolve(strict=False))
            segment.actual_duration_ms = _ffprobe_duration_ms(cached_path)
            # tts_cn_text unified into cn_text — no fallback needed
            if segment.target_duration_ms > 0:
                segment.alignment_ratio = segment.actual_duration_ms / segment.target_duration_ms
            else:
                segment.alignment_ratio = 0.0
            cached_segments.append(segment)

        return cached_segments, segments_needing_tts

    def _materialize_keep_original_segments(
        self,
        segments: list[DubbingSegment],
        *,
        source_audio_path: Path,
        tts_dir: Path,
    ) -> int:
        """Extract source-audio slices for segments marked keep_original.

        These slices occupy the same downstream slot as TTS output, so the
        normal alignment/export pipeline can stay deterministic and block-based.
        """
        keep_segments = [
            segment for segment in segments
            if is_keep_original_dubbing_mode(getattr(segment, "dubbing_mode", DUBBING_MODE_DUB))
        ]
        if not keep_segments:
            return 0
        if not source_audio_path.exists():
            raise FileNotFoundError(f"保留原音失败：找不到原始音频 {source_audio_path}")

        import subprocess

        tts_dir.mkdir(parents=True, exist_ok=True)
        materialized_count = 0
        for segment in keep_segments:
            start_ms = max(0, int(getattr(segment, "start_ms", 0) or 0))
            end_ms = max(start_ms, int(getattr(segment, "end_ms", 0) or 0))
            target_duration_ms = max(0, int(getattr(segment, "target_duration_ms", 0) or 0))
            duration_ms = max(target_duration_ms, end_ms - start_ms)
            if duration_ms <= 0:
                continue

            output_path = (
                tts_dir / f"segment_{int(segment.segment_id):03d}_{segment.speaker_id}_original.wav"
            ).resolve(strict=False)
            if not output_path.exists():
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    f"{start_ms / 1000:.3f}",
                    "-i",
                    str(source_audio_path.resolve(strict=False)),
                    "-t",
                    f"{duration_ms / 1000:.3f}",
                    "-vn",
                    "-acodec",
                    "pcm_s16le",
                    "-ar",
                    "44100",
                    "-ac",
                    "2",
                    str(output_path),
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, check=False)
                if result.returncode != 0:
                    stderr = (result.stderr or "").strip()
                    raise RuntimeError(f"保留原音片段提取失败 segment_{segment.segment_id:03d}: {stderr}")

            actual_duration_ms = _ffprobe_duration_ms(output_path)
            segment.tts_audio_path = str(output_path)
            segment.aligned_audio_path = str(output_path)
            segment.actual_duration_ms = actual_duration_ms
            segment.alignment_ratio = (
                actual_duration_ms / target_duration_ms
                if target_duration_ms > 0
                else 1.0
            )
            segment.alignment_method = DUBBING_MODE_KEEP_ORIGINAL
            segment.needs_review = False
            segment.rewrite_count = 0
            segment.force_dsp_severity = ""
            segment.force_dsp_review_suppressed = False
            segment.force_dsp_review_reason = ""
            segment.dsp_speed_ratio_used = 1.0
            segment.dsp_silence_padded_ms = 0
            segment.dsp_truncated_ms = 0
            segment.dsp_initial_duration_ms = 0
            segment.dsp_trimmed_duration_ms = 0
            segment.dsp_stretched_duration_ms = 0
            segment.short_content_compact_attempted = False
            segment.short_content_compact_accepted = False
            segment.short_content_compact_rejected_reason = ""
            segment.short_content_compact_class = ""
            segment.short_content_compact_lower_chars = 0
            segment.short_content_compact_upper_chars = 0
            segment.short_content_compact_pre_chars = 0
            segment.short_content_compact_post_chars = 0
            segment.selected_voice = "original_audio"
            segment.match_confidence = DUBBING_MODE_KEEP_ORIGINAL
            segment.tts_provider = "original"
            segment.first_pass_duration_ms = actual_duration_ms
            if not getattr(segment, "first_pass_cn_text", ""):
                segment.first_pass_cn_text = ""
            segment.first_pass_error_pct = (
                (actual_duration_ms - target_duration_ms) / target_duration_ms
                if target_duration_ms > 0
                else 0.0
            )
            materialized_count += 1

        return materialized_count

    def _materialize_empty_text_keep_original_segments(
        self,
        segments: list[DubbingSegment],
        *,
        source_audio_path: Path,
        tts_dir: Path,
    ) -> int:
        """Convert safe empty-translation segments to original audio before TTS.

        Empty Chinese text is never a valid TTS input. As a final fallback,
        preserve the source slice instead of retrying a request that cannot
        succeed.
        """
        auto_keep_segments: list[DubbingSegment] = []
        for segment in segments:
            if is_keep_original_dubbing_mode(getattr(segment, "dubbing_mode", DUBBING_MODE_DUB)):
                continue
            if str(getattr(segment, "cn_text", "") or "").strip():
                continue
            segment.dubbing_mode = DUBBING_MODE_KEEP_ORIGINAL
            segment.auto_keep_original_reason = "empty_text"
            segment.auto_keep_original_source = "empty_text_guard"
            auto_keep_segments.append(segment)

        if not auto_keep_segments:
            return 0
        return self._materialize_keep_original_segments(
            auto_keep_segments,
            source_audio_path=source_audio_path,
            tts_dir=tts_dir,
        )

    def _auto_keep_low_information_underflow_segments(
        self,
        segments: list[DubbingSegment],
        *,
        source_audio_path: Path,
        tts_dir: Path,
    ) -> int:
        """Preserve original audio for severe low-information underflow cues.

        This runs after alignment, so it only acts on segments where the
        normal DSP/rewrite path has already proven that the translated TTS is
        far too short for the slot. It is intentionally conservative: generic
        timer/filler/backchannel cues can stay as source audio, while normal
        contentful sentences remain dubbed and reviewable.
        """
        auto_keep_segments: list[DubbingSegment] = []
        for segment in segments:
            reason = self._low_information_underflow_keep_original_reason(segment)
            if not reason:
                continue
            segment.dubbing_mode = DUBBING_MODE_KEEP_ORIGINAL
            segment.auto_keep_original_reason = reason
            segment.auto_keep_original_source = "low_information_underflow_route"
            auto_keep_segments.append(segment)

        if not auto_keep_segments:
            return 0
        return self._materialize_keep_original_segments(
            auto_keep_segments,
            source_audio_path=source_audio_path,
            tts_dir=tts_dir,
        )

    @staticmethod
    def _low_information_underflow_keep_original_reason(
        segment: DubbingSegment,
    ) -> str:
        if is_keep_original_dubbing_mode(getattr(segment, "dubbing_mode", DUBBING_MODE_DUB)):
            return ""
        if str(getattr(segment, "alignment_method", "") or "") != "capped_dsp_underflow":
            return ""
        target_duration_ms = int(getattr(segment, "target_duration_ms", 0) or 0)
        first_pass_duration_ms = int(getattr(segment, "first_pass_duration_ms", 0) or 0)
        if (
            target_duration_ms < LOW_INFORMATION_UNDERFLOW_KEEP_ORIGINAL_MIN_TARGET_MS
            or first_pass_duration_ms <= 0
        ):
            return ""
        stretch_ratio = target_duration_ms / first_pass_duration_ms
        if stretch_ratio < LOW_INFORMATION_UNDERFLOW_KEEP_ORIGINAL_MIN_STRETCH_RATIO:
            return ""

        source_tokens = ProcessPipeline._source_word_tokens(
            getattr(segment, "source_text", "") or ""
        )
        if len(source_tokens) > LOW_INFORMATION_UNDERFLOW_KEEP_ORIGINAL_MAX_SOURCE_WORDS:
            return ""
        spoken_chars = count_spoken_chars(getattr(segment, "cn_text", "") or "")
        if spoken_chars > LOW_INFORMATION_UNDERFLOW_KEEP_ORIGINAL_MAX_SPOKEN_CHARS:
            return ""
        if not ProcessPipeline._looks_like_low_information_cue(source_tokens):
            return ""
        return "low_information_cue_underflow"

    @staticmethod
    def _source_word_tokens(text: str) -> list[str]:
        return [token.lower() for token in re.findall(r"[A-Za-z0-9']+", text)]

    @staticmethod
    def _looks_like_low_information_cue(source_tokens: list[str]) -> bool:
        if not source_tokens:
            return False
        cue_hits = sum(1 for token in source_tokens if token in LOW_INFORMATION_CUE_TOKENS)
        has_numeric = any(any(ch.isdigit() for ch in token) for token in source_tokens)
        has_you_know = "you" in source_tokens and "know" in source_tokens
        has_filler = any(token in {"uh", "um", "er", "hmm", "ah"} for token in source_tokens)
        if has_filler and len(source_tokens) <= 4:
            return True
        if has_you_know and len(source_tokens) <= 5:
            return True
        if has_numeric and cue_hits >= 1:
            return True
        if cue_hits >= 2:
            return True
        return False

    @staticmethod
    def _short_content_compact_char_bounds(target_duration_ms: int) -> tuple[int, int]:
        target_seconds = max(0.0, int(target_duration_ms) / 1000.0)
        lower_cps = SHORT_CONTENT_COMPACT_CHARS_PER_SECOND_LOWER
        if target_duration_ms >= SHORT_CONTENT_COMPACT_LONG_TARGET_MIN_MS:
            lower_cps = SHORT_CONTENT_COMPACT_LONG_CHARS_PER_SECOND_LOWER
        lower = max(6, int(round(target_seconds * lower_cps)))
        upper = max(lower + 2, int(round(target_seconds * SHORT_CONTENT_COMPACT_CHARS_PER_SECOND_UPPER)))
        return lower, upper

    @staticmethod
    def _short_content_compact_retry_char_bounds(
        lower_chars: int,
        upper_chars: int,
    ) -> tuple[int, int]:
        if upper_chars - lower_chars >= 4:
            return lower_chars, upper_chars - 1
        return lower_chars, upper_chars

    @staticmethod
    def _short_content_required_tokens(text: str) -> list[str]:
        # Hard guard only for literal ASCII/digit tokens already present in the
        # Chinese draft. Proper names may be translated, but digits/acronyms
        # that are already literal should not disappear during compression.
        tokens = re.findall(r"[A-Za-z0-9]+", text or "")
        result: list[str] = []
        for token in tokens:
            if any(ch.isdigit() for ch in token) or token.isupper():
                result.append(token)
        return result

    @staticmethod
    def _short_content_compact_class(segment: DubbingSegment) -> str:
        source_text = getattr(segment, "source_text", "") or ""
        tokens = ProcessPipeline._source_word_tokens(source_text)
        role = str(getattr(segment, "speaker_role", "") or "").strip().lower()
        source_lower = source_text.lower()
        if role == "non_speech":
            return "non_speech"
        if any(marker in source_lower for marker in ("[music]", "[applause]", "[laughter]", "♪")):
            return "non_speech"
        if tokens and all(token in SHORT_CONTENT_COMPACT_NON_SPEECH_TOKENS for token in tokens):
            return "non_speech"
        if ProcessPipeline._is_low_information_short_content(tokens, source_text):
            return "low_information"
        if "?" in source_text or (tokens and tokens[0] in SHORT_CONTENT_COMPACT_QUESTION_STARTERS):
            return "question"
        content_tokens = [
            token for token in tokens
            if token not in SHORT_CONTENT_COMPACT_FILLER_TOKENS
        ]
        if len(tokens) <= 14 and len(content_tokens) >= 2:
            return "short_answer_or_clause"
        return "content_clause"

    @staticmethod
    def _is_low_information_short_content(
        source_tokens: list[str],
        source_text: str = "",
    ) -> bool:
        if not source_tokens:
            return True
        if "?" in (source_text or ""):
            return False
        token_set = set(source_tokens)
        if len(source_tokens) <= 3 and token_set <= LOW_INFORMATION_CUE_TOKENS:
            return True
        if len(source_tokens) <= 6 and len(token_set - LOW_INFORMATION_CUE_TOKENS) <= 1:
            return True
        return False

    @staticmethod
    def _is_short_content_compact_candidate(
        segment: DubbingSegment,
        *,
        rewrite_label: str,
        pre_chars: int,
        estimated_duration_ms: int,
        decision_estimated_duration_ms: int,
        target_duration_ms: int,
    ) -> tuple[bool, str, int, int]:
        if rewrite_label != "overshoot":
            return False, "", 0, 0
        if not (
            SHORT_CONTENT_COMPACT_MIN_TARGET_MS
            <= target_duration_ms
            < SHORT_CONTENT_COMPACT_MAX_TARGET_MS
        ):
            return False, "", 0, 0
        if estimated_duration_ms <= 0 or decision_estimated_duration_ms <= 0:
            return False, "", 0, 0
        overshoot_ratio = (
            decision_estimated_duration_ms - target_duration_ms
        ) / target_duration_ms
        if overshoot_ratio < SHORT_CONTENT_COMPACT_MIN_OVERSHOOT_RATIO:
            return False, "", 0, 0

        content_class = ProcessPipeline._short_content_compact_class(segment)
        if content_class in {"low_information", "non_speech"}:
            return False, content_class, 0, 0
        source_tokens = ProcessPipeline._source_word_tokens(
            getattr(segment, "source_text", "") or ""
        )
        if len(source_tokens) < SHORT_CONTENT_COMPACT_MIN_SOURCE_WORDS:
            return False, content_class, 0, 0
        lower, upper = ProcessPipeline._short_content_compact_char_bounds(target_duration_ms)
        if pre_chars <= upper + SHORT_CONTENT_COMPACT_MIN_PRE_CHARS_OVER_UPPER:
            return False, content_class, lower, upper
        return True, content_class, lower, upper

    @staticmethod
    def _short_content_compact_rejection_reason(
        *,
        pre_chars: int,
        post_chars: int,
        lower_chars: int,
        upper_chars: int,
        rewritten_text: str,
        current_text: str,
    ) -> str:
        if not (rewritten_text or "").strip():
            return "empty"
        if rewritten_text.strip() == (current_text or "").strip():
            return "unchanged"
        if pre_chars <= 0 or post_chars <= 0:
            return "empty"
        if post_chars >= pre_chars:
            return "wrong_direction"
        if post_chars < lower_chars:
            return "below_floor"
        if post_chars > upper_chars:
            return "above_ceiling"
        missing_tokens = [
            token for token in ProcessPipeline._short_content_required_tokens(current_text)
            if token not in rewritten_text
        ]
        if missing_tokens:
            return "missing_required_token"
        return ""

    def _legacy_speaker_inference_and_review(
        self,
        translator,
        transcript_result,
        effective_speakers,
        speaker_name_a,
        speaker_name_b,
        speaker_name_a_is_placeholder,
        speaker_name_b_is_placeholder,
        download_result,
        normalized_url,
    ) -> tuple:
        """Fallback: use old separate LLM calls for speaker inference + review.

        Returns (transcript_result, speaker_name_a, speaker_name_b) so the
        caller can pick up the updated state.
        """
        if effective_speakers in {1, 2} and (
            speaker_name_a_is_placeholder or speaker_name_b_is_placeholder
        ):
            infer_fn = getattr(translator, "infer_speaker_names", None)
            if callable(infer_fn):
                print("[S2-legacy] Inferring speaker identities...")
                inferred = infer_fn(
                    transcript_result.lines,
                    num_speakers=effective_speakers,
                    video_title=download_result.video_title,
                    youtube_url=normalized_url,
                    video_description=download_result.description,
                )
                if speaker_name_a_is_placeholder:
                    speaker_name_a = inferred.get("speaker_a", speaker_name_a)
                if speaker_name_b_is_placeholder:
                    speaker_name_b = inferred.get("speaker_b", speaker_name_b)
                print(f"[S2-legacy] Speaker A -> {speaker_name_a}")
                if effective_speakers == 2:
                    print(f"[S2-legacy] Speaker B -> {speaker_name_b}")

        if effective_speakers == 2:
            print("[S2-legacy] Reviewing speaker labels...")
            reviewed = translator.review_speaker_labels(
                transcript_result.lines,
                {"speaker_a": speaker_name_a, "speaker_b": speaker_name_b},
                video_title=download_result.video_title,
                youtube_url=normalized_url,
            )
            corrections = sum(
                1 for o, r in zip(transcript_result.lines, reviewed)
                if o.speaker_id != r.speaker_id
            )
            if corrections > 0:
                print(f"[S2-legacy] Corrected {corrections} speaker label(s).")
                transcript_result = TranscriptResult(
                    lines=reviewed,
                    total_duration_ms=transcript_result.total_duration_ms,
                    language=transcript_result.language,
                    raw_response_path=transcript_result.raw_response_path,
                    structured_transcript_path=transcript_result.structured_transcript_path,
                )
                self._write_transcript_result(transcript_result)

        return transcript_result, speaker_name_a, speaker_name_b

    @staticmethod
    def _fallback_minimal_speaker_styles(
        *,
        effective_speakers: int,
        speaker_name_a: str,
        speaker_name_b: str,
        speaker_ids: list[str] | None = None,
    ) -> dict[str, dict]:
        """Generate minimal speaker_styles when unified review is completely unavailable.

        Only fills ``gender`` and ``age_group`` using the simplest possible
        heuristic (default to "male" / "middle").  The ``_source`` field marks
        these entries as low-confidence rule-based fallbacks so that downstream
        consumers (e.g. voice matcher) can distinguish them from LLM review output.

        This is intentionally *not* a smart name-lookup system — it's the absolute
        minimum to prevent the voice matcher from receiving empty gender fields and
        falling back to a single default voice for all speakers.
        """
        styles: dict[str, dict] = {}
        resolved_speaker_ids = [
            speaker_id
            for speaker_id in (speaker_ids or [])
            if _is_valid_speaker_id(speaker_id)
        ]
        if not resolved_speaker_ids:
            max_speakers = max(1, int(effective_speakers))
            resolved_speaker_ids = [
                f"speaker_{chr(ord('a') + offset)}"
                for offset in range(max_speakers)
            ]

        # Default assumption: male / middle. Crude, but better than empty.
        for spk_id in resolved_speaker_ids:
            if spk_id == "speaker_a":
                spk_name = speaker_name_a or _default_speaker_display_name(spk_id)
            elif spk_id == "speaker_b":
                spk_name = speaker_name_b or _default_speaker_display_name(spk_id)
            else:
                spk_name = _default_speaker_display_name(spk_id)
            styles[spk_id] = {
                "name": spk_name,
                "gender": "male",
                "age_group": "middle",
                "voice_description": "",
                "_source": "fallback_minimal",
            }

        print(
            f"[S2-fallback] Minimal speaker profiling: {len(styles)} speaker(s) "
            f"(gender=male, age_group=middle, source=fallback_minimal)",
            flush=True,
        )
        return styles

    def _is_default_placeholder_speaker_name(self, *, speaker_id: str, speaker_name: str) -> bool:
        normalized_speaker_id = speaker_id.strip().casefold()
        normalized_name = " ".join(speaker_name.strip().replace("_", " ").split()).casefold()
        placeholder_names = DEFAULT_PLACEHOLDER_SPEAKER_NAMES.get(normalized_speaker_id, set())
        return normalized_name in placeholder_names

    def _repair_failed_long_segments(
        self,
        *,
        translation_result: TranslationResult,
        tts_generator: TTSGenerator,
        rewriter: GeminiRewriter,
        tts_dir: Path,
        post_tts_budget_tracker: PostTTSBudgetTracker,
    ) -> int:
        next_segment_id = max((segment.segment_id for segment in translation_result.segments), default=0) + 1
        repaired_count = 0
        repaired_segments: list[DubbingSegment] = []

        for segment in translation_result.segments:
            if is_keep_original_dubbing_mode(getattr(segment, "dubbing_mode", DUBBING_MODE_DUB)):
                repaired_segments.append(segment)
                continue
            repaired_children = self._attempt_semantic_split_repair(
                segment=segment,
                next_segment_id=next_segment_id,
                tts_generator=tts_generator,
                rewriter=rewriter,
                tts_dir=tts_dir,
                post_tts_budget_tracker=post_tts_budget_tracker,
            )
            if repaired_children is None:
                repaired_segments.append(segment)
                continue

            repaired_segments.extend(repaired_children)
            next_segment_id = max(child.segment_id for child in repaired_children) + 1
            repaired_count += 1

        if repaired_count > 0:
            translation_result.segments = repaired_segments
            translation_result.total_segments = len(repaired_segments)
        return repaired_count

    def _pre_rewrite_obvious_overshoot_segments_before_tts(
        self,
        *,
        segments: list[DubbingSegment],
        rewriter: GeminiRewriter,
        chars_per_second: float,
        chars_per_second_by_speaker: dict[str, float],
        job_provider: str | None = None,
    ) -> int:
        """Pre-TTS LLM rewrite for obvious over/undershoots.

        ``job_provider`` mirrors ``TTSGenerator._generate_one``'s provider
        resolution chain so that pre-rewrite skip stays consistent with
        the actual TTS code path:

            effective_provider = segment.tts_provider or job_provider

        Without this, a single-engine VolcEngine job (where
        ``_speaker_providers`` is empty, so ``segment.tts_provider``
        stays ``""``) would run VolcEngine speed at TTS time but the
        rewrite skip would not fire — CodeX Phase 2 follow-up review
        2026-04-15.
        """
        rewritten_count = 0
        job_provider_norm = (job_provider or "").strip().lower() or None

        for segment in segments:
            if is_keep_original_dubbing_mode(getattr(segment, "dubbing_mode", DUBBING_MODE_DUB)):
                continue
            target_duration_ms = int(segment.target_duration_ms)
            if target_duration_ms <= 0:
                continue
            is_short_target = (
                PRE_TTS_REWRITE_SHORT_MIN_TARGET_MS
                <= target_duration_ms
                < PRE_TTS_REWRITE_MIN_TARGET_MS
            )
            if target_duration_ms < PRE_TTS_REWRITE_MIN_TARGET_MS and not is_short_target:
                continue

            current_text = segment.cn_text.strip()
            if not current_text:
                continue

            speaker_chars_per_second = chars_per_second_by_speaker.get(
                segment.speaker_id,
                chars_per_second,
            )
            estimated_duration_ms = TTSDurationEstimator(
                chars_per_second=speaker_chars_per_second
            ).estimate_duration_ms(current_text)
            if estimated_duration_ms <= 0:
                continue

            is_near_short_target = (
                PRE_TTS_REWRITE_MIN_TARGET_MS
                <= target_duration_ms
                < PRE_TTS_REWRITE_NEAR_SHORT_TARGET_MS
            )
            decision_estimated_duration_ms = estimated_duration_ms
            if is_short_target or is_near_short_target:
                decision_estimated_duration_ms = int(
                    round(
                        estimated_duration_ms
                        * PRE_TTS_REWRITE_SHORT_DECISION_ESTIMATE_MARGIN
                    )
                )

            overshoot_ratio = (
                decision_estimated_duration_ms - target_duration_ms
            ) / target_duration_ms
            undershoot_ratio = (target_duration_ms - estimated_duration_ms) / target_duration_ms

            # Plan-C+: if TTS speed can absorb the drift safely, skip the LLM
            # rewrite call. Effective range = admin clamp ∩ listen-comfort
            # guardrail. CodeX P1-1 + P1-2: skip is gated on (a) admin
            # tts_speed_adjustment_enabled and (b) provider has speed knob
            # wired (minimax + volcengine as of 2026-04-15). The segment-level
            # override takes precedence; falls back to the job-level provider
            # so single-engine jobs (which don't populate segment.tts_provider)
            # also benefit. Without these, speed won't run in TTS, so rewrite
            # remains the only safety net.
            seg_provider = (getattr(segment, "tts_provider", "") or "").strip().lower()
            if not seg_provider and job_provider_norm:
                seg_provider = job_provider_norm
            try:
                from services.tts.speed_decision import (
                    _get_speed_clamp,
                    is_speed_adjustment_enabled,
                )
                _speed_runtime_ok = (
                    is_speed_adjustment_enabled()
                    and seg_provider in SPEED_AWARE_TTS_PROVIDERS
                )
                _smin, _smax = _get_speed_clamp() if _speed_runtime_ok else (1.0, 1.0)
            except Exception:
                _speed_runtime_ok = False
                _smin, _smax = (1.0, 1.0)
            if _speed_runtime_ok:
                _eff_max = min(_smax, PRE_TTS_REWRITE_LISTEN_LIMIT_HIGH)
                _eff_min = max(_smin, PRE_TTS_REWRITE_LISTEN_LIMIT_LOW)
                ratio = decision_estimated_duration_ms / target_duration_ms
                if _eff_min <= ratio <= _eff_max:
                    continue  # speed will handle it within listen-comfort range

            needs_rewrite = False
            rewrite_label = ""
            if is_short_target:
                if overshoot_ratio >= PRE_TTS_REWRITE_SHORT_OVERSHOOT_RATIO:
                    needs_rewrite = True
                    rewrite_label = "overshoot"
            elif is_near_short_target:
                if overshoot_ratio >= PRE_TTS_REWRITE_NEAR_SHORT_OVERSHOOT_RATIO:
                    needs_rewrite = True
                    rewrite_label = "overshoot"
            elif overshoot_ratio >= PRE_TTS_REWRITE_OVERSHOOT_RATIO:
                needs_rewrite = True
                rewrite_label = "overshoot"
            elif undershoot_ratio > PRE_TTS_REWRITE_UNDERSHOOT_RATIO:
                needs_rewrite = True
                rewrite_label = "undershoot"

            if not needs_rewrite:
                continue

            pre_rewrite_chars = _count_spoken_chars_for_metering(current_text)
            (
                is_short_content_compact,
                short_content_class,
                compact_lower_chars,
                compact_upper_chars,
            ) = self._is_short_content_compact_candidate(
                segment,
                rewrite_label=rewrite_label,
                pre_chars=pre_rewrite_chars,
                estimated_duration_ms=estimated_duration_ms,
                decision_estimated_duration_ms=decision_estimated_duration_ms,
                target_duration_ms=target_duration_ms,
            )
            if is_short_content_compact:
                compact_text = self._rewrite_short_content_compact_with_guardrails(
                    rewriter=rewriter,
                    current_text=current_text,
                    source_text=segment.source_text,
                    target_duration_ms=target_duration_ms,
                    target_lower_chars=compact_lower_chars,
                    target_upper_chars=compact_upper_chars,
                ).strip()
                compact_chars = _count_spoken_chars_for_metering(compact_text)
                compact_rejection_reason = self._short_content_compact_rejection_reason(
                    pre_chars=pre_rewrite_chars,
                    post_chars=compact_chars,
                    lower_chars=compact_lower_chars,
                    upper_chars=compact_upper_chars,
                    rewritten_text=compact_text,
                    current_text=current_text,
                )
                segment.short_content_compact_attempted = True
                segment.short_content_compact_class = short_content_class
                segment.short_content_compact_lower_chars = compact_lower_chars
                segment.short_content_compact_upper_chars = compact_upper_chars
                segment.short_content_compact_pre_chars = pre_rewrite_chars
                segment.short_content_compact_post_chars = compact_chars

                if compact_rejection_reason:
                    retry_attempted = False
                    initial_rejection_reason = compact_rejection_reason
                    if compact_rejection_reason == "above_ceiling":
                        (
                            retry_lower_chars,
                            retry_upper_chars,
                        ) = self._short_content_compact_retry_char_bounds(
                            compact_lower_chars,
                            compact_upper_chars,
                        )
                        retry_text = self._rewrite_short_content_compact_with_guardrails(
                            rewriter=rewriter,
                            current_text=current_text,
                            source_text=segment.source_text,
                            target_duration_ms=target_duration_ms,
                            target_lower_chars=retry_lower_chars,
                            target_upper_chars=retry_upper_chars,
                            strict_retry_reason=(
                                f"{compact_rejection_reason}:{compact_chars}>{compact_upper_chars}"
                            ),
                        ).strip()
                        retry_chars = _count_spoken_chars_for_metering(retry_text)
                        retry_rejection_reason = (
                            self._short_content_compact_rejection_reason(
                                pre_chars=pre_rewrite_chars,
                                post_chars=retry_chars,
                                lower_chars=retry_lower_chars,
                                upper_chars=retry_upper_chars,
                                rewritten_text=retry_text,
                                current_text=current_text,
                            )
                        )
                        retry_attempted = True
                        segment.short_content_compact_lower_chars = retry_lower_chars
                        segment.short_content_compact_upper_chars = retry_upper_chars
                        segment.short_content_compact_post_chars = retry_chars
                        if not retry_rejection_reason:
                            self._apply_pre_tts_rewrite_success(
                                segment=segment,
                                rewritten_text=retry_text,
                                rewrite_label=rewrite_label,
                                estimate_ms=estimated_duration_ms,
                                target_ms=target_duration_ms,
                                pre_chars=pre_rewrite_chars,
                                post_chars=retry_chars,
                                task_name=SHORT_CONTENT_COMPACT_TASK,
                                retry_attempted=True,
                                retry_accepted=True,
                                initial_rejected_reason=initial_rejection_reason,
                            )
                            segment.short_content_compact_accepted = True
                            segment.short_content_compact_rejected_reason = ""
                            rewritten_count += 1
                            print(
                                f"[S4] Short-content compact strict retry "
                                f"({short_content_class}) segment_{segment.segment_id:03d}: "
                                f"chars {pre_rewrite_chars}->{retry_chars}, "
                                f"target {target_duration_ms}ms"
                            )
                            continue
                        compact_text = retry_text
                        compact_chars = retry_chars
                        compact_rejection_reason = f"{retry_rejection_reason}_after_retry"
                    segment.short_content_compact_accepted = False
                    segment.short_content_compact_rejected_reason = compact_rejection_reason
                    self._record_pre_tts_rewrite_rejection(
                        segment=segment,
                        reason=f"short_compact_{compact_rejection_reason}",
                        direction=rewrite_label,
                        estimate_ms=estimated_duration_ms,
                        target_ms=target_duration_ms,
                        pre_chars=pre_rewrite_chars,
                        post_chars=compact_chars,
                        lower_chars=segment.short_content_compact_lower_chars,
                        upper_chars=segment.short_content_compact_upper_chars,
                        retry_attempted=retry_attempted,
                        initial_rejected_reason=initial_rejection_reason
                        if retry_attempted
                        else "",
                    )
                    print(
                        f"[S4] Short-content compact rejected segment_{segment.segment_id:03d}: "
                        f"{short_content_class} chars {pre_rewrite_chars}->{compact_chars} "
                        f"outside guardrails ({compact_rejection_reason})"
                    )
                    continue

                self._apply_pre_tts_rewrite_success(
                    segment=segment,
                    rewritten_text=compact_text,
                    rewrite_label=rewrite_label,
                    estimate_ms=estimated_duration_ms,
                    target_ms=target_duration_ms,
                    pre_chars=pre_rewrite_chars,
                    post_chars=compact_chars,
                    task_name=SHORT_CONTENT_COMPACT_TASK,
                    retry_attempted=False,
                    retry_accepted=False,
                )
                segment.short_content_compact_accepted = True
                segment.short_content_compact_rejected_reason = ""
                rewritten_count += 1
                print(
                    f"[S4] Short-content compact ({short_content_class}) "
                    f"segment_{segment.segment_id:03d}: chars "
                    f"{pre_rewrite_chars}->{compact_chars}, "
                    f"target {target_duration_ms}ms"
                )
                continue

            rewrite_char_bounds = self._pre_tts_rewrite_char_bounds(
                rewrite_label=rewrite_label,
                pre_chars=pre_rewrite_chars,
                target_duration_ms=target_duration_ms,
                chars_per_second=speaker_chars_per_second,
            )
            if rewrite_char_bounds is None:
                continue
            target_lower_chars, target_upper_chars = rewrite_char_bounds
            rewritten_text = self._rewrite_pre_tts_with_guardrail_prompt(
                rewriter=rewriter,
                current_text=current_text,
                estimated_duration_ms=estimated_duration_ms,
                target_duration_ms=target_duration_ms,
                source_text=segment.source_text,
                speaker_id=segment.speaker_id,
                rewrite_label=rewrite_label,
                target_lower_chars=target_lower_chars,
                target_upper_chars=target_upper_chars,
                task_name="s5_rewrite",
            ).strip()
            post_rewrite_chars = _count_spoken_chars_for_metering(rewritten_text)
            rejection_reason = self._pre_tts_rewrite_rejection_reason(
                rewrite_label=rewrite_label,
                pre_chars=pre_rewrite_chars,
                post_chars=post_rewrite_chars,
                lower_chars=target_lower_chars,
                upper_chars=target_upper_chars,
                rewritten_text=rewritten_text,
                current_text=current_text,
            )
            retry_attempted = False
            retry_accepted = False
            initial_rejected_reason = ""
            accepted_task = "s5_rewrite"

            if rejection_reason:
                initial_rejected_reason = rejection_reason
                if self._should_retry_pre_tts_rewrite_strict(
                    rewrite_label=rewrite_label,
                    target_duration_ms=target_duration_ms,
                    rejection_reason=rejection_reason,
                ):
                    retry_attempted = True
                    print(
                        f"[S4] Pre-TTS rewrite strict retry segment_{segment.segment_id:03d}: "
                        f"{rewrite_label} first_attempt={rejection_reason} "
                        f"chars {pre_rewrite_chars}->{post_rewrite_chars}, "
                        f"bounds {target_lower_chars}-{target_upper_chars}"
                    )
                    retry_text = self._rewrite_pre_tts_with_guardrail_prompt(
                        rewriter=rewriter,
                        current_text=current_text,
                        estimated_duration_ms=estimated_duration_ms,
                        target_duration_ms=target_duration_ms,
                        source_text=segment.source_text,
                        speaker_id=segment.speaker_id,
                        rewrite_label=rewrite_label,
                        target_lower_chars=target_lower_chars,
                        target_upper_chars=target_upper_chars,
                        task_name=PRE_TTS_REWRITE_STRICT_RETRY_TASK,
                        strict_retry_reason=rejection_reason,
                    ).strip()
                    retry_chars = _count_spoken_chars_for_metering(retry_text)
                    retry_rejection_reason = self._pre_tts_rewrite_rejection_reason(
                        rewrite_label=rewrite_label,
                        pre_chars=pre_rewrite_chars,
                        post_chars=retry_chars,
                        lower_chars=target_lower_chars,
                        upper_chars=target_upper_chars,
                        rewritten_text=retry_text,
                        current_text=current_text,
                    )
                    if not retry_rejection_reason:
                        rewritten_text = retry_text
                        post_rewrite_chars = retry_chars
                        retry_accepted = True
                        accepted_task = PRE_TTS_REWRITE_STRICT_RETRY_TASK
                        rejection_reason = ""
                    else:
                        rewritten_text = retry_text
                        post_rewrite_chars = retry_chars
                        rejection_reason = f"strict_{retry_rejection_reason}"

            if rejection_reason:
                self._record_pre_tts_rewrite_rejection(
                    segment=segment,
                    reason=rejection_reason,
                    direction=rewrite_label,
                    estimate_ms=estimated_duration_ms,
                    target_ms=target_duration_ms,
                    pre_chars=pre_rewrite_chars,
                    post_chars=post_rewrite_chars,
                    lower_chars=target_lower_chars,
                    upper_chars=target_upper_chars,
                    retry_attempted=retry_attempted,
                    initial_rejected_reason=initial_rejected_reason,
                )
                print(
                    f"[S4] Pre-TTS rewrite rejected segment_{segment.segment_id:03d}: "
                    f"{rewrite_label} chars {pre_rewrite_chars}->{post_rewrite_chars} "
                    f"outside guardrails ({rejection_reason})"
                )
                continue

            self._apply_pre_tts_rewrite_success(
                segment=segment,
                rewritten_text=rewritten_text,
                rewrite_label=rewrite_label,
                estimate_ms=estimated_duration_ms,
                target_ms=target_duration_ms,
                pre_chars=pre_rewrite_chars,
                post_chars=post_rewrite_chars,
                task_name=accepted_task,
                retry_attempted=retry_attempted,
                retry_accepted=retry_accepted,
                initial_rejected_reason=initial_rejected_reason,
            )
            rewritten_count += 1
            print(
                f"[S4] Pre-TTS rewrite ({rewrite_label}) segment_{segment.segment_id:03d}: "
                f"estimate {estimated_duration_ms}ms -> target {target_duration_ms}ms"
            )

        return rewritten_count

    @staticmethod
    def _rewrite_short_content_compact_with_guardrails(
        *,
        rewriter: GeminiRewriter,
        current_text: str,
        source_text: str,
        target_duration_ms: int,
        target_lower_chars: int,
        target_upper_chars: int,
        strict_retry_reason: str = "",
    ) -> str:
        compact_rewrite = getattr(rewriter, "rewrite_short_content_compact", None)
        if callable(compact_rewrite):
            kwargs: dict[str, object] = {
                "source_text": source_text,
                "target_duration_ms": target_duration_ms,
                "target_lower_chars": target_lower_chars,
                "target_upper_chars": target_upper_chars,
                "task_name": SHORT_CONTENT_COMPACT_TASK,
            }
            if strict_retry_reason:
                kwargs["strict_retry_reason"] = strict_retry_reason
            return compact_rewrite(
                current_text,
                **kwargs,
            )
        return rewriter.rewrite_for_duration(
            current_text,
            actual_duration_ms=max(target_duration_ms + 1, target_duration_ms * 2),
            target_duration_ms=target_duration_ms,
            source_text=source_text,
        )

    @staticmethod
    def _rewrite_pre_tts_with_guardrail_prompt(
        *,
        rewriter: GeminiRewriter,
        current_text: str,
        estimated_duration_ms: int,
        target_duration_ms: int,
        source_text: str,
        speaker_id: str,
        rewrite_label: str,
        target_lower_chars: int,
        target_upper_chars: int,
        task_name: str = "s5_rewrite",
        strict_retry_reason: str = "",
    ) -> str:
        rewrite_with_profile = getattr(rewriter, "rewrite_for_duration_with_profile", None)
        if callable(rewrite_with_profile):
            if rewrite_label == "overshoot":
                preferred_min_ratio, preferred_max_ratio = (1.0, 1.12)
            else:
                preferred_min_ratio, preferred_max_ratio = (0.88, 1.0)
            return rewrite_with_profile(
                current_text,
                actual_duration_ms=estimated_duration_ms,
                target_duration_ms=target_duration_ms,
                source_text=source_text,
                speaker_id=speaker_id,
                preferred_min_ratio=preferred_min_ratio,
                preferred_max_ratio=preferred_max_ratio,
                target_lower_chars=target_lower_chars,
                target_upper_chars=target_upper_chars,
                task_name=task_name,
                strict_retry_reason=strict_retry_reason,
            )
        return rewriter.rewrite_for_duration(
            current_text,
            actual_duration_ms=estimated_duration_ms,
            target_duration_ms=target_duration_ms,
            source_text=source_text,
            speaker_id=speaker_id,
        )

    @staticmethod
    def _pre_tts_rewrite_rejection_reason(
        *,
        rewrite_label: str,
        pre_chars: int,
        post_chars: int,
        lower_chars: int,
        upper_chars: int,
        rewritten_text: str,
        current_text: str,
    ) -> str:
        if not (rewritten_text or "").strip():
            return "empty"
        if rewritten_text.strip() == (current_text or "").strip():
            return "unchanged"
        if pre_chars <= 0 or post_chars <= 0:
            return "empty"
        if rewrite_label == "overshoot":
            if post_chars >= pre_chars:
                return "wrong_direction"
            if post_chars < lower_chars:
                return "below_floor"
            if post_chars > upper_chars:
                return "above_ceiling"
            return ""
        if rewrite_label == "undershoot":
            if post_chars <= pre_chars:
                return "wrong_direction"
            if post_chars < lower_chars:
                return "below_floor"
            if post_chars > upper_chars:
                return "above_ceiling"
            return ""
        return "unknown_direction"

    @staticmethod
    def _should_retry_pre_tts_rewrite_strict(
        *,
        rewrite_label: str,
        target_duration_ms: int,
        rejection_reason: str,
    ) -> bool:
        if rewrite_label != "overshoot":
            return False
        if target_duration_ms <= PRE_TTS_REWRITE_STRICT_RETRY_MIN_TARGET_MS:
            return False
        return rejection_reason in {
            "above_ceiling",
            "below_floor",
            "wrong_direction",
            "unchanged",
            "empty",
        }

    @staticmethod
    def _record_pre_tts_rewrite_rejection(
        *,
        segment: DubbingSegment,
        reason: str,
        direction: str,
        estimate_ms: int,
        target_ms: int,
        pre_chars: int,
        post_chars: int,
        lower_chars: int,
        upper_chars: int,
        retry_attempted: bool,
        initial_rejected_reason: str = "",
    ) -> None:
        segment.pre_tts_rewrite_rejected = True
        segment.pre_tts_rewrite_rejected_reason = reason
        segment.pre_tts_rewrite_rejected_direction = direction
        segment.pre_tts_rewrite_rejected_estimate_ms = estimate_ms
        segment.pre_tts_rewrite_rejected_target_ms = target_ms
        segment.pre_tts_rewrite_rejected_pre_chars = pre_chars
        segment.pre_tts_rewrite_rejected_post_chars = post_chars
        segment.pre_tts_rewrite_rejected_lower_chars = lower_chars
        segment.pre_tts_rewrite_rejected_upper_chars = upper_chars
        segment.pre_tts_rewrite_retry_attempted = retry_attempted
        segment.pre_tts_rewrite_retry_accepted = False
        segment.pre_tts_rewrite_initial_rejected_reason = initial_rejected_reason

    @staticmethod
    def _apply_pre_tts_rewrite_success(
        *,
        segment: DubbingSegment,
        rewritten_text: str,
        rewrite_label: str,
        estimate_ms: int,
        target_ms: int,
        pre_chars: int,
        post_chars: int,
        task_name: str,
        retry_attempted: bool,
        retry_accepted: bool,
        initial_rejected_reason: str = "",
    ) -> None:
        segment.cn_text = rewritten_text
        segment.tts_input_cn_text = ""
        segment.rewrite_count += 1
        segment.pre_tts_rewrite_direction = rewrite_label
        segment.pre_tts_estimate_ms = estimate_ms
        segment.pre_tts_target_ms = target_ms
        segment.pre_tts_pre_chars = pre_chars
        segment.pre_tts_post_chars = post_chars
        segment.pre_tts_rewrite_task = task_name
        segment.pre_tts_rewrite_retry_attempted = retry_attempted
        segment.pre_tts_rewrite_retry_accepted = retry_accepted
        segment.pre_tts_rewrite_initial_rejected_reason = initial_rejected_reason
        segment.pre_tts_rewrite_rejected = False
        segment.pre_tts_rewrite_rejected_reason = ""
        if task_name != SHORT_CONTENT_COMPACT_TASK:
            segment.short_content_compact_attempted = False
            segment.short_content_compact_accepted = False
            segment.short_content_compact_rejected_reason = ""
            segment.short_content_compact_class = ""
            segment.short_content_compact_lower_chars = 0
            segment.short_content_compact_upper_chars = 0
            segment.short_content_compact_pre_chars = 0
            segment.short_content_compact_post_chars = 0

    @staticmethod
    def _pre_tts_rewrite_char_bounds(
        *,
        rewrite_label: str,
        pre_chars: int,
        target_duration_ms: int,
        chars_per_second: float,
    ) -> tuple[int, int] | None:
        if pre_chars <= 0 or target_duration_ms <= 0 or chars_per_second <= 0:
            return None

        target_chars = max(1.0, target_duration_ms / 1000.0 * chars_per_second)
        if rewrite_label == "overshoot":
            if pre_chars <= 1:
                return None
            target_floor = max(1, int(round(target_chars)))
            required_shrink = max(0.0, 1.0 - (target_chars / pre_chars))
            max_shrink = min(
                PRE_TTS_REWRITE_MAX_CHANGE_CAP,
                max(
                    PRE_TTS_REWRITE_MAX_BASE_CHANGE_RATIO,
                    required_shrink + PRE_TTS_REWRITE_REQUIRED_CHANGE_MARGIN,
                ),
            )
            high_shrink_risk = (
                target_duration_ms <= PRE_TTS_REWRITE_HIGH_SHRINK_RISK_TARGET_MS
                and required_shrink >= PRE_TTS_REWRITE_HIGH_SHRINK_RISK_REQUIRED_SHRINK
            )
            mid_undershoot_risk = (
                not high_shrink_risk
                and PRE_TTS_REWRITE_MID_UNDERSHOOT_RISK_MIN_TARGET_MS
                <= target_duration_ms
                < PRE_TTS_REWRITE_MID_UNDERSHOOT_RISK_MAX_TARGET_MS
                and required_shrink
                >= PRE_TTS_REWRITE_MID_UNDERSHOOT_RISK_REQUIRED_SHRINK
            )
            long_undershoot_risk = (
                target_duration_ms
                > PRE_TTS_REWRITE_LONG_UNDERSHOOT_RISK_MIN_TARGET_MS
                and required_shrink
                >= PRE_TTS_REWRITE_LONG_UNDERSHOOT_RISK_REQUIRED_SHRINK
            )
            if high_shrink_risk:
                max_shrink = min(
                    max_shrink,
                    PRE_TTS_REWRITE_HIGH_SHRINK_RISK_MAX_CHANGE_RATIO,
                )
            if mid_undershoot_risk:
                target_floor = max(
                    target_floor,
                    int(math.ceil(
                        target_chars
                        * PRE_TTS_REWRITE_MID_UNDERSHOOT_RISK_MIN_TARGET_MULTIPLIER
                    )),
                )
                max_shrink = min(
                    max_shrink,
                    PRE_TTS_REWRITE_MID_UNDERSHOOT_RISK_MAX_CHANGE_RATIO,
                )
            if long_undershoot_risk:
                target_floor = max(
                    target_floor,
                    int(math.ceil(
                        target_chars
                        * PRE_TTS_REWRITE_LONG_UNDERSHOOT_RISK_MIN_TARGET_MULTIPLIER
                    )),
                )
                max_shrink = min(
                    max_shrink,
                    PRE_TTS_REWRITE_LONG_UNDERSHOOT_RISK_MAX_CHANGE_RATIO,
                )
            floor_by_shrink_cap = max(1, int(math.ceil(pre_chars * (1.0 - max_shrink))))
            lower = max(target_floor, floor_by_shrink_cap)
            upper_by_target = int(math.ceil(target_chars * 1.12))
            if mid_undershoot_risk:
                upper_by_target = max(
                    upper_by_target,
                    int(math.ceil(
                        target_chars
                        * PRE_TTS_REWRITE_MID_UNDERSHOOT_RISK_MAX_TARGET_MULTIPLIER
                    )),
                )
            if long_undershoot_risk:
                upper_by_target = max(
                    upper_by_target,
                    int(math.ceil(
                        target_chars
                        * PRE_TTS_REWRITE_LONG_UNDERSHOOT_RISK_MAX_TARGET_MULTIPLIER
                    )),
                )
            if high_shrink_risk:
                upper_by_target = max(
                    upper_by_target,
                    int(math.ceil(lower * (1.0 + PRE_TTS_REWRITE_HIGH_SHRINK_RISK_UPPER_SLACK))),
                )
            upper = min(pre_chars - 1, max(lower, upper_by_target))
            if lower > upper:
                return None
            return lower, upper

        if rewrite_label == "undershoot":
            target_ceiling = max(1, int(round(target_chars)))
            required_expand = max(0.0, (target_chars / pre_chars) - 1.0)
            max_expand = min(
                PRE_TTS_REWRITE_MAX_CHANGE_CAP,
                max(
                    PRE_TTS_REWRITE_MAX_BASE_CHANGE_RATIO,
                    required_expand + PRE_TTS_REWRITE_REQUIRED_CHANGE_MARGIN,
                ),
            )
            ceiling_by_expand_cap = max(1, int(math.floor(pre_chars * (1.0 + max_expand))))
            upper = min(target_ceiling, ceiling_by_expand_cap)
            lower = max(pre_chars + 1, int(math.floor(target_chars * 0.88)))
            if lower > upper:
                return None
            return lower, upper

        return None

    @staticmethod
    def _is_pre_tts_rewrite_within_char_guardrails(
        *,
        rewrite_label: str,
        pre_chars: int,
        post_chars: int,
        target_duration_ms: int,
        chars_per_second: float,
    ) -> bool:
        if pre_chars <= 0 or post_chars <= 0 or target_duration_ms <= 0:
            return False
        bounds = ProcessPipeline._pre_tts_rewrite_char_bounds(
            rewrite_label=rewrite_label,
            pre_chars=pre_chars,
            target_duration_ms=target_duration_ms,
            chars_per_second=chars_per_second,
        )
        if bounds is None:
            return False
        lower, upper = bounds

        if rewrite_label == "overshoot":
            if post_chars >= pre_chars:
                return False
            return lower <= post_chars <= upper

        if rewrite_label == "undershoot":
            if post_chars <= pre_chars:
                return False
            return lower <= post_chars <= upper

        return True

    def _presplit_long_overshoot_segments_before_alignment(
        self,
        *,
        translation_result: TranslationResult,
        tts_generator: TTSGenerator,
        tts_dir: Path,
        post_tts_budget_tracker: PostTTSBudgetTracker,
    ) -> int:
        next_segment_id = max((segment.segment_id for segment in translation_result.segments), default=0) + 1
        presplit_count = 0
        updated_segments: list[DubbingSegment] = []

        for segment in translation_result.segments:
            if not self._should_presplit_segment_before_alignment(segment):
                updated_segments.append(segment)
                continue

            child_segments = self._build_semantic_split_children(
                segment=segment,
                next_segment_id=next_segment_id,
            )
            if child_segments is None:
                updated_segments.append(segment)
                continue
            if not post_tts_budget_tracker.try_consume_for_segment(segment, len(child_segments)):
                updated_segments.append(segment)
                continue
            post_tts_budget_tracker.register_child_segments(
                parent_segment=segment,
                child_segments=child_segments,
            )

            print(
                f"[S4] Pre-splitting long overshoot segment_{segment.segment_id:03d} "
                f"-> {len(child_segments)} sub-segments."
            )
            _generate_tts_all_with_bucket(
                tts_generator,
                child_segments,
                str(tts_dir),
                usage_bucket=TTS_BUCKET_POST_TTS_RESYNTH,
            )
            updated_segments.extend(child_segments)
            next_segment_id = max(child.segment_id for child in child_segments) + 1
            presplit_count += 1

        if presplit_count > 0:
            translation_result.segments = updated_segments
            translation_result.total_segments = len(updated_segments)
        return presplit_count

    def _should_presplit_segment_before_alignment(self, segment: DubbingSegment) -> bool:
        if is_keep_original_dubbing_mode(getattr(segment, "dubbing_mode", DUBBING_MODE_DUB)):
            return False
        target_duration_ms = int(segment.target_duration_ms)
        if target_duration_ms <= 0:
            return False

        actual_duration_ms = int(segment.actual_duration_ms)
        if actual_duration_ms <= 0:
            tts_audio_path = Path(str(segment.tts_audio_path or "")).resolve(strict=False)
            if not tts_audio_path.exists():
                return False
            actual_duration_ms = _ffprobe_duration_ms(tts_audio_path)
            segment.actual_duration_ms = actual_duration_ms
            segment.alignment_ratio = actual_duration_ms / target_duration_ms

        overshoot_ratio = (actual_duration_ms - target_duration_ms) / target_duration_ms
        if (
            target_duration_ms >= FAILED_SEGMENT_SEMANTIC_SPLIT_MIN_TARGET_MS
            and overshoot_ratio >= PRE_ALIGNMENT_SEMANTIC_SPLIT_OVERSHOOT_RATIO
        ):
            return True
        if (
            target_duration_ms >= SEVERE_PRE_ALIGNMENT_SEMANTIC_SPLIT_MIN_TARGET_MS
            and overshoot_ratio >= SEVERE_PRE_ALIGNMENT_SEMANTIC_SPLIT_OVERSHOOT_RATIO
        ):
            return True
        return False

    def _attempt_semantic_split_repair(
        self,
        *,
        segment: DubbingSegment,
        next_segment_id: int,
        tts_generator: TTSGenerator,
        rewriter: GeminiRewriter,
        tts_dir: Path,
        post_tts_budget_tracker: PostTTSBudgetTracker,
    ) -> list[DubbingSegment] | None:
        if segment.alignment_method != "force_dsp" or not segment.needs_review:
            return None
        if int(segment.target_duration_ms) < FAILED_SEGMENT_SEMANTIC_SPLIT_MIN_TARGET_MS:
            return None

        child_segments = self._build_semantic_split_children(
            segment=segment,
            next_segment_id=next_segment_id,
        )
        if child_segments is None:
            return None
        if not post_tts_budget_tracker.try_consume_for_segment(segment, len(child_segments)):
            return None
        post_tts_budget_tracker.register_child_segments(
            parent_segment=segment,
            child_segments=child_segments,
        )

        print(
            f"[S5] Attempting semantic split repair for segment_{segment.segment_id:03d} "
            f"-> {len(child_segments)} sub-segments."
        )
        _generate_tts_all_with_bucket(
            tts_generator,
            child_segments,
            str(tts_dir),
            usage_bucket=TTS_BUCKET_POST_TTS_RESYNTH,
        )
        SegmentAligner(
            rewriter=rewriter,
            tts_generator=tts_generator,
            post_tts_budget_tracker=post_tts_budget_tracker,
        ).align_all(
            child_segments,
            str(tts_dir),
        )
        failed_children = [child for child in child_segments if child.needs_review]
        if not failed_children:
            print(f"[S5] Semantic split repair succeeded for segment_{segment.segment_id:03d}.")
            return child_segments

        if len(failed_children) == 1:
            failed_child = failed_children[0]
            print(
                f"[S5] Semantic split left one unresolved child "
                f"(segment_{failed_child.segment_id:03d}); retrying child-only rewrite."
            )
            self._retry_failed_semantic_child(
                child_segment=failed_child,
                tts_generator=tts_generator,
                rewriter=rewriter,
                tts_dir=tts_dir,
                post_tts_budget_tracker=post_tts_budget_tracker,
            )
            if failed_child.needs_review:
                print(
                    f"[S5] Child-only rewrite still exceeded target; keeping split result "
                    f"and force_dsp on segment_{failed_child.segment_id:03d}."
                )
            else:
                print(
                    f"[S5] Child-only rewrite resolved segment_{failed_child.segment_id:03d}; "
                    f"keeping split result."
                )
            return child_segments

        print(f"[S5] Semantic split repair did not fully resolve segment_{segment.segment_id:03d}.")
        return None

    def _build_semantic_split_children(
        self,
        *,
        segment: DubbingSegment,
        next_segment_id: int,
    ) -> list[DubbingSegment] | None:
        cn_text = segment.cn_text.strip()
        if not cn_text:
            return None

        # Script-aware split: cn_text carries the TARGET language, source_text the
        # SOURCE language. Pick each split pattern by its script family so an
        # English target splits on ASCII '.' (not full-width 。) and a Chinese
        # source splits on full-width punctuation. Default pair (target CJK /
        # source Latin) resolves to the two legacy constants → byte-identical.
        # Descriptors come from the profile (SoT), robust to direct helper calls.
        _split_profile = getattr(self, "_language_profile", DEFAULT_LANGUAGE_PAIR_PROFILE)
        _tgt_desc = get_language_descriptor(_split_profile.target_language)
        _src_desc = get_language_descriptor(_split_profile.source_language)
        _target_pattern = _FAILED_SEGMENT_SPLIT_PATTERN_BY_SCRIPT.get(
            _tgt_desc.script_family if _tgt_desc is not None else SCRIPT_CJK,
            FAILED_SEGMENT_SEMANTIC_SPLIT_PATTERN,
        )
        _source_pattern = _FAILED_SEGMENT_SPLIT_PATTERN_BY_SCRIPT.get(
            _src_desc.script_family if _src_desc is not None else SCRIPT_LATIN,
            FAILED_SEGMENT_SOURCE_SPLIT_PATTERN,
        )
        # Re-join separator for the TARGET split: a Latin target needs a space
        # between sentences ("First. Second."), a CJK target must NOT add one
        # ("句一。句二。"). Default pair (CJK target) → "" → byte-identical. The
        # source split keeps the legacy "" joiner so the default (English) source
        # chunks are unchanged; a zh source is CJK and also wants "".
        _target_joiner = " " if (_tgt_desc is not None and _tgt_desc.script_family == SCRIPT_LATIN) else ""

        cn_chunks = self._split_text_for_failed_segment(cn_text, _target_pattern, _target_joiner)
        if cn_chunks is None:
            return None

        source_chunks = self._split_text_for_failed_segment(
            segment.source_text,
            _source_pattern,
        )
        if source_chunks is None or len(source_chunks) != len(cn_chunks):
            source_chunks = [segment.source_text for _ in cn_chunks]

        spans = self._allocate_semantic_split_spans(
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            weights=[self._semantic_split_weight(chunk) for chunk in cn_chunks],
        )
        if spans is None or len(spans) != len(cn_chunks):
            return None

        child_segments: list[DubbingSegment] = []
        for index, ((start_ms, end_ms), cn_chunk) in enumerate(zip(spans, cn_chunks)):
            if end_ms <= start_ms:
                return None
            child = DubbingSegment(
                    segment_id=next_segment_id + index,
                    speaker_id=segment.speaker_id,
                    display_name=segment.display_name,
                    voice_id=segment.voice_id,
                    voice_description=getattr(segment, "voice_description", ""),
                    gender=getattr(segment, "gender", ""),
                    age_group=getattr(segment, "age_group", ""),
                    persona_style=getattr(segment, "persona_style", ""),
                    energy_level=getattr(segment, "energy_level", ""),
                    start_ms=start_ms,
                    end_ms=end_ms,
                    target_duration_ms=end_ms - start_ms,
                    source_text=source_chunks[index],
                    cn_text=cn_chunk,
                )
            # Inherit per-speaker TTS provider from parent
            if getattr(segment, "tts_provider", None):
                child.tts_provider = segment.tts_provider
            child_segments.append(child)
        return child_segments

    def _retry_failed_semantic_child(
        self,
        *,
        child_segment: DubbingSegment,
        tts_generator: TTSGenerator,
        rewriter: GeminiRewriter,
        tts_dir: Path,
        post_tts_budget_tracker: PostTTSBudgetTracker,
    ) -> None:
        tts_audio_path = Path(str(child_segment.tts_audio_path or "")).resolve(strict=False)
        if not tts_audio_path.exists():
            return

        current_actual_duration_ms = _ffprobe_duration_ms(tts_audio_path)
        child_segment.actual_duration_ms = current_actual_duration_ms
        if child_segment.target_duration_ms > 0:
            child_segment.alignment_ratio = current_actual_duration_ms / child_segment.target_duration_ms
        else:
            child_segment.alignment_ratio = 0.0

        current_text = child_segment.cn_text.strip()
        rewritten_text = rewriter.rewrite_for_duration(
            current_text,
            actual_duration_ms=current_actual_duration_ms,
            target_duration_ms=child_segment.target_duration_ms,
            source_text=child_segment.source_text,
            speaker_id=child_segment.speaker_id,
        ).strip()
        if rewritten_text and rewritten_text != current_text:
            if not post_tts_budget_tracker.try_consume_for_segment(child_segment, 1):
                return
            child_segment.cn_text = rewritten_text
            child_segment.rewrite_count += 1
            tts_result = _generate_tts_one_with_bucket(
                tts_generator,
                child_segment,
                str(tts_dir),
                usage_bucket=TTS_BUCKET_POST_TTS_RESYNTH,
            )
            child_segment.tts_audio_path = tts_result.audio_path
            child_segment.actual_duration_ms = tts_result.duration_ms
            if getattr(tts_result, "selected_voice", ""):
                child_segment.selected_voice = tts_result.selected_voice
            if getattr(tts_result, "match_confidence", ""):
                child_segment.match_confidence = tts_result.match_confidence
            child_segment.fallback_used_provider = getattr(
                tts_result,
                "fallback_used_provider",
                None,
            )
            child_segment.tts_input_cn_text = child_segment.cn_text.strip()

            refreshed_tts_path = Path(str(child_segment.tts_audio_path or "")).resolve(strict=False)
            if refreshed_tts_path.exists():
                refreshed_duration_ms = _ffprobe_duration_ms(refreshed_tts_path)
                child_segment.actual_duration_ms = refreshed_duration_ms
                if child_segment.target_duration_ms > 0:
                    child_segment.alignment_ratio = (
                        refreshed_duration_ms / child_segment.target_duration_ms
                    )
                else:
                    child_segment.alignment_ratio = 0.0

        SegmentAligner(
            rewriter=rewriter,
            tts_generator=tts_generator,
            max_rewrites=0,
            post_tts_budget_tracker=post_tts_budget_tracker,
        ).align_all(
            [child_segment],
            str(tts_dir),
        )

    @staticmethod
    def _annotate_short_segment_merge_candidates(
        segments: list[DubbingSegment],
    ) -> dict[str, int]:
        """Mark safe same-speaker short-block merge candidates.

        This only writes audit metadata. The pipeline still emits one TTS/audio
        unit per SemanticBlock until the candidate distribution is validated.
        Cross-speaker adjacency is explicitly blocked.
        """
        candidate_count = 0
        blocked_cross_speaker_count = 0

        for segment in segments:
            if getattr(segment, "short_merge_applied", False):
                segment.short_merge_candidate = False
                segment.short_merge_target_segment_id = 0
                segment.short_merge_blocked_reason = ""
                continue
            segment.short_merge_candidate = False
            segment.short_merge_target_segment_id = 0
            segment.short_merge_reason = ""
            segment.short_merge_blocked_reason = ""

        for index, segment in enumerate(segments):
            if getattr(segment, "short_merge_applied", False):
                continue
            if is_keep_original_dubbing_mode(getattr(segment, "dubbing_mode", DUBBING_MODE_DUB)):
                continue
            target_duration_ms = int(getattr(segment, "target_duration_ms", 0) or 0)
            if target_duration_ms <= 0 or target_duration_ms > SHORT_MERGE_CANDIDATE_MAX_TARGET_MS:
                continue
            spoken_chars = count_spoken_chars(getattr(segment, "cn_text", "") or "")
            if spoken_chars > SHORT_MERGE_CANDIDATE_MAX_SPOKEN_CHARS:
                continue

            candidates: list[tuple[int, int, str, DubbingSegment]] = []
            adjacent_cross_speaker = False

            if index > 0:
                prev_segment = segments[index - 1]
                prev_gap_ms = int(segment.start_ms) - int(prev_segment.end_ms)
                if 0 <= prev_gap_ms <= SHORT_MERGE_MAX_GAP_MS:
                    if is_keep_original_dubbing_mode(getattr(prev_segment, "dubbing_mode", DUBBING_MODE_DUB)):
                        pass
                    elif prev_segment.speaker_id == segment.speaker_id:
                        combined_ms = target_duration_ms + int(prev_segment.target_duration_ms)
                        if combined_ms <= SHORT_MERGE_MAX_COMBINED_TARGET_MS:
                            candidates.append((prev_gap_ms, 0, "same_speaker_prev", prev_segment))
                    else:
                        adjacent_cross_speaker = True

            if index + 1 < len(segments):
                next_segment = segments[index + 1]
                next_gap_ms = int(next_segment.start_ms) - int(segment.end_ms)
                if 0 <= next_gap_ms <= SHORT_MERGE_MAX_GAP_MS:
                    if is_keep_original_dubbing_mode(getattr(next_segment, "dubbing_mode", DUBBING_MODE_DUB)):
                        pass
                    elif next_segment.speaker_id == segment.speaker_id:
                        combined_ms = target_duration_ms + int(next_segment.target_duration_ms)
                        if combined_ms <= SHORT_MERGE_MAX_COMBINED_TARGET_MS:
                            candidates.append((next_gap_ms, 1, "same_speaker_next", next_segment))
                    else:
                        adjacent_cross_speaker = True

            if candidates:
                _gap_ms, _tie_breaker, reason, target_segment = min(candidates)
                segment.short_merge_candidate = True
                segment.short_merge_target_segment_id = int(target_segment.segment_id)
                segment.short_merge_reason = reason
                candidate_count += 1
            elif adjacent_cross_speaker:
                segment.short_merge_blocked_reason = "cross_speaker_adjacent"
                blocked_cross_speaker_count += 1

        return {
            "candidate_count": candidate_count,
            "blocked_cross_speaker_count": blocked_cross_speaker_count,
        }

    @staticmethod
    def _is_short_segment_merge_source(segment: DubbingSegment) -> bool:
        if is_keep_original_dubbing_mode(getattr(segment, "dubbing_mode", DUBBING_MODE_DUB)):
            return False
        target_duration_ms = int(getattr(segment, "target_duration_ms", 0) or 0)
        if target_duration_ms <= 0 or target_duration_ms > SHORT_MERGE_CANDIDATE_MAX_TARGET_MS:
            return False
        return (
            count_spoken_chars(getattr(segment, "cn_text", "") or "")
            <= SHORT_MERGE_CANDIDATE_MAX_SPOKEN_CHARS
        )

    @staticmethod
    def _short_merge_gap_ms(left: DubbingSegment, right: DubbingSegment) -> int:
        return int(getattr(right, "start_ms", 0) or 0) - int(getattr(left, "end_ms", 0) or 0)

    @staticmethod
    def _can_short_merge_adjacent(left: DubbingSegment, right: DubbingSegment) -> bool:
        if (
            is_keep_original_dubbing_mode(getattr(left, "dubbing_mode", DUBBING_MODE_DUB))
            or is_keep_original_dubbing_mode(getattr(right, "dubbing_mode", DUBBING_MODE_DUB))
        ):
            return False
        if left.speaker_id != right.speaker_id:
            return False
        gap_ms = ProcessPipeline._short_merge_gap_ms(left, right)
        return 0 <= gap_ms <= SHORT_MERGE_MAX_GAP_MS

    @staticmethod
    def _short_merge_group_span_ms(group: list[DubbingSegment]) -> int:
        if not group:
            return 0
        return max(0, int(group[-1].end_ms) - int(group[0].start_ms))

    @staticmethod
    def _can_add_to_short_merge_group(
        group: list[DubbingSegment],
        segment: DubbingSegment,
    ) -> bool:
        if not group:
            return False
        if not ProcessPipeline._can_short_merge_adjacent(group[-1], segment):
            return False
        span_ms = max(0, int(segment.end_ms) - int(group[0].start_ms))
        return span_ms <= SHORT_MERGE_MAX_COMBINED_TARGET_MS

    @staticmethod
    def _join_short_merge_texts(values: list[str]) -> str:
        return " ".join(value.strip() for value in values if value and value.strip())

    @staticmethod
    def _parse_short_merge_absorbed_segment_ids(segment: object) -> list[int]:
        raw = str(getattr(segment, "short_merge_absorbed_segment_ids", "") or "")
        result: list[int] = []
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                result.append(int(item))
            except ValueError:
                continue
        return result

    @staticmethod
    def _short_merge_original_segment_ids(segment: DubbingSegment) -> list[int]:
        ids = [int(segment.segment_id)]
        ids.extend(ProcessPipeline._parse_short_merge_absorbed_segment_ids(segment))
        return sorted(dict.fromkeys(ids))

    @staticmethod
    def _materialize_short_merge_group(group: list[DubbingSegment]) -> DubbingSegment:
        if len(group) == 1:
            return group[0]

        base = group[0]
        if ProcessPipeline._is_short_segment_merge_source(base):
            for candidate in group[1:]:
                if not ProcessPipeline._is_short_segment_merge_source(candidate):
                    base = candidate
                    break

        ordered_ids = [int(segment.segment_id) for segment in group]
        absorbed_ids = [sid for sid in ordered_ids if sid != int(base.segment_id)]
        base.source_text = ProcessPipeline._join_short_merge_texts(
            [segment.source_text for segment in group]
        )
        base.cn_text = ProcessPipeline._join_short_merge_texts(
            [segment.cn_text for segment in group]
        )
        # 2026-05-04 P0b — short_merge collapses N segments into one base.
        # Join tts_input_cn_text in parallel with cn_text so the merged
        # base's drift state matches the input ground truth: any
        # constituent segment in drift propagates to the merged base.
        # (Segments with empty tts_input_cn_text fall back to cn_text via
        # the dataclass-default backfill applied at load — by the time
        # we reach short_merge, both fields are non-empty for all members
        # that were in sync, so the join produces the right text either
        # way.)
        base.tts_input_cn_text = ProcessPipeline._join_short_merge_texts(
            [
                segment.tts_input_cn_text or segment.cn_text
                for segment in group
            ]
        )
        base.start_ms = int(group[0].start_ms)
        base.end_ms = int(group[-1].end_ms)
        base.target_duration_ms = ProcessPipeline._short_merge_group_span_ms(group)
        base.tts_audio_path = None
        base.aligned_audio_path = None
        base.actual_duration_ms = 0
        base.alignment_ratio = 0.0
        base.alignment_method = ""
        base.needs_review = False
        base.fallback_used_provider = None
        base.pre_tts_rewrite_direction = ""
        base.pre_tts_estimate_ms = 0
        base.pre_tts_target_ms = 0
        base.pre_tts_pre_chars = 0
        base.pre_tts_post_chars = 0
        base.pre_tts_post_tts_first_pass_ms = 0
        base.pre_tts_contradiction = False
        base.pre_tts_harmful_contradiction = False
        base.pre_tts_rewrite_task = ""
        base.pre_tts_rewrite_retry_attempted = False
        base.pre_tts_rewrite_retry_accepted = False
        base.pre_tts_rewrite_initial_rejected_reason = ""
        base.pre_tts_rewrite_rejected = False
        base.pre_tts_rewrite_rejected_reason = ""
        base.pre_tts_rewrite_rejected_direction = ""
        base.pre_tts_rewrite_rejected_estimate_ms = 0
        base.pre_tts_rewrite_rejected_target_ms = 0
        base.pre_tts_rewrite_rejected_pre_chars = 0
        base.pre_tts_rewrite_rejected_post_chars = 0
        base.pre_tts_rewrite_rejected_lower_chars = 0
        base.pre_tts_rewrite_rejected_upper_chars = 0
        base.first_pass_duration_ms = 0
        base.first_pass_error_pct = 0.0
        base.dsp_speed_param = 1.0
        base.force_dsp_severity = ""
        base.force_dsp_review_suppressed = False
        base.force_dsp_review_reason = ""
        base.dsp_speed_ratio_used = 1.0
        base.dsp_silence_padded_ms = 0
        base.dsp_truncated_ms = 0
        base.dsp_initial_duration_ms = 0
        base.dsp_trimmed_duration_ms = 0
        base.dsp_stretched_duration_ms = 0
        base.short_merge_candidate = False
        base.short_merge_target_segment_id = 0
        base.short_merge_reason = "same_speaker_adjacent"
        base.short_merge_blocked_reason = ""
        base.short_merge_applied = True
        base.short_merge_absorbed_segment_ids = ",".join(str(sid) for sid in absorbed_ids)
        base.auto_keep_original_reason = ""
        base.auto_keep_original_source = ""
        base.short_content_compact_attempted = False
        base.short_content_compact_accepted = False
        base.short_content_compact_rejected_reason = ""
        base.short_content_compact_class = ""
        base.short_content_compact_lower_chars = 0
        base.short_content_compact_upper_chars = 0
        base.short_content_compact_pre_chars = 0
        base.short_content_compact_post_chars = 0
        return base

    def _apply_short_segment_merges_before_tts(
        self,
        translation_result: TranslationResult,
    ) -> dict[str, int]:
        segments = list(translation_result.segments)
        summary = self._annotate_short_segment_merge_candidates(segments)
        if len(segments) < 2:
            summary["applied_count"] = 0
            summary["absorbed_count"] = 0
            return summary

        groups: list[list[DubbingSegment]] = []
        index = 0
        while index < len(segments):
            segment = segments[index]
            if (
                self._is_short_segment_merge_source(segment)
                and groups
                and self._can_add_to_short_merge_group(groups[-1], segment)
            ):
                groups[-1].append(segment)
                index += 1
                continue

            if (
                self._is_short_segment_merge_source(segment)
                and index + 1 < len(segments)
                and self._can_short_merge_adjacent(segment, segments[index + 1])
                and max(0, int(segments[index + 1].end_ms) - int(segment.start_ms))
                <= SHORT_MERGE_MAX_COMBINED_TARGET_MS
            ):
                groups.append([segment, segments[index + 1]])
                index += 2
                continue

            groups.append([segment])
            index += 1

        merged_segments: list[DubbingSegment] = []
        absorbed_count = 0
        for group in groups:
            merged = self._materialize_short_merge_group(group)
            merged_segments.append(merged)
            if len(group) > 1:
                absorbed_count += len(group) - 1

        applied_count = sum(
            1 for segment in merged_segments if getattr(segment, "short_merge_applied", False)
        )
        if absorbed_count:
            translation_result.segments = merged_segments
            translation_result.total_segments = len(merged_segments)
            summary["candidate_count"] = int(summary.get("candidate_count", 0) or 0)
        summary["applied_count"] = applied_count
        summary["absorbed_count"] = absorbed_count
        return summary

    @staticmethod
    def _clear_short_merge_tts_cache(
        segments: list[DubbingSegment],
        tts_dir: Path,
    ) -> int:
        cleared = 0
        for segment in segments:
            if not getattr(segment, "short_merge_applied", False):
                continue
            expected_path = tts_dir / f"segment_{segment.segment_id:03d}_{segment.speaker_id}.wav"
            if not expected_path.exists():
                continue
            try:
                expected_path.unlink()
                cleared += 1
            except OSError as exc:
                print(
                    f"[S4] Warning: failed to clear stale short-merge TTS cache "
                    f"{expected_path}: {exc}",
                    flush=True,
                )
        return cleared

    @staticmethod
    def _clear_pre_tts_rewrite_audio_cache(
        segments: list[DubbingSegment],
        tts_dir: Path,
    ) -> int:
        """Invalidate path-based audio checkpoints after pre-TTS text rewrite."""
        cleared = 0
        for segment in segments:
            if not getattr(segment, "pre_tts_rewrite_direction", ""):
                continue
            raw_path = tts_dir / f"segment_{segment.segment_id:03d}_{segment.speaker_id}.wav"
            aligned_path = tts_dir / f"segment_{segment.segment_id:03d}_aligned.wav"
            candidates = [raw_path, aligned_path]
            candidates.extend(tts_dir.glob(f"segment_{segment.segment_id:03d}_aligned.wav.*"))
            for candidate in candidates:
                if not candidate.exists():
                    continue
                try:
                    candidate.unlink()
                    cleared += 1
                except OSError as exc:
                    print(
                        f"[S4] Warning: failed to clear stale pre-rewrite audio cache "
                        f"{candidate}: {exc}",
                        flush=True,
                    )
            segment.tts_audio_path = None
            segment.aligned_audio_path = None
            segment.actual_duration_ms = 0
            segment.alignment_ratio = 0.0
            segment.alignment_method = ""
        return cleared

    def _build_aligned_segments(self, segments: list[DubbingSegment]) -> list[AlignedSegment]:
        aligned_segments: list[AlignedSegment] = []
        for segment in segments:
            aligned_segments.append(
                AlignedSegment(
                    segment_id=segment.segment_id,
                    speaker_id=segment.speaker_id,
                    display_name=segment.display_name,
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    cn_text=segment.cn_text,
                    en_text=getattr(segment, "en_text", ""),
                    aligned_audio_path=str(segment.aligned_audio_path or ""),
                    actual_duration_ms=int(segment.actual_duration_ms),
                    alignment_method=segment.alignment_method,
                    needs_review=segment.needs_review,
                    dubbing_mode=normalize_dubbing_mode(
                        getattr(segment, "dubbing_mode", DUBBING_MODE_DUB)
                    ),
                )
            )
        return aligned_segments

    def _build_process_workflow_build_result(
        self,
        *,
        project_dir: Path,
        youtube_url: str,
        download_result: DownloadResult,
        video_path: Path,
        source_audio_path: Path,
        separated_audio: AudioSeparationResult,
        transcript_result: TranscriptResult,
        translation_result: TranslationResult,
        total_duration_ms: int,
        segments: list[DubbingSegment],
        stage_snapshot: dict[str, object],
        source_type: str = "youtube_url",
    ) -> WorkflowBuildResult:
        artifact_index = self._build_process_artifact_index(
            project_dir=project_dir,
            video_path=video_path,
            source_audio_path=source_audio_path,
            separated_audio=separated_audio,
            transcript_result=transcript_result,
            translation_result=translation_result,
        )
        return self.project_builder.build_result(
            project_id=project_dir.name,
            source_info=self._build_process_source_info(
                project_dir=project_dir,
                youtube_url=youtube_url,
                download_result=download_result,
                total_duration_ms=total_duration_ms,
                source_type=source_type,
            ),
            artifact_index=artifact_index,
            stage_snapshot=stage_snapshot,
            stage_outputs=self._build_process_stage_outputs(segments),
        )

    def _dispatch_process_output_bundle(
        self,
        *,
        project_dir: Path,
        build_result: WorkflowBuildResult,
        watermark_text: str | None = None,
    ) -> OutputBundleResult:
        return OutputDispatcher().dispatch(
            build_result.localized_project,
            build_result.artifact_index,
            OutputRequest(
                # PUBLISH target also runs EDITOR (dubbed audio is needed for muxing).
                # Pipeline always produces the final video (原视频画面 + 配音 + 背景音).
                targets=[OutputTarget.PUBLISH],
                output_dir=str(project_dir.resolve(strict=False)),
                # Phase 2a Task 8 (gate #8): free jobs carry a watermark text; the
                # γ resume_publish_only call site leaves this None (Studio, clean).
                watermark_text=watermark_text,
            ),
        )

    def _build_process_artifact_index(
        self,
        *,
        project_dir: Path,
        video_path: Path,
        source_audio_path: Path,
        separated_audio: AudioSeparationResult,
        transcript_result: TranscriptResult,
        translation_result: TranslationResult,
    ):
        metadata_path = (project_dir / "download_metadata.json").resolve(strict=False)
        content_compliance_path = (project_dir / DEFAULT_REPORT_RELATIVE_PATH).resolve(strict=False)
        review_state_path = (project_dir / "review_state.json").resolve(strict=False)
        project_state_path = (project_dir / "project_state.json").resolve(strict=False)
        artifact_entries = build_core_media_artifact_entries(
            source_original_video=video_path.resolve(strict=False) if video_path.exists() else None,
            source_original_audio=(
                source_audio_path.resolve(strict=False) if source_audio_path.exists() else None
            ),
            working_speech_for_asr=Path(separated_audio.speech_audio_path).resolve(strict=False),
            working_ambient_audio=Path(separated_audio.ambient_audio_path).resolve(strict=False),
            media_transcript_raw=transcript_result.raw_response_path,
            media_transcript_structured=transcript_result.structured_transcript_path,
            translation_segments=translation_result.output_path,
        )
        if metadata_path.exists():
            artifact_entries.append(("source.download_metadata", metadata_path))
        if content_compliance_path.exists():
            artifact_entries.append(("state.content_compliance", content_compliance_path))
        if review_state_path.exists():
            artifact_entries.append(("state.review", review_state_path))
        if project_state_path.exists():
            artifact_entries.append(("state.project", project_state_path))
        return self.project_builder.build_artifact_index(artifact_entries)

    def _build_process_source_info(
        self,
        *,
        project_dir: Path,
        youtube_url: str,
        download_result: DownloadResult,
        total_duration_ms: int,
        source_type: str = "youtube_url",
    ) -> dict[str, object]:
        # Use real video/audio path from download_result for local sources
        if source_type in ("local_video", "local_audio"):
            source_path = download_result.video_path if source_type == "local_video" else download_result.audio_path
        else:
            source_path = str((project_dir / "video" / "original.mp4").resolve(strict=False))
        return build_canonical_source_info(
            source_kind=source_type,
            locator=download_result.url,
            source_path=source_path,
            metadata={
                "video_title": download_result.video_title,
                "duration_ms": total_duration_ms,
                "description": download_result.description,
            },
        )

    def _build_process_stage_outputs(self, segments: list[DubbingSegment]) -> dict[str, object]:
        aligned_blocks = self._build_process_output_blocks(segments)
        return {
            "semantic_blocks": list(aligned_blocks),
            "aligned_blocks": aligned_blocks,
            "captions": self._build_process_output_captions(segments),
        }

    def _build_process_output_captions(self, segments: list[DubbingSegment]) -> list[SubtitleLine]:
        captions: list[SubtitleLine] = []
        for segment in segments:
            captions.append(
                SubtitleLine(
                    index=int(segment.segment_id),
                    start_ms=int(segment.start_ms),
                    end_ms=int(segment.end_ms),
                    speaker_id=segment.speaker_id,
                    speaker_name=segment.display_name,
                    en_text=segment.source_text,
                    cn_text=segment.cn_text,
                )
            )
        return captions

    def _build_process_output_blocks(
        self,
        segments: list[DubbingSegment],
    ) -> list[SemanticBlock]:
        blocks: list[SemanticBlock] = []
        for segment in segments:
            original_srt_indices = self._short_merge_original_segment_ids(segment)
            blocks.append(
                SemanticBlock(
                    block_id=f"segment_{int(segment.segment_id):03d}",
                    speaker_id=segment.speaker_id,
                    speaker_name=segment.display_name,
                    original_srt_indices=original_srt_indices,
                    first_start_ms=int(segment.start_ms),
                    last_end_ms=int(segment.end_ms),
                    target_duration_ms=int(segment.target_duration_ms),
                    merged_cn_text=segment.cn_text,
                    # 2026-05-04 P0b — propagate the audio's source-of-truth
                    # text to the block. Empty-after-backfill defaults to
                    # cn_text (treat as in-sync). Cue pipeline compares
                    # this against merged_cn_text for drift detection.
                    tts_input_cn_text=(
                        segment.tts_input_cn_text or segment.cn_text
                    ),
                    actual_audio_duration_ms=int(segment.actual_duration_ms),
                    rewrite_count=int(segment.rewrite_count),
                    tts_audio_path=_normalize_optional_text(segment.tts_audio_path),
                    aligned_audio_path=_normalize_optional_text(segment.aligned_audio_path),
                    status=self._resolve_process_output_block_status(segment),
                    alignment_method=segment.alignment_method or "direct",
                    needs_review=bool(segment.needs_review),
                    dubbing_mode=normalize_dubbing_mode(
                        getattr(segment, "dubbing_mode", DUBBING_MODE_DUB)
                    ),
                )
            )
        return blocks

    def _build_ingestion_stage_payload(
        self,
        *,
        final_project_dir: Path,
        download_result: DownloadResult,
        video_path: Path,
        source_audio_path: Path,
        execution_mode: str,
        source_type: str = "youtube_url",
    ) -> dict[str, object]:
        metadata_path = final_project_dir / "download_metadata.json"
        return {
            "execution_mode": execution_mode,
            "source_kind": source_type,
            "locator": download_result.url,
            "title": download_result.video_title,
            "duration_ms": int(download_result.duration_ms),
            "artifacts": build_artifacts_payload(
                kind="ingestion_assets",
                file_paths=[
                    str(video_path.resolve(strict=False)) if video_path.exists() else None,
                    str(source_audio_path.resolve(strict=False)) if source_audio_path.exists() else None,
                    str(metadata_path.resolve(strict=False)) if metadata_path.exists() else None,
                ],
            ),
        }

    def _build_audio_preparation_stage_payload(
        self,
        *,
        source_audio_path: Path,
        separated_audio: AudioSeparationResult,
    ) -> dict[str, object]:
        execution_mode = "cache_restore_full" if separated_audio.reused_cache else "fresh_prepare"
        return {
            "execution_mode": execution_mode,
            "source_audio_path": str(source_audio_path.resolve(strict=False)),
            "speech_audio_path": str(Path(separated_audio.speech_audio_path).resolve(strict=False)),
            "ambient_audio_path": str(Path(separated_audio.ambient_audio_path).resolve(strict=False)),
            "artifacts": build_artifacts_payload(
                kind="prepared_audio",
                file_paths=[
                    str(source_audio_path.resolve(strict=False)),
                    str(Path(separated_audio.speech_audio_path).resolve(strict=False)),
                    str(Path(separated_audio.ambient_audio_path).resolve(strict=False)),
                ],
            ),
        }

    def _run_content_compliance_review(
        self,
        *,
        final_project_dir: Path,
        transcript_result: TranscriptResult,
        download_result: DownloadResult,
        source_type: str,
        source_ref: str,
        llm_generate_json: Callable[[str], str] | None = None,
        llm_model_name: str | None = None,
        admin_override: bool = False,
        job_id: str | None = None,
        user_id: str | None = None,
        display_name: str | None = None,
        anonymous_strict: bool = False,
    ) -> dict[str, object]:
        if anonymous_strict:
            # APF P0 T5（AD-2 v2）：sentinel 匿名 job 不享受 admin override。
            admin_override = False
        if not is_content_compliance_enabled():
            if anonymous_strict:
                # C2 skip 路径 1：总开关关闭对匿名 lane 必须 fail-closed。
                print("[S2] 匿名预览：内容合规总开关关闭 → fail-closed 拒绝。")
                raise ContentPolicyViolationError(
                    _anonymous_compliance_fail_closed_result(
                        reason_code="compliance_disabled",
                        source_type=source_type,
                        source_ref=source_ref,
                        video_title=str(download_result.video_title or ""),
                    )
                )
            print("[S2] 内容合规审核已关闭，跳过。")
            return {
                "status": "skipped",
                "message": "内容合规审核已关闭。",
            }

        if anonymous_strict:
            # 近空转录（F7）：有音轨但转录内容过少 → 软拦截；匿名无人工
            # 复审通道，软拦截即 blocked。
            _transcript_chars = sum(
                len(str(getattr(line, "source_text", "") or "").strip())
                for line in transcript_result.lines
            )
            if _transcript_chars < ANONYMOUS_PREVIEW_MIN_TRANSCRIPT_CHARS:
                print("[S2] 匿名预览：转录内容近空 → fail-closed 软拦截。")
                raise ContentPolicyViolationError(
                    _anonymous_compliance_fail_closed_result(
                        reason_code="transcript_near_empty",
                        source_type=source_type,
                        source_ref=source_ref,
                        video_title=str(download_result.video_title or ""),
                    )
                )
            if llm_generate_json is None or not is_content_compliance_llm_enabled():
                # C2 skip 路径 2：LLM 第二层未启用/未配置对匿名 lane 必须
                # fail-closed（非匿名路径保持原"静默跳过第二层"行为）。
                print("[S2] 匿名预览：LLM 合规层未启用或未配置 → fail-closed 拒绝。")
                raise ContentPolicyViolationError(
                    _anonymous_compliance_fail_closed_result(
                        reason_code="llm_unavailable",
                        source_type=source_type,
                        source_ref=source_ref,
                        video_title=str(download_result.video_title or ""),
                    )
                )

        print("[S2] 审核视频内容合规性...")
        reviewer = MainlandChinaContentComplianceReviewer()
        result = reviewer.review(
            transcript_lines=transcript_result.lines,
            video_title=download_result.video_title,
            video_description=download_result.description,
            source_type=source_type,
            source_ref=source_ref,
        )
        llm_result = None
        if result.blocked:
            print("[S2] 本地规则明确命中禁忌内容，跳过大模型审核。")
        elif is_content_compliance_llm_enabled() and llm_generate_json is not None:
            print("[S2] 本地规则未命中，调用大模型进行第二层内容合规审核...")
            llm_reviewer = LLMContentComplianceReviewer(
                generate_json=llm_generate_json,
                prompt_template=load_content_compliance_prompt_template(),
                model_name=str(llm_model_name or ""),
            )
            try:
                llm_result = llm_reviewer.review(
                    transcript_lines=transcript_result.lines,
                    local_result=result,
                    video_title=download_result.video_title,
                    video_description=download_result.description,
                    source_type=source_type,
                    source_ref=source_ref,
                )
            except Exception as exc:
                llm_result = make_content_compliance_llm_error(
                    exc,
                    model_name=str(llm_model_name or ""),
                )
                print(f"[S2] 大模型内容合规审核失败：{exc}")
        else:
            print("[S2] 本地规则未命中，大模型审核未启用或未配置，跳过第二层。")

        final_result = combine_content_compliance_results(
            local_result=result,
            llm_result=llm_result,
            # APF P0 T5（C2 skip 路径 3）：匿名 lane 代码级强制 fail-closed，
            # 不读 env；登录任务沿用 env 配置。
            llm_fail_closed=True if anonymous_strict else is_content_compliance_llm_fail_closed(),
        )
        admin_override_applied = bool(admin_override and final_result.blocked)
        if admin_override_applied:
            final_result = _dc_replace(
                final_result,
                message=_content_compliance_admin_override_message(final_result),
            )
        report_path = reviewer.write_report(final_result, project_dir=final_project_dir)
        payload = final_result.to_dict()
        payload["artifact_path"] = str(report_path)
        if admin_override_applied:
            payload["admin_override"] = True
            payload["notification_dispatched"] = (
                _dispatch_content_compliance_admin_override_notification(
                    job_id=job_id,
                    user_id=user_id,
                    display_name=display_name,
                    summary=final_result.message,
                )
            )
            print(
                "[S2] 内容合规审核未通过，但当前任务属于管理员，已记录警告并继续流程。"
            )
            return payload
        if final_result.blocked:
            print(f"[S2] 内容合规审核未通过，报告：{report_path}")
            raise ContentPolicyViolationError(final_result)

        print(f"[S2] 内容合规审核通过，报告：{report_path}")
        return payload

    def _build_media_understanding_stage_payload(
        self,
        *,
        transcript_result: TranscriptResult,
        effective_speakers: int,
        execution_mode: str,
        content_compliance: dict[str, object] | None = None,
    ) -> dict[str, object]:
        speaker_ids = self._detect_speaker_ids(transcript_result.lines)
        transcript_artifacts = [
            transcript_result.raw_response_path,
            transcript_result.structured_transcript_path,
        ]
        payload: dict[str, object] = {
            "execution_mode": execution_mode,
            "line_count": len(transcript_result.lines),
            "speaker_count": max(int(effective_speakers), len(speaker_ids)),
            "speaker_ids": speaker_ids,
            "language": transcript_result.language,
            "artifacts": build_artifacts_payload(
                kind="transcript_assets",
                file_paths=transcript_artifacts,
            ),
        }
        if content_compliance is not None:
            payload["content_compliance"] = content_compliance
        return payload

    def _build_translation_stage_payload(
        self,
        *,
        translation_result: TranslationResult,
        execution_mode: str,
    ) -> dict[str, object]:
        cn_line_count = sum(1 for segment in translation_result.segments if bool(segment.cn_text.strip()))
        return {
            "execution_mode": execution_mode,
            "segment_count": translation_result.total_segments,
            "text_layer_summary": {
                "cn_line_count": cn_line_count,
            },
            "artifacts": build_artifacts_payload(
                kind="translation_segments",
                file_paths=[translation_result.output_path],
            ),
        }

    def _build_alignment_stage_payload(
        self,
        segments: list[DubbingSegment],
    ) -> dict[str, object]:
        aligned_audio_paths = [segment.aligned_audio_path for segment in segments]
        needs_review_count = sum(1 for segment in segments if segment.needs_review)
        return {
            "execution_mode": "legacy_process",
            "block_count": len(segments),
            "needs_review_count": needs_review_count,
            "cn_text_produced": any(bool(segment.cn_text.strip()) for segment in segments),
            "artifacts": build_artifacts_payload(
                kind="aligned_audio",
                file_paths=aligned_audio_paths,
            ),
        }

    def _build_legacy_process_output_stage_payload(
        self,
        *,
        output_bundle: OutputBundleResult,
    ) -> dict[str, object]:
        editor_result = output_bundle.editor_result
        manifest_path = output_bundle.manifest_path
        if editor_result is None:
            raise ValueError("Legacy process output stage requires an editor output result.")
        if not manifest_path:
            raise ValueError("Legacy process output stage requires a manifest path.")
        return {
            "execution_mode": "legacy_process_output_dispatch",
            "segment_count": int(editor_result.segment_count),
            "needs_review_count": int(editor_result.needs_review_count),
            "manifest_path": manifest_path,
            "artifacts": build_artifacts_payload(
                kind="process_output_bundle",
                file_paths=[
                    editor_result.dubbed_audio_path,
                    editor_result.ambient_audio_path,
                    editor_result.segments_dir,
                    editor_result.subtitles_path,
                    editor_result.background_sounds_path,
                    editor_result.alignment_report_path,
                    manifest_path,
                ],
            ),
        }

    @staticmethod
    def _resolve_process_output_block_status(segment: DubbingSegment) -> str:
        if segment.alignment_method in {
            "force_dsp",
            "capped_dsp_overflow",
            "capped_dsp_underflow",
        }:
            return "align_done_fallback"
        if segment.needs_review:
            return "align_review_needed"
        return "align_done"

    def _split_text_for_failed_segment(
        self, text: str, pattern: re.Pattern[str], joiner: str = ""
    ) -> list[str] | None:
        normalized_text = text.strip()
        if not normalized_text:
            return None

        pieces = [piece.strip() for piece in pattern.split(normalized_text) if piece.strip()]
        if len(pieces) < 2:
            return None

        piece_weights = [self._semantic_split_weight(piece) for piece in pieces]
        total_weight = sum(piece_weights)
        if total_weight <= 0:
            return None

        best_index: int | None = None
        best_balance_score: float | None = None
        running_weight = 0
        for index in range(len(pieces) - 1):
            running_weight += piece_weights[index]
            left_ratio = running_weight / total_weight
            right_ratio = 1.0 - left_ratio
            if left_ratio < FAILED_SEGMENT_SEMANTIC_SPLIT_MIN_RATIO:
                continue
            if right_ratio < FAILED_SEGMENT_SEMANTIC_SPLIT_MIN_RATIO:
                continue
            balance_score = abs(0.5 - left_ratio)
            if best_balance_score is None or balance_score < best_balance_score:
                best_index = index
                best_balance_score = balance_score

        if best_index is None:
            return None

        left_text = joiner.join(pieces[: best_index + 1]).strip()
        right_text = joiner.join(pieces[best_index + 1 :]).strip()
        if not left_text or not right_text:
            return None
        return [left_text, right_text]

    def _allocate_semantic_split_spans(
        self,
        *,
        start_ms: int,
        end_ms: int,
        weights: list[int],
    ) -> list[tuple[int, int]] | None:
        if len(weights) < 2:
            return None

        total_duration_ms = end_ms - start_ms
        total_weight = sum(max(weight, 1) for weight in weights)
        if total_duration_ms <= 0 or total_weight <= 0:
            return None

        spans: list[tuple[int, int]] = []
        cursor_ms = start_ms
        consumed_weight = 0
        for index, weight in enumerate(weights):
            normalized_weight = max(weight, 1)
            consumed_weight += normalized_weight
            if index == len(weights) - 1:
                next_end_ms = end_ms
            else:
                next_end_ms = start_ms + round(total_duration_ms * consumed_weight / total_weight)
            next_end_ms = min(max(next_end_ms, cursor_ms + 1), end_ms)
            spans.append((cursor_ms, next_end_ms))
            cursor_ms = next_end_ms

        if spans[-1][1] != end_ms:
            spans[-1] = (spans[-1][0], end_ms)
        return spans

    def _semantic_split_weight(self, text: str) -> int:
        normalized = re.sub(r"[\s，。,！？!?:;；、…\-—]+", "", text)
        return max(len(normalized), 1)

    def _write_transcript_result(self, transcript_result: TranscriptResult) -> None:
        transcript_path = Path(transcript_result.structured_transcript_path)
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_data = {
            "lines": [
                {
                    "index": line.index,
                    "start_ms": line.start_ms,
                    "end_ms": line.end_ms,
                    "speaker_id": line.speaker_id,
                    "speaker_label": line.speaker_label,
                    "source_text": line.source_text,
                }
                for line in transcript_result.lines
            ],
            "total_duration_ms": transcript_result.total_duration_ms,
            "language": transcript_result.language,
            "raw_response_path": transcript_result.raw_response_path,
            "structured_transcript_path": transcript_result.structured_transcript_path,
        }
        transcript_path.write_text(
            json.dumps(transcript_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _write_segments_snapshot(self, translation_result: TranslationResult) -> None:
        output_path = Path(translation_result.output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(
                {
                    "segments": [
                        {
                            "segment_id": segment.segment_id,
                            "speaker_id": segment.speaker_id,
                            "display_name": segment.display_name,
                            "voice_id": segment.voice_id,
                            "start_ms": segment.start_ms,
                            "end_ms": segment.end_ms,
                            "target_duration_ms": segment.target_duration_ms,
                            "source_text": segment.source_text,
                            "cn_text": segment.cn_text,
                            "tts_audio_path": segment.tts_audio_path,
                            "aligned_audio_path": segment.aligned_audio_path,
                            "actual_duration_ms": segment.actual_duration_ms,
                            "alignment_ratio": segment.alignment_ratio,
                            "alignment_method": segment.alignment_method,
                            "rewrite_count": segment.rewrite_count,
                            "needs_review": segment.needs_review,
                            "voice_description": segment.voice_description,
                            "gender": segment.gender,
                            "age_group": segment.age_group,
                            "persona_style": segment.persona_style,
                            "energy_level": segment.energy_level,
                            "selected_voice": segment.selected_voice,
                            "match_confidence": segment.match_confidence,
                            "tts_provider": segment.tts_provider,
                            "tts_model_key": segment.tts_model_key,
                            "requires_worker": segment.requires_worker,
                            "worker_target_model": segment.worker_target_model,
                            "first_pass_cn_text": segment.first_pass_cn_text,
                            "tts_input_cn_text": segment.tts_input_cn_text,
                            # T7: fallback provider when primary failed (None if
                            # primary succeeded). Lets users audit voice
                            # substitutions in the final manifest.
                            "fallback_used_provider": segment.fallback_used_provider,
                            "pre_tts_rewrite_direction": segment.pre_tts_rewrite_direction,
                            "pre_tts_estimate_ms": segment.pre_tts_estimate_ms,
                            "pre_tts_target_ms": segment.pre_tts_target_ms,
                            "pre_tts_pre_chars": segment.pre_tts_pre_chars,
                            "pre_tts_post_chars": segment.pre_tts_post_chars,
                            "pre_tts_rewrite_task": segment.pre_tts_rewrite_task,
                            "pre_tts_rewrite_retry_attempted": segment.pre_tts_rewrite_retry_attempted,
                            "pre_tts_rewrite_retry_accepted": segment.pre_tts_rewrite_retry_accepted,
                            "pre_tts_rewrite_initial_rejected_reason": segment.pre_tts_rewrite_initial_rejected_reason,
                            "pre_tts_rewrite_rejected": segment.pre_tts_rewrite_rejected,
                            "pre_tts_rewrite_rejected_reason": segment.pre_tts_rewrite_rejected_reason,
                            "pre_tts_rewrite_rejected_direction": segment.pre_tts_rewrite_rejected_direction,
                            "pre_tts_rewrite_rejected_estimate_ms": segment.pre_tts_rewrite_rejected_estimate_ms,
                            "pre_tts_rewrite_rejected_target_ms": segment.pre_tts_rewrite_rejected_target_ms,
                            "pre_tts_rewrite_rejected_pre_chars": segment.pre_tts_rewrite_rejected_pre_chars,
                            "pre_tts_rewrite_rejected_post_chars": segment.pre_tts_rewrite_rejected_post_chars,
                            "pre_tts_rewrite_rejected_lower_chars": segment.pre_tts_rewrite_rejected_lower_chars,
                            "pre_tts_rewrite_rejected_upper_chars": segment.pre_tts_rewrite_rejected_upper_chars,
                            "force_dsp_severity": segment.force_dsp_severity,
                            "force_dsp_review_suppressed": segment.force_dsp_review_suppressed,
                            "force_dsp_review_reason": segment.force_dsp_review_reason,
                            "dsp_speed_ratio_used": segment.dsp_speed_ratio_used,
                            "dsp_silence_padded_ms": segment.dsp_silence_padded_ms,
                            "dsp_truncated_ms": segment.dsp_truncated_ms,
                            "dsp_initial_duration_ms": segment.dsp_initial_duration_ms,
                            "dsp_trimmed_duration_ms": segment.dsp_trimmed_duration_ms,
                            "dsp_stretched_duration_ms": segment.dsp_stretched_duration_ms,
                            "short_merge_candidate": segment.short_merge_candidate,
                            "short_merge_target_segment_id": segment.short_merge_target_segment_id,
                            "short_merge_reason": segment.short_merge_reason,
                            "short_merge_blocked_reason": segment.short_merge_blocked_reason,
                            "short_merge_applied": segment.short_merge_applied,
                            "short_merge_absorbed_segment_ids": segment.short_merge_absorbed_segment_ids,
                            "speaker_role": segment.speaker_role,
                            "speaker_role_label": segment.speaker_role_label,
                            "speaker_duration_ms": segment.speaker_duration_ms,
                            "speaker_duration_share": segment.speaker_duration_share,
                            "speaker_segment_count": segment.speaker_segment_count,
                            "speaker_short_segment_count": segment.speaker_short_segment_count,
                            "speaker_short_segment_rate": segment.speaker_short_segment_rate,
                            "speaker_structure_reason": segment.speaker_structure_reason,
                            "speaker_review_hint": segment.speaker_review_hint,
                            "dubbing_mode": normalize_dubbing_mode(
                                getattr(segment, "dubbing_mode", DUBBING_MODE_DUB)
                            ),
                            "auto_keep_original_reason": segment.auto_keep_original_reason,
                            "auto_keep_original_source": segment.auto_keep_original_source,
                            "short_content_compact_attempted": segment.short_content_compact_attempted,
                            "short_content_compact_accepted": segment.short_content_compact_accepted,
                            "short_content_compact_rejected_reason": segment.short_content_compact_rejected_reason,
                            "short_content_compact_class": segment.short_content_compact_class,
                            "short_content_compact_lower_chars": segment.short_content_compact_lower_chars,
                            "short_content_compact_upper_chars": segment.short_content_compact_upper_chars,
                            "short_content_compact_pre_chars": segment.short_content_compact_pre_chars,
                            "short_content_compact_post_chars": segment.short_content_compact_post_chars,
                        }
                        for segment in translation_result.segments
                    ],
                    "total_segments": translation_result.total_segments,
                    "output_path": translation_result.output_path,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _ingest_local_source(
        self,
        *,
        source_type: str,
        source_ref: str,
        project_dir: Path,
    ) -> tuple[DownloadResult, Path, Path]:
        """Ingest a local video or audio file into the workspace.

        Returns (download_result, video_path, source_audio_path, execution_mode).
        """
        source_path = Path(source_ref).resolve(strict=False)
        if not source_path.exists():
            raise FileNotFoundError(f"本地来源文件不存在: {source_ref}")

        video_dir = project_dir / "video"
        audio_dir = project_dir / "audio"
        video_dir.mkdir(parents=True, exist_ok=True)
        audio_dir.mkdir(parents=True, exist_ok=True)

        if source_type == "local_video":
            print(f"[S0] 使用本地视频: {source_ref}")
            # Copy/link video into workspace preserving original extension
            workspace_video = video_dir / f"original{source_path.suffix or '.mp4'}"
            if not workspace_video.exists():
                shutil.copy2(str(source_path), str(workspace_video))
            video_path = workspace_video.resolve(strict=False)
            # Extract audio from video
            workspace_audio = audio_dir / "original.wav"
            if not workspace_audio.exists():
                self._extract_audio_from_video(video_path, workspace_audio)
            source_audio_path = workspace_audio.resolve(strict=False)
        else:
            # local_audio — no video file
            print(f"[S0] 使用本地音频: {source_ref}")
            workspace_audio = audio_dir / f"original{source_path.suffix or '.wav'}"
            if not workspace_audio.exists():
                shutil.copy2(str(source_path), str(workspace_audio))
            source_audio_path = workspace_audio.resolve(strict=False)
            video_path = project_dir / "video" / "original.mp4"  # placeholder, won't exist

        # Build a DownloadResult-compatible object for downstream compatibility
        title = source_path.stem or "local_source"
        duration_ms = _ffprobe_duration_ms(source_audio_path) if source_audio_path.exists() else 0
        download_result = DownloadResult(
            video_path=str(video_path),
            audio_path=str(source_audio_path),
            video_title=title,
            duration_ms=duration_ms,
            url=source_ref,
            description="",
        )

        # Write download_metadata.json for compatibility
        metadata_path = project_dir / "download_metadata.json"
        metadata_path.write_text(
            json.dumps(
                {
                    "source_type": source_type,
                    "url": source_ref,
                    "video_title": title,
                    "duration_ms": duration_ms,
                    "video_path": str(video_path),
                    "audio_path": str(source_audio_path),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        return download_result, video_path, source_audio_path, "local_ingest"

    @staticmethod
    def _extract_audio_from_video(video_path: Path, output_audio_path: Path) -> None:
        """Extract audio track from video using ffmpeg."""
        import subprocess
        cmd = [
            "ffmpeg", "-i", str(video_path),
            "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "1",
            "-y", str(output_audio_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg 音频提取失败: {result.stderr[:500]}")

    def _ensure_separated_audio_assets(
        self,
        *,
        project_dir: Path,
        source_audio_path: Path,
    ) -> AudioSeparationResult:
        preparation_result = SourceAudioPreparationService().prepare(
            SourceAudioPreparationRequest(
                project_dir=str(project_dir),
                source_audio_path=str(source_audio_path),
            )
        )
        return AudioSeparationResult(
            source_audio_path=preparation_result.source_audio_path,
            speech_audio_path=preparation_result.speech_audio_path,
            ambient_audio_path=preparation_result.ambient_audio_path,
            reused_cache=preparation_result.reused_cache,
        )

    def _refresh_download_metadata(
        self,
        *,
        final_project_dir: Path,
        video_path: Path,
        source_audio_path: Path,
        video_title: str,
        duration_ms: int,
        url: str,
        description: str,
        speech_audio_path: Path,
        ambient_audio_path: Path,
    ) -> None:
        metadata_path = final_project_dir / "download_metadata.json"
        metadata: dict[str, object] = {}
        if metadata_path.exists():
            try:
                loaded = json.loads(metadata_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    metadata = loaded
            except json.JSONDecodeError:
                metadata = {}

        metadata.update(
            {
                "video_path": str(video_path.resolve(strict=False)),
                "audio_path": str(source_audio_path.resolve(strict=False)),
                "speech_audio_path": str(speech_audio_path.resolve(strict=False)),
                "ambient_audio_path": str(ambient_audio_path.resolve(strict=False)),
                "video_title": video_title,
                "duration_ms": duration_ms,
                "url": url,
                "description": description,
            }
        )
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _resolve_project_dir(self, config: ProcessConfig, video_title: str) -> str:
        if config.project_dir is not None:
            return str(Path(config.project_dir).expanduser().resolve(strict=False))

        normalized_title = video_title.lower()
        normalized_title = re.sub(r"[^a-z0-9\s]+", " ", normalized_title)
        normalized_title = re.sub(r"\s+", "_", normalized_title).strip("_")
        slug = (normalized_title or "untitled_video")[:50].rstrip("_") or "untitled_video"
        return str((_resolve_projects_root() / slug).resolve(strict=False))

    def _load_stage_config(self, label: str, loader: Callable[[], T]) -> T:
        try:
            loaded = loader()
        except Exception as exc:
            print(f"[配置] {label} 失败：{exc}")
            raise
        print(f"[配置] {label} OK")
        return loaded

    def _calibrate_tts_duration(
        self,
        segments: list[DubbingSegment],
        *,
        min_speaker_samples: int = DEFAULT_SPEAKER_TTS_CALIBRATION_MIN_SAMPLES,
    ) -> tuple[float, dict[str, float]]:
        """Recompute chars/sec from real TTS output, normalized by dsp_speed_param.

        Phase 2 note (2026-04-15): ``segment.actual_duration_ms`` reflects
        TTS output AFTER the provider has applied whatever speech_rate /
        voice_setting.speed we asked for. Naively feeding that duration
        into the estimator treats a speed-accelerated segment as if the
        voice naturally speaks that fast — the resulting cps drifts high
        whenever any segment used speed>1.0 (low whenever speed<1.0).

        Fix: multiply duration by dsp_speed_param to recover the
        "speed=1.0 equivalent" duration before calibrating. Pre-Phase 2
        segments carry dsp_speed_param=1.0 (the dataclass default), so
        this is a no-op for the legacy path.
        """
        def _natural_duration_ms(segment: DubbingSegment) -> int:
            """Remove speech_rate/speed from actual_duration_ms.

            For MiniMax voice_setting.speed, CosyVoice speech_rate, and
            VolcEngine speech_rate (all already mapped back to a float
            multiplier before storage), duration × speed = natural time.
            """
            speed = float(getattr(segment, "dsp_speed_param", 1.0) or 1.0)
            if speed <= 0:
                speed = 1.0
            return int(round(segment.actual_duration_ms * speed))

        global_estimator = TTSDurationEstimator(chars_per_second=4.5)
        global_samples = [
            (segment.cn_text, _natural_duration_ms(segment))
            for segment in segments
            if segment.actual_duration_ms > 0
            and not is_keep_original_dubbing_mode(getattr(segment, "dubbing_mode", DUBBING_MODE_DUB))
        ]
        global_estimator.calibrate(global_samples)

        speaker_samples: dict[str, list[tuple[str, int]]] = {}
        for segment in segments:
            if is_keep_original_dubbing_mode(getattr(segment, "dubbing_mode", DUBBING_MODE_DUB)):
                continue
            if segment.actual_duration_ms <= 0:
                continue
            speaker_samples.setdefault(segment.speaker_id, []).append(
                (segment.cn_text, _natural_duration_ms(segment))
            )

        chars_per_second_by_speaker: dict[str, float] = {}
        for speaker_id, samples in speaker_samples.items():
            if len(samples) < min_speaker_samples:
                continue
            speaker_estimator = TTSDurationEstimator(chars_per_second=global_estimator.chars_per_second)
            speaker_estimator.calibrate(samples)
            chars_per_second_by_speaker[speaker_id] = speaker_estimator.chars_per_second

        return global_estimator.chars_per_second, chars_per_second_by_speaker

    @staticmethod
    def _normalize_runtime_tts_provider(provider: object) -> str:
        value = str(provider or "").strip().lower()
        if value in {"minimax", "minimax_tts", "minimax_voice_clone"}:
            return "minimax"
        if value in {"cosyvoice", "cosyvoice_tts", "cosyvoice_voice_clone"}:
            return "cosyvoice"
        if value in {"volcengine", "volcengine_tts", "doubao", "doubao_tts"}:
            return "volcengine"
        return value

    @staticmethod
    def _build_user_voice_speed_profiles(
        segments: list[DubbingSegment],
        *,
        default_provider: str = "minimax",
        tts_model: str | None = None,
    ) -> tuple[list[dict[str, object]], dict[str, int]]:
        """Build conservative cloned-voice speed profiles from first-pass TTS.

        The key guardrail is ``first_pass_cn_text``: post-TTS rewrites may
        mutate ``segment.cn_text`` after the first audio was generated, so the
        profile must never pair a first-pass duration with a rewritten text.
        """
        from services.tts.voice_speed_bounds import MAX_VALID_CPS, MIN_VALID_CPS

        skipped: dict[str, int] = {}

        def _skip(reason: str, count: int = 1) -> None:
            skipped[reason] = skipped.get(reason, 0) + count

        buckets: dict[tuple[str, str, str], dict[str, object]] = {}
        fallback_model_key = str(tts_model or "").strip()
        default_provider_key = (
            ProcessPipeline._normalize_runtime_tts_provider(default_provider) or "minimax"
        )

        for segment in segments:
            if getattr(segment, "fallback_used_provider", None):
                _skip("fallback_provider_used")
                continue

            voice_id = (
                str(getattr(segment, "selected_voice", "") or "").strip()
                or str(getattr(segment, "voice_id", "") or "").strip()
            )
            if not voice_id or voice_id == "auto":
                _skip("missing_voice_id")
                continue

            first_pass_text = str(getattr(segment, "first_pass_cn_text", "") or "").strip()
            if not first_pass_text:
                if int(getattr(segment, "rewrite_count", 0) or 0) == 0:
                    first_pass_text = str(getattr(segment, "cn_text", "") or "").strip()
                else:
                    _skip("missing_first_pass_text")
                    continue
            spoken_chars = count_spoken_chars(first_pass_text)
            if spoken_chars < VOICE_SPEED_PROFILE_MIN_SAMPLE_CHARS:
                _skip("sample_too_short")
                continue

            first_pass_duration_ms = int(
                getattr(segment, "first_pass_duration_ms", 0) or 0
            )
            if first_pass_duration_ms <= 0:
                _skip("missing_first_pass_duration")
                continue

            speed = float(getattr(segment, "dsp_speed_param", 1.0) or 1.0)
            if speed <= 0:
                speed = 1.0
            natural_duration_ms = int(round(first_pass_duration_ms * speed))
            if natural_duration_ms <= 0:
                _skip("invalid_natural_duration")
                continue

            provider_key = ProcessPipeline._normalize_runtime_tts_provider(
                getattr(segment, "tts_provider", "") or default_provider_key
            )
            if provider_key not in {"minimax", "cosyvoice", "volcengine"}:
                _skip("unsupported_provider")
                continue

            model_key = (
                str(getattr(segment, "tts_model_key", "") or "").strip()
                or fallback_model_key
            )
            key = (voice_id, provider_key, model_key)
            bucket = buckets.setdefault(
                key,
                {
                    "voice_id": voice_id,
                    "tts_provider": provider_key,
                    "model_key": model_key,
                    "sample_count": 0,
                    "spoken_chars": 0,
                    "natural_duration_ms": 0,
                    "speaker_ids": set(),
                },
            )
            bucket["sample_count"] = int(bucket["sample_count"]) + 1
            bucket["spoken_chars"] = int(bucket["spoken_chars"]) + spoken_chars
            bucket["natural_duration_ms"] = (
                int(bucket["natural_duration_ms"]) + natural_duration_ms
            )
            speaker_ids = bucket["speaker_ids"]
            if isinstance(speaker_ids, set):
                speaker_ids.add(str(getattr(segment, "speaker_id", "") or ""))

        profiles: list[dict[str, object]] = []
        for bucket in buckets.values():
            sample_count = int(bucket["sample_count"])
            spoken_chars = int(bucket["spoken_chars"])
            natural_duration_ms = int(bucket["natural_duration_ms"])
            if sample_count < VOICE_SPEED_PROFILE_MIN_SAMPLES:
                _skip("insufficient_samples")
                continue
            if spoken_chars < VOICE_SPEED_PROFILE_MIN_SPOKEN_CHARS:
                _skip("insufficient_spoken_chars")
                continue
            if natural_duration_ms < VOICE_SPEED_PROFILE_MIN_NATURAL_DURATION_MS:
                _skip("insufficient_duration")
                continue
            cps = spoken_chars / (natural_duration_ms / 1000.0)
            if not (MIN_VALID_CPS <= cps <= MAX_VALID_CPS):
                _skip("cps_out_of_range")
                continue
            speaker_ids_obj = bucket.get("speaker_ids", set())
            speaker_ids = (
                sorted(s for s in speaker_ids_obj if s)
                if isinstance(speaker_ids_obj, set)
                else []
            )
            profiles.append({
                "voice_id": bucket["voice_id"],
                "tts_provider": bucket["tts_provider"],
                "model_key": bucket["model_key"],
                "chars_per_second": round(cps, 4),
                "sample_count": sample_count,
                "spoken_chars": spoken_chars,
                "natural_duration_ms": natural_duration_ms,
                "speaker_ids": speaker_ids,
            })

        profiles.sort(
            key=lambda item: (
                int(item.get("spoken_chars", 0) or 0),
                int(item.get("natural_duration_ms", 0) or 0),
            ),
            reverse=True,
        )
        if len(profiles) > VOICE_SPEED_PROFILE_MAX_PROFILES_PER_JOB:
            _skip("profile_cap_exceeded", len(profiles) - VOICE_SPEED_PROFILE_MAX_PROFILES_PER_JOB)
            profiles = profiles[:VOICE_SPEED_PROFILE_MAX_PROFILES_PER_JOB]
        return profiles, skipped

    @staticmethod
    def _persist_user_voice_speed_profiles(
        *,
        job_id: str,
        user_id: str | None,
        profiles: list[dict[str, object]],
        skipped_reasons: dict[str, int] | None = None,
    ) -> dict[str, object]:
        summary: dict[str, object] = {
            "voice_speed_profile_candidate_count": len(profiles),
            "voice_speed_profile_sent_count": 0,
            "voice_speed_profile_updated_count": 0,
            "voice_speed_profile_skipped_count": sum((skipped_reasons or {}).values()),
            "voice_speed_profile_skipped_reason_distribution": dict(skipped_reasons or {}),
        }
        if not profiles:
            return summary

        user_id_text = str(user_id or "").strip()
        if not user_id_text:
            reasons = dict(summary["voice_speed_profile_skipped_reason_distribution"])  # type: ignore[arg-type]
            reasons["missing_user_id"] = reasons.get("missing_user_id", 0) + len(profiles)
            summary["voice_speed_profile_skipped_count"] = (
                int(summary["voice_speed_profile_skipped_count"]) + len(profiles)
            )
            summary["voice_speed_profile_skipped_reason_distribution"] = reasons
            return summary

        import urllib.request

        gateway_base = os.environ.get("AVT_GATEWAY_URL", "http://127.0.0.1:8880").rstrip("/")
        # P0-2b (audit 2026-05-07): /internal/* → /api/internal/* so Caddy block applies.
        url = f"{gateway_base}/api/internal/user-voices/speed-profiles"
        headers = {"Content-Type": "application/json"}
        internal_key = os.environ.get("AVT_INTERNAL_API_KEY", "").strip()
        if internal_key:
            headers["X-Internal-Key"] = internal_key
        body = {
            "job_id": job_id,
            "user_id": user_id_text,
            "profiles": profiles,
        }

        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                response = json.loads(resp.read().decode("utf-8") or "{}")
            summary["voice_speed_profile_sent_count"] = len(profiles)
            updated_count = int(response.get("updated_count", 0) or 0)
            skipped_count = int(response.get("skipped_count", 0) or 0)
            summary["voice_speed_profile_updated_count"] = updated_count
            summary["voice_speed_profile_skipped_count"] = (
                int(summary["voice_speed_profile_skipped_count"]) + skipped_count
            )
            reason_counts = dict(summary["voice_speed_profile_skipped_reason_distribution"])  # type: ignore[arg-type]
            for item in response.get("skipped", []) or []:
                if not isinstance(item, dict):
                    continue
                reason = str(item.get("reason") or "gateway_skipped")
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            summary["voice_speed_profile_skipped_reason_distribution"] = reason_counts
            if updated_count:
                print(
                    f"[P1-l] Persisted {updated_count} cloned voice speed profile(s).",
                    flush=True,
                )
        except Exception as exc:
            reasons = dict(summary["voice_speed_profile_skipped_reason_distribution"])  # type: ignore[arg-type]
            reasons["gateway_persist_failed"] = reasons.get("gateway_persist_failed", 0) + len(profiles)
            summary["voice_speed_profile_skipped_count"] = (
                int(summary["voice_speed_profile_skipped_count"]) + len(profiles)
            )
            summary["voice_speed_profile_skipped_reason_distribution"] = reasons
            print(
                f"[P1-l] Warning: failed to persist cloned voice speed profile(s): {exc}",
                flush=True,
            )
        return summary

    # ------------------------------------------------------------------
    # Probe helpers: word counting, sentence-aware truncation, word timestamps
    # ------------------------------------------------------------------

    @staticmethod
    def _count_source_words(text: str, source_script: str = SCRIPT_LATIN) -> int:
        """Count spoken units in source text for probe sizing.

        Latin (default) → word-like tokens, byte-identical to the legacy English
        count (and translator.py). CJK → per-ideograph count, so a Chinese source
        yields non-zero probe candidates instead of silently collapsing to an
        empty probe set (the Latin regex matches ~0 tokens in Chinese text).
        """
        import re as _re
        if source_script == SCRIPT_CJK:
            return sum(1 for ch in (text or "") if "一" <= ch <= "鿿")
        return len(_re.findall(r"[A-Za-z0-9']+", text or ""))

    @staticmethod
    def _load_raw_word_timestamps(project_dir: Path) -> list[dict[str, object]]:
        """Load word-level timestamps from raw_assemblyai.json.

        Returns list of {text: str, start: int(ms), end: int(ms)}.
        Returns [] if file not found or unparseable.
        """
        raw_path = project_dir / "transcript" / "raw_assemblyai.json"
        if not raw_path.exists():
            return []
        try:
            data = json.loads(raw_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        raw_words = data.get("words") or []
        return [
            {"text": str(w.get("text", "")), "start": int(w.get("start", 0)), "end": int(w.get("end", 0))}
            for w in raw_words
            if isinstance(w, dict) and w.get("start") is not None
        ]

    @staticmethod
    def _refine_truncated_probe(
        line: TranscriptLine,
        raw_words: list[dict[str, object]],
        target_words: int = 80,
        min_duration_ms: int = 3_000,
    ) -> TranscriptLine:
        """Refine a truncated probe segment using word-level timestamps.

        Finds words within the segment's time range, truncates at a sentence
        boundary near *target_words*, and sets end_ms to the last word's
        precise timestamp.
        """
        from dataclasses import replace as _dc_replace

        # Find words belonging to this segment (within start_ms..end_ms)
        seg_words = [
            w for w in raw_words
            if int(w["start"]) >= line.start_ms and int(w["end"]) <= line.end_ms
        ]
        if len(seg_words) < 5:
            return line  # not enough words to refine

        # Find sentence boundary near target_words
        best_cut = min(target_words, len(seg_words))
        # Look backward from target for sentence-ending punctuation
        for i in range(min(best_cut, len(seg_words)) - 1, max(best_cut // 2, 4), -1):
            word_text = str(seg_words[i]["text"])
            if word_text.endswith((".", "?", "!", "。", "？", "！")):
                best_cut = i + 1
                break
        else:
            # No sentence boundary found; try comma/semicolon
            for i in range(min(best_cut, len(seg_words)) - 1, max(best_cut // 2, 4), -1):
                word_text = str(seg_words[i]["text"])
                if word_text.endswith((",", ";", "，", "；")):
                    best_cut = i + 1
                    break

        truncated_words = seg_words[:best_cut]
        truncated_text = " ".join(str(w["text"]) for w in truncated_words)
        precise_end_ms = int(truncated_words[-1]["end"])
        # Ensure minimum duration
        if precise_end_ms - line.start_ms < min_duration_ms:
            precise_end_ms = line.start_ms + min_duration_ms

        return _dc_replace(line, source_text=truncated_text, end_ms=precise_end_ms)

    @staticmethod
    def _select_probe_segments(
        lines: list[TranscriptLine],
        *,
        source_script: str = SCRIPT_LATIN,
        min_words: int = 20,
        max_words: int = 100,
        min_duration_ms: int = 3_000,
        max_duration_ms: int = 60_000,
        per_speaker: int = 3,
        max_words_per_speaker: int = 200,
        max_total: int = 15,
        truncate_words: int = 80,
    ) -> list[TranscriptLine]:
        """Select representative segments for probe TTS calibration.

        Hybrid selection: word count primary (20-100 words) + duration guard
        (3-60s). Picks up to 3 segments per speaker, preferring mid-length
        segments (40-70 words). Skips first/last segments.

        Progressive fallback: if a speaker has no candidates at min_words=20,
        retries at 10, then 5. If a speaker still has no candidates (e.g. all
        their segments are too long), one segment is truncated to *truncate_words*
        with proportionally adjusted duration — used for TTS calibration only.
        """
        if len(lines) <= 2:
            return []

        def _count_words(_t: str) -> int:
            return ProcessPipeline._count_source_words(_t, source_script)

        # Build candidate pool: skip first/last, apply word + duration filters
        def _filter_candidates(
            _min_words: int,
        ) -> list[TranscriptLine]:
            result: list[TranscriptLine] = []
            for i, line in enumerate(lines):
                if i == 0 or i == len(lines) - 1:
                    continue
                wc = _count_words(line.source_text)
                dur = line.end_ms - line.start_ms
                if _min_words <= wc <= max_words and min_duration_ms <= dur <= max_duration_ms:
                    result.append(line)
            return result

        candidates = _filter_candidates(min_words)

        # Group by speaker
        by_speaker: dict[str, list[TranscriptLine]] = {}
        for line in candidates:
            by_speaker.setdefault(line.speaker_id, []).append(line)

        # Identify all speakers in transcript (include first/last so that
        # speakers whose only segment is at the boundary still get a probe
        # via the truncation fallback below).
        all_speakers: set[str] = {line.speaker_id for line in lines}

        # Progressive fallback for speakers with no candidates
        for fallback_min in (10, 5):
            missing = all_speakers - set(by_speaker.keys())
            if not missing:
                break
            fallback = _filter_candidates(fallback_min)
            for line in fallback:
                if line.speaker_id in missing:
                    by_speaker.setdefault(line.speaker_id, []).append(line)

        # Truncation fallback: ensure every speaker has at least one probe.
        # For speakers with no candidates (segments too long / too many words),
        # mark their best segment for truncation. Actual truncation with
        # word-level timestamps is done later in _run_probe_translation.
        still_missing = all_speakers - set(by_speaker.keys())
        if still_missing:
            from dataclasses import replace as _dc_replace
            for sid in still_missing:
                # Include first/last segments — for speakers with only
                # boundary segments, calibration coverage > intro avoidance
                speaker_segs = [
                    ln for ln in lines if ln.speaker_id == sid
                ]
                if not speaker_segs:
                    continue
                # Pick the segment with most words (best calibration signal)
                best = max(speaker_segs, key=lambda ln: _count_words(ln.source_text))
                total_wc = _count_words(best.source_text)
                if total_wc < 5:
                    continue  # too little text even for truncation
                # Proportional truncation (will be refined with word timestamps later)
                import re as _re
                target_wc = min(truncate_words, total_wc)
                if source_script == SCRIPT_CJK:
                    # CJK has no whitespace word tokens — a long Chinese turn is a
                    # single ``\S+`` token, so the legacy whitespace truncation
                    # would not shrink it and the synthetic segment would still be
                    # skipped by max_words_per_speaker. Truncate by character and
                    # measure with the CJK-aware counter instead.
                    truncated_text = (best.source_text or "")[:target_wc]
                    actual_wc = _count_words(truncated_text)
                    ratio = actual_wc / max(total_wc, 1)
                else:
                    words_list = _re.findall(r"\S+", best.source_text or "")
                    # Try to break at sentence boundary near target_wc
                    truncated_text = _truncate_at_sentence(words_list, target_wc)
                    actual_wc = len(truncated_text.split())
                    ratio = actual_wc / max(len(words_list), 1)
                orig_dur = best.end_ms - best.start_ms
                adj_dur = max(int(orig_dur * ratio), min_duration_ms)
                adj_end_ms = best.start_ms + adj_dur
                synthetic = _dc_replace(best, source_text=truncated_text, end_ms=adj_end_ms)
                by_speaker.setdefault(sid, []).append(synthetic)

        if not by_speaker:
            return []

        # Per-speaker selection: prefer mid-length (40-70 words), evenly spaced
        ideal_mid = 55  # midpoint of 40-70
        selected: list[TranscriptLine] = []
        for speaker_lines in by_speaker.values():
            # Sort by distance from ideal midpoint (prefer 40-70 word segments)
            speaker_lines.sort(
                key=lambda ln: abs(_count_words(ln.source_text) - ideal_mid),
            )
            picked: list[TranscriptLine] = []
            cumulative_words = 0
            for line in speaker_lines:
                if len(picked) >= per_speaker:
                    break
                wc = _count_words(line.source_text)
                if cumulative_words + wc > max_words_per_speaker:
                    continue
                picked.append(line)
                cumulative_words += wc
            selected.extend(picked)

        # Cap at max_total
        selected = selected[:max_total]

        # Sort by original order
        line_order = {id(line): i for i, line in enumerate(lines)}
        selected.sort(key=lambda ln: line_order.get(id(ln), 0))

        return selected

    # ------------------------------------------------------------------
    # Probe cache helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_probe_fingerprint(
        probe_lines: list[TranscriptLine],
        *,
        model_name: str,
        glossary: dict[str, str] | None,
        video_title: str,
        youtube_url: str,
    ) -> str:
        """Build a fingerprint for probe translation cache invalidation.

        Includes segment durations because probe translation prompt uses
        target_duration_seconds — timestamp changes invalidate the cache.
        """
        import hashlib as _hl
        payload = {
            "segment_ids": sorted(ln.index for ln in probe_lines),
            "source_texts": [ln.source_text for ln in probe_lines],
            "durations": [[ln.start_ms, ln.end_ms] for ln in probe_lines],
            "model_name": model_name,
            "glossary": glossary or {},
            "video_title": video_title or "",
            "youtube_url": youtube_url or "",
        }
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return _hl.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _save_probe_cache(
        cache_path: Path,
        segments: list["DubbingSegment"],
        fingerprint: str,
    ) -> None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "fingerprint": fingerprint,
            "segments": [
                {
                    "segment_id": s.segment_id,
                    "speaker_id": s.speaker_id,
                    "source_text": s.source_text,
                    "cn_text": s.cn_text,
                    "start_ms": s.start_ms,
                    "end_ms": s.end_ms,
                    "target_duration_ms": s.target_duration_ms,
                }
                for s in segments
            ],
        }
        cache_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _load_probe_cache(
        cache_path: Path,
        expected_fingerprint: str,
    ) -> list["DubbingSegment"] | None:
        """Load cached probe segments if fingerprint matches."""
        if not cache_path.exists():
            return None
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if data.get("fingerprint") != expected_fingerprint:
            return None
        from services.gemini.translator import DubbingSegment
        result: list[DubbingSegment] = []
        for s in data.get("segments", []):
            seg = DubbingSegment(
                segment_id=s["segment_id"],
                speaker_id=s.get("speaker_id", "speaker_a"),
                display_name=s.get("speaker_id", "speaker_a"),
                voice_id="",
                source_text=s.get("source_text", ""),
                cn_text=s.get("cn_text", ""),
                start_ms=s.get("start_ms", 0),
                end_ms=s.get("end_ms", 0),
                target_duration_ms=s.get("target_duration_ms", 0),
            )
            result.append(seg)
        return result if result else None

    # ------------------------------------------------------------------
    # Probe Phase 1: translation (before voice selection)
    # ------------------------------------------------------------------

    def _run_probe_translation(
        self,
        transcript_lines: list[TranscriptLine],
        translator: "GeminiTranslator",
        *,
        cache_dir: Path | None = None,
        video_title: str = "",
        youtube_url: str = "",
        glossary: dict[str, str] | None = None,
        speaker_voices: dict[str, str] | None = None,
        voice_id_a: str | None = None,
        display_name_a: str = "Speaker A",
        voice_id_b: str | None = None,
        display_name_b: str | None = None,
    ) -> list["DubbingSegment"]:
        """Phase 1: Select probe segments and translate (no char constraints).

        Returns list of DubbingSegments with cn_text populated.
        Caches result with fingerprint for resume.
        """
        _probe_profile = getattr(self, "_language_profile", DEFAULT_LANGUAGE_PAIR_PROFILE)
        _probe_src_desc = get_language_descriptor(_probe_profile.source_language)
        probe_lines = self._select_probe_segments(
            transcript_lines,
            source_script=_probe_src_desc.script_family if _probe_src_desc is not None else SCRIPT_LATIN,
        )
        if not probe_lines:
            print("[S4-probe] 无满足条件的探针段落")
            return []

        # Refine truncated probes with word-level timestamps if available
        project_dir = cache_dir.parent if cache_dir else None
        if project_dir:
            raw_words = self._load_raw_word_timestamps(project_dir)
            if raw_words:
                refined: list[TranscriptLine] = []
                for ln in probe_lines:
                    orig_dur = ln.end_ms - ln.start_ms
                    wc = self._count_source_words(ln.source_text)
                    # Detect truncated segments: text much shorter than duration implies
                    # (normal speech ~2.5 words/sec; if wc < dur*1.5 it's probably truncated)
                    # Simpler check: find original line and compare source_text
                    orig_line = next(
                        (l for l in transcript_lines if l.index == ln.index),
                        None,
                    )
                    if orig_line and ln.source_text != orig_line.source_text:
                        # This probe was truncated — refine with word timestamps
                        refined_ln = self._refine_truncated_probe(
                            orig_line, raw_words, target_words=80,
                        )
                        refined.append(refined_ln)
                        print(f"  [S4-probe] 精确截断 seg#{ln.index}: "
                              f"{self._count_source_words(refined_ln.source_text)}词 "
                              f"{round((refined_ln.end_ms - refined_ln.start_ms) / 1000, 1)}s "
                              f"(词级时间戳)")
                    else:
                        refined.append(ln)
                probe_lines = refined

        print(f"[S4-probe] 选取 {len(probe_lines)} 段探针段落")
        for ln in probe_lines:
            wc = self._count_source_words(ln.source_text)
            dur_s = round((ln.end_ms - ln.start_ms) / 1000, 1)
            print(f"  {ln.speaker_id}: seg#{ln.index} {wc}词 {dur_s}s")

        fingerprint = self._build_probe_fingerprint(
            probe_lines,
            model_name=translator.model_name,
            glossary=glossary,
            video_title=video_title,
            youtube_url=youtube_url,
        )

        # Try loading from cache (for resume after voice selection pause)
        cache_path = cache_dir / "_probe_segments.json" if cache_dir else None
        if cache_path:
            cached = self._load_probe_cache(cache_path, fingerprint)
            if cached:
                print(f"[S4-probe] 从缓存加载 {len(cached)} 段探针翻译")
                return cached

        # Translate
        previous_phase = getattr(translator, "_metering_usage_context", "")
        setattr(translator, "_metering_usage_context", "probe_translate")
        try:
            probe_segments = translator.translate_probe(
                probe_lines,
                video_title=video_title,
                youtube_url=youtube_url,
                glossary=glossary,
                speaker_voices=speaker_voices,
                voice_id=voice_id_a,
                display_name=display_name_a,
                voice_id_b=voice_id_b,
                display_name_b=display_name_b,
            )
        finally:
            setattr(translator, "_metering_usage_context", previous_phase)

        if not probe_segments:
            print("[S4-probe] 探针翻译无结果")
            return []

        # Cache for resume
        if cache_path:
            try:
                self._save_probe_cache(cache_path, probe_segments, fingerprint)
            except Exception:
                pass  # non-fatal

        return probe_segments

    # ------------------------------------------------------------------
    # Probe Phase 2: TTS + calibration (after voice selection)
    # ------------------------------------------------------------------

    def _run_probe_tts_and_calibrate(
        self,
        probe_segments: list["DubbingSegment"],
        tts_generator: "TTSGenerator",
        tts_dir: Path,
        *,
        voice_id_a: str = "",
        display_name_a: str = "",
        voice_id_b: str | None = None,
        display_name_b: str = "",
        speaker_voices: dict[str, str] | None = None,
        speaker_providers: dict[str, str] | None = None,
        speaker_voice_routing: dict[str, dict[str, object]] | None = None,
    ) -> tuple[float, dict[str, float]]:
        """Phase 2: Run TTS on translated probe segments, then calibrate.

        Applies user-confirmed voice_id + tts_provider + worker routing before
        TTS so that calibration runs on the exact voices the user selected.
        Phase 4.1 E.5 (Codex 2026-05-25)：probe path 也必须接 ``speaker_voice_routing``
        否则 cloned voice 的 probe TTS 漂到国际 endpoint，与主 TTS 不一致。

        Returns (global_chars_per_second, {speaker_id: chars_per_second}).
        Falls back to DEFAULT 4.5 on any failure.
        """
        default_cps = DEFAULT_ESTIMATED_TTS_CHARS_PER_SECOND

        # Apply user-confirmed voice_id so probe TTS uses the correct voice
        self._apply_runtime_voice_overrides(
            probe_segments,
            voice_id_a=voice_id_a,
            display_name_a=display_name_a,
            voice_id_b=voice_id_b,
            display_name_b=display_name_b,
            speaker_voices=speaker_voices,
            speaker_providers=speaker_providers,
            speaker_voice_routing=speaker_voice_routing,
        )

        # Probe TTS — output to _probe subdirectory to avoid collision with main TTS
        probe_tts_dir = (tts_dir / "_probe").resolve(strict=False)
        probe_tts_dir.mkdir(parents=True, exist_ok=True)
        try:
            _generate_tts_all_with_bucket(
                tts_generator,
                probe_segments,
                str(probe_tts_dir),
                usage_bucket=TTS_BUCKET_PROBE,
            )
        except Exception as exc:
            print(f"[S4-probe] 探针 TTS 失败（回退 {default_cps}）：{exc}")
            return default_cps, {}

        # Calibrate from probe results
        # Probe has few segments per speaker — use min_speaker_samples=1
        # so even 1 sample produces per-speaker calibration (better than global fallback)
        chars_per_second, chars_per_second_by_speaker = self._calibrate_tts_duration(
            probe_segments,
            min_speaker_samples=1,
        )

        # Sanity check — reject implausible values
        if chars_per_second < 2.0 or chars_per_second > 8.0:
            print(
                f"[S4-probe] 校准值异常 ({chars_per_second:.2f} 字/秒)，"
                f"回退默认 {default_cps} 字/秒"
            )
            return default_cps, {}

        print(f"[S4-probe] 校准完成：global={chars_per_second:.2f} 字/秒")
        for spk, cps in chars_per_second_by_speaker.items():
            print(f"  {spk}: {cps:.2f} 字/秒")

        return chars_per_second, chars_per_second_by_speaker

    def _resolve_voice_registry_path(self) -> str:
        project_config = config_loader.load_project_local_config()
        resolved_path, _ = config_loader.resolve_path_value(
            env_keys=["AUTODUB_TTS_VOICE_REGISTRY_PATH"],
            config=project_config,
            config_key_paths=(
                ("voice_registry", "registry_path"),
                ("tts", "voice_registry_path"),
                ("paths", "voice_registry_path"),
            ),
        )
        if resolved_path is not None:
            return str(Path(resolved_path).expanduser().resolve(strict=False))
        return str((PROJECT_ROOT / "voice_registry.json").resolve(strict=False))


def _classify_failed_stage(exc: Exception) -> str:
    exc_text = str(exc)
    if "分离" in exc_text or "ambient" in exc_text or isinstance(exc, AudioSeparationError):
        return "S0"
    if "AssemblyAI" in exc_text:
        return "S1"
    if isinstance(exc, ContentPolicyViolationError):
        return "S2"
    if (
        "voice_id for speaker_b" in exc_text
        or "voice_registry" in exc_text
        or "样本" in exc_text
        or "克隆" in exc_text
        or isinstance(exc, (VoiceLookupError, SampleExtractionError, AutoCloneError))
    ):
        return "S2"
    if "Gemini" in exc_text:
        return "S3"
    if "MiniMax" in exc_text or "TTS" in exc_text:
        return "S4"
    if "ffmpeg" in exc_text or "Alignment" in exc_text or "对齐" in exc_text:
        return "S5"
    return "流程"


def _slugify_text(value: str) -> str:
    normalized = value.strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized)
    return normalized.strip("_") or "speaker"


def _coerce_int(value: object, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: object, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_optional_text(value: object) -> str | None:
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            return normalized
    return None


def _normalize_tts_sync_text(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


def _sync_tts_text_audio_for_publish(segments: list[DubbingSegment]) -> list[str]:
    """Keep main-pipeline publish text in sync with the current TTS audio.

    In the main pipeline, ``cn_text`` is the text shown in the editor and
    ``tts_input_cn_text`` is the text that produced the current TTS audio.
    New runs should normally already be in sync; this non-fatal normalizer
    prevents an unknown post-TTS rewrite path from shipping mismatched text.
    """
    repairs: list[str] = []
    for segment in segments:
        if is_keep_original_dubbing_mode(
            getattr(segment, "dubbing_mode", DUBBING_MODE_DUB)
        ):
            continue
        cn_text = str(getattr(segment, "cn_text", "") or "").strip()
        if not cn_text:
            continue
        has_audio = bool(
            getattr(segment, "tts_audio_path", None)
            or getattr(segment, "aligned_audio_path", None)
        )
        if not has_audio:
            continue
        tts_input = str(getattr(segment, "tts_input_cn_text", "") or "").strip()
        try:
            rewrite_count = int(getattr(segment, "rewrite_count", 0) or 0)
        except (TypeError, ValueError):
            rewrite_count = 0

        if not tts_input:
            segment.tts_input_cn_text = cn_text
            if rewrite_count > 0:
                repairs.append(f"segment_{segment.segment_id}: backfilled missing tts_input")
            continue

        if _normalize_tts_sync_text(tts_input) != _normalize_tts_sync_text(cn_text):
            segment.cn_text = tts_input
            repairs.append(f"segment_{segment.segment_id}: cn_text <- tts_input_cn_text")

    return repairs


def _backfill_legacy_tts_input_cn_text(segment: DubbingSegment) -> None:
    """Backfill ``tts_input_cn_text`` for legacy editor/segments.json files.

    2026-05-04 P0a context: pre-rollout JSON has no ``tts_input_cn_text``
    key, so the dataclass default of ``""`` lands on every segment loaded
    from disk. Downstream cue generation compares ``cn_text`` to
    ``tts_input_cn_text`` to detect text↔audio drift; an empty stamp would
    falsely flag the entire legacy job as drift.

    Conservative default: only assume the audio matches the current cn_text
    once the record already looks like a completed audio-bearing segment.
    Translation-review snapshots are written before TTS exists; backfilling
    those would create a stale text witness before the first synthesis pass.
    """
    has_audio_witness = bool(
        getattr(segment, "tts_audio_path", None)
        or getattr(segment, "aligned_audio_path", None)
        or int(getattr(segment, "actual_duration_ms", 0) or 0) > 0
        or (getattr(segment, "alignment_method", "") or "").strip()
    )
    if not segment.tts_input_cn_text and segment.cn_text and has_audio_witness:
        segment.tts_input_cn_text = segment.cn_text


def _deserialize_transcript_line_payload(line_payload: object) -> TranscriptLine:
    if not isinstance(line_payload, dict):
        raise TypeError("Transcript cache line must be an object.")
    speaker_id = _normalize_optional_text(line_payload.get("speaker_id")) or "speaker_a"
    speaker_label = (
        _normalize_optional_text(line_payload.get("speaker_label"))
        or _normalize_optional_text(line_payload.get("speaker_name"))
        or _normalize_optional_text(line_payload.get("display_name"))
        or ("B" if speaker_id == "speaker_b" else "A" if speaker_id == "speaker_a" else speaker_id)
    )
    return TranscriptLine(
        index=_coerce_int(line_payload.get("index"), default=0),
        start_ms=_coerce_int(line_payload.get("start_ms"), default=0),
        end_ms=_coerce_int(line_payload.get("end_ms"), default=0),
        speaker_id=speaker_id,
        speaker_label=speaker_label,
        source_text=_normalize_optional_text(line_payload.get("source_text")) or "",
        dubbing_mode=normalize_dubbing_mode(line_payload.get("dubbing_mode")),
    )
