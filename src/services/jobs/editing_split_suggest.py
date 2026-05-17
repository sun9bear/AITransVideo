"""Phase 2b v2 — LLM-backed split suggestion for editing-mode segments.

Replaces the Phase 2b v1 heuristic (ASR speaker_label scan) with a
multimodal LLM call that listens to the segment's audio + reads the
text and decides:

  - Does this segment internally contain multiple speakers?
  - If yes, where should it be split?

Uses the SAME multimodal Gemini call as S2 Pass 1 speaker review
(`transcript_reviewer._review_pass1_speakers`) but scoped to a single
segment so the LLM can focus.

Paid API constraint (CLAUDE.md):
  ✅ User-explicit trigger only (frontend button click)
  ✅ Per-segment cap = 1
  ✅ Per-job cap = MAX(MIN(0.2 × N, anomaly_count), 5)
  ❌ Never auto-fallback; never batch-call; never invoke from a
     defensive `except` branch
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services._file_lock import file_lock
from services.jobs.editing import EditingConflictError
from services.jobs.editing_segments import (
    _editing_dir,
    _editing_lock_anchor,
    _read_segments_json_raw,
    _SOURCE_AUDIO_CANDIDATES,
)
from services.jobs.input_validators import validate_segment_id

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt — single-segment audio + text analysis
# ---------------------------------------------------------------------------

_SPLIT_SUGGEST_PROMPT = """\
你是音频内容审听专家。请仔细听这段音频，判断段落内是否包含**多个不同说话人**。

判断原则：
- **严格基于音频判断**（音色、说话风格、停顿模式），不要仅根据文本推测
- **conservative**：当不确定时，倾向于「不需要拆分」
- 简短的应答词（"yeah" / "right" / "uh" / "嗯"）即使是不同人说的，
  如果不影响整段连贯性，**不算独立说话人**
- 只在段内有清晰、明显的说话人切换时才建议拆分

段落信息：
- 段时长：{duration_seconds:.1f}s
- 当前标注的说话人：{speaker_name}
- 视频标题：{video_title}

英文原文：
{source_text}

中文译文：
{cn_text}

请仔细听音频并分析。输出 JSON，且只能输出 JSON：

如果整段都是同一个人说的：
{{
  "needs_split": false,
  "reason": "听感判断说明（30 字内）"
}}

