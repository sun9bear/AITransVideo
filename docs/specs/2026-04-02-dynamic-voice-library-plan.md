# 动态音色库管理系统 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将音色管理从"代码硬编码静态列表"升级为"管理后台可维护的动态音色库"，通用架构兼容所有 TTS provider。按 4 期递进交付，每期独立可用。

**Status:** ✅ 全部 4 期已完成并部署（2026-04-03）。额外完成：CosyVoice 切 DB、CosyVoice verify、异步任务队列、内部端点安全加固。

**Architecture:** Gateway PostgreSQL `voice_catalog` + `voice_labels` + `label_tasks` 三张表，JSONB 字段适配不同 provider 的参数和验证维度。

**Tech Stack:** PostgreSQL 16 + SQLAlchemy (async) + FastAPI (gateway) + Next.js 16 (frontend-next)

---

## 1. 当前基线（截至 2026-04-02）

### 1.1 代码现状

| 项 | 值 | 来源 |
|---|---|---|
| VolcEngine 1.0 音色 | **302** | `volcengine_voice_catalog.py` VOICES_1_0 |
| VolcEngine 2.0 音色 | **36**（含 Saturn） | `volcengine_voice_catalog.py` VOICES_2_0 |
| CosyVoice 音色 | **68**（59 matchable + 9 方言/海外） | `cosyvoice_voice_catalog.py` |
| VolcEngine 标签覆盖 | 338/338（age_group/persona_style/energy_level） | catalog 内联 + Gemini 文本标注合并 |
| VolcEngine audio profile | **21** 条 | `volcengine_voice_profile_data.json` |
| CosyVoice B2 profile | **59** 条 | `b2_voice_profiles_final.json` |
| VolcEngine rerank | **占位 no-op**，未进入运行时 | `volcengine_voice_selector.py:_try_rerank_with_profiles()` |
| voice_review 门槛 | **已收口**（volcengine + studio only） | voice_library.py + handler.py + page.tsx |

### 1.2 关键约束

- `volcengine_voice_catalog.py` 的 338 条已经包含完整的 age_group/persona_style/energy_level（Phase 1 手动推测 + Phase 2 Gemini 精修合并），不是只有 258 条有标签
- CosyVoice 主路径稳定，本轮不迁移到 DB
- 现有离线脚本 `scripts/volcengine_batch_label.py` + `scripts/volcengine_voice_profiler.py` 已验证可用
- Gemini 3.1 Pro RPD 限额 250/天，批量操作需要控节奏

---

## 2. 通用 Provider 模型

### 2.1 JSONB 字段设计

**`provider_config`** — Provider 特有参数：

```jsonc
// volcengine
{"resource_id": "seed-tts-1.0"}
{"resource_id": "seed-tts-2.0"}

// cosyvoice
{"model": "cosyvoice-v3-flash", "endpoint_modes": ["international", "mainland"]}

// minimax
{"model": "speech-2.8-hd"}

// mimo
{}
```

**`verify_status`** — 多维度验证结果：

```jsonc
// volcengine — 单维度
{"default": {"verified": true, "at": "2026-04-02T...", "error": null}}

// cosyvoice — 双端点
{"international": {"verified": true, "at": "...", "error": null},
 "mainland": {"verified": true, "at": "...", "error": null}}

// minimax / mimo — 单维度
{"default": {"verified": true, "at": "...", "error": null}}
```

### 2.2 Matchable 判定规则

```python
def is_effectively_matchable(voice) -> bool:
    if not voice.matchable:
        return False
    if voice.archived_at is not None:
        return False
    status = voice.verify_status or {}
    if not status:
        return False
    return any(dim.get("verified") is True for dim in status.values())
```

---

## 3. DB Schema

### 3.1 voice_catalog 表

