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


def _build_per_job_report(fact: dict, decisions: list[dict], args=None) -> dict:
    stage_decisions = [d for d in decisions if d.get("decision_kind") == "stage"]
    segment_decisions = [d for d in decisions if d.get("decision_kind") == "segment"]

    # Eligibility
    elig = next((d for d in stage_decisions if d["stage_or_segment_id"] == "eligibility_gate"), None)
    smart_eligibility = "unevaluable"
    if elig and isinstance(elig["smart_decision"], dict):
        smart_eligibility = elig["smart_decision"].get("decision", "unevaluable")

    # Match counts (B7 → B9 wiring)
    stage_match = sum(1 for d in stage_decisions if d.get("match") is True)
    seg_match = sum(1 for d in segment_decisions if d.get("match") is True)
    smart_more = sum(1 for d in decisions if d.get("diff_kind") == "smart_more_aggressive")
    smart_less = sum(1 for d in decisions if d.get("diff_kind") == "smart_less_aggressive")
    orthogonal = sum(1 for d in decisions if d.get("diff_kind") == "orthogonal")

    # Unevaluable stages
    stages_unevaluable = []
    for d in stage_decisions:
        sd = d.get("smart_decision")
        if isinstance(sd, dict) and (sd.get("unevaluable") or sd.get("decision") == "unevaluable"):
            stages_unevaluable.append(d["stage_or_segment_id"])

    thresholds_used = {}
    if args is not None:
        thresholds_used = {
            "main_speaker_threshold": getattr(args, "main_speaker_threshold", None),
            "clone_min_seconds_soft": getattr(args, "clone_min_seconds_soft", None),
            "clone_min_seconds_preferred": getattr(args, "clone_min_seconds_preferred", None),
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "job_id": fact["job_id"],
        "smart_eligibility": smart_eligibility,
        "stage_decisions_count": len(stage_decisions),
        "stage_decisions_match": stage_match,
        "segment_decisions_count": len(segment_decisions),
        "segment_decisions_match": seg_match,
        "smart_more_aggressive_count": smart_more,
        "smart_less_aggressive_count": smart_less,
        "orthogonal_count": orthogonal,
        "stages_unevaluable": stages_unevaluable,
        "thresholds_used": thresholds_used,
        "warnings": [],
    }


def _stage_decision(kind: str, stage_id: str, smart_decision, evidence: dict | None = None) -> dict:
    """Build a stage-level decision record. studio_actual / match / diff_kind in B6/B7."""
    return {
        "schema_version": SCHEMA_VERSION,
        "decision_kind": "stage",
        "stage_or_segment_id": stage_id,
        "smart_decision": smart_decision,
        "studio_actual": None,         # Filled in B6
        "match": None,                  # Filled in B7
        "diff_kind": "pending",         # Filled in B7
        "evidence": evidence or {},
    }


def _decide_eligibility_gate(fact: dict, main_threshold: float) -> tuple[dict, dict]:
    """Returns (smart_decision, evidence). main_threshold like 0.10."""
    sct = (fact.get("speaker_stats") or {}).get("speaker_count_by_threshold") or {}
    key = f"{main_threshold:.2f}"
    main_count = sct.get(key)
    if not isinstance(main_count, int):
        return ({"decision": "unevaluable", "reason": "missing_speaker_stats"},
                {"fact_field_path": f"speaker_stats.speaker_count_by_threshold.{key}", "fact_value": main_count})
    if main_count > 3:
        return ({"decision": "reject_main_speakers_gt_3", "main_count": main_count},
                {"fact_field_path": f"speaker_stats.speaker_count_by_threshold.{key}", "fact_value": main_count})
    # Check clone sample insufficient
    css = fact.get("clone_sample_stats") or {}
    eligible = css.get("eligible_speakers", 0)
    if eligible < main_count:
        return ({"decision": "reject_clone_samples_insufficient",
                 "main_count": main_count, "eligible_speakers": eligible},
                {"fact_field_path": "clone_sample_stats.eligible_speakers", "fact_value": eligible})
    return ({"decision": "pass", "main_count": main_count},
            {"fact_field_path": f"speaker_stats.speaker_count_by_threshold.{key}", "fact_value": main_count})


