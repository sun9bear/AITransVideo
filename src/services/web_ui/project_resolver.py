from __future__ import annotations

import json
import mimetypes
from pathlib import Path

from services.manifest_reader import load_manifest_artifact_index, load_manifest_payload
from services.source_context_summary import (
    build_empty_source_context_summary,
    build_source_context_summary,
)
from services.project_state_summary import build_empty_project_state_summary
from services.review_state import REVIEW_STAGE_TAB_MAP
from services.jobs.models import JOB_STATUS_WAITING_FOR_REVIEW

from .config_helpers import _normalize_optional_text, _ensure_dict
from .constants import (
    DEFAULT_RESULT_PAGE_SIZE,
    PROJECT_AUDIO_FILE_SUFFIXES,
    PUBLIC_RESULT_DOWNLOAD_KEYS,
    RESULT_DOWNLOAD_KEY_MANIFEST,
    RESULT_PAGE_SIZE_OPTIONS,
    RESULT_SOURCE_LABELS,
    WINDOWS_PATH_PATTERN,
)
from .output_entries import (
    _build_editor_output_entries,
    _build_output_entry,
    _build_publish_output_entries,
    _resolve_artifact_path,
    _resolve_translation_segments_path,
)
from .review_state_helpers import (
    _build_review_flow_snapshot,
    _load_project_state_summary,
)
from .segment_loader import _build_segment_speaker_options
from .speaker_review import _load_transcript_review_items, _load_translation_review_items
from .utils import _copy_optional_mapping, _stringify_existing_path


