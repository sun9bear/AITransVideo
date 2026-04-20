"""T1-11 — subtitle sync contract.

Per plan §12: after an editing/commit, the publish stage's subtitle
generation reads ``editor/segments.json`` and picks up the edited
``cn_text`` automatically. No code change in ``editor_package_writer``
was required — this test file locks that assumption down.

The real publish flow needs a full pipeline spawn (ffmpeg / alignment /
etc.); we can't drive that in a unit test. Instead we assert the two
halves of the chain:

1. After ``commit_editing_pipeline(strategy='overwrite')``, the
   baseline ``editor/segments.json`` contains the user's cn_text edits
   AND voice_map overrides.
2. ``editor_package_writer._build_subtitle_slices`` (the function the
   publish stage calls) reads from ``AlignedSegment.cn_text`` directly —
   so any JSON we hand it will be reflected in the SRT output.

Together these guarantee subtitle content will update post-commit.
"""

from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass, field

import pytest

from tests.test_editing_commit import (  # reuse fixture
    _RecordingRunner,
    _build_editing_job_with_diff,
)
from services.jobs.editing_commit import commit_editing_pipeline


def test_commit_propagates_edited_cn_text_into_baseline_segments_json(tmp_path: Path) -> None:
    """Overwrite commit writes merged segments.json that contains every
    user edit. This is the source of truth that editor_package_writer
    reads during publish — which is how subtitles pick up new text."""
    store, record, project_dir = _build_editing_job_with_diff(
        tmp_path,
        text_edits={
            "seg_001": "用户改过的第一段",
            "seg_003": "用户改过的第三段",
        },
        voice_map={"seg_002": {"provider": "cosyvoice", "voice_id": "cv_new"}},
    )
    runner = _RecordingRunner()

    commit_editing_pipeline(record, store, runner, strategy="overwrite")

    baseline = json.loads(
        (project_dir / "editor" / "segments.json").read_text(encoding="utf-8")
    )
    assert baseline[0]["cn_text"] == "用户改过的第一段"
    assert baseline[2]["cn_text"] == "用户改过的第三段"
    # seg_002 untouched in cn_text, but voice_id overridden
    assert baseline[1]["cn_text"] == "text2"
    assert baseline[1]["voice_id"] == "cv_new"
    # voice_map override writes to the canonical tts_provider field
    # (ultrareview #2 fix — was drifted 'provider' before).
    assert baseline[1]["tts_provider"] == "cosyvoice"


def test_editor_package_writer_reads_segment_cn_text_field() -> None:
    """Sanity guard: editor_package_writer's subtitle builder reads
    ``segment.cn_text`` — so changes to segments.json propagate into SRT
    output. If this line ever moves, subtitle sync breaks."""
    writer_source = (
        Path(__file__).resolve().parents[1]
        / "src" / "modules" / "output" / "editor" / "editor_package_writer.py"
    ).read_text(encoding="utf-8")
    assert "segment.cn_text" in writer_source, (
        "editor_package_writer must read cn_text from AlignedSegment — the "
        "plan's subtitle-sync-is-automatic assumption depends on this. "
        "See docs/plans/2026-04-18-studio-post-edit-plan.md §12."
    )


def test_copy_as_new_propagates_edits_to_copy_segments_json(tmp_path: Path) -> None:
    """Same guarantee for copy_as_new: the new project_dir's
    segments.json reflects editing edits, so when the runner drives
    publish on the copy, the subtitle will be correct."""
    store, record, project_dir = _build_editing_job_with_diff(
        tmp_path,
        text_edits={"seg_001": "副本第一段新文本"},
    )
    runner = _RecordingRunner()

    result = commit_editing_pipeline(
        record, store, runner,
        strategy="copy_as_new", copy_display_name="副本",
        new_job_id_factory=lambda: "job_copy_sub",
    )

    new_dir = Path(result["new_project_dir"])
    new_baseline = json.loads(
        (new_dir / "editor" / "segments.json").read_text(encoding="utf-8")
    )
    assert new_baseline[0]["cn_text"] == "副本第一段新文本"
    # Source baseline UNCHANGED — copy_as_new leaves source untouched
    source_baseline = json.loads(
        (project_dir / "editor" / "segments.json").read_text(encoding="utf-8")
    )
    assert source_baseline[0]["cn_text"] == "text1"
