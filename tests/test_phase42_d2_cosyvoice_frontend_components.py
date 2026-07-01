"""Phase 4.2 D.2 — CosyVoice frontend components 静态守卫。

Per Codex 2026-05-27 决策：repo 没有 JS test runner（Vitest / RTL / jsdom
均未装），D.2 测试**严格只用 Python 静态扫描** + 项目已有的 ``npx tsc
--noEmit`` + ``npm run lint``。本测试**不**引入任何 JS 测试栈依赖。

锁死的 D.2 合约（10 项）：

1. 必备文件存在：API client / ConsentModal / CloneModal
2. ``CONSENT_MODAL_VERSION`` 三处常量字面量一致（gateway + 前端 API client +
   ConsentModal 用 import）—— 防止悄悄改一边而漏改另一边
3. ``COSYVOICE_TARGET_MODELS`` 集合与后端 ``ALLOWED_TARGET_MODELS`` 严格一致
4. ConsentModal 必须含 3 个可勾选项（``CHECKBOX_IDS`` 长度 == 3）
5. ConsentModal 禁止在内部直接调用 ``submitCosyvoiceClone`` —— 必须由父组件
   接收 ``onConfirm`` 后再发起 paid API
6. CloneModal 在用户**显式**点击之前**不调用** ``submitCosyvoiceClone``：
   不能出现 ``useEffect`` 自动触发的 paid API 调用
7. API client 端点 path 锁死为 ``/api/voice/cosyvoice/...``，禁止任何
   ``/api/admin/...`` 路径混入
8. 不引入 ``vitest`` / ``@testing-library`` / ``jsdom`` / ``happy-dom``
   到 ``frontend-next/package.json``
9. 失败不自动重试：API client + CloneModal 都不允许出现自动 retry 循环
   （regex 检查 ``setTimeout.*submitCosyvoiceClone`` / 重试关键字）
10. ``sample`` / ``source_segments`` 互斥已在 API client 写明（防止
    浪费多 MB 的 multipart upload 才发现 mismatch）
"""
from __future__ import annotations

import re
from pathlib import Path

def _strip_comments(src: str) -> str:
    """剥掉 TS/TSX 的 ``//``  行注释 + ``/* ... */`` 块注释。

    守卫只看实际代码逻辑，不被解释注释 / docstring 里的反例文本干扰。
    使用 DOTALL flag 让 ``.`` 匹配换行，正则按出现顺序非贪婪匹配。
    """
    # Block comments first (greedy across lines).
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    # Then line comments.
    src = re.sub(r"//[^\n]*", "", src)
    return src


REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = REPO_ROOT / "frontend-next"
API_CLIENT = FRONTEND_DIR / "src" / "lib" / "api" / "cosyvoiceClone.ts"
CONSENT_MODAL = (
    FRONTEND_DIR / "src" / "components" / "voice-clone" / "CosyVoiceConsentModal.tsx"
)
CLONE_MODAL = (
    FRONTEND_DIR / "src" / "components" / "voice-clone" / "CosyVoiceCloneModal.tsx"
)
PACKAGE_JSON = FRONTEND_DIR / "package.json"

# Backend constants used as the single source of truth.
GATEWAY_CLONE_API = REPO_ROOT / "gateway" / "cosyvoice_clone" / "api.py"

# ---------------------------------------------------------------------------
# 1. Required files exist
# ---------------------------------------------------------------------------


def test_d2_required_files_exist():
    """**D.2 守卫 1**：D.2 必备的 3 个文件存在。"""
    missing = [p for p in (API_CLIENT, CONSENT_MODAL, CLONE_MODAL) if not p.exists()]
    assert not missing, (
        "D.2 缺以下必备文件:\n"
        + "\n".join(str(p) for p in missing)
        + "\n详见 docs/plans/2026-05-24-cosyvoice-phase4-go-live-plan.md §Phase 4.2"
    )


# ---------------------------------------------------------------------------
# 2. CONSENT_MODAL_VERSION must match across gateway / API client / ConsentModal
# ---------------------------------------------------------------------------


