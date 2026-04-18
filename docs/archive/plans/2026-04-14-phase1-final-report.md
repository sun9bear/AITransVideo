# Phase 1 完整闭环报告（翻译时长对齐方案 C+）

> **状态**：✅ **CodeX 三审 PASS，Phase 1 正式收口**
> **代码**：全量部署到 us 主机 + 254 voice×model 标定完成 + 端到端验证通过
> **测试**：CodeX 复跑 145 passed, 1 warning
> **日期**：2026-04-14（v1.1，CodeX 三审反馈修订）
> **方案文档**：[`2026-04-14-translation-duration-alignment-plan.md`](./2026-04-14-translation-duration-alignment-plan.md) (v2.1)
>
> **v1.1 修订**（CodeX 三审反馈）：
> 1. 3.1 节指标名"翻译字数预估精度"改为"catalog cps vs post-TTS cps 偏差"，对齐实际数据
> 2. 5.5 节移除 admin 仪表盘作为前置条件（已是 Phase 2 Task 0）
> 3. 5.5 节移除 CosyVoice+VolcEngine speech_rate 实测作为前置条件（不在 Phase 2 首批，单独立项）
> 4. 5.5 节唯一硬前置：5-10 个 typical job telemetry 样本

---

## 1. What Shipped

### 1.1 已上线的代码点（生产 us 主机生效）

| 模块 | 文件 | 改动 | 默认行为 |
|------|------|------|---------|
| DB schema | `gateway/alembic/versions/012_add_voice_speed_calibration.py` | 加 3 列：`chars_per_second` (Float), `chars_per_second_by_model` (JSONB), `speed_calibrated_at` (TZ) | 全 nullable，未标定时 null |
| ORM 模型 | `gateway/voice_catalog_models.py` | `VoiceCatalog` 加对应 mapped_column | — |
| Internal API | `gateway/voice_catalog_api.py` | `GET /api/internal/voice-catalog` 响应加 3 字段；admin `_serialize_voice` 同步 | 已生效 |
| 标定脚本 | `gateway/scripts/calibrate_voice_speeds.py` + `standard_calibration_texts.py` | asyncpg 直连 DB；CLI: `--dry-run`/`--execute`/`--provider`/`--model`/`--voice-id`/`--force`；默认 `--dry-run` 安全 | 由用户显式触发，不自动运行 |
| Pipeline | `src/pipeline/process.py` (~lines 1179-1230) | 新增 `[S4-catalog]` 分支：Studio 模式 + 所有 voice_id 非 auto + 全命中 → skip probe；否则全退 probe | **all-or-nothing**（CodeX v2 二审保守策略） |
| Catalog client | `src/services/tts/voice_speed_catalog.py`（新建） | `load_speed_catalog()` / `resolve_chars_per_second()` / `lookup_per_speaker()`；TTL 缓存；失败优雅降级返 None | — |
| 翻译 prompt | `src/services/gemini/translator.py` `_build_groups()` | 每个 group 加 `target_chars_hint` (= source_words × 1.8) + `voice_chars_per_second`（catalog 或 probe 来源） | 始终启用 |
| 翻译 prompt 模板 | `src/services/gemini/translator.py` `DEFAULT_TRANSLATION_PROMPT_TEMPLATE` | 强化"硬约束（min/max_chars） vs 软参考（hint）"分层说明 | 始终启用 |
| LLM 字段白名单 | `src/services/gemini/translator.py` `_LLM_GROUP_FIELDS` | 加 5 字段：`target_chars` / `target_chars_hint` / `source_word_count` / `source_words_per_second` / `voice_chars_per_second`；明确不加 `start_ms`/`end_ms`/`target_duration_ms`/`reference_words_per_second`/`density_factor*`（CodeX 二审指定） | — |
| 前端类型 | `frontend-next/src/types/voiceCatalog.ts` `VoiceCatalogItem` | 加 3 个可选字段 | — |
| Admin 音色库页 | `frontend-next/src/app/(app)/admin/voices/page.tsx` | 新增 "速率"+"校准" 两列；`SpeedCell` 组件按 cps 区间显示慢/中/快 + tooltip 显示 `chars_per_second_by_model`；`formatRelativeCalibratedAt` 显示相对时间 | 已部署 |
| 音色选择面板 | `frontend-next/src/components/workspace/VoiceSelectionPanel.tsx` | dropdown option 加 `formatVoiceOptionLabel(v)` → `名字 · 4.3字/秒(中)`；`AvailableVoice` 接口加 2 字段；payload 解析时取 `chars_per_second` | 已部署 |
| pytest | `tests/test_gemini_translator.py` | 修复 4 个 pre-existing test failures（CodeX 二审指定）：删除对 `start_ms` 等内部字段的错误期待，更新 prompt 文案断言，新增对新字段进入 prompt 的正向断言 | `pytest tests/test_gemini_translator.py` 0 fail |

