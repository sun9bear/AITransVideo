# 会话交接文档 — 2026-04-02

> 本文档记录本次长会话的全部工作成果、当前状态和待执行计划，供新会话无缝衔接。

---

## 一、本次会话完成的任务总览

### 1. Block A：LLM Review 提质（全部完成）

| 阶段 | 内容 | 状态 |
|------|------|------|
| A1 | 音频预处理（16kHz mono opus 压缩）+ audio-first 主路径 + batched 策略（≤20min 整段 / >20min batch-local clip） | ✅ |
| A2 | Prompt 双版本（有/无音频）+ 共享 speaker correction rules 拆分 | ✅ |
| A3 | `_MODEL_MAP` 集中映射 + 默认 `gemini_pro` → `gemini-3.1-pro-preview` + admin 配置 | ✅ |
| A4 | Legacy fallback 最小 speaker profiling + Generator speaker 级 voice cache | ✅ |

### 2. Block B：VolcEngine 双模式改造（全部完成）

| 阶段 | 内容 | 状态 |
|------|------|------|
| B1 | Provider 支持 `resource_id` + `model` + `voice_id` 按 resource 自动选默认音色 | ✅ |
| B2 | Gateway `compute_job_policy()` volcengine express→`seed-tts-1.1` / studio→`None` | ✅ |
| B3 | 共享 `VoiceMatchRequest/VoiceMatchResult` 类型 + `resolve_voice_match()` 入口 | ✅ |
| B4 | VolcEngine catalog（338 音色）+ B1 baseline matcher + 跨 resource 安全 | ✅ |
| B5 | Generator 接入 resolver + 运行时 resource_id 推导 + mismatch retry + auto 旁路 + catalog 兼容性检查 | ✅ |
| B6 | 用户侧 VoiceReviewPanel + 后端校验（auto + 2.0 白名单 + volcengine+studio 门槛） | ✅ |

### 3. 音色目录扩充

| 项 | 结果 |
|---|------|
| VolcEngine 1.0 | **302** 音色（通用+角色扮演+视频配音+有声阅读+客服+多情感+英语） |
| VolcEngine 2.0 | **36** 音色（25 uranus + 3 英语 + 5 Saturn 角色 + 3 Saturn 客服） |
| CosyVoice | **68** 音色（59 matchable + 9 非 matchable） |
| Gemini 文本标注 | 258/338 VolcEngine 音色精修（76%覆盖率） |
| 音频 profiling | 21/338 完成（Gemini RPD 限额暂停，190 个校准 WAV 已合成缓存） |
| Saturn 兼容性 | 实测验证：全部兼容 `seed-tts-2.0`，`seed-tts-1.0` 报 55000000 |

### 4. 动态音色库第 1 期（Schema + Seed + 只读列表）

| 项 | 结果 |
|---|------|
| DB 表 | `voice_catalog` + `voice_labels` 已创建（Alembic migration 005） |
| Seed | **406 音色 + 359 标签**写入 PostgreSQL |
| Admin API | `GET /api/admin/voices`（分页+筛选 provider/resource_id/gender/verified）+ `GET /api/admin/voices/{voice_id}` |
| 前端页面 | `/admin/voices` 只读列表，侧栏已有入口，支持豆包 1.0/2.0/CosyVoice 子筛选 |
| Seed SQL 生成器 | `gateway/scripts/generate_seed_sql.py` 可离线生成幂等 INSERT SQL |
| alembic env.py | 修复了 `%` 字符在 configparser 中的转义问题 |

### 5. Codex 审核反馈修复

| 反馈项 | 修复 |
|--------|------|
| P1: voice_review 链路未限定 volcengine+studio | snapshot/handler/前端三层收口 |
| P2: handler-level 测试不足 | 补 5 个 route-level 测试 |
| P3: 临时文件 | 已清理 `.tmp-*` + 加 `.gitignore` |
| 文档基线脱节 | 更新为 338+68=406 真实值 |

---

## 二、当前代码状态

### 2.1 文件清单（本次新增/修改）

**Block A（LLM Review）：**
- `src/services/transcript_reviewer.py` — 音频预处理、prompt 双版本、模型映射、batched 策略
- `gateway/admin_settings.py` — review_model 默认值 `gemini_pro`
- `frontend-next/src/app/admin/settings/page.tsx` — REVIEW_OPTIONS 更新

