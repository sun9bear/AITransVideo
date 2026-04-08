# Session Handoff: CosyVoice 统一音色匹配模块

> 写给下一个 Claude Code 会话的交接文档
> 日期：2026-04-08

---

## 1. 项目现状

多用户视频翻译/配音 SaaS。React (Next.js) 前端 + Python 后端 + FastAPI Gateway。

### 本轮已完成的主要改动

**1) Studio 模式音色选择独立阶段 (`voice_selection_review`)**
- Pipeline S5 新阶段，翻译审核后、TTS 前
- 用户为每个说话人选择/克隆音色，N 说话人支持
- VolcEngine 音色自动匹配（基于 gender/age/persona/energy 评分）
- MiniMax 克隆音色支持（含 shadow credits、need_noise_reduction）

**2) 个人音色库 (`user_voices` PostgreSQL 表)**
- per-user 存储 MiniMax 克隆音色
- 克隆成功自动写入，试听失效自动标记过期
- Gateway CRUD API: `GET/POST/DELETE /gateway/user-voices`

**3) 音色匹配 reranker 改进**
- 原来只用 4 个 profile 维度（maturity/pitch/childlike/texture），49 个音色并列满分
- 新增目录标签（age_group/persona_style）和 profile 标签（delivery_style/energy）参与评分
- Gateway API 修复：demographic 字段级 fallback（final → text 标签）
- 中文音色优先过滤（`ICL_zh_*` 优先）
- Studio 用 seed-tts-2.0，Express 用 seed-tts-1.0

**4) 其他修复**
- Gemini Vertex AI 音频上传（`Part.from_bytes` fallback）
- S2 speaker 校验支持 N 说话人（`speaker_a` ~ `speaker_z`）
- ASR `speaker_labels` 支持 3+ 说话人
- S2 审校 prompt 去掉二人偏见，说话人姓名识别强化
- VolcEngine 试听 + MiniMax 试听
- 翻译审核页面删除说话人配置区块和试听配音按钮

---

## 2. 本次任务：CosyVoice 接入统一音色匹配模块

### 目标

CosyVoice 目前用独立的硬编码映射表匹配音色（`_BASE_MAP` + `_STYLE_OVERRIDES`，12 条规则）。改为复用 VolcEngine 的评分逻辑，用同一套 combined_rerank 评分。

### 用户确认的前提
- CosyVoice 的 text 标签已从管理后台补齐（age_group, persona_style, energy_level）
- Gateway DB 有 59 个 CosyVoice Final 标签（maturity/pitch/delivery_style/texture/energy）
- 后期还需接入 MiniMax 音色库（当前 MiniMax 只有克隆音色，没有官方音色目录）

---

## 3. 必读文件

### 核心 — 音色匹配
| 文件 | 读什么 |
|------|--------|
| `src/services/tts/volcengine_voice_selector.py` | **重点** — `select_volcengine_voice()` 的 combined_rerank 评分逻辑，`_PERSONA_TEXTURE`/`_PERSONA_DELIVERY`/`_PERSONA_ADJACENT` 映射表。这是要复用的评分模块 |
| `src/services/tts/cosyvoice_voice_selector.py` | **重点** — 当前 CosyVoice 匹配逻辑：`select_voice_match()` 用 `_BASE_MAP` + `_STYLE_OVERRIDES` 硬编码，`_try_rerank_with_profiles()` 用离线 JSON profile |
| `src/services/tts/voice_match_resolver.py` | 统一入口 `resolve_voice_match()`，当前只 dispatch VolcEngine，CosyVoice 没接入 |
| `src/services/tts/voice_match_types.py` | `VoiceMatchRequest` / `VoiceMatchResult` 数据类型（两个 provider 已统一） |
| `src/services/tts/cosyvoice_voice_catalog.py` | CosyVoice 103 个音色的静态目录，字段：voice_id/name/category/traits/age/language/gender/matchable |
| `src/services/tts/cosyvoice_instruction_enhancer.py` | 包装层，`enhance_voice_selection()` 是 TTSGenerator 的直接调用入口 |

### TTS Generator — 消费侧
| 文件 | 读什么 |
|------|--------|
| `src/services/tts/tts_generator.py` 400-470 行 | `_generate_one_cosyvoice()` — 当前 CosyVoice 音色解析链路：explicit_voice → speaker_cache → enhance_voice_selection() |
| `src/services/tts/tts_generator.py` 540-610 行 | `_generate_one_volcengine()` — VolcEngine 音色解析链路（已用 `resolve_voice_match`） |

