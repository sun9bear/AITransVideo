"""T1-2 — editing segments CRUD tests.

Covers:
- input_validators.validate_segment_id (allowlist regex)
- editing_segments.load_editing_segments / load_segment_status / editing_payload
- editing_segments.patch_editing_segment (cn_text + translation_confirmed +
  rewrite_requested; silently drops non-patchable keys; auto-flags text_dirty)
- editing_segments.mark_segment_status (setting + clearing for accepted)
- JobService delegates (get_editing_segments / patch_editing_segment /
  mark_editing_segment_status) with touched_at refresh + editing-state check
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.jobs.editing import (
    EDITING_SUBDIR,
    EditingConflictError,
    enter_editing,
)
from services.jobs.editing_segments import (
    PATCHABLE_SEGMENT_FIELDS,
    SEGMENT_STATUS_ACCEPTED,
    SEGMENT_STATUS_TEXT_DIRTY,
    SEGMENT_STATUS_TTS_DIRTY,
    SUPPORTED_SEGMENT_STATUSES,
    editing_payload,
    load_editing_segments,
    load_segment_status,
    mark_segment_status,
    patch_editing_segment,
)
from services.jobs.input_validators import (
    SEGMENT_ID_RE,
    validate_commit_strategy,
    validate_segment_id,
)
from services.jobs.models import JOB_STATUS_EDITING, JOB_STATUS_SUCCEEDED, JobRecord
from services.jobs.service import JobService
from services.jobs.store import JobStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _NullRunner:
    """JobService.__init__ only stores the runner; editing delegates never
    touch its attributes, so a bare class is enough."""


def _build_editing_job(tmp_path: Path) -> tuple[JobService, Path, JobRecord]:
    """Returns (service, project_dir, editing_record). The baseline
    segments.json has 3 segments we can patch against."""
    project_dir = tmp_path / "projects" / "job_abc"
    (project_dir / "editor").mkdir(parents=True)
    baseline_segments = [
        {
            "segment_id": "seg_001",
            "speaker_id": "A",
            "cn_text": "你好",
            "source_text": "hello",
            "start_ms": 0,
            "end_ms": 1000,
        },
        {
            "segment_id": "seg_002",
            "speaker_id": "B",
            "cn_text": "世界",
            "source_text": "world",
            "start_ms": 1000,
            "end_ms": 2000,
        },
        {
            "segment_id": "seg_003",
            "speaker_id": "A",
            "cn_text": "再见",
            "source_text": "goodbye",
            "start_ms": 2000,
            "end_ms": 3000,
        },
    ]
    (project_dir / "editor" / "segments.json").write_text(
        json.dumps(baseline_segments, ensure_ascii=False), encoding="utf-8"
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    record = JobRecord(
        job_id="job_abc",
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
    service = JobService(store=store, runner=_NullRunner())
    return service, project_dir, editing_record


# ---------------------------------------------------------------------------
# validate_segment_id / SEGMENT_ID_RE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ok",
    ["seg_001", "s1", "abc", "1", "seg_" + "a" * 60, "speaker_a_042"],
)
def test_validate_segment_id_accepts_allowlist(ok: str) -> None:
    assert validate_segment_id(ok) == ok


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "Seg_001",        # uppercase
        "seg-001",        # hyphen
        "seg.001",        # dot
        "seg/001",        # slash
        "seg\\001",       # backslash
        "../etc/passwd",  # traversal
        "seg_" + "a" * 61,  # too long (65 chars)
        "seg_001 ",       # trailing space
        " seg_001",       # leading space
    ],
)
def test_validate_segment_id_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid segment_id"):
        validate_segment_id(bad)


def test_validate_segment_id_rejects_non_string() -> None:
    with pytest.raises(ValueError, match="must be a string"):
        validate_segment_id(42)  # type: ignore[arg-type]


def test_segment_id_regex_is_anchored() -> None:
    """Confirm the regex does not let ``seg_001/../x`` slip through as a
    substring match."""
    assert SEGMENT_ID_RE.match("seg_001/../x") is None


# ---------------------------------------------------------------------------
# validate_commit_strategy
# ---------------------------------------------------------------------------


def test_validate_commit_strategy_allows_overwrite_and_copy() -> None:
    assert validate_commit_strategy("overwrite") == "overwrite"
    assert validate_commit_strategy("copy_as_new") == "copy_as_new"


def test_validate_commit_strategy_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unsupported commit strategy"):
        validate_commit_strategy("force_push")


# ---------------------------------------------------------------------------
# editing_segments.load_* / editing_payload
# ---------------------------------------------------------------------------


def test_load_editing_segments_returns_list(tmp_path: Path) -> None:
    _, project_dir, _ = _build_editing_job(tmp_path)
    segments = load_editing_segments(project_dir)
    assert len(segments) == 3
    assert segments[0]["segment_id"] == "seg_001"
    assert segments[0]["cn_text"] == "你好"


def test_load_segment_status_missing_returns_empty(tmp_path: Path) -> None:
    _, project_dir, _ = _build_editing_job(tmp_path)
    # enter_editing doesn't pre-create segment_status.json
    assert load_segment_status(project_dir) == {}


def test_editing_payload_bundle(tmp_path: Path) -> None:
    _, project_dir, _ = _build_editing_job(tmp_path)
    payload = editing_payload(project_dir)
    assert payload["total"] == 3
    assert len(payload["segments"]) == 3
    assert payload["segment_status"] == {}


# ---------------------------------------------------------------------------
# patch_editing_segment
# ---------------------------------------------------------------------------


def test_patch_segment_updates_cn_text_and_flags_dirty(tmp_path: Path) -> None:
    _, project_dir, _ = _build_editing_job(tmp_path)

    updated = patch_editing_segment(
        project_dir, "seg_001", {"cn_text": "你好呀"}
    )

    assert updated["cn_text"] == "你好呀"
    # Reload from disk to confirm persistence
    segments = load_editing_segments(project_dir)
    assert segments[0]["cn_text"] == "你好呀"
    assert segments[1]["cn_text"] == "世界"  # untouched
    # Status auto-flagged
    status = load_segment_status(project_dir)
    assert status["seg_001"] == SEGMENT_STATUS_TEXT_DIRTY


def test_patch_segment_accepts_translation_confirmed(tmp_path: Path) -> None:
    _, project_dir, _ = _build_editing_job(tmp_path)
    updated = patch_editing_segment(
        project_dir, "seg_001", {"translation_confirmed": True}
    )
    assert updated["translation_confirmed"] is True
    # translation_confirmed alone does NOT flag text_dirty (TTS still valid)
    status = load_segment_status(project_dir)
    assert "seg_001" not in status


def test_patch_segment_silently_drops_non_patchable_fields(tmp_path: Path) -> None:
    _, project_dir, _ = _build_editing_job(tmp_path)
    updated = patch_editing_segment(
        project_dir, "seg_001",
        {"cn_text": "new", "voice_id": "X", "segment_id": "hacker", "start_ms": 99999},
    )
    # cn_text applied; others dropped
    assert updated["cn_text"] == "new"
    assert updated["segment_id"] == "seg_001"  # NOT "hacker"
    assert updated["start_ms"] == 0            # NOT 99999
    assert "voice_id" not in updated           # voice_id goes through voice_map path


def test_patch_segment_with_only_unknown_fields_raises(tmp_path: Path) -> None:
    _, project_dir, _ = _build_editing_job(tmp_path)
    with pytest.raises(ValueError, match="no patchable fields"):
        patch_editing_segment(project_dir, "seg_001", {"foo": "bar"})


def test_patch_segment_unknown_segment_id_raises_conflict(tmp_path: Path) -> None:
    _, project_dir, _ = _build_editing_job(tmp_path)
    with pytest.raises(EditingConflictError, match="not found"):
        patch_editing_segment(project_dir, "seg_999", {"cn_text": "x"})


def test_patch_segment_tolerates_legacy_integer_segment_id(tmp_path: Path) -> None:
    """Legacy editor/editing/segments.json snapshots (seeded before the
    normalisation patch landed) persisted segment_id as int because
    translation/segments.json carries integer ids. The HTTP layer always
    sends strings, so the lookup must str-cast both sides rather than
    relying on Python == between int and str."""
    project_dir = tmp_path / "projects" / "legacy_int_ids"
    (project_dir / "editor" / "editing" / "tts_segments_draft").mkdir(parents=True)
    (project_dir / "editor" / "editing" / "segments.json").write_text(
        json.dumps([
            {"segment_id": 1, "cn_text": "一"},
            {"segment_id": 4, "cn_text": "四"},
            {"segment_id": 10, "cn_text": "十"},
        ]),
        encoding="utf-8",
    )

    # HTTP layer sends the literal string '4'; must match the int 4 record.
    updated = patch_editing_segment(project_dir, "4", {"cn_text": "四改"})
    assert updated["cn_text"] == "四改"
    # segment_id survives in whatever shape it was — patch preserves existing
    # fields verbatim (normalisation happens at seed time, not patch time).
    assert updated["segment_id"] == 4


def test_patch_segment_rejects_bad_id_before_fs_access(tmp_path: Path) -> None:
    _, project_dir, _ = _build_editing_job(tmp_path)
    with pytest.raises(ValueError, match="invalid segment_id"):
        patch_editing_segment(project_dir, "../hack", {"cn_text": "x"})


def test_patch_segment_missing_editing_dir_raises(tmp_path: Path) -> None:
    _, project_dir, _ = _build_editing_job(tmp_path)
    # Blow away editing/ to simulate corruption
    import shutil
    shutil.rmtree(project_dir / EDITING_SUBDIR)
    with pytest.raises(EditingConflictError, match="editing dir does not exist"):
        patch_editing_segment(project_dir, "seg_001", {"cn_text": "x"})


# ---------------------------------------------------------------------------
# mark_segment_status
# ---------------------------------------------------------------------------


def test_mark_status_sets_and_reloads(tmp_path: Path) -> None:
    _, project_dir, _ = _build_editing_job(tmp_path)
    mark_segment_status(project_dir, "seg_001", SEGMENT_STATUS_TTS_DIRTY)
    assert load_segment_status(project_dir) == {"seg_001": "tts_dirty"}


def test_mark_status_accepted_clears_entry(tmp_path: Path) -> None:
    _, project_dir, _ = _build_editing_job(tmp_path)
    mark_segment_status(project_dir, "seg_001", SEGMENT_STATUS_TEXT_DIRTY)
    assert "seg_001" in load_segment_status(project_dir)
    mark_segment_status(project_dir, "seg_001", SEGMENT_STATUS_ACCEPTED)
    assert load_segment_status(project_dir) == {}


def test_mark_status_rejects_unknown_status(tmp_path: Path) -> None:
    _, project_dir, _ = _build_editing_job(tmp_path)
    with pytest.raises(ValueError, match="unsupported segment status"):
        mark_segment_status(project_dir, "seg_001", "bogus_status")


def test_supported_segment_statuses_contract() -> None:
    assert "accepted" in SUPPORTED_SEGMENT_STATUSES
    assert "text_dirty" in SUPPORTED_SEGMENT_STATUSES
    assert "tts_dirty" in SUPPORTED_SEGMENT_STATUSES
    assert "voice_dirty" in SUPPORTED_SEGMENT_STATUSES
    # Implicit accepted ≡ absent from map, so "accepted" cannot be stored
    # but IS a valid input (clears entry) — covered by the dedicated test.


# ---------------------------------------------------------------------------
# 2026-04-20: 修改说话人归属
#
# 用户场景："S2 审核时把第 5 段归错 speaker 了，editing 阶段想改正"。
#
# Semantics:
#   - `speaker_id` becomes patchable via the same PATCH endpoint.
#   - The new speaker_id MUST already exist in the task's segments
#     (no implicit creation of new speakers).
#   - **voice_id + tts_provider propagate automatically** from another
#     segment of the new speaker, so re-synthesis uses the new
#     speaker's voice without the user having to touch the voice Tab.
#   - voice_map override on this segment is cleared (the old override
#     was tied to the old speaker's voice pick).
#   - segment_status flips to voice_dirty → batch re-TTS picks it up.
# ---------------------------------------------------------------------------


def _build_editing_job_with_voices(tmp_path: Path) -> tuple[Path, list[dict]]:
    """Like _build_editing_job but segments carry voice_id + tts_provider
    so we can test propagation on speaker change."""
    project_dir = tmp_path / "projects" / "job_speaker_swap"
    (project_dir / "editor").mkdir(parents=True)
    baseline = [
        {
            "segment_id": "seg_001", "speaker_id": "speaker_a",
            "cn_text": "段一", "source_text": "one",
            "start_ms": 0, "end_ms": 1000,
            "voice_id": "voice_a_pro", "tts_provider": "minimax",
        },
        {
            "segment_id": "seg_002", "speaker_id": "speaker_b",
            "cn_text": "段二", "source_text": "two",
            "start_ms": 1000, "end_ms": 2000,
            "voice_id": "voice_b_radio", "tts_provider": "cosyvoice",
        },
        {
            "segment_id": "seg_003", "speaker_id": "speaker_a",
            "cn_text": "段三", "source_text": "three",
            "start_ms": 2000, "end_ms": 3000,
            "voice_id": "voice_a_pro", "tts_provider": "minimax",
        },
    ]
    (project_dir / "editor" / "segments.json").write_text(
        json.dumps(baseline, ensure_ascii=False), encoding="utf-8",
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    record = JobRecord(
        job_id="job_speaker_swap",
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
    return project_dir, baseline


def test_patch_speaker_id_propagates_baseline_voice_from_new_speaker(
    tmp_path: Path,
) -> None:
    """Change seg_001 from speaker_a → speaker_b. seg_001's voice_id +
    tts_provider MUST become speaker_b's baseline (copied from seg_002)."""
    project_dir, _ = _build_editing_job_with_voices(tmp_path)

    updated = patch_editing_segment(
        project_dir, "seg_001", {"speaker_id": "speaker_b"},
    )

    assert updated["speaker_id"] == "speaker_b"
    assert updated["voice_id"] == "voice_b_radio", (
        "voice_id should propagate from speaker_b's other segment"
    )
    assert updated["tts_provider"] == "cosyvoice"
    # Persisted to disk
    segs = load_editing_segments(project_dir)
    seg1 = next(s for s in segs if s["segment_id"] == "seg_001")
    assert seg1["speaker_id"] == "speaker_b"
    assert seg1["voice_id"] == "voice_b_radio"
    assert seg1["tts_provider"] == "cosyvoice"