def _extract_consent_modal_version_from_gateway() -> str:
    """从 gateway/cosyvoice_clone/api.py 提取 ``CONSENT_MODAL_VERSION`` 常量。"""
    src = GATEWAY_CLONE_API.read_text(encoding="utf-8")
    m = re.search(
        r"^CONSENT_MODAL_VERSION\s*=\s*['\"]([^'\"]+)['\"]",
        src,
        re.MULTILINE,
    )
    assert m, "找不到 gateway/cosyvoice_clone/api.py 的 CONSENT_MODAL_VERSION 常量"
    return m.group(1)


def test_d2_consent_modal_version_locked_across_layers():
    """**D.2 守卫 2（关键）**：``CONSENT_MODAL_VERSION`` 字面量必须三处一致：

    1. ``gateway/cosyvoice_clone/api.py::CONSENT_MODAL_VERSION``
    2. ``frontend-next/.../cosyvoiceClone.ts::CONSENT_MODAL_VERSION``
    3. ConsentModal 的 ``data-cosyvoice-consent-modal-version`` sentinel

    任何一处漂移：用户提交的 ``modal_version`` 不匹配 backend，所有 clone
    请求会被 400 ``consent_outdated`` 拒绝。
    """
    backend_version = _extract_consent_modal_version_from_gateway()

    # API client export
    client_src = API_CLIENT.read_text(encoding="utf-8")
    m_client = re.search(
        r"export\s+const\s+CONSENT_MODAL_VERSION\s*=\s*['\"]([^'\"]+)['\"]",
        client_src,
    )
    assert m_client, "API client 缺 export const CONSENT_MODAL_VERSION"
    client_version = m_client.group(1)
    assert client_version == backend_version, (
        f"frontend API client CONSENT_MODAL_VERSION='{client_version}' "
        f"≠ gateway '{backend_version}'。两侧必须严格一致。"
    )

    # ConsentModal must import (not redefine) the constant — single source of truth
    modal_src = CONSENT_MODAL.read_text(encoding="utf-8")
    assert "CONSENT_MODAL_VERSION" in modal_src, (
        "ConsentModal 必须使用 CONSENT_MODAL_VERSION 常量"
    )
    assert (
        re.search(
            r"import\s*\{[^}]*\bCONSENT_MODAL_VERSION\b[^}]*\}\s*from\s*['\"]@/lib/api/cosyvoiceClone['\"]",
            modal_src,
        )
        is not None
    ), (
        "ConsentModal 必须 import CONSENT_MODAL_VERSION（不允许重新定义），"
        "确保只有一处 source of truth"
    )

    # ConsentModal 也不应该在**代码**（非注释）中硬编码版本字面量
    # —— 防止 import + 又写死的双源问题。
    modal_code = _strip_comments(modal_src)
    hardcoded = re.findall(
        rf"['\"]{re.escape(backend_version)}['\"]",
        modal_code,
    )
    # data-cosyvoice-consent-modal-version sentinel 用 React 表达式 {CONSENT_MODAL_VERSION}
    # 不该出现硬编码字符串。允许 0 次，> 0 次是问题。
    assert len(hardcoded) == 0, (
        f"ConsentModal 不应在代码里硬编码版本字面量 '{backend_version}' —— "
        f"必须只用 import 的常量（注释里说明可以）。发现 {len(hardcoded)} 次硬编码。"
    )


# ---------------------------------------------------------------------------
# 3. COSYVOICE_TARGET_MODELS must mirror backend ALLOWED_TARGET_MODELS
# ---------------------------------------------------------------------------


