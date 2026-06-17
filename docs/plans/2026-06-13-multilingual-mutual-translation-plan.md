# 多语言互翻可行性研究与执行方案

> ⚠️ **本文档（v2）已被 [`2026-06-13-multilingual-mutual-translation-plan-v3.md`](2026-06-13-multilingual-mutual-translation-plan-v3.md) 取代为执行基线。** v2 保留作为**证据底座 / 核实记录**（§3 全量耦合表、§12 核查记录仍有效且被 v3 引用）。所有「该建什么、什么顺序、逐 PR 编辑点」以 **v3** 为准；不要从 v2 派工。

- 状态：**建议立项，但需按本文 v2 的修订执行；按受控语言对灰度，不做一次性全语种开放**
- 版本：**v2（2026-06-13，已做代码级核实，取代同日 v1 草稿）**
- 日期：2026-06-13（Asia/Shanghai）
- 核实方法：6 个领域并行核查（process / translator / asr / tts-voice / subtitles / gateway-frontend）逐条打开工作树源码确认 22 条声明（0 refuted / 3 partial），并由架构评审挖掘 plan 遗漏耦合点；3 个 blocker 锚点由主程亲自抽验。详见 §12 核查记录。
- 关联历史方案：`docs/plans/2026-04-15-i18n-target-language-direction.md`（方案 B 完整清单的来源）
- 关联图谱：`docs/graphs/GITNEXUS_WORKFLOW_CORE_GRAPH.md`、`docs/graphs/GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md`、`docs/graphs/GITNEXUS_COMMERCIALIZATION_GRAPH.md`
- 关联记忆：`project_i18n_direction`（matchable=false 止血）、`feedback_copy_as_new_invariants`、`feedback_terminal_state_single_entry`、`feedback_apf_deploy_incident`、`feedback_compose_env_file_recreate`、`feedback_r2_publisher_consumer_contract`

---

## 0. 结论

**可行，但工程量被 v1 草稿系统性低估约 2-3 倍。** 核实后维持「可行」判断，但把它从「加一个语言能力层」修正为「跨 8 个子系统、约 4-6 周（单 owner 折算）的横切改造」。

主干架构不阻止多语言互翻这一点成立：TTS 单元、DSP-first 对齐、确定性字幕重定时、剪映/editor 交付对「目标语言是什么」基本中立。真正的阻力在外围与约束层，且**比 v1 列出的多一倍**。v1 准确识别了 4 个显性耦合点（英文门、翻译器、长度预算、selector），但漏掉了至少同等数量的隐性中文耦合面。

### 0.1 v1 草稿判断准确的部分（22 条声明 0 refuted）

- `process.py` 有 `_enforce_english_source_language()` / `_enforce_english_transcript_language()` 英文-only 硬门（确认 `src/pipeline/process.py:8099-8151`）。
- 翻译器中文耦合：`DEFAULT_TRANSLATION_PROMPT_TEMPLATE` 写死英→中、输出字段 `cn_text`、`_parse_response()` 只读 `cn_text`、checkpoint/segments.json 只写 `cn_text`（确认 `translator.py:109/155/1645/885`）。
- 长度预算 `_ENGLISH_TO_CHINESE_CHAR_RATIO = 1.8`、`_estimate_dynamic_target_chars()`（确认 `translator.py:2543/2546`）。
- `VoiceMatchRequest.target_language` 已存在，仅 MiniMax selector 消费（确认 `voice_match_types.py:60`、`voice_match_resolver.py:121`）。
- `voice_catalog` 有 `language` 列 + `provider_config` JSONB + GIN index（确认 `005_add_voice_catalog.py`）。
- jobs 表 / JobRecord 当前无语言字段（确认 `gateway/models.py:172-263`、`src/services/jobs/models.py:114-229`）。
- `_snap()` snapshot 机制可零迁移加语言键（确认 `process.py:2712-2727`）。
- `translation_quality.py` 写死 `TARGET_LANGUAGE = "zh-CN"`（确认 `:16`）。

### 0.2 核实新增的关键修正（v1 必须吸收）

| # | 修正 | 证据 | 影响 |
|---|---|---|---|
| C1 | **真实 pipeline 单元是 `DubbingSegment.cn_text`，不是 `SemanticBlock`** | `process.py:1604-1607` 明文注释；SemanticBlock 是 legacy/cue 路径 | 改造主对象定位 |
| C2 | **`×1.8` 有 5 处裸字面量**，不止命名常量一处 | `translator.py:138(prompt)/651/2334`、`process.py:7932/8018` | 只改一处会让长度口径分叉 |
| C3 | **`UsageMeter.record_tts` 无 `extra` 参数**，`summarize()` 忽略 extra | `usage_meter.py:118-131/176` | §4.4「extra 记 language_pair」对 TTS 不成立 |
| C4 | **matchable 止血未撤回**：生产 `volcengine` en 音色 `matchable=false`（36 行），runtime catalog 硬过滤 `matchable==True` | `voice_catalog_api.py:160`（主程实测）；`docs/plans/2026-04-15...:61-76,209` | **blocker**：Phase 5 验收在不回填的前提下不可达 |
| C5 | **checkpoint fingerprint 改 hash 会让现网 en→zh checkpoint 全失效**，触发付费重翻 | `translator.py:784-824,845` | 违反「默认零回归」+ 付费 API 成本回归 |

### 0.3 路线（维持 v1 方向，收窄 DoD）

- v1 只支持 **单任务一个 `source_language` + 一个 `target_language`**。
- v1 首发 **中文视频 → 英文配音（`zh-CN→en`），仅 Studio，且禁入 post-edit / suggest-split**（见 §2.2 non-goals）。
- 默认行为保持：未传语言字段 ≡ `source_language=en` / `target_language=zh-CN`，零迁移、零回归。
- `en→zh-CN` 为既有 GA，作为零回归基线锚点。

---

## 1. 研究方法与可信度

本研究只基于仓库现状，不引入新外部 API 依赖，不把测试路径绑到真实网络服务。

可信度：6 域核查的 22 条声明逐条打开工作树源码（非 `.codex_worktrees` 副本）确认，0 refuted、3 partial（均为带 nuance 的补充而非推翻）。3 个改变方案结论的 blocker 锚点（matchable 硬过滤、post-edit 字段白名单、第二字幕写出器）由主程在本会话亲自 Read 确认。所有 file:line 引用均来自实读。

**核查发现的总体形态**：v1 对「显性」耦合点（有明确英文/中文字面量的 prompt、常量、字段名）判断准确；但对「隐性」耦合点（按英文词分词的计数器、按 CJK 标点切分的算法、第二/第三套并行写出器、跨 provider fallback 链、绕过校验点的第二创建路径）几乎全部遗漏。隐性耦合的共同危险特征是**静默失效**——中文源喂给「只数 Latin token」的计数器会得到 0 而非报错，使长度预算、探针校准、audience guard 默默退化到默认值。

---

## 2. 产品定义

### 2.1 v1 定义

```text
一个任务 = 一个源语言 source_language + 一个目标语言 target_language
输出 = 目标语言配音 + 源/目标/双语字幕 + 剪映草稿
首发 = zh-CN → en，Studio，内测 allowlist
```

