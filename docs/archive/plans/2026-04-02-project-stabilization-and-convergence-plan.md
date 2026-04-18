# AIVideoTrans 稳定性与收敛实施方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 通过先建立可复用的测试基线，再补 per-job TTS 路由运行时证据、前置根目录 Python 依赖可复现化、按审计结果最小推进 `process -> workflow` 收敛、归档过期文档并新增协作入口，降低当前代码库的最高工程风险。

**Architecture:** 保留 `src/pipeline/process.py` 作为兼容壳，但不再把它当作第二套架构中心。本修订版遵循“先建立基线，再证明，再声明”的顺序：Task 1 不再追求真实外部依赖的端到端 smoke，而是用 unit + mocked runtime + runner log capture 三层证据证明 TTS 路由；Task 3 改为 audit-first，只在确认确有剩余 canonical-shape 重复逻辑时做最小收敛。

**Tech Stack:** Python 3.12、FastAPI gateway、自定义 Job API、Next.js 16、PostgreSQL 16、Docker Compose、pytest

---

## 1. 范围与成功标准

本方案聚焦 5 条高优先级工作流：

1. 建立当前测试基线
2. 运行时 TTS 路由加固与证据化
3. 根目录 Python 依赖可复现化
4. `process -> workflow` 审计后最小收敛
5. 文档真相刷新与旧文档归档

本方案**不包含**：

- 完整 publish 能力补完
- VolcEngine rerank 正式投产
- 动态音色库第 2 期 CRUD / 导入 / verify
- 直接硬切替换 `process`
- 在测试中引入真实外部 API、网络依赖或生产服务调用

### 成功标准

- 截至 **2026-04-03** 的测试基线被记录为文档事实，后续每个 Task 都以“不新增失败”为最低验收门槛。
- free / express 与 paid / studio 的 provider 选择，能被三层证据证明：
  - 纯 provider 决策单测
  - mocked `ProcessPipeline` 运行时证据测试
  - runner / job events 日志透传测试
- 根目录 Python app 可以从已提交的依赖清单安装，而不是只依赖 `Dockerfile` 内联 `pip install`。
- `process -> workflow` 收敛只在审计确认仍有值得抽离的 canonical-shape 重复逻辑时推进；若剩余量很小，则任务可以以“补回归测试 + 写审计结论”收束。
- 新协作者可以从 `docs/QUICKSTART.md` 进入当前真实架构，而不是先被过期 frozen 文档误导。

---

## 2. 当前事实快照（截至 2026-04-03）

### 2.1 已确认的代码事实

- `process_runner` 已经传递 `--job-id`：`src/services/jobs/process_runner.py`
- `main.py` 已经解析 `--job-id`：`main.py`
- `ProcessPipeline` 已经会根据 `job_id` 回读 job snapshot：`src/pipeline/process.py`
- `TTSGenerator` 在有 job record 时已经优先使用 per-job provider：`src/services/tts/tts_generator.py`
- `process` 已经通过 `ProjectBuilder` 构建 `WorkflowBuildResult`，并经由 `OutputDispatcher` 出产物：`src/pipeline/process.py`
- VolcEngine rerank 仍然是占位 no-op：`src/services/tts/volcengine_voice_selector.py`
- 根目录 Python 依赖管理仍然主要存在于 `Dockerfile`：`Dockerfile`
- 当前 Web 主线前端是 `frontend-next`，不是旧的 `frontend`

### 2.2 已确认的测试事实

以下结果均为 **2026-04-03** 在当前工作区实际执行所得：

- `pytest tests/test_tts_routing_invariants.py -q`
  - 当前结果：PASS（`11 passed`）
- `pytest tests/test_project_builder.py tests/test_project_shape_helpers.py tests/test_output_dispatcher.py -q`
  - 当前结果：PASS（`17 passed`）
- `pytest tests/test_process_pipeline.py -q`
  - 当前结果：FAIL（`10 failed, 63 passed`）

这意味着：

- “`tests/test_process_pipeline.py` 仍有 15 个既有失败”是历史文档事实，不应再被当成当前精确基线。
- 当前最先需要的是**建立新的测试基线文档**，而不是直接按旧文档数字继续排期。

### 2.3 需要明确降级为“历史背景”的文档

