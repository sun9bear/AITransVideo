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
import signal as _signal
import socket
import subprocess as sp
import sys
import traceback
from collections import defaultdict
from pathlib import Path


_INTERRUPTED = {"flag": False}


def _signal_handler(signum, frame):
    _INTERRUPTED["flag"] = True


def _install_signal_handlers():
    try:
        _signal.signal(_signal.SIGINT, _signal_handler)
        _signal.signal(_signal.SIGTERM, _signal_handler)
    except (AttributeError, ValueError):
        pass  # Some platforms (Windows) restrict


SCHEMA_VERSION = 1


SPEAKER_THRESHOLDS = (0.05, 0.10, 0.15, 0.20)


SAMPLE_THRESHOLDS_S = (5, 8, 10, 15)


REWRITE_TASKS = frozenset({
    "s5_rewrite", "s5_rewrite_strict", "s5_short_content_compact",
})
RETTS_BUCKETS = frozenset({"post_tts_resynth", "post_edit_resynth"})


_WHISPER_ALIGNED_SOURCE = "semantic_block_v2_whisper_aligned"
# Stage names that may contain workflow alignment cache (prod smoke 验证)
_ALIGNMENT_STAGE_CANDIDATES = ("audio_alignment", "subtitle_alignment", "alignment")


SPEAKER_EVENT_TYPES = frozenset({
    "translation_segment_speaker_changed",
    "post_edit_segment_speaker_changed",
    "voice_selection_speaker_reassigned",
})
SPLIT_EVENT_TYPES = frozenset({
    "translation_segment_split_confirmed",
    "post_edit_segment_split_confirmed",
})
TEXT_EVENT_TYPES = frozenset({
    "translation_segment_text_changed",
    "post_edit_text_changed",
})


ARTIFACT_PATHS = {
    # JOBS root (flat)
    "job_record":             "{job_id}.json",
    "job_events":             "{job_id}.events.jsonl",

    # PROJECT/JOB level (2-level nested)
    "project_state":          "project_state.json",
    "review_state":           "review_state.json",
    "manifest":               "manifest.json",
    "download_metadata":      "download_metadata.json",
    "transcript":             "transcript/transcript.json",
    "s2_review_result":       "transcript/s2_review_result.json",
    "s2_review_audit":        "transcript/s2_review_audit.json",
    "s2_pass1_result":        "transcript/s2_pass1_result.json",
    "translation_segments":   "translation/segments.json",
    "editor_segments":        "editor/segments.json",
    "subtitle_quality_report": "output/subtitle_quality_report.json",
    "subtitle_cues":           "output/subtitle_cues.json",
    "usage_events":           "metering/usage_events.jsonl",
    "user_edit_events":       "audit/user_edit_events.jsonl",
}


def _extract_project_id_from_record(rec: dict, job_id: str) -> str | None:
    """Get project_id, falling back to parsing project_dir / manifest_path.

    Real JobRecord may not have a top-level project_id field; instead, the
    project_dir / manifest_path absolute path encodes it as the segment
    between '/projects/' and '/job_<job_id>/'.
    """
    pid = rec.get("project_id")
    if isinstance(pid, str) and pid:
        return pid
    bare_job_id = job_id.removeprefix("job_") if job_id.startswith("job_") else job_id
    job_segment = f"job_{bare_job_id}"
    for path_field in ("project_dir", "manifest_path"):
        path_value = rec.get(path_field)
        if not isinstance(path_value, str) or not path_value:
            continue
        # Normalize separators (real data has POSIX slashes)
        parts = path_value.replace("\\", "/").split("/")
        for i, part in enumerate(parts):
            if part == "projects" and i + 1 < len(parts):
                candidate = parts[i + 1]
                # Sanity: candidate is not the job segment itself
                if candidate and candidate != job_segment:
                    return candidate
    return None


def _resolve_project_dir(projects_root: Path, project_id: str | None,
                          job_id: str) -> Path | None:
    """projects/<project_id>/job_<bare_id>/ — handles missing project_id."""
    if not project_id:
        return None
    bare_job_id = job_id.removeprefix("job_") if job_id.startswith("job_") else job_id
    candidate = projects_root / project_id / f"job_{bare_job_id}"
    return candidate if candidate.is_dir() else None


