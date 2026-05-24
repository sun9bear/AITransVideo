from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from utils.env_flags import env_flag


logger = logging.getLogger(__name__)

TRANSLATION_QUALITY_REPORT_SCHEMA_VERSION = "translation_quality_report_v1"
TARGET_LANGUAGE = "zh-CN"
TARGET_LANGUAGE_FULL_NAME = "Chinese (Simplified)"

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_DIGIT_RE = re.compile(r"\d")


def translation_quality_report_path(project_dir: Path) -> Path:
    return project_dir / "reports" / "translation_quality_report.json"


def translation_script_gate_shadow_enabled() -> bool:
    return (
        env_flag("AVT_TRANSLATION_SCRIPT_GATE_SHADOW")
        or env_flag("AVT_TRANSLATION_SCRIPT_GATE_DETECT_ONLY")
    )


def evaluate_zh_cn_script_gate(
    text: str,
    *,
    allowed_latin_terms: Iterable[str] | None = None,
) -> dict[str, Any]:
    normalized = str(text or "").strip()
    stripped_for_counts = _remove_allowed_latin_terms(
        normalized,
        allowed_latin_terms=allowed_latin_terms,
    )
    cjk_count = len(_CJK_RE.findall(stripped_for_counts))
    latin_count = len(_LATIN_RE.findall(stripped_for_counts))
    digit_count = len(_DIGIT_RE.findall(stripped_for_counts))
    relevant_count = cjk_count + latin_count
    latin_ratio = round(latin_count / max(1, relevant_count), 4)

    reason_codes: list[str] = []
    if cjk_count == 0 and latin_count >= 8:
        reason_codes.append("no_cjk_latin_long")
    if latin_ratio >= 0.70 and cjk_count <= 2 and latin_count >= 10:
        reason_codes.append("latin_dominant")
    if cjk_count == 0 and (latin_count + digit_count) >= 6:
        reason_codes.append("no_cjk_nontrivial")

    return {
        "ok": not reason_codes,
        "reason_codes": reason_codes,
        "target_language": TARGET_LANGUAGE,
        "target_language_full_name": TARGET_LANGUAGE_FULL_NAME,
        "cjk_count": cjk_count,
        "latin_count": latin_count,
        "digit_count": digit_count,
        "latin_ratio": latin_ratio,
        "text_length": len(normalized),
        "text_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    }


def build_translation_quality_report(
    *,
    project_id: str,
    segments: Iterable[Any],
    glossary: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    allowed_latin_terms = _allowed_latin_terms_from_glossary(glossary or {})
    issues: list[dict[str, Any]] = []
    checked_segments = 0
    skipped_keep_original = 0
    reason_counts: dict[str, int] = {}

    for segment in segments:
        segment_id = _segment_value(segment, "segment_id")
        dubbing_mode = str(_segment_value(segment, "dubbing_mode") or "").strip().lower()
        if dubbing_mode == "keep_original":
            skipped_keep_original += 1
            continue
        checked_segments += 1
        text = str(_segment_value(segment, "cn_text") or "")
        result = evaluate_zh_cn_script_gate(
            text,
            allowed_latin_terms=allowed_latin_terms,
        )
        if result["ok"]:
            continue
        for reason in result["reason_codes"]:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        issues.append(
            {
                "segment_id": segment_id,
                "speaker_id": _segment_value(segment, "speaker_id"),
                "reason_codes": result["reason_codes"],
                "cjk_count": result["cjk_count"],
                "latin_count": result["latin_count"],
                "digit_count": result["digit_count"],
                "latin_ratio": result["latin_ratio"],
                "text_length": result["text_length"],
                "text_sha256": result["text_sha256"],
                "advisory_only": True,
            }
        )

    return {
        "schema_version": TRANSLATION_QUALITY_REPORT_SCHEMA_VERSION,
        "project_id": project_id,
        "advisory_only": True,
        "gate_mode": "detect_only",
        "target_language": TARGET_LANGUAGE,
        "target_language_full_name": TARGET_LANGUAGE_FULL_NAME,
        "script_gate_fail_count": len(issues),
        "issue_count": len(issues),
        "checked_segments": checked_segments,
        "skipped_keep_original_segments": skipped_keep_original,
        "reason_counts": reason_counts,
        "allowed_latin_term_count": len(allowed_latin_terms),
        "issues": issues,
    }


def write_translation_quality_report(
    project_dir: Path,
    *,
    segments: Iterable[Any],
    glossary: Mapping[str, str] | None = None,
) -> bool:
    if not translation_script_gate_shadow_enabled():
        return False
    report_path = translation_quality_report_path(project_dir)
    payload = build_translation_quality_report(
        project_id=project_dir.name,
        segments=segments,
        glossary=glossary,
    )
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError:
        logger.warning(
            "translation quality report sidecar write failed; continuing: %s",
            report_path,
            exc_info=True,
        )
        return False
    return True


def _segment_value(segment: Any, key: str) -> Any:
    if isinstance(segment, Mapping):
        return segment.get(key)
    return getattr(segment, key, None)


def _allowed_latin_terms_from_glossary(glossary: Mapping[str, str]) -> set[str]:
    terms: set[str] = set()
    for key, value in glossary.items():
        for raw in (key, value):
            text = str(raw or "").strip()
            if not text or not _LATIN_RE.search(text):
                continue
            terms.add(text)
    return terms


def _remove_allowed_latin_terms(
    text: str,
    *,
    allowed_latin_terms: Iterable[str] | None,
) -> str:
    result = text
    for term in sorted(set(allowed_latin_terms or []), key=len, reverse=True):
        if not term:
            continue
        result = re.sub(re.escape(term), "", result, flags=re.IGNORECASE)
    return result


__all__ = [
    "TARGET_LANGUAGE",
    "TARGET_LANGUAGE_FULL_NAME",
    "TRANSLATION_QUALITY_REPORT_SCHEMA_VERSION",
    "build_translation_quality_report",
    "evaluate_zh_cn_script_gate",
    "translation_quality_report_path",
    "translation_script_gate_shadow_enabled",
    "write_translation_quality_report",
]
