# S2 大模型审校阶段：数据链全景分析 + 拆分优化方案

> 日期：2026-04-08
> 代码位置：`src/services/transcript_reviewer.py` + `src/pipeline/process.py`

---

## 一、当前 S2 做了什么？一次性输出 JSON

### Prompt 输入

| 输入 | 来源 | 格式 |
|------|------|------|
| 压缩音频 | source_audio → ffmpeg 16kHz mono opus | OGG 文件上传 |
| 转录稿 | ASR (AssemblyAI) 输出 | `[index](start-end) speaker_id: text` |
| 视频标题 | YouTube 元数据 | 字符串 |
| 视频链接 | 下载链接 | 字符串 |

### Prompt 要求大模型做 9 件事

| # | 任务 | 目的 | 输出字段 |
|---|------|------|---------|
| 1 | 识别说话人身份 | 把 speaker_a/b/c 对应到真实姓名 | `speakers.*.name` |
| 2 | 纠正说话人标注 | 修正 ASR 标错的 speaker | `corrections[{action:"correct_speaker"}]` |
| 3 | 修正转录文本 | 去重、修正 ASR 错误 | `corrections[{action:"fix_text"}]` |
| 4 | 合并误拆段落 | A-B-A 快速交叉修复 | `corrections[{action:"merge"}]` |
| 5 | 拆分超长段落 | >60s 段落拆到 15-45s | `corrections[{action:"split"}]` |
| 6 | 生成术语表 | 人名/专有名词中文翻译 | `glossary` |
| 7 | 分析说话风格 | 语气/口头禅（给翻译参考） | `speakers.*.style` |
| 8 | 描述音色特征 | TTS 音色匹配参考 | `speakers.*.voice_description` |
| 9 | 标注性别/年龄 | TTS 音色匹配必需 | `speakers.*.gender`, `speakers.*.age_group` |

### Gemini 输出 JSON 结构

```json
{
  "speakers": {
    "speaker_a": {
      "name": "贝基·奎克",
      "gender": "female",
      "age_group": "middle",
      "role": "CNBC主持人",
      "style": "语速适中，提问专业",
      "voice_description": "声音清晰专业，语速适中，音调中等偏高"
    },
    "speaker_b": { ... }
  },
  "glossary": {
    "Berkshire Hathaway": "伯克希尔·哈撒韦",
    "Warren Buffett": "沃伦·巴菲特"
  },
  "corrections": [
    {"action": "correct_speaker", "index": 7, "to": "speaker_b", "reason": "..."},
    {"action": "merge", "indices": [3, 4], "speaker": "speaker_b", "reason": "..."},
    {"action": "split", "index": 15, "at_text": "So what changes", "reason": "..."},
    {"action": "fix_text", "index": 5, "old": "Gred Abel", "new": "Greg Abel", "reason": "..."}
  ]
}
```

---

## 二、JSON 处理链路：4 步处理

### Step 1: `_apply_corrections()` — 应用 Gemini 的 4 种修改指令

| action | 做了什么 | 校验/限制 |
|--------|---------|----------|
| `correct_speaker` | 改某行的 speaker_id | `speaker_id` 必须匹配 `^speaker_[a-z0-9_]+$` |
| `merge` | 合并相邻行（删后面的，拼接文本） | 间隔 <2s，合并后 <180s |
| `split` | 在文本断点处拆行 | 原行 >15s 才拆 |
| `fix_text` | 替换文本 | 编辑距离 <30%，长度变化 <30% |

### Step 2: `_apply_interview_sanity_check()` — 采访类 2-speaker 后处理

仅当 `len(speakers) == 2` 时触发：
- 短问句 (<4s) → 分给 host
- 短回应 (<1.2s) → 分给 host
- 第一人称长回答 (>2.5s) → 分给 guest
- 长句 (>2s) → 不动

**已知问题**：当 Gemini speakers dict 只返回 2 个 key（省略了 speaker_c），该检查会在 3-speaker 视频上被错误触发。

### Step 3: `_enforce_max_duration()` — 强制拆分 >180s 段

安全保底，防止 TTS 超长段落。

### Step 4: Re-index — 重新编号

`line.index = i + 1`，确保连续。

---

## 三、数据去了哪里？对后续流程的 6 个影响

### 3.1 `speakers.*.name` → 说话人姓名

