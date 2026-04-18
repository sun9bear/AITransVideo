# JOB_SERVICE_PHASE_A1.md

# AIVideoTrans Job Service Phase A1

## 1. 目标

本文件只定义 **当前阶段** 的最小实施边界。

AIVideoTrans 当前不应继续把主精力放在更深的 `process` convergence，也不应直接展开 Web MVP 或 OpenClaw Skill MVP。当前最合适的动作是：

**建立一个最小、保守、process-backed 的 job-based service layer。**

Phase A1 要解决的问题只有这些：

- 给现有最完整执行链路提供统一任务入口
- 给未来 Web / OpenClaw Skill 提供统一后端协议
- 统一任务状态、日志、review wait、continue 语义
- 在不破坏既有 review / voice_review / output 闭环的前提下，完成最小服务化收口

Phase A1 应交付的最小能力：

1. 提交任务
2. 查询任务状态
3. 查询任务日志
4. 识别任务是否进入 review wait
5. 在 review 完成后继续任务
6. 在任务成功后返回结果句柄（`project_dir` / `manifest_path`）

---

## 2. 非目标

以下内容 **明确不属于** Phase A1：

### 2.1 不继续深收 `process` 主逻辑
不做：

- 强行推进 `process -> ProjectWorkflow.run_build()` 的进一步深融合
- 重写 `process.py` 内部主执行路径
- 深改 runtime / recovery / TTS / alignment 边界
- 重写 process-only state adapter

### 2.2 不做完整 Web 产品
不做：

- 大规模 Web 页面扩张
- 富交互结果中心
- 波形 / 时间轴 / 高级前端交互
- 复杂任务历史管理后台

### 2.3 不做完整 OpenClaw Skill
不做：

- 完整对话式 Skill 状态机
- 多轮复杂参数收集
- Skill 富交互编排
- Skill 专属执行后端

### 2.4 不做商业层
不做：

- 账户
- 组织
- 登录/权限
- 套餐/计费
- 配额

### 2.5 不做重型基础设施
不做：

- 多 worker 队列
- 数据库优先改造
- 插件式执行后端注册
- 通用事件总线
- WebSocket / SSE 实时架构

### 2.6 不重建底层真相源
不做：

- 让 JobRecord 成为第二个 artifact truth source
- 复制新的 project state / review state / artifact index 体系

---

## 3. JobRecord

Phase A1 只需要最小 JobRecord。

建议结构：

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

### 3.1 JobRecord 必须保留的字段

- `job_id`
- `status`
- `current_stage`
- `project_dir`
- `manifest_path`
- `review_gate`
- `error_summary`

### 3.2 JobRecord 允许保留的摘要字段

- `fallback_summary`

### 3.3 JobRecord 不应长期持久化的字段

- `artifacts[]`

说明：

- `artifacts` 可以作为读取响应时的派生信息
- 但不应持久化进 JobRecord 作为第二真相源
- `manifest.json`、`ArtifactIndex`、`project_state.json`、`review_state.json` 仍然是底层真实产物

### 3.4 状态机

Phase A1 最小状态机：

- `queued`
- `running`
- `waiting_for_review`
- `succeeded`
- `failed`
- `cancelled`

### 3.5 `current_stage` 语义

对外应暴露语义阶段名，不应长期暴露 `S0/S1/S2` 这种内部编号。

可接受值示例：

- `ingestion`
- `transcription`
- `translation`
- `alignment`
- `draft_output`
- `speaker_review`
- `translation_review`
- `voice_review`
- `final_output`

---

## 4. JobStore

Phase A1 使用最小文件型存储，不要求数据库。

建议：

- `jobs/<job_id>.json`
- `jobs/<job_id>.events.jsonl`

职责：

- 持久化 `JobRecord`
- 追加 `JobEvent`
- 按 `job_id` 读取
- 列出最近 jobs

要求：

- 简单
- 可靠
- 可测试
- 可直接被 API 层复用

不要求：

- 分布式一致性
- 真正队列语义
- 多 worker 协调

