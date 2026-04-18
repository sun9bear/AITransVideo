"""Shared input validators for job-scoped endpoints.

D36 deep defense: every endpoint that accepts a ``segment_id`` from the
path or body MUST validate it against ``SEGMENT_ID_RE`` before any
filesystem or DB access. Without this the ``editor/editing/`` /
``editor/tts_segments/{sid}.wav`` path joins would be vulnerable to
traversal (``../../etc/...``) from authenticated users.
"""

from __future__ import annotations

import re

__all__ = [
    "SEGMENT_ID_RE",
    "validate_segment_id",
    "validate_commit_strategy",
]

# Path-safe + reasonable length. Existing segment IDs in the codebase are
# ``seg_001`` / ``seg_042`` style; we also allow all-digits / mixed for
# forward compatibility. Uppercase forbidden so fs lookups are deterministic
# on case-insensitive Windows dev boxes.
SEGMENT_ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")


def validate_segment_id(segment_id: str) -> str:
    """Return the segment_id unchanged if valid; raise ValueError otherwise.

    Uses a strict allowlist (lowercase alnum + underscore, 1-64 chars) so
    that path traversal attempts (``..`` / ``/`` / ``\\``) and overly long
    pathological inputs are rejected before they hit the filesystem.
    """
    if not isinstance(segment_id, str):
        raise ValueError(f"segment_id must be a string, got {type(segment_id).__name__}")
    if not SEGMENT_ID_RE.match(segment_id):
        raise ValueError(
            f"invalid segment_id format: {segment_id!r}; must match "
            f"^[a-z0-9_]{{1,64}}$ (lowercase alnum + underscore only)"
        )
    return segment_id


_SUPPORTED_COMMIT_STRATEGIES = frozenset({"overwrite", "copy_as_new"})


def validate_commit_strategy(strategy: str) -> str:
    """Return the strategy unchanged if valid; raise ValueError otherwise.

    Single source of truth for the commit-strategy allowlist so frontend
    contract + backend dispatch agree.
    """
    if not isinstance(strategy, str):
        raise ValueError(f"strategy must be a string, got {type(strategy).__name__}")
    if strategy not in _SUPPORTED_COMMIT_STRATEGIES:
        raise ValueError(
            f"unsupported commit strategy: {strategy!r}; "
            f"must be one of {sorted(_SUPPORTED_COMMIT_STRATEGIES)}"
        )
    return strategy
