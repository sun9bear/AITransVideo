"""Phase 1 mainland worker 契约级守卫测试。

每条守卫都对应方案文档 / CLAUDE.md / AGENTS.md 的一条硬约束。任何
违反都会让 CI 立刻红。

守卫清单：

1. **mainland_worker 包不 import dashscope** — Phase 1 必须 mock-first。
   AST 扫整个 ``src/services/mainland_worker/`` 找 ``import dashscope`` /
   ``from dashscope ...``。Phase 4 真实 provider 出现时这条守卫要更新
   到"只允许 ``providers/real_cosyvoice`` 单文件 import"。

2. **mainland_worker 不反向依赖主 pipeline** — worker 是独立部署组件，
   不应该 import ``services.jobs``、``services.gemini``、``services.tts``。
   AST 扫整个包。

3. **client / worker 路径无无上限 retry** — AGENTS.md "tests, local
   development, and default paths, prefer mocks/stubs/fakes" + CLAUDE.md
   付费 API 硬约束。AST 扫 ``while True:`` 后跟 HTTP / retry 调用模式；
   保守起见，只扫禁止字面量 ``while True``。

4. **single-segment regenerate 复用 /synthesize-batch** — plan §Studio
   Post-Edit / Regenerate TTS 明确"不开 /synthesize-one"。AST 扫 worker
   端 routes，不应该有名为 ``/synthesize-one``、``/synthesize_segment``
   等替代 endpoint。

5. **mock provider 不调外部网络** — AST 扫
   ``providers/mock_cosyvoice.py`` 不 import ``httpx`` / ``requests`` /
   ``urllib``。

6. **client 路径无自动 fallback 到其他 provider** — plan §Failure Handling
   "no automatic fallback to MiniMax clone without user confirmation"。
   AST 扫 ``client.py`` 不出现 ``minimax`` / ``volcengine`` / ``doubao``
   字面量。

7. **审计 sanitize 字段集合与 plan §审计日志 同步** — 防止悄悄漂移。

8. **clone 必须 consent 显式 True** — mock provider consent_required
   抛错（行为测试已经覆盖，这里再加一条 AST 守卫：``MockCosyvoiceProvider.clone``
   必须包含 ``voice_clone_confirmed`` 检查）。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKER_PKG_ROOT = REPO_ROOT / "src" / "services" / "mainland_worker"


def _iter_python_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _collect_imports(tree: ast.Module) -> set[str]:
    """收集 ``import X`` / ``from X.y import z`` 中的顶层 module 名。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
                names.add(node.module)  # full path 也加入，给后续守卫用
    return names


# ---------------------------------------------------------------------------
# Guard 1: 不 import dashscope
# ---------------------------------------------------------------------------

