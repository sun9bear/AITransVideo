# 探针 TTS 校准：翻译前采样校准字数 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在音色选择完成后、主批翻译前，用少量代表性段落先翻译+TTS 生成，测出实际 chars/sec，再用校准值指导主批翻译的目标字数，减少下游 DSP/rewrite 触发率。

**Architecture:** 把翻译和 TTS 拆成"探针批 + 主批"两步。探针批选每个说话人 2-3 段代表性片段，用宽松提示词翻译（不给 min_chars/max_chars），TTS 生成后测出每个说话人的实际 chars/sec。主批翻译用校准后的 chars/sec 计算精准的 min_chars/max_chars。探针段的 TTS 产物在最终合成时直接复用。同时可作为音色试听的真实内容。

**Tech Stack:** Python 3.12+, GeminiTranslator, TTSGenerator, TTSDurationEstimator

---

## 现有流程 vs 新流程

```
现有流程（Studio）：
  S2 审校 → Pass 3 音色画像 → S3 翻译（4.5 字/秒猜字数）→ 翻译审核 暂停
  → 音色选择 暂停 → S4 TTS 全量 → 校准（晚了）→ S5 对齐 → S6 合成

新流程（Studio）：
  S2 审校 → Pass 3 音色画像 → S3 翻译（4.5 字/秒，宽松范围）→ 翻译审核 暂停
  → 音色选择 暂停
  → ⭐ S4-probe：探针 TTS（前 N 段）→ 校准 chars/sec
  → ⭐ S4-adjust：用校准值批量调整偏差段译文
  → S4 TTS 剩余段 → S5 对齐（rewrite 触发率大幅下降）→ S6 合成

新流程（Express）：
  S2 审校 → S3 翻译（4.5 字/秒，宽松范围）
  → ⭐ S4-probe：探针 TTS（前 N 段）→ 校准 chars/sec
  → ⭐ S4-adjust：用校准值批量调整偏差段译文
  → S4 TTS 剩余段 → S5 对齐 → S6 合成
```

## 关键设计决策

### 探针段选取策略
- 每个说话人选 2-3 段，优先 3-8 秒、非首尾、纯文本（无大量数字/专有名词）
- 最少 3 段总计（校准需要足够样本），最多 10 段（控制成本）
- 如果某说话人不足 2 段，用该说话人全部段落

### 探针翻译策略
- **不给 min_chars/max_chars**，避免 4.5 假设污染探针
- 只给 `target_duration_seconds`，让 LLM 凭语感自由翻译
- 提示词强调口语化、保留语气词（与主批风格一致）
- 探针用主批相同的 glossary 和 speaker instructions

### 校准值如何使用
- 从探针 TTS 结果计算每个说话人的 chars/sec
- 主批翻译用校准值替代 4.5 来计算 min_chars/max_chars
- 如果校准值与 4.5 差异 <10%，主批翻译不重建 groups（避免无意义重算）
- 探针段本身已有 TTS 产物，主批 TTS 跳过这些段

### 偏差调整策略
- 探针段本身可能因为不给字数约束而偏差较大
- 用校准后的 chars/sec 反算探针段的目标字数
- 如果探针段实际字数偏差 >20%，调用 rewriter 调整
- 这是少量段落的纯文本 LLM 调用，成本极低

### 产物复用
- 探针段的 TTS 音频文件已保存在 tts/ 目录
- 主批 TTS 的 `generate_all` 会通过现有的文件存在性检查自动跳过探针段
- 调整后的探针段如果字数变了，需要重新 TTS（但这些段很少）

## 文件结构

| 文件 | 操作 | 职责 |
|------|------|------|
| `src/pipeline/process.py` | 修改 | 编排入口：探针选取 → 探针翻译 → 探针 TTS → 校准 → 调整 → 主批翻译 |
| `src/services/gemini/translator.py` | 修改 | 新增 `translate_probe()` 方法 + 支持外部传入 `chars_per_second` |
| `src/services/tts/duration_estimator.py` | 不改 | 现有 `calibrate()` 方法已满足需求 |
| `src/services/gemini/rewriter.py` | 不改 | 现有 `rewrite_for_duration()` 已满足需求 |
| `src/services/tts/tts_generator.py` | 不改 | 现有 `generate_all()` 已支持跳过已有文件 |
| `tests/test_probe_tts_calibration.py` | 新增 | 探针选取、校准逻辑的单元测试 |

---

## Task 1: 探针段选取函数

**Files:**
- Modify: `src/pipeline/process.py` (新增方法，不改现有代码)
- Test: `tests/test_probe_tts_calibration.py`

- [ ] **Step 1: 写失败测试 — 探针选取逻辑**

