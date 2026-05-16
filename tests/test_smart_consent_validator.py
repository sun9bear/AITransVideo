"""SmartConsent validator tests (Codex 第四十轮 P1.1).

Per master plan §5.3 + Codex 40 finding: smart submissions must carry
a complete consent payload that locks user's product-side choices
(auto-clone consent, fixed price acceptance, budget-exhaustion
policy). Before this validator, frontend sent only 2 fields and
Gateway / Job API passed any dict through. Result: third-party
clients could submit incomplete consent and the system would still
charge Smart price + run Smart flow.

Schema (6 required fields, master plan §5.3):
  - auto_voice_clone: bool
  - auto_retranslate: bool
  - auto_retts: bool
  - auto_multimodal_verification: bool
  - no_extra_charge_without_confirmation: bool
  - on_budget_exhausted: "degraded_delivery_with_report" | "fail_and_refund"

Note: ``fixed_rate_credits_per_minute`` is NOT a consent field — it's
derivable from the pricing snapshot. Excluded from validator.

Codex 40 P1.2 cross-coupling: ``on_budget_exhausted="fail_and_refund"``
drives ``capture_actual_cost_capped_at_studio_price`` settlement, which
is currently a STUB in gateway/credits_service.py. Validator rejects
that policy until the implementation lands (defense-in-depth — the
settle dispatcher hard-gate is the second layer).
"""
from __future__ import annotations

import sys
from pathlib import Path


_REPO = Path(__file__).resolve().parents[1]
_GATEWAY = _REPO / "gateway"
if str(_GATEWAY) not in sys.path:
    sys.path.insert(0, str(_GATEWAY))


# ===========================================================================
# Cycle 1 — happy path: full valid payload
# ===========================================================================


_VALID_PAYLOAD = {
    "auto_voice_clone": True,
    "auto_retranslate": False,
    "auto_retts": True,
    "auto_multimodal_verification": False,
    "no_extra_charge_without_confirmation": True,
    "on_budget_exhausted": "degraded_delivery_with_report",
}


class TestSmartConsentValidatorHappyPath:

    def test_full_valid_payload_passes(self):
        from smart_consent import validate_smart_consent

        consent, error = validate_smart_consent(_VALID_PAYLOAD)
        assert error is None
        assert consent is not None
        # Fields preserved verbatim
        assert consent.auto_voice_clone is True
        assert consent.auto_retranslate is False
        assert consent.auto_retts is True
        assert consent.auto_multimodal_verification is False
        assert consent.no_extra_charge_without_confirmation is True
        assert consent.on_budget_exhausted == "degraded_delivery_with_report"

    def test_extra_fields_ignored_for_forward_compat(self):
        """Adding fields later shouldn't break older clients."""
        from smart_consent import validate_smart_consent

        payload = dict(_VALID_PAYLOAD)
        payload["future_field"] = "some value"
        payload["fixed_rate_credits_per_minute"] = 100  # accepted but unused
        consent, error = validate_smart_consent(payload)
        assert error is None
        assert consent is not None

    def test_to_dict_returns_canonical_six_fields(self):
        """Serialization for JobRecord persistence must produce exactly
        the 6 documented fields (no extras leaking in / out)."""
        from smart_consent import validate_smart_consent

        consent, _ = validate_smart_consent(_VALID_PAYLOAD)
        d = consent.to_dict()
        assert set(d.keys()) == {
            "auto_voice_clone",
            "auto_retranslate",
            "auto_retts",
            "auto_multimodal_verification",
            "no_extra_charge_without_confirmation",
            "on_budget_exhausted",
        }


# ===========================================================================
# Cycle 2 — required fields enforcement
# ===========================================================================


