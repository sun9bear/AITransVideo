"""Phase 4.2 E.2 — `source_segments` picker UI 静态守卫。

Per Codex 2026-05-27 v2.1 spec lock：仓库**没有** JS test runner（Vitest /
RTL / jsdom 均未装），E.2 守卫沿用 D.2 / E.1 模式 —— Python 静态扫描 +
``npx tsc --noEmit`` + ``npm run lint``。本测试**不**引入 JS 测试栈依赖。

锁死的 E.2 合约（共 19 项；§0 五条决策每条至少 1 个守卫 + picker→modal
回传契约 2 条）：

1. 文件存在 sanity
2. Picker 以 prop ``speakerId`` 调 ``getSpeakerAudioSegments`` —— 跨 speaker
   防御 #1（§0 决策 4 / R1）
3. Picker 不 import ``submitCosyvoiceClone`` —— 付费 API 入口隔离（§0 决策 5a）
4. Picker 不含字面量 ``/api/voice/cosyvoice/clone``（注释除外）（§0 决策 5a）
5. Modal 含 internal ``useState<number[]>`` + setter ``setSelectedSegmentIds``
   （§0 决策 3）
6. Modal **保留** ``sourceSegmentIds?: number[]`` 公开 prop（§0 决策 3 / D.2 契约）
7. Modal 含 useEffect 把 prop ``sourceSegmentIds`` 拷入内部 state（§0 决策 3）
8. Modal 中 ``setSampleMode("file")`` 邻近含 ``setSelectedSegmentIds([])``
   （§0 决策 5b / XOR 状态机）
9. Modal 中 ``setSampleMode("segments")`` 邻近含 ``setSampleFile(null)``
   （§0 决策 5b / XOR 状态机）
10. Modal 关闭 useEffect 含 ``setSelectedSegmentIds([])``
11. Modal 含 ``3`` / ``3.0`` + ``60`` / ``60.0`` 字面量阈值；同时
    backend ``sample_validator.py`` 含 ``MIN_DURATION_MS = 3_000`` 和
    ``MAX_DURATION_MS = 60_000`` —— 守卫做 ×1000 换算 cross-file 比对
    （§0 决策 2）
12. Modal ``handleSubmitClick``（或同名闸函数）含 ``availableSegmentIds`` +
    ``.has(`` 模式的子集校验（§0 决策 4 / L1.5）
13. ``VoiceModifyTab.tsx`` 中 ``<CosyVoiceCloneModal>`` 渲染**不**含
    ``defaultSourceJobId=`` —— editing 路径 file-only（§0 决策 1 / R7）
14. ``VoiceSelectionPanel.tsx`` 中 ``<CosyVoiceCloneModal>`` 渲染**含**
    ``defaultSourceJobId={jobId}`` —— approve 路径启用 picker
15. 全前端 grep 禁止 ``source_segments`` 字面量出现在 ``string`` / ``Array<string>``
    / ``.toString()`` 紧跟 ``segment_id`` 的上下文 —— 类型漂移（§0 R2）
16. ``VoiceCloneModal.tsx``（MiniMax 旧 clone）不含 ``cosyvoice`` 任何字面量
    + AST 无新增 cosyvoice import（§0 R4 / 不动 MiniMax）
17. ``package.json`` 不新增 vitest / @testing-library / jsdom / happy-dom
18. **v2.1** —— Picker 声明 ``onAvailableSegmentIdsChange: (ids: number[]) => void``
    prop；加载完成路径含调用（§0 决策 4）
19. **v2.1** —— Modal 渲染 ``<CosyVoiceSegmentPicker>`` 时传入
    ``onAvailableSegmentIdsChange=`` prop；handler 内含
    ``setAvailableSegmentIds(new Set(`` 模式（§0 决策 4）
"""
from __future__ import annotations

import json
import re
from pathlib import Path


def _strip_ts_comments(src: str) -> str:
    """剥掉 TS/TSX 的 ``//`` 行注释 + ``/* ... */`` 块注释。"""
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    src = re.sub(r"//[^\n]*", "", src)
    return src


REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = REPO_ROOT / "frontend-next"
PICKER_FILE = (
    FRONTEND_DIR / "src" / "components" / "voice-clone" / "CosyVoiceSegmentPicker.tsx"
)
CLONE_MODAL = (
    FRONTEND_DIR / "src" / "components" / "voice-clone" / "CosyVoiceCloneModal.tsx"
)
MINIMAX_CLONE_MODAL = (
    FRONTEND_DIR / "src" / "components" / "voice-clone" / "VoiceCloneModal.tsx"
)
VOICE_SELECTION_PANEL = (
    FRONTEND_DIR / "src" / "components" / "workspace" / "VoiceSelectionPanel.tsx"
)
VOICE_MODIFY_TAB = (
    FRONTEND_DIR
    / "src"
    / "app"
    / "(app)"
    / "workspace"
    / "[jobId]"
    / "edit"
    / "VoiceModifyTab.tsx"
)
PACKAGE_JSON = FRONTEND_DIR / "package.json"
BACKEND_SAMPLE_VALIDATOR = (
    REPO_ROOT / "gateway" / "cosyvoice_clone" / "sample_validator.py"
)


# ---------------------------------------------------------------------------
# 1. 文件存在
# ---------------------------------------------------------------------------


def test_e2_segment_picker_file_exists():
    """E.2 C1 落地的 picker 文件必须存在。"""
    assert PICKER_FILE.is_file(), f"picker missing at {PICKER_FILE}"


# ---------------------------------------------------------------------------
# 2. Picker 以 prop speakerId 调 getSpeakerAudioSegments —— 跨 speaker 防御 #1
# ---------------------------------------------------------------------------


def test_e2_segment_picker_speaker_id_required_in_api_call():
    """跨 speaker 防御 #1：picker 必须用 prop ``speakerId`` 调 backend
    speaker-filter 端点，**不允许**写死或绕过。"""
    src = _strip_ts_comments(PICKER_FILE.read_text(encoding="utf-8"))
    # getSpeakerAudioSegments(jobId, speakerId) ——参数顺序必须是 jobId, speakerId
    pattern = re.compile(
        r"getSpeakerAudioSegments\s*\(\s*jobId\s*,\s*speakerId\s*\)"
    )
    assert pattern.search(src), (
        "CosyVoiceSegmentPicker 必须以 prop speakerId 调用 "
        "getSpeakerAudioSegments(jobId, speakerId)。当前未找到该调用。"
    )


# ---------------------------------------------------------------------------
# 3. Picker 不 import submitCosyvoiceClone —— 付费 API 入口隔离 (§0 决策 5a)
# ---------------------------------------------------------------------------


def test_e2_segment_picker_no_paid_api_imports():
    """付费 API 隔离：picker 是纯选择层，**不允许** import 任何能触发付费
    调用的函数（``submitCosyvoiceClone``）。"""
    src = PICKER_FILE.read_text(encoding="utf-8")
    stripped = _strip_ts_comments(src)
    assert "submitCosyvoiceClone" not in stripped, (
        "CosyVoiceSegmentPicker 文件出现了 submitCosyvoiceClone 引用。"
        "Picker 不允许触发付费 clone 调用 —— 那只能由 modal consent 之后"
        "发起。"
    )


# ---------------------------------------------------------------------------
# 4. Picker 不含字面量 /api/voice/cosyvoice/clone （注释除外）
# ---------------------------------------------------------------------------


def test_e2_segment_picker_no_clone_endpoint_in_source():
    """付费 API 端点字面量隔离 —— picker 源码不能出现 clone endpoint 字符串。
    注释里允许（解释为什么不能用），剥注释后扫。"""
    src = _strip_ts_comments(PICKER_FILE.read_text(encoding="utf-8"))
    assert "/api/voice/cosyvoice/clone" not in src, (
        "CosyVoiceSegmentPicker 非注释代码出现 /api/voice/cosyvoice/clone "
        "字面量。Picker 是纯选择层，不允许直接调 clone 端点。"
    )


# ---------------------------------------------------------------------------
# 5. Modal 含 internal selectedSegmentIds state
# ---------------------------------------------------------------------------


def test_e2_modal_internalizes_selected_segment_ids():
    """§0 决策 3：modal 把 picker 的选段结果存到 internal state。"""
    src = _strip_ts_comments(CLONE_MODAL.read_text(encoding="utf-8"))
    has_state = re.search(
        r"useState<number\[\]>\s*\(\s*\[\s*\]\s*\)",
        src,
    )
    has_setter = "setSelectedSegmentIds" in src
    assert has_state and has_setter, (
        "CosyVoiceCloneModal 必须含 `useState<number[]>([])` 和 setter "
        "`setSelectedSegmentIds` —— 这是 §0 决策 3 的 internal state。"
    )


