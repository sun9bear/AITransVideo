"""P0-5 (audit 2026-05-07) regression: file_lock coverage on the
JSON-state mutate hot paths.

Audit reference:
    docs/audits/2026-05-07-comprehensive-codebase-audit.md
        D-CRITICAL-2 — JobStore / StateManager / editing 三件套 / admin_settings
                       all did read→modify→write without file_lock, so under
                       ThreadingHTTPServer two HTTP threads could interleave
                       and last-write-wins, silently dropping updates.

Coverage strategy (two layers):

§1 — Source-level guard (cheap, runs in any environment).
     `inspect.getsource` + `ast.parse` to confirm each protected function
     has a `with file_lock(...)` statement. Catches anyone removing the
     lock during refactor.

§2 — Real concurrency check (exercises the actual lock).
     Spawn N threads each performing a load→modify→save against the same
     file. Without the lock, last-write-wins drops at least one update;
     with it, all N updates survive. Assert all updates are visible in
     the final on-disk state.

Note: JobStore.save_job is intentionally NOT covered here. The fix for
its load→modify→save race lives at the caller layer (see audit P1 in
docs/audits/2026-05-07-comprehensive-codebase-audit.md §9 P1) and is out
of scope for P0-5 — adding a lock inside save_job alone wouldn't help
because callers do load() in their own scope.
"""
from __future__ import annotations

import ast
import inspect
import json
import sys
import textwrap
import threading
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
# conftest.py already prepends src/, but add gateway/ so admin_settings
# imports cleanly here too.
_GATEWAY_DIR = str(_REPO_ROOT / "gateway")
if _GATEWAY_DIR not in sys.path:
    sys.path.insert(0, _GATEWAY_DIR)


# ====================================================================
# §1 — Source-level guards
# ====================================================================


def _has_with_file_lock(fn) -> bool:
    """AST-level check: does `fn`'s body contain a `with file_lock(...)`
    statement?"""
    src = textwrap.dedent(inspect.getsource(fn))
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            ctx = item.context_expr
            # Match either bare `file_lock(...)` or attribute access.
            if isinstance(ctx, ast.Call):
                if isinstance(ctx.func, ast.Name) and ctx.func.id == "file_lock":
                    return True
                if isinstance(ctx.func, ast.Attribute) and ctx.func.attr == "file_lock":
                    return True
    return False


def test_state_manager_set_stage_uses_file_lock():
    from services.state_manager import StateManager
    assert _has_with_file_lock(StateManager.set_stage), (
        "P0-5 regression: StateManager.set_stage no longer wraps load→save "
        "in file_lock — concurrent stage transitions can lose updates."
    )


def test_state_manager_set_project_uses_file_lock():
    from services.state_manager import StateManager
    assert _has_with_file_lock(StateManager.set_project)


def test_editing_segments_patch_uses_file_lock():
    from services.jobs.editing_segments import patch_editing_segment
    assert _has_with_file_lock(patch_editing_segment), (
        "P0-5 regression: patch_editing_segment must hold the editing "
        "lock anchor across load→modify→save → side-effects sequence."
    )


def test_editing_segments_mark_status_uses_file_lock():
    from services.jobs.editing_segments import mark_segment_status
    assert _has_with_file_lock(mark_segment_status)


def test_editing_segments_split_uses_file_lock():
    from services.jobs.editing_segments import split_editing_segment
    assert _has_with_file_lock(split_editing_segment)


def test_editing_segments_revert_uses_file_lock():
    from services.jobs.editing_segments import revert_text_changes_to_audio_baseline
    assert _has_with_file_lock(revert_text_changes_to_audio_baseline)


def test_editing_voice_map_set_uses_file_lock():
    from services.jobs.editing_voice_map import set_voice_override
    assert _has_with_file_lock(set_voice_override)


def test_editing_voice_map_clear_uses_file_lock():
    from services.jobs.editing_voice_map import clear_voice_override
    assert _has_with_file_lock(clear_voice_override)


def _admin_settings_function_uses_file_lock(fn_name: str) -> bool:
    """Helper: does the named module-level fn in gateway/admin_settings.py
    contain a `with file_lock(...)` somewhere in its body?

    Used as a static guard so any future revert that drops the lock from
    the prompt-history / settings paths is caught at CI time.
    """
    src_path = _REPO_ROOT / "gateway" / "admin_settings.py"
    src = src_path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != fn_name:
            continue
        fn_src = ast.get_source_segment(src, node)
        if fn_src is None:
            return False
        fn_tree = ast.parse(textwrap.dedent(fn_src))
        for sub in ast.walk(fn_tree):
            if not isinstance(sub, ast.With):
                continue
            for it in sub.items:
                ce = it.context_expr
                if isinstance(ce, ast.Call) and isinstance(ce.func, ast.Name) and ce.func.id == "file_lock":
                    return True
        return False
    return False


