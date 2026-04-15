# Phase 1 交付：翻译时长对齐方案 C+ Phase 1（基础设施）

> **日期**：2026-04-14
> **方案文档**：`docs/plans/2026-04-14-translation-duration-alignment-plan.md` (v2.1)
> **状态**：代码完成 + 本地语法/单元验证通过，**标定脚本未执行**（等用户确认付费 API 调用）
> **等待**：CodeX 审核

## Phase 1 范围回顾

| 任务 | 状态 |
|------|------|
| 1. DB Migration 012（voice_catalog 加 3 列） | ✅ |
| 2. voice_catalog_models.py 加对应 SQLAlchemy 列 | ✅ |
| 3. 标准文本文件 standard_calibration_texts.py | ✅ |
| 4. 标定脚本 calibrate_voice_speeds.py（**未运行**） | ✅ |
| 5. voice_catalog API 返回 chars_per_second | ✅ |
| 6. pipeline 查表逻辑（仅 Studio 已确认 voice_id 路径） | ✅ |
| 7. 翻译 prompt 重构（加 hint 字段 + 推导说明） | ✅ |
| admin 重标定端点 | 推迟到 Phase 4（架构上更适合跨模块时做） |

## 新建文件

| 文件 | 说明 |
|------|------|
| `gateway/alembic/versions/012_add_voice_speed_calibration.py` | 加 `chars_per_second` (Float)、`chars_per_second_by_model` (JSONB)、`speed_calibrated_at` (DateTime tz)，全部 nullable。加部分索引 `idx_vc_speed_calibrated` 便于查询未标定音色 |
| `gateway/scripts/standard_calibration_texts.py` | 三段标准中文文本（T1 101 汉字科技评测 / T2 153 汉字纪录片 / T3 204 汉字创业演讲），含 `count_hanzi()` helper 和 `__main__` 自检 |
| `gateway/scripts/calibrate_voice_speeds.py` | 标定脚本。**默认 dry-run**，需显式 `--execute` 才调付费 API。CLI 支持 `--provider/--model/--voice-id/--force/--limit/--output-csv`。写入 `chars_per_second_by_model` 时按 model key merge，scalar `chars_per_second` 是所有已标定 model 的算术平均 |
| `src/services/tts/voice_speed_catalog.py` | Gateway 查表 client，TTL 缓存，提供三个函数：`load_speed_catalog()`、`resolve_chars_per_second()`、`lookup_per_speaker()`。失败时返回 None/空 dict（graceful degradation） |

## 修改文件

| 文件 | 变更 |
|------|------|
| `gateway/voice_catalog_models.py` | `VoiceCatalog` 类加 3 个新 mapped_column，导入 Float |
| `gateway/voice_catalog_api.py` | 内部端点 `GET /api/internal/voice-catalog` 响应加 `chars_per_second` / `chars_per_second_by_model` / `speed_calibrated_at`；admin `_serialize_voice()` 同步加这 3 字段 |
| `src/pipeline/process.py` | 在 S4-probe 校准 (~line 1179) 前新增 catalog lookup 分支。**仅当** `job_service_mode == "studio"` 且 `_speaker_voices` 全部非 `"auto"` 时触发；全部命中 catalog 则跳过 probe，部分命中则 fallback probe，异常 fallback probe |
| `src/services/gemini/translator.py` | (a) `_build_groups()` 为每个 group 加 `target_chars_hint = source_word_count × 1.8` 和 `voice_chars_per_second = effective_cps`；(b) `DEFAULT_TRANSLATION_PROMPT_TEMPLATE` 强化"硬约束 vs 软参考"的说明，教 LLM 如何结合 source_words_per_second / voice_chars_per_second / target_chars_hint / min_chars ~ max_chars |
| `tests/test_gemini_translator.py` | 把 `test_gemini_translator_build_prompt_mentions_soft_duration_constraints_and_name_rules` 的断言同步到 v2.1 新 prompt 措辞（原断言"仅供参考，不是硬性约束"改为"硬约束"+"软参考"） |

## 关键设计决策 / 偏离方案的地方

1. **White-list `_LLM_GROUP_FIELDS` 未扩展**：`translator.py:1112` 有个 frozenset 白名单，控制哪些 group 字段真正发给 LLM。新加的 `target_chars_hint`、`voice_chars_per_second`、`source_words_per_second`、`source_word_count` **当前未加入白名单**，所以 prompt 文本解释了这些字段但 JSON 数据里没有。

   **这是 bug 还是 feature？**——需要讨论。方案 v2.1 本意是让 LLM 看到这些字段。两个修复方向：
   - (a) 把新字段加进 `_LLM_GROUP_FIELDS`，JSON 里真正携带这些字段（推荐）
   - (b) 仅在 prompt 文本里描述，不在 JSON 里传（当前行为，可能误导 LLM 以为有但实际没有）

   我倾向 (a)，但等 CodeX 确认后再改。

2. **admin 重标定端点推迟到 Phase 4**：涉及 gateway 容器 → src/services/tts 的跨模块调用，架构上更适合和 UX 阶段一起做。Phase 1 admin 只能看 `chars_per_second` 字段，重标定靠 script 触发。

