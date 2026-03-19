from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from core.enums import StageStatus
from core.exceptions import StateError


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateManager:
    """Persist lightweight project stage state as JSON."""

    def __init__(self, state_path: str) -> None:
        self.state_path = Path(state_path)

    def load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"project_id": None, "stages": {}}

        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"Failed to load state file: {self.state_path}") from exc
        return self._normalize_state(data)

    def save(self, state: dict[str, Any]) -> None:
        temp_path: Path | None = None
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            serialized_state = json.dumps(state, indent=2, sort_keys=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.state_path.parent,
                prefix=f"{self.state_path.stem}_",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_file.write(serialized_state)
                temp_file.flush()
                os.fsync(temp_file.fileno())
                temp_path = Path(temp_file.name)
            os.replace(temp_path, self.state_path)
        except OSError as exc:
            raise StateError(f"Failed to save state file: {self.state_path}") from exc
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink(missing_ok=True)

    def set_project(self, project_id: str) -> dict[str, Any]:
        state = self.load()
        state["project_id"] = project_id
        self.save(state)
        return state

    def get_stage(self, stage_name: str) -> dict[str, Any] | None:
        state = self.load()
        return state.get("stages", {}).get(stage_name)

    def set_stage(
        self,
        stage_name: str,
        status: StageStatus | str,
        payload: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        state = self.load()
        stages = state.setdefault("stages", {})
        existing_stage = stages.get(stage_name, self._empty_stage())
        normalized_status = self._normalize_status(status)
        timestamp = utc_now_iso()

        started_at = existing_stage.get("started_at")
        finished_at = existing_stage.get("finished_at")
        if normalized_status == StageStatus.PENDING.value:
            started_at = None
            finished_at = None
        elif normalized_status == StageStatus.RUNNING.value:
            started_at = started_at or timestamp
            finished_at = None
        else:
            started_at = started_at or timestamp
            finished_at = timestamp

        stages[stage_name] = {
            "status": normalized_status,
            "started_at": started_at,
            "finished_at": finished_at,
            "updated_at": timestamp,
            "error_message": error_message if normalized_status == StageStatus.FAILED.value else None,
            "payload": payload or {},
        }
        self.save(state)
        return stages[stage_name]

    def _normalize_state(self, state: dict[str, Any]) -> dict[str, Any]:
        normalized_state = {
            "project_id": state.get("project_id"),
            "stages": {},
        }
        for stage_name, stage_data in state.get("stages", {}).items():
            normalized_state["stages"][stage_name] = {
                "status": self._normalize_status(stage_data.get("status", StageStatus.PENDING.value)),
                "started_at": stage_data.get("started_at"),
                "finished_at": stage_data.get("finished_at"),
                "updated_at": stage_data.get("updated_at"),
                "error_message": stage_data.get("error_message"),
                "payload": stage_data.get("payload", {}),
            }
        return normalized_state

    def _normalize_status(self, status: StageStatus | str) -> str:
        if isinstance(status, StageStatus):
            return status.value
        status_text = str(status).strip().lower()
        legacy_status_map = {
            "align_done": StageStatus.DONE.value,
            "align_done_fallback": StageStatus.DONE.value,
            "tts_done": StageStatus.RUNNING.value,
        }
        if status_text in legacy_status_map:
            return legacy_status_map[status_text]
        allowed_statuses = {item.value for item in StageStatus}
        if status_text not in allowed_statuses:
            raise StateError(f"Unsupported stage status: {status}")
        return status_text

    def _empty_stage(self) -> dict[str, Any]:
        return {
            "status": StageStatus.PENDING.value,
            "started_at": None,
            "finished_at": None,
            "updated_at": None,
            "error_message": None,
            "payload": {},
        }