def _build_results_snapshot(
    *,
    project_root: Path,
    job_snapshot: dict[str, object],
) -> dict[str, object]:
    project_dir, source = _resolve_project_dir_for_results(project_root=project_root, job_snapshot=job_snapshot)
    if project_dir is None:
        return {
            "available": False,
            "source": source,
            "source_label": _describe_results_source(source),
            "project_dir": None,
            "project_name": None,
            "source_context": build_empty_source_context_summary(),
            "workflow_note": "\u5f53\u524d\u8fd8\u6ca1\u6709\u53ef\u5c55\u793a\u7684\u9879\u76ee\u7ed3\u679c\u3002\u5148\u8fd0\u884c\u4e00\u6b21\u4efb\u52a1\uff0c\u6216\u7b49\u5f85\u5df2\u6709\u9879\u76ee\u4ea7\u7269\u88ab\u8bc6\u522b\u3002",
            "manifest_path": None,
            "editor_outputs": [],
            "publish_outputs": [],
            "needs_review": {
                "total_items": 0,
                "default_page_size": DEFAULT_RESULT_PAGE_SIZE,
                "page_size_options": list(RESULT_PAGE_SIZE_OPTIONS),
                "speaker_options": [],
                "items": [],
            },
            "transcript_review": {
                "total_items": 0,
                "default_page_size": DEFAULT_RESULT_PAGE_SIZE,
                "page_size_options": list(RESULT_PAGE_SIZE_OPTIONS),
                "speaker_options": [],
                "speaker_count": 0,
                "confirmed_count": 0,
                "needs_review_count": 0,
                "items": [],
            },
            "translation_review": {
                "total_items": 0,
                "default_page_size": DEFAULT_RESULT_PAGE_SIZE,
                "page_size_options": list(RESULT_PAGE_SIZE_OPTIONS),
                "speaker_options": [],
                "confirmed_count": 0,
                "rewrite_requested_count": 0,
                "existing_rewrite_count": 0,
                "items": [],
            },
            "audio_alignment": {
                "total_items": 0,
                "default_page_size": DEFAULT_RESULT_PAGE_SIZE,
                "page_size_options": list(RESULT_PAGE_SIZE_OPTIONS),
                "speaker_options": [],
                "items": [],
            },
            "review_flow": {
                "path": None,
                "load_error": None,
                "active_stage": None,
                "active_review": None,
                "stages": {},
            },
            "project_state": build_empty_project_state_summary(),
        }

    editor_outputs = _build_editor_output_entries(project_dir)
    publish_outputs = _build_publish_output_entries(project_dir)
    transcript_items = _load_transcript_review_items(project_dir)
    translation_items = _load_translation_review_items(project_dir)
    audio_alignment_items = translation_items
    needs_review_items = [item for item in translation_items if bool(item.get("needs_review"))]
    transcript_confirmed_count = sum(
        1
        for item in transcript_items
        if bool(item.get("speaker_confirmed")) and bool(item.get("transcript_confirmed"))
    )
    transcript_needs_review_count = sum(1 for item in transcript_items if bool(item.get("needs_review")))
    translation_confirmed_count = sum(1 for item in translation_items if bool(item.get("translation_confirmed")))
    translation_rewrite_requested_count = sum(1 for item in translation_items if bool(item.get("rewrite_requested")))
    translation_existing_rewrite_count = sum(1 for item in translation_items if int(item.get("rewrite_count") or 0) > 0)
    project_state = _load_project_state_summary(project_dir)
    manifest_payload = load_manifest_payload(project_dir=project_dir)
    manifest_path = _stringify_existing_path(project_dir / "manifest.json")
    review_flow = _build_review_flow_snapshot(project_dir)
    source_context = build_source_context_summary(
        manifest_payload=manifest_payload,
        fallback_locator=_normalize_optional_text(job_snapshot.get("youtube_url")),
    )
    project_name = source_context["video_title"] or project_dir.name
    available_output_count = sum(
        1
        for item in [*editor_outputs, *publish_outputs]
        if item.get("path")
    )
    return {
        "available": True,
        "source": source,
        "source_label": _describe_results_source(source),
        "project_dir": str(project_dir),
        "project_name": project_name,
        "source_context": source_context,
        "workflow_note": (
            "\u5f53\u524d Web UI \u4ecd\u4ee5 legacy process \u4e3a\u4e3b\uff0c\u6240\u4ee5\u7ed3\u679c\u9875\u4f18\u5148\u5c55\u793a editor \u4ea7\u7269\uff1b"
            "manifest \u548c publish \u4ea7\u7269\u4f1a\u5728\u9879\u76ee\u4e2d\u5b58\u5728\u65f6\u81ea\u52a8\u663e\u793a\u3002"
        ),
        "manifest_path": manifest_path,
        "available_output_count": available_output_count,
        "editor_outputs": editor_outputs,
        "publish_outputs": publish_outputs,
        "needs_review": {
            "total_items": len(needs_review_items),
            "default_page_size": DEFAULT_RESULT_PAGE_SIZE,
            "page_size_options": list(RESULT_PAGE_SIZE_OPTIONS),
            "speaker_options": _build_segment_speaker_options(needs_review_items),
            "items": needs_review_items,
        },
        "transcript_review": {
            "total_items": len(transcript_items),
            "default_page_size": DEFAULT_RESULT_PAGE_SIZE,
            "page_size_options": list(RESULT_PAGE_SIZE_OPTIONS),
            "speaker_options": _build_segment_speaker_options(transcript_items),
            "speaker_count": len(_build_segment_speaker_options(transcript_items)),
            "confirmed_count": transcript_confirmed_count,
            "needs_review_count": transcript_needs_review_count,
            "items": transcript_items,
        },
        "translation_review": {
            "total_items": len(translation_items),
            "default_page_size": DEFAULT_RESULT_PAGE_SIZE,
            "page_size_options": list(RESULT_PAGE_SIZE_OPTIONS),
            "speaker_options": _build_segment_speaker_options(translation_items),
            "confirmed_count": translation_confirmed_count,
            "rewrite_requested_count": translation_rewrite_requested_count,
            "existing_rewrite_count": translation_existing_rewrite_count,
            "items": translation_items,
        },
        "audio_alignment": {
            "total_items": len(audio_alignment_items),
            "default_page_size": DEFAULT_RESULT_PAGE_SIZE,
            "page_size_options": list(RESULT_PAGE_SIZE_OPTIONS),
            "speaker_options": _build_segment_speaker_options(audio_alignment_items),
            "items": audio_alignment_items,
        },
        "review_flow": review_flow,
        "project_state": project_state,
    }


