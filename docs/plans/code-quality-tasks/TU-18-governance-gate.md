# TU-18 · 中长期治理决策门（Phase 4）

- **目标 / 价值**：四项中长期架构演进的**决策框架**——不写实现代码，而是为每项给出「触发条件 / 前置就绪信号 / 收益 / 风险 / 可判断的决策标准 / 不做的代价」，让项目主在未来某个时间点能据此做出有依据的开展 / 暂缓 / 放弃决策，而不是在压力下拍脑袋。
- **关联发现**：母方案 §9 Phase 4；§6.1 STRUCT-02；§5.1 TOOL-01/03；§7 分阶段路线图；§2 架构不变量
- **前置依赖**：本文档本身无前置（随时可读）。四项决策各自的「开启前置」见对应小节。**本单元不产出代码**——DoD 是「决策标准清晰、项目主可据此判断是否启动各项」。
- **建议分支**：`quality/governance-gate`（仅文档提交，无代码变更）
- **预估工时**：决策（非实现）——文档起草约 S；各项实际执行工时在决策时另估

> **命令环境**：本文档内所有"确认现状"命令默认 **Git Bash / CI Linux**（仓库已配 Bash 工具）。PowerShell 执行者改用等价命令：`grep` → `Select-String`、`tail -n N` → `Select-Object -Last N`、`test -f` → `Test-Path`、`wc -l` → `(Get-Content file | Measure-Object -Line).Lines`；避免 `<(...)` 进程替换。

---

## 决策记录（CodeX 审核 2026-06-25，已采纳）

1. **Q1 已定方向（Job API 迁 FastAPI）**：TU-09 / TU-12 合 main 后立即评估迁移可行性；**现在不启动实现**，评估结果决定是否开独立 TU。
2. **Q2 已定方向（JSON store 迁 DB）**：**现在不做**。等 TU-17 benchmark harness 跑出生产 `list_jobs` P95 数据与实际 job 文件数后再决定是否启动，触发条件 B1/B2/B3 以真实观测数据为准。
3. **Q3 已定方向（coverage 硬门阈值）**：**不直接设 75%**。先跑 nightly baseline（`backend-full-suite continue-on-error: true`），初始阈值取**首次实测覆盖率向下取整 5%**（如实测 62% → 设 57%），此后逐步上调，最终目标 ≥ 75%。
4. 上述三条均为「已定方向」，不再是开放确认项；各项执行时的前置验证动作（跑 SQL 计数、读 P95 日志、读 nightly 报告）保留为执行时前置动作。

---

## 不在本单元范围（out-of-scope）

- 执行任何代码变更、迁移或重构——本单元输出仅为文档。
- 为四项中的任何一项制定逐步执行方案——那是各项决策后各自独立 TU 的事。
- 评估 Wave A–D 各单元内已经明确要做的改动（TU-01 至 TU-17）。
- 讨论 process.py Option B 收敛的具体步骤（属 TU-14，ADR 已存档于 `docs/architecture/PROCESS_WORKFLOW_CONVERGENCE.md`）。

---

## 必守不变量

以下不变量对四项决策均约束（决策时不得提出与之相悖的方案）：

1. **付费 API 硬约束**：MiniMax 付费克隆 / 付费 TTS / 付费 LLM / 付费 ASR 绝不在 fallback / except / retry / batch 路径自动触发；只走用户显式 consent。任何迁移计划不得引入新的自动付费路径。
2. **Gateway 是 plan / pricing / entitlement 唯一事实源**：迁 FastAPI 或迁 DB 均不得把这些事实下沉到 Job API 侧或前端。
3. **默认测试不接真实外部服务**：新框架下的测试仍须可在无外部依赖的本地环境跑通。
4. **process.py 走 Option B**（ADR `docs/architecture/PROCESS_WORKFLOW_CONVERGENCE.md`）：退成兼容壳消费 `ProjectWorkflow` / `OutputDispatcher`；不另起独立架构；Job API 迁 FastAPI 不得成为绕过 Option B 的理由。
5. **Alignment DSP-first，retiming 数学确定性**：迁移技术栈不得把确定性对齐迁到 LLM 决策路径。
6. **剪映 draft 为主交付物**：任何 JSON store 迁 DB 的决策不得破坏 `jianying_draft_runner.py` 对本地文件路径的依赖。

