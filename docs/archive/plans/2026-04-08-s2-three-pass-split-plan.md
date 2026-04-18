# S2 审校三轮拆分方案

> 日期：2026-04-08
> 状态：方案（待审批）
> 前置：S2 证据链（s2_review_result.json + audit.json）已落地，merge 跨 speaker 兜底已加

---

## 总原则

**这次是一轮内部实现拆分，不是一轮外部协议重写。统一入口、统一聚合结果、严格 contract、明确 fallback、legacy 保留，是本方案稳定落地的前提。**

---

## 1. 目标

将当前 S2 单次 9 任务大模型调用拆为 3 轮，各轮职责清晰、互不干扰。

对外接口保持不变：
- 外部仍然只调用 `review_transcript()`
- 下游仍然只消费统一的 `ReviewResult`
- 后续流程（翻译、翻译审校、音色确认、TTS）不感知 S2 内部是单次还是三轮

三轮拆分只发生在 `review_transcript()` 内部编排层。

---

## 2. 当前 vs 拆分后流程

```
当前：
ASR → S2(一次性9任务) → [speaker_review] → 翻译配置 → 翻译 → 翻译审核 → 音色选择 → TTS

拆分后：
ASR → S2 { Pass1(speaker) → Pass2(text) } → [speaker_review] → 翻译配置 → 翻译 → 翻译审核 → Pass3(voice profile) → 音色选择 → TTS

对外：review_transcript() 的入口和 ReviewResult 输出不变
```

---

## 3. 严格 Contract

### Pass 1 只允许输出

| 字段 | 允许 |
|------|------|
| `speakers` | ✅ |
| `corrections[action=correct_speaker]` | ✅ |
| `corrections[action=fix_text]` | ❌ 禁止 |
| `corrections[action=merge]` | ❌ 禁止 |
| `corrections[action=split]` | ❌ 禁止 |
| `glossary` | ❌ 禁止 |

### Pass 2 只允许输出

| 字段 | 允许 |
|------|------|
| `corrections[action=fix_text]` | ✅ |
| `corrections[action=split]` | ✅ |
| `glossary` | ✅ |
| `corrections[action=correct_speaker]` | ❌ 禁止 |
| `corrections[action=merge]` | ❌ 禁止 |

### Pass 3 只允许输出

| 字段 | 允许 |
|------|------|
| `speaker_profiles` | ✅ |
| `corrections` | ❌ 禁止 |
| `glossary` | ❌ 禁止 |

### 越界处理

任一 pass 如果输出越界字段：
- 调用端丢弃越界字段
- 写 audit（记录丢弃事件）
- 不让越界结果污染主链

---

## 4. Fallback 规则

| 场景 | 行为 |
|------|------|
| Pass 1 失败（API/JSON/必填缺失） | 整次 fallback 到 `legacy_review_transcript_single_pass()` |
| Pass 2 失败（API/JSON/必填缺失） | 整次 fallback 到 `legacy_review_transcript_single_pass()` |
| Pass 3 失败 | 不回滚 Pass 1/2，回退到 `_fallback_minimal_speaker_styles()` |
| JSON 解析失败 | 视为该 pass 失败 |
| 必填字段缺失 | 视为该 pass 失败 |

不允许输出半成品 transcript truth。

---

## 5. Pass 1：说话人识别 + 纠正

### 输入

| 项目 | 来源 |
|------|------|
| 压缩音频 | source_audio → ffmpeg 16kHz mono opus（现有逻辑不变） |
| 转录稿 | ASR 输出，格式 `[index](start-end) speaker_id: text` |
| 视频标题 | YouTube 元数据 |
| 视频链接 | 下载链接 |

### 任务

1. 识别每个 speaker 的身份（name, gender, age_group, role, style）
2. 只在非常确定时纠正 ASR 的 speaker 标注（只输出 `correct_speaker`）

### 处理逻辑

1. 调用大模型（带音频）
2. 过滤越界 corrections（只保留 `correct_speaker`，丢弃其他 action）
3. 应用 `correct_speaker` corrections
4. `_apply_interview_sanity_check` 保留为可控 safety net（不删除，但不作为主路径依赖，后续 benchmark 验证后再决定）
5. 写入 `transcript/s2_pass1_result.json`
6. 更新 transcript.json

### 成本

音频 token 只在 Pass 1 消耗。10 分钟视频 ≈ 19,200 audio tokens + 4,800 text tokens。

---

## 6. Pass 2：文本修正 + 语义拆分 + 术语表

### 输入