语言对推进梯队：

- `en → zh-CN`：现网默认，保持完全兼容（零回归基线）。
- `zh-CN → en`：**首发新增方向**，工程对称性最好；但首发受 §2.2 non-goals 收窄。
- `ja → zh-CN` / `zh-CN → ja`：第二批，需 ASR/TTS 能力确认 + 字幕层 per-script。
- `en → es/fr/de`：第三批，依赖目标语言音色池与字幕基准集。

### 2.2 v1 不做（non-goals，必须对内测用户显式声明）

核查发现首发 `zh-CN→en Studio 内测`会撞上一批未改造路径。为让 v1 可控落地，以下显式划出范围外，**Gateway 对非默认 pair 主动拒绝进入这些路径**（返回明示错误，而非静默走错路径）：

- **不做 post-edit / 编辑态**：非默认 pair 任务 `enter-edit` 返回 409 + `post_edit_not_supported_for_language_pair`。理由：post-edit 可变字段白名单只有 `cn_text`（`editing_segments.py:98-99`，主程实测），且整套审校/拆分 UI 是「源=英文」硬假设（§3.8）。
- **不做 suggest-split 多模态识别**：非默认 pair 返回 501。理由：`editing_split_suggest.py:64-84` prompt 写死「英文原文/中文译文」+ 英文 exact-match 定位契约。
- **不做同一任务内多目标语言 / 逐段自动判断 target**。
- **不做 mixed-language 保留源语言夹杂片段**。
- **不扩 Free / Anonymous / Smart**（首发只 Studio；Express 待 TTS provider 兼容矩阵验证后）。
- **字幕只承诺 target SRT 内容正确**，不承诺 Latin 目标的排版/断行最优（字幕 per-script 引擎延后，§3.7）。
- **剪映 draft 轨名、`*_zh.srt` 兼容文件命名延后**（§3.7）。

这些不是永久不做，而是把首发风险锁在「转录→翻译→TTS→对齐→交付字幕」的非交互主干上。

---

## 3. 已核实的语言耦合点全图

按子系统组织。标注 **[v1]** = 草稿已识别；**[新]** = 核查新增；severity 为对首发 `zh-CN→en` 的阻塞度。

### 3.1 主 pipeline 编排层（`src/pipeline/process.py`，12k 行）

| 耦合点 | 证据 | severity | 处置 |
|---|---|---|---|
| **[v1]** 英文-only 输入门 | `:8099-8151`；S0 后 `:2923`、S1 后 `:3011`；ASCII 字母占比阈值 0.6 | high | 改 source-aware；**保留 metadata 缺失即放行（fail-open）语义**，否则本地上传全挂（`:8100-8102`） |
| **[新]** voice_selection 音色池 builder 写死中文 | `:7726 _build_voice_selection_review_payload`；VolcEngine 只收 `ICL_zh_/zh_/saturn_zh_`（`:7796-7807`），MiniMax 只收 `中文-普通话/中文-粤语`（`:7821-7829`）；不传 target_language（`:7880-7889`）。**同时驱动 Smart 自动决策**（`:4886-4891`） | **high** | 非中文 target 的审核页与 Smart 自动匹配将只有中文音色 |
| **[新]** S4 probe 选段按英文词计数 | `_count_source_words` 用 `[A-Za-z0-9']+`（`:11597-11600`）；min_words=20 过滤（`:11672`）；中文源 word≈0 → 探针全空（`:11914`）→ 整条 cps 校准/speed 维度静默退化 | **high** | 首发源是中文，必踩 |
| **[新]** 长段修复 split 正则常量写死中目标/英源 | `:342 FAILED_SEGMENT_SEMANTIC_SPLIT_PATTERN`（不含 ASCII `.`）/ `:343 ..._SOURCE_SPLIT_PATTERN`（纯 ASCII）；消费 `:9940/9944` | high | en target 句末多为 `.`→无法语义拆分 |
| **[新]** `×1.8` 第二份拷贝（speed-aware voice match） | `:7909-7932 speaker_target_cps = wps×1.8`→前端 payload `:8016-8021`→`_auto_match_for_provider` | medium | 只改 translator 会让 voice speed 口径与翻译长度分叉（与 C2 同源） |
| **[新]** job metering payload 按「中文字符」定义 | `:1595-1626 _build_job_metering_payload`；`final_cn_chars` 计为 total Chinese chars（`:1694`）；term_preservation 假设中文术语 substring | medium | 直报 Gateway 计费/质量口径，多语言后无声错位 |
| **[新]** S2 产出 `display_title_zh` 直写 Gateway display_name | `:3204-3214` | low | 需区分「UI 语言产物」（保持中文）vs「目标语言产物」 |

> **关键事实**：除两个 gate 外，`source_language`/`target_language` 字符串在 `process.py` 全文零出现——编排层今天没有任何语言管道可复用，必须新建（见 §6 PR-W）。

### 3.2 翻译器 / 改写器 / 长度预算（`src/services/gemini/translator.py`、`rewriter.py`）

| 耦合点 | 证据 | severity | 处置 |
|---|---|---|---|
| **[v1]** 默认翻译 prompt 写死英→中 + `cn_text` 输出/解析/持久化 | `translator.py:109/155-160/1645/885-891/634-673` | high | prompt registry + parser 兼容 |
| **[v1]** `_ENGLISH_TO_CHINESE_CHAR_RATIO=1.8` / `_estimate_dynamic_target_chars` | `:2543/2546/2564` | high | language pair profile；**注意 C2 共 5 处落点** |
| **[v1]** rewrite prompt 写死中文 | `DEFAULT_REWRITE_PROMPT_TEMPLATE :161-184` | high | 线上走 `GeminiRewriter`；`rewrite_engine.py` 是休眠死代码（`AlignmentOrchestrator` 无生产构造点） |
| **[新]** `_count_source_words` 只数 Latin token | `:2482-2483` | **high** | CJK 源 → wps=0 → 长度预算静默退化 + per-speaker reference wps 中位数机制失效 |
| **[新]** 长度重试验证器 + rewrite 字数口径全是 char 级 | `_count_cn_chars :1712`、`_needs_translation_retry_for_length :1716`、`rewriter.py:23/83/196/213/279` | **high** | en target 一词≈5 字母，gate 系统性偏差 ~5× |
| **[新]** probe 翻译整条路径写死英→中 | `PROBE_TRANSLATION_PROMPT_TEMPLATE :57-87`；`translate_probe :683`；独立 override key `probe_translate`（`:1875-1891` 只验 `__GROUPS_JSON__`） | medium | 产出 per-speaker cps 直灌长度预算 |
| **[新]** GeminiRewriter 有 3 条 prompt 路径，2 条绕过模板 | `(a)_build_short_content_compact_prompt :186-229` 整段硬编码中文+「英文原文：」标签；`(b)_build_rewrite_prompt :277-292` 在可覆盖模板后追加硬编码中文尾巴 | medium | 即便 admin 换 language-aware 模板，尾巴仍强制中文输出 |
| **[新]** DubbingSegment 伴生字段语义/drift 契约 | `first_pass_cn_text :281`、`tts_input_cn_text :290`（`cn_text != tts_input_cn_text` 是 cue 生成的「字幕改了音频没改」安全契约 `:284-289`）、`target_chars_per_second :293-297` | medium | target_text 双写后有 drift 检测错对字段风险（→ §4.3 裁决） |
| **[v1]** admin prompt override 老模板只要求 `__GROUPS_JSON__` | `validate_translation_prompt_template :1867-1872` | high | 非默认 pair override 未声明语言 → fail-closed 回默认 |
| **[v1]** checkpoint fingerprint 可加 pair 维度 | `_build_translation_fingerprint :784-824` | — | **但见 C5：改 hash 会 bust 现网 checkpoint**，需 default-pair-preserving |
| **[新]** speaker infer prompt 写死英文源 | `DEFAULT_SPEAKER_INFER_PROMPT_TEMPLATE :100`；英文 backchannel 示例 `:1074-1078`；独立 override key `s2_infer` | low | — |

