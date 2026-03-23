from __future__ import annotations

import json
import os
import webbrowser
from datetime import UTC, datetime
from pathlib import Path

from services.review_state import REVIEW_STAGE_TAB_MAP

from .constants import DOWNLOAD_PROGRESS_PATTERN, STAGE_LOG_PATTERN


def _resolve_snapshot_status_from_log_line(
    *,
    line: str,
    current_stage: str | None,
    current_message: str | None,
) -> tuple[str | None, str | None]:
    stage_match = STAGE_LOG_PATTERN.match(line)
    if stage_match:
        next_stage = stage_match.group(1)
        next_message = stage_match.group(2).strip() or current_message
        return next_stage, next_message

    download_match = DOWNLOAD_PROGRESS_PATTERN.match(line)
    if download_match:
        progress_message = download_match.group(1).strip()
        if not progress_message:
            return "S0", "下载中..."
        return "S0", f"下载中：{progress_message}"

    if line.lower().startswith("process failed:"):
        return current_stage, line

    if line.startswith("[WEB]"):
        return current_stage, current_message

    normalized_line = line.strip()
    if normalized_line:
        return current_stage, normalized_line

    return current_stage, current_message


def _parse_web_review_marker(line: str) -> dict[str, object] | None:
    normalized_line = line.strip()
    if not normalized_line.startswith("[WEB_REVIEW]"):
        return None
    raw_payload = normalized_line.removeprefix("[WEB_REVIEW]").strip()
    if not raw_payload:
        return None
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    stage = _normalize_optional_text(payload.get("stage"))
    project_dir = _normalize_optional_text(payload.get("project_dir"))
    message = _normalize_optional_text(payload.get("message"))
    tab = _normalize_optional_text(payload.get("tab")) or (
        REVIEW_STAGE_TAB_MAP.get(stage) if stage is not None else None
    )
    if stage is None:
        return None
    return {
        "stage": stage,
        "tab": tab,
        "project_dir": project_dir,
        "message": message,
    }


def _stringify_existing_path(path: Path) -> str | None:
    resolved_path = path.resolve(strict=False)
    if not resolved_path.exists():
        return None
    return str(resolved_path)


def _normalize_optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _resolve_provider_key_source(
    section: dict[str, object],
    *,
    api_key_env_var: str,
) -> str | None:
    configured_key = _normalize_optional_text(section.get("api_key"))
    if configured_key is not None:
        return "config"
    if api_key_env_var and _normalize_optional_text(os.environ.get(api_key_env_var)) is not None:
        return "env"
    return None


def _copy_optional_mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return dict(value)


def _coerce_float(value: object, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ensure_dict(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url, new=2)
    except Exception:
        return


def _utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
