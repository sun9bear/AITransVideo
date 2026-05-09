"""Speaker reassignment with editing/speakers.json registration (Task 2).

Plan §Task 2: PATCH /segments/{sid}/update accepts speaker_id values that
are already in segments.json OR already registered in editing/speakers.json.
Unknown ids still rejected.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.jobs.editing_segments import patch_editing_segment
from services.jobs.editing_speakers import create_speaker


def _bootstrap_project(tmp_path: Path) -> Path:
    """project_dir 含 editor/editing/{segments,segment_status}.json,
    内有 speaker_a / speaker_b 两段。"""
    project = tmp_path / "project_xyz"
    edit_dir = project / "editor" / "editing"
    edit_dir.mkdir(parents=True)
    segments = [
        {"segment_id": "seg_1", "speaker_id": "speaker_a", "cn_text": "a",
         "voice_id": "v_a", "tts_provider": "minimax"},
        {"segment_id": "seg_2", "speaker_id": "speaker_b", "cn_text": "b",
         "voice_id": "v_b", "tts_provider": "minimax"},
    ]
    (edit_dir / "segments.json").write_text(
        json.dumps(segments, ensure_ascii=False), "utf-8"
    )
    (edit_dir / "segment_status.json").write_text("{}", "utf-8")
    return project


def test_reassign_to_baseline_speaker_still_works(tmp_path: Path) -> None:
    """Regression: 现有 reassign 到已存在 speaker 路径不应被破坏。"""
    project = _bootstrap_project(tmp_path)
    result = patch_editing_segment(
        project, segment_id="seg_1", patch={"speaker_id": "speaker_b"}
    )
    assert result["speaker_id"] == "speaker_b"
    # voice_id / tts_provider 应被 propagate 成 speaker_b 的
    assert result["voice_id"] == "v_b"


def test_reassign_to_unknown_speaker_still_rejected(tmp_path: Path) -> None:
    """speaker_z 既不在 segments 也不在 editing/speakers.json -> 仍 reject。"""
    project = _bootstrap_project(tmp_path)
    with pytest.raises(ValueError, match="not found"):
        patch_editing_segment(
            project, segment_id="seg_1", patch={"speaker_id": "speaker_z"}
        )


def test_reassign_to_registered_editing_speaker_succeeds(tmp_path: Path) -> None:
    """speaker_c 仅注册在 editing/speakers.json,没有任何 segment 用过 -> 应允许。"""
    project = _bootstrap_project(tmp_path)
    sp = create_speaker(
        project, display_name="C说话人",
        baseline_speakers=[{"speaker_id": "speaker_a"}, {"speaker_id": "speaker_b"}],
    )
    assert sp.speaker_id == "speaker_c"  # 第三个字母

    result = patch_editing_segment(
        project, segment_id="seg_1", patch={"speaker_id": "speaker_c"}
    )
    assert result["speaker_id"] == "speaker_c"
    # speaker_c 在其他段没出现 -> voice_id / tts_provider 保持原值（不能 propagate）
    assert result.get("voice_id") == "v_a"  # baseline 没动


def test_error_message_lists_both_known_and_editing_ids(tmp_path: Path) -> None:
    """Reject 时错误消息应同时列出 segments + editing speakers,方便定位。"""
    project = _bootstrap_project(tmp_path)
    create_speaker(
        project, display_name="C",
        baseline_speakers=[{"speaker_id": "speaker_a"}, {"speaker_id": "speaker_b"}],
    )
    with pytest.raises(ValueError) as exc_info:
        patch_editing_segment(
            project, segment_id="seg_1", patch={"speaker_id": "speaker_z"}
        )
    msg = str(exc_info.value)
    # 有 speaker_a / speaker_b（来自 segments）+ speaker_c（来自 editing/speakers.json）
    assert "speaker_a" in msg
    assert "speaker_b" in msg
    assert "speaker_c" in msg
