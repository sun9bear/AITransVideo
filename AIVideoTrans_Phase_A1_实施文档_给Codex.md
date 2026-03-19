# AIVideoTrans Phase A1 实施文档（给 Codex）

## 1. 文档目的

本文件用于指导 AIVideoTrans 从当前“Phase 1 + controlled convergence 已基本到位”的仓库状态，进入下一阶段：

**Phase A1：建立最小 job-based service layer。**

本阶段的目标不是继续深收 `process`，也不是直接大做 Web UI 或 OpenClaw Skill，而是：

- 将现有最完整、最兼容的本地执行链路封装为统一任务入口
- 为未来 Web 页面、OpenClaw Skill、后续商业化入口提供同一后端协议
- 在不破坏现有 review / voice_review / output 闭环的前提下，建立最小可调用后端骨架

---

## 2. 当前仓库阶段判断

基于当前代码与文档，项目现状可总结为：

- `legacy process` 已通过 shared `OutputDispatcher` 输出结果
- canonical build 装配已更多收回 shared `ProjectBuilder`
- canonical source/artifact shape 已更多收回 `project_shape_helpers`
- `voice_review` 已是现有兼容闭环的一部分
- Web UI 已具备本地审校工作台属性，但其内部 `ProcessJobManager` 仍是 process-only、UI-embedded 的任务层原型
- 继续深收 `process` 的低风险高收益部分基本已完成；剩余多为 review gate、voice_review、TTS、alignment、runtime recovery 等高风险边界

因此，当前主目标应从：

- **继续以 `process convergence` 为唯一主线**

转为：

- **冻结当前 convergence 基线**
- **进入 Phase A1：最小 job service layer 落地**

---

## 3. Phase A1 的目标

Phase A1 的正式目标只有一个：

**将当前最完整的 process 兼容执行链路封装成统一的 job-based service layer。**

它要解决的问题是：

- 外部入口不应继续直接绑定 `main.py process`
- Web / Skill 不应直接理解项目目录内部细节
- 仓库需要统一的 job record / job status / job logs / review wait / continue 语义

Phase A1 应提供的最小能力：

1. 提交任务
2. 查询任务状态
3. 查询任务日志
4. 识别任务是否进入 review wait
5. 在 review 完成后继续任务
6. 在任务成功后返回结果句柄（如 `project_dir`、`manifest_path`）

---

## 4. 非目标（本阶段明确不做）

以下内容不属于 Phase A1 范围：

### 4.1 不继续深收 `process` 主逻辑
不做：

- 强行推进 `process -> ProjectWorkflow.run_build()` 的更深融合
- 重写 `process.py` 内部高风险执行逻辑
- 改造 review gate 核心语义
- 改造 `voice_review` 闭环

### 4.2 不做完整 Web 产品
不做：

- 大规模页面扩张
- 富交互结果中心
- 波形 / 时间轴 / 高级可视化前端
- 历史任务复杂管理后台

### 4.3 不做完整 OpenClaw Skill
不做：

- 完整对话式 Skill 编排
- 复杂多轮参数收集
- Skill 专属状态机
- Skill 富交互能力

### 4.4 不做商业层
不做：

- 用户体系
- 登录/权限
- 组织/团队
- 套餐/计费
- 配额管理

### 4.5 不做基础设施升级
不做：

- 多 worker 队列系统
- 数据库优先改造
- 插件化执行后端注册表
- 事件总线
- WebSocket / SSE 实时架构

### 4.6 不重建底层真相源
不做：

- 让 JobRecord 成为新的 artifact truth source
- 复制一套新的 project state / review state / artifact index 体系

---

## 5. Phase A1 的核心设计原则

### 5.1 先服务现有仓库，不做过度平台化
Phase A1 是 AIVideoTrans 当前仓库的最小任务服务层，不是通用 localization platform。

### 5.2 只支持一个正式执行后端
Phase A1 **只允许一个正式执行后端：`process-backed runner`。**

说明：

- 当前最完整、最兼容、覆盖 YouTube + review + voice_review 链路的执行主链仍然是 `process`
- `workflow` 相关能力可以保留 extension seam，但不作为 A1 对外正式支持的 job execution backend

### 5.3 review state 仍为真相源
- `review_state.json` 仍然是 review / voice_review 的 authoritative source
- job layer 只保存 lightweight review summary，用于状态查询与导航
- 不重新定义 review state schema

### 5.4 OutputDispatcher 仍为输出出口真相源
- 任务成功后的 canonical output 仍以现有 shared output 路径为准
- 结果句柄应优先引用 `manifest.json`、`project_dir`
- 不在 job layer 里持久化完整 artifacts 镜像

### 5.5 单活跃任务语义可接受
Phase A1 可以先接受：

- 单进程
- 单活跃任务
- 无真正队列系统

只要结构上不阻碍后续扩展即可。

---

## 6. Phase A1 正式范围

### 6.1 Job domain

新增最小 job model。

建议最小 `JobRecord`：

