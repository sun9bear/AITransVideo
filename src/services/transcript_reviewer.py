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
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model registry — import from shared llm_registry (single source of truth)
# ---------------------------------------------------------------------------
from services.llm_registry import (
    MODEL_REGISTRY as _MODEL_REGISTRY,
    get_api_key as _get_model_api_key,
    get_prompt_model as _get_prompt_model,
    resolve_model_id as _resolve_model_id_from_registry,
    get_fallback_candidates,
)

_MIMO_OMNI_API_URL = "https://api.xiaomimimo.com/v1/chat/completions"

_TRANSIENT_RETRY_WAIT_S = 3  # seconds to wait before retrying on transient error


def _is_transient_error(exc: Exception) -> bool:
    """Classify whether an exception is transient (worth retrying same model).

    Transient: connection timeout, rate-limit (429), server errors (500/502/503).
    Non-transient: JSON parse errors, empty responses, auth errors, etc.
    """
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    # urllib errors
    if isinstance(exc, urllib.error.URLError):
        # Socket timeout or unreachable
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (429, 500, 502, 503, 504)
    # google-genai SDK errors (wrapped in various exception types)
    err_str = str(exc).lower()
    if any(kw in err_str for kw in ("timeout", "429", "rate limit", "503", "502", "500", "unavailable", "connection")):
        return True
    return False

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
_SPEAKER_ID_PATTERN = re.compile(r"^speaker_[a-z0-9_]+$")

# Gemini explicit cache requires at least 32 768 input tokens.
# 20 min audio ≈ 38 400 tokens (32 tok/s × 1200 s), safely above the threshold.
# Shorter audio may not qualify — the code will fall back to plain upload.
_GEMINI_MIN_CACHE_TOKENS = 32_768


def _resolve_model_id(logical_name: str) -> str:
    """Resolve a logical review model name to its API model ID."""
    return _resolve_model_id_from_registry(logical_name)


def _create_review_client(api_key: str | None = None):
    """Create a Gemini client for review.

    Isolated as a module-level helper so tests can monkeypatch a single
    function instead of reaching into ``google.genai`` internals.

    When ``api_key`` is None, ``create_gemini_client`` uses its built-in
    credential priority (GOOGLE_APPLICATION_CREDENTIALS → VERTEX_AI_EXPRESS_KEY
    → GEMINI_API_KEY env var).
    """
    from services.gemini.client_factory import create_gemini_client
    return create_gemini_client(api_key=api_key)


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
    """Get the logical review model name — legacy compat wrapper.

    New code should use ``_get_prompt_model(mode, prompt_key)`` directly.
    This wrapper exists for call-sites that haven't been migrated yet
    (e.g. legacy_review_transcript_single_pass).
    """
    return _get_prompt_model("studio", "pass1")


def _get_admin_prompt_override(prompt_key: str) -> str | None:
    """Load a prompt override from admin settings.

    Admin can set custom prompts in admin_settings.json under the
    ``"review_prompts"`` dict, keyed by prompt name
    (``"pass1"``, ``"pass2"``, ``"pass3"``).

    Returns the override string, or None if not configured.
    """
    try:
        settings_path = str(
            Path(os.environ.get("AIVIDEOTRANS_CONFIG_DIR", "/opt/aivideotrans/config"))
            / "admin_settings.json"
        )
        if os.path.exists(settings_path):
            with open(settings_path) as f:
                settings = json.load(f)
            prompts = settings.get("review_prompts", {})
            if isinstance(prompts, dict):
                override = prompts.get(prompt_key, "")
                if override and isinstance(override, str):
                    return override
    except Exception:
        pass
    return None


@dataclass
class ReviewResult:
    """Output of unified transcript review."""
    speakers: dict[str, dict[str, str]]  # {"speaker_a": {"name": "...", "gender": "male/female", "age_group": "young/middle/elderly", "role": "...", "style": "...", "voice_description": "..."}}
    glossary: dict[str, str]             # {"English term": "中文翻译"}
    corrections_applied: int
    lines: list[Any]                     # Updated TranscriptLine objects
    debug_artifacts: dict[str, str] = field(default_factory=dict)


def _dump_retry_response(
    debug_output_dir: str | Path | None,
    pass_name: str,
    attempt_idx: int,
    label: str,
    model_id: str,
    response_text: str | None,
    error: Exception | None,
) -> None:
    """Save each retry's raw response (including failures) for debugging."""
    if debug_output_dir is None:
        return
    try:
        out_dir = Path(debug_output_dir).resolve(strict=False)
        out_dir.mkdir(parents=True, exist_ok=True)
        filename = f"s2_{pass_name}_attempt{attempt_idx + 1}_{label}.json"
        from datetime import datetime, timezone
        payload = {
            "pass": pass_name,
            "attempt": attempt_idx + 1,
            "label": label,
            "model": model_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "success": error is None,
            "error": str(error) if error else None,
            "response_length": len(response_text) if response_text else 0,
            "response_text": response_text,
        }
        (out_dir / filename).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass  # never break the pipeline for debug logging


def _detect_speaker_ids_from_lines(lines: list) -> list[str]:
    """Extract unique speaker IDs from transcript lines, preserving order."""
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        sid = str(getattr(line, "speaker_id", "speaker_a"))
        if sid not in seen:
            seen.add(sid)
            result.append(sid)
    return result or ["speaker_a"]


def _snapshot_lines(lines: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "index": int(line.index),
            "start_ms": int(line.start_ms),
            "end_ms": int(line.end_ms),
            "speaker_id": str(line.speaker_id),
            "speaker_label": str(line.speaker_label),
            "source_text": str(line.source_text),
        }
        for line in lines
    ]


