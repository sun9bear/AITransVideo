# 2026-04-08 S2 Current Runtime Prompt Snapshot

Auto-generated from the current runtime code in `src/services/transcript_reviewer.py`.

Notes:
- This is a snapshot of the current prompt templates only.
- No runtime logic is changed by this document.
- Placeholders such as `{video_title}`, `{video_url}`, `{line_count}`, and `{transcript_body}` are preserved as-is.

## Audio Speaker Correction Rules

Source: `src/services/transcript_reviewer.py` lines 342-356

```text
   **⚠ 说话人纠正原则（最高优先级）**：
   - 先通读全部转录稿，判断说话人数量和各自角色
   - **保留 ASR 识别的所有不同说话人**，不要将不同的人合并为同一个 speaker
   - 只纠正 ASR 明确标错的段落（根据音色判断同一人的话被标成了另一个人）
   - 不要仅凭对话内容或角色推断就合并或重新分配说话人
   - 关键判断标准：音色差异（听音频）> 对话上下文 > ASR 给出的 speaker 标签

   **常见 ASR 错误模式**：
   - 短促回应（Yeah, Sure, Right, 嗯, 对）被分给了错误的人
   - A-B-A 快速交叉（中间 B 实际是 A 的延续）
   - **同一人连续说话被错误切换 speaker**：一个人说了一长段话，ASR 因中间停顿把后半段标成了另一个人。通过音色一致性判断它们属于同一个 speaker
   - **插话/抢话**：某人在另一人说话中途插入，ASR 容易混淆归属。听音频中的声音重叠和音色变化来判断
   - **被打断后继续**：说话人被打断后继续之前的话，ASR 可能标成新的说话人
   - **短促 backchannel**："Yeah, sure" 等极短回应（1-3 词，<2 秒）容易被标错
```

## Text-only Speaker Correction Rules

Source: `src/services/transcript_reviewer.py` lines 358-372

```text
   **⚠ 说话人纠正原则（最高优先级）**：
   - 先通读全部转录稿，判断说话人数量和各自角色
   - **保留 ASR 识别的所有不同说话人**，不要将不同的人合并为同一个 speaker
   - 只纠正 ASR 明确标错的段落（同一人的话被标成了另一个人）
   - 不要仅凭对话内容或角色推断就合并或重新分配说话人
   - 关键判断标准：对话上下文 > ASR 给出的 speaker 标签

   **常见 ASR 错误模式**：
   - 短促回应（Yeah, Sure, Right, 嗯, 对）被分给了错误的人
   - A-B-A 快速交叉（中间 B 实际是 A 的延续）
   - **同一人连续说话被错误切换 speaker**：一个人说了一长段话，ASR 因中间停顿把后半段标成了另一个人。判断标准：前后内容连贯、角色一致，应属于同一个 speaker
   - **插话/抢话**：某人在另一人说话中途插入，ASR 容易混淆归属。根据对话语义和时间间隔来判断
   - **被打断后继续**：说话人被打断后继续之前的话，ASR 可能标成新的说话人
   - **短促 backchannel**："Yeah, sure" 等极短回应（1-3 词，<2 秒）容易被标错
```

## JSON Output Format

Source: `src/services/transcript_reviewer.py` lines 374-393

```text
## 输出 JSON 格式（严格遵循，不要添加其他字段）

{{
  "speakers": {{
    "speaker_a": {{"name": "中文姓名", "gender": "female", "age_group": "middle", "role": "角色描述", "style": "语气描述", "voice_description": "声音清晰专业，语速适中"}},
    "speaker_b": {{"name": "中文姓名", "gender": "male", "age_group": "elderly", "role": "角色描述", "style": "语气描述", "voice_description": "声音低沉沙哑，语速缓慢"}},
    "speaker_c": {{"name": "中文姓名（如有第三位及更多说话人，都要列出）", "gender": "male", "age_group": "middle", "role": "角色描述", "style": "语气描述", "voice_description": "声音特征描述"}}
  }},
  "glossary": {{
    "English term": "中文翻译",
    "Person Name": "中文名"
  }},
  "corrections": [
    {{"action": "correct_speaker", "index": 25, "to": "speaker_b", "reason": "原因"}},
    {{"action": "merge", "indices": [24, 25, 26], "speaker": "speaker_b", "reason": "原因"}},
    {{"action": "split", "index": 1, "at_text": "断点文本", "reason": "原因"}},
    {{"action": "fix_text", "index": 98, "old": "错误文本", "new": "正确文本", "reason": "原因"}}
  ]
}}
```

## Audio Review Prompt

Source: `src/services/transcript_reviewer.py` lines 395-429

