# 代码质量、效率与规范优化方案（2026-06-24）

状态：可实施方案  
适用范围：Python 工作流、Job API、Gateway、Next 前端、测试与 CI  
结论类型：基于本仓库当前代码、图谱文档、计划文档、历史审计和守卫测试的综合评估

## 1. 执行摘要

本项目已经不是“缺少测试和规范”的早期代码库，而是一个在快速商业化和多条产品线并行推进中形成的大型系统。当前最主要的质量风险不是单个 P0 漏洞，而是：

- 核心模块体积过大，真实接口不够窄，导致修改成本高、回归面难估计。
- 多条演进线同时存在，`process.py`、Job API、Gateway、Next 前端都保留了兼容壳和过渡代码，需要继续按阶段收敛，而不是重写。
- 守卫测试很强，但质量门禁还缺少统一的分层、标记、覆盖率和静态分析预算。
- 前端和后端都已有好的局部抽象，但热点页面和热点路由仍承担过多职责。
- 性能优化目前应优先做可观测和基准化，而不是提前改算法；尤其不能破坏 DSP-first 对齐、数学 retiming、SemanticBlock TTS 单元等架构不变量。

推荐策略：

1. **先建立可度量质量基线**：Ruff/类型检查/pytest markers/前端 lint+typecheck/性能基准脚本。
2. **再做低风险标准化**：统一 JSON 原子写、文本归一化、数值 coercion、错误载荷、导入规则和 API 客户端解析。
3. **随后按真实接缝拆热点模块**：优先拆 Gateway `job_intercept.py`、Next 编辑页、语音选择页、Job API dispatch、`JobService` post-edit 逻辑。
4. **最后推进工作流收敛**：延续既定 Option B，让 `process.py` 逐步消费 `ProjectWorkflow.run_build()` 和 `OutputDispatcher`，最终成为兼容入口而不是第二套架构。

本方案不建议大爆炸重写，不建议把 Gateway 的商业事实下沉到前端，不建议把确定性 retiming/对齐迁移到 LLM，不建议在默认测试路径引入真实外部服务。

## 2. 已阅读与抽样依据

### 2.1 项目级文档

- `AGENTS.md` 与本会话项目指令。
- `README.md`。
- `DESIGN.md`。
- `docs/graphs/README.md`。
- `docs/graphs/GITNEXUS_PROJECT_GRAPH.md` 及相关图谱索引。
- `docs/plans/README.md`。
- `docs/architecture/PROCESS_WORKFLOW_CONVERGENCE.md`。
- `docs/audits/2026-05-07-comprehensive-codebase-audit.md`。
- `docs/audits/2026-05-21-risk-remediation-and-re-audit-report.md`。
- `.github/workflows/ci.yml`。

### 2.2 当前代码与配置抽样

- Python 包配置：`pyproject.toml`、`requirements-dev.txt`。
- Gateway 依赖与入口：`gateway/requirements.txt`、`gateway/database.py`、`gateway/job_intercept.py`、`gateway/models.py`、`gateway/billing.py`、`gateway/credits_service.py`。
- 核心工作流：`src/pipeline/process.py`、`src/workflow/project_workflow.py`、`src/workflow/output_dispatcher.py`。
- Job 层：`src/services/jobs/api.py`、`src/services/jobs/service.py`、`src/services/jobs/store.py`、`src/services/jobs/editing_segments.py`。
- 对齐/翻译/TTS：`src/services/alignment/aligner.py`、`src/services/gemini/translator.py`、`src/services/tts/tts_generator.py`。
- 工具与并发：`src/utils/atomic_io.py`、`src/services/_file_lock.py`。
- 前端：`frontend-next/package.json`、`frontend-next/tsconfig.json`、`frontend-next/eslint.config.mjs`、`frontend-next/src/lib/api/client.ts`、`frontend-next/src/hooks/usePollingTask.ts`、`frontend-next/src/hooks/useBackgroundTask.ts`、大型页面与 API 客户端文件。
- 测试守卫：`tests/test_legacy_cleanup_guards.py`、`tests/test_phase1_guards.py`、`tests/test_phase4_1_f_lockdown_guards.py`、`tests/test_status_vocab_in_sync.py`、`tests/test_admin_gate_coverage.py`。

### 2.3 代码规模快照

以下数字为本次本地扫描快照，主要用于判断治理优先级，不作为精确项目统计口径：

| 类型 | 文件数 | 约行数 | 说明 |
| --- | ---: | ---: | --- |
| Python | 约 1090 | 约 345,813 | 包含测试；核心业务与守卫测试都很大 |
| Markdown | 约 170 | 约 66,709 | 文档体系丰富，历史计划较多 |
| TSX | 约 161 | 约 44,988 | 前端页面与管理端热点明显 |
| TypeScript | 约 63 | 约 8,910 | API 客户端和共享类型逐步增多 |
| 测试文件 | 约 558 个 `tests/*.py` | - | 已有大量行为、守卫、集成测试 |

主要热点文件：

