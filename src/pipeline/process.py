from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

# Load .env file if present (for API keys not in container env)
_ENV_FILE = Path("/opt/aivideotrans/config/.env")
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _, _val = _line.partition("=")
            if _key.strip() and _key.strip() not in os.environ:
                os.environ[_key.strip()] = _val.strip()
from typing import Callable, TypeVar

from core.enums import OutputTarget, StageStatus
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
from services.assemblyai.transcriber import (
    AssemblyAITranscriber,
    TranscriptLine,
    TranscriptResult,
    TranscriptionError,
    load_assemblyai_config,
)
from services.gemini.rewriter import GeminiRewriter
from services.gemini.translator import (
    DubbingSegment,
    GeminiTranslator,
    TranslationResult,
    load_gemini_config,
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
from services.state_manager import StateManager
from services.tts.duration_estimator import TTSDurationEstimator
from services.tts.tts_generator import TTSConfig, TTSGenerator, load_tts_config
from services.voice.auto_clone import AutoCloneError, AutoVoiceCloner
from services.voice_clone import VoiceCloneConfig
from services.voice.sample_extractor import (
    MIN_SAMPLE_DURATION_SECONDS,
    SampleExtractionError,
    VoiceSampleExtractor,
)
from services.voice.voice_lookup import VoiceLookupError, lookup_voice_ids
from utils.audio_utils import measure_duration_ms as _ffprobe_duration_ms


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
PRE_ALIGNMENT_SEMANTIC_SPLIT_OVERSHOOT_RATIO = 0.30
SEVERE_PRE_ALIGNMENT_SEMANTIC_SPLIT_MIN_TARGET_MS = 30_000
SEVERE_PRE_ALIGNMENT_SEMANTIC_SPLIT_OVERSHOOT_RATIO = 0.35
FAILED_SEGMENT_SEMANTIC_SPLIT_PATTERN = re.compile(r"(?<=[。！？!?；;])\s*")
FAILED_SEGMENT_SOURCE_SPLIT_PATTERN = re.compile(r"(?<=[.!?;])\s+")

T = TypeVar("T")

_ADMIN_SETTINGS_PATH = "/opt/aivideotrans/config/admin_settings.json"
_SPEAKER_ID_PATTERN = re.compile(r"^speaker_[a-z0-9_]+$")


# _should_skip_translation_config / _should_skip_all_reviews removed —
# decision now comes from the job record's snapshot fields
# (job_requires_review, job_service_mode).  See run() below.


def _get_default_translation_model(mode: str = "studio") -> str:
    """Get default translation model from llm_registry."""
    from services.llm_registry import get_prompt_model
    return get_prompt_model(mode, "translate")


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
    merged["speaker_a"] = speaker_name_a
    merged["speaker_b"] = speaker_name_b
    return merged


def _report_source_metadata(job_id: str, duration_seconds: float, title: str | None = None) -> None:
    """Best-effort callback to Gateway /job-api/jobs/{job_id}/source-metadata."""
    import urllib.request
    gateway_base = os.environ.get("AVT_GATEWAY_URL", "http://localhost:8880")
    url = f"{gateway_base}/job-api/jobs/{job_id}/source-metadata"
    body: dict = {"source_duration_seconds": duration_seconds}
    if title:
        body["title"] = title
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            print(f"[S0] Reported source metadata to gateway: {resp.status}", flush=True)
    except Exception as e:
        print(f"[S0] Warning: failed to report source metadata: {e}", flush=True)


def _report_job_metering(job_id: str, segments: list, *, tts_billed_chars: int | None = None) -> None:
    """Best-effort callback to Gateway /job-api/jobs/{job_id}/metering.

    Computes and reports pipeline metering fields from real segment objects.
    Supports both ``DubbingSegment`` (real pipeline path) and ``SemanticBlock``
    (legacy/alternative path) by checking for available text fields.

    Text field: ``cn_text`` (DubbingSegment) or ``merged_cn_text`` (SemanticBlock).

    Reports:
    - final_cn_chars: total Chinese characters in final translated text
    - rewrite_triggered: whether any segment had rewrite_count > 0
    - rewrite_count: total rewrite operations across all segments
    - tts_billed_chars: total chars submitted to TTS provider (from TTSResult.billed_chars)
    """
    import urllib.request
    gateway_base = os.environ.get("AVT_GATEWAY_URL", "http://localhost:8880")
    url = f"{gateway_base}/job-api/jobs/{job_id}/metering"

    try:
        total_cn_chars = 0
        total_rewrite_count = 0
        for seg in segments:
            text = getattr(seg, "cn_text", "") or ""
            if not text:
                text = getattr(seg, "merged_cn_text", "") or ""
            total_cn_chars += len(text)
            total_rewrite_count += getattr(seg, "rewrite_count", 0)

        body: dict = {
            "final_cn_chars": total_cn_chars,
            "rewrite_triggered": total_rewrite_count > 0,
            "rewrite_count": total_rewrite_count,
        }
        # V3-5: include tts_billed_chars only if truthfully available from TTS layer
        if tts_billed_chars is not None:
            body["tts_billed_chars"] = tts_billed_chars

        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
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


# Plan-based max duration (minutes).  Mirrors PLAN_CATALOG in gateway.
# The pipeline uses these only as a hard safety-net; the primary check
# is done by Gateway at job-creation time.
_PLAN_MAX_DURATION_MINUTES = {
    "free": 10,
    "plus": 60,
    "pro": 180,
}


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


class ProcessPipeline:
    """Legacy compatibility pipeline: YouTube URL -> editor-facing dubbing bundle."""

    def __init__(self, project_builder: ProjectBuilder | None = None) -> None:
        self.project_builder = project_builder or ProjectBuilder()

    def run(self, config: ProcessConfig) -> ProcessResult:
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

            if media_execution_mode == "fresh_run" and transcript_result.lines:
                detected_language = self._detect_transcript_language(transcript_result.lines)
                if detected_language != "en":
                    raise ValueError(
                        f"当前只支持英文视频翻译。检测到转录稿语言为非英文"
                        f"（英文字符占比过低）。请确认输入的视频是英文内容。"
                    )

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
                    for spk_id, spk_info in _review_speaker_styles.items():
                        name = spk_info.get("name", "")
                        if name:
                            _review_speaker_names[spk_id] = name
                            if spk_id == "speaker_a" and speaker_name_a_is_placeholder:
                                speaker_name_a = name
                            elif spk_id == "speaker_b" and speaker_name_b_is_placeholder:
                                speaker_name_b = name
                    print(f"[S2] 已有审校结果，跳过重新审校（{len(_review_speaker_names)} 位说话人，{len(_review_glossary)} 条术语）")
                except Exception as _s2_err:
                    print(f"[S2] 加载缓存审校结果失败 ({_s2_err})，将重新审校...")
                    s2_cache_hit = False
            elif config.skip_review:
                print("[S2] Skipping review (--skip-review).")

            if not s3_cache_hit and not s2_cache_hit and not config.skip_review:
                print("[S2] Running unified LLM transcript review (audio + text)...")
                try:
                    from src.services.transcript_reviewer import review_transcript

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
                        skip_pass1=_is_express,
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

            if s3_cache_hit:
                print("[S3] 已有翻译结果，跳过翻译")
                translation_execution_mode = "cache_restore_full"
                translation_result = self._load_translation_result(segments_path)
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
                    if _review_speaker_styles:
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
                default_model = _get_default_translation_model()
                print(f"[S3] 自动使用默认翻译模型: {default_model}")
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
                        from src.services.transcript_reviewer import review_pass3_voice_profiles

                        print("[S2.5] Running Pass 3: voice profiling...", flush=True)
                        _pass3_profiles = review_pass3_voice_profiles(
                            transcript_result.lines,
                            source_audio_path=source_audio_path if source_audio_path.exists() else None,
                            speakers=_review_speaker_styles,
                            video_title=download_result.video_title,
                            debug_output_dir=final_project_dir / "transcript",
                        )
                    except Exception as exc:
                        print(f"[S2.5] Pass 3 failed (non-fatal): {exc}", flush=True)
                        logger.warning("[S2.5] Pass 3 voice profiling failed: %s", exc)
                if _pass3_profiles:
                    for spk_id, profile in _pass3_profiles.items():
                        if spk_id in _review_speaker_styles:
                            existing = _review_speaker_styles[spk_id]
                            for key in ("style", "voice_description", "gender", "age_group", "persona_style", "energy_level"):
                                val = profile.get(key, "")
                                if val:
                                    existing[key] = val
                    print(f"[S2.5] Pass 3 voice profiles: {len(_pass3_profiles)} speakers", flush=True)

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
                    return self._build_paused_result(
                        project_dir=final_project_dir,
                        stage=VOICE_SELECTION_REVIEW_STAGE,
                        message=review_message,
                    )

            # --- S3 Translation (voice already confirmed above) ---
            if s3_cache_hit:
                self._apply_runtime_voice_overrides(
                    translation_result.segments,
                    voice_id_a=voice_id_a,
                    display_name_a=speaker_name_a,
                    voice_id_b=voice_id_b,
                    display_name_b=speaker_name_b,
                    speaker_voices=_speaker_voices if effective_speakers > 2 else None,
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
                )
                print(f"[S3] 完成：共 {translation_result.total_segments} 段")

            self._apply_review_speaker_styles_to_segments(
                translation_result.segments,
                _review_speaker_styles,
            )
            self._log_review_speaker_styles(_review_speaker_styles)

            state_manager.set_stage(
                current_stage_name,
                StageStatus.DONE,
                self._build_media_understanding_stage_payload(
                    transcript_result=transcript_result,
                    effective_speakers=effective_speakers,
                    execution_mode=media_execution_mode,
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
            tts_dir = (final_project_dir / "tts").resolve(strict=False)
            rewriter_kwargs: dict[str, object] = {}
            custom_rewrite_prompt_template = gemini_config.get("rewrite_prompt_template")
            if custom_rewrite_prompt_template is not None:
                rewriter_kwargs["rewrite_prompt_template"] = str(custom_rewrite_prompt_template)
            if s3_cache_hit:
                cached_segments, segments_needing_tts = self._hydrate_cached_tts_segments(
                    translation_result.segments,
                    tts_dir,
                )
                pre_tts_chars_per_second = 4.5
                pre_tts_chars_per_second_by_speaker: dict[str, float] = {}
                if cached_segments:
                    pre_tts_chars_per_second, pre_tts_chars_per_second_by_speaker = (
                        self._calibrate_tts_duration(cached_segments)
                    )
                pre_tts_rewriter = GeminiRewriter(
                    translator,
                    chars_per_second=pre_tts_chars_per_second,
                    chars_per_second_by_speaker=pre_tts_chars_per_second_by_speaker,
                    **rewriter_kwargs,
                )
                pre_tts_rewrite_count = self._pre_rewrite_obvious_overshoot_segments_before_tts(
                    segments=segments_needing_tts,
                    rewriter=pre_tts_rewriter,
                    chars_per_second=pre_tts_chars_per_second,
                    chars_per_second_by_speaker=pre_tts_chars_per_second_by_speaker,
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
                    tts_generator.generate_all(segments_needing_tts, str(tts_dir))
                    print(
                        f"[S4] 完成：复用 {len(cached_segments)} 段缓存，"
                        f"新生成 {len(segments_needing_tts)} 段"
                    )
                else:
                    print("[S4] 所有TTS音频已缓存，跳过生成")
            else:
                if _is_pre_tts_rewrite_enabled():
                    pre_tts_rewriter = GeminiRewriter(translator, **rewriter_kwargs)
                    pre_tts_rewrite_count = self._pre_rewrite_obvious_overshoot_segments_before_tts(
                        segments=translation_result.segments,
                        rewriter=pre_tts_rewriter,
                        chars_per_second=pre_tts_rewriter.chars_per_second,
                        chars_per_second_by_speaker=pre_tts_rewriter.chars_per_second_by_speaker,
                    )
                    if pre_tts_rewrite_count > 0:
                        print(
                            f"[S4] Pre-rewrote {pre_tts_rewrite_count} obvious long segment(s) "
                            "before TTS generation."
                        )
                else:
                    print("[S4] Pre-TTS 预重写已关闭（管理员设置）")
                print("[S4] 生成TTS音频...")
                tts_results = tts_generator.generate_all(
                    translation_result.segments,
                    str(tts_dir),
                )
                print(f"[S4] 完成：生成 {len(tts_results)} 个音频片段")

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

            # V3-4/V3-5: report pipeline metering to Gateway (best-effort)
            if config.job_id and hasattr(translation_result, "segments"):
                # V3-5: sum billed_chars from TTSResult (truthful TTS-layer source)
                _tts_billed = None
                try:
                    _tts_billed = sum(getattr(r, "billed_chars", 0) for r in tts_results)
                except Exception:
                    pass
                _report_job_metering(
                    config.job_id, translation_result.segments,
                    tts_billed_chars=_tts_billed if _tts_billed else None,
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

    def _build_voice_review_payload(self, exc: VoiceReviewRequiredError) -> dict[str, object]:
        sample_duration_s = _coerce_float(exc.sample_metrics.get("duration_s"), default=0.0)
        silence_ratio = _coerce_float(exc.sample_metrics.get("silence_ratio"), default=0.0)
        return {
            "reason": "sample_too_short",
            "message": str(exc),
            "speakers": [
                {
                    "speaker_id": exc.speaker_id,
                    "speaker_label": exc.speaker_label,
                    "speaker_name": exc.speaker_name,
                    "voice_arg_name": exc.voice_arg_name,
                    "sample_path": exc.sample_path,
                    "sample_duration_s": round(sample_duration_s, 1),
                    "silence_ratio": round(silence_ratio, 2),
                }
            ],
        }

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
        """Best-effort notify gateway to mark voice as expired in user's library."""
        if not job_id:
            return
        try:
            from urllib import request as urllib_request
            import json as _json
            req = urllib_request.Request(
                "http://127.0.0.1:8880/internal/user-voices/expire",
                data=_json.dumps({"job_id": job_id, "voice_id": voice_id}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib_request.urlopen(req, timeout=5)
        except Exception:
            pass

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
    ) -> dict[str, object]:
        """Build pending payload for voice_selection_review stage."""
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
                    voices.append({"voice_id": vid, "label": lbl, "gender": str(v.get("gender", "")), "provider": prov})

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
                    voices.append({"voice_id": vid, "label": lbl, "gender": str(v.get("gender", "")), "provider": prov})

            elif prov == "minimax":
                from services.tts.minimax_voice_selector import _load_minimax_pool
                for v in _load_minimax_pool():
                    if v.get("language") not in ("中文-普通话", "中文-粤语"):
                        continue
                    vid = str(v.get("voice_id", ""))
                    lbl = str(v.get("display_name", v.get("name", vid)))
                    display_map[vid] = lbl
                    voices.append({"voice_id": vid, "label": lbl, "gender": str(v.get("gender", "")), "provider": prov})

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
        ) -> dict[str, object] | None:
            if not gender:
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
                ))
                dmap = all_display_maps.get(prov, {})
                matched_name = dmap.get(result.voice_id, result.voice_id)
                return {"voice_id": result.voice_id, "label": matched_name, "match_confidence": result.match_confidence}
            except Exception:
                return None

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
            can_clone = total_dur >= 10  # clone eligibility (MiniMax-only in frontend)

            # Auto-match: default provider (backward compat)
            profile = speaker_profiles.get(sid, {})
            g = profile.get("gender", "")
            ag = profile.get("age_group", "")
            ps = profile.get("persona_style", "")
            el = profile.get("energy_level", "")
            auto_matched = _auto_match_for_provider(tts_provider, g, ag, ps, el)

            # Auto-match: all three providers
            auto_matched_by_provider: dict[str, object] = {}
            for prov in ("minimax", "cosyvoice", "volcengine"):
                auto_matched_by_provider[prov] = _auto_match_for_provider(prov, g, ag, ps, el)

            speakers_payload.append({
                "speaker_id": sid,
                "speaker_name": speaker_names.get(sid, sid),
                "segment_count": len(segs),
                "total_duration_s": round(total_dur, 1),
                "auto_matched_voice": auto_matched,
                "auto_matched_by_provider": auto_matched_by_provider,
                "can_clone": can_clone,
                "segments": segs_sorted,
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
            runtime_file = _Path("/opt/aivideotrans/config/pricing_runtime.json")
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
        segments = [DubbingSegment(**segment_payload) for segment_payload in payload.get("segments", [])]
        return TranslationResult(
            segments=segments,
            total_segments=_coerce_int(payload.get("total_segments"), default=len(segments)),
            output_path=str(segments_path.resolve(strict=False)),
        )

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

    def _resolve_or_auto_clone_voice(
        self,
        *,
        speaker_id: str,
        transcript_result: TranscriptResult,
        audio_path: Path,
        final_project_dir: Path,
        speaker_name: str,
        voice_registry_path: str,
        tts_config: TTSConfig,
        skip_voice_registry_lookup: bool,
    ) -> str:
        speaker_label = "Speaker B" if speaker_id == "speaker_b" else "Speaker A"
        voice_arg_name = "voice-b" if speaker_id == "speaker_b" else "voice-a"
        if skip_voice_registry_lookup:
            print(
                f"[S2] {speaker_label} 仍为默认占位名，跳过音色库通用命中，"
                "优先克隆当前视频音色..."
            )
        else:
            print(f"[S2] 音色库查找 {speaker_label} ({speaker_name})...")
            try:
                resolved_voice_ids = lookup_voice_ids(
                    {speaker_id: speaker_name},
                    voice_registry_path=voice_registry_path,
                )
                voice_id = resolved_voice_ids[speaker_id]
                print(f"[S2] 音色库命中：voice_id = {voice_id}")
                return voice_id
            except VoiceLookupError as exc:
                if "Missing voice_id" not in str(exc):
                    raise

        print(f"[S2] {speaker_label} 未找到，开始自动提取样本...")
        speaker_lines = [line for line in transcript_result.lines if line.speaker_id == speaker_id]
        extractor = VoiceSampleExtractor()
        sample_dir = (final_project_dir / "voice_samples").resolve(strict=False)
        sample_dir.mkdir(parents=True, exist_ok=True)
        sample_path = sample_dir / f"{_slugify_text(speaker_name)}_sample.wav"
        extracted_sample_path = extractor.extract_sample(
            str(audio_path),
            speaker_lines,
            str(sample_path),
        )
        sample_metrics = extractor.validate_sample(extracted_sample_path)
        print(
            "[S2] 样本提取完成："
            f"{sample_metrics['duration_s']}秒，RMS {sample_metrics['rms_dbfs']}dBFS"
        )
        warnings = sample_metrics.get("warnings", [])
        if isinstance(warnings, list) and warnings:
            print(f"[S2] 样本警告：{'；'.join(str(item) for item in warnings)}")
        sample_duration_s = float(sample_metrics.get("duration_s") or 0.0)
        if sample_duration_s < MIN_SAMPLE_DURATION_SECONDS:
            raise VoiceReviewRequiredError(
                speaker_id=speaker_id,
                speaker_label=speaker_label,
                speaker_name=speaker_name,
                voice_arg_name=voice_arg_name,
                sample_path=extracted_sample_path,
                sample_metrics=sample_metrics,
                message=(
                    f"{speaker_label} 自动克隆失败：提取到的样本仅 {sample_duration_s:.1f} 秒，"
                    f"低于 MiniMax 克隆要求的最小时长 {MIN_SAMPLE_DURATION_SECONDS:.1f} 秒。"
                    f"请手工提供 --{voice_arg_name}，或改用该说话人语音更长的素材。"
                ),
            )

        clone_runtime_config = VoiceCloneConfig.from_env()
        clone_api_key = clone_runtime_config.resolved_api_key() or tts_config.api_key
        clone_base_url = clone_runtime_config.base_url or tts_config.base_url or "https://api.minimaxi.com"
        cloner = AutoVoiceCloner(
            api_key=clone_api_key,
            base_url=clone_base_url,
        )
        print(f"[S2] 正在克隆 {speaker_label} 音色...")
        clone_config_payload = getattr(cloner, "clone_config", None)
        if clone_config_payload is not None:
            clone_config_payload.timeout_seconds = clone_runtime_config.timeout_seconds
            clone_config_payload.max_retries = clone_runtime_config.max_retries
            clone_config_payload.retry_backoff_seconds = (
                clone_runtime_config.retry_backoff_seconds
            )
        voice_id = cloner.clone_voice(extracted_sample_path, speaker_name)
        print(f"[S2] {speaker_label} 克隆成功：voice_id = {voice_id}")

        if not cloner.wait_until_ready(voice_id):
            raise AutoCloneError(
                f"{speaker_label} 自动克隆完成，但音色在等待时间内未就绪。"
                f"请稍后重试或手工提供 --{voice_arg_name}。"
            )

        cloner.register_voice(
            voice_id=voice_id,
            speaker_name=speaker_name,
            sample_path=extracted_sample_path,
            voice_registry_path=voice_registry_path,
        )
        print(f"[S2] {speaker_label} 已写入音色库")
        return voice_id

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
    ) -> int:
        rewritten_count = 0

        for segment in segments:
            target_duration_ms = int(segment.target_duration_ms)
            if target_duration_ms < PRE_TTS_REWRITE_MIN_TARGET_MS:
                continue
            if target_duration_ms <= 0:
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

            overshoot_ratio = (estimated_duration_ms - target_duration_ms) / target_duration_ms
            undershoot_ratio = (target_duration_ms - estimated_duration_ms) / target_duration_ms

            needs_rewrite = False
            rewrite_label = ""
            if overshoot_ratio >= PRE_TTS_REWRITE_OVERSHOOT_RATIO:
                needs_rewrite = True
                rewrite_label = "overshoot"
            elif undershoot_ratio > PRE_TTS_REWRITE_UNDERSHOOT_RATIO:
                needs_rewrite = True
                rewrite_label = "undershoot"

            if not needs_rewrite:
                continue

            rewritten_text = rewriter.rewrite_for_duration(
                current_text,
                actual_duration_ms=estimated_duration_ms,
                target_duration_ms=target_duration_ms,
                source_text=segment.source_text,
                speaker_id=segment.speaker_id,
            ).strip()
            if not rewritten_text or rewritten_text == current_text:
                continue

            segment.cn_text = rewritten_text
            segment.rewrite_count += 1
            rewritten_count += 1
            print(
                f"[S4] Pre-TTS rewrite ({rewrite_label}) segment_{segment.segment_id:03d}: "
                f"estimate {estimated_duration_ms}ms -> target {target_duration_ms}ms"
            )

        return rewritten_count

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
            tts_generator.generate_all(child_segments, str(tts_dir))
            updated_segments.extend(child_segments)
            next_segment_id = max(child.segment_id for child in child_segments) + 1
            presplit_count += 1

        if presplit_count > 0:
            translation_result.segments = updated_segments
            translation_result.total_segments = len(updated_segments)
        return presplit_count

    def _should_presplit_segment_before_alignment(self, segment: DubbingSegment) -> bool:
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
        tts_generator.generate_all(child_segments, str(tts_dir))
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
            child_segments.append(
                DubbingSegment(
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
            )
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
            tts_generator.generate_all([child_segment], str(tts_dir))

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
                    aligned_audio_path=str(segment.aligned_audio_path or ""),
                    actual_duration_ms=int(segment.actual_duration_ms),
                    alignment_method=segment.alignment_method,
                    needs_review=segment.needs_review,
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
                targets=[OutputTarget.EDITOR],
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
            blocks.append(
                SemanticBlock(
                    block_id=f"segment_{int(segment.segment_id):03d}",
                    speaker_id=segment.speaker_id,
                    speaker_name=segment.display_name,
                    original_srt_indices=[int(segment.segment_id)],
                    first_start_ms=int(segment.start_ms),
                    last_end_ms=int(segment.end_ms),
                    target_duration_ms=int(segment.target_duration_ms),
                    merged_cn_text=segment.cn_text,
                    actual_audio_duration_ms=int(segment.actual_duration_ms),
                    rewrite_count=int(segment.rewrite_count),
                    tts_audio_path=_normalize_optional_text(segment.tts_audio_path),
                    aligned_audio_path=_normalize_optional_text(segment.aligned_audio_path),
                    status=self._resolve_process_output_block_status(segment),
                    alignment_method=segment.alignment_method or "direct",
                    needs_review=bool(segment.needs_review),
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

    def _build_media_understanding_stage_payload(
        self,
        *,
        transcript_result: TranscriptResult,
        effective_speakers: int,
        execution_mode: str,
    ) -> dict[str, object]:
        speaker_ids = self._detect_speaker_ids(transcript_result.lines)
        transcript_artifacts = [
            transcript_result.raw_response_path,
            transcript_result.structured_transcript_path,
        ]
        return {
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
        if segment.alignment_method == "force_dsp":
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
    ) -> tuple[float, dict[str, float]]:
        global_estimator = TTSDurationEstimator(chars_per_second=4.5)
        global_samples = [
            (segment.cn_text, segment.actual_duration_ms)
            for segment in segments
            if segment.actual_duration_ms > 0
        ]
        global_estimator.calibrate(global_samples)

        speaker_samples: dict[str, list[tuple[str, int]]] = {}
        for segment in segments:
            if segment.actual_duration_ms <= 0:
                continue
            speaker_samples.setdefault(segment.speaker_id, []).append(
                (segment.cn_text, segment.actual_duration_ms)
            )

        chars_per_second_by_speaker: dict[str, float] = {}
        for speaker_id, samples in speaker_samples.items():
            if len(samples) < DEFAULT_SPEAKER_TTS_CALIBRATION_MIN_SAMPLES:
                continue
            speaker_estimator = TTSDurationEstimator(chars_per_second=global_estimator.chars_per_second)
            speaker_estimator.calibrate(samples)
            chars_per_second_by_speaker[speaker_id] = speaker_estimator.chars_per_second

        return global_estimator.chars_per_second, chars_per_second_by_speaker

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
    )
