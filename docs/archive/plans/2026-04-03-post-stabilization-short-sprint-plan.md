# Post-Stabilization Short Sprint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不扩展新功能的前提下，完成部署态验证、`tests/test_process_pipeline.py` 的 10 个既有失败分流、以及 VolcEngine rerank readiness review，为下一轮功能开发提供清晰的 go / no-go 依据。

**Architecture:** 这个短 Sprint 只做验证、审计、分流和决策文档，不直接实现 VolcEngine rerank、动态音色库 Phase 2、或 publish 扩展。执行顺序遵循“先部署态事实、再测试债分流、再 rerank readiness review”的节奏，每个任务都产出一份独立的 acceptance/review 文档，并明确后续动作。

**Tech Stack:** Python 3.12, Docker Compose, FastAPI gateway, Job API, pytest, Markdown docs

---

## 1. Sprint 边界

### 本轮要完成的事

1. 验证当前部署态是否与稳定化结果一致，特别是容器拓扑、代码生效方式、日志/命令入口是否正常。
2. 把 `tests/test_process_pipeline.py` 当前 10 个失败分成“过时断言 / 真实行为差异 / 需要产品决定”的可执行队列。
3. 对 VolcEngine rerank 是否适合进入下一轮做 readiness review，并明确给出 go / no-go。

### 本轮明确不做的事

- 不实现 VolcEngine rerank
- 不实现动态音色库 Phase 2（CRUD / import / verify）
- 不实现 publish 能力扩展
- 不修改生产代码
- 不修改测试代码
- 不引入真实外部 API 调用

### Sprint 成功标准

- 产出 3 份独立文档，分别覆盖部署态验证、失败测试分流、rerank readiness review
- 每份文档都包含明确的事实证据、命令、结论和下一步建议
- 对 rerank 给出明确的 `GO` 或 `NO-GO`
- 对 10 个失败测试给出明确的去向，而不是继续停留在“只是基线”

---

## 2. 执行前置条件

### 2.1 工作区前提

当前仓库工作区仍然包含大量**不属于稳定化计划**的脏改动。因此执行本 Sprint 时，优先建议：

- 在当前分支 `codex/review-guidelines` 的最新提交上创建干净 worktree
- 或至少先记录当前 `git status --short`，并在 Sprint 文档中明确哪些观察来自“当前工作区本地状态”，哪些来自“已提交事实”

### 2.2 已知稳定化基线

以下结论应视为本 Sprint 的输入，而不是重新发明：

- `pytest tests/test_process_pipeline.py --tb=no -q`
  - 当前 accepted baseline: `10 failed, 63 passed, 1 warning`
- `pytest tests/test_tts_routing_invariants.py tests/test_project_builder.py tests/test_project_shape_helpers.py tests/test_output_dispatcher.py --tb=no -q`
  - 当前 accepted baseline: `28 passed, 2 warnings`
- `python main.py --help`
  - 当前 accepted behavior: 正常打印 usage，退出码 `1`

参考文档：

- `docs/acceptance/2026-04-03-test-baseline.md`
- `docs/acceptance/2026-04-03-process-convergence-audit.md`
- `docs/QUICKSTART.md`
- `docs/plans/2026-04-02-project-stabilization-and-convergence-plan.md`

---

## 3. 文件分工

### Sprint 产出

- Create: `docs/acceptance/2026-04-03-deployment-runtime-validation.md`
- Create: `docs/acceptance/2026-04-03-process-pipeline-failure-triage.md`
- Create: `docs/acceptance/2026-04-03-volcengine-rerank-readiness-review.md`

### 只读参考

- Read: `docs/QUICKSTART.md`
- Read: `docs/acceptance/2026-04-03-test-baseline.md`
- Read: `docs/specs/2026-04-02-dynamic-voice-library-plan.md`
- Read: `docs/deployment/RUN_ENVIRONMENT.md`
- Read: `docs/handover/2026-04-02-session-handover.md`
- Read: `docs/archive/2026-03-30-free-vs-paid-tts-routing-status-and-recovery-plan.md`
- Read: `src/services/tts/volcengine_voice_selector.py`
- Read: `src/services/tts/volcengine_voice_profile_data.json`

---

## 4. 任务实施计划

### Task 1: 部署态验证

**Files:**
- Create: `docs/acceptance/2026-04-03-deployment-runtime-validation.md`
- Read: `docs/QUICKSTART.md`
- Read: `docs/deployment/RUN_ENVIRONMENT.md`
- Read: `docs/archive/2026-03-30-free-vs-paid-tts-routing-status-and-recovery-plan.md`
- Read: `docs/handover/2026-04-02-session-handover.md`

- [ ] **Step 1: 先写部署态验证文档骨架**

文档至少包含以下小节：

- 执行环境
- 容器/服务拓扑
- 代码部署方式（bind mount / rebuild）
- 当前可验证的运行时入口
- 无法验证的部分与原因
- 结论：当前部署态 `GO` / `BLOCKED`

- [ ] **Step 2: 记录当前分支与工作区状态**

Run: `git branch --show-current`
Expected: 输出当前分支名

Run: `git status --short`
Expected: 能看见当前工作区是否存在无关脏改动；写入文档作为边界说明

- [ ] **Step 3: 验证本地/容器拓扑事实**

Run: `docker compose ps`
Expected: 成功列出当前 Compose 服务；如果服务未启动，也要把“未启动”作为事实写入文档

Run: `python main.py --help`
Expected: 正常打印 usage；当前 accepted behavior 是 exit code `1`

Run: `docker compose logs --tail 50 app gateway`
Expected: 日志命令可运行；如服务不存在或未启动，要如实写入文档，不要假装通过

- [ ] **Step 4: 验证代码生效路径**

Run: `docker compose ps -q app`
Expected: 输出 app 容器 ID；若无输出，文档中记录为 blocked

Run: `docker inspect (docker compose ps -q app) --format "{{json .Mounts}}"`
Expected: 看清 app 是否使用 bind mount；如果不是 bind mount，则记录“代码更新需要 rebuild 而非 restart”

- [ ] **Step 5: 给出部署态结论**

必须明确回答：

- 当前部署拓扑是否与 `docs/QUICKSTART.md` 一致
- `app` 代码改动通过什么方式生效
- 当前是否已经有**不依赖真实外部 API**的部署态 smoke 路径
- 如果没有，该缺口是否阻塞下一轮 rerank 或 Phase 2

- [ ] **Step 6: 运行本任务回归**

Run: `python main.py --help`
Expected: usage 正常打印

- [ ] **Step 7: Commit**

```bash
git add docs/acceptance/2026-04-03-deployment-runtime-validation.md
git commit -m "docs: capture deployment runtime validation state"
```

### Task 2: `tests/test_process_pipeline.py` 10 个失败测试去向梳理

**Files:**
- Create: `docs/acceptance/2026-04-03-process-pipeline-failure-triage.md`
- Read: `docs/acceptance/2026-04-03-test-baseline.md`
- Read: `tests/test_process_pipeline.py`

- [ ] **Step 1: 先写 triage 文档骨架**

文档至少包含以下小节：

- 当前基线命令与结果
- 10 个失败用例清单
- 症状分组
- 去向分流（下一轮修 / 可接受历史债 / 需要产品决定）
- 推荐的下一小轮动作

- [ ] **Step 2: 重跑当前失败基线**

Run: `pytest tests/test_process_pipeline.py --tb=no -q`
Expected: 结果仍为 accepted baseline，或如实记录变化

Run: `pytest tests/test_process_pipeline.py --tb=short -q`
Expected: 拿到精简错误信息，足够支持 triage 分流

- [ ] **Step 3: 将 10 个失败按“处理方式”而不是仅按症状分组**

至少分成以下三类：

- A. 明显像过时断言或命名漂移，适合下一小轮直接修
- B. 行为差异真实存在，但需要先确认期望语义
- C. 不适合在当前 Sprint 处理，应继续保留为 accepted baseline

每个失败用例都必须有：

- 当前症状
- 初步判断
- 建议去向
- 是否需要产品/维护者决定

- [ ] **Step 4: 给出“是否值得开一个 1-2 天的小修 Sprint”结论**

必须明确回答：

- 10 个失败里哪些适合集中修复
- 哪些不适合现在动
- 是否建议在 rerank / 动态音色库之前，先开一个小 sprint 清理这些失败

- [ ] **Step 5: 运行本任务回归**

Run: `pytest tests/test_process_pipeline.py --tb=no -q`
Expected: 本任务只写文档，不应改变基线结果

- [ ] **Step 6: Commit**

```bash
git add docs/acceptance/2026-04-03-process-pipeline-failure-triage.md
git commit -m "docs: triage current process pipeline baseline failures"
```

### Task 3: VolcEngine rerank readiness review

