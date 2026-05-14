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

    def test_pre_tts_voice_validation_gate_widened_with_handoff_for_smart(self):
        """Plan §4.3 末段 row 4 — PR#3C-b2 lands the widening alongside
        the matching ``emit_handoff_markers()`` plumbing. The expired-
        voices branch under this gate now has a smart-specific path
        that emits the three-marker handoff tuple (set_stage PENDING
        + ``[SMART_STATE]`` + web review marker) before the paused
        return, so process_runner / Gateway billing see the consistent
        smart_state.status=downgraded_to_studio when the smart job
        lands waiting_for_review."""
        # Gate itself widened via effective mode (Codex 第十八轮 P1-2:
        # pipeline-control reads effective, not raw).
        gate_block = _find_anchor_block(
            self._source(),
            "Pre-TTS voice validation (cloned voices, before translation)",
            window=24,
        )
        assert 'job_effective_pipeline_mode in {"studio", "smart"}' in gate_block, (
            "Pre-TTS voice validation gate is no longer widened for smart "
            "via job_effective_pipeline_mode (Codex 第十八轮 P1-2). "
            f"Block:\n{gate_block}"
        )

        # Branch under the gate emits the smart handoff three-tuple
        # for expired smart jobs — anchor on the smart-branch docstring
        # comment placed just before the emit_handoff_markers() call.
        branch_block = _find_anchor_block(
            self._source(),
            "Smart expiry → handoff",
            window=20,
        )
        assert "emit_handoff_markers" in branch_block, (
            "Expired-voices smart branch is missing emit_handoff_markers() "
            "call — handoff three-tuple was the whole point of this "
            "PR#3C-b2 widening. Block:\n"
            f"{branch_block}"
        )
        assert '"cloned_voice_expired"' in branch_block, (
            "smart_state_update reason changed away from cloned_voice_expired. "
            f"Block:\n{branch_block}"
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

    def test_voice_selection_review_trigger_widened_with_inline_auto_approve(self):
        """Plan §4.3 末段 row 3 + §6.0.5 + §6.2.1 — PR#3C-b2 lands the
        widening AND the smart-inline-auto-approve path together. Smart
        jobs must NOT pause-return here (§6.0.5 invariant); instead they
        invoke ``evaluate_voice_review`` and apply per-speaker decisions
        in the same frame, falling through to the next pipeline stage.

        PR#3C-b2-fix additionally locks four invariants from Codex 第十八轮:
          - P1-2: pipeline-control branch reads ``job_effective_pipeline_mode``
            not raw ``job_service_mode``
          - P0-2: smart clone path goes through a fail-closed local stub
            (``_build_b2_not_wired_clone_provider``), NOT the real
            ``build_smart_clone_provider`` import
          - P0-1: ``emit_smart_state_marker`` is called WITHOUT setting
            a top-level ``status`` (intermediate-state pollution would
            block editing/jianying gates + settle dispatcher)
          - P1-1: ``cloned_provider_name`` is written as ``clone_provider``
            audit field, NOT as ``tts_provider`` (clone vendor != TTS
            provider for routing)
        """
        # Codex 第十八轮 P1-2: gate uses effective pipeline mode.
        gate_block = _find_anchor_block(
            self._source(),
            "elif config.wait_for_review and job_requires_review and job_effective_pipeline_mode",
            window=1,
        )
        assert 'job_effective_pipeline_mode in {"studio", "smart"}' in gate_block, (
            "voice_selection_review trigger is no longer widened for smart "
            "via job_effective_pipeline_mode (Codex 第十八轮 P1-2). "
            f"Block:\n{gate_block}"
        )

        # Verify smart branch calls evaluate_voice_review + handles
        # both outcomes (PAUSED → emit_handoff_markers; AUTO_APPROVED →
        # set_stage(APPROVED) + emit_smart_state_marker + fall through).
        # Window 290 to cover the smart block AFTER PR#3C-b3b inserted
        # the eligibility-gate prelude (~80 extra lines).
        smart_block = _find_anchor_block(
            self._source(),
            "Smart inline auto-approve path",
            window=290,
        )
        for required_call in (
            "evaluate_voice_review",
            "VoiceReviewOutcome.PAUSED",
            "emit_handoff_markers",
            "REVIEW_STATUS_APPROVED",
            "emit_smart_state_marker",
            # Codex 第十八轮 P0-2: fail-closed clone provider
            "_build_b2_not_wired_clone_provider",
            # Codex 第十八轮 P1-1: clone vendor recorded as audit, not as
            # TTS provider override
            "_sp_entry[\"clone_provider\"]",
        ):
            assert required_call in smart_block, (
                f"smart auto-approve branch missing required call "
                f"{required_call!r}; the §6.0.5 inline-not-paused-return "
                f"contract relies on it. Block:\n{smart_block}"
            )

        # Codex 第十八轮 P0-2: smart branch MUST NOT *import* the real
        # ``build_smart_clone_provider`` (the call site is what burns
        # paid API). Comments may still refer to it for context. Detect
        # the import statement specifically.
        for forbidden_import in (
            "from services.smart_wiring import build_smart_clone_provider",
            "from services.smart_wiring import (\n                        build_smart_clone_provider",
        ):
            assert forbidden_import not in smart_block, (
                "smart branch imports the real build_smart_clone_provider — "
                "PR#3C-b2-fix routes smart through the fail-closed stub "
                "_build_b2_not_wired_clone_provider to avoid burning paid "
                "clone API with stub source_audio_path + stub "
                "voice_library_quota. Replacing the stub is PR#3C-b3 "
                "territory (alongside real ffmpeg + quota snapshot)."
            )
        # Verify smart actually CALLS the fail-closed stub.
        assert "_build_b2_not_wired_clone_provider(" in smart_block, (
            "smart branch should invoke _build_b2_not_wired_clone_provider() "
            "to obtain the fail-closed CloneProvider stub. Codex 第十八轮 P0-2."
        )

        # Codex 第十八轮 P0-1: smart_state marker MUST NOT carry a
        # ``status`` key here — intermediate ``voice_review_auto_approved``
        # would clobber the editable-state predicate in
        # services.smart.state until the terminal-finalize marker lands.
        # Find the emit_smart_state_marker call in this block and verify
        # the payload doesn't set "status".
        marker_call_idx = smart_block.find("emit_smart_state_marker(")
        assert marker_call_idx >= 0, (
            "emit_smart_state_marker call not found in smart auto-approve "
            "branch — required to bridge JobRecord.smart_state.\n"
            f"Block:\n{smart_block}"
        )
        # Look at the ~6 lines after the call site; status keys would
        # appear as ``"status":`` somewhere in the dict literal.
        marker_payload = smart_block[marker_call_idx:marker_call_idx + 400]
        assert '"status"' not in marker_payload, (
            "emit_smart_state_marker in the smart voice-review-approve "
            "branch carries a top-level ``status`` key — Codex 第十八轮 "
            "P0-1 forbids any non-terminal status here because "
            "_SMART_STATE_EDITABLE_STATUSES only accepts ``completed`` / "
            "``downgraded_to_studio``. Pollution would lock the job out "
            "of editing/jianying until the terminal-finalize marker "
            "(PR#3C-b3) lands.\n"
            f"Marker payload:\n{marker_payload}"
        )

        # Codex 第十八轮 P1-1: smart MUST NOT touch _speaker_providers
        # — clone_provider_name is an audit string, not a TTS routing
        # provider. Find the explicit "deliberately do NOT touch" comment.
        assert "deliberately do NOT touch _speaker_providers" in smart_block, (
            "smart auto-approve branch is missing the explicit '_speaker_providers "
            "NOT touched' guard. Codex 第十八轮 P1-1: cloned_provider_name "
            "(e.g. 'minimax_voice_clone') is the clone-API vendor, NOT a "
            "TTS provider for downstream segment routing.\n"
            f"Block:\n{smart_block}"
        )

    def test_b2_stub_clone_provider_routes_to_preset_not_quota_pause(self):
        """Codex 第十九轮 P1: ``_build_b2_not_wired_clone_provider()``'s
        exception MUST NOT match auto_voice_review's
        ``_looks_like_quota_error`` substring heuristic (class name OR
        str(exc) containing "quota"). If it did, every normal Smart
        job with consent=True + sample>=10s would hit
        ``PAUSED/provider_quota_exhausted_mid_flight`` on the first
        clone attempt and trigger handoff to Studio — instead of the
        intended ``PRESET/provider_failure_max_retries_3`` after the
        retry budget exhausts.

        Functional integration: run the actual stub + actual
        evaluate_voice_review with realistic happy-path inputs
        (consent True, sample >= 10s, quota plenty) and assert the
        outcome is AUTO_APPROVED with a PRESET decision."""
        from pipeline.process import _build_b2_not_wired_clone_provider
        from services.smart.auto_voice_review import (
            VoiceReviewChoice,
            VoiceReviewOutcome,
            VoiceReviewSpeakerInput,
            evaluate_voice_review,
        )

        stub_provider = _build_b2_not_wired_clone_provider()
        speaker = VoiceReviewSpeakerInput(
            speaker_id="speaker_a",
            speaker_name="A",
            sample_seconds=20.0,  # well past the 10s floor
            source_audio_path=Path("/tmp/fake_sample.wav"),
        )

        result = evaluate_voice_review(
            main_speakers=[speaker],
            smart_consent={"auto_voice_clone": True},
            clone_provider=stub_provider,
            voice_library_quota_remaining=100,  # plenty
            smart_decision_id_factory=lambda: "dec_001",
        )

        # CRITICAL: outcome is AUTO_APPROVED, not PAUSED — proving the
        # stub's exception did NOT trip the quota-error heuristic.
        assert result.outcome is VoiceReviewOutcome.AUTO_APPROVED, (
            f"Expected AUTO_APPROVED outcome after retry exhaust → PRESET, "
            f"got {result.outcome!r} with pause_reason={result.pause_reason!r}. "
            f"This means the stub's exception was misidentified as a quota "
            f"error and routed to handoff instead of preset fall-through."
        )
        assert len(result.decisions) == 1
        decision = result.decisions[0]
        assert decision.choice is VoiceReviewChoice.PRESET, (
            f"Expected PRESET fall-through after retry exhaust, got "
            f"{decision.choice!r} with reason={decision.reason_code!r}."
        )
        assert decision.reason_code == "provider_failure_max_retries_3", (
            f"Expected reason ``provider_failure_max_retries_3``, got "
            f"{decision.reason_code!r}. The retry loop should have exhausted "
            f"the per-speaker retry budget (default 3 attempts)."
        )

    def test_b2_stub_clone_provider_exception_message_clean(self):
        """Defensive: pin the exception class + message contents so a
        future refactor that "improves" the wording doesn't quietly
        reintroduce 'quota' / a quota-like class name."""
        from pathlib import Path as _Path
        from pipeline.process import _build_b2_not_wired_clone_provider

        stub_provider = _build_b2_not_wired_clone_provider()
        try:
            stub_provider.clone_voice(
                speaker_id="a", speaker_name="A",
                source_audio_path=_Path("/fake.wav"),
            )
        except Exception as exc:
            raised = exc
        else:  # pragma: no cover — stub MUST raise
            raised = None

        assert raised is not None, (
            "stub MUST raise on clone_voice; never silently return."
        )
        # The auto_voice_review heuristic substring-matches lowercased
        # name + str(exc); both surfaces must avoid "quota".
        assert "quota" not in type(raised).__name__.lower(), (
            f"stub exception class name {type(raised).__name__!r} contains "
            f"'quota' — would trip _looks_like_quota_error."
        )
        assert "quota" not in str(raised).lower(), (
            f"stub exception message {str(raised)!r} contains 'quota' — "
            f"would trip _looks_like_quota_error and route smart to "
            f"handoff instead of PRESET fall-through."
        )

    def test_terminal_marker_helper_emits_completed_for_smart(self, capsys):
        """Plan §4.3 mapping + §6.0.5 + Codex 第二十轮 — PR#3C-b3a.

        Pipeline's happy-path terminal must emit
        ``{"status": "completed", "credits_policy": "capture_full"}``
        for smart jobs so:
          - editing.py / jianying gates admit the job into post-edit
            (smart_state.status must be in EDITABLE_SERVICE_MODES)
          - credits_service settle dispatcher routes through
            smart_capture_full (credits_policy must be populated)

        Codex 第二十一轮 P0: helper now takes ``service_mode`` as an
        explicit kwarg (was previously reading ``self._current_service_mode``
        which is not set on the resume-publish-only path)."""
        from pipeline.process import ProcessPipeline as _PP
        from services.smart.state import parse_smart_state_marker

        class _Stub:
            _emit_smart_terminal_completion_marker = (
                _PP._emit_smart_terminal_completion_marker
            )

        _Stub()._emit_smart_terminal_completion_marker(service_mode="smart")
        captured = capsys.readouterr().out
        markers = [
            parse_smart_state_marker(line)
            for line in captured.splitlines()
            if line.startswith("[SMART_STATE]")
        ]
        non_none = [m for m in markers if m is not None]
        assert len(non_none) == 1, (
            f"Expected exactly one [SMART_STATE] marker emit; got "
            f"{len(non_none)}.\nCaptured stdout:\n{captured}"
        )
        marker = non_none[0]
        assert marker == {
            "status": "completed",
            "credits_policy": "capture_full",
        }, f"Terminal marker payload drifted: {marker!r}"

    def test_terminal_marker_helper_noop_for_non_smart_modes(self, capsys):
        """Defensive: terminal helper must be a no-op for studio /
        express / unknown modes. Emitting a smart terminal marker on
        a non-smart job would corrupt JobRecord.smart_state for a job
        that should never carry one."""
        from pipeline.process import ProcessPipeline as _PP

        class _Stub:
            _emit_smart_terminal_completion_marker = (
                _PP._emit_smart_terminal_completion_marker
            )

        for mode in ("studio", "express", None, ""):
            capsys.readouterr()  # clear
            _Stub()._emit_smart_terminal_completion_marker(service_mode=mode)
            captured = capsys.readouterr().out
            assert "[SMART_STATE]" not in captured, (
                f"Non-smart mode {mode!r} unexpectedly emitted a terminal "
                f"smart marker:\n{captured}"
            )

    def test_terminal_marker_helper_no_implicit_self_state_read(self):
        """Codex 第二十一轮 P0: helper must NOT silently fall back to
        ``self._current_service_mode``. A fresh ProcessPipeline() that
        enters via the resume-publish-only path NEVER traverses the
        main run() assignment around line 1520. Pre-fix, calling
        the helper on such an instance raised AttributeError, breaking
        commit copy_as_new / overwrite publish.

        Pin the contract: helper called WITHOUT service_mode (or with
        None) emits nothing — never raises AttributeError, never reads
        residual instance state."""
        from pipeline.process import ProcessPipeline as _PP

        class _FreshStub:  # NO _current_service_mode attribute
            _emit_smart_terminal_completion_marker = (
                _PP._emit_smart_terminal_completion_marker
            )

        # Must not raise — and must not emit (None → non-smart noop).
        _FreshStub()._emit_smart_terminal_completion_marker(service_mode=None)

    def test_resume_path_loads_raw_service_mode_without_instance_state(self, tmp_path):
        """Codex 第二十一轮 P0: ``_run_alignment_and_publish_only`` must
        be able to surface the raw service_mode for the terminal helper
        WITHOUT touching ``self._current_service_mode`` (which the
        resume path never sets). Test ``_load_raw_service_mode_for_resume``
        directly: a fresh ProcessPipeline() can load service_mode from
        the JobRecord-shaped dict in config.job_record."""
        from types import SimpleNamespace

        from pipeline.process import ProcessPipeline

        pipe = ProcessPipeline()  # FRESH — no _current_service_mode

        # Path A: config.job_record carries a dict — return the
        # service_mode field.
        cfg_dict = SimpleNamespace(
            job_id="job_resume_x",
            job_record={"service_mode": "smart"},
        )
        assert pipe._load_raw_service_mode_for_resume(cfg_dict) == "smart"

        # Path B: config.job_record is None (no pre-load), no job_id
        # either → returns None safely.
        cfg_none = SimpleNamespace(job_id=None, job_record=None)
        assert pipe._load_raw_service_mode_for_resume(cfg_none) is None

        # Path C: config carries job_id but no job_record; JobStore
        # lookup miss returns None (best-effort, never raises).
        cfg_missing = SimpleNamespace(
            job_id="does_not_exist_anywhere",
            job_record=None,
        )
        # Result depends on JobStore env; either None or it loads
        # something. The contract is "never raises".
        result = pipe._load_raw_service_mode_for_resume(cfg_missing)
        assert result is None or isinstance(result, str)

    def test_terminal_marker_call_sites_wired_at_both_happy_path_returns(self):
        """Anchor-based: both happy-path ProcessResult returns in
        run() and the resume-publish-only path MUST call
        ``_emit_smart_terminal_completion_marker`` before returning.

        Without the wiring at both sites, smart jobs that complete
        normally (run path) OR via commit copy_as_new / overwrite
        (resume-publish-only path) would never get the editable
        terminal status, and editing/jianying/settle would all
        misbehave per the helper docstring."""
        source = self._source()
        # The helper is called immediately before each ProcessResult
        # construction. Count call-site openings — should be ≥ 2
        # (main run, resume-publish-only). After Codex 第二十一轮 P0
        # the helper takes ``service_mode=`` so we anchor on the open
        # paren, not the bare ``()``.
        call_anchor = "self._emit_smart_terminal_completion_marker("
        call_count = source.count(call_anchor)
        assert call_count >= 2, (
            f"Expected ≥2 call sites for _emit_smart_terminal_completion_marker "
            f"(main run + resume-publish-only happy-path returns); got "
            f"{call_count}. Smart jobs that finish via one path but not the "
            f"other would silently fail editable / settle invariants."
        )
        # Defensive: each call must be paired with a ``return ProcessResult(``
        # within the next ~5 lines (so the marker actually surfaces on the
        # terminal frame, not buried in some other path).
        idx = 0
        paired_calls = 0
        while True:
            idx = source.find(call_anchor, idx)
            if idx < 0:
                break
            window = source[idx : idx + 800]
            if "return ProcessResult(" in window:
                paired_calls += 1
            idx += 1
        assert paired_calls >= 2, (
            f"Helper call sites must be immediately followed by "
            f"``return ProcessResult(``; got {paired_calls} pairings."
        )

        # Codex 第二十一轮 P0: every call MUST pass an explicit
        # ``service_mode=`` keyword. Bare ``()`` invocations would read
        # the no-default arg as positional — and previously masked the
        # resume-path attribute-error bug.
        bare_call = "self._emit_smart_terminal_completion_marker()"
        assert bare_call not in source, (
            "Found bare _emit_smart_terminal_completion_marker() with no "
            "service_mode kwarg — Codex 第二十一轮 P0 requires every call "
            "site pass service_mode explicitly so the resume-publish-only "
            "path can't silently inherit stale self._current_service_mode."
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

    def test_eligibility_gate_wired_into_smart_inline_auto_approve(self):
        """PR#3C-b3b — plan §6.1 eligibility gate runs BEFORE
        ``evaluate_voice_review`` in process.py's smart inline branch.
        Without this wiring smart would burn clone retry budget on
        speakers Studio human-review would have excluded (keep_original /
        low-share / role-based) and could auto-approve jobs whose main-
        speaker count exceeds the limit.

        Anchor on the "Smart inline auto-approve path" comment and walk
        ~290 lines down to cover the entire smart branch (matches the
        existing ``test_voice_selection_review_trigger_widened_with_inline_auto_approve``
        anchor window after PR#3C-b3b inserted the eligibility prelude).
        """
        smart_block = _find_anchor_block(
            self._source(),
            "Smart inline auto-approve path",
            window=290,
        )

        # 1. Helper + gate imported.
        assert "aggregate_segment_dubbing_modes_to_speaker" in smart_block, (
            "Smart inline branch missing ``aggregate_segment_dubbing_modes_to_speaker`` "
            "import / call. Without segment→speaker aggregation the eligibility "
            "gate reads default ``dub`` for every speaker (keep_original / "
            "mute_or_background never excluded). Codex 第二十二轮 P0.\n"
            f"Block:\n{smart_block}"
        )
        assert "evaluate_eligibility" in smart_block, (
            "Smart inline branch missing ``evaluate_eligibility`` call. "
            "PR#3C-b3b plan §6.1 requires the gate run BEFORE voice "
            "auto-approve so over-limit / no-speakers / role-excluded "
            "jobs hand off to Studio.\n"
            f"Block:\n{smart_block}"
        )

        # 2. Rejection branch wired with handoff three-tuple.
        # Find the eligibility check + the if-not-approved branch within it.
        eligibility_idx = smart_block.find("evaluate_eligibility(")
        assert eligibility_idx >= 0, (
            "evaluate_eligibility call site missing in smart inline branch."
        )
        rejection_window = smart_block[eligibility_idx : eligibility_idx + 1200]
        assert "if not _smart_eligibility.approved" in rejection_window, (
            "Smart inline branch missing eligibility rejection check. "
            "Without it ``evaluate_eligibility`` is decorative — both "
            "approved + rejected paths would proceed identically.\n"
            f"Block around eligibility call:\n{rejection_window}"
        )
        assert "emit_handoff_markers" in rejection_window, (
            "Eligibility-rejection branch missing emit_handoff_markers — "
            "plan §6.5 handoff three-tuple required so process_runner / "
            "Gateway mirror see the downgraded_to_studio terminal state.\n"
            f"Block:\n{rejection_window}"
        )
        assert "downgraded_to_studio" in rejection_window, (
            "Eligibility-rejection branch must set smart_state.status to "
            "``downgraded_to_studio`` so editing/jianying gates admit the "
            "job into post-edit recovery.\n"
            f"Block:\n{rejection_window}"
        )
        assert "_smart_eligibility.reason_code" in rejection_window, (
            "Eligibility-rejection branch must propagate reason_code "
            "(e.g. ``main_speaker_count_exceeded`` / ``no_speakers_detected``) "
            "to smart_state.reason so audit logs / sidecar can record the "
            "rejection cause.\n"
            f"Block:\n{rejection_window}"
        )

        # 3. Approved branch filters main_speakers by main_speaker_ids.
        # PR#3C-b2 took vs_payload speakers verbatim, which bypassed the
        # gate's keep_original / low-share / role exclusions. PR#3C-b3b
        # filters via ``_smart_main_speaker_ids``.
        assert "_smart_main_speaker_ids" in smart_block, (
            "Smart inline branch missing ``_smart_main_speaker_ids`` "
            "filter variable. Without it ``evaluate_voice_review`` would "
            "see vs_payload speakers verbatim (the PR#3C-b2 behaviour) "
            "rather than the eligibility-vetted subset.\n"
            f"Block:\n{smart_block}"
        )
        # The list comp building _smart_main_speakers must include the
        # eligibility-gate filter clause. Window 900 chars to reach
        # through the multi-line ``if isinstance / speaker_id / in
        # _smart_main_speaker_ids`` clause.
        main_speakers_idx = smart_block.find("_smart_main_speakers = [")
        assert main_speakers_idx >= 0, (
            "Smart inline branch lost ``_smart_main_speakers = [`` list "
            "comprehension — required by evaluate_voice_review."
        )
        ms_window = smart_block[main_speakers_idx : main_speakers_idx + 900]
        assert "_smart_main_speaker_ids" in ms_window, (
            "``_smart_main_speakers`` list comprehension is NOT filtered "
            "by ``_smart_main_speaker_ids``. Codex 第二十二轮 P0: the gate's "
            "exclusions (keep_original / low-share / role) MUST propagate "
            "to the voice-review candidate set, otherwise smart burns "
            "clone retry budget on speakers Studio review would have "
            "excluded.\n"
            f"_smart_main_speakers block:\n{ms_window}"
        )

    def test_eligibility_aggregation_uses_segment_dubbing_modes(self):
        """Plan §6.1 + Codex 第二十二轮 — the aggregation MUST overlay
        per-speaker dubbing_mode onto the speaker_structure_profiles
        dict BEFORE calling evaluate_eligibility, otherwise the
        ``normalize_speaker_stats`` process.py-shape branch defaults
        every speaker to ``"dub"`` and the keep_original exclusion
        never fires.
        """
        smart_block = _find_anchor_block(
            self._source(),
            "Smart inline auto-approve path",
            window=290,
        )

        # The overlay should pull from the aggregation result. Anchor on
        # the comment that explains what's happening.
        assert "Overlay dubbing_mode onto the speaker_structure_profiles" in smart_block, (
            "Smart inline branch missing the per-speaker dubbing_mode "
            "overlay step. Without it ``evaluate_eligibility`` sees no "
            "dubbing_mode field on the profile dicts and defaults every "
            "speaker to ``dub``.\n"
            f"Block:\n{smart_block}"
        )

        # Verify the aggregation feeds the overlay.
        agg_idx = smart_block.find("aggregate_segment_dubbing_modes_to_speaker(")
        assert agg_idx >= 0, (
            "aggregate_segment_dubbing_modes_to_speaker call missing."
        )
        # Overlay block typically appears within ~600 chars of the
        # aggregation call.
        overlay_window = smart_block[agg_idx : agg_idx + 800]

        # Codex 第二十四轮 P2: pin the EXACT field name being read.
        # TranscriptResult only has ``.lines`` — ``.segments`` doesn't
        # exist and the previous wiring of ``getattr(..., "segments",
        # None) or []`` silently returned None → aggregation was {} →
        # every keep_original / mute_or_background speaker overlay
        # defaulted to "dub". Codex 第二十三轮 P1 fix swapped to
        # ``.lines``. The functional test in test_smart_business_logic.py
        # (TestAggregateWithRealTranscriptLineShape) covers the
        # runtime side; THIS guard pins the source so an anchor-only
        # refactor that swaps back to ``.segments`` (or any other
        # non-existent field) is flagged immediately, even before any
        # functional test runs against real TranscriptLine objects.
        assert 'getattr(transcript_result, "lines", None)' in overlay_window, (
            "Aggregation no longer reads from ``transcript_result.lines``. "
            "TranscriptResult exposes only ``lines: list[TranscriptLine]`` "
            "(see ``src/services/assemblyai/transcriber.py``); any other "
            "field name will return None → aggregation {} → all speakers "
            "default to ``dub`` → keep_original / mute_or_background "
            "exclusions silently disabled. Codex 第二十三轮 P1 + 第二十四轮 "
            "P2 anchor guard.\n"
            f"Overlay window:\n{overlay_window}"
        )
        assert 'getattr(transcript_result, "segments", None)' not in overlay_window, (
            "Aggregation regressed to ``.segments`` field name — "
            "TranscriptResult has no such field. See "
            "``src/services/assemblyai/transcriber.py:48``: only "
            "``lines: list[TranscriptLine]`` is defined. Codex 第二十三轮 "
            "P1 regression.\n"
            f"Overlay window:\n{overlay_window}"
        )

        assert '_enriched["dubbing_mode"]' in overlay_window, (
            "Aggregation result not written onto enriched profile dict "
            "as ``dubbing_mode`` — normalise_speaker_stats reads this "
            "field name verbatim.\n"
            f"Block:\n{overlay_window}"
        )

    # ===================================================================
    # PR#3C-b3c — translation_review smart-aware inline auto-approve
    # ===================================================================

    def test_translation_review_smart_branch_present(self):
        """PR#3C-b3c — translation_review trigger gains a smart inline
        auto-approve path symmetric to voice_selection_review. Verifies:
          - gate uses ``job_effective_pipeline_mode == "smart"``
            (Codex 第十八轮 P1-2 — effective, not raw)
          - evaluate_translation_review is called
          - rejection branch fires emit_handoff_markers with
            TRANSLATION_REVIEW_STAGE and downgraded_to_studio
          - approved branch sets REVIEW_STATUS_APPROVED and emits
            intermediate smart_state marker (no top-level status key
            per Codex 第十八轮 P0-1)
          - no paused-return on approved (fall through to alignment)
        """
        source = self._source()

        # Anchor: comment marker on the smart inline translation branch.
        anchor = "Smart inline auto-translation-review path"
        idx = source.find(anchor)
        assert idx >= 0, (
            f"PR#3C-b3c anchor {anchor!r} missing — smart translation "
            f"review branch not added to process.py."
        )
        block = source[idx : idx + 16000]

        # Imports — co-located so a future refactor doesn't accidentally
        # move them outside the smart branch (which would make the
        # legacy path import them too, no functional harm but signals
        # confusion about who owns the call).
        for required in (
            "from services.smart.auto_translation_review import",
            "evaluate_translation_review",
            "from services.smart.handoff import emit_handoff_markers",
            "from services.smart.state import emit_smart_state_marker",
            "check_glossary_preservation",
        ):
            assert required in block, (
                f"smart translation-review branch missing required "
                f"reference {required!r}. Block (first 1200 chars):\n"
                f"{block[:1200]}"
            )

        # Gate uses effective mode (P1-2).
        # We anchor on the if-line BEFORE the comment block.
        gate_window = source[max(0, idx - 600) : idx + 100]
        assert 'job_effective_pipeline_mode == "smart"' in gate_window, (
            "Smart translation-review gate must use "
            "``job_effective_pipeline_mode == \"smart\"`` (Codex 第十八轮 "
            "P1-2) so downgraded smart jobs don't re-enter the smart "
            "branch on resume.\n"
            f"Window:\n{gate_window}"
        )

    def test_translation_review_smart_rejection_emits_handoff(self):
        """Rejection branch — must fire emit_handoff_markers with
        TRANSLATION_REVIEW_STAGE + downgraded_to_studio + reason_code,
        then paused-return."""
        source = self._source()
        idx = source.find("Smart inline auto-translation-review path")
        assert idx >= 0
        block = source[idx : idx + 16000]

        # Find the rejection branch (if not auto_approved).
        rejection_anchor = "if not _smart_translation_decision.auto_approved:"
        rej_idx = block.find(rejection_anchor)
        assert rej_idx >= 0, (
            "Smart translation-review branch missing the "
            "``if not auto_approved:`` rejection check.\n"
            f"Block:\n{block[:2000]}"
        )
        rejection_window = block[rej_idx : rej_idx + 2500]

        for required in (
            "emit_handoff_markers(",
            "TRANSLATION_REVIEW_STAGE",
            "REVIEW_STATUS_PENDING",
            "downgraded_to_studio",
            "_smart_translation_decision.reason_code",
            "self._build_paused_result(",
        ):
            assert required in rejection_window, (
                f"Translation-review rejection branch missing required "
                f"call/reference {required!r}.\n"
                f"Rejection window:\n{rejection_window}"
            )

    def test_translation_review_smart_approval_falls_through(self):
        """Approval branch — must set_stage(APPROVED), emit
        intermediate smart_state marker (NO ``status`` key per Codex
        第十八轮 P0-1), and NOT paused-return."""
        source = self._source()
        idx = source.find("Smart inline auto-translation-review path")
        assert idx >= 0
        block = source[idx : idx + 16000]

        # Approval branch lives AFTER the `if not auto_approved:` return.
        approval_anchor = "Auto-approved: set_stage(APPROVED) + intermediate"
        appr_idx = block.find(approval_anchor)
        assert appr_idx >= 0, (
            "Smart translation-review branch missing the approval "
            "anchor comment. Pin so future refactors keep the "
            "set_stage(APPROVED) + intermediate-marker contract.\n"
            f"Block:\n{block[:2000]}"
        )
        approval_window = block[appr_idx : appr_idx + 1500]

        # set_stage(APPROVED).
        assert "REVIEW_STATUS_APPROVED" in approval_window, (
            "Approval branch must set_stage with REVIEW_STATUS_APPROVED.\n"
            f"Window:\n{approval_window}"
        )
        assert "TRANSLATION_REVIEW_STAGE" in approval_window
        # Intermediate marker — no "status" key.
        marker_idx = approval_window.find("emit_smart_state_marker(")
        assert marker_idx >= 0, (
            "Approval branch must call emit_smart_state_marker for "
            "the auto_translation_review audit dict.\n"
            f"Window:\n{approval_window}"
        )
        marker_payload = approval_window[marker_idx : marker_idx + 600]
        assert '"status"' not in marker_payload, (
            "emit_smart_state_marker in the smart translation-review "
            "approve branch carries a top-level ``status`` key — "
            "Codex 第十八轮 P0-1 forbids any non-terminal status here "
            "because EDITABLE_SMART_STATE_STATUSES only accepts "
            "``completed`` / ``downgraded_to_studio``. Pollution "
            "would lock the job out of editing/jianying until the "
            "terminal-finalize marker lands.\n"
            f"Marker payload:\n{marker_payload}"
        )

        # NO paused-return on approval — pipeline must fall through.
        # Search for ``return self._build_paused_result`` in the
        # approval window; if present, smart would block the same way
        # legacy Studio does, defeating the auto-approve story.
        assert "return self._build_paused_result(" not in approval_window, (
            "Approval branch has a paused-return — smart inline "
            "auto-approve MUST fall through to alignment, not pause. "
            "If you need to pause here for some reason, document why "
            "in a docstring and pin a separate anchor test for that "
            "shape; don't silently lose the auto-approve.\n"
            f"Window:\n{approval_window}"
        )

    def test_translation_review_legacy_studio_path_still_paused(self):
        """Regression — the legacy Studio path under the same review
        trigger must still set_stage(PENDING) + paused-return. This
        is the non-smart else-branch right after the smart branch."""
        source = self._source()
        idx = source.find("Smart inline auto-translation-review path")
        assert idx >= 0

        # Walk forward from the smart block to find the legacy
        # ``else:`` branch (anchored on the comment marker).
        legacy_anchor = "Legacy Studio path: pending + paused-return"
        legacy_idx = source.find(legacy_anchor, idx)
        assert legacy_idx >= 0, (
            f"Legacy Studio translation-review path anchor missing "
            f"after the smart branch — the else: branch should be "
            f"preserved verbatim from pre-b3c behaviour."
        )
        legacy_window = source[legacy_idx : legacy_idx + 1500]

        for required in (
            "REVIEW_STATUS_PENDING",
            "TRANSLATION_REVIEW_STAGE",
            "等待在 Web UI 确认翻译稿",
            "self._build_paused_result(",
        ):
            assert required in legacy_window, (
                f"Legacy Studio translation-review path missing "
                f"required {required!r}.\n"
                f"Window:\n{legacy_window}"
            )

    # ===================================================================
    # PR#3C-b3c-fix (Codex 第二十五轮) — close 2 P1 fail-opens
    # ===================================================================

    def test_translation_review_passes_compliance_block_to_gate(self):
        """Codex 第二十五轮 P1-1: compliance_block was previously not
        plumbed into evaluate_translation_review. Pin that:
          - the call site includes ``compliance_block=`` kwarg
          - the value is derived from ``content_compliance_payload``
            with ``status == "blocked"`` semantics
          - the gate gate is INVOKED with this kwarg (not just
            computed)
        """
        source = self._source()
        idx = source.find("Smart inline auto-translation-review path")
        assert idx >= 0
        block = source[idx : idx + 16000]

        # _smart_compliance_block must be derived from content_compliance_payload.
        assert "_smart_compliance_block" in block, (
            "Smart translation-review branch missing the "
            "``_smart_compliance_block`` derivation — Codex 第二十五轮 "
            "P1-1 fail-open. Without it, a blocked / needs_manual_review "
            "compliance result would still auto-approve translation.\n"
            f"Block first 3000 chars:\n{block[:3000]}"
        )

        # Derivation must reference content_compliance_payload + "blocked".
        deriv_idx = block.find("_smart_compliance_block =")
        assert deriv_idx >= 0, (
            "Could not locate ``_smart_compliance_block =`` assignment "
            "in smart translation-review branch."
        )
        deriv_window = block[deriv_idx : deriv_idx + 600]
        assert "content_compliance_payload" in deriv_window, (
            "_smart_compliance_block must derive from "
            "content_compliance_payload, not a hardcoded constant.\n"
            f"Window:\n{deriv_window}"
        )
        assert '"blocked"' in deriv_window, (
            "_smart_compliance_block derivation must check ``status == "
            "\"blocked\"`` (see ``src/services/content_compliance.py:131`` "
            "where ContentComplianceResult.blocked is defined that way).\n"
            f"Window:\n{deriv_window}"
        )

        # evaluate_translation_review call site must pass the kwarg.
        eval_idx = block.find("evaluate_translation_review(")
        assert eval_idx >= 0, "evaluate_translation_review call missing."
        eval_window = block[eval_idx : eval_idx + 800]
        assert "compliance_block=_smart_compliance_block" in eval_window, (
            "evaluate_translation_review call site missing "
            "``compliance_block=_smart_compliance_block`` kwarg — the "
            "Codex 第二十五轮 P1-1 fix didn't reach the actual call.\n"
            f"Call window:\n{eval_window}"
        )

    def test_translation_review_glossary_failure_short_circuits_to_handoff(self):
        """Codex 第二十五轮 P1-2: glossary helper exception when
        ``_review_glossary`` is configured MUST route to handoff with
        ``reason_code="glossary_check_error"`` + ``failed_check=
        "glossary_preservation"`` instead of vacuous-pass.

        Pin the source-level structure:
          - ``_smart_glossary_check_failed`` boolean state variable
          - the helper call is gated on ``if _review_glossary:`` (not
            always called, so empty-glossary stays vacuous-pass)
          - failed → synthesize ``TranslationReviewDecision(
            auto_approved=False, reason_code="glossary_check_error",
            failed_check="glossary_preservation", ...)``
          - the synthetic decision flows into the existing handoff
            branch (not a separate code path)
        """
        source = self._source()
        idx = source.find("Smart inline auto-translation-review path")
        assert idx >= 0
        block = source[idx : idx + 16000]

        assert "_smart_glossary_check_failed" in block, (
            "Smart translation-review branch missing "
            "``_smart_glossary_check_failed`` state — Codex 第二十五轮 "
            "P1-2 fail-open. Without it, a broken glossary checker "
            "produces ``total=0`` which the gate vacuous-passes as "
            "``no glossary configured``.\n"
            f"Block first 3000 chars:\n{block[:3000]}"
        )

        # The glossary check call must be gated on ``if _review_glossary:``.
        # The previous fail-open passed `_review_glossary or None` always,
        # treating empty glossary the same as failed glossary.
        # NB: we check substring presence, not exact line layout.
        assert "if _review_glossary:" in block, (
            "Glossary check no longer gated on ``if _review_glossary:`` "
            "— PR#3C-b3c-fix changes the call-site to only invoke "
            "check_glossary_preservation when the glossary is non-empty, "
            "so an empty glossary stays in the vacuous-pass path AND a "
            "broken helper on a configured glossary doesn't silently "
            "fall through to total=0.\n"
            f"Block first 3000 chars:\n{block[:3000]}"
        )

        # The synthesized handoff decision must have the right shape.
        synth_idx = block.find('reason_code="glossary_check_error"')
        assert synth_idx >= 0, (
            "Could not locate ``reason_code=\"glossary_check_error\"`` "
            "in the smart branch — Codex 第二十五轮 P1-2 fix incomplete.\n"
            f"Block:\n{block[:4000]}"
        )
        synth_window = block[max(0, synth_idx - 400) : synth_idx + 600]
        assert "TranslationReviewDecision(" in synth_window, (
            "glossary_check_error reason must be inside a synthesized "
            "TranslationReviewDecision dataclass so the existing handoff "
            "branch picks it up.\n"
            f"Window:\n{synth_window}"
        )
        assert 'failed_check="glossary_preservation"' in synth_window, (
            "glossary failure must report ``failed_check="
            "\"glossary_preservation\"`` so audit logs / sidecar see "
            "the same label other glossary failures carry.\n"
            f"Window:\n{synth_window}"
        )
        assert "auto_approved=False" in synth_window, (
            "Synthesized glossary_check_error decision must have "
            "``auto_approved=False`` — this is the whole point of "
            "the fail-CLOSED fix.\n"
            f"Window:\n{synth_window}"
        )
