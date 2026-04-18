# 音色语速预标定方案（Voice Speed Pre-Calibration）

> 状态：方案设计完成，待实施
> 日期：2026-04-13
> 预估成本：~¥54（一次性）
> 预估工时：标定脚本 + Pipeline 改造 + DB migration

## 1. 问题背景

当前 Pipeline 用 probe TTS 校准测量每个说话人的 chars/sec：翻译几个 probe 段 → TTS → 测时长 → 算 chars_per_second。

**问题：**
- Probe 样本太少（每 speaker 仅 1-2 段），噪声大（10-12% 偏差）
- S5 rewrite 率仍高达 36-57%
- 不同引擎/音色语速差异大（MiniMax ~4.1、CosyVoice ~3.5、VolcEngine ~5.0 字/秒）
- Probe 是间接测量（受翻译质量影响）

**核心洞察：** 真正需要的就一个数据——用户所选音色说中文的 chars/sec。提前标定好，查表即可。

## 2. 标定范围与成本

### 音色数量

| 引擎/档位 | 音色数 | 备注 |
|-----------|--------|------|
| MiniMax Turbo (speech-2.8-turbo) | 81 | 中文音色（59 普通话 + 22 粤语） |
| MiniMax HD (speech-2.8-hd) | 81 | 同一批音色，不同模型 |
| CosyVoice flash (cosyvoice-v3-flash) | 65 | 中文音色 |
| VolcEngine 2.0 (seed-tts-2.0) | 33 | uranus + saturn 音色 |
| **合计** | **260 组** | 179 个音色 × 部分双档 |

**不做：** CosyVoice plus、VolcEngine 1.0

### 计费精算

计费规则差异：
- MiniMax / CosyVoice：1 汉字 = 2 计费字符，标点 = 1 计费字符
- VolcEngine：1 汉字 = 1 字符，标点 = 1 字符

| 引擎/档位 | 音色 | 计费字符/voice | 单价 | 费用 |
|-----------|------|---------------|------|------|
| MiniMax Turbo | 81 | 458×2+48 = 964 | ¥2/万 | ¥15.6 |
| MiniMax HD | 81 | 964 | ¥3.5/万 | ¥27.3 |
| CosyVoice flash | 65 | 964 | ¥1/万 | ¥6.3 |
| VolcEngine 2.0 | 33 | 458+48 = 506 | ¥3/万 | ¥5.0 |
| **合计** | **260** | | | **¥54.2** |

共 260 × 3 = **780 次 TTS 调用**，预计耗时 ~40 分钟。

## 3. 标准测试文本

三段不同长度、不同场景、不同情绪的标准中文口播文本：

### T1 科技评测（101 汉字）
情绪：好奇 → 兴奋 → 惊叹

> 这款手机的屏幕素质让我很震惊。色彩通透，对比度极高，黑色几乎和关屏没有区别。拿来和上一代对比，亮度提升了将近四成，户外强光下也能看得清楚。最让我意外的是功耗居然还降低了，续航多了将近两小时。这块屏幕，确实是今年旗舰里最强的。

### T2 纪录片旁白（153 汉字）
情绪：平静叙述 → 紧张 → 悲伤 → 温暖感动

> 每年十一月，数以万计的藏羚羊从可可西里腹地向南迁徙。这是一段漫长而危险的旅程，它们要穿越结冰的河流，躲避狼群的追捕，忍受零下三十度的严寒。曾经因为盗猎，藏羚羊数量一度不足两万只。那些年，巡护员冒着生命危险日夜巡逻，有人为此献出了生命。如今种群已恢复到三十万只以上，每到迁徙季节，绵延几十公里的生命长河再次出现在高原上，壮观而又令人动容。

### T3 创业演讲（204 汉字）
情绪：低落沮丧 → 犹豫动摇 → 转折惊喜 → 坚定 → 幽默释然

> 三年前辞职创业的时候，卡里只剩四万块。产品上线第一个月，日活用户只有七个人，其中三个是我们自己。合伙人说要不算了吧，回去上班至少能还房贷。那天晚上我确实动摇了，躺在出租屋里盯着天花板想了一整夜。但第二天早上打开后台，发现一个陌生用户连续用了我们的产品四个小时。我给他发消息问感觉怎么样，他回了一句：要是再完善一点，我愿意付费。就这句话，让我决定继续干下去。后来产品打磨了三个月，终于拿到第一笔融资。现在回头看，最庆幸的不是拿到了钱，而是那天没删掉后台。

