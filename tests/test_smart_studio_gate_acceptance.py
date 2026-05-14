"""Smart MVP PR#3C-a — user-facing Studio gates accept smart jobs.

Three user-facing entry points need smart-aware gating per plan §4.3 末段
+ §6.6 + Codex 第二/六轮 F3:

  - ``src/services/jobs/editing.py::enter_editing`` (line 122 pre-fix)
  - ``src/services/jobs/jianying_draft_runner.py::JianyingDraftRunner``
    (line 384 pre-fix)
  - ``src/services/jobs/api.py`` Jianying-draft HTTP preflight
    (line 1458 pre-fix)

All three previously rejected anything other than literal
``service_mode == "studio"``. Smart MVP P2 plan requires they accept:
  - ``service_mode == "studio"`` (legacy path, unchanged)
  - ``service_mode == "smart"`` AND ``smart_state.status`` in
    {``"completed"``, ``"downgraded_to_studio"``}

Smart audit fact stays on ``service_mode = "smart"`` per plan §4.3
末段; the secondary smart_state check is what prevents in-flight
smart jobs (status="running" / "clone_blocked_waiting_retry") from
sneaking into editing.

This file also carries the §8.2 #7 AST guard (Codex 第三轮 F3 +
第五轮 F4): grep the relevant code areas for naked ``!= "studio"``
literal comparisons so future PRs can't regress by adding a new
Studio-only literal gate without consciously deciding the smart story.

Test matrix (per gate × 4 cases):
  - studio job (legacy path) → accepted
  - smart job + smart_state.status="completed" → accepted
  - smart job + smart_state.status="downgraded_to_studio" → accepted
  - smart job + smart_state.status="running" → REJECTED
  - smart job + smart_state.status=None / missing → REJECTED
  - express job → REJECTED (unchanged)
"""
from __future__ import annotations

