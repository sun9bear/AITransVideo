"""Pan orphan cleanup (Phase 8 §T8.2).

Plan 2026-05-13 §10. Weekly Saturday 04:00 BJT (= 20:00 UTC Fri) cron
runs 3 passes:

  Pass A — Baidu Pan remote orphans
    List remote files under /apps/AIVideoTrans/backups/.
    Compute pan_orphans = remote_files - PG.backup_records.remote_path
                          (filter status IN uploaded/restoring/restored).
    For each orphan: safety guard + baidu_pan_client.delete.

  Pass B — R2 residue from archived jobs
    SELECT archived jobs with non-null r2_artifacts.
    For each r2_key: r2_client.delete_object → remove key from JSONB.
    Best-effort; failures continue. Empty list eventually clears
    Job.r2_artifacts to NULL via the post-commit finalize path.

  Pass C — pan_oauth_states GC
    DELETE FROM pan_oauth_states WHERE expires_at < now()

3 passes run serially. Pass-level exceptions are caught and reported
in the returned stats so one pass failing doesn't block the others.

## Safety guards

- Pass A delete: remote path must prefix `/apps/AIVideoTrans/backups/`
  (defense against a bad list response leaking unrelated paths).
- All passes accept `client_factory` / `r2_delete_fn` injection for tests.
- `dry_run=True` collects + reports but performs no deletes.
"""
from __future__ import annotations

import logging
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import and_, delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from models import BackupRecord, Job, PanCredentials, PanOauthState

from pan.baidu_pan_client import BaiduPanClient
from pan.token_crypto import decrypt_token


logger = logging.getLogger(__name__)

REMOTE_PATH_PREFIX = '/apps/AIVideoTrans/backups/'


def _default_client_factory() -> BaiduPanClient:
    from config import settings
    return BaiduPanClient(
        appkey=settings.baidu_pan_appkey,
        appsecret=settings.baidu_pan_appsecret,
    )


def _default_r2_delete(r2_key: str) -> None:
    from config import settings
    from storage.r2_client import _get_client
    _get_client().delete_object(
        Bucket=settings.r2_artifacts_bucket, Key=r2_key,
    )


async def run_orphan_cleanup_tick(
    engine: AsyncEngine,
    *,
    client_factory: Callable[[], Any] | None = None,
    r2_delete_fn: Callable[[str], None] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """One weekly orphan cleanup tick. Returns:
      {'pass_a': {'orphans': [str], 'deleted': int, 'errors': [str]},
       'pass_b': {'jobs_processed': int, 'keys_deleted': int,
                  'errors': [{'job_id', 'key', 'error'}]},
       'pass_c': {'states_deleted': int},
       'dry_run': bool}
    """
    factory = client_factory or _default_client_factory
    r2_delete = r2_delete_fn or _default_r2_delete

    stats: dict[str, Any] = {
        'pass_a': {'orphans': [], 'deleted': 0, 'errors': []},
        'pass_b': {'jobs_processed': 0, 'keys_deleted': 0, 'errors': []},
        'pass_c': {'states_deleted': 0},
        'dry_run': dry_run,
    }

    # ---- Pass A: pan remote orphans ----
    try:
        await _pass_a_pan_orphans(engine, factory, dry_run, stats['pass_a'])
    except Exception as exc:  # noqa: BLE001
        logger.warning("pan_orphan_cleanup Pass A failed: %s", exc)
        stats['pass_a']['errors'].append(f"pass_a_top_level: {exc}"[:300])

    # ---- Pass B: R2 residue ----
    try:
        await _pass_b_r2_residue(engine, r2_delete, dry_run, stats['pass_b'])
    except Exception as exc:  # noqa: BLE001
        logger.warning("pan_orphan_cleanup Pass B failed: %s", exc)
        stats['pass_b']['errors'].append({
            'job_id': '_top_level_', 'key': '_top_level_',
            'error': str(exc)[:300],
        })

    # ---- Pass C: oauth_states GC ----
    try:
        await _pass_c_oauth_states(engine, dry_run, stats['pass_c'])
    except Exception as exc:  # noqa: BLE001
        logger.warning("pan_orphan_cleanup Pass C failed: %s", exc)

    return stats


async def _pass_a_pan_orphans(
    engine: AsyncEngine,
    client_factory: Callable[[], Any],
    dry_run: bool,
    stats: dict[str, Any],
) -> None:
    """List pan remote files, cross-check with PG backup_records, delete
    orphans (= on pan but no matching active BackupRecord row)."""
    Session = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False,
    )
    # Need an active PanCredentials row to call Baidu — use the first
    # one available (admin user). For multi-admin systems, this would
    # iterate per-admin; MVP is single-admin.
    async with Session() as db:
        cred = (await db.execute(
            select(
                PanCredentials.access_token_encrypted, PanCredentials.user_id,
            ).where(PanCredentials.status == 'active').limit(1)
        )).one_or_none()
        if cred is None:
            logger.info(
                "pan_orphan_cleanup Pass A: no active credentials, skip"
            )
            return

        access_token = decrypt_token(cred.access_token_encrypted)
        # Snapshot remote_path of all active backup_records — anything
        # NOT in this set is an orphan.
        db_paths = set((await db.execute(
            select(BackupRecord.remote_path).where(
                BackupRecord.status.in_(['uploaded', 'restoring', 'restored']),
                BackupRecord.remote_path != '',
            )
        )).scalars().all())

    client = client_factory()
    try:
        remote_entries = client.list(REMOTE_PATH_PREFIX, access_token=access_token)
    except Exception as exc:  # noqa: BLE001
        stats['errors'].append(f"list_remote: {exc}"[:300])
        return

    remote_paths = [e.get('path', '') for e in remote_entries if e.get('path')]
    orphans = [p for p in remote_paths if p not in db_paths]
    stats['orphans'] = orphans

    if dry_run or not orphans:
        return

    for path in orphans:
        # Safety: refuse paths outside the trusted prefix.
        if not path.startswith(REMOTE_PATH_PREFIX):
            stats['errors'].append(f"refuse unsafe path {path!r}")
            logger.warning(
                "pan_orphan_cleanup Pass A: REFUSE unsafe orphan path %r "
                "(does not prefix %s)", path, REMOTE_PATH_PREFIX,
            )
            continue
        try:
            client.delete(path, access_token=access_token)
            stats['deleted'] += 1
            logger.info("pan_orphan_cleanup Pass A: deleted orphan %s", path)
        except Exception as exc:  # noqa: BLE001
            stats['errors'].append(
                f"delete_failed path={path!r} err={exc}"[:300]
            )
            logger.warning(
                "pan_orphan_cleanup Pass A: delete failed %s: %s", path, exc,
            )


