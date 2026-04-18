# VolcEngine 豆包音色目录扩充与 Profiling 方案

**Date:** 2026-04-02

**Status:** Fully Implemented (all phases complete as of 2026-04-03)

**Related Docs:**
- `docs/specs/2026-04-01-volcengine-dual-mode-refactor-plan.md`
- `docs/specs/2026-03-30-cosyvoice-routing-and-voice-matching-design.md`
- `docs/handover/2026-04-01-session-handover.md`

## 1. 目标

在已完成的双模式改造（Block A + Block B）基础上，扩充豆包音色目录至官方完整列表，并按 CosyVoice B2 同等标准完成 voice profiling 和 rerank，使豆包 matcher 的匹配精度达到 CosyVoice 当前水平。

## 2. 当前状态

### 2.1 已完成

- `volcengine_voice_catalog.py`：1.0 × 17 + 2.0 × 18 = 35 个音色
- `volcengine_voice_selector.py`：B1 baseline 四级回退（style override → base map → gender-only → fallback）
- `voice_match_resolver.py`：共享 resolver 已接入 VolcEngine 主路径
- Generator 双模式：express → 1.0, studio → 2.0, speaker cache, mismatch retry
- 前端 VoiceReviewPanel：studio 用户选音色下拉

### 2.2 当前状态（2026-04-03 更新）

| 维度 | CosyVoice | 豆包 | 状态 |
|------|-----------|------|------|
| Catalog 规模 | 68 声音（DB 动态） | **338**（DB 动态，1.0×302 + 2.0×36） | ✅ 双方均已切 DB |
| 标注方式 | 管理后台触发 Gemini | 管理后台触发 Gemini（文本 + 音频） | ✅ 完全对齐 |
| Profiling | 管理后台批量触发 | 管理后台批量触发（异步任务队列） | ✅ 管理后台可操作 |
| Rerank | 4 维度 rerank | 4 维度 rerank（maturity/childlike/pitch/texture） | ✅ 已激活 |
| 管理后台 | 验证 + 标注 + 筛选 | 验证 + 标注 + 筛选 + 批量操作 | ✅ 完整 |

## 3. 官方音色实测发现

### 3.1 2.0 Saturn 系列兼容性

2026-04-02 在 US 主机容器内实测：

| 组合 | 结果 |
|------|------|
| `saturn_zh_female_keainvsheng_tob` + `seed-tts-2.0` | ✅ 成功 (155,056 bytes) |
| `saturn_zh_female_keainvsheng_tob` + `seed-tts-1.0` | ❌ 55000000 mismatch |
| `saturn_zh_male_shuanglangshaonian_tob` + `seed-tts-2.0` | ✅ 成功 (129,566 bytes) |
| `saturn_zh_male_shuanglangshaonian_tob` + `seed-tts-1.0` | ❌ 55000000 mismatch |

**结论**：Saturn 音色属于 2.0 resource，voice_id 格式不同（`saturn_*_tob`）但完全兼容 `seed-tts-2.0`。

### 3.2 voice_id 格式分类

根据官方完整列表，1.0 音色有多种后缀：

| 后缀格式 | 示例 | 约数量 | Resource |
|----------|------|--------|----------|
| `_moon_bigtts` | `zh_female_shuangkuaisisi_moon_bigtts` | ~40 | 1.0 |
| `_mars_bigtts` | `zh_female_cancan_mars_bigtts` | ~35 | 1.0 |
| `_emo_v2_mars_bigtts` | `zh_male_lengkugege_emo_v2_mars_bigtts` | ~22 | 1.0 |
| `_conversation_wvae_bigtts` | `zh_male_xudong_conversation_wvae_bigtts` | ~10 | 1.0 |
| `ICL_*_tob` | `ICL_zh_female_wenrounvshen_239eff5e8ffa_tob` | ~80 | 1.0 |
| `_emo_mars_bigtts` | `zh_male_guangzhoudege_emo_mars_bigtts` | ~2 | 1.0 |
| `_audiobook_ummv3_bigtts` | `zh_male_bv139_audiobook_ummv3_bigtts` | 1 | 1.0 |
| `_uranus_bigtts` | `zh_female_vv_uranus_bigtts` | ~28 | 2.0 |
| `saturn_*_tob` | `saturn_zh_female_keainvsheng_tob` | 8 | 2.0 |

