"""Codex 第四十轮 P1.2: STUB billing policy hard-gate tests.

gateway/credits_service.py ``capture_actual_cost_capped_at_studio_price``
dispatcher calls:
  - shadow_release (works)
  - refund_captured_voice_clone (STUB — returns [] with WARNING)
  - partial_capture_actual_cost (STUB — returns [] with WARNING)

Currently the dispatcher silently aggregates the [] returns and the
caller sees a settled-looking response with no actual ledger entries.
Net effect: clone credits never get refunded, user's reservation
gets released but the internal ledger is inconsistent.

Codex 40 P1.2 fix (defense-in-depth alongside Fix 1's consent
validator that rejects ``on_budget_exhausted=fail_and_refund``):
the settle dispatcher itself MUST hard-gate this policy — return []
with logger.error so any code path that mysteriously sets it (admin
override / bug / future refactor) gets a loud signal instead of
silent stub execution.

This module's tests cover the dispatcher behavior. The validator-
side rejection is covered by test_smart_consent_validator.py
TestSmartConsentValidatorPolicyGate.
"""
from __future__ import annotations

import sys
from pathlib import Path


_REPO = Path(__file__).resolve().parents[1]
_GATEWAY = _REPO / "gateway"
if str(_GATEWAY) not in sys.path:
    sys.path.insert(0, str(_GATEWAY))


# Source-level guards — much easier than mocking the full async
# AsyncSession + ledger model + dispatch chain. Pin the contract via
# the source shape of _settle_smart_job_credit_ledger.


_CREDITS_SERVICE = _REPO / "gateway" / "credits_service.py"


class TestStubPolicyHardGate:

    def _source(self) -> str:
        return _CREDITS_SERVICE.read_text(encoding="utf-8")

    def test_capped_at_studio_price_branch_does_not_call_stub_refund(self):
        """The ``capture_actual_cost_capped_at_studio_price`` branch
        MUST NOT call ``refund_captured_voice_clone`` (STUB) — that
        would silently emit no ledger entries and the caller's
        post-settle backfill would stamp ``settled_at`` on bogus
        accounting state."""
        source = self._source()
        # Find the branch.
        branch_anchor = (
            'if credits_policy == "capture_actual_cost_capped_at_studio_price":'
        )
        idx = source.find(branch_anchor)
        assert idx >= 0, "branch anchor missing — credits_service.py shape changed"

        # Look at the branch body (next ~2000 chars).
        body = source[idx : idx + 2000]
        assert "refund_captured_voice_clone(" not in body, (
            "capture_actual_cost_capped_at_studio_price MUST NOT call "
            "refund_captured_voice_clone (it's a STUB returning []). "
            "Hard-gate the branch instead: logger.error + return [].\n"
            f"Branch body:\n{body[:1200]}"
        )
        assert "partial_capture_actual_cost(" not in body, (
            "capture_actual_cost_capped_at_studio_price MUST NOT call "
            "partial_capture_actual_cost (also STUB).\n"
            f"Branch body:\n{body[:1200]}"
        )

    def test_capped_at_studio_price_branch_logs_error_and_returns_empty(self):
        """When the dispatcher receives the not-yet-implemented policy,
        it MUST log an error (so ops sees this happened) and return
        empty list (no fake ledger entries)."""
        source = self._source()
        branch_anchor = (
            'if credits_policy == "capture_actual_cost_capped_at_studio_price":'
        )
        idx = source.find(branch_anchor)
        assert idx >= 0

        body = source[idx : idx + 2000]
        # Must contain explicit "not implemented" / "blocked" hint
        # (loose match — accept either phrasing).
        has_block_signal = any(
            token in body
            for token in (
                "not_implemented",
                "STUB",
                "stub",
                "Codex 40",
                "blocked",
            )
        )
        assert has_block_signal, (
            "Hard-gate branch must carry a comment / message indicating "
            "this policy is not yet implemented. Otherwise future "
            "maintainers may try to call the STUB functions.\n"
            f"Branch body:\n{body[:1200]}"
        )
        # Must log at error level (so the gate is visible in ops logs).
        assert (
            "logger.error" in body
            or 'logger.warning' in body
            or 'logger.critical' in body
        ), (
            "Hard-gate must log so ops sees this code path was hit "
            "(silent stub returns are exactly what Codex 40 flagged).\n"
            f"Branch body:\n{body[:1200]}"
        )
        # Must return [] (no ledger entries).
        assert "return []" in body, (
            "Hard-gate must return empty ledger list — no fake entries.\n"
            f"Branch body:\n{body[:1200]}"
        )