---

## Step 0 · 确认现状

> 执行此步以便将本文档中的 file:line 与运行时实际位置核对。行号可能因并行 agent 提交而漂移——以实际输出为准。

```bash
git switch -c quality/governance-gate

# 1. 确认 Job API 当前使用的 HTTP 框架（stdlib，非 FastAPI）
grep -n "BaseHTTPRequestHandler\|ThreadingHTTPServer\|from fastapi\|from flask" \
    src/services/jobs/api.py | head -5
# 预期：仅见 BaseHTTPRequestHandler / ThreadingHTTPServer（line 9）；无 fastapi

# 2. 确认 JobStore 是 JSON 文件存储（非数据库）
grep -n "class JobStore\|file_lock\|_write_json_atomic\|SQLAlchemy\|engine" \
    src/services/jobs/store.py | head -10
# 预期：class JobStore(line ~44)，file_lock，无 SQLAlchemy

# 3. 确认 ci.yml 中尚无 ruff/mypy/coverage 阻断 job
grep -n "python-lint\|ruff\|mypy\|cov-fail-under\|backend-full-suite\|file-size-guard" \
    .github/workflows/ci.yml | head -10
# 预期：均无匹配（TU-03 落地前）

# 4. 确认 openapi-typescript / hey-api / orval 等 TS 生成工具未安装
grep -rn "openapi-typescript\|@hey-api/openapi-ts\|orval\|zodios" \
    frontend-next/package.json frontend-next/package-lock.json 2>/dev/null | head -5
# 预期：无匹配

# 5. 记录关键热点文件当前行数（决策触发指标基线）
wc -l src/pipeline/process.py \
       src/services/jobs/api.py \
       gateway/job_intercept.py \
       src/services/jobs/store.py
# 预期（2026-06-24 基线）：process.py ~12806 / jobs/api.py ~2645 / job_intercept.py ~6880 / store.py ~460

# 6. 确认 ruff / mypy 尚未在 pyproject.toml 配置为质量门（TU-03 前）
grep -n "tool.ruff\|tool.mypy\|cov-fail" pyproject.toml | head -5
# 预期：无匹配或仅见 dev 依赖列表（无 [tool.ruff] 节）
```

> ⚠️ **行号漂移说明**：`api.py:9`（BaseHTTPRequestHandler import）、`store.py:44`（class JobStore）为 2026-06-24 基线，如有漂移按实际输出更新本文档对应引用。

---

## Step 1 · 决策项 A：Job API 迁 FastAPI

### 背景

当前 Job API（`src/services/jobs/api.py`，2,645 行）使用 **stdlib `BaseHTTPRequestHandler` + `ThreadingHTTPServer`**（`api.py:9`）构建，包含约 32 个方法的手工路由分发（`do_GET`/`do_POST`/`do_DELETE`）。这与 Gateway 侧已全面使用 FastAPI 形成二元异构，带来以下代价：

- 无请求级 Pydantic 自动校验，入参解析全靠手工 `parse_qs`。
- 无标准 OpenAPI schema 生成，TS contract 生成（决策项 C）被迫依赖此项。
- 错误响应手工构造，与 Gateway 的 FastAPI `HTTPException` 格式不统一。
- 手工线程管理（`ThreadingHTTPServer`），无 `asyncio` 原生支持，async 调用需额外 workaround。

### 触发条件（满足任一项，启动评估）

| # | 信号 | 可验证命令 |
|---|---|---|
| A1 | TU-09 / TU-12 完成（job_intercept 路由拆分 + jobs/api dispatch table），**route contract 测试集已就位** | `pytest -m contract -q 2>&1 \| grep -c "passed"` 结果 > 0 |
| A2 | `jobs/api.py` 新增需求（如 middleware 鉴权、WebSocket、streaming 响应）在 stdlib 框架下实现代价显著增大 | 开发者主观评估 + 工时超出预期 ≥ 2x |
| A3 | CI `python-lint` job（TU-03）稳定运行 ≥ 4 周，无历史债阻断 | `gh run list --workflow=ci.yml --status=success \| head -20` |

