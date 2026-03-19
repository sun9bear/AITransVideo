from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_manifest_payload(
    *,
    project_dir: Path | None = None,
    manifest_path: Path | str | None = None,
) -> dict[str, Any] | None:
    path = resolve_manifest_path(project_dir=project_dir, manifest_path=manifest_path)
    if path is None or not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def load_manifest_artifact_index(
    *,
    project_dir: Path | None = None,
    manifest_path: Path | str | None = None,
    manifest_payload: dict[str, Any] | None = None,
) -> dict[str, str]:
    payload = manifest_payload if isinstance(manifest_payload, dict) else load_manifest_payload(
        project_dir=project_dir,
        manifest_path=manifest_path,
    )
    if payload is None:
        return {}
    artifact_index = payload.get("artifact_index")
    if not isinstance(artifact_index, dict):
        return {}

    normalized_artifact_index: dict[str, str] = {}
    for raw_key, raw_path in artifact_index.items():
        key = str(raw_key or "").strip()
        path = _normalize_optional_text(raw_path)
        if key and path is not None:
            normalized_artifact_index[key] = path
    return normalized_artifact_index


def resolve_manifest_artifact_path(
    project_dir: Path,
    artifact_key: str,
    *,
    artifact_index: dict[str, str] | None = None,
    manifest_payload: dict[str, Any] | None = None,
) -> Path | None:
    resolved_artifact_index = (
        artifact_index
        if artifact_index is not None
        else load_manifest_artifact_index(project_dir=project_dir, manifest_payload=manifest_payload)
    )
    raw_path = resolved_artifact_index.get(artifact_key)
    normalized_path = _normalize_optional_text(raw_path)
    if normalized_path is None:
        return None

    artifact_path = Path(normalized_path).expanduser()
    if not artifact_path.is_absolute():
        artifact_path = (project_dir / artifact_path).resolve(strict=False)
    else:
        artifact_path = artifact_path.resolve(strict=False)
    if not artifact_path.exists():
        return None
    return artifact_path


def resolve_manifest_path(
    *,
    project_dir: Path | None = None,
    manifest_path: Path | str | None = None,
) -> Path | None:
    if manifest_path is not None:
        normalized_manifest_path = _normalize_optional_text(str(manifest_path))
        if normalized_manifest_path is None:
            return None
        return Path(normalized_manifest_path).expanduser().resolve(strict=False)
    if project_dir is None:
        return None
    return (project_dir / "manifest.json").resolve(strict=False)


def _normalize_optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized
