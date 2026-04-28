# 任务级 LLM 与 TTS 消耗量记录方案

日期：2026-04-28

## 目标

为每条视频翻译任务建立可审计、可复算的成本观测链路，覆盖：

- S1 转录方式事实记录；Gemini 多模态转录建议取消/关闭，仅作为 legacy guard 统计。
- S2 阶段 Pass1 / Pass2 / Pass3 调用的大模型用量。
- S2 辅助 speaker verifier / legacy review / 可选说话人纠错等大模型用量。
- 探针翻译、正式翻译、pre-TTS 重写、TTS 后重写调用的大模型用量。
- probe TTS、首次正式 TTS、TTS 后重写重合成、post-edit 再生成的供应商计费字符。
- 每条任务按阶段、模型、供应商、输入/输出模态拆分后的成本估算。

本方案只记录用量事实和聚合结果，不记录完整 prompt、response 正文、音频内容，避免显著增加存储、隐私和合规风险。

AssemblyAI ASR 当前单分钟成本很低，且本阶段目标是先补齐大模型与 TTS 主成本，因此 AssemblyAI ASR 费用暂不进入每条视频 provider 成本。S1 仍保留 `transcription_method` / `asr_provider` 等事实字段，后续需要精算时可补。

Gemini 多模态转录建议从产品/配置入口取消，避免引入视频输入 token 计费和与 AssemblyAI 不一致的 S1 成本口径。如果代码路径暂时保留，usage recorder 必须把它标记为 `task=s1_gemini_transcribe`、`scope=legacy_guard`，用于发现漏关流量；该路径不作为 P0/P1 主干验收目标。

## 设计原则

1. Pipeline 记录用量事实，Gateway/Admin 负责价格折算。
2. 原始用量必须可复算，不把人民币成本作为唯一事实来源。
3. 优先利用 provider 返回的 usage；缺失时允许估算，但必须标明 `token_source=estimated`。
4. TTS 成本必须来自 `TTSResult.billed_chars`，不能用最终中文字数反推。
5. 保持 V3 shadow / observability 口径，不改变当前点数扣费生产真相。
6. 不把 prompt、响应正文、音频 bytes 写入 Gateway DB。
7. 先用 `Job.metering_snapshot` JSONB 承载聚合字段，后续数据量证明需要时再拆事件表。
8. AssemblyAI ASR 暂时只记录 provider/method，不折算成本；Gemini 转录建议禁用，若 legacy 路径仍可触发则必须按 LLM/video-audio 输入成本统计并告警。

## 当前相关入口

Gateway 现有任务级 metering 写入口：

- `POST /job-api/jobs/{job_id}/metering`
- 实现：`gateway/job_intercept.py::update_job_metering`
- 落库：`Job.metering_snapshot` JSONB

Pipeline 现有上报入口：

- `src/pipeline/process.py::_report_job_metering`
- 当前已上报 `final_cn_chars`、`rewrite_count`、`tts_billed_chars`、pre-TTS rewrite 事件等。

关键模型调用入口：

