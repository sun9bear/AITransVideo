# 说话人、TTS 与字幕对齐优化方案

日期：2026-05-21

状态：审核草案

范围：翻译提示词与 script gate、说话人证据链、音色克隆样本选择、TTS 音频适配、Whisper/DTW 字幕置信度、字幕显示宽度质量门

相关架构约束：

- TTS 单元保持为 `SemanticBlock`，不是字幕行。
- 对齐策略保持 DSP 优先，rewrite 只作为兜底。
- 字幕重定时保持数学/确定性逻辑，不交给 LLM。
- 主交付目标保持为剪映草稿，不把渲染 MP4 作为主产物。
- 默认本地路径和测试路径不能新增强制实时外部服务依赖。

## 1. 摘要

对 OmniVoice Studio、GitCC/gcc-media-workshop 以及几个类似开源视频配音项目做过对比后，真正值得吸收的不是把本项目替换成 WhisperX、pyannote、RubberBand 这类重依赖方案。本项目已经有更贴合业务目标的基础：

- `SemanticBlock` 已经是 TTS 和对齐的核心单位。
- 翻译链路已经有 prompt 模板、断点续跑、时长提示和术语表检查；下一步应该加强语言/script 质量门，而不是重建翻译子系统。
- 说话人链路已经包含 ASR speaker label、Gemini 说话人审核、低样本说话人验证、观众/非主体保护、speaker structure profile。
- TTS 对齐已经有 direct/DSP/rewrite/capped DSP、短段合并和较完整的审计指标。
- 字幕链路已经有确定性比例分配、可选 faster-whisper/DTW 对齐、文本音频漂移 gate、跨 block overlap clamp。

主要优化方向是让现有决策更可观测、更可审核、更有选择性：

1. 增加翻译提示词和 script gate：语言全名提示、非拉丁文字检查、直译后 reflect/adapt。
2. 增加 `speaker_evidence.jsonl`，让说话人修正、保留、拆分、验证都有证据链。
3. 把音色克隆样本从“最长且够响”升级为“可评分、可解释、可复查”的样本 manifest。
4. 对长 TTS 音频增加有限尾部静音裁剪，避免因为尾巴静音触发 rewrite 或高倍速 DSP。
5. 为 Whisper/DTW 字幕对齐增加匹配质量评分，弱对齐自动回退比例字幕。
6. 使用已有 `text_width` 能力增加字幕显示宽度质量门，先 report-only。

这些优化不改变核心架构，不引入默认重依赖，也不把 LLM 放到时间轴控制位置。

## 2. 当前代码现状

### 2.1 翻译提示词与 script 控制

当前实现：

- `src/services/gemini/translator.py`
  - 通过 `get_effective_translation_prompt_template()` 支持翻译 prompt 模板。
  - 构建翻译 batch 时已有 duration 和 character hints。
  - 翻译结果有 checkpoint，支持失败后续跑。
  - 已有输出结构校验和术语表 post-translation 检查。

判断：

翻译链路已经足够集中，适合在现有 `translator.py` 附近补质量门，不需要新增独立翻译框架。OmniVoice 类项目中值得借鉴的是：

- prompt 中使用目标语言全名，而不是只给 `zh-CN` 这类 locale code。
- 对非拉丁目标语言增加 script gate，防止模型输出英文、罗马音或混合脚本。
- 先保留一个直译语义锚点，再做有限 reflect/adapt，让最终文本更适合配音但不改变事实和术语。

边界：

这些能力只能作为翻译质量控制，不能变成字幕时间、TTS 对齐或说话人判断的控制源。

### 2.2 说话人归属

当前实现：

- `src/services/assemblyai/transcriber.py`
  - `transcribe()` 在配置允许时启用 AssemblyAI speaker labels。
  - `_build_transcript_lines()` 优先使用 utterance speaker。
  - `_build_lines_from_words()` 可以保留 word-level speaker label。
- `src/services/gemini/transcriber.py`
  - Gemini 转写支持标准化的 `speaker_a`、`speaker_b`、`speaker_c`。
- `src/services/transcript_reviewer.py`
  - Pass1 音频审核可以修正说话人或拆分行。
  - 低样本说话人 verifier 会对可疑小样本 speaker 做局部音频验证。
  - 不确定结果保持原始说话人，不强行修改。
- `src/pipeline/process.py`
  - `_build_speaker_structure_profiles()` 会分类 primary、incidental、fragmented、non_speech 等角色。
  - `_apply_speaker_structure_profiles_to_segments()` 把 speaker profile 信息带到后续 segment。

判断：

项目已经有分层说话人系统。默认引入 pyannote/WhisperX diarization 会重复已有逻辑，增加 GPU/依赖/部署成本，也可能制造新的 speaker/timeline drift。当前真正缺的是“证据链”，不是再加一个默认说话人引擎。

### 2.3 音色克隆样本提取

当前实现：

- `src/services/voice/sample_extractor.py`
  - `VoiceSampleExtractor.extract_sample()` 提取每个 speaker 的参考音频。
  - `_build_candidate_ranges()` 会把相邻同 speaker 行按 gap 合并，并用 RMS 过滤。
  - `validate_sample()` 会输出 duration、RMS、silence ratio、warnings。
- `src/pipeline/process.py`
  - Smart 模式只有在用户授权和 admin gate 允许时才提取克隆样本。
  - 如果 speaker 已经有强匹配的用户音色，会跳过新克隆。
  - 样本低于硬性最短时长时会失败或降级。

判断：

当前提取器比较保守，但样本选择主要依赖时长/RMS。对音色克隆来说，最长的可用片段未必最好。一个较短但干净、说话稳定、静音少、边界安全的片段，通常优于一段很长但混有噪声、背景音或其他说话人尾音的片段。

### 2.4 TTS 对齐

当前实现：

- `src/services/alignment/aligner.py`
  - `SegmentAligner._align_one()` 选择 direct、DSP、rewrite 或 capped DSP。
  - `_evaluate_alignment()` 决定音频是否可直接接受、DSP 适配或 rewrite。
  - `_should_attempt_rewrite()` 把 rewrite 保持为兜底。
  - `_apply_dsp_fit_audit()` 记录 speed ratio、silence padding、truncation、initial/trimmed/stretched duration。
  - `_dsp_stretch()` 委托 `utils.audio_fit.fit_audio_to_slot()`。