def _safe_load_json(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _extract_from_project_state(project_state: dict | None) -> dict:
    """Return {duration_seconds, source_language, asr_speaker_count} or null fields."""
    out = {
        "duration_seconds": None,
        "source_language": None,
        "asr_speaker_count": None,
    }
    if not isinstance(project_state, dict):
        return out
    stages = project_state.get("stages") or {}
    ingestion = (stages.get("ingestion") or {}).get("payload") or {}
    media = (stages.get("media_understanding") or {}).get("payload") or {}
    if isinstance(ingestion.get("duration_ms"), (int, float)):
        out["duration_seconds"] = ingestion["duration_ms"] / 1000.0
    if isinstance(media.get("language"), str):
        out["source_language"] = media["language"]
    if isinstance(media.get("speaker_count"), int):
        out["asr_speaker_count"] = media["speaker_count"]
    return out


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


def _count_orphaned_project_dirs(projects_root: Path, jobs_root: Path) -> int:
    """Count project_dirs under projects_root with no matching JobRecord.

    Production has 12 jobs but 84 project_dirs in some samples — this number
    surfaces that gap to operators. Pattern: projects/<project_id>/job_<bare_id>/.
    """
    if not projects_root.is_dir() or not jobs_root.is_dir():
        return 0
    # Build set of known job_ids (with prefix) from jobs_root
    known_job_ids = set()
    for p in jobs_root.glob("job_*.json"):
        if p.name.endswith(".events.jsonl"):
            continue
        # Strip ".json" suffix and use as job_id
        known_job_ids.add(p.stem)
    # Walk projects/<pid>/job_<bare>/ and count those whose job_id has no record
    orphaned = 0
    for project_id_dir in projects_root.iterdir():
        if not project_id_dir.is_dir():
            continue
        for job_dir in project_id_dir.iterdir():
            if not job_dir.is_dir():
                continue
            if not job_dir.name.startswith("job_"):
                continue
            if job_dir.name not in known_job_ids:
                orphaned += 1
    return orphaned


def _build_artifact_presence(project_dir: Path | None) -> dict:
    """Check existence of each artifact path."""
    if project_dir is None or not project_dir.is_dir():
        return {key: False for key in [
            "project_state_json", "review_state_json", "manifest_json",
            "transcript_json", "s2_review_result_json", "s2_pass1_result_json",
            "translation_segments_json", "editor_segments_json",
            "subtitle_quality_report", "subtitle_cues",
            "metering_usage_events", "audit_user_edit_events",
        ]}
    return {
        "project_state_json": (project_dir / ARTIFACT_PATHS["project_state"]).is_file(),
        "review_state_json": (project_dir / ARTIFACT_PATHS["review_state"]).is_file(),
        "manifest_json": (project_dir / ARTIFACT_PATHS["manifest"]).is_file(),
        "transcript_json": (project_dir / ARTIFACT_PATHS["transcript"]).is_file(),
        "s2_review_result_json": (project_dir / ARTIFACT_PATHS["s2_review_result"]).is_file(),
        "s2_pass1_result_json": (project_dir / ARTIFACT_PATHS["s2_pass1_result"]).is_file(),
        "translation_segments_json": (project_dir / ARTIFACT_PATHS["translation_segments"]).is_file(),
        "editor_segments_json": (project_dir / ARTIFACT_PATHS["editor_segments"]).is_file(),
        "subtitle_quality_report": (project_dir / ARTIFACT_PATHS["subtitle_quality_report"]).is_file(),
        "subtitle_cues": (project_dir / ARTIFACT_PATHS["subtitle_cues"]).is_file(),
        "metering_usage_events": (project_dir / ARTIFACT_PATHS["usage_events"]).is_file(),
        "audit_user_edit_events": (project_dir / ARTIFACT_PATHS["user_edit_events"]).is_file(),
    }


def _build_fact_sheet(rec: dict, project_dir: Path | None,
                      ps_extracted: dict, run_id: str,
                      ps: dict | None = None,
                      resolved_project_id: str | None = None) -> dict:
    """Build the per-job fact sheet."""
    edit_gen = rec.get("edit_generation", 0) or 0
    transcript = (_safe_load_json(project_dir / ARTIFACT_PATHS["transcript"])
                  if project_dir else None)
    speaker_stats = _compute_speaker_stats(
        transcript, ps_extracted.get("asr_speaker_count")
    )
    clone_sample_stats = _compute_clone_sample_stats(transcript)
    if resolved_project_id is None:
        resolved_project_id = _extract_project_id_from_record(rec, rec["job_id"])
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "job_id": rec["job_id"],
        "project_id": resolved_project_id,
        "root_job_id": rec.get("root_job_id") or rec["job_id"],
        "service_mode": rec.get("service_mode"),
        "status": rec["status"],
        "created_at": rec["created_at"],
        "duration_seconds": ps_extracted["duration_seconds"],
        "source_language": ps_extracted["source_language"],
        "target_language": "zh-CN",
        "tts_provider": rec.get("tts_provider"),
        "tts_model": rec.get("tts_model"),
        "edit_generation": edit_gen,
        "had_post_edit": edit_gen > 0 or rec.get("copy_of_job_id") is not None,
        "artifact_presence": _build_artifact_presence(project_dir),
        "speaker_stats": speaker_stats,
        "clone_sample_stats": clone_sample_stats,
        "actual_clone_stats": _compute_actual_clone_stats(project_dir),
        "retry_stats": _compute_retry_stats(project_dir),
        "usage_meter": (
            _compute_usage_meter(project_dir / ARTIFACT_PATHS["usage_events"])
            if project_dir else None
        ),
        "subtitle_sync": _compute_subtitle_sync(project_dir),
        "whisper": _compute_whisper(project_dir),
        "workflow_alignment_cache": _compute_workflow_alignment_cache(ps),
        "user_edits": _compute_user_edits(project_dir),
    }


