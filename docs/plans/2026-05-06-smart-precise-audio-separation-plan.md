# 智能版精准音频分离方案

> 日期：2026-05-06
>
> 范围：仅面向智能版的高质量人声 / 背景音分离能力。
>
> 本文档整理了关于音频分离改造的讨论结论：在不阻塞主流程字幕提取、说话人识别、S2 审校的前提下，引入异步 AI 音频分离，用于更干净的音色克隆样本和更自然的最终背景音混合。

## 目标

为智能版任务生成两份可用音频：

- `speech`：更干净的人声 / 对话音轨，优先用于音色克隆样本提取。
- `ambient`：非人声背景音轨，尽量保留掌声、笑声、现场底噪、观众反应、环境音和背景音乐，用于最终合成时混回视频。

主流程必须保持快速和稳定。精准音频分离是质量增强能力，不应该成为 S1 转写和 S2 审校的新阻塞依赖。

## 非目标

- 不改变项目主交付目标：仍然以剪映草稿 / 编辑产物为主，不把直接渲染 MP4 变成核心目标。
- 不把字幕重定时、对齐或说话人审校逻辑迁移到音频分离模型里。
- 第一阶段不把精准分离默认开放给快速版或工作台版。
- 不把重模型依赖加入默认本地开发 / 默认测试路径。
- 单元测试不得依赖真实外部模型服务。

## 架构约束

- TTS 单元仍然是 `SemanticBlock`，不是字幕行。
- 对齐仍然是 DSP 优先，rewrite 只是 fallback。
- 字幕重定时仍然是确定性数学逻辑。
- Gateway 仍然是套餐、权益、能力开关的事实源。
- 前端可以展示能力状态和任务状态，但不能自行决定某个任务是否有精准音频分离权限。

## 当前真实流程

### 下载 / 上传后的音频提取

YouTube 下载后会提取双声道 `audio/original.wav`：

- `src/modules/ingestion/youtube/downloader.py`
- `_extract_audio()`
- ffmpeg 参数包含 `-ac 2 -ar 44100 -sample_fmt s16`

本地上传视频目前会提取单声道 `audio/original.wav`：

- `src/pipeline/process.py`
- `_extract_audio_from_video()`
- ffmpeg 参数目前是 `-ac 1`

本地上传视频强制单声道是一个质量问题。它会在音频分离前就丢掉立体声信息，对后续任何背景音分离都不利。

### 当前分离器

当前分离逻辑在：

- `src/services/audio/separator.py`
- `AudioStemSeparator`

当前输出：

- `audio/speech_for_asr.wav`
- `audio/ambient.wav`

当前双声道处理逻辑：

```text
speech  = 0.5 * FL + 0.5 * FR
ambient = (0.5 * FL - 0.5 * FR, 0.5 * FR - 0.5 * FL)
```

当前单声道处理逻辑：

```text
speech  = 原音频重采样为 16 kHz 单声道
ambient = 同时长静音双声道
```

这不是 AI 源分离，而是中置声估计加侧声道差分。它会天然削弱或删除很多居中的非人声内容，包括掌声、笑声、观众反应、房间底噪和音乐。对于单声道输入，背景音会直接变成静音。

### 当前下游消费者

S1 AssemblyAI 转写使用 `speech_for_asr.wav`：

- `src/pipeline/process.py`
- `transcriber.transcribe(str(speech_audio_path), ...)`

S2 审校和说话人分析使用原始音频：

- `src/pipeline/process.py`
- `review_transcript(..., audio_path=source_audio_path, ...)`

工作台里的音色克隆接口目前优先使用 `audio/speech_for_asr.wav`，没有则回退到 `audio/original.wav`：

- `gateway/voice_selection_api.py`
- 查找顺序：`audio/speech_for_asr.wav`，再 `audio/original.wav`

S4 保留原音片段使用 `source_audio_path`：

- `src/pipeline/process.py`
- `_materialize_keep_original_segments(...)`

S6 输出阶段才真正需要背景音：

- `src/modules/output/output_dispatcher.py`
- 发布时取 `editor.ambient_audio` 或 `working.ambient_audio`
- `src/modules/output/publish/video_renderer.py`
- 当前发布混音默认会把背景音降 `-12.0 dB`

剪映草稿会把背景音作为独立轨道加入：

- `src/modules/output/jianying/jianying_draft_writer.py`
- 当前背景音轨音量是 `0.3`