- S1 Gemini 转录：`src/services/gemini/transcriber.py::GeminiTranscriber._call_gemini`（建议取消/关闭；若 legacy 入口仍保留则作为 guard 统计）
- S1 AssemblyAI ASR：`src/services/assemblyai/transcriber.py`（本阶段只记录 method，不折算成本）
- S2 Pass1：`src/services/transcript_reviewer.py::_review_pass1_speakers`
- S2 Pass2：`src/services/transcript_reviewer.py::_review_pass2_text`
- S2 Pass3：`src/services/transcript_reviewer.py::_review_pass3_voice_profiles`
- S2 speaker verifier：`src/services/transcript_reviewer.py::_run_low_support_speaker_verifier`
- S2 legacy/unified review：`src/services/transcript_reviewer.py::legacy_review_transcript_single_pass` / `_call_review`
- 可选 transcript 后处理：`src/services/assemblyai/semantic_segmenter.py::segment_with_llm` / `src/services/assemblyai/speaker_corrector.py::correct_speakers`
- 探针翻译：`src/pipeline/process.py::_run_probe_translation`
- 正式翻译 / rewrite fallback：`src/services/gemini/translator.py::_call_task_with_fallback`
- pre-TTS rewrite：`src/services/gemini/rewriter.py::rewrite_for_duration_with_profile`
- TTS 后 rewrite：`src/services/alignment/aligner.py::_attempt_rewrite_loop`
- TTS 生成：`src/services/tts/tts_generator.py::generate_all` / `_generate_one`
- Studio post-edit 单段再生成：`src/services/tts/segment_regenerate.py`
- 音色试听：`src/services/jobs/review_actions.py`（计入 account-level interactive cost，不计入视频翻译 job 成本）

## 数据模型

### 1. 单次 LLM 调用事件

Pipeline 内部维护 `LLMUsageEvent`，每次大模型调用生成一条事件。

字段建议：

```json
{
  "job_id": "job_xxx",
  "stage": "s2",
  "task": "pass1",
  "scope": "speaker_review",
  "provider": "gemini",
  "model": "gemini-3.1-pro-preview",
  "attempt_label": "primary",
  "status": "success",
  "json_mode": true,
  "has_audio_input": true,
  "has_audio_output": false,
  "input_text_chars": 18200,
  "input_text_tokens": 6200,
  "input_audio_seconds": 614.2,
  "input_audio_tokens": 15355,
  "cached_input_tokens": 0,
  "output_text_tokens": 1800,
  "output_audio_tokens": 0,
  "total_tokens": 23355,
  "token_source": "provider_usage",
  "modal_split_source": "provider_usage",
  "duration_ms": 41800,
  "error_type": "",
  "created_at": "2026-04-28T12:00:00Z"
}
```

字段说明：

- `stage`：`s1_transcribe` / `s2` / `s3` / `s4_probe` / `s4_pre_tts` / `s5_alignment` / `post_edit`
- `task`：`s1_gemini_transcribe` / `pass1` / `pass2` / `pass3` / `speaker_verifier` / `legacy_review` / `semantic_segmenter` / `speaker_corrector` / `probe_translate` / `s3_translate` / `pre_tts_rewrite` / `post_tts_rewrite`
- `scope`：更细粒度归因，例如 `speaker_review`、`translation`、`duration_rewrite`
- `attempt_label`：`primary` / `retry` / `fallback_1` 等
- `status`：`success` / `invalid_output` / `provider_error` / `skipped`
- `token_source`：`provider_usage` / `estimated` / `missing`
- `modal_split_source`：区分文本、音频 token 拆分是否来自 provider 原始 usage

注意：fallback 或 retry 只要 provider 已返回响应，就可能收费。即使 JSON 解析失败，也应记录 `status=invalid_output` 并保留 usage。

### 2. 单次 TTS 消耗事件

Pipeline 内部维护 `TTSUsageEvent`，每次实际调用 TTS 生成音频后记录一条事件。

字段建议：

```json
{
  "job_id": "job_xxx",
  "stage": "s4",
  "bucket": "first_tts",
  "provider": "minimax",
  "model": "speech-2.8-hd",
  "segment_id": 12,
  "speaker_id": "speaker_a",
  "raw_text_chars": 58,
  "spoken_chars": 54,
  "billed_chars": 116,
  "billing_unit": "provider_billed_chars",
  "fallback_used_provider": "",
  "duration_ms": 4600,
  "status": "success"
}
```

`bucket` 取值：

