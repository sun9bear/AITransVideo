# CodeX 任务单元审核与决策建议（2026-06-25）

## 审核范围

本文审核 `docs/plans/code-quality-tasks/` 下的 TU-01 至 TU-18 任务单元文档，并顺带检查 TU-00 索引的一致性。审核目标不是重新实现方案，而是判断每份任务单元是否适合派发给独立 agent，以及其中的「需项目主确认」问题应如何拍板。

审核口径：

- 优先保护项目不变量：SemanticBlock 作为 TTS 单元、DSP-first alignment、数学 retiming、剪映 draft 为主交付物、付费 API 不被自动触发。
- 用 Module / Interface / Seam / Adapter / Locality / Leverage 的标准判断拆分是否值得：新模块必须减少调用者认知负担，而不是只把长文件搬成多个浅文件。
- 每个任务单元应可独立分支、独立验收、独立回滚；未决问题不得留给执行 agent 临场猜。
- 对涉及生产配置、数据库迁移、付费路径、前端兼容字段的任务，默认要求项目主先确认。

## 总体结论

这 18 份任务单元整体是合理的，可以作为多 agent 派发基础。Claude Code 后续批量生成前先校准样板是对的，TU-01 里 H3/H5 两个关键问题已经修正为正确方向：取消标记覆盖问题覆盖两处 running 写入，`DubbingSegment` 不新增 `en_text`，改复用既有 `source_text`。

但不建议「不改文档就直接全量开工」。当前仍有三类问题需要先处理：

1. **分支命令残留不一致**：TU-01/TU-02/TU-03 元数据写 `quality/...`，但 Step 0 仍是 `git switch -c claude/...`。应统一改为 `quality/...`，否则多 agent 派发时容易出现分支命名漂移。
2. **若干任务有架构边界决策未定**：尤其 TU-06、TU-07、TU-09、TU-12、TU-13、TU-16。必须先决定兼容字段、mypy 范围、shared helper 放置、internal auth 前置、付费 LLM seam、DB migration 时机。
3. **TU-17 建议拆 PR**：文档可以保留一份，但执行时应拆成「logs cursor」和「benchmark harness」两个 PR，降低可观测性改造与工具脚本互相拖累的风险。

建议先修正文档一致性，再执行 Wave A；Wave B 之后的重构单元按本文决策建议更新原文档后再派发。

### 代码核实后的四项拍板建议

2026-06-25 进一步核实部署、前端、DB、Gateway/Job API 代码后，原先需要项目主选择的四个问题建议直接按以下口径拍板：

- **TU-02 部署构建卫生**：同意生产 compose 转向 immutable image。删除 app 服务的 3 个开发期代码热更新 bind mount；保留数据、config、jobs、model cache 等持久化挂载；暂不动 gateway 对 `app/src` 的只读挂载。Deno `curl | sh` 安装删除。`cloudflared` pin 当前生产已验证 digest，不盲目选最新版 tag。`.env.example` 只补变量名、空值和说明，不写真实 secret。
- **TU-10 free mode checkbox**：保留「离开 free mode 自动清除授权勾选」的产品语义，但把清除动作从 `useEffect` 移到模式切换事件 handler。即新增 `selectServiceMode(next)`，在 `next !== "free"` 时清 `freeVoiceRightsConfirmed`，再设置 `serviceMode`。
- **TU-16 DB hygiene**：拆成非 migration PR 和 migration PR。先做 `pool_pre_ping` / statement timeout、分页、ORM/default 对齐等低风险卫生；CHECK/Numeric 迁移单独排维护窗口。`CreditsLedger.direction` 的 CHECK 白名单必须由代码和生产 `select distinct direction` 双重确认，不能照模型注释抄，因为代码已有 `revoke` 等注释外方向。`UserNotification.id` 生产 migration 有 `server_default=gen_random_uuid()`，ORM 应补齐 `server_default`；`SupportAIUsage Float -> Numeric` 暂缓到独立迁移。
- **TU-18 中长期治理**：Job API 迁 FastAPI 在 TU-09/TU-12 合并后立即评估，但现在不启动实现；JSON store 迁 DB 等 TU-17 benchmark 与生产 job 数/P95 数据；coverage 硬门先跑 nightly baseline，初始阈值取首次实测覆盖率向下 5%，不直接设 75%。

