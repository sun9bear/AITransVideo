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
        return {
            "error_code": self.error_code,
            "message": self.message,
            "retryable": self.retryable,
            "detail": self.detail,
            "user_action": self.user_action,
        }


# Well-known stable error codes (extend as needed; NEVER rename existing ones).
RETRYABLE_CODES: frozenset[str] = frozenset(
    {
        "db_write_failed",
        "upstream_timeout",
        "worker_unavailable",
    }
)

NON_RETRYABLE_CODES: frozenset[str] = frozenset(
    {
        "job_not_found",
        "job_not_owned",
        "credit_insufficient",
        "voice_clone_consent_required",
        "plan_upgrade_required",
        "invalid_request",
    }
)
