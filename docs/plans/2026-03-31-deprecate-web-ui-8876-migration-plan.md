# Web UI API (8876) 废弃迁移执行计划

> **审核状态：** 待执行  
> **创建日期：** 2026-03-31  
> **最后更新：** 2026-03-31  
> **执行方式：** Claude 每次只执行一个 Phase；完成后必须停下，按本文模板回传结果，待人工审核通过后再进入下一阶段。

## 目标

将当前仍依赖 8876 的真实活跃能力迁移到 Job API 8877 或 Gateway 自有端点，最终移除 8876 作为运行时服务。

这次迁移的目标不是“把 8876 的全部历史端点原样搬走”，而是：

- 先覆盖 Next.js 当前真实活跃流量。
- 修正 `review-state` / `cancel` 的全局语义，改成显式 `jobId`。
- 把上传和全局声音库从“伪 job 子资源”中拆出来。
- 用尽量小、可测试、可回滚的改动完成 8876 下线。

## 已确认决策

- 首批只迁移真实活跃 surface，不默认迁移 8876 上所有历史端点。
- `review-state` 和 `cancel` 优先级最高，且必须改成显式 `jobId` 语义，禁止“自动发现 active job”。
- `voice library` 是全局资源，挂在 8877 进程上，但不挂在 `/jobs/{id}` 下面。
- `upload-video` 是创建 job 之前的动作，改成 Gateway-native 端点，默认目标路径为 `POST /gateway/upload-video`。
- `project-file` 不进入首批迁移范围；除非 Phase 0 找到真实活跃 consumer，否则不做 `/jobs/{id}/files/{path}`。
- `JobService` 保持 job 生命周期边界；审核、上传、声音库等逻辑优先放在窄的 helper / handler 中，不把 `JobService` 做成大杂烩。
- 每个 Phase 都以“冻结范围、做完即停、人工审核后再继续”为硬约束。

## 当前活跃 Surface 盘点（2026-03-31）

### 首批必须迁移

| Legacy surface | 当前调用方 | 结论 | 目标归属 | 目标路径 | Phase |
|---|---|---|---|---|---|
| `GET /api/state` | `frontend-next/src/app/workspace/[jobId]/page.tsx`、`frontend-next/src/lib/api/reviews.ts`、`frontend-next/src/lib/api/voiceLibrary.ts` | 活跃，但当前是全局语义，需拆成 job-scoped review state | Job API | `GET /job-api/jobs/{jobId}/review-state` | 1 |
| `POST /api/job/cancel` | `frontend-next/src/app/workspace/[jobId]/page.tsx`、`frontend-next/src/lib/api/reviews.ts` | 活跃，且当前语义有串 job 风险 | Job API | `POST /job-api/jobs/{jobId}/cancel` | 1 |
| `GET /api/result-download` | `frontend-next/src/lib/api/downloads.ts`、结果下载列表 | 活跃 | Job API | `GET /job-api/jobs/{jobId}/download/{key}` | 1 |
| `GET /api/tts-segments-zip` | `frontend-next/src/lib/api/downloads.ts` | 活跃 | Job API | `GET /job-api/jobs/{jobId}/tts-segments-zip` | 1 |
| 全局 voice library 读取（当前通过 `/api/state` 快照间接提供） | `frontend-next/src/app/translations/new/page.tsx`、`frontend-next/src/app/voices/page.tsx`、审核面板 | 活跃，应从 review-state 中拆出 | Job API | `GET /job-api/voice-library` | 1 |
| `POST /api/review/translation/approve` | `frontend-next/src/components/workspace/TranslationReviewPanel.tsx` | 活跃 | Job API | `POST /job-api/jobs/{jobId}/review/translation/approve` | 2 |
| `POST /api/review/split-segment` | `TranslationReviewPanel.tsx`；`SpeakerReviewPanel.tsx` 中也有遗留调用 | 活跃 | Job API | `POST /job-api/jobs/{jobId}/review/split-segment` | 2 |
| `POST /api/review/preview-segment` | `TranslationReviewPanel.tsx` 直接 `fetch()` | 活跃 | Job API | `POST /job-api/jobs/{jobId}/review/preview-segment` | 2 |
| `POST /api/review/voice/clone` | `TranslationReviewPanel.tsx`；`VoiceReviewPanel.tsx` 中也有遗留调用 | 活跃 | Job API | `POST /job-api/jobs/{jobId}/review/voice/clone` | 2 |
| `POST /api/voice-library/set-default` | `frontend-next/src/lib/api/reviews.ts` export，但 **无前端组件 import**（Phase 0 确认为死代码） | 遗留兼容 | Job API | `POST /job-api/voice-library/set-default` | 暂不迁移 |
| `POST /api/voice-library/register-manual` | `frontend-next/src/lib/api/reviews.ts` export，但 **无前端组件 import**（Phase 0 确认为死代码） | 遗留兼容 | Job API | `POST /job-api/voice-library/register-manual` | 暂不迁移 |
| `POST /api/upload-video` | `frontend-next/src/app/translations/new/page.tsx` 当前走 `/web-ui-api/api/upload-video` | 活跃，但发生在 job 创建前 | Gateway | `POST /gateway/upload-video` | 2 |

