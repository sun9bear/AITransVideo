# 代码审计整改与结构收敛方案

> Status: Draft
> Last updated: 2026-05-10
> Source audit: `docs/audits/2026-05-10-comprehensive-codebase-review.md`
> Scope: 基于 2026-05-10 全面代码审计与二次核验结果，制定可执行的分阶段整改方案。优先修复低风险、高收益、可验证的问题；大文件拆分、provider/配置统一和 API 版本化按增量迁移推进，不做大爆炸重写。

## 1. 执行结论

当前最该马上做的不是重写 pipeline，也不是把 `gateway/job_intercept.py` 一次性拆成多个子系统，而是先补上能保护后续改造的“安全网”：

1. Gateway 启动恢复失败不能静默吞掉。
2. 最小 CI 必须入仓，至少覆盖 Python 守卫测试与前端 type/lint。
3. `_report_job_metering` 要先抽出纯 payload builder，并补参数化测试。
4. post-edit 限额逻辑要补直接单测，尤其是 TTS 字数、segment 数、batch 分支。
5. `except BaseException` 不直接机械替换，先用 characterization test 锁住并发清理语义。

这些 P0 项都不要求改变主架构，不引入外部服务，不改变生产业务规则，适合作为第一批修复。

## 2. 事实基线

二次核验的当前工作区事实：

- `git ls-files`: 1397
- `rg --files`: 1375
- Python 测试文件：294
- `docs/graphs/*.md`: 11，其中架构子图 10 张
- `docs/plans/**/*.md`: 166
- `src/pipeline/process.py`: 8,430 行
- `gateway/job_intercept.py`: 3,300 行
- `src/services/gemini/translator.py`: 2,731 行
- `src/services/alignment/aligner.py` 中 `getattr(segment, ...)`: 13 处
- `src/services/gemini/translator.py` 中 `Any`: 17 处
- `/job-api` 引用：运行时代码/配置约 71 处，测试约 48 处，文档另计
- `print(`: 约 451 处；`logging/logger/getLogger`: 约 797 处

关键约束：

- TTS 单元仍必须是 `SemanticBlock`，不能退回 subtitle line。
- Alignment 仍必须 DSP-first，rewrite loop 是 fallback。
- Subtitle retiming 仍必须是数学/确定性逻辑，不交给 LLM。
- 主交付目标仍是 Jianying draft output，不把 rendered MP4 变成主 deliverable。
- Gateway 仍是 plan/pricing/trial/entitlement 真源，frontend 只能消费商业事实。
- 本方案所有测试和默认路径继续使用 mock/stub/fake，不引入真实外部 API 依赖。

## 3. 目标与非目标

### 3.1 目标

- 让确认问题先有可观测信号，避免后台恢复失败无声消失。
- 让审计指出的商业/计量边界被测试锁住。
- 为后续拆分大文件、大函数、大类建立 CI 和 characterization tests。
- 将大改造拆成可回滚、可验收的薄切片。
- 保持 `main.py` 和 `pytest` 在干净本地环境可运行。

### 3.2 非目标

- 不在 P0 阶段重写 `process.py` 为完整 stage framework。
- 不在 P0 阶段把 `intercept_create_job` 改成全量 middleware chain。
- 不在 P0 阶段拆 `GeminiTranslator`。
- 不在 P0 阶段修改 `/job-api` 路由前缀。
- 不在 P0 阶段统一全部配置系统。
- 不接入真实 CI secrets、真实支付、真实短信、真实 TTS/LLM 调用。

## 4. 分阶段计划

## P0：先补安全网与确认问题

P0 的验收标准是：小改动、低风险、能通过测试证明、不会改变主业务行为。

### P0-1 Gateway 启动恢复失败不静默

当前问题：

- `gateway/main.py:109-130` 中 `recover_stale_tasks` 与 `background_task_queue.recover_stale` 被 `except Exception: pass` 包住。
- 注释说明部分异常可能来自 migration 前表不存在，但真实恢复失败也会被吞掉。

改造方案：

1. 新增小 helper，例如：
   - `_log_startup_recovery_failure(name: str, exc: Exception) -> None`
   - 或 `_is_expected_startup_recovery_schema_error(exc: Exception) -> bool`
