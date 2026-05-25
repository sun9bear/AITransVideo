"""Phase 4.1 F — 跨子树锁死守卫测试集（Codex 2026-05-25 三轮 review 签字）。

F 是补全，不是中心化：D / E 既有 AST 守卫保留原位，本文件聚合 9 项
**跨子树架构 invariant**，覆盖单测试文件无法覆盖的范围。

任何后续 PR 违反任一守卫立刻 red。实施顺序按风险优先：
F.4 → F.9 → F.6 → F.2 → F.8 → F.1 → F.7 → F.3 → F.5

| F.x | 守卫 | Codex 重点 |
|---|---|---|
| F.1 | secret 真实值 regex（sk- / PEM 私钥） | #1 |
| F.2 | gateway/job_intercept.py 不 import 危险 services.tts.* 模块 | #2 |
| F.3 | src/pipeline 整树 + src/services/tts 整树不 import gateway | (D 扩大) |
| F.4 ★ | 所有 _apply_runtime_voice_overrides 调用必须传 speaker_voice_routing | #3 |
| F.5 | _enrich_speakers... 函数体内 new_sp 写入 key ∈ 白名单 | #5 |
| F.6 | env var 名 allowlist (AVT_MAINLAND... + WORKER_HMAC_KEYS) | #1 |
| F.7 | 端到端 serialize：JSON dump 输出无敏感字段名 | #1+5 |
| F.8 | client_factory.py 不 import I/O / HTTP / DB | #1 |
| F.9 ★ | gateway/ 树内 "requires_worker"/"worker_target_model" 文件 allowlist | Codex 新加 |
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = REPO_ROOT / "src"
GATEWAY_PATH = REPO_ROOT / "gateway"
TESTS_PATH = REPO_ROOT / "tests"

for p in (str(SRC_PATH), str(GATEWAY_PATH), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iter_py_files(root: Path, exclude_dirs: set[str] | None = None):
    """递归遍历 root 下所有 .py 文件，跳过 __pycache__ / exclude_dirs。"""
    skip = (exclude_dirs or set()) | {"__pycache__", ".pytest_cache",
                                       ".venv", "venv", "node_modules"}
    for p in root.rglob("*.py"):
        if any(part in skip for part in p.parts):
            continue
        yield p


def _parse_file(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return None


def _string_literals_in_node(node: ast.AST, *, exclude_docstrings: bool = True):
    """Walk node, yield (Constant_node, str_value) pairs, optionally skip
    docstring (first ast.Expr in module/class/function bodies)."""
    docstring_consts: set[int] = set()
    if exclude_docstrings:
        for sub in ast.walk(node):
            if isinstance(sub, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
                if (sub.body
                        and isinstance(sub.body[0], ast.Expr)
                        and isinstance(sub.body[0].value, ast.Constant)
                        and isinstance(sub.body[0].value.value, str)):
                    docstring_consts.add(id(sub.body[0].value))
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if id(sub) in docstring_consts:
                continue
            yield sub, sub.value


def _relative(path: Path) -> str:
    """repo-relative POSIX path."""
    return path.relative_to(REPO_ROOT).as_posix()


# ===========================================================================
# F.4 ★ — All _apply_runtime_voice_overrides call sites must pass
#         speaker_voice_routing= kwarg
# ===========================================================================

def test_f4_all_apply_runtime_voice_overrides_callsites_pass_routing_kwarg():
    """Phase 4.1 F.4 ★ (Codex 重点 #3)：``_apply_runtime_voice_overrides`` 的
    每个 **业务调用** 都必须显式传 ``speaker_voice_routing=`` kwarg，否则
    cloned voice 会漂到 legacy 国际 DashScope endpoint。

    覆盖范围：``src/pipeline/process.py`` 所有 Call 节点。

    白名单：函数自身定义（FunctionDef body 不算调用）。
    """
    target = SRC_PATH / "pipeline" / "process.py"
    tree = _parse_file(target)
    assert tree is not None, f"can't parse {target}"

    missing: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match self._apply_runtime_voice_overrides(...)
        if isinstance(func, ast.Attribute) and func.attr == "_apply_runtime_voice_overrides":
            kw_names = {kw.arg for kw in node.keywords if kw.arg}
            if "speaker_voice_routing" not in kw_names:
                missing.append(
                    f"line {node.lineno}: _apply_runtime_voice_overrides "
                    f"called without speaker_voice_routing= kwarg "
                    f"(keywords seen: {sorted(kw_names)})"
                )

    assert not missing, (
        "F.4 invariant violated — these _apply_runtime_voice_overrides() "
        "call sites do NOT pass speaker_voice_routing=:\n  "
        + "\n  ".join(missing)
        + "\nCosyVoice cloned voice would drift to legacy DashScope endpoint. "
        + "Add `speaker_voice_routing=_speaker_voice_routing or None` to each call."
    )


# ===========================================================================
# F.9 ★ — "requires_worker" / "worker_target_model" string literal in gateway/
#         tree only allowed in 5 files
# ===========================================================================

F9_ROUTING_LITERAL_ALLOWLIST = {
    "gateway/job_intercept.py",                # _enrich_speakers_with_clone_routing
    "gateway/cosyvoice_clone/api.py",          # C.2 writes user_voices
    "gateway/user_voice_service.py",           # lookup + add helpers + ROUTING_METADATA_FIELDS
    "gateway/models.py",                       # ORM column definition
    "gateway/alembic/versions/030_cosyvoice_clone_metadata.py",  # migration
}


def test_f9_routing_string_literals_only_in_allowlisted_gateway_files():
    """Phase 4.1 F.9 ★ (Codex 三轮 P1 新增)：在 ``gateway/`` 子树内，
    字符串 literal ``"requires_worker"`` / ``"worker_target_model"`` 只允许
    出现在 5 个 allowlist 文件。

    防止其它 gateway 路径绕过 E.2 的 ``_enrich_speakers_with_clone_routing``
    strict filter 临时拼装 routing payload。
    """
    forbidden_strings = {"requires_worker", "worker_target_model"}
    violations: list[str] = []

    for py in _iter_py_files(GATEWAY_PATH):
        rel = _relative(py)
        if rel in F9_ROUTING_LITERAL_ALLOWLIST:
            continue
        tree = _parse_file(py)
        if tree is None:
            continue
        for const_node, value in _string_literals_in_node(tree):
            if value in forbidden_strings:
                violations.append(f"{rel}:{const_node.lineno}: literal {value!r}")

    assert not violations, (
        "F.9 invariant violated — routing string literal(s) appear in files "
        "NOT in the allowlist. These fields must only be written by the "
        "approve-time enrichment helper or C.2 clone endpoint:\n  "
        + "\n  ".join(violations)
        + "\nAllowlist:\n  - "
        + "\n  - ".join(sorted(F9_ROUTING_LITERAL_ALLOWLIST))
    )


# ===========================================================================
# F.6 — env var 名 双 allowlist
# ===========================================================================

F6_AVT_WORKER_ENV_VARS = {
    "AVT_MAINLAND_VOICE_WORKER_ENABLED",
    "AVT_MAINLAND_VOICE_WORKER_URL",
    "AVT_MAINLAND_VOICE_WORKER_HMAC_KEY_ID",
    "AVT_MAINLAND_VOICE_WORKER_HMAC_SECRET",
}
F6_AVT_WORKER_ALLOWLIST = {
    "gateway/config.py",
    "gateway/mainland_voice_worker.py",
    "gateway/startup_checks.py",
    "src/services/mainland_worker/client_factory.py",
}

F6_WORKER_APP_ENV_VARS = {
    "WORKER_HMAC_KEYS",
    "WORKER_HMAC_DEPRECATED_KEYS",
}
F6_WORKER_APP_ALLOWLIST = {
    "src/services/mainland_worker/worker/config.py",
    "src/services/mainland_worker/worker/app.py",
}


def _scan_env_var_literals(
    roots: list[Path],
    env_vars: set[str],
    allowlist: set[str],
) -> list[str]:
    violations: list[str] = []
    for root in roots:
        for py in _iter_py_files(root):
            rel = _relative(py)
            # tests/** allowed for both pools
            if rel.startswith("tests/"):
                continue
            if rel in allowlist:
                continue
            tree = _parse_file(py)
            if tree is None:
                continue
            for const_node, value in _string_literals_in_node(tree):
                if value in env_vars:
                    violations.append(f"{rel}:{const_node.lineno}: literal {value!r}")
    return violations


def test_f6_avt_mainland_worker_env_var_only_in_allowlist():
    """Phase 4.1 F.6 (Pool A)：``AVT_MAINLAND_VOICE_WORKER_*`` env var 名
    literal 只允许出现在 4 个业务源文件 + ``tests/**``。"""
    violations = _scan_env_var_literals(
        [GATEWAY_PATH, SRC_PATH],
        F6_AVT_WORKER_ENV_VARS,
        F6_AVT_WORKER_ALLOWLIST,
    )
    assert not violations, (
        "F.6 (Pool A) violated — AVT_MAINLAND_VOICE_WORKER_* env var name "
        "literal in non-allowlisted file:\n  " + "\n  ".join(violations)
        + "\nAllowlist (+ tests/**):\n  - "
        + "\n  - ".join(sorted(F6_AVT_WORKER_ALLOWLIST))
    )


def test_f6_worker_app_env_var_only_in_allowlist():
    """Phase 4.1 F.6 (Pool B)：``WORKER_HMAC_KEYS`` / ``WORKER_HMAC_DEPRECATED_KEYS``
    env var 名 literal 只允许出现在 worker app 2 个文件 + ``tests/**``。"""
    violations = _scan_env_var_literals(
        [GATEWAY_PATH, SRC_PATH],
        F6_WORKER_APP_ENV_VARS,
        F6_WORKER_APP_ALLOWLIST,
    )
    assert not violations, (
        "F.6 (Pool B) violated — WORKER_HMAC_KEYS / DEPRECATED env var name "
        "literal in non-allowlisted file:\n  " + "\n  ".join(violations)
        + "\nAllowlist (+ tests/**):\n  - "
        + "\n  - ".join(sorted(F6_WORKER_APP_ALLOWLIST))
    )


# ===========================================================================
# F.2 — gateway/job_intercept.py 不 import 危险 services.tts.* 模块
# ===========================================================================

F2_FORBIDDEN_IMPORTS = (
    "services.tts.cosyvoice_voice_catalog",
    "services.tts.cosyvoice_provider",
)


def test_f2_job_intercept_no_dangerous_services_tts_imports():
    """Phase 4.1 F.2 (Codex 重点 #2)：``gateway/job_intercept.py`` 整文件
    不得 import 以下危险 services.tts 模块（self-HTTP / 国际 endpoint
    helper）：

      - services.tts.cosyvoice_voice_catalog（self-HTTP 调 Gateway 自己）
      - services.tts.cosyvoice_provider（直接发到国际 DashScope）

    其它 ``services.*`` 子模块允许（不一刀切）。
    """
    target = GATEWAY_PATH / "job_intercept.py"
    tree = _parse_file(target)
    assert tree is not None

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for bad in F2_FORBIDDEN_IMPORTS:
                if mod == bad or mod.startswith(bad + "."):
                    violations.append(f"line {node.lineno}: from {mod} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                for bad in F2_FORBIDDEN_IMPORTS:
                    if alias.name == bad or alias.name.startswith(bad + "."):
                        violations.append(f"line {node.lineno}: import {alias.name}")

    assert not violations, (
        "F.2 violated — gateway/job_intercept.py imports dangerous "
        "services.tts module(s):\n  " + "\n  ".join(violations)
        + "\nUse Gateway-local async DB query instead (Codex E P1 #2)."
    )


# ===========================================================================
# F.8 — src/services/mainland_worker/client_factory.py 不引入 I/O / HTTP / DB
# ===========================================================================

F8_FORBIDDEN_IMPORT_MODULES = (
    "httpx", "requests", "urllib.request", "urllib.error",
    "subprocess",
    "sqlalchemy", "asyncpg",
    # File-system high level (Path.read_*/write_* checked separately as Calls)
)
F8_FORBIDDEN_CALL_PATTERNS = ("read_text", "read_bytes", "write_text", "write_bytes")