- `src/utils/audio_fit.py`
  - `fit_audio_to_slot()` 已有静音裁剪、限制范围内变速、补静音和截断。
  - 当前 silence trim 主要偏向短音频，长音频尾部静音不一定被优先处理。
- `src/pipeline/process.py`
  - `_annotate_short_segment_merge_candidates()` 和 `_apply_short_segment_merges_before_tts()` 会在 TTS 前合并安全短段。
- `src/modules/output/editor/editor_package_writer.py`
  - 输出剪映包时会复制并适配 aligned audio。

判断：

DSP-first 的大方向是正确的。实际缺口是：部分 TTS 输出会带较长尾部静音或呼吸尾巴。如果超时主要来自非语义尾部静音，就不应该优先 rewrite 文本或强行提高语速。

### 2.5 字幕对齐

当前实现：

- `src/modules/subtitles/cue_timing.py`
  - 对 CJK、英文词、数字、标点使用确定性 speech weight。
- `src/modules/subtitles/semantic_segmenter.py`
  - 按中文标点和弱边界拆字幕。
- `src/modules/subtitles/cue_builder.py`
  - `build_cues_for_block()` 构建比例字幕。
  - `build_cues_with_char_times()` 可以基于 char time 构建字幕。
- `src/modules/subtitles/cue_pipeline.py`
  - `_try_whisper_aligned_cues()` 在 gate 允许时使用 faster-whisper + DTW。
  - `_block_is_in_sync()` 做文本/音频一致性 gate。
  - `_clamp_cross_block_cue_overlaps()` 避免跨 block 重叠。
- `src/services/whisper_align/dtw.py`
  - `align_chars_to_words()` 把中文目标文本字符映射到 Whisper word timings。
- `src/utils/text_width.py`
  - 已有显示宽度工具，CJK/fullwidth/ambiguous 会比 ASCII 更宽。

判断：

字幕系统已经有确定性 fallback 和可选升级路径。当前问题是 accepted Whisper/DTW 对齐缺少质量分数；另外，语音 timing 和视觉可读性是两件事，显示宽度应该作为质量门加入，但不能让 LLM 控制字幕时间。

## 3. 优化候选优先级

| 候选项 | 建议 | 原因 |
| --- | --- | --- |
| 翻译语言全名、script gate、直译/反思/改写 | P0，实施 | 直接提升翻译质量，吸收 OmniVoice 中最适合本项目的 prompt 思路，验证层仍然可确定。 |
| 说话人证据链 sidecar | P0，实施 | 贴合现有 speaker review 栈，提升可审核性，不改变时间轴。 |
| 音色克隆样本评分和 manifest | P0/P1，实施 | 直接影响克隆质量，让样本选择可复查。 |
| 有限 TTS tail trim | P1，实施 | 当超时来自尾部静音时，减少不必要 rewrite 或高倍速 DSP。 |
| Whisper/DTW 匹配质量分 | P1，实施 | 让可选字幕强对齐更安全、更可调试。 |
| 字幕显示宽度 gate | P1，先 report-only | 复用已有能力，捕获视觉可读性问题，不改变确定性 timing。 |
| next-gap borrowing | P2，仅 report-only | 可作为人工审核提示，但自动借用会改变源时间轴。 |
| 默认 pyannote/WhisperX diarization | 不进主路径 | 重依赖、部署成本高、和已有 speaker 栈重复。 |
| 默认 RubberBand/Silero | 不进主路径 | 增加二进制/模型依赖，当前 DSP 路径更简单可审计。 |
| LLM 控制字幕时间 | 拒绝 | 违反确定性字幕重定时约束。 |

## 4. 具体优化方案

### 4.1 P0：翻译提示词、script gate 与 reflect/adapt loop

#### 代码现状

`src/services/gemini/translator.py` 已经集中处理翻译 prompt、模型路由、checkpoint、响应校验、术语表检查和长度 retry。这里是增加翻译质量控制的合适位置。

#### 为什么优化

翻译质量问题后面会伪装成 TTS 或字幕问题：

- 目标语言脚本错误会产生不可用配音文本。
- 非拉丁语言输出罗马音或英文时，普通非空检查无法发现。
- 单一“自由翻译” prompt 容易过度改写名称、术语和事实。
- 纯直译能保语义，但不一定适合配音。

正确做法是在 TTS 前加强翻译约束和验证，而不是靠对齐或字幕阶段补救。

#### 依据

吸收 OmniVoice 类项目的三个实用点：

- prompt 中写目标语言全名，例如 `Chinese (Simplified)`，而不是只写 `zh-CN`。
- 本轮只对当前主链路 `zh-CN` 加 script gate；完整 i18n 和第二真实 language pair 需求独立立项。
- 对非拉丁目标语言加 script gate，但不把下游 `cn_text`/`tts_input_cn_text` 字段悄悄改成泛化 i18n 字段。
- 先让模型保留直译语义，再做有限 reflect/adapt，让最终文本自然、可配音，同时保留人名、数字、术语和意图。

#### 预期效果

- 减少 wrong-script 和 mixed-script 翻译。
- 提高人名、术语、数字和事实保真。
- 让配音文本更自然，但不把 LLM 放到时间轴控制位置。
- 翻译 QA 有结构化拒绝原因。

#### 关键实现草案

新增轻量语言元数据 helper：

```python
# src/services/translation_language.py
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TargetLanguageSpec:
    code: str
    full_name: str
    expected_scripts: tuple[str, ...]
    allow_latin_ratio: float


LANGUAGE_SPECS = {
    "zh-CN": TargetLanguageSpec(
        code="zh-CN",
        full_name="Chinese (Simplified)",
        expected_scripts=("Han",),
        allow_latin_ratio=0.20,
    ),
}
```

翻译响应解析后做确定性 script gate：

```python
@dataclass(frozen=True, slots=True)
class ScriptGateResult:
    ok: bool
    latin_ratio: float
    expected_script_ratio: float
    reason_codes: tuple[str, ...] = ()


def check_target_script(text: str, spec: TargetLanguageSpec) -> ScriptGateResult:
    # 用 Unicode range/category 做检查，不调用 LLM。
    # 标点、空格和允许的术语表 token 不计入失败。
    ...
```

Prompt 调整：

- 注入 `target_language_full_name`。
- 可选输出 `literal_target_text` 作为语义锚点。
- 最终 `cn_text` 面向配音自然表达；本轮不重命名为 `target_text`。
- 输出 schema 保持严格，兼容旧响应。

