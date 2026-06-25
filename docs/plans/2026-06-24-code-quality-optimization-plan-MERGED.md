# 代码质量 / 效率 / 规范 优化方案（合并版 v3）

> 日期：2026-06-24
> 本文是两份独立 2026-06-24 方案的**合并定稿**，取两者所长、纠两者所短：
> - **战略骨架 / 架构不变量 / 收敛方向 / 治理与 Issue 切分** ← 采用 CodeX 版 [`2026-06-24-code-quality-efficiency-standards-optimization-plan(CodeX).md`](2026-06-24-code-quality-efficiency-standards-optimization-plan%28CodeX%29.md)
> - **已逐条核实的具体缺陷 / 可直接粘贴的工具配置 / 准确的规模口径 / 验证纪律** ← 采用 Claude 版 [`2026-06-24-code-quality-efficiency-standards-optimization-plan.md`](2026-06-24-code-quality-efficiency-standards-optimization-plan.md)
> - **关键纠正**：`process.py` 的改造方向**改为遵循既有 ADR Option B 收敛**（`docs/architecture/PROCESS_WORKFLOW_CONVERGENCE.md`），废弃 Claude 版原先「拆成独立 `stages/` 子包」的蓝图——后者与项目既定架构方向相左。
>
> 一句话定位：**CodeX 版是更好的「战略计划」，Claude 版是更好的「落地审计」；本合并版 = CodeX 的方向 + Claude 的弹药。**
>
> **校准记录（CodeX review 2026-06-24，评 8.5→修后 9+/10）**：已采纳 5 处校准——① H1（projects/）经复核已清理、守卫通过，改标已解决；② 全量测试 + coverage **改 nightly/report-only 起步**，不做第一周 PR 硬门；③ ruff **改 report-only + 仅改动文件阻断**，不默认 `--add-noqa` 批量压制；④ file-size-guard 配真实基线白名单（`tools/file_size_baseline.json`，记录行数、禁止增长）；⑤ pytest `addopts` 与"默认排除 slow/real_provider"文字对齐。

---

## 0. 阅读指南

