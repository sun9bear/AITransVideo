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


def test_load_speakers_corrupt_json_returns_empty(tmp_path: Path) -> None:
    """JSON 损坏 → 不抛，返 []（与 editing_segments / admin_settings 风格一致）。"""
    project = _bootstrap_project(tmp_path)
    editing_speakers_path(project).write_text("{not json", "utf-8")
    assert load_speakers(project) == []


def test_color_stable_across_calls(tmp_path: Path) -> None:
    """_color_for_id 必须确定性（不是 hash() PYTHONHASHSEED 随机）。"""
    from services.jobs.editing_speakers import _color_for_id
    # 多次调用同一 id 必须给同一 color
    c1 = _color_for_id("speaker_c")
    c2 = _color_for_id("speaker_c")
    assert c1 == c2
    # 不同 id 应能产出不同 color（统计意义；palette 8 色可能碰撞，
    # 但 a/b/c 三个一致碰撞概率低，本测试只检 a 和 z 不冲，足以
    # 反向证明 hash 不是恒定常量）。
    assert _color_for_id("speaker_a") != _color_for_id("speaker_z")


# ---------------------------------------------------------------------------
# load_baseline_speakers (Task 3)
# ---------------------------------------------------------------------------
import json as _json_mod  # 避免和 fixture 命名冲突


def test_load_baseline_speakers_reads_review_state(tmp_path: Path) -> None:
    """从 <project_dir>/review_state.json 读 baseline speaker_names。"""
    project = _bootstrap_project(tmp_path)
    (project / "review_state.json").write_text(_json_mod.dumps({
        "stages": {
            "speaker_review": {
                "payload": {
                    "speaker_names": {"speaker_a": "Demis", "speaker_b": "Gary"}
                }
            }
        }
    }), "utf-8")
    from services.jobs.editing_speakers import load_baseline_speakers
    bl = load_baseline_speakers(project)
    assert {"speaker_id": "speaker_a", "display_name": "Demis"} in bl
    assert {"speaker_id": "speaker_b", "display_name": "Gary"} in bl
    assert len(bl) == 2


def test_load_baseline_speakers_missing_file_returns_empty(tmp_path: Path) -> None:
    project = _bootstrap_project(tmp_path)
    from services.jobs.editing_speakers import load_baseline_speakers
    assert load_baseline_speakers(project) == []


def test_load_baseline_speakers_corrupt_json_returns_empty(tmp_path: Path) -> None:
    project = _bootstrap_project(tmp_path)
    (project / "review_state.json").write_text("not json{", "utf-8")
    from services.jobs.editing_speakers import load_baseline_speakers
    assert load_baseline_speakers(project) == []


def test_load_baseline_speakers_missing_speaker_review_stage_returns_empty(tmp_path: Path) -> None:
    project = _bootstrap_project(tmp_path)
    (project / "review_state.json").write_text(_json_mod.dumps({"stages": {}}), "utf-8")
    from services.jobs.editing_speakers import load_baseline_speakers
    assert load_baseline_speakers(project) == []
