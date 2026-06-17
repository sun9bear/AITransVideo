"""APF 限制旋钮（2026-06-11）验收测试。

覆盖：
  1. AdminSettings 6 个新字段默认值（与 env GatewaySettings 出厂默认一致）
  2. _APF_LIMIT_BOUNDS validator 边界矩阵（拒 0/负数/天文数字，收边界值）
  3. ApfLimits 字段名与 GatewaySettings env 字段同名契约（消费方可当 settings 传）
  4. resolve_apf_limits：admin 值优先（含 MB→bytes 转换）/ 任何异常回落 env
  5. GET /gateway/anonymous-preview/limits：flag off → 404；on → 返回 resolver 值
"""
from __future__ import annotations

import sys
from dataclasses import fields as dc_fields
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# sys.path 准备（同 test_anonymous_preview_t1_config.py）
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_REPO_ROOT / "gateway"), str(_REPO_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---------------------------------------------------------------------------
# 1. AdminSettings 新字段默认值
# ---------------------------------------------------------------------------

class TestAdminSettingsApfLimitDefaults:
    def _load_admin_settings(self):
        import admin_settings as adm
        return adm.AdminSettings

    def test_defaults_match_env_factory_values(self):
        """admin 默认值必须与 GatewaySettings env 出厂默认严格一致
        （200MB / 180s / 500 / 3 / 1 / 1）——否则 fallback 行为不连续。"""
        AdminSettings = self._load_admin_settings()
        s = AdminSettings()
        assert s.anonymous_preview_max_upload_mb == 200
        assert s.anonymous_preview_max_seconds == 180
        assert s.anonymous_preview_cap_global_per_day == 500
        assert s.anonymous_preview_cap_per_ip == 3
        assert s.anonymous_preview_cap_per_device == 1
        assert s.anonymous_preview_cap_per_source == 1

    def test_admin_default_upload_mb_equals_env_default_bytes(self):
        import config as cfg
        AdminSettings = self._load_admin_settings()
        env = cfg.GatewaySettings()
        adm = AdminSettings()
        assert adm.anonymous_preview_max_upload_mb * 1024 * 1024 == \
            env.anonymous_preview_max_upload_bytes


# ---------------------------------------------------------------------------
# 2. validator 边界矩阵
# ---------------------------------------------------------------------------

# (field, low, high) 与 gateway/admin_settings.py _APF_LIMIT_BOUNDS 一致
_BOUNDS = [
    ("anonymous_preview_max_upload_mb", 10, 2048),
    ("anonymous_preview_max_seconds", 30, 7200),
    ("anonymous_preview_cap_global_per_day", 1, 100000),
    ("anonymous_preview_cap_per_ip", 1, 1000),
    ("anonymous_preview_cap_per_device", 1, 100),
    ("anonymous_preview_cap_per_source", 1, 100),
]


class TestApfLimitValidatorBounds:
    def _make(self, **kwargs):
        import admin_settings as adm
        return adm.AdminSettings(**kwargs)

    @pytest.mark.parametrize("field,low,high", _BOUNDS)
    def test_boundary_values_accepted(self, field, low, high):
        assert getattr(self._make(**{field: low}), field) == low
        assert getattr(self._make(**{field: high}), field) == high

    @pytest.mark.parametrize("field,low,high", _BOUNDS)
    def test_below_lower_bound_rejected(self, field, low, high):
        """0（误关停）与 low-1 必须拒——cap=0 等效熔断但难排查，熔断走主开关。"""
        with pytest.raises(ValidationError):
            self._make(**{field: 0})
        with pytest.raises(ValidationError):
            self._make(**{field: low - 1})
        with pytest.raises(ValidationError):
            self._make(**{field: -1})

    @pytest.mark.parametrize("field,low,high", _BOUNDS)
    def test_above_upper_bound_rejected(self, field, low, high):
        with pytest.raises(ValidationError):
            self._make(**{field: high + 1})
        with pytest.raises(ValidationError):
            self._make(**{field: 10 ** 9})

    def test_bounds_table_in_sync_with_module(self):
        """本测试的边界表与模块 _APF_LIMIT_BOUNDS 逐项一致（防单侧漂移）。"""
        import admin_settings as adm
        assert dict((f, (lo, hi)) for f, lo, hi in _BOUNDS) == adm._APF_LIMIT_BOUNDS


# ---------------------------------------------------------------------------
# 3. ApfLimits 字段名契约
# ---------------------------------------------------------------------------

class TestApfLimitsFieldNameContract:
    def test_field_names_match_gateway_settings_env_fields(self):
        """ApfLimits 字段必须与 GatewaySettings env 字段完全同名——消费方
        （admit_for_free_preview 等）把 limits 当 settings 直接传的前提。"""
        import config as cfg
        from anonymous_preview_limits import ApfLimits

        limit_fields = {f.name for f in dc_fields(ApfLimits)}
        gw_fields = set(cfg.GatewaySettings.model_fields)
        missing = limit_fields - gw_fields
        assert not missing, (
            f"ApfLimits 字段 {missing} 在 GatewaySettings 没有同名 env 字段，"
            "同名契约破坏（消费方无法把 limits 当 settings 传）"
        )

    def test_covers_all_six_limits(self):
        from anonymous_preview_limits import ApfLimits
        assert {f.name for f in dc_fields(ApfLimits)} == {
            "anonymous_preview_max_upload_bytes",
            "anonymous_preview_max_seconds",
            "anonymous_preview_cap_global_per_day",
            "anonymous_preview_cap_per_ip",
            "anonymous_preview_cap_per_device",
            "anonymous_preview_cap_per_source",
        }


# ---------------------------------------------------------------------------
# 4. resolve_apf_limits — admin 优先 / 异常 fallback
# ---------------------------------------------------------------------------

class TestResolveApfLimits:
    def _env(self):
        """env fallback 快照（不依赖真实 env，用 SimpleNamespace 注入）。"""
        return SimpleNamespace(
            anonymous_preview_max_upload_bytes=200 * 1024 * 1024,
            anonymous_preview_max_seconds=180,
            anonymous_preview_cap_global_per_day=500,
            anonymous_preview_cap_per_ip=3,
            anonymous_preview_cap_per_device=1,
            anonymous_preview_cap_per_source=1,
        )

    def test_admin_values_win_with_mb_to_bytes_conversion(self, monkeypatch):
        import admin_settings as adm
        import anonymous_preview_limits as limits_mod

        monkeypatch.setattr(
            adm,
            "load_settings",
            lambda: adm.AdminSettings(
                anonymous_preview_max_upload_mb=500,
                anonymous_preview_max_seconds=300,
                anonymous_preview_cap_global_per_day=1000,
                anonymous_preview_cap_per_ip=10,
                anonymous_preview_cap_per_device=2,
                anonymous_preview_cap_per_source=3,
            ),
        )
        limits = limits_mod.resolve_apf_limits(self._env())
        assert limits.anonymous_preview_max_upload_bytes == 500 * 1024 * 1024
        assert limits.anonymous_preview_max_seconds == 300
        assert limits.anonymous_preview_cap_global_per_day == 1000
        assert limits.anonymous_preview_cap_per_ip == 10
        assert limits.anonymous_preview_cap_per_device == 2
        assert limits.anonymous_preview_cap_per_source == 3

    def test_load_settings_raises_falls_back_to_env(self, monkeypatch):
        import admin_settings as adm
        import anonymous_preview_limits as limits_mod

        def _boom():
            raise OSError("config dir unreadable")

        monkeypatch.setattr(adm, "load_settings", _boom)
        limits = limits_mod.resolve_apf_limits(self._env())
        assert limits.anonymous_preview_max_upload_bytes == 200 * 1024 * 1024
        assert limits.anonymous_preview_cap_global_per_day == 500

    def test_admin_object_missing_fields_falls_back_to_env(self, monkeypatch):
        """load_settings 被外部 stub 成缺字段对象（如旧版回滚）→ AttributeError
        → 同样走 env fallback，不向调用方抛异常。"""
        import admin_settings as adm
        import anonymous_preview_limits as limits_mod

        monkeypatch.setattr(adm, "load_settings", lambda: SimpleNamespace())
        limits = limits_mod.resolve_apf_limits(self._env())
        assert limits.anonymous_preview_cap_per_ip == 3

    def test_result_is_frozen(self, monkeypatch):
        import admin_settings as adm
        import anonymous_preview_limits as limits_mod

        monkeypatch.setattr(adm, "load_settings", lambda: adm.AdminSettings())
        limits = limits_mod.resolve_apf_limits(self._env())
        with pytest.raises(Exception):
            limits.anonymous_preview_cap_per_ip = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 5. GET /gateway/anonymous-preview/limits
# ---------------------------------------------------------------------------

class TestLimitsEndpoint:
    def _client(self, monkeypatch, *, flag_enabled: bool, limits=None):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import anonymous_preview_api as api
        from anonymous_preview_limits import ApfLimits

        monkeypatch.setattr(
            api.settings, "enable_anonymous_preview", flag_enabled, raising=False
        )
        if limits is None:
            limits = ApfLimits(
                anonymous_preview_max_upload_bytes=512 * 1024 * 1024,
                anonymous_preview_max_seconds=240,
                anonymous_preview_cap_global_per_day=500,
                anonymous_preview_cap_per_ip=3,
                anonymous_preview_cap_per_device=1,
                anonymous_preview_cap_per_source=1,
            )
        monkeypatch.setattr(api, "resolve_apf_limits", lambda: limits)
        # plan 2026-06-12 §G：/limits 增返 active_lane / master_open，
        # 测试钉死 resolver 隔离 admin_settings.json 文件状态。
        monkeypatch.setattr(api, "_resolve_active_lane", lambda: "free")

        app = FastAPI()
        app.include_router(api.router)
        return TestClient(app, raise_server_exceptions=False)

    def test_flag_off_returns_404(self, monkeypatch):
        client = self._client(monkeypatch, flag_enabled=False)
        resp = client.get("/gateway/anonymous-preview/limits")
        assert resp.status_code == 404
        assert resp.json() == {"error": "not_found"}

    def test_flag_on_returns_resolved_values(self, monkeypatch):
        client = self._client(monkeypatch, flag_enabled=True)
        resp = client.get("/gateway/anonymous-preview/limits")
        assert resp.status_code == 200
        # plan 2026-06-12 §G：增返 lane 三态字段（前端面板渲染依据）
        assert resp.json() == {
            "max_upload_mb": 512,
            "preview_seconds": 240,
            "active_lane": "free",
            "master_open": True,
        }

    def test_no_session_or_csrf_required(self, monkeypatch):
        """GET 只读端点：无 cookie、无 Origin header 也必须可访问。"""
        client = self._client(monkeypatch, flag_enabled=True)
        resp = client.get(
            "/gateway/anonymous-preview/limits",
            headers={},
        )
        assert resp.status_code == 200

    def test_literal_path_not_shadowed_by_preview_id_routes(self, monkeypatch):
        """'/limits' 不得被 '/{preview_id}/...' 动态路由吞掉（两段 vs 一段，
        此测试钉死路由形状防未来加单段动态路由时回归）。"""
        client = self._client(monkeypatch, flag_enabled=True)
        resp = client.get("/gateway/anonymous-preview/limits")
        assert resp.status_code == 200
        assert "max_upload_mb" in resp.json()
