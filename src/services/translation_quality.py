from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from services.language_registry import normalize_language
from services.runtime_flags import runtime_flag

logger = logging.getLogger(__name__)

TRANSLATION_QUALITY_REPORT_SCHEMA_VERSION = "translation_quality_report_v1"
# Legacy default (pre-language-pair era). Callers with a real language pair
# must pass the job's target explicitly \u2014 zh->en jobs stamped "zh-CN" here
# made the zh script gate flag normal English dubbing text (2026-07-02 fix).
TARGET_LANGUAGE = "zh-CN"
TARGET_LANGUAGE_FULL_NAME = "Chinese (Simplified)"

_TARGET_LANGUAGE_FULL_NAMES = {
    "zh-CN": TARGET_LANGUAGE_FULL_NAME,
    "en": "English",
}

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_DIGIT_RE = re.compile(r"\d")


def _resolve_target_language(value: str | None) -> tuple[str, str]:
    """Canonicalize *value* to (code, full_name).

    Uses the language registry so aliases ("English", "EN", "\u4e2d\u6587") resolve to
    canonical codes; unknown values pass through raw so the report stays honest
    about what it was given (the script gate then simply doesn't run).
    """
    raw = str(value or "").strip() or TARGET_LANGUAGE
    resolved = normalize_language(raw) or raw
    return resolved, _TARGET_LANGUAGE_FULL_NAMES.get(resolved, resolved)


def translation_quality_report_path(project_dir: Path) -> Path:
    return project_dir / "reports" / "translation_quality_report.json"


def translation_script_gate_shadow_enabled() -> bool:
    return runtime_flag("AVT_TRANSLATION_SCRIPT_GATE_SHADOW") or runtime_flag("AVT_TRANSLATION_SCRIPT_GATE_DETECT_ONLY")


def _script_gate_result(
    text: str,
    *,
    allowed_terms: Iterable[str] | None,
    target_language: str,
    reason_codes_fn: Any,
) -> dict[str, Any]:
    """Shared counting + result shape for the per-language script gates."""
    normalized = str(text or "").strip()
    stripped_for_counts = _remove_allowed_terms(
        normalized,
        allowed_terms=allowed_terms,
    )
    cjk_count = len(_CJK_RE.findall(stripped_for_counts))
    latin_count = len(_LATIN_RE.findall(stripped_for_counts))
    digit_count = len(_DIGIT_RE.findall(stripped_for_counts))
    relevant_count = cjk_count + latin_count
    latin_ratio = round(latin_count / max(1, relevant_count), 4)
    cjk_ratio = round(cjk_count / max(1, relevant_count), 4)

    reason_codes: list[str] = reason_codes_fn(cjk_count, latin_count, digit_count)

    resolved, full_name = _resolve_target_language(target_language)
    return {
        "ok": not reason_codes,
        "reason_codes": reason_codes,
        "target_language": resolved,
        "target_language_full_name": full_name,
        "cjk_count": cjk_count,
        "latin_count": latin_count,
        "digit_count": digit_count,
        "latin_ratio": latin_ratio,
        "cjk_ratio": cjk_ratio,
        "text_length": len(normalized),
        "text_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    }


def evaluate_zh_cn_script_gate(
    text: str,
    *,
    allowed_latin_terms: Iterable[str] | None = None,
) -> dict[str, Any]:
    """zh-CN target: flag text that is not Chinese script (translation no-op)."""

    def _reasons(cjk_count: int, latin_count: int, digit_count: int) -> list[str]:
        latin_ratio = latin_count / max(1, cjk_count + latin_count)
        reason_codes: list[str] = []
        if cjk_count == 0 and latin_count >= 8:
            reason_codes.append("no_cjk_latin_long")
        if latin_ratio >= 0.70 and cjk_count <= 2 and latin_count >= 10:
            reason_codes.append("latin_dominant")
        if cjk_count == 0 and (latin_count + digit_count) >= 6:
            reason_codes.append("no_cjk_nontrivial")
        return reason_codes

    return _script_gate_result(
        text,
        allowed_terms=allowed_latin_terms,
        target_language="zh-CN",
        reason_codes_fn=_reasons,
    )


def evaluate_en_script_gate(
    text: str,
    *,
    allowed_cjk_terms: Iterable[str] | None = None,
) -> dict[str, Any]:
    """en target: flag CJK leaking into what should be pure English output.

    Mirror of the zh-CN gate with the roles swapped. Digits are script-neutral
    for English (pure numbers are legitimate), so every reason code requires
    actual CJK evidence.
    """

    def _reasons(cjk_count: int, latin_count: int, digit_count: int) -> list[str]:
        cjk_ratio = cjk_count / max(1, cjk_count + latin_count)
        reason_codes: list[str] = []
        if cjk_ratio >= 0.70 and latin_count <= 2 and cjk_count >= 10:
            reason_codes.append("cjk_dominant")
        if cjk_count >= 6:
            reason_codes.append("cjk_nontrivial")
        return reason_codes

    return _script_gate_result(
        text,
        allowed_terms=allowed_cjk_terms,
        target_language="en",
        reason_codes_fn=_reasons,
    )


def build_translation_quality_report(
    *,
    project_id: str,
    segments: Iterable[Any],
    glossary: Mapping[str, str] | None = None,
    target_language: str | None = None,
) -> dict[str, Any]:
    resolved_target, target_full_name = _resolve_target_language(target_language)
    # Per-target gate dispatch. Unknown targets get no gate (advisory report
    # stays noise-free) and are marked via script_gate_supported=False.
    if resolved_target == "en":
        allowed_terms = _allowed_cjk_terms_from_glossary(glossary or {})
        gate = lambda text: evaluate_en_script_gate(  # noqa: E731
            text, allowed_cjk_terms=allowed_terms
        )
    elif resolved_target == "zh-CN":
        allowed_terms = _allowed_latin_terms_from_glossary(glossary or {})
        gate = lambda text: evaluate_zh_cn_script_gate(  # noqa: E731
            text, allowed_latin_terms=allowed_terms
        )
    else:
        allowed_terms = set()
        gate = None

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
        if gate is None:
            continue
        text = str(_segment_value(segment, "cn_text") or "")
        result = gate(text)
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
        "target_language": resolved_target,
        "target_language_full_name": target_full_name,
        "script_gate_supported": gate is not None,
        "script_gate_fail_count": len(issues),
        "issue_count": len(issues),
        "checked_segments": checked_segments,
        "skipped_keep_original_segments": skipped_keep_original,
        "reason_counts": reason_counts,
        # Count of glossary terms excluded from counting for the ACTIVE gate
        # (latin terms for zh-CN, CJK terms for en). Key name kept for schema
        # stability with pre-language-pair reports.
        "allowed_latin_term_count": len(allowed_terms),
        "issues": issues,
    }


