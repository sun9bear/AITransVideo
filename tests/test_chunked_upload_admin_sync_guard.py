"""分片上传 admin 旋钮三处同步守卫 — plan 2026-06-11 §3.7.

``POST /api/admin/settings`` 是 full-body 整文档替换语义：新增字段必须
同一 commit 内完成 ① gateway AdminSettings Pydantic 字段+validator；
② 前端 admin 设置页 interface + DEFAULT_SETTINGS；③ 本守卫断言两端
字段集一致——任一侧漏改，旧前端保存会把新字段静默打回默认。

模式沿用 ``test_phase42_d1_admin_settings_ui_guard.py``（Python 静态扫描，
repo 无 JS test runner）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ADMIN_SETTINGS_PAGE = (
    REPO_ROOT / "frontend-next" / "src" / "app" / "(app)" / "admin"
    / "settings" / "page.tsx"
)

# 双端契约：{字段名: (TS 类型 regex, TS 默认值 regex, Pydantic 默认值)}
CHUNKED_FIELDS: dict[str, tuple[str, str, object]] = {
    "chunked_upload_enabled": (r"boolean", r"false", False),
    "chunked_upload_max_file_mb": (r"number", r"2048", 2048),
    "chunked_upload_chunk_mb": (r"number", r"64", 64),
    "chunked_upload_per_user_active": (r"number", r"2", 2),
    "chunked_upload_per_user_inflight_gb": (r"number", r"4", 4),
    "chunked_upload_global_inflight_gb": (r"number", r"20", 20),
    "chunked_upload_daily_per_user_gb": (r"number", r"8", 8),
    "chunked_upload_disk_floor_gb": (r"number", r"20", 20),
    "chunked_upload_ttl_hours": (r"number", r"24", 24),
    "chunked_upload_ready_ttl_hours": (r"number", r"6", 6),
    # --- 匿名档分片扩展（plan §9 r1，2026-06-12）---
    "chunked_upload_anonymous_enabled": (r"boolean", r"false", False),
    "chunked_upload_anonymous_ttl_hours": (r"number", r"6", 6),
    "chunked_upload_anonymous_daily_gb": (r"number", r"5", 5),
}

# 布尔主开关集合：StrictBool 语义 + 不参与数字下界测试。
_BOOL_FIELDS = {"chunked_upload_enabled", "chunked_upload_anonymous_enabled"}


def _read_page() -> str:
    assert ADMIN_SETTINGS_PAGE.exists(), f"admin settings page 不存在: {ADMIN_SETTINGS_PAGE}"
    return ADMIN_SETTINGS_PAGE.read_text(encoding="utf-8")


def _backend_settings_cls():
    from admin_settings import AdminSettings

    return AdminSettings


# ---------------------------------------------------------------------------
# 1. 后端 Pydantic 字段集 + 默认值
# ---------------------------------------------------------------------------


def test_backend_has_all_chunked_fields_with_expected_defaults():
    cls = _backend_settings_cls()
    defaults = cls()
    for field_name, (_ts_type, _ts_default, py_default) in CHUNKED_FIELDS.items():
        assert field_name in cls.model_fields, (
            f"gateway AdminSettings 缺字段 {field_name}"
        )
        assert getattr(defaults, field_name) == py_default, (
            f"gateway AdminSettings.{field_name} 默认值应为 {py_default!r}，"
            f"实际 {getattr(defaults, field_name)!r}"
        )


def test_backend_enabled_is_strict_bool():
    """主开关必须 StrictBool：字符串 '1'/'on'/'true' 不得被宽松解析为 True。"""
    cls = _backend_settings_cls()
    for field in _BOOL_FIELDS:
        for bad in ("1", "on", "true", 1):
            with pytest.raises(Exception):
                cls(**{field: bad})


def test_backend_chunk_mb_hard_cap_80():
    """chunk_mb > 80 必须被 validator 拒绝（CF 单请求体硬约束）。"""
    cls = _backend_settings_cls()
    with pytest.raises(Exception):
        cls(chunked_upload_chunk_mb=81)
    assert cls(chunked_upload_chunk_mb=80).chunked_upload_chunk_mb == 80


def test_backend_bounds_reject_zero():
    """数字旋钮下界 ≥1：0 等效误关停且难排查，必须被拒。"""
    cls = _backend_settings_cls()
    for field_name in CHUNKED_FIELDS:
        if field_name in _BOOL_FIELDS:
            continue
        with pytest.raises(Exception):
            cls(**{field_name: 0})


# ---------------------------------------------------------------------------
# 2. 前端 interface + DEFAULT_SETTINGS 同步
# ---------------------------------------------------------------------------


def test_frontend_interface_contains_all_chunked_fields():
    src = _read_page()
    m = re.search(r"interface\s+AdminSettings\s*\{(?P<body>[\s\S]*?)\n\}", src)
    assert m, "找不到 AdminSettings interface 定义"
    body = m.group("body")
    for field_name, (ts_type, _d, _p) in CHUNKED_FIELDS.items():
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
    for field_name, (_t, ts_default, _p) in CHUNKED_FIELDS.items():
        field_re = re.compile(rf"{re.escape(field_name)}\s*:\s*{ts_default}\s*,")
        assert field_re.search(body), (
            f"前端 DEFAULT_SETTINGS 缺 `{field_name}: <匹配 {ts_default}>` —— "
            f"前后端默认值不一致会让 reset / 初次加载偷偷改后端配置"
        )
    # 双重锁：主开关绝不能默认 true（部署后休眠灰度）
    assert not re.search(r"chunked_upload_enabled\s*:\s*true", body), (
        "DEFAULT_SETTINGS 不应把 chunked_upload_enabled 默认设为 true"
    )
    assert not re.search(r"chunked_upload_anonymous_enabled\s*:\s*true", body), (
        "DEFAULT_SETTINGS 不应把 chunked_upload_anonymous_enabled 默认设为 true"
    )


def test_frontend_toggle_uses_spread():
    """toggle onChange 必须 spread 保留其它字段（full-body save 不丢字段）。"""
    src = _read_page()
    for field in ("chunked_upload_enabled", "chunked_upload_anonymous_enabled"):
        pattern = re.compile(
            r"setSettings\s*\(\s*\(s\)\s*=>\s*\(\s*\{\s*\.\.\.s\s*,"
            rf"[^}}]*{field}"
        )
        assert pattern.search(src), (
            f"{field} toggle 的 onChange 必须用 "
            f"setSettings((s) => ({{ ...s, {field}: ... }})) spread 模式"
        )


# ---------------------------------------------------------------------------
# 3. resolve_chunked_limits fail-closed
# ---------------------------------------------------------------------------


def test_resolve_chunked_limits_fail_closed(monkeypatch):
    """admin settings 读取异常 → enabled=False（fail-closed），数值回默认。"""
    import chunked_upload_api

    def _boom():
        raise RuntimeError("settings unavailable")

    import admin_settings

    monkeypatch.setattr(admin_settings, "load_settings", _boom)
    limits = chunked_upload_api.resolve_chunked_limits()
    assert limits.enabled is False
    assert limits.max_file_mb == 2048