def test_f8_client_factory_no_io_or_http_or_db():
    """Phase 4.1 F.8 (Codex 重点 #1)：``src/services/mainland_worker/client_factory.py``
    模块体禁止 import HTTP / subprocess / DB；禁止 ``open()`` / ``Path.read_*``
    / ``Path.write_*`` 调用。**允许** ``from .client import MainlandWorkerClient``
    + ``import os`` + ``import logging``。
    """
    target = SRC_PATH / "services" / "mainland_worker" / "client_factory.py"
    tree = _parse_file(target)
    assert tree is not None

    violations: list[str] = []

    # Import scan
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for bad in F8_FORBIDDEN_IMPORT_MODULES:
                if mod == bad or mod.startswith(bad + "."):
                    violations.append(
                        f"line {node.lineno}: forbidden 'from {mod} import ...'"
                    )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                for bad in F8_FORBIDDEN_IMPORT_MODULES:
                    if alias.name == bad or alias.name.startswith(bad + "."):
                        violations.append(
                            f"line {node.lineno}: forbidden 'import {alias.name}'"
                        )

    # Call scan: open(...), x.read_text(), x.write_bytes() etc.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id == "open":
                violations.append(f"line {node.lineno}: forbidden open(...) call")
            if isinstance(f, ast.Attribute) and f.attr in F8_FORBIDDEN_CALL_PATTERNS:
                violations.append(
                    f"line {node.lineno}: forbidden Path.{f.attr}(...) I/O call"
                )

    assert not violations, (
        "F.8 violated — client_factory.py contains I/O / HTTP / DB usage:\n  "
        + "\n  ".join(violations)
        + "\nFactory must read secrets ONLY from os.environ. Construct "
        + "MainlandWorkerClient via `from .client import MainlandWorkerClient`."
    )