### 暂不首批迁移

| Legacy surface | 当前证据 | 结论 | 处理方式 |
|---|---|---|---|
| `POST /api/review/speaker/approve` | 当前 `/reviews/[jobId]/speaker` 页面已跳回 workspace | 暂无真实活跃入口 | Phase 4 决定删除遗留调用或单独立项，不首批迁移 |
| `POST /api/review/translation-config/approve` | 当前 `/reviews/[jobId]/translation-config` 页面已跳回 workspace | 暂无真实活跃入口 | 同上 |
| `POST /api/review/voice/approve` | 当前 `/reviews/[jobId]/voice` 页面已跳回 workspace | 暂无真实活跃入口 | 同上 |
| `POST /api/review/voice/preview` | 代码里有 helper 和旧组件调用，但不在当前主流程 | 兼容面，非首批 | 仅当 Phase 0 发现真实活跃调用才纳入后续 |
| `GET /api/project-file` | 在 `frontend-next/src` 中未发现真实活跃 consumer | 高风险低收益 | 默认不做，除非 Phase 0 发现遗漏调用 |
| `POST /api/review/*/save`、`POST /api/stop`、`POST /api/settings`、`GET /` | 已废弃或已由其他服务接管 | 不迁移 | 保持废弃状态 |

## 执行总规则

### 阶段边界

- Claude 一次只允许执行一个 Phase。
- 未经人工明确批准，不得提前做下一阶段内容。
- 每个 Phase 只能触碰该阶段列出的能力和必要测试，不得顺手扩展 API surface。
- 若发现文档未覆盖的真实活跃调用，允许补充盘点，但不得直接实现未审批的新范围。

### 代码边界

- 允许把 8876 中可复用逻辑提取到共享 helper，但不要为“未来可能的接口”预建抽象。
- `JobService` 只保留 job 生命周期、结果和状态读取职责；审核/声音库/上传逻辑应优先放在窄 helper 或 handler 层。
- 新的 job 子资源端点必须显式要求 `jobId`，不能再走“推断当前 active job”。
- 不引入 `/jobs/{id}/files/{path}` 这一类通用文件读取面，除非后续人工明确批准。

### 测试门槛

- 不以“全仓测试全部通过”作为阶段门槛，因为仓库中存在与本迁移无关的预存测试债。
- 每个 Phase 的门槛是两层：
  - 冻结的预存失败列表不得增加。
  - 本阶段触达的测试集必须通过。
- Claude 回传结果时，必须写出“命令 + 结果”，不能只写“测试已通过”。

### 兼容策略

- 在 Phase 1-2 期间，允许 8876 与新端点并存，但仅作为过渡。
- 在 Phase 3 完成并验收前，不删除仍被活跃前端路径使用的旧代理。
- 在 Phase 4 结束时，8876 必须从运行时配置、启动脚本、Gateway 配置和部署入口中一起移除。

