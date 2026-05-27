"""Phase 4.2 E.1 PR #15 P1 五轮 fix (Codex 2026-05-27).

Closes the last gap in the editing → re-TTS routing chain: after voice_map
persists ``requires_worker`` / ``worker_target_model`` (P1 三轮 fix), the
commit + copy_service paths that materialise voice_map onto the final
``editor/segments.json`` were stripping those fields. Resume / batch
re-TTS / γ publish then saw only ``{tts_provider, voice_id}`` and routed
CosyVoice clone voices through the legacy DashScope endpoint instead of
the mainland worker.

This suite locks the commit + copy_as_new propagation through 4 phases:

Phase A — ``editing_commit._apply_voice_map`` direct behavior
Phase B — ``editing_commit._apply_editing_to_baseline`` end-to-end with
          a real on-disk editing/voice_map.json
Phase C — ``copy_service._apply_voice_map_to_segments`` direct behavior
Phase D — Static guards locking that any segments.json writer that
          consumes voice_map normalises ``requires_worker`` /
          ``worker_target_model`` alongside the other entry fields

Architectural constraint preserved: pipeline subprocess (src/services/)
does NOT do its own user_voices DB lookup. The routing fields are
read from voice_map.json (which gateway intercept enriched server-side
per P1 三轮 fix) and propagated forward — pure file-level passthrough.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (REPO_ROOT / "src", REPO_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


# ---------------------------------------------------------------------------
# Phase A — editing_commit._apply_voice_map direct behavior
# ---------------------------------------------------------------------------


def test_phase_a_clone_routing_preserved_in_editor_segments():
    """**Phase A.1**: voice_map entry with ``requires_worker=True`` →
    segment carries both routing fields after _apply_voice_map. This
    is what editor/segments.json downstream readers (resume, γ publish,
    batch re-TTS) see.
    """
    from services.jobs.editing_commit import _apply_voice_map

    segments = [
        {"segment_id": "seg_001", "tts_provider": "minimax", "voice_id": "old"},
    ]
    voice_map = {
        "seg_001": {
            "provider": "cosyvoice",
            "voice_id": "voice-cosy-mine-uuid",
            "requires_worker": True,
            "worker_target_model": "cosyvoice-v3.5-flash",
        }
    }
    out = _apply_voice_map(segments, voice_map)
    assert len(out) == 1
    seg = out[0]
    assert seg["tts_provider"] == "cosyvoice"
    assert seg["voice_id"] == "voice-cosy-mine-uuid"
    assert seg["requires_worker"] is True
    assert seg["worker_target_model"] == "cosyvoice-v3.5-flash"


def test_phase_a_non_clone_override_clears_stale_routing():
    """**Phase A.2 (stale cleanup)**: segment has routing from a previous
    clone override; user swaps to a MiniMax / builtin voice. _apply_voice_map
    must POP the stale ``requires_worker`` / ``worker_target_model`` —
    otherwise the new MiniMax voice would inherit the old worker dispatch.
    """
    from services.jobs.editing_commit import _apply_voice_map

    segments = [
        {
            "segment_id": "seg_002",
            "tts_provider": "cosyvoice",
            "voice_id": "voice-cosy-old-clone",
            "requires_worker": True,
            "worker_target_model": "cosyvoice-v3.5-flash",
        },
    ]
    voice_map = {
        "seg_002": {
            "provider": "minimax",
            "voice_id": "Chinese (Mandarin)_Wise_Woman",
            # no requires_worker → not a clone
        }
    }
    out = _apply_voice_map(segments, voice_map)
    seg = out[0]
    assert seg["tts_provider"] == "minimax"
    assert seg["voice_id"] == "Chinese (Mandarin)_Wise_Woman"
    # **Critical**: stale clone routing must be cleared
    assert "requires_worker" not in seg, (
        "Stale routing leaked. Voice-swap clone→MiniMax must wipe "
        "requires_worker, otherwise re-TTS still tries the worker path."
    )
    assert "worker_target_model" not in seg


def test_phase_a_clone_to_different_clone_swaps_target_model():
    """**Phase A.3**: clone → different clone (different target_model)
    must update the segment's ``worker_target_model``, not keep stale.
    """
    from services.jobs.editing_commit import _apply_voice_map

    segments = [
        {
            "segment_id": "seg_003",
            "tts_provider": "cosyvoice",
            "voice_id": "voice-clone-A",
            "requires_worker": True,
            "worker_target_model": "cosyvoice-v3.5-flash",
        },
    ]
    voice_map = {
        "seg_003": {
            "provider": "cosyvoice",
            "voice_id": "voice-clone-B",
            "requires_worker": True,
            "worker_target_model": "cosyvoice-v3.5-plus",  # NEW
        }
    }
    seg = _apply_voice_map(segments, voice_map)[0]
    assert seg["voice_id"] == "voice-clone-B"
    assert seg["worker_target_model"] == "cosyvoice-v3.5-plus"


def test_phase_a_strict_bool_check_rejects_truthy_junk():
    """**Phase A.4 (defense)**: only ``requires_worker is True`` triggers
    routing injection. Truthy junk (``1``, ``"true"``, ``[1]``) on a
    hand-edited file MUST NOT promote a non-clone segment to worker path.
    """
    from services.jobs.editing_commit import _apply_voice_map

    for junky in (1, "true", "True", [1], {"x": 1}, "yes"):
        segments = [{"segment_id": "seg", "tts_provider": "minimax"}]
        voice_map = {
            "seg": {
                "provider": "cosyvoice",
                "voice_id": "voice-x",
                "requires_worker": junky,
                "worker_target_model": "cosyvoice-v3.5-flash",
            }
        }
        out = _apply_voice_map(segments, voice_map)[0]
        assert "requires_worker" not in out, (
            f"Truthy junk requires_worker={junky!r} promoted to True. "
            f"Strict 'is True' check failed."
        )


# ---------------------------------------------------------------------------
# Phase B — editing_commit._apply_editing_to_baseline end-to-end
# ---------------------------------------------------------------------------


def _seed_editing_project(tmp_path: Path) -> Path:
    """Build a minimal project dir with editing/ ready. ``editor/`` is
    created by ``_apply_editing_to_baseline`` itself (with mkdir
    parents+exist_ok), so we don't pre-create it here."""
    from services.jobs.editing import EDITING_SUBDIR

    project = tmp_path / "proj"
    (project / EDITING_SUBDIR).mkdir(parents=True)
    return project


