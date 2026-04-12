# 方案一：任务监控 + AI 日志分析

> 日期：2026-04-10
> 状态：方案（已审校，待实施）
> 审校：Claude Opus + Codex，2026-04-10

---

## 1. 目标

在管理员后台 `/admin/jobs` 页面上增加两个能力：
1. **查看任意任务的完整事件日志** — 点击任务行展开日志面板
2. **一键 AI 分析** — 将日志 + 结构化上下文发送给 DeepSeek，返回结构化的问题诊断报告

---

## 2. 现有基础设施

| 组件 | 文件 | 说明 |
|------|------|------|
| 任务列表页 | `frontend-next/src/app/(app)/admin/jobs/page.tsx` | 已有表格（状态/取消/删除），无日志 |
| LogViewer 组件 | `frontend-next/src/components/log-viewer.tsx` | workspace 页面已在用，展开/收起/分级着色 |
| 日志 API | `GET /job-api/jobs/{job_id}/logs` | 返回 `{ events: JobEvent[], lines: string[] }` |
| 日志获取函数 | `frontend-next/src/lib/api/jobs.ts:63` | `getJobLogs(jobId)` → `JobLogEntry[]` |
| 日志映射 | `frontend-next/src/lib/api/mappers.ts:93` | `toJobLogEntries()` |
| Admin 权限模式 | `gateway/admin_settings.py` | `_require_admin(user)` 校验 |
| DeepSeek 环境变量 | `DEEPSEEK_API_KEY` | 已在 `.env` 中配置 |
| JobEvent 模型 | `src/services/jobs/events.py:25` | 结构化字段：`event_type`, `level`, `status`, `payload` |
| result-summary API | `GET /job-api/jobs/{job_id}/result-summary` | 含 `error_summary`, `fallback_summary` |

---

## 3. 前端改动

### 3.1 扩展 `admin/jobs/page.tsx`

**交互设计**：

```
┌─────────────────────────────────────────────────────┐
│ 任务管理                              共 N 个任务    │
├─────────────────────────────────────────────────────┤
│ Job ID | 标题 | 用户 | 状态 | 阶段 | 创建时间 | 操作 │
│ abc..  | xxx  | u@.. | 运行中| S2  | 04-10   | 取消 │ ← 点击行
│ def..  | yyy  | ...  | 完成  | —   | 04-09   | 删除 │
├─────────────────────────────────────────────────────┤
│ ▼ 任务日志 — job_abc...           [AI 分析] 按钮    │
│ ┌─────────────────────────────────────────────────┐ │
│ │ LogViewer（完整事件日志，默认展开最近 20 条）      │ │
│ └─────────────────────────────────────────────────┘ │
│ ┌─ AI 分析结果 ─────────────────────────────────┐   │
│ │ 概要                                          │   │
│ │ 该任务整体正常，S2 审校阶段 Pass 1 在 Pro 模型  │   │
│ │ 上两次 JSON 解析失败后降级到 Flash Lite...      │   │
│ │                                                │   │
│ │ 发现的问题                                      │   │
│ │ [!] Pass 1 gemini-3.1-pro JSON 截断 (2/2 失败)  │   │
│ │ [!] Speaker A 和 B 被识别为同一人                │   │
│ │                                                │   │
│ │ 建议                                            │   │
│ │ 1. 检查 Pass 1 prompt 输出 token 限制...       │   │
│ └────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
```

**实现要点**：
- 新增 state：`expandedJobId`（当前展开的任务）、`logs`（日志数据）、`analysis`（AI 分析结果）、`analyzing`（加载状态）
- 点击行 → 如果已展开则收起，否则调 admin 日志端点加载日志并展开
- 复用 `<LogViewer entries={logs} initialVisibleCount={20} />`
- "AI 分析" 按钮 → `POST /api/admin/jobs/{jobId}/analyze-logs` → 设置 analyzing → 返回后渲染
- **前端缓存**：`analysisCache: Record<string, AnalysisResult>` — 同一 session 内同 jobId 不重复请求 DeepSeek（`AnalysisResult` = 4.6 中 JSON schema 对应的类型）
- **AI 分析结果渲染**：后端返回结构化 JSON（见 4.3），前端原生渲染各分节，无需 markdown 库

### 3.2 日志 API 路径（admin 专用）

`getJobLogs()` 走 Gateway 常规 proxy，有**用户归属校验**。Admin 看别人的 job 会被拦。