**消费者**：
- `process.py:769-772` — 写入 `speaker_name_a`, `speaker_name_b`
- 前端翻译审核页面 → 下拉框 option 文本
- 前端音色选择页面 → 说话人名称显示

**问题**：
- 只处理 speaker_a 和 speaker_b（line 769-772 硬编码）
- speaker_c+ 的名字被丢弃
- 日志只打印 Speaker A 的名字（`effective_speakers == 2` 判断）

### 3.2 `speakers.*.gender` + `speakers.*.age_group` → 音色匹配核心

**消费者**：
- `process.py:1267-1268` — 写入 segment.gender, segment.age_group
- `voice_reranker.combined_rerank()` — 作为评分维度
- `voice_match_resolver.resolve_voice_match()` — 驱动自动音色选择

**重要性**：★★★★★ 这是音色匹配的第一输入。gender 错 → 男声配女声，age 错 → 老人配少年声。

### 3.3 `speakers.*.voice_description` → TTS 音色描述

**消费者**：
- `process.py:1266` — 写入 segment.voice_description
- `cosyvoice_voice_selector.infer_persona_style()` — 从描述推断 persona
- `cosyvoice_voice_selector.infer_energy_level()` — 从描述推断 energy

**重要性**：★★★ 间接影响音色匹配质量。

### 3.4 `speakers.*.style` + `speakers.*.role` → 翻译风格参考

**消费者**：
- `_review_speaker_styles` 传给翻译阶段
- 翻译 prompt 里用于指导语气/口吻
- `_apply_interview_sanity_check` 里用 role 判断 host/guest

**重要性**：★★ 翻译质量间接受影响。

### 3.5 `glossary` → 术语表

**消费者**：
- `process.py:1034` — 传给 `translator.translate()` 的 `glossary` 参数
- 翻译 prompt 的术语约束（"Berkshire Hathaway" 必须译为 "伯克希尔·哈撒韦"）

**重要性**：★★★ 直接影响翻译一致性。

### 3.6 `corrections` → 修改后的 transcript

**消费者**：
- `transcript_result.lines` 被替换为修正后的版本
- 写入 `transcript/transcript.json`
- 所有后续阶段（翻译、TTS、对齐）都基于修正后的转录

**重要性**：★★★★★ 说话人标注错误会导致 TTS 用错音色。

---

## 四、核心问题：为什么 9 件事塞进 1 个 prompt？

### 4.1 历史原因

原来是 4 个独立 LLM 调用（speaker 审校、文本修正、术语提取、风格分析），后来为了节省 token 和减少 API 调用次数，合并成了 1 个 multimodal 调用。

### 4.2 当前的问题

| 问题 | 影响 |
|------|------|
| **任务干扰** | 9 个任务竞争 attention，低优先级任务（术语表）可能拖累高优先级任务（speaker 识别） |
| **输出不可控** | 一个 JSON 里所有数据耦合，speaker 识别错了，correction 也跟着错 |
| **无法分步验证** | 不能先确认 speaker 对不对，再做 correction |
| **prompt 过长** | 9 个任务的指令 + 转录稿 + 音频 → token 消耗大 |
| **错误传播** | speaker 识别出错 → sanity check 基于错误的 speakers dict 做二次修改 → 错上加错 |
| **非确定性叠加** | 1 次调用的随机性已经够大，correction 和 speaker 识别的随机性叠加 |

---

## 五、拆分优化方案

### 方案：按任务类型拆为 3 个阶段调用

```
S2-A: Speaker 识别（听音频 + 看转录）
  输入: 音频 + 转录稿 + 视频标题
  输出: speakers dict（name, gender, age_group, role, style, voice_description）
  特点: 只识别，不修改。这是最关键的任务，独享完整 attention

S2-B: 转录修正（看转录 + 可选音频）
  输入: 转录稿 + S2-A 的 speakers dict（作为 context）
  输出: corrections（correct_speaker, merge, split, fix_text）+ glossary
  特点: 已知每个 speaker 是谁，修正时有明确锚点

S2-C: 音色描述增强（可选，当 S2-A 输出不够详细时）
  输入: 音频片段 + S2-A 的 speakers dict
  输出: 更详细的 voice_description, persona_style, energy_level
  特点: 纯感知任务，不影响转录内容
```

