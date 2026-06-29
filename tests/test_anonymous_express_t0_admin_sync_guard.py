"""APF 匿名 Express lane admin 旋钮守卫 — plan 2026-06-12 anonymous-express-preview T0.

``POST /api/admin/settings`` 是 full-body 整文档替换语义：新增字段必须
同一 commit 内完成 ① gateway AdminSettings Pydantic 字段+validator；
② 前端 admin 设置页 interface + DEFAULT_SETTINGS + reset；③ 本守卫断言
两端字段集一致——任一侧漏改，旧前端保存会把新字段静默打回默认。

另含 MiMo 组合双向硬拒（plan §E 双层之一：admin 保存校验 422）：
anonymous_express_enabled=true ⇄ express_tts_provider=mimo 互斥。
MiMo 海外恒定 mia 音色（gender 不参与选音），匿名 express 用它必然
音色错配，违背"免费触点必须真实管线效果"最高指导原则。

模式沿用 ``test_chunked_upload_admin_sync_guard.py``（Python 静态扫描，
repo 无 JS test runner）。
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ADMIN_SETTINGS_PAGE = (
    REPO_ROOT / "frontend-next" / "src" / "app" / "[locale]" / "(app)" / "admin"
    / "settings" / "page.tsx"
)

# 双端契约：{字段名: (TS 类型 regex, TS 默认值 regex, Pydantic 默认值)}
EXPRESS_FIELDS: dict[str, tuple[str, str, object]] = {
    "anonymous_express_enabled": (r"boolean", r"false", False),
    "anonymous_express_daily_global_cap": (r"number", r"50", 50),
}

_BOOL_FIELDS = {"anonymous_express_enabled"}


def _read_page() -> str:
    assert ADMIN_SETTINGS_PAGE.exists(), f"admin settings page 不存在: {ADMIN_SETTINGS_PAGE}"
    return ADMIN_SETTINGS_PAGE.read_text(encoding="utf-8")


def _backend_settings_cls():
    from admin_settings import AdminSettings

    return AdminSettings


# ---------------------------------------------------------------------------
# 1. 后端 Pydantic 字段集 + 默认值
# ---------------------------------------------------------------------------


def test_backend_has_express_fields_with_expected_defaults():
    cls = _backend_settings_cls()
    defaults = cls()
    for field_name, (_ts_type, _ts_default, py_default) in EXPRESS_FIELDS.items():
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


def test_backend_cap_bounds():
    """express 子闸 cap 边界 [1, 100000]：0/负数/天文数字被拒，边界值收。"""
    cls = _backend_settings_cls()
    for bad in (0, -1, 100001):
        with pytest.raises(Exception):
            cls(anonymous_express_daily_global_cap=bad)
    assert cls(anonymous_express_daily_global_cap=1).anonymous_express_daily_global_cap == 1
    assert (
        cls(anonymous_express_daily_global_cap=100000).anonymous_express_daily_global_cap
        == 100000
    )


def test_backend_apf_limit_bounds_table_untouched():
    """express cap 不得扩进 _APF_LIMIT_BOUNDS——那张表被
    test_anonymous_preview_limits_knobs 钉死为 free 6 旋钮契约。"""
    import admin_settings as adm

    assert "anonymous_express_daily_global_cap" not in adm._APF_LIMIT_BOUNDS


# ---------------------------------------------------------------------------
# 2. 前端 interface + DEFAULT_SETTINGS + reset 同步
# ---------------------------------------------------------------------------


def test_frontend_interface_contains_express_fields():
    src = _read_page()
    m = re.search(r"interface\s+AdminSettings\s*\{(?P<body>[\s\S]*?)\n\}", src)
    assert m, "找不到 AdminSettings interface 定义"
    body = m.group("body")
    for field_name, (ts_type, _d, _p) in EXPRESS_FIELDS.items():
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
    for field_name, (_t, ts_default, _p) in EXPRESS_FIELDS.items():
        field_re = re.compile(rf"{re.escape(field_name)}\s*:\s*{ts_default}\s*,")
        assert field_re.search(body), (
            f"前端 DEFAULT_SETTINGS 缺 `{field_name}: <匹配 {ts_default}>` —— "
            f"前后端默认值不一致会让 reset / 初次加载偷偷改后端配置"
        )
    # 双重锁：主开关绝不能默认 true（休眠上线，项目主自行灰度）
    assert not re.search(r"anonymous_express_enabled\s*:\s*true", body), (
        "DEFAULT_SETTINGS 不应把 anonymous_express_enabled 默认设为 true"
    )


def test_frontend_toggle_uses_spread():
    """toggle onChange 必须 spread 保留其它字段（full-body save 不丢字段）。"""
    src = _read_page()
    pattern = re.compile(
        r"setSettings\s*\(\s*\(s\)\s*=>\s*\(\s*\{\s*\.\.\.s\s*,"
        r"[^}]*anonymous_express_enabled"
    )
    assert pattern.search(src), (
        "anonymous_express_enabled toggle 的 onChange 必须用 "
        "setSettings((s) => ({ ...s, anonymous_express_enabled: ... })) spread 模式"
    )


def test_frontend_reset_restores_express_defaults():
    """「恢复默认」必须显式把两个 express 字段回 DEFAULT_SETTINGS
    （可见控件复位语义 + 防将来 reset 改为白名单透传时漏掉）。"""
    src = _read_page()
    for field_name in EXPRESS_FIELDS:
        pattern = re.compile(
            rf"{re.escape(field_name)}\s*:\s*\n?\s*DEFAULT_SETTINGS\.{re.escape(field_name)}"
        )
        assert pattern.search(src), (
            f"reset onClick 缺 `{field_name}: DEFAULT_SETTINGS.{field_name}`"
        )


# ---------------------------------------------------------------------------
# 3. MiMo 组合双向硬拒（plan §E：admin 保存校验 422）
# ---------------------------------------------------------------------------


def _make_admin_user():
    # _require_admin 只读 role 属性（duck typing），无需真 ORM 行
    return SimpleNamespace(role="admin")


def test_mimo_exclusion_helper_rejects_combo():
    """组合命中 → HTTPException 422，detail 含可操作文案。"""
    import admin_settings as adm
    from fastapi import HTTPException

    combo = adm.AdminSettings(
        anonymous_express_enabled=True, express_tts_provider="mimo"
    )
    with pytest.raises(HTTPException) as exc_info:
        adm.validate_anonymous_express_tts_exclusion(combo)
    assert exc_info.value.status_code == 422
    assert "express TTS provider" in str(exc_info.value.detail)


def test_mimo_exclusion_helper_case_insensitive():
    """provider 大小写/空白变体不得绕过硬拒。"""
    import admin_settings as adm
    from fastapi import HTTPException

    for variant in ("MiMo", "MIMO", " mimo "):
        combo = adm.AdminSettings(
            anonymous_express_enabled=True, express_tts_provider=variant
        )
        with pytest.raises(HTTPException):
            adm.validate_anonymous_express_tts_exclusion(combo)


def test_mimo_exclusion_helper_allows_valid_combos():
    """单独任一字段合法：express+cosyvoice 放行；free 档继续用 mimo 放行。"""
    import admin_settings as adm

    adm.validate_anonymous_express_tts_exclusion(
        adm.AdminSettings(
            anonymous_express_enabled=True, express_tts_provider="cosyvoice"
        )
    )
    adm.validate_anonymous_express_tts_exclusion(
        adm.AdminSettings(
            anonymous_express_enabled=False, express_tts_provider="mimo"
        )
    )


def test_post_settings_mimo_combo_returns_422_and_does_not_persist(
    monkeypatch, tmp_path
):
    """endpoint 集成：命中组合 → 422 且不落盘（拒绝即不产生半状态）。

    双向语义：full-body POST 看终态——无论本次翻的是 enabled 还是
    provider，组合命中即拒，天然覆盖两个方向。
    """
    import admin_settings as adm
    from fastapi import HTTPException

    settings_file = tmp_path / "admin_settings.json"
    monkeypatch.setattr(adm, "SETTINGS_FILE", settings_file)

    combo = adm.AdminSettings(
        anonymous_express_enabled=True, express_tts_provider="mimo"
    )
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(adm.update_admin_settings(combo, _make_admin_user()))
    assert exc_info.value.status_code == 422
    assert not settings_file.exists(), "422 拒绝时不得写 admin_settings.json"


def test_post_settings_valid_express_combo_persists(monkeypatch, tmp_path):
    """合法组合保存成功且两个新字段持久化（round-trip）。"""
    import admin_settings as adm

    settings_file = tmp_path / "admin_settings.json"
    monkeypatch.setattr(adm, "SETTINGS_FILE", settings_file)

    body = adm.AdminSettings(
        anonymous_express_enabled=True,
        express_tts_provider="cosyvoice",
        anonymous_express_daily_global_cap=20,
    )
    result = asyncio.run(adm.update_admin_settings(body, _make_admin_user()))
    assert result["settings"]["anonymous_express_enabled"] is True
    assert result["settings"]["anonymous_express_daily_global_cap"] == 20

    persisted = json.loads(settings_file.read_text(encoding="utf-8"))
    assert persisted["anonymous_express_enabled"] is True
    assert persisted["anonymous_express_daily_global_cap"] == 20

    loaded = adm.load_settings()
    assert loaded.anonymous_express_enabled is True
    assert loaded.anonymous_express_daily_global_cap == 20
