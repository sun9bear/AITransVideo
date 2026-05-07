"""P0-8 (audit 2026-05-07) regression: split editing segment three-pack.

Audit reference:
    docs/audits/2026-05-07-comprehensive-codebase-audit.md
        B-CRITICAL-2 — _find_text_edits_without_tts skipped split segments
                       because their new ids were never in the baseline,
                       silently passing the audio-sync gate. Commit then
                       failed at alignment with confusing "missing wavs".
        B-HIGH-4    — split allowed mid_ms == start_ms / end_ms producing
                       a zero-duration half.
        B-HIGH-5    — split discarded the voice_map override of the parent
                       segment; both halves silently fell back to speaker
                       default voice.

Three regressions, three test groups:

§1  test_split_rejects_zero_duration_half — segment too short / ratio rounds
    to boundary → ValueError raised before any state mutation.

§2  test_split_migrates_voice_map_override — both new halves inherit the
    parent's voice_map entry; old sid removed.

§3  test_audio_sync_gate_catches_split_without_draft — full happy-path of
    "split + edit text + try to commit" still produces an
    EditingAudioSyncRequiredError listing both halves; same setup but
    with draft wavs present → unsynced is empty.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ====================================================================
# §1 — zero-duration guard
# ====================================================================


def _seed_one_segment(project_dir: Path, *, start_ms: int, end_ms: int,
                     source_text: str = "this is the source",
                     cn_text: str = "原文文本测试用") -> None:
    from services.jobs.editing import EDITING_SUBDIR

    editing_dir = project_dir / EDITING_SUBDIR
    editing_dir.mkdir(parents=True, exist_ok=True)
    seg = {
        "segment_id": "seg_001",
        "source_text": source_text,
        "cn_text": cn_text,
        "speaker_id": "spk_a",
        "start_ms": start_ms,
        "end_ms": end_ms,
        "voice_id": "v1",
        "tts_provider": "minimax",
    }
    (editing_dir / "segments.json").write_text(
        json.dumps([seg], ensure_ascii=False), encoding="utf-8",
    )


def test_split_rejects_zero_duration_half(tmp_path):
    """A 1ms-wide segment cannot be split — both halves would round to 0ms.
    Reject up-front rather than letting downstream alignment crash."""
    from services.jobs.editing_segments import split_editing_segment

    _seed_one_segment(tmp_path, start_ms=1000, end_ms=1001,
                     source_text="ab", cn_text="字符")

    with pytest.raises(ValueError, match="zero-duration half"):
        split_editing_segment(
            tmp_path,
            segment_id="seg_001",
            split_source_index=1,
            split_cn_index=1,
            speaker_a="spk_a",
            speaker_b="spk_a",
        )


def test_split_rejects_when_ratio_rounds_to_end(tmp_path):
    """Even with non-trivial duration, an extreme split position that rounds
    mid_ms onto end_ms is rejected (no zero-duration half on the B side)."""
    from services.jobs.editing_segments import split_editing_segment

    # 100-char source, 1000ms duration → split at index 99 → ratio = 0.99
    # mid_ms = 0 + round(1000 * 0.99) = 990 (not on boundary, OK)
    # We need a setup where the math forces mid_ms == end_ms. Use a
    # tiny duration so rounding bites: 2ms wide, split index 2 of 3 → ratio = 0.667
    # mid_ms = 0 + round(2 * 0.667) = round(1.33) = 1 → OK actually.
    # Use a 1-char source: split_source_index=0 is rejected by the
    # earlier guard (must be > 0). So we keep this case for the
    # zero-source-text fallback (ratio=0.5):
    # 2ms wide, source_text="" → ratio=0.5 → mid_ms=1. Not boundary.
    # The simpler trigger is start_ms == end_ms → mid_ms == both.
    _seed_one_segment(tmp_path, start_ms=500, end_ms=500,
                     source_text="abcde", cn_text="一二三四五")

    with pytest.raises(ValueError, match="zero-duration half"):
        split_editing_segment(
            tmp_path,
            segment_id="seg_001",
            split_source_index=2,
            split_cn_index=2,
            speaker_a="spk_a",
            speaker_b="spk_a",
        )


def test_split_succeeds_on_normal_duration(tmp_path):
    """Sanity: a normal split (long-enough segment, mid-position) does
    NOT trigger the zero-duration guard."""
    from services.jobs.editing_segments import split_editing_segment

    _seed_one_segment(tmp_path, start_ms=0, end_ms=2000,
                     source_text="abcdefghij", cn_text="一二三四五六七八九十")

    result = split_editing_segment(
        tmp_path,
        segment_id="seg_001",
        split_source_index=5,
        split_cn_index=5,
        speaker_a="spk_a",
        speaker_b="spk_a",
    )
    assert result["replaced_segment_id"] == "seg_001"
    assert len(result["new_segments"]) == 2


# ====================================================================
# §2 — voice_map migration
# ====================================================================


def test_split_migrates_voice_map_override_to_both_halves(tmp_path):
    """When seg_001 has a user-picked voice override and the user splits
    the segment, both halves must inherit the override; the old sid key
    must be removed so commit's voice_map merge does not leave a dangling
    reference."""
    from services.jobs.editing import EDITING_SUBDIR
    from services.jobs.editing_segments import split_editing_segment
    from services.jobs.editing_voice_map import set_voice_override, load_voice_map

    _seed_one_segment(tmp_path, start_ms=0, end_ms=2000,
                     source_text="abcdefghij", cn_text="一二三四五六七八九十")

    # User picks a non-default voice for the parent segment.
    set_voice_override(tmp_path, "seg_001",
                      provider="minimax", voice_id="cloned_voice_xyz")

    voice_map = load_voice_map(tmp_path)
    assert voice_map["seg_001"]["voice_id"] == "cloned_voice_xyz"

    result = split_editing_segment(
        tmp_path,
        segment_id="seg_001",
        split_source_index=5,
        split_cn_index=5,
        speaker_a="spk_a",
        speaker_b="spk_a",
    )
    new_id_a = result["new_segments"][0]["segment_id"]
    new_id_b = result["new_segments"][1]["segment_id"]

    voice_map_after = load_voice_map(tmp_path)
    assert "seg_001" not in voice_map_after, (
        "P0-8 regression: voice_map still has the orphan parent sid after split"
    )
    assert voice_map_after.get(new_id_a, {}).get("voice_id") == "cloned_voice_xyz", (
        f"P0-8 regression: voice_map override NOT migrated to first half "
        f"(new_id_a={new_id_a}); user's voice selection silently lost"
    )
    assert voice_map_after.get(new_id_b, {}).get("voice_id") == "cloned_voice_xyz", (
        f"P0-8 regression: voice_map override NOT migrated to second half "
        f"(new_id_b={new_id_b}); user's voice selection silently lost"
    )
    assert voice_map_after[new_id_a]["provider"] == "minimax"
    assert voice_map_after[new_id_b]["provider"] == "minimax"


def test_split_with_no_voice_map_override_leaves_voice_map_unchanged(tmp_path):
    """If the parent had no voice_map entry, split must not magically
    create one for the halves."""
    from services.jobs.editing_segments import split_editing_segment
    from services.jobs.editing_voice_map import load_voice_map

    _seed_one_segment(tmp_path, start_ms=0, end_ms=2000,
                     source_text="abcdefghij", cn_text="一二三四五六七八九十")

    assert load_voice_map(tmp_path) == {}

    split_editing_segment(
        tmp_path,
        segment_id="seg_001",
        split_source_index=5,
        split_cn_index=5,
        speaker_a="spk_a",
        speaker_b="spk_a",
    )
    assert load_voice_map(tmp_path) == {}


def test_split_removes_orphan_parent_draft_wav(tmp_path):
    """P1-16 (Codex P0-8 review): if the parent segment had a regenerated
    draft wav at the time of split, that file becomes an orphan after
    the split (no segment with parent_sid exists anymore). Commit's
    draft-promotion phase would copy it to editor/tts_segments/, leaving
    stale audio. Verify split now cleans up the orphan."""
    from services.jobs.editing import EDITING_SUBDIR
    from services.jobs.editing_segments import split_editing_segment

    _seed_one_segment(tmp_path, start_ms=0, end_ms=2000,
                     source_text="abcdefghij", cn_text="一二三四五六七八九十")

    # Simulate a previous regenerate-tts: a draft wav for seg_001
    drafts_dir = tmp_path / EDITING_SUBDIR / "tts_segments_draft"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    parent_draft = drafts_dir / "seg_001.wav"
    parent_draft.write_bytes(b"\x00\x00fake-wav-bytes\x00")
    assert parent_draft.exists()

    split_editing_segment(
        tmp_path,
        segment_id="seg_001",
        split_source_index=5,
        split_cn_index=5,
        speaker_a="spk_a",
        speaker_b="spk_a",
    )

    assert not parent_draft.exists(), (
        "P1-16 regression: orphan parent draft wav was NOT removed by "
        "split. commit's draft-promotion would copy seg_001.wav back to "
        "editor/tts_segments/, leaving stale audio for a segment id that "
        "no longer exists."
    )


def test_split_with_no_parent_draft_wav_does_not_raise(tmp_path):
    """Sanity: if there's no parent draft wav, split must not crash on
    the unlink — it should be a no-op."""
    from services.jobs.editing_segments import split_editing_segment

    _seed_one_segment(tmp_path, start_ms=0, end_ms=2000,
                     source_text="abcdefghij", cn_text="一二三四五六七八九十")

    # No drafts_dir, no draft wav — should still work
    result = split_editing_segment(
        tmp_path,
        segment_id="seg_001",
        split_source_index=5,
        split_cn_index=5,
        speaker_a="spk_a",
        speaker_b="spk_a",
    )
    assert len(result["new_segments"]) == 2


# ====================================================================
# §3 — audio-sync gate covers split halves
# ====================================================================


def _seed_full_editing_state(project_dir: Path) -> tuple[str, str]:
    """Create a project_dir with both editor/segments.json (baseline) and
    editor/editing/segments.json (editable copy). Returns (new_id_a,
    new_id_b) of a freshly-split segment."""
    from services.jobs.editing import EDITING_SUBDIR
    from services.jobs.editing_segments import split_editing_segment

    editor_dir = project_dir / "editor"
    editor_dir.mkdir(parents=True, exist_ok=True)
    editing_dir = project_dir / EDITING_SUBDIR
    editing_dir.mkdir(parents=True, exist_ok=True)

    parent_seg = {
        "segment_id": "seg_001",
        "source_text": "this is the source line",
        "cn_text": "这是源文本一行",
        "tts_input_cn_text": "这是源文本一行",
        "speaker_id": "spk_a",
        "start_ms": 0,
        "end_ms": 4000,
        "voice_id": "v1",
        "tts_provider": "minimax",
    }
    # baseline (editor/segments.json) — pre-edit snapshot
    (editor_dir / "segments.json").write_text(
        json.dumps([parent_seg], ensure_ascii=False), encoding="utf-8",
    )
    # editable copy (editor/editing/segments.json) — split from this
    (editing_dir / "segments.json").write_text(
        json.dumps([parent_seg], ensure_ascii=False), encoding="utf-8",
    )

    result = split_editing_segment(
        project_dir,
        segment_id="seg_001",
        split_source_index=10,
        split_cn_index=4,
        speaker_a="spk_a",
        speaker_b="spk_a",
    )
    return (
        result["new_segments"][0]["segment_id"],
        result["new_segments"][1]["segment_id"],
    )


def test_audio_sync_gate_catches_split_segment_without_draft_wav(tmp_path):
    """Full P0-8 happy-path scenario: user splits a segment, edits both
    halves' text, but does NOT regenerate TTS. The audio-sync gate must
    surface BOTH halves in unsynced; previously the gate silently
    skipped them (B-CRITICAL-2)."""
    from services.jobs.editing_commit import (
        EditingAudioSyncRequiredError,
        _require_text_audio_sync_before_commit,
    )

    new_id_a, new_id_b = _seed_full_editing_state(tmp_path)

    # The split itself already marks both halves text_dirty + has no
    # baseline entries for them. _require_text_audio_sync_before_commit
    # MUST now raise.
    with pytest.raises(EditingAudioSyncRequiredError) as exc_info:
        _require_text_audio_sync_before_commit(tmp_path)

    surfaced_ids = {item["segment_id"] for item in exc_info.value.unsynced_segments}
    assert new_id_a in surfaced_ids, (
        f"P0-8 regression: audio-sync gate did not surface split half "
        f"{new_id_a}. surfaced_ids={surfaced_ids}"
    )
    assert new_id_b in surfaced_ids, (
        f"P0-8 regression: audio-sync gate did not surface split half "
        f"{new_id_b}. surfaced_ids={surfaced_ids}"
    )


def test_audio_sync_gate_passes_split_segment_with_draft_wav(tmp_path):
    """Same scenario as above but the user HAS regenerated a draft wav
    for both halves. Gate should now pass — the audio represents the
    current text."""
    from services.jobs.editing import EDITING_SUBDIR
    from services.jobs.editing_commit import _require_text_audio_sync_before_commit

    new_id_a, new_id_b = _seed_full_editing_state(tmp_path)

    drafts_dir = tmp_path / EDITING_SUBDIR / "tts_segments_draft"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    # Empty placeholder wavs — _find_text_edits_without_tts only checks
    # is_file, not contents.
    (drafts_dir / f"{new_id_a}.wav").write_bytes(b"\x00")
    (drafts_dir / f"{new_id_b}.wav").write_bytes(b"\x00")

    # Should NOT raise.
    _require_text_audio_sync_before_commit(tmp_path)