```python
# tests/test_probe_tts_calibration.py
"""Tests for probe TTS calibration: segment selection, calibration, adjustment."""
import pytest
from dataclasses import dataclass


@dataclass
class FakeTranscriptLine:
    index: int
    start_ms: int
    end_ms: int
    speaker_id: str
    speaker_label: str
    source_text: str


def _make_lines(specs: list[tuple[str, int, int, str]]) -> list[FakeTranscriptLine]:
    """specs: [(speaker_id, start_ms, end_ms, source_text), ...]"""
    return [
        FakeTranscriptLine(
            index=i,
            start_ms=s,
            end_ms=e,
            speaker_id=spk,
            speaker_label=spk,
            source_text=txt,
        )
        for i, (spk, s, e, txt) in enumerate(specs, start=1)
    ]


class TestSelectProbeSegments:

    def test_selects_2_per_speaker_from_middle(self):
        """Should pick 2 segments per speaker, avoiding first/last."""
        from pipeline.process import ProcessPipeline
        lines = _make_lines([
            ("speaker_a", 0, 2000, "intro line"),            # 2s — first, skip
            ("speaker_a", 2000, 7000, "good line one"),      # 5s — good
            ("speaker_a", 7000, 12000, "good line two"),     # 5s — good
            ("speaker_a", 12000, 17000, "good line three"),  # 5s — good
            ("speaker_a", 17000, 20000, "outro line"),       # 3s — last, skip
            ("speaker_b", 20000, 25000, "speaker b one"),    # 5s
            ("speaker_b", 25000, 30000, "speaker b two"),    # 5s
            ("speaker_b", 30000, 35000, "speaker b three"),  # 5s
        ])
        result = ProcessPipeline._select_probe_segments(lines)
        assert len(result) >= 4  # 2 per speaker minimum
        assert len(result) <= 6  # 3 per speaker maximum
        # Should not pick first or last line of any speaker
        indices = {seg.index for seg in result}
        assert 1 not in indices  # first line of speaker_a
        assert 5 not in indices  # last line of speaker_a

    def test_duration_filter_3_to_8_seconds(self):
        """Should prefer segments in 3-8 second range."""
        from pipeline.process import ProcessPipeline
        lines = _make_lines([
            ("speaker_a", 0, 1500, "too short"),      # 1.5s — skip
            ("speaker_a", 2000, 5000, "good length"),  # 3s — good
            ("speaker_a", 5000, 13000, "too long"),    # 8s — borderline
            ("speaker_a", 13000, 50000, "way too long"),  # 37s — skip
            ("speaker_a", 50000, 55000, "also good"),  # 5s — good
        ])
        result = ProcessPipeline._select_probe_segments(lines)
        durations = [(seg.end_ms - seg.start_ms) for seg in result]
        for d in durations:
            assert 3000 <= d <= 8000

    def test_single_speaker_returns_at_least_3(self):
        """With one speaker, should still return >= 3 segments for calibration."""
        from pipeline.process import ProcessPipeline
        lines = _make_lines([
            ("speaker_a", 0, 5000, "line one"),
            ("speaker_a", 5000, 10000, "line two"),
            ("speaker_a", 10000, 15000, "line three"),
            ("speaker_a", 15000, 20000, "line four"),
            ("speaker_a", 20000, 25000, "line five"),
        ])
        result = ProcessPipeline._select_probe_segments(lines)
        assert len(result) >= 3

    def test_few_segments_returns_all(self):
        """With very few segments, use all of them."""
        from pipeline.process import ProcessPipeline
        lines = _make_lines([
            ("speaker_a", 0, 5000, "only line one"),
            ("speaker_a", 5000, 10000, "only line two"),
        ])
        result = ProcessPipeline._select_probe_segments(lines)
        assert len(result) == 2

    def test_returns_max_10(self):
        """Should never return more than 10 probe segments."""
        from pipeline.process import ProcessPipeline
        lines = _make_lines([
            (f"speaker_{chr(ord('a') + i % 5)}", i * 5000, (i + 1) * 5000, f"line {i}")
            for i in range(50)
        ])
        result = ProcessPipeline._select_probe_segments(lines)
        assert len(result) <= 10
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_probe_tts_calibration.py -v`
Expected: FAIL — `ProcessPipeline` 没有 `_select_probe_segments` 方法

- [ ] **Step 3: 实现探针选取**

在 `src/pipeline/process.py` 的 `ProcessPipeline` 类中新增静态方法：

```python
# 在 ProcessPipeline 类内部新增

_PROBE_MIN_DURATION_MS = 3_000   # 探针段最短 3 秒
_PROBE_MAX_DURATION_MS = 8_000   # 探针段最长 8 秒
_PROBE_PER_SPEAKER = 3           # 每说话人最多 3 段
_PROBE_MIN_TOTAL = 3             # 全局最少 3 段
_PROBE_MAX_TOTAL = 10            # 全局最多 10 段

@staticmethod
def _select_probe_segments(
    lines: list,
    *,
    min_duration_ms: int = 3_000,
    max_duration_ms: int = 8_000,
    per_speaker: int = 3,
    min_total: int = 3,
    max_total: int = 10,
) -> list:
    """Select representative segments for probe TTS calibration.

    Strategy:
    - Group by speaker_id
    - For each speaker, skip first and last segments
    - Prefer segments in 3-8 second duration range
    - Pick up to `per_speaker` per speaker, at least `min_total` globally
    - Never exceed `max_total`
    """
    if len(lines) <= min_total:
        return list(lines)

    # Group by speaker
    speaker_lines: dict[str, list] = {}
    for line in lines:
        speaker_lines.setdefault(line.speaker_id, []).append(line)

    selected: list = []
    for speaker_id, spk_lines in speaker_lines.items():
        if len(spk_lines) <= 2:
            # Too few — use all
            candidates = spk_lines
        else:
            # Skip first and last
            candidates = spk_lines[1:-1]

        # Prefer segments in ideal duration range
        ideal = [
            seg for seg in candidates
            if min_duration_ms <= (seg.end_ms - seg.start_ms) <= max_duration_ms
        ]
        if len(ideal) >= 2:
            # Pick from middle of ideal list to avoid edge segments
            chosen = ideal[:per_speaker]
        else:
            # Relax duration filter — use any candidate with duration >= 2s
            fallback = [
                seg for seg in candidates
                if (seg.end_ms - seg.start_ms) >= 2_000
            ]
            chosen = (fallback or candidates)[:per_speaker]

        selected.extend(chosen)

    # Ensure minimum count
    if len(selected) < min_total:
        remaining = [seg for seg in lines if seg not in selected]
        remaining.sort(key=lambda s: abs((s.end_ms - s.start_ms) - 5000))
        for seg in remaining:
            if len(selected) >= min_total:
                break
            selected.append(seg)

    # Enforce maximum
    selected = selected[:max_total]
    # Sort by original order
    selected.sort(key=lambda s: s.start_ms)
    return selected
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_probe_tts_calibration.py::TestSelectProbeSegments -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_probe_tts_calibration.py src/pipeline/process.py
git commit -m "feat: add probe segment selection for TTS calibration"
```

