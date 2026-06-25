# TU-08 · 计费 & 付费路径结构化日志（print→logger）

- **目标 / 价值**：把计费、付费 API 重试、LLM fallback 等关键路径的 `print()` 替换为结构化 `logger.*` 调用，并注入 `extra={job_id, stage}`。这些路径在生产中静默无日志索引，出现 MiniMax/VolcEngine/Gemini 重试风暴或 metering 异常时无法快速定位，是审计盲区。改完后运维人员可用 `grep "tts_retry\|llm_fallback\|metering_skip"` 或 ELK/Loki 的结构化字段过滤直接定位事件。**本单元只改日志格式，绝不改调用逻辑，绝不新增任何自动付费调用。**
- **关联发现**：EH-001 · EH-002 · EH-008 · EH-011
- **前置依赖**：无（与 TU-01 的 EH-003/004/005 完全不重叠；与 TU-03 护栏无硬依赖，但若 TU-03 已就位可同步在 pre-commit `T20` 规则中验证 print 清零）
- **建议分支**：`quality/billing-logging`
- **预估工时**：M（预计 1–1.5 天；process.py 体量大，需分批次仔细核对）

> **命令环境**：默认 Git Bash / CI Linux（仓库已配 Bash 工具）；PowerShell 执行者改用等价命令（`grep`→`Select-String`、`wc -l`→`(Get-Content).Count`、`test -f`→`Test-Path`、避免 `<(...)` 进程替换）。

---

## 决策记录（CodeX 审核 2026-06-25，已采纳）

- **审计盲区优先**：本单元核心目标是覆盖计费、付费 API 重试、LLM fallback、metering skip 等审计盲区，而非追求全仓 print 总量大幅下降。
- **process.py 窄范围迁移**：process.py 只迁移"明确属于诊断日志且不参与 CLI/progress 协议"的少量 print（本单元约 11 处），不追求总数量目标；"230→≤220"类总量指标改为"审计盲区覆盖"指标。
- **不追求 print 总量指标**：量化收尾指标中 process.py 一行从 total-count 指标改为"审计盲区关键 event 名覆盖"；其余文件（tts_generator / translator / transcriber）的 print 计数目标保持不变（这些文件体量小，目标仍有意义）。
- **保留既有 observability 测试**：`test_tts_fallback_observability.py` 等现有契约测试完整保留，不为日志格式重构改动其业务断言或 event 字符串。
- **不改业务断言**：任何现有测试中的业务逻辑断言（非日志字符串断言）在本单元中不得修改；test_billing_logging_contracts.py 新增测试只做源码断言。
- **不引入新依赖**：logger 引入只用标准库 `logging`，不引入第三方结构化日志库。

---

## 不在本单元范围（out-of-scope）

- `process.py` **全量** print→logger 大迁移（230+ 处）：本单元只迁移**计费 / 付费路径**的关键 print（`[metering]` / `[smart][MONEY]` / `[PIPELINE]` snapshot / `[S1]` AssemblyAI retry / `[S2]` 内容合规 LLM retry）；其余非付费路径的 print 留给后续 Wave 专项（TU-14 做 process.py Option B 收敛时一并整理）。
- `tts_generator.py` 中已有 `logger.warning("tts_fallback_triggered ...")` 和 `logger.warning("free_voiceclone_fallback_to_preset ...")` 的结构化日志——**不改**，这是契约日志行（`test_tts_fallback_observability.py` 有源码断言，禁止修改字符串）。
- `gemini/translator.py` 中已有的 `_record_llm_usage()` usage 结构化记录——该机制已覆盖成功/失败元数据，不重复写；本单元只补缺失的 logger 行。
- EH-003/004/005（计费金额静默归零、billing webhook exc_info、credits_service fallback）是 TU-01 已覆盖的 gateway 侧问题，不在此处重做。
- `assemblyai/transcriber.py` 中非付费路径（音频预处理分词统计）的 print 留给后续全量清理批次。

---

## 必守不变量