- `docs/CURRENT_PROJECT_STATUS.md`
- `docs/FREE_VS_PAID_TTS_ROUTING_STATUS_AND_RECOVERY_PLAN.md`
- `docs/COMMERCIALIZATION_HANDOVER_2026-03-30.md`

这些文档仍有价值，但不能再无条件代表当前 HEAD。

---

## 3. 文件分工

### 3.1 Task 0：测试基线

- Create: `docs/acceptance/2026-04-03-test-baseline.md`

### 3.2 Task 1：TTS 路由运行时证据化

- Modify: `src/services/tts/tts_generator.py`
- Modify: `src/pipeline/process.py`
- Modify: `src/services/jobs/process_runner.py`
- Modify: `tests/test_tts_routing_invariants.py`
- Create: `tests/test_tts_runtime_evidence.py`
- Modify: `tests/test_process_runner.py`

### 3.3 Task 2：根目录 Python 依赖可复现化

- Create: `pyproject.toml`
- Create: `requirements-dev.txt`
- Create: `tests/test_environment_manifest.py`
- Modify: `Dockerfile`
- Modify: `README.md`

### 3.4 Task 3：Process To Workflow 审计后最小收敛

- Create: `docs/acceptance/2026-04-03-process-convergence-audit.md`
- Create: `tests/test_process_workflow_convergence.py`
- Modify: `src/pipeline/process.py`
- Modify: `src/modules/workflow/project_builder.py`
- Modify: `src/modules/workflow/project_shape_helpers.py`
- Modify: `tests/test_output_dispatcher.py`

### 3.5 Task 4：文档真相刷新与旧文档归档

- Create: `docs/QUICKSTART.md`
- Create: `docs/archive/2026-03-30-free-vs-paid-tts-routing-status-and-recovery-plan.md`
- Modify: `docs/FREE_VS_PAID_TTS_ROUTING_STATUS_AND_RECOVERY_PLAN.md`
- Modify: `docs/CURRENT_PROJECT_STATUS.md`
- Modify: `docs/COMMERCIALIZATION_HANDOVER_2026-03-30.md`
- Modify: `README.md`

### 3.6 Task 5：仅整理后续队列

- Modify: `docs/specs/2026-04-02-dynamic-voice-library-plan.md`
- Modify: `docs/plans/2026-04-02-project-stabilization-and-convergence-plan.md`

---

## 4. 交付顺序

### Phase 0

先建立测试基线，统一后续验收口径。

### Phase 1

把 TTS 路由变成“在 mocked 运行时中可证明的不变量”。

### Phase 2

补齐根目录 Python 依赖可复现化。

### Phase 3

先审计，再决定 `process -> workflow` 是否需要继续抽离 canonical-shape 逻辑。

### Phase 4

归档过期文档，建立新的协作入口和当前真相文档。

每个 Phase 都必须可以独立落地，并在进入下一个 Phase 前完成对应的定向验证。

---

## 5. 任务实施计划

### Task 0: 建立当前测试基线

**Files:**
- Create: `docs/acceptance/2026-04-03-test-baseline.md`

- [ ] **Step 1: 跑当前最关键的基线测试组合**

Run: `pytest tests/test_process_pipeline.py -q`
Expected: 当前基线为 `10 failed, 63 passed`；如果 HEAD 已变化，则以实际输出为准写入基线文档

Run: `pytest tests/test_tts_routing_invariants.py tests/test_project_builder.py tests/test_project_shape_helpers.py tests/test_output_dispatcher.py -q`
Expected: 当前基线为 PASS；如果 HEAD 已变化，则同步记录

- [ ] **Step 2: 把基线结果写入文档**

文档必须包含：

- 执行日期：`2026-04-03` 或实际执行日期
- 精确命令
- 精确通过 / 失败数量
- 当前失败集中在哪一类行为
- 后续验收规则：`不新增失败；已有失败数量只允许下降，不允许上升`

- [ ] **Step 3: 不默认引入 `xfail`**

本任务默认只记录基线，不把现有失败立即改成 `xfail`。

只有在满足以下条件时，才单独开后续小任务处理 `xfail`：

- 历史失败已被产品 / 维护者明确接受
- 这些失败确实会阻塞后续 Task 的验收判断
- `xfail` 变更能用单独 PR / commit 与功能改动分离