### 前置就绪信号（开启前必须全部达到）

- [ ] TU-09（job_intercept 路由族拆分）已合 main，路由不变量测试绿。
- [ ] TU-12（jobs/api dispatch table 化）已合 main，每个 handler 已有独立 contract 测试。
- [ ] TU-03 `python-lint` CI job 已就位（ruff + 窄域 mypy），且对 `src/services/jobs/api.py` 已跑通。
- [ ] `tools/file_size_baseline.json` 已生成并提交（TU-03 Step 4），api.py 的基线行数已记录。

### 收益

- Job API 与 Gateway 同构（均 FastAPI），路由声明式化，消除手工 `parse_qs` 和手工响应构造。
- 自动 OpenAPI schema，为决策项 C（TS contract 生成）提供 schema 来源。
- 原生 `async def` handler，消除 `asyncio.to_thread` 绕行需求。
- 标准 `HTTPException` 格式与 Gateway 错误载荷统一（见 §6.4 DRY-03/04）。

### 风险

| 风险 | 严重度 | 缓解 |
|---|---|---|
| 迁移期间并行 agent 冲突（`api.py` 是高热点） | 高 | 独立 worktree；迁移前冻结对 `api.py` 的其他 PR |
| ThreadingHTTPServer → Uvicorn 并发模型差异（线程 vs 协程）导致隐性竞争 | 中 | 逐 handler 迁移；每批跑现有 contract 测试；先在 staging 验证 |
| Gateway → Job API 内部调用的 `internal_headers` 注入需同步更新 | 中 | `gateway/internal_auth.py:internal_headers()` 调用点不变，只改 Job API 接收侧 |
| 迁移工时超预期（当前 32 个 handler + 手工路由） | 中 | dispatch table（TU-12）完成后再启动，将路由表 → FastAPI router 映射机械化 |

### 决策标准

**开展**（同时满足）：
1. 上述前置就绪信号全部打勾。
2. 触发条件 A1 已达成（route contract 测试集就位）。
3. 项目主评估迁移窗口不与其他大型功能 PR 重叠。

**暂缓**（满足任一）：
- TU-09 / TU-12 任一未完成（缺少 contract 覆盖，迁移无回归网）。
- 当前有活跃的 Job API 功能 PR（避免同区域冲突）。
- `jobs/api.py` 行数在 TU-12 后已降至 1,500 行以下且无新增 async 需求（迁移 ROI 下降）。

**不做的代价**（永久不迁）：
- 无法生成标准 OpenAPI schema，决策项 C（TS contract 生成）需另辟蹊径（手工维护 schema 或用 Gateway 侧生成）。
- Job API 异常/超时处理永远独立维护，与 Gateway 错误格式持续分叉。
- 新增 middleware 需求（如 tracing header）需在两套框架分别实现。

> ✅ **已决策（CodeX 2026-06-25）**：TU-09 / TU-12 合 main 后立即评估迁 FastAPI 可行性，现在不启动实现。执行时前置动作（已定方向）：评估前确认当前无活跃 Job API 功能 PR，若有冲突推迟评估窗口，由执行者自行判断排期。

---

## Step 2 · 决策项 B：JSON store 迁 / 混合 DB

### 背景

当前 `JobStore`（`src/services/jobs/store.py`，约 460 行）以 **JSON 文件 + file_lock + in-memory list cache** 实现作业持久化（每个 job 一个 `.json` 文件，目录 = `AIVIDEOTRANS_JOBS_DIR`）。`gateway/models.py` 通过 SQLAlchemy + Alembic 维护用户/计费/语音等关系型数据（PostgreSQL）。

