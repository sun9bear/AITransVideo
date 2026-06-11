"""T1 验收测试：双端 flag + admin 开关 + 启动校验 + payload 白名单。

plan 2026-06-10 APF T1 验收标准：
  - flag 默认全 False
  - StrictBool 拒 coercion
  - HMAC 密钥缺失且 flag 开 → CRITICAL+降级
  - validate_create_payload：合法/违规矩阵
  - Dockerfile/compose 守卫：NEXT_PUBLIC_ENABLE_ANONYMOUS_PREVIEW 同时出现
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# sys.path 准备
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
_GATEWAY_DIR = str(_REPO_ROOT / "gateway")
if _GATEWAY_DIR not in sys.path:
    sys.path.insert(0, _GATEWAY_DIR)


# ---------------------------------------------------------------------------
# 1. GatewaySettings 默认值
# ---------------------------------------------------------------------------

class TestGatewaySettingsDefaults:
    def test_enable_anonymous_preview_defaults_false(self, monkeypatch):
        """enable_anonymous_preview 默认必须是 False（双端 flag 默认关）。"""
        monkeypatch.delenv("AVT_ENABLE_ANONYMOUS_PREVIEW", raising=False)
        # 重新导入以消除模块级缓存影响
        import importlib
        import config as cfg_mod
        importlib.reload(cfg_mod)
        s = cfg_mod.GatewaySettings()
        assert s.enable_anonymous_preview is False

    def test_anonymous_preview_max_seconds_default(self, monkeypatch):
        monkeypatch.delenv("AVT_ANONYMOUS_PREVIEW_MAX_SECONDS", raising=False)
        import importlib
        import config as cfg_mod
        importlib.reload(cfg_mod)
        s = cfg_mod.GatewaySettings()
        assert s.anonymous_preview_max_seconds == 180

    def test_anonymous_preview_max_upload_bytes_default(self, monkeypatch):
        monkeypatch.delenv("AVT_ANONYMOUS_PREVIEW_MAX_UPLOAD_BYTES", raising=False)
        import importlib
        import config as cfg_mod
        importlib.reload(cfg_mod)
        s = cfg_mod.GatewaySettings()
        assert s.anonymous_preview_max_upload_bytes == 200 * 1024 * 1024

    def test_anonymous_preview_caps_defaults(self, monkeypatch):
        import importlib
        import config as cfg_mod
        importlib.reload(cfg_mod)
        s = cfg_mod.GatewaySettings()
        assert s.anonymous_preview_cap_global_per_day == 500
        assert s.anonymous_preview_cap_per_ip == 3
        assert s.anonymous_preview_cap_per_device == 1
        assert s.anonymous_preview_cap_per_source == 1

    def test_anonymous_preview_hash_secret_default_empty(self, monkeypatch):
        monkeypatch.delenv("AVT_ANONYMOUS_PREVIEW_HASH_SECRET", raising=False)
        import importlib
        import config as cfg_mod
        importlib.reload(cfg_mod)
        s = cfg_mod.GatewaySettings()
        assert s.anonymous_preview_hash_secret == ""


# ---------------------------------------------------------------------------
# 2. AdminSettings.anonymous_free_preview_enabled — StrictBool
# ---------------------------------------------------------------------------

class TestAdminSettingsAnonymousPreview:
    def _load_admin_settings(self):
        import importlib
        import admin_settings as adm
        importlib.reload(adm)
        return adm.AdminSettings

    def test_anonymous_free_preview_enabled_defaults_false(self):
        AdminSettings = self._load_admin_settings()
        s = AdminSettings()
        assert s.anonymous_free_preview_enabled is False

    def test_anonymous_preview_max_in_flight_default(self):
        AdminSettings = self._load_admin_settings()
        s = AdminSettings()
        assert s.anonymous_preview_max_in_flight == 2

    def test_anonymous_free_preview_explicit_true(self):
        AdminSettings = self._load_admin_settings()
        s = AdminSettings(anonymous_free_preview_enabled=True)
        assert s.anonymous_free_preview_enabled is True

    @pytest.mark.parametrize("bad_value", [
        # 字符串形态 — 普通 bool Pydantic 宽松解析为 True，StrictBool 拒
        "true", "True", "TRUE", "1", "0", "yes", "on", "false", "False",
        # int 形态
        1, 0, -1,
        # 其他类型
        1.0, None, [], {},
    ])
    def test_anonymous_free_preview_strict_bool_rejects_non_bool(self, bad_value):
        """StrictBool 必须拒绝所有非 Python bool 输入（防 admin UI bug 意外开启）。"""
        AdminSettings = self._load_admin_settings()
        with pytest.raises(ValidationError):
            AdminSettings(anonymous_free_preview_enabled=bad_value)

    def test_anonymous_free_preview_field_uses_strict_bool_metadata(self):
        """架构守卫：确认字段实际声明是 StrictBool。"""
        from pydantic.types import Strict
        AdminSettings = self._load_admin_settings()
        field = AdminSettings.model_fields["anonymous_free_preview_enabled"]
        strict_markers = [m for m in field.metadata if isinstance(m, Strict)]
        assert strict_markers, (
            "anonymous_free_preview_enabled 必须用 StrictBool，"
            f"当前 metadata: {field.metadata}"
        )


# ---------------------------------------------------------------------------
# 3. validate_anonymous_preview_config — 降级型校验
# ---------------------------------------------------------------------------

class TestValidateAnonymousPreviewConfig:
    def _get_validator(self):
        import importlib
        import startup_checks as sc
        importlib.reload(sc)
        return sc.validate_anonymous_preview_config

    def _make_settings(self, enabled: bool, secret: str):
        """构造最小 settings 对象（dataclass-like namespace）。"""
        import types
        s = types.SimpleNamespace(
            enable_anonymous_preview=enabled,
            anonymous_preview_hash_secret=secret,
            anonymous_preview_max_seconds=180,
            anonymous_preview_cap_global_per_day=500,
            anonymous_preview_cap_per_ip=3,
            anonymous_preview_cap_per_device=1,
            anonymous_preview_cap_per_source=1,
        )
        return s

    def test_flag_off_is_noop(self):
        validate = self._get_validator()
        s = self._make_settings(enabled=False, secret="")
        validate(s)  # must not raise, must not mutate
        assert s.enable_anonymous_preview is False

    def test_flag_on_secret_missing_downgrades(self, caplog):
        validate = self._get_validator()
        s = self._make_settings(enabled=True, secret="")
        with caplog.at_level(logging.CRITICAL, logger="startup_checks"):
            validate(s)
        assert s.enable_anonymous_preview is False
        assert any("CRITICAL" in r.levelname or r.levelno >= logging.CRITICAL
                   for r in caplog.records), "Must emit CRITICAL log on downgrade"

    def test_flag_on_secret_too_short_downgrades(self, caplog):
        validate = self._get_validator()
        s = self._make_settings(enabled=True, secret="short_secret")
        with caplog.at_level(logging.CRITICAL, logger="startup_checks"):
            validate(s)
        assert s.enable_anonymous_preview is False

    def test_flag_on_secret_exactly_31_bytes_downgrades(self, caplog):
        validate = self._get_validator()
        secret_31 = "a" * 31
        s = self._make_settings(enabled=True, secret=secret_31)
        with caplog.at_level(logging.CRITICAL, logger="startup_checks"):
            validate(s)
        assert s.enable_anonymous_preview is False

    def test_flag_on_secret_exactly_32_bytes_ok(self, caplog):
        validate = self._get_validator()
        secret_32 = "a" * 32
        s = self._make_settings(enabled=True, secret=secret_32)
        with caplog.at_level(logging.CRITICAL, logger="startup_checks"):
            validate(s)
        # Must NOT downgrade
        assert s.enable_anonymous_preview is True
        assert not any(r.levelno >= logging.CRITICAL for r in caplog.records)

    def test_flag_on_long_secret_ok(self):
        validate = self._get_validator()
        import secrets
        secret = secrets.token_urlsafe(32)
        s = self._make_settings(enabled=True, secret=secret)
        validate(s)
        assert s.enable_anonymous_preview is True


# ---------------------------------------------------------------------------
# 4. validate_create_payload
# ---------------------------------------------------------------------------

class TestValidateCreatePayload:
    def _get_validator(self):
        import importlib
        import anonymous_preview_payload_spec as spec
        importlib.reload(spec)
        return spec.validate_create_payload

    def test_minimal_valid_payload_returns_empty_list(self):
        validate = self._get_validator()
        payload = {
            "job_type": "localize_video",
            "source_type": "local_video",
            "source_ref": "/tmp/teaser.mp4",
            "output_target": "editor",
            "service_mode": "free",
            "requires_review": False,
            "voice_strategy": "preset_mapping",
            "tts_provider": "mimo",
            "source_content_hash": "abc123",
            "anonymous_preview": True,
        }
        assert validate(payload) == []

    def test_empty_payload_returns_empty_list(self):
        validate = self._get_validator()
        assert validate({}) == []

    def test_forbidden_voice_clone_field_reported(self):
        validate = self._get_validator()
        payload = {"job_type": "localize_video", "voice_clone": "some_voice_id"}
        violations = validate(payload)
        assert "voice_clone" in violations

    def test_forbidden_voiceclone_reference_path_reported(self):
        validate = self._get_validator()
        payload = {"voiceclone_reference_path": "/tmp/sample.wav"}
        violations = validate(payload)
        assert "voiceclone_reference_path" in violations

    def test_forbidden_voice_a_reported(self):
        validate = self._get_validator()
        violations = validate({"voice_a": "voice_123"})
        assert "voice_a" in violations

    def test_forbidden_voice_b_reported(self):
        validate = self._get_validator()
        violations = validate({"voice_b": "voice_456"})
        assert "voice_b" in violations

    def test_forbidden_free_consent_reported(self):
        validate = self._get_validator()
        violations = validate({"free_consent": {"voice_rights_confirmed": True}})
        assert "free_consent" in violations

    def test_unknown_field_outside_whitelist_reported(self):
        validate = self._get_validator()
        violations = validate({"totally_unknown_field": "x"})
        assert "totally_unknown_field" in violations

    def test_multiple_violations_all_reported(self):
        validate = self._get_validator()
        payload = {
            "job_type": "localize_video",       # allowed
            "voice_clone": "v1",                # forbidden
            "secret_extra": "x",                # not in whitelist
            "voiceclone_reference_path": "/x",  # forbidden
        }
        violations = validate(payload)
        assert "voice_clone" in violations
        assert "secret_extra" in violations
        assert "voiceclone_reference_path" in violations
        assert "job_type" not in violations

    def test_anonymous_preview_marker_is_allowed(self):
        validate = self._get_validator()
        violations = validate({"anonymous_preview": True})
        assert "anonymous_preview" not in violations


# ---------------------------------------------------------------------------
# 5. Dockerfile / compose 守卫
# ---------------------------------------------------------------------------

class TestBuildArgGuards:
    """确认 NEXT_PUBLIC_ENABLE_ANONYMOUS_PREVIEW 同时出现在
    frontend-next/Dockerfile 和 docker-compose.yml。"""

    _DOCKERFILE = _REPO_ROOT / "frontend-next" / "Dockerfile"
    _COMPOSE = _REPO_ROOT / "docker-compose.yml"
    _TOKEN = "NEXT_PUBLIC_ENABLE_ANONYMOUS_PREVIEW"

    def test_token_in_frontend_dockerfile(self):
        if not self._DOCKERFILE.exists():
            pytest.skip("frontend-next/Dockerfile not present")
        text = self._DOCKERFILE.read_text(encoding="utf-8")
        assert self._TOKEN in text, (
            f"{self._TOKEN} must appear in frontend-next/Dockerfile "
            "(ARG + ENV pattern, same as NEXT_PUBLIC_PADDLE_CLIENT_TOKEN). "
            "Without it the build arg is silently ignored and the flag never "
            "reaches the client bundle."
        )

    def test_token_in_docker_compose(self):
        if not self._COMPOSE.exists():
            pytest.skip("docker-compose.yml not present")
        text = self._COMPOSE.read_text(encoding="utf-8")
        assert self._TOKEN in text, (
            f"{self._TOKEN} must appear in docker-compose.yml "
            "(next service build args section). "
            "Without it docker-compose build never passes the arg to the Dockerfile."
        )
