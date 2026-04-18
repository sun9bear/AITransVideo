# 长视频稳定性设计方案

> 状态: DRAFT
> 日期: 2026-03-24
> 作者: Claude + 用户协作

## 1. 目标

确保视频翻译配音 pipeline 能稳定处理不同时长的视频：

| 层级 | 视频时长 | 目标 |
|------|---------|------|
| Tier 1 | ≤30min | 稳定完成，优化效率，15-30 分钟内跑完 |
| Tier 2 | 30-120min | 异步执行，容错机制，完善通知，1-2 小时内完成 |
| Tier 3 | 120-180min | 针对性优化，2-3 小时内完成 |
| 拒绝 | >180min | 前端拦截，提示裁剪后重试 |

## 2. 当前瓶颈分析

### 2.1 Pipeline 各环节耗时（3 小时 / 600 段视频）

| 环节 | 当前耗时 | 瓶颈原因 | 优化后预估 |
|------|---------|---------|---------|
| 视频下载 | 5-10min | yt-dlp 无断点续传 | 5-10min |
| 音频提取 | 2-5min | pydub 全量加载内存 | 1-2min |
| AssemblyAI 转录 | 10-15min | 异步 API，无瓶颈 | 10-15min |
| LLM 语义分段 | 1-2min | 超长文本需分块 | 3-5min |
| 翻译 | 48-52min | 顺序执行，无上下文 | 10-15min |
| TTS 配音 | 顺序 10h | 同步 API + 20 RPM 限制 | 40-60min |
| 时长对齐 | 20-40min | 25% 重试率 | 15-25min |
| 最终合成 | 5-10min | ffmpeg 内存峰值 | 5-10min |
| **总计** | **~12 小时** | | **~2-3 小时** |

### 2.2 硬限制

| 限制项 | 当前值 | 问题 |
|-------|-------|------|
| Pipeline 硬超时 | 60 分钟 | >15 分钟视频几乎不可能完成 |
| TTS RPM | 20/min（同步） | 600 段顺序执行需 10 小时 |
| 翻译批次 | 5 段/批，顺序 | 120 批 × 15s = 30 分钟 |
| 音频转码 | pydub 内存加载 | 3h WAV ~2GB → OOM 风险 |

### 2.3 成本分析（1 小时视频）

| 环节 | 费用 | 占比 |
|------|------|------|
| AssemblyAI 转录 | ¥1.2-2.8 | 2-4% |
| Gemini 语义分段 | ¥0.03 | <1% |
| Deepseek 翻译 | ¥2-3 | 3-5% |
| **MiniMax TTS** | **¥40-80** | **80-90%** |
| 音色克隆 | ¥9.9/人（一次性） | — |

TTS 占绝对大头 → checkpoint 不重复调用直接省钱。

## 3. B+ Checkpoint 方案

### 3.1 核心原则

**文件存在且完整 = 该步骤已完成。** 每次恢复扫描文件系统确定精确进度点。

### 3.2 项目目录结构

```
projects/{slug}/
  checkpoint.json                ← 全局进度（当前阶段 + 元数据）
  video/
    original.mp4                 ← 存在 = 下载完成
  audio/
    original.wav                 ← 存在 = 音频提取完成
    original_upload.mp3          ← 临时文件，上传后可删
    speech_for_asr.wav           ← 人声分离结果
  transcript/
    raw_assemblyai.json          ← 存在 = 转录完成
    segmented.json               ← 存在 = LLM 分段完成
    transcript.json              ← 存在 = 最终转录稿完成
  translation/
    glossary.json                ← 术语表（首批翻译后提取）
    batch_001.json               ← 存在 = 第 1 批翻译完成
    batch_002.json
    ...
    translation_merged.json      ← 存在 = 翻译合并完成
  tts/
    segment_001.wav              ← 存在且 >0 字节 = 完成
    segment_001.wav.tmp          ← 存在 = 写入中（不完整，需重做）
    ...
  alignment/
    segment_001_aligned.wav      ← 存在 = 对齐完成
    ...
  output/
    dubbed_audio.wav             ← 存在 = 最终合成完成
```