- **付费 API 红线**：MiniMax 付费克隆 / 付费 TTS / 付费 LLM / 付费 ASR **绝不**在 fallback / except / retry / batch 路径自动触发。本单元只改 `print` → `logger.*`，不改任何条件分支、不改任何 `time.sleep`、不改任何 retry 计数——改前改后行为语义完全等价。如遇调用逻辑与日志混写（同一行），必须先拆行再仅改日志部分。
- **已有结构化日志契约不可破坏**：`tts_generator.py` 中的 `tts_fallback_triggered`、`free_voiceclone_fallback_to_preset`、`tts_fallback_failed` 字符串是 `test_tts_fallback_observability.py::test_fallback_trigger_emits_structured_log` 的源码断言锚点；**这三行 logger.warning 的字符串内容禁止修改**。
- **不改 process.py 整体架构**：本单元只在函数体内加/改日志行，不移动函数、不新增函数、不引入 import 层依赖，不触碰 Option B 收敛（那是 TU-14 的工作）。
- **Alignment DSP-first**、**剪映 draft 主交付物**、**Gateway 是 pricing 唯一事实源** 与日志变更无交叉，不影响。
- **默认测试不接真实外部服务**：新增测试必须用 `caplog` / mock，不调 MiniMax / Gemini / AssemblyAI 真实 API。

---

## Step 0 · 确认现状

```bash
# 1. 建分支
git switch -c quality/billing-logging

# 2. 确认基线守卫干净（不破坏现有契约）
python -m pytest tests/test_tts_fallback_observability.py -q          # 应全绿（8 passed）
python -m pytest tests/test_legacy_cleanup_guards.py -q               # 应全绿

# 3. 核对关键 file:line（多 agent 仓库行号可能已漂移，以实际输出为准）
# EH-001: process.py print 计数（spec 233，本文写作时核实为 230）
grep -c "^\s*print(" src/pipeline/process.py

# EH-002 / EH-008: tts_generator print 计数（spec & 核实均 38）
grep -c "^\s*print(" src/services/tts/tts_generator.py

# EH-002: assemblyai transcriber print 计数（核实 13）
grep -c "^\s*print(" src/services/assemblyai/transcriber.py

# EH-002: gemini transcriber print 计数（核实 4）
grep -c "^\s*print(" src/services/gemini/transcriber.py

# EH-002 / EH-011: gemini translator print 计数（核实 14，含 LLM fallback chain）
grep -c "^\s*print(" src/services/gemini/translator.py

# 4. 确认 tts_generator 已有 logger（应有 "import logging" + getLogger）
grep -n "import logging\|getLogger" src/services/tts/tts_generator.py

# 5. 确认 translator/transcriber 尚无 logger（本单元需补）
grep -rn "import logging\|getLogger" \
  src/services/gemini/translator.py \
  src/services/gemini/transcriber.py \
  src/services/assemblyai/transcriber.py \
  src/pipeline/process.py
# 预期：translator/transcriber/process.py 均无命中；tts_generator 已有
```

> 若任一 print 计数与上述核实值相差 > 10%，先核查哪些行被移动（`git log --oneline -5`），再继续。本文档 Step 1–5 的具体 file:line 均以 Step 0 实际输出为准，文内行号仅供参考。

---

## Step 1 · tts_generator.py：付费 TTS 重试 / fallback 路径 print → logger

**背景**：`tts_generator.py` 已有 `import logging` 和 `logger = logging.getLogger(__name__)`（第 6、14 行），但 38 个 print 均未使用这个 logger（EH-008）。本步只迁移**付费相关**的 7 个关键 print；进度显示类 print（`[S4] TTS 进度:` 等）留给后续全量批次。

**目标 print（需迁移，核实行号如下——执行前用 Step 0 命令确认）**：

