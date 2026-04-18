"""T1-8 — copy_as_new filesystem helpers.

Covers:
- hardlink_baseline_audio: wav linking semantics + inode sharing
- write_audio_safely: atomic write, no shared-inode corruption
- apply_draft_segment: unlink-before-move preserves source inode
- prepare_copy_project_dir: full Phase A orchestration + failure paths
- rollback_prepared_target: cleanup
- submit_job_from_existing_project_dir: signature + runner hand-off
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from services.jobs.copy_service import (
    CopyPreparationError,
    apply_draft_segment,
    hardlink_baseline_audio,
    prepare_copy_project_dir,
    rollback_prepared_target,
    write_audio_safely,
)
from services.jobs.runner_extensions import (
    SUPPORTED_START_STAGES,
    submit_job_from_existing_project_dir,
)

# hardlink / os.link is supported on Windows NTFS but pytest runs tend to be
# slow and permission-gated. Skip platform-sensitive tests if we can't.
IS_WINDOWS = sys.platform.startswith("win")


# ---------------------------------------------------------------------------
# hardlink_baseline_audio
# ---------------------------------------------------------------------------


def _make_source_project(tmp_path: Path, *, n_segments: int = 3) -> Path:
    source = tmp_path / "src_project"
    tts_dir = source / "editor" / "tts_segments"
    tts_dir.mkdir(parents=True)
    for i in range(1, n_segments + 1):
        (tts_dir / f"seg_{i:03d}.wav").write_bytes(f"BASE_{i}".encode())
    (source / "editor" / "segments.json").write_text(
        json.dumps([
            {"segment_id": f"seg_{i:03d}", "cn_text": f"text{i}",
             "provider": "minimax", "voice_id": "v_default"}
            for i in range(1, n_segments + 1)
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    (source / "editor" / "transcript.json").write_text("{}", encoding="utf-8")
    (source / "editor" / "manifest.json").write_text(
        json.dumps({"artifact_count": n_segments}),
        encoding="utf-8",
    )
    # editing/ dir (required by prepare_copy_project_dir)
    editing = source / "editor" / "editing"
    (editing / "tts_segments_draft").mkdir(parents=True)
    return source


def test_hardlink_links_all_segments(tmp_path: Path) -> None:
    source = _make_source_project(tmp_path, n_segments=3)
    target = tmp_path / "dst_project"

    linked = hardlink_baseline_audio(source, target)

    assert sorted(linked) == ["seg_001", "seg_002", "seg_003"]
    for i in range(1, 4):
        dst_wav = target / "editor" / "tts_segments" / f"seg_{i:03d}.wav"
        src_wav = source / "editor" / "tts_segments" / f"seg_{i:03d}.wav"
        assert dst_wav.is_file()
        # Same inode → both counted in the link count
        assert src_wav.stat().st_ino == dst_wav.stat().st_ino
        assert src_wav.stat().st_nlink >= 2


def test_hardlink_missing_source_is_noop(tmp_path: Path) -> None:
    source = tmp_path / "nothing"
    target = tmp_path / "dst"
    linked = hardlink_baseline_audio(source, target)
    assert linked == []


def test_hardlink_existing_target_file_raises(tmp_path: Path) -> None:
    source = _make_source_project(tmp_path, n_segments=1)
    target = tmp_path / "dst"
    (target / "editor" / "tts_segments").mkdir(parents=True)
    (target / "editor" / "tts_segments" / "seg_001.wav").write_bytes(b"EXISTING")

    with pytest.raises(FileExistsError):
        hardlink_baseline_audio(source, target)


# ---------------------------------------------------------------------------
# write_audio_safely — atomic + no shared-inode corruption
# ---------------------------------------------------------------------------


def test_write_audio_safely_creates_file(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "out.wav"
    write_audio_safely(target, b"HELLO")
    assert target.read_bytes() == b"HELLO"


def test_write_audio_safely_does_not_mutate_hardlinked_source(tmp_path: Path) -> None:
    """Critical invariant (plan §3.5): if target is a hardlink to source,
    overwriting via write_audio_safely must NOT change source's bytes."""
    source_file = tmp_path / "source.wav"
    source_file.write_bytes(b"ORIGINAL_SOURCE")
    target_file = tmp_path / "target.wav"
    os.link(source_file, target_file)
    assert source_file.stat().st_ino == target_file.stat().st_ino

    write_audio_safely(target_file, b"NEW_BYTES")

    # Target has new bytes, source untouched (link was broken cleanly).
    assert target_file.read_bytes() == b"NEW_BYTES"
    assert source_file.read_bytes() == b"ORIGINAL_SOURCE"
    assert source_file.stat().st_ino != target_file.stat().st_ino


