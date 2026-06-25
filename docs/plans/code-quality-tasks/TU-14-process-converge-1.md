# TU-14 · process.py Option B 输出收敛第一刀

- **目标 / 价值**：严格遵照 ADR `docs/architecture/PROCESS_WORKFLOW_CONVERGENCE.md` 的 **Option B Step 1**（输出收敛优先），让 `process.py` 的输出路径更完整地走 `OutputDispatcher`，削减 legacy 输出分支，消除"两套输出真源"的架构债。本切片聚焦**可见行为不变**的机械收敛：先补 golden/contract 测试再动代码，收尾用 `file-size-guard` 强制 `process.py` 不再增长。
- **关联发现**：STRUCT-01（`process.py` 自 ADR 后 +52% 达 12,806 行，收敛停滞）· PRIOR-17（Option B 被违背，无强制执行门）
- **前置依赖**：独占 `process.py`（与 TU-15 若有 `process` 内性能点重叠，TU-14 先执行）；TU-03（`file-size-guard` 配置落地后可把 `process.py` 纳入基线）可并行但 TU-14 的 file-size 棘轮依赖 TU-03 的 `tools/file_size_baseline.json` 基础设施——若 TU-03 尚未合并，TU-14 需在本分支内自建最小 size-guard CI 脚本。
- **建议分支**：`quality/process-converge-1`
- **预估工时**：L（3–5 天，分段标注于各 Step）

> **命令环境**：默认 Git Bash / CI Linux（仓库已配 Bash 工具）；PowerShell 执行者改用等价命令（`grep`→`Select-String`、`tail -N`→`Select-Object -Last N`、`test -f`→`Test-Path`、`wc -l`→`(Get-Content file).Count`、避免 `<(...)` 进程替换）。

---

## 决策记录（CodeX 审核 2026-06-25，已采纳）

- **不预删数据结构**：未证明 `AlignedSegment` 与 `_write_srt_from_segments` 无活跃消费者前，不执行删除；Step 5 的删除操作只能在调查结论写入 PR 并确认无活跃消费者后才能触发，否则跳过。
- **BASELINE 只填实测值**：`PROCESS_PY_SIZE_BASELINE`（Step 6b 测试）与 `BASELINE`（Step 6a 脚本）均须在 Step 5 完成、行数稳定后填入实测行数，禁止预填宽松估算值（如 12500）。
- **严格 ADR Option B 小步走**：本单元只做"输出收敛第一刀"，不借机改 pipeline 架构方向（不引入新抽象层、不拆子包、不改 review gate 边界）；每步 commit 独立，遵守 out-of-scope 限制。
- **调查结论必须进 PR**：Step 5 无论是否删除，PR 描述中必须包含对 `AlignedSegment` / `_write_srt_from_segments` 活跃消费路径的调查结论（调用图 + grep 结果）。
- **Step 5 是条件执行**：Step 5b 改法的触发条件是"5a 调查确认无活跃消费者"，不是"项目主拍板"；若有活跃消费者则跳过并记录，推迟到 ADR Step 2。
- **不引入新依赖、不批量 type:ignore**：收敛操作不引入新运行时依赖；不通过批量添加 `# type: ignore` 绕过 mypy 错误。

---

## 不在本单元范围（out-of-scope）

- **ADR Step 2 资产/构建收敛**：`ProjectWorkflow.run_build()` 接管构建阶段，不在本切片。
- **ADR Step 3 review gate 收敛**：`speaker_review`／`translation_review`／`voice_review` 边界迁移，不在本切片。
- **ADR Step 4 物理退役**：删死分支、收缩兼容 shim，不在本切片。
- TTS / alignment / LLM 路径的任何改动。
- `process.py` 内 print→logging 大迁移（TU-08）。
- `job_intercept.py` route family 拆分（TU-09）。
- 付费 API 调用点的改动（硬约束，见下）。

---

## 必守不变量

