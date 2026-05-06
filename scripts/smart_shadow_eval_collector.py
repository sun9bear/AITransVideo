"""Smart Shadow Evaluator collector — stdlib-only read-only scanner.

Quick usage:
  # Local smoke (against .codex_tmp samples):
  python scripts/smart_shadow_eval_collector.py \\
    --projects-root D:/Claude/AIVideoTrans_Codex_web_mvp/.codex_tmp/us_fetch/extracted/opt/aivideotrans/data/projects \\
    --jobs-root D:/Claude/AIVideoTrans_Codex_web_mvp/.codex_tmp/us_fetch/extracted/opt/aivideotrans/data/jobs \\
    --out-dir D:/Claude/temp/smart_shadow_eval/local_smoke --limit 3

  # Production (on 154 host):
  python3 scripts/smart_shadow_eval_collector.py \\
    --projects-root /opt/aivideotrans/data/projects \\
    --jobs-root /opt/aivideotrans/data/jobs \\
    --out-dir /tmp/smart_shadow_eval/<run_id>

See docs/plans/2026-05-06-smart-shadow-evaluator-design.md.
"""
from __future__ import annotations
import argparse
import datetime
import json
import os
import socket
import subprocess as sp
import sys
import traceback
from pathlib import Path


SCHEMA_VERSION = 1


def _git_sha() -> str:
    try:
        out = sp.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=sp.DEVNULL, text=True, timeout=2,
        )
        return out.strip()
    except Exception:
        return "unknown"


def _make_run_id() -> str:
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H-%MZ")
    return f"{ts}-{socket.gethostname()}-{_git_sha()}"


def _resolve_out_dir(args, run_id: str) -> Path:
    """Receive pre-computed run_id to avoid drift across multiple calls."""
    if args.out_dir:
        return Path(args.out_dir)
    return Path("/tmp") / "smart_shadow_eval" / run_id


def _iter_job_record_paths(jobs_root: Path):
    """Yield job_*.json files (not .events.jsonl)."""
    for p in sorted(jobs_root.glob("job_*.json")):
        if p.name.endswith(".events.jsonl"):
            continue
        yield p


def _atomic_write_summary(out_dir: Path, summary: dict) -> None:
    """Write summary.json via .tmp + rename to avoid partial reads."""
    tmp = out_dir / "summary.json.tmp"
    tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.rename(out_dir / "summary.json")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Smart shadow eval collector (read-only)."
    )
    parser.add_argument(
        "--projects-root",
        default=os.environ.get(
            "AIVIDEOTRANS_PROJECTS_DIR",
            "/opt/aivideotrans/data/projects",
        ),
    )
    parser.add_argument(
        "--jobs-root",
        default=os.environ.get(
            "AIVIDEOTRANS_JOBS_DIR",
            "/opt/aivideotrans/data/jobs",
        ),
    )
    parser.add_argument("--out-dir", required=False)
    parser.add_argument("--since", default="2026-01-01")
    parser.add_argument("--until", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--include-running", action="store_true")
    parser.add_argument("--scan-from", choices=["jobs", "projects"], default="jobs")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    # Pre-flight (exit 2 path — no summary written yet)
    jobs_root = Path(args.jobs_root)
    projects_root = Path(args.projects_root)
    if not jobs_root.is_dir() or not projects_root.is_dir():
        print(f"ERROR: jobs_root or projects_root not a directory", file=sys.stderr)
        return 2

    # Single run_id used everywhere (out_dir, summary, fact sheets)
    run_id = _make_run_id()
    out_dir = _resolve_out_dir(args, run_id)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"ERROR: out_dir not writable: {exc}", file=sys.stderr)
        return 2

    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    facts_tmp = out_dir / "facts.jsonl.tmp"
    inventory_tmp = out_dir / "inventory.jsonl.tmp"
    facts_count = 0
    inventory_count = 0
    errors: list[dict] = []
    skipped_status = 0
    skipped_date = 0
    skipped_identity = 0
    fatal_exception: BaseException | None = None

    # Wrap main scan + write in try/except to guarantee a degraded summary
    # is written for ANY uncaught exception (BLOCKER #1 fix).
    try:
        with facts_tmp.open("w", encoding="utf-8") as ff, \
             inventory_tmp.open("w", encoding="utf-8") as fi:
            if args.limit is not None:
                paths = list(_iter_job_record_paths(jobs_root))[: args.limit]
            else:
                paths = list(_iter_job_record_paths(jobs_root))
            for record_path in paths:
                try:
                    rec = json.loads(record_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    errors.append({
                        "job_id": record_path.stem,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "traceback": traceback.format_exc(),
                    })
                    continue

                job_id = rec.get("job_id")
                status = rec.get("status")
                created_at = rec.get("created_at")
                if not job_id or not created_at or not status:
                    skipped_identity += 1
                    continue

                if not args.include_running and status != "succeeded":
                    skipped_status += 1
                    continue

                # Date filter (later)
                inv_entry = {
                    "schema_version": SCHEMA_VERSION,
                    "job_id": job_id,
                    "project_id": rec.get("project_id"),
                    "status": status,
                    "created_at": created_at,
                    "service_mode": rec.get("service_mode"),
                    "had_post_edit": (rec.get("edit_generation", 0) or 0) > 0
                        or rec.get("copy_of_job_id") is not None,
                }
                fi.write(json.dumps(inv_entry, ensure_ascii=False) + "\n")
                inventory_count += 1
    except BaseException as exc:
        fatal_exception = exc
        errors.append({
            "job_id": None,
            "error_type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        })

    # Build summary FIRST (small, less likely to fail than facts rename)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "args": vars(args),
        "is_complete_run": fatal_exception is None,
        "scan_stats": {
            "jobs_inventoried": inventory_count,
            "jobs_factsheeted": facts_count,
            "skipped_for_status_filter": skipped_status,
            "skipped_for_date_filter": skipped_date,
            "skipped_for_missing_identity": skipped_identity,
            "orphaned_project_dir_count": 0,  # filled in Task A3
        },
        "errors": errors,
        "git_sha": _git_sha(),
        "hostname": socket.gethostname(),
    }

    # Always try to write summary (even on fatal exception).
    try:
        _atomic_write_summary(out_dir, summary)
    except OSError as exc:
        # Last resort — print to stderr so caller knows something terminal happened.
        print(f"ERROR: could not write summary.json: {exc}", file=sys.stderr)

    # Only rename facts/inventory IF the run completed (preserves spec §3.7
    # invariant: "facts.jsonl 存在 = run 完整").
    if fatal_exception is None:
        facts_tmp.rename(out_dir / "facts.jsonl")
        inventory_tmp.rename(out_dir / "inventory.jsonl")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