| 项目 | 来源 |
|------|------|
| 修正后转录稿 | Pass 1 输出（speaker 已修正） |
| speakers dict | Pass 1 的 speakers 输出（作为 context） |
| 视频标题 | YouTube 元数据 |

**不传音频**——纯文本调用。

### 任务

1. 修正 ASR 文本错误（fix_text）
2. 拆分超长段落（split，>60s 在语义断点拆为 15-45s）
3. 生成术语表（glossary）

**不做合并（merge）**——去掉，容易出错。

### 处理逻辑

1. 调用大模型（纯文本，不传音频）
2. 过滤越界 corrections（只保留 `fix_text` + `split`，丢弃 `correct_speaker` / `merge`）
3. 应用 corrections
4. `_enforce_max_duration` 兜底（>180s 机械拆分）
5. 写入 `transcript/s2_pass2_result.json`
6. 更新 transcript.json

### 成本

纯文本，~5,000 tokens 输入。Flash Lite ≈ ¥0.003。

---

## 7. Pass 3：音色描述（翻译审核后、音色确认前）

### 位置

```
S2 { Pass1 + Pass2 } → 翻译 → 翻译审核 → Pass3(音色描述) → 音色选择
```

不新增用户可见的 stage 名称，不扰乱现有 review_state/UI 协议。

### 输入

| 项目 | 来源 |
|------|------|
| 每个 speaker 的音频片段 | 从 source_audio 按时间码提取，每人 15-30s |
| speaker 基本信息 | Pass 1 的 speakers dict |

### 音频提取策略

每个 speaker：
1. 找该 speaker 最长的连续 utterance
2. 如果 <15s，拼接相邻同 speaker utterances，直到 15-30s
3. ffmpeg 提取 → 压缩为 opus

### 任务

1. 描述音色特征（voice_description）
2. 确认/修正性别（gender）和年龄段（age_group）
3. 推断 persona_style 和 energy_level

### 处理逻辑

1. 提取音频片段
2. 调用大模型（音频 + prompt）
3. 过滤越界字段（只接受 `speaker_profiles`，丢弃任何 corrections/glossary）
4. 写入 `transcript/s2_pass3_result.json`
5. 将结果注入 segments（voice_description, gender, age_group, persona_style, energy_level）

### 与音色匹配衔接

Pass 3 输出直接供 `voice_reranker.combined_rerank()` 使用：
- `gender` → 性别过滤
- `age_group` → 年龄评分
- `persona_style` → persona 评分
- `energy_level` → 能量评分
- `voice_description` → CosyVoice infer_persona_style / infer_energy_level

### 成本

每 speaker 15-30s 音频 ≈ 500-1000 tokens。3 speakers ≈ 3,000 audio tokens。

---

## 8. 产物设计

一次性写 4 份：

| 文件 | 作用 |
|------|------|
| `transcript/s2_pass1_result.json` | Pass 1 原始结果 |
| `transcript/s2_pass2_result.json` | Pass 2 原始结果 |
| `transcript/s2_pass3_result.json` | Pass 3 原始结果 |
| `transcript/s2_review_result.json` | **统一聚合出口**（继续作为排障首选） |

每个 pass 文件至少包含：
- `review_model`
- `prompt_version`
- `has_audio`
- `fallback_used`
- `generated_at`

`s2_review_result.json` 聚合 3 轮结果，格式不变，下游排障/分析优先看聚合文件。

---

## 9. 代码结构

### 新函数

```python
# transcript_reviewer.py

def review_transcript(...)  -> ReviewResult:
    """统一入口（不变）。内部编排 Pass1→Pass2→Pass3。"""
    try:
        pass1 = _review_pass1_speakers(...)
        pass2 = _review_pass2_text(...)
    except Pass1Or2Failure:
        return legacy_review_transcript_single_pass(...)

    # Pass3 在 pipeline 层调用（不在这里）
    return ReviewResult(speakers=pass1.speakers, glossary=pass2.glossary, ...)

def _review_pass1_speakers(...)  -> Pass1Result:
    """Pass 1：speaker 识别 + 纠正"""

def _review_pass2_text(...)  -> Pass2Result:
    """Pass 2：文本修正 + 拆分 + 术语表"""

def review_pass3_voice_profiles(...)  -> dict:
    """Pass 3：音色描述。由 pipeline 在翻译审核后单独调用。"""

def legacy_review_transcript_single_pass(...)  -> ReviewResult:
    """旧单次 S2 逻辑，作为 fallback 保留。"""
```

### Legacy 保留

不删旧逻辑。当前 `review_transcript()` 的核心逻辑收为 `legacy_review_transcript_single_pass()`，新的 `review_transcript()` 作为 orchestrator。

