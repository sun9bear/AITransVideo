#!/usr/bin/env python3
"""Summarize P2 speaker attribution judge outputs.

This combines one audit batch with one or more model judgement JSON files.
It is intentionally offline-only: no model calls, no production writes.
"""

from __future__ import annotations

import argparse
import glob
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_OUTPUT_DIR = Path("reports/benchmark")
DEFAULT_OUTPUT_STEM = "speaker_attribution_judgement_summary"
KEEP_ACTION = "keep"


@dataclass(frozen=True)
class SummaryConfig:
    audit_batch: Path
    judgement_files: tuple[Path, ...]
    judgement_globs: tuple[str, ...] = ()
    output_dir: Path = DEFAULT_OUTPUT_DIR
    output_stem: str = DEFAULT_OUTPUT_STEM
    force: bool = False


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _candidate_map(audit_batch: Path) -> dict[str, dict[str, Any]]:
    payload = _read_json(audit_batch)
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        candidate_id = str(candidate.get("candidate_id") or "").strip()
        if candidate_id:
            result[candidate_id] = candidate
    return result


def _resolve_judgement_files(config: SummaryConfig) -> list[Path]:
    files = list(config.judgement_files)
    for pattern in config.judgement_globs:
        files.extend(Path(path) for path in glob.glob(pattern))
    unique: dict[str, Path] = {}
    for path in files:
        resolved = path.resolve(strict=False)
        unique[resolved.as_posix()] = path
    return [unique[key] for key in sorted(unique)]


def _iter_decisions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    decisions = payload.get("decisions")
    if isinstance(decisions, list):
        return [decision for decision in decisions if isinstance(decision, dict)]
    return []


def _job_id_from_candidate_id(candidate_id: str) -> str:
    marker = "_cand_"
    if marker not in candidate_id:
        return ""
    return candidate_id.split(marker, 1)[0]


def _decision_key(decision: dict[str, Any]) -> str:
    return str(decision.get("decision") or "unknown")


def _action_key(decision: dict[str, Any]) -> str:
    return str(decision.get("recommended_action") or "unknown")