class TestSmartConsentValidatorMissingFields:

    def test_missing_auto_voice_clone_rejected(self):
        from smart_consent import validate_smart_consent

        payload = dict(_VALID_PAYLOAD)
        del payload["auto_voice_clone"]
        consent, error = validate_smart_consent(payload)
        assert consent is None
        assert error is not None
        assert "auto_voice_clone" in error

    def test_missing_on_budget_exhausted_rejected(self):
        from smart_consent import validate_smart_consent

        payload = dict(_VALID_PAYLOAD)
        del payload["on_budget_exhausted"]
        consent, error = validate_smart_consent(payload)
        assert consent is None
        assert error is not None
        assert "on_budget_exhausted" in error

    def test_each_required_bool_field_individually(self):
        """All 5 bool fields are required — verify each rejects when
        missing (table-driven test)."""
        from smart_consent import validate_smart_consent

        for field in (
            "auto_voice_clone",
            "auto_retranslate",
            "auto_retts",
            "auto_multimodal_verification",
            "no_extra_charge_without_confirmation",
        ):
            payload = dict(_VALID_PAYLOAD)
            del payload[field]
            consent, error = validate_smart_consent(payload)
            assert consent is None, f"{field} missing should reject"
            assert field in error, f"{field} missing must be in error msg"

    def test_empty_dict_rejected(self):
        from smart_consent import validate_smart_consent

        consent, error = validate_smart_consent({})
        assert consent is None
        assert error is not None

    def test_none_rejected(self):
        from smart_consent import validate_smart_consent

        consent, error = validate_smart_consent(None)
        assert consent is None
        assert error is not None


# ===========================================================================
# Cycle 3 — type / enum constraint enforcement
# ===========================================================================


class TestSmartConsentValidatorTypeChecks:

    def test_bool_field_with_string_value_rejected(self):
        from smart_consent import validate_smart_consent

        payload = dict(_VALID_PAYLOAD)
        payload["auto_voice_clone"] = "true"  # string, not bool
        consent, error = validate_smart_consent(payload)
        assert consent is None
        assert "auto_voice_clone" in error

    def test_bool_field_with_int_value_rejected(self):
        from smart_consent import validate_smart_consent

        payload = dict(_VALID_PAYLOAD)
        payload["auto_voice_clone"] = 1  # int, not bool
        consent, error = validate_smart_consent(payload)
        assert consent is None

    def test_on_budget_exhausted_invalid_enum_rejected(self):
        from smart_consent import validate_smart_consent

        payload = dict(_VALID_PAYLOAD)
        payload["on_budget_exhausted"] = "some_unknown_policy"
        consent, error = validate_smart_consent(payload)
        assert consent is None
        assert "on_budget_exhausted" in error

    def test_on_budget_exhausted_must_be_string(self):
        from smart_consent import validate_smart_consent

        payload = dict(_VALID_PAYLOAD)
        payload["on_budget_exhausted"] = True
        consent, error = validate_smart_consent(payload)
        assert consent is None


# ===========================================================================
# Cycle 4 — fail_and_refund rejected until partial-capture implemented
# ===========================================================================


class TestSmartConsentValidatorPolicyGate:
    """Codex 40 P1.2: ``on_budget_exhausted=fail_and_refund`` drives the
    ``capture_actual_cost_capped_at_studio_price`` settle policy, which
    has STUB ``refund_captured_voice_clone`` / ``partial_capture_actual_cost``
    in gateway/credits_service.py. Until those are implemented, accept
    only ``degraded_delivery_with_report``.

    Defense-in-depth: also hard-gated in _settle_smart_job_credit_ledger
    (Fix 2). But rejecting at the consent boundary gives better UX
    (user sees the limit at job creation time, not silently degraded
    at settle).
    """

    def test_fail_and_refund_rejected_with_implementation_message(self):
        from smart_consent import validate_smart_consent

        payload = dict(_VALID_PAYLOAD)
        payload["on_budget_exhausted"] = "fail_and_refund"
        consent, error = validate_smart_consent(payload)
        assert consent is None
        assert error is not None
        # Error should hint at why (so admin / dev can diagnose)
        assert "fail_and_refund" in error or "not_implemented" in error

    def test_degraded_delivery_with_report_accepted(self):
        from smart_consent import validate_smart_consent

        payload = dict(_VALID_PAYLOAD)
        payload["on_budget_exhausted"] = "degraded_delivery_with_report"
        consent, error = validate_smart_consent(payload)
        assert error is None
        assert consent is not None