当前已知的查询压力点：
- `intercept_list_jobs`（`gateway/job_intercept.py:1360`）需全表扫描 `SELECT job_id`，无法利用 DB 索引过滤。
- list cache（`JobStore._list_cache`）仅在同进程有效，多进程 / 容器重启后冷启动重扫目录。
- 事件日志（`.events.jsonl`）在 `gateway/storage/event_log.py` 已独立为 JSONL append-only——这是「先 event log 再 store 迁移」的合理分层。

### 触发条件（满足任一项，启动评估）

| # | 信号 | 量化阈值 |
|---|---|---|
| B1 | `intercept_list_jobs` P95 响应时间在生产环境持续超过 **2,000 ms** | `grep "intercept_list_jobs" runtime_logs/*.log \| grep -oP '"duration_ms":\K[0-9]+' \| sort -n \| awk 'NR==int(0.95*NR)'`（需 benchmark harness TU-17 就位） |
| B2 | 单作业 JSON 文件数量超过 **50,000 个**（目录扫描 Cold start 耗时 > 10s） | `ls -1 $AIVIDEOTRANS_JOBS_DIR/*.json 2>/dev/null \| wc -l` |
| B3 | 出现 **跨进程并发 job 写冲突**（file_lock 无法在多容器水平扩展场景保护共享状态） | 生产事故报告 + `grep "file_lock" runtime_logs/*.log \| grep -i "error\|timeout"` |
| B4 | 需要复杂 **跨字段查询**（如「过去 7 天由 user X 创建且 status=succeeded 的 job」）且通过 list cache 实现代价 > 2x SQL | 开发者评估 |

### 前置就绪信号（开启前必须全部达到）

- [ ] TU-17（events/logs cursor 化）已合 main，event 已独立于 job JSON，不阻塞 store 迁移。
- [ ] TU-04（统一原子写 helper）已合 main，所有 JSON 写入点已归一（便于替换底层存储）。
- [ ] `file_size_baseline.json` 白名单中 `store.py` 的行数已记录（迁移后可量化瘦身）。
- [ ] benchmark harness（TU-17）已跑出 `list_jobs` / `get_job` 的 baseline P95 数据。

### 收益（分三种迁移形态）

| 形态 | 说明 | 主要收益 |
|---|---|---|
| **A：JSON 保留，加 SQLite 索引层** | 仍写 JSON，另维护 SQLite 作索引（job_id / user_id / status / created_at） | 最低迁移代价；list 可走索引查询；多进程可共享索引 |
| **B：混合 DB**（Job metadata → PG，payload 仍 JSON） | `JobRecord` 标量字段进 PG `jobs` 表，`workspace_dir` / `artifacts` 路径仍文件 | 可跨容器查询；Gateway 侧 ORM 可直接 JOIN；保留大 payload 的文件友好性 |
| **C：全量迁 PG** | job JSON 全部序列化进 PG `jobs.payload JSONB` | 最彻底；`alembic` 统一管理；消除双轨 |

> 推荐序：先评估形态 A（最低风险），再按实际压力决定是否升形态 B / C。

### 风险

| 风险 | 严重度 | 缓解 |
|---|---|---|
| Alembic migration 误操作删除现有 PG 表 | 高 | alembic `heads` 单头断言（TU-16 Step 0）；迁移前 `pg_dump` 备份 |
| `workspace_dir` / `manifest_path` / `jianying_draft_runner` 依赖本地文件路径，迁 DB 后路径串改 | 高 | 迁移 schema 时 `workspace_dir` 仍保留为文件路径字段，不替换为 BLOB |
| `services._file_lock` 跨进程无效（`threading.RLock` 进程内，`fcntl` 进程间可用但容器 overlay FS 不保证） | 中 | 混合 DB 后 file_lock 只用于单文件内部序列化（PG 侧用 `SELECT FOR UPDATE`） |
| list cache 失效导致冷启动 P99 尖刺 | 低 | 形态 A 加 SQLite 索引层即可消除 |

### 决策标准

**开展**（同时满足）：
1. 触发条件 B1 / B2 / B3 任一已达阈值，且已有 benchmark 数据佐证。
2. TU-17 benchmark harness 已就位（有数据，有对比基线）。
3. TU-04 统一原子写已合 main（写入点归一，替换底层代价低）。