**影响**：B5.1 的 `_is_volcengine_usable_explicit_voice()` 后缀检查需要更新——不能只认 `_moon_bigtts`，要覆盖所有 1.0 后缀。

### 3.3 代码影响：后缀兼容性检查

当前 `tts_generator.py` 的 `_VOLCENGINE_RESOURCE_SUFFIX` 只映射了两个后缀：

```python
_VOLCENGINE_RESOURCE_SUFFIX = {
    "seed-tts-1.0": "_moon_bigtts",
    "seed-tts-2.0": "_uranus_bigtts",
}
```

需要改为基于 catalog 的动态查询，而非硬编码后缀。

## 4. 范围

### 4.1 纳入

| 分类 | 来源 | Resource | 预估数量 |
|------|------|----------|---------|
| **2.0 uranus** | 官方 2.0 列表（含英语 3 个） | 2.0 | 28 |
| **2.0 Saturn** | 官方 2.0 列表（角色扮演 + 客服） | 2.0 | 8 |
| **1.0 通用场景** | 官方 1.0 列表 | 1.0 | ~45 |
| **1.0 角色扮演** | 官方 1.0 列表 ICL_*_tob | 1.0 | ~55 |
| **1.0 视频配音** | 官方 1.0 列表 | 1.0 | ~20 |
| **1.0 有声阅读** | 官方 1.0 列表 | 1.0 | ~8 |
| **1.0 客服** | 官方 1.0 列表 ICL_*_cs_tob | 1.0 | 24 |
| **1.0 多情感** | 官方 1.0 列表 _emo_v2_mars | 1.0 | 22 |
| **1.0 英语** | 官方 1.0 列表（美式 + 英式 + 澳式） | 1.0 | ~20 |
| **合计** | | | **~230** |

### 4.2 排除

| 分类 | 原因 |
|------|------|
| IP 仿音 | 鲁班七号、唐僧等版权敏感 |
| 方言/口音 | 粤语、四川、台湾、广西等（非标准中英） |
| 日语/西语/多语种 | 当前产品只做中英翻译 |

### 4.3 2.0 Saturn 客服音色说明

Saturn 客服音色（`saturn_zh_female_*_cs_tob`）的后缀是 `_cs_tob`，和角色扮演 Saturn（`saturn_*_tob`）不同。需要在 catalog 中统一标注 `resource_id = "seed-tts-2.0"`，并在兼容性检查中支持这两种 Saturn 格式。

## 5. 分阶段实施

### Phase 1：Catalog 全量录入 + gender 标注

**目标**：把 ~230 个音色全部录入 `volcengine_voice_catalog.py`。

**标注策略**：

1. `voice_id` / `display_name` / `resource_id` / `language` — 直接从官方列表复制
2. `gender` — 从 voice_id 前缀自动推断：
   - `zh_female_*` / `en_female_*` / `multi_female_*` / `ICL_zh_female_*` / `saturn_zh_female_*` → `"female"`
   - `zh_male_*` / `en_male_*` / `multi_male_*` / `ICL_zh_male_*` / `saturn_zh_male_*` → `"male"`
   - 特殊情况手动标注（如佩奇猪 → child）
3. `scene` — 从官方列表的"场景"列复制：通用场景 / 角色扮演 / 视频配音 / 有声阅读 / 客服场景 / 教育场景 / 多情感
4. `age_group` / `persona_style` / `energy_level` — **Phase 1 先留空或用最粗粒度推断**，Phase 2 由 Gemini 标注
5. `matchable` — 默认 `True`（纳入范围内的都可匹配）

**文件改动**：
- `src/services/tts/volcengine_voice_catalog.py` — 扩充
- `src/services/tts/volcengine_voice_selector.py` — 适配新 catalog 结构
- `src/services/tts/tts_generator.py` — 更新 `_VOLCENGINE_RESOURCE_SUFFIX` → 改为 catalog 查询
- `tests/test_volcengine_voice_selector.py` — 扩展跨 resource 安全测试

### Phase 2：Gemini 批量标注 age_group / persona_style / energy_level

**目标**：用 Gemini 3.1 Pro 为 ~230 个音色批量标注结构化人口统计学标签。

**方法**：

调用 Gemini API，每个音色输入：
- `display_name`（中文名）
- `scene`（场景分类）
- `voice_id`（技术标识，可辅助推断）