**Block B（VolcEngine 双模式）：**
- `src/services/tts/volcengine_tts_provider.py` — resource_id + model + voice_id 自动选择
- `src/services/tts/tts_generator.py` — resolver 接入 + speaker cache + mismatch retry
- `src/services/tts/voice_match_types.py` — 共享类型（新建）
- `src/services/tts/voice_match_resolver.py` — 统一入口（新建）
- `src/services/tts/volcengine_voice_catalog.py` — 338 音色全量 catalog（新建→扩充）
- `src/services/tts/volcengine_voice_selector.py` — B1 baseline matcher + rerank stub（新建）
- `src/services/tts/volcengine_voice_profile_data.json` — 21 条 Phase 3 profile（新建）
- `gateway/job_intercept.py` — compute_job_policy volcengine 分支
- `src/services/web_ui/handler.py` — voice_review approve 校验收口
- `src/services/web_ui/voice_library.py` — 2.0 音色列表 volcengine+studio 门槛
- `src/services/web_ui/snapshot.py` — 传递 job_tts_provider/job_service_mode
- `src/pipeline/process.py` — legacy fallback 最小 speaker profiling
- `frontend-next/src/components/workspace/VoiceReviewPanel.tsx` — studio 音色下拉（新建）
- `frontend-next/src/app/workspace/[jobId]/page.tsx` — voice_review 渲染 VoiceReviewPanel
- `frontend-next/src/lib/api/reviews.ts` — approveVoiceReview API
- `frontend-next/src/types/reviews.ts` + `api.ts` — 新增类型

**动态音色库第 1 期：**
- `gateway/voice_catalog_models.py` — ORM 模型（新建）
- `gateway/voice_catalog_api.py` — 只读 Admin API（新建）
- `gateway/alembic/versions/005_add_voice_catalog.py` — migration（新建）
- `gateway/alembic/env.py` — 修复 `%` 转义
- `gateway/main.py` — 注册 voice_catalog_router
- `gateway/scripts/seed_voice_catalog.py` — seed 脚本（新建）
- `gateway/scripts/generate_seed_sql.py` — SQL 生成器（新建）
- `frontend-next/src/app/admin/voices/page.tsx` — 音色管理页面（新建）
- `frontend-next/src/lib/api/voiceCatalog.ts` — API client（新建）
- `frontend-next/src/types/voiceCatalog.ts` — 类型（新建）
- `frontend-next/src/components/app-shell.tsx` — 侧栏加"音色管理"

**文档：**
- `docs/specs/2026-04-01-volcengine-dual-mode-refactor-plan.md` — 双模式改造计划（已完成）
- `docs/specs/2026-04-02-volcengine-voice-catalog-expansion-design.md` — 音色扩充方案
- `docs/specs/2026-04-02-dynamic-voice-library-plan.md` — 动态音色库 4 期方案

### 2.2 测试覆盖

| 测试文件 | 数量 |
|---------|------|
| `tests/test_transcript_reviewer.py` | 32 |
| `tests/test_pipeline_speaker_fallback.py` | 5 |
| `tests/test_volcengine_tts_provider.py` | 26 |
| `tests/test_gateway_job_policy.py` + `create_job` + `snapshot` | 47 |
| `tests/test_voice_match_resolver.py` | 11 |
| `tests/test_volcengine_voice_selector.py` | 16 |
| `tests/test_tts_generator.py` | 35 |
| `tests/test_web_ui.py` (voice_review) | 12 |
| `tests/test_voice_catalog_api.py` | 17 |
| **总计** | **~201** |

---

## 三、远程部署状态（US 主机 5.78.122.220）

### 3.1 容器状态

| 容器 | 状态 |
|------|------|
| aivideotrans-app | Up, healthy |
| aivideotrans-gateway | Recreated, healthy |
| aivideotrans-next | Recreated, healthy |
| aivideotrans-postgres | Up, healthy |
| aivideotrans-caddy | Up |

### 3.2 数据库

- Alembic head: `005_voice_catalog`
- `voice_catalog`: **406** 行（volcengine 338 + cosyvoice 68）
- `voice_labels`: **359** 行（338 text + 21 audio_round1）
- 所有 seed 音色 `verify_status = {"default": {"verified": true, ...}}`

### 3.3 已知遗留

- `alembic/env.py` 的 `%` 转义修复是在容器内 patch 的，已同步到源码但需要下次 rebuild 才持久化
- CosyVoice B2 profiles（59 条）在生产环境 `/opt/aivideotrans/data/b2_voice_profiles_final.json`，本次 seed SQL 未包含（因为本地没有该文件）
- app 容器临时安装了 `sqlalchemy` + `asyncpg`（pip install），容器重建后会丢失（不影响，seed 已完成）

---

## 四、待执行：动态音色库第 2 期

### 4.1 方案文档

