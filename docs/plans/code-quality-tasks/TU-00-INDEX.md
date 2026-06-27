# 代码质量治理 · 任务单元总索引（TU-00）

> 母方案：[`../2026-06-24-code-quality-optimization-plan-MERGED.md`](../2026-06-24-code-quality-optimization-plan-MERGED.md)（canonical）。
> 本目录把母方案拆成 ~18 个**可独立派发、可独立成 PR**的任务单元，每个单元一份执行文档（含分步骤 + 每步验收标准）。
> 本阶段**只产出文档、不执行代码**。
>
> **状态（2026-06-25）**：CodeX 已审核全部单元并给逐项决策，已回填进各文档「## 决策记录（CodeX 审核 2026-06-25，已采纳）」节；分支统一 `quality/...`、回滚 `git revert` 优先。可据此派发执行。

## 如何使用

1. 按 **Wave 顺序**推进；同 Wave 内「可并行=是」的单元可分给不同 agent，各自独立 worktree + feature 分支（遵守 [CLAUDE.md 多 agent git 协作模型](../../../CLAUDE.md)：禁止多 agent 共用工作树做改状态操作）。
2. 认领单元 → 读该单元文档 → 建分支 → 按步骤执行（每步先过该步验收）→ 全单元 DoD 达标 → 项目主 review 合并 → 勾掉本索引状态。
3. 每个单元文档的 **Step 0 都是「确认当前状态」**：执行时先复核 `file:line`（仓库是多 agent 并行，行号可能已漂移），以实际代码为准。
4. **命令环境**：各单元验收命令默认 **Git Bash / CI Linux**（仓库已配 Bash 工具）；PowerShell 执行者改用等价命令（`grep`→`Select-String`、`tail`→`Select-Object -Last`、`test -f`→`Test-Path`、避免 `<(...)`）。
5. **分支前缀中性化**：建议分支用 `quality/...`（执行者无关）；若按编排约定区分执行者，Claude Code 用 `claude/...`、CodeX 用 `codex/...`。

## 状态图例

`☐ 待开始` ｜ `◐ 进行中` ｜ `✅ 已完成` ｜ `⏸ 阻塞`

## 任务单元清单

### Wave A — 本周（高 ROI / 低风险，先做）