要求 Gemini 输出：
- `age_group`：young / middle / elderly / child
- `persona_style`：professional / warm / serious / energetic / cute / neutral
- `energy_level`：low / medium / high

**约束**：
- 不需要合成音频，纯文本推断
- 批量处理，一次 20 个音色一组
- 输出 JSON 格式，人工抽检后写入 catalog
- 成本极低（纯文本 input/output，~230 条 ≈ $0.01）

**输出**：
- 更新 `volcengine_voice_catalog.py` 中每个 entry 的 `age_group` / `persona_style` / `energy_level`

### Phase 3：B2 Voice Profiling（合成音频 + Gemini 多模态分析）

**目标**：对照 CosyVoice B2 标准，为每个音色生成结构化声音 profile。

**方法**（沿用 CosyVoice B2 的 uniform calibration 方案）：

1. **定义标准校准文本**：
   - 1 段中性校准文本（50-80 字，情感中性，音韵多样）
   - 可选 1 段轻对话文本（用于区分能力较弱的音色）
   - 英语音色额外 1 段英语校准文本

2. **批量合成**：
   - 对每个音色调用 `synthesize()` 生成校准音频
   - 1.0 音色用 `resource_id="seed-tts-1.0"`
   - 2.0 音色用 `resource_id="seed-tts-2.0"`
   - Saturn 音色也用 `seed-tts-2.0`
   - 输出保存为 `calibration_samples/{voice_id}.wav`

3. **Gemini 多模态分析**：
   - 将校准音频 + voice_id 发给 Gemini 3.1 Pro
   - 要求输出 CosyVoice B2 同规格的结构化 profile

4. **Profile 结构**（与 CosyVoice `VoiceProfile` 对齐）：

   **Primary rerank labels**：
   - `pitch_level`：low / mid / high
   - `warmth`：low / medium / high（0-10 可选）
   - `authority`：low / medium / high
   - `intimacy`：low / medium / high

   **Secondary consistency labels**：
   - `energy_level`：low / medium / high
   - `brightness`：low / medium / high
   - `maturity`：child / young / adult / elder
   - `delivery_style`：narration / assistant / customer_service / companion / explainer / storyteller
   - `texture_tags`：soft / crisp / magnetic / husky / airy / steady（多选）
   - `childlike`：true / false

5. **人工抽检**：
   - Gemini 输出的 profile 需要人工抽检后才能写入正式 catalog
   - 抽检比例：至少 20%（~50 个）

**成本估算**：
- 230 个音色 × ~5 秒校准音频 × 32 tok/s = ~36,800 audio tokens + text prompt
- Gemini 3.1 Pro: ~$0.10 总 input + ~$0.05 output ≈ **¥1**
- 合成成本：230 × 5s × ¥5/万字 ≈ ¥0.5（1.0）+ 230 × 5s × ¥3/万字 ≈ ¥0.3（2.0）
- **总成本 < ¥5**

**文件改动**：
- 新建 `src/services/tts/volcengine_voice_profile_catalog.py` — 存储 profile 数据
- 新建 `scripts/volcengine_calibration_sample_builder.py` — 批量合成校准音频
- 新建 `scripts/volcengine_voice_profiler.py` — 批量 Gemini profiling

### Phase 4：Rerank

**目标**：在 `volcengine_voice_selector.py` 中加入 B2 profile-based rerank。

**方法**（沿用 CosyVoice `_rerank_with_profiles()` 的 4 维度评分）：

评分维度及权重：
- maturity 匹配 (0.3)：source speaker age ↔ voice maturity
- childlike 匹配 (0.2)：child speaker 需要 childlike voice
- pitch_level 匹配 (0.3)：male → low/mid, female → mid/high, child → high
- texture_tags 匹配 (0.2)：persona → texture 对齐

**触发条件**：
- 仅在 B1 baseline 匹配置信度为 low 或 medium 时触发
- high confidence 不触发 rerank（避免过度干预精确匹配）

**文件改动**：
- `src/services/tts/volcengine_voice_selector.py` — 加入 `_rerank_with_profiles()`
- `tests/test_volcengine_voice_selector.py` — 加入 rerank 测试

### Phase 5：兼容性修复 + 前端更新

**目标**：修复后缀检查、更新前端 2.0 下拉列表。

**改动**：

