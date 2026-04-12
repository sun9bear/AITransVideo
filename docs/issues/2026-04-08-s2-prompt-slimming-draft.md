# S2 Prompt 瘦身草案

> 日期：2026-04-08
> 状态：草案，未接入运行时
> 目标：把 **speaker correction** 从当前 S2 的 9 合 1 任务里解耦出来，先让模型专注“谁在说话”，再考虑文本修正和配音侧元数据

---

## 1. 当前问题

当前 S2 prompt 一次性要求模型完成这些任务：

1. 识别说话人身份
2. 纠正说话人标注
3. 修正转录文本
4. 合并误拆段落
5. 拆分超长段落
6. 生成术语表
7. 分析说话风格
8. 描述音色特征
9. 标注性别和年龄段

这会让 speaker correction 这个本应最保守、最克制的任务，和身份识别、文本修正、配音元数据一起竞争模型注意力。

---

## 2. 建议拆分方向

### Phase A：Speaker Correction Only

只做两件事：

1. 判断 ASR 的说话人标注是否明显错误
2. 输出最小化 `correct_speaker` / `merge` 指令

**不做：**

- 文本修正
- 术语表
- 说话风格
- 音色描述
- 性别年龄
- 强制识别每个 speaker 的真实姓名

### Phase B：Metadata / Text Polish

只在 speaker 已稳定后，再做：

- 文本修正
- 术语表
- 风格
- 音色描述
- 性别年龄

这样可以把“谁在说话”和“这个人是谁/怎么配音”分开。

---

## 3. Speaker Correction 专注版 Prompt 草案

```text
你是转录审校专家。
你的唯一任务是：听音频、对照转录稿，判断 ASR 的 speaker 标注是否有明显错误。

视频标题：{video_title}
视频链接：{video_url}

## 任务边界

你这次只负责：

1. 保守地纠正明显错误的 speaker 标注
2. 在确实因为 speaker 误判而误拆时，输出 merge 指令

你这次不要负责：

- 修正文案
- 拆分长段落
- 生成术语表
- 分析风格
- 生成音色描述
- 推断 gender / age_group
- 强行给每个 speaker 识别真实姓名

## 最高优先级规则

1. 保留 ASR 已识别出的所有不同 speaker。
2. 不要因为角色猜测、语义猜测或“谁更像主持人/嘉宾”就重分配 speaker。
3. 只有在音色证据非常明确时，才允许改 speaker。
4. 如果你不确定，就不要改。
5. 默认信任 ASR，除非你能明确听出它错了。

## 常见允许纠正的情况

- 同一人连续说话，中间因停顿被错误切到另一个 speaker
- 极短 backchannel（Yeah / Right / 嗯 / 对）被分错
- 插话/抢话导致的明显错分
- A-B-A 模式里中间极短段明显属于前后同一个人

## 明确禁止

- 不要因为“内容更像主持人在说”就改 speaker
- 不要因为“这句话像是回答/提问”就改 speaker
- 不要主动合并不同的人
- 不要减少 speaker 数量
- 不要输出任何与 speaker correction 无关的字段

## 输出 JSON

{
  "corrections": [
    {"action": "correct_speaker", "index": 25, "to": "speaker_b", "reason": "音色与前后同一人一致"},
    {"action": "merge", "indices": [24, 25], "speaker": "speaker_a", "reason": "中间短段误切"}
  ]
}

如果没有足够明确的 speaker 错误，就返回：

{
  "corrections": []
}

## 转录稿（{line_count} 行）

{transcript_body}
```

---

## 4. 设计取舍

### 为什么去掉“必须识别真实姓名”

真实姓名识别对 UI 展示和后续音色选择有价值，但它不是 speaker correction 的必要前提。
把“认人”也压进这一步，会诱导模型把“谁是谁”与“谁在说话”混在一起。

### 为什么禁止“像提问/像回答就改 speaker”

当前 deterministic post-pass 已经证明，基于 interview 角色推断的自动改 speaker 很危险。
LLM 侧也应保持同样的保守口径：**语义只能辅助，不能主导 speaker 重分配。**

### 为什么暂时只允许 `correct_speaker` / `merge`

当前问题首先是 speaker 误改，不是文本润色质量。
先把 speaker correction 收窄，才能看清后面真正还剩什么问题。

---

## 5. 建议接入顺序

1. 先保留现有 S2 运行时逻辑不变，只增加 raw response / speaker diff 证据链
2. 观察几次真实 job，确认当前错误主要来自：
   - LLM speaker correction
   - 还是后处理逻辑
3. 如果证据显示 LLM 也在误改，再把当前 9 合 1 prompt 拆成：
   - `speaker_correction`
   - `metadata_and_text_polish`
4. 拆分后先灰度到少量 Studio job，再决定是否默认启用

---

## 6. 本草案的边界

- 这是 prompt 草案，不是已实现方案
- 本草案不改变现有输出协议
- 本草案不改变下游翻译、配音、前端协议
- 本草案只解决“speaker correction 过载”这个方向