- `probe_tts`：S4-probe 语速校准 TTS
- `first_tts`：首次正式 TTS
- `post_tts_resynth`：S5 rewrite 后重合成
- `repair_resynth`：长段修复 / presplit 后的重合成，可聚合到 `post_tts_resynth`
- `post_edit_resynth`：Studio 工作台中用户确认修改后的单段再生成，计入该视频 job 成本
- `interactive_preview`：音色试听/预览，只计入 account-level interactive cost，不计入视频翻译 job 成本

供应商计费口径：

- MiniMax：`billed_chars = len(tts_text) * 2`
- CosyVoice：`billed_chars = len(tts_text) * 2`
- VolcEngine：`billed_chars = len(tts_text)`
- MiMo：token 计费，当前 `billed_chars=0`，应记录 `billing_unit=token_based_unknown`

### 3. Job 聚合字段

写入 `Job.metering_snapshot` 的聚合字段：

```json
{
  "llm_usage_summary": {
    "s2_pass1": {
      "call_count": 1,
      "success_count": 1,
      "fallback_count": 0,
      "input_text_tokens": 6200,
      "input_audio_tokens": 15355,
      "input_audio_seconds": 614.2,
      "cached_input_tokens": 0,
      "output_text_tokens": 1800,
      "output_audio_tokens": 0,
      "total_tokens": 23355,
      "estimated_cost_rmb": 0.32
    }
  },
  "llm_input_text_tokens": 123456,
  "llm_input_audio_tokens": 34567,
  "llm_input_audio_seconds": 1380.0,
  "llm_cached_input_tokens": 0,
  "llm_output_text_tokens": 23456,
  "llm_output_audio_tokens": 0,
  "llm_total_tokens": 181479,
  "llm_estimated_cost_rmb": 1.23,
  "llm_cost_pricing_version": "2026-04-28",
  "llm_usage_token_source_distribution": {
    "provider_usage": 12,
    "estimated": 3,
    "missing": 0
  },
  "transcription_method": "assemblyai",
  "asr_provider_cost_status": "ignored_low_cost",
  "legacy_gemini_transcription_call_count": 0,
  "tts_usage_summary": {
    "probe_tts": {
      "call_count": 4,
      "billed_chars": 420
    },
    "first_tts": {
      "call_count": 86,
      "billed_chars": 31200
    },
    "post_tts_resynth": {
      "call_count": 12,
      "billed_chars": 4100
    },
    "post_edit_resynth": {
      "call_count": 3,
      "billed_chars": 900
    }
  },
  "probe_tts_billed_chars": 420,
  "first_tts_billed_chars": 31200,
  "post_tts_resynth_billed_chars": 4100,
  "post_edit_resynth_tts_billed_chars": 900,
  "interactive_preview_tts_billed_chars": 0,
  "tts_billed_chars": 36620,
  "tts_estimated_cost_rmb": 12.5,
  "total_estimated_provider_cost_rmb": 13.73
}
```

`tts_billed_chars` 保持兼容，但语义调整为全量 TTS 计费字符：

```text
tts_billed_chars =
  probe_tts_billed_chars
+ first_tts_billed_chars
+ post_tts_resynth_billed_chars
+ post_edit_resynth_tts_billed_chars
```

`interactive_preview_tts_billed_chars` 不进入 `tts_billed_chars`。试听/预览是用户交互成本，不是视频翻译 pipeline 成本；如果后续要做账号级成本分析，应在 account-level metering 单独汇总。

## 运行流程

### Phase A：Pipeline 侧收集器

新增轻量收集器：

```python
class JobUsageMeter:
    llm_events: list[LLMUsageEvent]
    tts_events: list[TTSUsageEvent]

    def record_llm(...): ...
    def record_tts_result(bucket: str, result: TTSResult, segment: DubbingSegment): ...
    def build_snapshot() -> dict[str, object]: ...
    def write_artifact(project_dir: Path) -> Path: ...
```

生命周期：