| 核实行 | 当前 print 内容 | 迁移后 logger 级别 | 建议 event 名 |
|---|---|---|---|
| ~280 | `[S4] TTS provider: {provider} (source: {decision['source']})` | `logger.info` | `tts_provider_selected` |
| ~385 | `[S4] TTS 段 {seg.segment_id} 重试仍失败: {retry_exc}` | `logger.error` | `tts_segment_retry_exhausted` |
| ~405 | `[S4] TTS 段 {seg.segment_id} 重试成功` | `logger.info` | `tts_segment_retry_success` |
| ~659 | `[CosyVoice] speed_decision exception (fallback 1.0): {exc}` | `logger.warning` | `cosyvoice_speed_decision_fallback` |
| ~1085 | `[VolcEngine] speed_decision exception (fallback 1.0): {exc}` | `logger.warning` | `volcengine_speed_decision_fallback` |
| ~1533 | `[MiniMax] speed_decision exception (fallback 1.0): {exc}` | `logger.warning` | `minimax_speed_decision_fallback` |
| ~1666 | `[metering] TTS usage record skipped: {exc}` | `logger.warning` | `tts_metering_skip` |
| ~1205–1208 | `[S4] TTS 段 {segment_id} ({provider}) 失败，{wait}s 后重试` | `logger.warning` | `tts_segment_attempt_failed` |
| ~1785 | `[S4] MiniMax请求失败，{wait_seconds}秒后重试（{attempt+1}/{max_retries}）：{last_error}` | `logger.warning` | `minimax_request_retry` |
| ~1289–1291 | `[S4] TTS 段 {segment_id} 连续 {max_attempts} 次失败，暂停 {pause}s 后最后重试` | `logger.warning` | `tts_segment_pause_retry` |

> **注意**：`tts_fallback_triggered`（~1261）、`free_voiceclone_fallback_to_preset`（~1223）、`tts_fallback_failed`（~1279）已是结构化 `logger.warning`，**禁止改动**——`test_tts_fallback_observability.py` 断言这些字符串。

**具体改法示例**（其余参照同一模式）：

```python
# Before（约第 1205 行）:
print(
    f"[S4] TTS 段 {segment.segment_id} ({provider}) 失败，"
    f"{wait}s 后重试 ({attempt}/{max_attempts})..."
)

# After:
logger.warning(
    "tts_segment_attempt_failed segment=%s provider=%s attempt=%d/%d wait_s=%s",
    segment.segment_id, provider, attempt, max_attempts, wait,
    extra={"job_id": getattr(self, "_job_id", None), "stage": "s4_tts"},
)
```

```python
# Before（约第 1666 行）:
print(f"[metering] TTS usage record skipped: {exc}", flush=True)

# After:
logger.warning(
    "tts_metering_skip error=%s",
    exc,
    extra={"job_id": getattr(self, "_job_id", None), "stage": "s4_tts"},
)
```

> `getattr(self, "_job_id", None)` 是无副作用的 fallback——`TTSGenerator` 可能在无 job 上下文中被调用，None 可接受。若已有更确定的 job_id 获取路径（如构造函数注入），按实际用。

**该步验收**：
```bash
# 目标行已改为 logger 调用
grep -n "tts_segment_attempt_failed\|tts_segment_retry_exhausted\|minimax_request_retry\|tts_metering_skip\|tts_provider_selected\|tts_segment_pause_retry" src/services/tts/tts_generator.py
# 预期：≥8 命中（上表中的 event 名）

# 已有契约日志字符串未被修改
grep -c "tts_fallback_triggered" src/services/tts/tts_generator.py      # 应为 1
grep -c "free_voiceclone_fallback_to_preset" src/services/tts/tts_generator.py  # 应为 1

# print 计数应从 38 减少（目标：本步迁移约 10 处，剩约 28）
grep -c "^\s*print(" src/services/tts/tts_generator.py
# 预期：≤28（若 Step 0 基线是 38）

# 回归
python -m pytest tests/test_tts_fallback_observability.py -q   # 全绿（8 passed，契约字符串未动）
python -m pytest tests/ -q -k "tts" -p no:cacheprovider --timeout=60   # 已有 tts 相关测试全绿
```

---

## Step 2 · gemini/translator.py：LLM fallback chain + metering skip print → logger

**背景**：`gemini/translator.py` 无 `import logging`、无 `logger`（核实确认）。共 14 个 print（EH-002 + EH-011），其中 LLM fallback chain 和 metering skip 是最高优先级。

**文件顶部添加 logger**（在现有 `from __future__ import annotations` 之后、第一个 `import` 之前或附近，保持风格一致）：

```python
import logging
logger = logging.getLogger(__name__)
```

**目标 print（需迁移，核实行号如下）**：