### 阶段指令维护方式

- 发给 Claude 的执行指令不固化在本计划正文中。
- 每一轮只为当前要执行的 Phase 单独生成指令文档或对话消息。
- 指令必须引用本计划中的阶段边界、验收标准和审核回传模板，但允许根据上一轮执行结果动态调整。

## 审核回传模板

Claude 每个 Phase 结束后，必须严格按下面格式回传：

```md
## Phase X Completion Report

### 1. Scope Completed
- ...

### 2. Files Changed
- `path/to/file`: 一句话说明改动目的

### 3. API / Runtime Surface Changed
- Added:
  - `METHOD /path` - purpose
- Updated:
  - `METHOD /path` - purpose
- Removed or Deferred:
  - ...

### 4. Verification
- `command`
  - result
- `command`
  - result

### 5. Explicitly Deferred
- ...

### 6. Risks / Open Questions
- ...

### 7. Ready For Review
- `READY_FOR_REVIEW`
- Suggested next phase: `Phase N`
```

若阶段被阻塞，则改用：

```md
## Phase X Blocked Report

### Blocker
- ...

### Evidence
- `path/to/file`
- `command`
  - result

### Options
1. ...
2. ...

### Recommendation
- ...
```

## Phase 0: 活跃 Surface 盘点与基线清理

**目标：** 冻结本次迁移的真实活跃范围，并清理直接阻塞迁移的测试基线问题。

### In Scope

- 核对本文的活跃 surface 盘点表，必要时修正“活跃 / 遗留兼容 / 不迁移”的分类。
- 清理当前直接阻塞迁移的测试债，至少覆盖：
  - `tests/test_main_cli.py` 中仍引用 `_shutdown_cli_tts_runtimes` 的过时断言。
  - `tests/test_job_api.py::test_job_api_continue_reuses_existing_review_semantics` 的 flaky 问题，若无法在本阶段彻底修复，则要给出稳定复现条件和冻结说明。
- 在本文中写明“冻结的预存失败列表”与“后续阶段统一使用的验证命令”。

### Out Of Scope

- 不新增任何 8877 / Gateway 业务端点。
- 不修改前端调用路径。
- 不开始清理 8876 runtime wiring。

### 可能涉及文件

- `docs/plans/2026-03-31-deprecate-web-ui-8876-migration-plan.md`
- `tests/test_main_cli.py`
- `tests/test_job_api.py`
- 为修复测试而必须触达的最小实现文件

### 验收标准

- 活跃 surface 盘点表与阶段范围收敛完成。
- 迁移相关测试基线清楚可复用。
- Phase 0 不引入新的 API surface。

### 审核重点

- 是否真的把”必须迁”和”遗留兼容”分开了。
- 是否只修了基线，不偷跑功能改造。
- 是否把后续阶段的验证口径写清楚了。

### Phase 0 执行结果 (2026-03-31)

#### 活跃 surface 盘点修正

通过代码证据核实，对原计划盘点表做了以下修正：

1. **`SpeakerReviewPanel`、`VoiceReviewPanel`、`TranslationConfigPanel`** — 组件代码存在且内部调用了 8876 端点，但 **workspace 页面不渲染这三个组件**。workspace 在 `speaker_review` / `voice_review` / `translation_config_review` 阶段只显示”正在自动处理”spinner（`workspace/[jobId]/page.tsx` L217-228）。这三个组件仅在 `components/workspace/index.ts` 中 export，无其他页面 import —— **确认为死代码**。

2. **`bindVoiceReviewDefault` (`/api/voice-library/set-default`)** 和 **`registerVoiceReviewManual` (`/api/voice-library/register-manual`)** — `reviews.ts` 中 export 但 **无任何前端组件 import 或调用** —— **确认为死代码**。原计划盘点表中标记为”活跃”需修正为”遗留兼容”。