## 产品决策

第一阶段，精准音频分离只给智能版使用。

```text
快速版 Express:
  precise_audio_separation = false
  只使用当前快速分离 / fallback

工作台版 Studio:
  precise_audio_separation = false by default
  保持可编辑、可审校、可重新生成的工作流
  后续可考虑单次加购或管理员灰度

智能版 Smart:
  precise_audio_separation = true
  original.wav 生成后异步启动 AI 分离
  AI speech 优先用于音色克隆
  AI ambient 用于最终合成
```

理由：

- 精准分离是 GPU / CPU 重资源能力。
- 它会引入排队、重试、模型缓存、临时文件和失败恢复复杂度。
- 作为智能版核心质量能力，更容易定价和解释。
- 工作台版可以继续强调人工可控、可编辑、可审校。
- 智能版强调自动高质量增强，定位更清晰。

Gateway 应在任务创建时把能力快照写进 job。pipeline 读取任务快照，而不是读取用户当前实时套餐，避免任务中途套餐变化导致行为漂移。

建议快照字段：

```json
{
  "features": {
    "precise_audio_separation": true,
    "ai_background_stem": true,
    "ai_voice_clone_source": true
  }
}
```

## 建议的新流程

### 总体流程

```text
S0 下载 / 上传
  -> audio/original.wav

快速音频准备，同步执行：
  -> audio/speech_for_asr.wav
  -> audio/ambient.wav

智能版专属，异步执行：
  -> audio/stems_ai/speech.wav
  -> audio/stems_ai/ambient.wav
  -> audio/stems_ai/separation_manifest.json

S1 转写：
  使用 audio/speech_for_asr.wav

S2 审校：
  使用 audio/original.wav

音色克隆：
  如果 AI speech 已完成，优先使用 audio/stems_ai/speech.wav
  未完成则按策略短暂等待或回退到 audio/speech_for_asr.wav

S6 输出：
  如果 AI ambient 已完成，优先使用 audio/stems_ai/ambient.wav
  未完成则按智能版策略等待、暂停或回退
```

### 为什么要并行执行 AI 分离

AI 分离不是 S1 / S2 正确性的硬依赖：

- S1 已经有快速生成的 ASR 用音频。
- S2 当前本来就使用原始音频。
- 用户在 S2 审校、音色选择、人工确认期间，本身会产生一段等待时间，适合让 AI 分离在后台并行跑。

真正强依赖精准分离的消费点在后面：

- 音色克隆样本更适合使用干净人声。
- 最终视频合成更适合使用干净背景音。

因此，精准分离应该在 `audio/original.wav` 出现后立即启动，但不应该阻塞 S1 / S2。

## 产物契约

保留现有文件不变：

```text
audio/original.wav
audio/speech_for_asr.wav
audio/ambient.wav
```

新增精准分离产物：

```text
audio/stems_ai/speech.wav
audio/stems_ai/ambient.wav
audio/stems_ai/separation_manifest.json
audio/stems_ai/status.json
```

第一阶段不要直接覆盖 `audio/speech_for_asr.wav` 或 `audio/ambient.wav`。AI 输出应先写入 `audio/stems_ai/`，通过校验后由后续消费者显式优先选择。

### `separation_manifest.json`

建议字段：

```json
{
  "schema_version": 1,
  "backend": "audio_separator",
  "model_name": "model_bs_roformer_ep_317_sdr_12.9755.ckpt",
  "backend_version": "x.y.z",
  "source_audio_path": "audio/original.wav",
  "source_sha256": "...",
  "source_duration_ms": 2780000,
  "chunk_seconds": 600,
  "device": "cuda",
  "started_at": "2026-05-06T00:00:00Z",
  "completed_at": "2026-05-06T00:10:00Z",
  "outputs": {
    "speech": "audio/stems_ai/speech.wav",
    "ambient": "audio/stems_ai/ambient.wav"
  },
  "quality": {
    "speech_rms_dbfs": -21.4,
    "ambient_rms_dbfs": -28.7,
    "ambient_non_silent_ratio": 0.64,
    "warnings": []
  }
}
```

当前缓存判断只比较 mtime。对于模型分离来说这不够，因为模型名、模型版本、chunk 参数、源音频 hash 任意变化，都应该让旧分离结果失效。

## AI 后端选择

第一版推荐后端：