1. **付费 API 红线**：`process.py` 内 MiniMax 付费克隆 / 付费 TTS / 付费 LLM / 付费 ASR **绝不**在 fallback / except / retry / batch 路径自动触发，只走用户显式 consent。本切片不触碰任何付费调用点，收敛仅限输出分支。

2. **Option B 兼容壳语义**：`process.py` 仍是兼容入口；不另起新架构；不物理拆成 `stages/` 子包；不引入 `PipelineContext` 或其他新抽象层。每一个迁移步骤都是"让 `process.py` 多消费 `OutputDispatcher` 已提供的能力"，而不是"把 `OutputDispatcher` 里的逻辑搬回 `process.py`"。

3. **Alignment DSP-first**：不迁移 `_run_alignment_and_publish_only`、`rewrite loop`、任何 alignment/retiming 逻辑。这些属于 ADR Step 3/4 范围。

4. **剪映 draft 主交付物不变**：`OutputDispatcher.dispatch()` 已负责产出 editor 结果；本切片不改 `OutputTarget.PUBLISH` 的语义、不改 manifest 写入路径、不改剪映 draft runner 的调用入口。

5. **可见行为不变原则**：每个迁移子步骤前后，`tests/test_process_pipeline.py` + `tests/test_process_workflow_convergence.py` + `tests/test_output_dispatcher.py` 全绿，且本单元新增的 golden/contract 测试也全绿。

6. **Gateway 是 plan/pricing/entitlement 唯一事实源**：本切片不改任何计费、额度、plan 检查逻辑。

7. **默认测试不接入真实外部服务**：所有新增测试用 mock/fake/monkeypatch，不调用真实 TTS / Gemini / AssemblyAI / MiniMax。

8. **`process.py` 行数只减不增**：本切片结束时 `process.py` 行数必须低于起始基线（12,806 行）。若某步骤暂时增行（如加注释/类型注解），必须在同步骤末尾说明并在该步骤的 PR 中净减。

---

## Step 0 · 确认现状（~0.5 天）

> 目的：固化当前基线、核对关键 file:line（多 agent 并行仓库行号可能漂移），建分支。

```bash
# 1. 建分支
git switch -c quality/process-converge-1

# 2. 记录基线行数（应约 12,806）
wc -l src/pipeline/process.py

# 3. 确认 OutputDispatcher import 位置（应 :33）
grep -n "from modules.output.output_dispatcher import OutputDispatcher" src/pipeline/process.py

# 4. 确认现有唯一调用点（_dispatch_process_output_bundle 定义 :11049；call sites :6772 + :7626）
grep -n "_dispatch_process_output_bundle\|OutputDispatcher().dispatch" src/pipeline/process.py

# 5. 确认 OutputDispatcher 源文件行数（应约 444）
wc -l src/modules/output/output_dispatcher.py

# 6. 现有两条收敛测试全绿
python -m pytest tests/test_process_workflow_convergence.py tests/test_output_dispatcher.py -q 2>&1 | tail -5
# 期待：7 passed … 7 passed

# 7. 现有两个关键集成测试全绿
python -m pytest tests/test_process_pipeline.py -k "end_to_end or output_bundle" -q 2>&1 | tail -5
# 期待：2 passed

# 8. 记录直接调用 process.py 的底层模块数（代表"process 作为独立架构中心"的程度）
grep -c "^from\|^import" src/pipeline/process.py
```

**若某个 file:line 与 spec 不符**：按实际位置填写本文后续 Step 中的引用，并在对应步骤行末注明 `[实际行号: XXXX，规格行号: YYYY]`。

**该步验收**：
```bash
wc -l src/pipeline/process.py   # 输出记录为 BASELINE_LINES（≥12000）
python -m pytest tests/test_process_workflow_convergence.py tests/test_output_dispatcher.py tests/test_process_pipeline.py -k "end_to_end or output_bundle" -q 2>&1 | tail -3
# 期待：9 passed
```

---

## Step 1 · 补 golden/contract 测试——先建回归网（~0.5 天）