**方案**：前端用 `fetch("/api/admin/jobs/${jobId}/logs")` 走 admin 路由。后端在 `admin_job_monitor_api.py` 新增 admin 日志端点，直接从 Job API 取日志，绕过归属校验。

**LogViewer 映射**：admin 日志端点返回 `{ events, lines }`，前端复用现有 `toJobLogEntries(response.events)` 做映射（`mappers.ts:93`），不另写映射函数。

---

## 4. 后端改动

### 4.1 新建文件：`gateway/admin_job_monitor_api.py`

不再往 `admin_settings.py`（已 1000+ 行）加功能。新建独立模块，在 `gateway/main.py` 注册 router。

包含两个端点：
- `GET /api/admin/jobs/{job_id}/logs` — admin 日志（绕过归属校验）
- `POST /api/admin/jobs/{job_id}/analyze-logs` — AI 分析

### 4.2 端点一：`GET /api/admin/jobs/{job_id}/logs`

**流程**：
1. `_require_admin(user)` 校验
2. `GET {JOB_API_BASE}/jobs/{job_id}/logs` 获取完整事件列表
3. 原样返回 `{ events, lines }`

### 4.3 端点二：`POST /api/admin/jobs/{job_id}/analyze-logs`

**流程**：
1. `_require_admin(user)` 校验
2. **并行**从 Job API 获取三项数据（`asyncio.gather`）：
   - `GET {JOB_API_BASE}/jobs/{job_id}/logs` → events
   - `GET {JOB_API_BASE}/jobs/{job_id}` → job info
   - `GET {JOB_API_BASE}/jobs/{job_id}/result-summary` → result summary（失败 → `None`，不阻塞）
3. **智能裁剪**日志事件（见 4.4）
4. **构建富上下文**发送给 DeepSeek（见 4.5）
5. httpx POST `https://api.deepseek.com/v1/chat/completions`
   - model: `deepseek-chat`
   - API key: `os.environ.get("DEEPSEEK_API_KEY")`
   - **timeout: 90s**（LLM 推理可能较慢）
6. **解析 DeepSeek 返回的 JSON** + **schema 校验**（必须包含 `summary` 字段；`timeline`/`issues`/`suggestions` 缺失时补空数组；JSON 解析失败或 `summary` 缺失 → 返回 `{ "error": "AI 返回格式异常，请重试" }`）
7. DeepSeek key 缺失 → 返回 `{ "error": "未配置 DEEPSEEK_API_KEY" }`
8. 调用失败 → 返回 `{ "error": "分析失败" }`（不暴露内部错误详情）

### 4.4 日志裁剪策略

`read_logs()` 无截断（`service.py:192`），长任务可能有数百事件。DeepSeek-chat ~64K tokens，需要控制输入。

**裁剪规则**（后端实现，发给 DeepSeek 前执行）：
1. **必保留**：全部 `level=error` 和 `level=warn` 事件
2. **必保留**：全部 `event_type=status` 事件（状态变更），以及 `stage` 字段相对上一条发生变化的事件（阶段切换）
3. **必保留**：message 含关键词 `fallback|error|retry|fail|timeout|降级|回退` 的事件
4. **必保留**：首 5 条 + 末 10 条事件（上下文锚点）
5. **其余 info 事件**：如果总数超 200 条，只保留均匀采样的 100 条
6. 裁剪后在事件列表开头插入一条元信息：`[系统] 原始事件共 {N} 条，已裁剪为 {M} 条（保留全部 warn/error/阶段切换/关键词事件）`
7. **最终硬限**：裁剪后文本超 25K 字符 → 从中间截断 info 事件，保留首尾

### 4.5 AI 分析输入构建（富上下文）

不再只喂扁平文本。构建三段输入：

**第一段：任务元数据**
```
== 任务信息 ==
job_id: {job_id}
status: {status}
stage: {current_stage}
video_title: {video_title}
service_mode: {service_mode}
tts_provider: {tts_provider}
speakers: {speakers}
created_at: {created_at}
error_summary: {error_summary JSON（如有）}
fallback_summary: {fallback_summary JSON（如有）}
```

**第二段：结构化事件（JSON Lines）**