3. **`POST /api/review/speaker/approve`** — 虽标注为”暂无活跃入口”，但 `SpeakerReviewPanel.tsx` L65 确实调用了 `approveSpeakerReview`。然而该组件是死代码（见第 1 点），结论不变。

#### 冻结的预存失败列表

迁移相关测试集 **Phase 0 后基线为 136 passed, 0 failed**。

仓库中已知的预存失败（与本迁移无关，不纳入阶段门槛）：
- `tests/test_process_pipeline.py` — **10 failed, 63 passed**（pipeline 集成测试，涉及审核流程语义变更和声音克隆 mock 不匹配，与 8876 迁移无关）

#### 后续阶段统一验证命令

```bash
# 迁移相关测试集（Phase 0 基线：136 passed, 0 failed）
# 使用项目 Python 环境，Windows 上需要完整路径
python -m pytest tests/test_main_cli.py tests/test_job_api.py tests/test_web_ui.py tests/test_gateway_proxy.py tests/test_gateway_job_policy.py tests/test_gateway_quota.py tests/test_gateway_entitlements.py tests/test_gateway_create_job.py -v --tb=short

# 每个 Phase 的门槛：
# 1. 上述命令的 passed 数 >= 136（允许因新增测试而增加）
# 2. failed 数 = 0（不允许新增失败）
# 3. 本阶段新增的测试文件单独跑一次确认通过
```

#### test_job_api flaky 说明

`test_job_api_continue_reuses_existing_review_semantics` 在隔离运行和批量运行中均 **5/5 稳定通过**。Codex 报告的 flaky 现象在当前环境无法复现，可能是资源竞争导致的偶发问题。冻结决策：当前标记为稳定，后续阶段若复现再单独立项排查。

## Phase 1: 核心 Job-Scoped 读面与取消语义

**目标：** 先把最危险的“全局当前 job”语义改掉，并完成活跃读面迁移。

### In Scope

- 抽取或复用最小共享 helper，支撑 8876 与 8877 过渡期并存。
- 在 Job API 上实现：
  - `GET /job-api/jobs/{jobId}/review-state`
  - `POST /job-api/jobs/{jobId}/cancel`
  - `GET /job-api/jobs/{jobId}/download/{key}`
  - `GET /job-api/jobs/{jobId}/tts-segments-zip`
  - `GET /job-api/voice-library`
- 明确保证：新 `review-state` / `cancel` 只针对显式 `jobId`，不能自动发现 active job。
- 若 8876 仍需暂时存在，允许它改为复用共享 helper，但不扩大旧端点能力。

### Out Of Scope

- 不做 `project-file`。
- 不做 `upload-video`。
- 不做审核写操作（approve / split / preview / clone）。
- 不删除前端中的 8876 调用。

### 可能涉及文件

- `src/services/web_ui/handler.py`
- `src/services/web_ui/snapshot.py`
- `src/services/web_ui/project_resolver.py`
- `src/services/web_ui/voice_library.py`
- `src/services/jobs/api.py`
- `src/services/jobs/service.py`
- `src/services/shared/*`（如确有必要）
- `tests/test_web_ui.py`
- `tests/test_job_api.py`
- 新增的 job-api review/download 测试文件

### 建议验证命令

- `python -m pytest tests/test_web_ui.py tests/test_job_api.py -q`
- 仅针对本阶段新增测试文件再跑一次精确用例

### 验收标准

- 新 `review-state` / `cancel` 使用显式 `jobId`，不再走全局 active job 发现逻辑。
- 下载面可覆盖当前 key-based 下载需求。
- voice library 读取已从 `/api/state` 快照耦合中拆出来。
- 没有引入 `GET /jobs/{id}/files/{path}`。

### 审核重点

- 是否真的修掉了串 job 语义。
- 是否把全局声音库从 job 快照里拆开了。
- 是否忍住没有顺手做通用文件读取面。

## Phase 2: 活跃写面迁移与 Gateway 上传端点

**目标：** 迁移当前真实活跃的审核写操作，并把上传从 8876 移到 Gateway 自有端点。

### In Scope