# ---------------------------------------------------------------------------
# 6. Modal 保留 sourceSegmentIds prop（D.2 契约不破坏）
# ---------------------------------------------------------------------------


def test_e2_modal_keeps_source_segment_ids_prop():
    """§0 决策 3：modal 公开 prop ``sourceSegmentIds?: number[]`` 保留。
    D.2 已把此 prop 锁定为公开契约，E.2 升级为"外部注入初始值"，但**不**
    删除接口。"""
    src = _strip_ts_comments(CLONE_MODAL.read_text(encoding="utf-8"))
    # interface CosyVoiceCloneModalProps 里必须仍有 sourceSegmentIds?: number[]
    pattern = re.compile(r"sourceSegmentIds\?\s*:\s*number\[\]")
    assert pattern.search(src), (
        "CosyVoiceCloneModalProps 移除了 `sourceSegmentIds?: number[]` "
        "公开 prop —— D.2 契约破坏。§0 决策 3 要求 prop 保留并升级为"
        "外部注入初始值。"
    )


# ---------------------------------------------------------------------------
# 7. Modal 含 useEffect 把 prop sourceSegmentIds 拷入内部 state
# ---------------------------------------------------------------------------


def test_e2_modal_initializes_internal_state_from_prop():
    """§0 决策 3：modal 打开时把 prop ``sourceSegmentIds`` 作为初始值
    拷入内部 ``selectedSegmentIds`` state。"""
    src = _strip_ts_comments(CLONE_MODAL.read_text(encoding="utf-8"))
    # 找一个 useEffect 块，里面同时含 sourceSegmentIds + setSelectedSegmentIds
    pattern = re.compile(
        r"useEffect\s*\([\s\S]*?sourceSegmentIds[\s\S]*?setSelectedSegmentIds[\s\S]*?\}\s*,\s*\[",
    )
    assert pattern.search(src), (
        "未找到 useEffect 把 sourceSegmentIds 拷入 selectedSegmentIds "
        "的代码。§0 决策 3 要求 modal 在 open 时初始化 internal state。"
    )


# ---------------------------------------------------------------------------
# 8. setSampleMode("file") 邻近含 setSelectedSegmentIds([])
# ---------------------------------------------------------------------------


def test_e2_modal_resets_segments_on_mode_switch_to_file():
    """§0 决策 5b：用户从 segments 切回 file 时，必须清空 selectedSegmentIds
    避免 XOR 状态残留。"""
    src = _strip_ts_comments(CLONE_MODAL.read_text(encoding="utf-8"))
    # 找一个含 setSampleMode("file") 的窗口，往后 200 字符内含 setSelectedSegmentIds([])
    matches = list(
        re.finditer(r'setSampleMode\(\s*"file"\s*\)', src)
    )
    assert matches, "未找到 setSampleMode(\"file\") 调用点。"
    found = False
    for m in matches:
        window = src[m.end() : m.end() + 400]
        if re.search(r"setSelectedSegmentIds\s*\(\s*\[\s*\]\s*\)", window):
            found = True
            break
    assert found, (
        "至少一个 setSampleMode(\"file\") 调用点的紧邻 400 字符内必须含 "
        "setSelectedSegmentIds([]) —— XOR 一致性。§0 决策 5b。"
    )


# ---------------------------------------------------------------------------
# 9. setSampleMode("segments") 邻近含 setSampleFile(null)
# ---------------------------------------------------------------------------


def test_e2_modal_resets_file_on_mode_switch_to_segments():
    """§0 决策 5b：用户从 file 切到 segments 时，必须清空 sampleFile
    避免 XOR 状态残留。"""
    src = _strip_ts_comments(CLONE_MODAL.read_text(encoding="utf-8"))
    matches = list(
        re.finditer(r'setSampleMode\(\s*"segments"\s*\)', src)
    )
    assert matches, "未找到 setSampleMode(\"segments\") 调用点。"
    found = False
    for m in matches:
        window = src[m.end() : m.end() + 400]
        if re.search(r"setSampleFile\s*\(\s*null\s*\)", window):
            found = True
            break
    assert found, (
        "至少一个 setSampleMode(\"segments\") 调用点的紧邻 400 字符内必须含 "
        "setSampleFile(null) —— XOR 一致性。§0 决策 5b。"
    )


# ---------------------------------------------------------------------------
# 10. Modal 关闭 useEffect 含 setSelectedSegmentIds([])
# ---------------------------------------------------------------------------