| 文件 | 规模/特征 | 风险判断 |
| --- | ---: | --- |
| `src/pipeline/process.py` | 约 12k+ 行，200+ defs/classes | 兼容壳仍承担第二架构风险，需按既定收敛路线拆分 |
| `gateway/job_intercept.py` | 约 6k+ 行，100+ defs | 路由、商业、同步、下载、post-edit 策略混杂，局部性不足 |
| `tests/test_process_pipeline.py` | 约 6k+ 行，100+ tests | 覆盖价值高，但回归定位成本高 |
| `src/services/transcript_reviewer.py` | 约 3k+ 行 | 审核、重写、状态决策复杂，适合抽窄接口 |
| `src/services/gemini/translator.py` | 约 2.5k+ 行 | Provider、重试、分块、策略混合 |
| `src/services/jobs/api.py` | 约 2.5k+ 行 | stdlib HTTP handler 过胖，dispatch 不够清晰 |
| `src/services/jobs/service.py` | 约 1.7k+ 行 | post-edit 与 job lifecycle 交织 |
| `frontend-next/src/app/(app)/workspace/[jobId]/edit/page.tsx` | 约 2k 行 | route shell 承担太多状态和交互 |
| `frontend-next/src/app/(app)/admin/settings/page.tsx` | 约 1.9k 行 | 管理设置页面可配置化不足 |
| `frontend-next/src/app/(app)/workspace/[jobId]/edit/VoiceModifyTab.tsx` | 约 1.5k 行 | 语音修改流程与选择逻辑需要复用抽取 |
| `frontend-next/src/components/workspace/TranslationForm.tsx` | 约 1.2k+ 行 | 表单状态、提交、展示逻辑应分层 |

## 3. 必须保持的架构不变量

后续所有优化都必须服从这些不变量：

- TTS 单元仍然是 `SemanticBlock`，不是字幕行。
- Alignment 仍然 DSP-first；rewrite loop 是 fallback，不是主路径。
- Subtitle retiming 仍然数学化、确定性，不迁移到 LLM。
- 主交付物仍然是 Jianying draft，而不是直接渲染 MP4。
- Gateway 是 plan catalog、trial rules、prices、entitlements 的事实源。
- 前端消费 Gateway 商业事实，不复制最终定价或权益逻辑。
- Auth、billing、payment 继续走增量迁移，不做大爆炸替换。
- 默认测试和本地路径优先 mock/stub/fake，不接入真实外部服务。
- `main.py` 与 `pytest` 必须保持在干净本地环境可运行。
- 面向营销、认证、支付的用户体验继续中文优先。

## 4. 当前优势

### 4.1 守卫测试文化已经建立

仓库已有多组高价值守卫：

- legacy cleanup / root projects / gateway hardcoded URL / frontend billing estimator 等结构性守卫。
- Phase 1 post-edit 模块结构、路径 parity、paid API 禁止误调用 TTS 的守卫。
- Mainland Worker、CosyVoice、secrets、gateway-worker 边界的跨子树守卫。
- Python/TS 状态词汇同步测试。
- Admin 路由权限覆盖测试。

这说明项目已经具备“把架构约束写成测试”的能力。下一步不是丢掉这些守卫，而是把守卫测试、契约测试、单元测试、集成测试、慢测试分层，降低 CI 噪声。

### 4.2 部分历史 P0 已修复

历史审计中若干高优问题已经有后续实现：

- `JobStore.update_job(mutator, ...)` 已存在，并使用文件锁和原子保存。
- `JobStore.list_jobs()` 已有 mtime cache 和 deepcopy 防变异。
- 前端已有 `usePollingTask` 与 `useBackgroundTask`，支持 hidden pause、visible refresh、in-flight guard、backoff、stalled hint 等。
- CI 已覆盖 backend guard、P0 remediation、Postgres integration、frontend lint/typecheck。
- CSRF 已形成当前阶段决策：SameSite=Lax + same-origin write guards，而不是立即切 strict。

因此后续方案应避免重复提出已完成事项，而要聚焦“完成迁移、收窄接口、统一工具、建立质量预算”。

### 4.3 文档和图谱体系可作为改造导航

`docs/graphs/README.md` 显示当前 GitNexus 图谱已覆盖 32k+ nodes、77k+ relations、300 flows，并按 APF、Smart Preview、Commercialization、Post-Edit、R2、Admin Ops 等子图组织。后续优化应让图谱承担“定位真实接缝”的职责，而不是靠人工猜测模块边界。

## 5. 主要问题与优化方向

## 5.1 模块深度不足：热点文件承担过多职责

### 现象

`process.py`、`gateway/job_intercept.py`、`src/services/jobs/api.py`、`JobService`、Next 编辑页、管理设置页和语音选择页都呈现相同模式：

- 文件内部有多个独立业务流。
- 函数和状态共享范围大。
- 新增行为时倾向于继续加分支。
- 测试常常只能覆盖大入口，难以精准定位回归。

### 原则

使用“深模块”策略：保留外部入口稳定，把内部复杂性藏到窄接口之后。每个新接口必须满足：

- 对调用方暴露少量参数和少量返回结构。
- 能独立测试。
- 不复制事实源。
- 不把过渡期兼容逻辑扩散到更多调用点。

### 推荐拆分顺序

| 优先级 | 目标 | 建议接缝 | 预期收益 |
| --- | --- | --- | --- |
| P0 | `gateway/job_intercept.py` | route family modules：job read、job create/admission、artifacts/download、voice review、post-edit policy、metering callbacks | 先降低 Gateway 修改风险，保持外部路由不变 |
| P0 | Next 编辑页 | route shell + hooks + panels：segments、bulk replace、commit flow、selection、job sync | 降低前端回归面，提升交互逻辑可测性 |
| P1 | `src/services/jobs/api.py` | dispatch table + resource handlers，先不替换 HTTP 框架 | 把 stdlib handler 从巨型 if/else 中解放 |
| P1 | `JobService` post-edit | `EditingApplicationModule` 或同等窄接口 | 让 post-edit 能单独演进和测试 |
| P1 | `process.py` | 延续 Option B：output convergence -> asset/build convergence -> review gate convergence | 消除第二架构风险 |
| P2 | `gateway/models.py` | 领域分段或受控拆文件，先保护 Alembic metadata | 降低模型文件认知负担 |

