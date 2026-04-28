#!/usr/bin/env python3
"""Generate a P2 speaker attribution convergence report from job artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_OUTPUT_DIR = Path("reports/benchmark")
DEFAULT_REPORT_STEM = "speaker_attribution_p2_convergence"


@dataclass(frozen=True)
class ReportConfig:
    projects_root: Path
    output_dir: Path = DEFAULT_OUTPUT_DIR
    output_stem: str = DEFAULT_REPORT_STEM
    max_jobs: int = 30
    sample_segments_per_speaker: int = 3
    force: bool = False


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _job_dirs(projects_root: Path, max_jobs: int) -> list[Path]:
    candidates: list[Path] = []
    for path in projects_root.rglob("job_*"):
        if path.is_dir() and (path / "translation" / "segments.json").exists():
            candidates.append(path)
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[:max_jobs]


def _duration_ms(segment: dict[str, Any]) -> int:
    start_ms = int(segment.get("start_ms") or 0)
    end_ms = int(segment.get("end_ms") or 0)
    return max(0, end_ms - start_ms)


def _source_key(url: str, title: str) -> str:
    material = url.strip() or title.strip()
    if not material:
        return "unknown"
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:12]


def _speaker_name(segment: dict[str, Any]) -> str:
    return str(segment.get("display_name") or segment.get("speaker_id") or "")


def _first_pass_errors(segments: list[dict[str, Any]]) -> list[float]:
    errors: list[float] = []
    for segment in segments:
        value = segment.get("first_pass_error_pct")
        if value in (None, ""):
            continue
        try:
            errors.append(abs(float(value)))
        except (TypeError, ValueError):
            continue
    errors.sort()
    return errors


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    index = max(0, min(len(sorted_values) - 1, int(len(sorted_values) * pct) - 1))
    return sorted_values[index]


def _speaker_profiles(segments: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    for segment in segments:
        speaker_id = str(segment.get("speaker_id") or "")
        if not speaker_id or speaker_id in profiles:
            continue
        profiles[speaker_id] = {
            "speaker_id": speaker_id,
            "display_name": _speaker_name(segment),
            "role": str(segment.get("speaker_role") or ""),
            "role_label": str(segment.get("speaker_role_label") or ""),
            "duration_ms": int(segment.get("speaker_duration_ms") or 0),
            "duration_share": round(float(segment.get("speaker_duration_share") or 0.0), 4),
            "segment_count": int(segment.get("speaker_segment_count") or 0),
            "short_segment_count": int(segment.get("speaker_short_segment_count") or 0),
            "short_segment_rate": round(float(segment.get("speaker_short_segment_rate") or 0.0), 4),
            "reason": str(segment.get("speaker_structure_reason") or ""),
            "review_hint": str(segment.get("speaker_review_hint") or ""),
        }
    has_structured_profiles = any(
        profile.get("role")
        or float(profile.get("duration_share") or 0.0) > 0
        or int(profile.get("duration_ms") or 0) > 0
        for profile in profiles.values()
    )
    if profiles and has_structured_profiles:
        return profiles

    total_duration_ms = sum(_duration_ms(segment) for segment in segments)
    by_speaker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for segment in segments:
        speaker_id = str(segment.get("speaker_id") or "")
        if speaker_id:
            by_speaker[speaker_id].append(segment)
    for speaker_id, speaker_segments in by_speaker.items():
        duration_ms = sum(_duration_ms(segment) for segment in speaker_segments)
        profiles[speaker_id] = {
            "speaker_id": speaker_id,
            "display_name": _speaker_name(speaker_segments[0]),
            "role": "",
            "role_label": "",
            "duration_ms": duration_ms,
            "duration_share": round(duration_ms / total_duration_ms, 4)
            if total_duration_ms
            else 0.0,
            "segment_count": len(speaker_segments),
            "short_segment_count": sum(
                1 for segment in speaker_segments if _duration_ms(segment) <= 8_000
            ),
            "short_segment_rate": 0.0,
            "reason": "",
            "review_hint": "",
        }
        count = profiles[speaker_id]["segment_count"]
        if count:
            profiles[speaker_id]["short_segment_rate"] = round(
                profiles[speaker_id]["short_segment_count"] / count,
                4,
            )
    return profiles


def _sample_segments(
    segments: list[dict[str, Any]],
    *,
    primary_speaker_id: str,
    limit: int,
) -> dict[str, list[dict[str, Any]]]:
    samples: dict[str, list[dict[str, Any]]] = {}
    for speaker_id in sorted({str(segment.get("speaker_id") or "") for segment in segments}):
        if not speaker_id or speaker_id == primary_speaker_id:
            continue
        speaker_segments = [
            segment for segment in segments if str(segment.get("speaker_id") or "") == speaker_id
        ]
        items: list[dict[str, Any]] = []
        for segment in speaker_segments[:limit]:
            items.append(
                {
                    "segment_id": segment.get("segment_id"),
                    "duration_s": round(_duration_ms(segment) / 1000, 1),
                    "method": segment.get("alignment_method") or "",
                    "severity": segment.get("force_dsp_severity") or "",
                    "source_text": str(segment.get("source_text") or "")[:160],
                    "cn_text": str(segment.get("cn_text") or "")[:120],
                }
            )
        samples[speaker_id] = items
    return samples


def _verifier_summary(job_dir: Path) -> dict[str, Any]:
    payload = _read_json(job_dir / "transcript" / "s2_pass1_result.json")
    verifier = payload.get("speaker_verifier")
    if not isinstance(verifier, dict):
        return {"enabled": False}
    decisions = verifier.get("decisions")
    decision_counts: dict[str, int] = {}
    if isinstance(decisions, list):
        for decision in decisions:
            if isinstance(decision, dict):
                key = str(decision.get("decision") or decision.get("label") or "unknown")
                decision_counts[key] = decision_counts.get(key, 0) + 1
    candidates = verifier.get("candidates")
    return {
        "enabled": bool(verifier.get("enabled", True)),
        "applied": bool(payload.get("speaker_verifier_applied")),
        "candidate_count": len(candidates) if isinstance(candidates, list) else 0,
        "decision_counts": decision_counts,
        "skipped_reason": verifier.get("skipped_reason") or "",
    }


def summarize_job(job_dir: Path, *, sample_segments_per_speaker: int) -> dict[str, Any]:
    metadata = _read_json(job_dir / "download_metadata.json")
    payload = _read_json(job_dir / "translation" / "segments.json")
    segments = payload.get("segments")
    if not isinstance(segments, list):
        segments = []

    profiles = _speaker_profiles(segments)
    primary_speaker_id = ""
    if profiles:
        primary_speaker_id = max(
            profiles,
            key=lambda speaker_id: float(profiles[speaker_id].get("duration_share") or 0.0),
        )
    role_counts = Counter(str(profile.get("role") or "unknown") for profile in profiles.values())
    methods = Counter(str(segment.get("alignment_method") or "") for segment in segments)
    force_dsp_segments = [
        segment for segment in segments if segment.get("alignment_method") == "force_dsp"
    ]
    pre_tts_segments = [
        segment for segment in segments if segment.get("pre_tts_rewrite_direction")
    ]
    errors = _first_pass_errors(segments)
    title = str(metadata.get("video_title") or job_dir.name)
    url = str(metadata.get("url") or "")
    non_primary_share = round(
        sum(
            float(profile.get("duration_share") or 0.0)
            for speaker_id, profile in profiles.items()
            if speaker_id != primary_speaker_id
        ),
        4,
    )
    fragmented_count = role_counts.get("fragmented", 0)
    incidental_count = role_counts.get("incidental", 0)
    primary_share = float(profiles.get(primary_speaker_id, {}).get("duration_share") or 0.0)
    risk_level = "low"
    risk_reasons: list[str] = []
    if primary_share >= 0.75 and fragmented_count >= 2:
        risk_level = "high"
        risk_reasons.append("dominant_primary_with_multiple_fragmented_speakers")
    if methods.get("force_dsp", 0) >= max(8, len(segments) * 0.15):
        risk_level = "high"
        risk_reasons.append("high_force_dsp")
    if incidental_count:
        risk_reasons.append("incidental_speaker_detected")
    if role_counts.get("unknown", 0):
        risk_reasons.append("missing_speaker_structure_metadata")

    return {
        "job_id": job_dir.name,
        "project_dir": job_dir.as_posix(),
        "mtime": job_dir.stat().st_mtime,
        "source_key": _source_key(url, title),
        "title": title,
        "url_present": bool(url),
        "segment_count": len(segments),
        "speaker_count": len(profiles),
        "primary_speaker_id": primary_speaker_id,
        "primary_share": round(primary_share, 4),
        "non_primary_share": non_primary_share,
        "speaker_role_distribution": dict(role_counts),
        "speakers": profiles,
        "risk_level": risk_level,
        "risk_reasons": risk_reasons,
        "rewrite_count": sum(int(segment.get("rewrite_count") or 0) for segment in segments),
        "pre_tts_rewrite_count": len(pre_tts_segments),
        "pre_tts_contradiction_count": sum(
            1 for segment in pre_tts_segments if segment.get("pre_tts_contradiction")
        ),
        "first_pass_abs_error_avg": round(sum(errors) / len(errors), 4) if errors else 0.0,
        "first_pass_abs_error_p50": round(_percentile(errors, 0.5), 4),
        "first_pass_abs_error_p90": round(_percentile(errors, 0.9), 4),
        "alignment_method_distribution": dict(methods),
        "force_dsp_count": len(force_dsp_segments),
        "short_force_dsp_count": sum(
            1
            for segment in force_dsp_segments
            if 2_000 <= int(segment.get("target_duration_ms") or 0) < 8_000
        ),
        "needs_review_count": sum(1 for segment in segments if segment.get("needs_review")),
        "force_dsp_review_suppressed_count": sum(
            1 for segment in segments if segment.get("force_dsp_review_suppressed")
        ),
        "short_merge_applied_count": sum(
            1 for segment in segments if segment.get("short_merge_applied")
        ),
        "short_merge_blocked_cross_speaker_count": sum(
            1
            for segment in segments
            if segment.get("short_merge_blocked_reason") == "cross_speaker_adjacent"
        ),
        "verifier": _verifier_summary(job_dir),
        "samples": _sample_segments(
            segments,
            primary_speaker_id=primary_speaker_id,
            limit=sample_segments_per_speaker,
        ),
    }


def _duplicate_groups(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for job in jobs:
        grouped[str(job["source_key"])].append(job)
    groups: list[dict[str, Any]] = []
    for source_key, items in grouped.items():
        if len(items) < 2:
            continue
        ordered = sorted(items, key=lambda item: float(item.get("mtime") or 0))
        best_by_cost = min(
            ordered,
            key=lambda item: (
                int(item.get("force_dsp_count") or 0)
                + int(item.get("needs_review_count") or 0)
                + int(item.get("pre_tts_rewrite_count") or 0),
                int(item.get("rewrite_count") or 0),
            ),
        )
        best_by_speaker = min(
            ordered,
            key=lambda item: (
                int(item.get("speaker_role_distribution", {}).get("fragmented", 0)),
                -int(item.get("speaker_role_distribution", {}).get("incidental", 0)),
                int(item.get("speaker_count") or 0),
            ),
        )
        groups.append(
            {
                "source_key": source_key,
                "title": ordered[-1].get("title"),
                "jobs": [item["job_id"] for item in ordered],
                "job_summaries": [_job_snapshot(item) for item in ordered],
                "best_by_cost": best_by_cost["job_id"],
                "best_by_speaker": best_by_speaker["job_id"],
            }
        )
    groups.sort(key=lambda group: len(group["jobs"]), reverse=True)
    return groups


def _job_snapshot(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job.get("job_id"),
        "speaker_count": job.get("speaker_count"),
        "primary_share": job.get("primary_share"),
        "speaker_role_distribution": job.get("speaker_role_distribution") or {},
        "force_dsp_count": job.get("force_dsp_count"),
        "needs_review_count": job.get("needs_review_count"),
        "rewrite_count": job.get("rewrite_count"),
        "pre_tts_rewrite_count": job.get("pre_tts_rewrite_count"),
        "risk_level": job.get("risk_level"),
        "risk_reasons": job.get("risk_reasons") or [],
    }


def build_report(config: ReportConfig) -> dict[str, Any]:
    jobs = [
        summarize_job(
            job_dir,
            sample_segments_per_speaker=config.sample_segments_per_speaker,
        )
        for job_dir in _job_dirs(config.projects_root, config.max_jobs)
    ]
    jobs.sort(key=lambda item: float(item.get("mtime") or 0), reverse=True)
    high_risk_jobs = [
        job for job in jobs if job.get("risk_level") == "high"
    ]
    duplicate_groups = _duplicate_groups(jobs)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "projects_root": config.projects_root.as_posix(),
        "job_count": len(jobs),
        "high_risk_job_count": len(high_risk_jobs),
        "duplicate_group_count": len(duplicate_groups),
        "summary": {
            "jobs": len(jobs),
            "high_risk_jobs": len(high_risk_jobs),
            "duplicate_groups": len(duplicate_groups),
        },
        "duplicate_groups": duplicate_groups,
        "jobs": jobs,
    }


def _fmt_pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "0.0%"


def render_markdown(report: dict[str, Any]) -> str:
    jobs = report.get("jobs", [])
    duplicate_groups = report.get("duplicate_groups", [])
    high_risk = [job for job in jobs if job.get("risk_level") == "high"]
    lines = [
        "# P2 Speaker Attribution Convergence Report",
        "",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Projects root: `{report.get('projects_root')}`",
        f"- Jobs scanned: `{report.get('job_count')}`",
        f"- High-risk jobs: `{report.get('high_risk_job_count')}`",
        f"- Duplicate source groups: `{len(duplicate_groups)}`",
        "",
        "## Current Read",
        "",
        "- Do not add phrase-cue or single-video rules. Recent failures are mixed structural cases: presenter, host, audience, music, and guest segments can all coexist in one source.",
        "- Deterministic profiling is useful for observation and UI hints, but should not auto-merge all low-share speakers in multi-speaker videos.",
        "- P2-b should stay local-verifier based: only high-risk low-support candidates are allowed to change speaker assignment, and uncertain decisions should keep the existing assignment.",
        "",
        "## Duplicate Runs",
        "",
    ]
    if not duplicate_groups:
        lines.append("No duplicate source groups were found in this scan.")
    else:
        lines.extend(
            [
                "| Source | Runs | Best By Cost | Best By Speaker |",
                "| --- | ---: | --- | --- |",
            ]
        )
        for group in duplicate_groups:
            lines.append(
                "| `{source}` | {runs} | `{cost}` | `{speaker}` |".format(
                    source=group.get("source_key"),
                    runs=len(group.get("jobs", [])),
                    cost=group.get("best_by_cost"),
                    speaker=group.get("best_by_speaker"),
                )
            )
    lines.extend(["", "## Job Summary", ""])
    lines.extend(
        [
            "| Job | Title | Speakers | Primary | Roles | Force DSP | Needs Review | Rewrite | Risk |",
            "| --- | --- | ---: | ---: | --- | ---: | ---: | ---: | --- |",
        ]
    )
    for job in jobs:
        roles = ", ".join(
            f"{key}:{value}"
            for key, value in sorted((job.get("speaker_role_distribution") or {}).items())
        )
        title = str(job.get("title") or "").replace("|", "\\|")[:48]
        lines.append(
            "| `{job_id}` | {title} | {speakers} | {primary} | {roles} | {force} | {needs} | {rewrite} | {risk} |".format(
                job_id=job.get("job_id"),
                title=title,
                speakers=job.get("speaker_count"),
                primary=_fmt_pct(job.get("primary_share")),
                roles=roles,
                force=job.get("force_dsp_count"),
                needs=job.get("needs_review_count"),
                rewrite=job.get("rewrite_count"),
                risk=job.get("risk_level"),
            )
        )
    lines.extend(["", "## High-Risk Speaker Details", ""])
    if not high_risk:
        lines.append("No high-risk speaker fragmentation jobs were found.")
    for job in high_risk[:10]:
        lines.append(f"### `{job.get('job_id')}`")
        lines.append("")
        lines.append(
            "- Title: {title}".format(title=str(job.get("title") or "")[:100])
        )
        lines.append(
            "- Reasons: {reasons}".format(
                reasons=", ".join(job.get("risk_reasons") or []) or "n/a"
            )
        )
        lines.append(
            "- Metrics: force_dsp={force}, needs_review={needs}, rewrite={rewrite}, pre_tts={pre}".format(
                force=job.get("force_dsp_count"),
                needs=job.get("needs_review_count"),
                rewrite=job.get("rewrite_count"),
                pre=job.get("pre_tts_rewrite_count"),
            )
        )
        lines.append("")
        lines.append("| Speaker | Name | Role | Share | Segments | Short Rate | Reason |")
        lines.append("| --- | --- | --- | ---: | ---: | ---: | --- |")
        for speaker_id, profile in sorted((job.get("speakers") or {}).items()):
            name = str(profile.get("display_name") or "").replace("|", "\\|")[:36]
            lines.append(
                "| `{sid}` | {name} | {role} | {share} | {segments} | {short_rate} | {reason} |".format(
                    sid=speaker_id,
                    name=name,
                    role=profile.get("role") or "",
                    share=_fmt_pct(profile.get("duration_share")),
                    segments=profile.get("segment_count"),
                    short_rate=_fmt_pct(profile.get("short_segment_rate")),
                    reason=profile.get("reason") or "",
                )
            )
        lines.append("")
        samples = job.get("samples") or {}
        if samples:
            lines.append("Sample non-primary segments:")
            for speaker_id, items in sorted(samples.items()):
                if not items:
                    continue
                lines.append(f"- `{speaker_id}`:")
                for item in items[:3]:
                    source = str(item.get("source_text") or "").replace("\n", " ")[:90]
                    lines.append(
                        "  - segment {segment_id}, {duration_s}s, {method}/{severity}: {source}".format(
                            segment_id=item.get("segment_id"),
                            duration_s=item.get("duration_s"),
                            method=item.get("method") or "n/a",
                            severity=item.get("severity") or "-",
                            source=source,
                        )
                    )
        lines.append("")
    lines.extend(
        [
            "## P2 Next Step",
            "",
            "1. Freeze these jobs as the first P2 speaker convergence set before changing more rules.",
            "2. Add a replay check that flags `dominant primary + multiple fragmented low-share speakers` without automatically merging them.",
            "3. Tighten P2-b verifier reporting: candidate count, decision distribution, and before/after speaker assignment must be visible per job.",
            "4. Only after the report shows stable false-positive patterns should we adjust verifier trigger thresholds. Do not add text phrase rules.",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(payload: dict[str, Any], config: ReportConfig) -> tuple[Path, Path]:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = config.output_dir / f"{config.output_stem}.json"
    md_path = config.output_dir / f"{config.output_stem}.md"
    if not config.force:
        for path in (json_path, md_path):
            if path.exists():
                raise FileExistsError(f"{path} exists; pass --force to overwrite")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    return json_path, md_path


def parse_args() -> ReportConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--projects-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--output-stem", default=DEFAULT_REPORT_STEM)
    parser.add_argument("--max-jobs", type=int, default=30)
    parser.add_argument("--sample-segments-per-speaker", type=int, default=3)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    return ReportConfig(
        projects_root=args.projects_root,
        output_dir=args.output_dir,
        output_stem=args.output_stem,
        max_jobs=args.max_jobs,
        sample_segments_per_speaker=args.sample_segments_per_speaker,
        force=args.force,
    )


def main() -> int:
    config = parse_args()
    payload = build_report(config)
    json_path, md_path = write_report(payload, config)
    print(
        json.dumps(
            {
                "json": json_path.as_posix(),
                "markdown": md_path.as_posix(),
                "jobs": payload["job_count"],
                "high_risk": payload["high_risk_job_count"],
                "duplicates": len(payload["duplicate_groups"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