| 核实行 | 当前 print 内容 | logger 级别 | event 名 |
|---|---|---|---|
| ~522 | `[metering] LLM usage record skipped: {exc}` | `logger.warning` | `llm_metering_skip` |
| ~1300 | `[LLM] {task} using {m} ({_resolve_model_id(m)})` | `logger.info` | `llm_attempt_start` |
| ~1356 | `[LLM] {task} {m} failed, falling back to {next_m}: {exc}` | `logger.warning` | `llm_fallback_triggered` |
| ~1372 | `[LLM-ROUTER-LEGACY] hit task=... service_mode=...` | `logger.warning` | `llm_router_legacy_path` |
| ~1424 | `[LLM] {task} using {alias}` (legacy router path) | `logger.info` | `llm_attempt_start` (同 event 名) |
| ~1487–1490 | `[LLM] {task} {alias} transient failure, retrying same model in {wait_seconds}s` | `logger.warning` | `llm_transient_retry` |
| ~1497 | `[LLM] {task} {alias} failed, falling back to {next_alias}: {last_error}` (legacy) | `logger.warning` | `llm_fallback_triggered` (同 event 名) |

> 其余 translator.py print（进度 `[S3]`、`[S2]` 审核进度等）为流程输出，不在本单元迁移范围。

**具体改法示例**：

```python
# Before（约第 1356 行）:
print(f"[LLM] {task} {m} failed, falling back to {next_m}: {exc}")

# After:
logger.warning(
    "llm_fallback_triggered task=%s model=%s fallback=%s error=%s",
    task, m, next_m, exc,
    extra={"stage": "llm_dispatch"},
)
```

```python
# Before（约第 1372 行）:
print(_legacy_path_msg, flush=True)

# After:
logger.warning(
    "llm_router_legacy_path task=%s prompt_key=%r mode=%r has_router=%s",
    task, prompt_key, mode, self.llm_router is not None,
    extra={"stage": "llm_dispatch"},
)
```

> `job_id` 在 translator 方法内通常不直接可见（translator 无 job_id 属性）；`extra` 只传 `stage`，不强求 job_id（不影响日志可用性，operator 可通过 request correlation 关联）。

**该步验收**：
```bash
# logger 已引入
grep -n "^import logging\|^logger = logging.getLogger" src/services/gemini/translator.py
# 预期：2 命中

# 关键 event 名已就位
grep -c "llm_fallback_triggered\|llm_metering_skip\|llm_router_legacy_path" src/services/gemini/translator.py
# 预期：≥3

# print 计数应从 14 减少（本步迁移约 7 处，剩约 7）
grep -c "^\s*print(" src/services/gemini/translator.py
# 预期：≤7

# 回归
python -m pytest tests/ -q -k "translator or llm" -p no:cacheprovider --timeout=60  # 全绿
```

---

## Step 3 · process.py：计费 / billing 关键 print → logger（process.py 最小集）

**背景**：`process.py` 共 230 个 print（EH-001），全量迁移是 L 级工作，留给 TU-14。本步**只迁移计费/付费可见性最高的路径**，给审计人员可索引的结构化事件。

**文件顶部添加 logger**（`process.py` 当前无 `import logging`，在已有 `import` 块末尾补）：

```python
import logging
logger = logging.getLogger(__name__)
```

**目标 print（核实行号）**：

| 核实行 | tag / 内容 | logger 级别 | event 名 |
|---|---|---|---|
| ~668–672 | `[smart] voice reuse usage audit failed speaker=...` | `logger.warning` | `smart_voice_reuse_audit_failed` |
| ~1367–1372 | `[smart] sidecar emit failed (call-site bug?)` | `logger.error` | `smart_sidecar_emit_failed` |
| ~1501–1506 | `[smart][MONEY] reservation present ({id}) but task_id empty` | `logger.error` | `smart_billing_inconsistency` |
| ~1521–1526 | `[smart][MONEY] register-billed FAILED status=...` | `logger.error` | `smart_register_billed_failed` |
| ~1531–1537 | `[smart][MONEY] register-billed EXCEPTION ...` | `logger.error` | `smart_register_billed_exception` |
| ~2177 | `[metering] Reported job metering to gateway: {status}` | `logger.info` | `job_metering_reported` |
| ~2179 | `[metering] Warning: failed to report job metering: {e}` | `logger.warning` | `job_metering_report_failed` |
| ~2202 | `[smart] Reported smart_state to gateway` | `logger.info` | `smart_state_reported` |
| ~2205 | `[smart] Warning: failed to report smart_state: {e}` | `logger.warning` | `smart_state_report_failed` |
| ~2333 | `[metering] set_usage_meter skipped: {exc}` | `logger.warning` | `metering_setup_skip` |
| ~2342 | `[metering] usage summary skipped: {exc}` | `logger.warning` | `metering_summary_skip` |