def test_admin_settings_save_uses_file_lock():
    """admin_settings.save_settings — used by /api/admin/settings."""
    for name in {"save_settings", "update_review_prompts", "toggle_model"}:
        assert _admin_settings_function_uses_file_lock(name), (
            f"P0-5 regression: gateway/admin_settings.py::{name} is no "
            f"longer wrapped in file_lock — concurrent admin saves can "
            f"lose updates."
        )


def test_admin_settings_save_prompt_history_uses_file_lock():
    """P0-5 follow-up (codex review 2026-05-07): _save_prompt_history was
    raw write_text without lock or atomic write. Two concurrent admin
    actions (save prompts vs delete history) could lose the append or
    leave a half-written file."""
    assert _admin_settings_function_uses_file_lock("_save_prompt_history"), (
        "P0-5 follow-up regression: _save_prompt_history must use "
        "file_lock(_PROMPT_HISTORY_FILE) + atomic_write_json."
    )


def test_admin_settings_delete_prompt_history_uses_file_lock():
    """P0-5 follow-up (codex review 2026-05-07): delete_prompt_history
    must hold the prompt_history lock around its load→pop→save sequence,
    not just rely on _save_prompt_history's inner backstop."""
    assert _admin_settings_function_uses_file_lock("delete_prompt_history"), (
        "P0-5 follow-up regression: delete_prompt_history must wrap "
        "load→pop→save in file_lock(_PROMPT_HISTORY_FILE) so a concurrent "
        "update_review_prompts cannot lose its history append."
    )


def _admin_settings_function_locks_target(fn_name: str, target_name: str) -> bool:
    """Stricter than _admin_settings_function_uses_file_lock: also asserts
    the lock argument is the named module variable (e.g. _PROMPT_HISTORY_FILE
    or SETTINGS_FILE), not some other path. Catches a refactor that swaps
    in the wrong file.
    """
    src_path = _REPO_ROOT / "gateway" / "admin_settings.py"
    src = src_path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != fn_name:
            continue
        fn_src = ast.get_source_segment(src, node)
        if fn_src is None:
            return False
        fn_tree = ast.parse(textwrap.dedent(fn_src))
        for sub in ast.walk(fn_tree):
            if not isinstance(sub, ast.With):
                continue
            for it in sub.items:
                ce = it.context_expr
                # file_lock(<arg>)
                if isinstance(ce, ast.Call) \
                   and isinstance(ce.func, ast.Name) and ce.func.id == "file_lock" \
                   and len(ce.args) >= 1 \
                   and isinstance(ce.args[0], ast.Name) \
                   and ce.args[0].id == target_name:
                    return True
        return False
    return False


def test_admin_settings_save_prompt_history_locks_correct_target():
    """P1-15c (Codex P0-5 review): assert the lock target is specifically
    _PROMPT_HISTORY_FILE, not some other path. Catches a refactor that
    swaps in the wrong file or accidentally locks SETTINGS_FILE here."""
    assert _admin_settings_function_locks_target(
        "_save_prompt_history", "_PROMPT_HISTORY_FILE"
    ), (
        "P1-15c regression: _save_prompt_history must hold "
        "file_lock(_PROMPT_HISTORY_FILE) specifically — locking any other "
        "path silently lets a concurrent prompt-history mutation lose "
        "its update."
    )


def test_admin_settings_delete_prompt_history_locks_correct_target():
    """Same stricter check for delete_prompt_history."""
    assert _admin_settings_function_locks_target(
        "delete_prompt_history", "_PROMPT_HISTORY_FILE"
    )


def test_admin_settings_save_prompt_history_uses_atomic_write_json():
    """P1-15c (Codex P0-5 review): _save_prompt_history must use
    atomic_write_json (which does temp + os.replace) so a crash mid-write
    cannot leave a half-written prompt_history.json. The earlier raw
    write_text was the bug pattern."""
    src_path = _REPO_ROOT / "gateway" / "admin_settings.py"
    src = src_path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "_save_prompt_history":
            continue
        fn_src = ast.get_source_segment(src, node)
        assert fn_src is not None
        fn_tree = ast.parse(textwrap.dedent(fn_src))
        # Walk: must find a Call to atomic_write_json
        # AND must NOT find a Call to write_text (that would be the bug)
        found_atomic = False
        found_write_text = False
        for sub in ast.walk(fn_tree):
            if isinstance(sub, ast.Call):
                func = sub.func
                if isinstance(func, ast.Name) and func.id == "atomic_write_json":
                    found_atomic = True
                if isinstance(func, ast.Attribute) and func.attr == "write_text":
                    found_write_text = True
        assert found_atomic, (
            "P1-15c regression: _save_prompt_history is no longer using "
            "atomic_write_json — a partial write would corrupt the file."
        )
        assert not found_write_text, (
            "P1-15c regression: _save_prompt_history reverted to write_text. "
            "atomic_write_json provides crash-safety; raw write_text does not."
        )
        return  # found and asserted
    pytest.fail("_save_prompt_history function not found in admin_settings.py")


