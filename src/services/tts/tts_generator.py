from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any
from urllib import error, request

logger = logging.getLogger(__name__)

from services.gemini.translator import DubbingSegment
from utils.audio_utils import measure_duration_ms as _ffprobe_duration_ms
from utils.atomic_io import atomic_write_bytes, is_valid_output
from services.tts.rate_limiter import RateLimiter
from services.tts.tts_strategy import (
    get_tts_provider,
    get_tts_provider_for_job,
    get_tts_rpm,
    get_fallback_provider,
)
from services.tts.cosyvoice_voice_catalog import is_cosyvoice_v3_flash_builtin_voice
from services.usage_meter import TTS_BUCKET_FIRST
import re as _re


def _normalize_mimo_style(raw: str) -> str:
    """Normalize voice_description for MiMo <style> tag.

    Strips person name prefix, keeps short style traits:
    性别 / 年龄 / 音色 / 语速 / 气质

    Example:
        "查理·芒格，年迈男性，声音低沉沙哑，语速缓慢，带有智慧感"
        → "年迈男性，低沉沙哑，语速缓慢，睿智沉稳"
    """
    if not raw:
        return ""
    # Split by comma
    parts = [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()]
    # Drop parts that look like person names (contain · like 查理·芒格)
    filtered = []
    for p in parts:
        if "·" in p:
            continue
        # Skip overly long descriptive phrases (>15 chars)
        if len(p) > 15:
            # Try to extract core trait
            core = p
            for prefix in ["带有", "具有", "略带", "偶尔", "偶有"]:
                if core.startswith(prefix):
                    core = core[len(prefix):]
                    break
            if len(core) <= 10:
                filtered.append(core)
            continue
        filtered.append(p)
    return "，".join(filtered[:6])  # Keep max 6 traits

try:
    import requests
except ImportError:  # pragma: no cover - depends on local environment
    requests = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_AUTODUB_LOCAL_CONFIG_PATH = PROJECT_ROOT / "autodub.local.json"
DEFAULT_BASE_URL = "https://api.minimaxi.com"
DEFAULT_MODEL = "speech-2.8-turbo"
MINIMAX_TTS_MODELS = frozenset({DEFAULT_MODEL, "speech-2.8-hd"})
DEFAULT_AUDIO_FORMAT = "wav"
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_RETRIES = 5
DEFAULT_RETRY_BACKOFF_SECONDS = 5.0


class TTSGenerationError(Exception):
    pass


def _is_invalid_cosyvoice_voice_error(exc: Exception) -> bool:
    lowered = str(exc).lower()
    return (
        "returned none for voice=" in lowered
        or "does not match the selected model" in lowered
        or ("invalidparameter" in lowered and "voice" in lowered)
    )


_VOLCENGINE_AUTO_VOICE_IDS = frozenset({"auto", "auto_match", ""})


def _is_volcengine_usable_explicit_voice(voice_id: str, resource_id: str) -> bool:
    """Return True if *voice_id* is a concrete, resource-compatible VolcEngine voice.

    Returns False when:
    - voice_id is the ``"auto"`` placeholder (studio auto-match)
    - voice_id is not found in the catalog for the given *resource_id*

    Uses the catalog's ``is_voice_in_resource()`` for dynamic lookup instead
    of hardcoded suffix checks, since 1.0/2.0 voices have multiple suffix
    formats (``_moon_bigtts``, ``_mars_bigtts``, ``_emo_v2_mars_bigtts``,
    ``ICL_*_tob``, ``saturn_*_tob``, etc.).
    """
    if voice_id.lower().strip() in _VOLCENGINE_AUTO_VOICE_IDS:
        return False
    from services.tts.volcengine_voice_catalog import is_voice_in_resource
    return is_voice_in_resource(voice_id, resource_id)


def _is_volcengine_voice_resource_mismatch(exc: Exception) -> bool:
    """Detect VolcEngine errors indicating a voice/resource_id incompatibility.

    Known error codes from production testing:
    - 45000000: invalid speaker
    - 55000000: speaker / resource mismatch
    Also matches keyword patterns in case new codes appear.
    """
    msg = str(exc).lower()
    if any(code in msg for code in ("45000000", "55000000")):
        return True
    keywords = ("speaker", "voice", "resource", "invalid", "mismatch")
    return sum(1 for kw in keywords if kw in msg) >= 2


def _read_job_field(job_record: Any, key: str) -> Any:
    if isinstance(job_record, dict):
        return job_record.get(key)
    return getattr(job_record, key, None)


def _resolve_minimax_model_for_job(job_record: Any, fallback_model: str) -> str:
    raw_model = _read_job_field(job_record, "tts_model") if job_record is not None else None
    model = _normalize_optional_text(raw_model)
    if model in MINIMAX_TTS_MODELS:
        return model
    return _normalize_optional_text(fallback_model) or DEFAULT_MODEL


@dataclass(slots=True)
class TTSConfig:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    speed: float = 1.0
    vol: float = 1.0
    audio_format: str = DEFAULT_AUDIO_FORMAT
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS


@dataclass(slots=True)
class TTSResult:
    segment_id: int
    audio_path: str
    duration_ms: int
    voice_id: str
    selected_voice: str = ""
    match_confidence: str = ""
    billed_chars: int = 0  # V3-5: actual chars submitted to TTS provider
    # T7: populated when primary provider failed and fallback (e.g. MiniMax →
    # CosyVoice) produced the audio. None means primary succeeded; the string
    # is the fallback provider name. Surfaces in segment manifest so users can
    # audit which segments used a different voice than they selected.
    fallback_used_provider: str | None = None


