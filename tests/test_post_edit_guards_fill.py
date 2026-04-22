"""§16.4 plan guard tests — fill-ins not covered elsewhere.

Plan doc: ``docs/plans/2026-04-18-studio-post-edit-plan.md`` §16.4
(2026-04-21 post-edit milestone guard list). The ones covered here are:

- ``test_no_raw_open_wb_on_shared_paths`` (§16.4 row 11) — AST guard:
  the modules that touch ``editor/tts_segments/{sid}.wav`` must never
  ``open(target, 'wb')`` directly. The only allowed write path is
  temp-then-rename (``write_audio_safely`` / ``apply_draft_segment``)
  so hardlinks to the source's baseline wav are not inadvertently
  poisoned (plan D27).

- ``test_editing_status_reentrancy`` (§16.4 row 1) — calling
  ``enter_editing`` on a job already in ``editing`` status returns 409
  rather than silently re-snapshotting the baseline.

- ``test_logs_redactor_covers_registry_providers`` (§16.4 row 6) —
  when a new provider is registered in the tts / llm registry, the
  shared ``build_default_redactor`` picks it up automatically (no
  hardcoded provider list hides the new name from redaction).

- ``test_editing_touched_at_refresh_on_mutation`` (§16.4 row 12) —
  mutation endpoints refresh ``editing_touched_at`` via
  ``touch_editing``; the GET ``/editing/segments`` read endpoint does
  NOT, so polling the page doesn't keep the 24h idle scanner at bay
  forever.

- ``test_copy_as_new_preserves_source_draft_on_runner_failure``
  (§16.4 row 13) — if Phase A5 ``runner.submit_job_from_existing_project_dir``
  raises, the source job's ``status='editing'``, ``editor/editing/``
  contents, and ``editing_touched_at`` all stay exactly as before.

- ``test_editing_idle_scanner_cancels_25h_job`` (§16.4 row 8) — a job
  whose ``editing_touched_at`` is 25h in the past is flagged as a
  cancel candidate by ``scan_editing_idle``.

Not covered here (see §16 cleanup roadmap in plan):

- ``test_copy_ttl_respects_user_and_lineage`` / ``test_copy_ttl_select_for_update``
  require a real PostgreSQL for ``FOR UPDATE`` — verified in staging.
- ``test_commit_overwrite_happy_path`` / ``test_commit_copy_as_new_happy_path``
  require a full pipeline fixture — verified via live smoke tests.
"""

from __future__ import annotations

import ast
import hashlib
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"


# ============================================================================
# §16.4 row 11 — test_no_raw_open_wb_on_shared_paths
# ============================================================================


# Modules that operate on ``editor/tts_segments/`` (shared hardlink inodes).
_SCANNED_MODULES = (
    "src/services/jobs/copy_service.py",
    "src/services/jobs/editing.py",
    "src/services/jobs/editing_commit.py",
    "src/services/jobs/editing_tts.py",
)

# A first-arg expression is considered "temp file" (and thus allowed for
# ``open(..., 'wb')``) if it matches any of these patterns:
#   - a Name whose id starts with ``tmp_`` or ``temp_`` (``tmp_fd``, ``temp_path``)
#   - an Attribute whose trailing attr matches the same prefixes
#     (e.g. ``self.tmp_path``)
# Anything else ⇒ the module is trying to write to a non-temp path directly,
# which with hardlinks in play risks poisoning the source's inode.
_TEMP_PREFIXES = ("tmp", "tmp_", "temp", "temp_")


