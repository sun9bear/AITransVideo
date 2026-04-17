"""One-off backfill: populate jobs.project_dir in Gateway DB from Job API JSON store.

Background (2026-04-17 incident):
    gateway/job_intercept.py:452 writes `project_dir=job_data.get("project_dir")`
    at job creation. At that moment the pipeline hasn't assigned a project_dir
    yet, so the column is always written as NULL. List_jobs sync now mirrors
    project_dir on subsequent polls (B-fix in the same commit), but existing
    rows — 55 at the time of the incident on US — stay NULL until a list_jobs
    call happens. The "生成视频" button fails with 404 "项目目录不存在" for all
    of them in the meantime.

This script walks every NULL-project_dir row, fetches the job detail from the
Job API (which reads its JSON store, the source of truth for project_dir),
and backfills the Gateway DB column in one transaction.

Usage (inside gateway container):
    python -m backfill_project_dir [--dry-run]

Exit code 0 on success, 1 if backfill partially failed.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

import httpx
from sqlalchemy import select

from config import settings
from database import async_session, init_db
from models import Job


async def _fetch_upstream_job(client: httpx.AsyncClient, job_id: str) -> dict | None:
    """GET {job_api_upstream}/jobs/{job_id}; return parsed dict or None on any failure."""
    url = f"{settings.job_api_upstream.rstrip('/')}/jobs/{job_id}"
    try:
        resp = await client.get(url, timeout=10.0)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception as exc:
        print(f"  ! fetch {job_id} failed: {exc}", flush=True)
        return None


async def backfill(dry_run: bool = False) -> int:
    init_db()
    async with async_session() as db:
        result = await db.execute(select(Job).where(Job.project_dir.is_(None)))
        null_rows = list(result.scalars().all())
        print(f"Found {len(null_rows)} jobs with NULL project_dir", flush=True)

        if not null_rows:
            return 0

        updated = 0
        not_in_upstream = 0
        upstream_also_null = 0

        async with httpx.AsyncClient() as client:
            for db_job in null_rows:
                upstream_job = await _fetch_upstream_job(client, db_job.job_id)
                if upstream_job is None:
                    not_in_upstream += 1
                    continue

                upstream_pd = upstream_job.get("project_dir")
                if not upstream_pd:
                    upstream_also_null += 1
                    continue

                if dry_run:
                    print(f"  [dry-run] would set {db_job.job_id}.project_dir = {upstream_pd}", flush=True)
                else:
                    db_job.project_dir = upstream_pd
                updated += 1

        if not dry_run and updated > 0:
            await db.commit()
            print(f"Committed {updated} row updates", flush=True)

        print(
            f"Summary: updated={updated} "
            f"not_in_upstream={not_in_upstream} "
            f"upstream_also_null={upstream_also_null}",
            flush=True,
        )

        # Exit nonzero if some rows couldn't be resolved — user can inspect.
        return 0 if (not_in_upstream + upstream_also_null) == 0 else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print proposed updates without committing.",
    )
    args = parser.parse_args()

    sys.exit(asyncio.run(backfill(dry_run=args.dry_run)))


if __name__ == "__main__":
    main()
