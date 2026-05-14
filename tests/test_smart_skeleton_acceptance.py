"""Smart MVP P2 skeleton — three acceptance tests.

Locks the dispatcher / state-machine wiring contract for F1-F4 so the
stubs cannot silently regress before the real implementations land.
Codex 第七轮 review explicitly requested this 3-line acceptance suite
(see plan §15 末段 第七轮 codex review log).

Mainline 1: ``test_smart_handoff_marker_pipeline_emits_runner_parses``
  Pipeline emits ``[SMART_STATE]`` + ``[WEB_REVIEW]`` markers via
  ``emit_handoff_markers``; verifies the runner-side parser recognises
  both shapes and the handoff state would land in JobRecord.smart_state
  + drive job.status to ``waiting_for_review`` (not ``succeeded``).

Mainline 2: ``test_continue_after_handoff_does_not_re_enter_smart_gate``
  A smart job whose ``smart_state.status == "downgraded_to_studio"``
  (set during a previous handoff) returns effective_pipeline_mode
  ``"studio"`` from ``derive_effective_pipeline_mode`` — so on /continue
  the pipeline traverses the Studio human-review control flow rather
  than re-triggering auto-review and looping the same failure.

Mainline 3: ``test_settle_dispatch_routes_smart_credits_policy``
  ``settle_job_credit_ledger`` invoked on a job whose
  ``smart_state.credits_policy == "capture_actual_cost_capped_at_studio_price"``
  takes the smart dispatcher branch (via stubs ``shadow_release`` +
  ``refund_captured_voice_clone`` + ``partial_capture_actual_cost``)
  rather than the legacy ``terminal_status in {failed, cancelled}``
  full-release branch.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# Repo path setup mirrors tests/conftest.py — credits_service imports
# `from models import` which expects gateway/ on sys.path.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
_GATEWAY = _PROJECT_ROOT / "gateway"
for _p in (str(_SRC), str(_GATEWAY)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Same DB stub as test_credits_service.py — credits_service imports
# `database` at module load, which would otherwise need a real PG.
if "database" not in sys.modules:
    _fake_database = types.ModuleType("database")
    _fake_database.get_db = MagicMock()
    _fake_database.engine = MagicMock()
    _fake_database.async_session = MagicMock()
    sys.modules["database"] = _fake_database

from services.smart.handoff import emit_handoff_markers
from services.smart.state import (
    SMART_STATE_MARKER_PREFIX,
    derive_effective_pipeline_mode,
    emit_smart_state_marker,
    parse_smart_state_marker,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ===================================================================
# Mainline 1: handoff marker triple — pipeline emits, runner parses,
# job lands in waiting_for_review (not silently succeeded)
# ===================================================================


class TestSmartHandoffMarkerPipeline:
    """F2 + F1 — handoff helper emits the right marker triple and
    the runner-side parser would pick them up correctly.

    We can't spin up a real subprocess + ProcessJobRunner in a unit
    test, but we can:
      1. Capture pipeline stdout when emit_handoff_markers runs.
      2. Verify the captured output contains both markers in parseable shape.
      3. Verify the runner-side parser (parse_smart_state_marker) and
         the existing _parse_web_review_marker (process_runner.py:784)
         recognise the captured bytes.
    """

    def test_smart_handoff_marker_pipeline_emits_runner_parses(self, capsys):
        # Stand-in for the pipeline's review_state_manager — record set_stage
        # calls so we can assert the contract.
        review_state_calls = []
        review_state_manager = SimpleNamespace(
            set_stage=lambda *args, **kwargs: review_state_calls.append((args, kwargs))
        )
        pending_status = "PENDING_SENTINEL"  # opaque — handoff helper just forwards

        # Stand-in for pipeline._build_web_review_marker — mirrors the real
        # process.py method shape (see process.py:3645).
        def fake_web_review_marker_builder(*, stage, project_dir, message):
            payload = {
                "stage": stage,
                "tab": "voice_review",
                "project_dir": str(project_dir),
                "message": message,
            }
            return f"[WEB_REVIEW] {json.dumps(payload, ensure_ascii=False)}"

        emit_handoff_markers(
            review_state_manager=review_state_manager,
            review_stage="voice_selection_review",
            review_payload={"speakers": [{"speaker_id": "speaker_a"}]},
            review_pending_status=pending_status,
            smart_state_update={
                "status": "downgraded_to_studio",
                "reason": "translation_auto_approve_failed",
                "handoff_stage": "voice_selection_review",
                "credits_policy": "refund_full",
            },
            project_dir=Path("/projects/abc"),
            user_message="智能版自动流程已停止,请人工接管",
            web_review_marker_builder=fake_web_review_marker_builder,
        )

        # 1. set_stage was called with PENDING — the existing review-state
        # contract Studio relies on. Without this the runner never sees
        # the review state and /continue can't pick up where smart left off.
        assert len(review_state_calls) == 1
        args, kwargs = review_state_calls[0]
        assert args == ("voice_selection_review",)
        assert kwargs["status"] == pending_status
        assert kwargs["activate"] is True
        assert kwargs["payload"]["speakers"][0]["speaker_id"] == "speaker_a"

        # 2 + 3. Captured stdout contains both [SMART_STATE] and [WEB_REVIEW]
        # markers in parseable form.
        captured = capsys.readouterr().out
        smart_marker_lines = [
            line for line in captured.splitlines()
            if line.startswith(SMART_STATE_MARKER_PREFIX)
        ]
        web_marker_lines = [
            line for line in captured.splitlines()
            if line.startswith("[WEB_REVIEW]")
        ]
        assert len(smart_marker_lines) == 1, (
            f"Expected exactly one [SMART_STATE] marker; got {smart_marker_lines}"
        )
        assert len(web_marker_lines) == 1, (
            f"Expected exactly one [WEB_REVIEW] marker; got {web_marker_lines}"
        )

        # The smart marker's parsed payload carries the handoff state that
        # the runner will write into JobRecord.smart_state.
        parsed_smart = parse_smart_state_marker(smart_marker_lines[0])
        assert parsed_smart is not None
        assert parsed_smart["status"] == "downgraded_to_studio"
        assert parsed_smart["reason"] == "translation_auto_approve_failed"
        assert parsed_smart["credits_policy"] == "refund_full"

        # The web review marker drives job.status → waiting_for_review on
        # the runner side. Re-import the runner-side parser to guarantee
        # the bytes round-trip through the same regex shape Studio uses.
        from services.jobs.process_runner import _parse_web_review_marker

        parsed_web = _parse_web_review_marker(web_marker_lines[0])
        assert parsed_web is not None
        assert parsed_web["stage"] == "voice_selection_review"
        # Path serialisation is platform-dependent (Path("/x").str() →
        # "\x" on Windows, "/x" on POSIX). Compare via Path round-trip
        # so the test runs on both.
        assert Path(parsed_web["project_dir"]) == Path("/projects/abc")
        assert "message" in parsed_web

    def test_emit_smart_state_marker_roundtrips_through_parser(self, capsys):
        """Sanity: the marker byte format matches the parser. Cheap
        regression that catches accidental prefix or json shape changes."""
        emit_smart_state_marker({"status": "completed", "credits_policy": "capture_full"})
        captured = capsys.readouterr().out
        lines = [l for l in captured.splitlines() if l.startswith(SMART_STATE_MARKER_PREFIX)]
        assert len(lines) == 1
        parsed = parse_smart_state_marker(lines[0])
        assert parsed == {"status": "completed", "credits_policy": "capture_full"}

    def test_parse_smart_state_marker_rejects_non_marker_lines(self):
        """Other log lines (stage progress, errors, ...) MUST return None
        so the runner's per-line walker keeps falling through to the
        next parser instead of treating noise as state updates."""
        assert parse_smart_state_marker("[S2] running") is None
        assert parse_smart_state_marker("[WEB_REVIEW] {}") is None
        assert parse_smart_state_marker("") is None
        # Malformed JSON inside marker must NOT raise — return None so
        # the runner just logs the line and moves on.
        assert parse_smart_state_marker("[SMART_STATE] {oops") is None
        # Non-dict JSON also rejected (we only carry state dicts).
        assert parse_smart_state_marker("[SMART_STATE] [1, 2, 3]") is None


# ===================================================================
# Mainline 2: continue after handoff does not re-enter smart gate —
# derive_effective_pipeline_mode flips smart→studio when status is
# downgraded_to_studio or fail_and_refunded
# ===================================================================


class TestContinueAfterHandoffDoesNotReEnterSmartGate:
    """F3 — derive_effective_pipeline_mode contract.

    record.service_mode stays "smart" (audit fact preserved, Gateway
    routing/billing still smart-priced). Pipeline-internal smart-aware
    branches read this derivation and traverse Studio control flow when
    the smart job has already handed off — preventing the auto layer
    from re-running the same failure on /continue.
    """

    def test_running_smart_job_returns_smart_mode(self):
        record = SimpleNamespace(
            service_mode="smart",
            smart_state={"status": "running"},
        )
        assert derive_effective_pipeline_mode(record) == "smart"

    def test_smart_job_with_no_smart_state_returns_smart_mode(self):
        # First-time entry — smart_state hasn't been written yet.
        record = SimpleNamespace(service_mode="smart", smart_state=None)
        assert derive_effective_pipeline_mode(record) == "smart"

    def test_handoff_smart_job_returns_studio_mode(self):
        # /continue after handoff — must NOT re-enter auto-review.
        record = SimpleNamespace(
            service_mode="smart",
            smart_state={"status": "downgraded_to_studio",
                         "reason": "translation_auto_approve_failed",
                         "handoff_stage": "voice_selection_review",
                         "credits_policy": "refund_full"},
        )
        assert derive_effective_pipeline_mode(record) == "studio"

    def test_terminal_smart_job_returns_studio_mode(self):
        # fail_and_refunded is terminal but if anything triggers a
        # rerun (sweeper / debug) it must NOT re-engage auto layer.
        record = SimpleNamespace(
            service_mode="smart",
            smart_state={"status": "fail_and_refunded",
                         "credits_policy": "capture_actual_cost_capped_at_studio_price"},
        )
        assert derive_effective_pipeline_mode(record) == "studio"

    def test_clone_blocked_waiting_retry_returns_smart_mode(self):
        # Plan §4.3: clone_blocked_waiting_retry is a paused state but
        # NOT a downgrade — a /continue should re-engage the auto layer
        # so the user's "use preset voice instead" decision flows back
        # through smart auto-review, not Studio human-review.
        record = SimpleNamespace(
            service_mode="smart",
            smart_state={"status": "clone_blocked_waiting_retry"},
        )
        assert derive_effective_pipeline_mode(record) == "smart"

    def test_express_job_unchanged(self):
        record = SimpleNamespace(service_mode="express", smart_state=None)
        assert derive_effective_pipeline_mode(record) == "express"

    def test_studio_job_unchanged(self):
        record = SimpleNamespace(service_mode="studio", smart_state=None)
        assert derive_effective_pipeline_mode(record) == "studio"

    def test_unknown_service_mode_falls_back_to_express(self):
        # Defensive: a record with no service_mode should not crash
        # the pipeline branch picker. Default to express (cheapest /
        # least automation) is the safest fallback.
        record = SimpleNamespace(service_mode=None, smart_state=None)
        assert derive_effective_pipeline_mode(record) == "express"


# ===================================================================
# Mainline 3: settle dispatcher routes smart_state.credits_policy
# before falling back to the legacy succeeded/failed branches
# ===================================================================


class TestSettleDispatchSmartCreditsPolicy:
    """F4 — settle_job_credit_ledger smart dispatcher.

    Verifies the smart_state.credits_policy branch is evaluated BEFORE
    the legacy succeeded/failed branches. Without this dispatcher,
    a smart `fail_and_refunded` job (which lands terminal_status="failed")
    would hit the legacy `failed → shadow_release` branch and the
    partial-capture / refund-clone settlement would never run.
    """

    def _make_smart_job(self, *, credits_policy, terminal_status="failed"):
        # Mirror estimate_actual_job_credits + settle inputs minimally.
        return SimpleNamespace(
            job_id="job_smart_001",
            user_id=uuid.uuid4(),
            service_mode="smart",
            smart_state={
                "status": "fail_and_refunded" if "actual_cost" in credits_policy else "completed",
                "credits_policy": credits_policy,
                "reserved_credits_per_minute": 100,
            },
            metering_snapshot={
                "credits_estimated": 500,
                "service_mode": "smart",
                "quality_tier": "standard",
            },
            actual_minutes=5.0,
            estimated_minutes=5.0,
            tts_provider="minimax",
            tts_model="speech-2.8-hd",
            role_snapshot="user",
        )

    def test_credits_policy_capture_actual_cost_dispatches_to_three_step(self):
        """capture_actual_cost_capped_at_studio_price → smart dispatcher
        runs shadow_release + refund_captured_voice_clone +
        partial_capture_actual_cost. Even with terminal_status="failed",
        the dispatcher must NOT take the legacy `failed → release_full`
        path."""
        from credits_service import settle_job_credit_ledger

        job = self._make_smart_job(
            credits_policy="capture_actual_cost_capped_at_studio_price",
            terminal_status="failed",
        )

        # Mock DB session: the dispatcher's row-lock select returns the
        # same job; _has_job_credit_reserve short-circuits via the
        # snapshot's credits_estimated > 0.
        mock_db = AsyncMock()
        mock_lock_result = MagicMock()
        mock_lock_result.scalar_one_or_none = MagicMock(return_value=job)
        mock_db.execute = AsyncMock(return_value=mock_lock_result)

        with patch("credits_service.shadow_release", new=AsyncMock(return_value=[])) as m_release, \
             patch("credits_service.refund_captured_voice_clone", new=AsyncMock(return_value=[])) as m_refund, \
             patch("credits_service.partial_capture_actual_cost", new=AsyncMock(return_value=[])) as m_partial, \
             patch("credits_service.shadow_capture", new=AsyncMock(return_value=[])) as m_capture, \
             patch("credits_service.should_settle_job_credits", return_value=True), \
             patch("credits_service._has_job_credit_reserve", new=AsyncMock(return_value=True)):
            _run(settle_job_credit_ledger(mock_db, job, "failed"))

        # Smart dispatcher took the three-step path.
        assert m_release.await_count == 1
        assert m_refund.await_count == 1
        assert m_partial.await_count == 1
        # CRITICAL: the legacy succeeded-branch capture must NOT have run.
        assert m_capture.await_count == 0
        # The smart release call carries the smart-distinct reason_code,
        # not the legacy job_release.
        release_call = m_release.await_args_list[0]
        assert release_call.kwargs["reason_code"] == "smart_fail_and_refund_release"

    def test_credits_policy_refund_full_dispatches_to_release_only(self):
        """refund_full (early downgrade / system bug) → just shadow_release
        with smart-distinct reason_code; no clone refund or partial capture."""
        from credits_service import settle_job_credit_ledger

        job = self._make_smart_job(credits_policy="refund_full")
        mock_db = AsyncMock()
        mock_lock_result = MagicMock()
        mock_lock_result.scalar_one_or_none = MagicMock(return_value=job)
        mock_db.execute = AsyncMock(return_value=mock_lock_result)

        with patch("credits_service.shadow_release", new=AsyncMock(return_value=[])) as m_release, \
             patch("credits_service.refund_captured_voice_clone", new=AsyncMock(return_value=[])) as m_refund, \
             patch("credits_service.partial_capture_actual_cost", new=AsyncMock(return_value=[])) as m_partial, \
             patch("credits_service.shadow_capture", new=AsyncMock(return_value=[])) as m_capture, \
             patch("credits_service.should_settle_job_credits", return_value=True), \
             patch("credits_service._has_job_credit_reserve", new=AsyncMock(return_value=True)):
            _run(settle_job_credit_ledger(mock_db, job, "failed"))

        assert m_release.await_count == 1
        assert m_release.await_args_list[0].kwargs["reason_code"] == "smart_refund_full"
        assert m_refund.await_count == 0
        assert m_partial.await_count == 0
        assert m_capture.await_count == 0

    def test_credits_policy_capture_full_dispatches_to_smart_capture(self):
        """capture_full (degraded_delivery_with_report) → shadow_capture
        with smart-distinct reason_code; not the legacy job_capture."""
        from credits_service import settle_job_credit_ledger

        job = self._make_smart_job(credits_policy="capture_full",
                                   terminal_status="succeeded")
        mock_db = AsyncMock()
        mock_lock_result = MagicMock()
        mock_lock_result.scalar_one_or_none = MagicMock(return_value=job)
        mock_db.execute = AsyncMock(return_value=mock_lock_result)

        # estimate_actual_job_credits expects to find tts_provider/model
        # to derive the rate; 5min × smart=100 ≈ 500.
        with patch("credits_service.shadow_release", new=AsyncMock(return_value=[])) as m_release, \
             patch("credits_service.refund_captured_voice_clone", new=AsyncMock(return_value=[])) as m_refund, \
             patch("credits_service.partial_capture_actual_cost", new=AsyncMock(return_value=[])) as m_partial, \
             patch("credits_service.shadow_capture", new=AsyncMock(return_value=[])) as m_capture, \
             patch("credits_service.estimate_actual_job_credits",
                   return_value=(500, "standard", 5.0, "smart")), \
             patch("credits_service.ensure_credit_buckets_for_user", new=AsyncMock()), \
             patch("credits_service.should_settle_job_credits", return_value=True), \
             patch("credits_service._has_job_credit_reserve", new=AsyncMock(return_value=True)):
            _run(settle_job_credit_ledger(mock_db, job, "succeeded"))

        assert m_capture.await_count == 1
        capture_call = m_capture.await_args_list[0]
        assert capture_call.kwargs["reason_code"] == "smart_capture_full"
        assert capture_call.kwargs["actual_credits"] == 500
        assert capture_call.kwargs["service_mode"] == "smart"
        assert m_release.await_count == 0
        assert m_refund.await_count == 0
        assert m_partial.await_count == 0

    def test_no_smart_state_falls_through_to_legacy_branches(self):
        """Express / studio jobs (smart_state=None) MUST go through the
        existing succeeded → shadow_capture path unchanged. Regression
        guard for the three-line patch in settle_job_credit_ledger."""
        from credits_service import settle_job_credit_ledger

        job = SimpleNamespace(
            job_id="job_studio_001",
            user_id=uuid.uuid4(),
            service_mode="studio",
            smart_state=None,
            metering_snapshot={
                "credits_estimated": 75,
                "service_mode": "studio",
                "quality_tier": "standard",
            },
            actual_minutes=5.0,
            estimated_minutes=5.0,
            tts_provider="minimax",
            tts_model="speech-2.8-hd",
            role_snapshot="user",
        )
        mock_db = AsyncMock()
        mock_lock_result = MagicMock()
        mock_lock_result.scalar_one_or_none = MagicMock(return_value=job)
        mock_db.execute = AsyncMock(return_value=mock_lock_result)

        with patch("credits_service.shadow_release", new=AsyncMock(return_value=[])) as m_release, \
             patch("credits_service.refund_captured_voice_clone", new=AsyncMock(return_value=[])) as m_refund, \
             patch("credits_service.partial_capture_actual_cost", new=AsyncMock(return_value=[])) as m_partial, \
             patch("credits_service.shadow_capture", new=AsyncMock(return_value=[])) as m_capture, \
             patch("credits_service.estimate_actual_job_credits",
                   return_value=(75, "standard", 5.0, "studio")), \
             patch("credits_service.ensure_credit_buckets_for_user", new=AsyncMock()), \
             patch("credits_service.should_settle_job_credits", return_value=True), \
             patch("credits_service._has_job_credit_reserve", new=AsyncMock(return_value=True)):
            _run(settle_job_credit_ledger(mock_db, job, "succeeded"))

        assert m_capture.await_count == 1
        # Legacy reason_code, not the smart-distinct one.
        assert m_capture.await_args_list[0].kwargs["reason_code"] == "job_capture"
        assert m_release.await_count == 0
        assert m_refund.await_count == 0
        assert m_partial.await_count == 0

    def test_unrecognised_credits_policy_falls_through_with_warning(self):
        """A typo'd or future-version credits_policy returns empty list
        rather than crashing or silently double-billing. Logged as
        warning; legacy branches NOT taken (they'd produce wrong amount)."""
        from credits_service import settle_job_credit_ledger

        job = self._make_smart_job(credits_policy="some_future_policy_v2")
        mock_db = AsyncMock()
        mock_lock_result = MagicMock()
        mock_lock_result.scalar_one_or_none = MagicMock(return_value=job)
        mock_db.execute = AsyncMock(return_value=mock_lock_result)

        with patch("credits_service.shadow_release", new=AsyncMock(return_value=[])) as m_release, \
             patch("credits_service.shadow_capture", new=AsyncMock(return_value=[])) as m_capture, \
             patch("credits_service.should_settle_job_credits", return_value=True), \
             patch("credits_service._has_job_credit_reserve", new=AsyncMock(return_value=True)):
            result = _run(settle_job_credit_ledger(mock_db, job, "failed"))

        # No ledger writes — the dispatcher refused to guess.
        assert result == []
        assert m_release.await_count == 0
        assert m_capture.await_count == 0


# ===================================================================
# Codex 第八轮 followups — F1 mirror chain + F2 idempotency
# + F3 dict-helper + F4 export
# ===================================================================


class TestSmartStateMirrorChain:
    """Codex 第八轮 F1 — Gateway DB.Job.smart_state must reflect the
    upstream JSON record. Without this the F4 settle dispatcher reads
    db_job.smart_state=None and skips the smart credits_policy branch.
    """

    def test_record_from_payload_extracts_smart_state(self):
        """Reader-side: JSON store payload → JobJsonRecord carries smart_state."""
        from pathlib import Path

        from gateway.storage.job_store_reader import _record_from_payload

        record = _record_from_payload(
            Path("/dummy/job_x.json"),
            {
                "job_id": "job_x",
                "status": "succeeded",
                "service_mode": "smart",
                "smart_state": {
                    "status": "completed",
                    "credits_policy": "capture_full",
                    "reserved_credits_per_minute": 100,
                },
            },
        )
        assert record.job_id == "job_x"
        assert record.smart_state == {
            "status": "completed",
            "credits_policy": "capture_full",
            "reserved_credits_per_minute": 100,
        }

    def test_record_from_payload_smart_state_absent_returns_none(self):
        """Express/studio jobs (no smart_state field) → None, not {}."""
        from pathlib import Path

        from gateway.storage.job_store_reader import _record_from_payload

        record = _record_from_payload(
            Path("/dummy/job_x.json"),
            {"job_id": "job_x", "status": "succeeded", "service_mode": "studio"},
        )
        assert record.smart_state is None

    def test_record_from_payload_smart_state_non_dict_treated_as_none(self):
        """Defensive — corrupted JSON store with smart_state="garbage" must
        not crash; treat as None so existing DB value isn't clobbered."""
        from pathlib import Path

        from gateway.storage.job_store_reader import _record_from_payload

        record = _record_from_payload(
            Path("/dummy/job_x.json"),
            {"job_id": "job_x", "status": "succeeded", "smart_state": "not a dict"},
        )
        assert record.smart_state is None

    def test_mirror_terminal_state_writes_smart_state_to_db(self):
        """The actual mirror function must populate db_job.smart_state from
        upstream.smart_state BEFORE the settle block runs (so the F4
        dispatcher sees the credits_policy)."""
        from datetime import datetime, timezone

        from gateway.job_terminal_mirror import mirror_job_terminal_state
        from gateway.storage.job_store_reader import JobJsonRecord

        db_job = SimpleNamespace(
            job_id="job_smart_002",
            status="running",
            current_stage="alignment",
            project_dir="/projects/abc",
            completed_at=None,
            edit_generation=0,
            smart_state=None,  # DB column starts NULL — express/studio default
            quota_state="reserved",
        )
        upstream = JobJsonRecord(
            job_id="job_smart_002",
            status="succeeded",
            completed_at=datetime.now(timezone.utc),
            project_dir="/projects/abc",
            current_stage="completed",
            edit_generation=0,
            jianying_draft_zip_path=None,
            service_mode="smart",
            smart_state={
                "status": "fail_and_refunded",
                "credits_policy": "capture_actual_cost_capped_at_studio_price",
            },
        )
        mock_db = AsyncMock()

        # Mock both settle helpers to no-ops so we focus on the mirror.
        with patch("gateway.job_terminal_mirror.settle_job_quota", new=AsyncMock()), \
             patch("gateway.job_terminal_mirror.settle_job_credit_ledger",
                   new=AsyncMock(return_value=[])):
            changed = _run(mirror_job_terminal_state(mock_db, db_job, upstream))

        assert changed is True
        # smart_state was mirrored from the upstream payload onto db_job.
        assert db_job.smart_state == {
            "status": "fail_and_refunded",
            "credits_policy": "capture_actual_cost_capped_at_studio_price",
        }

    def test_mirror_terminal_state_merges_partial_smart_state_updates(self):
        """An earlier marker may have set reserved_credits_per_minute; a later
        marker only adds status/credits_policy. The merge must preserve the
        earlier key (last-write-wins per key, not full replace)."""
        from datetime import datetime, timezone

        from gateway.job_terminal_mirror import mirror_job_terminal_state
        from gateway.storage.job_store_reader import JobJsonRecord

        db_job = SimpleNamespace(
            job_id="job_smart_003",
            status="running",
            current_stage="alignment",
            project_dir="/projects/abc",
            completed_at=None,
            edit_generation=0,
            # Earlier marker already wrote the reserve snapshot.
            smart_state={"reserved_credits_per_minute": 100},
            quota_state="reserved",
        )
        upstream = JobJsonRecord(
            job_id="job_smart_003",
            status="succeeded",
            completed_at=datetime.now(timezone.utc),
            project_dir="/projects/abc",
            current_stage="completed",
            edit_generation=0,
            jianying_draft_zip_path=None,
            service_mode="smart",
            # Only carries the new keys.
            smart_state={"status": "completed", "credits_policy": "capture_full"},
        )
        mock_db = AsyncMock()
        with patch("gateway.job_terminal_mirror.settle_job_quota", new=AsyncMock()), \
             patch("gateway.job_terminal_mirror.settle_job_credit_ledger",
                   new=AsyncMock(return_value=[])):
            _run(mirror_job_terminal_state(mock_db, db_job, upstream))

        # Both old reserve key AND new policy keys should be present.
        assert db_job.smart_state == {
            "reserved_credits_per_minute": 100,
            "status": "completed",
            "credits_policy": "capture_full",
        }

    def test_mirror_terminal_state_no_op_when_upstream_smart_state_none(self):
        """Express/studio mirror (upstream.smart_state=None) must NOT clobber
        an existing DB smart_state — though for express/studio it's None
        anyway, the no-clobber semantics matter for partial-payload polls."""
        from datetime import datetime, timezone

        from gateway.job_terminal_mirror import mirror_job_terminal_state
        from gateway.storage.job_store_reader import JobJsonRecord

        existing = {"status": "completed", "credits_policy": "capture_full"}
        db_job = SimpleNamespace(
            job_id="job_smart_004",
            status="running",
            current_stage="alignment",
            project_dir="/projects/abc",
            completed_at=None,
            edit_generation=0,
            smart_state=dict(existing),
            quota_state="reserved",
        )
        upstream = JobJsonRecord(
            job_id="job_smart_004",
            status="succeeded",
            completed_at=datetime.now(timezone.utc),
            project_dir="/projects/abc",
            current_stage="completed",
            edit_generation=0,
            jianying_draft_zip_path=None,
            service_mode="smart",
            smart_state=None,  # upstream payload didn't carry the field
        )
        mock_db = AsyncMock()
        with patch("gateway.job_terminal_mirror.settle_job_quota", new=AsyncMock()), \
             patch("gateway.job_terminal_mirror.settle_job_credit_ledger",
                   new=AsyncMock(return_value=[])):
            _run(mirror_job_terminal_state(mock_db, db_job, upstream))

        # Existing db_job.smart_state preserved verbatim.
        assert db_job.smart_state == existing


class TestSettlementIdempotencyForSmartReasonCodes:
    """Codex 第八轮 F2 — _settlement_reason_codes must include smart codes
    in the same idempotency family as the legacy job_reserve codes; otherwise
    a sweeper retry can re-write smart ledger entries on top of an already-
    settled job, double-charging the user.
    """

    def test_smart_reason_codes_in_legacy_idempotency_family(self):
        """When checking dedup for any legacy job_reserve settlement code,
        the returned set must include the new smart codes (and vice versa).
        This is what _has_existing_settlement uses to detect prior writes."""
        from credits_service import _settlement_reason_codes

        legacy = _settlement_reason_codes("job_capture", "job_reserve")
        # Legacy codes still present.
        assert "job_capture" in legacy
        assert "job_release" in legacy
        # Smart codes joined the family.
        for smart_code in (
            "smart_refund_full",
            "smart_capture_full",
            "smart_fail_and_refund_release",
            "smart_fail_and_refund_clone_reversal",
            "smart_fail_and_refund_partial_capture",
        ):
            assert smart_code in legacy, (
                f"{smart_code!r} missing from job_reserve idempotency family — "
                "sweeper retry will not detect prior smart settlement."
            )

    def test_smart_reason_codes_alone_returns_full_family(self):
        """A query keyed on a smart code must also expand to the full family
        so a smart-write followed by a legacy-style retry still dedups."""
        from credits_service import _settlement_reason_codes

        family = _settlement_reason_codes("smart_fail_and_refund_release", None)
        assert "job_capture" in family
        assert "smart_capture_full" in family

    def test_unrelated_reason_code_does_not_pollute_family(self):
        """Non-job-reserve codes (e.g. voice_clone_capture) keep their own
        single-element set — we don't want clone settlement to be deduped
        against job-reserve settlement."""
        from credits_service import _settlement_reason_codes

        family = _settlement_reason_codes("voice_clone_capture", None)
        assert family == {"voice_clone_capture"}


class TestDeriveEffectiveModeOnDictRecord:
    """Codex 第八轮 F3 — derive_effective_pipeline_mode MUST handle the
    dict shape that the real pipeline uses (process.py:1426
    ``_jr = _job_record.to_dict()``). The first-cut implementation used
    getattr() only and silently returned 'express' on every smart job in
    production, defeating the entire F3 mechanism.
    """

    def test_dict_record_running_smart_returns_smart(self):
        record = {"service_mode": "smart", "smart_state": {"status": "running"}}
        assert derive_effective_pipeline_mode(record) == "smart"

    def test_dict_record_handoff_returns_studio(self):
        record = {
            "service_mode": "smart",
            "smart_state": {"status": "downgraded_to_studio",
                            "credits_policy": "refund_full"},
        }
        assert derive_effective_pipeline_mode(record) == "studio"

    def test_dict_record_no_smart_state_key_returns_smart_for_smart_mode(self):
        # A fresh smart job before any marker emit — dict has service_mode
        # but no smart_state key at all. Must NOT silently downgrade to express.
        record = {"service_mode": "smart"}
        assert derive_effective_pipeline_mode(record) == "smart"

    def test_dict_record_express_returns_express(self):
        record = {"service_mode": "express", "smart_state": None}
        assert derive_effective_pipeline_mode(record) == "express"

    def test_dict_record_smart_state_is_none_returns_smart(self):
        # Explicit None for smart_state (vs key absent) must behave identically.
        record = {"service_mode": "smart", "smart_state": None}
        assert derive_effective_pipeline_mode(record) == "smart"

    def test_attribute_record_still_works(self):
        """Regression — the dataclass / SimpleNamespace path must keep working
        after we added the Mapping branch."""
        record = SimpleNamespace(
            service_mode="smart",
            smart_state={"status": "fail_and_refunded"},
        )
        assert derive_effective_pipeline_mode(record) == "studio"


class TestSmartPackageReExports:
    """Codex 第八轮 F4 — public surface declared in __init__ docstring
    must match the actual __all__ / re-exports."""

    def test_emit_handoff_markers_re_exported_from_smart_package(self):
        """The package docstring lists handoff.emit_handoff_markers as
        public surface; the symbol must be importable from the package
        root, otherwise the doc lies."""
        import services.smart as smart_pkg

        assert hasattr(smart_pkg, "emit_handoff_markers")
        assert "emit_handoff_markers" in smart_pkg.__all__

    def test_state_module_symbols_re_exported(self):
        """Same check for state.py exports — sanity guard the __init__
        contract doesn't drift away from the docstring."""
        import services.smart as smart_pkg

        for name in (
            "SMART_STATE_MARKER_PREFIX",
            "derive_effective_pipeline_mode",
            "emit_smart_state_marker",
            "parse_smart_state_marker",
        ):
            assert hasattr(smart_pkg, name), f"{name!r} missing from smart package"
            assert name in smart_pkg.__all__