def _compute_speaker_stats(transcript: dict | None,
                            asr_speaker_count: int | None) -> dict | None:
    """Return speaker_stats dict or None if transcript missing."""
    if not isinstance(transcript, dict):
        return None
    lines = transcript.get("lines")
    if not isinstance(lines, list) or not lines:
        return None
    durations = defaultdict(float)
    for line in lines:
        spk = line.get("speaker_id")
        s = line.get("start_ms", 0)
        e = line.get("end_ms", 0)
        if spk and isinstance(s, (int, float)) and isinstance(e, (int, float)):
            durations[spk] += max(0.0, e - s)
    total = sum(durations.values())
    if total <= 0:
        return None
    shares = sorted(
        (d / total for d in durations.values()),
        reverse=True,
    )
    by_threshold = {
        f"{t:.2f}": sum(1 for s in shares if s >= t)
        for t in SPEAKER_THRESHOLDS
    }
    return {
        "asr_speaker_count": asr_speaker_count or len(durations),
        "speaker_duration_shares": [round(s, 4) for s in shares],
        "speaker_count_by_threshold": by_threshold,
    }


def _compute_clone_sample_stats(transcript: dict | None) -> dict | None:
    """Per speaker: bucket-count of sample durations ≥ each threshold."""
    if not isinstance(transcript, dict):
        return None
    lines = transcript.get("lines")
    if not isinstance(lines, list) or not lines:
        return None
    by_speaker = defaultdict(list)
    for line in lines:
        spk = line.get("speaker_id")
        s = line.get("start_ms", 0)
        e = line.get("end_ms", 0)
        if spk and isinstance(s, (int, float)) and isinstance(e, (int, float)):
            dur_s = max(0.0, e - s) / 1000.0
            by_speaker[spk].append(dur_s)
    # Order by total duration descending (matches speaker_duration_shares ordering)
    sorted_speakers = sorted(
        by_speaker.items(),
        key=lambda kv: sum(kv[1]),
        reverse=True,
    )
    buckets = []
    for _, durations in sorted_speakers:
        bucket = {f"≥{t}s": sum(1 for d in durations if d >= t)
                  for t in SAMPLE_THRESHOLDS_S}
        buckets.append(bucket)
    return {
        "eligible_speakers": len(buckets),
        "eligible_sample_count_buckets_by_speaker": buckets,
    }