- `audio-separator`
- GitHub: `https://github.com/nomadkaraoke/python-audio-separator`

理由：

- 同时支持 Python API 和 CLI。
- 封装了 UVR 系列 vocal / instrumental 分离模型。
- 支持模型缓存和 chunk 处理。
- 两轨输出可以自然映射到本项目的人声 / 背景音需求。

初始映射：

```text
Vocals       -> audio/stems_ai/speech.wav
Instrumental -> audio/stems_ai/ambient.wav
```

候选模型方向：

- RoFormer / MDXC 两轨 vocal-instrumental 模型。
- 第一版不要使用 ensemble。
- 当前 4 核美国主机上不要默认使用高内存 Demucs fine-tuned 模式。

fallback 后端：

- 当前 `AudioStemSeparator` 的 ffmpeg center / side 实现。

备选后端：

- Demucs `--two-stems=vocals`
- GitHub: `https://github.com/facebookresearch/demucs`
- 可作为实验备选，但不建议第一版默认使用。Demucs 更偏音乐源分离，而且官方仓库已归档，维护风险更高。

后续研究方向：

- AudioSep 这类文本条件分离模型，可用于显式提取 `applause`、`laughter`、`room tone`、`music` 等类别。但第一阶段不建议作为生产依赖。

## 服务器和资源策略

### 现有美国主机

已查看的当前配置：

```text
CPU: 4 vCPU AMD EPYC
内存: 7.6 GiB
Swap: 无
GPU: 无
磁盘: 150 GiB，总体约剩 49 GiB
```

这台主机可以做有限 CPU 试点，但不适合多个 AI 分离任务并发。

如果临时在现有主机试点：

```text
AVT_PRECISE_AUDIO_SEPARATOR_MAX_CONCURRENT=1
AVT_PRECISE_AUDIO_SEPARATOR_DEVICE=cpu
AVT_PRECISE_AUDIO_SEPARATOR_CHUNK_SECONDS=300-600
AVT_PRECISE_AUDIO_SEPARATOR_TIMEOUT_SECONDS=7200
```

风险：

- AI 分离长时间占满 CPU，会拖慢 app / gateway / Next。
- 无 swap，模型推理和长音频处理中有 OOM 风险。
- 长视频会产生大量 wav / stem / 临时 chunk 文件，可能消耗磁盘。
- 45-180 分钟视频在 CPU 上耗时不可控。

### 推荐生产形态

使用独立的按需音频分离 worker：

```text
主站:
  负责 job 状态、权益、项目元数据

共享存储:
  持久化 original.wav 和 stems

音频分离 worker:
  下载 original.wav
  运行 AI separator
  上传 speech.wav / ambient.wav / manifest
  回写状态
```

部署选项：

1. Serverless GPU worker
   - 第一版生产更推荐。
   - 空闲时 scale to zero。
   - 冷启动可以接受，但 UI 必须明确显示进度。

2. 按量 GPU VM
   - 对长视频控制力更强。
   - 需要维护生命周期自动化：启动、健康检查、空闲关机、失败恢复、驱动、模型缓存。

最低合理 GPU worker 配置：

```text
8 vCPU
32 GiB RAM
NVIDIA T4 或 L4 级别 GPU
200 GiB SSD 或等价临时存储
持久化模型缓存
```

CPU worker fallback 配置：

```text
8 vCPU
16-32 GiB RAM
200 GiB SSD
精准分离全局并发 = 1
```

## 长视频处理

长视频必须 chunk 处理。不能把完整音频读进 Python 内存。

初始策略：

```text
<= 30 分钟:
  chunk_seconds = 600

30-120 分钟:
  chunk_seconds = 600

> 120 分钟:
  chunk_seconds = 300
```

最终值需要基于实测调整。

临时文件目录建议：

```text
audio/stems_ai/tmp/
```

成功或失败后应清理临时目录。失败任务只保留简洁日志和状态元数据，不保留完整中间 chunk。

## 各阶段语义

### S0

先修本地视频音频提取：

```text
把 -ac 1 改成 -ac 2
```

这是精准分离前的必要修复，避免本地上传视频提前损失立体声信息。

然后同步运行现有快速音频准备：

```text
AudioStemSeparator -> speech_for_asr.wav / ambient.wav
```

如果是智能版任务，在 `original.wav` 存在后立即提交精准分离后台任务。

### S1

不依赖精准分离。

继续使用：

