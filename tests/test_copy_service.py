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


# ---------------------------------------------------------------------------
# prepare_copy_project_dir — full pipeline artifact cloning (2026-04-19 fix)
#
# Background: without these, a copy_as_new new-job pipeline sees an
# otherwise-empty project_dir and re-runs S0 ingestion → S6 publish from
# scratch, burning paid API (ASR / LLM / TTS) and violating the "增量编辑"
# promise of the video-edit feature. The tests below pin down what
# prepare_copy_project_dir must additionally hardlink / copy / rewrite so
# that the pipeline's own cache-check logic at each stage successfully
# skips to alignment.
# ---------------------------------------------------------------------------


def _add_media_artifacts(source: Path) -> None:
    """Populate the media artifacts that a succeeded pipeline would have
    produced on disk. Used by the cloning regression tests below — the
    original _make_source_project only set up editor/ because the T1-8
    tests predate the need for full artifact cloning."""
    (source / "video").mkdir(parents=True, exist_ok=True)
    (source / "video" / "original.mp4").write_bytes(b"FAKE_MP4_BYTES")
    (source / "audio").mkdir(parents=True, exist_ok=True)
    (source / "audio" / "original.wav").write_bytes(b"FAKE_WAV_ORIGINAL")
    (source / "audio" / "speech.wav").write_bytes(b"FAKE_WAV_SPEECH")
    (source / "audio" / "ambient.wav").write_bytes(b"FAKE_WAV_AMBIENT")


def test_prepare_copy_hardlinks_media_artifacts(tmp_path: Path) -> None:
    """video/original.mp4 and audio/{original,speech,ambient}.wav are the
    on-disk markers pipeline S0 / audio_preparation / S1 cache-check
    against — without them, demucs re-separates and transcription re-runs,
    costing real money and time. They're large and immutable after
    ingestion, so hardlink is correct."""
    source = _make_source_project(tmp_path, n_segments=2)
    _add_media_artifacts(source)
    _populate_editing_dir(source)
    target = tmp_path / "copy"

    prepare_copy_project_dir(source, target)

    for rel in (
        "video/original.mp4",
        "audio/original.wav",
        "audio/speech.wav",
        "audio/ambient.wav",
    ):
        src = source / rel
        dst = target / rel
        assert dst.is_file(), f"{rel} missing in target"
        assert dst.stat().st_ino == src.stat().st_ino, (
            f"{rel} not hardlinked (target inode {dst.stat().st_ino} != "
            f"source {src.stat().st_ino})"
        )


