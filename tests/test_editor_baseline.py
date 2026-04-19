"""Tests for the shared editor/segments.json baseline writer.

The writer is used by two callers:
- src.pipeline.process (S6 publish stage) — authoritative path, runs on
  every newly completed task
- services.jobs.editing.enter_editing — legacy fallback for tasks whose
  publish ran before this helper was wired in

Contract this suite pins down:
- Normalisation: segment_id int → str (pipeline writes int, editing layer
  treats as str; str-cast-both-sides was not enough once the baseline
  needed to round-trip through commit/copy_as_new).
- Input tolerance: both ``{"segments": [...]}`` and raw list top-level.
- Refusal: missing file, unreadable JSON, non-list payload all raise
  EditorBaselineError rather than silently producing an empty baseline
  (blank baseline would show an empty edit page — worse UX than 409).
- Atomic write: no .tmp residue on success.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.jobs.editor_baseline import (
    EditorBaselineError,
    normalise_segment_record,
    write_editor_segments_from_translation,
)


# ---------------------------------------------------------------------------
# normalise_segment_record
# ---------------------------------------------------------------------------


def test_normalise_keeps_str_segment_id_unchanged() -> None:
    seg = {"segment_id": "seg_001", "cn_text": "hi"}
    assert normalise_segment_record(seg) is seg, (
        "already-str segment_id must be returned unchanged to avoid "
        "allocating new dicts on the hot pipeline path"
    )


def test_normalise_casts_int_segment_id_to_str() -> None:
    result = normalise_segment_record({"segment_id": 42, "cn_text": "hi"})
    assert result["segment_id"] == "42"
    assert result["cn_text"] == "hi"


def test_normalise_leaves_none_segment_id_for_downstream_validation() -> None:
    """None must stay None — stringifying would produce the literal 'None'
    which downstream regex (input_validators) would accept as a valid id,
    masking a real data-quality problem."""
    seg = {"segment_id": None, "cn_text": "hi"}
    assert normalise_segment_record(seg) is seg


def test_normalise_passes_non_dict_through() -> None:
    """Caller is responsible for rejecting wholly-invalid payloads
    upstream; the per-record helper should be a pure mapper."""
    assert normalise_segment_record("not a dict") == "not a dict"
    assert normalise_segment_record(None) is None


def test_normalise_preserves_extra_fields_and_int_ids_together() -> None:
    """Pipeline segments carry 20+ fields (voice_id, alignment_method,
    start_ms, ...). Normalisation must touch segment_id ONLY."""
    seg = {
        "segment_id": 7,
        "speaker_id": "speaker_a",
        "start_ms": 400,
        "end_ms": 1200,
        "voice_id": "v1",
        "alignment_method": "direct",
        "needs_review": False,
    }
    result = normalise_segment_record(seg)
    assert result["segment_id"] == "7"
    # Every other key pass-through
    for k, v in seg.items():
        if k == "segment_id":
            continue
        assert result[k] == v


# ---------------------------------------------------------------------------
# write_editor_segments_from_translation
# ---------------------------------------------------------------------------


def _seed_translation(project_dir: Path, payload: object) -> Path:
    (project_dir / "translation").mkdir(parents=True, exist_ok=True)
    path = project_dir / "translation" / "segments.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_write_baseline_from_dict_wrapped_translation(tmp_path: Path) -> None:
    """Pipeline's canonical shape is ``{"segments": [...], "total_segments": N}``.
    The writer should extract the segments list and normalise ids."""
    _seed_translation(tmp_path, {
        "segments": [
            {"segment_id": 1, "cn_text": "一"},
            {"segment_id": 2, "cn_text": "二"},
        ],
        "total_segments": 2,
        "output_path": "/ignored",
    })

    result = write_editor_segments_from_translation(tmp_path)

    assert result == tmp_path / "editor" / "segments.json"
    assert result.is_file()
    segments = json.loads(result.read_text(encoding="utf-8"))
    assert [s["segment_id"] for s in segments] == ["1", "2"]
    assert [s["cn_text"] for s in segments] == ["一", "二"]


def test_write_baseline_from_raw_list_translation(tmp_path: Path) -> None:
    """Defensive: some hypothetical translation writer emits a raw list at
    the top level. Don't refuse that shape — it's still a valid segments
    list."""
    _seed_translation(tmp_path, [
        {"segment_id": "seg_a", "cn_text": "A"},
        {"segment_id": "seg_b", "cn_text": "B"},
    ])

    write_editor_segments_from_translation(tmp_path)

    segments = json.loads(
        (tmp_path / "editor" / "segments.json").read_text(encoding="utf-8")
    )
    assert [s["segment_id"] for s in segments] == ["seg_a", "seg_b"]


def test_write_baseline_raises_when_translation_missing(tmp_path: Path) -> None:
    with pytest.raises(EditorBaselineError, match="not found"):
        write_editor_segments_from_translation(tmp_path)


def test_write_baseline_raises_when_translation_is_unreadable(tmp_path: Path) -> None:
    (tmp_path / "translation").mkdir()
    (tmp_path / "translation" / "segments.json").write_text(
        "{not: valid json,", encoding="utf-8"
    )

    with pytest.raises(EditorBaselineError, match="unreadable"):
        write_editor_segments_from_translation(tmp_path)


def test_write_baseline_raises_when_top_level_has_no_segments_list(
    tmp_path: Path,
) -> None:
    for bad in [
        {"total_segments": 0, "output_path": "/p"},   # dict without segments
        {"segments": "not a list"},                    # wrong shape
        42,                                            # not container at all
    ]:
        _seed_translation(tmp_path, bad)
        with pytest.raises(EditorBaselineError, match="no usable 'segments' list"):
            write_editor_segments_from_translation(tmp_path)


def test_write_baseline_overwrites_existing(tmp_path: Path) -> None:
    """Per the docstring contract, the pipeline caller unconditionally
    overwrites to keep editor/segments.json in sync with the translation
    snapshot. editing.enter_editing guards its own "only if absent" check
    outside of this helper."""
    (tmp_path / "editor").mkdir()
    stale = tmp_path / "editor" / "segments.json"
    stale.write_text(
        json.dumps([{"segment_id": "stale", "cn_text": "old"}]),
        encoding="utf-8",
    )
    _seed_translation(tmp_path, {"segments": [{"segment_id": 1, "cn_text": "fresh"}]})

    write_editor_segments_from_translation(tmp_path)

    segments = json.loads(stale.read_text(encoding="utf-8"))
    assert segments == [{"segment_id": "1", "cn_text": "fresh"}]


def test_write_baseline_does_not_touch_translation(tmp_path: Path) -> None:
    """Shared helper must NEVER mutate translation/segments.json. One-way
    read only — same invariant that T1-3's lazy-seed test pinned down."""
    payload = {"segments": [{"segment_id": 1, "cn_text": "一"}]}
    translation_path = _seed_translation(tmp_path, payload)
    before = translation_path.read_bytes()

    write_editor_segments_from_translation(tmp_path)

    after = translation_path.read_bytes()
    assert before == after, "translation/segments.json must be untouched"


def test_write_baseline_leaves_no_tmp_file_on_success(tmp_path: Path) -> None:
    """Atomic-write via tempfile+replace should remove the temp on success.
    A lingering .seed.tmp would trip later file listings."""
    _seed_translation(tmp_path, {"segments": [{"segment_id": 1, "cn_text": "一"}]})
    write_editor_segments_from_translation(tmp_path)

    editor_dir = tmp_path / "editor"
    tmp_files = [p for p in editor_dir.iterdir() if p.suffix.endswith("tmp")]
    assert tmp_files == [], f"stale tmp files: {tmp_files}"