```text
audio/speech_for_asr.wav
```

这样可以保持转写速度，避免 ASR 等待重模型。

### S2

不硬依赖精准分离。

继续使用：

```text
audio/original.wav
```

这与当前 `review_transcript(..., audio_path=source_audio_path)` 行为一致。

### 音色选择 / 音色克隆

智能版策略：

```text
如果 audio/stems_ai/speech.wav 已完成:
  用它拼接所选说话人的克隆样本
否则如果精准分离正在运行:
  等待一个可配置的短超时
否则:
  按策略回退到 audio/speech_for_asr.wav
```

建议超时：

```text
AVT_SMART_CLONE_WAIT_FOR_PRECISE_SPEECH_SECONDS=180
```

如果用户在精准人声还没完成时手动触发克隆，UI 应提示“高质量音色样本仍在准备中”。

Gateway 克隆源查找应改为能力感知：

```text
Smart:
  audio/stems_ai/speech.wav
  audio/speech_for_asr.wav
  audio/original.wav

非 Smart:
  audio/speech_for_asr.wav
  audio/original.wav
```

### S4 保留原音片段

继续使用 `source_audio_path`。这些片段是用户或规则明确要保留原声音频，不应该被 AI speech 替换。

### S6 输出

智能版策略：

```text
如果 audio/stems_ai/ambient.wav 已完成并校验通过:
  用它进行编辑包和最终发布混音
否则如果精准分离仍在运行:
  按智能版策略等待或暂停最终输出
否则:
  仅在策略允许时回退到 audio/ambient.wav
```

第一版智能版推荐：

```text
最终视频等待 AI ambient 完成
等待期间明确显示“高质量背景音分离中”
只有超时或失败时才回退，并在 manifest/status 中写明 warning
```

快速版 / 工作台版继续使用当前快速 `audio/ambient.wav`。

## 背景音响度和混音策略

不要对所有背景音无脑固定降音量。

当前行为：

- 发布视频默认对背景音应用 `ambient_volume_db = -12.0 dB`
- 剪映草稿背景音轨固定 `volume = 0.3`

AI ambient 应该走分析型增益：

1. 测原视频非说话 / 背景段响度。
2. 测 `stems_ai/ambient.wav` 响度。
3. 如果背景音已经在舒适范围内，不调整。
4. 如果偏小，提升到舒适下限。
5. 如果偏大或有明显人声泄漏，降低。
6. 最终发布混音加 true-peak limiter，避免爆音。

初始行为目标：

```text
正常背景音:
  不调整

偏小背景音:
  提升到舒适下限

偏大背景音:
  降低，避免压住配音

人声泄漏明显的背景音:
  说话区间 duck 或触发 fallback / warning
```

剪映草稿里建议让 AI ambient 更接近中性音量，因为用户可手动调节。最终渲染发布视频可以使用更保守的 limiter。

## 队列和状态

精准分离应该作为独立异步任务，而不是隐藏在 S0 里的同步 subprocess。

最小本地状态文件：

```json
{
  "status": "queued|running|succeeded|failed|timed_out|fallback_used",
  "progress": 0.0,
  "message": "separating audio",
  "backend": "audio_separator",
  "model_name": "...",
  "started_at": "...",
  "updated_at": "...",
  "error": ""
}
```

后续推荐：

- 持久化为 background task 或 job subtask。
- 写 job events，前端可展示进度。
- app / container 重启后可恢复。
- worker 以 source hash 和 manifest 实现幂等。

## 失败策略

失败不能污染快速流程产物。

失败场景：

- 模型下载失败。
- GPU / CPU worker 不可用。
- 超时。
- OOM。
- 输出为空或不可用。
- 输出时长与源音频不匹配。
- ambient 中人声泄漏明显。

fallback 规则：

```text
Express:
  没有精准分离路径

Studio:
  默认没有精准分离路径

Smart:
  worker 临时失败时重试一次
  仍失败时二选一：
    a) 暂停最终输出，提示用户 / 管理员
    b) 回退到快速 ambient，但必须有明确 warning
```

第一阶段智能版建议不要静默回退。即使回退，也要在状态和 manifest 中明确写出来。

## 质量校验

AI 输出在使用前必须校验：

- speech 文件存在且非空。
- ambient 文件存在且非空。
- 两个输出时长与 `original.wav` 基本一致。
- speech RMS 不能接近静音。
- ambient 非静音比例合理。
- 没有明显 clipping。
- 可选：对 ambient 做 VAD / ASR 人声泄漏评分。