## 5.2 `process.py` 收敛路线

### 当前判断

`docs/architecture/PROCESS_WORKFLOW_CONVERGENCE.md` 已明确 Option B：保留 `src/pipeline/process.py` 作为兼容壳，让它逐步消费 `ProjectWorkflow.run_build()` 与 `OutputDispatcher`。这个方向是正确的，仍应继续执行。

### 不建议

- 不建议直接删除 `process.py`。
- 不建议一次性把全部流程改到 `ProjectWorkflow`。
- 不建议在迁移中改变 CLI 行为、输出目录结构或 Jianying draft 生成语义。

### 建议步骤

1. **冻结入口契约**
   - 对 `main.py --help`、主要 CLI 参数、典型输出目录、Jianying draft 生成结果补足 golden/contract 测试。
   - 把 `process.py` 仍然允许存在的职责写入 docstring 或架构文档：compatibility shell、legacy operator entry、old project adapter。

2. **先收敛输出路径**
   - 让 `process.py` 在生成输出时优先走 `OutputDispatcher`。
   - 保留旧路径 fallback，但用测试证明新旧输出兼容。
   - 完成标准：新增或更新测试覆盖 Jianying draft、字幕、音频、可选 review artifact 的输出路径。

3. **再收敛 build 阶段**
   - 将项目构建阶段迁移到 `ProjectWorkflow.run_build()`。
   - 把文件系统准备、manifest、asset resolve 封装成窄接口。
   - 完成标准：`process.py` 不再直接拼接核心 draft 输出结构。

4. **最后收敛 review gate**
   - 将 review gate 的状态转换和 artifact 选择抽成确定性模块。
   - 保证 rewrite fallback 仍是 fallback，不成为主路径。

5. **设置下降指标**
   - `process.py` 每完成一个切片，行数和直接调用的底层模块数应下降。
   - 新功能不再直接添加到 `process.py`，除非是兼容入口参数映射。

## 5.3 Gateway `job_intercept.py` 治理

### 当前风险

`gateway/job_intercept.py` 同时承担：

- Job list/get/create 代理与镜像。
- 商业试用、额度、支付后的状态反映。
- 下载、artifact、R2/本地资源转换。
- voice review 和 post-edit 策略。
- read-path mirror、settlement 保护与回滚。
- 多阶段兼容路由。

这些职责都合理存在，但不应该长期集中在单一文件。

### 建议目标结构

保持 `gateway/main.py` 和现有路由路径不变，内部拆为：

| 模块候选 | 职责 |
| --- | --- |
| `gateway/routes/jobs_read.py` | list/get 只读路由、DB/job-api 聚合、只读 guard |
| `gateway/routes/jobs_create.py` | create/admission、匿名/登录态适配、请求校验 |
| `gateway/routes/job_artifacts.py` | 下载、artifact metadata、R2/local URL 映射 |
| `gateway/routes/job_voice_review.py` | voice review 读取、提交、状态变化 |
| `gateway/routes/job_post_edit.py` | post-edit endpoint whitelist、policy、commit/update |
| `gateway/routes/job_metering_callbacks.py` | settlement/reconcile/callback，只允许写路径 |
| `gateway/job_projection.py` | Job API <-> Gateway DB projection，禁止 settlement 副作用 |

### 必须加的守卫

- read route 不得触发 settlement。
- list/get mirror 不得修改 payment/credit entitlement 事实。
- post-edit endpoint whitelist 继续保持与前端 path parity。
- Gateway 仍然是 plan/pricing/trial/entitlement 事实源。

### 迁移方式

1. 复制最小路由族到新模块。
2. `job_intercept.py` 只保留注册入口和兼容 import。
3. 每次只迁一组路由。
4. 迁移前后跑相同路由测试。
5. 文件缩小后再清理重复 helper。

完成标准：

- `job_intercept.py` 降到 4.5k 行以下。
- 每个 route family 有独立测试文件。
- `tests/test_admin_gate_coverage.py` 与 post-edit whitelist 守卫仍通过。

## 5.4 Job API 与 JobService 优化

### `src/services/jobs/api.py`

当前 stdlib `ThreadingHTTPServer` + nested handler 可保持短期不变，但需要把 dispatch 逻辑变深：

- 增加 route dispatch table：method + path matcher -> handler。
- 将 job read、logs/events、post-edit、download、admin/internal health 分文件或分 class。
- handler 只做 request/response adaptation，不直接承载业务决策。
- 增加 response helper，统一 JSON error、status code、headers。

建议新增测试：

- 路由表覆盖测试：每个公开路径都有 handler。
- 错误载荷一致性测试。
- logs/events since/tail 参数测试。

### `src/services/jobs/service.py`

建议把 post-edit 从通用 JobService 中抽出：

- `EditingPlan`：描述一次编辑提交的 deterministic plan。
- `EditingApplicationModule`：应用 bulk replace、voice modify、segment commit。
- `EditingAuditEmitter`：记录事件、用户可见消息、内部调试上下文。

迁移策略：

