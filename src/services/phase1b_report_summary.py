"""Aggregate Phase 1a/1b observational job reports.

The helpers here are pure file readers. They do not mutate job artifacts and do
not make provider calls. Gateway admin APIs and local scripts both use this
module so the on-screen analysis and offline export stay consistent.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any, Iterable


PHASE1B_REPORT_SUMMARY_SCHEMA_VERSION = "phase1b_report_summary_v1"

_TRANSLATION_REPORT = Path("reports/translation_quality_report.json")
_SUBTITLE_WIDTH_REPORT = Path("reports/subtitle_width_report.json")
_SPEAKER_EVIDENCE = Path("reports/speaker_evidence.jsonl")


def summarize_project_reports(
    project_dir: Path | str | None,
    *,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Summarize one project's report sidecars.

    Missing files produce zero-count summaries rather than errors; this lets the
    dashboard include old jobs that ran before reports existed.
    """

    path = Path(project_dir) if project_dir else None
    job_label = str(job_id or (path.name if path else ""))
    if path is None:
        return _empty_project_summary(job_label, project_dir_name=None)
    return {
        "job_id": job_label,
        "project_dir_name": path.name,
        "translation_quality": _summarize_translation_report(path / _TRANSLATION_REPORT),
        "subtitle_width": _summarize_subtitle_width_report(path / _SUBTITLE_WIDTH_REPORT),
        "speaker_evidence": _summarize_speaker_evidence(path / _SPEAKER_EVIDENCE),
        "voice_sample_scoring": _summarize_voice_sample_scoring(path),
    }


def build_phase1b_summary(
    job_rows: Iterable[dict[str, Any]],
    *,
    days: int | None = None,
) -> dict[str, Any]:
    rows = list(job_rows)
    total_jobs = len(rows)

    translation_reports = [
        row for row in rows if row["reports"]["translation_quality"]["exists"]
    ]
    translation_issue_count = sum(
        int(row["reports"]["translation_quality"]["issue_count"] or 0)
        for row in rows
    )
    translation_checked = sum(
        int(row["reports"]["translation_quality"]["checked_segments"] or 0)
        for row in rows
    )
    translation_issue_jobs = [
        row for row in translation_reports
        if int(row["reports"]["translation_quality"]["issue_count"] or 0) > 0
    ]

    voice_reports = [
        row for row in rows
        if int(row["reports"]["voice_sample_scoring"]["manifest_count"] or 0) > 0
    ]
    voice_manifest_count = sum(
        int(row["reports"]["voice_sample_scoring"]["manifest_count"] or 0)
        for row in rows
    )
    voice_candidate_count = sum(
        int(row["reports"]["voice_sample_scoring"]["candidate_count"] or 0)
        for row in rows
    )
    voice_hard_reject_count = sum(
        int(row["reports"]["voice_sample_scoring"]["hard_reject_candidate_count"] or 0)
        for row in rows
    )
    selected_hard_reject_count = sum(
        int(row["reports"]["voice_sample_scoring"]["selected_hard_reject_manifest_count"] or 0)
        for row in rows
    )

    subtitle_issue_count = sum(
        int(row["reports"]["subtitle_width"]["issue_count"] or 0)
        for row in rows
    )
    speaker_changed_rows = sum(
        int(row["reports"]["speaker_evidence"]["changed_count"] or 0)
        for row in rows
    )
    speaker_uncertain_rows = sum(
        int(row["reports"]["speaker_evidence"]["uncertain_count"] or 0)
        for row in rows
    )

    return {
        "schema_version": PHASE1B_REPORT_SUMMARY_SCHEMA_VERSION,
        "window": {"days": days},
        "kpi": {
            "total_jobs": total_jobs,
            "jobs_with_any_report": sum(1 for row in rows if _row_has_any_report(row)),
            "translation_report_jobs": len(translation_reports),
            "translation_issue_jobs": len(translation_issue_jobs),
            "translation_issue_count": translation_issue_count,
            "translation_checked_segments": translation_checked,
            "translation_issue_rate": _rate(translation_issue_count, translation_checked),
            "voice_sample_report_jobs": len(voice_reports),
            "voice_manifest_count": voice_manifest_count,
            "voice_candidate_count": voice_candidate_count,
            "voice_hard_reject_candidate_count": voice_hard_reject_count,
            "voice_hard_reject_rate": _rate(voice_hard_reject_count, voice_candidate_count),
            "voice_selected_hard_reject_manifest_count": selected_hard_reject_count,
            "subtitle_width_issue_count": subtitle_issue_count,
            "speaker_changed_rows": speaker_changed_rows,
            "speaker_uncertain_rows": speaker_uncertain_rows,
        },
        "recommendations": _build_recommendations(
            total_jobs=total_jobs,
            translation_reports=len(translation_reports),
            translation_issue_count=translation_issue_count,
            translation_checked=translation_checked,
            translation_issue_jobs=len(translation_issue_jobs),
            voice_manifest_count=voice_manifest_count,
            voice_candidate_count=voice_candidate_count,
            voice_hard_reject_count=voice_hard_reject_count,
            selected_hard_reject_count=selected_hard_reject_count,
        ),
        "reason_counts": {
            "translation": _merge_counter(
                row["reports"]["translation_quality"]["reason_counts"]
                for row in rows
            ),
            "speaker_decisions": _merge_counter(
                row["reports"]["speaker_evidence"]["decision_counts"]
                for row in rows
            ),
            "voice_hard_reject_reasons": _merge_counter(
                row["reports"]["voice_sample_scoring"]["hard_reject_reason_counts"]
                for row in rows
            ),
            "voice_warnings": _merge_counter(
                row["reports"]["voice_sample_scoring"]["warning_counts"]
                for row in rows
            ),
        },
        "jobs": rows,
    }