- [ ] **Step 4: Commit**

```bash
git add docs/acceptance/2026-04-03-test-baseline.md
git commit -m "docs: capture 2026-04-03 test baseline"
```

### Task 1: 把 TTS 路由变成“mocked 运行时可证明的不变量”

**Files:**
- Modify: `src/services/tts/tts_generator.py`
- Modify: `src/pipeline/process.py`
- Modify: `src/services/jobs/process_runner.py`
- Modify: `tests/test_tts_routing_invariants.py`
- Create: `tests/test_tts_runtime_evidence.py`
- Modify: `tests/test_process_runner.py`

- [ ] **Step 1: 先写 provider 决策单测**

```python
def test_tts_generator_prefers_per_job_provider(monkeypatch):
    monkeypatch.setattr("services.tts.tts_generator.get_tts_provider", lambda: "minimax")
    monkeypatch.setattr(
        "services.tts.tts_generator.get_tts_provider_for_job",
        lambda job: job["tts_provider"],
    )
    generator = TTSGenerator(TTSConfig(api_key="x"), job_record={"tts_provider": "cosyvoice"})
    decision = generator._resolve_provider_decision(job_record=None)
    assert decision["provider"] == "cosyvoice"
    assert decision["source"] == "job_record"
```

- [ ] **Step 2: 再写 mocked pipeline 运行时证据测试**

```python
def test_process_pipeline_emits_runtime_tts_provider_evidence(tmp_path, monkeypatch, capsys):
    _install_single_speaker_pipeline_mocks(monkeypatch)
    ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=tts-proof",
            voice_a="voice_demo_001",
            project_dir=str(tmp_path / "project"),
            job_record={"service_mode": "express", "tts_provider": "cosyvoice"},
        )
    )
    captured = capsys.readouterr()
    assert "[S4] TTS provider: cosyvoice" in captured.out
```

- [ ] **Step 3: 再写 runner / logs 透传测试**

```python
def test_process_runner_persists_s4_provider_log(tmp_path):
    runner = _make_runner(tmp_path)
    job = _make_job()
    fake_plan = {"lines": ["[S4] TTS provider: cosyvoice"], "returncode": 0}
    ...
    assert any("[S4] TTS provider: cosyvoice" in line for line in stored_logs)
```

- [ ] **Step 4: 运行新增测试，确认缺口**

Run: `pytest tests/test_tts_runtime_evidence.py tests/test_process_runner.py -q`
Expected: 初次运行 FAIL，或虽然 PASS 但证据粒度不足，需要补稳定决策接口

- [ ] **Step 5: 在代码中补稳定的证据面**

实现目标：

- 在 `src/services/tts/tts_generator.py` 中，把 provider 决策提炼为可单测的小函数 / 小方法，例如 `_resolve_provider_decision()`
- `generate_all()` 继续打印 `[S4] TTS provider: ...`，但同时带上稳定的 `source` 语义，例如 `job_record` / `global_default`
- 在 `src/pipeline/process.py` 中，对 `job_id` 已传入但 snapshot 无法加载的情况输出受控错误，而不是静默继续
- 在 `src/services/jobs/process_runner.py` 中，确保 `[S4]` 日志能稳定进入 job events / logs

- [ ] **Step 6: 跑 Task 1 定向回归**

Run: `pytest tests/test_tts_routing_invariants.py tests/test_tts_runtime_evidence.py tests/test_process_runner.py -q`
Expected: PASS

- [ ] **Step 7: 用现有 pipeline 基线做回归对照**

Run: `pytest tests/test_process_pipeline.py -q`
Expected: 与 Task 0 基线相比不新增失败；失败数量只能持平或减少

- [ ] **Step 8: Commit**

```bash
git add src/services/tts/tts_generator.py src/pipeline/process.py src/services/jobs/process_runner.py tests/test_tts_routing_invariants.py tests/test_tts_runtime_evidence.py tests/test_process_runner.py
git commit -m "test: harden runtime tts routing evidence"
```

### Task 2: 补齐根目录 Python 依赖可复现化

**Files:**
- Create: `pyproject.toml`
- Create: `requirements-dev.txt`
- Create: `tests/test_environment_manifest.py`
- Modify: `Dockerfile`
- Modify: `README.md`