**暂缓**（满足任一）：
- 生产 job 数 < 10,000，`list_jobs` P95 < 500 ms——当前 JSON + file_lock 已够用。
- TU-17 / TU-04 任一未完成。
- 当前有活跃的 Alembic migration PR（避免 heads 分叉）。

**不做的代价**（永久不迁）：
- `list_jobs` 随业务增长持续变慢，最终需额外加内存缓存或外部 Redis 来掩盖。
- 多容器水平扩展受限（file_lock 进程内，多进程并发写无跨进程保护）。
- Gateway 侧无法对 job 做 JOIN 查询，数据分析 / 运营大屏需回捞 JSON 文件重建。

> ✅ **已决策（CodeX 2026-06-25）**：JSON store 迁 DB 现在不做。执行时前置动作（已定方向）：TU-17 benchmark harness 就位后，读取生产 job 文件数（`ls -1 $AIVIDEOTRANS_JOBS_DIR/*.json | wc -l`）与 `list_jobs` P95 时延，核对是否达到触发条件 B1/B2/B3，再决定是否开启迁移。

---

## Step 3 · 决策项 C：OpenAPI → TypeScript contract 生成

### 背景

当前前端 API 客户端为**手写薄 fetch wrapper**（`CLAUDE.md` 已定，无 axios / react-query）。类型定义散在 `frontend-next/src/types/`，与 Gateway / Job API 的实际响应结构靠人工同步。未发现任何自动化 schema 生成工具（`package.json` 无 `openapi-typescript` / `@hey-api/openapi-ts` / `orval` 等）。

Gateway 已是 FastAPI 应用，**具备生成 OpenAPI schema 的能力**（`/openapi.json`），但该 schema 是否已在部署中暴露、TS 侧是否已配套消费，需 Step 0 确认。Job API 目前为 stdlib HTTP server，**无内置 OpenAPI 支持**（决策项 A 是 C 的前置之一）。

### 触发条件（满足任一项，启动评估）

| # | 信号 | 可验证方式 |
|---|---|---|
| C1 | 前端 TypeScript 编译错误或运行时类型不匹配 bug **每月 > 3 次**且根因为 API 响应字段名/类型漂移 | PR history + `grep -rn "as any\|@ts-ignore" frontend-next/src/ \| wc -l`（debt baseline） |
| C2 | TU-07（类型契约硬化 + mypy 窄域）完成后，TS 侧仍有 > 20 个 `as any` / `@ts-ignore` 指向 API 响应类型 | `grep -rn "as any\|@ts-ignore" frontend-next/src/lib/api/ \| wc -l` |
| C3 | 决策项 A（Job API 迁 FastAPI）完成，Job API 也能生成 OpenAPI schema | Step 1 决策落地后自动满足 |

### 前置就绪信号（开启前必须全部达到）

- [ ] TU-07（类型契约 + mypy 窄域）已合 main（先硬化现有手写类型，再评估自动生成的收益）。
- [ ] Gateway `/openapi.json` 已在 staging / production 可访问（`curl http://localhost:8880/openapi.json | python -m json.tool | head -20`）。
- [ ] 若希望 Job API 也纳入生成范围：决策项 A 已决定开展并有明确时间表。

### 收益

- API 响应类型与后端 Pydantic schema **单一事实源**，消除手工同步漂移。
- 新增字段 / 改字段后前端立即得到构建期类型错误，而非运行时 `undefined`。
- 减少 API 调用层 `as any` / `@ts-ignore` 数量（可量化 debt 下降）。
- `openapi-typescript`（仅生成类型，无运行时依赖）与现有薄 fetch wrapper 架构完全兼容——不引入 axios / react-query 等重量库。

### 风险

