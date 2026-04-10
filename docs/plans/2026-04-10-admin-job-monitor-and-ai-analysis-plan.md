# 方案一：任务监控 + AI 日志分析

> 日期：2026-04-10
> 状态：方案（待审批）

---

## 1. 目标

在管理员后台 `/admin/jobs` 页面上增加两个能力：
1. **查看任意任务的完整事件日志** — 点击任务行展开日志面板
2. **一键 AI 分析** — 将日志发送给 DeepSeek，返回结构化的问题诊断报告

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
│ │ ## 概要                                        │   │
│ │ 该任务整体正常，S2 审校阶段 Pass 1 在 Pro 模型  │   │
│ │ 上两次 JSON 解析失败后降级到 Flash Lite...      │   │
│ │ ## 问题                                        │   │
│ │ 1. Pass 1 gemini-3.1-pro JSON 截断 (2/2 失败)  │   │
│ │ 2. Speaker A 和 B 被识别为同一人                │   │
│ │ ## 建议                                        │   │
│ │ 1. 检查 Pass 1 prompt 输出 token 限制...       │   │
│ └────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
```

**实现要点**：
- 新增 state：`expandedJobId`（当前展开的任务）、`logs`（日志数据）、`analysis`（AI 分析结果）、`analyzing`（加载状态）
- 点击行 → 如果已展开则收起，否则调 `getJobLogs(jobId)` 加载日志并展开
- 复用 `<LogViewer entries={logs} initialVisibleCount={20} />`
- "AI 分析" 按钮 → `POST /api/admin/jobs/{jobId}/analyze-logs` → 设置 analyzing → 返回后渲染 markdown
- AI 分析结果用简单的 `whitespace-pre-wrap` 渲染（不引入 markdown 库，保持轻量）

---

## 4. 后端改动

### 4.1 新增端点：`POST /api/admin/jobs/{job_id}/analyze-logs`

**位置**：`gateway/admin_settings.py`，在现有 admin jobs 端点附近

**流程**：
1. `_require_admin(user)` 校验
2. 从 Job API 获取日志：`GET {JOB_API_BASE}/jobs/{job_id}/logs`
3. 从 Job API 获取任务信息：`GET {JOB_API_BASE}/jobs/{job_id}`
4. 拼接 system prompt + 日志文本作为 user message
5. httpx POST `https://api.deepseek.com/v1/chat/completions`
   - model: `deepseek-chat`
   - API key: `os.environ.get("DEEPSEEK_API_KEY")`
6. 返回 `{ "analysis": "..." }`
7. DeepSeek key 缺失 → 返回 `{ "error": "未配置 DEEPSEEK_API_KEY" }`
8. 调用失败 → 返回 `{ "error": "分析失败: {详情}" }`

**日志文本构建**：
- 过滤掉 `[download]` 进度行（噪音太多）
- 每行格式：`{timestamp} [{stage}] {message}`
- 附加任务元数据：job_id、status、stage、video_title、service_mode、tts_provider

### 4.2 系统提示词

```
你是 AIVideoTrans 视频翻译/配音平台的运维分析专家。
用户会给你一个任务的完整事件日志，请分析流程是否正常、有无异常。

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

## 常见问题模式

1. **Pass 1 JSON 解析失败**：gemini-3.1-pro 输出截断，自动降级到 flash-lite。关注：是否频繁降级
2. **Speaker 重复命名**：两个 speaker_id 被识别为同一人名（ASR 把一个人拆成了两个 ID）
3. **Edit distance 超限**：文本修正幅度过大被拒绝，日志中会显示 ratio
4. **TTS 音色失效**：MiniMax 返回 status_code=2054，音色 ID 不存在
5. **Split 偏差回退**：word-level split 和 text-ratio split 时间偏差过大
6. **S2 重复执行**：pipeline 恢复后重跑了 S2（已修复，但旧任务可能有此问题）
7. **翻译段数不匹配**：翻译返回的 segment 数与请求不一致

## 分析输出格式

请按以下格式输出（中文）：

### 概要
一两句话总结任务整体状况。

### 流程耗时
列出各阶段的起止时间和耗时。

### 发现的问题
编号列出所有异常，每个问题包含：
- 问题描述
- 相关日志行
- 严重程度（高/中/低）

### 建议
针对发现的问题给出具体建议。

如果流程完全正常无异常，简要说明即可，不必强行找问题。
```

---

## 5. 改动文件清单

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `frontend-next/src/app/(app)/admin/jobs/page.tsx` | 修改 | 扩展：日志展开 + AI 分析 |
| `gateway/admin_settings.py` | 修改 | 新增 `analyze-logs` 端点 |

**不改动**：`log-viewer.tsx`、`app-shell.tsx`、`jobs.ts`、`mappers.ts`

---

## 6. 验证

1. 访问 `/admin/jobs` → 任务列表正常显示
2. 点击任务行 → 展开日志面板，LogViewer 显示完整事件日志
3. 再次点击 → 收起
4. 点击 "AI 分析" → 显示 loading → 显示分析结果
5. 对失败任务点 AI 分析 → 分析结果中有具体问题和建议
6. 非 admin 用户访问 → 403
7. DEEPSEEK_API_KEY 缺失 → 友好错误提示