def _build_speaker_diff_entries(
    before_snapshot: list[dict[str, Any]],
    after_snapshot: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compare two snapshots for speaker changes using stable key matching.

    When line counts differ (merge/split), positional zip is unreliable.
    Instead, match lines by (start_ms, source_text prefix) as a stable key.
    Unmatched lines are skipped — conservative: no false speaker diffs.
    """
    if len(before_snapshot) == len(after_snapshot):
        # Same length: safe to use positional comparison
        entries: list[dict[str, Any]] = []
        for position, (before_line, after_line) in enumerate(zip(before_snapshot, after_snapshot)):
            if before_line["speaker_id"] == after_line["speaker_id"]:
                continue
            entries.append(
                {
                    "position": position,
                    "before_index": before_line["index"],
                    "after_index": after_line["index"],
                    "before_speaker_id": before_line["speaker_id"],
                    "after_speaker_id": after_line["speaker_id"],
                    "start_ms": before_line["start_ms"],
                    "end_ms": before_line["end_ms"],
                    "source_text": before_line["source_text"],
                }
            )
        return entries

    # Different length (merge/split happened): use start_ms key matching
    after_by_start: dict[int, dict[str, Any]] = {}
    for line in after_snapshot:
        after_by_start.setdefault(line["start_ms"], line)

    entries = []
    for before_line in before_snapshot:
        after_line = after_by_start.get(before_line["start_ms"])
        if after_line is None:
            continue  # line was merged away — skip, don't fabricate a diff
        if before_line["speaker_id"] == after_line["speaker_id"]:
            continue
        entries.append(
            {
                "position": None,  # positional index not meaningful after merge/split
                "before_index": before_line["index"],
                "after_index": after_line["index"],
                "before_speaker_id": before_line["speaker_id"],
                "after_speaker_id": after_line["speaker_id"],
                "start_ms": before_line["start_ms"],
                "end_ms": before_line["end_ms"],
                "source_text": before_line["source_text"],
                "note": "matched_by_start_ms",
            }
        )
    return entries


def _write_review_debug_artifacts(
    output_dir: str | Path,
    *,
    review_events: list[dict[str, Any]],
    original_snapshot: list[dict[str, Any]],
    after_corrections_snapshot: list[dict[str, Any]],
    after_sanity_snapshot: list[dict[str, Any]],
    final_snapshot: list[dict[str, Any]],
    speakers: dict[str, dict[str, str]] | None = None,
    glossary: dict[str, str] | None = None,
    raw_corrections: list[dict] | None = None,
    corrections_applied: int = 0,
    sanity_applied: int = 0,
    review_model: str = "",
    has_audio: bool = False,
) -> dict[str, str]:
    output_root = Path(output_dir).resolve(strict=False)
    output_root.mkdir(parents=True, exist_ok=True)

    raw_response_path = (output_root / "s2_review_raw_response.json").resolve(strict=False)
    speaker_diff_path = (output_root / "s2_review_speaker_diff.json").resolve(strict=False)
    result_path = (output_root / "s2_review_result.json").resolve(strict=False)
    audit_path = (output_root / "s2_review_audit.json").resolve(strict=False)

    # --- Existing: raw response ---
    raw_response_path.write_text(
        json.dumps({"events": review_events}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # --- Existing: speaker diff ---
    diff_payload = {
        "line_counts": {
            "original": len(original_snapshot),
            "after_corrections": len(after_corrections_snapshot),
            "after_sanity": len(after_sanity_snapshot),
            "final": len(final_snapshot),
        },
        "speaker_diffs": {
            "original_to_after_corrections": _build_speaker_diff_entries(
                original_snapshot,
                after_corrections_snapshot,
            ),
            "after_corrections_to_after_sanity": _build_speaker_diff_entries(
                after_corrections_snapshot,
                after_sanity_snapshot,
            ),
            "after_sanity_to_final": _build_speaker_diff_entries(
                after_sanity_snapshot,
                final_snapshot,
            ),
        },
        "snapshots": {
            "original": original_snapshot,
            "after_corrections": after_corrections_snapshot,
            "after_sanity": after_sanity_snapshot,
            "final": final_snapshot,
        },
    }
    speaker_diff_path.write_text(
        json.dumps(diff_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # --- NEW: structured result ---
    result_payload = {
        "version": 1,
        "review_model": review_model,
        "has_audio": has_audio,
        "speakers": speakers or {},
        "speaker_names": {k: v.get("name", "") for k, v in (speakers or {}).items()},
        "glossary": glossary or {},
        "raw_corrections": raw_corrections or [],
        "corrections_applied": corrections_applied,
        "sanity_applied": sanity_applied,
        "line_counts": {
            "original": len(original_snapshot),
            "final": len(final_snapshot),
        },
    }
    result_path.write_text(
        json.dumps(result_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # --- NEW: lightweight audit trail ---
    # Records each speaker change with clear before/after and source
    audit_events: list[dict[str, Any]] = []
    for entry in _build_speaker_diff_entries(original_snapshot, after_corrections_snapshot):
        audit_events.append({**entry, "source": "correction"})
    for entry in _build_speaker_diff_entries(after_corrections_snapshot, after_sanity_snapshot):
        audit_events.append({**entry, "source": "sanity_check"})
    for entry in _build_speaker_diff_entries(after_sanity_snapshot, final_snapshot):
        audit_events.append({**entry, "source": "post_processing"})
    audit_path.write_text(
        json.dumps({"audit_events": audit_events}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        "raw_response_path": str(raw_response_path),
        "speaker_diff_path": str(speaker_diff_path),
        "result_path": str(result_path),
        "audit_path": str(audit_path),
    }


# ---------------------------------------------------------------------------
# Prompt templates — audio vs text-only
# ---------------------------------------------------------------------------

# Shared sections (interview-specific speaker correction rules, output format)
# are factored into constants to avoid duplication.

_PROMPT_SPEAKER_CORRECTION_RULES_AUDIO = """\
   **⚠ 说话人纠正原则（最高优先级）**：
   - 先通读全部转录稿，判断说话人数量和各自角色
   - **保留 ASR 识别的所有不同说话人**，不要将不同的人合并为同一个 speaker
   - 只纠正 ASR 明确标错的段落（根据音色判断同一人的话被标成了另一个人）
   - 不要仅凭对话内容或角色推断就合并或重新分配说话人
   - 关键判断标准：音色差异（听音频）> 对话上下文 > ASR 给出的 speaker 标签
   - **不确定时不要改**：如果你对某段话的说话人归属没有足够把握，保持 ASR 原始标注不变。宁可漏改，不可错改。错误的 correct_speaker 会导致后续 TTS 用错音色，代价远大于保留一个 ASR 标注不变
   - **不要仅凭身份猜测重分配说话人**：例如"这段话在介绍巴菲特，所以一定是巴菲特说的"是错误推理。主持人介绍嘉宾时说的话仍然属于主持人

   **常见 ASR 错误模式**：
   - 短促回应（Yeah, Sure, Right, 嗯, 对）被分给了错误的人
   - A-B-A 快速交叉（中间 B 实际是 A 的延续）
   - **同一人连续说话被错误切换 speaker**：一个人说了一长段话，ASR 因中间停顿把后半段标成了另一个人。通过音色一致性判断它们属于同一个 speaker
   - **插话/抢话**：某人在另一人说话中途插入，ASR 容易混淆归属。听音频中的声音重叠和音色变化来判断
   - **被打断后继续**：说话人被打断后继续之前的话，ASR 可能标成新的说话人
   - **短促 backchannel**："Yeah, sure" 等极短回应（1-3 词，<2 秒）容易被标错"""

_PROMPT_SPEAKER_CORRECTION_RULES_TEXT = """\
   **⚠ 说话人纠正原则（最高优先级）**：
   - 先通读全部转录稿，判断说话人数量和各自角色
   - **保留 ASR 识别的所有不同说话人**，不要将不同的人合并为同一个 speaker
   - 只纠正 ASR 明确标错的段落（同一人的话被标成了另一个人）
   - 不要仅凭对话内容或角色推断就合并或重新分配说话人
   - 关键判断标准：对话上下文 > ASR 给出的 speaker 标签
   - **不确定时不要改**：如果你对某段话的说话人归属没有足够把握，保持 ASR 原始标注不变。宁可漏改，不可错改。错误的 correct_speaker 会导致后续 TTS 用错音色，代价远大于保留一个 ASR 标注不变
   - **不要仅凭身份猜测重分配说话人**：例如"这段话在介绍巴菲特，所以一定是巴菲特说的"是错误推理。主持人介绍嘉宾时说的话仍然属于主持人

   **常见 ASR 错误模式**：
   - 短促回应（Yeah, Sure, Right, 嗯, 对）被分给了错误的人
   - A-B-A 快速交叉（中间 B 实际是 A 的延续）
   - **同一人连续说话被错误切换 speaker**：一个人说了一长段话，ASR 因中间停顿把后半段标成了另一个人。判断标准：前后内容连贯、角色一致，应属于同一个 speaker
   - **插话/抢话**：某人在另一人说话中途插入，ASR 容易混淆归属。根据对话语义和时间间隔来判断
   - **被打断后继续**：说话人被打断后继续之前的话，ASR 可能标成新的说话人
   - **短促 backchannel**："Yeah, sure" 等极短回应（1-3 词，<2 秒）容易被标错"""

_PROMPT_OUTPUT_FORMAT = """\
## 输出 JSON 格式（严格遵循，不要添加其他字段）

{{
  "speakers": {{
    "speaker_a": {{"name": "中文姓名", "gender": "female", "age_group": "middle", "role": "角色描述", "style": "语气描述", "voice_description": "声音清晰专业，语速适中"}},
    "speaker_b": {{"name": "中文姓名", "gender": "male", "age_group": "elderly", "role": "角色描述", "style": "语气描述", "voice_description": "声音低沉沙哑，语速缓慢"}},
    "speaker_c": {{"name": "中文姓名（如有第三位及更多说话人，都要列出）", "gender": "male", "age_group": "middle", "role": "角色描述", "style": "语气描述", "voice_description": "声音特征描述"}}
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

1. **识别说话人身份**：
   - 从视频标题、对话内容中查找所有被提及的人名（如 "Let's bring in Ryan Reilly" → 下一位说话人是 Ryan Reilly）
   - 根据音频声音特征区分不同说话人，将人名与 speaker 对应
   - **每个 speaker 都必须尽力识别真实姓名**，不要留空或用 "Speaker B" 代替。如果对话中有人被称呼或介绍，把名字关联到对应的 speaker
   - 姓名统一使用中文（如 Warren Buffett → 沃伦·巴菲特，Becky Quick → 贝基·奎克）
   - 如果实在无法确定姓名，标注为"未知说话人"并说明原因
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

1. **识别说话人身份**：
   - 从视频标题、对话内容中查找所有被提及的人名（如 "Let's bring in Ryan Reilly" → 下一位说话人是 Ryan Reilly）
   - 根据对话上下文区分不同说话人，将人名与 speaker 对应
   - **每个 speaker 都必须尽力识别真实姓名**，不要留空或用 "Speaker B" 代替
   - 姓名统一使用中文（如 Warren Buffett → 沃伦·巴菲特，Becky Quick → 贝基·奎克）
   - 如果实在无法确定姓名，标注为"未知说话人"并说明原因
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
    debug_output_dir: str | Path | None = None,
    mode: str = "studio",
    skip_pass1: bool = False,
) -> ReviewResult | None:
    """Unified entry point for LLM transcript review.

    Orchestrates Pass 1 (speaker) → Pass 2 (text).  Falls back to the
    legacy single-pass path when either pass fails.

    Parameters
    ----------
    mode : "studio" | "express" — determines which model set to use.
    skip_pass1 : if True, skip Pass 1 speaker identification (Express optimization).
    """
    try:
        return _orchestrate_three_pass(
            lines,
            audio_path=audio_path,
            video_title=video_title,
            video_url=video_url,
            words_data=words_data,
            debug_output_dir=debug_output_dir,
            mode=mode,
            skip_pass1=skip_pass1,
        )
    except _PassFailure as exc:
        logger.warning("[S2] Pass %s failed after retries: %s", exc.pass_name, exc)
        return None
    except Exception as exc:
        logger.warning("[S2] Three-pass orchestrator failed: %s", exc)
        return None


class _PassFailure(Exception):
    """Raised when Pass 1 or Pass 2 fails, triggering legacy fallback."""
    def __init__(self, pass_name: str, reason: str = ""):
        self.pass_name = pass_name
        super().__init__(f"Pass {pass_name}: {reason}" if reason else f"Pass {pass_name} failed")


def _orchestrate_three_pass(
    lines: list,
    *,
    audio_path: str | Path | None = None,
    video_title: str = "",
    video_url: str = "",
    words_data: list[dict] | None = None,
    debug_output_dir: str | Path | None = None,
    mode: str = "studio",
    skip_pass1: bool = False,
) -> ReviewResult | None:
    """Internal orchestrator: Pass 1 (speakers) → Pass 2 (text) → aggregate.

    Parameters
    ----------
    mode : "studio" | "express" — model set selection.
    skip_pass1 : skip Pass 1 speaker identification (Express optimization).
    """
    # Resolve per-pass models from llm_registry
    pass1_model = _get_prompt_model(mode, "pass1") if not skip_pass1 else None
    pass2_model = _get_prompt_model(mode, "pass2")

    # MiMo Omni for pass1 → legacy single-pass (text-only, no three-pass)
    if pass1_model == "mimo_omni" and not skip_pass1:
        return legacy_review_transcript_single_pass(
            lines,
            audio_path=audio_path,
            video_title=video_title,
            video_url=video_url,
            words_data=words_data,
            debug_output_dir=debug_output_dir,
        )

    if not lines:
        return None

    # --- Audio preparation (shared with Pass 1) ---
    original_audio: Path | None = None
    review_tmp_dir: Path | None = None
    compressed_audio: Path | None = None

    if not skip_pass1 and audio_path and Path(audio_path).exists():
        original_audio = Path(audio_path)
        review_tmp_dir = original_audio.parent / ".review_tmp"
        compressed_audio = _try_compress_audio(original_audio, review_tmp_dir)

    # --- Pass 1: Speaker identification + correction ---
    if skip_pass1:
        # Express optimization: use ASR speaker labels as-is
        speaker_ids = _detect_speaker_ids_from_lines(lines)
        pass1_result = {
            "speakers": {sid: {"name": sid.replace("_", " ").title()} for sid in speaker_ids},
            "corrections": [],
            "raw_corrections": [],
            "contract_violations": [],
            "has_audio": False,
            "response_text": "",
            "parsed_payload": {},
            "success_attempt_label": "skipped",
            "success_attempt_model": None,
        }
        pass1_lines = lines
        pass1_applied = 0
        sanity_applied = 0
        original_snapshot = _snapshot_lines(lines)
        after_corrections_snapshot = original_snapshot
        after_sanity_snapshot = original_snapshot
        logger.info("[S2] Express mode: skipping Pass 1 (speaker identification)")
    else:
        # Pass 1 uses Gemini (audio required)
        pass1_api_key = _get_model_api_key(pass1_model)
        pass1_result = _review_pass1_speakers(
            lines=lines,
            api_key=pass1_api_key,
            audio_path=compressed_audio,
            video_title=video_title,
            video_url=video_url,
            review_model=pass1_model,
            debug_output_dir=debug_output_dir,
        )

        # Apply Pass 1 corrections (correct_speaker only)
        original_snapshot = _snapshot_lines(lines)
        pass1_lines, pass1_applied = _apply_corrections(
            lines, pass1_result["corrections"], words_data=words_data,
        )
        after_corrections_snapshot = _snapshot_lines(pass1_lines)

        # Interview sanity check DISABLED — Pass 1 audio analysis is more
        # accurate than text-based heuristics, and the rules were overriding
        # correct LLM corrections (e.g. "I think" forced to guest).
        sanity_applied = 0
        after_sanity_snapshot = after_corrections_snapshot

    # --- Pass 2: Text correction + split + glossary (no audio) ---
    pass2_api_key = _get_model_api_key(pass2_model)
    pass2_result = _review_pass2_text(
        lines=pass1_lines,
        speakers=pass1_result["speakers"],
        api_key=pass2_api_key,
        video_title=video_title,
        review_model=pass2_model,
        debug_output_dir=debug_output_dir,
    )

    # Apply Pass 2 corrections (fix_text + split only)
    pass2_lines, pass2_applied = _apply_corrections(
        pass1_lines, pass2_result["corrections"], words_data=words_data,
    )

    # Final safety: enforce max duration
    final_lines = _enforce_max_duration(pass2_lines, words_data=words_data)

    # Re-index
    for i, line in enumerate(final_lines):
        line.index = i + 1
    final_snapshot = _snapshot_lines(final_lines)

    total_applied = pass1_applied + sanity_applied + pass2_applied

    # --- Write debug artifacts ---
    debug_artifacts: dict[str, str] = {}
    if debug_output_dir is not None:
        output_root = Path(debug_output_dir).resolve(strict=False)
        output_root.mkdir(parents=True, exist_ok=True)

        # Pass 1 result
        _write_pass_artifact(output_root / "s2_pass1_result.json", {
            "pass": "pass1_speakers",
            "review_model": pass1_model or "(skipped)",
            "skipped": skip_pass1,
            "prompt_version": "v1",
            "has_audio": original_audio is not None and not skip_pass1,
            "fallback_used": pass1_result.get("success_attempt_label", "primary") not in ("primary", "skipped"),
            "success_attempt_label": pass1_result.get("success_attempt_label", "primary"),
            "success_attempt_model": pass1_result.get("success_attempt_model"),
            "generated_at": _now_iso(),
            "duration_ms": pass1_result.get("duration_ms"),
            "speakers": pass1_result["speakers"],
            "corrections": pass1_result["raw_corrections"],
            "corrections_applied": pass1_applied,
            "sanity_applied": sanity_applied,
            "contract_violations": pass1_result.get("contract_violations", []),
        })

        # Pass 2 result
        _write_pass_artifact(output_root / "s2_pass2_result.json", {
            "pass": "pass2_text",
            "review_model": pass2_model,
            "prompt_version": "v1",
            "has_audio": False,
            "fallback_used": pass2_result.get("success_attempt_label", "primary") != "primary",
            "success_attempt_label": pass2_result.get("success_attempt_label", "primary"),
            "success_attempt_model": pass2_result.get("success_attempt_model"),
            "generated_at": _now_iso(),
            "duration_ms": pass2_result.get("duration_ms"),
            "glossary": pass2_result["glossary"],
            "corrections": pass2_result["raw_corrections"],
            "corrections_applied": pass2_applied,
            "contract_violations": pass2_result.get("contract_violations", []),
        })

        # Build review_events from Pass 1/2 raw responses
        review_events: list[dict[str, Any]] = []
        review_events.append({
            "pass": "pass1_speakers",
            "model": _resolve_model_id(pass1_model) if pass1_model else "(skipped)",
            "review_model": pass1_model or "(skipped)",
            "has_audio": pass1_result.get("has_audio", False),
            "response_text": pass1_result.get("response_text", ""),
            "parsed_payload": pass1_result.get("parsed_payload", {}),
        })
        review_events.append({
            "pass": "pass2_text",
            "model": _resolve_model_id(pass2_model),
            "review_model": pass2_model,
            "has_audio": False,
            "response_text": pass2_result.get("response_text", ""),
            "parsed_payload": pass2_result.get("parsed_payload", {}),
        })

        # Aggregated result (same format as legacy, 排障首选)
        debug_artifacts = _write_review_debug_artifacts(
            debug_output_dir,
            review_events=review_events,
            original_snapshot=original_snapshot,
            after_corrections_snapshot=after_corrections_snapshot,
            after_sanity_snapshot=after_sanity_snapshot,
            final_snapshot=final_snapshot,
            speakers=pass1_result["speakers"],
            glossary=pass2_result["glossary"],
            raw_corrections=pass1_result["raw_corrections"] + pass2_result["raw_corrections"],
            corrections_applied=total_applied - sanity_applied,
            sanity_applied=sanity_applied,
            review_model=pass1_model or pass2_model,
            has_audio=original_audio is not None and not skip_pass1,
        )

    logger.info(
        "[S2] Three-pass review: pass1=%d corrections (+%d sanity), pass2=%d corrections, %d→%d lines",
        pass1_applied, sanity_applied, pass2_applied, len(lines), len(final_lines),
    )

    return ReviewResult(
        speakers=pass1_result["speakers"],
        glossary=pass2_result["glossary"],
        corrections_applied=total_applied,
        lines=final_lines,
        debug_artifacts=debug_artifacts,
    )


# ---------------------------------------------------------------------------
# Pass 1: Speaker identification + correction
# ---------------------------------------------------------------------------

_PASS1_PROMPT = """\
你是转录审校专家。根据音频和上下文，完成以下任务：

1. **识别每个说话人的身份**：姓名和角色
2. **纠正 ASR 的说话人标注错误**：听音频判断，标错的用 `correct_speaker` 修正
3. **拆分混合发言段落**：如果一段音频中包含多个说话人，用 `split` 在切换点拆开，并用 `speaker_after` 标注后半段的说话人

视频标题：{video_title}
视频链接：{video_url}

格式要求：
- 姓名使用中文（如 Warren Buffett → 沃伦·巴菲特）
- 多个未知说话人按编号区分：未知说话人1、未知说话人2
- 保留所有已有的 speaker_id，不要删除
- 只允许输出 `correct_speaker` 和 `split`，不要输出 `fix_text` / `merge`
- 不要输出 gender、age_group、style（由后续音色分析阶段处理）

输出 JSON，且只能输出 JSON：

{{
  "speakers": {{
    "speaker_a": {{"name": "中文姓名", "role": "角色"}},
    "speaker_b": {{"name": "中文姓名", "role": ""}}
  }},
  "corrections": [
    {{"action": "correct_speaker", "index": 12, "to": "speaker_b", "reason": "原因"}},
    {{"action": "split", "index": 2, "at_text": "切换点文本", "speaker_after": "speaker_a", "reason": "原因"}}
  ]
}}

转录稿（{line_count} 行）：

{transcript_body}"""

_PASS1_ALLOWED_ACTIONS = frozenset({"correct_speaker", "split"})


def _review_pass1_speakers(
    *,
    lines: list,
    api_key: str,
    audio_path: Path | None,
    video_title: str,
    video_url: str,
    review_model: str,
    debug_output_dir: str | Path | None = None,
) -> dict:
    """Pass 1: speaker identification + correct_speaker corrections.

    Returns dict with keys: speakers, corrections, raw_corrections, contract_violations.
    Raises ``_PassFailure`` on API / JSON / missing-field failure.
    """
    transcript_body = _build_transcript_body(lines)
    prompt_template = _get_admin_prompt_override("pass1") or _PASS1_PROMPT
    prompt = prompt_template.format(
        video_title=video_title or "(unknown)",
        video_url=video_url or "(unknown)",
        line_count=len(lines),
        transcript_body=transcript_body,
    )

    api_model_id = _resolve_model_id(review_model)
    try:
        types = _load_genai_types()
        client = _create_review_client(api_key=api_key)

        contents: list = []
        has_audio = False

        _MIME_MAP = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".flac": "audio/flac", ".ogg": "audio/ogg"}
        if audio_path and audio_path.exists():
            try:
                audio_bytes = audio_path.read_bytes()
                mime_type = _MIME_MAP.get(audio_path.suffix.lower(), "audio/wav")
                audio_part = types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)
                contents.append(audio_part)
                has_audio = True
                logger.info("[S2][Pass1] Audio loaded inline (%d KB)", len(audio_bytes) // 1024)
            except Exception as e:
                logger.warning("[S2][Pass1] Audio load failed: %s", e)

        contents.append(prompt)

        # Smart retry: transient errors → retry same model; output errors → fallback
        _fallback_models = get_fallback_candidates(review_model, requires_audio=True)
        _cheapest = _fallback_models[-1] if _fallback_models else None
        _second_cheapest = _fallback_models[-2] if len(_fallback_models) >= 2 else None

        # Build fallback chain: primary → (retry if transient) → cheapest → second_cheapest
        _fallback_chain: list[tuple[str, str, str]] = []  # (label, model_name, provider)
        if _cheapest and _cheapest != review_model:
            _fallback_chain.append(("cheapest", _cheapest, _MODEL_REGISTRY.get(_cheapest, {}).get("provider", "gemini")))
        if _second_cheapest and _second_cheapest != review_model:
            _fallback_chain.append(("2nd_cheapest", _second_cheapest, _MODEL_REGISTRY.get(_second_cheapest, {}).get("provider", "gemini")))

        _pass1_last_error: Exception | None = None
        _retried_transient = False
        _t0 = time.monotonic()
        response_text: str | None = None
        _success_label = "primary"
        _success_model = review_model

        def _attempt_gemini(label: str, model_id: str) -> dict:
            nonlocal response_text
            response = client.models.generate_content(
                model=model_id,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                    max_output_tokens=65536,
                    thinking_config=types.ThinkingConfig(thinking_budget=1024),
                ),
            )
            response_text = _extract_text(response)
            if not response_text:
                raise json.JSONDecodeError("empty response", "", 0)
            result = json.loads(response_text)
            _dump_retry_response(debug_output_dir, "pass1", 0, label, model_id, response_text, None)
            return result

        def _attempt_mimo(label: str, model_name: str) -> dict:
            nonlocal response_text
            mimo_key = _get_model_api_key("mimo_omni") or ""
            mimo_model_id = _resolve_model_id(model_name)
            response_text = _call_mimo_omni_raw(
                api_key=mimo_key,
                prompt=prompt,
                model_id=mimo_model_id,
                audio_paths=[audio_path] if audio_path and audio_path.exists() else None,
            )
            result = json.loads(response_text)
            _dump_retry_response(debug_output_dir, "pass1", 0, label, mimo_model_id, response_text, None)
            return result

        # --- Primary attempt ---
        try:
            payload = _attempt_gemini("primary", api_model_id)
        except Exception as exc:
            _pass1_last_error = exc
            _dump_retry_response(debug_output_dir, "pass1", 0, "primary", api_model_id, response_text, exc)

            # Transient → retry same model once
            if _is_transient_error(exc) and not _retried_transient:
                _retried_transient = True
                logger.warning("[S2][Pass1] Transient error (%s, model=%s), retrying in %ds: %s",
                               "primary", api_model_id, _TRANSIENT_RETRY_WAIT_S, exc)
                time.sleep(_TRANSIENT_RETRY_WAIT_S)
                try:
                    payload = _attempt_gemini("retry", api_model_id)
                except Exception as retry_exc:
                    _pass1_last_error = retry_exc
                    _dump_retry_response(debug_output_dir, "pass1", 1, "retry", api_model_id, response_text, retry_exc)
                    logger.warning("[S2][Pass1] Retry also failed (%s): %s", api_model_id, retry_exc)
                    payload = None
                else:
                    logger.info("[S2][Pass1] Succeeded on retry (model=%s)", api_model_id)
                    _success_label = "retry"
                    payload = payload  # already set
            else:
                logger.warning("[S2][Pass1] Output error (%s, model=%s), skipping retry: %s",
                               "primary", api_model_id, exc)
                payload = None

        # --- Fallback chain ---
        if payload is None:
            for _fb_label, _fb_model, _fb_provider in _fallback_chain:
                try:
                    if _fb_provider == "mimo":
                        payload = _attempt_mimo(_fb_label, _fb_model)
                    else:
                        _fb_model_id = _resolve_model_id(_fb_model)
                        payload = _attempt_gemini(_fb_label, _fb_model_id)
                    logger.info("[S2][Pass1] Succeeded on %s (model=%s)", _fb_label, _fb_model)
                    _success_label = _fb_label
                    _success_model = _fb_model
                    break
                except Exception as fb_exc:
                    _pass1_last_error = fb_exc
                    _dump_retry_response(debug_output_dir, "pass1", 0, _fb_label, _resolve_model_id(_fb_model), response_text, fb_exc)
                    logger.warning("[S2][Pass1] %s failed (model=%s): %s", _fb_label, _fb_model, fb_exc)
                    continue

        if payload is None:
            raise _PassFailure("1", f"all attempts failed: {_pass1_last_error}")

    except _PassFailure:
        raise
    except Exception as exc:
        raise _PassFailure("1", f"API call failed: {exc}") from exc

    speakers = payload.get("speakers")
    if not speakers or not isinstance(speakers, dict):
        raise _PassFailure("1", "missing or invalid 'speakers' field")

    # --- Contract enforcement: only correct_speaker allowed ---
    raw_corrections = payload.get("corrections", [])
    contract_violations: list[dict] = []
    filtered_corrections: list[dict] = []

    for c in raw_corrections:
        action = c.get("action", "")
        if action in _PASS1_ALLOWED_ACTIONS:
            filtered_corrections.append(c)
        else:
            contract_violations.append({"dropped_action": action, "correction": c})
            logger.warning("[S2][Pass1] Contract violation: dropped %s correction", action)

    # Drop any glossary the model sneaked in
    if "glossary" in payload:
        contract_violations.append({"dropped_field": "glossary"})
        logger.warning("[S2][Pass1] Contract violation: dropped glossary")

    logger.info(
        "[S2][Pass1] %d speakers, %d corrections (%d filtered), audio=%s",
        len(speakers), len(filtered_corrections), len(contract_violations), has_audio,
    )

    _duration_ms = int((time.monotonic() - _t0) * 1000)
    return {
        "speakers": speakers,
        "corrections": filtered_corrections,
        "raw_corrections": raw_corrections,
        "contract_violations": contract_violations,
        "response_text": response_text,
        "parsed_payload": payload,
        "has_audio": has_audio,
        "duration_ms": _duration_ms,
        "success_attempt_label": _success_label,
        "success_attempt_model": _success_model,
    }


# ---------------------------------------------------------------------------
# Pass 2: Text correction + split + glossary
# ---------------------------------------------------------------------------

_PASS2_PROMPT = """\
你正在执行视频转录审校的 Pass 2。Pass 1 已经完成 speaker 识别与 speaker 纠正。
你的唯一目标是：
1. 修正文本文字错误
2. 对过长段落做语义拆分
3. 提取术语表

你不是在做 speaker 重分配，不是在做音色描述，不是在做身份识别。

输入信息：
- 视频标题：{video_title}
- 已校正 speaker 的转录文本：{transcript_body}
- speakers 信息：{speakers_json}

必须遵守的规则：
1. 绝对不要修改任何 speaker_id
2. 绝对不要输出 `correct_speaker`
3. 不要输出 `merge`
4. 只允许输出：
   - `fix_text`
   - `split`
   - `glossary`
5. `fix_text` 只修正明显 ASR 错误、重复、漏词、错词
6. 不要改写语气，不要润色，不要重写内容
7. 不要改变原文核心含义
8. `split` 只用于过长段落（>60s），并且必须在自然语义断点切开
9. 如果某段并不适合拆分，就不要强行拆分
10. glossary 只收录稳定、值得后续翻译统一的专名、机构名、术语、人名

输出 JSON，且只能输出 JSON：

{{
  "corrections": [
    {{
      "action": "fix_text",
      "index": 5,
      "old": "原错误文本",
      "new": "修正后文本",
      "reason": "简短说明"
    }},
    {{
      "action": "split",
      "index": 18,
      "at_text": "建议切分点附近的文本",
      "reason": "该段过长，需要在自然断点拆分"
    }}
  ],
  "glossary": {{
    "Berkshire Hathaway": "伯克希尔·哈撒韦",
    "Greg Abel": "格雷格·艾贝尔"
  }}
}}

转录稿（{line_count} 行）：

{transcript_body}"""

_PASS2_ALLOWED_ACTIONS = frozenset({"fix_text", "split"})


def _call_text_llm(
    *,
    model_name: str,
    api_key: str | None,
    prompt: str,
    max_output_tokens: int = 65536,
) -> str:
    """Call an LLM for text-only tasks, dispatching by provider.

    Gemini → google-genai SDK;  MiMo → xiaomimimo API;
    DeepSeek/OpenAI → OpenAI-compatible HTTP.
    """
    info = _MODEL_REGISTRY.get(model_name, {})
    provider = info.get("provider", "gemini")
    api_model_id = _resolve_model_id(model_name)

    if provider == "gemini":
        types = _load_genai_types()
        client = _create_review_client(api_key=api_key)
        response = client.models.generate_content(
            model=api_model_id,
            contents=[prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
                max_output_tokens=max_output_tokens,
                thinking_config=types.ThinkingConfig(thinking_budget=1024),
            ),
        )
        return _extract_text(response) or ""

    # Non-Gemini: OpenAI-compatible HTTP call
    effective_key = api_key or ""
    if not effective_key:
        env_var = info.get("api_key_env", "")
        effective_key = os.environ.get(env_var, "").strip() if env_var else ""
    if not effective_key:
        raise RuntimeError(f"{model_name} API key not configured")

    if provider == "mimo":
        url = _MIMO_OMNI_API_URL
    elif provider == "deepseek":
        url = "https://api.deepseek.com/v1/chat/completions"
    else:
        url = "https://api.openai.com/v1/chat/completions"

    payload = {
        "model": api_model_id,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": max_output_tokens,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {effective_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"]


def _review_pass2_text(
    *,
    lines: list,
    speakers: dict,
    api_key: str | None,
    video_title: str,
    review_model: str,
    debug_output_dir: str | Path | None = None,
) -> dict:
    """Pass 2: text correction + split + glossary.  Pure text, no audio.

    Supports any provider (Gemini, DeepSeek, OpenAI, MiMo) via _call_text_llm.

    Returns dict with keys: glossary, corrections, raw_corrections, contract_violations.
    Raises ``_PassFailure`` on API / JSON failure.
    """
    transcript_body = _build_transcript_body(lines)
    speakers_json = json.dumps(speakers, ensure_ascii=False, indent=2)
    prompt_template = _get_admin_prompt_override("pass2") or _PASS2_PROMPT
    prompt = prompt_template.format(
        video_title=video_title or "(unknown)",
        line_count=len(lines),
        transcript_body=transcript_body,
        speakers_json=speakers_json,
    )

    # Smart retry: transient → retry same model; output error → fallback
    _fallback_models = get_fallback_candidates(review_model, requires_audio=False)
    _cheapest = _fallback_models[-1] if _fallback_models else None
    _second_cheapest = _fallback_models[-2] if len(_fallback_models) >= 2 else None

    _fallback_chain_p2: list[tuple[str, str]] = []
    if _cheapest and _cheapest != review_model:
        _fallback_chain_p2.append(("cheapest", _cheapest))
    if _second_cheapest and _second_cheapest != review_model:
        _fallback_chain_p2.append(("2nd_cheapest", _second_cheapest))

    _pass2_last_error: Exception | None = None
    _t0 = time.monotonic()
    payload: dict = {}
    response_text: str | None = None
    _success_label = "primary"
    _success_model = review_model

    def _attempt_p2(label: str, model: str, key: str | None) -> dict:
        nonlocal response_text
        response_text = _call_text_llm(
            model_name=model, api_key=key, prompt=prompt, max_output_tokens=65536,
        )
        if not response_text:
            raise json.JSONDecodeError("empty response", "", 0)
        result = json.loads(response_text)
        _dump_retry_response(debug_output_dir, "pass2", 0, label, _resolve_model_id(model), response_text, None)
        return result

    try:
        # --- Primary attempt ---
        try:
            payload = _attempt_p2("primary", review_model, api_key)
        except Exception as exc:
            _pass2_last_error = exc
            _dump_retry_response(debug_output_dir, "pass2", 0, "primary", _resolve_model_id(review_model), response_text, exc)

            if _is_transient_error(exc):
                logger.warning("[S2][Pass2] Transient error (%s), retrying in %ds: %s",
                               review_model, _TRANSIENT_RETRY_WAIT_S, exc)
                time.sleep(_TRANSIENT_RETRY_WAIT_S)
                try:
                    payload = _attempt_p2("retry", review_model, api_key)
                    _success_label = "retry"
                    logger.info("[S2][Pass2] Succeeded on retry (model=%s)", review_model)
                except Exception as retry_exc:
                    _pass2_last_error = retry_exc
                    _dump_retry_response(debug_output_dir, "pass2", 1, "retry", _resolve_model_id(review_model), response_text, retry_exc)
                    logger.warning("[S2][Pass2] Retry failed (%s): %s", review_model, retry_exc)
                    payload = {}
            else:
                logger.warning("[S2][Pass2] Output error (%s), skipping retry: %s", review_model, exc)
                payload = {}

        # --- Fallback chain ---
        if not payload:
            for _fb_label, _fb_model in _fallback_chain_p2:
                _fb_key = _get_model_api_key(_fb_model)
                try:
                    payload = _attempt_p2(_fb_label, _fb_model, _fb_key)
                    _success_label = _fb_label
                    _success_model = _fb_model
                    logger.info("[S2][Pass2] Succeeded on %s (model=%s)", _fb_label, _fb_model)
                    break
                except Exception as fb_exc:
                    _pass2_last_error = fb_exc
                    _dump_retry_response(debug_output_dir, "pass2", 0, _fb_label, _resolve_model_id(_fb_model), response_text, fb_exc)
                    logger.warning("[S2][Pass2] %s failed (model=%s): %s", _fb_label, _fb_model, fb_exc)

        if not payload:
            raise _PassFailure("2", f"all attempts failed: {_pass2_last_error}")
    except _PassFailure:
        raise
    except Exception as exc:
        raise _PassFailure("2", f"API call failed: {exc}") from exc

    # --- Contract enforcement: only fix_text + split allowed ---
    raw_corrections = payload.get("corrections", [])
    contract_violations: list[dict] = []
    filtered_corrections: list[dict] = []

    for c in raw_corrections:
        action = c.get("action", "")
        if action in _PASS2_ALLOWED_ACTIONS:
            filtered_corrections.append(c)
        else:
            contract_violations.append({"dropped_action": action, "correction": c})
            logger.warning("[S2][Pass2] Contract violation: dropped %s correction", action)

    # Drop any speakers the model sneaked in
    if "speakers" in payload:
        contract_violations.append({"dropped_field": "speakers"})
        logger.warning("[S2][Pass2] Contract violation: dropped speakers")

    glossary = payload.get("glossary", {})
    if not isinstance(glossary, dict):
        glossary = {}

    logger.info(
        "[S2][Pass2] %d corrections (%d filtered), %d glossary terms",
        len(filtered_corrections), len(contract_violations), len(glossary),
    )

    _duration_ms = int((time.monotonic() - _t0) * 1000)
    return {
        "glossary": glossary,
        "corrections": filtered_corrections,
        "raw_corrections": raw_corrections,
        "contract_violations": contract_violations,
        "response_text": response_text,
        "parsed_payload": payload,
        "duration_ms": _duration_ms,
        "success_attempt_label": _success_label,
        "success_attempt_model": _success_model,
    }


# ---------------------------------------------------------------------------
# Pass 3: Voice profile (called separately from pipeline, after translation review)
# ---------------------------------------------------------------------------

_PASS3_PROMPT = """\
你正在执行视频音色画像分析的 Pass 3。
前两个阶段已经完成 speaker 识别、speaker 纠正、文本修正与术语表提取。
你的唯一目标是：根据每个 speaker 的代表性音频片段，生成适合 TTS 选音匹配的音色画像。

你不是在做 speaker 纠正，不是在做文本修正，不是在做术语表。

输入信息：
- 视频标题：{video_title}
- speaker 基础信息：{speakers_json}
- 当前 speaker 列表：{speaker_ids}
- 每个 speaker 的代表音频片段（单独提供）

必须遵守的规则：
1. 不要输出 corrections
2. 不要输出 glossary
3. 只输出每个 speaker 的音色画像
4. voice_description 要面向 TTS 匹配，描述声音特征，不要写成人物背景介绍
5. style 描述说话风格（如"专业、稳重"、"信息丰富、分析性强"）
6. gender 只能是：male / female / unknown
7. age_group 只能是：young / middle / elderly / unknown
8. persona_style 尽量从以下集合中选最接近者：
   - professional
   - warm
   - serious
   - energetic
   - calm
9. energy_level 只能是：low / medium / high
10. 不确定时可以输出 `unknown`，不要强猜

输出 JSON，且只能输出 JSON：

{{
  "speaker_profiles": {{
    "speaker_a": {{
      "voice_description": "声音清晰、语速中等偏快、音高偏中高，整体专业且稳定",
      "style": "专业、稳重",
      "gender": "female",
      "age_group": "middle",
      "persona_style": "professional",
      "energy_level": "medium"
    }},
    "speaker_b": {{
      "voice_description": "声音偏低沉，语速较慢，带停顿感，整体沉稳",
      "style": "沉稳、分析性强",
      "gender": "male",
      "age_group": "elderly",
      "persona_style": "calm",
      "energy_level": "low"
    }}
  }}
}}"""

# Audio extraction constants for Pass 3
_PASS3_MIN_CLIP_DURATION_S = 15
_PASS3_MAX_CLIP_DURATION_S = 30
_PASS3_AUDIO_BITRATE = "32k"


def _extract_speaker_audio_clips(
    lines: list,
    source_audio: Path,
    tmp_dir: Path,
) -> dict[str, Path]:
    """Extract representative audio clip for each speaker.

    Strategy per speaker:
    1. Find the longest continuous utterance
    2. If <15s, concatenate adjacent same-speaker utterances until 15-30s
    3. ffmpeg extract + compress to opus

    Returns {speaker_id: clip_path}.
    """
    from collections import defaultdict

    # Group utterances by speaker
    speaker_utterances: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for line in lines:
        speaker_utterances[line.speaker_id].append((line.start_ms, line.end_ms))

    tmp_dir.mkdir(parents=True, exist_ok=True)
    clips: dict[str, Path] = {}

    for speaker_id, utterances in speaker_utterances.items():
        # Sort by start time
        utterances.sort()

        # Find the longest single utterance
        best_start, best_end = max(utterances, key=lambda u: u[1] - u[0])
        best_duration_s = (best_end - best_start) / 1000

        if best_duration_s < _PASS3_MIN_CLIP_DURATION_S and len(utterances) > 1:
            # Extend by finding the densest cluster of utterances
            # Start from the longest utterance and expand outward
            best_idx = next(i for i, u in enumerate(utterances) if u == (best_start, best_end))
            clip_start = best_start
            clip_end = best_end

            # Expand forward
            for i in range(best_idx + 1, len(utterances)):
                gap = utterances[i][0] - clip_end
                if gap > 5000:  # skip if gap > 5s
                    break
                clip_end = utterances[i][1]
                if (clip_end - clip_start) / 1000 >= _PASS3_MAX_CLIP_DURATION_S:
                    break

            # Expand backward if still short
            if (clip_end - clip_start) / 1000 < _PASS3_MIN_CLIP_DURATION_S:
                for i in range(best_idx - 1, -1, -1):
                    gap = clip_start - utterances[i][1]
                    if gap > 5000:
                        break
                    clip_start = utterances[i][0]
                    if (clip_end - clip_start) / 1000 >= _PASS3_MAX_CLIP_DURATION_S:
                        break

            best_start, best_end = clip_start, clip_end

        # Cap at max duration
        if (best_end - best_start) / 1000 > _PASS3_MAX_CLIP_DURATION_S:
            best_end = best_start + _PASS3_MAX_CLIP_DURATION_S * 1000

        # Extract clip via ffmpeg
        clip_path = tmp_dir / f"pass3_{speaker_id}.ogg"
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{best_start / 1000:.3f}",
            "-i", str(source_audio),
            "-t", f"{(best_end - best_start) / 1000:.3f}",
            "-ac", "1",
            "-ar", "16000",
            "-c:a", "libopus",
            "-b:a", _PASS3_AUDIO_BITRATE,
            str(clip_path),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=120)
            if clip_path.exists() and clip_path.stat().st_size > 0:
                clips[speaker_id] = clip_path
                logger.info(
                    "[S2][Pass3] Extracted %s clip: %.1fs-%.1fs (%.1fs)",
                    speaker_id, best_start / 1000, best_end / 1000,
                    (best_end - best_start) / 1000,
                )
            else:
                logger.warning("[S2][Pass3] Empty clip for %s", speaker_id)
        except Exception as exc:
            logger.warning("[S2][Pass3] Failed to extract clip for %s: %s", speaker_id, exc)

    return clips


def review_pass3_voice_profiles(
    lines: list,
    *,
    source_audio_path: Path | None,
    speakers: dict[str, dict],
    video_title: str = "",
    debug_output_dir: str | Path | None = None,
) -> dict[str, dict]:
    """Pass 3: voice profiling.  Called by pipeline after translation review.

    Returns speaker_profiles dict: {speaker_id: {voice_description, gender, age_group, ...}}.
    On failure, returns ``_fallback_minimal_speaker_styles(speakers)``.
    """
    review_model = _get_review_model()
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        logger.warning("[S2][Pass3] GEMINI_API_KEY not set, using fallback profiles")
        return _fallback_minimal_speaker_styles(speakers)

    if not source_audio_path or not source_audio_path.exists():
        logger.warning("[S2][Pass3] No audio available, using fallback profiles")
        return _fallback_minimal_speaker_styles(speakers)

    if not lines or not speakers:
        return _fallback_minimal_speaker_styles(speakers)

    # Extract per-speaker audio clips
    review_tmp_dir = source_audio_path.parent / ".review_tmp"
    try:
        clips = _extract_speaker_audio_clips(lines, source_audio_path, review_tmp_dir)
    except Exception as exc:
        logger.warning("[S2][Pass3] Audio extraction failed: %s", exc)
        return _fallback_minimal_speaker_styles(speakers)

    if not clips:
        logger.warning("[S2][Pass3] No clips extracted, using fallback profiles")
        return _fallback_minimal_speaker_styles(speakers)

    # Build prompt
    speaker_ids = list(speakers.keys())
    speakers_json = json.dumps(speakers, ensure_ascii=False, indent=2)
    prompt_template = _get_admin_prompt_override("pass3") or _PASS3_PROMPT
    prompt = prompt_template.format(
        video_title=video_title or "(unknown)",
        speakers_json=speakers_json,
        speaker_ids=", ".join(speaker_ids),
    )

    # Build contents: audio clips first, then prompt
    api_model_id = _resolve_model_id(review_model)
    try:
        types = _load_genai_types()
        client = _create_review_client(api_key=api_key)

        contents: list = []
        for spk_id in speaker_ids:
            clip_path = clips.get(spk_id)
            if clip_path and clip_path.exists():
                try:
                    audio_bytes = clip_path.read_bytes()
                    mime_type = {".wav": "audio/wav", ".ogg": "audio/ogg", ".mp3": "audio/mpeg"}.get(clip_path.suffix.lower(), "audio/ogg")
                    audio_part = types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)
                    contents.append(f"[音频片段: {spk_id}]")
                    contents.append(audio_part)
                except Exception as e:
                    logger.warning("[S2][Pass3] Failed to load clip for %s: %s", spk_id, e)

        contents.append(prompt)

        # Smart retry: transient → retry same model; output error → fallback
        _fallback_models = get_fallback_candidates(review_model, requires_audio=True)
        _cheapest_p3 = _fallback_models[-1] if _fallback_models else None
        _second_cheapest_p3 = _fallback_models[-2] if len(_fallback_models) >= 2 else None

        _fb_chain_p3: list[tuple[str, str, str]] = []
        if _cheapest_p3 and _cheapest_p3 != review_model:
            _fb_chain_p3.append(("cheapest", _cheapest_p3, _MODEL_REGISTRY.get(_cheapest_p3, {}).get("provider", "gemini")))
        if _second_cheapest_p3 and _second_cheapest_p3 != review_model:
            _fb_chain_p3.append(("2nd_cheapest", _second_cheapest_p3, _MODEL_REGISTRY.get(_second_cheapest_p3, {}).get("provider", "gemini")))

        # Collect clip paths for mimo fallback
        _clip_paths = [clips[sid] for sid in speaker_ids if sid in clips and clips[sid] and clips[sid].exists()]

        response_text_p3: str | None = None

        def _attempt_gemini_p3(label: str, model_id: str) -> dict:
            nonlocal response_text_p3
            resp = client.models.generate_content(
                model=model_id, contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", temperature=0.1, max_output_tokens=65536,
                    thinking_config=types.ThinkingConfig(thinking_budget=1024),
                ),
            )
            response_text_p3 = _extract_text(resp)
            if not response_text_p3:
                raise json.JSONDecodeError("empty response", "", 0)
            result = json.loads(response_text_p3)
            _dump_retry_response(debug_output_dir, "pass3", 0, label, model_id, response_text_p3, None)
            return result

        def _attempt_mimo_p3(label: str, model_name: str) -> dict:
            nonlocal response_text_p3
            mimo_key = _get_model_api_key("mimo_omni") or ""
            response_text_p3 = _call_mimo_omni_raw(
                api_key=mimo_key, prompt=prompt, model_id=_resolve_model_id(model_name),
                audio_paths=_clip_paths or None,
            )
            result = json.loads(response_text_p3)
            _dump_retry_response(debug_output_dir, "pass3", 0, label, _resolve_model_id(model_name), response_text_p3, None)
            return result

        payload: dict = {}
        _pass3_last_error: Exception | None = None
        _t0_p3 = time.monotonic()
        _success_label_p3 = "primary"
        _success_model_p3 = review_model

        # --- Primary attempt ---
        try:
            payload = _attempt_gemini_p3("primary", api_model_id)
        except Exception as exc:
            _pass3_last_error = exc
            _dump_retry_response(debug_output_dir, "pass3", 0, "primary", api_model_id, response_text_p3, exc)

            if _is_transient_error(exc):
                logger.warning("[S2][Pass3] Transient error, retrying in %ds: %s", _TRANSIENT_RETRY_WAIT_S, exc)
                time.sleep(_TRANSIENT_RETRY_WAIT_S)
                try:
                    payload = _attempt_gemini_p3("retry", api_model_id)
                    _success_label_p3 = "retry"
                    logger.info("[S2][Pass3] Succeeded on retry (model=%s)", api_model_id)
                except Exception as retry_exc:
                    _pass3_last_error = retry_exc
                    _dump_retry_response(debug_output_dir, "pass3", 1, "retry", api_model_id, response_text_p3, retry_exc)
                    logger.warning("[S2][Pass3] Retry failed: %s", retry_exc)
            else:
                logger.warning("[S2][Pass3] Output error, skipping retry: %s", exc)

        # --- Fallback chain ---
        if not payload:
            for _fb_label, _fb_model, _fb_prov in _fb_chain_p3:
                try:
                    if _fb_prov == "mimo":
                        payload = _attempt_mimo_p3(_fb_label, _fb_model)
                    else:
                        payload = _attempt_gemini_p3(_fb_label, _resolve_model_id(_fb_model))
                    _success_label_p3 = _fb_label
                    _success_model_p3 = _fb_model
                    logger.info("[S2][Pass3] Succeeded on %s (model=%s)", _fb_label, _fb_model)
                    break
                except Exception as fb_exc:
                    _pass3_last_error = fb_exc
                    _dump_retry_response(debug_output_dir, "pass3", 0, _fb_label, _resolve_model_id(_fb_model), response_text_p3, fb_exc)
                    logger.warning("[S2][Pass3] %s failed (model=%s): %s", _fb_label, _fb_model, fb_exc)

        if not payload:
            logger.warning("[S2][Pass3] All attempts failed, using fallback profiles")
            return _fallback_minimal_speaker_styles(speakers)

    except Exception as exc:
        logger.warning("[S2][Pass3] API call failed: %s, using fallback", exc)
        return _fallback_minimal_speaker_styles(speakers)

    # --- Contract enforcement: only speaker_profiles allowed ---
    profiles = payload.get("speaker_profiles", {})
    contract_violations: list[dict] = []

    if "corrections" in payload:
        contract_violations.append({"dropped_field": "corrections"})
        logger.warning("[S2][Pass3] Contract violation: dropped corrections")
    if "glossary" in payload:
        contract_violations.append({"dropped_field": "glossary"})
        logger.warning("[S2][Pass3] Contract violation: dropped glossary")

    if not profiles or not isinstance(profiles, dict):
        logger.warning("[S2][Pass3] No speaker_profiles in response, using fallback")
        return _fallback_minimal_speaker_styles(speakers)

    # Write Pass 3 artifact
    if debug_output_dir is not None:
        output_root = Path(debug_output_dir).resolve(strict=False)
        output_root.mkdir(parents=True, exist_ok=True)
        _duration_ms_p3 = int((time.monotonic() - _t0_p3) * 1000)
        _write_pass_artifact(output_root / "s2_pass3_result.json", {
            "pass": "pass3_voice_profiles",
            "review_model": review_model,
            "prompt_version": "v1",
            "has_audio": True,
            "fallback_used": _success_label_p3 != "primary",
            "success_attempt_label": _success_label_p3,
            "success_attempt_model": _success_model_p3,
            "generated_at": _now_iso(),
            "duration_ms": _duration_ms_p3,
            "speaker_profiles": profiles,
            "clips_extracted": list(clips.keys()),
            "contract_violations": contract_violations,
        })

    logger.info("[S2][Pass3] Generated voice profiles for %d speakers", len(profiles))
    return profiles