def test_phase_b_editor_segments_json_carries_clone_routing(tmp_path):
    """**Phase B.1**: end-to-end — write editing/voice_map.json with
    clone routing → call _apply_editing_to_baseline → read
    editor/segments.json → assert routing fields present.
    """
    from services.jobs.editing import EDITING_SUBDIR
    from services.jobs.editing_commit import _apply_editing_to_baseline

    project = _seed_editing_project(tmp_path)
    editing = project / EDITING_SUBDIR
    # Baseline segments
    baseline_segments = [
        {"segment_id": "seg_b_001", "tts_provider": "minimax", "voice_id": "old"},
    ]
    (editing / "segments.json").write_text(
        json.dumps(baseline_segments), encoding="utf-8"
    )
    # voice_map.json with clone routing
    voice_map_payload = {
        "seg_b_001": {
            "provider": "cosyvoice",
            "voice_id": "voice-cosy-clone-b",
            "requires_worker": True,
            "worker_target_model": "cosyvoice-v3.5-flash",
        }
    }
    (editing / "voice_map.json").write_text(
        json.dumps(voice_map_payload), encoding="utf-8"
    )

    _apply_editing_to_baseline(project)

    written = json.loads((project / "editor" / "segments.json").read_text(encoding="utf-8"))
    assert len(written) == 1
    seg = written[0]
    assert seg["voice_id"] == "voice-cosy-clone-b"
    assert seg["requires_worker"] is True, (
        "editor/segments.json missing requires_worker after commit. "
        "Pipeline resume / batch re-TTS / γ publish will fall back to "
        "legacy CosyVoice and the clone voice silently won't take effect."
    )
    assert seg["worker_target_model"] == "cosyvoice-v3.5-flash"


