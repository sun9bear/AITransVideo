"""Smart MVP handoff helper — F2 of the skeleton.

A Smart pipeline that downgrades to Studio (per plan §6.5) MUST emit
**three things** before returning a paused result, otherwise the
process_runner will silently finalise the job as ``succeeded`` even
though the user is supposed to take over manually:

1. ``review_state_manager.set_stage(stage, status=PENDING, payload=...)``
   — the existing review-state contract Studio already uses
2. ``[SMART_STATE] {"status": "downgraded_to_studio", ...}``
   — F1 marker so JobRecord.smart_state reflects the handoff
3. ``[WEB_REVIEW] {"stage": ..., "project_dir": ..., "message": ...}``
   — the existing web review marker that
   ``process_runner._parse_web_review_marker`` recognises and turns
   into ``JOB_STATUS_WAITING_FOR_REVIEW``

Skipping (3) is the bug plan §6.0.5 "F2 BLOCKER" warns about: without
the web review marker the runner ``_finalize_process`` sees
``returncode == 0`` and writes ``status=succeeded`` (process_runner.py
:610-639), even though smart_state.status="downgraded_to_studio".
The QA dashboard would show a green job that the user is actually
supposed to be reviewing.

This module bundles all three so callers can't accidentally do only
two of three. Pipeline call site is roughly:

    from services.smart.handoff import emit_handoff_markers
    emit_handoff_markers(
        review_state_manager=review_state_manager,
        review_stage=VOICE_SELECTION_REVIEW_STAGE,
        review_payload=vs_payload,
        smart_state_update={"status": "downgraded_to_studio",
                            "reason": "translation_auto_approve_failed",
                            "handoff_stage": "voice_selection_review"},
        project_dir=final_project_dir,
        user_message="智能版自动流程已停止,请人工接管",
        web_review_marker_builder=self._build_web_review_marker,
    )
    return self._build_paused_result(...)

The ``web_review_marker_builder`` argument is injected from the
pipeline class so this module doesn't need to import process.py
internals (avoids circular import).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from services.smart.state import emit_smart_state_marker


def emit_handoff_markers(
    *,
    review_state_manager: Any,
    review_stage: str,
    review_payload: Mapping[str, Any] | None,
    review_pending_status: Any,
    smart_state_update: Mapping[str, Any],
    project_dir: Path,
    user_message: str,
    web_review_marker_builder: Callable[..., str],
) -> None:
    """Emit the full Smart handoff marker triple.

    Side effects (in order):
      1. ``review_state_manager.set_stage(stage, status=PENDING, payload=...)``
      2. ``emit_smart_state_marker(smart_state_update)``
      3. ``print(web_review_marker_builder(stage=..., project_dir=...,
         message=...))``

    Caller must subsequently ``return self._build_paused_result(...)``
    so the pipeline frame exits — the runner picks up the web_review
    marker and writes ``JOB_STATUS_WAITING_FOR_REVIEW``.

    Notes:
      - smart_state_update should at minimum carry ``status`` and
        ``reason``; ``handoff_stage`` and ``credits_policy`` SHOULD
        be present per plan §4.3 mapping table.
      - The ``review_pending_status`` argument is the
        ReviewStateManager's pending-status enum value — passed in
        rather than imported here to avoid coupling smart/ to the
        ReviewStateManager module's import path.
      - ``review_payload`` may be None when the handoff happens
        BEFORE a review payload was built (e.g. speaker gate fail
        rejecting the job entirely). In that case set_stage still
        records the handoff for audit, with empty payload.
    """
    # 1. Update review state — same contract as Studio human-review
    review_state_manager.set_stage(
        review_stage,
        status=review_pending_status,
        payload=dict(review_payload) if review_payload else {},
        activate=True,
    )

    # 2. F1 smart_state marker — pipeline → process_runner → JobRecord
    emit_smart_state_marker(smart_state_update)

    # 3. Web review marker — runner sees this and writes
    #    JOB_STATUS_WAITING_FOR_REVIEW (without it the job finalises as
    #    succeeded; see plan §6.0.5 / §6.5).
    print(
        web_review_marker_builder(
            stage=review_stage,
            project_dir=project_dir,
            message=user_message,
        )
    )
