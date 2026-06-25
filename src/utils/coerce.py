"""Shared type-coercion and text-normalization helpers.

All functions are pure (no I/O, no external calls). They consolidate the
``_normalize_optional_text`` / ``_coerce_bool`` / ``_coerce_int`` /
``_coerce_optional_int`` micro-helpers that were duplicated across many
modules (DRY audit finding; see docs/plans/code-quality-tasks/TU-06-...).
"""
from __future__ import annotations

__all__ = [
    "normalize_optional_text",
    "coerce_bool",
    "coerce_int",
    "coerce_optional_int",
]


def normalize_optional_text(value: object) -> str | None:
    """Strip *value* to ``str``; return ``None`` if empty or ``None``."""
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def coerce_bool(value: object, *, default: bool) -> bool:
    """Coerce a loose value to ``bool``.

    Truthy strings: ``"1"``, ``"true"``, ``"yes"``, ``"on"``.
    Falsy  strings: ``"0"``, ``"false"``, ``"no"``, ``"off"``.
    Anything else (unknown string / unparseable) returns *default*.
    """
    if isinstance(value, bool):
        return value
    normalized = normalize_optional_text(value)
    if normalized is None:
        return default
    lowered = normalized.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return default


def coerce_optional_int(value: object) -> int | None:
    """Return ``int(value)`` or ``None`` on failure."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def coerce_int(value: object, *, default: int) -> int:
    """Return ``int(value)`` or *default* on failure."""
    coerced = coerce_optional_int(value)
    return default if coerced is None else coerced