def _fallback_minimal_speaker_styles(speakers: dict[str, dict]) -> dict[str, dict]:
    """Generate minimal voice profiles from existing speaker info (no LLM call)."""
    profiles: dict[str, dict] = {}
    for spk_id, spk_info in speakers.items():
        profiles[spk_id] = {
            "voice_description": spk_info.get("voice_description", ""),
            "gender": spk_info.get("gender", "unknown"),
            "age_group": spk_info.get("age_group", "unknown"),
            "persona_style": spk_info.get("style", ""),
            "energy_level": spk_info.get("energy_level", "") or "medium",
        }
    return profiles


def _write_pass_artifact(path: Path, payload: dict) -> None:
    """Write a per-pass JSON artifact."""
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _now_iso() -> str:
    """Current UTC time in ISO format."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def legacy_review_transcript_single_pass(
    lines: list,
    *,
    audio_path: str | Path | None = None,
    video_title: str = "",
    video_url: str = "",
    words_data: list[dict] | None = None,
    debug_output_dir: str | Path | None = None,
) -> ReviewResult | None:
    """Legacy single-pass review (fallback path).

    This is the original ``review_transcript()`` logic preserved as-is.
    Called when the three-pass orchestrator encounters a failure in Pass 1
    or Pass 2, ensuring the pipeline always has a working fallback.
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

    review_events: list[dict[str, Any]] = []
    debug_artifacts: dict[str, str] = {}

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
            trace_sink=review_events,
            trace_context={"call_type": "single"},
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
            trace_sink=review_events,
        )

    # Apply corrections with validation
    original_snapshot = _snapshot_lines(lines)
    updated_lines, applied_count = _apply_corrections(
        lines, corrections, words_data=words_data
    )
    after_corrections_snapshot = _snapshot_lines(updated_lines)

    # Interview sanity check DISABLED — see comment in _orchestrate_three_pass.
    sanity_applied = 0
    after_sanity_snapshot = after_corrections_snapshot

    # Final safety: ensure no segment > 180s
    final_lines = _enforce_max_duration(updated_lines, words_data=words_data)

    # Re-index
    for i, line in enumerate(final_lines):
        line.index = i + 1
    final_snapshot = _snapshot_lines(final_lines)

    if debug_output_dir is not None:
        debug_artifacts = _write_review_debug_artifacts(
            debug_output_dir,
            review_events=review_events,
            original_snapshot=original_snapshot,
            after_corrections_snapshot=after_corrections_snapshot,
            after_sanity_snapshot=after_sanity_snapshot,
            final_snapshot=final_snapshot,
            speakers=speakers,
            glossary=glossary,
            raw_corrections=corrections,
            corrections_applied=applied_count - sanity_applied,
            sanity_applied=sanity_applied,
            review_model=review_model,
            has_audio=original_audio is not None,
        )

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
        debug_artifacts=debug_artifacts,
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
    trace_sink: list[dict[str, Any]] | None = None,
    trace_context: dict[str, Any] | None = None,
) -> tuple[dict, dict, list] | None:
    """Single LLM call for review. Dispatches to Gemini or MiMo Omni.

    If *cached_content_name* is provided (explicit cache hit for whole-audio),
    the audio is referenced from cache instead of being uploaded again.
    """

    # Resolve logical model name → API model ID
    api_model_id = _resolve_model_id(review_model)
    is_mimo = review_model == "mimo_omni"

    if is_mimo:
        # MiMo Omni now supports audio (multimodal)
        _has_mimo_audio = audio_path is not None and audio_path.exists()
        prompt = _format_prompt(
            has_audio=_has_mimo_audio,
            video_title=video_title,
            video_url=video_url,
            line_count=line_count,
            transcript_body=transcript_body,
        )
        return _call_review_mimo_omni(
            api_key=api_key,
            prompt=prompt,
            model_id=api_model_id,
            audio_paths=[audio_path] if _has_mimo_audio else None,
        )

    try:
        types = _load_genai_types()
        client = _create_review_client(api_key=api_key)

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
                audio_bytes = audio_path.read_bytes()
                file_size_mb = len(audio_bytes) / (1024 * 1024)
                mime_map = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".flac": "audio/flac", ".ogg": "audio/ogg"}
                mime_type = mime_map.get(audio_path.suffix.lower(), "audio/wav")
                audio_part = types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)
                contents.append(audio_part)
                has_audio = True
                logger.info("[Review] Audio loaded inline (%s, %.1f MB)", audio_path.name, file_size_mb)
            except Exception as e:
                logger.warning("[Review] Audio load failed: %s", e)

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
                max_output_tokens=65536,
                thinking_config=types.ThinkingConfig(thinking_budget=1024),
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

        if trace_sink is not None:
            event = {
                "model": api_model_id,
                "review_model": review_model,
                "has_audio": has_audio,
                "line_count": line_count,
                "response_text": response_text,
                "parsed_payload": payload,
            }
            if trace_context:
                event.update(trace_context)
            trace_sink.append(event)

        logger.info(
            "Review response: %d speakers, %d glossary terms, %d corrections (audio=%s)",
            len(speakers), len(glossary), len(corrections), has_audio,
        )
        return speakers, glossary, corrections

    except Exception:
        logger.exception("Unified review LLM call failed")
        return None