class TTSGenerator:
    def __init__(self, config: TTSConfig, *, job_record: Any = None):
        normalized_api_key = _normalize_optional_text(config.api_key)
        if normalized_api_key is None:
            raise TTSGenerationError("TTS api_key is required.")

        self.config = TTSConfig(
            api_key=normalized_api_key,
            base_url=_normalize_optional_text(config.base_url) or DEFAULT_BASE_URL,
            model=_normalize_optional_text(config.model) or DEFAULT_MODEL,
            speed=float(config.speed),
            vol=float(config.vol),
            audio_format=_normalize_optional_text(config.audio_format) or DEFAULT_AUDIO_FORMAT,
            timeout_seconds=max(1.0, float(config.timeout_seconds)),
            max_retries=max(0, int(config.max_retries)),
            retry_backoff_seconds=max(0.0, float(config.retry_backoff_seconds)),
        )
        self._default_job_record = job_record

        # Speaker-level voice cache: caches auto-matched voices so that
        # multiple segments from the same speaker_id use a consistent voice.
        # Only stores results from automatic matching (enhancer / resolver);
        # explicit voice_id or user-selected voices are NOT cached here.
        # Key: speaker_id → (voice_id, confidence)
        self._speaker_voice_cache: dict[str, tuple[str, str]] = {}

        # Phase 2 Task 1 — per-speaker chars_per_second map for the speed
        # decision logic. Populated by the pipeline after S4 catalog/probe
        # calibration; left empty when caller hasn't set it (which makes
        # speed_decision return missing_inputs and fall back to speed=1.0).
        self._chars_per_second_by_speaker: dict[str, float] = {}
        self._global_chars_per_second: float | None = None
        self._usage_meter: Any | None = None
        # Phase 2a free tier — set by the pipeline (set_voice_strategy) before
        # generate_all. Gates the MiMo voiceclone dispatch: only a
        # "free_voiceclone" job routes a reference-bearing segment to voiceclone
        # (defense in depth — a stray voiceclone_reference_path on a non-free
        # MiMo segment must NOT trigger a clone).
        self._voice_strategy: str = ""

    def set_usage_meter(self, usage_meter: Any | None) -> None:
        self._usage_meter = usage_meter

    def set_voice_strategy(self, voice_strategy: str | None) -> None:
        """Inject the job's voice_strategy before generate_all (Phase 2a free
        tier). Gates the MiMo voiceclone dispatch — only ``"free_voiceclone"``
        jobs clone the original speaker; any other value keeps the base MiMo
        preset path."""
        self._voice_strategy = (voice_strategy or "").strip()

    def set_speaker_chars_per_second(
        self,
        per_speaker: dict[str, float] | None,
        *,
        global_cps: float | None = None,
    ) -> None:
        """Inject per-speaker chars/sec for the Task 1 speed decision.

        Called by the pipeline right before generate_all() so the TTS
        layer knows how fast each speaker's voice will read the text.
        Safe to call with None / empty dict — speed adjustment will then
        short-circuit to ``missing_inputs``.
        """
        self._chars_per_second_by_speaker = dict(per_speaker or {})
        self._global_chars_per_second = global_cps

    # ≤100 segments: sequential (simple, reliable)
    # >100 segments: 3-worker parallel (3x throughput for long videos)
    _PARALLEL_THRESHOLD = 100
    _PARALLEL_WORKERS = 3

    def _resolve_provider_decision(
        self, *, job_record: Any = None
    ) -> dict[str, str]:
        """Resolve TTS provider and record decision source.

        Returns ``{"provider": "<name>", "source": "job_record"|"global_default"}``.
        """
        if job_record is not None:
            provider = get_tts_provider_for_job(job_record)
            return {"provider": provider, "source": "job_record"}
        return {"provider": get_tts_provider(), "source": "global_default"}

    def generate_all(
        self,
        segments: list[DubbingSegment],
        output_dir: str,
        *,
        job_record: Any = None,
        usage_bucket: str = TTS_BUCKET_FIRST,
    ) -> list[TTSResult]:
        output_root = Path(output_dir).resolve(strict=False)
        output_root.mkdir(parents=True, exist_ok=True)

        total_segments = len(segments)

        # Reset speaker voice cache per generate_all invocation
        self._speaker_voice_cache.clear()

        # Resolve provider once for the whole job.
        # Save the active job_record so that _generate_one_volcengine() can
        # read tts_model and service_mode from the record that is actually
        # in effect for this call (not just self._default_job_record).
        effective_job_record = job_record or self._default_job_record
        self._active_job_record = effective_job_record
        decision = self._resolve_provider_decision(job_record=effective_job_record)
        self._job_provider = decision["provider"]
        print(f"[S4] TTS provider: {self._job_provider} (source: {decision['source']})")

        # Count how many actually need generation (not cached)
        pending_count = sum(
            1 for seg in segments
            if not is_valid_output(
                str(output_root / f"segment_{seg.segment_id:03d}_{seg.speaker_id}.wav")
            )
        )

        if pending_count > self._PARALLEL_THRESHOLD:
            print(f"[S4] {pending_count} 段待生成，启用 {self._PARALLEL_WORKERS} 路并行 TTS")
            return self._generate_all_parallel(
                segments,
                output_root,
                total_segments,
                usage_bucket=usage_bucket,
            )

        return self._generate_all_sequential(
            segments,
            output_root,
            total_segments,
            usage_bucket=usage_bucket,
        )

    def _get_rate_limiter(self) -> RateLimiter:
        """Get rate limiter with provider-appropriate RPM."""
        provider = getattr(self, "_job_provider", None) or get_tts_provider()
        rpm = get_tts_rpm(provider)
        return RateLimiter(rpm=rpm)

    def _generate_all_sequential(
        self,
        segments: list[DubbingSegment],
        output_root: Path,
        total_segments: int,
        *,
        usage_bucket: str,
    ) -> list[TTSResult]:
        """Sequential TTS generation with rate limiting (Tier 1: ≤30min videos)."""
        results: list[TTSResult] = []
        rate_limiter = self._get_rate_limiter()
        for index, segment in enumerate(segments, start=1):
            result = self._process_segment(
                segment,
                output_root,
                index,
                total_segments,
                rate_limiter,
                usage_bucket=usage_bucket,
            )
            results.append(result)
        return results

    def _generate_all_parallel(
        self,
        segments: list[DubbingSegment],
        output_root: Path,
        total_segments: int,
        *,
        usage_bucket: str,
    ) -> list[TTSResult]:
        """Parallel TTS generation with shared rate limiter (Tier 2/3: >30min videos)."""
        # Shared rate limiter across all workers — use provider-specific RPM
        provider = getattr(self, "_job_provider", None) or get_tts_provider()
        rate_limiter = RateLimiter(rpm=get_tts_rpm(provider))
        completed_count = 0
        completed_lock = threading.Lock()
        results_dict: dict[int, TTSResult] = {}

        def _worker(index: int, segment: DubbingSegment) -> tuple[int, TTSResult]:
            nonlocal completed_count
            result = self._process_segment(
                segment,
                output_root,
                index,
                total_segments,
                rate_limiter,
                quiet=True,
                usage_bucket=usage_bucket,
            )
            with completed_lock:
                completed_count += 1
                if completed_count % 15 == 0 or completed_count == total_segments:
                    print(f"[S4] TTS 进度: {completed_count}/{total_segments} 段")
            return segment.segment_id, result

        failed_segments: list[tuple[int, DubbingSegment, Exception]] = []

        with ThreadPoolExecutor(max_workers=self._PARALLEL_WORKERS) as executor:
            futures = {
                executor.submit(_worker, idx, seg): (idx, seg)
                for idx, seg in enumerate(segments, start=1)
            }
            for future in as_completed(futures):
                idx, seg = futures[future]
                try:
                    _, result = future.result()
                    results_dict[seg.segment_id] = result
                except Exception as exc:
                    if isinstance(exc, TTSGenerationError) and _is_non_retryable_tts_input_error(exc):
                        raise TTSGenerationError(
                            f"TTS 段 {seg.segment_id} 输入无效，停止重试: {exc}"
                        ) from exc
                    print(f"[S4] TTS 段 {seg.segment_id} 失败（已保留已完成段）: {exc}")
                    failed_segments.append((idx, seg, exc))

        # Retry failed segments after a cooldown
        if failed_segments:
            completed_count = len(results_dict)
            total = len(segments)
            print(
                f"[S4] TTS {completed_count}/{total} 段已完成，"
                f"{len(failed_segments)} 段失败，5 分钟后重试…",
                flush=True,
            )
            time.sleep(300)  # 5 分钟冷却

            for idx, seg, _ in failed_segments:
                try:
                    _, result = _worker(idx, seg)
                    results_dict[seg.segment_id] = result
                    print(f"[S4] TTS 段 {seg.segment_id} 重试成功")
                except Exception as retry_exc:
                    print(f"[S4] TTS 段 {seg.segment_id} 重试仍失败: {retry_exc}")
                    raise TTSGenerationError(
                        f"TTS 段 {seg.segment_id} 在重试后仍失败: {retry_exc}"
                    ) from retry_exc

        # Return results in original segment order
        return [results_dict[seg.segment_id] for seg in segments]

    def _process_segment(
        self,
        segment: DubbingSegment,
        output_root: Path,
        index: int,
        total_segments: int,
        rate_limiter: RateLimiter,
        quiet: bool = False,
        usage_bucket: str = TTS_BUCKET_FIRST,
    ) -> TTSResult:
        """Process a single segment: check cache → rate limit → generate → update segment."""
        output_path = output_root / f"segment_{segment.segment_id:03d}_{segment.speaker_id}.wav"
        current_tts_text = _normalize_optional_text(segment.cn_text)
        stale_text_witness = False
        if is_valid_output(str(output_path)) and current_tts_text is not None:
            cached_tts_text = _normalize_optional_text(
                getattr(segment, "tts_input_cn_text", None)
            )
            stale_text_witness = (
                cached_tts_text is not None
                and _normalize_cache_text(cached_tts_text)
                != _normalize_cache_text(current_tts_text)
            )

        if is_valid_output(str(output_path)) and not stale_text_witness:
            if not quiet:
                print(f"[TTS] 跳过已完成段 {index}/{total_segments}")
            duration_ms = _ffprobe_duration_ms(output_path)
            # Preserve selected_voice/match_confidence from a previous run if
            # already on the segment; otherwise derive from explicit voice_id.
            cached_voice = getattr(segment, "selected_voice", "") or ""
            cached_conf = getattr(segment, "match_confidence", "") or ""
            if not cached_voice:
                explicit = _normalize_optional_text(getattr(segment, "voice_id", None))
                if explicit and is_cosyvoice_v3_flash_builtin_voice(explicit):
                    cached_voice = explicit
                    cached_conf = cached_conf or "high"
                else:
                    cached_conf = cached_conf or "cached"
            result = TTSResult(
                segment_id=segment.segment_id,
                audio_path=str(output_path.resolve(strict=False)),
                duration_ms=duration_ms,
                voice_id=segment.voice_id,
                selected_voice=cached_voice,
                match_confidence=cached_conf,
            )
        else:
            if stale_text_witness and not quiet:
                print(
                    f"[TTS] 段 {segment.segment_id} 文本已变化，忽略旧TTS缓存并重新合成",
                    flush=True,
                )
            rate_limiter.wait()
            result = self._generate_one_with_backoff(
                segment,
                str(output_root),
                usage_bucket=usage_bucket,
            )

        segment.tts_audio_path = result.audio_path
        segment.actual_duration_ms = result.duration_ms
        if result.selected_voice:
            segment.selected_voice = result.selected_voice
        if result.match_confidence:
            segment.match_confidence = result.match_confidence
        # T7: mirror fallback flag onto the segment so process.py's manifest
        # dict picks it up alongside selected_voice / match_confidence.
        segment.fallback_used_provider = result.fallback_used_provider
        if current_tts_text is not None:
            segment.tts_input_cn_text = current_tts_text
        if segment.target_duration_ms > 0:
            segment.alignment_ratio = result.duration_ms / segment.target_duration_ms
        else:
            segment.alignment_ratio = 0.0

        if not quiet and total_segments > 0 and (index % 15 == 0 or index == total_segments):
            print(f"[S4] TTS 进度: {index}/{total_segments} 段")

        return result

    # Outer retry backoff schedule (seconds) for _generate_one failures.
    # Each _generate_one call already does its own inner retries via _post_json;
    # this outer layer handles persistent 429/503 rate-limit / overload scenarios.
    _OUTER_BACKOFF_SCHEDULE = [5, 10, 20, 40, 60]
    _OUTER_PAUSE_SECONDS = 300  # 5-minute cooldown after exhausting backoff

    def _generate_one_mimo(
        self,
        segment: DubbingSegment,
        tts_text: str,
        output_root: Path,
    ) -> TTSResult:
        """Generate TTS via MiMo-V2-TTS API."""
        from services.tts.mimo_tts_provider import synthesize as mimo_synthesize

        output_path = output_root / f"segment_{segment.segment_id:03d}_{segment.speaker_id}.wav"

        # Pass voice_description directly to MiMo TTS as style
        mimo_style = getattr(segment, "voice_description", "") or ""
        print(f"[MiMo-TTS] style={mimo_style[:80] or '(none)'}, text={tts_text[:40]}...", flush=True)

        audio_bytes = mimo_synthesize(text=tts_text, voice_id=mimo_style)
        atomic_write_bytes(str(output_path), audio_bytes)
        duration_ms = _ffprobe_duration_ms(output_path)

        return TTSResult(
            segment_id=segment.segment_id,
            audio_path=str(output_path.resolve(strict=False)),
            duration_ms=duration_ms,
            voice_id=segment.voice_id,
        )

    def _generate_one_mimo_voiceclone(
        self,
        segment: DubbingSegment,
        tts_text: str,
        output_root: Path,
    ) -> TTSResult:
        """Free tier (Phase 2a): clone the original speaker's voice via MiMo
        voiceclone, using the per-speaker reference clip the pipeline stamped on
        ``segment.voiceclone_reference_path``. Reuses the Phase 1 provider
        primitive ``synthesize_voiceclone`` (inline base64 ref + 10MB guard).

        The no-reference case is handled by the caller's dispatch (→ base MiMo
        preset). Runtime voiceclone *failure* fallback (→ free preset, made
        visible to user/admin) is Task 6; here a failure propagates as a normal
        provider error so the caller's existing handling applies.
        """
        from services.tts.mimo_tts_provider import synthesize_voiceclone

        output_path = output_root / f"segment_{segment.segment_id:03d}_{segment.speaker_id}.wav"
        print(
            f"[MiMo-VoiceClone] ref={segment.voiceclone_reference_path}, "
            f"text={tts_text[:40]}...",
            flush=True,
        )

        audio_bytes = synthesize_voiceclone(
            tts_text, reference_audio=segment.voiceclone_reference_path
        )
        atomic_write_bytes(str(output_path), audio_bytes)
        duration_ms = _ffprobe_duration_ms(output_path)

        return TTSResult(
            segment_id=segment.segment_id,
            audio_path=str(output_path.resolve(strict=False)),
            duration_ms=duration_ms,
            voice_id=segment.voice_id,
        )

    def _generate_one_cosyvoice(
        self,
        segment: DubbingSegment,
        tts_text: str,
        output_root: Path,
    ) -> TTSResult:
        """Generate TTS via CosyVoice API."""
        from services.tts.cosyvoice_provider import DEFAULT_VOICE as cosyvoice_default_voice
        from services.tts.cosyvoice_provider import synthesize as cosyvoice_synthesize

        output_path = output_root / f"segment_{segment.segment_id:03d}_{segment.speaker_id}.wav"

        explicit_voice_id = _normalize_optional_text(getattr(segment, "voice_id", None))
        confidence = ""
        # Only a fresh resolver match writes the speaker cache, and only *after* a
        # successful synth (see below).  Caching before synth persists a voice that the
        # provider may reject, poisoning every later segment of the same speaker.
        resolved_fresh = False
        if explicit_voice_id and is_cosyvoice_v3_flash_builtin_voice(explicit_voice_id):
            # Explicit builtin voice — use directly, do NOT cache (user-chosen).
            voice = explicit_voice_id
            gender = getattr(segment, "gender", None)
            age_group = getattr(segment, "age_group", None)
            persona = getattr(segment, "persona_style", None)
            energy = getattr(segment, "energy_level", None)
            resolution_source = "explicit_builtin_voice_id"
            confidence = "high"
        elif segment.speaker_id in self._speaker_voice_cache:
            # Speaker cache hit — reuse the auto-matched voice from an earlier
            # segment of the same speaker_id.  This prevents matcher jitter.
            voice, confidence = self._speaker_voice_cache[segment.speaker_id]
            gender = getattr(segment, "gender", None)
            age_group = getattr(segment, "age_group", None)
            persona = getattr(segment, "persona_style", None)
            energy = getattr(segment, "energy_level", None)
            resolution_source = f"speaker_cache({segment.speaker_id})"
        else:
            # Auto-match via shared resolver (same pipeline as VolcEngine).
            from services.tts.voice_match_resolver import resolve_voice_match
            from services.tts.voice_match_types import VoiceMatchRequest

            gender = getattr(segment, "gender", None)
            age_group = getattr(segment, "age_group", None)
            persona = getattr(segment, "persona_style", None)
            energy = getattr(segment, "energy_level", None)
            voice_desc = getattr(segment, "voice_description", None) or ""
            if not gender:
                print(
                    f"[CosyVoice] WARNING: segment {segment.segment_id} ({segment.speaker_id}) "
                    f"has empty gender — voice matcher will use fallback",
                    flush=True,
                )
            match_result = resolve_voice_match(VoiceMatchRequest(
                tts_provider="cosyvoice",
                mode="auto",
                gender=gender,
                age_group=age_group,
                persona_style=persona,
                energy_level=energy,
                voice_description=voice_desc,
                # PR-E re-CodeX P2: drive language-aware selection in the main path.
                target_language=getattr(segment, "target_language", None),
                target_chars_per_second=(
                    float(getattr(segment, "target_chars_per_second", 0.0)) or None
                ),
            ))
            # PR-E re-CodeX P2: a fail_closed match must actually abort — returning the
            # (Chinese) fallback voice_id here would let _generate_one_cosyvoice synthesize
            # wrong-language audio. zh never produces a fail_closed reason → byte-identical.
            if str(match_result.match_reason or "").startswith("fail_closed"):
                raise TTSGenerationError(
                    f"CosyVoice has no voice for target_language="
                    f"{getattr(segment, 'target_language', None)!r}; failing closed "
                    f"({match_result.match_reason})"
                )
            voice = match_result.voice_id
            confidence = match_result.match_confidence
            resolution_source = f"resolver({match_result.match_reason})"
            # Defer caching until the synth succeeds (post-fallback voice), and only when
            # this segment carried a real gender signal — caching a gender-less fallback
            # would let a first gender-less segment poison the cache so later segments
            # that DO carry gender never re-resolve.
            resolved_fresh = True

        print(
            f"[CosyVoice] voice={voice}, confidence={confidence}, gender={gender}, age={age_group}, "
            f"persona={persona}, energy={energy}, source={resolution_source}, "
            f"text={tts_text[:50]}...",
            flush=True,
        )

        # --- Phase 2 Task 1 (CosyVoice branch, 2026-04-15): per-segment speech_rate.
        # DashScope SpeechSynthesizer accepts a float multiplier (default 1.0),
        # identical semantics to MiniMax voice_setting.speed — so no mapping
        # needed, just forward the decide_tts_speed output.
        speaker_cps = self._chars_per_second_by_speaker.get(segment.speaker_id)
        if speaker_cps is None:
            speaker_cps = self._global_chars_per_second
        try:
            from services.tts.speed_decision import decide_tts_speed
            decision = decide_tts_speed(
                cn_text=tts_text,
                target_duration_ms=int(getattr(segment, "target_duration_ms", 0) or 0),
                chars_per_second=float(speaker_cps) if speaker_cps else None,
            )
        except Exception as exc:  # never let metric path break TTS
            print(f"[CosyVoice] speed_decision exception (fallback 1.0): {exc}", flush=True)
            from services.tts.speed_decision import SpeedDecision
            decision = SpeedDecision(speed=1.0, reason="error", estimated_ms=0, ratio=0.0)

        if decision.reason in ("disabled", "missing_inputs", "error"):
            effective_speed = 1.0
        else:
            effective_speed = float(decision.speed)

        try:
            segment.dsp_speed_param = effective_speed
        except Exception:
            pass

        if decision.reason == "in_range":
            print(
                f"[CosyVoice] speed={effective_speed:.4f} "
                f"(ratio={decision.ratio:.3f}, est={decision.estimated_ms}ms)",
                flush=True,
            )

        try:
            audio_bytes = cosyvoice_synthesize(
                text=tts_text,
                voice=voice,
                speech_rate=effective_speed,
            )
        except Exception as exc:
            if voice != cosyvoice_default_voice and _is_invalid_cosyvoice_voice_error(exc):
                print(
                    f"[CosyVoice] selected voice {voice} was rejected; retrying with safe default "
                    f"{cosyvoice_default_voice}.",
                    flush=True,
                )
                voice = cosyvoice_default_voice
                confidence = "low"
                audio_bytes = cosyvoice_synthesize(
                    text=tts_text,
                    voice=voice,
                    speech_rate=effective_speed,
                )
            else:
                raise

        # Synth succeeded — now (and only now) cache the *actually used* voice for this
        # speaker.  `voice` here reflects any fallback-to-default that happened above, so
        # later segments reuse the working voice instead of re-trying a rejected one.
        # Guard on gender so a gender-less fallback match never poisons the cache.
        if resolved_fresh and gender:
            self._speaker_voice_cache[segment.speaker_id] = (voice, confidence)
            print(
                f"[CosyVoice] Speaker cache set: {segment.speaker_id} → {voice} "
                f"(confidence={confidence})",
                flush=True,
            )

        atomic_write_bytes(str(output_path), audio_bytes)
        duration_ms = _ffprobe_duration_ms(output_path)

        return TTSResult(
            segment_id=segment.segment_id,
            audio_path=str(output_path.resolve(strict=False)),
            duration_ms=duration_ms,
            voice_id=segment.voice_id,
            selected_voice=voice,
            match_confidence=confidence,
        )

    def _resolve_active_job_record(self) -> Any:
        """Return the job record in effect for the current generate_all() call."""
        return getattr(self, "_active_job_record", None) or self._default_job_record

    def _generate_one_cosyvoice_via_worker(
        self,
        segment: DubbingSegment,
        tts_text: str,
        output_root: Path,
    ) -> TTSResult:
        """Phase 4.1 D.3：CosyVoice clone voice 走武汉 mainland worker。

        当 ``segment.requires_worker=True`` 时由 ``_generate_one_cosyvoice``
        早分支调用。**严格 fail-closed** —— 失败抛 ``TTSGenerationError``，
        绝不静默 fallback 到 MiniMax / 其它 provider（CLAUDE.md 付费 API
        硬约束）。

        头部硬校验（Codex 2026-05-25 D v3 review #2）：

        - ``voice_id`` 必填（worker clone voice 已经由 C.2 endpoint 落库时
          锁定，pipeline 不再 matcher 或 default voice）
        - ``worker_target_model`` 必填（来自 ``user_voices.target_model``，
          不允许 hardcode）

        speech_rate 来源（Codex 2026-05-25 D v3 review #3）：

        - 与 legacy ``_generate_one_cosyvoice`` 完全一致地调用 ``decide_tts_speed``
          算出 ``effective_speed``，传给 ``WorkerSegmentRequest.speech_rate``。
          否则同段切到 cloned voice 后时长漂。

        重试边界（Codex 2026-05-25 D HC#1）：

        - 这里**不写任何 retry 逻辑**。唯一合法重试源是 ``MainlandWorkerClient``
          内部的单段 3 / 多段 2 上限（plan §Retry，Phase 1 Fix #5 确立）。
        - ``_generate_one_with_backoff`` 看到 ``requires_worker=True`` 走早
          分支单次调用此方法，不进 5-min final retry / fallback provider。
        """
        # 延迟 import 避免 services.tts → services.mainland_worker 在不需要
        # 走 worker 的部署里增加 import 时间
        from services.mainland_worker.client import (
            WorkerArtifactIntegrityError,
            WorkerError,
            WorkerNetworkError,
            WorkerSignatureRejectedError,
            MainlandWorkerClient,
        )
        from services.mainland_worker.client_factory import build_client_from_env
        from services.mainland_worker.types import (
            WorkerSegmentRequest,
            WorkerSynthesizeBatchRequest,
            compute_text_hash,
        )

        # ---- Head guard #1 (D v3 #2)：voice_id 必填 ----
        voice_id = _normalize_optional_text(getattr(segment, "voice_id", None))
        if not voice_id:
            raise TTSGenerationError(
                "requires_worker=True but voice_id is empty; "
                "cloned voice must have an explicit voice_id (no matcher / default fallback on worker path)"
            )

        # ---- Head guard #2 (D v3 #2)：worker_target_model 必填 ----
        target_model = _normalize_optional_text(getattr(segment, "worker_target_model", None))
        if not target_model:
            raise TTSGenerationError(
                f"requires_worker=True but worker_target_model is empty for voice_id={voice_id!r}; "
                "value must come from user_voices.target_model (no hardcoded default)"
            )

        # ---- speech_rate (D v3 #3)：与 legacy 路径完全一致的 decide_tts_speed ----
        speaker_cps = self._chars_per_second_by_speaker.get(segment.speaker_id)
        if speaker_cps is None:
            speaker_cps = self._global_chars_per_second
        try:
            from services.tts.speed_decision import decide_tts_speed
            decision = decide_tts_speed(
                cn_text=tts_text,
                target_duration_ms=int(getattr(segment, "target_duration_ms", 0) or 0),
                chars_per_second=float(speaker_cps) if speaker_cps else None,
            )
        except Exception as exc:  # 与 legacy 一致：speed_decision 异常不阻塞 TTS
            print(
                f"[CosyVoice-Worker] speed_decision exception (fallback 1.0): {exc}",
                flush=True,
            )
            from services.tts.speed_decision import SpeedDecision
            decision = SpeedDecision(speed=1.0, reason="error", estimated_ms=0, ratio=0.0)

        if decision.reason in ("disabled", "missing_inputs", "error"):
            effective_speed = 1.0
        else:
            effective_speed = float(decision.speed)

        try:
            segment.dsp_speed_param = effective_speed
        except Exception:
            pass

        # ---- 构造 client (D.2 唯一 secret 入口) ----
        client = build_client_from_env()
        if client is None:
            raise TTSGenerationError(
                "mainland voice worker unavailable (env config missing or disabled); "
                "refusing to fall back to MiniMax/other provider per CLAUDE.md paid-API constraint"
            )

        # ---- 构造 WorkerSynthesizeBatchRequest (单段 batch) ----
        job_rec = self._resolve_active_job_record()
        job_id = (
            _read_job_field(job_rec, "job_id")
            or _read_job_field(job_rec, "id")
            or "no_job"
        )

        seg_req = WorkerSegmentRequest(
            segment_id=int(segment.segment_id),
            speaker_id=str(segment.speaker_id),
            voice_id=voice_id,
            text=tts_text,
            speech_rate=effective_speed,
            target_duration_ms=int(getattr(segment, "target_duration_ms", 0) or 0) or None,
            text_hash=compute_text_hash(tts_text),
        )
        batch_req = WorkerSynthesizeBatchRequest(
            job_id=str(job_id),
            target_model=target_model,
            audio_format="wav",
            segments=(seg_req,),
        )

        output_path = output_root / f"segment_{segment.segment_id:03d}_{segment.speaker_id}.wav"

        # ---- 调 worker、解 artifact、写 wav；finally close ----
        try:
            try:
                resp = client.synthesize_batch(batch_req)
            except (WorkerNetworkError, WorkerSignatureRejectedError, WorkerError,
                    WorkerArtifactIntegrityError) as exc:
                # Codex D HC#4：转 TTSGenerationError；**不** retry，**不** fallback
                raise TTSGenerationError(
                    f"CosyVoice-Worker: {type(exc).__name__}: {exc}"
                ) from exc

            # 期望响应：单段返回，target_model 回包等于请求
            if resp.target_model != target_model:
                raise TTSGenerationError(
                    f"CosyVoice-Worker: target_model echo mismatch "
                    f"(requested {target_model!r} got {resp.target_model!r})"
                )
            if len(resp.segments) != 1:
                raise TTSGenerationError(
                    f"CosyVoice-Worker: expected 1 segment in batch response, "
                    f"got {len(resp.segments)}"
                )

            # ★ Phase 4.1 D P2-1 (Codex 2026-05-25 D 三轮 review)：单段响应除
            # target_model 外还要校验 segment_id / speaker_id / voice_id echo。
            # 与 C.2 endpoint 同类防漂移；worker / provider bug 让 voice_id
            # 漂到错误段会导致后续音频被写到错误位置。
            seg_result = resp.segments[0]
            if seg_result.segment_id != int(segment.segment_id):
                raise TTSGenerationError(
                    f"CosyVoice-Worker: segment_id echo mismatch "
                    f"(requested {segment.segment_id!r} got {seg_result.segment_id!r})"
                )
            if seg_result.speaker_id != str(segment.speaker_id):
                raise TTSGenerationError(
                    f"CosyVoice-Worker: speaker_id echo mismatch "
                    f"(requested {segment.speaker_id!r} got {seg_result.speaker_id!r})"
                )
            if seg_result.voice_id != voice_id:
                raise TTSGenerationError(
                    f"CosyVoice-Worker: voice_id echo mismatch "
                    f"(requested {voice_id!r} got {seg_result.voice_id!r})"
                )

            # 解 artifact zip 拿 wav bytes (含三层 sha256 完整性校验)
            try:
                audio_map = MainlandWorkerClient.extract_artifact_segments(resp)
            except WorkerArtifactIntegrityError as exc:
                raise TTSGenerationError(f"CosyVoice-Worker artifact: {exc}") from exc

            wav_bytes = audio_map.get(seg_result.audio_path)
            if not wav_bytes:
                raise TTSGenerationError(
                    f"CosyVoice-Worker: artifact missing audio_path={seg_result.audio_path!r}"
                )

            atomic_write_bytes(str(output_path), wav_bytes)
            duration_ms = _ffprobe_duration_ms(output_path)

            print(
                f"[CosyVoice-Worker] voice={voice_id} target_model={target_model} "
                f"speed={effective_speed:.4f} worker_request_id={resp.worker_request_id} "
                f"provider_request_id={seg_result.provider_request_id} "
                f"billed_chars={seg_result.billed_chars} duration_ms={duration_ms} "
                f"text={tts_text[:50]}...",
                flush=True,
            )

            # Phase 4.1 D P2-2 (Codex 2026-05-25 D 三轮 review)：``match_confidence``
            # 既有契约枚举 ``"high" / "medium" / "low"``（见 DubbingSegment 注释 +
            # cosyvoice_voice_selector / minimax_voice_selector 等下游消费者）。
            # 用户显式选了 cloned voice + worker 端 echo 一致 → 等价于既有
            # "explicit_voice_id" 路径的 high confidence。避免引入 enum 外的
            # ``"explicit_worker_voice"`` 让 analytics / UI 看到非预期值。
            return TTSResult(
                segment_id=segment.segment_id,
                audio_path=str(output_path.resolve(strict=False)),
                duration_ms=duration_ms,
                voice_id=voice_id,
                selected_voice=voice_id,
                match_confidence="high",
                billed_chars=seg_result.billed_chars,  # worker authoritative (HC#2)
            )
        finally:
            try:
                client.close()
            except Exception:  # pragma: no cover — close 失败不阻塞主流
                pass

    def _generate_one_volcengine(
        self,
        segment: DubbingSegment,
        tts_text: str,
        output_root: Path,
    ) -> TTSResult:
        """Generate TTS via VolcEngine (豆包) V3 Chunked API — dual-mode (1.0 / 2.0).

        Three independent concepts drive the request:

        * **resource_id** — derived at runtime from ``tts_provider + service_mode``
          (NOT stored in DB).  Written to ``X-Api-Resource-Id`` header.
        * **model** — read from job snapshot ``tts_model``.  For volcengine this
          means ``req_params.model`` (e.g. ``"seed-tts-1.1"`` for express, *None*
          for studio 2.0 public voices).
        * **voice_id** — resolved via explicit segment.voice_id → speaker cache →
          shared resolver auto-match → resource default.
        """
        from services.tts.volcengine_tts_provider import (
            RESOURCE_ID_1_0,
            RESOURCE_ID_2_0,
            default_speaker_for_resource,
            synthesize as vc_synthesize,
        )
        from services.tts.voice_match_types import VoiceMatchRequest
        from services.tts.voice_match_resolver import resolve_voice_match

        output_path = output_root / f"segment_{segment.segment_id:03d}_{segment.speaker_id}.wav"

        # --- 1. Derive resource_id from tts_provider + service_mode ---
        job_rec = self._resolve_active_job_record()
        service_mode = None
        tts_model = None
        if job_rec is not None:
            service_mode = (
                job_rec.get("service_mode") if isinstance(job_rec, dict)
                else getattr(job_rec, "service_mode", None)
            )
            raw_model = (
                job_rec.get("tts_model") if isinstance(job_rec, dict)
                else getattr(job_rec, "tts_model", None)
            )
            tts_model = _normalize_optional_text(raw_model)

        resource_id = RESOURCE_ID_2_0 if service_mode == "studio" else RESOURCE_ID_1_0
        # tts_model for volcengine = req_params.model (e.g. "seed-tts-1.1" or None).
        # Ignore non-volcengine models (e.g. "speech-2.8-hd" from MiniMax jobs
        # when per-speaker provider override switches to volcengine).
        model = tts_model if tts_model and tts_model.startswith("seed-tts") else None

        # --- 2. Voice selection (priority chain per plan §5.7) ---
        #
        # An explicit voice is only usable when it is:
        #   a) not the literal string "auto" (studio auto-match placeholder), AND
        #   b) compatible with the current resource_id (suffix check).
        # Otherwise it falls through to the resolver auto-match path.
        raw_explicit = _normalize_optional_text(getattr(segment, "voice_id", None))
        explicit_voice = (
            raw_explicit
            if raw_explicit and _is_volcengine_usable_explicit_voice(raw_explicit, resource_id)
            else None
        )
        fallback_voice = default_speaker_for_resource(resource_id)
        confidence = ""
        resolution_source = ""
        # Hoisted so the post-synth cache guard can read it in every branch.
        gender = getattr(segment, "gender", None)
        # Only a fresh resolver match writes the speaker cache, and only after a
        # successful synth (mirrors the CosyVoice path) — caching before synth would
        # persist a voice the provider then rejects, poisoning every later segment.
        resolved_fresh = False

        if explicit_voice:
            # Explicit segment.voice_id — compatible with resource, use directly, do NOT cache
            voice_id = explicit_voice
            confidence = "high"
            resolution_source = "explicit_voice_id"
        elif segment.speaker_id in self._speaker_voice_cache:
            # Speaker cache hit — reuse auto-matched voice
            voice_id, confidence = self._speaker_voice_cache[segment.speaker_id]
            resolution_source = f"speaker_cache({segment.speaker_id})"
        else:
            # Auto-match via shared resolver
            if not gender:
                print(
                    f"[VolcEngine] WARNING: segment {segment.segment_id} ({segment.speaker_id}) "
                    f"has empty gender — matcher will use fallback",
                    flush=True,
                )
            match_result = resolve_voice_match(VoiceMatchRequest(
                tts_provider="volcengine",
                resource_id=resource_id,
                mode="auto",
                gender=gender,
                age_group=getattr(segment, "age_group", None),
                persona_style=getattr(segment, "persona_style", None),
                energy_level=getattr(segment, "energy_level", None),
                voice_description=getattr(segment, "voice_description", None),
                # PR-E re-CodeX P2: drive language-aware selection in the main path.
                target_language=getattr(segment, "target_language", None),
                target_chars_per_second=(
                    float(getattr(segment, "target_chars_per_second", 0.0)) or None
                ),
            ))
            # PR-E re-CodeX P2: a fail_closed match must abort, not synthesize the
            # (Chinese) fallback voice for a non-zh target. zh never fail_closes →
            # byte-identical.
            if str(match_result.match_reason or "").startswith("fail_closed"):
                raise TTSGenerationError(
                    f"VolcEngine has no voice for target_language="
                    f"{getattr(segment, 'target_language', None)!r}; failing closed "
                    f"({match_result.match_reason})"
                )
            voice_id = match_result.voice_id
            confidence = match_result.match_confidence
            resolution_source = f"resolver({match_result.match_reason})"
            # Defer caching until the synth succeeds (post-mismatch-retry voice), guarded
            # on gender below.
            resolved_fresh = True

        print(
            f"[VolcEngine] voice={voice_id}, resource={resource_id}, model={model}, "
            f"confidence={confidence}, source={resolution_source}, "
            f"text={tts_text[:50]}...",
            flush=True,
        )

        # --- Phase 2 Task 1 (VolcEngine branch, 2026-04-15): per-segment
        # speech_rate. Mirrors the MiniMax path: reuse the speaker's cps
        # (catalog or probe value piped in by pipeline.set_speaker_chars_per_second),
        # call the shared decide_tts_speed decision tree, and map the
        # MiniMax-style multiplier to VolcEngine's integer speech_rate.
        # Empirically validated 2026-04-15: speed=1.15 -> speech_rate=+15
        # produces a duration ratio within <2% of MiniMax's speed=1.15.
        speaker_cps = self._chars_per_second_by_speaker.get(segment.speaker_id)
        if speaker_cps is None:
            speaker_cps = self._global_chars_per_second
        try:
            from services.tts.speed_decision import (
                decide_tts_speed,
                speed_to_volcengine_speech_rate,
            )
            decision = decide_tts_speed(
                cn_text=tts_text,
                target_duration_ms=int(getattr(segment, "target_duration_ms", 0) or 0),
                chars_per_second=float(speaker_cps) if speaker_cps else None,
            )
        except Exception as exc:  # never let metric path break TTS
            print(f"[VolcEngine] speed_decision exception (fallback 1.0): {exc}", flush=True)
            from services.tts.speed_decision import SpeedDecision
            decision = SpeedDecision(speed=1.0, reason="error", estimated_ms=0, ratio=0.0)

        if decision.reason in ("disabled", "missing_inputs", "error"):
            effective_speed = 1.0
            speech_rate_param = 0
        else:
            effective_speed = float(decision.speed)
            speech_rate_param = speed_to_volcengine_speech_rate(effective_speed)

        # Stamp the metric on the segment for metering aggregation.
        try:
            segment.dsp_speed_param = effective_speed
        except Exception:
            pass

        if decision.reason == "in_range":
            print(
                f"[VolcEngine] speed={effective_speed:.4f} -> speech_rate={speech_rate_param:+d} "
                f"(ratio={decision.ratio:.3f}, est={decision.estimated_ms}ms)",
                flush=True,
            )

        # --- 3. Call provider with mismatch retry ---
        # PR-E slice 4: pass the dub target language so VolcEngine can hint
        # explicit_language for a non-zh dub (default zh → omitted → byte-identical).
        _vc_target_language = getattr(segment, "target_language", None)
        try:
            audio_bytes = vc_synthesize(
                text=tts_text,
                voice_id=voice_id,
                resource_id=resource_id,
                model=model,
                speech_rate=speech_rate_param,
                target_language=_vc_target_language,
            )
        except Exception as exc:
            if voice_id != fallback_voice and _is_volcengine_voice_resource_mismatch(exc):
                print(
                    f"[VolcEngine] Voice {voice_id} / resource {resource_id} mismatch; "
                    f"retrying with default {fallback_voice}",
                    flush=True,
                )
                voice_id = fallback_voice
                confidence = "low"
                resolution_source = "mismatch_retry"
                audio_bytes = vc_synthesize(
                    text=tts_text,
                    voice_id=voice_id,
                    resource_id=resource_id,
                    model=model,
                    speech_rate=speech_rate_param,
                    target_language=_vc_target_language,
                )
            else:
                raise

        # Synth succeeded — cache the *actually used* voice (post-mismatch-retry) for this
        # speaker, only after success and only with a real gender signal (see CosyVoice
        # path for rationale).
        if resolved_fresh and gender:
            self._speaker_voice_cache[segment.speaker_id] = (voice_id, confidence)
            print(
                f"[VolcEngine] Speaker cache set: {segment.speaker_id} → {voice_id} "
                f"(confidence={confidence})",
                flush=True,
            )

        atomic_write_bytes(str(output_path), audio_bytes)
        duration_ms = _ffprobe_duration_ms(output_path)

        return TTSResult(
            segment_id=segment.segment_id,
            audio_path=str(output_path.resolve(strict=False)),
            duration_ms=duration_ms,
            voice_id=segment.voice_id,
            selected_voice=voice_id,
            match_confidence=confidence,
        )

    def _generate_one_with_backoff(
        self,
        segment: DubbingSegment,
        output_dir: str,
        *,
        usage_bucket: str = TTS_BUCKET_FIRST,
    ) -> TTSResult:
        """Wrap _generate_one with exponential backoff + fallback provider chain."""
        # Phase 4.1 D.5 (Codex 2026-05-25 v3 review #1)：先按现有逻辑解析
        # ``provider``，再判断 ``requires_worker``，避免漂移 provider 解析路径。
        provider = getattr(self, "_job_provider", None) or get_tts_provider()

        # === Phase 4.1 D.5 HC#1 early branch ===
        # ``requires_worker=True`` 时 **单次** 调 ``_generate_one`` 并立即返回/抛错。
        # 唯一合法重试源是 ``MainlandWorkerClient`` 内部（单段 3 / 多段 2，plan §Retry）。
        # 这里**不进** ``_OUTER_BACKOFF_SCHEDULE`` 循环、**不 sleep**、**不**走
        # ``get_fallback_provider`` 改调 MiniMax 等其它 provider（CLAUDE.md 付费
        # API 硬约束 + 客户期望同一克隆音色不被静默切换）。
        if bool(getattr(segment, "requires_worker", False)):
            return self._generate_one(
                segment,
                output_dir,
                provider=provider,
                usage_bucket=usage_bucket,
            )

        max_attempts = len(self._OUTER_BACKOFF_SCHEDULE)
        last_error: Exception | None = None

        # --- Primary provider attempts ---
        for attempt in range(1, max_attempts + 1):
            try:
                return self._generate_one(
                    segment,
                    output_dir,
                    provider=provider,
                    usage_bucket=usage_bucket,
                )
            except TTSGenerationError as exc:
                last_error = exc
                if _is_non_retryable_tts_input_error(exc):
                    raise
                if attempt < max_attempts:
                    wait = self._OUTER_BACKOFF_SCHEDULE[attempt - 1]
                    print(
                        f"[S4] TTS 段 {segment.segment_id} ({provider}) 失败，"
                        f"{wait}s 后重试 ({attempt}/{max_attempts})..."
                    )
                    time.sleep(wait)

        # --- Phase 2a Task 6 (gate #6): free voiceclone -> base MiMo preset ---
        # MiMo voiceclone is run-to-run unstable on long input (Phase 1 finding).
        # When a free segment's voiceclone retries are exhausted, DEGRADE to the
        # base MiMo preset (free, SAME provider) instead of failing the job —
        # and NEVER to a paid provider/clone (get_fallback_provider("mimo") is
        # None anyway; this branch runs first and returns). The substitution is
        # made VISIBLE (logger.warning + console + result.fallback_used_provider)
        # so user/admin see it — never silent (CLAUDE.md + plan Task 6).
        if (
            getattr(self, "_voice_strategy", "") == "free_voiceclone"
            and getattr(segment, "voiceclone_reference_path", None)
        ):
            logger.warning(
                "free_voiceclone_fallback_to_preset segment=%s reason=%s",
                segment.segment_id, last_error,
            )
            print(
                f"[S4] 免费版声音克隆段 {segment.segment_id} 重试耗尽，"
                f"回落 MiMo 基础预设音色（不切换付费引擎）"
            )
            try:
                result = self._generate_one(
                    segment,
                    output_dir,
                    provider="mimo",
                    usage_bucket=usage_bucket,
                    force_mimo_preset=True,
                )
                result.fallback_used_provider = "mimo_preset"
                return result
            except TTSGenerationError as preset_exc:
                logger.error(
                    "free_voiceclone_preset_fallback_failed segment=%s error=%s",
                    segment.segment_id, preset_exc,
                )
                # fall through to the generic handling below

        # --- Fallback provider ---
        voice_clone_enabled = bool(getattr(segment, "voice_id", None))
        # PR-E slice 3: pass the dub target language so a non-zh dub never falls back
        # to the Chinese-only CosyVoice (fail-closed). Default (no attr → zh) unchanged.
        _seg_target_language = getattr(segment, "target_language", None)
        fallback = get_fallback_provider(provider, voice_clone_enabled, _seg_target_language)
        if fallback:
            # T7: user-visible warning AND structured log for traceability.
            # The primary provider here is the one the user selected (e.g.
            # MiniMax); silently switching to CosyVoice would alter the audio
            # character without the user knowing. We can't easily change the
            # fallback behavior itself this batch (would require surfacing the
            # decision to the review UI), but we guarantee the substitution is
            # discoverable: (a) structured log line for operators, (b) the
            # returned TTSResult carries fallback_used_provider so downstream
            # persistence (process.py segment manifest) records it.
            logger.warning(
                "tts_fallback_triggered segment=%s primary=%s fallback=%s reason=%s",
                segment.segment_id, provider, fallback, last_error,
            )
            print(
                f"[S4] TTS 段 {segment.segment_id} 主 provider ({provider}) 耗尽，"
                f"尝试 fallback → {fallback}"
            )
            try:
                result = self._generate_one(
                    segment,
                    output_dir,
                    provider=fallback,
                    usage_bucket=usage_bucket,
                )
                result.fallback_used_provider = fallback
                return result
            except TTSGenerationError as fb_exc:
                logger.error(
                    "tts_fallback_failed segment=%s fallback=%s error=%s",
                    segment.segment_id, fallback, fb_exc,
                )
                print(
                    f"[S4] TTS 段 {segment.segment_id} fallback ({fallback}) 也失败: {fb_exc}"
                )
                # Continue to pause-and-retry below

        # All normal attempts exhausted — pause 5 minutes then try once more
        print(
            f"[S4] TTS 段 {segment.segment_id} 连续 {max_attempts} 次失败，"
            f"暂停 {self._OUTER_PAUSE_SECONDS}s 后最后重试..."
        )
        time.sleep(self._OUTER_PAUSE_SECONDS)

        try:
            return self._generate_one(
                segment,
                output_dir,
                provider=provider,
                usage_bucket=usage_bucket,
            )
        except TTSGenerationError:
            # Final failure — let the caller handle it (checkpoint already saved)
            raise TTSGenerationError(
                f"TTS 段 {segment.segment_id} 在 {max_attempts} 次重试 + "
                f"{self._OUTER_PAUSE_SECONDS}s 暂停后仍然失败: {last_error}"
            ) from last_error

    def _generate_one(
        self,
        segment: DubbingSegment,
        output_dir: str,
        *,
        provider: str | None = None,
        usage_bucket: str = TTS_BUCKET_FIRST,
        force_mimo_preset: bool = False,
    ) -> TTSResult:
        output_root = Path(output_dir).resolve(strict=False)
        output_root.mkdir(parents=True, exist_ok=True)

        tts_text = _normalize_optional_text(segment.cn_text)
        if tts_text is None:
            raise TTSGenerationError("segment.cn_text is required.")

        # V3-5: record billed chars per provider's billing unit.
        # Frozen V3 doc: MiniMax and CosyVoice bill "1 汉字 = 2 计费字符".
        # VolcEngine bills on raw character count (no multiplier).
        # MiMo bills on tokens (not chars) — billed_chars left as 0 (unknown).
        _cn_chars = len(tts_text)

        # Resolve provider: force_mimo_preset > segment override > explicit arg > job-level > legacy
        segment_provider = getattr(segment, "tts_provider", None)
        if force_mimo_preset:
            # Task 6 (gate #6) hard pin (CodeX P1): the free-voiceclone preset
            # fallback MUST stay on MiMo (free) regardless of any
            # segment.tts_provider drift / upstream bug AND regardless of
            # requires_worker — a stray tts_provider="minimax" must never route
            # the free fallback into a paid provider / worker (CLAUDE.md).
            provider = "mimo"
        elif segment_provider:
            provider = segment_provider
        elif provider is None:
            provider = getattr(self, "_job_provider", None) or get_tts_provider()

        # ★ Phase 4.1 D P1 fix (Codex 2026-05-25 D review)：``requires_worker=True``
        # 必须强制走 cosyvoice 分支（→ worker path）。否则 E 阶段 segment producer
        # 漏写 ``tts_provider="cosyvoice"`` + job-level provider 是 MiniMax 时，
        # CosyVoice cloned voice_id 会被发到 MiniMax，触发错误付费 provider 调用。
        # 这正违反 D 核心约束。
        #
        # 两种 fail-closed 形态（**只看 segment.tts_provider 不看 resolved
        # provider**，否则会把"job-level fallback 到 MiniMax 但 segment 自己
        # 没显式 mismatch"误判为数据不一致）：
        #
        #   1) ``segment.tts_provider`` 非空且 ≠ ``"cosyvoice"`` → 显式数据
        #      不一致（用户/producer 主动配错），**抛错**拒绝付费
        #   2) ``segment.tts_provider`` 空 / None → 强制 ``provider="cosyvoice"``
        #      覆盖 job-level fallback；clone voice 只能在 cosyvoice provider 下用，
        #      强制锁定不产生歧义。
        if not force_mimo_preset and bool(getattr(segment, "requires_worker", False)):
            seg_tts_provider = (getattr(segment, "tts_provider", "") or "").strip()
            if seg_tts_provider and seg_tts_provider != "cosyvoice":
                raise TTSGenerationError(
                    f"requires_worker=True but segment.tts_provider={seg_tts_provider!r}; "
                    f"CosyVoice cloned voice (voice_id={getattr(segment, 'voice_id', None)!r}) "
                    f"cannot be used with non-cosyvoice provider. "
                    f"Refusing to call paid {seg_tts_provider!r} provider with mismatched voice."
                )
            # 强制锁定走 worker 分支；覆盖 job-level / default 解析结果
            provider = "cosyvoice"

        # Dispatch: cosyvoice / mimo / minimax (default)
        # Wrap provider-specific exceptions as TTSGenerationError so
        # _generate_one_with_backoff can catch them uniformly.
        if provider == "cosyvoice":
            # Phase 4.1 D.3+D.4 (2026-05-25 Codex 三签字)：
            # ``requires_worker=True`` 走武汉 mainland worker，**不允许** 静默
            # fallback 到国际 DashScope endpoint，**不允许** 用 ``len(text)*2``
            # 覆盖 worker 的 authoritative billed_chars。
            requires_worker = bool(getattr(segment, "requires_worker", False))
            try:
                if requires_worker:
                    result = self._generate_one_cosyvoice_via_worker(
                        segment, tts_text, output_root,
                    )
                    # HC#2：worker 路径保留 worker 返回的 billed_chars（来自
                    # Phase 4.0b billing_character_count，DashScope 真实计费规则）。
                    # 不 overwrite。
                else:
                    result = self._generate_one_cosyvoice(segment, tts_text, output_root)
                    # Legacy 国际 DashScope 路径：阿里云百炼 1 汉字 = 2 计费字符
                    result.billed_chars = _cn_chars * 2
                self._record_tts_usage(
                    result,
                    bucket=usage_bucket,
                    provider="cosyvoice",
                    text=tts_text,
                )
                return result
            except TTSGenerationError:
                raise
            except Exception as exc:
                raise TTSGenerationError(f"CosyVoice: {exc}") from exc
        if provider == "mimo":
            try:
                # Free tier (Phase 2a): clone the original speaker via MiMo
                # voiceclone ONLY when this is a free_voiceclone job AND the
                # pipeline stamped a per-speaker reference on the segment.
                # Requiring the voice_strategy (not the reference alone) is
                # defense in depth: a stray voiceclone_reference_path on a
                # non-free MiMo segment can never trigger a clone. No reference
                # / non-free → base MiMo preset (unchanged for existing jobs).
                if (
                    not force_mimo_preset
                    and getattr(self, "_voice_strategy", "") == "free_voiceclone"
                    and getattr(segment, "voiceclone_reference_path", None)
                ):
                    result = self._generate_one_mimo_voiceclone(segment, tts_text, output_root)
                else:
                    result = self._generate_one_mimo(segment, tts_text, output_root)
                # MiMo: token-based billing, truthful billed_chars unavailable
                # result.billed_chars stays 0 (default)
                self._record_tts_usage(
                    result,
                    bucket=usage_bucket,
                    provider="mimo",
                    text=tts_text,
                )
                return result
            except TTSGenerationError:
                raise
            except Exception as exc:
                raise TTSGenerationError(f"MiMo: {exc}") from exc
        if provider == "volcengine":
            try:
                result = self._generate_one_volcengine(segment, tts_text, output_root)
                result.billed_chars = _cn_chars  # VolcEngine: direct char billing
                self._record_tts_usage(
                    result,
                    bucket=usage_bucket,
                    provider="volcengine",
                    text=tts_text,
                )
                return result
            except TTSGenerationError:
                raise
            except Exception as exc:
                raise TTSGenerationError(f"VolcEngine: {exc}") from exc

        # --- MiniMax voice resolution (same pattern as VolcEngine/CosyVoice) ---
        explicit_voice = _normalize_optional_text(getattr(segment, "voice_id", None))
        mm_confidence = ""
        mm_resolution = ""
        # Hoisted so the post-synth cache guard can read it in every branch.
        mm_gender = getattr(segment, "gender", None)
        # Only a fresh resolver match writes the speaker cache, and only after a
        # successful synth (mirrors CosyVoice/VolcEngine) — caching before synth would
        # persist a voice the provider then rejects, poisoning every retry / later segment.
        mm_resolved_fresh = False

        if explicit_voice and explicit_voice != "auto":
            # Explicit voice_id — use directly, do NOT cache (user-chosen).
            mm_voice = explicit_voice
            mm_confidence = "high"
            mm_resolution = "explicit_voice_id"
        elif segment.speaker_id in self._speaker_voice_cache:
            mm_voice, mm_confidence = self._speaker_voice_cache[segment.speaker_id]
            mm_resolution = f"speaker_cache({segment.speaker_id})"
        else:
            # Auto-match via shared resolver
            from services.tts.voice_match_resolver import resolve_voice_match
            from services.tts.voice_match_types import VoiceMatchRequest

            if not mm_gender:
                print(
                    f"[MiniMax] WARNING: segment {segment.segment_id} ({segment.speaker_id}) "
                    f"has empty gender — matcher will use fallback",
                    flush=True,
                )
            match_result = resolve_voice_match(VoiceMatchRequest(
                tts_provider="minimax",
                mode="auto",
                gender=mm_gender,
                age_group=getattr(segment, "age_group", None),
                persona_style=getattr(segment, "persona_style", None),
                energy_level=getattr(segment, "energy_level", None),
                voice_description=getattr(segment, "voice_description", None),
                target_language=getattr(segment, "target_language", None),
                target_chars_per_second=(
                    float(getattr(segment, "target_chars_per_second", 0.0)) or None
                ),
            ))
            mm_voice = match_result.voice_id
            mm_confidence = match_result.match_confidence
            mm_resolution = f"resolver({match_result.match_reason})"
            # Defer caching until the synth succeeds, guarded on gender below.
            mm_resolved_fresh = True

        segment_model = _normalize_optional_text(
            getattr(segment, "tts_model_key", None)
        )
        minimax_model = (
            segment_model
            if segment_model in MINIMAX_TTS_MODELS
            else _resolve_minimax_model_for_job(
                self._resolve_active_job_record(),
                self.config.model,
            )
        )

        print(
            f"[MiniMax] voice={mm_voice}, confidence={mm_confidence}, "
            f"source={mm_resolution}, model={minimax_model}, text={tts_text[:50]}...",
            flush=True,
        )

        # --- Phase 2 Task 1: per-segment speed decision (MiniMax only) ---
        # Resolve the chars-per-second for this speaker (catalog or probe value
        # piped in by pipeline.set_speaker_chars_per_second()), then ask
        # speed_decision module whether to deviate from 1.0.  When the feature
        # flag is off, the decision is "disabled" → speed stays self.config.speed.
        speaker_cps = self._chars_per_second_by_speaker.get(segment.speaker_id)
        if speaker_cps is None:
            speaker_cps = self._global_chars_per_second
        try:
            from services.tts.speed_decision import decide_tts_speed
            decision = decide_tts_speed(
                cn_text=tts_text,
                target_duration_ms=int(getattr(segment, "target_duration_ms", 0) or 0),
                chars_per_second=float(speaker_cps) if speaker_cps else None,
            )
        except Exception as exc:  # never let metric path break TTS
            print(f"[MiniMax] speed_decision exception (fallback 1.0): {exc}", flush=True)
            from services.tts.speed_decision import SpeedDecision  # local import to avoid bootstrap cycles
            decision = SpeedDecision(speed=1.0, reason="error", estimated_ms=0, ratio=0.0)

        # When disabled or fallback, honor the global config speed (legacy behavior).
        # When enabled and the decision returned a non-1.0 speed, use it.
        if decision.reason in ("disabled", "missing_inputs", "error"):
            effective_speed = float(self.config.speed)
        else:
            effective_speed = float(decision.speed)

        # Stamp the metric on the segment so Task 0's metering aggregator
        # can build the speed_param_distribution histogram.
        try:
            segment.dsp_speed_param = effective_speed
            segment.tts_model_key = minimax_model
        except Exception:
            pass  # best-effort; ignore if segment is read-only somehow

        if decision.reason in ("in_range",):
            print(
                f"[MiniMax] speed={effective_speed:.4f} (ratio={decision.ratio:.3f}, "
                f"reason={decision.reason}, est={decision.estimated_ms}ms)",
                flush=True,
            )

        endpoint = _build_tts_endpoint(self.config.base_url)
        payload = {
            "model": minimax_model,
            "text": tts_text,
            "voice_setting": {
                "voice_id": mm_voice,
                "speed": effective_speed,
                "vol": self.config.vol,
            },
            "audio_setting": {
                "format": self.config.audio_format,
                "sample_rate": 24000,
            },
        }
        # PR-E slice 4: hint the dub language for a non-zh target (MiniMax
        # language_boost). Default zh / no target → key omitted → byte-identical.
        _mm_target = getattr(segment, "target_language", None)
        if _mm_target and _mm_target.split("-")[0].lower() == "en":
            payload["language_boost"] = "English"

        response_payload = _post_json(
            endpoint=endpoint,
            api_key=self.config.api_key,
            payload=payload,
            timeout_seconds=self.config.timeout_seconds,
            max_retries=self.config.max_retries,
            retry_backoff_seconds=self.config.retry_backoff_seconds,
        )
        base_resp = response_payload.get("base_resp")
        if not isinstance(base_resp, dict):
            raise TTSGenerationError("MiniMax TTS response is missing base_resp.")
        status_code = _coerce_int(base_resp.get("status_code"), default=-1)
        status_msg = _normalize_optional_text(base_resp.get("status_msg")) or "unknown error"
        if status_code != 0:
            raise TTSGenerationError(
                f"MiniMax TTS business error: status_code={status_code} status_msg={status_msg}"
            )

        data = response_payload.get("data")
        if not isinstance(data, dict):
            raise TTSGenerationError("MiniMax TTS response is missing data.")
        audio_hex = _normalize_optional_text(data.get("audio"))
        if audio_hex is None:
            raise TTSGenerationError("MiniMax TTS response is missing data.audio.")
        try:
            audio_bytes = bytes.fromhex(audio_hex)
        except ValueError as exc:
            raise TTSGenerationError("MiniMax TTS audio payload is not valid hex.") from exc

        output_path = output_root / f"segment_{segment.segment_id:03d}_{segment.speaker_id}.wav"
        try:
            atomic_write_bytes(str(output_path), audio_bytes)
            duration_ms = _ffprobe_duration_ms(output_path)
        except OSError as exc:
            raise TTSGenerationError(f"Failed to write or read TTS audio output: {output_path}") from exc
        except Exception as exc:
            raise TTSGenerationError(f"Failed to decode generated wav audio: {output_path}") from exc

        # Synth + write succeeded — cache the voice for this speaker, only after success
        # and only with a real gender signal (see CosyVoice path for rationale).
        if mm_resolved_fresh and mm_gender:
            self._speaker_voice_cache[segment.speaker_id] = (mm_voice, mm_confidence)
            print(
                f"[MiniMax] Speaker cache set: {segment.speaker_id} → {mm_voice} "
                f"(confidence={mm_confidence})",
                flush=True,
            )

        result = TTSResult(
            segment_id=segment.segment_id,
            audio_path=str(output_path.resolve(strict=False)),
            duration_ms=duration_ms,
            voice_id=segment.voice_id,
            selected_voice=mm_voice,
            match_confidence=mm_confidence,
            billed_chars=_cn_chars * 2,  # MiniMax: 1 汉字 = 2 计费字符
        )
        self._record_tts_usage(
            result,
            bucket=usage_bucket,
            provider=str(provider or "minimax"),
            model=minimax_model,
            text=tts_text,
        )
        return result

    def _record_tts_usage(
        self,
        result: TTSResult,
        *,
        bucket: str,
        provider: str,
        model: str | None = None,
        text: str,
    ) -> None:
        meter = getattr(self, "_usage_meter", None)
        if meter is None:
            return
        try:
            meter.record_tts(
                bucket=bucket,
                provider=provider,
                model=_normalize_optional_text(model) or self.config.model,
                text=text,
                billed_chars=result.billed_chars,
                segment_id=result.segment_id,
                voice_id=result.voice_id,
                selected_voice=result.selected_voice,
                duration_ms=result.duration_ms,
                fallback_used_provider=result.fallback_used_provider,
            )
        except Exception as exc:
            print(f"[metering] TTS usage record skipped: {exc}", flush=True)


