"""APF2c-2 local temp upload storage health helper.

Pure, fail-closed helper that future backend wiring can call to compute
``IntakeConfig.temp_storage_available`` from a caller-provided temp
upload directory ``Path``.

Design source of truth:
``docs/plans/2026-06-02-apf2c-backend-adapter-boundary.md``.

The helper is intentionally minimal and stdlib-only:

* it never imports Gateway / frontend / DB / Redis / network / preview
  media / clone provider / pricing / payment / counter store modules;
* it does not read ``.env`` or any production secret;
* it does not touch ``anonymous_preview_backend_adapter.py`` or
  ``anonymous_preview_intake.py``;
* the only filesystem operations are ``Path.exists`` / ``Path.is_dir``
  on the caller-provided directory and writing then deleting a single
  probe file inside that directory;
* it never creates missing directories or parent directories;
* it never recursively deletes anything and never deletes the
  caller-provided directory itself.

The helper always fail-closes: any unexpected exception or missing /
invalid input collapses to ``StorageHealthResult(available=False, ...)``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


DEFAULT_PROBE_FILENAME_PREFIX = "aivt_storage_health_"


@dataclass(frozen=True)
class StorageHealthResult:
    """Result of a single temp upload storage health probe.

    ``available`` is the sole boolean a future backend wiring will feed
    into ``IntakeConfig.temp_storage_available``. ``reason`` is a short
    human-readable string suitable for logging / fail-closed diagnostics
    and must never carry secrets or full filesystem paths from the
    caller.
    """

    available: bool
    reason: str


def _write_probe(probe_path: Path) -> None:
    """Create an empty probe file. Isolated to make tests deterministic."""

    probe_path.write_bytes(b"")


def _remove_probe(probe_path: Path) -> None:
    """Remove the previously created probe file. Isolated for tests."""

    probe_path.unlink()


def check_temp_upload_storage(
    path: Optional[Path],
    *,
    probe_filename_prefix: str = DEFAULT_PROBE_FILENAME_PREFIX,
) -> StorageHealthResult:
    """Probe a caller-provided temp upload directory for write health.

    Returns ``StorageHealthResult(available=True, ...)`` only when:

    * ``path`` is not ``None``;
    * ``path.exists()`` is True;
    * ``path.is_dir()`` is True;
    * a single small probe file with the configured prefix could be
      created inside ``path`` and then removed.

    Any other outcome — missing config, missing directory, non-directory
    path, write failure, delete failure, or an unexpected exception —
    fail-closes to ``available=False``. This function never raises to
    its caller.
    """

    if path is None:
        return StorageHealthResult(
            available=False,
            reason="temp upload directory path is None (fail closed)",
        )

    try:
        if not path.exists():
            return StorageHealthResult(
                available=False,
                reason="temp upload directory does not exist (fail closed)",
            )
        if not path.is_dir():
            return StorageHealthResult(
                available=False,
                reason="temp upload path is not a directory (fail closed)",
            )
    except Exception as exc:  # noqa: BLE001 — fail closed on any error
        return StorageHealthResult(
            available=False,
            reason=(
                "temp upload directory inspection raised "
                f"{type(exc).__name__} (fail closed)"
            ),
        )

    probe_name = f"{probe_filename_prefix}{uuid.uuid4().hex}.tmp"
    probe_path = path / probe_name

    # Defense-in-depth: a caller-provided ``probe_filename_prefix`` that
    # carries path components (``../``, ``subdir/``, an absolute path,
    # a Windows drive-letter prefix) would make ``path / probe_name``
    # resolve outside the caller-provided temp directory, so a probe
    # write/delete could land on an unrelated file. We reject any prefix
    # whose joined path is not a direct child of ``path`` (no
    # ``resolve()`` so we never follow symlinks; the comparison is purely
    # lexical). The result is a stable, low-sensitivity fail-closed
    # reason that never echoes the raw prefix or any filesystem path
    # (PR #22 external review P2, discussion_r3345886353).
    if probe_path.parent != path:
        return StorageHealthResult(
            available=False,
            reason=(
                "probe filename prefix escapes temp directory (fail closed)"
            ),
        )

    try:
        _write_probe(probe_path)
    except Exception as exc:  # noqa: BLE001 — fail closed on any error
        return StorageHealthResult(
            available=False,
            reason=(
                "probe write failed with "
                f"{type(exc).__name__} (fail closed)"
            ),
        )

    try:
        _remove_probe(probe_path)
    except Exception as exc:  # noqa: BLE001 — fail closed on any error
        return StorageHealthResult(
            available=False,
            reason=(
                "probe delete failed with "
                f"{type(exc).__name__} (fail closed)"
            ),
        )

    return StorageHealthResult(
        available=True,
        reason="probe write+delete succeeded",
    )


__all__ = [
    "DEFAULT_PROBE_FILENAME_PREFIX",
    "StorageHealthResult",
    "check_temp_upload_storage",
]