利用 JobEvent 的完整字段，不丢信息。`event_type` 实际只有 `log` 和 `status` 两种（`events.py:7-8`），阶段推进通过 `status` 事件 + `stage` 字段变化体现：
```
== 事件日志（{M}/{N} 条）==
{"ts":"2026-04-10T12:00:01","type":"status","level":"info","stage":"ingestion","status":"running","msg":"开始下载视频"}
{"ts":"2026-04-10T12:00:30","type":"log","level":"info","stage":"ingestion","msg":"下载完成","payload":{"duration_ms":29000}}
{"ts":"2026-04-10T12:01:00","type":"log","level":"error","stage":"speaker_review","msg":"JSON parse failed","payload":{"model":"gemini-3.1-pro","attempt":1}}
{"ts":"2026-04-10T12:01:05","type":"status","level":"info","stage":"speaker_review","status":"running","msg":"S2 审校开始"}
...
```

每条事件保留：`ts`(created_at), `type`(event_type: `log`|`status`), `level`, `stage`, `msg`(message), `status`(如有，任务状态变更), `payload`(如有，截断到 200 字符)

**第三段：result-summary（如有）**
```
== 结果摘要 ==
{result-summary JSON，移除大型嵌套，只保留顶层字段}
```

### 4.6 系统提示词

```
你是 AIVideoTrans 视频翻译/配音平台的运维分析专家。
用户会给你一个任务的元数据、结构化事件日志和结果摘要，请分析流程是否正常、有无异常。

## 平台架构

这是一个视频翻译/配音 SaaS，Pipeline 流程：
- S0 输入准备：下载视频、提取音频、分离人声
- S1 媒体理解：AssemblyAI 转录（说话人分离）、语言检测
- S2 说话人审核：三段式 LLM 审校
  - Pass 1（说话人识别）：Gemini + 音频，识别说话人身份、纠正 speaker 分配
  - Pass 2（文本修正）：Gemini 纯文本，修正转录错误、拆分过长段落、提取术语表
  - Pass 3（音色画像）：Gemini + 音频片段，为每个说话人生成音色描述
  - 失败自动降级到 legacy 单次审校
- S3 翻译审核：翻译（默认 DeepSeek）→ 等待用户确认翻译稿
- S4 草稿与配音：注入音色描述
- TTS 合成：MiniMax(studio) / CosyVoice(express) / VolcEngine
- 音频对齐：时长匹配 + 可能触发重写
- 输出：配音音频 + 字幕 + 下载包

## 两种模式

- Studio（工作台版）：需人工审核（翻译审核 + 音色选择），MiniMax TTS，支持声音克隆
- Express（快捷版）：跳过 Pass 1，无人工审核，CosyVoice TTS，自动匹配音色

## 审核暂停点

Studio 模式下 pipeline 会在以下阶段暂停等待用户操作：
- 翻译审核（translation_review）：用户确认翻译文本
- 音色选择审核（voice_selection_review）：用户为每个说话人选择或克隆音色

## 事件日志说明

事件日志为 JSON Lines 格式，每条包含：
- ts：时间戳
- type：事件类型，只有两种 — `log`（普通日志）和 `status`（状态变更）
- level：info / warn / error
- stage：所处流水线阶段（阶段推进通过 stage 字段值的变化体现）
- msg：消息文本
- status：任务状态变更（如有，常见值：running / waiting_for_review / succeeded / failed）
- payload：附加数据（如有，可能含模型名、重试次数、耗时等）

日志可能经过裁剪，会在开头注明原始/保留数量。全部 warn/error 事件已保留。

## 常见问题模式

1. **Pass 1 JSON 解析失败**：gemini-3.1-pro 输出截断，自动降级到 flash-lite。关注：是否频繁降级
2. **Speaker 重复命名**：两个 speaker_id 被识别为同一人名（ASR 把一个人拆成了两个 ID）
3. **Edit distance 超限**：文本修正幅度过大被拒绝，日志中会显示 ratio
4. **TTS 音色失效**：MiniMax 返回 status_code=2054，音色 ID 不存在
5. **Split 偏差回退**：word-level split 和 text-ratio split 时间偏差过大
6. **S2 重复执行**：pipeline 恢复后重跑了 S2（已修复，但旧任务可能有此问题）
7. **翻译段数不匹配**：翻译返回的 segment 数与请求不一致

## 输出要求

请严格按以下 JSON 格式输出（不要输出 JSON 以外的内容）：

{
  "summary": "一两句话总结任务整体状况",
  "timeline": [
    { "stage": "阶段名", "start": "时间", "end": "时间", "duration": "耗时", "note": "备注（可选）" }
  ],
  "issues": [
    {
      "title": "问题标题",
      "severity": "high | medium | low",
      "detail": "问题描述",
      "evidence": "相关日志行或数据"
    }
  ],
  "suggestions": [
    "具体建议 1",
    "具体建议 2"
  ]
}

如果流程完全正常无异常，issues 和 suggestions 可以为空数组，summary 简要说明即可。
```