```python
@dataclass(slots=True)
class JobRecord:
    job_id: str
    job_type: str                  # "localize_video"
    source_type: str               # "youtube_url" | "local_audio" | "local_video"
    source_ref: str
    output_target: str             # "editor" | "publish" | "both"

    status: str                    # "queued" | "running" | "waiting_for_review" | "succeeded" | "failed" | "cancelled"
    current_stage: str | None
    progress_message: str | None

    created_at: str
    updated_at: str
    started_at: str | None
    completed_at: str | None

    project_dir: str | None
    manifest_path: str | None

    review_gate: dict[str, object] | None
    error_summary: dict[str, object] | None
    fallback_summary: dict[str, object] | None
```

#### JobRecord 字段约束

**必须保留**
- `job_id`
- `status`
- `current_stage`
- `project_dir`
- `manifest_path`
- `review_gate`
- `error_summary`

**允许保留摘要，不允许保留完整镜像**
- `fallback_summary`

**明确不应成为长期持久主字段**
- `artifacts[]`

说明：

- `artifacts` 可以作为响应时的派生信息
- 但不应持久化进 `JobRecord` 作为第二真相源

### 6.2 Job store

新增最小文件型存储。

建议：

- `jobs/<job_id>.json`
- `jobs/<job_id>.events.jsonl`

职责：

- 持久化 `JobRecord`
- 追加 job events / logs
- 支持按 job_id 读取
- 支持返回最近 jobs 列表

要求：

- 简单可靠
- 无数据库依赖
- 可被后续 API 层直接复用

### 6.3 Process-backed JobRunner

新增一个最小 runner，将当前 `process` 执行链路封装为统一 job backend。

建议职责：

- 接收 job spec
- 启动 process-backed execution
- 解析运行状态
- 写入 `JobRecord`
- 写入 `JobEvent`
- 识别 review wait
- 在任务完成后记录 `project_dir` / `manifest_path`
- 在任务失败时写 `error_summary`

#### A1 对 runner 的要求

**必须支持：**
- `queued -> running`
- `running -> waiting_for_review`
- `running -> succeeded`
- `running -> failed`

**建议支持：**
- `waiting_for_review -> running`（continue after review）
- `running -> cancelled`（可后置到 A2）

#### 关于执行方式

Phase A1 不强制要求立即重写底层执行方式。可接受：

- 包装现有 `ProcessPipeline` 调用链
- 或包装现有 process 命令入口
- 但对外必须收口为统一 job runner/service

重点是：

**Web / Skill / future callers 不再直接绑定 `main.py process`。**

### 6.4 最小 Job API

Phase A1 必做 API：

#### `POST /jobs`
创建任务。

输入最小示例：

```json
{
  "job_type": "localize_video",
  "source": {
    "type": "youtube_url",
    "value": "https://youtube.com/..."
  },
  "output_target": "editor"
}
```

返回：

```json
{
  "job_id": "job_xxx",
  "status": "queued"
}
```

#### `GET /jobs`
返回最近任务摘要列表。

#### `GET /jobs/{job_id}`
返回完整 job record。

#### `GET /jobs/{job_id}/logs`
返回任务事件/日志。

### 6.5 建议一并支持的 API

#### `POST /jobs/{job_id}/continue`
在 review 完成后继续任务。

这是 A1 中非常重要的能力，因为现有流程中 review 并不是附属功能，而是主链路的一部分。

如果实现成本可控，建议与 A1 一并落地。

### 6.6 A2 再考虑的 API

以下接口不属于 A1 core scope：

#### `POST /jobs/{job_id}/cancel`
可放到 A2。

#### `GET /jobs/{job_id}/artifacts`
可放到 A2。

说明：

- artifacts 更适合作为从 `manifest.json` 派生的读取接口
- 不应先于 job semantics/review continue 成为优先项

---

## 7. 与现有代码的接线策略

### 7.1 总体策略

A1 的接线原则不是“重做内核”，而是：

**在现有最完整链路外面包一层统一任务服务。**

因此：

- 不重写 `process`
- 不改变 shared output truth
- 不重建 review 系统
- 只抽离并统一“任务入口 / 状态 / 日志 / continue”

### 7.2 与 `process.py` 的关系

当前 `process.py` 已经具备：

- shared `ProjectBuilder`
- shared `project_shape_helpers`
- shared `OutputDispatcher`

因此 A1 应：

- 将 `process` 视为现阶段正式执行后端
- 通过 `ProcessJobRunner` 对其进行封装
- 避免让外部入口继续直接依赖 `process.py` 细节

### 7.3 与 `OutputDispatcher` 的关系

`OutputDispatcher` 继续作为 canonical output handoff point。

A1 只需在任务成功后记录：

- `project_dir`
- `manifest_path`

不要在 job service 中重复组织输出结构。

### 7.4 与 review / voice_review 的关系

A1 必须遵守以下约束：

- `review_state.json` 仍然 authoritative
- `voice_review` 流程保持不变
- job layer 只提供 `review_gate summary`
- `continue` 只是任务层的恢复入口，不是 review state 语义重写

### 7.5 与现有 Web UI 的关系

`src/services/web_ui.py` 中的 `ProcessJobManager` 可以作为参考来源，但不能继续成为长期真相源。

