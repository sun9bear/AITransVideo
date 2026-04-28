#!/usr/bin/env python3
"""Build real-prompt translation evaluation samples from job artifacts.

This script is intentionally offline: it does not call any LLM provider.  It
reconstructs the same prompt inputs used by the production S3 translation path
and, when real timing data exists, the S5 rewrite prompt path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from services.assemblyai.transcriber import TranscriptLine
from services.gemini.rewriter import GeminiRewriter
from services.gemini.translator import (
    DEFAULT_ESTIMATED_TTS_CHARS_PER_SECOND,
    DEFAULT_MAX_SEGMENT_DURATION_MS,
    DEFAULT_REWRITE_PROMPT_TEMPLATE,
    DEFAULT_TRANSLATION_PROMPT_TEMPLATE,
    PROBE_TRANSLATION_PROMPT_TEMPLATE,
    GeminiTranslator,
    _build_groups,
    validate_rewrite_prompt_template,
    validate_translation_prompt_template,
)

DATASET_VERSION = "translation_model_eval_samples.v1"
DEFAULT_DATA_ROOT = Path(".codex_tmp/us_fetch/extracted/opt/aivideotrans/data")
DEFAULT_OUTPUT = Path("reports/benchmark/translation_eval_samples.json")

CATEGORY_ORDER = (
    "glossary_hit",
    "numeric_financial",
    "high_density",
    "low_density_oral",
    "multi_speaker",
    "rewrite_risk",
    "short_backchannel",
    "long_segment",
    "technical_terms",
    "generic_context",
)

ORAL_MARKERS = (
    "uh",
    "um",
    "you know",
    "i mean",
    "kind of",
    "sort of",
    "well",
    "i would say",
    "you see",
    "like",
)
BACKCHANNELS = {
    "yeah",
    "yes",
    "right",
    "okay",
    "ok",
    "sure",
    "absolutely",
    "exactly",
    "mm-hmm",
}
NUMERIC_RE = re.compile(
    r"[$€£¥]|\b\d+(?:\.\d+)?\s?(?:%|percent|million|billion|trillion|"
    r"thousand|people|employees|years?|months?|days?)\b|\bq[1-4]\b|\bs&p\b",
    re.IGNORECASE,
)
TECHNICAL_RE = re.compile(
    r"\b(?:AI|API|GPU|CPU|LLM|TTS|ASR|CEO|CFO|IPO|SaaS|model|platform|"
    r"algorithm|software|cloud|database|token|pipeline)\b"
)
WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?|\d+(?:\.\d+)?")
WHITESPACE_RE = re.compile(r"\s+")
NON_SPOKEN_CHAR_RE = re.compile(r"[^\u4e00-\u9fff\u3400-\u4dbfa-zA-Z0-9]")


@dataclass(frozen=True)
class PromptBundle:
    translate_template: str
    translate_source: str
    rewrite_template: str
    rewrite_source: str
    probe_translate_template: str
    probe_translate_source: str


@dataclass(frozen=True)
class JobArtifact:
    job_id: str
    user_root: str
    job_hash: str
    job_dir: Path
    segments_path: Path
    segments: list[dict[str, Any]]
    groups: list[dict[str, object]]
    glossary: dict[str, str]
    video_title: str
    source_url: str
    service_mode: str
    tts_provider: str
    tts_model: str
    global_chars_per_second: float
    chars_per_second_by_speaker: dict[str, float]


@dataclass(frozen=True)
class CandidateWindow:
    job: JobArtifact
    start: int
    end: int
    reasons: tuple[str, ...]
    score: float
    metrics: dict[str, Any]
    glossary_hits: tuple[str, ...]
    text_hash: str


def stable_hash(value: str, *, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_text(value: Any) -> str:
    return WHITESPACE_RE.sub(" ", str(value or "")).strip()


def coerce_int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def coerce_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return number


def spoken_char_count(text: str) -> int:
    return len(NON_SPOKEN_CHAR_RE.sub("", text or ""))


def _load_dict(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = read_json(path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _string_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, val in value.items():
        normalized_key = normalize_text(key)
        normalized_val = normalize_text(val)
        if normalized_key and normalized_val:
            result[normalized_key] = normalized_val
    return result


def load_prompt_bundle(
    *,
    admin_settings_path: Path | None = None,
    autodub_config_path: Path | None = None,
) -> PromptBundle:
    admin_payload = _load_dict(admin_settings_path) if admin_settings_path else {}
    admin_prompts = _string_dict(admin_payload.get("review_prompts"))
    autodub_payload = _load_dict(autodub_config_path) if autodub_config_path else {}
    config_prompts = _string_dict(autodub_payload.get("prompts"))

    translate_source = "runtime default"
    translate_template = DEFAULT_TRANSLATION_PROMPT_TEMPLATE
    config_translate = config_prompts.get("s3_translate")
    admin_translate = admin_prompts.get("translate")
    if config_translate:
        translate_template = validate_translation_prompt_template(config_translate)
        translate_source = f"{autodub_config_path} prompts.s3_translate"
    elif admin_translate:
        translate_template = validate_translation_prompt_template(admin_translate)
        translate_source = f"{admin_settings_path} review_prompts.translate"

    rewrite_source = "runtime default"
    rewrite_template = DEFAULT_REWRITE_PROMPT_TEMPLATE
    config_rewrite = config_prompts.get("s5_rewrite")
    admin_rewrite = admin_prompts.get("rewrite")
    if config_rewrite:
        rewrite_template = validate_rewrite_prompt_template(config_rewrite)
        rewrite_source = f"{autodub_config_path} prompts.s5_rewrite"
    elif admin_rewrite:
        rewrite_template = validate_rewrite_prompt_template(admin_rewrite)
        rewrite_source = f"{admin_settings_path} review_prompts.rewrite"

    probe_source = "runtime default"
    probe_template = PROBE_TRANSLATION_PROMPT_TEMPLATE
    admin_probe = admin_prompts.get("probe_translate")
    if admin_probe:
        probe_template = validate_translation_prompt_template(admin_probe)
        probe_source = f"{admin_settings_path} review_prompts.probe_translate"

    return PromptBundle(
        translate_template=translate_template,
        translate_source=translate_source,
        rewrite_template=rewrite_template,
        rewrite_source=rewrite_source,
        probe_translate_template=probe_template,
        probe_translate_source=probe_source,
    )


def _load_glossary(job_dir: Path) -> dict[str, str]:
    candidates = (
        job_dir / "translation" / "glossary.json",
        job_dir / "transcript" / "s2_pass2_result.json",
        job_dir / "transcript" / "s2_review_result.json",
    )
    for path in candidates:
        payload = _load_dict(path)
        glossary = payload.get("glossary", payload)
        normalized = _string_dict(glossary)
        if normalized:
            return normalized
    return {}


def _load_project_metadata(job_dir: Path) -> tuple[str, str]:
    project_state = _load_dict(job_dir / "project_state.json")
    stages = project_state.get("stages", {})
    if isinstance(stages, dict):
        ingestion = stages.get("ingestion", {})
        payload = ingestion.get("payload", {}) if isinstance(ingestion, dict) else {}
        if isinstance(payload, dict):
            title = normalize_text(payload.get("title"))
            locator = normalize_text(payload.get("locator"))
            if title or locator:
                return title, locator

    download_metadata = _load_dict(job_dir / "download_metadata.json")
    title = normalize_text(download_metadata.get("video_title") or download_metadata.get("title"))
    url = normalize_text(download_metadata.get("url") or download_metadata.get("source_ref"))
    return title, url


def _load_job_metadata(data_root: Path, job_id: str) -> dict[str, Any]:
    return _load_dict(data_root / "jobs" / f"{job_id}.json")


def _load_calibration(job_dir: Path) -> tuple[float, dict[str, float]]:
    payload = _load_dict(job_dir / "audio" / "probe_calibration.json")
    global_cps = coerce_float(
        payload.get("global_chars_per_second"),
        default=DEFAULT_ESTIMATED_TTS_CHARS_PER_SECOND,
    )
    by_speaker_payload = payload.get("chars_per_second_by_speaker", {})
    by_speaker: dict[str, float] = {}
    if isinstance(by_speaker_payload, dict):
        for speaker_id, value in by_speaker_payload.items():
            cps = coerce_float(value)
            if cps > 0:
                by_speaker[str(speaker_id)] = cps
    return global_cps, by_speaker


def _segment_sort_key(segment: dict[str, Any]) -> tuple[int, int]:
    return (
        coerce_int(segment.get("start_ms"), default=0),
        coerce_int(segment.get("segment_id"), default=0),
    )


def _segments_to_lines(segments: list[dict[str, Any]]) -> list[TranscriptLine]:
    lines: list[TranscriptLine] = []
    for index, segment in enumerate(sorted(segments, key=_segment_sort_key), start=1):
        source_text = normalize_text(segment.get("source_text"))
        if not source_text:
            continue
        start_ms = coerce_int(segment.get("start_ms"))
        end_ms = coerce_int(segment.get("end_ms"))
        if end_ms <= start_ms:
            duration_ms = coerce_int(segment.get("target_duration_ms"))
            end_ms = start_ms + max(duration_ms, 1)
        speaker_id = normalize_text(segment.get("speaker_id")) or "speaker_a"
        display_name = normalize_text(segment.get("display_name")) or speaker_id
        lines.append(
            TranscriptLine(
                index=index,
                start_ms=start_ms,
                end_ms=end_ms,
                speaker_id=speaker_id,
                speaker_label=display_name,
                source_text=source_text,
            )
        )
    return lines


def load_job_artifacts(data_root: Path) -> list[JobArtifact]:
    projects_root = data_root / "projects"
    artifacts: list[JobArtifact] = []
    if not projects_root.is_dir():
        return artifacts

    for segments_path in sorted(projects_root.rglob("translation/segments.json")):
        job_dir = segments_path.parent.parent
        job_id = job_dir.name
        user_root = job_dir.parent.name
        payload = _load_dict(segments_path)
        raw_segments = payload.get("segments", [])
        if not isinstance(raw_segments, list):
            continue
        segments = [
            dict(segment)
            for segment in raw_segments
            if isinstance(segment, dict) and normalize_text(segment.get("source_text"))
        ]
        if not segments:
            continue

        title, source_url = _load_project_metadata(job_dir)
        job_meta = _load_job_metadata(data_root, job_id)
        service_mode = normalize_text(job_meta.get("service_mode")) or "unknown"
        tts_provider = normalize_text(job_meta.get("tts_provider")) or normalize_text(job_meta.get("snapshot_tts_provider"))
        tts_model = normalize_text(job_meta.get("tts_model")) or normalize_text(job_meta.get("snapshot_tts_model"))
        global_cps, cps_by_speaker = _load_calibration(job_dir)
        lines = _segments_to_lines(segments)
        if not lines:
            continue
        groups = _build_groups(
            lines,
            max_segment_duration_ms=DEFAULT_MAX_SEGMENT_DURATION_MS,
            chars_per_second=global_cps,
            chars_per_second_by_speaker=cps_by_speaker or None,
        )
        if not groups:
            continue

        artifacts.append(
            JobArtifact(
                job_id=job_id,
                user_root=user_root,
                job_hash=stable_hash(f"{user_root}/{job_id}", length=12),
                job_dir=job_dir,
                segments_path=segments_path,
                segments=segments,
                groups=groups,
                glossary=_load_glossary(job_dir),
                video_title=title or job_id,
                source_url=source_url,
                service_mode=service_mode,
                tts_provider=tts_provider,
                tts_model=tts_model,
                global_chars_per_second=global_cps,
                chars_per_second_by_speaker=cps_by_speaker,
            )
        )
    return artifacts


def _window_text(groups: list[dict[str, object]]) -> str:
    return " ".join(normalize_text(group.get("source_text")) for group in groups)


def _glossary_hits(text: str, glossary: dict[str, str]) -> list[str]:
    lowered = text.lower()
    return [term for term in glossary if term.lower() in lowered]


def _word_count(text: str) -> int:
    return len(WORD_RE.findall(text))


def _oral_marker_count(text: str) -> int:
    lowered = text.lower()
    return sum(lowered.count(marker) for marker in ORAL_MARKERS)


def _is_short_backchannel(text: str, duration_seconds: float) -> bool:
    normalized = re.sub(r"[^a-zA-Z\s-]", "", text).strip().lower()
    words = normalized.split()
    if duration_seconds > 3.0 or len(words) > 5:
        return False
    if normalized in BACKCHANNELS:
        return True
    return any(word in BACKCHANNELS for word in words)


def classify_window(
    *,
    job: JobArtifact,
    start: int,
    end: int,
) -> CandidateWindow:
    groups = job.groups[start:end]
    segments = job.segments[start:end]
    text = _window_text(groups)
    glossary_hits = _glossary_hits(text, job.glossary)
    speakers = {normalize_text(group.get("speaker_id")) for group in groups}
    duration_seconds = sum(coerce_float(group.get("target_duration_seconds")) for group in groups)
    source_words = sum(coerce_int(group.get("source_word_count")) for group in groups)
    max_wps = max((coerce_float(group.get("source_words_per_second")) for group in groups), default=0.0)
    avg_wps = source_words / duration_seconds if duration_seconds > 0 else 0.0

    reasons: list[str] = []
    if glossary_hits:
        reasons.append("glossary_hit")
    if NUMERIC_RE.search(text):
        reasons.append("numeric_financial")
    if avg_wps >= 2.8 or max_wps >= 3.4:
        reasons.append("high_density")
    if _oral_marker_count(text) >= 2 and (avg_wps <= 2.3 or duration_seconds >= 20):
        reasons.append("low_density_oral")
    if len(speakers - {""}) > 1:
        reasons.append("multi_speaker")
    if any(
        coerce_int(segment.get("rewrite_count")) > 0
        or bool(segment.get("needs_review"))
        or normalize_text(segment.get("alignment_method")) == "force_dsp"
        for segment in segments
    ):
        reasons.append("rewrite_risk")
    if any(
        _is_short_backchannel(
            normalize_text(group.get("source_text")),
            coerce_float(group.get("target_duration_seconds")),
        )
        for group in groups
    ):
        reasons.append("short_backchannel")
    if any(
        coerce_float(group.get("target_duration_seconds")) >= 45.0
        or len(normalize_text(group.get("source_text"))) >= 500
        for group in groups
    ):
        reasons.append("long_segment")
    if TECHNICAL_RE.search(text):
        reasons.append("technical_terms")
    if not reasons:
        reasons.append("generic_context")

    score = (
        len(set(reasons)) * 10
        + min(len(glossary_hits), 5) * 3
        + min(source_words / 40, 8)
        + min(duration_seconds / 30, 5)
    )
    if "rewrite_risk" in reasons:
        score += 8
    if "multi_speaker" in reasons:
        score += 5

    metrics = {
        "segment_count": len(groups),
        "duration_seconds": round(duration_seconds, 2),
        "source_word_count": source_words,
        "avg_source_words_per_second": round(avg_wps, 3),
        "max_source_words_per_second": round(max_wps, 3),
        "source_chars": len(text),
        "speaker_count": len(speakers - {""}),
        "glossary_hits": glossary_hits,
    }
    return CandidateWindow(
        job=job,
        start=start,
        end=end,
        reasons=tuple(dict.fromkeys(reasons)),
        score=round(score, 3),
        metrics=metrics,
        glossary_hits=tuple(glossary_hits),
        text_hash=stable_hash(text, length=16),
    )


def build_candidate_windows(
    jobs: list[JobArtifact],
    *,
    window_size: int,
    min_window_segments: int,
) -> list[CandidateWindow]:
    candidates: list[CandidateWindow] = []
    step = max(1, window_size // 2)
    for job in jobs:
        total = min(len(job.groups), len(job.segments))
        if total <= 0:
            continue
        for start in range(0, total, step):
            end = min(total, start + window_size)
            if end - start < min_window_segments and total >= min_window_segments:
                continue
            candidates.append(classify_window(job=job, start=start, end=end))
            if end >= total:
                break
    return candidates


def select_windows(candidates: list[CandidateWindow], *, max_samples: int) -> list[CandidateWindow]:
    ranked = sorted(candidates, key=lambda item: (item.score, len(item.reasons)), reverse=True)
    selected: list[CandidateWindow] = []
    used_text_hashes: set[str] = set()
    per_title_counts: Counter[str] = Counter()
    per_job_counts: Counter[str] = Counter()

    def title_key(candidate: CandidateWindow) -> str:
        title = normalize_text(candidate.job.video_title).lower()
        return stable_hash(title or candidate.job.job_hash, length=12)

    def add(
        candidate: CandidateWindow,
        *,
        title_limit: int | None = None,
        job_limit: int | None = None,
    ) -> bool:
        if len(selected) >= max_samples:
            return False
        if candidate.text_hash in used_text_hashes:
            return False
        current_title_key = title_key(candidate)
        if title_limit is not None and per_title_counts[current_title_key] >= title_limit:
            return False
        if job_limit is not None and per_job_counts[candidate.job.job_hash] >= job_limit:
            return False
        selected.append(candidate)
        used_text_hashes.add(candidate.text_hash)
        per_title_counts[current_title_key] += 1
        per_job_counts[candidate.job.job_hash] += 1
        return True

    best_by_title: dict[str, CandidateWindow] = {}
    for candidate in ranked:
        key = title_key(candidate)
        if key not in best_by_title:
            best_by_title[key] = candidate
    for candidate in sorted(best_by_title.values(), key=lambda item: item.score, reverse=True):
        add(candidate, job_limit=1)
        if len(selected) >= max_samples:
            return selected

    for category in CATEGORY_ORDER:
        for candidate in ranked:
            if category in candidate.reasons and add(candidate, title_limit=2, job_limit=1):
                break
        if len(selected) >= max_samples:
            return selected

    job_soft_limit = max(2, math.ceil(max_samples / max(1, len({c.job.job_hash for c in candidates}))) + 1)
    for candidate in ranked:
        if len(selected) >= max_samples:
            break
        add(candidate, title_limit=2, job_limit=job_soft_limit)

    for candidate in ranked:
        if len(selected) >= max_samples:
            break
        add(candidate, title_limit=4, job_limit=job_soft_limit)

    for candidate in ranked:
        if len(selected) >= max_samples:
            break
        add(candidate, job_limit=job_soft_limit)

    return selected


def _llm_groups(groups: list[dict[str, object]], translator: GeminiTranslator) -> list[dict[str, object]]:
    return [
        {key: value for key, value in group.items() if key in translator._LLM_GROUP_FIELDS}
        for group in groups
    ]


def _reference_translations(candidate: CandidateWindow) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    for group, segment in zip(
        candidate.job.groups[candidate.start:candidate.end],
        candidate.job.segments[candidate.start:candidate.end],
    ):
        references.append(
            {
                "segment_id": group.get("segment_id"),
                "source_text": normalize_text(segment.get("source_text")),
                "cn_text": normalize_text(segment.get("cn_text")),
                "tts_cn_text": normalize_text(segment.get("tts_cn_text")),
                "target_duration_ms": coerce_int(segment.get("target_duration_ms")),
                "actual_duration_ms": coerce_int(segment.get("actual_duration_ms")),
                "alignment_method": normalize_text(segment.get("alignment_method")),
                "rewrite_count": coerce_int(segment.get("rewrite_count")),
                "needs_review": bool(segment.get("needs_review")),
            }
        )
    return references


def _build_rewrite_cases(
    candidate: CandidateWindow,
    *,
    translator: GeminiTranslator,
    prompt_bundle: PromptBundle,
    max_cases: int = 2,
) -> list[dict[str, Any]]:
    rewriter = GeminiRewriter(
        translator,
        chars_per_second=candidate.job.global_chars_per_second,
        chars_per_second_by_speaker=candidate.job.chars_per_second_by_speaker,
        rewrite_prompt_template=prompt_bundle.rewrite_template,
    )
    cases: list[dict[str, Any]] = []
    for group, segment in zip(
        candidate.job.groups[candidate.start:candidate.end],
        candidate.job.segments[candidate.start:candidate.end],
    ):
        if len(cases) >= max_cases:
            break
        target_ms = coerce_int(segment.get("target_duration_ms") or group.get("target_duration_ms"))
        actual_ms = coerce_int(segment.get("actual_duration_ms"))
        if target_ms <= 0 or actual_ms <= 0:
            continue
        error_ratio = abs(actual_ms - target_ms) / target_ms
        if error_ratio < 0.10 and not segment.get("needs_review") and coerce_int(segment.get("rewrite_count")) <= 0:
            continue
        text = normalize_text(segment.get("tts_cn_text") or segment.get("cn_text"))
        if not text:
            continue
        speaker_id = normalize_text(group.get("speaker_id"))
        cps = candidate.job.chars_per_second_by_speaker.get(
            speaker_id,
            candidate.job.global_chars_per_second,
        )
        target_chars = max(1, int(target_ms / 1000 * cps))
        direction = "shrink" if actual_ms > target_ms else "expand"
        prompt = rewriter._build_rewrite_prompt(
            text,
            direction=direction,
            current_chars=spoken_char_count(text),
            target_chars=target_chars,
            target_lower_chars=max(1, int(target_chars * 0.9)),
            target_upper_chars=max(1, int(target_chars * 1.1)),
            target_lower_ratio_pct=90,
            target_upper_ratio_pct=110,
            change_pct=error_ratio * 100,
            source_text=normalize_text(segment.get("source_text")),
        )
        cases.append(
            {
                "segment_id": group.get("segment_id"),
                "direction": direction,
                "actual_duration_ms": actual_ms,
                "target_duration_ms": target_ms,
                "duration_error_ratio": round(error_ratio, 4),
                "current_spoken_chars": spoken_char_count(text),
                "target_chars": target_chars,
                "rewrite_prompt_source": prompt_bundle.rewrite_source,
                "rewrite_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "rewrite_prompt": prompt,
            }
        )
    return cases


def candidate_to_sample(
    candidate: CandidateWindow,
    *,
    sample_index: int,
    translator: GeminiTranslator,
    prompt_bundle: PromptBundle,
    include_source_url: bool,
    include_retry_prompt: bool,
) -> dict[str, Any]:
    groups = candidate.job.groups[candidate.start:candidate.end]
    glossary = candidate.job.glossary or {}
    source_url = candidate.job.source_url if include_source_url else ""
    prompt = translator._build_prompt(
        groups,
        video_title=candidate.job.video_title,
        youtube_url=source_url,
        glossary=glossary or None,
    )
    retry_prompt = ""
    if include_retry_prompt:
        retry_prompt = translator._build_prompt(
            groups,
            video_title=candidate.job.video_title,
            youtube_url=source_url,
            glossary=glossary or None,
            strict_length_control=True,
        )
    llm_groups = _llm_groups(groups, translator)
    output_schema = [{"segment_id": group.get("segment_id"), "cn_text": ""} for group in llm_groups]
    sample: dict[str, Any] = {
        "sample_id": f"trans_eval_{sample_index:03d}",
        "source_job_hash": candidate.job.job_hash,
        "source_path": candidate.job.segments_path.as_posix(),
        "video_title": candidate.job.video_title,
        "source_url": source_url,
        "service_mode": candidate.job.service_mode,
        "tts_provider": candidate.job.tts_provider,
        "tts_model": candidate.job.tts_model,
        "selection_reasons": list(candidate.reasons),
        "selection_score": candidate.score,
        "metrics": candidate.metrics,
        "window": {
            "start_index": candidate.start,
            "end_index_exclusive": candidate.end,
            "segment_count": candidate.end - candidate.start,
        },
        "calibration": {
            "global_chars_per_second": round(candidate.job.global_chars_per_second, 4),
            "chars_per_second_by_speaker": candidate.job.chars_per_second_by_speaker,
        },
        "glossary": glossary,
        "groups": llm_groups,
        "expected_translation_output_schema": output_schema,
        "reference_translations": _reference_translations(candidate),
        "translation_prompt_source": prompt_bundle.translate_source,
        "translation_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "translation_prompt": prompt,
        "rewrite_cases": _build_rewrite_cases(
            candidate,
            translator=translator,
            prompt_bundle=prompt_bundle,
        ),
    }
    if retry_prompt:
        sample["strict_length_retry_prompt_sha256"] = hashlib.sha256(
            retry_prompt.encode("utf-8")
        ).hexdigest()
        sample["strict_length_retry_prompt"] = retry_prompt
    return sample


def _coverage(samples: list[dict[str, Any]], jobs: list[JobArtifact], candidates: list[CandidateWindow]) -> dict[str, Any]:
    reason_counts: Counter[str] = Counter()
    service_modes: Counter[str] = Counter()
    prompt_sources: Counter[str] = Counter()
    rewrite_cases = 0
    for sample in samples:
        reason_counts.update(sample.get("selection_reasons", []))
        service_modes.update([sample.get("service_mode") or "unknown"])
        prompt_sources.update([sample.get("translation_prompt_source") or "unknown"])
        rewrite_cases += len(sample.get("rewrite_cases", []))
    return {
        "samples": len(samples),
        "jobs_scanned": len(jobs),
        "jobs_selected": len({sample.get("source_job_hash") for sample in samples}),
        "candidate_windows": len(candidates),
        "selection_reasons": dict(sorted(reason_counts.items())),
        "service_modes": dict(sorted(service_modes.items())),
        "translation_prompt_sources": dict(sorted(prompt_sources.items())),
        "rewrite_cases": rewrite_cases,
    }


def validate_sample_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("version") != DATASET_VERSION:
        raise ValueError("Unexpected dataset version")
    samples = payload.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("No samples generated")
    for sample in samples:
        prompt = sample.get("translation_prompt")
        groups = sample.get("groups")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"{sample.get('sample_id')} missing translation_prompt")
        if "__GROUPS_JSON__" in prompt:
            raise ValueError(f"{sample.get('sample_id')} prompt still has __GROUPS_JSON__")
        if not isinstance(groups, list) or not groups:
            raise ValueError(f"{sample.get('sample_id')} missing groups")
        for group in groups:
            if not all(key in group for key in ("source_text", "min_chars", "max_chars", "target_chars")):
                raise ValueError(f"{sample.get('sample_id')} has incomplete group metadata")
        expected_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if sample.get("translation_prompt_sha256") != expected_sha:
            raise ValueError(f"{sample.get('sample_id')} prompt hash mismatch")
    return {"status": "ok", "samples": len(samples)}


def build_translation_eval_samples(
    *,
    data_root: Path = DEFAULT_DATA_ROOT,
    output: Path = DEFAULT_OUTPUT,
    max_samples: int = 24,
    window_size: int = 5,
    min_window_segments: int = 1,
    admin_settings_path: Path | None = None,
    autodub_config_path: Path | None = None,
    include_source_url: bool = True,
    include_retry_prompt: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    if output.exists() and not force:
        raise FileExistsError(f"{output} already exists; pass force=True or --force")

    prompt_bundle = load_prompt_bundle(
        admin_settings_path=admin_settings_path,
        autodub_config_path=autodub_config_path,
    )
    translator = GeminiTranslator(
        "benchmark-key",
        translation_prompt_template=prompt_bundle.translate_template,
        _skip_init=True,
    )
    jobs = load_job_artifacts(data_root)
    candidates = build_candidate_windows(
        jobs,
        window_size=max(1, window_size),
        min_window_segments=max(1, min_window_segments),
    )
    selected = select_windows(candidates, max_samples=max(1, max_samples))
    samples = [
        candidate_to_sample(
            candidate,
            sample_index=index,
            translator=translator,
            prompt_bundle=prompt_bundle,
            include_source_url=include_source_url,
            include_retry_prompt=include_retry_prompt,
        )
        for index, candidate in enumerate(selected, start=1)
    ]

    payload: dict[str, Any] = {
        "version": DATASET_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "data_root": data_root.as_posix(),
            "admin_settings_path": admin_settings_path.as_posix() if admin_settings_path else "",
            "autodub_config_path": autodub_config_path.as_posix() if autodub_config_path else "",
        },
        "prompt_sources": {
            "translate": {
                "source": prompt_bundle.translate_source,
                "template_sha256": hashlib.sha256(
                    prompt_bundle.translate_template.encode("utf-8")
                ).hexdigest(),
            },
            "rewrite": {
                "source": prompt_bundle.rewrite_source,
                "template_sha256": hashlib.sha256(
                    prompt_bundle.rewrite_template.encode("utf-8")
                ).hexdigest(),
            },
            "probe_translate": {
                "source": prompt_bundle.probe_translate_source,
                "template_sha256": hashlib.sha256(
                    prompt_bundle.probe_translate_template.encode("utf-8")
                ).hexdigest(),
            },
            "precedence": [
                "autodub.local.json prompts.s3_translate / prompts.s5_rewrite",
                "admin_settings.json review_prompts.translate / rewrite / probe_translate",
                "runtime default constants",
            ],
        },
        "selection_policy": {
            "max_samples": max_samples,
            "window_size": window_size,
            "min_window_segments": min_window_segments,
            "category_order": list(CATEGORY_ORDER),
            "include_source_url": include_source_url,
            "include_strict_length_retry_prompt": include_retry_prompt,
        },
        "coverage": _coverage(samples, jobs, candidates),
        "samples": samples,
    }
    validate_sample_payload(payload)
    write_json(output, payload)
    write_markdown_summary(output.with_suffix(".md"), payload)
    return payload


def _shorten(text: str, length: int = 72) -> str:
    text = normalize_text(text)
    if len(text) <= length:
        return text
    return text[: length - 1].rstrip() + "..."


def _md_escape(text: Any) -> str:
    return normalize_text(text).replace("|", "\\|")


def write_markdown_summary(path: Path, payload: dict[str, Any]) -> None:
    coverage = payload["coverage"]
    lines = [
        "# Translation Evaluation Samples",
        "",
        f"- Version: `{payload['version']}`",
        f"- Samples: `{coverage['samples']}` from `{coverage['jobs_selected']}` selected jobs",
        f"- Candidate windows: `{coverage['candidate_windows']}` from `{coverage['jobs_scanned']}` scanned jobs",
        f"- Translation prompt source: `{payload['prompt_sources']['translate']['source']}`",
        f"- Rewrite prompt source: `{payload['prompt_sources']['rewrite']['source']}`",
        f"- Rewrite cases with real timing: `{coverage['rewrite_cases']}`",
        "",
        "## Coverage",
        "",
        "```json",
        json.dumps(coverage, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Samples",
        "",
        "| Sample | Reasons | Title | Segments | Seconds | Words | Glossary Hits | Prompt Hash |",
        "| --- | --- | --- | ---: | ---: | ---: | --- | --- |",
    ]
    for sample in payload["samples"]:
        metrics = sample["metrics"]
        lines.append(
            "| {sample} | {reasons} | {title} | {segments} | {seconds} | {words} | {terms} | `{hash}` |".format(
                sample=_md_escape(sample["sample_id"]),
                reasons=_md_escape(", ".join(sample["selection_reasons"])),
                title=_md_escape(_shorten(sample["video_title"])),
                segments=metrics["segment_count"],
                seconds=metrics["duration_seconds"],
                words=metrics["source_word_count"],
                terms=_md_escape(", ".join(metrics.get("glossary_hits", []))),
                hash=str(sample["translation_prompt_sha256"])[:16],
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-samples", type=int, default=24)
    parser.add_argument("--window-size", type=int, default=5)
    parser.add_argument("--min-window-segments", type=int, default=1)
    parser.add_argument("--admin-settings", type=Path, default=None)
    parser.add_argument("--autodub-config", type=Path, default=None)
    parser.add_argument("--redact-source-url", action="store_true")
    parser.add_argument("--no-retry-prompt", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    payload = build_translation_eval_samples(
        data_root=args.data_root,
        output=args.output,
        max_samples=args.max_samples,
        window_size=args.window_size,
        min_window_segments=args.min_window_segments,
        admin_settings_path=args.admin_settings,
        autodub_config_path=args.autodub_config,
        include_source_url=not args.redact_source_url,
        include_retry_prompt=not args.no_retry_prompt,
        force=args.force,
    )
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "summary": args.output.with_suffix(".md").as_posix(),
                "coverage": payload["coverage"],
                "prompt_sources": payload["prompt_sources"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