def test_write_audio_safely_no_temp_leftover(tmp_path: Path) -> None:
    target = tmp_path / "out.wav"
    write_audio_safely(target, b"X")
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []


# ---------------------------------------------------------------------------
# apply_draft_segment
# ---------------------------------------------------------------------------


def test_apply_draft_installs_over_hardlink_without_touching_source(tmp_path: Path) -> None:
    source_wav = tmp_path / "source" / "seg_001.wav"
    source_wav.parent.mkdir()
    source_wav.write_bytes(b"SOURCE_BYTES")
    target_wav = tmp_path / "target" / "seg_001.wav"
    target_wav.parent.mkdir()
    os.link(source_wav, target_wav)

    draft_wav = tmp_path / "draft" / "seg_001.wav"
    draft_wav.parent.mkdir()
    draft_wav.write_bytes(b"DRAFT_BYTES")

    apply_draft_segment(draft_wav, target_wav)

    # Target has draft bytes, source pristine, draft moved away
    assert target_wav.read_bytes() == b"DRAFT_BYTES"
    assert source_wav.read_bytes() == b"SOURCE_BYTES"
    assert not draft_wav.exists()


def test_apply_draft_creates_target_when_absent(tmp_path: Path) -> None:
    """Edge case: segment added during editing, no baseline wav yet."""
    draft_wav = tmp_path / "draft.wav"
    draft_wav.write_bytes(b"NEW_SEG")
    target_wav = tmp_path / "target" / "seg_new.wav"

    apply_draft_segment(draft_wav, target_wav)

    assert target_wav.read_bytes() == b"NEW_SEG"
    assert not draft_wav.exists()


def test_apply_draft_missing_draft_raises(tmp_path: Path) -> None:
    target_wav = tmp_path / "target.wav"
    with pytest.raises(FileNotFoundError):
        apply_draft_segment(tmp_path / "nonexistent", target_wav)


# ---------------------------------------------------------------------------
# prepare_copy_project_dir — full Phase A
# ---------------------------------------------------------------------------


def _populate_editing_dir(
    source: Path,
    *,
    text_edits: dict[str, str] | None = None,
    voice_map: dict[str, dict[str, Any]] | None = None,
    draft_segments: dict[str, bytes] | None = None,
) -> None:
    """Simulate the state an enter_editing + a few user actions would produce.

    - editing/segments.json is a copy of baseline with ``text_edits`` applied
    - editing/voice_map.json holds the per-segment overrides
    - editing/tts_segments_draft/ holds the draft wavs
    """
    editing = source / "editor" / "editing"
    editing.mkdir(parents=True, exist_ok=True)
    baseline = json.loads(
        (source / "editor" / "segments.json").read_text(encoding="utf-8")
    )
    if text_edits:
        for seg in baseline:
            if seg["segment_id"] in text_edits:
                seg["cn_text"] = text_edits[seg["segment_id"]]
    (editing / "segments.json").write_text(
        json.dumps(baseline, ensure_ascii=False), encoding="utf-8"
    )
    if voice_map is not None:
        (editing / "voice_map.json").write_text(
            json.dumps(voice_map, ensure_ascii=False), encoding="utf-8"
        )
    (editing / "tts_segments_draft").mkdir(exist_ok=True)
    if draft_segments:
        for sid, payload in draft_segments.items():
            (editing / "tts_segments_draft" / f"{sid}.wav").write_bytes(payload)