def _resolve_project_dir_for_results(
    *,
    project_root: Path,
    job_snapshot: dict[str, object],
) -> tuple[Path | None, str]:
    projects_root = (project_root / "projects").resolve(strict=False)
    if not projects_root.exists():
        return None, "no_projects_root"

    explicit_project_dir = _normalize_optional_text(job_snapshot.get("project_dir"))
    if explicit_project_dir is not None:
        candidate_project_dir = Path(explicit_project_dir).resolve(strict=False)
        if candidate_project_dir.exists() and _path_is_within_root(candidate_project_dir, projects_root):
            return candidate_project_dir, "matched_youtube_url"

    youtube_url = _normalize_optional_text(job_snapshot.get("youtube_url"))
    if youtube_url is not None:
        matched_project = _find_project_dir_by_youtube_url(projects_root=projects_root, youtube_url=youtube_url)
        if matched_project is not None:
            return matched_project, "matched_youtube_url"

    logs = job_snapshot.get("logs")
    if isinstance(logs, list):
        for raw_line in reversed(logs):
            if not isinstance(raw_line, str):
                continue
            path = _extract_project_dir_from_log_line(raw_line, projects_root=projects_root)
            if path is not None:
                return path, "log_path"

    return None, "no_project_match"


def _describe_results_source(source: str) -> str:
    normalized_source = source.strip()
    if not normalized_source:
        return "\u672a\u77e5\u6765\u6e90"
    return RESULT_SOURCE_LABELS.get(normalized_source, normalized_source)


def _find_project_dir_by_youtube_url(*, projects_root: Path, youtube_url: str) -> Path | None:
    normalized_url = youtube_url.strip()
    if not normalized_url:
        return None
    for candidate in projects_root.iterdir():
        if not candidate.is_dir():
            continue
        manifest_source_url = build_source_context_summary(
            manifest_payload=load_manifest_payload(project_dir=candidate),
        ).get("locator")
        if manifest_source_url == normalized_url:
            return candidate.resolve(strict=False)
        metadata_path = candidate / "download_metadata.json"
        if not metadata_path.exists():
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(metadata, dict):
            continue
        stored_url = _normalize_optional_text(metadata.get("url"))
        if stored_url == normalized_url:
            return candidate.resolve(strict=False)
    return None


def _find_latest_project_dir(projects_root: Path) -> Path | None:
    candidates = [candidate for candidate in projects_root.iterdir() if candidate.is_dir()]
    if not candidates:
        return None
    latest_candidate = max(candidates, key=lambda item: item.stat().st_mtime)
    return latest_candidate.resolve(strict=False)


def _extract_project_dir_from_log_line(raw_line: str, *, projects_root: Path) -> Path | None:
    for match in WINDOWS_PATH_PATTERN.findall(raw_line):
        candidate_path = Path(match).resolve(strict=False)
        if candidate_path.name.lower() == "output":
            project_dir = candidate_path.parent
        else:
            project_dir = candidate_path
        if project_dir.exists() and _path_is_within_root(project_dir, projects_root):
            return project_dir
    return None


def _path_is_within_root(candidate_path: Path, root_path: Path) -> bool:
    try:
        candidate_path.resolve(strict=False).relative_to(root_path.resolve(strict=False))
    except ValueError:
        return False
    return True


def _is_project_audio_file(candidate_path: Path) -> bool:
    if not candidate_path.exists() or not candidate_path.is_file():
        return False
    if candidate_path.suffix.lower() in PROJECT_AUDIO_FILE_SUFFIXES:
        return True
    guessed_type = mimetypes.guess_type(str(candidate_path))[0] or ""
    return guessed_type.lower().startswith("audio/")