### 3.3 恢复逻辑

```python
def find_resume_point(project_dir):
    """扫描文件系统，确定精确恢复点。每次任务启动时执行。"""

    # 1. 清理所有 .tmp 文件（上次中断的不完整写入）
    for tmp in glob(project_dir / "**/*.tmp"):
        os.remove(tmp)

    # 2. 从后往前检查每个阶段
    if exists("output/dubbed_audio.wav"):
        return ResumePoint(stage="completed")

    if exists("alignment/"):
        done = count_valid_files("alignment/segment_*_aligned.wav")
        total = count_total_segments()
        if done == total:
            return ResumePoint(stage="output_merge")
        return ResumePoint(stage="alignment", start_segment=done)

    if exists("tts/"):
        done = count_valid_files("tts/segment_*.wav")  # 不含 .tmp
        total = count_total_segments()
        if done == total:
            return ResumePoint(stage="alignment", start_segment=0)
        return ResumePoint(stage="tts", start_segment=done)

    if exists("translation/translation_merged.json"):
        return ResumePoint(stage="tts", start_segment=0)

    if any(exists(f"translation/batch_{i:03d}.json") for i in range(999)):
        done = count_valid_files("translation/batch_*.json")
        return ResumePoint(stage="translation", start_batch=done)

    if exists("transcript/transcript.json"):
        return ResumePoint(stage="review_or_translate")

    if exists("transcript/raw_assemblyai.json"):
        return ResumePoint(stage="segmentation")

    if exists("video/original.mp4"):
        return ResumePoint(stage="audio_extraction")

    return ResumePoint(stage="ingestion")
```

### 3.4 原子写入

```python
def atomic_write(target_path, data):
    """写入 .tmp，完成后原子重命名。防止半写入被误判为已完成。"""
    tmp_path = target_path + ".tmp"
    with open(tmp_path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp_path, target_path)  # 原子操作
```

### 3.5 段级跳过逻辑

```python
# TTS 示例
for segment in segments:
    output_path = f"tts/segment_{segment.index:03d}.wav"
    if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        log(f"跳过已完成的 TTS 段 {segment.index}")
        continue  # 不调用 API，不花钱
    audio = tts_provider.synthesize(segment.text, segment.voice_id)
    atomic_write(output_path, audio)
```

## 3.6 统一 LLM 转录审校（音频 + Diff 模式）

### 背景

当前 pipeline 有 4 次独立的 LLM 调用：推断说话人姓名 → 纠正说话人标签 → 语义分段（>300s） → （无转录校对）。
优化为 **1 次多模态 LLM 调用**（音频 + 转录稿文本），一步完成全部审校任务。

### 3.6.1 流程对比

```
当前（4 次调用）：
转录 → 5层机械拆分 → ① 推断姓名 → ② 纠正说话人 → ③④ 语义拆分 → 说话人审核

优化后（1 次调用）：
转录 → 3层机械拆分(纯停顿) → ★ 统一LLM审校(音频+文本) → 说话人审核
```

机械拆分只保留前 3 层（纯停顿，不需要 LLM）：
- Layer 1: >15s + ≥3s停顿 → 按所有停顿切
- Layer 2: >45s + ≥2s停顿 → 按最长停顿切
- Layer 3: >90s + ≥1.5s停顿 → 按最长停顿切

Layer 4-5（语义拆分）移入统一审校。

### 3.6.2 多模态输入