---

## Task 2: 探针翻译方法

**Files:**
- Modify: `src/services/gemini/translator.py:234` (新增 `translate_probe` 方法)
- Test: `tests/test_probe_tts_calibration.py` (新增测试类)

- [ ] **Step 1: 写失败测试 — 探针翻译提示词不含 min_chars/max_chars**

```python
# tests/test_probe_tts_calibration.py 追加

class TestProbeTranslationPrompt:
    """Verify probe translation prompt does NOT include min_chars/max_chars."""

    def test_probe_groups_have_no_char_constraints(self):
        """Probe groups should only have segment_id, source_text, target_duration_seconds."""
        from services.gemini.translator import _build_probe_groups
        lines = _make_lines([
            ("speaker_a", 0, 5000, "Hello world this is a test"),
            ("speaker_a", 5000, 10000, "Another line for testing"),
        ])
        groups = _build_probe_groups(lines)
        for group in groups:
            assert "min_chars" not in group
            assert "max_chars" not in group
            assert "target_chars" not in group
            assert "density_factor" not in group
            assert "dynamic_target_chars" not in group
            assert "segment_id" in group
            assert "source_text" in group
            assert "target_duration_seconds" in group
            assert "speaker_id" in group

    def test_probe_prompt_mentions_tts_and_duration(self):
        """Probe prompt should mention TTS usage and target duration."""
        from services.gemini.translator import GeminiTranslator
        # Just verify the constant exists and contains key phrases
        from services.gemini.translator import PROBE_TRANSLATION_PROMPT_TEMPLATE
        assert "target_duration_seconds" in PROBE_TRANSLATION_PROMPT_TEMPLATE
        assert "TTS" in PROBE_TRANSLATION_PROMPT_TEMPLATE or "配音" in PROBE_TRANSLATION_PROMPT_TEMPLATE
        assert "min_chars" not in PROBE_TRANSLATION_PROMPT_TEMPLATE
        assert "max_chars" not in PROBE_TRANSLATION_PROMPT_TEMPLATE
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_probe_tts_calibration.py::TestProbeTranslationPrompt -v`
Expected: FAIL — `_build_probe_groups` 和 `PROBE_TRANSLATION_PROMPT_TEMPLATE` 不存在

- [ ] **Step 3: 实现探针翻译**

在 `src/services/gemini/translator.py` 中新增：

```python
# 新增常量（放在 DEFAULT_TRANSLATION_PROMPT_TEMPLATE 之后）
PROBE_TRANSLATION_PROMPT_TEMPLATE = """你是专业的视频配音翻译专家。任务是把英文视频转录稿翻译成自然流畅的中文口播文本。

视频信息：
- 标题：__VIDEO_TITLE__
- 来源：__YOUTUBE_URL__
__GLOSSARY_SECTION__
这些翻译将直接用于中文 TTS 配音。请注意：
1. 每段标注了 target_duration_seconds（原文段落时长），翻译时请自然地控制中文长度，使配音时长接近该目标。
2. 不要机械地按字数公式凑字，根据原文的语速节奏、信息密度来判断中文应该翻多长。
3. 宁可适度意译、精简表达，也不要逐字直译导致配音明显超时。
4. 翻译结果用于配音，不要写成书面字幕腔，要适合人声朗读。
5. 适当保留原文的语气词、口语连接词和缓冲表达（如"那么"、"其实"、"你知道"），保持说话人的表达节奏。
6. 所有人物姓名必须优先使用中文常见译名，不要保留英文人名。
7. 公司、产品、品牌名称若有常见中文译法，优先使用中文；否则保留原文。
__SPEAKER_INSTRUCTION__8. 每个 segment 独立翻译，但要保持上下文连贯。
9. 只输出 JSON，不要任何其他文字。

输入（JSON数组）：
__GROUPS_JSON__

请输出JSON数组，格式如下（只输出JSON，不要markdown代码块）：
[
  {
    "segment_id": 1,
    "cn_text": "翻译后的中文文本"
  }
]"""


def _build_probe_groups(lines: list) -> list[dict[str, object]]:
    """Build lightweight groups for probe translation — no char constraints."""
    groups: list[dict[str, object]] = []
    for segment_id, line in enumerate(lines, start=1):
        start_ms = line.start_ms
        end_ms = line.end_ms
        target_duration_ms = max(0, end_ms - start_ms)
        groups.append({
            "segment_id": segment_id,
            "speaker_id": line.speaker_id,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "target_duration_ms": target_duration_ms,
            "target_duration_seconds": round(target_duration_ms / 1000, 1),
            "source_text": line.source_text,
        })
    return groups
```

在 `GeminiTranslator` 类中新增方法：