def _decide_voice_sample_selection(fact: dict, soft_seconds: int, preferred_seconds: int) -> tuple:
    """Per-main-speaker clone vs preset decision."""
    css = fact.get("clone_sample_stats") or {}
    buckets = css.get("eligible_sample_count_buckets_by_speaker")
    if not isinstance(buckets, list):
        return ({"unevaluable": True, "reason": "missing_clone_samples"},
                {"fact_field_path": "clone_sample_stats.eligible_sample_count_buckets_by_speaker"})
    sct = (fact.get("speaker_stats") or {}).get("speaker_count_by_threshold") or {}
    main_count = sct.get("0.10", len(buckets))
    if not isinstance(main_count, int):
        main_count = len(buckets)
    decisions = []
    for i, bucket in enumerate(buckets[:main_count]):
        soft_key = f"≥{soft_seconds}s"
        pref_key = f"≥{preferred_seconds}s"
        if bucket.get(pref_key, 0) >= 1:
            decisions.append({"speaker_index": i, "choice": "clone", "reason": f"≥{preferred_seconds}s_sample_available"})
        elif bucket.get(soft_key, 0) >= 1:
            decisions.append({"speaker_index": i, "choice": "clone", "reason": f"≥{soft_seconds}s_sample_available_soft"})
        else:
            decisions.append({"speaker_index": i, "choice": "preset", "reason": "no_sufficient_sample"})
    return (decisions, {"fact_field_path": "clone_sample_stats.eligible_sample_count_buckets_by_speaker",
                        "main_count": main_count})


def _decide_clone_policy(voice_selection_decision) -> tuple:
    """List of speaker indices Smart would auto-clone."""
    if not isinstance(voice_selection_decision, list):
        return ({"unevaluable": True, "reason": "voice_selection_unevaluable"}, {})
    cloned = [d["speaker_index"] for d in voice_selection_decision if d.get("choice") == "clone"]
    return ({"auto_clone_main_speakers": cloned}, {"derived_from": "voice_sample_selection"})


# Threshold for uncertain_speaker_duration_share triggering manual review
TRANSLATION_REVIEW_UNCERTAIN_THRESHOLD = 0.10  # 10%
TRANSLATION_REVIEW_MIN_CLONE_ELIGIBLE_RATIO = 0.5  # at least half of asr speakers eligible


def _decide_translation_review(fact: dict) -> tuple:
    ss = fact.get("speaker_stats") or {}
    css = fact.get("clone_sample_stats") or {}
    uncertain = ss.get("uncertain_speaker_duration_share")
    asr_count = ss.get("asr_speaker_count")
    eligible = css.get("eligible_speakers")
    if uncertain is None or asr_count is None or eligible is None:
        return ({"decision": "unevaluable", "reason": "missing_signals"},
                {"missing": [k for k, v in (("uncertain_speaker_duration_share", uncertain),
                                              ("asr_speaker_count", asr_count),
                                              ("eligible_speakers", eligible)) if v is None]})
    if uncertain > TRANSLATION_REVIEW_UNCERTAIN_THRESHOLD:
        return ({"decision": "manual_review_required",
                 "reason": f"high_uncertain_speaker_share_{uncertain:.2f}"},
                {"uncertain_speaker_duration_share": uncertain})
    if asr_count > 0 and eligible / asr_count < TRANSLATION_REVIEW_MIN_CLONE_ELIGIBLE_RATIO:
        return ({"decision": "manual_review_required",
                 "reason": f"low_clone_eligible_ratio_{eligible}/{asr_count}"},
                {"eligible_speakers": eligible, "asr_speaker_count": asr_count})
    return ({"decision": "auto_approve",
             "reason": "uncertain_low_and_clone_eligible_high"},
            {"uncertain_speaker_duration_share": uncertain,
             "eligible_speakers": eligible, "asr_speaker_count": asr_count})


K_CN_CHARS_PER_SRC_MIN = 240  # default from spec §3.5