Phase A1 允许单活跃任务语义。

---

## 5. process-backed runner

## 5.1 正式执行后端

Phase A1 **只允许一个正式执行后端：`process-backed runner`。**

原因：

- 当前最完整、最兼容、覆盖 YouTube + review + voice_review 闭环的执行主链仍然是 `process`
- `workflow` 虽然已经承载共享 vocabulary / build / output 语义，但还不应在 A1 被正式产品化为外部 job execution backend

### 明确约束

- 可以保留 workflow extension seam
- 但不能把 workflow route 作为 A1 正式支持路径

## 5.2 runner 职责

`ProcessJobRunner` 只负责：

- 接收 job spec
- 启动 process-backed execution
- 解析运行状态
- 写入 `JobRecord`
- 写入 `JobEvent`
- 识别 `waiting_for_review`
- 在成功后回填 `project_dir` / `manifest_path`
- 在失败时写 `error_summary`

## 5.3 runner 最小生命周期支持

必须支持：

- `queued -> running`
- `running -> waiting_for_review`
- `running -> succeeded`
- `running -> failed`

建议支持：

- `waiting_for_review -> running`（continue after review）
- `running -> cancelled`（可推迟到 A2）

## 5.4 执行方式要求

Phase A1 不要求立即重写底层调用方式。可接受：

- 包装现有 `ProcessPipeline`
- 或包装现有 process 命令入口

但无论内部如何实现，对外都必须体现为统一的 job runner / service 语义。

重点：

**外部调用方不再直接绑定 `main.py process`。**

---

## 6. minimal API

Phase A1 必做 API：

### `POST /jobs`
创建任务。

最小输入示例：

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

最小返回：

```json
{
  "job_id": "job_xxx",
  "status": "queued"
}
```

### `GET /jobs`
返回最近任务摘要列表。

### `GET /jobs/{job_id}`
返回完整 JobRecord。

### `GET /jobs/{job_id}/logs`
返回任务事件/日志。

---

### 建议与 A1 一并支持

#### `POST /jobs/{job_id}/continue`
用于 review 完成后继续任务。

这是 A1 中非常重要的接口，因为 review 在当前项目中是主链路的一部分，而不是附属功能。

---

### 不属于 A1 core 的接口

以下可放到 A2 之后再考虑：

- `POST /jobs/{job_id}/cancel`
- `GET /jobs/{job_id}/artifacts`

原因：

- 当前更重要的是 job semantics
- `continue after review` 比 artifacts/download 更优先
- artifacts 更适合作为从 `manifest.json` 派生的读取接口

---

## 7. review / voice_review guardrails

这一节是 Phase A1 的硬边界。

### 7.1 review_state 仍然 authoritative
- `review_state.json` 仍然是 review / voice_review 的真相源
- job layer 只保存 lightweight review summary
- 不重新定义 review schema

### 7.2 不得改造 `voice_review` 闭环
- 不改变 `voice_review` 的 gate 语义
- 不重构其恢复/继续机制
- 不为了 job service 重写 voice selection / binding 流程

### 7.3 `continue` 只是任务层入口
- `continue` 只是任务恢复入口
- 不是 review state 语义重写
- 不是新的 review orchestrator

### 7.4 不得让 JobRecord 成为第二个 review truth source
- JobRecord 只保存：
  - `review_gate.stage`
  - `review_gate.message`
- 不保存完整 review state 镜像

### 7.5 必须保留 smoke compatibility
Phase A1 完成后，至少应保持：

- `voice_review` 闭环 smoke 不回退
- process output dispatch smoke 不回退

---

## 8. 一句话执行边界

**请在当前仓库上实现一个最小、保守、process-backed 的 job-based service layer：先完成 JobRecord、JobStore、ProcessJobRunner、Minimal Job API，并支持 review wait / continue；不得把 workflow route 作为 A1 正式执行后端，不得重构 review gate / voice_review，不得把 JobRecord 做成第二 artifact truth source，不得把本阶段扩张成 Web MVP 或 Skill MVP。**