2. 直接使用 `gateway/main.py` 已有模块级 `logger`，顺手去掉当前局部 `import logging` + `logging.getLogger(__name__)` 的不一致写法。
3. 对“表不存在/未迁移”类异常打 `logger.warning` 或 `logger.info`，保留降级。
4. 对其他异常打 `logger.exception`，内容包含恢复目标：`label_tasks` / `background_tasks`。
5. 可选：在 Gateway 内存状态保存最近一次 startup recovery 结果，后续 admin/health 可读取。

涉及文件：

- `gateway/main.py`
- `tests/test_gateway_startup_checks.py` 或新增 `tests/test_gateway_startup_recovery.py`

测试建议：

- mock `recover_stale_tasks` 抛普通 `RuntimeError`，断言 `logger.exception` 被调用且 lifespan 不崩。
- mock schema/migration 预期异常，断言降级日志不是 exception。
- mock `recover_stale` 成功返回正数，保留现有 info 日志行为。

验收：

- Gateway 启动恢复失败不再无日志消失。
- 预期迁移前状态仍不会阻断本地启动。

### P0-2 最小 CI 入仓

当前问题：

- 仓库无 `.github/workflows/`。
- 架构守卫测试、前端 type/lint 依赖人工运行。

改造方案：

新增 `.github/workflows/ci.yml`，第一版只做最小保护：

```yaml
name: CI

on:
  pull_request:
  push:
    branches: [main]
  workflow_dispatch:

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

jobs:
  python-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: python -m pip install -U pip
      - run: python -m pip install -r requirements-dev.txt
      - run: pytest -q tests/test_phase1_guards.py tests/test_legacy_cleanup_guards.py
      - run: pytest -q tests/test_metering_payload_builder.py tests/test_job_metering_writeback.py tests/test_gateway_editing_commit_sync.py

  frontend:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: frontend-next
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "22"
          cache: npm
          cache-dependency-path: frontend-next/package-lock.json
      - run: npm ci
      - run: npm run lint
      - run: npx tsc --noEmit
```

注意：

- 项目要求 Python `>=3.12,<3.13`，CI 必须用 3.12。
- 仓库没有 `requirements.txt`；开发/测试入口是 `requirements-dev.txt`，内容等价于 `pip install -e ".[dev]"`。
- 不接真实外部服务，不配置 secrets。
- 不先跑全量 pytest，避免第一版 CI 因慢/环境差异无法落地。

验收：

- PR/push 会自动跑关键守卫测试。
- 失败信息能定位到 Python 或 frontend。

### P0-3 `_report_job_metering` 抽纯 payload builder

当前问题：

- `src/pipeline/process.py::_report_job_metering` 约 452 行。
- 已有窄单测覆盖 internal key 发送，但 body 构造、glossary、异常降级等分支难测。

改造方案：

1. 保留 `_report_job_metering(...)` 作为外部入口，不改调用点。
2. 抽出纯函数：
   - `_build_job_metering_payload(...) -> dict[str, object]`
   - 只负责字段计算和白名单输出，不做 HTTP。
3. `_report_job_metering` 只做：
   - 读取 gateway URL / internal key
   - 调用 builder
   - `urllib.request.Request`
   - best-effort 发送和错误日志
4. glossary 计算可拆为：
   - `_append_glossary_metering_fields(body, segments, glossary) -> None`
   - 或让 builder 接受可注入的 glossary checker，方便测试。

涉及文件：

- `src/pipeline/process.py`
- 新增 `tests/test_metering_payload_builder.py`
- `tests/test_job_metering_writeback.py`
- `tests/test_process_pipeline.py` 只保留现有 wire-level/回归测试，不继续承载新 builder 参数化用例；该文件已超过 6,000 行。

测试建议：

- 最小 segments：验证 `final_cn_chars`、`tts_billed_chars`。
- 带 rewrite metadata：验证 rewrite count / pre-TTS 字段。
- 带 glossary：验证 `term_preservation_rate`。
- glossary checker 抛异常：验证 builder 或 caller 降级，不影响发送。
- internal key 已有测试保留。

验收：

- `_report_job_metering` 行数明显下降。
- 核心 metering payload 不需要完整 pipeline 即可测试。
- Gateway `update_job_metering` 仍收到兼容字段。

### P0-4 post-edit 限额逻辑补直接测试

当前问题：

- commit 次数和试用 `copy_as_new` 已有测试。
- `_consume_post_edit_tts_usage` 的 TTS 字数、segment 数、batch regenerate 分支仍缺直接覆盖。

改造方案：

