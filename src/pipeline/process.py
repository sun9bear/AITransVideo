from __future__ import annotations

from dataclasses import asdict as _dc_asdict, dataclass, fields as _dc_fields
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

T = TypeVar("T")

_ADMIN_SETTINGS_PATH = str(
    Path(os.environ.get("AIVIDEOTRANS_CONFIG_DIR", "/opt/aivideotrans/config"))
    / "admin_settings.json"
)
_SPEAKER_ID_PATTERN = re.compile(r"^speaker_[a-z0-9_]+$")


# _should_skip_translation_config / _should_skip_all_reviews removed —
# decision now comes from the job record's snapshot fields
# (job_requires_review, job_service_mode).  See run() below.


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


def _report_job_metering(
    job_id: str,
    segments: list,
    *,
    tts_billed_chars: int | None = None,
    glossary: dict[str, str] | None = None,
    extra_metering: dict[str, object] | None = None,
) -> None:
    """Best-effort callback to Gateway /job-api/jobs/{job_id}/metering.

    Computes and reports pipeline metering fields from real segment objects.
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
    import urllib.request
    gateway_base = os.environ.get("AVT_GATEWAY_URL", "http://localhost:8880")
    url = f"{gateway_base}/job-api/jobs/{job_id}/metering"

    try:
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


def _check_duration_limit(
    duration_ms: int,
    *,
    plan_code_snapshot: str = "free",
    role_snapshot: str = "user",
) -> None:
    """Check video duration against plan-based limit from snapshot.

    Admin users bypass the check entirely.  The primary validation is done
    by Gateway at creation time; this is a hard safety-net inside the pipeline.
    """
    if role_snapshot == "admin":
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
        self._current_service_mode = job_service_mode  # for recovery paths
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
            self._enforce_english_source_language(download_result)

            # --- 套餐时长限制 (snapshot-based, Gateway 主检查的安全网) ---
            _check_duration_limit(
                actual_duration_ms,
                plan_code_snapshot=job_plan_code,
                role_snapshot=job_role,
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
                self._enforce_english_transcript_language(transcript_result)

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
            if _review_speaker_styles:
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
                        _pass3_profiles = review_pass3_voice_profiles(
                            transcript_result.lines,
                            source_audio_path=source_audio_path if source_audio_path.exists() else None,
                            speakers=_review_speaker_styles,
                            video_title=download_result.video_title,
                            debug_output_dir=final_project_dir / "transcript",
                            mode=job_service_mode,
                            usage_meter=usage_meter,
                        )
                    except Exception as exc:
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

            # --- voice_selection_review gate (Studio mode, BEFORE translation) ---
            approved_voice_selection = self._get_approved_review_payload(
                review_state_manager,
                VOICE_SELECTION_REVIEW_STAGE,
            )
            _speaker_providers: dict[str, str] = {}
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
                    voice_id_a = _speaker_voices.get("speaker_a", voice_id_a)
                    voice_id_b = _speaker_voices.get("speaker_b", voice_id_b)
                    print(f"[S2.5] 用户确认音色：{_speaker_voices}")
                    if _speaker_providers:
                        print(f"[S2.5] 用户选择引擎：{_speaker_providers}")

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
            elif config.wait_for_review and job_requires_review and job_service_mode == "studio":
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
            if config.wait_for_review and job_service_mode == "studio":
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
                    review_state_manager.set_stage(
                        VOICE_SELECTION_REVIEW_STAGE,
                        status=REVIEW_STATUS_PENDING,
                        payload=vs_payload,
                        activate=True,
                    )
                    review_message = f"检测到 {len(expired_voices)} 个音色已失效，请重新选择"
                    print(f"[S2.5] {review_message}")
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
            if (
                job_service_mode == "studio"
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
                )
                # translate() creates fresh DubbingSegments without tts_provider;
                # apply per-speaker TTS provider overrides from voice selection
                if _speaker_providers:
                    for seg in translation_result.segments:
                        if seg.speaker_id in _speaker_providers:
                            seg.tts_provider = _speaker_providers[seg.speaker_id]
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
                # Re-apply per-speaker TTS provider: snapshot may predate the
                # tts_provider field, or was written before voice selection.
                if _speaker_providers:
                    for seg in translation_result.segments:
                        if seg.speaker_id in _speaker_providers:
                            seg.tts_provider = _speaker_providers[seg.speaker_id]
                self._apply_speaker_structure_profiles_to_segments(
                    translation_result.segments,
                    _speaker_structure_profiles,
                )
                if dubbing_modes_synced:
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
            output_bundle = self._dispatch_process_output_bundle(
                project_dir=final_project_dir,
                build_result=build_result,
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
                    if v.get("language") not in ("中文-普通话", "中文-粤语"):
                        continue
                    vid = str(v.get("voice_id", ""))
                    lbl = str(v.get("display_name", v.get("name", vid)))
                    display_map[vid] = lbl
                    voices.append(_voice_dict(v, vid, lbl))

            return voices, display_map

        # Build all three providers
        all_providers: dict[str, dict[str, object]] = {}
        all_display_maps: dict[str, dict[str, str]] = {}
        for prov in ("minimax", "cosyvoice", "volcengine"):
            try:
                voices, dmap = _build_provider_voices(prov)
            except Exception:
                voices, dmap = [], {}
            all_display_maps[prov] = dmap
            all_providers[prov] = {
                "label": _PROVIDER_LABELS.get(prov, prov),
                "available_voices": voices,
                "supports_clone": prov == "minimax",
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
            _w = self._count_source_words(_line.source_text or "")
            _d = max(0, int(_line.end_ms - _line.start_ms))
            if _w > 0 and _d > 0:
                _speaker_word_totals[_line.speaker_id] = (
                    _speaker_word_totals.get(_line.speaker_id, 0) + _w
                )
                _speaker_dur_ms_totals[_line.speaker_id] = (
                    _speaker_dur_ms_totals.get(_line.speaker_id, 0) + _d
                )
        speaker_target_cps: dict[str, float] = {}
        for _sid, _words in _speaker_word_totals.items():
            _dur_s = _speaker_dur_ms_totals.get(_sid, 0) / 1000.0
            if _dur_s > 0:
                _wps = _words / _dur_s
                speaker_target_cps[_sid] = round(_wps * 1.8, 2)

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
                return data.get("credits", {}).get("voice_clone_cost_credits", 500)
        except Exception:
            pass
        return 500

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

    def _enforce_english_source_language(self, download_result: DownloadResult) -> None:
        source_language = str(getattr(download_result, "language", "") or "").strip()
        if not source_language:
            return
        if _is_english_language_code(source_language):
            print(f"[S0] 视频源语言元数据：{source_language}")
            return
        raise ValueError(
            "当前只支持英文视频翻译。"
            f"视频源语言元数据为 {source_language!r}，请确认输入的视频是英文内容。"
        )

    def _enforce_english_transcript_language(
        self,
        transcript_result: TranscriptResult,
    ) -> None:
        explicit_language = str(getattr(transcript_result, "language", "") or "").strip()
        if explicit_language and not _is_english_language_code(explicit_language):
            if explicit_language.lower() not in {"auto", "unknown", "und", "undefined"}:
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

    def _detect_transcript_language(
        self,
        lines: list[TranscriptLine],
        sample_limit: int = 20,
        english_threshold: float = 0.6,
    ) -> str:
        """Detect language from early transcript lines. Returns 'en' or 'unknown'."""
        sample_lines = lines[:sample_limit]
        combined_text = " ".join(
            str(line.source_text).strip() for line in sample_lines if line.source_text
        )
        if not combined_text:
            return "en"  # Empty transcript, let downstream handle it

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
            voice_description = str(speaker_info.get("voice_description", "") or "")
            segment.voice_description = voice_description
            segment.gender = str(speaker_info.get("gender", "") or "")
            segment.age_group = str(speaker_info.get("age_group", "") or "")
            # Propagate reviewer name to display_name for all speakers (including c+).
            # Overwrite default placeholders ("Speaker B", "Speaker C", etc.) but
            # do NOT overwrite a user-confirmed custom name.
            speaker_name = str(speaker_info.get("name", "") or "")
            if speaker_name and self._is_placeholder_display_name(segment.display_name, segment.speaker_id):
                segment.display_name = speaker_name
            segment.persona_style = str(
                speaker_info.get("persona_style", "") or infer_persona_style(voice_description)
            )
            segment.energy_level = str(
                speaker_info.get("energy_level", "") or infer_energy_level(voice_description)
            )

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
    ) -> dict[str, dict[str, object]]:
        # Try loading from cached s2_review_result.json first (avoids expensive LLM re-call).
        # Voice fields (gender/age_group/persona/energy) are filled by Pass 3 later,
        # so it's fine if they're empty here — no need to re-run S2 to get them.
        s2_cache = Path(transcript_result.structured_transcript_path).parent / "s2_review_result.json"
        if s2_cache.exists():
            try:
                cached = json.loads(s2_cache.read_text(encoding="utf-8"))
                speakers = cached.get("speakers", {})
                if speakers:
                    print(f"[S2] Restored speaker styles from cache ({len(speakers)} speakers).", flush=True)
                    return speakers
            except Exception as exc:
                print(f"[S2] Failed to load cached s2_review_result.json: {exc}", flush=True)

        # Fallback: build minimal styles from transcript speaker IDs
        print("[S2] No cached S2 result; using minimal speaker styles (Pass 3 will enrich later).", flush=True)
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
    ) -> None:
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

        cn_chunks = self._split_text_for_failed_segment(cn_text, FAILED_SEGMENT_SEMANTIC_SPLIT_PATTERN)
        if cn_chunks is None:
            return None

        source_chunks = self._split_text_for_failed_segment(
            segment.source_text,
            FAILED_SEGMENT_SOURCE_SPLIT_PATTERN,
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
    ) -> OutputBundleResult:
        return OutputDispatcher().dispatch(
            build_result.localized_project,
            build_result.artifact_index,
            OutputRequest(
                # PUBLISH target also runs EDITOR (dubbed audio is needed for muxing).
                # Pipeline always produces the final video (原视频画面 + 配音 + 背景音).
                targets=[OutputTarget.PUBLISH],
                output_dir=str(project_dir.resolve(strict=False)),
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
    ) -> dict[str, object]:
        if not is_content_compliance_enabled():
            print("[S2] 内容合规审核已关闭，跳过。")
            return {
                "status": "skipped",
                "message": "内容合规审核已关闭。",
            }

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
            llm_fail_closed=is_content_compliance_llm_fail_closed(),
        )
        report_path = reviewer.write_report(final_result, project_dir=final_project_dir)
        payload = final_result.to_dict()
        payload["artifact_path"] = str(report_path)
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

    def _split_text_for_failed_segment(self, text: str, pattern: re.Pattern[str]) -> list[str] | None:
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

        left_text = "".join(pieces[: best_index + 1]).strip()
        right_text = "".join(pieces[best_index + 1 :]).strip()
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
    def _count_source_words(text: str) -> int:
        """Count spoken words in source text (same logic as translator.py)."""
        import re as _re
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

        _count_words = ProcessPipeline._count_source_words

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
        probe_lines = self._select_probe_segments(transcript_lines)
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
    ) -> tuple[float, dict[str, float]]:
        """Phase 2: Run TTS on translated probe segments, then calibrate.

        Applies user-confirmed voice_id + tts_provider before TTS so that
        calibration runs on the exact voices the user selected.

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

    Conservative default: assume the audio matches the current cn_text
    (i.e. user hasn't done text-edit-without-regen-tts on this old job).
    If they had, ``editor/segment_status.json`` would already mark the
    segment ``text_dirty`` and the editing layer guards drift independently.
    """
    if not segment.tts_input_cn_text and segment.cn_text:
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