> **执行时前置动作（已定方向）**：`process.py:1365-1366` 有一段现有注释 `# process.py uses print for diagnostic output throughout (no module-level logger configured); follow the convention.`——迁移后此注释已过时，需同步删除（不要留过时注释误导后续）。

**`extra` 字段约定**：process.py 中大多数函数通过参数或闭包可取到 `job_id`；在能取到的地方传 `extra={"job_id": job_id, "stage": "<stage>"}` ；取不到时 `extra={"stage": "<stage>"}` 即可。

**具体改法示例**：

```python
# Before（约第 1501 行）:
print(
    f"[smart][MONEY] reservation present ({_reservation_id}) but task_id "
    f"empty — routing to register-smart (NO billing event). voice={voice_id}. "
    f"finalizer will release; business eats clone cost. Needs reconcile.",
    flush=True,
)

# After:
logger.error(
    "smart_billing_inconsistency reservation=%s voice=%s reason=task_id_empty",
    _reservation_id, voice_id,
    extra={"stage": "smart_clone_register"},
)
```

```python
# Before（约第 2177 行）:
print(f"[metering] Reported job metering to gateway: {resp.status}", flush=True)

# After:
logger.info(
    "job_metering_reported status=%s job=%s",
    resp.status, job_id,
    extra={"stage": "metering", "job_id": job_id},
)
```

**该步验收**：
```bash
# logger 已引入
grep -n "^import logging\|^logger = logging.getLogger" src/pipeline/process.py
# 预期：2 命中

# 审计盲区关键 event 名就位（覆盖计费/付费路径）
grep -c "smart_billing_inconsistency\|smart_register_billed_failed\|smart_register_billed_exception\|job_metering_reported\|job_metering_report_failed" src/pipeline/process.py
# 预期：≥5

# 过时注释已删
grep -c "no module-level logger configured" src/pipeline/process.py
# 预期：0

# 注：不以 print 总量下降幅度为验收门槛；本步目标是"审计盲区覆盖"而非"print 清零"
# 仅供参考（不作 gate）：本步约迁移 11 处计费/付费路径 print
grep -c "^\s*print(" src/pipeline/process.py

# 回归
python -m pytest tests/test_legacy_cleanup_guards.py tests/test_phase1_guards.py -q   # 全绿
```

---

## Step 4 · assemblyai/transcriber.py + gemini/transcriber.py：ASR 付费 retry print → logger

**背景**：AssemblyAI 是付费 ASR（EH-002），retry/fallback 路径不可见（`transcriber.py` 13 print，无 logger）。Gemini transcriber 是 LLM 付费调用，4 个 print 也无 logger。

**assemblyai/transcriber.py 顶部添加**：
```python
import logging
logger = logging.getLogger(__name__)
```

**gemini/transcriber.py 顶部添加**（在现有 `from __future__ import annotations` 之后）：
```python
import logging
logger = logging.getLogger(__name__)
```

**目标 print（assemblyai/transcriber.py，核实行号）**：

| 核实行 | 内容 | logger 级别 | event 名 |
|---|---|---|---|
| ~100–102 | `[S1] AssemblyAI请求失败，{wait_seconds}秒后重试` | `logger.warning` | `assemblyai_request_retry` |
| ~139 | `[S1] 正在上传音频到 AssemblyAI...` | `logger.info` | `assemblyai_upload_start` |
| ~143–145 | 上传完成 / 等待转录结果 | `logger.info` | `assemblyai_upload_done` |
| ~150 | `[S1] 转录结果已返回，正在整理文本...` | `logger.info` | `assemblyai_transcript_received` |
| ~180 | `[S1] 音频文件较大，生成MP3上传优化文件` | `logger.info` | `assemblyai_mp3_optimize_start` |
| ~194 | `[S1] 生成MP3上传优化文件失败，回退原始音频上传：{exc}` | `logger.warning` | `assemblyai_mp3_optimize_fallback` |

