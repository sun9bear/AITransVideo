# Studio 模式音色确认独立阶段设计

> 状态：设计文档
> 日期：2026-04-07
> 适用范围：Studio 模式（工作台版）的音色选择和克隆流程

---

## 1. 背景与目标

当前 Studio 模式的音色选择和翻译审核合并在同一页面，存在两个问题：

1. 只有确认了每段说话人身份后，才能精确查找每个说话人的音频片段用于克隆
2. 音色选择/克隆的交互复杂度不应污染翻译审核

本设计将音色确认拆分为独立的 review stage，插入翻译审核之后、TTS 生成之前。

### 本轮范围

**做：**
- 新增 `voice_selection_review` pipeline stage
- 音色选择 UI（含音色库下拉，按 TTS provider 区分）
- MiniMax 音色克隆流程（选片段 → 合成 → API，`need_noise_reduction=true`）
- 克隆扣点（默认 500 点，Gateway admin_settings 可配置）
- N 说话人支持（不限人数）
- 自动音色匹配（复用现有 voice_match_resolver）
- 音频片段试听
- 自动选择按钮（从列表顶部勾选至总时长 <300s）

**不做：**
- MiniMax 官方音色库完整打标签
- 用户个人克隆音色库持久化
- Gemini 音频样本质量评分
- 跨任务音色复用

---

## 2. Pipeline 流程

### 2.1 阶段顺序

```
S1 转录 → S2 说话人审核 → S3 翻译配置 → S4 翻译审核
→ 【新】S5 音色确认 → S6 TTS → S7 对齐 → S8 输出
```

### 2.2 Stage 定义

```python
VOICE_SELECTION_REVIEW_STAGE = "voice_selection_review"
# tab 映射
REVIEW_STAGE_TAB_MAP["voice_selection_review"] = "voice-selection"
```

### 2.3 进入条件

翻译审核（`translation_review`）通过后，如果 `service_mode == "studio"`：

1. 遍历翻译结果中的所有 `speaker_id`
2. 为每个说话人调用 `voice_match_resolver` 自动匹配优选音色
3. 收集每个说话人的音频片段元数据（时长、时间戳、文本）
4. 构建 payload，设置 stage 为 pending，暂停 pipeline

### 2.4 恢复条件

用户在前端确认所有说话人的音色后，approve 写入 `review_state.json`。Pipeline 恢复时：

1. 读取 approved payload 中每个说话人的 `voice_id`
2. 构建 `speaker_voices: dict[str, str]`
3. 传入 TTS 阶段

---

## 3. Review Payload 结构

### 3.1 Pending payload（Pipeline → 前端）

```json
{
  "message": "请为每位说话人选择或克隆配音音色",
  "tts_provider": "minimax",
  "speakers": [
    {
      "speaker_id": "speaker_a",
      "speaker_name": "查理·芒格",
      "segment_count": 18,
      "total_duration_s": 165.2,
      "auto_matched_voice": {
        "voice_id": "qn_male_01",
        "label": "青年男声·沉稳",
        "match_confidence": "high"
      },
      "can_clone": true,
      "segments": [
        {
          "segment_id": 1,
          "start_ms": 0,
          "end_ms": 18200,
          "duration_s": 18.2,
          "source_text": "But part of your advice..."
        }
      ]
    }
  ],
  "available_voices": [
    {
      "voice_id": "qn_male_01",
      "label": "青年男声·沉稳",
      "gender": "male",
      "provider": "minimax"
    }
  ],
  "clone_cost_credits": 500,
  "_note": "clone_cost_credits 从 Gateway admin_settings 读取，默认 500"
}
```

`can_clone` 规则：
- `true` 当该说话人所有音频片段总时长 ≥ 10s 且 TTS provider 为 minimax
- `false` 否则

`segments` 按时长降序排列。

### 3.2 Approved payload（前端 → Pipeline）

```json
{
  "speakers": [
    {
      "speaker_id": "speaker_a",
      "voice_id": "qn_male_01",
      "voice_source": "catalog"
    },
    {
      "speaker_id": "speaker_b",
      "voice_id": "vt_speaker_b_1712345678",
      "voice_source": "cloned"
    }
  ]
}
```

`voice_source`：`"catalog"` | `"cloned"` | `"auto_matched"`

---

## 4. Gateway API

### 4.1 审核确认

```
POST /job-api/jobs/{job_id}/review/voice-selection/approve
Content-Type: application/json

{
  "speakers": [
    {"speaker_id": "speaker_a", "voice_id": "qn_male_01", "voice_source": "catalog"},
    {"speaker_id": "speaker_b", "voice_id": "vt_speaker_b_xxx", "voice_source": "cloned"}
  ]
}
```

验证：所有说话人都必须有 `voice_id`，否则返回 400。