def test_phase_b_editor_segments_json_omits_routing_for_minimax(tmp_path):
    """**Phase B.2**: voice_map with MiniMax override → editor/segments.json
    has NO routing fields. Otherwise we'd promote a MiniMax voice to
    worker path through a stale flag.
    """
    from services.jobs.editing import EDITING_SUBDIR
    from services.jobs.editing_commit import _apply_editing_to_baseline

    project = _seed_editing_project(tmp_path)
    editing = project / EDITING_SUBDIR
    (editing / "segments.json").write_text(
        json.dumps([
            {
                "segment_id": "seg_b_002",
                "tts_provider": "cosyvoice",
                "voice_id": "old-clone",
                "requires_worker": True,
                "worker_target_model": "cosyvoice-v3.5-flash",
            },
        ]),
        encoding="utf-8",
    )
    (editing / "voice_map.json").write_text(
        json.dumps({
            "seg_b_002": {
                "provider": "minimax",
                "voice_id": "Chinese (Mandarin)_Wise_Woman",
            }
        }),
        encoding="utf-8",
    )

    _apply_editing_to_baseline(project)

    written = json.loads((project / "editor" / "segments.json").read_text(encoding="utf-8"))
    seg = written[0]
    assert seg["tts_provider"] == "minimax"
    assert "requires_worker" not in seg, (
        "MiniMax override didn't clear stale clone routing → re-TTS "
        "would still try the worker path."
    )
    assert "worker_target_model" not in seg


# ---------------------------------------------------------------------------
# Phase C — copy_service._apply_voice_map_to_segments direct
# ---------------------------------------------------------------------------


def test_phase_c_copy_service_propagates_clone_routing():
    """**Phase C.1**: copy_as_new path applies voice_map → target
    segments.json carries routing. Without this, copy_as_new creates a
    new project where the clone voice is selected but the routing flags
    are missing, so pipeline re-runs fall back to legacy.
    """
    from services.jobs.copy_service import _apply_voice_map_to_segments

    segments = [
        {"segment_id": "seg_c_001", "tts_provider": "minimax", "voice_id": "old"},
    ]
    voice_map = {
        "seg_c_001": {
            "provider": "cosyvoice",
            "voice_id": "voice-clone-copied",
            "requires_worker": True,
            "worker_target_model": "cosyvoice-v3.5-plus",
        }
    }
    seg = _apply_voice_map_to_segments(segments, voice_map)[0]
    assert seg["tts_provider"] == "cosyvoice"
    assert seg["voice_id"] == "voice-clone-copied"
    assert seg["requires_worker"] is True
    assert seg["worker_target_model"] == "cosyvoice-v3.5-plus"


def test_phase_c_copy_service_clears_stale_routing_on_swap():
    """**Phase C.2**: copy_as_new applies voice_map that swaps clone →
    builtin must clear stale routing in target segments.json.
    """
    from services.jobs.copy_service import _apply_voice_map_to_segments

    segments = [
        {
            "segment_id": "seg_c_002",
            "tts_provider": "cosyvoice",
            "voice_id": "voice-old-clone",
            "requires_worker": True,
            "worker_target_model": "cosyvoice-v3.5-flash",
        },
    ]
    voice_map = {
        "seg_c_002": {
            "provider": "cosyvoice",
            "voice_id": "cosyvoice-v3.5-flash-zh-builtin",
            # no requires_worker
        }
    }
    seg = _apply_voice_map_to_segments(segments, voice_map)[0]
    assert seg["voice_id"] == "cosyvoice-v3.5-flash-zh-builtin"
    assert "requires_worker" not in seg
    assert "worker_target_model" not in seg