def build_summary(config: SummaryConfig) -> dict[str, Any]:
    candidates_by_id = _candidate_map(config.audit_batch)
    judgement_files = _resolve_judgement_files(config)

    parts: list[dict[str, Any]] = []
    unique_decisions: dict[str, dict[str, Any]] = {}
    duplicate_decisions = 0
    unknown_candidate_decisions = 0
    errors: list[dict[str, Any]] = []

    for path in judgement_files:
        payload = _read_json(path)
        summary = payload.get("summary")
        part = {
            "file": path.as_posix(),
            "summary": summary if isinstance(summary, dict) else {},
        }
        parts.append(part)
        part_errors = payload.get("errors")
        if isinstance(part_errors, list):
            errors.extend(error for error in part_errors if isinstance(error, dict))
        for decision in _iter_decisions(payload):
            candidate_id = str(decision.get("candidate_id") or "").strip()
            if not candidate_id:
                continue
            if candidate_id not in candidates_by_id:
                unknown_candidate_decisions += 1
                continue
            if candidate_id in unique_decisions:
                duplicate_decisions += 1
                continue
            unique_decisions[candidate_id] = dict(decision)

    decision_counts = Counter(_decision_key(decision) for decision in unique_decisions.values())
    action_counts = Counter(_action_key(decision) for decision in unique_decisions.values())
    job_counts = Counter(
        _job_id_from_candidate_id(candidate_id)
        for candidate_id in unique_decisions
        if _job_id_from_candidate_id(candidate_id)
    )
    reason_action_counts: dict[str, Counter[str]] = defaultdict(Counter)
    reason_decision_counts: dict[str, Counter[str]] = defaultdict(Counter)
    non_keep: list[dict[str, Any]] = []
    for candidate_id, decision in unique_decisions.items():
        candidate = candidates_by_id[candidate_id]
        action = _action_key(decision)
        decision_value = _decision_key(decision)
        for reason in candidate.get("reasons") or []:
            reason_action_counts[str(reason)][action] += 1
            reason_decision_counts[str(reason)][decision_value] += 1
        if action != KEEP_ACTION:
            non_keep.append(
                {
                    "candidate_id": candidate_id,
                    "job_id": candidate.get("job_id") or _job_id_from_candidate_id(candidate_id),
                    "segment_id": candidate.get("segment_id"),
                    "assigned_speaker_id": candidate.get("assigned_speaker_id"),
                    "assigned_display_name": candidate.get("assigned_display_name"),
                    "duration_ms": candidate.get("duration_ms"),
                    "reasons": candidate.get("reasons") or [],
                    "decision": decision_value,
                    "confidence": decision.get("confidence"),
                    "recommended_action": action,
                    "reason": decision.get("reason"),
                }
            )
    non_keep.sort(
        key=lambda item: (
            str(item.get("job_id") or ""),
            int(item.get("segment_id") or 0),
            str(item.get("candidate_id") or ""),
        )
    )

    candidate_count = len(candidates_by_id)
    judged_count = len(unique_decisions)
    keep_count = action_counts.get(KEEP_ACTION, 0)
    non_keep_count = sum(count for action, count in action_counts.items() if action != KEEP_ACTION)
    main_speaker_count = decision_counts.get("main_speaker", 0)
    non_speech_count = decision_counts.get("music_or_non_speech", 0)
    coverage_pct = round(judged_count / candidate_count * 100, 1) if candidate_count else 0.0
    keep_pct = round(keep_count / judged_count * 100, 1) if judged_count else 0.0
    non_keep_pct = round(non_keep_count / judged_count * 100, 1) if judged_count else 0.0
    main_speaker_pct = round(main_speaker_count / judged_count * 100, 1) if judged_count else 0.0

    go_no_go = {
        "broad_low_support_auto_merge": {
            "decision": "NO_GO" if keep_pct >= 80.0 and main_speaker_pct <= 10.0 else "REVIEW",
            "reason": (
                f"{keep_pct}% of judged candidates were keep, and only "
                f"{main_speaker_pct}% were main_speaker."
            ),
        },
        "verifier_gated_main_reassignment": {
            "decision": "CAUTIOUS_GO" if main_speaker_count else "NO_GO",
            "reason": (
                "Allow only medium/high-confidence local audio verifier "
                f"main_speaker decisions; observed count={main_speaker_count}."
            ),
        },
        "non_speech_profile_marking": {
            "decision": "GO" if non_speech_count else "REVIEW",
            "reason": (
                "Use high-confidence music/non-speech decisions to mark complete "
                f"low-support non-dialogue speakers; observed count={non_speech_count}."
            ),
        },
        "phrase_or_title_specific_rules": {
            "decision": "NO_GO",
            "reason": "Judged failures span mixed presenter, host, audience, music, and guest cases.",
        },
    }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "audit_batch": config.audit_batch.as_posix(),
        "judgement_files": [path.as_posix() for path in judgement_files],
        "parts": parts,
        "summary": {
            "audit_candidates": candidate_count,
            "judged_unique_candidates": judged_count,
            "coverage_pct": coverage_pct,
            "decision_counts": dict(decision_counts.most_common()),
            "recommended_action_counts": dict(action_counts.most_common()),
            "jobs_covered": len(job_counts),
            "job_counts": dict(job_counts.most_common()),
            "non_keep_count": non_keep_count,
            "non_keep_pct": non_keep_pct,
            "duplicate_decisions_ignored": duplicate_decisions,
            "unknown_candidate_decisions": unknown_candidate_decisions,
            "judge_errors": len(errors),
        },
        "go_no_go": go_no_go,
        "reason_action_counts": {
            reason: dict(counter.most_common())
            for reason, counter in sorted(reason_action_counts.items())
        },
        "reason_decision_counts": {
            reason: dict(counter.most_common())
            for reason, counter in sorted(reason_decision_counts.items())
        },
        "non_keep_decisions": non_keep,
        "errors": errors,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "# P2 Speaker Attribution Judgement Summary",
        "",
        f"- Generated at: `{payload.get('generated_at')}`",
        f"- Audit batch: `{payload.get('audit_batch')}`",
        f"- Audit candidates: `{summary.get('audit_candidates')}`",
        f"- Judged unique candidates: `{summary.get('judged_unique_candidates')}`",
        f"- Coverage: `{summary.get('coverage_pct')}%`",
        f"- Jobs covered: `{summary.get('jobs_covered')}`",
        f"- Non-keep: `{summary.get('non_keep_count')}` (`{summary.get('non_keep_pct')}%`)",
        f"- Duplicate decisions ignored: `{summary.get('duplicate_decisions_ignored')}`",
        f"- Judge errors: `{summary.get('judge_errors')}`",
        "",
        "## Go / No-Go",
        "",
        "| Item | Decision | Reason |",
        "| --- | --- | --- |",
    ]
    for key, item in (payload.get("go_no_go") or {}).items():
        reason = str(item.get("reason") or "").replace("|", "\\|")
        lines.append(f"| `{key}` | `{item.get('decision')}` | {reason} |")

    lines.extend(["", "## Decision Counts", "", "| Decision | Count |", "| --- | ---: |"])
    for decision, count in (summary.get("decision_counts") or {}).items():
        lines.append(f"| `{decision}` | {count} |")

    lines.extend(["", "## Recommended Actions", "", "| Action | Count |", "| --- | ---: |"])
    for action, count in (summary.get("recommended_action_counts") or {}).items():
        lines.append(f"| `{action}` | {count} |")

    lines.extend(
        [
            "",
            "## Non-Keep Decisions",
            "",
            "| Candidate | Job | Segment | Assigned | Decision | Action | Confidence | Reason |",
            "| --- | --- | ---: | --- | --- | --- | --- | --- |",
        ]
    )
    for item in payload.get("non_keep_decisions") or []:
        reason = str(item.get("reason") or "").replace("|", "\\|")[:180]
        assigned = "{sid} / {name}".format(
            sid=item.get("assigned_speaker_id") or "",
            name=str(item.get("assigned_display_name") or "")[:28],
        ).replace("|", "\\|")
        lines.append(
            "| `{cid}` | `{job}` | {segment} | {assigned} | {decision} | {action} | {confidence} | {reason} |".format(
                cid=item.get("candidate_id"),
                job=item.get("job_id"),
                segment=item.get("segment_id"),
                assigned=assigned,
                decision=item.get("decision"),
                action=item.get("recommended_action"),
                confidence=item.get("confidence"),
                reason=reason,
            )
        )

    lines.extend(["", "## Reason To Action Counts", "", "| Reason | Actions |", "| --- | --- |"])
    for reason, counts in (payload.get("reason_action_counts") or {}).items():
        cells = ", ".join(f"{key}:{value}" for key, value in counts.items())
        lines.append(f"| `{reason}` | {cells} |")

    lines.extend(
        [
            "",
            "## Implications",
            "",
            "- Broad deterministic low-support speaker merging remains rejected by this sample.",
            "- Main-speaker reassignment should stay gated by local audio verifier decisions.",
            "- Non-speech/music/crowd handling is the clearest production path, but only for complete low-support speakers or explicit review flags.",
            "- Phrase, title, person-name, or fixed-line rules should not be added from this report.",
            "",
        ]
    )
    return "\n".join(lines)


def write_outputs(payload: dict[str, Any], config: SummaryConfig) -> tuple[Path, Path]:
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


def parse_args() -> SummaryConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-batch", type=Path, required=True)
    parser.add_argument("--judgement", type=Path, action="append", default=[])
    parser.add_argument("--judgement-glob", action="append", default=[])
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--output-stem", default=DEFAULT_OUTPUT_STEM)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    return SummaryConfig(
        audit_batch=args.audit_batch,
        judgement_files=tuple(args.judgement),
        judgement_globs=tuple(args.judgement_glob),
        output_dir=args.output_dir,
        output_stem=args.output_stem,
        force=args.force,
    )


def main() -> int:
    config = parse_args()
    payload = build_summary(config)
    json_path, md_path = write_outputs(payload, config)
    print(
        json.dumps(
            {
                "json": json_path.as_posix(),
                "markdown": md_path.as_posix(),
                **(payload.get("summary") or {}),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
