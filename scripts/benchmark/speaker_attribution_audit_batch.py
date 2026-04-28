#!/usr/bin/env python3
"""Build a P2-b speaker attribution audit batch from convergence reports.

This script does not change production output. It turns the P2 convergence
report into a bounded set of local audio-review candidates, optionally writing
short audio clips for a later model adjudication pass.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.benchmark.speaker_attribution_report import (
    _duration_ms,
    _read_json,
    _speaker_profiles,
)


DEFAULT_OUTPUT_DIR = Path("reports/benchmark")
DEFAULT_OUTPUT_STEM = "speaker_attribution_audit_batch"
DEFAULT_PADDING_MS = 8_000
DEFAULT_MAX_CANDIDATES_PER_JOB = 6


@dataclass(frozen=True)
class AuditConfig:
    report_path: Path
    projects_root: Path | None = None
    output_dir: Path = DEFAULT_OUTPUT_DIR
    output_stem: str = DEFAULT_OUTPUT_STEM
    max_candidates_per_job: int = DEFAULT_MAX_CANDIDATES_PER_JOB
    clip_padding_ms: int = DEFAULT_PADDING_MS
    write_clips: bool = False
    force: bool = False


def _load_segments(job_dir: Path) -> list[dict[str, Any]]:
    payload = _read_json(job_dir / "translation" / "segments.json")
    segments = payload.get("segments")
    return segments if isinstance(segments, list) else []


def _load_transcript_lines(job_dir: Path) -> dict[int, dict[str, Any]]:
    payload = _read_json(job_dir / "transcript" / "transcript.json")
    lines = payload.get("lines")
    if not isinstance(lines, list):
        return {}
    result: dict[int, dict[str, Any]] = {}
    for line in lines:
        if not isinstance(line, dict):
            continue
        try:
            result[int(line.get("index"))] = line
        except (TypeError, ValueError):
            continue
    return result


def _speaker_label(profile: dict[str, Any]) -> str:
    return " ".join(
        str(profile.get(key) or "")
        for key in ("display_name", "role", "role_label", "reason", "review_hint")
    ).lower()


def _looks_low_support_or_non_speech(profile: dict[str, Any]) -> bool:
    text = _speaker_label(profile)
    tokens = (
        "audience",
        "listener",
        "unknown",
        "unidentified",
        "music",
        "background",
        "non-speech",
        "观众",
        "听众",
        "未知",
        "背景音乐",
        "音乐",
        "现场",
        "互动",
    )
    return any(token in text for token in tokens)


def _audio_path(job_dir: Path) -> Path | None:
    for rel in (
        "audio/original.wav",
        "audio/speech_for_asr.wav",
        "audio/.review_tmp/review_audio.ogg",
    ):
        candidate = job_dir / rel
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _resolve_job_dir(job: dict[str, Any], projects_root: Path | None) -> Path:
    report_path = Path(str(job.get("project_dir") or ""))
    if report_path.exists():
        return report_path
    if projects_root is not None:
        direct = projects_root / str(job.get("job_id"))
        if direct.exists():
            return direct
        matches = list(projects_root.rglob(str(job.get("job_id"))))
        for match in matches:
            if match.is_dir():
                return match
    return report_path


def _duplicate_job_ids(report: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for group in report.get("duplicate_groups") or []:
        jobs = group.get("jobs")
        if isinstance(jobs, list):
            result.update(str(job_id) for job_id in jobs)
    return result


def _duplicate_group_by_job(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for group in report.get("duplicate_groups") or []:
        jobs = group.get("jobs")
        if not isinstance(jobs, list):
            continue
        for job_id in jobs:
            result[str(job_id)] = group
    return result


def _verifier_candidates(job_dir: Path) -> dict[int, dict[str, Any]]:
    payload = _read_json(job_dir / "transcript" / "s2_pass1_result.json")
    verifier = payload.get("speaker_verifier")
    if not isinstance(verifier, dict):
        return {}
    candidates = verifier.get("candidates")
    if not isinstance(candidates, list):
        return {}
    result: dict[int, dict[str, Any]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        try:
            result[int(candidate.get("line_index"))] = candidate
        except (TypeError, ValueError):
            continue
    return result


def _segment_index(segment: dict[str, Any]) -> int:
    try:
        return int(segment.get("segment_id") or segment.get("index") or 0)
    except (TypeError, ValueError):
        return 0


def _context_segments(
    segments: list[dict[str, Any]],
    position: int,
) -> list[dict[str, Any]]:
    start = max(0, position - 2)
    end = min(len(segments), position + 3)
    context: list[dict[str, Any]] = []
    for item in segments[start:end]:
        context.append(
            {
                "segment_id": item.get("segment_id"),
                "speaker_id": item.get("speaker_id"),
                "display_name": item.get("display_name"),
                "start_ms": item.get("start_ms"),
                "end_ms": item.get("end_ms"),
                "source_text": str(item.get("source_text") or "")[:260],
            }
        )
    return context


def _candidate_reasons(
    *,
    segment: dict[str, Any],
    profile: dict[str, Any],
    primary_speaker_id: str,
    original_line: dict[str, Any] | None,
    verifier_candidate: dict[str, Any] | None,
    duplicate_group: dict[str, Any] | None,
) -> tuple[list[str], int]:
    reasons: list[str] = []
    score = 0
    speaker_id = str(segment.get("speaker_id") or "")
    role = str(profile.get("role") or "")
    duration_ms = _duration_ms(segment)
    speaker_share = float(profile.get("duration_share") or 0.0)
    speaker_segment_count = int(profile.get("segment_count") or 0)

    if speaker_id != primary_speaker_id:
        reasons.append("non_primary_speaker")
        score += 12
    if role in {"fragmented", "incidental"}:
        reasons.append(f"{role}_speaker")
        score += 28
    if speaker_share and speaker_share <= 0.05:
        reasons.append("low_duration_share")
        score += 22
    if speaker_segment_count and speaker_segment_count <= 3:
        reasons.append("low_segment_count")
        score += 12
    if _looks_low_support_or_non_speech(profile):
        reasons.append("audience_unknown_or_non_speech_profile")
        score += 20
    if duration_ms <= 2_000:
        reasons.append("short_interaction")
        score += 12
    if duration_ms >= 8_000 and (
        role in {"fragmented", "incidental"}
        or speaker_share <= 0.05
        or _looks_low_support_or_non_speech(profile)
    ):
        reasons.append("long_low_support_segment")
        score += 20
    if segment.get("alignment_method") == "force_dsp":
        reasons.append("force_dsp")
        score += 14
    if segment.get("needs_review"):
        reasons.append("needs_review")
        score += 10
    if duplicate_group is not None:
        reasons.append("duplicate_source_group")
        score += 8

    original_speaker_id = ""
    if original_line is not None:
        original_speaker_id = str(original_line.get("speaker_id") or "")
    if original_speaker_id and original_speaker_id != speaker_id:
        reasons.append("asr_s2_speaker_changed")
        score += 35
    if verifier_candidate is not None:
        reasons.append("existing_verifier_candidate")
        score += 50

    return reasons, score


def _is_candidate(reasons: list[str]) -> bool:
    if "existing_verifier_candidate" in reasons:
        return True
    if "asr_s2_speaker_changed" in reasons and (
        "force_dsp" in reasons
        or "needs_review" in reasons
        or "duplicate_source_group" in reasons
        or "audience_unknown_or_non_speech_profile" in reasons
    ):
        return True
    if "non_primary_speaker" not in reasons:
        return False
    strong = {
        "fragmented_speaker",
        "incidental_speaker",
        "low_duration_share",
        "audience_unknown_or_non_speech_profile",
        "long_low_support_segment",
        "short_interaction",
    }
    return bool(strong.intersection(reasons))


def _select_job_candidates(
    *,
    job: dict[str, Any],
    job_dir: Path,
    duplicate_group: dict[str, Any] | None,
    max_candidates: int,
) -> list[dict[str, Any]]:
    segments = _load_segments(job_dir)
    if not segments:
        return []
    profiles = job.get("speakers")
    if not isinstance(profiles, dict) or not profiles:
        profiles = _speaker_profiles(segments)
    primary_speaker_id = str(job.get("primary_speaker_id") or "")
    if not primary_speaker_id and profiles:
        primary_speaker_id = max(
            profiles,
            key=lambda speaker_id: float(profiles[speaker_id].get("duration_share") or 0.0),
        )
    transcript_lines = _load_transcript_lines(job_dir)
    verifier_by_line = _verifier_candidates(job_dir)
    audio_path = _audio_path(job_dir)

    candidates: list[dict[str, Any]] = []
    for position, segment in enumerate(segments):
        speaker_id = str(segment.get("speaker_id") or "")
        if not speaker_id:
            continue
        profile = profiles.get(speaker_id, {}) if isinstance(profiles, dict) else {}
        segment_index = _segment_index(segment)
        original_line = transcript_lines.get(segment_index)
        verifier_candidate = verifier_by_line.get(segment_index)
        reasons, score = _candidate_reasons(
            segment=segment,
            profile=profile,
            primary_speaker_id=primary_speaker_id,
            original_line=original_line,
            verifier_candidate=verifier_candidate,
            duplicate_group=duplicate_group,
        )
        if not _is_candidate(reasons):
            continue
        start_ms = int(segment.get("start_ms") or 0)
        end_ms = int(segment.get("end_ms") or 0)
        candidate = {
            "candidate_id": "",
            "job_id": job.get("job_id"),
            "job_title": job.get("title"),
            "project_dir": job_dir.as_posix(),
            "source_key": job.get("source_key"),
            "duplicate_group": {
                "source_key": duplicate_group.get("source_key"),
                "jobs": duplicate_group.get("jobs"),
                "best_by_cost": duplicate_group.get("best_by_cost"),
                "best_by_speaker": duplicate_group.get("best_by_speaker"),
            }
            if duplicate_group
            else None,
            "segment_id": segment.get("segment_id"),
            "line_index": segment_index,
            "position": position,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "duration_ms": max(0, end_ms - start_ms),
            "assigned_speaker_id": speaker_id,
            "assigned_display_name": segment.get("display_name") or profile.get("display_name"),
            "primary_speaker_id": primary_speaker_id,
            "original_asr_speaker_id": str(original_line.get("speaker_id") or "")
            if original_line
            else "",
            "speaker_profile": profile,
            "alignment_method": segment.get("alignment_method") or "",
            "force_dsp_severity": segment.get("force_dsp_severity") or "",
            "needs_review": bool(segment.get("needs_review")),
            "source_text": str(segment.get("source_text") or "")[:600],
            "cn_text": str(segment.get("cn_text") or "")[:400],
            "context": _context_segments(segments, position),
            "reasons": reasons,
            "priority_score": score,
            "audio_source_path": audio_path.as_posix() if audio_path else "",
            "clip_path": "",
            "clip_error": "" if audio_path else "missing_audio",
            "model_task": {
                "decision_options": [
                    "asr_speaker",
                    "s2_speaker",
                    "main_speaker",
                    "distinct_speaker",
                    "overlap",
                    "music_or_non_speech",
                    "uncertain",
                ],
                "recommended_action_options": [
                    "keep",
                    "revert_to_asr",
                    "reassign_to_main",
                    "mark_review",
                    "mark_non_speech",
                ],
            },
        }
        candidates.append(candidate)

    candidates.sort(
        key=lambda item: (
            int(item.get("priority_score") or 0),
            -int(item.get("duration_ms") or 0),
        ),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []
    seen_speakers: set[str] = set()
    for candidate in candidates:
        speaker_id = str(candidate.get("assigned_speaker_id") or "")
        if speaker_id in seen_speakers and len(selected) < max_candidates // 2:
            continue
        selected.append(candidate)
        seen_speakers.add(speaker_id)
        if len(selected) >= max_candidates:
            break
    if len(selected) < max_candidates:
        selected_ids = {id(item) for item in selected}
        for candidate in candidates:
            if id(candidate) in selected_ids:
                continue
            selected.append(candidate)
            if len(selected) >= max_candidates:
                break
    for idx, candidate in enumerate(selected, start=1):
        candidate["candidate_id"] = f"{candidate['job_id']}_cand_{idx:03d}"
    return selected


def _write_clip(
    *,
    candidate: dict[str, Any],
    output_dir: Path,
    padding_ms: int,
) -> None:
    audio_source = Path(str(candidate.get("audio_source_path") or ""))
    if not audio_source.exists():
        candidate["clip_error"] = "missing_audio"
        return
    job_id = str(candidate["job_id"])
    clip_dir = output_dir / "clips" / job_id
    clip_dir.mkdir(parents=True, exist_ok=True)
    clip_path = clip_dir / f"{candidate['candidate_id']}.ogg"
    start_ms = max(0, int(candidate["start_ms"]) - padding_ms)
    end_ms = int(candidate["end_ms"]) + padding_ms
    duration_ms = max(1, end_ms - start_ms)
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{start_ms / 1000:.3f}",
        "-i",
        str(audio_source),
        "-t",
        f"{duration_ms / 1000:.3f}",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "libopus",
        "-b:a",
        "24k",
        str(clip_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=180)
    except FileNotFoundError:
        candidate["clip_error"] = "ffmpeg_not_found"
        return
    except Exception as exc:
        candidate["clip_error"] = f"ffmpeg_failed:{type(exc).__name__}"
        return
    if not clip_path.exists() or clip_path.stat().st_size == 0:
        candidate["clip_error"] = "empty_clip"
        return
    candidate["clip_path"] = clip_path.as_posix()
    candidate["clip_start_ms"] = start_ms
    candidate["clip_end_ms"] = end_ms
    candidate["clip_duration_ms"] = duration_ms
    candidate["clip_size_bytes"] = clip_path.stat().st_size
    candidate["clip_error"] = ""


def build_audit_batch(config: AuditConfig) -> dict[str, Any]:
    report = _read_json(config.report_path)
    jobs = report.get("jobs")
    if not isinstance(jobs, list):
        jobs = []
    duplicate_ids = _duplicate_job_ids(report)
    duplicate_by_job = _duplicate_group_by_job(report)
    target_jobs = [
        job for job in jobs
        if job.get("risk_level") == "high" or str(job.get("job_id")) in duplicate_ids
    ]

    all_candidates: list[dict[str, Any]] = []
    missing_job_dirs: list[str] = []
    for job in target_jobs:
        job_dir = _resolve_job_dir(job, config.projects_root)
        if not job_dir.exists():
            missing_job_dirs.append(str(job.get("job_id")))
            continue
        all_candidates.extend(
            _select_job_candidates(
                job=job,
                job_dir=job_dir,
                duplicate_group=duplicate_by_job.get(str(job.get("job_id"))),
                max_candidates=config.max_candidates_per_job,
            )
        )

    if config.write_clips:
        for candidate in all_candidates:
            _write_clip(
                candidate=candidate,
                output_dir=config.output_dir,
                padding_ms=config.clip_padding_ms,
            )

    reason_counts = Counter(
        reason for candidate in all_candidates for reason in candidate.get("reasons", [])
    )
    job_counts = Counter(str(candidate.get("job_id")) for candidate in all_candidates)
    total_clip_ms = sum(int(candidate.get("clip_duration_ms") or 0) for candidate in all_candidates)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_report": config.report_path.as_posix(),
        "projects_root": config.projects_root.as_posix() if config.projects_root else "",
        "summary": {
            "source_jobs": len(jobs),
            "target_jobs": len(target_jobs),
            "missing_job_dirs": len(missing_job_dirs),
            "candidates": len(all_candidates),
            "jobs_with_candidates": len(job_counts),
            "clips_written": sum(1 for item in all_candidates if item.get("clip_path")),
            "clip_audio_seconds": round(total_clip_ms / 1000, 1),
        },
        "missing_job_dirs": missing_job_dirs,
        "reason_counts": dict(reason_counts.most_common()),
        "candidate_count_by_job": dict(job_counts.most_common()),
        "candidates": all_candidates,
    }


def render_markdown(batch: dict[str, Any]) -> str:
    summary = batch.get("summary") or {}
    lines = [
        "# P2-b Speaker Attribution Audit Batch",
        "",
        f"- Generated at: `{batch.get('generated_at')}`",
        f"- Source report: `{batch.get('source_report')}`",
        f"- Target jobs: `{summary.get('target_jobs')}`",
        f"- Candidates: `{summary.get('candidates')}`",
        f"- Clips written: `{summary.get('clips_written')}`",
        f"- Clip audio seconds: `{summary.get('clip_audio_seconds')}`",
        "",
        "## Reason Counts",
        "",
        "| Reason | Count |",
        "| --- | ---: |",
    ]
    for reason, count in (batch.get("reason_counts") or {}).items():
        lines.append(f"| `{reason}` | {count} |")
    lines.extend(["", "## Candidates By Job", ""])
    by_job: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in batch.get("candidates") or []:
        by_job[str(candidate.get("job_id"))].append(candidate)
    for job_id, candidates in sorted(by_job.items(), key=lambda item: item[0]):
        title = str(candidates[0].get("job_title") or "")[:100]
        lines.append(f"### `{job_id}`")
        lines.append("")
        lines.append(f"- Title: {title}")
        lines.append("")
        lines.append("| Candidate | Segment | Speaker | Duration | Score | Reasons | Clip |")
        lines.append("| --- | ---: | --- | ---: | ---: | --- | --- |")
        for candidate in candidates:
            reasons = ", ".join(candidate.get("reasons") or [])
            clip = "yes" if candidate.get("clip_path") else (candidate.get("clip_error") or "no")
            speaker = "{sid} / {name}".format(
                sid=candidate.get("assigned_speaker_id"),
                name=str(candidate.get("assigned_display_name") or "")[:24],
            ).replace("|", "\\|")
            lines.append(
                "| `{cid}` | {seg} | {speaker} | {dur:.1f}s | {score} | {reasons} | {clip} |".format(
                    cid=candidate.get("candidate_id"),
                    seg=candidate.get("segment_id"),
                    speaker=speaker,
                    dur=int(candidate.get("duration_ms") or 0) / 1000,
                    score=candidate.get("priority_score"),
                    reasons=reasons,
                    clip=clip,
                )
            )
        lines.append("")
    lines.extend(
        [
            "## Model Adjudication Contract",
            "",
            "Each candidate is intended for a local audio model judge. The model should use the clip first, then the structured context.",
            "",
            "Allowed `decision` values: `asr_speaker`, `s2_speaker`, `main_speaker`, `distinct_speaker`, `overlap`, `music_or_non_speech`, `uncertain`.",
            "",
            "Allowed `recommended_action` values: `keep`, `revert_to_asr`, `reassign_to_main`, `mark_review`, `mark_non_speech`.",
            "",
        ]
    )
    return "\n".join(lines)


def write_outputs(batch: dict[str, Any], config: AuditConfig) -> tuple[Path, Path, Path]:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = config.output_dir / f"{config.output_stem}.json"
    md_path = config.output_dir / f"{config.output_stem}.md"
    jsonl_path = config.output_dir / f"{config.output_stem}_model_inputs.jsonl"
    if not config.force:
        for path in (json_path, md_path, jsonl_path):
            if path.exists():
                raise FileExistsError(f"{path} exists; pass --force to overwrite")
    json_path.write_text(json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(batch), encoding="utf-8")
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for candidate in batch.get("candidates") or []:
            fh.write(json.dumps(candidate, ensure_ascii=False) + "\n")
    return json_path, md_path, jsonl_path


def parse_args() -> AuditConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--speaker-report", type=Path, required=True)
    parser.add_argument("--projects-root", type=Path)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--output-stem", default=DEFAULT_OUTPUT_STEM)
    parser.add_argument("--max-candidates-per-job", type=int, default=DEFAULT_MAX_CANDIDATES_PER_JOB)
    parser.add_argument("--clip-padding-ms", type=int, default=DEFAULT_PADDING_MS)
    parser.add_argument("--write-clips", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    return AuditConfig(
        report_path=args.speaker_report,
        projects_root=args.projects_root,
        output_dir=args.output_dir,
        output_stem=args.output_stem,
        max_candidates_per_job=max(1, args.max_candidates_per_job),
        clip_padding_ms=max(0, args.clip_padding_ms),
        write_clips=args.write_clips,
        force=args.force,
    )


def main() -> int:
    config = parse_args()
    batch = build_audit_batch(config)
    json_path, md_path, jsonl_path = write_outputs(batch, config)
    print(
        json.dumps(
            {
                "json": json_path.as_posix(),
                "markdown": md_path.as_posix(),
                "model_inputs": jsonl_path.as_posix(),
                **(batch.get("summary") or {}),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
