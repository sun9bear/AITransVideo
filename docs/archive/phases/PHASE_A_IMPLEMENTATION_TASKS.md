# PHASE_A_IMPLEMENTATION_TASKS.md

# AIVideoTrans Phase A 实施任务拆解

本文件只用于把 Codex 当前真正要做的事情拆清楚，按 **A1 / A2 / A3** 分阶段推进。

原则：

- 先后端最小服务层
- 再薄接 Web
- 再为 Skill 和未来前端面保留接口
- 不直接铺开 Web / Skill 全量开发

---

## A1：最小 job service layer 骨架

### A1 目标
把当前最完整的 `process` 兼容执行链路封装成统一的 job-based service layer。

### A1 交付物
建议新增：

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

### A1 任务拆解

#### A1-1 Job model
实现最小模型：

- `JobRecord`
- `JobEvent`
- request/response DTO（如需要）

要求：

- 状态机最小化
- 不持久化 `artifacts[]` 镜像
- `current_stage` 使用语义名

#### A1-2 JobStore
实现最小文件型存储：

- `jobs/<job_id>.json`
- `jobs/<job_id>.events.jsonl`

必须支持：

- create/load/update job
- append/read events
- list recent jobs

#### A1-3 Process-backed runner
新增 `ProcessJobRunner`：

- submit -> running
- running -> succeeded / failed
- 识别 `waiting_for_review`
- 成功后记录 `project_dir` / `manifest_path`
- 失败后写 `error_summary`

约束：

- 只支持 process-backed execution
- 不把 workflow route 做成正式外部执行路径

#### A1-4 JobService
实现最小 service facade：

- submit job
- get job
- list jobs
- read logs
- continue job（如果 A1 一并做）

#### A1-5 Minimal API
A1 必做：

- `POST /jobs`
- `GET /jobs`
- `GET /jobs/{job_id}`
- `GET /jobs/{job_id}/logs`

A1 强烈建议一起做：

- `POST /jobs/{job_id}/continue`

#### A1-6 测试
至少补齐：

- JobStore tests
- lifecycle tests
- review wait tests
- continue tests
- smoke compatibility tests

### A1 完成标准

满足以下即视为 A1 完成：

1. 可通过统一 API 提交任务
2. 可查询状态
3. 可查询日志
4. 可进入 `waiting_for_review`
5. review 完成后可继续
6. 成功后可返回 `project_dir` / `manifest_path`
7. 不破坏现有 `voice_review`
8. 不引入第二真相源

### A1 明确不做

- 不做 Web 大规模页面开发
- 不做 Skill 实现
- 不做 artifacts/download 丰富接口
- 不做多 worker / 队列 / 数据库
- 不做 workflow-backed execution 产品化

---

## A2：薄 Web 接线与任务壳收口

### A2 目标
让现有 Web UI 从“内嵌 process-only job manager”过渡到“调用统一 Job API 的薄壳”。

### A2 交付物
- Web UI 对 Job API 的最小调用接线
- 用统一 API 替换 UI 内嵌任务真相源
- 保持现有 review 页面和项目目录审校能力

### A2 任务拆解

#### A2-1 Web UI 提交任务改走 Job API
- 新建任务页面/入口不再直接驱动 process-only 内部逻辑
- 改为调用 `POST /jobs`

#### A2-2 状态轮询改走 Job API
- 改为调用：
  - `GET /jobs/{job_id}`
  - `GET /jobs/{job_id}/logs`

#### A2-3 review continue 改走 Job API
- review 完成后调用：
  - `POST /jobs/{job_id}/continue`

#### A2-4 兼容层收口
- `ProcessJobManager` 可以保留临时兼容层角色
- 但不能继续作为长期真相源

#### A2-5 最小补充接口（如确有必要）
A2 可考虑加入：

- `POST /jobs/{job_id}/cancel`
- `GET /jobs/{job_id}/artifacts`

但只有在 A1 稳定后再做。

### A2 完成标准

1. Web UI 能通过统一 Job API 提交任务
2. Web UI 能通过统一 Job API 读取状态与日志
3. Web UI 能通过统一 Job API continue
4. 不需要 UI 内嵌完整 process-only job manager 才能运行主流程
5. review / voice_review 闭环不回退

### A2 明确不做

- 不扩张复杂结果页面
- 不大做历史任务后台
- 不做高级前端交互
- 不重构 review 页面本身

---

## A3：未来扩展预留与表面能力补齐

### A3 目标
在不进入完整 Web MVP / Skill MVP 的前提下，为未来产品表面预留稳定接口面。

### A3 可交付内容

#### A3-1 补充读取接口
可考虑：

- `GET /jobs/{job_id}/artifacts`
- `GET /jobs/{job_id}/result-summary`

注意：

- 这些读取结果应从 `manifest.json` 派生
- 不得把 JobRecord 变成新的结果真相源

#### A3-2 Job summary 语义补齐
统一对外摘要：

- 失败阶段
- 失败类型
- fallback 是否发生
- 当前 review gate
- 最终结果句柄

#### A3-3 为 Skill 预留最小协议
预留 payload 结构即可，不要求实现完整 Skill：

- submit payload
- status payload
- review-required payload
- result-summary payload

#### A3-4 为未来 Web 页面预留表面能力
保留未来可扩张面：

- 任务列表
- 状态页
- 日志页
- 结果页

但此阶段仍不展开前端大规模开发。

### A3 完成标准

1. Future-facing payload 结构清晰
2. 结果读取接口不破坏现有真相源
3. Skill / Web 后续不需要重新设计基础任务协议
4. 仍未进入完整 Web / Skill 大开发阶段

### A3 明确不做

- 不做完整 Skill
- 不做完整 Web MVP
- 不做商业层
- 不做权限/账户/计费
- 不做平台化大重构

---

## 总体顺序约束

### 正确顺序
- 先 A1
- 再 A2
- 再 A3

### 错误顺序
不要出现以下顺序：

- 先大做 Web 页面，再补后端
- 先做 Skill 壳，再倒逼后端
- 先做 artifacts/download 丰富接口，再补 review continue
- 先做 workflow-backed execution 产品化，再收 job semantics

---

## 给 Codex 的一句话任务顺序

**请按 A1 -> A2 -> A3 顺序推进：先实现最小 process-backed job service layer，再把现有 Web UI 薄接到统一 Job API，最后再补未来 Web/Skill 会用到的读取面和 payload surface；不要反过来先做 Web、Skill 或 artifacts 丰富层。**