def load_tts_config() -> TTSConfig:
    config_path = DEFAULT_AUTODUB_LOCAL_CONFIG_PATH.resolve(strict=False)
    payload: dict[str, object] = {}

    if config_path.exists():
        try:
            loaded_payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TTSGenerationError(f"Failed to load TTS config from {config_path}") from exc
        if not isinstance(loaded_payload, dict):
            raise TTSGenerationError(f"TTS config file must contain a top-level JSON object: {config_path}")
        payload = loaded_payload

    section = payload.get("tts", {})
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise TTSGenerationError("tts config section must be a JSON object.")

    api_key_env_var = _normalize_optional_text(section.get("api_key_env_var")) or "AUTODUB_TTS_API_KEY"
    api_key = _normalize_optional_text(section.get("api_key"))
    if api_key is None:
        api_key = _normalize_optional_text(os.getenv(api_key_env_var))
    if api_key is None:
        raise TTSGenerationError(
            f"TTS API key is required via autodub.local.json or env {api_key_env_var}."
        )

    return TTSConfig(
        api_key=api_key,
        base_url=_normalize_optional_text(section.get("base_url")) or DEFAULT_BASE_URL,
        model=_normalize_optional_text(section.get("model_name")) or DEFAULT_MODEL,
        speed=_coerce_float(section.get("speed"), default=1.0),
        vol=_coerce_float(section.get("vol"), default=1.0),
        audio_format=_normalize_optional_text(section.get("audio_format")) or DEFAULT_AUDIO_FORMAT,
        timeout_seconds=_coerce_float(section.get("timeout_seconds"), default=DEFAULT_TIMEOUT_SECONDS),
        max_retries=_coerce_int(section.get("max_retries"), default=DEFAULT_MAX_RETRIES),
        retry_backoff_seconds=_coerce_float(
            section.get("retry_backoff_seconds"),
            default=DEFAULT_RETRY_BACKOFF_SECONDS,
        ),
    )


