"""Smart MVP P3-c Codex 第三十八轮 P1: synthesize a minimal quality_report
payload from ``smart_decisions.jsonl`` for handoff jobs.

Decision log §P3-a scope-down: smart jobs that hit handoff BEFORE
reaching happy-path terminal don't emit ``smart_quality_report.json``
— their audit lives in ``smart_decisions.jsonl`` as
``downgrade_handoff`` events. The user-facing P3-c renderer needs
SOMETHING to show those users (status + reason + stage) so they know
the job needs Studio takeover, not "正在处理中" misleading text.

This module is the bridge: read JSONL, find ``downgrade_handoff``
events, synthesize a schema_version=1 quality_report dict the Job API
endpoint can serve. Pure: no network / no DB / no provider call.

Failure semantics: returns ``None`` when there are no handoff events
(truly in-flight smart job — frontend gets 404 + shows "处理中").
Malformed JSONL lines are skipped, not fatal.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def synthesize_quality_report_from_jsonl(
    audit_dir: Path,
    *,
    job_id: str,
    user_id: str = "",
) -> dict[str, Any] | None:
    """Read ``{audit_dir}/smart_decisions.jsonl`` and synthesize a
    minimal-but-schema-valid quality_report payload IF the JSONL
    contains at least one ``downgrade_handoff`` event.

    Returns:
      - ``dict`` with schema_version=1 + populated ``handoff_history``
        and ``smart_state_final={status: downgraded_to_studio, ...}``
        + ``speaker_summary`` from any ``speaker_gate`` event.
      - ``None`` when the JSONL doesn't exist, is empty, or has no
        ``downgrade_handoff`` events.

    Defensive: malformed JSONL lines are skipped (parser uses
    ``json.JSONDecodeError`` catch per-line).
    """
    jsonl_path = audit_dir / "smart_decisions.jsonl"
    if not jsonl_path.is_file():
        return None

    handoff_events: list[dict[str, Any]] = []
    speaker_gate_event: dict[str, Any] | None = None

    try:
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            decision_type = event.get("decision_type")
            if decision_type == "downgrade_handoff":
                handoff_events.append(event)
            elif decision_type == "speaker_gate" and speaker_gate_event is None:
                speaker_gate_event = event
    except OSError:
        return None

    if not handoff_events:
        return None

    last = handoff_events[-1]

    # speaker_summary from speaker_gate (b3b) if available, else empty.
    speaker_summary: dict[str, Any] = {
        "main_speaker_count": 0,
        "main_speaker_ids": [],
        "excluded_speakers": [],
    }
    if speaker_gate_event is not None:
        evidence = speaker_gate_event.get("evidence") or {}
        if isinstance(evidence, dict):
            speaker_summary = {
                "main_speaker_count": int(
                    evidence.get("main_speaker_count") or 0
                ),
                "main_speaker_ids": list(
                    evidence.get("main_speaker_ids") or []
                ),
                "excluded_speakers": list(
                    evidence.get("excluded_speakers") or []
                ),
            }

    return {
        "schema_version": 1,
        "job_id": job_id,
        "user_id": user_id,
        "service_mode": "smart",
        "smart_state_final": {
            "status": "downgraded_to_studio",
            "credits_policy": "pending_settle",
            "reason": last.get("reason_code"),
        },
        "speaker_summary": speaker_summary,
        "voice_decisions": [],
        "translation_review": None,
        "retry_summary": {
            "rewrite_attempts_used": 0,
            "retts_attempts_used": 0,
            "budget_remaining_minutes": 0.0,
        },
        "handoff_history": [
            {
                "stage": str(
                    (h.get("extra") or {}).get("handoff_stage") or "unknown"
                ),
                "reason": str(h.get("reason_code") or ""),
                "occurred_at": str(h.get("created_at") or ""),
            }
            for h in handoff_events
        ],
        "generated_at": str(last.get("created_at") or ""),
        # Synthesized signal — admin tooling can distinguish from
        # pipeline-emitted reports.
        "synthesized_from_jsonl": True,
    }


__all__ = ["synthesize_quality_report_from_jsonl"]
