"""Tests for editor/tts_segments/ baseline backfill (lazy legacy migration).

Modern pipeline writes ``editor/tts_segments/{sid}.wav`` as part of publish
(S6 editor package). Tasks that completed BEFORE that wiring landed have
only legacy ``tts/segment_{sid:03d}_aligned.wav`` files.

On first ``enter_editing`` for a legacy task, a helper materialises
``editor/tts_segments/{sid}.wav`` from the legacy aligned wavs. After that,
copy_as_new's ``hardlink_baseline_audio`` finds the wavs normally, and γ
publish-only resume has the inputs it needs.

Contract pinned here:

- Every segment with a corresponding ``tts/segment_{sid:03d}_aligned.wav``
  gets a matching ``editor/tts_segments/{sid}.wav``.
- Idempotency: running twice is a no-op; existing wavs are not overwritten.
- Missing legacy wav for a specific segment is skipped (not raised) — the
  sibling behaviour in copy_service is "hardlink what's there", and γ's
  own hard guard will surface any remaining gaps on commit.
- ``tts/`` entirely absent → helper raises so callers can decide (e.g.
  enter_editing treats this as "task too old, cannot enter editing").
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from services.jobs.editor_tts_baseline import (
    EditorTtsBaselineError,
    ensure_editor_tts_segments_baseline,
)


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------


def _write_wav(path: Path, content: bytes = b"FAKE_WAV_BYTES") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _build_legacy_project(project_dir: Path, segment_ids: list[int]) -> None:
    """Create a minimal legacy-shaped project: tts/segment_NNN_aligned.wav
    for each id, editor/segments.json listing those ids."""
    tts_dir = project_dir / "tts"
    tts_dir.mkdir(parents=True, exist_ok=True)
    for sid in segment_ids:
        _write_wav(tts_dir / f"segment_{sid:03d}_aligned.wav",
                   f"aligned-{sid}".encode())
        # The pre-alignment raw wav also exists in real projects — include
        # one to prove the helper ignores it (we want the aligned variant).
        _write_wav(tts_dir / f"segment_{sid:03d}_speaker_a.wav",
                   f"raw-{sid}".encode())
    editor_dir = project_dir / "editor"
    editor_dir.mkdir(parents=True, exist_ok=True)
    import json
    editor_dir.joinpath("segments.json").write_text(
        json.dumps([
            {"segment_id": str(sid), "speaker_id": "speaker_a",
             "display_name": "A", "voice_id": "",
             "start_ms": sid * 1000, "end_ms": sid * 1000 + 900,
             "target_duration_ms": 900,
             "source_text": f"s{sid}", "cn_text": f"c{sid}"}
            for sid in segment_ids
        ]),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_backfill_copies_aligned_wavs_for_every_segment(tmp_path: Path) -> None:
    """Legacy task with 3 segments → 3 wavs materialised in editor/tts_segments/."""
    project = tmp_path / "project"
    _build_legacy_project(project, [1, 2, 3])

    result = ensure_editor_tts_segments_baseline(project)

    assert sorted(result["backfilled_segment_ids"]) == ["1", "2", "3"]
    for sid in (1, 2, 3):
        dst = project / "editor" / "tts_segments" / f"{sid}.wav"
        assert dst.is_file(), f"missing {dst}"
        # Content must come from the _aligned variant, not the raw TTS.
        assert dst.read_bytes() == f"aligned-{sid}".encode()


def test_backfill_uses_segment_id_without_zero_padding(tmp_path: Path) -> None:
    """Legacy file is segment_007_aligned.wav but the editing HTTP contract
    (input_validators regex + editor/segments.json) uses '7' not '007'.
    The destination filename must be the non-padded form."""
    project = tmp_path / "project"
    _build_legacy_project(project, [7])

    ensure_editor_tts_segments_baseline(project)

    assert (project / "editor" / "tts_segments" / "7.wav").is_file()
    assert not (project / "editor" / "tts_segments" / "007.wav").exists()


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------


def test_backfill_is_idempotent_when_baseline_already_populated(tmp_path: Path) -> None:
    """Running twice must not duplicate / overwrite / fail."""
    project = tmp_path / "project"
    _build_legacy_project(project, [1, 2])

    ensure_editor_tts_segments_baseline(project)
    # Simulate: someone replaced 1.wav with user-accepted-draft content.
    already_there = project / "editor" / "tts_segments" / "1.wav"
    already_there.write_bytes(b"USER_DRAFT_CONTENT")

    result = ensure_editor_tts_segments_baseline(project)

    # The second pass must NOT overwrite the existing wav.
    assert already_there.read_bytes() == b"USER_DRAFT_CONTENT"
    # Segment 2 was already backfilled in the first pass — second pass skips.
    assert result["skipped_existing_segment_ids"] == ["1", "2"]
    assert result["backfilled_segment_ids"] == []


def test_backfill_populates_only_missing_wavs(tmp_path: Path) -> None:
    """Hybrid state: some wavs already in editor/tts_segments/, rest still
    only in tts/. Helper fills the gaps, leaves existing alone."""
    project = tmp_path / "project"
    _build_legacy_project(project, [1, 2, 3])
    # Pre-populate segment 2 (e.g. from a prior partial migration or draft).
    editor_tts = project / "editor" / "tts_segments"
    editor_tts.mkdir(parents=True, exist_ok=True)
    (editor_tts / "2.wav").write_bytes(b"PRE_EXISTING")

    result = ensure_editor_tts_segments_baseline(project)

    assert sorted(result["backfilled_segment_ids"]) == ["1", "3"]
    assert result["skipped_existing_segment_ids"] == ["2"]
    assert (editor_tts / "2.wav").read_bytes() == b"PRE_EXISTING"


# ---------------------------------------------------------------------------
# partial legacy data
# ---------------------------------------------------------------------------


def test_backfill_skips_segments_missing_from_legacy_tts(tmp_path: Path) -> None:
    """If a specific segment's aligned wav isn't in tts/ (old split case,
    partial pipeline failure), skip it — don't raise. γ's pre-publish guard
    will surface the gap on commit with an actionable sid list."""
    project = tmp_path / "project"
    _build_legacy_project(project, [1, 2, 3])
    # Remove segment 2's aligned wav to simulate partial legacy data.
    (project / "tts" / "segment_002_aligned.wav").unlink()

    result = ensure_editor_tts_segments_baseline(project)

    assert "1" in result["backfilled_segment_ids"]
    assert "3" in result["backfilled_segment_ids"]
    assert result["missing_legacy_segment_ids"] == ["2"]
    assert not (project / "editor" / "tts_segments" / "2.wav").exists()


# ---------------------------------------------------------------------------
# refusal: tts/ dir entirely absent
# ---------------------------------------------------------------------------


def test_backfill_raises_when_tts_dir_absent_and_editor_tts_empty(tmp_path: Path) -> None:
    """No tts/ directory AND no editor/tts_segments/ content = we cannot
    produce audio for this task. Raise so enter_editing surfaces it as a
    clear 409 instead of silently creating an empty editor/tts_segments/."""
    project = tmp_path / "project"
    (project / "editor").mkdir(parents=True, exist_ok=True)
    # editor/segments.json exists so segment list is known
    import json
    (project / "editor" / "segments.json").write_text(
        json.dumps([{"segment_id": "1", "speaker_id": "speaker_a",
                     "display_name": "A", "voice_id": "",
                     "start_ms": 0, "end_ms": 1000, "target_duration_ms": 1000,
                     "source_text": "x", "cn_text": "x"}]),
        encoding="utf-8",
    )
    # NO tts/ dir at all, NO editor/tts_segments/

    with pytest.raises(EditorTtsBaselineError, match="no audio source"):
        ensure_editor_tts_segments_baseline(project)


def test_backfill_is_noop_when_tts_dir_absent_but_editor_tts_already_complete(
    tmp_path: Path,
) -> None:
    """Modern task: pipeline wrote editor/tts_segments/{sid}.wav directly
    and cleanup removed tts/. Helper must be a no-op, not raise."""
    project = tmp_path / "project"
    (project / "editor" / "tts_segments").mkdir(parents=True, exist_ok=True)
    (project / "editor" / "tts_segments" / "1.wav").write_bytes(b"modern")
    import json
    (project / "editor").joinpath("segments.json").write_text(
        json.dumps([{"segment_id": "1", "speaker_id": "speaker_a",
                     "display_name": "A", "voice_id": "",
                     "start_ms": 0, "end_ms": 1000, "target_duration_ms": 1000,
                     "source_text": "x", "cn_text": "x"}]),
        encoding="utf-8",
    )

    result = ensure_editor_tts_segments_baseline(project)

    assert result["backfilled_segment_ids"] == []
    assert result["skipped_existing_segment_ids"] == ["1"]
    assert (project / "editor" / "tts_segments" / "1.wav").read_bytes() == b"modern"


# ---------------------------------------------------------------------------
# segment source: prefer editor/ then translation/
# ---------------------------------------------------------------------------


def test_backfill_falls_back_to_translation_segments_when_editor_missing(
    tmp_path: Path,
) -> None:
    """Legacy task may have translation/segments.json but not editor/segments.json
    (latter was seeded only by modern publish or lazy enter_editing). Helper
    must tolerate that by reading segment_ids from translation/."""
    project = tmp_path / "project"
    (project / "tts").mkdir(parents=True)
    _write_wav(project / "tts" / "segment_001_aligned.wav", b"aligned-1")
    _write_wav(project / "tts" / "segment_001_speaker_a.wav", b"raw-1")
    (project / "translation").mkdir(parents=True)
    import json
    (project / "translation" / "segments.json").write_text(
        json.dumps({
            "segments": [{"segment_id": 1, "speaker_id": "speaker_a",
                          "display_name": "A", "voice_id": "",
                          "start_ms": 0, "end_ms": 1000, "target_duration_ms": 1000,
                          "source_text": "x", "cn_text": "x"}],
            "total_segments": 1, "output_path": "",
        }),
        encoding="utf-8",
    )

    result = ensure_editor_tts_segments_baseline(project)

    assert result["backfilled_segment_ids"] == ["1"]
    assert (project / "editor" / "tts_segments" / "1.wav").read_bytes() == b"aligned-1"