```python
import google.genai as genai

# 上传音频到 Gemini File API
audio_file = genai.upload_file(project_dir / "audio/speech_for_asr.wav")

# 多模态请求
response = client.models.generate_content(
    model="gemini-2.5-flash-lite",
    contents=[
        audio_file,                    # 音频（Gemini 听实际发音）
        transcript_text_prompt,        # 转录稿 + 审校指令
    ],
    config=GenerateContentConfig(
        response_mime_type="application/json",
        temperature=0.1,
        max_output_tokens=8192,
    ),
)
```

不传 YouTube URL — 直接传已有音频更可靠（不依赖 YouTube 可访问性）。

### 3.6.3 Diff 模式输出

**核心：输入全量（看全貌），输出只改动（极小 token）。**

```
输入：完整 363 行转录稿（~18K tokens） + 音频（~153K tokens）
输出：只改动指令 JSON（~2-3K tokens） ✅ 极小
```

### 3.6.4 Prompt

```
你是转录审校专家。听音频、对照转录稿，输出修改指令 JSON。

视频标题：{video_title}

## 审校任务（一次性完成）

1. **识别说话人身份**：根据音频声音特征、视频标题、对话内容推断每个 speaker 的真实姓名和角色。
2. **纠正说话人标注**：听音频分辨说话人，修正标注错误。常见错误：
   - 短促回应（Yeah, Sure）被分给了错误的人
   - A-B-A 快速交叉（中间 B 实际是 A 的延续）
   - 旁白/介绍被标成了被采访者
   - **插话/抢话**：主持人在受访者说话中途插入提问，ASR 容易把插话段标成受访者。听音频中的声音重叠和音色变化来判断
   - **被打断后继续**：受访者被打断后继续之前的话，ASR 可能标成新的说话人
3. **修正转录文本**：
   - 去除重复内容（同一句出现在相邻段落）
   - 修正 ASR 错误（对照音频）
   - 不改变原文意思
4. **合并误拆段落**：仅当相邻段落因说话人标注错误被误拆时才合并（A-B-A 模式且中间段极短）。不要合并因自然停顿分开的段落。
5. **拆分超长段落**：超过 60 秒的段落，在语义断点处拆分为 15-45 秒。
6. **生成术语表**：提取人名、专有术语的中文翻译。
7. **分析说话风格**：每个说话人的语气、口头禅特点（给翻译参考）。

只输出有问题的行。没问题的不用管。

## 输出 JSON 格式

{
  "speakers": {
    "speaker_a": {"name": "Becky Quick", "role": "CNBC主持人", "style": "职业化提问，简洁直接"},
    "speaker_b": {"name": "Charlie Munger", "role": "伯克希尔·哈撒韦副董事长", "style": "睿智幽默，喜用比喻"}
  },
  "glossary": {
    "Benjamin Franklin": "本杰明·富兰克林",
    "Berkshire Hathaway": "伯克希尔·哈撒韦",
    "compounding": "复利效应"
  },
  "corrections": [
    {"action": "correct_speaker", "index": 25, "to": "speaker_b", "reason": "延续24段的话"},
    {"action": "merge", "indices": [24, 25, 26], "speaker": "speaker_b", "reason": "同一人连续发言被误拆"},
    {"action": "split", "index": 1, "at_text": "Hey, Charlie.", "reason": "旁白和提问应分开"},
    {"action": "fix_text", "index": 98, "old": "Marbles.", "new": "Marvel's.", "reason": "ASR错误"}
  ]
}

转录稿：
{transcript_lines}
```

### 3.6.5 代码解析 + 合理性校验