可选新响应结构：

```json
{
  "id": 12,
  "literal_target_text": "...",
  "cn_text": "...",
  "reflection_notes": ["短操作性说明，不保存链式推理"],
  "quality_flags": []
}
```

边界：

`reflection_notes` 只能是短的操作性说明，不要求也不保存私有推理。TTS 消费的仍然是最终 target text。

付费 retry 预算：

- 默认不因 script gate 失败自动重试付费 LLM，失败结果写入 `quality_flags: ["wrong_script"]` 并进入审核/降级路径。
- 如后续通过 feature flag 显式允许重试，每个 segment 最多强化 prompt 重试 1 次。
- 同一 batch 的 script gate 失败率超过 30% 时，整批 abort 并写入 reason，不继续循环重试。
- 禁止在 batch/loop/retry 中出现无上限付费 API 调用。

#### 测试计划

- 语言 spec lookup 单测。
- CJK、混合 Latin/CJK、纯标点、数字、术语例外的 script gate 单测。
- 旧 prompt 响应仍可解析。
- wrong-script 输出默认不进入 TTS；如启用重试，也最多 1 次，失败后进入审核/降级。

#### 验收标准

- script gate 是确定性代码。
- 不改变字幕时间。
- 不改变 TTS 对齐策略。
- 翻译 retry 默认关闭；启用时有硬上限和 batch 失败率熔断。
- 现有 prompt template 自定义保持兼容。

### 4.2 P0：说话人证据链 sidecar

#### 代码现状

说话人归属来自多个阶段：

- AssemblyAI 或 Gemini 转写的 ASR speaker label。
- `src/services/transcript_reviewer.py` 的 Pass1 音频审核。
- `src/services/transcript_reviewer.py` 的低样本 speaker verifier。
- `src/pipeline/process.py` 的 speaker structure profiling。

最终 segment 有 `speaker_id`、`speaker_role` 和 duration share 等字段，但 speaker 为什么被改、为什么被保留、来自哪个阶段，目前分散在日志和中间对象里。

#### 为什么优化

生产审核和后续 UI 调试需要回答：

- 这个 speaker id 来自 ASR、Gemini review、verifier，还是 fallback？
- 是否从原 ASR speaker 被修改？
- 是否因为 speaker 修正发生 split？
- 这个 speaker 是 primary、incidental、fragmented 还是 non-speech？
- 哪些结果是不确定但故意保持原状？

当前很难从最终产物还原这些信息。

#### 依据

借鉴 diarization-heavy 项目中“谁在什么时候说话需要可解释”的部分，但不把新的 diarization 引擎作为默认事实来源。

#### 预期效果

- 更容易人工审核说话人错误。
- 更容易调试 Smart voice clone。
- 为未来“可疑说话人段落复核”提供基础。
- 不新增运行时依赖，不改变时间轴。

#### 建议产物

写入 job-scoped JSONL，例如：

`<project_dir>/reports/speaker_evidence.jsonl`

每行代表一个源行、拆分子行或最终 dubbing segment：

```json
{
  "job_id": "optional",
  "line_id": "line_000123",
  "source_line_ids": ["line_000123"],
  "parent_line_id": null,
  "semantic_block_id": "block_000042",
  "segment_id": 42,
  "final_segment_id": "seg_000042",
  "merge_group_id": null,
  "stage": "pass1_audio_review",
  "source_start_ms": 10230,
  "source_end_ms": 14510,
  "initial_speaker_id": "speaker_a",
  "final_speaker_id": "speaker_b",
  "source": "pass1_audio_review",
  "decision": "changed",
  "confidence": "medium",
  "evidence": {
    "asr_speaker_id": "speaker_a",
    "pass1_action": "correct_speaker",
    "verifier_result": "not_checked",
    "speaker_role": "primary",
    "low_support": false
  },
  "reason_codes": ["pass1_corrected_speaker"],
  "created_at": "2026-05-21T00:00:00Z"
}
```

字段语义需要固定：

- `stage`：流程阶段，例如 `pass1_audio_review`、`pass2_text_review`、`pass3_voice_profile`、`structure_profiling`、`low_support_verify`。
- `source`：本次决策的事实来源，例如 `asr`、`gemini_pass1`、`verifier`、`fallback`。
- `decision`：本次记录的动作，例如 `kept`、`changed`、`kept_uncertain`、`split`、`merged`。

#### 关键实现草案

新增小 helper，不做重框架：

```python
# src/services/speaker_evidence.py
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class SpeakerEvidence:
    line_id: str | None
    source_line_ids: list[str]
    parent_line_id: str | None
    semantic_block_id: str | None
    segment_id: int | None
    final_segment_id: str | None
    merge_group_id: str | None
    stage: str
    source_start_ms: int | None
    source_end_ms: int | None
    initial_speaker_id: str | None
    final_speaker_id: str | None
    source: str
    decision: str
    confidence: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    reason_codes: list[str] = field(default_factory=list)


def write_speaker_evidence_jsonl(path: Path, rows: list[SpeakerEvidence]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(asdict(row), ensure_ascii=False, sort_keys=True))
            fh.write("\n")
```

集成点：

- 在 `src/services/transcript_reviewer.py` 中记录：
  - Pass1 保持原 speaker。
  - Pass1 修正 speaker。
  - Pass1 拆分行。
  - 低样本 verifier 修改 speaker。
  - 低样本 verifier 不确定并保持原 speaker。
- 在 `src/pipeline/process.py` 中，`_build_speaker_structure_profiles()` 后补充 `speaker_role`、`speaker_duration_share`、low-support 状态。
- split/merge 必须保留 `source_line_ids`、`parent_line_id`、`semantic_block_id`、`final_segment_id`、`merge_group_id`、`stage`，保证最终 segment 可以追溯到原 transcript line。

Artifact contract：

- 不写仓库级松散 `artifacts/` 目录。
- canonical 位置应在现有 job `project_dir` 下，例如 `<project_dir>/reports/speaker_evidence.jsonl`。
- UI 第一阶段只作为下载/debug artifact 或高级审核面板暴露。
- 用户可见 badge 不暴露 provider 名称。用 `speaker_corrected`、`low_confidence`、`voice_verified`、`proportional_fallback` 这类稳定 label，不写 `Gemini fixed`。

#### 测试计划