| 风险 | 严重度 | 缓解 |
|---|---|---|
| Gateway OpenAPI schema 的 Pydantic 模型不完整（Optional 字段缺 default，Union 类型过宽），生成的 TS 类型不可用 | 中 | 先评估 `/openapi.json` 的覆盖质量再引入生成工具；不完整则先补 Pydantic 类型 |
| Job API 未迁 FastAPI 时只能为 Gateway 侧生成，与 Job API 类型仍需手写 | 中 | 先只生成 Gateway 侧类型，Job API 侧类型沿用手写（标注 `// TODO: auto-generate after FastAPI migration`） |
| 生成工具版本升级破坏生成结果 | 低 | 锁 `openapi-typescript` 版本在 `package.json`；CI 中检测 schema 变更时重新生成并 diff |
| `Any` 的 Pydantic 字段生成 `unknown`，导致调用点需要 type narrowing 改造量大 | 低 | 逐模块引入（从最干净的响应类型开始），不一次全量替换 |

### 决策标准

**开展**（同时满足）：
1. TU-07 已合 main，手写类型债务 baseline 已建立。
2. 触发条件 C1 或 C2 任一已达阈值（有数据说明手写同步代价实际存在）。
3. Gateway `/openapi.json` 可访问且覆盖率评估 ≥ 70%（主要 response model 已有 Pydantic 类型）。

**暂缓**（满足任一）：
- TU-07 未完成（先硬化手写类型，再评估自动生成的增量价值）。
- Gateway Pydantic model 覆盖率 < 50%（生成 TS 类型噪音太多，维护成本反升）。
- 前端团队近期正在大规模改动 API 调用层（避免生成工具与手动改动的双重维护）。

**不做的代价**（永久不迁）：
- API 类型同步依赖人工纪律，字段漂移 bug 持续发生但难以在 CI 捕获。
- 决策项 A（迁 FastAPI）带来的 OpenAPI schema 生成能力闲置（只有服务端收益，前端无收益）。
- 随接口数量增长，`frontend-next/src/types/` 的手写类型维护成本线性增长。

---

## Step 4 · 决策项 D：ruff / mypy / coverage 全仓阻断

### 背景

当前 CI 只跑约 14 个手选守卫测试（`ci.yml:28-47`），无 ruff / mypy / coverage job。TU-03 将装入三个 job：
- `python-lint`：ruff changed-files 阻断 + 全仓 report-only + 窄域 mypy。
- `backend-full-suite`：`continue-on-error: true`（首周不阻断，nightly 或 report-only）。
- `file-size-guard`：新文件 >800 行阻断。

**全仓阻断**是指在此基础上进一步：
- ruff 全仓 **exit-code 非零即阻断**（不再 `--exit-zero`）。
- mypy 扩展覆盖域（从 `src/core` / `src/utils` 等干净模块扩展到全量 `src/` + `gateway/`）。
- `pytest --cov-fail-under=<阈值>` 设定覆盖率硬门（PR 低于阈值即失败）。

当前历史 ruff 报告行数未知（TU-03 Step 2 的 baseline 尚未建立）。历史 mypy 错误数未知。全量 8,474 个测试从未在 CI 跑过，覆盖率 baseline 从未度量过。

### 触发条件（所有三项子门独立评估，可分开开启）

**ruff 全仓阻断**（满足全部）：
- TU-03 `python-lint` job 稳定 ≥ 4 周（changed-files 阻断已运行，无误报）。
- 母方案 §10.2 配置的 `E/W/F/I/UP` 全仓 report-only **报告行数净减至 0** 或剩余均为合理 `noqa` 抑制。
- 各重构单元（TU-04 ~ TU-13）完成后新产生的 ruff 问题数 < 10。

**mypy 全仓阻断**（满足全部）：
- TU-07（类型契约硬化）已合 main，`src/core` / `src/utils` 等窄域已全绿。
- 全量 `src/` mypy 错误数 < 50（通过分批扩展域逐步清零）。
- `en_text` getattr bug（`src/services/alignment/aligner.py:361/542/591/778`，TU-01 H5）已修，DubbingSegment（`src/services/gemini/translator.py:252`）已补字段。

**coverage 硬门**（满足全部）：
- `backend-full-suite` CI job 已从 `continue-on-error: true` 改为硬门（意味着全量测试在 CI 稳定通过）。
- 首次设定阈值 = nightly baseline 实测全量覆盖率**向下取整 5%**（如实测 62% → 设 **57%**），避免首期即造成无法合 PR；**不直接设 75%**。
- 阈值逐步上调（建议每季度 +3~5%），最终目标 ≥ 75%；每次上调须有对应 nightly 数据支撑。

