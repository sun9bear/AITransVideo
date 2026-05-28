from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GATEWAY_DIR = REPO_ROOT / "gateway"
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

ADMIN_COSYVOICE_PAGE = (
    REPO_ROOT
    / "frontend-next"
    / "src"
    / "app"
    / "(app)"
    / "admin"
    / "cosyvoice"
    / "page.tsx"
)
ADMIN_COSYVOICE_API = REPO_ROOT / "frontend-next" / "src" / "lib" / "api" / "adminCosyvoice.ts"
APP_SHELL = REPO_ROOT / "frontend-next" / "src" / "components" / "app-shell.tsx"


def test_backend_control_model_is_narrow_and_extra_forbid():
    from admin_cosyvoice_control_api import CosyVoiceControlSettings, _EDITABLE_FIELDS

    fields = set(CosyVoiceControlSettings.model_fields)
    assert fields == set(_EDITABLE_FIELDS)
    assert "smart_mode_enabled" not in fields
    assert "review_prompts" not in fields
    assert "mainland_voice_worker_hmac_secret" not in fields
    assert CosyVoiceControlSettings.model_config.get("extra") == "forbid"


def test_backend_control_model_rejects_string_bools():
    from admin_cosyvoice_control_api import CosyVoiceControlSettings
    from admin_settings import AdminSettings
    from pydantic import ValidationError

    payload = {
        key: getattr(AdminSettings(), key)
        for key in CosyVoiceControlSettings.model_fields
    }
    payload["express_cosyvoice_auto_clone_enabled"] = "true"
    with pytest.raises(ValidationError):
        CosyVoiceControlSettings(**payload)


@pytest.mark.asyncio
async def test_update_cosyvoice_control_merges_without_resetting_unrelated_fields(monkeypatch):
    import admin_cosyvoice_control_api as mod
    from admin_settings import AdminSettings

    current = AdminSettings(
        smart_mode_enabled=True,
        smart_auto_clone_enabled=False,
        free_user_max_duration_minutes=42.0,
        cosyvoice_clone_user_allowlist=["manual-user"],
        express_cosyvoice_auto_clone_user_allowlist=["express-user"],
    )
    payload = {
        key: getattr(current, key)
        for key in mod.CosyVoiceControlSettings.model_fields
    }
    payload["express_cosyvoice_auto_clone_enabled"] = True
    payload["cosyvoice_clone_general_availability_enabled"] = True
    body = mod.CosyVoiceControlSettings(**payload)

    saved: list[AdminSettings] = []
    monkeypatch.setattr(mod, "load_settings", lambda: current)
    monkeypatch.setattr(mod, "save_settings", lambda settings: saved.append(settings))
    monkeypatch.setattr(mod, "_runtime_status", lambda settings: {"ok": True})

    response = await mod.update_cosyvoice_control(
        body,
        user=SimpleNamespace(role="admin"),
    )

    assert response["settings"]["express_cosyvoice_auto_clone_enabled"] is True
    assert response["settings"]["cosyvoice_clone_general_availability_enabled"] is True
    assert saved and saved[0].smart_mode_enabled is True
    assert saved[0].smart_auto_clone_enabled is False
    assert saved[0].free_user_max_duration_minutes == 42.0


def test_runtime_status_shape_does_not_return_secret_values(monkeypatch):
    import admin_cosyvoice_control_api as mod
    from admin_settings import AdminSettings

    runtime = mod._runtime_status(AdminSettings())
    text = repr(runtime)
    assert "mainland_voice_worker_hmac_secret" not in text
    assert "cosyvoice_oss_access_key_secret" not in text
    assert "hmac_secret_configured" in runtime["mainland_worker"]
    assert "missing_config_fields" in runtime["sample_uploader"]


def test_frontend_page_uses_narrow_endpoint_not_full_admin_settings():
    src = ADMIN_COSYVOICE_PAGE.read_text(encoding="utf-8")
    api_src = ADMIN_COSYVOICE_API.read_text(encoding="utf-8")

    assert "/api/admin/cosyvoice-control" in api_src
    assert "/api/admin/settings" not in src
    assert "updateAdminCosyvoiceControl" in src
    assert "mainland_voice_worker_hmac_secret" not in src
    assert "cosyvoice_oss_access_key_secret" not in src


def test_frontend_page_exposes_core_control_sections():
    src = ADMIN_COSYVOICE_PAGE.read_text(encoding="utf-8")
    for label in [
        "运行时状态",
        "端点策略",
        "手动克隆",
        "Express 自动克隆",
        "保存 CosyVoice 配置",
    ]:
        assert label in src


def test_app_shell_links_to_cosyvoice_admin_page():
    src = APP_SHELL.read_text(encoding="utf-8")
    assert 'href: "/admin/cosyvoice"' in src