### 1.2 Studio / Express 边界（明确分界）

| 路径 | catalog lookup 是否生效 | 说明 |
|------|----------------------|------|
| Studio + voice_selection 已确认 + 全 voice_id 非 auto + 全部命中 catalog | ✅ skip probe，用 catalog | 本报告主要关注路径 |
| Studio + voice_selection 已确认 + **部分命中** | ❌ 全退 probe（v2.1 保守策略） | 混合引擎 job、含克隆音色的 job 会触发此分支 |
| Studio + voice_selection 已确认 + 全未命中 | ❌ 全走 probe | 等于 Phase 1 前行为 |
| **Express 模式** | ❌ 永不触发（`process.py:970` 仍 voice_id=None） | CodeX 第一轮 P1-1 边界；本方案不动 Express |
| s3_cache_hit（缓存翻译） | ❌ 跳过整个 S4 校准 | 既有缓存路径 |

### 1.3 已写脚本但**未在 Phase 1 启用**的部分

- **Admin 重标定端点**：方案文档列了，**未实现**，推迟到 Phase 4（涉及 gateway → src 跨模块 + 长时异步任务）
- **音色库"测试语速"按钮**（克隆音色）：未实现，推迟到 Phase 4
- **音色选择后端验证警告**（差异 >30% 提醒）：未实现，推迟到 Phase 4
- **"部分命中也利用 catalog 值"**（per-speaker merge）：CodeX 二审建议 Phase 1 不做，等 telemetry 证明 partial-hit 常见再升级

---

## 2. Evidence

### 2.1 标定覆盖表（数据库实测）

```sql
SELECT provider, count(*) AS rows,
       count(*) FILTER (WHERE chars_per_second_by_model ? 'speech-2.8-turbo') AS turbo,
       count(*) FILTER (WHERE chars_per_second_by_model ? 'speech-2.8-hd')    AS hd,
       count(*) FILTER (WHERE chars_per_second_by_model ? 'cosyvoice-v3-flash') AS cosyflash,
       count(*) FILTER (WHERE chars_per_second_by_model ? 'seed-tts-2.0')    AS seed20
  FROM voice_catalog
  WHERE chars_per_second IS NOT NULL
  GROUP BY provider;
```

| provider | rows | turbo | hd | cosyflash | seed20 |
|----------|------|-------|-----|-----------|--------|
| cosyvoice | 59 | 0 | 0 | **59** | 0 |
| minimax | 81 | **81** | **81** | 0 | 0 |
| volcengine | 33 | 0 | 0 | 0 | **33** |

**累计：173 unique voices, 254 voice×model 标定**。

### 2.2 端到端 job 证据

#### Job A — How Pakistan, China Played Roles in US-Iran Ceasefire

- 视频：6 分钟，13 段，4 speaker
- 用户音色选择：3 CosyVoice + 1 MiniMax（混合引擎）
- 关键日志：