def _call_mimo_omni_raw(
    *,
    api_key: str,
    prompt: str,
    model_id: str = "",
    audio_paths: list[Path] | None = None,
    max_tokens: int = 8192,
) -> str:
    """Call MiMo-V2-Omni API and return raw response text.

    Supports multimodal input: when *audio_paths* is provided, audio files
    are base64-encoded and sent as ``input_audio`` parts (OpenAI-compatible).
    Raises on any failure so callers (retry loops) can classify errors.
    """
    import base64 as _b64

    effective_model = model_id or _resolve_model_id("mimo_omni")

    # Build message content — text-only or multimodal
    if audio_paths:
        _AUDIO_FMT = {"wav": "wav", "ogg": "ogg", "mp3": "mp3", "m4a": "mp4", "flac": "flac"}
        content: list[dict[str, object]] = []
        for ap in audio_paths:
            if ap and ap.exists():
                audio_b64 = _b64.b64encode(ap.read_bytes()).decode("utf-8")
                fmt = _AUDIO_FMT.get(ap.suffix.lower().lstrip("."), "wav")
                content.append({"type": "input_audio", "input_audio": {"data": audio_b64, "format": fmt}})
        content.append({"type": "text", "text": prompt})
        msg_content: object = content
    else:
        msg_content = prompt

    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": msg_content}],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }

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
        raise json.JSONDecodeError("empty response from MiMo", "", 0)

    # Strip markdown fences if present
    response_text = response_text.strip()
    if response_text.startswith("```"):
        response_text = re.sub(r"^```(?:json)?\s*", "", response_text)
        response_text = re.sub(r"\s*```$", "", response_text)
    return response_text