---

## 10. 涉及文件

| 文件 | 改动 |
|------|------|
| `src/services/transcript_reviewer.py` | 拆函数 + 3 套 prompt + contract 过滤 + fallback |
| `src/pipeline/process.py` | Pass3 调用插入翻译审核后；`review_transcript()` 入口不变 |

### 不需要改的

- 前端（speaker_review、翻译审核、音色选择 UI 不变）
- Gateway
- 音色匹配模块（voice_reranker 照用）
- review_state 阶段定义

---

## 11. `_apply_interview_sanity_check` 处理

- 第一版不作为主路径依赖
- 不立刻物理删除
- 保留为可控 fallback/debug safety net
- 等 benchmark 证明 Pass 1 足够稳定后再决定是否完全删除

---

## 12. 成本对比（10 分钟视频）

| | Pass 1 | Pass 2 | Pass 3 | 总计 | 当前单次 |
|---|---:|---:|---:|---:|---:|
| **音频 tokens** | 19,200 | 0 | ~2,000 | 21,200 | 19,200 |
| **文本 tokens** | 4,800 | 5,000 | 500 | 10,300 | 6,000 |
| **输出 tokens** | 800 | 1,500 | 600 | 2,900 | 2,500 |
| **Flash Lite** | ¥0.007 | ¥0.003 | ¥0.002 | **¥0.012** | **¥0.009** |
| **Pro** | ¥0.050 | ¥0.015 | ¥0.010 | **¥0.075** | **¥0.065** |

增幅：Flash Lite +33%（+¥0.003/任务），Pro +15%（+¥0.010/任务）。

---

## 13. 测试要求

| # | 测试场景 | 验证内容 |
|---|---------|---------|
| 1 | Pass 1 只改 speaker | corrections 里无 fix_text/merge/split |
| 2 | Pass 2 只改 text/split | corrections 里无 correct_speaker/merge |
| 3 | Pass 3 只产出 profile | 无 corrections、无 glossary |
| 4 | `review_transcript()` 聚合 | 输出兼容当前 `ReviewResult` |
| 5 | Pass 1 失败 | legacy fallback 正常返回 |
| 6 | Pass 2 失败 | legacy fallback 正常返回 |
| 7 | Pass 3 失败 | 下游不崩，用 minimal profile |
| 8 | 多场景覆盖 | 多 speaker / news interview / 实名人物 / batch transcript |

---

## 14. 实施步骤

| 步骤 | 内容 |
|------|------|
| 1 | 将当前 `review_transcript()` 核心逻辑收为 `legacy_review_transcript_single_pass()` |
| 2 | 写 Pass 1 prompt + `_review_pass1_speakers()` + contract 过滤 |
| 3 | 写 Pass 2 prompt + `_review_pass2_text()` + contract 过滤 |
| 4 | 新的 `review_transcript()` 编排 Pass1→Pass2 + fallback 到 legacy |
| 5 | 写 Pass 3 prompt + `review_pass3_voice_profiles()` + 音频提取 |
| 6 | `process.py` 在翻译审核后插入 Pass 3 调用 |
| 7 | 产物写入（4 份 JSON） |
| 8 | 测试验证 |

---

## 15. 风险

| 风险 | 缓解 |
|------|------|
| Pass 1/2 连续失败 → 用户体验退化 | fallback 到 legacy 单次路径，用户无感 |
| Pass 3 音频提取逻辑复杂 | 复用现有 ffmpeg 工具函数 |
| 缓存恢复兼容性 | 各 Pass 独立写产物，恢复按产物存在性判断 |
| 大模型输出越界 | contract 过滤 + audit 记录 |

---

## 16. 向后兼容

- `review_transcript()` 入口签名不变
- `ReviewResult` 输出格式不变
- 后续阶段不感知内部拆分
- Pass 3 的输出格式兼容 `_apply_review_speaker_styles_to_segments()`

---

## 附录：推荐 Prompt 草案

### Pass 1 Prompt