### 3.3 ASR / 转录（`assemblyai/transcriber.py`、`gemini/transcriber.py`）

| 耦合点 | 证据 | severity | 处置 |
|---|---|---|---|
| **[v1]** AssemblyAI `language_code` 没参数化 | `:14 DEFAULT_LANGUAGE_CODE="en"`；`_build_transcription_config :262-273` 硬写；调用侧 `process.py:3002-3007` 不传 | high | transcribe 接受 job source language，默认仍 en |
| **[新]** AssemblyAI 英文耦合远不止 language_code | `:24-27` 英文 filler prompt（um/uh/you know）+ `disfluencies:True`（`:268`）+ speech_models 锁死（`:23/280`）；diarization 部分语言不可用 | **high** | 需 per-language ASR profile，不是换个 code |
| **[新]** AssemblyAI 转录行构建是 Latin 标点+空格假设 | `SENTENCE_END_PATTERN [.?!;] :28` 不含全角；`_ends_sentence :704` 在 CJK 永远 False；`_join_tokens :708` 空格连词 | **high** | zh/ja 源产出超长段 + 空格夹字文本，污染 S2/split/字幕 |
| **[v1]** Gemini transcriber prompt 写死英文 | `:34-59` 「输出完整的英文转录稿」「不要翻译」；metadata default `language="en"`（`:140`） | high | 加 source language 入参 |
| **[新]** TranscriptResult.language 缺失 fallback "en" | `:735-750` | medium | 多语言后改 fail-closed 或显式 unknown |

### 3.4 S2 三轮审校（`src/services/transcript_reviewer.py`）

| 耦合点 | 证据 | severity | 处置 |
|---|---|---|---|
| **[新]** glossary 抽取方向写死「English term→中文翻译」，且直灌翻译 prompt | 输出契约 `"English term":"中文翻译" :626-628`；示例 `:1535-1538`；被 translator 以「严格遵循」注入（`translator.py:1605-1608/770-771`） | **high** | zh→en 时方向反转，把英文译文往中文带 |
| **[新]** S2 admin override（pass1/2/3）无 language fail-closed | `_get_admin_prompt_override :271-295`，消费 `:1247/1638/1991` 无条件覆盖 | **high** | §4.5 的 fail-closed 只覆盖了 Translator，未覆盖 S2 |
| **[新]** S2 三轮 prompt 语言硬编码远超 v1 一句话 | Pass1 姓名中文化 `:1162-1198` + 英文 backchannel `:591-615`；Pass2 `display_title_zh` 规则「不要用英文原题」`:1483-1543`；Pass3 voice_description 全中文 `:1786-1840` | medium | 区分 UI 语言 vs 目标语言，逐条清单化 |
| **[新]** `_rough_spoken_word_count` 不匹配假名/谚文 | `:3234-3236 [A-Za-z0-9]+|[一-鿿]`；驱动 audience guard `:3151-3155` | low | ja/ko 源 audience guard 判定面放大 |
| **[新]** CLAUDE.md「MiMo 走 legacy 单次」「Pass1/2 失败→legacy」已 stale | `review_transcript :754-771` 捕获失败直接 return None；`legacy_review_transcript_single_pass` 无生产 caller；MiMo 已并入三轮内部 dispatch（`:1340/2077/1587`） | medium | plan 应显式决定 legacy prompt（`:638-708`）随改或确认死代码后删 |

### 3.5 TTS / 音色匹配（`src/services/tts/*`、gateway voice_catalog）

| 耦合点 | 证据 | severity | 处置 |
|---|---|---|---|
| **[新]** matchable 止血死锁 | runtime catalog 硬过滤 `matchable==True`（`voice_catalog_api.py:160`，主程实测）；生产 volcengine en 36 行 matchable=false；MiniMax seed 仅 `{中文-普通话,中文-粤语,英语}` 置 matchable（`seed_voice_catalog.py:210`） | **blocker** | 不回填 matchable，Phase 5「en target 返回兼容音色」不可达；提前回填又复发 2026-04-15 事故 → **PR-E 必须原子迁移** |
| **[v1]** VolcEngine/CosyVoice selector 不消费 target_language | `voice_match_resolver.py:79-109`；selector 签名无 target_language | high | 加入参 |
| **[新]** 跨 provider TTS fallback 链是语言盲区 | `tts_strategy.py:64-78 get_fallback_provider` minimax/volc→cosyvoice（中文-only 池 + `longanyang` 兜底 `cosyvoice_voice_selector.py:107/470`）；TTSGenerator 自动切（`:1221-1247`） | **high** | en target 失败会被静默路由到中文-only CosyVoice |
| **[新]** TTS 合成 payload 不带语言参数 | MiniMax 无 `language_boost`（`tts_generator.py:1529-1541`）；VolcEngine V3 无 `explicit_language`（`volcengine_tts_provider.py:161-176`），default speaker 硬编码 `zh_female_shuangkuaisisi` | medium | 「中文音色读英文」稳定性很可能依赖该参数；Phase 5 兼容矩阵验证会失真 |
| **[v1]** `count_spoken_chars` 保留 CJK+Latin+数字；reranker speed 按 chars/sec | `duration_estimator.py:6-10`；`voice_reranker.py:164-188` 基线 4.20 cps（中文库均值） | high | per-language spoken unit |
| **[新]** per-voice cps 是中文语料单值 | `voice_catalog.chars_per_second` 校准用 `count_hanzi`（`calibrate_voice_speeds.py:76`）；`VoiceMatchRequest.target_chars_per_second` 契约写死「Chinese hanzi/sec」 | medium | 单位已知=word 但 catalog 值是 hanzi/sec → 不可比；需 per-language 重校准 |
| **[v1]** UserVoice 无语言元数据 | `010/028/030 migration` 无 language 列；Express auto-clone 注册 payload 无语言（`pipeline_clients.py:110-135`） | medium | **需新 migration（v1 未列）** + 3 条写入路径 stamping |
| **[新]** voice_catalog.language 跨 provider 双约定 | CosyVoice/Volc 存 `zh`/`en`，MiniMax 存 `中文-普通话`；`calibrate_voice_speeds.py:264` 被迫双模式匹配 | low | registry 需 per-provider 码值映射 |
| **[新]** DubbingSegment 无 target_language 字段 → MiniMax 现网恒落中文池 | `tts_generator.py:1454 getattr(segment,"target_language",None)` 恒 None | — | PR-E 验收须点名给 DubbingSegment/segments.json 加 per-segment target_language |