1. `ProcessPipeline.process()` 开始时创建 `JobUsageMeter(job_id=...)`。
2. 若存在 `metering/usage_events.json`，先加载并按 event id 去重合并，支持 Studio pause/resume。
3. 传给 S2 reviewer、translator、rewriter、aligner、TTS generator。
4. 各阶段只追加事件，不直接写 Gateway。
5. 每个阶段完成、进入人工 review 暂停前、异常退出前，都 flush 到本地 artifact：`metering/usage_events.json` 和 `metering/usage_summary.partial.json`。
6. S6 完成时 `build_snapshot()`，通过 `_report_job_metering(..., extra_metering=snapshot)` 一次性上报最终 summary。
7. 同时写最终本地 artifact：`metering/usage_summary.json`。

不要只在 S6 才写 artifact。Studio 模式会在 voice selection / translation review 等阶段暂停，进程可能退出；如果早期 S2 / probe / pre-TTS 用量只在内存中，会造成 resume 后成本丢失。

### Phase B：LLM 调用接入

#### S1 转录

默认口径：

- `transcription_method=assemblyai` 时，仅记录 `transcription_method`、`asr_provider`、`source_duration_seconds`，暂不折算 ASR provider 成本。
- `transcription_method=gemini` 建议在产品/配置层取消；如果 legacy 路径仍可触发，必须记录为 `stage=s1_transcribe`、`task=s1_gemini_transcribe`、`scope=legacy_guard`。

Gemini 转录 legacy guard 记录内容：

- 视频/音频输入时长或 provider 返回的 video/audio input tokens。
- prompt 文本输入 tokens。
- transcript JSON 输出 tokens。
- `legacy_gemini_transcription_call_count` 聚合字段。

Go/No-Go：

- 新任务默认不应触发 Gemini 转录。
- Admin 成本监控中如出现 `legacy_gemini_transcription_call_count > 0`，应作为配置漂移或旧入口未关闭处理。

#### S2 Pass1

Pass1 输入包含转录文本和原始音频。

记录内容：

- `stage=s2`
- `task=pass1`
- `input_text_chars=len(prompt)`
- `input_audio_seconds=source_audio_duration`
- Gemini/MiMo 返回 usage 时读取真实 token
- 如果无法拆分音频 token，用 `audio_seconds * 25` 估算 Gemini 音频 token，并标 `modal_split_source=estimated`

#### S2 Pass2

Pass2 是纯文本纠错、split、glossary。

记录内容：

- `stage=s2`
- `task=pass2`
- `input_text_tokens`
- `output_text_tokens`
- fallback/retry 的每次尝试都记录

#### S2 Pass3

Pass3 输入包含 speaker profiles 文本和说话人音频 clips。

记录内容：

- `stage=s2`
- `task=pass3`
- `input_audio_seconds=sum(clip_duration_seconds)`
- `input_audio_tokens`
- `output_text_tokens`

#### S2 speaker verifier

低支持说话人 verifier 会发送局部音频 clips 和候选 JSON prompt。

记录内容：

- `stage=s2`
- `task=speaker_verifier`
- `input_audio_seconds=sum(candidate_clip_duration_seconds)`
- `input_audio_tokens`
- `input_text_tokens`
- `output_text_tokens`
- `candidate_count`

#### S2 legacy / unified review

`legacy_review_transcript_single_pass` / `_call_review` 属于旧路径或 fallback 路径。只要代码路径仍存在，就不能假设成本为 0。

记录内容：

- `stage=s2`
- `task=legacy_review`
- `scope=legacy_guard`
- 文本/音频输入 tokens
- 输出 tokens
- `status=success|invalid_output|provider_error`

#### 可选 transcript 后处理

`semantic_segmenter` 和 `speaker_corrector` 如果在实际运行路径中启用，也要记录。

记录内容：

- `task=semantic_segmenter`：纯文本输入 + 文本输出
- `task=speaker_corrector`：纯文本输入 + 文本输出
- 未启用时不产生 event，不需要写 0 成本明细

#### 探针翻译