- JSONL 序列化单测。
- Pass1 speaker correction 产生 `decision="changed"`。
- verifier 不确定时 speaker 不变，产生 `decision="kept_uncertain"`。
- speaker review 开启时 pipeline artifact 中存在 `speaker_evidence.jsonl`。

#### 验收标准

- 不改变 speaker timing。
- 不新增默认外部服务。
- 证据不可用时 sidecar 可以为空或缺省，不影响主流程。
- `main.py` 和 `pytest` 保持可运行。

### 4.3 P0/P1：音色克隆样本评分与 manifest

#### 代码现状

`VoiceSampleExtractor` 当前会把相邻同 speaker transcript lines 合并为候选区间，做 RMS 过滤，抽取音频样本，再用 `validate_sample()` 检查 duration/RMS/silence。Smart 模式再基于授权、provider 能力和最小时长决定是否使用样本。

这比盲目克隆每个 speaker 已经安全很多。弱点是样本排序。

#### 为什么优化

音色克隆质量对参考音频很敏感：

- 静音太多会削弱音色身份。
- 长片段可能混入噪声、音乐、重叠人声或情绪不一致。
- 边界太贴近其他 speaker，容易混入尾音。
- 干净的 8-20 秒样本，经常优于 90 秒混杂样本。

当前“最长可用候选”可能选到不够干净的片段。

#### 依据

很多开源 dubbing 项目也提取 speaker reference clips，但常常只是 longest-window 或 diarized-window。本项目已有 speaker lines、RMS 检查和 Smart clone gate，可以做更稳的 deterministic scoring。

#### 预期效果

- 克隆输入更稳定。
- 每个样本都有 manifest，可审核。
- 无高质量样本时更容易安全降级。
- 减少 provider 因静音/噪声/混音样本导致的 clone 失败。

#### 建议 manifest

```json
{
  "speaker_id": "speaker_a",
  "tts_provider": "cosyvoice",
  "policy_name": "cosyvoice_default",
  "selected_sample_path": "speaker_a_clone.wav",
  "total_duration_ms": 24000,
  "score": 0.86,
  "hard_reject_reasons": [],
  "warnings": ["moderate_silence_ratio"],
  "intervals": [
    {
      "start_ms": 45200,
      "end_ms": 56200,
      "duration_ms": 11000,
      "rms_dbfs": -21.4,
      "silence_ratio": 0.08,
      "speaker_gap_before_ms": 900,
      "speaker_gap_after_ms": 1300,
      "score": 0.91,
      "line_ids": ["line_00031", "line_00032"]
    }
  ]
}
```

#### 评分策略

第一版保持简单确定性：

- `duration_score`
  - 单段最佳区间：4-12 秒。
  - 总参考音频较好区间：15-45 秒。
  - 极短和过长都惩罚。
- `rms_score`
  - 偏好中等响度，例如 -30 dBFS 到 -12 dBFS。
  - 过低响度硬拒。
- `silence_score`
  - 静音比例高则惩罚。
  - 极高静音比例硬拒。
- `boundary_score`
  - 偏好前后有干净 speaker gap 的片段。
  - `gap_before_ms` 或 `gap_after_ms` 小于 500 ms 时强惩罚，避免混入相邻 speaker 尾音。
- `text_score`
  - 偏好有足够可发声内容。
  - 惩罚明显非语音、纯笑声、过度重复文本。

初始公式：

```python
score = (
    0.30 * duration_score
    + 0.25 * rms_score
    + 0.25 * silence_score
    + 0.15 * boundary_score
    + 0.05 * text_score
)
```

#### 关键实现草案

保持现有 extractor 接口兼容，增加可选 scoring：

```python
# src/services/voice/sample_extractor.py
@dataclass(slots=True)
class VoiceSampleCandidate:
    start_ms: int
    end_ms: int
    line_ids: list[str]
    transcript_text: str
    speech_char_count: int
    non_speech_reason: str | None
    rms_dbfs: float | None
    silence_ratio: float | None
    gap_before_ms: int | None
    gap_after_ms: int | None
    score: float = 0.0
    warnings: list[str] = field(default_factory=list)
    hard_reject_reasons: list[str] = field(default_factory=list)


def _score_candidate(candidate: VoiceSampleCandidate) -> VoiceSampleCandidate:
    duration_ms = candidate.end_ms - candidate.start_ms
    duration_score = _score_preferred_range(duration_ms, best_min=4000, best_max=12000)
    rms_score = _score_rms(candidate.rms_dbfs)
    silence_score = _score_silence(candidate.silence_ratio)
    boundary_score = _score_boundary(candidate.gap_before_ms, candidate.gap_after_ms)
    text_score = _score_text(
        candidate.transcript_text,
        candidate.speech_char_count,
        candidate.non_speech_reason,
    )
    candidate.score = (
        0.30 * duration_score
        + 0.25 * rms_score
        + 0.25 * silence_score
        + 0.15 * boundary_score
        + 0.05 * text_score
    )
    return candidate
```

provider 适配保持轻量：

```python
@dataclass(frozen=True, slots=True)
class VoiceCloneSamplePolicy:
    name: str = "default"
    min_total_ms: int = 10_000
    preferred_total_ms: int = 24_000
    max_total_ms: int = 45_000
    preferred_clip_min_ms: int = 4_000
    preferred_clip_max_ms: int = 12_000
    max_silence_ratio: float = 0.35


VOICE_CLONE_SAMPLE_POLICIES = {
    "default": VoiceCloneSamplePolicy(name="default"),
    "cosyvoice": VoiceCloneSamplePolicy(name="cosyvoice_default", max_total_ms=45_000),
    "minimax": VoiceCloneSamplePolicy(name="minimax_default", max_total_ms=60_000),
    "mock": VoiceCloneSamplePolicy(name="mock", min_total_ms=1_000, max_total_ms=5_000),
}
```

不要在 Phase 1 引入重 registry。provider key 到 policy 的简单映射足够可测、可替换。

选择流程：

1. 按现有逻辑构建候选区间。
2. 给所有候选区间打分。
3. 按 provider policy 选择最佳候选集合。
4. 提取选中的 interval。
5. 写 manifest。
6. 继续跑现有 `validate_sample()` 作为最终 gate。

hard reject 行为必须写死：

- 如果所有候选都进入 `hard_reject_reasons`，extractor 返回 `None` 或抛出明确异常。
- 调用方必须降级为预设音色、复用已有用户音色或进入人工审核。
- 禁止放宽阈值后再次抽样并调用付费 clone API。
- 禁止自动切换到另一个 clone provider 继续调用付费 API。
- manifest 中保留 reject 原因，便于后续审核和调参。