def _classify_voice_id(voice_id: str) -> str:
    """'cloned' | 'preset' | 'unknown' / 'auto'."""
    if not voice_id or voice_id.lower() == "auto":
        return "unknown"
    # MiniMax cloned voices typically have moss_audio_ prefix or long uuid hash
    if voice_id.startswith("moss_audio_"):
        return "cloned"
    if len(voice_id) >= 32 and "-" in voice_id:
        return "cloned"
    return "preset"


def _compute_actual_clone_stats(project_dir: Path | None) -> dict | None:
    """Per speaker: cloned vs preset voice classification."""
    if not project_dir:
        return None
    # Prefer editor/segments.json (post-edit-aware), fall back to translation/segments.json
    segs_path = project_dir / ARTIFACT_PATHS["editor_segments"]
    if not segs_path.is_file():
        segs_path = project_dir / ARTIFACT_PATHS["translation_segments"]
    segs = _safe_load_json(segs_path)
    if not isinstance(segs, list) or not segs:
        return None
    # First voice_id seen per speaker
    by_speaker = {}
    for seg in segs:
        spk = seg.get("speaker_id")
        vid = seg.get("voice_id")
        if spk and vid and spk not in by_speaker:
            by_speaker[spk] = vid
    # Order by appearance (= order in segments)
    voice_ids = list(by_speaker.values())
    classifications = [_classify_voice_id(v) for v in voice_ids]
    return {
        "cloned_speakers": classifications.count("cloned"),
        "preset_speakers": classifications.count("preset"),
        "voice_ids_by_speaker": voice_ids,
    }


def _compute_retry_stats(project_dir: Path | None) -> dict | None:
    """Prefer metering, fall back to editor/segments.json rewrite_count sum."""
    if not project_dir:
        return None
    metering_path = project_dir / ARTIFACT_PATHS["usage_events"]
    if metering_path.is_file():
        # Phase D: implement metering parsing (Task C2)
        return _retry_stats_from_metering(metering_path)
    # Fallback
    editor_segs = _safe_load_json(project_dir / ARTIFACT_PATHS["editor_segments"])
    if not isinstance(editor_segs, list):
        return {
            "rewrite_count": None,
            "retts_count": None,
            "retts_total_duration_ms": None,
            "_data_source": "no_data",
        }
    return {
        "rewrite_count": sum(s.get("rewrite_count", 0) or 0 for s in editor_segs),
        "retts_count": None,
        "retts_total_duration_ms": None,
        "_data_source": "fallback_editor_segments",
    }


def _retry_stats_from_metering(metering_path: Path) -> dict:
    rewrite = 0
    retts_count = 0
    retts_dur_ms = 0
    try:
        for line in metering_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = ev.get("kind")
            if kind == "llm" and ev.get("task") in REWRITE_TASKS:
                rewrite += 1
            elif kind == "tts" and ev.get("bucket") in RETTS_BUCKETS:
                retts_count += 1
                retts_dur_ms += int(ev.get("duration_ms") or 0)
    except OSError:
        return {
            "rewrite_count": None, "retts_count": None,
            "retts_total_duration_ms": None,
            "_data_source": "metering_unreadable",
        }
    return {
        "rewrite_count": rewrite,
        "retts_count": retts_count,
        "retts_total_duration_ms": retts_dur_ms,
        "_data_source": "metering",
    }