def test_d2_target_models_match_backend():
    """**D.2 守卫 3**：前端 ``COSYVOICE_TARGET_MODELS`` 必须等于后端
    ``ALLOWED_TARGET_MODELS``，单值偏差 → 400 invalid_target_model。"""
    backend_src = GATEWAY_CLONE_API.read_text(encoding="utf-8")
    # ALLOWED_TARGET_MODELS may be a set / frozenset literal — match elements.
    m = re.search(
        r"ALLOWED_TARGET_MODELS\s*[:=][^\n]*\{([^}]*)\}",
        backend_src,
    )
    assert m, "找不到 gateway ALLOWED_TARGET_MODELS 集合定义"
    backend_models = set(re.findall(r"['\"]([^'\"]+)['\"]", m.group(1)))
    assert backend_models, "ALLOWED_TARGET_MODELS 解析为空"

    client_src = API_CLIENT.read_text(encoding="utf-8")
    m_client = re.search(
        r"COSYVOICE_TARGET_MODELS\s*=\s*\[(.+?)\]\s*as\s+const",
        client_src,
        re.DOTALL,
    )
    assert m_client, "API client 缺 COSYVOICE_TARGET_MODELS = [...] as const"
    client_models = set(re.findall(r"['\"]([^'\"]+)['\"]", m_client.group(1)))

    assert client_models == backend_models, (
        f"frontend COSYVOICE_TARGET_MODELS={sorted(client_models)} ≠ "
        f"backend ALLOWED_TARGET_MODELS={sorted(backend_models)}。两侧必须一致。"
    )


# ---------------------------------------------------------------------------
# 4. ConsentModal must have exactly 3 checkbox config entries
# ---------------------------------------------------------------------------


def test_d2_consent_modal_has_three_checkboxes():
    """**D.2 守卫 4**：``CHECKBOX_IDS`` 必须正好包含 3 个 id（source /
    data_flow / consequences），对应 legal v1 的 3 个独立同意。

    单独同意是声纹生物特征的合规要求（《信安技术 §个人信息安全规范》），
    不允许压缩成 "我同意全部"。
    """
    src = CONSENT_MODAL.read_text(encoding="utf-8")
    m = re.search(
        r"CHECKBOX_IDS\s*=\s*\[([^\]]+)\]\s*as\s+const",
        src,
    )
    assert m, "ConsentModal 缺 CHECKBOX_IDS = [...] as const"
    ids = re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))
    assert len(ids) == 3, (
        f"CHECKBOX_IDS 必须有 3 个 id（声音来源 / 数据流向 / 违规后果），"
        f"发现 {len(ids)}: {ids}"
    )
    assert set(ids) == {"source", "data_flow", "consequences"}, (
        f"CHECKBOX_IDS 不匹配 legal v1：期望 source/data_flow/consequences，"
        f"实际 {ids}"
    )


# ---------------------------------------------------------------------------
# 5. ConsentModal must NOT call submitCosyvoiceClone directly
# ---------------------------------------------------------------------------


def test_d2_consent_modal_does_not_call_paid_api():
    """**D.2 守卫 5（付费 API 硬约束 / CLAUDE.md）**：ConsentModal 不允许
    直接调 ``submitCosyvoiceClone``。授权 modal 是"用户同意"的纯 UI 步骤；
    付费 API 由父 CloneModal 在收到 ``onConfirm`` payload 后再触发，且需要
    用户**第二次**确认（在父 modal 上点"提交克隆" + 在子 modal 上点
    "开始克隆"）。
    """
    src = CONSENT_MODAL.read_text(encoding="utf-8")
    # 注释里可以提到（解释为什么不调），代码里禁止 import / 调用。
    code = _strip_comments(src)
    forbidden = re.compile(r"\bsubmitCosyvoiceClone\b")
    matches = forbidden.findall(code)
    assert not matches, (
        "ConsentModal 不应在代码里 import / 调用 submitCosyvoiceClone "
        "（注释里说明可以）—— 必须由父组件（CloneModal）在收到 onConfirm "
        "payload 后才发起 paid API"
    )


# ---------------------------------------------------------------------------
# 6. CloneModal must NOT auto-trigger paid API in useEffect / on mount
# ---------------------------------------------------------------------------


