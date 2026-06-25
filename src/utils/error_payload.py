"""Canonical API error payload shape for backend HTTP responses.

Gateway side (gateway/job_intercept.py) produces JSON from ``_error_response()``.
Job API side (src/services/jobs/api.py) uses similar patterns. This module is the
single source of truth for the error *shape* used in documentation and tests.

NOTE: Gateway must NOT import this module, to avoid pulling ``src/services``
(and its heavy transitive deps, e.g. pydub) into the gateway Python process.
Gateway keeps its own ``_error_response()`` helper; this module only mirrors the
shape (see TU-06 invariant 3).

Scope note (TU-06): this module intentionally defines ONLY the payload shape. A
retryable/non-retryable *code registry* was deliberately dropped — gateway emits
a large, evolving set of ``error_code`` values and no classifier consumes such a
registry yet, so maintaining a hand-curated copy here would be speculative
(YAGNI) and would drift from the gateway source of truth. The runtime source of
truth for codes is ``gateway/job_intercept.py``; add a classifier here only when
a real consumer needs one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["ErrorPayload"]


@dataclass(frozen=True)
class ErrorPayload:
    """Canonical error response shape.

    ``error_code`` + ``message`` are always serialized. ``retryable`` /
    ``detail`` / ``user_action`` are optional and OMITTED from ``to_dict()`` /
    the wire when left at their default — so an unset field is never serialized
    as a misleading ``false`` / ``{}`` / ``""``. This mirrors the gateway
    ``_error_response`` convention and matters because the frontend branches on
    key *presence* (e.g. ``'detail' in payload``; see lib/api/client.ts).

    Fields
    ------
    error_code : str
        Stable machine-readable English identifier (snake_case), matching the
        codes gateway actually emits. Frontend and tests key on this — must
        never change once shipped (e.g. ``"insufficient_credits"``,
        ``"job_not_found"``).
    message : str
        Human-readable Chinese description shown in the UI.
    retryable : bool
        Whether the client should offer a retry action. Omitted when False.
    detail : dict[str, Any]
        Optional structured diagnostic (no secrets, no PII). Omitted when empty.
    user_action : str
        Suggested next step for the user (Chinese, one sentence). Omitted when "".
    """

    error_code: str
    message: str
    retryable: bool = False
    detail: dict[str, Any] = field(default_factory=dict)
    user_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error_code": self.error_code, "message": self.message}
        if self.retryable:
            payload["retryable"] = self.retryable
        if self.detail:
            payload["detail"] = self.detail
        if self.user_action:
            payload["user_action"] = self.user_action
        return payload