def _compute_usage_meter(metering_path: Path) -> dict | None:
    """Aggregate llm tokens / tts chars / clone calls for cost estimation.

    Includes rewrite_input_text_chars_total (sum of input_text_chars for
    LLM events with task IN REWRITE_TASKS) — needed by analyzer §4.2 cost
    formula's rewrite_extra_rmb term.
    """
    if not metering_path.is_file():
        return None
    agg = {
        "llm_input_tokens": 0,
        "llm_output_tokens": 0,
        "tts_chars_total": 0,
        "post_tts_resynth_billed_chars": 0,
        "post_edit_resynth_billed_chars": 0,
        "clone_calls": 0,
        "rewrite_count": 0,
        "rewrite_input_text_chars_total": 0,
    }
    try:
        for line in metering_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = ev.get("kind")
            if kind == "llm":
                agg["llm_input_tokens"] += int(ev.get("input_tokens") or 0)
                agg["llm_output_tokens"] += int(ev.get("output_tokens") or 0)
                if ev.get("task") in REWRITE_TASKS:
                    agg["rewrite_count"] += 1
                    agg["rewrite_input_text_chars_total"] += int(
                        ev.get("input_text_chars") or 0
                    )
            elif kind == "tts":
                bc = int(ev.get("billed_chars") or 0)
                agg["tts_chars_total"] += bc
                bucket = ev.get("bucket")
                if bucket == "post_tts_resynth":
                    agg["post_tts_resynth_billed_chars"] += bc
                elif bucket == "post_edit_resynth":
                    agg["post_edit_resynth_billed_chars"] += bc
            elif kind == "voice_clone":
                agg["clone_calls"] += 1
    except OSError:
        return None
    return agg


def _compute_subtitle_sync(project_dir: Path | None) -> dict | None:
    if not project_dir:
        return None
    path = project_dir / ARTIFACT_PATHS["subtitle_quality_report"]
    if not path.is_file():
        return {
            "text_audio_drift_count": None,
            "drift_block_ids": [],
            "_reason_null": "subtitle_quality_report not present",
        }
    data = _safe_load_json(path)
    if not isinstance(data, dict):
        return {"text_audio_drift_count": None, "drift_block_ids": [], "_reason_null": "unreadable"}
    drift_count = data.get("text_audio_drift_count")
    drift_ids = []
    for issue in (data.get("issues") or []):
        if issue.get("type") == "text_audio_drift":
            bid = issue.get("block_id")
            # Sanitize: only positional ID, no content
            if isinstance(bid, str) and bid.startswith("block_"):
                drift_ids.append(bid)
    return {
        "text_audio_drift_count": drift_count if isinstance(drift_count, int) else None,
        "drift_block_ids": drift_ids[:50],  # cap to prevent fingerprint bloat
    }


def _compute_whisper(project_dir: Path | None) -> dict | None:
    if not project_dir:
        return None
    cues_path = project_dir / ARTIFACT_PATHS["subtitle_cues"]
    sidecar_count = sum(
        1 for _ in project_dir.rglob("*.whisper_*_*.json")
    )
    if not cues_path.is_file():
        return {
            "alignment_model": None,
            "alignment_fingerprint": None,
            "whisper_aligned_cue_count": None,
            "proportional_fallback_cue_count": None,
            "whisper_sidecar_count": sidecar_count,
            "_reason_null": "subtitle_cues.json absent (pre-Phase-D job)",
        }
    data = _safe_load_json(cues_path)
    if not isinstance(data, dict):
        return None
    cues = data.get("cues") or []
    aligned = sum(1 for c in cues
                  if _WHISPER_ALIGNED_SOURCE in str(c.get("source", "")))
    total = len(cues)
    return {
        "alignment_model": data.get("alignment_model"),
        "alignment_fingerprint": data.get("alignment_fingerprint"),
        "whisper_aligned_cue_count": aligned,
        "proportional_fallback_cue_count": max(0, total - aligned),
        "whisper_sidecar_count": sidecar_count,
    }


def _compute_workflow_alignment_cache(project_state: dict | None) -> dict | None:
    """DSP TTS aligned-audio stage cache (NOT whisper)."""
    if not isinstance(project_state, dict):
        return None
    stages = project_state.get("stages") or {}
    for name in _ALIGNMENT_STAGE_CANDIDATES:
        stage = stages.get(name)
        if isinstance(stage, dict):
            payload = stage.get("payload") or {}
            chb = payload.get("cache_hit_blocks")
            bc = payload.get("block_count")
            if isinstance(chb, int):
                return {
                    "cache_hit_blocks": chb,
                    "block_count": bc if isinstance(bc, int) else None,
                    "_stage_name": name,
                }
    return {
        "cache_hit_blocks": None,
        "block_count": None,
        "_reason_null": "no alignment stage found in project_state",
    }


