"""Editing speakers merging into baseline at commit time (Task 9).

Plan §Task 9: 当用户在 editing 期间新建了 speaker (editing/speakers.json),
overwrite commit 把新 speaker 合并到 baseline review_state.json:
- speaker_names + speaker_options 都更新
- voice_profile 写到 voice_selection_review.payload.voice_profiles
copy_as_new 把 editing speakers 落到新 job 的 review_state.json,源 job 不动.
"""
from __future__ import annotations

import json
from pathlib import Path

# 这些 helper 由 commit 实现侧导出
from services.jobs.editing_commit import _merge_editing_speakers_into_review_state
from services.jobs.editing_speakers import (
    create_speaker, load_speakers, save_speakers,
)
from services.review_state import (
    ReviewStateManager,
    SPEAKER_REVIEW_STAGE,
    VOICE_SELECTION_REVIEW_STAGE,
)


def _bootstrap_project(tmp_path: Path) -> Path:
    """Project with baseline review_state (2 speakers) + editing/speakers.json."""
    project = tmp_path / "project_xyz"
    edit_dir = project / "editor" / "editing"
    edit_dir.mkdir(parents=True)
    # baseline review_state.json
    rs_path = project / "review_state.json"
    rs_path.write_text(json.dumps({
        "stages": {
            "speaker_review": {
                "stage": "speaker_review",
                "status": "approved",
                "payload": {
                    "speaker_names": {"speaker_a": "Demis", "speaker_b": "Gary"},
                    "speaker_options": [
                        {"speaker_id": "speaker_a", "display_name": "Demis"},
                        {"speaker_id": "speaker_b", "display_name": "Gary"},
                    ],
                },
            },
            "voice_selection_review": {
                "stage": "voice_selection_review",
                "status": "approved",
                "payload": {},
            },
        }
    }), "utf-8")
    return project


def test_merge_writes_new_speaker_to_speaker_names(tmp_path: Path) -> None:
    project = _bootstrap_project(tmp_path)
    # 用户在 editing 加了 speaker_c
    create_speaker(
        project, display_name="Sundar",
        baseline_speakers=[{"speaker_id": "speaker_a"}, {"speaker_id": "speaker_b"}],
    )
    # 模拟 voice profile 推断完成
    speakers = load_speakers(project)
    sp_c = next(s for s in speakers if s.display_name == "Sundar")
    sp_c.profile_status = "ready"
    sp_c.voice_profile = {"voice_description": "warm", "gender": "male"}
    save_speakers(project, speakers)

    # 调 merge
    _merge_editing_speakers_into_review_state(project, load_speakers(project))

    # 验证：speaker_names 含三个 speaker
    rs = ReviewStateManager(project / "review_state.json")
    sr = rs.get_stage(SPEAKER_REVIEW_STAGE)
    assert sr is not None
    names = sr["payload"]["speaker_names"]
    assert names == {
        "speaker_a": "Demis",
        "speaker_b": "Gary",
        "speaker_c": "Sundar",
    }
    # speaker_options 也同步
    options = sr["payload"]["speaker_options"]
    assert {"speaker_id": "speaker_c", "display_name": "Sundar"} in options
    assert len(options) == 3


def test_merge_writes_voice_profile_to_voice_selection_review(tmp_path: Path) -> None:
    project = _bootstrap_project(tmp_path)
    create_speaker(
        project, display_name="C",
        baseline_speakers=[{"speaker_id": "speaker_a"}, {"speaker_id": "speaker_b"}],
    )
    speakers = load_speakers(project)
    speakers[0].profile_status = "ready"
    speakers[0].voice_profile = {"voice_description": "warm"}
    save_speakers(project, speakers)

    _merge_editing_speakers_into_review_state(project, load_speakers(project))

    rs = ReviewStateManager(project / "review_state.json")
    vsr = rs.get_stage(VOICE_SELECTION_REVIEW_STAGE)
    assert vsr is not None
    profiles = vsr["payload"].get("voice_profiles", {})
    assert profiles.get("speaker_c") == {"voice_description": "warm"}


def test_merge_skips_speakers_without_profile(tmp_path: Path) -> None:
    """profile_status='pending_segments' 或 'failed' 的 speaker:
    speaker_names/options 仍然要写(用户已显式创建),但 voice_profile 不写."""
    project = _bootstrap_project(tmp_path)
    create_speaker(
        project, display_name="C-pending",
        baseline_speakers=[{"speaker_id": "speaker_a"}, {"speaker_id": "speaker_b"}],
    )
    # status 保持 pending_segments,无 profile

    _merge_editing_speakers_into_review_state(project, load_speakers(project))

    rs = ReviewStateManager(project / "review_state.json")
    sr = rs.get_stage(SPEAKER_REVIEW_STAGE)
    assert "speaker_c" in sr["payload"]["speaker_names"]

    vsr = rs.get_stage(VOICE_SELECTION_REVIEW_STAGE)
    profiles = (vsr or {}).get("payload", {}).get("voice_profiles", {})
    assert "speaker_c" not in profiles  # 没 profile 就不写