### 4.2 音频片段列表

```
GET /job-api/jobs/{job_id}/speaker-audio/{speaker_id}
```

返回：
```json
{
  "speaker_id": "speaker_a",
  "segments": [
    {
      "segment_id": 1,
      "start_ms": 0,
      "end_ms": 18200,
      "duration_s": 18.2,
      "source_text": "But part of your advice...",
      "audio_url": "/job-api/jobs/{job_id}/speaker-audio/{speaker_id}/{segment_id}.wav"
    }
  ],
  "total_duration_s": 165.2
}
```

### 4.3 音频片段播放

```
GET /job-api/jobs/{job_id}/speaker-audio/{speaker_id}/{segment_id}.wav
```

返回：音频文件（WAV），从源音频中按时间戳切片。

### 4.4 克隆音色

```
POST /job-api/jobs/{job_id}/voice-clone
Content-Type: application/json

{
  "speaker_id": "speaker_a",
  "segment_ids": [1, 5, 8]
}
```

流程：
1. 校验 `speaker_id` 格式（正则白名单：`^speaker_[a-z0-9_]+$`），拒绝非法值
2. 检查 `review_state` 中该 speaker 是否已有 `cloning` 状态锁 → 有且 `started_at` 距今 < 5 分钟则返回 409；超过 5 分钟视为死锁，强制覆盖
3. 设置 `cloning` 状态锁（含 `started_at` 时间戳，防重复提交 + 死锁恢复）
4. 验证选中片段总时长 ≥ 10s 且 < 300s
5. shadow credits 扣 500 点（`shadow_reserve`，best-effort，不阻塞克隆）
6. 通过 `run_in_executor` 异步执行 ffmpeg：将选中片段合成为一段 WAV（24kHz, 单声道, 16-bit PCM）
7. 调 MiniMax file upload API
8. 调 MiniMax voice_clone API（`need_noise_reduction: true`）
9. 等待 voice ready
10. `shadow_capture` 确认扣点
11. 清除 `cloning` 状态锁
12. 返回 `{"voice_id": "vt_speaker_a_xxx", "status": "ready"}`

失败时：`shadow_release` 退还预扣点数，清除状态锁，返回错误，前端提示用户。

---

## 5. 前端组件

### 5.1 VoiceSelectionPanel.tsx

独立组件，作为 workspace `[jobId]` 页面的一个 review tab。

**布局：**
- 顶部：阶段说明
- 中间：说话人卡片列表（每人一行）
  - 头像圆圈（A/B/C...）+ 名字 + speaker_id + 段数 + 总时长
  - 状态标签：`✓ 已匹配` | `🎤 已克隆` | `⚠ 待选择`
  - 音色下拉（按性别分组）+ 克隆按钮（MiniMax only，时长 ≥10s）
  - 时长不足时：克隆按钮灰显 + 提示文字
- 底部：统计 + 确认按钮（所有人有音色才可点击）

**设计风格：** 严格遵循 DESIGN.md §4（App Guardrails）：
- slate/teal 色系，不用紫色
- 系统中文字体
- 8px 间距节奏
- 克制、可扫读、无戏剧化

### 5.2 VoiceCloneModal.tsx

克隆音色浮层，点击「克隆音色」按钮弹出。

**布局：**
- 标题：克隆音色 — {说话人名字}
- 顶部工具栏：「⚡ 自动选择」按钮 + 选择说明
- 已选统计栏：段数 + 总时长 + 是否满足要求
- 音频片段列表（按时长降序）：
  - 勾选框 + 播放按钮 + 原文文本 + 时间戳 + 时长
  - 已选中的高亮显示
- 底部：费用提示（500 点）+ 取消 / 开始克隆按钮

**自动选择逻辑：**
- 清除当前选择
- 从列表顶部（最长片段）开始勾选
- 直到总时长接近但 < 300s
- 用户可在自动选择后手动增减

**试听：**
- 点击播放按钮 → `GET /job-api/jobs/{job_id}/speaker-audio/{speaker_id}/{segment_id}.wav`
- HTML5 Audio 播放
- 同一时间只播放一个（点击新的自动停止前一个）

**克隆状态流转：**
- 选择中 → 点击"开始克隆" → 确认费用 → 按钮立即禁用 + 克隆中（loading + 进度提示）→ 成功（关闭浮层，填入音色）/ 失败（恢复按钮，错误提示）
- 前端点击后立即 disable 按钮，防止重复提交；后端有 `cloning` 状态锁做二次防护

---

## 6. 音频切片

从源音频中按说话人 segment 的时间戳提取片段。