def build_phase1b_csv(job_rows: Iterable[dict[str, Any]]) -> bytes:
    columns = [
        "job_id",
        "service_mode",
        "status",
        "user_email",
        "display_name",
        "created_at",
        "translation_checked_segments",
        "translation_issue_count",
        "translation_issue_rate",
        "subtitle_width_issue_count",
        "speaker_evidence_rows",
        "speaker_changed_count",
        "speaker_uncertain_count",
        "voice_manifest_count",
        "voice_candidate_count",
        "voice_hard_reject_candidate_count",
        "voice_hard_reject_rate",
        "voice_selected_hard_reject_manifest_count",
    ]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for row in job_rows:
        reports = row.get("reports", {})
        translation = reports.get("translation_quality", {})
        subtitle = reports.get("subtitle_width", {})
        speaker = reports.get("speaker_evidence", {})
        voice = reports.get("voice_sample_scoring", {})
        writer.writerow([
            row.get("job_id") or "",
            row.get("service_mode") or "",
            row.get("status") or "",
            row.get("user_email") or "",
            row.get("display_name") or "",
            row.get("created_at") or "",
            translation.get("checked_segments") or 0,
            translation.get("issue_count") or 0,
            translation.get("issue_rate") if translation.get("issue_rate") is not None else "",
            subtitle.get("issue_count") or 0,
            speaker.get("row_count") or 0,
            speaker.get("changed_count") or 0,
            speaker.get("uncertain_count") or 0,
            voice.get("manifest_count") or 0,
            voice.get("candidate_count") or 0,
            voice.get("hard_reject_candidate_count") or 0,
            voice.get("hard_reject_rate") if voice.get("hard_reject_rate") is not None else "",
            voice.get("selected_hard_reject_manifest_count") or 0,
        ])
    return b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")


def discover_project_dirs(project_root: Path | str) -> list[Path]:
    root = Path(project_root)
    if not root.is_dir():
        return []
    return sorted([path for path in root.iterdir() if path.is_dir()], key=lambda p: p.name)


def _empty_project_summary(
    job_id: str,
    *,
    project_dir_name: str | None,
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "project_dir_name": project_dir_name,
        "translation_quality": _empty_translation_summary(False),
        "subtitle_width": _empty_subtitle_summary(False),
        "speaker_evidence": _empty_speaker_summary(False),
        "voice_sample_scoring": _empty_voice_summary(False),
    }


def _summarize_translation_report(path: Path) -> dict[str, Any]:
    payload = _read_json(path)
    if not isinstance(payload, dict):
        return _empty_translation_summary(False)
    issue_count = _int(payload.get("issue_count"), _int(payload.get("script_gate_fail_count"), 0))
    checked = _int(payload.get("checked_segments"), 0)
    return {
        "exists": True,
        "schema_version": payload.get("schema_version"),
        "checked_segments": checked,
        "issue_count": issue_count,
        "issue_rate": _rate(issue_count, checked),
        "skipped_keep_original_segments": _int(payload.get("skipped_keep_original_segments"), 0),
        "reason_counts": _counter(payload.get("reason_counts")),
    }


