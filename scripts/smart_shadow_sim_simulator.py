"""Smart Shadow Simulator (P1) — read fact sheets + Studio artifacts (read-only),
emit per-job decisions + report. NO production lifecycle hooks. NO paid API calls.

Quick usage:
  python scripts/smart_shadow_sim_simulator.py \
    --facts D:/Claude/temp/smart_shadow_eval/prod_full/facts.jsonl \
    --projects-root D:/Claude/AIVideoTrans_Codex_web_mvp/.codex_tmp/us_fetch/extracted/opt/aivideotrans/data/projects \
    --out-dir D:/Claude/temp/smart_shadow_sim/local_smoke \
    --limit 3

See docs/plans/2026-05-06-smart-shadow-sim-design.md.
"""
from __future__ import annotations
import argparse
import datetime
import json
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


def _load_facts(facts_path: Path) -> list[dict]:
    if not facts_path.is_file():
        return []
    out = []
    for line in facts_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _build_per_job_report(fact: dict, decisions: list[dict]) -> dict:
    """Phase A3 scaffold: most fields TBD in Phase B."""
    return {
        "schema_version": SCHEMA_VERSION,
        "job_id": fact["job_id"],
        "smart_eligibility": "unevaluable",
        "stage_decisions_count": 0,
        "stage_decisions_match": 0,
        "segment_decisions_count": 0,
        "segment_decisions_match": 0,
        "smart_more_aggressive_count": 0,
        "smart_less_aggressive_count": 0,
        "orthogonal_count": 0,
        "stages_unevaluable": [],
        "thresholds_used": {},
        "warnings": [],
    }


def _resolve_project_dir(projects_root: Path | None, fact: dict) -> Path | None:
    """Locate <projects_root>/<project_id>/job_<bare_id>/ or None."""
    if not projects_root or not projects_root.is_dir():
        return None
    project_id = fact.get("project_id")
    job_id = fact.get("job_id", "")
    if not project_id or not job_id:
        return None
    bare = job_id.removeprefix("job_") if job_id.startswith("job_") else job_id
    candidate = projects_root / project_id / f"job_{bare}"
    return candidate if candidate.is_dir() else None


def _load_editor_segments(project_dir: Path | None) -> list[dict]:
    """Read editor/segments.json (preferred) or translation/segments.json (fallback)."""
    if not project_dir:
        return []
    for rel in ("editor/segments.json", "translation/segments.json"):
        p = project_dir / rel
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
            except (OSError, json.JSONDecodeError):
                continue
    return []


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Smart shadow simulator (P1, read-only, offline)."
    )
    parser.add_argument("--facts", required=True,
                        help="Path to facts.jsonl produced by P0 evaluator collector.")
    parser.add_argument("--projects-root", required=False,
                        help="Optional. Project artifacts root (read-only).")
    parser.add_argument("--out-dir", required=True,
                        help="Simulator output dir. Per-job sidecars go under <out-dir>/<job_id>/.")
    parser.add_argument("--main-speaker-threshold", type=float, default=0.10)
    parser.add_argument("--clone-min-seconds-soft", type=int, default=8)
    parser.add_argument("--clone-min-seconds-preferred", type=int, default=10)
    parser.add_argument("--limit", type=int, default=None,
                        help="Optional. Only simulate first N facts (for smoke).")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    facts_path = Path(args.facts)
    out_dir = Path(args.out_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"ERROR: out_dir not writable: {exc}", file=sys.stderr)
        return 2

    run_id = _make_run_id()
    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    facts = _load_facts(facts_path)
    if args.limit is not None:
        facts = facts[: args.limit]

    jobs_simulated = 0
    errors: list[dict] = []

    for fact in facts:
        job_id = fact.get("job_id")
        if not job_id:
            continue
        try:
            job_dir = out_dir / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            projects_root_path = (Path(args.projects_root) if args.projects_root else None)
            project_dir = _resolve_project_dir(projects_root_path, fact)
            segments = _load_editor_segments(project_dir)
            decisions: list[dict] = []  # Phase B will populate
            # Phase A3: write empty decisions.jsonl + scaffold report
            (job_dir / "smart_shadow_decisions.jsonl").write_text(
                "\n".join(json.dumps(d, ensure_ascii=False) for d in decisions),
                encoding="utf-8",
            )
            report = _build_per_job_report(fact, decisions)
            (job_dir / "smart_shadow_report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            jobs_simulated += 1
        except Exception as exc:
            errors.append({
                "job_id": job_id,
                "error_type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            })

    summary = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "args": vars(args),
        "is_complete_run": True,
        "scan_stats": {
            "facts_loaded": len(facts),
            "jobs_simulated": jobs_simulated,
        },
        "errors": errors,
        "git_sha": _git_sha(),
        "hostname": socket.gethostname(),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return 0 if jobs_simulated > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
