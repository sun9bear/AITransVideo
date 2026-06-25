# 代码质量 / 效率 / 规范 优化方案（可实施）

> 日期：2026-06-24
> 范围：`src/`（276 文件 / 93,938 行）、`gateway/`（211 文件 / 77,351 行）、`frontend-next/src/`（223 文件 / 57,644 行）、`tests/`（553 文件）、`main.py`、`docker-compose.yml`、`Dockerfile`、`.github/`、`pyproject.toml`
> 方法：13 路并行子代理（Sonnet 4.6，独立额度）按维度深度调查 → 主模型（Opus）综合、去重、对最高危结论逐条 `file:line` 复核 → 排优先级
> 关系：本报告以 [`docs/audits/2026-05-10-comprehensive-codebase-review.md`](../audits/2026-05-10-comprehensive-codebase-review.md) 等既有审计为基线，每条发现标注「延续未解决 / 新增 / 已恶化」。**已独立核实**的关键结论会显式标注 ✅。

---

## 0. 阅读指南

- 只想知道「现在该做什么」→ 跳到 [§3 综合优先级 backlog](#3-综合优先级-backlog) 和 [§4 分阶段路线图](#4-分阶段路线图)。
- 想要可直接粘贴的配置（ruff / mypy / pre-commit / CI）→ [§6 附录 A：即用配置](#6-附录-a即用配置可直接落地)。
- 想看某一维度的完整发现 → [§5 分维度详细发现](#5-分维度详细发现)。
- 发现编号约定：`STRUCT-*`（结构）、`DRY-*`（重复）、`TS-*`（类型）、`PERF-*`（性能）、`EH-*`（错误处理）、`DC-*`（死代码）、`DB-*`（数据库）、`DEP-*`（依赖/配置）、`TOOL-*`（工具链/规范）、`FE-*`（前端）、`ASYNC-*`（异步）、`PRIOR-*`（上一轮审计跟进）。

---

## 1. 执行摘要

### 1.1 一句话结论

> **正确性与安全纪律罕见的优秀（上一轮全部 P0/P1 已落地），但结构债务正在加速积累，且整条 Python 链没有任何自动化质量护栏（无 lint / 无类型检查 / CI 不跑全量测试）。当务之急不是大爆炸重写，而是先装护栏止血、修掉几个已证实的财务/数据/并发隐患，再分阶段收敛巨型文件。**

### 1.2 总体评分（本次细分维度，0–10）

| 维度 | 评分 | 趋势 vs 2026-05-10 |
|---|---|---|
| 工具链 / Lint / 规范 | **2.5** | 新增 CI（+），但仍无 lint/类型/全量测试门禁 |
| 模块 & 文件结构 | **3.5** | ⬇ 恶化（process.py +52%，job_intercept +108%） |
| 类型安全 / 数据契约 | **3.5** | ⬇ 轻微恶化（Any 17→22+） |
| 依赖 / 配置 / 构建卫生 | **3.5** | 新发现，问题密集 |
| 重复代码 / DRY | **4.5** | 持平 |
| 错误处理 / 静默失败 / 日志 | **4.5** | 持平（print 451→472） |
| 测试套件质量与覆盖 | **5.0** | 原调查 agent 未完成，主模型补做核实 |
| 性能与效率（后端） | **5.5** | 新发现 |
| 前端代码质量 | **5.5** | 持平（edit/page 1907→1975） |
| 异步 / FastAPI 正确性 | **5.5** | 新发现 |
| 上一轮审计跟进 | **6.0** | follow-through ≈ 65% |
| 死代码 / 遗留债 | **6.5** | 边界清晰 |
| 数据库 / 模型 / 迁移 | **6.5** | 纪律良好 |

**加权综合 ≈ 4.8 / 10**：逻辑正确性高、安全护栏到位（P0/P1 全清），但「工程可维护性 + 自动化规范」这一层系统性偏低。

### 1.3 三条最重要的判断

1. **结构债在加速，不是收敛。** `src/pipeline/process.py` 6 周内从 8,430 → **12,806** 行（+52%），`gateway/job_intercept.py` 从 3,300 → **6,880** 行（+108%），其中 `intercept_create_job()` 单函数从 ~376 → **~1,627** 行（+332%）。25+ 个文件超过项目自定的 800 行上限。**根因是缺一道「文件不许继续变大」的 CI 门**——每个新功能都往同一文件追加。

2. **整条 Python 没有质量护栏。** 17 万行 Python 代码 **没有** ruff / mypy / black / isort / pre-commit；CI 只跑手挑的 **14 个**守卫测试（全量 8,400+ 测试从不在 CI 跑），无 Python lint / 类型 / 覆盖率门禁。前端虽有 `tsc --noEmit` + eslint，但 4 条 react-hooks 规则被降级为 warn。这是评分最低（2.5）的维度，也是 **ROI 最高的改进入口**。

3. **几个已证实的「隐形」隐患值得本周就修**（均已逐条核实，详见 §2）：财务静默归零、付费任务取消失效、editing 写盘无 fsync、一个数据契约 bug，外加一个**当前就红的 CI 守卫测试**。

### 1.4 发现总量

约 **107 条新/未解决发现**（不含 12 条已确认关闭的历史项）。其中 CRITICAL 2、HIGH 21、MEDIUM 约 50、LOW 约 32（含主模型补做的 6 条 `TEST-*`，见 §5.13）。

---

## 2. 已独立核实的重点发现（主模型逐条复核 ✅）

下列结论我都打开了引用的 `file:line` 当场确认，确保方案不误报、不重复修已修好的东西。

### 2.1 ✅ 当前就红的 CI 守卫测试 — 根 `projects/` 空目录（PRIOR-15，S，**本周**）

- 核实：`projects/` 空目录存在；[`tests/test_legacy_cleanup_guards.py:141`](../../tests/test_legacy_cleanup_guards.py) 的 `test_no_root_projects_dir` 断言 `not (REPO / "projects").exists()` → **当前红灯**。
- 修复：删除根 `projects/`（真实数据在 `data/projects/`），确认 `.gitignore` 中 `projects/` 带前导斜杠精确匹配根目录。**5 分钟。**

### 2.2 ✅ 财务静默归零 — `_derive_credits_from_minutes`（EH-003，S，**本周**）

- 核实：[`gateway/cost_management.py:839`](../../gateway/cost_management.py) `except Exception: return 0`，无任何日志。`estimate_credits` 一旦抛错（如 `pricing_runtime.json` 损坏），任务以 **0 点成本**结算，无告警。与 memory `feedback_terminal_state_single_entry`（曾因结算漏算少扣点）同类。
- 修复：`except Exception: logger.exception("_derive_credits_from_minutes failed job=%s minutes=%s", ...); return 0`，并在调用侧加 `if credits == 0 and minutes > 0: logger.error("ZERO_CREDITS_SUSPECT ...")`。
- 同批：[`gateway/billing.py:1311`](../../gateway/billing.py) 支付成功后 `ensure_subscription_bucket` 失败只 `logger.warning(...)` **无 `exc_info=True`**（EH-04，栈帧丢失，用户付了钱拿不到额度难诊断）；[`gateway/credits_service.py:121/137/146`](../../gateway/credits_service.py) 三处 pricing 加载 `except: return <常量>` 无 WARNING（EH-05，定价文件损坏静默回退旧费率）。

### 2.3 ✅ 付费任务「取消」可被静默吞掉 — 数据竞争（ASYNC-06，S，**本周**）

- 核实：[`src/services/jobs/regenerate_all_async.py:372`](../../src/services/jobs/regenerate_all_async.py) `_run_batch` 在进入 `stage=running` 时用全新 `_initial_status(task_id)` dict 覆盖状态文件，**抹掉**了竞态窗口内已写入的 `cancel_requested=True`。调查代理实跑 `tests/test_regenerate_all_async.py::test_cancel_writes_cancel_requested_flag` 复现了**间歇性失败**。
- 影响：用户点「取消批量重生成」在特定时序下无效，批处理继续消耗付费 TTS 额度。
- 修复：改为「读现有状态 → 仅 merge 变更字段 → 写回」，保留 `cancel_requested`。

### 2.4 ✅ editing 写盘无 fsync — 数据 durability（DRY-02 子项，S，**本周**）

- 核实：[`src/services/jobs/editing_segments.py:172`](../../src/services/jobs/editing_segments.py) `_atomic_write_json` 做了 mkstemp → write → `os.replace`（保证不出现半写可见），但 **没有 `os.fsync`**。其他 5 处原子写实现都 fsync。断电 / OOM kill 时 `segments.json` / `voice_map.json` / `segment_status.json` 可能丢内容。
- 修复：见 DRY-02 统一原子写工具（顺带消除 6 处重复实现）。

### 2.5 ✅ 数据契约 bug — `getattr(segment,"en_text")` 读不存在的字段（TS-01，S，**本周**）

- 核实：`class DubbingSegment`（[`translator.py:252`](../../src/services/gemini/translator.py)，`slots=True`）**没有** `en_text` 字段（只有 `cn_text` / `first_pass_cn_text` / `tts_input_cn_text`）；[`aligner.py:361/542/591/778`](../../src/services/alignment/aligner.py) 四处 `en_text=getattr(segment, "en_text", "")` 因此**永远返回空串**，写入 `AlignedSegment.en_text`。
- **准确的影响边界（主模型额外核实，避免夸大）**：`AlignedSegment.en_text` 的下游消费者主要落在 [`editor_package_writer.py:417/419/666`](../../src/modules/output/editor/editor_package_writer.py) 的 `_write_srt_from_segments`——而该路径已被 DC-003 标注为 **DEPRECATED**，仅当任务无 `subtitle_cues` 时触发（新任务一律走 cue 管线，源文本来自另一条 `SubtitleLine`/`cue` 路径）。**因此今天的生产实际影响是有限的，但这是一颗潜伏炸弹**：一旦该 fallback 重新激活，导出的 SRT 英文行会是空白。
- **价值定位**：这是「`getattr` 在已知 `slots=True` dataclass 上静默吞掉拼写/缺字段」这一系统性问题（TS-02：共 65+ 处）最有说服力的实证，也是**引入 mypy 的最强论据**——`mypy` 一行就能报 `"DubbingSegment" has no attribute "en_text"`。
- 修复：给 `DubbingSegment` 加 `en_text: str = ""` 并在 `_build_groups` 填充；删除四处 `getattr` 改直接属性访问。

### 2.6 ✅ 工具链与日志的硬数据（独立 grep 核实）

- `print()`：src **371** + gateway **96** = **467** 处，与 1,424 处 logging 混用；`process.py` 单文件 **233** 处 print、**0** logging。
- 无 ruff/mypy/black/isort/flake8/pylint/pre-commit（`pyproject.toml` 仅 pytest + pytest-asyncio + aiosqlite）。
- CI 显式只跑 **13–14** 个测试目标（全量 553 文件 / 8,400+ 测试不跑），无 Python lint/类型/覆盖率门。
- Alembic：036 确有双 `036_*` 分叉（均 `down_revision="035_anonymous_preview"`），但 **041 是正确的 merge 迁移**收敛了两条链——**非数据事故**，仅残留「双 036_ 命名混淆」+「036_payment 迁移非幂等」两个小风险（见 DB-001 / DC-002）。

---

## 3. 综合优先级 backlog

优先级判据：**价值 ÷ 工作量 ÷ 风险**。工作量：S（<1h）/ M（半天–1天）/ L（数天）/ XL（专项 sprint，分多 PR）。

### P0 — 本周（全 S，止血 + 装护栏，不阻断业务）

| ID | 标题 | 维度 | 工作量 |
|---|---|---|---|
| PRIOR-15 ✅ | `rmdir projects/`（CI 守卫当前红灯） | 遗留 | S |
| DEP-04 | docker-compose 删除 3 个开发期 code bind-mount（生产可变镜像隐患） | 配置 | S |
| DEP-06 | Dockerfile 删除 `curl\|sh` 安装 Deno（死代码 + 供应链 + ~100MB） | 配置 | S |
| DEP-07 | `cloudflared:latest` 改为 pin 版本（唯一公网入口） | 配置 | S |
| DEP-02 | `.env.example` 补全 36 个缺失生产变量（含所有 API Key） | 配置 | S |
| EH-003 ✅ | `_derive_credits_from_minutes` 加日志 + ZERO_CREDITS_SUSPECT 告警 | 错误处理 | S |
| EH-004 | billing webhook `ensure_subscription_bucket` 失败加 `exc_info=True` | 错误处理 | S |
| EH-005 | credits 三处 pricing fallback 加 WARNING 日志 | 错误处理 | S |
| DRY-02 ✅ | `editing_segments._atomic_write_json` 加 `fsync`（数据 durability） | 重复 | S |
| ASYNC-06 ✅ | `regenerate_all_async` 修 cancel-flag 覆盖竞争 | 异步 | S |
| TS-01 ✅ | `DubbingSegment` 加 `en_text` 字段，去 4 处 getattr | 类型 | S |
| TOOL-01 | 引入 ruff（lint+format）+ `pyproject` 配置 + `--add-noqa` 建基线 | 工具链 | S |
| TOOL-03 | CI 新增 `python-lint` + `backend-full-suite`（覆盖率起步 60–65%）job | 工具链 | S |

> P0 合计约 1.5 人日，全部不改业务逻辑、不阻断交付，且其中 5 条已逐条核实。

### P1 — 2–4 周（S/M，护栏成型 + 高价值正确性/性能）

| ID | 标题 | 维度 | 工作量 |
|---|---|---|---|
| (新) | CI 加「文件行数门」：新提交 Python 文件 >800 行即失败（白名单现存超标文件并冻结其增长） | 工具链 | S |
| TOOL-02 / TS-10 | mypy Phase-1（`src/core` + `src/services/llm` + `gateway/storage`）+ CI 类型门 | 工具链/类型 | M |
| TOOL-04 | `.pre-commit-config.yaml`（ruff + mypy 窄域 + 基础卫生） | 工具链 | S |
| TEST-02/03/06 | 加最小 pytest 配置（注册 marks + asyncio_mode）+ 装 pytest-timeout/pytest-cov；CI 全量 job 加 `--timeout=120` + `-n auto` 分片 | 测试 | S |
| TEST-04 | conftest 收口高频共享 fixture（减 69 文件各自造轮子） | 测试 | M |
| ASYNC-01 | 9 处 `file_lock` 在 async 端点用 `asyncio.to_thread` 包裹 | 异步 | S |
| ASYNC-02 | SMS / CAPTCHA `urllib.urlopen` 在 async 路由 `to_thread` 包裹 | 异步 | S |
| ASYNC-03 | `build_disk_overview` 磁盘扫描 `to_thread` | 异步 | S |
| ASYNC-05 | 替换废弃 `asyncio.get_event_loop()`（voice_selection / calibration_inflight） | 异步 | S |
| PERF-001 | `minimax_voice_selector._load_minimax_pool` 加 120s TTL 缓存 | 性能 | S |
| PERF-002 | `admin_settings.load_settings` 加 5s TTL 缓存（61 调用点，单请求最多 5 次） | 性能 | S |
| PERF-003 | `intercept_list_jobs` 去掉全表 `SELECT job_id` 扫描 | 性能 | S |
| PERF-004/005/006/007 | Pan auth/orphan、clone sample、disk overview 的同步 I/O `to_thread` | 性能 | S–M |
| DB-001 / DC-002 | CI 加 `alembic heads` 单头断言；`036_payment` 迁移幂等化（`ADD COLUMN IF NOT EXISTS`） | 数据库 | S |
| DB-002 | `GET /users` 加 LIMIT/OFFSET 分页 | 数据库 | S |
| DB-003 | `CreditsLedger.direction` 加 CHECK 约束（含 `revoke`） | 数据库 | S |
| DB-004 | 连接池加 `pool_pre_ping` + `statement_timeout` | 数据库 | S |
| DB-005 / DB-010 | `FreeServiceDailyUsage` / `BackupRecord` / `PanOauthState` 补 `__table_args__`（防 autogenerate 误删索引） | 数据库 | S |
| DRY-01 | 新建 `gateway/admin_auth.py`，消除 13 份 `_require_admin`（安全） | 重复 | M |
| TS-02 | 去 `aligner.py`(13) + `tts_generator.py`(52) 防御性 `getattr`（恢复 slots 安全网） | 类型 | S×2 |
| TS-04 / TS-05 | `GeminiConfig` / `JobPolicy` TypedDict | 类型 | S |
| TS-07 | `job_record` 统一为 `dict[str,object]`，去 `_read_job_field` 双路 | 类型 | S |
| FE-002 | 修 `TranslationForm` set-state-in-effect 根因，eslint 4 规则恢复 `error` | 前端 | S |
| FE-005 | 修 `smartPreviewCloneCostLabel` 错误标志 bug + 8 个初始化请求加 AbortController | 前端 | M |
| PRIOR-16 | `samesite='lax' → 'strict'`（auth cookie） | 安全 | S |
| PRIOR-23 | `PlanDefinition` 加 `rank` 字段，消除 `plan_rank` 硬编码 dict | 规范 | S |

### P2 — 1–3 月（M/L/XL，结构重构 + 系统化，分多 PR 小步推进）

| ID | 标题 | 维度 | 工作量 |
|---|---|---|---|
| STRUCT-01 | `process.py` → `stages/` 子包 + `PipelineContext`（先抽 `_constants.py`） | 结构 | XL |
| STRUCT-02 | `job_intercept.py` → `gateway/jobs/` 子模块（先抽 admin_logs/download/list） | 结构 | XL |
| EH-001/002/008 / PRIOR-20 | `process.py` / translator / transcriber / tts_generator 的 print→logging 系统化迁移 | 错误处理 | M–L |
| STRUCT-03 | `transcript_reviewer.py` → `transcript_review/` 子包 | 结构 | L |
| STRUCT-04 / PRIOR-18 | `GeminiTranslator` 拆分（先抽 speaker attribution） | 结构 | L |
| STRUCT-06 | `tts_generator.py` → `providers/` 子包 + TTSProvider Protocol | 结构 | M |
| STRUCT-07 | `jobs/service.py` 拆 editing_lifecycle / editing_tts / voice_map | 结构 | M |
| STRUCT-05 | `jobs/api.py` 内嵌 handler 路由分支提取为 `_handle_*` 方法 | 结构 | M |
| STRUCT-10/11 | `traffic_analytics.py` / `admin_settings.py` 子包化 | 结构 | M |
| STRUCT-12 | `credits_service.py` 拆 `credits/` 子包（金融：先补集成测试再迁） | 结构 | M |
| DB-008 | `gateway/models.py` 按域拆 `models/` 包（re-export 保兼容） | 数据库 | M |
| DB-006 / DB-007 | 公告 fan-out 批量化/异步化；匿名上传去掉每请求独立 sync 引擎 | 数据库 | M |
| FE-001 | `edit/page.tsx` 拆 `useBatchRegenPoll` + BulkReplacePanel + CommitModal | 前端 | L |
| FE-009 | `VoiceModifyTab` / `VoiceSelectionPanel` 提共享 hook + 组件（去重 400+ 行） | 前端 | L |
| FE-003 / FE-004 | admin/settings 静态常量外移；SpeakerPayload 共享类型 | 前端 | S/M |
| DEP-01 / DEP-09 | Docker + CI 改用 `uv sync --frozen`，pyproject 加版本下界 | 配置 | M |
| TOOL-06 / DEP-03 | 配置读取统一（src 侧 `src/core/env.py` 收口 93 处裸 `os.environ`） | 工具链 | L |
| DC-001 | `web_ui` 包孤立导出清理 | 死代码 | M |
| STRUCT-08 / STRUCT-09 | `control_panel.py` HTML 外移；`main.py` → `cli/` 子包 | 结构 | S/M |

### P3 — 持续（清理 + 棘轮收紧，低风险）

| ID | 标题 | 维度 | 工作量 |
|---|---|---|---|
| TOOL-01（棘轮） | ruff 规则集按月扩展（B/C4 → SIM/N → T20 print → ANN） | 工具链 | 持续 |
| TOOL-05 / PRIOR-25 | TTS provider 注册表统一（替换 if/elif 分发） | 工具链 | M |
| TOOL-07 | `gateway/main.py` 38 个 `include_router` 改注册表循环 | 工具链 | S |
| TOOL-08 | 前端 eslint warning budget CI 门 | 工具链 | S |
| DRY-03/04/05/06/07 | 抽 `_json_utils`、age-bucket、`VoiceMatchResult`、rerank 提取、job_ownership | 重复 | S 各 |
| DC-003/005/006/007 | 删 deprecated SRT fallback、spike 脚本、`daysRemaining`、aliyun captcha 死分支 | 死代码 | S 各 |
| DC-004 | 补 `post_edit_segment_split_many_confirmed` 事件类型（TODO） | 死代码 | S |
| TS-03/08/09 | `validator` Callable + UsageMeterProtocol；合并 `VoiceMatchResult`；voice catalog TypedDict | 类型 | S–M |
| ASYNC-07/08 | 全局异常处理器；核心端点补 `response_model` | 异步 | S/M |
| DB-009 | `SupportAIUsage` 成本字段 Float→Numeric | 数据库 | S |
| DEP-08 | VolcEngine / MiniMax 双命名 env 收敛 | 配置 | S |
| FE-006/007/008/010 | gatewayClient 统一；轮询语义显式化；a11y label；TTL 缓存 | 前端 | S–M |
| PRIOR-24 | `useBackgroundTask` 加 AbortController | 前端 | S |

---

## 4. 分阶段路线图

### 阶段 A：止血 + 装护栏（本周，~1.5 人日）

完成 **P0 全部 13 项**。产出可见效果：
- CI 守卫从红转绿（projects/）；
- 引入 ruff + 全量测试 job + 文件行数门 → **从此新代码有自动门禁**；
- 修掉 5 个已证实隐患（财务归零、cancel 竞争、fsync、en_text、+ billing 栈帧）。

**关键：先装「文件行数门」+「全量测试 job」，再做任何结构重构**——否则重构期间无回归网，且巨型文件会继续长大。

### 阶段 B：护栏成型 + 高价值修复（2–4 周）

完成 **P1**。mypy Phase-1 + pre-commit 上线；清掉所有 `asyncio` 事件循环阻塞与高频无缓存读取；补齐 DB 约束/分页/连接池；统一 admin 鉴权；修前端 hooks 根因。此阶段结束后：**质量护栏完整，已知正确性/性能/安全隐患清零**。

### 阶段 C：结构收敛（1–3 月，分多 PR）

完成 **P2**。按「**只移动代码、不改逻辑、每步 `pytest -x` 绿灯**」推进：
1. `process.py`：先抽 `_constants.py` → `_report_job_metering` 纯函数化 → S0..S6 逐 stage 外移。
2. `job_intercept.py`：先抽风险最低的 `admin_logs` / `download` / `list_jobs`，最后才动 `create_job`。
3. print→logging 与文件拆分同批做（拆 stage 时顺手换 logger）。
4. 金融模块（`credits_service`）**先补集成测试再迁**。

### 阶段 D：清理 + 棘轮（持续）

完成 **P3**。ruff 规则集逐月加严；死代码删除；provider 注册表 / 配置收口等长尾。

---

## 5. 分维度详细发现

> 完整结构化结论（含每条 `file:line`、修复代码、工作量）由 12 个调查代理产出。下面给出每维度的要点与最关键修复；逐条细节见 §3 backlog 的 ID 索引。

### 5.1 工具链 / Lint / 规范（2.5）— 最高 ROI 入口

无 Python lint/format/type/pre-commit；CI 只跑 14/8400+ 测试；276 处 `# noqa`（说明开发者已有规则意识，采纳成本低于从零）；4 类跨切面不一致（provider 注册 4 种、配置读取 4 种、38 个 include_router、魔法数字）。
**核心动作**：ruff（建基线→月度棘轮）+ mypy（窄域起步）+ pre-commit + CI 三件套（lint / 全量测试 + 覆盖率 / 文件行数门）。**即用配置见 §6。**

### 5.2 模块 & 文件结构（3.5）— 债务在加速

25+ 文件超 800 行；`process.py` 12,806（16×）、`job_intercept.py` 6,880（8.6×）、`transcript_reviewer.py` 4,173、`GeminiTranslator` 2,825、`jobs/api.py` 2,645…。`intercept_create_job` 单函数 ~1,627 行是全库最长。
**核心动作**：先用 CI 文件行数门**冻结增长**，再按 STRUCT-01/02 蓝图（`stages/` 子包 + `PipelineContext`；`gateway/jobs/` 子模块）小步外移。每个超大文件的分解蓝图已在调查产出中给到目录级。

### 5.3 类型安全 / 数据契约（3.5）

无 mypy/pyright → 所有类型注解零构建期收益。65+ 处 `getattr` 架空 `slots=True`（TS-02），TS-01 已是实证 bug；`load_gemini_config`/`compute_job_policy` 返回裸 dict（应 TypedDict）；job create 入口无 Pydantic 模型（TS-06）。
**核心动作**：mypy `--ignore-missing-imports --check-untyped-defs` 起步即可捕获 TS-01/02/04/05/07；逐步 TypedDict 化跨层 dict。

### 5.4 依赖 / 配置 / 构建卫生（3.5）— 改动小、收益高

`pyproject` 5 个核心依赖无版本约束且 **uv.lock 在 CI/Docker 中完全未生效**（CI/Docker 每次从 PyPI 新解析）；`.env.example` 缺 36 个在用变量（含所有 API Key）；开发期 bind-mount 仍在 main 分支（生产可变镜像）；Deno `curl|sh` 死安装；`cloudflared:latest` 浮动 tag；`pyJianYingDraft` 在 Dockerfile 旁路安装不在 pyproject。
**核心动作**：P0 删 bind-mount/Deno + pin cloudflared + 补 .env.example；P2 迁 `uv sync --frozen`。

### 5.5 重复代码 / DRY（4.5）

13 份 `_require_admin`（行为已分叉：返回 `User` vs `None`，角色判断三种写法 → **安全维护风险**）；6 处原子 JSON 写实现（其中 `editing_segments` 漏 fsync = 数据风险）；`_write_json`/`_to_jsonable` 逐字重复；age-bucket、`VoiceMatchResult`、rerank 提取等重复。
**核心动作**：`gateway/admin_auth.py` 收口鉴权；`src/utils/atomic_io.py` 收口原子写并补 fsync。

### 5.6 错误处理 / 静默失败 / 日志（4.5）

835 处 `except Exception`，约 297 处无日志/重抛；`process.py` 233 print/0 logging。**付费 API 硬约束总体合规**（TTS fallback 不回落 MiniMax clone、S2 fallback 有 cap、express clone 入口有 log）；但财务路径有静默盲区（EH-003/04/05）。
**核心动作**：P0 修财务 3 条；P2 随 stage 拆分系统化 print→logging（注入 `extra={"job_id","stage"}`）。

### 5.7 性能与效率（5.5）

架构正确（async gateway + sync worker 分离）。7 处新发现：MiniMax 音色池无缓存（每说话人一次同步 HTTP，3 说话人=4 次）、`load_settings` 每请求重读+解析（单请求最多 5 次）、list_jobs 全表扫描、Pan/clone/disk 多处 async 内同步阻塞 I/O。
**核心动作**：复用已有 `voice_speed_catalog` TTL 缓存模式 + `asyncio.to_thread`。**这些都是 S 级、复制现成模式即可。**

### 5.8 前端代码质量（5.5）

无 CRITICAL 安全问题（dangerouslySetInnerHTML 仅 JSON-LD 且转义、target=_blank 带 noreferrer、无 key={index}）。问题：10 文件超 800 行（`edit/page.tsx` 1975、35+ 状态、~70 行轮询重复）；eslint 4 规则降级 warn（根因是 `TranslationForm` 3 处 set-state-in-effect，**可修复**）；/gateway 路径裸 fetch 无超时；`VoiceModifyTab`/`VoiceSelectionPanel` 重复 400+ 行；一个真实小 bug（FE-005 错误标志引用）。
**核心动作**：P1 修 hooks 根因 + 恢复 eslint error；P2 拆 edit 页与 Voice* 组件。

### 5.9 异步 / FastAPI 正确性（5.5）

DB 层正确（async + Depends(get_db)，sweeper 正确 cancel）。5 处事件循环阻塞（file_lock / urllib / 磁盘扫描 / 逐行文件读 / 废弃 get_event_loop）+ 1 个数据竞争（ASYNC-06 已证实）。无全局异常处理器；170 路由仅 31 有 response_model。
**核心动作**：统一 `asyncio.to_thread` 包裹阻塞调用；P0 修 ASYNC-06。

### 5.10 数据库 / 模型 / 迁移（6.5）— 纪律良好

正向：FOR UPDATE / SKIP LOCKED 使用规范、timestamptz 全覆盖、整型货币、partial unique 幂等模式一致。缺陷：alembic 双头需运维透明度（DB-001）、`GET /users` 无分页（DB-002）、`CreditsLedger.direction` 无 CHECK（DB-003）、连接池无 pre_ping/timeout（DB-004）、3 个模型 `__table_args__` 与迁移索引不同步（DB-005/010，autogenerate 误删风险）、公告 fan-out 单事务循环写、匿名上传旁路连接池。

### 5.11 死代码 / 遗留债（6.5）

边界清晰：`web_ui` 包 5 组孤立导出（DC-001）、deprecated SRT fallback（DC-003）、spike 脚本、前端 `daysRemaining`/aliyun 死分支。TODO 仅 4 处（健康）。alembic 036 双命名 + 036_payment 非幂等（DC-002，与 DB-001 同源）。

### 5.12 上一轮审计跟进（6.0）

**8/8 P0（安全/数据完整性）+ 8/8 P1（稳定性/性能）全部已落地**（IDOR fail-closed、JSON store file_lock、quota FOR UPDATE、alembic env import、captcha 死链清理、登录限速、OTP consume-after-compare、ffprobe timeout、S2 fallback cap、CI 新建…）。**P2 架构层 7/9 仍 OPEN 且部分恶化**（process.py / job_intercept / GeminiTranslator / print / edit page / job_record Any / plan_rank）。这正是本报告 P2 的来源。

---

### 5.13 测试套件质量与覆盖（5.0）— 广度优秀、CI 集成与卫生薄弱

> 本维度的原调查 agent **未完成**（疑似去跑全量 pytest 时挂住而终止——见下方 TEST-03 的根因）。由主模型亲自补做并核实。

**正向（真实优势）**：`pytest --collect-only` 干净收集 **8,474 个测试 / 553 文件，6.7s 无导入错误**；契约级守卫测试（`phase1_guards` / `legacy_cleanup_guards`）成熟，以 AST 级守护付费 API 硬约束、模块结构、前后端 path 对齐——守的是"不变量不漂移"。广度与不变量守护是这个项目的亮点。

**关键缺陷**：

| ID | 问题 | 证据 | 工作量 |
|---|---|---|---|
| TEST-01 | CI 只跑约 **14** 个测试目标，全量 **8,474** 从不在 CI 运行 | `.github/workflows/ci.yml:27-47` | S（§6 已给 `backend-full-suite`） |
| TEST-02 | **无任何 pytest 配置**（pyproject/pytest.ini/setup.cfg 均无 `[tool.pytest]`）→ `asyncio_mode` 未设、mark 未注册 | collect-only 报 `PytestUnknownMarkWarning: postgres / timeout` | S |
| TEST-03 | **无 per-test 超时**：`pytest-timeout` 未安装 → `test_process_runner_watchdog.py:119` 的 `@pytest.mark.timeout(15)` 是**静默 no-op**；任一挂住的测试会永久 hang——**这正是调查 agent 与全量跑挂死的根因** | `pip show pytest-timeout` 为空 | S |
| TEST-04 | `conftest.py` 仅 **16 行**（只设 sys.path），**零共享 fixture**；114 个 fixture 散落在 **69** 个文件，重复/分叉风险 | `tests/conftest.py` + grep | M |
| TEST-05 | 慢/外部依赖面大：**264** 处 `requests`/`urlopen`、**32** 处 `time.sleep`、**30** 个文件用 `subprocess`；单文件 `test_process_pipeline.py` 在 **200s 内跑不完** | grep + 限时实跑 | M |
| TEST-06 | `pytest-cov` 未安装，无覆盖率基线/门 | `pip show pytest-cov` 为空 | S（§6 已加入 dev 依赖 + `--cov-fail-under`） |

**关于 `test_process_pipeline.py`（诚实标注）**：我用 200s 限时实跑，前 ~56% **全部通过**（无 F）即被超时中断——**单文件 >200s**，故**未能取得完整 pass/fail 计数**。项目记忆 `project_test_process_pipeline_drift_fix` 记录其约 44 例失败源于 fake/mock 漂移（**预存、非回归**）；该精确数目**本次未独立确认**（我看到的部分全绿，可能已修或失败集中在后段）。无论计数如何，"单文件 >200s 且无 per-test 超时"本身就是必须修的点。

**落地建议**：

1. 加最小 `[tool.pytest.ini_options]`：注册 marks、设 `asyncio_mode`、默认排除慢/PG 标记。
2. dev 依赖加 `pytest-timeout`，CI 全量 job 加 `--timeout=120 --timeout-method=thread`——让挂住的测试被杀而非永久 hang。**这是"让 CI 跑全量"可行的前提**（否则会重蹈调查 agent 挂死的覆辙）。
3. CI 全量 job 用 `-n auto`（`pytest-xdist`）分片并行 + 上述超时，把 8,400+ 测试墙钟时间压到可接受。
4. `conftest.py` 收口高频 fixture（fake job store、tmp project dir、db stub 约定见 memory `feedback_test_database_stub_convention`），减少 69 文件各自造轮子。
5. 覆盖率从 60% 起步（`pytest-cov`），优先补计费 / 付费 gate / pipeline 关键缝。

最小 `[tool.pytest.ini_options]`（可直接粘进 pyproject.toml）：

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
markers = [
    "postgres: 需要真实 PostgreSQL（CI 单独 job 运行）",
    "timeout: per-test 超时（需 pytest-timeout）",
    "integration: 跨进程/外部依赖集成测试",
]
addopts = "-p no:cacheprovider"
```

> 注：当前 `@pytest.mark.postgres` 已在被用于 `-m`/分流意图，但因未注册，filter 不可靠且产生 warning 噪音；`@pytest.mark.timeout(15)` 因 plugin 缺失完全失效。注册 + 安装后这两个意图才真正生效。

---

## 6. 附录 A：即用配置（可直接落地）

> 以下为可直接粘贴的起步配置。策略统一为「**建基线 → 月度棘轮**」，每项都是独立 PR，不阻断业务开发。

### A.1 `pyproject.toml` 追加依赖

```toml
[project.optional-dependencies]
dev = [
    "pytest",
    "pytest-asyncio",
    "aiosqlite",
    "ruff>=0.5.0",          # 新增
    "mypy>=1.11",           # 新增（Phase 1）
    "pytest-cov>=5.0",      # 新增（覆盖率门；当前未安装）
    "pytest-timeout>=2.3",  # 新增（per-test 超时——当前未安装，导致 @pytest.mark.timeout 是 no-op，挂住的测试永久 hang）
    "pytest-xdist>=3.6",    # 新增（-n auto 分片并行，让 8,474 测试全量跑墙钟可接受）
]
```

### A.2 `[tool.ruff]`（第一轮只开 E/W/F/I/UP，建基线后月度加严）

```toml
[tool.ruff]
target-version = "py312"
line-length = 120          # 与现有长行对齐，避免初始海量 E501

[tool.ruff.lint]
select = ["E", "W", "F", "I", "UP"]
ignore = ["E501", "E402", "E711", "E712", "F401"]  # 增量清理，先不阻断

[tool.ruff.lint.per-file-ignores]
"gateway/alembic/versions/*.py" = ["E501", "I001", "F401"]
"tests/*.py" = ["S101", "ANN"]
"gateway/scripts/*.py" = ["T201"]   # CLI 脚本允许 print

[tool.ruff.format]
quote-style = "double"
indent-style = "space"

[tool.ruff.lint.isort]
known-first-party = ["src", "gateway", "core", "services", "utils"]
```

建基线（一次性，不改逻辑）：`ruff check src/ gateway/ --add-noqa` + `ruff format src/ gateway/ --diff`（先看 diff 再提交）。

棘轮：M+1 加 `B`,`C4`；M+2 加 `SIM`,`N`；M+3 加 `T20`(print，分批清 process.py)；M+4 对 `src/core/` 开 `ANN`。

### A.3 `[tool.mypy]`（非 strict 起步，仅对最干净模块强制）

```toml
[tool.mypy]
python_version = "3.12"
warn_unused_ignores = true
check_untyped_defs = true
ignore_missing_imports = true
exclude = ["gateway/alembic/", "\\.codex_worktrees/", "\\.claude/", "\\.codex_tmp/"]

[[tool.mypy.overrides]]
module = ["src.core.*", "gateway.storage.*", "src.services.llm.*", "src.services.llm_registry"]
disallow_untyped_defs = true
warn_return_any = true

[[tool.mypy.overrides]]
module = ["assemblyai.*", "dashscope.*", "google.genai.*", "boto3.*", "botocore.*", "yt_dlp.*", "pydub.*"]
ignore_missing_imports = true
```

> 起步即跑：`mypy src/services/gemini/translator.py src/services/alignment/aligner.py src/services/tts/tts_generator.py src/services/tts/voice_match_types.py gateway/job_intercept.py --ignore-missing-imports --check-untyped-defs --warn-return-any` —— 这一条就能抓住 TS-01/02/04/05/07。

### A.4 `.pre-commit-config.yaml`

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.5.7
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v4.6.0
    hooks:
      - id: trailing-whitespace
      - id: end-of-file-fixer
      - id: check-merge-conflict
      - id: check-yaml
      - id: check-toml
      - id: debug-statements        # 捕获 breakpoint()/pdb
  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.11.0
    hooks:
      - id: mypy
        files: ^src/core/|^gateway/storage/|^src/services/llm
        additional_dependencies: ["pydantic>=2.11", "pydantic-settings>=2.9"]
```

### A.5 `.github/workflows/ci.yml` 新增 job

```yaml
  backend-full-suite:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12", cache: pip }
      - run: python -m pip install --upgrade pip
      - run: python -m pip install -r requirements-dev.txt
      - run: python -m pip install -r gateway/requirements.txt
      - run: python -m pip install pytest-cov
      - name: Full suite (excl. PG integration)
        run: >
          pytest -q
          --ignore=tests/test_phase43a_pr2_reservation_pg_atomic.py
          --ignore=tests/test_phase43b_voice_cleanup_pg_concurrent.py
          --cov=src --cov=gateway --cov-report=term-missing
          --cov-fail-under=60          # 现实起点；每季度 +5%，目标 75%

  python-lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12", cache: pip }
      - run: python -m pip install ruff mypy pydantic pydantic-settings
      - run: ruff check src/ gateway/ --output-format=github
      - run: ruff format src/ gateway/ --check
      - run: mypy src/core/ src/services/llm/ src/services/llm_registry.py gateway/storage/ --ignore-missing-imports --check-untyped-defs

  file-size-guard:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Block NEW oversized python files (>800 lines, with frozen whitelist)
        run: |
          # 白名单 = 当前已超标文件（冻结，不许再变大；逐步清零后删除对应行）
          python - <<'PY'
          import subprocess, sys
          WHITELIST = {
            "src/pipeline/process.py","gateway/job_intercept.py",
            "src/services/transcript_reviewer.py","src/services/gemini/translator.py",
            "src/services/jobs/api.py","gateway/traffic_analytics.py","gateway/admin_settings.py",
            # … 其余当前超标文件，建基线时用 find 一次性生成 …
          }
          import pathlib
          bad = []
          for p in pathlib.Path("src").rglob("*.py"):
              n = sum(1 for _ in open(p, encoding="utf-8", errors="ignore"))
              if n > 800 and str(p).replace("\\","/") not in WHITELIST:
                  bad.append((str(p), n))
          for p in pathlib.Path("gateway").rglob("*.py"):
              n = sum(1 for _ in open(p, encoding="utf-8", errors="ignore"))
              if n > 800 and str(p).replace("\\","/") not in WHITELIST:
                  bad.append((str(p), n))
          if bad:
              print("New files exceed 800-line ceiling:")
              for f,n in bad: print(f"  {f}: {n}")
              sys.exit(1)
          PY
```

前端 eslint warning budget（在现有 `frontend` job 追加）：

```yaml
      - name: ESLint warning budget
        run: |
          WARN=$(npx eslint src/ --format json | python3 -c "import json,sys;print(sum(f['warningCount'] for f in json.load(sys.stdin)))")
          echo "warnings=$WARN"; [ "$WARN" -le 50 ] || { echo "::error::warnings $WARN > 50"; exit 1; }
```

### A.6 跨切面规范统一片段

**TTS Provider 注册表**（替换 `tts_generator.py:1372-1448` if/elif）：

```python
# src/services/tts/provider_registry.py（新建）
_TTS_PROVIDERS: dict[str, type[TTSProviderBase]] = {
    "cosyvoice": CosyVoiceProvider, "mimo": MiMoProvider,
    "minimax": MinimaxProvider, "volcengine": VolcEngineProvider,
}
def get_tts_provider_cls(name: str) -> type[TTSProviderBase]:
    cls = _TTS_PROVIDERS.get(name)
    if cls is None:
        raise TTSConfigurationError(f"Unknown TTS provider: {name!r}")
    return cls
```

**统一原子写**（修 DRY-02 的 fsync 缺失，收口 6 处实现）：

```python
# src/utils/atomic_io.py — 扩展 atomic_write_json
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

**统一 admin 鉴权**（消除 13 份副本）：

```python
# gateway/admin_auth.py（新建）
def is_admin(user: "User | None") -> bool:
    return bool(user and (getattr(user, "role", None) or "user") == "admin")
def require_admin(user: "User | None") -> "User":
    if user is None: raise HTTPException(401, "未登录")
    if not is_admin(user): raise HTTPException(403, "需要管理员权限")
    return user
```

---

## 7. 风险与执行纪律

- **多 agent 协作**：本项目由项目主 + 多 AI agent 协作，结构重构必须在**独立 worktree + feature 分支**进行，禁止多 agent 同时对共享工作树做切分支/stash/reset（见 CLAUDE.md）。
- **重构铁律**：拆分类/文件时**只移动代码、不改逻辑**，每步 `pytest tests/ -x -q` 绿灯后再继续；金融模块（credits/billing）**先补集成测试再迁**。
- **付费 API 硬约束不可破**：任何 fallback/缓存优化都不得在 except/retry 路径自动触发付费 TTS/clone/LLM/ASR。本报告所有性能建议（TTL 缓存、to_thread）均不涉及付费调用点。
- **先护栏后重构**：必须先落地 P0 的「全量测试 job + 文件行数门」，再开始 P2 结构拆分——否则无回归网且巨型文件继续生长。
- **提交纪律**：用显式 pathspec（`git commit -- <files>`），勿 `git add .`（会误纳 `.codegraph/` / `.codex_worktrees/`）。

---

## 8. 附录 B：本次调查方法

- 13 路并行子代理（Sonnet 4.6，独立额度，不消耗主模型订阅），每路一个维度，read-only，强制 `file:line` 引用；其中 8 路用专家 agent（architect / refactor-cleaner / silent-failure-hunter / performance-optimizer / python-reviewer / database-reviewer / react-reviewer / fastapi-reviewer）。
- **2 个 agent 未完成，均由主模型亲自接管补做**：① 测试质量维度调查 agent（疑似跑全量 pytest 挂住而终止）→ 主模型用 `pytest --collect-only`（8,474 测试 / 6.7s）、`pip show`、限时实跑、conftest/fixture/mark grep 补做出完整 §5.13；② 准确性审查 critic（卡在工具调用 ~1h）→ 主模型从 workflow journal 取出 12 份已完成结论，逐条对最高危新结论现场核实（§2）。两块缺口已显式标注，未隐瞒。
- 主模型（Opus）综合全部结构化结论，并对最高危的**新增**结论逐条打开 `file:line` 现场核实（§2 标 ✅ 的 5 项 + alembic merge + 工具链/日志硬数据），刻意校准了 TS-01 的真实影响边界，避免误报为「全量字幕损坏」。
- 全程排除 worktree 副本（`.codex_worktrees` / `.codex_tmp` / `.claude/worktrees` / `.venv` / `node_modules` / `.next`）。