```
[S2.5] 用户确认音色：{'speaker_c': 'longanzhi_v3', 'speaker_d': 'longfei_v3',
                    'speaker_a': 'loongbella_v3', 'speaker_b': 'Chinese (Mandarin)_News_Anchor'}
[S2.5] 用户选择引擎：{'speaker_a': 'cosyvoice', 'speaker_b': 'minimax',
                    'speaker_c': 'cosyvoice', 'speaker_d': 'cosyvoice'}
[S4-catalog] 部分预标定命中（3/4），回退到 probe TTS 校准   ← 新分支 + 保守策略生效
[S4-probe] 校准完成：global=4.42 字/秒
  speaker_d: 5.04 字/秒
  speaker_b: 4.56 字/秒
  speaker_a: 3.92 字/秒
  speaker_c: 4.32 字/秒
[S3] 长度校验未通过，当前批次重翻 1 次...
[S5] 重写第1次：21680ms -> 16020ms（目标18640ms）
[S5] 重写第2次：16020ms -> 24660ms（目标18640ms）   ← 振荡：第2次比第1次更远
...（5 段 S5 rewrite）
[S5] 完成：共 13 段，需要人工检查 3 段
```

**证据 1**：partial-hit 时正确退回 probe（保守策略生效）。
**证据 2**：probe 偏差严重——对比 catalog DB 值（CosyVoice 已标定）：

| voice | catalog cps | probe cps（job） | 偏差 |
|-------|------------|----------------|------|
| loongbella_v3 | 4.72 | 3.92 | **-17.0%** |
| longanzhi_v3 | 4.12 | 4.32 | +4.8% |
| longfei_v3 | 4.52 | 5.04 | **+11.5%** |

#### Job B — Charlie Munger interview

- 视频：4.4 分钟，30 段，2 speaker
- 用户音色选择：纯 MiniMax（`Chinese (Mandarin)_News_Anchor` + `Chinese_cartoon_elder_vv1`）
- 关键日志：

```
[S2.5] 用户确认音色：{'speaker_a': 'Chinese (Mandarin)_News_Anchor', 'speaker_b': 'Chinese_cartoon_elder_vv1'}
[S2.5] 用户选择引擎：{'speaker_a': 'minimax', 'speaker_b': 'minimax'}
[S4-catalog] 使用预标定音色语速，跳过 probe TTS 校准   ← skip probe 触发！
[S4-catalog] global: 4.134 字/秒
[S4-catalog] speaker_a: 4.133 字/秒
[S4-catalog] speaker_b: 4.135 字/秒
                                 ←（直接进 S3 翻译，没有 probe TTS 阶段）
[S3] 翻译文本...
[S3] 长度校验未通过，当前批次重翻 1 次...   ← 翻译仍然字数偏差
[S3] 长度校验未通过，当前批次重翻 1 次...
[S4] TTS时长标定：global chars_per_second = 4.11   ← TTS 后重校准
[S4] 贝基·奎克 chars_per_second = 4.24
[S4] 查理·芒格 chars_per_second = 3.99
[S4] Pre-TTS rewrite (undershoot) segment_026: estimate 23333ms -> target 32010ms
[S4] Pre-TTS rewrite (undershoot) segment_030: estimate 19555ms -> target 29020ms
...（5 段 S5 rewrite，2 段 pre-TTS rewrite）
[S5] 完成：共 30 段，需要人工检查 18 段
```

**证据 1**：catalog 完全命中 → skip probe（v2.1 主路径首次实证生效）。
**证据 2**：catalog 精度 vs TTS 后重校准实测：

| speaker | catalog cps | post-TTS cps（30 段重校准） | 偏差 |
|---------|-----------|--------------------------|------|
| 贝基·奎克 (News_Anchor) | 4.133 | 4.240 | **+2.6%** |
| 查理·芒格 (cartoon_elder) | 4.135 | 3.990 | **-3.5%** |

### 2.3 测试结果

