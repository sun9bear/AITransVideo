# VolcEngine Rerank Readiness Review — 2026-04-03

> 本文档评估 VolcEngine rerank 是否适合直接进入下一轮实现。
> 所有结论都区分"仓库已提交事实"和"工作区/部署态事实"。

## 1. 审计边界

- 本轮只做 readiness review，不实现 rerank
- 不修改任何代码或测试
- 审计对象：已提交代码（HEAD）+ 部署态数据（handover 文档记录）

## 2. 已提交事实 vs 工作区本地事实

| 文件 | git status | 说明 |
|------|-----------|------|
| `src/services/tts/volcengine_voice_selector.py` | 已提交，无未提交修改 | HEAD 版本即为当前真相 |
| `src/services/tts/volcengine_voice_profile_data.json` | 已提交，无未提交修改 | 21 条 audio_round1 profile |
| `gateway/voice_catalog_models.py` | 已提交，无未提交修改 | VoiceCatalog + VoiceLabel ORM 模型 |
| `gateway/voice_catalog_api.py` | 已提交，无未提交修改 | 含 `/api/internal/voice-catalog` 端点 |

**结论：** 所有 rerank 相关文件均已提交，以下分析基于仓库 HEAD 事实。

## 3. 代码 Readiness

### 3.1 `_try_rerank_with_profiles()` 不是 no-op

**与 handover 文档的偏差：** `docs/handover/2026-04-02-session-handover.md` §5.1 写道"_try_rerank_with_profiles() 仍是 no-op"。这已过时。

**已提交代码事实（`volcengine_voice_selector.py` L253-326）：** `_try_rerank_with_profiles()` 是一个完整实现的 4 维度评分函数：
- Maturity match: 0.30 权重
- Childlike match: 0.20 权重
- Pitch preference: 0.30 权重
- Texture match: 0.20 权重

评分逻辑完整，包含排序和 rerank 日志输出。**代码层面 rerank 已经实现。**

### 3.2 数据加载路径已实现

`_load_profiles()` (L224-250) 从 Gateway 内部 API `http://127.0.0.1:8880/api/internal/voice-catalog` 读取 voice profiles，带 60 秒缓存和 fallback。

### 3.3 Gateway 内部 API 已实现

`gateway/voice_catalog_api.py` L101-187 实现了 `/api/internal/voice-catalog` 端点，返回 matchable + verified 音色及其标签数据。响应包含 rerank 所需的全部字段：`pitch_level`、`warmth`、`maturity`、`childlike`、`texture_tags`。

### 3.4 rerank 已经在运行时被调用

`volcengine_voice_selector.py` L109、L134、L154 — `_try_rerank_with_profiles()` 在 matcher 的三个匹配路径中都已被调用。这意味着 rerank **已经处于运行时激活状态**，不是需要"激活"的开关。

**当前行为：** 如果 `_load_profiles()` 返回空 dict（Gateway API 不可达或 DB 无 profile 数据），rerank 退化为 identity（返回原始顺序）。如果有 profile 数据，rerank 已经生效。

## 4. 数据 Readiness

### 4.1 DB 中的标签数据（部署态事实，来自 handover 文档）

根据 `docs/handover/2026-04-02-session-handover.md` §3.2：

| 表 | 数据量 | 说明 |
|---|--------|------|
| `voice_catalog` | 406 行 | 338 VolcEngine + 68 CosyVoice |
| `voice_labels` | 359 行 | 338 text (demographic) + 21 audio_round1 (profile) |

### 4.2 rerank 可用的 profile 数据

rerank 评分依赖 4 个字段：`maturity`、`pitch_level`、`childlike`、`texture_tags`。

- **有 audio profile 的音色：** 21 个（`audio_round1` 标签），全部包含 4 个评分维度
- **总 VolcEngine matchable 音色：** 338 个
- **覆盖率：** 21/338 = **6.2%**

### 4.3 静态 profile 文件

`volcengine_voice_profile_data.json` 包含与 DB 中 21 条 `audio_round1` 相同的数据（dict 格式，key 为 voice_id）。但 `_load_profiles()` 不读此文件 — 它只从 Gateway API 读取。此文件是 seed 数据来源，不是运行时数据来源。

### 4.4 CosyVoice B2 profiles

handover 文档记录 59 条 CosyVoice B2 profiles 存在于生产环境 `/opt/aivideotrans/data/b2_voice_profiles_final.json`，但**未写入 DB**（seed SQL 未包含）。这些数据与 VolcEngine rerank 无关（不同 provider）。

## 5. 运行时风险

### 5.1 6.2% 覆盖率下的 rerank 行为

当前只有 21/338 个 VolcEngine 音色有 profile。rerank 对没有 profile 的音色给 0 分（L286: `scored.append((vid, 0.0))`）。这意味着：

- 如果 primary + backup 中有 profiled 音色，它会被评分并可能被提升
- 如果 primary + backup 全部没有 profile，所有评分为 0，排序不变（退化为 identity）
- 如果只有部分 backup 有 profile，profiled 的 backup 会被提升到 primary 之前

**风险判断：** 在 6.2% 覆盖率下，大多数匹配场景 rerank 会退化为 identity（无 profile → 无效果）。少数场景 rerank 会把一个有 profile 的 backup 提升为 primary，但这个 backup 之所以不是 primary，是因为 gender/scene 匹配分没有赢过 primary。**在低覆盖率下，rerank 的偶发提升可能降低匹配质量而非提升。**

### 5.2 Gateway API 可用性

rerank 依赖 Gateway 内部 API。如果 Gateway 不可达（容器重启间隙），`_load_profiles()` fallback 到缓存或空 dict，rerank 退化为 identity。这是安全的退化路径。

## 6. 结论：GO — 但前提条件需确认

### 为什么是 GO 而非 NO-GO

**代码已就绪。** `_try_rerank_with_profiles()` 已完整实现，已在运行时被调用，Gateway 内部 API 已实现，数据加载路径已打通。没有需要"激活"的开关 — rerank 已经在运行。

**风险是可控的。** 低覆盖率下 rerank 退化为 identity，不会导致崩溃或错误。

### 前提条件

下一轮实现 Sprint 不应以"激活 rerank"为目标（它已经激活），而应以**提升 profile 覆盖率**为目标：

1. **最小阻塞项：** 338 个 VolcEngine 音色中只有 21 个有 audio profile。在覆盖率达到足够水平（建议 ≥30%，即 ≥100 个音色有 profile）之前，rerank 的效果不可观测和验证。
2. **CosyVoice B2 profiles 未入库：** 59 条 profiles 存在于生产环境但未写入 DB。应作为下一步数据清洗的一部分。

### 最小实现切片（下一轮）

**不是"激活 rerank"，而是"扩充 profile 数据 + 验证 rerank 效果"：**

1. 将现有 21 条 audio_round1 确认为初始 baseline
2. 利用已有的标注工具链（`volcengine_voice_profiler.py` + 190 条已缓存 WAV）扩充 audio profile 覆盖
3. 在扩充到 ≥100 条后，对比 rerank 前后的匹配结果差异
4. 如果差异正向，保持 rerank 激活；如果差异负向，调整权重或禁用