```python
def apply_corrections(lines, corrections_json):
    """程序化执行 LLM 返回的修改指令，每条指令都做合理性校验"""
    for c in corrections_json.get("corrections", []):
        try:
            action = c["action"]
            if action == "correct_speaker":
                idx = c["index"]
                to = c["to"]
                if to not in {"speaker_a", "speaker_b"}:
                    log.warning(f"无效 speaker_id: {to}，跳过")
                    continue
                lines[idx].speaker_id = to

            elif action == "merge":
                indices = c["indices"]
                # 检查段间停顿：≥2s 的停顿是 Layer 1-3 有意拆开的，不合并
                has_long_pause = False
                for k in range(len(indices) - 1):
                    gap = lines[indices[k+1]].start_ms - lines[indices[k]].end_ms
                    if gap >= 2000:
                        log.warning(f"段 {indices[k]}→{indices[k+1]} 间有 {gap}ms 停顿，不合并")
                        has_long_pause = True
                        break
                if has_long_pause:
                    continue
                merged_duration = lines[indices[-1]].end_ms - lines[indices[0]].start_ms
                if merged_duration > 180_000:  # 合并后不能超过 180s
                    log.warning(f"合并后超过 180s ({merged_duration}ms)，跳过")
                    continue
                merge_lines(lines, indices, c.get("speaker"))

            elif action == "split":
                idx = c["index"]
                at_text = c["at_text"]
                if lines[idx].duration_ms < 15_000:  # 短于 15s 不拆
                    log.warning(f"段落 {idx} 仅 {lines[idx].duration_ms}ms，无需拆分")
                    continue
                split_line_at_text(lines, idx, at_text)

            elif action == "fix_text":
                idx = c["index"]
                old, new = c["old"], c["new"]
                # 编辑距离不超过原文 30%
                if edit_distance(old, new) / max(len(old), 1) > 0.3:
                    log.warning(f"修改幅度过大: '{old}' → '{new}'，跳过")
                    continue
                lines[idx].source_text = lines[idx].source_text.replace(old, new)

        except Exception as e:
            log.warning(f"跳过无效指令: {c}, 错误: {e}")
            continue

    # 最终兜底：确保没有超长段落
    final_lines = []
    for line in lines:
        if line.duration_ms > 180_000:
            # 找段内最长停顿机械拆分（复用 Layer 2/3 逻辑）
            split_result = split_at_longest_pause(line, words_data)
            final_lines.extend(split_result)
        else:
            final_lines.append(line)
    return final_lines
```

### 3.6.6 分批策略（超长视频）

```
≤200 行：一次搞定（音频 + 全文）
200-500 行：分 2 批（前半 + 后半，重叠 20 行上下文）
>500 行：分 3 批（每批含 10 行重叠上下文）
```

音频分批：按行的时间范围切割音频片段，每批传对应区间的音频。

### 3.6.7 成本估算

| 视频时长 | 文本 tokens | 音频 tokens | 输出 tokens | 总成本 (Flash Lite) |
|---------|-----------|-----------|-----------|-------------------|
| 30min | 6K | 45K | 2K | ¥0.12 |
| 1h | 12K | 90K | 3K | ¥0.22 |
| 102min | 18K | 153K | 3K | ¥0.35 |
| 3h | 30K | 270K | 4K | ¥0.65 |

对比原来 4 次纯文本调用（¥0.08），增加 ¥0.15-0.57，但审校质量显著提升（听音频分辨说话人 vs 纯猜）。

### 3.6.8 Fallback

```
Gemini 多模态调用失败
  → 降级为纯文本审校（不传音频，只传转录稿）
  → 再失败 → 跳过 LLM 审校，直接用机械拆分结果进入说话人审核
```

### 3.6.9 输出复用

审校输出的 `speakers`、`glossary`、`style` 直接传给翻译阶段：
- `glossary` → 翻译 prompt 的术语表（确保人名一致）
- `style` → 翻译 prompt 的风格指南（确保语气匹配）
- 无额外 API 调用成本

## 4. TTS 混合策略

### 4.1 策略选择

```python
def choose_tts_strategy(total_segments, video_duration_min):
    if video_duration_min <= 30 and total_segments <= 100:
        return SyncTTSStrategy(rpm_limit=20)
    else:
        return AsyncTTSStrategy(batch_by_speaker=True)
```

### 4.2 Tier 1：同步限速