def _summarize_subtitle_width_report(path: Path) -> dict[str, Any]:
    payload = _read_json(path)
    if not isinstance(payload, dict):
        return _empty_subtitle_summary(False)
    issues = payload.get("issues")
    issue_list = issues if isinstance(issues, list) else []
    widths = [
        _int(issue.get("width_units"), 0)
        for issue in issue_list
        if isinstance(issue, dict)
    ]
    return {
        "exists": True,
        "schema_version": payload.get("schema_version"),
        "max_display_width": _int(payload.get("max_display_width"), 0),
        "issue_count": _int(payload.get("issue_count"), len(issue_list)),
        "max_width_units": max(widths) if widths else 0,
    }


def _summarize_speaker_evidence(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return _empty_speaker_summary(False)
    decision_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    row_count = 0
    for record in _iter_jsonl(path):
        row_count += 1
        decision = str(record.get("decision") or "unknown")
        decision_counts[decision] = decision_counts.get(decision, 0) + 1
        for reason in record.get("reason_codes") or []:
            key = str(reason)
            reason_counts[key] = reason_counts.get(key, 0) + 1
    return {
        "exists": True,
        "row_count": row_count,
        "decision_counts": decision_counts,
        "reason_counts": reason_counts,
        "changed_count": decision_counts.get("changed", 0),
        "uncertain_count": decision_counts.get("kept_uncertain", 0),
    }


def _summarize_voice_sample_scoring(project_dir: Path) -> dict[str, Any]:
    manifests = sorted(project_dir.rglob("*.manifest.v2.json"))
    if not manifests:
        return _empty_voice_summary(False)

    candidate_count = 0
    hard_reject_candidate_count = 0
    selected_hard_reject_manifest_count = 0
    hard_reject_reasons: dict[str, int] = {}
    warnings: dict[str, int] = {}
    score_values: list[float] = []
    rel_paths: list[str] = []

    for manifest in manifests:
        payload = _read_json(manifest)
        if not isinstance(payload, dict):
            continue
        rel_paths.append(_relative_path(manifest, project_dir))
        for warning in payload.get("warnings") or []:
            key = str(warning)
            warnings[key] = warnings.get(key, 0) + 1
        selected = payload.get("selected_sample_stats")
        if isinstance(selected, dict):
            selected_reasons = selected.get("hard_reject_reasons") or []
            if selected_reasons:
                selected_hard_reject_manifest_count += 1
            for reason in selected_reasons:
                key = str(reason)
                hard_reject_reasons[key] = hard_reject_reasons.get(key, 0) + 1
            for warning in selected.get("warnings") or []:
                key = str(warning)
                warnings[key] = warnings.get(key, 0) + 1

        for candidate in payload.get("candidate_scores") or []:
            if not isinstance(candidate, dict):
                continue
            candidate_count += 1
            score = _float(candidate.get("score"))
            if score is not None:
                score_values.append(score)
            reasons = candidate.get("hard_reject_reasons") or []
            if reasons:
                hard_reject_candidate_count += 1
            for reason in reasons:
                key = str(reason)
                hard_reject_reasons[key] = hard_reject_reasons.get(key, 0) + 1
            for warning in candidate.get("warnings") or []:
                key = str(warning)
                warnings[key] = warnings.get(key, 0) + 1

    return {
        "exists": True,
        "manifest_count": len(manifests),
        "manifest_paths": rel_paths[:10],
        "candidate_count": candidate_count,
        "hard_reject_candidate_count": hard_reject_candidate_count,
        "hard_reject_rate": _rate(hard_reject_candidate_count, candidate_count),
        "selected_hard_reject_manifest_count": selected_hard_reject_manifest_count,
        "avg_candidate_score": _mean(score_values),
        "hard_reject_reason_counts": hard_reject_reasons,
        "warning_counts": warnings,
    }


def _build_recommendations(
    *,
    total_jobs: int,
    translation_reports: int,
    translation_issue_count: int,
    translation_checked: int,
    translation_issue_jobs: int,
    voice_manifest_count: int,
    voice_candidate_count: int,
    voice_hard_reject_count: int,
    selected_hard_reject_count: int,
) -> dict[str, Any]:
    translation_issue_rate = _rate(translation_issue_count, translation_checked) or 0.0
    voice_hard_reject_rate = _rate(voice_hard_reject_count, voice_candidate_count) or 0.0
    return {
        "translation_script_gate": _recommend_translation_gate(
            total_jobs=total_jobs,
            report_jobs=translation_reports,
            issue_count=translation_issue_count,
            issue_jobs=translation_issue_jobs,
            issue_rate=translation_issue_rate,
        ),
        "voice_sample_scoring": _recommend_voice_scoring(
            manifest_count=voice_manifest_count,
            candidate_count=voice_candidate_count,
            hard_reject_rate=voice_hard_reject_rate,
            selected_hard_reject_count=selected_hard_reject_count,
        ),
    }


def _recommend_translation_gate(
    *,
    total_jobs: int,
    report_jobs: int,
    issue_count: int,
    issue_jobs: int,
    issue_rate: float,
) -> dict[str, Any]:
    if report_jobs < 3:
        return {
            "status": "collect_more_data",
            "rationale": "Need at least 3 jobs with translation quality reports before behavior canary.",
        }
    if issue_count == 0:
        return {
            "status": "hold_no_signal",
            "rationale": "No wrong-script signal observed; keep detect-only until a real issue appears.",
        }
    if issue_rate <= 0.08 and issue_jobs <= max(2, int(total_jobs * 0.35)):
        return {
            "status": "ready_for_small_canary",
            "rationale": "Wrong-script signal exists and is sparse enough for a no-retry canary.",
        }
    return {
        "status": "inspect_before_canary",
        "rationale": "Wrong-script signal is broad; inspect report rows before enabling behavior.",
    }


def _recommend_voice_scoring(
    *,
    manifest_count: int,
    candidate_count: int,
    hard_reject_rate: float,
    selected_hard_reject_count: int,
) -> dict[str, Any]:
    if manifest_count < 3:
        return {
            "status": "collect_more_data",
            "rationale": "Need at least 3 v2 voice manifests before scoring canary.",
        }
    if selected_hard_reject_count > 0:
        return {
            "status": "inspect_selected_samples",
            "rationale": "Current selected samples include hard rejects; inspect audio before changing selection behavior.",
        }
    if candidate_count > 0 and hard_reject_rate <= 0.40:
        return {
            "status": "ready_after_manual_spot_check",
            "rationale": "Candidate pool looks usable; spot-check recommended samples before behavior canary.",
        }
    return {
        "status": "hold_scoring",
        "rationale": "Candidate hard-reject rate is high; scoring may overfit weak samples.",
    }


def _empty_translation_summary(exists: bool) -> dict[str, Any]:
    return {
        "exists": exists,
        "schema_version": None,
        "checked_segments": 0,
        "issue_count": 0,
        "issue_rate": None,
        "skipped_keep_original_segments": 0,
        "reason_counts": {},
    }


def _empty_subtitle_summary(exists: bool) -> dict[str, Any]:
    return {
        "exists": exists,
        "schema_version": None,
        "max_display_width": None,
        "issue_count": 0,
        "max_width_units": 0,
    }


def _empty_speaker_summary(exists: bool) -> dict[str, Any]:
    return {
        "exists": exists,
        "row_count": 0,
        "decision_counts": {},
        "reason_counts": {},
        "changed_count": 0,
        "uncertain_count": 0,
    }


def _empty_voice_summary(exists: bool) -> dict[str, Any]:
    return {
        "exists": exists,
        "manifest_count": 0,
        "manifest_paths": [],
        "candidate_count": 0,
        "hard_reject_candidate_count": 0,
        "hard_reject_rate": None,
        "selected_hard_reject_manifest_count": 0,
        "avg_candidate_score": None,
        "hard_reject_reason_counts": {},
        "warning_counts": {},
    }


def _row_has_any_report(row: dict[str, Any]) -> bool:
    reports = row.get("reports") or {}
    return any(bool(value.get("exists")) for value in reports.values() if isinstance(value, dict))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _iter_jsonl(path: Path):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(record, dict):
            yield record


def _counter(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, int] = {}
    for key, count in value.items():
        out[str(key)] = _int(count, 0)
    return out


def _merge_counter(counters: Iterable[dict[str, int]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for counter in counters:
        if not isinstance(counter, dict):
            continue
        for key, count in counter.items():
            out[str(key)] = out.get(str(key), 0) + _int(count, 0)
    return dict(sorted(out.items(), key=lambda item: (-item[1], item[0])))


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(float(numerator) / float(denominator), 6)


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 6)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


__all__ = [
    "PHASE1B_REPORT_SUMMARY_SCHEMA_VERSION",
    "build_phase1b_csv",
    "build_phase1b_summary",
    "discover_project_dirs",
    "summarize_project_reports",
]