# ===========================================================================
# F.1 — secret 真实值 regex（保守 patterns）
# ===========================================================================

# Codex 三轮建议：限定长度 + 字符集，避免误伤普通文案。
# 只扫两类高风险模式：
F1_SECRET_PATTERNS = [
    # OpenAI / DeepSeek / DashScope 风格 API key（长度 ≥ 20，限定字符）
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    # PEM 格式私钥头部（一旦出现即怀疑泄漏）
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----"),
]


def test_f1_no_high_risk_secret_literal_in_business_code():
    """Phase 4.1 F.1 (Codex 重点 #1, 保守规则)：业务代码不得包含 ``sk-...``
    长 API key literal 或 PEM 私钥 literal。

    保守 patterns 避免误伤普通文案。Env var **名** literal 漂移由 F.6
    allowlist 负责（本测试不扫 env var 名）。

    扫描范围：``gateway/`` + ``src/`` 全部 ``.py``；**跳过** ``tests/`` (允许
    测试 fixture)。
    """
    violations: list[str] = []
    for root in (GATEWAY_PATH, SRC_PATH):
        for py in _iter_py_files(root):
            rel = _relative(py)
            if rel.startswith("tests/"):
                continue
            tree = _parse_file(py)
            if tree is None:
                continue
            for const_node, value in _string_literals_in_node(
                tree, exclude_docstrings=False
            ):
                for pat in F1_SECRET_PATTERNS:
                    if pat.search(value):
                        violations.append(
                            f"{rel}:{const_node.lineno}: matched {pat.pattern!r} "
                            f"in literal {value[:60]!r}..."
                        )

    assert not violations, (
        "F.1 violated — possible secret literal leak in business code:\n  "
        + "\n  ".join(violations)
        + "\nMove secrets to environment variables; reference env name only."
    )


