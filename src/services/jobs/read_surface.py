from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from services.jobs.models import JOB_STATUS_SUCCEEDED, JobRecord
from services.manifest_reader import (
    load_manifest_artifact_index,
    load_manifest_payload,
    resolve_manifest_path,
)


RESULT_OUTPUT_SPECS = (
    ("state.project", "project_state"),
    ("state.review", "review_state"),
    ("translation.segments", "translation_segments"),
    ("editor.dubbed_audio_complete", "dubbed_audio"),
    ("editor.subtitles", "subtitles"),
    ("publish.dubbed_video", "dubbed_video"),
)


@dataclass(slots=True)
class ManifestDerivedRead:
    project_dir: str | None
    manifest_path: str | None
    manifest_available: bool
    artifacts: list[dict[str, object]]


def build_job_artifacts_payload(record: JobRecord) -> dict[str, object]:
    derived = _build_manifest_derived_read(record)
    return {
        **_build_base_summary(record, derived=derived),
        "manifest": {
            "available": derived.manifest_available,
            "artifact_count": len(derived.artifacts),
        },
        "artifacts": list(derived.artifacts),
    }


def build_job_result_summary(record: JobRecord) -> dict[str, object]:
    derived = _build_manifest_derived_read(record)
    return {
        **_build_base_summary(record, derived=derived),
        "manifest": {
            "available": derived.manifest_available,
            "artifact_count": len(derived.artifacts),
        },
        "outputs": _build_output_summary(record=record, artifacts=derived.artifacts),
        "artifacts": _build_artifact_summary(derived.artifacts),
    }


def _build_base_summary(record: JobRecord, *, derived: ManifestDerivedRead) -> dict[str, object]:
    return {
        "job_id": record.job_id,
        "status": record.status,
        "project_dir": derived.project_dir,
        "manifest_path": derived.manifest_path,
        "review_gate": _copy_optional_dict(record.review_gate),
        "error_summary": _copy_optional_dict(record.error_summary),
        "fallback_summary": _copy_optional_dict(record.fallback_summary),
    }


def _build_manifest_derived_read(record: JobRecord) -> ManifestDerivedRead:
    project_dir = _resolve_project_dir(record.project_dir)
    manifest_path = _resolve_job_manifest_path(record=record, project_dir=project_dir)
    if project_dir is None and manifest_path is not None:
        project_dir = manifest_path.parent.resolve(strict=False)

    manifest_payload = load_manifest_payload(
        project_dir=project_dir,
        manifest_path=manifest_path,
    )
    artifact_index = load_manifest_artifact_index(
        project_dir=project_dir,
        manifest_path=manifest_path,
        manifest_payload=manifest_payload,
    )
    artifact_entries = _build_artifact_entries(
        project_dir=project_dir,
        artifact_index=artifact_index,
    )
    return ManifestDerivedRead(
        project_dir=str(project_dir) if project_dir is not None else None,
        manifest_path=str(manifest_path) if manifest_path is not None else None,
        manifest_available=manifest_payload is not None,
        artifacts=artifact_entries,
    )


def _resolve_project_dir(project_dir: str | None) -> Path | None:
    normalized_project_dir = _normalize_optional_text(project_dir)
    if normalized_project_dir is None:
        return None
    return Path(normalized_project_dir).expanduser().resolve(strict=False)


def _resolve_job_manifest_path(*, record: JobRecord, project_dir: Path | None) -> Path | None:
    explicit_manifest_path = None
    normalized_manifest_path = _normalize_optional_text(record.manifest_path)
    if normalized_manifest_path is not None:
        explicit_manifest_path = resolve_manifest_path(manifest_path=normalized_manifest_path)
        if explicit_manifest_path is not None and explicit_manifest_path.exists():
            return explicit_manifest_path

    project_manifest_path = None
    if project_dir is not None:
        project_manifest_path = resolve_manifest_path(project_dir=project_dir)
        if project_manifest_path is not None and project_manifest_path.exists():
            return project_manifest_path

    return explicit_manifest_path or project_manifest_path


def _build_artifact_entries(
    *,
    project_dir: Path | None,
    artifact_index: dict[str, str],
) -> list[dict[str, object]]:
    artifact_entries: list[dict[str, object]] = []
    for key, declared_path in sorted(artifact_index.items()):
        resolved_path = _resolve_declared_artifact_path(
            project_dir=project_dir,
            declared_path=declared_path,
        )
        artifact_entries.append(
            {
                "key": key,
                "category": key.split(".", 1)[0],
                "declared_path": declared_path,
                "path": str(resolved_path) if resolved_path is not None else declared_path,
                "exists": bool(resolved_path is not None and resolved_path.exists()),
            }
        )
    return artifact_entries


def _resolve_declared_artifact_path(*, project_dir: Path | None, declared_path: str) -> Path | None:
    normalized_declared_path = _normalize_optional_text(declared_path)
    if normalized_declared_path is None:
        return None

    artifact_path = Path(normalized_declared_path).expanduser()
    if artifact_path.is_absolute():
        return artifact_path.resolve(strict=False)
    if project_dir is None:
        return None
    return (project_dir / artifact_path).resolve(strict=False)


def _build_output_summary(
    *,
    record: JobRecord,
    artifacts: list[dict[str, object]],
) -> list[dict[str, object]]:
    if record.status != JOB_STATUS_SUCCEEDED:
        return []

    artifacts_by_key = {
        str(artifact.get("key") or ""): artifact for artifact in artifacts if isinstance(artifact, dict)
    }
    outputs: list[dict[str, object]] = []
    for artifact_key, output_name in RESULT_OUTPUT_SPECS:
        artifact = artifacts_by_key.get(artifact_key)
        if artifact is None:
            continue
        outputs.append(
            {
                "name": output_name,
                "key": artifact_key,
                "category": artifact.get("category"),
                "path": artifact.get("path"),
                "exists": bool(artifact.get("exists")),
            }
        )
    return outputs


def _build_artifact_summary(artifacts: list[dict[str, object]]) -> dict[str, object]:
    category_counts: dict[str, dict[str, int | str]] = {}
    existing_count = 0
    for artifact in artifacts:
        category = str(artifact.get("category") or "other")
        if bool(artifact.get("exists")):
            existing_count += 1
        bucket = category_counts.setdefault(
            category,
            {
                "name": category,
                "count": 0,
                "existing_count": 0,
            },
        )
        bucket["count"] = int(bucket["count"]) + 1
        if bool(artifact.get("exists")):
            bucket["existing_count"] = int(bucket["existing_count"]) + 1

    return {
        "total_count": len(artifacts),
        "existing_count": existing_count,
        "categories": [category_counts[name] for name in sorted(category_counts)],
    }


def _copy_optional_dict(value: dict[str, object] | None) -> dict[str, object] | None:
    if value is None:
        return None
    return dict(value)


def _normalize_optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized_value = value.strip()
    return normalized_value or None