def test_patch_speaker_id_flags_voice_dirty(tmp_path: Path) -> None:
    """Change of speaker_id triggers voice_dirty so batch re-TTS picks
    the segment up (audio needs to be re-generated with new voice)."""
    from services.jobs.editing_segments import SEGMENT_STATUS_VOICE_DIRTY
    project_dir, _ = _build_editing_job_with_voices(tmp_path)

    patch_editing_segment(project_dir, "seg_001", {"speaker_id": "speaker_b"})

    status = load_segment_status(project_dir)
    assert status.get("seg_001") == SEGMENT_STATUS_VOICE_DIRTY


def test_patch_speaker_id_clears_existing_voice_map_override(
    tmp_path: Path,
) -> None:
    """If the segment had an explicit voice_map override tied to the old
    speaker, it's stale the moment speaker_id changes. Clear it so the
    fresh baseline voice_id (propagated above) is what re-synth uses."""
    from services.jobs.editing_voice_map import (
        load_voice_map,
        set_voice_override,
    )
    project_dir, _ = _build_editing_job_with_voices(tmp_path)
    # Pre-existing override pointing at a speaker_a voice
    set_voice_override(
        project_dir, "seg_001",
        provider="minimax", voice_id="voice_a_special",
    )
    assert "seg_001" in load_voice_map(project_dir)

    patch_editing_segment(project_dir, "seg_001", {"speaker_id": "speaker_b"})

    # Override cleared
    assert "seg_001" not in load_voice_map(project_dir), (
        "stale voice_map override must be cleared when speaker changes"
    )