- 保持 `JobService` 对外方法名不变。
- 内部转调新模块。
- 先迁纯函数和 audit payload，再迁状态修改。
- 每一步都用现有 post-edit 测试保护。

完成标准：

- `JobService` 不直接持有 post-edit 的大量分支。
- post-edit 模块可用 fake `JobStore` 单测。
- 失败路径能证明状态不半写。

## 5.5 数据一致性与状态写入

### 已有基础

`JobStore` 已经有：

- 文件锁。
- 原子保存。
- `update_job(mutator, initial=None, fsync=True)`。
- list cache。
- event append。

因此下一步不是“新增 update_job”，而是完成调用方迁移。

### 建议任务

1. **扫描并消除直接 read-modify-save**
   - 查找 `require_job(...)` 后紧跟 `save_job(...)` 的模式。
   - 能迁的改为 `update_job`。
   - 不能迁的写下注释说明需要跨资源事务或兼容原因。

2. **统一 JSON 原子写**
   - 当前 `src/utils/atomic_io.py`、`JobStore._write_json_atomic`、个别 backfill 脚本有重复实现。
   - 推荐升级为一个 canonical helper：
     - 支持 `Path`。
     - 支持 `dict/list` JSON。
     - 支持 `fsync` 选项。
     - 支持同目录临时文件。
     - Windows 下避免替换打开中的文件失败时吞错。

3. **事件读取增量化**
   - 为 logs/events 增加 `since` 或 `cursor`。
   - 前端和 Gateway 不再为了轮询反复拉全量事件。

4. **状态词汇继续单源**
   - 保持 `tests/test_status_vocab_in_sync.py`。
   - 新状态必须先更新 Python 源，再同步 TS 映射和用户文案。

## 5.6 Python 代码规范与静态质量

### 当前缺口

- `pyproject.toml` 目前没有 Ruff/Black/Mypy/Pyright/Coverage 配置。
- 依赖声明里 dev 工具较少，只有 pytest/pytest-asyncio/aiosqlite。
- `# noqa`、`# type: ignore`、`Any`、`dict[str, Any]` 分布较多，但没有 debt budget。
- 多处重复的 normalize/coerce helper，没有统一位置。
- 不同目录存在 `sys.path.insert` 和 `services.*` / `src.services.*` 双导入兼容。

### 推荐质量门禁分阶段

#### 阶段 A：只报告，不阻断

新增 Ruff 配置，先只开低噪声规则：

- `E`, `F`：基础 pycodestyle/pyflakes。
- `I`：import 排序。
- `UP`：pyupgrade，适配 Python 3.12。
- `B`：bugbear 的高价值规则。
- `SIM`：只选低噪声规则，不一次性启全。

先排除：

- `docs/archive/**`。
- 生成物目录。
- 大型 fixture。
- 历史迁移脚本中不适合立即修的文件。

建议新增命令：

```bash
ruff check src gateway tests
ruff format --check src gateway tests
```

#### 阶段 B：对新改文件阻断

- CI 对 changed Python files 跑 Ruff。
- 全仓 Ruff 仍可暂时 report-only。
- 新增 `# noqa` 必须带原因，或进入 allowlist。

#### 阶段 C：扩大到全仓阻断

- 当剩余问题低于可承受数量后，CI 全仓阻断。

### 类型检查建议

不建议直接全仓 strict mypy。建议从纯模块开始：

第一批：

- `src/utils/**`。
- `src/modules/subtitles/**`。
- `src/services/language_registry*`。
- `src/services/jobs/store.py`。
- `src/workflow/output_dispatcher.py`。

第二批：

- `src/services/jobs/editing_segments.py`。
- `src/services/alignment/**` 中不依赖重外部包的模块。
- Gateway 中 Pydantic/schema 类模块。

第三批：

- Provider adapter。
- 大型 orchestration。

建议新增：

```bash
mypy src/utils src/modules/subtitles src/workflow/output_dispatcher.py
```

或若团队更偏 TS/Pyright 风格，使用 Pyright 也可以，但必须同样按目录递进。

### Python helper 标准化

建议新增或扩展统一模块：

| Helper | 当前问题 | 建议 |
| --- | --- | --- |
| optional text normalize | 多文件重复 `_normalize_optional_text` | `src/utils/coerce.py` 或领域内 helper |
| int/float coercion | Gateway/Job/API/成本统计重复 | 统一 `coerce_int`, `coerce_float`, `coerce_bool` |
| JSON atomic write | 多份实现 | 升级 `src/utils/atomic_io.py` |
| error payload | Gateway/Job API 格式不完全统一 | `error_code/message/detail/retryable/user_action` |
| datetime serialization | 多处局部转换 | 统一 UTC/ISO helper |

## 5.7 导入与包边界规范

### 当前问题

测试和 Gateway 中存在多处 `sys.path.insert`。部分注释和兼容代码同时提到 `from src.services...` 和 `from services...`。这在迁移期可以理解，但长期会造成：

- 本地、Docker、CI 导入路径不一致。
- 测试通过但生产路径失败。
- 类型检查和 IDE 分析失真。

### 推荐规则

1. 运行时应用内部统一一种导入风格。
   - 对 `src` 包内代码，优先使用 `services.*`、`modules.*`、`workflow.*` 这类以 `src` 为 import root 的风格。
   - Gateway 如需访问 `src`，只在一个 bootstrap 位置处理路径注入，业务模块不重复注入。

2. 测试只在 `tests/conftest.py` 或 pytest 配置中设置 import path。

