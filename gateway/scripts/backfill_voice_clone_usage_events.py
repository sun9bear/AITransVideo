from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


GATEWAY_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = GATEWAY_ROOT.parent
for candidate in (
    GATEWAY_ROOT,
    REPO_ROOT / "src",
    Path("/opt/aivideotrans/app/src"),
):
    if candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from database import async_session, init_db  # noqa: E402
from models import CreditsLedger, Job  # noqa: E402
from sqlalchemy import select  # noqa: E402
from services.usage_meter import UsageMeter  # noqa: E402


VOICE_CLONE_REASON_CODE = "voice_clone_capture"
DEFAULT_PROVIDER_COST_RMB = 9.9


@dataclass
class BackfillCandidate:
    job: Job
    credits_captured: int
    first_capture_at: datetime | None
    last_capture_at: datetime | None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill historical MiniMax voice clone provider-cost usage events "
            "from credits_ledger voice_clone_capture rows."
        )
    )
    parser.add_argument(
        "--job-id",
        action="append",
        default=[],
        help="Specific job_id to backfill. Can be passed multiple times.",
    )
    parser.add_argument(
        "--job-ids",
        default="",
        help="Comma-separated job_id list. Useful for one-off admin page rows.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Look back this many days when no explicit job id is provided.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Maximum candidate jobs to scan.",
    )
    parser.add_argument(
        "--reason-code",
        default=VOICE_CLONE_REASON_CODE,
        help="CreditsLedger reason_code for successful voice clone captures.",
    )
    parser.add_argument(
        "--clone-credit-cost",
        type=int,
        default=0,
        help="Credits charged per clone. Defaults to runtime pricing, fallback 600.",
    )
    parser.add_argument(
        "--provider-cost-rmb",
        type=float,
        default=DEFAULT_PROVIDER_COST_RMB,
        help="Provider cost estimate per MiniMax cloned voice.",
    )
    parser.add_argument(
        "--project-dir-map",
        action="append",
        default=[],
        metavar="FROM=TO",
        help=(
            "Rewrite project_dir prefix before writing, e.g. "
            "/opt/aivideotrans/app/projects=/opt/aivideotrans/data/projects. "
            "Can be passed multiple times."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Write backfill events even when the job already has voice_clone usage events.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned writes without touching usage_events or DB snapshot.",
    )
    return parser.parse_args()


def _clone_credit_cost(arg_value: int) -> int:
    if arg_value > 0:
        return arg_value
    try:
        from pricing_runtime import get_runtime_pricing

        value = int(get_runtime_pricing().credits.voice_clone_cost_credits or 0)
        if value > 0:
            return value
    except Exception:
        pass
    return 600  # plan 2026-06-14 §4.2: 500→600（fallback 对齐新 canonical）


def _job_ids(args: argparse.Namespace) -> list[str]:
    values: list[str] = []
    values.extend(args.job_id or [])
    if args.job_ids:
        values.extend(args.job_ids.split(","))
    return [value.strip() for value in values if value and value.strip()]


def _dir_maps(raw_maps: list[str]) -> list[tuple[str, str]]:
    maps: list[tuple[str, str]] = []
    for raw in raw_maps:
        if "=" not in raw:
            raise SystemExit(f"Invalid --project-dir-map value: {raw!r}")
        src, dst = raw.split("=", 1)
        src = src.rstrip("/\\")
        dst = dst.rstrip("/\\")
        if src and dst:
            maps.append((src, dst))
    return maps


def _resolve_project_dir(raw_project_dir: str | None, maps: list[tuple[str, str]]) -> Path | None:
    if not raw_project_dir:
        return None
    raw = str(raw_project_dir)
    candidates = [Path(raw)]
    for src, dst in maps:
        if raw == src or raw.startswith(src + "/") or raw.startswith(src + "\\"):
            candidates.append(Path(dst + raw[len(src):]))

    # Common host-side fallback when DB stores the in-container app path.
    src = "/opt/aivideotrans/app/projects"
    dst = "/opt/aivideotrans/data/projects"
    if raw == src or raw.startswith(src + "/"):
        candidates.append(Path(dst + raw[len(src):]))

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[-1] if candidates else None


async def _load_candidates(
    *,
    args: argparse.Namespace,
    explicit_job_ids: list[str],
) -> list[BackfillCandidate]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(args.days or 7)))
    stmt = (
        select(Job, CreditsLedger)
        .join(CreditsLedger, CreditsLedger.related_job_id == Job.job_id)
        .where(
            CreditsLedger.direction == "capture",
            CreditsLedger.reason_code == args.reason_code,
        )
        .order_by(CreditsLedger.created_at.desc())
        .limit(max(1, int(args.limit or 200)) * 10)
    )
    if explicit_job_ids:
        stmt = stmt.where(Job.job_id.in_(explicit_job_ids))
    else:
        stmt = stmt.where(CreditsLedger.created_at >= cutoff)

    grouped: dict[str, BackfillCandidate] = {}
    async with async_session() as db:
        result = await db.execute(stmt)
        for job, ledger in result.all():
            credits = abs(int(ledger.credits_delta or 0))
            existing = grouped.get(job.job_id)
            if existing is None:
                grouped[job.job_id] = BackfillCandidate(
                    job=job,
                    credits_captured=credits,
                    first_capture_at=ledger.created_at,
                    last_capture_at=ledger.created_at,
                )
                continue
            existing.credits_captured += credits
            if ledger.created_at and (
                existing.first_capture_at is None
                or ledger.created_at < existing.first_capture_at
            ):
                existing.first_capture_at = ledger.created_at
            if ledger.created_at and (
                existing.last_capture_at is None
                or ledger.created_at > existing.last_capture_at
            ):
                existing.last_capture_at = ledger.created_at

    return list(grouped.values())[: max(1, int(args.limit or 200))]