`_run_probe_translation()` 调 translator 的 probe 逻辑。

记录内容：

- `stage=s4_probe`
- `task=probe_translate`
- 纯文本 token

#### 正式翻译

`GeminiTranslator._call_task_with_fallback("s3_translate", ...)`

记录内容：

- `stage=s3`
- `task=s3_translate`
- 按 batch 记录多条事件
- retry/fallback 单独记录

#### pre-TTS rewrite

在 `_pre_rewrite_obvious_overshoot_segments_before_tts()` 调 `_rewrite_pre_tts_with_guardrail_prompt()` 时传入 usage scope。

记录内容：

- `stage=s4_pre_tts`
- `task=pre_tts_rewrite`
- `segment_id`
- accepted/rejected/strict retry 都记录

#### post-TTS rewrite

在 `SegmentAligner._attempt_rewrite_loop()` 里调用 rewriter 时记录。

记录内容：

- `stage=s5_alignment`
- `task=post_tts_rewrite`
- `segment_id`
- rewrite 后若触发 TTS 重合成，TTS 事件单独记到 `post_tts_resynth`

### Phase C：Provider usage 读取

新增统一返回结构：

```python
@dataclass
class LLMCallResult:
    text: str
    usage: dict[str, object]
```

兼容旧接口：

```python
def generate_text(...) -> str:
    return self.generate_text_result(...).text
```

各 provider 读取：

- OpenAI / DeepSeek：`usage.prompt_tokens`、`usage.completion_tokens`、`usage.total_tokens`
- DeepSeek：如返回 cache hit/miss token，保留到 `cached_input_tokens` 或 provider-specific fields
- Anthropic：`usage.input_tokens`、`usage.output_tokens`、cache read/create tokens
- Gemini：读取 SDK response usage metadata；如果字段不稳定，用 helper 做 best-effort extract
- MiMo：优先读 OpenAI-compatible `usage`；没有则估算并标 `estimated`

不得因为 usage 字段缺失导致主流程失败。usage 采集必须 best-effort。

### Phase D：TTS 调用接入

#### Probe TTS

`_run_probe_tts_and_calibrate()` 中：

```python
probe_results = tts_generator.generate_all(probe_segments, str(probe_tts_dir))
usage_meter.record_tts_results("probe_tts", probe_results, probe_segments)
```

#### 首次正式 TTS

S4 主路径：

```python
tts_results = tts_generator.generate_all(segments_needing_tts, str(tts_dir))
usage_meter.record_tts_results("first_tts", tts_results, segments_needing_tts)
```

#### TTS 后重合成

`SegmentAligner` 增加 callback：

```python
SegmentAligner(
    ...,
    tts_usage_callback=usage_meter.record_tts_result,
)
```

在 `_attempt_rewrite_loop()` 重合成后：

```python
tts_result = self.tts_generator._generate_one(...)
self.tts_usage_callback("post_tts_resynth", tts_result, segment)
```

长段修复、presplit、repair 路径里如有额外 `generate_all()`，统一记为 `post_tts_resynth` 或 `repair_resynth`。

#### Studio post-edit 再生成与试听

Studio 工作台中存在两类 TTS：

1. 用户确认修改后触发的单段再生成：计入该视频 job 成本，bucket=`post_edit_resynth`。
2. 音色试听/预览：不计入该视频 job 成本，bucket=`interactive_preview`，后续如需要可进入 account-level interactive metering。

落库口径：

- `post_edit_resynth_tts_billed_chars`：进入 `tts_billed_chars` 总计。
- `interactive_preview_tts_billed_chars`：不进入 `tts_billed_chars`，只用于账号级交互成本分析。

## 价格折算

新增价格表模块，建议放在 Gateway/Admin 侧：