def test_d2_clone_modal_does_not_auto_trigger_paid_api():
    """**D.2 守卫 6（付费 API 硬约束 / CLAUDE.md）**：CloneModal 不允许
    在 ``useEffect`` 里调 ``submitCosyvoiceClone`` —— 付费 API 只能由用户
    显式点击"提交克隆" + ConsentModal 全勾选 + 点击"开始克隆"才触发。

    检测方式：扫所有 ``useEffect(...)`` 块，断言其 body 不含
    ``submitCosyvoiceClone``。
    """
    src = CLONE_MODAL.read_text(encoding="utf-8")
    # 用 multiline regex 抓所有 useEffect block bodies. 嵌套 ()/{} 不细致 parse；
    # 简化：找 useEffect( 后面到最早的 ", ["  (deps array start) 之间的内容。
    # 多个 useEffect 都查。
    useeffect_blocks = re.findall(
        r"useEffect\s*\(\s*\(\)\s*=>\s*\{(.+?)\}\s*,\s*\[",
        src,
        re.DOTALL,
    )
    assert useeffect_blocks, "找不到 useEffect 块（CloneModal 至少有 gate 拉取）"
    for block in useeffect_blocks:
        assert "submitCosyvoiceClone" not in block, (
            "CloneModal 的 useEffect 不能调用 submitCosyvoiceClone —— "
            "付费 API 必须由用户显式点击触发，**不能**自动 fire。"
            f"违规 useEffect 体:\n{block[:300]}..."
        )


# ---------------------------------------------------------------------------
# 7. API client endpoints must be /api/voice/cosyvoice/...
# ---------------------------------------------------------------------------


def test_d2_api_client_endpoints_under_voice_namespace():
    """**D.2 守卫 7**：API client 所有 fetch 必须打到 ``/api/voice/cosyvoice/...``，
    禁止任何 ``/api/admin/...`` 路径混入。D.1 P1 review 明确：clone-gate 是
    per-user display state，不是 admin op。
    """
    src = API_CLIENT.read_text(encoding="utf-8")
    # 抓所有 fetch 字面量字符串
    fetch_paths = re.findall(r"fetch\s*\(\s*['\"]([^'\"]+)['\"]", src)
    assert fetch_paths, "API client 没有 fetch 调用"
    for path in fetch_paths:
        assert path.startswith("/api/voice/cosyvoice/"), (
            f"API client fetch 路径 '{path}' 不在 /api/voice/cosyvoice/ 命名空间"
        )
        assert "/api/admin/" not in path, (
            f"API client 不允许调 admin endpoint: '{path}'"
        )


# ---------------------------------------------------------------------------
# 8. No JS test framework dependency introduced
# ---------------------------------------------------------------------------


def test_d2_no_js_test_framework_introduced():
    """**D.2 守卫 8（Codex 2026-05-27 锁死）**：D.2 不能引入任何 JS 测试栈
    依赖。Vitest / RTL / jsdom / happy-dom 都不允许。前端测试一律走 Python
    静态守卫 + ``npx tsc --noEmit`` + ``npm run lint``。
    """
    pkg = PACKAGE_JSON.read_text(encoding="utf-8")
    forbidden_deps = [
        "vitest",
        "@testing-library/react",
        "@testing-library/jest-dom",
        "@testing-library/user-event",
        "jsdom",
        "happy-dom",
        "jest",
    ]
    for dep in forbidden_deps:
        # quoted in JSON: "vitest": "..."
        assert f'"{dep}"' not in pkg, (
            f"package.json 不允许引入 '{dep}' 测试栈依赖 —— "
            f"D.2 测试栈一律用 Python 静态守卫 + tsc + lint。"
            f"详见 docs/plans/...phase4-go-live-plan.md / Codex 2026-05-27 决策。"
        )


# ---------------------------------------------------------------------------
# 9. No auto-retry of paid API
# ---------------------------------------------------------------------------


