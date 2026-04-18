# 多目标语言（target_language）架构方向

> **Status:** direction (parked until trigger)  
> **Last updated:** 2026-04-15  
> **Trigger:** 第一个真实的"非英→中"产品需求  
> **前置止血：** 已执行 `UPDATE voice_catalog SET matchable=false WHERE provider='volcengine' AND language='en'`（禁用 36 个 VolcEngine 英文音色，详见下文 §3）

---

## 1. Context — 当前产品现状与未来方向

当前产品只支持**一个翻译方向**：

```
英文视频 → 中文配音
```

整个 pipeline 默认 target_language = `zh-CN`：
- Translator prompt 硬编码要求输出中文
- Rewriter prompt 硬编码要求输出中文
- TTSDurationEstimator 按中文 chars/sec 算
- Voice matcher 候选池里混杂多语言音色，靠上游保证只选中文音色

**未来可能的扩展方向**（按可能性排序）：

| 方向 | 可能性 | 备注 |
|---|---|---|
| **中→英** | 高 | 流程对称，VolcEngine 中文音色能合成英文（实证） |
| **日→中 / 中→日** | 中 | ASR（AssemblyAI 支持日语）+ MiniMax 日语音色已就绪 |
| **英↔西/法/德** | 中 | 三引擎都支持多语言，主要是 prompt 模板 |
| **多段多方向**（中英夹杂的视频混合配音） | 低 | 架构复杂度高一个数量级 |

---

## 2. 2026-04-15 触发事件

[Job 5dec0e0bc43c4f5fa9787f20f888f071](../handover/2026-04-15-session-handover.md) 失败。用户在中文配音任务里选了 `en_male_tim_uranus_bigtts` 作 speaker_b 音色，pipeline 段 2 TTS 合成失败 5 + 1 次，job 彻底 fail。

### 根因实测（scripts/test_volcengine_explicit_language.py, 2026-04-15）

5 个测试组合 × VolcEngine seed-tts-2.0：

| voice | text | explicit_language | 结果 |
|---|---|---|---|
| `en_male_tim_uranus_bigtts` | 中文 | *(omitted)* | ❌ no audio data |
| `en_male_tim_uranus_bigtts` | 中文 | `"zh-cn"` | ❌ no audio data |
| `en_male_tim_uranus_bigtts` | 中文 | `"crosslingual"` | ❌ no audio data |
| `zh_female_yingyujiaoxue_uranus_bigtts` | 英文 | *(omitted)* | ✅ 12598ms |
| `zh_female_yingyujiaoxue_uranus_bigtts` | 英文 | `"en-us"` | ✅ 11827ms |

**两个铁证**：
1. **VolcEngine 英文音色硬绑定英语** — 任何 `explicit_language` 取值都无法让英文音色合成中文
2. **VolcEngine 中文音色天然支持多语言** — 可合成英文，无需任何参数

这个非对称关系是后续架构设计的关键依据。

---

## 3. 两种修复方案对比

### 方案 A：DB `matchable=false` 一刀切（已执行）

```sql
UPDATE voice_catalog SET matchable = false
WHERE provider = 'volcengine' AND language = 'en';
```

**优点**：
- 立即止血（5 秒）
- 零代码改动
- 零架构风险

**缺点**：
- 语义不准（音色**能用**，只是**不适合当前 target**）
- 未来做"中→英"时需要撤回（再 36 行 UPDATE）
- 如果未来有"保留英文片段"需求（中文视频里英文段用英文音色），`matchable=false` 会妨碍

### 方案 B：target_language 全架构（未来做）

每个音色标注"兼容的 target_language 列表"，Voice Match / Translator / Rewriter 按当前 job 的 target_language 筛选 / 生成内容。

**优点**：
- 语义准确
- 未来多方向扩展零改动
- 利用 VolcEngine 中文音色"能读英文"的副产物

**缺点**：
- 工作量大（见下 §4）
- 在当前只有一个方向的现实里，**过度抽象**风险高

### 决策

**今天做方案 A**（立即止血），**方案 B 等触发条件**（下 §5）。

---

## 4. 方案 B 完整架构清单（供未来参照）

### 4.1 DB schema

```sql
-- voice_catalog 加字段
ALTER TABLE voice_catalog
  ADD COLUMN compatible_target_languages TEXT[] NOT NULL DEFAULT ARRAY[language];

-- 示例数据（基于实测 + 官方文档）：
--   VolcEngine zh_*:  ['zh', 'en']   (中文音色能读英文)
--   VolcEngine en_*:  ['en']          (英文音色硬绑定)
--   MiniMax zh/en/..: ['zh', 'en', 'ja', 'ko', ...]  (多语言模型)
--   CosyVoice v3 zh_*: ['zh', 'en', 'ja', 'ko', 'de', 'es', 'fr', 'it', 'ru']
```

回填数据策略：
- 对每个 provider 按实测矩阵初始化（VolcEngine 实测已完成）
- MiniMax / CosyVoice 需要类似的小规模实测（每 provider 5-10 个组合）

### 4.2 Job schema

```sql
ALTER TABLE jobs
  ADD COLUMN target_language TEXT NOT NULL DEFAULT 'zh-CN';
-- 已有 jobs 回填 'zh-CN'
```

或者用现有 service_config JSONB 存 target_language，无 migration 成本但查询稍弱。

### 4.3 Translator prompt 模板化

`src/services/gemini/translator.py` 的 `DEFAULT_TRANSLATION_PROMPT_TEMPLATE` 当前硬编码"把英文翻译成中文"。需要：