- 在 Job API 上实现：
  - `POST /job-api/jobs/{jobId}/review/translation/approve`
  - `POST /job-api/jobs/{jobId}/review/split-segment`
  - `POST /job-api/jobs/{jobId}/review/preview-segment`
  - `POST /job-api/jobs/{jobId}/review/voice/clone`
- **不包含** `voice-library/set-default` 和 `voice-library/register-manual`（Phase 0 确认为死代码，留到 Phase 4 决定删除遗留调用或在需求复活时单独立项）。
- 仅当 Phase 0 证明 `voice/preview` 有真实活跃调用时，才可将其并入本阶段；否则继续后置。
- 在 Gateway 上实现：
  - `POST /gateway/upload-video`
- Gateway 上传端点负责：
  - 接收 multipart 请求
  - 使用当前认证身份做用户隔离
  - 落到用户作用域的上传目录
  - 返回与现有前端尽可能兼容的上传结果

### Out Of Scope

- 不做 `speaker/translation-config/voice approve` 这类当前非活跃审核面。
- 不删除 8876 代理。
- 不做运行脚本和部署清理。

### 可能涉及文件

- `src/services/web_ui/translation_review.py`
- `src/services/web_ui/review_state_helpers.py`
- `src/services/web_ui/segment_loader.py`
- `src/services/web_ui/voice_library.py`
- `src/services/jobs/api.py`
- `src/services/jobs/service.py`（仅在确有生命周期边界需求时最小改动）
- `gateway/main.py`
- `gateway/job_intercept.py`（如上传需要共用鉴权/身份逻辑）
- `tests/test_job_api.py`
- `tests/test_gateway_proxy.py`
- 新增的 review / upload 测试文件

### 建议验证命令

- `python -m pytest tests/test_job_api.py tests/test_gateway_proxy.py -q`
- 仅针对本阶段新增测试文件再跑一次精确用例

### 验收标准

- 当前活跃审核写面可以在不依赖 8876 的情况下由 8877 / Gateway 提供。
- 上传不再依赖 `/web-ui-api/*` 注入链，改由 Gateway 原生身份上下文处理。
- `JobService` 没有因为这批端点而失去职责边界。

### 审核重点

- 上传是否真的留在 Gateway，而不是绕回来要求 8877 补 `x-user-id` 注入。
- 是否只实现了活跃写面，没有把 dormant review API 一起搬过去。
- `JobService` 是否仍然聚焦。

## Phase 3: 前端切换与 Gateway 归口

**目标：** 让当前 Next.js 活跃路径完全切到新 surface，并把 Gateway 的 ownership / proxy 路径收敛到新归属。

### In Scope

- 修改前端活跃调用路径，使其不再依赖 8876：
  - `frontend-next/src/lib/api/reviews.ts`
  - `frontend-next/src/lib/api/voiceLibrary.ts`
  - `frontend-next/src/lib/api/downloads.ts`
  - `frontend-next/src/app/translations/new/page.tsx`
  - `frontend-next/src/components/workspace/TranslationReviewPanel.tsx`
  - 其他被活跃路径直接调用的相关文件
- 所有活跃审核函数必须显式传入 `jobId`。
- 上传改走 `POST /gateway/upload-video`。
- Gateway 对新的 `/job-api/jobs/{jobId}/review/*`、下载类子资源做 ownership / 代理收敛。
- 删除活跃前端路径中对 `webUiApiClient` 的依赖。

### Out Of Scope

- 不移除 8876 的启动脚本和运行时配置。
- 不处理 dormant API 的最终命运。
- 不删除整个 `src/services/web_ui/` 目录。

### 可能涉及文件

- `frontend-next/src/lib/api/reviews.ts`
- `frontend-next/src/lib/api/voiceLibrary.ts`
- `frontend-next/src/lib/api/downloads.ts`
- `frontend-next/src/lib/api/client.ts`
- `frontend-next/src/app/translations/new/page.tsx`
- `frontend-next/src/app/workspace/[jobId]/page.tsx`
- `frontend-next/src/components/workspace/TranslationReviewPanel.tsx`
- `gateway/main.py`
- `gateway/job_intercept.py`
- `gateway/admin_settings.py`
- `gateway/config.py`
- `tests/test_gateway_proxy.py`
- 相关前端测试或构建配置