### 3.6 Gateway / 数据库 / 服务模式创建路径

| 耦合点 | 证据 | severity | 处置 |
|---|---|---|---|
| **[v1]** jobs 表 / JobRecord 无语言字段 | `gateway/models.py:172-263`、`jobs/models.py:114-229` | — | additive migration 036 + lockstep |
| **[v1]** job_intercept 是创建校验点 | `intercept_create_job :1077-1093`；entitlements/plan_catalog/admin_settings 职责如 §4.2 | — | 加 pair 校验 |
| **[新]** 匿名预览是第二条创建路径，绕过 job_intercept | `anonymous_preview_api.py:1419-1425` 直接 POST job_api_upstream；`anonymous_preview_payload_spec.py:14-35` frozenset 白名单不含语言字段，多字段即 500 | **high** | 必须显式声明匿名 lane 语言锁定靠 JobRecord 缺省值、白名单不动；纳入回归 |
| **[新]** PG insert / metering_snapshot / copy_as_new 逐字段枚举 | `job_intercept.py:1634-1670 insert`；`:1699-1705 metering_snapshot`；`:4747 copy_as_new copy_row=Job(` | medium | 语言字段须显式加三处；copy_as_new 漏加会静默回落缺省（`feedback_copy_as_new_invariants`） |
| **[新]** 多语言灰度开关形态未定 | 仓库惯例：双层 kill switch = env（`config.py`）+ AdminSettings bool 热翻（`admin_settings.py:192 smart_mode_enabled`）；entitlements 单一真源、admin 不可绕、fail-closed | medium | 照此落 `language_pairs_enabled`/allowlist + entitlements 出口统一计算 + **admin 前端开关 UI（v1 未提）** |
| **[新]** migration 惯例未写 | alembic `versions/NNN_`（最新 035→新 036）；models lockstep autogenerate 对账；NOT NULL 带 server_default；`docker compose run --rm --no-deps gateway alembic upgrade head` 前 `--sql` dry-run；gateway 需 `PYTHONPATH=app/`（`feedback_apf_deploy_incident`） | medium | Phase 1 落成 checklist |

### 3.7 字幕 / 剪映 / 产物命名 / 质量报告

| 耦合点 | 证据 | severity | 处置 |
|---|---|---|---|
| **[新]** `ensure_whisper_alignment` 是第二字幕写出器 | `:404-410`（主程实测）写死 `subtitles_zh.srt`+`subtitles.srt` alias；剪映 draft（`jianying_draft_runner.py:1060-1077`）与 materials pack（`api.py:1750`）前整体重建 cue，从 segments.json 读死 `cn_text/source_text` | **blocker** | 只改 dispatcher，zh→en 一点「生成剪映草稿」就用 zh whisper 重建覆盖正确产物 |
| **[新]** 字幕 cue 切分/时间分配/DTW 整体 CJK 硬编码 | `semantic_segmenter.py:62-145` 中文标点边界 + CJK=1.0/其他=0.5 等效长度；`cue_timing.py` 同权重；`cue_pipeline.py:240` whisper 强制 `language="zh"` + DTW 字符级 `_align_chars_to_words`（`dtw.py` 契约即 cn_text 逐字 vs whisper words，数字归一为中文数字） | **blocker** | Latin target 需词级对齐，不是换 language 参数；Phase 6 实为 per-script 字幕引擎改造（1-2 周量级，非 1-2 天） |
| **[v1]** 字幕文件名 `subtitles_zh/en/bilingual.srt`，`subtitles.srt` alias=zh；`SubtitleCue.text/en_text` 固定中英 | `editor_package_writer.py:363-371`；`cue_models.py:67-68`；`srt_writer.py:104-139` | high | 新增 source/target 通用文件 |
| **[新]** artifact key 同步面 ≥9 处，且 §3.7 v1 兼容策略答错对象 | 真兼容对象是 artifact key `editor.subtitles`（=zh 无后缀，`output_dispatcher.py:269`），非 `subtitles.srt` 文件；同步面：`downloadable_keys.py:54-80`、`web_ui/constants.py:30-41`、`read_surface.py:14-23`、`jobs/api.py:749-757`、`materials_pack_common.py:20`、`r2_publisher.py:174-179`、前端 `types/jobs.ts:118-120`+`mappers.ts:23-78`+`downloads.ts:92-94`+`ResultMediaCard.tsx:473` | **high** | PR-F 必须枚举全部并带回归守卫（CLAUDE.md 已警告 key 集合漂移） |
| **[新]** 下载文件名（RFC 6266）硬编码 `_zh/_en` 且被 R2 registry 持久化 | `r2_publisher.py:159-179 _filename_for`→写入 `ArtifactRegistryEntry.filename`（`:83`）持久化；gateway 下载优先读 registry（`job_intercept.py:2171`）；materials pack arcname 取磁盘文件名（`materials_pack_common.py:97`） | **high** | zh→en 英文内容会存成 `*_zh.srt`（`feedback_r2_publisher_consumer_contract` 同源） |
| **[v1]** `translation_quality.py` 写死 zh-CN + CJK/Latin 比例 | `:16-63`，gate 仅 shadow | medium | target-aware script gate，shadow-only |
| **[新]** 剪映轨名 `zh_subtitle` 硬编码 + 字幕宽度报告 CJK 预算 | `jianying_draft_writer.py:266/381`；`quality.py:35 max_display_width=32`+`text_width.py CJK=2` | low | v1 延后（non-goal） |

### 3.8 前端

| 耦合点 | 证据 | severity | 处置 |
|---|---|---|---|
| **[v1]** 提交模型 / 表单无语言字段 | `types/jobs.ts:159-191`；`TranslationForm.tsx` 零命中 | — | 加 selector，从 Gateway facts 读 |
| **[新]** 编辑/审校 UI 整套「源=英文/目标=中文」硬假设 | `types/reviews.ts:30 cnText`；`lib/api/editing.ts:33/110/182 cn_text`；`SegmentRow.tsx:458/470`（aria「英文原文」）；`edit page.tsx:466`（Source text (English)）；`SplitSegmentDialog.tsx:410/649/675`（英文词边界 snap + 英→中切点比例镜像，方向完全反转） | **high** | 首发 zh→en Studio 内测进审校/post-edit 即语义错乱 → v1 经 §2.2 non-goal 禁入；GA 前需独立「review UI 语言中立化」工作项 |
| **[新]** 下载列表语言标签/availability 硬编码中英 | `mappers.ts:25-27`「中文字幕」/「英文字幕」；`downloads.ts:92-94 subtitles_zh/en/bilingual`；`ResultMediaCard.tsx:473` | medium | 标签改语言字段驱动「目标语言字幕」/「源语言字幕」 |
| **[新]** 营销/SEO/support 知识库写死「仅英文源」 | `support_knowledge.py:59-66`（支持 AI 会否认新能力）；`faq.tsx:34`、`hero.tsx:84/99`、`lib/seo/site.ts:37`、`app/layout.tsx:27`；前端无 i18n 框架，全中文硬编码 | low | GA 前同步；语言对展示名只能靠 Gateway facts 的 `label` 下发（唯一来源） |

