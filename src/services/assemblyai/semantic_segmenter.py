"""Use LLM to split a flat transcript into semantic paragraphs.

Input:  list of sentences with timestamps (from word-level splitting)
Output: list of TranscriptLine, each a coherent paragraph
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_SEGMENTER_MODEL = "gemini-2.5-flash-lite"
_PAUSE_THRESHOLD_MS = 2_000

SEGMENT_PROMPT_TEMPLATE = """\
将以下带时间戳的转录文本按语义段落和长停顿（2秒以上）重新组合。
每段应是一个完整的话题或论点，大约15-60秒。
输出格式与输入相同，段落之间用空行分隔。保留原始时间戳，不要修改文本内容。

转录文本：
{transcript_body}"""


@dataclass
class TimestampedSentence:
    start_ms: int
    end_ms: int
    speaker_id: str
    speaker_label: str
    text: str


def build_segmenter_input(sentences: list[TimestampedSentence]) -> str:
    """Build the LLM input: timestamped sentences with pause markers."""
    parts: list[str] = []
    for i, sent in enumerate(sentences):
        # Insert pause marker if gap > threshold
        if i > 0:
            gap_ms = sent.start_ms - sentences[i - 1].end_ms
            if gap_ms >= _PAUSE_THRESHOLD_MS:
                parts.append(f"[PAUSE {gap_ms / 1000:.1f}s]")

        start_s = sent.start_ms / 1000
        end_s = sent.end_ms / 1000
        parts.append(f"({start_s:.2f}-{end_s:.2f}) {sent.text}")

    return "\n".join(parts)


def parse_segmenter_output(
    output: str,
    *,
    default_speaker_id: str = "speaker_a",
    default_speaker_label: str = "A",
) -> list[dict]:
    """Parse LLM output into transcript lines.

    Each paragraph (separated by blank lines) becomes one line.
    Timestamps are extracted from the first and last entry in each paragraph.
    """
    # Split by blank lines into paragraphs
    paragraphs = re.split(r"\n\s*\n", output.strip())

    timestamp_pattern = re.compile(r"\((\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)\)\s*(.*)")
    lines: list[dict] = []

    for para in paragraphs:
        para = para.strip()
        if not para or para.startswith("[PAUSE"):
            continue

        # Extract all timestamped lines in this paragraph
        entries = []
        for raw_line in para.split("\n"):
            raw_line = raw_line.strip()
            if not raw_line or raw_line.startswith("[PAUSE"):
                continue
            match = timestamp_pattern.match(raw_line)
            if match:
                start_s = float(match.group(1))
                end_s = float(match.group(2))
                text = match.group(3).strip()
                if text:
                    entries.append((int(start_s * 1000), int(end_s * 1000), text))

        if not entries:
            continue

        # Merge entries into one paragraph line
        para_start_ms = entries[0][0]
        para_end_ms = entries[-1][1]
        para_text = " ".join(e[2] for e in entries)

        lines.append({
            "index": len(lines) + 1,
            "start_ms": para_start_ms,
            "end_ms": para_end_ms,
            "speaker_id": default_speaker_id,
            "speaker_label": default_speaker_label,
            "source_text": para_text,
        })

    return lines


def segment_with_llm(
    sentences: list[TimestampedSentence],
    *,
    speaker_id: str = "speaker_a",
    speaker_label: str = "A",
) -> list[dict] | None:
    """Call Gemini to segment transcript. Returns None on failure (caller should fallback)."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        logger.warning("GEMINI_API_KEY not set, skipping LLM segmentation")
        return None

    transcript_body = build_segmenter_input(sentences)
    prompt = SEGMENT_PROMPT_TEMPLATE.format(transcript_body=transcript_body)

    try:
        from services.gemini.client_factory import create_gemini_client
        client = create_gemini_client(api_key=api_key)
        types = _load_genai_types()

        response = client.models.generate_content(
            model=_SEGMENTER_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=65536,
            ),
        )
        output_text = _extract_text(response)
        if not output_text:
            logger.warning("LLM segmenter returned empty response")
            return None

        result = parse_segmenter_output(
            output_text,
            default_speaker_id=speaker_id,
            default_speaker_label=speaker_label,
        )
        if len(result) < 2:
            logger.warning("LLM segmenter returned %d segments, likely failed", len(result))
            return None

        logger.info("LLM segmenter: %d sentences → %d paragraphs", len(sentences), len(result))
        return result

    except Exception:
        logger.exception("LLM segmentation failed, will fallback")
        return None


def _extract_text(response: object) -> str | None:
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