```python
class SyncTTSStrategy:
    """≤30min 视频：逐段同步调用，自动限速 20 RPM"""

    def __init__(self, rpm_limit=20):
        self.min_interval = 60.0 / rpm_limit  # 3 秒/请求
        self.last_call_time = 0

    def process(self, segments):
        for seg in segments:
            if self.is_completed(seg):
                continue
            self._wait_for_rate_limit()
            audio = minimax_t2a_v2(seg.text, seg.voice_id)
            atomic_write(seg.output_path, audio)

    def _wait_for_rate_limit(self):
        elapsed = time.time() - self.last_call_time
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self.last_call_time = time.time()
```

### 4.3 Tier 2/3：异步批量

```python
class AsyncTTSStrategy:
    """>30min 视频：按 speaker 合并文本，提交异步任务"""

    def process(self, segments):
        # 1. 按 speaker 分组
        groups = group_by_speaker(segments)

        # 2. 过滤已完成的组
        pending_groups = {}
        for speaker_id, segs in groups.items():
            pending = [s for s in segs if not self.is_completed(s)]
            if pending:
                pending_groups[speaker_id] = pending

        if not pending_groups:
            return  # 全部已完成

        # 3. 每组合并文本，插入分段标记，提交异步任务
        tasks = []
        for speaker_id, segs in pending_groups.items():
            merged_text = self._merge_with_markers(segs)
            task_id = minimax_t2a_async_v2(
                text=merged_text,
                voice_id=speaker_voice_map[speaker_id],
                model="speech-2.8-turbo"
            )
            tasks.append(AsyncTask(task_id, speaker_id, segs))

        # 4. 轮询等待完成
        for task in tasks:
            audio_url = self._poll_until_done(task.task_id, interval=10)
            audio_data = download(audio_url)  # URL 9 小时有效
            self._split_and_save(audio_data, task.segs)

    def _merge_with_markers(self, segs):
        """合并文本并插入 SSML 标记用于后期切分"""
        parts = []
        for seg in segs:
            parts.append(f"[SEG:{seg.index}]{seg.text}")
        return "\n".join(parts)

    def _split_and_save(self, full_audio, segs):
        """按时间戳切分完整音频为各段，原子写入"""
        # 利用 MiniMax 返回的时间戳信息切分
        # 或使用 ffmpeg 按静音点切分
        for seg, audio_chunk in zip(segs, split_audio(full_audio)):
            atomic_write(seg.output_path, audio_chunk)

    def _poll_until_done(self, task_id, interval=10, max_wait=3600):
        """轮询异步任务状态"""
        start = time.time()
        while time.time() - start < max_wait:
            status = minimax_query_async_task(task_id)
            if status.state == "completed":
                return status.file_url
            if status.state == "failed":
                raise TTSError(f"异步 TTS 失败: {status.error}")
            time.sleep(interval)
        raise TTSError("异步 TTS 超时")
```

### 4.4 降级策略

```
异步 API 不可用 → 自动降级为同步限速模式
同步 API 限流 → 指数退避（5s, 10s, 20s, 40s, 60s）
连续失败 >5 次 → 暂停 5 分钟后重试
全部失败 → 保存 checkpoint，通知用户手动重试
```

## 5. 翻译优化

### 5.1 加大批次 + 并行

```python
# 优化后配置
BATCH_SIZE = 15          # 原来 5 → 现在 15
PARALLEL_WORKERS = 3     # 3 路并行

# 效果：
# 600 段 / 15 = 40 批
# 40 / 3 = 14 轮 × 15s = 3.5 分钟（原来 30 分钟）
```

### 5.2 上下文窗口传递

