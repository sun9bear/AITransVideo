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
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REVIEWER_MODEL = "gemini-2.5-flash-lite"
_MAX_LINES_PER_BATCH = 200
_BATCH_OVERLAP = 20
_MAX_MERGE_DURATION_MS = 180_000
_MAX_EDIT_DISTANCE_RATIO = 0.3
_MIN_SPLIT_DURATION_MS = 15_000
_MAX_PAUSE_FOR_MERGE_MS = 2_000

_MIMO_OMNI_MODEL = "mimo-v2-omni"
_MIMO_OMNI_API_URL = "https://api.xiaomimimo.com/v1/chat/completions"


def _get_review_model() -> str:
    """Get review model from admin settings or env.

    Returns 'gemini' or 'mimo_omni'.
    """
    model = os.environ.get("REVIEW_MODEL", "gemini")
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
    speakers: dict[str, dict[str, str]]  # {"speaker_a": {"name": "...", "role": "...", "style": "...", "voice_description": "..."}}
    glossary: dict[str, str]             # {"English term": "中文翻译"}
    corrections_applied: int
    lines: list[Any]                     # Updated TranscriptLine objects


REVIEW_PROMPT_TEMPLATE = """\
你是转录审校专家。听音频、对照转录稿，输出修改指令 JSON。

视频标题：{video_title}
视频链接：{video_url}

## 审校任务（一次性完成）

1. **识别说话人身份**：根据音频声音特征、视频标题、对话内容推断每个 speaker 的真实姓名和角色。
2. **纠正说话人标注**：听音频分辨说话人，修正标注错误。常见错误：
   - 短促回应（Yeah, Sure）被分给了错误的人
   - A-B-A 快速交叉（中间 B 实际是 A 的延续）
   - 旁白/介绍被标成了被采访者
   - **插话/抢话**：主持人在受访者说话中途插入提问，ASR 容易把插话段标成受访者。听音频中的声音重叠和音色变化来判断
   - **被打断后继续**：受访者被打断后继续之前的话，ASR 可能标成新的说话人
3. **修正转录文本**：
   - 去除重复内容（同一句出现在相邻段落）
   - 修正 ASR 错误（对照音频）
   - 不改变原文意思
4. **合并误拆段落**：仅当相邻段落因说话人标注错误被误拆时才合并（A-B-A 模式且中间段极短）。不要合并因自然停顿分开的段落。
5. **拆分超长段落**：超过 60 秒的段落，在语义断点处拆分为 15-45 秒。
6. **生成术语表**：提取人名、专有术语的中文翻译。
7. **分析说话风格**：每个说话人的语气、口头禅特点（给翻译参考）。
8. **描述音色特征**：听音频，为每个说话人输出一段自然语言音色描述（用于 TTS 语音合成），包括性别、年龄段、音调高低、语速快慢、声音质感（如低沉/清亮/沙哑）、情感特点等。

只输出有问题的行。没问题的不用管。

## 输出 JSON 格式（严格遵循，不要添加其他字段）

{{
  "speakers": {{
    "speaker_a": {{"name": "姓名", "role": "角色", "style": "语气描述", "voice_description": "中年女性，声音清晰专业，语速适中，略带温和的采访语气"}},
    "speaker_b": {{"name": "姓名", "role": "角色", "style": "语气描述", "voice_description": "年迈男性，声音低沉沙哑，语速缓慢，带有智慧感和幽默感"}}
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
}}

## 转录稿（{line_count} 行）

{transcript_body}"""


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

    # Build transcript body
    transcript_body = _build_transcript_body(lines)

    # Batch if needed
    if len(lines) <= _MAX_LINES_PER_BATCH:
        result = _call_review(
            api_key=api_key,
            transcript_body=transcript_body,
            line_count=len(lines),
            audio_path=audio_path,
            video_title=video_title,
            video_url=video_url,
            review_model=review_model,
        )
        if result is None:
            return None
        speakers, glossary, corrections = result
    else:
        # Batch processing
        speakers, glossary, corrections = _batched_review(
            api_key=api_key,
            lines=lines,
            audio_path=audio_path,
            video_title=video_title,
            video_url=video_url,
            review_model=review_model,
        )

    # Apply corrections with validation
    updated_lines, applied_count = _apply_corrections(
        lines, corrections, words_data=words_data
    )

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
    video_title: str,
    video_url: str,
    review_model: str = "gemini",
) -> tuple[dict, dict, list] | None:
    """Single LLM call for review. Dispatches to Gemini or MiMo Omni."""
    prompt = REVIEW_PROMPT_TEMPLATE.format(
        video_title=video_title or "(unknown)",
        video_url=video_url or "(unknown)",
        line_count=line_count,
        transcript_body=transcript_body,
    )

    if review_model == "mimo_omni":
        return _call_review_mimo_omni(api_key=api_key, prompt=prompt)

    try:
        genai = _load_genai()
        types = _load_genai_types()
        client = genai.Client(api_key=api_key)

        contents: list = []

        # Upload audio for multimodal review
        if audio_path and Path(audio_path).exists():
            audio_path = Path(audio_path)
            file_size_mb = audio_path.stat().st_size / (1024 * 1024)
            if file_size_mb <= 200:  # Gemini file upload limit
                try:
                    logger.info("Uploading audio for multimodal review (%dMB)...", int(file_size_mb))
                    audio_file = client.files.upload(file=audio_path)
                    contents.append(audio_file)
                    logger.info("Audio uploaded successfully")
                except Exception as e:
                    logger.warning("Audio upload failed, falling back to text-only: %s", e)
            else:
                logger.warning("Audio too large (%dMB), using text-only review", int(file_size_mb))

        contents.append(prompt)

        response = client.models.generate_content(
            model=_REVIEWER_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
                max_output_tokens=8192,
            ),
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
            "Review response: %d speakers, %d glossary terms, %d corrections",
            len(speakers), len(glossary), len(corrections),
        )
        return speakers, glossary, corrections

    except Exception:
        logger.exception("Unified review LLM call failed")
        return None


def _call_review_mimo_omni(
    *,
    api_key: str,
    prompt: str,
) -> tuple[dict, dict, list] | None:
    """Call MiMo-V2-Omni API for text-only review (OpenAI-compatible endpoint)."""
    payload = {
        "model": _MIMO_OMNI_MODEL,
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
    audio_path: str | Path | None,
    video_title: str,
    video_url: str,
    review_model: str = "gemini",
) -> tuple[dict, dict, list]:
    """Process large transcripts in batches with overlap."""
    all_corrections: list = []
    speakers: dict = {}
    glossary: dict = {}

    batch_size = _MAX_LINES_PER_BATCH
    overlap = _BATCH_OVERLAP

    batch_num = 0
    offset = 0
    while offset < len(lines):
        end = min(offset + batch_size, len(lines))
        batch = lines[offset:end]
        batch_num += 1

        logger.info("Review batch %d: lines %d-%d", batch_num, offset + 1, end)

        # For audio, calculate time range of this batch
        batch_audio_path = audio_path  # Pass full audio; Gemini handles seeking

        transcript_body = _build_transcript_body(batch)
        result = _call_review(
            api_key=api_key,
            transcript_body=transcript_body,
            line_count=len(batch),
            audio_path=batch_audio_path if batch_num == 1 else None,  # Only first batch gets audio
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
