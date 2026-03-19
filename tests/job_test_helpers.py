from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

from services.review_state import ReviewStateManager


class FakeProcess:
    def __init__(self, command: list[str], *, lines: list[str], returncode: int = 0) -> None:
        self.command = command
        self.stdout = iter([line if line.endswith("\n") else f"{line}\n" for line in lines])
        self._planned_returncode = returncode
        self._returncode: int | None = None

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self._returncode = self._planned_returncode
        return self._planned_returncode

    def kill(self) -> None:
        self._returncode = -9

    def terminate(self) -> None:
        self._returncode = -15


class FakePopenFactory:
    def __init__(self, plans: list[dict[str, Any]]) -> None:
        self._plans = list(plans)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> FakeProcess:
        if not self._plans:
            raise AssertionError("Unexpected subprocess invocation with no remaining fake plans.")
        plan = dict(self._plans.pop(0))
        self.calls.append({"command": list(command), "kwargs": dict(kwargs)})
        return FakeProcess(
            command,
            lines=list(plan.get("lines", [])),
            returncode=int(plan.get("returncode", 0)),
        )


def wait_for(predicate, *, timeout_seconds: float = 3.0, interval_seconds: float = 0.02) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval_seconds)
    raise AssertionError("Timed out waiting for condition.")


def write_process_project(
    project_root: Path,
    *,
    project_name: str,
    youtube_url: str,
    fallback_summary: dict[str, object] | None = None,
    failed_stage_name: str | None = None,
    failed_stage_error: str | None = None,
    failed_stage_error_type: str = "process_failed",
) -> Path:
    project_dir = project_root / "projects" / project_name
    output_dir = project_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    (project_dir / "download_metadata.json").write_text(
        json.dumps(
            {
                "url": youtube_url,
                "video_title": project_name,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    manifest_path = project_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "fallback_summary": dict(fallback_summary or {}),
                "artifact_index": {
                    "state.project": str((project_dir / "project_state.json").resolve(strict=False)),
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    stages: dict[str, object] = {
        "legacy_process_output": {
            "status": "done",
            "started_at": "2026-03-18T00:00:00+00:00",
            "finished_at": "2026-03-18T00:00:10+00:00",
            "updated_at": "2026-03-18T00:00:10+00:00",
            "error_message": None,
            "payload": {
                "execution_mode": "legacy_process_output_dispatch",
                "manifest_path": str(manifest_path.resolve(strict=False)),
            },
        }
    }
    if failed_stage_name is not None:
        stages[failed_stage_name] = {
            "status": "failed",
            "started_at": "2026-03-18T00:00:00+00:00",
            "finished_at": "2026-03-18T00:00:05+00:00",
            "updated_at": "2026-03-18T00:00:05+00:00",
            "error_message": failed_stage_error or "stage failed",
            "payload": {
                "error_type": failed_stage_error_type,
            },
        }

    (project_dir / "project_state.json").write_text(
        json.dumps(
            {
                "project_id": project_name,
                "stages": stages,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return project_dir


def set_review_stage(
    project_dir: Path,
    *,
    stage_name: str,
    status: str,
    payload: dict[str, object] | None = None,
    activate: bool | None = None,
) -> None:
    manager = ReviewStateManager(project_dir / "review_state.json")
    manager.set_stage(stage_name, status=status, payload=payload, activate=activate)