- [ ] **Step 1: 先写失败的环境 manifest 检查**

```python
def test_root_python_manifest_exists():
    assert Path("pyproject.toml").exists()


def test_dockerfile_installs_from_committed_manifest():
    text = Path("Dockerfile").read_text(encoding="utf-8")
    assert "pip install --no-cache-dir \\" not in text
```

- [ ] **Step 2: 运行检查**

Run: `pytest tests/test_environment_manifest.py -q`
Expected: FAIL，因为当前根目录 manifest 不存在，`Dockerfile` 仍使用内联依赖

- [ ] **Step 3: 增加最小可用的根目录依赖清单**

实现目标：

- 在 `pyproject.toml` 中声明 Python `>=3.12,<3.13`
- 把当前根目录 app 的 runtime 依赖从 `Dockerfile` 迁回清单
- `gateway` 依赖继续独立管理，不强行做 monorepo Python packaging 合并
- 提供 `requirements-dev.txt` 作为团队仍使用 `pip -r` 时的兼容入口

- [ ] **Step 4: 改造 Dockerfile，从已提交清单安装**

实现目标：

- 替换当前内联的 `pip install assemblyai ...`
- 保留 apt 包安装与 Deno 安装逻辑
- 让 Docker 构建不再隐藏真实 Python 依赖集合

- [ ] **Step 5: 跑环境与 CLI 回归**

Run: `pytest tests/test_environment_manifest.py tests/test_main_cli.py -q`
Expected: PASS

Run: `python main.py --help`
Expected: 正常打印 usage

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml requirements-dev.txt tests/test_environment_manifest.py Dockerfile README.md
git commit -m "build: add reproducible root python dependency manifests"
```

### Task 3: 先审计，再决定是否继续推进 Process To Workflow 收敛

**Files:**
- Create: `docs/acceptance/2026-04-03-process-convergence-audit.md`
- Create: `tests/test_process_workflow_convergence.py`
- Modify: `src/pipeline/process.py`
- Modify: `src/modules/workflow/project_builder.py`
- Modify: `src/modules/workflow/project_shape_helpers.py`
- Modify: `tests/test_output_dispatcher.py`

- [ ] **Step 1: 先做审计，不先假设一定需要重构**

审计必须回答三个问题：

1. `process.py` 里还有哪些 canonical-shape 逻辑绕过了 shared helpers
2. 这些逻辑是否真的值得抽离，而不是 process-only compatibility state
3. 如果剩余量很小，是否应该只补测试而不是继续做结构性迁移

输出到：`docs/acceptance/2026-04-03-process-convergence-audit.md`

- [ ] **Step 2: 给审计结论设硬门槛**

只有在满足以下条件时，才进入生产代码收敛：

- 剩余逻辑属于 canonical shape，而不是 process-only runtime fact interpretation
- 这部分逻辑已经在 shared helper 中有明显近邻
- 能用一个小切片完成，不触碰 review-gate 主流程

若不满足，则本 Task 以“审计文档 + 回归测试”结束，不强行重构。

- [ ] **Step 3: 若门槛满足，先写失败的收敛回归测试**

```python
def test_process_reuses_shared_source_info_builder(...):
    build_result = build_process_build_result(...)
    assert build_result.localized_project.source_info["source_kind"] == "youtube_url"


def test_process_artifact_entries_still_flow_through_shared_builder(...):
    build_result = build_process_build_result(...)
    assert build_result.artifact_index.require("source.original_audio")