```python
LLM_PRICE_CATALOG = {
    "2026-04-28": {
        "gemini:gemini-3.1-pro-preview": {
            "text_input_per_mtok_usd": 2.0,
            "audio_input_per_mtok_usd": 2.0,
            "text_output_per_mtok_usd": 12.0,
            "audio_output_per_mtok_usd": 0.0,
            "cached_input_per_mtok_usd": 0.0,
        }
    }
}
```

计算公式：

```text
llm_cost =
  input_text_tokens / 1_000_000 * text_input_price
+ input_audio_tokens / 1_000_000 * audio_input_price
+ cached_input_tokens / 1_000_000 * cached_input_price
+ output_text_tokens / 1_000_000 * text_output_price
+ output_audio_tokens / 1_000_000 * audio_output_price
```

TTS 价格表：

```python
TTS_PRICE_CATALOG = {
    "minimax:speech-2.8-turbo": {"rmb_per_10k_billed_chars": 2.0},
    "minimax:speech-2.8-hd": {"rmb_per_10k_billed_chars": 3.5},
    "cosyvoice:cosyvoice-v3-flash": {"rmb_per_10k_billed_chars": 1.0},
    "volcengine:seed-tts-1.1": {"rmb_per_10k_billed_chars": 3.0}
}
```

计算公式：

```text
tts_cost = billed_chars / 10_000 * rmb_per_10k_billed_chars
```

MiMo TTS 目前无法用 billed chars 精准计费，应标记：

```json
{
  "billing_status": "unknown_token_based",
  "estimated_cost_rmb": null
}
```

## Gateway 改动

### 1. metering 白名单

`gateway/job_intercept.py::update_job_metering` 增加允许字段：

- `llm_usage_summary`
- `llm_input_text_tokens`
- `llm_input_audio_tokens`
- `llm_input_audio_seconds`
- `llm_cached_input_tokens`
- `llm_output_text_tokens`
- `llm_output_audio_tokens`
- `llm_total_tokens`
- `llm_estimated_cost_rmb`
- `llm_cost_pricing_version`
- `llm_usage_token_source_distribution`
- `transcription_method`
- `asr_provider_cost_status`
- `legacy_gemini_transcription_call_count`
- `tts_usage_summary`
- `probe_tts_billed_chars`
- `first_tts_billed_chars`
- `post_tts_resynth_billed_chars`
- `post_edit_resynth_tts_billed_chars`
- `interactive_preview_tts_billed_chars`
- `tts_estimated_cost_rmb`
- `total_estimated_provider_cost_rmb`
- `usage_events_artifact_path`

### 2. 模型注释

更新 `gateway/models.py` 中 `metering_snapshot schema` 注释，明确旧字段 `tts_billed_chars` 是全量 TTS 字符汇总。

### 3. Admin observability

`gateway/credits_observability.py` 增加字段状态：

- LLM usage summary：`LIVE_PARTIAL`
- TTS split fields：MiniMax/CosyVoice/VolcEngine `LIVE`，MiMo `LIVE_PARTIAL`
- estimated cost：`DERIVED`

新增或扩展 admin API：

- `/api/admin/credits/cost-metrics`
- `/api/admin/credits/provider-breakdown`

输出字段：

- LLM 成本/分钟 p50/p90
- TTS 成本/分钟 p50/p90
- pre-TTS rewrite 成本/分钟
- post-TTS resynth 成本/分钟
- provider/model 分组成本
- token_source 覆盖率

## 本地 artifact

每个项目目录写：

```text
metering/
  usage_events.json
  usage_summary.json
```

`usage_events.json` 保存明细事件，但不保存 prompt/response/audio。

`metering_snapshot` 中只保存：

```json
{
  "usage_events_artifact_path": "metering/usage_events.json"
}
```

对于后台成本页，优先读 DB summary；只有排查单 job 时再看 artifact。

## 性能影响

预期影响很小：

