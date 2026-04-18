# 豆包 TTS 双模式改造计划（含 LLM Review 前置提质）

> 说明：当前仓库已经完成 `volcengine` 单 provider 的 V3 HTTP Chunked 接入。本计划把工作拆成两块：先提升 LLM review 产出的 `speaker_styles` 质量，再在不拆分 provider 名称的前提下，把 `express` 路由到豆包 1.0，把 `studio` 路由到豆包 2.0，并把 `resource_id / req_params.model / speaker` 三者语义彻底拆开。

## 1. 目标

- 先做 `Block A`：提升 review 阶段音频输入、prompt 和 speaker profiling 质量
- 再做 `Block B`：完成 VolcEngine 双模式、自动匹配和 `frontend-next` 用户侧 Studio 音色选择
- 保持 `volcengine` 作为单一 provider 名称
- `express` 使用豆包 1.0：
  - `resource_id = "seed-tts-1.0"`
  - `req_params.model = "seed-tts-1.1"`
  - `speaker` 由应用层自动匹配
- `studio` 使用豆包 2.0：
  - `resource_id = "seed-tts-2.0"`
  - 公版 2.0 默认不传 `req_params.model`
  - `speaker` 以工作区 `voice_review` 阶段的用户下拉框选择为主，支持“自动匹配”模式
  - 用户必须为每个 speaker 显式选择“具体音色”或“自动匹配”，不允许留空跳过
- 不改现有 `Gateway -> Job snapshot -> Pipeline -> TTSGenerator` 总体链路
- 音色匹配能力做成可复用框架，但本轮先只让 VolcEngine 接入共享 resolver 主路径

## 2. 范围控制与复杂度约束

为了避免 scope 膨胀，本次明确采用以下收窄策略：

- `Block A` 正式并入计划，且优先于 `Block B`
- `Block B` 采用简化版：
  - 先不新增 `tts_resource_id` 数据库字段
  - 先不做 DB migration
  - `resource_id` 由 Generator 按 `tts_provider + service_mode` 推导
- 共享 matcher 框架可以保留，但这次先只让 VolcEngine 接入
- CosyVoice 保持现有主路径，不在本次强迁到共享 resolver
- 豆包 1.0 catalog 先做 `20-30` 个核心音色子集验证 matcher 框架
- 不在本次接入声音复刻 2.0 的 `standard / expressive` 前端切换
- 不修改旧 `frontend/*`，唯一有效前端仍然是 `frontend-next`

## 3. 已确认前提

- `resource_id` 已实测可用，本计划直接使用：
  - `seed-tts-1.0`
  - `seed-tts-2.0`
- 仍然使用同一个 V3 endpoint：
  - `POST https://openspeech.bytedance.com/api/v3/tts/unidirectional`
- 仍然请求 `pcm`，本地封装成 `wav`
- 当前代码和官方 demo 都未传 `req_params.model`
- 官方文档已确认：
  - `seed-tts-1.1` 相对默认版本音质更好、延时更优
  - `seed-tts-2.0-expressive / seed-tts-2.0-standard` 仅对声音复刻 2.0 的音色生效
  - 公版 2.0 音色当前先不引入 `standard / expressive`
- 当前控制台可见音色数量大致为：
  - 豆包 1.0：300+ 公版音色
  - 豆包 2.0：30+ 公版音色
  - 上述数字以控制台当前实际可见列表为准，不在代码中硬编码为永久常量

参考资料：