def test_prepare_copy_no_edits_just_hardlinks(tmp_path: Path) -> None:
    source = _make_source_project(tmp_path, n_segments=2)
    _populate_editing_dir(source)  # empty editing state

    target = tmp_path / "copy"
    summary = prepare_copy_project_dir(source, target)

    assert summary["linked_segment_ids"] == ["seg_001", "seg_002"]
    assert summary["applied_draft_segment_ids"] == []
    # Target has 2 wavs, all hardlinked to source
    for sid in ("seg_001", "seg_002"):
        src_wav = source / "editor" / "tts_segments" / f"{sid}.wav"
        dst_wav = target / "editor" / "tts_segments" / f"{sid}.wav"
        assert src_wav.stat().st_ino == dst_wav.stat().st_ino
    # transcript / manifest copied
    assert (target / "editor" / "transcript.json").is_file()
    assert (target / "editor" / "manifest.json").is_file()


def test_prepare_copy_with_text_edits_writes_new_segments_json(tmp_path: Path) -> None:
    source = _make_source_project(tmp_path, n_segments=3)
    _populate_editing_dir(source, text_edits={"seg_002": "NEW_TEXT_2"})
    target = tmp_path / "copy"

    prepare_copy_project_dir(source, target)

    out = json.loads(
        (target / "editor" / "segments.json").read_text(encoding="utf-8")
    )
    assert out[1]["segment_id"] == "seg_002"
    assert out[1]["cn_text"] == "NEW_TEXT_2"
    # Source segments.json untouched
    src_segs = json.loads(
        (source / "editor" / "segments.json").read_text(encoding="utf-8")
    )
    assert src_segs[1]["cn_text"] == "text2"


def test_prepare_copy_applies_voice_map_overrides(tmp_path: Path) -> None:
    source = _make_source_project(tmp_path, n_segments=2)
    _populate_editing_dir(
        source,
        voice_map={"seg_001": {"provider": "cosyvoice", "voice_id": "cv_new"}},
    )
    target = tmp_path / "copy"

    prepare_copy_project_dir(source, target)

    out = json.loads(
        (target / "editor" / "segments.json").read_text(encoding="utf-8")
    )
    assert out[0]["provider"] == "cosyvoice"
    assert out[0]["voice_id"] == "cv_new"
    # seg_002 untouched
    assert out[1]["provider"] == "minimax"


def test_prepare_copy_applies_draft_wavs_over_hardlinks(tmp_path: Path) -> None:
    source = _make_source_project(tmp_path, n_segments=3)
    _populate_editing_dir(
        source,
        draft_segments={
            "seg_002": b"DRAFT_SEG_2",
            "seg_003": b"DRAFT_SEG_3",
        },
    )
    target = tmp_path / "copy"

    summary = prepare_copy_project_dir(source, target)

    assert sorted(summary["applied_draft_segment_ids"]) == ["seg_002", "seg_003"]
    # seg_001 still hardlinked
    assert (
        (source / "editor" / "tts_segments" / "seg_001.wav").stat().st_ino
        == (target / "editor" / "tts_segments" / "seg_001.wav").stat().st_ino
    )
    # seg_002/003 were overwritten via apply_draft_segment: new bytes + broken inode
    assert (target / "editor" / "tts_segments" / "seg_002.wav").read_bytes() == b"DRAFT_SEG_2"
    assert (source / "editor" / "tts_segments" / "seg_002.wav").read_bytes() == b"BASE_2"
    assert (
        (source / "editor" / "tts_segments" / "seg_002.wav").stat().st_ino
        != (target / "editor" / "tts_segments" / "seg_002.wav").stat().st_ino
    )
    # Draft file was COPIED not moved — source editing/ dir remains intact for Phase B
    assert (source / "editor" / "editing" / "tts_segments_draft" / "seg_002.wav").is_file()