1. 不先改业务逻辑，先补 characterization tests。
2. 覆盖 `_consume_post_edit_tts_usage`：
   - trial / plus / pro 的 `tts_segments`
   - `tts_chars`
   - `batch_regenerates`
   - 超限时 HTTPException 状态码和 message
   - 成功消费时 `metering_snapshot[post_edit_usage]` 累加
3. 对 batch regenerate 入口补一条上层测试，确认调用 `_consume_post_edit_tts_usage` 的参数正确。

涉及文件：

- `gateway/job_intercept.py`
- `tests/test_gateway_editing_commit_sync.py` 或新增 `tests/test_gateway_post_edit_limits.py`

验收：

- 商业限额边界被测试锁住。
- 后续重构 `POST_EDIT_LIMITS` 或 plan rank 时能防回归。

### P0-5 `except BaseException` 清理语义 characterization test

当前问题：

- `src/services/alignment/aligner.py:465` 捕获 `BaseException`。
- 当前代码会 `stop_event.set()`、取消 pending futures 并重新抛出，所以不是确认的 Ctrl+C 吞掉 bug。
- 风险是清理语义依赖过宽异常捕获，缺少测试。

改造方案：

1. 不先把 `BaseException` 改成 `Exception`。
2. 扩展 `tests/test_aligner_concurrency.py` 现有并发测试（当前已有 19 个相关测试，包含 stop_event 和 paid fallback concurrency 覆盖），新增 1-2 条专门针对 `BaseException` 子类的 characterization：
   - 某个 future 抛出自定义 `BaseException` 子类。
   - 可选补 `KeyboardInterrupt` 场景。
   - `_align_all_parallel` 应重新抛出同一个异常。
   - pending futures 被 cancel 或 stop_event 阻止新增 paid work。
3. 测试稳定后，再评估是否改为：
   - `except BaseException: ... raise`
   - 或 `try/finally` + 更窄 `except Exception`

验收：

- 并发清理语义被锁定。
- 后续异常边界调整不会误伤 paid fallback guard。

## P1：契约与迁移准备

### P1-1 Metering payload schema

目标：

- 从“任意 JSONB merge”收敛为“白名单字段 + schema”。

方案：

1. 定义 `JobMeteringPayload`：
   - 可用 Pydantic model，也可先用 `TypedDict` + validator。
2. Gateway `update_job_metering` 只接受已知字段。
3. Pipeline builder 输出符合 schema 的 dict。
4. 对未知字段保留兼容策略：
   - P1 初期可忽略未知字段并打 warning。
   - 连续 7 天 warning 0 命中，且 CI 覆盖所有已知 pipeline writer 后，再切换为 400。

涉及文件：

- `src/pipeline/process.py`
- `gateway/job_intercept.py`
- 可选：`src/shared/` 或 `gateway/metering_schema.py`
- `tests/test_job_metering_writeback.py`

验收：

- metering 字段漂移有测试。
- Gateway 不再隐式接受任意 JSON。

### P1-2 `/job-api` 版本化兼容迁移

目标：

- 为未来移动端/第三方 API 留出版本演进空间，不破坏现有 frontend/pipeline。

事实：

- `/job-api` 运行时代码/配置约 71 处。
- 测试约 48 处。

方案：

1. 不直接替换现有 `/job-api`。
2. 选择方向 A：`/job-api` 继续作为当前 canonical 生产路径，新增 `/v1/job-api` alias/rewrite 到同一套 Gateway intercept 逻辑。这样移动端/第三方可以先使用 v1 入口，老 frontend 和 pipeline 不动。
3. 前端 `frontend-next/src/lib/api/config.ts` 支持配置 base URL。
4. Pipeline callback URL 通过配置生成，不手写拼接。
5. 添加 route coverage 测试：
   - `/job-api/jobs`
   - `/v1/job-api/jobs`
   - catch-all subresource
6. 等所有客户端支持 base URL，并且 `/v1/job-api` 运行一段时间无路由差异后，再评估是否把 canonical 切到 v1。默认不在本轮切。

涉及文件：

- `gateway/main.py`
- `Caddyfile`
- `frontend-next/src/lib/api/config.ts`
- `src/pipeline/process.py`
- `tests/test_gateway_route_coverage.py`

验收：

- 旧路径继续可用。
- 新路径通过同一套 ownership/intercept 逻辑。
- 不出现绕过 Gateway truth 的旁路。

### P1-3 Plan rank 显式化

当前问题：

- `gateway/billing.py` 中存在 `{"free": 0, "plus": 1, "pro": 2}` 硬编码 rank。
- 新增 `enterprise` 之类套餐会扩散修改点。