### 为什么这样拆？

| 维度 | 当前（1 次调用） | 拆分后（2-3 次调用） |
|------|---------------|------------------|
| **Speaker 识别准确性** | 被其他 8 个任务分散 attention | 独享完整 attention，准确率最高 |
| **Correction 质量** | 不确定 speaker 是谁就开始改 | 已知 speaker 身份后再改，有明确依据 |
| **错误传播** | speaker 错 → correction 错 → sanity check 错 | speaker 单独验证，correction 基于确认的身份 |
| **token 消耗** | 音频 token 被 9 个任务共享 | S2-A 音频 token 集中用于识别；S2-B 可以不传音频 |
| **可验证性** | 全部合在一起，无法中间检查 | S2-A 结果可以在 speaker_review 阶段让用户确认 |
| **API 成本** | 1 次调用 ~1200 tokens/s × 音频时长 | S2-A 同样消耗；S2-B 纯文本便宜很多；S2-C 可选 |

### 各阶段 Prompt 设计思路

#### S2-A: Speaker 识别 Prompt

```
你是说话人识别专家。听音频，结合视频标题和转录稿，识别每个说话人。

任务：
1. 听音频，区分不同说话人的音色
2. 从视频标题和对话内容推断每个人的真实姓名
3. 标注性别（male/female）和年龄段（young/middle/elderly）
4. 描述每个人的音色特征（用于后续 TTS 配音选择）
5. 描述每个人的说话风格（用于后续翻译）

⚠ 核心原则：
- 不要修改任何转录内容
- 不要合并或拆分任何段落
- 不要输出 corrections
- 只做"识别"，不做"修改"

输出：
{
  "speakers": {
    "speaker_a": {"name": "中文姓名", "gender": "...", "age_group": "...", "role": "...", "style": "...", "voice_description": "..."},
    ...
  },
  "glossary": {"English term": "中文翻译", ...}
}
```

**好处**：Gemini 只需要专注听音频分辨谁是谁，不用同时想着怎么改转录。

#### S2-B: 转录修正 Prompt

```
你是转录审校专家。以下是已确认的说话人信息：
{speakers_json}

请对照转录稿，输出修改指令。

任务：
1. 纠正说话人标注错误（根据已确认的说话人身份）
2. 修正明显的 ASR 文本错误
3. 合并被误拆的段落（仅 A-B-A 快速交叉）
4. 拆分超长段落（>60s）

输出：
{
  "corrections": [...]
}
```

**好处**：
- 已经知道 speaker_a = 贝基·奎克（女），speaker_b = 巴菲特（男），修正时不会搞混
- 不需要传音频（speaker 已确认），纯文本调用，成本低
- 如果 S2-A 的 speaker 识别有误，用户可以在 speaker_review 阶段修正后再跑 S2-B

### 实施优先级

| 优先级 | 改动 | 收益 |
|--------|------|------|
| **P0** | S2-A 独立出来，speaker 识别不再和 correction 混在一起 | 解决说话人识别被干扰的根本问题 |
| **P1** | S2-B 基于确认的 speakers 做 correction | 解决 correction 基于错误 speaker 的问题 |
| **P2** | 去掉 `_apply_interview_sanity_check`，用 S2-B 的明确 correction 替代 | 去掉当前的错误放大器 |
| **P3** | S2-C 音色描述增强（可选） | 提升音色匹配质量 |

### 与现有 Pipeline 的兼容性

```
当前流程：
  ASR → S2(一次性审校) → speaker_review(用户确认) → 翻译 → 音色选择 → TTS

拆分后：
  ASR → S2-A(speaker识别) → speaker_review(用户确认/修正) → S2-B(转录修正) → 翻译 → 音色选择 → TTS
```

关键变化：
- `speaker_review` 阶段在 S2-A 之后、S2-B 之前
- 用户确认了 speaker 身份后，S2-B 带着确认的身份去做 correction
- 如果用户在 speaker_review 里改了某个 speaker 的名字/性别，S2-B 用改后的版本

---

## 六、拆分成本分析

### 基准：Gemini Token 定价

| 模型 | 输入价格 (¥/百万 token) | 输出价格 (¥/百万 token) |
|------|---:|---:|
| Gemini 2.5 Flash Lite | 0.27 | 1.10 |
| Gemini 3.1 Pro | 1.75 | 7.00 |