def test_phase_c_copy_service_strict_bool_check():
    """**Phase C.3 (defense)**: mirror of A.4 — copy_service must use the
    same strict ``is True`` check. Otherwise hand-edited / forged
    voice_map can promote non-clone copy targets to worker path.
    """
    from services.jobs.copy_service import _apply_voice_map_to_segments

    for junky in (1, "true", [1], "yes"):
        seg = _apply_voice_map_to_segments(
            [{"segment_id": "x", "tts_provider": "minimax"}],
            {
                "x": {
                    "provider": "cosyvoice",
                    "voice_id": "v",
                    "requires_worker": junky,
                    "worker_target_model": "cosyvoice-v3.5-flash",
                }
            },
        )[0]
        assert "requires_worker" not in seg


# ---------------------------------------------------------------------------
# Phase D — Static guards: voice_map normalizers must list routing fields
# ---------------------------------------------------------------------------


_VOICE_MAP_NORMALIZER_FILES: list[tuple[Path, str]] = [
    (REPO_ROOT / "src" / "services" / "jobs" / "editing_commit.py",
     "_apply_editing_to_baseline"),
    (REPO_ROOT / "src" / "services" / "jobs" / "copy_service.py",
     "prepare_copy_project_dir"),
    (REPO_ROOT / "src" / "services" / "jobs" / "editing_voice_map.py",
     "_load_voice_map_raw"),
]


@pytest.mark.parametrize("path,func_name", _VOICE_MAP_NORMALIZER_FILES)
def test_phase_d_voice_map_normalizers_handle_routing_fields(path, func_name):
    """**Phase D**: every place that loads voice_map.json + normalises
    entries MUST reference both ``requires_worker`` and
    ``worker_target_model`` in the normalisation block. Otherwise a
    future refactor that only writes ``{provider, voice_id, tts_model_key}``
    silently drops routing again.

    Detection: source-text scan for the field names appearing alongside
    the ``provider`` / ``voice_id`` extraction block. Allow comment-only
    references — but require at least one occurrence in the function
    body source. (We check the whole file rather than walking AST
    because the normalizers are inline rather than module-level helpers.)
    """
    src = path.read_text(encoding="utf-8")
    # The function must exist
    assert f"def {func_name}" in src or f"async def {func_name}" in src, (
        f"{path.name} missing function {func_name}"
    )
    # Both fields referenced (in normalization, comments, or both)
    assert "requires_worker" in src, (
        f"{path.name} doesn't reference `requires_worker` at all — "
        f"voice_map normalisation drops it. Add normalization in "
        f"{func_name}() so editor/segments.json (or copy_as_new "
        f"target segments.json) carries the routing."
    )
    assert "worker_target_model" in src, (
        f"{path.name} doesn't reference `worker_target_model` at all — "
        f"voice_map normalisation drops it. Add normalization in {func_name}()."
    )


def test_phase_d_apply_voice_map_writers_propagate_routing():
    """**Phase D.2**: both `_apply_voice_map` (editing_commit) and
    `_apply_voice_map_to_segments` (copy_service) must reference
    ``requires_worker`` in the write path. This locks that future
    refactors can't silently drop the field even if the normalizer
    keeps it.
    """
    for path in (
        REPO_ROOT / "src" / "services" / "jobs" / "editing_commit.py",
        REPO_ROOT / "src" / "services" / "jobs" / "copy_service.py",
    ):
        src = path.read_text(encoding="utf-8")
        # The "write" block must include both keys as new_seg keys.
        # Pattern: `new_seg["requires_worker"]` somewhere.
        assert re.search(
            r'new_seg\[\s*[\'"]requires_worker[\'"]\s*\]\s*=\s*True',
            src,
        ), (
            f"{path.name}: missing `new_seg[\"requires_worker\"] = True` "
            f"in the voice_map application. Segments.json writers must "
            f"propagate the routing for downstream pipeline / γ publish "
            f"to dispatch CosyVoice clone voices through the worker."
        )
        # And the stale-cleanup pop
        assert re.search(
            r'new_seg\.pop\(\s*[\'"]requires_worker[\'"]\s*,\s*None\s*\)',
            src,
        ), (
            f"{path.name}: missing `new_seg.pop(\"requires_worker\", None)` — "
            f"voice-swap clone→builtin would leave stale routing on the "
            f"segment, dispatching MiniMax voice through the worker path."
        )