# ---------------------------------------------------------------------------
# F.1b — secret 变量赋值守卫（Codex F 三轮非阻塞建议落地）
# ---------------------------------------------------------------------------

# 变量名含以下任一关键字（大小写不敏感）的赋值，value 不应是 20+ 字符 literal。
# 这种形态高度可疑为明文 secret 泄漏（DashScope key / OpenAI key / HMAC key 等）。
F1B_SECRET_NAME_KEYWORDS = (
    "api_key", "apikey",
    "secret", "secrets",
    "hmac_key", "hmac_secret",
    "access_key", "private_key",
    "token", "auth_token",
    "dashscope_api_key", "openai_api_key",
)

# 名字白名单（避免误伤这些合法变量名）：
# - 函数/方法名（参数 / 局部变量名通常不是 secret 容器）
# - 常用术语如 "voice_clone_metadata_missing" 不含上述关键字，自然过
F1B_NAME_DENYLIST_HINTS = ("_KEYS", "_MAP", "_NAMES", "_LIST", "_FIELDS",
                            "_HINTS", "_CONSTANTS", "_OPTIONS")


def _name_looks_like_secret_var(name: str) -> bool:
    """变量名是否触发 secret 嫌疑（大小写不敏感包含任一关键字）。"""
    lower = name.lower()
    upper = name.upper()
    # 跳过明显的常量集合命名（如 _SECRET_KEYS, _API_KEY_MAP）
    for hint in F1B_NAME_DENYLIST_HINTS:
        if upper.endswith(hint):
            return False
    # 跳过明显指向 env var **名**（非 value）的变量命名约定：
    #   ``api_key_env_var = "DASHSCOPE_API_KEY"``
    #   ``ENV_SECRET = "AVT_MAINLAND_VOICE_WORKER_HMAC_SECRET"``
    #   ``KEY_VAR_NAME = "..."``
    # 以及 prompt template token 命名：
    #   ``TRANSLATION_PROMPT_TEMPLATE_GLOSSARY_TOKEN = "__GLOSSARY__"``
    if (
        lower.endswith("_env_var")
        or lower.endswith("_env_name")
        or lower.endswith("_var_name")
        or lower.endswith("_token")
        or lower.startswith("env_")
    ):
        return False
    return any(kw in lower for kw in F1B_SECRET_NAME_KEYWORDS)


