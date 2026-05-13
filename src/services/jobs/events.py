from __future__ import annotations

from dataclasses import dataclass
from typing import Any


EVENT_TYPE_LOG = "log"
EVENT_TYPE_STATUS = "status"
EVENT_TYPE_DOWNLOAD_REDIRECT_R2 = "download.redirect.r2"
# Plan 2026-05-07 §4.7: ``r2_registry`` distinguishes the new
# registry-driven 302 path (Stage A) from the legacy lazy-upload one.
# The rollout dashboard splits them so we can observe registry-path
# adoption independently of the lazy fallback usage.
EVENT_TYPE_DOWNLOAD_REDIRECT_R2_REGISTRY = "download.redirect.r2_registry"
EVENT_TYPE_DOWNLOAD_FALLBACK_LOCAL = "download.fallback.local"
EVENT_TYPE_DOWNLOAD_LOCAL_DIRECT = "download.local.direct"
# Plan 2026-05-07 §11.3 C6 (Stage C, 2026-05-12): /stream/{kind} mirrors
# the download event vocabulary. We keep the dashboard split between
# stream and download because the failure modes differ — stream is
# latency-sensitive (player buffering), download is throughput-sensitive
# (zip transfer time). Same registry / lazy / fallback semantics, but
# the metric breakdowns need separate buckets.
EVENT_TYPE_STREAM_REDIRECT_R2 = "stream.redirect.r2"
EVENT_TYPE_STREAM_REDIRECT_R2_REGISTRY = "stream.redirect.r2_registry"
EVENT_TYPE_STREAM_FALLBACK_LOCAL = "stream.fallback.local"
EVENT_TYPE_STREAM_LOCAL_DIRECT = "stream.local.direct"
SUPPORTED_EVENT_TYPES = {
    EVENT_TYPE_LOG,
    EVENT_TYPE_STATUS,
    EVENT_TYPE_DOWNLOAD_REDIRECT_R2,
    EVENT_TYPE_DOWNLOAD_REDIRECT_R2_REGISTRY,
    EVENT_TYPE_DOWNLOAD_FALLBACK_LOCAL,
    EVENT_TYPE_DOWNLOAD_LOCAL_DIRECT,
    EVENT_TYPE_STREAM_REDIRECT_R2,
    EVENT_TYPE_STREAM_REDIRECT_R2_REGISTRY,
    EVENT_TYPE_STREAM_FALLBACK_LOCAL,
    EVENT_TYPE_STREAM_LOCAL_DIRECT,
}

EVENT_LEVEL_INFO = "info"
EVENT_LEVEL_WARN = "warn"
EVENT_LEVEL_ERROR = "error"
# 2026-04-21 plan §7.8 / D35: needs-ops-intervention severity — e.g.
# copy_as_new's Phase B cleanup failing (new job already running, source
# stuck in editing until the 24h idle scanner tides it over). Admin
# LogViewer should render this differently; logs_redactor must pass it
# through to admins unredacted for root-cause triage.
EVENT_LEVEL_CRITICAL = "critical"
SUPPORTED_EVENT_LEVELS = {
    EVENT_LEVEL_INFO,
    EVENT_LEVEL_WARN,
    EVENT_LEVEL_ERROR,
    EVENT_LEVEL_CRITICAL,
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