def test_e2_modal_close_resets_segments():
    """R3：modal 关闭 useEffect 必须清空 selectedSegmentIds，避免下次
    打开 / 切换 speaker 时残留旧选。"""
    src = _strip_ts_comments(CLONE_MODAL.read_text(encoding="utf-8"))
    # 找 `if (!open)` 块，里面应含 setSelectedSegmentIds([])
    pattern = re.compile(
        r"if\s*\(\s*!\s*open\s*\)\s*\{[\s\S]*?setSelectedSegmentIds\s*\(\s*\[\s*\]\s*\)",
    )
    assert pattern.search(src), (
        "Modal 关闭 useEffect（`if (!open)` 块）内未找到 "
        "setSelectedSegmentIds([])。R3：状态机不允许残留。"
    )


# ---------------------------------------------------------------------------
# 11. 3.0-60.0 秒阈值前后端 cross-file 同步
# ---------------------------------------------------------------------------


def test_e2_modal_three_to_sixty_second_threshold_literal():
    """§0 决策 2 / v2.1：客户端阈值固定 3.0-60.0 秒，与 backend
    ``MIN_DURATION_MS = 3_000`` / ``MAX_DURATION_MS = 60_000`` 一致（单位
    不同：前端 seconds，后端 ms；守卫做 ×1000 换算）。

    两侧任一字面量改了都会同步红。"""
    # 前端 modal canRequestConsent 含 selectedDurationSeconds < 3 / > 60
    modal_src = _strip_ts_comments(CLONE_MODAL.read_text(encoding="utf-8"))
    has_min = bool(
        re.search(r"selectedDurationSeconds\s*<\s*3(?:\.0)?(?!\d)", modal_src)
    )
    has_max = bool(
        re.search(r"selectedDurationSeconds\s*>\s*60(?:\.0)?(?!\d)", modal_src)
    )
    assert has_min and has_max, (
        "CosyVoiceCloneModal canRequestConsent 必须含 "
        "`selectedDurationSeconds < 3` 和 `selectedDurationSeconds > 60` "
        "字面量阈值（§0 决策 2）。"
    )

    # 后端 sample_validator.py 含 MIN_DURATION_MS = 3_000 / MAX_DURATION_MS = 60_000
    backend_src = BACKEND_SAMPLE_VALIDATOR.read_text(encoding="utf-8")
    backend_min = re.search(
        r"MIN_DURATION_MS\s*=\s*3_?000\b", backend_src
    )
    backend_max = re.search(
        r"MAX_DURATION_MS\s*=\s*60_?000\b", backend_src
    )
    assert backend_min and backend_max, (
        "Backend sample_validator.py 必须含 MIN_DURATION_MS = 3_000 和 "
        "MAX_DURATION_MS = 60_000。前后端阈值漂移会让前端禁用条件与"
        "后端拒绝条件不一致 —— 用户体验断层。"
    )
    # ×1000 换算的语义 cross-check（防御：有人改前端为 4 / 50 但后端没改）：
    # 后端 3_000 ms == 前端 3 s；后端 60_000 ms == 前端 60 s。本测试已经
    # 同时扫两侧字面量，等价于做了这个换算的 invariant。


# ---------------------------------------------------------------------------
# 12. handleSubmitClick 含子集 assert
# ---------------------------------------------------------------------------


def test_e2_modal_subset_assert_before_submit():
    """§0 决策 4：modal 提交前必须做子集 assert，防止 picker 状态泄漏。"""
    src = _strip_ts_comments(CLONE_MODAL.read_text(encoding="utf-8"))
    # 找 handleSubmitClick 函数，里面含 availableSegmentIds.has(
    pattern = re.compile(
        r"const\s+handleSubmitClick[\s\S]*?availableSegmentIds\.has\s*\(",
    )
    assert pattern.search(src), (
        "Modal 中未找到 handleSubmitClick 含 `availableSegmentIds.has(` "
        "子集 assert。§0 决策 4 / spec §4 E.2.4 L1.5 要求提交前防泄漏。"
    )


# ---------------------------------------------------------------------------
# 13. VoiceModifyTab 不传 defaultSourceJobId —— editing file-only
# ---------------------------------------------------------------------------