## 跨文档必须修正

| 项目 | 当前问题 | 建议决策 |
|---|---|---|
| 分支命令 | TU-01 第 29 行、TU-02 第 29 行、TU-03 第 26 行仍使用 `claude/...` 建分支命令 | 全部改为 `quality/...`；TU-00 可保留「Claude Code 用 claude、CodeX 用 codex」作为说明，但执行建议统一中性分支 |
| 命令环境 | 文档声明默认 Git Bash / CI Linux，PowerShell 需等价命令 | 保留该声明；派发给 Codex/Windows agent 时，任务开头必须写明可用 PowerShell 等价命令 |
| 兼容字段 | 多处提到字段重命名或响应格式统一 | 任何面向前端或第三方的字段，不做硬替换；先双写再观察 |
| 测试门槛 | 多数文档有 report-only / baseline 设计 | 第一阶段不设全仓硬门；只对 changed files 和新增 guard 做阻断 |
| 回滚边界 | 有些文档写 `git reset --hard` | 执行时优先用独立 commit + `git revert`；不要把 `git reset --hard` 作为常规回滚指令发给 agent |

## 逐项审核与决策建议

| 单元 | 审核结论 | 需要拍板的问题 | CodeX 建议决策 |
|---|---|---|---|
| TU-01 hotfix-stabilize | 方向正确，可以优先执行。H3 已覆盖初始快照和每段进度写入，H5 已改为 `source_text`，不再制造双事实源。 | Step 0 分支命令仍残留 `claude/hotfix-stabilize`。 | 先把分支命令改为 `quality/hotfix-stabilize`，然后直接执行。H2 只加结构化日志，不改变 `return 0` 兜底语义；H3 必须让所有 running 写入走同一 helper；H5 禁止新增 `en_text` 字段。 |
| TU-02 build-hygiene | 两段式拆分合理。A 本地构建卫生和 B 生产配置确认不应混在一个无条件 PR 中。代码核实显示 app 服务确有 3 个开发期 code bind mount，生产部署文档又把根 compose 作为生产入口。 | 是否删除 dev bind mount、cloudflared pin 到哪个版本或 digest、`.env.example` 36 项是否全部公开。 | 决策：同意生产化，但分层做。删除 app 的 3 个开发期代码热更新 bind mount；保留数据/config/jobs/model_cache 等持久化挂载；暂不动 gateway 对 `app/src` 的只读挂载。删除 Dockerfile 中 Deno `curl \| sh` 安装。`cloudflared` pin 当前生产已验证 digest，不选随机最新版。`.env.example` 只补变量名、空值和说明，不填真实 secret。分支命令改为 `quality/build-hygiene`。 |
| TU-03 quality-scaffold | 作为质量护栏脚手架是必要的，report-only 和 changed-files 阻断组合也合理。 | `uv.lock` 是否同步、CI 是否首周不阻断、pre-commit 是否强制安装。 | 推荐同步运行 `uv lock` 并提交锁文件；若临时选择 pip-only，必须在 PR 描述说明。首周 full suite 和全仓 ruff 保持 report-only / continue-on-error，不设 coverage 硬门。pre-commit 配置可提交，但不强制每个开发者本地安装。分支命令改为 `quality/quality-scaffold`。 |
| TU-04 atomic-write | 价值明确，但要避免「字节语义变化」被包装成重构。 | `editing_segments` / `editing_voice_map` 是否接受 `sort_keys` 变化；`editing_voice_map` / `review_actions` 是否默认启用 fsync。 | `editing_segments` 和 `editing_voice_map` 迁移时传 `sort_keys=False`，保持现有 JSON 字节顺序，避免 diff 噪声。业务状态类写入默认 `fsync=True`；若后续性能数据证明有问题，再按调用点降级。目录 fsync 可作为后续增强，不要求本单元完成。 |
| TU-05 admin-auth-dep | 安全收益高，适合 Wave B。统一 admin Interface 是正确方向。 | `gateway/pan/` 子包是否一起迁到共享 `admin_auth`。 | 第一 PR 不迁 `pan/`，先把 gateway 顶层 13 个重复实现收口。`pan/` 保留为显式例外，并在文档和代码注释中说明其认证上下文独立；后续若确认语义完全一致，再单独 PR 迁移。 |
| TU-06 shared-helpers | DRY 收益存在，但错误响应属于外部 Interface，不能硬切。 | legacy `select_voice` 是否有直接调用者；gateway 的 `body["error"]` 前端是否消费。 | `select_voice` 先保留兼容 shim，确认无外部直接调用后再收口。错误响应采用双写：保留旧 `error` 字段，同时新增 `error_code`、`retryable`、`user_action`；至少一个版本周期后再考虑弃用旧字段。`src/utils/error_payload.py` 不应被 gateway 强行 import，gateway 可保持本地 Adapter，但响应 shape 对齐。 |
| TU-07 type-contracts | 方向正确，但 mypy 范围必须收窄。不要用大量 ignore 换一个表面绿色。 | 是否允许给 `gateway/job_intercept.py` 批量加数百处 `# type: ignore`。 | 不允许批量 ignore。当前 PR 只把 `tts_generator` + `aligner` 纳入 mypy 窄域；`compute_job_policy` 可加 TypedDict 和局部测试，但不要让整个 `job_intercept.py` 成为 mypy 阻断目标。`target_language` 若不是 `DubbingSegment` 字段，保留防御式访问并注释原因。 |
| TU-08 billing-logging | 计费和付费路径结构化日志很有价值，但 `process.py` 的 print 需要谨慎。 | 是否把 `process.py` 的 print 迁移纳入同一单元。 | 本单元优先迁移计费、付费 API 重试、LLM fallback、metering skip 等审计盲区。`process.py` 只迁移明确属于诊断日志且不参与 CLI/progress 协议的少量 print；不要追求 print 总数大幅下降。保留既有 observability 测试，不为日志格式重构而改业务断言。 |
| TU-09 intercept-split | 大文件拆分必要，但这是高风险结构单元，必须按 family contract 测试先行。 | shared helper 何时搬到 `intercept/shared.py`；`gateway/main.py` 是否改 import 到新包。 | 第一阶段只迁移各 family 独占函数，共享 helper 先留在 `job_intercept.py` 或只在两处以上明确需要时移到 `intercept/shared.py`。`main.py` 第一 PR 继续从 `job_intercept` re-export 导入，等 family 拆分稳定后再切到 `from intercept import ...`，减少回归面。 |
| TU-10 edit-page-shell | 前端 shell 化是合理的，FE-002 根因处理方式也对。代码核实显示提交 payload 和 Gateway 都已有 free consent fail-closed，但当前注释明确要求 consent 不得 linger。 | 离开 free mode 后 checkbox 视觉状态是否自动清除；eslint hook 规则是否立刻改 error。 | 决策：保留「离开 free 自动清除」语义，但换实现方式。新增 `selectServiceMode(next)`，在 `next !== "free"` 时 `setFreeVoiceRightsConfirmed(false)`，所有方案卡片点击改调该 helper；不要再用 `useEffect` 清 state。eslint 从 warn 改 error 前先全仓扫描；若仍有遗留违规，先 warn + 追踪清单，不用大量 disable 掩盖。 |
| TU-11 voice-select-shared | 边界划分正确。共享类型、常量、纯函数、候选加载 hook，保留组件私有 draft 初始化。 | hook 是否负责下游 draft / voice state 初始化。 | 不负责。`useVoiceCandidates` 只加载和缓存 candidate map；两个组件各自的 draft 初始化留在组件内。这个 seam 深度合适，避免共享 hook 变成业务大杂烩。 |
| TU-12 jobsapi-dispatch | dispatch table 化能提升定位性，但不是大幅减行工具。 | 同一 family 下是否做二级 dispatch；internal endpoint 的 `X-Internal-Key` 校验是否前置；低净减行是否接受。 | 第一版采用一级 dispatch + family handler 内部少量 if/elif，二级 table 留给后续。`X-Internal-Key` 做 dispatch 前统一 guard，可以接受，但必须有 contract 测试证明未授权仍被拒绝、授权路径状态码不变。接受低净减行，收益指标改为 Locality 和路由可定位性。 |
| TU-13 jobservice-postedit | 抽 `EditingApplicationModule` 有价值，但要避免把 orchestration 和付费触发藏进模块。 | 是否覆盖已有测试文件；`suggest_split_for_segment` 付费 LLM 路径如何迁；regen async/thread 依赖放哪。 | 若测试文件已存在，只扩展不覆盖。`suggest_split_for_segment` 只能保留 user-initiated Interface，不得被批量或后台路径调用。regen async/thread 生命周期先留在 `JobService` 薄层，除非新模块显式接收 `runner/status_store` Adapter 并有取消测试覆盖。 |
| TU-14 process-converge-1 | 必须按 ADR Option B 小步走，文档基本正确。 | `AlignedSegment` 是否仍被活跃路径消费；`PROCESS_PY_SIZE_BASELINE` 何时填写。 | 未证明无活跃消费者前，不删除 `AlignedSegment` 或 deprecated writer；把调查结论写进 PR。baseline 只填 Step 5 完成后的实测行数，不预填宽松值。不要借本单元改 pipeline 架构方向，只做输出收敛第一刀。 |
| TU-15 perf-bounded | 有界性能优化合理，范围控制较好。 | settings cache 写后是否 invalidate；孤立任务对账是否加 LRU / 后台；慢磁盘 helper 如何限时。 | 选择写后 invalidate，保证管理配置读写立即一致。孤立任务对账先测量，不预加 LRU 或后台任务。同步磁盘和 HTTP helper 使用 `asyncio.to_thread` 包裹，并对可能慢的 helper 加 `asyncio.wait_for`；暂不引入自定义线程池。 |
| TU-16 db-hygiene | 数据库卫生值得做，但 migration 和非 migration 要拆开。代码核实显示 DB engine 当前缺 `pool_pre_ping`，`CreditsLedger.direction` 模型注释落后于实际代码，`UserNotification.id` 的 ORM 与 migration default 不完全对齐。 | `CreditsLedger.direction` CHECK 上线时机；`UserNotification.id` 是否 server default；`SupportAIUsage Float -> Numeric` 是否值得。 | 决策：先合非 migration PR，再排 migration PR。第一 PR 做 `pool_pre_ping` / statement timeout、分页、ORM default 对齐等低风险改动。`CreditsLedger.direction` CHECK 前先跑生产 `select distinct direction from credits_ledger`，白名单至少覆盖代码中的 `grant/reserve/capture/release/revoke/rollback` 等实际方向，不能照模型注释抄。`UserNotification.id` ORM 补 `server_default=text("gen_random_uuid()")`。`SupportAIUsage Float -> Numeric` 属独立 migration，暂缓到维护窗口。 |
| TU-17 events-benchmark | 方向很好，但两个目标独立，执行上应拆 PR。 | 是否一口气做 logs cursor 和 benchmark harness。 | 文档可保留一份，执行拆成两个 PR：先做 `/logs` 增量 cursor，保持无参数调用完全兼容；再做 benchmark harness，输出 artifact/report-only，不阻断普通 CI。前端 polling 调参不在本单元做。 |
| TU-18 governance-gate | 作为决策门文档合格，开放问题清楚。代码核实显示 Gateway 已是 FastAPI，但 Job API 仍是 `BaseHTTPRequestHandler`；当前 CI 只有精选测试，没有 coverage gate。 | FastAPI 迁移时机、JSON store 迁 DB 的数据门槛、coverage 初始阈值。 | 决策：TU-09/TU-12 合 main 后立即评估 Job API 迁 FastAPI，但现在不启动实现。JSON store 迁 DB 现在不做，等 TU-17 benchmark 和生产 job 数/P95 数据。coverage 硬门不直接设 75%；先跑 nightly baseline，初始阈值取首次实测覆盖率向下 5%，后续逐步上调。 |