### 建议验证命令

- `python -m pytest tests/test_gateway_proxy.py tests/test_job_api.py -q`
- `npm --prefix frontend-next run build`

### 验收标准

- 当前活跃 Next.js 路径不再触发任何 8876 调用。
- 活跃调用都改成显式 `jobId` 契约。
- Gateway 的 active proxy / intercept 指向新归属。
- `webUiApiClient` 不再服务于活跃前端路径。

### 审核重点

- 前端是否还有隐藏的 8876 活跃调用。
- 是否真的把 `jobId` 变成显式参数，而不是继续靠 `project_dir` 隐式推断。
- Gateway ownership 逻辑是否覆盖了新 review 子资源。

## Phase 4: 运行时清理、遗留面决策与 8876 下线

**目标：** 从运行时和部署层面真正移除 8876，并处理遗留非活跃 surface 的去留。

### In Scope

- 清理 8876 运行时 wiring：
  - `docker-compose.yml`
  - `scripts/linux_app_service.sh`
  - `scripts/run_remote_workbench_service.py`
  - `src/services/remote_workbench_runtime.py`
  - `src/services/public_entry_caddy.py`
  - `gateway/config.py`
  - `gateway/admin_settings.py`
  - 其他仍把 `web_ui` 视作独立服务的入口
- 删除 `AVT_WEB_UI_UPSTREAM`、`web_ui_upstream`、`WEB_UI_API_BASE` 等仍指向 8876 的运行时配置。
- 对 dormant API 做最终决策，默认顺序为：
  - 先删除活跃代码路径中已经不需要的遗留 helper / route / import
  - 不主动补迁 dormant backend endpoint
  - 若发现仍有真实使用场景，则单独立 follow-up plan，不挤进本次下线收尾
- 更新架构与部署文档。

### Out Of Scope

- 不扩展新的业务能力。
- 不做与 8876 下线无关的前端重构。

### 可能涉及文件

- `docker-compose.yml`
- `scripts/linux_app_service.sh`
- `scripts/run_remote_workbench_service.py`
- `src/services/remote_workbench_runtime.py`
- `src/services/public_entry_caddy.py`
- `gateway/config.py`
- `gateway/admin_settings.py`
- `main.py`
- `src/services/web_ui/*`（只做删除、去 wiring 或显式 deprecated 标记）
- `tests/test_remote_workbench_runtime.py`
- `tests/test_main_cli.py`
- 部署/架构文档

### 建议验证命令

- `python -m pytest tests/test_remote_workbench_runtime.py tests/test_main_cli.py tests/test_gateway_proxy.py -q`
- 如涉及前端活跃路径，再补跑 `npm --prefix frontend-next run build`

### 验收标准

- 8876 不再被启动、不再被代理、不再被部署配置引用。
- 活跃功能全部由 8877 / Gateway 承接。
- dormant surface 已被明确删除、保留为死代码待后续清理，或单独拆出后续计划；不能以“先留着再说”结束。

### 审核重点

- 是否真的把运行脚本、runtime config、public entry、compose 一起清掉了。
- 是否还残留任何 8876 运行时入口。
- 是否把 dormant surface 的命运说清楚了。

## 冻结的基线说明

截至 2026-03-31，本迁移相关的局部测试基线曾观察到以下问题，需要在 Phase 0 明确处理或冻结：

- `tests/test_main_cli.py` 中仍有对 `_shutdown_cli_tts_runtimes` 的过时断言。
- `tests/test_job_api.py::test_job_api_continue_reuses_existing_review_semantics` 曾出现过 flaky 现象，需要在 Phase 0 给出处理结果。

在这些问题被明确前，后续 Phase 不以“全仓无失败”为唯一门槛，而以“冻结失败不增加 + 本阶段触达测试通过”为准。
