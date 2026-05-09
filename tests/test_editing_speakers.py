from __future__ import annotations
import re
from pathlib import Path
import pytest
from services.jobs.editing_speakers import (
    EditingSpeaker, DisplayNameConflictError,
    load_speakers, create_speaker, next_speaker_id, editing_speakers_path,
)


def _bootstrap_project(tmp_path: Path) -> Path:
    """Build a minimal project_dir with editor/editing/ subdir."""
    project = tmp_path / "project_xyz"
    (project / "editor" / "editing").mkdir(parents=True)
    return project


def test_speakers_path_under_editor_editing(tmp_path: Path) -> None:
    p = _bootstrap_project(tmp_path)
    assert editing_speakers_path(p) == p / "editor" / "editing" / "speakers.json"


def test_load_speakers_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_speakers(_bootstrap_project(tmp_path)) == []


def test_create_speaker_writes_file(tmp_path: Path) -> None:
    project = _bootstrap_project(tmp_path)
    sp = create_speaker(project, display_name="桑达尔", baseline_speakers=[])
    assert sp.speaker_id == "speaker_a"
    assert sp.display_name == "桑达尔"
    assert sp.source == "editing"
    assert sp.profile_status == "pending_segments"
    assert load_speakers(project)[0].speaker_id == "speaker_a"


def test_next_speaker_id_skips_baseline(tmp_path: Path) -> None:
    project = _bootstrap_project(tmp_path)
    baseline = [{"speaker_id": "speaker_a"}, {"speaker_id": "speaker_b"}]
    sp = create_speaker(project, display_name="C", baseline_speakers=baseline)
    assert sp.speaker_id == "speaker_c"


def test_display_name_conflict_within_editing(tmp_path: Path) -> None:
    project = _bootstrap_project(tmp_path)
    create_speaker(project, display_name="A", baseline_speakers=[])
    with pytest.raises(DisplayNameConflictError):
        create_speaker(project, display_name="A", baseline_speakers=[])


def test_display_name_conflict_against_baseline(tmp_path: Path) -> None:
    project = _bootstrap_project(tmp_path)
    baseline = [{"speaker_id": "speaker_a", "display_name": "Demis"}]
    with pytest.raises(DisplayNameConflictError):
        create_speaker(project, display_name="Demis", baseline_speakers=baseline)


def test_display_name_strips_whitespace(tmp_path: Path) -> None:
    project = _bootstrap_project(tmp_path)
    create_speaker(project, display_name=" Demis ", baseline_speakers=[])
    with pytest.raises(DisplayNameConflictError):
        create_speaker(project, display_name="Demis", baseline_speakers=[])


def test_overflow_after_z_falls_back_to_hex(tmp_path: Path) -> None:
    project = _bootstrap_project(tmp_path)
    baseline = [{"speaker_id": f"speaker_{c}"} for c in "abcdefghijklmnopqrstuvwxyz"]
    sp = create_speaker(project, display_name="X", baseline_speakers=baseline)
    assert re.fullmatch(r"speaker_[0-9a-f]{8}", sp.speaker_id)