> **重构类单元必守纪律**：先补 contract/golden/回归测试再动代码；迁移前后跑相同测试。本步骤是所有后续 Step 的安全网。

### 1a. CLI golden：`main.py process --help` 与输出目录结构

在 `tests/test_main_cli.py` 末尾新增一个测试类 `TestProcessCommandOutputStructure`，覆盖以下 contract：

- `run_process_command` 在 `result.status == "waiting_for_review"` 时**提前返回**，不打印"处理完成"横幅（`main.py:997-998`，核对后填入实际行号）。
- `run_process_command` 在 `result.status == "completed"` 时打印含"处理完成"的横幅，且横幅中包含 `result.dubbed_audio_path` 所指文件名（`main.py:1018-1038`）。
- `main --help` / `main help` 输出包含字符串 `"process"` 和 `"job-api"`（`main.py:1827-1828`，`_build_main_usage`）。

**改法**：在 `tests/test_main_cli.py` 末尾添加新测试类，全部用 `monkeypatch` + `FakePipeline`，不调用真实 pipeline。

**该步验收**：
```bash
python -m pytest tests/test_main_cli.py -q 2>&1 | tail -5
# 期待：新增测试全部 passed（基线原有测试不回退）
grep -c "def test_" tests/test_main_cli.py   # 行数应比 Step 0 前多 ≥3
```

### 1b. `_dispatch_process_output_bundle` contract 测试

在 `tests/test_process_workflow_convergence.py` 末尾新增测试类 `TestDispatchProcessOutputBundleContract`，覆盖：

- `_dispatch_process_output_bundle` 调用 `OutputDispatcher().dispatch()`，且传入 `OutputTarget.PUBLISH`（当前 `process.py:11062`）。
- 返回的 `OutputBundleResult.editor_result` 不为 `None`（pipeline 的 `assert output_bundle.editor_result is not None` 前提，`process.py:6783` 和 `:7630`）。
- 调用时传入的 `watermark_text` 参数正确透传给 `OutputRequest.watermark_text`。

**改法**：用 `monkeypatch` 替换 `OutputDispatcher`，返回符合 contract 的 `FakeOutputBundleResult`；不需要真实文件系统。

**该步验收**：
```bash
python -m pytest tests/test_process_workflow_convergence.py -q 2>&1 | tail -5
# 期待：原有 7 passed + 新增 ≥3 passed
```

### 1c. `_build_legacy_process_output_stage_payload` contract 测试

在 `tests/test_process_workflow_convergence.py` 新增测试类 `TestLegacyOutputStagePayloadContract`，覆盖：

- `_build_legacy_process_output_stage_payload` 要求 `editor_result` 不为 `None`，否则抛 `ValueError`（`process.py:11453-11454`）。
- 返回 dict 包含 `"execution_mode": "legacy_process_output_dispatch"`、`"segment_count"`、`"manifest_path"`（`process.py:11457-11461`）。
- `_build_process_stage_outputs` 返回 dict 含 `"aligned_blocks"` key，且值列表元素类型为 `SemanticBlock`（已有测试 `TestProcessOutputBlockType`，补充验证 `"semantic_blocks"` 与 `"aligned_blocks"` key 同时存在）。

**该步验收**：
```bash
python -m pytest tests/test_process_workflow_convergence.py -q 2>&1 | tail -3
# 期待：全部 passed（新增累计 ≥6 个新测试）
```

---

## Step 2 · 清理 `legacy_process_output` stage 名称一致性（~0.5 天）

> **发现**：主路径用字符串字面量 `"legacy_process_output"`（`process.py:6742`），resume 路径用常量 `STAGE_LEGACY_PROCESS_OUTPUT`（`process.py:7603`）；两者逻辑相同但写法不一致，是潜在维护隐患。

### 2a. 确认两处调用

