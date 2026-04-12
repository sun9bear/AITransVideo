from __future__ import annotations

from dataclasses import dataclass
from typing import Any


EVENT_TYPE_LOG = "log"
EVENT_TYPE_STATUS = "status"
SUPPORTED_EVENT_TYPES = {
    EVENT_TYPE_LOG,
    EVENT_TYPE_STATUS,
}

EVENT_LEVEL_INFO = "info"
EVENT_LEVEL_WARN = "warn"
EVENT_LEVEL_ERROR = "error"
SUPPORTED_EVENT_LEVELS = {
    EVENT_LEVEL_INFO,
    EVENT_LEVEL_WARN,
    EVENT_LEVEL_ERROR,
}


@dataclass(slots=True)
class JobEvent:
    job_id: str
    event_type: str
    created_at: str
    message: str | None = None
    stage: str | None = None
    status: str | None = None
    level: str = EVENT_LEVEL_INFO
    payload: dict[str, object] | None = None

    def __post_init__(self) -> None:
        self.job_id = str(self.job_id).strip()
        self.event_type = str(self.event_type).strip().lower()
        self.created_at = str(self.created_at).strip()
        self.message = _normalize_optional_text(self.message)
        self.stage = _normalize_optional_text(self.stage)
        self.status = _normalize_optional_text(self.status)
        self.level = str(self.level).strip().lower()
        self.payload = dict(self.payload or {})

        if not self.job_id:
            raise ValueError("job_id is required")
        if self.event_type not in SUPPORTED_EVENT_TYPES:
            raise ValueError(f"Unsupported event_type: {self.event_type}")
        if not self.created_at:
            raise ValueError("created_at is required")
        if self.level not in SUPPORTED_EVENT_LEVELS:
            raise ValueError(f"Unsupported event level: {self.level}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "event_type": self.event_type,
            "created_at": self.created_at,
            "message": self.message,
            "stage": self.stage,
            "status": self.status,
            "level": self.level,
            "payload": dict(self.payload or {}),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> JobEvent:
        return cls(
            job_id=payload["job_id"],
            event_type=payload["event_type"],
            created_at=payload["created_at"],
            message=payload.get("message"),
            stage=payload.get("stage"),
            status=payload.get("status"),
            level=payload.get("level", EVENT_LEVEL_INFO),
            payload=payload.get("payload"),
        )


def _normalize_optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized_value = str(value).strip()
    return normalized_value or None