1. `tts_generator.py`：`_is_volcengine_usable_explicit_voice()` 改为 catalog 查询
   - 不再硬编码后缀
   - 用 `volcengine_voice_catalog.is_voice_in_resource(voice_id, resource_id)` 判断

2. `frontend-next`：VoiceReviewPanel 的 2.0 下拉列表自动跟随 catalog 扩充
   - 后端 snapshot 已经从 `VOICES_2_0` 动态生成
   - 只需 catalog 扩充，前端无需额外改动

3. `voice_library.py`：`get_volcengine_2_0_allowed_voice_ids()` 自动跟随 catalog
   - 已经从 `VOICES_2_0` 派生
   - Saturn 音色加入后自动包含

## 6. 执行顺序

| 阶段 | 内容 | 依赖 | 预估工作量 |
|------|------|------|-----------|
| **Phase 1** | Catalog 全量录入 + gender 标注 | 无 | 大（数据录入） |
| **Phase 2** | Gemini 批量标注 age/persona/energy | Phase 1 | 小（脚本 + 抽检） |
| **Phase 3** | B2 Voice Profiling | Phase 2 | 中（合成 + profiling） |
| **Phase 4** | Rerank | Phase 3 | 小（复用 CosyVoice 模式） |
| **Phase 5** | 兼容性修复 + 前端 | Phase 1 | 小 |

Phase 1 和 Phase 5 可以并行。Phase 2 可以在 Phase 1 完成后立即开始。

## 7. 验证标准

### Phase 1 Done

- catalog 包含 ~230 个音色
- 所有音色有 `voice_id` / `display_name` / `resource_id` / `gender`
- 跨 resource 安全测试覆盖所有后缀格式
- 1.0 后缀兼容性检查覆盖 `_moon_bigtts` / `_mars_bigtts` / `_emo_v2_mars_bigtts` / `ICL_*_tob` / `_conversation_wvae_bigtts` / `_audiobook_ummv3_bigtts`
- 2.0 后缀兼容性检查覆盖 `_uranus_bigtts` / `saturn_*_tob`

### Phase 2 Done

- 所有音色有 `age_group` / `persona_style` / `energy_level` 标注
- 人工抽检通过率 > 90%

### Phase 3 Done

- 校准音频生成完成（至少 200 个成功）
- Profile JSON 包含 primary + secondary 全部维度
- 人工抽检通过率 > 85%
- Profile 数据存储在独立文件，不和 catalog 混在一起

### Phase 4 Done

- Rerank 在 low/medium confidence 场景下提升匹配质量
- high confidence 匹配不被 rerank 覆盖
- 所有现有测试无回归

## 8. 风险

### 8.1 高风险：数据录入量大

~230 条音色录入 + 标注是体力活，且容易出错。

**缓解**：
- gender 从 voice_id 前缀自动推断
- age/persona/energy 用 Gemini 批量标注
- 录入后用脚本校验 voice_id 唯一性和 resource_id 一致性

### 8.2 中风险：Gemini 标注噪声

Gemini 仅从 display_name 推断标签，可能不准确。

**缓解**：
- Phase 2 是文本推断，Phase 3 是音频分析，两次标注可交叉验证
- 人工抽检
- 不准确的标签在 rerank 中权重有限，不会覆盖 B1 primary match

### 8.3 中风险：1.0 ICL 音色可能不稳定

`ICL_*_tob` 格式的音色可能是 ICL（In-Context Learning）克隆音色而非固定公版音色，合成质量可能不如 `_moon_bigtts` / `_mars_bigtts` 系列。

**缓解**：
- Phase 3 合成校准音频时自然暴露质量问题
- 质量不达标的音色设为 `matchable=False`

### 8.4 低风险：Saturn 客服音色的 resource_id

Saturn 客服音色（`saturn_*_cs_tob`）未实测验证。基于角色扮演 Saturn 的实测结果推断应兼容 `seed-tts-2.0`，但需要在 Phase 1 早期验证。

**缓解**：Phase 1 第一步就验证剩余 Saturn 音色

## 9. 明确不做

- 不做在线拉取官方音色列表
- 不做 1.0 ↔ 2.0 自动映射表
- 不做方言/口音音色
- 不做日语/西语等非中英音色
- 不做 IP 仿音
- 不改现有 CosyVoice matcher
- 不做 LLM-in-the-loop 实时匹配
