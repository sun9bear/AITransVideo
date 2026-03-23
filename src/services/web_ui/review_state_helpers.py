from __future__ import annotations

from pathlib import Path

from core.exceptions import StateError
from services.project_state_summary import (
    build_empty_project_state_summary,
    build_project_state_summary,
)
from services.review_state import ReviewStateManager
from services.state_manager import StateManager

from .output_entries import _resolve_artifact_path, _resolve_review_state_path


def _load_project_state_summary(project_dir: Path) -> dict[str, object]:
    state_path = _resolve_artifact_path(project_dir, "state.project") or (
        project_dir / "project_state.json"
    ).resolve(strict=False)
    snapshot = build_empty_project_state_summary()
    if not state_path.exists():
        return snapshot

    state_manager = StateManager(str(state_path))
    try:
        state = state_manager.load()
    except StateError as exc:
        snapshot["path"] = str(state_path)
        snapshot["load_error"] = str(exc)
        return snapshot
    return build_project_state_summary(state, state_path=str(state_path))


def _build_review_flow_snapshot(project_dir: Path) -> dict[str, object]:
    review_state_path = _resolve_review_state_path(project_dir)
    snapshot: dict[str, object] = {
        "path": str(review_state_path),
        "load_error": None,
        "active_stage": None,
        "active_review": None,
        "stages": {},
    }
    review_state_manager = ReviewStateManager(review_state_path)
    try:
        state = review_state_manager.load()
    except StateError as exc:
        snapshot["load_error"] = str(exc)
        return snapshot

    stages = state.get("stages", {})
    active_stage = state.get("active_stage")
    active_review = stages.get(active_stage) if isinstance(stages, dict) and active_stage else None
    snapshot["active_stage"] = active_stage
    snapshot["active_review"] = active_review
    snapshot["stages"] = stages if isinstance(stages, dict) else {}
    return snapshot


def _load_review_stage_payload(project_dir: Path, stage_name: str) -> dict[str, object] | None:
    review_state_manager = ReviewStateManager(_resolve_review_state_path(project_dir))
    try:
        stage_payload = review_state_manager.get_stage(stage_name)
    except StateError:
        return None
    if not stage_payload:
        return None
    payload = stage_payload.get("payload")
    return payload if isinstance(payload, dict) else None
