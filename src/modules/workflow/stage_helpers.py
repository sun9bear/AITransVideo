from typing import Any

from services.state_manager import StateManager


def build_artifacts_payload(
    kind: str,
    file_paths: list[str | None],
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    normalized_paths = [path for path in file_paths if isinstance(path, str) and path]
    artifacts: dict[str, object] = {
        "kind": kind,
        "file_paths": normalized_paths,
        "file_count": len(normalized_paths),
    }
    if extra:
        artifacts.update(extra)
    return artifacts


def resolve_cache_execution_mode(cache_hits: int, total_units: int) -> str:
    if total_units <= 0:
        return "no_work"
    if cache_hits <= 0:
        return "fresh_run"
    if cache_hits >= total_units:
        return "cache_restore_full"
    return "cache_restore_partial"


def read_stage_payload(stage: dict[str, Any]) -> dict[str, object]:
    payload = stage.get("payload", {})
    if isinstance(payload, dict):
        return payload
    return {}


def get_stage_payload_value(
    state_manager: StateManager,
    stage_name: str,
    key: str,
) -> object | None:
    stage = state_manager.get_stage(stage_name)
    if stage is None:
        return None
    payload = read_stage_payload(stage)
    return payload.get(key)
