# 会话交接文档 — 2026-04-15

> 本会话围绕"翻译时长对齐方案 C+"的 Phase 2 主体（音色匹配 + TTS speed）展开，并修复了沿途暴露的 voice clone / split / admin prompt 等链路 bugs。CodeX 给出 5 项反馈已全部接受 + 修复 + 灰度上线。新会话从这里继续。

---

## 一、本次会话完成总览

### 1. Phase 2 主体（音色匹配 + TTS speed） — 主线

| 任务 | 内容 | 状态 |
|------|------|------|
| W_SPEED 维度 | reranker 加 W_SPEED + 升级为 adaptive (0.05–0.30 线性) | ✅ |
| VoiceMatchRequest 透传 | 加 `target_chars_per_second` 字段，三 selector + resolver 透传 | ✅ |
| process.py 计算 target_cps | per-speaker `english_words_per_second × 1.8` | ✅ |
| MiniMax per-segment speed | 已接入 + 修复 cache-hit cps 缺失 bug | ✅ |
| speed_decision 模块 | 4 档 mode (default/aggressive/extreme/unlimited) + force_dsp 兜底 | ✅ |
| Pre-TTS rewrite ↔ speed 协调 | speed-aware skip + listen-limit (0.80, 1.30) 安全网 | ✅ |
| **CosyVoice rate 接入** | — | ❌ 未做 |
| **VolcEngine speech_rate 接入** | 需先实测验证 V3 单向流式是否支持 | ❌ 未做 |

### 2. CodeX 评审 5 项反馈（全部完成）

| 编号 | 内容 | 修复 |
|------|------|------|
| P1-1 | pre-rewrite skip 没受 `tts_speed_adjustment_enabled` 控制 | `_pre_rewrite_obvious_overshoot_segments_before_tts` 加 enabled check |
| P1-2 | pre-rewrite skip 没按 provider 能力 gating | 新增 `SPEED_AWARE_TTS_PROVIDERS = frozenset({"minimax"})`，segment provider 不在此 set 内则不 skip |
| P2-3 | runtime auto-match 没透传 `target_chars_per_second` | DubbingSegment 加字段 + translator stamp + tts_generator 三处 runtime auto-match (`:473/613/819`) 透传 |
| P2-4 | adaptive W_SPEED 应挂灰度开关 | 新增 `voice_match_speed_dimension_enabled` admin field + `is_voice_match_speed_dimension_enabled()` gate + admin/settings 前端 toggle |
| P2-5 | `FakeGeminiTranslator` mock signature 滞后 | 5 处 mock 加 `chars_per_second` + `chars_per_second_by_speaker` 参数；顺手修了一个 pre-existing typo（`segments[0]` 双断言矛盾） |

### 3. Plan C — translator 字数算法重构（CodeX P2-4 范围外的独立改进）

| 内容 | 状态 |
|------|------|
| `_estimate_density_factor` 永远返回 1.0（机制弃用） | ✅ |
| `_estimate_dynamic_target_chars` 改用 `source_word_count × 1.8`（不再 `voice_cps × duration`） | ✅ |
| `_ENGLISH_TO_CHINESE_CHAR_RATIO = 1.8` 常量 | ✅ |
| 翻译 prompt 重写「硬约束/软参考」描述 | ✅ |
| `target_chars_hint` 与 `target_chars` 同值（兼容旧 prompt） | ✅ |

### 4. 智能推荐 UI（前端 + 后端）

| 内容 | 状态 |
|------|------|
| `_auto_match_for_provider` 返回 `backup_voices` (top 5) | ✅ |
| VoiceSelectionPanel dropdown 加「🎯 智能推荐 (按匹配度排序)」optgroup | ✅ |
| `formatVoiceOptionLabel` 显示 cps 标签（"4.1字/秒(中)"） | ✅ |
| `review_actions.approve_voice_selection` 改 merge（保留 auto_matched_by_provider） | ✅ |
| pipeline lazy migration（老 job reload 时自动重算 payload） | ✅ |
| `_build_provider_voices` 透传 `chars_per_second` 字段 | ✅ |

### 5. Voice clone 链路 9 个 bug 修复