```python
def _build_prompt(self, batch, previous_results=None, glossary=None):
    parts = [self.translation_instructions]

    # 术语表（所有批次共享，触发 Deepseek prefix caching）
    if glossary:
        parts.append(f"术语表（请严格遵循）：\n{glossary}")

    # 最近翻译上下文
    if previous_results:
        recent = previous_results[-5:]
        context = "前文翻译参考（保持风格一致）：\n"
        for r in recent:
            context += f"  EN: {r.source[:60]}…\n  CN: {r.cn[:60]}…\n"
        parts.append(context)

    parts.append(f"请翻译以下段落：\n{format_batch(batch)}")
    return "\n\n".join(parts)
```

### 5.3 术语表自动提取

```python
def extract_glossary(first_batch_results):
    """首批翻译完成后，让 LLM 提取人名和专有术语的固定翻译"""
    prompt = f"""从以下翻译结果中提取所有人名和专有术语，输出 JSON：
    {format_results(first_batch_results)}

    输出格式：
    {{"Charlie Munger": "查理·芒格", "compounding": "复利效应", ...}}
    """
    glossary = llm.call(prompt)
    save_json("translation/glossary.json", glossary)
    return glossary
```

### 5.4 Deepseek Prefix Caching

通过固定 prompt 前缀（翻译指令 + 术语表），最大化 Deepseek 的缓存命中率：
- 正常价: $0.28/M tokens
- 缓存价: $0.028/M tokens（节省 90%）
- 120 批中第 2-120 批的前缀部分按缓存价计费

### 5.5 限流感知

```python
def _call_with_rate_awareness(self, prompt):
    for attempt in range(5):
        try:
            return self.llm.call(prompt)
        except RateLimitError:
            wait = min(60, 5 * (2 ** attempt))  # 5s, 10s, 20s, 40s, 60s
            log(f"限流退避 {wait}s（第 {attempt+1} 次）")
            time.sleep(wait)
    raise TranslationError("翻译 API 持续限流，请稍后重试")
```

## 6. Pipeline 超时改造

### 6.1 分层超时

```python
TIMEOUT_CONFIG = {
    "tier1": {
        "total": 2 * 3600,        # 2 小时
        "soft_warning": 0.8,       # 80% 时发送预警
    },
    "tier2": {
        "total": 6 * 3600,        # 6 小时
        "soft_warning": 0.8,
    },
    "tier3": {
        "total": 8 * 3600,        # 8 小时
        "soft_warning": 0.8,
    },
}
```

### 6.2 软超时预警

```python
def on_soft_timeout(project, elapsed, total):
    save_checkpoint(project)
    notify_user(
        title="任务处理时间较长",
        message=f"已处理 {elapsed//60} 分钟，预计还需 {(total-elapsed)//60} 分钟。进度已保存。",
        channel=["browser_push"]
    )
```

## 7. 音频处理优化

### 7.1 ffmpeg 流式转码（替代 pydub 内存加载）

```python
# 当前（危险）：
audio = AudioSegment.from_file(wav_path)  # 2GB 全部加载到内存
audio.export(mp3_path, format="mp3", ...)

# 优化（安全）：
subprocess.run([
    "ffmpeg", "-i", wav_path,
    "-ac", "1", "-ar", "16000", "-b:a", "64k",
    "-f", "mp3", mp3_path, "-y"
], check=True)
# 流式处理，内存 < 10MB
```

### 7.2 中间文件清理

```python
def cleanup_intermediate_files(project_dir, current_stage):
    """阶段完成后清理不再需要的中间文件"""
    if current_stage >= "tts":
        # 转录上传用的 MP3 不再需要
        safe_remove(project_dir / "audio/original_upload.mp3")

    if current_stage >= "output":
        # 单段 TTS 文件不再需要（已合并）
        # 保留 aligned 段用于调试
        pass
```

## 8. 前端改动

### 8.1 时长预警