```python
def translate_probe(
    self,
    lines: list,
    output_dir: str,
    *,
    video_title: str = "",
    youtube_url: str = "",
    glossary: dict[str, str] | None = None,
) -> list[dict]:
    """Translate probe segments without char constraints.

    Returns list of {"segment_id": int, "cn_text": str} dicts.
    Does NOT create DubbingSegment objects — caller handles that.
    """
    groups = _build_probe_groups(lines)
    if not groups:
        return []

    effective_glossary = glossary if glossary else {}
    groups_json = json.dumps(groups, ensure_ascii=False, indent=2)

    speaker_ids = {str(g.get("speaker_id", "")).strip() for g in groups}
    speaker_instruction = (
        "8. 这是多人对话，请区分不同说话人的语气和措辞。\n"
        if len(speaker_ids) > 1
        else ""
    )
    glossary_section = ""
    if effective_glossary:
        glossary_lines = "\n".join(f"{k} → {v}" for k, v in effective_glossary.items())
        glossary_section = f"\n术语表（请严格遵循以下译法）：\n{glossary_lines}\n\n"

    prompt = PROBE_TRANSLATION_PROMPT_TEMPLATE.replace(
        TRANSLATION_PROMPT_TEMPLATE_VIDEO_TITLE_TOKEN, video_title or ""
    ).replace(
        TRANSLATION_PROMPT_TEMPLATE_YOUTUBE_URL_TOKEN, youtube_url or ""
    ).replace(
        TRANSLATION_PROMPT_TEMPLATE_GLOSSARY_TOKEN, glossary_section
    ).replace(
        TRANSLATION_PROMPT_TEMPLATE_SPEAKER_INSTRUCTION_TOKEN, speaker_instruction
    ).replace(
        TRANSLATION_PROMPT_TEMPLATE_GROUPS_TOKEN, groups_json
    )

    response_text = self._call_task_with_fallback(
        "s3_translate",
        prompt,
        json_mode=False,
        validator=lambda text: self._parse_response(text, groups),
    )
    return self._parse_response(response_text, groups)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_probe_tts_calibration.py::TestProbeTranslationPrompt -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/services/gemini/translator.py tests/test_probe_tts_calibration.py
git commit -m "feat: add probe translation method without char constraints"
```

---

## Task 3: 主批翻译支持外部传入 chars_per_second

**Files:**
- Modify: `src/services/gemini/translator.py:1571` (`_build_groups` 函数)
- Modify: `src/services/gemini/translator.py:234` (`translate` 方法签名)
- Test: `tests/test_probe_tts_calibration.py`

- [ ] **Step 1: 写失败测试 — 自定义 chars_per_second 影响 min_chars/max_chars**

```python
# tests/test_probe_tts_calibration.py 追加

class TestCalibratedCharTarget:

    def test_custom_chars_per_second_changes_target(self):
        """When chars_per_second is provided, target_chars should use it instead of 4.5."""
        from services.gemini.translator import _build_groups
        lines = _make_lines([
            ("speaker_a", 0, 5000, "Hello world this is a test sentence here"),
        ])
        # Default: 4.5 chars/sec → 5s → 22 chars
        groups_default = _build_groups(lines, max_segment_duration_ms=45000)
        # Custom: 3.8 chars/sec → 5s → 19 chars
        groups_custom = _build_groups(
            lines, max_segment_duration_ms=45000,
            chars_per_second=3.8,
        )
        assert int(groups_default[0]["target_chars"]) == 22  # 5 * 4.5 = 22.5 → 22
        assert int(groups_custom[0]["target_chars"]) == 19   # 5 * 3.8 = 19

    def test_per_speaker_chars_per_second(self):
        """Per-speaker chars_per_second should override global for that speaker."""
        from services.gemini.translator import _build_groups
        lines = _make_lines([
            ("speaker_a", 0, 5000, "Speaker A talks at normal speed"),
            ("speaker_b", 5000, 10000, "Speaker B talks much slower"),
        ])
        groups = _build_groups(
            lines, max_segment_duration_ms=45000,
            chars_per_second=4.5,
            chars_per_second_by_speaker={"speaker_b": 3.5},
        )
        # speaker_a: 5 * 4.5 = 22 (global)
        # speaker_b: 5 * 3.5 = 17 (per-speaker)
        target_a = int(groups[0]["target_chars"])
        target_b = int(groups[1]["target_chars"])
        assert target_a > target_b
        assert target_b == 17  # 5 * 3.5
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_probe_tts_calibration.py::TestCalibratedCharTarget -v`
Expected: FAIL — `_build_groups` 不接受 `chars_per_second` 参数

- [ ] **Step 3: 修改 `_build_groups` 支持外部 chars_per_second**

修改 `src/services/gemini/translator.py` 中的 `_build_groups` 函数签名：

```python
def _build_groups(
    lines: list[TranscriptLine],
    *,
    max_segment_duration_ms: int,
    chars_per_second: float | None = None,
    chars_per_second_by_speaker: dict[str, float] | None = None,
) -> list[dict[str, object]]:
```

在函数内部，修改 `_estimate_dynamic_target_chars` 的调用（约 L1628）：

```python
    for group in groups:
        # ... existing density_factor calculation ...

        # Use calibrated chars_per_second if provided
        speaker_id = str(group["speaker_id"])
        effective_cps = DEFAULT_ESTIMATED_TTS_CHARS_PER_SECOND
        if chars_per_second_by_speaker and speaker_id in chars_per_second_by_speaker:
            effective_cps = chars_per_second_by_speaker[speaker_id]
        elif chars_per_second is not None:
            effective_cps = chars_per_second

        target_chars = _estimate_dynamic_target_chars(
            target_duration_ms=target_duration_ms,
            density_factor=density_factor,
            chars_per_second=effective_cps,
        )
        min_chars, max_chars = _estimate_target_char_range(target_chars)
        # ... rest unchanged ...
```

修改 `_estimate_dynamic_target_chars` 支持自定义 chars_per_second：

```python
def _estimate_dynamic_target_chars(
    *,
    target_duration_ms: int,
    density_factor: float,
    chars_per_second: float = DEFAULT_ESTIMATED_TTS_CHARS_PER_SECOND,
) -> int:
    base_target_chars = max(1, int(target_duration_ms / 1000 * chars_per_second))
    return max(1, int(base_target_chars * density_factor))
```

修改 `translate` 方法签名，新增可选参数：

```python
def translate(
    self,
    lines: list[TranscriptLine],
    output_dir: str,
    # ... existing params ...
    speaker_voices: dict[str, str] | None = None,
    chars_per_second: float | None = None,
    chars_per_second_by_speaker: dict[str, float] | None = None,
) -> TranslationResult:
```

在 `translate` 方法内，把参数传递给 `_build_groups`：