```python
PROMPT_TEMPLATES: dict[tuple[str, str], str] = {
    ("en", "zh"): DEFAULT_EN_TO_ZH_PROMPT,
    ("zh", "en"): DEFAULT_ZH_TO_EN_PROMPT,
    # ...
}
```

每个模板要独立调节（中→英 target_chars 计算公式完全不同，不是 1.8x）。

### 4.4 Rewriter prompt 同上

`src/services/rewriter.py` 的 rewrite_for_duration prompt 也硬编码中文改写。target_language 化。

### 4.5 TTSDurationEstimator

当前假设 spoken chars = CJK ideographs。英文要改成 words。需要 per-language spoken-char counting 规则。

### 4.6 Probe 翻译

使用同一 translator，自动跟随 target_language。

### 4.7 Voice Match 候选池过滤

`src/services/tts/voice_match_resolver.py` 及下属 selectors，接受 `target_language` 参数：

```python
@dataclass(frozen=True, slots=True)
class VoiceMatchRequest:
    # ... existing fields ...
    target_language: str | None = None  # "zh" / "en" / ... 已经有了这个字段！但实际没用于筛选
```

**注意**：`VoiceMatchRequest.target_language` [src/services/tts/voice_match_types.py:60](../../src/services/tts/voice_match_types.py:60) 已经存在，但只传递给 MiniMax selector 做**语言预过滤**（[minimax_voice_selector.py](../../src/services/tts/minimax_voice_selector.py)）。扩展时：
- VolcEngine / CosyVoice selector 也要消费这个字段
- 过滤逻辑改成按 `compatible_target_languages ∋ target_language`

### 4.8 Catalog API

`gateway/voice_catalog_api.py` `/api/internal/voice-catalog` 加 `target_language` query：

```python
GET /api/internal/voice-catalog?provider=volcengine&target_language=zh
# 返回 compatible_target_languages 包含 'zh' 且 matchable=True 的音色
```

### 4.9 前端

- `VoiceSelectionPanel` 按 job.target_language 过滤 dropdown
- Job 创建页加 target_language 选择器（如果需要用户显式选）

### 4.10 字数预估（Phase 1/2 链路）

- `_estimate_dynamic_target_chars` 的 `_ENGLISH_TO_CHINESE_CHAR_RATIO = 1.8` 要参数化
- 每个 source×target pair 有自己的 ratio（中→英约 0.55，英→日约 1.2 等）

---

## 5. 什么时候启动方案 B

### 启动触发条件（任一满足即可）

1. **产品明确添加第二个 language pair**（例如 "中→英" 写入 roadmap）
2. **用户反馈"想保留英文原声片段"** 的需求（夹杂语言场景）
3. **未来某天** VolcEngine 的中文音色能合成英文这个能力被产品特性化（例如"双语配音"功能）

### 不应该触发的场景

- **仅为了让 36 个 VolcEngine 英文音色"能用"** — 他们当前不适合 target=zh，方案 A 已解决
- **架构洁癖** — YAGNI

---

## 6. NOT-TODO 列表（防止下一轮重新讨论时踩坑）

❌ **不要** 现在就加 `compatible_target_languages` 字段 — migration 成本 + 缺乏对比验证基准
❌ **不要** 现在就 parameter化 translator prompt — 只有一个方向，可 premature
❌ **不要** 一次重构所有模块 — 从 DB + Voice Match 两层开始就好
❌ **不要** 把 `matchable=false` 恢复成 `matchable=true` 直到方案 B 完整部署 — 会让 VolcEngine 英文音色回到候选池，中文 job 又会挂

---

## 7. 当前一些已知的"预先正确"的设计

这些将来做方案 B 时不用改，只用扩：

✅ `voice_catalog.language` 字段已存在且填值正确（36 个 VolcEngine en_ 音色 language='en'）
✅ `VoiceMatchRequest.target_language` 字段已存在（只是下游没全用）
✅ `minimax_voice_selector.py` 已经做 target_language 预过滤（可作为其他 provider 的参考实现）
✅ TTSDurationEstimator 有 `_NON_SPOKEN_CHAR_PATTERN` 机制（用于中文 CJK，扩展到其他语言是加不同 pattern）

---

## 8. 实施预估（给未来的参考）

按 §4 清单做完完整方案 B：

| 阶段 | 工作量 | 备注 |
|---|---|---|
| DB migration + compatible_target_languages 字段 + 回填 | 1 小时 | 纯 additive |
| Voice Match 三 selector 加 target_language 过滤 | 2-3 小时 | 参考 minimax 现有实现 |
| Catalog API + 前端 dropdown 过滤 | 2 小时 | 前后端小改动 |
| Translator + Rewriter prompt 模板化 | 4-6 小时 | 每个 language pair 单独调 |
| 第二个 language pair 的 TTSDurationEstimator / chars ratio | 2-4 小时 | 实测 + 数据调优 |
| 端到端测试（新方向 real job） | 2-3 小时 | 需付费 API 真实跑 |
| **合计** | **~2 工作日** | 不含标定和优化 |

---

## 9. 相关引用

- 触发事件 job: `job_5dec0e0bc43c4f5fa9787f20f888f071` (2026-04-15 18:40)
- 实测脚本: `scripts/test_volcengine_explicit_language.py`
- 实测 CSV: `/tmp/volcengine_explicit_language_test.csv` (us host)
- 执行的 SQL: `D:\Claude\temp\disable_volcengine_en_voices.sql`
- voice_catalog schema: `gateway/voice_catalog_models.py`
- VoiceMatchRequest: `src/services/tts/voice_match_types.py`
- MiniMax language pre-filter（参考实现）: `src/services/tts/minimax_voice_selector.py`