def write_translation_quality_report(
    project_dir: Path,
    *,
    segments: Iterable[Any],
    glossary: Mapping[str, str] | None = None,
    target_language: str | None = None,
) -> bool:
    if not translation_script_gate_shadow_enabled():
        return False
    report_path = translation_quality_report_path(project_dir)
    payload = build_translation_quality_report(
        project_id=project_dir.name,
        segments=segments,
        glossary=glossary,
        target_language=target_language,
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


def _allowed_terms_from_glossary(
    glossary: Mapping[str, str],
    pattern: re.Pattern[str],
    *,
    sides: tuple[str, ...] = ("key", "value"),
) -> set[str]:
    terms: set[str] = set()
    for key, value in glossary.items():
        picks = {"key": key, "value": value}
        for side in sides:
            text = str(picks[side] or "").strip()
            if not text or not pattern.search(text):
                continue
            terms.add(text)
    return terms


def _allowed_latin_terms_from_glossary(glossary: Mapping[str, str]) -> set[str]:
    # zh-CN gate (GA lane): keys AND values, unchanged legacy behavior — for
    # en->zh glossaries either side may carry a Latin term the zh output is
    # expected to keep verbatim.
    return _allowed_terms_from_glossary(glossary, _LATIN_RE)


def _allowed_cjk_terms_from_glossary(glossary: Mapping[str, str]) -> set[str]:
    # en gate: VALUE side only (@codex review round-2 P2). zh->en glossaries
    # map zh source -> en target, so a glossary KEY appearing in the English
    # output is exactly the untranslated-term leak this gate exists to catch —
    # it must NOT be exempted. Only target-side CJK (e.g. a brand the glossary
    # deliberately keeps in hanzi, value containing CJK) is legitimate output.
    return _allowed_terms_from_glossary(glossary, _CJK_RE, sides=("value",))


def _remove_allowed_terms(
    text: str,
    *,
    allowed_terms: Iterable[str] | None,
) -> str:
    result = text
    for term in sorted(set(allowed_terms or []), key=len, reverse=True):
        if not term:
            continue
        result = re.sub(re.escape(term), "", result, flags=re.IGNORECASE)
    return result


__all__ = [
    "TARGET_LANGUAGE",
    "TARGET_LANGUAGE_FULL_NAME",
    "TRANSLATION_QUALITY_REPORT_SCHEMA_VERSION",
    "build_translation_quality_report",
    "evaluate_en_script_gate",
    "evaluate_zh_cn_script_gate",
    "translation_quality_report_path",
    "translation_script_gate_shadow_enabled",
    "write_translation_quality_report",
]
