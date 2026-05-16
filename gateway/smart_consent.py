"""Smart MVP §5.3: SmartConsent contract validator (Codex 第四十轮 P1.1).

Master plan §5.3 mandates a 6-field consent payload for every smart
job submission. Before this module, the contract was loose:
gateway/job_intercept.py and src/services/jobs/api.py both passed
the raw dict through, so frontend / third-party clients could submit
partial consent and still get Smart pricing + Smart pipeline routing.

Codex 第四十轮 P1.1: lock the contract. Gateway rejects
``service_mode=smart`` submissions whose ``smart_consent`` is missing,
malformed, or carries unsupported policy values.

Schema (master plan §5.3, MVP form):
  - auto_voice_clone: bool
  - auto_retranslate: bool
  - auto_retts: bool
  - auto_multimodal_verification: bool
  - no_extra_charge_without_confirmation: bool
  - on_budget_exhausted: "degraded_delivery_with_report" | "fail_and_refund"

NOT a consent field:
  - fixed_rate_credits_per_minute — derivable from pricing snapshot

Codex 40 P1.2 coupling: ``on_budget_exhausted="fail_and_refund"``
drives the ``capture_actual_cost_capped_at_studio_price`` settle
policy whose ``refund_captured_voice_clone`` and
``partial_capture_actual_cost`` are STUB in credits_service.py. Until
those land, validator rejects ``fail_and_refund`` at the consent
boundary (defense in depth — the settle dispatcher also hard-gates).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# Allowed values for the on_budget_exhausted policy enum.
# ``fail_and_refund`` is documented in master plan but its settle path
# (capture_actual_cost_capped_at_studio_price) has STUB implementations
# in gateway/credits_service.py. Reject until implemented.
_ALLOWED_BUDGET_POLICIES = frozenset({
    "degraded_delivery_with_report",
    # "fail_and_refund",  # blocked — Codex 40 P1.2; see _BLOCKED_BUDGET_POLICIES
})
_BLOCKED_BUDGET_POLICIES = frozenset({
    "fail_and_refund",
})

_REQUIRED_BOOL_FIELDS = (
    "auto_voice_clone",
    "auto_retranslate",
    "auto_retts",
    "auto_multimodal_verification",
    "no_extra_charge_without_confirmation",
)


@dataclass(frozen=True)
class SmartConsent:
    """Validated SmartConsent payload. Caller persists via
    ``to_dict()`` so JobRecord.smart_consent reflects exactly the
    6 documented fields (no extras leaked through)."""

    auto_voice_clone: bool
    auto_retranslate: bool
    auto_retts: bool
    auto_multimodal_verification: bool
    no_extra_charge_without_confirmation: bool
    on_budget_exhausted: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "auto_voice_clone": self.auto_voice_clone,
            "auto_retranslate": self.auto_retranslate,
            "auto_retts": self.auto_retts,
            "auto_multimodal_verification": self.auto_multimodal_verification,
            "no_extra_charge_without_confirmation": (
                self.no_extra_charge_without_confirmation
            ),
            "on_budget_exhausted": self.on_budget_exhausted,
        }


def validate_smart_consent(
    raw: Any,
) -> tuple[SmartConsent | None, str | None]:
    """Validate a raw smart_consent payload against the §5.3 schema.

    Returns:
        ``(SmartConsent, None)`` on success.
        ``(None, error_message)`` on any failure; ``error_message`` is
        a short human-readable string suitable for inclusion in the
        Gateway 400 response (NOT internationalized).

    Defensive:
        - Non-dict input → reject
        - Missing required field → reject naming the field
        - Wrong type → reject naming the field
        - Invalid / blocked enum value → reject naming the field +
          the offending value
        - Extra unknown fields → silently ignored (forward compat)
    """
    if not isinstance(raw, dict):
        return None, (
            "smart_consent must be a JSON object; got "
            f"{type(raw).__name__}"
        )

    # 5 bool fields
    for field in _REQUIRED_BOOL_FIELDS:
        if field not in raw:
            return None, f"smart_consent missing required field: {field}"
        value = raw[field]
        # Strictly bool — reject int (0/1) and str ("true"/"false")
        # so accidental coercion doesn't slip through.
        if not isinstance(value, bool):
            return None, (
                f"smart_consent.{field} must be a boolean; got "
                f"{type(value).__name__}"
            )

    # on_budget_exhausted enum
    if "on_budget_exhausted" not in raw:
        return None, "smart_consent missing required field: on_budget_exhausted"
    policy = raw["on_budget_exhausted"]
    if not isinstance(policy, str):
        return None, (
            "smart_consent.on_budget_exhausted must be a string; got "
            f"{type(policy).__name__}"
        )
    if policy in _BLOCKED_BUDGET_POLICIES:
        return None, (
            f"smart_consent.on_budget_exhausted='{policy}' not_implemented_yet — "
            "partial-capture / clone-refund settle path is STUB in "
            "credits_service.py. Use 'degraded_delivery_with_report'."
        )
    if policy not in _ALLOWED_BUDGET_POLICIES:
        return None, (
            f"smart_consent.on_budget_exhausted='{policy}' is not one of "
            f"{sorted(_ALLOWED_BUDGET_POLICIES)}"
        )

    return SmartConsent(
        auto_voice_clone=bool(raw["auto_voice_clone"]),
        auto_retranslate=bool(raw["auto_retranslate"]),
        auto_retts=bool(raw["auto_retts"]),
        auto_multimodal_verification=bool(
            raw["auto_multimodal_verification"]
        ),
        no_extra_charge_without_confirmation=bool(
            raw["no_extra_charge_without_confirmation"]
        ),
        on_budget_exhausted=str(raw["on_budget_exhausted"]),
    ), None


__all__ = ["SmartConsent", "validate_smart_consent"]