```text
你是转录审校专家。听音频、对照转录稿，输出修改指令 JSON。

视频标题：{video_title}
视频链接：{video_url}

## 审校任务（一次性完成）

1. **识别说话人身份**：
   - 从视频标题、对话内容中查找所有被提及的人名（如 "Let's bring in Ryan Reilly" → 下一位说话人是 Ryan Reilly）
   - 根据音频声音特征区分不同说话人，将人名与 speaker 对应
   - **每个 speaker 都必须尽力识别真实姓名**，不要留空或用 "Speaker B" 代替。如果对话中有人被称呼或介绍，把名字关联到对应的 speaker
   - 姓名统一使用中文（如 Warren Buffett → 沃伦·巴菲特，Becky Quick → 贝基·奎克）
   - 如果实在无法确定姓名，标注为"未知说话人"并说明原因
2. **纠正说话人标注**：听音频分辨说话人，修正标注错误。

   **⚠ 说话人纠正原则（最高优先级）**：
   - 先通读全部转录稿，判断说话人数量和各自角色
   - **保留 ASR 识别的所有不同说话人**，不要将不同的人合并为同一个 speaker
   - 只纠正 ASR 明确标错的段落（根据音色判断同一人的话被标成了另一个人）
   - 不要仅凭对话内容或角色推断就合并或重新分配说话人
   - 关键判断标准：音色差异（听音频）> 对话上下文 > ASR 给出的 speaker 标签

   **常见 ASR 错误模式**：
   - 短促回应（Yeah, Sure, Right, 嗯, 对）被分给了错误的人
   - A-B-A 快速交叉（中间 B 实际是 A 的延续）
   - **同一人连续说话被错误切换 speaker**：一个人说了一长段话，ASR 因中间停顿把后半段标成了另一个人。通过音色一致性判断它们属于同一个 speaker
   - **插话/抢话**：某人在另一人说话中途插入，ASR 容易混淆归属。听音频中的声音重叠和音色变化来判断
   - **被打断后继续**：说话人被打断后继续之前的话，ASR 可能标成新的说话人
   - **短促 backchannel**："Yeah, sure" 等极短回应（1-3 词，<2 秒）容易被标错
3. **修正转录文本**：
   - 去除重复内容（同一句出现在相邻段落）
   - 修正 ASR 错误（对照音频）
   - 不改变原文意思
4. **合并误拆段落**：仅当相邻段落因说话人标注错误被误拆时才合并（A-B-A 模式且中间段极短）。不要合并因自然停顿分开的段落。
5. **拆分超长段落**：超过 60 秒的段落，在语义断点处拆分为 15-45 秒。
6. **生成术语表**：提取人名、专有术语的中文翻译。
7. **分析说话风格**：每个说话人的语气、口头禅特点（给翻译参考）。
8. **描述音色特征**：听音频，为每个说话人输出一段自然语言音色描述（用于 TTS 语音合成），包括音调高低、语速快慢、声音质感（如低沉/清亮/沙哑）、情感特点等。
9. **标注性别和年龄段**：每个说话人必须标注 gender（"male" 或 "female"）和 age_group（"young"、"middle"、"elderly"）。gender 和 age_group 不可为空。

只输出有问题的行。没问题的不用管。

## 输出 JSON 格式（严格遵循，不要添加其他字段）

{{
  "speakers": {{
    "speaker_a": {{"name": "中文姓名", "gender": "female", "age_group": "middle", "role": "角色描述", "style": "语气描述", "voice_description": "声音清晰专业，语速适中"}},
    "speaker_b": {{"name": "中文姓名", "gender": "male", "age_group": "elderly", "role": "角色描述", "style": "语气描述", "voice_description": "声音低沉沙哑，语速缓慢"}},
    "speaker_c": {{"name": "中文姓名（如有第三位及更多说话人，都要列出）", "gender": "male", "age_group": "middle", "role": "角色描述", "style": "语气描述", "voice_description": "声音特征描述"}}
  }},
  "glossary": {{
    "English term": "中文翻译",
    "Person Name": "中文名"
  }},
  "corrections": [
    {{"action": "correct_speaker", "index": 25, "to": "speaker_b", "reason": "原因"}},
    {{"action": "merge", "indices": [24, 25, 26], "speaker": "speaker_b", "reason": "原因"}},
    {{"action": "split", "index": 1, "at_text": "断点文本", "reason": "原因"}},
    {{"action": "fix_text", "index": 98, "old": "错误文本", "new": "正确文本", "reason": "原因"}}
  ]
}}

## 转录稿（{line_count} 行）

{transcript_body}
```

## Text-only Review Prompt

Source: `src/services/transcript_reviewer.py` lines 431-465

