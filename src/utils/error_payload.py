"""Canonical API error payload shape for backend HTTP responses.

Gateway side (gateway/job_intercept.py) produces JSON from ``_error_response()``.
Job API side (src/services/jobs/api.py) uses similar patterns.
Both MUST match this schema — this module is the single source of truth for
documentation and test assertions.

NOTE: Gateway must NOT import this module, to avoid pulling ``src/services``
(and its heavy transitive deps, e.g. pydub) into the gateway Python process.
Gateway keeps its own ``_error_response()`` helper; this module defines the
contract *shape* for testing and documentation only (see TU-06 invariant 3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["ErrorPayload", "RETRYABLE_CODES", "NON_RETRYABLE_CODES"]


@dataclass(frozen=True)
class ErrorPayload:
    """Canonical error response shape.

    Fields
    ------
    error_code : str
        Stable machine-readable English identifier (snake_case). Frontend and
        tests key on this — must never change once shipped. Examples:
        ``"job_not_found"``, ``"credit_insufficient"``,
        ``"voice_clone_consent_required"``.
    message : str
        Human-readable Chinese description shown in the UI.
    retryable : bool
        Whether the client should offer a retry action.
    detail : dict[str, Any]
        Optional structured diagnostic (no secrets, no PII). Default: empty.
        Omitted from ``to_dict()`` / the wire when empty, matching the gateway
        ``_error_response`` convention — the frontend branches on
        ``'detail' in payload`` (see lib/api/client.ts), so an empty ``{}``
        must never be serialized.
    user_action : str
        Suggested next step for the user (Chinese, one sentence). Empty string
        means no action needed.
    """

    error_code: str
    message: str
    retryable: bool = False
    detail: dict[str, Any] = field(default_factory=dict)
    user_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        # ``detail`` omitted when empty to mirror the gateway wire convention
        # (gateway/job_intercept._error_response) and keep the contract a faithful
        # model of what clients actually receive. retryable/user_action always present.
        payload: dict[str, Any] = {
            "error_code": self.error_code,
            "message": self.message,
            "retryable": self.retryable,
            "user_action": self.user_action,
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload


# Curated subset of the error codes actually emitted by gateway/job_intercept.py —
# that module is the RUNTIME source of truth. These are verified-real codes (NOT
# speculative); extend as classifiers/tests need them, but NEVER rename a shipped
# code (e.g. ``insufficient_credits`` is keyed on by the frontend smart-preview
# reason union — see gateway ``_insufficient_credits_response``).
RETRYABLE_CODES: frozenset[str] = frozenset(
    {
        "rate_limited",
        "timeout",
        "upstream_error",
        "storage_error",
        "gate_unavailable",
    }
)

NON_RETRYABLE_CODES: frozenset[str] = frozenset(
    {
        "insufficient_credits",
        "job_not_found",
        "unauthorized",
        "invalid_body",
        "feature_not_available",
        "anonymous_preview_disabled",
    }
)