def _resolve_authoritative_review_project_dir(
    *,
    manager: object,
    requested_project_dir: object,
    expected_stage: str | None = None,
    require_waiting_review: bool = False,
) -> Path:
    job_snapshot = manager.snapshot()  # type: ignore[union-attr]
    project_root = manager.project_root.resolve(strict=False)  # type: ignore[union-attr]
    projects_root = (project_root / "projects").resolve(strict=False)
    authoritative_project_dir_text = _normalize_optional_text(job_snapshot.get("project_dir"))
    if authoritative_project_dir_text is None:
        raise ValueError("\u5f53\u524d\u6ca1\u6709\u53ef\u5199\u5165 review \u7684\u771f\u5b9e\u9879\u76ee\u4e0a\u4e0b\u6587\u3002")

    authoritative_project_dir = Path(authoritative_project_dir_text).expanduser().resolve(strict=False)
    if not _path_is_within_root(authoritative_project_dir, projects_root):
        raise ValueError("\u5f53\u524d\u4efb\u52a1\u7ed1\u5b9a\u7684\u9879\u76ee\u76ee\u5f55\u8d85\u51fa\u4e86 projects \u6839\u76ee\u5f55\u3002")

    requested_project_dir_text = _normalize_optional_text(requested_project_dir)
    if requested_project_dir_text is not None:
        requested_path = Path(requested_project_dir_text).expanduser().resolve(strict=False)
        if requested_path != authoritative_project_dir:
            raise ValueError("\u8bf7\u6c42\u91cc\u7684 project_dir \u4e0e\u5f53\u524d\u771f\u5b9e\u4efb\u52a1\u9879\u76ee\u4e0d\u4e00\u81f4\u3002")

    if require_waiting_review:
        if str(job_snapshot.get("status") or "").strip() != JOB_STATUS_WAITING_FOR_REVIEW:
            raise ValueError("\u5f53\u524d\u4efb\u52a1\u4e0d\u5728\u7b49\u5f85 review \u7684\u72b6\u6001\u3002")
        active_review = _copy_optional_mapping(job_snapshot.get("review_gate")) or {}
        active_stage = _normalize_optional_text(active_review.get("stage"))
        if expected_stage is not None and active_stage != expected_stage:
            raise ValueError("\u5f53\u524d\u7b49\u5f85\u786e\u8ba4\u7684 review \u9636\u6bb5\u4e0e\u8bf7\u6c42\u4e0d\u4e00\u81f4\u3002")

    return authoritative_project_dir


def _build_current_project_audio_preview_paths(
    *,
    project_dir: Path,
    results_snapshot: dict[str, object],
) -> set[Path]:
    allowed_paths: set[Path] = set()
    for section_name in ("translation_review", "audio_alignment"):
        section_payload = results_snapshot.get(section_name)
        if not isinstance(section_payload, dict):
            continue
        raw_items = section_payload.get("items")
        if not isinstance(raw_items, list):
            continue
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            for field_name in ("tts_audio_path", "aligned_audio_path"):
                resolved_path_text = _normalize_optional_text(raw_item.get(field_name))
                if resolved_path_text is None:
                    continue
                resolved_path = Path(resolved_path_text).expanduser().resolve(strict=False)
                if _path_is_within_root(resolved_path, project_dir) and _is_project_audio_file(resolved_path):
                    allowed_paths.add(resolved_path)
    return allowed_paths


def _resolve_allowed_project_file_download_path(
    *,
    manager: object,
    requested_path: str,
) -> Path:
    candidate_path = Path(requested_path).expanduser().resolve(strict=False)
    if not candidate_path.exists() or not candidate_path.is_file():
        raise FileNotFoundError("Requested file was not found.")

    # Late import to avoid circular dependency
    from .snapshot import build_web_ui_snapshot

    snapshot = build_web_ui_snapshot(manager=manager)  # type: ignore[arg-type]
    results_snapshot = _ensure_dict(snapshot.get("results"))
    current_project_dir_text = _normalize_optional_text(results_snapshot.get("project_dir"))
    if current_project_dir_text is None:
        raise ValueError("\u5f53\u524d\u6ca1\u6709\u53ef\u7528\u4e8e\u97f3\u9891\u9884\u89c8\u7684\u9879\u76ee\u76ee\u5f55\u3002")

    project_root = manager.project_root.resolve(strict=False)  # type: ignore[union-attr]
    projects_root = (project_root / "projects").resolve(strict=False)
    current_project_dir = Path(current_project_dir_text).expanduser().resolve(strict=False)
    if not _path_is_within_root(current_project_dir, projects_root):
        raise ValueError("\u5f53\u524d\u7ed3\u679c\u9879\u76ee\u76ee\u5f55\u8d85\u51fa\u4e86 projects \u6839\u76ee\u5f55\u3002")
    if not _path_is_within_root(candidate_path, current_project_dir):
        raise ValueError("Requested file is outside the current project directory.")
    if not _is_project_audio_file(candidate_path):
        raise ValueError("Requested file is not an allowed audio preview file.")

    allowed_paths = _build_current_project_audio_preview_paths(
        project_dir=current_project_dir,
        results_snapshot=results_snapshot,
    )
    if candidate_path not in allowed_paths:
        raise ValueError("Requested file is not in the current project's audio preview whitelist.")
    return candidate_path