def _call_review_mimo_omni(
    *,
    api_key: str,
    prompt: str,
    model_id: str = "",
    audio_paths: list[Path] | None = None,
) -> tuple[dict, dict, list] | None:
    """Call MiMo-V2-Omni API for review (legacy wrapper).

    Supports audio when *audio_paths* is provided.
    Returns (speakers, glossary, corrections) or None on failure.
    """
    try:
        response_text = _call_mimo_omni_raw(
            api_key=api_key,
            prompt=prompt,
            model_id=model_id,
            audio_paths=audio_paths,
        )
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
    trace_sink: list[dict[str, Any]] | None = None,
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
            trace_sink=trace_sink,
            trace_context={
                "call_type": "batch",
                "batch_number": batch_num,
                "batch_start_index": batch[0].index,
                "batch_end_index": batch[-1].index,
            },
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
    api_model_id = _resolve_model_id(review_model) if review_model else _resolve_model_id("gemini_pro")
    try:
        genai = _load_genai()
        types = _load_genai_types()
        from services.gemini.client_factory import create_gemini_client
        client = create_gemini_client(api_key=api_key)

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
    from services.assemblyai.transcriber import TranscriptLine

    # Work on a copy
    working_lines = list(lines)
    applied = 0

    # Build index map for fast lookup
    index_map: dict[int, int] = {line.index: i for i, line in enumerate(working_lines)}

    # Sort: correct_speaker first, then split, then fix_text last.
    # This ensures split runs on original text before fix_text modifies it.
    _ACTION_ORDER = {"correct_speaker": 0, "split": 1, "merge": 2, "fix_text": 3}
    corrections = sorted(corrections, key=lambda c: _ACTION_ORDER.get(c.get("action", ""), 9))

    for c in corrections:
        try:
            action = c.get("action", "")

            if action == "correct_speaker":
                idx = c.get("index")
                to = c.get("to", "")
                if not _SPEAKER_ID_PATTERN.match(to):
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

                # Reject merge across different speakers — never merge
                # different people's words into one segment
                merged_speakers = {working_lines[p].speaker_id for p in positions}
                if len(merged_speakers) > 1:
                    logger.warning(
                        "Merge spans %d speakers %s, skipping",
                        len(merged_speakers), merged_speakers,
                    )
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
                if not _SPEAKER_ID_PATTERN.match(str(speaker)):
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

                # Estimate time split using word-level timing when available
                split_ms = estimate_split_ms(
                    start_ms=line.start_ms,
                    end_ms=line.end_ms,
                    source_text=line.source_text,
                    split_char_pos=split_pos,
                    words_data=words_data,
                )

                line_a = TranscriptLine(
                    index=line.index,
                    start_ms=line.start_ms,
                    end_ms=split_ms,
                    speaker_id=line.speaker_id,
                    speaker_label=line.speaker_label,
                    source_text=line.source_text[:split_pos].strip(),
                )
                # speaker_after: if provided and valid, the second half
                # gets a different speaker (Pass 1 speaker-switch split)
                split_speaker = line.speaker_id
                split_label = line.speaker_label
                speaker_after = c.get("speaker_after", "")
                if speaker_after and _SPEAKER_ID_PATTERN.match(speaker_after):
                    split_speaker = speaker_after
                    split_label = speaker_after.replace("speaker_", "").upper()

                line_b = TranscriptLine(
                    index=line.index + 1000,  # Temporary, will re-index
                    start_ms=split_ms,
                    end_ms=line.end_ms,
                    speaker_id=split_speaker,
                    speaker_label=split_label,
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
    from services.assemblyai.transcriber import TranscriptLine

    if not lines or not speakers:
        return list(lines), 0

    actual_speakers = {
        str(line.speaker_id).strip()
        for line in lines
        if str(line.speaker_id).strip()
    }
    if len(actual_speakers) != 2:
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
    from services.assemblyai.transcriber import TranscriptLine

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


def estimate_split_ms(
    *,
    start_ms: int,
    end_ms: int,
    source_text: str,
    split_char_pos: int,
    words_data: list[dict] | None = None,
) -> int:
    """Estimate the audio timestamp for a text split position.

    Uses word-level timing data when available for precise split points.
    Falls back to text-ratio estimation when words_data is unavailable.

    Args:
        start_ms: segment start time in ms
        end_ms: segment end time in ms
        source_text: full source text of the segment
        split_char_pos: character position in source_text where the split occurs
        words_data: ASR word-level timing (each entry has 'start', 'end', 'text')

    Returns:
        Estimated split timestamp in ms, clamped to [start_ms+1, end_ms-1].
    """
    duration_ms = end_ms - start_ms

    # Text-ratio estimation (always computed as baseline / fallback)
    text_len = max(len(source_text), 1)
    ratio = split_char_pos / text_len
    ratio_ms = start_ms + int(duration_ms * ratio)

    if words_data and split_char_pos > 0:
        # Filter words within this segment's time range
        seg_words = [
            w for w in words_data
            if w.get("start", 0) >= start_ms and w.get("end", 0) <= end_ms
            and w.get("text", "").strip()
        ]

        if len(seg_words) >= 2:
            # Walk through words and accumulate character positions to find
            # the word boundary closest to split_char_pos
            char_cursor = 0
            best_word_idx = 0
            best_char_diff = abs(split_char_pos)

            for i, w in enumerate(seg_words):
                word_text = w.get("text", "").strip()
                # Try to find this word's approximate position in source_text
                pos = source_text.lower().find(word_text.lower(), max(0, char_cursor - 5))
                if pos >= 0:
                    char_cursor = pos + len(word_text)
                else:
                    char_cursor += len(word_text) + 1

                diff = abs(char_cursor - split_char_pos)
                if diff < best_char_diff:
                    best_char_diff = diff
                    best_word_idx = i

            # Split between best_word_idx and best_word_idx+1
            if best_word_idx < len(seg_words) - 1:
                word_end = seg_words[best_word_idx].get("end", 0)
                next_start = seg_words[best_word_idx + 1].get("start", 0)
                word_ms = word_end + (next_start - word_end) // 2
            else:
                word_ms = seg_words[best_word_idx].get("end", 0)

            # Sanity check: if word-level result deviates >30% of segment
            # duration from text-ratio estimate, the word matching may be
            # unreliable — fall back to text-ratio
            max_deviation_ms = int(duration_ms * 0.3)
            if abs(word_ms - ratio_ms) <= max_deviation_ms:
                return max(start_ms + 1, min(word_ms, end_ms - 1))
            else:
                logger.warning(
                    "[Split] Word-level split %dms deviates %dms from text-ratio %dms "
                    "(max %dms), falling back to text-ratio",
                    word_ms, abs(word_ms - ratio_ms), ratio_ms, max_deviation_ms,
                )

    # Fallback: text-ratio estimation
    return max(start_ms + 1, min(ratio_ms, end_ms - 1))


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