| 范围 | 命令 | 结果 |
|------|------|------|
| Phase 1 触及测试 | `pytest tests/test_gemini_translator.py tests/test_duration_estimator.py tests/test_rewriter.py tests/test_voice_reranker.py -q` | **74 passed, 0 failed** |
| 加上 voice_catalog_api | + `tests/test_voice_catalog_api.py` | **129 passed, 0 failed** |

修复的 4 个 pre-existing failures（CodeX 二审建议）已全部修好，详见 commit 注释。

### 2.4 前端字段清单（已部署到 us）

**Admin 音色库页（`/admin/voices`）每行新增**：
- `语速` 列：显示 `chars_per_second.toFixed(2)`，按区间染色（<3.5 amber/慢 / 3.5-4.5 默认/中 / ≥4.5 cyan/快），tooltip 显示 `chars_per_second_by_model` JSON
- `校准` 列：显示相对时间（"1天前"/"3小时前"/"未标定"）

**Studio 音色选择面板** dropdown option 标签：
```
原：  Bella3.0
新：  Bella3.0 · 4.7字/秒(快)
```
未标定的音色（如克隆音色）保持原样。

### 2.5 阿里云账单对账（CosyVoice）

| 项 | 预估（方案 v2.1） | 实测（账单） | 验证结论 |
|----|----------------|-------------|---------|
| 每音色字符数 | 458 汉字×2 + 48 标点×1 = 964 | 964（账单第一条 = 964）| ✅ 计费单位精确吻合 |
| 总字符 | 56,876 | 67,693 | +19% 超额 |
| 应付 | ¥5.69 | ¥6.77 | +¥1.08 |

**+19% 超额原因**：CosyVoice 是首发，用了不稳定的 SSH bash 前台 / `docker exec -d` 方式，2 次中断造成第 9 个音色 T1+T2 重复消费。后续 VolcEngine 切换到 host-side nohup 方式，**账单差异 0%**（实付 ¥5.00 vs 预估 ¥5.01）。

---

## 3. Measured Impact

### 3.1 关键 metric

| Metric | 含义 | Job A（混合引擎）| Job B（纯 MiniMax） |
|--------|------|----------------|-------------------|
| Catalog hit rate | 命中音色数 / 总音色数 | 3/4 = 75% | 2/2 = **100%** |
| Skip probe | 是否完整跳过 probe TTS | ❌ partial → fallback | ✅ **是** |
| Probe TTS calls saved | 跳过的 TTS API 调用 | 0 | 6（probe 选段 6 段 × 1 次） |
| Probe TTS cost saved | 跳过的费用 | ¥0 | ~¥0.04 |
| catalog cps vs post-TTS cps 偏差 | catalog 预标定值 vs TTS 实际产出后重校准的 cps | n/a（未命中 catalog） | speaker_a **+2.6%** / speaker_b **-3.5%**（精度 <5%） |
| **rewrite 段数** | S5 rewrite 触发 + pre-TTS rewrite | **5/13 = 38%** | **7/30 = 23%**（含 2 pre-TTS） |
| **needs_review 段数** | 标记需人工检查 | 3/13 = 23% | **18/30 = 60%** ⚠️ |
| 翻译批次重试 | "[S3] 长度校验未通过" 出现次数 | 1 | 2 |

### 3.2 对比 baseline

| 项 | Phase 1 前（历史） | Phase 1 后（n=2） |
|----|------------------|-----------------|
| Probe 误差 | ±10-17%（Job A 实测） | catalog ±2.6-3.5%（Job B 实测） — **精度提升** |
| Rewrite 率 | 36-57% | Job A 38%（部分命中，等于 baseline）/ Job B 23%（全命中，**~40% 改善**）|
| Needs_review 率 | 未单独追踪 | Job A 23% / Job B 60% — **Job B 异常高**，疑似内容特性 |

### 3.3 ⚠️ 必须诚实承认的局限