- CPU：累加 token 和字符计数，基本可忽略。
- 内存：每个 job 几十到几百条事件，通常几十 KB。
- DB：job 结束时一次性写 summary，避免每次模型调用同步写库。
- 存储：summary 通常小于 10KB/job；本地 events 通常 50KB-300KB/job。

禁止事项：

- 不在每次 LLM 调用后同步写 Gateway DB。
- 不把完整 prompt、response、音频 bytes 写入 `metering_snapshot`。
- 不把 segment 全量文本明细塞进 JSONB。

## 分阶段实施

### P0：TTS 分桶先闭环

目标：解决当前 `tts_billed_chars` 低估问题。

改动：

1. 新增 `JobUsageMeter` 的 TTS 部分。
2. probe / first / post-resynth / post-edit-resynth 四个 bucket 全部从 `TTSResult.billed_chars` 汇总。
3. Gateway 白名单接收 split 字段。
4. Admin provider breakdown 使用全量 `tts_billed_chars`。

验收：

- `tts_billed_chars = probe + first + post_resynth + post_edit_resynth`
- post-TTS rewrite 后的 `_generate_one()` 能进入 `post_tts_resynth_billed_chars`
- 用户确认修改后的单段再生成能进入 `post_edit_resynth_tts_billed_chars`
- 音色试听不进入视频 job 的 `tts_billed_chars`
- 旧测试 `tts_billed_chars` 兼容

### P1：LLM 调用次数和估算 token 闭环

目标：先不依赖 provider usage，也能知道各阶段调用次数和估算成本。

改动：

1. 新增 `LLMUsageRecorder`。
2. 对 S1 Gemini legacy guard、S2 Pass1/2/3、S2 speaker_verifier / legacy_review / 可选 transcript 后处理、S3 translate、probe translate、pre/post rewrite 记录事件。
3. token 暂用估算：
   - 文本 tokens：保守用字符数 / 2 或接入 tokenizer helper
   - Gemini 音频 tokens：`audio_seconds * 25`
4. `token_source=estimated`
5. AssemblyAI ASR 只记录 method/provider，不折算成本。

验收：

- 每个成功 job 有 `llm_usage_summary`
- pre-TTS accepted/rejected/strict retry 都能体现调用次数
- fallback/retry 有单独 event
- 默认新任务 `legacy_gemini_transcription_call_count == 0`
- 暂停/恢复后早期 S2 / probe usage 不丢失

### P2：Provider 真实 usage 接入

目标：把估算升级为真实 usage。

改动：

1. LLM provider 返回 `LLMCallResult(text, usage)`。
2. OpenAI/DeepSeek/Anthropic 读取 response usage。
3. Gemini 读取 usage metadata，缺字段时降级 estimated。
4. MiMo 能读 usage 就读，不能读就继续 estimated。
5. Gemini 转录若仍被 legacy 入口触发，也必须读取 provider usage 或降级 estimated。

验收：

- `llm_usage_token_source_distribution.provider_usage` 覆盖率可见
- provider usage 缺失不影响主流程
- invalid JSON 但 provider 返回 usage 的调用仍计入成本

### P3：成本折算与后台视图

目标：按最新价格表输出 job/provider/model/阶段级成本。

改动：

1. 新增 LLM/TTS price catalog。
2. `build_snapshot()` 输出 `llm_estimated_cost_rmb`、`tts_estimated_cost_rmb`。
3. Admin 成本页展示按分钟成本、毛利率、异常 job。

验收：

- 可按 job 查看：收入点数、LLM 成本、TTS 成本、总 provider 成本、毛利估算
- 可按 provider/model 聚合
- 可按 service_mode/quality_tier 聚合

### P4：可选事件表

当数据量和分析需求稳定后，再从 JSONB/artifact 迁出明细事件：

```text
job_llm_usage_events
job_tts_usage_events
```

迁出的触发条件：

- 需要跨 job 查询单次调用明细。
- `metering_snapshot` JSONB 过大影响查询。
- 需要长期保留 usage event 而项目目录会清理。