def test_e2_voice_modify_tab_no_default_source_job_id():
    """§0 决策 1 / R7：editing 路径 E.2 阶段只保留 file upload。
    VoiceModifyTab 渲染 ``<CosyVoiceCloneModal>`` 时**不传**
    ``defaultSourceJobId`` —— modal 自然回落 file-only。"""
    src = _strip_ts_comments(VOICE_MODIFY_TAB.read_text(encoding="utf-8"))
    # 找 <CosyVoiceCloneModal ... /> 的 JSX 块（可能跨行）
    blocks = re.findall(
        r"<CosyVoiceCloneModal\b[\s\S]*?/>",
        src,
    )
    assert blocks, "VoiceModifyTab 中未找到 <CosyVoiceCloneModal /> 渲染"
    for block in blocks:
        assert "defaultSourceJobId" not in block, (
            f"VoiceModifyTab 渲染 <CosyVoiceCloneModal> 含 defaultSourceJobId: "
            f"{block!r}\n§0 决策 1 要求 editing 路径不接 picker，"
            f"故意不传该 prop 让 modal 回落 file-only。"
        )


# ---------------------------------------------------------------------------
# 14. VoiceSelectionPanel 传 defaultSourceJobId —— 启用 picker
# ---------------------------------------------------------------------------


def test_e2_voice_selection_panel_passes_default_source_job_id():
    """§0 决策 1 反向 sanity：VoiceSelectionPanel（approve 路径）**必须**
    传 ``defaultSourceJobId={jobId}`` 启用 picker。"""
    src = _strip_ts_comments(VOICE_SELECTION_PANEL.read_text(encoding="utf-8"))
    blocks = re.findall(
        r"<CosyVoiceCloneModal\b[\s\S]*?/>",
        src,
    )
    assert blocks, (
        "VoiceSelectionPanel 中未找到 <CosyVoiceCloneModal /> 渲染"
    )
    found = False
    for block in blocks:
        if re.search(r"defaultSourceJobId\s*=\s*\{?\s*jobId\s*\}?", block):
            found = True
            break
    assert found, (
        "VoiceSelectionPanel 中至少一个 <CosyVoiceCloneModal /> 必须传 "
        "defaultSourceJobId={jobId} —— 启用 picker。"
    )


# ---------------------------------------------------------------------------
# 15. 全前端无 string segment id 漂移
# ---------------------------------------------------------------------------


def test_e2_no_string_segment_id_drift():
    """R2：CosyVoice clone 路径**不允许**把 segment id 当 string 用。

    严格扫**新建 / 修改的** E.2 文件：picker + modal + 两个调用点。
    其它前端文件可能因 MiniMax 旧逻辑等历史原因含此类模式，本守卫不扫
    （会触发误报）。"""
    files = [
        PICKER_FILE,
        CLONE_MODAL,
        VOICE_SELECTION_PANEL,
        VOICE_MODIFY_TAB,
    ]
    forbidden_patterns = [
        # `Array<string>` 紧跟 segment 上下文
        re.compile(r"Array<string>\s*[/;,]?\s*[^a-zA-Z_]*segment", re.IGNORECASE),
        # `source_segments.*string` —— 严格 number[] 漂移
        re.compile(r"source_segments[^;]*?:\s*string\["),
        re.compile(r"sourceSegmentIds[^;]*?:\s*string\["),
        # `.toString()` 直接跟 segment id 上下文
        re.compile(r"segment_?[iI]d\s*\.toString\s*\(\s*\)"),
    ]
    for fp in files:
        if not fp.is_file():
            continue
        src = _strip_ts_comments(fp.read_text(encoding="utf-8"))
        for pat in forbidden_patterns:
            m = pat.search(src)
            assert not m, (
                f"{fp.name} 出现 segment id string 漂移模式 {pat.pattern!r}："
                f"{m.group(0)!r}\nR2：CosyVoice clone 路径必须严格 number[]。"
            )


# ---------------------------------------------------------------------------
# 16. MiniMax VoiceCloneModal 不动
# ---------------------------------------------------------------------------


def test_e2_minimax_voice_clone_modal_untouched():
    """R4 / 用户硬约束：MiniMax 旧 clone 文件 ``VoiceCloneModal.tsx``
    不允许引入 cosyvoice 任何字面量 / import。"""
    if not MINIMAX_CLONE_MODAL.is_file():
        # 旧 MiniMax 文件可能改名 / 删除；存在性不是本守卫的责任
        return
    src = _strip_ts_comments(MINIMAX_CLONE_MODAL.read_text(encoding="utf-8"))
    lower = src.lower()
    assert "cosyvoice" not in lower, (
        f"MiniMax VoiceCloneModal.tsx 中出现 cosyvoice 字面量。"
        f"§0 决策 R4 / 用户硬约束：MiniMax 旧 clone 路径不动。"
    )


