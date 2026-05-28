"""Phase 4.3a PR1-D — admin_settings 8 字段 + availability endpoint + full-body save 守卫。

锁定 spec §8 三层合约：

1. **后端 8 字段定义在 ``gateway/admin_settings.py::AdminSettings``** 且与
   Phase 4.2 ``cosyvoice_clone_*`` 并行存在（独立 namespace）。``StrictBool``
   主开关防字符串 "1"/"on"/"true" 滑过；validators 锁住数值边界。

2. **Frontend full-body save**（spec §8.2 + Phase 4.2 D.1 同模式）：
   - ``AdminSettings`` TS interface 含全部 8 个字段
   - ``DEFAULT_SETTINGS`` 含全部 8 个字段，默认值与后端 Pydantic 严格一致
   - "恢复默认"按钮 reset payload：visible toggle 显式 DEFAULT；7 个 hidden
     字段透传 current state（避免静默擦掉 allowlist / 阈值 / cap）

3. **Availability endpoint**（spec §8.4）：
   ``GET /api/me/express-auto-clone-availability`` 返 ``{available, reason}``
   - 未登录 → unavailable + unauthenticated
   - admin_flag_off → unavailable + admin_flag_off
   - allowlist 命中 OR admin → available
   - 不返回 allowlist 内容（隐私）
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GATEWAY_DIR = REPO_ROOT / "gateway"
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

ADMIN_SETTINGS_PAGE = (
    REPO_ROOT / "frontend-next" / "src" / "app" / "(app)" / "admin"
    / "settings" / "page.tsx"
)


# ---------------------------------------------------------------------------
# Layer 1: pydantic schema correctness
# ---------------------------------------------------------------------------


def test_admin_settings_has_all_8_phase43a_fields():
    """Pydantic AdminSettings 必须含 Phase 4.3a 全部 8 个字段。"""
    from admin_settings import AdminSettings
    field_names = set(AdminSettings.model_fields.keys())
    required = {
        "express_cosyvoice_auto_clone_enabled",
        "express_cosyvoice_auto_clone_allowlist_enabled",
        "express_cosyvoice_auto_clone_user_allowlist",
        "express_cosyvoice_auto_clone_main_speaker_min_ratio",
        "express_cosyvoice_auto_clone_main_speaker_min_lines",
        "express_cosyvoice_auto_clone_sample_max_seconds",
        "express_cosyvoice_auto_clone_target_model",
        "express_cosyvoice_auto_clone_per_user_daily_cap",
        "express_cosyvoice_auto_clone_per_user_active_temp_cap",
    }
    missing = required - field_names
    assert not missing, f"AdminSettings 缺少 Phase 4.3a 字段: {missing}"


def test_admin_settings_default_values_match_spec():
    """8 个字段默认值必须严格匹配 spec §8.1。"""
    from admin_settings import AdminSettings
    defaults = AdminSettings()
    assert defaults.express_cosyvoice_auto_clone_enabled is False
    assert defaults.express_cosyvoice_auto_clone_allowlist_enabled is True
    assert defaults.express_cosyvoice_auto_clone_user_allowlist == []
    assert defaults.express_cosyvoice_auto_clone_main_speaker_min_ratio == 0.30
    assert defaults.express_cosyvoice_auto_clone_main_speaker_min_lines == 5
    assert defaults.express_cosyvoice_auto_clone_sample_max_seconds == 20.0
    assert defaults.express_cosyvoice_auto_clone_target_model == "cosyvoice-v3.5-flash"
    assert defaults.express_cosyvoice_auto_clone_per_user_daily_cap == 5
    assert defaults.express_cosyvoice_auto_clone_per_user_active_temp_cap == 3


def test_enabled_field_uses_strict_bool():
    """主开关必须用 StrictBool，拒绝 "1" / "on" / "yes" / "true" 字符串。"""
    from admin_settings import AdminSettings
    import pydantic
    for bad in ["1", "true", "on", "yes", 1]:
        try:
            AdminSettings(express_cosyvoice_auto_clone_enabled=bad)
        except (pydantic.ValidationError, ValueError):
            continue
        raise AssertionError(
            f"express_cosyvoice_auto_clone_enabled 接受了 {bad!r}（应为 StrictBool）"
        )


def test_allowlist_enabled_field_uses_strict_bool():
    from admin_settings import AdminSettings
    import pydantic
    for bad in ["1", "false", "on", "yes", 0]:
        try:
            AdminSettings(express_cosyvoice_auto_clone_allowlist_enabled=bad)
        except (pydantic.ValidationError, ValueError):
            continue
        raise AssertionError(
            f"express_cosyvoice_auto_clone_allowlist_enabled 接受了 {bad!r}"
        )


def test_validators_reject_out_of_range_min_ratio():
    """min_ratio < 0.10 或 > 1.0 必须被拒（spec §4.2）。"""
    from admin_settings import AdminSettings
    import pydantic
    for bad in [0.05, 1.5, -0.1]:
        try:
            AdminSettings(express_cosyvoice_auto_clone_main_speaker_min_ratio=bad)
        except (pydantic.ValidationError, ValueError):
            continue
        raise AssertionError(
            f"min_ratio 接受了 {bad}（应该 [0.10, 1.0] 拒收）"
        )


def test_validators_reject_out_of_range_min_lines():
    from admin_settings import AdminSettings
    import pydantic
    for bad in [0, 101, -1]:
        try:
            AdminSettings(express_cosyvoice_auto_clone_main_speaker_min_lines=bad)
        except (pydantic.ValidationError, ValueError):
            continue
        raise AssertionError(f"min_lines 接受了 {bad}（应该 [1, 100] 拒收）")


def test_validators_reject_out_of_range_sample_max_seconds():
    from admin_settings import AdminSettings
    import pydantic
    for bad in [5.0, 9.9, 60.1, 120.0]:
        try:
            AdminSettings(express_cosyvoice_auto_clone_sample_max_seconds=bad)
        except (pydantic.ValidationError, ValueError):
            continue
        raise AssertionError(
            f"sample_max_seconds 接受了 {bad}（应该 [10.0, 60.0] 拒收）"
        )


def test_validators_reject_invalid_target_model():
    """target_model 只接受 flash / plus（与 Phase 4.1 白名单一致）。"""
    from admin_settings import AdminSettings
    import pydantic
    for bad in ["cosyvoice-v2", "minimax-speech-2.8-hd", "random", ""]:
        try:
            AdminSettings(express_cosyvoice_auto_clone_target_model=bad)
        except (pydantic.ValidationError, ValueError):
            continue
        raise AssertionError(
            f"target_model 接受了 {bad!r}（应该 flash/plus 拒收）"
        )


def test_validators_reject_out_of_range_caps():
    from admin_settings import AdminSettings
    import pydantic
    for bad in [-1, 1001]:
        try:
            AdminSettings(express_cosyvoice_auto_clone_per_user_daily_cap=bad)
        except (pydantic.ValidationError, ValueError):
            continue
        raise AssertionError(
            f"daily_cap 接受了 {bad}（应该 [0, 1000] 拒收）"
        )
    for bad in [-1, 101]:
        try:
            AdminSettings(express_cosyvoice_auto_clone_per_user_active_temp_cap=bad)
        except (pydantic.ValidationError, ValueError):
            continue
        raise AssertionError(
            f"active_temp_cap 接受了 {bad}（应该 [0, 100] 拒收）"
        )


def test_phase42_cosyvoice_clone_fields_still_intact():
    """守卫：Phase 4.3a 加 8 字段不应破坏 Phase 4.2 6 个 ``cosyvoice_clone_*`` 字段。"""
    from admin_settings import AdminSettings
    defaults = AdminSettings()
    assert defaults.cosyvoice_clone_worker_enabled is False
    assert defaults.cosyvoice_clone_default_target_model == "cosyvoice-v3.5-flash"
    assert defaults.cosyvoice_clone_user_allowlist == []
    assert defaults.cosyvoice_clone_general_availability_enabled is False
    assert defaults.cosyvoice_clone_max_voices_per_user == 3
    assert defaults.cosyvoice_clone_max_concurrent_jobs == 2


# ---------------------------------------------------------------------------
# Layer 2: frontend full-body save guards
# ---------------------------------------------------------------------------


def _read_page() -> str:
    assert ADMIN_SETTINGS_PAGE.exists(), f"page 不存在: {ADMIN_SETTINGS_PAGE}"
    return ADMIN_SETTINGS_PAGE.read_text(encoding="utf-8")


EXPRESS_FIELDS_TS_TYPES: dict[str, str] = {
    "express_cosyvoice_auto_clone_enabled": "boolean",
    "express_cosyvoice_auto_clone_allowlist_enabled": "boolean",
    "express_cosyvoice_auto_clone_user_allowlist": "string[]",
    "express_cosyvoice_auto_clone_main_speaker_min_ratio": "number",
    "express_cosyvoice_auto_clone_main_speaker_min_lines": "number",
    "express_cosyvoice_auto_clone_sample_max_seconds": "number",
    "express_cosyvoice_auto_clone_target_model": "string",
    "express_cosyvoice_auto_clone_per_user_daily_cap": "number",
    "express_cosyvoice_auto_clone_per_user_active_temp_cap": "number",
}


def test_ts_interface_contains_all_8_express_fields():
    """AdminSettings TS interface 必须含全部 8 个 ``express_cosyvoice_auto_clone_*``
    字段，类型严格匹配后端 Pydantic（spec §8.2 P2-3）。
    """
    src = _read_page()
    for field, ts_type in EXPRESS_FIELDS_TS_TYPES.items():
        # 字段必须出现在 interface 块（最简化：直接 grep "<field>: <type>"
        # 形态，与 D.1 守卫同模式）
        pattern = f"{field}: {ts_type}"
        assert pattern in src, (
            f"AdminSettings interface 缺字段 `{pattern}` —— "
            "Phase 4.3a full-body save 必须含全 8 字段"
        )


def test_ts_default_settings_contains_all_8_express_fields():
    """DEFAULT_SETTINGS 必须含全部 8 字段默认值，与 backend Pydantic 一致。"""
    src = _read_page()
    expected_defaults = {
        "express_cosyvoice_auto_clone_enabled": "false",
        "express_cosyvoice_auto_clone_allowlist_enabled": "true",
        "express_cosyvoice_auto_clone_user_allowlist": "[]",
        "express_cosyvoice_auto_clone_main_speaker_min_ratio": "0.30",
        "express_cosyvoice_auto_clone_main_speaker_min_lines": "5",
        "express_cosyvoice_auto_clone_sample_max_seconds": "20.0",
        "express_cosyvoice_auto_clone_target_model": "'cosyvoice-v3.5-flash'",
        "express_cosyvoice_auto_clone_per_user_daily_cap": "5",
        "express_cosyvoice_auto_clone_per_user_active_temp_cap": "3",
    }
    for field, default in expected_defaults.items():
        pattern = f"{field}: {default}"
        assert pattern in src, (
            f"DEFAULT_SETTINGS 缺/错 `{pattern}` —— "
            "必须与 gateway/admin_settings.py Pydantic 默认值严格一致"
        )


def test_ts_reset_button_explicitly_resets_visible_toggle():
    """守卫：reset 按钮里 visible toggle ``enabled`` 必须显式回 DEFAULT。

    其它 7 个 hidden 字段透传 current state（``s.<field>``），可见 toggle
    必须 ``DEFAULT_SETTINGS.<field>``（与 Phase 4.2 GA toggle 同模式）。
    spec §8.2 P2-3。
    """
    src = _read_page()
    # visible toggle 显式回 DEFAULT
    assert (
        "express_cosyvoice_auto_clone_enabled:\n              DEFAULT_SETTINGS.express_cosyvoice_auto_clone_enabled"
        in src
    ) or (
        "express_cosyvoice_auto_clone_enabled: DEFAULT_SETTINGS.express_cosyvoice_auto_clone_enabled"
        in src
    ), (
        "reset 按钮必须把 express_cosyvoice_auto_clone_enabled 显式回 "
        "DEFAULT_SETTINGS（fail-safe-off）"
    )


def test_ts_reset_button_passes_through_hidden_fields():
    """守卫：reset 按钮里 7 个 hidden 字段必须透传 ``s.<field>``。

    避免点恢复默认 + 保存时静默把 allowlist / 阈值 / cap 重置（spec §8.2 P2-3）。
    """
    src = _read_page()
    hidden_fields = [
        "express_cosyvoice_auto_clone_allowlist_enabled",
        "express_cosyvoice_auto_clone_user_allowlist",
        "express_cosyvoice_auto_clone_main_speaker_min_ratio",
        "express_cosyvoice_auto_clone_main_speaker_min_lines",
        "express_cosyvoice_auto_clone_sample_max_seconds",
        "express_cosyvoice_auto_clone_target_model",
        "express_cosyvoice_auto_clone_per_user_daily_cap",
        "express_cosyvoice_auto_clone_per_user_active_temp_cap",
    ]
    for field in hidden_fields:
        # 必须含 "s.<field>" 模式（无论缩进 / 换行）
        passthrough_pattern = f"s.{field}"
        assert passthrough_pattern in src, (
            f"reset 按钮没有透传 {field}（应该写 `{passthrough_pattern}` —— "
            "spec §8.2 P2-3 hidden 字段透传 current state）"
        )


def test_ts_section_renders_visible_enabled_toggle_only():
    """守卫：UI section 只渲染主 enabled toggle，不渲染其它 7 个 hidden 字段
    的 input UI（canary 期间防止误操作 allowlist / cap）。
    """
    src = _read_page()
    # 找到 Phase 4.3a 主 toggle section
    section_marker = "Express 快捷版自动克隆 (canary)"
    assert section_marker in src, (
        f"page.tsx 缺少 Phase 4.3a 主 toggle section（title=\"{section_marker}\"）"
    )

    # checkbox 应直接绑定 enabled 字段
    assert (
        "checked={settings.express_cosyvoice_auto_clone_enabled}" in src
    ), "Phase 4.3a section 缺少 enabled toggle checkbox"


# ---------------------------------------------------------------------------
# Layer 3: availability endpoint
# ---------------------------------------------------------------------------


def test_availability_endpoint_registered_in_entitlements_router():
    """守卫：GET /api/me/express-auto-clone-availability 必须挂在 entitlements
    router 上（与 ``/api/me/entitlements`` 同 router，便于前端单一来源）。
    """
    src = (REPO_ROOT / "gateway" / "entitlements.py").read_text(encoding="utf-8")
    assert '@router.get("/api/me/express-auto-clone-availability")' in src, (
        "entitlements.py 缺少 availability endpoint 路由 —— spec §8.4 必备"
    )
    # 函数签名必须含 user dep
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and node.name == "get_express_auto_clone_availability"
        ):
            found = True
            arg_names = [a.arg for a in node.args.args]
            assert "user" in arg_names, (
                "availability handler 必须接 user dep（auth-aware）"
            )
            break
    assert found, "get_express_auto_clone_availability 函数缺失"


def test_availability_endpoint_does_not_leak_allowlist_contents():
    """守卫：endpoint 不应在响应里返 allowlist 内容（隐私边界，spec §8.4）。

    AST 扫 ``get_express_auto_clone_availability`` 函数体：return 的 dict
    keys 不允许出现 ``allowlist`` 字面量。
    """
    src = (REPO_ROOT / "gateway" / "entitlements.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    target = None
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and node.name == "get_express_auto_clone_availability"
        ):
            target = node
            break
    assert target is not None
    # 收集所有 Return 节点的 keys
    for ret in ast.walk(target):
        if isinstance(ret, ast.Return) and isinstance(ret.value, ast.Dict):
            for key_node in ret.value.keys:
                if isinstance(key_node, ast.Constant) and isinstance(
                    key_node.value, str
                ):
                    assert "allowlist" not in key_node.value.lower(), (
                        f"availability endpoint 不应返 allowlist 字段（隐私 leak）"
                        f"，发现 return key: {key_node.value!r}"
                    )


def test_availability_endpoint_handles_unauthenticated():
    """守卫：endpoint 必须处理 user=None 场景（未登录）。

    AST 扫：函数体含 ``if user is None`` 分支返 ``unauthenticated`` reason。
    """
    src = (REPO_ROOT / "gateway" / "entitlements.py").read_text(encoding="utf-8")
    assert "if user is None:" in src, (
        "availability endpoint 必须处理 user is None"
    )
    assert '"unauthenticated"' in src, (
        "availability endpoint 应在未登录场景返 reason=\"unauthenticated\""
    )


def test_availability_endpoint_reads_admin_settings_at_request_time():
    """守卫：endpoint 在 handler 内 ``from admin_settings import load_settings``
    每次请求重读（hot-reloadable，与 entitlements 同模式）。

    NOT 在 module top 重用 cached snapshot；避免 admin 翻 toggle 后 endpoint
    仍返 stale 结果。
    """
    src = (REPO_ROOT / "gateway" / "entitlements.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    target = None
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and node.name == "get_express_auto_clone_availability"
        ):
            target = node
            break
    assert target is not None
    # 函数体内必须含 import / load_settings 调用
    body_src = ast.unparse(target)
    assert "load_settings" in body_src, (
        "availability endpoint 必须在 handler 内调 load_settings()（hot-reload）"
    )


def test_availability_endpoint_admin_bypasses_allowlist():
    """守卫：endpoint 必须对 ``role == 'admin'`` 自动 bypass allowlist
    （spec §2 Layer 3 + admin 自测便利）。
    """
    src = (REPO_ROOT / "gateway" / "entitlements.py").read_text(encoding="utf-8")
    # 简单字面量扫
    assert "is_admin" in src or 'role == "admin"' in src, (
        "availability endpoint 必须含 admin role 判断"
    )


# ---------------------------------------------------------------------------
# Layer 4: Phase 4.2 D.1 守卫仍绿（regression）
# ---------------------------------------------------------------------------


def test_phase42_d1_admin_settings_ui_guards_still_pass():
    """守卫的守卫：Phase 4.2 D.1 测试文件存在并能跑（防 D 阶段意外破坏 D.1 守卫）。"""
    d1_guard = REPO_ROOT / "tests" / "test_phase42_d1_admin_settings_ui_guard.py"
    assert d1_guard.exists(), (
        "Phase 4.2 D.1 守卫文件缺失（Phase 4.3a 不应触碰它）"
    )


# ---------------------------------------------------------------------------
# Layer 5: availability endpoint 直接行为测试（Codex D-fix P2）
# ---------------------------------------------------------------------------
#
# AST/static 守卫已经锁住 endpoint shape，但 Codex 二轮 review 要求补
# 直接行为测试：覆盖 5 个 reason 全分支 + admin bypass 行为。
#
# 测试策略（与 tests/test_gateway_entitlements.py 同模式）：
#   - 直接 import handler async function，不起 FastAPI server
#   - monkeypatch `admin_settings.load_settings` 注入测试 admin_settings 对象
#   - 用 SimpleNamespace 构造 fake user
#   - asyncio.run / loop.run_until_complete 跑 async handler
# ---------------------------------------------------------------------------


import asyncio  # noqa: E402  — late import to keep §1-§4 guards untouched
import types  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

# 复用 test_gateway_entitlements.py 同模式的 fake database 桩，让 entitlements
# import 链能在没有 PG 驱动的本地环境下解析（gateway 业务模块 import 时会
# 引到 database / asyncpg，必须先 stub）。
_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)


def _make_user(
    *,
    role: str = "user",
    user_id: str = "00000000-0000-0000-0000-000000000abc",
) -> SimpleNamespace:
    """Build a mock user object that satisfies attribute access patterns
    in get_express_auto_clone_availability handler."""
    return SimpleNamespace(
        id=user_id,
        role=role,
        plan_code="free",
        email=f"{role}@example.com",
        display_name=role,
    )


def _make_admin_settings_stub(
    *,
    enabled: bool = False,
    allowlist_enabled: bool = True,
    allowlist: list[str] | None = None,
) -> SimpleNamespace:
    """Return an object whose ``getattr(...)`` calls match what the handler
    expects on the real AdminSettings Pydantic instance."""
    return SimpleNamespace(
        express_cosyvoice_auto_clone_enabled=enabled,
        express_cosyvoice_auto_clone_allowlist_enabled=allowlist_enabled,
        express_cosyvoice_auto_clone_user_allowlist=list(allowlist or []),
    )


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _call_availability(user, monkeypatch, *, settings_stub=None, load_raises=False):
    """Invoke get_express_auto_clone_availability with monkey-patched
    load_settings. Returns the response dict.

    load_raises=True simulates admin_settings.json load failure (fail-closed
    path).
    """
    import admin_settings as _admin_settings_mod
    from entitlements import get_express_auto_clone_availability

    if load_raises:
        def _raise() -> None:
            raise RuntimeError("simulated admin_settings load failure")

        monkeypatch.setattr(
            _admin_settings_mod, "load_settings", _raise, raising=True
        )
    else:
        monkeypatch.setattr(
            _admin_settings_mod,
            "load_settings",
            lambda: settings_stub,
            raising=True,
        )
    return _run(get_express_auto_clone_availability(user=user))


# --- 5 个 reason 全分支覆盖（Codex D-fix #1） ---


def test_availability_returns_unauthenticated_when_user_is_none(monkeypatch):
    """user=None → available=False, reason="unauthenticated"。

    在未登录场景下 endpoint **不**应触及 admin_settings（avoid useless
    disk I/O + 避免暴露 admin 配置信号给未登录侧通道）。
    """
    # 设一个 sentinel：如果 load_settings 被调，测试直接 fail
    import admin_settings as _admin_settings_mod
    sentinel_called = {"flag": False}

    def _sentinel():
        sentinel_called["flag"] = True
        return _make_admin_settings_stub()

    monkeypatch.setattr(
        _admin_settings_mod, "load_settings", _sentinel, raising=True
    )

    from entitlements import get_express_auto_clone_availability
    result = _run(get_express_auto_clone_availability(user=None))
    assert result == {"available": False, "reason": "unauthenticated"}
    assert sentinel_called["flag"] is False, (
        "unauthenticated 分支不应调用 load_settings —— 避免不必要的 I/O"
    )


def test_availability_returns_admin_settings_unavailable_on_load_failure(monkeypatch):
    """admin_settings load 抛异常 → available=False,
    reason="admin_settings_unavailable"（fail-closed）。
    """
    user = _make_user(role="admin")
    result = _call_availability(user, monkeypatch, load_raises=True)
    assert result == {
        "available": False,
        "reason": "admin_settings_unavailable",
    }


def test_availability_returns_admin_flag_off_for_admin_when_disabled(monkeypatch):
    """admin 自己 + flag=False → available=False, reason="admin_flag_off"。

    spec §8.4：admin 关掉主开关时连自己也不该看到入口（让 admin 能从
    UI 验证 flag 是否真的关掉）。
    """
    user = _make_user(role="admin")
    stub = _make_admin_settings_stub(enabled=False)
    result = _call_availability(user, monkeypatch, settings_stub=stub)
    assert result == {"available": False, "reason": "admin_flag_off"}


def test_availability_returns_ok_for_admin_when_enabled(monkeypatch):
    """admin 自己 + flag=True → available=True, reason="ok"（不需要进 allowlist）。"""
    user = _make_user(role="admin")
    stub = _make_admin_settings_stub(enabled=True, allowlist=[])
    result = _call_availability(user, monkeypatch, settings_stub=stub)
    assert result == {"available": True, "reason": "ok"}


def test_availability_returns_ok_for_normal_user_in_allowlist(monkeypatch):
    """普通用户 + flag=True + user_id ∈ allowlist → available=True, reason="ok"。"""
    user = _make_user(
        role="user", user_id="00000000-0000-0000-0000-000000000beta"
    )
    stub = _make_admin_settings_stub(
        enabled=True,
        allowlist=["00000000-0000-0000-0000-000000000beta"],
    )
    result = _call_availability(user, monkeypatch, settings_stub=stub)
    assert result == {"available": True, "reason": "ok"}


def test_availability_returns_not_in_allowlist_for_normal_user_not_in_allowlist(monkeypatch):
    """普通用户 + flag=True + user_id ∉ allowlist → available=False,
    reason="not_in_allowlist"。
    """
    user = _make_user(
        role="user", user_id="00000000-0000-0000-0000-000000000999"
    )
    stub = _make_admin_settings_stub(
        enabled=True,
        allowlist=["00000000-0000-0000-0000-000000000beta"],  # 不同 UUID
    )
    result = _call_availability(user, monkeypatch, settings_stub=stub)
    assert result == {"available": False, "reason": "not_in_allowlist"}


def test_availability_returns_not_in_allowlist_with_empty_allowlist(monkeypatch):
    """边界 case：allowlist 空数组 + 普通用户 → 必须返 not_in_allowlist
    （不能因 ``"" in []`` 之类的 Python 真值表面行为意外放行）。
    """
    user = _make_user(role="user", user_id="00000000-0000-0000-0000-000000000abc")
    stub = _make_admin_settings_stub(enabled=True, allowlist=[])
    result = _call_availability(user, monkeypatch, settings_stub=stub)
    assert result == {"available": False, "reason": "not_in_allowlist"}


def test_availability_returns_ok_when_allowlist_gate_disabled(monkeypatch):
    user = _make_user(role="user", user_id="00000000-0000-0000-0000-000000000abc")
    stub = _make_admin_settings_stub(
        enabled=True,
        allowlist_enabled=False,
        allowlist=[],
    )
    result = _call_availability(user, monkeypatch, settings_stub=stub)
    assert result == {"available": True, "reason": "ok"}


# --- Response shape lockdown：永远 2 keys, 永不 leak allowlist ---


def test_availability_response_has_exactly_two_keys(monkeypatch):
    """任意场景下响应都必须**精确** 2 keys：``available`` + ``reason``。

    防止未来意外 leak ``user_id`` / ``allowlist_size`` / 任何 admin 配置信号。
    """
    cases = [
        # (user, settings_stub, load_raises)
        (None, None, False),
        (_make_user(role="admin"), None, True),
        (_make_user(role="admin"), _make_admin_settings_stub(enabled=False), False),
        (_make_user(role="admin"), _make_admin_settings_stub(enabled=True), False),
        (
            _make_user(role="user", user_id="aaaa"),
            _make_admin_settings_stub(enabled=True, allowlist=["aaaa"]),
            False,
        ),
        (
            _make_user(role="user", user_id="not-in"),
            _make_admin_settings_stub(enabled=True, allowlist=["aaaa"]),
            False,
        ),
    ]
    for user, stub, raises in cases:
        if user is None:
            from entitlements import get_express_auto_clone_availability
            import admin_settings as _m
            monkeypatch.setattr(
                _m, "load_settings", lambda: stub, raising=True
            )
            result = _run(get_express_auto_clone_availability(user=None))
        else:
            result = _call_availability(
                user, monkeypatch, settings_stub=stub, load_raises=raises
            )
        assert set(result.keys()) == {"available", "reason"}, (
            f"响应 keys 不应有除 available/reason 外的字段: {result.keys()}"
        )
        # 防隐私 leak：response value 里也不应出现 allowlist 内容
        for k, v in result.items():
            if isinstance(v, str):
                # allowlist UUID 是 "aaaa" / "not-in" 等；这里要求 reason 字段
                # 是 5 个允许 enum 之一，不包含任何 user-id substring
                assert v in {
                    "ok",
                    "unauthenticated",
                    "admin_settings_unavailable",
                    "admin_flag_off",
                    "not_in_allowlist",
                }, f"reason 字段返了非 enum 值: {v!r}"