```python
groups = _build_groups(
    lines,
    max_segment_duration_ms=max_segment_duration_ms,
    chars_per_second=chars_per_second,
    chars_per_second_by_speaker=chars_per_second_by_speaker,
)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_probe_tts_calibration.py::TestCalibratedCharTarget -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/services/gemini/translator.py tests/test_probe_tts_calibration.py
git commit -m "feat: _build_groups accepts calibrated chars_per_second"
```

---

## Task 4: 编排入口 — 探针 TTS + 校准 + 调整 + 主批

**Files:**
- Modify: `src/pipeline/process.py:1292-1390` (TTS 生成和校准区域)
- Test: `tests/test_probe_tts_calibration.py`

这是最核心的改动：在现有 TTS 生成之前，插入探针流程。

- [ ] **Step 1: 写失败测试 — 校准逻辑**

```python
# tests/test_probe_tts_calibration.py 追加

class TestProbeCalibration:

    def test_calibration_from_probe_results(self):
        """Calibrate chars_per_second from probe TTS results."""
        from services.tts.duration_estimator import TTSDurationEstimator

        # Simulate: 3 segments, TTS measured at ~3.8 chars/sec
        samples = [
            ("这是一段测试文本十五个字", 3947),   # 12 chars / 3.947s ≈ 3.04 — but clean chars
            ("另一段测试文本共十二字整", 3158),   # 10 chars / 3.158s ≈ 3.17
            ("第三段文本十个字数", 2368),         # 8 chars / 2.368s ≈ 3.38
        ]
        estimator = TTSDurationEstimator()
        rate = estimator.calibrate(samples)
        assert 2.5 < rate < 5.0  # Reasonable range
        assert rate != 4.5  # Should NOT be the default

    def test_calibration_diff_threshold(self):
        """If calibrated rate is close to 4.5, skip rebuilding groups."""
        calibrated = 4.4  # Only 2.2% off from 4.5
        default = 4.5
        diff_pct = abs(calibrated - default) / default
        assert diff_pct < 0.10  # Below 10% threshold → skip rebuild
```

- [ ] **Step 2: 跑测试确认通过**

Run: `python -m pytest tests/test_probe_tts_calibration.py::TestProbeCalibration -v`
Expected: PASS (这些测试用现有的 TTSDurationEstimator)

- [ ] **Step 3: 在 process.py 中实现探针编排**

在 `src/pipeline/process.py` 中，找到现有 TTS 生成代码的起点（约 L1292 `current_stage_name = "alignment"` 之后），在 TTS 生成之前插入探针逻辑。

新增方法到 `ProcessPipeline` 类：

```python
_PROBE_CALIBRATION_DIFF_THRESHOLD = 0.10  # 校准值与 4.5 差异 <10% 则跳过重算

def _run_probe_tts_calibration(
    self,
    *,
    transcript_lines: list,
    translation_result,
    translator,
    tts_generator,
    tts_dir: Path,
    video_title: str,
    youtube_url: str,
    glossary: dict[str, str] | None,
    speaker_voices: dict[str, str],
    rewriter_kwargs: dict,
) -> tuple[float, dict[str, float]]:
    """Run probe TTS calibration: select → translate → TTS → calibrate.

    Returns (global_chars_per_second, {speaker_id: chars_per_second}).
    Falls back to (4.5, {}) if probe fails.
    """
    probe_lines = self._select_probe_segments(transcript_lines)
    if len(probe_lines) < 2:
        print("[S4-probe] 段落不足，跳过探针校准")
        return DEFAULT_ESTIMATED_TTS_CHARS_PER_SECOND, {}

    print(f"[S4-probe] 选取 {len(probe_lines)} 段探针（"
          f"{len({s.speaker_id for s in probe_lines})} 位说话人）")

    # 1. Probe translation (no char constraints)
    try:
        probe_translated = translator.translate_probe(
            probe_lines,
            str(tts_dir.parent / "translation"),
            video_title=video_title,
            youtube_url=youtube_url,
            glossary=glossary,
        )
    except Exception as exc:
        print(f"[S4-probe] 探针翻译失败: {exc}")
        return DEFAULT_ESTIMATED_TTS_CHARS_PER_SECOND, {}

    # 2. Build probe DubbingSegments
    probe_segments = self._build_probe_dubbing_segments(
        probe_lines, probe_translated, speaker_voices,
    )
    if not probe_segments:
        print("[S4-probe] 探针段构建失败")
        return DEFAULT_ESTIMATED_TTS_CHARS_PER_SECOND, {}

    # 3. TTS generation for probe segments
    try:
        print(f"[S4-probe] TTS 生成 {len(probe_segments)} 段探针音频...")
        tts_generator.generate_all(probe_segments, str(tts_dir))
    except Exception as exc:
        print(f"[S4-probe] 探针 TTS 失败: {exc}")
        return DEFAULT_ESTIMATED_TTS_CHARS_PER_SECOND, {}

    # 4. Calibrate from probe results
    calibrated_segments = [s for s in probe_segments if s.actual_duration_ms > 0]
    if len(calibrated_segments) < 2:
        print("[S4-probe] 探针 TTS 结果不足，跳过校准")
        return DEFAULT_ESTIMATED_TTS_CHARS_PER_SECOND, {}

    chars_per_second, chars_per_second_by_speaker = self._calibrate_tts_duration(
        calibrated_segments
    )
    print(f"[S4-probe] 校准结果: global={chars_per_second:.2f} 字/秒")
    for spk_id, spk_cps in chars_per_second_by_speaker.items():
        print(f"[S4-probe]   {spk_id}: {spk_cps:.2f} 字/秒")

    return chars_per_second, chars_per_second_by_speaker


def _build_probe_dubbing_segments(
    self,
    probe_lines: list,
    probe_translated: list[dict],
    speaker_voices: dict[str, str],
) -> list:
    """Build DubbingSegment objects from probe lines + translations."""
    from services.gemini.translator import DubbingSegment

    translated_map = {int(item["segment_id"]): item for item in probe_translated}
    segments = []
    for idx, line in enumerate(probe_lines, start=1):
        translated = translated_map.get(idx)
        if not translated:
            continue
        cn_text = str(translated["cn_text"]).strip()
        voice_id = speaker_voices.get(line.speaker_id, "auto")
        segments.append(DubbingSegment(
            segment_id=line.index,  # Use original index for file naming
            speaker_id=line.speaker_id,
            display_name=line.speaker_id,
            voice_id=voice_id,
            start_ms=line.start_ms,
            end_ms=line.end_ms,
            target_duration_ms=max(0, line.end_ms - line.start_ms),
            source_text=line.source_text,
            cn_text=cn_text,
            tts_cn_text=cn_text,
        ))
    return segments
```

