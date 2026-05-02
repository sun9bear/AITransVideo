"""T1-5 — single-segment TTS regenerate + accept/discard draft.

Tests exercise the business logic directly (no HTTP). The default
``tts_caller`` is ``_not_wired_tts_caller`` which raises
``TtsNotWiredError``; we inject fake callers in each regenerate test so
no real TTS provider is invoked (paid-API safety per CLAUDE.md).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.jobs.editing import EDITING_SUBDIR, enter_editing
from services.jobs.editing_segments import (
    SEGMENT_STATUS_ACCEPTED,
    SEGMENT_STATUS_TTS_DIRTY,
    SEGMENT_STATUS_TTS_FAILED,
    SEGMENT_STATUS_TTS_LOADING,
    load_segment_status,
)
from services.jobs.editing_tts import (
    DRAFT_TTS_SUBDIR,
    TtsNotWiredError,
    accept_draft_tts,
    discard_draft_tts,
    draft_audio_path,
    regenerate_segment_tts,
)
from services.jobs.editing import EditingConflictError
from services.jobs.models import JOB_STATUS_SUCCEEDED, JobRecord
from services.jobs.service import JobService
from services.jobs.store import JobStore


class _NullRunner:
    pass


def _build_editing_job(tmp_path: Path) -> tuple[JobService, Path]:
    """Build a Studio editing-state job with 2 segments + baseline audio files."""
    project_dir = tmp_path / "projects" / "job_xyz"
    editor_dir = project_dir / "editor"
    editor_dir.mkdir(parents=True)
    (editor_dir / "tts_segments").mkdir()
    # Baseline audio — must remain untouched throughout editing
    (editor_dir / "tts_segments" / "seg_001.wav").write_bytes(b"BASELINE_001")
    (editor_dir / "tts_segments" / "seg_002.wav").write_bytes(b"BASELINE_002")
    (editor_dir / "segments.json").write_text(
        json.dumps([
            {"segment_id": "seg_001", "speaker_id": "A", "cn_text": "你好",
             "start_ms": 0, "end_ms": 1000, "voice_id": "voice_a"},
            {"segment_id": "seg_002", "speaker_id": "B", "cn_text": "世界",
             "start_ms": 1000, "end_ms": 2000, "voice_id": "voice_b"},
        ], ensure_ascii=False),
        encoding="utf-8",
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    record = JobRecord(
        job_id="job_xyz",
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
    enter_editing(record, store)
    service = JobService(store=store, runner=_NullRunner())
    return service, project_dir


def _fake_tts_caller_factory(fake_bytes: bytes = b"DRAFT_WAV"):
    """Produces a tts_caller that writes ``fake_bytes`` to the output path.
    Captures invocation count + seen segment ids so tests can assert."""
    calls: list[tuple[str, Path]] = []

    def caller(segment: dict, output_path: Path) -> None:
        calls.append((segment["segment_id"], output_path))
        output_path.write_bytes(fake_bytes)

    caller.calls = calls  # type: ignore[attr-defined]
    return caller


# ---------------------------------------------------------------------------
# Default caller refuses (paid-API guard)
# ---------------------------------------------------------------------------


def test_regenerate_default_caller_raises_not_wired(tmp_path: Path) -> None:
    _, project_dir = _build_editing_job(tmp_path)

    with pytest.raises(TtsNotWiredError):
        regenerate_segment_tts(project_dir, "seg_001")
    # Status must reflect the failure — not loading
    status = load_segment_status(project_dir)
    assert status.get("seg_001") == SEGMENT_STATUS_TTS_FAILED


def test_not_wired_error_is_subclass_of_not_implemented() -> None:
    """Lets the API layer keep a single ``except NotImplementedError → 501``
    dispatcher."""
    assert issubclass(TtsNotWiredError, NotImplementedError)


# ---------------------------------------------------------------------------
# Happy path with injected caller
# ---------------------------------------------------------------------------


def test_regenerate_writes_draft_and_flags_tts_dirty(tmp_path: Path) -> None:
    _, project_dir = _build_editing_job(tmp_path)
    caller = _fake_tts_caller_factory(b"NEW_WAV_001")

    result = regenerate_segment_tts(project_dir, "seg_001", tts_caller=caller)

    draft = draft_audio_path(project_dir, "seg_001")
    assert draft.is_file()
    assert draft.read_bytes() == b"NEW_WAV_001"
    assert result["segment_id"] == "seg_001"
    assert result["size_bytes"] == len(b"NEW_WAV_001")
    # Baseline never touched
    assert (project_dir / "editor" / "tts_segments" / "seg_001.wav").read_bytes() == b"BASELINE_001"
    # Status flagged tts_dirty
    status = load_segment_status(project_dir)
    assert status.get("seg_001") == SEGMENT_STATUS_TTS_DIRTY
    # Caller saw one invocation with correct segment
    assert len(caller.calls) == 1
    sid, out_path = caller.calls[0]
    assert sid == "seg_001"
    assert out_path == draft


def test_regenerate_fits_draft_audio_to_segment_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, project_dir = _build_editing_job(tmp_path)
    calls: list[tuple[Path, int]] = []

    def fake_fit(path: Path, *, slot_duration_ms: int):
        calls.append((path, slot_duration_ms))
        path.write_bytes(b"FITTED_TO_SLOT")
        return None

    monkeypatch.setattr("services.jobs.editing_tts.fit_audio_to_slot", fake_fit)
    caller = _fake_tts_caller_factory(b"RAW_TTS_TOO_LONG")

    result = regenerate_segment_tts(project_dir, "seg_001", tts_caller=caller)

    draft = draft_audio_path(project_dir, "seg_001")
    assert calls == [(draft, 1000)]
    assert draft.read_bytes() == b"FITTED_TO_SLOT"
    assert result["size_bytes"] == len(b"FITTED_TO_SLOT")


def test_regenerate_overlays_voice_map_override_onto_segment(tmp_path: Path) -> None:
    """CodeX A.2 P1 regression. After set_voice_override, the caller must
    receive a segment dict whose tts_provider + voice_id reflect the voice
    map entry, NOT the baseline from editing/segments.json. Without this,
    the Phase 2 voice-modify Tab would silently regenerate the old voice.

    Critical: voice_map is never merged back into editing/segments.json
    during the editing session (that only happens at commit time), so the
    overlay must be applied every call."""
    from services.jobs.editing_voice_map import set_voice_override

    _, project_dir = _build_editing_job(tmp_path)
    set_voice_override(
        project_dir,
        "seg_001",
        provider="volcengine",
        voice_id="override_voice_xyz",
    )

    seen_segments: list[dict] = []

    def recording_caller(segment: dict, output_path: Path) -> None:
        seen_segments.append(dict(segment))
        output_path.write_bytes(b"OVERRIDE_WAV")

    regenerate_segment_tts(project_dir, "seg_001", tts_caller=recording_caller)

    assert len(seen_segments) == 1
    received = seen_segments[0]
    assert received["tts_provider"] == "volcengine", (
        "voice_map override must drive tts_provider sent to caller"
    )
    assert received["voice_id"] == "override_voice_xyz", (
        "voice_map override must drive voice_id sent to caller"
    )
    # Baseline editing/segments.json must NOT have been mutated — the
    # overlay is a per-call shallow copy.
    baseline_json = json.loads(
        (project_dir / "editor" / "editing" / "segments.json").read_text(encoding="utf-8")
    )
    seg_001 = next(s for s in baseline_json if s["segment_id"] == "seg_001")
    assert seg_001["voice_id"] == "voice_a", (
        "editing/segments.json baseline must stay clean — override lives "
        "in voice_map.json only until commit merges them"
    )
    assert "tts_provider" not in seg_001 or seg_001.get("tts_provider") != "volcengine"


def test_regenerate_skips_voice_overlay_when_no_override(tmp_path: Path) -> None:
    """Sanity: without set_voice_override, the caller still sees the
    baseline segment unchanged — overlay is opt-in per voice_map entry."""
    _, project_dir = _build_editing_job(tmp_path)

    seen_segments: list[dict] = []

    def recording_caller(segment: dict, output_path: Path) -> None:
        seen_segments.append(dict(segment))
        output_path.write_bytes(b"BASELINE_WAV")

    regenerate_segment_tts(project_dir, "seg_001", tts_caller=recording_caller)

    assert seen_segments[0]["voice_id"] == "voice_a"
    # Fixture baseline doesn't set tts_provider, so the received segment
    # must not acquire one out of thin air either.
    assert "tts_provider" not in seen_segments[0]


def test_regenerate_uses_current_speaker_voice_for_stale_split_segment(
    tmp_path: Path,
) -> None:
    """Regression for split+speaker-change edits: an already-created split
    segment may show the new speaker_id while still carrying old voice fields.
    Re-TTS should follow the current speaker before calling the provider."""
    _, project_dir = _build_editing_job(tmp_path)
    editing_segments = project_dir / "editor" / "editing" / "segments.json"
    editing_segments.write_text(
        json.dumps([
            {
                "segment_id": "11_b",
                "speaker_id": "B",
                "cn_text": "新拆分段",
                "source_text": "split segment",
                "start_ms": 0,
                "end_ms": 1000,
                "target_duration_ms": 1000,
                "voice_id": "voice_a_old",
                "tts_provider": "minimax",
            },
            {
                "segment_id": "seg_002",
                "speaker_id": "B",
                "cn_text": "参考段",
                "source_text": "reference",
                "start_ms": 1000,
                "end_ms": 2000,
                "target_duration_ms": 1000,
                "voice_id": "voice_b",
                "tts_provider": "cosyvoice",
            },
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    seen_segments: list[dict] = []

    def recording_caller(segment: dict, output_path: Path) -> None:
        seen_segments.append(dict(segment))
        output_path.write_bytes(b"SPLIT_WAV")

    regenerate_segment_tts(project_dir, "11_b", tts_caller=recording_caller)

    assert seen_segments[0]["speaker_id"] == "B"
    assert seen_segments[0]["voice_id"] == "voice_b"
    assert seen_segments[0]["tts_provider"] == "cosyvoice"


def test_regenerate_two_segments_isolated(tmp_path: Path) -> None:
    _, project_dir = _build_editing_job(tmp_path)
    caller = _fake_tts_caller_factory()

    regenerate_segment_tts(project_dir, "seg_001", tts_caller=caller)
    regenerate_segment_tts(project_dir, "seg_002", tts_caller=caller)

    assert draft_audio_path(project_dir, "seg_001").is_file()
    assert draft_audio_path(project_dir, "seg_002").is_file()
    status = load_segment_status(project_dir)
    assert status == {"seg_001": "tts_dirty", "seg_002": "tts_dirty"}


# ---------------------------------------------------------------------------
# Failures are surfaced + status marked
# ---------------------------------------------------------------------------


def test_regenerate_caller_exception_flags_tts_failed(tmp_path: Path) -> None:
    _, project_dir = _build_editing_job(tmp_path)

    def angry_caller(segment, output_path):
        raise RuntimeError("upstream 429")

    with pytest.raises(RuntimeError, match="upstream 429"):
        regenerate_segment_tts(project_dir, "seg_001", tts_caller=angry_caller)
    status = load_segment_status(project_dir)
    assert status.get("seg_001") == SEGMENT_STATUS_TTS_FAILED
    # No draft file left behind
    assert not draft_audio_path(project_dir, "seg_001").exists()


def test_regenerate_caller_returns_without_writing_file(tmp_path: Path) -> None:
    _, project_dir = _build_editing_job(tmp_path)

    def silent_caller(segment, output_path):
        # Caller returns without creating the file — we treat this as failure
        return None

    with pytest.raises(EditingConflictError, match="returned without writing output"):
        regenerate_segment_tts(project_dir, "seg_001", tts_caller=silent_caller)
    status = load_segment_status(project_dir)
    assert status.get("seg_001") == SEGMENT_STATUS_TTS_FAILED


def test_regenerate_unknown_segment_raises_conflict(tmp_path: Path) -> None:
    _, project_dir = _build_editing_job(tmp_path)
    caller = _fake_tts_caller_factory()

    with pytest.raises(EditingConflictError, match="not found"):
        regenerate_segment_tts(project_dir, "seg_999", tts_caller=caller)


def test_regenerate_rejects_bad_segment_id_before_fs(tmp_path: Path) -> None:
    _, project_dir = _build_editing_job(tmp_path)
    with pytest.raises(ValueError, match="invalid segment_id"):
        regenerate_segment_tts(project_dir, "../hack", tts_caller=lambda s, p: None)


# ---------------------------------------------------------------------------
# Accept / discard
# ---------------------------------------------------------------------------


def test_accept_keeps_draft_and_clears_status(tmp_path: Path) -> None:
    _, project_dir = _build_editing_job(tmp_path)
    caller = _fake_tts_caller_factory(b"NEW")
    regenerate_segment_tts(project_dir, "seg_001", tts_caller=caller)

    result = accept_draft_tts(project_dir, "seg_001")

    assert result["action"] == "accepted"
    assert result["segment_status"] == {}  # cleared
    # Draft still exists
    assert draft_audio_path(project_dir, "seg_001").is_file()
    # Baseline still unchanged
    assert (project_dir / "editor" / "tts_segments" / "seg_001.wav").read_bytes() == b"BASELINE_001"


def test_accept_without_draft_raises(tmp_path: Path) -> None:
    _, project_dir = _build_editing_job(tmp_path)
    with pytest.raises(EditingConflictError, match="no draft audio to accept"):
        accept_draft_tts(project_dir, "seg_001")


def test_discard_deletes_draft_and_clears_status(tmp_path: Path) -> None:
    _, project_dir = _build_editing_job(tmp_path)
    caller = _fake_tts_caller_factory(b"NEW")
    regenerate_segment_tts(project_dir, "seg_001", tts_caller=caller)
    draft = draft_audio_path(project_dir, "seg_001")
    assert draft.is_file()

    result = discard_draft_tts(project_dir, "seg_001")

    assert result["action"] == "discarded"
    assert not draft.exists()
    assert result["segment_status"] == {}
    # Baseline untouched → segment now effectively uses baseline audio
    assert (project_dir / "editor" / "tts_segments" / "seg_001.wav").read_bytes() == b"BASELINE_001"


def test_discard_idempotent_on_missing_draft(tmp_path: Path) -> None:
    _, project_dir = _build_editing_job(tmp_path)
    # No draft exists; discard should succeed without raising (admin force-cancel
    # reuses this path and must tolerate missing files).
    result = discard_draft_tts(project_dir, "seg_001")
    assert result["action"] == "discarded"
    assert result["segment_status"] == {}


# ---------------------------------------------------------------------------
# Bug (Claude Code ultrareview #3, CodeX P1 silent-data-error):
# discard_draft_tts unconditionally writes SEGMENT_STATUS_ACCEPTED, even
# when the text edit or voice override that triggered the regen is
# still in place. Demoting to accepted hides the remaining dirt from
# batch re-TTS → commit uses baseline audio against an edited cn_text.
#
# Fix: compute the residual dirty state after deleting the draft —
# voice_dirty if voice_map still has an override / text_dirty if
# editing cn_text still differs from baseline / else accepted.
# ---------------------------------------------------------------------------


def test_discard_draft_preserves_text_dirty_when_cn_text_differs(
    tmp_path: Path,
) -> None:
    """User edits text, regens TTS, then discards the draft: segment
    must revert to text_dirty (baseline audio no longer matches the
    edited text)."""
    from services.jobs.editing_segments import patch_editing_segment

    _, project_dir = _build_editing_job(tmp_path)
    patch_editing_segment(project_dir, "seg_001", {"cn_text": "new edited"})
    caller = _fake_tts_caller_factory(b"DRAFT_AFTER_EDIT")
    regenerate_segment_tts(project_dir, "seg_001", tts_caller=caller)
    # After regen, status = tts_dirty (clobbered over text_dirty)
    assert load_segment_status(project_dir).get("seg_001") == SEGMENT_STATUS_TTS_DIRTY

    discard_draft_tts(project_dir, "seg_001")

    status = load_segment_status(project_dir)
    from services.jobs.editing_segments import SEGMENT_STATUS_TEXT_DIRTY
    assert status == {"seg_001": SEGMENT_STATUS_TEXT_DIRTY}, (
        f"discard_draft_tts stamped segment_status to {status!r} — user's "
        "text edit is now invisible to batch re-TTS (silent data loss)"
    )


def test_discard_draft_preserves_voice_dirty_when_voice_override_remains(
    tmp_path: Path,
) -> None:
    """User changes voice, regens TTS, then discards the draft: segment
    must revert to voice_dirty so batch re-TTS will use the override."""
    from services.jobs.editing_voice_map import set_voice_override
    from services.jobs.editing_segments import SEGMENT_STATUS_VOICE_DIRTY

    _, project_dir = _build_editing_job(tmp_path)
    set_voice_override(project_dir, "seg_001", provider="minimax", voice_id="v1")
    caller = _fake_tts_caller_factory(b"DRAFT_WITH_OVERRIDE")
    regenerate_segment_tts(project_dir, "seg_001", tts_caller=caller)
    assert load_segment_status(project_dir).get("seg_001") == SEGMENT_STATUS_TTS_DIRTY

    discard_draft_tts(project_dir, "seg_001")

    status = load_segment_status(project_dir)
    assert status == {"seg_001": SEGMENT_STATUS_VOICE_DIRTY}, (
        f"discard_draft_tts stamped segment_status to {status!r} — user's "
        "voice override is no longer flagged as dirty (batch re-TTS skips it)"
    )


def test_discard_draft_goes_to_accepted_when_no_residual_dirt(
    tmp_path: Path,
) -> None:
    """Baseline regression: a draft regenerated without any text edit or
    voice change (force regen of a clean segment) discards to accepted."""
    _, project_dir = _build_editing_job(tmp_path)
    caller = _fake_tts_caller_factory(b"REGEN_CLEAN")
    regenerate_segment_tts(project_dir, "seg_001", tts_caller=caller)

    discard_draft_tts(project_dir, "seg_001")

    assert load_segment_status(project_dir) == {}


def test_accept_and_discard_reject_bad_sid(tmp_path: Path) -> None:
    _, project_dir = _build_editing_job(tmp_path)
    with pytest.raises(ValueError, match="invalid segment_id"):
        accept_draft_tts(project_dir, "../x")
    with pytest.raises(ValueError, match="invalid segment_id"):
        discard_draft_tts(project_dir, "../x")


# ---------------------------------------------------------------------------
# JobService delegates refresh touched_at
# ---------------------------------------------------------------------------


def test_service_regenerate_default_is_not_wired(tmp_path: Path) -> None:
    service, _ = _build_editing_job(tmp_path)
    with pytest.raises(TtsNotWiredError):
        service.regenerate_segment_tts("job_xyz", "seg_001")


def test_service_regenerate_with_caller_refreshes_touched_at(tmp_path: Path) -> None:
    import time as _time

    service, _ = _build_editing_job(tmp_path)
    before = service.require_job("job_xyz").editing_touched_at
    _time.sleep(0.005)
    caller = _fake_tts_caller_factory(b"X")
    service.regenerate_segment_tts("job_xyz", "seg_001", tts_caller=caller)
    after = service.require_job("job_xyz").editing_touched_at
    assert after > before


def test_service_accept_and_discard_refresh_touched_at(tmp_path: Path) -> None:
    import time as _time

    service, project_dir = _build_editing_job(tmp_path)
    caller = _fake_tts_caller_factory(b"Y")
    service.regenerate_segment_tts("job_xyz", "seg_001", tts_caller=caller)

    before = service.require_job("job_xyz").editing_touched_at
    _time.sleep(0.005)
    service.accept_segment_draft_tts("job_xyz", "seg_001")
    after = service.require_job("job_xyz").editing_touched_at
    assert after > before

    # Produce a new draft to discard
    service.regenerate_segment_tts("job_xyz", "seg_001", tts_caller=caller)
    before2 = service.require_job("job_xyz").editing_touched_at
    _time.sleep(0.005)
    service.discard_segment_draft_tts("job_xyz", "seg_001")
    after2 = service.require_job("job_xyz").editing_touched_at
    assert after2 > before2


def test_service_regenerate_non_editing_rejected(tmp_path: Path) -> None:
    from dataclasses import replace

    service, _ = _build_editing_job(tmp_path)
    record = service.require_job("job_xyz")
    service.store.save_job(replace(record, status=JOB_STATUS_SUCCEEDED))

    with pytest.raises(EditingConflictError, match="not in editing state"):
        service.regenerate_segment_tts("job_xyz", "seg_001")


# ---------------------------------------------------------------------------
# Invariant: baseline tts_segments/ mtime + hash unchanged after any editing_tts call
# ---------------------------------------------------------------------------


def test_baseline_tts_segments_never_mutated(tmp_path: Path) -> None:
    """Guard the critical §3.5 invariant: baseline audio stays untouched
    no matter which editing_tts function runs."""
    import hashlib

    service, project_dir = _build_editing_job(tmp_path)
    baseline_path = project_dir / "editor" / "tts_segments" / "seg_001.wav"
    sha_before = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
    mtime_before = baseline_path.stat().st_mtime

    caller = _fake_tts_caller_factory(b"DRAFT")
    # Exercise every editing_tts entry point
    service.regenerate_segment_tts("job_xyz", "seg_001", tts_caller=caller)
    service.accept_segment_draft_tts("job_xyz", "seg_001")
    service.regenerate_segment_tts("job_xyz", "seg_001", tts_caller=caller)
    service.discard_segment_draft_tts("job_xyz", "seg_001")

    sha_after = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
    mtime_after = baseline_path.stat().st_mtime
    assert sha_after == sha_before, "baseline audio was mutated by editing_tts"
    assert mtime_after == mtime_before, "baseline mtime was touched by editing_tts"