---

## 4. 推荐架构

### 4.1 Language Registry（`src/services/language_registry.py`）

统一管理（注意：要覆盖核查发现的全部口径，不止 v1 列的几项）：

- **规范化**：`zh/zh-CN/zh-Hans → zh-CN`；per-provider 码值映射（解决 §3.5 双约定：CosyVoice/Volc `zh` vs MiniMax `中文-普通话`）。
- **ASR profile**（per-language，解决 §3.3）：AssemblyAI `language_code` + `disfluencies` 适用性 + filler prompt + speech_models 可用性 + diarization 可用性；Gemini/Whisper code。
- **TTS code**：MiniMax display language / `language_boost`、VolcEngine `explicit_language`、Whisper code。
- **script family**：`cjk/latin/kana/hangul/mixed` → 驱动字幕 segmenter/DTW 的 char-vs-word 策略（§3.7）。
- **spoken unit**：`word/spoken_char` → 驱动 §3.2 的 `_count_source_words`、长度 gate、rewrite 计数、TTS duration estimator 统一换算。
- **LanguagePairProfile**：自然长度 ratio、prompt template key、quality gate、**per-capability 适配位**（`probe/s2/suggest_split` 是否已适配 → 未适配则付费路径 fail-closed，见 §4.6）。

```python
SUPPORTED_LANGUAGE_PAIRS = {
    ("en", "zh-CN"): LanguagePairProfile(..., status="ga",
        natural_length_ratio=1.8, source_unit="word", target_unit="spoken_char",
        capabilities={"probe", "s2", "suggest_split", "post_edit"}),
    ("zh-CN", "en"): LanguagePairProfile(..., status="internal",
        natural_length_ratio=0.55, source_unit="spoken_char", target_unit="word",
        capabilities=set()),  # 首发禁 post_edit/suggest_split
}
```

第一批 ratio（来自 2026-04-15 旧方案 + 待样本校准）：

| Pair | source unit | target unit | 初始 ratio | 备注 |
|---|---|---|---:|---|
| en→zh-CN | word | CJK spoken char | 1.8 | 现网真值（**C2：5 处落点全部引此 registry**） |
| zh-CN→en | CJK char | English word | 0.55 | 旧方案估计，需样本校准 |
| ja→zh-CN / zh-CN→ja | char/mora proxy | CJK char / proxy | 0.9-1.1 | 先 shadow |
| en→es/fr/de | word | word | 1.1-1.3 | 需基准集 |

### 4.2 Gateway 是语言可用性的 source of truth

语言能力事实归 Gateway，前端只消费（不自建语言/价格真相）。**落点裁决（v1 三选未决，此处定）**：

- `admin_settings.py`：存 pairs 灰度配置（`language_pairs_enabled` + allowlist），沿用 smart kill switch 双层模式（env + AdminSettings bool 热翻）。
- `entitlements.py`：`get_effective_allowed_language_pairs(plan, service_mode)` 单一出口统一计算，admin 不可绕、store 不可读时 fail-closed。
- `plan_catalog.py`：仅当语言对涉及定价/套餐展示时介入。
- **admin 前端**：`app/(app)/admin/settings/page.tsx` 加 pairs 开关 UI（v1 未提，必须补）。

返回前端的 facts（`label` 是非默认 pair 的唯一展示名来源，因前端无 i18n 框架）：

```json
{
  "language_pairs": [
    {"source_language":"en","target_language":"zh-CN","label":"英文 → 中文","service_modes":["express","studio","smart","free"],"status":"ga"},
    {"source_language":"zh-CN","target_language":"en","label":"中文 → 英文","service_modes":["studio"],"status":"internal","capabilities":["transcribe","translate","tts","subtitles","jianying"]}
  ]
}
```

### 4.3 Job Schema + **target_text 单一写者裁决（blocker 级）**

PostgreSQL `jobs`（migration 036，NOT NULL 带 server_default）：

```sql
ALTER TABLE jobs ADD COLUMN source_language VARCHAR(20) NOT NULL DEFAULT 'en';
ALTER TABLE jobs ADD COLUMN target_language VARCHAR(20) NOT NULL DEFAULT 'zh-CN';
ALTER TABLE jobs ADD COLUMN language_pair  VARCHAR(50) NOT NULL DEFAULT 'en->zh-CN';
```

`JobRecord` 同步加同名字段（lockstep）。

**target_text 单写裁决（修正 v1 §3.3 的双写方案）**：

v1 的「pipeline 双写 `target_text`+`cn_text`、新代码优先读 `target_text`」与现网架构**互斥**——post-edit 可变字段白名单只有 `cn_text`（`editing_segments.py:98-99`，主程实测），text_dirty 判定、split 切片、accept-draft 回填全部只动 `cn_text`；`src/` 下 `cn_text` 有约 392 处/50 文件消费者 + drift 契约（`cn_text != tts_input_cn_text`）+ copy_as_new 显式字段表。双写会让任何被编辑过的任务 `target_text` 变 stale，「优先读 target_text」的新消费者读到编辑前文本。

**裁决：v1 不做双持久化字段。**

- `cn_text` 保持为 **canonical 目标语言文本容器**（语义从「中文文本」泛化为「目标语言文本」，字段名暂不改）。
- `segments.json` 加**顶层 + 逐段 `source_language`/`target_language` 元数据**（这是 §3.5 MiniMax target_language 贯通的锚点）。
- `target_text` 仅作**翻译器 JSON 输出别名**（parser 优先读 `target_text`、缺失回 `cn_text`）与**导出层字段**，不进 persistence/不进 post-edit 白名单。
- 字段重命名（`cn_text → target_text` 全量）延到后续**独立原子 sweep PR**，不在本方案范围。

兼容规则：

- 字段缺失：默认 `en->zh-CN`（JobRecord 缺省值兜底）。
- 不支持 pair / service_mode 不支持：Gateway `job_intercept` create 前 400/403，错误码 `language_pair_not_allowed`。
- **匿名 lane（绕过 job_intercept）**：语言锁定靠 JobRecord 缺省值，`anonymous_preview_payload_spec` 白名单**不动**；回归测试断言白名单不含语言字段。

### 4.4 Pipeline 消费语言（PR-W 集中承载）

`process.py` 早期：

```python
job_source_language = normalize_language(_snap("source_language", "en"))
job_target_language = normalize_language(_snap("target_language", "zh-CN"))
language_profile = resolve_language_pair(job_source_language, job_target_language)
```

贯通点（11+ 处，核查后补全）：S0/S1 source gate（保留 fail-open）→ ASR profile（language_code + disfluencies + filler + diarization 适用性）→ S2 三轮 prompt + glossary 方向 → translator prompt/parser/fingerprint → probe 模板 → 长度 profile（5 处 ×1.8）→ `_count_source_words` 源分词器 → voice_selection 音色池 builder → VoiceMatchRequest.target_language → TTS speed unit + 合成 payload 语言参数 → 长段修复 split 正则 → cue whisper language + DTW 单位 → 输出命名 → quality report → **metering**。