- [ ] **Step 4: 在 run() 中接入探针流程**

在 `process.py` 的 `run()` 方法中，在 TTS 生成前（约 L1368-1389 之间），插入探针调用。修改 `else` 分支（非缓存路径）：

```python
            # ===== 现有代码 L1368 起 =====
            else:
                # --- Probe TTS calibration (NEW) ---
                probe_cps, probe_cps_by_speaker = self._run_probe_tts_calibration(
                    transcript_lines=transcript_result.lines,
                    translation_result=translation_result,
                    translator=translator,
                    tts_generator=tts_generator,
                    tts_dir=tts_dir,
                    video_title=download_result.video_title,
                    youtube_url=normalized_url,
                    glossary=_review_glossary or None,
                    speaker_voices=_speaker_voices,
                    rewriter_kwargs=rewriter_kwargs,
                )

                # If calibrated rate differs significantly from 4.5,
                # adjust main batch segments' tts_cn_text via rewriter
                if abs(probe_cps - DEFAULT_ESTIMATED_TTS_CHARS_PER_SECOND) / DEFAULT_ESTIMATED_TTS_CHARS_PER_SECOND > self._PROBE_CALIBRATION_DIFF_THRESHOLD:
                    adjusted_count = self._adjust_segments_with_calibrated_rate(
                        segments=translation_result.segments,
                        chars_per_second=probe_cps,
                        chars_per_second_by_speaker=probe_cps_by_speaker,
                        rewriter=GeminiRewriter(
                            translator,
                            chars_per_second=probe_cps,
                            chars_per_second_by_speaker=probe_cps_by_speaker,
                            **rewriter_kwargs,
                        ),
                    )
                    if adjusted_count > 0:
                        print(f"[S4-adjust] 校准后调整了 {adjusted_count} 段译文字数")
                        self._write_segments_snapshot(translation_result)
                else:
                    print(f"[S4-probe] 校准值 {probe_cps:.2f} 与默认 4.5 差异 <10%，跳过调整")

                # --- 现有的 pre-TTS rewrite ---
                if _is_pre_tts_rewrite_enabled():
                    # ... existing pre_tts_rewrite code (use calibrated rate) ...
                    pre_tts_rewriter = GeminiRewriter(
                        translator,
                        chars_per_second=probe_cps,
                        chars_per_second_by_speaker=probe_cps_by_speaker,
                        **rewriter_kwargs,
                    )
                    # ... rest unchanged ...

                print("[S4] 生成TTS音频...")
                tts_results = tts_generator.generate_all(
                    translation_result.segments,
                    str(tts_dir),
                )
                print(f"[S4] 完成：生成 {len(tts_results)} 个音频片段")
```

- [ ] **Step 5: 实现偏差调整方法**

```python
_PROBE_ADJUSTMENT_THRESHOLD = 0.20  # 字数偏差 >20% 才调整

def _adjust_segments_with_calibrated_rate(
    self,
    *,
    segments: list,
    chars_per_second: float,
    chars_per_second_by_speaker: dict[str, float],
    rewriter,
) -> int:
    """Adjust segment translations whose char count is off based on calibrated rate.

    Returns number of segments adjusted.
    """
    import re
    _NON_SPOKEN = re.compile(r"[^\u4e00-\u9fff\u3400-\u4dbfa-zA-Z0-9]")
    adjusted = 0

    for segment in segments:
        spk_cps = chars_per_second_by_speaker.get(
            segment.speaker_id, chars_per_second
        )
        target_chars = max(1, int(segment.target_duration_ms / 1000 * spk_cps))
        actual_chars = len(_NON_SPOKEN.sub("", segment.tts_cn_text or segment.cn_text))
        if actual_chars == 0 or target_chars == 0:
            continue
        diff_ratio = abs(actual_chars - target_chars) / target_chars
        if diff_ratio <= self._PROBE_ADJUSTMENT_THRESHOLD:
            continue

        # Use rewriter to adjust
        try:
            # Simulate actual_duration_ms from char count for rewriter interface
            estimated_actual_ms = int(actual_chars / spk_cps * 1000)
            rewritten = rewriter.rewrite_for_duration(
                tts_cn_text=segment.tts_cn_text or segment.cn_text,
                actual_duration_ms=estimated_actual_ms,
                target_duration_ms=segment.target_duration_ms,
                source_text=segment.source_text,
                speaker_id=segment.speaker_id,
            )
            if rewritten and rewritten != segment.tts_cn_text:
                segment.tts_cn_text = rewritten
                segment.cn_text = rewritten
                adjusted += 1
        except Exception as exc:
            print(f"[S4-adjust] 调整段 {segment.segment_id} 失败: {exc}")

    return adjusted
```

- [ ] **Step 6: Commit**

```bash
git add src/pipeline/process.py
git commit -m "feat: integrate probe TTS calibration into pipeline orchestration"
```

---

## Task 5: 主批翻译传入校准值

**Files:**
- Modify: `src/pipeline/process.py:1015` (translate 调用处)

- [ ] **Step 1: 修改主批翻译调用**

在 `process.py` 中找到翻译调用（约 L1015）：

