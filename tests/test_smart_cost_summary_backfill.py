"""Smart MVP P3-b Phase 2: post-settle cost_summary backfill tests.

Per decision log §2 Phase 2: after Gateway's ``settle_job_credit_ledger``
runs at job terminal, the cost_summary.json on disk has its two
``pending_*`` fields replaced with real values:

  - ``pending_credits_charged`` ← net credits captured for this job
  - ``cost_breakdown_internal_only.pending_minimax_quota_used_after``
    ← current ``used`` count from /api/internal/user-voices/quota

These tests pin the pure-function helper that performs the file
read-modify-write — actual Gateway wiring lives in
``gateway/job_terminal_mirror.py``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace


_REPO = Path(__file__).resolve().parents[1]
_GATEWAY = _REPO / "gateway"
if str(_GATEWAY) not in sys.path:
    sys.path.insert(0, str(_GATEWAY))


def _write_cost_summary(
    project_dir: Path,
    *,
    pending_credits_charged=None,
    pending_minimax_quota_used_after=None,
    credits_policy="capture_full",
) -> Path:
    audit = project_dir / "audit"
    audit.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "job_id": "job_x",
        "service_mode": "smart",
        "minutes_processed": 12.5,
        "pending_credits_charged": pending_credits_charged,
        "credits_policy": credits_policy,
        "cost_breakdown_internal_only": {
            "asr_seconds": 45.2,
            "llm_translation_chars": 5234,
            "tts_chars": 8120,
            "voice_clone_calls": 1,
            "pending_minimax_quota_used_after": pending_minimax_quota_used_after,
        },
        "generated_at": "2026-05-15T11:00:00+00:00",
    }
    target = audit / "smart_cost_summary.json"
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return target


def _ledger_entry(direction: str, delta: int, reason_code: str = "rc"):
    """Build a duck-typed CreditsLedger row for the helper to sum."""
    return SimpleNamespace(
        direction=direction,
        credits_delta=delta,
        reason_code=reason_code,
    )


# ===========================================================================
# Cycle 1 — gating: skip non-smart / no project_dir / no cost_summary.json
# ===========================================================================


class TestBackfillGating:

    def test_returns_false_for_non_smart_job(self, tmp_path):
        from cost_summary_backfill import backfill_smart_cost_summary

        ok = backfill_smart_cost_summary(
            service_mode="studio",
            project_dir=str(tmp_path),
            credit_entries=[],
            quota_used=10,
        )
        assert ok is False

    def test_returns_false_when_project_dir_is_none(self):
        from cost_summary_backfill import backfill_smart_cost_summary

        ok = backfill_smart_cost_summary(
            service_mode="smart",
            project_dir=None,
            credit_entries=[],
            quota_used=10,
        )
        assert ok is False

    def test_returns_false_when_cost_summary_file_missing(self, tmp_path):
        from cost_summary_backfill import backfill_smart_cost_summary

        # No file written. Helper must NOT crash, just return False.
        ok = backfill_smart_cost_summary(
            service_mode="smart",
            project_dir=str(tmp_path),
            credit_entries=[],
            quota_used=10,
        )
        assert ok is False


# ===========================================================================
# Cycle 2 — happy path: credit + quota fields backfilled
# ===========================================================================


class TestBackfillHappyPath:

    def test_capture_entries_sum_into_pending_credits_charged(self, tmp_path):
        from cost_summary_backfill import backfill_smart_cost_summary

        target = _write_cost_summary(tmp_path)
        entries = [
            _ledger_entry("capture", 800),
            _ledger_entry("capture", 200),  # split across buckets
        ]
        ok = backfill_smart_cost_summary(
            service_mode="smart",
            project_dir=str(tmp_path),
            credit_entries=entries,
            quota_used=5,
        )
        assert ok is True

        result = json.loads(target.read_text(encoding="utf-8"))
        assert result["pending_credits_charged"] == 1000
        assert (
            result["cost_breakdown_internal_only"][
                "pending_minimax_quota_used_after"
            ]
            == 5
        )

    def test_refund_entries_subtract_from_net_charged(self, tmp_path):
        """Smart fail_and_refund: capture 600, then refund 600 → net 0."""
        from cost_summary_backfill import backfill_smart_cost_summary

        target = _write_cost_summary(tmp_path)
        entries = [
            _ledger_entry("capture", 600),
            _ledger_entry("refund", 600),
        ]
        ok = backfill_smart_cost_summary(
            service_mode="smart",
            project_dir=str(tmp_path),
            credit_entries=entries,
            quota_used=2,
        )
        assert ok is True
        result = json.loads(target.read_text(encoding="utf-8"))
        assert result["pending_credits_charged"] == 0

    def test_refund_full_writes_zero_credits(self, tmp_path):
        """No capture entries (refund_full) → credits_charged = 0."""
        from cost_summary_backfill import backfill_smart_cost_summary

        target = _write_cost_summary(tmp_path)
        entries = [
            _ledger_entry("release", 1000),  # reserve released, no capture
        ]
        ok = backfill_smart_cost_summary(
            service_mode="smart",
            project_dir=str(tmp_path),
            credit_entries=entries,
            quota_used=0,
        )
        assert ok is True
        result = json.loads(target.read_text(encoding="utf-8"))
        assert result["pending_credits_charged"] == 0

    def test_negative_delta_treated_as_absolute(self, tmp_path):
        """Per shadow_capture (gateway/credits_service.py:693), the
        codebase treats credits_delta sign inconsistently — some paths
        write negative for outflow. Helper must use abs() to match the
        existing convention."""
        from cost_summary_backfill import backfill_smart_cost_summary

        target = _write_cost_summary(tmp_path)
        entries = [
            _ledger_entry("capture", -500),  # negative = outflow
            _ledger_entry("capture", 300),
        ]
        ok = backfill_smart_cost_summary(
            service_mode="smart",
            project_dir=str(tmp_path),
            credit_entries=entries,
            quota_used=1,
        )
        assert ok is True
        result = json.loads(target.read_text(encoding="utf-8"))
        assert result["pending_credits_charged"] == 800

    def test_quota_used_none_leaves_quota_field_unchanged(self, tmp_path):
        """If Gateway can't query quota at settle time (Codex 27 P0
        fail-closed contract: unknown quota stays None), backfill MUST
        NOT pretend a fake number. Leave that field as None and only
        update credits."""
        from cost_summary_backfill import backfill_smart_cost_summary

        target = _write_cost_summary(tmp_path)
        ok = backfill_smart_cost_summary(
            service_mode="smart",
            project_dir=str(tmp_path),
            credit_entries=[_ledger_entry("capture", 100)],
            quota_used=None,  # quota lookup failed upstream
        )
        assert ok is True
        result = json.loads(target.read_text(encoding="utf-8"))
        assert result["pending_credits_charged"] == 100
        assert (
            result["cost_breakdown_internal_only"][
                "pending_minimax_quota_used_after"
            ]
            is None
        )

    def test_backfill_stamps_settled_at_timestamp(self, tmp_path):
        """Admin tooling needs to distinguish pre-settle (pipeline emit
        time) from post-settle (Gateway backfill time). Add a
        ``settled_at`` field on backfill."""
        from cost_summary_backfill import backfill_smart_cost_summary

        target = _write_cost_summary(tmp_path)
        ok = backfill_smart_cost_summary(
            service_mode="smart",
            project_dir=str(tmp_path),
            credit_entries=[_ledger_entry("capture", 100)],
            quota_used=3,
        )
        assert ok is True
        result = json.loads(target.read_text(encoding="utf-8"))
        assert "settled_at" in result
        assert isinstance(result["settled_at"], str)
        assert "T" in result["settled_at"]


# ===========================================================================
# Cycle 3 — idempotency: calling twice produces same result
# ===========================================================================


class TestBackfillIdempotent:

    def test_two_backfills_with_same_inputs_yield_same_payload(self, tmp_path):
        """Mirror is level-triggered. Settlement is idempotent via ledger
        guards; backfill must be idempotent too — calling twice doesn't
        accumulate or duplicate the pending_* fields."""
        from cost_summary_backfill import backfill_smart_cost_summary

        target = _write_cost_summary(tmp_path)
        entries = [_ledger_entry("capture", 250)]

        ok1 = backfill_smart_cost_summary(
            service_mode="smart",
            project_dir=str(tmp_path),
            credit_entries=entries,
            quota_used=7,
        )
        first = json.loads(target.read_text(encoding="utf-8"))

        ok2 = backfill_smart_cost_summary(
            service_mode="smart",
            project_dir=str(tmp_path),
            credit_entries=entries,
            quota_used=7,
        )
        second = json.loads(target.read_text(encoding="utf-8"))

        assert ok1 is True and ok2 is True
        assert first["pending_credits_charged"] == 250
        assert second["pending_credits_charged"] == 250
        assert (
            first["cost_breakdown_internal_only"]["pending_minimax_quota_used_after"]
            == 7
        )
        # ``settled_at`` may differ across calls (it's a timestamp); the
        # important invariant is the numeric fields are stable.
        first.pop("settled_at", None)
        second.pop("settled_at", None)
        assert first == second


# ===========================================================================
# Cycle 4 — never raises on I/O failure (plan §6.4 末段)
# ===========================================================================


class TestBackfillBestEffort:

    def test_never_raises_on_malformed_existing_file(self, tmp_path):
        """If the file on disk is somehow corrupt, helper logs + returns
        False — does not crash the mirror callback."""
        from cost_summary_backfill import backfill_smart_cost_summary

        audit = tmp_path / "audit"
        audit.mkdir()
        (audit / "smart_cost_summary.json").write_text(
            "not json at all", encoding="utf-8",
        )
        ok = backfill_smart_cost_summary(
            service_mode="smart",
            project_dir=str(tmp_path),
            credit_entries=[_ledger_entry("capture", 100)],
            quota_used=1,
        )
        assert ok is False


# ===========================================================================
# Cycle 5 — Codex 第三十九轮 P1: orchestrator queries canonical persisted
# ledger instead of trusting per-call return; skips on settle failure
# ===========================================================================


import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestOrchestratorCanonicalLedger:
    """Codex 第三十九轮 P1 regression tests for
    ``_backfill_smart_cost_summary_post_settle`` (gateway/job_terminal_mirror.py).

    Two failure modes the pure-helper tests above do NOT cover:

    1. ``settle_job_credit_ledger`` raised → orchestrator's
       ``credit_entries`` would have stayed ``[]`` (from initialization).
       Pre-fix code wrote ``pending_credits_charged=0`` + ``settled_at``,
       misleading admin into thinking the job settled at 0 credits.
    2. ``settle_job_credit_ledger`` returned ``[]`` because idempotency
       guard short-circuited (job already settled by previous mirror
       pass) → same false 0 problem, plus historical jobs settled
       before Phase 2 deploy can never get backfilled.

    Fix: orchestrator queries CreditsLedger by ``related_job_id`` to get
    the CANONICAL net charge regardless of what the current call
    returned. Skips entirely on settle exception.
    """

    @pytest.mark.asyncio
    async def test_orchestrator_skips_when_settle_failed(
        self, monkeypatch, tmp_path,
    ):
        """settle raised → orchestrator MUST NOT call backfill_helper
        (no settled_at stamp, no false 0 in pending_credits_charged)."""
        from job_terminal_mirror import _backfill_smart_cost_summary_post_settle

        calls: list[dict] = []
        monkeypatch.setattr(
            "job_terminal_mirror.backfill_smart_cost_summary",
            lambda **kwargs: calls.append(kwargs) or True,
        )

        fake_db = MagicMock()
        fake_db.execute = AsyncMock(return_value=MagicMock())
        fake_db_job = MagicMock(
            service_mode="smart",
            project_dir=str(tmp_path),
            job_id="job_x",
            user_id="user_x",
        )

        await _backfill_smart_cost_summary_post_settle(
            fake_db,
            db_job=fake_db_job,
            settle_succeeded=False,  # settle raised
        )
        assert calls == [], (
            "Orchestrator wrote backfill despite settle raising — would "
            "stamp settled_at on incomplete data."
        )

    @pytest.mark.asyncio
    async def test_orchestrator_queries_persisted_ledger_when_settle_ok(
        self, monkeypatch, tmp_path,
    ):
        """settle returned []` (idempotent or already settled) BUT
        persisted ledger has captures → backfill MUST receive persisted
        entries, not the empty per-call list."""
        from job_terminal_mirror import _backfill_smart_cost_summary_post_settle

        # Persisted ledger has 2 captures totaling 1500.
        persisted_entries = [
            _ledger_entry("capture", 1000, "smart_capture_full"),
            _ledger_entry("capture", 500, "smart_capture_full"),
        ]

        # Mock db.execute to return ledger entries on first call (the
        # CreditsLedger query) and quota result on second.
        ledger_result = MagicMock()
        ledger_result.scalars.return_value = MagicMock()
        ledger_result.scalars.return_value.all.return_value = persisted_entries
        quota_result = MagicMock()
        quota_result.scalar.return_value = 5

        fake_db = MagicMock()
        fake_db.execute = AsyncMock(side_effect=[ledger_result, quota_result])

        calls: list[dict] = []
        monkeypatch.setattr(
            "job_terminal_mirror.backfill_smart_cost_summary",
            lambda **kwargs: calls.append(kwargs) or True,
        )

        import uuid as _uuid
        fake_db_job = MagicMock(
            service_mode="smart",
            project_dir=str(tmp_path),
            job_id="job_x",
            user_id=_uuid.uuid4(),
        )

        await _backfill_smart_cost_summary_post_settle(
            fake_db,
            db_job=fake_db_job,
            settle_succeeded=True,
        )
        assert len(calls) == 1, "backfill helper not called"
        call_entries = calls[0]["credit_entries"]
        # Must receive PERSISTED entries (length 2), not the (empty)
        # current-call return.
        assert len(call_entries) == 2
        assert calls[0]["quota_used"] == 5

    @pytest.mark.asyncio
    async def test_orchestrator_skips_when_ledger_query_fails(
        self, monkeypatch, tmp_path,
    ):
        """DB execute raises → orchestrator returns early, no backfill
        (don't stamp settled_at on incomplete data)."""
        from job_terminal_mirror import _backfill_smart_cost_summary_post_settle

        fake_db = MagicMock()
        fake_db.execute = AsyncMock(side_effect=RuntimeError("db down"))

        calls: list[dict] = []
        monkeypatch.setattr(
            "job_terminal_mirror.backfill_smart_cost_summary",
            lambda **kwargs: calls.append(kwargs) or True,
        )

        import uuid as _uuid
        fake_db_job = MagicMock(
            service_mode="smart",
            project_dir=str(tmp_path),
            job_id="job_x",
            user_id=_uuid.uuid4(),
        )

        await _backfill_smart_cost_summary_post_settle(
            fake_db,
            db_job=fake_db_job,
            settle_succeeded=True,
        )
        assert calls == [], (
            "Orchestrator called backfill despite ledger query failure"
        )

    @pytest.mark.asyncio
    async def test_orchestrator_skips_non_smart(
        self, monkeypatch, tmp_path,
    ):
        """Non-smart job → orchestrator returns early, no DB query, no
        backfill."""
        from job_terminal_mirror import _backfill_smart_cost_summary_post_settle

        fake_db = MagicMock()
        fake_db.execute = AsyncMock()

        calls: list[dict] = []
        monkeypatch.setattr(
            "job_terminal_mirror.backfill_smart_cost_summary",
            lambda **kwargs: calls.append(kwargs) or True,
        )

        fake_db_job = MagicMock(
            service_mode="studio",  # NOT smart
            project_dir=str(tmp_path),
            job_id="job_x",
            user_id="user_x",
        )

        await _backfill_smart_cost_summary_post_settle(
            fake_db,
            db_job=fake_db_job,
            settle_succeeded=True,
        )
        assert calls == []
        fake_db.execute.assert_not_called()