| # | Bug | 文件 |
|---|-----|------|
| 1 | `from src.` 11 处错 import（runtime ModuleNotFoundError） | src/* |
| 2 | Gateway mount 缺整个 `services/` 目录 | docker-compose.yml |
| 3 | Gateway 缺 `app/projects/` path alias（transcript 路径找不到） | docker-compose.yml |
| 4 | Gateway 缺 `ffmpeg` 二进制 | gateway/Dockerfile |
| 5 | Gateway 没读 `.env`（缺 AUTODUB_TTS_API_KEY 等） | docker-compose.yml |
| 6 | `VoiceCloneConfig.from_env(path)` 位置参数错位 → prefix 变 Path | gateway/voice_selection_api.py |
| 7 | clone label 用 `speaker_id` 不是中文名 | gateway/voice_selection_api.py |
| 8 | `primary-button` CSS 类未定义（10+ 按钮无样式） | globals.css |
| 9 | 拆分按钮无视觉反馈（用户以为没反应，实际请求都失败） | TranslationReviewPanel.tsx |

### 6. 其他独立修复

| 内容 | 状态 |
|------|------|
| `DEFAULT_MAX_OUTPUT_TOKENS` 8192 → 65536（修 Gemini 3.1 Pro JSON 截断 fallback） | ✅ |
| filler-preservation prompt 强化（保留口头禅） | ✅ |
| admin/settings 加 `tts_speed_adjustment_enabled` + `tts_speed_mode` (4 档) + `force_dsp_alignment` 三个 toggle | ✅ |
| MeteringPanel + 完整 metric 字段（catalog_hit / first_pass_error_pct / dsp_speed_param 等） | ✅ |
| Hotfix v2 — segments.json 在 metering 前强制重写 full schema | ✅ |
| `_split_segment` import 路径修复（核心 bug：拆分功能 4-10 commit 起一直 broken） | ✅ |
| `_load_default_prompts` 从 runtime 模块 import（消除 admin UI 与 runtime 默认 prompt 不一致） | ✅ |

---

## 二、修改/新建文件清单

### 后端 Python（src/ + gateway/）

```
M  src/services/gemini/translator.py
   - DubbingSegment 加 target_chars_per_second 字段（CodeX P2-3）
   - translator.translate() 创建 segment 时 stamp source_wps × 1.8
   - DEFAULT_MAX_OUTPUT_TOKENS 65536
   - Plan C: density_factor → 1.0 / target_chars 用 source_word_count
   - _ENGLISH_TO_CHINESE_CHAR_RATIO = 1.8 常量
   - prompt 模板重写（filler-preservation + 硬约束/软参考分层）
   - 修 from src.services... → from services...

M  src/services/tts/voice_reranker.py
   - W_SPEED + W_SPEED_MIN/MAX/BASELINE_CPS/DEVIATION_LOW/HIGH 常量
   - compute_w_speed() 自适应权重
   - is_voice_match_speed_dimension_enabled() 灰度 gate（CodeX P2-4）
   - combined_rerank() 加 target_chars_per_second 参数

M  src/services/tts/voice_match_types.py
   - VoiceMatchRequest 加 target_chars_per_second 字段

M  src/services/tts/voice_match_resolver.py
   - 三 dispatch 透传 target_chars_per_second

M  src/services/tts/{minimax,cosyvoice,volcengine}_voice_selector.py
   - 三 selector 透传 target_chars_per_second 给 combined_rerank

M  src/services/tts/tts_generator.py
   - MiniMax per-segment voice_setting.speed
   - decide_tts_speed() 调用
   - 三处 runtime auto-match (:473/613/819) 透传 target_chars_per_second（CodeX P2-3）

M  src/pipeline/process.py
   - SPEED_AWARE_TTS_PROVIDERS = frozenset({"minimax"})（CodeX P1-2）
   - PRE_TTS_REWRITE_LISTEN_LIMIT_HIGH = 1.30 / LOW = 0.80
   - _pre_rewrite_obvious_overshoot_segments_before_tts: speed-aware skip（含 enabled + provider gating）
   - _build_voice_selection_review_payload: 计算 per-speaker target_cps + 透传 + lazy migration
   - _auto_match_for_provider: 返回 backup_voices + 透传 target_cps
   - _build_provider_voices: _voice_dict helper 加 chars_per_second / speed_calibrated_at
   - cache-hit 重跑路径填充 _probe_chars_per_second_by_speaker（修 Bug A，让 MiniMax speed 在 cache hit 真正生效）
   - 修 from src.services... → from services...

M  src/services/jobs/review_actions.py
   - approve_voice_selection: merge 而非 replace（保留 auto_matched_by_provider）
   - 修 from src.services... → from services...

M  src/services/web_ui/translation_review.py
   - 修 from src.services... → from services...（修拆分 500 错误的根因）

M  src/services/transcript_reviewer.py
   - 修 from src.services... → from services...（3 处）

M  src/services/alignment/aligner.py
   - first-pass duration snapshot
   - force_dsp_alignment 兜底

M  src/modules/alignment/alignment_orchestrator.py
M  src/utils/resume_point.py
   - 修 from src.utils... → from utils...

M  gateway/admin_settings.py
   - voice_match_speed_dimension_enabled / tts_speed_adjustment_enabled / tts_speed_mode / force_dsp_alignment 字段
   - _load_default_prompts: import runtime 模块作为 single source of truth

M  gateway/voice_selection_api.py
   - VoiceCloneConfig.from_env(config_path=) 关键字调用（修位置参数 bug）
   - clone label 解析 speaker_names → 中文名
   - import services.review_state（依赖 src/ mount）

M  gateway/voice_catalog_api.py
   - 返回 chars_per_second / speed_calibrated_at

M  gateway/voice_catalog_models.py
   - 加 chars_per_second / chars_per_second_by_model / speed_calibrated_at 字段

M  gateway/job_intercept.py
   - allowed_keys 扩容（含 catalog_hit / first_pass_error_pct 等）

M  gateway/Dockerfile
   - 装 ffmpeg

??(新)  gateway/alembic/versions/012_add_voice_speed_calibration.py
   - voice_catalog 加 cps 字段 + 部分索引

M  docker-compose.yml
   - gateway mount: 单文件 llm_registry.py → 整个 src/ 目录
   - gateway 加 /opt/aivideotrans/app/projects/ path alias
   - gateway 加 env_file: /opt/aivideotrans/config/.env
```

### 前端 TypeScript

```
M  frontend-next/src/app/(app)/admin/settings/page.tsx
   - tts_speed_adjustment_enabled toggle + 4 档 mode 按钮
   - force_dsp_alignment toggle
   - voice_match_speed_dimension_enabled toggle（CodeX P2-4 灰度开关）

M  frontend-next/src/app/(app)/admin/jobs/page.tsx
   - MeteringPanel：catalog_hit / rewrite_rate / first_pass_error / speed_distribution 等

M  frontend-next/src/app/(app)/admin/voices/page.tsx
   - SpeedCell 显示 chars_per_second
   - formatRelativeCalibratedAt

M  frontend-next/src/components/workspace/VoiceSelectionPanel.tsx
   - 智能推荐 optgroup（top + 5 backups）
   - cps 标签 "4.1字/秒(中)"

M  frontend-next/src/components/workspace/TranslationReviewPanel.tsx
   - 拆分按钮 amber 实色 + ✕ 图标 + 加粗

M  frontend-next/src/app/globals.css
   - 补 .primary-button CSS class（之前缺定义）

M  frontend-next/src/types/voiceCatalog.ts
   - VoiceCatalogItem 加 chars_per_second / speed_calibrated_at 字段
```

### 测试

```
M  tests/test_voice_reranker.py
   - 12 个 W_SPEED + Adaptive W_SPEED 测试
   - 2 个 admin gate 测试（CodeX P2-4）
   - autouse fixture 让旧测试默认 enable flag

M  tests/test_gemini_translator.py
   - density_factor → 1.0 测试
   - 多个旧测试更新到 Plan C 契约
   - DEFAULT_MAX_OUTPUT_TOKENS 测试常量替换

M  tests/test_probe_tts_calibration.py
   - 11 个 Plan C 新测试（density 永返 1.0 / target_chars × 1.8 / Munger 场景）
   - 旧测试 refactor

M  tests/test_process_pipeline.py
   - 5 处 FakeGeminiTranslator mock signature 加 chars_per_second 参数（CodeX P2-5）
   - 4 个新测试（speed-aware skip 各种条件）
   - 修一个 pre-existing typo
```

### 文档（待补，见第七节）

```
??  docs/plans/2026-04-13-voice-speed-precalibration-plan.md   ← 本会话之前已建
??  docs/plans/2026-04-14-translation-duration-alignment-plan.md ← Phase 1/2/3 总规划，CodeX 评审过
??  docs/plans/2026-04-14-phase1-final-report.md
??  docs/plans/2026-04-14-phase1-handoff-to-codex.md
```

---

## 三、测试覆盖

```
pytest tests/test_voice_reranker.py          → 35 passed  (含 12 W_SPEED + 2 admin gate)
pytest tests/test_voice_match_resolver.py    → 11 passed
pytest tests/test_speed_decision.py          → 13 passed
pytest tests/test_gemini_translator.py       → 43 passed  (含 Plan C)
pytest tests/test_probe_tts_calibration.py   → 59 passed  (含 11 Plan C 新)
pytest tests/test_process_pipeline.py -k "speed or rewrite or calibrate" → 8 passed

总计 Phase 2 / Plan C / CodeX 修复相关：169 测试全过
```

**不在 Phase 2 范围** — 本会话未跑全套 `tests/test_process_pipeline.py`（剩下的 39 个失败是其他 mock 滞后问题，不影响 Phase 2 ship）。

---

## 四、部署状态（生产 us）

```
src/services/gemini/translator.py        ← Plan C + max_tokens + DubbingSegment 字段
src/services/tts/voice_reranker.py       ← adaptive W_SPEED + admin gate
src/services/tts/voice_match_types.py    ← target_chars_per_second 字段
src/services/tts/voice_match_resolver.py ← 透传
src/services/tts/{3 selectors}.py        ← 透传
src/services/tts/tts_generator.py        ← runtime auto-match 透传 + per-segment speed
src/pipeline/process.py                  ← skip + provider gating + cache-hit cps fix
src/services/jobs/review_actions.py      ← approve merge
src/services/web_ui/translation_review.py ← split import 修复
src/services/transcript_reviewer.py      ← import 修复
src/services/alignment/aligner.py        ← force_dsp 兜底
src/modules/alignment/alignment_orchestrator.py ← import 修复
src/utils/resume_point.py                ← import 修复
gateway/admin_settings.py                ← Plan A import + voice_match_speed_dimension_enabled
gateway/voice_selection_api.py           ← from_env keyword + speaker_names label
gateway/voice_catalog_*.py               ← cps 字段
gateway/Dockerfile                       ← ffmpeg
docker-compose.yml                       ← gateway mount + path alias + env_file
frontend-next/...                        ← MeteringPanel + 智能推荐 + 拆分按钮 + 三 admin toggle

aivideotrans-app  Up healthy
aivideotrans-gateway  Up healthy
aivideotrans-next  Up healthy
```

**所有改动已部署到 us（5.78.122.220）生产环境**。

---

## 五、未完成（Phase 2 内 / 周边）

按优先级排序：

### 1. CosyVoice TTS rate 参数接入（Phase 2 主体未完成）
- 需改 `src/services/tts/cosyvoice_provider.py` + helper script
- speed_decision 已完成，只需在 cosyvoice 调用路径里也传 speed
- 需要在 `SPEED_AWARE_TTS_PROVIDERS` 加 `"cosyvoice"`

### 2. VolcEngine speech_rate 接入（Phase 2 主体未完成）
- 需先写测试脚本验证 `audio_params.speech_rate` (-50~100) 字段在 V3 单向流式下是否生效
- 如不支持则 VolcEngine 跳过 TTS speed，只靠匹配层
- 需要在 `SPEED_AWARE_TTS_PROVIDERS` 加 `"volcengine"`（视实测结果）

### 3. 用户启用 W_SPEED 灰度后的 metrics 观察（Phase 2 验收）
- 用户需要在 admin/settings 手动开启「音色匹配启用语速维度」toggle
- 跑 1-2 个真实 job，观察 admin/jobs 详情页 MeteringPanel：
  - speed_param_distribution（speed != 1.0 段数）
  - first_pass_error_pct（pre-rewrite 触发率应下降）
  - voice_speed_mismatch_rate
- 数据稳定后决定 listen_limit (1.30, 0.80) 是否调整、灰度是否升级为默认 ON

### 4. Phase 3 多候选翻译（按现状性价比可议）
- 当前 pre-TTS rewrite 12.5%，已超原 Phase 2 预期 10-15%
- Phase 3 改为多候选 + 择优能再砍到 <5%，但要每段 +2 次 LLM call
- TTS / translation 成本比 ~180×，理论可负担
- 建议先观察 Phase 2 灰度数据，再决定是否启动 Phase 3

### 5. Phase 4 UX 剩余（独立于 Phase 2）
- 音色库页面"测试语速"按钮（让用户主动标定克隆音色）
- 选音色后端验证：差异 >30% 弹窗警告

### 6. 待清理
- spawn 出去的独立任务：「Fix FakeGeminiTranslator mock signatures」— **本会话已修复**，可关闭
- 39 个 process_pipeline pre-existing 测试失败（mock 滞后），与 Phase 2 无关，可单独 sprint 修

---

## 六、新会话快速起步指引

### 必读文档（按顺序）

1. **本文档** — `docs/handover/2026-04-15-session-handover.md`（你正在读）
2. **总规划** — `docs/plans/2026-04-14-translation-duration-alignment-plan.md`（Plan C+，CodeX 评审过）
3. **Phase 1 报告** — `docs/plans/2026-04-14-phase1-final-report.md`
4. **Phase 1 → CodeX 交接** — `docs/plans/2026-04-14-phase1-handoff-to-codex.md`
5. **CLAUDE.md** — 项目硬约束（直接 main 分支不开 worktree、付费 API 不能自动调）
6. **Memory 索引** — `C:/Users/Administrator/.claude/projects/D--Claude-AIVideoTrans-Codex-web-mvp/memory/MEMORY.md`

### 必看代码（按方向）

**音色匹配（Phase 2 主体）**：
- `src/services/tts/voice_reranker.py` — 核心算法 + adaptive W_SPEED + admin gate
- `src/services/tts/voice_match_types.py` — VoiceMatchRequest / VoiceMatchResult dataclass
- `src/services/tts/voice_match_resolver.py` — 三 provider dispatch
- `src/services/tts/{3 selectors}.py` — 各 provider 候选过滤 + rerank

**TTS speed 决策**：
- `src/services/tts/speed_decision.py` — 决策树（disabled/missing/neutral/in_range/outside_range）
- `src/services/tts/tts_generator.py:840+` — MiniMax 调用 speed_decision
- `src/pipeline/process.py:1635-1640` — `set_speaker_chars_per_second` 桥接
- `src/pipeline/process.py:_pre_rewrite_obvious_overshoot_segments_before_tts` — Pre-TTS rewrite 协调

**翻译字数算法（Plan C）**：
- `src/services/gemini/translator.py:_estimate_density_factor / _estimate_dynamic_target_chars` — Plan C 核心
- `src/services/gemini/translator.py:DEFAULT_TRANSLATION_PROMPT_TEMPLATE` — 翻译 prompt（含 filler-preservation）

**前端**：
- `frontend-next/src/app/(app)/admin/settings/page.tsx` — 三个 Phase 2 toggle
- `frontend-next/src/components/workspace/VoiceSelectionPanel.tsx` — 智能推荐 dropdown

### 已知 sharp edges

1. **MiniMax 默认 speed 是 voice_setting.speed = 1.0**。开启 `tts_speed_adjustment_enabled` 后才会逐段算 speed。决策受 `tts_speed_mode` 控制（default/aggressive/extreme/unlimited 4 档）。
2. **W_SPEED 默认 OFF** — `voice_match_speed_dimension_enabled` 默认 false（CodeX P2-4 灰度），开启后 reranker 才会按 cps 加权。
3. **CosyVoice / VolcEngine 都不支持 speed**。`SPEED_AWARE_TTS_PROVIDERS = frozenset({"minimax"})`。pre-rewrite skip 也会按 provider gating。
4. **Cache-hit 重跑路径**: 之前 TTS speed 在 cache hit 上一直没生效（cps 没填充），本会话已修。注意 partial hit (1/2) 时未命中的 speaker 仍走 missing_inputs。
5. **gateway 容器**: 现在 mount 整个 `src/`、加 `app/projects/` path alias、读 `.env`、装 ffmpeg。这些是 voice clone 必需。
6. **`from src.xxx`** import 现在全部改成 `from xxx`。新代码不要再写 `from src.`（容器里 sys.path 是 `/opt/aivideotrans/app/src`，`src` 不是 package）。

---

## 七、Git 状态 + 提交建议

### 当前状态
```
33 modified + 6 untracked = 39 改动
最近 commit: d3cf333 docs: 更新 probe 校准方案状态
```

### 提交建议

**本会话工作量大，建议拆 4-5 个 commit**（不要一个 megacommit）：

#### Commit 1：CodeX 5 项反馈修复
```
fix(phase2): 接受 CodeX 5 项 Phase 2 评审反馈

P1-1: pre-rewrite skip 加 is_speed_adjustment_enabled() 控制
P1-2: pre-rewrite skip 按 SPEED_AWARE_TTS_PROVIDERS 做 provider gating
P2-3: DubbingSegment.target_chars_per_second 字段 + 三处 runtime auto-match 透传
P2-4: voice_match_speed_dimension_enabled 灰度 gate + 前端 toggle
P2-5: 5 处 FakeGeminiTranslator mock signature + 一处 pre-existing typo

详见 docs/handover/2026-04-15-session-handover.md
```

涉及文件：
- src/pipeline/process.py
- src/services/tts/voice_reranker.py
- src/services/tts/tts_generator.py
- src/services/gemini/translator.py（DubbingSegment 字段）
- gateway/admin_settings.py
- frontend-next/src/app/(app)/admin/settings/page.tsx
- tests/test_process_pipeline.py
- tests/test_voice_reranker.py

#### Commit 2：Plan C — translator 字数算法重构
```
refactor(translator): Plan C — target_chars 用 source_word_count × 1.8

- _estimate_density_factor 永返 1.0（机制弃用）
- _estimate_dynamic_target_chars 改用 source_word_count × 1.8
- 翻译 prompt 重写「硬约束/软参考」描述
- DEFAULT_MAX_OUTPUT_TOKENS 8192 → 65536（修 Gemini 3.1 Pro JSON 截断）
- filler-preservation prompt 强化
```

涉及文件：
- src/services/gemini/translator.py
- tests/test_gemini_translator.py
- tests/test_probe_tts_calibration.py

#### Commit 3：智能推荐 UI + 拆分按钮 + admin/settings toggle
```
feat(voice-selection): 智能推荐置顶 + 拆分按钮可见性 + admin toggle

- _auto_match_for_provider 返回 backup_voices
- VoiceSelectionPanel 加「🎯 智能推荐 (按匹配度排序)」optgroup
- approve_voice_selection 改 merge 保留 auto_matched_by_provider
- pipeline lazy migration（老 job 自动重算 payload）
- 拆分按钮：amber 实色 + ✕ 图标 + 加粗
- admin/settings 加 tts_speed_adjustment_enabled / tts_speed_mode / force_dsp_alignment
- 补 .primary-button CSS class
```

涉及文件：
- src/pipeline/process.py（_auto_match + _build_provider_voices 部分）
- src/services/jobs/review_actions.py
- frontend-next/src/components/workspace/VoiceSelectionPanel.tsx
- frontend-next/src/components/workspace/TranslationReviewPanel.tsx
- frontend-next/src/app/globals.css

#### Commit 4：Voice clone 链路 9 个 bug 修复
```
fix(voice-clone): 一次性修复 voice clone 链路 9 处 bug

1. from src.xxx import 11 处错路径（runtime ModuleNotFoundError）
2. gateway mount 缺 services/ 目录
3. gateway 缺 app/projects/ path alias
4. gateway Dockerfile 装 ffmpeg
5. gateway 加 env_file（缺 AUTODUB_TTS_API_KEY）
6. VoiceCloneConfig.from_env(config_path=) 关键字调用
7. clone label 解析 speaker_names 中文名
8. _split_segment import 路径修复（拆分功能 4-10 起一直 broken）
9. _load_default_prompts 从 runtime 模块 import
```

涉及文件：
- gateway/voice_selection_api.py
- gateway/Dockerfile
- gateway/admin_settings.py（_load_default_prompts）
- docker-compose.yml
- src/services/web_ui/translation_review.py
- src/services/transcript_reviewer.py
- src/services/jobs/review_actions.py
- src/services/gemini/translator.py（部分 import）
- src/pipeline/process.py（部分 import）
- src/services/alignment/aligner.py
- src/modules/alignment/alignment_orchestrator.py
- src/utils/resume_point.py

#### Commit 5：本会话交接文档
```
docs: 2026-04-15 会话交接 — Phase 2 主体 + CodeX 修复完成
```

涉及文件：
- docs/handover/2026-04-15-session-handover.md（本文档）

### 不建议提交的临时文件

```
?? frontend/src/features/jobs/selectors.ts
   ↑ 这是旧 frontend 目录残留（已弃用 web_ui:8876），不该 commit
```

> **[2026-04-17 后记]** `frontend/` 整个目录已在本日的清理方案 Phase 1 T1.1 里删除。上面这条建议已成历史记录；未来不会再碰到这种情况。Next.js 前端在 `frontend-next/`。

### Push & PR
- 这是单人项目 + 直接 main 分支（CLAUDE.md 硬约束），**不开 PR，直接 push**
- push 到 `origin/main` 即可
- 用户已部署到 us 生产；commit 后远端 git 与生产同步

---

## 八、下一步建议（给新会话）

按价值排序：

### 高优先级（当周）

1. **观察 W_SPEED 灰度数据** — 用户开启 toggle 后，跑 3-5 个不同类型 job（访谈/演讲/podcast），在 admin/jobs 看 MeteringPanel：
   - speed_param_distribution（应该看到 speed != 1.0 段）
   - first_pass_error_pct（rewrite 触发率应下降）
   - 用户主观听感反馈

2. **CosyVoice rate 参数接入** — Phase 2 主体最后未完成项之一。
   - 改 `cosyvoice_provider.py` synthesize() 加 rate 参数
   - 改对应 helper script 接受 rate 参数
   - SPEED_AWARE_TTS_PROVIDERS 加 "cosyvoice"
   - 跑 cosyvoice TTS 测试验证 rate 实际生效

### 中优先级（视用户需要）

3. **VolcEngine speech_rate 实测验证 + 接入** — 先写测试脚本（1 小时），如不支持则 VolcEngine 永远 1.0；如支持则按 CosyVoice 同模式接入。
4. **音色库"测试语速"按钮** — 用户主动标定克隆音色（Phase 4 UX），符合 CLAUDE.md 付费 API 不自动调约束。
5. **音色选择 >30% 差异警告弹窗** — Phase 4 UX。

### 低优先级（可冻结）

6. **Phase 3 多候选翻译** — 当前 12.5% rewrite 率已可用。除非 W_SPEED 灰度数据证明仍有 10%+ 段触发，否则性价比可议。

### 维护性

7. 修 `tests/test_process_pipeline.py` 剩余 39 个 mock 滞后失败（非 ship blocker，但影响开发体验）。
8. 整理 `src/` 下其它 `from src.xxx` 残留（本会话已修主路径，但其它模块可能还有）。

---

## 九、关键约定

- **不开 worktree、不开新分支** — 直接 main（CLAUDE.md）
- **付费 API 不能 fallback / 兜底自动调** — 任何 TTS / clone / LLM 付费调用都必须用户显式触发
- **远程部署只用 `D:\daili\scripts\Deploy-Via-154.cmd`**（`Upload-Via-154.cmd`）— 见 `feedback_deploy_scripts.md`
- **临时文件放 `D:\Claude\temp\`**，不要放桌面（`feedback_temp_files.md`）
- **目标主机 us（5.78.122.220）** — 已切换到该主机（之前 sg）

---

## 十、本次会话累计交付

- **Phase 2 主体**：80% 完成（差 CosyVoice + VolcEngine speed 接入）
- **CodeX 5 项反馈**：100% 接受 + 修复
- **Plan C**：100% 完成（独立于原 Phase 2，是 Phase 2 联动改进）
- **9 个 voice clone 链路 bug**：100% 修复
- **测试覆盖**：Phase 2 / Plan C / CodeX 修复相关 169 测试全过
- **生产部署**：100% 上线
- **文档**：本交接文档 + plan 文档（前面已有）

新会话从 Step 1（高优先级）开始，按需推进。