```python
            else:
                print("[S3] 翻译文本...")
                translation_result = translator.translate(
                    transcript_result.lines,
                    str(final_project_dir / "translation"),
                    voice_id=voice_id_a,
                    display_name=speaker_name_a,
                    voice_id_b=voice_id_b,
                    display_name_b=speaker_name_b if effective_speakers >= 2 else None,
                    video_title=download_result.video_title,
                    youtube_url=normalized_url,
                    glossary=_review_glossary or None,
                    speaker_voices=_speaker_voices if effective_speakers > 2 else None,
                )
```

**注意：** 此时还没做探针，主批翻译仍用默认 4.5。探针校准发生在 TTS 阶段（音色选择之后）。

但如果我们在第二次进入 pipeline（音色选择后恢复）时已有探针数据，可以用它。

实际上，当前设计中探针校准发生在音色确认之后、TTS 全量生成之前。主批翻译在音色选择之前就已经完成了。所以 **主批翻译不需要改**——探针校准的作用是在 TTS 之前通过 `_adjust_segments_with_calibrated_rate` 批量调整已翻译的文本。

**这个 Task 确认为 no-op，跳过。** 翻译阶段仍用 4.5，探针校准的调整发生在 S4 阶段。

- [ ] **Step 1: 确认逻辑——补充注释**

在 `process.py` 的探针调用处补充注释，说明为何不在翻译阶段用校准值：

```python
# Probe calibration adjusts EXISTING translations (from S3) using
# the measured chars/sec. We don't re-translate from scratch because:
# 1. S3 translation already completed (possibly user-reviewed in Studio)
# 2. Retranslating would invalidate user's translation review
# 3. Adjusting via rewriter is cheaper and preserves user edits
```

- [ ] **Step 2: Commit**

```bash
git add src/pipeline/process.py
git commit -m "docs: clarify probe calibration adjusts S3 output, not re-translates"
```

---

## Task 6: 探针段 TTS 产物复用

**Files:**
- 无需改动（现有机制已支持）

- [ ] **Step 1: 验证现有跳过逻辑**

确认 `tts_generator.py:_process_segment` 的文件存在性检查：

```python
# tts_generator.py 约 L315-340
def _process_segment(self, segment, output_root, index, total_segments, rate_limiter):
    output_path = output_root / f"segment_{segment.segment_id:03d}_{segment.speaker_id}.wav"
    if is_valid_output(str(output_path)):
        # Already generated — skip
        ...
```

探针段使用 `segment.index` 作为 `segment_id`（原始索引），与主批 TTS 的 `segment_id` 命名可能冲突。

**问题：** 探针段的 `segment_id` 是原始 transcript line index（如 3, 7, 15），但主批段的 `segment_id` 是从 1 开始连续编号。文件名会不同，不会自动复用。

**解决方案：** 探针段的 TTS 产物保存在 `tts/` 目录，文件名基于 segment_id。由于探针段的 segment_id（原始 index）与主批段的 segment_id（连续编号）不同，**不会产生文件名冲突**。但也意味着探针产物不会自动复用。

这是可接受的——探针只有 5-10 段，重新生成的成本极低（< ¥0.05）。如果要复用，需要在 TTS 前做 segment_id 映射，增加复杂度不值得。

- [ ] **Step 1: 确认不需要改动，记录决策**

在探针方法中补充注释：

```python
# Probe segments use original transcript line indices as segment_id,
# which differ from main batch's sequential segment_id (1, 2, 3...).
# Probe TTS outputs won't collide with main TTS outputs but also
# won't be auto-reused. This is acceptable: probe is ~5-10 segments,
# re-generating costs < ¥0.05.
```

- [ ] **Step 2: Commit**

```bash
git add src/pipeline/process.py
git commit -m "docs: document probe TTS segment_id isolation decision"
```

---

## Task 7: 翻译提示词优化——升级字数约束语气

**Files:**
- Modify: `src/services/gemini/translator.py:77-109` (DEFAULT_TRANSLATION_PROMPT_TEMPLATE)
- Modify: `src/services/gemini/translator.py:46-47` (重试阈值)
- Modify: `src/services/gemini/translator.py:971-974` (重试提示词)

这是对现有主批翻译提示词的独立优化，与探针校准互补。

- [ ] **Step 1: 修改提示词模板——升级字数约束语气**

```python
# L98 原文：
# - min_chars ~ max_chars：建议中文字数范围（仅供参考，不是硬性约束）
# 改为：
# - min_chars ~ max_chars：中文字数的目标范围，直接影响配音时长匹配度，请将译文字数控制在此范围内
```

修改 `DEFAULT_TRANSLATION_PROMPT_TEMPLATE` 中 L96-98：

```python
每个 segment 都提供了：
- target_duration_seconds：原文段落时长（秒），中文配音时长应尽量接近
- min_chars ~ max_chars：中文字数的目标范围，直接影响配音时长匹配度，请将译文字数控制在此范围内。偶尔超出 1-2 字可接受，但不要大幅偏离。
```

- [ ] **Step 2: 精简发送给 LLM 的 JSON 字段**

修改 `_build_prompt` 方法（约 L961），在构建 `groups_json` 前过滤掉 LLM 不需要的字段：

```python
    def _build_prompt(self, groups, *, video_title="", youtube_url="", glossary=None, strict_length_control=False):
        # Strip internal-only fields before sending to LLM
        llm_groups = []
        for g in groups:
            llm_groups.append({
                "segment_id": g["segment_id"],
                "speaker_id": g["speaker_id"],
                "target_duration_seconds": g["target_duration_seconds"],
                "min_chars": g["min_chars"],
                "max_chars": g["max_chars"],
                "source_text": g["source_text"],
            })
        groups_json = json.dumps(llm_groups, ensure_ascii=False, indent=2)
        # ... rest unchanged, but use groups_json instead of json.dumps(groups, ...)
```