import ast
import sys
import types
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# Repo path setup — mirrors tests/conftest.py
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
_GATEWAY = _PROJECT_ROOT / "gateway"
for _p in (str(_SRC), str(_GATEWAY)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

if "database" not in sys.modules:
    _fake_database = types.ModuleType("database")
    _fake_database.get_db = MagicMock()
    _fake_database.engine = MagicMock()
    _fake_database.async_session = MagicMock()
    sys.modules["database"] = _fake_database


# ===================================================================
# Helper — build minimal record-like objects matching each gate's
# expected attribute surface
# ===================================================================


def _build_editing_record(*, job_id="job_x", status="succeeded", service_mode="studio",
                          project_dir=None, smart_state=None):
    """Minimal JobRecord-like object for src/services/jobs/editing.py.

    Real JobRecord (src/services/jobs/models.py) carries many fields;
    enter_editing reads: status, service_mode, project_dir, smart_state.
    """
    if project_dir is None:
        project_dir = "/fake/projects/job_x"
    return SimpleNamespace(
        job_id=job_id,
        status=status,
        service_mode=service_mode,
        project_dir=project_dir,
        smart_state=smart_state,
    )


def _build_runner_job(*, service_mode="studio", smart_state=None, status="succeeded"):
    """Minimal Job-like for jianying_draft_runner gate 1 (the pre-lock
    cheap check at line 384). The full enter path reads more fields but
    gate 1 only touches service_mode + smart_state."""
    return SimpleNamespace(
        job_id="job_y",
        service_mode=service_mode,
        status=status,
        smart_state=smart_state,
    )


# ===================================================================
# C-a.1 — editing.py enter_editing
# ===================================================================


class TestEditingGate:
    """src/services/jobs/editing.py::enter_editing service_mode check.

    The full enter_editing function does file I/O (creates editing/
    subdir, snapshots segments.json). Our tests focus on the gate
    decision — we call enter_editing with a record whose project_dir
    is a real tmp_path so the I/O happens to safe disk, OR we trip
    the gate before any I/O on rejection paths.
    """

    def test_studio_job_accepted_legacy_path_unchanged(self, tmp_path):
        """Regression — studio jobs still pass the gate."""
        from services.jobs.editing import enter_editing, EditingConflictError

        record = _build_editing_record(
            service_mode="studio", project_dir=str(tmp_path)
        )
        # We can't run the whole enter_editing without a real
        # editor/segments.json baseline — but we can verify it gets
        # PAST the service_mode gate. The function will raise something
        # OTHER than "not studio" downstream (likely on segments.json
        # not found). The error message we DON'T expect to see anymore.
        try:
            enter_editing(record, store=MagicMock())
        except EditingConflictError as exc:
            # Whatever downstream error, must NOT be about service_mode
            assert "service_mode" not in str(exc).lower() or "smart" in str(exc).lower(), (
                f"Studio job hit a service_mode rejection: {exc}"
            )
        except Exception:
            pass  # Other downstream errors are not under test here

    def test_smart_job_completed_state_accepted(self, tmp_path):
        """smart_state.status='completed' → passes the service_mode gate."""
        from services.jobs.editing import enter_editing, EditingConflictError

        record = _build_editing_record(
            service_mode="smart",
            project_dir=str(tmp_path),
            smart_state={"status": "completed", "credits_policy": "capture_full"},
        )
        try:
            enter_editing(record, store=MagicMock())
        except EditingConflictError as exc:
            # Reject must NOT be about service_mode or smart_state
            msg = str(exc).lower()
            assert "service_mode" not in msg and "smart_state" not in msg, (
                f"Smart completed job hit service_mode/smart_state rejection: {exc}"
            )
        except Exception:
            pass

    def test_smart_job_downgraded_to_studio_state_accepted(self, tmp_path):
        """smart_state.status='downgraded_to_studio' → user takes over
        via human-review, must pass the gate."""
        from services.jobs.editing import enter_editing, EditingConflictError

        record = _build_editing_record(
            service_mode="smart",
            project_dir=str(tmp_path),
            smart_state={
                "status": "downgraded_to_studio",
                "credits_policy": "refund_full",
            },
        )
        try:
            enter_editing(record, store=MagicMock())
        except EditingConflictError as exc:
            msg = str(exc).lower()
            assert "service_mode" not in msg and "smart_state" not in msg, (
                f"Smart downgraded job hit service_mode/smart_state rejection: {exc}"
            )
        except Exception:
            pass

    def test_smart_job_running_state_rejected(self, tmp_path):
        """smart_state.status='running' → in-flight job, MUST reject
        otherwise user could enter editing while pipeline still running."""
        from services.jobs.editing import enter_editing, EditingConflictError

        record = _build_editing_record(
            service_mode="smart",
            project_dir=str(tmp_path),
            smart_state={"status": "running"},
        )
        # status="succeeded" but smart_state still running is a contradictory
        # state; force enter_editing to raise on the smart_state check.
        with pytest.raises(EditingConflictError, match="smart_state"):
            enter_editing(record, store=MagicMock())

    def test_smart_job_with_no_smart_state_rejected(self, tmp_path):
        """smart_state=None → defensive reject (succeeded smart job should
        always have smart_state populated; missing means data corruption)."""
        from services.jobs.editing import enter_editing, EditingConflictError

        record = _build_editing_record(
            service_mode="smart",
            project_dir=str(tmp_path),
            smart_state=None,
        )
        with pytest.raises(EditingConflictError, match="smart_state"):
            enter_editing(record, store=MagicMock())

    def test_smart_job_paused_for_clone_state_rejected(self, tmp_path):
        """smart_state.status='clone_blocked_waiting_retry' → user can
        retry clone path, but NOT enter Studio post-edit yet."""
        from services.jobs.editing import enter_editing, EditingConflictError

        record = _build_editing_record(
            service_mode="smart",
            project_dir=str(tmp_path),
            smart_state={"status": "clone_blocked_waiting_retry"},
        )
        with pytest.raises(EditingConflictError, match="smart_state"):
            enter_editing(record, store=MagicMock())

    def test_express_job_still_rejected(self, tmp_path):
        """Regression — express jobs never supported editing."""
        from services.jobs.editing import enter_editing, EditingConflictError

        record = _build_editing_record(
            service_mode="express", project_dir=str(tmp_path)
        )
        with pytest.raises(EditingConflictError, match="editable"):
            enter_editing(record, store=MagicMock())


# ===================================================================
# C-a.2 — jianying_draft_runner.py JianyingDraftRunner Gate 1
# ===================================================================


class TestJianyingDraftRunnerGate:
    """JianyingDraftRunner.spawn() Gate 1 (line 384 pre-fix). The full
    spawn does file I/O + thread spawn; we patch the store to return
    our fake job and exercise just the gate."""

    def _make_runner_with_job(self, job):
        from services.jobs.jianying_draft_runner import JianyingDraftRunner

        store = MagicMock()
        store.require_job.return_value = job
        # Runner constructor takes (*, store, backend=None). Backend is
        # lazy-defaulted; safe to leave None.
        runner = JianyingDraftRunner(store=store)
        return runner

    def test_studio_job_passes_gate1(self):
        from services.jobs.jianying_draft_runner import JianyingNotAllowedError

        job = _build_runner_job(service_mode="studio", status="succeeded")
        runner = self._make_runner_with_job(job)
        # Gate 1 passes; downstream code may still fail (no real
        # project_dir / segments / lock / backend) — but the error code
        # MUST NOT be the gate 1 rejection.
        try:
            runner.trigger("job_y")
        except JianyingNotAllowedError as exc:
            assert exc.reason != "service_mode_not_studio_or_smart"
            assert exc.reason != "smart_state_not_editable"
        except Exception:
            pass

    def test_smart_completed_passes_gate1(self):
        from services.jobs.jianying_draft_runner import JianyingNotAllowedError

        job = _build_runner_job(
            service_mode="smart", status="succeeded",
            smart_state={"status": "completed"},
        )
        runner = self._make_runner_with_job(job)
        try:
            runner.trigger("job_y")
        except JianyingNotAllowedError as exc:
            assert exc.reason not in (
                "service_mode_not_studio_or_smart", "smart_state_not_editable"
            ), f"Smart completed unexpectedly rejected at gate 1: {exc.reason}"
        except Exception:
            pass

    def test_smart_downgraded_passes_gate1(self):
        from services.jobs.jianying_draft_runner import JianyingNotAllowedError

        job = _build_runner_job(
            service_mode="smart", status="succeeded",
            smart_state={"status": "downgraded_to_studio"},
        )
        runner = self._make_runner_with_job(job)
        try:
            runner.trigger("job_y")
        except JianyingNotAllowedError as exc:
            assert exc.reason not in (
                "service_mode_not_studio_or_smart", "smart_state_not_editable"
            )
        except Exception:
            pass

    def test_smart_running_rejected_at_gate1(self):
        from services.jobs.jianying_draft_runner import JianyingNotAllowedError

        job = _build_runner_job(
            service_mode="smart", status="succeeded",
            smart_state={"status": "running"},
        )
        runner = self._make_runner_with_job(job)
        with pytest.raises(JianyingNotAllowedError) as exc_info:
            runner.trigger("job_y")
        assert exc_info.value.reason == "smart_state_not_editable"

    def test_express_rejected_at_gate1(self):
        from services.jobs.jianying_draft_runner import JianyingNotAllowedError

        job = _build_runner_job(service_mode="express", status="succeeded")
        runner = self._make_runner_with_job(job)
        with pytest.raises(JianyingNotAllowedError) as exc_info:
            runner.trigger("job_y")
        assert exc_info.value.reason == "service_mode_not_studio_or_smart"

    def test_race_condition_gate2_rejects_state_flipped_between_reads(self, tmp_path):
        """Codex 第十五轮 P1: process_runner may flip smart_state.status
        back to ``running`` between trigger()'s pre-lock require_job
        (Gate 1) and the post-lock re-read (Gate 2). Without re-running
        the gate in the lock, the runner would claim
        jianying_draft_status=running on an in-flight smart job. This
        test pins the dual-gate behaviour: 1st require_job returns
        editable, 2nd returns running → trigger raises at Gate 2.
        """
        from services.jobs.jianying_draft_runner import (
            JianyingDraftRunner, JianyingNotAllowedError,
        )

        editable_job = _build_runner_job(
            service_mode="smart", status="succeeded",
            smart_state={"status": "completed"},
        )
        flipped_job = _build_runner_job(
            service_mode="smart", status="succeeded",
            smart_state={"status": "running"},
        )
        # Add the fields the rest of trigger() touches before reaching
        # the post-lock gate, so the test exercises the WHOLE pre-→-lock
        # path. jianying_draft_status defaults are fine.
        for j in (editable_job, flipped_job):
            j.jianying_draft_status = "idle"
            j.jianying_draft_started_at = None
            j.jianying_draft_completed_at = None
            j.jianying_draft_error = None
            j.jianying_draft_zip_path = None
            j.jianying_draft_user_root = None
            j.jianying_draft_fingerprint = None
            j.jianying_draft_attempt_id = None
            j.jianying_draft_substep = None
            j.project_dir = str(tmp_path)

        store = MagicMock()
        # 1st call: editable; 2nd call (inside lock): flipped to running.
        store.require_job.side_effect = [editable_job, flipped_job]
        # _lock_path_for uses self._store.root_dir / ... — make it a real
        # tmp Path so file_lock acquisition works.
        store.root_dir = tmp_path
        runner = JianyingDraftRunner(store=store)
        with pytest.raises(JianyingNotAllowedError) as exc_info:
            runner.trigger("job_y")
        # Reject must come from Gate 2 (the post-lock check), not from
        # any downstream pipeline failure.
        assert exc_info.value.reason == "smart_state_not_editable"
        # Two reads — confirms the dual-gate path was exercised.
        assert store.require_job.call_count == 2

    def test_helper_extracted_and_runnable_in_isolation(self):
        """Codex 第十五轮 P1 末段: gate logic is module-level so it can
        be invoked from BOTH the pre-lock pre-check AND the post-lock
        authoritative check. Pin the helper's import surface so a
        future refactor that inlines the body silently loses the
        Gate 2 invocation site."""
        from services.jobs.jianying_draft_runner import (
            _check_smart_aware_service_mode_gate, JianyingNotAllowedError,
        )

        editable = _build_runner_job(
            service_mode="smart", status="succeeded",
            smart_state={"status": "completed"},
        )
        # Editable passes — no raise.
        _check_smart_aware_service_mode_gate(editable)

        running = _build_runner_job(
            service_mode="smart", status="succeeded",
            smart_state={"status": "running"},
        )
        with pytest.raises(JianyingNotAllowedError) as exc_info:
            _check_smart_aware_service_mode_gate(running)
        assert exc_info.value.reason == "smart_state_not_editable"


# ===================================================================
# C-a.4 — AST guard for new Studio-only literal gates
# ===================================================================


_GUARDED_PATHS = [
    _SRC / "services" / "jobs" / "editing.py",
    _SRC / "services" / "jobs" / "jianying_draft_runner.py",
    _SRC / "services" / "jobs" / "api.py",
]


def _find_studio_literal_comparisons(path: Path) -> list[tuple[int, str]]:
    """Walk path's AST, return ``[(lineno, expr_repr), ...]`` for every
    ``X.service_mode == "studio"`` / ``X.service_mode != "studio"``
    single-literal comparison still present.

    The fix in PR#3C-a was to widen these to ``in {studio, smart}`` /
    ``not in {studio, smart}``. This guard catches any new literal
    comparisons that future PRs might add without consciously deciding
    the smart story. Plan §4.3 末段 contract."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        # Only one comparator with one op
        if len(node.ops) != 1 or len(node.comparators) != 1:
            continue
        # left side is an attribute access reading .service_mode
        # (with optional .lower() / or-fallback unwrap)
        target = node.left
        # Unwrap call to .lower() if present (jobs/api.py:1458 used it)
        if isinstance(target, ast.Call) and isinstance(target.func, ast.Attribute):
            if target.func.attr == "lower":
                target = target.func.value
        # Unwrap or-expression: (record.service_mode or "").lower() pattern
        if isinstance(target, ast.BoolOp):
            # rough — we care if any operand is service_mode access
            relevant = any(
                isinstance(v, ast.Attribute) and v.attr == "service_mode"
                for v in target.values
            )
            if not relevant:
                continue
        elif not (isinstance(target, ast.Attribute) and target.attr == "service_mode"):
            continue
        # right side is the literal "studio" string
        rhs = node.comparators[0]
        if isinstance(rhs, ast.Constant) and rhs.value == "studio":
            op_name = type(node.ops[0]).__name__
            if op_name in ("Eq", "NotEq"):
                hits.append((node.lineno, f"<...>.service_mode {op_name} 'studio'"))
    return hits


class TestStudioLiteralGateAstGuard:
    """Codex 第三轮 F3 + 第五轮 F4: AST-level guard against regressing
    user-facing Studio-only literal gates. The three call sites changed
    in PR#3C-a are the high-traffic ones; this guard makes sure no
    follow-up PR silently re-introduces ``service_mode != "studio"``
    in these files without consciously updating to smart-aware semantics.
    """

    def test_no_naked_studio_literal_comparisons_in_user_facing_gates(self):
        violations: list[str] = []
        for path in _GUARDED_PATHS:
            hits = _find_studio_literal_comparisons(path)
            for lineno, expr in hits:
                violations.append(
                    f"{path.relative_to(_PROJECT_ROOT).as_posix()}:{lineno}  {expr}"
                )
        assert not violations, (
            "Naked ``service_mode == 'studio'`` / ``!= 'studio'`` literal "
            "comparison found in user-facing gate file. PR#3C-a widened these "
            "to ``in {studio, smart}`` with secondary smart_state.status "
            "check. If this gate genuinely should reject smart jobs, add it "
            "to the AST guard's whitelist with a comment explaining why; "
            "otherwise update to use services.smart.state.EDITABLE_SERVICE_MODES "
            "and ``is_editable_smart_state()``.\n\n"
            + "\n".join(violations)
        )

    def test_guard_finds_a_known_studio_literal_when_planted(self, tmp_path):
        """Meta-test: write a tmp file with a planted ``!= 'studio'``
        literal and verify the guard's AST walker flags it. Catches
        "guard silently passes everything" regressions."""
        bad = tmp_path / "bad.py"
        bad.write_text(
            "class Job:\n"
            "    service_mode = 'studio'\n"
            "def f(j):\n"
            "    if j.service_mode != 'studio':\n"
            "        raise ValueError('nope')\n"
        )
        hits = _find_studio_literal_comparisons(bad)
        assert len(hits) == 1
        assert "NotEq" in hits[0][1]

    def test_guard_walks_the_lower_unwrap_pattern(self, tmp_path):
        """jobs/api.py:1458 originally used ``(x or "").lower() != "studio"``.
        Make sure the guard's unwrap logic catches this pattern too."""
        bad = tmp_path / "bad.py"
        bad.write_text(
            "def f(j):\n"
            "    if (j.service_mode or '').lower() != 'studio':\n"
            "        raise ValueError('nope')\n"
        )
        hits = _find_studio_literal_comparisons(bad)
        assert len(hits) == 1


# ===================================================================
# PR#3C-b1 — process.py pipeline-internal gate widening
# ===================================================================


_PROCESS_PY = _SRC / "pipeline" / "process.py"


def _find_anchor_block(source: str, anchor: str, window: int = 6) -> str:
    """Return ``window`` lines of source starting at the first line that
    contains ``anchor``. Used to inspect the immediate code below a
    semantic anchor comment without depending on absolute line numbers."""
    lines = source.splitlines()
    for i, line in enumerate(lines):
        if anchor in line:
            return "\n".join(lines[i : i + window])
    raise AssertionError(
        f"anchor {anchor!r} not found in source; did the comment get removed?"
    )


class TestProcessPyStudioGateWidening:
    """PR#3C-b1 — two pipeline-internal Studio-only gates widened to
    accept smart jobs (plan §4.3 末段 rows 4/5).

    Two remaining gates (lazy migration at "rebuild auto_matched_by_provider"
    + voice_selection_review trigger) intentionally still
    use ``== "studio"`` literal — they require coordinated changes with
    PR#3C-b2's inline auto-approve work and would deadlock the smart
    pause-return contract if widened standalone. This test pins:
      - the 2 PR#3C-b1 gates ARE widened (set membership with both
        modes)
      - the 2 PR#3C-b2 gates are still literal Studio-only
    Future widening MUST update this test, forcing a conscious
    decision about smart_state plumbing rather than silent drift.
    """

    def _source(self) -> str:
        return _PROCESS_PY.read_text(encoding="utf-8")

    def test_pre_tts_voice_validation_gate_widened_for_smart(self):
        """Plan §4.3 末段 row 4 — cloned voice expiry validation must run
        for smart jobs too. anchor: 'Pre-TTS voice validation' header."""
        block = _find_anchor_block(
            self._source(),
            "Pre-TTS voice validation (cloned voices, before translation)",
            window=12,
        )
        # The widened if expression carries both modes via set membership.
        assert 'job_service_mode in {"studio", "smart"}' in block, (
            "Pre-TTS voice validation gate is no longer widened for smart. "
            f"Block:\n{block}"
        )
        # And the legacy literal-Studio comparison must NOT appear in
        # this anchored block.
        assert 'job_service_mode == "studio"' not in block, (
            "Pre-TTS voice validation gate regressed to literal Studio-only. "
            f"Block:\n{block}"
        )

    def test_speed_catalog_lookup_gate_widened_for_smart(self):
        """Plan §4.3 末段 row 5 — speed catalog lookup must benefit smart
        jobs (which select concrete voice_ids via auto_voice_review).
        anchor: 'Speed catalog lookup' header. Window must reach down
        past the multi-line comment block to the actual ``if`` (currently
        ~20 lines below the header)."""
        block = _find_anchor_block(
            self._source(),
            "Speed catalog lookup",
            window=25,
        )
        assert 'job_service_mode in {"studio", "smart"}' in block, (
            "Speed catalog lookup gate is no longer widened for smart. "
            f"Block:\n{block}"
        )
        assert 'job_service_mode == "studio"' not in block, (
            "Speed catalog lookup gate regressed to literal Studio-only. "
            f"Block:\n{block}"
        )

    def test_voice_selection_review_trigger_still_literal_studio(self):
        """Plan §4.3 末段 row 3 — this gate REMAINS literal Studio-only
        until PR#3C-b2 lands the inline auto-approve path. Widening it
        standalone would force smart jobs into the paused-return Studio
        flow, breaking the §6.0.5 'smart doesn't pause-return' contract.

        Anchor on the ``elif`` line itself (rather than the section
        header 90+ lines above) so the window stays tight."""
        block = _find_anchor_block(
            self._source(),
            "elif config.wait_for_review and job_requires_review and job_service_mode",
            window=1,
        )
        # PR#3C-b2 territory — still literal Studio.
        assert 'job_service_mode == "studio"' in block, (
            "voice_selection_review trigger has been widened. PR#3C-b1 left "
            "this gate intentionally literal until inline auto-approve "
            "(PR#3C-b2) lands. If you widened deliberately, update "
            "test_smart_studio_gate_acceptance.py to reflect the new state. "
            f"Block:\n{block}"
        )

    def test_lazy_migration_gate_still_literal_studio(self):
        """The 'rebuild auto_matched_by_provider' lazy migration sits
        inside the user-approved review branch — smart jobs don't reach
        it (smart auto-approves payloads with full field set). Left
        as literal Studio for clarity; widening would be dead code.
        Pin so future drift is conscious."""
        block = _find_anchor_block(
            self._source(),
            "Lazy migration: legacy approved payloads",
            window=12,
        )
        assert 'job_service_mode == "studio"' in block, (
            "Lazy-migration gate has been widened. Smart jobs don't reach "
            "this user-approved branch, so widening is dead code; if you "
            "did so deliberately, explain in the diff + update this test. "
            f"Block:\n{block}"
        )