| Column | Type | Description |
|--------|------|-------------|
| id | SERIAL PK | |
| voice_id | VARCHAR(200) UNIQUE NOT NULL | 技术标识符（全局唯一） |
| provider | VARCHAR(50) NOT NULL | `volcengine` / `cosyvoice` / `minimax` / `mimo` / ... |
| provider_config | JSONB DEFAULT '{}' | Provider 特有配置 |
| display_name | VARCHAR(200) NOT NULL | 显示名称 |
| gender | VARCHAR(20) | `male` / `female` / `child` |
| language | VARCHAR(20) DEFAULT 'zh' | `zh` / `en` |
| scene | VARCHAR(50) | 场景分类 |
| matchable | BOOLEAN DEFAULT TRUE | 管理员开关 |
| verify_status | JSONB DEFAULT '{}' | 各维度验证结果 |
| verify_attempts | INTEGER DEFAULT 0 | |
| source | VARCHAR(50) DEFAULT 'manual' | `seed_migration` / `manual` / `csv_import` |
| archived_at | TIMESTAMP | 软删除时间戳（NULL = 活跃） |
| notes | TEXT | 管理员备注 |
| created_at | TIMESTAMP DEFAULT NOW() | |
| updated_at | TIMESTAMP DEFAULT NOW() | |

索引：`UNIQUE(voice_id)`、`INDEX(provider, matchable)`、`GIN(provider_config)`、`GIN(verify_status)`

### 3.2 voice_labels 表

| Column | Type | Description |
|--------|------|-------------|
| id | SERIAL PK | |
| voice_id | VARCHAR(200) FK → voice_catalog.voice_id ON DELETE CASCADE | |
| label_type | VARCHAR(30) NOT NULL | `text` / `audio_round1` / `audio_round2` / `audio_round3` / `final` |
| source_run_id | VARCHAR(100) | 本次标注的运行标识（如 `gemini-text-2026-04-02-batch3`） |
| is_current | BOOLEAN DEFAULT TRUE | 是否为当前生效标签（同 voice_id + label_type 可有多条，仅最新为 true） |
| age_group | VARCHAR(20) | |
| persona_style | VARCHAR(30) | |
| energy_level | VARCHAR(20) | |
| pitch_level | VARCHAR(10) | 音频 profiling only |
| warmth | VARCHAR(10) | |
| authority | VARCHAR(10) | |
| intimacy | VARCHAR(10) | |
| brightness | VARCHAR(10) | |
| maturity | VARCHAR(20) | |
| delivery_style | VARCHAR(30) | |
| texture_tags | JSONB | `["soft","crisp"]` |
| childlike | BOOLEAN | |
| labeled_by | VARCHAR(50) | `gemini-3.1-pro` / `manual` / `seed_migration` |
| labeled_at | TIMESTAMP DEFAULT NOW() | |
| superseded_at | TIMESTAMP | 被新版标签取代的时间（NULL = 未取代） |

索引：`INDEX(voice_id, label_type, is_current)`

**覆盖关系**：新标签写入时，同 voice_id + label_type 的旧 is_current 记录 set `is_current=False, superseded_at=now()`。不删除旧记录，保留审计链。

### 3.3 Seed Data

| 来源 | 写入表 | 数量 | 说明 |
|------|-------|------|------|
| `volcengine_voice_catalog.py` VOICES_1_0 | voice_catalog | 302 | `provider_config.resource_id = "seed-tts-1.0"` |
| `volcengine_voice_catalog.py` VOICES_2_0 | voice_catalog | 36 | `provider_config.resource_id = "seed-tts-2.0"` |
| `cosyvoice_voice_catalog.py` | voice_catalog | 68 | `provider_config.model = "cosyvoice-v3-flash"`（含 9 非 matchable） |
| catalog 内联标签（338 VolcEngine 全覆盖） | voice_labels | 338 | label_type=`text`, labeled_by=`seed_migration` |
| `volcengine_voice_profile_data.json` | voice_labels | 21 | label_type=`audio_round1` |
| `b2_voice_profiles_final.json` | voice_labels | 59 | label_type=`audio_round1`（生产环境可用） |
| **合计** | | **406 音色 + 418 标签** | |

seed data 音色默认 `verify_status = {"default": {"verified": true, ...}}`。

---

## 4. 分期计划

### 第 1 期：Schema + Seed + 只读列表

**目标**：建表、导入现有数据、管理后台可查看。

**文件**：
- 新建：`gateway/voice_catalog_models.py`
- 新建：`gateway/alembic/versions/005_add_voice_catalog.py`
- 新建：`gateway/voice_catalog_api.py`（只读端点）
- 新建：`frontend-next/src/app/admin/voices/page.tsx`（只读列表）
- 新建：`frontend-next/src/lib/api/voiceCatalog.ts`
- 新建：`frontend-next/src/types/voiceCatalog.ts`
- 修改：`gateway/main.py`（注册 router）