3. 新增守卫：
   - 禁止新测试文件重复 `sys.path.insert`，除非写入 allowlist。
   - Gateway route module 禁止直接插入 `sys.path`。

4. Docker 与本地命令统一：
   - 文档给出 `PYTHONPATH` 或 editable install 推荐方式。
   - `main.py` 和 `pytest` 继续保持干净环境可运行。

## 5.8 前端代码质量与体验稳定性

### 当前基础

前端使用 Next 16、React 19、TypeScript strict、ESLint 9，并启用了 React Compiler 相关 lint 规则。已有 `ApiClient`、`usePollingTask`、`useBackgroundTask` 等基础抽象。

### 主要风险

- 页面文件过大，route shell 承担过多状态。
- 类型虽然 strict，但后端响应解析仍主要依赖 TS interface，运行时 contract 不够强。
- 编辑页、语音选择、管理设置、pricing/admin 页重复展示和转换逻辑较多。
- 中国用户关键文案应集中维护，避免不同页面同一状态不同说法。

### 编辑页拆分方案

目标：`frontend-next/src/app/(app)/workspace/[jobId]/edit/page.tsx` 从近 2k 行降为 route shell。

建议拆为：

| 新模块 | 职责 |
| --- | --- |
| `useEditingJobSync` | 加载 job、轮询、visibility refresh、错误状态 |
| `useEditingSegments` | segments 选择、局部修改、dirty 状态 |
| `useBulkReplace` | 批量替换参数、预览、提交 |
| `useCommitFlow` | commit、pending、toast、失败恢复 |
| `EditingToolbar` | 顶部动作区 |
| `SegmentEditorPanel` | segment list + editor |
| `EditingStatusBar` | 保存状态、任务状态、错误摘要 |

完成标准：

- route page 只负责参数读取、hook 组合、布局。
- hooks 使用 fake API client 单测。
- 核心用户流用 Playwright 或组件级测试覆盖。

### 语音选择与修改复用

`VoiceSelectionPanel.tsx` 和 `VoiceModifyTab.tsx` 都有 provider、voice candidate、试听/选择/过滤等逻辑。建议抽取：

| 模块 | 职责 |
| --- | --- |
| `voiceSelectionModel.ts` | voice option、provider、availability、display name |
| `useVoiceCandidates` | 拉取/过滤/排序候选 |
| `VoiceCandidateList` | 列表展示 |
| `VoiceProviderTabs` | provider tab |
| `VoicePreviewButton` | 试听与 loading 状态 |

必须保证：

- 不把 Gateway 或 worker 的真实能力写死到前端。
- 前端只展示 Gateway/Job API 返回的能力事实。
- 中文文案统一。

### API 客户端与契约

短期不建议为了契约引入大型生成链路。建议：

- 对关键响应增加 runtime normalizer。
- 所有 critical endpoint 响应至少通过 `parseXxxResponse`。
- `parseXxxResponse` 不做商业决策，只做类型收窄和默认值处理。
- Python/TS 增加契约测试或 fixture parity 测试。

关键 endpoint：

- job list/get。
- post-edit status/commit。
- pricing/catalog。
- trial/entitlement。
- voice catalog。
- anonymous preview。
- Smart Preview billing。

## 5.9 测试体系优化

### 当前问题

测试文件多且守卫强，但缺少统一分类会造成：

- PR CI 时间不可控。
- 慢测试和守卫测试混在一起。
- 大型测试文件定位难。
- provider/mock/integration 边界不够显性。

### 推荐 pytest markers

建议在 `pyproject.toml` 或 `pytest.ini` 中定义：

| Marker | 用途 |
| --- | --- |
| `unit` | 纯函数/小模块 |
| `contract` | API/schema/status/path parity |
| `guard` | 架构守卫、禁止回退 |
| `integration` | 多模块本地集成 |
| `pg` | 需要 Postgres |
| `slow` | 长耗时 |
| `real_provider` | 真实外部服务，默认禁止 |
| `e2e` | 浏览器/端到端 |
| `benchmark` | 性能基准，不作为普通 pass/fail |

默认本地建议：

```bash
pytest -q -m "not slow and not real_provider and not benchmark"
```

PR CI 建议：

```bash
pytest -q -m "unit or contract or guard"
pytest -q tests/test_legacy_cleanup_guards.py tests/test_phase1_guards.py tests/test_status_vocab_in_sync.py tests/test_admin_gate_coverage.py
```

Nightly CI：

```bash
pytest -q
pytest -q -m pg
```

### 大测试文件治理

优先拆分：

- `tests/test_process_pipeline.py`。
- `tests/test_smart_business_logic.py`。
- `tests/test_jianying_draft_runner.py`。
- `tests/test_anonymous_preview_backend_adapter.py`。
- `tests/test_web_ui.py`。

拆分原则：

- 不是为了小文件而小文件。
- 按 behavior/capability 分组。
- 保留共享 fixture。
- 每次拆分不改断言语义。

### 覆盖率策略

不建议立刻设全仓 80%。推荐风险预算：

| 区域 | 建议覆盖目标 |
| --- | --- |
| subtitles/retiming/deterministic transforms | 高覆盖，接近 90% |
| JobStore/atomic state | 高覆盖，含并发与失败路径 |
| post-edit plan/application | 高覆盖 |
| Gateway commercial truth | contract + integration 覆盖 |
| Provider adapters | mock 覆盖主要错误路径，真实 provider 单独标记 |
| legacy compatibility shell | 以 contract/golden 为主，不追求每行覆盖 |

