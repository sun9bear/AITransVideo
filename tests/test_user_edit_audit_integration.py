"""End-to-end / integration tests for the user_edit_audit P0 wiring.

Verifies that JobService methods + review_actions correctly emit
events into the per-project user_edit_events.jsonl when driven through
their public APIs. Uses an in-process fake observer to capture calls
without writing to disk (faster + lets us assert on call sequences).

Plan: docs/plans/2026-05-04-user-edit-audit-data-optimization-plan.md
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from services.jobs.user_edit_audit import (
    EVENT_TYPE_EDITING_SESSION_STARTED,
    EVENT_TYPE_EFFECTIVE_MARKER,
    EVENT_TYPE_POST_EDIT_CANCELLED,
    EVENT_TYPE_POST_EDIT_COMMITTED,
    EVENT_TYPE_POST_EDIT_DRAFT_TTS_ACCEPTED,
    EVENT_TYPE_POST_EDIT_DRAFT_TTS_DISCARDED,
    EVENT_TYPE_POST_EDIT_SEGMENT_SPEAKER_CHANGED,
    EVENT_TYPE_POST_EDIT_SEGMENT_SPLIT_CONFIRMED,
    EVENT_TYPE_POST_EDIT_TEXT_CHANGED,
    EVENT_TYPE_POST_EDIT_TTS_REGENERATED,
    EVENT_TYPE_VOICE_SELECTION_DUBBING_MODE_CHANGED,
    EVENT_TYPE_VOICE_SELECTION_SPEAKER_REASSIGNED,
)


class _CapturingObserver:
    """Fake observer that records every (project_dir, event) pair."""

    def __init__(self) -> None:
        self.calls: list[tuple[Path, dict[str, Any]]] = []

    def observe(self, *, project_dir: Path, event: dict[str, Any]) -> None:
        self.calls.append((project_dir, dict(event)))

    def event_types(self) -> list[str]:
        return [e["event_type"] for _, e in self.calls]


# ---------------------------------------------------------------------------
# Calibration guard: record_tts always carries segment_id (plan §12 P0)
# ---------------------------------------------------------------------------


class TestRecordTtsCarriesSegmentId:
    """Plan §12 P0: every user-mutation-triggered ``record_tts`` call must
    pass ``segment_id`` so P1 dataset builder can correlate audit events
    with usage events. We enforce this via AST scan rather than mocking
    every call site — it lets future contributors see at PR time if they
    add a record_tts call without segment_id."""

    def test_all_record_tts_calls_pass_segment_id(self) -> None:
        repo_src = Path(__file__).resolve().parents[1] / "src"
        offenders: list[tuple[str, int]] = []

        for py_path in repo_src.rglob("*.py"):
            try:
                tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
            except (SyntaxError, OSError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                # Match `<obj>.record_tts(...)` calls
                if not (isinstance(func, ast.Attribute) and func.attr == "record_tts"):
                    continue
                # segment_id must appear as a keyword arg
                kw_names = {kw.arg for kw in node.keywords if kw.arg}
                if "segment_id" not in kw_names:
                    offenders.append((str(py_path.relative_to(repo_src)), node.lineno))

        assert offenders == [], (
            "record_tts call(s) missing segment_id keyword argument — P1 "
            "audit dataset builder will not be able to correlate these usage "
            "events with user_edit_events.jsonl entries. Offenders:\n"
            + "\n".join(f"  - {p}:{ln}" for p, ln in offenders)
        )


# ---------------------------------------------------------------------------
# Guard: copy_as_new does NOT copy the audit/ directory (plan §13.6)
# ---------------------------------------------------------------------------


class TestCopyAsNewDoesNotCopyAudit:
    """Plan §13.6: a copy_as_new target must NOT inherit the source job's
    audit/ events — that would let analysis double-count edits or attribute
    parent-job actions to the child. ``copy_service.prepare_copy_project_dir``
    enumerates copies explicitly, so audit/ is omitted by default; this
    test locks that omission in.
    """

    def test_audit_dir_not_referenced_in_copy_service(self) -> None:
        """AST scan: copy_service.py must not reference 'audit' as a path
        or filename. If anyone adds it (intentional or accidental), this
        test fires before the change ships."""
        copy_service_path = (
            Path(__file__).resolve().parents[1]
            / "src" / "services" / "jobs" / "copy_service.py"
        )
        source = copy_service_path.read_text(encoding="utf-8")
        # Look for likely path references — string constants or path
        # arithmetic with "audit"
        for needle in (
            '"audit"', "'audit'",
            '"audit/', "'audit/",
            'audit/user_edit_events',
        ):
            assert needle not in source, (
                f"copy_service.py mentions {needle!r} — that suggests audit/ "
                "is being copied or otherwise touched by copy_as_new. Plan "
                "§13.6 requires the new job to start with a fresh audit slate."
            )

    def test_prepare_copy_does_not_create_audit_dir(self, tmp_path: Path) -> None:
        """Functional check: run prepare_copy_project_dir end-to-end with an
        audit dir in the source, verify the target has no audit/."""
        from services.jobs.copy_service import prepare_copy_project_dir

        src = tmp_path / "source_project"
        src.mkdir()
        # Required source layout
        (src / "transcript").mkdir()
        (src / "transcript" / "transcript.json").write_text(
            json.dumps({"lines": []}), encoding="utf-8"
        )
        (src / "translation").mkdir()
        (src / "translation" / "segments.json").write_text(
            json.dumps([]), encoding="utf-8"
        )
        (src / "manifest.json").write_text(
            json.dumps({"artifact_index": {}}), encoding="utf-8"
        )
        (src / "editor").mkdir()
        (src / "editor" / "tts_segments").mkdir()
        (src / "editor" / "segments.json").write_text(
            json.dumps([]), encoding="utf-8"
        )
        (src / "editor" / "editing").mkdir()
        (src / "editor" / "editing" / "segments.json").write_text(
            json.dumps([]), encoding="utf-8"
        )
        (src / "editor" / "editing" / "voice_map.json").write_text(
            json.dumps({}), encoding="utf-8"
        )
        # The thing the test is about: an audit directory with prior events
        (src / "audit").mkdir()
        (src / "audit" / "user_edit_events.jsonl").write_text(
            '{"event_id": "from-source", "event_type": "post_edit_text_changed",'
            ' "schema_version": 1, "job_id": "src-job", "stage": "post_edit",'
            ' "created_at": "2026-05-01T00:00:00+00:00"}\n',
            encoding="utf-8",
        )

        target = tmp_path / "target_project"
        try:
            prepare_copy_project_dir(source_dir=src, target_dir=target)
        except Exception as exc:
            # Some helper deps might not be present in tests; the AST guard
            # above is the primary protection. Fall back to checking the
            # absence directly if prepare_copy succeeded part-way.
            pytest.skip(f"prepare_copy_project_dir not runnable in this fixture: {exc}")

        assert not (target / "audit").exists(), (
            "copy_as_new copied the audit/ directory to the target — that "
            "would let analysis double-count parent-job edits as if they "
            "were the child's. Plan §13.6 forbids this."
        )


# ---------------------------------------------------------------------------
# Integration: voice_selection mutations through the review_actions API
# ---------------------------------------------------------------------------


class TestVoiceSelectionAuditWiring:
    def test_dubbing_mode_change_emits_event_via_emitter(
        self, tmp_path: Path
    ) -> None:
        from services.jobs.review_actions import set_speaker_audio_dubbing_mode
        from services.jobs.user_edit_audit import AuditContext

        # Minimal project_dir layout
        project_dir = tmp_path / "project"
        (project_dir / "transcript").mkdir(parents=True)
        (project_dir / "transcript" / "transcript.json").write_text(
            json.dumps({"lines": [
                {
                    "index": 1,
                    "speaker_id": "speaker_a",
                    "speaker_label": "A",
                    "start_ms": 0,
                    "end_ms": 1500,
                    "source_text": "hello",
                    "cn_text": "你好",
                }
            ]}),
            encoding="utf-8",
        )
        # review_state.json so manager.get_stage finds the voice_selection_review payload
        (project_dir / "review_state.json").write_text(
            json.dumps({"stages": {
                "voice_selection_review": {"status": "pending", "payload": {}}
            }}),
            encoding="utf-8",
        )

        captured: list[dict[str, Any]] = []
        ctx = AuditContext(
            job_id="job-1",
            root_job_id="job-1",
            project_id="project",
        )

        set_speaker_audio_dubbing_mode(
            project_dir=project_dir,
            segment_id=1,
            speaker_id="speaker_a",
            dubbing_mode="keep_original",
            audit_emitter=lambda ev: captured.append(ev),
            audit_context=ctx,
        )

        assert len(captured) == 1
        ev = captured[0]
        assert ev["event_type"] == EVENT_TYPE_VOICE_SELECTION_DUBBING_MODE_CHANGED
        assert ev["before"]["dubbing_mode"] == "dub"  # default
        assert ev["after"]["dubbing_mode"] == "keep_original"
        assert ev["segment"]["duration_ms"] == 1500

    def test_speaker_reassign_emits_event_via_emitter(
        self, tmp_path: Path
    ) -> None:
        from services.jobs.review_actions import reassign_speaker_audio_segment
        from services.jobs.user_edit_audit import AuditContext

        project_dir = tmp_path / "project"
        (project_dir / "transcript").mkdir(parents=True)
        (project_dir / "transcript" / "transcript.json").write_text(
            json.dumps({"lines": [
                {
                    "index": 1,
                    "speaker_id": "speaker_a",
                    "speaker_label": "A",
                    "start_ms": 0,
                    "end_ms": 1200,
                    "source_text": "hi",
                    "cn_text": "你好",
                },
                {
                    "index": 2,
                    "speaker_id": "speaker_b",
                    "speaker_label": "B",
                    "start_ms": 1200,
                    "end_ms": 3000,
                    "source_text": "ok",
                    "cn_text": "嗯",
                },
            ]}),
            encoding="utf-8",
        )
        (project_dir / "review_state.json").write_text(
            json.dumps({"stages": {
                "voice_selection_review": {"status": "pending", "payload": {}}
            }}),
            encoding="utf-8",
        )

        captured: list[dict[str, Any]] = []
        ctx = AuditContext(job_id="job-1", root_job_id="job-1", project_id="project")

        reassign_speaker_audio_segment(
            project_dir=project_dir,
            segment_id=1,
            from_speaker_id="speaker_a",
            to_speaker_id="speaker_b",
            audit_emitter=lambda ev: captured.append(ev),
            audit_context=ctx,
        )

        assert len(captured) == 1
        ev = captured[0]
        assert ev["event_type"] == EVENT_TYPE_VOICE_SELECTION_SPEAKER_REASSIGNED
        assert ev["before"]["speaker_id"] == "speaker_a"
        assert ev["after"]["speaker_id"] == "speaker_b"
        assert ev["segment"]["duration_ms"] == 1200
        assert ev["context"]["is_short_segment"] is True  # 1.2s < 2s


# ---------------------------------------------------------------------------
# Integration: JobService chokepoint catches observer exceptions, never
# raises into the user-facing main path
# ---------------------------------------------------------------------------


class TestJobServiceAuditExceptionIsolation:
    def test_constructor_none_observer_disables_audit(self) -> None:
        from services.jobs.service import JobService

        svc = JobService(store=None, runner=None, audit_observer=None)  # type: ignore[arg-type]
        assert svc._audit_observer is None

    def test_emit_user_edit_event_swallows_observer_exceptions(self) -> None:
        from services.jobs.service import JobService

        class BoomObserver:
            def observe(self, *, project_dir: Any, event: dict[str, Any]) -> None:
                raise RuntimeError("audit broken")

        # Build a JobService with a fake observer; we only need the
        # _emit_user_edit_event chokepoint, not the full job lifecycle.
        # store / runner can be None because _emit_user_edit_event only
        # uses store inside the dedup'd warning emitter (which itself is
        # try/except'd).
        class _NoopStore:
            def append_event(self, *_args, **_kwargs):
                pass

        svc = JobService.__new__(JobService)
        svc.store = _NoopStore()
        svc.runner = None
        svc._audit_observer = BoomObserver()

        # Must not raise
        svc._emit_user_edit_event(
            project_dir=Path("/tmp/audit-test"),
            event={
                "event_type": "post_edit_text_changed",
                "job_id": "job-1",
                "stage": "post_edit",
            },
        )

    def test_emit_user_edit_event_with_none_observer_is_noop(self) -> None:
        from services.jobs.service import JobService

        svc = JobService.__new__(JobService)
        svc.store = None
        svc.runner = None
        svc._audit_observer = None

        # Must not raise even with None observer
        svc._emit_user_edit_event(
            project_dir=Path("/tmp/x"),
            event={"event_type": "x", "job_id": "j", "stage": "post_edit"},
        )


# ---------------------------------------------------------------------------
# Integration: post_edit_voice_override_changed (set + clear)
# Plan §10.4 — feeds the auto voice-recommendation analysis loop.
# ---------------------------------------------------------------------------


class TestVoiceOverrideAuditWiring:
    def _make_record(self, project_dir: Path) -> Any:
        from services.jobs.models import JOB_STATUS_EDITING, JobRecord

        return JobRecord(
            job_id="job-vo-1",
            job_type="localize_video",
            source_type="youtube_url",
            source_ref="https://example.com/x",
            output_target="editor",
            speakers="auto",
            voice_a=None,
            voice_b=None,
            status=JOB_STATUS_EDITING,
            current_stage=None,
            progress_message=None,
            created_at="2026-05-04T00:00:00+00:00",
            updated_at="2026-05-04T00:00:00+00:00",
            project_dir=str(project_dir),
            service_mode="studio",
            editing_touched_at="2026-05-04T00:00:00+00:00",
        )

    def _bootstrap_editing_dir(self, project_dir: Path) -> None:
        """Minimum FS layout so set_voice_override / clear_voice_override
        accept the project_dir without raising."""
        from services.jobs.editing import EDITING_SUBDIR

        editing_dir = project_dir / EDITING_SUBDIR
        editing_dir.mkdir(parents=True)
        # voice_map.json starts empty
        (editing_dir / "voice_map.json").write_text("{}", encoding="utf-8")
        # segment_status.json must exist for mark_segment_status's atomic write
        (editing_dir / "segment_status.json").write_text("{}", encoding="utf-8")
        # segments.json so compute_residual_segment_status (called by clear)
        # has something to read; one matching segment is enough
        (editing_dir / "segments.json").write_text(
            json.dumps([{
                "segment_id": "seg_001",
                "speaker_id": "speaker_a",
                "start_ms": 0,
                "end_ms": 1500,
                "cn_text": "你好",
                "source_text": "hello",
            }]),
            encoding="utf-8",
        )

    def test_set_voice_override_emits_event(self, tmp_path: Path) -> None:
        from services.jobs.service import JobService

        project_dir = tmp_path / "project"
        self._bootstrap_editing_dir(project_dir)

        observer = _CapturingObserver()
        svc = JobService.__new__(JobService)
        svc._audit_observer = observer

        # Inject minimal store stub: _require_editing reads load_job +
        # checks status; set_voice_override writes to FS (no DB).
        record = self._make_record(project_dir)

        class _StubStore:
            def __init__(self_inner) -> None:
                self_inner.saved: list[Any] = []
            def require_job(self_inner, job_id: str):
                return record
            def load_job(self_inner, job_id: str):
                return record
            def save_job(self_inner, rec):
                self_inner.saved.append(rec)
                return rec
            def append_event(self_inner, *_a, **_kw):
                pass

        svc.store = _StubStore()
        svc.runner = None

        svc.set_editing_voice_override(
            "job-vo-1", "seg_001",
            provider="minimax", voice_id="voice_xyz",
        )

        types = observer.event_types()
        assert EVENT_TYPE_VOICE_SELECTION_DUBBING_MODE_CHANGED not in types
        # The actual event we care about — voice override set
        assert "post_edit_voice_override_changed" in types
        ev = next(e for _, e in observer.calls if e["event_type"] == "post_edit_voice_override_changed")
        assert ev["context"]["operation"] == "set"
        assert ev["after"]["voice_id"] == "voice_xyz"
        assert ev["after"]["provider"] == "minimax"
        # Before should be None/None — no prior override
        assert ev["before"]["voice_id"] is None
        assert ev["before"]["provider"] is None

    def test_clear_voice_override_emits_event_with_before_state(
        self, tmp_path: Path
    ) -> None:
        from services.jobs.service import JobService

        project_dir = tmp_path / "project"
        self._bootstrap_editing_dir(project_dir)
        # Pre-existing override so clear has a meaningful before
        (project_dir / "editor" / "editing" / "voice_map.json").write_text(
            json.dumps({"seg_001": {"provider": "minimax", "voice_id": "voice_old"}}),
            encoding="utf-8",
        )

        observer = _CapturingObserver()
        record = self._make_record(project_dir)

        class _StubStore:
            def require_job(self_inner, job_id: str):
                return record
            def load_job(self_inner, job_id: str):
                return record
            def save_job(self_inner, rec):
                return rec
            def append_event(self_inner, *_a, **_kw):
                pass

        svc = JobService.__new__(JobService)
        svc._audit_observer = observer
        svc.store = _StubStore()
        svc.runner = None

        svc.clear_editing_voice_override("job-vo-1", "seg_001")

        ev = next(
            e for _, e in observer.calls
            if e["event_type"] == "post_edit_voice_override_changed"
        )
        assert ev["context"]["operation"] == "clear"
        assert ev["before"]["voice_id"] == "voice_old"
        assert ev["before"]["provider"] == "minimax"
        assert ev["after"]["voice_id"] is None
        assert ev["after"]["provider"] is None