### 4.7 前端渲染 AI 分析结果

后端返回结构化 JSON，前端原生渲染各分节：

- **概要**：`analysis.summary` → 普通文本段落
- **流程耗时**：`analysis.timeline` → 简单表格（阶段 / 起止 / 耗时 / 备注）
- **发现的问题**：`analysis.issues` → 卡片列表（severity 颜色区分：high=红/medium=黄/low=灰）
- **建议**：`analysis.suggestions` → 编号列表

这样无需 markdown 库，渲染效果也比 `pre-wrap` 好得多。

---

## 5. 改动文件清单

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `frontend-next/src/app/(app)/admin/jobs/page.tsx` | 修改 | 扩展：日志展开 + AI 分析 |
| `gateway/admin_job_monitor_api.py` | **新建** | admin 日志端点 + AI 分析端点 |
| `gateway/main.py` | 修改 | 注册 admin_job_monitor_api router |

**不改动**：`admin_settings.py`、`log-viewer.tsx`、`app-shell.tsx`、`jobs.ts`、`mappers.ts`

---

## 6. 验证

1. 访问 `/admin/jobs` → 任务列表正常显示
2. 点击任务行 → 展开日志面板，LogViewer 显示完整事件日志
3. 再次点击 → 收起
4. 点击 "AI 分析" → 显示 loading → 显示结构化分析结果（概要 + 耗时表 + 问题卡片 + 建议列表）
5. 对失败任务点 AI 分析 → 分析结果中有具体问题和建议，severity 颜色正确
6. 再次点同一任务的 AI 分析 → 命中前端缓存，不重复请求
7. 非 admin 用户访问 → 403
8. DEEPSEEK_API_KEY 缺失 → 友好错误提示
9. 长任务（100+ 事件）→ 日志正常裁剪，分析正常返回

---

## 附录：审校记录

### 审校来源
- Claude Opus 审校（2026-04-10）
- Codex 审校（2026-04-10）

### 合并采纳的建议

| 来源 | 建议 | 处理 |
|------|------|------|
| **Codex [P1]** | AI 输入太"扁平"，丢失 event_type/level/payload 结构化字段 | 采纳，改为 JSON Lines 格式保留全部结构化字段 |
| **Codex [P1]** | 缺少日志裁剪策略，长任务会打爆 token | 采纳，新增 4.4 裁剪规则（保留 warn/error/阶段切换/关键词，其余采样+硬限） |
| **Codex [P2]** | 只取 logs 和 job info 不够，应加 result-summary | 采纳，并行获取三项数据，error_summary/fallback_summary 一起喂 |
| **Codex [P2]** | 不应继续塞 admin_settings.py | 采纳，新建 admin_job_monitor_api.py |
| **Codex [P2]** | markdown 输出 + pre-wrap 渲染不匹配 | 采纳，改为结构化 JSON 输出 + 前端原生渲染 |
| **Opus** | 日志截断上限 | 采纳，与 Codex 裁剪建议合并 |
| **Opus** | DeepSeek 超时 | 采纳，设 90s timeout |
| **Opus** | 并行获取 job info + logs | 采纳，扩展为三项并行（加 result-summary） |
| **Opus** | Markdown 渲染问题 | 采纳，与 Codex 建议合并，改为结构化 JSON |
| **Opus** | 日志 API 权限问题 | 采纳，新建 admin 日志端点 |
| **Opus** | 前端缓存避免重复调 DeepSeek | 采纳 |
| **Opus** | 错误信息清洗 | 采纳，不暴露内部错误详情 |
| **Codex R2 [P1]** | event_type 只有 `log`/`status`，方案用了虚构的 `stage_started/stage_completed` | 采纳，裁剪规则改为保留全部 `status` 事件 + `stage` 变化事件；示例和 prompt 全部对齐真实模型 |
| **Codex R2** | 前端缓存类型应为对象而非 string | 采纳，改为 `Record<string, AnalysisResult>` |
| **Codex R2** | admin 日志端点到 LogViewer 的映射路径未明确 | 采纳，明确复用 `toJobLogEntries()` |
| **Codex R2** | AI 返回 JSON 需后端 schema 校验 | 采纳，新增校验 + 降级错误处理 |