### 前置就绪信号（全仓阻断前必须全部达到）

| 子门 | 前置 |
|---|---|
| ruff 全仓阻断 | TU-03 report-only 基线建立 + 各重构单元已清理各自引入的新 ruff 问题 |
| mypy 全仓 | TU-07 窄域已全绿 + TU-01 H5 已修（en_text slots bug）|
| coverage 硬门 | TU-03 backend-full-suite nightly 已稳定 ≥ 2 周（无 hang、无随机红）+ `--timeout=120` 插件已装（TU-03 Step 3）|

### 收益

- ruff 全仓阻断：新代码永远不引入旧 lint 问题，代码库质量单调递增。
- mypy 全仓：构建期捕获类型错误，消除「`getattr` 在 `slots=True` dataclass 上静默吞字段」类问题（65+ 处，H5 是实证）。
- coverage 硬门：防止在测试覆盖率上倒退，为重构安全网提供量化保障。

### 风险

| 风险 | 严重度 | 缓解 |
|---|---|---|
| 历史 ruff 问题数量大（`process.py` 12,806 行含大量 print/logging 混用）导致全仓阻断后 CI 持续红 | 高 | 严格遵循「先 report-only、逐批 `--add-noqa` 仅作临时标记、再逐步清除」；不在第一天设全仓硬门 |
| mypy 对 `gateway/models.py`（1,832 行）、`tts_generator.py`（1,856 行）等大文件的 `Any` 覆盖会暴露大量未标注类型，修复量超预估 | 高 | 按目录递进（干净模块 → 中等 → 热点最后）；设 `warn_return_any = false` 过渡期宽松配置 |
| pytest timeout 120s 在某些集成测试仍不够 | 中 | 先 nightly 跑一轮，记录实际耗时分布；按 P99 上调 timeout；给合理的长测试加 `@pytest.mark.slow` 排除 |
| 全量 8,474 测试中有多少当前就红（历史漂移 bug）尚未知 | 高 | 先 `continue-on-error: true` nightly 跑出 baseline；仅在「全量 - PG 集成」全绿后才考虑转硬门 |

### 决策标准

**开展 ruff 全仓阻断**（同时满足）：
1. TU-03 report-only 已运行 ≥ 4 周，基线问题数已记录并已有清理 PR 陆续合入。
2. 全仓 `ruff check src/ gateway/ --exit-zero` 输出行数 < 100（历史问题已基本清零）。
3. `git diff --name-only origin/main... | grep '\.py$' | xargs ruff check` 对改动文件无新增问题。

**开展 mypy 全仓阻断**（同时满足）：
1. TU-07 窄域已全绿（`mypy src/core/ src/utils/ gateway/storage/ --ignore-missing-imports` 无错误）。
2. TU-01 H5 已修（`grep -n "getattr.*en_text" src/services/alignment/aligner.py` 无匹配）。
3. `mypy src/ gateway/ --ignore-missing-imports --check-untyped-defs 2>&1 | grep -c "error:"` < 50。

**开展 coverage 硬门**（同时满足）：
1. `backend-full-suite` CI job `continue-on-error` 已去除且连续 2 周绿。
2. `pytest --co -q 2>&1 | tail -1` 确认 collect 数量未回退（仍约 8,474）。
3. 实测全量覆盖率 baseline 已写入 PR 描述。

**暂缓**（满足任一）：
- 对应前置单元（TU-03 / TU-07）未完成。
- 当前 CI 任意 job 有非预期的持续红（先稳再收紧）。
- 项目处于活跃发布窗口（上线前 2 周内）。

**不做的代价**（永久不收紧）：
- ruff：新代码引入旧 lint 问题无阻断，债务持续累积，月度「清理 lint」任务变成季度大工程。
- mypy：类型注解只有文档价值，`getattr` / `Any` 滥用无构建期惩罚，H5 类 bug 持续出现。
- coverage：覆盖率无基线，重构后覆盖下滑无法量化，「重构=安全」的信念缺乏数据支撑。

