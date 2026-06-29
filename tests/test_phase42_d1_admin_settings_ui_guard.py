"""Phase 4.2 D.1 — admin settings 页面静态守卫。

Codex 2026-05-27 review 明确："不要在 D.1 顺手引入 Vitest / RTL / jsdom"。
repo 没有 JS test runner，前端守卫改用 Python 静态扫描（regex / 文本搜索）+
依赖 ``npx tsc --noEmit`` + ``npm run lint`` 跑实际类型检查 / lint。
（``frontend-next/package.json`` **没有** ``typecheck`` 脚本，所以 typecheck
靠直接调 tsc；Codex 2026-05-27 二次复核明确这一点。）

本测试锁死的 7 项 D.1 关键合约：

1. **interface 含全部 6 个 ``cosyvoice_clone_*`` 字段**：``AdminSettings``
   interface 必须包含 worker_enabled / default_target_model / user_allowlist /
   general_availability_enabled / max_voices_per_user / max_concurrent_jobs，
   类型严格匹配后端 Pydantic（PR #13 Codex P1 fix）。
2. **DEFAULT_SETTINGS 与后端默认值严格一致**：6 个字段都必须有默认值且与
   ``gateway/admin_settings.py`` Pydantic 默认值匹配。否则 full-body POST
   会让用户翻 GA toggle 时把其它字段擦掉。
3. **UI 控件文案**：页面存在"CosyVoice 克隆全用户开放"标题（admin 必须看得到 toggle）。
4. **整体保存**：``handleSave`` 用 ``JSON.stringify(settings)`` 整体 body，
   **不**做 partial save / 不只 stringify GA 字段。
5. **onChange spread**：toggle 的 onChange 用 ``...s`` 或 ``...settings`` 保留
   其它字段 —— 防止打开 GA 时无意 reset 其他设置。
6. **付费 API 警告文案**：toggle 必须显式标注"付费 API"+ "默认关闭"。
7. **"恢复默认"按钮的语义分级**（PR #13 Codex P2 二轮 discussion_r3308167865）：
   - **5 个 hidden 字段** 从 current state 透传（``<field>: s.<field>``）：
     worker_enabled / default_target_model / user_allowlist /
     max_voices_per_user / max_concurrent_jobs。
   - **1 个 visible GA toggle**（``cosyvoice_clone_general_availability_enabled``）
     必须**显式回 DEFAULT_SETTINGS 的 false**，**禁止** ``s.*`` 透传 —— 它是
     admin UI 上唯一可见的 clone toggle，"恢复默认"语义就是让它回 fail-safe
     false，也是付费 API 紧急关闭入口。

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
    REPO_ROOT / "frontend-next" / "src" / "app" / "[locale]" / "(app)" / "admin"
    / "settings" / "page.tsx"
)


def _read_page() -> str:
    assert ADMIN_SETTINGS_PAGE.exists(), (
        f"admin settings page 不存在: {ADMIN_SETTINGS_PAGE}"
    )
    return ADMIN_SETTINGS_PAGE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. AdminSettings interface 含全部 6 个 cosyvoice_clone_* 字段
#    （PR #13 Codex P1 fix — 防止 full-body save 擦掉其它字段）
# ---------------------------------------------------------------------------


# 6 个字段的 TypeScript 类型契约（必须和 gateway/admin_settings.py Pydantic 一致）。
# key 是 TS 字段名，value 是匹配类型的 regex pattern。
CLONE_INTERFACE_FIELDS: dict[str, str] = {
    "cosyvoice_clone_worker_enabled": r"boolean",
    "cosyvoice_clone_default_target_model": r"string",
    "cosyvoice_clone_user_allowlist": r"string\[\]",
    "cosyvoice_clone_general_availability_enabled": r"boolean",
    "cosyvoice_clone_max_voices_per_user": r"number",
    "cosyvoice_clone_max_concurrent_jobs": r"number",
}


def test_admin_settings_interface_contains_all_clone_fields():
    """**D.1 守卫 1（PR #13 Codex P1）**：``AdminSettings`` interface 必须含
    全部 6 个 ``cosyvoice_clone_*`` 字段，类型严格匹配后端 Pydantic。

    背景：``POST /api/admin/settings`` 是 full-body replace 语义。如果前端
    interface 只有 GA 一个字段，用户翻 toggle 时其它 5 个字段会被 backend
    用 Pydantic 默认值替换 —— 等于"翻 GA 顺手关 worker_enabled / 擦
    allowlist / reset max_voices_per_user"。Codex 在 PR #13 thread
    https://github.com/sun9bear/AITransVideo/pull/13#discussion_r3308057829
    指出此契约脆弱性。修复方案：interface 必须显式列出全部 6 个字段，
    state 才包含它们，``JSON.stringify(settings)`` body 才不会丢字段。
    """
    src = _read_page()
    m = re.search(
        r"interface\s+AdminSettings\s*\{(?P<body>[\s\S]*?)\n\}",
        src,
        re.MULTILINE,
    )
    assert m, "找不到 AdminSettings interface 定义"
    interface_body = m.group("body")

    for field_name, ts_type_pattern in CLONE_INTERFACE_FIELDS.items():
        # `\b` 在 `]` 后不匹配（``]`` 不是 word 字符），所以用 lookahead
        # 检查类型边界：行尾、空白、分号、注释引导 ``//`` 或 ``/*``。
        field_re = re.compile(
            rf"{re.escape(field_name)}\s*:\s*{ts_type_pattern}"
            r"(?=\s|;|$|//|/\*)",
        )
        assert field_re.search(interface_body), (
            f"AdminSettings interface 缺 `{field_name}: {ts_type_pattern}` —— "
            f"full-body POST 会让此字段被 backend 默认值替换。"
            f"修复参考：PR #13 Codex P1 discussion_r3308057829。"
        )


# ---------------------------------------------------------------------------
# 2. DEFAULT_SETTINGS 含全部 6 个 cosyvoice_clone_* 默认值
#    （PR #13 Codex P1 fix — 与 gateway/admin_settings.py Pydantic 默认值严格一致）
# ---------------------------------------------------------------------------


# 默认值契约（必须和 gateway/admin_settings.py Pydantic 默认值一致）。
# key 是 TS 字段名，value 是匹配该字段默认值的 regex pattern。
# 注意：bool 字段允许 ``true`` / ``false`` 字面量；string 字段允许单引号/双引号；
# array 字段允许 ``[]``；number 字段允许整数。
CLONE_DEFAULT_VALUES: dict[str, str] = {
    "cosyvoice_clone_worker_enabled": r"false",
    "cosyvoice_clone_default_target_model": r"['\"]cosyvoice-v3\.5-flash['\"]",
    "cosyvoice_clone_user_allowlist": r"\[\s*\]",
    "cosyvoice_clone_general_availability_enabled": r"false",
    "cosyvoice_clone_max_voices_per_user": r"3",
    "cosyvoice_clone_max_concurrent_jobs": r"2",
}


def test_admin_settings_default_settings_contains_all_clone_defaults():
    """**D.1 守卫 2（PR #13 Codex P1）**：``DEFAULT_SETTINGS`` 必须含全部 6 个
    ``cosyvoice_clone_*`` 默认值，且与 ``gateway/admin_settings.py`` Pydantic
    默认值严格一致。

    后端 Pydantic 默认值（gateway/admin_settings.py:194-230）：

        cosyvoice_clone_worker_enabled                = False
        cosyvoice_clone_default_target_model          = "cosyvoice-v3.5-flash"
        cosyvoice_clone_user_allowlist                = []
        cosyvoice_clone_general_availability_enabled  = False  (StrictBool)
        cosyvoice_clone_max_voices_per_user           = 3
        cosyvoice_clone_max_concurrent_jobs           = 2

    任何前端 default 与后端 default 不一致都会让 "reset to default" 按钮
    在用户视角下偷偷改了后端配置 —— 是 D.1 PR #13 Codex P1 同源风险。
    """
    src = _read_page()
    m = re.search(
        r"DEFAULT_SETTINGS[^=]*=\s*\{(?P<body>[\s\S]*?)\n\}",
        src,
    )
    assert m, "找不到 DEFAULT_SETTINGS 定义"
    default_body = m.group("body")

    for field_name, value_pattern in CLONE_DEFAULT_VALUES.items():
        field_re = re.compile(
            rf"{re.escape(field_name)}\s*:\s*{value_pattern}\s*,",
        )
        assert field_re.search(default_body), (
            f"DEFAULT_SETTINGS 缺 `{field_name}: <匹配 {value_pattern}>` —— "
            f"前后端默认值不一致会让 reset 按钮 / 初次加载偷偷改配置。"
            f"参考 gateway/admin_settings.py:194-230 + PR #13 Codex P1。"
        )

    # 双重锁：付费 API GA flag **绝对不能** 默认 true（fail-safe-off）
    ga_true = re.compile(
        r"cosyvoice_clone_general_availability_enabled\s*:\s*true",
    )
    assert not ga_true.search(default_body), (
        "DEFAULT_SETTINGS 不应该把 GA flag 默认设为 true（付费 API 必须显式开启）。"
    )
    # 双重锁：worker_enabled 也不能默认 true（runtime 总开关）
    worker_true = re.compile(
        r"cosyvoice_clone_worker_enabled\s*:\s*true",
    )
    assert not worker_true.search(default_body), (
        "DEFAULT_SETTINGS 不应该把 worker_enabled 默认设为 true —— "
        "武汉 worker 可达性 / OSS 配置 / DashScope key 全部就绪才能打开。"
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
        # 该子串里必须含某种 spread —— 三种合法形态：
        #   - ``...s`` / ``...settings``：onChange 透传 prev state
        #   - ``...DEFAULT_SETTINGS``：reset 按钮 spread defaults，再透传 hidden
        #     clone state（reset 场景的合法 spread，由守卫 7 单独锁更细的合约）
        has_state_spread = ("...s" in slice_) or ("...settings" in slice_)
        has_default_spread = "...DEFAULT_SETTINGS" in slice_
        assert has_state_spread or has_default_spread, (
            "setSettings 调用涉及 "
            + field_name
            + " 但缺 spread 保护（没有 ...s / ...settings / ...DEFAULT_SETTINGS）:"
            + "\n上下文: ..."
            + repr(slice_[-200:])
            + "...\n必须用 setSettings((s) => ({ ...s, "
            + field_name
            + ": ... })) 或 setSettings((s) => ({ ...DEFAULT_SETTINGS, "
            + field_name
            + ": s." + field_name + " })) 保留其它字段。"
        )


# ---------------------------------------------------------------------------
# 7. "恢复默认" 按钮的语义分级：5 hidden 字段透传 + 1 visible GA 强制回 false
#    （PR #13 Codex P2 二轮复审 — discussion_r3308167865，2026-05-27）
# ---------------------------------------------------------------------------


# 5 个 **hidden** 字段：admin UI 不渲染 → "恢复默认"必须从 current state 透传
# （不被 reset 覆盖），否则 reset+save 会静默关克隆 worker / 擦 allowlist。
CLONE_RESET_PRESERVE_FIELDS: frozenset[str] = frozenset({
    "cosyvoice_clone_worker_enabled",
    "cosyvoice_clone_default_target_model",
    "cosyvoice_clone_user_allowlist",
    "cosyvoice_clone_max_voices_per_user",
    "cosyvoice_clone_max_concurrent_jobs",
})

# 1 个 **visible** 字段：D.1 admin UI 上有 toggle → "恢复默认"必须回 fail-safe
# 默认（== false），不允许从 current state 透传 —— 否则点 reset 时 admin 看到
# toggle 没变化、也无法用 reset 紧急关闭误打开的 GA。
CLONE_RESET_FORCE_DEFAULT_FIELDS: frozenset[str] = frozenset({
    "cosyvoice_clone_general_availability_enabled",
})

# 两个集合应**互不相交**且**并集等于全部 6 个 clone 字段**（与 interface 一致）。
assert CLONE_RESET_PRESERVE_FIELDS.isdisjoint(CLONE_RESET_FORCE_DEFAULT_FIELDS)
assert (
    CLONE_RESET_PRESERVE_FIELDS | CLONE_RESET_FORCE_DEFAULT_FIELDS
    == set(CLONE_INTERFACE_FIELDS.keys())
)


def test_admin_settings_reset_button_preserves_hidden_and_resets_visible():
    """**D.1 守卫 7（PR #13 Codex P2 二轮 — discussion_r3308167865）**：
    ``恢复默认`` 按钮必须按字段可见性分级：

    - **5 个 hidden 字段** 从 current state 透传（``<field>: s.<field>``）：
      worker_enabled / default_target_model / user_allowlist /
      max_voices_per_user / max_concurrent_jobs。理由：admin 在 UI 上看不到，
      reset 不应该意外重置它们。
    - **1 个 visible 字段**（``cosyvoice_clone_general_availability_enabled``）
      必须**显式回 DEFAULT_SETTINGS 的 false**，**禁止**从 s.* 透传。理由：
      它是 D.1 admin UI 上唯一渲染的 toggle，"恢复默认"语义就是让它回 false；
      同时这也是付费 API 紧急关闭的入口，必须 fail-safe。

    Codex 二轮原话："Don't keep the visible GA toggle on reset; admin should
    see it return to false. Preserving s.cosyvoice_clone_general_availability_enabled
    means reset-button has no visible effect on GA, and admin can't use it
    to emergency-close an accidentally-flipped GA."

    Codex thread:
    https://github.com/sun9bear/AITransVideo/pull/13#discussion_r3308167865
    """
    src = _read_page()

    # 1. 找 "恢复默认" 按钮的 JSX text node。**用 rfind 找最后一次出现** ——
    # 因为 onClick 代码上方的解释注释里也写了 "恢复默认"，src.find 会匹配
    # 注释里的而不是按钮文案。按钮文案在 JSX 树**最后**出现，所以 rfind 可靠。
    # 窗口取按钮文案前 6000 字符（包含 onClick 长注释 + 6 个字段透传代码）。
    btn_idx = src.rfind("恢复默认")
    assert btn_idx >= 0, "找不到 '恢复默认' 按钮"
    onclick_window_raw = src[max(0, btn_idx - 6000): btn_idx]

    # 1b. 剥掉 ``//`` 行注释 —— 守卫只看实际代码，不被注释里的反例文本干扰。
    onclick_window = re.sub(r"//[^\n]*", "", onclick_window_raw)

    # 2. 禁止裸调用 setSettings(DEFAULT_SETTINGS) —— 必须用 callback + spread
    forbidden_pattern = re.compile(
        r"setSettings\s*\(\s*DEFAULT_SETTINGS\s*\)",
    )
    assert not forbidden_pattern.search(onclick_window), (
        "'恢复默认' 按钮直接 setSettings(DEFAULT_SETTINGS) 会覆盖 hidden clone "
        "fields —— 必须用 setSettings((s) => ({ ...DEFAULT_SETTINGS, ... })) "
        "callback + spread。详见 PR #13 Codex P2 discussion_r3308123420。"
    )

    # 3. 必须含 callback form: setSettings((s) => ({ ...DEFAULT_SETTINGS, ... }))
    spread_pattern = re.compile(
        r"setSettings\s*\(\s*\(s\)\s*=>\s*\(\s*\{\s*\.\.\.DEFAULT_SETTINGS\s*,",
    )
    assert spread_pattern.search(onclick_window), (
        "'恢复默认' 按钮必须用 setSettings((s) => ({ ...DEFAULT_SETTINGS, ... })) "
        "callback + spread 模式，否则没法透传 hidden clone fields。"
    )

    # 4. 5 个 hidden 字段必须有 ``<field>: s.<field>`` 透传
    for field_name in CLONE_RESET_PRESERVE_FIELDS:
        preserve_pattern = re.compile(
            rf"{re.escape(field_name)}\s*:\s*s\.{re.escape(field_name)}",
        )
        assert preserve_pattern.search(onclick_window), (
            f"'恢复默认' 按钮 onClick 缺 `{field_name}: s.{field_name}` "
            f"透传。漏掉这 5 个 hidden 字段中任一会让 reset+save 把后端配置覆盖。"
            f"详见 PR #13 Codex P2 discussion_r3308123420。"
        )

    # 5. visible GA 字段**禁止**从 s.* 透传（必须从 DEFAULT_SETTINGS 拿默认 false）
    for field_name in CLONE_RESET_FORCE_DEFAULT_FIELDS:
        s_passthrough_pattern = re.compile(
            rf"{re.escape(field_name)}\s*:\s*s\.{re.escape(field_name)}",
        )
        assert not s_passthrough_pattern.search(onclick_window), (
            f"'恢复默认' 按钮 onClick **不应该**有 `{field_name}: s.{field_name}` —— "
            f"该字段是 admin UI 上的 visible toggle，恢复默认必须让它回 fail-safe "
            f"false，不能从 current state 透传。详见 PR #13 Codex P2 二轮 "
            f"discussion_r3308167865。"
        )
        # 必须显式从 DEFAULT_SETTINGS 取值（接受 ``DEFAULT_SETTINGS.<field>`` 形式
        # 或不在 onClick 中出现该字段名 —— 后者意味着走 ...DEFAULT_SETTINGS 兜底）。
        from_default_pattern = re.compile(
            rf"{re.escape(field_name)}\s*:\s*DEFAULT_SETTINGS\.{re.escape(field_name)}",
        )
        in_onclick = re.search(re.escape(field_name), onclick_window) is not None
        if in_onclick:
            # 出现了就必须用 DEFAULT_SETTINGS.* 形式
            assert from_default_pattern.search(onclick_window), (
                f"'恢复默认' 按钮 onClick 出现了 `{field_name}` 但既不是 spread "
                f"兜底也不是 `DEFAULT_SETTINGS.{field_name}` —— 必须显式回默认。"
            )
        # 如果完全不出现，``...DEFAULT_SETTINGS`` spread 就会用 DEFAULT 默认值；
        # 这是合法的"隐式"形态，不强求显式 ``DEFAULT_SETTINGS.<field>``。