async def _pass_b_r2_residue(
    engine: AsyncEngine,
    r2_delete: Callable[[str], None],
    dry_run: bool,
    stats: dict[str, Any],
) -> None:
    """Archived jobs that still have r2_artifacts (residue from
    backup_executor step k failure)."""
    Session = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False,
    )
    async with Session() as db:
        rows = (await db.execute(
            select(Job.user_id, Job.job_id, Job.r2_artifacts).where(
                Job.status == 'archived',
                Job.r2_artifacts.isnot(None),
            )
        )).all()

    for row in rows:
        stats['jobs_processed'] += 1
        artifacts = row.r2_artifacts or []
        if isinstance(artifacts, str):
            import json as _json
            try:
                artifacts = _json.loads(artifacts)
            except Exception:  # noqa: BLE001
                artifacts = []

        if dry_run:
            stats['keys_deleted'] += sum(
                1 for a in artifacts if isinstance(a, dict) and a.get('r2_key')
            )
            continue

        # Delete each key; collect the successes for the JSONB rewrite.
        successfully_deleted: set[str] = set()
        for a in artifacts:
            if not isinstance(a, dict):
                continue
            r2_key = a.get('r2_key')
            if not r2_key:
                continue
            try:
                r2_delete(r2_key)
                successfully_deleted.add(r2_key)
                stats['keys_deleted'] += 1
            except Exception as exc:  # noqa: BLE001
                stats['errors'].append({
                    'job_id': row.job_id, 'key': r2_key,
                    'error': str(exc)[:200],
                })

        if not successfully_deleted:
            continue

        # Rewrite r2_artifacts JSONB minus the successfully-deleted keys.
        remaining = [
            a for a in artifacts
            if not (isinstance(a, dict) and a.get('r2_key') in successfully_deleted)
        ]
        async with Session() as db:
            await db.execute(
                update(Job)
                .where(Job.user_id == row.user_id, Job.job_id == row.job_id)
                .values(r2_artifacts=(remaining if remaining else None))
            )
            await db.commit()


async def _pass_c_oauth_states(
    engine: AsyncEngine,
    dry_run: bool,
    stats: dict[str, Any],
) -> None:
    """GC pan_oauth_states with expires_at < now()."""
    Session = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False,
    )
    now = datetime.now(timezone.utc)

    async with Session() as db:
        if dry_run:
            # Count without deleting.
            from sqlalchemy import func as _func
            count = (await db.execute(
                select(_func.count())
                .select_from(PanOauthState)
                .where(PanOauthState.expires_at < now)
            )).scalar_one()
            stats['states_deleted'] = count
            return

        result = await db.execute(
            delete(PanOauthState).where(PanOauthState.expires_at < now)
        )
        await db.commit()
        stats['states_deleted'] = result.rowcount or 0
