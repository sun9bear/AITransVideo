"""Shared JSON serialization helpers (non-atomic write).

All functions are pure aside from ``write_json`` which touches the filesystem.
They consolidate the ``_to_jsonable`` / ``_write_json`` micro-helpers that were
duplicated byte-for-byte across ``services/assemblyai/transcriber.py`` and
``services/gemini/translator.py`` (DRY audit finding DRY-03;
see docs/plans/code-quality-tasks/TU-06-shared-helpers.md).

For *atomic* writes (used in draft_writer, manifest_writer, config_loader,
jobs/store) see src/utils/atomic_io.py — those are NOT in scope here and have
different durability semantics (temp file + fsync + atomic rename).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

__all__ = ["to_jsonable", "write_json"]


def to_jsonable(value: Any) -> Any:
    """Recursively convert *value* to a JSON-serializable type."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if hasattr(value, "__dict__"):
        return {key: to_jsonable(item) for key, item in vars(value).items() if not key.startswith("_")}
    return str(value)


def write_json(path: Path, payload: Any) -> None:
    """Write *payload* as pretty-printed UTF-8 JSON to *path* (non-atomic)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