## 5.10 性能与效率优化

### 原则

先观测，再优化。不要为了“效率”改变核心语义：

- 不降低对齐准确性。
- 不绕过 DSP-first。
- 不让 LLM 负责确定性 retiming。
- 不把失败重试变成静默吞错。

### 性能基准建议

新增轻量 benchmark harness，输出 JSON：

```bash
python scripts/benchmark_pipeline_stage_timings.py --fixture tests/fixtures/benchmark/<case>
```

记录：

- media ingestion 时间。
- subtitle parse/semantic block 时间。
- translation 分块时间。
- TTS 生成时间。
- alignment DSP 时间。
- rewrite fallback 次数与时间。
- draft output 时间。
- artifact upload/R2 时间。
- peak memory。
- subprocess 次数。

建议输出到：

- `reports/benchmark/<date>-stage-timings.json`。
- CI 可只保存 artifact，不阻断。

### 后端效率优先级

| 优先级 | 目标 | 建议 |
| --- | --- | --- |
| P0 | logs/events 轮询 | 增加 cursor/since/tail，减少全量读取 |
| P0 | Job list read path | 保持 no-settlement guard，减少每次 list 的 DB mirror 副作用；引入 TTL 或 background projection |
| P1 | JSON 状态写入 | 继续迁移到 `update_job`，批量事件 append 默认可配置 fsync |
| P1 | Gateway DB pool | 改为 settings 可配置并暴露指标，不盲目硬调 |
| P1 | TTS/alignment 并发 | 只在 benchmark 后调整 semaphore/worker 数 |
| P2 | provider fallback | 标准化超时、重试、熔断与错误分类 |

### 前端效率优先级

| 优先级 | 目标 | 建议 |
| --- | --- | --- |
| P0 | 编辑页渲染 | 确认 segment list 使用 virtualization；拆分局部 state，避免整页重渲染 |
| P0 | 轮询 payload | 配合 logs/events cursor，避免全量 job/event payload |
| P1 | 大型管理页 | 设置页和 pricing 页拆配置 schema 与子面板 |
| P1 | API 请求 | critical action 使用 AbortController 与请求去重 |
| P2 | bundle | 路由级 code splitting，管理端重组件延迟加载 |

## 5.11 错误处理与日志规范

### 问题

系统中存在多种错误表示方式：

- Python exception。
- Gateway HTTP error。
- Job API JSON error。
- 前端 toast/message。
- provider 原始错误。
- pipeline print/log marker。

### 推荐标准载荷

面向 API 的错误统一包含：

```json
{
  "error_code": "POST_EDIT_CONFLICT",
  "message": "当前任务状态不允许提交修改",
  "detail": {},
  "retryable": false,
  "user_action": "请刷新任务状态后重试"
}
```

规则：

- `message` 面向用户或前端展示，中文自然。
- `error_code` 稳定、英文、可测试。
- `detail` 不含 secret。
- `retryable` 明确。
- `user_action` 可选，但对支付/上传/编辑失败很有价值。

### 日志

- CLI/protocol marker 可以继续使用 print。
- 服务路径逐步迁移到 logger。
- provider 原始错误进入 debug/detail，不直接展示给用户。
- 支付、额度、trial、settlement 必须有 audit log。
- 日志不得包含 token、cookie、真实密钥、完整支付凭证。

## 5.12 商业化与 Gateway 事实源

### 当前约束

Gateway 是 plan、trial、pricing、entitlement 的事实源。前端不能重建商业规则。

### 建议

1. **Plan catalog contract test**
   - Gateway 返回 plan catalog fixture。
   - 前端只做展示映射和中文文案。
   - 禁止在前端计算最终价格、试用资格、额度扣减。

2. **Billing state machine**
   - PaymentOrder、credit、trial、Smart Preview settlement 形成状态机文档。
   - 每次新增状态必须有迁移、回滚、reconcile 规则。

3. **Read/write path 明确**
   - list/get 只读。
   - callback/reconcile/write endpoint 才能改变商业事实。
   - 守卫测试继续覆盖。

4. **中文支付信任文案集中化**
   - “试用剩余”“扣费中”“支付确认中”“已入账”“需人工核查”等文案集中维护。
   - 避免页面间同一状态不同说法。

## 5.13 文档治理

### 当前问题

文档数量丰富，但历史计划可能和当前实现不同步。比如历史审计中提到的部分 P0 已经修复，若继续被引用为当前问题，会误导排期。

### 建议

1. **新增 Current Quality Roadmap**
   - 本文档作为当前优化路线。
   - 后续每完成阶段，在本文档或 `docs/plans/README.md` 更新状态。

2. **历史审计状态标注**
   - 对 2026-05-07 与 2026-05-21 审计中的事项标注：
     - done。
     - superseded。
     - still relevant。
     - intentionally deferred。

3. **架构决策进入 ADR**
   - `process.py` 收敛。
   - Gateway route family 拆分。
   - Job API 是否迁 FastAPI。
   - JSON store 是否迁 Postgres。
   - API contract 生成策略。

4. **图谱文档作为入口**
   - 每次大改模块前先更新或参考对应 graph。
   - 新模块命名应能在图谱中体现真实业务接缝。

## 6. 推荐实施路线图

## 6.1 Phase 0：建立基线（1-2 天）

目标：不改变运行行为，只建立可观测和可执行标准。

任务：