**统计：** 458 汉字 + 48 标点 = 506 总字符/voice

## 4. 实施方案

### Phase A：DB Migration（012）

**新文件：** `gateway/alembic/versions/012_add_voice_speed_calibration.py`

在 `voice_catalog` 表新增三列：

| 列名 | 类型 | 说明 |
|------|------|------|
| `chars_per_second` | Float, nullable | 默认查询值（所有模型的均值或单模型值） |
| `chars_per_second_by_model` | JSONB, nullable | 按模型存储：`{"speech-2.8-turbo": 4.32, "speech-2.8-hd": 4.18}` |
| `speed_calibrated_at` | DateTime(tz), nullable | 最近一次标定时间 |

**修改：** `gateway/voice_catalog_models.py` — VoiceCatalog 类加对应 mapped_column

**设计决策：用 JSONB 而非多个 scalar 列**——MiniMax 有 Turbo/HD 两个模型，未来可能新增模型，JSONB 不需要改 schema。`chars_per_second` scalar 列作为快速查询的默认值。

### Phase B：标定脚本

**新文件：** `gateway/scripts/calibrate_voice_speeds.py`
**新文件：** `gateway/scripts/standard_calibration_texts.py`（三段标准文本常量）

核心逻辑：
```python
for provider, model, synth_fn, rpm_limit in CALIBRATION_TARGETS:
    voices = query_voice_catalog(provider=provider, matchable=True)
    for voice in voices:
        samples = []
        for text_name, text in STANDARD_TEXTS.items():
            audio_bytes = synth_fn(text, voice.voice_id, model=model)
            duration_ms = ffprobe_duration(audio_bytes)
            hanzi_count = count_spoken_chars(text)  # 复用 _NON_SPOKEN_CHAR_PATTERN
            samples.append((hanzi_count, duration_ms))
        
        total_chars = sum(s[0] for s in samples)
        total_ms = sum(s[1] for s in samples)
        cps = total_chars / (total_ms / 1000)
        
        # 合理性检查：2.0 - 8.0 chars/sec
        if 2.0 <= cps <= 8.0:
            update_voice_catalog(voice.voice_id, model, cps)
        time.sleep(60 / rpm_limit)  # 限速
```

标定目标（4 轮）：

| 轮次 | Provider | Model | 音色数 | RPM |
|------|----------|-------|--------|-----|
| 1 | minimax | speech-2.8-turbo | 81 | 20 |
| 2 | minimax | speech-2.8-hd | 81 | 20 |
| 3 | cosyvoice | cosyvoice-v3-flash | 65 | 180 |
| 4 | volcengine | seed-tts-2.0 | 33 | 60 |

TTS 调用复用现有 provider：
- MiniMax：`src/services/tts/tts_generator.py` 的 HTTP POST 模式
- CosyVoice：`src/services/tts/cosyvoice_provider.py` 的 `synthesize()`
- VolcEngine：`src/services/tts/volcengine_tts_provider.py` 的 `synthesize()`

CLI 参数：
- `--dry-run`：只打印计划，不调 API
- `--provider <name>`：只标定某个引擎
- `--voice-id <id>`：标定单个音色
- `--model <name>`：只标定某个模型
- `--force`：覆盖已有标定值
- `--output-csv <path>`：输出 CSV 供分析

### Phase C：API 更新

**修改：** `gateway/voice_catalog_api.py`

1. 内部端点 `GET /api/internal/voice-catalog` 的返回值加入：
   ```python
   "chars_per_second": v.chars_per_second,
   "chars_per_second_by_model": v.chars_per_second_by_model,
   ```

2. 新增管理端点 `POST /api/admin/voices/recalibrate-speed`：
   - 支持单音色或按 provider 批量重新标定
   - 调用标定逻辑（单音色 ~15s，可内联执行）

### Phase D：Pipeline 集成 — 查表替代 Probe

**修改：** `src/pipeline/process.py`

新增方法：
```python
def _lookup_catalog_chars_per_second(
    self,
    speaker_voices: dict[str, str],  # speaker_id -> voice_id
    tts_model: str | None = None,
) -> tuple[float | None, dict[str, float]]:
    """从音色目录查询预标定的 chars_per_second。
    
    查找优先级：chars_per_second_by_model[tts_model] > chars_per_second
    克隆音色（不在 voice_catalog 中）返回 None。
    """
```