`docs/specs/2026-04-02-dynamic-voice-library-plan.md` 第 2 期部分

### 4.2 目标

管理员可验证、编辑、导入音色。

### 4.3 核心内容

1. **CRUD 端点**：POST/PATCH/DELETE 音色
2. **批量导入**：CSV/粘贴文本 → Gemini 整理 → diff preview → 确认写入
3. **Verify**：调 TTS API 合成测试文本验证可用性
   - VolcEngine：`synthesize(text, voice_id, resource_id=)`
   - CosyVoice：分国际/国内端点验证
   - resource_id 自动探测（未知格式先尝试 2.0 再尝试 1.0）
4. **CosyVoice 端点可用性**：从 `cosyvoice_voice_catalog.py` 的 `list_endpoint_available_voices()` 导入端点可用性到 `provider_config.endpoint_modes`，使前端可以按国际/国内筛选

### 4.4 关键架构决策

- **Provider Adapter 模式**：每个 TTS provider 实现 verify/synthesize 接口
- **Matchable 生效条件**：`matchable=True` AND `archived_at IS NULL` AND `verify_status` 至少一个维度 verified
- **seed 音色 verify_status 是"迁移基线信任"**，后续新导入必须走 verify

### 4.5 文件范围

- 修改：`gateway/voice_catalog_api.py`（加写端点）
- 新建：`gateway/voice_catalog_service.py`（verify + import 逻辑）
- 新建：`gateway/voice_provider_adapters.py`（Provider Adapter 实现）
- 修改：`frontend-next/src/app/admin/voices/page.tsx`（编辑+导入 UI）

---

## 五、已知问题 / 技术债

### 5.1 VolcEngine rerank 未激活

`volcengine_voice_selector.py:_try_rerank_with_profiles()` 仍是 no-op。21 条 audio profile 数据存在但未进入运行时决策。等第 4 期标注工具链完成后激活。

### 5.2 voice_review 触发时机

当前 Pipeline 中 voice_review 是否为 volcengine+studio 自动设为 pending 取决于 Pipeline 运行时（`process.py` 中 `wait_for_review` 逻辑）。如果 express 任务也触发了 voice_review，VoiceReviewPanel 会检测到无 2.0 音色并自动 auto-approve。

### 5.3 CosyVoice 国际/国内筛选

seed 数据的 CosyVoice `provider_config` 没有 `endpoint_modes` 字段，前端暂时合并为"CosyVoice（全部）"。第 2 期 verify 时需要把端点可用性写入 `provider_config`。

### 5.4 Gemini RPD 限额

Gemini 3.1 Pro 的 RPD 限额是 250/天。音色文本标注和音频 profiling 需要控制每日调用量。当前 Phase 2 文本标注覆盖率 76%（258/338），Phase 3 音频 profiling 覆盖率 6%（21/338）。190 个校准 WAV 已缓存在 app 容器内。

### 5.5 本机 Python 环境

Windows Store 的 `python.exe` 返回 exit code 49（打开 Store），必须用 `uv run python` 运行测试。

---

## 六、部署注意事项

### 6.1 部署脚本

参考 `docs/handover/2026-04-01-session-handover.md` 的部署脚本部分，方式不变。

### 6.2 Gateway 重建

gateway 代码改动后必须 `docker compose build gateway && docker compose up -d gateway`。

### 6.3 Frontend 重建

frontend-next 代码改动后必须 `docker compose build next && docker compose up -d next`。

### 6.4 App 重启

Python src/ 代码改动后只需 `docker restart aivideotrans-app`（bind mount）。

### 6.5 DB Migration

新 migration 在 gateway 容器内执行：`docker exec aivideotrans-gateway alembic upgrade head`。
注意：密码含 `%` 需要在 `alembic/env.py` 中转义为 `%%`（已修复）。

### 6.6 Seed 执行

seed 通过 SQL 文件直接在 postgres 容器执行：
```bash
python gateway/scripts/generate_seed_sql.py > /tmp/seed.sql
docker cp /tmp/seed.sql aivideotrans-postgres:/tmp/seed.sql
docker exec aivideotrans-postgres psql -U avt -d aivideotrans -f /tmp/seed.sql
```

---

## 七、推荐的新会话第一步

1. 读取本交接文档
2. 读取方案文档 `docs/specs/2026-04-02-dynamic-voice-library-plan.md`（第 2 期部分）
3. 确认 `git status` + working tree 状态
4. 执行第 2 期：Verify + 编辑 + 导入
5. CosyVoice 端点可用性数据导入
6. 测试 + 部署 + 汇报