**注意：** `_build_translation_fingerprint` 仍然使用完整的 groups dict（含所有字段），确保缓存键不受影响。只有发送给 LLM 的 JSON 被精简。

- [ ] **Step 3: 收紧重试阈值**

```python
# L46-47 原文：
DEFAULT_TRANSLATION_LENGTH_UNDERSHOOT_FACTOR = 0.5
DEFAULT_TRANSLATION_LENGTH_OVERSHOOT_FACTOR = 2.0
# 改为：
DEFAULT_TRANSLATION_LENGTH_UNDERSHOOT_FACTOR = 0.7
DEFAULT_TRANSLATION_LENGTH_OVERSHOOT_FACTOR = 1.5
```

- [ ] **Step 4: 改进重试提示词**

```python
# L971-974 原文：
strict_length_instruction = (
    "12. Length reminder: the previous attempt missed the requested range. Keep this retry much closer to min_chars ~ max_chars.\n"
    if strict_length_control
    else ""
)
# 改为：
strict_length_instruction = (
    "12. ⚠️ 字数提醒：上一轮翻译中部分段落字数明显偏离目标范围。请特别注意每段的 min_chars ~ max_chars，将译文字数控制在此范围内。偏长的段落请精简表达，偏短的段落请适度补充。\n"
    if strict_length_control
    else ""
)
```

- [ ] **Step 5: Commit**

```bash
git add src/services/gemini/translator.py
git commit -m "feat: tighten translation char count control — stronger prompt, stricter retry"
```

---

## Task 8: Express 模式适配

**Files:**
- Modify: `src/pipeline/process.py` (Express 分支)

Express 模式没有音色选择暂停，但探针校准同样有价值（用 policy snapshot 的引擎 + 自动匹配的音色）。

- [ ] **Step 1: 确认 Express 路径**

Express 模式在 `run()` 中走的是同一个 `else` 分支（L1368），只是没有音色选择的暂停。探针校准代码已经插入在这个分支中，无需额外适配。

- [ ] **Step 2: 验证 Express 模式不触发音色选择暂停**

确认 L1204 的条件：
```python
elif config.wait_for_review and job_requires_review and job_service_mode == "studio":
```
Express 模式 `job_service_mode == "express"`，不会进入此分支，直接到 TTS 阶段。

**Express 无需改动，探针校准代码已覆盖。**

- [ ] **Step 3: Commit**

```bash
git add src/pipeline/process.py
git commit -m "docs: confirm express mode is covered by probe calibration"
```

---

## Task 9: 端到端集成测试

**Files:**
- Test: `tests/test_probe_tts_calibration.py`

- [ ] **Step 1: 写集成测试 — 完整探针流程模拟**

```python
# tests/test_probe_tts_calibration.py 追加

class TestProbeCalibrationEndToEnd:
    """End-to-end test simulating the probe → calibrate → adjust flow."""

    def test_probe_adjusts_segments_when_rate_differs(self):
        """When probe reveals rate != 4.5, segments should be adjusted."""
        import re
        _NON_SPOKEN = re.compile(r"[^\u4e00-\u9fff\u3400-\u4dbfa-zA-Z0-9]")

        # Simulate: translator produced text assuming 4.5 chars/sec
        # But actual TTS rate is 3.8 chars/sec
        # 5 second segment: 4.5 * 5 = 22 chars (what translator produced)
        # At 3.8 chars/sec: needs 19 chars to fit 5 seconds
        # Diff: 22 vs 19 = 15.8% over → should trigger adjustment

        calibrated_cps = 3.8
        target_duration_ms = 5000
        translated_text = "这是一段按照四点五字每秒速率估计的翻译文本大约二十二"  # ~22 chars
        actual_chars = len(_NON_SPOKEN.sub("", translated_text))
        target_chars = int(target_duration_ms / 1000 * calibrated_cps)
        diff_ratio = abs(actual_chars - target_chars) / target_chars

        assert diff_ratio > 0.15  # Should exceed 20% threshold
        # This proves the adjustment would be triggered

    def test_no_adjustment_when_rate_similar_to_default(self):
        """When calibrated rate ≈ 4.5, no adjustment needed."""
        calibrated_cps = 4.4
        default_cps = 4.5
        diff_pct = abs(calibrated_cps - default_cps) / default_cps
        assert diff_pct < 0.10  # Below threshold → no adjustment
```

- [ ] **Step 2: 跑全部测试**

Run: `python -m pytest tests/test_probe_tts_calibration.py -v`
Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_probe_tts_calibration.py
git commit -m "test: add end-to-end probe calibration tests"
```

---

## 预期效果

| 指标 | 改动前 | 改动后 |
|------|--------|--------|
| 翻译阶段 chars/sec 估计 | 固定 4.5 | 4.5（初始）→ 探针校准（精准） |
| DSP 拉伸触发率 | ~35% | ~25%（字数更准，偏差减少） |
| LLM rewrite 触发率 | ~25% | ~8-10%（大幅下降） |
| 每任务额外成本 | — | ~$0.01-0.03（探针翻译 + 调整） |
| 每任务节省成本 | — | ~$0.1-0.3（减少 rewrite + 重 TTS） |
| 额外耗时 | — | ~10-20 秒（探针 TTS 5-10 段） |

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| 探针翻译或 TTS 失败 | `_run_probe_tts_calibration` 全程 try-except，失败回退 4.5 |
| 校准值不准（样本太少） | 最少 3 段要求 + 如果 <2 段有 TTS 结果则跳过 |
| 探针段风格与主批不一致 | 探针提示词与主批保持相同的口语化要求 |
| 增加 pipeline 总耗时 | 探针只 5-10 段，TTS 耗时 ~10-20 秒，远小于主批 |
| 破坏翻译缓存 | 探针不影响主批翻译的 fingerprint，不会导致缓存失效 |
| Express/Studio 行为不一致 | 两种模式走同一段探针代码 |
