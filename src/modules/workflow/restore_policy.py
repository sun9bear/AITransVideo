from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from core.enums import StageStatus
from modules.workflow.stage_helpers import get_stage_payload_value, read_stage_payload
from services.state_manager import StateManager


@dataclass(slots=True)
class DraftRestoreDecision:
    reuse_allowed: bool
    restore_reason: str | None = None
    rerun_reason: str | None = None
    source_input_hash: str | None = None
    artifact_paths: list[str] = field(default_factory=list)
    reused_artifacts: list[str] = field(default_factory=list)
    restored_result: Any | None = None


def build_restore_audit_payload(
    *,
    source_input_hash: str | None,
    artifact_paths: list[str | None],
    reused_artifacts: list[str | None],
    restore_reason: str | None,
    rerun_reason: str | None,
) -> dict[str, object]:
    return {
        "source_input_hash": source_input_hash,
        "artifact_paths": _normalize_paths(artifact_paths),
        "reused_artifacts": _normalize_paths(reused_artifacts),
        "restore_reason": restore_reason,
        "rerun_reason": rerun_reason,
    }


def build_always_rerun_audit(
    *,
    source_input_hash: str | None,
    artifact_paths: list[str | None],
    rerun_reason: str,
) -> dict[str, object]:
    return build_restore_audit_payload(
        source_input_hash=source_input_hash,
        artifact_paths=artifact_paths,
        reused_artifacts=[],
        restore_reason=None,
        rerun_reason=rerun_reason,
    )


def build_cache_restore_audit(
    *,
    stage_name: str,
    previous_payload: dict[str, object] | None,
    source_input_hash: str | None,
    context_fields: dict[str, object],
    total_units: int,
    cache_hits: int,
    artifact_paths: list[str | None],
    reused_artifacts: list[str | None],
) -> dict[str, object]:
    restore_reason = None
    rerun_reason = _resolve_cache_rerun_reason(
        previous_payload=previous_payload,
        source_input_hash=source_input_hash,
        context_fields=context_fields,
    )

    if total_units <= 0:
        rerun_reason = rerun_reason or f"{stage_name}_no_work"
    elif cache_hits >= total_units:
        restore_reason = f"{stage_name}_cache_restore_full"
    elif cache_hits > 0:
        restore_reason = f"{stage_name}_cache_restore_partial"
        rerun_reason = rerun_reason or f"{stage_name}_cache_partial_miss"
    else:
        rerun_reason = rerun_reason or f"{stage_name}_cache_miss"

    return build_restore_audit_payload(
        source_input_hash=source_input_hash,
        artifact_paths=artifact_paths,
        reused_artifacts=reused_artifacts,
        restore_reason=restore_reason,
        rerun_reason=rerun_reason,
    )


def evaluate_draft_restore(
    state_manager: StateManager,
    load_existing_result: Callable[[], Any | None],
) -> DraftRestoreDecision:
    current_input_hash = get_stage_payload_value(state_manager, "ingestion", "input_hash")
    existing_stage = state_manager.get_stage("draft")
    if existing_stage is None or existing_stage.get("status") != StageStatus.DONE.value:
        return DraftRestoreDecision(
            reuse_allowed=False,
            rerun_reason="draft_stage_state_missing_or_not_done",
            source_input_hash=_read_optional_text(current_input_hash),
        )

    existing_payload = read_stage_payload(existing_stage)
    normalized_input_hash = _read_optional_text(current_input_hash)
    previous_input_hash = _read_optional_text(existing_payload.get("source_input_hash"))
    if normalized_input_hash is not None and previous_input_hash != normalized_input_hash:
        return DraftRestoreDecision(
            reuse_allowed=False,
            rerun_reason="source_input_hash_changed",
            source_input_hash=normalized_input_hash,
        )

    translation_mode = _read_optional_text(get_stage_payload_value(state_manager, "translation", "execution_mode"))
    if translation_mode is not None and translation_mode not in {"cache_restore_full", "no_work"}:
        return DraftRestoreDecision(
            reuse_allowed=False,
            rerun_reason="upstream_translation_not_reusable",
            source_input_hash=normalized_input_hash,
        )

    alignment_mode = _read_optional_text(get_stage_payload_value(state_manager, "alignment", "execution_mode"))
    if alignment_mode is not None and alignment_mode not in {"cache_restore_full", "no_work"}:
        return DraftRestoreDecision(
            reuse_allowed=False,
            rerun_reason="upstream_alignment_not_reusable",
            source_input_hash=normalized_input_hash,
        )

    restored_result = load_existing_result()
    if restored_result is None:
        return DraftRestoreDecision(
            reuse_allowed=False,
            rerun_reason="draft_artifacts_missing_or_invalid",
            source_input_hash=normalized_input_hash,
        )

    artifact_paths = _normalize_paths(
        [
            getattr(restored_result, "draft_content_path", None),
            getattr(restored_result, "draft_meta_info_path", None),
            getattr(restored_result, "export_path", None),
        ]
    )
    return DraftRestoreDecision(
        reuse_allowed=True,
        restore_reason="existing_draft_artifacts_valid",
        source_input_hash=normalized_input_hash,
        artifact_paths=artifact_paths,
        reused_artifacts=artifact_paths,
        restored_result=restored_result,
    )


def _resolve_cache_rerun_reason(
    *,
    previous_payload: dict[str, object] | None,
    source_input_hash: str | None,
    context_fields: dict[str, object],
) -> str | None:
    if previous_payload is None:
        return None

    previous_input_hash = _read_optional_text(previous_payload.get("source_input_hash"))
    if source_input_hash is not None and previous_input_hash is not None and previous_input_hash != source_input_hash:
        return "source_input_hash_changed"

    for key, current_value in context_fields.items():
        if previous_payload.get(key) != current_value:
            return "provider_context_changed"
    return None


def _normalize_paths(paths: list[str | None]) -> list[str]:
    return [path for path in paths if isinstance(path, str) and path]


def _read_optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