# ====================================================================
# §2 — Real concurrency: drive the actual code paths and assert that
#       all N parallel updates survive in the on-disk file.
# ====================================================================


@pytest.fixture
def state_manager(tmp_path):
    from services.state_manager import StateManager
    return StateManager(str(tmp_path / "state.json"))


def test_state_manager_concurrent_set_stage_does_not_lose_updates(state_manager):
    """Spawn 8 threads, each setting a distinct stage to RUNNING. Without
    the lock the JSON file would interleave reads and lose stages.
    """
    from core.enums import StageStatus

    stage_names = [f"stage_{i:02d}" for i in range(8)]
    barrier = threading.Barrier(len(stage_names))

    def worker(name: str) -> None:
        barrier.wait()  # release all threads at once
        state_manager.set_stage(name, StageStatus.RUNNING.value)

    threads = [threading.Thread(target=worker, args=(name,)) for name in stage_names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = state_manager.load()
    survived = set(final.get("stages", {}).keys())
    expected = set(stage_names)
    missing = expected - survived
    assert not missing, (
        f"P0-5 regression: {len(missing)} of {len(expected)} concurrent "
        f"set_stage calls lost their updates. Missing stages: {missing}"
    )


def test_editing_segments_concurrent_patch_does_not_lose_updates(tmp_path):
    """Spawn 6 threads each patching a different segment's cn_text. All
    six edits must survive the final segments.json."""
    from services.jobs.editing import EDITING_SUBDIR
    from services.jobs.editing_segments import patch_editing_segment

    project_dir = tmp_path
    editing_dir = project_dir / EDITING_SUBDIR
    editing_dir.mkdir(parents=True, exist_ok=True)

    seed_segments = [
        {
            "segment_id": f"seg_{i:03d}",
            "cn_text": f"原文{i}",
            "source_text": f"src{i}",
            "speaker_id": "spk_a",
            "start_ms": i * 1000,
            "end_ms": (i + 1) * 1000,
            "voice_id": "v1",
            "tts_provider": "minimax",
        }
        for i in range(8)
    ]
    (editing_dir / "segments.json").write_text(
        json.dumps(seed_segments, ensure_ascii=False),
        encoding="utf-8",
    )

    target_ids = [f"seg_{i:03d}" for i in range(6)]
    new_text_for = {sid: f"修改后_{sid}" for sid in target_ids}
    barrier = threading.Barrier(len(target_ids))
    errors: list[Exception] = []

    def worker(sid: str) -> None:
        try:
            barrier.wait()
            patch_editing_segment(project_dir, sid, {"cn_text": new_text_for[sid]})
        except Exception as exc:  # pragma: no cover — surfaced via errors list
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(sid,)) for sid in target_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"patch worker errors: {errors}"

    final = json.loads((editing_dir / "segments.json").read_text(encoding="utf-8"))
    final_by_id = {s["segment_id"]: s for s in final}
    for sid in target_ids:
        assert final_by_id[sid]["cn_text"] == new_text_for[sid], (
            f"P0-5 regression: concurrent patch on {sid} was overwritten "
            f"by another thread. Final cn_text={final_by_id[sid]['cn_text']!r}, "
            f"expected {new_text_for[sid]!r}."
        )


def test_editing_voice_map_concurrent_set_does_not_lose_updates(tmp_path):
    """Spawn 6 threads each setting a different voice override. All six
    overrides must coexist in the final voice_map.json."""
    from services.jobs.editing import EDITING_SUBDIR
    from services.jobs.editing_voice_map import set_voice_override

    project_dir = tmp_path
    editing_dir = project_dir / EDITING_SUBDIR
    editing_dir.mkdir(parents=True, exist_ok=True)
    # voice_map.json is created lazily; segments.json must exist for
    # mark_segment_status's _ensure_editing_dir check.
    seed_segments = [
        {"segment_id": f"seg_{i:03d}", "cn_text": "x", "speaker_id": "spk_a"}
        for i in range(6)
    ]
    (editing_dir / "segments.json").write_text(
        json.dumps(seed_segments, ensure_ascii=False), encoding="utf-8",
    )

    voices = [(f"seg_{i:03d}", "minimax", f"voice_{i}") for i in range(6)]
    barrier = threading.Barrier(len(voices))
    errors: list[Exception] = []

    def worker(sid: str, provider: str, voice_id: str) -> None:
        try:
            barrier.wait()
            set_voice_override(project_dir, sid, provider=provider, voice_id=voice_id)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(sid, provider, vid))
        for sid, provider, vid in voices
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"voice_map worker errors: {errors}"

    final = json.loads((editing_dir / "voice_map.json").read_text(encoding="utf-8"))
    for sid, provider, vid in voices:
        assert sid in final, f"P0-5 regression: voice_map missing {sid}"
        assert final[sid]["voice_id"] == vid, (
            f"P0-5 regression: voice_map[{sid}].voice_id was overwritten; "
            f"got {final[sid]['voice_id']!r} expected {vid!r}"
        )
