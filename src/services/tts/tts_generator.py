from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import json
import os
from pathlib import Path
import threading
import time
from typing import Any
from urllib import error, request

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
            return self._generate_all_parallel(segments, output_root, total_segments)

        return self._generate_all_sequential(segments, output_root, total_segments)

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
    ) -> list[TTSResult]:
        """Sequential TTS generation with rate limiting (Tier 1: ≤30min videos)."""
        results: list[TTSResult] = []
        rate_limiter = self._get_rate_limiter()
        for index, segment in enumerate(segments, start=1):
            result = self._process_segment(segment, output_root, index, total_segments, rate_limiter)
            results.append(result)
        return results

    def _generate_all_parallel(
        self,
        segments: list[DubbingSegment],
        output_root: Path,
        total_segments: int,
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
            result = self._process_segment(segment, output_root, index, total_segments, rate_limiter, quiet=True)
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
    ) -> TTSResult:
        """Process a single segment: check cache → rate limit → generate → update segment."""
        output_path = output_root / f"segment_{segment.segment_id:03d}_{segment.speaker_id}.wav"

        if is_valid_output(str(output_path)):
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
            rate_limiter.wait()
            result = self._generate_one_with_backoff(segment, str(output_root))

        segment.tts_audio_path = result.audio_path
        segment.actual_duration_ms = result.duration_ms
        if result.selected_voice:
            segment.selected_voice = result.selected_voice
        if result.match_confidence:
            segment.match_confidence = result.match_confidence
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
            # Fall back to demographic selector when the segment voice_id is not
            # a compatible builtin preset for the active CosyVoice model.
            from services.tts.cosyvoice_instruction_enhancer import enhance_voice_selection
            from services.tts.cosyvoice_voice_selector import infer_is_childlike

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
            childlike = infer_is_childlike(age_group or "", voice_desc)
            enhanced = enhance_voice_selection(
                gender=gender, age_group=age_group,
                persona_style=persona, energy_level=energy,
                is_childlike=childlike,
            )
            voice = enhanced.voice_id
            confidence = enhanced.match_confidence
            resolution_source = f"enhancer({confidence})"
            # Cache the auto-matched result for this speaker
            self._speaker_voice_cache[segment.speaker_id] = (voice, confidence)
            print(
                f"[CosyVoice] Speaker cache set: {segment.speaker_id} → {voice}",
                flush=True,
            )

        print(
            f"[CosyVoice] voice={voice}, confidence={confidence}, gender={gender}, age={age_group}, "
            f"persona={persona}, energy={energy}, source={resolution_source}, "
            f"text={tts_text[:50]}...",
            flush=True,
        )

        try:
            audio_bytes = cosyvoice_synthesize(text=tts_text, voice=voice)
        except Exception as exc:
            if voice != cosyvoice_default_voice and _is_invalid_cosyvoice_voice_error(exc):
                print(
                    f"[CosyVoice] selected voice {voice} was rejected; retrying with safe default "
                    f"{cosyvoice_default_voice}.",
                    flush=True,
                )
                voice = cosyvoice_default_voice
                confidence = "low"
                audio_bytes = cosyvoice_synthesize(text=tts_text, voice=voice)
            else:
                raise
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
        # tts_model for volcengine = req_params.model (e.g. "seed-tts-1.1" or None)
        model = tts_model

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
            gender = getattr(segment, "gender", None)
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
            ))
            voice_id = match_result.voice_id
            confidence = match_result.match_confidence
            resolution_source = f"resolver({match_result.match_reason})"
            # Cache auto-matched result for this speaker
            self._speaker_voice_cache[segment.speaker_id] = (voice_id, confidence)
            print(
                f"[VolcEngine] Speaker cache set: {segment.speaker_id} → {voice_id}",
                flush=True,
            )

        print(
            f"[VolcEngine] voice={voice_id}, resource={resource_id}, model={model}, "
            f"confidence={confidence}, source={resolution_source}, "
            f"text={tts_text[:50]}...",
            flush=True,
        )

        # --- 3. Call provider with mismatch retry ---
        try:
            audio_bytes = vc_synthesize(
                text=tts_text,
                voice_id=voice_id,
                resource_id=resource_id,
                model=model,
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
                )
            else:
                raise

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
    ) -> TTSResult:
        """Wrap _generate_one with exponential backoff + fallback provider chain."""
        provider = getattr(self, "_job_provider", None) or get_tts_provider()
        max_attempts = len(self._OUTER_BACKOFF_SCHEDULE)
        last_error: Exception | None = None

        # --- Primary provider attempts ---
        for attempt in range(1, max_attempts + 1):
            try:
                return self._generate_one(segment, output_dir, provider=provider)
            except TTSGenerationError as exc:
                last_error = exc
                if attempt < max_attempts:
                    wait = self._OUTER_BACKOFF_SCHEDULE[attempt - 1]
                    print(
                        f"[S4] TTS 段 {segment.segment_id} ({provider}) 失败，"
                        f"{wait}s 后重试 ({attempt}/{max_attempts})..."
                    )
                    time.sleep(wait)

        # --- Fallback provider ---
        voice_clone_enabled = bool(getattr(segment, "voice_id", None))
        fallback = get_fallback_provider(provider, voice_clone_enabled)
        if fallback:
            print(
                f"[S4] TTS 段 {segment.segment_id} 主 provider ({provider}) 耗尽，"
                f"尝试 fallback → {fallback}"
            )
            try:
                return self._generate_one(segment, output_dir, provider=fallback)
            except TTSGenerationError as fb_exc:
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
            return self._generate_one(segment, output_dir, provider=provider)
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
    ) -> TTSResult:
        output_root = Path(output_dir).resolve(strict=False)
        output_root.mkdir(parents=True, exist_ok=True)

        tts_text = _normalize_optional_text(segment.tts_cn_text) or _normalize_optional_text(segment.cn_text)
        if tts_text is None:
            raise TTSGenerationError("segment.tts_cn_text or segment.cn_text is required.")

        # V3-5: record billed chars per provider's billing unit.
        # Frozen V3 doc: MiniMax and CosyVoice bill "1 汉字 = 2 计费字符".
        # VolcEngine bills on raw character count (no multiplier).
        # MiMo bills on tokens (not chars) — billed_chars left as 0 (unknown).
        _cn_chars = len(tts_text)

        # Resolve provider: explicit arg > job-level > legacy
        if provider is None:
            provider = getattr(self, "_job_provider", None) or get_tts_provider()

        # Dispatch: cosyvoice / mimo / minimax (default)
        # Wrap provider-specific exceptions as TTSGenerationError so
        # _generate_one_with_backoff can catch them uniformly.
        if provider == "cosyvoice":
            try:
                result = self._generate_one_cosyvoice(segment, tts_text, output_root)
                result.billed_chars = _cn_chars * 2  # 阿里云百炼: 1 汉字 = 2 计费字符
                return result
            except TTSGenerationError:
                raise
            except Exception as exc:
                raise TTSGenerationError(f"CosyVoice: {exc}") from exc
        if provider == "mimo":
            try:
                result = self._generate_one_mimo(segment, tts_text, output_root)
                # MiMo: token-based billing, truthful billed_chars unavailable
                # result.billed_chars stays 0 (default)
                return result
            except TTSGenerationError:
                raise
            except Exception as exc:
                raise TTSGenerationError(f"MiMo: {exc}") from exc
        if provider == "volcengine":
            try:
                result = self._generate_one_volcengine(segment, tts_text, output_root)
                result.billed_chars = _cn_chars  # VolcEngine: direct char billing
                return result
            except TTSGenerationError:
                raise
            except Exception as exc:
                raise TTSGenerationError(f"VolcEngine: {exc}") from exc

        endpoint = _build_tts_endpoint(self.config.base_url)
        payload = {
            "model": self.config.model,
            "text": tts_text,
            "voice_setting": {
                "voice_id": segment.voice_id,
                "speed": self.config.speed,
                "vol": self.config.vol,
            },
            "audio_setting": {
                "format": self.config.audio_format,
                "sample_rate": 24000,
            },
        }

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

        return TTSResult(
            segment_id=segment.segment_id,
            audio_path=str(output_path.resolve(strict=False)),
            duration_ms=duration_ms,
            voice_id=segment.voice_id,
            billed_chars=_cn_chars * 2,  # MiniMax: 1 汉字 = 2 计费字符
        )


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


def _normalize_optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


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