```bash
grep -n "\"legacy_process_output\"\|STAGE_LEGACY_PROCESS_OUTPUT" src/pipeline/process.py
# 期待：行 6742（字符串）、行 7603（常量），以及 import :25
grep -n "STAGE_LEGACY_PROCESS_OUTPUT" src/services/jobs/models.py
# 期待：定义在 :93-94，含 SUPPORTED_STAGES 列表
```

### 2b. 改法

将 `process.py:6742` 的字符串字面量 `"legacy_process_output"` 改用已 import 的常量 `STAGE_LEGACY_PROCESS_OUTPUT`：

```python
# 改前（process.py:6742，核对后填实际行号）
current_stage_name = "legacy_process_output"

# 改后
current_stage_name = STAGE_LEGACY_PROCESS_OUTPUT
```

此改动纯机械替换，不改逻辑，字符串值相同（`"legacy_process_output"`），只消除字面量重复。

**该步验收**：
```bash
# 改后不再有字面量副本
grep -c "\"legacy_process_output\"" src/pipeline/process.py
# 期待：0

# 常量引用数应增加 1
grep -c "STAGE_LEGACY_PROCESS_OUTPUT" src/pipeline/process.py
# 期待：≥3（import + 两处赋值）

# 回归：全量收敛相关测试
python -m pytest tests/test_process_workflow_convergence.py tests/test_output_dispatcher.py -q 2>&1 | tail -3
# 期待：全部 passed
```

---

## Step 3 · `_build_process_workflow_build_result` 参数化瘦身（~1 天）

> **现状**：`_build_process_workflow_build_result`（`process.py:11011`）接受 9 个关键字参数，函数体约 36 行。两个调用点（`process.py:6752` + `process.py:7612`）传参高度相似，差异只在 `total_duration_ms` 的变量名（主路径 `actual_duration_ms`，resume 路径 `actual_total_duration_ms`）和有无 `segments` 参数。
>
> **目标**：不改语义，通过验证当前测试仍覆盖两条调用路径，为后续 Step 4（提取 `_build_process_aligned_segment_info` 单一转换函数）打基础。本步骤不改函数签名，只补测试覆盖。

### 3a. 确认两个调用点参数差异

```bash
grep -n "build_process_workflow_build_result\|actual_duration_ms\|actual_total_duration_ms" src/pipeline/process.py | head -20
# 预期：:6752（主路径）、:7612（resume 路径）；duration 变量名不同
```

### 3b. 补 contract 测试

在 `tests/test_process_workflow_convergence.py` 新增测试类 `TestBuildProcessWorkflowBuildResult`，验证：

- 调用 `_build_process_workflow_build_result` 后返回值是 `WorkflowBuildResult` 实例。
- `WorkflowBuildResult.localized_project` 的 `project_id` 等于传入 `project_dir.name`（`process.py:11036`）。
- `WorkflowBuildResult.artifact_index` 包含由 `build_core_media_artifact_entries` 构建的条目键（`"source.original_video"` 或 `"working.speech_for_asr"` 等，依文件是否存在而定）。

测试用 `tmp_path` 创建最小文件树（`video/original.mp4`、`separated/speech.wav`、`separated/ambient.wav`），用 `monkeypatch` 替换 `AssemblyAI`/`Gemini` 等外部依赖。

**该步验收**：
```bash
python -m pytest tests/test_process_workflow_convergence.py -q 2>&1 | tail -3
# 期待：全部 passed（累计新增 ≥9 个测试）
python -m pytest tests/test_process_pipeline.py -k "end_to_end or output_bundle" -q 2>&1 | tail -3
# 期待：2 passed（原有集成测试不退）
```

---

## Step 4 · 提取 `_build_process_aligned_segment_info` 为独立函数（~1 天）

> **现状**：`_build_aligned_segments`（`process.py:10988`）和 `_build_process_output_blocks`（`process.py:11154`）对同一 `DubbingSegment` 列表各自做一次独立遍历，构建两种几乎平行的数据结构（`AlignedSegment` 列表 vs `SemanticBlock` 列表）。两个函数都在 `_build_process_workflow_build_result` 的间接调用链中。
>
> **目标（第一步）**：提取 `_resolve_process_output_block_status`（`process.py:11476`，当前已是 `@staticmethod`）为模块级函数（不再是 `ProcessPipeline` 的方法），以便在单测中直接调用，不需要实例化整个 `ProcessPipeline`。这是消除"process 作为独立架构中心"的第一个具体动作：把不依赖 `self` 的纯函数移出类。

