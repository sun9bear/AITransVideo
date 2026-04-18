# Probe TTS 校准重构：混合选段 + 双用途试听 + Pipeline 拆分

> **v2 — 2026-04-13** 采纳 Codex 审阅建议：hybrid 选段、probe cache fingerprint、
> 试听文本截断归一化、补充 payload/API/resume 三层测试。

## Context

当前 S4-probe 在音色确认**之后**才做翻译+TTS，选段基于时长(3-8s)。
改进方案：混合选段（word count + 时长 guard）、probe 翻译提前到音色确认前、
翻译结果同时作为试听素材（截断后）。

试听时用户听到的是真实翻译内容而非固定样本文，
校准数据更可靠（per-speaker + per-engine 消除引擎差异）。

## Pipeline 流程变化

```
当前:  Pass3 → voice_auto_match → voice_selection(pause) → S4-probe(translate+TTS) → S3
新:    Pass3 → voice_auto_match → S4-probe-translate → voice_selection(pause) → S4-probe-TTS → calibrate → S3
```

Express 模式：去掉 pause，其余相同。

---

## Part 1: 混合选段（替换 `_select_probe_segments`）

**文件**: `src/pipeline/process.py` (line 3554)

重写 `_select_probe_segments()`，从纯时长过滤改为 **word count 主导 + 时长 guard**：

- **参数**: `min_words=20, max_words=100, min_duration_ms=3000, max_duration_ms=15000, per_speaker=3, max_words_per_speaker=200`
- **逻辑**:
  1. 跳过首尾段（同现在）
  2. **主过滤**: `_count_source_words(line.source_text)` 在 20-100 词（复用 `translator.py:1956` 已有函数）
  3. **时长 guard**: 同时要求 `3s ≤ duration ≤ 15s`（过滤极端语速段落）
  4. 按 speaker 分组，每组优先选中间长度(40-70词)段落，均匀分布
  5. 每 speaker 累计词数不超 200
  6. **渐进降级**: 如果某 speaker 无候选 → 降到 min_words=10 → 再降到 5
  7. 防御性总上限 15 段，按原始顺序排序

**为什么用 word count 而非 char count**: 英文 raw char count 被空格、标点、专有名词放大，
和 spoken density 不对应。`_count_source_words()` 用 `re.findall(r"[A-Za-z0-9']+")`
提取实际单词数，与 S3 主翻译的 density 估算一致。

## Part 2: Pipeline 拆分 — 翻译与 TTS 分离

**文件**: `src/pipeline/process.py`

### 2a. 新方法 `_run_probe_translation()`

从 `_run_probe_tts_calibration()` (line 3619) 拆出翻译部分：
- 调用 `_select_probe_segments()` + `translator.translate_probe()`
- 返回 `list[DubbingSegment]`（含 cn_text）
- **缓存 + fingerprint**: 写入 `{project_dir}/translation/_probe_segments.json`，
  包含 fingerprint（selected segment_ids + source_text hash + model + glossary + video_title/url）。
  Resume 时先校验 fingerprint，不匹配则重新翻译。

**Fingerprint 构建**（复用主翻译 `_build_translation_fingerprint` 模式，`translator.py:492`）：
```python
probe_fingerprint = hashlib.sha256(json.dumps({
    "segment_ids": sorted([s.segment_id for s in probe_lines]),
    "source_texts": [s.source_text for s in probe_lines],
    "model_name": translator.model_name,
    "glossary": glossary or {},
    "video_title": video_title,
    "youtube_url": youtube_url,
}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
```

### 2b. 新方法 `_run_probe_tts_and_calibrate()`

拆出 TTS + 校准部分：
- 接收已翻译的 `probe_segments`
- 应用 `speaker_providers` → TTS → 校准 → 返回 `(global_cps, per_speaker_cps)`

### 2c. `run()` 编排改动

在 **voice_selection gate 之前**（line 1031 后）插入 probe 翻译：

```python
# --- S4-probe Phase 1: 预翻译（音色确认前） ---
_probe_segments: list[DubbingSegment] = []
if not s3_cache_hit:
    _probe_segments = self._run_probe_translation(
        ...,
        cache_dir=final_project_dir / "translation",
    )
```

在 **voice_selection gate 之后**（line 1093 后、现 S4-probe 位置）改为只做 TTS：

```python
# --- S4-probe Phase 2: TTS 校准（音色确认后） ---
if not s3_cache_hit and _probe_segments:
    _probe_cps, _probe_cps_by_speaker = self._run_probe_tts_and_calibrate(
        _probe_segments, tts_generator, tts_dir,
        speaker_providers=_speaker_providers,
    )
```

## Part 3: Probe 翻译写入音色选择 Payload

**文件**: `src/pipeline/process.py`, `_build_voice_selection_review_payload()` (line 1775)

- 新增参数 `probe_segments: list[DubbingSegment] | None = None`
- 构建 `probe_texts_by_speaker: {speaker_id: [{segment_id, source_text, cn_text}]}`
- 每个 speaker payload 加 `"probe_texts": [...]`
- 两个调用点（line 1055 首次构建 + line 1101 过期重建）都传入 `_probe_segments`