音频 token rate：~32 tokens/秒

### 场景 1：4 分钟 Studio 视频（13 段，3 speaker）

| | 输入 tokens | 输出 tokens | Flash Lite 成本 | Pro 成本 |
|---|---:|---:|---:|---:|
| **当前（1 次调用）** | | | | |
| 音频 (240s × 32) | 7,680 | | | |
| Prompt (9 个任务) | 2,000 | | | |
| 转录稿 | 1,500 | | | |
| 输出 JSON | | 1,500 | | |
| **小计** | **11,180** | **1,500** | **¥0.005** | **¥0.030** |
| | | | | |
| **拆分后（2 次调用）** | | | | |
| S2-A: 音频+prompt+转录 | 9,980 | 800 | | |
| S2-B: 纯文本 prompt+转录 | 2,400 | 500 | | |
| **小计** | **12,380** | **1,300** | **¥0.005** | **¥0.031** |
| | | | | |
| **差异** | +1,200 (+11%) | -200 | **+¥0.000** | **+¥0.001** |

### 场景 2：10 分钟快捷版视频（~40 段，2 speaker）

| | 输入 tokens | 输出 tokens | Flash Lite 成本 | Pro 成本 |
|---|---:|---:|---:|---:|
| **当前（1 次调用）** | | | | |
| 音频 (600s × 32) | 19,200 | | | |
| Prompt (9 个任务) | 2,000 | | | |
| 转录稿 (~40 段) | 4,000 | | | |
| 输出 JSON | | 2,500 | | |
| **小计** | **25,200** | **2,500** | **¥0.009** | **¥0.062** |
| | | | | |
| **拆分后（2 次调用）** | | | | |
| S2-A: 音频+prompt+转录 | 24,000 | 1,000 | | |
| S2-B: 纯文本 prompt+转录 | 5,000 | 1,200 | | |
| **小计** | **29,000** | **2,200** | **¥0.010** | **¥0.073** |
| | | | | |
| **差异** | +3,800 (+15%) | -300 | **+¥0.001** | **+¥0.011** |

### 场景 3：30 分钟长视频（~120 段，分 batch）

| | 输入 tokens | 输出 tokens | Flash Lite 成本 | Pro 成本 |
|---|---:|---:|---:|---:|
| **当前（2 batch）** | | | | |
| 音频 (1800s × 32) | 57,600 | | | |
| Prompt × 2 | 4,000 | | | |
| 转录稿 × 2 (overlap) | 13,000 | | | |
| 输出 JSON × 2 | | 5,000 | | |
| **小计** | **74,600** | **5,000** | **¥0.026** | **¥0.166** |
| | | | | |
| **拆分后（1+2 调用）** | | | | |
| S2-A: 音频+prompt+转录 | 70,600 | 1,500 | | |
| S2-B: 纯文本 × 2 batch | 14,000 | 3,000 | | |
| **小计** | **84,600** | **4,500** | **¥0.028** | **¥0.180** |
| | | | | |
| **差异** | +10,000 (+13%) | -500 | **+¥0.002** | **+¥0.014** |

### 成本结论

| 视频长度 | 额外成本 (Flash Lite) | 额外成本 (Pro) | 增幅 |
|----------|---:|---:|---:|
| 4 分钟 | +¥0.000 | +¥0.001 | ~3% |
| 10 分钟 | +¥0.001 | +¥0.011 | ~12% |
| 30 分钟 | +¥0.002 | +¥0.014 | ~8% |

**音频 token 是绝对大头（占 75%+），只在 S2-A 传 1 次，不增加。S2-B 是纯文本调用，成本可忽略。**

10 分钟视频拆分后多花不到 1 分钱。而一次 speaker 标错导致用户重跑任务的 TTS API 成本远高于此。

---

## 七、快速止血（不拆分，修复当前问题）

如果暂时不做大拆分，以下小修可以止血：

1. **`_apply_interview_sanity_check` 检查实际 speaker 数量**（而非 Gemini dict 大小）
2. **修复 3 处 hardcoded 2-speaker 限制**
3. **保存 Gemini 原始 JSON 到文件**（已有 debug_output_dir 机制，确认已启用）
4. **sanity check 前加日志**，记录每步改了什么