> 剩余 `[S1]` print（分词统计）为纯流程输出，不在本步范围。

**目标 print（gemini/transcriber.py，核实行号）**：

| 核实行 | 内容 | logger 级别 | event 名 |
|---|---|---|---|
| ~116–117 | `[S1] Gemini 多模态转录：{url}` / `使用模型：{model}` | `logger.info` | `gemini_transcribe_start` |
| ~126 | `[S1] Gemini 原始响应已保存：{path}` | `logger.debug` | `gemini_response_saved` |
| ~160 | `[S1] Gemini 转录完成：共 {n} 条，总时长 {ms}ms` | `logger.info` | `gemini_transcribe_done` |

**改法示例**（assemblyai retry）：

```python
# Before（约第 100 行）:
print(
    f"[S1] AssemblyAI请求失败，{wait_seconds}秒后重试"
    f"（{attempt+1}/{DEFAULT_MAX_RETRIES}）: {exc}"
)

# After:
logger.warning(
    "assemblyai_request_retry attempt=%d/%d wait_s=%d error=%s",
    attempt + 1, DEFAULT_MAX_RETRIES, wait_seconds, exc,
    extra={"stage": "s1_transcribe"},
)
```

**该步验收**：
```bash
# logger 已引入（两个文件）
grep -n "import logging\|getLogger" \
  src/services/assemblyai/transcriber.py \
  src/services/gemini/transcriber.py
# 预期：每个文件各 2 行

# 关键付费 retry event 就位
grep -c "assemblyai_request_retry" src/services/assemblyai/transcriber.py   # ≥1
grep -c "gemini_transcribe_start\|gemini_transcribe_done" src/services/gemini/transcriber.py  # ≥2

# print 计数下降
grep -c "^\s*print(" src/services/assemblyai/transcriber.py   # ≤9（从 13 减少约 4）
grep -c "^\s*print(" src/services/gemini/transcriber.py       # 0（全部 4 个迁完）

# 回归
python -m pytest tests/ -q -k "transcrib or assemblyai or gemini" -p no:cacheprovider --timeout=60
```

---

## Step 5 · 新增回归测试锁定审计日志契约

**目标**：为本单元新增的结构化日志关键路径添加契约测试，防止未来重构时 event 名被意外改掉或删掉。

**新建文件**：`tests/test_billing_logging_contracts.py`

**测试内容**：