```
输入视频时：
  ≤15min  → 无提示
  15-30min → 灰色提示"预计处理 15-30 分钟"
  30-60min → ⚠️ 黄色"长视频，预计 1-2 小时，完成后通知您"
  60-120min → ⚠️⚠️ 橙色"超长视频，预计 2-3 小时"
  120-180min → ⚠️⚠️⚠️ 红色"极长视频，预计 3-4 小时，建议分段"
  >180min → 🚫 拦截"超出最大支持时长（3 小时），请裁剪"
```

### 8.2 段级进度展示

```
工作区显示：
  转录中: 正在转录音频… (AssemblyAI)
  翻译中: 32/40 批 (80%) · 预计剩余 2 分钟
  TTS 中: 247/600 段 (41%) · 预计剩余 35 分钟
  对齐中: 180/600 段 (30%) · 预计剩余 25 分钟
```

### 8.3 进度上报机制

```python
# 后端每完成 N 段/批，更新 job 的 progress_message
def update_progress(job_id, stage, done, total):
    pct = int(done / total * 100)
    eta = estimate_remaining(done, total, start_time)
    message = f"{STAGE_LABELS[stage]}: {done}/{total} ({pct}%) · 预计剩余 {eta}"
    job_service.update_progress(job_id, message)
```

## 9. 通知机制

### 9.1 浏览器推送

```javascript
// 页面加载时请求权限
if ("Notification" in window && Notification.permission === "default") {
    Notification.requestPermission()
}

// 工作区轮询检测到状态变化
function onJobStatusChange(oldStatus, newStatus) {
    if (newStatus === "succeeded") {
        new Notification("任务完成", {
            body: `${jobTitle} 已完成，点击查看结果`,
            icon: "/ai-logo.png"
        })
    } else if (newStatus === "failed") {
        new Notification("任务失败", {
            body: `${jobTitle} 处理失败，点击查看详情`,
            icon: "/ai-logo.png"
        })
    }
}
```

### 9.2 邮件通知

```python
# Gateway 层新增端点
@app.post("/api/notifications/send")
async def send_notification(user_id, event_type, job_id):
    user = get_user(user_id)
    if not user.email:
        return

    template = EMAIL_TEMPLATES[event_type]
    workspace_url = f"https://us.aivideotrans.site/workspace/{job_id}"

    await send_email(
        to=user.email,
        subject=template.subject.format(job_title=job.title),
        html=template.render(job=job, url=workspace_url)
    )

# 触发时机
EMAIL_TRIGGERS = {
    "job_completed": "任务完成",
    "job_failed": "任务失败",
    "review_waiting_long": "审核等待超过 30 分钟",
}
```

### 9.3 邮件服务选型

推荐 **Resend**（$0/月起，每月 3000 封免费）：
- API 简洁：`resend.emails.send(from, to, subject, html)`
- 支持自定义域名
- 无需复杂配置

## 10. 磁盘空间管理

### 10.1 空间预检

```python
def check_disk_space_before_submit(video_duration_min):
    """提交前检查磁盘空间"""
    estimated_gb = video_duration_min * 0.035  # ~35 MB/分钟
    free_gb = get_free_disk_space_gb()

    if free_gb < estimated_gb * 1.5:  # 预留 50% 余量
        raise InsufficientDiskError(
            f"磁盘空间不足：需要约 {estimated_gb:.1f}GB，"
            f"当前可用 {free_gb:.1f}GB"
        )
```

### 10.2 阶段性清理

```python
CLEANUP_RULES = {
    "after_transcription": ["audio/original_upload.mp3"],
    "after_tts_complete": [],  # 保留 TTS 段用于对齐
    "after_output": ["audio/speech_for_asr.wav"],
    "after_download": [],  # 用户下载后可清理更多
}
```

### 10.3 自动过期清理（已实现）

项目 7 天后自动删除。每日定时任务扫描过期项目。

## 11. 多用户并发

### 11.1 任务队列优先级