修改 S4 校准流程（~lines 1179-1203）：

```
当前流程：
  probe 翻译 → probe TTS → 校准 chars/sec → 翻译用校准值

改造后：
  ① 查音色目录 chars/sec
  ② 如果所有 speaker 都有目录值 → 直接用，跳过 probe TTS
  ③ 如果部分/全部没有（克隆音色等）→ fallback 到 probe TTS 校准
  ④ 合并：目录值（有的 speaker）+ probe 值（没有的 speaker）
```

**chars/sec 来源优先级：**
1. 音色目录预标定值（by_model 精确匹配）
2. 音色目录预标定值（scalar 默认值）
3. Probe TTS 校准值
4. 默认 4.5

### Phase E：Express 模式优化

**修改：** `src/pipeline/process.py`

当 `job_service_mode == "express"` 且所有 speaker 都有目录 CPS 时：
- 跳过 probe 翻译（省 1 次 LLM 调用）
- 跳过 probe TTS（省 2-6 次 TTS 调用 + ~5-15 秒延迟）
- 直接用目录值进入翻译阶段

### Phase F：不需要改动的模块

以下模块**无需改动**，因为它们接收上游传入的 chars_per_second 参数：

| 模块 | 文件 | 原因 |
|------|------|------|
| 翻译器 | `translator.py` `_build_groups()` | 已接收 chars_per_second 参数 |
| 字数估算 | `translator.py` `_estimate_target_char_range()` | 纯计算，不关心数据来源 |
| Pre-TTS 重写 | `rewriter.py` | 已接收 chars_per_second 参数 |
| Post-TTS 重写 | `rewriter.py` | 同上 |
| 时长估算器 | `duration_estimator.py` | 构造函数已接收 chars_per_second |
| 音频对齐 | `aligner.py` | 用实际音频时长，不依赖 chars/sec |

### Phase G：Post-TTS 重校准（保留）

TTS 后的重校准（process.py ~lines 1441-1464）**继续保留**：
- 实际 TTS 产出的时长仍是最准确的数据
- 目录值用于 TTS 前（翻译字数计算）
- 实际值用于 TTS 后（S5 重写判断）

## 5. 关键文件清单

| 操作 | 文件 |
|------|------|
| **新建** | `gateway/alembic/versions/012_add_voice_speed_calibration.py` |
| **新建** | `gateway/scripts/calibrate_voice_speeds.py` |
| **新建** | `gateway/scripts/standard_calibration_texts.py` |
| **修改** | `gateway/voice_catalog_models.py` — 加 3 个列 |
| **修改** | `gateway/voice_catalog_api.py` — 内部端点返回 + admin 重标定端点 |
| **修改** | `src/pipeline/process.py` — 查表逻辑 + probe fallback + Express 优化 |

## 6. 执行顺序

1. ✅ Migration 012 + Model 更新（纯 additive，零影响）
2. ✅ 标准文本文件
3. ✅ 标定脚本开发 + 测试（dry-run）
4. ⚠️ **运行标定**（需调付费 API，~¥54，~40 分钟）— 需用户确认
5. ✅ API 更新
6. ✅ Pipeline 集成
7. ✅ 端到端测试

## 7. 风险与缓解

| 风险 | 缓解措施 |
|------|---------|
| 目录值对特定内容类型不准 | Post-TTS 重校准仍运行，目录值只影响翻译阶段 |
| TTS 引擎静默更新语速 | `speed_calibrated_at` 时间戳 + admin 重标定端点，建议每月检查 |
| Gateway 查询失败 | 返回 None，fallback 到 probe 校准，零回归 |
| 混合场景（部分目录/部分克隆） | per-speaker 合并：目录值 + probe 值 |
| 标定脚本中某个音色 TTS 失败 | 跳过该音色，记录日志，后续可单独重试 |

## 8. 预期收益

- **精度提升**：从 1-2 个 probe 样本（10-12% 噪声）→ 3 段标准文本 458 汉字（预期 <5% 噪声）
- **S5 重写率**：预期从 36-57% 降至 25-35%
- **Express 延迟**：省去 probe TTS，减少 5-15 秒
- **Express 成本**：省去 2-6 次 probe TTS 调用（~¥0.02-0.05/job）
