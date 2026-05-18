"""Unified project_dir safety guard for pan executors.

CodeX 2026-05-18 P0: previously backup_executor had its own helper that
no-op'd when ``AIVIDEOTRANS_PROJECTS_DIR`` was unset, and residue_cleanup
had no guard at all. Design doc requires reusing
``gateway/project_cleanup._is_safe_project_dir`` (the same helper TTL
cleanup uses) with ``DEFAULT_SAFE_PROJECT_ROOTS``, so the destructive-path
contract is identical across every pan code path.

This module is the single import-from point for that helper across
backup_executor / restore_executor / residue_cleanup. Don't reinvent.
"""
from __future__ import annotations

import os
from pathlib import Path


def safe_project_roots() -> tuple[Path, ...]:
    """Compose the trusted project_dir roots.

    Production: ``DEFAULT_SAFE_PROJECT_ROOTS`` from ``project_cleanup``
    (the same /opt/aivideotrans/{data,app}/projects whitelist TTL cleanup
    uses).

    Tests / custom deployments: ``AIVIDEOTRANS_PROJECTS_DIR`` (when set
    and non-empty) is PREPENDED to the default tuple so tests can use
    ``tmp_path`` as a safe root without disabling production defaults.

    Returns: tuple of ``Path`` objects, never empty.
    """
    from project_cleanup import DEFAULT_SAFE_PROJECT_ROOTS

    env_root = (os.environ.get('AIVIDEOTRANS_PROJECTS_DIR') or '').strip()
    if env_root:
        return (Path(env_root),) + DEFAULT_SAFE_PROJECT_ROOTS
    return DEFAULT_SAFE_PROJECT_ROOTS


def verify_project_dir_safe(project_dir: Path) -> None:
    """Raise RuntimeError if ``project_dir`` is NOT a strict descendant
    of any safe root.

    Wraps ``project_cleanup._is_safe_project_dir`` so callers don't need
    to remember the safe_roots argument shape. The helper rejects:

      - empty / root filesystem paths
      - the safe-root path itself (never wipe the whole projects/ dir)
      - paths that traverse outside via ``..`` or symlinks
      - paths that fail resolve()

    Used by backup_executor (pre-COMMIT step e), restore_executor (before
    os.replace into project_dir), and residue_cleanup (before rmtree).
    """
    from project_cleanup import _is_safe_project_dir

    roots = safe_project_roots()
    if not _is_safe_project_dir(project_dir, safe_roots=roots):
        raise RuntimeError(
            f"refuse to operate on project_dir {str(project_dir)!r}: "
            f"not under any safe root. "
            f"Allowed roots: {', '.join(str(r) for r in roots)}"
        )