def _resolve_public_result_download_path(
    *,
    project_root: Path,
    project_dir: Path,
    download_key: str,
) -> Path | None:
    normalized_key = download_key.strip()
    if normalized_key not in PUBLIC_RESULT_DOWNLOAD_KEYS:
        raise ValueError(f"Requested download key is not allowed: {normalized_key}")

    projects_root = (project_root / "projects").resolve(strict=False)
    resolved_project_dir = _resolve_project_dir_under_projects_root(
        project_dir=project_dir,
        projects_root=projects_root,
    )
    if resolved_project_dir is None:
        raise ValueError("Requested project is outside projects root.")

    resolved_project_dir = resolved_project_dir.resolve(strict=False)
    projects_root = projects_root.resolve(strict=False)
    if not _path_is_within_root(resolved_project_dir, projects_root):
        raise ValueError("Requested project is outside projects root.")

    if normalized_key == RESULT_DOWNLOAD_KEY_MANIFEST:
        candidate_path = (resolved_project_dir / "manifest.json").resolve(strict=False)
    elif normalized_key == "editor.jianying_draft_zip":
        # Phase 1 on-demand drafts are generated post-dispatcher by
        # JianyingDraftRunner (services.jobs.jianying_draft_runner) and live
        # at {project_dir}/jianying/exports/jianying_draft_*.zip. The project
        # manifest is written by OutputDispatcher BEFORE the runner generates
        # the zip, so manifest.artifact_index has no editor.jianying_draft_zip
        # entry. Resolve via the runner's known convention path instead.
        exports_dir = resolved_project_dir / "jianying" / "exports"
        candidates: list[Path] = []
        if exports_dir.exists() and exports_dir.is_dir():
            candidates = [
                entry
                for entry in exports_dir.iterdir()
                if entry.is_file()
                and entry.name.startswith("jianying_draft_")
                and entry.suffix == ".zip"
            ]
        if not candidates:
            return None
        # If multiple zips exist (e.g. user re-triggered after a fix), serve
        # the most recently generated one.
        candidate_path = max(candidates, key=lambda p: p.stat().st_mtime).resolve(strict=False)
    else:
        artifact_index = load_manifest_artifact_index(project_dir=resolved_project_dir)
        candidate_path = _resolve_artifact_path(
            resolved_project_dir,
            normalized_key,
            artifact_index=artifact_index,
        )
        if candidate_path is None:
            return None
        candidate_path = candidate_path.resolve(strict=False)

    if not candidate_path.exists() or not candidate_path.is_file():
        return None
    if not _path_is_within_root(candidate_path, resolved_project_dir):
        raise ValueError("Resolved download path is outside the project directory.")
    return candidate_path


def _resolve_project_dir_by_job_id(
    *,
    manager: object,
    job_id: str,
) -> str | None:
    normalized_job_id = job_id.strip()
    if not normalized_job_id:
        return None

    # Late import to check manager type without circular dependency
    from .models import ProcessJobSnapshot  # noqa: F401

    # Check if it's a JobAPIBackedJobManager by duck-typing
    if hasattr(manager, "_request_json"):
        try:
            payload = manager._request_json("GET", f"/jobs/{normalized_job_id}", None)  # type: ignore[union-attr]
        except Exception:
            return None
        return _normalize_optional_text(payload.get("project_dir"))

    snapshot = manager.snapshot()  # type: ignore[union-attr]
    snapshot_job_id = _normalize_optional_text(snapshot.get("job_id"))
    if snapshot_job_id != normalized_job_id:
        return None
    return _normalize_optional_text(snapshot.get("project_dir"))


def _resolve_project_dir_under_projects_root(
    *,
    project_dir: Path,
    projects_root: Path,
) -> Path | None:
    normalized_projects_root = projects_root.resolve(strict=False)
    resolved_project_dir = project_dir.resolve(strict=False)
    if _path_is_within_root(resolved_project_dir, normalized_projects_root):
        return resolved_project_dir

    relative_candidate = _extract_relative_path_after_projects_segment(resolved_project_dir)
    if relative_candidate is None:
        return None
    rewritten_project_dir = (normalized_projects_root / relative_candidate).resolve(strict=False)
    if not _path_is_within_root(rewritten_project_dir, normalized_projects_root):
        return None
    return rewritten_project_dir


def _extract_relative_path_after_projects_segment(path: Path) -> Path | None:
    parts = path.parts
    projects_index = -1
    for index, value in enumerate(parts):
        if value == "projects":
            projects_index = index
    if projects_index < 0 or projects_index + 1 >= len(parts):
        return None
    return Path(*parts[projects_index + 1 :])