**方式：** 使用 ffmpeg 按 `start_ms` / `end_ms` 切片，输出统一格式 WAV（24kHz, 单声道, 16-bit PCM）。24kHz 与 MiniMax TTS 输出采样率一致，保留足够音色细节（频率响应到 12kHz），同时控制文件体积不超过 MiniMax 20MB 上传限制。Gateway 端点中通过 `run_in_executor` 异步执行 ffmpeg，避免阻塞事件循环。ffmpeg 子进程必须设置 `timeout`（建议 60s），防止损坏音频导致进程挂起产生僵尸进程。

**合成：** 克隆时将多个选中片段拼接为一段连续 WAV，按选中顺序（不重新排序）。使用 ffmpeg `concat` 拼接，输出同一格式规格（24kHz, 单声道, 16-bit PCM）。

**缓存：** 切片后的音频缓存到 job 项目目录下 `speaker_audio/{speaker_id}/segment_{id}.wav`，避免重复切片。缓存文件跟随 Job 删除时一并清理。

**安全：** 所有路径参数（`speaker_id`、`segment_id`）做正则白名单校验。切片输出路径在写入前校验绝对路径位于 job 工作目录内，防止路径遍历攻击。

---

## 7. Credits 扣点

克隆音色的扣点数由 Gateway `admin_settings` 配置，默认 500 点。

**配置方式：**
- `admin_settings.json` 新增字段 `voice_clone_cost_credits`（int，默认 500）
- 管理员可在后台系统设置页面修改
- 前端从 review payload 的 `clone_cost_credits` 字段读取并展示，不硬编码
- Gateway 在构建 voice_selection_review payload 时从 `admin_settings` 读取当前值

**扣点时机：** 克隆 API 调用前，通过 `shadow_reserve` 预扣（金额从 `admin_settings` 实时读取）。克隆成功后 `shadow_capture` 确认。克隆失败则 `shadow_release` 退还。

**当前为 shadow mode：** 扣点不阻塞克隆（V2 仍是真值），但记录到 ledger 以便后续切真。

---

## 8. 与现有系统的集成细节

### 8.1 与现有 `voice_review` stage 的关系

现有 `VOICE_REVIEW_STAGE = "voice_review"` 是 S2 阶段自动克隆失败时触发的紧急审核（如样本时长不足）。新增的 `voice_selection_review` **替代** 它成为 Studio 模式唯一的音色确认入口：

- **迁移策略：** 现有 `voice_review` 的触发路径（`VoiceReviewRequiredError`）改为触发新的 `voice_selection_review`
- 旧的 `VoiceReviewPanel.tsx` 保留但不再被 Studio 模式新任务触发
- `resolveActiveReviewStage()` 白名单新增 `"voice_selection_review"`
- 已有的进行中任务如果 review_state 中有 `voice_review`，仍由旧面板处理（向后兼容）

### 8.2 N 说话人的 Pipeline 消费

现有 Pipeline 用 `voice_id_a` / `voice_id_b` 两个变量。新的 approved payload 返回 `speakers: [{speaker_id, voice_id}]` 格式。Pipeline 消费逻辑：

```python
approved = _get_approved_review_payload(review_state_manager, VOICE_SELECTION_REVIEW_STAGE)
if approved:
    # 构建 speaker_voices dict
    speaker_voices = {s["speaker_id"]: s["voice_id"] for s in approved["speakers"]}
    # 向后兼容：同时设置 voice_id_a / voice_id_b
    voice_id_a = speaker_voices.get("speaker_a", voice_id_a)
    voice_id_b = speaker_voices.get("speaker_b", voice_id_b)
```

translator 调用时传入 `speaker_voices` dict（已在 Express 多说话人改动中实现）。

### 8.3 MiniMax 自动音色匹配

现有 `voice_match_resolver` 只支持 VolcEngine。MiniMax 暂无打标签的官方音色库，无法做基于特征的自动匹配。

**本轮策略：**
- MiniMax 进入音色确认时，每个说话人状态为「⚠ 待选择」（无自动匹配）
- CosyVoice / VolcEngine 进入时，调用各自的 matcher 自动匹配，状态为「✓ 已匹配」
- 后续迭代：MiniMax 官方音色库打标签后，扩展 `voice_match_resolver` 支持 MiniMax

### 8.4 参数校验规则

| 参数 | 校验规则 |
|------|----------|
| `speaker_id` | 正则 `^speaker_[a-z0-9_]+$`（支持 speaker_a ~ speaker_z 及数字后缀） |
| `segment_id` | 正整数（`^[1-9][0-9]*$`） |
| `job_id` | 现有 UUID 格式校验 + job 归属验证（`require_auth` + ownership check） |

路径拼接后的绝对路径必须位于该 job 的合法工作目录内。

### 8.5 Shadow Credits 跟踪

每次克隆调用使用 `job_id` + `speaker_id` 作为复合关联键：
- `reason_code = "voice_clone"`
- `related_job_id = job_id`
- ledger `metadata_json` 中记录 `{"speaker_id": "speaker_a"}`

