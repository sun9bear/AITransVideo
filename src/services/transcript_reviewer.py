"""Unified LLM transcript review: speaker ID, correction, text proofreading, segmentation.

Replaces 4 separate LLM calls with 1 multimodal call (audio + text).
Uses Gemini's audio understanding to hear actual voices for speaker identification.
Supports Gemini (default) or MiMo-V2-Omni as alternative review model.
Output is diff-mode: only corrections, not full rewrite.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Review model mapping — single source of truth for API model IDs
# ---------------------------------------------------------------------------
# Admin settings store *logical* names (gemini_pro, gemini, mimo_omni).
# This map resolves them to real API model IDs.  If an upstream provider
# renames a model, update ONLY this dict — no other call-site should
# hard-code raw model ID strings.
_MODEL_MAP: dict[str, str] = {
    "gemini_pro": "gemini-3.1-pro-preview",   # highest quality, ~¥2.4/h audio
    "gemini": "gemini-2.5-flash-lite",         # low cost, ~¥0.27/h audio
    "mimo_omni": "mimo-v2-omni",               # text-only alternative
}
_DEFAULT_REVIEW_MODEL = "gemini_pro"  # logical name, resolved via _MODEL_MAP

_MIMO_OMNI_API_URL = "https://api.xiaomimimo.com/v1/chat/completions"

_MAX_LINES_PER_BATCH = 200
_BATCH_OVERLAP = 20

# Audio preprocessing for review
_REVIEW_AUDIO_SAMPLE_RATE = 16_000    # 16 kHz — sufficient for speech analysis
_REVIEW_AUDIO_CHANNELS = 1            # mono
_REVIEW_AUDIO_BITRATE = "32k"         # 32 kbps opus — ~4 KB/s, transparent for speech
_REVIEW_AUDIO_BITRATE_AGGRESSIVE = "16k"  # fallback if first upload fails
_REVIEW_AUDIO_CLIP_PADDING_MS = 10_000    # ±10 s padding for batch-local clips
# Threshold: ≤20 min → whole compressed audio reused per batch;
#            >20 min → batch-local clips to avoid redundant audio tokens.
_REVIEW_AUDIO_WHOLE_FILE_THRESHOLD_MS = 20 * 60 * 1_000  # 20 minutes
_MAX_MERGE_DURATION_MS = 180_000
_MAX_EDIT_DISTANCE_RATIO = 0.3
_MIN_SPLIT_DURATION_MS = 15_000
_MAX_PAUSE_FOR_MERGE_MS = 2_000
_SHORT_QUESTION_MAX_MS = 4_000
_SHORT_BACKCHANNEL_MAX_MS = 1_200
_ANSWER_MIN_MS = 2_500
_ANSWER_CONTINUATION_MIN_MS = 1_500
_NO_AUTO_FLIP_IF_LONGER_THAN_MS = 2_000

# Gemini explicit cache requires at least 32 768 input tokens.
# 20 min audio ≈ 38 400 tokens (32 tok/s × 1200 s), safely above the threshold.
# Shorter audio may not qualify — the code will fall back to plain upload.
_GEMINI_MIN_CACHE_TOKENS = 32_768


def _resolve_model_id(logical_name: str) -> str:
    """Resolve a logical review model name to its API model ID.

    Falls back to the default model if the name is unknown.
    """
    if logical_name in _MODEL_MAP:
        return _MODEL_MAP[logical_name]
    logger.warning("[Review] Unknown review model %r, falling back to %s", logical_name, _DEFAULT_REVIEW_MODEL)
    return _MODEL_MAP[_DEFAULT_REVIEW_MODEL]


# ---------------------------------------------------------------------------
# Audio preprocessing for review
# ---------------------------------------------------------------------------

def _prepare_review_audio(
    audio_path: Path,
    tmp_dir: Path,
    *,
    bitrate: str = _REVIEW_AUDIO_BITRATE,
) -> Path:
    """Compress audio to a lightweight format suitable for LLM review upload.

    Output: 16 kHz mono opus/ogg at the given *bitrate*.
    A 45-min WAV (~450 MB) compresses to ~10 MB at 32 kbps.

    Returns the path to the compressed file.
    Raises ``AudioPreprocessError`` if ffmpeg fails.
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out_path = tmp_dir / "review_audio.ogg"
    cmd = [
        "ffmpeg", "-y",
        "-i", str(audio_path),
        "-ac", str(_REVIEW_AUDIO_CHANNELS),
        "-ar", str(_REVIEW_AUDIO_SAMPLE_RATE),
        "-c:a", "libopus",
        "-b:a", bitrate,
        str(out_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=600)
    except FileNotFoundError as exc:
        raise AudioPreprocessError("ffmpeg not found on PATH") from exc
    except Exception as exc:
        raise AudioPreprocessError(f"Audio compression failed: {exc}") from exc

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise AudioPreprocessError("Compressed audio file is empty or missing")

    logger.info(
        "[Review] Audio compressed: %s → %s (%.1f MB → %.1f MB)",
        audio_path.name, out_path.name,
        audio_path.stat().st_size / (1024 * 1024),
        out_path.stat().st_size / (1024 * 1024),
    )
    return out_path


def _prepare_review_audio_clip(
    audio_path: Path,
    tmp_dir: Path,
    *,
    start_ms: int,
    end_ms: int,
    clip_index: int = 0,
    bitrate: str = _REVIEW_AUDIO_BITRATE,
) -> Path:
    """Extract a time-range clip from audio and compress it for review.

    The clip covers [start_ms - padding, end_ms + padding], clamped to [0, ∞).
    Returns path to the compressed clip.
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out_path = tmp_dir / f"review_clip_{clip_index:03d}.ogg"

    padded_start_ms = max(0, start_ms - _REVIEW_AUDIO_CLIP_PADDING_MS)
    padded_end_ms = end_ms + _REVIEW_AUDIO_CLIP_PADDING_MS
    duration_ms = padded_end_ms - padded_start_ms

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{padded_start_ms / 1000:.3f}",
        "-i", str(audio_path),
        "-t", f"{duration_ms / 1000:.3f}",
        "-ac", str(_REVIEW_AUDIO_CHANNELS),
        "-ar", str(_REVIEW_AUDIO_SAMPLE_RATE),
        "-c:a", "libopus",
        "-b:a", bitrate,
        str(out_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
    except FileNotFoundError as exc:
        raise AudioPreprocessError("ffmpeg not found on PATH") from exc
    except Exception as exc:
        raise AudioPreprocessError(f"Audio clip extraction failed: {exc}") from exc

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise AudioPreprocessError(f"Compressed audio clip {clip_index} is empty or missing")

    logger.info(
        "[Review] Audio clip %d: %.1fs–%.1fs → %s (%.1f MB)",
        clip_index,
        padded_start_ms / 1000, padded_end_ms / 1000,
        out_path.name,
        out_path.stat().st_size / (1024 * 1024),
    )
    return out_path


def _get_audio_duration_ms(audio_path: Path) -> int | None:
    """Get audio duration in milliseconds using ffprobe. Returns None on failure."""
    try:
        from utils.audio_utils import measure_duration_ms
        return measure_duration_ms(audio_path)
    except Exception as exc:
        logger.warning("[Review] Failed to probe audio duration: %s", exc)
        return None


class AudioPreprocessError(Exception):
    """Raised when review audio preprocessing fails."""


def _try_compress_audio(audio_path: Path, tmp_dir: Path | None) -> Path | None:
    """Best-effort compress: try normal bitrate, then aggressive, then give up."""
    if tmp_dir is None:
        tmp_dir = audio_path.parent / ".review_tmp"
    try:
        return _prepare_review_audio(audio_path, tmp_dir)
    except AudioPreprocessError as exc:
        logger.warning("[Review] Audio compression failed (%s), trying aggressive...", exc)
    try:
        return _prepare_review_audio(audio_path, tmp_dir, bitrate=_REVIEW_AUDIO_BITRATE_AGGRESSIVE)
    except AudioPreprocessError as exc2:
        logger.warning("[Review] Aggressive compression also failed: %s", exc2)
    return None


def _get_review_model() -> str:
    """Get the logical review model name from admin settings or env.

    Returns a logical name such as ``"gemini_pro"``, ``"gemini"``, or
    ``"mimo_omni"``.  Use :func:`_resolve_model_id` to convert to an API
    model ID string.
    """
    model = os.environ.get("REVIEW_MODEL", _DEFAULT_REVIEW_MODEL)
    # Also check admin settings file
    try:
        settings_path = "/opt/aivideotrans/config/admin_settings.json"
        if os.path.exists(settings_path):
            with open(settings_path) as f:
                settings = json.load(f)
            model = settings.get("review_model", model)
    except Exception:
        pass
    return model


@dataclass
class ReviewResult:
    """Output of unified transcript review."""
    speakers: dict[str, dict[str, str]]  # {"speaker_a": {"name": "...", "gender": "male/female", "age_group": "young/middle/elderly", "role": "...", "style": "...", "voice_description": "..."}}
    glossary: dict[str, str]             # {"English term": "中文翻译"}
    corrections_applied: int
    lines: list[Any]                     # Updated TranscriptLine objects


# ---------------------------------------------------------------------------
# Prompt templates — audio vs text-only
# ---------------------------------------------------------------------------

# Shared sections (interview-specific speaker correction rules, output format)
# are factored into constants to avoid duplication.

_PROMPT_SPEAKER_CORRECTION_RULES_AUDIO = """\
   **⚠ 采访场景全局角色锁定（最高优先级）**：
   - 先通读全部转录稿，判断这是否是采访/对话场景
   - 如果是：先确定谁是"主持人/提问者"、谁是"受访者/回答者"
   - 然后统一检查：所有提问句应归属主持人，所有回答句应归属受访者
   - 如果发现同一人的连续回答被交替标成 A 和 B，必须统一纠正
   - 关键判断标准：语义角色（提问 vs 回答）> ASR 给出的 speaker 标签

   **常见 ASR 错误模式**：
   - 短促回应（Yeah, Sure, Right, 嗯, 对）被分给了错误的人
   - A-B-A 快速交叉（中间 B 实际是 A 的延续）
   - 旁白/介绍被标成了被采访者
   - **同一人连续回答被错误切换 speaker**：受访者说了一长段话，ASR 因中间停顿把后半段标成了另一个人。判断标准：如果上一句是提问，接下来连续几句都是回答同一个问题，它们应该属于同一个 speaker（受访者），即使 ASR 标了不同的 speaker
   - **插话/抢话**：主持人在受访者说话中途插入提问，ASR 容易把插话段标成受访者。听音频中的声音重叠和音色变化来判断
   - **被打断后继续**：受访者被打断后继续之前的话，ASR 可能标成新的说话人
   - **短促 backchannel vs 长回答**：主持人的"Yeah, sure"（backchannel）和受访者的长段回答要区别对待。backchannel 通常只有 1-3 个词且时长 <2 秒"""

_PROMPT_SPEAKER_CORRECTION_RULES_TEXT = """\
   **⚠ 采访场景全局角色锁定（最高优先级）**：
   - 先通读全部转录稿，判断这是否是采访/对话场景
   - 如果是：先确定谁是"主持人/提问者"、谁是"受访者/回答者"
   - 然后统一检查：所有提问句应归属主持人，所有回答句应归属受访者
   - 如果发现同一人的连续回答被交替标成 A 和 B，必须统一纠正
   - 关键判断标准：语义角色（提问 vs 回答）> ASR 给出的 speaker 标签

   **常见 ASR 错误模式**：
   - 短促回应（Yeah, Sure, Right, 嗯, 对）被分给了错误的人
   - A-B-A 快速交叉（中间 B 实际是 A 的延续）
   - 旁白/介绍被标成了被采访者
   - **同一人连续回答被错误切换 speaker**：受访者说了一长段话，ASR 因中间停顿把后半段标成了另一个人。判断标准：如果上一句是提问，接下来连续几句都是回答同一个问题，它们应该属于同一个 speaker（受访者），即使 ASR 标了不同的 speaker
   - **插话/抢话**：主持人在受访者说话中途插入提问，ASR 容易把插话段标成受访者。根据对话语义和时间间隔来判断
   - **被打断后继续**：受访者被打断后继续之前的话，ASR 可能标成新的说话人
   - **短促 backchannel vs 长回答**：主持人的"Yeah, sure"（backchannel）和受访者的长段回答要区别对待。backchannel 通常只有 1-3 个词且时长 <2 秒"""

_PROMPT_OUTPUT_FORMAT = """\
## 输出 JSON 格式（严格遵循，不要添加其他字段）

{{
  "speakers": {{
    "speaker_a": {{"name": "中文姓名", "gender": "female", "age_group": "middle", "role": "角色", "style": "语气描述", "voice_description": "声音清晰专业，语速适中，略带温和的采访语气"}},
    "speaker_b": {{"name": "中文姓名", "gender": "male", "age_group": "elderly", "role": "角色", "style": "语气描述", "voice_description": "声音低沉沙哑，语速缓慢，带有智慧感和幽默感"}}
  }},
  "glossary": {{
    "English term": "中文翻译",
    "Person Name": "中文名"
  }},
  "corrections": [
    {{"action": "correct_speaker", "index": 25, "to": "speaker_b", "reason": "原因"}},
    {{"action": "merge", "indices": [24, 25, 26], "speaker": "speaker_b", "reason": "原因"}},
    {{"action": "split", "index": 1, "at_text": "断点文本", "reason": "原因"}},
    {{"action": "fix_text", "index": 98, "old": "错误文本", "new": "正确文本", "reason": "原因"}}
  ]
}}"""

_REVIEW_PROMPT_WITH_AUDIO = """\
你是转录审校专家。听音频、对照转录稿，输出修改指令 JSON。

视频标题：{video_title}
视频链接：{video_url}

## 审校任务（一次性完成）

1. **识别说话人身份**：根据音频声音特征、视频标题、对话内容推断每个 speaker 的真实姓名和角色。姓名统一使用中文（如 Charlie Munger → 查理·芒格，Becky Quick → 贝基·奎克）。
2. **纠正说话人标注**：听音频分辨说话人，修正标注错误。

""" + _PROMPT_SPEAKER_CORRECTION_RULES_AUDIO + """
3. **修正转录文本**：
   - 去除重复内容（同一句出现在相邻段落）
   - 修正 ASR 错误（对照音频）
   - 不改变原文意思
4. **合并误拆段落**：仅当相邻段落因说话人标注错误被误拆时才合并（A-B-A 模式且中间段极短）。不要合并因自然停顿分开的段落。
5. **拆分超长段落**：超过 60 秒的段落，在语义断点处拆分为 15-45 秒。
6. **生成术语表**：提取人名、专有术语的中文翻译。
7. **分析说话风格**：每个说话人的语气、口头禅特点（给翻译参考）。
8. **描述音色特征**：听音频，为每个说话人输出一段自然语言音色描述（用于 TTS 语音合成），包括音调高低、语速快慢、声音质感（如低沉/清亮/沙哑）、情感特点等。
9. **标注性别和年龄段**：每个说话人必须标注 gender（"male" 或 "female"）和 age_group（"young"、"middle"、"elderly"）。gender 和 age_group 不可为空。

只输出有问题的行。没问题的不用管。

""" + _PROMPT_OUTPUT_FORMAT + """

## 转录稿（{line_count} 行）

{transcript_body}"""

_REVIEW_PROMPT_TEXT_ONLY = """\
你是转录审校专家。**本次没有提供音频**，请根据对话内容、说话人姓名、角色关系和语境进行分析。

视频标题：{video_title}
视频链接：{video_url}

## 审校任务（一次性完成）

1. **识别说话人身份**：根据视频标题、对话内容推断每个 speaker 的真实姓名和角色。姓名统一使用中文（如 Charlie Munger → 查理·芒格，Becky Quick → 贝基·奎克）。
2. **纠正说话人标注**：根据对话语义和角色关系推断说话人，修正标注错误。

""" + _PROMPT_SPEAKER_CORRECTION_RULES_TEXT + """
3. **修正转录文本**：
   - 去除重复内容（同一句出现在相邻段落）
   - 修正明显的 ASR 错误（根据上下文推断）
   - 不改变原文意思
4. **合并误拆段落**：仅当相邻段落因说话人标注错误被误拆时才合并（A-B-A 模式且中间段极短）。不要合并因自然停顿分开的段落。
5. **拆分超长段落**：超过 60 秒的段落，在语义断点处拆分为 15-45 秒。
6. **生成术语表**：提取人名、专有术语的中文翻译。
7. **分析说话风格**：每个说话人的语气特点（给翻译参考）。
8. **描述配音风格建议**：根据说话人的角色、身份和对话风格，建议适合的中文配音声音风格（用于 TTS 语音合成选择参考），例如"建议使用低沉稳重的男声"。注意：本次分析基于文本推断，未听到实际音频。
9. **标注性别和年龄段**：每个说话人必须标注 gender（"male" 或 "female"）和 age_group（"young"、"middle"、"elderly"）。gender 和 age_group 不可为空。请根据姓名和对话内容推断。

只输出有问题的行。没问题的不用管。

""" + _PROMPT_OUTPUT_FORMAT + """

## 转录稿（{line_count} 行）

{transcript_body}"""

# Keep backward-compatible name for any external references
REVIEW_PROMPT_TEMPLATE = _REVIEW_PROMPT_WITH_AUDIO


def _format_prompt(
    *,
    has_audio: bool,
    video_title: str,
    video_url: str,
    line_count: int,
    transcript_body: str,
) -> str:
    """Select and format the appropriate prompt template."""
    template = _REVIEW_PROMPT_WITH_AUDIO if has_audio else _REVIEW_PROMPT_TEXT_ONLY
    return template.format(
        video_title=video_title or "(unknown)",
        video_url=video_url or "(unknown)",
        line_count=line_count,
        transcript_body=transcript_body,
    )


def review_transcript(
    lines: list,
    *,
    audio_path: str | Path | None = None,
    video_title: str = "",
    video_url: str = "",
    words_data: list[dict] | None = None,
) -> ReviewResult | None:
    """Run unified LLM transcript review.

    Args:
        lines: list of TranscriptLine objects
        audio_path: path to audio file for multimodal review
        video_title: video title for context
        video_url: video URL for context
        words_data: word-level timing data for post-processing splits

    Returns:
        ReviewResult with updated lines, or None on failure (caller should fallback).
    """
    review_model = _get_review_model()

    if review_model == "mimo_omni":
        api_key = os.environ.get("MIMO_API_KEY", "").strip()
        if not api_key:
            logger.warning("MIMO_API_KEY not set, skipping unified review (mimo_omni)")
            return None
    else:
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            logger.warning("GEMINI_API_KEY not set, skipping unified review")
            return None

    if not lines:
        return None

    # --- Audio strategy: probe duration FIRST, then decide compression path ---
    original_audio: Path | None = None
    review_tmp_dir: Path | None = None
    audio_duration_ms: int | None = None

    if audio_path and Path(audio_path).exists():
        original_audio = Path(audio_path)
        review_tmp_dir = original_audio.parent / ".review_tmp"
        audio_duration_ms = _get_audio_duration_ms(original_audio)

    use_whole_audio = (
        audio_duration_ms is not None
        and audio_duration_ms <= _REVIEW_AUDIO_WHOLE_FILE_THRESHOLD_MS
    )

    # Only compress the whole file when we plan to reuse it across batches
    # (≤20 min). For >20 min the batched-review path generates per-batch clips
    # directly from the original, so a full-file compression would be wasted work.
    compressed_audio: Path | None = None
    if original_audio and use_whole_audio:
        compressed_audio = _try_compress_audio(original_audio, review_tmp_dir)

    # For single-batch (≤200 lines) non-batched path, always try to provide audio.
    # If it's a short file we already have compressed_audio; if it's longer we
    # compress on demand here (single call, not repeated per batch).
    single_batch_audio: Path | None = compressed_audio
    if original_audio and single_batch_audio is None and len(lines) <= _MAX_LINES_PER_BATCH:
        single_batch_audio = _try_compress_audio(original_audio, review_tmp_dir)

    # Build transcript body
    transcript_body = _build_transcript_body(lines)

    # Batch if needed
    if len(lines) <= _MAX_LINES_PER_BATCH:
        result = _call_review(
            api_key=api_key,
            transcript_body=transcript_body,
            line_count=len(lines),
            audio_path=single_batch_audio,
            video_title=video_title,
            video_url=video_url,
            review_model=review_model,
        )
        if result is None:
            return None
        speakers, glossary, corrections = result
    else:
        # Batch processing with audio strategy based on duration
        speakers, glossary, corrections = _batched_review(
            api_key=api_key,
            lines=lines,
            original_audio_path=original_audio,
            compressed_audio_path=compressed_audio,
            audio_duration_ms=audio_duration_ms,
            review_tmp_dir=review_tmp_dir,
            video_title=video_title,
            video_url=video_url,
            review_model=review_model,
        )

    # Apply corrections with validation
    updated_lines, applied_count = _apply_corrections(
        lines, corrections, words_data=words_data
    )

    # Conservative post-pass for 2-speaker interview transcripts only.
    updated_lines, sanity_applied = _apply_interview_sanity_check(updated_lines, speakers)
    applied_count += sanity_applied

    # Final safety: ensure no segment > 180s
    final_lines = _enforce_max_duration(updated_lines, words_data=words_data)

    # Re-index
    for i, line in enumerate(final_lines):
        line.index = i + 1

    logger.info(
        "Unified review: %d corrections applied, %d→%d lines, speakers=%s",
        applied_count, len(lines), len(final_lines),
        {k: v.get("name", "?") for k, v in speakers.items()},
    )

    return ReviewResult(
        speakers=speakers,
        glossary=glossary,
        corrections_applied=applied_count,
        lines=final_lines,
    )


def _build_transcript_body(lines: list) -> str:
    """Build numbered transcript with timestamps."""
    parts: list[str] = []
    for line in lines:
        start_s = line.start_ms / 1000
        end_s = line.end_ms / 1000
        parts.append(
            f"[{line.index}]({start_s:.2f}-{end_s:.2f}) {line.speaker_id}: {line.source_text}"
        )
    return "\n".join(parts)


def _call_review(
    *,
    api_key: str,
    transcript_body: str,
    line_count: int,
    audio_path: str | Path | None,
    cached_content_name: str | None = None,
    video_title: str,
    video_url: str,
    review_model: str = "gemini",
) -> tuple[dict, dict, list] | None:
    """Single LLM call for review. Dispatches to Gemini or MiMo Omni.

    If *cached_content_name* is provided (explicit cache hit for whole-audio),
    the audio is referenced from cache instead of being uploaded again.
    """

    # Resolve logical model name → API model ID
    api_model_id = _resolve_model_id(review_model)
    is_mimo = review_model == "mimo_omni"

    if is_mimo:
        # MiMo Omni is text-only — always use no-audio prompt
        prompt = _format_prompt(
            has_audio=False,
            video_title=video_title,
            video_url=video_url,
            line_count=line_count,
            transcript_body=transcript_body,
        )
        return _call_review_mimo_omni(api_key=api_key, prompt=prompt, model_id=api_model_id)

    try:
        genai = _load_genai()
        types = _load_genai_types()
        client = genai.Client(api_key=api_key)

        contents: list = []
        has_audio = False
        generate_kwargs: dict[str, Any] = {}

        # Audio-first: always try to provide audio when available.
        # Priority: cached content > upload compressed file.
        if cached_content_name:
            # Use explicit cache — audio already uploaded & cached
            generate_kwargs["cached_content"] = cached_content_name
            has_audio = True
            logger.info("[Review] Using cached audio content")
        elif audio_path and Path(audio_path).exists():
            audio_path = Path(audio_path)
            try:
                file_size_mb = audio_path.stat().st_size / (1024 * 1024)
                logger.info(
                    "[Review] Uploading audio for multimodal review (%s, %.1f MB)...",
                    audio_path.name, file_size_mb,
                )
                audio_file = client.files.upload(file=audio_path)
                contents.append(audio_file)
                has_audio = True
                logger.info("[Review] Audio uploaded successfully")
            except Exception as e:
                logger.warning("[Review] Audio upload failed: %s", e)

        if not has_audio and audio_path:
            logger.warning(
                "[Review] Proceeding WITHOUT audio — speaker profiling "
                "quality (gender/age/voice_description) will be degraded"
            )

        # Select prompt template based on whether audio is available
        prompt = _format_prompt(
            has_audio=has_audio,
            video_title=video_title,
            video_url=video_url,
            line_count=line_count,
            transcript_body=transcript_body,
        )
        contents.append(prompt)

        response = client.models.generate_content(
            model=api_model_id,
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
                max_output_tokens=8192,
            ),
            **generate_kwargs,
        )

        response_text = _extract_text(response)
        if not response_text:
            logger.warning("Review returned empty response")
            return None

        payload = json.loads(response_text)
        speakers = payload.get("speakers", {})
        glossary = payload.get("glossary", {})
        corrections = payload.get("corrections", [])

        logger.info(
            "Review response: %d speakers, %d glossary terms, %d corrections (audio=%s)",
            len(speakers), len(glossary), len(corrections), has_audio,
        )
        return speakers, glossary, corrections

    except Exception:
        logger.exception("Unified review LLM call failed")
        return None


def _call_review_mimo_omni(
    *,
    api_key: str,
    prompt: str,
    model_id: str = "",
) -> tuple[dict, dict, list] | None:
    """Call MiMo-V2-Omni API for text-only review (OpenAI-compatible endpoint)."""
    effective_model = model_id or _MODEL_MAP.get("mimo_omni", "mimo-v2-omni")
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": 8192,
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            _MIMO_OMNI_API_URL,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=300) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        response_text = body["choices"][0]["message"]["content"]
        if not response_text:
            logger.warning("MiMo Omni review returned empty response")
            return None

        # Strip markdown fences if present
        response_text = response_text.strip()
        if response_text.startswith("```"):
            response_text = re.sub(r"^```(?:json)?\s*", "", response_text)
            response_text = re.sub(r"\s*```$", "", response_text)

        result = json.loads(response_text)
        speakers = result.get("speakers", {})
        glossary = result.get("glossary", {})
        corrections = result.get("corrections", [])

        logger.info(
            "MiMo Omni review response: %d speakers, %d glossary terms, %d corrections",
            len(speakers), len(glossary), len(corrections),
        )
        return speakers, glossary, corrections

    except Exception:
        logger.exception("MiMo Omni review LLM call failed")
        return None


def _batched_review(
    *,
    api_key: str,
    lines: list,
    original_audio_path: Path | None,
    compressed_audio_path: Path | None,
    audio_duration_ms: int | None,
    review_tmp_dir: Path | None,
    video_title: str,
    video_url: str,
    review_model: str = "gemini",
) -> tuple[dict, dict, list]:
    """Process large transcripts in batches with overlap.

    Audio strategy (per plan §4.2):
    - ≤20 min total duration: every batch receives the same compressed
      whole-file audio.  We first attempt to create a Gemini explicit cache
      for this audio; if that fails (e.g. token count too low) we fall back
      to passing the same compressed file to each batch call.
    - >20 min: each batch receives a batch-local clip generated directly
      from the *original* audio (no whole-file compression).
    """
    all_corrections: list = []
    speakers: dict = {}
    glossary: dict = {}

    batch_size = _MAX_LINES_PER_BATCH
    overlap = _BATCH_OVERLAP

    use_whole_audio = (
        audio_duration_ms is not None
        and audio_duration_ms <= _REVIEW_AUDIO_WHOLE_FILE_THRESHOLD_MS
        and compressed_audio_path is not None
    )

    strategy = "whole_audio" if use_whole_audio else "batch_local_clip"
    if audio_duration_ms is not None:
        logger.info(
            "[Review] Batched review: duration=%.0fs, strategy=%s",
            audio_duration_ms / 1000, strategy,
        )

    # --- ≤20 min: try explicit caching for the compressed whole audio ---
    cached_content_name: str | None = None
    if use_whole_audio and review_model not in ("mimo_omni",):
        cached_content_name = _try_create_audio_cache(
            api_key=api_key,
            audio_path=compressed_audio_path,
            review_model=review_model,
        )
        if cached_content_name:
            logger.info("[Review] Using explicit audio cache: %s", cached_content_name)

    batch_num = 0
    offset = 0
    while offset < len(lines):
        end = min(offset + batch_size, len(lines))
        batch = lines[offset:end]
        batch_num += 1

        logger.info("Review batch %d: lines %d-%d", batch_num, offset + 1, end)

        # Resolve audio for this batch
        batch_audio: Path | None = None
        if use_whole_audio:
            # ≤20 min: reuse same compressed audio for every batch
            batch_audio = compressed_audio_path
        elif original_audio_path and original_audio_path.exists() and review_tmp_dir:
            # >20 min: extract batch-local clip directly from original audio
            batch_start_ms = batch[0].start_ms
            batch_end_ms = batch[-1].end_ms
            try:
                batch_audio = _prepare_review_audio_clip(
                    original_audio_path,
                    review_tmp_dir,
                    start_ms=batch_start_ms,
                    end_ms=batch_end_ms,
                    clip_index=batch_num,
                )
            except AudioPreprocessError as exc:
                logger.warning("[Review] Batch %d clip failed: %s", batch_num, exc)

        transcript_body = _build_transcript_body(batch)
        result = _call_review(
            api_key=api_key,
            transcript_body=transcript_body,
            line_count=len(batch),
            audio_path=batch_audio,
            cached_content_name=cached_content_name,
            video_title=video_title,
            video_url=video_url,
            review_model=review_model,
        )

        if result is not None:
            batch_speakers, batch_glossary, batch_corrections = result
            if not speakers:  # Use first batch's speaker identification
                speakers = batch_speakers
            glossary.update(batch_glossary)
            all_corrections.extend(batch_corrections)

        # Advance with overlap
        offset = end - overlap if end < len(lines) else end

    return speakers, glossary, all_corrections


def _try_create_audio_cache(
    *,
    api_key: str,
    audio_path: Path | None,
    review_model: str = "",
) -> str | None:
    """Attempt to create a Gemini explicit cache for the given audio file.

    Returns the cache resource name (str) on success, or None if caching
    is unavailable, the audio has too few tokens, or the SDK call fails.
    """
    if audio_path is None or not audio_path.exists():
        return None
    api_model_id = _resolve_model_id(review_model) if review_model else _MODEL_MAP[_DEFAULT_REVIEW_MODEL]
    try:
        genai = _load_genai()
        types = _load_genai_types()
        client = genai.Client(api_key=api_key)

        uploaded = client.files.upload(file=audio_path)

        # Count tokens to check we meet the minimum threshold
        token_count_resp = client.models.count_tokens(
            model=api_model_id,
            contents=[uploaded],
        )
        total_tokens = getattr(token_count_resp, "total_tokens", 0) or 0
        if total_tokens < _GEMINI_MIN_CACHE_TOKENS:
            logger.info(
                "[Review] Audio has %d tokens, below cache minimum %d — skipping cache",
                total_tokens, _GEMINI_MIN_CACHE_TOKENS,
            )
            return None

        cached = client.caches.create(
            model=api_model_id,
            config=types.CreateCachedContentConfig(
                contents=[uploaded],
                display_name="review-audio-cache",
            ),
        )
        return cached.name
    except Exception as exc:
        logger.warning("[Review] Explicit audio cache creation failed: %s", exc)
        return None


def _apply_corrections(
    lines: list,
    corrections: list[dict],
    *,
    words_data: list[dict] | None = None,
) -> tuple[list, int]:
    """Apply corrections with validation. Returns (updated_lines, applied_count)."""
    from src.services.assemblyai.transcriber import TranscriptLine

    # Work on a copy
    working_lines = list(lines)
    applied = 0

    # Build index map for fast lookup
    index_map: dict[int, int] = {line.index: i for i, line in enumerate(working_lines)}

    for c in corrections:
        try:
            action = c.get("action", "")

            if action == "correct_speaker":
                idx = c.get("index")
                to = c.get("to", "")
                if to not in {"speaker_a", "speaker_b"}:
                    logger.warning("Invalid speaker_id '%s', skipping", to)
                    continue
                pos = index_map.get(idx)
                if pos is None:
                    continue
                line = working_lines[pos]
                if line.speaker_id != to:
                    working_lines[pos] = TranscriptLine(
                        index=line.index,
                        start_ms=line.start_ms,
                        end_ms=line.end_ms,
                        speaker_id=to,
                        speaker_label=to.replace("speaker_", "").upper(),
                        source_text=line.source_text,
                    )
                    applied += 1

            elif action == "merge":
                indices = c.get("indices", [])
                if len(indices) < 2:
                    continue

                # Find positions
                positions = [index_map.get(idx) for idx in indices]
                if any(p is None for p in positions):
                    continue
                positions = sorted(p for p in positions if p is not None)

                # Check for long pauses between segments
                has_long_pause = False
                for k in range(len(positions) - 1):
                    gap = working_lines[positions[k + 1]].start_ms - working_lines[positions[k]].end_ms
                    if gap >= _MAX_PAUSE_FOR_MERGE_MS:
                        logger.warning(
                            "Segments %s have %dms pause, not merging",
                            indices, gap,
                        )
                        has_long_pause = True
                        break
                if has_long_pause:
                    continue

                # Check merged duration
                merged_duration = (
                    working_lines[positions[-1]].end_ms - working_lines[positions[0]].start_ms
                )
                if merged_duration > _MAX_MERGE_DURATION_MS:
                    logger.warning(
                        "Merged duration %dms exceeds limit, skipping",
                        merged_duration,
                    )
                    continue

                # Merge: keep first, extend it, remove rest
                first = working_lines[positions[0]]
                merged_text = " ".join(
                    working_lines[p].source_text for p in positions
                )
                speaker = c.get("speaker", first.speaker_id)
                if speaker not in {"speaker_a", "speaker_b"}:
                    speaker = first.speaker_id

                working_lines[positions[0]] = TranscriptLine(
                    index=first.index,
                    start_ms=first.start_ms,
                    end_ms=working_lines[positions[-1]].end_ms,
                    speaker_id=speaker,
                    speaker_label=speaker.replace("speaker_", "").upper(),
                    source_text=merged_text,
                )
                # Remove merged lines (reverse order to preserve indices)
                for p in reversed(positions[1:]):
                    working_lines.pop(p)

                # Rebuild index map
                index_map = {line.index: i for i, line in enumerate(working_lines)}
                applied += 1

            elif action == "split":
                idx = c.get("index")
                at_text = c.get("at_text", "")
                pos = index_map.get(idx)
                if pos is None or not at_text:
                    continue

                line = working_lines[pos]
                if line.end_ms - line.start_ms < _MIN_SPLIT_DURATION_MS:
                    logger.warning(
                        "Segment %d only %dms, too short to split",
                        idx, line.end_ms - line.start_ms,
                    )
                    continue

                # Find split position in text
                split_pos = line.source_text.find(at_text)
                if split_pos <= 0:
                    continue

                # Estimate time split proportionally
                text_ratio = split_pos / max(len(line.source_text), 1)
                split_ms = line.start_ms + int(
                    (line.end_ms - line.start_ms) * text_ratio
                )

                line_a = TranscriptLine(
                    index=line.index,
                    start_ms=line.start_ms,
                    end_ms=split_ms,
                    speaker_id=line.speaker_id,
                    speaker_label=line.speaker_label,
                    source_text=line.source_text[:split_pos].strip(),
                )
                line_b = TranscriptLine(
                    index=line.index + 1000,  # Temporary, will re-index
                    start_ms=split_ms,
                    end_ms=line.end_ms,
                    speaker_id=line.speaker_id,
                    speaker_label=line.speaker_label,
                    source_text=line.source_text[split_pos:].strip(),
                )

                if line_a.source_text and line_b.source_text:
                    working_lines[pos] = line_a
                    working_lines.insert(pos + 1, line_b)
                    index_map = {line.index: i for i, line in enumerate(working_lines)}
                    applied += 1

            elif action == "fix_text":
                idx = c.get("index")
                old_text = c.get("old", "")
                new_text = c.get("new", "")
                pos = index_map.get(idx)
                if pos is None or not old_text or not new_text:
                    continue

                # Check edit distance ratio
                distance = _edit_distance(old_text, new_text)
                ratio = distance / max(len(old_text), 1)
                if ratio > _MAX_EDIT_DISTANCE_RATIO:
                    logger.warning(
                        "Edit distance ratio %.2f exceeds limit for '%s'→'%s'",
                        ratio, old_text[:30], new_text[:30],
                    )
                    continue

                line = working_lines[pos]
                if old_text in line.source_text:
                    working_lines[pos] = TranscriptLine(
                        index=line.index,
                        start_ms=line.start_ms,
                        end_ms=line.end_ms,
                        speaker_id=line.speaker_id,
                        speaker_label=line.speaker_label,
                        source_text=line.source_text.replace(old_text, new_text, 1),
                    )
                    applied += 1

        except Exception as e:
            logger.warning("Skipping invalid correction %s: %s", c, e)
            continue

    return working_lines, applied


def _apply_interview_sanity_check(
    lines: list,
    speakers: dict[str, dict[str, str]] | None,
) -> tuple[list, int]:
    """Apply conservative speaker fixes for clear two-party interview patterns."""
    from src.services.assemblyai.transcriber import TranscriptLine

    if not lines or not speakers:
        return list(lines), 0

    interview_roles = _resolve_interview_roles(speakers)
    if interview_roles is None:
        return list(lines), 0

    host_speaker, guest_speaker = interview_roles
    adjusted_lines = list(lines)
    applied = 0

    for idx, line in enumerate(adjusted_lines):
        duration_ms = max(0, line.end_ms - line.start_ms)
        text = line.source_text.strip()
        if not text:
            continue

        if _contains_named_utterance(text):
            logger.info(
                "[S2][sanity] line %d: keep %s (named utterance, conservative)",
                line.index,
                line.speaker_id,
            )
            continue

        current_speaker = line.speaker_id
        target_speaker = current_speaker
        reason = ""

        if _is_short_question(text, duration_ms):
            target_speaker = host_speaker
            reason = "short question => host"
        elif _is_short_backchannel(text, duration_ms):
            target_speaker = host_speaker
            reason = "short backchannel => host"
        elif _is_first_person_answer(text, duration_ms):
            target_speaker = guest_speaker
            reason = "first-person answer => guest"
        elif _is_answer_continuation(
            lines=adjusted_lines,
            position=idx,
            host_speaker=host_speaker,
            guest_speaker=guest_speaker,
        ):
            target_speaker = guest_speaker
            reason = "answer continuation => guest"
        elif duration_ms > _NO_AUTO_FLIP_IF_LONGER_THAN_MS:
            logger.info(
                "[S2][sanity] line %d: keep %s (long sentence, conservative)",
                line.index,
                line.speaker_id,
            )
            continue

        if target_speaker == current_speaker or not reason:
            continue

        adjusted_lines[idx] = TranscriptLine(
            index=line.index,
            start_ms=line.start_ms,
            end_ms=line.end_ms,
            speaker_id=target_speaker,
            speaker_label=target_speaker.replace("speaker_", "").upper(),
            source_text=line.source_text,
        )
        logger.info(
            "[S2][sanity] line %d: %s -> %s (%s)",
            line.index,
            current_speaker,
            target_speaker,
            reason,
        )
        applied += 1

    return adjusted_lines, applied


def _resolve_interview_roles(
    speakers: dict[str, dict[str, str]],
) -> tuple[str, str] | None:
    if len(speakers) != 2:
        return None

    host_speaker: str | None = None
    guest_speaker: str | None = None
    for speaker_id, profile in speakers.items():
        role_text = " ".join(
            str(profile.get(key, "")).strip().lower()
            for key in ("role", "style", "voice_description")
        )
        if any(token in role_text for token in ("host", "interviewer", "anchor", "主持", "采访", "访谈")):
            host_speaker = speaker_id
        if any(token in role_text for token in ("guest", "interviewee", "受访", "嘉宾")):
            guest_speaker = speaker_id

    if host_speaker and guest_speaker and host_speaker != guest_speaker:
        return host_speaker, guest_speaker
    return None


def _is_short_question(text: str, duration_ms: int) -> bool:
    lowered = text.strip().lower()
    if duration_ms > _SHORT_QUESTION_MAX_MS:
        return False
    if "?" in text or "？" in text:
        return True

    patterns = (
        "what",
        "why",
        "how",
        "which",
        "when",
        "do you",
        "did you",
        "was it",
        "is it",
        "what was",
        "what do you",
        "which means what",
        "什么",
        "为什么",
        "怎么",
        "你觉得",
        "你会",
        "那你",
        "那您",
        "什么意思",
        "怎么看",
    )
    return any(token in lowered for token in patterns)


def _is_first_person_answer(text: str, duration_ms: int) -> bool:
    if duration_ms < _ANSWER_MIN_MS:
        return False
    lowered = text.strip().lower()
    patterns = (
        "i think",
        "i mean",
        "i guess",
        "well, i",
        "well i",
        " my ",
        " we ",
        "for me",
        "我觉得",
        "我认为",
        "我想",
        "我的",
        "我们",
        "对我来说",
        "老实说",
    )
    padded = f" {lowered} "
    return any(token in padded or token in lowered for token in patterns)


def _is_short_backchannel(text: str, duration_ms: int) -> bool:
    if duration_ms > _SHORT_BACKCHANNEL_MAX_MS:
        return False

    normalized = text.strip().lower()
    normalized = re.sub(r"[.?!,;:\u3002\uff01\uff1f\uff0c\uff1b\uff1a]+$", "", normalized).strip()
    if not normalized:
        return False

    tokens = {
        "yes",
        "yeah",
        "right",
        "sure",
        "okay",
        "ok",
        "uh-huh",
        "mm-hmm",
        "嗯",
        "对",
        "对啊",
        "是",
        "是啊",
        "好",
    }
    return normalized in tokens


def _is_answer_continuation(
    *,
    lines: list,
    position: int,
    host_speaker: str,
    guest_speaker: str,
) -> bool:
    current = lines[position]
    duration_ms = max(0, current.end_ms - current.start_ms)
    if duration_ms < _ANSWER_CONTINUATION_MIN_MS:
        return False
    if _is_short_question(current.source_text, duration_ms):
        return False

    previous = lines[position - 1] if position > 0 else None
    if previous is None or previous.speaker_id != guest_speaker:
        return False

    lowered = current.source_text.strip().lower()
    continuation_tokens = (
        "and",
        "but",
        "so",
        "because",
        "well",
        "yeah",
        "yes, but",
        "i mean",
        "而且",
        "但是",
        "所以",
        "因为",
        "其实",
        "嗯",
        "对",
        "是的",
        "我觉得",
    )
    if any(lowered.startswith(token) for token in continuation_tokens):
        return True
    return False


def _contains_named_utterance(text: str) -> bool:
    stripped = text.strip()
    if re.search(r"(?:thanks|thank you|hi|hello|hey)[,\s]+[A-Z][a-z]{2,}\b", stripped, re.I):
        return True
    if re.search(r",\s*[A-Z][a-z]{2,}\b", stripped):
        return True
    return False


def _enforce_max_duration(
    lines: list,
    *,
    max_duration_ms: int = _MAX_MERGE_DURATION_MS,
    words_data: list[dict] | None = None,
) -> list:
    """Final safety: split any segment exceeding max duration."""
    from src.services.assemblyai.transcriber import TranscriptLine

    result: list = []
    for line in lines:
        duration = line.end_ms - line.start_ms
        if duration <= max_duration_ms:
            result.append(line)
            continue

        # Try to find best split point using word pauses
        split_ms = _find_best_split_point(line, words_data)
        if split_ms and line.start_ms < split_ms < line.end_ms:
            # Estimate text split position
            text_ratio = (split_ms - line.start_ms) / duration
            text_pos = int(len(line.source_text) * text_ratio)
            # Snap to nearest sentence boundary
            text_pos = _snap_to_sentence_boundary(line.source_text, text_pos)

            line_a = TranscriptLine(
                index=line.index,
                start_ms=line.start_ms,
                end_ms=split_ms,
                speaker_id=line.speaker_id,
                speaker_label=line.speaker_label,
                source_text=line.source_text[:text_pos].strip(),
            )
            line_b = TranscriptLine(
                index=line.index + 5000,
                start_ms=split_ms,
                end_ms=line.end_ms,
                speaker_id=line.speaker_id,
                speaker_label=line.speaker_label,
                source_text=line.source_text[text_pos:].strip(),
            )
            if line_a.source_text and line_b.source_text:
                result.append(line_a)
                result.append(line_b)
                logger.info(
                    "Force-split segment %d (%dms) at %dms",
                    line.index, duration, split_ms,
                )
                continue

        # Couldn't split, keep as-is
        logger.warning("Could not split segment %d (%dms), keeping as-is", line.index, duration)
        result.append(line)

    return result


def _find_best_split_point(line, words_data: list[dict] | None) -> int | None:
    """Find the longest pause within this segment's time range."""
    if not words_data:
        # Fallback: split at midpoint
        return line.start_ms + (line.end_ms - line.start_ms) // 2

    # Find words in this segment's time range
    segment_words = [
        w for w in words_data
        if w.get("start", 0) >= line.start_ms and w.get("end", 0) <= line.end_ms
    ]

    if len(segment_words) < 2:
        return line.start_ms + (line.end_ms - line.start_ms) // 2

    # Find longest gap
    best_gap = 0
    best_pos = None
    for i in range(len(segment_words) - 1):
        gap = segment_words[i + 1].get("start", 0) - segment_words[i].get("end", 0)
        if gap > best_gap:
            best_gap = gap
            best_pos = segment_words[i].get("end", 0) + gap // 2

    return best_pos


def _snap_to_sentence_boundary(text: str, pos: int) -> int:
    """Snap position to nearest sentence ending."""
    # Look ±50 chars for sentence boundary
    search_range = 50
    best = pos
    best_dist = search_range + 1

    for i in range(max(0, pos - search_range), min(len(text), pos + search_range)):
        if i < len(text) and text[i] in ".!?":
            dist = abs(i - pos)
            if dist < best_dist:
                best = i + 1
                best_dist = dist

    return best


def _edit_distance(s1: str, s2: str) -> int:
    """Simple Levenshtein distance."""
    if len(s1) < len(s2):
        return _edit_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)

    prev_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = prev_row[j + 1] + 1
            deletions = curr_row[j] + 1
            substitutions = prev_row[j] + (c1 != c2)
            curr_row.append(min(insertions, deletions, substitutions))
        prev_row = curr_row

    return prev_row[-1]


def _extract_text(response: object) -> str | None:
    """Extract text from Gemini response."""
    if hasattr(response, "text"):
        return response.text
    if hasattr(response, "candidates"):
        candidates = response.candidates or []
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            if content and hasattr(content, "parts"):
                for part in content.parts:
                    text = getattr(part, "text", None)
                    if text:
                        return text
    return None


def _load_genai():
    import importlib
    return importlib.import_module("google.genai")


def _load_genai_types():
    import importlib
    return importlib.import_module("google.genai.types")