### 4a. 确认 `_resolve_process_output_block_status` 位置与 self 依赖

```bash
grep -n "_resolve_process_output_block_status" src/pipeline/process.py
# 期待：:11476（定义，@staticmethod）、以及在 _build_process_output_blocks 的引用行（如 :11182）
```

### 4b. 改法

将 `_resolve_process_output_block_status` 从 `ProcessPipeline` 类内部移出，变为模块级私有函数 `_resolve_process_output_block_status`（保持下划线前缀），函数签名不变：

```python
# 改前：ProcessPipeline 内
@staticmethod
def _resolve_process_output_block_status(segment: DubbingSegment) -> str:
    ...

# 改后：模块级（ProcessPipeline 类定义之前或之后均可，建议紧邻相关函数）
def _resolve_process_output_block_status(segment: DubbingSegment) -> str:
    ...
```

类内引用 `self._resolve_process_output_block_status(segment)` 改为直接调用 `_resolve_process_output_block_status(segment)`。

### 4c. 补 contract 测试

在 `tests/test_process_workflow_convergence.py` 新增测试类 `TestResolveProcessOutputBlockStatus`，直接从 `pipeline.process` 导入并测试 `_resolve_process_output_block_status`：

```python
from pipeline.process import _resolve_process_output_block_status
```

验证：
- `alignment_method in {"force_dsp", "capped_dsp_overflow", "capped_dsp_underflow"}` 时返回 `"align_done_fallback"`。
- `needs_review=True` 时返回 `"align_review_needed"`。
- 其余情况返回 `"align_done"`。

**该步验收**：
```bash
# 函数已不在类内（不再通过 self. 调用）
grep -n "self\._resolve_process_output_block_status" src/pipeline/process.py
# 期待：0 行

# 模块级定义存在
grep -n "^def _resolve_process_output_block_status" src/pipeline/process.py
# 期待：≥1 行

# 所有测试全绿
python -m pytest tests/test_process_workflow_convergence.py tests/test_output_dispatcher.py tests/test_process_pipeline.py -k "end_to_end or output_bundle" -q 2>&1 | tail -3
# 期待：全部 passed

# 行数应有轻微下降（移出 @staticmethod 装饰器行，以及可能节省的缩进行）
wc -l src/pipeline/process.py   # 应 ≤ BASELINE_LINES
```

---

## Step 5 · 移除 `_build_aligned_segments` 的重复遍历（~1 天）

> **现状**：`_build_aligned_segments`（`process.py:10988`）与 `_build_process_output_blocks`（`process.py:11154`）对同一 `DubbingSegment` 列表各自一次遍历，字段高度重叠（`speaker_id`、`display_name`、`start_ms`、`end_ms`、`cn_text`、`alignment_method`、`needs_review`、`dubbing_mode`）。
>
> **查明是否有独立消费者**：`_build_aligned_segments` 的结果是否被 `_build_process_workflow_build_result` 之外的地方使用。

### 5a. 确认调用图

```bash
grep -n "_build_aligned_segments" src/pipeline/process.py
# 如果只被 _build_process_workflow_build_result 内部调用（间接通过 stage_outputs）
# 且 AlignedSegment 只在 editor_package_writer（已被标注 deprecated）中消费，
# 则可以安全内联到 _build_process_stage_outputs 后删除独立方法。
grep -rn "AlignedSegment\|aligned_segments" src/ tests/ | grep -v "process\.py\|__pycache__" | head -20
```

### 5b. 改法（仅当 5a 确认 `AlignedSegment` 无活跃外部消费者）