def _estimate_retry(segments: list[dict], source_duration_seconds: float | None) -> tuple:
    """§3.5 retry estimation v1."""
    if not segments:
        return ({"unevaluable": True, "reason": "no_segments"}, {"segments_count": 0})
    expected_retts_count = 0
    rewrite_count_total = 0
    for seg in segments:
        cn_text = seg.get("cn_text", "") or ""
        estimated_cn_chars = len(cn_text)
        start_ms = seg.get("start_ms", 0)
        end_ms = seg.get("end_ms", 0)
        if isinstance(start_ms, (int, float)) and isinstance(end_ms, (int, float)) and end_ms > start_ms:
            duration_min = (end_ms - start_ms) / 60000.0
            threshold = K_CN_CHARS_PER_SRC_MIN * duration_min * 1.05
            if estimated_cn_chars > threshold:
                expected_retts_count += 1
        rewrite_count_total += seg.get("rewrite_count", 0) or 0
    expected_retts_count += rewrite_count_total
    avg_segment_duration_s = 0.0
    if segments:
        durs = [(seg.get("end_ms", 0) - seg.get("start_ms", 0)) / 1000.0 for seg in segments]
        durs = [d for d in durs if d > 0]
        avg_segment_duration_s = sum(durs) / len(durs) if durs else 0.0
    would_hit_budget_cap = False
    if source_duration_seconds and source_duration_seconds > 0:
        retts_audio_estimate = expected_retts_count * avg_segment_duration_s
        would_hit_budget_cap = retts_audio_estimate > 1.5 * source_duration_seconds
    return ({"expected_retts_count": expected_retts_count,
             "expected_rewrite_count": rewrite_count_total,
             "would_hit_budget_cap": would_hit_budget_cap},
            {"k_cn_chars_per_src_min": K_CN_CHARS_PER_SRC_MIN,
             "rewrite_threshold_multiplier": 1.05,
             "budget_cap_multiplier": 1.5,
             "segments_count": len(segments)})


def _decide_subtitle_sync(fact: dict) -> tuple:
    w = fact.get("whisper") or {}
    if w.get("alignment_model") is None:
        return ({"unevaluable": True, "reason": "pre_phase_d_job_or_no_whisper"},
                {"whisper_alignment_model": None})
    aligned = w.get("whisper_aligned_cue_count") or 0
    fallback = w.get("proportional_fallback_cue_count") or 0
    total = aligned + fallback
    expected_fallback_ratio = (fallback / total) if total > 0 else 0.0
    return ({"whisper_align_recommended": True,
             "expected_fallback_ratio": round(expected_fallback_ratio, 4),
             "alignment_model": w.get("alignment_model")},
            {"whisper_aligned_cue_count": aligned,
             "proportional_fallback_cue_count": fallback})


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


def _classify_voice_id(voice_id: str) -> str:
    """Classify voice_id as 'cloned' / 'preset' / 'unknown'.

    Cloned voice ID patterns (per src/pipeline/process.py:3635-3638
    and src/services/tts/* heuristics):
    - "vt_*" prefix (canonical cloned ID prefix per _validate_cloned_voices)
    - "moss_audio_*" prefix (MiniMax cloned audio sample IDs)
    - UUID-like (>=32 chars with hyphens, e.g. cloned voice resource UUIDs)

    Preset patterns (must be explicit):
    - "preset_*" prefix (test fixtures + admin UI convention)

    Anything else -> "unknown" (NOT "preset" by default - that caused
    false "smart_more_aggressive" classifications on production data).

    NOTE: this helper MUST stay byte-for-byte identical with
    ``smart_shadow_eval_collector._classify_voice_id`` (enforced by
    ``test_classify_voice_id_consistency_across_p0_and_p1``). When this
    classification needs to be compared against smart's vocabulary
    (which uses "clone" not "cloned"), the caller is responsible for
    the cloned -> clone translation - see _extract_studio_actual.
    """
    if not voice_id or voice_id.lower() == "auto":
        return "unknown"
    # Cloned voice patterns (most specific first):
    if voice_id.startswith("vt_"):
        return "cloned"
    if voice_id.startswith("moss_audio_"):
        return "cloned"
    if len(voice_id) >= 32 and "-" in voice_id:
        return "cloned"
    # Explicit preset:
    if voice_id.startswith("preset_"):
        return "preset"
    # Unknown - don't default to preset
    return "unknown"