建议时长容差：

```text
abs(output_duration_ms - source_duration_ms) <= max(500 ms, source_duration_ms * 0.001)
```

建议 warnings：

```text
speech_too_quiet
ambient_too_quiet
ambient_probably_silent
duration_mismatch
speech_leakage_suspected
model_fallback_used
```

## 配置项

建议环境变量：

```text
AVT_PRECISE_AUDIO_SEPARATOR_ENABLED=0|1
AVT_PRECISE_AUDIO_SEPARATOR_BACKEND=audio_separator|remote|disabled
AVT_PRECISE_AUDIO_SEPARATOR_MODEL=model_bs_roformer_ep_317_sdr_12.9755.ckpt
AVT_PRECISE_AUDIO_SEPARATOR_MODEL_DIR=/opt/aivideotrans/model_cache/audio-separator
AVT_PRECISE_AUDIO_SEPARATOR_DEVICE=cpu|cuda|auto
AVT_PRECISE_AUDIO_SEPARATOR_CHUNK_SECONDS=600
AVT_PRECISE_AUDIO_SEPARATOR_MAX_CONCURRENT=1
AVT_PRECISE_AUDIO_SEPARATOR_TIMEOUT_SECONDS=7200
AVT_SMART_CLONE_WAIT_FOR_PRECISE_SPEECH_SECONDS=180
AVT_SMART_OUTPUT_WAIT_FOR_PRECISE_AMBIENT_SECONDS=7200
AVT_PRECISE_AUDIO_SEPARATOR_FALLBACK=ffmpeg_center
```

依赖必须可选：

```toml
[project.optional-dependencies]
audio-separation = [
  "audio-separator==..."
]
```

默认本地安装和默认测试不安装模型依赖。

## 实施计划

### Phase 0：保留源音频质量

- [ ] 将本地视频音频提取从单声道改为双声道。
- [ ] 增加回归测试，确认 `_extract_audio_from_video()` 使用 `-ac 2`。
- [ ] YouTube 双声道提取保持不变。

涉及文件：

- `src/pipeline/process.py`
- 对应测试文件

### Phase 1：拆分快速准备和精准准备

- [ ] 保留当前 `AudioStemSeparator`，将其定位为快速 / fallback 分离器。
- [ ] 增加小型 separator backend strategy。
- [ ] 保持同步快速产物稳定：
  - `audio/speech_for_asr.wav`
  - `audio/ambient.wav`
- [ ] 新增 `audio/stems_ai/` 产物布局。
- [ ] 新增 manifest / status 写入和校验。

涉及文件：

- `src/services/audio/separator.py`
- 新增 `src/services/audio/precise_separator.py`
- 新增 `src/services/audio/separation_manifest.py`
- manifest / cache invalidation 测试

### Phase 2：智能版权益快照

- [ ] Gateway 增加智能版精准音频分离能力字段。
- [ ] 任务创建时写入 feature snapshot。
- [ ] pipeline 读取 job snapshot。
- [ ] 前端只展示后端下发的 capability / status，不自行定义权益。

涉及文件：

- `gateway/plan_catalog.py`
- `gateway/entitlements.py`
- job 创建 / intercept 路径
- pipeline config / job snapshot 读取逻辑

### Phase 3：本地 / 远程任务编排

- [ ] 新增精准分离任务 launcher。
- [ ] 支持本地 CPU backend，供开发和管理员试点。
- [ ] 支持 remote worker 模式，供生产使用。
- [ ] 增加全局并发锁。
- [ ] 增加 timeout / retry / fallback 语义。
- [ ] 写 job events / status。

涉及文件：

- 新增 `src/services/audio/precise_separation_task.py`
- job event / status 集成
- 可选 gateway background task 集成

### Phase 4：`audio-separator` 后端

- [ ] 增加可选依赖 extra。
- [ ] 实现 `AudioSeparatorBackend`。
- [ ] 统一输出格式：
  - speech：用于克隆时建议 16 kHz mono s16 WAV
  - ambient：44.1 kHz stereo s16 WAV
- [ ] 处理 chunk 和临时文件清理。
- [ ] 校验时长和响度。
- [ ] 写 manifest。

### Phase 5：音色克隆消费方