def test_no_dashscope_import_in_mainland_worker_package() -> None:
    """整包只允许 ``providers/real_cosyvoice.py`` 单文件 import dashscope。

    Phase 1 mock-first 时所有文件都禁止 import dashscope。Phase 2 落地后
    放开一个文件：``services/mainland_worker/worker/providers/real_cosyvoice.py``
    —— 这是真实 DashScope provider 唯一允许的 SDK 引用点。

    任何其它文件 import dashscope（哪怕只是 ``import dashscope``）都立刻红，
    确保 mock 模式启动路径不依赖 SDK、确保未来重构不会偷偷扩散 SDK 引用面。
    """
    # 允许的单一例外文件（相对 REPO_ROOT）
    ALLOWED_DASHSCOPE_IMPORTERS = {
        "src/services/mainland_worker/worker/providers/real_cosyvoice.py".replace("/", "\\"),
        "src/services/mainland_worker/worker/providers/real_cosyvoice.py",
    }

    offenders: list[str] = []
    for path in _iter_python_files(WORKER_PKG_ROOT):
        rel = str(path.relative_to(REPO_ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = _collect_imports(tree)
        if any(mod.startswith("dashscope") for mod in imports):
            # 标准化路径分隔符（Windows / Linux）
            rel_norm = rel.replace("\\", "/")
            allowed_norm = {p.replace("\\", "/") for p in ALLOWED_DASHSCOPE_IMPORTERS}
            if rel_norm in allowed_norm:
                continue
            offenders.append(rel)
    assert not offenders, (
        f"mainland_worker 包内除 real_cosyvoice.py 之外不允许 import dashscope，但发现:\n"
        + "\n".join(f"  - {p}" for p in offenders)
    )


# ---------------------------------------------------------------------------
# Guard 2: 不反向依赖主 pipeline
# ---------------------------------------------------------------------------

_FORBIDDEN_REVERSE_DEPS = {
    "services.jobs",
    "services.gemini",
    "services.tts",
    "services.alignment",
    "services.assemblyai",
    "services.audio",
    "services.smart",
    "services.r2_publisher_lib",
}


def test_mainland_worker_does_not_import_main_pipeline_modules() -> None:
    """worker 是独立部署组件，不能依赖主 pipeline。

    允许 import ``services.mainland_worker.*`` 内部模块；其他 ``services.*``
    全部拒绝。
    """
    offenders: list[tuple[str, str]] = []
    for path in _iter_python_files(WORKER_PKG_ROOT):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module
                for forbidden in _FORBIDDEN_REVERSE_DEPS:
                    if module == forbidden or module.startswith(forbidden + "."):
                        offenders.append((str(path.relative_to(REPO_ROOT)), module))
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for forbidden in _FORBIDDEN_REVERSE_DEPS:
                        if alias.name == forbidden or alias.name.startswith(forbidden + "."):
                            offenders.append(
                                (str(path.relative_to(REPO_ROOT)), alias.name),
                            )
    assert not offenders, (
        f"mainland_worker 包反向依赖了主 pipeline:\n"
        + "\n".join(f"  - {f}: imports {m}" for f, m in offenders)
    )


# ---------------------------------------------------------------------------
# Guard 3: 不出现无上限 retry 模式
# ---------------------------------------------------------------------------

def test_no_unbounded_while_true_in_worker_package() -> None:
    """CLAUDE.md：batch / loop / retry 里无上限调用付费 API 禁止。

    AST 扫 ``while True:`` —— 这是无限循环最常见的字面量；保守阻断。
    如果未来需要 ``while True``（例如事件循环），加注释 ``# pragma:
    worker-allow-infinite-loop`` 后再单独允许（这里不实现，避免开口子）。
    """
    offenders: list[str] = []
    for path in _iter_python_files(WORKER_PKG_ROOT):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.While) and isinstance(node.test, ast.Constant) and node.test.value is True:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not offenders, (
        f"mainland_worker 包含 ``while True``（无上限循环）:\n"
        + "\n".join(f"  - {p}" for p in offenders)
    )


# ---------------------------------------------------------------------------
# Guard 4: single-segment 不分叉到 /synthesize-one
# ---------------------------------------------------------------------------

_FORBIDDEN_ROUTE_PATHS = {
    "/synthesize-one",
    "/synthesize_segment",
    "/cosyvoice/synthesize-one",
    "/cosyvoice/synthesize_one",
}


def test_no_synthesize_one_route_in_worker_app() -> None:
    """plan §Studio Post-Edit 明确："不新增 /synthesize-one，避免两套
    重试和审计路径"。

    AST-level：只看 ``@app.get/post/delete/put("...")`` 的字符串字面量，
    不看 docstring / 注释（plan 引用文本会包含这个 path，但那是说明
    不是 route 定义）。
    """
    app_py = WORKER_PKG_ROOT / "worker" / "app.py"
    tree = ast.parse(app_py.read_text(encoding="utf-8"))

    route_paths: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for deco in node.decorator_list:
            # @app.get("/path") / @app.post("/path") / ...
            if not isinstance(deco, ast.Call):
                continue
            func = deco.func
            method_name = ""
            if isinstance(func, ast.Attribute):
                method_name = func.attr
            elif isinstance(func, ast.Name):
                method_name = func.id
            if method_name not in {"get", "post", "delete", "put", "patch"}:
                continue
            if not deco.args:
                continue
            first = deco.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                route_paths.append(first.value)

    leaked = [p for p in route_paths if p in _FORBIDDEN_ROUTE_PATHS]
    assert not leaked, (
        f"worker app.py 暴露了禁止的 route {leaked}；"
        f"single-segment regenerate 必须复用 /cosyvoice/synthesize-batch。"
        f"全部路由: {route_paths}"
    )


# ---------------------------------------------------------------------------
# Guard 5: mock provider 不调外部网络
# ---------------------------------------------------------------------------

_FORBIDDEN_NETWORK_MODULES = {"httpx", "requests", "urllib", "aiohttp", "socket"}


def test_mock_provider_no_network_imports() -> None:
    mock_path = WORKER_PKG_ROOT / "worker" / "providers" / "mock_cosyvoice.py"
    tree = ast.parse(mock_path.read_text(encoding="utf-8"))
    imports = _collect_imports(tree)
    leaked = imports & _FORBIDDEN_NETWORK_MODULES
    assert not leaked, (
        f"mock_cosyvoice.py import 了网络模块 {leaked}；mock 不应访问公网"
    )


# ---------------------------------------------------------------------------
# Guard 6: client 不出现自动 fallback 到其他 provider 的痕迹
# ---------------------------------------------------------------------------

_FORBIDDEN_PROVIDER_NAMES = {"minimax", "MiniMax", "volcengine", "VolcEngine", "doubao", "Doubao"}


def test_client_has_no_other_provider_references() -> None:
    """plan §Failure Handling："no automatic fallback to MiniMax clone
    without user confirmation"。

    client.py 是 worker 路径的唯一入口；如果出现其他 provider 名字，
    就有 fallback 风险。这条守卫只保护 client，worker 内部的 provider
    协议可以提及其他厂商作为注释。
    """
    client_py = WORKER_PKG_ROOT / "client.py"
    source = client_py.read_text(encoding="utf-8")
    leaked = [name for name in _FORBIDDEN_PROVIDER_NAMES if name in source]
    assert not leaked, (
        f"client.py 出现了其他 provider 名字 {leaked}；"
        f"client 必须 worker-only，不允许 fallback 到其他付费 provider"
    )


# ---------------------------------------------------------------------------
# Guard 7: audit 字段集合与 plan §审计日志 同步
# ---------------------------------------------------------------------------

_PLAN_AUDIT_FIELDS = {
    "event_id",
    "request_id",
    "job_id",
    "user_id",
    "speaker_id",
    "segment_id",          # Phase 4.0b §A：synthesize_segment 路径按段定位
    "voice_id",
    "operation",
    "provider",
    "target_model",
    "provider_request_id",
    "status",
    "duration_ms",
    "billed_chars",
    "audio_seconds",
    "artifact_bytes",
    "error_code",
    "created_at",
}


def test_audit_sanitize_fields_match_plan() -> None:
    """plan §审计日志 字段列表与 ``audit._AUDIT_FIELDS`` 保持一致。

    任何一方加 / 删字段，都必须同步另一方，否则这条守卫红。
    """
    from services.mainland_worker.worker.audit import _AUDIT_FIELDS
    missing_in_code = _PLAN_AUDIT_FIELDS - _AUDIT_FIELDS
    extra_in_code = _AUDIT_FIELDS - _PLAN_AUDIT_FIELDS
    assert not missing_in_code, (
        f"plan §审计日志 列了字段 {missing_in_code} 但 audit._AUDIT_FIELDS 缺失"
    )
    assert not extra_in_code, (
        f"audit._AUDIT_FIELDS 包含 plan §审计日志 没列的字段 {extra_in_code}；"
        f"要么加进 plan、要么删掉代码"
    )


def test_audit_forbidden_fields_cover_secrets_and_audio() -> None:
    """audit 必须主动拒绝 raw audio / secret 字段（plan §审计日志）。"""
    from services.mainland_worker.worker.audit import _FORBIDDEN_FIELDS
    required = {"raw_audio", "audio_bytes", "api_key", "hmac_secret"}
    missing = required - _FORBIDDEN_FIELDS
    assert not missing, (
        f"audit._FORBIDDEN_FIELDS 至少要覆盖 {required}，但缺少 {missing}"
    )


# ---------------------------------------------------------------------------
# Guard 8: clone 必须显式 consent
# ---------------------------------------------------------------------------

def test_mock_clone_requires_explicit_consent() -> None:
    """plan §Clone Flow："用户选择样本片段并显式确认。"

    Mock provider 必须拒绝 consent.voice_clone_confirmed=False 的请求。
    AST 扫 mock_cosyvoice.py 的 ``clone`` 方法 body 含 ``voice_clone_confirmed``
    引用。
    """
    mock_path = WORKER_PKG_ROOT / "worker" / "providers" / "mock_cosyvoice.py"
    tree = ast.parse(mock_path.read_text(encoding="utf-8"))

    clone_methods: list[ast.FunctionDef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "MockCosyvoiceProvider":
            for member in node.body:
                if isinstance(member, ast.FunctionDef) and member.name == "clone":
                    clone_methods.append(member)
    assert clone_methods, "MockCosyvoiceProvider.clone 找不到"

    clone_src = ast.unparse(clone_methods[0])
    assert "voice_clone_confirmed" in clone_src, (
        "MockCosyvoiceProvider.clone 必须检查 consent.voice_clone_confirmed；"
        "plan §Clone Flow 要求 clone 前用户显式确认"
    )


# ---------------------------------------------------------------------------
# Guard 9: retry 函数有 max_attempts 参数
# ---------------------------------------------------------------------------

def test_client_send_request_has_max_attempts_parameter() -> None:
    """client 的内部发送函数必须显式接受 ``max_attempts``。

    这条守卫防止有人未来重构时把 retry 上限默默移走。
    """
    client_path = WORKER_PKG_ROOT / "client.py"
    tree = ast.parse(client_path.read_text(encoding="utf-8"))

    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_send_request":
            arg_names = {a.arg for a in node.args.kwonlyargs} | {a.arg for a in node.args.args}
            assert "max_attempts" in arg_names, (
                f"_send_request 必须接 max_attempts，当前签名: {arg_names}"
            )
            # body 中必须有 ValueError 校验 max_attempts >= 1
            unparsed = ast.unparse(node)
            assert "max_attempts" in unparsed and "must be >= 1" in unparsed, (
                "_send_request 缺少 max_attempts >= 1 的运行时校验；"
                "没有校验 retry 0 / 负值时容易跑出未预期路径"
            )
            found = True
            break
    assert found, "client._send_request 函数未找到"


def test_clone_uses_max_attempts_one() -> None:
    """plan §Retry/Clone：'每次用户确认最多 1 次 provider call'。

    AST 检查 ``MainlandWorkerClient.clone`` 方法体里 ``max_attempts=1``
    出现且 ``max_attempts=self._max_network_retries`` 不出现。
    """
    client_path = WORKER_PKG_ROOT / "client.py"
    tree = ast.parse(client_path.read_text(encoding="utf-8"))

    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "MainlandWorkerClient":
            for member in node.body:
                if isinstance(member, ast.FunctionDef) and member.name == "clone":
                    src = ast.unparse(member)
                    assert "max_attempts=1" in src, (
                        "MainlandWorkerClient.clone 必须用 max_attempts=1，"
                        "plan §Retry/Clone 规定每次用户确认最多 1 次。"
                        "当前方法体：\n" + src
                    )
                    found = True
    assert found, "MainlandWorkerClient.clone 未找到"


def test_synthesize_batch_does_not_use_raw_max_network_retries() -> None:
    """plan §Retry：synthesize_batch 多段时上限必须是 2，不能直接用
    ``self._max_network_retries``（=3，会让多段批量重提 2 次）。

    Codex 2026-05-24 P1 finding：旧实现 ``max_attempts=self._max_network_retries``
    把多段 5xx 跑 3 次。修复后必须根据 ``len(segments)`` 区分；这条守卫
    防止有人未来"简化"回去。
    """
    client_path = WORKER_PKG_ROOT / "client.py"
    tree = ast.parse(client_path.read_text(encoding="utf-8"))

    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "MainlandWorkerClient":
            for member in node.body:
                if isinstance(member, ast.FunctionDef) and member.name == "synthesize_batch":
                    src = ast.unparse(member)
                    # 必须出现 segment-数量分流
                    assert "SINGLE_SEGMENT_MAX_ATTEMPTS" in src, (
                        "synthesize_batch 必须引用 SINGLE_SEGMENT_MAX_ATTEMPTS。"
                        "当前方法体：\n" + src
                    )
                    assert "MULTI_SEGMENT_MAX_ATTEMPTS" in src, (
                        "synthesize_batch 必须引用 MULTI_SEGMENT_MAX_ATTEMPTS。"
                        "当前方法体：\n" + src
                    )
                    # 不允许直接用 self._max_network_retries 作为 max_attempts
                    assert "max_attempts=self._max_network_retries" not in src, (
                        "synthesize_batch 不允许把 max_attempts 直接绑到 "
                        "self._max_network_retries —— 多段 batch 重提次数会越过 plan §Retry 上限。"
                        "当前方法体：\n" + src
                    )
                    found = True
    assert found, "MainlandWorkerClient.synthesize_batch 未找到"


def test_retry_constants_locked_to_plan_values() -> None:
    """``SINGLE_SEGMENT_MAX_ATTEMPTS=3`` 和 ``MULTI_SEGMENT_MAX_ATTEMPTS=2``
    是 plan §Retry 明文规定，常量值漂移必须先改 plan。"""
    from services.mainland_worker.client import (
        MULTI_SEGMENT_MAX_ATTEMPTS,
        SINGLE_SEGMENT_MAX_ATTEMPTS,
    )
    assert SINGLE_SEGMENT_MAX_ATTEMPTS == 3
    assert MULTI_SEGMENT_MAX_ATTEMPTS == 2