> ✅ 已决策（CodeX 2026-06-25）：执行时前置动作（已定方向）——在执行任何删除操作前，必须先跑 5a 调查命令，将 `AlignedSegment` 与 `_write_srt_from_segments` 的活跃消费路径调查结论（含 grep 输出与调用图）写入 PR 描述。未证明无活跃消费者前，**不删除** `AlignedSegment` 或 `_write_srt_from_segments`；若有活跃消费者，则本步骤跳过，在 PR 描述中记录结论并推迟到 ADR Step 2（资产/构建收敛）处理。

若确认 `AlignedSegment` 无活跃消费（或仅被 deprecated 路径使用）：

1. 将 `_build_process_stage_outputs` 中对 `_build_aligned_segments` 的调用改为直接从 `_build_process_output_blocks` 结果中读取已有字段，或合并两次遍历为一次。
2. 删除 `_build_aligned_segments` 方法定义（约 22 行）。
3. 若 `AlignedSegment` 仅被 deprecated 路径引用且此路径已有测试守卫，可将 import 降级为 `TYPE_CHECKING` 块以减少运行时依赖。

**该步验收**（改动发生时）：
```bash
# _build_aligned_segments 已删除
grep -c "def _build_aligned_segments" src/pipeline/process.py
# 期待：0

# AlignedSegment 仍在 import（如仍被 deprecated 路径需要）或已移入 TYPE_CHECKING
grep -n "AlignedSegment" src/pipeline/process.py

# 行数应下降（删除约 22 行方法定义）
wc -l src/pipeline/process.py   # 应 ≤ BASELINE_LINES - 20

# 回归
python -m pytest tests/test_process_workflow_convergence.py tests/test_output_dispatcher.py tests/test_process_pipeline.py -q 2>&1 | tail -3
# 期待：全部 passed
```

---

## Step 6 · 安装 file-size 棘轮守卫，防止 process.py 再次长大（~0.5 天）

> **目标**：为 Option B 收敛提供强制执行机制——没有这道门，收敛会像过去 3 个月一样被违背。

### 6a. 前置：检查 TU-03 是否已合并

```bash
test -f tools/file_size_baseline.json && echo "TU-03 已就位" || echo "需要在本分支自建"
```

**如果 TU-03 已合并**：将 `process.py` 的实际行数（Step 5 后的值）写入 `tools/file_size_baseline.json`，并确认 CI `file-size-guard` job 会检查 `src/pipeline/process.py`。

**如果 TU-03 尚未合并**：在本分支内新建脚本 `scripts/check_process_size.py`（最小实现）：

```python
"""CI guard: src/pipeline/process.py must not grow beyond recorded baseline."""
import sys
from pathlib import Path

BASELINE = 0  # ← 必须在 Step 5 完成后填入实测行数（执行者填写，不得预填宽松估算值）
target = Path("src/pipeline/process.py")
lines = len(target.read_text(encoding="utf-8").splitlines())
if BASELINE == 0:
    print("FAIL: BASELINE not set. Fill in the actual line count after Step 5 completes.", file=sys.stderr)
    sys.exit(1)
if lines > BASELINE:
    print(f"FAIL: process.py has {lines} lines, baseline is {BASELINE}.", file=sys.stderr)
    sys.exit(1)
print(f"OK: process.py {lines} lines ≤ {BASELINE}")
```

> ✅ 已决策（CodeX 2026-06-25）：`BASELINE` 必须在 Step 5 完成、行数稳定后由执行者填入实测行数，禁止预填宽松估算值（如 12500）。脚本用 `BASELINE == 0` 作防呆门，未填时 CI 直接报错。

### 6b. 在 `tests/test_process_workflow_convergence.py` 末尾加 size-guard contract 测试