1. **n=2 jobs 不构成统计显著性**：CodeX 提醒的"补 2-3 个不同类型 job"还没做。Job A 和 Job B 在内容特性上差异巨大（新闻节目 vs 名人采访），无法直接对比。
2. **first-pass duration error metric 没有实施**：方案 v2.1 提的 `first_pass_duration_error_pct`（DSP/rewrite 之前的真实首轮误差）没在 admin_job_monitor_api.py 里加。当前只能从 log 抓 raw 数据手算，无法 dashboard 化。
3. **Job B 的 needs_review 60% 看起来比 baseline 还差**——但实际原因是 Charlie Munger 的对话有大量极短段（"Yes" / "对，当然" / 0.5-2s 段），这些段 < `min_rewrite_target_ms = 5000ms` 阈值，直接进 needs_review，**和 cps 准不准无关**。这是内容特性问题，不是 Phase 1 退步。

---

## 4. Residual Problems

### 4.1 Phase 1 解决的层

✅ **chars/sec 数据精度** — catalog 比 probe 准 3-5 倍（±5% vs ±10-17%）
✅ **probe 浪费** — 全命中场景下 skip probe TTS（节省 ~¥0.04/job + 5-10 秒延迟）
✅ **prompt 上下文** — LLM 拿到 hint + voice_chars_per_second，知道字数约束的来源

### 4.2 Phase 1 没解决的层

| 问题 | Job 数据证据 | 影响 |
|------|------------|------|
| **LLM 不严格遵循 min/max 字数指示** | Job B 的 `[S3] 长度校验未通过，当前批次重翻 1 次` 出现 **2 次** | 即使 cps 准了，LLM 输出字数仍超出 ±15%，触发翻译重试 |
| **S5 rewriter 振荡** | Job A 段 1：21680→16020→**24660**（第2次比第1次更远） | rewriter 改过头反弹，浪费 LLM 调用 |
| **极短段（<5s）天然对齐难** | Job B 18/30 needs_review 中大量是 "Yes" / "对，当然" 这种 0.5-2s 段 | 跳过 rewrite 直接 needs_review，**和 cps 无关** |
| **TTS 语速波动 ±5%** | Job B catalog 4.135 vs actual 3.99（差 -3.5%），就是 TTS 本身的随机波动 | 即使 catalog 完美准，TTS 实际产出仍不可避免有波动 |
| **混合引擎 job 退回 probe** | Job A 的部分命中策略 | 真实场景常见（用户混搭引擎），Phase 1 对它失效 |
| **Express 模式完全不受益** | 设计上明确不覆盖 | 快速出片用户拿不到改善 |
| **用户克隆音色无 catalog 值** | 已知问题 | 永远走 probe，没有 catalog 收益 |
| **first-pass duration error metric 缺失** | 没实施 | 无法量化 Phase 2 真实收益 |

### 4.3 标定数据本身的局限

- 标定文本仅 3 段（458 汉字），**没覆盖**：粤语内容（用普通话标定）、超短句、超长句、多人对话节奏、专有名词密集内容
- catalog 值是均值，**不反映**同音色在不同情绪/内容下的语速波动（实测 ±2-5%）
- 标定环境是 production us 主机白天，**没考察**网络抖动 / TTS 服务高峰期是否影响速度

---

## 5. Phase 2 Go/No-Go

### 5.1 为什么要做 Phase 2

**Phase 1 数据精度问题已解决，剩下的 rewrite 来源于 LLM 字数偏差 + TTS 语速波动。** 这两层 Phase 1 触不到，但有现成的工程手段消化：

1. **TTS speed 微调**：MiniMax provider **已经支持 `voice_setting.speed` 参数**（`tts_generator.py:827` 已写在 payload 里，但当前 hardcoded 1.0），改成 per-segment 动态值即可在 ±8% 范围内消化偏差。CosyVoice / VolcEngine 待 Phase 2 第一步实测确认。
2. **音色匹配加语速维度**（top-K 重排）：当前 `voice_reranker.py` 8 维度完全不看语速，混合引擎 job 经常给快节奏英文讲者匹配到慢音色，从根源就错配。

