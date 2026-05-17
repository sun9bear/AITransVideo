#!/usr/bin/env python3
"""Phase 2 artifact survey — does Phase 2 functionality have enough
data to ship as default-on, or must features be gated to opt-in?

Reads every project's manifest.json under the projects root on this
host. For each, checks:

  1. Does `artifact_index['source.original_video']` exist + does the
     referenced file still live on disk? Gates `stream/source-video`
     Phase 2c rollout.
  2. Does `artifact_index['media.transcript_raw']` exist + does the
     raw_*.json file have word-level speaker labels (`speaker` /
     `speaker_label` keys on the words array)? Gates smart-prefill
     Phase 2b rollout.

Default thresholds (configurable via env):
  AVT_PHASE2_GATE_PCT (default 70) — minimum coverage % for "default on"

Path translation: manifests record container-side paths
(``/opt/aivideotrans/app/projects/...``) but the script runs on the
host where the same bind-mount lives at
``/opt/aivideotrans/data/projects/``. Both are tried.

Plan ref: docs/plans/2026-05-17-edit-page-redesign.md §8.2.
Standard-library only — no pip install needed on the survey host.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


_HOST_PROJECTS_CANDIDATES = (
    Path("/opt/aivideotrans/data/projects"),
    Path("/opt/aivideotrans/app/projects"),
)

# Manifests in the wild use container paths; map to host.
_CONTAINER_PROJECTS_PREFIX = "/opt/aivideotrans/app/projects"
_HOST_PROJECTS_PREFIX = "/opt/aivideotrans/data/projects"

GATE_PCT = float(os.environ.get("AVT_PHASE2_GATE_PCT", "70"))


def find_projects_root() -> Path | None:
    for cand in _HOST_PROJECTS_CANDIDATES:
        if cand.is_dir():
            return cand
    return None


def translate_artifact_path(raw: str, projects_root: Path) -> Path:
    """Map container-recorded path → host filesystem path."""
    if raw.startswith(_CONTAINER_PROJECTS_PREFIX):
        rel = raw[len(_CONTAINER_PROJECTS_PREFIX):].lstrip("/")
        return projects_root / rel
    return Path(raw)


def sample_speaker_labels(words: list[Any], sample_size: int = 200) -> float:
    """Return % of sampled words that carry a non-empty speaker label."""
    if not words:
        return 0.0
    sample = words[:sample_size]
    keys = ("speaker", "speaker_label", "speaker_id")
    labeled = 0
    for w in sample:
        if not isinstance(w, dict):
            continue
        for k in keys:
            v = w.get(k)
            if v is not None and v != "":
                labeled += 1
                break
    return labeled / len(sample) * 100


def inspect_raw_transcript(raw_path: Path) -> dict[str, Any]:
    """Open raw_*.json and detect word-level schema + speaker coverage."""
    result: dict[str, Any] = {
        "file_exists": raw_path.is_file(),
        "words_found": 0,
        "speaker_label_pct": 0.0,
        "schema_hint": None,
    }
    if not result["file_exists"]:
        return result
    try:
        with raw_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return result

    words: list[Any] = []
    if isinstance(data, dict):
        # AssemblyAI-style top-level "words"
        if isinstance(data.get("words"), list):
            words = data["words"]
            result["schema_hint"] = "top-level_words"
        # Sometimes nested under utterances
        elif isinstance(data.get("utterances"), list):
            agg: list[Any] = []
            for utt in data["utterances"]:
                if isinstance(utt, dict) and isinstance(utt.get("words"), list):
                    agg.extend(utt["words"])
            words = agg
            result["schema_hint"] = "utterances_nested_words"
        elif isinstance(data.get("results"), dict):
            # Generic fallback — some providers nest under results
            inner = data["results"].get("words")
            if isinstance(inner, list):
                words = inner
                result["schema_hint"] = "results_words"
    elif isinstance(data, list):
        # Some providers dump a flat list
        words = data
        result["schema_hint"] = "flat_list"

    result["words_found"] = len(words) if isinstance(words, list) else 0
    if result["words_found"] > 0:
        result["speaker_label_pct"] = sample_speaker_labels(words)
    return result


def survey() -> dict[str, Any]:
    projects_root = find_projects_root()
    if projects_root is None:
        return {"error": "no projects/ root found"}

    totals = {
        "user_dirs": 0,
        "job_dirs": 0,
        "with_manifest": 0,
        "with_source_video_key": 0,
        "with_source_video_file": 0,
        "with_transcript_raw_key": 0,
        "with_transcript_raw_file": 0,
        "with_speaker_labels": 0,
    }
    schema_hist: defaultdict[str, int] = defaultdict(int)
    raw_filename_hist: defaultdict[str, int] = defaultdict(int)

    for user_dir in sorted(projects_root.iterdir()):
        if not user_dir.is_dir():
            continue
        totals["user_dirs"] += 1
        for job_dir in sorted(user_dir.iterdir()):
            if not job_dir.is_dir():
                continue
            totals["job_dirs"] += 1
            manifest_path = job_dir / "manifest.json"
            if not manifest_path.is_file():
                continue
            totals["with_manifest"] += 1
            try:
                with manifest_path.open("r", encoding="utf-8") as f:
                    manifest = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

            artifact_index = manifest.get("artifact_index") or {}
            if not isinstance(artifact_index, dict):
                continue

            # source.original_video — Phase 2c
            sv_raw = artifact_index.get("source.original_video")
            if isinstance(sv_raw, str) and sv_raw:
                totals["with_source_video_key"] += 1
                sv_path = translate_artifact_path(sv_raw, projects_root)
                if sv_path.is_file():
                    totals["with_source_video_file"] += 1

            # media.transcript_raw — Phase 2b
            tr_raw = artifact_index.get("media.transcript_raw")
            if isinstance(tr_raw, str) and tr_raw:
                totals["with_transcript_raw_key"] += 1
                tr_path = translate_artifact_path(tr_raw, projects_root)
                raw_filename_hist[tr_path.name] += 1
                rt = inspect_raw_transcript(tr_path)
                if rt["file_exists"] and rt["words_found"] > 0:
                    totals["with_transcript_raw_file"] += 1
                    if rt["schema_hint"]:
                        schema_hist[rt["schema_hint"]] += 1
                    # ≥ 50% of sampled words carry a speaker label
                    if rt["speaker_label_pct"] >= 50:
                        totals["with_speaker_labels"] += 1

    return {
        "projects_root": str(projects_root),
        "totals": totals,
        "raw_filename_distribution": dict(raw_filename_hist),
        "schema_hint_distribution": dict(schema_hist),
    }


def fmt_pct(num: int, denom: int) -> str:
    if denom <= 0:
        return "0.0% (0/0)"
    return f"{num / denom * 100:.1f}% ({num}/{denom})"


def gate_verdict(num: int, denom: int) -> str:
    if denom <= 0:
        return "INDETERMINATE (no data)"
    pct = num / denom * 100
    return "PASS — default-on" if pct >= GATE_PCT else f"FAIL — opt-in only (< {GATE_PCT:.0f}%)"


def main() -> int:
    out = survey()
    if "error" in out:
        print(f"ERROR: {out['error']}", file=sys.stderr)
        return 1

    t = out["totals"]
    denom = t["with_manifest"]
    print(f"Projects root:                  {out['projects_root']}")
    print(f"User dirs:                      {t['user_dirs']}")
    print(f"Job dirs:                       {t['job_dirs']}")
    print(f"With manifest.json (denom):     {t['with_manifest']}")
    print()
    print("== Phase 2c · stream/source-video gate ==")
    print(f"  artifact_index key set:       {fmt_pct(t['with_source_video_key'], denom)}")
    print(f"  file present on disk:         {fmt_pct(t['with_source_video_file'], denom)}")
    print(f"  Gate (file-present) ≥ {GATE_PCT:.0f}%:  {gate_verdict(t['with_source_video_file'], denom)}")
    print()
    print("== Phase 2b · smart-prefill gate ==")
    print(f"  transcript_raw key set:       {fmt_pct(t['with_transcript_raw_key'], denom)}")
    print(f"  raw_*.json on disk + parsed:  {fmt_pct(t['with_transcript_raw_file'], denom)}")
    print(f"  word-level speaker labels:    {fmt_pct(t['with_speaker_labels'], denom)}")
    print(f"  Gate (speaker-labels) ≥ {GATE_PCT:.0f}%: {gate_verdict(t['with_speaker_labels'], denom)}")
    print()
    print("Raw transcript filename distribution:")
    for name, cnt in sorted(out["raw_filename_distribution"].items(), key=lambda kv: -kv[1]):
        print(f"  {name:40s}  {cnt}")
    print()
    print("Schema hint distribution:")
    for hint, cnt in sorted(out["schema_hint_distribution"].items(), key=lambda kv: -kv[1]):
        print(f"  {hint:40s}  {cnt}")
    print()
    print("---JSON---")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