```python
def test_process_py_does_not_exceed_size_baseline() -> None:
    """Regression guard: process.py must not grow beyond the Option B convergence baseline."""
    from pathlib import Path
    process_path = Path(__file__).parent.parent / "src" / "pipeline" / "process.py"
    lines = len(process_path.read_text(encoding="utf-8").splitlines())
    # Baseline set after TU-14 Step 5 completes.
    # Update ONLY when a convergence commit legitimately REDUCES line count.
    # MUST be filled with actual measured line count — do NOT pre-fill a loose estimate.
    PROCESS_PY_SIZE_BASELINE = 0  # ← 执行者在 Step 5 完成后填入实测行数
    assert PROCESS_PY_SIZE_BASELINE > 0, (
        "PROCESS_PY_SIZE_BASELINE not set. Fill in the actual line count after Step 5 completes."
    )
    assert lines <= PROCESS_PY_SIZE_BASELINE, (
        f"process.py has {lines} lines, exceeding baseline {PROCESS_PY_SIZE_BASELINE}. "
        "New features must not be added directly to process.py per Option B ADR. "
        "See docs/architecture/PROCESS_WORKFLOW_CONVERGENCE.md"
    )
```

> ✅ 已决策（CodeX 2026-06-25）：`PROCESS_PY_SIZE_BASELINE` 数值在 Step 5 完成后由执行者填入实测值，禁止预填宽松估算值。测试用 `PROCESS_PY_SIZE_BASELINE > 0` 作防呆断言，未填时测试直接失败并提示。PR 描述中须标注"当前行数 = XXXX，基线 = XXXX，下降 XXXX 行"。

**该步验收**：
```bash
python scripts/check_process_size.py   # （若 TU-03 未就位才建了此脚本）
# 期待：OK: process.py XXXX lines ≤ BASELINE

python -m pytest tests/test_process_workflow_convergence.py::test_process_py_does_not_exceed_size_baseline -q 2>&1 | tail -3
# 期待：1 passed
```

---

## 测试计划（新增 / 回归）

### 新增测试（本单元需写）

| 测试文件 | 测试类/函数 | 覆盖点 | Step |
|---|---|---|---|
| `tests/test_main_cli.py` | `TestProcessCommandOutputStructure` | CLI golden：`waiting_for_review` 提前返回；`completed` 横幅输出；`--help` 含 process/job-api | Step 1a |
| `tests/test_process_workflow_convergence.py` | `TestDispatchProcessOutputBundleContract` | `_dispatch_process_output_bundle` 调用 `OutputDispatcher`，传 `PUBLISH`，透传 `watermark_text` | Step 1b |
| `tests/test_process_workflow_convergence.py` | `TestLegacyOutputStagePayloadContract` | payload 含 `execution_mode`/`segment_count`/`manifest_path`；editor_result 为 None 时抛 ValueError | Step 1c |
| `tests/test_process_workflow_convergence.py` | `TestBuildProcessWorkflowBuildResult` | 返回 `WorkflowBuildResult`，`project_id` 等于 `project_dir.name`，`artifact_index` 含预期 key | Step 3b |
| `tests/test_process_workflow_convergence.py` | `TestResolveProcessOutputBlockStatus` | 模块级函数可直接导入；三条分支状态正确 | Step 4c |
| `tests/test_process_workflow_convergence.py` | `test_process_py_does_not_exceed_size_baseline` | file-size 棘轮：行数 ≤ 基线 | Step 6b |

### 回归测试（每步都必须通过）

```bash
# 每个 Step 完成后运行：
python -m pytest tests/test_process_workflow_convergence.py \
                 tests/test_output_dispatcher.py \
                 tests/test_output_dispatcher_subtitle_v2.py \
                 tests/test_process_pipeline.py -k "end_to_end or output_bundle" \
                 tests/test_main_cli.py \
                 -q 2>&1 | tail -5
```

---

## 回滚方案

### commit 边界

每个 Step 独立 commit，使用显式 pathspec（见 DoD）：