## 建议派发顺序

1. 先修正 TU-01/TU-02/TU-03 的分支命令残留，并把本文决策回填到对应任务文档的「需项目主确认」位置。
2. 执行 Wave A：TU-01 可直接开；TU-02 只做 A 段，本地构建卫生先合；TU-03 按 report-only 方式合。
3. Wave B 中 TU-05、TU-06、TU-08 可并行；TU-04 等 TU-01 合并后开；TU-07 等 TU-03 合并后开。
4. Wave C 中先做 TU-11 或 TU-10 这类前端局部重构；TU-09/TU-12/TU-13 每个都需要 contract tests 先行，不建议同时改同一接口入口。
5. Wave D 中 TU-15/TU-17 可先于 TU-14/TU-16；TU-16 migration 部分单独排维护窗口。
6. TU-18 不执行代码，只在 TU-09/TU-12/TU-17/TU-03 产出数据后更新决策状态。

## 必须回填到原任务文档的改动清单

- TU-01：Step 0 分支命令改 `quality/hotfix-stabilize`。
- TU-02：Step 0 分支命令改 `quality/build-hygiene`；标明生产化决策为「删 app 开发期 code bind mount，保留持久化挂载和 gateway 只读 src 挂载」；`cloudflared` pin 生产 digest；`.env.example` 只补空值说明。
- TU-03：Step 0 分支命令改 `quality/quality-scaffold`；明确 `uv lock` 为首选。
- TU-04：默认 `sort_keys=False` 保持 editing JSON 字节语义；业务状态写默认 `fsync=True`。
- TU-05：`pan/` 第一 PR 保留例外，不作为 DoD 必须迁移。
- TU-06：错误响应双写 `error` + `error_code`，不硬替换。
- TU-07：禁止批量 `type: ignore`；mypy 范围只到窄域。
- TU-08：`process.py` print 迁移只做明确诊断日志，不追求总量。
- TU-09：第一 PR 维持 `main.py` import 经 re-export；shared helper 延后或最小化。
- TU-10：free mode checkbox 必须在离开 free 时清除，但清除动作移到模式切换事件 handler，禁止回到 effect。
- TU-12：internal key 前置 guard 必须配 contract test。
- TU-13：付费 LLM suggest 必须保持 user-initiated；regen thread orchestration 默认留在薄层。
- TU-16：migration 与非 migration 拆 PR；第一 PR 加 `pool_pre_ping` / statement timeout、分页、ORM default 对齐；CHECK 约束前先跑生产 `select distinct direction`；`SupportAIUsage Float -> Numeric` 暂缓到维护窗口。
- TU-17：执行拆两个 PR，cursor 与 benchmark 分离。
- TU-18：Job API FastAPI 迁移等 TU-09/TU-12 后评估但现在不做；JSON store 迁 DB 等 TU-17 数据；coverage 阈值基于首次 nightly baseline 向下 5%，不是直接 75%。

## 最终建议

可以采纳 Claude Code 的 18 单元拆分，但要把它视为「可派发的任务包草案」，不是最终执行指令。最稳的下一步是先由文档维护 agent 按本文回填原 TU 文档，再启动 Wave A。这样后续每个执行 agent 只需要遵守自己那份任务书，不需要重新理解整套治理方案。