def _compute_user_edits(project_dir: Path | None) -> dict | None:
    if not project_dir:
        return None
    path = project_dir / ARTIFACT_PATHS["user_edit_events"]
    if not path.is_file():
        return {
            "speaker_corrections_effective": None,
            "splits_confirmed_effective": None,
            "text_changes_effective": None,
            "_reason_null": "audit/user_edit_events.jsonl absent",
        }
    counts = {"speaker": 0, "split": 0, "text": 0}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("effective_marker") != "effective":
                continue
            et = ev.get("event_type")
            if et in SPEAKER_EVENT_TYPES:
                counts["speaker"] += 1
            elif et in SPLIT_EVENT_TYPES:
                counts["split"] += 1
            elif et in TEXT_EVENT_TYPES:
                counts["text"] += 1
    except OSError:
        return None
    return {
        "speaker_corrections_effective": counts["speaker"],
        "splits_confirmed_effective": counts["split"],
        "text_changes_effective": counts["text"],
    }


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

    _install_signal_handlers()
    _INTERRUPTED["flag"] = False  # Reset for each run (test isolation)

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
            paths = list(_iter_job_record_paths(jobs_root))
            for record_path in paths:
                if _INTERRUPTED["flag"]:
                    break
                # Stop scanning once we've factsheeted enough (--limit applies
                # AFTER status + date filters so prod smoke gets real samples,
                # not the first N alphabetically).
                if args.limit is not None and facts_count >= args.limit:
                    break
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

                # Date filter — relies on YYYY-MM-DD prefix being lexicographically
                # sortable. Real data is all `+00:00` UTC, so simple string compare
                # works without timezone arithmetic. The `+99:99` sentinel for
                # --until trivially sorts after any real timezone suffix, giving
                # an inclusive end-of-day cutoff.
                if args.since and created_at < args.since:
                    skipped_date += 1
                    continue
                if args.until:
                    until_marker = args.until + "T23:59:59.999999+99:99"
                    if created_at > until_marker:
                        skipped_date += 1
                        continue

                resolved_project_id = _extract_project_id_from_record(rec, job_id)
                project_dir = _resolve_project_dir(
                    projects_root, resolved_project_id, job_id
                )
                ps = (_safe_load_json(project_dir / ARTIFACT_PATHS["project_state"])
                      if project_dir else None)
                ps_extracted = _extract_from_project_state(ps)

                inv_entry = {
                    "schema_version": SCHEMA_VERSION,
                    "job_id": job_id,
                    "project_id": resolved_project_id,
                    "status": status,
                    "created_at": created_at,
                    "duration_seconds": ps_extracted["duration_seconds"],
                    "source_language": ps_extracted["source_language"],
                    "target_language": "zh-CN",
                    "service_mode": rec.get("service_mode"),
                    "had_post_edit": (rec.get("edit_generation", 0) or 0) > 0
                        or rec.get("copy_of_job_id") is not None,
                }
                fi.write(json.dumps(inv_entry, ensure_ascii=False) + "\n")
                inventory_count += 1

                fact_sheet = _build_fact_sheet(
                    rec, project_dir, ps_extracted, run_id, ps,
                    resolved_project_id=resolved_project_id,
                )
                ff.write(json.dumps(fact_sheet, ensure_ascii=False) + "\n")
                facts_count += 1
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
        "is_complete_run": fatal_exception is None and not _INTERRUPTED["flag"],
        "scan_stats": {
            "jobs_inventoried": inventory_count,
            "jobs_factsheeted": facts_count,
            "skipped_for_status_filter": skipped_status,
            "skipped_for_date_filter": skipped_date,
            "skipped_for_missing_identity": skipped_identity,
            "orphaned_project_dir_count":
                _count_orphaned_project_dirs(projects_root, jobs_root),
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
    if fatal_exception is None and not _INTERRUPTED["flag"]:
        facts_tmp.rename(out_dir / "facts.jsonl")
        inventory_tmp.rename(out_dir / "inventory.jsonl")
        return 0
    # Interrupted → exit 130; fatal → exit 1
    if _INTERRUPTED["flag"]:
        return 130
    return 1


if __name__ == "__main__":
    sys.exit(main())