```python
"""TU-08 · 计费 & 付费路径结构化日志契约守卫。

使用 inspect.getsource 做源码断言（同 test_tts_fallback_observability.py 惯例），
不调用任何真实外部服务。
"""
from __future__ import annotations
import inspect


class TestTTSGeneratorBillingLogContracts:
    def test_metering_skip_uses_logger(self):
        from services.tts import tts_generator
        src = inspect.getsource(tts_generator)
        assert "tts_metering_skip" in src, (
            "tts_generator 必须有结构化日志 tts_metering_skip（metering 异常审计）"
        )

    def test_tts_retry_uses_logger(self):
        from services.tts import tts_generator
        src = inspect.getsource(tts_generator)
        assert "tts_segment_attempt_failed" in src, (
            "tts_generator 必须有结构化日志 tts_segment_attempt_failed（重试路径）"
        )

    def test_minimax_retry_uses_logger(self):
        from services.tts import tts_generator
        src = inspect.getsource(tts_generator)
        assert "minimax_request_retry" in src, (
            "tts_generator 必须有结构化日志 minimax_request_retry（MiniMax 重试路径）"
        )

    def test_existing_fallback_contracts_intact(self):
        """TU-08 不得破坏 T7 既有契约日志字符串。"""
        from services.tts import tts_generator
        src = inspect.getsource(tts_generator)
        assert "tts_fallback_triggered" in src
        assert "free_voiceclone_fallback_to_preset" in src
        assert "tts_fallback_failed" in src


class TestTranslatorLLMLogContracts:
    def test_llm_fallback_uses_logger(self):
        from services.gemini import translator
        src = inspect.getsource(translator)
        assert "llm_fallback_triggered" in src, (
            "translator 必须有结构化日志 llm_fallback_triggered（LLM fallback chain 审计）"
        )

    def test_llm_legacy_path_uses_logger(self):
        from services.gemini import translator
        src = inspect.getsource(translator)
        assert "llm_router_legacy_path" in src, (
            "translator 必须有结构化日志 llm_router_legacy_path（legacy router 审计）"
        )

    def test_llm_metering_skip_uses_logger(self):
        from services.gemini import translator
        src = inspect.getsource(translator)
        assert "llm_metering_skip" in src, (
            "translator 必须有结构化日志 llm_metering_skip（metering 异常审计）"
        )


class TestProcessBillingLogContracts:
    def test_smart_billing_inconsistency_uses_logger(self):
        from pipeline import process as pipeline_process
        src = inspect.getsource(pipeline_process)
        assert "smart_billing_inconsistency" in src, (
            "process.py 必须有结构化日志 smart_billing_inconsistency（reservation 与 task_id 不一致）"
        )

    def test_smart_register_billed_failed_uses_logger(self):
        from pipeline import process as pipeline_process
        src = inspect.getsource(pipeline_process)
        assert "smart_register_billed_failed" in src, (
            "process.py 必须有结构化日志 smart_register_billed_failed（register-billed 失败路径）"
        )

    def test_job_metering_reported_uses_logger(self):
        from pipeline import process as pipeline_process
        src = inspect.getsource(pipeline_process)
        assert "job_metering_reported" in src, (
            "process.py 必须有结构化日志 job_metering_reported（metering 上报成功）"
        )

    def test_no_stale_no_logger_comment(self):
        """过时注释「no module-level logger configured」不能残留。"""
        from pipeline import process as pipeline_process
        src = inspect.getsource(pipeline_process)
        assert "no module-level logger configured" not in src, (
            "process.py 中「no module-level logger configured」注释应在 TU-08 后删除"
        )


class TestASRTranscriberLogContracts:
    def test_assemblyai_retry_uses_logger(self):
        from services.assemblyai import transcriber
        src = inspect.getsource(transcriber)
        assert "assemblyai_request_retry" in src, (
            "assemblyai/transcriber 必须有结构化日志 assemblyai_request_retry（付费 ASR 重试）"
        )

    def test_gemini_transcribe_lifecycle_uses_logger(self):
        from services.gemini import transcriber
        src = inspect.getsource(transcriber)
        assert "gemini_transcribe_start" in src, (
            "gemini/transcriber 必须有结构化日志 gemini_transcribe_start"
        )
```

**该步验收**：
```bash
# 测试文件存在
test -f tests/test_billing_logging_contracts.py   # exit 0

# 修改完 Step 1–4 后，契约守卫全部通过
python -m pytest tests/test_billing_logging_contracts.py -v --tb=short
# 预期：全绿（14 passed）；Step 1–4 完成前会有部分 FAIL，这是预期行为
```

> **建议执行顺序**：先写测试（本步），运行看 FAIL（RED），再执行 Step 1–4（GREEN）。这样每步完成后可立刻看测试由红转绿，符合 TDD 节奏。

---

## 测试计划

### 新增

- `tests/test_billing_logging_contracts.py`（Step 5）：14 个源码断言，全覆盖本单元引入的 event 名；可在 CI 无网络环境下运行（纯 `inspect.getsource`）。

### 回归

```bash
# 所有现有 TTS fallback 契约（最关键，禁止破坏）
python -m pytest tests/test_tts_fallback_observability.py -q            # 8 passed

# legacy cleanup 守卫（防止引入 import 破坏隔离）
python -m pytest tests/test_legacy_cleanup_guards.py -q                 # 全绿

# phase1 守卫（防止 commit 管线引入 TTS 调用）
python -m pytest tests/test_phase1_guards.py -q                         # 全绿

# TTS 相关测试
python -m pytest tests/ -q -k "tts" -p no:cacheprovider --timeout=60

# 翻译/转录相关测试
python -m pytest tests/ -q -k "translator or transcrib or assemblyai or gemini" \
  -p no:cacheprovider --timeout=60
```