**Files:**
- Create: `docs/acceptance/2026-04-03-volcengine-rerank-readiness-review.md`
- Read: `docs/specs/2026-04-02-dynamic-voice-library-plan.md`
- Read: `docs/handover/2026-04-02-session-handover.md`
- Read: `src/services/tts/volcengine_voice_selector.py`
- Read: `src/services/tts/volcengine_voice_profile_data.json`

- [ ] **Step 1: 先写 readiness review 文档骨架**

文档至少包含以下小节：

- 审计边界
- 已提交事实 vs 当前工作区本地事实
- 数据 readiness
- 代码 readiness
- 运行时风险
- 结论：`GO` / `NO-GO`

- [ ] **Step 2: 先确认 rerank 相关文件属于什么状态**

Run: `git status --short -- src/services/tts/volcengine_voice_selector.py src/services/tts/volcengine_voice_profile_data.json gateway/voice_catalog_models.py gateway/voice_catalog_service.py`
Expected: 明确这些文件是已提交、未提交、还是仅存在于当前脏工作区；文档必须区分“仓库基线事实”和“工作区本地事实”

- [ ] **Step 3: 汇总当前已知的标签和 profile 证据**

Run: `Select-String -Path docs/specs/2026-04-02-dynamic-voice-library-plan.md -Pattern "final 标签","VolcEngine rerank","voice_labels","audio_round1"`
Expected: 收集计划文档中的数据 readiness 证据

Run: `Select-String -Path docs/handover/2026-04-02-session-handover.md -Pattern "voice_labels","VolcEngine rerank","final","359","418"`
Expected: 收集 handover 文档中的补充事实，并与 spec 做交叉核对

- [ ] **Step 4: 检查代码 readiness**

必须回答：

- `_try_rerank_with_profiles()` 是否仍为 no-op
- 当前运行时是否已经有可读的 final label 数据来源
- rerank 若激活，会依赖哪些尚未稳定的路径

如需要，可运行：

Run: `Select-String -Path src/services/tts/volcengine_voice_selector.py -Pattern "_try_rerank_with_profiles","final","score","profile"`
Expected: 收集代码层面的 readiness 证据

- [ ] **Step 5: 明确给出 GO / NO-GO**

结论必须明确回答：

- 是否建议下一轮直接激活 rerank
- 如果 `NO-GO`，最小阻塞项是什么
- 如果 `GO`，下一轮最小实现切片是什么

本任务**不允许**把“值得做”写成“已经 ready”。

- [ ] **Step 6: 运行本任务只读验证**

Run: `Select-String -Path docs/specs/2026-04-02-dynamic-voice-library-plan.md -Pattern "VolcEngine rerank","blocked-by"`
Expected: 能看到 rerank 仍被标记为 follow-up item，而非当前已启动功能

- [ ] **Step 7: Commit**

```bash
git add docs/acceptance/2026-04-03-volcengine-rerank-readiness-review.md
git commit -m "docs: review VolcEngine rerank readiness"
```

---

## 5. 推荐执行顺序

1. Task 1: 部署态验证
2. Task 2: 10 个失败测试去向梳理
3. Task 3: VolcEngine rerank readiness review

原因：

- 先拿到部署态事实，避免后面把“理论上 ready”误写成“部署上已 ready”
- 再把 10 个失败测试从“悬着的基线”转成“有去向的队列”
- 最后再判断 rerank 是否适合开下一个实现 Sprint

---

## 6. Sprint 验收清单

- [ ] `docs/acceptance/2026-04-03-deployment-runtime-validation.md` 已写入容器拓扑、代码生效路径、部署态结论
- [ ] `docs/acceptance/2026-04-03-process-pipeline-failure-triage.md` 已写入 10 个失败测试的去向分流
- [ ] `docs/acceptance/2026-04-03-volcengine-rerank-readiness-review.md` 已写入明确的 `GO` / `NO-GO`
- [ ] 本 Sprint 未修改任何生产代码
- [ ] 本 Sprint 未修改任何测试代码
- [ ] 本 Sprint 未引入真实外部 API 调用

---

## 7. 完成后的决策出口

执行完这个短 Sprint 后，只允许进入以下三种之一：

1. **Deployment blocked**
说明部署态验证表明还缺少安全的 smoke 路径或代码部署方式不稳定，先补部署/验证能力。

2. **Test-debt cleanup first**
说明 10 个失败测试里有一批低成本、应优先修掉的断言/语义漂移，先开小修 Sprint。

3. **Rerank implementation next**
说明部署态和失败测试都不再阻塞，且 rerank readiness review 给出 `GO`，则下一轮开 VolcEngine rerank 激活计划。
