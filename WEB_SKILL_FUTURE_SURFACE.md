# WEB_SKILL_FUTURE_SURFACE.md

# AIVideoTrans Web / Skill Future Surface（未来参考）

本文件不是当前 Phase A1 的实施依据。  
它的作用是保存此前 Web / OpenClaw Skill 相关文档中仍然有价值的页面、API、交互设想，供后续阶段参考。

原则：

- 现在不直接开发完整 Web MVP 或 Skill MVP
- 但当前的 Job API / service layer 设计，不应堵死未来表面能力
- 本文只保留未来值得预留的 surface，不把它们升级为当前实现范围

---

## 1. 产品结构方向

未来产品结构仍建议保持：

- **Web**：主产品壳 / 主交互面 / 主商业化承载面
- **OpenClaw Skill**：智能入口 / 轻操作入口 / 辅助式任务入口
- **统一后端**：Job API / service layer 作为 Web 与 Skill 的共同后端

这意味着：

- Web 和 Skill 不应分别直连不同内部执行链路
- 当前 Phase A 的设计，应保证后续两个入口都能复用同一任务协议

---

## 2. Future Web surface

未来 Web 页面可围绕以下最小结构展开。

### 2.1 新建任务页
目标：

- 输入 YouTube URL / 本地媒体源
- 选择基础输出目标
- 提交任务到统一 Job API

未来可扩展项：

- provider 选项
- voice preset 选项
- 输出目标切换
- 高级参数折叠面板

当前只需保证 Job API 的 submit payload 不会把这些未来选项完全堵死。

---

### 2.2 任务状态页
目标：

- 展示任务状态
- 展示当前阶段
- 展示是否进入 review gate
- 展示失败摘要 / fallback 摘要

未来可扩展项：

- 更细的阶段进度条
- 更丰富的 stage badges
- 更细粒度错误分类展示

当前只需保证：

- `GET /jobs/{id}` 能稳定提供这些摘要信息

---

### 2.3 日志页
目标：

- 查看当前任务日志
- 帮助定位失败阶段
- 帮助理解任务停在哪

未来可扩展项：

- 日志过滤
- 仅错误日志
- 搜索
- 按阶段折叠展开

当前只需保证：

- `GET /jobs/{id}/logs` 有稳定事件流/日志输出

---

### 2.4 结果页
目标：

- 展示任务成功后的结果句柄
- 提供面向用户的结果摘要
- 后续可以挂 editor / publish / manifest 派生读取

未来可扩展项：

- result summary
- artifact cards
- 下载区
- manifest-based output grouping

当前只需保证：

- job record 至少返回 `project_dir` / `manifest_path`
- 后续 artifacts/result-summary 可从 manifest 派生

---

### 2.5 历史任务页
目标：

- 查看最近任务
- 进入状态/日志/结果页

未来可扩展项：

- 搜索
- 过滤
- 失败任务筛选
- review-waiting 筛选

当前只需保证：

- `GET /jobs` 能返回最近 jobs 摘要列表

---

## 3. Future Skill surface

OpenClaw Skill 未来更适合作为“智能入口”，而不是完整产品壳。

### 3.1 适合 Skill 的核心意图
未来 Skill 更适合先支持以下意图：

1. 创建任务  
   例如：
   - “帮我翻译这个 YouTube 视频”
   - “创建一个视频本地化任务”

2. 查询任务状态  
   例如：
   - “这个任务跑到哪一步了”
   - “为什么它停住了”

3. 查看失败原因 / review gate  
   例如：
   - “它为什么失败了”
   - “是不是卡在 voice review 了”

4. 获取结果摘要  
   例如：
   - “任务完成了吗”
   - “产出了什么结果”

### 3.2 当前不适合先做进 Skill 的能力
不建议早期放进 Skill：

- 复杂多轮参数配置
- 大量高级执行选项
- 细粒度 artifact 浏览
- 富交互审校 UI
- 大量下载操作

这些更适合 Web 承载。

### 3.3 Skill 对后端的最小依赖
未来 Skill 至少会依赖这些 payload surface：

- submit payload
- status payload
- review-required payload
- result-summary payload

因此当前 Job API 设计时，应预留这些返回摘要，而不要求现在就把 Skill 做出来。

---

## 4. Future API surface（仅参考）

以下 API 面是未来可保留的参考面，不等于 Phase A1 必做项。

### 4.1 核心任务接口
- `POST /jobs`
- `GET /jobs`
- `GET /jobs/{job_id}`
- `GET /jobs/{job_id}/logs`
- `POST /jobs/{job_id}/continue`

这些是未来 Web / Skill 的共同核心。

### 4.2 未来可扩张接口
- `POST /jobs/{job_id}/cancel`
- `GET /jobs/{job_id}/artifacts`
- `GET /jobs/{job_id}/result-summary`

说明：

- 这些适合后续 Web/Skill richer surface
- 但当前不应先于 A1 基础任务语义而开发

---

## 5. Future interaction notes

以下交互设想值得保留，但不属于当前开发任务。

### 5.1 review gate 引导
未来 Web / Skill 可在任务进入 review gate 时展示更友好的引导，例如：

- 当前卡在哪个 review stage
- 下一步用户需要做什么
- 处理完成后如何继续任务

当前只需先保证：

- job layer 能给出稳定的 `review_gate summary`

### 5.2 结果摘要而非立即暴露全部文件
未来更好的结果面，往往不是直接把所有路径裸露给用户，而是：

- 先给结果摘要
- 再按 manifest 派生展示 editor / publish / other outputs

当前只需保证：

- 输出真相源继续是 `manifest.json`
- 后续结果页可围绕 manifest 发展

### 5.3 Web 承担“重操作”，Skill 承担“轻操作”
未来建议保持：

- Web：配置、查看、审阅、下载、复杂操作
- Skill：创建、追问、查询、提醒、轻量控制

当前 Phase A 设计时，应避免把所有未来交互都压进 Skill 方向。

---

## 6. 现在不做，但设计上要留意的事情

虽然这些不是当前要做的功能，但设计上最好别把路堵死：

- Job API payload 里应允许未来增加可选 `options`
- status payload 里应允许未来增加 richer summary
- logs/events 结构应允许未来区分 info/warn/error/stage
- result surface 应尽量围绕 manifest 派生，而不是围绕 job record 镜像

---

## 7. 使用方式说明

本文件的使用方式是：

- 作为未来 Web / Skill 设计参考
- 作为当前 API 设计时的“预留面提醒”
- 不作为当前 Codex 实现范围扩张依据

换句话说：

**可以参考本文件预留接口面，但不能因为本文件存在，就把当前 Phase A1/A2/A3 扩张成完整 Web 或 Skill 开发。**
