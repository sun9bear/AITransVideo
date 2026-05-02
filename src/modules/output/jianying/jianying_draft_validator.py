"""Jianying draft validator: inspects draft on-disk and produces compatibility report (Task J3).

Pure on-disk inspection — does NOT call pyJianYingDraft or modify the draft.
Writes jianying_compatibility_report.json with schema_version, engine info, draft metadata,
material status, track summary, and validation_status (passed / needs_review / failed).

Plan: docs/plans/2026-05-02-jianying-draft-delivery-integration-plan.md (J3)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from modules.output.jianying.jianying_draft_models import JianyingDraftRequest

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Engine version detection (clean env safe)
# ---------------------------------------------------------------------------


def _detect_pyjianyingdraft_version() -> str | None:
    """Detect pyJianYingDraft version; returns None if not installed."""
    try:
        from importlib.metadata import version, PackageNotFoundError
        try:
            return version("pyJianYingDraft")
        except PackageNotFoundError:
            return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Material checking
# ---------------------------------------------------------------------------


def _check_material_exists(draft_dir: Path, filename: str) -> tuple[bool, int | None]:
    """Check if a material file exists in materials/ subdirectory.

    Returns (found, size_bytes). size_bytes is None if not found.
    """
    mat_path = draft_dir / "materials" / filename
    if mat_path.is_file():
        return True, mat_path.stat().st_size
    return False, None


def _infer_material_filename(stem: str, request: JianyingDraftRequest) -> str | None:
    """Infer the expected filename for a material.

    For a given stem (e.g. 'source_video'), check if the requested path
    exists and return its filename with extension. If not found, return None.
    """
    if stem == "source_video":
        path = Path(request.source_video_path)
    elif stem == "dubbed_audio":
        path = Path(request.dubbed_audio_path)
    elif stem == "ambient_audio":
        path = Path(request.ambient_audio_path) if request.ambient_audio_path else None
    else:
        return None

    if path and path.is_file():
        return f"{stem}{path.suffix}"
    return None


# ---------------------------------------------------------------------------
# Draft content inspection
# ---------------------------------------------------------------------------


def _read_draft_content(draft_dir: Path) -> dict[str, Any] | None:
    """Read draft_content.json; return None if missing or malformed."""
    content_path = draft_dir / "draft_content.json"
    if not content_path.is_file():
        return None

    try:
        return json.loads(content_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return None


def _read_draft_meta(draft_dir: Path) -> dict[str, Any] | None:
    """Read draft_meta_info.json; return None if missing or malformed."""
    meta_path = draft_dir / "draft_meta_info.json"
    if not meta_path.is_file():
        return None

    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return None


def _extract_draft_info(content: dict[str, Any]) -> dict[str, Any]:
    """Extract draft info from draft_content.json.

    Returns dict with: platform_app_version, draft_id, duration_us, fps, width, height.
    """
    platform = content.get("platform", {})
    canvas = content.get("canvas", {})

    return {
        "platform_app_version": platform.get("app_version", "unknown"),
        "draft_id": content.get("id", "unknown"),
        "duration_us": content.get("duration", 0),
        "fps": content.get("fps", 30),
        "canvas_width": canvas.get("width", 0),
        "canvas_height": canvas.get("height", 0),
    }


def _extract_tracks(content: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract track summary from draft_content.json tracks array.

    Returns list of dicts: type, name, segment_count.
    """
    tracks = []
    for track in content.get("tracks", []):
        track_type = track.get("type", "unknown")
        track_name = track.get("name", "unnamed")
        segment_count = len(track.get("segments", []))
        tracks.append({
            "type": track_type,
            "name": track_name,
            "segment_count": segment_count,
        })
    return tracks


# ---------------------------------------------------------------------------
# Validation logic
# ---------------------------------------------------------------------------