def _post_json(
    *,
    endpoint: str,
    api_key: str,
    payload: dict[str, object],
    timeout_seconds: float,
    max_retries: int,
    retry_backoff_seconds: float,
) -> dict[str, object]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    last_error: TTSGenerationError | None = None
    for attempt in range(max_retries + 1):
        try:
            if requests is not None:
                response = requests.post(
                    endpoint,
                    headers=headers,
                    json=payload,
                    timeout=timeout_seconds,
                )
                status_code = _coerce_int(getattr(response, "status_code", None), default=0)
                if status_code != 200:
                    if _is_retryable_http_status(status_code):
                        raise TTSGenerationError(f"MiniMax TTS HTTP error: status_code={status_code}")
                    raise TTSGenerationError(f"MiniMax TTS HTTP error: status_code={status_code}")
                try:
                    loaded = response.json()
                except Exception as exc:
                    raise TTSGenerationError("MiniMax TTS response is not valid JSON.") from exc
                if not isinstance(loaded, dict):
                    raise TTSGenerationError("MiniMax TTS response JSON must be an object.")
                return loaded

            serialized_payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_obj = request.Request(endpoint, data=serialized_payload, headers=headers, method="POST")
            with request.urlopen(request_obj, timeout=timeout_seconds) as response:
                body = response.read()
                status_code = _coerce_int(getattr(response, "status", None), default=response.getcode())
            if status_code != 200:
                if _is_retryable_http_status(status_code):
                    raise TTSGenerationError(f"MiniMax TTS HTTP error: status_code={status_code}")
                raise TTSGenerationError(f"MiniMax TTS HTTP error: status_code={status_code}")
            try:
                loaded = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise TTSGenerationError("MiniMax TTS response is not valid JSON.") from exc
            if not isinstance(loaded, dict):
                raise TTSGenerationError("MiniMax TTS response JSON must be an object.")
            return loaded
        except error.HTTPError as exc:
            if _is_retryable_http_status(exc.code):
                last_error = TTSGenerationError(f"MiniMax TTS HTTP error: status_code={exc.code}")
            else:
                raise TTSGenerationError(f"MiniMax TTS HTTP error: status_code={exc.code}") from exc
        except error.URLError as exc:
            last_error = TTSGenerationError(f"MiniMax TTS request failed: {exc.reason}")
        except OSError as exc:
            last_error = TTSGenerationError(f"MiniMax TTS request failed: {exc}")
        except TTSGenerationError as exc:
            if not _is_retryable_tts_error(exc):
                raise
            last_error = exc
        except Exception as exc:
            last_error = TTSGenerationError(f"MiniMax TTS request failed: {exc}")

        if attempt < max_retries and last_error is not None:
            wait_seconds = min(retry_backoff_seconds * (2 ** attempt), 60.0)
            print(
                f"[S4] MiniMax请求失败，{wait_seconds:g}秒后重试（{attempt + 1}/{max_retries}）：{last_error}"
            )
            time.sleep(wait_seconds)
        elif last_error is not None:
            raise last_error

    raise TTSGenerationError("MiniMax TTS request failed: unknown error")