def test_merge_idempotent_doesnt_duplicate_options(tmp_path: Path) -> None:
    """第二次跑 merge 不应让 speaker_options 出现 duplicate entries."""
    project = _bootstrap_project(tmp_path)
    create_speaker(
        project, display_name="C",
        baseline_speakers=[{"speaker_id": "speaker_a"}, {"speaker_id": "speaker_b"}],
    )
    _merge_editing_speakers_into_review_state(project, load_speakers(project))
    _merge_editing_speakers_into_review_state(project, load_speakers(project))  # 再跑一次

    rs = ReviewStateManager(project / "review_state.json")
    sr = rs.get_stage(SPEAKER_REVIEW_STAGE)
    options = sr["payload"]["speaker_options"]
    assert len(options) == 3
    # speaker_options 中 speaker_id 唯一
    ids = [o["speaker_id"] for o in options]
    assert len(set(ids)) == len(ids)


def test_merge_with_no_editing_speakers_is_noop(tmp_path: Path) -> None:
    """editing/speakers.json 为空 → merge 不动 baseline."""
    project = _bootstrap_project(tmp_path)
    rs_before = (project / "review_state.json").read_text("utf-8")
    _merge_editing_speakers_into_review_state(project, [])
    rs_after = (project / "review_state.json").read_text("utf-8")
    assert rs_before == rs_after  # 字节相同


# ---------------------------------------------------------------------------
# E2E: commit overwrite + commit copy_as_new actually call the merge helper
# ---------------------------------------------------------------------------


def test_overwrite_commit_merges_editing_speakers(tmp_path: Path) -> None:
    """E2E: succeeded → enter-edit → create speaker_c (with profile) →
    overwrite commit → baseline review_state contains speaker_c +
    voice_profile."""
    from datetime import datetime, timezone

    from services.jobs.editing import enter_editing
    from services.jobs.editing_commit import commit_editing_pipeline
    from services.jobs.editing_speakers import save_speakers
    from services.jobs.models import (
        JOB_STATUS_SUCCEEDED,
        JobRecord,
    )
    from services.jobs.store import JobStore

    # Build minimal project with baseline review_state + editor/.
    project_dir = tmp_path / "projects" / "job_overwrite_speakers"
    editor = project_dir / "editor"
    editor.mkdir(parents=True)
    (editor / "tts_segments").mkdir()
    (editor / "segments.json").write_text(
        json.dumps([
            {"segment_id": "seg_001", "cn_text": "t1", "start_ms": 0, "end_ms": 1000},
        ], ensure_ascii=False),
        "utf-8",
    )
    (editor / "transcript.json").write_text("{}", "utf-8")
    (editor / "manifest.json").write_text("{}", "utf-8")
    (project_dir / "review_state.json").write_text(json.dumps({
        "stages": {
            "speaker_review": {
                "stage": "speaker_review",
                "status": "approved",
                "payload": {
                    "speaker_names": {"speaker_a": "Demis"},
                    "speaker_options": [
                        {"speaker_id": "speaker_a", "display_name": "Demis"},
                    ],
                },
            },
            "voice_selection_review": {
                "stage": "voice_selection_review",
                "status": "approved",
                "payload": {},
            },
        }
    }), "utf-8")
    now_iso = datetime.now(timezone.utc).isoformat()
    record = JobRecord(
        job_id="job_overwrite_speakers",
        job_type="localize_video",
        source_type="youtube_url",
        source_ref="https://example.com/video",
        output_target="editor",
        speakers="auto",
        voice_a=None,
        voice_b=None,
        status=JOB_STATUS_SUCCEEDED,
        current_stage="completed",
        progress_message=None,
        created_at=now_iso,
        updated_at=now_iso,
        project_dir=str(project_dir),
        service_mode="studio",
    )
    store = JobStore(tmp_path / "jobs")
    store.save_job(record)
    editing_record = enter_editing(record, store)

    # Create new speaker in editing/ and stamp ready+profile.
    create_speaker(
        project_dir,
        display_name="Sundar",
        baseline_speakers=[{"speaker_id": "speaker_a", "display_name": "Demis"}],
    )
    speakers = load_speakers(project_dir)
    sp_new = next(s for s in speakers if s.display_name == "Sundar")
    sp_new.profile_status = "ready"
    sp_new.voice_profile = {"voice_description": "calm"}
    save_speakers(project_dir, speakers)

    class _Runner:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def start(self, record, continue_existing: bool = False) -> None:
            self.calls.append({"job_id": record.job_id})

    runner = _Runner()
    commit_editing_pipeline(editing_record, store, runner, strategy="overwrite")

    # Verify baseline review_state was merged.
    rs = ReviewStateManager(project_dir / "review_state.json")
    sr = rs.get_stage(SPEAKER_REVIEW_STAGE)
    assert sr is not None
    assert "speaker_b" in sr["payload"]["speaker_names"], (
        "create_speaker should have allocated speaker_b (next free slot)"
    )
    assert sr["payload"]["speaker_names"]["speaker_b"] == "Sundar"

    vsr = rs.get_stage(VOICE_SELECTION_REVIEW_STAGE)
    assert vsr is not None
    profiles = vsr["payload"].get("voice_profiles", {})
    assert profiles.get("speaker_b") == {"voice_description": "calm"}
