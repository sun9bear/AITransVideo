"""fail_and_refund deferred — Task #25 (P2 launch blocker #3).

Pins three boundaries so a future PR can't silently re-expose
fail_and_refund without an explicit design + settlement implementation:

  1. Gateway validator MUST reject `on_budget_exhausted=fail_and_refund`.
  2. Frontend createJob MUST submit `degraded_delivery_with_report`
     (NOT fail_and_refund) for any smart job.
  3. Frontend MUST NOT expose a user-facing choice for on_budget_exhausted.

Per codex 2026-05-24 audit + rebaseline §3.3 decision: fail_and_refund
is DEFERRED, not removed. Validator's _BLOCKED_BUDGET_POLICIES + this
test set are the safety net.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[1]
_GATEWAY = _REPO_ROOT / "gateway"
if str(_GATEWAY) not in sys.path:
    sys.path.insert(0, str(_GATEWAY))


# ─────────────────────────────────────────────────────────────────────
# 1. Validator MUST reject fail_and_refund
# ─────────────────────────────────────────────────────────────────────


class TestValidatorRejectsFailAndRefund:
    """Source of truth: gateway/smart_consent.py::validate_smart_consent.

    Without these guards, a future refactor moving fail_and_refund from
    _BLOCKED_BUDGET_POLICIES to _ALLOWED_BUDGET_POLICIES would silently
    expose the broken settlement path."""

    def test_fail_and_refund_in_blocked_set(self):
        from smart_consent import _BLOCKED_BUDGET_POLICIES

        assert "fail_and_refund" in _BLOCKED_BUDGET_POLICIES, (
            "fail_and_refund MUST remain in _BLOCKED_BUDGET_POLICIES. "
            "Removing it without implementing the partial-capture / "
            "clone-refund settle path will silently break user billing."
        )

    def test_fail_and_refund_NOT_in_allowed_set(self):
        from smart_consent import _ALLOWED_BUDGET_POLICIES

        assert "fail_and_refund" not in _ALLOWED_BUDGET_POLICIES, (
            "fail_and_refund MUST NOT be in _ALLOWED_BUDGET_POLICIES "
            "until full three-step settle implemented (release reserve, "
            "reverse captured clone, partial capture)."
        )

    def test_validator_rejects_fail_and_refund_with_explanatory_error(self):
        """End-to-end: validate_smart_consent returns error mentioning
        not_implemented_yet and pointing to degraded_delivery_with_report."""
        from smart_consent import validate_smart_consent

        result, error = validate_smart_consent({
            "auto_voice_clone": True,
            "auto_retranslate": False,
            "auto_retts": True,
            "auto_multimodal_verification": False,
            "no_extra_charge_without_confirmation": True,
            "on_budget_exhausted": "fail_and_refund",
        })

        assert result is None
        assert error is not None
        assert "fail_and_refund" in error
        assert "not_implemented_yet" in error
        assert "degraded_delivery_with_report" in error, (
            "Error message must point the caller at the only allowed "
            "policy, otherwise debugging is painful."
        )

    def test_validator_accepts_degraded_delivery_with_report(self):
        """The allowed policy MUST pass — paranoid regression in case
        someone tightens the validator further."""
        from smart_consent import validate_smart_consent

        result, error = validate_smart_consent({
            "auto_voice_clone": True,
            "auto_retranslate": False,
            "auto_retts": True,
            "auto_multimodal_verification": False,
            "no_extra_charge_without_confirmation": True,
            "on_budget_exhausted": "degraded_delivery_with_report",
        })

        assert error is None
        assert result is not None
        assert result.on_budget_exhausted == "degraded_delivery_with_report"


# ─────────────────────────────────────────────────────────────────────
# 2. Frontend createJob hardcodes degraded_delivery_with_report
# ─────────────────────────────────────────────────────────────────────


class TestFrontendCreateJobHardcodesDegradedDelivery:
    """Source-level pin on the frontend smart_consent payload.

    Lives in tests/ not in frontend-next/__tests__ because we don't
    currently have a JS test runner wired into CI for this concern."""

    def _jobs_ts_source(self) -> str:
        path = _REPO_ROOT / "frontend-next" / "src" / "lib" / "api" / "jobs.ts"
        return path.read_text(encoding="utf-8")

    def test_create_job_submits_degraded_delivery_with_report(self):
        source = self._jobs_ts_source()
        assert "on_budget_exhausted: 'degraded_delivery_with_report'" in source, (
            "frontend-next/src/lib/api/jobs.ts MUST hardcode "
            "on_budget_exhausted: 'degraded_delivery_with_report' in the "
            "smart createJob payload. Anything else either bypasses the "
            "validator (404 errors for users) or re-exposes broken "
            "fail_and_refund settlement."
        )

    def test_create_job_does_NOT_submit_fail_and_refund(self):
        source = self._jobs_ts_source()
        # Any literal use of fail_and_refund as a VALUE (not just a
        # comment) would slip past the validator if validator is later
        # loosened. Pin that no such literal exists in the submission
        # payload. Comments mentioning fail_and_refund for documentation
        # are fine — we only ban actual string-value literals.
        # Crude but effective: search for the exact value-position pattern.
        forbidden_patterns = (
            "on_budget_exhausted: 'fail_and_refund'",
            'on_budget_exhausted: "fail_and_refund"',
            "on_budget_exhausted:'fail_and_refund'",
        )
        for pat in forbidden_patterns:
            assert pat not in source, (
                f"frontend jobs.ts must NOT submit {pat!r}. "
                f"Use 'degraded_delivery_with_report' until fail_and_refund "
                f"settlement is implemented (see rebaseline §3.3)."
            )


# ─────────────────────────────────────────────────────────────────────
# 3. Frontend Smart card MUST NOT offer user choice for on_budget_exhausted
# ─────────────────────────────────────────────────────────────────────


class TestFrontendSmartCardNoUserChoice:
    """Smart UX is 'pick smart card → fully automatic'. No on_budget_exhausted
    picker, dropdown, or radio. If we ever add one, we MUST also implement
    the settlement path for any non-degraded choice."""

    def test_translation_form_smart_card_has_no_on_budget_exhausted_picker(self):
        path = (
            _REPO_ROOT / "frontend-next" / "src" / "components"
            / "workspace" / "TranslationForm.tsx"
        )
        source = path.read_text(encoding="utf-8")
        # Negative checks — none of these UI primitives should reference
        # on_budget_exhausted inside the Smart card area.
        forbidden = (
            'name="on_budget_exhausted"',
            "id='on_budget_exhausted'",
            'id="on_budget_exhausted"',
        )
        for pat in forbidden:
            assert pat not in source, (
                f"TranslationForm must NOT expose a user picker for "
                f"on_budget_exhausted ({pat!r} found). Smart is fully "
                f"automatic — adding a choice means also implementing "
                f"settlement for every selectable value."
            )