def _smart_choice_for_voice_id(voice_id: str) -> str:
    """Map voice_id classification onto smart's choice vocabulary.

    smart's _decide_voice_sample_selection emits "clone" / "preset", while
    _classify_voice_id (canonical, kept identical with collector) emits
    "cloned" / "preset" / "unknown". This helper translates between them
    so studio_actual choices compare cleanly with smart_decision choices.
    """
    cls = _classify_voice_id(voice_id)
    if cls == "cloned":
        return "clone"
    return cls


def _extract_studio_actual(stage_id: str, fact: dict, smart_decision) -> object:
    """Per §3.4 取数表 — what Studio ACTUALLY did, from existing facts only (no writes)."""
    if stage_id == "eligibility_gate":
        # Tautology: task completed in Studio = "pass"
        return "pass"
    if stage_id == "voice_sample_selection":
        acs = fact.get("actual_clone_stats") or {}
        voice_ids = acs.get("voice_ids_by_speaker") or []
        if not voice_ids:
            return "unknown"
        return [{"speaker_index": i,
                 "choice": _smart_choice_for_voice_id(vid),
                 "voice_id": vid}
                for i, vid in enumerate(voice_ids)]
    if stage_id == "clone_policy":
        acs = fact.get("actual_clone_stats") or {}
        voice_ids = acs.get("voice_ids_by_speaker") or []
        if not voice_ids:
            return "unknown"
        cloned_indices = [i for i, vid in enumerate(voice_ids)
                          if _classify_voice_id(vid) == "cloned"]
        # Surface unknown speakers explicitly so _classify_diff can degrade
        # to no_studio_signal rather than silently dropping them and reporting
        # smart_more_aggressive (Codex Gate 2 follow-up).
        unknown_indices = [i for i, vid in enumerate(voice_ids)
                           if _classify_voice_id(vid) == "unknown"]
        return {"cloned_speaker_indices": cloned_indices,
                "unknown_speaker_indices": unknown_indices}
    if stage_id == "translation_review_auto_approval":
        ue = fact.get("user_edits") or {}
        tc = ue.get("text_changes_effective")
        if tc is None:
            return "unknown"
        return "auto_approved" if tc == 0 else "user_modified"
    if stage_id == "tts_duration_repair_policy":
        rs = fact.get("retry_stats") or {}
        ds = rs.get("_data_source")
        if ds not in ("metering", "fallback_editor_segments"):
            return "unknown"
        return {
            "actual_retts_count": rs.get("retts_count"),
            "actual_retts_total_duration_ms": rs.get("retts_total_duration_ms"),
            "data_source": ds,
        }
    if stage_id == "subtitle_sync_policy":
        w = fact.get("whisper") or {}
        sm = w.get("alignment_model")
        ss = fact.get("subtitle_sync") or {}
        drift = ss.get("text_audio_drift_count")
        if sm is None and drift is None:
            return "unknown"
        return {"alignment_model": sm, "drift_count": drift}
    return "unknown"


def _per_segment_decisions(segments: list[dict], fact: dict) -> list[dict]:
    """Record per-segment decisions only for "interesting" segments per §3.2.

    Interesting if any of:
      - Smart would trigger expected_retts (long cn_text)
      - Smart would trigger expected_rewrite (rewrite_count > 0)
      - Studio user_edit_events touched it (TODO: needs project_dir read; in B8 v1 use fact.user_edits as proxy)
      - segment in subtitle drift list (fact.subtitle_sync.drift_block_ids)
    """
    out_decisions = []
    drift_block_ids = set((fact.get("subtitle_sync") or {}).get("drift_block_ids") or [])
    for seg in segments:
        seg_id = str(seg.get("segment_id", ""))
        if not seg_id:
            continue
        cn_text = seg.get("cn_text", "") or ""
        start_ms = seg.get("start_ms", 0)
        end_ms = seg.get("end_ms", 0)
        rewrite_count = seg.get("rewrite_count", 0) or 0
        # Long text trigger
        expected_retts = False
        retts_reason = ""
        if isinstance(start_ms, (int, float)) and isinstance(end_ms, (int, float)) and end_ms > start_ms:
            duration_min = (end_ms - start_ms) / 60000.0
            threshold = K_CN_CHARS_PER_SRC_MIN * duration_min * 1.05
            if len(cn_text) > threshold:
                expected_retts = True
                retts_reason = f"cn_text_chars_{len(cn_text)}_exceeds_{threshold:.0f}"
        # Rewrite trigger
        expected_rewrite = rewrite_count > 0
        rewrite_reason = f"editor_rewrite_count_{rewrite_count}" if expected_rewrite else ""
        # Drift trigger
        drift_match = (f"block_{int(seg_id):04d}" in drift_block_ids
                        if seg_id.isdigit() else False)
        # Skip if not interesting
        if not (expected_retts or expected_rewrite or drift_match):
            continue
        smart = {}
        if expected_retts:
            smart["expected_retts"] = True
            smart["retts_reason"] = retts_reason
        if expected_rewrite:
            smart["expected_rewrite"] = True
            smart["rewrite_reason"] = rewrite_reason
        if drift_match:
            smart["drift"] = True
        out_decisions.append({
            "schema_version": SCHEMA_VERSION,
            "decision_kind": "segment",
            "stage_or_segment_id": f"segment_{seg_id}",
            "smart_decision": smart,
            "studio_actual": None,
            "match": None,
            "diff_kind": "pending",
            "evidence": {
                "cn_text_chars": len(cn_text),
                "duration_ms": end_ms - start_ms if end_ms > start_ms else 0,
                "rewrite_count": rewrite_count,
            },
        })
    return out_decisions