**端点**：

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/admin/voices` | 音色列表（分页、筛选 provider/gender/verified） |
| GET | `/api/admin/voices/{voice_id}` | 单个音色详情（含所有标签历史） |

**前端**：只读表格，按 provider 分 tab，显示 voice_id / display_name / gender / 标签状态 / 验证状态。

**Done 定义**：
- [ ] migration up/down 可执行
- [ ] 406 音色 + 418 标签 seed 成功
- [ ] admin 页面可查看完整列表
- [ ] DB 不可用不影响现有运行时

---

### 第 2 期：Verify + 编辑 + 导入

**目标**：管理员可验证、编辑、导入音色。

**文件**：
- 修改：`gateway/voice_catalog_api.py`（新增写端点）
- 新建：`gateway/voice_catalog_service.py`（verify + import 逻辑）
- 修改：`frontend-next/src/app/admin/voices/page.tsx`（编辑 + 导入 UI）

**新增端点**：

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/admin/voices` | 新增单个音色 |
| PATCH | `/api/admin/voices/{voice_id}` | 编辑元数据 |
| DELETE | `/api/admin/voices/{voice_id}` | 软删除（设 archived_at） |
| POST | `/api/admin/voices/import` | 批量导入（CSV/粘贴 → Gemini 整理 → diff preview → 确认） |
| POST | `/api/admin/voices/{voice_id}/verify` | 验证音色可用性 |
| POST | `/api/admin/voices/verify-batch` | 批量验证 |

**验证逻辑**（复用 Provider Adapter 概念，但第 2 期只实现 VolcEngine）：

对目标音色调 TTS API 合成测试文本：
- 成功 WAV > 1KB → `verify_status.default.verified = true`
- 错误码 55000000 → resource mismatch
- 错误码 45000000 → voice 不存在
- 超时 → 不更新，`verify_attempts += 1`

**resource_id 自动探测**：未知格式 → 先尝试 2.0 再尝试 1.0。

**matchable 生效条件**：`matchable=True` AND `archived_at IS NULL` AND `verify_status` 至少一个维度 verified。

**Done 定义**：
- [ ] 可新增/编辑/软删除音色
- [ ] 可批量导入（CSV + 粘贴文本）
- [ ] 导入后自动 verify
- [ ] 管理页面显示验证状态 + "重新验证"按钮

---

### 第 3 期：VolcEngine Matcher 切 DB

**目标**：VolcEngine matcher 从 DB 读取替代静态 list。CosyVoice 不动。

**文件**：
- 修改：`src/services/tts/volcengine_voice_catalog.py`（DB 适配层）
- 修改：`src/services/tts/volcengine_voice_selector.py`（读 final labels）
- 修改：`src/services/web_ui/voice_library.py`（allowed IDs 从 DB 读）

**适配策略**：

```python
def get_voices_for_resource(resource_id: str) -> list[VoiceEntry]:
    """DB 优先 + 静态 fallback。"""
    try:
        return _load_from_db(resource_id)
    except Exception:
        return _static_fallback(resource_id)
```

Matcher 优先级：
1. voice 有 `final` label（is_current=True）→ 用 final 的 age_group/persona_style/energy_level
2. 没有 final → 用 voice_catalog 表中的 seed 数据

**CosyVoice 保持现有静态路径不变。**

**Done 定义**：
- [ ] VolcEngine matcher 从 DB 读取音色池
- [ ] 新导入的音色 verify 通过后自动参与匹配
- [ ] DB 不可用时 fallback 到静态 list
- [ ] CosyVoice 无回归

---

### 第 4 期：标注 / Profiling 离线工具链

**目标**：将现有离线脚本接入管理后台，管理员可触发标注。

**执行模型**：继续维持"离线工具 + 显式触发"，不做在线异步任务系统。

**实现方式**：
- 管理页面增加"文本标注"和"音频 profiling"按钮
- 按钮触发后端 API → 后端调用现有脚本逻辑（同步/限量，非后台队列）
- 单次触发限量（如一次最多 10 个音色），防止 API quota 被打爆
- 结果写入 voice_labels 表（is_current=True，旧记录标 superseded）

**标注类型**：