def test_d2_no_auto_retry_of_paid_clone_api():
    """**D.2 守卫 9（CLAUDE.md 付费 API 硬约束）**：API client 和 CloneModal
    不允许出现自动 retry 循环。任何调 ``submitCosyvoiceClone`` 的位置都
    必须由用户主动点击触发；不允许 ``setTimeout`` / setInterval / for-loop
    包裹该函数。
    """
    for path in (API_CLIENT, CLONE_MODAL):
        src = path.read_text(encoding="utf-8")
        # 找 submitCosyvoiceClone 所在行，断言往前 200 字符不出现 setTimeout /
        # setInterval / while / for（loop 关键字）
        for m in re.finditer(r"submitCosyvoiceClone", src):
            start = max(0, m.start() - 200)
            window = src[start : m.start()]
            for forbidden in (
                "setTimeout",
                "setInterval",
                # naive but effective: paid API call from inside a for/while
                # is the pattern we want to ban
                "for (",
                "while (",
            ):
                assert forbidden not in window, (
                    f"{path.name}: submitCosyvoiceClone 调用前 200 字符内出现 "
                    f"`{forbidden}` —— 不允许包在自动 retry / loop 里。"
                    f"上下文: ...{window[-150:]}..."
                )


# ---------------------------------------------------------------------------
# 10. Sample / segments mutex documented + enforced client-side
# ---------------------------------------------------------------------------


def test_d2_api_client_enforces_sample_source_mutex():
    """**D.2 守卫 10**：API client 必须在网络请求**之前**校验 sample 模式
    与 source segments 的互斥（防止浪费 multipart 上传）。锁定方式：检查
    出现 ``client_sample_source_mutex`` 错误码。
    """
    src = API_CLIENT.read_text(encoding="utf-8")
    assert "client_sample_source_mutex" in src, (
        "API client 必须在网络请求前 catch sample/source_segments 互斥违规，"
        "用错误码 `client_sample_source_mutex` 抛 CosyvoiceCloneApiError。"
    )
    # 双锁：确保 client_missing_sample_file / client_missing_source_segments
    # 也存在 —— file 模式必须有 sampleFile, segments 模式必须有 source segments
    assert "client_missing_sample_file" in src
    assert "client_missing_source_segments" in src
    assert "client_missing_source_job_id" in src


# ---------------------------------------------------------------------------
# 11. sourceSegmentIds type must be number[] (NOT string[])
#     PR #14 Codex P2 二轮 (discussion 2026-05-27)
# ---------------------------------------------------------------------------


def test_d2_source_segment_ids_typed_as_number_array():
    """**D.2 守卫 11（PR #14 Codex P2 二轮）**：``sourceSegmentIds`` 必须是
    ``number[]``，**不能**是 ``string[]``。

    后端 Phase 4.2 A.2b ``_parse_source_segments`` 严格 ``type(x) is int``
    校验，拒收 bool / float / str / None。``"1"`` 会被静默拒绝，``true``
    也会（即使 Python ``isinstance(True, int)`` 为真）。前端类型必须 ``number[]``
    才能让 TS 编译器在 caller 侧拦下 ``string[]`` 误用，不让坏值到达 API。
    """
    src = API_CLIENT.read_text(encoding="utf-8")
    code = _strip_comments(src)
    # 必须存在 ``sourceSegmentIds?: number[]`` 或 ``sourceSegmentIds: number[]``
    assert re.search(r"sourceSegmentIds\??:\s*number\[\]", code), (
        "API client 缺 sourceSegmentIds?: number[] 类型声明。"
        "后端严格 int[] 校验，frontend 必须 number[]。"
    )
    # 禁止 ``sourceSegmentIds: string[]`` / ``?: string[]``
    forbidden = re.search(r"sourceSegmentIds\??:\s*string\[\]", code)
    assert forbidden is None, (
        "API client 不允许把 sourceSegmentIds 声明为 string[] —— "
        "后端 _parse_source_segments 严格 type(x) is int 校验，"
        "string[] 会让所有调用失败。详见 PR #14 Codex P2 二轮。"
    )

    # CloneModal 也必须 number[]
    modal_src = CLONE_MODAL.read_text(encoding="utf-8")
    modal_code = _strip_comments(modal_src)
    assert re.search(r"sourceSegmentIds\??:\s*number\[\]", modal_code), (
        "CloneModal 的 sourceSegmentIds prop 也必须声明为 number[]"
    )
    assert (
        re.search(r"sourceSegmentIds\??:\s*string\[\]", modal_code) is None
    ), "CloneModal 不允许 sourceSegmentIds: string[]"