```
你正在执行视频转录审校的 Pass 1。你的唯一目标是：
1. 识别每个 speaker 的身份与基础属性
2. 只在非常确定时纠正 ASR 的 speaker 标注

你不是在做全文润色，不是在做术语表，不是在做拆分或合并。

输入信息：
- 视频标题：{video_title}
- 视频链接：{video_url}
- 转录文本：{transcript_body}
- 如有音频，请优先使用音频判断说话人是否为同一人

必须遵守的规则：
1. 保留转录中已经出现的所有 speaker_id，不要删除任何 speaker key
2. 只输出你能确认的 speaker 基础信息：name, gender, age_group, role, style
3. 如果某个 speaker 的真实姓名不确定，可以留空字符串，但不要漏掉该 speaker_id
4. 只允许输出 `correct_speaker` 类型的 corrections
5. 绝对不要输出 `fix_text` / `merge` / `split`
6. 不要仅凭"这句话在谈某个人"就推断说话人归属
7. 不要仅凭人物身份猜测重分配 speaker
8. 不确定时不要改 speaker。宁可少改，也不要错改
9. 只有在音色、上下文、连续发言关系都支持时，才允许 `correct_speaker`
10. 保持 speaker_id 使用输入里已有的格式，如 `speaker_a`, `speaker_b`, `speaker_c`

输出 JSON，且只能输出 JSON：

{
  "speakers": {
    "speaker_a": {
      "name": "",
      "gender": "",
      "age_group": "",
      "role": "",
      "style": ""
    }
  },
  "corrections": [
    {
      "action": "correct_speaker",
      "index": 12,
      "to": "speaker_b",
      "reason": "简短说明为什么非常确定"
    }
  ]
}
```

### Pass 2 Prompt

```
你正在执行视频转录审校的 Pass 2。Pass 1 已经完成 speaker 识别与 speaker 纠正。
你的唯一目标是：
1. 修正文本文字错误
2. 对过长段落做语义拆分
3. 提取术语表

你不是在做 speaker 重分配，不是在做音色描述，不是在做身份识别。

输入信息：
- 视频标题：{video_title}
- 已校正 speaker 的转录文本：{transcript_body_after_pass1}
- speakers 信息：{speakers_json}

必须遵守的规则：
1. 绝对不要修改任何 speaker_id
2. 绝对不要输出 `correct_speaker`
3. 不要输出 `merge`
4. 只允许输出：
   - `fix_text`
   - `split`
   - `glossary`
5. `fix_text` 只修正明显 ASR 错误、重复、漏词、错词
6. 不要改写语气，不要润色，不要重写内容
7. 不要改变原文核心含义
8. `split` 只用于过长段落，并且必须在自然语义断点切开
9. 如果某段并不适合拆分，就不要强行拆分
10. glossary 只收录稳定、值得后续翻译统一的专名、机构名、术语、人名

输出 JSON，且只能输出 JSON：

{
  "corrections": [
    {
      "action": "fix_text",
      "index": 5,
      "old": "原错误文本",
      "new": "修正后文本",
      "reason": "简短说明"
    },
    {
      "action": "split",
      "index": 18,
      "at_text": "建议切分点附近的文本",
      "reason": "该段过长，需要在自然断点拆分"
    }
  ],
  "glossary": {
    "Berkshire Hathaway": "伯克希尔·哈撒韦",
    "Greg Abel": "格雷格·艾贝尔"
  }
}
```

### Pass 3 Prompt

```
你正在执行视频音色画像分析的 Pass 3。
前两个阶段已经完成 speaker 识别、speaker 纠正、文本修正与术语表提取。
你的唯一目标是：根据每个 speaker 的代表性音频片段，生成适合 TTS 选音匹配的音色画像。

你不是在做 speaker 纠正，不是在做文本修正，不是在做术语表。

输入信息：
- 视频标题：{video_title}
- speaker 基础信息：{speakers_json}
- 当前 speaker 列表：{speaker_ids}
- 每个 speaker 的代表音频片段（单独提供）

必须遵守的规则：
1. 不要输出 corrections
2. 不要输出 glossary
3. 只输出每个 speaker 的音色画像
4. voice_description 要面向 TTS 匹配，描述声音特征，不要写成人物背景介绍
5. gender 只能是：male / female / unknown
6. age_group 只能是：young / middle / elderly / unknown
7. persona_style 尽量从以下集合中选最接近者：
   - professional
   - warm
   - serious
   - energetic
   - calm
8. energy_level 只能是：low / medium / high
9. 不确定时可以输出 `unknown`，不要强猜

输出 JSON，且只能输出 JSON：

{
  "speaker_profiles": {
    "speaker_a": {
      "voice_description": "声音清晰、语速中等偏快、音高偏中高，整体专业且稳定",
      "gender": "female",
      "age_group": "middle",
      "persona_style": "professional",
      "energy_level": "medium"
    },
    "speaker_b": {
      "voice_description": "声音偏低沉，语速较慢，带停顿感，整体沉稳",
      "gender": "male",
      "age_group": "elderly",
      "persona_style": "calm",
      "energy_level": "low"
    }
  }
}
```