# Env var name 风格 / template token 风格的 literal value 视为非 secret，跳过：
_ENV_VAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")
_TEMPLATE_TOKEN_RE = re.compile(r"^__[A-Z0-9_]+__$")


def _value_looks_like_env_name_or_token(value: str) -> bool:
    """value 是否长得像 env var 名 / template token 占位符（不是实际 secret value）。"""
    return bool(_ENV_VAR_NAME_RE.match(value) or _TEMPLATE_TOKEN_RE.match(value))


def test_f1b_no_long_literal_assigned_to_secret_named_variable():
    """Phase 4.1 F.1b (Codex F 三轮非阻塞建议)：变量名含 ``api_key`` /
    ``secret`` / ``hmac_key`` / ``token`` 等关键字的**赋值**，value 不应是
    ≥ 20 字符的明文 literal。

    检测形态::

        dashscope_api_key = "sk-realkeyvalueexample20chars"   # ★ 违规
        DASHSCOPE_API_KEY = "actualkeyvalue20chars"            # ★ 违规
        hmac_secret = "real_long_secret_string_value"         # ★ 违规

    合法形态（不触发）::

        api_key = os.environ.get("...")                       # value is Call
        api_key = settings.api_key                             # value is Attribute
        api_key = ""                                            # 短 literal
        DASHSCOPE_API_KEY_ENV = "DASHSCOPE_API_KEY"           # env name (但 < 20)

    F.6 已经锁了 env var **名** 的位置；本守卫补**赋值语法**这个角度的纵深防御。
    """
    violations: list[str] = []
    for root in (GATEWAY_PATH, SRC_PATH):
        for py in _iter_py_files(root):
            rel = _relative(py)
            if rel.startswith("tests/"):
                continue
            tree = _parse_file(py)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                # AnnAssign has single target; Assign has list
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if not isinstance(node.value, ast.Constant):
                    continue
                if not isinstance(node.value.value, str):
                    continue
                literal_value = node.value.value
                if len(literal_value) < 20:
                    continue
                # 跳过明显是 env var 名 / template token 占位符的 value
                # （这些不是 secret value）
                if _value_looks_like_env_name_or_token(literal_value):
                    continue
                for tgt in targets:
                    name: str | None = None
                    if isinstance(tgt, ast.Name):
                        name = tgt.id
                    elif isinstance(tgt, ast.Attribute):
                        name = tgt.attr
                    if not name or not _name_looks_like_secret_var(name):
                        continue
                    violations.append(
                        f"{rel}:{node.lineno}: {name} = {literal_value[:30]!r}... "
                        f"(len={len(literal_value)})"
                    )

    assert not violations, (
        "F.1b violated — suspicious long literal assigned to secret-named "
        "variable. Move to env var:\n  " + "\n  ".join(violations)
    )


# ===========================================================================
# F.7 — 端到端 serialize scan
# ===========================================================================

F7_FORBIDDEN_TOKENS = (
    "hmac_secret",
    "AVT_MAINLAND_VOICE_WORKER_HMAC_SECRET",
    "WORKER_HMAC_KEYS",
    "mainland_voice_worker_url",
    "api_key",
    "dashscope_api_key",
    "DASHSCOPE_API_KEY",
)


def test_f7_full_serialization_round_trip_has_no_secret_leak():
    """Phase 4.1 F.7 (Codex 重点 #1+#5)：端到端构造一个 routing-enriched
    DubbingSegment + speakers payload，``json.dumps`` 后扫敏感**字段名**
    不出现。这是 runtime 真实 invariant，比 AST 守卫更直接。
    """
    from dataclasses import asdict
    from services.gemini.translator import DubbingSegment

    seg = DubbingSegment(
        segment_id=1,
        speaker_id="speaker_a",
        display_name="X",
        voice_id="cosyvoice_custom_test",
        start_ms=0,
        end_ms=1000,
        target_duration_ms=1000,
        source_text="hi",
        cn_text="你好",
        tts_provider="cosyvoice",
        requires_worker=True,
        worker_target_model="cosyvoice-v3.5-flash",
    )
    enriched_speakers_payload = [
        {
            "speaker_id": "speaker_a",
            "voice_id": "cosyvoice_custom_test",
            "tts_provider": "cosyvoice",
            "requires_worker": True,
            "worker_target_model": "cosyvoice-v3.5-flash",
        }
    ]

    blob = (
        json.dumps(asdict(seg), ensure_ascii=False)
        + json.dumps(enriched_speakers_payload, ensure_ascii=False)
    )

    leaked: list[str] = []
    for token in F7_FORBIDDEN_TOKENS:
        if token in blob:
            leaked.append(token)
    assert not leaked, (
        "F.7 violated — serialized DubbingSegment / speakers payload leaks "
        f"sensitive token(s) {leaked}. Output excerpt:\n{blob[:500]}"
    )


