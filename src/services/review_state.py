from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from core.exceptions import StateError


REVIEW_STATUS_PENDING = "pending"
REVIEW_STATUS_APPROVED = "approved"
REVIEW_STATUS_REJECTED = "rejected"
REVIEW_STATUS_SKIPPED = "skipped"

SPEAKER_REVIEW_STAGE = "speaker_review"
TRANSLATION_CONFIG_REVIEW_STAGE = "translation_config_review"
TRANSLATION_REVIEW_STAGE = "translation_review"
VOICE_REVIEW_STAGE = "voice_review"
AUDIO_ALIGNMENT_REVIEW_STAGE = "audio_alignment_review"

REVIEW_STAGE_TAB_MAP = {
    SPEAKER_REVIEW_STAGE: "review",
    TRANSLATION_CONFIG_REVIEW_STAGE: "translation-config",
    TRANSLATION_REVIEW_STAGE: "translation",
    VOICE_REVIEW_STAGE: "voice-library",
    AUDIO_ALIGNMENT_REVIEW_STAGE: "audio-alignment",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReviewStateManager:
    """Persist lightweight human-review checkpoints for Web UI assisted runs."""

    def __init__(self, state_path: str | Path) -> None:
        self.state_path = Path(state_path)

    def load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"active_stage": None, "stages": {}}

        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"Failed to load review state file: {self.state_path}") from exc
        return self._normalize_state(payload)

    def save(self, state: dict[str, Any]) -> None:
        temp_path: Path | None = None
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            serialized_state = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)
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
            raise StateError(f"Failed to save review state file: {self.state_path}") from exc
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink(missing_ok=True)

    def get_active_stage(self) -> str | None:
        return self.load().get("active_stage")

    def set_active_stage(self, stage_name: str | None) -> dict[str, Any]:
        state = self.load()
        state["active_stage"] = self._normalize_stage_name(stage_name)
        self.save(state)
        return state

    def get_stage(self, stage_name: str) -> dict[str, Any] | None:
        state = self.load()
        return state.get("stages", {}).get(self._normalize_stage_name(stage_name))

    def set_stage(
        self,
        stage_name: str,
        *,
        status: str,
        payload: dict[str, Any] | None = None,
        activate: bool | None = None,
    ) -> dict[str, Any]:
        normalized_stage_name = self._normalize_stage_name(stage_name)
        normalized_status = self._normalize_status(status)
        state = self.load()
        stages = state.setdefault("stages", {})
        existing_stage = stages.get(normalized_stage_name, self._empty_stage(normalized_stage_name))
        timestamp = utc_now_iso()

        next_stage = {
            "stage": normalized_stage_name,
            "tab": REVIEW_STAGE_TAB_MAP.get(normalized_stage_name),
            "status": normalized_status,
            "updated_at": timestamp,
            "approved_at": existing_stage.get("approved_at"),
            "payload": payload if payload is not None else existing_stage.get("payload", {}),
        }
        if normalized_status == REVIEW_STATUS_APPROVED:
            next_stage["approved_at"] = timestamp
        elif normalized_status == REVIEW_STATUS_PENDING:
            next_stage["approved_at"] = None

        stages[normalized_stage_name] = next_stage
        if activate is True:
            state["active_stage"] = normalized_stage_name
        elif activate is False and state.get("active_stage") == normalized_stage_name:
            state["active_stage"] = None
        elif normalized_status == REVIEW_STATUS_APPROVED and state.get("active_stage") == normalized_stage_name:
            state["active_stage"] = None

        self.save(state)
        return next_stage

    def clear_stage(self, stage_name: str) -> dict[str, Any]:
        normalized_stage_name = self._normalize_stage_name(stage_name)
        state = self.load()
        stages = state.setdefault("stages", {})
        stages.pop(normalized_stage_name, None)
        if state.get("active_stage") == normalized_stage_name:
            state["active_stage"] = None
        self.save(state)
        return state

    def _normalize_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized_state = {
            "active_stage": self._normalize_stage_name(payload.get("active_stage")),
            "stages": {},
        }
        stages = payload.get("stages", {})
        if not isinstance(stages, dict):
            return normalized_state

        for raw_stage_name, raw_stage in stages.items():
            normalized_stage_name = self._normalize_stage_name(raw_stage_name)
            if normalized_stage_name is None or not isinstance(raw_stage, dict):
                continue
            normalized_state["stages"][normalized_stage_name] = {
                "stage": normalized_stage_name,
                "tab": REVIEW_STAGE_TAB_MAP.get(normalized_stage_name),
                "status": self._normalize_status(raw_stage.get("status", REVIEW_STATUS_PENDING)),
                "updated_at": raw_stage.get("updated_at"),
                "approved_at": raw_stage.get("approved_at"),
                "payload": raw_stage.get("payload", {}) if isinstance(raw_stage.get("payload"), dict) else {},
            }
        return normalized_state

    def _empty_stage(self, stage_name: str) -> dict[str, Any]:
        return {
            "stage": stage_name,
            "tab": REVIEW_STAGE_TAB_MAP.get(stage_name),
            "status": REVIEW_STATUS_PENDING,
            "updated_at": None,
            "approved_at": None,
            "payload": {},
        }

    def _normalize_status(self, status: object) -> str:
        normalized_status = str(status or "").strip().lower()
        allowed_statuses = {
            REVIEW_STATUS_PENDING,
            REVIEW_STATUS_APPROVED,
            REVIEW_STATUS_REJECTED,
            REVIEW_STATUS_SKIPPED,
        }
        if normalized_status not in allowed_statuses:
            raise StateError(f"Unsupported review status: {status}")
        return normalized_status

    def _normalize_stage_name(self, stage_name: object) -> str | None:
        normalized_stage_name = str(stage_name or "").strip()
        if not normalized_stage_name:
            return None
        return normalized_stage_name