如果段内有清晰说话人切换：
{{
  "needs_split": true,
  "reason": "判断依据（30 字内）",
  "cuts": [
    {{
      "at_text": "切换点前最后说的英文原文片段（5-10 个单词，必须能在原文中精确匹配）",
      "speaker_before": "前段说话人的中文名或描述",
      "speaker_after": "后段说话人的中文名或描述"
    }}
  ]
}}
"""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SplitSuggestError(Exception):
    """Base exception for suggest-split flow."""


class SplitSuggestSegmentUsedError(SplitSuggestError):
    """The segment has already been analyzed once this editing session."""


class SplitSuggestCapExhaustedError(SplitSuggestError):
    """The job-level call cap has been reached."""

    def __init__(self, cap: int, used: int):
        super().__init__(f"task cap exhausted: used {used} of {cap}")
        self.cap = cap
        self.used = used


class SplitSuggestNoAudioError(SplitSuggestError):
    """No source audio file available for the segment."""


# ---------------------------------------------------------------------------
# Rate limit storage
# ---------------------------------------------------------------------------

_USAGE_FILE_NAME = "suggest_split_usage.json"


@dataclass
class _UsageState:
    segment_ids_used: list[str]
    total_calls: int
    cap: int
    computed_at: str | None


def _usage_path(project_dir: str | Path) -> Path:
    return _editing_dir(project_dir) / _USAGE_FILE_NAME


def _read_usage(project_dir: str | Path) -> _UsageState | None:
    path = _usage_path(project_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return _UsageState(
        segment_ids_used=list(data.get("segment_ids_used") or []),
        total_calls=int(data.get("total_calls") or 0),
        cap=int(data.get("cap") or 0),
        computed_at=data.get("computed_at"),
    )


def _write_usage(project_dir: str | Path, state: _UsageState) -> None:
    path = _usage_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "segment_ids_used": list(state.segment_ids_used),
        "total_calls": int(state.total_calls),
        "cap": int(state.cap),
        "computed_at": state.computed_at,
    }
    # Atomic write via tmp + os.replace
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _compute_initial_cap(segments: list[dict[str, Any]]) -> int:
    """cap = MAX(MIN(0.2 × N, anomaly_count), 5)

    "anomaly_count" = segments with force_dsp alignment (the strongest
    signal we have that something might have been mis-merged). Floor 5
    ensures jobs with clean alignment still allow a few investigative
    calls.
    """
    total = len(segments)
    anomaly = sum(
        1 for s in segments
        if isinstance(s, dict) and s.get("alignment_method") == "force_dsp"
    )
    ratio_cap = int(round(total * 0.2))
    return max(min(ratio_cap, anomaly), 5)


# ---------------------------------------------------------------------------
# Audio source lookup
# ---------------------------------------------------------------------------


def _find_source_audio(project_dir: str | Path) -> Path | None:
    """Pick the best available source audio for review. Mirrors the
    candidate list in editing_segments (speech_for_asr.wav preferred
    over original.wav)."""
    base = Path(project_dir)
    for rel in _SOURCE_AUDIO_CANDIDATES:
        candidate = base / rel
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# at_text → source_index matching
# ---------------------------------------------------------------------------


def _find_source_index_for_at_text(source_text: str, at_text: str) -> int | None:
    """Locate the END position of `at_text` in `source_text`. Tries:
    1. Case-insensitive substring match (most LLM cases)
    2. Whitespace-collapsed fuzzy match (handles minor LLM punctuation jitter)
    Returns char index (0..len(source_text)) after the matched span, or
    None when no reasonable match.
    """
    if not at_text or not source_text:
        return None
    needle = at_text.strip()
    if not needle:
        return None

    haystack_lower = source_text.lower()
    needle_lower = needle.lower()

    # Exact (case-insensitive) substring
    pos = haystack_lower.find(needle_lower)
    if pos >= 0:
        return pos + len(needle)

    # Whitespace-normalized fuzzy match
    def normalize(s: str) -> str:
        return re.sub(r"\s+", " ", s).strip()

    norm_hay = normalize(source_text).lower()
    norm_needle = normalize(needle).lower()
    if norm_needle and norm_needle in norm_hay:
        # Re-anchor in original text by walking it with the normalized one
        i_orig = 0
        i_norm = 0
        target_norm_end = norm_hay.find(norm_needle) + len(norm_needle)
        while i_norm < target_norm_end and i_orig < len(source_text):
            ch = source_text[i_orig]
            if ch.isspace():
                # Compress to single space in normalized — advance source
                if i_norm < len(norm_hay) and norm_hay[i_norm] == " ":
                    # Skip the rest of the run in source
                    while i_orig < len(source_text) and source_text[i_orig].isspace():
                        i_orig += 1
                    i_norm += 1
                    continue
            i_orig += 1
            i_norm += 1
        return min(i_orig, len(source_text))

    return None


# ---------------------------------------------------------------------------
# speaker name → speaker_id reverse lookup
# ---------------------------------------------------------------------------


def _reverse_lookup_speaker_id(
    name: str,
    speaker_name_map: dict[str, str],
    current_speaker_id: str | None,
    available_speaker_ids: list[str],
) -> str:
    """LLM returns Chinese name → find existing sid by name. Fallback:
    first available sid that's NOT the current segment's speaker (so
    we don't accidentally re-assign the same speaker to both halves)."""
    if not name:
        return _first_other(current_speaker_id, available_speaker_ids)
    name_clean = name.strip()
    for sid, sname in speaker_name_map.items():
        if str(sname).strip() == name_clean:
            return sid
    # Case-insensitive fallback
    name_lower = name_clean.lower()
    for sid, sname in speaker_name_map.items():
        if str(sname).strip().lower() == name_lower:
            return sid
    return _first_other(current_speaker_id, available_speaker_ids)


def _first_other(
    current: str | None,
    available: list[str],
) -> str:
    for sid in available:
        if sid != current:
            return sid
    return current or (available[0] if available else "")


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------


def _build_prompt(
    *,
    duration_seconds: float,
    speaker_name: str,
    video_title: str,
    source_text: str,
    cn_text: str,
) -> str:
    return _SPLIT_SUGGEST_PROMPT.format(
        duration_seconds=duration_seconds,
        speaker_name=speaker_name or "(未知)",
        video_title=video_title or "(unknown)",
        source_text=source_text or "(空)",
        cn_text=cn_text or "(空)",
    )


def _call_llm(
    *,
    audio_clip_path: Path,
    prompt: str,
    review_model: str,
) -> dict[str, Any]:
    """Call Gemini multimodal with audio + prompt. Returns parsed JSON.

    Mirrors the Pass1 pattern (transcript_reviewer._review_pass1_speakers)
    in the most basic form — single attempt, no fallback chain (caller
    can retry if needed). Returns raw parsed dict; caller validates.
    """
    from services.transcript_reviewer import _create_review_client, _load_genai_types
    from services.llm_registry import (
        resolve_model_id as _resolve_model_id_from_registry,
        get_api_key as _get_model_api_key,
    )

    gemini_types = _load_genai_types()
    client = _create_review_client(api_key=_get_model_api_key(review_model))
    model_id = _resolve_model_id_from_registry(review_model)

    _MIME_MAP = {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
    }
    mime = _MIME_MAP.get(audio_clip_path.suffix.lower(), "audio/ogg")
    audio_bytes = audio_clip_path.read_bytes()
    audio_part = gemini_types.Part.from_bytes(data=audio_bytes, mime_type=mime)

    response = client.models.generate_content(
        model=model_id,
        contents=[audio_part, prompt],
        config=gemini_types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,
            max_output_tokens=8192,
        ),
    )
    response_text = getattr(response, "text", None) or ""
    if not response_text.strip():
        raise SplitSuggestError("LLM returned empty response")
    try:
        return json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise SplitSuggestError(f"LLM returned non-JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# Public kernel
# ---------------------------------------------------------------------------


def suggest_split_for_segment(
    project_dir: str | Path,
    segment_id: str,
    *,
    speaker_name_map: dict[str, str],
    available_speaker_ids: list[str],
    video_title: str = "",
    review_model: str | None = None,
) -> dict[str, Any]:
    """LLM-backed suggest-split for a single editing segment.

    Args:
        project_dir: project root.
        segment_id: which segment to analyze.
        speaker_name_map: sid → display_name (for reverse lookup of LLM
            speaker tags).
        available_speaker_ids: currently-known speakers in this editing
            session (used as fallback target).
        video_title: optional video title (helps LLM context).
        review_model: admin-configured Pass1 model. If None, falls back
            to llm_registry's default.

    Returns:
        {
            "segment_id": str,
            "needs_split": bool,
            "reason": str,
            "cuts": [{source_index, cn_index, speaker_id, at_text}, ...],
            "usage": {"used": int, "cap": int, "remaining": int},
        }

    Raises:
        EditingConflictError: segment_id not found.
        SplitSuggestSegmentUsedError: per-segment cap reached.
        SplitSuggestCapExhaustedError: per-job cap reached.
        SplitSuggestNoAudioError: no source audio available.
        SplitSuggestError: LLM call / parse failure.
    """
    validate_segment_id(segment_id)

    if review_model is None:
        from services.llm_registry import get_prompt_model
        review_model = get_prompt_model("studio", "pass1")

    with file_lock(_editing_lock_anchor(project_dir)):
        segments = _read_segments_json_raw(project_dir)

        # ── Rate limit check (inside lock to serialize concurrent calls) ──
        usage = _read_usage(project_dir)
        if usage is None:
            cap = _compute_initial_cap(segments)
            usage = _UsageState(
                segment_ids_used=[],
                total_calls=0,
                cap=cap,
                computed_at=None,
            )

        if segment_id in usage.segment_ids_used:
            raise SplitSuggestSegmentUsedError(
                f"segment {segment_id} already analyzed this session"
            )
        if usage.total_calls >= usage.cap:
            raise SplitSuggestCapExhaustedError(cap=usage.cap, used=usage.total_calls)

        # ── Locate segment ──
        seg: dict[str, Any] | None = None
        for s in segments:
            if isinstance(s, dict) and str(s.get("segment_id")) == segment_id:
                seg = s
                break
        if seg is None:
            raise EditingConflictError(
                f"segment_id {segment_id!r} not found in editing/segments.json"
            )

        source_text = str(seg.get("source_text") or "")
        cn_text = str(seg.get("cn_text") or "")
        start_ms = int(seg.get("start_ms", 0) or 0)
        end_ms = int(seg.get("end_ms", start_ms) or start_ms)
        current_speaker_id = str(seg.get("speaker_id") or "") or None
        speaker_name = speaker_name_map.get(current_speaker_id or "", current_speaker_id or "")

        # ── Audio prep ──
        audio_path = _find_source_audio(project_dir)
        if audio_path is None:
            raise SplitSuggestNoAudioError(
                "no source audio available (audio/speech_for_asr.wav or audio/original.wav)"
            )

        from services.transcript_reviewer import _prepare_review_audio_clip
        from datetime import datetime, timezone

        with tempfile.TemporaryDirectory(prefix="avt_suggest_split_") as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            try:
                clip_path = _prepare_review_audio_clip(
                    audio_path,
                    tmp_dir,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    clip_index=0,
                )
            except Exception as exc:  # noqa: BLE001 — ffmpeg subprocess failure
                raise SplitSuggestError(f"audio clip extraction failed: {exc}") from exc

            duration_s = max(0.0, (end_ms - start_ms) / 1000.0)
            prompt = _build_prompt(
                duration_seconds=duration_s,
                speaker_name=speaker_name,
                video_title=video_title,
                source_text=source_text,
                cn_text=cn_text,
            )

            llm_response = _call_llm(
                audio_clip_path=clip_path,
                prompt=prompt,
                review_model=review_model,
            )

        # ── Parse LLM response + map to frontend format ──
        needs_split = bool(llm_response.get("needs_split"))
        reason = str(llm_response.get("reason") or "")
        cuts_out: list[dict[str, Any]] = []

        if needs_split:
            raw_cuts = llm_response.get("cuts") or []
            if not isinstance(raw_cuts, list):
                raise SplitSuggestError("LLM cuts field is not a list")
            seen_source_indices: set[int] = set()
            for raw in raw_cuts:
                if not isinstance(raw, dict):
                    continue
                at_text = str(raw.get("at_text") or "")
                idx = _find_source_index_for_at_text(source_text, at_text)
                if idx is None:
                    logger.warning(
                        "suggest_split: at_text %r not found in source for %s",
                        at_text[:60], segment_id,
                    )
                    continue
                if not (0 < idx < len(source_text)):
                    continue
                if idx in seen_source_indices:
                    continue
                seen_source_indices.add(idx)
                cn_ratio = idx / max(1, len(source_text))
                cn_index = max(
                    1,
                    min(round(cn_ratio * len(cn_text)), max(1, len(cn_text) - 1)),
                )
                spk_after = str(raw.get("speaker_after") or "")
                spk_id = _reverse_lookup_speaker_id(
                    spk_after,
                    speaker_name_map,
                    current_speaker_id,
                    available_speaker_ids,
                )
                cuts_out.append({
                    "source_index": idx,
                    "cn_index": cn_index,
                    "speaker_id": spk_id,
                    "at_text": at_text,
                })

            # If parse dropped all cuts (e.g. LLM hallucinated at_texts),
            # downgrade to needs_split=false so frontend doesn't show
            # "needs split but 0 cuts".
            if not cuts_out:
                needs_split = False
                reason = "LLM 给出切分建议但未能在原文中精确定位（已忽略）"

        # ── Cuts sorted by source_index ──
        cuts_out.sort(key=lambda c: c["source_index"])

        # ── Update usage counter + persist ──
        usage.segment_ids_used.append(segment_id)
        usage.total_calls += 1
        if usage.computed_at is None:
            usage.computed_at = datetime.now(timezone.utc).isoformat()
        _write_usage(project_dir, usage)

        return {
            "segment_id": segment_id,
            "needs_split": needs_split,
            "reason": reason,
            "cuts": cuts_out,
            "usage": {
                "used": usage.total_calls,
                "cap": usage.cap,
                "remaining": max(0, usage.cap - usage.total_calls),
            },
        }


def get_suggest_split_quota(project_dir: str | Path) -> dict[str, Any]:
    """Read-only: how many suggest-split calls remain. Used by the
    frontend to disable the button when cap reached + show counter."""
    with file_lock(_editing_lock_anchor(project_dir)):
        segments = _read_segments_json_raw(project_dir)
        usage = _read_usage(project_dir)
        if usage is None:
            cap = _compute_initial_cap(segments)
            return {
                "used": 0,
                "cap": cap,
                "remaining": cap,
                "segment_ids_used": [],
            }
        return {
            "used": usage.total_calls,
            "cap": usage.cap,
            "remaining": max(0, usage.cap - usage.total_calls),
            "segment_ids_used": list(usage.segment_ids_used),
        }
