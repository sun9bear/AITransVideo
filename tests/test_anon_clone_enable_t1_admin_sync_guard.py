"""P1 守卫 — 匿名/快捷 CosyVoice + 智能版 MiniMax 克隆 admin 旋钮双端同步.

plan 2026-06-14-anonymous-express-cosyvoice-clone-enable §4.1.

``POST /api/admin/settings`` 是 full-body 整文档替换语义：新增 admin 字段
必须同一 commit 内完成 ① gateway AdminSettings Pydantic 字段 + validator；
② 前端 admin 设置页 interface + DEFAULT_SETTINGS + reset；③ 本守卫断言
两端字段集一致——任一侧漏改，旧前端保存会把新字段静默打回默认（admin 也
就无法用 UI 开灰度）。

模式沿用 ``test_anonymous_express_t0_admin_sync_guard.py``（Python 静态扫描，
repo 无 JS test runner）。所有 6 个旋钮**默认 OFF / 休眠值**（真钱/真克隆
灰度由项目主用 admin 旋钮开）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ADMIN_SETTINGS_PAGE = (
    REPO_ROOT / "frontend-next" / "src" / "app" / "(app)" / "admin"
    / "settings" / "page.tsx"
)

# 双端契约：{字段名: (TS 类型 regex, TS 默认值 regex, Pydantic 默认值)}
CLONE_FIELDS: dict[str, tuple[str, str, object]] = {
    # 匿名/快捷 CosyVoice 免费克隆（§3.4）
    "anonymous_express_cosyvoice_clone_enabled": (r"boolean", r"false", False),
    "anonymous_clone_daily_global_cap": (r"number", r"100", 100),
    "anonymous_clone_active_cap": (r"number", r"20", 20),
    # 智能版 MiniMax 克隆预览（§5）
    "smart_preview_clone_enabled": (r"boolean", r"false", False),
    "smart_preview_clone_daily_global_cap": (r"number", r"200", 200),
    "smart_preview_clone_inflight_cap": (r"number", r"5", 5),
}

_BOOL_FIELDS = {
    "anonymous_express_cosyvoice_clone_enabled",
    "smart_preview_clone_enabled",
}
_INT_CAP_FIELDS = {
    "anonymous_clone_daily_global_cap",
    "anonymous_clone_active_cap",
    "smart_preview_clone_daily_global_cap",
    "smart_preview_clone_inflight_cap",
}


def _read_page() -> str:
    assert ADMIN_SETTINGS_PAGE.exists(), f"admin settings page 不存在: {ADMIN_SETTINGS_PAGE}"
    return ADMIN_SETTINGS_PAGE.read_text(encoding="utf-8")


def _backend_settings_cls():
    from admin_settings import AdminSettings

    return AdminSettings


# ---------------------------------------------------------------------------
# 1. 后端 Pydantic 字段集 + 默认值
# ---------------------------------------------------------------------------


def test_backend_has_clone_fields_with_expected_defaults():
    cls = _backend_settings_cls()
    defaults = cls()
    for field_name, (_ts_type, _ts_default, py_default) in CLONE_FIELDS.items():
        assert field_name in cls.model_fields, (
            f"gateway AdminSettings 缺字段 {field_name}"
        )
        assert getattr(defaults, field_name) == py_default, (
            f"gateway AdminSettings.{field_name} 默认值应为 {py_default!r}，"
            f"实际 {getattr(defaults, field_name)!r}"
        )


def test_backend_clone_master_switches_are_strict_bool():
    """两个克隆主开关必须 StrictBool：'1'/'on'/'true' 不得被宽松解析为 True
    （否则 admin UI bug 可能意外打开真克隆 / 真钱路径）。"""
    cls = _backend_settings_cls()
    for field in _BOOL_FIELDS:
        for bad in ("1", "on", "true", 1):
            with pytest.raises(Exception):
                cls(**{field: bad})


def test_backend_clone_cap_bounds():
    """4 个克隆 cap 边界：下界 ≥1（禁用走主开关，不靠 cap=0）、拒天文数字。"""
    cls = _backend_settings_cls()
    for field in _INT_CAP_FIELDS:
        for bad in (0, -1):
            with pytest.raises(Exception):
                cls(**{field: bad})
        # 边界下值 1 必收
        assert getattr(cls(**{field: 1}), field) == 1


def test_backend_clone_cap_bounds_reject_astronomical():
    """上界拒天文数字（成本敞口失控）。"""
    cls = _backend_settings_cls()
    for field in _INT_CAP_FIELDS:
        with pytest.raises(Exception):
            cls(**{field: 10**9})


def test_backend_clone_caps_not_in_apf_limit_bounds_table():
    """克隆 cap 不得扩进 _APF_LIMIT_BOUNDS——那张表被
    test_anonymous_preview_limits_knobs 钉死为 free 6 旋钮契约。"""
    import admin_settings as adm

    for field in _INT_CAP_FIELDS:
        assert field not in adm._APF_LIMIT_BOUNDS


# ---------------------------------------------------------------------------
# 2. 前端 interface + DEFAULT_SETTINGS + reset 同步
# ---------------------------------------------------------------------------


def test_frontend_interface_contains_clone_fields():
    src = _read_page()
    m = re.search(r"interface\s+AdminSettings\s*\{(?P<body>[\s\S]*?)\n\}", src)
    assert m, "找不到 AdminSettings interface 定义"
    body = m.group("body")
    for field_name, (ts_type, _d, _p) in CLONE_FIELDS.items():
        field_re = re.compile(
            rf"{re.escape(field_name)}\s*:\s*{ts_type}(?=\s|;|$|//|/\*)"
        )
        assert field_re.search(body), (
            f"前端 AdminSettings interface 缺 `{field_name}: {ts_type}` —— "
            f"full-body POST 会让此字段被后端默认值替换"
        )


def test_frontend_defaults_match_backend_defaults():
    src = _read_page()
    m = re.search(r"DEFAULT_SETTINGS[^=]*=\s*\{(?P<body>[\s\S]*?)\n\}", src)
    assert m, "找不到 DEFAULT_SETTINGS 定义"
    body = m.group("body")
    for field_name, (_t, ts_default, _p) in CLONE_FIELDS.items():
        field_re = re.compile(rf"{re.escape(field_name)}\s*:\s*{ts_default}\s*,")
        assert field_re.search(body), (
            f"前端 DEFAULT_SETTINGS 缺 `{field_name}: <匹配 {ts_default}>` —— "
            f"前后端默认值不一致会让 reset / 初次加载偷偷改后端配置"
        )
    # 双重锁：两个克隆主开关绝不能默认 true（休眠上线，项目主自行灰度）
    for field in _BOOL_FIELDS:
        assert not re.search(rf"{re.escape(field)}\s*:\s*true", body), (
            f"DEFAULT_SETTINGS 不应把 {field} 默认设为 true"
        )


def test_frontend_toggles_use_spread():
    """两个克隆主开关 toggle onChange 必须 spread 保留其它字段。"""
    src = _read_page()
    for field in _BOOL_FIELDS:
        pattern = re.compile(
            r"setSettings\s*\(\s*\(s\)\s*=>\s*\(\s*\{\s*\.\.\.s\s*,"
            rf"[^}}]*{re.escape(field)}"
        )
        assert pattern.search(src), (
            f"{field} toggle 的 onChange 必须用 "
            f"setSettings((s) => ({{ ...s, {field}: ... }})) spread 模式"
        )


def test_frontend_reset_restores_clone_defaults():
    """「恢复默认」必须显式把 6 个克隆字段回 DEFAULT_SETTINGS。"""
    src = _read_page()
    for field_name in CLONE_FIELDS:
        pattern = re.compile(
            rf"{re.escape(field_name)}\s*:\s*\n?\s*DEFAULT_SETTINGS\.{re.escape(field_name)}"
        )
        assert pattern.search(src), (
            f"reset onClick 缺 `{field_name}: DEFAULT_SETTINGS.{field_name}`"
        )


# ---------------------------------------------------------------------------
# 3. round-trip 持久化
# ---------------------------------------------------------------------------


def test_clone_fields_round_trip_persist(monkeypatch, tmp_path):
    """6 个克隆字段保存 → 读回一致（full-body 语义不丢字段）。"""
    import admin_settings as adm

    settings_file = tmp_path / "admin_settings.json"
    monkeypatch.setattr(adm, "SETTINGS_FILE", settings_file)

    body = adm.AdminSettings(
        anonymous_express_cosyvoice_clone_enabled=True,
        anonymous_clone_daily_global_cap=42,
        anonymous_clone_active_cap=7,
        smart_preview_clone_enabled=True,
        smart_preview_clone_daily_global_cap=88,
        smart_preview_clone_inflight_cap=3,
    )
    adm.save_settings(body)
    persisted = json.loads(settings_file.read_text(encoding="utf-8"))
    for field in CLONE_FIELDS:
        assert field in persisted

    loaded = adm.load_settings()
    assert loaded.anonymous_express_cosyvoice_clone_enabled is True
    assert loaded.anonymous_clone_daily_global_cap == 42
    assert loaded.anonymous_clone_active_cap == 7
    assert loaded.smart_preview_clone_enabled is True
    assert loaded.smart_preview_clone_daily_global_cap == 88
    assert loaded.smart_preview_clone_inflight_cap == 3


# ---------------------------------------------------------------------------
# 4. 克隆点数 500 → 600（§4.2，默认 schema 真源）
# ---------------------------------------------------------------------------


def test_pricing_schema_voice_clone_cost_is_600():
    """默认 pricing schema 的 voice_clone_cost_credits = 600（§4.2）。

    生产真源是 /opt/.../config/pricing_runtime.json（缺失才回退此默认）；
    本断言锁默认值，runtime snapshot 发布由项目主单独灰度。
    """
    from pricing_schema import CreditsConfig, build_default_pricing_payload

    # CreditsConfig 字段默认值
    assert CreditsConfig.model_fields["voice_clone_cost_credits"].default == 600
    # builder 产出（pricing_runtime 缺失时的 fallback）
    payload = build_default_pricing_payload()
    assert payload.credits.voice_clone_cost_credits == 600
