"""One-time migration: import existing jobs/*.json into PostgreSQL.

Usage: python migrate_jobs.py [--jobs-dir /path/to/jobs] [--admin-email admin@example.com]

All imported jobs are assigned to the admin user.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from database import async_session, engine, init_db
from models import Base, Job, User


async def migrate(jobs_dir: str, admin_email: str) -> None:
    # T3 fix: standalone scripts must explicitly init the DB — with the lazy
    # init pattern in database.py, `engine` and `async_session` are proxies
    # that raise RuntimeError unless init_db() has been called. In the main
    # app this runs in the FastAPI lifespan; here we do it manually.
    init_db()

    # Ensure tables exist
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as db:
        # Find or create admin user
        result = await db.execute(select(User).where(User.email == admin_email))
        admin = result.scalar_one_or_none()
        if admin is None:
            print(f"Admin user {admin_email} not found. Please register first.")
            return
        print(f"Admin user: {admin.email} ({admin.id})")

        jobs_path = Path(jobs_dir)
        if not jobs_path.exists():
            print(f"Jobs directory not found: {jobs_path}")
            return

        imported = 0
        skipped = 0
        for json_file in sorted(jobs_path.glob("*.json")):
            if json_file.name.endswith(".events.jsonl"):
                continue
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                job_id = data.get("job_id")
                if not job_id:
                    continue

                # Check if already imported
                existing = await db.execute(select(Job).where(Job.job_id == job_id))
                if existing.scalar_one_or_none() is not None:
                    skipped += 1
                    continue

                job = Job(
                    job_id=job_id,
                    user_id=admin.id,
                    source_type=data.get("source_type", "youtube_url"),
                    source_ref=data.get("youtube_url", ""),
                    title=data.get("title", ""),
                    speakers=data.get("speakers", "auto"),
                    status=data.get("status", "unknown"),
                    current_stage=data.get("current_stage"),
                    project_dir=data.get("project_dir"),
                    review_gate=data.get("review_gate"),
                    error_summary=data.get("error_summary"),
                )
                db.add(job)
                imported += 1
                print(f"  Imported: {job_id} ({data.get('status', '?')})")
            except Exception as e:
                print(f"  Error processing {json_file.name}: {e}")

        await db.commit()
        print(f"\nDone. Imported: {imported}, Skipped: {skipped}")

    await engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs-dir", default="/opt/aivideotrans/data/jobs")
    parser.add_argument("--admin-email", default="admin@aivideotrans.com")
    args = parser.parse_args()
    asyncio.run(migrate(args.jobs_dir, args.admin_email))


if __name__ == "__main__":
    main()