#### 测试计划

- duration/RMS/silence/boundary 的 candidate scoring 单测。
- manifest 序列化单测。
- 旧 `extract_sample()` 调用方不受影响。
- fixture：较短干净片段胜过较长噪声片段。
- fixture：所有候选都低质时，不发生任何 clone HTTP 调用。

#### 验收标准

- 同输入输出 deterministic。
- clone extraction 运行时写 manifest。
- 现有用户授权/admin gate 不变。
- provider-specific limit 可配置，不侵入 pipeline 架构。
- 所有候选 hard reject 时只能降级/审核，不能自动触发付费克隆 API。

### 4.4 P1：有限 TTS 尾部静音裁剪

#### 代码现状

`fit_audio_to_slot()` 已经做 edge silence trim、限制范围内 tempo、padding 和 truncation。但默认 silence trim 更偏向短音频。长 TTS 音频如果带尾部静音，仍可能进入变速、截断或 rewrite。

#### 为什么优化

TTS 服务经常生成可变长尾部静音。尾部静音不是语义语音。如果超时主要由尾部静音造成，最佳修复是先做有边界的 tail trim，再考虑变速或 rewrite。

这符合架构：它是确定性 DSP 预处理，不是 LLM timing。

#### 依据

其他 dubbing 项目常用 VAD trim。当前项目不需要引入完整 VAD 栈，但 tail-only bounded trim 可以拿到主要收益，风险更低。

#### 预期效果

- 减少不必要 rewrite。
- 减少对高倍速 DSP 的依赖。
- 减少因尾部静音导致的 capped DSP/truncation。
- 音频适配指标更清楚。

#### 关键实现草案

扩展 `src/utils/audio_fit.py` 的 `FitPolicy`：

```python
@dataclass(frozen=True)
class FitPolicy:
    min_atempo: float = 0.8
    max_atempo: float = 1.5
    silence_trim_enabled: bool = True
    silence_trim_max_ms: int = 3000
    tail_trim_enabled: bool = True
    tail_trim_max_ms: int = 1200
    tail_trim_keep_silence_ms: int = 120
    tail_trim_fade_out_ms: int = 15
    tail_trim_apply_when_long: bool = True
    tail_trim_min_improvement_ms: int = 200
```

增加显式指标：

```python
@dataclass(frozen=True)
class FitResult:
    path: Path
    initial_duration_ms: int
    trimmed_duration_ms: int
    stretched_duration_ms: int
    final_duration_ms: int
    speed_ratio: float
    silence_padded_ms: int
    truncated_ms: int
    edge_trimmed_ms: int = 0
    tail_trimmed_ms: int = 0
```

兼容规则：

- 保留 `trimmed_duration_ms` 的现有语义：所有 trim 操作后的 duration。
- 新增 `edge_trimmed_ms` 和 `tail_trimmed_ms`，让报表可以区分常规 edge trim 和长音频 tail trim。
- alignment audit 单独传播 `tail_trimmed_ms`；现有消费 `trimmed_duration_ms` 的代码 Phase 1 不需要改。

tail trim 决策：

```python
def _should_apply_tail_trim(
    initial_ms: int,
    target_ms: int,
    candidate_ms: int,
    policy: FitPolicy,
) -> bool:
    before = abs(initial_ms - target_ms)
    after = abs(candidate_ms - target_ms)
    return before - after >= policy.tail_trim_min_improvement_ms
```

流程：

1. 测 initial duration。
2. 短音频继续走现有 edge trim。
3. 如果音频仍然过长且 tail trim 开启，只检测 trailing silence。
4. tail trim 只能删除音频最右侧连续静音；遇到任何不低于 30 ms 的非静音 chunk 必须停止，不能跨越内部停顿继续向左裁剪。
5. 裁剪不超过 `tail_trim_max_ms`。
6. 只有当接近 target 的改善超过 `tail_trim_min_improvement_ms` 时才应用。
7. 保留 `tail_trim_keep_silence_ms` 的尾部静音，避免剪映轨道拼接显得被硬切。
8. 如果不新增依赖即可实现，可加非常短 fade-out。
9. 继续正常 atempo/pad/truncate。

#### 测试计划

- 长音频带 900 ms 尾部静音且超时，应触发 tail trim。
- 长音频没有尾部静音，不应改变。
- tail trim 不超过配置上限。
- tail trim 保留 100-150 ms 静音 buffer。
- 包含 `voice + 内部 500 ms 静音 + voice + 800 ms 尾静音` 的 fixture，trim 后只能去掉最右侧尾静音，不能误剪内部停顿或第二段语音。
- 短音频现有 edge trim 行为不变。
- alignment audit 记录 `edge_trimmed_ms` 和 `tail_trimmed_ms`。

#### 验收标准

- 不引入完整 VAD 依赖。
- 不改变 semantic block timing。
- rewrite 数不能增加。
- DSP audit 能区分 tail trim、speed ratio、truncation。

### 4.5 P1：Whisper/DTW 匹配质量分

#### 代码现状

`cue_pipeline._try_whisper_aligned_cues()` 在 gate 允许时可以用 faster-whisper + DTW char alignment。失败时会回退到确定性比例字幕。但 accepted alignment 缺少质量指标。

#### 为什么优化

一个 Whisper/DTW 结果可能非空，但质量仍然弱：

- 很多目标字符没有锚点。
- 识别文本只匹配了部分目标文本。
- timing 由很少的 matched words 推导出来。
- 中英混合、数字、术语可能造成误导匹配。

需要只有高质量匹配才接受强对齐，并把质量写入报告。

#### 依据

吸收 WhisperX 类 forced alignment 的有用部分：word timing 能改善字幕时间。但不能让 Whisper 成为必选，也不能静默相信弱匹配。

#### 预期效果

- 减少错误的“已对齐”字幕。
- fallback 到比例字幕时有原因。
- 可以基于真实 job artifact 调 threshold。

#### 关键实现草案

保持现有 API 兼容，新增带质量返回的函数：