## 测试计划

新增或更新测试：

1. `tests/test_job_metering_writeback.py`
   - Gateway 接收并合并 split TTS 字段。
   - Gateway 接收并合并 LLM summary 字段。
   - unknown usage 字段仍被忽略。

2. `tests/test_process_pipeline.py`
   - first TTS 进入 `first_tts_billed_chars`。
   - probe TTS 进入 `probe_tts_billed_chars`。
   - post-TTS rewrite `_generate_one()` 进入 `post_tts_resynth_billed_chars`。
   - pause/resume 后 usage artifact reload/merge 不重复、不丢失。

3. `tests/test_aligner.py`
   - rewrite loop 触发 resynth callback。
   - budget exhausted 时不记录额外 resynth。

4. `tests/test_llm_usage_metering.py`
   - fallback/retry 事件聚合正确。
   - estimated token source 分布正确。
   - provider usage 缺失时不抛异常。
   - S2 speaker_verifier / legacy_review / semantic_segmenter / speaker_corrector 可按 task 聚合。
   - Gemini 转录 legacy guard 触发时写 `legacy_gemini_transcription_call_count`。

5. `tests/test_credits_observability.py`
   - FIELD_STATUS 包含新增字段。
   - cost metrics 返回 LLM/TTS cost coverage。

6. `tests/test_post_edit_metering.py`
   - 用户确认修改后的单段再生成进入 `post_edit_resynth_tts_billed_chars`。
   - voice preview 不进入视频 job 的 `tts_billed_chars`。

## 风险与处理

### provider usage 字段不稳定

处理：

- usage extract helper 必须 best-effort。
- 原始 provider payload 不落库，只落规范化 usage。
- 缺失时标 `estimated`。

### JSONB 膨胀

处理：

- DB 只落 summary。
- 明细事件写 artifact。
- 后续按 P4 拆表。

### 暂停 / 恢复导致用量丢失

处理：

- 每个阶段完成和进入人工 review 暂停前 flush `usage_events.json`。
- resume 时先加载已有 artifact，并按稳定 event id 去重。
- S6 只做最终 summary 上报，不作为唯一持久化时机。

### retry/fallback 重复计费遗漏

处理：

- 每次 provider attempt 都经过统一 recorder。
- 成功、invalid output、fallback 前失败都要有状态。

### 成本与价格变动

处理：

- summary 同时保留原始 tokens/chars 和 `cost_pricing_version`。
- Admin 可用新价格表重算历史成本。

### MiMo TTS 无法按 billed chars 精算

处理：

- 保留 `billing_status=unknown_token_based`。
- 不把 MiMo 的 0 billed chars 当作 0 成本。

### Gemini 转录入口未完全关闭

处理：

- 产品/配置层默认禁用 Gemini 转录。
- 如果 legacy 路径仍被触发，记录 `task=s1_gemini_transcribe` 并增加 `legacy_gemini_transcription_call_count`。
- Admin 成本页对该字段非 0 的 job 给出配置漂移提示。

## 最终可回答的问题

实施后，每个 job 可以直接回答：

- S1 使用了哪种转录方式；AssemblyAI ASR 暂不折算成本；是否误触发 Gemini 转录 legacy guard。
- S2 Pass1 / Pass2 / Pass3 各用了哪个模型、多少文本/音频输入、多少输出。
- S2 speaker verifier / legacy review / 可选 transcript 后处理是否触发，消耗多少。
- 翻译、probe、pre-TTS rewrite、post-TTS rewrite 各花了多少。
- TTS 首次合成、probe、后置重合成、post-edit 再生成各产生多少供应商计费字符。
- 音色试听是否被排除在视频 job 成本外。
- fallback/retry 额外消耗了多少。
- 成本是 provider 真实 usage 还是估算。
- 按当前价格表，单视频总 provider 成本、每分钟成本、毛利率是多少。