| 类型 | 工具 | 输入 | 输出 |
|------|------|------|------|
| 文本标注 | Gemini API (text) | display_name + scene | age_group / persona_style / energy_level |
| 音频 Round 1 | TTS 合成 + Gemini 多模态 | 中性校准文本 | 10 维度 profile |
| 音频 Round 2 | TTS 合成 + Gemini 多模态 | 轻对话校准文本 | 10 维度 profile |
| 音频 Round 3 | TTS 合成 + Gemini 多模态 | 正式/专业校准文本 | 10 维度 profile |
| Final | 综合分析 | 所有已有标签 | 多数投票合并 → final label |

**Final 标签规则**：
```
有 3 轮 audio → 3 轮多数投票
有 1-2 轮 audio → 最后一轮
仅有 text → text label
都没有 → 不生成
```

**新增端点**（第 4 期）：

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/admin/voices/{voice_id}/label/text` | 触发文本标注 |
| POST | `/api/admin/voices/{voice_id}/label/audio/{round}` | 触发音频 profiling |
| POST | `/api/admin/voices/{voice_id}/label/finalize` | 生成 final 标签 |
| POST | `/api/admin/voices/label/batch-text` | 批量文本标注（限量 10） |
| POST | `/api/admin/voices/label/batch-audio` | 批量音频 profiling（限量 5） |
| GET | `/api/admin/voices/label/status` | 标注进度概览 |

**VolcEngine rerank 激活**：当 voice_labels 表中有足够的 final 标签后，`_try_rerank_with_profiles()` 从 DB 读取 profile 并执行 4 维度评分。这是第 4 期的最后一步。

**Done 定义**：
- [ ] 管理员可在页面触发文本标注和音频 profiling
- [ ] 标注结果写入 voice_labels 表（带 source_run_id + is_current 审计链）
- [ ] final 标签自动生成
- [ ] VolcEngine rerank 从 DB 读取 final profile 并真正参与运行时排序
- [ ] 不超过 Gemini RPD 限额

---

## 5. 执行顺序

| 期 | 内容 | 依赖 | 风险 |
|----|------|------|------|
| **第 1 期** | Schema + Seed + 只读列表 | 无 | 低 |
| **第 2 期** | Verify + 编辑 + 导入 | 第 1 期 | 中（API 调用） |
| **第 3 期** | VolcEngine Matcher 切 DB | 第 1 期 | 中（运行时变更） |
| **第 4 期** | 标注 / Profiling 工具链 | 第 1-3 期 | 中（Gemini 限额） |

第 2 期和第 3 期可并行。第 4 期依赖前三期全部完成。

---

## 5.1 关键架构决策

### 第 3 期：app 运行时如何读 catalog

**决策：app 通过 Gateway 内部 API 读取，不直连 PostgreSQL。**

原因：
- app 容器当前没有 PG 连接配置（docker-compose.yml 中只有 gateway 有 `AVT_DATABASE_URL`）
- Gateway 是 app 与 PG 之间的唯一桥梁（Job API 已是这个模式）
- 新增直连 PG 等于打破现有隔离边界，风险大于收益

实现方式：
- Gateway 新增内部端点：`GET /api/internal/voice-catalog?provider=volcengine&resource_id=seed-tts-1.0`
- 返回当前 matchable + verified 的音色列表（含 final labels）
- app 侧 `volcengine_voice_catalog.py` 的 `get_voices_for_resource()` 改为：
  1. 检查进程内缓存（TTL 60s）
  2. cache miss → 调 Gateway 内部 API
  3. API 失败 → fallback 到静态 Python list
- `voice_library.py` 的 `get_volcengine_2_0_allowed_voice_ids()` 同理：Gateway API 优先 + 短 TTL 缓存 + 静态 fallback
- 这保证 DB 临时不可用时不会拖慢用户侧 studio 提交流程

### 第 4 期：标注/profiling 执行位点

**决策：仍由 app 侧执行，Gateway 只做触发入口和结果存储。**

原因：
- 现有脚本（`volcengine_batch_label.py`、`volcengine_voice_profiler.py`）运行在 app 容器内
- app 容器有完整运行时依赖：Gemini API key、VolcEngine TTS 凭据、ffmpeg
- Gateway 容器（基于 uvicorn 的轻量镜像）没有这些

执行链路：
```
管理后台点击"标注"
  → Gateway API 接收请求
  → Gateway 调 app 的 Job API 内部端点：POST /api/internal/label-task
  → app 同步执行标注逻辑（限量：文本 10 个/次，音频 5 个/次）
  → app 将结果通过 Gateway API 回写 voice_labels 表
  → Gateway 返回结果给前端