```

- [ ] **Step 4: 仅抽离一个最小边界**

优先顺序：

1. `stage_outputs` 的 canonical shape
2. artifact-entry family 的补齐规则
3. `source_info` 的小型规范化重复

不要在本任务里移动：

- `speaker_review`
- `translation_review`
- `voice_review`
- download/cache reuse
- process-only runtime recovery

- [ ] **Step 5: 跑收敛相关回归**

Run: `pytest tests/test_process_workflow_convergence.py tests/test_project_builder.py tests/test_project_shape_helpers.py tests/test_output_dispatcher.py -q`
Expected: PASS

Run: `pytest tests/test_process_pipeline.py -q`
Expected: 与 Task 0 基线相比不新增失败；失败数量只能持平或减少

- [ ] **Step 6: Commit**

```bash
git add docs/acceptance/2026-04-03-process-convergence-audit.md tests/test_process_workflow_convergence.py src/pipeline/process.py src/modules/workflow/project_builder.py src/modules/workflow/project_shape_helpers.py tests/test_output_dispatcher.py
git commit -m "refactor: audit and reduce remaining process-owned canonical shape logic"
```

### Task 4: 归档过期文档，建立新的协作入口

**Files:**
- Create: `docs/QUICKSTART.md`
- Create: `docs/archive/2026-03-30-free-vs-paid-tts-routing-status-and-recovery-plan.md`
- Modify: `docs/FREE_VS_PAID_TTS_ROUTING_STATUS_AND_RECOVERY_PLAN.md`
- Modify: `docs/CURRENT_PROJECT_STATUS.md`
- Modify: `docs/COMMERCIALIZATION_HANDOVER_2026-03-30.md`
- Modify: `README.md`

- [ ] **Step 1: 先建立新的“当前入口”**

`docs/QUICKSTART.md` 必须回答：

- 当前主后端拓扑是什么
- 当前前端主线是哪一套
- 新协作者应该先读哪几份文档
- 哪些文档是历史背景，不应直接当成当前事实

- [ ] **Step 2: 归档明显过时的路由恢复文档**

处理方式：

- 把当前 `docs/FREE_VS_PAID_TTS_ROUTING_STATUS_AND_RECOVERY_PLAN.md` 的完整旧内容迁入 `docs/archive/2026-03-30-free-vs-paid-tts-routing-status-and-recovery-plan.md`
- 原路径保留为短版状态说明，明确：
  - `--job-id` 代码链路已恢复
  - 旧文档内容属于历史调查记录
  - 当前未完成项是“运行时验证与部署态确认”，不是“重新修代码链路”

- [ ] **Step 3: 把其它历史文档改成“带边界的背景资料”**

更新目标：

- `docs/CURRENT_PROJECT_STATUS.md`
  - 明确它是 frozen baseline / 历史快照
  - 链接到 `docs/QUICKSTART.md`
- `docs/COMMERCIALIZATION_HANDOVER_2026-03-30.md`
  - 明确哪些仍是当前业务真相，哪些只是阶段性交接背景
- `README.md`
  - 当前运行入口
  - 当前前端主线
  - 根目录 Python 依赖安装方式

- [ ] **Step 4: 跑文档相关回归**

Run: `python main.py --help`
Expected: 正常打印 usage

Run: `pytest tests/test_main_cli.py tests/test_job_read_surface.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add docs/QUICKSTART.md docs/archive/2026-03-30-free-vs-paid-tts-routing-status-and-recovery-plan.md docs/FREE_VS_PAID_TTS_ROUTING_STATUS_AND_RECOVERY_PLAN.md docs/CURRENT_PROJECT_STATUS.md docs/COMMERCIALIZATION_HANDOVER_2026-03-30.md README.md
git commit -m "docs: archive stale status docs and add current quickstart"
```

### Task 5: 只整理后续队列，不在本轮实现

**Files:**
- Modify: `docs/specs/2026-04-02-dynamic-voice-library-plan.md`
- Modify: `docs/plans/2026-04-02-project-stabilization-and-convergence-plan.md`

- [ ] **Step 1: 给动态音色库 spec 增加 blocked-by 说明**

需要补充：

- VolcEngine rerank 应等待 stabilization / convergence 稳定之后再推进
- 在 provider 管理面继续扩张之前，必须先有运行时路由证据与环境可复现性

- [ ] **Step 2: 给本计划补充 follow-up queue 小节**

队列包括：

- VolcEngine rerank 激活
- 动态音色库第 2 期 CRUD / import / verify
- publish 能力扩展

- [ ] **Step 3: Commit**

```bash
git add docs/specs/2026-04-02-dynamic-voice-library-plan.md docs/plans/2026-04-02-project-stabilization-and-convergence-plan.md
git commit -m "docs: align next-queue priorities with stabilized roadmap"
```

---

## 6. 验收清单

### Phase 0 验收

- [ ] `docs/acceptance/2026-04-03-test-baseline.md` 已写入精确命令与结果
- [ ] 后续 Task 统一采用“相对 Task 0 不新增失败”的口径

### Phase 1 验收

- [ ] `tests/test_tts_routing_invariants.py` 通过
- [ ] `tests/test_tts_runtime_evidence.py` 通过
- [ ] `tests/test_process_runner.py` 中新增日志透传断言通过
- [ ] free / express 运行时证据显示 `cosyvoice`
- [ ] paid / studio 运行时证据显示 snapshot-selected provider

### Phase 2 验收

- [ ] 根目录 app 可以从已提交的依赖清单安装
- [ ] `Dockerfile` 不再隐藏真实 Python 依赖集合

### Phase 3 验收

- [ ] `docs/acceptance/2026-04-03-process-convergence-audit.md` 明确列出仍存与不存的重复逻辑
- [ ] 若进入生产代码改动，则至少一个值得迁移的 canonical-shape 边界被共享化
- [ ] `process` 仍可作为 CLI / operator shell 使用

### Phase 4 验收

- [ ] 主状态文档不再把已恢复的 `--job-id` 代码链路继续描述为现行 bug
- [ ] 新协作者可以从 `docs/QUICKSTART.md` 找到正确阅读顺序
- [ ] 旧 TTS 路由恢复文档已被归档并降级为历史调查记录

---

## 7. 风险与缓解

### 风险：没有测试基线，后续验收标准漂移

缓解：

- 先执行 Task 0
- 每个后续 Task 都与 Task 0 基线比较
- 不把历史文档里的失败数量直接当作当前真相

### 风险：把“真实外部依赖端到端 smoke”误当成唯一证明方式

缓解：

- Task 1 明确采用三层证据：
  - unit
  - mocked pipeline runtime
  - runner log capture
- 全程遵守 Sprint 1 的 mocks / stubs 约束

### 风险：在收敛 process 时重构一个其实已经基本收敛的边界

缓解：

- Task 3 先审计，后决定是否改生产代码
- 若剩余量很小，就以文档 + 回归测试收束，不强行重构

### 风险：Docker 与本地环境的依赖清单继续漂移

缓解：

- 让 Docker 从同一份已提交 manifest 安装
- `gateway` 依赖继续独立管理，避免本轮顺手扩 scope

### 风险：过期文档继续把协作者带回错误结论

缓解：

- 不只“刷新”旧文档，而是归档明显过时的调查记录
- 新写 `docs/QUICKSTART.md` 作为第一入口

---

## 8. 推荐执行顺序

1. Task 0
2. Task 1
3. Task 2
4. Task 3
5. Task 4
6. Task 5

Task 0-4 已于 2026-04-03 完成。Task 5 是队列整理，不是新功能实现。

---

## 9. Follow-up Queue（2026-04-03 追加）

以下后续工作已识别但**不在本轮稳定性计划中实施**。按建议优先级排序：

### 优先级 1：VolcEngine rerank 激活

- **前置条件**：稳定性计划已完成（runtime routing evidence + reproducible environment 已就绪）；voice_labels 表中需有足够 final 标签
- **范围**：将 `volcengine_voice_selector.py:_try_rerank_with_profiles()` 从占位 no-op 改为从 DB 读取 final profile 并执行 4 维度评分
- **风险**：中等。首次激活 rerank 可能改变现有匹配结果，需要对比验证
- **状态**：未启动

### 优先级 2：动态音色库 Phase 2 — CRUD / import / verify

- **前置条件**：Phase 1 schema + seed 已在生产环境部署并验证；稳定性计划已完成
- **范围**：管理员可新增/编辑/软删除音色，可批量导入（CSV + 粘贴），导入后自动 verify
- **风险**：中等（涉及 TTS API 调用）
- **详细计划**：见 `docs/specs/2026-04-02-dynamic-voice-library-plan.md` §4 第 2 期
- **状态**：未启动

### 优先级 3：publish 能力扩展

- **前置条件**：process → workflow 收敛稳定；当前 minimal publish 已验证可用
- **范围**：字幕烧录、原音混合、更丰富的发布控制
- **风险**：较高（涉及 ffmpeg pipeline 复杂度）
- **状态**：未启动，无详细计划