- 新增 Ruff 配置，先 report-only 或只对小范围执行。
- 定义 pytest markers。
- 新增 `scripts/check_quality.ps1` 和/或 `scripts/check_quality.sh`：
  - `python main.py --help`
  - core guard tests
  - selected unit/contract tests
  - frontend lint/typecheck
- 记录当前大文件、测试耗时、CI 耗时、前端 lint/typecheck 状态。
- 给 `# type: ignore`、`# noqa`、`Any` 建一个 debt baseline。

验收：

- 本地一条命令能跑核心质量检查。
- 文档列出当前 baseline。
- CI 不因新工具一次性爆红。

## 6.2 Phase 1：低风险标准化（1 周）

目标：减少重复 helper 和不一致，不触碰大业务流。

任务：

- 升级/统一 `src/utils/atomic_io.py`。
- 新增 `src/utils/coerce.py` 或等价 helper，迁移最明显重复点。
- 统一 API error payload helper。
- Gateway 只读路由 no-settlement 守卫补强。
- 前端 critical response 增加 parser/normalizer。
- 测试按 markers 分类，保留现有 CI 行为。

验收：

- 重复 helper 数量下降。
- 新增 helper 有单元测试。
- 守卫测试通过。
- 前端 lint/typecheck 通过。

## 6.3 Phase 2：热点模块深挖（2-3 周）

目标：拆掉最影响迭代速度的热点文件。

任务：

- Gateway `job_intercept.py` 先拆 post-edit route family。
- Next 编辑页拆 hooks 与 panels。
- `VoiceSelectionPanel` / `VoiceModifyTab` 抽 shared voice selection 模块。
- `src/services/jobs/api.py` 增加 dispatch table。
- `JobService` post-edit 逻辑转调新模块。

验收：

- 每个拆分都有行为等价测试。
- 外部路由、前端路径、用户文案不回退。
- `job_intercept.py`、编辑页、voice 文件行数明显下降。

## 6.4 Phase 3：工作流收敛与性能可观测（1 个月）

目标：降低第二架构风险，开始用数据优化性能。

任务：

- `process.py` 输出路径迁 `OutputDispatcher`。
- `process.py` build 阶段逐步消费 `ProjectWorkflow.run_build()`。
- 新增 pipeline stage timing benchmark。
- logs/events 增量读取。
- Gateway DB pool 设置化和指标化。
- 前端轮询改用 cursor/since。

验收：

- `process.py` 行数下降，兼容行为不变。
- benchmark 产出稳定 JSON。
- logs/events payload 明显下降。
- 性能改动有前后对比。

## 6.5 Phase 4：中长期治理

目标：在稳定 contract 后考虑更大结构调整。

候选：

- Job API 从 stdlib HTTP 迁到 FastAPI。
- JSON job store 逐步迁 DB 或混合 projection。
- OpenAPI/schema 生成 TS contract。
- 后台任务队列标准化。
- Provider adapter 熔断/限流统一。
- 全仓 Ruff/mypy/coverage 阻断。

前置条件：

- 当前路由 contract 完整。
- 性能和可靠性指标明确。
- 迁移能灰度或 fallback。

## 7. 建议拆成的前 12 个 Issue

| # | 标题 | 范围 | 完成标准 |
| ---: | --- | --- | --- |
| 1 | 建立 Python/前端质量基线脚本 | `scripts/`, `pyproject.toml`, `frontend-next` | 一条命令跑核心 guard、lint、typecheck；不破坏现有 CI |
| 2 | 定义 pytest markers 并标记核心守卫 | `pyproject.toml` 或 `pytest.ini`, `tests/` | `pytest -m "guard or contract"` 可运行 |
| 3 | Ruff report-only 引入 | `pyproject.toml`, CI | 新增 lint 报告，不阻断历史问题 |
| 4 | 统一 JSON atomic write helper | `src/utils/atomic_io.py`, `JobStore`, backfill scripts | helper 有 fsync/Path/list 支持；关键调用迁移 |
| 5 | 统一 coerce/normalize helper | `src/utils/`, Gateway/Job/API 局部文件 | 删除至少 3 处重复 helper；测试覆盖边界输入 |
| 6 | Job API logs/events cursor | `src/services/jobs/api.py`, `JobService`, frontend polling | 支持 since/cursor/tail；旧调用兼容 |
| 7 | Gateway post-edit route family 拆分 | `gateway/job_intercept.py`, `gateway/routes/job_post_edit.py` | 路由不变；whitelist/path parity 测试通过 |
| 8 | 编辑页 route shell 化 | `frontend-next/src/app/(app)/workspace/[jobId]/edit` | `page.tsx` 降到约 400 行以内；hooks 可单测 |
| 9 | 语音选择共享模块 | `frontend-next/src/components/**` | TranslationForm/VoiceModifyTab 复用候选逻辑 |
| 10 | JobService post-edit 应用模块 | `src/services/jobs/` | post-edit plan/application 可独立测试 |
| 11 | process 输出路径收敛第一刀 | `src/pipeline/process.py`, `src/workflow/output_dispatcher.py` | 新旧输出 contract 测试通过 |
| 12 | Pipeline stage benchmark | `scripts/`, `reports/benchmark`, `tests/fixtures/benchmark` | 输出 stage timing JSON；不作为普通 CI 阻断 |

## 8. 质量指标建议

### 8.1 代码结构指标