```python
def calculate_priority(video_duration_min):
    """短视频优先处理"""
    if video_duration_min <= 15:
        return 10  # 最高优先级
    elif video_duration_min <= 30:
        return 7
    elif video_duration_min <= 60:
        return 5
    else:
        return 3   # 最低优先级
```

### 11.2 API 配额分配

```python
# 多用户共享 MiniMax 20 RPM
# 使用全局 rate limiter，而非每用户独立限速
GLOBAL_TTS_RATE_LIMITER = RateLimiter(rpm=20)

# 翻译 API 配额充裕（Deepseek 500 RPM），无需分配
```

## 12. 降级策略

```
MiniMax 异步 API 不可用
  → 降级为同步 + 限速 20 RPM
  → 通知用户"处理速度降低"

MiniMax 同步 API 也不可用
  → 暂停 TTS 阶段，保存 checkpoint
  → 通知用户"TTS 服务暂时不可用，已保存进度"
  → 后台每 5 分钟探测一次，恢复后自动继续

Deepseek 翻译 API 不可用
  → 降级为 Gemini Flash（已有 fallback 机制）

AssemblyAI 不可用
  → 无备选方案，直接报错
  → 未来可加 Whisper 本地转录作为 fallback
```

## 13. 未来扩展

### 13.1 自部署 TTS（中期）

当月处理量 >30 个视频时，可引入自部署 TTS：
- **推荐模型**: CosyVoice 2.0 或 Qwen3-TTS（Apache-2.0 商用友好）
- **硬件需求**: RTX 4090（16GB VRAM）或 A10 云 GPU
- **接口兼容**: TTS provider 抽象层已预留，只需实现 `synthesize()` 接口

### 13.2 任务队列（长期）

当前单 job 硬限制。未来需要：
- Redis/Celery 任务队列
- 多 worker 并行处理
- 优先级调度

## 14. 工程量估算

| 模块 | 工作量 | 优先级 |
|------|--------|--------|
| Pipeline 超时分层 | 2h | P0 |
| find_resume_point 恢复逻辑 | 4h | P0 |
| 原子写入改造 | 2h | P0 |
| TTS 同步限速 + checkpoint | 4h | P0 |
| 前端时长预警 | 3h | P0 |
| ffmpeg 流式转码 | 1h | P0 |
| 统一 LLM 审校（音频+diff） | 6h | P0 |
| **P0 小计** | **22h** | |
| TTS 异步模式（Tier 2/3） | 6h | P1 |
| 翻译并行化（3 路） | 3h | P1 |
| 翻译批次加大（5→15） | 1h | P1 |
| 上下文窗口 + 术语表 | 3h | P1 |
| 对齐 checkpoint | 3h | P1 |
| 段级进度上报 | 3h | P1 |
| 浏览器推送通知 | 3h | P1 |
| **P1 小计** | **22h** | |
| 邮件通知集成 | 4h | P2 |
| 磁盘空间预检 | 1h | P2 |
| 中间文件清理 | 2h | P2 |
| 多用户并发限流 | 3h | P2 |
| 测试（中断恢复场景） | 4h | P2 |
| **P2 小计** | **14h** | |
| **总计** | **~58h（8-10 天）** | |

## 15. 实施顺序

### Phase 1（P0，3-4 天）：基础稳定性 + 审校质量
1. 统一 LLM 转录审校（音频 + diff 模式，替代 4 次独立调用）
2. Pipeline 超时分层
3. find_resume_point + 原子写入
4. TTS 同步限速 + 段级 checkpoint
5. ffmpeg 流式转码
6. 前端时长预警

### Phase 2（P1，3-4 天）：长视频支持
1. TTS 异步模式
2. 翻译并行 + 加大批次
3. 术语表 + 风格指南（复用审校输出，工作量减少）
4. 对齐 checkpoint
5. 段级进度上报
6. 浏览器推送通知

### Phase 3（P2，2 天）：完善
1. 邮件通知
2. 磁盘管理
3. 多用户并发
4. 中断恢复测试