| 单元 | 文档 | 状态 | 关联 ID | 工时 | 可并行 | 建议分支 |
|---|---|---|---|---|---|---|
| TU-01 | [止血四修](TU-01-hotfix-stabilize.md) | ✅ | H2 H3 H4ᵐⁱⁿ H5 | S | 是 | [PR #40](https://github.com/sun9bear/AITransVideo/pull/40) 已合并 |
| TU-02 | [部署构建卫生](TU-02-build-hygiene.md) | ✅ | DEP-02/04/05/06/07 | S(A)·需确认(B) | 是 | [PR #52](https://github.com/sun9bear/AITransVideo/pull/52)(A) + [#55](https://github.com/sun9bear/AITransVideo/pull/55)(B) 已合并 |
| TU-03 | [质量护栏脚手架](TU-03-quality-scaffold.md) | ✅ | TOOL-01/03/04 TEST-* | S–M | 部分 | [PR #41](https://github.com/sun9bear/AITransVideo/pull/41) 已合并 |

### Wave B — 低风险标准化（Phase 1）

| 单元 | 文档 | 状态 | 关联 ID | 工时 | 可并行 | 建议分支 |
|---|---|---|---|---|---|---|
| TU-04 | 统一 JSON 原子写 helper | ✅ | DRY-02（含 H4 完整版） | M | 否（依 TU-01） | [PR #42](https://github.com/sun9bear/AITransVideo/pull/42) 已合并 |
| TU-05 | 统一 admin 鉴权依赖 | ✅ | DRY-01 | M | 是 | [PR #43](https://github.com/sun9bear/AITransVideo/pull/43) 已合并 |
| TU-06 | coerce/normalize + 统一 error payload | ✅ | DRY-03/04/06 | M | 是 | [PR #44](https://github.com/sun9bear/AITransVideo/pull/44) 已合并 |
| TU-07 | 类型契约硬化 + mypy 窄域 | ✅ | TS-01/02/05/07/10 | M | 否（依 TU-03） | [PR #47](https://github.com/sun9bear/AITransVideo/pull/47) 已合并 |
| TU-08 | 计费&付费路径结构化日志 | ✅ | EH-001/002/008/011 | M | 是 | [PR #51](https://github.com/sun9bear/AITransVideo/pull/51) 已合并 |

### Wave C — 热点深挖（Phase 2）

| 单元 | 文档 | 状态 | 关联 ID | 工时 | 可并行 | 建议分支 |
|---|---|---|---|---|---|---|
| TU-09 | `job_intercept.py` route family 拆分 | ☐ | STRUCT-02 | L | 否 | `quality/intercept-split` |
| TU-10 | 前端编辑页 route shell 化 | ☐ | FE-001/002 | L | 是 | `quality/edit-page-shell` |
| TU-11 | 前端语音选择共享模块 | ☐ | FE-004/009 TS-08 | L | 是 | `quality/voice-select-shared` |
| TU-12 | `jobs/api.py` dispatch table 化 | ☐ | STRUCT-05 | M | 否 | `quality/jobsapi-dispatch` |
| TU-13 | JobService post-edit 模块抽取 | ☐ | STRUCT-07 | M | 否 | `quality/jobservice-postedit` |

### Wave D — 收敛 + 性能（Phase 3）

| 单元 | 文档 | 状态 | 关联 ID | 工时 | 可并行 | 建议分支 |
|---|---|---|---|---|---|---|
| TU-14 | **process.py Option B 输出收敛第一刀** | ☐ | STRUCT-01 PRIOR-17 | L | 否 | `quality/process-converge-1` |
| TU-15 | 性能有界优化 | ☐ | PERF-* ASYNC-01/02/03 | M | 是 | `quality/perf-bounded` |
| TU-16 | DB 卫生 | ☐ | DB-001..010 | M | 是 | `quality/db-hygiene` |
| TU-17 | logs/events cursor 化 + benchmark harness | ☐ | §6.3/§6.6 | M | 是 | `quality/events-benchmark` |

### Wave E — 中长期（Phase 4，决策门）

| 单元 | 文档 | 状态 | 关联 ID | 工时 | 可并行 | 建议分支 |
|---|---|---|---|---|---|---|
| TU-18 | 治理决策门（Job API→FastAPI / JSON→DB / OpenAPI→TS / 全仓阻断） | ☐ | §9 Phase4 | 决策 | — | （仅决策） |

## 文档直达

- [TU-01 止血四修](TU-01-hotfix-stabilize.md) · [TU-02 部署构建卫生](TU-02-build-hygiene.md) · [TU-03 质量护栏脚手架](TU-03-quality-scaffold.md)
- [TU-04 原子写统一](TU-04-atomic-write.md) · [TU-05 admin 鉴权统一](TU-05-admin-auth-dep.md) · [TU-06 shared helpers](TU-06-shared-helpers.md) · [TU-07 类型契约](TU-07-type-contracts.md) · [TU-08 计费日志](TU-08-billing-logging.md)
- [TU-09 intercept 拆分](TU-09-intercept-split.md) · [TU-10 编辑页 shell](TU-10-edit-page-shell.md) · [TU-11 语音选择共享](TU-11-voice-select-shared.md) · [TU-12 jobs/api dispatch](TU-12-jobsapi-dispatch.md) · [TU-13 JobService post-edit](TU-13-jobservice-postedit.md)
- [TU-14 process Option B 收敛](TU-14-process-converge-1.md) · [TU-15 性能有界优化](TU-15-perf-bounded.md) · [TU-16 DB 卫生](TU-16-db-hygiene.md) · [TU-17 events/benchmark](TU-17-events-benchmark.md)
- [TU-18 治理决策门](TU-18-governance-gate.md)

## 实施 LOG

| 日期 | 单元 | PR | 审查 | 结果 |
|---|---|---|---|---|
| 2026-06-25 | TU-01 止血四修 | [#40](https://github.com/sun9bear/AITransVideo/pull/40) squash | 对抗式多 lens（抓出第 5 个 `en_text` 站点 + de-flake 预存 40% flaky 测试）→ CodeX CLI ×3（P2 免费档误报→P3 aligner 测试→clean）→ @codex bot 无问题 → CI 3/3 | ✅ 合并 main。deferral：credits_service 3 警告→TU-08；billing `logger.info` 误置→独立观察项 |
| 2026-06-25 | TU-03 质量护栏脚手架 | [#41](https://github.com/sun9bear/AITransVideo/pull/41) squash (`dc12c071`) | 多 lens 对抗 Workflow（2×P2 删除文件误阻断 / addopts 漏 §10.4 -m + 1×P3）→ CodeX CLI ×3（r1 2P2+1dup → r2 2P2 FETCH_HEAD/只阻断新增 → r3 clean）→ @codex bot「no major issues」→ CI 5/5 blocking 绿 | ✅ 合并 main。**关键设计**：ruff 仅阻断**新增** .py（改动既有+全仓 report-only），file-size-guard 读 **base ref** 基线防同 PR grow+bump 绕过，asyncio_mode=auto 实证 collection 8687 不变。**待办**：backend-full-suite（continue-on-error 非阻断，271 预存测试债+ffprobe 环境缺失，非回归）后续可选挪 nightly / 装 ffmpeg / 升硬门；mypy 9 窄域债→TU-07 |
| 2026-06-25 | TU-04 统一 JSON 原子写 | [#42](https://github.com/sun9bear/AITransVideo/pull/42) squash (`3f8508f3`) | 多 lens Workflow 0 real + CodeX CLI clean + @codex bot「no major issues」+ set-diff 实证 0 回归（失败集与 main 完全一致，37→复跑 36=flaky）+ CI 5/5 blocking 绿 | ✅ 合并 main。canonical helper 升级（str\|Path/Any/fsync/sort_keys/trailing_newline）收口 **7 处**（spec 6 + 发现 config_loader 第7）；保字节等价（review_actions 纠正 spec 遗漏的 sort_keys=False、store 保 fsync=False group-commit、draft 保 DraftError 红线）。net −169 行。**deferral**：另 4 处命名不同内联原子写（editing_speakers._write_speakers / editing_split_suggest._write_usage / video_render_async._write_status_atomic / speaker_evidence inline）→ 后续 DRY 微单元 |
| 2026-06-25 | TU-05 统一 admin 鉴权依赖 | [#43](https://github.com/sun9bear/AITransVideo/pull/43) squash (`592dc226`) | 多 lens Workflow 0 real + CodeX CLI「no introduced correctness issues」+ @codex bot「no major issues」+ 8696 收集 0 collection error / 341 admin + 105 cosyvoice 测试绿 + CI 5/5 blocking 绿 | ✅ 合并 main。13 文件 `_require_admin`/`_is_admin` 副本收口到单一 `gateway/admin_auth.py`（消除返回 None vs User、role 判断带不带 `or "user"` 兜底两类分叉）。**完整性 lens 抓真 bug**：首轮顶层 grep 漏扫子目录→`cosyvoice_clone/api.py:80` 仍 import 被删的 `_is_admin`，被 test_admin_cosyvoice_control 4 连挂暴露并修，递归复核确认唯一遗漏。gate-coverage 纳入 admin_support_api.py（baseline 51→80）。pan/ 独立认证上下文显式例外保留。net −110 行。CI fix：ruff format 新增文件 + 删 cosyvoice pre-existing 未用 import 抵消 file-size ratchet +1 |
| 2026-06-25 | TU-06 coerce/normalize + 统一 error payload | [#44](https://github.com/sun9bear/AITransVideo/pull/44) squash (`0e81e21a`) | 对抗式多 lens Workflow（仅 2×LOG isort，0 等价性/红线）+ CodeX CLI（1×P2：json_helpers dataclass 分支不递归→嵌套 Path 崩 TypeError）+ @codex bot **三轮 4×P2**（detail 口径不一致 / insufficient_credits 真码 / 占位码 / retryable 恒 false）全收敛 + set-diff 实证 0 回归（transcriber 7 / voice-match 13 / intercept 1 失败集与 main 逐字一致、isolation 证伪 male_elderly+volcengine 污染、新文件 0 失败）+ 8755 收集 0 collection error + CI 5/5 blocking 绿 | ✅ 合并 main。DRY-01/02/03/04/06 微工具收口：`src/utils/coerce.py`（normalize/coerce_*）/ `src/utils/json_helpers.py`（**递归修复 dataclass 嵌套 Path 序列化**）/ cosyvoice age-bucket→`voice_reranker.resolve_age_bucket` / 三 selector rerank 提取→`voice_reranker.unpack_rerank_result`；`_normalize_optional_text` 副本 18→14。error_payload：`ErrorPayload` 形状 + gateway `error`/`error_code` 双写（前端按 `'detail' in payload` 分支→可选字段默认即省略，不发误导性 false/{}/""）。**按 YAGNI 删除投机的错误码注册表**：bot 三轮反复戳无消费方的 RETRYABLE/NON_RETRYABLE_CODES + 从未接线的 retryable，故从根删除而非逐个补码/接线（runtime source of truth=`gateway/job_intercept.py`，瞬时错误显式接线属 anchor 推迟项）。CI fix：coerce.py 补 ruff(0.15.19) format 后空行（Step1 漏跑 format --check）+ `_error_response` 收紧行数（双写扩写撞 file-size 白名单基线 6936、guard 读 base 基线不可自助抬→压到 6935）。**教训**：新增文件必跑 `ruff format --check` 非仅 check；whitelisted 大文件改动须预算行数 |
| 2026-06-25 | TU-07 类型契约硬化 + mypy 窄域 | [#47](https://github.com/sun9bear/AITransVideo/pull/47) squash (`436a0d82`) | 对抗式多 lens Workflow（5 lens 逐发现验证，7 findings→2 confirmed-real 均 LOW；byte-equivalence/type-contract/paid-api lens **0 real**）+ CodeX CLI「did not find a concrete regression」+ @codex bot **1×P2**（requires_worker source-pin 失配）已修 + **全量套件 set-diff 证 0 回归**（branch 314 fail / main 315 fail，唯一 branch-only `test_preview_rejects_unknown_job` 隔离下 branch+main 均 passed=flaky；main-only `projects/`/`next_config` 同类 flaky）+ 0 mypy 引入（base 13==now 13 逐行一致）+ 8783 collect 0 error + 5 阻断 CI 全绿 | ✅ 合并 main。getattr 架空清理 aligner 9 + tts_generator 60 **全归零**（纯机械字节等价，**不加** spec 表的 `or None`，slots dataclass default 是死代码）；`compute_job_policy -> JobPolicy` TypedDict（放**独立** `gateway/job_policy_types.py` 避撑破 job_intercept 基线 6936）；`job_record: Any`→`dict\|JobRecordLike\|None` Protocol ×5（`_read_job_field` 返回保留 `-> Any` 避 warn_return_any）；mypy 窄域 override 仅 tts_generator+aligner（job_intercept 延后 TU-09），13 处预存 type 债记后续工作**不 type:ignore**；`target_language` 现已声明字段→转直接访问（CodeX「若未声明则保留」前提失效）。新增 `TestNoGetattrSegmentRegression` 源码守卫。**两个教训**：① refactor 改源码文本必查 **source-pin guard**——原 `-k` 过窄漏跑 `test_anon_clone requires_worker` pin，靠 @codex bot 抓到→改用**全量 set-diff**而非窄 `-k`；② whitelisted 文件改动须预算**所有**相关文件基线——漏算 tts_generator 自身基线 1921，Protocol 撑到 1924 撞 file-size-guard（修：docstring 8→1 行压回 1917）。process.py ~90 getattr 移交 **TU-14**（spec 表 + TU-14 关联发现已显式记录，避免指标静默丢弃）|
| 2026-06-26 | TU-08 计费&付费路径结构化日志 | [#51](https://github.com/sun9bear/AITransVideo/pull/51) squash (`8a016072`) | CodeX CLI **1×P2**（INFO 审计日志经 main.py subprocess 无 logging 配置→root=WARNING 丢弃运行期可见）+ @codex bot **1×P2**（修复用的 root `basicConfig(INFO)` 致其他模块 INFO 日志含 cosyvoice TTS 文本=用户内容写入 job 日志 PII 泄漏）+ **全量套件 set-diff 证 0 回归**（收敛后 branch-only **0**；branch 315 / main 316 fail，main-only `review_state` flaky）+ 5 阻断 CI 全绿（file-size-guard pass）+ 契约 13 passed + 34 logger %-arg 自查全匹配 | ✅ 合并 main。print→logger 迁移审计盲区：tts 38→29 / translator 14→7 / gemini-transcriber 4→0 / assemblyai 13→9；process 11 处计费盲区（[smart][MONEY] reservation/register-billed 不一致、metering 上报）。新增 `tests/test_billing_logging_contracts.py` 13 源码守卫。**紧凑式 logger**（event 名 + %-args，无 verbose extra dict）守 file-size 基线（whitelisted: tts 1921/translator 3090/process 13341/assemblyai 977/main.py 1868 全 0~低余量；多行 [MONEY] print 压缩反省行）。**两 P2 权衡决策**：撤销 basicConfig——财务**关键**审计事件（smart_billing_inconsistency/register_billed_failed=ERROR、metering_skip/report_failed/retry/fallback=WARNING）经 logging **lastResort** 输出 stderr 本就可见无需 root 配置；审计价值在**失败/异常事件**（已可见）非 INFO 成功确认（job_metering_reported 成功态丢弃无碍，其失败态 WARNING 可见）；INFO 成功事件运行期可见性留独立 **PII-aware 应用级 logging 配置**基础设施任务。**关键教训**：①print→logger 系统性破坏断言 print 输出（capsys）的既有 observability 测试——窄 `-k` 漏，**必须全量 set-diff**（捕获 6 真：provider/sidecar/retry/money/runtime-evidence）；收敛守 spec「不改业务断言、可改日志字符串断言」：非契约 evidence 行 revert 回 print（且确认 ProcessJobRunner 捕获存 job-event）、契约事件保 logger 改 caplog/源码 pin。②生产 logging **必须配置才看得到 INFO**，但 root INFO 会连带泄漏用户内容——logging 配置是独立 PII-aware infra 关切，不塞进窄重构。process.py 全量 print 迁移留 **TU-14**。**Wave B 全 8 单元完成（TU-01/03/04/05/06/07/08 + TU-02 见 Wave A）**|
| 2026-06-27 | TU-02 PR-A 构建卫生（Step 1/2/5）| [#52](https://github.com/sun9bear/AITransVideo/pull/52) squash (`814769e6`) | 主模型终审 8 lens（额外抓 **gateway/app 部署 skew footgun** 并补 CLAUDE.md）+ CodeX CLI「no actionable regressions」（实跑 `pip install --dry-run .` 确认 pyJianYingDraft 解析 0.2.7）+ @codex bot **1×P2**（uv.lock 漏 dep）已修（`uv lock` 纯新增 82 行 0 既有变动）+ CI 5 阻断绿 | ✅ 合并 main。删 app 服务 3 个开发期 bind-mount（切镜像不可变）+ 删 Dockerfile Deno + pyJianYingDraft→pyproject + uv.lock 同步 + CLAUDE.md 部署节同步。**前置阻塞处理**：发现 main 预存 file-size-guard 红——PayPal 把 `gateway/billing.py` 撑到 1996 超基线 1617 却没 bump，且 PayPal 直推 main 未过 PR CI。独立 [#54](https://github.com/sun9bear/AITransVideo/pull/54)(`e44999d3`) bump 基线 1617→1996 先合（**file-size-guard 红 by-design**：anti-self-bump 永远比对 base 基线，任何基线 bump PR 自身必红，合进 main 才生效；main 无分支保护故不挡合并）→ PR-A rebase 转绿。**部署影响**：app 不再随 `docker restart` 热更，须 `up -d --build app` + 重启 gateway（同源主机 app/src 避 skew）|
| 2026-06-27 | TU-02 PR-B 生产配置（Step 3/4）| [#55](https://github.com/sun9bear/AITransVideo/pull/55) squash | 主模型终审（**codex 双路均限额→主模型唯一复核**）：Step3 digest 64-hex 合法 + `grep cloudflared:latest`=0；Step4 `{compose ${VAR}}−{.env.example}`=**0 缺口** + 无任何真实 secret + 无重复 + 分类正确 + CI 5 阻断绿 | ✅ 合并 main。cloudflared `:latest`→`@sha256:6b599ca3…`（项目主确认生产在用 digest）+ `.env.example` 补 **53** 个 docker-compose `${VAR}` 插值变量。**口径修正**：只补 compose `${...}` 插值变量（= 根 `.env.example` 真正职责），**不补**仅经 `env_file`(/opt/aivideotrans/config/.env)+`os.environ` 读取的运行期变量（否则污染根模板）；gap 取 `{compose ${VAR}}−{.env.example}`=53，**非** `{代码 os.environ refs}`（后者含 config-dir 变量会高估 89）。**TU-02 整单元完成（PR-A+PR-B），Wave A 收口；下一步 Wave C（项目主拍板）**|

## 依赖关系（DAG）

```
TU-01 ──→ TU-04 (原子写完整版基于止血的 fsync)
TU-03 ──→ TU-07 (mypy 窄域门先就位，类型硬化才有 CI 反馈)
TU-03 ──→ 所有后续 (护栏先于重构，重构才有回归网)
TU-09 / TU-12 / TU-13  顺序做（都改 Job API/Gateway 路由侧，避免同区并发冲突）
TU-14  独占 process.py（与 TU-15 的 process 内性能点若重叠，TU-14 先）
其余（TU-02 / TU-05 / TU-06 / TU-08 / TU-10 / TU-11 / TU-15 / TU-16 / TU-17）相互独立，可并行派发
```

**派发建议**：Wave A 三个可同时开三条分支；Wave B 的 TU-05/06/08 可并行，TU-04 等 TU-01 合并后开，TU-07 等 TU-03 合并后开。

## 统一文档模板（每个单元文档都遵循）

```
# TU-NN · <标题>
- 目标 / 价值 ；关联发现 ID ；前置依赖 ；建议分支
- 不在本单元范围（out-of-scope）
- 必守不变量（逐单元复述：付费 API 红线 / Option B / DSP-first / 剪映 draft 主目标 / Gateway 事实源 等，取相关项）
- 执行步骤：Step 0 确认现状 → Step 1..N（每步含：动作 + 文件:行 + 具体改法 + 该步验收标准[可机器验证命令]）
- 测试计划（新增/回归）
- 回滚方案（哪些文件、commit 边界）
- 完成定义 DoD（清单式，全部可勾选验证）
- 预估工时
```

## 全局必守红线（所有单元通用）

- **付费 API 硬约束**：MiniMax 付费克隆 / 付费 TTS / 付费 LLM / 付费 ASR 绝不在 fallback / except / retry / batch 路径自动触发；只走用户显式 consent。任何单元不得为「修 bug」而引入自动付费调用。
- **架构不变量**：SemanticBlock 为 TTS 单元；Alignment DSP-first；retiming 数学确定性不迁 LLM；剪映 draft 为主交付物；Gateway 是 plan/pricing/entitlement 唯一事实源；默认测试不接真实外部服务。
- **process.py 走 Option B**（[ADR](../../architecture/PROCESS_WORKFLOW_CONVERGENCE.md)）：退成兼容壳消费 `ProjectWorkflow`/`OutputDispatcher`，**不**另起独立架构。
- **git**：每单元独立 worktree + feature 分支；提交用显式 pathspec（`git commit -- <files>`），**禁止 `git add .`**（会误纳 `.codegraph/`、`.codex_worktrees/`）。