| 指标 | 当前倾向 | 目标 |
| --- | --- | --- |
| 最大 Python 文件 | `process.py` 约 12k+ 行 | 阶段性降到 9k 以下，长期继续下降 |
| 最大 Gateway route 文件 | `job_intercept.py` 约 6k+ 行 | Phase 2 降到 4.5k 以下 |
| 最大 TSX 页面 | edit page 约 2k 行 | route shell 400 行以内 |
| 新增业务文件 | 不限制 | 新文件应有单一职责和测试 |
| 新增 `Any`/`type: ignore` | 未预算 | 新增需说明或进入 allowlist |

### 8.2 测试指标

| 指标 | 目标 |
| --- | --- |
| Guard tests | PR 必跑 |
| Contract tests | PR 必跑 |
| Slow/integration/pg | Nightly 或路径触发 |
| Real provider | 默认不跑，需要显式 opt-in |
| Coverage | 先按高风险模块设阈值，不全仓一刀切 |

### 8.3 性能指标

| 指标 | 目标 |
| --- | --- |
| Job list p95 | 建立基线后逐步下降 |
| logs/events payload | cursor 化后显著下降 |
| pipeline stage timings | 每类 fixture 有历史对比 |
| rewrite fallback 次数 | 可观测，不异常增长 |
| frontend edit render | 大 job 场景不卡顿，segment list virtualization 生效 |

## 9. 风险与防回退守卫

| 风险 | 防护 |
| --- | --- |
| 大拆分引入行为回退 | 每次只迁一个 route family 或一个 UI flow；先 contract 后移动 |
| Gateway 商业事实漂移到前端 | 保留并加强 frontend no estimator / plan catalog parity 守卫 |
| `process.py` 收敛破坏 CLI | `main.py --help`、golden output、draft contract 必跑 |
| Provider 真实调用进入测试 | `real_provider` marker 默认禁止 |
| Ruff/mypy 一次性制造大量噪声 | report-only + changed files 阶段化 |
| 新 helper 变成薄包装 | 只抽重复且有真实复杂度的逻辑 |
| route 拆分丢权限 | admin gate coverage 与 route registration test 必跑 |
| 性能优化破坏准确性 | benchmark 只观察，语义 contract 优先 |

## 10. 推荐验证命令

文档类改动不需要全部执行，但代码优化 PR 应至少按范围选择：

```bash
python main.py --help
pytest -q tests/test_legacy_cleanup_guards.py tests/test_phase1_guards.py tests/test_status_vocab_in_sync.py tests/test_admin_gate_coverage.py
pytest -q -m "not slow and not real_provider and not benchmark"
```

前端：

```bash
cd frontend-next
npm run lint
npx tsc --noEmit
```

Gateway/Postgres 相关：

```bash
pytest -q -m pg
```

性能基准：

```bash
python scripts/benchmark_pipeline_stage_timings.py --fixture tests/fixtures/benchmark/<case>
```

## 11. 不建议现在做的事

- 不建议全仓大重构。
- 不建议立即把 Job API 迁 FastAPI，除非 route contract 先补齐。
- 不建议全仓 strict mypy。
- 不建议把所有 JSON state 立即迁 DB。
- 不建议引入重量级前端状态库来解决单页过大问题。
- 不建议为了减少文件数而合并模块。
- 不建议用 LLM 替代 deterministic retiming 或 DSP alignment 主路径。
- 不建议默认测试路径接入真实支付、TTS、LLM、R2、Pan、YouTube 等外部服务。

## 12. 建议的第一阶段落地 PR 切分

### PR 1：质量基线，不改行为

内容：

- 加 Ruff 配置。
- 加 pytest markers。
- 加 `scripts/check_quality.*`。
- CI 增加 report-only job 或本地文档。

测试：

- `python main.py --help`
- core guard tests。
- frontend lint/typecheck。

风险：低。

### PR 2：JSON atomic helper 标准化

内容：

- 升级 `src/utils/atomic_io.py`。
- `JobStore` 或一个 backfill 脚本先迁移。
- 增加 Windows/异常路径测试。

风险：中低。涉及文件写入，需要保守。

### PR 3：Gateway post-edit route family 拆分

内容：

- 新增 `gateway/routes/job_post_edit.py`。
- `job_intercept.py` 保留注册兼容。
- 路由路径不变。

测试：

- post-edit whitelist。
- path parity。
- admin/permission。
- 相关 gateway endpoint tests。

风险：中。

### PR 4：编辑页 hooks 拆分

内容：

- 抽 `useEditingJobSync`、`useEditingSegments`、`useCommitFlow`。
- UI 不改版。

测试：

- frontend lint/typecheck。
- 编辑页关键 flow E2E 或组件测试。

风险：中。

### PR 5：pipeline stage benchmark

内容：

- 新增 benchmark 脚本。
- 输出 JSON。
- 文档记录如何运行。

测试：

- 小 fixture smoke test。

风险：低。

## 13. 最终判断

本项目代码质量的下一步重点不是“加更多抽象”，而是把已经自然形成的业务接缝变成窄接口，并用测试和工具固定下来。最值得优先做的不是最显眼的 `process.py` 大拆，而是：

1. 质量基线与 markers。
2. Gateway route family 拆分。
3. 前端编辑页和语音选择逻辑拆分。
4. JSON/错误/导入/helper 规范化。
5. 在有性能基准后再优化 pipeline 和轮询。

这样可以在不破坏现有商业化迁移、不影响 Jianying draft 主目标、不牺牲架构不变量的前提下，逐步降低后续迭代成本。