def _classify_diff(stage_id: str, smart_decision, studio_actual) -> tuple[bool | None, str]:
    """Returns (match, diff_kind). Per §3.4 / §3.3 enum.

    diff_kind:
      - match: smart_decision == studio_actual (semantic)
      - smart_more_aggressive: smart rejects/degrades while studio passed
      - smart_less_aggressive: smart passes while studio actually intervened (user_modified, etc.)
      - orthogonal: dimensions don't directly compare
      - no_studio_signal: studio_actual is "unknown" or unavailable
    """
    if studio_actual == "unknown":
        return None, "no_studio_signal"
    # Stage-specific equivalence
    if stage_id == "eligibility_gate":
        if isinstance(smart_decision, dict):
            sd = smart_decision.get("decision")
            if sd == "pass" and studio_actual == "pass":
                return True, "match"
            if sd in ("reject_main_speakers_gt_3", "reject_clone_samples_insufficient") and studio_actual == "pass":
                return False, "smart_more_aggressive"
            if sd == "unevaluable":
                return None, "no_studio_signal"
        return False, "orthogonal"
    if stage_id == "voice_sample_selection":
        if isinstance(smart_decision, list) and isinstance(studio_actual, list):
            if len(smart_decision) != len(studio_actual):
                return False, "orthogonal"
            # If ANY speaker in studio_actual is "unknown", we can't reliably diff.
            if any(a.get("choice") == "unknown" for a in studio_actual):
                return None, "no_studio_signal"
            choices_match = all(
                s.get("choice") == a.get("choice")
                for s, a in zip(smart_decision, studio_actual))
            if choices_match:
                return True, "match"
            smart_clones = sum(1 for s in smart_decision if s.get("choice") == "clone")
            actual_clones = sum(1 for a in studio_actual if a.get("choice") == "clone")
            if smart_clones > actual_clones:
                return False, "smart_more_aggressive"
            elif smart_clones < actual_clones:
                return False, "smart_less_aggressive"
            return False, "orthogonal"
        return None, "no_studio_signal"
    if stage_id == "clone_policy":
        if isinstance(smart_decision, dict) and isinstance(studio_actual, dict):
            # If ANY studio voice is unknown (can't tell if it was a clone
            # or a preset), we can't honestly compare set membership. Degrade
            # to no_studio_signal rather than silently treating unknown as
            # "not cloned" and reporting false smart_more_aggressive.
            unknown_indices = studio_actual.get("unknown_speaker_indices", [])
            if unknown_indices:
                return None, "no_studio_signal"
            smart_set = set(smart_decision.get("auto_clone_main_speakers", []))
            actual_set = set(studio_actual.get("cloned_speaker_indices", []))
            if smart_set == actual_set:
                return True, "match"
            if smart_set > actual_set:
                return False, "smart_more_aggressive"
            if smart_set < actual_set:
                return False, "smart_less_aggressive"
            return False, "orthogonal"
        return None, "no_studio_signal"
    if stage_id == "translation_review_auto_approval":
        sd = smart_decision.get("decision") if isinstance(smart_decision, dict) else None
        if sd == "unevaluable":
            return None, "no_studio_signal"
        # smart auto_approve + studio auto_approved → match
        # smart auto_approve + studio user_modified → smart_less_aggressive (smart should have flagged)
        # smart manual_review + studio auto_approved → smart_more_aggressive (smart over-cautious)
        # smart manual_review + studio user_modified → match (both flag)
        if sd == "auto_approve" and studio_actual == "auto_approved":
            return True, "match"
        if sd == "manual_review_required" and studio_actual == "user_modified":
            return True, "match"
        if sd == "auto_approve" and studio_actual == "user_modified":
            return False, "smart_less_aggressive"
        if sd == "manual_review_required" and studio_actual == "auto_approved":
            return False, "smart_more_aggressive"
        return False, "orthogonal"
    if stage_id == "tts_duration_repair_policy":
        if not isinstance(studio_actual, dict):
            return None, "no_studio_signal"
        if not isinstance(smart_decision, dict) or smart_decision.get("unevaluable"):
            return None, "no_studio_signal"
        smart_n = smart_decision.get("expected_retts_count", 0)
        actual_n = studio_actual.get("actual_retts_count")
        if actual_n is None:
            return None, "no_studio_signal"
        if smart_n == actual_n:
            return True, "match"
        # match if within 50% of actual (loose)
        if actual_n > 0 and abs(smart_n - actual_n) / max(1, actual_n) < 0.5:
            return True, "match"
        if smart_n > actual_n:
            return False, "smart_more_aggressive"
        return False, "smart_less_aggressive"
    if stage_id == "subtitle_sync_policy":
        if not isinstance(studio_actual, dict):
            return None, "no_studio_signal"
        if isinstance(smart_decision, dict) and smart_decision.get("unevaluable"):
            return None, "no_studio_signal"
        # If smart recommends whisper and studio used a whisper alignment_model → match
        if smart_decision.get("whisper_align_recommended") and studio_actual.get("alignment_model"):
            return True, "match"
        return False, "orthogonal"
    return None, "no_studio_signal"


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
            decisions: list[dict] = []
            # B2: stage decisions
            elig_dec, elig_ev = _decide_eligibility_gate(fact, args.main_speaker_threshold)
            decisions.append(_stage_decision("stage", "eligibility_gate", elig_dec, elig_ev))
            vs_dec, vs_ev = _decide_voice_sample_selection(
                fact, args.clone_min_seconds_soft, args.clone_min_seconds_preferred,
            )
            decisions.append(_stage_decision("stage", "voice_sample_selection", vs_dec, vs_ev))
            cp_dec, cp_ev = _decide_clone_policy(vs_dec)
            decisions.append(_stage_decision("stage", "clone_policy", cp_dec, cp_ev))
            tr_dec, tr_ev = _decide_translation_review(fact)
            decisions.append(_stage_decision("stage", "translation_review_auto_approval", tr_dec, tr_ev))
            retry_dec, retry_ev = _estimate_retry(segments, fact.get("duration_seconds"))
            decisions.append(_stage_decision("stage", "tts_duration_repair_policy", retry_dec, retry_ev))
            sub_dec, sub_ev = _decide_subtitle_sync(fact)
            decisions.append(_stage_decision("stage", "subtitle_sync_policy", sub_dec, sub_ev))
            # B6: extract studio_actual for each stage
            for d in decisions:
                d["studio_actual"] = _extract_studio_actual(
                    d["stage_or_segment_id"], fact, d["smart_decision"])
            # B7: classify match / diff_kind based on studio_actual
            for d in decisions:
                m, dk = _classify_diff(d["stage_or_segment_id"], d["smart_decision"], d["studio_actual"])
                d["match"] = m
                d["diff_kind"] = dk
            # B8: per-segment decisions for "interesting" segments only
            decisions += _per_segment_decisions(segments, fact)
            (job_dir / "smart_shadow_decisions.jsonl").write_text(
                "\n".join(json.dumps(d, ensure_ascii=False) for d in decisions) + ("\n" if decisions else ""),
                encoding="utf-8",
            )
            report = _build_per_job_report(fact, decisions, args)
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
