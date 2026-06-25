"""Admin-only CosyVoice control surface.

This router intentionally exposes a narrow PATCH-like contract for CosyVoice
business controls. It does not reuse ``POST /api/admin/settings`` because that
endpoint has full-body semantics: missing fields are reset to defaults.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, StrictBool

from admin_auth import _require_admin
from admin_settings import AdminSettings, load_settings, save_settings
from auth import User, get_current_user
from config import settings as gateway_settings
from cosyvoice_clone.api import _resolve_runtime_ready
from cosyvoice_clone.sample_uploader import (
    PRODUCTION_READY_BACKENDS,
    missing_aliyun_oss_settings,
)
from csrf import require_same_origin_state_change
from mainland_voice_worker import is_mainland_voice_worker_config_ready


router = APIRouter(prefix="/api/admin/cosyvoice-control")


class CosyVoiceControlSettings(BaseModel):
    """Editable CosyVoice business controls.

    Env-owned runtime capability and secrets remain read-only status in this
    endpoint; they are not accepted here.
    """

    model_config = ConfigDict(extra="forbid")

    cosyvoice_runtime_endpoint_mode: str
    cosyvoice_offline_endpoint_mode: str
    cosyvoice_clone_worker_enabled: StrictBool
    cosyvoice_clone_default_target_model: str
    cosyvoice_clone_user_allowlist: list[str]
    cosyvoice_clone_general_availability_enabled: StrictBool
    cosyvoice_clone_max_voices_per_user: int
    express_cosyvoice_auto_clone_enabled: StrictBool
    express_cosyvoice_auto_clone_allowlist_enabled: StrictBool
    express_cosyvoice_auto_clone_user_allowlist: list[str]
    express_cosyvoice_auto_clone_main_speaker_min_ratio: float
    express_cosyvoice_auto_clone_main_speaker_min_lines: int
    express_cosyvoice_auto_clone_sample_max_seconds: float
    express_cosyvoice_auto_clone_target_model: str
    express_cosyvoice_auto_clone_per_user_daily_cap: int
    express_cosyvoice_auto_clone_per_user_active_temp_cap: int
    express_cosyvoice_auto_clone_reservation_ttl_minutes: int


_EDITABLE_FIELDS = tuple(CosyVoiceControlSettings.model_fields.keys())


def _editable_settings_snapshot(settings: AdminSettings) -> dict[str, Any]:
    data = settings.model_dump()
    return {key: data[key] for key in _EDITABLE_FIELDS}


def _runtime_status(settings: AdminSettings) -> dict[str, Any]:
    readiness = _resolve_runtime_ready(settings, gateway_settings)
    uploader_backend = getattr(gateway_settings, "cosyvoice_sample_uploader", "local_fs_stub")
    cleanup_raw = os.getenv("AVT_EXPRESS_VOICE_CLEANUP_DRY_RUN")
    return {
        "manual_clone_runtime_ready": readiness.runtime_ready,
        "manual_clone_runtime_unavailable_code": readiness.runtime_unavailable_code,
        "mainland_worker": {
            "effective_enabled": bool(getattr(gateway_settings, "mainland_voice_worker_enabled", False)),
            "config_ready": is_mainland_voice_worker_config_ready(gateway_settings),
            "url_configured": bool((getattr(gateway_settings, "mainland_voice_worker_url", "") or "").strip()),
            "hmac_key_id_configured": bool(
                (getattr(gateway_settings, "mainland_voice_worker_hmac_key_id", "") or "").strip()
            ),
            "hmac_secret_configured": bool(getattr(gateway_settings, "mainland_voice_worker_hmac_secret", "")),
        },
        "sample_uploader": {
            "backend": uploader_backend,
            "production_ready": uploader_backend in PRODUCTION_READY_BACKENDS,
            "missing_config_fields": missing_aliyun_oss_settings(gateway_settings),
        },
        "cleanup": {
            "dry_run_env": cleanup_raw,
            "dry_run_effective": (cleanup_raw or "true").lower() != "false",
        },
    }


@router.get("")
async def get_cosyvoice_control(
    user: User | None = Depends(get_current_user),
) -> dict[str, Any]:
    _require_admin(user)
    settings = load_settings()
    return {
        "settings": _editable_settings_snapshot(settings),
        "runtime": _runtime_status(settings),
    }


@router.post("", dependencies=[Depends(require_same_origin_state_change)])
async def update_cosyvoice_control(
    body: CosyVoiceControlSettings,
    user: User | None = Depends(get_current_user),
) -> dict[str, Any]:
    _require_admin(user)
    current = load_settings()
    merged = current.model_dump()
    merged.update(body.model_dump())
    updated = AdminSettings(**merged)
    save_settings(updated)
    return {
        "settings": _editable_settings_snapshot(updated),
        "runtime": _runtime_status(updated),
    }