方案：

1. 在 `PlanDefinition` 增加 `rank: int`。
2. `plan_catalog.py` 为 `free/plus/pro` 填 rank。
3. `billing.py` 从 runtime/catalog 读取 rank。
4. 保持 frontend 仍通过 Gateway plan API 消费，不复制 rank。

涉及文件：

- `gateway/plan_catalog.py`
- `gateway/billing.py`
- `gateway/pricing_schema.py`
- `tests/test_plan_catalog.py`
- `tests/test_billing.py`

验收：

- 新增套餐不需要改 billing rank dict。
- plan truth 仍集中在 Gateway。

### P1-4 TTS Provider 注册表模式

目标：

- 降低新增 provider 的跨文件分发成本。

方案：

1. 先定义 provider protocol 和 registry，不动现有 provider 行为。
2. 将 provider metadata 表驱动：
   - provider code
   - supported modes
   - billed chars multiplier / policy
   - voice match resolver
   - speed capability
3. 逐个 provider 迁移：
   - minimax
   - cosyvoice
   - volcengine
   - mimo
4. 保留旧分支作为 fallback，直到测试覆盖齐。
5. 用 env-driven 灰度控制 registry 生效范围，例如 `AVT_TTS_REGISTRY_PROVIDERS=minimax,cosyvoice`；未列入 provider 继续走旧分支。
6. 回滚方式必须是清空或移除该环境变量，立即退回旧分支，不需要代码回滚。

涉及文件：

- `src/services/tts_provider.py`
- `src/services/tts/tts_strategy.py`
- `src/services/tts/tts_generator.py`
- `src/services/tts/voice_match_resolver.py`
- `gateway/job_intercept.py`
- `tests/test_tts_routing_invariants.py`
- `tests/test_tts_generator.py`

验收：

- 新增 provider 不需要改 5+ 文件。
- 原有 provider 的 billed chars、mode gate、voice matching 不漂移。

### P1-5 关键路径日志统一

目标：

- 不是清空所有 `print`，而是先统一用户任务关键路径。

优先范围：

- `src/pipeline/process.py`
- `src/services/alignment/aligner.py`
- `src/services/gemini/translator.py`
- `gateway/job_intercept.py`
- `gateway/main.py`

方案：

1. 建立小 helper：
   - `get_job_logger(logger, job_id=None, stage=None)`
   - 或使用 `logging.LoggerAdapter`
2. P1 只替换：
   - error / warning
   - metering/reporting
   - external callback
   - startup recovery
3. 普通进度 `print` 后续再迁移，避免一次性噪音 diff。

验收：

- 关键错误可以按 `job_id` / `stage` 检索。
- 不破坏现有 UI log/event 输出。

## P2：结构收敛

### P2-1 `intercept_create_job` 薄切片抽取

原则：

- 不直接上完整 middleware chain。
- 每次只抽一个纯职责块，并保持 route 行为不变。

建议顺序：

1. display name 生成：迁到 `gateway/display_name_orchestrator.py` 或已有模块。
2. source metadata probe：抽成独立 helper。
3. quota reserve/rollback：抽成带上下文的小函数。
4. upstream forwarding：保留在主函数尾部。
5. PG writeback：抽成可测试 helper。

测试：

- `tests/test_gateway_create_job.py`
- `tests/test_gateway_quota.py`
- `tests/test_gateway_suggested_copy_name.py`

验收：

- `intercept_create_job` 行数逐步下降。
- 每个抽取 helper 有独立测试。

### P2-2 `process.py` 先抽小模块，再 stage 化

原则：

- 不直接把 8,430 行切成 7 个 stage。
- 先抽风险低、边界清晰的纯逻辑。

建议顺序：

1. `_report_job_metering` payload builder（P0 已做）。
2. `_rewrite_policy.py`：pre-TTS rewrite 阈值和策略选择。
3. `_speaker_structure.py`：speaker profile/structure 纯分析。
4. `_gateway_callbacks.py`：source metadata / metering / notification callbacks。
5. `_output_publish.py`：与 OutputDispatcher 交界的轻封装。

阶段化条件：

- 上述小模块已有测试。
- `ProcessPipeline.run()` 的调用顺序被 characterization tests 锁住。
- 再引入 `PipelineStage` Protocol。

验收：

- 每次抽取都能通过现有 pipeline tests。
- 不改变 TTS unit、DSP-first、数学 retiming 不变量。

### P2-3 `GeminiTranslator` 分阶段拆分

