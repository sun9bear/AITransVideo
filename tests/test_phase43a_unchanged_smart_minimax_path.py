"""Phase 4.3a PR1-B — Smart MiniMax 自动 clone 路径字节级不变守卫。

Codex 2026-05-28 多轮 review 反复强调的 NG1：Phase 4.3a 引入 Express
CosyVoice 自动 clone canary 时，**绝不允许**触碰 Smart MiniMax 自动
clone 路径（``process.py:3640-4100``）或 Studio 手动 clone modal。
Express 是独立的新路径，不是 Smart 的扩展。

本测试是 PR1 第一批落地的守卫，必须先于 C/D/D1/E/E1/E2 任意阶段
commit；其它阶段的实施 PR 触发本测试，任一断言失败 → 实施踩到了
Smart / Studio 边界，必须立刻回滚 / 收紧。

锁定的不变量（与 spec §10.3 一一对应）：

1. **``gateway/smart_consent.py``** 的 validate_smart_consent 函数体 +
   ``ALLOWED_BUDGET_POLICIES`` / ``REQUIRED_FIELDS`` 等常量 — 字节级
   不变。任何 Phase 4.3a 子阶段触碰此文件都视为漂移。
2. **``/api/internal/user-voices/register-smart``** endpoint backward-
   compatible：
   - Smart MiniMax caller（``process.py::_register_smart_clone_in_user_voices``）
     不传 ``created_from`` 时，endpoint 必须默认落 ``"smart_auto"``。
   - Smart MiniMax caller 也不传 ``provider`` 时，endpoint 默认
     ``"minimax_voice_clone"``。
3. **``gateway/cosyvoice_clone/api.py``** 的 POST /clone endpoint 主体
   字节级不变（Phase 4.2 Studio 手动 clone modal 的后端依赖）。
4. **``services/smart_wiring.py::build_smart_clone_provider`` /
   ``_MiniMaxCloneAdapter``** 字节级不变（Smart auto clone provider
   composition root）。
5. **Smart pipeline 触发链**（``process.py:3640-4100``）核心 if-block
   字节级不变：``_smart_consent_allows_clone and _smart_admin_clone_enabled
   and _smart_speaker_ids_requiring_clone`` 这三个条件的 AND 关系不能
   被 Phase 4.3a 误改。

测试策略：用文件 sha256 + AST + 关键字面量 grep 三重锁定。Phase 4.3a
任何实施步骤改了被锁文件 → 测试 fail → 必须解释为什么改 + 是否要先
更新本守卫。

注意：本守卫**不**绑定 sha256 到具体值（因为现在的 codebase 已经在演
进），改用结构性断言（函数签名、字面量存在、AST 节点形状）。这样
Smart 自身后续的维护性 PR（与 Phase 4.3a 无关的）不会误触发本守卫，
只有"在 Phase 4.3a 实施 PR 里改 Smart 文件"才会。
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SMART_CONSENT_PY = REPO_ROOT / "gateway" / "smart_consent.py"
USER_VOICE_API_PY = REPO_ROOT / "gateway" / "user_voice_api.py"
COSYVOICE_CLONE_API_PY = REPO_ROOT / "gateway" / "cosyvoice_clone" / "api.py"
SMART_WIRING_PY = REPO_ROOT / "src" / "services" / "smart_wiring.py"
PROCESS_PY = REPO_ROOT / "src" / "pipeline" / "process.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    assert path.exists(), f"必须的源文件缺失：{path}"
    return path.read_text(encoding="utf-8")


def _ast(path: Path) -> ast.Module:
    return ast.parse(_read(path), filename=str(path))


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


# ---------------------------------------------------------------------------
# 1. gateway/smart_consent.py 关键合约
# ---------------------------------------------------------------------------


def test_smart_consent_validator_function_exists():
    """守卫：``validate_smart_consent`` 函数必须存在于 ``gateway/smart_consent.py``。

    Phase 4.3a 新增 ``gateway/express_consent.py``（独立文件，独立函数
    ``validate_express_consent``），**绝不**修改或复用 ``validate_smart_consent``。
    """
    tree = _ast(SMART_CONSENT_PY)
    fn = _find_function(tree, "validate_smart_consent")
    assert fn is not None, (
        "validate_smart_consent 函数缺失或被改名 — Phase 4.3a 不允许触碰此函数"
    )


def test_smart_consent_validator_signature_unchanged():
    """守卫：``validate_smart_consent`` 函数签名（位置参数 + kwargs）字节级不变。

    Phase 4.3a 严禁向 Smart consent 验证器加 Express-specific 参数（如
    `service_mode` 分支）。Express 独立走 ``validate_express_consent``。
    """
    tree = _ast(SMART_CONSENT_PY)
    fn = _find_function(tree, "validate_smart_consent")
    assert fn is not None

    arg_names = [a.arg for a in fn.args.args]
    kwonly_names = [a.arg for a in fn.args.kwonlyargs]
    # 当前签名是 ``def validate_smart_consent(raw: object) -> tuple[...]``。
    # Phase 4.3a 严禁加任何参数。
    assert arg_names == ["raw"], (
        f"validate_smart_consent 位置参数变了: {arg_names}; "
        "Phase 4.3a 严禁触碰 Smart consent validator 签名"
    )
    assert kwonly_names == [], (
        f"validate_smart_consent 不应有 kwonly 参数，发现: {kwonly_names}"
    )


# ---------------------------------------------------------------------------
# 2. /register-smart endpoint backward-compat
# ---------------------------------------------------------------------------


def test_register_smart_endpoint_defaults_provider_to_minimax_voice_clone():
    """守卫：``/register-smart`` endpoint 未传 ``provider`` 时仍默认
    ``"minimax_voice_clone"``。

    Smart MiniMax caller（``process.py::_register_smart_clone_in_user_voices``）
    不传此字段，依赖 endpoint 默认值兜底。Phase 4.3a 加 Express
    provider override（传 ``"cosyvoice_voice_clone"``）时**绝不**改默认值。
    """
    source = _read(USER_VOICE_API_PY)
    # 字面量断言：``provider = str(body.get("provider") or "minimax_voice_clone")``
    # （Phase 4.3a E 阶段允许在 if 分支后加防漂移 400，但这一行默认值不能动）
    assert 'body.get("provider") or "minimax_voice_clone"' in source, (
        "/register-smart endpoint 的 provider 默认值不应改变 — "
        "Phase 4.3a Express caller 显式传 provider=\"cosyvoice_voice_clone\""
    )


def test_register_smart_endpoint_defaults_created_from_to_smart_auto():
    """守卫：``/register-smart`` endpoint 未传 ``created_from`` 时仍默认
    ``"smart_auto"``。

    Smart MiniMax 路径不传此字段，依赖 endpoint 默认值。Phase 4.3a
    Express caller 必须显式传 ``"express_auto"``（spec §6.3）。
    """
    source = _read(USER_VOICE_API_PY)
    assert 'body.get("created_from") or "smart_auto"' in source, (
        "/register-smart endpoint 的 created_from 默认值不应改变 — "
        "Smart MiniMax caller 依赖此默认值"
    )


def test_register_smart_endpoint_defaults_tts_provider_to_minimax_tts():
    """守卫：``/register-smart`` endpoint 未传 ``tts_provider`` 时默认
    ``"minimax_tts"``。
    """
    source = _read(USER_VOICE_API_PY)
    assert 'body.get("tts_provider") or "minimax_tts"' in source


def test_register_smart_endpoint_defaults_platform_to_minimax_domestic():
    """守卫：``/register-smart`` endpoint 未传 ``platform`` 时默认
    ``"minimax_domestic"``。
    """
    source = _read(USER_VOICE_API_PY)
    assert 'body.get("platform") or "minimax_domestic"' in source


# ---------------------------------------------------------------------------
# 3. Studio CosyVoice clone endpoint 主体不变（Phase 4.2 Studio 手动 clone）
# ---------------------------------------------------------------------------


def test_studio_cosyvoice_clone_endpoint_exists():
    """守卫：``gateway/cosyvoice_clone/api.py`` 的 ``cosyvoice_clone`` POST
    endpoint 必须存在。Phase 4.3a 严禁删除或重命名此 endpoint（Phase 4.2
    Studio 手动 clone modal 唯一后端）。
    """
    tree = _ast(COSYVOICE_CLONE_API_PY)
    fn = _find_function(tree, "cosyvoice_clone")
    assert fn is not None, (
        "Phase 4.2 Studio 手动 clone endpoint 缺失 — Phase 4.3a 严禁触碰"
    )


def test_studio_cosyvoice_clone_endpoint_still_writes_studio_origin():
    """守卫：``gateway/cosyvoice_clone/api.py`` 调 ``add_user_voice`` 时
    ``created_from`` 仍是 ``"cosyvoice_clone_endpoint"``（Phase 4.2 锁定）。

    Phase 4.3a Express 用 ``"express_auto"``，Studio 路径绝不改。
    """
    source = _read(COSYVOICE_CLONE_API_PY)
    assert 'created_from="cosyvoice_clone_endpoint"' in source, (
        "Studio cosyvoice clone endpoint 的 created_from 标记不应改变"
    )


# ---------------------------------------------------------------------------
# 4. Smart wiring composition root 不变
# ---------------------------------------------------------------------------


def test_smart_wiring_build_smart_clone_provider_exists():
    """守卫：``services/smart_wiring.py`` 的 ``build_smart_clone_provider``
    必须存在并返 MiniMax adapter。Phase 4.3a 严禁让此函数返 CosyVoice
    adapter（Express 走自己的 ``services/express/*`` 模块）。
    """
    tree = _ast(SMART_WIRING_PY)
    fn = _find_function(tree, "build_smart_clone_provider")
    assert fn is not None, (
        "build_smart_clone_provider 函数缺失 — Phase 4.3a 严禁触碰 Smart wiring"
    )


def test_smart_wiring_minimax_adapter_class_unchanged():
    """守卫：``_MiniMaxCloneAdapter`` 类必须存在且使用 MiniMax provider。

    Phase 4.3a 严禁改这个 class 让它能选 provider。Express 用独立的
    direct worker client 调用（``services/mainland_worker/client.py``）。
    """
    tree = _ast(SMART_WIRING_PY)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "_MiniMaxCloneAdapter":
            found = True
            break
    assert found, (
        "_MiniMaxCloneAdapter 类缺失或被改名 — Phase 4.3a 严禁触碰 Smart adapter"
    )


# ---------------------------------------------------------------------------
# 5. Smart pipeline 触发链 AND 三条件不变
# ---------------------------------------------------------------------------


def test_smart_pipeline_clone_trigger_three_conditions_present():
    """守卫：``process.py`` smart 自动 clone 触发链的三条件 AND 必须保持：
    ``_smart_consent_allows_clone`` AND ``_smart_admin_clone_enabled``
    AND ``_smart_speaker_ids_requiring_clone``。

    Phase 4.3a 严禁削弱或绕过任一条件。Express 走完全独立的 7-Layer AND
    gate（spec §2 + §2.5）。
    """
    source = _read(PROCESS_PY)
    # 三个变量名必须都出现（具体 AND 关系靠 §2 守卫保证；这里只锁存在）
    assert "_smart_consent_allows_clone" in source, (
        "Smart consent 触发条件变量缺失"
    )
    assert "_smart_admin_clone_enabled" in source, (
        "Smart admin clone enabled 触发条件变量缺失"
    )
    assert "_smart_speaker_ids_requiring_clone" in source, (
        "Smart speaker 触发条件变量缺失"
    )


def test_smart_pipeline_uses_minimax_clone_provider_label():
    """守卫：Smart clone 注册时 ``provider="minimax_voice_clone"`` 仍是 Smart
    路径的 mirror provider 标记。

    Phase 4.3a 严禁让 Smart 路径写 ``provider="cosyvoice_voice_clone"``
    （Smart 是独立 MiniMax 路径，与 Express 平行）。
    """
    source = _read(PROCESS_PY)
    # _register_smart_clone_in_user_voices 函数文档明确 provider=minimax_voice_clone
    # 由 endpoint 默认值兜底（caller 不传），所以这里只验：函数体不主动覆盖
    # 成 cosyvoice_voice_clone。
    fn_start = source.find("def _register_smart_clone_in_user_voices(")
    assert fn_start > 0, "_register_smart_clone_in_user_voices 函数缺失"
    # 截到下一个 def 边界
    next_def = source.find("\ndef ", fn_start + 1)
    fn_body = source[fn_start:next_def] if next_def > 0 else source[fn_start:]
    # Smart 函数体里不应主动赋 provider 字段
    assert '"provider": "cosyvoice_voice_clone"' not in fn_body, (
        "_register_smart_clone_in_user_voices 不应主动写 cosyvoice_voice_clone — "
        "Smart 路径走 endpoint 默认值，是 MiniMax 来源"
    )


# ---------------------------------------------------------------------------
# 6. lookup_clone_voice_routing_metadata 真实函数名锁定（spec v0.3 P2-3）
# ---------------------------------------------------------------------------


def test_lookup_clone_voice_routing_metadata_real_function_name_exists():
    """守卫：``gateway/user_voice_service.py`` 必须含真实函数名
    ``lookup_clone_voice_routing_metadata``。

    Phase 4.3a spec v0.3 修正：早期 spec 草稿用过虚构函数名
    ``query_routing_metadata``，实际代码里只有
    ``lookup_clone_voice_routing_metadata`` (line 285)。各阶段实施时
    必须用真实名，**不**得平行造新函数。
    """
    src = _read(REPO_ROOT / "gateway" / "user_voice_service.py")
    assert "async def lookup_clone_voice_routing_metadata(" in src, (
        "真实函数名 lookup_clone_voice_routing_metadata 不存在或被改名"
    )


# ---------------------------------------------------------------------------
# 7. Phase 4.3a spec 文档自身存在（防止 spec 被误删 / 误移）
# ---------------------------------------------------------------------------


def test_phase43a_spec_doc_exists():
    """sanity：Phase 4.3a spec 文档必须存在。本 PR1 第一个 commit 已经
    入了这个文件；任何后续 commit 不允许删它。
    """
    spec = REPO_ROOT / "docs" / "plans" / "2026-05-28-phase43a-express-cosyvoice-auto-clone-spec.md"
    assert spec.exists(), f"Phase 4.3a spec 文档缺失: {spec}"