def _is_temp_target(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        name = node.id.lower()
    elif isinstance(node, ast.Attribute):
        name = node.attr.lower()
    else:
        return False
    return any(name == p or name.startswith(p) for p in _TEMP_PREFIXES)


def _find_forbidden_wb_opens(source: str) -> list[tuple[int, str]]:
    """Walk ``source``'s AST looking for ``open(X, 'wb')`` calls where X
    isn't a temp-target name. Returns a list of (lineno, description)."""
    tree = ast.parse(source)
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Only bare ``open(...)`` — skip ``io.open`` / ``gzip.open`` / etc.
        if not (isinstance(func, ast.Name) and func.id == "open"):
            continue
        if len(node.args) < 2:
            continue
        mode_arg = node.args[1]
        # Mode constant 'wb' (or 'wb+' / 'w+b' variants).
        if not (isinstance(mode_arg, ast.Constant) and isinstance(mode_arg.value, str)):
            continue
        if "w" not in mode_arg.value or "b" not in mode_arg.value:
            continue
        target = node.args[0]
        if _is_temp_target(target):
            continue
        # Describe the offender for diagnostics.
        try:
            desc = ast.unparse(target)
        except Exception:
            desc = "<unparse-failed>"
        offenders.append((node.lineno, desc))
    return offenders


@pytest.mark.parametrize("module_rel", _SCANNED_MODULES)
def test_no_raw_open_wb_on_shared_paths(module_rel: str) -> None:
    """§16.4 row 11 — forbid ``open(path, 'wb')`` on non-temp targets in
    modules that manipulate shared (hardlinkable) audio files. Writing
    through ``open('wb')`` on a hardlinked target corrupts the source
    inode the hardlink shares (plan D27)."""
    path = REPO_ROOT / module_rel
    source = path.read_text(encoding="utf-8")
    offenders = _find_forbidden_wb_opens(source)
    assert offenders == [], (
        f"{module_rel} has raw open(..., 'wb') on non-temp targets: "
        f"{offenders}. Use write_audio_safely() / apply_draft_segment() "
        "instead — they do temp-then-replace so hardlinks aren't polluted."
    )


# ============================================================================
# §16.4 row 1 — test_editing_status_reentrancy
# ============================================================================


def _make_succeeded_project(tmp_path: Path, job_id: str = "job_reenter") -> tuple:
    """Build a minimal succeeded job + project_dir that can enter editing."""
    if str(SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(SRC_ROOT))
    from services.jobs.models import JobRecord, JOB_STATUS_SUCCEEDED
    from services.jobs.store import JobStore

    project_dir = tmp_path / "projects" / job_id
    (project_dir / "editor").mkdir(parents=True)
    # Minimal baseline segments — enter_editing snapshots these.
    import json
    (project_dir / "editor" / "segments.json").write_text(
        json.dumps(
            [
                {
                    "segment_id": "seg_001",
                    "speaker_id": "A",
                    "cn_text": "x",
                    "source_text": "x",
                    "start_ms": 0,
                    "end_ms": 1000,
                }
            ]
        ),
        encoding="utf-8",
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    record = JobRecord(
        job_id=job_id,
        job_type="localize_video",
        source_type="youtube_url",
        source_ref="https://example.com/x",
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
    return record, store, project_dir


def test_editing_status_reentrancy(tmp_path: Path) -> None:
    """§16.4 row 1 — calling enter_editing twice for the same job must
    raise EditingConflictError on the second call, not silently wipe
    the editing buffer or re-snapshot baseline over user edits."""
    if str(SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(SRC_ROOT))
    from services.jobs.editing import EditingConflictError, enter_editing

    record, store, _project_dir = _make_succeeded_project(tmp_path)
    enter_editing(record, store)
    # Reload — the in-memory record is now stale (status is still
    # 'succeeded' in our copy; enter_editing updated the store).
    refreshed = store.require_job(record.job_id)
    assert refreshed.status == "editing", refreshed.status

    # Second call must refuse.
    with pytest.raises(EditingConflictError):
        enter_editing(refreshed, store)


# ============================================================================
# §16.4 row 6 — test_logs_redactor_covers_registry_providers
# ============================================================================


def test_logs_redactor_covers_registry_providers(monkeypatch) -> None:
    """§16.4 row 6 — build_default_redactor consults the live provider
    registries (``_collect_llm_provider_names`` + ``_collect_tts_provider_names``)
    at build time; a newly-registered provider's name must get redacted
    out of user-visible logs without anyone updating a hardcoded list.
    """
    if str(SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(SRC_ROOT))
    import services.jobs.logs_redactor as redactor_module

    # Patch the TTS collector (it's the easier of the two to override
    # since it returns a plain list). The assertion is that the
    # returned redactor actually strips the sentinel — proving the list
    # flowed through ``Redactor(names)`` without being hardcoded.
    sentinel_provider = "QuantumBabbleVox"

    def _fake_tts_names() -> list[str]:
        return ["MiniMax", sentinel_provider]

    monkeypatch.setattr(
        redactor_module, "_collect_tts_provider_names", _fake_tts_names
    )
    redactor = redactor_module.build_default_redactor()
    input_line = f"Calling {sentinel_provider} with key abc123..."
    cleaned = redactor.redact(input_line)
    assert sentinel_provider not in cleaned, (
        f"redactor did not cover newly-registered provider "
        f"{sentinel_provider!r}; output was {cleaned!r}"
    )


# ============================================================================
# §16.4 row 12 — test_editing_touched_at_refresh_on_mutation
# ============================================================================


def test_editing_touched_at_refresh_on_mutation(tmp_path: Path) -> None:
    """§16.4 row 12 — mutation-shaped service methods (patch_editing_segment,
    split_editing_segment, mark_editing_segment_status,
    set_editing_voice_override, clear_editing_voice_override) refresh
    ``editing_touched_at`` on success, so the 24h idle scanner resets
    whenever the user is actively editing. A GET-shaped method
    (``get_editing_segments``) must NOT refresh — otherwise a polling
    tab would prevent the idle cancel forever.
    """
    if str(SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(SRC_ROOT))
    from services.jobs.editing import enter_editing
    from services.jobs.service import JobService
    from services.jobs.store import JobStore

    record, store, _ = _make_succeeded_project(tmp_path, job_id="job_touched_refresh")
    enter_editing(record, store)
    editing_record = store.require_job(record.job_id)
    original_touched = editing_record.editing_touched_at
    assert original_touched is not None, (
        "enter_editing must set editing_touched_at"
    )

    # Force the touched_at backwards so a "refresh" is observably newer.
    from dataclasses import replace as dc_replace
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    store.save_job(dc_replace(editing_record, editing_touched_at=past))

    class _NullRunner:
        pass
    service = JobService(store=store, runner=_NullRunner())

    # Mutation path: patching cn_text should refresh touched_at.
    service.patch_editing_segment(record.job_id, "seg_001", {"cn_text": "new"})
    after_patch = store.require_job(record.job_id)
    assert after_patch.editing_touched_at is not None
    assert after_patch.editing_touched_at > past, (
        "patch_editing_segment must refresh editing_touched_at "
        f"(was {past}, now {after_patch.editing_touched_at})"
    )

    # Read path: GET /editing/segments must NOT refresh (would defeat
    # the idle scanner's whole point).
    store.save_job(dc_replace(after_patch, editing_touched_at=past))
    service.get_editing_segments(record.job_id)
    after_read = store.require_job(record.job_id)
    assert after_read.editing_touched_at == past, (
        "get_editing_segments must NOT refresh editing_touched_at "
        f"(expected unchanged {past}, got {after_read.editing_touched_at})"
    )


# ============================================================================
# §16.4 row 13 — test_copy_as_new_preserves_source_draft_on_runner_failure
# ============================================================================


def test_copy_as_new_preserves_source_draft_on_runner_failure(tmp_path: Path) -> None:
    """§16.4 row 13 — if Phase A5 (runner.submit_job_from_existing_project_dir)
    raises, copy_as_new must leave the source exactly as we found it:
    status='editing', editor/editing/ contents unchanged, new project
    dir cleaned up, no new Job record persisted.
    """
    if str(SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(SRC_ROOT))
    import json

    from services.jobs.editing import enter_editing
    from services.jobs.editing_commit import commit_editing_pipeline
    from services.jobs.store import JobStore

    record, store, project_dir = _make_succeeded_project(
        tmp_path, job_id="job_copy_rollback"
    )
    enter_editing(record, store)
    editing_record = store.require_job(record.job_id)

    # Seed an editing/ artefact so we can hash-compare after the failure.
    editing_dir = project_dir / "editor" / "editing"
    sentinel = editing_dir / "marker.bin"
    sentinel.write_bytes(b"source_editing_marker_payload_v1")
    pre_sha = hashlib.sha256(sentinel.read_bytes()).hexdigest()
    pre_touched = editing_record.editing_touched_at

    # Runner stub that raises on submit → Phase A5 failure.
    class _FailingRunner:
        project_root = tmp_path / "projects"

        def submit_job_from_existing_project_dir(self, *args, **kwargs):
            raise RuntimeError("simulated runner overload")

    with pytest.raises(Exception):
        commit_editing_pipeline(
            editing_record,
            store,
            _FailingRunner(),
            strategy="copy_as_new",
            copy_display_name="测试副本 1",
            new_job_id_factory=lambda: "job_copy_rollback_copy",
        )

    # --- Post-conditions: source must be pristine -------------------
    post = store.require_job(record.job_id)
    assert post.status == "editing", (
        f"source must stay in editing state, got {post.status}"
    )
    assert post.editing_touched_at == pre_touched, (
        "editing_touched_at must not be bumped by a failed commit"
    )
    assert sentinel.exists(), "editing/ marker file was deleted by rollback"
    assert (
        hashlib.sha256(sentinel.read_bytes()).hexdigest() == pre_sha
    ), "editing/ marker contents changed despite rollback"

    # --- New copy must be cleaned up --------------------------------
    try:
        store.require_job("job_copy_rollback_copy")
    except KeyError:
        pass  # expected — new job record must not persist
    else:
        pytest.fail("new job record leaked despite Phase A5 rollback")


# ============================================================================
# §16.4 row 8 — test_editing_idle_scanner_cancels_25h_job
# ============================================================================


def test_editing_idle_scanner_cancels_25h_job(tmp_path: Path) -> None:
    """§16.4 row 8 — a job that's been in ``editing`` with
    ``editing_touched_at`` 25h in the past lands on the scanner's
    cancellation candidate list. The scanner itself is the detection
    half; the cancel callback wiring is verified separately in
    test_phase1_guards's editing module structure assertions."""
    if str(SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(SRC_ROOT))
    from services.web_ui.editing_idle_scanner import scan_editing_idle
    from services.jobs.editing import enter_editing

    # Seed an editing job whose touched_at is well into the past.
    record, store, _ = _make_succeeded_project(tmp_path, job_id="job_idle_25h")
    enter_editing(record, store)
    editing_record = store.require_job(record.job_id)
    from dataclasses import replace as dc_replace
    ancient = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    store.save_job(dc_replace(editing_record, editing_touched_at=ancient))

    # Point the scanner at our tmp jobs dir by monkey-injecting the
    # env var the scanner reads. (The scanner is designed to be called
    # with a ``now`` arg so tests don't have to time-travel.)
    import os
    old_env = os.environ.get("AIVIDEOTRANS_JOBS_DIR")
    os.environ["AIVIDEOTRANS_JOBS_DIR"] = str(tmp_path / "jobs")
    try:
        cancelled_ids: list[str] = []

        # Scanner signature is ``callback(job_id, reason)`` positional —
        # see src/services/web_ui/editing_idle_scanner.py line ~135.
        def _cancel_callback(job_id: str, reason: str) -> bool:
            cancelled_ids.append(job_id)
            return True

        result = scan_editing_idle(
            datetime.now(timezone.utc),
            _cancel_callback,
        )
        assert record.job_id in [c for c in result["candidates"]], (
            f"25h-idle editing job not on candidate list: {result}"
        )
        assert record.job_id in cancelled_ids, (
            "cancel callback was not invoked for an idle editing job"
        )
    finally:
        if old_env is None:
            os.environ.pop("AIVIDEOTRANS_JOBS_DIR", None)
        else:
            os.environ["AIVIDEOTRANS_JOBS_DIR"] = old_env
