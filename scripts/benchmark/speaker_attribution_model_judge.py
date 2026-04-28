#!/usr/bin/env python3
"""Adjudicate P2-b speaker attribution audit candidates with an audio model."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import time

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
for path in (REPO_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


DEFAULT_OUTPUT_DIR = Path("reports/benchmark")
DEFAULT_OUTPUT_STEM = "speaker_attribution_audit_judged"
DEFAULT_MODEL = "gemini_pro"


@dataclass(frozen=True)
class JudgeConfig:
    audit_batch: Path
    output_dir: Path = DEFAULT_OUTPUT_DIR
    output_stem: str = DEFAULT_OUTPUT_STEM
    review_model: str = DEFAULT_MODEL
    batch_size: int = 6
    start: int = 0
    limit: int = 0
    sleep_seconds: float = 0.0
    continue_on_error: bool = True
    force: bool = False


PROMPT = """\
You are an expert audio judge for speaker attribution in a video dubbing pipeline.

Task:
- For each candidate, listen to the attached audio clip first.
- Use the structured context only to understand the current ASR/S2 assignment.
- Decide whether the current assigned speaker is reasonable, should revert to ASR,
  should be reassigned to the main speaker, should be marked for manual review,
  or is music/non-speech.
- Do not infer from text alone when audio evidence is unclear.
- If there are multiple simultaneous voices, use decision `overlap` and recommend
  `mark_review` unless the assigned speaker is clearly correct.

Allowed decision values:
- `asr_speaker`: original ASR speaker is more likely correct.
- `s2_speaker`: current S2/final assigned speaker is more likely correct.
- `main_speaker`: the clip belongs to the dominant/main speaker.
- `distinct_speaker`: the assigned low-support speaker is a real distinct speaker.
- `overlap`: multiple voices overlap or attribution is inherently ambiguous.
- `music_or_non_speech`: this is music, singing, crowd noise, or non-speech.
- `uncertain`: insufficient audio evidence.

Allowed recommended_action values:
- `keep`
- `revert_to_asr`
- `reassign_to_main`
- `mark_review`
- `mark_non_speech`

Return JSON only:
{{
  "decisions": [
    {{
      "candidate_id": "job_x_cand_001",
      "decision": "s2_speaker",
      "confidence": "high|medium|low",
      "recommended_action": "keep",
      "reason": "brief reason based on audio"
    }}
  ]
}}

