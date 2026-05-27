"""Phase 4.2 D.1 — admin settings 页面静态守卫。

Codex 2026-05-27 review 明确："不要在 D.1 顺手引入 Vitest / RTL / jsdom"。
repo 没有 JS test runner，前端守卫改用 Python 静态扫描（regex / 文本搜索）+
依赖 ``npx tsc --noEmit`` + ``npm run lint`` 跑实际类型检查 / lint。
（``frontend-next/package.json`` **没有** ``typecheck`` 脚本，所以 typecheck
靠直接调 tsc；Codex 2026-05-27 二次复核明确这一点。）

本测试锁死的 5 项 D.1 关键合约：

1. **interface 含字段**：``AdminSettings`` interface 含
   ``cosyvoice_clone_general_availability_enabled: boolean``。
2. **默认 false**：``DEFAULT_SETTINGS`` 含
   ``cosyvoice_clone_general_availability_enabled: false``。
3. **UI 控件文案**：页面存在"CosyVoice 克隆全用户开放"标题（admin 必须看得到 toggle）。
4. **整体保存**：``handleSave`` 用 ``JSON.stringify(settings)`` 整体 body，
   **不**做 partial save / 不只 stringify GA 字段。
5. **onChange spread**：toggle 的 onChange 用 ``...s`` 或 ``...settings`` 保留
   其它字段 —— 防止打开 GA 时无意 reset 其他设置。

为什么用静态扫描而不是 e2e：
- repo 没装 Vitest / RTL / jsdom；引入纯为 D.1 写一个 case 不值。
- 本测试关心"代码里有没有这段文本/结构"，对 React render 行为没要求。
- ``npx tsc --noEmit`` + ``npm run lint`` 会跑 TS 编译，所以 interface 字段
  类型错配会被 CI 拦下来（不需要本测试重复验证类型）。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ADMIN_SETTINGS_PAGE = (
    REPO_ROOT / "frontend-next" / "src" / "app" / "(app)" / "admin"
    / "settings" / "page.tsx"
)


def _read_page() -> str:
    assert ADMIN_SETTINGS_PAGE.exists(), (
        f"admin settings page 不存在: {ADMIN_SETTINGS_PAGE}"
    )
    return ADMIN_SETTINGS_PAGE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. interface 含 cosyvoice_clone_general_availability_enabled: boolean
# ---------------------------------------------------------------------------


def test_admin_settings_interface_contains_clone_ga_field():
    """**D.1 守卫 1**：``AdminSettings`` interface 必须含 GA 字段。

    用 regex 在 interface 块中查找。允许任意空白 + 可选注释（doc comment）。
    """
    src = _read_page()
    # 抓 "interface AdminSettings {" 到下一个 "}" 之间的内容
    m = re.search(
        r"interface\s+AdminSettings\s*\{(?P<body>[\s\S]*?)\n\}",
        src,
        re.MULTILINE,
    )
    assert m, "找不到 AdminSettings interface 定义"
    interface_body = m.group("body")

    # 在 interface 体里找 cosyvoice_clone_general_availability_enabled: boolean
    field_pattern = re.compile(
        r"cosyvoice_clone_general_availability_enabled\s*:\s*boolean",
    )
    assert field_pattern.search(interface_body), (
        "AdminSettings interface 缺 cosyvoice_clone_general_availability_enabled: boolean —— "
        "D.1 admin UI toggle 类型不一致会让 TS 编译 fail。"
    )


# ---------------------------------------------------------------------------
# 2. DEFAULT_SETTINGS 含 cosyvoice_clone_general_availability_enabled: false
# ---------------------------------------------------------------------------


def test_admin_settings_default_settings_contains_ga_false():
    """**D.1 守卫 2**：``DEFAULT_SETTINGS`` 默认 GA = ``false``。

    付费 API 默认 OFF 是 fail-safe，admin 显式打开才生效。如果默认 true，
    任何 reset 按钮 / 初次加载都会让全用户暴露在付费 API 入口下。
    """
    src = _read_page()
    # 抓 "DEFAULT_SETTINGS" 块（const 或 let）
    m = re.search(
        r"DEFAULT_SETTINGS[^=]*=\s*\{(?P<body>[\s\S]*?)\n\}",
        src,
    )
    assert m, "找不到 DEFAULT_SETTINGS 定义"
    default_body = m.group("body")

    field_pattern = re.compile(
        r"cosyvoice_clone_general_availability_enabled\s*:\s*false",
    )
    assert field_pattern.search(default_body), (
        "DEFAULT_SETTINGS 缺 cosyvoice_clone_general_availability_enabled: false —— "
        "付费 API 默认开启是严重 fail-open 风险。"
    )

    # 双重锁：不应该出现 "...enabled: true" 作为默认值
    field_true = re.compile(
        r"cosyvoice_clone_general_availability_enabled\s*:\s*true",
    )
    assert not field_true.search(default_body), (
        "DEFAULT_SETTINGS 不应该把 GA flag 默认设为 true（付费 API 必须显式开启）。"
    )


# ---------------------------------------------------------------------------
# 3. UI 控件存在 + 文案锁死
# ---------------------------------------------------------------------------


def test_admin_settings_ui_has_cosyvoice_clone_ga_label():
    """**D.1 守卫 3**：页面必须能看到 "CosyVoice 克隆全用户开放" 文案。

    没有 UI 控件 = admin 永远没法切 GA = D.1 后端开关 sweep 死代码。
    """
    src = _read_page()
    assert "CosyVoice 克隆全用户开放" in src, (
        "admin settings 页面缺 'CosyVoice 克隆全用户开放' UI 文案 —— "
        "D.1 后端做了 _resolve_clone_gate + admin_setting 字段，但 admin "
        "没法在 UI 上切换它就是死代码。"
    )


def test_admin_settings_ui_has_paid_api_warning():
    """**D.1 守卫 3.1**：toggle 必须有付费 API 警告文案。

    锁死至少出现 "付费 API" + "默认关闭" 字样，让 admin 看到 toggle 时立刻
    知道这是 fail-safe-off 的付费功能开关，不是普通 feature flag。
    """
    src = _read_page()
    assert "付费 API" in src, (
        "admin settings 缺 '付费 API' 警告文案。CosyVoice clone GA toggle 必须"
        "让 admin 看到这是付费 API 入口开关。"
    )
    assert "默认关闭" in src, (
        "admin settings 缺 '默认关闭' 文案。付费 API toggle 必须显式标注"
        "默认行为防误打开。"
    )


# ---------------------------------------------------------------------------
# 4. handleSave 用 JSON.stringify(settings) — 整体保存，不偷工
# ---------------------------------------------------------------------------


def test_admin_settings_save_uses_full_settings_body():
    """**D.1 守卫 4（Codex 明确锁死）**：``handleSave`` 必须 ``JSON.stringify(settings)``，
    不允许 partial save 或仅 stringify GA 字段。

    Codex 原话："Admin settings UI toggle 不能只加 interface，要确认 full-body
    save 不丢字段"。本守卫保证保存请求 body 永远是完整 settings —— 后端
    update_settings 是 full-document replace 语义。
    """
    src = _read_page()
    # 抓 handleSave 函数体
    m = re.search(
        r"const\s+handleSave\s*=\s*async\s*\([^)]*\)\s*=>\s*\{"
        r"(?P<body>[\s\S]*?)\n\s*\}\s*\n",
        src,
    )
    assert m, "找不到 handleSave 函数定义"
    body = m.group("body")
    # 必须用 JSON.stringify(settings) 整体序列化
    assert "JSON.stringify(settings)" in body, (
        "handleSave 必须用 JSON.stringify(settings)（整体 body），"
        "不是 partial save 或单字段 stringify。"
    )
    # 防御性：不允许出现单字段 stringify
    forbidden = re.compile(
        r"JSON\.stringify\(\s*\{\s*cosyvoice_clone_general_availability_enabled"
    )
    assert not forbidden.search(body), (
        "handleSave 不应该单独 stringify GA 字段 —— 必须整体 settings 一次发出。"
    )


# ---------------------------------------------------------------------------
# 5. toggle onChange 用 spread 保留其它字段
# ---------------------------------------------------------------------------


def test_admin_settings_ga_toggle_onchange_uses_spread_to_preserve_settings():
    """**D.1 守卫 5（Codex 锁死的核心）**：GA toggle 的 ``onChange``
    必须用 ``...s`` / ``...settings`` spread 保留其他字段。

    如果写成 ``setSettings({ cosyvoice_clone_general_availability_enabled: ... })``
    会清空所有其他字段 → 任何点击 GA toggle 会把其他设置 reset 成 undefined。
    本守卫是 D.1 最严防御 —— 测试不通过等于潜在大范围 setting wipe。
    """
    src = _read_page()
    # 模式 1（核心）：spread 用法 ``setSettings((s) => ({ ...s, ...enabled: ... }))``
    # 用 [^}]* 不 [^)]* —— 因为 setSettings 调用里有嵌套 ``)`` (如 ``e.target.checked``
    # 后的 ``}))``)，而 spread 段 ``{ ...s, ...enabled: ... }`` 结束于 ``}``。
    pattern_spread = re.compile(
        r"setSettings\s*\(\s*\(s\)\s*=>\s*\(\s*\{\s*\.\.\.s\s*,"
        r"[^}]*cosyvoice_clone_general_availability_enabled"
    )
    assert pattern_spread.search(src), (
        "GA toggle 的 onChange 必须用 setSettings((s) => ({ ...s, "
        "cosyvoice_clone_general_availability_enabled: ... })) 模式 —— "
        "spread 保留其它字段。直接 setSettings({...}) 会清空所有 settings。"
    )

    # 模式 2（防御性）：不允许 ``setSettings({ ...enabled: ... })`` 这种**没有 spread**
    # 的裸字段对象。直接 setSettings({...}) 不带 prev state callback 会清空其它字段。
    # 用更宽松的正则定位"包含 ...enabled 字段但**前面没有 ...s/...settings**"的位置。
    field_name = "cosyvoice_clone_general_availability_enabled"
    # 找该字段在 setSettings 块里的所有出现位置
    for m in re.finditer(re.escape(field_name), src):
        # 向前回溯到最近的 setSettings(，再前的 800 字符窗口里检查 spread
        window_start = max(0, m.start() - 800)
        window = src[window_start: m.start()]
        if "setSettings(" not in window:
            continue  # 出现位置不在 setSettings 内（如 interface / DEFAULT_SETTINGS）
        # 找到 window 中**最后一个** setSettings( 之后的子串
        last_set = window.rfind("setSettings(")
        slice_ = window[last_set:]
        # 该子串里必须含 spread (...s 或 ...settings)
        assert ("...s" in slice_) or ("...settings" in slice_), (
            "setSettings 调用涉及 "
            + field_name
            + " 但缺 spread 保护:\n上下文: ..."
            + repr(slice_[-200:])
            + "...\n必须用 setSettings((s) => ({ ...s, "
            + field_name
            + ": ... })) 保留其它字段。"
        )