**metering 口径（修正 C3）**：`record_tts` 无 `extra`，需改签名或 summary 层注入；`metering_snapshot`（`job_intercept.py:1699`）+ pipeline metering payload（`process.py:1595`）加语言字段；`final_cn_chars` 等 cn 命名字段语义标注为 `=target units`，否则 §5 Phase 8 的「cost per minute by pair」算不出来。

### 4.5 Translator / S2 Prompt Registry + fail-closed

按 pair 选模板（不塞巨型 prompt）：

```python
TRANSLATION_PROMPTS = {("en","zh-CN"): DEFAULT_EN_TO_ZH_PROMPT, ("zh-CN","en"): DEFAULT_ZH_TO_EN_PROMPT}
PROBE_PROMPTS = {...}   # 核查补：probe 独立模板 + 独立 override key
S2_PROMPTS = {...}      # 核查补：Pass1/2/3 + glossary 方向
```

输出 JSON：`[{"segment_id":1,"target_text":"..."}]`；parser 优先 `target_text`、缺失 `cn_text`；写出 canonical `cn_text`（见 §4.3）。

**admin override fail-closed（扩展到 S2）**：Translator + probe + **S2 三个 override key（pass1/2/3）**，非默认 pair 若 override 不含 language-aware 声明 → fail-closed 回默认模板，不拿中文 prompt 翻英文。

### 4.6 付费 API 语言错配 fail-closed（项目硬约束）

项目硬约束：付费 API 不得在错误/兜底语义下被调用。非默认 pair 下 probe translate / S2 三轮 / suggest-split 多模态会带语义错误 prompt 烧 Gemini 钱并产垃圾。

**总闸**：`LanguagePairProfile.capabilities` per-capability 适配位（`probe/s2/suggest_split`）。未适配 → skip / 501 / 拒绝调用并显式报错，**绝不带错 prompt 调用付费 API**。首发 `zh-CN→en` capabilities 不含 `suggest_split`/`post_edit`（§2.2）。

### 4.7 Quality Report（target-aware，shadow-only）

| target | 检测 |
|---|---|
| zh-CN | CJK 占比、Latin-only 风险（现有） |
| en | Latin/word 占比、CJK-dominant 风险 |
| ja | Kana/Kanji presence |
| ko | Hangul presence |
| es/fr/de | Latin + accent tolerant |

全部 shadow-only，报告加 `source_language/target_language/language_pair/gate_mode:"detect_only"`。

### 4.8 服务模式边界

| 模式 | v1 | 备注 |
|---|---|---|
| Studio | 首发（zh→en），但禁 post-edit/suggest-split | 人工兜底，风险最低 |
| Express | 第二批 | 须先验 TTS provider target 兼容 + preset pool |
| Smart | 第三批 | auto translation/voice review 报告带语言 + shadow 稳定后 |
| Free / Anonymous | 不扩 | 避免未验证多语种成为付费 API 放大器；匿名 lane 白名单不动 |

---

## 5. 分阶段执行计划（工期修正）

> **工期总览**：v1 估 10.5-15 人日；核查后修正为 **4-6 周（单 owner 折算）**。失真最大的是 Phase 6（字幕 1-2 天 → per-script 引擎 1-2 周）与 Phase 4（漏 `_count_source_words`/probe/process.py 两处 ×1.8）。

### Phase 0：能力矩阵与测试夹具（1-2 天）

1. `docs/research/multilingual-provider-capability-matrix.md`：ASR/Translator/TTS/Whisper/subtitle per-language 支持（含 AssemblyAI disfluencies/diarization 可用性、VolcEngine explicit_language 实测结论引 `scripts/test_volcengine_explicit_language.py`）。
2. 离线 fixture：en→zh 现有 + zh-CN→en 2-3 段中文短访谈。
3. fake translator/transcriber 测试 + **golden snapshot 基线录制**（见 §8）。

### Phase 1：语言事实入库 + API 贯通 + 灰度开关（2-3 天）

1. alembic 036 + models lockstep + JobRecord 字段（NOT NULL server_default）。
2. job_intercept allowed pair 校验；**PG insert / metering_snapshot / copy_as_new 三处显式加字段**。
3. entitlements `get_effective_allowed_language_pairs` + admin_settings `language_pairs_enabled`/allowlist + **admin 前端开关 UI**。
4. **匿名 lane 声明**：白名单不动 + 回归断言。
5. 前端 `CreateTranslationJobInput` 可选语言字段；facts 端点；Job summary 返回语言。
6. **部署 runbook 进 PR 验收**（migration 先行 → gateway/app 滚动；`--sql` dry-run；compose env 变更先查 in-flight pipeline，`feedback_compose_env_file_recreate`）。

验收：缺省 `en->zh-CN`；非法 pair Gateway 拒；匿名 lane 不炸；前端从 facts 读。

### Phase 2：source-aware gate + ASR 参数化（2-3 天）

1. `_enforce_english_*` → `_enforce_source_language/_enforce_transcript_language(expected)`；**保留 metadata 缺失/auto 放行（fail-open）+ 回归用例**。
2. AssemblyAI：language_code 参数化 + **per-language disfluencies/filler/speech_models/diarization profile**（核查补）。
3. Gemini transcriber prompt 加 source language；TranscriptResult.language 缺失改 fail-closed。
4. **AssemblyAI 转录行构建**：SENTENCE_END/join_tokens 按 script family 分派（CJK 全角标点 + 无空格连词）。
5. transcript.json 记 source language。

验收：en→zh 老测试不变；zh-CN fake transcript 不被英文占比拒；本地上传（无 metadata）仍放行。

### Phase 3：Translator + Rewriter + Probe 语言化（合并原 PR-C/D，3-5 天）

1. language_pair profile + prompt registry（translation + probe + rewrite）。
2. `translate()` / `translate_probe()` 接受 source/target；`_build_prompt` 注入语言 + 单位 + 输出字段。
3. parser 兼容 `target_text/cn_text`；canonical 写 `cn_text`（§4.3）。
4. **`_count_source_words` 按 source script 分词**；reference wps 机制随之。
5. **长度三件套统一换 spoken unit**：`_estimate_dynamic_target_chars`（5 处 ×1.8）+ `_count_cn_chars` 重试 gate + rewriter 字数口径。
6. GeminiRewriter **3 条 prompt 路径全覆盖**（含 compact prompt + 硬编码中文尾巴）。
7. **fingerprint default-pair-preserving**（C5）：仅非默认 pair 注入新键 / 版本化 + 默认 pair 锚定旧算法 + prompt 模板 hash 纳入；回归断言默认 pair fingerprint = 录制哈希。

验收：en→zh 输出 + fingerprint 不变；zh→en `target_text` 双解析；中文 prompt 不用于 zh→en；probe 不用中文 prompt 校准英文 cps。

### Phase 4：长段修复 + voice speed 单位 + 编排耦合（2-3 天）

1. 长段修复 split 正则按 source/target script 分派（`process.py:342-343`）。
2. voice_reranker speed 维度读 target unit type；未知禁用 speed 权重；**per-language cps 不可比问题**：catalog hanzi/sec 值在 en target 下标记不可比或重校准。
3. **process.py ×1.8 第二份拷贝**（`:7909-7932`）引 registry。