Candidates:
{candidates_json}
"""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _extract_text(response: object) -> str:
    text = getattr(response, "text", None)
    if text:
        return str(text)
    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", []) or []:
            value = getattr(part, "text", None)
            if value:
                return str(value)
    return ""


def _parse_json_response(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            return {"decisions": [], "parse_error": "no_json_object", "raw": text[:1000]}
        try:
            payload = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError as exc:
            return {"decisions": [], "parse_error": str(exc), "raw": text[:1000]}
    return payload if isinstance(payload, dict) else {"decisions": []}


def _load_candidates(path: Path, start: int, limit: int) -> list[dict[str, Any]]:
    payload = _read_json(path)
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return []
    result: list[dict[str, Any]] = []
    base_dir = path.parent
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        clip = Path(str(candidate.get("clip_path") or ""))
        if not clip.is_absolute():
            clip = base_dir.parent / clip if str(clip).startswith("benchmark/") else REPO_ROOT / clip
            if not clip.exists():
                clip = base_dir / str(candidate.get("clip_path") or "")
        if not clip.exists():
            continue
        candidate = dict(candidate)
        candidate["resolved_clip_path"] = clip.as_posix()
        result.append(candidate)
    if start:
        result = result[start:]
    return result[:limit] if limit else result


def _candidate_for_prompt(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": candidate.get("candidate_id"),
        "job_id": candidate.get("job_id"),
        "segment_id": candidate.get("segment_id"),
        "time_range_ms": [candidate.get("start_ms"), candidate.get("end_ms")],
        "assigned_speaker_id": candidate.get("assigned_speaker_id"),
        "assigned_display_name": candidate.get("assigned_display_name"),
        "primary_speaker_id": candidate.get("primary_speaker_id"),
        "original_asr_speaker_id": candidate.get("original_asr_speaker_id"),
        "speaker_profile": candidate.get("speaker_profile"),
        "reasons": candidate.get("reasons"),
        "source_text": candidate.get("source_text"),
        "context": candidate.get("context"),
    }


def _resolve_model_id(logical_name: str) -> str:
    from services.llm_registry import MODEL_REGISTRY

    info = MODEL_REGISTRY.get(logical_name)
    if not info:
        raise KeyError(f"unknown review model {logical_name!r}")
    if not info.get("supports_audio"):
        raise ValueError(f"review model {logical_name!r} does not support audio")
    if info.get("provider") != "gemini":
        raise ValueError(f"only gemini audio judge is implemented, got {info.get('provider')!r}")
    return str(info["api_model_id"])


def _judge_chunk(candidates: list[dict[str, Any]], *, review_model: str) -> dict[str, Any]:
    import importlib
    from services.gemini.client_factory import create_gemini_client

    types = importlib.import_module("google.genai.types")
    model_id = _resolve_model_id(review_model)
    client = create_gemini_client()
    candidates_json = json.dumps(
        [_candidate_for_prompt(candidate) for candidate in candidates],
        ensure_ascii=False,
        indent=2,
    )
    contents: list[Any] = []
    for candidate in candidates:
        contents.append(f"[candidate_id={candidate['candidate_id']}]")
        clip_path = Path(candidate["resolved_clip_path"])
        contents.append(
            types.Part.from_bytes(
                data=clip_path.read_bytes(),
                mime_type="audio/ogg",
            )
        )
    contents.append(PROMPT.format(candidates_json=candidates_json))
    response = client.models.generate_content(
        model=model_id,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.0,
            max_output_tokens=8192,
            thinking_config=types.ThinkingConfig(thinking_budget=1024),
        ),
    )
    raw_text = _extract_text(response)
    parsed = _parse_json_response(raw_text)
    return {
        "model": review_model,
        "model_id": model_id,
        "candidate_ids": [candidate["candidate_id"] for candidate in candidates],
        "raw_text": raw_text,
        "parsed": parsed,
    }


def build_judgement(config: JudgeConfig) -> dict[str, Any]:
    candidates = _load_candidates(config.audit_batch, config.start, config.limit)
    chunks = [
        candidates[index:index + config.batch_size]
        for index in range(0, len(candidates), config.batch_size)
    ]
    responses: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        try:
            response = _judge_chunk(chunk, review_model=config.review_model)
        except Exception as exc:
            error = {
                "chunk_index": index,
                "candidate_ids": [candidate["candidate_id"] for candidate in chunk],
                "error_type": type(exc).__name__,
                "error": str(exc)[:1000],
            }
            responses.append({"error": error, "candidate_ids": error["candidate_ids"]})
            errors.append(error)
            if not config.continue_on_error:
                raise
            if config.sleep_seconds:
                time.sleep(config.sleep_seconds)
            continue
        responses.append(response)
        parsed_decisions = response.get("parsed", {}).get("decisions")
        if isinstance(parsed_decisions, list):
            for decision in parsed_decisions:
                if isinstance(decision, dict):
                    decisions.append(decision)
        if config.sleep_seconds:
            time.sleep(config.sleep_seconds)
    decision_counts = Counter(str(item.get("decision") or "unknown") for item in decisions)
    action_counts = Counter(str(item.get("recommended_action") or "unknown") for item in decisions)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "audit_batch": config.audit_batch.as_posix(),
        "review_model": config.review_model,
        "summary": {
            "candidates_loaded": len(candidates),
            "start": config.start,
            "limit": config.limit,
            "chunks": len(chunks),
            "decisions": len(decisions),
            "errors": len(errors),
            "decision_counts": dict(decision_counts.most_common()),
            "recommended_action_counts": dict(action_counts.most_common()),
        },
        "decisions": decisions,
        "errors": errors,
        "responses": responses,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "# P2-b Speaker Attribution Model Judgement",
        "",
        f"- Generated at: `{payload.get('generated_at')}`",
        f"- Audit batch: `{payload.get('audit_batch')}`",
        f"- Review model: `{payload.get('review_model')}`",
        f"- Candidates loaded: `{summary.get('candidates_loaded')}`",
        f"- Start: `{summary.get('start')}`",
        f"- Limit: `{summary.get('limit')}`",
        f"- Decisions: `{summary.get('decisions')}`",
        f"- Errors: `{summary.get('errors')}`",
        "",
        "## Decision Counts",
        "",
        "| Decision | Count |",
        "| --- | ---: |",
    ]
    for decision, count in (summary.get("decision_counts") or {}).items():
        lines.append(f"| `{decision}` | {count} |")
    lines.extend(["", "## Recommended Actions", "", "| Action | Count |", "| --- | ---: |"])
    for action, count in (summary.get("recommended_action_counts") or {}).items():
        lines.append(f"| `{action}` | {count} |")
    lines.extend(["", "## Decisions", "", "| Candidate | Decision | Confidence | Action | Reason |", "| --- | --- | --- | --- | --- |"])
    for decision in payload.get("decisions") or []:
        reason = str(decision.get("reason") or "").replace("|", "\\|")[:180]
        lines.append(
            "| `{cid}` | {decision} | {confidence} | {action} | {reason} |".format(
                cid=decision.get("candidate_id"),
                decision=decision.get("decision"),
                confidence=decision.get("confidence"),
                action=decision.get("recommended_action"),
                reason=reason,
            )
        )
    return "\n".join(lines)


def write_outputs(payload: dict[str, Any], config: JudgeConfig) -> tuple[Path, Path]:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = config.output_dir / f"{config.output_stem}.json"
    md_path = config.output_dir / f"{config.output_stem}.md"
    if not config.force:
        for path in (json_path, md_path):
            if path.exists():
                raise FileExistsError(f"{path} exists; pass --force to overwrite")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    return json_path, md_path


def parse_args() -> JudgeConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-batch", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--output-stem", default=DEFAULT_OUTPUT_STEM)
    parser.add_argument("--review-model", default=DEFAULT_MODEL)
    parser.add_argument("--batch-size", type=int, default=6)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    return JudgeConfig(
        audit_batch=args.audit_batch,
        output_dir=args.output_dir,
        output_stem=args.output_stem,
        review_model=args.review_model,
        batch_size=max(1, args.batch_size),
        start=max(0, args.start),
        limit=max(0, args.limit),
        sleep_seconds=max(0.0, args.sleep_seconds),
        continue_on_error=not args.stop_on_error,
        force=args.force,
    )


def main() -> int:
    config = parse_args()
    payload = build_judgement(config)
    json_path, md_path = write_outputs(payload, config)
    print(
        json.dumps(
            {
                "json": json_path.as_posix(),
                "markdown": md_path.as_posix(),
                **(payload.get("summary") or {}),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