> ✅ **已决策（CodeX 2026-06-25）**：不直接设 75% 硬门。执行时前置动作（已定方向）：先跑 `backend-full-suite` nightly baseline（`continue-on-error: true`），以**首次实测覆盖率向下取整 5%** 作为初始阈值（如实测 62% → 设 57%），后续逐步上调，最终目标 ≥ 75%。

---

## 测试计划（新增 / 回归）

**本单元不产出代码，无新增测试。**

四项决策各自开展时，对应执行单元需覆盖：
- 决策项 A 执行时：每个迁移 handler 配对 contract 测试（路径 / 状态码 / 错误格式），`gateway/job_intercept.py:_is_post_edit_mutation_subpath`（`:5044`）的 path-parity 测试必须在迁移前已覆盖。
- 决策项 B 执行时：`JobStore.list_jobs` / `get_job` 的 golden test（固定 job JSON 文件 → 固定返回值），在 store 迁移前后必须绿。
- 决策项 C 执行时：CI 中加 `openapi-typescript --check`（schema 变更时 generated 类型须重新提交），防止 schema 改了但 TS 类型未同步。
- 决策项 D 执行时：各阶段收紧前须确认 `pytest -m "guard or contract" -q` 仍全绿（守卫测试不得因配置收紧而误改）。

---

## 回滚方案

本单元只提交文档文件，无代码变更。回滚 = `git revert <commit>` 该文档。

四项决策各自执行时的回滚边界：
- **决策项 A**：每批 handler 迁移独立 commit；若某批导致 contract 测试红，`git revert` 该批 commit 后回退。
- **决策项 B**：Alembic 迁移必须有对应 downgrade；迁移前 `pg_dump` 备份；分三阶段（A → B → C 形态）逐步迁移，每阶段独立 PR。
- **决策项 C**：生成工具为 dev 依赖，不影响运行时；生成产物（`src/lib/api/generated.ts` 等）独立 commit，可单独 revert。
- **决策项 D**：CI 配置收紧独立 commit；若误触历史问题导致 CI 全红，`git revert` CI 配置 commit 即可恢复。

---

## 完成定义（DoD）

> 本单元的 DoD 是「决策标准清晰」，非代码完成。

- [ ] 四项决策的触发条件均为可量化命令（grep 计数 / 行数 / pytest passed 数），无主观判断标准。
- [ ] 每项的前置就绪信号均可勾选验证（与 TU-01 ~ TU-17 的 DoD 相互引用）。
- [ ] 每项的决策标准明确给出「开展 / 暂缓」二值条件，无模糊措辞。
- [ ] Step 0 确认现状命令已在本地执行，file:line 已与实际代码核对（或标注漂移说明）。
- [x] Q1/Q2/Q3 三个开放问题已由 CodeX 审核（2026-06-25）拍板为「已定方向」，各节末已改为「✅ 已决策」或「执行时前置动作（已定方向）」；不再有待确认的开放决策项。
- [ ] 文档以独立 commit 提交，显式 pathspec（`git commit -- docs/plans/code-quality-tasks/TU-18-governance-gate.md`），**未 `git add .`**。

---

## 开放问题（✅ 已由 CodeX 审核 2026-06-25 拍板）

| # | 问题 | 影响项 | 决策结果 |
|---|---|---|---|
| Q1 | 决策项 A（Job API 迁 FastAPI）的时机 | 决策项 A、C | ✅ TU-09/12 合 main 后立即评估，现在不启动实现 |
| Q2 | 决策项 B（JSON store 迁 DB）：生产 job 文件数与 `list_jobs` P95 | 决策项 B 触发条件 B1/B2 | ✅ 现在不做；等 TU-17 benchmark 出真实数据后再决定 |
| Q3 | 决策项 D（coverage 硬门）目标阈值 | 决策项 D coverage 硬门 | ✅ 不直接设 75%；初始阈值 = 首次 nightly 实测值向下取整 5%，逐步上调至 ≥ 75% |