def test_prepare_copy_missing_editing_dir_raises(tmp_path: Path) -> None:
    source = _make_source_project(tmp_path)
    # Remove editing dir that _make_source_project stubbed
    import shutil
    shutil.rmtree(source / "editor" / "editing")
    with pytest.raises(CopyPreparationError, match="no editor/editing/"):
        prepare_copy_project_dir(source, tmp_path / "copy")


def test_prepare_copy_target_already_exists_raises(tmp_path: Path) -> None:
    source = _make_source_project(tmp_path)
    _populate_editing_dir(source)
    target = tmp_path / "copy"
    target.mkdir()
    with pytest.raises(CopyPreparationError, match="already exists"):
        prepare_copy_project_dir(source, target)


def test_rollback_prepared_target_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "gone"
    rollback_prepared_target(target)  # nothing to do → no crash
    target.mkdir()
    (target / "file").write_text("x")
    rollback_prepared_target(target)
    assert not target.exists()


# ---------------------------------------------------------------------------
# Critical invariant: source never mutated through Phase A + rollback
# ---------------------------------------------------------------------------


def _hash_tree(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def test_source_immutable_through_prepare_and_rollback(tmp_path: Path) -> None:
    source = _make_source_project(tmp_path, n_segments=3)
    _populate_editing_dir(
        source,
        text_edits={"seg_001": "E1"},
        voice_map={"seg_002": {"provider": "cosyvoice", "voice_id": "v"}},
        draft_segments={"seg_003": b"DRAFT_3"},
    )
    before = _hash_tree(source)

    target = tmp_path / "copy"
    prepare_copy_project_dir(source, target)
    # Roll the target back to simulate commit failure
    rollback_prepared_target(target)

    after = _hash_tree(source)
    assert before == after, "source tree mutated by prepare + rollback"
    assert not target.exists()


# ---------------------------------------------------------------------------
# runner_extensions
# ---------------------------------------------------------------------------


def test_supported_start_stages_contract() -> None:
    assert SUPPORTED_START_STAGES == frozenset({"alignment"})


def test_submit_from_existing_rejects_unsupported_stage() -> None:
    class FakeRunner:
        calls: list = []
        def start(self, record, continue_existing=False):
            self.calls.append((record, continue_existing))

    from datetime import datetime, timezone
    from services.jobs.models import JobRecord, JOB_STATUS_QUEUED

    now_iso = datetime.now(timezone.utc).isoformat()
    record = JobRecord(
        job_id="j1", job_type="localize_video", source_type="youtube_url",
        source_ref="https://example.com", output_target="editor",
        speakers="auto", voice_a=None, voice_b=None, status=JOB_STATUS_QUEUED,
        current_stage=None, progress_message=None,
        created_at=now_iso, updated_at=now_iso, project_dir="/tmp",
        service_mode="studio",
    )
    runner = FakeRunner()
    with pytest.raises(ValueError, match="unsupported start_stage"):
        submit_job_from_existing_project_dir(runner, record, start_stage="ingestion")


def test_submit_from_existing_invokes_runner_with_continue_existing() -> None:
    class FakeRunner:
        def __init__(self):
            self.calls = []
        def start(self, record, continue_existing=False):
            self.calls.append((record, continue_existing))

    from datetime import datetime, timezone
    from services.jobs.models import JobRecord, JOB_STATUS_QUEUED

    now_iso = datetime.now(timezone.utc).isoformat()
    record = JobRecord(
        job_id="j1", job_type="localize_video", source_type="youtube_url",
        source_ref="https://example.com", output_target="editor",
        speakers="auto", voice_a=None, voice_b=None, status=JOB_STATUS_QUEUED,
        current_stage=None, progress_message=None,
        created_at=now_iso, updated_at=now_iso, project_dir="/tmp",
        service_mode="studio",
    )
    runner = FakeRunner()
    updated = submit_job_from_existing_project_dir(runner, record)

    assert len(runner.calls) == 1
    sent_record, continue_flag = runner.calls[0]
    assert continue_flag is True
    assert sent_record.current_stage == "alignment"
    assert updated.current_stage == "alignment"