---

## 量化收尾指标

本单元完成后，以下指标应满足（由 DoD 验收命令机器核实）：

| 指标 | 起始值（Step 0 核实） | 目标值 |
|---|---|---|
| `tts_generator.py` print 计数 | 38 | ≤28 |
| `gemini/translator.py` print 计数 | 14 | ≤7 |
| `gemini/transcriber.py` print 计数 | 4 | 0 |
| `assemblyai/transcriber.py` print 计数 | 13 | ≤9 |
| `process.py` 审计盲区 event 名覆盖（计费/付费路径） | 0 | ≥5（`smart_billing_inconsistency` 等关键 event 就位） |
| 新增结构化 event 名（跨 4 文件） | 0 | ≥20 |
| 新增契约守卫测试 | 0 | 14 |

---

## 回滚方案

各步独立 commit（遵守 DoD 末项），任一步出问题可单独 `git revert <该步 commit>`：

| Commit | 文件 | 可独立回滚 |
|---|---|---|
| Step 1 commit | `src/services/tts/tts_generator.py` | 是 |
| Step 2 commit | `src/services/gemini/translator.py` | 是 |
| Step 3 commit | `src/pipeline/process.py` | 是 |
| Step 4 commit | `src/services/assemblyai/transcriber.py`, `src/services/gemini/transcriber.py` | 是 |
| Step 5 commit | `tests/test_billing_logging_contracts.py`（新增） | 是 |

**测试文件（Step 5）**可最先 commit（仅新增文件，不影响任何生产代码），也可最后 commit（先做完 Step 1–4 再提测试）；两种顺序均可，但**每步一个 commit**不变。

---

## 完成定义（DoD）

- [ ] **Step 1**：`tts_generator.py` print 从 38 → ≤28；`tts_metering_skip` / `tts_segment_attempt_failed` / `minimax_request_retry` 等 event 名就位；已有 `tts_fallback_triggered` 等 3 个契约字符串未被改动；`test_tts_fallback_observability.py` 全绿；不引入新依赖（仅标准库 `logging`）。
- [ ] **Step 2**：`gemini/translator.py` 新增 `import logging` + `logger`；`llm_fallback_triggered` / `llm_metering_skip` / `llm_router_legacy_path` event 名就位；print 从 14 → ≤7；translator 相关测试全绿；不引入新依赖（仅标准库 `logging`）。
- [ ] **Step 3**：`process.py` 新增 `import logging` + `logger`；`smart_billing_inconsistency` / `smart_register_billed_failed` / `job_metering_reported` 等 5+ 审计盲区 event 名就位；过时注释已删；不以 print 总量作为门槛（process.py 全量迁移属 TU-14 范围）；`test_legacy_cleanup_guards.py` 全绿；不引入新依赖（仅标准库 `logging`）。
- [ ] **Step 4**：`assemblyai/transcriber.py` 和 `gemini/transcriber.py` 均新增 logger；`assemblyai_request_retry` / `gemini_transcribe_start` 等就位；gemini transcriber print 清零；assemblyai transcriber print ≤9；对应回归测试全绿；不引入新依赖（仅标准库 `logging`）。
- [ ] **Step 5**：`tests/test_billing_logging_contracts.py` 存在；14 个契约守卫全绿（`python -m pytest tests/test_billing_logging_contracts.py -q` → 14 passed）；新增测试只做源码断言，不改任何现有测试的业务断言；`test_tts_fallback_observability.py` 等既有 observability 测试断言内容不得修改。
- [ ] **全程未引入任何自动付费调用**：`grep -n "minimax\|cosyvoice\|assemblyai\|volcengine" tests/test_billing_logging_contracts.py` 无命中（测试文件中无真实 provider 引用）。
- [ ] **付费 API 红线未被触碰**：`git diff quality/billing-logging...main -- src/services/tts/tts_generator.py | grep "^+" | grep -E "time\.sleep|retry|fallback"` 输出仅为日志相关改动，无逻辑变更。
- [ ] **各步独立 commit（≥5 个）、显式 pathspec（`git commit -- <files>`）、未使用 `git add .`**。