def _validate_draft(
    draft_dir: Path,
    draft_zip_path: Path,
    request: JianyingDraftRequest,
    draft_content: dict[str, Any] | None,
    tracks: list[dict[str, Any]],
) -> tuple[str, list[dict[str, str]]]:
    """Determine validation_status and issues list.

    Returns (status, issues) where status is 'passed', 'needs_review', or 'failed'.
    issues is a list of dicts with keys: code, message.
    """
    issues: list[dict[str, str]] = []

    # Hard errors (failed)
    if draft_content is None:
        issues.append({
            "code": "malformed_draft_content",
            "message": "draft_content.json missing or invalid JSON",
        })
        return "failed", issues

    if not draft_zip_path.is_file():
        issues.append({
            "code": "zip_missing",
            "message": f"Zip file not found: {draft_zip_path}",
        })
        return "failed", issues

    zip_size = draft_zip_path.stat().st_size
    if zip_size < 1024:  # 1 KB
        issues.append({
            "code": "zip_too_small",
            "message": f"Zip file too small: {zip_size} bytes (expected >= 1 KB)",
        })
        return "failed", issues

    # Review items
    review_status = False

    # Check source_video material
    # Material is expected if the request path exists (i.e., was provided and accessible)
    source_expected = Path(request.source_video_path).is_file()
    if source_expected:
        expected_filename = _infer_material_filename("source_video", request)
        if expected_filename:
            found, _ = _check_material_exists(draft_dir, expected_filename)
            if not found:
                issues.append({
                    "code": "missing_video_material",
                    "message": f"Source video material not found: {expected_filename}",
                })
                review_status = True

    # Check ambient_audio material
    if request.ambient_audio_path:
        ambient_expected = Path(request.ambient_audio_path).is_file()
        if ambient_expected:
            expected_filename = _infer_material_filename("ambient_audio", request)
            if expected_filename:
                found, _ = _check_material_exists(draft_dir, expected_filename)
                if not found:
                    issues.append({
                        "code": "missing_ambient_material",
                        "message": f"Ambient audio material not found: {expected_filename}",
                    })
                    review_status = True

    # Check for empty text tracks
    for track in tracks:
        if track["type"] == "text" and track["segment_count"] == 0:
            issues.append({
                "code": "empty_text_track",
                "message": f"Text track '{track['name']}' has no segments",
            })
            review_status = True

    # Sanity check: at least one track
    if not tracks:
        issues.append({
            "code": "no_tracks",
            "message": "Draft has no tracks",
        })
        return "failed", issues

    # Determine final status
    if review_status:
        return "needs_review", issues

    return "passed", issues


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def write_compatibility_report(
    *,
    request: JianyingDraftRequest,
    draft_dir: Path,
    draft_zip_path: Path,
    output_root: Path,
    engine_name: str = "pyJianYingDraft",
    engine_version: str | None = None,
) -> Path:
    """Write jianying_compatibility_report.json and return the report path.

    Arguments:
        request: JianyingDraftRequest with project metadata.
        draft_dir: Path to the draft directory (contains draft_content.json, materials/, etc).
        draft_zip_path: Path to the draft zip file.
        output_root: Path to {output_dir}/jianying/ where report will be written.
        engine_name: Name of the engine (default "pyJianYingDraft").
        engine_version: Version string or None. If None, auto-detect.

    Returns:
        Path to the written jianying_compatibility_report.json.
    """
    draft_dir = Path(draft_dir)
    draft_zip_path = Path(draft_zip_path)
    output_root = Path(output_root)

    # Auto-detect engine version if not provided
    if engine_version is None:
        engine_version = _detect_pyjianyingdraft_version()

    # Read draft content and metadata
    draft_content = _read_draft_content(draft_dir)
    draft_meta = _read_draft_meta(draft_dir)

    # Extract draft info and tracks
    draft_info: dict[str, Any] = {}
    tracks: list[dict[str, Any]] = []

    if draft_content:
        draft_info = _extract_draft_info(draft_content)
        tracks = _extract_tracks(draft_content)

    # Get draft name from metadata
    draft_name = (draft_meta or {}).get("draft_name", "unknown")

    # Check materials
    materials: list[dict[str, Any]] = []

    # source_video
    source_expected = _infer_material_filename("source_video", request) is not None
    expected_filename = _infer_material_filename("source_video", request)
    found, size = _check_material_exists(draft_dir, expected_filename) if expected_filename else (False, None)
    materials.append({
        "key": "source_video",
        "expected": source_expected,
        "found": found,
        "filename": expected_filename if expected_filename else None,
        "size_bytes": size,
    })

    # dubbed_audio
    dubbed_expected = _infer_material_filename("dubbed_audio", request) is not None
    expected_filename = _infer_material_filename("dubbed_audio", request)
    found, size = _check_material_exists(draft_dir, expected_filename) if expected_filename else (False, None)
    materials.append({
        "key": "dubbed_audio",
        "expected": dubbed_expected,
        "found": found,
        "filename": expected_filename if expected_filename else None,
        "size_bytes": size,
    })

    # ambient_audio
    ambient_expected = request.ambient_audio_path is not None
    expected_filename = _infer_material_filename("ambient_audio", request)
    found, size = _check_material_exists(draft_dir, expected_filename) if expected_filename else (False, None)
    materials.append({
        "key": "ambient_audio",
        "expected": ambient_expected,
        "found": found,
        "filename": expected_filename if expected_filename else None,
        "size_bytes": size,
    })

    # subtitles (inlined, no separate file)
    materials.append({
        "key": "subtitles",
        "expected": True,
        "found": True,
        "filename": None,
        "size_bytes": None,
        "note": "inlined into draft_content.json by import_srt; no separate file in materials/",
    })

    # Validate
    validation_status, issues = _validate_draft(
        draft_dir,
        draft_zip_path,
        request,
        draft_content,
        tracks,
    )

    # Determine zip size
    zip_size = draft_zip_path.stat().st_size if draft_zip_path.is_file() else 0

    # Build report
    report = {
        "schema_version": "jianying_compatibility_report_v1",
        "project_id": request.project_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "engine": {
            "name": engine_name,
            "version": engine_version,
        },
        "draft": {
            "platform_app_version": draft_info.get("platform_app_version", "unknown"),
            "draft_id": draft_info.get("draft_id", "unknown"),
            "duration_us": draft_info.get("duration_us", 0),
            "draft_name": draft_name,
            "fps": draft_info.get("fps", 30),
            "canvas_width": draft_info.get("canvas_width", 0),
            "canvas_height": draft_info.get("canvas_height", 0),
        },
        "materials": materials,
        "tracks": tracks,
        "draft_zip_path": str(draft_zip_path),
        "draft_zip_size_bytes": zip_size,
        "validation_status": validation_status,
        "issues": issues,
    }

    # Write report
    output_root.mkdir(parents=True, exist_ok=True)
    report_path = output_root / "jianying_compatibility_report.json"

    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    logger.info(
        "wrote compatibility report: %s (status=%s, issues=%d)",
        report_path,
        validation_status,
        len(issues),
    )

    return report_path