当前职责：

- translation
- speaker attribution
- LLM call routing / fallback
- checkpoint/cache helpers

建议顺序：

1. 抽 `LLMClientRouter`，只搬 `_call_by_model` / `_call_mimo_text` / `_call_openai_compatible` / fallback。
2. 抽 `GeminiSpeakerAttributionService`，只搬 `infer_speaker_names` / `review_speaker_labels` 及 prompt/validator。
3. `GeminiTranslator` 留 translation provider 行为。
4. 最后收窄 `Any`，不要第一步就做类型大清洗。

测试：

- `tests/test_gemini_translator.py`
- fallback/error classification 新增参数化测试。

验收：

- public API 不变。
- usage metering 不丢。
- fallback 行为一致。

### P2-4 `JobSnapshot` 类型统一

目标：

- 消除 `job_record` 的 dict/object 双重人格。

方案：

1. 定义 `JobSnapshot` dataclass 或 TypedDict。
2. 在 Gateway/Job API 边界统一转换。
3. TTSGenerator、SegmentAligner、JianyingDraftRunner 逐步改为读取 `JobSnapshot`。
4. 保留 adapter 一段时间兼容旧 dict。

验收：

- `_read_job_field` 调用点减少。
- 类型错误更早暴露。

### P2-5 前端数据层收敛

当前问题：

- 前端没有 TanStack Query/SWR 等统一数据层。
- `usePollingTask`、项目列表、任务详情和 support/handoff 面存在多处手写轮询。

方案：

1. 不一次性替换所有 fetch。
2. 先引入 TanStack Query 或等价轻量数据层，覆盖 1-2 个高频轮询入口：
   - `frontend-next/src/app/(app)/workspace/[jobId]/page.tsx`
   - `frontend-next/src/app/(app)/projects/page.tsx`
   - 可选：support handoff 轮询
3. 保留现有 `frontend-next/src/lib/api/*` fetch client，只把请求生命周期交给 query layer。
4. 对正在运行的 job 设置合理 refetch interval；terminal 状态停止轮询。

验收：

- 高频 job detail/projects 页面不再各自手写 4s 轮询。
- loading/error/retry 状态一致。
- 不把商业 plan/pricing/trial 事实写死到 frontend cache 初始值里。

## P3：低优先级清理

### P3-1 `getattr(segment, ...)` 收敛

方案：

- 对固定 `DubbingSegment` 字段改直接访问。
- 对历史兼容字段通过 adapter 或属性方法显式表达。

验收：

- 拼错字段能暴露为测试失败或 AttributeError。

### P3-2 Router 批量注册

方案：

- 将 `gateway/main.py` 的 router include 改成 ordered tuple。
- 保留背景任务 router 必须早于 catch-all 的注释。

验收：

- `tests/test_gateway_route_coverage.py` 通过。
- route 顺序不漂移。

### P3-3 `JobStore` 查询封装

方案：

- 先封装 `get_by_id` / `get_by_id_for_update` / `update_status`。
- 不一次性改所有 query，只改重复度最高的 job ownership / post-edit 路径。

验收：

- `job_intercept.py` 直接 `select(Job).where(Job.job_id == job_id)` 次数下降。

### P3-4 中文文案集中管理

方案：

- 不一次性迁移所有中文字符串。
- 先覆盖营销/支付/审核错误这三类用户可见文案。
- 保持中文优先、自然、符合国内 SaaS/支付预期。

验收：

- 修改支付/试用/审核文案不需要改业务逻辑文件。

### P3-5 配置读取机制统一

方案：

- 先出配置来源矩阵：
  - env
  - `autodub.local.json`
  - `admin_settings.json`
  - `pricing_runtime`
- 定义 `AppConfig` 只作为协议和迁移边界。
- 不在一个 PR 中替换所有 `os.environ`。

验收：

- 新配置项有明确归属。
- Gateway truth / pipeline local config 边界不混淆。

### P3-6 Gateway lifespan 内联任务抽取

当前状态：

- `gateway/main.py` 中仍有 `_periodic_pack_cleanup`、`_periodic_project_cleanup` 两个 inline async function。
- R2 sweeper 已经从 `r2_artifact_sweeper.sweeper_loop` 引入，主问题是 lifespan 可读性和测试边界。

方案：

- 将 pack/project cleanup loop 抽成模块级 helper 或小模块。
- 保留 lifespan 中的启动顺序和 `create_task` name。
- 不在同一 PR 改 cleanup 策略。