```python
# src/services/whisper_align/dtw.py
@dataclass(frozen=True, slots=True)
class CharAlignmentQuality:
    source_chars: int
    anchored_chars: int
    unanchored_chars: int
    coverage_ratio: float
    normalized_edit_distance: float
    word_span_ms: int
    accepted: bool
    reason_codes: tuple[str, ...] = ()


def align_chars_to_words_with_quality(
    cn_text: str,
    words: list[WhisperWord],
    *,
    min_coverage_ratio: float = 0.72,
    max_edit_distance: float = 0.38,
) -> tuple[list[CharTime], CharAlignmentQuality]:
    char_times = align_chars_to_words(cn_text, words)
    quality = _score_char_alignment(cn_text, words, char_times)
    accepted = (
        quality.coverage_ratio >= min_coverage_ratio
        and quality.normalized_edit_distance <= max_edit_distance
    )
    return char_times if accepted else [], replace(quality, accepted=accepted)
```

阈值来源：

- `min_coverage_ratio=0.72` 和 `max_edit_distance=0.38` 只能作为待校准占位值，不能直接作为生产默认。
- Phase 0 需要用至少 20 个历史成功任务跑现有 Whisper 对齐，记录 coverage/edit-distance 分布后再确定默认阈值。
- 如果校准数据不足，feature flag 开启后也应先 report-only，不改变 accepted/fallback 决策。

文本归一化必须显式并有测试：

- 全角/半角归一。
- Latin 小写化。
- 忽略标点、空格和字幕分隔符。
- CJK 字符作为一等 match 单元。
- 数字和常见本地化数字形式尽量一致处理。
- 允许术语表中的产品名/品牌名在非拉丁目标语言中保留 Latin，不触发 script gate 失败。

集成：

- `cue_pipeline._try_whisper_aligned_cues()` 调用质量函数。
- accepted alignment 把质量摘要写入 subtitle report metadata。
- rejected alignment 回退到比例字幕并记录原因。
- warning 分级：
  - `whisper_aligned`：强对齐通过，不提示普通用户。
  - `proportional_fallback`：Whisper 弱或缺失，但比例 fallback 成功；默认只进高级 QA。
  - `alignment_review_warning`：弱对齐叠加文本/音频漂移、低 coverage 或字幕过密；审核 UI 显示轻量 badge。

#### 测试计划

- 完整 CJK 文本 alignment 通过。
- 部分匹配低于 coverage threshold 时 fallback。
- 空/disjoint word list fallback 并给 reason。
- CJK/Latin 混合归一化得分稳定。
- 旧 `align_chars_to_words()` 调用方兼容。

#### 验收标准

- 比例字幕 fallback 保持确定性。
- Whisper alignment 仍是可选。
- 质量阈值可配置。
- accepted cues 有足够 metadata 用于诊断。

### 4.6 P1：字幕显示宽度 gate

#### 代码现状

字幕 timing 当前基于 speech weight，项目已有 `src/utils/text_width.py` 显示宽度工具，但字幕生成还没有把它作为视觉可读性 gate。

#### 为什么优化

语音 timing 解决“什么时候出现”，不解决“屏幕上是否读得舒服”。中文、英文、数字、全角字符的视觉宽度不同。字幕可以 timing 正确但视觉上过密。

#### 依据

这是吸收 OmniVoice “script gate” 思路的本项目版本：用确定性代码检查视觉/脚本质量，而不是只写在 prompt 里。

#### 预期效果

- 导出/审核前发现字幕可读性问题。
- 更适合中文用户，因为 CJK 显示宽度被明确处理。
- 不把视觉适配交给 LLM。

#### 关键实现草案

第一版 report-only，避免过早改变 segmentation：

```python
# src/modules/subtitles/quality.py
from dataclasses import dataclass

from src.utils.text_width import display_width


@dataclass(frozen=True, slots=True)
class SubtitleWidthIssue:
    cue_index: int
    text: str
    width: int
    max_width: int
    severity: str
    jianying_font_size_used: int | None = None
    advisory_only: bool = True


def find_subtitle_width_issues(
    cues: list[SubtitleCue],
    *,
    max_display_width: int = 32,
) -> list[SubtitleWidthIssue]:
    issues: list[SubtitleWidthIssue] = []
    for index, cue in enumerate(cues):
        width = display_width(cue.text)
        if width > max_display_width:
            issues.append(
                SubtitleWidthIssue(
                    cue_index=index,
                    text=cue.text,
                    width=width,
                    max_width=max_display_width,
                    severity="warning",
                    jianying_font_size_used=None,
                    advisory_only=True,
                )
            )
    return issues
```

初始阈值建议：

- `max_display_width=32` 作为移动端优先 warning 阈值，约等于 16 个全角中文字符。
- 通过 admin/settings 配置，不能永久硬编码。
- 后续应绑定剪映草稿样式事实：画布比例、字体大小、单/双行策略、安全区。
- 第一版报告字段预留 `jianying_font_size_used`，即使当前无法读取剪映字体大小也写 `null`，避免后续 schema 破坏。

集成选择：

- 把 width warnings 写入现有 subtitle quality report。
- 如果 review model 支持 cue-level warning，可标记 `needs_review`。
- 后续再考虑让 semantic segmenter 使用 width 作为 deterministic split constraint。

#### 测试计划

- CJK 宽度大于 ASCII。
- 中英数字混排宽度符合预期。
- 超宽 cue 产生 warning。
- 正常 cue 不产生 warning。

#### 验收标准

- 第一版只 report-only。
- 不用 LLM 重定时。
- 不造成字幕 timing 回归。
- 阈值配置化。

### 4.7 P2：next-gap borrowing 仅作为诊断

#### 代码现状

当前系统把 TTS 音频适配到 source block slot。部分外部项目会借用下一个字幕/音频 segment 的空隙来减少变速。

#### 为什么不自动应用

自动借 gap 会改变源时间轴，让剪映草稿更难推理，也不符合本项目“基于源 block 做确定性数学重定时”的边界。

#### 可用的有限版本

只写报告字段：

```json
{
  "segment_id": 42,
  "overflow_ms": 360,
  "gap_after_ms": 900,
  "borrowable_gap_ms": 250,
  "suggestion": "manual_review_can_extend_audio_tail"
}
```

预期效果：

- 帮人工审核判断哪里可以手动调整。
- 不改变默认输出时间轴。
- 保持 DSP-first invariant。

### 4.8 P2：本地 diarization 仅作为 benchmark research

默认不把 pyannote/WhisperX diarization 加入主路径。如果后续确实需要，只作为离线 benchmark：