def _add_metadata_and_transcript(source: Path) -> None:
    """Populate download_metadata.json + transcript/transcript.json.
    Pipeline S1 cache-check reads transcript.json; download_metadata.json
    is used as ground-truth for video title / duration / URL during
    ingestion restart."""
    (source / "download_metadata.json").write_text(
        json.dumps({
            "url": "https://example.com/v",
            "video_title": "sample",
            "duration_ms": 60000,
            "video_path": str(source / "video" / "original.mp4"),
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (source / "transcript").mkdir(parents=True, exist_ok=True)
    (source / "transcript" / "transcript.json").write_text(
        json.dumps({"lines": [{"speaker_id": "A", "text": "hello"}]}),
        encoding="utf-8",
    )


def test_prepare_copy_copies_download_metadata_and_transcript(tmp_path: Path) -> None:
    """Pipeline S1 cache-check reads ``transcript/transcript.json`` to
    decide "skip transcription". ``download_metadata.json`` is consulted
    on ingestion restart for title / duration / URL. Both must land in
    the target directory."""
    source = _make_source_project(tmp_path, n_segments=2)
    _add_media_artifacts(source)
    _add_metadata_and_transcript(source)
    _populate_editing_dir(source)
    target = tmp_path / "copy"

    prepare_copy_project_dir(source, target)

    assert (target / "download_metadata.json").is_file(), (
        "download_metadata.json missing in target — pipeline S0 restart "
        "would lose title / duration / URL"
    )
    assert (target / "transcript" / "transcript.json").is_file(), (
        "transcript/transcript.json missing in target — pipeline S1 "
        "would re-run transcription and re-charge AssemblyAI quota"
    )
    # Metadata content preserved byte-for-byte.
    assert (
        (target / "download_metadata.json").read_text(encoding="utf-8")
        == (source / "download_metadata.json").read_text(encoding="utf-8")
    )


def _add_translation_segments(source: Path, *, n_segments: int = 2) -> None:
    """Populate translation/segments.json in the pipeline shape the
    S3 cache-check expects. Each segment carries absolute paths to its
    tts_audio_path under the source project_dir — copy_as_new must
    rewrite these to target paths, otherwise the new job reads audio
    from the source's tree and any subsequent write would corrupt
    source state."""
    (source / "translation").mkdir(parents=True, exist_ok=True)
    segs = []
    for i in range(1, n_segments + 1):
        sid = f"seg_{i:03d}"
        segs.append({
            "segment_id": i,
            "speaker_id": "speaker_a",
            "cn_text": f"text{i}",
            "start_ms": 0,
            "end_ms": 1000,
            "target_duration_ms": 1000,
            "tts_audio_path": str(
                source / "editor" / "tts_segments" / f"{sid}.wav"
            ),
            "aligned_audio_path": str(
                source / "editor" / "aligned" / f"{sid}.wav"
            ),
            "voice_id": "v_default",
            "tts_provider": "minimax",
        })
    (source / "translation" / "segments.json").write_text(
        json.dumps({"segments": segs, "total_segments": n_segments}),
        encoding="utf-8",
    )


def test_prepare_copy_rewrites_absolute_paths_in_translation_segments(
    tmp_path: Path,
) -> None:
    """translation/segments.json carries absolute paths
    (tts_audio_path / aligned_audio_path / etc.) pointing at source
    project_dir. copy_as_new must rewrite them to target project_dir or:
      - pipeline S3 cache-restore reads bytes from source (correct now)
        but any S5 alignment rewrite (e.g. DSP stretch) would mutate the
        source wav, corrupting the original task.
      - path-sensitive logic (editor_package_writer, segment manifest)
        would emit source paths to downstream artifacts, breaking the
        "副本 is self-contained" invariant."""
    source = _make_source_project(tmp_path, n_segments=2)
    _add_media_artifacts(source)
    _add_translation_segments(source, n_segments=2)
    _populate_editing_dir(source)
    target = tmp_path / "copy"

    prepare_copy_project_dir(source, target)

    target_trans = target / "translation" / "segments.json"
    assert target_trans.is_file(), (
        "translation/segments.json missing in target — S3 cache-check "
        "would miss and re-run LLM translation"
    )
    out = json.loads(target_trans.read_text(encoding="utf-8"))
    segs = out["segments"] if isinstance(out, dict) else out
    for seg in segs:
        tts_path = seg["tts_audio_path"]
        aligned_path = seg["aligned_audio_path"]
        assert str(source) not in tts_path, (
            f"tts_audio_path still points at source: {tts_path}"
        )
        assert str(target) in tts_path, (
            f"tts_audio_path not rewritten to target: {tts_path}"
        )
        assert str(source) not in aligned_path, (
            f"aligned_audio_path still points at source: {aligned_path}"
        )
        assert str(target) in aligned_path, (
            f"aligned_audio_path not rewritten: {aligned_path}"
        )
    # Source file still holds source-dir paths (rewrite must not mutate source).
    src_trans = json.loads(
        (source / "translation" / "segments.json").read_text(encoding="utf-8")
    )
    src_segs = src_trans["segments"] if isinstance(src_trans, dict) else src_trans
    for seg in src_segs:
        assert str(source) in seg["tts_audio_path"], (
            "source translation/segments.json was mutated — rewrite must "
            "only touch the target copy"
        )


def _add_project_state(source: Path) -> None:
    """Populate project_state.json in the shape a succeeded pipeline
    would leave — every stage DONE. copy_as_new must keep upstream DONE
    (ingestion → translation) and prune alignment + legacy_process_output
    to PENDING so the new job actually re-runs those on the edited
    inputs."""
    (source / "project_state.json").write_text(
        json.dumps({
            "project_id": source.name,
            "stages": {
                "ingestion":          {"status": "done", "payload": {"ok": 1}},
                "audio_preparation":  {"status": "done", "payload": {"ok": 1}},
                "media_understanding":{"status": "done", "payload": {"ok": 1}},
                "translation_review": {"status": "done", "payload": {"ok": 1}},
                "voice_selection_review": {"status": "done", "payload": {"ok": 1}},
                "translation":        {"status": "done", "payload": {"ok": 1}},
                "alignment":          {"status": "done", "payload": {"ok": 1}},
                "legacy_process_output": {"status": "done", "payload": {"ok": 1}},
            },
        }),
        encoding="utf-8",
    )


def test_prepare_copy_prunes_alignment_stages_in_project_state(
    tmp_path: Path,
) -> None:
    """The copy inherits DONE for everything up to translation, but
    alignment + legacy_process_output MUST be reset to PENDING so
    pipeline re-runs them on the edited segments / drafts. Without
    this, ProcessPipeline would see "publish already done" and refuse
    to regenerate the final video."""
    source = _make_source_project(tmp_path, n_segments=2)
    _add_media_artifacts(source)
    _add_project_state(source)
    _populate_editing_dir(source)
    target = tmp_path / "copy"

    prepare_copy_project_dir(source, target)

    dst_state = target / "project_state.json"
    assert dst_state.is_file()
    state = json.loads(dst_state.read_text(encoding="utf-8"))
    stages = state["stages"]
    # Upstream stages preserved DONE so their cache-check + set_stage
    # writes still match on pipeline restart.
    for preserved in (
        "ingestion", "audio_preparation", "media_understanding",
        "translation_review", "translation",
    ):
        assert stages[preserved]["status"] == "done", (
            f"{preserved} was accidentally pruned; upstream stages must "
            "stay DONE so pipeline skips them"
        )
    # Alignment + publish reset to PENDING so they actually re-run.
    for pruned in ("alignment", "legacy_process_output"):
        assert stages[pruned]["status"] == "pending", (
            f"{pruned} still DONE; pipeline would skip the re-run and "
            "the edit would not affect final output"
        )
    # project_id rewritten to target (the project_id convention is to
    # use the directory's basename).
    assert state["project_id"] == target.name, (
        f"project_id={state['project_id']!r} not rewritten to target basename"
    )
    # Source state is untouched.
    src_state = json.loads((source / "project_state.json").read_text(encoding="utf-8"))
    assert src_state["stages"]["alignment"]["status"] == "done"
    assert src_state["stages"]["legacy_process_output"]["status"] == "done"


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


def test_source_immutable_with_full_pipeline_artifacts(tmp_path: Path) -> None:
    """The §3.5 source-never-mutated invariant must hold even when the
    source carries the full pipeline artifact set (video / audio /
    project_state / translation / etc.). Regression: the path-rewriting
    and project_state pruning steps both load source JSON files; if
    either re-wrote those files in place instead of only to the target,
    the source's baseline would drift.
    """
    source = _make_source_project(tmp_path, n_segments=3)
    _add_media_artifacts(source)
    _add_metadata_and_transcript(source)
    _add_translation_segments(source, n_segments=3)
    _add_project_state(source)
    _populate_editing_dir(
        source,
        text_edits={"seg_001": "E1"},
        voice_map={"seg_002": {"provider": "cosyvoice", "voice_id": "v"}},
        draft_segments={"seg_003": b"DRAFT_3"},
    )
    before = _hash_tree(source)

    target = tmp_path / "copy_full"
    prepare_copy_project_dir(source, target)
    rollback_prepared_target(target)

    after = _hash_tree(source)
    assert before == after, (
        "source tree mutated by prepare + rollback with full artifacts. "
        "Check: path rewrite and project_state prune must read from "
        "source, write only to target."
    )
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