def _existing_voice_clone_events(meter: UsageMeter) -> list[dict[str, Any]]:
    return [event for event in meter.events if event.get("kind") == "voice_clone"]


def _voice_clone_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    calls = 0
    success_calls = 0
    billable_count = 0
    source_audio_seconds = 0.0
    by_provider: dict[str, int] = {}
    for event in events:
        if event.get("kind") != "voice_clone":
            continue
        calls += 1
        provider = str(event.get("provider") or "unknown").strip().lower() or "unknown"
        clone_count = int(event.get("clone_count") or 0)
        if clone_count <= 0 and bool(event.get("success", True)):
            clone_count = 1
        if bool(event.get("success", True)):
            success_calls += 1
        if bool(event.get("billable", True)):
            billable_count += clone_count
            by_provider[provider] = by_provider.get(provider, 0) + clone_count
        try:
            source_audio_seconds += max(0.0, float(event.get("source_audio_seconds") or 0.0))
        except (TypeError, ValueError):
            pass
    return {
        "voice_clone_call_count": calls,
        "voice_clone_success_call_count": success_calls,
        "voice_clone_billable_count": billable_count,
        "voice_clone_count_by_provider": by_provider,
        "voice_clone_source_audio_seconds": round(source_audio_seconds, 3),
    }


def _write_summary_file(meter: UsageMeter, summary: dict[str, Any]) -> None:
    summary_path = getattr(meter, "summary_path", None)
    if summary_path is None:
        return
    path = Path(summary_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _clone_count(credits_captured: int, clone_credit_cost: int) -> int:
    if credits_captured <= 0:
        return 0
    if clone_credit_cost <= 0:
        return 1
    # Historical rows can split one clone across multiple credit buckets. The
    # captured credit total is more reliable than raw ledger row count.
    return max(1, int(round(credits_captured / clone_credit_cost)))


async def _update_snapshot(job_id: str, summary: dict[str, Any]) -> None:
    allowed = {
        "usage_metering_version",
        "usage_events_count",
        "voice_clone_call_count",
        "voice_clone_success_call_count",
        "voice_clone_billable_count",
        "voice_clone_count_by_provider",
        "voice_clone_source_audio_seconds",
    }
    async with async_session() as db:
        result = await db.execute(select(Job).where(Job.job_id == job_id))
        job = result.scalar_one_or_none()
        if job is None:
            return
        snapshot = dict(job.metering_snapshot or {})
        for key in allowed:
            if key in summary:
                snapshot[key] = summary[key]
        job.metering_snapshot = snapshot
        await db.commit()


async def _run() -> int:
    args = _parse_args()
    init_db()

    explicit_job_ids = _job_ids(args)
    maps = _dir_maps(args.project_dir_map)
    clone_credit_cost = _clone_credit_cost(args.clone_credit_cost)
    candidates = await _load_candidates(args=args, explicit_job_ids=explicit_job_ids)

    planned = 0
    written = 0
    skipped = 0
    missing_project_dir = 0

    for candidate in candidates:
        job = candidate.job
        snapshot = job.metering_snapshot if isinstance(job.metering_snapshot, dict) else {}
        project_dir = _resolve_project_dir(job.project_dir or snapshot.get("project_dir"), maps)
        clone_count = _clone_count(candidate.credits_captured, clone_credit_cost)
        title = getattr(job, "display_name", None) or getattr(job, "title", None) or job.job_id

        if clone_count <= 0:
            skipped += 1
            print(f"skip {job.job_id}: no captured clone credits")
            continue
        if project_dir is None:
            missing_project_dir += 1
            print(f"skip {job.job_id}: missing project_dir")
            continue

        meter = UsageMeter(project_dir, job_id=job.job_id)
        existing = _existing_voice_clone_events(meter)
        if existing and not args.force:
            skipped += 1
            print(f"skip {job.job_id}: already has {len(existing)} voice_clone event(s)")
            continue

        planned += clone_count
        print(
            f"{'plan' if args.dry_run else 'write'} {job.job_id}: "
            f"clones={clone_count}, credits={candidate.credits_captured}, "
            f"project_dir={project_dir}, title={title}"
        )
        if args.dry_run:
            continue

        created_at_ms = int(
            (candidate.last_capture_at.timestamp() if candidate.last_capture_at else time.time())
            * 1000
        )
        for index in range(1, clone_count + 1):
            meter.record_event({
                "event_id": f"voice_clone_backfill:{job.job_id}:{args.reason_code}:{index}",
                "kind": "voice_clone",
                "bucket": "voice_clone",
                "provider": "minimax_voice_clone",
                "model": "voice_clone",
                "voice_id": "",
                "speaker_id": "",
                "source_audio_seconds": 0,
                "source_audio_bytes": 0,
                "selected_segment_count": 0,
                "clone_count": 1,
                "billable": True,
                "success": True,
                "error": "",
                "created_at_ms": created_at_ms,
                "backfill": True,
                "backfill_reason": "credits_ledger_voice_clone_capture",
                "credits_captured_total": candidate.credits_captured,
                "clone_credit_cost": clone_credit_cost,
                "estimated_provider_cost_rmb": args.provider_cost_rmb,
                "billing_policy": "minimax_charges_voice_clone_on_first_tts_use",
            })
            written += 1
        summary = meter.write_summary()
        summary.update(_voice_clone_summary(meter.events))
        _write_summary_file(meter, summary)
        await _update_snapshot(job.job_id, summary)

    print(
        "done: "
        f"candidates={len(candidates)}, planned_events={planned}, "
        f"written_events={written}, skipped={skipped}, "
        f"missing_project_dir={missing_project_dir}, dry_run={args.dry_run}"
    )
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