- 只通过独立 research command 运行。
- 和现有 ASR/Gemini/verifier speaker evidence 对比。
- 输出 metrics，不自动修改 pipeline。
- 不加入 `pyproject.toml` 默认依赖。

## 5. Feature Flag 矩阵与上线安全

所有行为变更默认关闭。只写 sidecar/report 的功能也需要后端 flag，进入前端审核页的展示还需要对应 `NEXT_PUBLIC_*` gate。

| 功能 | 后端 env | 前端 env | 默认 | 关闭后行为 |
| --- | --- | --- | --- | --- |
| 翻译 script gate | `AVT_TRANSLATION_SCRIPT_GATE` | 无，除非展示质量标记 | off | 走旧 prompt、旧解析、旧校验 |
| 翻译 script gate 付费重试 | `AVT_TRANSLATION_SCRIPT_GATE_RETRY` | 无 | off | script gate 失败直接进审核/降级，不自动重试 |
| Speaker evidence sidecar | `AVT_SPEAKER_EVIDENCE` | `NEXT_PUBLIC_SPEAKER_EVIDENCE_PANEL` | off | 不写 JSONL，pipeline 行为零变化 |
| Voice sample manifest | `AVT_VOICE_SAMPLE_MANIFEST` | 无 | off | 不写样本 manifest |
| Voice sample scoring | `AVT_VOICE_SAMPLE_SCORING` | 无 | off | 仍按当前“最长可用”逻辑选择样本 |
| TTS tail trim | `AVT_AUDIO_TAIL_TRIM` | 无 | off | `fit_audio_to_slot()` 保持现状 |
| Whisper 质量门 | `AVT_WHISPER_QUALITY_GATE` | `NEXT_PUBLIC_ALIGNMENT_WARNINGS` | off | 现有 `_block_is_in_sync` 和 fallback 逻辑不变 |
| 字幕宽度报告 | `AVT_SUBTITLE_WIDTH_REPORT` | `NEXT_PUBLIC_SUBTITLE_WIDTH_WARNINGS` | off | 不写宽度报告，不显示宽度 warning |

上线原则：

- Phase 1a 只允许开启 sidecar/report/manifest 类零行为变更 flag。
- 行为变更 flag 必须先在样本任务和灰度任务里验证，再进入默认配置。
- 任一 flag 关闭后必须回到当前主流程，不得留下半生效状态。
- 前端 flag 关闭时，即使后端产物存在，也不在普通审核 UI 暴露。

## 6. 建议落地阶段

### Phase 0：文档与指标基线

- 落地本方案。
- 补 artifact path/report schema 的短设计说明。
- 用 N >= 10 个历史成功任务收集当前基线，覆盖短视频（< 60s）、中等视频（约 5min）、长视频（> 30min），建议记录到 `tests/fixtures/baseline_jobs.json`。
- 度量输出建议落到 `baselines/2026-05-baseline.json`，格式保持机器可 diff：
  - rewrite count
  - capped DSP count
  - average speed ratio
  - clone sample validation warnings
  - subtitle fallback rate
- Phase 1/2 完成后使用同一任务集重跑，核心指标应改善或持平；如恶化，需要保留 feature flag 关闭路径。

### Phase 1a：纯 sidecar/report/manifest，零行为变更

实施：

- job-scoped reports/manifests 的 artifact contract。
- `src/services/speaker_evidence.py`。
- transcript review 和 pipeline artifact 中写 speaker evidence。
- voice sample manifest 写入，但不改变现有“最长可用”样本选择。
- subtitle width report 写入，但只作为 advisory。
- serialization、speaker evidence、manifest、subtitle width report 单测。

先做 Phase 1a 的原因：

这些改动只增加可观测性，不改变翻译、TTS、对齐、字幕 timing 或付费 API 调用路径，可以更快进入主干收集数据。

### Phase 1b：翻译 gate 与样本 scoring 行为变更

实施：

- 翻译语言全名 prompt 注入、zh-CN script gate、有限 literal-reflect/adapt。
- voice sample scoring 接管候选选择。
- script gate retry 预算、batch 失败率熔断、hard reject 降级路径。
- scoring/script gate/付费 API 防重试单测。

先做 Phase 1a 再做 Phase 1b 的原因：

Phase 1b 会改变 prompt、解析、样本选择或降级路径，必须在 sidecar/report 数据足够后再灰度开启。

### Phase 2：Tail Trim 与字幕质量 gate

实施：

- `src/utils/audio_fit.py` 的 bounded tail trim。
- `edge_trimmed_ms`/`tail_trimmed_ms` 审计传播。
- Whisper/DTW quality scoring。
- 字幕显示宽度 warnings。

先有 Phase 1 artifact 后再做 Phase 2，能更清楚比较前后效果。

### Phase 3：高级建议只进报告

实施：

- next-gap borrowing suggestions。
- 如证据显示 speaker attribution 仍是瓶颈，再增加离线 diarization benchmark command。

这些不是主路径优化，不应阻塞 P0/P1。

## 7. 非目标

本方案不做以下事项：

- 用默认 pyannote/WhisperX diarization 替换现有 speaker 系统。
- 让 Whisper alignment 成为字幕 timing 必选路径。
- 让 LLM 决定字幕时间戳。
- 让翻译 reflect 阶段改写 timing、speaker identity 或 subtitle cue boundaries。
- 借本方案正式开启完整多语言 i18n 架构；本轮只处理当前 zh-CN 主链路质量门。
- 在主路径自动借用相邻 source block 的时间。
- 默认安装 RubberBand、Silero VAD 或其他重音频依赖。
- 把 TTS 单元从 `SemanticBlock` 改成字幕行。
- 把渲染 MP4 作为主交付目标。

## 8. 审核 checklist

实现前后需要确认：

- `main.py` 在干净本地环境可运行。
- `pytest` 不依赖实时外部服务。
- 如有新外部依赖，必须 optional 且 gated。
- 所有新增能力都有后端 feature flag；进入前端审核页的能力还有 `NEXT_PUBLIC_*` gate。
- 翻译 script gate 是确定性代码，且 retry 有上限。
- speaker evidence sidecar 本身不改变 speaker timing。
- voice sample scoring 是确定性的。
- voice sample hard reject 后不得自动调用任何付费 clone API。
- tail trim 有边界，并进入 audit metrics。
- subtitle alignment 有确定性 fallback。
- subtitle width gate 第一版只 report-only。
- 新 reports/manifests 不进 R2、不进剪映 zip，且 `copy_as_new` 不携带源绝对路径。
- 剪映草稿仍是主交付产物。

