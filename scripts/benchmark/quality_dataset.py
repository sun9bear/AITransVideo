#!/usr/bin/env python3
"""Build and validate the video translation quality benchmark dataset.

The dataset is intentionally derived from existing job artifacts and gateway
metrics, but the checked-in fixture must not contain source URLs, payment data,
or full raw transcript content. The fixture is for offline replay and
regression analysis of duration alignment, speaker attribution, and cost
observability.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

DATASET_VERSION = "video_translation_quality.v1"

DEFAULT_ANALYSIS_DIR = Path(".codex_tmp/us_fetch/analysis")
DEFAULT_ARTIFACTS_ROOT = Path(".codex_tmp/us_fetch/extracted/opt/aivideotrans/data")
DEFAULT_OUTPUT_DIR = Path("tests/fixtures/benchmark/video_translation_quality")

FORBIDDEN_KEYWORDS = (
    "source_ref",
    "youtube_url",
    "video_url",
    "payment",
    "alipay",
    "wechat",
    "provider_order",
    "trade_no",
    "transaction",
    "phone",
    "email",
)

TEXT_FIELDS = {"source_text", "cn_text", "tts_cn_text", "translated_text", "text"}

SEGMENT_FIELDS = (
    "segment_id",
    "speaker_id",
    "display_name",
    "start_ms",
    "end_ms",
    "target_duration_ms",
    "source_chars",
    "cn_chars",
    "cn_chars_per_target_sec",
    "pre_rewrite_direction",
    "pre_estimate_ms",
    "pre_target_ms",
    "first_pass_duration_ms",
    "first_pass_error_pct_field",
    "first_pass_error_pct_calc",
    "actual_duration_ms",
    "actual_error_pct_calc",
    "alignment_method",
    "rewrite_count",
    "needs_review",
    "tts_provider",
    "dsp_speed_param",
    "target_chars_per_second",
    "pre_tts_rewrite_direction",
    "pre_tts_estimate_ms",
    "pre_tts_target_ms",
    "pre_tts_pre_chars",
    "pre_tts_post_chars",
    "pre_tts_post_tts_first_pass_ms",
    "pre_tts_contradiction",
    "pre_tts_harmful_contradiction",
)

METERING_FIELDS = (
    "status",
    "current_stage",
    "created_at",
    "completed_at",
    "service_mode",
    "tts_provider",
    "tts_model",
    "plan_code_snapshot",
    "role_snapshot",
    "source_duration_seconds",
    "actual_minutes",
    "quota_cost",
    "quota_state",
    "requires_review",
    "voice_clone_enabled",
    "voice_strategy",
    "has_metering_snapshot",
    "snapshot_service_mode",
    "snapshot_tts_provider",
    "snapshot_tts_model",
    "quality_tier",
    "credits_estimated",
    "credits_actual",
    "final_cn_chars",
    "rewrite_triggered",
    "rewrite_count",
    "tts_billed_chars",
    "total_segments",
    "catalog_hit_count",
    "catalog_hit_rate",
    "skip_probe",
    "needs_review_count",
    "needs_review_rate",
    "micro_segment_count",
    "short_segment_count",
    "short_segment_needs_review_count",
    "short_segment_force_dsp_count",
    "first_pass_error_pct_avg",
    "first_pass_error_pct_p50",
    "first_pass_error_pct_p90",
    "first_pass_error_pct_n",
    "glossary_total_terms",
    "glossary_preserved_terms",
    "term_preservation_rate",
    "alignment_method_distribution",
    "speed_param_distribution",
    "segment_segments",
    "segment_rewrite_segments",
    "segment_needs_review",
    "segment_force_dsp",
    "pre_tts_rewrite_count",
    "pre_tts_contradiction_count",
    "pre_tts_contradiction_rate",
    "harmful_pre_tts_contradiction_count",
    "harmful_pre_tts_contradiction_rate",
    "pre_tts_rewrite_events",
)


@dataclass(frozen=True)
class BuildPaths:
    analysis_dir: Path = DEFAULT_ANALYSIS_DIR
    artifacts_root: Path = DEFAULT_ARTIFACTS_ROOT
    output_dir: Path = DEFAULT_OUTPUT_DIR


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def parse_int(value: Any) -> int:
    number = parse_float(value)
    if number is None:
        return 0
    return int(number)


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "t", "yes", "y"}


def stable_hash(text: str, *, length: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_LONG_DIGITS_RE = re.compile(r"\b\d{6,}\b")
_WHITESPACE_RE = re.compile(r"\s+")


def sanitize_text(value: Any, *, max_chars: int = 240) -> dict[str, Any]:
    text = "" if value is None else str(value)
    text = _EMAIL_RE.sub("[email]", text)
    text = _URL_RE.sub("[url]", text)
    text = _LONG_DIGITS_RE.sub("[number]", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    original_chars = len(text)
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return {
        "snippet": text,
        "chars": original_chars,
        "truncated": original_chars > len(text),
    }


def coerce_value(value: Any) -> Any:
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    lower = text.lower()
    if lower in {"true", "false"}:
        return lower == "true"
    number = parse_float(text)
    if number is not None:
        if number.is_integer() and not any(ch in text for ch in ".eE"):
            return int(number)
        return number
    if (text.startswith("{") and text.endswith("}")) or (
        text.startswith("[") and text.endswith("]")
    ):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return text


def load_input_tables(analysis_dir: Path) -> dict[str, list[dict[str, str]]]:
    return {
        "segments": read_csv_rows(analysis_dir / "segment_trace.csv"),
        "speaker_diffs": read_csv_rows(analysis_dir / "speaker_diff_summary.csv"),
        "metering": read_csv_rows(analysis_dir / "job_metering_joined.csv"),
        "ledger": read_csv_rows(analysis_dir / "credits_ledger_joined.csv"),
    }


def find_job_dirs(artifacts_root: Path) -> dict[str, Path]:
    if not artifacts_root.exists():
        return {}
    job_dirs: dict[str, Path] = {}
    for path in artifacts_root.rglob("job_*"):
        if not path.is_dir():
            continue
        existing = job_dirs.get(path.name)
        if existing is None or len(path.parts) < len(existing.parts):
            job_dirs[path.name] = path
    return job_dirs


def is_pre_tts_contradiction(row: dict[str, str]) -> bool:
    direction = str(row.get("pre_rewrite_direction") or "").strip().lower()
    shrink_directions = {"shrink", "overshoot", "too_long"}
    expand_directions = {"expand", "undershoot", "too_short"}
    if direction not in shrink_directions | expand_directions:
        return False
    error = parse_float(row.get("first_pass_error_pct_calc"))
    if error is None:
        error = parse_float(row.get("first_pass_error_pct_field"))
    if error is None:
        return False
    return (direction in shrink_directions and error < -0.05) or (
        direction in expand_directions and error > 0.05
    )


def classify_pre_tts_contradiction(row: dict[str, Any]) -> str | None:
    """Classify a contradiction when pre/post rewrite character counts exist."""
    if not is_pre_tts_contradiction({key: "" if value is None else str(value) for key, value in row.items()}):
        return None
    pre_chars = parse_float(row.get("pre_rewrite_chars"))
    post_chars = parse_float(row.get("post_rewrite_chars"))
    if pre_chars is None or post_chars is None or pre_chars <= 0:
        return "missing_pre_post_chars"

    direction = str(row.get("pre_rewrite_direction") or "").strip().lower()
    shrink_directions = {"shrink", "overshoot", "too_long"}
    expand_directions = {"expand", "undershoot", "too_short"}
    ratio = post_chars / pre_chars
    if direction in shrink_directions and ratio >= 0.98:
        return "llm_direction_not_followed"
    if direction in expand_directions and ratio <= 1.02:
        return "llm_direction_not_followed"
    return "estimator_or_voice_cps_mismatch"


def group_rows(rows: list[dict[str, str]], key: str) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        value = row.get(key)
        if value:
            grouped[value].append(row)
    return dict(grouped)


def compute_job_metrics(
    *,
    segment_rows: list[dict[str, str]],
    speaker_rows: list[dict[str, str]],
    metering_row: dict[str, str] | None,
    artifact_path: Path | None,
) -> dict[str, Any]:
    segment_count = len(segment_rows)
    rewrite_segments = sum(1 for row in segment_rows if parse_int(row.get("rewrite_count")) > 0)
    needs_review_segments = sum(1 for row in segment_rows if parse_bool(row.get("needs_review")))
    force_dsp_segments = sum(
        1 for row in segment_rows if str(row.get("alignment_method") or "").strip() == "force_dsp"
    )
    pre_tts_events = [
        row for row in segment_rows if str(row.get("pre_rewrite_direction") or "").strip()
    ]
    pre_tts_contradictions = [
        row for row in pre_tts_events if is_pre_tts_contradiction(row)
    ]

    durations = [
        parse_float(row.get("target_duration_ms"))
        for row in segment_rows
        if parse_float(row.get("target_duration_ms")) is not None
    ]
    speaker_durations = [
        parse_float(row.get("duration_ms"))
        for row in speaker_rows
        if parse_float(row.get("duration_ms")) is not None
    ]

    provider = None
    model = None
    service_mode = None
    if metering_row:
        provider = metering_row.get("tts_provider") or metering_row.get("snapshot_tts_provider")
        model = metering_row.get("tts_model") or metering_row.get("snapshot_tts_model")
        service_mode = metering_row.get("service_mode") or metering_row.get("snapshot_service_mode")
    if not provider and segment_rows:
        provider = next((row.get("tts_provider") for row in segment_rows if row.get("tts_provider")), None)

    metering_rewrite_count = parse_int(metering_row.get("rewrite_count")) if metering_row else 0
    rewrite_count = max(metering_rewrite_count, sum(parse_int(row.get("rewrite_count")) for row in segment_rows))

    return {
        "artifact_available": artifact_path is not None,
        "segment_count": segment_count,
        "rewrite_segments": rewrite_segments,
        "rewrite_segment_rate": round(rewrite_segments / segment_count, 4) if segment_count else 0.0,
        "rewrite_count": rewrite_count,
        "needs_review_segments": needs_review_segments,
        "needs_review_rate": round(needs_review_segments / segment_count, 4) if segment_count else 0.0,
        "force_dsp_segments": force_dsp_segments,
        "pre_tts_events": len(pre_tts_events),
        "pre_tts_contradictions": len(pre_tts_contradictions),
        "speaker_corrections": len(speaker_rows),
        "speaker_correction_duration_p50_ms": round(median(speaker_durations), 2)
        if speaker_durations
        else None,
        "target_duration_p50_ms": round(median(durations), 2) if durations else None,
        "provider": provider,
        "tts_model": model,
        "service_mode": service_mode,
    }


def build_job_catalog(
    tables: dict[str, list[dict[str, str]]],
    artifact_dirs: dict[str, Path],
) -> dict[str, dict[str, Any]]:
    segments_by_job = group_rows(tables["segments"], "job_id")
    speakers_by_job = group_rows(tables["speaker_diffs"], "job_id")
    metering_by_job = {
        row["job_id"]: row for row in tables["metering"] if row.get("job_id")
    }
    job_ids = sorted(set(segments_by_job) | set(speakers_by_job) | set(metering_by_job) | set(artifact_dirs))

    catalog: dict[str, dict[str, Any]] = {}
    for job_id in job_ids:
        artifact_path = artifact_dirs.get(job_id)
        segment_rows = segments_by_job.get(job_id, [])
        speaker_rows = speakers_by_job.get(job_id, [])
        metering_row = metering_by_job.get(job_id)
        metrics = compute_job_metrics(
            segment_rows=segment_rows,
            speaker_rows=speaker_rows,
            metering_row=metering_row,
            artifact_path=artifact_path,
        )
        catalog[job_id] = {
            "job_id": job_id,
            "segment_rows": segment_rows,
            "speaker_rows": speaker_rows,
            "metering_row": metering_row,
            "artifact_path": artifact_path,
            "metrics": metrics,
        }
    return catalog


def selection_score(entry: dict[str, Any]) -> float:
    metrics = entry["metrics"]
    return (
        metrics["pre_tts_contradictions"] * 50
        + metrics["speaker_corrections"] * 35
        + metrics["rewrite_segments"] * 2
        + metrics["rewrite_count"]
        + metrics["force_dsp_segments"] * 3
        + metrics["needs_review_segments"]
    )


def selection_reasons(entry: dict[str, Any]) -> list[str]:
    metrics = entry["metrics"]
    reasons: list[str] = []
    if metrics["pre_tts_contradictions"]:
        reasons.append("pre_tts_contradiction")
    if metrics["speaker_corrections"]:
        reasons.append("speaker_correction")
    if metrics["rewrite_segment_rate"] >= 0.2 or metrics["rewrite_count"] >= 10:
        reasons.append("high_rewrite")
    if metrics["force_dsp_segments"]:
        reasons.append("force_dsp")
    if metrics["rewrite_segments"] == 0 and metrics["segment_count"]:
        reasons.append("low_rewrite_control")
    provider = metrics.get("provider")
    if provider:
        reasons.append(f"provider:{provider}")
    return reasons or ["coverage"]


def select_jobs(catalog: dict[str, dict[str, Any]], *, max_jobs: int) -> list[str]:
    candidates = [
        entry
        for entry in catalog.values()
        if entry["metrics"]["segment_count"] > 0 or entry["metrics"]["speaker_corrections"] > 0
    ]
    selected: list[str] = []

    def add(entries: list[dict[str, Any]], limit: int) -> None:
        nonlocal selected
        for entry in entries:
            if len(selected) >= max_jobs or len([job for job in selected if job]) >= limit:
                break
            job_id = entry["job_id"]
            if job_id not in selected:
                selected.append(job_id)

    contradiction_jobs = sorted(
        [entry for entry in candidates if entry["metrics"]["pre_tts_contradictions"]],
        key=lambda item: (-item["metrics"]["pre_tts_contradictions"], -selection_score(item), item["job_id"]),
    )
    add(contradiction_jobs, min(max_jobs, 4))

    speaker_jobs = sorted(
        [entry for entry in candidates if entry["metrics"]["speaker_corrections"]],
        key=lambda item: (-item["metrics"]["speaker_corrections"], -selection_score(item), item["job_id"]),
    )
    add(speaker_jobs, min(max_jobs, 7))

    provider_best: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in candidates:
        metrics = entry["metrics"]
        key = (str(metrics.get("provider") or "unknown"), str(metrics.get("tts_model") or "unknown"))
        current = provider_best.get(key)
        if current is None or selection_score(entry) > selection_score(current):
            provider_best[key] = entry
    add(
        sorted(provider_best.values(), key=lambda item: (-selection_score(item), item["job_id"])),
        min(max_jobs, 10),
    )

    controls = sorted(
        [
            entry
            for entry in candidates
            if entry["metrics"]["segment_count"] and entry["metrics"]["rewrite_segments"] == 0
        ],
        key=lambda item: (-item["metrics"]["segment_count"], item["job_id"]),
    )
    add(controls, min(max_jobs, 11))

    fill = sorted(candidates, key=lambda item: (-selection_score(item), item["job_id"]))
    add(fill, max_jobs)

    return selected[:max_jobs]


def clean_output_dir(output_dir: Path) -> None:
    resolved = output_dir.resolve()
    cwd = Path.cwd().resolve()
    allowed = (cwd / "tests" / "fixtures" / "benchmark").resolve()
    suffix = tuple(part.lower() for part in resolved.parts[-4:])
    expected_suffix = ("tests", "fixtures", "benchmark", "video_translation_quality")
    try:
        resolved.relative_to(allowed)
    except ValueError as exc:
        if suffix != expected_suffix:
            raise ValueError(f"Refusing to clean output outside {allowed}: {resolved}") from exc
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)


def sanitize_segment_row(row: dict[str, str]) -> dict[str, Any]:
    result = {field: coerce_value(row.get(field)) for field in SEGMENT_FIELDS if field in row}
    if "pre_tts_contradiction" in row and str(row.get("pre_tts_contradiction") or "").strip():
        result["pre_tts_contradiction"] = parse_bool(row.get("pre_tts_contradiction"))
    else:
        result["pre_tts_contradiction"] = is_pre_tts_contradiction(row)
    if "pre_tts_harmful_contradiction" in row and str(row.get("pre_tts_harmful_contradiction") or "").strip():
        result["pre_tts_harmful_contradiction"] = parse_bool(row.get("pre_tts_harmful_contradiction"))
    return result


def sanitize_speaker_row(row: dict[str, str]) -> dict[str, Any]:
    result = {
        key: coerce_value(value)
        for key, value in row.items()
        if key not in {"user_root", "job_id", "source_text"}
    }
    result["source_text"] = sanitize_text(row.get("source_text"))
    return result


def sanitize_metering_row(row: dict[str, str] | None) -> dict[str, Any]:
    if not row:
        return {}
    return {field: coerce_value(row.get(field)) for field in METERING_FIELDS if field in row}


def summarize_artifacts(job_dir: Path | None) -> dict[str, Any]:
    if job_dir is None or not job_dir.exists():
        return {"available": False, "files": []}
    files: list[dict[str, Any]] = []
    interesting_names = {
        "project_state.json",
        "review_state.json",
        "transcript.json",
        "segments.json",
        "s2_review_audit.json",
        "s2_review_result.json",
        "s2_review_speaker_diff.json",
        "s2_pass1_result.json",
        "s2_pass2_result.json",
        "s2_pass3_result.json",
    }
    for path in sorted(job_dir.rglob("*.json")):
        if path.name not in interesting_names:
            continue
        rel = path.relative_to(job_dir).as_posix()
        entry: dict[str, Any] = {
            "path": rel,
            "size_bytes": path.stat().st_size,
        }
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, OSError):
            payload = None
        if isinstance(payload, dict):
            entry["top_level_keys"] = sorted(str(key) for key in payload.keys())[:40]
            if rel.endswith("s2_review_audit.json"):
                entry["audit_summary"] = {
                    key: payload.get(key)
                    for key in (
                        "enabled",
                        "reviewer",
                        "original_segments",
                        "reviewed_segments",
                        "corrections_applied",
                        "speaker_corrections_applied",
                    )
                    if key in payload
                }
        files.append(entry)
    return {"available": True, "files": files}


def write_job_fixture(
    *,
    output_dir: Path,
    benchmark_id: str,
    entry: dict[str, Any],
    source_index: int,
) -> dict[str, Any]:
    job_dir = output_dir / "jobs" / benchmark_id
    job_dir.mkdir(parents=True, exist_ok=True)

    metrics = entry["metrics"]
    meta = {
        "benchmark_id": benchmark_id,
        "source_job_hash": stable_hash(entry["job_id"]),
        "source_index": source_index,
        "selection_reasons": selection_reasons(entry),
        "metrics": metrics,
    }
    write_json(job_dir / "job_meta.json", meta)

    segments = [sanitize_segment_row(row) for row in entry["segment_rows"]]
    write_json(job_dir / "segments.json", {"segments": segments})

    speaker_corrections = [sanitize_speaker_row(row) for row in entry["speaker_rows"]]
    write_json(job_dir / "speaker_corrections.json", {"corrections": speaker_corrections})

    metering = sanitize_metering_row(entry["metering_row"])
    write_json(job_dir / "metering_snapshot.json", metering)

    pre_tts_events = [
        sanitize_segment_row(row)
        for row in entry["segment_rows"]
        if str(row.get("pre_rewrite_direction") or "").strip()
    ]
    write_json(job_dir / "pre_tts_events.json", {"events": pre_tts_events})

    artifact_summary = summarize_artifacts(entry["artifact_path"])
    write_json(job_dir / "artifact_index.json", artifact_summary)

    return {
        "benchmark_id": benchmark_id,
        "source_job_hash": meta["source_job_hash"],
        "selection_reasons": meta["selection_reasons"],
        "metrics": metrics,
        "files": {
            "meta": f"jobs/{benchmark_id}/job_meta.json",
            "segments": f"jobs/{benchmark_id}/segments.json",
            "speaker_corrections": f"jobs/{benchmark_id}/speaker_corrections.json",
            "metering_snapshot": f"jobs/{benchmark_id}/metering_snapshot.json",
            "pre_tts_events": f"jobs/{benchmark_id}/pre_tts_events.json",
            "artifact_index": f"jobs/{benchmark_id}/artifact_index.json",
        },
    }


def build_quality_dataset(
    *,
    paths: BuildPaths,
    max_jobs: int = 12,
    force: bool = False,
) -> dict[str, Any]:
    if paths.output_dir.exists() and not force:
        raise FileExistsError(f"{paths.output_dir} already exists; pass --force to replace it")
    clean_output_dir(paths.output_dir)

    tables = load_input_tables(paths.analysis_dir)
    artifact_dirs = find_job_dirs(paths.artifacts_root)
    catalog = build_job_catalog(tables, artifact_dirs)
    selected_job_ids = select_jobs(catalog, max_jobs=max_jobs)

    jobs: list[dict[str, Any]] = []
    for index, job_id in enumerate(selected_job_ids, start=1):
        benchmark_id = f"bench_{index:03d}"
        jobs.append(
            write_job_fixture(
                output_dir=paths.output_dir,
                benchmark_id=benchmark_id,
                entry=catalog[job_id],
                source_index=index,
            )
        )

    provider_counts = Counter(
        str(job["metrics"].get("provider") or "unknown") for job in jobs
    )
    manifest = {
        "version": DATASET_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "analysis_dir": paths.analysis_dir.as_posix(),
            "artifacts_root": paths.artifacts_root.as_posix(),
            "job_count_available": len(catalog),
            "job_count_selected": len(jobs),
        },
        "selection_policy": {
            "max_jobs": max_jobs,
            "priorities": [
                "pre_tts_contradiction",
                "speaker_correction",
                "provider_model_coverage",
                "low_rewrite_control",
                "overall_risk_score",
            ],
        },
        "coverage": {
            "providers": dict(sorted(provider_counts.items())),
            "pre_tts_contradiction_jobs": sum(
                1 for job in jobs if job["metrics"]["pre_tts_contradictions"]
            ),
            "speaker_correction_jobs": sum(
                1 for job in jobs if job["metrics"]["speaker_corrections"]
            ),
            "low_rewrite_control_jobs": sum(
                1 for job in jobs if "low_rewrite_control" in job["selection_reasons"]
            ),
        },
        "jobs": jobs,
    }
    write_json(paths.output_dir / "manifest.json", manifest)
    write_dataset_readme(paths.output_dir, manifest)
    return manifest


def write_dataset_readme(output_dir: Path, manifest: dict[str, Any]) -> None:
    lines = [
        "# Video Translation Quality Benchmark Fixture",
        "",
        "This fixture is generated from historical intermediate project artifacts and gateway metering exports.",
        "It is sanitized for offline benchmark and regression work: no source URLs, payment fields, media files, or full raw transcript payloads should be present.",
        "",
        "## Contents",
        "",
        "- `manifest.json`: dataset version, selection policy, coverage, and benchmark job index.",
        "- `jobs/bench_*/segments.json`: whitelisted segment-level duration/alignment metrics.",
        "- `jobs/bench_*/pre_tts_events.json`: whitelisted pre-TTS rewrite trigger metrics.",
        "- `jobs/bench_*/speaker_corrections.json`: sanitized speaker correction rows with short redacted snippets.",
        "- `jobs/bench_*/metering_snapshot.json`: whitelisted gateway metering fields.",
        "- `jobs/bench_*/artifact_index.json`: file-level artifact inventory, not raw S2 responses.",
        "",
        "## Coverage",
        "",
        f"- Jobs selected: {manifest['source']['job_count_selected']} / {manifest['source']['job_count_available']}",
        f"- Provider coverage: {manifest['coverage']['providers']}",
        f"- Jobs with pre-TTS contradictions: {manifest['coverage']['pre_tts_contradiction_jobs']}",
        f"- Jobs with speaker corrections: {manifest['coverage']['speaker_correction_jobs']}",
        "",
        "Regenerate with:",
        "",
        "```bash",
        "python scripts/benchmark/build_quality_dataset.py --force --max-jobs 12",
        "python scripts/benchmark/validate_quality_dataset.py",
        "python scripts/benchmark/report_quality_baseline.py",
        "```",
        "",
    ]
    (output_dir / "README.md").write_text("\n".join(lines), encoding="utf-8")


def _walk_json_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.json") if path.is_file())


def _scan_forbidden(payload: Any, *, path: str = "") -> list[str]:
    findings: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            key_text = str(key).lower()
            if any(token in key_text for token in FORBIDDEN_KEYWORDS):
                findings.append(f"{path}.{key}" if path else str(key))
            findings.extend(_scan_forbidden(value, path=f"{path}.{key}" if path else str(key)))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            findings.extend(_scan_forbidden(value, path=f"{path}[{index}]"))
    elif isinstance(payload, str):
        lower = payload.lower()
        if "http://" in lower or "https://" in lower or _EMAIL_RE.search(payload):
            findings.append(path or "<string>")
    return findings


def validate_quality_dataset(dataset_dir: Path) -> dict[str, Any]:
    manifest_path = dataset_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("version") != DATASET_VERSION:
        raise ValueError(f"Unexpected dataset version: {manifest.get('version')}")
    jobs = manifest.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("Manifest must contain at least one job")

    benchmark_ids: set[str] = set()
    errors: list[str] = []
    for job in jobs:
        benchmark_id = job.get("benchmark_id")
        if not benchmark_id:
            errors.append("Job without benchmark_id")
            continue
        if benchmark_id in benchmark_ids:
            errors.append(f"Duplicate benchmark_id: {benchmark_id}")
        benchmark_ids.add(benchmark_id)
        for label, rel_path in (job.get("files") or {}).items():
            path = dataset_dir / rel_path
            if not path.exists():
                errors.append(f"Missing {label} file for {benchmark_id}: {rel_path}")

    for path in _walk_json_files(dataset_dir):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"Invalid JSON {path}: {exc}")
            continue
        for finding in _scan_forbidden(payload):
            errors.append(f"Forbidden data in {path.relative_to(dataset_dir)} at {finding}")

    media_exts = {".mp3", ".wav", ".m4a", ".mp4", ".mov", ".aac", ".flac"}
    media_files = [path for path in dataset_dir.rglob("*") if path.suffix.lower() in media_exts]
    for path in media_files:
        errors.append(f"Media file should not be in text fixture: {path.relative_to(dataset_dir)}")

    if errors:
        raise ValueError("Dataset validation failed:\n" + "\n".join(errors))

    summary = {
        "dataset_dir": dataset_dir.as_posix(),
        "jobs": len(jobs),
        "json_files": len(_walk_json_files(dataset_dir)),
        "status": "ok",
    }
    return summary


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 4)
    index = (len(ordered) - 1) * p
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return round(ordered[int(index)], 4)
    weight = index - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 4)


def load_job_fixture(dataset_dir: Path, job: dict[str, Any]) -> dict[str, Any]:
    files = job.get("files") or {}
    payload: dict[str, Any] = {"manifest_entry": job}
    for key, rel_path in files.items():
        path = dataset_dir / rel_path
        if path.exists():
            payload[key] = json.loads(path.read_text(encoding="utf-8"))
    return payload


def build_baseline_report(
    *,
    dataset_dir: Path,
    output_dir: Path,
    llm_rewrite_cost_cny: float = 0.0003,
    tts_rewrite_cost_low_cny: float = 0.02,
    tts_rewrite_cost_high_cny: float = 0.07,
    manual_speaker_fix_cost_cny: float = 3.0,
) -> dict[str, Any]:
    validation = validate_quality_dataset(dataset_dir)
    manifest = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
    fixtures = [load_job_fixture(dataset_dir, job) for job in manifest["jobs"]]

    segment_count = 0
    rewrite_segments = 0
    needs_review_segments = 0
    force_dsp_segments = 0
    pre_tts_events = 0
    pre_tts_contradictions = 0
    harmful_pre_tts_contradictions = 0
    pre_tts_root_causes: Counter[str] = Counter()
    rewrite_counts: list[float] = []
    segment_durations: list[float] = []
    correction_durations: list[float] = []
    provider_summary: dict[str, dict[str, Any]] = {}

    for fixture in fixtures:
        manifest_entry = fixture["manifest_entry"]
        metrics = manifest_entry.get("metrics") or {}
        provider_key = "/".join(
            str(metrics.get(part) or "unknown") for part in ("provider", "tts_model", "service_mode")
        )
        provider_entry = provider_summary.setdefault(
            provider_key,
            {
                "jobs": 0,
                "segments": 0,
                "rewrite_segments": 0,
                "rewrite_count": 0,
                "pre_tts_contradictions": 0,
                "harmful_pre_tts_contradictions": 0,
            },
        )
        provider_entry["jobs"] += 1

        segments = (fixture.get("segments") or {}).get("segments") or []
        speaker_corrections = (fixture.get("speaker_corrections") or {}).get("corrections") or []
        events = (fixture.get("pre_tts_events") or {}).get("events") or []

        segment_count += len(segments)
        provider_entry["segments"] += len(segments)
        for segment in segments:
            if parse_int(segment.get("rewrite_count")) > 0:
                rewrite_segments += 1
                provider_entry["rewrite_segments"] += 1
            if parse_bool(segment.get("needs_review")):
                needs_review_segments += 1
            if segment.get("alignment_method") == "force_dsp":
                force_dsp_segments += 1
            duration = parse_float(segment.get("target_duration_ms"))
            if duration is not None:
                segment_durations.append(duration)
        for event in events:
            pre_tts_events += 1
            if event.get("pre_tts_contradiction"):
                pre_tts_contradictions += 1
                provider_entry["pre_tts_contradictions"] += 1
                pre_tts_root_causes[classify_pre_tts_contradiction(event) or "not_contradiction"] += 1
            if event.get("pre_tts_harmful_contradiction") or event.get("harmful_contradiction"):
                harmful_pre_tts_contradictions += 1
                provider_entry["harmful_pre_tts_contradictions"] += 1
        for correction in speaker_corrections:
            duration = parse_float(correction.get("duration_ms"))
            if duration is not None:
                correction_durations.append(duration)
        rewrite_count = parse_float(metrics.get("rewrite_count"))
        if rewrite_count is not None:
            rewrite_counts.append(rewrite_count)
            provider_entry["rewrite_count"] += rewrite_count

    total_rewrite_count = int(sum(rewrite_counts))
    total_speaker_corrections = len(correction_durations)
    cost_model = {
        "assumptions_cny": {
            "llm_rewrite_per_segment": llm_rewrite_cost_cny,
            "tts_rewrite_per_segment_low": tts_rewrite_cost_low_cny,
            "tts_rewrite_per_segment_high": tts_rewrite_cost_high_cny,
            "manual_speaker_fix": manual_speaker_fix_cost_cny,
        },
        "current_dataset_cost_cny": {
            "llm_rewrite": round(total_rewrite_count * llm_rewrite_cost_cny, 4),
            "tts_rewrite_low": round(total_rewrite_count * tts_rewrite_cost_low_cny, 4),
            "tts_rewrite_high": round(total_rewrite_count * tts_rewrite_cost_high_cny, 4),
            "manual_speaker_fix_proxy": round(
                total_speaker_corrections * manual_speaker_fix_cost_cny, 4
            ),
        },
        "break_even": {
            "extra_llm_candidates_per_rewrite_to_match_tts_low": round(
                tts_rewrite_cost_low_cny / llm_rewrite_cost_cny, 2
            )
            if llm_rewrite_cost_cny
            else None,
            "extra_llm_candidates_per_rewrite_to_match_tts_high": round(
                tts_rewrite_cost_high_cny / llm_rewrite_cost_cny, 2
            )
            if llm_rewrite_cost_cny
            else None,
        },
    }

    report = {
        "dataset": {
            "dir": dataset_dir.as_posix(),
            "version": manifest.get("version"),
            "jobs": len(fixtures),
            "validation": validation,
        },
        "quality_baseline": {
            "segments": segment_count,
            "rewrite_segments": rewrite_segments,
            "rewrite_segment_rate": round(rewrite_segments / segment_count, 4)
            if segment_count
            else 0.0,
            "rewrite_count_total": total_rewrite_count,
            "rewrite_count_p50": percentile(rewrite_counts, 0.5),
            "rewrite_count_p90": percentile(rewrite_counts, 0.9),
            "needs_review_segments": needs_review_segments,
            "needs_review_rate": round(needs_review_segments / segment_count, 4)
            if segment_count
            else 0.0,
            "force_dsp_segments": force_dsp_segments,
            "pre_tts_events": pre_tts_events,
            "pre_tts_contradictions": pre_tts_contradictions,
            "pre_tts_contradiction_rate": round(pre_tts_contradictions / pre_tts_events, 4)
            if pre_tts_events
            else 0.0,
            "harmful_pre_tts_contradictions": harmful_pre_tts_contradictions,
            "harmful_pre_tts_contradiction_rate": round(
                harmful_pre_tts_contradictions / pre_tts_events, 4
            )
            if pre_tts_events
            else 0.0,
            "pre_tts_contradiction_root_cause_proxy": dict(sorted(pre_tts_root_causes.items())),
            "speaker_corrections": total_speaker_corrections,
            "segment_duration_ms_cdf": {
                "p10": percentile(segment_durations, 0.10),
                "p25": percentile(segment_durations, 0.25),
                "p50": percentile(segment_durations, 0.50),
                "p75": percentile(segment_durations, 0.75),
                "p90": percentile(segment_durations, 0.90),
            },
            "speaker_correction_duration_ms_cdf": {
                "p10": percentile(correction_durations, 0.10),
                "p25": percentile(correction_durations, 0.25),
                "p50": percentile(correction_durations, 0.50),
                "p75": percentile(correction_durations, 0.75),
                "p90": percentile(correction_durations, 0.90),
            },
        },
        "provider_summary": dict(sorted(provider_summary.items())),
        "cost_model": cost_model,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "baseline.json", report)
    (output_dir / "baseline.md").write_text(render_baseline_markdown(report), encoding="utf-8")
    return report


def render_baseline_markdown(report: dict[str, Any]) -> str:
    baseline = report["quality_baseline"]
    cost = report["cost_model"]
    provider_summary = report["provider_summary"]
    lines = [
        "# Video Translation Quality Baseline",
        "",
        f"- Jobs: {report['dataset']['jobs']}",
        f"- Segments: {baseline['segments']}",
        f"- Rewrite segment rate: {baseline['rewrite_segment_rate']:.2%}",
        f"- Pre-TTS contradiction rate: {baseline['pre_tts_contradiction_rate']:.2%}",
        f"- Harmful Pre-TTS contradiction rate: {baseline['harmful_pre_tts_contradiction_rate']:.2%}",
        f"- Speaker corrections: {baseline['speaker_corrections']}",
        f"- Pre-TTS contradiction root-cause proxy: {baseline['pre_tts_contradiction_root_cause_proxy']}",
        "",
        "## Duration CDF",
        "",
        "| Metric | p10 | p25 | p50 | p75 | p90 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, values in (
        ("All segments target ms", baseline["segment_duration_ms_cdf"]),
        ("Speaker correction ms", baseline["speaker_correction_duration_ms_cdf"]),
    ):
        lines.append(
            f"| {label} | {values['p10']} | {values['p25']} | {values['p50']} | {values['p75']} | {values['p90']} |"
        )
    lines.extend(
        [
            "",
            "## Provider Summary",
            "",
            "| Provider / model / mode | jobs | segments | rewrite segments | rewrite count | contradictions | harmful contradictions |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for key, values in provider_summary.items():
        lines.append(
            f"| {key} | {values['jobs']} | {values['segments']} | {values['rewrite_segments']} | "
            f"{values['rewrite_count']} | {values['pre_tts_contradictions']} | "
            f"{values['harmful_pre_tts_contradictions']} |"
        )
    current_cost = cost["current_dataset_cost_cny"]
    break_even = cost["break_even"]
    lines.extend(
        [
            "",
            "## Cost Model",
            "",
            f"- LLM rewrite cost proxy: CNY {current_cost['llm_rewrite']}",
            f"- TTS rewrite cost proxy: CNY {current_cost['tts_rewrite_low']} - {current_cost['tts_rewrite_high']}",
            f"- Manual speaker fix proxy: CNY {current_cost['manual_speaker_fix_proxy']}",
            f"- One avoided TTS rewrite equals about {break_even['extra_llm_candidates_per_rewrite_to_match_tts_low']} - "
            f"{break_even['extra_llm_candidates_per_rewrite_to_match_tts_high']} extra LLM rewrite-sized calls.",
            "",
        ]
    )
    return "\n".join(lines)