def choose_tts_strategy(total_segments: int, video_duration_min: float) -> str:
    """根据视频参数选择 TTS 策略。"""
    if video_duration_min <= 30 and total_segments <= 100:
        return "sync"
    return "async"


def _build_tts_endpoint(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1/t2a_v2"):
        return normalized
    if normalized.endswith("/v1"):
        return f"{normalized}/t2a_v2"
    return f"{normalized}/v1/t2a_v2"


def _is_retryable_http_status(status_code: int) -> bool:
    return status_code in {408, 409, 425, 429} or 500 <= status_code < 600


def _is_retryable_tts_error(error_obj: TTSGenerationError) -> bool:
    message = str(error_obj)
    return (
        "request failed" in message
        or "HTTP error: status_code=408" in message
        or "HTTP error: status_code=409" in message
        or "HTTP error: status_code=425" in message
        or "HTTP error: status_code=429" in message
        or "HTTP error: status_code=5" in message
        or "response is not valid JSON" in message
    )


def _is_non_retryable_tts_input_error(error_obj: TTSGenerationError) -> bool:
    message = str(error_obj)
    # Deterministic failures retrying cannot fix: a missing cn_text input, and a PR-E
    # language fail-closed (no target-language voice / Chinese-only provider for a non-zh
    # dub). Classifying fail-closed non-retryable avoids the 5-minute final-retry stall.
    return "segment.cn_text is required" in message or "failing closed" in message


def _normalize_optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _normalize_cache_text(value: object) -> str:
    return _re.sub(r"\s+", "", str(value or "")).strip()


def _coerce_float(value: object, *, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: object, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