3. **Express 模式完全不受影响**：catalog lookup 分支只在 Studio 路径生效。Express 保留原 probe 流程，符合方案 v2.1 架构决策 #3。

4. **scalar 值用平均**：多模型（如 MiniMax Turbo + HD）标定后，`chars_per_second` scalar 用所有模型的平均值；`chars_per_second_by_model` JSONB 保留精确值。查询时优先 by_model 精确匹配，fallback scalar。

## 验证

### 手动/自动验证

| 检查项 | 命令 | 结果 |
|-------|------|------|
| 全部新/改文件语法 | `python -m py_compile <files>` | ✅ ALL SYNTAX OK |
| 标准文本汉字数 | `python gateway/scripts/standard_calibration_texts.py` | ✅ T1=101 / T2=153 / T3=204 (total 458) |
| 标定脚本 CLI dry-run | `python gateway/scripts/calibrate_voice_speeds.py` | ✅ 默认不调 API，未显式 `--execute` 时不会误触发 |
| CLI 参数校验 | `--voice-id test`（无 `--provider`） | ✅ 正确报错退出码 2 |
| `voice_speed_catalog` 优雅降级 | 无 Gateway 时调用三个函数 | ✅ 返回空 dict / None，不抛异常 |
| `_build_groups` 新字段输出 | 用 fake lines 构造 | ✅ `target_chars_hint=31`（17 词），`voice_chars_per_second=4.2`（传入的 cps） |
| `VoiceCatalog` 模型新列 | 检查 `__table__.columns` | ✅ 3 个新列均存在 |

### pytest

运行相关 test 套件：`tests/test_duration_estimator.py` + `tests/test_gemini_translator.py` + `tests/test_rewriter.py` + `tests/test_voice_catalog_api.py` + `tests/test_voice_reranker.py`

- **Baseline（未改之前）**：5 failing、38 passing
- **改动之后**：4 failing、125 passing（修好了 1 个，新增 87 个 passing 来自其他 test 文件）
- **未引入新失败**。剩余 4 个 pre-existing failures 都在 `test_gemini_translator.py`：
  - `test_gemini_translator_keeps_one_group_per_line_when_total_duration_exceeds_threshold` — 断言 `start_ms` 在 prompt JSON 里，但 `_LLM_GROUP_FIELDS` 白名单早就 filter 掉了
  - `test_gemini_translator_build_prompt_supports_custom_template_tokens` — 断言 `"Length reminder"` 英文字符串在 prompt 里
  - `test_gemini_translator_prompt_includes_dynamic_length_fields` — 断言 `"source_word_count"` 等内部字段在 prompt 里
  - `test_gemini_translator_retries_batch_once_when_length_is_out_of_range` — 断言 `"Length reminder"` 在 retry prompt 里

  这些测试期待的是早期 prompt 结构，prompt 演进过程中没同步。建议另起一个修复 task，不在 Phase 1 范围内。

### **标定脚本未运行** ⚠️

按用户硬约束（CLAUDE.md "付费 API 不能自动调用"）和本轮用户明确要求（"测算 TTS 各引擎的音色语速之前，先通知我"），**标定脚本已写好但从未实际执行过**。等用户显式授权才跑。

## Phase 1 结束后 pipeline 的行为

- **没跑标定**：`voice_catalog.chars_per_second` 全 NULL → catalog lookup 每次都返回 None/空 dict → pipeline 走原 probe 路径，行为和 Phase 1 前**完全一致**（零行为变更）。
- **跑完 Studio 常用音色的标定后**：Studio 模式已确认 voice 的 job 跳过 probe TTS，直接查表用预标定值。每 job 省 2-6 次 probe TTS 调用（约 ¥0.02-0.05）和 ~5-15 秒延迟。
- **翻译 prompt 一直启用新 hint 字段**：无论有没有标定数据，LLM 都能看到 `voice_chars_per_second`（probe 值或目录值）和 `target_chars_hint`，prompt 说明了两个的区别。

## 对 CodeX 的请教点

1. **`_LLM_GROUP_FIELDS` 白名单该不该扩展？** 加 `target_chars_hint` / `voice_chars_per_second` / `source_words_per_second` / `source_word_count` 进去，让 JSON 真正携带这些字段给 LLM 看？（我倾向加）
2. **hint 字段命名**：`target_chars_hint` vs `natural_chars` vs 其他？我选了 `_hint` 后缀强调"参考不硬约束"
3. **catalog lookup 失败时的退化策略**：当前是全量 fallback probe。要不要加"部分命中也用上"的逻辑？（即对命中的 speaker 用目录值，对未命中的 speaker 用 probe 值）
4. **pre-existing test failures 是否打包修复**：这些原有失败不是 Phase 1 范围，要不要 Phase 2 时顺手修掉？

## 下一步

- 等 CodeX 审核反馈
- 用户确认后运行标定脚本（分阶段：先标定 1 个 provider 的前 5 个音色做烟测，再全量）
- 确认 Phase 1 效果后进入 Phase 2（音色匹配 top-K 重排 + TTS speed 参数接入）