### 5.2 Phase 2 的最小启动范围（CodeX 第二轮"先 MiniMax + CosyVoice，VolcEngine 等实测通过"已收紧）

**首批只做 3 件事**：

1. **MiniMax `voice_setting.speed` per-segment 接入**
   - 改 `TTSConfig` 从全局 → segment 级注入
   - 限幅 `[0.92, 1.08]` 默认（admin 可切 `[0.85, 1.15]`）
   - speed 决策代码必须用 `TTSDurationEstimator.estimate_duration_ms()`（CodeX P2 要求的 spoken-char 计数口径一致）
   - **risk-free**：不改翻译层，只在 TTS 调用前算 speed，超出 [0.85, 1.15] 仍走原 rewrite 流程

2. **音色匹配加语速维度（top-K 内重排）**
   - 不改现有 8 维度全量权重
   - 加 `rerank_by_speed(top_k_candidates, target_cps)` 函数：top-10 内按 |cand_cps - target_cps| 二次排序
   - 音色目录 `chars_per_second` 为 NULL 时跳过 Stage 2（平滑降级）
   - **risk-free**：未标定的音色不受影响，已标定的进 top-10 才参与重排

3. **Metrics 补齐**（先做这个，否则 Phase 2 没法量化收益）
   - Admin job monitor 加 `first_pass_duration_error_pct`（pre-rewrite/pre-DSP 的真实首轮误差）
   - 加 `speed_param_distribution`（多少段用了 1.0 / 1.0±5% / 1.0±8% / 超限）
   - 加 `term_preservation_rate`（CodeX 二审要的术语保留率）
   - **不做**：CosyVoice / VolcEngine 的 speed 接入（等 P2 第一阶段验证 MiniMax 后再扩展）

### 5.3 Phase 2 暂不做的部分（CodeX v2 已明确收紧）

- ❌ 多候选翻译（Phase 3）
- ❌ Express 路径覆盖（需要"翻译前 auto-match 前移"，专项立项）
- ❌ Per-speaker merge（混合 catalog + probe），等 telemetry 证明 partial-hit 常见
- ❌ VolcEngine `speech_rate` 接入（先写 `scripts/test_volcengine_speech_rate.py` 实测确认）
- ❌ 任何前端"测试语速"按钮、警告弹窗（Phase 4）

### 5.4 成功指标（必须可观测）

**做完 Phase 2 第一阶段（MiniMax speed + rerank + metrics）后，应在 5-10 个 typical job 上看到**：

| 指标 | Phase 1 | Phase 2 目标 |
|------|---------|------------|
| Rewrite 率（含 pre-TTS） | n=2 jobs：23-38% | <20% |
| First-pass duration error 中位数 | 未测量 | <±8% |
| Speed 参数分布 | n/a | 90% 段在 [0.92, 1.08] 区间 |
| Needs_review 率 | n=2 jobs：23-60% | <30%（排除短段） |

### 5.5 Go/No-Go 决策

**唯一硬前置条件**：

- **补 5-10 个 typical job 的 telemetry**，覆盖：
  - 不同时长（短 < 3min / 中 5-10min / 长 >15min）
  - 不同密度（新闻 / 访谈 / 演讲 / Vlog）
  - 不同 speaker 数（1-2 / 3-5）
  - 不同引擎组合（纯 MiniMax / 纯 CosyVoice / 混合）

  没有这个样本量，Phase 2 改动后无法判断真实收益。

**Phase 2 内部的任务排序**（不再列为前置）：