### Gateway API — profile 数据源
| 文件 | 读什么 |
|------|--------|
| `gateway/voice_catalog_api.py` 102-187 行 | `internal_voice_catalog` 端点 — 返回音色列表 + profile 标签（demographic fallback final→text 已修复） |
| `gateway/voice_catalog_models.py` | VoiceCatalog + VoiceLabel 表结构 |

### Pipeline — 音色选择阶段
| 文件 | 读什么 |
|------|--------|
| `src/pipeline/process.py` 1700-1780 行 | `_build_voice_selection_review_payload()` — 构建音色选择 payload，当前只对 volcengine 填充 available_voices 和 auto_match |

### 前端
| 文件 | 读什么 |
|------|--------|
| `frontend-next/src/components/workspace/VoiceSelectionPanel.tsx` | 音色选择 UI，按 ttsProvider 切换 dropdown（minimax=个人音色库，volcengine=按性别分组） |

---

## 4. 关键数据对比

| | CosyVoice | VolcEngine |
|---|---|---|
| 音色数 | ~103（matchable ~60） | 1.0: ~300, 2.0: ~30 |
| 匹配方式 | 硬编码映射表 12 条 | combined_rerank 评分 |
| 目录标签 | `traits`(中文描述), `age`("20~30"), `category` | `age_group`, `persona_style`, `energy_level` |
| DB Final 标签 | 59 个（maturity/pitch/delivery/texture/energy） | 338 个 |
| DB text 标签 | 23 个（用户刚补齐 age_group/persona_style） | 338 个 |
| Profile 加载 | 离线 JSON 文件 | Gateway API + 60s cache |
| `VoiceMatchResult` | 相同结构 | 相同结构 |

---

## 5. 建议实施方案

### Phase 1: 抽取通用评分模块
- 从 `volcengine_voice_selector.py` 抽取 `combined_rerank` 评分函数为独立模块（如 `voice_reranker.py`）
- 输入：candidates 列表 + speaker profile（gender/age/persona/energy）+ profiles dict
- 输出：排序后的 `(voice_id, score)` 列表
- 映射表 `_PERSONA_TEXTURE`/`_PERSONA_DELIVERY`/`_PERSONA_ADJACENT`/`_MATURITY_MAP`/`_GENDER_PITCH` 移到通用模块

### Phase 2: CosyVoice 接入
- `voice_match_resolver.py` 添加 `provider == "cosyvoice"` dispatch
- 新增 `select_cosyvoice_voice()` 函数，调用通用 reranker
- CosyVoice 音色池从 Gateway API 动态加载（已有 `list_matchable_cosyvoice_voices()`）
- Profile 从 Gateway API 加载（与 VolcEngine 相同路径）

### Phase 3: 更新消费侧
- `tts_generator._generate_one_cosyvoice()` 改用 `resolve_voice_match()` 替代直接调用 `enhance_voice_selection()`
- `_build_voice_selection_review_payload()` 添加 `tts_provider == "cosyvoice"` 分支

### Phase 4: 清理
- `cosyvoice_voice_selector.py` 的 `_BASE_MAP` / `_STYLE_OVERRIDES` 可保留为 fallback
- `cosyvoice_instruction_enhancer.py` 可简化为直接调 resolver
- 离线 JSON profile 文件可废弃（改用 Gateway API）

### 注意事项
- CosyVoice 目录标签字段名不同：`age` 是 "20~30" 格式不是 "young/middle/elderly"，`traits` 是中文描述不是 persona_style。需要做映射或依赖 DB text 标签
- Gateway `internal_voice_catalog` 端点已支持 `provider=cosyvoice` 查询
- CosyVoice 有 endpoint_modes（international/mainland），音色可用性取决于端点模式
- CLAUDE.md 约束：付费 API 不能自动调用（试听是用户触发的，OK）

---

## 6. 不需要读的文件
- V3 credits 全套（credits_service / credits_read / credits_observability）
- 前端 billing / marketing 页面
- docs/plans/AI-workgroup/ 里的协议文件
- gateway/alembic/ 迁移文件（已执行完毕）
- 声音克隆相关（voice_clone.py, voice_selection_api.py）— 不影响此次改动
