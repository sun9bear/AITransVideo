# 音色语速预标定方案

## 当前问题

### 重写率仍然偏高（36-57%）

尽管已经做了 probe TTS 校准，S5 rewrite 率仍然高。根因：

1. **Probe 样本太少**：每 speaker 只有 1-2 个 probe 段，per-speaker chars/sec 噪声大
   - 实测：probe 校准 VolcEngine speaker_b = 5.48 字/秒，实际 TTS 后 = 4.98（偏差 10%）
   - probe 校准 MiniMax speaker_a = 3.66，实际 = 4.14（偏差 12%）

2. **不同引擎语速差异大**：同一段文本在不同引擎上语速完全不同
   - MiniMax: ~4.1 字/秒
   - CosyVoice: ~3.5 字/秒
   - VolcEngine: ~5.0 字/秒

3. **不同音色语速也不同**：同一引擎内，不同音色的语速也有差异

4. **Probe 间接测量**：当前 probe 是翻译原文 → TTS → 测时长，受翻译质量影响，不是纯粹测音色语速

### 核心洞察

真正需要的就一个数据：**用户所选的这个音色，说中文的语速是多少 chars/sec**。
有了这个数据，结合原文段落时长，就能精确计算每段应该翻译多少个中文字。

## 优化方案：音色语速预标定

### 思路

对音色库里所有可用的中文音色，**提前用标准文本做一次 TTS，测出 chars/sec**，存到音色库。
用户选音色后，直接查表得到 chars/sec，零成本、零延迟、零噪声。

### 执行步骤

#### 1. 设计标准测试文本

- 需要一段**有代表性的中文口播文本**，100-200 字
- 包含不同类型的内容：陈述句、疑问句、数字、人名
- 避免过短（测量噪声大）或过长（浪费 TTS 调用费）
- 可以准备 2-3 段不同风格的文本，取平均值更稳

#### 2. 编写标定脚本

遍历三个引擎所有中文音色：
- **MiniMax**：604 音色中的中文音色
- **CosyVoice**：~60 中文音色
- **VolcEngine 2.0**：~30 音色

对每个音色：
1. 用标准文本调用 TTS
2. 用 ffprobe 测量生成音频时长
3. 计算 chars/sec = len(标准文本) / 时长秒数
4. 存储结果

#### 3. 存储方案

**方案 A**：扩展 Gateway 的 `voice_catalog` 表，加 `chars_per_second` 列
- 优点：和现有音色数据在一起，查询方便
- 缺点：需要 DB migration

**方案 B**：独立 JSON 文件 `/opt/aivideotrans/config/voice_speed_catalog.json`
- 优点：简单，不需要 DB 改动，脚本直接写入
- 缺点：和 voice_catalog 分离

**方案 C**：扩展 `voice_labels` 表（Gateway 已有的音色标签表）
- 加一个 `speed_chars_per_sec` 标签

**建议方案 A**——和音色数据在一起最合理。

#### 4. 翻译阶段改造

当前流程：
```
probe 翻译 → probe TTS → 校准 chars/sec → 翻译用校准值
```

改造后：
```
用户选音色 → 查音色库 chars/sec → 翻译直接用精确值
```

- `_estimate_target_char_range` 和 `_build_groups` 接收 per-speaker chars/sec
- chars/sec 来源优先级：音色库预标定 > probe 校准 > 默认 4.5

#### 5. Probe 的去留

**不需要完全废掉 probe**，但角色变了：
- **音色库有预标定值**：直接用，跳过 probe TTS 校准
- **音色库没有**（新音色、克隆音色）：fallback 到 probe 校准
- **Probe 翻译保留**：仍然用于音色选择阶段的试听文本

### 需要注意的问题

1. **MiniMax 克隆音色没有预标定**：用户克隆的音色不在音色库里，需要在克隆完成后单独测速，或 fallback 到 probe 校准

2. **音色语速可能随文本内容变化**：标准文本测出的 chars/sec 是个近似值，实际翻译文本的语速会有波动。需要评估标准文本和真实内容的语速差异

3. **定期更新**：引擎更新可能改变音色语速。需要定期重新标定（比如每月一次）或在引擎版本更新时触发

4. **标定脚本的成本**：
   - MiniMax 中文音色约 200 个 × 200 字 = 40000 字 ≈ ¥8
   - CosyVoice 60 个 × 200 字 = 12000 字 ≈ ¥2.4
   - VolcEngine 30 个 × 200 字 = 6000 字 ≈ ¥1.8
   - 总计约 ¥12，一次性成本

5. **并发限制**：标定脚本不能并发太快，受各引擎 RPM 限制
   - MiniMax: RPM 20
   - CosyVoice: RPM 需确认
   - VolcEngine: RPM 需确认

6. **不同质量档位**：MiniMax 有高级音质(30点/分钟)和旗舰音质(50点/分钟)，语速可能不同。VolcEngine 有 1.0 和 2.0。需要分别标定还是统一？

7. **VolcEngine 的特殊性**：VolcEngine 支持语速参数（speed_ratio），标定时用默认语速（1.0）

8. **音色选择面板展示**：标定后可以在音色选择面板展示 chars/sec，帮助用户理解不同音色的语速特征

## 当前会话已完成的改动

本次会话的所有提交（`codex/review-guidelines` 分支，从 `d17e4a4` 到 `ca8851e`）：

### Probe TTS 校准重构
- 选段从时长过滤改为 hybrid 字数(20-100词)+时长(3-60s)
- Pipeline 拆分：probe 翻译提前到音色确认前，TTS 校准留在确认后
- 截断 fallback：超长段落句子边界截断 + 词级时间戳精确 end_ms
- 全 speaker 覆盖：首尾段落的 speaker 也通过截断 fallback 覆盖
- probe_texts 写入音色选择 payload，前端试听用真实翻译内容
- preview API 支持 sample_text
- probe cache 带 SHA256 fingerprint（含时长）
- probe TTS 前应用用户确认的 voice_id/tts_provider
- per-speaker 最小样本数降至 1

### Phase 4 翻译提示词优化
- 字数引导收紧："仅供参考" → "请将译文字数控制在此范围内"
- JSON 精简：只发 6 个字段给 LLM
- 重试直判：删除 0.5x/2.0x factor，超出 min/max 即重试
- 字数范围可配置化：admin_settings.json 读取 min/max factor
- 重试提示词改为中文具体指导

### 后台管理
- 探针翻译提示词接入 admin 管理（probe_translate prompt key）
- 系统设置页增加翻译字数范围 factor 配置
- Gemini 3.1 Flash Lite 注册到模型库
- Express 模式恢复 S2 Pass1 说话人识别

### MiMo-V2-Omni 音频支持 + 智能重试
- MiMo supports_audio: True，base64 音频通过 input_audio 发送
- 智能重试：临时性错误(429/500/超时) → 等3s重试同模型；输出错误(JSON截断) → 直接降级
- 两层降级兜底：cheapest → second_cheapest
- Gemini 音频传入改为 inline bytes（移除 files.upload，解决 Vertex AI 截断）

### Gemini 优化
- max_output_tokens 从 8192 调至 65536（解决 Vertex AI thinking token 占满输出预算）
- thinking_config(thinking_budget=1024) 控制 thinking token 消耗

### Bug fixes
- 语义拆分子段继承父段 tts_provider（解决 CosyVoice 音色发给 MiniMax 报错）
- 修复智能重试引入的 NameError（stale 变量引用 + process.py logger 未定义）
