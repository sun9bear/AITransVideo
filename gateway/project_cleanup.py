"""Gateway-side 7d project TTL cleanup.

Runs alongside the Job API's legacy cleanup (``src/services/web_ui/cleanup.py``)
but owns the **authoritative DB transition**: it flips expired jobs to
``status='purged'`` and, when the project_dir is still on disk, removes
it with a hard path-whitelist guard.

Why two cleaners? Historical:

- The Job API side (``cleanup.py``) predates this. It reads / writes
  the JSON store (``jobs/*.json``) and has been running for weeks. It's
  the reason the US host's disk top-20 shows only recent projects —
  Apr 8-era projects have already been rm'd on disk.
- Gateway DB was never in that loop, so the DB accumulated "ghost"
  rows whose ``project_dir`` no longer exists (~25 ghosts observed
  2026-04-21). This module closes that gap.

Design principles:

1. **Path whitelist** — never ``rmtree`` anything outside the registered
   safe roots. Protects against the 2026-04-20 ``/s`` regression class
   where a poisoned ``project_dir`` could have cascaded into the rest
   of the host.
2. **Ghost-friendly** — even when ``project_dir`` is gone (Job API
   cleanup got there first), we still flip status so the UI stops
   listing it. This is the primary contract.
3. **Active-status safe** — queued / running / waiting_for_review /
   editing are never touched, even if ``expires_at`` has somehow slipped
   past (clock skew, manual SQL edit). Wait for the state machine to
   land on a terminal state first.
4. **Legacy fallback** — NULL ``expires_at`` on rows from before
   migration 015's backfill caught up fall back to ``created_at + 7d``.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Job

logger = logging.getLogger(__name__)

RETENTION_DAYS = 7

# Statuses eligible for purge. Must NEVER include any active state — the
# state machine promotes those through terminal outcomes, and only after
# that does the retention window even start conceptually.
PURGEABLE_STATUSES = frozenset({"succeeded", "failed", "cancelled"})

# Paths we're willing to ``shutil.rmtree`` under. The Gateway container
# mounts host ``/opt/aivideotrans/data/projects`` RW at the same path
# and also exposes the app-side ``/opt/aivideotrans/app/projects`` as
# a legacy alias. Real project_dirs sit at depth ≥2 (``{root}/{user_id}/{job_id}``).
DEFAULT_SAFE_PROJECT_ROOTS: tuple[Path, ...] = (
    Path("/opt/aivideotrans/data/projects"),
    Path("/opt/aivideotrans/app/projects"),
)


def _is_safe_project_dir(
    path: Path,
    *,
    safe_roots: tuple[Path, ...] | tuple[Path] | list[Path] = DEFAULT_SAFE_PROJECT_ROOTS,
) -> bool:
    """True iff ``path`` resolves to a strict descendant of one of the
    registered safe roots.

    Refuses:
    - empty / root filesystem paths
    - paths that resolve to a safe root exactly (never blast the whole
      projects/ dir)
    - paths that traverse outside a safe root via ``..`` or symlinks
    - malformed paths that fail resolve()
    """
    if not path or str(path) in ("", "/"):
        return False
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    if resolved == Path("/") or str(resolved) == "":
        return False
    for root in safe_roots:
        try:
            resolved_root = Path(root).resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if resolved == resolved_root:
            return False  # exact root — never delete
        try:
            resolved.relative_to(resolved_root)
        except ValueError:
            continue
        return True
    return False


def _as_aware_utc(dt: datetime | None) -> datetime | None:
    """Return ``dt`` as a timezone-aware UTC datetime, or None if ``dt`` is
    None. Production (Postgres TIMESTAMPTZ) gives us aware values; SQLite
    in unit tests strips tzinfo. This shim lets the comparison work on
    both without lying about the stored semantics."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _is_expired(job: Job, now: datetime) -> bool:
    """Has this job's retention window elapsed?

    Priority: explicit ``expires_at`` > legacy ``created_at + 7d``.
    """
    expires_at = _as_aware_utc(job.expires_at)
    if expires_at is not None:
        return expires_at < now
    # Legacy row pre-migration-015 backfill — fall back to the same
    # ``created_at + 7d`` rule Job API cleanup.py uses.
    created_at = _as_aware_utc(job.created_at)
    if created_at is None:
        return False  # defensive: can't determine age
    return created_at + timedelta(days=RETENTION_DAYS) < now


async def cleanup_expired_projects(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    safe_roots: tuple[Path, ...] | None = None,
) -> int:
    """Sweep expired terminal jobs.

    - For each candidate: if ``project_dir`` exists AND passes the
      whitelist, ``shutil.rmtree`` it. If it fails the whitelist, log a
      warning and leave the disk alone.
    - Unconditionally flip ``status`` to ``'purged'`` (unless ``dry_run``).
    - Never commits when the caller passes ``dry_run=True``.

    Returns the count of jobs that were transitioned (or *would* have
    been, under dry_run) to ``'purged'``.
    """
    now = now or datetime.now(timezone.utc)
    effective_roots = safe_roots if safe_roots is not None else DEFAULT_SAFE_PROJECT_ROOTS

    legacy_cutoff = now - timedelta(days=RETENTION_DAYS)
    # One SQL pass to narrow down candidates. The DB predicate is an
    # over-approximation (it catches anything that might be expired per
    # the fast path); the python-side ``_is_expired`` is the precise
    # decision including the NULL-fallback.
    result = await db.execute(
        select(Job).where(
            Job.status.in_(tuple(PURGEABLE_STATUSES)),
            or_(
                Job.expires_at.is_not(None) & (Job.expires_at < now),
                Job.expires_at.is_(None) & (Job.created_at < legacy_cutoff),
            ),
        )
    )
    candidates = list(result.scalars().all())

    purged = 0
    for job in candidates:
        if not _is_expired(job, now):
            continue  # race / precision catch — skip the row silently

        project_dir_str = job.project_dir
        if project_dir_str:
            project_dir = Path(project_dir_str)
            if _is_safe_project_dir(project_dir, safe_roots=effective_roots):
                if not dry_run and project_dir.is_dir():
                    try:
                        shutil.rmtree(project_dir, ignore_errors=False)
                    except OSError as exc:
                        # Disk / perm issue — log and continue so we
                        # still flip status. Next cleanup pass will
                        # re-try removal if the dir re-appears.
                        logger.warning(
                            "project cleanup: rmtree failed for %s: %s",
                            project_dir, exc,
                        )
            else:
                # Unsafe path — refuse to touch disk. Still transition
                # status so the DB ghost doesn't linger. This is the
                # regression-safe path for any future ``/s``-style
                # pollution: DB heals, disk stays untouched.
                logger.warning(
                    "project cleanup: refusing to purge unsafe path %r "
                    "for job %s — flipping status only",
                    project_dir_str, job.job_id,
                )
        # If project_dir_str is empty / None, the row is a pure DB ghost
        # already — still flip status so the UI can filter it out.

        if not dry_run:
            job.status = "purged"
            job.updated_at = now
        purged += 1

    if purged > 0 and not dry_run:
        await db.commit()
    return purged