# ===========================================================================
# F.3 — src/pipeline + src/services/tts 不 import gateway
# ===========================================================================

def test_f3_pipeline_and_tts_modules_do_not_import_gateway():
    """Phase 4.1 F.3：``src/pipeline/`` 整树 + ``src/services/tts/`` 整树
    不允许 import 任何 ``gateway`` 模块。Pipeline 是子进程，没有 Gateway
    命名空间访问权。"""
    violations: list[str] = []
    for root in (SRC_PATH / "pipeline", SRC_PATH / "services" / "tts"):
        if not root.is_dir():
            continue
        for py in _iter_py_files(root):
            rel = _relative(py)
            tree = _parse_file(py)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    if mod == "gateway" or mod.startswith("gateway."):
                        violations.append(
                            f"{rel}:{node.lineno}: 'from {mod} import ...'"
                        )
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "gateway" or alias.name.startswith("gateway."):
                            violations.append(
                                f"{rel}:{node.lineno}: 'import {alias.name}'"
                            )

    assert not violations, (
        "F.3 violated — pipeline / services.tts module imports gateway:\n  "
        + "\n  ".join(violations)
        + "\nPipeline subprocess has no Gateway namespace; use src-side "
        + "equivalents (e.g. client_factory.py for worker secrets)."
    )


# ===========================================================================
# F.5 — _enrich_speakers_with_clone_routing new_sp key 白名单
# ===========================================================================

F5_ROUTING_KEY_WHITELIST = {
    "requires_worker",
    "worker_target_model",
    "tts_provider",
}


def test_f5_enrichment_only_writes_whitelisted_keys_to_speaker():
    """Phase 4.1 F.5 (Codex 重点 #5)：``_enrich_speakers_with_clone_routing``
    函数体内对 enriched speaker dict（``new_sp``）写入的字段 key 必须在
    routing 白名单内。

    防止后续 PR 给 speaker payload 添加 ``label`` / ``billing_sku`` /
    ``clone_provider_request_id`` 等 user_voices 字段（会泄漏给前端 +
    pipeline 可能误用）。
    """
    target = GATEWAY_PATH / "job_intercept.py"
    tree = _parse_file(target)
    assert tree is not None

    func_node: ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.AsyncFunctionDef)
                and node.name == "_enrich_speakers_with_clone_routing"):
            func_node = node
            break
    assert func_node is not None, (
        "F.5: _enrich_speakers_with_clone_routing not found in job_intercept.py"
    )

    violations: list[str] = []
    for sub in ast.walk(func_node):
        # 找 Assign(targets=[Subscript(value=Name('new_sp'), slice=Constant('...'))])
        if isinstance(sub, ast.Assign):
            for tgt in sub.targets:
                if (isinstance(tgt, ast.Subscript)
                        and isinstance(tgt.value, ast.Name)
                        and tgt.value.id == "new_sp"):
                    # Slice may be ast.Constant directly or wrapped
                    slc = tgt.slice
                    key_name: str | None = None
                    if isinstance(slc, ast.Constant) and isinstance(slc.value, str):
                        key_name = slc.value
                    # AugAssign also possible — handled in next loop
                    if key_name is not None and key_name not in F5_ROUTING_KEY_WHITELIST:
                        violations.append(
                            f"line {sub.lineno}: new_sp[{key_name!r}] = ... "
                            f"NOT in whitelist {sorted(F5_ROUTING_KEY_WHITELIST)}"
                        )

    assert not violations, (
        "F.5 violated — _enrich_speakers_with_clone_routing writes keys "
        f"outside the routing whitelist {sorted(F5_ROUTING_KEY_WHITELIST)}:\n  "
        + "\n  ".join(violations)
        + "\nUser-facing approve payload must not leak other user_voices fields."
    )