同一 job 多个说话人各自独立 reserve/capture/release。

### 8.6 克隆进行中 + 审核确认的竞态

approve 端点在处理前检查 review_state 中是否有任何说话人处于 `cloning` 状态。如有则返回 409：

```json
{"error": "clone_in_progress", "message": "有说话人正在克隆音色，请等待完成后再确认"}
```

### 8.7 前端集成变更清单

| 文件 | 变更 |
|------|------|
| `frontend-next/src/lib/api/reviews.ts` | `resolveActiveReviewStage()` 白名单新增 `voice_selection_review` |
| `frontend-next/src/app/(app)/workspace/[jobId]/page.tsx` | 新增 `voice-selection` tab 渲染分支 |
| `frontend-next/src/types/reviews.ts` | 新增 `VoiceSelectionApprovalInput` 类型（N 说话人） |
| 阶段进度条元数据 | 新增 S5 音色确认的 label 和图标 |

### 8.8 `need_noise_reduction` 集成

在 Gateway 克隆端点中硬编码 `need_noise_reduction=true`（所有从视频提取的音频都有背景音），不修改 `MiniMaxVoiceCloneClient` 的通用接口。克隆端点在调用 `_clone_voice` 前将参数注入 payload。

### 8.9 `available_voices` 数据来源

| TTS Provider | 音色列表来源 |
|-------------|-------------|
| MiniMax | 现有 voice_library 中已注册的音色（`voice_catalog_api` 端点已有） |
| CosyVoice | `cosyvoice_voice_catalog.py` 内置音色列表 |
| VolcEngine | `volcengine_voice_catalog.py` 内置音色列表 |

本轮不新建 MiniMax 官方音色目录文件。

### 8.10 Admin 设置页更新

`admin_settings.py` 的 `AdminSettings` 模型新增 `voice_clone_cost_credits: int = 500` 字段。现有 admin 设置前端页面（`/admin/settings`）基于模型字段自动渲染，新字段会自动出现在管理页。

---

## 9. 错误处理

| 场景 | 处理 |
|------|------|
| 说话人音频总时长 < 10s | 克隆按钮灰显，提示从音色库选 |
| 选中片段总时长 ≥ 300s | 前端阻止提交，提示减少选择 |
| MiniMax 上传失败 | 返回错误，前端提示"上传音频失败，请重试" |
| MiniMax 克隆失败 | 返回错误，前端提示"克隆失败，请重试或选择预设音色" |
| 克隆超时（>180s） | 返回超时错误，建议重试 |
| credits 不足 | 提示"点数不足，请充值后重试"（V3 shadow mode 下不实际阻塞） |
| 用户未选任何音色点确认 | 按钮灰显 + 提示"请为所有说话人选择或克隆音色" |

---

## 10. 关键文件清单

### 后端新增/修改

| 文件 | 变更 |
|------|------|
| `src/services/review_state.py` | 新增 `VOICE_SELECTION_REVIEW_STAGE` 常量 |
| `src/pipeline/process.py` | 翻译审核后插入音色确认暂停点 |
| `gateway/job_intercept.py` 或新文件 | 新增 voice-selection approve / voice-clone / speaker-audio 端点 |
| `gateway/main.py` | 注册新路由 |
| `src/services/voice_clone.py` | 添加 `need_noise_reduction` 参数支持 |
| `gateway/admin_settings.py` | 新增 `voice_clone_cost_credits` 配置字段（默认 500） |

### 前端新增

| 文件 | 用途 |
|------|------|
| `frontend-next/src/components/workspace/VoiceSelectionPanel.tsx` | 音色确认主面板 |
| `frontend-next/src/components/workspace/VoiceCloneModal.tsx` | 克隆音色浮层 |
| `frontend-next/src/lib/api/voiceSelection.ts` | API 客户端函数 |

### 前端修改

| 文件 | 变更 |
|------|------|
| `frontend-next/src/app/(app)/workspace/[jobId]/page.tsx` | 新增 voice-selection tab 渲染 |

---

## 11. 不在本轮范围

| 事项 | 说明 |
|------|------|
| MiniMax 官方音色库打标签 | 后续迭代，需要整理官方音色的性别/年龄/风格标签 |
| 用户个人克隆音色库 | 按用户持久化已克隆音色，跨任务复用 |
| Gemini 音频样本评分 | 用 Gemini 对每个片段打"适合克隆"分数，自动排序 |
| 音色库试听 | 预设音色的试听播放（需要 TTS 生成 demo 音频） |
| Express 模式音色选择 | Express 继续用自动预设，不需要用户介入 |
| credits 真值扣点 | 当前 shadow mode，后续 cutover 时再切 |