### Phase 5：音色兼容 + matchable 原子迁移（3-5 天）

1. **matchable 原子迁移（blocker）**：`compatible_target_languages` 回填 + `matchable=true` 回填 volcengine en + 新过滤上线，**同 PR 原子** + **独立 admin kill switch**（关闭即回 legacy 行为）。顺序：先部署新过滤代码（kill switch 默认关）→ DB 回填 → 开 switch。
2. internal `/api/internal/voice-catalog` 支持 `target_language` query + flatten。
3. VolcEngine/CosyVoice selector 消费 target_language；**voice_selection 音色池 builder（`process.py:7726`）去中文硬编码**。
4. **跨 provider fallback 链语言感知**：`get_fallback_provider` 对非默认 pair 不静默路由到中文-only CosyVoice；Gateway pool 不可用时 fail-closed。
5. **TTS 合成 payload 注入语言参数**（MiniMax language_boost / VolcEngine explicit_language）。
6. **user_voices 语言 migration（v1 未列）** + 3 条写入路径 stamping（手动 clone / Express auto-clone / Smart auto-clone）。
7. DubbingSegment/segments.json 加 per-segment target_language（MiniMax 贯通锚点）。

验收：zh target 不返回 Volc en-only；en target 返回兼容音色（依赖 matchable 回填）；fallback 不污染；MiniMax 不回中文池除非配置允许 + audit。

### Phase 6：字幕 per-script 引擎 + 输出通用化（**1-2 周，非 1-2 天**）

1. **`ensure_whisper_alignment` 第二写出器同步改造**（blocker）：不再写死 `subtitles_zh.srt`。
2. **字幕层 per-script**：semantic_segmenter（标点边界 + 等效长度）、cue_timing、whisper DTW（char-vs-word + 数字归一）按 script family 分派；cue whisper language 参数化。
3. canonical cue / dispatcher 写 source/target；新增 `subtitles_source/target.srt`；en→zh 继续写旧 `subtitles_zh/en.srt`。
4. **artifact key 同步面 ≥9 处**（§3.7）+ 回归守卫。
5. **下载文件名（RFC 6266）+ R2 registry 持久化**：按 target language 生成，不写死 `_zh`。
6. quality report target-aware；artifact index 标 subtitle language。

验收：en→zh 下载 key/文件名不变；zh→en 不生成语义错误的 `subtitles_zh` 主文件；剪映 draft 文本=目标语言；新 key 前端可见。

### Phase 7：前端灰度 UX（1-2 天，不含 review UI）

1. TranslationForm 语言方向 selector（默认「英文 → 中文」）；非 GA pair「内测」标识（admin/allowlist 可见）。
2. 下载列表标签语言字段驱动（「目标语言字幕」/「源语言字幕」）。
3. 服务模式卡片按 pair 刷新；不可用解释为「该语言方向暂不支持此模式」。
4. 切换 pair 清理不兼容音色选择 + consent。

> **review UI 语言中立化**（cnText 标签 + 英文词边界拆分算法按 script 分派）是 **GA 前置独立工作项**，不在首发；首发 zh→en 经 §2.2 non-goal 禁入 post-edit。

### Phase 8：灰度上线 + 数据闭环（持续）

顺序：Internal Studio zh→en → Allowlist Studio → Express（TTS 兼容覆盖后）→ Smart（shadow 稳定后）→ 更多 pair。

观测（落点见 §4.4 metering）：翻译长度超限率、TTS first-pass duration error、DSP/rewrite/force_dsp 分布、wrong-script rate、voice fallback rate、post-edit text edit rate、cost per minute by pair。

---

## 6. PR 拆分与多 agent 合并顺序（重做）

v1 的 PR-A..G 缺 pipeline wiring owner、误拆 C/D、漏 S2/probe/review-UI。修订：

| PR | 范围 | 文件领地 | 依赖 |
|---|---|---|---|
| **PR-A** | language registry + Job 字段贯通 + Gateway facts/校验 + 灰度开关 + admin UI + migration 036 + 匿名 lane 声明 | gateway + 前端 types + registry | — |
| **PR-W** | **pipeline wiring（新增）**：`_snap` 读取 + 构造 language_profile + 透传各阶段调用点（先 no-op 默认值） | `process.py`（**单一 owner 独占**） | A |
| **PR-CD** | **translator + rewriter + probe 语言化（合并 C/D）**：prompt registry + parser + fingerprint preserving + 长度三件套 + source 分词器 | `translator.py`+`rewriter.py`（**单一 owner 独占**） | W |
| **PR-B** | source-aware gate + ASR per-language profile + 转录行构建 script 分派 | `assemblyai/`+`gemini/transcriber.py` | W |
| **PR-H** | **S2 语言化（新增）**：三轮 prompt + glossary 方向 + S2 override fail-closed + legacy 死代码裁决 | `transcript_reviewer.py` | W |
| **PR-E** | 音色兼容 + **matchable 原子迁移** + 跨 provider fallback + 合成 payload 语言参数 + user_voices migration + 音色池 builder 去中文 | `tts/`+gateway voice_catalog | W |
| **PR-F** | 字幕 per-script 引擎 + 第二写出器 + artifact key ≥9 处 + 下载文件名 + quality report | `subtitles/`+output+下载层 | W |
| **PR-G** | 前端灰度入口（不含 review UI） | 前端 | A, F |

**合并顺序**：`A → W → CD → {B、H、E、F 四路并行} → G`。B/H/E/F 文件领地不相交，可并行派发给不同 agent。

**多 agent 硬约束**（CLAUDE.md 多 actor 底线的文件级细化）：

- `process.py` 归 PR-W owner，`translator.py`/`rewriter.py` 归 PR-CD owner；期间其他 agent 不得触碰这两个文件。
- 各 agent 在自己 worktree + feature 分支（`claude/ml-*` / `codex/ml-*`），完成由项目主 review 合并回 main。
- PR-A 跨 3 个部署单元（alembic + gateway + 前端），部署 runbook 进 PR-A 验收。

---

## 7. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| **matchable 死锁**（blocker） | Phase 5 验收不可达 / 复发 2026-04-15 事故 | PR-E 原子迁移 + 独立 kill switch + 顺序约束 |
| **第二字幕写出器覆盖正确产物**（blocker） | zh→en 生成剪映草稿即被 zh whisper 重建覆盖 | PR-F 同步改 `ensure_whisper_alignment` |
| **target_text 双写漂移**（blocker） | post-edit 后新消费者读 stale 文本 | §4.3 单写裁决：cn_text canonical + 元数据，不双持久化 |
| **checkpoint fingerprint bust** | 现网 en→zh 整批付费重翻 | default-pair-preserving + 回归锚定哈希 |
| **付费 API 错 prompt 调用** | 违反项目硬约束 + 烧钱产垃圾 | §4.6 per-capability 适配位 fail-closed |
| 中文源静默退化（_count_source_words 等） | 长度/探针/audience 默默用默认值 | 所有 Latin-only 计数器按 script 分派；fixture 覆盖 |
| 跨 provider fallback 语言盲区 | en target 失败路由到中文池 | fallback 链语言感知 + fail-closed |
| 匿名 lane 绕过校验 | 加必填字段当场 500 | 白名单不动 + JobRecord 缺省锁定 + 回归 |
| review UI 源=英文假设 | 首发审校语义错乱 | §2.2 non-goal 禁入 + GA 前独立工作项 |
| Gateway/前端 facts 漂移 | 价格/权益不一致 | facts 单一真源 + label 唯一展示名 |
| 工期低估 | 范围蔓延 | DoD 收窄（§10）+ non-goals 显式 |