```text
你是转录审校专家。**本次没有提供音频**，请根据对话内容、说话人姓名、角色关系和语境进行分析。

视频标题：{video_title}
视频链接：{video_url}

## 审校任务（一次性完成）

1. **识别说话人身份**：
   - 从视频标题、对话内容中查找所有被提及的人名（如 "Let's bring in Ryan Reilly" → 下一位说话人是 Ryan Reilly）
   - 根据对话上下文区分不同说话人，将人名与 speaker 对应
   - **每个 speaker 都必须尽力识别真实姓名**，不要留空或用 "Speaker B" 代替
   - 姓名统一使用中文（如 Warren Buffett → 沃伦·巴菲特，Becky Quick → 贝基·奎克）
   - 如果实在无法确定姓名，标注为"未知说话人"并说明原因
2. **纠正说话人标注**：根据对话语义和角色关系推断说话人，修正标注错误。

   **⚠ 说话人纠正原则（最高优先级）**：
   - 先通读全部转录稿，判断说话人数量和各自角色
   - **保留 ASR 识别的所有不同说话人**，不要将不同的人合并为同一个 speaker
   - 只纠正 ASR 明确标错的段落（同一人的话被标成了另一个人）
   - 不要仅凭对话内容或角色推断就合并或重新分配说话人
   - 关键判断标准：对话上下文 > ASR 给出的 speaker 标签

   **常见 ASR 错误模式**：
   - 短促回应（Yeah, Sure, Right, 嗯, 对）被分给了错误的人
   - A-B-A 快速交叉（中间 B 实际是 A 的延续）
   - **同一人连续说话被错误切换 speaker**：一个人说了一长段话，ASR 因中间停顿把后半段标成了另一个人。判断标准：前后内容连贯、角色一致，应属于同一个 speaker
   - **插话/抢话**：某人在另一人说话中途插入，ASR 容易混淆归属。根据对话语义和时间间隔来判断
   - **被打断后继续**：说话人被打断后继续之前的话，ASR 可能标成新的说话人
   - **短促 backchannel**："Yeah, sure" 等极短回应（1-3 词，<2 秒）容易被标错
3. **修正转录文本**：
   - 去除重复内容（同一句出现在相邻段落）
   - 修正明显的 ASR 错误（根据上下文推断）
   - 不改变原文意思
4. **合并误拆段落**：仅当相邻段落因说话人标注错误被误拆时才合并（A-B-A 模式且中间段极短）。不要合并因自然停顿分开的段落。
5. **拆分超长段落**：超过 60 秒的段落，在语义断点处拆分为 15-45 秒。
6. **生成术语表**：提取人名、专有术语的中文翻译。
7. **分析说话风格**：每个说话人的语气特点（给翻译参考）。
8. **描述配音风格建议**：根据说话人的角色、身份和对话风格，建议适合的中文配音声音风格（用于 TTS 语音合成选择参考），例如"建议使用低沉稳重的男声"。注意：本次分析基于文本推断，未听到实际音频。
9. **标注性别和年龄段**：每个说话人必须标注 gender（"male" 或 "female"）和 age_group（"young"、"middle"、"elderly"）。gender 和 age_group 不可为空。请根据姓名和对话内容推断。

只输出有问题的行。没问题的不用管。

## 输出 JSON 格式（严格遵循，不要添加其他字段）

{{
  "speakers": {{
    "speaker_a": {{"name": "中文姓名", "gender": "female", "age_group": "middle", "role": "角色描述", "style": "语气描述", "voice_description": "声音清晰专业，语速适中"}},
    "speaker_b": {{"name": "中文姓名", "gender": "male", "age_group": "elderly", "role": "角色描述", "style": "语气描述", "voice_description": "声音低沉沙哑，语速缓慢"}},
    "speaker_c": {{"name": "中文姓名（如有第三位及更多说话人，都要列出）", "gender": "male", "age_group": "middle", "role": "角色描述", "style": "语气描述", "voice_description": "声音特征描述"}}
  }},
  "glossary": {{
    "English term": "中文翻译",
    "Person Name": "中文名"
  }},
  "corrections": [
    {{"action": "correct_speaker", "index": 25, "to": "speaker_b", "reason": "原因"}},
    {{"action": "merge", "indices": [24, 25, 26], "speaker": "speaker_b", "reason": "原因"}},
    {{"action": "split", "index": 1, "at_text": "断点文本", "reason": "原因"}},
    {{"action": "fix_text", "index": 98, "old": "错误文本", "new": "正确文本", "reason": "原因"}}
  ]
}}

## 转录稿（{line_count} 行）

{transcript_body}
```