# ---------------------------------------------------------------------------
# 12. CloneModal: no placeholder segment id literal
#     PR #14 Codex P2 二轮 (discussion 2026-05-27)
# ---------------------------------------------------------------------------


def test_d2_clone_modal_no_placeholder_segment_id():
    """**D.2 守卫 12（PR #14 Codex P2 二轮）**：CloneModal **不允许**在
    segments 模式提交时传任何占位 segment id。后端 strict int 校验会拒收
    ``"__d2_placeholder__"`` / 0 / -1 / 同类 sentinel，让用户看到不明
    400 错误。

    实现约束：segments 模式 radio 只在父组件传入真实非空 ``sourceSegmentIds``
    时才放出；任何"占位 id"字符串都被禁。
    """
    src = CLONE_MODAL.read_text(encoding="utf-8")
    code = _strip_comments(src)
    # 禁止字面量 placeholder
    for forbidden in ("__d2_placeholder__", "__placeholder__"):
        assert forbidden not in code, (
            f"CloneModal 代码里出现 placeholder id '{forbidden}' —— "
            f"segments 模式必须用父组件传入的真实 number[]，不能提交占位 id。"
        )
    # 必须有 segmentsModeAvailable / 等价闸，禁止仅依赖 defaultSourceJobId
    assert (
        "segmentsModeAvailable" in code
    ), "CloneModal 必须用 segmentsModeAvailable 闸控（同时检查 jobId + 非空 array）"


# ---------------------------------------------------------------------------
# 13. ConsentModal: Cancel button must NOT call onClose directly —
#     must go through resetAndClose (PR #14 Codex P2 二轮)
# ---------------------------------------------------------------------------


def test_d2_consent_modal_cancel_button_uses_reset_path():
    """**D.2 守卫 13（PR #14 Codex P2 二轮）**：ConsentModal 的取消按钮
    必须经过 ``resetAndClose`` 路径，不允许直接 ``onClick={onClose}``。

    背景：用户勾选三个 checkbox 后点取消，如果不 reset，重开 modal 仍是
    已勾选状态 —— 削弱 "每次显式确认" 安全带。安全的 dismissal 必须从
    单一入口走（reset state + notify parent）。
    """
    src = CONSENT_MODAL.read_text(encoding="utf-8")
    code = _strip_comments(src)

    # 1. 必须存在 resetAndClose 函数
    assert (
        re.search(r"const\s+resetAndClose\s*=\s*\(\)\s*=>", code) is not None
    ), "ConsentModal 必须定义 resetAndClose helper"

    # 2. 取消按钮必须经 resetAndClose 路径 dismiss，不允许直接 onClick={onClose}。
    #    Locale-agnostic（uiloc W4b：取消 文案已迁 t("cancel")，不能再靠 `取消`
    #    字面量定位按钮）：断言 onClick={resetAndClose} 存在，且全文件**无任何**
    #    onClick={onClose}（cancel button + X/overlay/Esc 都必须走 resetAndClose /
    #    handleOpenChange，绝不裸调 onClose 绕过 checkbox reset）。
    assert (
        re.search(r"onClick=\{\s*resetAndClose\s*\}", code) is not None
    ), (
        "ConsentModal 取消按钮必须 onClick={resetAndClose} —— "
        "不允许直接 onClick={onClose}（会绕过 checkbox reset）。"
    )
    assert (
        re.search(r"onClick=\{\s*onClose\s*\}", code) is None
    ), (
        "ConsentModal 不允许任何 onClick={onClose} —— dismissal 必须经 "
        "resetAndClose 路径。详见 PR #14 Codex P2 二轮 + uiloc W4b。"
    )
