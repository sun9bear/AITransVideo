"""守卫 — 匿名预览→登录认领 admin 旋钮 anonymous_preview_claim_enabled 双端同步.

plan 2026-06-15-anonymous-preview-claim-binding-plan.md §9 T1 / §10.

``POST /api/admin/settings`` 是 full-body 整文档替换语义：新增 admin 字段必须同一
commit 内完成 ① gateway AdminSettings Pydantic 字段（StrictBool, default False）；
② 前端 admin 设置页 interface + DEFAULT_SETTINGS + reset + toggle spread；③ 本守卫
断言两端一致——任一侧漏改，旧前端保存会把新字段静默打回默认（admin 无法用 UI 开灰度）。

模式沿用 ``test_anon_clone_enable_t1_admin_sync_guard.py``（Python 静态扫描，repo 无
JS test runner）。本旗 **默认 OFF**（plan v3.1 #4：延长媒体保留 + 新增认证写端点）。
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

_FLAG = "anonymous_preview_claim_enabled"


def _read_page() -> str:
    assert ADMIN_SETTINGS_PAGE.exists(), f"admin settings page 不存在: {ADMIN_SETTINGS_PAGE}"
    return ADMIN_SETTINGS_PAGE.read_text(encoding="utf-8")


def _backend_settings_cls():
    from admin_settings import AdminSettings

    return AdminSettings


# ---------------------------------------------------------------------------
# 1. 后端：字段 + 默认 False + StrictBool
# ---------------------------------------------------------------------------


def test_backend_has_claim_flag_default_false():
    cls = _backend_settings_cls()
    assert _FLAG in cls.model_fields, f"gateway AdminSettings 缺字段 {_FLAG}"
    assert getattr(cls(), _FLAG) is False, f"{_FLAG} 默认必须 False（休眠灰度）"


def test_backend_claim_flag_is_strict_bool():
    """'1'/'on'/'true'/1 不得被宽松解析为 True（admin UI marshalling bug 防误开）。"""
    cls = _backend_settings_cls()
    for bad in ("1", "on", "true", 1):
        with pytest.raises(Exception):
            cls(**{_FLAG: bad})


def test_backend_claim_flag_not_in_bounds_tables():
    """纯 bool 旗不得进任何 *_BOUNDS 数字校验表。"""
    import admin_settings as adm

    assert _FLAG not in getattr(adm, "_APF_LIMIT_BOUNDS", {})


# ---------------------------------------------------------------------------
# 2. 前端 interface + DEFAULT_SETTINGS + toggle + reset
# ---------------------------------------------------------------------------


def test_frontend_interface_contains_claim_flag():
    src = _read_page()
    m = re.search(r"interface\s+AdminSettings\s*\{(?P<body>[\s\S]*?)\n\}", src)
    assert m, "找不到 AdminSettings interface 定义"
    body = m.group("body")
    assert re.search(rf"{_FLAG}\s*:\s*boolean(?=\s|;|$|//|/\*)", body), (
        f"前端 AdminSettings interface 缺 `{_FLAG}: boolean` —— full-body POST 会"
        f"让此字段被后端默认值替换"
    )


def test_frontend_default_matches_backend_false():
    src = _read_page()
    m = re.search(r"DEFAULT_SETTINGS[^=]*=\s*\{(?P<body>[\s\S]*?)\n\}", src)
    assert m, "找不到 DEFAULT_SETTINGS 定义"
    body = m.group("body")
    assert re.search(rf"{_FLAG}\s*:\s*false\s*,", body), (
        f"前端 DEFAULT_SETTINGS 缺 `{_FLAG}: false`"
    )
    assert not re.search(rf"{_FLAG}\s*:\s*true", body), (
        f"DEFAULT_SETTINGS 不应把 {_FLAG} 默认设为 true（休眠上线）"
    )


def test_frontend_toggle_uses_spread():
    src = _read_page()
    pattern = re.compile(
        r"setSettings\s*\(\s*\(s\)\s*=>\s*\(\s*\{\s*\.\.\.s\s*,"
        rf"[^}}]*{_FLAG}"
    )
    assert pattern.search(src), (
        f"{_FLAG} toggle 的 onChange 必须用 "
        f"setSettings((s) => ({{ ...s, {_FLAG}: ... }})) spread 模式"
    )


def test_frontend_reset_restores_claim_default():
    src = _read_page()
    pattern = re.compile(
        rf"{_FLAG}\s*:\s*\n?\s*DEFAULT_SETTINGS\.{_FLAG}"
    )
    assert pattern.search(src), (
        f"reset onClick 缺 `{_FLAG}: DEFAULT_SETTINGS.{_FLAG}`"
    )


# ---------------------------------------------------------------------------
# 3. round-trip 持久化
# ---------------------------------------------------------------------------


def test_claim_flag_round_trip_persist(monkeypatch, tmp_path):
    import admin_settings as adm

    settings_file = tmp_path / "admin_settings.json"
    monkeypatch.setattr(adm, "SETTINGS_FILE", settings_file)

    adm.save_settings(adm.AdminSettings(**{_FLAG: True}))
    persisted = json.loads(settings_file.read_text(encoding="utf-8"))
    assert _FLAG in persisted

    loaded = adm.load_settings()
    assert getattr(loaded, _FLAG) is True
