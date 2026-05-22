"""Advisory subtitle quality checks.

These helpers are report-only. They do not split text, retime cues, or mark
pipeline output as failed.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from modules.subtitles.cue_models import SubtitleCue
from utils.text_width import display_width

SUBTITLE_WIDTH_REPORT_SCHEMA_VERSION = "subtitle_width_report_v1"


@dataclass(frozen=True, slots=True)
class SubtitleWidthIssue:
    cue_index: int
    cue_id: str
    block_id: str
    text: str
    width_units: int
    max_units: int
    severity: str
    jianying_font_size_used: int | None = None
    advisory_only: bool = True


def find_subtitle_width_issues(
    cues: list[SubtitleCue],
    *,
    max_display_width: int = 32,
) -> list[SubtitleWidthIssue]:
    issues: list[SubtitleWidthIssue] = []
    for index, cue in enumerate(cues):
        width = display_width(cue.text)
        if width > max_display_width:
            issues.append(
                SubtitleWidthIssue(
                    cue_index=index,
                    cue_id=cue.cue_id,
                    block_id=cue.block_id,
                    text=cue.text,
                    width_units=width,
                    max_units=max_display_width,
                    severity="warning",
                    jianying_font_size_used=None,
                    advisory_only=True,
                )
            )
    return issues


def build_subtitle_width_report(
    *,
    project_id: str,
    cues: list[SubtitleCue],
    max_display_width: int = 32,
) -> dict[str, object]:
    issues = find_subtitle_width_issues(cues, max_display_width=max_display_width)
    return {
        "schema_version": SUBTITLE_WIDTH_REPORT_SCHEMA_VERSION,
        "project_id": project_id,
        "advisory_only": True,
        "max_display_width": max_display_width,
        "issue_count": len(issues),
        "issues": [asdict(issue) for issue in issues],
    }


def write_subtitle_width_report(
    path: Path,
    *,
    project_id: str,
    cues: list[SubtitleCue],
    max_display_width: int = 32,
) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = build_subtitle_width_report(
            project_id=project_id,
            cues=cues,
            max_display_width=max_display_width,
        )
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return True
    except OSError:
        return False


__all__ = [
    "SUBTITLE_WIDTH_REPORT_SCHEMA_VERSION",
    "SubtitleWidthIssue",
    "build_subtitle_width_report",
    "find_subtitle_width_issues",
    "write_subtitle_width_report",
]