- **本周就该动手** → [§4 本周止血：5 个已验证缺陷](#4-本周止血5-个已验证缺陷必做) + [§5 装质量护栏](#5-装质量护栏不阻断业务)。
- **要可粘配置** → [§10 附录：即用配置](#10-附录即用配置可直接粘贴)。
- **要战略方向 / 拆分顺序** → [§6 主要优化方向](#6-主要优化方向战略骨架) + [§7 路线图](#7-分阶段路线图)。
- **要转工单** → [§8 Issue / PR 切分](#8-issue--pr-切分)。

---

## 1. 执行摘要

本项目已不是「缺测试、缺规范」的早期代码库，而是一个在快速商业化 + 多产品线并行中长成的大型系统。**正确性与安全纪律罕见地优秀**（上一轮审计的全部 P0/P1 已落地），当前真正的风险有两层：

**战略层（结构债在加速，且方向偏离既定架构）**
- 核心模块体积失控且仍在长大：`process.py` 自 2026-03-18 的收敛 ADR 之后**反而 +52%**（→12,806 行），`gateway/job_intercept.py` 6 周 +108%（→6,880 行），`intercept_create_job` 单函数 ~1,627 行。
- **既有 Option B 收敛 ADR 正在被违背**——ADR 明确要求 `process.py`「停止作为独立架构中心演进」，现实却是它持续吸纳新功能。**根因是缺一道「文件不许继续变大」的 CI 门**。
- 整条 Python **没有任何质量门禁**：无 ruff / mypy / black / pre-commit；CI 只跑手挑的 ~14 个守卫测试，全量 8,474 测试从不在 CI 跑。

**战术层（几个"现在就坏"的隐患，纯战略视角会漏掉）**
- 一处**财务静默归零**、一个**付费任务取消失效**的数据竞争、一处 editing **写盘漏 fsync**、一个**数据契约 bug**——全部已逐条核实（§4）。（原列入的「`projects/` 红灯守卫」经 2026-06-24 复核已清理、守卫通过，已不是 open 项。）

**推荐策略（两版一致）**：不大爆炸重写、不把 Gateway 商业事实下沉前端、不把确定性 retiming/对齐迁到 LLM、不在默认测试引入真实外部服务。顺序是：**先止血 + 建可度量基线 → 低风险标准化 → 按真实接缝拆热点 → 推进 Option B 工作流收敛**。

---

## 2. 必须保持的架构不变量

后续所有优化都必须服从这些不变量（违反即视为错误优化）：

- TTS 单元是 `SemanticBlock`，不是字幕行。
- Alignment **DSP-first**；rewrite loop 是 fallback，不是主路径。
- Subtitle retiming 数学化、确定性，**不迁移到 LLM**。
- 主交付物是**剪映 draft**，不是直接渲染 MP4。
- Gateway 是 plan / trial / pricing / entitlement 的**唯一事实源**；前端只消费、不重算。
- Auth / billing / payment 走**增量迁移**，不做大爆炸替换。
- 默认测试与本地路径优先 mock/stub/fake，**不接入真实外部服务**。
- `main.py` 与 `pytest` 必须在干净本地环境可运行。
- 营销 / 认证 / 支付的用户文案中文优先。
- **付费 API 硬约束（项目最高红线）**：MiniMax 付费克隆 / 付费 TTS / 付费 LLM / 付费 ASR **绝不**在 fallback / except / retry / batch 路径自动触发，只走用户显式 consent。本方案所有性能/容错优化均不触碰付费调用点。

---

## 3. 现状快照与维度评分

### 3.1 规模（准确口径——已排除 worktree 副本）

> ⚠️ 口径说明：必须排除 `.codex_worktrees` / `.codex_tmp` / `.claude/worktrees` / `.venv` / `node_modules` 等副本目录，否则文件数/行数会虚高约 2×（codegraph 索引了 8,774 文件含副本，真实 live tree 远小于此）。

| 区域 | 文件 | 行数 | 备注 |
|---|---:|---:|---|
| `src/` | 276 | 93,938 | pipeline + services + modules |
| `gateway/` | 211 | 77,351 | FastAPI 网关 |
| `frontend-next/src/` | 223 | 57,644 | Next 16 / React 19 / TS strict |
| `tests/` | 553 | — | 8,474 个测试（collect-only 6.7s 无导入错误） |

**热点文件（live tree）**：`process.py` 12,806（16× 超 800 行上限）、`job_intercept.py` 6,880（8.6×）、`transcript_reviewer.py` 4,173、`gemini/translator.py` 2,825、`jobs/api.py` 2,645、`traffic_analytics.py` 2,236、`admin_settings.py` 2,034…（共 25+ 文件超标）。前端：`edit/page.tsx` 1,975、`admin/settings/page.tsx` 1,930、`VoiceModifyTab.tsx` 1,519。

### 3.2 维度评分（0–10）

| 维度 | 评分 | 一句话 |
|---|---|---|
| 工具链 / Lint / 规范 | **2.5** | 无 ruff/mypy/pre-commit；CI 不跑全量；ROI 最高入口 |
| 模块 & 文件结构 | **3.5** | 债务加速，且偏离 Option B 方向 |
| 类型安全 / 数据契约 | **3.5** | 无 mypy → 注解零构建期收益；65+ getattr 架空 slots |
| 依赖 / 配置 / 构建卫生 | **3.5** | uv.lock 在 CI/Docker 未生效；.env.example 缺 36 变量；dev bind-mount 仍在 main |
| 重复代码 / DRY | **4.5** | 13 份 admin 鉴权、6 处原子写（1 处漏 fsync） |
| 错误处理 / 静默失败 / 日志 | **4.5** | process.py 233 print/0 logging；计费路径静默盲区 |
| 测试套件质量与覆盖 | **5.0** | 广度优秀但 CI 只跑 14/8474、无 per-test 超时、conftest 空 |
| 性能与效率（后端） | **5.5** | 多处 async 内同步阻塞 + 高频无缓存读 |
| 前端代码质量 | **5.5** | 大组件 + 4 条 react-hooks 规则降 warn |
| 异步 / FastAPI 正确性 | **5.5** | 事件循环阻塞 + 1 个 cancel 数据竞争 |
| 数据库 / 模型 / 迁移 | **6.5** | 纪律良好；少量 CHECK/分页/索引同步缺口 |
| 死代码 / 遗留债 | **6.5** | 边界清晰 |

**加权综合 ≈ 4.8 / 10**：正确性高、安全护栏到位，但「工程可维护性 + 自动化规范」系统性偏低。

---

## 4. 本周止血：5 个已验证缺陷（必做）

> 这些均**当场打开 `file:line` 核实过**，S 级、不改业务逻辑、高 ROI——纯战略视角会漏掉的"现在就坏"的地雷。**H1 经 2026-06-24 复核已清理、守卫通过，标记已解决（保留守卫防回归）；活跃止血项为 H2–H5。**

| # | 问题 | 位置 | 核实 | 修法 |
|---|---|---|---|---|
| H1 | ~~根 `projects/` 空目录致守卫红灯~~ → **已清理、守卫通过**（2026-06-24 复核 `test_no_root_projects_dir` 1 passed） | `tests/test_legacy_cleanup_guards.py:141` | ✅ 已解决 | 无需动作；若 `projects/` 再现则 `rmdir` + 确认 `.gitignore` 用 `/projects/` |
| H2 | `_derive_credits_from_minutes` 异常时**财务静默归零** | `gateway/cost_management.py:839` | ✅ | `except` 加 `logger.exception(...)`；调用侧加 `if credits==0 and minutes>0: logger.error("ZERO_CREDITS_SUSPECT ...")` |
| H3 | 付费任务**取消可被静默吞掉**（数据竞争，测试间歇红） | `regenerate_all_async.py:372` | ✅ | 改「读现状→仅 merge 变更字段→写回」，保留 `cancel_requested` |
| H4 | editing **写盘漏 `fsync`**（断电丢 segments/voice_map） | `editing_segments.py:172` | ✅ | 接入统一原子写工具（见 §10，顺带消除 6 处重复实现） |
| H5 | **数据契约 bug**：`getattr(segment,"en_text")` 读不存在字段恒空 | `aligner.py:361/542/591/778` | ✅ | `DubbingSegment` 加 `en_text` 字段，去 4 处 getattr |

**配套（同批，S）**：H2 同源的 `billing.py:1311`（支付后 bucket 失败缺 `exc_info=True`）、`credits_service.py:121/137/146`（pricing fallback 无 WARNING）。

> H5 影响边界（诚实标注）：`AlignedSegment.en_text` 的下游消费者主要落在 **deprecated** 的 `editor_package_writer._write_srt_from_segments`，今天活影响有限——但它是「`getattr` 在 `slots=True` dataclass 上静默吞缺字段」这一系统性问题（共 65+ 处）的实证，也是**引入 mypy 的最强论据**。

---

## 5. 装质量护栏（不阻断业务）

护栏必须先于结构重构落地——否则重构无回归网，且巨型文件会继续违背 Option B 长大。策略统一为 **report-only → 改动文件阻断 → 全仓阻断** 的渐进棘轮。

### 5.1 三件套（即用配置见 §10）

1. **ruff（lint + format）**：先开 `E/W/F/I/UP`。**基线策略优先 report-only + 仅对新增/改动文件阻断**（`ruff check --exit-zero --output-format=github` 出报告；CI 用 `git diff` 范围对 changed files 阻断），**不默认用 `--add-noqa` 全仓批量压制**——它会把历史问题固化成 noqa 污染代码，仅在确需立刻全仓变绿时作可选加速、且需排期逐步清理。月度加严（`B`/`C4` → `SIM`/`N` → `T20` print）。
2. **mypy（非 strict 起步）**：仅对最干净模块强制（`src/core`、`src/utils`、`src/modules/subtitles`、`src/workflow/output_dispatcher`、`gateway/storage`、`jobs/store.py`），`--ignore-missing-imports --check-untyped-defs` 即可抓住 H5 这类问题。按目录递进。
3. **pre-commit**：ruff + 窄域 mypy + 基础卫生（trailing-whitespace / debug-statements / check-yaml）。

### 5.2 CI 补全（现 CI 只跑 14/8474）

- 新增 `python-lint` job（ruff check + format --check + 窄域 mypy）。
- 新增 `backend-full-suite` job：全量 pytest（排除 PG 集成）。**第一周先 nightly + report-only（不阻断 PR）**，跑出真实 baseline 后再设 `pytest-cov --cov-fail-under` 阈值并逐季棘轮——首版别把「全量 + 覆盖率」直接做成 PR 硬门，否则质量治理第一步就成了 CI 大爆炸。**前置必须装 `pytest-timeout` 并加 `--timeout=120`**，否则会重蹈"全量跑挂死"覆辙（当前 `@pytest.mark.timeout` 是 no-op，无任何 per-test 超时）；用 `pytest-xdist -n auto` 分片压墙钟。
- 新增 **`file-size-guard` job**（关键）：新提交 Python 文件 >800 行即失败，现存超标文件进**冻结白名单**——这正是 Option B「process.py 停止长大」的强制执行器（见 §6.2）。
- 前端 `frontend` job 追加 eslint warning budget 门。

### 5.3 pytest 配置（当前完全缺失）

加最小 `[tool.pytest.ini_options]`：注册 marks（`postgres`/`timeout`/`slow`/`real_provider`/`guard`/`contract`/`integration`）、设 `asyncio_mode`、默认排除慢/真实 provider 标记。conftest 收口高频共享 fixture（现 16 行零共享，114 fixture 散在 69 文件）。

---

## 6. 主要优化方向（战略骨架）

### 6.1 深模块原则：热点文件承担过多职责

`process.py` / `job_intercept.py` / `jobs/api.py` / `JobService` / Next 编辑页 / 语音选择页都是同一模式：内部多条业务流、状态共享面大、新增行为靠加分支、测试只能覆盖大入口。

**对策**：用「深模块」策略——外部入口稳定，内部复杂性藏到**窄接口**之后。每个新接口必须：暴露少量参数/返回；可独立测试；不复制事实源；不把过渡期兼容逻辑扩散到更多调用点。

**推荐拆分顺序**：

| 优先级 | 目标 | 建议接缝 |
|---|---|---|
| P0 | `gateway/job_intercept.py` | route family 模块化：job read / create-admission / artifacts-download / voice review / post-edit policy / metering callbacks（`gateway/main.py` 路由路径不变） |
| P0 | Next 编辑页 | route shell + hooks + panels（segments / bulk replace / commit flow / selection / job sync） |
| P1 | `jobs/api.py` | dispatch table + resource handlers（**不**替换 HTTP 框架） |
| P1 | `JobService` post-edit | 抽 `EditingApplicationModule` 窄接口，可用 fake `JobStore` 单测 |
| P1 | `process.py` | **延续 Option B 收敛**（见 §6.2，非独立 stages 拆分） |
| P2 | `gateway/models.py` | 按领域分文件，先保护 Alembic metadata（re-export 保兼容） |

> **守卫**：read route 不得触发 settlement；list/get mirror 不得改 payment/credit 事实；post-edit whitelist 保持前后端 path parity；admin gate coverage 测试必跑。每次只迁一组、先 contract 后移动、迁移前后跑相同路由测试。`job_intercept.py` 目标降到 4.5k 行以下。

### 6.2 `process.py`：遵循既有 Option B 收敛（纠正 Claude 版）

> **这是本合并版对 Claude 原方案的关键纠正。** Claude 版曾建议把 `process.py` 拆成独立 `stages/` 子包 + `PipelineContext`——但这与项目 2026-03-18 的架构决策 [`docs/architecture/PROCESS_WORKFLOW_CONVERGENCE.md`](../architecture/PROCESS_WORKFLOW_CONVERGENCE.md) **方向相左**。正确做法是 Option B：让 `process.py` **退化为兼容壳**，逐步消费 `ProjectWorkflow.run_build()` 与 `OutputDispatcher`，停止作为第二套架构中心。

**现状（已核实）**：`process.py` 已 import 并调用 `OutputDispatcher`（`process.py:33` + `:11056`），`ProjectBuilder` / `project_shape_helpers` 已承接 canonical 形状规则——收敛**已在桥接态**。但 ADR 之后 `process.py` 仍 +52%，说明**收敛停滞甚至反向**。

**收敛顺序（严格按 ADR）**：
1. **输出收敛优先**：更多输出走 `OutputDispatcher`，减少 legacy 输出分支，可见行为不变。
2. **资产/构建收敛**：构建阶段迁 `ProjectWorkflow.run_build()`，canonical artifact 命名/位置，把形状规则移出 `process.py`。
3. **review gate 收敛**：transcript/speaker/translation review 边界向 workflow-owned 状态靠拢，保持 `review_state.json` 行为稳定。
4. **物理退役最后**：收缩兼容 shim、删死分支、简化 `main.py` 命令内核。**不要从这步开始**。

**当前仍属于 `process.py` 的**（暂不动）：YouTube 源事实解释、download/cache 决策、review gate 行为、TTS/alignment/runtime 恢复。

**强制执行（合并版新增的关键一招）**：用 §5.2 的 **`file-size-guard` + 规则「新功能不得直接加进 `process.py`，除非是兼容入口参数映射」** 来阻止它继续长大——没有这道门，Option B 会像过去 3 个月一样被违背。每完成一个收敛切片，`process.py` 行数与其直接调用的底层模块数应**下降**。先给 `main.py --help` / 主要 CLI 参数 / 输出目录 / 剪映 draft 生成补 golden/contract 测试，再动收敛。

### 6.3 数据一致性与状态写入

`JobStore` 已有 file_lock / 原子保存 / `update_job(mutator)` / list cache。下一步是**完成调用方迁移**而非新增能力：
- 扫描 `require_job(...)` 后紧跟 `save_job(...)` 的 read-modify-save，能迁的改 `update_job`，不能迁的注明原因。
- **统一 JSON 原子写**（§10）：当前 `atomic_io.py` / `JobStore._write_json_atomic` / `editing_segments._atomic_write_json`（**漏 fsync**，即 H4）/ sidecar / backfill 等 6 处独立实现，收口为单一 canonical helper（支持 `Path`/`dict`/`list`/`fsync`/同目录临时文件）。
- logs/events 增量化（`since`/`cursor`），前端与 Gateway 不再轮询全量事件。

### 6.4 规范标准化（低风险、可批量）

- **统一 helper**：`coerce_int/float/bool`、`_normalize_optional_text`、error payload、datetime ISO 序列化——各有多处重复，收口到 `src/utils/`。
- **错误载荷统一**：`{error_code, message(中文), detail(无 secret), retryable, user_action}`；`error_code` 稳定英文可测试。
- **导入边界**：消除散落的 `sys.path.insert` 与 `from src.services` / `from services` 双导入；运行时统一以 `src` 为 import root，Gateway 仅在单一 bootstrap 注入路径，测试只在 conftest 设 path（加守卫禁止新测试重复 `sys.path.insert`）。
- **provider 注册表**：TTS `if/elif` 分发改 `dict[str, Protocol]` 注册表（消除「新增 provider 改 5 文件」）。
- **配置收口**：gateway 已用 Pydantic Settings（正确）；src 侧 93 处裸 `os.environ.get` 收口到 `src/core/env.py`。
- **plan_rank 等硬编码**：`PlanDefinition` 加 `rank` 字段 + `POST_EDIT_LIMITS` 移入 plan_catalog，消除多处重复。

### 6.5 前端

- **编辑页 route shell 化**：抽 `useEditingJobSync` / `useEditingSegments` / `useBulkReplace` / `useCommitFlow` + `SegmentEditorPanel` / `EditingToolbar`，`page.tsx` 降到 ~400 行；合并 H3 同类的两段 ~70 行重复轮询。
- **语音选择复用**：`VoiceSelectionPanel` / `VoiceModifyTab` 抽 `useVoiceCandidates` + `VoiceProviderTabs` + `VoiceCandidateList`（去重 400+ 行），共享 `SpeakerPayload` 类型（现两处已分叉）。
- **修 react-hooks 根因**：`TranslationForm.tsx:293-311` 的 3 个 set-state-in-effect 删掉改 handleSubmit 条件求值，然后把 eslint 4 条规则**恢复 error**。
- **/gateway 路径统一走 `gatewayClient`**（现 12+ 处裸 fetch 无超时）；critical 请求加 AbortController；修 `smartPreviewCloneCostLabel` 错误标志 bug（FE-005）。
- **不引入** 重量级状态库；如需缓存，用 module-level TTL Map 即可（CLAUDE.md 已定薄 fetch wrapper）。

### 6.6 性能：先观测，再优化

**原则**：不降对齐准确性、不绕 DSP-first、不让 LLM 负责确定性 retiming、不把失败重试变静默吞错。

- **先建 benchmark harness**：`scripts/benchmark_pipeline_stage_timings.py` 输出各 stage 计时 JSON 到 `reports/benchmark/`，CI 只存 artifact 不阻断。
- **可立即做的有界优化**（均不碰付费调用点）：`minimax_voice_selector` 加 120s TTL 缓存（复用 `voice_speed_catalog` 模式）；`admin_settings.load_settings` 加 5s TTL（61 调用点）；`intercept_list_jobs` 去全表 `SELECT job_id` 扫描；Pan/clone/disk 多处 async 内同步 I/O 用 `asyncio.to_thread` 包裹；替换废弃 `asyncio.get_event_loop()`。
- **DB**：连接池加 `pool_pre_ping` + `statement_timeout`；`GET /users` 加分页；`CreditsLedger.direction` 加 CHECK；`FreeServiceDailyUsage`/`BackupRecord`/`PanOauthState` 补 `__table_args__`（防 autogenerate 误删索引）；alembic 加 `heads` 单头断言 + `036_payment` 迁移幂等化。

### 6.7 依赖 / 配置 / 构建卫生

- **本周顺手做**：删 docker-compose 3 个开发期 code bind-mount（生产可变镜像隐患，CLAUDE.md 明令删）；删 Dockerfile `curl|sh` 装 Deno（死代码+供应链+100MB）；`cloudflared:latest` pin 版本；补 `.env.example` 36 个缺失生产变量（含所有 API Key）。
- **中期**：Docker/CI 改 `uv sync --frozen`，pyproject 加版本下界；`pyJianYingDraft` 纳入 pyproject。

### 6.8 错误处理与日志

- API 错误统一标准载荷（§6.4）；服务路径逐步 print→logger（随 §6.2 stage 收敛同批做，注入 `extra={"job_id","stage"}`）；支付/额度/trial/settlement 必须有 audit log；日志不含 token/cookie/密钥/完整支付凭证。

### 6.9 文档治理

- 本文作为 **Current Quality Roadmap**，每完成阶段更新状态。
- 历史审计（2026-05-07/10/21）逐项标注 done / superseded / still-relevant / intentionally-deferred。
- 重大架构决策进 ADR（process.py 收敛 / Gateway route family / Job API 是否迁 FastAPI / JSON store 是否迁 DB）。

> **关于 CSRF SameSite（两版分歧的裁定）**：当前 `SameSite=Lax + 同源写守卫` 是**有意的当前阶段决策**（CodeX 观点，更准），**不**列为 P0 缺陷。早期 plan-audit 曾把 `strict` 列为目标——按「intentionally-deferred」处理，仅在威胁模型变化时再评估。

---

## 7. 分阶段路线图

| 阶段 | 周期 | 目标 | 内容 |
|---|---|---|---|
| **止血** | 本周 | 修「现在就坏」+ 装护栏 | §4 五条已验证缺陷 + §6.7 本周项 + ruff/全量测试 job/**文件行数门** |
| **Phase 0：基线** | 1–2 天 | 不改行为，只建可观测/标准 | ruff report-only、pytest markers、`scripts/check_quality.*`、记录大文件/测试耗时/CI 耗时 baseline、给 `Any`/`noqa`/`type:ignore` 建 debt baseline |
| **Phase 1：低风险标准化** | 1 周 | 减重复与不一致，不碰大业务流 | 统一原子写/coerce/error payload、导入边界守卫、前端 critical response parser、测试按 marker 分类 |
| **Phase 2：热点深挖** | 2–3 周 | 拆最拖迭代速度的热点 | `job_intercept.py` 先拆 post-edit route family、编辑页 hooks/panels、语音选择共享模块、`jobs/api.py` dispatch table、`JobService` post-edit 抽模块 |
| **Phase 3：收敛 + 性能可观测** | 1 个月 | 降第二架构风险、用数据优化 | **process.py Option B 输出/构建收敛**、benchmark harness、logs/events cursor 化、DB pool 设置化 |
| **Phase 4：中长期治理** | 后续 | contract 稳后再大调 | Job API 迁 FastAPI（前提：route contract 补齐）、JSON store 迁/混合 DB、OpenAPI→TS contract、全仓 ruff/mypy/coverage 阻断 |

---

## 8. Issue / PR 切分

**第 0 批（本周止血，新增于 CodeX 原列表之前）**：
- ~~I0a：删根 `projects/` 修红灯守卫（H1）~~ → **已完成**（2026-06-24 已清理、守卫通过）；**保持守卫，若目录再现则清理**，不再列为本周 open 动作
- I0b：计费 3 处静默失败补日志 + ZERO_CREDITS_SUSPECT 告警（H2）
- I0c：`regenerate_all_async` 修 cancel 竞争（H3）
- I0d：统一原子写补 fsync（H4，与 I4 合并）
- I0e：`DubbingSegment` 加 en_text 去 getattr（H5）
- I0f：删 dev bind-mount + Deno + pin cloudflared + 补 .env.example（§6.7）

**第 1 批（CodeX 原 12 Issue，保留）**：

| # | 标题 | 完成标准 |
|---:|---|---|
| 1 | 质量基线脚本（ruff+markers+check_quality） | 一条命令跑核心 guard/lint/typecheck，不破坏现有 CI |
| 2 | 定义 pytest markers 并标记核心守卫 | `pytest -m "guard or contract"` 可运行 |
| 3 | ruff report-only 引入 | 新增 lint 报告，不阻断历史问题 |
| 4 | 统一 JSON atomic write helper（含 fsync） | helper 支持 fsync/Path/list；关键调用迁移（含 H4） |
| 5 | 统一 coerce/normalize helper | 删≥3 处重复，边界输入有测试 |
| 6 | Job API logs/events cursor | 支持 since/cursor/tail，旧调用兼容 |
| 7 | Gateway post-edit route family 拆分 | 路由不变；whitelist/path parity/admin gate 测试通过 |
| 8 | 编辑页 route shell 化 | `page.tsx` ≤ ~400 行，hooks 可单测 |
| 9 | 语音选择共享模块 | TranslationForm/VoiceModifyTab 复用候选逻辑 |
| 10 | JobService post-edit 应用模块 | post-edit plan/application 可独立测试 |
| 11 | **process 输出路径收敛第一刀（Option B Step 1）** | 新旧输出 contract 测试通过；process.py 行数下降 |
| 12 | Pipeline stage benchmark | 输出 stage timing JSON，不作普通 CI 阻断 |
| 13（新增）| **file-size-guard CI 门**（冻结现存超标文件） | 新文件 >800 行即失败；process.py 不再增长 |

---

## 9. 不该现在做的事

- 不全仓大重构；不立即把 Job API 迁 FastAPI（除非 route contract 先补齐）；不全仓 strict mypy；不把所有 JSON state 立即迁 DB。
- 不引入重量级前端状态库来解决单页过大；不为了减文件数而合并模块。
- **不**把 `process.py` 拆成与 Option B 相左的独立 `stages/` 架构。
- 不用 LLM 替代确定性 retiming / DSP 主路径；不在默认测试接入真实支付/TTS/LLM/R2/Pan/YouTube。
- 不重复提出已完成项（JobStore.update_job、usePollingTask/useBackgroundTask、历史 P0 安全修复均已落地）。

---

## 10. 附录：即用配置（可直接粘贴）

### 10.1 `pyproject.toml` dev 依赖

```toml
[project.optional-dependencies]
dev = [
    "pytest", "pytest-asyncio", "aiosqlite",
    "ruff>=0.5.0",
    "mypy>=1.11",
    "pytest-cov>=5.0",
    "pytest-timeout>=2.3",   # per-test 超时——当前未装，导致 @pytest.mark.timeout 是 no-op
    "pytest-xdist>=3.6",     # -n auto 分片跑全量 8,474 测试
]
```

### 10.2 `[tool.ruff]`

```toml
[tool.ruff]
target-version = "py312"
line-length = 120

[tool.ruff.lint]
select = ["E", "W", "F", "I", "UP"]
ignore = ["E501", "E402", "E711", "E712", "F401"]

[tool.ruff.lint.per-file-ignores]
"gateway/alembic/versions/*.py" = ["E501", "I001", "F401"]
"tests/*.py" = ["S101", "ANN"]
"gateway/scripts/*.py" = ["T201"]

[tool.ruff.format]
quote-style = "double"

[tool.ruff.lint.isort]
known-first-party = ["src", "gateway", "core", "services", "utils"]
```

建基线（report-only 优先）：`ruff check src/ gateway/ --exit-zero --output-format=github`（出报告不阻断）+ `ruff format src/ gateway/ --diff`（先看 diff）；CI 仅对改动文件阻断（`git diff --name-only origin/main... | grep '\.py$' | xargs -r ruff check`）。⚠️ `--add-noqa` 会批量固化历史问题为 noqa，**非默认推荐**，仅作可选加速且需后续清理。

### 10.3 `[tool.mypy]`

```toml
[tool.mypy]
python_version = "3.12"
warn_unused_ignores = true
check_untyped_defs = true
ignore_missing_imports = true
exclude = ["gateway/alembic/", "\\.codex_worktrees/", "\\.claude/", "\\.codex_tmp/"]

[[tool.mypy.overrides]]
module = ["src.core.*", "src.utils.*", "src.modules.subtitles.*",
          "gateway.storage.*", "src.services.llm.*", "src.services.jobs.store"]
disallow_untyped_defs = true
warn_return_any = true

[[tool.mypy.overrides]]
module = ["assemblyai.*", "dashscope.*", "google.genai.*", "boto3.*", "botocore.*", "yt_dlp.*", "pydub.*"]
ignore_missing_imports = true
```

### 10.4 `[tool.pytest.ini_options]`

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
# 默认排除 慢/真实 provider/benchmark；CI 需要时显式 -m 覆盖（如 nightly 全量、-m postgres）
addopts = "-p no:cacheprovider -m 'not slow and not real_provider and not benchmark'"
markers = [
    "unit: 纯函数/小模块",
    "contract: API/schema/status/path parity",
    "guard: 架构守卫，禁止回退",
    "integration: 多模块本地集成",
    "postgres: 需要真实 PostgreSQL（CI 单独 job）",
    "slow: 长耗时",
    "real_provider: 真实外部服务，默认禁止",
    "benchmark: 性能基准，不作普通 pass/fail",
]
```

### 10.5 `.pre-commit-config.yaml`

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.5.7
    hooks: [{id: ruff, args: [--fix]}, {id: ruff-format}]
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v4.6.0
    hooks:
      - {id: trailing-whitespace}
      - {id: end-of-file-fixer}
      - {id: check-merge-conflict}
      - {id: check-yaml}
      - {id: check-toml}
      - {id: debug-statements}
  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.11.0
    hooks:
      - id: mypy
        files: ^src/core/|^src/utils/|^gateway/storage/|^src/services/llm
        additional_dependencies: ["pydantic>=2.11", "pydantic-settings>=2.9"]
```

### 10.6 `.github/workflows/ci.yml` 新增 job

```yaml
  python-lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12", cache: pip }
      - run: python -m pip install ruff mypy pydantic pydantic-settings
      # 首期：仅对改动文件阻断 ruff + 全仓 report-only（历史问题不阻断）。Phase 2 再改全仓阻断。
      - name: Ruff (changed-files block + full-repo report-only)
        run: |
          git fetch origin "${{ github.base_ref }}" --depth=1 || true
          FILES=$(git diff --name-only "origin/${{ github.base_ref }}...HEAD" -- '*.py' | tr '\n' ' ')
          if [ -n "$FILES" ]; then ruff check $FILES --output-format=github && ruff format --check $FILES; fi
          ruff check src/ gateway/ --exit-zero --output-format=github   # 全仓只报告，不阻断
      # mypy 本就窄域（仅最干净模块），全量阻断与策略一致
      - run: mypy src/core/ src/utils/ src/services/llm/ gateway/storage/ --ignore-missing-imports --check-untyped-defs

  # 首周 report-only：continue-on-error 不阻断 PR（或整体挪到下方 nightly workflow）。
  # 跑出真实 baseline 后，删除 continue-on-error 并把 --cov-fail-under 设为实测值，逐季棘轮。
  # 别第一周就把「全量 + 覆盖率」做成 PR 硬门。
  backend-full-suite:
    runs-on: ubuntu-latest
    continue-on-error: true        # ← 首周不阻断；稳定后删除本行转为硬门
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12", cache: pip }
      - run: python -m pip install -r requirements-dev.txt -r gateway/requirements.txt pytest-cov pytest-timeout pytest-xdist
      - run: >
          pytest -q -n auto --timeout=120 --timeout-method=thread
          --ignore=tests/test_phase43a_pr2_reservation_pg_atomic.py
          --ignore=tests/test_phase43b_voice_cleanup_pg_concurrent.py
          -m "not real_provider and not benchmark"
          --cov=src --cov=gateway --cov-report=term-missing
          # baseline 稳定后追加： --cov-fail-under=<实测值>

# 或独立 nightly workflow（首周更稳）：
# on: { schedule: [{ cron: "0 18 * * *" }] }   # 每日定时跑全量，不挂在 PR 上

  file-size-guard:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: File-size ratchet (新文件 ≤800；白名单文件不许超过基线行数)
        run: |
          python - <<'PY'
          import json, pathlib, sys
          BASELINE = json.load(open("tools/file_size_baseline.json", encoding="utf-8"))
          targets = list(pathlib.Path("src").rglob("*.py")) + list(pathlib.Path("gateway").rglob("*.py")) + [pathlib.Path("main.py")]
          viol = []
          for p in targets:
              rel = str(p).replace("\\", "/")
              n = sum(1 for _ in open(p, encoding="utf-8", errors="ignore"))
              cap = BASELINE.get(rel, 800)   # 白名单文件=其基线行数；其余=800 上限
              if n > cap:
                  viol.append((rel, n, cap))
          if viol:
              print("File exceeds size budget (new file >800, or whitelisted file grew past baseline):")
              for f, n, cap in viol: print(f"  {f}: {n} > {cap}")
              sys.exit(1)
          print("file-size ratchet OK")
          PY
```

> ⚠️ **前置（否则 CI 直接 FileNotFoundError）：** guard 依赖 `tools/file_size_baseline.json`，该文件需先生成并提交。规则=白名单文件**只许变小**（缩小后同步下调对应值，逐步清零即恢复 800 上限），非白名单文件 ≤800。这样既阻止 `process.py`/`job_intercept.py` 继续长大，又记录了每个文件的当前行数。

落地第一步——生成并提交基线（之后只手工下调）：

```bash
python - <<'PY'
import json, pathlib
b = {}
targets = list(pathlib.Path("src").rglob("*.py")) + list(pathlib.Path("gateway").rglob("*.py")) + [pathlib.Path("main.py")]
for p in targets:
    n = sum(1 for _ in open(p, encoding="utf-8", errors="ignore"))
    if n > 800:
        b[str(p).replace("\\", "/")] = n
pathlib.Path("tools").mkdir(exist_ok=True)
json.dump(dict(sorted(b.items(), key=lambda kv: -kv[1])),
          open("tools/file_size_baseline.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
print("wrote tools/file_size_baseline.json:", len(b), "entries")
PY
git add tools/file_size_baseline.json   # 提交后 file-size-guard job 才会绿
```

> 下面是 2026-06-24 实测结果（42 条），可用上面脚本复现、或直接复制为 `tools/file_size_baseline.json`：

```json
{
  "src/pipeline/process.py": 12806,
  "gateway/job_intercept.py": 6880,
  "src/services/transcript_reviewer.py": 4173,
  "src/services/gemini/translator.py": 2825,
  "src/services/jobs/api.py": 2645,
  "gateway/traffic_analytics.py": 2236,
  "gateway/admin_settings.py": 2034,
  "gateway/anonymous_preview_api.py": 1970,
  "src/services/jobs/editing_segments.py": 1942,
  "gateway/user_voice_api.py": 1907,
  "src/services/control_panel.py": 1906,
  "src/services/jobs/service.py": 1902,
  "main.py": 1868,
  "src/services/tts/tts_generator.py": 1856,
  "gateway/models.py": 1832,
  "gateway/credits_service.py": 1767,
  "gateway/billing.py": 1623,
  "gateway/admin_smart_analytics_api.py": 1539,
  "src/services/jobs/jianying_draft_runner.py": 1530,
  "src/modules/media_understanding/providers.py": 1521,
  "gateway/cosyvoice_clone/api.py": 1518,
  "src/services/alignment/aligner.py": 1393,
  "src/services/jobs/editing_commit.py": 1267,
  "gateway/cost_management.py": 1238,
  "src/services/jobs/review_actions.py": 1202,
  "gateway/user_voice_service.py": 1176,
  "src/services/jobs/process_runner.py": 1150,
  "gateway/voice_catalog_api.py": 1126,
  "src/services/jobs/user_edit_audit.py": 1114,
  "gateway/pan/backup_executor.py": 1033,
  "gateway/admin_support_api.py": 1015,
  "src/modules/output/editor/editor_package_writer.py": 1009,
  "gateway/chunked_upload_store.py": 950,
  "gateway/voice_selection_api.py": 935,
  "src/services/content_compliance.py": 905,
  "gateway/smart_clone_reservation_service.py": 904,
  "src/services/voice_clone.py": 900,
  "src/services/tts_provider.py": 898,
  "src/services/assemblyai/transcriber.py": 884,
  "src/modules/workflow/project_workflow.py": 852,
  "gateway/admin_disk_api.py": 826,
  "src/services/smart/auto_voice_review.py": 825
}
```

### 10.7 关键 helper 片段

统一原子写（修 H4 漏 fsync，收口 6 处实现）：

```python
# src/utils/atomic_io.py
def atomic_write_json(target_path, data, *, sort_keys=False, indent=2) -> None:
    target = Path(target_path); target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(data, ensure_ascii=False, indent=indent, sort_keys=sort_keys).encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(encoded); f.flush(); os.fsync(f.fileno())   # ← editing_segments 缺的就是这行
        os.replace(tmp, target)
    except Exception:
        with contextlib.suppress(OSError): os.unlink(tmp)
        raise
```

---

## 11. 风险与防回退守卫

| 风险 | 防护 |
|---|---|
| 大拆分引入行为回退 | 每次只迁一个 route family / 一个 UI flow；先 contract 后移动；迁移前后跑相同测试 |
| Gateway 商业事实漂移到前端 | 保留并加强 frontend no-estimator / plan catalog parity 守卫 |
| process.py 收敛破坏 CLI | `main.py --help`、golden output、剪映 draft contract 必跑 |
| process.py 继续违背 Option B 长大 | **file-size-guard CI 门** + 「新功能不直接进 process.py」规则 |
| Provider 真实调用进测试 | `real_provider` marker 默认禁止 |
| ruff/mypy 一次性制造噪声 | report-only + changed-files 阶段化棘轮 |
| 全量 CI 跑挂死 | **先装 pytest-timeout + `--timeout`**，再开 backend-full-suite |
| route 拆分丢权限 | admin gate coverage + route registration test 必跑 |
| 性能优化破坏准确性/触发付费 API | benchmark 只观察；优化不碰付费调用点（守付费 API 硬约束） |
| 多 agent 改同一工作树冲突 | 重构在独立 worktree + feature 分支；显式 pathspec 提交（勿 `git add .`） |

---

## 12. 推荐验证命令

```bash
# 后端（按改动范围选）
python main.py --help
pytest -q tests/test_legacy_cleanup_guards.py tests/test_phase1_guards.py tests/test_status_vocab_in_sync.py tests/test_admin_gate_coverage.py
pytest -q -m "not slow and not real_provider and not benchmark"
ruff check src gateway && ruff format --check src gateway
mypy src/core src/utils gateway/storage --ignore-missing-imports --check-untyped-defs

# 前端
cd frontend-next && npm run lint && npx tsc --noEmit

# PG / 性能
pytest -q -m postgres
python scripts/benchmark_pipeline_stage_timings.py --fixture tests/fixtures/benchmark/<case>
```

---

## 13. 最终判断

下一步的重点不是「加更多抽象」，而是把已自然形成的业务接缝变成**窄接口**、用测试和工具**固定下来**，同时**重新落实早已决定却被违背的 Option B 收敛**。优先级：

1. **本周止血**（§4 五条已验证缺陷）+ 装护栏（ruff / 全量测试 job / **文件行数门**）。
2. Gateway `job_intercept.py` route family 拆分 + 前端编辑页/语音选择拆分。
3. JSON 原子写 / 错误载荷 / 导入 / helper 规范化。
4. **`process.py` 按 Option B 输出→构建→review 收敛**，并用文件行数门阻止其再生长。
5. 建立性能 benchmark 后再优化 pipeline 与轮询。

在不破坏商业化迁移、不影响剪映 draft 主目标、不牺牲架构不变量的前提下，逐步降低后续迭代成本。

---

## 附录 A：两份源方案的关系与本合并的取舍

| 维度 | CodeX 版 | Claude 版 | 本合并版采用 |
|---|---|---|---|
| 架构不变量 / Option B 收敛方向 | ✅ 强，且对齐既有 ADR | ❌ 漏 ADR，process.py 蓝图相左 | **CodeX**（§2、§6.2）|
| 治理 / Issue / PR 切分 / 不该做 | ✅ 完整 | 较弱 | **CodeX**（§8、§9）|
| 保守排序（先观测后优化） | ✅ | 偏激进 | **CodeX**（§6.6、§7）|
| 已验证具体缺陷（含财务/红灯 CI） | ❌ 零具体 bug | ✅ 5 条已核实 | **Claude**（§4）|
| 即用配置（ruff/mypy/pre-commit/CI） | ❌ 只列规则名 | ✅ 可粘 | **Claude**（§10）|
| 规模口径准确性 | ⚠️ 含 worktree 副本虚高 2× | ✅ 排除副本 | **Claude**（§3.1）|
| CSRF SameSite 定性 | ✅ 有意决策 | ⚠️ 误列待修 | **CodeX**（§6.9）|
| 验证纪律 / 缺口披露 | 未跑未核实 | ✅ 跑 collect-only + 复核 + 披露 2 个未完成 agent | **Claude** |

> 调查方法：Claude 版由 13 路并行 Sonnet 子 agent 按维度调查 + 主模型逐条核实最高危新结论（其中测试维度调查 agent 与审查 agent 未完成，由主模型亲自补做并如实披露）。CodeX 版为架构师视角的整体评估。本合并版由主模型在核实 CodeX 的 Option B 关键论据为真（ADR 与代码均存在、收敛进行中）后定稿。

---

## 附录 B：完整发现清单（123 条，按维度，含 file:line 与工时）

> 这是本方案的**完整可执行底账**——前面 §4–§9 是战略与排序，本附录是逐条发现。每条来自 12 路维度调查的结构化输出（+ 测试维度由主模型补做）。位置列只列前 1–2 个锚点，完整 locations / problem / impact / recommendation 见 Claude 原版各维度结论。级别：CRITICAL/HIGH/MEDIUM/LOW；工时：S(<1h)/M(半天–1天)/L(数天)/XL(专项 sprint)。
>
> 统计：123 条 = 112 条（下列 12 维度，含上一轮仍 OPEN 11 项）+ 6 条 `TEST-*` + 5 条止血 `H1–H5`（见正文 §4，此处不重复）。

### 止血项（已验证，详见正文 §4）

`H1` 根 projects/ 红灯守卫 → **已解决**（2026-06-24 已清理、守卫通过；保持守卫防回归） · `H2` cost_management 财务静默归零 · `H3` regenerate cancel 竞争 · `H4` editing 漏 fsync · `H5` en_text 契约 bug。

### 测试套件质量与覆盖（5.0/10，6 条）

| ID | 级别 | 问题 | 位置 | 工时 |
|---|---|---|---|---|
| TEST-01 | HIGH | CI 只跑 ~14 个目标 / 全量 8,474 从不在 CI 运行 | `.github/workflows/ci.yml:27-47` | S |
| TEST-03 | HIGH | 无 per-test 超时：pytest-timeout 未装 → `@pytest.mark.timeout(15)` 是 no-op，挂住的测试永久 hang | `tests/test_process_runner_watchdog.py:119` | S |
| TEST-02 | MEDIUM | 无任何 pytest 配置；mark 未注册（postgres/timeout 报 warning）；asyncio_mode 未设 | `pyproject.toml（无 [tool.pytest]）` | S |
| TEST-04 | MEDIUM | conftest 仅 16 行零共享 fixture；114 fixture 散在 69 文件 | `tests/conftest.py` | M |
| TEST-05 | MEDIUM | 慢/外部面大（264 requests/urlopen、32 sleep、30 subprocess 文件）；`test_process_pipeline.py` 单文件 >200s | `tests/test_process_pipeline.py` | M |
| TEST-06 | MEDIUM | pytest-cov 未装，无覆盖率基线/门 | `pyproject.toml` | S |

### 模块 & 文件结构（3.5/10，13 条）

| ID | 级别 | 问题 | 位置 | 工时 |
|---|---|---|---|---|
| STRUCT-01 | CRITICAL | process.py 12,806 行：run() 内联 12 个 pipeline stage（**改造按 §6.2 Option B 收敛，非独立 stages 拆分**） | `src/pipeline/process.py:2833 (run())` | XL |
| STRUCT-02 | CRITICAL | job_intercept.py 6,880 行（+108%）：8 个关注点混杂；intercept_create_job ~1,627 行 | `gateway/job_intercept.py:1565` | XL |
| STRUCT-03 | HIGH | transcript_reviewer.py 4,173 行：legacy 单次 + 三-pass + speaker verifier 混合 | `src/services/transcript_reviewer.py:916` | L |
| STRUCT-04 | HIGH | GeminiTranslator God Class 2,825 行：翻译+说话人推断+LLM 路由+checkpoint 四职责 | `src/services/gemini/translator.py:409` | L |
| STRUCT-05 | HIGH | jobs/api.py 2,645 行：_build_job_api_handler 内嵌 ~2,200 行路由 dispatch 内部类 | `src/services/jobs/api.py:210` | M |
| STRUCT-06 | HIGH | tts_generator.py 1,856 行：3 provider + 速度决策 + 并发调度混合 | `src/services/tts/tts_generator.py:1309` | M |
| STRUCT-07 | HIGH | jobs/service.py 1,902 行：editing 工作流 + TTS regen + voice map + 清理混合 | `src/services/jobs/service.py:84` | M |
| STRUCT-08 | MEDIUM | control_panel.py 1,906 行：830 行 HTML/CSS/JS 内嵌 Python 字符串 | `src/services/control_panel.py:275-1105` | S |
| STRUCT-09 | MEDIUM | main.py 1,868 行：CLI + demo 工作流 + error format + voice registry 混合 | `main.py:142` | M |
| STRUCT-10 | MEDIUM | traffic_analytics.py 2,236 行：3 个独立分析引擎 + 重复日志解析 | `gateway/traffic_analytics.py:807` | M |
| STRUCT-11 | MEDIUM | admin_settings.py 2,034 行：691 行 Pydantic 模型 + 4 类 admin API 混合 | `gateway/admin_settings.py:127` | M |
| STRUCT-12 | MEDIUM | credits_service.py 1,767 行：shadow 状态机 + 结算 + bucket 初始化（金融，先补测试） | `gateway/credits_service.py:871` | M |
| STRUCT-13 | LOW | editing_segments.py 1,942 行：split_many 163 行 + journal 逻辑耦合 | `src/services/jobs/editing_segments.py:1407` | S |

### 重复代码 / DRY（4.5/10，7 条）

| ID | 级别 | 问题 | 位置 | 工时 |
|---|---|---|---|---|
| DRY-01 | HIGH | 13 份 `_require_admin`/`_is_admin` 副本，行为已分叉（安全维护风险） | `gateway/*_api.py（13 处）` | M |
| DRY-02 | HIGH | 6 处原子 JSON 写实现，其中 editing_segments **漏 fsync**（= H4） | `src/utils/atomic_io.py + 5 处` | M |
| DRY-03 | MEDIUM | `_write_json`+`_to_jsonable` 逐字重复于 assemblyai/gemini | `transcriber.py:828；translator.py:2683` | S |
| DRY-04 | MEDIUM | `_AGE_*` 常量 + `_resolve_age_bucket` 重复于 voice_reranker/cosyvoice_selector | `voice_reranker.py:51；cosyvoice_voice_selector.py:110` | S |
| DRY-05 | MEDIUM | `VoiceMatchResult` 双重定义（迁移注释存在但未完成） | `voice_match_types.py:69；cosyvoice_voice_selector.py:189` | S |
| DRY-06 | LOW | rerank 结果提取样板在 3 个 selector 重复 | `minimax/volcengine/cosyvoice selector` | S |
| DRY-07 | LOW | 两份 `_verify_job_ownership` | `job_intercept.py:6256；voice_selection_api.py:222` | S |

### 类型安全 / 数据契约（3.5/10，10 条）

| ID | 级别 | 问题 | 位置 | 工时 |
|---|---|---|---|---|
| TS-01 | HIGH | getattr 读 slots dataclass 不存在的 en_text 字段恒空（= H5） | `aligner.py:361/542/591/778` | S |
| TS-02 | HIGH | 65+ getattr 架空 slots=True（tts_generator 52 + aligner 13） | `tts_generator.py:443-609；aligner.py:107-783` | S |
| TS-03 | HIGH | GeminiTranslator 8 个 `Any\|None` 属性；validator 应为 Callable | `translator.py:440-444,1272` | M |
| TS-10 | HIGH | 工具链无 mypy/pyright，所有注解零构建期收益 | `pyproject.toml；ci.yml` | M |
| TS-04 | MEDIUM | load_gemini_config 返回裸 dict，键用字符串字面量访问 | `translator.py:1768；process.py:3172` | S |
| TS-05 | MEDIUM | compute_job_policy user 无类型注解，返回裸 dict 8 键无契约 | `job_intercept.py:798,2965` | S |
| TS-06 | MEDIUM | Job create 入口裸 dict 解析，无 Pydantic 验证模型 | `job_intercept.py:1582` | M |
| TS-07 | MEDIUM | TTSGenerator.job_record: Any；_read_job_field dict/object 双路派发 | `tts_generator.py:130-174` | S |
| TS-08 | MEDIUM | 两个平行 VoiceMatchResult 字段已分叉 | `cosyvoice_voice_selector.py:188；voice_match_types.py:68` | S |
| TS-09 | LOW | combined_rerank `list[dict]`/`dict[str,dict]` 无内层类型 | `voice_reranker.py:208,352` | M |

### 错误处理 / 静默失败 / 日志（4.5/10，12 条）

| ID | 级别 | 问题 | 位置 | 工时 |
|---|---|---|---|---|
| EH-001 | HIGH | process.py 233 print / 0 logging，无结构化日志 | `src/pipeline/process.py（全文件）` | L |
| EH-002 | HIGH | translator.py / transcriber.py 纯 print，metering 失败静默 | `translator.py；transcriber.py` | M |
| EH-003 | HIGH | _derive_credits_from_minutes 异常静默返回 0（= H2，财务） | `gateway/cost_management.py:839-852` | S |
| EH-004 | HIGH | billing webhook bucket 失败缺 exc_info=True，栈帧丢失 | `gateway/billing.py:1299-1314` | S |
| EH-005 | MEDIUM | credits 三处 pricing 加载 except 无 WARNING，静默回退旧费率 | `credits_service.py:121,137,146` | S |
| EH-006 | MEDIUM | 临时音色 expiry 调用 `except: pass` 无信号 | `process.py:8003-8004` | S |
| EH-007 | MEDIUM | 音色目录构建/自动匹配双重 `except: pass/None`，静默降级 | `process.py:8336,8398` | S |
| EH-008 | MEDIUM | tts_generator 38 print，付费 TTS 重试/fallback 无结构日志 | `tts_generator.py:380,658` | M |
| EH-009 | MEDIUM | speaker_corrector Gemini 调用失败日志缺时长/状态/模型 | `speaker_corrector.py:216-218` | S |
| EH-010 | MEDIUM | chunked_upload `_load_usage_bytes` 异常返回 0 → 配额绕过 | `chunked_upload_store.py:298-303` | S |
| EH-011 | MEDIUM | LLM fallback chain 全 print，模型降级无监控 hook | `translator.py:1291-1360` | M |
| EH-012 | LOW | process.py admin_settings/pricing 读取静默失败 | `process.py:2312,8535` | S |

### 性能与效率（后端）（5.5/10，7 条）

| ID | 级别 | 问题 | 位置 | 工时 |
|---|---|---|---|---|
| PERF-001 | HIGH | MiniMax 音色池每说话人一次无缓存同步 HTTP | `minimax_voice_selector.py:63；process.py:8470` | S |
| PERF-002 | HIGH | load_settings 每请求重读+Pydantic 解析（61 调用点，单请求最多 5 次） | `admin_settings.py:860` | S |
| PERF-004 | HIGH | pan/auth.py async handler 内同步 requests.post（含循环） | `pan/auth.py:224,329` | S |
| PERF-003 | MEDIUM | intercept_list_jobs 全表 SELECT job_id 无 user 过滤 | `job_intercept.py:1385` | S |
| PERF-005 | MEDIUM | _pass_a_pan_orphans async 内同步 requests（含循环删除） | `pan/orphan_cleanup.py:156,178` | S |
| PERF-006 | MEDIUM | assemble_sample_from_job_segments async 内同步文件读 + subprocess | `cosyvoice_clone/sample_assembler.py:200,250` | S |
| PERF-007 | MEDIUM | build_disk_overview async 内同步磁盘扫描 + urllib | `admin_disk_api.py:531,268` | M |

### 异步 / FastAPI 正确性（5.5/10，8 条）

| ID | 级别 | 问题 | 位置 | 工时 |
|---|---|---|---|---|
| ASYNC-01 | HIGH | file_lock（OS 锁）直接在 async 端点事件循环线程调用（9 处） | `admin_settings.py:882；chunked_upload_store.py:389` | S |
| ASYNC-02 | HIGH | SMS/CAPTCHA urllib.urlopen 在 async 路由阻塞（15s/10s） | `sms_provider.py:119；risk_control.py:647,693` | S |
| ASYNC-03 | HIGH | build_disk_overview du+os.walk 在 async 端点阻塞 | `admin_disk_api.py:531-620` | S |
| ASYNC-04 | MEDIUM | _aggregate_report_rows async 内逐行同步文件读 | `admin_smart_analytics_api.py:1234` | S |
| ASYNC-05 | MEDIUM | 废弃 `asyncio.get_event_loop()` 在 async 上下文 | `voice_selection_api.py:741；voice_calibration_inflight.py:143` | S |
| ASYNC-06 | MEDIUM | regenerate_all_async cancel_requested 被覆盖（= H3，测试间歇红） | `regenerate_all_async.py:372-380` | S |
| ASYNC-07 | LOW | 无全局 FastAPI 异常处理器，500 错误体不统一 | `gateway/main.py` | S |
| ASYNC-08 | LOW | 170 路由仅 31 有 response_model | `job_intercept.py；admin_settings.py；billing.py` | M |

### 数据库 / 模型 / 迁移（6.5/10，10 条）

| ID | 级别 | 问题 | 位置 | 工时 |
|---|---|---|---|---|
| DB-001 | HIGH | Alembic 036 双分叉部署歧义（041 已 merge，建议加 heads 单头断言） | `036_job_language_fields.py:34；036_payment...py:15` | S |
| DB-002 | HIGH | GET /users 无 LIMIT 全表扫描 | `gateway/admin_settings.py:1851-1852` | S |
| DB-003 | HIGH | CreditsLedger.direction 无 CHECK 约束（revoke 可静默落库） | `models.py:681；009_..._metering.py:93` | S |
| DB-004 | MEDIUM | 连接池缺 pool_pre_ping + statement_timeout | `gateway/database.py:36` | S |
| DB-005 | MEDIUM | FreeServiceDailyUsage 缺 __table_args__（autogenerate 误删索引） | `models.py:1089；034_..._daily_usage.py:75` | S |
| DB-006 | MEDIUM | 公告 fan-out 单事务循环写，大受众持锁过长 | `system_announcements_service.py:408` | M |
| DB-007 | MEDIUM | 匿名上传 async 路由内建独立 sync psycopg2 引擎，绕过连接池 | `anonymous_preview_api.py:228-238` | M |
| DB-008 | LOW | models.py 1832 行 20+ ORM 类未按域拆 | `gateway/models.py:1-1832` | M |
| DB-009 | LOW | SupportAIUsage 成本用 Float 非 Numeric，累积浮点误差 | `models.py:1396-1404` | S |
| DB-010 | LOW | BackupRecord/PanOauthState 缺 __table_args__ | `models.py:1660,1698；029_pan_backup.py:57` | S |

### 依赖 / 配置 / 构建卫生（3.5/10，9 条）

| ID | 级别 | 问题 | 位置 | 工时 |
|---|---|---|---|---|
| DEP-01 | HIGH | pyproject 5 依赖无版本约束；uv.lock 在 CI/Docker 未生效 | `pyproject.toml:6-15；Dockerfile:38` | M |
| DEP-02 | HIGH | 36 个生产 env（含所有 API Key）在用却不在 .env.example | `.env.example；src/gateway 分散` | S |
| DEP-04 | HIGH | 开发期 code bind-mount 仍在 main 的 docker-compose | `docker-compose.yml:53-66` | S |
| DEP-03 | MEDIUM | 双路配置：145 处裸 os.environ.get 绕过 Pydantic Settings | `gateway/config.py:10 + 分散` | L |
| DEP-05 | MEDIUM | pyJianYingDraft 在 Dockerfile 旁路安装，不在 pyproject | `Dockerfile:39` | S |
| DEP-06 | MEDIUM | Deno 经 curl\|sh 安装但运行时从不调用（死代码+供应链） | `Dockerfile:46-47` | S |
| DEP-07 | MEDIUM | cloudflared:latest 浮动 tag（唯一公网入口） | `docker-compose.yml:484` | S |
| DEP-09 | MEDIUM | CI 用 pip cache 非 uv lock；boto3/botocore 仅上界 | `ci.yml:19-21；gateway/requirements.txt` | M |
| DEP-08 | LOW | VolcEngine/MiniMax 双命名 env 在 4 处并联 fallback | `volcengine_tts_provider.py:98；voice_catalog_service.py:108` | S |

### 工具链 / Lint / 规范（2.5/10，8 条）

| ID | 级别 | 问题 | 位置 | 工时 |
|---|---|---|---|---|
| TOOL-01 | HIGH | 无 ruff/formatter（487 文件，371 print，1,054 Any） | `pyproject.toml；ci.yml` | S |
| TOOL-02 | HIGH | 无 mypy（1,054 Any，74 type:ignore 无门） | `translator.py；tts_generator.py` | M |
| TOOL-03 | HIGH | CI 只跑 14/8,474 测试 | `.github/workflows/ci.yml:27-47` | S |
| TOOL-04 | MEDIUM | 无 pre-commit hooks | `.pre-commit-config.yaml（缺）` | S |
| TOOL-05 | MEDIUM | Provider 注册 4 种模式（TTS if/elif vs 支付 Protocol） | `tts_generator.py:1375；payment_providers.py:39` | M |
| TOOL-06 | MEDIUM | 配置读取 4 种机制 | `config.py；config_loader.py；admin_settings.json；os.environ` | L |
| TOOL-07 | LOW | gateway/main.py 38 个独立 include_router | `gateway/main.py:584-674` | S |
| TOOL-08 | LOW | 前端 4 条 react-hooks 规则降 warn，CI 不可见 | `frontend-next/eslint.config.mjs:8-13` | S |

### 前端代码质量（5.5/10，10 条）

| ID | 级别 | 问题 | 位置 | 工时 |
|---|---|---|---|---|
| FE-001 | HIGH | edit/page.tsx 1975 行，35+ 状态，~70 行轮询重复 | `edit/page.tsx:115,660-955` | L |
| FE-002 | HIGH | eslint 4 规则降 warn，根因 TranslationForm 3 处 set-state-in-effect | `eslint.config.mjs:14；TranslationForm.tsx:293-311` | S |
| FE-003 | MEDIUM | admin/settings/page.tsx 1930 行，46% 静态常量可外移 | `admin/settings/page.tsx:1-1930` | S |
| FE-004 | MEDIUM | SpeakerPayload 类型在两处重复且已分叉 | `VoiceSelectionPanel.tsx:59；VoiceModifyTab.tsx:81` | M |
| FE-005 | MEDIUM | TranslationForm 8 个初始化请求缺 AbortController + smartPreview 错误标志 bug | `TranslationForm.tsx:209-273,177-182` | M |
| FE-006 | MEDIUM | /gateway/* 用裸 fetch，缺超时/统一错误格式化（12+ 处） | `lib/api/voiceSelection.ts;voiceLibrary.ts` | M |
| FE-007 | MEDIUM | loadActiveJobs `!isLoadingGuard` 双重否定逻辑反直觉 | `TranslationForm.tsx:207` | S |
| FE-008 | MEDIUM | CommitModal 副本名 input 缺关联 label（a11y） | `edit/page.tsx:1942；TranslationForm.tsx:921` | S |
| FE-009 | MEDIUM | VoiceModifyTab/VoiceSelectionPanel 重复 400+ 行 | `VoiceModifyTab.tsx:311;VoiceSelectionPanel.tsx:213` | L |
| FE-010 | MEDIUM | 无 SWR/缓存，entitlements/credits/voiceLibrary 多页重复加载 | `TranslationForm.tsx:209；usePollingTask.ts` | M |

### 死代码 / 遗留债（6.5/10，7 条）

| ID | 级别 | 问题 | 位置 | 工时 |
|---|---|---|---|---|
| DC-001 | MEDIUM | web_ui 包 5 组导出零生产调用者 | `web_ui/job_managers.py:42;snapshot.py;config_helpers.py` | M |
| DC-004 | MEDIUM | TODO: split_many_confirmed 事件类型未定义 | `jobs/service.py:1141；user_edit_audit.py:85` | S |
| DC-002 | LOW | Alembic 双 036_ 命名混淆 + 036_payment 迁移非幂等 | `036_job_language_fields.py;036_payment...py` | S |
| DC-003 | LOW | _write_srt_from_segments + 7 helper 标 DEPRECATED 仍存活 | `editor_package_writer.py:376,656` | S |
| DC-005 | LOW | scripts/spike/ + phase0_probes/ 无 CI 引用 | `scripts/spike/*;scripts/phase0_probes/*` | S |
| DC-006 | LOW | 前端 daysRemaining 死函数靠 void 压 lint | `projects/page.tsx:93-101` | S |
| DC-007 | LOW | captcha-gate aliyun 死分支 | `captcha-gate.tsx:591-593` | S |

### 上一轮审计仍 OPEN 项（11 条）

| ID | 级别 | 问题 | 位置 | 工时 |
|---|---|---|---|---|
| PRIOR-17 | HIGH | process.py 未收敛且持续增长（8,430→12,806，+52%） | `src/pipeline/process.py` | XL |
| PRIOR-19 | HIGH | intercept_create_job 膨胀（~376→~1,627，+332%） | `job_intercept.py:1565-3192` | L |
| PRIOR-15 | ✅ 已解决 | ~~root projects/ 空目录致守卫红灯~~ → 2026-06-24 已清理、守卫通过（保持守卫防回归） | `tests/test_legacy_cleanup_guards.py:141` | — |
| PRIOR-18 | MEDIUM | GeminiTranslator God Class 未拆 | `translator.py:409` | L |
| PRIOR-20 | MEDIUM | print() 日志未统一（~451→472） | `process.py;gateway/` | M |
| PRIOR-21 | MEDIUM | edit/page.tsx 未拆组件（1,907→1,975） | `edit/page.tsx` | M |
| PRIOR-16 | LOW | samesite=lax 未改 strict（**本合并版裁定为有意决策，非缺陷**，见 §6.9） | `gateway/auth.py:85` | S |
| PRIOR-22 | LOW | job_record Any/双人格类型未统一（Any 17→22） | `tts_generator.py:130；translator.py:418` | M |
| PRIOR-23 | LOW | plan_rank 局部 dict 硬编码 | `billing.py:150；job_intercept.py:164` | S |
| PRIOR-24 | LOW | useBackgroundTask 缺 AbortController（usePollingTask 已修） | `useBackgroundTask.ts:222-244` | S |
| PRIOR-25 | LOW | TTS provider 注册表 / 配置统一 / 中文文案集中未动 | `tts_strategy.py:25；tts_generator.py:1375` | M |

> 全量 123 条的完整 problem/impact/recommendation 见 Claude 原版 [`2026-06-24-code-quality-efficiency-standards-optimization-plan.md`](2026-06-24-code-quality-efficiency-standards-optimization-plan.md) 各维度章节与逐条结论。