def test_patch_speaker_id_rejects_unknown_speaker(tmp_path: Path) -> None:
    """The new speaker_id must already exist in the task's segments.
    No implicit speaker creation (keeps voice-selection / speaker
    profile invariants intact)."""
    project_dir, _ = _build_editing_job_with_voices(tmp_path)

    with pytest.raises(ValueError, match="speaker.*not found|unknown speaker"):
        patch_editing_segment(
            project_dir, "seg_001", {"speaker_id": "speaker_zzz"},
        )
    # seg_001 unchanged on disk
    segs = load_editing_segments(project_dir)
    seg1 = next(s for s in segs if s["segment_id"] == "seg_001")
    assert seg1["speaker_id"] == "speaker_a"


def test_patch_speaker_id_noop_when_same_value(tmp_path: Path) -> None:
    """Setting speaker_id to its current value is a no-op — no status
    flip, no voice_map clearing. (Avoid spurious voice_dirty marks on
    accidental UI re-submits.)"""
    project_dir, _ = _build_editing_job_with_voices(tmp_path)

    patch_editing_segment(
        project_dir, "seg_001", {"speaker_id": "speaker_a"},  # same
    )
    status = load_segment_status(project_dir)
    assert "seg_001" not in status, "no-op speaker patch must not flip status"


