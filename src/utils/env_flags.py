"""Small helpers for opt-in feature flags.

Feature flags in this project are intentionally conservative: an unset value is
the same as ``False`` unless a caller explicitly passes a different default.
"""

from __future__ import annotations

import os

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return default


__all__ = ["env_flag"]