- [ ] Smart 下更新克隆源查找顺序：
  - `audio/stems_ai/speech.wav`
  - `audio/speech_for_asr.wav`
  - `audio/original.wav`
- [ ] Smart 下增加“等待精准 speech”的策略。
- [ ] 在 voice-selection UI / API 中展示状态。
- [ ] 非 Smart 行为保持不变。

涉及文件：

- `gateway/voice_selection_api.py`
- voice-selection 前端状态展示

### Phase 6：输出消费方

- [ ] Smart S6 优先使用 `audio/stems_ai/ambient.wav`。
- [ ] 增加 Smart 输出等待 / 暂停 / fallback 行为。
- [ ] 对 AI ambient 使用响度分析，不再固定无脑压低。
- [ ] 非 Smart 继续使用现有 fallback ambient。

涉及文件：

- `src/pipeline/process.py`
- `src/modules/output/output_dispatcher.py`
- `src/modules/output/publish/video_renderer.py`
- `src/modules/output/jianying/jianying_draft_writer.py`

### Phase 7：生产 worker

- [ ] 打包 GPU worker 镜像，配置模型缓存路径。
- [ ] 定义 remote worker API contract。
- [ ] 使用 R2 / S3 或共享存储传递 `original.wav` 和 stems。
- [ ] 实现空闲关机或 serverless scale-to-zero。
- [ ] 增加管理端指标：
  - 排队时间
  - 处理耗时
  - 模型名
  - 视频时长
  - 成功 / 失败
  - GPU / CPU 运行成本估算

## 测试策略

单元测试：

- 本地视频提取保留双声道。
- 快速分离输出契约不变。
- precise manifest 在源 hash / 模型 / 版本 / 参数变化时失效。
- Smart 权益会提交精准分离任务。
- 非 Smart 权益不会提交精准分离任务。
- 克隆源选择只在 Smart 下优先 AI speech。
- S6 ambient 选择只在 Smart 下优先 AI ambient。
- fallback 路径保持现有行为。

fake 集成测试：

- fake precise separator 写出有效 stems。
- fake precise separator 失败并写出 fallback 状态。
- 长视频 chunk 计划不加载整段音频。
- precise task 运行中时 S1 可以继续。
- S6 按策略等待 AI ambient 或 fallback。

人工 benchmark 样本：

- 有掌声 / 笑声的访谈。
- 有观众反应的会议演讲。
- 两人 panel discussion。
- 单声道上传。
- 45 分钟视频。
- 120 分钟视频。

成功标准：

- Smart ambient 比当前 `L-R` 更好地保留掌声、笑声、音乐和环境声。
- 克隆样本比当前 `speech_for_asr.wav` 背景泄漏更少。
- S1 / S2 不因为精准分离并行运行而变慢。
- 快速版 / 工作台版行为不变。
- 精准分离任务运行时主站仍然健康。

## 发布计划

1. 落地 Phase 0 和 Phase 1，默认关闭。
2. 加 fake backend 测试和 manifest / status 管道。
3. 在管理员任务上跑本地 CPU 试点。
4. 部署到美国主机，但用户侧保持关闭。
5. 对少量 Smart allowlist 开启。
6. 测试质量和耗时。
7. 将生产精准分离迁移到按需 GPU worker。
8. 对 Smart 普遍开放。
9. 观察成本和队列后，再决定是否给 Studio 做单次加购。

## 待定问题

- 哪个 `audio-separator` 模型最适合访谈 / 会议类视频的人声和背景保留？
- Smart 最终输出应该无限等待精准 ambient，还是在可见超时后 fallback？
- Studio 后续是否提供单次加购，还是让精准分离保持智能版专属？
- 按需 GPU / serverless worker 的冷启动等待多久可接受？
- 第一阶段 AI speech 是否只用于音色克隆，还是也可以替代 `speech_for_asr.wav` 做 ASR？

## 第一版推荐落地范围

第一版做最小安全实现：

```text
1. 本地上传视频保留双声道
2. 精准分离仅智能版可用，默认仍关闭
3. 精准分离只写 audio/stems_ai/*，不覆盖快速产物
4. fake backend 先打通异步状态和消费方逻辑
5. CPU backend 仅管理员试点，concurrency=1
6. 生产 Smart 正式开放前迁移到独立 GPU worker
```

这样可以在不影响现有主流程的前提下，为更好的背景音和更干净的克隆样本建立清晰、可控、可回滚的升级路径。