# ---------------------------------------------------------------------------
# CodeX nit finding 2026-04-20: _propagate_speaker_change 采样门闩过早关闭
#
# 原 gate: ``rep_voice_id is None`` → 一旦 voice_id 落位就停止扫描。
# 问题：如果新 speaker 的第 1 个同伴段是 legacy 数据（只有 voice_id，
# 没有 tts_provider / 老字段 provider），rep_voice_id 立刻填满，后面
# 即使有带 tts_provider 的同 speaker 段也采不到。写回时 voice_id 换
# 新、tts_provider 维持旧 speaker 的值 → provider/voice_id 不匹配，
# re-TTS 时要么路由失败，要么发错声。
#
# 修法：门闩改 OR：``rep_voice_id is None or rep_tts_provider is None``，
# 继续扫直到两项都有值。函数里的 isinstance+非空 guard 已经防止覆盖
# 先前成功采到的字段。
# ---------------------------------------------------------------------------


def test_propagate_speaker_change_samples_provider_from_later_segment(
    tmp_path: Path,
) -> None:
    """新 speaker_b 的两个同伴段：seg_002 legacy（只有 voice_id，无
    tts_provider），seg_004 完整（voice_id + tts_provider）。改
    seg_001 → speaker_b 时，voice_id 要采 seg_002（按扫描顺序最先
    落位），但 tts_provider 必须从 seg_004 采到——原 gate 会在 seg_002
    之后关闭采样，tts_provider 保持 None → 写回时 voice_id 换新、
    tts_provider 被遗弃，导致 provider/voice_id 不匹配。"""
    project_dir = tmp_path / "projects" / "job_nit"
    (project_dir / "editor").mkdir(parents=True)
    baseline = [
        {
            "segment_id": "seg_001", "speaker_id": "speaker_a",
            "cn_text": "一", "source_text": "one",
            "start_ms": 0, "end_ms": 1000,
            "voice_id": "va_pro", "tts_provider": "minimax",
        },
        {
            # Legacy segment of speaker_b: has voice_id but NO tts_provider.
            # The first scanned same-speaker segment.
            "segment_id": "seg_002", "speaker_id": "speaker_b",
            "cn_text": "二", "source_text": "two",
            "start_ms": 1000, "end_ms": 2000,
            "voice_id": "vb_radio",
        },
        {
            "segment_id": "seg_003", "speaker_id": "speaker_a",
            "cn_text": "三", "source_text": "three",
            "start_ms": 2000, "end_ms": 3000,
            "voice_id": "va_pro", "tts_provider": "minimax",
        },
        {
            # Full segment of speaker_b — later in scan order. Must be
            # reached so tts_provider can be propagated.
            "segment_id": "seg_004", "speaker_id": "speaker_b",
            "cn_text": "四", "source_text": "four",
            "start_ms": 3000, "end_ms": 4000,
            "voice_id": "vb_radio", "tts_provider": "cosyvoice",
        },
    ]
    (project_dir / "editor" / "segments.json").write_text(
        json.dumps(baseline, ensure_ascii=False), encoding="utf-8",
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    record = JobRecord(
        job_id="job_nit",
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

    updated = patch_editing_segment(
        project_dir, "seg_001", {"speaker_id": "speaker_b"},
    )

    assert updated["voice_id"] == "vb_radio", (
        f"voice_id should propagate from a speaker_b segment (seg_002 "
        f"or seg_004); got {updated.get('voice_id')!r}"
    )
    assert updated["tts_provider"] == "cosyvoice", (
        "tts_provider MUST propagate from seg_004 — the gate must keep "
        "scanning past seg_002 (which has no tts_provider) until both "
        "fields are filled. Stale tts_provider would leave "
        f"provider/voice_id mismatched. got {updated.get('tts_provider')!r}"
    )


def test_patchable_fields_contract() -> None:
    """voice_id deliberately excluded — goes through voice_map.json in T1-6."""
    assert "cn_text" in PATCHABLE_SEGMENT_FIELDS
    assert "voice_id" not in PATCHABLE_SEGMENT_FIELDS


# ---------------------------------------------------------------------------
# JobService delegates
# ---------------------------------------------------------------------------


def test_service_get_editing_segments_bundles_metadata(tmp_path: Path) -> None:
    service, _, editing_record = _build_editing_job(tmp_path)

    payload = service.get_editing_segments("job_abc")

    assert payload["total"] == 3
    assert payload["editing_touched_at"] == editing_record.editing_touched_at
    assert payload["edit_generation"] == 0
    assert payload["segment_status"] == {}


def test_service_get_editing_segments_rejects_non_editing(tmp_path: Path) -> None:
    service, _, _ = _build_editing_job(tmp_path)
    # Force back to succeeded without going through cancel
    record = service.require_job("job_abc")
    from dataclasses import replace as _replace
    service.store.save_job(_replace(record, status=JOB_STATUS_SUCCEEDED))

    with pytest.raises(EditingConflictError, match="not in editing state"):
        service.get_editing_segments("job_abc")


def test_service_patch_refreshes_editing_touched_at(tmp_path: Path) -> None:
    service, _, editing_record = _build_editing_job(tmp_path)
    original_touched = editing_record.editing_touched_at
    assert original_touched is not None

    time.sleep(0.005)
    service.patch_editing_segment("job_abc", "seg_001", {"cn_text": "hi"})

    after = service.require_job("job_abc")
    assert after.editing_touched_at is not None
    assert after.editing_touched_at > original_touched


def test_service_patch_returns_updated_segment_and_status(tmp_path: Path) -> None:
    service, _, _ = _build_editing_job(tmp_path)
    result = service.patch_editing_segment("job_abc", "seg_002", {"cn_text": "新世界"})
    assert result["segment"]["cn_text"] == "新世界"
    assert result["segment_status"] == {"seg_002": "text_dirty"}


def test_service_patch_rejects_bad_segment_id(tmp_path: Path) -> None:
    service, _, _ = _build_editing_job(tmp_path)
    with pytest.raises(ValueError, match="invalid segment_id"):
        service.patch_editing_segment("job_abc", "../hack", {"cn_text": "x"})


def test_service_mark_status_refreshes_touched_at(tmp_path: Path) -> None:
    service, _, editing_record = _build_editing_job(tmp_path)
    original = editing_record.editing_touched_at

    time.sleep(0.005)
    result = service.mark_editing_segment_status("job_abc", "seg_001", "tts_dirty")

    after = service.require_job("job_abc")
    assert after.editing_touched_at > original
    assert result["segment_status"]["seg_001"] == "tts_dirty"


def test_service_mark_status_accept_removes_entry(tmp_path: Path) -> None:
    service, _, _ = _build_editing_job(tmp_path)
    service.mark_editing_segment_status("job_abc", "seg_001", "tts_dirty")
    result = service.mark_editing_segment_status("job_abc", "seg_001", "accepted")
    assert result["segment_status"] == {}


def test_service_patch_non_editing_job_rejected(tmp_path: Path) -> None:
    service, _, _ = _build_editing_job(tmp_path)
    from dataclasses import replace as _replace
    record = service.require_job("job_abc")
    service.store.save_job(_replace(record, status=JOB_STATUS_SUCCEEDED))
    with pytest.raises(EditingConflictError, match="not in editing state"):
        service.patch_editing_segment("job_abc", "seg_001", {"cn_text": "x"})


# ---------------------------------------------------------------------------
# Atomic write correctness: segments.json must not be partial after crash
# ---------------------------------------------------------------------------


def test_atomic_write_replaces_segments_cleanly(tmp_path: Path) -> None:
    """After patch_editing_segment, there should be no .tmp leftover file
    and segments.json should be a valid JSON list."""
    _, project_dir, _ = _build_editing_job(tmp_path)

    patch_editing_segment(project_dir, "seg_001", {"cn_text": "x"})

    editing_dir = project_dir / EDITING_SUBDIR
    tmp_files = list(editing_dir.glob("*.tmp"))
    assert tmp_files == [], f"stray temp files: {tmp_files}"
    data = json.loads((editing_dir / "segments.json").read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert len(data) == 3


# ---------------------------------------------------------------------------
# D44 — editing_payload augments segments with draft wav duration
#
# γ publish's DSP stretch tolerates any ratio but quality degrades at
# >2x / <0.5x. For UX, we surface the ratio in the editing page so the
# user can decide to re-edit text before committing. This requires the
# backend payload to carry the draft wav's actual duration alongside
# the slot's ``target_duration_ms`` (which the frontend already has).
# ---------------------------------------------------------------------------


def _write_draft_wav(
    project_dir: Path, segment_id: str, duration_ms: int,
) -> Path:
    """Write a silent wav at ``editor/editing/tts_segments_draft/{sid}.wav``."""
    from pydub import AudioSegment
    draft_dir = project_dir / EDITING_SUBDIR / "tts_segments_draft"
    draft_dir.mkdir(parents=True, exist_ok=True)
    path = draft_dir / f"{segment_id}.wav"
    AudioSegment.silent(duration=duration_ms).export(path, format="wav")
    return path


def test_editing_payload_includes_draft_wav_duration_when_present(
    tmp_path: Path,
) -> None:
    """If ``editor/editing/tts_segments_draft/{sid}.wav`` exists, the
    editing payload must expose its actual duration via
    ``draft_wav_duration_ms`` so the frontend can compute slot-mismatch
    warnings."""
    _, project_dir, _ = _build_editing_job(tmp_path)
    _write_draft_wav(project_dir, "seg_001", duration_ms=1_840)

    payload = editing_payload(project_dir)

    seg1 = next(s for s in payload["segments"] if s["segment_id"] == "seg_001")
    assert "draft_wav_duration_ms" in seg1, (
        "seg with draft must carry draft_wav_duration_ms so frontend can "
        "show slot-mismatch warning in γ publish contract"
    )
    # 1840ms ± 30ms (pydub/ffmpeg encode rounding)
    assert 1810 <= seg1["draft_wav_duration_ms"] <= 1870


def test_editing_payload_omits_draft_duration_when_no_draft(
    tmp_path: Path,
) -> None:
    """Segments without a draft wav must not carry a stale duration
    field — frontend distinguishes "no draft" vs "draft duration 0"."""
    _, project_dir, _ = _build_editing_job(tmp_path)

    payload = editing_payload(project_dir)

    for seg in payload["segments"]:
        assert "draft_wav_duration_ms" not in seg, (
            f"seg {seg.get('segment_id')!r} has no draft but carries "
            f"draft_wav_duration_ms={seg.get('draft_wav_duration_ms')!r}"
        )


def test_editing_payload_tolerates_unreadable_draft_wav(tmp_path: Path) -> None:
    """If the draft wav is truncated / not a valid wav (corrupted drop),
    the helper must skip the field rather than raise — the rest of the
    payload still needs to render."""
    _, project_dir, _ = _build_editing_job(tmp_path)
    draft_dir = project_dir / EDITING_SUBDIR / "tts_segments_draft"
    draft_dir.mkdir(parents=True, exist_ok=True)
    (draft_dir / "seg_001.wav").write_bytes(b"not-a-real-wav")

    payload = editing_payload(project_dir)

    seg1 = next(s for s in payload["segments"] if s["segment_id"] == "seg_001")
    assert "draft_wav_duration_ms" not in seg1, (
        "corrupted draft wav should skip the field; got "
        f"{seg1.get('draft_wav_duration_ms')!r}"
    )


# ---------------------------------------------------------------------------
# source_text patch support (2026-04-21) — users may correct upstream S1
# ASR mistakes on the edit page; symmetric with cn_text but no auto-retranslate
# (user also updates cn_text themselves, then single-segment re-TTS).
# ---------------------------------------------------------------------------


def test_patch_segment_updates_source_text_and_flags_text_dirty(tmp_path: Path) -> None:
    _, project_dir, _ = _build_editing_job(tmp_path)
    updated = patch_editing_segment(
        project_dir, "seg_001", {"source_text": "hello world"}
    )
    assert updated["source_text"] == "hello world"
    # Persists
    segments = load_editing_segments(project_dir)
    assert segments[0]["source_text"] == "hello world"
    # Flags text_dirty because the translation is now potentially stale
    # (user is responsible for also updating cn_text before re-TTS).
    status = load_segment_status(project_dir)
    assert status["seg_001"] == SEGMENT_STATUS_TEXT_DIRTY


def test_patch_segment_source_text_and_cn_text_both_apply_and_flag_dirty(tmp_path: Path) -> None:
    _, project_dir, _ = _build_editing_job(tmp_path)
    updated = patch_editing_segment(
        project_dir, "seg_001",
        {"source_text": "hello world", "cn_text": "你好世界"},
    )
    assert updated["source_text"] == "hello world"
    assert updated["cn_text"] == "你好世界"
    status = load_segment_status(project_dir)
    assert status["seg_001"] == SEGMENT_STATUS_TEXT_DIRTY


# ---------------------------------------------------------------------------
# split_editing_segment (2026-04-21) — mirrors translation_review's split
# behaviour but operates on editor/editing/segments.json (different schema).
# ---------------------------------------------------------------------------


def test_split_editing_segment_replaces_one_with_two(tmp_path: Path) -> None:
    from services.jobs.editing_segments import split_editing_segment

    _, project_dir, _ = _build_editing_job(tmp_path)
    # seg_001: "hello" / "你好" — split in the middle.
    result = split_editing_segment(
        project_dir,
        segment_id="seg_001",
        split_source_index=3,   # "hel" | "lo"
        split_cn_index=1,       # "你" | "好"
        speaker_a="A",
        speaker_b="B",
    )

    assert result["replaced_segment_id"] == "seg_001"
    assert len(result["new_segments"]) == 2
    assert result["total_count"] == 4  # was 3, split adds 1

    segments = load_editing_segments(project_dir)
    assert len(segments) == 4
    assert segments[0]["source_text"] == "hel"
    assert segments[0]["cn_text"] == "你"
    assert segments[0]["speaker_id"] == "A"
    assert segments[1]["source_text"] == "lo"
    assert segments[1]["cn_text"] == "好"
    assert segments[1]["speaker_id"] == "B"
    # Unrelated segments unchanged, order preserved.
    assert segments[2]["segment_id"] == "seg_002"
    assert segments[3]["segment_id"] == "seg_003"


def test_split_editing_segment_proportional_timestamp_split(tmp_path: Path) -> None:
    """Time boundary splits proportionally to the character position so
    downstream alignment has a plausible midpoint to re-anchor on."""
    from services.jobs.editing_segments import split_editing_segment

    _, project_dir, _ = _build_editing_job(tmp_path)
    # seg_001 is [0ms, 1000ms] for "hello" (5 chars). Splitting at index 2
    # ("he" | "llo") should place the midpoint at 400ms (2/5 of the way).
    split_editing_segment(
        project_dir,
        segment_id="seg_001",
        split_source_index=2,
        split_cn_index=1,
        speaker_a="A",
        speaker_b="A",
    )
    segments = load_editing_segments(project_dir)
    assert segments[0]["start_ms"] == 0
    assert segments[0]["end_ms"] == 400
    assert segments[1]["start_ms"] == 400
    assert segments[1]["end_ms"] == 1000


def test_split_editing_segment_generates_unique_ids(tmp_path: Path) -> None:
    from services.jobs.editing_segments import split_editing_segment

    _, project_dir, _ = _build_editing_job(tmp_path)
    split_editing_segment(
        project_dir,
        segment_id="seg_002",
        split_source_index=3,
        split_cn_index=1,
        speaker_a="B",
        speaker_b="B",
    )
    segments = load_editing_segments(project_dir)
    ids = [s["segment_id"] for s in segments]
    # All ids unique
    assert len(ids) == len(set(ids)), f"duplicate segment ids: {ids}"
    # New ids carry forward a sensible suffix of the source id (not "hacker")
    # The precise scheme is an impl detail; we only assert uniqueness + stability.


def test_split_editing_segment_rejects_zero_part_a(tmp_path: Path) -> None:
    """If split_source_index == 0 the A half is empty — meaningless split."""
    from services.jobs.editing_segments import split_editing_segment

    _, project_dir, _ = _build_editing_job(tmp_path)
    with pytest.raises(ValueError, match="empty"):
        split_editing_segment(
            project_dir,
            segment_id="seg_001",
            split_source_index=0,
            split_cn_index=0,
            speaker_a="A",
            speaker_b="A",
        )


def test_split_editing_segment_rejects_full_length_split(tmp_path: Path) -> None:
    """If split_source_index == len(source_text) the B half is empty."""
    from services.jobs.editing_segments import split_editing_segment

    _, project_dir, _ = _build_editing_job(tmp_path)
    with pytest.raises(ValueError, match="empty"):
        split_editing_segment(
            project_dir,
            segment_id="seg_001",
            split_source_index=5,  # "hello" is 5 chars
            split_cn_index=2,
            speaker_a="A",
            speaker_b="A",
        )


def test_split_editing_segment_unknown_id_raises(tmp_path: Path) -> None:
    from services.jobs.editing_segments import split_editing_segment

    _, project_dir, _ = _build_editing_job(tmp_path)
    with pytest.raises(EditingConflictError, match="not found"):
        split_editing_segment(
            project_dir,
            segment_id="seg_999",
            split_source_index=2,
            split_cn_index=1,
            speaker_a="A",
            speaker_b="A",
        )


def test_split_editing_segment_marks_both_new_segments_text_dirty(tmp_path: Path) -> None:
    """Both halves need re-TTS (draft was sized for the old full segment)."""
    from services.jobs.editing_segments import split_editing_segment

    _, project_dir, _ = _build_editing_job(tmp_path)
    result = split_editing_segment(
        project_dir,
        segment_id="seg_001",
        split_source_index=3,
        split_cn_index=1,
        speaker_a="A",
        speaker_b="A",
    )
    status = load_segment_status(project_dir)
    for new_seg in result["new_segments"]:
        assert status.get(new_seg["segment_id"]) == SEGMENT_STATUS_TEXT_DIRTY


# ---------------------------------------------------------------------------
# slice_source_audio_for_editing_segment (2026-04-21) — ffmpeg integration
# is factored behind _ffmpeg_slice_to_wav_bytes so we can monkeypatch it
# and keep unit tests hermetic.
# ---------------------------------------------------------------------------


def test_slice_source_audio_returns_base64_and_timing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.jobs import editing_segments as es

    _, project_dir, _ = _build_editing_job(tmp_path)
    # Create a fake source audio so _find_source_audio_path returns a hit.
    (project_dir / "audio").mkdir(parents=True, exist_ok=True)
    src = project_dir / "audio" / "original.wav"
    src.write_bytes(b"RIFF0000WAVEfmt fake")

    captured: dict[str, object] = {}

    def fake_slice(source_path, start_ms, end_ms, *, timeout_s=30):
        captured["source_path"] = source_path
        captured["start_ms"] = start_ms
        captured["end_ms"] = end_ms
        return b"FAKE_WAV_BYTES"

    monkeypatch.setattr(es, "_ffmpeg_slice_to_wav_bytes", fake_slice)
    result = es.slice_source_audio_for_editing_segment(project_dir, "seg_001")

    # Verified against _build_editing_job's baseline segments fixture
    # where seg_001 spans [0ms, 1000ms].
    assert captured["start_ms"] == 0
    assert captured["end_ms"] == 1000
    assert result["start_ms"] == 0
    assert result["end_ms"] == 1000
    assert result["duration_ms"] == 1000
    assert result["mime_type"] == "audio/wav"
    # Base64 of "FAKE_WAV_BYTES"
    import base64
    assert result["source_audio_base64"] == base64.b64encode(b"FAKE_WAV_BYTES").decode("ascii")


def test_slice_source_audio_prefers_speech_for_asr_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.jobs import editing_segments as es

    _, project_dir, _ = _build_editing_job(tmp_path)
    (project_dir / "audio").mkdir(parents=True, exist_ok=True)
    (project_dir / "audio" / "original.wav").write_bytes(b"ORIG")
    (project_dir / "audio" / "speech_for_asr.wav").write_bytes(b"SPEECH")

    captured: dict[str, object] = {}

    def fake_slice(source_path, start_ms, end_ms, *, timeout_s=30):
        captured["source_path"] = source_path
        return b"OK"

    monkeypatch.setattr(es, "_ffmpeg_slice_to_wav_bytes", fake_slice)
    es.slice_source_audio_for_editing_segment(project_dir, "seg_001")
    # speech_for_asr.wav is first in _SOURCE_AUDIO_CANDIDATES, so it wins.
    assert str(captured["source_path"]).endswith("speech_for_asr.wav")


def test_slice_source_audio_missing_segment_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.jobs import editing_segments as es

    _, project_dir, _ = _build_editing_job(tmp_path)
    (project_dir / "audio").mkdir(parents=True, exist_ok=True)
    (project_dir / "audio" / "original.wav").write_bytes(b"x")
    monkeypatch.setattr(
        es, "_ffmpeg_slice_to_wav_bytes",
        lambda *a, **k: pytest.fail("ffmpeg should not be invoked for missing segment"),
    )
    with pytest.raises(EditingConflictError, match="not found"):
        es.slice_source_audio_for_editing_segment(project_dir, "seg_nope")


def test_slice_source_audio_missing_audio_file_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither speech_for_asr.wav nor original.wav exists → clear runtime error,
    not a None deref deeper down."""
    from services.jobs import editing_segments as es

    _, project_dir, _ = _build_editing_job(tmp_path)
    # Deliberately do NOT create audio/*.wav
    monkeypatch.setattr(
        es, "_ffmpeg_slice_to_wav_bytes",
        lambda *a, **k: pytest.fail("ffmpeg should not be invoked without source"),
    )
    with pytest.raises(RuntimeError, match="源音频"):
        es.slice_source_audio_for_editing_segment(project_dir, "seg_001")