A1 的方向是：

- 抽出通用 `JobService` / `JobStore` / `ProcessJobRunner`
- Web UI 后续通过 Job API 使用这些能力
- 而不是继续把 process-only job 管理逻辑埋在 UI 内部

---

## 8. 语义规范

### 8.1 `current_stage`
`current_stage` 应暴露语义 stage 名称，而不是长期暴露 `S0/S1/S2` 这样的内部编号。

可接受示例：

- `ingestion`
- `transcription`
- `translation`
- `alignment`
- `draft_output`
- `speaker_review`
- `translation_review`
- `voice_review`
- `final_output`

允许内部日志继续保留现有细节，但 API / JobRecord 语义应尽量稳定。

### 8.2 `review_gate`
建议只保存摘要，例如：

```json
{
  "stage": "voice_review",
  "message": "voice review required before continue"
}
```

不要把完整 review state 镜像塞进 JobRecord。

### 8.3 `error_summary`
建议最小结构：

```json
{
  "stage": "translation",
  "error_type": "provider_error",
  "message": "translation provider failed"
}
```

### 8.4 `fallback_summary`
建议仅保留对外可读摘要，不复制完整内部 runtime snapshots。

---

## 9. 目录建议

建议新增目录结构如下：

```text
src/services/jobs/
  __init__.py
  models.py
  store.py
  events.py
  service.py
  process_runner.py
  api.py
```

如果当前项目组织风格更适合，也可调整文件名，但职责应保持清晰：

- `models.py`：JobRecord / JobEvent / request/response types
- `store.py`：文件型持久化
- `process_runner.py`：process-backed execution wrapper
- `service.py`：submit / read / list / continue
- `api.py`：最小 HTTP surface

---

## 10. 实现顺序

推荐按以下顺序实施。

### Step 1：Job model + JobStore
完成：

- `JobRecord`
- `JobEvent`
- `jobs/*.json`
- `jobs/*.events.jsonl`
- 基础读写测试

### Step 2：Process-backed runner
完成：

- submit -> running
- running -> succeeded / failed
- 基础日志写入
- project_dir / manifest_path 回填

### Step 3：review wait handling
完成：

- 识别 `waiting_for_review`
- 回填 `review_gate summary`

### Step 4：continue after review
完成：

- `POST /jobs/{id}/continue`
- `waiting_for_review -> running`

### Step 5：Minimal Job API
完成：

- `POST /jobs`
- `GET /jobs`
- `GET /jobs/{id}`
- `GET /jobs/{id}/logs`

### Step 6：smoke tests / regression guards
完成：

- 不破坏现有 `voice_review`
- 不破坏现有 output handoff
- 不新增第二真相源

---

## 11. 测试要求

Phase A1 至少应覆盖：

### 11.1 JobStore tests
- create/load/update job record
- append/read job events
- list recent jobs

### 11.2 Job lifecycle tests
- queued -> running -> succeeded
- queued -> running -> failed
- running -> waiting_for_review

### 11.3 Continue tests
- waiting_for_review -> continue -> running
- 无 review gate 时 continue 应拒绝

### 11.4 Guardrail tests
- JobRecord 不持久化 canonical artifacts mirror
- review state 仍从原路径读取
- 成功结果仍以 `manifest_path` 为 handoff handle

### 11.5 Smoke compatibility tests
- `voice_review` 闭环 smoke 不回退
- process output dispatch smoke 不回退

---

## 12. 本阶段的明确红线

Codex 在实现过程中，必须严格遵守以下红线：

### 红线 1
**不要把 workflow-backed execution 做成 A1 正式支持路径。**

### 红线 2
**不要把 JobRecord 做成新的 artifact truth source。**

### 红线 3
**不要改造 review_state / voice_review 的 authoritative semantics。**

### 红线 4
**不要为了“通用化”引入插件系统、队列系统、数据库强依赖、多 worker 架构。**

### 红线 5
**不要把 A1 扩张成 Web MVP 或 Skill MVP 的直接开发阶段。**

---

## 13. Phase A1 完成标准

满足以下条件即可认为 A1 完成：

1. 外部调用者可通过统一 API 提交任务
2. 外部调用者可通过统一 API 查询任务状态
3. 外部调用者可通过统一 API 查询任务日志
4. 任务可正确进入 `waiting_for_review`
5. review 完成后可通过统一入口继续任务
6. 任务完成后可返回 `project_dir` / `manifest_path`
7. 不破坏现有 `voice_review` 闭环
8. 不引入新的底层真相源
9. 不需要先开发完整 Web UI 或 Skill 才能验证本阶段成果

---

## 14. 一句话执行指令

**请基于当前仓库现状，实施一个最小、保守、process-backed 的 job-based service layer：先完成 JobRecord、JobStore、ProcessJobRunner、Minimal Job API，并支持 review wait / continue；不得把 workflow route 作为 A1 正式执行后端，不得重构 review gate / voice_review，不得把 JobRecord 做成第二 artifact truth source，不得把本阶段扩张成完整 Web 或 Skill 开发。**