---

## 8. 测试与零回归策略

**零回归四件套**（v1 §10 条件 1「零回归」无可执行证明手段，此处补）：

1. **golden snapshot 测试**（进默认 pytest）：fake provider 全管线跑 en→zh fixture，byte/结构级 diff `segments.json`、cue JSON、artifact index、字幕文件集。每个 PR 前后对比。
2. **fingerprint 稳定性测试**：默认 pair `_build_translation_fingerprint` 输出锚定录制哈希，防 checkpoint 失效。
3. **set-diff 基线对照**：本机有约 335 条预先存在失败（`feedback_test_database_stub_convention`），每 PR 验收用 set-diff 而非绝对绿。
4. **生产 shadow 期**：Phase 1-6 全部落库但 Gateway pairs 锁死只剩默认 pair，跑 N 天对比部署前后默认 pair 的 first-pass duration error / fallback rate / 翻译重试率，再开 selector。

**新增无网络测试**：`test_language_registry`、`test_job_language_fields`（含匿名白名单断言）、`test_source_language_gate`（含 metadata 缺失放行）、`test_assemblyai_language_profile`、`test_translator_language_pair`（含 fingerprint 稳定）、`test_length_profile`、`test_source_word_count_by_script`、`test_voice_target_language_filter`（含 matchable 回填语义）、`test_multilingual_subtitle_outputs`、`test_translation_quality_multilingual`、`test_paid_api_capability_gate`。

真实 API 冒烟只放灰度手工/脚本，不进默认 pytest。

---

## 9. 迁移与回滚

### 9.1 数据迁移

历史任务默认 `en / zh-CN / en->zh-CN`；历史 segments.json 无 target_text 时 `target_text=cn_text`、`target_language=zh-CN`。**copy_as_new 副本行**显式加语言字段（漏加静默回落缺省，`feedback_copy_as_new_invariants`）。

### 9.2 回滚（硬约束，修正 v1 两个洞）

**区分 config 回滚 vs 代码回滚**：

- 回滚**只动 Gateway pairs + admin kill switch**（含 PR-E 新过滤独立开关，关闭即回 legacy）；**代码与 DB 状态保持前滚，不 revert**。
- 代码 revert 的两个后果（v1 未识别）：(a) 新 fingerprint 算法下创建的任务在旧代码 resume 时哈希必失配 → 付费重翻；(b) PR-E 的 matchable/compatible_target_languages 是 DB 状态，revert 代码后旧 selector 复发 2026-04-15 事故。
- **queued 取消退款**必须走 `mirror_job_terminal_state` + `settle_job_credit_ledger` 单一终态入口（`feedback_terminal_state_single_entry`），否则重演扣点少算。
- 已 succeeded 的 zh→en 任务其 R2 registry 持久化 filename 不随回滚清理，属可接受残留。

回滚不需 DB downgrade（字段 additive + 默认兼容）。

---

## 10. 最小可上线定义（收窄）

第一版「可上线」满足：

1. **en→zh-CN 零回归**，由 golden snapshot + fingerprint 锚定 + shadow 期三重证明（不靠「老测试不变」）。
2. **zh-CN→en 在 Studio 非交互主干**完成：转录 → 翻译 → TTS → DSP 对齐 → 源/目标/双语字幕 → 剪映草稿。
3. 不兼容音色不进候选池（matchable 回填 + 新过滤原子上线）。
4. 质量报告识别目标语言脚本异常（shadow）。
5. Gateway 能关闭该 pair（双层 kill switch）。
6. 默认测试不依赖真实外部 API。
7. **non-goals 对内测用户显式声明**：禁 post-edit / suggest-split，字幕只承诺内容正确。

达成后再讨论 Express / Smart / Free / Anonymous 与 review UI 中立化。

---

## 11. 最终建议

**建议立项，优先级中高，但按 v2 修订执行。**

理由：

- 架构主干成熟，不需推翻 `DubbingSegment` 主路径 / DSP-first / 剪映交付。
- 2026-04-15 旧方案已预留 target_language 方向；MiniMax selector、voice catalog、JobRecord、UsageMeter、report sidecar 都有可扩点。
- 最大风险不是算法，而是**语言假设的静默耦合**散布在编排层、ASR 行构建、S2 glossary、字幕 per-script 引擎、matchable 止血、第二写出器、绕过校验的匿名 lane——这些 v1 草稿大半遗漏，是工期被低估 2-3 倍的根因。本 v2 把每一层都锁进了对应 PR 与回归。

**推荐首发**：中文视频 → 英文配音，Studio 内测，禁 post-edit/suggest-split。

**不推荐**：一次性开放任意语言互翻，或直接把 Smart/Free/Anonymous/post-edit 拉进多语种。

---

## 12. 核查记录（可追溯）

**方法**：6 域并行核查 22 条 v1 声明（逐条实读工作树源码）+ 2 路架构评审；3 个 blocker 锚点主程亲验。

**声明核查**：0 refuted / 19 confirmed / 3 partial。partial 三条：(1) 「SemanticBlock 是基本 TTS 单元」→ 实为 `DubbingSegment.cn_text`（C1）；(2) 「UsageMeter extra 可记 language_pair」→ `record_tts` 无 extra（C3）；(3) 「前端无硬编码语言对」→ 提交模型确无，但标签映射/审校 UI/营销 SEO 大量硬编码 en→zh。

**3 个 blocker（改变方案结论）**：

1. **target_text 单写裁决缺失** — 双写与 post-edit `cn_text` 白名单互斥（`editing_segments.py:98-99` 实测）。→ §4.3 裁决不双写。
2. **第二字幕写出器 + 字幕 per-script 引擎** — `ensure_whisper_alignment.py:404-410` 写死 zh（实测）；Phase 6 实为 1-2 周。→ §3.7 / Phase 6。
3. **matchable 止血死锁** — `voice_catalog_api.py:160` 硬过滤 matchable（实测）；v1 全文无 matchable 一词。→ §3.5 / Phase 5 原子迁移。

**主程亲验锚点**：`voice_catalog_api.py:160`（`.where(VoiceCatalog.matchable == True)`）✅；`editing_segments.py:98-99`（`PATCHABLE_SEGMENT_FIELDS` 含 `cn_text` 不含 `target_text`）✅；`ensure_whisper_alignment.py:404-410`（写死 `subtitles_zh.srt`+`subtitles.srt` alias）✅。

**架构评审 verdict**：方向正确、证据底座扎实，additive 默认值 + Gateway 单一真源 + 灰度顺序符合仓库惯例；但 3 个 blocker + PR 拆分缺 wiring owner / 误拆 C-D / 漏 S2-probe-review-UI + 工期低估 2-3 倍 + 零回归与回滚缺可执行机制，需中等偏大修订后可作为执行基线。本 v2 已吸收全部修订。