验收：

- `gateway/main.py` lifespan 更短。
- cleanup loop 异常日志行为不变。

## 5. 推荐执行顺序

### 第一批 PR：可观测与 CI

内容：

1. P0-1 Gateway startup recovery logging。
2. P0-2 最小 CI。

验证：

```powershell
pytest -q tests/test_gateway_startup_checks.py
pytest -q tests/test_phase1_guards.py tests/test_legacy_cleanup_guards.py
cd frontend-next; npm run lint; npx tsc --noEmit
```

### 第二批 PR：metering 可测试化

内容：

1. P0-3 `_report_job_metering` payload builder。
2. 补 `tests/test_process_pipeline.py` / `tests/test_job_metering_writeback.py`。

验证：

```powershell
pytest -q tests/test_process_pipeline.py tests/test_job_metering_writeback.py
```

### 第三批 PR：post-edit 商业边界测试

内容：

1. P0-4 `_consume_post_edit_tts_usage` 直接单测。
2. 必要时只做最小 bug fix，不做结构调整。

验证：

```powershell
pytest -q tests/test_gateway_editing_commit_sync.py tests/test_gateway_quota.py
```

### 第四批 PR：alignment exception characterization

内容：

1. P0-5 `BaseException` 清理语义测试。
2. 若测试证明可以收窄，再小改实现。

验证：

```powershell
pytest -q tests/test_aligner.py tests/test_aligner_concurrency.py
```

### 第五批 PR：P1 schema 与兼容迁移准备

内容：

1. Metering schema。
2. `/job-api` alias/base URL 配置设计。
3. Plan rank 显式化。

验证：

```powershell
pytest -q tests/test_job_metering_writeback.py tests/test_gateway_route_coverage.py tests/test_plan_catalog.py tests/test_billing.py
```

## 6. 风险控制

### 6.1 避免大爆炸重构

任何超过 500 行 diff 的结构改造都应拆分。尤其是：

- `process.py` stage 化
- `GeminiTranslator` 拆类
- `intercept_create_job` middleware chain
- 配置系统统一

这些只能在 characterization tests 到位后推进。

### 6.2 保持商业事实真源

任何 plan/pricing/trial/entitlement 改动必须满足：

- Gateway 是唯一真源。
- Frontend 只消费 API 返回事实。
- 不在 frontend 写死套餐 rank、试用天数、价格、权益。

### 6.3 保持外部服务 mock 化

新增测试不得依赖：

- 真实 LLM/TTS
- 真实 SMS
- 真实支付
- 真实 R2
- 真实 YouTube 下载

外部接口统一用 monkeypatch/fake/recording runner。

### 6.4 保持用户路径稳定

P0/P1 不改变：

- `/job-api` 旧路径可用性
- Studio post-edit commit 行为
- metering snapshot 既有字段
- Jianying draft delivery 行为

## 7. 验收总表

| 阶段 | 必须完成 | 验收信号 |
|---|---|---|
| P0 | 启动恢复日志、CI、metering builder、post-edit limit tests、BaseException characterization | 关键测试通过，确认问题不再无信号 |
| P1 | Metering schema、`/job-api` 兼容版本化准备、plan rank、TTS registry 初版、关键日志统一 | 契约测试通过，旧路径兼容 |
| P2 | `intercept_create_job` 薄切片、`process.py` 小模块抽取、`GeminiTranslator` 分阶段拆分、`JobSnapshot`、前端数据层收敛 | 大文件行数下降，public API 不变，高频轮询更可控 |
| P3 | `getattr` 清理、router 批量注册、JobStore、中文文案、配置边界、lifespan 内联任务抽取 | 重复和隐式逻辑减少 |

## 8. 暂缓项

以下事项在 P0 完成前不建议启动：

- 全量 pipeline stage framework。
- 全量 middleware chain。
- 删除旧 `/job-api` 路径。
- 全量配置中心。
- 全量 i18n/messages 重构。
- 全量 frontend fetch/query 迁移；P2 只做高频入口试点。
- Team seats、reviewer seats、auto-renew、完整 minute ledger。

## 9. 成功标准

这轮整改成功的标志不是“所有大文件都被拆完”，而是：

- 后台恢复失败可见。
- 关键守卫测试自动跑。
- metering 和 post-edit 商业边界可独立测试。
- alignment 并发异常清理语义被测试锁住。
- 后续大改造有测试护栏，而不是靠人工阅读 8,000 行文件确认没坏。