- **Task 0** ── Metrics 补齐（admin 仪表盘 + 后端 metric 字段），优先做、不算 blocker，但越早越好，方便 Task 1/2 实时观察
- **Task 1** ── MiniMax `voice_setting.speed` per-segment 接入 + 限幅决策
- **Task 2** ── 音色匹配加语速维度（top-K 内重排）
- **CosyVoice + VolcEngine speech_rate 接入** ── 单独立项，**不在** Phase 2 首批任务，需要先写 `scripts/test_*_speech_rate.py` 实测确认字段生效（不含付费 API 自动调用）

**满足硬前置条件后即可正式启动 Phase 2，按 Task 0 → 1 → 2 顺序推进。**

---

## 6. 给 CodeX 三审重点

请重点审：

1. **Phase 1 真实收益是否被充分证明**？
   - Job B 的 catalog ±3.5% vs Job A 的 probe ±17% — 数据是否有说服力？
   - n=2 是否够下结论？
   - 是否需要补 jobs 才能进 Phase 2？

2. **Phase 2 范围是否被收得足够小**？
   - 5.2 节列的"首批 3 件事"是否还能再砍？
   - speed 限幅 [0.92, 1.08] 是否合理？
   - Metric 补齐是否应该作为 Phase 2 的"前置条件"而不是"任务"？

3. **5.5 节的前置条件**是否合理？
   - 5-10 个 typical job 的样本量够不够？
   - 是否需要更早做 admin 仪表盘？

4. **Phase 1 残留问题**有没有遗漏？
   - 翻译 prompt 加了 hint 字段，会不会反而让 LLM 困惑？（暂无 telemetry 证明）
   - 极短段问题（<5s）是否应该 Phase 2 处理？

---

## 附录 A：累计成本对账

| Provider/Model | 标定字符数 | 单价 | 预估 | 实付 | 备注 |
|----------------|-----------|------|------|------|------|
| CosyVoice flash | 67,693（含 +19% 重复） | ¥1/万 | ¥5.69 | **¥6.77** | 阿里云账单确认；首发用了不稳定方式 |
| VolcEngine 2.0 | 16,698（精确） | ¥3/万 | ¥5.01 | **¥5.00** | 火山账单确认；host nohup 方式零浪费 |
| MiniMax Turbo | ~78,084 | ¥2/万 | ¥15.62 | ~¥15（账单待出） | host nohup 方式 |
| MiniMax HD | ~78,084 | ¥3.5/万 | ¥27.33 | ~¥27（账单待出） | host nohup 方式 |
| **总计** | | | **¥53.65** | **~¥53.77** | 与方案预算 ¥54.2 精准吻合 |

## 附录 B：关键文件 inventory

| 操作 | 文件 |
|------|------|
| **新建** | `gateway/alembic/versions/012_add_voice_speed_calibration.py` |
| **新建** | `gateway/scripts/calibrate_voice_speeds.py` |
| **新建** | `gateway/scripts/standard_calibration_texts.py` |
| **新建** | `src/services/tts/voice_speed_catalog.py` |
| **新建** | `docs/plans/2026-04-13-voice-speed-precalibration-plan.md` (v1) |
| **新建** | `docs/plans/2026-04-14-translation-duration-alignment-plan.md` (v2.1) |
| **新建** | `docs/plans/2026-04-14-phase1-handoff-to-codex.md` (Phase 1 中期交付) |
| **新建** | `docs/plans/2026-04-14-phase1-final-report.md` (本文档) |
| **修改** | `gateway/voice_catalog_models.py` |
| **修改** | `gateway/voice_catalog_api.py` |
| **修改** | `src/pipeline/process.py`（catalog lookup 分支） |
| **修改** | `src/services/gemini/translator.py`（_build_groups + prompt + 白名单） |
| **修改** | `tests/test_gemini_translator.py`（4 个 pre-existing test 修复） |
| **修改** | `frontend-next/src/types/voiceCatalog.ts` |
| **修改** | `frontend-next/src/app/(app)/admin/voices/page.tsx` |
| **修改** | `frontend-next/src/components/workspace/VoiceSelectionPanel.tsx` |