## Part 4: Backend 试听支持自定义文本 + 截断归一化

**文件**: `src/services/jobs/review_actions.py`

### 4a. 试听文本归一化

新增 `_normalize_preview_text()` 函数：
```python
_MAX_PREVIEW_CHARS = 80
_MIN_PREVIEW_CHARS = 10

def _normalize_preview_text(text: str | None) -> str:
    """截断 probe 文本用于试听，异常时回退固定样本文。"""
    if not text or len(text.strip()) < _MIN_PREVIEW_CHARS:
        return _PREVIEW_SAMPLE_TEXT
    text = text.strip()
    if len(text) <= _MAX_PREVIEW_CHARS:
        return text
    # 在句号/逗号处截断，避免截断到半句
    for sep in ("。", "，", "、", ",", " "):
        pos = text.rfind(sep, 0, _MAX_PREVIEW_CHARS)
        if pos >= _MIN_PREVIEW_CHARS:
            return text[:pos + 1]
    return text[:_MAX_PREVIEW_CHARS]
```

### 4b. preview_voice 透传

`preview_voice()` (line 255) 新增 `sample_text: str | None = None` 参数：
- `effective_text = _normalize_preview_text(sample_text) if sample_text else _PREVIEW_SAMPLE_TEXT`
- 传递到三个路由：
  - Route 1 `_preview_volcengine_voice(voice_id, text=effective_text)` (line 334)
  - Route 2 `_preview_cosyvoice_voice(voice_id, text=effective_text)` (line 353)
  - Route 3 `verifier.verify_voice(sample_text=effective_text)` (line 296)

**文件**: `src/services/jobs/api.py` (line 321)

从 payload 提取 `sample_text` 传入 `preview_voice()`。

## Part 5: Frontend 试听用 Probe 文本

**文件**: `frontend-next/src/lib/api/voiceSelection.ts`

`previewVoice()` (line 74) options 加 `sampleText?: string`，body 传 `sample_text`。

**文件**: `frontend-next/src/components/workspace/VoiceSelectionPanel.tsx`

- Speaker 解析增加 `probeTexts` 字段（从 payload `probe_texts` 映射）
- `handlePreview` (line 268) 取第一条 probe cn_text 作为 sampleText
  （后端会截断归一化，前端无需处理长度）
- 试听按钮下方显示试听内容摘要（可选 UX 增强）

---

## 关键文件清单

| 文件 | 改动 |
|------|------|
| `src/pipeline/process.py` | 选段重写、pipeline 拆分、payload 增强、probe cache |
| `src/services/jobs/review_actions.py` | preview_voice + helper 加 text 参数 + 截断归一化 |
| `src/services/jobs/api.py` | 透传 sample_text |
| `frontend-next/src/lib/api/voiceSelection.ts` | previewVoice 加 sampleText |
| `frontend-next/src/components/workspace/VoiceSelectionPanel.tsx` | 解析 probeTexts、传入试听 |

**不改的文件**: translator.py (translate_probe 已就绪)、tts_generator.py、duration_estimator.py、S2/Pass3 代码

**复用的现有函数**:
- `_count_source_words()` (`translator.py:1956`) — 选段词数计算
- `_build_translation_fingerprint` 模式 (`translator.py:492`) — probe cache 指纹

## 降级安全

所有 probe 逻辑都在 try/except 内：
- 翻译失败 → `_probe_segments = []` → payload 无 probe_texts → 试听回退固定文本 → TTS 校准回退 4.5
- probe cache fingerprint 不匹配 → 重新翻译（不阻断）
- TTS 失败 → 校准回退 4.5
- 前端 probeTexts 为空 → sampleText=undefined → 后端用默认固定样本文
- 试听文本过短/异常 → `_normalize_preview_text()` 回退固定样本文

## 验证

### 单元测试

1. **选段**: `_select_probe_segments` 的 word count 过滤、时长 guard、渐进降级、per-speaker 上限、max_words_per_speaker
2. **试听截断**: `_normalize_preview_text` 的截断逻辑、句号/逗号断句、过短回退、空值回退

### Payload / API / Resume 测试

3. **Payload 构建**: `_build_voice_selection_review_payload` 传入 probe_segments 后，
   输出 payload 中每个 speaker 含 `probe_texts`，字段完整
4. **Preview API 透传**: `api.py` → `preview_voice()` → 三个 provider helper 均收到自定义 text
5. **Probe cache resume**: 写入 `_probe_segments.json` + fingerprint → 模拟 resume →
   fingerprint 匹配时直接加载、不匹配时重新翻译

### 端到端

6. **Studio**: 提交任务 → probe 翻译日志出现在音色确认前 → 音色面板试听听到截断后的视频翻译内容 →
   确认后 S4-probe TTS + 校准日志正常 → S3 用校准值翻译
7. **Express**: probe 翻译 + TTS + 校准一气呵成，per-speaker 校准值合理