```

替代方案（更简单，第 4 期 v1 推荐）：
```
管理后台点击"标注" → Gateway 写入 "待执行" 标记到 DB
管理员 SSH 进 app 容器 → 运行 CLI 工具读取待执行任务 → 执行 → 回写结果
```

第 4 期 v1 先用替代方案（CLI 工具），如果后续需要全自动再升级为完整链路。

### Seed data 的 verify_status 说明

Seed 音色默认 `verify_status = {"default": {"verified": true, ...}}` 是**迁移基线信任**——这些音色在当前静态 catalog 中已被生产环境使用，等同于已验证。

后续新导入的音色默认 `verify_status = {}`（未验证），必须通过第 2 期的 verify 流程才能参与匹配。

管理后台应区分展示：
- `verified (seed)` — 迁移继承的信任状态
- `verified` — 第 2 期真正验证通过的
- `unverified` — 待验证

---

## 5.2 前置条件与 blocked-by 说明（2026-04-03 追加）

以下前置条件在后续扩展（特别是 VolcEngine rerank 激活和 Phase 2 CRUD/import/verify）推进之前必须满足：

### blocked-by: 稳定性与收敛工作

- **VolcEngine rerank 激活** 应在 stabilization / convergence 计划完成后再推进。当前 rerank 仍是占位 no-op（`volcengine_voice_selector.py:_try_rerank_with_profiles()`），激活它需要确认 TTS 路由链路已端到端稳定。
- **runtime routing evidence 已具备**：在继续扩展 provider 管理面之前，必须先具备运行时路由证据。截至 2026-04-03，`tests/test_tts_runtime_evidence.py`（11 passed）已覆盖三层证据（provider 决策 / mocked pipeline / runner log capture）。此前置条件已满足。
- **reproducible environment / dependency manifest 已具备**：在继续扩展 provider 管理面之前，必须先具备可复现的根目录 Python 依赖管理。截至 2026-04-03，`pyproject.toml` 已落地，Dockerfile 已改为从 manifest 安装。此前置条件已满足。

### blocked-by: 本轮不实现的后续功能

- **动态音色库 Phase 2（CRUD / import / verify）**不属于当前稳定性计划的实施范围。Phase 2 的实现依赖 Phase 1 schema + seed 的生产部署验证。
- **VolcEngine rerank 正式激活**不属于当前稳定性计划的实施范围。激活需要 voice_labels 表中有足够 final 标签，以及 rerank 评分逻辑的端到端验证。

---

## 6. 原"明确不做"项目最终状态

| 原计划不做 | 最终状态 | 说明 |
|-----------|---------|------|
| 在线异步任务队列 | ✅ 已做 | DB `label_tasks` 表 + asyncio 后台协程 + 前端轮询进度 |
| CosyVoice matcher 迁移到 DB | ✅ 已做 | Gateway 内部 API + 60s 缓存 + 静态 fallback，与豆包同架构 |
| CosyVoice verify | ✅ 已做 | 通过 app 内部端点代理 DashScope TTS 合成检测 |
| 内部端点安全加固 | ✅ 已做 | `AVT_INTERNAL_API_KEY` + `X-Internal-Key` header |
| 音色试听播放器 | ❌ 未做 | |
| 声音克隆管理 | ❌ 未做 | 保持现有 voice_registry |
| 自动同步官方音色列表 | ❌ 未做 | |
| Minimax 音色导入 | ❌ 未做 | 架构预留 |

---

## 7. 未来扩展

本架构支持以下扩展，无需改 DB schema：

| 扩展 | 方式 |
|------|------|
| 新 TTS provider | `provider_config` JSONB 存新参数 |
| CosyVoice 切 DB | 第 3 期模式复用到 CosyVoice |
| Minimax 音色导入 | 复用导入 + verify 流程 |
| 新端点/region | `verify_status` JSONB 加新维度 |
| 音色分组/标签 | `scene` + 未来 `tags` JSONB |