# ---------------------------------------------------------------------------
# 17. package.json 不引入 JS 测试栈
# ---------------------------------------------------------------------------


def test_e2_no_vitest_or_jsdom_introduced():
    """项目硬约束：守卫只用 Python 静态扫，不引入 JS 测试栈。"""
    pkg = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    forbidden = (
        "vitest",
        "@testing-library/react",
        "@testing-library/dom",
        "@testing-library/jest-dom",
        "jsdom",
        "happy-dom",
    )
    for name in forbidden:
        assert name not in deps, (
            f"package.json 引入了 {name}，违反 D.2 / E.1 / E.2 锁定的"
            f"「不引入 JS 测试栈」约束。"
        )


# ---------------------------------------------------------------------------
# 18. Picker 声明 onAvailableSegmentIdsChange prop（v2.1）
# ---------------------------------------------------------------------------


def test_e2_picker_declares_on_available_segment_ids_change_prop():
    """v2.1 / §0 决策 4：picker 必须声明
    ``onAvailableSegmentIdsChange: (ids: number[]) => void`` prop，
    加载完成路径含调用。"""
    src = _strip_ts_comments(PICKER_FILE.read_text(encoding="utf-8"))
    # Props 接口含 onAvailableSegmentIdsChange: (ids: number[]) => void
    pattern_decl = re.compile(
        r"onAvailableSegmentIdsChange\s*:\s*\(\s*ids\s*:\s*number\[\]\s*\)\s*=>\s*void",
    )
    assert pattern_decl.search(src), (
        "CosyVoiceSegmentPicker props 接口必须含 "
        "`onAvailableSegmentIdsChange: (ids: number[]) => void`。"
        "v2.1 / §0 决策 4 锁定的回传契约。"
    )
    # 加载完成路径必须有 onAvailableSegmentIdsChange(...) 调用
    # 允许通过 ref 间接调（如 onAvailableIdsRef.current(...)）—— 二选一
    pattern_call_direct = re.compile(r"onAvailableSegmentIdsChange\s*\(")
    pattern_call_via_ref = re.compile(
        r"on[A-Za-z]*AvailableIds[A-Za-z]*\.current\s*\(",
    )
    assert pattern_call_direct.search(src) or pattern_call_via_ref.search(src), (
        "Picker 加载完成路径必须调用 onAvailableSegmentIdsChange(...) "
        "（直接调或通过 ref 调）。"
    )


# ---------------------------------------------------------------------------
# 19. Modal 把 onAvailableSegmentIdsChange 传给 picker（v2.1）
# ---------------------------------------------------------------------------


def test_e2_modal_passes_on_available_segment_ids_change_to_picker():
    """v2.1 / §0 决策 4：modal 渲染 ``<CosyVoiceSegmentPicker>`` 时必须传
    ``onAvailableSegmentIdsChange=`` prop，handler 内含
    ``setAvailableSegmentIds(new Set(`` 模式。"""
    src = _strip_ts_comments(CLONE_MODAL.read_text(encoding="utf-8"))
    # 找 <CosyVoiceSegmentPicker ... onAvailableSegmentIdsChange=... /> 块
    picker_blocks = re.findall(
        r"<CosyVoiceSegmentPicker\b[\s\S]*?/>",
        src,
    )
    assert picker_blocks, (
        "Modal 中未渲染 <CosyVoiceSegmentPicker /> —— 整个 E.2 wiring 缺失"
    )
    found_prop = False
    found_setter = False
    for block in picker_blocks:
        if re.search(r"onAvailableSegmentIdsChange\s*=", block):
            found_prop = True
            if re.search(
                r"setAvailableSegmentIds\s*\(\s*new\s+Set\s*\(", block
            ):
                found_setter = True
                break
    assert found_prop, (
        "<CosyVoiceSegmentPicker /> 渲染未传 onAvailableSegmentIdsChange "
        "prop。v2.1 / §0 决策 4 锁定的 picker → modal 回传契约。"
    )
    assert found_setter, (
        "onAvailableSegmentIdsChange handler 必须含 "
        "`setAvailableSegmentIds(new Set(...))` 模式 —— 包成 Set<number> "
        "供子集 assert 使用。"
    )