契约级回归守卫建议：

- `tests/test_speaker_evidence_invariants.py`
  - JSONL 中不出现源 job 绝对路径。
  - sidecar 缺失时主流程不 raise。
- `tests/test_voice_sample_scoring_guards.py`
  - scoring 路径不 import TTS/clone provider API。
  - 所有候选 hard reject 时不发生 clone HTTP 调用。
- `tests/test_audio_fit_tail_trim_guards.py`
  - flag off 时 `fit_audio_to_slot()` 行为保持现状。
  - tail trim 不跨越内部停顿。

## 9. 预计涉及文件

预计新增：

- `src/services/translation_language.py`
- `src/services/speaker_evidence.py`
- `src/modules/subtitles/quality.py`
- `tests/` 下新增 translation script gate、speaker evidence、sample scoring、tail trim、subtitle quality 相关测试。

预计修改：

- `src/services/gemini/translator.py`
- `src/services/transcript_reviewer.py`
- `src/pipeline/process.py`
- `src/services/voice/sample_extractor.py`
- `src/utils/audio_fit.py`
- `src/services/alignment/aligner.py`
- `src/services/whisper_align/dtw.py`
- `src/modules/subtitles/cue_pipeline.py`

潜在 report/artifact 路径：

- `<project_dir>/reports/speaker_evidence.jsonl`
- `<project_dir>/reports/subtitle_quality.json`
- `<project_dir>/smart_clone_samples/<speaker_id>.manifest.json`

Artifact lifecycle 约束：

- 使用现有 `project_dir` 语义，不新增 `final_project_dir` 这类未落地名词。
- 新报告和 manifest 默认不上传 R2，因为它们不是用户主下载交付物。
- 新报告和 manifest 默认不进入剪映 zip，避免污染主交付包。
- `copy_as_new` 时，JSON/JSONL 中不能写入源 job 绝对路径、源 job id 绑定路径或不可迁移路径；只保留 line/segment 的逻辑 id 和相对 artifact 关系。
- retention 策略跟随同 job 的 transcript/report 类产物，不单独延长保留。
- 实现时需要和现有 job artifact root 对齐。关键约束是 job-scoped storage，不写仓库级松散 artifact。

## 10. 已吸收的审核决策

以下决策已纳入实现规划：

1. `speaker_evidence.jsonl` 渐进暴露：先通过 job-scoped reports API 暴露给高级审核/调试面板，再考虑轻量用户 badge。
2. voice clone sample policy 可以按 provider 区分，但 Phase 1 用简单 provider-to-policy map，不做重 registry。
3. 移动端优先字幕显示宽度初始 warning 阈值为 32 visual width units，可配置且 report-only。
4. 弱 Whisper alignment 采用分级 QA 状态，不对每个 fallback 都打扰用户。
5. speaker evidence 同时关联原始 transcript identity 和最终 segment identity，并显式记录 split/merge lineage。
6. tail trim 保留约 100-150 ms 尾部静音；不新增依赖的前提下可做极短 fade-out。
7. 克隆样本 boundary scoring 对相邻 speaker gap 小于 500 ms 的候选强惩罚。
8. 所有新增能力必须有 feature flag，行为变更默认关闭。
9. 本轮 i18n 收缩到 zh-CN 主链路，不把 `cn_text` 暗改成泛化 target text 架构。
10. 所有 clone sample hard reject 时只能降级/审核，不能自动重试付费 clone API。
11. 新报告和 manifest 使用 `<project_dir>` 下 job-scoped storage，不上传 R2，不进入剪映 zip。
12. `<project_dir>/reports/` 由 Job API 暴露：`GET /jobs/{job_id}/reports` 返回白名单报告目录，`GET /jobs/{job_id}/reports/{name}` 只读取白名单报告文件；Gateway 继续通过 `/job-api/jobs/{job_id}/{subpath}` 做用户归属校验，不新增独立权限体系。

## 11. 剩余待确认问题

1. `AVT_TRANSLATION_SCRIPT_GATE_RETRY` 是否在任何付费套餐默认开启，还是长期保持手动灰度开关？
2. 字幕宽度 warning 后续是否要反馈给确定性 segmentation，还是长期保持 review-only？
3. Phase 0 baseline 的历史任务集合由谁确认，是否需要覆盖不同 TTS provider？

## 12. 建议立即执行的下一步

优先实施 Phase 1a：

1. 定义 `<project_dir>/reports/` 和 `<project_dir>/smart_clone_samples/` 的 artifact contract。
2. 增加 `speaker_evidence.jsonl` sidecar。
3. 增加 voice clone sample manifest，但不改变样本选择。
4. 增加 subtitle width report，但只 advisory。
5. 增加对应 feature flags 和契约级 guard tests。

这样可以先提升关键质量区域的可观测性，同时不改变默认 timing、翻译行为、字幕生成、样本选择和外部依赖。等这些 artifact 和 baseline 数据存在后，再进入 Phase 1b 的翻译 gate/样本 scoring 行为变更，以及 Phase 2 的音频/字幕适配优化。

Phase 1a 灰度开启建议：

1. 只在 staging 或内部小批量任务开启观测 flag：`AVT_SPEAKER_EVIDENCE=1`、`AVT_VOICE_SAMPLE_MANIFEST=1`、`AVT_SUBTITLE_WIDTH_REPORT=1`。
2. 保持行为变更 flag 关闭：`AVT_TRANSLATION_SCRIPT_GATE=0`、`AVT_TRANSLATION_SCRIPT_GATE_RETRY=0`、`AVT_VOICE_SAMPLE_SCORING=0`、`AVT_AUDIO_TAIL_TRIM=0`、`AVT_WHISPER_QUALITY_GATE=0`。
3. 用 `GET /job-api/jobs/{job_id}/reports` 检查报告是否生成；用 `GET /job-api/jobs/{job_id}/reports/speaker-evidence` 和 `/subtitle-width` 拉取具体报告。
4. 第一批样本建议覆盖短视频、5 分钟左右中视频、30 分钟以上长视频各至少 3 个，确认 reports 生成不影响 artifact index、R2 发布、剪映 zip 和主流程完成状态。
5. 只有当报告稳定、无明显磁盘/接口噪声后，再进入 Phase 1b 的 script gate 或 sample scoring 灰度。