- [VolcEngine V3 单向流式文档](https://www.volcengine.com/docs/6561/1598757?lang=zh)
- `C:/Users/Administrator/Desktop/HTTP Chunked SSE单向流式-V3.docx`
- `C:/Users/Administrator/Desktop/tts_http_demo.py`
- `C:/Users/Administrator/Desktop/tts_http_sse_demo.py`
- [Gemini Pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [Gemini Context Caching](https://ai.google.dev/gemini-api/docs/caching)
- [Gemini Token Counting](https://ai.google.dev/gemini-api/docs/tokens)

## 4. Block A：LLM Review 提质

### 4.1 目标

本块优先解决当前 review 产出对音色匹配不友好的几个问题：

- 大音频时退化为 text-only
- batched review 只有第一批带音频
- prompt 把“听音频”写死
- unified review 全失败时 `_review_speaker_styles` 过于空
- 同一说话人不同 segment 可能因为 matcher 抖动而选到不同音色

### 4.2 Review 音频输入策略

review 阶段的音频策略固定如下：

- `<= 20 分钟`
  - 使用整段压缩音频
  - 优先尝试对同一段音频做一次 explicit caching
  - 每个 review batch 引用同一个 cached audio
  - 若音频 token 数不足 explicit caching 最低门槛，或 cache 创建失败，则回退为“复用同一个压缩音频文件”
- `20 分钟 - 3 小时`
  - 不再让每个 batch 重复引用整段音频
  - 统一改为每批只携带对应时间窗的压缩音频 clip
  - clip 按 transcript batch 的时间范围生成，并加固定前后 padding
  - 推荐 padding：`±10 秒`

补充约束：

- 不把 implicit caching 当作成本优化前提
- 只有在“整段音频被多个 batch 重复使用”时，才引入 explicit caching
- `20 分钟 - 3 小时` 区间优先控制重复音频 token，而不是强行复用整段音频 cache
- `> 3 小时` 的 review 音频策略暂不在本计划中锁死；若后续进入该区间，优先延续 batch-local clip 思路单独评估

### 4.3 设计要求

#### A1. 音频预处理

文件：

- `src/services/transcript_reviewer.py`

新增：

```python
def _prepare_review_audio(audio_path: Path, tmp_dir: Path) -> Path: ...
```

要求：

- 输入原始音频，输出压缩后的 review 音频
- 优先统一生成 `16kHz mono` 的压缩格式
- 尽量复用项目内现有 `ffmpeg`
- 压缩失败时打 `WARNING`，但仍可回退到原始音频或后续 text-only prompt

#### A2. 去掉 `200MB` 阈值和默认 text-only 主路径

文件：

- `src/services/transcript_reviewer.py`

要求：

- 不再以 `200MB` 作为“是否带音频”的主判断条件
- 主路径改成“优先带音频”
- 音频失败的兜底顺序为：
  1. 尝试更激进压缩
  2. 若仍失败，再走 text-only prompt
- 不允许静默吞掉音频失败并让 `_review_speaker_styles` 默默变空

#### A3. Review 模型配置与映射

文件：

- `src/services/transcript_reviewer.py`
- `gateway/admin_settings.py`
- `frontend-next/src/components/workspace/VoiceReviewPanel.tsx`
- `frontend-next/src/components/workspace/index.ts`
- `frontend-next/src/app/workspace/[jobId]/page.tsx`
- `frontend-next/src/lib/api/reviews.ts`
- `frontend-next/src/types/reviews.ts`

要求：

- 管理后台继续暴露逻辑选项，例如：
  - `gemini_pro`
  - `gemini`
  - `mimo_omni`
- 实际 API model id 必须集中收敛到单一 `_MODEL_MAP`
- 不允许在多处散落硬编码 raw model id
- 不直接把 `gemini-3.1-pro` 这种未统一验证的字符串写死到多处
- 管理后台 label 与真实 API model id 明确分层，不混用

实现约束：

- 默认后台选项可设为 `gemini_pro`
- `_MODEL_MAP` 中使用“实现当日经官方文档核实的 Gemini Pro / Flash Lite 实际 model id”
- 若 Gemini Pro 当前公开 id 发生变动，只改 `_MODEL_MAP`，不改其他调用层

#### A4. Prompt 双版本

文件：

- `src/services/transcript_reviewer.py`

要求：

- 拆成两套 prompt：
  - 有音频版
  - 无音频版
- 有音频版继续强调“听音频 + 对照转录稿”
- 无音频版不能再写“听音频”
- 两版都要明确尽量产出：
  - `gender`
  - `age_group`
  - `voice_description`

#### A5. Batched review 的音频复用策略

文件：

- `src/services/transcript_reviewer.py`

要求：

- 不再简单写成“每批重新上传一次同一音频文件”
- 优先级如下：
  1. 压缩一次，复用同一个压缩产物
  2. 若 SDK 支持，优先复用上传后的 file handle / cached audio 引用
  3. 若不方便复用句柄，再退回复用同一个压缩音频文件
- 与 `4.2` 时长策略联动：
  - `<= 20 分钟`：整段音频 + explicit caching（失败时回退为复用同一压缩文件）
  - `20 分钟 - 3 小时`：batch-local clip
- 对 `batch-local clip`：
  - clip 时间窗由当前 batch 第一行 `start_ms` 与最后一行 `end_ms` 推导
  - clip 范围为 `[first.start_ms - 10s, last.end_ms + 10s]`
  - ffmpeg 可一步完成截取与压缩

#### A6. Legacy fallback 最小 speaker profiling

文件：

- `src/pipeline/process.py`

要求：

- 只在 unified review 完全失败时触发
- 只补最基础字段：
  - `gender`
  - `age_group`
- 只补缺失字段，不覆盖已有 review 输出
- 最好带低置信度来源标记，表明这是规则兜底而非高质量 review 结论
- 不做“聪明的人名库推断系统”，只做最小可用兜底

#### A7. Generator 层 speaker 级缓存

文件：

- `src/services/tts/tts_generator.py`
- `tests/test_tts_generator.py`

要求：

- 在 Generator 中维护 speaker 级缓存，保证同一说话人跨 segment 的自动匹配结果稳定
- speaker cache 只缓存“自动匹配得到的音色”
- 不覆盖以下情况：
  - review 或前端手选的显式音色
  - `segment.voice_id`

### 4.4 Block A 文件范围

修改文件：

- `src/services/transcript_reviewer.py`
- `tests/test_transcript_reviewer.py`
- `gateway/admin_settings.py`
- `frontend-next/src/app/admin/settings/page.tsx`
- `src/pipeline/process.py`
- `src/services/tts/tts_generator.py`
- `tests/test_tts_generator.py`

### 4.5 Block A 验证

建议定向验证：

```bash
python -m pytest tests/test_transcript_reviewer.py -v
python -m pytest tests/test_tts_generator.py -v -k "voice or volcengine or cosyvoice"
```

测试要求：

- 测试中只用 mocks / stubs，不打真实外网
- 覆盖：
  - review 音频预处理
  - audio-first fallback 顺序
  - 有音频 / 无音频 prompt 切换
  - batched review 的音频输入策略
  - legacy fallback 只补缺失字段
  - speaker cache 不覆盖显式音色

## 5. Block B：VolcEngine 双模式改造（简化版）

### 5.1 核心收敛

本块采用简化版方案：

- 不新增 `tts_resource_id` 数据库字段
- 不改 DB schema
- `resource_id` 作为运行时语义，由 Generator 根据 `tts_provider + service_mode` 推导
- `tts_model` 仅在 `volcengine` 场景下承载 `req_params.model`
- 共享 matcher 框架先只接 VolcEngine 主路径
- CosyVoice 先保持现有 selector 调用路径不变

### 5.2 三者分离语义

VolcEngine 接入明确拆成三层：

1. `resource_id`
   - 运行时值
   - 写入请求头 `X-Api-Resource-Id`
2. `req_params.model`
   - 写入请求体 `req_params.model`
   - 当前仅 `express` 的 1.0 公版路径使用 `seed-tts-1.1`
3. `speaker`
   - 写入请求体 `req_params.speaker`
   - 由显式选择或 matcher 决定

约束：

- `resource_id` 与 `req_params.model` 不是一回事
- `speaker` 与 `req_params.model` 也不是一回事
- 即便这次不把 `resource_id` 落库，代码和测试仍必须把这三层语义分开命名

### 5.3 任务模式映射

当 `tts_provider == "volcengine"` 时：

- `express`
  - 运行时 `resource_id = "seed-tts-1.0"`
  - `tts_model = "seed-tts-1.1"`
  - `voice_clone_enabled = False`
- `studio`
  - 运行时 `resource_id = "seed-tts-2.0"`
  - `tts_model = None` 或空值
  - `voice_clone_enabled = False`

当 `tts_provider != "volcengine"` 时：

- 保持当前 `tts_model` 语义不变
- MiniMax 仍使用 `speech-2.8-*`
- CosyVoice 仍使用 `cosyvoice-v3-flash`

补充说明：

- `tts_model` 在不同 provider 下语义不同，执行时必须在代码注释中明确：
  - `minimax`：MiniMax 模型名
  - `cosyvoice`：CosyVoice 模型名
  - `volcengine`：`req_params.model` 的值
- Generator 在读取 `tts_model` 时，也必须按 `tts_provider` 分支解释，不能把不同 provider 的语义混为一谈

### 5.4 共享 matcher 框架

本计划的“自动匹配音色”是应用层能力，不是火山 API 自动完成。

本轮新增共享层：

- `voice_match_types.py`
  - 统一定义 `VoiceMatchRequest / VoiceMatchResult`
- `voice_match_resolver.py`
  - 统一入口
  - 本轮只要求支持 VolcEngine 主路径

本轮不强制改造 CosyVoice 主路径：

- `cosyvoice_voice_selector.py` 保持现状
- 后续若要统一接入，再单独做一轮兼容改造

### 5.5 VolcEngine 专属 matcher

新增：

- `volcengine_voice_catalog.py`
- `volcengine_voice_selector.py`

要求：

- 先只维护 `20-30` 个 1.0 核心音色子集
- 2.0 公版音色先覆盖当前计划里实际会暴露的核心集合
- 输出结构对齐共享 `VoiceMatchResult`
- B1 baseline 先复用当前仓库已有字段：
  - `gender`
  - `age_group`
  - `persona_style`
  - `energy_level`
  - `voice_description`

### 5.6 Express 与 Studio 策略

`express`：

- `resource_id = seed-tts-1.0`
- `req_params.model = seed-tts-1.1`
- 若 `segment.voice_id` 显式存在且兼容 1.0，优先使用
- 否则走 VolcEngine 1.0 matcher
- 匹配失败时退回 1.0 默认安全音色

`studio`：

- `resource_id = seed-tts-2.0`
- 公版 2.0 默认不传 `req_params.model`
- 在工作区 `voice_review` 阶段展示 2.0 公版音色列表，并额外提供一个 `auto` 选项
- 用户显式选择时，直接使用该音色
- 未选择或选择 `auto` 时，走 VolcEngine 2.0 matcher
- 匹配失败时退回 2.0 默认安全音色

### 5.7 音色优先级与最小兜底

VolcEngine 音色优先级为：

- `segment.voice_id`
- `voice_review` 阶段用户显式选择的 studio 音色
- `VOLCENGINE_TTS_DEFAULT_SPEAKER`
- `default_speaker_for_resource(resource_id)`

这是有意行为调整，目的有二：

- 局部显式选择优先于全局环境变量
- 避免单个全局 env 把 1.0/2.0 模式串掉

最小兜底：

- 若 VolcEngine 返回“音色 / resource 不兼容”类错误，且当前音色不是该 resource 的默认音色，则自动用该 resource 默认音色重试一次
- 错误判断不写死单个 `"45000000"`，应收敛为 helper，例如：
  - `_is_volcengine_voice_resource_mismatch(exc)`
- 该 helper 可以结合：
  - 已知错误码集合
  - `speaker` / `voice` / `resource` / `invalid` 等关键词

### 5.8 Block B 文件范围

修改文件：

- `src/services/tts/volcengine_tts_provider.py`
- `tests/test_volcengine_tts_provider.py`
- `gateway/job_intercept.py`
- `tests/test_gateway_job_policy.py`
- `tests/test_gateway_create_job.py`
- `tests/test_job_model_snapshot.py`
- `src/services/tts/tts_generator.py`
- `tests/test_tts_generator.py`
- `src/services/web_ui/handler.py`
- `src/services/web_ui/voice_library.py`
- `tests/test_web_ui.py`
- `frontend-next/src/components/workspace/VoiceReviewPanel.tsx`
- `frontend-next/src/components/workspace/index.ts`
- `frontend-next/src/app/workspace/[jobId]/page.tsx`
- `frontend-next/src/lib/api/reviews.ts`
- `frontend-next/src/types/reviews.ts`

新建文件：

- `src/services/tts/voice_match_types.py`
- `src/services/tts/voice_match_resolver.py`
- `src/services/tts/volcengine_voice_catalog.py`
- `src/services/tts/volcengine_voice_selector.py`
- `tests/test_voice_match_resolver.py`
- `tests/test_volcengine_voice_selector.py`

不修改：

- 旧 `frontend/*`
- `src/services/tts_provider.py`
- `src/services/tts/cosyvoice_voice_selector.py`
- 部署脚本 / docker-compose

## 6. 分阶段实施

### 阶段 A1：Review 音频预处理 + audio-first 主路径 + batched 策略

文件：

- `src/services/transcript_reviewer.py`
- `tests/test_transcript_reviewer.py`

改动：

- 实现 `_prepare_review_audio()`
- 去掉 `200MB` 阈值作为主判断
- 主路径改成优先带音频
- 失败时按“更激进压缩 -> text-only prompt”顺序回退
- 接入 review 音频时长分档策略：
  - `<= 20 分钟`：整段音频 + explicit caching（失败时回退）
  - `20 分钟 - 3 小时`：batch-local clip
- batched review 的 clip 时间窗按 `start_ms / end_ms + padding` 计算

验证命令：

```bash
python -m pytest tests/test_transcript_reviewer.py -v
```

### 阶段 A2：Prompt 双版本

文件：

- `src/services/transcript_reviewer.py`
- `tests/test_transcript_reviewer.py`

改动：

- 拆分有音频 / 无音频 prompt

验证命令：

```bash
python -m pytest tests/test_transcript_reviewer.py -v
```

### 阶段 A3：Review 模型映射 + admin 配置

文件：

- `src/services/transcript_reviewer.py`
- `gateway/admin_settings.py`
- `frontend-next/src/app/admin/settings/page.tsx`

改动：

- 增加 `review_model` 的 label -> API model id 映射
- 统一默认选项与后台文案
- 不在多处散落硬编码 model id

验证命令：

```bash
python -m pytest tests/test_transcript_reviewer.py -v
cd frontend-next && npx tsc --noEmit && npx eslint src/app/admin/settings/page.tsx
```

### 阶段 A4：Legacy fallback + speaker cache

文件：

- `src/pipeline/process.py`
- `src/services/tts/tts_generator.py`
- `tests/test_tts_generator.py`

改动：

- unified review 完全失败时补最小 `gender / age_group`
- Generator 增加 speaker 级自动匹配缓存

验证命令：

```bash
python -m pytest tests/test_tts_generator.py -v -k "voice or volcengine or cosyvoice"
```

### 阶段 B1：Provider 支持 `resource_id + model`

文件：

- `src/services/tts/volcengine_tts_provider.py`
- `tests/test_volcengine_tts_provider.py`

改动：

- Provider 常量拆分：

```python
RESOURCE_ID_1_0 = "seed-tts-1.0"
RESOURCE_ID_2_0 = "seed-tts-2.0"
DEFAULT_RESOURCE_ID = RESOURCE_ID_1_0

MODEL_1_0 = "seed-tts-1.1"

DEFAULT_SPEAKER_1_0 = "zh_female_shuangkuaisisi_moon_bigtts"
DEFAULT_SPEAKER_2_0 = "zh_female_shuangkuaisisi_uranus_bigtts"
```

- `synthesize()` 支持显式 `resource_id` 和 `model`
- 保持当前 V3 chunked / PCM -> WAV 逻辑

验证命令：

```bash
python -m pytest tests/test_volcengine_tts_provider.py -v
```

### 阶段 B2：Gateway 写入简化版 snapshot

文件：

- `gateway/job_intercept.py`
- `tests/test_gateway_job_policy.py`
- `tests/test_gateway_create_job.py`
- `tests/test_job_model_snapshot.py`

改动：

- 不新增 `tts_resource_id` 字段
- `volcengine + express` 时写入 `tts_model = "seed-tts-1.1"`
- `volcengine + studio` 时写入 `tts_model = None or ""`
- 非 `volcengine` provider 保持既有逻辑
- 确保 `tts_model` 与 `voice_clone_enabled` 的 round-trip 正常

验证命令：

```bash
python -m pytest tests/test_gateway_job_policy.py tests/test_gateway_create_job.py tests/test_job_model_snapshot.py -v
```

### 阶段 B3：新增共享 matcher 框架

文件：

- `src/services/tts/voice_match_types.py`
- `src/services/tts/voice_match_resolver.py`
- `tests/test_voice_match_resolver.py`

改动：

- 定义统一 `VoiceMatchRequest / VoiceMatchResult`
- 提供统一入口 `resolve_voice_match(...)`
- 本轮只要求支持 VolcEngine 分发
- `manual` 模式直接返回显式音色

验证命令：

```bash
python -m pytest tests/test_voice_match_resolver.py -v
```

### 阶段 B4：新增 VolcEngine catalog + matcher

文件：

- `src/services/tts/volcengine_voice_catalog.py`
- `src/services/tts/volcengine_voice_selector.py`
- `tests/test_volcengine_voice_selector.py`

改动：

- 建立 1.0 核心音色子集与 2.0 公版候选池
- 实现 VolcEngine B1 baseline matcher
- 保证不同 resource 间不会串音色

验证命令：

```bash
python -m pytest tests/test_volcengine_voice_selector.py -v
```

### 阶段 B5：Generator 接入 resolver 与双模式

文件：

- `src/services/tts/tts_generator.py`
- `tests/test_tts_generator.py`

改动：

- 在 Generator 中按 `tts_provider + service_mode` 推导运行时 `resource_id`
- 读取 `tts_model` 作为 `req_params.model`
- `express` 走 1.0 matcher
- `studio` 显式音色优先，`auto` 时走 2.0 matcher
- 调用 provider 时显式传 `resource_id` 与 `model`
- 保留 mismatch -> 默认音色重试一次

验证命令：

```bash
python -m pytest tests/test_tts_generator.py -v -k "volcengine"
```

### 阶段 B6：`frontend-next` 用户侧 Studio 音色选择 UI

文件：

- `src/services/web_ui/handler.py`
- `src/services/web_ui/voice_library.py`
- `tests/test_web_ui.py`
- `frontend-next/src/components/workspace/VoiceReviewPanel.tsx`
- `frontend-next/src/components/workspace/index.ts`
- `frontend-next/src/app/workspace/[jobId]/page.tsx`
- `frontend-next/src/lib/api/reviews.ts`
- `frontend-next/src/types/reviews.ts`

改动：

- 不再把 Studio 2.0 音色选择放在管理员后台
- 在工作区 `voice_review` 阶段新增用户侧 `VoiceReviewPanel`
- `WorkspacePage` 在 `voice_review` 阶段渲染该面板，而不是继续显示“自动处理中”
- `voice_review` 快照需为每个 speaker 暴露 Studio 2.0 公版音色列表，并额外提供 `auto` 选项
- 面板按 speaker 展示该下拉列表
- 用户必须为每个 speaker 显式选择：
  - 一个具体音色，或
  - `auto`
- 若任一 speaker 未选择，点击“确认并继续”时必须弹出提示并阻止提交
- 后端 `voice_review approve` 也必须拒绝“存在待确认 speaker 但未提交选择”的 payload，避免绕过前端校验
- 复用现有 `/api/review/voice/approve` 链路提交用户选择
- 提交语义：
  - 选择具体 2.0 音色：写入 `voice_id_a / voice_id_b`
  - 选择 `auto`：写入 `auto`，运行时由 matcher 决定最终音色
- 后端必须接受 VolcEngine 2.0 公版音色和 `auto`，不要再把该链路绑定到旧的 builtin voice registry 语义
- 不在管理员后台新增“Studio 默认音色”设置

验证命令：

```bash
python -m pytest tests/test_web_ui.py -v -k "voice_review"
cd frontend-next
npx tsc --noEmit
npx eslint src/components/workspace/VoiceReviewPanel.tsx src/app/workspace/[jobId]/page.tsx src/lib/api/reviews.ts
```

## 7. 回归范围

按阶段做定向回归，不使用笼统的 `pytest tests/ -v`。

建议最终回归命令：

```bash
python -m pytest tests/test_transcript_reviewer.py -v
python -m pytest tests/test_volcengine_tts_provider.py -v
python -m pytest tests/test_gateway_job_policy.py tests/test_gateway_create_job.py tests/test_job_model_snapshot.py -v
python -m pytest tests/test_voice_match_resolver.py -v
python -m pytest tests/test_volcengine_voice_selector.py -v
python -m pytest tests/test_tts_generator.py -v -k "volcengine"
python -m pytest tests/test_web_ui.py -v -k "voice_review"
cd frontend-next && npx tsc --noEmit && npx eslint src/app/admin/settings/page.tsx src/components/workspace/VoiceReviewPanel.tsx src/app/workspace/[jobId]/page.tsx src/lib/api/reviews.ts
```

## 8. 明确不做

- 不拆出 `volcengine_1_0` / `volcengine_2_0` 两个 provider
- 不在本次新增 `tts_resource_id` 数据库字段
- 不在本次做 DB migration
- 不在本次强迁 CosyVoice 到共享 resolver 主路径
- 不在本次直接接入声音复刻 2.0 的 `standard / expressive` 切换
- 不做完整的火山官方音色列表在线同步
- 不做 1.0 与 2.0 间的复杂音色映射表
- 不修改旧 `frontend`

## 9. 执行建议

按以下顺序让 Claude 执行最稳妥：

1. `A1`：Review 音频预处理 + audio-first 主路径
2. `A2`：Prompt 双版本 + batched review 音频策略
3. `A3`：Review 模型映射 + admin 配置
4. `A4`：Legacy fallback + speaker cache
5. `B1`：Provider `resource_id + model`
6. `B2`：Gateway 简化版 snapshot
7. `B3`：共享 matcher 框架
8. `B4`：VolcEngine catalog + matcher
9. `B5`：Generator 接入 resolver 与双模式
10. `B6`：`frontend-next` 工作区 `voice_review` 面板 + 2.0 下拉 + auto 强制选择

每个阶段完成后单独汇报，再进入下一阶段。