| Step | 涉及文件 | commit message 前缀 |
|---|---|---|
| 1a | `tests/test_main_cli.py` | `test: add CLI golden contracts for process command output` |
| 1b/1c | `tests/test_process_workflow_convergence.py` | `test: add contract tests for dispatch and legacy output payload` |
| 2 | `src/pipeline/process.py` | `refactor: use STAGE_LEGACY_PROCESS_OUTPUT constant consistently` |
| 3b | `tests/test_process_workflow_convergence.py` | `test: add contract for _build_process_workflow_build_result` |
| 4 | `src/pipeline/process.py`、`tests/test_process_workflow_convergence.py` | `refactor: move _resolve_process_output_block_status to module level` |
| 5（条件） | `src/pipeline/process.py` | `refactor: remove redundant _build_aligned_segments traversal` |
| 6 | `tests/test_process_workflow_convergence.py`、（可选）`scripts/check_process_size.py` | `test: add file-size regression guard for process.py` |

### 如何回滚单步

```bash
# 回滚某个 Step 的 commit（用实际 commit hash）
git revert <commit-hash> --no-edit
# 或仅回滚文件（不动其他 Step）
git checkout <commit-before-step> -- src/pipeline/process.py
```

### 紧急回滚整个分支

```bash
# 不合并此 PR——丢弃 quality/process-converge-1 分支即可
# main 分支未变，process.py 原样保持
```

---

## 完成定义（DoD）

- [ ] Step 0 基线已记录：`process.py` 起始行数、关键 `file:line` 已核对并与本文一致（或已标注实际行号）。
- [ ] Step 1 新增 contract/golden 测试：`tests/test_main_cli.py` 新增 ≥3 个测试；`tests/test_process_workflow_convergence.py` 新增 ≥6 个测试；全部通过。
- [ ] Step 2 常量一致性：`grep -c '"legacy_process_output"' src/pipeline/process.py` 输出 `0`。
- [ ] Step 3 contract 测试补充：`TestBuildProcessWorkflowBuildResult` 新增 ≥3 个测试，全部通过。
- [ ] Step 4 `_resolve_process_output_block_status` 已移至模块级：`grep -c 'self\._resolve_process_output_block_status' src/pipeline/process.py` 输出 `0`；新增 contract 测试全通过。
- [ ] Step 5 结论已记录：PR 描述中必须包含 `AlignedSegment` / `_write_srt_from_segments` 活跃消费路径的调查结论（含 grep 输出与调用图）；或已删除 `_build_aligned_segments`（附行数下降数据，且调查确认无活跃消费者），或已记录"活跃消费者存在、推迟到 ADR Step 2"——两种结果均须进 PR。未证明无活跃消费者前，**不得删除** `AlignedSegment` 或 `_write_srt_from_segments`。
- [ ] Step 6 file-size 棘轮已安装：`test_process_py_does_not_exceed_size_baseline` 通过；`PROCESS_PY_SIZE_BASELINE` 与 `scripts/check_process_size.py` 中的 `BASELINE` 均为 Step 5 后实测行数（非预填估算值，`> 0` 防呆断言通过），低于起始基线 12,806 行。
- [ ] **收尾指标**：`wc -l src/pipeline/process.py` 低于起始基线（12,806 行），PR 描述中标注净减行数。
- [ ] 全量回归：`python -m pytest tests/test_process_workflow_convergence.py tests/test_output_dispatcher.py tests/test_process_pipeline.py -k "end_to_end or output_bundle" tests/test_main_cli.py -q` 全部通过。
- [ ] **各步独立 commit、显式 pathspec（`git commit -- <files>`）、未使用 `git add .`**。
- [ ] **不引入新运行时依赖、不批量添加 `# type: ignore`**；本单元只做"输出收敛第一刀"，不改 pipeline 架构方向（无新抽象层、无子包拆分、无 review gate 边界变更）。
- [ ] PR 描述包含：① 起始行数 vs 结束行数 vs 净减量；② 每个 Step 的验收命令输出截图或文本；③ Step 5 调查结论（删除 or 推迟 + 原因 + grep 证据）；④ `PROCESS_PY_SIZE_BASELINE` 的实测值（Step 5 完成后填入，非预填）。
